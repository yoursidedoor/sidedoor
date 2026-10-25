import resources
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import uuid

import applications
import application_destination
import provider_limits
import user_profile
import paths


READ_OPERATIONS = {"inspect", "verify", "reconcile", "investigate"}
# Part E: an employer-scoped, read-only Gmail history search. Deliberately not a job
# operation (not in READ_OPERATIONS): it has no job destination, is created only through
# `DesktopAdapter.history_search` and is ingested by `external_history.collect_history_searches`.


class StoredReadFailure(ConnectionError):
    """A committed read whose stored result is an error, carrying that error's real code.

    Still a ConnectionError for existing callers, but `code` is the code the consumer
    recorded (e.g. APPLICATION_UNOBSERVED_QUOTE), so `applications.process` no longer files
    a deterministic validation failure as a transient preflight problem.
    """
    stored_read_failure = True

    def __init__(self, code, request_id):
        super().__init__(code)
        self.code = code
        self.request_id = request_id


class ProviderUsageWait(applications.AdapterPending):
    """The provider refused the turn for usage: a capacity wait, never a failed read."""

    def __init__(self, request_id, next_at):
        super().__init__(request_id, next_at)
        self.code = provider_limits.CODE
        self.next_action = provider_limits.NEXT_ACTION


def _before(now, resume):
    return dt.datetime.fromisoformat(now) < dt.datetime.fromisoformat(resume)


def provider_wait(database, request, now):
    """Handle a saved read the provider refused for usage (caller owns the open transaction).

    While the provider's reset is ahead this commits and raises ProviderUsageWait; once it
    has passed it returns True and the caller treats the request as absent, because no read
    happened. Any other request returns False untouched.
    """
    if not request or request["state"] not in {"DONE", "CONSUMED"}:
        return False
    resume = provider_limits.stored_wait(request["result"])
    if not resume:
        return False
    if request["state"] == "DONE":
        database.execute("UPDATE desktop_requests SET state='CONSUMED' WHERE request_id=? AND state='DONE'",
                         (request["request_id"],))
        record(database, request["request_id"], provider_limits.EVENT, now,
               {"resume_at": resume, "counts_as_read": False, "counts_as_failure": False})
    if _before(now, resume):
        database.commit()
        raise ProviderUsageWait(request["request_id"], resume)
    return True


HOST_BLOCKER = "DESKTOP_UNATTENDED_HOST_UNAVAILABLE"


def host_health(database=None, now=None):
    now = now or applications.stamp()
    result = {
        "schema_version": 1, "observed_at": now, "state": "BLOCKED",
        "code": HOST_BLOCKER, "owner": "orchestrator",
        "unattended_ready": False, "can_submit": False,
        "implementation": "foreground_mailbox_only",
        "required_capability": "supported_unattended_existing_desktop_browser_session",
        "next_action": "Establish a supported existing-subscription desktop Browser Use host and verify a scheduled read-only mailbox round trip before enabling unattended consumption",
        "mailbox": {"state": "NOT_CHECKED"},
    }
    if database is None:
        return result
    try:
        rows = database.execute("""
            SELECT operation,state,count(*) AS total,
                sum(CASE WHEN expires_at<=? OR
                    (state='CLAIMED' AND (lease_until IS NULL OR lease_until<=?))
                    THEN 1 ELSE 0 END) AS expired
            FROM desktop_requests GROUP BY operation,state
        """, (now, now)).fetchall()
    except sqlite3.Error:
        result["mailbox"] = {"state": "UNAVAILABLE", "code": "DESKTOP_MAILBOX_UNREADABLE"}
        return result
    counts = {}
    expired_reads = expired_submits = unknown_submits = active_claims = 0
    for operation, state, total, expired in rows:
        counts[state] = counts.get(state, 0) + total
        if state == "CLAIMED":
            active_claims += total - expired
        if state in {"PENDING", "CLAIMED"}:
            if operation == "submit":
                expired_submits += expired
            elif operation in READ_OPERATIONS:
                expired_reads += expired
        if operation == "submit" and state == "UNKNOWN":
            unknown_submits += total
    result["mailbox"] = {
        "state": "READABLE", "request_counts": counts,
        "active_claims": active_claims, "expired_read_requests": expired_reads,
        "expired_submit_requests": expired_submits,
        "unknown_submit_requests": unknown_submits,
        "requires_reconciliation": bool(expired_submits or unknown_submits),
        "submission_retry_authorized": False,
    }
    return result


def host_health_at(root, now=None):
    database_path = paths.database(Path(root).resolve())
    try:
        database = sqlite3.connect(database_path.as_uri() + "?mode=ro", uri=True, timeout=1)
    except sqlite3.Error:
        result = host_health(now=now)
        result["mailbox"] = {"state": "UNAVAILABLE", "code": "DESKTOP_MAILBOX_UNREADABLE"}
        return result
    try:
        return host_health(database, now)
    finally:
        database.close()


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def later(now, seconds):
    return (dt.datetime.fromisoformat(now) + dt.timedelta(seconds=seconds)).isoformat()


# One claim's lease. Must exceed application_browser.MAX_SECONDS plus consume()'s 10s
# margin, or the lease caps the session below its configured budget.
LEASE_SECONDS = 960
# How long a request may wait to be serviced (it must fit a full MAX_SECONDS session after a late
# claim), and - separately - how long a FINISHED read is reused for an identical request. The two
# were one 900 s value until the session limit went to 900 s (owner, 2026-09-28); reuse keeps 900 s
# so a scheduled re-read still reads the page afresh.
REQUEST_SECONDS = 1500
READ_REUSE_SECONDS = 900


def initialize(database):
    database.executescript("""
        CREATE TABLE IF NOT EXISTS desktop_requests (
            request_id TEXT PRIMARY KEY, binding TEXT NOT NULL, job_key TEXT NOT NULL,
            operation TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL,
            created_at TEXT NOT NULL, expires_at TEXT NOT NULL, claims INTEGER NOT NULL DEFAULT 0,
            owner TEXT, token_hash TEXT, lease_until TEXT, armed_at TEXT, result TEXT,
            attempt_id TEXT);
        CREATE INDEX IF NOT EXISTS desktop_requests_binding ON desktop_requests(binding,created_at);
        CREATE INDEX IF NOT EXISTS desktop_requests_state ON desktop_requests(state,lease_until);
        CREATE UNIQUE INDEX IF NOT EXISTS desktop_submit_attempt ON desktop_requests(attempt_id)
            WHERE operation='submit';
        CREATE TABLE IF NOT EXISTS desktop_events (
            id INTEGER PRIMARY KEY, request_id TEXT NOT NULL, at TEXT NOT NULL,
            event TEXT NOT NULL, detail TEXT NOT NULL);
    """)


