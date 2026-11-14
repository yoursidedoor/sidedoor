"""A sign-in or a human check met while Sidedoor confirms an application it may already have sent
(E16, owner's pick S7 a, 2026-11-13).

The job is uncertain (`applications.uncertain_submission`): Submit was pressed and the outcome is not
known. Its verify or reconcile READ met a wall. The hard line: nothing here may lead to a new
application session, a re-arm or a second submit. So this is NOT an access wait
(`access_outcomes`): the job STAYS in RECONCILE, held on the wall's code, and

* `record` writes one row in this module's own table (never `application_access_waits`, so no rule of
  `access_outcomes` reads it: no release, close, expiry, site hold or Try again) and answers the time
  the job's next read is due: the wait's deadline, `access_outcomes.WAIT_SECONDS` ahead;
* the card (`access_cards.confirm_cards`) offers the user's step on the job's own stored address;
* `done` (the user finished the step) ends the row and makes the job's next READ due now. It changes
  `next_at` and nothing else: not the state, not the blocker, not the attempts, no request. The next
  round's `applications.process` routes the job to `reconcile` exactly as it would at the deadline.

Reads: every blocked read counts toward `applications.RECONCILE_MAX_ATTEMPTS`, as before, so the
count still bounds the reads; nothing is read or counted while the job waits. A confirming read
always really opens the site (`site_sessions.ALWAYS_OPEN`), so a wait is recorded only when that read
itself met the wall. If the user never acts, the deadline passes, the
reconcile path carries on with its next read by itself, and when the reads run out it holds the job as
it always did (`applications.RECONCILE_SPENT_EVENT`).

The card's key is `key(job_key)`, never a bare job key: the buttons of an application card (Abandon,
"I've signed in", Try again) cannot be addressed to it.
"""
import datetime as dt
import json

TABLE = "application_confirm_waits"
WAITING, ENDED = "WAITING", "ENDED"
KEY = "confirm-read:"
RECORDED_EVENT = "CONFIRM_READ_WAIT_RECORDED"
DUE_EVENT = "CONFIRM_READ_DUE"
STEP_DONE, READ_AGAIN = "step_done", "read_again"
# The session stop codes a confirming read can wait on, each with the user's step that settles it.
WALLS = {"AUTHENTICATION_REQUIRED": "SIGN_IN", "MFA_REQUIRED": "SIGN_IN", "CAPTCHA_REQUIRED": "HUMAN_CHECK"}
# What the job's hold says: the card's own sentence (owner's wording, 2026-11-13), less its full stop.
ACTION = {"SIGN_IN": "The site needs you to sign in. Do not apply again",
          "HUMAN_CHECK": "The site needs a human check. Do not apply again"}


def key(job_key):
    return KEY + job_key


def job_of(card_key):
    """The job key a card key names, or None when it is not this module's card key."""
    if isinstance(card_key, str) and card_key.startswith(KEY) and len(card_key) > len(KEY):
        return card_key[len(KEY):]
    return None


def present(database):
    return bool(database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone())


