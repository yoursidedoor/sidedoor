"""E16.T2 (owner, 2026-11-13, after the first real-engine run on the kit's form-check site): a read that
finds a human check ON THE FORM, with the form readable, "is NOT a blocked job: it is a completed read with
the fact stored for the submit". The shape below is that run's own output (request-w54_dyc9): the session
stopped on CAPTCHA_REQUIRED with the place and a partial review listing the form's six questions."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import application_browser  # noqa: E402
from manual_application_browser import Fixture, event, request_at  # noqa: E402

CHECK = {"place": "ON_FORM", "kind": "OTHER", "after_question": 5, "form_readable": True}
QUESTIONS = [{"id": "q%d" % index, "label": label, "semantic": semantic, "required": True, "answer_type": "text",
              "control": "text", "choices": [], "max_characters": None, "format": None}
             for index, (label, semantic) in enumerate((("Full name", "full_name"), ("Email", "email"), ("Phone", "phone"),
                                                        ("LinkedIn", "linkedin"), ("Location", "location"),
                                                        ("Why this role?", "motivation")))]
REAL = {"blocked": {"code": "CAPTCHA_REQUIRED", "human_check": CHECK,
                    "next_action": "Complete the 'I am human' check on the application form, then request a new inspection."},
        "partial_review": {"action_scope": "application_only", "all_required_questions_enumerated": False,
                           "application_method": "FORM", "candidate_email": None, "employer": "Kestrel Aerospace",
                           "employer_matches_expected": True, "form_revision": None, "human_check": CHECK,
                           "official_destination_verified": True, "questions": QUESTIONS,
                           "role": "Staff Software Engineer", "role_matches_expected": True, "verified": False,
                           "visible_required_questions_enumerated": False}}
EVIDENCE = [{"pointer": "/blocked/human_check/place", "tool_call_id": "s1", "quote": "I am human"},
            {"pointer": "/blocked/next_action", "tool_call_id": "s1", "quote": "I am human"},
            {"pointer": "/partial_review/employer", "tool_call_id": "s1", "quote": "Kestrel Aerospace"},
            {"pointer": "/partial_review/questions/0/label", "tool_call_id": "s1", "quote": "Full name"}]


class TheShape(unittest.TestCase):
    def read(self, result, evidence=EVIDENCE):
        return application_browser.form_check_read(copy.deepcopy(result), {"evidence": copy.deepcopy(evidence)})

    def test_the_real_result_becomes_the_read_with_every_value_as_the_session_reported_it(self):
        result, raw = self.read(REAL)
        self.assertNotIn("blocked", result)
        self.assertNotIn("partial_review", result)
        self.assertEqual(result["human_check"], CHECK)
        self.assertEqual([question["label"] for question in result["questions"]], [q["label"] for q in QUESTIONS])
        # Nothing is raised or filled in: the session's own flags stand.
        for key in ("verified", "all_required_questions_enumerated", "visible_required_questions_enumerated"):
            self.assertIs(result[key], False, key)
        self.assertEqual((result["employer"], result["role"]), ("Kestrel Aerospace", "Staff Software Engineer"))
        self.assertEqual([entry["pointer"] for entry in raw["evidence"]],
                         ["/human_check/place", "/employer", "/questions/0/label"])

    def test_what_is_not_a_readable_form_check_stays_the_stop_it_is(self):
        def changed(**blocked):
            value = copy.deepcopy(REAL)
            value["blocked"].update(blocked)
            value["partial_review"]["human_check"] = value["blocked"].get("human_check")
            return value
        hidden = copy.deepcopy(REAL)
        hidden["partial_review"]["questions"] = []
        for value in (changed(human_check=dict(CHECK, place="BEFORE_FORM")),              # a check that gates the site
                      changed(human_check=dict(CHECK, form_readable=False)),              # questions hidden until it is done
                      changed(human_check={key: value for key, value in CHECK.items() if key != "form_readable"}),   # not said: not "readable"
                      changed(human_check={"place": "somewhere on the form"}),            # not the contract's values
                      changed(human_check=None), changed(code="AUTHENTICATION_REQUIRED"),
                      hidden, {"blocked": REAL["blocked"]}):
            result, raw = self.read(value)
            self.assertEqual(result, value)
            self.assertEqual(raw["evidence"], EVIDENCE)

    # The dry run of 2026-11-13 (request-y48t7q34): no stop at all, the whole read under partial_review. It
    # was taken for a read with no questions: an empty application was approved and submitted with no answers.
    THIRD = {"verified": False,
             "next_action": "The browser view shows every field but does not identify which fields are required.",
             "partial_review": dict(REAL["partial_review"], human_check=dict(CHECK, form_readable=True),
                                    questions=[dict(question, required=None) for question in QUESTIONS])}

    def test_a_read_given_whole_under_partial_review_with_no_stop_becomes_the_read_too(self):
        result, raw = self.read(self.THIRD, [{"pointer": "/partial_review/questions/0/label", "tool_call_id": "s1", "quote": "Full name"}])
        self.assertNotIn("partial_review", result)
        self.assertNotIn("blocked", result)
        self.assertEqual(len(result["questions"]), 6)
        self.assertEqual(result["human_check"]["place"], "ON_FORM")
        self.assertIs(result["verified"], False)                               # as the session said
        self.assertIsNone(result["questions"][0]["required"])
        self.assertEqual([entry["pointer"] for entry in raw["evidence"]], ["/questions/0/label"])

    def test_the_read_is_told_a_form_check_is_not_in_its_way_and_how_to_say_the_form_is_hidden(self):
        for words in ("is not in the way of an inspect", "form_readable is true", "not blocked",
                      "A check with form_readable false, or one in front of the page, is in the way",
                      "never click, tick, solve"):
            self.assertIn(words, application_browser.PROMPT, words)
        self.assertIn("form_readable", application_browser.result_contract("inspect")["human_check"])


class ThroughTheSession(unittest.TestCase):
    """Real `consume` and `normalize`: the stored result of that session is a read, not a stop."""

    def stored(self, raw_result):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        fixture = Fixture(folder / "fixture")
        self.addCleanup(fixture.database.close)
        request_id = fixture.queue("inspect")
        seen = {}

        def scripted(config, prompt, directory, timeout):
            request = request_at(directory)
            seen["directory"] = directory
            (directory / "output.json").write_text(json.dumps({
                "request_id": request["request_id"], "binding": request["binding"],
                "result_json": json.dumps(raw_result), "evidence": []}))
            (directory / "trace.jsonl").write_text(json.dumps(event("s1", '- checkbox "I am human" [ref=e9]\n')) + "\n")

        summary = application_browser.consume(fixture.root, fixture.database, fixture.config, request_id, scripted)
        self.assertEqual(summary["state"], "COMPLETE", summary)
        row = fixture.database.execute("SELECT result FROM desktop_requests WHERE request_id=?", (request_id,)).fetchone()
        return json.loads(row["result"])

    def test_the_real_shape_is_stored_as_a_completed_read_with_the_check_on_the_form(self):
        result = self.stored(REAL)
        self.assertNotIn("blocked", result)
        self.assertEqual({key: result["human_check"][key] for key in CHECK}, CHECK)
        self.assertEqual(len(result["questions"]), 6)

    def test_the_third_shape_is_stored_as_a_read_with_its_questions(self):
        result = self.stored(TheShape.THIRD)
        self.assertNotIn("blocked", result)
        self.assertEqual(len(result["questions"]), 6)
        self.assertEqual(result["human_check"]["place"], "ON_FORM")

    def test_a_read_that_is_only_a_partial_review_with_no_form_check_is_an_incomplete_read(self):
        """Never a complete read with no questions (what let an empty application through)."""
        partial = copy.deepcopy(TheShape.THIRD)
        partial["partial_review"].pop("human_check")
        result = self.stored(partial)
        self.assertEqual(result["blocked"]["code"], "INSPECTION_INCOMPLETE")
        self.assertEqual(result["blocked"]["next_action"], partial["next_action"])
        self.assertEqual(len(result["partial_review"]["questions"]), 6)

    def test_a_check_in_front_of_the_page_is_stored_as_the_stop_it_is(self):
        gate = copy.deepcopy(REAL)
        gate["blocked"]["human_check"] = {"place": "BEFORE_FORM", "kind": "TURNSTILE", "after_question": None}
        gate["partial_review"]["human_check"] = gate["blocked"]["human_check"]
        gate["partial_review"]["questions"] = []
        self.assertEqual(self.stored(gate)["blocked"]["code"], "CAPTCHA_REQUIRED")


if __name__ == "__main__":
    unittest.main()
