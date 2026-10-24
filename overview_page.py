"""What the Overview page shows (E6.T3; E6.T0 spec section 5). Display only: every number here is a
count of records the rest of the product already wrote, read without writing anything (`snapshot`),
and every health item is a check result already stored by Setup (`setup_steps`). Nothing here
judges, writes, or calls the network or a model: a page load must stay cheap and side-effect free.

"Today" is the service day (owner, 2026-09-30: the day starts at `day_start_hour`, 9:00 Pacific),
from `workflow_tasks.service_day_start`, never calendar midnight: at 08:30 it is still the day
that began yesterday at 9:00.

A fresh install has no database file, or a database without some tables: each count is then 0 and
each time "Not yet", never an error. A section whose read fails with sqlite3.DatabaseError (an older
schema, a damaged file) shows "Not available" on its own and the others still render; "database is
locked/busy" is not degraded but raised to the dashboard's 503 "Busy, try again" handler, because it
is momentary and a retry shows real numbers.

Sources (why each is the record the product itself treats as that fact):
- research: `research` (one row per daily attempt: started_at, state), `research_days` (the
  service day's state and retry time, `daily_schedule`), `rw_companies` (companies research
  reserved for the day; `unplanned:` rows are observed work, not research, as linkedin_contacts
  reads them); roles and contacts found that day from the same plans' rw_results opportunities and
  rw_contacts reservations, the contact finder's included (`_research_found`).
- invitations sent: `engagement_events` kind initial_sent that is an invitation: purpose INVITE (the
  Unipile path) or INITIAL carrying campaign_id/preparation_action_id (the HeyReach campaign
  delivery record, outreach_campaign's delivery reconciliation). Any other INITIAL is a direct
  message into an existing chat. `engagement._record` refuses initial_sent without independent
  verification, and `sequence.reconcile_invite` writes it only once a read shows the invitation
  pending or accepted, so an acknowledgement alone is never counted. INVITE rows on
  `outreach_actions` in REQUEST_STARTED, ACKNOWLEDGED_DELIVERY_UNCONFIRMED or INVITE_RETRY_WAIT are
  "waiting for confirmation".
- the day's cap: `pacing.daily_cap` (the warm-up ramp the sender obeys); it needs only the first
  INVITE request on record, found in `outreach_actions` claimed_at order (`first_invite_request`).
- messages sent: `engagement_events` initial_sent that is not an invitation (FIRST_MESSAGE, a
  non-campaign INITIAL), followup_sent, and the REPLY receipts (outbound_observed with purpose
  REPLY), all written only on confirmed sends.
- replies received: the people Networking & Referrals lists whose latest message to the user is in the
  service day (`networking_page.replied_between`, its "Replied today" filter); cold strangers are not.
- applications today: `application_events` SUBMISSION_VERIFIED (as the daily report counts verified
  submissions). Right now: `application_jobs` SUBMITTING/RECONCILE (submitted, outcome being
  confirmed: `applications.UNCERTAIN`); BLOCKED jobs split by `applications.job_route`: route OWNER
  (`application_recovery.user_hold`) is "Blocked", any other route retries on its own ("Waiting to
  retry"), e.g. the three-strikes access blocks.
- needs you: `user_decisions` PENDING (owner questions, not the transport smoke test, not expired),
  owner holds (route OWNER) on jobs without such a pending question, messages waiting for approval
  (`approvals.waiting_count`) and the reply AI's notes not marked done (`reply_tools.open_flags`).
- compute: `workflow_tasks.compute_used` for `workflow_tasks.budget_day` (the same arithmetic
  `reserve_compute` admits against) against config `daily_compute_seconds`.
- last cycle: `runs` (one row per hourly cycle).
- Outreach and Applications "Last run" / "Next run": see `job_runs` below.

Indexes the reads use are created by the writers' own schema setup (engagement.initialize,
applications.initialize, orchestrator.connect, outreach.initialize_actions,
workflow_tasks.initialize); the page never creates one.
"""

import datetime as dt
import json
import os
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

import paths
import setup_steps
import workflow_tasks


INVITE_WAITING = ("REQUEST_STARTED", "ACKNOWLEDGED_DELIVERY_UNCONFIRMED", "INVITE_RETRY_WAIT")
DEFAULT_COMPUTE_SECONDS = 10800   # the default every reserve_compute caller uses
UNAVAILABLE = "unavailable"

# Plain words for stored state codes; an unknown code never shows as itself.
RESEARCH_DAY_WORDS = {"COMPLETE": "finished", "RUNNING": "running now", "RETRY_WAIT": "will retry",
                      "EXHAUSTED": "gave up for today"}
RESEARCH_RUN_WORDS = {"COMPLETE": "finished", "RUNNING": "running now", "FAILED": "did not finish",
                      "INTERRUPTED": "was interrupted"}
CYCLE_WORDS = {"COMPLETE": "finished", "DEGRADED": "finished with problems", "FAILED": "failed",
               "INTERRUPTED": "was interrupted", "RUNNING": "running now"}
UNKNOWN_WORDS = "ended"


def words(table, code):
    return table.get(str(code or "").upper(), UNKNOWN_WORDS)


# Within a day of the calendar's ends a time cannot be shown in every time zone (NL1): no time.
_EDGE = dt.timedelta(days=2)
_FIRST = dt.datetime.min.replace(tzinfo=dt.timezone.utc) + _EDGE
_LAST = dt.datetime.max.replace(tzinfo=dt.timezone.utc) - _EDGE


