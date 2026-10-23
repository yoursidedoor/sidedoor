import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import unittest

import manual_application_recovery_integrated


class ApplicationRecoveryIntegratedTests(unittest.TestCase):
    def test_late_conversion_events_cannot_reopen_abandoned_work(self):
        self.assertEqual(manual_application_recovery_integrated.validate_abandoned_handoffs()["state"], "PASS")

    def test_host_coordinator_browser_lifecycle_and_reports(self):
        self.assertEqual(manual_application_recovery_integrated.validate()["state"], "PASS")


if __name__ == "__main__":
    unittest.main()
