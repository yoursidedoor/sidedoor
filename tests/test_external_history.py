import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
import json
import unittest

import applications
import external_history
import user_profile
import test_applications_e2e as fixtures
import test_same_posting_before_submit_20261006 as same_posting


class ExternalHistoryTests(unittest.TestCase):
    setUp = fixtures.ApplicationEndToEndTests.setUp
    discover = fixtures.ApplicationEndToEndTests.discover
    cycle = fixtures.ApplicationEndToEndTests.cycle
    job = fixtures.ApplicationEndToEndTests.job

    def judged_cycle(self, seconds=0):
        """A cycle whose same-posting judge answers DIFFERENT: the check before the first submit is
        the dedupe and holds the job when it cannot run (owner, 2026-10-06), so a job meant to be
        submitted here needs a judge that ran."""
        same_posting.workflow_tasks.initialize(self.database)  # the judge books compute
        self.adapter.same_job_executor = same_posting.judge("DIFFERENT")
        self.adapter.now = (dt.datetime.fromisoformat(fixtures.NOW) + dt.timedelta(seconds=seconds)).isoformat()
        return applications.cycle(self.database, same_posting.CONFIG, self.root, self.adapter, self.adapter.now)

    def record(self, **changes):
        value = {"evidence_id": "gmail:fixture-123", "source_message_id": "fixture-123",
                 "employer": "Example AI", "kind": "APPLICATION_CONFIRMED", "role_title": None,
                 "source": "https://mail.google.com/mail/u/0/#all/fixture-123", "requisition": None,
                 "evidence_kind": "employer_email", "source_system": "employer ATS via Gmail",
                 "verified": True, "sender_authenticated": True, "independent_read": True,
                 "mailbox_email": self.policy["answers"]["email"]["value"], "recipient": "alias@example.invalid",
                 "delivered_to_owner": True, "sender": "fixture@employer.example", "summary": "Synthetic employer receipt",
                 "occurred_at": "2026-08-01T00:00:00+00:00", "observed_at": fixtures.NOW}
        value.update(changes)
        return value

    def ingest(self, record):
        with self.database:
            return external_history.ingest(self.database, record, self.policy["answers"]["email"]["value"])

    def resolution(self, key, relationship):
        evidence = dict(fixtures.evidence(), evidence_id="gmail:fixture-123", job_key=key,
                        comparison="Synthetic authoritative requisition comparison")
        with self.database:
            return external_history.resolve(self.database, "gmail:fixture-123", key, relationship, evidence, fixtures.NOW)

    def test_unresolved_positive_history_neither_holds_nor_becomes_a_false_duplicate(self):
        # A confirmation naming no requisition cannot show it is this job, so it no longer holds
        # (owner, 2026-10-06: "we should never block employers unless the jobs are the same").
        self.discover()
        self.ingest(self.record())
        self.assertEqual(self.database.execute("SELECT count(*) FROM application_history").fetchone()[0], 0)
        self.assertIsNone(applications.local_gate(self.database, self.job(), fixtures.NOW))
        self.judged_cycle()
        self.assertEqual(self.job()["state"], "SUBMITTED")

    def test_exact_historical_application_blocks_resubmission(self):
        self.discover()
        self.ingest(self.record(requisition="role-123", requisition_source="explicit identifier in synthetic employer receipt"))
        self.cycle()
        self.assertEqual(self.job()["state"], "DUPLICATE")
        self.assertEqual(self.adapter.calls, [])

    def test_closed_unknown_process_not_mislabeled_as_active_interview(self):
        self.discover()
        self.ingest(self.record(kind="PROCESS_CLOSED", role_title="Old infrastructure role"))
        self.cycle()
        self.assertEqual(self.database.execute("SELECT count(*) FROM application_history WHERE kind='INTERVIEW'").fetchone()[0], 0)
        self.assertNotEqual(self.job()["state"], "DUPLICATE")

    def test_other_company_proceeds_and_import_is_not_attributed_submission(self):
        key = self.discover(fixtures.role("Different AI", "other-456"))
        self.ingest(self.record())
        self.cycle()
        self.assertEqual(self.job(key)["state"], "SUBMITTED")
        self.assertEqual(self.database.execute("SELECT count(*) FROM application_attempts").fetchone()[0], 1)

    def test_reimport_preserves_original_evidence(self):
        self.assertTrue(self.ingest(self.record()))
        self.assertFalse(self.ingest(self.record(observed_at="2026-09-18T16:00:00+00:00")))
        self.assertEqual(self.database.execute("SELECT observed_at FROM application_external_history").fetchone()[0], fixtures.NOW)
        with self.assertRaisesRegex(ValueError, "Conflicting history"):
            self.ingest(self.record(summary="overwrite"))

    def test_distinct_role_resolution_only_releases_that_evidence_for_that_job(self):
        key = self.discover()
        other = self.discover(fixtures.role("Example AI", "other-456"))
        self.ingest(self.record())
        self.resolution(key, "DISTINCT_ROLE")
        # Since 2026-10-06 history at the employer never holds a job (owner: "we should never block
        # employers unless the jobs are the same"); both apply.
        self.judged_cycle()
        self.assertEqual((self.job(key)["state"], self.job(other)["state"]), ("SUBMITTED", "SUBMITTED"))

    def test_same_role_mapping_prevents_duplicate_and_cannot_be_reversed(self):
        key = self.discover()
        self.ingest(self.record())
        self.resolution(key, "SAME_ROLE")
        self.assertFalse(self.resolution(key, "SAME_ROLE"))
        with self.assertRaisesRegex(ValueError, "Conflicting resolution"):
            self.resolution(key, "DISTINCT_ROLE")
        self.cycle()
        self.assertEqual(self.job(key)["state"], "DUPLICATE")

    def test_unverified_wrong_owner_absence_and_aiapply_rejected(self):
        for changes in [{"verified": False}, {"sender_authenticated": False}, {"mailbox_email": "other@example.invalid"},
                        {"kind": "NOT_SUBMITTED"}, {"sender": "notice@aiapply.example"}, {"delivered_to_owner": False},
                        {"requisition": "*"}, {"requisition": "guessed-id"}, {"source_message_id": "wrong"}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.ingest(self.record(**changes))
        self.assertEqual(self.database.execute("SELECT count(*) FROM application_external_history").fetchone()[0], 0)

    def test_import_does_not_change_saved_candidate_answers_or_resume(self):
        before = user_profile.profile_path().read_bytes()
        self.ingest(self.record())
        self.assertEqual(user_profile.profile_path().read_bytes(), before)
        self.assertEqual(self.database.execute("SELECT count(*) FROM application_answers").fetchone()[0], 0)

    def local_receipt(self, requisition="a-different-requisition", kind="APPLICATION"):
        with self.database:
            self.database.execute("INSERT INTO application_history VALUES (?,?,?,?)",
                                  (self.job()["employer"], requisition, kind,
                                   json.dumps({"observed_at": "2026-09-22T09:59:38+00:00"})))
        return external_history.local_receipt_id(requisition, kind)

    def test_a_same_role_verdict_records_the_duplicate_instead(self):
        self.discover()
        evidence_id = self.local_receipt()
        evidence = dict(fixtures.evidence(), evidence_id=evidence_id, job_key=self.job()["job_key"],
                        comparison="The saved receipt is this same posting")
        with self.database:
            external_history.resolve(self.database, evidence_id, self.job()["job_key"], "SAME_ROLE", evidence, fixtures.NOW)
        gate = applications.local_gate(self.database, dict(self.job()), fixtures.NOW,
                                       {"referrals_block_applications": False})
        self.assertEqual(gate[0], "DUPLICATE")
        self.assertEqual(gate[1], "EXISTING_APPLICATION")


if __name__ == "__main__":
    unittest.main()
