"""Isolated validation for the generic incomplete-inspection recovery path.

`INSPECTION_INCOMPLETE` is a real browser blocker code that root did not recognize.
It is admitted here through the existing allowlist (`application_recovery.RECOVERABLE`),
and an incomplete read is re-read through the existing bounded inspection path before
the recovery lifecycle is ever started, so abandonment can only follow genuine
bounded retries. Nothing outside the allowlist is admitted, so a future AI-authored
user-dependent code still holds instead of being abandoned.
"""

import contextlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from unittest.mock import patch

import application_connection
import application_recovery
import applications
import desktop_adapter
import workflow_tasks
from manual_application_browser import Fixture
from manual_partial_review_recovery import (NOW, complete, cycle, forbidden, inspections,
                                            offset, records, reread_phase)


CODE = "INSPECTION_INCOMPLETE"
SECOND_CODE = "UNRECOGNIZED_SYNTHETIC_READ_FAILURE"
CANONICAL = "https://jobs.ashbyhq.com/synthetic/role-123-canonical"
RESULT = {"blocked": {"code": CODE, "next_action": "Complete read-only inspection"}}


class RecoveryAdapter(desktop_adapter.DesktopAdapter):
    def __init__(self, fixture):
        self.now = NOW
        self.code = CODE
        self.decisions = []
        self.operations = []
        super().__init__(fixture.database, fixture.root, fixture.config, lambda: self.now)

    def recovery_decision(self, packet):
        self.decisions.append(packet)
        assert packet["read_only"] and packet["original_blocker"] == self.code
        return {"action": "RETRY", "method": "official_posting", "progress": False,
                "instructions": "Synthetic bounded read " + str(packet["recovery"]["attempt"]),
                "reason": "Inspection remains incomplete"}

    def call(self, operation, payload):
        assert operation in {"inspect", "investigate", "reconcile"}, operation
        self.operations.append(operation)
        return super().call(operation, payload)


def seed(fixture, code=CODE, held=False):
    fixture.adapter.code = code
    cycle(fixture)
    result = {"blocked": {"code": code, "next_action": "Complete read-only inspection"}}
    request_id = complete(fixture, "inspect", result)
    if held:
        with fixture.database:
            applications.hold(fixture.database, fixture.job(), code, result["blocked"]["next_action"], NOW)
    return request_id


def result_for(code):
    return {"blocked": {"code": code, "next_action": "Complete read-only inspection"}}


def bound_evidence(fixture, seconds):
    return {"source": "application-browser:synthetic-investigation",
            "observed_at": desktop_adapter.later(NOW, seconds), "verified": True,
            "independent_read": True, "job_key": fixture.key}


def destination_evidence(fixture, seconds, url=CANONICAL):
    return dict(bound_evidence(fixture, seconds),
                official_destination_verified=True, identity_basis="official_canonical_posting_id",
                employer="synthetic company", role="Staff Software Engineer",
                candidate_email="candidate@example.invalid", requisition="role-123",
                official_url=url,
                browser_evidence=[{"pointer": "/resolution/destination/evidence/official_url",
                                   "tool_call_id": "synthetic-role", "quote": url}])


