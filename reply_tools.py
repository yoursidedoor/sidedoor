"""Tools the reply AI can use while it reads a message (owner, 2026-10-06).

"Reply agent needs to have tools to connect to other components so it can read information or
write information to them. for e.g, if the reply agent had tools access to the db, it could have
checked to see if an soylent application existed already. Or if one did exist and the contact gave
us an application link, the reply agent could queue the job so the application agent could pick
it up." A job it queues is applied to automatically (owner, 2026-10-06: "it should be applied to
automatically").

A stdio MCP server, the same shape as application_browser's human-check server, that codex
starts for one reply run. Read tools open the database read-only. The write tools never write
during the run: the reply worker re-reads its source after the model finishes and refuses a
decision whose inputs changed, and a job queued mid-run would change them. They record an
intent in the run's directory instead; `apply_intents` carries it out in the same transaction
that saves the decision, through the existing entry points (`applications.ingest`, which keeps
every duplicate and eligibility check of the ordinary application path). A failed run's intents
are dropped with it and its retry asks again.
"""

import argparse
import json
import sqlite3
import sys
import urllib.parse
from pathlib import Path


SERVER = "reply_tools"
CONTEXT_FILE = "reply_tools_context.json"
LOG_FILE = "reply_tools_calls.jsonl"
MAX_CALLS = 12
MAX_QUEUED_JOBS = 3
MAX_OWNER_FLAGS = 2
MAX_RESULT_CHARACTERS = 12000
MAX_TEXT = 300
READ_TIMEOUT_SECONDS = 10

TOOLS = [
    {"name": "find_jobs",
     "description": "Jobs on file for a company or a posting URL: role, URL, state (SUBMITTED means a verified "
                    "application), blocker and when it was submitted. Use it before saying whether the owner applied.",
     "inputSchema": {"type": "object", "additionalProperties": False,
                     "properties": {"company": {"type": "string"}, "url": {"type": "string"}}}},
    {"name": "person_history",
     "description": "What is on file for a LinkedIn profile: our sends and their messages, outreach attempts, "
                    "referral state, stops, closure and suppressions.",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["profile"],
                     "properties": {"profile": {"type": "string"}}}},
    {"name": "company_research",
     "description": "What research recorded about a company: roles found, contacts and company facts.",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["company"],
                     "properties": {"company": {"type": "string"}}}},
    {"name": "application_answers",
     "description": "The answers submitted on one job's application form (job_key from find_jobs).",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["job_key"],
                     "properties": {"job_key": {"type": "string"}}}},
    {"name": "queue_job",
     "description": "Queue a job posting for the application pipeline, e.g. a link the contact sent. It is applied "
                    "to automatically after the usual duplicate and eligibility checks; a posting already on file "
                    "is not queued twice. Takes effect when your decision is saved. https URL of the posting only.",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["url", "company", "role", "reason"],
                     "properties": {"url": {"type": "string"}, "company": {"type": "string"},
                                    "role": {"type": "string"}, "reason": {"type": "string"}}}},
    {"name": "flag_owner",
     "description": "Put a short note for the owner in the daily report, for something only he can act on. "
                    "Takes effect when your decision is saved.",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["reason"],
                     "properties": {"reason": {"type": "string"}}}},
]
WRITE_TOOLS = {"queue_job", "flag_owner"}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _text(value, limit=MAX_TEXT):
    return value if not isinstance(value, str) or len(value) <= limit else value[:limit] + "..."


def _rows(database, sql, parameters=()):
    return [dict(row) for row in database.execute(sql, parameters)]


