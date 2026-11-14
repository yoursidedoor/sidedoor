"""The attended submit (E16.T2 phase 2; owner, 2026-11-12, Y3 to Y5; E16.T4).

A human check ON THE FORM gives a one-time pass that counts only in the session that presses Submit, so the
user does it while that session runs. This module is what makes that reachable from the card:

- `press` (the card's button, through `access_cards.step`): the request is recorded, the job's wait is
  released as any user step releases it (`access_outcomes.resume`), and the applications job is started
  at once (`scheduler_loop.run_now`). No session is started here: the applications job starts every
  session, as it always has. A request counts only while its card is being watched (`due`, WATCH_SECONDS):
  one whose card stopped asking is ended, and the job waits for its card again.
- `applications.cycle` takes a requested job first, and a job requested while a round is running next,
  after the job in hand (`due`). Nothing running is interrupted (Y5: the session starts when that one ends).
- `claim` (the adapter, just before the submit session of a requested job): that session is ATTENDED, its
  config carries form_check_hold.ATTENDED_KEY with where the check sits and the order to work in (`order_for`).
- `status` / `step` (the card, polled): one true line while nothing can be shown yet, then the live view
  attached to that session's Chrome (`live_view.LiveView.offer_attached`). The session's folder is the one
  the session itself records on its request (BROWSER_SESSION_DIRECTORY); its DevTools endpoint is the record
  its guard writes when it starts Chrome (`live_view.session_endpoint`).
- Review mode (E16.T4): nothing is submitted unapproved, attended or not (`applications.process` holds the
  job for approval before any submit). The card says so before a request is recorded when the job has no
  approval, and while the job waits on Approvals.
- The card never says something that is not happening: what the product knows is in the way is said instead,
  before a request is recorded and after (`in_the_way`), and "Opening the application." is said for
  OPENING_BOUND_SECONDS at most; then one line, and the card stops asking until its button is pressed.

The order (Y3 and the owner's fallback): one stored setting per kind of check (`human_check_orders`),
HALF_FIRST unless set: fill about half the answers, then the check, then the rest. CHECK_FIRST is for a kind
whose pass is known to last through the whole fill. The number that decides it is recorded by every attended
session that reaches an accepted submit (`human_check_measurements`: seconds from the user's Done to the arm).
Nothing changes a setting by itself.

The site's own Submit (owner, 2026-11-13, "a+b"): the card's red line, and the attached view passes no press
or key that would submit the form (`live_view.Chrome._gate`). What stays unguarded is listed in AGENTS.md.
"""
import contextlib
import datetime as dt
import html
import json
import sqlite3
from pathlib import Path

import log

LOG = log.get(__name__)
TABLE = "application_attended_submits"
READY = "application_attended_ready"
ORDERS = "human_check_orders"
MEASUREMENTS = "human_check_measurements"
REQUESTED, STARTED, ENDED = "REQUESTED", "STARTED", "ENDED"
HALF_FIRST, CHECK_FIRST = "HALF_FIRST", "CHECK_FIRST"
ORDER_VALUES = (HALF_FIRST, CHECK_FIRST)
APPROVAL_CODES = ("AWAITING_APPLICATION_APPROVAL", "APPLICATION_APPROVED")
# An attended session starts only while its card is being watched NOW (review, 2026-11-13: a request whose
# card had stopped asking started a session nobody saw, which held the browser for 9 minutes). The card asks
# every POLL_SECONDS; a request not asked about for this long is no longer due and is ended.
WATCH_SECONDS = 15
POLL_SECONDS = 1   # the card asks this often while there is no page yet (the user is waiting)