def bounded(fixture, code=CODE, held=False):
    """Bounded fresh re-reads, then bounded recovery, then justified abandonment."""
    assert code in application_recovery.RECOVERABLE
    request_id = seed(fixture, code, held)
    original = dict(fixture.database.execute("SELECT * FROM desktop_requests WHERE request_id=?", (request_id,)).fetchone())
    original_job = fixture.job()
    base = reread_phase(fixture, result_for(code))
    assert inspections(fixture) == 3
    for attempt in range(3):
        seconds = base + attempt * 1000
        cycle(fixture, seconds)
        assert len(fixture.adapter.decisions) == attempt + 1, fixture.job()
        complete(fixture, "investigate", {"status": "FAILED", "reason": "Synthetic unresolved read"})
        cycle(fixture, seconds + 60)
        if attempt < 2:
            count = len(fixture.adapter.operations)
            cycle(fixture, seconds + 61)
            assert len(fixture.adapter.operations) == count
            fixture.database.close()
            fixture.database = sqlite3.connect(fixture.root / "state/orchestrator.sqlite3")
            fixture.database.row_factory = sqlite3.Row
            fixture.adapter.database = fixture.database
    assert_reviewable_hold(fixture, base, 3)
    assert len(records(fixture, "APPLICATION_RECOVERY_STARTED")) == 1
    assert len(records(fixture, "INSPECTION_REREAD_SCHEDULED")) == 2
    assert inspections(fixture) == 3
    assert fixture.database.execute("SELECT sum(claims) FROM desktop_requests WHERE operation='investigate'").fetchone()[0] == 3
    assert dict(fixture.database.execute("SELECT * FROM desktop_requests WHERE request_id=?", (request_id,)).fetchone()) == original
    assert fixture.job()["payload"] == original_job["payload"]
    assert fixture.job()["attempts"] == 0 and fixture.job()["receipt"] is None
    with fixture.database:
        assert applications.ingest(fixture.database, json.loads(original_job["payload"])) == fixture.key
    count = len(fixture.adapter.operations)
    cycle(fixture, 200000)
    applications.process(fixture.database, original_job, fixture.adapter, fixture.policy,
                         fixture.root, fixture.adapter.now, config=fixture.config)
    cycle(fixture, 300000)
    assert len(fixture.adapter.operations) == count and fixture.job()["state"] == "BLOCKED"
    assert fixture.job()["blocker"] == application_recovery.CLAIMS_EXHAUSTED_BLOCKER
    assert not records(fixture, "APPLICATION_ABANDONED")
    assert len(records(fixture, application_recovery.CLAIMS_EXHAUSTED_EVENT)) == 1
    assert len(records(fixture, "APPLICATION_RECOVERY_STARTED")) == 1
    assert len(records(fixture, "APPLICATION_RECOVERY_INVESTIGATED")) == 3
    assert fixture.database.execute("SELECT sum(claims) FROM desktop_requests WHERE operation='investigate'").fetchone()[0] == 3


def assert_reviewable_hold(fixture, base, attempts, method="RETRY"):
    """Bound exhaustion without an AI ABANDON is a reviewable hold, never a terminal record."""
    job = fixture.job()
    assert job["state"] == "BLOCKED" and job["blocker"] == application_recovery.CLAIMS_EXHAUSTED_BLOCKER, job
    assert not records(fixture, "APPLICATION_ABANDONED")
    held = records(fixture, application_recovery.CLAIMS_EXHAUSTED_EVENT)
    assert len(held) == 1
    detail = held[0]
    assert detail["reason"] == "RECOVERY_CLAIMS_EXHAUSTED_PENDING_REVIEW" and detail["attempts"] == attempts
    assert detail["submission_status"] == "NOT_SUBMITTED_BY_RECOVERY" and detail["terminal"] is False
    assert detail["last_decision"]["action"] == method
    assert len(detail["evidence"]) == attempts and detail["next_action"] == application_recovery.CLAIMS_EXHAUSTED_ACTION
    row = dict(fixture.database.execute("SELECT * FROM application_blockers WHERE job_key=?", (fixture.key,)).fetchone())
    assert row["code"] == application_recovery.CLAIMS_EXHAUSTED_BLOCKER and row["owner"] == "orchestrator"
    assert row["required_input"] == application_recovery.CLAIMS_EXHAUSTED_ACTION
    report = applications.report(fixture.database, fixture.adapter.now)
    assert [entry["code"] for entry in report["blockers"] if entry["job_key"] == fixture.key] == [application_recovery.CLAIMS_EXHAUSTED_BLOCKER]
    return detail