def _tables(database):
    return {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _payload(row):
    try:
        value = json.loads(row.get("payload") or "{}")
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def _bounded(result):
    """Drop list items from the end until the answer fits, and say so."""
    text = encoded(result)
    while len(text) > MAX_RESULT_CHARACTERS:
        lists = [key for key, value in result.items() if isinstance(value, list) and value]
        if not lists:
            return encoded({"error": "RESULT_TOO_LARGE"})
        longest = max(lists, key=lambda key: len(encoded(result[key])))
        result[longest] = result[longest][:-1]
        result["truncated"] = True
        text = encoded(result)
    return text


def _company_matcher(database, company):
    """Employers that may be this company: the same canonical name, or one whose words contain all
    of the other's ("Soylent" finds "Soylent Energy"). The model sees which names matched and judges;
    an exact-only match made "Soylent" find nothing and the model concluded no application existed
    (replay, 2026-10-06)."""
    import champion_composition

    wanted = champion_composition._company_key(database, company) if isinstance(company, str) and company.strip() else None
    if not wanted:
        return None
    wanted_words = set(wanted.split())

    def matches(name):
        key = champion_composition._company_key(database, name or "")
        if not key:
            return False
        words = set(key.split())
        return key == wanted or wanted_words <= words or words <= wanted_words
    return matches


NOTHING_MATCHED = ("Nothing on file matched this name. That is not proof there is nothing: try the company's "
                   "full or other name before concluding.")


def _https(url):
    parsed = urllib.parse.urlsplit(url.strip()) if isinstance(url, str) else None
    return bool(parsed and parsed.scheme == "https" and parsed.hostname)


# --- read tools ---------------------------------------------------------------------------------

def _submitted_at(database, key):
    """When the application was submitted: the receipt's armed time, else the armed time of its
    attempt's submit request, else when it was verified. A page confirmation settled on 10-07 for a
    submit armed 09-24 must not read as applied that day (2026-10-06)."""
    tables = _tables(database)
    if "application_events" not in tables:
        return None
    row = database.execute("""SELECT at,detail FROM application_events WHERE job_key=? AND event='SUBMISSION_VERIFIED'
        ORDER BY at, id LIMIT 1""", (key,)).fetchone()
    if not row:
        return None
    try:
        detail = json.loads(row[1] or "{}")
    except (TypeError, ValueError):
        detail = {}
    detail = detail if isinstance(detail, dict) else {}
    if isinstance(detail.get("armed_at"), str) and detail["armed_at"]:
        return detail["armed_at"]
    if isinstance(detail.get("attempt_id"), str) and "desktop_requests" in tables:
        armed = database.execute("""SELECT MIN(armed_at) FROM desktop_requests WHERE attempt_id=? AND operation='submit'
            AND armed_at IS NOT NULL""", (detail["attempt_id"],)).fetchone()[0]
        if armed:
            return armed
    return row[0]


def find_jobs(database, company=None, url=None):
    import applications

    if not (isinstance(company, str) and company.strip()) and not (isinstance(url, str) and url.strip()):
        return {"error": "Give a company or a url"}
    if "application_jobs" not in _tables(database):
        return {"jobs": []}
    keys = set()
    if isinstance(url, str) and url.strip():
        wanted = url.strip().rstrip("/")
        for row in _rows(database, "SELECT job_key,payload FROM application_jobs"):
            if str(_payload(row).get("url") or "").rstrip("/") == wanted:
                keys.add(row["job_key"])
        if _https(url):
            source_key = applications.posting_source_key({"company": company or "", "url": url.strip()})
            if source_key:
                keys.update(row["job_key"] for row in _rows(
                    database, "SELECT job_key FROM application_source_keys WHERE source_key=?", (source_key,)))
    matches = _company_matcher(database, company)
    if matches:
        keys.update(row["job_key"] for row in _rows(database, "SELECT job_key,employer FROM application_jobs")
                    if matches(row["employer"]))
    if keys and "application_aliases" in _tables(database):
        placeholders = ",".join("?" for unused in keys)
        keys.update(row["canonical_key"] for row in _rows(
            database, f"SELECT canonical_key FROM application_aliases WHERE alias_key IN ({placeholders})", sorted(keys)))
    jobs = []
    for key in sorted(keys)[:40]:
        job = database.execute("SELECT * FROM application_jobs WHERE job_key=?", (key,)).fetchone()
        if job is None:
            continue
        job = dict(job)
        payload = _payload(job)
        submitted = _submitted_at(database, key)
        jobs.append({"job_key": key, "employer": job["employer"], "role": payload.get("role"), "url": payload.get("url"),
                     "state": job["state"], "blocker": job["blocker"], "submitted_at": submitted,
                     "updated_at": job["updated_at"]})
    result = {"jobs": jobs, "basis": "saved application records; check each employer is the company you mean"}
    if not jobs:
        result["note"] = NOTHING_MATCHED
    return result


def person_history(database, profile):
    import engagement
    import referrals

    try:
        identities = engagement.identity_keys(database, profile)
    except (ValueError, TypeError, AttributeError):
        return {"error": "Give a LinkedIn /in/ profile URL"}
    tables = _tables(database)
    placeholders = ",".join("?" for unused in identities)
    result = {"identities": identities, "messages": [], "outreach_attempts": [], "stops": [], "closure": None,
              "suppressions": [], "referral": None}
    if "engagement_events" in tables:
        for row in _rows(database, f"""SELECT kind,identity,occurred_at,payload FROM engagement_events
                WHERE identity IN ({placeholders}) ORDER BY occurred_at DESC LIMIT 20""", identities):
            text = None
            try:
                inbox = json.loads(row["payload"] or "{}")
                inbox = inbox.get("inbox", inbox) if isinstance(inbox, dict) else {}
                text = inbox.get("text") if isinstance(inbox, dict) else None
            except (ValueError, TypeError):
                pass
            result["messages"].append({"kind": row["kind"], "occurred_at": row["occurred_at"], "text": _text(text)})
    if "outreach_attempts" in tables:
        result["outreach_attempts"] = _rows(database, f"""SELECT identity,campaign_id,state,claimed_at FROM outreach_attempts
            WHERE identity IN ({placeholders}) ORDER BY claimed_at""", identities)
    if "engagement_stops" in tables:
        result["stops"] = _rows(database, f"SELECT identity,state,source FROM engagement_stops WHERE identity IN ({placeholders})", identities)
    if "suppressions" in tables:
        result["suppressions"] = _rows(database, f"SELECT identity,reason FROM suppressions WHERE identity IN ({placeholders})", identities)
    result["closure"] = engagement.closure_status(database, profile)
    if "referrals" in tables:
        for identity in identities:
            record = referrals.current(database, identity)
            if record:
                record.pop("history", None)
                result["referral"] = {key: _text(value) for key, value in record.items()}
                break
    return result


def company_research(database, company):
    matches = _company_matcher(database, company)
    if not matches:
        return {"error": "Give a company name"}
    tables = _tables(database)
    result = {"roles": [], "contacts": [], "facts": []}
    if "opportunities" in tables:
        for row in _rows(database, "SELECT url,payload,observed_at FROM opportunities ORDER BY observed_at DESC"):
            payload = _payload(row)
            if matches(str(payload.get("company") or "")):
                result["roles"].append({"company": payload.get("company"), "role": payload.get("role"), "url": row["url"],
                                        "observed_at": row["observed_at"], "rationale": _text(payload.get("rationale"))})
    if "rw_contacts" in tables:
        result["contacts"] = [{"company": row["company"], "name": row["name"], "profile": row["identity"],
                               "historical": row["historical"]}
                              for row in _rows(database, "SELECT company,identity,name,historical FROM rw_contacts")
                              if matches(row["company"])][:30]
    if "application_company_facts" in tables:
        result["facts"] = [{"employer": row["employer"], "updated_at": row["updated_at"], "facts": _text(row["facts"], 3000)}
                           for row in _rows(database, "SELECT employer,facts,updated_at FROM application_company_facts")
                           if matches(row["employer"])][:2]
    result["roles"] = result["roles"][:30]
    if not (result["roles"] or result["contacts"] or result["facts"]):
        result["note"] = NOTHING_MATCHED
    return result


def application_answers(database, job_key):
    if not isinstance(job_key, str) or not job_key.strip() or "application_answers" not in _tables(database):
        return {"answers": []}
    return {"job_key": job_key, "answers": [{"field": row["field"], "value": _text(row["value"])} for row in _rows(
        database, "SELECT field,value FROM application_answers WHERE job_key=? ORDER BY field LIMIT 80", (job_key,))]}


READERS = {"find_jobs": lambda database, arguments: find_jobs(database, arguments.get("company"), arguments.get("url")),
           "person_history": lambda database, arguments: person_history(database, arguments.get("profile")),
           "company_research": lambda database, arguments: company_research(database, arguments.get("company")),
           "application_answers": lambda database, arguments: application_answers(database, arguments.get("job_key"))}


# --- the server ---------------------------------------------------------------------------------

class Session:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.context = json.loads((self.directory / CONTEXT_FILE).read_text(encoding="utf-8"))
        self.calls = 0
        self.intents = {"queue_job": 0, "flag_owner": 0}

    def log(self, entry):
        with (self.directory / LOG_FILE).open("a", encoding="utf-8") as handle:
            handle.write(encoded(entry) + "\n")

    def call(self, name, arguments):
        arguments = arguments if isinstance(arguments, dict) else {}
        self.calls += 1
        entry = {"tool": name, "arguments": {key: _text(value) for key, value in arguments.items()}}
        if self.calls > MAX_CALLS:
            entry["refused"] = "CALL_LIMIT"
            self.log(entry)
            return f"Refused: at most {MAX_CALLS} tool calls per message. Decide with what you have.", True
        if name in READERS:
            import paths  # here, not at the top: this server otherwise needs only the standard library
            database = sqlite3.connect("file:" + str(paths.database(self.context["root"])) + "?mode=ro",
                                       uri=True, timeout=READ_TIMEOUT_SECONDS)
            database.row_factory = sqlite3.Row
            try:
                text = _bounded(READERS[name](database, arguments))
            finally:
                database.close()
            entry["result_characters"] = len(text)
            self.log(entry)
            return text, False
        if name in WRITE_TOOLS:
            refusal = self.refusal(name, arguments)
            if refusal:
                entry["refused"] = refusal
                self.log(entry)
                return "Refused: " + refusal, True
            self.intents[name] += 1
            entry["intent"] = {key: arguments[key].strip() for key in sorted(arguments)}
            self.log(entry)
            return "Recorded. It takes effect when your decision is saved.", False
        entry["refused"] = "UNKNOWN_TOOL"
        self.log(entry)
        return "Refused: unknown tool", True

    def refusal(self, name, arguments):
        required = {"queue_job": ("url", "company", "role", "reason"), "flag_owner": ("reason",)}[name]
        if any(not isinstance(arguments.get(key), str) or not arguments[key].strip() for key in required):
            return "every field is required"
        if any(len(arguments[key]) > 500 for key in required):
            return "a field is over 500 characters"
        if name == "queue_job":
            if not _https(arguments["url"]):
                return "the posting URL must be https"
            if self.intents[name] >= MAX_QUEUED_JOBS:
                return f"at most {MAX_QUEUED_JOBS} jobs per message"
        if name == "flag_owner" and self.intents[name] >= MAX_OWNER_FLAGS:
            return f"at most {MAX_OWNER_FLAGS} notes per message"
        return None


def serve(directory):
    session = Session(directory)
    for line in sys.stdin:
        if len(line) > 65536:
            return 2
        try:
            message = json.loads(line)
            if "id" not in message:
                continue
            method = message.get("method")
            if method == "initialize":
                result = {"protocolVersion": message.get("params", {}).get("protocolVersion", "2024-11-05"),
                          "capabilities": {"tools": {}}, "serverInfo": {"name": "reply-tools", "version": "1"}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                params = message.get("params", {})
                try:
                    text, failed = session.call(params.get("name"), params.get("arguments"))
                except Exception as error:
                    session.log({"tool": params.get("name"), "error_type": type(error).__name__})
                    text, failed = "The lookup failed; decide with what you have.", True
                result = {"content": [{"type": "text", "text": text}], "isError": failed}
            else:
                # Machine contract (E2.T6): MCP JSON-RPC over stdout; stays print.
                print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "Method not found"}}), flush=True)
                continue
            # Machine contract (E2.T6): MCP JSON-RPC over stdout; stays print.
            print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
        except Exception:
            return 2
    return 0


