"""Bounded public-web investigations; caller commits and initializes the queue first.

execute(config, prompt, attempt_dir, timeout) may replace the subprocess for manual
validation; it must write output.json and trace.jsonl using the production format.
Task completion records investigation evidence only, never permission for actions.
"""

import resources
import datetime as dt
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
from urllib.parse import urlsplit

import portable
import openai_auth
import provider_limits
import workflow_tasks
import paths


WORKER = "agent-investigation"
MAX_BYTES = 2 * 1024 * 1024


def _url(value):
    if not isinstance(value, str) or len(value) > 2048 or any(char.isspace() for char in value):
        raise ValueError("PUBLIC_HTTPS_SOURCE_REQUIRED")
    parsed = urlsplit(value)
    hostname = (parsed.hostname or "").rstrip(".")
    if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443)
            or "." not in hostname or hostname.endswith((".localhost", ".local", ".internal", ".test"))
            or "aiapply" in hostname.split(".") or parsed.fragment):
        raise ValueError("PUBLIC_HTTPS_SOURCE_REQUIRED")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("PUBLIC_HTTPS_SOURCE_REQUIRED")
    return value


def _inputs(question, sources):
    if not isinstance(question, str) or not 1 <= len(question.strip()) <= 2000:
        raise ValueError("BOUNDED_PUBLIC_QUESTION_REQUIRED")
    if not isinstance(sources, list) or not 1 <= len(sources) <= 12:
        raise ValueError("PUBLIC_SOURCES_REQUIRED")
    inputs = []
    for source in sources:
        if not isinstance(source, dict) or set(source) != {"source", "observed_at"}:
            raise ValueError("SOURCE_AND_OBSERVATION_ONLY")
        workflow_tasks.instant(source["observed_at"])
        inputs.append({"source": _url(source["source"]), "observed_at": source["observed_at"], "question": question})
    return inputs


def enqueue(database, action_id, parent_run_id, question, sources, deadline, now, role_id=None):
    """Queue public facts only; caller must exclude secrets/personal answers and commit."""
    return workflow_tasks.enqueue(database, WORKER, action_id, parent_run_id, _inputs(question, sources),
                                  deadline, now, role_id=role_id, effect="READ")


def enqueue_pending_applications(database, now, limit=1):
    if database.in_transaction:
        raise ValueError("COMMITTED_CALLER_TRANSACTION_REQUIRED")
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"application_jobs", "opportunities"} <= tables:
        return []
    question = ("Investigate this existing role's official public listing: title, current availability, "
                "published base compensation, location/work mode and mandatory requirements. Reconcile "
                "conflicting public evidence and distinguish indexed text from a readable current page. "
                "Preserve unknowns. Do not inspect forms, candidate facts, accounts, application history "
                "or referrals. This investigation cannot clear an application or outreach guard.")
    rows = database.execute("""SELECT jobs.job_key, opportunities.url, opportunities.observed_at
        FROM application_jobs AS jobs JOIN opportunities
        ON opportunities.url=CASE WHEN json_valid(jobs.payload) THEN json_extract(jobs.payload,'$.url') END
        WHERE jobs.state='BLOCKED' AND NOT EXISTS
        (SELECT 1 FROM workflow_tasks WHERE worker=? AND role_id=jobs.job_key)
        ORDER BY jobs.updated_at,jobs.job_key""", (WORKER,)).fetchall()
    queued = []
    for row in rows:
        if len(queued) >= limit:
            break
        try:
            _inputs(question, [{"source": row["url"], "observed_at": row["observed_at"]}])
        except (ValueError, TypeError):
            continue
        with database:
            queued.append(enqueue(database, "application-public-evidence:" + row["job_key"],
                                  "application:" + row["job_key"], question,
                                  [{"source": row["url"], "observed_at": row["observed_at"]}],
                                  (workflow_tasks.instant(now) + dt.timedelta(days=1)).isoformat(),
                                  now, role_id=row["job_key"]))
    return queued


def _command(config, directory):
    command = [config["codex"], "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
               "--skip-git-repo-check", "--sandbox", "read-only", "--model", config["model"], "--json"]
    for setting in ('approval_policy="never"', 'forced_login_method="chatgpt"', 'model_provider="openai"',
                    'web_search="live"', "features.shell_tool=false", "features.apps=false",
                    "features.skills=false", "features.apply_patch_freeform=false", "features.unified_exec=false",
                    "features.multi_agent=false", "mcp_servers={}"):
        command.extend(["-c", setting])
    # E0.T8: ChatGPT sign-in returns this command untouched; an API key drops the forced login.
    return openai_auth.command(command + ["--output-schema", str(directory / "schema.json"), "--output-last-message",
                                          str(directory / "output.json"), "--color", "never", "-"])