def record(database, request_id, kind, now, detail=None):
    database.execute("INSERT INTO desktop_events(request_id,at,event,detail) VALUES (?,?,?,?)",
                     (request_id, now, kind, encode(detail or {})))


def destination(job):
    role = json.loads(job["payload"])
    return application_destination.public_url(role["url"])


# The browser consumer receives the recovery context inside the desktop request body, capped
# at RECOVERY_CONTEXT_LIMIT bytes. application_recovery repeats every earlier finding (in
# previous_failure AND previous_attempts) with the investigating AI's own verbose report, so by
# the third claim a context routinely outgrew the cap and the request was refused with
# APPLICATION_RECOVERY_CONTEXT_INVALID before any read - an orchestrator-side failure the
# coordinator then read as evidence and abandoned on (Stark, 2026-09-19). A context within
# the cap is sent byte-for-byte as before (same request binding). An oversized one is reduced
# deterministically to the structured fields the consumer acts on; the full context stays in
# the APPLICATION_RECOVERY_PREPARED event. Nothing here is an identity, quote or destination
# guard: the dropped material is the earlier model's unverified narrative, and every guard
# still runs on what the browser itself returns.
RECOVERY_CONTEXT_LIMIT = 16384
RECOVERY_LIFECYCLE_KEYS = ("blocker", "started_at", "deadline", "max_attempts", "attempt",
                           "supported_methods", "supported_alternative")
RECOVERY_FINDING_KEYS = ("attempt", "at", "actual_reads", "supported_alternative", "reason", "status",
                         "error_code", "resolution_verified", "next_action", "next_alternative",
                         "source", "browser_trace", "blocked", "resolution")
RECOVERY_REPORT_KEYS = ("reported_status", "reported_reason", "reported_next_action")
RECOVERY_DECISION_KEYS = ("action", "method", "instructions", "reason", "progress")


def compact_decision(decision):
    return {key: decision[key] for key in RECOVERY_DECISION_KEYS if key in decision} if isinstance(decision, dict) else decision


def compact_finding(finding):
    if not isinstance(finding, dict):
        return finding
    compact = {key: finding[key] for key in RECOVERY_FINDING_KEYS if key in finding}
    report = finding.get("reported_investigation")
    if isinstance(report, dict):
        compact["reported_investigation"] = {key: report[key] for key in RECOVERY_REPORT_KEYS if key in report}
    return compact


def recovery_context(recovery):
    if not isinstance(recovery, dict):
        raise ValueError("APPLICATION_RECOVERY_CONTEXT_INVALID")
    size = len(encode(recovery))
    if size <= RECOVERY_CONTEXT_LIMIT:
        return recovery
    compact = {key: recovery[key] for key in RECOVERY_LIFECYCLE_KEYS if key in recovery}
    if "decision" in recovery:
        compact["decision"] = compact_decision(recovery["decision"])
    if "previous_failure" in recovery:
        compact["previous_failure"] = compact_finding(recovery["previous_failure"])
    attempts = [compact_finding(item) for item in recovery.get("previous_attempts") or []]
    decisions = [dict(item, decision=compact_decision(item.get("decision"))) if isinstance(item, dict) else item
                 for item in recovery.get("previous_decisions") or []]
    compact["previous_execution_failures"] = recovery.get("previous_execution_failures") or []
    compact["coordinator_failures"] = [{key: item.get(key) for key in ("attempt", "reason")} if isinstance(item, dict) else item
                                       for item in recovery.get("coordinator_failures") or []]
    omitted = 0
    while True:
        candidate = dict(compact, previous_attempts=attempts[omitted:], previous_decisions=decisions[omitted:],
                         context_compacted={"original_bytes": size, "omitted_oldest_attempts": omitted,
                                            "full_context": "APPLICATION_RECOVERY_PREPARED event"})
        if len(encode(candidate)) <= RECOVERY_CONTEXT_LIMIT:
            return candidate
        if omitted >= max(len(attempts), len(decisions)):
            raise ValueError("APPLICATION_RECOVERY_CONTEXT_INVALID")
        omitted += 1


def expired_investigation(request):
    return {"status": "UNRESOLVED", "reason": "DESKTOP_INVESTIGATION_EXPIRED",
            "next_action": "The single investigation claim is exhausted; the orchestrator must choose the next bounded instruction",
            "resolution": {"identity": None, "destination": None, "history": []},
            "error": {"code": "DESKTOP_INVESTIGATION_EXPIRED", "request_id": request["request_id"], "claims": request["claims"]}}


def submission_verdict(database, job_key, attempt_id=None):
    """What the submit page itself reported, for the one in-flight submission.

    Returns {"acknowledged": bool, "reported_failure": str|None, "observed_alternatives": [...]},
    or None when no submit result is recorded. `acknowledged` is the only outcome-shaped fact a
    submit is allowed to produce, and until 2026-09-24 nothing in applications.py ever read it -
    so a page that reported a failure and a page that acknowledged success produced the same
    SUBMISSION_OUTCOME_UNKNOWN hold. This is a report of evidence, never a receipt: a submit still
    cannot establish that an application was accepted.
    """
    request = submission_request(database, job_key, attempt_id)
    if not request or not request["result"]:
        return None
    try:
        result = json.loads(request["result"])
    except (ValueError, TypeError):
        return None
    if not isinstance(result, dict):
        return None
    alternatives = result.get("observed_alternatives")
    failure = result.get("reported_failure")
    return {"acknowledged": result.get("acknowledged") is True,
            "reported_failure": failure if isinstance(failure, str) and failure.strip() else None,
            "observed_alternatives": alternatives if isinstance(alternatives, list) else []}


def submission_request(database, job_key, attempt_id=None):
    if not database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='desktop_requests'").fetchone():
        return None
    return database.execute("""SELECT request.* FROM desktop_requests AS request
        JOIN application_attempts AS attempt ON attempt.attempt_id=request.attempt_id
        AND attempt.job_key=request.job_key WHERE request.job_key=? AND request.operation='submit'
        AND attempt.state='SUBMITTING' AND (? IS NULL OR request.attempt_id=?)
        ORDER BY attempt.started_at DESC,request.rowid DESC LIMIT 1""",
        (job_key, attempt_id, attempt_id)).fetchone()


