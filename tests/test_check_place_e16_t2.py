"""E16.T2 phase 1, piece 2 (owner, 2026-11-12: "this should be gotten on read session"): a session
reports WHERE a human check sits as a structured value, and the wait records it, so a check on the form
itself can be told from one that gates the site. Nothing reads the position yet."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import access_outcomes  # noqa: E402
import application_browser  # noqa: E402
import applications  # noqa: E402
import desktop_adapter  # noqa: E402
import test_access_outcomes_e15_t1 as walls  # noqa: E402

STOP = {"code": "CAPTCHA_REQUIRED", "next_action": "A person has to do the human check"}


class TheReport(unittest.TestCase):
    def test_the_read_is_asked_for_a_structured_place_and_position(self):
        contract = application_browser.result_contract("inspect")
        self.assertEqual(contract["human_check"], {"place": "NONE|BEFORE_FORM|ON_FORM", "after_question": None,
                                                   "kind": "HCAPTCHA|RECAPTCHA|TURNSTILE|OTHER", "form_readable": None})
        for words in ("BEFORE_FORM", "ON_FORM", "after_question", "inside blocked"):
            self.assertIn(words, application_browser.PROMPT)
        # The rule it sits beside is unchanged: the AI never touches a check.
        self.assertIn("never click, tick, solve", application_browser.PROMPT)

    def test_only_the_contracts_values_are_kept(self):
        def clean(value):
            found = access_outcomes.clean_check(value)
            return found and {key: found[key] for key in ("place", "after_question")}
        self.assertEqual(clean({"place": "ON_FORM", "after_question": 3}), {"place": "ON_FORM", "after_question": 3})
        self.assertEqual(clean({"place": "ON_FORM", "after_question": -1}), {"place": "ON_FORM", "after_question": -1})
        self.assertEqual(clean({"place": "BEFORE_FORM", "after_question": 3}), {"place": "BEFORE_FORM", "after_question": None})
        for position in (True, "3", 3.0, -2, 1000, None, [3]):
            self.assertEqual(clean({"place": "ON_FORM", "after_question": position}),
                             {"place": "ON_FORM", "after_question": None}, position)
        for value in (None, "ON_FORM", [], {}, {"place": "NONE"}, {"place": "on the form"}, {"place": None},
                      {"place": ["ON_FORM"]}, {"after_question": 2}):
            self.assertIsNone(clean(value), value)


class TheWait(unittest.TestCase):
    for _name in ("setUp", "connect", "write_config", "job", "cycle", "advance", "run_until_submit", "row",
                  "requests", "waits", "events", "assert_waiting", "second_job"):
        locals()[_name] = getattr(walls.AccessOutcomeTests, _name)
    del _name
    read_check = None

    def review(self):
        review = walls.AccessOutcomeTests.review(self)
        return dict(review, human_check=self.read_check) if self.read_check is not None else review

    def stop_read(self, blocked):
        self.cycle()
        request = desktop_adapter.claim(self.database, self.root, "access-fixture", self.now)
        self.assertEqual(request["payload"]["operation"], "inspect")
        self.assertTrue(desktop_adapter.complete(self.database, dict(request, result={"blocked": blocked}), self.now)["accepted"])
        self.advance(60)
        self.cycle()
        return self.assert_waiting("HUMAN_CHECK", "CAPTCHA_REQUIRED")

    def stop_submit(self, blocked):
        request = self.run_until_submit()
        self.assertTrue(desktop_adapter.complete(self.database, dict(
            request, result={"blocked": blocked, "acknowledged": False, "prearm_blocked": True}), self.now)["accepted"])
        self.advance(60)
        self.cycle()
        return self.assert_waiting("HUMAN_CHECK", "CAPTCHA_REQUIRED")

    def check(self, wait):
        found = access_outcomes.check(wait)
        return found and {key: found[key] for key in ("place", "after_question")}

    def test_the_kind_of_check_is_one_of_the_known_kinds(self):
        for given, kept in (("HCAPTCHA", "HCAPTCHA"), ("RECAPTCHA", "RECAPTCHA"), ("TURNSTILE", "TURNSTILE"),
                            ("OTHER", "OTHER"), ("hcaptcha", "OTHER"), (None, "OTHER"), (7, "OTHER")):
            self.assertEqual(access_outcomes.clean_check({"place": "ON_FORM", "kind": given})["kind"], kept, given)
        wait = self.stop_submit(dict(STOP, human_check={"place": "ON_FORM", "after_question": 4, "kind": "HCAPTCHA"}))
        self.assertEqual(access_outcomes.check(wait)["kind"], "HCAPTCHA")

    def card(self):
        return [card for card in access_outcomes.waiting(self.database) if card["job_key"] == self.key][0]

    def test_a_check_that_gates_the_page_is_recorded_as_before_the_form(self):
        wait = self.stop_read(dict(STOP, human_check={"place": "BEFORE_FORM", "after_question": None}))
        self.assertEqual(self.check(wait), {"place": "BEFORE_FORM", "after_question": None})
        self.assertEqual(self.card()["words"], access_outcomes.WAIT_REASONS["HUMAN_CHECK"])   # the card is unchanged

    def test_a_check_on_the_form_is_recorded_with_its_position_as_the_submit_session_reports_it(self):
        wait = self.stop_submit(dict(STOP, human_check={"place": "ON_FORM", "after_question": 4}))
        self.assertEqual(self.check(wait), {"place": "ON_FORM", "after_question": 4})

    def test_form_readable_is_true_only_when_the_session_says_true(self):
        for given, kept in ((True, True), (None, False), ("yes", False), (1, False), (False, False)):
            self.assertIs(access_outcomes.clean_check({"place": "ON_FORM", "form_readable": given})["form_readable"], kept)
        self.assertIsNotNone(access_outcomes.form_check({"place": "ON_FORM", "form_readable": True}))
        for value in ({"place": "ON_FORM"}, {"place": "ON_FORM", "form_readable": False},
                      {"place": "BEFORE_FORM", "form_readable": True}, {"place": "NONE"}, None):
            self.assertIsNone(access_outcomes.form_check(value), value)

    def test_no_report_and_a_report_outside_the_contract_record_nothing(self):
        self.read_check = {"place": "NONE", "after_question": None}
        wait = self.stop_submit(dict(STOP, human_check={"place": "somewhere near the bottom"}))
        self.assertIsNone(wait["human_check"])
        self.assertIsNone(access_outcomes.check(wait))

    def test_a_sign_in_wall_records_no_check(self):
        self.cycle()
        request = desktop_adapter.claim(self.database, self.root, "access-fixture", self.now)
        result = {"blocked": {"code": "AUTHENTICATION_REQUIRED", "next_action": "Sign in",
                              "human_check": {"place": "ON_FORM", "after_question": 1}}}
        self.assertTrue(desktop_adapter.complete(self.database, dict(request, result=result), self.now)["accepted"])
        self.advance(60)
        self.cycle()
        self.assertEqual([wait["human_check"] for wait in self.waits()], [None])


if __name__ == "__main__":
    unittest.main()
