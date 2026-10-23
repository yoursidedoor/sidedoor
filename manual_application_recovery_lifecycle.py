"""Synthetic lifecycle rehearsal; never opens production records or a live browser."""

import datetime as dt
import hashlib
import json
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
from unittest.mock import patch

import application_recovery
import applications
import application_connection
import external_history
import desktop_adapter
import workflow_tasks

sys.path.insert(0, str(Path(__file__).resolve().parent / "tests"))
from test_applications_e2e import MockATS, NOW, role
import user_profile


CONFIG = {"application_recovery": {"enabled": True, "max_attempts": 3}}


class Fixture:
    def __init__(self, root):
        self.root = root
        self.database = sqlite3.connect(root / "synthetic.sqlite3")
        self.database.row_factory = sqlite3.Row
        applications.initialize(self.database)
        desktop_adapter.initialize(self.database)
        resume = b"SYNTHETIC ONLY"
        (root / "resume.pdf").write_bytes(resume)
        policy = {"minimum_base_salary_usd": 180000, "funding_stages": ["Series B"],
                  "industries": ["AI"], "levels": ["Staff Software Engineer"],
                  "max_attempts": 3, "max_reconcile_attempts": 3, "retry_delays_seconds": [900],
                  "preapproved_applications": True,
                  "resume": {"path": "resume.pdf", "sha256": hashlib.sha256(resume).hexdigest()},
                  "answers": {"email": {"value": "candidate@example.invalid", "semantic": "contact_email", "source": "synthetic"}}}
        user_profile.use_policy(policy, root / "profile-home")
        self.adapter = RecoveryATS(root)

    def discover(self, company="Example AI", unresolved=True):
        posting = role(company)
        if unresolved:
            posting.update(url="https://careers.example.com/roles/staff", requisition_id="")
        with self.database:
            key = applications.ingest(self.database, posting, NOW)
            if unresolved:
                # Seeded here, not minted by the gate, and seeded as REQUISITION_MISMATCH.
                # These scenarios rehearse the IDENTITY RECOVERY lifecycle, which is unchanged.
                # They used to reach it through REQUISITION_UNKNOWN, which local_gate raised for
                # a weak identity until 2026-09-24 (approach A) and now raises nowhere, so it is
                # superseded on sight and no lifecycle would run. REQUISITION_MISMATCH is raised
                # by ats_browser, is RECOVERABLE, and maps to the same {"identity"} methods, so
                # the rehearsal now covers a path production can actually take.
                self.database.execute(
                    "UPDATE application_jobs SET state='BLOCKED',blocker='REQUISITION_MISMATCH',"
                    "next_action='Reconcile employer/requisition identity before applying',"
                    "next_at=NULL WHERE job_key=?", (key,))
        return key, posting

    def cycle(self, seconds=0):
        self.adapter.now = (dt.datetime.fromisoformat(NOW) + dt.timedelta(seconds=seconds)).isoformat()
        return applications.cycle(self.database, CONFIG, self.root, self.adapter, self.adapter.now)

    def job(self, key):
        return dict(self.database.execute("SELECT * FROM application_jobs WHERE job_key=?", (key,)).fetchone())

    def events(self, key, kind):
        return application_recovery.records(self.database, key, kind)


class RecoveryATS(MockATS):
    def __init__(self, root):
        super().__init__(root)
        self.responses = []
        self.investigations = []
        self.decisions = []
        self.decision_packets = []

    def recovery_decision(self, packet):
        self.decision_packets.append(packet)
        if self.decisions:
            decision = self.decisions.pop(0)
            if isinstance(decision, Exception):
                raise decision
            return decision
        attempt = packet["recovery"]["attempt"]
        method = list(application_recovery.METHODS)[min(attempt - 1, 2)]
        return {"action": "RETRY", "method": method, "instructions": "Synthetic AI-selected read " + str(attempt),
                "reason": "Synthetic orchestrator assessed the previous failure and selected this supported action", "progress": False}

    def call(self, operation, payload):
        if operation != "investigate":
            return super().call(operation, payload)
        self.investigations.append(payload)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response(payload, self.now) if callable(response) else response