def _ensure(database):
    database.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE} (
        id INTEGER PRIMARY KEY, job_key TEXT NOT NULL, host TEXT, wall TEXT NOT NULL, since TEXT NOT NULL,
        deadline TEXT NOT NULL, state TEXT NOT NULL, ended_at TEXT, outcome TEXT, updated_at TEXT NOT NULL)""")
    database.execute(f"CREATE INDEX IF NOT EXISTS application_confirm_waits_job ON {TABLE}(job_key, state)")


def action(code):
    return ACTION[WALLS.get(code, "SIGN_IN")]


def record(database, job, code, now):
    """A confirming read stopped on `code`: one waiting row, and the deadline the job's next read is
    due at (the caller holds the job in RECONCILE with it as next_at). Call inside a transaction."""
    import access_outcomes
    _ensure(database)
    end(database, job["job_key"], now, READ_AGAIN)   # a row a read at the deadline left open
    deadline = (dt.datetime.fromisoformat(now) + dt.timedelta(seconds=access_outcomes.WAIT_SECONDS)).isoformat()
    database.execute(f"INSERT INTO {TABLE}(job_key,host,wall,since,deadline,state,updated_at) VALUES (?,?,?,?,?,?,?)",
                     (job["job_key"], access_outcomes.host_of(job), code, now, deadline, WAITING, now))
    return deadline


def end(database, job_key, now, outcome):
    """Every open row of the job ends (the job read again, or finished). Returns how many ended."""
    if not present(database):
        return 0
    return database.execute(f"UPDATE {TABLE} SET state=?,ended_at=?,outcome=?,updated_at=? WHERE job_key=? AND state=?",
                            (ENDED, now, outcome, now, job_key, WAITING)).rowcount


def wait(database, job_key):
    """The job's open wait as a card needs it, or None. Open means: a waiting row, and the job is still
    exactly as `record`'s caller left it (RECONCILE, held on the row's wall, due at the row's deadline).
    Anything else (the read ran again, the job finished, the user finished the step) is no card."""
    if not job_key or not present(database):
        return None
    row = database.execute(
        f"""SELECT wait.*, job.employer, job.payload FROM {TABLE} AS wait JOIN application_jobs AS job
            ON job.job_key=wait.job_key WHERE wait.job_key=? AND wait.state=? AND job.state='RECONCILE'
            AND job.blocker=wait.wall AND job.next_at=wait.deadline ORDER BY wait.id DESC LIMIT 1""",
        (job_key, WAITING)).fetchone()
    return _card(row) if row else None


def waiting(database):
    """Every open wait (`wait`'s rule), oldest first, one per job."""
    if database is None or not present(database):
        return []
    try:
        rows = database.execute(
            f"""SELECT wait.*, job.employer, job.payload FROM {TABLE} AS wait JOIN application_jobs AS job
                ON job.job_key=wait.job_key WHERE wait.state=? AND job.state='RECONCILE'
                AND job.blocker=wait.wall AND job.next_at=wait.deadline ORDER BY wait.id""", (WAITING,)).fetchall()
    except Exception:
        return []
    found = {}
    for row in rows:
        found[row["job_key"]] = _card(row)
    return list(found.values())


def _card(row):
    try:
        payload = json.loads(row["payload"]) or {}
    except (TypeError, ValueError):
        payload = {}
    payload = payload if isinstance(payload, dict) else {}
    return {"id": row["id"], "job_key": row["job_key"], "employer": payload.get("company") or row["employer"],
            "role": payload.get("role") or payload.get("title"), "url": payload.get("url"), "host": row["host"],
            "wall": row["wall"], "asks": WALLS.get(row["wall"], "SIGN_IN"), "since": row["since"],
            "deadline": row["deadline"]}


def done(database, job_key, now, wait_id=None):
    """The user finished the step: the row ends and the job's next READ is due now. Only `next_at`
    changes on the job, and only while it is still RECONCILE on that wall and due at that deadline.
    `wait_id`: the wait the step was started for; a late Done never touches a newer one. Call inside a
    transaction. Returns True when this call made the read due."""
    import applications
    if not database.in_transaction:
        database.execute("BEGIN IMMEDIATE")
    current = wait(database, job_key)
    if current is None or (wait_id is not None and current["id"] != wait_id):
        return False
    due = database.execute("UPDATE application_jobs SET next_at=? WHERE job_key=? AND state='RECONCILE' "
                           "AND blocker=? AND next_at=?", (now, job_key, current["wall"], current["deadline"]))
    if due.rowcount != 1:
        return False   # the job moved on: nothing is ended and nothing is recorded
    database.execute(f"UPDATE {TABLE} SET state=?,ended_at=?,outcome=?,updated_at=? WHERE id=? AND state=?",
                     (ENDED, now, STEP_DONE, now, current["id"], WAITING))
    applications.event(database, job_key, DUE_EVENT, {"wall": current["wall"], "since": current["since"]}, now)
    return True
