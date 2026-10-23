"""Duplicate-protection and verified-execution key normalization.

Every fixture here feeds values in the case a real employer email, ATS page or
browser would supply them ("Role-123", "Example AI", a non-breaking space).
Nothing is pre-normalized on the incoming side: the point of these tests is that
the production code, not the fixture, performs the normalization.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
import json
import unittest

import applications
import conversion
import external_history
import source_records
import test_applications_e2e as fixtures
import test_same_posting_before_submit_20261006 as same_posting
import test_conversion as conversion_fixtures


DISPLAYED = "Role-123"           # as an employer confirmation email renders it
CANONICAL = "role-123"           # as applications.identity() stores it


def employer_record(email, **changes):
    """A complete, authenticated Gmail employer-history record."""
    value = {"evidence_id": "gmail:fixture-123", "source_message_id": "fixture-123",
             "employer": "Example AI", "kind": "APPLICATION_CONFIRMED", "role_title": "Staff Software Engineer",
             "source": "https://mail.google.com/mail/u/0/#all/fixture-123", "requisition": None,
             "evidence_kind": "employer_email", "source_system": "employer ATS via Gmail",
             "verified": True, "sender_authenticated": True, "independent_read": True,
             "mailbox_email": email, "recipient": "alias@example.invalid",
             "delivered_to_owner": True, "sender": "careers@employer.example",
             "summary": "Synthetic employer application confirmation",
             "occurred_at": "2026-08-01T00:00:00+00:00", "observed_at": fixtures.NOW}
    value.update(changes)
    return value


class RequisitionCaseDuplicateTests(unittest.TestCase):
    """external_history.ingest -> applications.local_gate (the submission gate)."""

    setUp = fixtures.ApplicationEndToEndTests.setUp
    discover = fixtures.ApplicationEndToEndTests.discover
    cycle = fixtures.ApplicationEndToEndTests.cycle
    job = fixtures.ApplicationEndToEndTests.job
    submissions = fixtures.ApplicationEndToEndTests.submissions

    def judged_cycle(self, seconds=0):
        """A cycle whose same-posting judge answers DIFFERENT: the check before the first submit is
        the dedupe and holds the job when it cannot run (owner, 2026-10-06), so a job meant to be
        submitted here needs a judge that ran."""
        same_posting.workflow_tasks.initialize(self.database)  # the judge books compute
        self.adapter.same_job_executor = same_posting.judge("DIFFERENT")
        self.adapter.now = (dt.datetime.fromisoformat(fixtures.NOW) + dt.timedelta(seconds=seconds)).isoformat()
        return applications.cycle(self.database, same_posting.CONFIG, self.root, self.adapter, self.adapter.now)

    def email(self):
        return self.policy["answers"]["email"]["value"]

    def ingest(self, **changes):
        with self.database:
            return external_history.ingest(self.database, employer_record(self.email(), **changes), self.email())

    def confirmed(self, requisition, employer="Example AI"):
        return self.ingest(requisition=requisition, employer=employer,
                           requisition_source="requisition quoted in the synthetic employer confirmation")

    def test_displayed_case_requisition_confirms_duplicate_and_prevents_submission(self):
        """The employer email spells the requisition 'Role-123'; the job stores 'role-123'."""
        key = self.discover()
        self.confirmed(DISPLAYED)
        self.cycle()
        self.assertEqual(self.job(key)["state"], "DUPLICATE")
        self.assertEqual(self.job(key)["blocker"], "EXISTING_APPLICATION")
        self.assertEqual(self.submissions(), [])
        self.assertEqual(self.adapter.calls, [])

    def test_unicode_and_spacing_variant_requisition_still_confirms_duplicate(self):
        key = self.discover()
        self.confirmed("  ROLE-123  ")
        self.cycle()
        self.assertEqual(self.job(key)["state"], "DUPLICATE")
        self.assertEqual(self.submissions(), [])

    def test_local_gate_sees_the_confirmed_application_directly(self):
        key = self.discover()
        self.confirmed(DISPLAYED)
        decision = applications.local_gate(self.database, self.job(key), fixtures.NOW)
        self.assertEqual((decision[0], decision[1]), ("DUPLICATE", "EXISTING_APPLICATION"))

    def test_stored_history_requisition_is_written_in_the_lookup_key_space(self):
        self.discover()
        self.confirmed(DISPLAYED)
        stored = self.database.execute("SELECT employer,requisition FROM application_history").fetchall()
        self.assertEqual([tuple(row) for row in stored], [("example ai", CANONICAL)])

    # ---- indeterminate history must block, never permit -------------------

    def test_legacy_uncanonical_history_row_blocks_instead_of_permitting(self):
        """A history row that no exact-key lookup can see: existence is indeterminate."""
        key = self.discover()
        with self.database:
            self.database.execute("INSERT INTO application_history VALUES (?,?,?,?)",
                                  ("example ai", "ROLE-123", "APPLICATION", "legacy raw evidence"))
        self.cycle()
        self.assertEqual(self.job(key)["state"], "BLOCKED")
        self.assertEqual(self.job(key)["blocker"], "PRIOR_APPLICATION_IDENTITY_UNRESOLVED")
        self.assertEqual(self.submissions(), [])

    def test_indeterminate_history_survives_repeated_cycles_without_submitting(self):
        key = self.discover()
        with self.database:
            self.database.execute("INSERT INTO application_history VALUES (?,?,?,?)",
                                  ("example ai", "Role-123", "INTERVIEW", "legacy raw evidence"))
        self.cycle()
        self.cycle(3600)
        self.assertEqual(self.job(key)["blocker"], "PRIOR_APPLICATION_IDENTITY_UNRESOLVED")
        self.assertEqual(self.submissions(), [])
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM application_attempts").fetchone()[0], 0)

    # ---- negative: genuinely different identities stay distinct -----------

    def test_different_employer_history_never_suppresses_this_application(self):
        key = self.discover()
        self.confirmed(DISPLAYED, employer="Different AI")
        self.cycle()
        self.assertEqual(self.job(key)["state"], "SUBMITTED")
        self.assertEqual(len(self.submissions()), 1)

    def test_different_requisition_at_the_same_employer_stays_distinct(self):
        key = self.discover()
        self.confirmed("Role-999")
        self.judged_cycle()
        # Never mistaken for the same requisition, and a different role at the employer is not
        # held (owner, 2026-10-06: "we should never block employers unless the jobs are the same").
        self.assertEqual(self.job(key)["state"], "SUBMITTED")
        self.assertEqual(len(self.submissions()), 1)

    def test_legacy_row_for_a_genuinely_different_requisition_does_not_block(self):
        key = self.discover()
        with self.database:
            self.database.execute("INSERT INTO application_history VALUES (?,?,?,?)",
                                  ("example ai", "ROLE-999", "APPLICATION", "legacy raw evidence"))
        # Not blocked by the row itself; the same-posting judge (shown the row as an earlier
        # application on record) is what decides, and here it answers DIFFERENT.
        self.assertIsNone(applications.local_gate(self.database, self.job(key), fixtures.NOW))
        self.judged_cycle()
        self.assertEqual(self.job(key)["state"], "SUBMITTED")
        self.assertEqual(len(self.submissions()), 1)

    def test_company_wide_wildcard_no_longer_blocks_but_exact_row_does(self):
        # A '*' row no longer matches every role (owner, 2026-10-06); the job's own row still does.
        key = self.discover()
        with self.database:
            self.database.execute("INSERT INTO application_history VALUES (?,?,?,?)",
                                  ("example ai", "*", "INTERVIEW", "company-wide evidence"))
        self.assertIsNone(applications.local_gate(self.database, self.job(key), fixtures.NOW))
        with self.database:
            self.database.execute("INSERT INTO application_history VALUES (?,?,?,?)",
                                  ("example ai", CANONICAL, "INTERVIEW", "this role's evidence"))
        self.cycle()
        self.assertEqual(self.job(key)["state"], "DUPLICATE")
        self.assertEqual(self.job(key)["blocker"], "EXISTING_INTERVIEW")
        self.assertEqual(self.submissions(), [])

    def test_unresolved_and_wildcard_requisitions_are_still_rejected_after_folding(self):
        for value in ("*", " * ", "＊", "unresolved:abc", "UNRESOLVED:abc", "  ", 7):
            with self.subTest(requisition=value), self.assertRaises(ValueError):
                self.confirmed(value)
        with self.assertRaises(ValueError):                      # no requisition_source
            self.ingest(requisition=DISPLAYED)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM application_history").fetchone()[0], 0)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM application_external_history").fetchone()[0], 0)

    def test_null_requisition_evidence_is_unchanged(self):
        key = self.discover()
        self.ingest()
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM application_history").fetchone()[0], 0)
        self.judged_cycle()
        # Requisition-less employer evidence no longer holds the job (the 90-day window is gone,
        # owner 2026-10-06); it is still recorded as given.
        self.assertEqual(self.job(key)["state"], "SUBMITTED")
        payload = json.loads(self.database.execute("SELECT payload FROM application_external_history").fetchone()[0])
        self.assertIn("requisition", payload)
        self.assertIsNone(payload["requisition"])


class ConfirmedApplicationDuplicatePredicateTests(unittest.TestCase):
    """conversion._duplicate sees a display-cased confirmed application."""

    setUp = conversion_fixtures.ConversionTests.setUp
    job = conversion_fixtures.ConversionTests.job

    EMAIL = "candidate@example.invalid"

    def confirm_application(self, requisition):
        record = employer_record(self.EMAIL, employer="Mock Company", requisition=requisition,
                                 requisition_source="requisition quoted in the synthetic employer confirmation")
        return external_history.ingest(self.database, record, self.EMAIL)

    def test_duplicate_predicate_sees_the_confirmed_application(self):
        self.confirm_application("Req-1")
        self.assertTrue(conversion._duplicate(self.database, self.job()))

    def test_duplicate_predicate_still_distinguishes_a_different_requisition(self):
        self.confirm_application("Req-9")
        self.assertFalse(conversion._duplicate(self.database, self.job()))

class ReceiptExecutionParityTests(unittest.TestCase):
    """source_records.execution (reporting's verified submissions) must not reject a receipt
    applications.py:360 admitted."""

    setUp = fixtures.ApplicationEndToEndTests.setUp

    NOW = "2026-09-18T01:00:00+00:00"

    def submitted(self, receipt_employer, company="Example AI", requisition="role-123", receipt_requisition=None):
        role = dict(fixtures.role(company, requisition), segment={"industry": "AI"})
        job_key = applications.ingest(self.database, role, fixtures.NOW)
        receipt = dict(fixtures.evidence(fixtures.NOW), status="SUBMITTED", independent_read=True,
                       employer=receipt_employer,
                       requisition=requisition if receipt_requisition is None else receipt_requisition,
                       candidate_email=self.policy["answers"]["email"]["value"],
                       receipt_id="mock-confirmation-" + requisition)
        self.database.execute("UPDATE application_jobs SET state='SUBMITTED',receipt=? WHERE job_key=?",
                              (json.dumps(receipt), job_key))
        self.database.commit()
        row = dict(self.database.execute("SELECT * FROM application_jobs WHERE job_key=?", (job_key,)).fetchone())
        row["candidate_email"] = self.policy["answers"]["email"]["value"]
        return job_key, receipt, row

    def admitted_variants(self):
        """Employer strings a real ATS/browser emits that applications.py:360 accepts."""
        return ["Example AI", "example ai", "EXAMPLE AI", "Example AI", "Example  AI",
                " Example AI ", "Ｅxample AI"]

    def test_every_admitted_receipt_employer_is_also_an_execution(self):
        for employer in self.admitted_variants():
            with self.subTest(employer=employer):
                job_key, receipt, row = self.submitted(employer, requisition="role-" + str(abs(hash(employer)) % 997))
                self.assertTrue(applications.verified_receipt(receipt, row, self.NOW),
                                "fixture must be admissible at applications.py:360")
                action = source_records.execution(self.database, {"table": "application_jobs", "key": job_key}, self.NOW)
                self.assertIsNotNone(action)
                self.assertEqual(action["kind"], "APPLICATION_SUBMITTED")

    # ---- negative: a genuinely different receipt is still not an execution

    def test_a_different_employer_receipt_is_still_rejected_everywhere(self):
        job_key, receipt, row = self.submitted("Different AI")
        self.assertFalse(applications.verified_receipt(receipt, row, self.NOW))
        self.assertIsNone(source_records.execution(self.database, {"table": "application_jobs", "key": job_key}, self.NOW))

    def test_a_different_requisition_receipt_is_still_rejected_everywhere(self):
        job_key, receipt, row = self.submitted("Example AI", receipt_requisition="role-999")
        self.assertFalse(applications.verified_receipt(receipt, row, self.NOW))
        self.assertIsNone(source_records.execution(self.database, {"table": "application_jobs", "key": job_key}, self.NOW))

    def test_a_missing_or_unverified_receipt_is_still_not_an_execution(self):
        job_key, receipt, row = self.submitted("Example AI")
        for replacement in ({"acknowledged": True}, dict(receipt, independent_read=False),
                            dict(receipt, status="ACKNOWLEDGED"), dict(receipt, receipt_id="")):
            with self.subTest(replacement=sorted(replacement)):
                self.database.execute("UPDATE application_jobs SET receipt=? WHERE job_key=?",
                                      (json.dumps(replacement), job_key))
                self.assertIsNone(source_records.execution(self.database, {"table": "application_jobs", "key": job_key}, self.NOW))

    def test_an_unsubmitted_job_is_never_an_execution(self):
        job_key, receipt, row = self.submitted("Example AI")
        self.database.execute("UPDATE application_jobs SET state='QUEUED' WHERE job_key=?", (job_key,))
        self.assertIsNone(source_records.execution(self.database, {"table": "application_jobs", "key": job_key}, self.NOW))


if __name__ == "__main__":
    unittest.main()