# --- the reply worker's side --------------------------------------------------------------------

def server_argv(directory, executable=None):
    """The command codex starts as this server. From source: this Python with reply_tools.py. In a
    frozen build there is no .py file and the binary takes no script (E9.T1): the binary itself with
    the `reply-tools` subcommand (orchestrator.main), as scheduler_loop.job_command runs the jobs."""
    import scheduler_loop
    tail = ["serve", "--directory", str(directory)]
    if scheduler_loop.frozen():
        if executable is not None:  # a frozen build has no reply_tools.py for another program to run
            raise ValueError("REPLY_TOOLS_EXECUTABLE_FROZEN: a frozen build starts its own binary")
        return [scheduler_loop.program(), "reply-tools"] + tail
    import portable
    return [executable or sys.executable, *portable.UTF8_FLAGS, str(Path(__file__).resolve())] + tail


def options(directory, executable=None):
    """codex -c settings that attach this server to one run."""
    argv = server_argv(directory, executable)
    command = json.dumps(argv[0])
    arguments = json.dumps(argv[1:])
    result = ["mcp_servers." + SERVER + ".command=" + command,
              "mcp_servers." + SERVER + ".args=" + arguments,
              "mcp_servers." + SERVER + ".enabled=true",
              "mcp_servers." + SERVER + ".startup_timeout_sec=20",
              "mcp_servers." + SERVER + ".tool_timeout_sec=" + str(READ_TIMEOUT_SECONDS + 20)]
    result.extend("mcp_servers." + SERVER + ".tools." + tool["name"] + '.approval_mode="approve"' for tool in TOOLS)
    return result