def instrumented(fixture):
    """Headline measurement: partial form/eligibility read at an already-correct destination."""
    seed(fixture, held=True)
    base = reread_phase(fixture, RESULT)
    for attempt in range(3):
        seconds = base + attempt * 1000
        cycle(fixture, seconds)
        complete(fixture, "investigate", {"status": "FAILED", "reason": "Form facts still unknown"})
        cycle(fixture, seconds + 60)
    measurement = {
        "inspect_requests": inspections(fixture),
        "investigate_claims": fixture.database.execute("SELECT COALESCE(sum(claims),0) FROM desktop_requests WHERE operation='investigate'").fetchone()[0],
        "inspection_reread_scheduled": len(records(fixture, "INSPECTION_REREAD_SCHEDULED")),
        "recovery_started": len(records(fixture, "APPLICATION_RECOVERY_STARTED")),
        "job_attempts": fixture.job()["attempts"],
        "abandoned": len(records(fixture, "APPLICATION_ABANDONED")),
        "claims_exhausted_holds": len(records(fixture, application_recovery.CLAIMS_EXHAUSTED_EVENT)),
        "final_state": fixture.job()["state"],
        "final_blocker": fixture.job()["blocker"]}
    assert measurement == {"inspect_requests": 3, "investigate_claims": 3, "inspection_reread_scheduled": 2,
                           "recovery_started": 1, "job_attempts": 0, "abandoned": 0, "claims_exhausted_holds": 1,
                           "final_state": "BLOCKED",
                           "final_blocker": "APPLICATION_BROWSER_INVESTIGATION_EXHAUSTED"}, measurement
    assert_reviewable_hold(fixture, base, 3)
    return measurement


def outside_allowlist(fixture):
    """A code outside the allowlist still holds; it is never abandoned (review finding F5)."""
    assert SECOND_CODE not in application_recovery.RECOVERABLE
    assert not application_recovery.user_hold(SECOND_CODE)
    seed(fixture, SECOND_CODE, held=True)
    cycle(fixture, 60)
    cycle(fixture, 120)
    job = fixture.job()
    assert job["state"] == "BLOCKED" and job["blocker"] == SECOND_CODE and job["attempts"] == 0
    assert not fixture.adapter.decisions
    assert not records(fixture, "INSPECTION_REREAD_SCHEDULED")
    assert not records(fixture, "APPLICATION_RECOVERY_STARTED")
    assert not records(fixture, "APPLICATION_ABANDONED")
    assert not application_recovery.recover(fixture.database, fixture.job(), fixture.adapter,
        fixture.config, SECOND_CODE, "Unrecognized saved blocker", fixture.adapter.now)


def resolved(fixture, hard_gate=False):
    """A verified destination resolution actually advances the job (review finding F4)."""
    seed(fixture, held=True)
    base = reread_phase(fixture, RESULT)
    cycle(fixture, base)
    assert len(fixture.adapter.decisions) == 1
    started = records(fixture, "APPLICATION_RECOVERY_STARTED")
    assert len(started) == 1
    previous = json.loads(fixture.job()["payload"])["url"]
    complete(fixture, "investigate", {
        "status": "RESOLVED",
        "resolution": {"destination": {"url": CANONICAL, "evidence": destination_evidence(fixture, base)}},
        "evidence": bound_evidence(fixture, base)})
    cycle(fixture, base + 60)
    assert len(records(fixture, "APPLICATION_RECOVERY_RESOLVED")) == 1
    assert not records(fixture, "APPLICATION_RECOVERY_RESOLUTION_REJECTED")
    reconciled = records(fixture, "DESTINATION_RECONCILED")
    assert len(reconciled) == 1 and reconciled[0]["previous_url"] == previous and reconciled[0]["url"] == CANONICAL
    assert json.loads(fixture.job()["payload"])["url"] == CANONICAL
    assert fixture.job()["attempts"] == 0
    fresh = fixture.database.execute("""SELECT payload FROM desktop_requests WHERE operation='inspect'
        ORDER BY rowid DESC LIMIT 1""").fetchone()
    assert json.loads(fresh[0])["url"] == CANONICAL
    assert inspections(fixture) == 4
    assert records(fixture, "APPLICATION_RECOVERY_STARTED") == started
    if not hard_gate:
        complete(fixture, "inspect", RESULT)
        cycle(fixture, base + 120)
        assert records(fixture, "APPLICATION_RECOVERY_STARTED") == started
        assert len(records(fixture, "INSPECTION_REREAD_SCHEDULED")) == 2
        assert len(fixture.adapter.decisions) == 2
        assert fixture.job()["state"] != "ABANDONED"
        return
    with patch.object(applications, "stamp", return_value=fixture.adapter.now):
        review = fixture.review()
    review["facts"]["work_mode"] = "onsite"
    # The owner's onsite exclusion is the read's own verdict since 2026-10-07 (item 5).
    review.setdefault("scope_fit", {})["location"] = {"in_scope": False, "reason": "onsite in an owner-excluded place"}
    complete(fixture, "inspect", review)
    cycle(fixture, base + 120)
    assert fixture.job()["state"] == "INELIGIBLE" and fixture.job()["blocker"] == "OUTSIDE_CONFIRMED_SCOPE"
    assert not records(fixture, "APPLICATION_ABANDONED")