def evidence(payload, now):
    return {"source": json.loads(payload["job"]["payload"])["url"], "observed_at": now,
            "verified": True, "independent_read": True, "job_key": payload["job"]["job_key"]}


def resolved(payload, now):
    job = payload["job"]
    identity = dict(evidence(payload, now), employer=job["employer"],
                    role=json.loads(job["payload"])["role"], requisition="role-123",
                    identity_basis="official_canonical_posting_id")
    return {"status": "RESOLVED", "reason": "Official exact requisition observed", "evidence": evidence(payload, now),
            "resolution": {"identity": {"requisition": "role-123", "evidence": identity}}}


def bounded_failure(fixture):
    key, posting = fixture.discover()
    fixture.adapter.responses = [RuntimeError("official posting unavailable"), RuntimeError("observed link unavailable"), RuntimeError("saved history cannot resolve identity")]
    for seconds in (0, 901, 1802):
        fixture.cycle(seconds)
    assert fixture.job(key)["state"] == "BLOCKED"
    assert fixture.job(key)["blocker"] == application_recovery.CLAIMS_EXHAUSTED_BLOCKER
    assert fixture.job(key)["attempts"] == 0
    assert not fixture.events(key, "APPLICATION_ABANDONED")
    held = fixture.events(key, application_recovery.CLAIMS_EXHAUSTED_EVENT)
    assert len(held) == 1 and held[0]["reason"] == "RECOVERY_CLAIMS_EXHAUSTED_PENDING_REVIEW"
    assert held[0]["attempts"] == 3 and held[0]["terminal"] is False
    assert held[0]["last_decision"]["action"] == "RETRY"
    assert held[0]["submission_status"] == "NOT_SUBMITTED_BY_RECOVERY"
    assert held[0]["next_action"] == application_recovery.CLAIMS_EXHAUSTED_ACTION
    assert len({packet["recovery"]["supported_alternative"]["method"] for packet in fixture.adapter.investigations}) == 3
    assert fixture.adapter.investigations[1]["recovery"]["previous_failure"]["reason"] == "RuntimeError"
    with fixture.database:
        alias = dict(posting, requisition_id="verified-alias")
        applications.add_alias(fixture.database, alias, key, {"source": "synthetic verified alias"})
        assert applications.ingest(fixture.database, alias) == key
        assert applications.ingest(fixture.database, posting) == key
    fixture.database.close()
    fixture.database = sqlite3.connect(fixture.root / "synthetic.sqlite3")
    fixture.database.row_factory = sqlite3.Row
    fixture.cycle(200000)
    fixture.cycle(300000)
    assert fixture.job(key)["state"] == "BLOCKED"
    assert fixture.job(key)["blocker"] == application_recovery.CLAIMS_EXHAUSTED_BLOCKER
    assert not fixture.events(key, "APPLICATION_ABANDONED")
    assert len(fixture.events(key, application_recovery.CLAIMS_EXHAUSTED_EVENT)) == 1
    assert len(fixture.adapter.investigations) == 3
    assert len(fixture.events(key, "APPLICATION_RECOVERY_STARTED")) == 1


def compute_wait(fixture):
    key, _ = fixture.discover()
    fixture.adapter.responses = [application_connection.CapacityWait("APPLICATION_BROWSER_SHARED_BUDGET_EXHAUSTED", "2026-09-18T07:00:00+00:00"), resolved]
    fixture.cycle()
    original = fixture.events(key, "APPLICATION_RECOVERY_STARTED")[0]
    assert len(fixture.events(key, "APPLICATION_RECOVERY_INVESTIGATED")) == 0
    assert fixture.job(key)["attempts"] == 0
    fixture.cycle(60)
    assert len(fixture.adapter.investigations) == 1
    fixture.cycle(55000)
    assert fixture.job(key)["state"] == "SUBMITTED"
    assert fixture.events(key, "APPLICATION_RECOVERY_STARTED")[0] == original
    assert fixture.adapter.investigations[0]["recovery"] == fixture.adapter.investigations[1]["recovery"]