def validate_read_result(database, request, result, now):
    status = result.get("status")
    if status not in {"SUBMITTED", "NOT_SUBMITTED"}:
        return
    code = "INDEPENDENT_EMPLOYER_RECEIPT_REQUIRED" if status == "SUBMITTED" else "AUTHORITATIVE_ABSENCE_REQUIRED"
    submit = submission_request(database, request["job_key"], request["attempt_id"])
    if request["operation"] not in {"verify", "reconcile"} or not submit:
        raise ValueError(code)
    if status == "NOT_SUBMITTED" and database.execute("SELECT 1 FROM desktop_requests WHERE job_key=? AND operation='submit' AND state IN ('PENDING','CLAIMED')",
                                                      (request["job_key"],)).fetchone():
        raise ValueError("SUBMISSION_STILL_INFLIGHT")
    job = json.loads(submit["payload"])["job"]
    read_job = json.loads(request["payload"])["job"]
    attempt = database.execute("SELECT started_at,evidence FROM application_attempts WHERE attempt_id=?", (submit["attempt_id"],)).fetchone()
    try:
        observed = dt.datetime.fromisoformat(result["observed_at"])
        after_attempt = (observed <= dt.datetime.fromisoformat(now)
                         and all(observed >= dt.datetime.fromisoformat(value) for value in
                                 (attempt["started_at"], request["created_at"], submit["armed_at"] or submit["created_at"])))
    except (KeyError, TypeError, ValueError):
        after_attempt = False
    # An employer confirmation email carries no requisition id (see verified_receipt): the
    # email route binds on job_key and attempt_id below, which this product issued and can
    # check, instead of a field the evidence cannot contain.
    # Only a SUBMITTED receipt may take an email route (and so skip the requisition match): an
    # absence is always matched on the requisition, whatever label the read gave itself.
    email_receipt = status == "SUBMITTED" and result.get("evidence_kind") in {"employer_confirmation_email", "sent_application_email"}
    requisition_keys = ("job_key", "employer", "candidate_email") if email_receipt else (
        "job_key", "employer", "requisition", "candidate_email")
    # employer_accepted, not a raw fold (owner, 2026-09-21): a page rendering a legal name
    # is not a different employer, and `.get(key, "")` defaults only when the key is ABSENT,
    # so a present-and-null employer reached normalized(None) and raised TypeError here.
    # The read's employer judgement (employer_matches_expected) settles a SUBMITTED receipt
    # (owner, 2026-10-07, item 1); a "not submitted" result, which could open a second
    # application, still needs the employer exactly as recorded.
    employer_read = result if status == "SUBMITTED" else {key: value for key, value in result.items()
                                                          if key != "employer_matches_expected"}
    identity_matches = (application_destination.employer_accepted(employer_read, job)
                        and (email_receipt
                             or str(result.get("requisition") or "").strip().casefold() == job["requisition"])
                        and result.get("candidate_email") == job["candidate_email"]
                        and all(read_job.get(key) == job.get(key) for key in requisition_keys))
    if (not after_attempt or not identity_matches or not applications.fresh(result, now)
            or result.get("independent_read") is not True
            or any(not isinstance(result.get(key), str) or not result[key].strip()
                   for key in ("source",))
            or result.get("job_key", job["job_key"]) != job["job_key"]
            or result.get("attempt_id", submit["attempt_id"]) != submit["attempt_id"]):
        raise ValueError(code)
    if status == "SUBMITTED":
        if (not applications.verified_receipt(result, job, now)
                or result.get("evidence_kind") not in {"employer_confirmation_email", "authenticated_ats_application_record",
                                                       "sent_application_email"}):
            raise ValueError(code)
        if result.get("evidence_kind") == "sent_application_email":
            # Authoritative, from this attempt's own saved review rather than anything the read
            # reports about itself: the sent message must be this attempt's email application,
            # addressed to exactly the recipient quoted from the official posting, and sent no
            # earlier than the instant the arm opened - nothing this attempt sent can predate it.
            try:
                target = applications.email_application(json.loads(attempt["evidence"]).get("review"))
                sent_at = dt.datetime.fromisoformat(str(result.get("sent_at")).replace("Z", "+00:00"))
                armed = dt.datetime.fromisoformat(submit["armed_at"])
            except (TypeError, ValueError, KeyError, AttributeError):
                raise ValueError(code)
            recipients = result.get("email_to")
            if (target is None or not isinstance(recipients, list) or len(recipients) != 1
                    or not isinstance(recipients[0], str)
                    or recipients[0].strip().casefold() != target["address"].casefold() or sent_at < armed):
                raise ValueError(code)
    else:
        # Absence no longer demands an authenticated ATS record (owner, 2026-09-21). Most
        # employers give a signed-out candidate no account and therefore no such record, so
        # the label made absence unprovable for exactly the jobs that needed it and parked
        # them on SUBMISSION_OUTCOME_UNKNOWN forever. evidence_kind is a string the reader
        # supplies about itself - nothing verifies it, unlike the quotes quote_observed
        # checks against real tool output - so requiring it bought no safety. What does the
        # work is kept: authoritative_absence, no_inflight, independent_read, a non-empty
        # source, the attempt/job binding, freshness, and the SUBMISSION_STILL_INFLIGHT
        # read above. applications.reconcile (:1767) has always accepted absence on exactly
        # these terms; this validator was the stricter duplicate that fired first.
        if (result.get("job_key") != job["job_key"] or result.get("attempt_id") != submit["attempt_id"]
                or result.get("authoritative_absence") is not True or result.get("no_inflight") is not True):
            raise ValueError(code)


def completed_result(database, request, now):
    result = json.loads(request["result"])
    committed = database.execute("SELECT at FROM desktop_events WHERE request_id=? AND event='RESULT_COMMITTED' ORDER BY id DESC LIMIT 1",
                                 (request["request_id"],)).fetchone()
    if not committed:
        raise ValueError("DESKTOP_RESULT_COMMIT_EVIDENCE_REQUIRED")
    validate_read_result(database, request, result, committed["at"] if result.get("status") == "SUBMITTED" else now)
    return dict(result, _desktop_request={"request_id": request["request_id"], "binding": request["binding"],
                                         "attempt_id": request["attempt_id"], "accepted_at": committed["at"]})