def empty_resolution(fixture):
    """An empty resolution can never record a false APPLICATION_RECOVERY_RESOLVED (finding F3)."""
    seed(fixture, held=True)
    base = reread_phase(fixture, RESULT)
    cycle(fixture, base)
    payload = fixture.job()["payload"]
    complete(fixture, "investigate", {"status": "RESOLVED", "resolution": {"history": []},
                                      "evidence": bound_evidence(fixture, base)})
    cycle(fixture, base + 60)
    rejected = records(fixture, "APPLICATION_RECOVERY_RESOLUTION_REJECTED")
    assert len(rejected) == 1 and rejected[0]["reason"] == "UNVERIFIED_RESOLUTION: EMPTY_RECOVERY_RESOLUTION"
    assert not records(fixture, "APPLICATION_RECOVERY_RESOLVED")
    assert not records(fixture, "DESTINATION_RECONCILED") and not records(fixture, "REQUISITION_RECONCILED")
    assert fixture.job()["payload"] == payload
    assert fixture.job()["state"] == "BLOCKED" and fixture.job()["blocker"] == CODE
    assert len(records(fixture, "APPLICATION_RECOVERY_STARTED")) == 1
    for resolution, reason in (({"history": []}, "EMPTY_RECOVERY_RESOLUTION"),
                               ({"identity": None, "history": [], "destination": None}, "EMPTY_RECOVERY_RESOLUTION"),
                               ({}, "VERIFIED_RECOVERY_RESOLUTION_REQUIRED")):
        try:
            application_recovery.apply_resolution(fixture.database, fixture.job(), resolution, fixture.adapter.now)
        except ValueError as error:
            assert str(error) == reason, (resolution, error)
        else:
            raise AssertionError("empty resolution must be rejected: " + json.dumps(resolution))




def protected(fixture, code):
    seed(fixture, code)
    cycle(fixture, 60)
    assert fixture.job()["blocker"] == code and fixture.job()["state"] == "BLOCKED"
    assert not fixture.adapter.decisions and not records(fixture, "APPLICATION_RECOVERY_STARTED")
    assert not records(fixture, "INSPECTION_REREAD_SCHEDULED")


def saved_hold(fixture, code):
    seed(fixture)
    with fixture.database:
        applications.hold(fixture.database, fixture.job(), code, "Preserve user hold", NOW)
    assert not applications.reread_inspection(fixture.database, fixture.job(), fixture.policy, CODE, schedule=True)
    cycle(fixture, 60)
    assert fixture.job()["blocker"] == code
    assert not fixture.adapter.decisions and not records(fixture, "APPLICATION_RECOVERY_STARTED")
    assert not records(fixture, "INSPECTION_REREAD_SCHEDULED")


def duplicate(fixture, kind):
    seed(fixture)
    with fixture.database:
        fixture.database.execute("INSERT INTO application_history VALUES (?,?,?,?)",
                                 (fixture.job()["employer"], fixture.job()["requisition"], kind, "{}"))
    cycle(fixture, 60)
    assert fixture.job()["state"] == "DUPLICATE" and fixture.adapter.operations == ["inspect"]
    assert not records(fixture, "APPLICATION_RECOVERY_STARTED")
    assert not records(fixture, "INSPECTION_REREAD_SCHEDULED")


def uncertain(fixture):
    seed(fixture)
    with fixture.database:
        fixture.database.execute("INSERT INTO application_attempts(attempt_id,job_key,idempotency_key,state,started_at,evidence) VALUES (?,?,?,'SUBMITTING',?,'{}')",
                                 ("synthetic-attempt", fixture.key, fixture.key, NOW))
    assert not applications.reread_inspection(fixture.database, fixture.job(), fixture.policy, CODE, schedule=True)
    cycle(fixture, 60)
    assert fixture.job()["state"] == "RECONCILE"
    assert fixture.database.execute("SELECT state FROM application_attempts").fetchone()[0] == "SUBMITTING"
    assert fixture.adapter.operations == ["inspect", "reconcile"]
    assert not fixture.adapter.decisions and not records(fixture, "APPLICATION_RECOVERY_STARTED")
    assert not records(fixture, "INSPECTION_REREAD_SCHEDULED")
    assert not records(fixture, "APPLICATION_ABANDONED")