def _utc(value):
    """An aware UTC time, or None for text that is not one (or a date out of range, E6.T5 N18, or so
    close to the calendar's ends that local time cannot show it, NL1)."""
    try:
        moment = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        moment = moment.astimezone(dt.timezone.utc) if moment.tzinfo else None
    except (ValueError, OverflowError):
        return None
    return moment if moment is not None and _FIRST <= moment <= _LAST else None


def _json(text):
    """A stored JSON object, or None when the row's JSON is malformed (that row is skipped)."""
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _busy(error):
    message = str(error).lower()
    return "locked" in message or "busy" in message


# --- Reading without writing ------------------------------------------------------------------

def _wal_header(path):
    """True when the file's header says WAL journal mode (bytes 18 and 19 are 2)."""
    try:
        with open(path, "rb") as handle:
            header = handle.read(20)
    except OSError:
        return False
    return len(header) == 20 and header[18] == 2 and header[19] == 2


def _stamp(path):
    try:
        status = os.stat(path)
    except OSError:
        return None
    return (status.st_size, status.st_mtime_ns)


def _connect(uri):
    database = sqlite3.connect(uri, uri=True)
    database.row_factory = sqlite3.Row
    return database


def snapshot(path, use, connect_read_only):
    """`use(connection or None)` on the database at `path` without creating anything.

    A plain read-only open of a WAL database leaves -wal and -shm files behind when none exist. So
    when the file is in WAL mode and has no -wal file, it is opened `immutable=1`: SQLite deletes
    -wal only when the last connection closes after checkpointing every commit into the main file,
    so with no -wal no writer is connected and the main file holds every committed change; the read
    is not stale. A writer that connects during the read writes to its own new -wal, not the main
    file, and only checkpoints into the main file later. If anything changed during the read (a -wal
    file appeared, the main file's size or mtime moved) or the immutable read failed, it is thrown
    away and done again on the ordinary read-only connection, which takes the normal locks; the
    side files then exist because a writer is there anyway. Any other case uses the ordinary
    read-only connection (`connect_read_only`, dashboard.read_connection)."""
    path = Path(path)
    if not path.exists():
        return use(None)
    wal = path.with_name(path.name + "-wal")
    if _wal_header(path) and not wal.exists():
        before = _stamp(path)
        result, clean = None, False
        database = _connect(path.resolve().as_uri() + "?mode=ro&immutable=1")
        try:
            result, clean = use(database), True
        except sqlite3.DatabaseError:
            clean = False
        finally:
            database.close()
        if clean and not wal.exists() and _stamp(path) == before:
            return result
    database = connect_read_only()
    try:
        return use(database)
    finally:
        database.close()


class Reader:
    """Read-only queries that treat a missing database or table as empty. `broken` is set when the
    file cannot be read at all (not a database)."""

    def __init__(self, database):
        self.database = database
        self.tables = set()
        self.broken = False
        if database is not None:
            try:
                self.tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            except sqlite3.DatabaseError as error:
                if _busy(error):
                    raise
                self.broken = True

    def has(self, *tables):
        return all(table in self.tables for table in tables)

    def rows(self, sql, parameters=(), *tables):
        if not self.has(*tables):
            return []
        return self.database.execute(sql, parameters).fetchall()

    def count(self, sql, parameters=(), *tables):
        found = self.rows(sql, parameters, *tables)
        return found[0][0] if found else 0


def section(reader, read, *arguments):
    """One section's numbers, or UNAVAILABLE when its read fails on the database (older schema,
    damaged file). Locked/busy goes on to the 503 handler; nothing outside sqlite3.DatabaseError
    is caught."""
    if reader.broken:
        return UNAVAILABLE
    try:
        return read(reader, *arguments)
    except sqlite3.DatabaseError as error:
        if _busy(error):
            raise
        return UNAVAILABLE


def _within(rows, start, end, column=0):
    """Rows whose timestamp (column) falls in [start, end); parsed, never compared as text."""
    return [row for row in rows if (moment := _utc(row[column])) is not None and start <= moment < end]


def _prefilter(start):
    # A coarse text bound (one day early) for the index range; the exact test is _within.
    return (start - dt.timedelta(days=1)).date().isoformat()


# --- Sections ------------------------------------------------------------------------------------

def research(reader, config, now, running):
    zone = config.get("timezone") or "America/Los_Angeles"
    hour = config.get("research_hour", workflow_tasks.day_start_hour(config))
    last = reader.rows("SELECT id,started_at,state FROM research ORDER BY started_at DESC LIMIT 1", (), "research")
    day = workflow_tasks.service_date(now, zone, hour)
    today = reader.rows("SELECT state,next_retry_at FROM research_days WHERE day=?", (day,), "research_days")
    state = today[0]["state"] if today else None
    if state == "RUNNING":
        upcoming = {"label": "Running now"}
    elif state in ("COMPLETE", "EXHAUSTED"):
        upcoming = {"at": workflow_tasks.next_day_start(now, zone, hour)}
    elif state == "CODEX_WAIT":   # no usable Codex: starts once it is found again (daily_schedule.codex_wait)
        upcoming = {"label": "Once Sidedoor finds Codex"}
    elif state == "RETRY_WAIT" and _utc(today[0]["next_retry_at"]) and _utc(today[0]["next_retry_at"]) > now:
        upcoming = {"at": _utc(today[0]["next_retry_at"])}
    elif config.get("research_enabled") is False:
        upcoming = {"label": "Off"}
    else:  # today's research has not run: the scheduler starts it on its next pass
        upcoming = {"label": "As soon as Sidedoor runs" if not running else "Now"}
    companies = reader.count("SELECT COUNT(*) FROM rw_companies WHERE day=? AND plan_id NOT LIKE 'unplanned:%'",
                             (day,), "rw_companies")
    found_today = _research_found(reader, day) if (today or companies) else None
    found = None
    if last:
        # The day's own state says what happens next (will retry, gave up); else the attempt's.
        own = reader.rows("SELECT state FROM research_days WHERE attempt_id=?", (last[0]["id"],), "research_days")
        found = {"at": _utc(last[0]["started_at"]),
                 "words": words(RESEARCH_DAY_WORDS, own[0]["state"]) if own else words(RESEARCH_RUN_WORDS, last[0]["state"])}
    return {"last": found, "next": upcoming, "companies": companies if found_today else None,
            "roles": found_today and found_today["roles"], "contacts": found_today and found_today["contacts"]}