def positive_resolution(fixture):
    key, posting = fixture.discover()
    fixture.adapter.responses = [resolved]
    fixture.cycle()
    assert fixture.job(key)["state"] == "SUBMITTED"
    assert [operation for operation, _ in fixture.adapter.calls] == ["inspect", "submit", "verify"]
    with fixture.database:
        assert applications.ingest(fixture.database, dict(posting, requisition_id="role-123")) == key


def uncertainty(fixture):
    key, _ = fixture.discover(unresolved=False)
    fixture.adapter.unknown_reconcile = True
    fixture.adapter.lose_submission = True
    fixture.cycle()
    for seconds in (901, 1802, 2703, 3604, 200000):
        fixture.cycle(seconds)
    assert fixture.job(key)["state"] == "RECONCILE"
    # Still reading on the widening schedule (owner, 2026-09-30: 10 reads with backoff), so the
    # blocker is the last read's own uncertain outcome rather than the spent-reads hold.
    assert fixture.job(key)["blocker"] in applications.ALWAYS_RECONCILE_CODES, fixture.job(key)["blocker"]
    assert fixture.database.execute("SELECT count(*) FROM application_attempts WHERE state='SUBMITTING'").fetchone()[0] == 1
    assert not fixture.adapter.investigations
    assert not fixture.events(key, "APPLICATION_ABANDONED")
    assert sum(operation == "submit" for operation, _ in fixture.adapter.calls) == 1


def explicit_abandon(fixture):
    key, _ = fixture.discover()
    fixture.adapter.responses = [lambda payload, now: {"status": "ABANDON", "reason": "Official posting removed and no observed replacement links", "evidence": evidence(payload, now)}]
    fixture.cycle()
    fixture.adapter.decisions = [{"action": "ABANDON", "method": "", "instructions": "", "reason": "Official posting removed and no observed replacement links, as established by previous browser findings", "progress": False}]
    fixture.cycle(901)
    assert fixture.job(key)["state"] == "ABANDONED"
    assert fixture.events(key, "APPLICATION_ABANDONED")[0]["attempts"] == 1


def explicit_release(fixture):
    # RELEASE (owner, 2026-09-24) could never be chosen until 2026-09-27: the answer form handed
    # to the coordinator allowed only RETRY/ABANDON (found, 2026-09-25). A release returns the
    # job to the ordinary path, which then submits it without another investigation.
    key, _ = fixture.discover()
    fixture.adapter.responses = [lambda payload, now: {"status": "UNRESOLVED", "reason": "Transient browser failure; facts already settled", "evidence": evidence(payload, now)}]
    fixture.cycle()
    fixture.adapter.decisions = [{"action": "RELEASE", "method": "", "instructions": "", "reason": "Facts are settled; the failure was transient, so the job returns to the ordinary path", "progress": True}]
    fixture.cycle(901)
    assert fixture.job(key)["state"] == "QUEUED" and fixture.job(key)["blocker"] is None
    assert len(fixture.events(key, "APPLICATION_RECOVERY_RELEASED")) == 1
    fixture.cycle(1802)
    assert fixture.job(key)["state"] == "SUBMITTED"
    assert len(fixture.adapter.investigations) == 1


