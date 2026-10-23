import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import copy
import json
import unittest
from unittest.mock import patch

import applications
import desktop_adapter
import manual_application_receipts as manual


class ApplicationReceiptTests(unittest.TestCase):
    def setUp(self):
        self.fixture = manual.Fixture()
        self.addCleanup(self.fixture.close)

    def complete_read(self, status="SUBMITTED", operation="verify"):
        fixture = self.fixture
        request = fixture.claim(fixture.queue(operation, {"job": fixture.job()}))
        desktop_adapter.complete(fixture.database, dict(request, result=fixture.receipt(status)), fixture.now)
        return request

    def test_restart_and_rollback_retain_receipt_past_transport_expiry(self):
        manual.check_recovery()

    def test_absence_requires_account_attempt_and_post_attempt_observation(self):
        manual.check_absence()

    def test_unarmed_blocker_revokes_claim_without_releasing_attempt(self):
        manual.check_blocker()

    def test_post_submit_blocker_reaches_user_hold_without_verification(self):
        manual.check_submission_blocker_handoff()

    def test_blocked_reconciliation_preserves_budget_and_resumes_after_resolution(self):
        manual.check_blocked_reconciliation()

    def test_cached_inspect_uses_same_binding_without_mutation(self):
        fixture = self.fixture
        payload = {"job": fixture.job()}
        request = fixture.claim(fixture.queue("inspect", payload))
        desktop_adapter.complete(fixture.database, dict(request, result=fixture.review), fixture.now)
        before = list(fixture.database.iterdump())
        self.assertEqual(fixture.adapter.cached("inspect", payload), fixture.review)
        self.assertTrue(fixture.adapter.cached_available("inspect", payload))
        self.assertEqual(before, list(fixture.database.iterdump()))
        changed = copy.deepcopy(payload)
        changed["job"]["payload"] = json.dumps({"company": "Synthetic", "url": "https://jobs.ashbyhq.com/synthetic/role-123", "updated": True})
        self.assertIsNone(fixture.adapter.cached("inspect", changed))

    def test_modified_cached_receipt_cannot_dispose_durable_result(self):
        fixture = self.fixture
        fixture.prepare()
        request = self.complete_read()
        result = fixture.adapter.cached("reconcile", {"job": fixture.job()})
        result["receipt_id"] = "invented"
        with self.assertRaisesRegex(ValueError, "BINDING_MISMATCH"), fixture.database:
            applications.finish_receipt(fixture.database, fixture.job(), result, fixture.now)
        self.assertEqual(fixture.database.execute("SELECT state FROM desktop_requests WHERE request_id=?", (request["request_id"],)).fetchone()[0], "DONE")
        self.assertNotEqual(fixture.job()["state"], "SUBMITTED")

    def test_older_attempt_absence_cannot_clear_new_attempt(self):
        fixture = self.fixture
        fixture.prepare()
        self.complete_read("NOT_SUBMITTED", "reconcile")
        old_result = fixture.adapter.cached("reconcile", {"job": fixture.job()})
        applications.reconcile(fixture.database, fixture.job(), fixture.adapter, fixture.policy, fixture.now)
        fixture.now = desktop_adapter.later(fixture.now, 901)
        fixture.review["observed_at"] = fixture.now
        request = fixture.claim(fixture.queue("inspect", {"job": fixture.job()}))
        desktop_adapter.complete(fixture.database, dict(request, result=fixture.review), fixture.now)
        applications.process(fixture.database, fixture.job(), fixture.adapter, fixture.policy, fixture.root, fixture.now)
        outstanding = fixture.database.execute("SELECT attempt_id FROM application_attempts WHERE state='SUBMITTING'").fetchone()[0]
        with patch.object(fixture.adapter, "call", return_value=old_result):
            with self.assertRaises(ValueError):
                applications.reconcile(fixture.database, fixture.job(), fixture.adapter, fixture.policy, fixture.now)
        self.assertEqual(fixture.database.execute("SELECT state FROM application_attempts WHERE attempt_id=?", (outstanding,)).fetchone()[0], "SUBMITTING")

    def test_positive_receipt_requires_provider_source_and_account(self):
        fixture = self.fixture
        fixture.prepare()
        request = fixture.claim(fixture.queue("verify", {"job": fixture.job()}))
        # provider_message_or_application_id is no longer required (owner, 2026-09-21): nothing
        # reads it to decide anything, so an empty one is not grounds to refuse a real receipt.
        for field, value in {"source": "", "candidate_email": "wrong@example.invalid",
                             "evidence_kind": "success_page", "observed_at": desktop_adapter.later(fixture.now, -1)}.items():
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "INDEPENDENT_EMPLOYER"):
                desktop_adapter.complete(fixture.database, dict(request, result=dict(fixture.receipt(), **{field: value})), fixture.now)

    def test_the_reads_employer_judgement_settles_only_a_submitted_receipt(self):
        # Owner, 2026-10-07 (item 1): a confirmation email naming the employer's legal name is
        # accepted on the read's employer_matches_expected; a "not submitted" read never is.
        fixture = self.fixture
        fixture.prepare()
        request = fixture.claim(fixture.queue("verify", {"job": fixture.job()}))
        legal = {"employer": "Synthetic Labs, Inc.", "employer_matches_expected": True}
        with self.assertRaisesRegex(ValueError, "AUTHORITATIVE_ABSENCE_REQUIRED"):
            desktop_adapter.complete(fixture.database, dict(
                request, result=dict(fixture.receipt("NOT_SUBMITTED"), **legal)), fixture.now)
        email = dict(fixture.receipt(), evidence_kind="employer_confirmation_email", requisition=None, **legal)
        with self.assertRaisesRegex(ValueError, "INDEPENDENT_EMPLOYER"):
            desktop_adapter.complete(fixture.database, dict(
                request, result=dict(email, employer_matches_expected=False)), fixture.now)
        desktop_adapter.complete(fixture.database, dict(request, result=email), fixture.now)

    def test_block_api_rejects_armed_read_expired_and_malformed_claims(self):
        fixture = self.fixture
        request = fixture.prepare(armed=False)
        for result in ({"blocked": {"code": "CAPTCHA_REQUIRED"}}, {"blocked": {"code": "CAPTCHA_REQUIRED", "next_action": "Resolve"}, "status": "NOT_SUBMITTED"}):
            with self.assertRaises(ValueError):
                desktop_adapter.block(fixture.database, dict(request, result=result), fixture.now)
        desktop_adapter.arm(fixture.database, fixture.root, dict(request, observed_form_revision="synthetic-v1"), fixture.now)
        blocker = {"blocked": {"code": "CAPTCHA_REQUIRED", "next_action": "Resolve"}}
        with self.assertRaisesRegex(ValueError, "UNARMED"):
            desktop_adapter.block(fixture.database, dict(request, result=blocker), fixture.now)
        desktop_adapter.complete(fixture.database, dict(request, result={"acknowledged": True}), fixture.now)
        read = fixture.claim(fixture.queue("verify", {"job": fixture.job()}))
        with self.assertRaisesRegex(ValueError, "UNARMED"):
            desktop_adapter.block(fixture.database, dict(read, result=blocker), fixture.now)
        with self.assertRaisesRegex(ValueError, "STALE"):
            desktop_adapter.block(fixture.database, dict(read, result=blocker), read["lease_until"])

    def test_unknown_read_is_consumed_with_reconciliation_disposition(self):
        fixture = self.fixture
        fixture.prepare()
        request = fixture.claim(fixture.queue("reconcile", {"job": fixture.job()}))
        desktop_adapter.complete(fixture.database, dict(request, result={"status": "UNKNOWN"}), fixture.now)
        applications.reconcile(fixture.database, fixture.job(), fixture.adapter, fixture.policy, fixture.now)
        self.assertEqual(fixture.job()["reconcile_attempts"], 1)
        self.assertEqual(fixture.database.execute("SELECT state FROM desktop_requests WHERE request_id=?", (request["request_id"],)).fetchone()[0], "CONSUMED")
        self.assertIsNone(fixture.adapter.cached("reconcile", {"job": fixture.job()}))


if __name__ == "__main__":
    unittest.main()