# The card's sentences. The owner's words (2026-11-13):
STARTING_WORDS = "Opening the application."
BUSY_WORDS = "Opens in a moment."                       # another application is mid-submit (Y5)
APPROVE_FIRST_WORDS = "Approve this application first."
NOT_RUNNING_WORDS = "Start Sidedoor, then open this again."
# PROPOSED, in his style, for his approval: what the product knows is in the way, and the bound's line.
OFF_WORDS = "Applications are off."
COMPUTE_WORDS = "Out of compute today. Open this again tomorrow."
SLOW_WORDS = "Not open yet. Open this again."
# The lines that waiting a few seconds cannot change: under them the request is not kept (`rehold`).
STUCK_WORDS = (NOT_RUNNING_WORDS, OFF_WORDS, COMPUTE_WORDS)
OPENING_BOUND_SECONDS = 180   # "Opening the application." is never said for longer than this
COMPUTE_CODE = "APPLICATION_BROWSER_SHARED_BUDGET_EXHAUSTED"   # the hold of a job whose session could not be booked
COMPUTE_ROOM_SECONDS = 240    # the room a session needs, as `applications.cycle` asks


def initialize(database):
    database.executescript(f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            job_key TEXT PRIMARY KEY, state TEXT NOT NULL, requested_at TEXT NOT NULL, human_check TEXT,
            request_id TEXT, started_at TEXT, ended_at TEXT, polled_at TEXT, stop_request TEXT);
        CREATE TABLE IF NOT EXISTS {READY} (job_key TEXT PRIMARY KEY, review TEXT NOT NULL, at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS {ORDERS} (kind TEXT PRIMARY KEY, check_order TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS {MEASUREMENTS} (
            id INTEGER PRIMARY KEY, kind TEXT NOT NULL, check_order TEXT NOT NULL, seconds REAL NOT NULL,
            job_key TEXT, request_id TEXT, at TEXT NOT NULL);
    """)


def _watched_column(database):
    columns = {row[1] for row in database.execute(f"PRAGMA table_info({TABLE})")}
    if "polled_at" not in columns:
        database.execute(f"ALTER TABLE {TABLE} ADD COLUMN polled_at TEXT")
    if "stop_request" not in columns:   # E16 Y1 c: the one session this request asked to stop (session_yield.ask)
        database.execute(f"ALTER TABLE {TABLE} ADD COLUMN stop_request TEXT")


def _present(database):
    return bool(database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone())


def _row(database, job_key):
    if not _present(database):
        return None
    return database.execute(f"SELECT * FROM {TABLE} WHERE job_key=? AND state<>?", (job_key, ENDED)).fetchone()


def live(database, job_key):
    """True while the job has an attended request that has not ended (REQUESTED or STARTED)."""
    try:
        return _row(database, job_key) is not None
    except sqlite3.Error:
        return False


# --- the order, one setting per kind of check ---------------------------------------------------------------

def order_for(database, kind):
    """The order an attended session works in for this kind of check: the stored setting, else HALF_FIRST
    (the safe order: the pass is used sooner)."""
    try:
        row = database.execute(f"SELECT check_order FROM {ORDERS} WHERE kind=?", (kind or "OTHER",)).fetchone()
    except sqlite3.Error:
        row = None
    return row[0] if row and row[0] in ORDER_VALUES else HALF_FIRST


def set_order(database, kind, order, now):
    """Store the order for one kind of check (the owner's decision, from the measured numbers)."""
    import access_outcomes
    if kind not in access_outcomes.CHECK_KINDS or order not in ORDER_VALUES:
        raise ValueError("HUMAN_CHECK_ORDER_INVALID")
    initialize(database)
    database.execute(f"INSERT INTO {ORDERS}(kind,check_order,updated_at) VALUES (?,?,?) "
                     "ON CONFLICT(kind) DO UPDATE SET check_order=excluded.check_order,updated_at=excluded.updated_at",
                     (kind, order, now))


def measurements(database, kind=None):
    """The recorded numbers, oldest first: seconds from the user's Done to the arm of a submit the site
    accepted, with the kind of check and the order that session worked in."""
    if not database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (MEASUREMENTS,)).fetchone():
        return []
    rows = database.execute(f"SELECT kind,check_order,seconds,job_key,request_id,at FROM {MEASUREMENTS} ORDER BY id").fetchall()
    return [dict(zip(("kind", "order", "seconds", "job_key", "request_id", "at"), row)) for row in rows
            if kind is None or row[0] == kind]


def _measure(database, row, request, now):
    """After an attended session: when its submit was armed after a Done and the page confirmed it, the
    seconds between the two are recorded. Anything else records nothing."""
    import application_browser
    import form_check_hold
    try:
        result = json.loads(request["result"]) if request["result"] else {}
        folder = database.execute("SELECT detail FROM desktop_events WHERE request_id=? AND event='BROWSER_SESSION_DIRECTORY' "
                                  "ORDER BY id DESC LIMIT 1", (request["request_id"],)).fetchone()
        record = form_check_hold.read(Path(json.loads(folder[0])["directory"]) / application_browser.HOLD_FILE) if folder else None
        done_at = (record or {}).get("done_at")
        if not request["armed_at"] or result.get("acknowledged") is not True or not done_at:
            return
        seconds = (dt.datetime.fromisoformat(request["armed_at"]) - dt.datetime.fromisoformat(done_at)).total_seconds()
        if seconds < 0:
            return
        known = json.loads(row["human_check"]) if row["human_check"] else {}
        kind = known.get("kind") or "OTHER"
        database.execute(f"INSERT INTO {MEASUREMENTS}(kind,check_order,seconds,job_key,request_id,at) VALUES (?,?,?,?,?,?)",
                         (kind, order_for(database, kind), round(seconds, 1), row["job_key"], request["request_id"], now))
    except (sqlite3.Error, TypeError, ValueError, KeyError, OSError):
        LOG.warning("ATTENDED_SUBMIT_NOT_MEASURED")


# --- the request ----------------------------------------------------------------------------------------

def form_check_wait(database, job_key):
    """The job's open wait when it is a human check on the FORM (`access_outcomes.check`), else None."""
    import access_outcomes
    wait = access_outcomes.open_wait(database, job_key)
    known = access_outcomes.check(wait) if wait and wait["reason"] == "HUMAN_CHECK" else None
    return wait if access_outcomes.form_check(known) else None


def approved(database, job_key):
    """False only in Review mode for a job with no approved application (E16.T4)."""
    import approvals
    if approvals.mode(database) != approvals.REVIEW:
        return True
    try:
        return bool(database.execute("SELECT 1 FROM application_approvals WHERE job_key=? AND state='APPROVED'",
                                     (job_key,)).fetchone())
    except sqlite3.Error:
        return False


def _running():
    import scheduler_loop
    return scheduler_loop.running()


def in_the_way(database, now):
    """The sentence for what the product KNOWS keeps the applications job from starting a session soon, or
    None: Sidedoor is not running (or paused), applications are switched off, or the day's compute is used
    up. Today's research is not one of them: an attended submit goes ahead while research holds the turn
    (owner, 2026-11-13, "E1: A"; `orchestrator.applications_job`). Never raises."""
    import scheduler_control
    import scheduler_loop
    try:
        if not _running() or scheduler_control.paused(database):
            return NOT_RUNNING_WORDS
        config = scheduler_loop.load_config()
        if config.get("scope") == "monitor_existing_pipeline" or not config.get("applications_enabled", False):
            return OFF_WORDS
        import workflow_tasks
        day = workflow_tasks.budget_day(now, config.get("timezone", "America/Los_Angeles"))
        try:
            used = workflow_tasks.compute_used(database, day)
        except sqlite3.Error:
            used = 0   # nothing was ever booked on this install
        if config.get("daily_compute_seconds", 10800) - used < COMPUTE_ROOM_SECONDS:
            return COMPUTE_WORDS
    except Exception as error:
        LOG.warning("ATTENDED_SUBMIT_REASONS_NOT_READ (%s)", type(error).__name__)
    return None


def _past(since, now, seconds=None):
    try:
        return (dt.datetime.fromisoformat(now) - dt.datetime.fromisoformat(since)).total_seconds() > (
            OPENING_BOUND_SECONDS if seconds is None else seconds)
    except (TypeError, ValueError):
        return False


def press(database, job_key, now):
    """The card's button on a form check. Returns None when the request is recorded (the job is released and
    due now), else the sentence that says why not; nothing is changed then. Never for a job whose submission
    may already be with the employer (`access_outcomes.resume` refuses it; its wait and card stay)."""
    import access_outcomes
    wait = form_check_wait(database, job_key)
    if wait is None:
        return ""
    if not approved(database, job_key):
        return APPROVE_FIRST_WORDS
    reason = in_the_way(database, now)
    if reason:
        return reason
    initialize(database)
    with database:
        access_outcomes._lock(database)
        if not access_outcomes.resume(database, job_key, now, siblings=False):
            return ""
        known = access_outcomes.check(wait)
        _watched_column(database)
        database.execute(f"INSERT OR REPLACE INTO {TABLE}(job_key,state,requested_at,human_check,polled_at) VALUES (?,?,?,?,?)",
                         (job_key, REQUESTED, now, json.dumps(known, sort_keys=True), now))
        import applications
        applications.event(database, job_key, "ATTENDED_SUBMIT_REQUESTED", {"human_check": known}, now)
    import scheduler_loop
    scheduler_loop.run_now("applications")   # at once: the user is waiting for the page
    return None


def remember_ready(database, job_key, review, now):
    """`applications.process`, as it makes a job with a form check wait for its card: the read this
    application was answered (and approved) from is kept, so the press goes straight to the attended submit
    with no second read of the posting (the user is waiting). Call inside a transaction."""
    import applications
    initialize(database)
    kept = {key: value for key, value in review.items() if key != applications.REUSED_READ_MARKER}
    database.execute(f"INSERT OR REPLACE INTO {READY}(job_key,review,at) VALUES (?,?,?)", (job_key, json.dumps(kept), now))


def ready_read(database, job_key):
    """The kept read of a job whose attended submit was asked for, marked as a carried-forward read
    (`applications.REUSED_READ_MARKER`), or None."""
    import applications
    try:
        row = database.execute(f"SELECT review,at FROM {READY} WHERE job_key=?", (job_key,)).fetchone()
        review = json.loads(row[0]) if row else None
    except (sqlite3.Error, TypeError, ValueError):
        return None
    return dict(review, **{applications.REUSED_READ_MARKER: row[1]}) if isinstance(review, dict) else None


def watched(row, now):
    """True while the request's card asked about it within WATCH_SECONDS of `now` (the press counts)."""
    try:
        asked = row["polled_at"] or row["requested_at"]
        return (dt.datetime.fromisoformat(now) - dt.datetime.fromisoformat(asked)).total_seconds() <= WATCH_SECONDS
    except (TypeError, ValueError, IndexError, KeyError):
        return False


def due(database, now=None):
    """The jobs whose attended submit was asked for, has not started, and whose card is being watched now,
    oldest request first. A request nobody is watching is ended here (when this connection may write): its
    job then waits for its card again (`applications.process`), and no session starts unseen."""
    if not _present(database):
        return []
    import applications
    now = now or applications.stamp()
    found, stale = [], []
    try:
        _watched_column(database)
    except sqlite3.Error:
        pass
    for row in database.execute(f"SELECT * FROM {TABLE} WHERE state=? ORDER BY requested_at,job_key", (REQUESTED,)).fetchall():
        (found if watched(row, now) else stale).append(row["job_key"])
    if stale:
        try:
            with _written(database):
                for key in stale:
                    database.execute(f"UPDATE {TABLE} SET state=?,ended_at=? WHERE job_key=? AND state=?",
                                     (ENDED, now, key, REQUESTED))
        except sqlite3.Error:
            pass
    return found


def claim(database, config, job_key, operation, request_id, now):
    """The adapter, about to run `operation` for the job: the config for that session. A requested job's
    SUBMIT is attended (once: the row is STARTED with this request); anything else gets `config` unchanged."""
    import form_check_hold
    if operation != "submit":
        return config
    row = _row(database, job_key)
    if row is None or row["state"] != REQUESTED or not watched(row, now):
        return config
    try:
        known = json.loads(row["human_check"]) if row["human_check"] else {}
    except (TypeError, ValueError):
        known = {}
    start = form_address(ready_read(database, job_key))
    with _written(database):
        database.execute(f"UPDATE {TABLE} SET state=?,request_id=?,started_at=? WHERE job_key=? AND state=?",
                         (STARTED, request_id, now, job_key, REQUESTED))
        database.execute(f"DELETE FROM {READY} WHERE job_key=?", (job_key,))   # the kept read has served
    told = {"human_check": known, "order": order_for(database, known.get("kind"))}
    if start:
        told["start_url"] = start   # the form's own address, as the read recorded it: the user sees the form first
    return dict(config, **{form_check_hold.ATTENDED_KEY: told})


def form_address(review):
    """The application form's own address as the kept read recorded it (its verified official destination),
    or None: the session then starts at the posting's address and the AI goes to the form."""
    if not isinstance(review, dict) or review.get("official_destination_verified") is not True:
        return None
    return official(review.get("official_url"))


def official(url):
    """`url` when it passes the official-destination rule a submit's own address passes
    (`application_destination.public_url`), else None."""
    import application_destination
    try:
        return application_destination.public_url(url)
    except ValueError:
        return None


def ended(database, job_key, request_id, now):
    """The adapter, after that session returned (however it ended): the row ends, and the session's number
    is recorded when it has one (`_measure`). Never raises."""
    try:
        row = _row(database, job_key)
        if row is None or row["state"] != STARTED or row["request_id"] != request_id:
            return
        request = database.execute("SELECT * FROM desktop_requests WHERE request_id=?", (request_id,)).fetchone()
        with _written(database):
            database.execute(f"UPDATE {TABLE} SET state=?,ended_at=? WHERE job_key=? AND request_id=?",
                             (ENDED, now, job_key, request_id))
            if request is not None:
                _measure(database, row, request, now)
    except sqlite3.Error:
        LOG.warning("ATTENDED_SUBMIT_END_NOT_RECORDED")


@contextlib.contextmanager
def _written(database):
    """One write, committed here unless the caller already holds a transaction (then it is the caller's)."""
    if database.in_transaction:
        yield
    else:
        with database:
            yield


def _running_request(database, row, now):
    """The STARTED row's request while its session can still be running, else None."""
    request = database.execute("SELECT * FROM desktop_requests WHERE request_id=?", (row["request_id"],)).fetchone()
    if request is None or request["state"] != "CLAIMED" or not request["lease_until"] or request["lease_until"] <= now:
        return None
    return request


def settle(database, job_key, now, write=True):
    """The job's request, if it is still live: a request whose job went another way (closed, held on
    anything but its approval, waiting on the user again) or whose session is over without the adapter
    saying so (a killed process) is ended, so no card is left saying something that is no longer true."""
    row = _row(database, job_key)
    if row is None:
        return None
    job = database.execute("SELECT state,blocker FROM application_jobs WHERE job_key=?", (job_key,)).fetchone()
    import applications
    over = (job is None or job["state"] in applications.FINAL
            or (job["state"] == "BLOCKED" and job["blocker"] not in APPROVAL_CODES)
            or (row["state"] == STARTED and _running_request(database, row, now) is None))
    if not over:
        return row
    if write:   # a page being drawn reads only: the row is ended by the next press, poll or session
        with _written(database):
            database.execute(f"UPDATE {TABLE} SET state=?,ended_at=? WHERE job_key=? AND state<>?", (ENDED, now, job_key, ENDED))
    return None


def rehold(database, job_key, now):
    """End the job's request that has not started and make the job wait for its card again, with the check
    it was asked for: one transaction. True when the job now waits (its card's button makes a new request);
    False when nothing was changed (no such request, or the job cannot be held: then the request stays).
    Never a job whose submission may have reached the employer."""
    import access_outcomes
    import applications
    row = _row(database, job_key)
    if row is None or row["state"] != REQUESTED or applications.uncertain_submission(database, job_key):
        return False
    job = database.execute("SELECT * FROM application_jobs WHERE job_key=?", (job_key,)).fetchone()
    if job is None or job["state"] in applications.FINAL:
        return False
    try:
        known = json.loads(row["human_check"]) if row["human_check"] else None
    except (TypeError, ValueError):
        known = None
    with _written(database):
        access_outcomes._lock(database)
        waiting = access_outcomes.open_wait(database, job_key) is not None or access_outcomes.record(
            database, dict(job), "CAPTCHA_REQUIRED", now,
            result={"blocked": {"code": "CAPTCHA_REQUIRED", "human_check": known}}) == access_outcomes.WAITING
        if not waiting:
            return False
        database.execute(f"UPDATE {TABLE} SET state=?,ended_at=? WHERE job_key=? AND state=?", (ENDED, now, job_key, REQUESTED))
        applications.event(database, job_key, "ATTENDED_SUBMIT_HANDED_BACK", {"human_check": known}, now)
    return True


def unwatched(database, job_key, now):
    """True when the job's attended submit was asked for and its card is no longer being watched."""
    try:
        row = _row(database, job_key)
    except sqlite3.Error:
        return False
    return row is not None and row["state"] == REQUESTED and not watched(row, now)


def hand_back(database, root, job_key, request_id, now):
    """The adapter, about to start the submit session of a job whose card stopped being watched in the
    seconds since the round checked (`due`): no session is started. The request is answered here as stopped
    before arming on the human check, which is the existing wall path (`applications.hold_submission_blocker`:
    the try is not counted, the attempt is closed as not submitted, the job waits on the user with its card).
    The attended request ends. Returns True when the request was answered."""
    import applications
    import desktop_adapter
    row = _row(database, job_key)
    try:
        known = json.loads(row["human_check"]) if row is not None and row["human_check"] else None
    except (TypeError, ValueError):
        known = None
    envelope = desktop_adapter.claim(database, root, "attended-submit", now, request_id=request_id)
    if not envelope:
        return False
    desktop_adapter.complete(database, dict(envelope, result={
        "acknowledged": False,
        "blocked": {"code": "CAPTCHA_REQUIRED", "next_action": applications.FORM_CHECK_ACTION, "human_check": known}}), now)
    with _written(database):
        database.execute(f"UPDATE {TABLE} SET state=?,ended_at=? WHERE job_key=? AND state=?", (ENDED, now, job_key, REQUESTED))
        applications.event(database, job_key, "ATTENDED_SUBMIT_HANDED_BACK", {"request_id": request_id, "human_check": known}, now)
    return True


def _other_submit(database, job_key, now):
    """True while ANOTHER job's submit session is running (a claimed submit whose lease is live)."""
    return bool(database.execute(
        "SELECT 1 FROM desktop_requests WHERE state='CLAIMED' AND operation='submit' AND lease_until>? "
        "AND job_key<>? LIMIT 1", (now, job_key)).fetchone())


def status(database, job_key, now, write=True):
    """What the card can show for a live request, or None when the job has none: {"directory"} once this
    job's attended session has started, else {"words", "stop"}. `stop`: the line says something that will
    not change soon (approve first; what the product knows is in the way, `in_the_way`; or "Opening the
    application." has been said for OPENING_BOUND_SECONDS). The card keeps asking all the same (`step`), which
    is what keeps the request due (`due`)."""
    row = settle(database, job_key, now, write)
    if row is None:
        return None
    if row["state"] == STARTED:
        found = database.execute("SELECT detail FROM desktop_events WHERE request_id=? AND event='BROWSER_SESSION_DIRECTORY' "
                                 "ORDER BY id DESC LIMIT 1", (row["request_id"],)).fetchone()
        try:
            if found:
                import form_check_hold
                folder = json.loads(found[0])["directory"]
                return {"directory": folder, "since": row["started_at"], "done": form_check_hold.user_done(folder)}
        except (TypeError, ValueError, KeyError):
            pass
        return opening(row["started_at"], now)
    job = database.execute("SELECT state,blocker FROM application_jobs WHERE job_key=?", (job_key,)).fetchone()
    if job is not None and job["state"] == "BLOCKED" and job["blocker"] == "AWAITING_APPLICATION_APPROVAL":
        return {"words": APPROVE_FIRST_WORDS, "stop": True}
    if job is not None and job["blocker"] == COMPUTE_CODE:   # the session's compute could not be booked today
        return {"words": COMPUTE_WORDS, "stop": True}
    reason = in_the_way(database, now)
    if reason:
        return {"words": reason, "stop": True}
    # Another application's session holds the browser (E16 Y1 c): it is asked to stop and requeued, and this
    # one starts next in the round ("Opening the application."). One that may not be stopped (an armed submit,
    # or a session of another kind) runs to its end: the short wait.
    import session_yield
    if session_yield.ask(database, job_key, now, write) == session_yield.WAIT:
        return {"words": BUSY_WORDS, "stop": False}
    return opening(row["requested_at"], now)


def due_requests(database, now=None):
    """`due`, with each request's own time: [(job key, requested at)]. The round takes each request once."""
    keys = due(database, now)
    if not keys:
        return []
    found = {row[0]: row[1] for row in database.execute(f"SELECT job_key,requested_at FROM {TABLE} WHERE state=?", (REQUESTED,))}
    return [(key, found.get(key)) for key in keys]


def opening(since, now):
    """"Opening the application.", and after OPENING_BOUND_SECONDS a line that is still true, said once."""
    return {"words": SLOW_WORDS, "stop": True} if _past(since, now) else {"words": STARTING_WORDS, "stop": False}


# --- the card -------------------------------------------------------------------------------------------

def cards(database, now):
    """The live requests as cards (`access_cards.view`): the job stopped waiting when the card was pressed,
    so its own card is gone; this one stays until the attended session is over."""
    found = []
    try:   # an install with no database yet, or one from before this table: no card
        if database is None or not _present(database):
            return []
        for row in database.execute(f"SELECT job_key FROM {TABLE} WHERE state<>? ORDER BY requested_at,job_key", (ENDED,)).fetchall():
            shown = status(database, row[0], now, write=False)
            job = database.execute("SELECT job_key,employer,payload FROM application_jobs WHERE job_key=?", (row[0],)).fetchone()
            if shown is not None and job is not None:
                found.append((job, bool(shown.get("done"))))
    except sqlite3.Error:
        return []
    return found


def _poll(job_key, words, at=None):
    """The line the card shows while there is no page yet (none after the user's Done: the card says that
    itself); it asks again by itself."""
    values = html.escape(json.dumps({"job_key": job_key, "at": at or "approvals", "poll": "1"}), quote=True)
    line = '<p class="access-hint muted" role="status">%s</p>' % html.escape(words) if words else ""
    return ('<div hx-post="/access/step" hx-vals="%s" hx-trigger="every %ds" hx-target="this" hx-swap="outerHTML">'
            '%s</div>' % (values, POLL_SECONDS, line))


def _redraw(at):
    """The cards' section drawn again (the attended session is over: whatever the job now is shows)."""
    at = "overview" if at == "overview" else "approvals"
    return ('<div hx-get="/access/section?at=%s" hx-trigger="load" hx-target="#%s" hx-swap="outerHTML"></div>'
            % (at, "overview-needs" if at == "overview" else "sign-ins"))


def any_done(database, now):
    """True when a live attended session is in the state after the user's Done (its card says so)."""
    return any(done for _, done in cards(database, now))


def _note(words):
    return '<p class="access-hint muted" role="status">%s</p>' % html.escape(words)


def over(database, job_key):
    """True when the job's attended request has ended and none is live."""
    try:
        return bool(_present(database) and _row(database, job_key) is None and database.execute(
            f"SELECT 1 FROM {TABLE} WHERE job_key=? AND state=?", (job_key, ENDED)).fetchone())
    except sqlite3.Error:
        return False


def step(database, job_key, now, live=None, at=None, poll=False):
    """`access_cards.step` for a job with a form check or a live request: the HTML the card embeds, or None
    when this job is neither (the ordinary step applies). `live`: the dashboard's live view."""
    import live_view
    import user_step
    live = live or (user_step._live or {}).get("view")
    if _row(database, job_key) is None:
        if poll:
            # The card asking by itself is never a press: with no live request (its session ended), the
            # section is drawn again, once, and whatever the job now is shows.
            return _redraw(at) if over(database, job_key) else None
        if form_check_wait(database, job_key) is None:
            return None
        if live is None or not live.available(live_view.ATTACHED_PURPOSE):
            return None   # this computer cannot show a view: the card says so (`access_cards.step_refusal`)
        if getattr(live, "session", None) is not None:
            # A page the user opened themselves is open in Sidedoor (a sign-in or a site check): it is theirs
            # and is never stopped. Nothing is asked for; the card says what is true.
            return _note(live_view.ONE_AT_A_TIME_WORDS)
        refused = press(database, job_key, now)
        if refused is not None:
            return _note(refused) if refused else None
    if not poll and _row(database, job_key)["state"] == REQUESTED:
        # The button pressed again on a request that has not started ("Not open yet. Open this again."): the
        # applications job is started now, as at the first press, instead of at its next scheduled start.
        import scheduler_loop
        scheduler_loop.run_now("applications")
    with contextlib.suppress(sqlite3.Error):   # the card is being watched now (`due`)
        with _written(database):
            _watched_column(database)
            database.execute(f"UPDATE {TABLE} SET polled_at=? WHERE job_key=? AND state<>?", (now, job_key, ENDED))
    shown = status(database, job_key, now)
    if shown is None:
        return _redraw(at) if poll else None   # it ended just now (its row was settled by this very call)
    if shown.get("done"):
        # After the user's Done: no view, no button, no instruction; the card says it is done, and this asks
        # again until the session ends or its hold opens again (then the view comes back).
        return _poll(job_key, "", at)
    if "directory" in shown:
        # Never a blank page: the view is offered once the session's Chrome shows a real page (its guard
        # opens the application's address itself at once). Until then the card says it is opening.
        endpoint = live_view.session_endpoint(shown["directory"]) if live is not None else None
        if endpoint is None or live_view.session_page(endpoint) is None:
            shown = opening(shown.get("since"), now)
    if "directory" not in shown:
        # Every line keeps asking while the request is live (review, 2026-11-13): a card that stopped asking
        # lost its request 15 s later (`due`) and could never open. So a pressed card still on screen ends in
        # the view opening, or in a true line that changes when what is in the way does.
        if shown["words"] in STUCK_WORDS and rehold(database, job_key, now):
            # Nothing a few seconds of waiting can change (owner, 2026-11-13): the request is ended at once,
            # the job waits for its card again, and the card says the line with its button working (a press
            # makes a new request). Nothing live is left to ask about every second.
            return _note(shown["words"])
        return _poll(job_key, shown["words"], at)
    job = database.execute("SELECT payload FROM application_jobs WHERE job_key=?", (job_key,)).fetchone()
    try:
        domain = live_view.domain_of(json.loads(job[0])["url"])
    except (TypeError, ValueError, KeyError):
        domain = None
    target = live.offer_attached(job_key, shown["directory"], domain or "")
    if not target:
        return _note(SLOW_WORDS)
    return user_step.FRAGMENT % target   # the card shows the one instruction itself, directly above this
