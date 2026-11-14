"""Stop the running job and requeue it (E16 Y1 c; owner, 2026-11-13: "Stop, requeue and continue from where
it stopped. If it was already on submission page and we stop it, there is no reason for it to go back to
reading phase").

The user pressed a human check's card (an attended submit, `attended_submit`) while another application's
session holds the browser. Only that press has this power.

- `ask` (the dashboard, from the card): the running session is asked to stop by a file in its own folder
  (`browser_guard.YIELD_FILE`). Only an application read or a submit that has not armed is asked; anything else
  is left alone and the card says the short wait.
- The session's guard stops it in its own message loop (`browser_guard.Proxy.yield_turn`): never once the arm
  was called, never an access step or an attended submit.
- `hand_back` (`application_browser.consume`, when its session ended that way): the request is handed back as
  one that never ran (its claim returned, no result), after checking in the database that it never armed.
- The job is left exactly as it was the moment before that session started, due now (`applications.process`
  on `application_connection.SessionYielded`): a read is simply due again as a read; a submit gets back its
  state, its try and its blocker, its attempt row is closed as not submitted, and the read it was answered
  from is kept (`keep_read`) so the next round goes straight to the submit again: no fresh read, and the
  answers and the approval are the stored ones. No try, read claim or recovery attempt is spent, no card, no
  failure. (The page itself is not kept: the new session opens the form and fills it again.)
- One stop per attended request, recorded on its row: the card asking again at every poll stops nothing
  more, and a later stop needs a new request (a new press after this one ended). A session is stopped only
  when the round now running will run the asking job next (`round_takes`); otherwise nothing is stopped.
"""
import contextlib
import json
import sqlite3
from pathlib import Path

import log

LOG = log.get(__name__)
READS = "application_yielded_reads"
ASKED_EVENT = "SESSION_ASKED_TO_YIELD"
YIELDED_EVENT = "REQUEST_YIELDED_TO_ATTENDED_SUBMIT"
RESTORED_EVENT = "SUBMISSION_YIELDED_TO_ATTENDED_SUBMIT"
STOPPABLE = ("inspect", "submit")
ASKED, WAIT, LATER = "asked", "wait", "later"
# The jobs whose session was stopped in THIS process and that the round has not yet put back (`take_stopped`):
# the round runs each again right after the attended session, once per stop (owner: "the other job continues").
_STOPPED = []


def initialize(database):
    database.execute(f"CREATE TABLE IF NOT EXISTS {READS} (job_key TEXT PRIMARY KEY, review TEXT NOT NULL, at TEXT NOT NULL)")


def running(database, job_key, now):
    """The request of ANOTHER job whose session can be running now (claimed, its lease live), or None."""
    return database.execute("SELECT * FROM desktop_requests WHERE state='CLAIMED' AND lease_until>? AND job_key<>? "
                            "ORDER BY rowid DESC LIMIT 1", (now, job_key)).fetchone()


def folder(database, request_id):
    row = database.execute("SELECT detail FROM desktop_events WHERE request_id=? AND event='BROWSER_SESSION_DIRECTORY' "
                           "ORDER BY id DESC LIMIT 1", (request_id,)).fetchone()
    try:
        return Path(json.loads(row[0])["directory"]) if row else None
    except (TypeError, ValueError, KeyError):
        return None


def ask(database, job_key, now, write=True):
    """For the job whose attended submit was asked for: None when no other job's session is running; ASKED
    when the running one was asked to stop by THIS request; WAIT when it runs to its end (an armed submit, a
    session of another kind, one whose guard refused, or this request's one stop is already spent on another
    session); LATER when the round now running will not run this job (it is outside the round's scope, or no
    round is running): nothing is stopped then. `write` False (a page being drawn): the same answer, and
    nothing is asked.

    One stop per attended request, recorded on its row (`stop_request`): asking again, at every poll of the
    card, never stops a second session. And a session is stopped only when the round will in fact run the
    asking job next (`round_takes`)."""
    import attended_submit
    import browser_guard
    request = running(database, job_key, now)
    if request is None:
        return None
    row = attended_submit._row(database, job_key)
    if row is None or row["state"] != attended_submit.REQUESTED:
        return WAIT
    spent = row["stop_request"] if "stop_request" in row.keys() else None
    if spent:
        return ASKED if spent == request["request_id"] else WAIT
    if request["operation"] not in STOPPABLE or request["armed_at"]:
        return WAIT
    if not round_takes(database, job_key):
        return LATER
    directory = folder(database, request["request_id"])
    if directory is None:
        return ASKED   # its session has not got its folder yet: asked at the card's next poll
    if (directory / browser_guard.YIELD_REFUSED_FILE).exists():
        return WAIT
    if write:
        import applications
        try:
            with attended_submit._written(database):
                attended_submit._watched_column(database)
                marked = database.execute(
                    f"UPDATE {attended_submit.TABLE} SET stop_request=? WHERE job_key=? AND state=? AND stop_request IS NULL",
                    (request["request_id"], job_key, attended_submit.REQUESTED))
                if marked.rowcount != 1:
                    return WAIT
                applications.event(database, job_key, ASKED_EVENT,
                                   {"request_id": request["request_id"], "operation": request["operation"],
                                    "job_key": request["job_key"]}, now)
            (directory / browser_guard.YIELD_FILE).write_text(json.dumps({"for": job_key, "at": now}), encoding="utf-8")
        except (OSError, sqlite3.Error):
            return WAIT
    return ASKED


