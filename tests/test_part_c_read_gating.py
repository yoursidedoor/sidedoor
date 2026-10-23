"""Part C (owner-approved 2026-09-19): no repeated read without new input, reachable verification.

Each test fails on the pre-Part-C code: it either observes the old repeated read/coordinator
call, the old policy-hash cap reset, the old ConnectionError relabelling, or it calls a
function the old code did not have.
"""

import datetime as dt
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import applications
import desktop_adapter
import external_history
import orchestrator
import profile_home


NOW = "2026-09-19T09:00:00+00:00"
# These tests are about the inspect cap, which deliberately ignores claims made before
# `applications.READ_CONTRACT_REVISION_AT`. A fixture on a fixed date therefore changes meaning
# the moment that constant is bumped - which is exactly what happened on 2026-09-22, the first
# bump since it was introduced: NOW fell before the contract date, every claim stopped counting
# and the cap tests passed vacuously or failed. Pinning the constant for the duration of these
# tests keeps them about the cap instead of about the calendar, and leaves every other fixture
# clock in this file (and the modules that import NOW from it) where it was.
CONTRACT_AT = "2026-09-01T00:00:00+00:00"
EMAIL = "candidate@example.invalid"
RECOVERY = {"application_recovery": {"enabled": True, "max_attempts": 3, "deadline_hours": 48}}


def at(seconds):
    return (dt.datetime.fromisoformat(NOW) + dt.timedelta(seconds=seconds)).isoformat()


def role(company="Gate Systems", requisition="4700000001"):
    return {"company": company, "role": "Staff Software Engineer", "requisition_id": requisition,
            "url": "https://job-boards.greenhouse.io/gate/jobs/" + requisition, "rationale": "MOCK FIXTURE ONLY",
            "evidence": [{"field": "funding_stage", "value": "Series B", "source": "https://gate.example/news",
                          "observed_at": "2026-09-10T00:00:00+00:00", "verified": True, "kind": "official"},
                         {"field": "employee_count", "value": "51-200", "source": "https://gate.example/about",
                          "observed_at": "2026-09-10T00:00:00+00:00", "verified": True, "kind": "public"}]}


def public_review(job, now):
    """What a real Greenhouse read can honestly return: no revision, no candidate, no history,
    no funding stage or headcount on the posting, visible required questions only."""
    source = {"source": "mock-browser", "verified": True, "observed_at": now}
    return dict(source, employer=job["employer"], requisition=job["requisition"],
                official_url=json.loads(job["payload"])["url"], role="Staff Software Engineer",
                candidate_email=None, official_destination_verified=True, action_scope="application_only",
                employee_count_evidence={"verified": False, "value": None},
                facts={"open": True, "funding_stage": None, "industry": "AI", "level": "Staff Software Engineer",
                       "country": "US", "city": "Austin", "work_mode": "remote", "specialization": "backend",
                       "requirements_met": True, "authorization": "US_PERMANENT_RESIDENT_ACCEPTED",
                       "hiring_geography_includes_candidate": True, "citizenship_clearance_requirements_met": True},
                scope_fit={"level": {"in_scope": True, "reason": "fixture: a target level"}, "specialization": {"in_scope": True, "reason": "fixture: a target specialization"}},
                compensation=dict(source, currency="USD", minimum=220000, maximum=260000, period="annual_base"),
                history={"verified": False, "job_key": None, "application": None, "interview": None, "referral": None},
                questions=[{"id": None, "label": "Email", "semantic": "contact_email", "required": True}],
                visible_required_questions_enumerated=True, all_required_questions_enumerated=False,
                form_revision=None)