def no_blind_retry(fixture):
    key, _ = fixture.discover()
    fixture.adapter.responses = [{"status": "RETRY", "reason": "Failed route", "next_alternative": {"method": "official_posting", "instructions": "Repeat identical failed route"}}]
    fixture.cycle()
    identical = {"action": "RETRY", "method": "official_posting", "instructions": "Synthetic AI-selected read 1", "reason": "Repeat without changed instructions", "progress": False}
    fixture.adapter.decisions = [identical, identical]
    fixture.cycle(901)
    fixture.cycle(1802)
    assert fixture.job(key)["state"] == "ABANDONED"
    assert fixture.events(key, "APPLICATION_ABANDONED")[0]["reason"] == "RECOVERY_COORDINATOR_FAILED"
    assert len(fixture.adapter.investigations) == 1


def same_method_different_action(fixture):
    key, _ = fixture.discover()
    fixture.adapter.responses = [RuntimeError("First official source unavailable"), resolved]
    fixture.adapter.decisions = [
        {"action": "RETRY", "method": "official_posting", "instructions": "Read first observed official source", "reason": "Initial investigation", "progress": False},
        {"action": "RETRY", "method": "official_posting", "instructions": "Read different observed official source", "reason": "Previous source failed; this observed source is available", "progress": False},
    ]
    fixture.cycle()
    fixture.cycle(901)
    assert fixture.job(key)["state"] == "SUBMITTED"
    assert len(fixture.adapter.investigations) == 2


def decision_compute_wait(fixture):
    key, _ = fixture.discover()
    fixture.adapter.decisions = [application_connection.CapacityWait("APPLICATION_BROWSER_CYCLE_CAPACITY", "2026-09-17T16:01:00+00:00")]
    fixture.adapter.responses = [resolved]
    fixture.cycle()
    assert not fixture.events(key, "APPLICATION_RECOVERY_DECIDED")
    assert not fixture.events(key, "APPLICATION_RECOVERY_DECISION_FAILED")
    assert not fixture.events(key, "APPLICATION_RECOVERY_INVESTIGATED")
    fixture.cycle(61)
    assert fixture.job(key)["state"] == "SUBMITTED"


def history_resolution(fixture, relationship="DISTINCT_ROLE"):
    key, _ = fixture.discover(unresolved=False)
    record = {"evidence_id": "gmail:synthetic", "source_message_id": "synthetic", "employer": "Example AI",
              "kind": "APPLICATION_CONFIRMED", "role_title": "Prior role", "source": "https://mail.google.com/mail/u/0/#all/synthetic",
              "requisition": None, "evidence_kind": "employer_email", "source_system": "ATS email", "verified": True,
              "sender_authenticated": True, "independent_read": True, "mailbox_email": "candidate@example.invalid",
              "recipient": "candidate@example.invalid", "delivered_to_owner": True, "sender": "ats@example.com",
              "summary": "Synthetic prior employer receipt", "occurred_at": "2026-08-01T00:00:00+00:00", "observed_at": NOW}
    with fixture.database:
        external_history.ingest(fixture.database, record, "candidate@example.invalid")
    # The AI's recorded comparison of that confirmation against this posting.
    with fixture.database:
        external_history.resolve(fixture.database, "gmail:synthetic", key, relationship,
                                 {"source": "synthetic comparison", "verified": True, "observed_at": NOW,
                                  "evidence_id": "gmail:synthetic", "job_key": key,
                                  "comparison": "Exact prior and current requisitions compared"}, NOW)

    # The same-posting judge before the first submit answers DIFFERENT for this posting (a
    # judgment that cannot run holds the job instead, owner 2026-10-06).
    def different(working, command, prompt, log, timeout):
        (Path(working) / "decision.json").write_text(json.dumps(
            {"verdict": "DIFFERENT", "same_as": "", "reason": "synthetic judgment", "quotes": []}))

    workflow_tasks.initialize(fixture.database)
    fixture.adapter.same_job_executor = different
    with patch.dict(CONFIG, model="synthetic", codex="synthetic"):
        fixture.cycle()
    # Owner, 2026-10-06: "we should never block employers unless the jobs are the same, hence
    # dedupe". The 90-day employer window is gone: only the same job is held, as a duplicate, and
    # no recovery read is spent mapping requisitions either way.
    if relationship == "SAME_ROLE":
        assert fixture.job(key)["state"] == "DUPLICATE" and fixture.job(key)["blocker"] == "EXISTING_APPLICATION"
    else:
        assert fixture.job(key)["state"] == "SUBMITTED"
    assert not fixture.adapter.investigations
    assert fixture.database.execute("SELECT COUNT(*) FROM application_external_history").fetchone()[0] == 1