def recover_result(database, job, now, attempt_id=None):
    submit = submission_request(database, job["job_key"], attempt_id)
    if not submit:
        return None
    requests = database.execute("""SELECT * FROM desktop_requests WHERE job_key=? AND attempt_id=?
        AND operation IN ('verify','reconcile') AND state='DONE' ORDER BY rowid DESC""",
        (job["job_key"], submit["attempt_id"])).fetchall()
    for request in requests:
        try:
            result = completed_result(database, request, now)
        except ValueError:
            continue
        except (KeyError, TypeError) as error:
            unreadable_result(database, request, error, now)
            continue
        if result.get("status") == "SUBMITTED":
            return result
    for request in requests:
        try:
            return completed_result(database, request, now)
        except ValueError:
            continue
        except (KeyError, TypeError) as error:
            unreadable_result(database, request, error, now)
            continue
    return None


def unreadable_result(database, request, error, now):
    """A stored verify/reconcile result our own code could not read (KeyError/TypeError), as
    opposed to one validation refused (ValueError). Still treated as no result, but recorded."""
    detail = {"exception_type": type(error).__name__, "detail": str(error)[:200]}
    if database.in_transaction:
        record(database, request["request_id"], "RESULT_UNREADABLE", now, detail)
    else:
        with database:
            record(database, request["request_id"], "RESULT_UNREADABLE", now, detail)


def acknowledge_result(database, job, result, now):
    if not database.in_transaction:
        database.execute("BEGIN IMMEDIATE")
    metadata = result.get("_desktop_request", {})
    request = database.execute("SELECT * FROM desktop_requests WHERE request_id=?", (metadata.get("request_id"),)).fetchone()
    if (not request or request["state"] != "DONE" or request["job_key"] != job["job_key"]
            or request["operation"] not in {"verify", "reconcile"}
            or completed_result(database, request, now) != result):
        raise ValueError("DESKTOP_RESULT_BINDING_MISMATCH")
    stored_job = json.loads(request["payload"])["job"]
    if any(job.get(key) != stored_job.get(key) for key in ("job_key", "employer", "requisition", "candidate_email")):
        raise ValueError("DESKTOP_RESULT_ACCOUNT_MISMATCH")
    changed = database.execute("UPDATE desktop_requests SET state='CONSUMED' WHERE request_id=? AND state='DONE'", (request["request_id"],))
    if changed.rowcount != 1:
        raise ValueError("DESKTOP_RESULT_ALREADY_CONSUMED")
    record(database, request["request_id"], "RESULT_DISPOSED", now)
    return request["attempt_id"]