def terminal(fixture, state):
    seed(fixture)
    stale = fixture.job()
    with fixture.database:
        fixture.database.execute("UPDATE application_jobs SET state=?", (state,))
    assert not applications.reread_inspection(fixture.database, fixture.job(), fixture.policy, CODE, schedule=True)
    cycle(fixture, 60)
    applications.process(fixture.database, stale, fixture.adapter, fixture.policy,
                         fixture.root, fixture.adapter.now, config=fixture.config)
    assert fixture.job()["state"] == state and fixture.adapter.operations == ["inspect"]
    assert not records(fixture, "APPLICATION_RECOVERY_STARTED")
    assert not records(fixture, "INSPECTION_REREAD_SCHEDULED")


def disabled(fixture):
    """With recovery disabled the bounded re-reads still happen and nothing is abandoned."""
    seed(fixture, held=True)
    fixture.config["application_recovery"]["enabled"] = False
    base = reread_phase(fixture, RESULT)
    cycle(fixture, base)
    assert fixture.job()["blocker"] == CODE and fixture.job()["state"] == "BLOCKED"
    assert len(records(fixture, "INSPECTION_REREAD_SCHEDULED")) == 2
    assert not records(fixture, "APPLICATION_RECOVERY_STARTED")
    assert not records(fixture, "APPLICATION_ABANDONED")
    assert not fixture.adapter.decisions


def saved_unknown_only(fixture):
    seed(fixture, "AUTHENTICATION_REQUIRED")
    with fixture.database:
        applications.hold(fixture.database, fixture.job(), CODE, "Saved unrelated blocker", NOW)
    cycle(fixture, 60)
    assert fixture.job()["blocker"] == "AUTHENTICATION_REQUIRED"
    assert not records(fixture, "APPLICATION_RECOVERY_STARTED")
    assert not records(fixture, "INSPECTION_REREAD_SCHEDULED")


def shared_compute(fixture):
    """A coordinator compute wait consumes no attempt and changes no reservation."""
    seed(fixture, held=True)
    base = reread_phase(fixture, RESULT)
    workflow_tasks.initialize(fixture.database)
    assert workflow_tasks.reserve_compute(fixture.database, "synthetic-existing-usage", NOW, 10800)
    original = [tuple(row) for row in fixture.database.execute("SELECT * FROM worker_compute_reservations")]
    adapter = application_connection.HostedDesktopAdapter(fixture.database, fixture.root, fixture.config,
        clock=lambda: desktop_adapter.later(NOW, base), consumer=forbidden)
    applications.cycle(fixture.database, fixture.config, fixture.root, adapter, desktop_adapter.later(NOW, base))
    assert fixture.job()["blocker"] == "APPLICATION_BROWSER_SHARED_BUDGET_EXHAUSTED", fixture.job()
    assert len(records(fixture, "APPLICATION_RECOVERY_STARTED")) == 1
    assert not records(fixture, "APPLICATION_RECOVERY_INVESTIGATED")
    assert not records(fixture, "APPLICATION_RECOVERY_DECISION_FAILED")
    assert fixture.database.execute("SELECT count(*) FROM desktop_requests WHERE operation='investigate'").fetchone()[0] == 0
    assert [tuple(row) for row in fixture.database.execute("SELECT * FROM worker_compute_reservations")] == original
    assert fixture.job()["attempts"] == 0
    assert len(records(fixture, "INSPECTION_REREAD_SCHEDULED")) == 2


