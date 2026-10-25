"""Bounded read-only recovery persisted in the existing application event ledger."""

import datetime as dt
import json
from pathlib import PurePath, PurePosixPath
import re

import applications
import provider_limits
import paths


METHODS = {
    "official_posting": "Read the official posting and its exact role identity using the existing browser",
    "observed_links": "Follow observed official company/ATS links to establish the same role and a supported destination",
    "authenticated_history": "Compare scoped saved authenticated employer history with freshly read public requisitions through the existing browser",
}
RECOVERABLE = {
    "REQUISITION_UNKNOWN", "REQUISITION_MISMATCH", "PRIOR_PROCESS_REQUISITION_UNRESOLVED",
    "UNSUPPORTED_OFFICIAL_DESTINATION", "APPLICATION_DESTINATION_OR_SCOPE_UNKNOWN",
    "APPLICATION_OFFICIAL_DESTINATION_REQUIRED", "APPLICATION_OFFICIAL_IDENTITY_REQUIRED",
    "APPLICATION_HISTORY_UNKNOWN", "IDENTITY_MISMATCH", "ELIGIBILITY_UNVERIFIED",
    "ELIGIBILITY_UNKNOWN", "EMPLOYEE_COUNT_UNVERIFIED", "SALARY_UNKNOWN",
    "CROSSING_BASE_BAND_UNRESOLVED", "REQUIRED_QUESTIONS_UNKNOWN",
    "APPLICATION_BROWSER_INVESTIGATION_EXHAUSTED", "PREFLIGHT_TRANSIENT",
    # PRIOR_APPLICATION_WITHIN_90_DAYS left this set on 2026-10-06: the 90-day employer window is
    # deleted ("never block employers unless the jobs are the same"), so a job still carrying it
    # takes the ordinary path and is re-evaluated instead of starting a recovery.
    "MALFORMED_OR_UNAVAILABLE_EVIDENCE", "FORM_REVIEW_INCOMPLETE",
    "INSPECTION_INCOMPLETE",
    # Owner, 2026-09-24: the submit page reported a failure and named a route (Rekall offered
    # an Ashby link). Recoverable so the coordinator can pursue that route once reconciliation
    # has resolved the outstanding attempt - `outstanding()` still refuses to recover before
    # then, so this grants nothing while a submission may still have reached an employer.
    # SUBMISSION_OBSERVATION_LOST is deliberately NOT here: nothing was observed, so there is
    # no finding to act on and reconciliation is the only honest next step.
    "SUBMISSION_REPORTED_FAILED",
}
# AUTHENTICATION_REQUIRED, CAPTCHA_REQUIRED and MFA_REQUIRED left this set on 2026-10-11 (owner:
# retried in an hour, abandoned on the third; applications.ACCESS_BLOCK_CODES). Recovery still
# never takes one of them as the blocker it recovers (`recover`).
USER_HOLDS = {
    "REQUIRED_ELIGIBILITY_ANSWER", "DESIGNATED_RESUME_MISMATCH", "APPLICATION_AUTHORIZATION_REQUIRED",
    # Review mode holds the application for the owner's approval (owner, 2026-10-24).
    "AWAITING_APPLICATION_APPROVAL",
}
# Browser permission handling is deleted (owner, 2026-09-27 item 2 and 2026-09-30): a read the
# browser platform refused is an ordinary failed read, retried on the ordinary path and bounded
# by the attempt limit.
# EXECUTION_OWNERSHIP_UNVERIFIED went with item 22.

# Spending the last investigation claim is a bound, not a contextual judgment that the
# task is unresolvable. Terminal abandonment stays with the AI coordinator; the
# deterministic layer parks the job on the existing reviewable browser-exhaustion
# blocker instead. The hold is terminal for *automatic* recovery - `claims_exhausted`
# below refuses re-entry so no fourth claim is ever granted - while the job record
# itself stays non-terminal and visible in `application_blockers`.
# At most 7 recovery attempts (owner, 2026-09-28: "Recover should be max 7 attempts").
MAX_RECOVERY_ATTEMPTS = 7
CLAIMS_EXHAUSTED_BLOCKER = "APPLICATION_BROWSER_INVESTIGATION_EXHAUSTED"
CLAIMS_EXHAUSTED_EVENT = "APPLICATION_RECOVERY_CLAIMS_EXHAUSTED"
# The AI review that used to consume this hold is deleted (owner, 2026-09-27); the hold is
# released only by an explicit reopen.
CLAIMS_EXHAUSTED_ACTION = ("Parked: the investigation allowance is spent and no further automatic investigation "
                           "or review will run; only an explicit owner reopen releases this job")

# The parked hold above is consumed by the same AI coordinator that made the RETRY/ABANDON
# decisions during the lifecycle, through the same `adapter.recovery_decision` protocol.
# Its own bounds are explicit: at most `max_hold_reviews` recorded reviews per job and at most
# `HOLD_REVIEW_FAILURE_LIMIT` coordinator failures per job. A review grants no investigation
# claim: `claims_exhausted` still refuses every re-entry, so the attempt allowance (at most 7)
# is never exceeded. A RETRY review resumes the job through the ordinary application path for one uninterrupted pass
# under the existing action guard, duplicate protection and submission claims; an ABANDON
# review records the terminal outcome through the existing AI-judged `abandon` path.
#
# RESUMPTION_WAITS are the codes that record an outstanding read rather than a decision:
# they do not conclude the one ordinary attempt a resumption is worth.
RESUMPTION_WAITS = {"DESKTOP_BROWSER_PENDING", "APPLICATION_BROWSER_CYCLE_CAPACITY",
                    "APPLICATION_BROWSER_SHARED_BUDGET_EXHAUSTED", "APPLICATION_BROWSER_BUSY", provider_limits.CODE,
                    "APPLICATION_BROWSER_RUNTIME_MISSING", "APPLICATION_BROWSER_PROFILE_IN_USE"}
HOLD_REVIEW_EVENT = "APPLICATION_RECOVERY_HOLD_REVIEWED"
HOLD_REVIEW_FAILED_EVENT = "APPLICATION_RECOVERY_HOLD_REVIEW_FAILED"
HOLD_REVIEW_FAILURE_LIMIT = 2
HOLD_RESUME_ACTION = ("Resumed by an AI orchestrator hold review: one ordinary application attempt through the "
                      "existing inspection, action guard and duplicate protection; no further recovery "
                      "investigation claim is granted")

# An investigation the orchestrator could not even hand to the browser (the desktop request
# body was refused before any claim, zero reads) is an orchestrator-side defect, not evidence
# about the world. Like a compute wait it is recorded but is not a finding: no
# APPLICATION_RECOVERY_INVESTIGATED is written, so the AI coordinator never sees it as a failed
# investigation to abandon on and no claim or attempt number is consumed. The job is parked on
# an orchestrator-owned hold (no owner notification) that automatic recovery and abandonment
# do not advance; it resumes by re-sending the same prepared AI decision as soon
# as that request body builds again (i.e. once the defect is fixed).
ORCHESTRATOR_DEFECT_CODES = {"APPLICATION_RECOVERY_CONTEXT_INVALID"}
ORCHESTRATOR_DEFECT_BLOCKER = "APPLICATION_RECOVERY_ORCHESTRATOR_DEFECT"
ORCHESTRATOR_DEFECT_EVENT = "APPLICATION_RECOVERY_NOT_EXECUTED"
ORCHESTRATOR_DEFECT_ACTION = ("Orchestrator defect: the prepared read-only investigation could not be issued and no read "
                              "was performed; it is not a failed investigation. Fix the defect; the same prepared "
                              "investigation resumes automatically once its request builds")


