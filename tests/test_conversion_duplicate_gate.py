"""Duplicate-protection and identity-normalization gates in conversion.py.

Fixture policy for this file (deliberate, see FIX-REPORT §3): every value that
reaches conversion.py from the *untrusted* side -- record_evidence facts, a
legacy application_history row, an operator-supplied contact_id -- is written in
the realistic casing a person or the trusted importer would actually produce
("Mock Company", "REQ-1", "Contact-1"). Only the stored application_jobs columns
are normalized, and they are normalized by applications.ingest, not by the test.
Copying job["employer"] into facts (what tests/test_conversion.py:44 does) would
make every assertion below vacuous.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import sqlite3
import unittest

import applications
import conversion
import external_history


NOW = "2026-09-17T16:00:00+00:00"

# Raw, realistically-cased values as an operator/AI reads them off a careers page.
RAW_COMPANY = "Mock Company"
RAW_REQUISITION = "REQ-1"
BLOCKER = "PRIOR_APPLICATION_IDENTITY_UNRESOLVED"


class DuplicateGateTests(unittest.TestCase):
    def setUp(self):
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        applications.initialize(self.database)
        conversion.initialize(self.database)
        external_history.initialize(self.database)
        self.key = applications.ingest(self.database, {
            "company": RAW_COMPANY, "requisition_id": RAW_REQUISITION, "role": "Staff Engineer",
            "url": "https://jobs.ashbyhq.com/mock/" + RAW_REQUISITION}, NOW)
        self.addCleanup(self.database.close)

    # ---- helpers ----------------------------------------------------------

    def job(self):
        return self.database.execute("SELECT * FROM application_jobs WHERE job_key=?", (self.key,)).fetchone()

    def legacy_history(self, requisition=RAW_REQUISITION, kind="APPLICATION"):
        """An application_history row written by a pre-fix writer: raw requisition."""
        self.database.execute("INSERT INTO application_history VALUES (?,?,?,?)",
                              (self.job()["employer"], requisition, kind,
                               '{"source":"legacy raw employer confirmation email"}'))

    # ---- 1. indeterminate prior-application history -----------------------

    def test_indeterminate_history_is_detected_but_the_exact_key_lookup_is_blind(self):
        self.legacy_history()
        job = dict(self.job())
        self.assertEqual([], conversion._identity_records(self.database, "application_history", job))
        self.assertFalse(conversion._duplicate(self.database, job))
        self.assertEqual(("REQ-1", "APPLICATION"), conversion._indeterminate_history(self.database, job))
        with self.assertRaises(ValueError) as caught:
            conversion._require_determinate_history(self.database, job)
        self.assertIn(BLOCKER, str(caught.exception))

    # ---- 2. the unmatched row is never folded into identity records ------

    def test_champion_summary_provenance_excludes_the_unmatched_row(self):
        self.legacy_history()
        history = conversion._identity_records(self.database, "application_history", dict(self.job()))
        self.assertEqual([], [dict(row) for row in history])

    # ---- 3. raw-vs-normalized comparisons ---------------------------------

    def test_bound_accepts_realistically_cased_evidence_for_the_same_job(self):
        job = dict(self.job())
        self.assertEqual("mock company", job["employer"])
        self.assertEqual("req-1", job["requisition"])
        for employer, requisition in [("Mock Company", "REQ-1"), ("MOCK COMPANY", "Req-1"),
                                      ("Mock  Company", "req-1"), (" Mock Company ", "req-1"),
                                      ("Mock Company", "req-1"), ("Ｍock Company", "req-1"),
                                      ("mock company", "REQ-1")]:
            with self.subTest(employer=employer, requisition=requisition):
                facts = conversion._bound({"facts": {"employer": employer, "requisition": requisition}}, job)
                self.assertEqual(employer, facts["employer"], "facts must be returned verbatim, not rewritten")

    def test_bound_accepts_a_case_variant_of_the_same_recipient(self):
        job = dict(self.job())
        record = {"facts": {"employer": RAW_COMPANY, "requisition": RAW_REQUISITION, "contact_id": "Contact-One"}}
        self.assertTrue(conversion._bound(record, job, "contact-one"))

    def test_a_genuinely_different_employer_is_still_rejected(self):
        job = dict(self.job())
        for employer in ["Other Company", "Mock Companies", "Mock Compan", "mockcompany"]:
            with self.subTest(employer=employer):
                with self.assertRaises(ValueError) as caught:
                    conversion._bound({"facts": {"employer": employer, "requisition": RAW_REQUISITION}}, job)
                self.assertIn("exact employer and requisition", str(caught.exception))

    def test_a_genuinely_different_requisition_is_still_rejected(self):
        job = dict(self.job())
        for requisition in ["REQ-999", "REQ-11", "req_1", "req1", "*"]:
            with self.subTest(requisition=requisition):
                with self.assertRaises(ValueError):
                    conversion._bound({"facts": {"employer": RAW_COMPANY, "requisition": requisition}}, job)

    def test_a_genuinely_different_recipient_is_still_rejected(self):
        job = dict(self.job())
        # Stored-column employer/requisition so only the recipient is under test.
        record = {"facts": {"employer": job["employer"], "requisition": job["requisition"],
                            "contact_id": "Contact-Two"}}
        with self.assertRaises(ValueError) as caught:
            conversion._bound(record, job, "contact-one")
        self.assertEqual("Evidence contact mismatch", str(caught.exception))

    def test_missing_or_non_string_identity_never_matches(self):
        job = dict(self.job())
        for employer in [None, 1, ["Mock Company"], {"value": "Mock Company"}]:
            with self.subTest(employer=employer):
                with self.assertRaises(ValueError):
                    conversion._bound({"facts": {"employer": employer, "requisition": RAW_REQUISITION}}, job)

    def test_a_different_requisition_in_history_does_not_suppress_a_new_application(self):
        """No false duplicate: an unrelated stored requisition must not block."""
        self.legacy_history(requisition="REQ-999")
        job = dict(self.job())
        self.assertIsNone(conversion._indeterminate_history(self.database, job))
        self.assertFalse(conversion._duplicate(self.database, job))
        self.assertIsNone(applications.local_gate(self.database, job, NOW, {}))

    def test_company_wide_wildcard_history_is_not_treated_as_indeterminate(self):
        self.legacy_history(requisition="*")
        self.assertIsNone(conversion._indeterminate_history(self.database, dict(self.job())))
        self.assertTrue(conversion._duplicate(self.database, dict(self.job())))

    def test_exact_history_stays_an_ordinary_duplicate_not_an_indeterminate_block(self):
        self.legacy_history(requisition=self.job()["requisition"])
        job = dict(self.job())
        self.assertTrue(conversion._duplicate(self.database, job))
        self.assertIsNone(conversion._indeterminate_history(self.database, job))
        conversion._require_determinate_history(self.database, job)

    # ---- submission path --------------------------------------------------

    def test_submission_path_gate_still_blocks_the_same_row(self):
        """applications.local_gate (agent M's external_history.gate) stays authoritative."""
        self.legacy_history()
        state, blocker, action, unused = applications.local_gate(self.database, dict(self.job()), NOW, {})
        self.assertEqual("BLOCKED", state)
        self.assertEqual(BLOCKER, blocker)
        self.assertIn("Do not submit while prior-application existence is indeterminate", action)


if __name__ == "__main__":
    unittest.main()
