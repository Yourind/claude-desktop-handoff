"""Persistent local deployment tasks shared by the two MCP endpoints."""

import hashlib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATABASE = ROOT / "deployments.sqlite3"
PROFILE = "docker-desktop-performance"
TERMINAL = {"succeeded", "failed", "cancelled", "blocked"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(DATABASE, timeout=15)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS deployments (
            task_id TEXT PRIMARY KEY,
            request TEXT NOT NULL,
            source TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            claim_token TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            heartbeat_at TEXT,
            worker_pid INTEGER,
            dispatch_stage TEXT,
            dispatch_error TEXT,
            error TEXT,
            result TEXT
        );
        CREATE TABLE IF NOT EXISTS deployment_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            at TEXT NOT NULL,
            stage TEXT NOT NULL,
            message TEXT NOT NULL
        );
    """)
    return db


def validate_id(task_id: str) -> None:
    if not re.fullmatch(r"DEP-[0-9A-F]{16}", task_id):
        raise ValueError("task_id must be DEP- followed by 16 uppercase hex digits.")


def row_for(db: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    row = db.execute("SELECT * FROM deployments WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        raise ValueError("Unknown deployment task")
    return row


def add_event(db: sqlite3.Connection, task_id: str, stage: str, message: str) -> None:
    db.execute(
        "INSERT INTO deployment_events(task_id, at, stage, message) VALUES (?, ?, ?, ?)",
        (task_id, now(), stage, message),
    )


def get_status(task_id: str, include_log: bool = False) -> dict:
    validate_id(task_id)
    with closing(connect()) as db:
        row = dict(row_for(db, task_id))
        row.pop("claim_token")
        row["request"] = json.loads(row["request"])
        row["source"] = json.loads(row["source"])
        row["result"] = json.loads(row["result"]) if row["result"] else None
        row["events"] = [dict(e) for e in db.execute(
            "SELECT at, stage, message FROM (SELECT * FROM deployment_events "
            "WHERE task_id = ? ORDER BY sequence DESC LIMIT 30) ORDER BY sequence",
            (task_id,),
        )]
    log = ROOT / "runs" / task_id / "deployment.log"
    row["log_path"] = str(log)
    command_logs = sorted(log.parent.glob("[0-9]*.log"))
    current_log = command_logs[-1] if command_logs else log
    row["current_command_log"] = str(current_log)
    if include_log and current_log.is_file():
        with current_log.open("rb") as stream:
            stream.seek(max(0, current_log.stat().st_size - 8000))
            row["log_tail"] = stream.read(8000).decode("utf-8", errors="replace")
    if row["heartbeat_at"] and row["status"] not in TERMINAL:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(row["heartbeat_at"])
        row["heartbeat_age_seconds"] = round(age.total_seconds())
    return row


def source_state(project: Path) -> dict:
    """Hash HEAD, its working-tree diff, and untracked source without rereading clean files."""
    def git(*args: str) -> bytes:
        # Git background helpers can keep inherited pipes open under stdio MCP.
        # Wait for Git itself and read ordinary files, matching the delegate launcher.
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            result = subprocess.run(
                ["git", "-C", str(project), *args],
                stdin=subprocess.DEVNULL, stdout=output, stderr=errors, timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW, check=False,
            )
            if result.returncode:
                errors.seek(0)
                raise ValueError(errors.read(2000).decode("utf-8", errors="replace"))
            output.seek(0)
            return output.read()
    top = Path(git("rev-parse", "--show-toplevel").decode().strip()).resolve()
    if top != project:
        raise ValueError("project_path must be the Git checkout root")
    head = git("rev-parse", "HEAD").decode().strip()
    digest = hashlib.sha256(head.encode())
    digest.update(git("diff", "--binary", "--no-ext-diff", "HEAD", "--"))
    names = set(git("ls-files", "-o", "--exclude-standard", "-z").split(b"\0"))
    for name in sorted(names - {b""}):
        path = project / os.fsdecode(name)
        digest.update(name + b"\0")
        if not path.exists():
            digest.update(b"deleted\0")
        elif path.is_file():
            with path.open("rb") as stream:
                digest.update(hashlib.file_digest(stream, "sha256").digest())
        else:
            raise ValueError(f"Unsupported source entry: {path}")
    return {"commit": head, "fingerprint": digest.hexdigest()}


def submit(task_id: str, project_path: str, summary: str, mode: str = "deploy") -> tuple[dict, bool]:
    validate_id(task_id)
    project = Path(project_path).expanduser().resolve(strict=True)
    if not Path(project_path).is_absolute():
        raise ValueError("Use an absolute project_path")
    if mode not in {"preflight", "deploy"} or not summary.strip():
        raise ValueError("Provide a summary and mode preflight or deploy")
    for file in ("Dockerfile", "scripts/deploy-performance.ps1",
                 "deployments/docker-desktop-performance/values.yaml", "charts/canway-quality/Chart.yaml"):
        if not (project / file).is_file():
            raise ValueError(f"The {PROFILE} profile requires {file}")
    request = json.dumps({
        "project_path": str(project), "summary": summary, "mode": mode,
        "profile": PROFILE, "context": "docker-desktop", "namespace": "performance",
        "release": "performance", "image": "canway-quality:local-mcp-" + task_id[4:].lower(),
    }, sort_keys=True)
    with closing(connect()) as db:
        prior = db.execute("SELECT request FROM deployments WHERE task_id = ?", (task_id,)).fetchone()
        if prior:
            if prior["request"] != request:
                raise ValueError("This task_id already belongs to a different request")
            return get_status(task_id), False
    source = source_state(project)
    with closing(connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("SELECT request FROM deployments WHERE task_id = ?", (task_id,)).fetchone()
        if prior:
            if prior["request"] != request:
                raise ValueError("This task_id already belongs to a different request")
            created = False
        else:
            active = db.execute(
                "SELECT task_id FROM deployments WHERE status NOT IN ('succeeded','failed','cancelled','blocked')"
            ).fetchone()
            if active:
                raise ValueError(f"Target environment already has active task {active['task_id']}")
            db.execute(
                "INSERT INTO deployments(task_id, request, source, created_at, updated_at) VALUES (?,?,?,?,?)",
                (task_id, request, json.dumps(source), now(), now()),
            )
            add_event(db, task_id, "queued", "Deployment request recorded")
            created = True
    return get_status(task_id), created


def record_dispatch(task_id: str, stage: str, error: str | None = None) -> None:
    validate_id(task_id)
    with closing(connect()) as db, db:
        previous = row_for(db, task_id)
        if error and previous["dispatch_stage"]:
            stage = previous["dispatch_stage"]
        db.execute(
            "UPDATE deployments SET dispatch_stage = ?, dispatch_error = ?, updated_at = ? WHERE task_id = ?",
            (stage, error, now(), task_id),
        )
        add_event(db, task_id, "dispatch", error or stage)


def claim(task_id: str, project_path: str) -> dict:
    validate_id(task_id)
    with closing(connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = row_for(db, task_id)
        request = json.loads(row["request"])
        if Path(project_path).resolve() != Path(request["project_path"]):
            raise ValueError("Project path differs from the submitted task")
        token = row["claim_token"]
        if row["status"] == "queued":
            token = secrets.token_hex(24)
            db.execute(
                "UPDATE deployments SET status='received', claim_token=?, updated_at=? WHERE task_id=?",
                (token, now(), task_id),
            )
            add_event(db, task_id, "received", "Claude receiver claimed deployment")
    return {**get_status(task_id), "claim_token": token,
            "next_action": "Call start_deployment with task_id and claim_token, then query status."}


def start(task_id: str, claim_token: str) -> dict:
    validate_id(task_id)
    with closing(connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = row_for(db, task_id)
        if not row["claim_token"] or not secrets.compare_digest(row["claim_token"], claim_token):
            raise ValueError("Invalid claim token")
        if row["status"] != "received":
            return get_status(task_id)
        db.execute("UPDATE deployments SET status='starting', updated_at=? WHERE task_id=?", (now(), task_id))
        add_event(db, task_id, "starting", "Claude requested the deployment worker")
    folder = ROOT / "runs" / task_id
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / "worker.log").open("ab") as log:
            child = subprocess.Popen(
                [sys.executable, "-B", str(ROOT / "deployment_worker.py"), task_id],
                cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True,
            )
        with closing(connect()) as db, db:
            db.execute("UPDATE deployments SET worker_pid=? WHERE task_id=?", (child.pid, task_id))
    except OSError as exc:
        finish(task_id, "failed", error=f"Worker could not start: {exc}")
    return get_status(task_id)


def progress(task_id: str, stage: str, message: str) -> None:
    with closing(connect()) as db, db:
        db.execute(
            "UPDATE deployments SET status=?, updated_at=?, heartbeat_at=? WHERE task_id=?",
            (stage, now(), now(), task_id),
        )
        add_event(db, task_id, stage, message)


def finish(task_id: str, status: str, result: dict | None = None, error: str | None = None) -> None:
    if status not in TERMINAL:
        raise ValueError("Invalid terminal status")
    with closing(connect()) as db, db:
        db.execute(
            "UPDATE deployments SET status=?, result=?, error=?, updated_at=? WHERE task_id=?",
            (status, json.dumps(result) if result else None, error, now(), task_id),
        )
        add_event(db, task_id, status, error or "Verification completed")


def stop_pending(task_id: str, reason: str, status: str = "cancelled") -> dict:
    validate_id(task_id)
    if status not in {"blocked", "cancelled"}:
        raise ValueError("Invalid pending-task outcome")
    with closing(connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = row_for(db, task_id)
        if row["status"] not in {"queued", "received"}:
            raise ValueError("Only a task whose worker has not started can be stopped this way")
        db.execute(
            "UPDATE deployments SET status=?, error=?, updated_at=? WHERE task_id=?",
            (status, reason, now(), task_id),
        )
        add_event(db, task_id, status, reason)
    return get_status(task_id)