# The coordinator's per-scope admission cap (application_connection.COORDINATOR_ADMISSION_CAP)
# is an orchestrator-side limit, not an AI judgment. A refusal on it is therefore not recorded
# as APPLICATION_RECOVERY_DECISION_FAILED and never leads to abandonment by itself: the job is
# parked on an orchestrator-owned hold (no owner notification) that neither automatic recovery
# nor abandonment advances, and resumes once a new scope (owner reopen grant or
# recovery supersession) makes an admission available again. AI-judged abandonment is kept.
COORDINATOR_CAP_CODE = "APPLICATION_COORDINATOR_ATTEMPTS_EXHAUSTED"
COORDINATOR_CAP_BLOCKER = "APPLICATION_RECOVERY_COORDINATOR_CAP_HELD"
COORDINATOR_CAP_EVENT = "APPLICATION_RECOVERY_COORDINATOR_CAP_REACHED"
COORDINATOR_CAP_ACTION = ("Orchestrator limit: the AI coordinator admissions for this job's current scope are spent, so "
                          "no coordinator decision was made; this is not a failed investigation or an AI judgment. "
                          "Held for owner review; resumes automatically when a new scope admits a coordinator call")


def coordinator_capped(adapter, key, now):
    check = getattr(adapter, "coordinator_cap_reached", None)
    try:
        return bool(check and check(key, now))
    except Exception:
        return False


def hold_coordinator_cap(database, job, lifecycle, attempt, now):
    current = database.execute("SELECT * FROM application_jobs WHERE job_key=?", (job["job_key"],)).fetchone()
    if not current or current["state"] in applications.FINAL or outstanding(database, current):
        return False
    if current["blocker"] != COORDINATOR_CAP_BLOCKER:
        applications.event(database, job["job_key"], COORDINATOR_CAP_EVENT, {
            "attempt": attempt, "code": COORDINATOR_CAP_CODE, "counts_as_coordinator_failure": False,
            "terminal": False, "previous_blocker": current["blocker"],
            "next_action": COORDINATOR_CAP_ACTION}, now)
    return applications.hold(database, current, COORDINATOR_CAP_BLOCKER, COORDINATOR_CAP_ACTION, now)


def defect_persists(database, key, adapter, since):
    """True while the parked investigation's request body still cannot be built."""
    findings = scoped(database, key, "APPLICATION_RECOVERY_INVESTIGATED", since)
    packet = next((item for item in scoped(database, key, "APPLICATION_RECOVERY_PREPARED", since)
                   if item["recovery"]["attempt"] == len(findings) + 1), None)
    if packet is None or not hasattr(adapter, "request_body"):
        return False
    try:
        adapter.request_body("investigate", packet)
    except ValueError as error:
        return str(error) in ORCHESTRATOR_DEFECT_CODES
    except Exception:
        return False
    return False


def failure_code(value):
    if isinstance(value, str) and re.fullmatch(r"(?:APPLICATION|DESKTOP|ATS|BROWSER|RECOVERY|PREFLIGHT)_[A-Z0-9_]{1,100}", value):
        return value
    return None


def exception_failure(error):
    name = type(error).__name__
    return {"exception_type": name if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", name) else "Exception",
            "code": failure_code(getattr(error, "code", None)) or failure_code(str(error))}


def trace_reference(value, root):
    if not isinstance(value, str) or len(value) > 1024:
        return None
    # This system's own path syntax (Windows: C:\\...\\trace.jsonl as recorded there; VM pass,
    # 2026-10-23), answered in the "reports/..." form either way.
    reference = PurePath(value)
    if ".." in reference.parts:
        return None
    if reference.is_absolute():
        # Under the data folder, or (a record written before E9.T1 moved the data into the home)
        # under the code folder it used to sit in.
        bases = ([PurePath(str(root))] if root is not None else []) + [
            PurePath(str(folder)) for folder in paths.legacy_roots(root, recorded=False)]
        for base in bases:
            try:
                reference = reference.relative_to(base)
                break
            except ValueError:
                continue
        else:
            return None
    reference = PurePosixPath(*reference.parts)
    if (len(reference.parts) == 4 and reference.parts[:2] == (paths.REPORTS, "application-browser")
            and re.fullmatch(r"request-[A-Za-z0-9_-]{1,100}", reference.parts[2])
            and reference.name == "trace.jsonl"):
        return str(reference)
    return None


