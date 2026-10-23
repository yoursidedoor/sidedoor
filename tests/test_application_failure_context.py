import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import unittest

import manual_application_failure_context as rehearsal


class ApplicationFailureContextTests(unittest.TestCase):
    def test_manually_validated_failure_context(self):
        for scenario in rehearsal.SCENARIOS:
            with self.subTest(scenario=scenario.__name__):
                result = rehearsal.run([scenario])
                self.assertEqual(result["passed"], [scenario.__name__])
                self.assertEqual(result["live_effects"], 0)


if __name__ == "__main__":
    unittest.main()
