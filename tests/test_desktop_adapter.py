import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
import json
import sqlite3
import unittest
from unittest.mock import patch

import applications
import desktop_adapter
import notifications
import orchestrator
import test_applications_e2e as fixtures


class DesktopHandoffTests(unittest.TestCase):
    setUpBase = fixtures.ApplicationEndToEndTests.setUp

    def setUp(self):
        self.setUpBase()
        self.config = {"application_adapter": "desktop", "applications_enabled": True}
        (self.root / "config.json").write_text(json.dumps(self.config))
        self.now = fixtures.NOW
        self.provider = self.adapter
        self.adapter = desktop_adapter.DesktopAdapter(self.database, self.root, self.config, lambda: self.now)
        self.actions = []

    def discover(self, company="Example AI", requisition="123"):
        opportunity = dict(fixtures.role(company, requisition), url="https://job-boards.greenhouse.io/example/jobs/" + requisition)
        orchestrator.store_research(self.database, fixtures.PIPELINE,
                                   {"summary": "MOCK DESKTOP E2E", "blockers": [], "opportunities": [opportunity], "drafts": []})
        return applications.identity(opportunity)[2]

    def advance(self, seconds=60):
        self.now = desktop_adapter.later(self.now, seconds)
        self.provider.now = self.now

    def cycle(self):
        return applications.cycle(self.database, self.config, self.root, now=self.now)

    def claim(self, read_only=False):
        return desktop_adapter.claim(self.database, self.root, "mock-existing-desktop", self.now, read_only)

    def complete(self, request, result):
        return desktop_adapter.complete(self.database, dict(request, result=result), self.now)

    def service(self):
        request = self.claim()
        if not request:
            return False
        payload = request["payload"]
        operation = payload["operation"]
        if operation == "submit":
            desktop_adapter.arm(self.database, self.root, dict(request, observed_form_revision=payload["form_revision"]), self.now)
        self.actions.append(operation)
        try:
            result = self.provider.call(operation, payload)
        except (ConnectionError, TimeoutError):
            result = {"error": {"code": "MOCK_TRANSIENT"}}
        if result.get("status") in {"SUBMITTED", "NOT_SUBMITTED"}:
            result.update(evidence_kind="authenticated_ats_application_record", provider_message_or_application_id="mock-provider-123")
        self.complete(request, result)
        return True

    def drive(self, rounds=4):
        for _ in range(rounds):
            self.cycle()
            while self.service():
                pass
            self.advance()
        return self.cycle()

    def job(self, key=None):
        if key:
            return self.database.execute("SELECT * FROM application_jobs WHERE job_key=?", (key,)).fetchone()
        return self.database.execute("SELECT * FROM application_jobs ORDER BY job_key LIMIT 1").fetchone()

    def test_01_discovery_desktop_execution_receipt_tracker(self):
        key = self.discover()
        with patch("builtins.input", side_effect=AssertionError("Routine applications are preapproved")):
            result = self.drive()
        self.assertEqual(result["verified_submissions"], 1)
        # The mock submit page confirms the application, which is the verified submission (owner,
        # 2026-10-06): no verify or reconcile read follows.
        self.assertEqual(self.actions, ["inspect", "submit"])
        self.assertEqual(json.loads(self.job(key)["receipt"])["evidence_kind"], "page_confirmation")
        self.assertEqual(self.database.execute("SELECT count(*) FROM application_history WHERE kind='APPLICATION'").fetchone()[0], 1)
        self.assertEqual(self.job(key)["state"], "SUBMITTED")

    def test_02_existing_application_and_interview_never_dispatch(self):
        for company, kind, requisition in [("First AI", "APPLICATION", "123"), ("Second AI", "INTERVIEW", "456")]:
            key = self.discover(company, requisition)
            # This job's own requisition: only the same job is a duplicate (owner, 2026-10-06).
            with self.database:
                self.database.execute("INSERT INTO application_history VALUES (?,?,?,?)",
                                      (applications.normalized(company), self.job(key)["requisition"], kind, "existing authoritative fixture"))
        self.drive()
        self.assertEqual(self.actions, [])
        self.assertEqual(self.database.execute("SELECT count(*) FROM application_jobs WHERE state='DUPLICATE'").fetchone()[0], 2)

    def test_04_unknown_required_answers_not_invented(self):
        self.discover()
        self.provider.customize = lambda result, job: result["questions"].append({"id": "citizenship", "semantic": "citizenship", "required": True})
        self.drive()
        self.assertIn("ANSWERING_RETRY", self.job()["blocker"])
        self.assertNotIn("submit", self.actions)

    def test_05_transient_read_failures_exhaust_bounded_budget(self):
        self.discover()
        self.provider.inspect_failures = 5
        for _ in range(3):
            self.cycle()
            self.service()
            self.advance()
            self.cycle()
            self.advance(3600)
        self.assertEqual(self.job()["attempts"], 3)
        self.assertEqual(self.job()["state"], "EXHAUSTED")
        self.assertEqual(self.actions, ["inspect"] * 3)

    def test_submit_failure_before_its_request_row(self):
        """Compute wait before the row: free wait. Other failure before the row: closed, try spent.
        Failure after the row exists: unchanged uncertain path, recorded."""
        real = desktop_adapter.DesktopAdapter.call
        def pending(adapter, operation, payload):
            if operation == "submit":
                raise applications.AdapterPending("capacity", desktop_adapter.later(self.now, 3600))
            return real(adapter, operation, payload)
        def after_row(adapter, operation, payload):
            if operation == "submit":
                self.database.execute("INSERT INTO desktop_requests(request_id,binding,job_key,operation,payload,state,created_at,expires_at,attempt_id) "
                                      "VALUES ('row','row',?,'submit','{}','PENDING',?,?,?)",
                                      (payload["job"]["job_key"], self.now, self.now, payload["attempt_id"]))
                self.database.commit()
                raise RuntimeError("after the row")
            return real(adapter, operation, payload)
        for label, patcher, state, attempts, attempt_state in (
                ("wait", patch.object(desktop_adapter.DesktopAdapter, "call", pending), "WAITING_BROWSER", 0, "CONFIRMED_NOT_SUBMITTED"),
                ("error", patch("cover_letter.prepare", side_effect=KeyError("questions")), "RETRY_WAIT", 1, "CONFIRMED_NOT_SUBMITTED"),
                ("row", patch.object(desktop_adapter.DesktopAdapter, "call", after_row), "RECONCILE", 1, "SUBMITTING")):
            with self.subTest(label):
                self.database.close()
                self.setUp()
                self.discover()
                with patcher:
                    self.drive(rounds=1)
                self.assertEqual((self.job()["state"], self.job()["attempts"]), (state, attempts))
                self.assertEqual(self.database.execute("SELECT state FROM application_attempts").fetchone()[0], attempt_state)

    def test_06_lost_submit_response_reconciles_without_second_submit(self):
        self.discover()
        self.provider.timeout_after_submit = True
        self.drive()
        self.assertEqual(self.job()["state"], "SUBMITTED")
        self.assertEqual(self.actions.count("submit"), 1)
        self.assertEqual(self.actions.count("reconcile"), 1)

    def test_07_blocked_role_does_not_stop_next_role(self):
        blocked = self.discover("Blocked AI", "123")
        ready = self.discover("Ready AI", "456")
        def customize(result, job):
            if job["job_key"] == blocked:
                result["facts"]["citizenship_clearance_requirements_met"] = False
        self.provider.customize = customize
        self.drive()
        self.assertEqual((self.job(blocked)["state"], self.job(blocked)["blocker"]), ("INELIGIBLE", "CLEARANCE_OR_CITIZENSHIP_REQUIRED"))
        self.assertEqual(self.job(ready)["state"], "SUBMITTED")

    def test_08_read_only_consumer_cannot_claim_or_arm_submit(self):
        self.discover()
        self.cycle()
        request = self.claim(read_only=True)
        with self.assertRaisesRegex(ValueError, "READ_ONLY"):
            desktop_adapter.arm(self.database, self.root, request, self.now)
        self.complete(request, fixtures.review(request["payload"]["job"], self.now))
        self.advance()
        self.cycle()
        self.assertIsNone(self.claim(read_only=True))
        self.assertEqual(self.job()["state"], "SUBMITTING")

    def test_09_auth_blocker_notifies_user_and_failed_delivery_stays_pending(self):
        self.discover()
        self.cycle()
        request = self.claim()
        self.complete(request, {"blocked": {"code": "AUTHENTICATION_REQUIRED", "next_action": "Sign in to this employer portal using the existing account"}})
        self.advance()
        self.cycle()
        self.assertEqual(self.database.execute("SELECT owner FROM application_blockers").fetchone()[0], "user")
        sender = fixtures.MockTelegram([notifications.Transient(1)])
        notifications.dispatch(self.database, {}, sender, self.now)
        self.assertEqual(self.database.execute("SELECT state FROM notification_outbox").fetchone()[0], "PENDING")
        self.advance(3600)
        notifications.dispatch(self.database, {}, sender, self.now)
        self.assertEqual(self.database.execute("SELECT state FROM notification_outbox").fetchone()[0], "DELIVERED")

    def test_10_restart_preserves_pending_request_and_suppression(self):
        self.discover()
        self.cycle()
        request_id = self.database.execute("SELECT request_id FROM desktop_requests").fetchone()[0]
        self.database.close()
        self.database = orchestrator.connect(self.root)
        self.advance()
        self.cycle()
        self.assertEqual(self.database.execute("SELECT request_id FROM desktop_requests").fetchone()[0], request_id)
        self.drive()
        self.discover()
        self.drive()
        self.assertEqual(self.actions.count("submit"), 1)
        self.assertEqual(self.database.execute("SELECT count(*) FROM application_jobs").fetchone()[0], 1)

    def test_waiting_does_not_consume_application_or_reconcile_budget(self):
        self.discover()
        self.cycle()
        self.advance()
        self.cycle()
        self.assertEqual(self.job()["attempts"], 0)
        self.service()
        self.advance()
        self.cycle()
        for _ in range(3):
            self.advance()
            self.cycle()
        self.assertEqual(self.job()["attempts"], 1)
        self.assertEqual(self.job()["reconcile_attempts"], 0)
        self.assertEqual(self.database.execute("SELECT count(*) FROM desktop_requests WHERE operation='submit'").fetchone()[0], 1)

    def test_claim_is_exclusive_and_old_token_cannot_complete_after_reclaim(self):
        self.discover()
        self.cycle()
        first = self.claim()
        self.assertIsNone(self.claim())
        self.advance(desktop_adapter.LEASE_SECONDS + 1)
        second = self.claim()
        self.assertEqual(first["request_id"], second["request_id"])
        with self.assertRaisesRegex(ValueError, "STALE"):
            self.complete(first, {})
        self.complete(second, {"blocked": {"code": "CAPTCHA_REQUIRED", "next_action": "Complete CAPTCHA yourself"}})

    def test_claim_result_bound_to_payload_and_not_replayable(self):
        self.discover()
        self.cycle()
        request = self.claim()
        with self.assertRaisesRegex(ValueError, "MISMATCHED"):
            self.complete(dict(request, binding="tampered"), {})
        self.complete(request, {"blocked": {"code": "MFA_REQUIRED", "next_action": "Complete MFA"}})
        with self.assertRaisesRegex(ValueError, "STALE"):
            self.complete(request, {})

    def submit_request(self):
        self.discover()
        self.cycle()
        self.service()
        self.advance()
        self.cycle()
        return self.claim()

    def test_arm_rechecks_duplicate_history(self):
        request = self.submit_request()
        with self.database:
            self.database.execute("INSERT INTO application_history VALUES ('example ai','123','APPLICATION','arrived during browser wait')")
        with self.assertRaisesRegex(ValueError, "CLAIM_INVALID"):
            desktop_adapter.arm(self.database, self.root, dict(request, observed_form_revision="mock-form-v1"), self.now)

    def test_arm_rejects_changed_resume(self):
        request = self.submit_request()
        (self.root / "mock-resume.pdf").write_bytes(b"changed resume")
        with self.assertRaisesRegex(ValueError, "RESUME_MISMATCH"):
            desktop_adapter.arm(self.database, self.root, dict(request, observed_form_revision="mock-form-v1"), self.now)

    def test_submit_arm_is_once_only_and_expired_claim_never_reissued(self):
        request = self.submit_request()
        envelope = dict(request, observed_form_revision="mock-form-v1")
        desktop_adapter.arm(self.database, self.root, envelope, self.now)
        with self.assertRaisesRegex(ValueError, "ALREADY_CONSUMED"):
            desktop_adapter.arm(self.database, self.root, envelope, self.now)
        self.advance(desktop_adapter.LEASE_SECONDS + 1)
        self.assertIsNone(self.claim())
        self.cycle()
        next_request = self.claim()
        self.assertEqual(next_request["payload"]["operation"], "reconcile")
        with self.assertRaisesRegex(ValueError, "STALE"):
            self.complete(request, {"acknowledged": True})
        self.assertEqual(self.database.execute("SELECT count(*) FROM desktop_requests WHERE operation='submit'").fetchone()[0], 1)

    def test_armed_page_confirmation_is_the_submission_receipt(self):
        # Owner, 2026-10-06: "If the agent captures a page confirmation, that should be useed as a
        # verified submission. no need for any additional python comparisons".
        request = self.submit_request()
        with self.assertRaisesRegex(ValueError, "SUBMISSION_NOT_ARMED"):
            self.complete(request, {"acknowledged": True, "confirmation_text": "Thanks for applying"})
        desktop_adapter.arm(self.database, self.root, dict(request, observed_form_revision="mock-form-v1"), self.now)
        self.complete(request, {"status": "SUBMITTED", "verified": True, "acknowledged": True,
                                "confirmation_text": "Thanks for applying"})
        self.advance()
        self.cycle()
        self.assertEqual(self.job()["state"], "SUBMITTED")
        receipt = json.loads(self.job()["receipt"])
        self.assertEqual((receipt["evidence_kind"], receipt["confirmation_text"]), ("page_confirmation", "Thanks for applying"))
        self.assertIsNone(self.claim())
        self.assertEqual(self.database.execute("SELECT count(*) FROM desktop_requests WHERE operation IN ('verify','reconcile')").fetchone()[0], 0)

    def test_absence_requires_independent_authoritative_source(self):
        request = self.submit_request()
        # Armed: a submission may have happened, so absence needs an authoritative source. (An
        # unarmed session cannot have submitted and is resolved structurally - patches CA/CK.)
        desktop_adapter.arm(self.database, self.root, dict(request, observed_form_revision="mock-form-v1"), self.now)
        self.advance(desktop_adapter.LEASE_SECONDS + 1)
        self.cycle()
        verification = self.claim()
        with self.assertRaisesRegex(ValueError, "AUTHORITATIVE_ABSENCE"):
            self.complete(verification, {"status": "NOT_SUBMITTED", "no_inflight": True})

    def test_unserviced_reads_cost_no_try(self):
        # A read no session ever picked up is a compute wait, not a failure (owner, 2026-09-28):
        # Gringotts Industries was marked EXHAUSTED by one. Each expired unclaimed read is replaced.
        self.discover()
        for _ in range(3):
            self.cycle()
            self.advance(desktop_adapter.REQUEST_SECONDS + 1)
            self.cycle()
            self.advance(3600)
        self.assertNotIn(self.job()["state"], applications.FINAL)
        self.assertEqual(self.job()["attempts"], 0)
        states = [row[0] for row in self.database.execute("SELECT state FROM desktop_requests WHERE operation='inspect' ORDER BY rowid")]
        self.assertEqual(states[-1], "PENDING")
        self.assertTrue(set(states[:-1]) <= {"CONSUMED"}, states)


if __name__ == "__main__":
    unittest.main()