def compute_wait_before_reread(fixture):
    """A capacity wait on the re-read itself consumes no attempt and schedules no re-read."""
    seed(fixture, held=True)
    cycle(fixture, 60)
    assert len(records(fixture, "INSPECTION_REREAD_SCHEDULED")) == 1
    # At or after the re-read, and after the earlier unserviced request's delivery window, so the
    # budget - not that still-live request - is what stands in the way.
    due = max(offset(fixture.job()["next_at"]), desktop_adapter.REQUEST_SECONDS + 60)
    workflow_tasks.initialize(fixture.database)
    assert workflow_tasks.reserve_compute(fixture.database, "synthetic-existing-usage", NOW, 10800)
    original = [tuple(row) for row in fixture.database.execute("SELECT * FROM worker_compute_reservations")]
    requests = inspections(fixture)
    adapter = application_connection.HostedDesktopAdapter(fixture.database, fixture.root, fixture.config,
        clock=lambda: desktop_adapter.later(NOW, due), consumer=forbidden)
    applications.cycle(fixture.database, fixture.config, fixture.root, adapter, desktop_adapter.later(NOW, due))
    assert fixture.job()["blocker"] == "APPLICATION_BROWSER_SHARED_BUDGET_EXHAUSTED", fixture.job()
    assert fixture.job()["attempts"] == 0
    assert len(records(fixture, "INSPECTION_REREAD_SCHEDULED")) == 1
    assert not records(fixture, "APPLICATION_RECOVERY_STARTED")
    assert inspections(fixture) == requests
    assert [tuple(row) for row in fixture.database.execute("SELECT * FROM worker_compute_reservations")] == original


def event_ledger(fixture):
    return [tuple(row) for row in fixture.database.execute(
        "SELECT id,job_key,event,detail,at FROM application_events ORDER BY id")]


def desktop_ledger(fixture):
    return [tuple(row) for row in fixture.database.execute("SELECT * FROM desktop_requests ORDER BY rowid")]


def claims_exhausted_review_hold(fixture):
    """Spending the third claim after an AI RETRY is a reviewable hold, never ABANDONED.

    Mirrors the live wingtip / massive dynamic shape: the coordinator's own last decision is
    RETRY with a supported method, and the last investigation returned concrete
    `browser_evidence`, yet the third claim is the last one the bound allows. The bound
    holds; the terminal judgment is not made by Python.
    """
    seed(fixture, held=True)
    base = reread_phase(fixture, RESULT)
    for attempt in range(3):
        seconds = base + attempt * 1000
        cycle(fixture, seconds)
        assert len(fixture.adapter.decisions) == attempt + 1, fixture.job()
        complete(fixture, "investigate", {
            "status": "FAILED",
            "reason": "Read the company posting and observed SEE ON ASHBY and APPLY NOW links",
            "browser_evidence": [{"pointer": "/observed_links", "quote": CANONICAL,
                                  "tool_call_id": "synthetic-read-" + str(attempt + 1)}]})
        cycle(fixture, seconds + 60)
    detail = assert_reviewable_hold(fixture, base, 3)
    assert detail["evidence"][-1]["result"]["browser_evidence"], detail
    decided = records(fixture, "APPLICATION_RECOVERY_DECIDED")
    assert len(decided) == 3 and decided[-1]["decision"]["action"] == "RETRY"
    events, desktop = event_ledger(fixture), desktop_ledger(fixture)
    operations = list(fixture.adapter.operations)
    for seconds in (base + 4000, base + 20000, base + 47 * 3600, base + 48 * 3600,
                    base + 72 * 3600, 400000, 500000):
        cycle(fixture, seconds)
        assert application_recovery.recover(fixture.database, fixture.job(), fixture.adapter,
            fixture.config, fixture.job()["blocker"], fixture.job()["next_action"], fixture.adapter.now)
        job = fixture.job()
        assert job["state"] == "BLOCKED" and job["blocker"] == application_recovery.CLAIMS_EXHAUSTED_BLOCKER, job
    assert fixture.adapter.operations == operations
    assert len(fixture.adapter.decisions) == 3
    assert event_ledger(fixture) == events and desktop_ledger(fixture) == desktop
    assert len(records(fixture, application_recovery.CLAIMS_EXHAUSTED_EVENT)) == 1
    assert not records(fixture, "APPLICATION_ABANDONED")
    assert len(records(fixture, "APPLICATION_RECOVERY_STARTED")) == 1
    assert fixture.database.execute("SELECT sum(claims) FROM desktop_requests WHERE operation='investigate'").fetchone()[0] == 3
    assert inspections(fixture) == 3 and fixture.job()["attempts"] == 0
    return {"investigate_claims": 3, "coordinator_decisions": 3, "abandoned": 0,
            "claims_exhausted_holds": 1, "final_state": fixture.job()["state"],
            "final_blocker": fixture.job()["blocker"]}