def prepare(directory, root, task_id):
    (Path(directory) / CONTEXT_FILE).write_text(encoded({"root": str(root), "task_id": task_id}), encoding="utf-8")


def collect(directory):
    """The run's tool log: every call (bounded) and the intents to apply."""
    path = Path(directory) / LOG_FILE
    calls = []
    if path.is_file() and not path.is_symlink():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[:MAX_CALLS + 8]:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                calls.append(entry)
    return calls


def initialize(database):
    database.execute("""CREATE TABLE IF NOT EXISTS reply_owner_flags (
        task_id TEXT NOT NULL, flagged_at TEXT NOT NULL, reason TEXT NOT NULL, source_key TEXT,
        PRIMARY KEY (task_id, reason))""")
    # E6.T4: [Mark done] on the Approvals page's "Notes for you" sets done_at (NULL while open).
    if "done_at" not in {row[1] for row in database.execute("PRAGMA table_info(reply_owner_flags)")}:
        database.execute("ALTER TABLE reply_owner_flags ADD COLUMN done_at TEXT")


def apply_intents(database, task_id, source_key, calls, now):
    """Carry out the run's write intents inside the caller's transaction; return what happened."""
    import applications
    import workflow_tasks

    applied = []
    for entry in calls:
        intent = entry.get("intent") if isinstance(entry, dict) else None
        if not isinstance(intent, dict):
            continue
        if entry.get("tool") == "queue_job":
            role = {"company": intent["company"], "role": intent["role"], "url": intent["url"],
                    "rationale": intent["reason"], "discovered_by": "reply", "reply_task_id": task_id,
                    "reply_source_key": source_key}
            try:
                existing = applications.posting_source_key(role)
                known = existing and database.execute("SELECT job_key FROM application_source_keys WHERE source_key=?",
                                                      (existing,)).fetchone()
                job_key = applications.ingest(database, role, now, source="reply")
                outcome = {"tool": "queue_job", "job_key": job_key, "url": intent["url"],
                           "state": "ALREADY_ON_FILE" if known else "QUEUED"}
            except (ValueError, KeyError, TypeError) as error:
                outcome = {"tool": "queue_job", "url": intent["url"], "state": "REFUSED", "reason": type(error).__name__}
            workflow_tasks.record(database, task_id, "REPLY_TOOL_QUEUE_JOB", now, outcome)
            applied.append(outcome)
        elif entry.get("tool") == "flag_owner":
            initialize(database)
            database.execute("INSERT OR IGNORE INTO reply_owner_flags(task_id,flagged_at,reason,source_key) VALUES (?,?,?,?)",
                             (task_id, now, intent["reason"], source_key))
            outcome = {"tool": "flag_owner", "reason": intent["reason"], "state": "RECORDED"}
            workflow_tasks.record(database, task_id, "REPLY_TOOL_OWNER_FLAG", now, outcome)
            applied.append(outcome)
    return applied