class DesktopAdapter:
    def __init__(self, database, root, config, clock=applications.stamp):
        self.database, self.root, self.config, self.clock = database, root, config, clock
        self.can_submit = bool(config.get("execution_ownership_verified"))
        initialize(database)

    def request_body(self, operation, payload):
        if operation not in READ_OPERATIONS | {"submit"}:
            raise ValueError("Unsupported desktop operation")
        job = payload["job"]
        policy = user_profile.load()
        latest = self.database.execute("SELECT attempt_id FROM application_attempts WHERE job_key=? ORDER BY started_at DESC,rowid DESC LIMIT 1", (job["job_key"],)).fetchone()
        body = {"job": {key: job[key] for key in ("job_key", "employer", "requisition", "payload", "candidate_email")},
                "url": destination(job), "attempt_id": payload.get("attempt_id") or (latest[0] if latest else None),
                "policy_sha256": digest(policy), "operation": operation, "read_only": operation != "submit"}
        if operation == "investigate":
            body["recovery"] = recovery_context(payload.get("recovery", {}))
        if operation == "submit":
            body.update({key: payload[key] for key in ("answers", "resume", "form_revision", "idempotency_key", "no_extra_answers")})
            if payload.get("attachments"):
                body["attachments"] = payload["attachments"]
            body.update({key: payload[key] for key in ("questions", "conditional_questions_deferred",
                                                       "candidate_email_binding", "recorded_facts", "eligible_location")
                         if key in payload})
            body["resume_sha256"] = user_profile.application_resume(policy)["sha256"]
        return body

    def cached(self, operation, payload):
        current = self.database.execute("SELECT state FROM application_jobs WHERE job_key=?", (payload["job"]["job_key"],)).fetchone()
        if not current or current["state"] in applications.FINAL:
            return None
        now = self.clock()
        if operation in {"verify", "reconcile"}:
            recovered = recover_result(self.database, payload["job"], now, payload.get("attempt_id"))
            if recovered is not None:
                return recovered
        body = self.request_body(operation, payload)
        request = self.database.execute("SELECT * FROM desktop_requests WHERE binding=? ORDER BY rowid DESC LIMIT 1", (digest(body),)).fetchone()
        resume = provider_limits.stored_wait(request["result"]) if request and request["state"] in {"DONE", "CONSUMED"} else None
        if resume:
            # A usage-limit refusal is not a cached read: while it lasts `call` raises the wait
            # without new admission; afterwards the read is simply absent.
            return json.loads(request["result"]) if operation not in {"verify", "reconcile"} and _before(now, resume) else None
        if request and operation == "investigate" and not request["claims"] and request["state"] != "DONE":
            # Never picked up by a session: a compute wait, not a failed investigation (2026-09-28).
            return None
        if request and operation == "investigate" and request["state"] in {"PENDING", "CLAIMED"}:
            if now >= request["expires_at"] or request["state"] == "CLAIMED" and (not request["lease_until"] or now >= request["lease_until"]):
                return expired_investigation(request)
        if request and operation == "investigate" and request["state"] in {"DONE", "CONSUMED", "EXPIRED"}:
            return json.loads(request["result"]) if request["result"] else expired_investigation(request)
        if not request or request["state"] != "DONE":
            return None
        if operation in {"verify", "reconcile"}:
            try:
                return completed_result(self.database, request, now)
            except ValueError:
                return None
            except (KeyError, TypeError) as error:
                unreadable_result(self.database, request, error, now)
                return None
        if operation == "submit" or now < request["expires_at"]:
            return json.loads(request["result"])
        return None

    def cached_available(self, operation, payload):
        return self.cached(operation, payload) is not None

    def call(self, operation, payload):
        if operation not in READ_OPERATIONS | {"submit"}:
            raise ValueError("Unsupported desktop operation")
        job = payload["job"]
        now = self.clock()
        current = self.database.execute("SELECT state FROM application_jobs WHERE job_key=?", (job["job_key"],)).fetchone()
        if not current or current["state"] in applications.FINAL:
            raise applications.AdapterUnavailable("APPLICATION_ALREADY_FINAL_OR_MISSING")
        try:
            body = self.request_body(operation, payload)
        except ValueError as error:
            return {"blocked": {"code": str(error), "next_action": "Verify official ATS destination and canonical requisition"}}
        attempt_id = body["attempt_id"]
        if operation in {"verify", "reconcile"}:
            recovered = self.cached(operation, payload)
            if recovered is not None:
                return recovered
        if operation == "reconcile" and attempt_id:
            pending = self.database.execute("SELECT * FROM desktop_requests WHERE job_key=? AND operation='submit' AND state IN ('PENDING','CLAIMED') ORDER BY rowid DESC LIMIT 1", (job["job_key"],)).fetchone()
            if pending:
                if now < pending["expires_at"] and (pending["state"] == "PENDING" or now < pending["lease_until"]):
                    raise applications.AdapterPending(pending["request_id"], later(now, 60))
                with self.database:
                    self.database.execute("UPDATE desktop_requests SET state='UNKNOWN' WHERE request_id=?", (pending["request_id"],))
                    record(self.database, pending["request_id"], "SUBMISSION_LEASE_EXPIRED", now)
        binding = digest(body)
        self.database.execute("BEGIN IMMEDIATE")
        try:
            current = self.database.execute("SELECT state FROM application_jobs WHERE job_key=?", (job["job_key"],)).fetchone()
            if not current or current["state"] in applications.FINAL:
                raise applications.AdapterUnavailable("APPLICATION_ALREADY_FINAL_OR_MISSING")
            if operation == "submit":
                previous = self.database.execute("SELECT binding FROM desktop_requests WHERE operation='submit' AND attempt_id=?", (attempt_id,)).fetchone()
                if previous and previous[0] != binding:
                    raise ValueError("SUBMISSION_ATTEMPT_PAYLOAD_CHANGED")
            request = self.database.execute("SELECT * FROM desktop_requests WHERE binding=? ORDER BY rowid DESC LIMIT 1", (binding,)).fetchone()
            if provider_wait(self.database, request, now):
                request = None
            if (request and operation in READ_OPERATIONS and not request["claims"]
                    and request["state"] in {"PENDING", "EXPIRED"} and now >= request["expires_at"]):
                # No session ever picked this read up, usually because browser time ran out first. That
                # is a compute wait, not a failed read, so it costs the job no try: a fresh request is
                # queued below instead. Before 2026-09-28 this raised TimeoutError and Gringotts Industries
                # was marked EXHAUSTED by a read that never ran.
                # The synthetic DESKTOP_INVESTIGATION_EXPIRED an older sweeper wrote here is cleared:
                # no session ran, so there is no failure for recovery to count.
                self.database.execute("UPDATE desktop_requests SET state='CONSUMED',result=NULL WHERE request_id=?", (request["request_id"],))
                record(self.database, request["request_id"], "REQUEST_NEVER_STARTED", now)
                request = None
            if request and operation == "investigate":
                lost_claim = request["state"] == "CLAIMED" and (not request["lease_until"] or now >= request["lease_until"])
                expired = request["state"] in {"PENDING", "CLAIMED"} and now >= request["expires_at"]
                if lost_claim or expired:
                    result = expired_investigation(request)
                    self.database.execute("UPDATE desktop_requests SET state='EXPIRED',result=? WHERE request_id=?",
                                          (encode(result), request["request_id"]))
                    record(self.database, request["request_id"], "INVESTIGATION_EXPIRED", now)
                    self.database.commit()
                    return result
                if request["state"] in {"DONE", "CONSUMED", "EXPIRED"}:
                    result = json.loads(request["result"]) if request["result"] else expired_investigation(request)
                    self.database.commit()
                    return result
            if request and request["state"] == "DONE" and operation not in {"verify", "reconcile"}:
                if operation == "submit" or now < later(request["created_at"], READ_REUSE_SECONDS):
                    result = json.loads(request["result"])
                    if result.get("error"):
                        self.database.execute("UPDATE desktop_requests SET state='CONSUMED' WHERE request_id=?", (request["request_id"],))
                    self.database.commit()
                    if result.get("error"):
                        error = result["error"] if isinstance(result["error"], dict) else {}
                        code = error.get("code") if isinstance(error.get("code"), str) and re.fullmatch(
                            r"(?:APPLICATION|DESKTOP|ATS|BROWSER)_[A-Z0-9_]{1,100}", error.get("code")) else "DESKTOP_READ_FAILED"
                        raise StoredReadFailure(code, request["request_id"])
                    return result
            if request and request["state"] == "EXPIRED" and operation in READ_OPERATIONS:
                self.database.execute("UPDATE desktop_requests SET state='CONSUMED' WHERE request_id=?", (request["request_id"],))
                self.database.commit()
                raise TimeoutError("Desktop read exhausted its delivery budget")
            if request and request["state"] in {"PENDING", "CLAIMED"}:
                if now < request["expires_at"]:
                    self.database.commit()
                    raise applications.AdapterPending(request["request_id"], later(now, 60))
                self.database.execute("UPDATE desktop_requests SET state='CONSUMED' WHERE request_id=?", (request["request_id"],))
                record(self.database, request["request_id"], "REQUEST_EXPIRED", now)
                self.database.commit()
                raise TimeoutError("Desktop request expired without a result")
            if operation == "submit" and request:
                self.database.commit()
                return {"status": "UNKNOWN", "reason": "DESKTOP_SUBMIT_REQUEST_ALREADY_EXISTS"}
            request_id = str(uuid.uuid4())
            self.database.execute("INSERT INTO desktop_requests(request_id,binding,job_key,operation,payload,state,created_at,expires_at,attempt_id) VALUES (?,?,?,?,?,'PENDING',?,?,?)",
                                  (request_id, binding, job["job_key"], operation, encode(body), now, later(now, REQUEST_SECONDS), attempt_id))
            record(self.database, request_id, "REQUEST_CREATED", now, {"operation": operation})
            self.database.commit()
        except BaseException:
            self.database.rollback()
            raise
        raise applications.AdapterPending(request_id, later(now, 60))


