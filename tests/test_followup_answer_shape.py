import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import json
import unittest
from unittest.mock import patch

import engagement

import followup_execution as followup
import manual_followup_e2e as manual
import resources


def conforms(value, schema):
    """The few JSON Schema keywords followup-schema.json uses (jsonschema is not a dependency)."""
    if "anyOf" in schema:
        return any(conforms(value, branch) for branch in schema["anyOf"])
    kind = {"object": dict, "string": str, "null": type(None)}[schema["type"]]
    if not isinstance(value, kind) or ("const" in schema and value != schema["const"]):
        return False
    if kind is str and not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", len(value)):
        return False
    if kind is dict:
        return (set(schema["required"]) <= set(value) and set(value) <= set(schema["properties"])
                and all(conforms(value[key], schema["properties"][key]) for key in value))
    return True


class FollowupAnswerShapeTests(unittest.TestCase):
    def setUp(self):
        self.boundaries = manual.offline()
        self.boundaries.__enter__()
        self.addCleanup(self.boundaries.__exit__, None, None, None)
        self.fixture = manual.Fixture()
        self.addCleanup(self.fixture.close)

    def answer(self, change):
        original = self.fixture.execute

        def execute(config, prompt, schema, timeout):
            return json.dumps(dict(json.loads(original(config, prompt, schema, timeout)), **change))
        self.fixture.execute = execute

    def test_schema_ties_message_to_decision(self):
        schema = json.loads(resources.read_text("followup-schema.json"))
        base = {"output_version": followup.VERSION, "source_sha256": "x", "reason": "r", "next_action": "n"}
        self.assertTrue(conforms(dict(base, decision="COMPOSE", message="Hello"), schema))
        self.assertTrue(conforms(dict(base, decision="HOLD", message=None), schema))
        for decision, message in (("COMPOSE", None), ("COMPOSE", ""), ("HOLD", "Hello")):
            with self.subTest(decision=decision, message=message):
                self.assertFalse(conforms(dict(base, decision=decision, message=message), schema))

    def test_invalid_answer_waits_for_a_retry_instead_of_blocking(self):
        self.answer({"message": None})
        result = self.fixture.run()
        self.assertEqual(result["composition"]["items"][0]["blocker"], "FOLLOWUP_MESSAGE_INVALID")
        task = self.fixture.database.execute("SELECT status,blocker FROM workflow_tasks").fetchone()
        self.assertEqual((task[0], task[1]), ("RETRY_WAIT", "FOLLOWUP_MESSAGE_INVALID"))
        self.assertFalse(self.fixture.sends)

    def test_queued_followup_has_no_dashes(self):
        self.answer({"message": "Following up — happy to share details.\nMonday–Friday works."})
        self.fixture.run()
        message = json.loads(self.fixture.database.execute("SELECT payload FROM outreach_actions").fetchone()[0])["message"]
        self.assertNotIn("—", message)
        self.assertNotIn("–", message)
        self.assertIn("\n", message)

    def test_send_judge_context_carries_the_dash_free_text(self):
        self.answer({"message": "Following up \u2014 happy to share details."})
        self.fixture.run()
        action = json.loads(self.fixture.database.execute("SELECT payload FROM outreach_actions").fetchone()[0])
        context = followup.judgment_context(self.fixture.database, action)
        self.assertEqual(context["followup_decision"]["message"], action["message"])
        self.assertNotIn("\u2014", action["message"])
    def test_a_followup_queued_before_dash_removal_still_matches_at_the_judge(self):
        raw = "Following up \u2014 happy to share details."
        self.answer({"message": raw})
        self.fixture.run()
        action = json.loads(self.fixture.database.execute("SELECT payload FROM outreach_actions").fetchone()[0])
        context = followup.judgment_context(self.fixture.database, dict(action, message=raw))   # pre-upgrade copy
        self.assertEqual(context["followup_decision"]["message"], raw)

    def test_failed_attempt_refunds_its_unused_reservation(self):
        self.answer({"message": None})
        # The fixture's clock, so the attempt's measured window is its real (short) length.
        with patch("followup_execution._now", side_effect=lambda value=None:
                   engagement.timestamp(self.fixture.now if value is None else value)):
            self.fixture.run()
        refunds = self.fixture.database.execute(
            "SELECT action_id,seconds FROM worker_compute_refunds WHERE action_id LIKE 'followup:%'").fetchall()
        self.assertEqual(len(refunds), 1)
        self.assertGreater(refunds[0][1], 0)


class DashReplacementTests(unittest.TestCase):
    def test_comma_sits_against_the_word_and_never_before_punctuation(self):
        import outreach_promotion
        import reply_execution
        for text, expected in (("Thanks for the chat \u2014 I'd love", "Thanks for the chat, I'd love"),
                               ("End \u2014.", "End."), ("Hi, \u2014 there", "Hi, there"),
                               ("a\u2014b", "a, b"), ("2\u20133 years", "2-3 years")):
            with self.subTest(text=text):
                self.assertEqual(outreach_promotion.without_dashes(text), expected)
        self.assertEqual(reply_execution.without_dashes("Thanks \u2014 happy.\n9\u20135 works."), "Thanks, happy.\n9-5 works.")

    def test_failure_page_has_words_for_followup_answer_codes(self):
        import failures_page
        for code in ("FOLLOWUP_MESSAGE_INVALID", "FOLLOWUP_DECISION_BINDING_INVALID", "FOLLOWUP_REASON_REQUIRED",
                     "FOLLOWUP_OUTPUT_TOO_LARGE", "FOLLOWUP_OUTPUT_INVALID"):
            with self.subTest(code=code):
                self.assertIn(code, failures_page.CAUSE_WORDS)


if __name__ == "__main__":
    unittest.main()