def _research_found(reader, day):
    """Roles and contacts the service day's research found (None counts mean "Not run yet").

    The day's research runs are its plans: the rw_companies plan ids (`unplanned:` rows excluded,
    as for companies). Roles: the distinct job_keys in those plans' rw_results `processed`
    opportunities, the roles research kept (fit NONE goes to rejected_opportunities and is not
    counted; a retried worker's second row for the same role counts once). Contacts: the distinct
    people reserved under those plans (rw_contacts historical=0; a drafted person keeps that row),
    plus the new people a completed contact-finder run for one of those research runs reserved
    under its own `linkedin-contacts-plan:` (linkedin_contact_runs.plan_id, tied to the research
    run by its result's research_result_id, else research_plan_id, as linkedin_contacts.history
    does). A held person re-reserved later keeps an earlier day's plan and is counted on that day."""
    plans = {row[0] for row in reader.rows(
        "SELECT DISTINCT plan_id FROM rw_companies WHERE day=? AND plan_id NOT LIKE 'unplanned:%'", (day,), "rw_companies")}
    if not plans:
        return {"roles": 0, "contacts": 0}
    marks = ",".join("?" for unused in plans)
    results = reader.rows(f"SELECT id,processed FROM rw_results WHERE plan_id IN ({marks})", tuple(plans), "rw_results")
    roles = set()
    for row in results:
        processed = _json(row[1])
        for role in (processed.get("opportunities") if isinstance(processed, dict) else None) or []:
            if isinstance(role, dict) and role.get("job_key"):
                roles.add(role["job_key"])
    owners = set(plans)
    result_ids = {row[0] for row in results}
    for row in reader.rows("SELECT plan_id,result FROM linkedin_contact_runs WHERE state='COMPLETE' AND plan_id IS NOT NULL",
                           (), "linkedin_contact_runs"):
        value = _json(row[1])
        if isinstance(value, dict) and (value.get("research_result_id") in result_ids or value.get("research_plan_id") in plans):
            owners.add(row[0])
    marks = ",".join("?" for unused in owners)
    contacts = reader.count(f"SELECT COUNT(DISTINCT identity) FROM rw_contacts WHERE historical=0 AND plan_id IN ({marks})",
                            tuple(owners), "rw_contacts")
    return {"roles": len(roles), "contacts": contacts}


def _invitation(kind, payload):
    if kind != "initial_sent":
        return False
    purpose = payload.get("purpose")
    return purpose == "INVITE" or (purpose == "INITIAL" and bool(payload.get("campaign_id") or payload.get("preparation_action_id")))


def first_invite_request(reader):
    """The earliest INVITE request start on record (what `pacing.daily_cap`'s warm-up counts from),
    read in claimed_at order: an attempt starts after its row was claimed, so once a row's
    claimed_at is past the earliest start seen, no later row can start earlier. Rows with malformed
    JSON or an unreadable started_at are skipped."""
    earliest = None
    cursor = reader.database.execute("SELECT claimed_at,payload,result FROM outreach_actions "
                                     "WHERE result IS NOT NULL ORDER BY claimed_at")
    for claimed_at, payload, result in cursor:
        claimed = _utc(claimed_at) if claimed_at else None
        if earliest is not None and claimed is not None and claimed > earliest:
            break
        action, outcome = _json(payload), _json(result)
        if not action or action.get("purpose") != "INVITE" or not outcome:
            continue
        attempts = outcome.get("attempts")
        for attempt in attempts if isinstance(attempts, list) else []:
            started = _utc(attempt.get("started_at")) if isinstance(attempt, dict) and attempt.get("started_at") else None
            if started is not None and (earliest is None or started < earliest):
                earliest = started
    return earliest


def outreach(reader, config, now, start, end):
    events = reader.rows("""SELECT occurred_at,kind,payload FROM engagement_events
                            WHERE occurred_at>=? AND kind IN ('initial_sent','followup_sent','outbound_observed','inbound')""",
                         (_prefilter(start),), "engagement_events")
    invitations = messages = replies = 0
    for _, kind, text in _within(events, start, end):
        payload = _json(text)
        if payload is None:
            continue
        if _invitation(kind, payload):
            invitations += 1
        elif kind in ("initial_sent", "followup_sent") or (kind == "outbound_observed" and payload.get("purpose") == "REPLY"):
            messages += 1
    # The people Networking & Referrals lists who wrote today (its "Replied today" filter): a stranger
    # who wrote first is not on that page, so not counted here either (E6.T5 N11).
    import networking_page
    replies = networking_page.replied_between(reader, start, end)
    waiting = 0
    for (text,) in reader.rows("SELECT payload FROM outreach_actions WHERE state IN (%s)" % ",".join("?" * len(INVITE_WAITING)),
                               INVITE_WAITING, "outreach_actions"):
        payload = _json(text)
        waiting += bool(payload and payload.get("purpose") == "INVITE")
    import pacing  # the sender's own warm-up rule; loaded only when the page needs it
    first = first_invite_request(reader) if reader.has("outreach_actions") else None
    cap = pacing.daily_cap(pacing._settings(config), [first] if first else [], now)
    return {"invitations": invitations, "cap": cap, "waiting": waiting, "messages": messages, "replies": replies}