def submission_gate(database, root, request, now):
    payload = json.loads(request["payload"])
    config = resources.read_json("config.json")
    policy = user_profile.load()
    # Owner, 2026-09-22 - guards removed from this gate by number, as listed to him:
    #   2 POLICY_CHANGED                  4 APPLICATION_DESTINATION_CHANGED
    #   7 first clause (form_revision)    8 second clause (history freshness)
    #   10 first clause (review-reported candidate_email)
    #   11 APPLICATION_OR_REFERRAL_HISTORY_UNCLEARED (the whole block)
    #
    # 1 is deliberately kept: the pause switch. `applications_enabled` in config.json is how a
    #   run is stopped. Since 2026-09-30 (owner: the gate runs only at arm) it is honoured at arm,
    #   so a paused run can still start a submit session but never arms or submits.
    # 5 (the official-destination 8-point match) and the other-runner flag are deleted
    #   (owner, 2026-09-27, items 22 and 27).
    if config.get("application_adapter") != "desktop" or not config.get("applications_enabled") or not policy.get("preapproved_applications"):
        raise ValueError("DESKTOP_SUBMISSION_NOT_ENABLED")
    job = database.execute("SELECT * FROM application_jobs WHERE job_key=?", (request["job_key"],)).fetchone()
    attempt = database.execute("SELECT * FROM application_attempts WHERE attempt_id=? AND job_key=? AND state='SUBMITTING'", (payload["attempt_id"], request["job_key"])).fetchone()
    if not job or job["state"] not in applications.UNCERTAIN or not attempt or applications.local_gate(database, job, now, policy=policy):
        raise ValueError("DURABLE_SUBMISSION_CLAIM_INVALID")
    evidence = json.loads(attempt["evidence"])
    review = evidence["review"]
    # Deleted (owner, 2026-09-27): the 8-point match (Guard 5, item 27) and the all-questions-found
    # check (item 17).
    # Guard 8's freshness window is gone. Vandelay Security's Gmail search failed, so its history
    # block was undated, so "not fresh" - and the submit was refused for having found nothing:
    # armed=None, never clicked, while `assess` passed the job as ELIGIBLE. The eligibility
    # re-run itself stays.
    if applications.assess(review, job, policy, now):
        raise ValueError("ELIGIBILITY_OR_HISTORY_EXPIRED")
    # The mailbox-derived absence re-check is deleted (owner, 2026-09-27): duplicates are decided from
    # the local database (local_gate above) only.
    email_target = applications.email_application(review)
    if email_target is not None and not applications.email_applications_enabled(config):
        raise ValueError("EMAIL_APPLICATIONS_NOT_ENABLED")
    # The candidate email binding (item 27) is deleted (owner, 2026-09-27).
    # Guard 11 removed in full (owner, 2026-09-22, having been shown what it bought: a cross-job
    # check on the history block, a mailbox-derived duplicate check independent of the tracker,
    # and referral-first enforcement at the gate). It demanded the saved block positively read
    # application False, interview False, referral NONE - so a read whose Gmail search failed
    # was treated exactly like a read that found a prior application. Vandelay Security's block is
    # all nulls. Duplicate protection remains `local_gate` above, which queries the tracker
    # live on this same call; referral blocking remains upstream in `applications.process`.
    # An answer to a FILE question legitimately leaves `answers` and becomes an attachment:
    # applications.py renders it to a PDF because a file control cannot be typed into, and the
    # attempt evidence is recorded BEFORE that split (deliberately - the row must exist before
    # any risky work so a crash still reconciles). Comparing the two dicts directly therefore
    # refused every submission carrying a generated cover letter with SUBMISSION_PAYLOAD_MISMATCH,
    # before any browser session ran (pawnee health, 2026-09-21). The comparison now puts
    # each attachment's field back, so the question is still "are these the exact recorded
    # answers, and nothing else" - an attachment for a field the evidence never held, or any
    # changed answer, still mismatches.
    submitted = dict(payload["answers"])
    for field in (payload.get("attachments") or {}):
        if field in evidence["answers"] and field not in submitted:
            submitted[field] = evidence["answers"][field]
    if (evidence["answers"] != submitted or review.get("form_revision") != payload["form_revision"]
            or payload.get("no_extra_answers") is not True or payload["idempotency_key"] != job["job_key"]):
        raise ValueError("SUBMISSION_PAYLOAD_MISMATCH")
    current_policy = dict(policy, answers=dict(policy["answers"]))
    for saved in database.execute("SELECT * FROM application_answers WHERE job_key=?", (job["job_key"],)):
        current_policy["answers"][saved["field"]] = {"semantic": saved["semantic"], "value": json.loads(saved["value"]), "source": saved["source"]}
    answers, missing = applications.resolve_answers(review["questions"] if isinstance(review.get("questions"), list) else [], current_policy)
    # `resolve_answers` returns every answer including the one rendered to an attachment, so it
    # is compared against the reconstructed set rather than the payload, for the same reason as
    # the check above. DH fixed that one and left this one, which then refused the next attempt
    # as VERIFIED_ANSWERS_CHANGED (pawnee health, 2026-09-21).
    if missing or answers != submitted:
        raise ValueError("VERIFIED_ANSWERS_CHANGED")
    designated = user_profile.application_resume(policy)   # the file the user chose in Settings > Résumé
    resume = root / designated["path"]
    if Path(payload["resume"]).resolve() != resume.resolve() or hashlib.sha256(resume.read_bytes()).hexdigest() != designated["sha256"]:
        raise ValueError("DESIGNATED_RESUME_MISMATCH")
    if payload.get("attachments"):
        # Every other attached file is one this product generated for this job (a cover letter),
        # unchanged since it was written. Nothing else on disk can be attached.
        import cover_letter
        cover_letter.verify(root, payload["attachments"])
    return evidence


