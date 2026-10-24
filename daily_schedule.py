import datetime as dt
import json
import uuid
from zoneinfo import ZoneInfo


COMPUTE_WAIT_HORIZON = dt.timedelta(days=1)
DAY_COLUMNS = "day,state,attempts,attempt_id,next_retry_at,completed_at,compute_wait_attempts"


def initialize(database, timezone):
    database.executescript("""
        CREATE TABLE IF NOT EXISTS research_days (
            day TEXT PRIMARY KEY, state TEXT NOT NULL, attempts INTEGER NOT NULL,
            attempt_id TEXT, next_retry_at TEXT, completed_at TEXT,
            compute_wait_attempts INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS schedule_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)
    # Additive migration only: existing rows keep their state, attempts, retry time and
    # completion exactly as recorded and simply gain a zero wait counter. No historical day
    # is reopened, re-attempted or rewritten.
    if not any(row[1] == "compute_wait_attempts"
               for row in database.execute("PRAGMA table_info(research_days)").fetchall()):
        with database:
            database.execute("ALTER TABLE research_days ADD COLUMN compute_wait_attempts INTEGER NOT NULL DEFAULT 0")
    with database:
        if not database.execute("SELECT 1 FROM schedule_meta WHERE key='daily_migration'").fetchone():
            for row in database.execute("SELECT id,started_at FROM research WHERE state='COMPLETE'").fetchall():
                day = dt.datetime.fromisoformat(row["started_at"]).astimezone(ZoneInfo(timezone)).date().isoformat()
                database.execute("INSERT OR IGNORE INTO research_days(" + DAY_COLUMNS + ") VALUES (?,'COMPLETE',1,?,NULL,?,0)",
                                 (day, row["id"], row["started_at"]))
            database.execute("INSERT INTO schedule_meta VALUES ('daily_migration','1')")


def check_hours(config):
    # The research day and the budget day must open together (owner, 2026-09-30: the service
    # day starts at 9:00): research_hour and day_start_hour are two names for one hour.
    if "day_start_hour" in config and config["day_start_hour"] != config["research_hour"]:
        raise ValueError("CONFIG_RESEARCH_HOUR_MUST_EQUAL_DAY_START_HOUR")


def due_day(now, config):
    check_hours(config)
    local = now.astimezone(ZoneInfo(config["timezone"]))
    scheduled = local.replace(hour=config["research_hour"], minute=0, second=0, microsecond=0)
    if local < scheduled:
        scheduled -= dt.timedelta(days=1)
    return scheduled.date().isoformat()


def next_daily_time(now, config):
    local = now.astimezone(ZoneInfo(config["timezone"]))
    scheduled = local.replace(hour=config["research_hour"], minute=0, second=0, microsecond=0)
    if local >= scheduled:
        scheduled += dt.timedelta(days=1)
    return scheduled.isoformat()


CODEX_WAIT = "CODEX_WAIT"


def codex_wait(database, config, now):
    """No usable Codex at research time (2026-10-23): the day's row is written as CODEX_WAIT with no
    attempt, so the hourly cycle and the applications job count research as having had its turn
    (`orchestrator.research_turn_pending`) instead of yielding all day. `claim` treats it like a day
    with no attempt yet: research starts the same day once Codex is found, spending no attempt."""
    day = due_day(now, config)
    with database:
        database.execute("INSERT OR IGNORE INTO research_days(" + DAY_COLUMNS + ") VALUES (?,?,0,NULL,NULL,NULL,0)",
                         (day, CODEX_WAIT))
    return {"state": "CODEX_NOT_FOUND", "day": day, "next_action": "Find Codex again in Setup or Settings"}


def claim(database, config, now):
    day = due_day(now, config)
    database.execute("BEGIN IMMEDIATE")
    try:
        row = database.execute("SELECT * FROM research_days WHERE day=?", (day,)).fetchone()
        authorization_key = "forced_research_retry:" + day
        saved_authorization = database.execute("SELECT value FROM schedule_meta WHERE key=?", (authorization_key,)).fetchone()
        authorization = json.loads(saved_authorization[0]) if saved_authorization else {}
        forced_retry = authorization.get("state") == "AUTHORIZED" and bool(authorization.get("authorization"))
        waits = row["compute_wait_attempts"] if row else 0
        if row:
            if row["state"] in {"COMPLETE", "RUNNING"} or row["state"] == "EXHAUSTED" and not forced_retry:
                database.commit()
                return {"state": row["state"], "day": day}
            # Recorded compute waits are not failed investigations, so they do not spend the
            # investigation attempt budget. `waits` is itself capped in finish(), so the total
            # number of claims per day stays bounded.
            if row["attempts"] - waits >= config["research_max_attempts"] and not forced_retry:
                database.commit()
                return {"state": "EXHAUSTED", "day": day}
            if row["next_retry_at"] and now < dt.datetime.fromisoformat(row["next_retry_at"]) and not forced_retry:
                database.commit()
                return {"state": "RETRY_WAIT", "day": day, "next_retry_at": row["next_retry_at"]}
        attempt = row["attempts"] + 1 if row else 1
        attempt_id = str(uuid.uuid4())
        if forced_retry:
            authorization.update(state="CONSUMED", attempt_id=attempt_id, consumed_at=now.isoformat())
            database.execute("UPDATE schedule_meta SET value=? WHERE key=?", (json.dumps(authorization), authorization_key))
        database.execute("INSERT INTO research_days(" + DAY_COLUMNS + ") VALUES (?,'RUNNING',?,?,NULL,NULL,?) ON CONFLICT(day) DO UPDATE SET state='RUNNING',attempts=excluded.attempts,attempt_id=excluded.attempt_id,next_retry_at=NULL",
                         (day, attempt, attempt_id, waits))
        database.execute("INSERT INTO research VALUES (?,?,'RUNNING',NULL)", (attempt_id, now.isoformat()))
        database.commit()
        return {"state": "CLAIMED", "day": day, "attempt": attempt, "id": attempt_id}
    except BaseException:
        database.rollback()
        raise


def compute_wait_retry(result, now):
    """The validated next-retry instant of a PURE compute wait, else None.

    AGENTS.md: "Compute waits are not failed investigations." The run reports the next shared
    allowance reset; this only accepts a timezone-aware instant that is in the future and
    expires within one day, so the allowance it waits for cannot be deferred indefinitely.
    """
    if not result.get("compute_wait") or result.get("retryable") is not True or result["state"] == "COMPLETE":
        return None
    value = result.get("next_retry_at")
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None or not now < moment <= now + COMPUTE_WAIT_HORIZON:
        return None
    return moment.isoformat()


LONG_WAIT_KINDS = (None, "openrouter_out_of_credit", "codex_not_found")   # the ChatGPT plan limit has kind None


def provider_wait(result):
    """True when every industry still waiting waits on the provider for something only the user or the
    clock ends: the ChatGPT usage limit, OpenRouter out of credit, or no usable Codex (2026-10-23).
    Those never count toward the day's tries, past the cap below too. The cap on ordinary compute waits
    stays: it bounds how many times one day's research is claimed (each claim starts a run that plans
    and reserves before it finds the shared budget empty). A provider wait is bounded anyway: its retry
    is at most COMPUTE_WAIT_HORIZON away (`compute_wait_retry`), and the day itself ends."""
    import provider_limits
    industries = [industry for industry in result.get("industries") or [] if isinstance(industry, dict)
                  and industry.get("state") == "COMPUTE_WAIT"]
    if not industries:
        return False
    for industry in industries:
        wait = industry.get("compute_wait") if isinstance(industry.get("compute_wait"), dict) else {}
        provider = wait.get("provider") if isinstance(wait.get("provider"), dict) else None
        if wait.get("reason") != provider_limits.CODE or provider is None or provider.get("kind") not in LONG_WAIT_KINDS:
            return False
    return True


def long_wait_retry(result, now, config):
    """A provider wait (`provider_wait`) whose reset is past COMPUTE_WAIT_HORIZON (a ChatGPT limit can
    state one up to provider_limits.MAX_WAIT away): the day waits until that reset, or until the next
    service day starts if that is sooner (the next day then waits likewise), spending no tries."""
    value = result.get("next_retry_at")
    try:
        moment = dt.datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        moment = None
    if moment is None or moment.tzinfo is None or moment <= now:
        return None
    return min(moment, dt.datetime.fromisoformat(next_daily_time(now, config))).isoformat()


def finish(database, config, attempt_id, result, now):
    row = database.execute("SELECT * FROM research_days WHERE attempt_id=? AND state='RUNNING'", (attempt_id,)).fetchone()
    if not row:
        raise ValueError("Daily attempt is not owned by this worker")
    success = result["state"] == "COMPLETE"
    waits = row["compute_wait_attempts"]
    waiting = None if success else compute_wait_retry(result, now)
    if not success and waiting is None and provider_wait(result):
        waiting = long_wait_retry(result, now, config)
    retry_at = None
    if success:
        state = "COMPLETE"
    elif waiting and (waits < config["research_max_attempts"] or provider_wait(result)):
        # A wait is recorded and bounded, never erased: the attempt row survives with its real
        # count, and `compute_wait_attempts` says how many of those attempts were waits rather
        # than investigations. No compute limit is raised and no usage is discarded.
        state, retry_at, waits = "RETRY_WAIT", waiting, waits + 1
    elif (row["attempts"] - waits >= config["research_max_attempts"]
          or result.get("retryable") is False and result.get("stop_reason") == "ALL_UNRESOLVED_INDUSTRIES_ABANDONED"):
        state = "EXHAUSTED"
    else:
        state = "RETRY_WAIT"
        delays = config["research_retry_delays_seconds"]
        delay = delays[min(max(row["attempts"] - waits, 1), len(delays)) - 1]
        retry_at = (now + dt.timedelta(seconds=delay)).isoformat()
    with database:
        database.execute("UPDATE research SET state=?,result=? WHERE id=?", (result["state"], json.dumps(result), attempt_id))
        database.execute("UPDATE research_days SET state=?,next_retry_at=?,completed_at=?,compute_wait_attempts=? WHERE attempt_id=?",
                         (state, retry_at, now.isoformat() if success else None, waits, attempt_id))
    return {"day": row["day"], "state": state, "attempts": row["attempts"],
            "compute_wait_attempts": waits, "investigation_attempts": row["attempts"] - waits,
            "next_retry_at": retry_at}


def recover_interrupted(database, config, now):
    for row in database.execute("SELECT attempt_id FROM research_days WHERE state='RUNNING'").fetchall():
        finish(database, config, row["attempt_id"], {"state": "INTERRUPTED", "error": "Previous worker exited before recording success"}, now)