class Adapter:
    def __init__(self, blocked=False):
        self.calls, self.decisions, self.now, self.can_submit, self.blocked = [], [], NOW, True, blocked

    def call(self, operation, payload):
        self.calls.append((operation, payload))
        if operation == "inspect":
            review = public_review(payload["job"], self.now)
            if self.blocked:
                review["facts"]["requirements_met"] = None
                return {"blocked": {"code": "FORM_REVIEW_INCOMPLETE", "next_action": "mock"},
                        "partial_review": review, "observed_browser_calls": 4}
            return review
        if operation == "submit":
            return {"acknowledged": False, "status": "UNKNOWN"}
        return {}

    def recovery_decision(self, packet):
        self.decisions.append(packet)
        return {"action": "RETRY", "method": "official_posting", "instructions": "read " + str(len(self.decisions)),
                "reason": "mock", "progress": False}


def search(outcome, searched_at=NOW, employer="Gate Systems", search_id="gmail-search:gate-1"):
    record = {"record_type": "gmail_history_search", "search_id": search_id, "employer": employer,
              "mailbox_email": EMAIL, "outcome": outcome, "searched_at": searched_at, "observed_at": searched_at}
    if outcome == "FAILED":
        record["failure_reason"] = "Gmail did not load"
    else:
        record.update(source="https://mail.google.com/mail/u/0/#search/gate+systems", query="Gate Systems",
                      performed=True, independent_read=True, result_count=0 if outcome == "NO_MATCH" else 2,
                      searched_for=["application_confirmation", "interview", "rejection"])
    return record


class PartCTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for folder in ("state", "reports", "logs"):
            (self.root / folder).mkdir()
        self.database = orchestrator.connect(self.root)
        self.addCleanup(self.database.close)
        applications.initialize(self.database)
        desktop_adapter.initialize(self.database)
        resume = b"mock resume; never uploaded"
        (self.root / "resume.pdf").write_bytes(resume)
        self.policy = {"minimum_base_salary_usd": 180000, "funding_stages": ["Series B"], "industries": ["AI"],
                       "levels": ["Staff Software Engineer"], "max_attempts": 3, "max_reconcile_attempts": 3,
                       "retry_delays_seconds": [900], "preapproved_applications": True, "referrals_block_applications": False,
                       "resume": {"path": "resume.pdf", "sha256": hashlib.sha256(resume).hexdigest()},
                       "answers": {"email": {"value": EMAIL, "semantic": "contact_email", "source": "mock"}}}
        profile_home.use(self, self.policy)
        network = patch("socket.create_connection", side_effect=AssertionError("no network in tests"))
        network.start()
        self.addCleanup(network.stop)
        contract = patch.object(applications, "READ_CONTRACT_REVISION_AT", CONTRACT_AT)
        contract.start()
        self.addCleanup(contract.stop)
        with self.database:
            self.key = applications.ingest(self.database, role(), NOW)

    def cycle(self, adapter, seconds, config=None):
        adapter.now = at(seconds)
        applications.cycle(self.database, config or {}, self.root, adapter, adapter.now)

    def job(self):
        return dict(self.database.execute("SELECT * FROM application_jobs WHERE job_key=?", (self.key,)).fetchone(),
                    candidate_email=EMAIL)

    def test_inconclusive_read_is_retried_each_cycle(self):
        """Owner, 2026-09-22: "A failed read should [re-read]."

        This asserted the opposite until then - Part C1 suppressed a repeat of an inconclusive
        read of an unchanged posting until new input arrived or the seven-day record expired.
        That is what left umbrella pushing next_at forward an hour at a time while emitting no
        event at all. A failed read now retries on each cycle; the bound is the inspect claim cap
        (`test_policy_edit_does_not_reset_inspect_cap` below still proves it), not the gate.
        """
        adapter = Adapter(blocked=True)
        for seconds in (0, 7200, 14400):
            self.cycle(adapter, seconds)
        self.assertEqual([op for op, _ in adapter.calls], ["inspect"] * 3)
        self.assertFalse([row for row in self.database.execute(
            "SELECT 1 FROM application_events WHERE job_key=? AND event='APPLICATION_READ_SUPPRESSED'", (self.key,))])

    def test_no_coordinator_call_when_no_method_can_supply_missing_fields(self):
        adapter = Adapter(blocked=True)
        for seconds in (0, 7200, 14400):
            self.cycle(adapter, seconds, RECOVERY)
        # The COORDINATOR suppression is unchanged and is what this test is about: no method can
        # supply the missing fields, so no coordinator call is made. Only the READ suppression was
        # inverted (owner, 2026-09-22), so the inspect itself now retries per cycle.
        self.assertEqual(adapter.decisions, [])
        self.assertEqual([op for op, _ in adapter.calls], ["inspect"] * 3)

    def test_policy_edit_does_not_reset_inspect_cap(self):
        import application_connection
        (self.root / "config.json").write_text("{}")
        # Claims are made inside the current inspect scope (after READ_CONTRACT_REVISION_AT):
        # claims made under a superseded contract no longer count (patch BB).
        later = at(4 * 3600)
        adapter = application_connection.HostedDesktopAdapter(self.database, self.root, {"application_browser": {"enabled": True}},
                                                              clock=lambda: later, consumer=lambda *a: self.fail("read scheduled"))
        body = adapter.request_body("inspect", {"job": self.job()})
        with self.database:
            for index in range(3):
                self.database.execute("""INSERT INTO desktop_requests(request_id,binding,job_key,operation,payload,state,
                    created_at,expires_at,claims,result) VALUES (?,?,?,?,?,'CONSUMED',?,?,1,'{}')""",
                    ("r" + str(index), "b" + str(index), self.key, "inspect", desktop_adapter.encode(body), later, at(4 * 3600 + 900)))
        profile_home.use(self, dict(self.policy, note="unrelated edit"))
        result = adapter.call("inspect", {"job": self.job(), "read_only": True})
        self.assertEqual(result["blocked"]["code"], "APPLICATION_BROWSER_INVESTIGATION_EXHAUSTED")

    def test_stored_validation_error_keeps_its_code(self):
        adapter = desktop_adapter.DesktopAdapter(self.database, self.root, {}, clock=lambda: NOW)
        body = adapter.request_body("inspect", {"job": self.job()})
        with self.database:
            self.database.execute("""INSERT INTO desktop_requests(request_id,binding,job_key,operation,payload,state,
                created_at,expires_at,claims,result) VALUES ('r1',?,?,'inspect',?,'DONE',?,?,1,?)""",
                (desktop_adapter.digest(body), self.key, desktop_adapter.encode(body), NOW, at(900),
                 json.dumps({"error": {"code": "APPLICATION_UNOBSERVED_QUOTE"}})))
        with self.assertRaises(ConnectionError) as raised:
            adapter.call("inspect", {"job": self.job(), "read_only": True})
        self.assertEqual(getattr(raised.exception, "code", None), "APPLICATION_UNOBSERVED_QUOTE")

    # test_d1_history_needs_a_search_that_ran_and_found_nothing deleted 2026-09-22: it pinned the
    # requirement that a fresh successful Gmail history search back every absence
    # conclusion. The owner removed that search entirely - the tracker database is the
    # source of truth for prior applications now.

    def test_public_form_reaches_the_guarded_submit(self):
        with self.database:
            external_history.ingest_search(self.database, search("NO_MATCH"), EMAIL)
        adapter = Adapter()
        self.cycle(adapter, 60)
        submits = [payload for op, payload in adapter.calls if op == "submit"]
        self.assertEqual(len(submits), 1)
        self.assertTrue(submits[0]["form_revision"].startswith("questions-sha256:"))
        self.assertEqual(list(submits[0]["answers"].values()), [EMAIL])
        review = json.loads(self.database.execute("SELECT evidence FROM application_reviews WHERE job_key=?", (self.key,)).fetchone()[0])
        # The history-basis inference was deleted with item 16 (owner, 2026-09-27).
        self.assertEqual(review["candidate_email_binding"], "DEFERRED_TO_GUARDED_SUBMIT_SESSION")
        self.assertEqual(review["employee_count_evidence"]["basis"], "RESEARCH_STORED")


if __name__ == "__main__":
    unittest.main()
