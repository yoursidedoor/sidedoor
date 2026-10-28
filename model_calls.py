"""One row per model call (C4, owner-approved cost cut, 2026-10-22).

`text_model.complete` (direct OpenRouter calls; every text worker makes them, through
`reply_worker.execute_text` since 2026-10-27) writes one row per call to `model_calls` in the jobs'
database: time, worker, job or person key, route, model, provider, prompt / completion / reasoning
tokens, cost (when OpenRouter reports it, else NULL), seconds, outcome (ok / refused / invalid /
timeout) and the generation id when present. Older rows of codex text runs carry only
total_tokens (codex printed "tokens used\\n15,582" on stderr) and no cost.

The database is the one `orchestrator.connect` last opened in this process (`use`), written on a
connection of its own with a short busy wait. Recording is best effort: a missing database or a
locked one never fails the call it records. A process that never connected (a unit test on an
in-memory database) records nothing.
"""

import contextlib
import contextvars
import datetime as dt
import sqlite3
import sys


_DATABASE = None
_CONTEXT = contextvars.ContextVar("model_call_context", default={})
BUSY_SECONDS = 2
TABLE = """CREATE TABLE IF NOT EXISTS model_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, worker TEXT, subject TEXT,
    route TEXT NOT NULL, model TEXT, provider TEXT, prompt_tokens INTEGER, completion_tokens INTEGER,
    reasoning_tokens INTEGER, total_tokens INTEGER, cost REAL, seconds REAL, outcome TEXT NOT NULL,
    generation_id TEXT)"""
FIELDS = ("at", "worker", "subject", "route", "model", "provider", "prompt_tokens", "completion_tokens",
          "reasoning_tokens", "total_tokens", "cost", "seconds", "outcome", "generation_id")


def use(path):
    """Record this process's model calls in the database at `path` (orchestrator.connect)."""
    global _DATABASE
    _DATABASE = str(path)


def initialize(database):
    database.execute(TABLE)
    database.execute("CREATE INDEX IF NOT EXISTS model_calls_at ON model_calls(at)")


@contextlib.contextmanager
def context(worker=None, subject=None):
    """Name the worker and the job or person key of the calls made inside this block."""
    token = _CONTEXT.set({**_CONTEXT.get(), **{key: value for key, value in
                                               (("worker", worker), ("subject", subject)) if value}})
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def caller(depth=2):
    """The calling module's name, the worker when no context names one."""
    try:
        return sys._getframe(depth).f_globals.get("__name__")
    except ValueError:
        return None


def record(route, outcome, *, worker=None, **fields):
    if _DATABASE is None:
        return
    current = _CONTEXT.get()
    row = {"at": dt.datetime.now(dt.timezone.utc).isoformat(), "route": route, "outcome": outcome,
           "worker": current.get("worker") or worker, "subject": current.get("subject"), **fields}
    for key in ("prompt_tokens", "completion_tokens", "reasoning_tokens", "total_tokens"):
        row[key] = row.get(key) if type(row.get(key)) is int else None
    row["cost"] = row.get("cost") if isinstance(row.get("cost"), (int, float)) and not isinstance(row.get("cost"), bool) else None
    try:
        database = sqlite3.connect(_DATABASE, timeout=BUSY_SECONDS)
        try:
            with database:
                initialize(database)
                database.execute("INSERT INTO model_calls (" + ",".join(FIELDS) + ") VALUES ("
                                 + ",".join("?" for unused in FIELDS) + ")", [row.get(key) for key in FIELDS])
        finally:
            database.close()
    except (sqlite3.Error, OSError):
        pass


def codex_tokens(stderr):
    """Codex's own total ("tokens used" then the number) from its stderr, else None (engines.CodexEngine.usage)."""
    import engines
    return engines.CODEX.usage(stderr).get("total_tokens")


def today(database, now=None, timezone="America/Los_Angeles"):
    """{"cost", "calls", "unpriced"} for the calls since the service day started."""
    import workflow_tasks
    now = now or dt.datetime.now(dt.timezone.utc)
    start = workflow_tasks.service_day_start(now, timezone).astimezone(dt.timezone.utc).isoformat()
    try:
        row = database.execute("SELECT COALESCE(SUM(cost),0), COUNT(*), SUM(cost IS NULL) FROM model_calls "
                               "WHERE at>=?", (start,)).fetchone()
    except sqlite3.Error:
        return {"cost": 0.0, "calls": 0, "unpriced": 0}
    return {"cost": float(row[0] or 0), "calls": int(row[1] or 0), "unpriced": int(row[2] or 0)}


def summary_line(totals):
    return "Model cost today: $%.4f across %d calls (%d without cost data)" % (
        totals["cost"], totals["calls"], totals["unpriced"])


def bound(function, worker=None, subject=None):
    """`function` run inside context(worker, subject)."""
    def run(*arguments, **named):
        with context(worker, subject):
            return function(*arguments, **named)
    return run