def submitted_today(reader, start, end):
    verified = reader.rows("SELECT at FROM application_events WHERE event='SUBMISSION_VERIFIED' AND at>=?",
                           (_prefilter(start),), "application_events")
    return len(_within(verified, start, end))


def _blocked(reader):
    """(owner holds, automatic waits): BLOCKED jobs by `applications.job_route`, as job keys.

    One more hold needs the owner although its route is RECONCILE (O19): SUBMISSION_OUTCOME_UNKNOWN
    once the reconcile reads are spent. `applications.reconcile` stops reading at exactly that
    test, `reconcile_attempts >= RECONCILE_MAX_ATTEMPTS` (its `spent`), and holds the job with
    next_at NULL and the next action "check the ATS account or confirmation inbox yourself", so the
    same test is used here. (With application recovery disabled the code also stops earlier, after
    the fast reads; that case still shows as waiting to retry.)"""
    import applications as jobs
    owner, waiting = [], []
    for key, code, reads in reader.rows("SELECT job_key,blocker,reconcile_attempts FROM application_jobs WHERE state='BLOCKED'",
                                        (), "application_jobs"):
        code = str(code or "")
        spent = code == "SUBMISSION_OUTCOME_UNKNOWN" and (reads or 0) >= jobs.RECONCILE_MAX_ATTEMPTS
        (owner if spent or jobs.job_route(code, "BLOCKED") == "OWNER" else waiting).append(key)
    return owner, waiting


def right_now(reader):
    owner, waiting = _blocked(reader)
    return {"confirming": reader.count("SELECT COUNT(*) FROM application_jobs WHERE state IN ('SUBMITTING','RECONCILE')",
                                       (), "application_jobs"),
            "blocked": len(owner), "retrying": len(waiting)}


def _pending_questions(reader, now):
    columns = {row[1] for row in reader.rows("PRAGMA table_info(user_decisions)", (), "user_decisions")}
    test = " AND kind!='test'" if "kind" in columns else ""   # the transport smoke test is not a question
    return reader.rows("SELECT job_key FROM user_decisions WHERE state='PENDING' AND expires_at>?" + test,
                       (int(now.timestamp() * 1000),), "user_decisions")


def needs_you(reader, now):
    """Questions, owner holds without a question, messages waiting for approval
    (`approvals.waiting_count`) and the reply AI's notes not marked done (`reply_tools.open_flags`);
    each count links to its section of Approvals (E6.T4)."""
    import approvals
    import reply_tools
    questions = _pending_questions(reader, now)
    asked = {row[0] for row in questions}
    owner, _ = _blocked(reader)
    present = reader.database is not None
    return {"questions": len(questions), "failures": sum(1 for key in owner if key not in asked),
            "messages": approvals.waiting_count(reader.database) if present else 0,
            "notes": reply_tools.open_flag_count(reader.database) if present else 0}


def compute(reader, config, now):
    zone = config.get("timezone") or "America/Los_Angeles"
    day = workflow_tasks.budget_day(now, zone, workflow_tasks.day_start_hour(config))
    return workflow_tasks.compute_used(reader.database, day) if reader.has("worker_compute_reservations") else 0


def compute_in_flight(reader, config, now):
    """Seconds of today's usage that are browser bookings not yet settled (2026-10-22): work
    still running, or finished and waiting for the next settle, so the meter can say so."""
    if not (reader.has("application_browser_reservations") and reader.has("worker_compute_reservations")):
        return 0
    zone = config.get("timezone") or "America/Los_Angeles"
    day = workflow_tasks.budget_day(now, zone, workflow_tasks.day_start_hour(config))
    return reader.database.execute(
        """SELECT COALESCE(SUM(booking.seconds),0) FROM application_browser_reservations AS browser
           JOIN worker_compute_reservations AS booking ON booking.action_id=browser.reservation
           WHERE browser.settled_at IS NULL AND booking.day=?""", (day,)).fetchone()[0]


def last_cycle(reader):
    row = reader.rows("SELECT started_at,state,completed_at FROM runs ORDER BY started_at DESC LIMIT 1", (), "runs")
    return {"at": _utc(row[0]["completed_at"] or row[0]["started_at"]), "words": words(CYCLE_WORDS, row[0]["state"])} if row else None


