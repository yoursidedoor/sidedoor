"""The two pause flags (E5.T1 shared precondition; E6.T0 sections 5 and 11): one `scheduler_control`
row in the execution database holds `user_paused_at` (the dashboard's Pause) and
`license_paused_at` (E8.T3's license pause), each with its reason. Work starts only while neither
is set (C10).

Who reads it: the scheduler loop (`scheduler_loop`) before every child it would start, and the
applications job's `should_stop` (`orchestrator.applications_service`), so a pause stops that job
between jobs, never inside one. Nothing here signals a running process.

Who writes it: the dashboard (`set_user_pause`, on its 10 s write connection) and E8.T3's license
sync (`sync_license_pause`, from the license heartbeat, the scheduler loop and the dashboard's
license actions; `scheduler_loop.sync_license_pause`). Writers create the table; readers treat a missing table or row as "not
paused", as on a fresh install.
"""

import datetime as dt
import sqlite3


SCHEMA = """CREATE TABLE IF NOT EXISTS scheduler_control(
    id INTEGER PRIMARY KEY CHECK (id = 1),
    user_paused_at TEXT,
    user_pause_reason TEXT,
    license_paused_at TEXT,
    license_pause_reason TEXT,
    updated_at TEXT)"""


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def initialize(database):
    database.execute(SCHEMA)
    database.execute("INSERT OR IGNORE INTO scheduler_control(id) VALUES (1)")


def state(database):
    """{"user_paused_at", "user_pause_reason", "license_paused_at", "license_pause_reason"}; all
    None when the table or the row does not exist yet. Other database errors are raised."""
    try:
        row = database.execute("SELECT user_paused_at,user_pause_reason,license_paused_at,license_pause_reason "
                               "FROM scheduler_control WHERE id=1").fetchone()
    except sqlite3.OperationalError as error:
        if "no such table" not in str(error):
            raise
        row = None
    keys = ("user_paused_at", "user_pause_reason", "license_paused_at", "license_pause_reason")
    return dict(zip(keys, tuple(row) if row else (None,) * 4))


def paused(database):
    """True while the user's pause or the license pause is set."""
    value = state(database)
    return bool(value["user_paused_at"] or value["license_paused_at"])


def _set(database, column, reason_column, on, reason, now):
    with database:
        initialize(database)
        if on:
            # A pause already set keeps its original time.
            database.execute(f"UPDATE scheduler_control SET {column}=COALESCE({column}, ?), {reason_column}=?, "
                             "updated_at=? WHERE id=1", (now or _now(), reason, now or _now()))
        else:
            database.execute(f"UPDATE scheduler_control SET {column}=NULL, {reason_column}=NULL, updated_at=? "
                             "WHERE id=1", (now or _now(),))
    return state(database)


def set_user_pause(database, on, reason=None, now=None):
    """The dashboard's Pause (on=True) and Resume (on=False)."""
    return _set(database, "user_paused_at", "user_pause_reason", on, reason, now)


def set_license_pause(database, on, reason=None, now=None):
    """The license pause (E8.T3): pauses before the next job, like the user's pause."""
    return _set(database, "license_paused_at", "license_pause_reason", on, reason, now)


def sync_license_pause(database, reason, usable, now=None):
    """E8.T3 (E6.T0 sections 5 and 11): set the license pause while `reason` names one ("expired",
    "grace_spent", "machine": `license_clock.pause_reason`), clear it once the license is usable
    again (a renewal, a new key, or a successful heartbeat), and otherwise leave it as it is. The
    user's pause is never touched. Writes only on a change. Returns the flags."""
    value = state(database)
    if reason:
        if value["license_paused_at"] and value["license_pause_reason"] == reason:
            return value
        return set_license_pause(database, True, reason, now)
    if usable and value["license_paused_at"]:
        return set_license_pause(database, False, now=now)
    return value
