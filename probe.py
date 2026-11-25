"""Claude-side MCP receiver for probes and claimed local deployment tasks."""

import argparse
import json
import re
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

STAGES = ("runtime_ready", "launching_desktop", "waiting_for_editor", "invoking_send", "send_invoked")
DIAGNOSTIC_COLUMNS = ("stage", "stage_at", "launcher_error")


def open_db(db_path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(db_path, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        db.execute(
            "CREATE TABLE IF NOT EXISTS probes ("
            "task_id TEXT PRIMARY KEY, receipt TEXT, acknowledged_at TEXT,"
            " stage TEXT, stage_at TEXT, launcher_error TEXT)"
        )
        columns = {r["name"] for r in db.execute("PRAGMA table_info(probes)")}
        if not columns.issuperset(DIAGNOSTIC_COLUMNS):
            with db:
                db.execute("BEGIN IMMEDIATE")
                # Another client may have upgraded the database while we waited.
                columns = {r["name"] for r in db.execute("PRAGMA table_info(probes)")}
                for column in DIAGNOSTIC_COLUMNS:
                    if column not in columns:
                        db.execute(f"ALTER TABLE probes ADD COLUMN {column} TEXT")
    except BaseException:
        db.close()
        raise
    return db


def new_probe(db_path: Path) -> str:
    task_id = "DEP-PROBE-" + secrets.token_hex(8).upper()
    with closing(open_db(db_path)) as db, db:
        db.execute("INSERT INTO probes(task_id) VALUES (?)", (task_id,))
    return task_id


def read_probe(db: sqlite3.Connection, task_id: str) -> dict:
    row = db.execute("SELECT * FROM probes WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        raise ValueError("Unknown task ID")
    return {**dict(row), "status": "acknowledged" if row["receipt"] else "pending"}


def record_stage(db: sqlite3.Connection, task_id: str, stage: str) -> None:
    if stage not in STAGES:
        raise ValueError("Unknown handoff stage")
    db.execute("BEGIN IMMEDIATE")
    previous = read_probe(db, task_id)["stage"]
    if previous is None or STAGES.index(previous) < STAGES.index(stage):
        db.execute(
            "UPDATE probes SET stage = ?, stage_at = ? WHERE task_id = ?",
            (stage, datetime.now(timezone.utc).isoformat(), task_id),
        )


def serve(db_path: Path) -> None:
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("cw-deploy-probe")

    import deployments

    @server.tool()
    def claim_deployment(task_id: str, project_path: str) -> dict:
        """Read and claim the exact deployment requested by Codex.

        Confirm the project's absolute path. Returns the fixed deployment plan,
        source fingerprint, target and claim_token needed by start_deployment.
        """
        return deployments.claim(task_id, project_path)

    @server.tool()
    def start_deployment(task_id: str, claim_token: str) -> dict:
        """Start the claimed Docker Desktop task once, then return immediately.

        The worker performs preflight or build/deploy/verify according to the task,
        including the existing Helm migration job. Poll get_deployment_status.
        """
        return deployments.start(task_id, claim_token)

    @server.tool()
    def get_deployment_status(task_id: str, include_log: bool = False) -> dict:
        """Read progress, command errors, log paths and verified deployment results."""
        return deployments.get_status(task_id, include_log)

    @server.tool()
    def report_deployment_blocked(task_id: str, reason: str) -> dict:
        """Report inability to start a received task. Valid only before the worker starts."""
        return deployments.stop_pending(task_id, reason, status="blocked")

    @server.tool()
    def acknowledge_probe(task_id: str) -> dict:
        """Acknowledge a local handoff probe and return its persistent receipt."""
        if not re.fullmatch(r"DEP-PROBE-[0-9A-F]{16}", task_id):
            raise ValueError("Invalid task ID")
        with closing(open_db(db_path)) as db, db:
            db.execute(
                "UPDATE probes SET receipt = ?, acknowledged_at = ? "
                "WHERE task_id = ? AND receipt IS NULL",
                (
                    "ACK-" + secrets.token_hex(8).upper(),
                    datetime.now(timezone.utc).isoformat(),
                    task_id,
                ),
            )
            return read_probe(db, task_id)

    server.run(transport="stdio")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path(__file__).with_name("probes.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve")
    commands.add_parser("new")
    commands.add_parser("status").add_argument("task_id")
    stage_parser = commands.add_parser("stage")
    stage_parser.add_argument("task_id")
    stage_parser.add_argument("stage", choices=STAGES)
    commands.add_parser("deployment-status").add_argument("task_id")
    commands.add_parser("deployment-prompt").add_argument("task_id")
    args = parser.parse_args()
    if args.command == "serve":
        serve(args.db)
    elif args.command == "new":
        print(new_probe(args.db))
    elif args.command == "stage":
        if args.task_id.startswith("DEP-PROBE-"):
            with closing(open_db(args.db)) as db, db:
                record_stage(db, args.task_id, args.stage)
        else:
            from deployments import record_dispatch
            record_dispatch(args.task_id, args.stage)
    elif args.command.startswith("deployment-"):
        from deployments import get_status
        task = get_status(args.task_id)
        if args.command == "deployment-status":
            print(json.dumps(task))
        else:
            project = json.dumps(task["request"]["project_path"])
            print(
                f"Execute deployment task {args.task_id} for project_path {project}. "
                "WaitForMcpServers servers=['cw-deploy-probe'] if its tools are still loading. "
                "Call cw-deploy-probe claim_deployment with that task_id and project_path, "
                "then start_deployment with the returned claim_token only if status is received. "
                "Poll get_deployment_status for the result. The worker alone executes the "
                "fixed requested preflight or build/deploy/verify plan and records evidence. "
                "Only use WaitForMcpServers and cw-deploy-probe tools for this request. "
                "Do not run terminal, shell, Docker, kubectl or Helm commands yourself. "
                "Do not edit files, create scripts, launch background jobs or repair failures. "
                "For terminal status succeeded, failed, blocked or cancelled, report the "
                "recorded result and stop immediately. A failed task must not be retried "
                "or continued outside these MCP tools. Codex owns any recovery decision."
            )
    else:
        with closing(open_db(args.db)) as db:
            print(json.dumps(read_probe(db, args.task_id)))


if __name__ == "__main__":
    main()