def ai_abandon_terminal(fixture):
    """An explicit AI ABANDON still yields the terminal record, with its reason and evidence."""
    seed(fixture, held=True)
    base = reread_phase(fixture, RESULT)
    cycle(fixture, base)
    assert len(fixture.adapter.decisions) == 1
    complete(fixture, "investigate", {"status": "FAILED", "reason": "Synthetic unresolved read"})
    cycle(fixture, base + 60)
    reason = "No supported official destination exists for this requisition after an actual read"

    def decide(packet):
        fixture.adapter.decisions.append(packet)
        return {"action": "ABANDON", "reason": reason, "progress": True}

    fixture.adapter.recovery_decision = decide
    cycle(fixture, base + 1000)
    job = fixture.job()
    assert job["state"] == "ABANDONED" and job["blocker"] == "APPLICATION_RECOVERY_ABANDONED", job
    abandoned = records(fixture, "APPLICATION_ABANDONED")
    assert len(abandoned) == 1 and abandoned[0]["reason"] == reason
    assert abandoned[0]["submission_status"] == "NOT_SUBMITTED_BY_RECOVERY"
    assert abandoned[0]["evidence"]["decision"] == {"action": "ABANDON", "reason": reason, "progress": True}
    assert len(abandoned[0]["evidence"]["findings"]) == 1
    assert abandoned[0]["next_action"] == "Retain terminal abandonment and its evidence; do not automatically reopen"
    assert not records(fixture, application_recovery.CLAIMS_EXHAUSTED_EVENT)
    assert fixture.database.execute("SELECT count(*) FROM application_blockers").fetchone()[0] == 0
    assert len(records(fixture, "APPLICATION_RECOVERY_INVESTIGATED")) == 1
    assert fixture.database.execute("SELECT sum(claims) FROM desktop_requests WHERE operation='investigate'").fetchone()[0] == 1
    before = list(fixture.database.iterdump())
    cycle(fixture, base + 200000)
    assert list(fixture.database.iterdump()) == before
    assert fixture.job()["state"] == "ABANDONED"


def existing_terminal_records_untouched(fixture):
    """A job already ABANDONED for RECOVERY_ATTEMPTS_EXHAUSTED is never reopened or reclassified."""
    seed(fixture, held=True)
    key = fixture.key
    lifecycle = {"blocker": CODE, "started_at": NOW,
                 "max_attempts": 3}
    action = "Retain terminal abandonment and its evidence; do not automatically reopen"
    with fixture.database:
        applications.event(fixture.database, key, "APPLICATION_RECOVERY_STARTED", lifecycle, NOW)
        applications.event(fixture.database, key, "APPLICATION_ABANDONED", dict(
            lifecycle, reason="RECOVERY_ATTEMPTS_EXHAUSTED", attempts=3, evidence=[], job_key=key,
            submission_status="NOT_SUBMITTED_BY_RECOVERY", next_action=action), NOW)
        applications.hold(fixture.database, fixture.job(), "APPLICATION_RECOVERY_ABANDONED", action, NOW, "ABANDONED")
        fixture.database.execute("DELETE FROM application_blockers WHERE job_key=?", (key,))
    before = list(fixture.database.iterdump())
    stale = fixture.job()
    for seconds in (60, 1000, 48 * 3600, 200000):
        cycle(fixture, seconds)
        applications.process(fixture.database, stale, fixture.adapter, fixture.policy,
                             fixture.root, fixture.adapter.now, config=fixture.config)
        assert not application_recovery.recover(fixture.database, fixture.job(), fixture.adapter,
            fixture.config, CODE, "Reopen attempt", fixture.adapter.now)
        assert not application_recovery.hold_exhausted(fixture.database, fixture.job(), lifecycle, [], fixture.adapter.now)
    assert list(fixture.database.iterdump()) == before
    job = fixture.job()
    assert job["state"] == "ABANDONED" and job["blocker"] == "APPLICATION_RECOVERY_ABANDONED", job
    assert len(records(fixture, "APPLICATION_ABANDONED")) == 1
    assert records(fixture, "APPLICATION_ABANDONED")[0]["reason"] == "RECOVERY_ATTEMPTS_EXHAUSTED"
    assert not records(fixture, application_recovery.CLAIMS_EXHAUSTED_EVENT)
    assert not application_recovery.claims_exhausted(fixture.database, key)


