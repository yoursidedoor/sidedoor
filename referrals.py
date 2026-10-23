"""Referrals, tracked per person and independently of job applications.

Owner, 2026-09-25: "referrals arent supposed to be coupled with job applications. these are
two different things that should evolve independently ... Referral should be tracked
independetly, same as job applications." Referrals used to be recorded against a job
(`conversion_referrals` keyed by job_key and contact, `application_referrals` keyed by employer
and requisition) and could only be written when a reply was tied to exactly one researched job.
Neither table ever held a row. A referral is now one record per person: where it stands, the
message it came from, what they said and the next step. It never names or waits on a job.
"""

import json


def initialize(database):
    database.execute("""CREATE TABLE IF NOT EXISTS referrals (
        contact_id TEXT PRIMARY KEY, state TEXT NOT NULL, source_key TEXT NOT NULL,
        statement TEXT NOT NULL, next_action TEXT, review_at TEXT, updated_at TEXT NOT NULL,
        history TEXT NOT NULL)""")


def current(database, contact_id):
    initialize(database)
    row = database.execute("SELECT * FROM referrals WHERE contact_id=?", (contact_id,)).fetchone()
    if row is None:
        return None
    columns = ("contact_id", "state", "source_key", "statement", "next_action", "review_at", "updated_at", "history")
    record = dict(zip(columns, row))
    record["history"] = json.loads(record["history"])
    return record


def record(database, contact_id, state, source_key, statement, next_action, review_at, now, evidence):
    """Set this person's referral state from one message; the latest message wins.

    Replaying the same message with the same state changes nothing, so a decision applied twice
    records once. Every earlier state stays in `history`.
    """
    if not isinstance(contact_id, str) or not contact_id.strip():
        raise ValueError("REFERRAL_CONTACT_REQUIRED")
    previous = current(database, contact_id)
    if previous and previous["source_key"] == source_key and previous["state"] == state:
        return previous
    entry = {"state": state, "source_key": source_key, "statement": statement, "next_action": next_action,
             "review_at": review_at, "at": now, "evidence": evidence}
    history = (previous["history"] if previous else []) + [entry]
    database.execute("""INSERT INTO referrals VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(contact_id) DO UPDATE SET
        state=excluded.state, source_key=excluded.source_key, statement=excluded.statement,
        next_action=excluded.next_action, review_at=excluded.review_at, updated_at=excluded.updated_at,
        history=excluded.history""",
        (contact_id, state, source_key, statement, next_action, review_at, now, json.dumps(history, sort_keys=True)))
    return current(database, contact_id)
