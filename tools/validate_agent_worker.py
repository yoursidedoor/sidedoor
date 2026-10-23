import argparse
import contextlib
import datetime as dt
import io
import json
from pathlib import Path
import re
import sqlite3
import sys

sys.dont_write_bytecode = True
PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
MARKER = "agent-worker-audit.json"
PUBLIC_ROLE = re.compile(r"https://(?:(?:job-boards|boards)\.greenhouse\.io/[\w-]+/jobs/\d+|(?:jobs\.ashbyhq\.com|jobs\.lever\.co)/[\w-]+/[0-9a-fA-F-]{36})/?")


def checked_root(value):
    root = Path(value).expanduser().resolve()
    if root == PROJECT or root in PROJECT.parents:
        raise ValueError("Production and ancestor roots are forbidden")
    if root.is_relative_to(PROJECT):
        raise ValueError("Only isolated roots outside the project are permitted")
    if root.exists() and any(path.is_symlink() or (path.is_file() and path.stat().st_nlink != 1)
                             for path in root.rglob("*")):
        raise ValueError("Audit roots cannot contain symbolic or hard links")
    return root


def connect(root, mode="ro"):
    database = sqlite3.connect((root / "state/orchestrator.sqlite3").as_uri() + "?mode=" + mode)
    database.row_factory = sqlite3.Row
    if mode == "ro":
        database.execute("PRAGMA query_only=ON")
    return database


def configuration(root):
    if json.loads((root / MARKER).read_text()) != {"root": str(root)}:
        raise ValueError("Root was not prepared by this harness")
    config = json.loads((root / "config.json").read_text())
    if set(config) != {"codex", "model", "timezone", "agent_worker", "applications_enabled"}:
        raise ValueError("Unexpected configuration fields")
    if config["agent_worker"] != {"enabled": True} or config["applications_enabled"] is not False:
        raise ValueError("Read-only audit configuration required")
    return config


def summary(root, database):
    tasks = [dict(row) for row in database.execute(
        "SELECT task_id,worker,status,attempts,effect,result FROM workflow_tasks ORDER BY task_id")]
    for task in tasks:
        result = json.loads(task.pop("result") or "{}")
        basis = result.get("trace_basis")
        task["trace_basis"] = basis if basis in {"EXECUTOR_TOOL_EVENTS", "INJECTED_EXECUTOR_TRACE"} else None
        task["trace_attempt"] = result.get("attempt") if type(result.get("attempt")) is int else None
    if any(task["effect"] != "READ" or task["worker"] != "agent-investigation" for task in tasks):
        raise ValueError("Only READ agent-investigation tasks are permitted")
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    counts = {name: database.execute("SELECT COUNT(*) FROM " + name).fetchone()[0] if name in tables else 0
              for name in ("application_jobs", "application_attempts", "drafts", "workflow_task_events")}
    references = []
    for path in sorted(root.glob("reports/agent-worker/*/*/trace.jsonl")):
        for number, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            item = event.get("item", event) if isinstance(event, dict) else None
            if isinstance(item, dict) and item.get("type") in {"web_search", "web_search_call"}:
                references.append({"file": str(path.relative_to(root)), "line": number,
                                   "basis": next((task["trace_basis"] for task in tasks if task["task_id"] == path.parent.parent.name and str(task["trace_attempt"]) == path.parent.name), None),
                                   "completed": event.get("type") == "item.completed" or item.get("status") == "completed"})
    return {"tasks": tasks, "counts": dict(counts, tasks=len(tasks), write_tasks=0,
            attempts=sum(task["attempts"] for task in tasks), web_tool_events=len(references)),
            "web_tool_evidence_refs": references,
            "real_completed_web_events": sum(item["completed"] and item["basis"] == "EXECUTOR_TOOL_EVENTS" for item in references)}


def prepare(root):
    if (root / MARKER).exists():
        configuration(root)
        return
    if root.exists() and any(root.iterdir()):
        raise ValueError("New audit root must be empty; existing audit roots are preserved")
    import agent_worker
    import workflow_tasks
    assets = {name: (PROJECT / name).read_bytes() for name in ("agent-worker-prompt.md", "agent-worker-schema.json")}
    now = dt.datetime.now(dt.timezone.utc)
    with contextlib.closing(connect(PROJECT)) as source_database:
        source = next((payload["url"] for row in source_database.execute("SELECT payload FROM application_jobs ORDER BY rowid")
                       if isinstance(payload := json.loads(row[0]), dict) and isinstance(payload.get("url"), str)
                       and PUBLIC_ROLE.fullmatch(payload["url"]) and "aiapply" not in payload["url"].lower()), None)
    if source is None:
        raise ValueError("No canonical public role URL found")
    original = json.loads((PROJECT / "config.json").read_text())
    config = {key: original[key] for key in ("codex", "model", "timezone")}
    config.update(agent_worker={"enabled": True}, applications_enabled=False)
    (root / "state").mkdir(parents=True)
    (root / "reports").mkdir()
    (root / "config.json").write_text(json.dumps(config, indent=2) + "\n")
    for name, content in assets.items():
        (root / name).write_bytes(content)
    with contextlib.closing(connect(root, "rwc")) as database:
        workflow_tasks.initialize(database)
        agent_worker.enqueue(database, action_id="manual-public-role-read", parent_run_id="manual-agent-validation",
            question="Read only this official role URL using the web tool; report its title and availability with source evidence. "
                     "The URL was read from a historical local queue, not freshly verified. Never access application forms, "
                     "fill fields, upload, submit, send, access accounts, or inspect candidate data. If unreadable, report that.",
            sources=[{"source": source, "observed_at": now.isoformat()}],
            deadline=(now + dt.timedelta(days=1)).isoformat(), now=now.isoformat())
        database.commit()
    (root / MARKER).write_text(json.dumps({"root": str(root)}) + "\n")


def main():
    parser = argparse.ArgumentParser(description="Isolated manual validation of the real read-only agent hook")
    parser.add_argument("command", choices=("prepare", "run", "inspect"))
    parser.add_argument("--root", required=True)
    arguments = parser.parse_args()
    root = checked_root(arguments.root)
    if arguments.command == "prepare":
        prepare(root)
    config = configuration(root)
    with contextlib.closing(connect(root, "rw" if arguments.command == "run" else "ro")) as database:
        summary(root, database)
        if arguments.command == "run":
            import orchestrator
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                orchestrator.agent_cycle(root, config, database)
            database.commit()
        # Machine contract (E2.T6): the JSON result; stays print.
        print(json.dumps(summary(root, database), indent=2))

if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Machine contract (E2.T6): the JSON result; stays print.
        print(json.dumps({"state": "FAILED", "error_type": type(error).__name__}))
        sys.exit(1)
