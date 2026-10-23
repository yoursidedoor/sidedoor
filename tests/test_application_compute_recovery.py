import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import unittest

import manual_application_compute_recovery


class ApplicationComputeRecoveryTests(unittest.TestCase):
    def test_old_investigation_handoff_preserves_failures_and_active_leases(self):
        self.assertEqual(manual_application_compute_recovery.validate_handoff()["state"], "PASS")

    def test_scoped_recovery_and_concurrent_restart_limits(self):
        result = manual_application_compute_recovery.validate()
        self.assertEqual(result["state"], "PASS")
        self.assertEqual(len(result["checks"]), 5)


if __name__ == "__main__":
    unittest.main()