def claim(database, root, owner, now=None, read_only=False, request_id=None):
    now = now or applications.stamp()
    if not owner.strip():
        raise ValueError("Desktop consumer identity required")
    database.execute("BEGIN IMMEDIATE")
    try:
        if database.execute("SELECT 1 FROM desktop_requests WHERE state='CLAIMED' AND lease_until>? LIMIT 1", (now,)).fetchone():
            database.commit()
            return None
        rows = database.execute("SELECT * FROM desktop_requests WHERE state IN ('PENDING','CLAIMED') ORDER BY rowid").fetchall()
        for request in rows:
            expired = now >= request["expires_at"]
            lost_submit = request["operation"] == "submit" and request["state"] == "CLAIMED"
            claim_limit = 1 if request["operation"] == "investigate" else 3
            if expired or lost_submit or request["claims"] >= claim_limit:
                state = "UNKNOWN" if request["operation"] == "submit" else "EXPIRED"
                database.execute("UPDATE desktop_requests SET state=? WHERE request_id=?", (state, request["request_id"]))
                if request["operation"] == "investigate" and request["claims"]:
                    database.execute("UPDATE desktop_requests SET result=? WHERE request_id=?",
                                     (encode(expired_investigation(request)), request["request_id"]))
                record(database, request["request_id"], state, now)
                continue
            if request_id and request["request_id"] != request_id or read_only and request["operation"] == "submit":
                continue
            job = database.execute("SELECT * FROM application_jobs WHERE job_key=?", (request["job_key"],)).fetchone()
            if not job or job["state"] in applications.FINAL:
                database.execute("UPDATE desktop_requests SET state='EXPIRED' WHERE request_id=?", (request["request_id"],))
                continue
            # The submission gate runs once, at arm, not also here at claim (owner, 2026-09-30:
            # "This only needs to happen at only one place. it should not be repeated... It should
            # only happen at arm."). The claim hands out the token; arm re-derives everything
            # immediately before the one Submit.
            token = secrets.token_hex(32)
            # The claim lease must outlast a full-length browser session or it, not the
            # session ceiling, becomes the real timeout: `application_browser.consume`
            # computes min(configured_budget, lease_remaining - 10). At 300s the 480s
            # ceiling raised on 2026-09-22 was unreachable - every read silently got 290s.
            # Kept above application_browser.MAX_SECONDS with headroom for that margin;
            # tests/test_browser_session_bounds.py pins the two together.
            until = min(later(now, LEASE_SECONDS), request["expires_at"])
            database.execute("UPDATE desktop_requests SET state='CLAIMED',owner=?,token_hash=?,lease_until=?,claims=claims+1 WHERE request_id=?",
                             (owner, digest(token), until, request["request_id"]))
            record(database, request["request_id"], "CLAIMED", now, {"owner": owner})
            database.commit()
            return {"request_id": request["request_id"], "binding": request["binding"], "token": token,
                    "lease_until": until, "payload": json.loads(request["payload"])}
        database.commit()
        return None
    except BaseException:
        database.rollback()
        raise


def extend_for_human_check(database, request_id, until, now):
    """Keep a claimed request alive while the owner clears a CAPTCHA (owner, 2026-09-28).

    The same claim is extended, never a new one: only a CLAIMED request whose lease is still
    running, and never past `until`, which the human-check wait bounds. An armed request is left
    alone, so nothing that has already reached the final guard gains time.
    """
    database.execute("BEGIN IMMEDIATE")
    try:
        request = database.execute("SELECT * FROM desktop_requests WHERE request_id=?", (request_id,)).fetchone()
        if (not request or request["state"] != "CLAIMED" or request["armed_at"]
                or now >= request["lease_until"] or now >= request["expires_at"]):
            database.rollback()
            return False
        database.execute("UPDATE desktop_requests SET lease_until=max(lease_until,?),expires_at=max(expires_at,?) WHERE request_id=?",
                         (until, until, request_id))
        record(database, request_id, "HUMAN_CHECK_WAIT", now, {"until": until})
        database.commit()
        return True
    except BaseException:
        database.rollback()
        raise


def owned(database, envelope, now):
    request = database.execute("SELECT * FROM desktop_requests WHERE request_id=?", (envelope["request_id"],)).fetchone()
    if (not request or request["state"] != "CLAIMED" or now >= request["lease_until"]
            or now >= request["expires_at"] or request["binding"] != envelope["binding"]
            or not secrets.compare_digest(request["token_hash"], digest(envelope["token"]))):
        raise ValueError("STALE_OR_MISMATCHED_DESKTOP_CLAIM")
    return request


def arm(database, root, envelope, now=None):
    now = now or applications.stamp()
    database.execute("BEGIN IMMEDIATE")
    try:
        request = owned(database, envelope, now)
        if request["operation"] != "submit" or request["armed_at"]:
            raise ValueError("SUBMIT_INTENT_ALREADY_CONSUMED_OR_READ_ONLY")
        evidence = submission_gate(database, root, request, now)
        review = evidence["review"]
        # The email-application From/To checks and the candidate email binding are deleted
        # (owner, 2026-09-27, item 27: "There is no 8 point check anymore or email binding").
        if isinstance(envelope.get("observed_questions"), list):
            # Owner, 2026-09-21: "If a selection reveals new unanswered required questions, the
            # right thing is to try to answer them, not break the entire submission."
            #
            # This compared a HASH of the form against the one recorded at inspect time, so any
            # difference refused the one Submit - a reworded label, a field the page renders
            # twice (Rippling renders First name, Last name and Location in two places, which is
            # what refused pawnee health on 2026-09-21 with the form complete, both files
            # uploaded, consent given and Apply enabled), and, self-defeatingly, the very
            # conditional question the old comment said it existed to catch: if filling the form
            # reveals a new required question the hash MUST differ, so such a form could never
            # be submitted at all.
            #
            # The invariant that actually matters is that nothing required is left unanswered.
            # That is checked directly below, against the answers this submission carries, so a
            # newly revealed question is caught when it is UNANSWERED rather than merely new.
            # The resume hash and the exact recorded answers are still verified by
            # submission_gate above.
            answered = set(json.loads(request["payload"]).get("answers") or {})
            answered |= set(json.loads(request["payload"]).get("attachments") or {})
            # The submission told the session which field each question's answer is stored
            # under; a question it echoes back is identified by that field, not by re-deriving
            # a key from how this session happened to word the label (owner, 2026-09-21). Only
            # a field the submission actually supplied is accepted, so the session cannot
            # invent one to silence this check, and a question it reports WITHOUT a known field
            # is treated as newly revealed and derived as before - which is exactly the
            # conditional-question case this check exists to catch.
            supplied = {question.get("field") for question in json.loads(request["payload"]).get("questions") or []
                        if isinstance(question, dict) and question.get("field")}

            def observed_field(question):
                field = question.get("field")
                if isinstance(field, str) and field in supplied:
                    return field
                return applications.question_field(question)

            unanswered = sorted({observed_field(question)
                                 for question in envelope["observed_questions"]
                                 if isinstance(question, dict) and question.get("required") is True
                                 and observed_field(question) not in answered})
            if unanswered:
                refusal = ValueError("APPLICATION_REQUIRED_QUESTION_UNANSWERED")
                refusal.unanswered = unanswered
                # Diagnostics, owner 2026-09-21. This refusal recorded only its reason, so an
                # operator could see THAT the arm was refused and never WHICH question, nor
                # whether the session's key for a question even matched the key its answer was
                # stored under. Two nights were spent inferring that from the outside. These
                # three lists make the next one a lookup: what the live session observed, what
                # the submission carried, and the difference the guard acted on.
                refusal.observed = sorted({applications.question_field(question)
                                           for question in envelope["observed_questions"]
                                           if isinstance(question, dict) and question.get("required") is True})
                refusal.answered = sorted(answered)
                refusal.observed_labels = [
                    {"key": applications.question_field(question), "label": question.get("label"),
                     "semantic": question.get("semantic"), "id": question.get("id")}
                    for question in envelope["observed_questions"]
                    if isinstance(question, dict) and question.get("required") is True]
                raise refusal
        # The form-version comparison (part of the 8-point match) and the deferred email
        # binding are deleted (owner, 2026-09-27). What remains is the invariant the owner
        # listed: nothing required is left unanswered (checked above when the session reports
        # the live form).
        database.execute("UPDATE desktop_requests SET armed_at=? WHERE request_id=?", (now, request["request_id"]))
        record(database, request["request_id"], "SUBMIT_POSSIBLE", now)
        database.commit()
        return {"may_submit_once": True, "lease_until": request["lease_until"]}
    except BaseException:
        database.rollback()
        raise


