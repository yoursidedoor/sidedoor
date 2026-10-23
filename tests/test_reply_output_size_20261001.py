"""A too-long reply decision is retried with feedback, never BLOCKED on its first try (owner, 2026-10-01).

Kiran's post-cutoff message went straight to BLOCKED CLASSIFICATION_OUTPUT_EXCEEDS_LIMIT on
attempt 1 of 3 because the model's answer was 2720 characters against a 2000 cap.
"""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import json
import unittest
from unittest import mock

import reply_worker as worker
import test_reply_worker_review as fixtures
from backlog_fixture import setUpModule, tearDownModule  # noqa: F401 (fixture messages predate the reply cutoff)


class OverLimitOutputRetriesWithFeedback(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.ReplyWorkerCycleReview("test_success_persists_audit_once_without_changing_source_or_business_tables")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.task_id = self.case.queue()
        self.prompts = []

    def oversize(self, config, prompt, schema, timeout):
        self.prompts.append(prompt)
        output = json.loads(self.case.execute(config, prompt.split("\n\n", 1)[1] if prompt.startswith("OUTPUT_SIZE") else prompt,
                                              schema, timeout))
        output["literal_quote"] = "q" * 7000
        return json.dumps(output)

    def normal(self, config, prompt, schema, timeout):
        self.prompts.append(prompt)
        return self.case.execute(config, prompt.split("\n\n", 1)[1] if prompt.startswith("OUTPUT_SIZE") else prompt,
                                 schema, timeout)

    def saved_input(self):
        return self.case.database.execute("SELECT input,input_sha256 FROM reply_worker_inputs WHERE task_id=?",
                                          (self.task_id,)).fetchone()

    def test_over_limit_waits_then_retries_with_feedback_and_completes(self):
        result, unused = self.case.cycle(effect=self.oversize)
        task = self.case.task(self.task_id)
        self.assertEqual(("RETRY_WAIT", "CLASSIFICATION_OUTPUT_EXCEEDS_LIMIT", 1), (task["status"], task["blocker"], task["attempts"]))
        self.assertIn("concise", task["next_action"])
        self.assertGreater(json.loads(task["result"])["output_characters"], worker.MAX_OUTPUT_CHARACTERS)
        self.assertFalse(self.prompts[0].startswith("OUTPUT_SIZE_FEEDBACK"))
        self.assertEqual(0, self.case.database.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0])

        result, unused = self.case.cycle(task["next_at"], effect=self.normal)
        task = self.case.task(self.task_id)
        self.assertEqual(("COMPLETE", 2), (task["status"], task["attempts"]))
        feedback, separator, rest = self.prompts[1].partition("\n\n")
        self.assertTrue(feedback.startswith("OUTPUT_SIZE_FEEDBACK"))
        self.assertIn("literal_quote was 7000 characters", feedback)
        self.assertIn("at most " + str(worker.MAX_OUTPUT_CHARACTERS), feedback)
        # The saved input is the request alone and binds both attempts and the decision.
        saved = self.saved_input()
        self.assertEqual(saved["input"], rest)
        hashes = {row[0] for row in self.case.database.execute(
            "SELECT input_sha256 FROM reply_worker_attempts WHERE task_id=?", (self.task_id,))}
        self.assertEqual({saved["input_sha256"]}, hashes)
        self.assertEqual(1, self.case.database.execute("SELECT COUNT(*) FROM reply_worker_results WHERE task_id=?",
                                                       (self.task_id,)).fetchone()[0])
        self.assertIn("output_size_feedback_sha256", json.loads(task["result"]))

    def outbox(self):
        return self.case.database.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0]

    def test_a_size_retry_sends_no_alert_for_a_contacted_person_while_a_terminal_hold_does(self):
        # Control first: with the person counted as contacted, a terminal BLOCKED does alert, so
        # the empty outbox after the size retry is the retry's doing, not the fixture's.
        with mock.patch.object(worker, "contacted", return_value=True):
            control = self.case.queue(source_id="control-message")
            self.case.database.execute("UPDATE workflow_tasks SET status='CANCELLED' WHERE task_id=?", (self.task_id,))
            self.case.database.commit()
            self.case.cycle(effect=lambda *unused: "{}")
            self.assertEqual("BLOCKED", self.case.task(control)["status"])
            self.assertEqual(1, self.outbox())
            self.case.database.execute("UPDATE workflow_tasks SET status='QUEUED' WHERE task_id=?", (self.task_id,))
            self.case.database.commit()
            self.case.cycle(effect=self.oversize)
        self.assertEqual("RETRY_WAIT", self.case.task(self.task_id)["status"])
        self.assertEqual(1, self.outbox())

    def test_padded_and_mid_size_outputs_complete_first_time(self):
        def padded(config, prompt, schema, timeout):
            output = json.loads(self.normal(config, prompt, schema, timeout))
            # 3000+ characters compact: over the old 2000 cap, under 6000; plus 12000 of padding.
            output["literal_quote"] = "q" * 1200
            output["quote_span"] = {"start": None, "end": None, "text": "s" * 1200}
            # A padded next_action within 500 once stripped: the worker and apply_reply_decision agree.
            output["next_action"] = "n" * 450 + " " * 120
            text = json.dumps(output, indent=30) + " " * 12000
            self.assertTrue(2000 < worker.compact_size(output) < worker.MAX_OUTPUT_CHARACTERS < len(text))
            return text
        self.case.cycle(effect=padded)
        task = self.case.task(self.task_id)
        self.assertEqual(("COMPLETE", 1), (task["status"], task["attempts"]))
        self.assertEqual("ROUTED", self.case.database.execute("SELECT status FROM pipeline_routing_sources").fetchone()[0])

    def attempt_row(self, number, blocker, output=None):
        with self.case.database:
            self.case.database.execute("INSERT INTO reply_worker_attempts VALUES (?,?,?,?,?,?,?,?)",
                                       (self.task_id, number, fixtures.NOW, None, output, None, "{}", blocker))

    def test_feedback_comes_from_the_latest_answer_and_survives_waits(self):
        base = json.loads(json.dumps(fixtures.inference("a" * 64)))
        self.attempt_row(1, "CLASSIFICATION_OUTPUT_EXCEEDS_LIMIT", json.dumps({**base, "literal_quote": "q" * 7000}))
        self.attempt_row(2, "CLASSIFICATION_FIELD_TOO_LONG", json.dumps({**base, "reason": "r" * 640}))
        # Waits and a timeout after it carry no answer and must not drop the feedback.
        self.attempt_row(3, "PROVIDER_USAGE_LIMIT_WAIT")
        self.attempt_row(4, "DAILY_COMPUTE_BUDGET_EXHAUSTED")
        self.attempt_row(5, "CLASSIFICATION_TIMEOUT")
        feedback = worker.size_feedback(self.case.database, self.task_id)
        self.assertIn("reason was 640 characters", feedback)
        self.assertNotIn("7000", feedback)
        # A later usable-size answer (here one refused for another reason) ends the feedback.
        self.attempt_row(6, "CLASSIFICATION_OUTPUT_BINDING_MISMATCH", json.dumps(base))
        self.assertIsNone(worker.size_feedback(self.case.database, self.task_id))
        # A byte-limit failure saves no output and still gets feedback.
        self.attempt_row(7, "CLASSIFICATION_OUTPUT_EXCEEDS_LIMIT")
        self.assertIn("more than " + str(worker.MAX_OUTPUT_BYTES) + " bytes", worker.size_feedback(self.case.database, self.task_id))

    def test_repeated_over_limit_uses_the_existing_attempts_then_exhausts(self):
        now = fixtures.NOW
        for attempt in range(1, 4):
            self.case.cycle(now, effect=self.oversize)
            task = self.case.task(self.task_id)
            self.assertEqual("EXHAUSTED" if attempt == 3 else "RETRY_WAIT", task["status"])
            now = task["next_at"] or now
        self.assertEqual(3, len(self.prompts))
        self.assertTrue(all(prompt.startswith("OUTPUT_SIZE_FEEDBACK") for prompt in self.prompts[1:]))


if __name__ == "__main__":
    unittest.main()