# --- the round now running, and what it will run -----------------------------------------------------------

ROUND = "application_round"


def round_started(database, only, now):
    """`applications.cycle`, at its start: the scope of the round now running (None: every job)."""
    database.execute(f"CREATE TABLE IF NOT EXISTS {ROUND} (id INTEGER PRIMARY KEY CHECK (id=1), scope TEXT, started_at TEXT NOT NULL)")
    database.execute(f"INSERT OR REPLACE INTO {ROUND}(id,scope,started_at) VALUES (1,?,?)",
                     (json.dumps(sorted(only)) if only is not None else None, now))


def round_ended(database):
    with contextlib.suppress(sqlite3.Error):
        database.execute(f"DELETE FROM {ROUND}")


def round_takes(database, job_key):
    """True when a round is running and its scope includes the job: it will take the job's attended request
    next, after the job in hand (`applications.cycle`). False: stopping a session would open nothing."""
    try:
        row = database.execute(f"SELECT scope FROM {ROUND} WHERE id=1").fetchone()
    except sqlite3.Error:
        return False
    if row is None:
        return False
    try:
        return row[0] is None or job_key in json.loads(row[0])
    except (TypeError, ValueError):
        return False


def yielded(directory):
    """True when the session run in `directory` was stopped by its guard for an attended submit."""
    import browser_guard
    import browser_runtime
    return browser_runtime.guard_stopped(directory) == browser_guard.YIELD_REASON


def hand_back(database, envelope, request_id, operation, now):
    """The session ended because its guard yielded: the request is handed back as one that never ran (its
    claim returned, no result, expired now), bound to this claim by its binding and token. Never a request
    that armed (checked here in the database, whatever the guard said). Whatever became of the card that
    asked (review, 2026-11-13): a session that stopped because it was asked to is never counted as a failure.
    True when handed back."""
    import desktop_adapter
    if operation not in STOPPABLE:
        return False
    handed = database.execute(
        """UPDATE desktop_requests SET state='EXPIRED',claims=max(claims-1,0),result=NULL,lease_until=NULL,
           token_hash=NULL,expires_at=? WHERE request_id=? AND state='CLAIMED' AND armed_at IS NULL
           AND binding=? AND token_hash=?""",
        (now, request_id, envelope["binding"], desktop_adapter.digest(envelope["token"])))
    if handed.rowcount != 1:
        database.rollback()
        return False
    desktop_adapter.record(database, request_id, YIELDED_EVENT, now, {"operation": operation})
    stopped = database.execute("SELECT job_key FROM desktop_requests WHERE request_id=?", (request_id,)).fetchone()
    database.commit()
    if stopped and stopped[0]:
        _STOPPED.append(stopped[0])
    return True


def take_stopped():
    """The jobs stopped in this process since the last call, in order, each once per stop."""
    taken = list(_STOPPED)
    del _STOPPED[:]
    return taken


# --- the read a stopped submit was answered from -----------------------------------------------------------

def keep_read(database, job_key, review, now):
    """Call inside a transaction: the read the stopped submit was answered from, for its next round."""
    import applications
    initialize(database)
    kept = {key: value for key, value in review.items() if key != applications.REUSED_READ_MARKER}
    database.execute(f"INSERT OR REPLACE INTO {READS}(job_key,review,at) VALUES (?,?,?)", (job_key, json.dumps(kept), now))


def kept_read(database, job_key):
    """The kept read of a job whose submit was stopped, marked as a carried-forward read, or None."""
    import applications
    try:
        row = database.execute(f"SELECT review,at FROM {READS} WHERE job_key=?", (job_key,)).fetchone()
        review = json.loads(row[0]) if row else None
    except (sqlite3.Error, TypeError, ValueError):
        return None
    return dict(review, **{applications.REUSED_READ_MARKER: row[1]}) if isinstance(review, dict) else None


def drop_read(database, job_key):
    with contextlib.suppress(sqlite3.Error):
        database.execute(f"DELETE FROM {READS} WHERE job_key=?", (job_key,))
