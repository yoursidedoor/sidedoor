"""Outreach pre-send judge: a direct OpenRouter call with low reasoning (owner, 2026-10-13). No network."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import json
import unittest
from unittest import mock

import outreach_agent
import text_model
import test_outreach_agent

SECRET = "sk-or-v1-synthetic-secret-0123456789"


class DirectOutreachJudgeTests(unittest.TestCase):
    # The OutreachAgentTests fixture (HeyReach reads, synthetic judge), borrowed method by method
    # so its own tests are not collected a second time here.
    _fixture = test_outreach_agent.OutreachAgentTests
    setUp, read, send, judge, make_adapter = _fixture.setUp, _fixture.read, _fixture.send, _fixture.judge, _fixture.make_adapter
    del _fixture

    def run_judge(self, status, answer=None, error=None, phase=None):
        self.config["text_model"] = {"model": "z-ai/glm-5.3"}
        self.executor = None  # no injected executor: the configured text model is called directly
        self.adapter = self.make_adapter(self.database)
        self.requests = []

        def transport(url, data, headers, timeout):
            body = json.loads(data)
            self.requests.append((url, body, timeout))
            if error is not None:
                return status, json.dumps(error).encode()
            schema = body["messages"][0]["content"].split("\n", 1)[1]
            content = "```json\n" + self.judge(self.config, body["messages"][-1]["content"], schema, timeout) + "\n```"
            return status, json.dumps({"model": "z-ai/glm-5.3", "usage": answer,
                                       "choices": [{"finish_reason": "stop", "message": {"content": content}}]}).encode()

        with mock.patch.object(text_model, "key", return_value=SECRET), mock.patch.object(text_model, "_post", transport):
            if phase is not None:
                return self.adapter._judge(self.action, phase, self.config)
            return self.adapter.inspect(self.action, self.config)

    def audit(self, kind):
        row = self.database.execute("SELECT record FROM outreach_agent_audit WHERE kind=?", (kind,)).fetchone()
        return json.loads(row[0]) if row else None

    def test_first_note_judge_is_one_low_effort_call_with_usage_recorded(self):
        usage = {"prompt_tokens": 9000, "completion_tokens": 150, "cost": 0.0012,
                 "completion_tokens_details": {"reasoning_tokens": 3}}
        self.run_judge(200, usage)
        url, body, timeout = self.requests[0]
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(url, text_model.BASE_URL + "/chat/completions")
        self.assertEqual((body["model"], body["reasoning"], body["max_tokens"], timeout),
                         ("z-ai/glm-5.3", {"effort": "low"}, 4000, 1))
        self.assertIn('"output_version"', body["messages"][0]["content"])
        self.assertEqual(body["response_format"]["json_schema"]["schema"]["properties"]["reason"]["maxLength"], 700)
        self.assertEqual(body["provider"], {"sort": "price", "require_parameters": True})
        decision = self.audit("DECISION")
        self.assertEqual(decision["decision"]["decision"], "READY")
        self.assertEqual((decision["route"], decision["reasoning_effort"]), ("openrouter_chat_completions", "low"))
        self.assertEqual(decision["calls"][0]["usage"],
                         {"prompt_tokens": 9000, "completion_tokens": 150, "reasoning_tokens": 3, "cost": 0.0012})
        reference = self.database.execute("SELECT id FROM outreach_agent_audit WHERE kind='OBSERVATION'").fetchone()[0]
        settled = self.database.execute("SELECT seconds FROM worker_compute_reservations WHERE action_id=?",
                                        ("outreach-ai:" + reference,)).fetchone()
        self.assertIsNotNone(settled)

    def test_a_402_is_the_same_retryable_failure_with_its_status_and_never_the_key(self):
        error = {"error": {"message": "This request requires more credits. key " + SECRET, "code": 402}}
        with self.assertRaisesRegex(TimeoutError, "OUTREACH_AI_TRANSIENT_FAILURE"):
            self.run_judge(402, error=error)
        failure = self.audit("FAILURE")
        self.assertEqual(failure["executor"]["http_status"], 402)
        self.assertIn("HTTP 402", failure["executor"]["error"])
        self.assertIsNone(self.audit("DECISION"))
        saved = json.dumps([json.loads(row[0]) for row in self.database.execute("SELECT record FROM outreach_agent_audit")])
        self.assertNotIn(SECRET, saved)


class LaterPhaseJudgeTests(DirectOutreachJudgeTests):
    """Owner, 2026-10-27: the judge after the first note (other phases, repair turns) is a direct call too."""

    test_first_note_judge_is_one_low_effort_call_with_usage_recorded = None
    test_a_402_is_the_same_retryable_failure_with_its_status_and_never_the_key = None

    def test_a_reconciliation_judgment_is_a_direct_call_not_codex(self):
        with mock.patch("subprocess.Popen", side_effect=AssertionError("no codex")):
            self.run_judge(200, {"prompt_tokens": 10, "completion_tokens": 2}, phase="RECONCILIATION")
        self.assertEqual(1, len(self.requests))
        url, body, unused_timeout = self.requests[0]
        self.assertEqual((body["reasoning"], body["response_format"]["json_schema"]["strict"]), ({"effort": "low"}, True))
        provenance = self.audit("OBSERVATION")["provenance"]
        self.assertEqual((provenance["executable"], provenance["route"]), (None, "openrouter_chat_completions"))
        self.assertEqual("openrouter_chat_completions", self.audit("DECISION")["route"])

    def test_a_repair_turn_is_a_direct_call_too(self):
        self.decision_override["reason"] = "x" * 800   # over the bound: _validate refuses, one repair turn follows
        answers = []
        original = self.judge

        def judge(config, prompt, schema, timeout):
            answers.append(prompt)
            if len(answers) > 1:
                self.decision_override.pop("reason", None)
            return original(config, prompt, schema, timeout)
        self.judge = judge
        self.run_judge(200, {"prompt_tokens": 10, "completion_tokens": 2}, phase="RECONCILIATION")
        self.assertEqual(2, len(self.requests))
        self.assertTrue(all(request[0].endswith("/chat/completions") for request in self.requests))


if __name__ == "__main__":
    unittest.main()
