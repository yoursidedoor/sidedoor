"""Cost cuts C2 and C3 (2026-10-22): note composition and the recovery coordinators call
OpenRouter directly (text_model.complete, low reasoning, strict schema, price routing) when a text
model is configured and no executor is injected. A fake transport only; no network, no codex."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import application_recovery_decision
import manual_application_recovery_decision_slice as manual
import outreach_agent
import outreach_promotion as promotion
import research_recovery
import text_model
from tests import test_outreach_composition as composition

SECRET = "sk-or-v1-synthetic-secret-0123456789"
USAGE = {"prompt_tokens": 5000, "completion_tokens": 200, "cost": 0.002, "completion_tokens_details": {"reasoning_tokens": 4}}


def reply(content, status=200):
    return status, json.dumps({"model": "z-ai/glm-5.3", "usage": USAGE,
                               "choices": [{"finish_reason": "stop", "message": {"content": content}}]}).encode()


class Transport:
    """Answers each request with the next scripted item: a callable(body) -> (status, bytes) or an exception."""

    def __init__(self, *script):
        self.script, self.requests = list(script), []

    def __call__(self, url, data, headers, timeout):
        body = json.loads(data)
        self.requests.append((url, body, timeout))
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item(body)


class DirectCompositionTests(unittest.TestCase):
    # The CompositionTests fixture, borrowed method by method so its tests are not collected twice.
    _fixture = composition.CompositionTests
    setUp, scaffold, good_execute = _fixture.setUp, _fixture.scaffold, _fixture.good_execute
    del _fixture
    config = {"model": "stub", "text_model": {"model": "z-ai/glm-5.3"}}

    @staticmethod
    def note(body):
        prompt = body["messages"][-1]["content"]
        packet = json.loads(prompt.split("UNTRUSTED_SOURCE_JSON:\n", 1)[1])
        return reply(json.dumps({"output_version": "outreach-compose-v1", "source_sha256": packet["source_sha256"],
                                 "decision": "COMPOSED", "reason": "stub", "invitation": composition.GOOD,
                                 "first_message": None, "proof_point": "L2",
                                 "question": "What is the hardest part of your API work?", "facts": composition.FACTS}))

    def run_direct(self, *script):
        transport = Transport(*script)
        with mock.patch.object(text_model, "key", return_value=SECRET), mock.patch.object(text_model, "_post", transport):
            try:
                return outreach_agent.compose_draft(promotion.ROOT, self.database, dict(self.config), self.scaffold(),
                                                    None, composition.NOW), transport
            except Exception as error:
                return error, transport

    def audit(self, kind):
        rows = self.database.execute("SELECT record FROM outreach_agent_audit WHERE kind=? ORDER BY id", (kind,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def test_valid_composition_is_one_low_effort_strict_call_with_usage_recorded(self):
        result, transport = self.run_direct(self.note)
        self.assertEqual(result["state"], "COMPOSED", result)
        url, body, timeout = transport.requests[0]
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(url, text_model.BASE_URL + "/chat/completions")
        self.assertEqual((body["reasoning"], body["max_tokens"]), ({"effort": "low"}, 4000))
        self.assertEqual(body["provider"], {"sort": "price", "require_parameters": True})
        schema = json.loads(outreach_agent.resources.read_text("outreach-compose-schema.json"))
        self.assertEqual(body["response_format"]["json_schema"], {"name": "outreach_composition", "strict": True, "schema": schema})
        self.assertTrue(body["messages"][-1]["content"].startswith(outreach_agent.resources.read_text("outreach-compose-prompt.md")))
        decision = self.audit("DECISION")[0]
        self.assertEqual((decision["route"], decision["reasoning_effort"]), ("openrouter_chat_completions", "low"))
        self.assertEqual(decision["calls"][0]["usage"],
                         {"prompt_tokens": 5000, "completion_tokens": 200, "reasoning_tokens": 4, "cost": 0.002})
        self.assertEqual(self.audit("OBSERVATION")[0]["provenance"]["route"], "openrouter_chat_completions")
        booked = [row[0] for row in self.database.execute("SELECT action_id FROM worker_compute_reservations")]
        self.assertTrue(any(key.startswith("outreach-compose:") for key in booked), booked)

    def test_a_composition_direct_call_writes_a_model_calls_row(self):
        # C2 x C4: the direct call goes through text_model.complete, so it is recorded like the judge's.
        import sqlite3
        import model_calls
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "orchestrator.sqlite3"
        self.addCleanup(setattr, model_calls, "_DATABASE", model_calls._DATABASE)
        model_calls.use(path)
        result, unused = self.run_direct(self.note)
        self.assertEqual(result["state"], "COMPOSED", result)
        database = sqlite3.connect(path)
        try:
            rows = database.execute("SELECT route, outcome, worker, model, prompt_tokens, completion_tokens, "
                                    "reasoning_tokens, cost FROM model_calls").fetchall()
        finally:
            database.close()
        self.assertEqual(rows, [("openrouter_direct", "ok", "outreach_agent", "z-ai/glm-5.3", 5000, 200, 4, 0.002)])

    def test_invalid_answer_gets_a_repair_turn_through_the_same_call(self):
        result, transport = self.run_direct(lambda body: reply('{"not": "valid"}'), self.note)
        self.assertEqual(result["state"], "COMPOSED", result)
        self.assertEqual(len(transport.requests), 2)
        self.assertIn("repair_feedback", transport.requests[1][1]["messages"][-1]["content"])
        self.assertEqual(len(self.audit("REPAIR")), 1)
        refunds = self.database.execute("SELECT action_id FROM worker_compute_refunds WHERE action_id LIKE 'outreach-compose-repair:%'").fetchall()
        self.assertEqual(len(refunds), 1)
        self.assertEqual(len(self.audit("DECISION")[0]["calls"]), 2)

    def test_a_402_is_the_same_retryable_held_with_its_status_and_never_the_key(self):
        error = {"error": {"message": "This request requires more credits. key " + SECRET, "code": 402}}
        result, _ = self.run_direct(lambda body: (402, json.dumps(error).encode()))
        self.assertIsInstance(result, outreach_agent.reply_worker.Held)
        self.assertEqual((result.code, result.retry), ("SUBSCRIPTION_EXECUTOR_FAILED", True))
        failure = self.audit("FAILURE")[0]
        self.assertEqual(failure["executor"]["http_status"], 402)
        self.assertNotIn(SECRET, json.dumps(self.audit("FAILURE")))

    def test_a_timeout_is_the_same_retryable_held(self):
        result, _ = self.run_direct(TimeoutError("timed out"))
        self.assertIsInstance(result, outreach_agent.reply_worker.Held)
        self.assertEqual((result.code, result.retry), ("CLASSIFICATION_TIMEOUT", True))

    def test_an_injected_executor_still_bypasses_the_direct_call(self):
        execute, calls = self.good_execute()
        with mock.patch.object(text_model, "_post", side_effect=AssertionError("no direct call")):
            result = outreach_agent.compose_draft(promotion.ROOT, self.database, dict(self.config), self.scaffold(),
                                                  execute, composition.NOW)
        self.assertEqual((result["state"], len(calls)), ("COMPOSED", 1))


class DirectRecoveryDecisionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="recovery-direct-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = dict(manual.CONFIG, text_model={"model": "z-ai/glm-5.3"})
        patcher = mock.patch("subprocess.Popen", side_effect=AssertionError("No codex"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def decide(self, *script):
        transport = Transport(*script)
        with mock.patch.object(text_model, "key", return_value=SECRET), mock.patch.object(text_model, "_post", transport):
            return application_recovery_decision.decide(self.root, self.config, manual.packet(), 30), transport

    def test_valid_decision_is_one_low_effort_strict_call_with_usage_recorded(self):
        result, transport = self.decide(lambda body: reply(json.dumps(manual.decision())))
        self.assertEqual(result["method"], "official_posting")
        url, body, timeout = transport.requests[0]
        self.assertEqual((body["reasoning"], body["max_tokens"], timeout), ({"effort": "low"}, 4000, 30))
        self.assertEqual(body["provider"], {"sort": "price", "require_parameters": True})
        self.assertEqual(body["response_format"]["json_schema"]["schema"], application_recovery_decision.DECISION_SCHEMA)
        self.assertEqual(body["messages"][-1]["content"], application_recovery_decision.decision_prompt(manual.packet()))
        call = result["artifacts"]["calls"][0]
        self.assertEqual((call["route"], call["reasoning_effort"]), ("openrouter_chat_completions", "low"))
        self.assertEqual(call["usage"]["cost"], 0.002)
        working = Path(result["artifacts"]["directory"])
        self.assertEqual(json.loads((working / "decision.json").read_text()), manual.decision())
        self.assertTrue((working / "prompt.txt").is_file() and (working / "recovery-schema.json").is_file())

    def test_invalid_decision_is_refused_by_the_same_validation(self):
        # The coordinator has no repair turn (nor had it under codex): its caller holds and retries.
        with self.assertRaisesRegex(ValueError, "Recovery decision envelope invalid"):
            self.decide(lambda body: reply('{"action": "RETRY"}'))

    def test_a_402_is_the_same_failure_as_a_non_zero_codex_exit_with_its_status(self):
        error = {"error": {"message": "credits " + SECRET, "code": 402}}
        with self.assertRaisesRegex(RuntimeError, "RECOVERY_DECISION_EXECUTOR_FAILED") as raised:
            self.decide(lambda body: (402, json.dumps(error).encode()))
        call = raised.exception.artifacts["calls"][0]
        self.assertEqual(call["http_status"], 402)
        self.assertNotIn(SECRET, json.dumps(call))
        log = Path(raised.exception.artifacts["log"]).read_text()
        self.assertIn("402", log)
        self.assertNotIn(SECRET, log)

    def test_a_timeout_is_a_timeout_error(self):
        with self.assertRaises(TimeoutError):
            self.decide(TimeoutError("timed out"))

    def test_research_recovery_decision_uses_the_same_direct_call(self):
        transport = Transport(lambda body: reply(json.dumps({"action": "ABANDON", "method": "", "instructions": "",
                                                             "reason": "nothing left", "progress": False})))
        with mock.patch.object(text_model, "key", return_value=SECRET), mock.patch.object(text_model, "_post", transport):
            value = research_recovery.research_recovery_decision(
                self.root, self.config, {"industry": "x", "supported_methods": {}}, 30)
        self.assertEqual(value["action"], "ABANDON")
        self.assertEqual(transport.requests[0][1]["response_format"]["json_schema"]["schema"], research_recovery.DECISION_SCHEMA)


if __name__ == "__main__":
    unittest.main()
