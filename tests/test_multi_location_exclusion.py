"""The owner's onsite location exclusions and funding stages are AI verdicts (owner, 2026-10-07, items 4/5)."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import unittest

import applications


def facts(city, mode="onsite", state=None, options=None, **extra):
    value = {"country": "US", "state": state, "city": city, "work_mode": mode, **extra}
    if options is not None:
        value["location_options"] = [{"city": c, "state": s, "country": k} for c, s, k in options]
    return value


class MultiLocationExclusionTests(unittest.TestCase):
    def refusal(self, value, **scope_fit):
        return applications.scope_refusal({"facts": value, "scope_fit": scope_fit}, {})

    def test_only_the_ai_verdict_excludes_a_location_or_funding_stage(self):
        # Python no longer matches city, state or stage text.
        self.assertIsNone(self.refusal(facts("Seattle, WA", "hybrid")))
        self.assertIsNone(self.refusal(facts("San Francisco", options=[("San Francisco", "CA", "US")])))
        self.assertIsNone(self.refusal(facts("Austin", funding_stage="Series G")))
        for dimension, value in (("location", facts("Seattle, WA", "hybrid")),
                                 ("funding_stage", facts("Austin", funding_stage="Series G"))):
            with self.subTest(dimension=dimension):
                outcome = self.refusal(value, **{dimension: {"in_scope": False, "reason": "outside the owner's policy"}})
                self.assertEqual(outcome[:2], ("INELIGIBLE", "OUTSIDE_CONFIRMED_SCOPE"))
                self.assertIn(dimension, outcome[2])


if __name__ == "__main__":
    unittest.main()
