"""K10 a (owner, 2026-11-23): employer mail that clearly concerns THIS application (a rejection, an interview
invitation, any reply about the role) proves it was received. The AI judges the mail; Python checks only the
binding it already checks for an employer confirmation email. Never a second submit."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import json
import unittest
from unittest.mock import patch

import applications
import conversion
import applications_page
import desktop_adapter
import manual_application_receipts as manual


class EmployerReplyReceipt(unittest.TestCase):
    def setUp(self):
        self.fixture = manual.Fixture()
        self.addCleanup(self.fixture.close)
        self.fixture.prepare()
        conversion.initialize(self.fixture.database)
        self.fixture.database.commit()

    def mail(self, outcome="REJECTED", **changes):
        return dict(self.fixture.receipt(), evidence_kind="employer_confirmation_email", requisition=None,
                    employer_outcome=outcome, **changes)

    def read(self, result):
        fixture = self.fixture
        request = fixture.claim(fixture.queue("reconcile", {"job": fixture.job()}))
        desktop_adapter.complete(fixture.database, dict(request, result=result), fixture.now)

    def settle(self):
        fixture = self.fixture
        with patch.object(fixture.adapter, "call", side_effect=AssertionError("no new browser work")):
            with fixture.database:
                fixture.database.execute("UPDATE application_jobs SET reconcile_attempts=3")
            applications.reconcile(fixture.database, fixture.job(), fixture.adapter, fixture.policy, fixture.now)
        return fixture.job()

    def submits(self):
        return self.fixture.database.execute("SELECT COUNT(*) FROM desktop_requests WHERE operation='submit'").fetchone()[0]

    def test_a_rejection_settles_the_unknown_outcome_as_applied_with_its_outcome(self):
        before = self.submits()
        self.read(self.mail("REJECTED"))
        job = self.settle()
        self.assertEqual(job["state"], "SUBMITTED")
        self.assertEqual(self.submits(), before)                               # never re-submitted
        shown = applications_page._receipt(job["state"], job["receipt"])
        self.assertEqual(shown["words"], "The employer replied: they are not moving forward")
        self.assertEqual(json.loads(job["receipt"])["employer_outcome"], "REJECTED")

    def test_k29_a_receipt_with_no_message_id_still_settles(self):
        result = self.mail("REJECTED")
        result.pop("receipt_id", None)
        self.read(result)
        job = self.settle()
        self.assertEqual(job["state"], "SUBMITTED")
        conversion.cycle(self.fixture.database, {}, desktop_adapter.later(self.fixture.now, 60))
        self.assertEqual([row[0] for row in self.fixture.database.execute(
            "SELECT kind FROM conversion_outcomes")], ["REJECTED"])

    def test_only_a_known_outcome_on_an_employer_email_is_taken(self):
        for case in ("unknown outcome",):   # K28: only the enum is checked
            with self.subTest(case=case):
                self.setUp()
                result = self.mail("MAYBE") if case == "unknown outcome" else dict(self.fixture.receipt(), employer_outcome="REJECTED")
                with self.assertRaisesRegex(ValueError, "INDEPENDENT_EMPLOYER"):
                    self.read(result)

    def outcomes(self):
        conversion.cycle(self.fixture.database, {}, desktop_adapter.later(self.fixture.now, 60))
        return [row[0] for row in self.fixture.database.execute(
            "SELECT kind FROM conversion_outcomes WHERE job_key=?", (self.fixture.key,))]

    def test_k16_a_rejection_settles_the_job_and_records_a_rejected_outcome(self):
        self.read(self.mail("REJECTED"))
        self.assertEqual(self.settle()["state"], "SUBMITTED")
        self.assertEqual(self.outcomes(), ["REJECTED"])
        # K22: the recorded statement is labelled as Sidedoor's summary; verified_by names the settling read.
        record = json.loads(self.fixture.database.execute(
            "SELECT record FROM conversion_evidence WHERE kind='OUTCOME'").fetchone()[0])
        self.assertEqual(record["facts"]["statement_kind"], "SIDEDOOR_SUMMARY")
        self.assertTrue(record["facts"]["statement"].startswith("Sidedoor's summary, not the employer's words"))
        self.assertEqual(record["verified_by"], "application_browser:reconcile-receipt")

    def test_k16_an_interview_reply_records_the_interview_outcome_and_a_plain_reply_nothing(self):
        self.read(self.mail("INTERVIEW"))
        self.assertEqual(self.settle()["state"], "SUBMITTED")
        self.assertEqual(self.outcomes(), ["INTERVIEW"])
        self.setUp()
        self.read(self.mail("REPLY"))
        self.assertEqual(self.settle()["state"], "SUBMITTED")
        self.assertEqual(self.outcomes(), [])


if __name__ == "__main__":
    unittest.main()