def history_duplicate(fixture):
    history_resolution(fixture, "SAME_ROLE")
    assert not any(operation == "submit" for operation, _ in fixture.adapter.calls)


def destination_resolution(fixture):
    key, posting = fixture.discover()
    official_url = "https://jobs.ashbyhq.com/example/role-123"

    def browser_result(payload, now):
        identity = dict(evidence(payload, now), source="application-browser:synthetic-trace", employer=payload["job"]["employer"],
                        role=posting["role"], requisition="role-123", official_url=official_url,
                        candidate_email="candidate@example.invalid", identity_basis="official_canonical_posting_id",
                        official_destination_verified=True, browser_evidence=[{"quote": "Synthetic exact public identity", "tool_call_id": "mock-browser"}])
        return {"status": "RESOLVED", "reason": "Official same role and destination read", "evidence": evidence(payload, now),
                "resolution": {"identity": {"requisition": "role-123", "evidence": identity},
                               "destination": {"url": official_url, "evidence": identity}}}

    fixture.adapter.responses = [browser_result]
    fixture.cycle()
    assert fixture.job(key)["state"] == "SUBMITTED"
    assert json.loads(fixture.job(key)["payload"])["url"] == official_url
    assert len(fixture.events(key, "DESTINATION_RECONCILED")) == 1


def rejected_resolution(fixture):
    key, _ = fixture.discover()
    fixture.adapter.responses = [lambda payload, now: dict(resolved(payload, now), evidence={}), resolved]
    fixture.cycle()
    assert fixture.job(key)["state"] == "BLOCKED"
    assert fixture.job(key)["requisition"].startswith("unresolved:")
    fixture.cycle(901)
    assert "UNVERIFIED_RESOLUTION" in fixture.adapter.decision_packets[-1]["recovery"]["previous_failure"]["reason"]
    assert fixture.job(key)["state"] == "SUBMITTED"


def grant_admits_only_scoped_wait(fixture):
    scoped, _ = fixture.discover()
    other, _ = fixture.discover("Other AI")
    with fixture.database:
        for key in (scoped, other):
            applications.hold(fixture.database, fixture.job(key), "APPLICATION_BROWSER_SHARED_BUDGET_EXHAUSTED",
                              "Wait for budget", NOW, "BLOCKED", "2026-09-18T07:00:00+00:00")
    fixture.adapter.responses = [resolved]
    with patch.object(workflow_tasks, "application_recovery_available", side_effect=lambda database, job_key, now, seconds=240: job_key == scoped):
        fixture.cycle(60)
    assert fixture.job(scoped)["state"] == "SUBMITTED"
    assert fixture.job(other)["blocker"] == "APPLICATION_BROWSER_SHARED_BUDGET_EXHAUSTED"
    assert fixture.job(other)["next_at"] == "2026-09-18T07:00:00+00:00"


def coordinator_failure_artifacts(fixture):
    key, _ = fixture.discover()
    error = RuntimeError("Synthetic coordinator output malformed")
    error.artifacts = {"raw": "synthetic-coordinator.json", "log": "synthetic-coordinator.log"}
    fixture.adapter.decisions = [error, error]
    fixture.cycle()
    fixture.cycle(901)
    assert fixture.job(key)["state"] == "ABANDONED"
    assert not fixture.adapter.investigations
    assert fixture.events(key, "APPLICATION_RECOVERY_DECISION_FAILED")[0]["artifacts"] == error.artifacts


