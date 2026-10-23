import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import applications
import ats_browser
import test_applications_e2e as fixtures


def fields():
    return ats_browser.normalize_fields([{"dom_id": "email", "name": "email", "label": "Email*", "type": "email", "required": True, "role": None, "value": "", "options": []}])


class FakePage:
    submits = 0
    fills = 0

    def __init__(self, *_):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def fields(self):
        return fields()

    def fill(self, *_):
        type(self).fills += 1

    def submit(self):
        type(self).submits += 1

    def observation(self):
        return {"url": "https://job-boards.greenhouse.io/example/jobs/123", "text": "Mock confirmation is NOT an independent employer receipt"}


class BrowserAdapterTests(unittest.TestCase):
    setUpBase = fixtures.ApplicationEndToEndTests.setUp

    def setUp(self):
        self.setUpBase()
        self.config = {"execution_ownership_verified": True}
        (self.root / "config.json").write_text(json.dumps(self.config))
        self.now = dt.datetime.now(dt.timezone.utc).isoformat()
        self.role = {"company": "Example AI", "requisition_id": "123", "role": "Staff Software Engineer", "url": "https://job-boards.greenhouse.io/example/jobs/123", "rationale": "FIXTURE ONLY"}
        applications.ingest(self.database, self.role, self.now)
        self.database.commit()
        self.job = dict(self.database.execute("SELECT * FROM application_jobs").fetchone())
        self.job["candidate_email"] = self.policy["answers"]["email"]["value"]
        self.review = fixtures.review(self.job, self.now)
        self.review["form_revision"] = ats_browser.form_revision(fields())
        self.review["questions"] = fields()
        FakePage.submits = FakePage.fills = 0
        self.browser = ats_browser.Adapter(self.root, FakePage)

    def claim(self):
        answers = {"email": self.job["candidate_email"]}
        with self.database:
            self.database.execute("UPDATE application_jobs SET state='SUBMITTING'")
            self.database.execute("INSERT INTO application_attempts(attempt_id,job_key,idempotency_key,state,started_at,evidence) VALUES ('fixture',?,?,'SUBMITTING',?,?)", (self.job["job_key"], self.job["job_key"], self.now, json.dumps({"answers": answers, "review": self.review})))
        return {"job": self.job, "idempotency_key": self.job["job_key"], "answers": answers,
                "form_revision": self.review["form_revision"], "resume": str(self.root / self.policy["resume"]["path"])}

    def test_destination_restricts_provider_and_requisition(self):
        self.assertEqual(ats_browser.destination(self.job)[0], "greenhouse")
        for url in ["https://127.0.0.1/jobs/123", "https://job-boards.greenhouse.io.evil.example/example/jobs/123", "https://job-boards.greenhouse.io/example/jobs/999", "https://user@job-boards.greenhouse.io/example/jobs/123"]:
            with self.subTest(url=url), self.assertRaises(ats_browser.Blocked):
                ats_browser.destination(dict(self.job, payload=json.dumps(dict(self.role, url=url))))

    def test_live_form_does_not_invent_company_or_history_evidence(self):
        result = self.browser.call("inspect", {"job": self.job})
        self.assertEqual(result["blocked"]["code"], "SOURCED_ELIGIBILITY_REVIEW_REQUIRED")
        self.assertEqual(FakePage.fills, 0)
        self.assertEqual(len(list(self.browser.directory.glob('*-inspect.json'))), 1)

    def test_adapter_blocker_is_not_a_transient_attempt(self):
        result = applications.cycle(self.database, {}, self.root, self.browser)
        self.assertEqual(result["applications"][0]["blocker"], "SOURCED_ELIGIBILITY_REVIEW_REQUIRED")
        self.assertEqual(result["applications"][0]["attempts"], 0)

    def test_submit_requires_worker_claim(self):
        result = self.browser.call("submit", {"job": self.job})
        self.assertEqual(result["blocked"]["code"], "DURABLE_SUBMISSION_CLAIM_REQUIRED")
        self.assertEqual(FakePage.fills, 0)

    def test_browser_ack_is_unknown_and_claim_cannot_click_twice(self):
        payload = self.claim()
        self.assertTrue(self.browser.call("submit", payload)["requires_independent_receipt"])
        self.assertEqual(self.browser.call("verify", {"job": self.job})["status"], "UNKNOWN")
        self.assertEqual(self.browser.call("submit", payload)["reason"], "BROWSER_CLAIM_ALREADY_CONSUMED")
        self.assertEqual(FakePage.submits, 1)

    def test_tampered_answers_never_reach_browser(self):
        payload = self.claim()
        payload["answers"] = {"email": "wrong@example.invalid"}
        self.assertEqual(self.browser.call("submit", payload)["blocked"]["code"], "SUBMISSION_PAYLOAD_MISMATCH")
        self.assertEqual(FakePage.fills, 0)

    def test_changed_form_never_fills_or_submits(self):
        payload = self.claim()
        with patch.object(FakePage, "fields", return_value=[]):
            result = self.browser.call("submit", payload)
        self.assertEqual(result["blocker"], "APPLICATION_FORM_CHANGED")
        self.assertEqual(FakePage.fills, 0)
        self.assertEqual(FakePage.submits, 0)

    def test_resume_changed_after_claim_never_uploads(self):
        payload = self.claim()
        Path(payload["resume"]).write_bytes(b'changed fixture')
        self.assertEqual(self.browser.call("submit", payload)["blocked"]["code"], "DESIGNATED_RESUME_MISMATCH")
        self.assertEqual(FakePage.fills, 0)

    def test_receipt_requires_independent_provider_record(self):
        directory = self.root / "state/ats-receipts"
        directory.mkdir()
        path = directory / (ats_browser.fingerprint(self.job["job_key"]) + '.json')
        receipt = dict(fixtures.evidence(self.now), status="SUBMITTED", independent_read=True, employer=self.job["employer"], requisition=self.job["requisition"], candidate_email=self.job["candidate_email"], receipt_id="mock-provider-message-1")
        path.write_text(json.dumps(receipt))
        self.assertEqual(self.browser.receipt(self.job)["status"], "UNKNOWN")
        receipt.update(provider_message_or_application_id="mock-provider-message-1", evidence_kind="employer_confirmation_email")
        path.write_text(json.dumps(receipt))
        self.assertEqual(self.browser.receipt(self.job)["receipt_id"], "mock-provider-message-1")

    def test_semantic_answer_mapping_is_exact_and_rejects_conflicts(self):
        questions = [{"id": "vendor_question_123", "semantic": "contact_email", "required": True}]
        answers, missing = applications.resolve_answers(questions, self.policy)
        # Keyed by applications.question_field, not the vendor's reported id (DU, 2026-09-21).
        vendor_field = applications.question_field(questions[0])
        self.assertEqual(answers, {vendor_field: self.policy["answers"]["email"]["value"]})
        self.assertEqual(missing, [])
        policy = dict(self.policy, answers=dict(self.policy["answers"], conflict={"semantic": "contact_email", "source": "different verified source", "value": "conflict@example.invalid"}))
        self.assertEqual(applications.resolve_answers(questions, policy)[1], [vendor_field])
