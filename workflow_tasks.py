import resources
import datetime as dt
import hashlib
import json
import math
import secrets
from pathlib import Path
from zoneinfo import ZoneInfo


REVISION = "handoff-2"
TERMINAL = {"COMPLETE", "CANCELLED", "EXHAUSTED"}
# Workers whose tasks have no deadline: only max_attempts ends them (owner, 2026-09-28, of the
# outreach history check, local records since 2026-09-29: "complete remove that deadline. The only constraint should be
# the 7 tries"). Their stored deadline column is kept for the schema and never read.
# Extended (owner, 2026-09-28: "Remove deadline for followup, remove deadline for reply review,
# remove deadline for enegagement review"): the follow-up/timed message writer, reply review and
# engagement review now end only on their tries as well.
# Extended (owner, 2026-10-02: "Remove the deadline from note writing, so only real tries can end
# it"): first-note composition. Its 24 h deadline closed tasks that had only waited on shared
# compute (a COMPUTE_WAIT spends no try), e.g. five drafts EXHAUSTED on 2026-10-03 with 2-3 tries used.
NO_DEADLINE_WORKERS = frozenset({"outreach-draft-inspection", "outreach-draft-composition", "message_writer",
                                 "pipeline_routing_review", "engagement_review"})
# Growing backoff, one entry per attempt (owner, 2026-09-23). Was [900, 3600] - two steps for
# three attempts, so a task that failed three times in an hour was terminal within the hour.
# 15m, 1h, 3h, 6h, 12h, 24h: seven attempts now span days rather than minutes, which is the
# point - an inconclusive read gets a next date, not a grave.
RETRY_BACKOFF_SECONDS = (900, 3600, 10800, 21600, 43200, 86400)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def instant(value):
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Timezone-aware task timestamps required")
    return parsed.astimezone(dt.timezone.utc)


def _configured_day_start_hour():
    # A missing config.json (a bare module import) means 0; a config.json that exists but cannot
    # be read or carries a bad hour fails loudly, so one process never books by calendar date into
    # a pool the 09:00 consumers already closed.
    path = resources.path("config.json")
    if not path.exists():
        return 0
    value = json.loads(path.read_text(encoding="utf-8")).get("day_start_hour", 0)
    if type(value) is not int or not 0 <= value <= 23:
        raise ValueError("CONFIG_DAY_START_HOUR_INVALID")
    return value


# The service day starts at this local hour (owner, 2026-09-30: "update the start time of the
# services from midnight to 9am pst"). Loaded ONCE from config.json "day_start_hour" at import,
# so call sites without a config in hand (reserve_compute and its many callers) still agree;
# callers that have a config pass day_start_hour(config). A missing key means 0, i.e.
# local midnight, exactly the old behaviour; an unreadable file or invalid hour raises. Changing it needs a process restart.
DAY_START_HOUR = _configured_day_start_hour()


def day_start_hour(config=None):
    value = (config or {}).get("day_start_hour", DAY_START_HOUR)
    return value if type(value) is int and 0 <= value <= 23 else DAY_START_HOUR


def _local(moment, timezone):
    value = instant(moment) if isinstance(moment, str) else moment
    if value.tzinfo is None:
        raise ValueError("Timezone-aware task timestamps required")
    return value.astimezone(ZoneInfo(timezone))


def service_day_start(moment, timezone="America/Los_Angeles", hour=None):
    """The local instant at which the service day containing `moment` began."""
    hour = DAY_START_HOUR if hour is None else hour
    local = _local(moment, timezone)
    start = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    if local < start:
        start = (start.replace(tzinfo=None) - dt.timedelta(days=1)).replace(tzinfo=start.tzinfo)
    return start


def next_day_start(moment, timezone="America/Los_Angeles", hour=None):
    """The next service-day start strictly after `moment` (local, timezone-aware): when every
    daily compute budget and cap rolls over."""
    start = service_day_start(moment, timezone, hour)
    return (start.replace(tzinfo=None) + dt.timedelta(days=1)).replace(tzinfo=start.tzinfo)


def service_date(moment, timezone="America/Los_Angeles", hour=None):
    """ISO date on which the current service day STARTED -- the research-day label, identical to
    daily_schedule.due_day when research_hour == day_start_hour. Hour 0: the local date."""
    return service_day_start(moment, timezone, hour).date().isoformat()


def budget_day(moment, timezone="America/Los_Angeles", hour=None):
    """ISO label of the compute-budget day containing `moment`: the local date on which the
    service day ENDS, so [D-1 09:00, D 09:00) is D. For hour 0 this is exactly the local
    calendar date (the old midnight rollover). Ending-date labels keep continuity at the
    2026-09-30 switch: usage already recorded under today's date stays in force until 09:00,
    and at 09:00 a fresh label begins; no historical row is relabelled or erased."""
    hour = DAY_START_HOUR if hour is None else hour
    start = service_day_start(moment, timezone, hour).date()
    return (start + dt.timedelta(days=1 if hour else 0)).isoformat()