def owner_flags(database, since):
    """Notes the reply AI left for the owner since `since`, for the daily report and status."""
    if "reply_owner_flags" not in _tables(database):
        return []
    return [dict(row) for row in database.execute(
        "SELECT task_id,flagged_at,reason FROM reply_owner_flags WHERE flagged_at>=? ORDER BY flagged_at", (since,))]


def open_flags(database, limit=None):
    """Notes not marked done, oldest first, for the Approvals page and Overview's "Needs you", each with
    its rowid (`note_id`, what [Mark done] names). Reads only; a table from before done_at existed
    counts every note as open."""
    if "reply_owner_flags" not in _tables(database):
        return []
    columns = {row[1] for row in database.execute("PRAGMA table_info(reply_owner_flags)")}
    done = " WHERE done_at IS NULL" if "done_at" in columns else ""
    bound = " LIMIT %d" % int(limit) if limit else ""
    return [dict(zip(("note_id", "task_id", "flagged_at", "reason"), row)) for row in database.execute(
        "SELECT rowid,task_id,flagged_at,reason FROM reply_owner_flags" + done + " ORDER BY flagged_at,task_id" + bound)]


def open_flag_count(database):
    if "reply_owner_flags" not in _tables(database):
        return 0
    columns = {row[1] for row in database.execute("PRAGMA table_info(reply_owner_flags)")}
    return database.execute("SELECT COUNT(*) FROM reply_owner_flags" + (" WHERE done_at IS NULL" if "done_at" in columns else "")).fetchone()[0]


def mark_done(database, note_id, now):
    """[Mark done]: one note (by its rowid; its text may hold any character, E6.T4 review 5) leaves the
    open list. True when it changed (False: already done or gone)."""
    try:
        note_id = int(note_id)
    except (TypeError, ValueError):
        return False
    with database:
        initialize(database)
        changed = database.execute("UPDATE reply_owner_flags SET done_at=? WHERE rowid=? AND done_at IS NULL", (now, note_id))
    return changed.rowcount == 1


def main(argv=None):
    """`reply_tools.py serve --directory D` from source, `sidedoor reply-tools serve --directory D` frozen."""
    parser = argparse.ArgumentParser(description="The reply AI's tools (one run); started by codex, not by hand")
    parser.add_argument("command", choices=["serve"])
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args(argv)
    return serve(args.directory)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.exit(main())