# --- Outreach and Applications: last run and next run ----------------------------------------------
#
# Last run, from what each job already records:
# - the hourly round (outreach: invitations, follow-ups, replies, inbox): `runs` (one row per hourly
#   slot: started_at, state, completed_at; ":local-check" slots are `sidedoor job check`, not the
#   round), and reports/hourly-yield.json, which `orchestrator.run` writes when the round yields to
#   daily research (it claims no runs row then, so the slot stays free).
# - applications: its durable worker step (`workflow_tasks` worker 'applications', parent_run_id
#   'applications-job:...', as `reporting.applications_job` reads it; started at its first
#   workflow_task_events row), the SUBMISSION_VERIFIED `application_events` inside that run, and
#   reports/applications-service.json, which `orchestrator.applications_service` writes on every
#   run, a yield included (a yield records no step).
# - both: the scheduler's `job_failures` (a child that exited non-zero) when that child left no
#   record of its own (no record is as new as that child's start, `job_failures[job]["started"]`,
#   else the job's `last_fired` instant).
# A run that found nothing to do leaves no record and is never a Last run: a duplicate hourly slot
# ("This hourly slot is already recorded") and a skipped applications run ("Another applications
# run is active") write no runs row, step or report file, and exit 0, so the scheduler's
# `last_fired` for them changes nothing shown here.
# Next run: `scheduler_loop.schedule` (the scheduler's own due logic: latest instant + period), and
# `orchestrator.research_holds_turn(read_only=True)` (the test both jobs yield on).

RUN_WORDS = {"COMPLETE": "finished", "DEGRADED": "finished", "FAILED": "did not finish",
             "INTERRUPTED": "did not finish", "RUNNING": "running now"}
YIELD_WORDS = "waited for research"
FAILED_WORDS = "did not finish"


def _report(folder, name):
    """A report file the jobs write (reports/<name>), or None (missing or unreadable)."""
    if folder is None:
        return None
    try:
        return _json((Path(folder) / name).read_text())
    except (OSError, UnicodeDecodeError):
        return None


def _latest(records):
    """The newest of [(started, at, words)], by start time; None when there is none."""
    records = [record for record in records if record and record[0] is not None]
    return max(records, key=lambda record: record[0]) if records else None


def _crashed(found, scheduler, job):
    """The scheduler's record of a child of `job` that exited non-zero, when that child (started at
    or after `last_fired[job]`) left no record of its own; else what was found."""
    failure = (scheduler.get("job_failures") or {}).get(job)
    at = _utc(failure.get("at")) if isinstance(failure, dict) else None
    if at is None:
        return found
    # When the failing child started: the scheduler's own record of that start. `last_fired` is
    # only the fallback (a loop state without it): it also moves when an instant is passed over
    # while the previous child still runs, so a round that ran past the hour and then exited
    # non-zero would otherwise hide its own runs row.
    fired = _utc(failure.get("started"))
    if fired is None:
        fired = (scheduler.get("last_fired") or {}).get(job)
        try:
            fired = dt.datetime.fromtimestamp(fired, dt.timezone.utc) if fired is not None else None
        except (TypeError, ValueError, OverflowError, OSError):
            fired = None
    if found is not None and (fired is None or found[0] >= fired):
        return found
    return (at, at, FAILED_WORDS)


def _shown(found):
    return {"at": found[1], "words": found[2]} if found else None


def hourly_last(reader, scheduler, reports):
    row = reader.rows("SELECT started_at,state,completed_at FROM runs WHERE slot NOT LIKE '%:local-check' "
                      "ORDER BY started_at DESC LIMIT 1", (), "runs")
    records = []
    if row:
        started = _utc(row[0]["started_at"])
        records.append((started, _utc(row[0]["completed_at"]) or started, words(RUN_WORDS, row[0]["state"])))
    saved = _report(reports, "hourly-yield.json")
    if saved and saved.get("state") == "YIELDED_TO_DAILY_RESEARCH":
        at = _utc(saved.get("at"))
        records.append((at, at, YIELD_WORDS))
    return _shown(_crashed(_latest(records), scheduler, "run"))


def applications_last(reader, scheduler, reports):
    records = []
    row = reader.rows("""SELECT task.status, task.updated_at, task.result,
                (SELECT MIN(at) FROM workflow_task_events WHERE task_id=task.task_id) AS started_at
            FROM workflow_tasks AS task WHERE task.worker='applications' AND task.parent_run_id LIKE 'applications-job:%'
            ORDER BY task.updated_at DESC LIMIT 1""", (), "workflow_tasks", "workflow_task_events")
    if row:
        status, result = row[0]["status"], _json(row[0]["result"]) or {}
        started = _utc(row[0]["started_at"]) or _utc(row[0]["updated_at"])
        ended = _utc(row[0]["updated_at"]) or started
        if status == "RUNNING":
            text = "running now"
        elif status == "FAILED" or result.get("state") == "FAILED":
            text = FAILED_WORDS
        else:
            verified = reader.rows("SELECT at FROM application_events WHERE event='SUBMISSION_VERIFIED' AND at>=?",
                                   (_prefilter(started),), "application_events") if started else []
            submitted = len(_within(verified, started, ended + dt.timedelta(seconds=1))) if started else 0
            text = ("%d submitted" % submitted if submitted else
                    YIELD_WORDS if result.get("stopped") else "finished")
        records.append((started, ended, text))
    saved = _report(reports, "applications-service.json")
    if saved and saved.get("state") == "YIELDED_TO_DAILY_RESEARCH":
        records.append((_utc(saved.get("started_at")), _utc(saved.get("completed_at") or saved.get("started_at")), YIELD_WORDS))
    return _shown(_crashed(_latest(records), scheduler, "applications"))