def confirmed_duplicate_gate(fixture):
    key, _ = fixture.discover(unresolved=False)
    with fixture.database:
        applications.hold(fixture.database, fixture.job(key), "APPLICATION_HISTORY_UNKNOWN", "Investigate", NOW)
        fixture.database.execute("INSERT INTO application_history VALUES (?,?,?,?)", ("example ai", "role-123", "APPLICATION", "{}"))
    fixture.cycle()
    assert fixture.job(key)["state"] == "DUPLICATE"
    assert not fixture.adapter.decision_packets
    assert not fixture.adapter.calls


def transient_release(fixture):
    """A transient failure on a settled job goes back to the ordinary path, not to abandonment.

    Tyrell AI was abandoned on 2026-09-24 after ONE claim: a TimeoutError on inspect, one
    investigation that failed validation, then "missing_fields is empty; the other supported
    methods offer no concrete different investigation addressing this failure". Nothing in that
    was evidence about the role. RELEASE is the move the coordinator did not have; `review_hold`
    has had it all along, but only after every investigation claim is spent.
    """
    key, _ = fixture.discover()
    def held():
        with fixture.database:
            fixture.database.execute("UPDATE application_jobs SET state='BLOCKED',blocker='REQUISITION_MISMATCH',"
                                     "next_action='seeded',next_at=NULL WHERE job_key=?", (key,))
    decision = {"action": "RELEASE", "method": "", "instructions": "", "progress": False,
                "reason": "The inspect timed out; employer, role, requisition and destination are settled"}
    for spent in range(1, application_recovery.RELEASE_LIMIT + 1):
        held()
        fixture.adapter.decisions = [dict(decision)]
        fixture.cycle(spent * 1000)
        assert fixture.job(key)["state"] == "QUEUED", fixture.job(key)
        assert fixture.job(key)["blocker"] is None
        assert application_recovery.releases_spent(fixture.database, key) == spent
    # No investigation claim is ever spent on a release, and the lifecycle ends so the next
    # cycle runs the ordinary path instead of pulling the job straight back into recovery.
    assert fixture.adapter.investigations == []
    assert not application_recovery.active(fixture.database, key)
    # Past the bound the job waits and is asked again. It is never abandoned, and the refusal is
    # not charged as a coordinator failure - two of those would abandon exactly the live role
    # this scenario exists to protect.
    for extra in range(2):
        held()
        fixture.adapter.decisions = [dict(decision)]
        fixture.cycle((application_recovery.RELEASE_LIMIT + extra + 1) * 1000)
        assert fixture.job(key)["state"] == "BLOCKED", fixture.job(key)
        assert application_recovery.releases_spent(fixture.database, key) == application_recovery.RELEASE_LIMIT
    assert not fixture.events(key, "APPLICATION_RECOVERY_DECISION_FAILED")
    assert not fixture.events(key, "APPLICATION_ABANDONED")


SCENARIOS = [bounded_failure, compute_wait, positive_resolution, uncertainty, explicit_abandon, explicit_release, no_blind_retry, same_method_different_action, decision_compute_wait, history_resolution,
             history_duplicate, destination_resolution, rejected_resolution, grant_admits_only_scoped_wait,
             coordinator_failure_artifacts, confirmed_duplicate_gate, transient_release]


def run(scenarios=SCENARIOS):
    passed = []
    with patch.object(socket, "create_connection", side_effect=AssertionError("No network in synthetic rehearsal")):
        for scenario in scenarios:
            with tempfile.TemporaryDirectory(prefix="application-recovery-manual-") as temporary:
                fixture = Fixture(Path(temporary))
                try:
                    scenario(fixture)
                    passed.append(scenario.__name__)
                finally:
                    fixture.database.close()
    return {"mode": "MOCK_ONLY", "passed": passed, "live_effects": 0}


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