# No core file for a worker (POSIX; None on Windows, where portable.no_crash_dialogs covers it).
resource = portable.resource
_limits = portable.no_core


def _execute(config, prompt, directory, timeout):
    allowed = {"HOME", "PATH", "CODEX_HOME", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT"}
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    worker = None
    with tempfile.TemporaryDirectory(prefix="agent-web-") as working, (directory / "trace.jsonl").open("w") as trace, (directory / "stderr.log").open("w") as errors:
        try:
            arguments = _command(config, directory)
            worker = subprocess.Popen(arguments, stdin=subprocess.PIPE, stdout=trace,
                                      stderr=errors, text=True, encoding="utf-8", cwd=working,
                                      env=openai_auth.environment(arguments, environment), **portable.worker_spawn_options())
            portable.contain(worker)
            worker.communicate(prompt, timeout=timeout)
            if worker.returncode:
                trace.flush()
                errors.flush()
                raise RuntimeError("EXECUTOR_FAILED") from provider_limits.check(
                    worker.returncode, directory / "trace.jsonl", directory / "stderr.log", command=arguments)
            openai_auth.succeeded(arguments)   # E0.T8 AK5: a call that worked ends a billing-wait streak
        finally:
            if worker is not None:
                portable.kill_tree(worker.pid)
                worker.wait(timeout=5)
            for stream in (trace, errors):
                stream.flush()
                os.fsync(stream.fileno())


def _read(path, limit=MAX_BYTES):
    if path.is_symlink():
        raise ValueError("INVALID_ARTIFACT")
    with path.open("rb") as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ValueError("ARTIFACT_TOO_LARGE")
    return content


def _result(directory):
    result = json.loads(_read(directory / "output.json", 32768))
    if (not isinstance(result, dict) or set(result) != {"status", "summary", "citations", "unresolved_reason", "next_action"}
            or result["status"] not in {"RESOLVED", "UNRESOLVED"}
            or not isinstance(result["summary"], str) or not 1 <= len(result["summary"].strip()) <= 4000
            or not isinstance(result["citations"], list) or len(result["citations"]) > 12):
        raise ValueError("INVALID_MODEL_RESULT")
    for citation in result["citations"]:
        if (not isinstance(citation, dict) or set(citation) != {"source", "claim"}
                or not isinstance(citation["claim"], str) or not 1 <= len(citation["claim"].strip()) <= 1000):
            raise ValueError("INVALID_MODEL_CITATION")
        _url(citation["source"])
    next_action = result["next_action"]
    reason = result["unresolved_reason"]
    if result["status"] == "UNRESOLVED":
        if (not isinstance(next_action, str) or not 1 <= len(next_action.strip()) <= 500
                or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500):
            raise ValueError("BOUNDED_NEXT_ACTION_REQUIRED")
    elif next_action is not None or reason is not None:
        raise ValueError("RESOLVED_REASON_AND_NEXT_ACTION_MUST_BE_NULL")
    events = []
    for line_number, line in enumerate(_read(directory / "trace.jsonl").splitlines(), 1):
        event = json.loads(line)
        if not isinstance(event, dict):
            raise ValueError("INVALID_TOOL_TRACE")
        item = event.get("item", {})
        if (event.get("type") == "item.completed" and isinstance(item, dict)
                and item.get("type") in {"web_search", "web_search_call"}
                and item.get("status") in {None, "completed"}):
            events.append(line_number)
    if result["status"] == "RESOLVED" and (not events or not result["citations"]):
        raise ValueError("OBSERVED_WEB_SEARCH_AND_CITATIONS_REQUIRED")
    return result, events


def _artifacts(directory, now):
    evidence = []
    for name in ("schema.json", "prompt.txt", "trace.jsonl", "stderr.log", "output.json"):
        path = directory / name
        if path.exists():
            content = _read(path)
            portable.fsync_path(path)
            evidence.append({"source": str(path), "observed_at": now, "kind": "ATTEMPT_ARTIFACT",
                             "sha256": hashlib.sha256(content).hexdigest()})
    portable.fsync_directory(directory)
    return evidence


def cycle(root, database, config, now=None, execute=None):
    """Consume only this worker's READ tasks; no schema initialization or tracker writes."""
    report = {"state": "DISABLED", "counts": {}, "tasks": []}
    settings = config.get("agent_worker", {})
    if settings.get("enabled") is not True:
        return report
    if database.in_transaction:
        raise ValueError("COMMITTED_CALLER_TRANSACTION_REQUIRED")
    timeout, limit = settings.get("timeout_seconds", 60), settings.get("max_items", 1)
    budget = config.get("daily_compute_seconds", 10800)
    if (type(timeout) is not int or not 1 <= timeout <= 120 or type(limit) is not int or not 1 <= limit <= 4
            or type(budget) is not int or not timeout <= budget <= 21000
            or any(not isinstance(config.get(key), str) or not config[key].strip() or config[key].startswith("-")
                   for key in ("codex", "model"))):
        raise ValueError("INVALID_AGENT_WORKER_CONFIGURATION")
    base = workflow_tasks.instant(now.isoformat() if isinstance(now, dt.datetime) else now or dt.datetime.now(dt.timezone.utc).isoformat())
    started = time.monotonic()

    def current():
        return (base + dt.timedelta(seconds=time.monotonic() - started)).isoformat()

    def interrupted(signum, frame):
        raise InterruptedError("AGENT_WORKER_INTERRUPTED")

    root = Path(root).resolve()
    template = resources.read_text("agent-worker-prompt.md")
    schema = resources.read_text("agent-worker-schema.json")
    tasks = database.execute("""SELECT * FROM workflow_tasks WHERE worker=? AND effect='READ'
        AND status IN ('QUEUED','RETRY_WAIT','RUNNING') AND (status!='RUNNING' OR lease_until<=?)
        AND (next_at IS NULL OR next_at<=?) ORDER BY updated_at,task_id LIMIT ?""",
                             (WORKER, current(), current(), limit)).fetchall()
    report["state"] = "COMPLETE"
    for task in tasks:
        try:
            stamp = current()
            day = workflow_tasks.budget_day(stamp, "America/Los_Angeles")
            reservation = WORKER + ":" + task["task_id"] + ":" + str(task["attempts"] + 1) + ":" + day
            if task["attempts"] < task["max_attempts"] and workflow_tasks.instant(stamp) < workflow_tasks.instant(task["deadline"]):
                with database:
                    reserved = workflow_tasks.reserve_compute(database, reservation, stamp, timeout,
                                                              "America/Los_Angeles", budget)
                if not reserved:
                    report["state"] = "BUDGET_WAIT"
                    report["tasks"].append({"task_id": task["task_id"], "state": "COMPUTE_BUDGET_EXHAUSTED"})
                    report["counts"]["COMPUTE_BUDGET_EXHAUSTED"] = report["counts"].get("COMPUTE_BUDGET_EXHAUSTED", 0) + 1
                    continue
            claim = workflow_tasks.claim(database, task["task_id"], current(), lease_seconds=timeout + 15)
        except Exception:
            report["state"] = "BLOCKED"
            report["tasks"].append({"task_id": task["task_id"], "state": "CLAIM_FAILED"})
            report["counts"]["CLAIM_FAILED"] = report["counts"].get("CLAIM_FAILED", 0) + 1
            continue
        state = claim["status"]
        if state == "CLAIMED":
            reserved_attempt = task["attempts"] + 1
            task = database.execute("SELECT * FROM workflow_tasks WHERE task_id=?", (task["task_id"],)).fetchone()
            directory, evidence, handlers = None, [], {}
            result = {"investigation_only": True, "can_apply": False, "can_send": False,
                      "task_id": task["task_id"], "attempt": task["attempts"]}
            status, blocker, next_action, provider = "RETRY_WAIT", None, None, None
            try:
                if task["attempts"] != reserved_attempt or day != workflow_tasks.budget_day(current(), "America/Los_Angeles"):
                    raise RuntimeError("COMPUTE_RESERVATION_CHANGED")
                for signum in (signal.SIGTERM, signal.SIGINT):
                    handlers[signum] = signal.signal(signum, interrupted)
                inputs = json.loads(task["inputs"])
                sources = [{"source": item["source"], "observed_at": item["observed_at"]} for item in inputs]
                if inputs != _inputs(inputs[0]["question"], sources):
                    raise ValueError("INVALID_TASK_INPUTS")
                directory = paths.reports(root) / "agent-worker" / task["task_id"] / str(result["attempt"])
                directory.mkdir(parents=True, mode=0o700)
                os.chmod(directory.parent, 0o700)
                os.chmod(directory.parent.parent, 0o700)
                prompt = template + "\n\nPUBLIC_INVESTIGATION_JSON:\n" + json.dumps({"question": inputs[0]["question"], "sources": sources,
                                                                                          "role_id": task["role_id"]})
                (directory / "schema.json").write_text(schema)
                (directory / "prompt.txt").write_text(prompt)
                remaining = (workflow_tasks.instant(claim["lease_until"]) - workflow_tasks.instant(current())).total_seconds()
                if remaining < timeout + 5:
                    raise TimeoutError("TASK_DEADLINE_TOO_CLOSE")
                (execute or _execute)(config, prompt, directory, timeout)
                model, events = _result(directory)
                result.update(model_result=model, web_search_event_lines=events,
                              citation_basis="MODEL_CLAIMS_NOT_INDEPENDENTLY_VERIFIED",
                              trace_basis="EXECUTOR_TOOL_EVENTS" if execute is None else "INJECTED_EXECUTOR_TRACE")
                evidence = [{"source": citation["source"], "observed_at": current(), "kind": "MODEL_CITATION",
                             "meaning": "Citation recorded, not independent verification or source observation"}
                            for citation in model["citations"]]
                if events:
                    evidence.append({"source": str(directory / "trace.jsonl"), "observed_at": current(),
                                     "kind": result["trace_basis"], "event_lines": events})
                status = "COMPLETE" if model["status"] == "RESOLVED" else "RETRY_WAIT"
                if status != "COMPLETE":
                    blocker, next_action = "INVESTIGATION_UNRESOLVED", model["next_action"]
            except Exception as error:
                provider = provider_limits.wait_of(error)
                blocker, next_action = type(error).__name__, "Retry bounded public-source investigation within existing limits; preserve prior evidence"
                if str(error) in {"COMPUTE_RESERVATION_CHANGED", "EXECUTOR_FAILED", "TASK_DEADLINE_TOO_CLOSE",
                                  "OBSERVED_WEB_SEARCH_AND_CITATIONS_REQUIRED", "INVALID_TASK_INPUTS"}:
                    blocker = str(error)
                result["failure_type"] = blocker
                if isinstance(error, OSError):
                    result["failure_errno"] = error.errno
                    result["failure_file"] = Path(error.filename).name if error.filename else None
                if isinstance(error, InterruptedError):
                    report["state"] = "INTERRUPTED"
            finally:
                for signum, handler in handlers.items():
                    signal.signal(signum, handler)
            try:
                if directory is not None:
                    evidence.extend(_artifacts(directory, current()))
                with database:
                    if provider:
                        state = workflow_tasks.provider_wait(database, task["task_id"], claim["token"], current(), provider,
                                                             dict(result, evidence=evidence))
                    elif blocker in {"COMPUTE_RESERVATION_CHANGED", "TASK_DEADLINE_TOO_CLOSE"}:
                        # Refused before the model ran (the day rolled over between booking and
                        # claiming, or too little lease was left): the try and the booking are both
                        # given back and the task runs next cycle; its deadline still ends it
                        # (2026-09-29).
                        state = workflow_tasks.compute_wait(database, task["task_id"], claim["token"], current(), current(),
                                                            blocker, dict(result, evidence=evidence))
                        workflow_tasks.settle_compute(database, reservation, stamp, current(),
                                                      "agent investigation refused before the model ran; nothing used")
                    else:
                        state = workflow_tasks.finish(database, task["task_id"], claim["token"], current(), result, evidence,
                                                      status=status, blocker=blocker, next_action=next_action)
                        if status == "COMPLETE":
                            workflow_tasks.settle_compute(database, reservation, stamp, current(),
                                                          "agent investigation completed; shared daily pool: unused tail of a cleanly completed reservation")
            except Exception:
                database.rollback()
                state = "UNCOMMITTED_OR_STALE"
        report["tasks"].append({"task_id": task["task_id"], "state": state})
        report["counts"][state] = report["counts"].get(state, 0) + 1
        if report["state"] == "INTERRUPTED":
            break
    return report