def next_run(reader, job, config, now, scheduler, pause):
    """{"at": time} or {"label": words}: what the scheduler will do for `job` ("run" or
    "applications"), in the order that decides it."""
    if job == "applications" and not config.get("applications_enabled", False):
        return {"label": "Turned off"}   # orchestrator.application_cycle's own test
    if not scheduler.get("running"):
        return {"label": "Not running"}
    if scheduler.get("data_hold"):
        return {"label": "Waiting to move your data"}
    if isinstance(pause, dict) and (pause.get("user_paused_at") or pause.get("license_paused_at")):
        return {"label": "Paused"}
    if reader.database is not None:
        import orchestrator
        try:
            if orchestrator.research_holds_turn(config, reader.database, now, read_only=True):
                return {"label": "after today's research"}
        except ValueError:   # research_hour != day_start_hour: the jobs raise on it too; no hold shown
            pass
    elif config.get("research_enabled") and config.get("scope") != "monitor_existing_pipeline" \
            and "timezone" in config and "research_hour" in config:
        return {"label": "after today's research"}   # no database yet: no research day on record
    import scheduler_loop
    instants, periods = scheduler_loop.schedule(now, config)
    return {"at": dt.datetime.fromtimestamp(instants[job] + periods[job], dt.timezone.utc)}


def job_runs(reader, config, now, scheduler, pause, reports):
    return {"outreach": {"last": hourly_last(reader, scheduler, reports),
                         "next": next_run(reader, "run", config, now, scheduler, pause)},
            "applications": {"last": applications_last(reader, scheduler, reports),
                             "next": next_run(reader, "applications", config, now, scheduler, pause)}}


# --- Health: stored check results only ------------------------------------------------------------

SETTINGS_CONNECTIONS = "/settings#connections"
SETTINGS_LICENSE = "/settings#license"
NOT_CHECKED = "Not checked yet"


def _item(name, tone, text, at=None, fix=None, checked=None, timed=True):
    """One health line. tone: success (green), warning (amber), error (red). `at` is the last time
    the check was good; `checked` the time of a stored check that was not good (Setup keeps only
    each check's latest result, so an earlier good time is not on record); `fix` is
    {"label", "href"} or {"label", "post"} (a form button). A line whose text already says it was
    never checked carries no time line."""
    return {"name": name, "tone": tone, "text": text, "at": at, "fix": fix, "checked": None if at else checked,
            "timed": timed and text != NOT_CHECKED}


def _good_at(saved):
    return _utc(saved.get("at")) if saved.get("status") == "done" else None


JOB_NAMES = {"research": "Research", "run": "The hourly round (outreach, replies, inbox)", "applications": "Applications"}


def pause_flags(reader):
    """The two pause flags (`scheduler_control.state`); all None when the table does not exist yet."""
    import scheduler_control
    return scheduler_control.state(reader.database) if reader.database is not None else \
        dict.fromkeys(("user_paused_at", "user_pause_reason", "license_paused_at", "license_pause_reason"))


def paywall_pause(database, config, now=None):
    """E8.T3 PW1: the paywall's pause line: {"user_paused_since": "HH:MM" or None}; None when the
    flags cannot be read (the paywall then shows [Pause] only). [Pause] stays available while
    Expired (E6.T0 section 5); [Resume] never shows there."""
    try:
        flags = pause_flags(Reader(database))
    except sqlite3.Error:
        return None
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        zone = ZoneInfo(config.get("timezone") or "America/Los_Angeles")
    except Exception:
        zone = ZoneInfo("America/Los_Angeles")
    moment = _utc(flags.get("user_paused_at"))
    return {"user_paused_since": when(moment, now, zone) if moment else None}


def scheduler_item(scheduler, cycle, pause, fmt):
    """The Scheduler health line from the loop's own state (`scheduler_loop`: running, last_error,
    job_failures) and the pause flags. Display only: its buttons post to the existing /start,
    /pause and /resume."""
    if cycle == UNAVAILABLE:
        last, at = "Last cycle: not available", None
    elif cycle:
        last, at = "Last cycle " + cycle["words"], cycle["at"]
    else:
        last, at = "No cycle yet", None
    flags = pause if isinstance(pause, dict) else {}
    notes = []
    if scheduler.get("last_error"):
        notes.append({"text": "Sidedoor ran into a problem keeping its schedule.", "detail": str(scheduler["last_error"])})
    for job, failure in sorted((scheduler.get("job_failures") or {}).items()):
        moment = _utc(failure.get("at")) if isinstance(failure, dict) else None
        notes.append({"text": "%s last stopped with an error%s." % (JOB_NAMES.get(job, "A background task"),
                                                                     " at " + fmt(moment) if moment else "")})
    if notes:  # a loop problem or a crashed job: the way to what failed, whatever the state (O27)
        notes.append({"text": "See what failed.", "href": "/failures#scheduler"})
    if scheduler.get("data_hold"):  # E9.T1: a wait, not a failure
        notes.append({"text": "Waiting to move your data.", "detail": str(scheduler["data_hold"])})
    if pause == UNAVAILABLE:
        notes.append({"text": "Whether Sidedoor is paused is not available."})
    if not scheduler.get("running"):
        if flags.get("user_paused_at"):
            notes.append({"text": "Also paused by you since %s." % fmt(_utc(flags["user_paused_at"]))})
        item = _item("Scheduler", "error", "Not running. " + last, at, {"label": "Start", "post": "/start"}, timed=bool(at))
    elif flags.get("license_paused_at"):
        # The license pause clears only through the license (E6.T0 section 5): no [Resume] here.
        item = _item("Scheduler", "error", "Paused because of your license. " + last, at,
                     {"label": "Open License", "href": SETTINGS_LICENSE}, timed=bool(at))
        if flags.get("user_paused_at"):
            # Section 5: the user's own pause outlives the license pause; it still shows.
            notes.append({"text": "Also paused by you since %s." % fmt(_utc(flags["user_paused_at"]))})
        else:
            item["also"] = {"label": "Pause", "post": "/pause"}  # section 5: [Pause] stays available
    elif flags.get("user_paused_at"):
        item = _item("Scheduler", "warning", "Paused by you since %s. " % fmt(_utc(flags["user_paused_at"])) + last, at,
                     {"label": "Resume", "post": "/resume"}, timed=bool(at))
    else:
        tone = "warning" if notes else "success"
        item = _item("Scheduler", tone, "Running. " + last, at, {"label": "Pause", "post": "/pause"}, timed=bool(at))
    item["notes"] = notes
    return item