def previous_execution_failures(database, job_key, root=None):
    failures = []
    if database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='desktop_requests'").fetchone():
        requests = database.execute("""SELECT request_id,operation,state,claims,created_at,result FROM desktop_requests
            WHERE job_key=? AND operation IN ('inspect','investigate') AND claims>0 AND json_valid(result)
            AND json_type(result,'$.error')='object' AND json_valid(payload)
            AND json_extract(payload,'$.job.job_key')=? ORDER BY rowid DESC LIMIT 5""", (job_key, job_key)).fetchall()
        for request in requests:
            if provider_limits.stored_result_limited(request["result"]):
                continue  # the provider refused the turn for usage: a wait, not a failed read
            stored = json.loads(request["result"])
            error = stored["error"]
            reference = trace_reference(error.get("trace_path"), root)
            failure = {"source": "desktop_requests", "request_id": request["request_id"], "job_key": job_key,
                       "operation": request["operation"], "state": request["state"], "actual_reads": request["claims"],
                       "at": request["created_at"], "code": failure_code(error.get("code")),
                       "artifacts": {"trace": reference} if reference else {}}
            # Forward WHICH condition failed, not just the code (owner, 2026-09-21). EC began
            # recording `failed_conditions` on a stored read error - submission_destination
            # names which of its nine conditions refused - and `unanswered` from the arm
            # guard. Without them the coordinator is told only
            # "APPLICATION_OFFICIAL_DESTINATION_REQUIRED" plus a path to a 600KB trace it
            # cannot read, so it cannot tell an employer-name mismatch from a missing
            # candidate email, a null form_revision or a wrong URL - four different repairs.
            # Bounded and shape-checked: these reach an AI packet, so only short strings from
            # a fixed alphabet are passed on, never arbitrary text from a read.
            for field in ("failed_conditions", "unanswered"):
                value = error.get(field)
                if isinstance(value, list):
                    names = [item for item in value[:20]
                             if isinstance(item, str) and 0 < len(item) <= 120 and re.fullmatch(r"[A-Za-z0-9_.:\-]+", item)]
                    if names:
                        failure[field] = names
            # The pages the failed session opened (2026-09-28), so the coordinator can send the
            # next read straight to what it found, e.g. an employer's own board. Unverified leads.
            carried = stored.get("carried_forward") if isinstance(stored.get("carried_forward"), dict) else {}
            pages = [url for url in (carried.get("pages_opened") or [])[:12]
                     if isinstance(url, str) and len(url) <= 300 and re.fullmatch(r"https?://[^\s]+", url)]
            if pages:
                failure["unverified_pages_opened"] = pages
            failures.append(failure)
    events = database.execute("""SELECT id,at,detail FROM application_events WHERE job_key=?
        AND event='APPLICATION_PREFLIGHT_FAILED' ORDER BY id DESC LIMIT 5""", (job_key,)).fetchall()
    for saved in events:
        try:
            detail = json.loads(saved["detail"])
            exception = detail.get("exception", {})
            name = exception.get("exception_type")
        except (ValueError, AttributeError, TypeError):
            continue
        failures.append({"source": "application_events", "event_id": saved["id"], "job_key": job_key,
                         "operation": "inspect", "at": saved["at"],
                         "code": failure_code(exception.get("code")) or failure_code(detail.get("reason")),
                         "exception_type": name if isinstance(name, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", name) else None})
    return sorted(failures, key=lambda item: item["at"], reverse=True)[:5]


def user_hold(code):
    return code in USER_HOLDS or str(code).startswith("UNKNOWN_REQUIRED_ANSWERS:")


# Owner decision 2026-09-19: a recovery lifecycle only ever recovers a blocker the CURRENT
# rules still raise. History-class blockers are raised by Python gates alone (the local
# tracker gate, `external_history.gate` and the D1 absence conclusion), so before any
# recovery step - coordinator decision, investigation read, claim-exhaustion hold, hold
# review or abandonment - the lifecycle's blocker is re-evaluated structurally (codes only).
# A lifecycle whose blocker the current rules no longer raise is ended as SUPERSEDED: no
# coordinator call, no read, no claim, nothing abandoned; prior events stay as recorded and
# the job goes back to the ordinary path (or onto the blocker the current gate does raise).
SUPERSEDED_EVENT = "APPLICATION_RECOVERY_SUPERSEDED"
SUPERSEDED_REASON = "blocker no longer raised by current rules"
# Every code here is one `applications.local_gate` itself raises, which is what makes
# `current_gate` able to ask whether it is STILL raised. REQUISITION_UNKNOWN joined them on
# 2026-09-24: local_gate stopped raising it at all (approach A - a posting identified by the
# hash of its canonical URL is accepted rather than held), so the four jobs parked on it, each
# carrying a live identity-recovery lifecycle, would otherwise have been handed straight back
# to recovery on every cycle to resolve an id their employers do not publish. Superseding is
# the existing route for exactly this - it charges nothing, preserves every prior record, and
# re-holds the job on whatever the current gates DO say, which is how vandelay security stays held
# on PRIOR_APPLICATION_WITHIN_90_DAYS instead of being released.
# PRIOR_APPLICATION_WITHIN_90_DAYS joined on 2026-10-06 for the same reason: nothing raises it any
# more, so a lifecycle on it is superseded.
HISTORY_CLASS = frozenset({"PRIOR_PROCESS_REQUISITION_UNRESOLVED", "PRIOR_APPLICATION_IDENTITY_UNRESOLVED",
                           "APPLICATION_HISTORY_UNKNOWN", "REQUISITION_UNKNOWN", "PRIOR_APPLICATION_WITHIN_90_DAYS"})


def history_class(code):
    return isinstance(code, str) and (code in HISTORY_CLASS or code.startswith("EXISTING_"))


def current_gate(database, job, now, policy=None, code=None):
    """The current code's verdict for this job's history-class blocker, as structured data."""
    gate = applications.local_gate(database, job, now, policy)
    result = ({"result": "PASS"} if not gate else
              {"result": gate[0], "code": gate[1], "next_action": gate[2], "next_at": gate[3]})
    if not gate and code == "APPLICATION_HISTORY_UNKNOWN":
        import external_history
        conclusion = external_history.absence_conclusion(
            database, dict(job, candidate_email=job.get("candidate_email")), now)
        result["history_absence_concluded"] = conclusion["concluded"]
        result["history_absence_reason"] = conclusion["reason"]
    return gate, result


def still_raised(code, gate, result):
    if code == "APPLICATION_HISTORY_UNKNOWN":
        # Raised by inspection only when the gate passes and D1 absence cannot be concluded.
        return not gate and not result["history_absence_concluded"]
    return bool(gate) and gate[1] == code


def superseded_scope(database, key, now):
    """Records after the latest supersession (or live reopen grant) form the current lifecycle."""
    reopen = reopen_scope(database, key, now)
    row = database.execute("SELECT MAX(id) FROM application_events WHERE job_key=? AND event=?",
                           (key, SUPERSEDED_EVENT)).fetchone()
    values = [value for value in (reopen, row[0] if row else None) if value is not None]
    return max(values) if values else None


def supersede_stale(database, job, blocker, lifecycle, now, policy=None):
    """End recovery on a history-class blocker the current rules no longer raise.

    Returns the recorded detail when superseded, else None (the blocker still applies, or it
    is not a gate-sourced blocker, so recovery - and AI-judged abandonment - proceeds).
    """
    if not history_class(blocker):
        return None
    job = dict(job)
    gate, result = current_gate(database, job, now, policy, blocker)
    if still_raised(blocker, gate, result):
        return None
    key = job["job_key"]
    with database:
        current = database.execute("SELECT * FROM application_jobs WHERE job_key=?", (key,)).fetchone()
        if not current or current["state"] in applications.FINAL or outstanding(database, current):
            return None
        detail = {"job_key": key, "old_blocker": blocker, "reason": SUPERSEDED_REASON,
                  "lifecycle": ({field: lifecycle.get(field) for field in ("blocker", "started_at", "max_attempts")}
                                if lifecycle else None),
                  "current_gate": result, "charged": {"coordinator_calls": 0, "investigation_claims": 0},
                  "prior_records_preserved": True, "previous_state": current["state"],
                  "previous_blocker": current["blocker"]}
        if gate:
            detail["outcome"] = "HELD_ON_CURRENT_BLOCKER"
            applications.event(database, key, SUPERSEDED_EVENT, detail, now)
            applications.hold(database, current, gate[1], gate[2], now, gate[0], gate[3])
        else:
            detail["outcome"] = "RETURNED_TO_NORMAL_PROCESSING"
            applications.event(database, key, SUPERSEDED_EVENT, detail, now)
            database.execute("""UPDATE application_jobs SET state='QUEUED',blocker=NULL,next_action=NULL,next_at=?,updated_at=?
                WHERE job_key=? AND state NOT IN ('SUBMITTED','DUPLICATE','INELIGIBLE','EXHAUSTED','ABANDONED')""", (now, now, key))
            database.execute("DELETE FROM application_blockers WHERE job_key=?", (key,))
    return detail


def settings(config):
    value = config.get("application_recovery", {})
    if value.get("enabled") is not True:
        return None
    # Recovery has no time limit of any kind (owner, 2026-09-24 and 2026-09-28: "Application
    # recovery should never have a deadline"). It ends only when the attempt budget is spent or
    # the AI coordinator explicitly abandons - both judgments about the work itself.
    # At most 7 attempts (owner, 2026-09-28: "Recover should be max 7 attempts").
    attempts = value.get("max_attempts", MAX_RECOVERY_ATTEMPTS)
    if type(attempts) is not int or not 1 <= attempts <= MAX_RECOVERY_ATTEMPTS:
        raise ValueError("APPLICATION_RECOVERY_BOUNDS_INVALID")
    return {"max_attempts": attempts}


def reopen_scope(database, key, now):
    """Event-id threshold of an explicit, job-scoped user reopen authorization.

    The owner's reopen tool (not part of the product; grants it wrote are still honoured)
    recorded a bounded allowance (the ordinary attempt cap, at most 7 investigation claims -
    never more) in
    `application_reopen_grants` / `application_reopen_grant_jobs`, alongside one
    `APPLICATION_REOPENED` event per job. Without this reader `recover()` would keep
    using `starts[0]` and every historical finding/decision row, so a reopened job is
    re-abandoned inside the same cycle and the recorded allowance is a dead letter.

    The scope is an `application_events.id`, not a timestamp, because
    `APPLICATION_RECOVERY_DECIDED` details carry only an attempt number: filtering by
    id is what stops a saved ABANDON decision for attempt N being replayed against a
    fresh attempt N. A grant never expires (owner, 2026-09-28);
    only its attempt allowance bounds it. Nothing is ever deleted, rewritten or un-counted.
    """
    if now is None or not database.execute("""SELECT 1 FROM sqlite_master WHERE type='table'
            AND name='application_reopen_grant_jobs'""").fetchone():
        return None
    row = database.execute("""SELECT consumed.reopen_event_id FROM application_reopen_grant_jobs AS consumed
        JOIN application_reopen_grants AS granted ON granted.authorization_id=consumed.authorization_id
        WHERE consumed.job_key=? AND consumed.reopened_at<=?
        ORDER BY consumed.reopen_event_id DESC LIMIT 1""", (key, now)).fetchone()
    return row[0] if row else None


def records(database, key, kind, since=None):
    values = [json.loads(row[0]) for row in database.execute(
        "SELECT detail FROM application_events WHERE job_key=? AND event=? AND id>COALESCE(?,-1) ORDER BY id", (key, kind, since))]
    if kind == "APPLICATION_RECOVERY_INVESTIGATED":
        rejected = {item["attempt"]: item["reason"] for item in records(database, key, "APPLICATION_RECOVERY_RESOLUTION_REJECTED", since)}
        for value in values:
            if value["attempt"] in rejected:
                value["reason"] = rejected[value["attempt"]]
    return values


def scoped(database, key, kind, since):
    """`records()` limited to the events a live reopen authorization put back in scope.

    `since is None` - no grant table, no grant for this job, or an expired grant -
    returns exactly what `records()` has always returned.
    """
    return records(database, key, kind, since)


def counted_failures(database, key, kind, since):
    """Recorded coordinator failures that count: a usage-limit refusal (its saved executor
    log shows it) made no decision and is a wait, not a failure."""
    return [item for item in scoped(database, key, kind, since)
            if not provider_limits.decision_limited(item) and item.get("reason") != COORDINATOR_CAP_CODE]


def investigate_claims(database, key, started_at, attempt=None):
    """(requests, claims) of this lifecycle's investigate reads, excluding provider refusals."""
    rows = [row for row in database.execute("""SELECT claims,result FROM desktop_requests
        WHERE job_key=? AND operation='investigate' AND json_extract(payload,'$.recovery.started_at')=?
        AND (? IS NULL OR json_extract(payload,'$.recovery.attempt')=?)""", (key, started_at, attempt, attempt))
        if not provider_limits.stored_result_limited(row[1])]
    return len(rows), sum(row[0] for row in rows)


def active(database, key, now=None):
    """Is a recovery lifecycle still running for this job?

    Two fixes, 2026-09-22, both found through umbrella.

    SCOPE. Every other read in this module goes through `scoped(..., since)` so a reopen
    authorization puts the old lifecycle out of scope; this one queried the raw event log and
    ignored grants entirely. That is why the owner's reopen tool forecast "no lifecycle is
    in scope" while `process` handed the job straight back to recovery on the very next cycle.

    ABANDONMENT CLOSES IT. A lifecycle that ended in ABANDON left APPLICATION_RECOVERY_DECIDED
    as the newest event, which this read counted as still running - so `active` stayed true
    FOREVER and every future reopen was captured by the dead lifecycle before the job could
    reach the ordinary path. Umbrella was abandoned at 17:38:55 by a coordinator that said,
    correctly, "no concrete different supported read is evidenced": investigation genuinely
    could not fix it, because what it needed was a submission attempt.
    """
    # `since` is an application_events.id (superseded_scope), so the scope compares ids. It used
    # to compare the `at` timestamp with that id; SQLite then compared '2026-...' with e.g. '28'
    # as text, which is false for every row, so any reopened job read as having no active
    # lifecycle and re-inspected instead of continuing its recovery (2026-09-28).
    since = superseded_scope(database, key, now or applications.stamp())
    latest = database.execute("""SELECT event, detail FROM application_events WHERE job_key=? AND event IN
        ('APPLICATION_RECOVERY_STARTED','APPLICATION_RECOVERY_RESOLVED','APPLICATION_RECOVERY_INVESTIGATED',
         'APPLICATION_RECOVERY_DECIDED','APPLICATION_RECOVERY_DECISION_FAILED','APPLICATION_RECOVERY_SUPERSEDED')
        """ + ("AND id >= ? " if since is not None else "") + """
        ORDER BY id DESC LIMIT 1""", ((key, since) if since is not None else (key,))).fetchone()
    if not latest or latest[0] in {"APPLICATION_RECOVERY_RESOLVED", SUPERSEDED_EVENT}:
        return False
    if latest[0] == "APPLICATION_RECOVERY_DECIDED":
        try:
            if (json.loads(latest[1]).get("decision") or {}).get("action") == "ABANDON":
                return False
        except (TypeError, ValueError):
            pass
    return True


def outstanding(database, job):
    if job["state"] in applications.UNCERTAIN or job["blocker"] == "SUBMISSION_OUTCOME_UNKNOWN":
        return True
    if database.execute("SELECT 1 FROM application_attempts WHERE job_key=? AND state='SUBMITTING'", (job["job_key"],)).fetchone():
        return True
    import desktop_adapter
    request = desktop_adapter.submission_request(database, job["job_key"])
    return bool(request and request["state"] not in {"CONSUMED"})


def counted_reads(findings):
    """Actual reads in recorded findings; a read the provider refused for usage is a wait."""
    return sum(item.get("actual_reads", 1) for item in findings
               if not provider_limits.stored_result_limited(item.get("result")))


def read_count(database, key, lifecycle, findings):
    total = counted_reads(findings)
    if database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='desktop_requests'").fetchone():
        total = max(total, investigate_claims(database, key, lifecycle["started_at"])[1])
    return total


def actual_reads(database, key, packet, adapter):
    import desktop_adapter
    fallback = 0 if isinstance(adapter, desktop_adapter.DesktopAdapter) else 1
    if not database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='desktop_requests'").fetchone():
        return fallback
    reads = investigate_claims(database, key, packet["recovery"]["started_at"], packet["recovery"]["attempt"])
    return reads[1] if reads[0] else fallback


def abandon(database, job, lifecycle, findings, reason, evidence, now):
    current = database.execute("SELECT * FROM application_jobs WHERE job_key=?", (job["job_key"],)).fetchone()
    if not current or current["state"] in applications.FINAL or outstanding(database, current):
        return False
    detail = dict(lifecycle, reason=reason, evidence=evidence, attempts=read_count(database, job["job_key"], lifecycle, findings),
                  blocker=lifecycle["blocker"], job_key=job["job_key"],
                  submission_status="NOT_SUBMITTED_BY_RECOVERY", next_action="Retain terminal abandonment and its evidence; do not automatically reopen")
    applications.hold(database, current, "APPLICATION_RECOVERY_ABANDONED", detail["next_action"], now, "ABANDONED")
    applications.event(database, job["job_key"], "APPLICATION_ABANDONED", detail, now)
    database.execute("DELETE FROM application_blockers WHERE job_key=?", (job["job_key"],))
    return True


def last_decision(database, key, since=None):
    decisions = records(database, key, "APPLICATION_RECOVERY_DECIDED", since)
    decision = decisions[-1]["decision"] if decisions else None
    return decision if isinstance(decision, dict) else None


def ai_abandoned(database, key, since=None):
    """True only when the AI coordinator's own last recorded decision was ABANDON."""
    decision = last_decision(database, key, since)
    return bool(decision) and decision.get("action") == "ABANDON"


def claims_exhausted(database, key, since=None):
    return bool(records(database, key, CLAIMS_EXHAUSTED_EVENT, since))


def hold_exhausted(database, job, lifecycle, findings, now, since=None):
    """Park a claim-exhausted lifecycle on the existing reviewable blocker.

    Grants no additional investigation claim and starts no new lifecycle: it only
    records the reason, the investigation evidence, the coordinator's last decision,
    the required next action, then holds the job BLOCKED so it surfaces in `application_blockers`
    and in consolidated reporting. Every protection `abandon` applies is applied
    here too: FINAL states and outstanding/uncertain submissions are refused.
    """
    current = database.execute("SELECT * FROM application_jobs WHERE job_key=?", (job["job_key"],)).fetchone()
    if not current or current["state"] in applications.FINAL or outstanding(database, current):
        return False
    detail = dict(lifecycle, reason="RECOVERY_CLAIMS_EXHAUSTED_PENDING_REVIEW",
                  evidence=findings, attempts=read_count(database, job["job_key"], lifecycle, findings),
                  blocker=lifecycle["blocker"], job_key=job["job_key"],
                  hold_blocker=CLAIMS_EXHAUSTED_BLOCKER, terminal=False,
                  last_decision=last_decision(database, job["job_key"], since),
                  automatic_recovery="EXHAUSTED: no further investigation claim will be granted for this job",
                  submission_status="NOT_SUBMITTED_BY_RECOVERY", next_action=CLAIMS_EXHAUSTED_ACTION)
    if not applications.hold(database, current, CLAIMS_EXHAUSTED_BLOCKER, CLAIMS_EXHAUSTED_ACTION, now,
                             "BLOCKED"):
        return False
    if not claims_exhausted(database, job["job_key"], since):
        applications.event(database, job["job_key"], CLAIMS_EXHAUSTED_EVENT, detail, now)
    return True


def hold_reviews(database, key, since=None):
    return records(database, key, HOLD_REVIEW_EVENT, since)


def resumption_active(database, key, job, blocker, now=None, since=None):
    """True only inside a recorded resumption: one ordinary attempt that has not concluded.

    A resumed job carries no blocker. The ordinary attempt may legitimately span cycles
    while the existing desktop mailbox services the read, so the codes in
    `RESUMPTION_WAITS` - which record an outstanding read rather than a decision - keep the
    window open. Anything else (a local gate, an inspection blocker, a preflight failure, a
    user hold, a final state) concludes it, and the recovery guard re-asserts the parked
    hold on the next pass. The window never closes on elapsed time; only the attempt budget
    and an explicit abandonment close it.
    """
    if job["state"] in applications.FINAL:
        return False
    if {code for code in (job["blocker"], blocker) if code} - RESUMPTION_WAITS:
        return False
    reviews = hold_reviews(database, key, since)
    if not reviews or reviews[-1].get("outcome") != "RESUMED":
        return False
    return True      # nothing time-based closes a resumption window


def bound_evidence(evidence, job, now):
    return (isinstance(evidence, dict) and applications.fresh(evidence, now)
            and evidence.get("independent_read") is True and evidence.get("job_key") == job["job_key"])


def apply_resolution(database, job, resolution, now):
    import external_history
    import identity_reconciliation
    if not isinstance(resolution, dict) or not resolution:
        raise ValueError("VERIFIED_RECOVERY_RESOLUTION_REQUIRED")
    if set(resolution) - {"identity", "history", "destination"}:
        raise ValueError("UNSUPPORTED_RECOVERY_RESOLUTION")
    if not resolution.get("identity") and not resolution.get("history") and not resolution.get("destination"):
        raise ValueError("EMPTY_RECOVERY_RESOLUTION")
    database.execute("SAVEPOINT application_recovery_resolution")
    try:
        identity = resolution.get("identity")
        if identity:
            if not bound_evidence(identity.get("evidence"), job, now):
                raise ValueError("RECOVERY_IDENTITY_EVIDENCE_REQUIRED")
            if identity["evidence"].get("candidate_email", job["candidate_email"]) != job["candidate_email"]:
                raise ValueError("RECOVERY_CANDIDATE_MISMATCH")
            identity_reconciliation.reconcile_requisition(database, job["job_key"], identity["requisition"], identity["evidence"], now)
        for history in resolution.get("history", []):
            if not bound_evidence(history.get("evidence"), job, now):
                raise ValueError("RECOVERY_HISTORY_EVIDENCE_REQUIRED")
            external_history.resolve(database, history["evidence_id"], job["job_key"], history["relationship"], history["evidence"], now)
        destination = resolution.get("destination")
        if destination:
            if not bound_evidence(destination.get("evidence"), job, now):
                raise ValueError("RECOVERY_DESTINATION_EVIDENCE_REQUIRED")
            if destination["evidence"].get("candidate_email") != job["candidate_email"]:
                raise ValueError("RECOVERY_CANDIDATE_MISMATCH")
            identity_reconciliation.reconcile_destination(database, job["job_key"], destination["url"], destination["evidence"], now)
        database.execute("RELEASE SAVEPOINT application_recovery_resolution")
    except Exception:
        database.execute("ROLLBACK TO SAVEPOINT application_recovery_resolution")
        database.execute("RELEASE SAVEPOINT application_recovery_resolution")
        raise


# A RELEASE hands the job back to the ordinary application path. `review_hold` has had exactly
# this move since it was written - its RETRY "resume this job through the ordinary application
# path ... NOT dispatched as a new investigation claim" - but only AFTER every investigation
# claim is spent and the job is parked. The main loop had no such option, so a job whose facts
# were already settled and whose only problem was a transient read failure could be answered
# only with an investigation that had nothing to resolve, or with abandonment. Tyrell AI
# (job-boards.greenhouse.io/tyrellai/jobs/9000000010) was abandoned that way on 2026-09-24 after
# ONE claim: a TimeoutError on inspect, one investigation that failed validation with
# APPLICATION_UNOBSERVED_QUOTE, then "missing_fields is empty; the other supported methods offer
# no concrete different investigation addressing this failure". Umbrella was lost the same way.
#
# Bounded because an ordinary attempt can fail transiently again and open a fresh lifecycle: the
# count is over ALL releases ever recorded for the job, never scoped to the current lifecycle, so
# a supersession or an authorized reopen does not hand back a fresh set.
RELEASE_EVENT = "APPLICATION_RECOVERY_RELEASED"
RELEASE_LIMIT = 3
RELEASE_ACTION = ("Resume the ordinary application path; the recovery coordinator judged the failure "
                  "transient and this job's own facts already settled")


def releases_spent(database, key):
    return len(records(database, key, RELEASE_EVENT))


def release(database, job, lifecycle, findings, decision, now):
    """Return the job to the ordinary path. Asserts nothing; changes no gate."""
    key = job["job_key"]
    resumed = database.execute("""UPDATE application_jobs SET state='QUEUED',blocker=NULL,next_action=?,next_at=NULL,updated_at=?
        WHERE job_key=? AND state NOT IN ('SUBMITTED','DUPLICATE','INELIGIBLE','EXHAUSTED','ABANDONED')""",
        (RELEASE_ACTION, now, key))
    if resumed.rowcount != 1:
        return False
    database.execute("DELETE FROM application_blockers WHERE job_key=?", (key,))
    applications.event(database, key, RELEASE_EVENT, {
        "job_key": key, "blocker": lifecycle["blocker"] if lifecycle else job["blocker"],
        "decision": decision, "reason": decision["reason"],
        "releases_spent": releases_spent(database, key) + 1, "release_limit": RELEASE_LIMIT,
        "findings": [finding_context(item) for item in findings],
        "next_action": RELEASE_ACTION, "terminal": False,
        "submission_status": "NOT_SUBMITTED_BY_RECOVERY",
        "gates_unchanged": True}, now)
    # The lifecycle ends here: without this `active()` stays true and the next cycle pulls the
    # job straight back into recovery instead of letting the ordinary path run.
    applications.event(database, key, SUPERSEDED_EVENT, {
        "job_key": key, "old_blocker": lifecycle["blocker"] if lifecycle else job["blocker"],
        "reason": "RECOVERY_RELEASED_TO_ORDINARY_PATH", "outcome": "RETURNED_TO_NORMAL_PROCESSING",
        "prior_records_preserved": True}, now)
    return True


def established(job):
    """What this job ALREADY knows durably, so no claim is spent rediscovering it.

    Owner, 2026-09-22: information must be reused whenever it is not stale or expired. The
    coordinator reasons over `previous_findings`, never over the job row, so a value that a
    previous attempt resolved AND had applied to the payload was invisible to it. Lucerne Energy
    spent claim 3 re-investigating a destination claim 2 had already written to
    `payload["url"]`. These are the applied, validated values - not a report to be verified.
    """
    try:
        payload = json.loads(job["payload"]) if isinstance(job["payload"], str) else (job["payload"] or {})
    except (TypeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    requisition = job["requisition"]
    return {"destination": payload.get("url"), "role": payload.get("role"),
            "employer": job["employer"], "employer_renderings": payload.get("employer_names") or [],
            "requisition": None if applications.weak_identity(requisition) else requisition,
            "basis": "APPLIED_AND_VALIDATED_ON_THE_JOB_RECORD",
            "use": "Treat these as settled. Do not spend a claim re-reading them; investigate only "
                   "what is missing or contradicted."}


def finding_context(finding):
    """What the coordinator is told about one investigation claim.

    `error_code` and `resolution_verified` exist because patch F stopped
    `application_browser.consume()` nulling a failed investigation's findings: the
    coordinator now sees the requisition/destination/history the browser actually
    reported, so it must also see (a) the recoverable code the read returned and
    (b) that the reported resolution was REJECTED by validation and never applied.
    Without `resolution_verified` a real requisition would sit next to
    `status: "UNRESOLVED"` with no explicit signal that it is unverified.
    `reported_investigation` carries F's preserved raw report, bounded to 64 KiB by
    `application_browser.reported_investigation`.
    """
    result = finding.get("result", {})
    resolution = result.get("resolution") or {}
    error = result.get("error") if isinstance(result.get("error"), dict) else {}
    return {"attempt": finding["attempt"], "at": finding["at"],
            "actual_reads": finding.get("actual_reads", 1), "supported_alternative": finding["supported_alternative"],
            "reason": finding["reason"], "status": result.get("status"),
            "error_code": failure_code(error.get("code")),
            "resolution_verified": result.get("resolution_verified"),
            "reported_investigation": result.get("reported_investigation"),
            "next_action": result.get("next_action"), "next_alternative": result.get("next_alternative"),
            "source": result.get("source"), "browser_trace": result.get("browser_trace"),
            # `destination` and the identity EVIDENCE were dropped here (owner, 2026-09-22).
            # The full resolution is recorded verbatim on APPLICATION_RECOVERY_INVESTIGATED,
            # but this projection forwarded only the bare requisition string, so the next
            # coordinator packet held no record that an earlier attempt had already read and
            # reported this destination and employer - and it authorised another read for
            # them. That is how lucerne energy and mooby each resolved the SAME employer-name
            # question four times. It is worst precisely on the destination-class blockers,
            # where `destination` IS the missing field being recovered. Carried under the
            # existing `resolution_verified` flag, which already marks a reported value as
            # unverified: this surfaces what was seen, it never applies it.
            "blocked": result.get("blocked"), "resolution": {
                "identity": (resolution.get("identity") or {}).get("requisition"),
                "identity_evidence": (resolution.get("identity") or {}).get("evidence"),
                "destination": (resolution.get("destination") or {}).get("url"),
                "destination_evidence": (resolution.get("destination") or {}).get("evidence"),
                "history": [{"evidence_id": entry.get("evidence_id"), "relationship": entry.get("relationship")}
                            for entry in resolution.get("history", [])]}}


def wait(database, job, pending, lifecycle, now):
    # Recovery has no time window (owner, 2026-09-24 / 2026-09-28), so the adapter's own wait
    # time is the whole answer.
    applications.wait_for_adapter(database, job, pending, now, "BLOCKED")


def recover(database, job, adapter, config, blocker, action, now, clock=None, policy=None):
    limits = settings(config)
    if (not limits or job["state"] in applications.FINAL or user_hold(job["blocker"])
            or user_hold(blocker) or blocker in applications.ACCESS_BLOCK_CODES or outstanding(database, job)):
        return False
    key = job["job_key"]
    # An explicit, expiring, job-scoped user reopen authorization puts a NEW bounded
    # lifecycle in scope, and so does a supersession (below). Outside both `since` is None
    # and every read below is byte-identical to the unscoped one.
    since = superseded_scope(database, key, now)
    starts = scoped(database, key, "APPLICATION_RECOVERY_STARTED", since)
    # The single choke point: every recovery step of every lifecycle passes here first. The
    # blocker being recovered (the lifecycle's own, or the one a new lifecycle would start
    # on) is re-evaluated with the current gates before anything is decided, read, held
    # exhausted or abandoned.
    if supersede_stale(database, job, starts[0]["blocker"] if starts else blocker, starts[0] if starts else None, now, policy):
        return True
    if starts and job["blocker"] == ORCHESTRATOR_DEFECT_BLOCKER and defect_persists(database, key, adapter, since):
        return True
    if starts and job["blocker"] == COORDINATOR_CAP_BLOCKER and coordinator_capped(adapter, key, now):
        return True
    if not starts and blocker not in RECOVERABLE:
        return False
    if not starts and applications.coordinator_suppression(database, job, blocker, now):
        # Part C1: no supported investigation can supply what is missing (or every one that
        # could was already spent on this unchanged state). Start no lifecycle and make no
        # coordinator call; the ordinary path decides whether a fresh read is warranted.
        return False
    if starts and claims_exhausted(database, key, since):
        # The reviewable hold is a terminal state for automatic recovery. Re-entering
        # would hand out a fourth claim, so re-assert the hold and stop; the caller
        # returns without inspecting, submitting or consuming any budget.
        # The single exception is an unblocked job inside a recorded AI hold review
        # resumption: it owes the ordinary path one ordinary attempt, and the hold is
        # re-asserted again the moment anything blocks it.
        if resumption_active(database, key, job, blocker, now, since):
            return False
        with database:
            hold_exhausted(database, job, starts[0], scoped(database, key, "APPLICATION_RECOVERY_INVESTIGATED", since), now, since)
        return True
    if starts:
        prepared_next = scoped(database, key, "APPLICATION_RECOVERY_PREPARED", since)
        findings_so_far = scoped(database, key, "APPLICATION_RECOVERY_INVESTIGATED", since)
        pending_packet = any(item["recovery"]["attempt"] == len(findings_so_far) + 1 for item in prepared_next)
        suppressed = None if pending_packet else applications.coordinator_suppression(
            database, job, blocker if blocker in RECOVERABLE else starts[0]["blocker"], now)
        # Suppression exists to stop repeating an identical investigation while the job waits
        # for new input; it must not advance a live lifecycle towards abandonment.
        if suppressed:
            # Part C1 inside an existing lifecycle: nothing a supported method can supply is
            # missing (or every capable method was spent on this unchanged state). Neither the
            # coordinator nor a read is scheduled, and the lifecycle is not advanced towards
            # abandonment on a question investigation cannot answer; the job waits on the
            # ordinary path for new input. Claims are unchanged.
            with database:
                latest = database.execute("SELECT detail FROM application_events WHERE job_key=? AND event=? ORDER BY id DESC LIMIT 1",
                                          (key, applications.COORDINATOR_SUPPRESSED_EVENT)).fetchone()
                if not latest or json.loads(latest[0]).get("input_sha256") != suppressed["input_sha256"]:
                    applications.event(database, key, applications.COORDINATOR_SUPPRESSED_EVENT,
                                       dict(suppressed, attempt=len(findings_so_far) + 1), now)
            return False
    with database:
        if not starts:
            lifecycle = dict(blocker=blocker, started_at=now,
                             max_attempts=limits["max_attempts"])
            applications.event(database, key, "APPLICATION_RECOVERY_STARTED", lifecycle, now)
        else:
            lifecycle = starts[0]
    findings = scoped(database, key, "APPLICATION_RECOVERY_INVESTIGATED", since)
    blocker = blocker if blocker in RECOVERABLE else lifecycle["blocker"]
    prepared = scoped(database, key, "APPLICATION_RECOVERY_PREPARED", since)
    packet = next((item for item in prepared if item["recovery"]["attempt"] == len(findings) + 1), None)
    completed_reads = counted_reads(findings)
    if completed_reads >= lifecycle["max_attempts"] or (packet is None and read_count(database, key, lifecycle, findings) >= lifecycle["max_attempts"]):
        with database:
            if not ai_abandoned(database, key, since):
                hold_exhausted(database, job, lifecycle, findings, now, since)
            else:
                abandon(database, job, lifecycle, findings, "RECOVERY_ATTEMPTS_EXHAUSTED", findings, now)
        return True
    if packet is None:
        untried, missing = applications.untried_capable_methods(database, job, blocker, now)
        used = {(item["supported_alternative"]["method"], item["supported_alternative"]["instructions"]) for item in findings}
        context = [finding_context(item) for item in findings]
        decisions = scoped(database, key, "APPLICATION_RECOVERY_DECIDED", since)
        execution_failures = previous_execution_failures(database, key, getattr(adapter, "root", None))
        packet = {"job": dict(job), "read_only": True, "already_established": established(job),
                  "supported_methods": METHODS, "previous_findings": context, "previous_decisions": decisions,
                  "previous_execution_failures": execution_failures,
                  "original_blocker": lifecycle["blocker"], 
                  "recovery": dict(lifecycle, attempt=len(findings) + 1, blocker=blocker,
                                   previous_failure=context[-1] if context else {"reason": blocker, "next_action": action},
                                   supported_methods=METHODS, previous_attempts=context, previous_decisions=decisions,
                                   previous_execution_failures=execution_failures,
                                   coordinator_failures=scoped(database, key, "APPLICATION_RECOVERY_DECISION_FAILED", since))}
        # Structured, Python-computed guidance: which fields are missing and which methods can
        # still supply one of them in this input state. Wording is never compared.
        packet["missing_fields"] = missing
        packet["method_supplies"] = {method: sorted(fields) for method, fields in applications.METHOD_SUPPLIES.items()}
        packet["permitted_methods"] = sorted(untried) if untried is not None else sorted(METHODS)
        # Stated so an exhausted RELEASE is a contract violation the coordinator can see and
        # avoid, rather than a refusal it walks into and gets charged a failure for.
        packet["release"] = {"spent": releases_spent(database, key), "limit": RELEASE_LIMIT,
                             "permitted": releases_spent(database, key) < RELEASE_LIMIT,
                             "meaning": RELEASE_ACTION}
        saved = next((entry for entry in decisions if entry["attempt"] == len(findings) + 1), None)
        try:
            decision = saved["decision"] if saved else adapter.recovery_decision(packet)
            if clock:
                now = clock()
            if (not isinstance(decision, dict) or decision.get("action") not in {"RETRY", "ABANDON", "RELEASE"}
                    or not isinstance(decision.get("reason"), str) or not decision["reason"].strip()
                    or type(decision.get("progress")) is not bool):
                raise ValueError("APPLICATION_RECOVERY_DECISION_INVALID")
            if decision["action"] == "RELEASE" and (decision.get("method") or decision.get("instructions")):
                raise ValueError("APPLICATION_RECOVERY_RELEASE_CANNOT_DISPATCH")
            if decision["action"] == "RETRY" and (decision.get("method") not in METHODS
                    or not isinstance(decision.get("instructions"), str) or not decision["instructions"].strip()
                    or (decision["method"], decision["instructions"]) in used
                    or untried is not None and decision["method"] not in untried):
                # The last clause is Part C1: a method that cannot supply any missing field,
                # or was already spent on this unchanged state, is refused whatever its
                # instructions say (rewording no longer buys another identical read).
                #
                #
                # Investigated twice and NOT a defect (owner, 2026-09-22). For the 12 RECOVERABLE
                # codes with no BLOCKER_FIELDS entry `untried` is None, so only exact equality on
                # (method, instructions) stops a repeat and a reworded instruction buys another
                # read. Refusing on the method looked like the fix; it was tried unconditionally
                # (43 test failures) and then gated on `untried is None` (42), and both times the
                # rehearsals showed why: re-running a method with REFINED instructions across
                # attempts is the design of this loop, not an accident of it. The real waste was
                # that the coordinator could not see what earlier attempts had already
                # established, which `established()` now puts in the packet.
                raise ValueError("APPLICATION_RECOVERY_SUPPORTED_ALTERNATIVE_REQUIRED")
        except applications.AdapterPending as pending:
            wait(database, job, pending, lifecycle, now)
            return True
        except Exception as error:
            failure = exception_failure(error)
            if failure["code"] == COORDINATOR_CAP_CODE:
                with database:
                    hold_coordinator_cap(database, job, lifecycle, len(findings) + 1, now)
                return True
            with database:
                applications.event(database, key, "APPLICATION_RECOVERY_DECISION_FAILED", {
                    "attempt": len(findings) + 1, "reason": failure["code"] or failure["exception_type"], "exception": failure,
                    "artifacts": getattr(error, "artifacts", None)}, now)
                failures = counted_failures(database, key, "APPLICATION_RECOVERY_DECISION_FAILED", since)
                if len(failures) >= 2:
                    abandon(database, job, lifecycle, findings, "RECOVERY_COORDINATOR_FAILED", failures, now)
                else:
                    next_at = (dt.datetime.fromisoformat(now) + dt.timedelta(minutes=15)).isoformat()
                    applications.hold(database, job, blocker, "Obtain a valid AI recovery directive; do not select a fallback automatically", now, "BLOCKED", next_at)
            return True
        with database:
            if not saved:
                applications.event(database, key, "APPLICATION_RECOVERY_DECIDED", {"attempt": len(findings) + 1, "decision": decision}, now)
            if decision["action"] == "ABANDON":
                abandon(database, job, lifecycle, findings, decision["reason"],
                        {"decision": decision, "findings": findings}, now)
                return True
            if decision["action"] == "RELEASE":
                # Out of budget is NOT a coordinator failure. Raising here recorded an
                # APPLICATION_RECOVERY_DECISION_FAILED, and two of those abandon the job - so a
                # coordinator making a reasonable but unaffordable choice twice would destroy a
                # live role, which is the exact defect this whole change exists to remove. The
                # job waits and is asked again instead; the packet states the remaining budget.
                if releases_spent(database, key) >= RELEASE_LIMIT:
                    next_at = (dt.datetime.fromisoformat(now) + dt.timedelta(minutes=15)).isoformat()
                    applications.event(database, key, "APPLICATION_RECOVERY_RELEASE_REFUSED", {
                        "job_key": key, "decision": decision, "releases_spent": releases_spent(database, key),
                        "release_limit": RELEASE_LIMIT, "terminal": False,
                        "next_action": "Investigate or abandon; the ordinary path has had its releases"}, now)
                    applications.hold(database, job, blocker,
                                      "Investigate or abandon; the ordinary path has had its releases",
                                      now, "BLOCKED", next_at)
                    return True
                release(database, job, lifecycle, findings, decision, now)
                return True
        alternative = {"method": decision["method"], "instructions": decision["instructions"]}
        packet["recovery"]["supported_alternative"] = alternative
        packet["recovery"]["decision"] = decision
        with database:
            applications.hold(database, job, blocker, alternative["instructions"], now)
            packet["job"] = dict(database.execute("SELECT * FROM application_jobs WHERE job_key=?", (key,)).fetchone(),
                                 candidate_email=job["candidate_email"])
            applications.event(database, key, "APPLICATION_RECOVERY_PREPARED", packet, now)
    try:
        result = adapter.call("investigate", packet)
    except applications.AdapterPending as pending:
        wait(database, job, pending, lifecycle, now)
        return True
    except applications.AdapterUnavailable as error:
        with database:
            applications.hold(database, job, str(error), "Restore the supported read-only investigation adapter", now)
        return True
    except Exception as error:
        failure = exception_failure(error)
        result = {"status": "FAILED", "reason": failure["code"] or failure["exception_type"], "exception": failure}
    if clock:
        now = clock()
    blocked = result.get("blocked") if isinstance(result, dict) else None
    if (isinstance(blocked, dict) and blocked.get("code") in ORCHESTRATOR_DEFECT_CODES
            and actual_reads(database, key, packet, adapter) == 0):
        with database:
            applications.event(database, key, ORCHESTRATOR_DEFECT_EVENT, {
                "attempt": packet["recovery"]["attempt"], "code": blocked["code"], "actual_reads": 0,
                "supported_alternative": packet["recovery"]["supported_alternative"],
                "counts_as_investigation": False}, now)
            current = database.execute("SELECT * FROM application_jobs WHERE job_key=?", (key,)).fetchone()
            if current and current["state"] not in applications.FINAL and not outstanding(database, current):
                applications.hold(database, current, ORCHESTRATOR_DEFECT_BLOCKER, ORCHESTRATOR_DEFECT_ACTION, now)
        return True
    if not isinstance(result, dict):
        result = {"status": "FAILED", "reason": "MALFORMED_INVESTIGATION_RESULT"}
    finding = {"attempt": packet["recovery"]["attempt"], "at": now,
               "actual_reads": actual_reads(database, key, packet, adapter),
               "supported_alternative": packet["recovery"]["supported_alternative"],
               "result": result, "reason": result.get("reason") or "Investigation returned no verified resolution",
               "next_alternative": result.get("next_alternative")}
    with database:
        applications.event(database, key, "APPLICATION_RECOVERY_INVESTIGATED", finding, now)
        findings.append(finding)
        # Only a read that completed and still found nothing is an inconclusive result. An
        # execution failure (exception, consumer error) is bounded by the claim limit instead.
        if (result.get("status") in {"UNRESOLVED", "ABANDON"} and not result.get("error")
                and packet["recovery"]["supported_alternative"].get("method") in METHODS):
            untried, missing = applications.untried_capable_methods(database, job, blocker, now)
            if missing:
                applications.event(database, key, applications.INCONCLUSIVE_EVENT, {
                    "method": packet["recovery"]["supported_alternative"]["method"], "blocker": blocker,
                    "missing_fields": missing, "input_sha256": applications.input_fingerprint(database, job),
                    "identity_sha256": applications.identity_fingerprint(job),
                    "contract_revision": applications.READ_CONTRACT_REVISION, "attempt": finding["attempt"]}, now)
        current = database.execute("SELECT * FROM application_jobs WHERE job_key=?", (key,)).fetchone()
        if not current or current["state"] in applications.FINAL or outstanding(database, current):
            return True
        blocked = result.get("blocked")
        if isinstance(blocked, dict) and user_hold(blocked.get("code")):
            applications.hold(database, current, blocked["code"], blocked["next_action"], now)
            return True
        if isinstance(blocked, dict) and blocked.get("code") in applications.ACCESS_BLOCK_CODES:
            applications.hold_access_block(database, current, blocked["code"], blocked["next_action"], now)
            return True
        # Patch F threads the code the browser actually returned into `result["error"]`.
        # A code that is in RECOVERABLE is actionable, not "no progress": adopt it as the
        # blocker `applications.hold` records below, so the next cycle re-enters recovery
        # on the real failure instead of the stale original blocker. `lifecycle["blocker"]`
        # is untouched, so abandonment evidence still names the original blocker;
        # RECOVERABLE and USER_HOLDS are disjoint, so a user hold can never be replaced.
        returned = failure_code((result.get("error") or {}).get("code"))
        if returned in RECOVERABLE:
            blocker = returned
        if result.get("status") == "RESOLVED":
            try:
                if not bound_evidence(result.get("evidence"), job, now):
                    raise ValueError("RECOVERY_RESULT_EVIDENCE_REQUIRED")
                apply_resolution(database, job, result.get("resolution"), now)
            except (ValueError, KeyError, TypeError, AttributeError) as error:
                finding["reason"] = "UNVERIFIED_RESOLUTION: " + str(error)
                applications.event(database, key, "APPLICATION_RECOVERY_RESOLUTION_REJECTED", finding, now)
                untried, missing = applications.untried_capable_methods(database, job, blocker, now)
                if missing and packet["recovery"]["supported_alternative"].get("method") in METHODS:
                    applications.event(database, key, applications.INCONCLUSIVE_EVENT, {
                        "method": packet["recovery"]["supported_alternative"]["method"], "blocker": blocker,
                        "missing_fields": missing, "input_sha256": applications.input_fingerprint(database, job),
                        "identity_sha256": applications.identity_fingerprint(job),
                        "contract_revision": applications.READ_CONTRACT_REVISION, "attempt": finding["attempt"]}, now)
            else:
                database.execute("UPDATE application_jobs SET state='QUEUED',blocker=NULL,next_action=NULL,next_at=NULL,updated_at=? WHERE job_key=?", (now, key))
                database.execute("DELETE FROM application_blockers WHERE job_key=?", (key,))
                applications.event(database, key, "APPLICATION_RECOVERY_RESOLVED", finding, now)
                return False
        if read_count(database, key, lifecycle, findings) >= lifecycle["max_attempts"]:
            if not ai_abandoned(database, key, since):
                hold_exhausted(database, current, lifecycle, findings, now, since)
            else:
                abandon(database, current, lifecycle, findings,
                        "RECOVERY_ATTEMPTS_EXHAUSTED", findings, now)
        else:
            next_at = (dt.datetime.fromisoformat(now) + dt.timedelta(minutes=15)).isoformat()
            applications.hold(database, current, blocker, "Return findings to the AI orchestrator for a supported next action or abandonment", now, "BLOCKED", next_at)
    return True
