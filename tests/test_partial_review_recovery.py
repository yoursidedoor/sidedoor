import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import unittest

import manual_partial_review_recovery as rehearsal


class PartialReviewRecoveryTests(unittest.TestCase):
    def test_manually_validated_scenarios(self):
        for name in rehearsal.SCENARIOS:
            with self.subTest(scenario=name):
                result = rehearsal.run([name])
                self.assertEqual(result["passed"], [name])
                self.assertEqual(result["external_effects"], 0)


if __name__ == "__main__":
    unittest.main()