def explicit_admission(fixture):
    before = list(fixture.database.iterdump())
    assert not application_recovery.recover(fixture.database, fixture.job(), fixture.adapter,
        fixture.config, SECOND_CODE, "Unrecognized saved blocker", NOW)
    assert list(fixture.database.iterdump()) == before
    for active in (False, True):
        if active:
            seed(fixture, held=True)
            base = reread_phase(fixture, RESULT)
            cycle(fixture, base)
            assert len(records(fixture, "APPLICATION_RECOVERY_STARTED")) == 1
        for code in sorted(application_recovery.USER_HOLDS | {"UNKNOWN_REQUIRED_ANSWERS:legal_name"}):
            for source in ("input", "job"):
                job = fixture.job()
                if source == "job":
                    job["blocker"] = code
                before = list(fixture.database.iterdump())
                decisions = len(fixture.adapter.decisions)
                assert not applications.reread_inspection(fixture.database, job, fixture.policy,
                    code if source == "input" else CODE, schedule=True), (active, source, code)
                assert not application_recovery.recover(fixture.database, job, fixture.adapter,
                    fixture.config, code if source == "input" else CODE, "Protected hold",
                    fixture.adapter.now), (active, source, code)
                assert list(fixture.database.iterdump()) == before
                assert len(fixture.adapter.decisions) == decisions


SCENARIOS = {"first_unknown": bounded, "cached_unknown": lambda fixture: bounded(fixture, held=True),
             "instrumented": instrumented, "outside_allowlist": outside_allowlist,
             "resolved_destination": resolved, "empty_resolution": empty_resolution, "uncertain": uncertain, "disabled": disabled,
             "saved_unknown_only": saved_unknown_only, "shared_compute": shared_compute,
             "compute_wait_before_reread": compute_wait_before_reread,
             "explicit_admission": explicit_admission,
             "claims_exhausted_review_hold": claims_exhausted_review_hold,
             "ai_abandon_terminal": ai_abandon_terminal,
             "existing_terminal_records_untouched": existing_terminal_records_untouched,
             "eligibility_after_recovery": lambda fixture: resolved(fixture, hard_gate=True)}
SCENARIOS.update({"fresh_" + code: lambda fixture, code=code: protected(fixture, code)
                  for code in sorted(application_recovery.USER_HOLDS | {"UNKNOWN_REQUIRED_ANSWERS:legal_name"})})
SCENARIOS.update({"saved_" + code: lambda fixture, code=code: saved_hold(fixture, code)
                  for code in sorted(application_recovery.USER_HOLDS | {"UNKNOWN_REQUIRED_ANSWERS:legal_name"})})
SCENARIOS.update({"history_" + kind: lambda fixture, kind=kind: duplicate(fixture, kind)
                  for kind in ("APPLICATION", "INTERVIEW")})
SCENARIOS.update({state: lambda fixture, state=state: terminal(fixture, state)
                  for state in sorted(applications.FINAL)})


def run(names=None):
    passed = []
    measurements = {}
    with contextlib.ExitStack() as guards:
        for target in ("socket.create_connection", "socket.socket.connect", "socket.socket.connect_ex",
                       "socket.socket.sendto", "socket.getaddrinfo", "subprocess.Popen"):
            guards.enter_context(patch(target, forbidden))
        for name in names if names is not None else SCENARIOS:
            with tempfile.TemporaryDirectory(prefix="unknown-inspection-") as temporary:
                fixture = Fixture(Path(temporary))
                fixture.config["application_recovery"] = {"enabled": True, "max_attempts": 3}
                fixture.adapter = RecoveryAdapter(fixture)
                try:
                    measured = SCENARIOS[name](fixture)
                    if measured:
                        measurements[name] = measured
                    assert fixture.database.execute("SELECT count(*) FROM desktop_requests WHERE operation IN ('submit','verify')").fetchone()[0] == 0
                    assert fixture.database.execute("SELECT count(*) FROM application_answers").fetchone()[0] == 0
                    passed.append(name)
                finally:
                    fixture.database.close()
    return {"mode": "ISOLATED_SYNTHETIC", "passed": passed, "measurements": measurements, "external_effects": 0}


if __name__ == "__main__":
    print(json.dumps(run(sys.argv[1:] or None), indent=2))
