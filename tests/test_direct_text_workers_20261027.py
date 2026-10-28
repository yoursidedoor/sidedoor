"""Owner, 2026-10-27: the follow-up writer and send judge and the reply repair are direct GLM calls
(text_model.complete), never Codex. The model is a fake behind text_model.complete; no network."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import json
import sqlite3
import unittest
from unittest.mock import patch

import engagement
import followup_execution as followup
import manual_followup_e2e as manual
import reply_execution
import reply_worker
import text_model


def fake_complete(execute, calls):
    """text_model.complete answering with `execute(config, prompt, schema, timeout)`, recording each call."""
    def complete(config, prompt, **named):
        calls.append(named)
        schema = named["system"].split("\n", 1)[1]
        content = execute(config, prompt, schema, named["timeout"])
        return {"content": content, "usage": {}, "finish_reason": "stop", "model": "z-ai/glm-5.3"}
    return complete


class FollowupDirectTests(unittest.TestCase):
    def test_followup_writer_and_send_judge_are_direct_calls(self):
        with manual.offline():
            fixture = manual.ProviderFixture()
            self.addCleanup(fixture.close)
            fixture.config["text_model"] = {"model": "z-ai/glm-5.3"}
            fixture.config["followup_execution"]["dispatch_enabled"] = True
            # No injected executor: the product default.
            fixture.adapter = followup.FollowupAdapter(manual.ROOT, fixture.database, fixture.config, fixture)
            calls = []
            with patch.object(text_model, "complete", side_effect=fake_complete(fixture.execute, calls)), \
                    patch("outreach_provider._now", side_effect=lambda: fixture.now.isoformat()), \
                    patch("followup_execution._now",
                          side_effect=lambda value=None: engagement.timestamp(fixture.now if value is None else value)):
                followup.cycle(manual.ROOT, fixture.database, fixture.config, fixture.adapter,
                               candidates=[fixture.candidate], now=fixture.now)
        names = [call["response_format"]["json_schema"]["name"] for call in calls]
        self.assertEqual(["followup", "followup_send_judge"], names[:2])
        self.assertTrue(all(call["effort"] == "low" and call["response_format"]["json_schema"]["strict"] for call in calls))
        self.assertEqual(1, len(fixture.sends))
        provenance = [json.loads(row[0])["provenance"] for row in fixture.database.execute(
            "SELECT record FROM outreach_agent_audit WHERE kind='OBSERVATION' AND phase='INSPECTION'")]
        self.assertEqual((None, "openrouter_chat_completions"), (provenance[0]["executable"], provenance[0]["route"]))

    def test_no_text_model_is_unavailable_never_codex(self):
        with patch("subprocess.Popen", side_effect=AssertionError("no codex")), \
                self.assertRaises(reply_worker.Held) as raised:
            reply_worker.text_executor("followup")({"model": "m", "codex": "/bin/codex"}, "prompt", "{}", 5)
        self.assertEqual(("SUBSCRIPTION_EXECUTOR_UNAVAILABLE", False), (raised.exception.code, raised.exception.retry))


class SlotWaitTests(unittest.TestCase):
    def test_a_slot_wait_is_spent_from_the_timeout(self):
        # Review B2: with wait_in_timeout the pacing wait counts against the call's deadline.
        import provider_pacing
        clock = iter([0.0, 9.0] + [9.0] * 20)
        with patch.object(provider_pacing, "acquire", return_value=None) as acquire, \
                patch.object(text_model.time, "monotonic", side_effect=lambda: next(clock)), \
                patch.object(text_model, "key", return_value="sk-synthetic-0000"):
            with self.assertRaises(TimeoutError):
                text_model.complete({"text_model": {"model": "m"}}, "p", timeout=5, max_wait=60, wait_in_timeout=True,
                                    transport=lambda *a: self.fail("sent after the deadline"))
        self.assertEqual(60, acquire.call_args.kwargs["max_wait"])


class ReplyRepairDirectTests(unittest.TestCase):
    def test_reply_repair_is_a_direct_call_named_reply_decision(self):
        class Adapter:   # the product adapter's seam: no injected executor
            injected_execute = False

            def text_execute(self, name, calls=None, **named):
                return reply_worker.text_executor(name, calls, **named)

        seen = {}

        def rewrite(config, prompt, schema, timeout):
            seen["executor"] = "called"
            raise reply_worker.Held("CLASSIFICATION_TIMEOUT", retry=True)

        calls = []
        action = {"purpose": "REPLY", "action_id": "a1", "message": "old", "profile": "https://www.linkedin.com/in/x",
                  "reply_task_id": "t1"}
        request = {"source_snapshot": {"payload": json.dumps({"inbox": {"text": "hi"}})}}
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.execute("CREATE TABLE outreach_actions (action_id TEXT, payload_hash TEXT, state TEXT, claimed_at TEXT)")
        database.execute("CREATE TABLE outreach_agent_audit (id TEXT, action_id TEXT, phase TEXT, kind TEXT, observed_at TEXT, record TEXT)")
        database.execute("INSERT INTO outreach_actions VALUES ('a1','h','PENDING',NULL)")
        import workflow_tasks
        workflow_tasks.initialize(database)
        database.commit()
        with patch.object(reply_execution, "backlog", return_value=False), \
                patch.object(reply_execution, "context", return_value=(request, {}, None)), \
                patch.object(reply_execution.outreach_agent, "pending_objection", return_value="Too long"), \
                patch.object(text_model, "complete", side_effect=fake_complete(rewrite, calls)), \
                patch.object(text_model, "settings", return_value={"model": "z-ai/glm-5.3"}):
            self.assertIsNone(reply_execution.repair_rejected(manual.ROOT, database, {"timezone": "UTC"}, Adapter(), action, {}))
        self.assertEqual("called", seen["executor"])
        self.assertEqual(["reply_decision"], [call["response_format"]["json_schema"]["name"] for call in calls])
        self.assertEqual(reply_worker.REPLY_MAX_TOKENS, calls[0]["max_tokens"])


if __name__ == "__main__":
    unittest.main()
