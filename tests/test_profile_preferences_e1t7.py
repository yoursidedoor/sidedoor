"""E1.T7: the owner's preferences are profile fields with neutral defaults.

A profile without `onsite_location_exclusions`, `countries`,
`target_specializations` or any US authorization answer restricts, ranks and assumes nothing;
the example profile (which carries the owner's former hard-coded values in the same shape)
reproduces the decisions the code made before.
"""

import json
import unittest

import profile_home  # noqa: F401 (example SIDEDOOR_HOME)
import applications
import research_workflow as research
import user_profile
from test_research_workflow import NOW, change, role


EXAMPLE = json.loads(user_profile.EXAMPLE_PROFILE.read_text())
BARE = {key: value for key, value in EXAMPLE.items()
        if key not in ("onsite_location_exclusions",) + user_profile.OPTIONAL_PROFILE_KEYS}
BARE["answers"] = {key: value for key, value in EXAMPLE["answers"].items()
                   if key not in ("us_work_authorized", "us_permanent_resident", "work_authorization_by_jurisdiction",
                                  "requires_visa_sponsorship_us")}


def silent_us_read():
    return {"verified": True, "browser_evidence": [], "observed_browser_calls": 3,
            "facts": {"country": "US", "work_mode": "remote", "stated_eligibility_requirements": ["NONE_STATED"]}}


def ranked(policy, city, mode, home=None, country=None):
    item = role()
    if country:
        change(item, "country", country)
    change(item, "city", city)
    change(item, "work_mode", mode)
    item["in_user_home_area"] = home
    return research.normalize_opportunity(item, now=NOW, policy=policy)["ranking"]["location_priority"]


class ProfilePreferencesTest(unittest.TestCase):
    def test_profile_without_the_fields_restricts_ranks_and_assumes_nothing(self):
        tokyo = {"facts": {"country": "JP", "work_mode": "onsite"}}
        self.assertIsNone(applications.scope_refusal(tokyo, BARE))
        self.assertIsNone(applications.location_exclusion({"country": "US", "city": "Seattle", "state": "WA",
                                                           "work_mode": "onsite"}, BARE))
        self.assertEqual(ranked(BARE, "Seattle", "onsite"), 3)
        self.assertEqual(ranked(BARE, "Arlington", "hybrid"), 3)
        self.assertEqual(applications.target_specializations(BARE), [])
        self.assertIsNone(applications.us_authorization_label(BARE))
        read = applications.derive_silent_eligibility_facts(silent_us_read(), BARE)
        self.assertIsNone(read["facts"].get("authorization"))
        # A user who records no US work authorization still applies; sponsorship is the
        # employer's decision (owner, 2026-10-14).
        unauthorized = dict(BARE, answers=dict(BARE["answers"], us_work_authorized=dict(
            EXAMPLE["answers"]["us_work_authorized"], value=False)))
        self.assertNotEqual((applications.assess(silent_us_read(), {}, unauthorized, NOW) or ("",))[0], "BLOCKED")

    def test_example_profile_reproduces_the_former_hard_coded_decisions(self):
        tokyo = {"facts": {"country": "JP", "work_mode": "remote"}}
        self.assertEqual(applications.scope_refusal(tokyo, EXAMPLE)[1], "OUTSIDE_CONFIRMED_SCOPE")
        self.assertIsNone(applications.scope_refusal({"facts": {"country": "DE", "work_mode": "onsite"}}, EXAMPLE))
        self.assertEqual(applications.location_exclusion({"country": "US", "city": "Bellevue", "work_mode": "hybrid"},
                                                         EXAMPLE), ("EXCLUDED", "excluded_city_in_excluded_state:Bellevue"))
        self.assertIsNone(applications.location_exclusion({"country": "US", "city": "Washington", "state": "DC",
                                                           "work_mode": "onsite"}, EXAMPLE))
        self.assertEqual(ranked(EXAMPLE, "Seattle", "onsite"), 99)
        # Owner, 2026-10-14: research's in_user_home_area ranks first; an excluded place stays 99.
        self.assertEqual(ranked(EXAMPLE, "Seattle", "onsite", home=True), 99)
        self.assertEqual(ranked(EXAMPLE, "Springfield", "hybrid", home=True), 1)
        self.assertEqual(ranked(EXAMPLE, "Springfield", "hybrid", home=False), 3)
        self.assertEqual(ranked(EXAMPLE, "Springfield", "hybrid", home=None), 3)
        # True ranks 1 with no country condition (E1.T7 decision); other tiers unchanged.
        self.assertEqual(ranked(EXAMPLE, "Berlin", "hybrid", home=True, country="DE"), 1)
        self.assertEqual(ranked(EXAMPLE, "Berlin", "hybrid", home=None, country="DE"), 99)
        self.assertEqual(applications.target_specializations(EXAMPLE),
                         ["backend", "distributed systems", "infrastructure", "platform", "data engineering"])
        read = applications.derive_silent_eligibility_facts(silent_us_read(), EXAMPLE)
        self.assertEqual(read["facts"]["authorization"], "US_PERMANENT_RESIDENT_ACCEPTED")
        self.assertIsNone(applications.assess(read, {}, EXAMPLE, NOW))


if __name__ == "__main__":
    unittest.main()