def initialize(database):
    database.executescript("""
        CREATE TABLE IF NOT EXISTS workflow_tasks (
            task_id TEXT PRIMARY KEY, worker TEXT NOT NULL, action_id TEXT NOT NULL,
            parent_run_id TEXT NOT NULL, company_id TEXT, role_id TEXT, contact_id TEXT,
            requirement_revision TEXT NOT NULL, inputs TEXT NOT NULL, deadline TEXT NOT NULL,
            status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL,
            result TEXT, evidence TEXT, blocker TEXT, next_action TEXT, owner TEXT NOT NULL,
            effect TEXT NOT NULL, token_hash TEXT, lease_until TEXT, next_at TEXT, updated_at TEXT NOT NULL,
            UNIQUE(worker,action_id));
        CREATE TABLE IF NOT EXISTS workflow_task_events (
            id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, at TEXT NOT NULL,
            event TEXT NOT NULL, detail TEXT NOT NULL);
        -- The dashboard's Failures page (E6.T7) reads tasks by status and time, and each task's
        -- latest events; it is read-only, so the writer creates the indexes it uses.
        CREATE INDEX IF NOT EXISTS workflow_tasks_status_updated ON workflow_tasks(status, updated_at);
        CREATE INDEX IF NOT EXISTS workflow_task_events_task ON workflow_task_events(task_id, id);
        CREATE TABLE IF NOT EXISTS worker_compute_reservations (
            action_id TEXT PRIMARY KEY, day TEXT NOT NULL, seconds INTEGER NOT NULL,
            created_at TEXT NOT NULL);
        -- compute_used/reserve_compute sum by day; the dashboard's Overview reads it on every load.
        CREATE INDEX IF NOT EXISTS worker_compute_reservations_day ON worker_compute_reservations(day);
        -- One row per browser reservation, so its unused tail can be credited back when the
        -- operations it booked have all committed (owner, 2026-09-22). A browser reservation
        -- books `operations * MAX_SECONDS` up front and, unlike every other worker, never
        -- refunded: `refund_compute` existed but only linkedin_contacts and parallel_research
        -- called it (every other worker class was wired up on 2026-09-23 via `settle_compute`).
        -- Raising the session ceiling to 480s therefore doubled what every read
        -- spent whether it needed it or not, halving the day's capacity. Nothing linked a
        -- finished session back to its reservation - `desktop_requests` carries no reservation
        -- - so this table is that link, kept beside the reservation it settles.
        CREATE TABLE IF NOT EXISTS application_browser_reservations (
            reservation TEXT PRIMARY KEY, job_key TEXT NOT NULL, operations INTEGER NOT NULL,
            seconds INTEGER NOT NULL, started_at TEXT NOT NULL, settled_at TEXT);
        CREATE TABLE IF NOT EXISTS worker_compute_resets (
            reset_id TEXT PRIMARY KEY, day TEXT NOT NULL, prior_seconds INTEGER NOT NULL,
            authorized_at TEXT NOT NULL, authorization TEXT NOT NULL);
        CREATE TRIGGER IF NOT EXISTS worker_compute_resets_no_update BEFORE UPDATE ON worker_compute_resets
            BEGIN SELECT RAISE(ABORT, 'Immutable compute reset'); END;
        CREATE TRIGGER IF NOT EXISTS worker_compute_resets_no_delete BEFORE DELETE ON worker_compute_resets
            BEGIN SELECT RAISE(ABORT, 'Immutable compute reset'); END;
        CREATE TABLE IF NOT EXISTS worker_compute_refunds (
            action_id TEXT PRIMARY KEY, day TEXT NOT NULL, seconds INTEGER NOT NULL,
            reserved_seconds INTEGER NOT NULL, elapsed_seconds INTEGER NOT NULL,
            refunded_at TEXT NOT NULL, reason TEXT NOT NULL);
        CREATE TRIGGER IF NOT EXISTS worker_compute_refunds_no_update BEFORE UPDATE ON worker_compute_refunds
            BEGIN SELECT RAISE(ABORT, 'Immutable compute refund'); END;
        CREATE TRIGGER IF NOT EXISTS worker_compute_refunds_no_delete BEFORE DELETE ON worker_compute_refunds
            BEGIN SELECT RAISE(ABORT, 'Immutable compute refund'); END;
        CREATE TABLE IF NOT EXISTS worker_compute_recovery_grants (
            grant_id TEXT PRIMARY KEY, day TEXT NOT NULL UNIQUE, seconds INTEGER NOT NULL,
            job_keys TEXT NOT NULL, authorized_at TEXT NOT NULL,
            expires_at TEXT NOT NULL, authorization TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS worker_compute_recovery_charges (
            action_id TEXT PRIMARY KEY, grant_id TEXT NOT NULL,
            job_key TEXT NOT NULL, seconds INTEGER NOT NULL);
    """)


