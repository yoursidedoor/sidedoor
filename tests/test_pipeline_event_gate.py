import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import unittest

import applications
import conversion
import test_applications_e2e as fixtures


class PipelineEventGateTests(unittest.TestCase):
    setUp = fixtures.ApplicationEndToEndTests.setUp
    discover = fixtures.ApplicationEndToEndTests.discover
    cycle = fixtures.ApplicationEndToEndTests.cycle
    job = fixtures.ApplicationEndToEndTests.job
    submissions = fixtures.ApplicationEndToEndTests.submissions

    def test_applied_event_leaves_existing_duplicate_gate_authoritative(self):
        conversion.initialize(self.database)
        key = self.discover()
        with self.database:
            self.database.execute("INSERT INTO conversion_events(event_id,kind,job_key,evidence_id,status) VALUES ('interview','INTERVIEW_STAGE',?,'source','APPLIED')", (key,))
            self.database.execute("INSERT INTO application_history VALUES ('example ai','role-123','INTERVIEW','saved employer evidence')")
        self.cycle()
        self.assertEqual(self.job(key)["state"], "DUPLICATE")
        self.assertEqual(self.submissions(), [])


if __name__ == "__main__":
    unittest.main()