def shortened_answers(result):
    """Free-text answers the submit session rewrote to fit a character limit (owner, 2026-10-01).

    Kept as the record of what was typed; never evidence of an outcome.
    """
    items = result.get("shortened_answers") if isinstance(result, dict) else None
    if not isinstance(items, list):
        return {}
    kept = [item for item in items[:50] if isinstance(item, dict)]
    return {"shortened_answers": kept} if kept else {}


def block(database, envelope, now=None):
    return complete(database, envelope, now, prearm_blocker=True)


def complete(database, envelope, now=None, *, prearm_blocker=False):
    now = now or applications.stamp()
    database.execute("BEGIN IMMEDIATE")
    try:
        request = owned(database, envelope, now)
        result = envelope["result"]
        if not isinstance(result, dict) or "_desktop_request" in result:
            raise ValueError("Structured browser result required")
        if prearm_blocker and (request["operation"] != "submit" or request["armed_at"]):
            raise ValueError("UNARMED_SUBMISSION_BLOCKER_REQUIRED")
        if request["operation"] == "submit":
            if not request["armed_at"]:
                blocker = result.get("blocked")
                if (not isinstance(blocker, dict) or any(not isinstance(blocker.get(key), str) or not blocker[key].strip()
                                                       for key in ("code", "next_action"))
                        or result.get("acknowledged") or result.get("status") in {"SUBMITTED", "NOT_SUBMITTED"}):
                    raise ValueError("SUBMISSION_NOT_ARMED")
                result = {"status": "UNKNOWN", "acknowledged": False, "prearm_blocked": True,
                          "blocked": blocker, "requires_independent_receipt": True,
                          **({"observed_questions": result["observed_questions"]}
                             if isinstance(result.get("observed_questions"), list) else {}),
                          **shortened_answers(result)}
                record(database, request["request_id"], "PREARM_BLOCKED", now, blocker)
            else:
                # A submit may still never ASSERT an outcome: status stays UNKNOWN (since 2026-10-06
                # an armed submit with acknowledged true is the receipt, settled by
                # applications.settle_page_confirmation; requires_independent_receipt is kept as
                # recorded shape only). What changes (owner, 2026-09-24) is that
                # the page's NEGATIVE evidence survives. This reduction used to keep only
                # `acknowledged`, so a page reporting "submission failed" and a page reporting
                # "thanks, we got it" arrived downstream identical, and the reported failure - plus
                # any alternative route the page offered, such as Rekall's Ashby link - was
                # discarded here. Preserving it can only make the system more conservative or send
                # it to recovery; it can never manufacture a success.
                result = {"acknowledged": result.get("acknowledged") is True,
                          "status": "UNKNOWN", "requires_independent_receipt": True,
                          **({"confirmation_text": str(result["confirmation_text"])[:600]}
                             if isinstance(result.get("confirmation_text"), str) and result["confirmation_text"].strip() else {}),
                          **({"reported_failure": str(result["reported_failure"])[:600]}
                             if isinstance(result.get("reported_failure"), str) and result["reported_failure"].strip() else {}),
                          **({"observed_alternatives": [str(item)[:300] for item in result["observed_alternatives"][:5]]}
                             if isinstance(result.get("observed_alternatives"), list) and result["observed_alternatives"] else {}),
                          **shortened_answers(result)}
        validate_read_result(database, request, result, now)
        database.execute("UPDATE desktop_requests SET state='DONE',result=? WHERE request_id=?", (encode(result), request["request_id"]))
        record(database, request["request_id"], "RESULT_COMMITTED", now)
        database.commit()
        return {"accepted": True, "request_id": request["request_id"]}
    except BaseException:
        database.rollback()
        raise


def main():
    parser = argparse.ArgumentParser(description="Durable handoff to existing desktop Browser Use; no browser implementation")
    parser.add_argument("command", choices=["claim", "arm", "complete", "block", "status", "host-health"])
    parser.add_argument("--root", type=Path, default=None, help="the data folder (default: the Sidedoor home)")
    parser.add_argument("--owner", default="desktop-browser")
    parser.add_argument("--read-only", action="store_true")
    parser.add_argument("--request-id")
    parser.add_argument("--mailbox", action="store_true", help="Include read-only mailbox health; only valid with host-health")
    args = parser.parse_args()
    args.root = paths.data(args.root)
    if args.mailbox and args.command != "host-health":
        parser.error("--mailbox requires host-health")
    if args.command == "host-health":
        result = host_health_at(args.root) if args.mailbox else host_health()
        # Machine contract (E2.T6): the command's JSON result; stays print.
        print(json.dumps(result, indent=2))
        return 2
    with sqlite3.connect(paths.database(args.root), timeout=10) as database:
        database.row_factory = sqlite3.Row
        initialize(database)
        if args.command == "claim":
            result = claim(database, args.root, args.owner, read_only=args.read_only, request_id=args.request_id)
        elif args.command == "arm":
            result = arm(database, args.root, json.load(sys.stdin))
        elif args.command == "complete":
            result = complete(database, json.load(sys.stdin))
        elif args.command == "block":
            result = block(database, json.load(sys.stdin))
        else:
            result = [dict(row) for row in database.execute("SELECT request_id,job_key,operation,state,claims,created_at,expires_at,lease_until,armed_at FROM desktop_requests ORDER BY rowid")]
    # Machine contract (E2.T6): the command's JSON result; stays print.
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    sys.exit(main())