def reopen_without_deadline(database, task_id, now):
    """Reopen a no-deadline task that its old deadline ended while tries remain (caller commits).

    Its attempts and every recorded failure are kept; it simply becomes claimable again, and
    only max_attempts can end it now.
    """
    row = database.execute("SELECT * FROM workflow_tasks WHERE task_id=?", (task_id,)).fetchone()
    if (not row or row["worker"] not in NO_DEADLINE_WORKERS or row["status"] != "EXHAUSTED"
            or row["attempts"] >= row["max_attempts"]):
        return False
    changed = database.execute("""UPDATE workflow_tasks SET status='QUEUED',blocker=NULL,next_action=NULL,next_at=NULL,
        updated_at=? WHERE task_id=? AND status='EXHAUSTED'""", (now, task_id))
    if changed.rowcount:
        record(database, task_id, "REOPENED_NO_DEADLINE", now, {"attempts": row["attempts"], "max_attempts": row["max_attempts"],
                                                               "previous_blocker": row["blocker"]})
    return bool(changed.rowcount)


def record(database, task_id, kind, now, detail):
    database.execute("INSERT INTO workflow_task_events(task_id,at,event,detail) VALUES (?,?,?,?)",
                     (task_id, now, kind, encoded(detail)))


def enqueue(database, worker, action_id, parent_run_id, inputs, deadline, now,
            company_id=None, role_id=None, contact_id=None, effect="READ", max_attempts=3):
    if not all(isinstance(value, str) and value.strip() for value in (worker, action_id, parent_run_id)):
        raise ValueError("Stable worker/action/parent identifiers required")
    # Ceiling raised 3 -> 7 (owner, 2026-09-23): "reply attempts should not be bounded and
    # exhausted. If it's inconclusive, it should have a Next_at with some growing backoff. Max
    # retries can be 7." Three attempts with a two-step backoff turned every transient failure
    # into a terminal grave: 6 outreach tasks and 47 reply decisions died that way.
    if effect not in {"READ", "WRITE"} or not 1 <= max_attempts <= 7:
        raise ValueError("Invalid effect or retry limit")
    instant(deadline)
    instant(now)
    if not isinstance(inputs, list) or any(not isinstance(item, dict) or not item.get("source") or
                                          not item.get("observed_at") for item in inputs):
        raise ValueError("Every input requires provenance")
    task_id = hashlib.sha256(encoded([worker, action_id]).encode()).hexdigest()
    inserted = database.execute("""INSERT OR IGNORE INTO workflow_tasks
        (task_id,worker,action_id,parent_run_id,company_id,role_id,contact_id,requirement_revision,
         inputs,deadline,status,max_attempts,owner,effect,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,'QUEUED',?,'local-mac',?,?)""",
                     (task_id, worker, action_id, parent_run_id, company_id, role_id, contact_id,
                      REVISION, encoded(inputs), deadline, max_attempts, effect, now))
    existing = database.execute("SELECT * FROM workflow_tasks WHERE task_id=?", (task_id,)).fetchone()
    # provider_wait and compute_wait each refund a try by raising max_attempts by one; count those refunds
    refunds = database.execute("SELECT COUNT(*) FROM workflow_task_events WHERE task_id=? AND event IN (?,?)",
                               (task_id, "PROVIDER_USAGE_LIMIT_WAIT", "COMPUTE_WAIT")).fetchone()[0]
    expected = (encoded(inputs), company_id, role_id, contact_id, effect, deadline, max_attempts + refunds, parent_run_id)
    actual = tuple(existing[field] for field in ("inputs", "company_id", "role_id", "contact_id", "effect", "deadline", "max_attempts", "parent_run_id"))
    if actual != expected:
        raise ValueError("A task identity cannot be rebound to changed inputs")
    if inserted.rowcount:
        record(database, task_id, "QUEUED", now, {"parent_run_id": parent_run_id})
    return task_id


