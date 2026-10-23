"""A captured page confirmation is the verified submission; the employer-wide wait is gone.

Owner, 2026-10-06: "If the agent captures a page confirmation, that should be useed as a verified
submission. no need for any additional python comparisons" and "Okay remove that Blcok employers
constraint ... completely remove it."
"""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import json
import unittest
from unittest.mock import patch

import application_browser
import applications
import desktop_adapter
import manual_application_receipts as manual


class PageConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = manual.Fixture()
        self.addCleanup(self.fixture.close)

    def submit(self, result, armed=True):
        fixture = self.fixture
        request = fixture.prepare(armed=False)
        if armed:
            desktop_adapter.arm(fixture.database, fixture.root, dict(request, observed_form_revision="synthetic-v1"), fixture.now)
            desktop_adapter.complete(fixture.database, dict(request, result=result), fixture.now)
        return request

    def assert_page_receipt(self, confirmation):
        fixture = self.fixture
        job = fixture.job()
        self.assertEqual(job["state"], "SUBMITTED")
        receipt = json.loads(job["receipt"])
        self.assertEqual((receipt["evidence_kind"], receipt["confirmation_text"]), ("page_confirmation", confirmation))
        self.assertEqual(fixture.database.execute("SELECT state FROM application_attempts").fetchone()[0], "VERIFIED")
        self.assertEqual(fixture.database.execute(
            "SELECT count(*) FROM desktop_requests WHERE operation IN ('verify','reconcile')").fetchone()[0], 0)

    def test_armed_acknowledged_submit_settles_without_a_verify_read(self):
        self.submit({"acknowledged": True, "confirmation_text": "Thank you for applying to Synthetic"})
        self.assertEqual(applications.settle_page_confirmations(self.fixture.database, self.fixture.now), [self.fixture.key])
        self.assert_page_receipt("Thank you for applying to Synthetic")

    def test_email_application_keeps_its_sent_folder_reconcile(self):
        fixture = self.fixture
        self.submit({"acknowledged": True, "confirmation_text": "Message sent"})
        with fixture.database:
            evidence = json.loads(fixture.database.execute("SELECT evidence FROM application_attempts").fetchone()[0] or "{}")
            evidence["review"] = dict(evidence.get("review") or {}, application_method="EMAIL")
            fixture.database.execute("UPDATE application_attempts SET evidence=?", (json.dumps(evidence),))
        self.assertEqual(applications.settle_page_confirmations(fixture.database, fixture.now), [])
        self.assertNotEqual(fixture.job()["state"], "SUBMITTED")

    def test_unarmed_acknowledgment_does_not_settle(self):
        fixture = self.fixture
        request = self.submit(None, armed=False)
        with fixture.database:
            fixture.database.execute("UPDATE desktop_requests SET state='DONE',result=? WHERE request_id=?",
                                     (json.dumps({"acknowledged": True, "confirmation_text": "Thanks"}), request["request_id"]))
        self.assertEqual(applications.settle_page_confirmations(fixture.database, fixture.now, fixture.root), [])
        self.assertNotEqual(fixture.job()["state"], "SUBMITTED")
        self.assertEqual(fixture.database.execute("SELECT state FROM application_attempts").fetchone()[0], "SUBMITTING")

    def test_session_report_acknowledgment_settles_a_stored_false(self):
        fixture = self.fixture
        request = self.submit({"acknowledged": False})
        folder = fixture.root / "reports" / "application-browser" / "request-fixture"
        folder.mkdir(parents=True)
        (folder / "request.json").write_text(json.dumps({"request_id": request["request_id"]}))
        (folder / "output.json").write_text(json.dumps({"result_json": json.dumps(
            {"acknowledged": True, "confirmation_text": "Your application was received"})}))
        with patch.dict(application_browser._SESSION_FOLDERS, clear=True):
            self.assertEqual(applications.settle_page_confirmations(fixture.database, fixture.now), [])
            self.assertEqual(applications.settle_page_confirmations(fixture.database, fixture.now, fixture.root), [fixture.key])
        self.assert_page_receipt("Your application was received")

    def test_unsettled_submission_elsewhere_at_the_employer_no_longer_holds(self):
        fixture = self.fixture
        with fixture.database:
            other = applications.ingest(fixture.database, {"company": "Synthetic", "role": "Staff Software Engineer",
                                                           "url": "https://synthetic.example/careers/staff"}, fixture.now)
            fixture.database.execute("UPDATE application_jobs SET state='RECONCILE' WHERE job_key=?", (fixture.key,))
        job = dict(fixture.database.execute("SELECT * FROM application_jobs WHERE job_key=?", (other,)).fetchone())
        self.assertTrue(applications.weak_identity(job["requisition"]))
        self.assertIsNone(applications.local_gate(fixture.database, job, fixture.now))


class QuestionChoicesTests(unittest.TestCase):
    """A menu the submit session saw for a question the read listed without one (Woodgrove, 2026-10-06)."""

    def test_observed_choices_are_recorded_and_the_stale_answer_dropped(self):
        import sqlite3
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        applications.initialize(database)
        job_key = applications.ingest(database, {"company": "Woodgrove", "role": "Senior DevOps Engineer",
                                                 "url": "https://job-boards.greenhouse.io/woodgrove/jobs/1"}, "2026-10-06T00:00:00+00:00")
        job = dict(database.execute("SELECT * FROM application_jobs WHERE job_key=?", (job_key,)).fetchone())
        question = {"id": "q1", "label": "When could you start a new role?", "semantic": "start_timeline",
                    "required": True, "answer_type": "choice"}
        field = applications.question_field(question)
        columns = [row[1] for row in database.execute("PRAGMA table_info(application_requisition_records)")]
        values = {name: None for name in columns}
        values.update(job_key=job_key, employer=job["employer"], requisition=job["requisition"], questions=json.dumps([question]),
                      conditional_questions="[]", identity_sha256=applications.identity_fingerprint(job), complete=1,
                      observed_at="2026-10-06T00:00:00+00:00", updated_at="2026-10-06T00:00:00+00:00")
        for name, kind, notnull in [(row[1], row[2], row[3]) for row in database.execute("PRAGMA table_info(application_requisition_records)")]:
            if notnull and values[name] is None:
                values[name] = 0 if "INT" in str(kind).upper() else ""
        database.execute(f"INSERT INTO application_requisition_records ({','.join(columns)}) VALUES ({','.join('?' * len(columns))})",
                         [values[name] for name in columns])
        database.execute("INSERT INTO application_answers VALUES (?,?,?,?,?)",
                         (job_key, field, "start_timeline", json.dumps("4 weeks from application date"), "ai-answer:test"))
        observed = [dict(question, field=field, choices=["Right away", "In about 2 weeks", "In over a month",
                                                         "Open, let's discuss"])]
        self.assertTrue(applications.record_conditional_questions(database, job, observed, "2026-10-06T01:00:00+00:00"))
        self.assertIsNone(database.execute("SELECT 1 FROM application_answers WHERE job_key=? AND field=?", (job_key, field)).fetchone())
        review = {"questions": [dict(question)]}
        recorded = json.loads(database.execute("SELECT conditional_questions FROM application_requisition_records").fetchone()[0])
        self.assertEqual(recorded[0]["choices"][2], "In over a month")
        # A second report of the same menu changes nothing.
        self.assertFalse(applications.record_conditional_questions(database, job, observed, "2026-10-06T02:00:00+00:00"))
        merged = applications.complete_review(database, job, review, "2026-10-06T02:00:00+00:00")
        self.assertEqual(merged["questions"][0]["choices"][2], "In over a month")


if __name__ == "__main__":
    unittest.main()
