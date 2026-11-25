"""Codex-side stdio server for Claude Desktop probes and local deployments."""

import ctypes
import locale
import os
import re
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from probe import open_db, read_probe
import deployments


ROOT = Path(__file__).resolve().parent
DATABASE = ROOT / "probes.sqlite3"
SERVER = FastMCP("claude-desktop-handoff")


def validate_task_id(task_id: str) -> None:
    if not re.fullmatch(r"DEP-PROBE-[0-9A-F]{16}", task_id):
        raise ValueError("Use DEP-PROBE- followed by exactly 16 uppercase hex digits.")


@SERVER.tool()
def get_handoff_status(task_id: str) -> dict:
    """Read receipt, latest sender stage, stage time and launcher error without sending."""
    validate_task_id(task_id)
    with closing(open_db(DATABASE)) as db:
        return read_probe(db, task_id)


@SERVER.tool()
def get_deployment_status(task_id: str, include_log: bool = False) -> dict:
    """Read persistent deployment progress and verification evidence. Never resubmits."""
    return deployments.get_status(task_id, include_log)


@SERVER.tool()
def submit_deployment(task_id: str, project_path: str, summary: str, mode: str = "deploy") -> dict:
    """Ask Claude Desktop to deploy the current local checkout to Docker Desktop performance.

    Use a fresh DEP- plus 16 uppercase hex digits and an absolute project_path.
    mode=preflight only checks prerequisites and renders templates.
    mode=deploy builds an image, upgrades the existing performance release (including
    its migration job), and verifies rollouts and health. The fixed profile requires
    this project's Dockerfile, scripts/deploy-performance.ps1 and local Helm overlay.
    Retry queries with the original task_id. Identical submissions never send twice.
    After submission, wait with get_deployment_status until a terminal outcome.
    """
    status, created = deployments.submit(task_id, project_path, summary, mode)
    if created:
        attempt, error = launch_sender(task_id)
        if error:
            deployments.record_dispatch(task_id, attempt, error)
        status = deployments.get_status(task_id)
    return status


@SERVER.tool()
def cancel_pending_deployment(task_id: str, reason: str) -> dict:
    """Cancel an undelivered or received task whose deployment worker has not started."""
    return deployments.stop_pending(task_id, reason)


def windows_environment() -> dict[str, str]:
    env = os.environ.copy()
    if not env.get("SYSTEMROOT"):
        directory = ctypes.create_unicode_buffer(32768)
        size = ctypes.windll.kernel32.GetWindowsDirectoryW(directory, len(directory))
        if not 0 < size < len(directory):
            raise OSError("Windows system directory lookup failed")
        env["SYSTEMROOT"] = directory.value
    # stdio clients commonly supply only PATH and user identity. Windows CLI
    # plugins and credential helpers also depend on the standard special folders.
    folders = {"USERPROFILE": 0x28, "APPDATA": 0x1A, "LOCALAPPDATA": 0x1C,
               "PROGRAMDATA": 0x23, "PROGRAMFILES": 0x26, "PROGRAMFILES(X86)": 0x2A}
    for name, folder_id in folders.items():
        if not env.get(name):
            directory = ctypes.create_unicode_buffer(260)
            if ctypes.windll.shell32.SHGetFolderPathW(None, folder_id, None, 0, directory) != 0:
                raise OSError(f"Windows {name} directory lookup failed")
            env[name] = directory.value
    if not env.get("TEMP"):
        directory = ctypes.create_unicode_buffer(32768)
        size = ctypes.windll.kernel32.GetTempPathW(len(directory), directory)
        if not 0 < size < len(directory):
            raise OSError("Windows temporary directory lookup failed")
        env["TEMP"] = directory.value
    env.setdefault("TMP", env["TEMP"])
    return env


def launch_sender(task_id: str) -> tuple[str, str | None]:
    try:
        env = windows_environment()
        powershell = Path(env["SYSTEMROOT"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        # Desktop descendants can inherit stderr. A file lets us read diagnostics
        # without hanging on a pipe held by a long-lived GUI process.
        with tempfile.TemporaryFile() as errors:
            try:
                result = subprocess.run(
                    [str(powershell), "-NoProfile", "-NonInteractive", "-File",
                     str(ROOT / "Test-DesktopHandoff.ps1"), "-PythonPath", sys.executable,
                     "-TaskId", task_id, "-SendOnly"],
                    env=env,
                    cwd=ROOT,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=errors,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    timeout=120,
                    check=False,
                )
                if result.returncode == 0:
                    return "send_invoked", None
                detail = f"PowerShell exited with code {result.returncode}"
            except subprocess.TimeoutExpired:
                detail = "PowerShell timed out after 120 seconds"
            length = errors.seek(0, os.SEEK_END)
            errors.seek(max(0, length - 4096))
            data = errors.read(4096)
            try:
                log = data.decode("utf-8").strip()
            except UnicodeDecodeError:
                log = data.decode(locale.getpreferredencoding(False), errors="replace").strip()
            if log:
                detail += "\n" + log
            return "send_unconfirmed", detail
    except OSError as error:
        return "launcher_unavailable", str(error)


@SERVER.tool()
def send_handoff_probe(task_id: str) -> dict:
    """Send a fixed connection test to Claude Desktop. This does not deploy.

    Choose a fresh DEP-PROBE- plus 16 uppercase hex digits for each new test.
    Reuse the same ID after an interruption. It can trigger the sender only once.
    After sending, poll get_handoff_status for an acknowledged receipt.
    """
    validate_task_id(task_id)
    # Reserve and commit before opening the desktop. A retry or concurrent call
    # can observe the original probe even if the first caller was disconnected.
    with closing(open_db(DATABASE)) as db, db:
        inserted = db.execute(
            "INSERT INTO probes(task_id) VALUES (?) ON CONFLICT(task_id) DO NOTHING",
            (task_id,),
        ).rowcount
    if not inserted:
        return {**get_handoff_status(task_id), "send_attempt": "already_exists"}

    attempt, error = launch_sender(task_id)
    if error:
        with closing(open_db(DATABASE)) as db, db:
            db.execute("UPDATE probes SET launcher_error = ? WHERE task_id = ?", (error, task_id))
    return {**get_handoff_status(task_id), "send_attempt": attempt}


if __name__ == "__main__":
    SERVER.run(transport="stdio")
