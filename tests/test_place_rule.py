"""The place and work-mode picks are a limit, and set the order roles are worked in (owner picks M2 a and
M3 a, 2026-11-16).

The verdict is the model's (`tools/place_rule_eval.py` asks a real one); here: the rule is in both prompts
with the same meaning, the eval's cases say what the owner picked, the worker's schema carries the index,
and a role research judged outside the places is not in scope.
"""
import importlib.util
import json
from pathlib import Path
import unittest

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import application_browser
import research_workflow
from test_research_workflow import NOW, role

ROOT = Path(__file__).resolve().parents[1]
RESEARCH = " ".join((ROOT / "research-prompt.md").read_text().replace("\n- ", "\n").split())
READ = " ".join(Path(application_browser.__file__).read_text().split())
spec = importlib.util.spec_from_file_location("place_rule_eval", ROOT / "tools/place_rule_eval.py")
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


class PlaceRuleTests(unittest.TestCase):
    def test_both_prompts_carry_the_rule(self):
        for text in (RESEARCH, READ):
            lowered = text.lower()
            self.assertIn("Place rule (", text)
            self.assertIn("they are a limit, not a preference; an empty or missing list means the user named none: no limit from this rule", text)
            self.assertIn("kept only when at least one way of doing it matches at least one entry and is not excluded above", text)
            self.assertIn("a role clearly outside every entry is location in_scope false", lowered)
            self.assertIn("when the list has entries and none of them names hybrid or onsite, that is every role that can only be done hybrid or onsite", lowered)
            self.assertIn("means the user is open to relocating there", text)
            self.assertIn("The user's current location never excludes anything.", text)
            self.assertIn("it is not clearly outside: under this rule it is kept (in_scope true)", text)
        self.assertIn("DATA.policy.location_priority", RESEARCH)
        self.assertIn("context.scope_targets.location_priority", READ)
        self.assertIn("location.priority_index: the 0-based index of the first entry", RESEARCH)

    def test_the_read_is_given_the_places_it_judges_against(self):
        source = Path(application_browser.__file__).read_text()
        self.assertIn('"location_priority": policy.get("location_priority", [])', source)

    def test_the_eval_cases_say_what_the_owner_picked(self):
        remote = evaluation.PROFILES["remote-europe"]
        self.assertEqual(["Anywhere in Europe remote"], remote["location_priority"])
        self.assertEqual({"hybrid_berlin": (False, None), "remote_eu_or_berlin": (True, 0),
                          "remote_us_only": (False, None), "mode_not_stated": (True, None)}, remote["expected"])
        austin = evaluation.PROFILES["austin-then-us-remote"]
        self.assertEqual(["Austin, Texas, United States hybrid/onsite", "United States remote"], austin["location_priority"])
        self.assertEqual({"onsite_austin": (True, 0), "remote_us": (True, 1),
                          "onsite_chicago": (False, None), "remote_canada_only": (False, None)}, austin["expected"])
        self.assertTrue(all(verdict == (True, None) for verdict in evaluation.PROFILES["no-places"]["expected"].values()))
        self.assertIn("Place rule", evaluation.section())
        self.assertIn("priority_index", evaluation.section())
        self.assertIn("Place rule (context.scope_targets.location_priority", evaluation.read_section())
        self.assertIn("scope_fit.location: a place is excluded", evaluation.read_section())
        self.assertNotIn("Level rule", evaluation.read_section())

    def test_the_workers_schema_carries_the_index(self):
        schema = json.loads((ROOT / "research-schema.json").read_text())
        location = schema["properties"]["opportunities"]["items"]["properties"]["policy_fit"]["properties"]["location"]
        self.assertEqual(["integer", "null"], location["properties"]["priority_index"]["type"])
        self.assertIn("priority_index", location["required"])   # strict output: every property is required

    def test_a_role_judged_outside_the_places_is_out_of_scope_and_one_inside_is_ranked(self):
        policy = dict(research_workflow.DEFAULT_POLICY, location_priority=["Anywhere in Europe remote"])
        outside = role()
        outside["policy_fit"]["location"] = {"in_scope": False, "reason": "Hybrid in Berlin; no entry allows hybrid.", "priority_index": None}
        opportunity = research_workflow.normalize_opportunity(outside, now=NOW, policy=policy)
        self.assertIs(False, opportunity["review"]["scope_fit"]["location"]["in_scope"])
        self.assertEqual("INELIGIBLE", opportunity["eligibility"])
        inside = role()
        inside["policy_fit"]["location"] = {"in_scope": True, "reason": "Remote in the EU matches entry 0.", "priority_index": 0}
        opportunity = research_workflow.normalize_opportunity(inside, now=NOW, policy=policy)
        self.assertEqual(2, opportunity["ranking"]["location_priority"])


if __name__ == "__main__":
    unittest.main()