def claim(database, task_id, now, lease_seconds=300):
    current = instant(now)
    # Up to 6 hours: a whole cycle step (applications with 15-minute browser sessions) can run past
    # an hour, and a 1-hour cap left it unable to record its own completion (2026-09-29 DEGRADED).
    if not 1 <= lease_seconds <= 21600:
        raise ValueError("Bounded lease required")
    database.execute("BEGIN IMMEDIATE")
    try:
        row = database.execute("SELECT * FROM workflow_tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            raise ValueError("Unknown task")
        status = row["status"]
        if status == "RUNNING" and instant(row["lease_until"]) <= current:
            status = "UNCERTAIN" if row["effect"] == "WRITE" else "QUEUED"
            database.execute("UPDATE workflow_tasks SET status=?,token_hash=NULL,lease_until=NULL,blocker=?,next_action=?,updated_at=? WHERE task_id=?",
                             (status, "INTERRUPTED_ATTEMPT", "Reconcile before retry" if status == "UNCERTAIN" else "Retry bounded read", now, task_id))
            record(database, task_id, status, now, {"previous_attempt": row["attempts"]})
        if status in TERMINAL or status in {"RUNNING", "UNCERTAIN", "BLOCKED"}:
            database.commit()
            return {"status": status, "task_id": task_id}
        expired = row["worker"] not in NO_DEADLINE_WORKERS and current >= instant(row["deadline"])
        if expired or row["attempts"] >= row["max_attempts"]:
            database.execute("UPDATE workflow_tasks SET status='EXHAUSTED',blocker='DEADLINE_OR_RETRY_LIMIT',updated_at=? WHERE task_id=?", (now, task_id))
            record(database, task_id, "EXHAUSTED", now, {})
            database.commit()
            return {"status": "EXHAUSTED", "task_id": task_id}
        if row["next_at"] and current < instant(row["next_at"]):
            database.commit()
            return {"status": "RETRY_WAIT", "task_id": task_id, "next_at": row["next_at"]}
        token = secrets.token_urlsafe(32)
        expiry = current + dt.timedelta(seconds=lease_seconds)
        if row["worker"] not in NO_DEADLINE_WORKERS:
            expiry = min(expiry, instant(row["deadline"]))
        expiry = expiry.isoformat()
        database.execute("UPDATE workflow_tasks SET status='RUNNING',attempts=attempts+1,token_hash=?,lease_until=?,next_at=NULL,updated_at=? WHERE task_id=?",
                         (hashlib.sha256(token.encode()).hexdigest(), expiry, now, task_id))
        record(database, task_id, "CLAIMED", now, {"attempt": row["attempts"] + 1})
        database.commit()
        import log
        log.enter_task(task_id)   # its lines carry its own [task:<id>] (Failures page run view)
        return {"status": "CLAIMED", "task_id": task_id, "token": token, "lease_until": expiry}
    except BaseException:
        database.rollback()
        raise


def finish(database, task_id, token, now, result, evidence, status="COMPLETE", blocker=None, next_action=None):
    if status not in {"COMPLETE", "BLOCKED", "UNCERTAIN", "RETRY_WAIT", "CANCELLED"}:
        raise ValueError("Invalid task result status")
    if status != "COMPLETE" and (not blocker or not next_action):
        raise ValueError("Unfinished work requires blocker and next action")
    if not isinstance(evidence, list) or any(not item.get("source") or not item.get("observed_at") for item in evidence):
        raise ValueError("Result evidence requires provenance")
    if status == "COMPLETE" and not evidence:
        raise ValueError("Completion requires persisted evidence")
    row = database.execute("SELECT * FROM workflow_tasks WHERE task_id=?", (task_id,)).fetchone()
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    if not row or row["status"] != "RUNNING" or row["token_hash"] != token_hash or instant(row["lease_until"]) <= instant(now):
        raise ValueError("Task claim is stale or not owned")
    if status == "RETRY_WAIT" and row["effect"] == "WRITE":
        status = "UNCERTAIN"
        next_action = "Reconcile external effect before any retry"
    next_at = None
    if status == "RETRY_WAIT":
        if row["attempts"] >= row["max_attempts"]:
            status = "EXHAUSTED"
        else:
            next_at = (instant(now) + dt.timedelta(seconds=RETRY_BACKOFF_SECONDS[
                min(row["attempts"], len(RETRY_BACKOFF_SECONDS)) - 1])).isoformat()
    changed = database.execute("""UPDATE workflow_tasks SET status=?,result=?,evidence=?,blocker=?,next_action=?,
        token_hash=NULL,lease_until=NULL,next_at=?,updated_at=? WHERE task_id=? AND status='RUNNING' AND token_hash=?""",
                               (status, encoded(result), encoded(evidence), blocker, next_action, next_at, now, task_id, token_hash))
    if changed.rowcount != 1:
        raise ValueError("Task changed during completion")
    import log
    log.leave_task(task_id)
    detail = {"result": result, "evidence": evidence}
    if blocker:
        # Kept with the event so a reader can tell a failed try from a wait (the dashboard's Failures
        # page, E6.T7). Readers of workflow_task_events.detail: failures_page._counts, and
        # reporting (REPLY_TOOL_QUEUE_JOB rows only); none reads a RETRY_WAIT/EXHAUSTED detail otherwise.
        detail["blocker"] = blocker
    record(database, task_id, status, now, detail)
    return status


def provider_wait(database, task_id, token, now, wait, result=None):
    """Finish a claimed attempt the model provider refused for usage as a WAIT, not a failure.

    `wait` is a provider_limits.ProviderUsageLimit. The claim already counted an attempt; the
    refusal was not one, so the task's attempt allowance grows by exactly one (attempt
    numbers stay unique for per-attempt identities and the CLAIMED record stays true). The
    task waits until the provider's reset without extending its deadline, which `claim`
    still enforces. Nothing is escalated to the owner.
    """
    import provider_limits
    row = database.execute("SELECT * FROM workflow_tasks WHERE task_id=?", (task_id,)).fetchone()
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    if not row or row["status"] != "RUNNING" or row["token_hash"] != token_hash:
        raise ValueError("Task claim is stale or not owned")
    changed = database.execute("""UPDATE workflow_tasks SET status='RETRY_WAIT',max_attempts=max_attempts+1,result=?,
        blocker=?,next_action=?,token_hash=NULL,lease_until=NULL,next_at=?,updated_at=? WHERE task_id=? AND status='RUNNING'
        AND token_hash=?""", (encoded(result if result is not None else {}), provider_limits.CODE,
                               provider_limits.NEXT_ACTION, wait.next_at, now, task_id, token_hash))
    if changed.rowcount != 1:
        raise ValueError("Task changed during completion")
    import log
    log.leave_task(task_id)
    record(database, task_id, provider_limits.EVENT, now, dict(wait.detail(), attempt=row["attempts"],
           max_attempts_before=row["max_attempts"], max_attempts_after=row["max_attempts"] + 1,
           deadline=row["deadline"], deadline_extended=False))
    return "RETRY_WAIT"


def compute_wait(database, task_id, token, now, next_at, blocker, result=None, next_action=None):
    """Finish a claimed attempt that found the shared daily compute used up as a WAIT, not a try.

    The same shape as `provider_wait` (2026-09-28): the claim already counted an attempt that
    never ran, so the allowance grows by one and the task resumes at `next_at`.
    """
    row = database.execute("SELECT * FROM workflow_tasks WHERE task_id=?", (task_id,)).fetchone()
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    if not row or row["status"] != "RUNNING" or row["token_hash"] != token_hash:
        raise ValueError("Task claim is stale or not owned")
    changed = database.execute("""UPDATE workflow_tasks SET status='RETRY_WAIT',max_attempts=max_attempts+1,result=?,
        blocker=?,next_action=?,token_hash=NULL,lease_until=NULL,next_at=?,updated_at=? WHERE task_id=? AND status='RUNNING'
        AND token_hash=?""", (encoded(result if result is not None else {}), blocker,
                               next_action or "Resume when shared compute resets; this wait did not use a try",
                               next_at, now, task_id, token_hash))
    if changed.rowcount != 1:
        raise ValueError("Task changed during completion")
    import log
    log.leave_task(task_id)
    record(database, task_id, "COMPUTE_WAIT", now, {"attempt": row["attempts"], "max_attempts_after": row["max_attempts"] + 1,
                                                     "resume_at": next_at})
    return "RETRY_WAIT"


def handoff(database, task_id, token, now, children, result, evidence):
    database.execute("SAVEPOINT workflow_handoff")
    try:
        child_ids = [enqueue(database, parent_run_id=task_id, now=now, **child) for child in children]
        finish(database, task_id, token, now, dict(result, child_task_ids=child_ids), evidence)
        database.execute("RELEASE SAVEPOINT workflow_handoff")
        return child_ids
    except BaseException:
        database.execute("ROLLBACK TO SAVEPOINT workflow_handoff")
        database.execute("RELEASE SAVEPOINT workflow_handoff")
        raise


def compute_used(database, day):
    """How much of the day's shared allowance is gone -- the same arithmetic reserve_compute
    admits against, so a preflight that consults this sees exactly what a reservation would.
    Raw reservations remain visible in worker_compute_reservations; nothing here erases usage."""
    spent = database.execute("SELECT COALESCE(SUM(seconds),0) FROM worker_compute_reservations WHERE day=?", (day,)).fetchone()[0]
    if not database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='worker_compute_resets'").fetchone():
        return spent
    baseline = database.execute("SELECT COALESCE(MAX(prior_seconds),0) FROM worker_compute_resets WHERE day=?", (day,)).fetchone()[0]
    refunded = 0
    if database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='worker_compute_refunds'").fetchone():
        refunded = database.execute("SELECT COALESCE(SUM(seconds),0) FROM worker_compute_refunds WHERE day=?", (day,)).fetchone()[0]
    return max(0, spent - baseline - refunded)


def reset_compute(database, reset_id, now, authorization, timezone="America/Los_Angeles"):
    if not isinstance(reset_id, str) or not reset_id.strip() or not isinstance(authorization, str) or not authorization.strip():
        raise ValueError("Explicit owner authorization and reset identity required")
    day = budget_day(now, timezone)
    database.execute("SAVEPOINT authorized_compute_reset")
    try:
        previous = database.execute("SELECT day,authorization FROM worker_compute_resets WHERE reset_id=?", (reset_id,)).fetchone()
        if previous:
            if tuple(previous) != (day, authorization):
                raise ValueError("Compute reset identity cannot change")
            changed = False
        else:
            baseline = database.execute("SELECT COALESCE(SUM(seconds),0) FROM worker_compute_reservations WHERE day=?", (day,)).fetchone()[0]
            database.execute("INSERT INTO worker_compute_resets VALUES (?,?,?,?,?)", (reset_id, day, baseline, now, authorization))
            changed = True
        database.execute("RELEASE SAVEPOINT authorized_compute_reset")
        return changed
    except BaseException:
        database.execute("ROLLBACK TO SAVEPOINT authorized_compute_reset")
        database.execute("RELEASE SAVEPOINT authorized_compute_reset")
        raise


def reserve_compute(database, action_id, now, seconds, timezone="America/Los_Angeles", daily_seconds=10800):
    if not 1 <= seconds <= daily_seconds or daily_seconds > 21000:
        raise ValueError("Compute allocation exceeds the documented 21000-second ceiling (owner, 2026-09-19)")
    day = budget_day(now, timezone)
    database.execute("SAVEPOINT compute_reservation")
    try:
        database.execute("INSERT OR IGNORE INTO worker_compute_reservations SELECT ?,?,?,? WHERE NOT EXISTS (SELECT 1 FROM worker_compute_reservations WHERE action_id=?) AND COALESCE((SELECT SUM(seconds) FROM worker_compute_reservations WHERE day=?),0)-COALESCE((SELECT MAX(prior_seconds) FROM worker_compute_resets WHERE day=?),0)-COALESCE((SELECT SUM(seconds) FROM worker_compute_refunds WHERE day=?),0)+?<=?",
                         (action_id, day, seconds, now, action_id, day, day, day, seconds, daily_seconds))
        row = database.execute("SELECT day,seconds FROM worker_compute_reservations WHERE action_id=?", (action_id,)).fetchone()
        if row and (row["day"] != day or row["seconds"] != seconds):
            raise ValueError("Compute reservation identity cannot change")
        database.execute("RELEASE SAVEPOINT compute_reservation")
    except BaseException:
        database.execute("ROLLBACK TO SAVEPOINT compute_reservation")
        database.execute("RELEASE SAVEPOINT compute_reservation")
        raise
    return row is not None


def settle_compute(database, action_id, started_at, now, reason):
    """Credit a cleanly completed reservation's unused tail. Returns the seconds credited.

    A thin wrapper so every worker settles the same way (owner, 2026-09-23: "Fix 2."). Refund
    was implemented but wired into only 3 of the 14 reservation sites, so eleven worker classes
    booked their full timeout and kept it even when they finished in a fraction of it. Every one
    of them admits against the SAME daily pool, so a 60-second booking that completed in 2
    stranded 58 seconds from every other worker for the rest of the service day.

    Settlement must never fail work that already succeeded, so a missing reservation, an already
    settled one or a post-reset booking credits 0 and says nothing. The CALLER owns the
    transaction. Callers settle a completed phase, or (agent_worker) a booking refused before the
    model ran; the unused tail of a committed failure is refunded too (owner, 2026-09-29).
    """
    try:
        return refund_compute(database, action_id, started_at, now, now, reason)
    except Exception:
        return 0


def refund_compute(database, action_id, started_at, completed_at, now, reason):
    """Credit back the unused tail of a CLEANLY COMPLETED reservation. Returns the seconds credited.

    Reservations are pessimistic: a worker books its timeout up front, so a phase that books
    120 seconds and finishes in 31 strands 89 for the rest of the service day, protecting nothing.
    This turns the booking into a ceiling instead of a spend. Four properties hold:

    1. MEASURED, NEVER DECLARED. `started_at`/`completed_at` are stamped by the reserving
       process around the operation. No worker, prompt or model output can say how much it
       used, and an incoherent window (end before start) is refused outright.
    2. BOUNDED BY THE ORIGINAL RESERVATION. The credit is `max(0, min(reserved, reserved -
       elapsed))`, so it can never exceed what was booked and never goes negative. Effective
       daily usage stays at or above zero, so the 10,800-second ceiling in reserve_compute
       remains unbreachable through this path.
    3. APPEND, NEVER MUTATE. reserve_compute enforces "Compute reservation identity cannot
       change" and the reservation row is audit evidence of the original charge. The credit
       is its own immutable row carrying the reservation it belongs to, what was booked, what
       was measured and why, and admission subtracts it -- the same shape as
       worker_compute_resets.prior_seconds. The refund is keyed by action_id, so it is
       idempotent: a replay credits nothing further.
    4. CLEAN COMPLETION, WITH TWO MEASURED EXCEPTIONS. The caller decides. A clean completion
       refunds its tail; so does a committed browser session's failure (owner, 2026-09-29) and a
       contact-finder run that started and returned a failure (owner, 2026-10-22, CM1 b), and a
       failed follow-up writer attempt (owner, 2026-10-27, D4 a). Every
       other failure keeps its whole charge (AGENTS.md: "other workers' failed attempts keep
       their charge"), as does an interrupted, killed or uncertain outcome.

    The credit belongs to the day the reservation was charged to, not the day the refund is
    recorded, so a phase that ends just after the day start still credits the day it spent.

    A reservation already folded into an authorized reset baseline (worker_compute_resets
    subtracts the whole sum reserved at reset time) is NOT refundable: subtracting it twice
    would manufacture allowance. Such a request credits nothing and says so.
    """
    if not isinstance(action_id, str) or not action_id.strip() or not isinstance(reason, str) or not reason.strip():
        raise ValueError("A refund needs the reservation it belongs to and a recorded reason")
    started, completed = instant(started_at), instant(completed_at)
    if completed < started:
        raise ValueError("Compute refunds require a measured window, not a reported duration")
    row = database.execute("SELECT day,seconds,created_at FROM worker_compute_reservations WHERE action_id=?", (action_id,)).fetchone()
    if not row:
        raise ValueError("No compute reservation to refund")
    database.execute("SAVEPOINT compute_refund")
    try:
        existing = database.execute("SELECT seconds FROM worker_compute_refunds WHERE action_id=?", (action_id,)).fetchone()
        if existing:
            database.execute("RELEASE SAVEPOINT compute_refund")
            return existing["seconds"]
        reset = database.execute("SELECT MAX(authorized_at) FROM worker_compute_resets WHERE day=?", (row["day"],)).fetchone()[0]
        if reset and instant(reset) >= instant(row["created_at"]):
            database.execute("RELEASE SAVEPOINT compute_refund")
            return 0
        elapsed = math.ceil((completed - started).total_seconds())
        credited = max(0, min(row["seconds"], row["seconds"] - elapsed))
        if credited:
            database.execute("INSERT INTO worker_compute_refunds VALUES (?,?,?,?,?,?,?)",
                             (action_id, row["day"], credited, row["seconds"], elapsed, now, reason))
        database.execute("RELEASE SAVEPOINT compute_refund")
        return credited
    except BaseException:
        database.execute("ROLLBACK TO SAVEPOINT compute_refund")
        database.execute("RELEASE SAVEPOINT compute_refund")
        raise


def grant_application_recovery(database, grant_id, job_keys, seconds, now, expires_at,
                               authorization, timezone="America/Los_Angeles"):
    if (not isinstance(grant_id, str) or not grant_id.strip()
            or type(seconds) is not int or not 1 <= seconds <= 21000
            or not isinstance(authorization, str) or not authorization.strip()
            or not isinstance(job_keys, list) or not job_keys
            or any(not isinstance(key, str) or not key for key in job_keys)):
        raise ValueError("Explicit bounded recovery authorization and job scope required")
    started, expiry = instant(now), instant(expires_at)
    if not dt.timedelta(0) < expiry - started <= dt.timedelta(days=1):
        raise ValueError("Recovery grant must expire within one day")
    scope = sorted(set(job_keys))
    for key in scope:
        if not database.execute("SELECT 1 FROM application_jobs WHERE job_key=?", (key,)).fetchone():
            raise ValueError("Recovery scope must reference existing application work")
    day = budget_day(started, timezone)
    values = (day, seconds, encoded(scope), started.isoformat(), expiry.isoformat(), authorization)
    prior = database.execute("SELECT day,seconds,job_keys,authorized_at,expires_at,authorization FROM worker_compute_recovery_grants WHERE grant_id=?", (grant_id,)).fetchone()
    if prior:
        if tuple(prior) != values:
            raise ValueError("Recovery grant cannot be overwritten or replenished")
        return False
    if database.execute("SELECT 1 FROM worker_compute_recovery_grants WHERE day=?", (day,)).fetchone():
        raise ValueError("One explicit application recovery grant per budget day")
    database.execute("INSERT INTO worker_compute_recovery_grants VALUES (?,?,?,?,?,?,?)", (grant_id, *values))
    return True


def _available_recovery_grant(database, job_key, now, seconds):
    if not database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='worker_compute_recovery_grants'").fetchone():
        return None
    current = instant(now)
    for row in database.execute("""SELECT grant_id,seconds,job_keys,authorized_at,expires_at
            FROM worker_compute_recovery_grants ORDER BY authorized_at,grant_id""").fetchall():
        grant_id, allowance, scope, authorized_at, expires_at = row
        if (not instant(authorized_at) <= current < instant(expires_at)
                or job_key not in json.loads(scope)):
            continue
        spent = database.execute("SELECT COALESCE(SUM(seconds),0) FROM worker_compute_recovery_charges WHERE grant_id=?", (grant_id,)).fetchone()[0]
        if spent + seconds <= allowance:
            return grant_id
    return None


def application_recovery_available(database, job_key, now, seconds=240):
    if type(seconds) is not int or seconds <= 0:
        raise ValueError("Positive recovery reservation required")
    return _available_recovery_grant(database, job_key, now, seconds) is not None


def track_browser_reservation(database, reservation, job_key, operations, seconds, now):
    """Remember what a browser reservation booked, so `settle_browser_reservations` can
    credit back whatever it did not use. Idempotent; never raises into the caller's path."""
    try:
        database.execute("INSERT OR IGNORE INTO application_browser_reservations VALUES (?,?,?,?,?,NULL)",
                         (reservation, job_key, int(operations), int(seconds), now))
    except Exception:
        pass


def settle_browser_reservations(database, job_key, now):
    """Credit back the unused tail of this job's finished browser reservations.

    Owner, 2026-09-22. A browser reservation books `operations * MAX_SECONDS` before the work
    starts, and that booking was previously never revisited - so once the session ceiling went
    to 480s, a read that finished in 118s still spent 480 of the day's 21,000. `refund_compute`
    has existed for exactly this ("a phase that books 120 seconds and finishes in 31 strands 89
    for the rest of the service day") and only two other workers called it.

    A reservation settles on what actually ran (rules below the docstring): a request that was
    never claimed ran nothing and is refunded in full; a started session must have COMMITTED a
    result (`RESULT_COMMITTED` or `BROWSER_CONSUMER_COMPLETED`, recorded after `desktop_adapter.complete`
    succeeds) before its tail is credited - a committed failure, a timeout included, counts (owner,
    2026-09-29) - so only a session that was killed or never returned keeps its whole charge. The credit is measured
    from the reservation's own timestamp to the last commit, never declared by any worker, and
    `refund_compute` clamps it to the booking and refuses to go negative.

    Contractually non-raising: a failure to credit compute back must never fail an application.
    """
    credited = 0
    try:
        rows = list(database.execute(
            """SELECT reservation, operations, started_at FROM application_browser_reservations
               WHERE job_key=? AND settled_at IS NULL""", (job_key,)))
    except Exception:
        return 0
    # Owner, 2026-09-28: a reservation now settles on what actually happened, not on whether
    # every operation it booked ran. A booking covers every step the job might need (a read and
    # a submit), but a job often needs only the read, and a queued request may never be picked
    # up at all; both used to keep their whole charge forever, which charged ~13,000 of
    # 2026-09-28's 21,000 seconds to browser work that never ran. The requests a reservation
    # covers are this job's requests created from its start until the job's next reservation.
    #   - none started (never claimed, and expired/superseded/cancelled): refund it all;
    #   - every started session committed and nothing is still queued: refund the unused tail;
    #   - a started session never committed anything (killed, lost, uncertain): keep the whole
    #     charge; a committed failure (a timeout included) settles like any commit (2026-09-29);
    #   - a request still queued and unexpired: wait, it may yet run.
    for row in rows:
        try:
            following = database.execute(
                """SELECT MIN(started_at) FROM application_browser_reservations
                   WHERE job_key=? AND started_at > ?""", (job_key, row["started_at"])).fetchone()[0]
            requests = list(database.execute(
                """SELECT request_id, state, claims, expires_at FROM desktop_requests
                   WHERE job_key=? AND created_at >= ? AND (? IS NULL OR created_at < ?)""",
                (job_key, row["started_at"], following, following)))
            # CONSUMED with no claim is a read no session picked up that was replaced (2026-09-28).
            never_started = [item for item in requests if not item["claims"] and (
                item["state"] in {"EXPIRED", "SUPERSEDED", "CANCELLED", "CONSUMED"}
                or item["state"] == "PENDING" and instant(item["expires_at"]) <= instant(now))]
            waiting = [item for item in requests if not item["claims"] and item["state"] == "PENDING"
                       and instant(item["expires_at"]) > instant(now)]
            started = [item for item in requests if item not in never_started and item not in waiting]
            # A booking is used only by the cycle that made it, and that cycle issues its requests
            # within minutes; a booking older than 30 minutes can take on no further work. Until
            # then a partly used booking waits (its next step, e.g. the submit after a read, may
            # still come); a fully used one settles at once, as before.
            cycle_over = instant(now) - instant(row["started_at"]) >= dt.timedelta(minutes=30)
            if waiting:
                continue
            if not started:
                if not cycle_over:
                    continue
                credited += refund_compute(database, row["reservation"], row["started_at"], row["started_at"],
                                           now, "BROWSER_RESERVATION_NEVER_STARTED") or 0
            else:
                # A session counts as committed once its result is stored, a failure included (owner,
                # 2026-09-29: the unused tail of a failed session "should be refunded"). Only a
                # session that never committed anything (killed, or timed out without committing, or lost) keeps its whole
                # charge. One commit per request: the latest of either marker.
                commits = sorted(event["at"] for event in database.execute(
                    """SELECT MAX(event.at) AS at FROM desktop_events AS event
                       WHERE event.event IN ('BROWSER_CONSUMER_COMPLETED','RESULT_COMMITTED')
                       AND event.request_id IN (%s) GROUP BY event.request_id""" % ",".join("?" * len(started)),
                    [item["request_id"] for item in started]))
                if len(commits) < len(started) or len(commits) < row["operations"] and not cycle_over:
                    continue
                credited += refund_compute(database, row["reservation"], row["started_at"], commits[-1], now,
                                           "BROWSER_SESSION_COMMITTED") or 0
            database.execute("UPDATE application_browser_reservations SET settled_at=? WHERE reservation=?",
                             (now, row["reservation"]))
        except Exception:
            continue
    return credited


def reserve_application_compute(database, action_id, job_key, now, seconds,
                                timezone="America/Los_Angeles", daily_seconds=10800):
    if not action_id.startswith("application-browser:"):
        raise ValueError("Recovery allocation is restricted to the application browser")
    database.execute("SAVEPOINT application_compute")
    try:
        reserved = reserve_compute(database, action_id, now, seconds, timezone, daily_seconds)
        previous = database.execute("SELECT job_key,seconds FROM worker_compute_recovery_charges WHERE action_id=?", (action_id,)).fetchone()
        if previous and tuple(previous) != (job_key, seconds):
            raise ValueError("Recovery reservation identity cannot change")
        if not reserved:
            grant_id = _available_recovery_grant(database, job_key, now, seconds)
            if grant_id:
                day = budget_day(now, timezone)
                database.execute("INSERT INTO worker_compute_reservations VALUES (?,?,?,?)", (action_id, day, seconds, now))
                database.execute("INSERT INTO worker_compute_recovery_charges VALUES (?,?,?,?)", (action_id, grant_id, job_key, seconds))
                reserved = True
        database.execute("RELEASE SAVEPOINT application_compute")
        return reserved
    except BaseException:
        database.execute("ROLLBACK TO SAVEPOINT application_compute")
        database.execute("RELEASE SAVEPOINT application_compute")
        raise
