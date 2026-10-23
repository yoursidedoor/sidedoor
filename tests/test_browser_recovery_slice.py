import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import copy
from pathlib import Path
import tempfile
import unittest

import application_destination
import applications
import desktop_adapter
import identity_reconciliation
import manual_browser_recovery_slice as manual


class BrowserRecoverySliceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.fixture = manual.RecoveryFixture(Path(temporary.name) / "fixture")
        self.addCleanup(self.fixture.database.close)

    def test_destination_reconciliation_is_rejected_after_submission_intent(self):
        evidence = dict(manual.identity(self.fixture), source="application-browser:synthetic", observed_at=applications.stamp(),
                        browser_evidence=[{"pointer": "/resolution/identity/evidence/official_url", "quote": "synthetic"}])
        with self.fixture.database:
            self.fixture.database.execute("INSERT INTO application_attempts(attempt_id,job_key,idempotency_key,state,started_at) VALUES ('synthetic',?,?,'SUBMITTING',?)",
                (self.fixture.key, self.fixture.key, applications.stamp()))
        before = list(self.fixture.database.iterdump())
        with self.assertRaises(ValueError):
            identity_reconciliation.reconcile_destination(self.fixture.database, self.fixture.key, evidence["official_url"], evidence, applications.stamp())
        self.assertEqual(before, list(self.fixture.database.iterdump()))

    def test_empty_resolution_or_submission_status_is_rejected(self):
        request = manual.envelope(self.fixture)
        for status in ("RESOLVED", "SUBMITTED", "NOT_SUBMITTED"):
            result = {"status": status, "reason": "Synthetic", "next_action": "Inspect", "resolution": {"history": []}}
            with self.subTest(status=status), self.assertRaises(ValueError):
                manual.normalized(self.fixture, request, result)

    def test_exact_recovery_context_changes_binding_but_replay_does_not(self):
        packet = {"job": self.fixture.job(), "recovery": {"attempt": 1, "max_attempts": 3,
            "previous_failure": "Unknown official requisition", "instruction": "Read official role"}}
        body = self.fixture.adapter.request_body("investigate", packet)
        repeated = self.fixture.adapter.request_body("investigate", copy.deepcopy(packet))
        changed = copy.deepcopy(packet)
        changed["recovery"].update(attempt=2, instruction="Read observed company link")
        self.assertEqual(desktop_adapter.digest(body), desktop_adapter.digest(repeated))
        self.assertNotEqual(desktop_adapter.digest(body), desktop_adapter.digest(self.fixture.adapter.request_body("investigate", changed)))

    def test_query_requisition_is_preserved_and_private_or_credential_urls_rejected(self):
        self.assertEqual(application_destination.public_url("https://careers.example/application?requisition=123"),
                         "https://careers.example/application?requisition=123")
        for url in ("https://169.254.169.254/", "https://2130706433/", "https://0177.0.0.1/",
                    "https://example.com/?password=secret", "https://example.com/%61iapply/", "https://user@example.com/"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                application_destination.public_url(url)


if __name__ == "__main__":
    unittest.main()