def browser_unavailable():
    """Why Sidedoor's browser cannot start now ("RUNTIME_MISSING", "PROFILE_IN_USE"), "SESSION_BUSY" when a
    Sidedoor session owns the Chrome on the profile (FG1 problem 2: never "close the window" then), or
    None (browser_runtime.display_state). Never raises."""
    try:
        import browser_runtime
        return browser_runtime.display_state(None)
    except Exception:
        return None


def browser_offscreen():
    """True when Sidedoor's session Chrome runs off-screen (browser_runtime.offscreen). Never raises."""
    try:
        import browser_runtime
        return browser_runtime.offscreen(None)
    except Exception:
        return False


def health(home, progress, scheduler, cycle, pause=None, fmt=None):
    state = progress["state"]
    steps = state["steps"]
    items = []

    import license_clock  # E8.T2: the stored license state; no relay call on render
    license = license_clock.view(home)
    checked = _utc(license["last_check"]) or _good_at(steps.get("license", {}))
    action = license["action"]
    fix = None
    if action:  # red and amber items show their banner's button (E6.T0 section 5)
        fix = {"label": action["label"], "post": action["post"]} if action.get("post") else \
            {"label": action["label"], "href": action["href"]}
    if license["state"] == "no_license":
        items.append(_item("License", "warning", "No license yet", fix={"label": "Enter license key", "href": SETTINGS_LICENSE}))
    else:
        tone = license["tone"] if license["tone"] in ("error", "warning") else "success"
        items.append(_item("License", "warning" if license["state"] == "checking" else tone,
                           license["text"], checked, fix))

    linkedin = steps.get("linkedin", {})
    status, offer = linkedin.get("status"), linkedin.get("offer")
    if status == "done":
        items.append(_item("LinkedIn", "success", "Connected", _good_at(linkedin)))
    elif status == "checking":
        items.append(_item("LinkedIn", "warning", "Waiting for LinkedIn", fix={"label": "Check LinkedIn", "href": SETTINGS_CONNECTIONS}))
    elif status is None:
        items.append(_item("LinkedIn", "warning", NOT_CHECKED, fix={"label": "Check LinkedIn", "href": SETTINGS_CONNECTIONS}))
    else:
        label = {"connect": "Connect LinkedIn", "reconnect": "Reconnect LinkedIn"}.get(offer, "Check LinkedIn")
        items.append(_item("LinkedIn", "error", linkedin.get("message") or "Needs you",
                           fix={"label": label, "href": SETTINGS_CONNECTIONS}, checked=_utc(linkedin.get("at"))))

    openrouter = steps.get("openrouter", {})
    if openrouter.get("status") == "done":
        items.append(_item("OpenRouter", "success", "Key works", _good_at(openrouter)))
    else:
        items.append(_item("OpenRouter", "error", openrouter.get("message") or NOT_CHECKED,
                           fix={"label": "Replace key", "href": SETTINGS_CONNECTIONS}, checked=_utc(openrouter.get("at"))))

    codex = steps.get("codex", {})
    if progress["done"][6]:
        items.append(_item("Codex", "success", "Signed in with ChatGPT", _good_at(codex)))
    else:  # the same fix as the "Codex stopped working." banner
        failed = codex.get("message") if codex.get("status") == "failed" else None
        items.append(_item("Codex", "error", failed or "Codex stopped working",
                           fix={"label": "Fix", "href": SETTINGS_CONNECTIONS}, checked=_utc(codex.get("at"))))

    email = steps.get("email", {})
    browser = browser_unavailable() if setup_steps.chrome_path() else None
    if not setup_steps.chrome_path():  # the same fix as the "Google Chrome stopped working." banner
        items.append(_item("Browser profile", "error", "Google Chrome stopped working", fix={"label": "Fix", "href": SETTINGS_CONNECTIONS}))
    elif browser == "RUNTIME_MISSING":   # E7.T1: the bundled Node / @playwright/mcp is gone
        import browser_runtime
        items.append(_item("Browser profile", "error", browser_runtime.UNAVAILABLE_WORDS["RUNTIME_MISSING"] + " "
                           + browser_runtime.UNAVAILABLE_TODO["RUNTIME_MISSING"]))
    elif browser == "PROFILE_IN_USE":    # a Chrome window holds Sidedoor's profile and no session owns it
        import browser_runtime
        items.append(_item("Browser profile", "warning", browser_runtime.UNAVAILABLE_WORDS["PROFILE_IN_USE"] + " "
                           + browser_runtime.UNAVAILABLE_TODO["PROFILE_IN_USE"],
                           fix={"label": "Open Settings", "href": SETTINGS_CONNECTIONS}))
    elif email.get("mismatch"):  # the same fix as the mailbox banner
        items.append(_item("Browser profile", "error", "Signed in to a different mailbox",
                           fix={"label": "Open Sidedoor's browser", "href": SETTINGS_CONNECTIONS}))
    elif setup_steps.email_locked(state):
        at = _good_at(email) or _utc(state.get("email_check_passed_at"))
        items.append(_item("Browser profile", "success", "Mailbox matches", at))
    else:
        items.append(_item("Browser profile", "warning", email.get("message") or "Email check not done yet",
                           fix={"label": "Open Settings", "href": SETTINGS_CONNECTIONS}))
    if setup_steps.chrome_path() and browser_offscreen():   # FG1 = b: where the window is, and when it shows
        import browser_runtime
        items.append(_item("Browser window", "success", browser_runtime.OFFSCREEN_WORDS, timed=False))

    if not isinstance(scheduler, dict):
        scheduler = {"running": bool(scheduler)}
    items.append(scheduler_item(scheduler, cycle, pause, fmt or (lambda moment: str(moment))))
    return items


