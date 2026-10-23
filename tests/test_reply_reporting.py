import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import contextlib
import json
import sqlite3
from unittest import mock

import orchestrator
import unittest

import engagement
import reporting
import reply_worker
import test_reply_worker as fixtures
from test_pipeline_routing import NOW, PROFILE
from backlog_fixture import setUpModule, tearDownModule  # noqa: F401 (fixture messages predate the reply cutoff)
import synthetic_resume  # noqa: F401 (synthetic designated resume; the owner's is not in the repo)


# `holds` became `observations` on 2026-09-23 (owner): every one of these signals is
# recorded and none of them blocks. The safety properties each case also asserts -
# can_advance_conversion, can_send, no conversion rows - are unchanged.


class ReplyReportingTests(unittest.TestCase):
    setUp = fixtures.ReplyWorkerTests.setUp
    process = fixtures.ReplyWorkerTests.process
    enqueue = fixtures.ReplyWorkerTests.enqueue
    run_cycle = fixtures.ReplyWorkerTests.run_cycle

    def test_worker_to_saved_report_keeps_private_text_in_immutable_results(self):
        private_message = "Private mock question about the role?"
        private_proposal = "Private proposed response to the sender."
        private_next_action = "Privately review: " + private_message
        private_reason = "Private interpretation of the sender's circumstances."
        task_id = self.enqueue(private_message)

        def output(value):
            value.update(unknown_factual_requirements=["ROLE_ASSOCIATION"],
                         reason=private_reason, next_action=private_next_action,
                         reply_proposal={"text": private_proposal, "basis_quote": value["quote_span"]})

        self.mutate_output = output
        self.run_cycle()
        original = self.database.execute("SELECT handoff FROM reply_worker_results").fetchone()[0]
        payload = reporting.daily(self.database, dict(self.config, daily_report_hour=0), NOW)
        summary = payload["reply_decisions"]
        record = summary["records"][0]
        self.assertEqual("SAVED_AI_INTERPRETATION_NOT_CONFIRMED_REFERRAL_OR_DELIVERY", summary["basis"])
        self.assertFalse(summary["checked_by_this_report"])
        self.assertEqual(task_id, record["task_id"])
        self.assertEqual(("heyreach", "message-1", "linkedin.com/in/mock-contact"),
                         (record["source"], record["source_id"], record["identity"]))
        self.assertEqual("QUESTION", record["ai_label"])
        self.assertEqual(["ROLE_ASSOCIATION"], record["missing_facts"])
        self.assertIn("UNKNOWN_FACT:ROLE_ASSOCIATION", record["observations"])
        self.assertEqual("2026-09-17T20:00:00+00:00", record["review_at"])
        self.assertTrue(record["next_action"])
        self.assertTrue(record["unsent_draft_available"])
        self.assertFalse(record["can_send"])
        self.assertFalse(record["can_advance_conversion"])
        self.assertIsNone(record["verified_business_state"])
        saved_report = self.database.execute("SELECT payload FROM daily_reports").fetchone()[0]
        telegram = self.database.execute("SELECT message FROM notification_outbox WHERE event_key LIKE 'daily-report:%'").fetchone()[0]
        for secret in (private_message, private_proposal, private_next_action, private_reason):
            self.assertIn(secret, original)
            self.assertNotIn(secret, saved_report)
            self.assertNotIn(secret, telegram)
            self.assertNotIn(secret, json.dumps(payload))
        # Telegram carries the short summary; the full report (saved) keeps these lines.
        full = reporting.full_report(self.database, json.loads(saved_report)["day"])
        self.assertNotIn(private_message, full)
        self.assertIn("Saved AI reply interpretations", full)
        self.assertIn("Not confirmed referrals or delivery", full)
        self.assertEqual(original, self.database.execute("SELECT handoff FROM reply_worker_results").fetchone()[0])

    def test_closed_alias_overrides_saved_agreement_without_changing_handoff(self):
        self.enqueue("Happy to help with that role!")
        self.mutate_output = lambda value: value.update(label="AGREEMENT")
        self.run_cycle()
        original = self.database.execute("SELECT handoff FROM reply_worker_results").fetchone()[0]
        ready = reporting.snapshot(self.database, self.config, NOW)["reply_decisions"]["records"][0]
        self.assertEqual("DECISION_READY", ready["state"])
        self.assertTrue(ready["can_advance_conversion"])
        self.assertFalse(ready["can_send"])
        self.assertFalse(ready["unsent_draft_available"])
        self.assertIsNone(ready["verified_business_state"])
        alias = "https://www.linkedin.com/in/mock-alias"
        self.database.execute("INSERT INTO rw_aliases VALUES (?,?,?,?)", ("contact", alias, PROFILE, "{}"))
        engagement.close_outreach(self.database, alias, "Mock explicit closure", NOW)
        closed = reporting.snapshot(self.database, self.config, NOW)["reply_decisions"]["records"][0]
        self.assertEqual("CLOSED", closed["state"])
        self.assertIn("OUTREACH_CLOSED", closed["holds"])
        self.assertFalse(closed["can_advance_conversion"])
        self.assertFalse(closed["can_send"])
        self.assertEqual(original, self.database.execute("SELECT handoff FROM reply_worker_results").fetchone()[0])
        self.database.commit()
        with contextlib.closing(sqlite3.connect(self.path)) as reopened:
            self.assertEqual(closed, reporting.snapshot(reopened, self.config, NOW)["reply_decisions"]["records"][0])

    def test_absent_table_is_unknown_and_not_created_by_snapshot(self):
        with contextlib.closing(sqlite3.connect(":memory:")) as database:
            summary = reporting.snapshot(database, {}, NOW)["reply_decisions"]
            self.assertEqual("UNKNOWN_TABLE_ABSENT", summary["state"])
            self.assertIsNone(summary["records"])
            self.assertIsNone(database.execute("SELECT name FROM sqlite_master WHERE name='reply_worker_results'").fetchone())

    def test_empty_results_are_known_empty(self):
        reply_worker.initialize(self.database)
        summary = reporting.snapshot(self.database, self.config, NOW)["reply_decisions"]
        self.assertEqual("KNOWN_SAVED", summary["state"])
        self.assertEqual([], summary["records"])

    def test_service_status_publishes_fresh_decisions_without_private_draft_text(self):
        self.enqueue("Please share the details.")
        self.run_cycle()
        self.database.commit()
        with mock.patch.object(orchestrator, "worker_step", return_value={"state": "UNRELATED_STEP_SKIPPED"}), \
                mock.patch.object(orchestrator, "write_json") as write:
            result = orchestrator.service_cycle(orchestrator.ROOT, {"scope": "full"}, self.database)
        self.assertEqual(1, len(result["reply_decisions"]["records"]))
        status = next(call.args[1] for call in write.call_args_list if call.args[0].name == "service-status.json")
        self.assertEqual(result["reply_decisions"], status["reply_decisions"])
        self.assertNotIn("Please share the details.", json.dumps(status))
        self.assertFalse(status["reply_decisions"]["records"][0]["can_send"])

    def test_malformed_or_incomplete_handoffs_do_not_expose_private_fields(self):
        reply_worker.initialize(self.database)
        for task_id, handoff in (("malformed", "{"), ("list", "[]"), ("missing", "{}"),
                                 ("unexpected", json.dumps({"classification": {
                                     "label": "PRIVATE", "unknown_factual_requirements": ["PRIVATE"],
                                     "review_at": "PRIVATE", "reply_proposal": {"text": "PRIVATE"}},
                                     "holds": ["PRIVATE"], "required_next_action": "PRIVATE",
                                     "state": "PRIVATE", "can_send": True, "can_advance_conversion": True}))):
            self.database.execute("INSERT INTO reply_worker_results VALUES (?,?,?,?,?)",
                                  (task_id, reply_worker.VERSION, "input-hash", "output-hash", handoff))
        summary = reporting.snapshot(self.database, self.config, NOW)["reply_decisions"]
        self.assertEqual(4, len(summary["records"]))
        self.assertNotIn("PRIVATE", json.dumps(summary))
        for record in summary["records"]:
            self.assertFalse(record["can_send"])
            self.assertFalse(record["can_advance_conversion"])
            self.assertEqual("UNKNOWN", record["state"])


if __name__ == "__main__":
    unittest.main()
