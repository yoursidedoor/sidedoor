"""The level pick is a limit (a beta user's feedback, 2026-11-16; owner picks L1 a+b, L2 b).

The verdict is the model's (`tools/level_rule_eval.py` asks a real one); here: the rule is in both prompts,
the eval's cases say what the owner picked, and a role the worker judged NONE is not kept.
"""
import importlib.util
from pathlib import Path
import unittest

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import application_browser
import research_workflow

ROOT = Path(__file__).resolve().parents[1]
RESEARCH = " ".join((ROOT / "research-prompt.md").read_text().split())
READ = " ".join(Path(application_browser.__file__).read_text().split())
spec = importlib.util.spec_from_file_location("level_rule_eval", ROOT / "tools/level_rule_eval.py")
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


class LevelRuleTests(unittest.TestCase):
    def test_both_prompts_carry_the_rule_and_the_old_soft_wording_is_gone(self):
        for text in (RESEARCH, READ):
            self.assertIn("clearly above the highest target level", text)
            self.assertIn("clearly below the lowest target level", text)
            self.assertIn("when the highest target level is Senior or higher, a role one level above it is still", text)
            self.assertIn("never from the title alone", text)
        self.assertNotIn("level above/below target", RESEARCH)
        self.assertNotIn("on that basis alone", READ)   # the years rule was a floor with no ceiling
        self.assertIn("in both directions", READ)

    def test_the_eval_cases_say_an_entry_level_user_sees_entry_roles_only(self):
        expected = evaluation.PROFILES["entry-level"]["expected"]
        self.assertEqual(evaluation.OUT, expected["senior"])
        self.assertEqual(evaluation.OUT, expected["manager"])
        self.assertEqual(evaluation.KEPT, expected["entry"])
        senior = evaluation.PROFILES["senior"]["expected"]
        self.assertEqual((("PARTIAL",), evaluation.OUT, evaluation.OUT),
                         (senior["staff"], senior["principal"], senior["entry"]))
        self.assertIn("Level rule", evaluation.section())

    def test_a_role_judged_outside_the_levels_is_not_in_scope(self):
        self.assertIn("NONE", research_workflow.ROLE_FITS)
        for fit, kept in (("NONE", False), ("PARTIAL", True), ("STRONG", True)):
            self.assertEqual(kept, research_workflow.role_fit_of({"role_fit": {"fit": fit, "reason": "x"}})["fit"] != "NONE")


if __name__ == "__main__":
    unittest.main()