def loop_state(running):
    """The scheduler loop's in-memory state, the fields `scheduler_loop.status()` returns, read from
    the loop object itself: status() also reads the pause flags through its own read-only
    connection, which would leave -wal/-shm files behind (O17); gather reads them from its snapshot."""
    import scheduler_loop
    loop = scheduler_loop.current()
    return {"running": bool(running),
            "last_error": getattr(loop, "last_error", None) if loop is not None else None,
            "job_failures": dict(getattr(loop, "job_failures", {}) or {}) if loop is not None else {},
            **({"data_hold": loop.data_hold} if isinstance(getattr(loop, "data_hold", None), str) else {}),
            **({"last_fired": dict(loop.last_fired)} if isinstance(getattr(loop, "last_fired", None), dict)
               and loop.last_fired else {})}


# --- [Start] ------------------------------------------------------------------------------------

NOT_STARTABLE_YET = "Sidedoor can't be started from this page yet. That comes in an update."


def start_message(saved, running):
    """Plain words for what [Start] did (`setup_steps.start_scheduler`'s stored result). A start the
    product cannot do yet says so without its internal name; a failure shows the start function's
    own message, which is written for the user."""
    if running or saved.get("status") == "done":
        return "Sidedoor is running."
    if saved.get("status") == setup_steps.NOT_BUILT:
        return NOT_STARTABLE_YET
    return saved.get("message") or "Sidedoor could not start. Try again."


# --- Formatting ---------------------------------------------------------------------------------

def when(moment, now, zone):
    """"10:02" today, "yesterday 09:00", "tomorrow 09:00", "Wed 10:02" within a week, else
    "Oct 3, 10:02" (local time)."""
    if moment is None:
        return None
    try:
        local, today = moment.astimezone(zone), now.astimezone(zone)
    except OverflowError:   # a time local time cannot show is no time (NL1)
        return None
    clock = local.strftime("%H:%M")
    days = (local.date() - today.date()).days
    if days == 0:
        return clock
    if days == 1:
        return "tomorrow " + clock
    if days == -1:
        return "yesterday " + clock
    if abs(days) < 7:
        return local.strftime("%a ") + clock
    return "%s %d, %s" % (local.strftime("%b"), local.day, clock)


def duration(seconds):
    minutes = int(seconds) // 60
    return "%d h %02d min" % divmod(minutes, 60) if minutes >= 60 else "%d min" % minutes


def gather(database, config, home, progress, scheduler, now=None, root=None):
    """Everything the Overview shows. `database` is a read-only connection or None (no file yet).
    `scheduler` is the loop's state ({"running", "last_error", "job_failures"}, `loop_state`) or a
    bare running flag. A section that cannot be read is UNAVAILABLE; `unreadable` is set when the
    file is not a database at all."""
    if not isinstance(scheduler, dict):
        scheduler = {"running": bool(scheduler)}
    scheduler_running = bool(scheduler.get("running"))
    now = now or dt.datetime.now(dt.timezone.utc)
    zone_name = config.get("timezone") or "America/Los_Angeles"
    zone = ZoneInfo(zone_name)
    hour = workflow_tasks.day_start_hour(config)
    start = workflow_tasks.service_day_start(now, zone_name, hour).astimezone(dt.timezone.utc)
    end = workflow_tasks.next_day_start(now, zone_name, hour).astimezone(dt.timezone.utc)
    reader = Reader(database)
    cycle = section(reader, last_cycle)
    pause = section(reader, pause_flags)
    fmt = lambda moment: when(moment, now, zone)
    found = {"research": section(reader, research, config, now, scheduler_running),
             "outreach": section(reader, outreach, config, now, start, end),
             "submitted": section(reader, submitted_today, start, end),
             "right_now": section(reader, right_now),
             "needs": section(reader, needs_you, now),
             "compute": {"used": section(reader, compute, config, now),
                         "in_flight": section(reader, compute_in_flight, config, now),
                         "allowed": config.get("daily_compute_seconds", DEFAULT_COMPUTE_SECONDS),
                         "resets": workflow_tasks.next_day_start(now, zone_name, hour)},
             "runs": section(reader, job_runs, config, now, scheduler, pause,
                             paths.reports(root, home) if (root is not None or home is not None) else None),
             "health": health(home, progress, scheduler, cycle, pause, fmt),
             "day_start": when(start, now, zone),
             "unreadable": reader.broken,
             "unavailable": UNAVAILABLE}
    found["when"] = lambda moment: when(moment, now, zone)
    found["duration"] = duration
    return found
