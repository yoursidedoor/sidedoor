import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import json
import sqlite3
import unittest

import applications
import conversion
import engagement
import pipeline_routing as routing
import reply_worker
import research_workflow as research
import workflow_tasks
from test_pipeline_routing import NOW, message


BLOCKER = "JOB_BOUND_CLASSIFICATION_SCOPE_REQUIRED"


class ReplyScopeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        self.addCleanup(lambda: self.database.close())
        for module in (applications, research, engagement, conversion, routing, workflow_tasks, reply_worker):
            module.initialize(self.database)
        self.config = {"pipeline_routing": {"account_ids": [123]}, "reply_worker": {"enabled": True}}

    def blocked_task(self, source_id="scope-message"):
        event = message("Could you explain?", source_id)
        event["raw"]["quoted"] = event["inbox"]["quoted"] = None
        engagement.record_event(self.database, event)
        routing.cycle(self.database, self.config, NOW)
        source = routing.one(self.database,
            "SELECT * FROM pipeline_routing_sources WHERE source_id=?",
            (routing.encoded(["heyreach", source_id]),))
        task_id = json.loads(source["result"])["review_task_id"]
        self.database.commit()
        claim = workflow_tasks.claim(self.database, task_id, NOW)
        self.database.execute("INSERT INTO reply_worker_attempts VALUES (?,?,?,?,?,?,?,?)",
            (task_id, 1, NOW, None, None, None, "{}", BLOCKER))
        workflow_tasks.finish(self.database, task_id, claim["token"], NOW, {"state": "HELD"},
            [{"source": "reply_worker_attempts:" + task_id + ":1", "observed_at": NOW}],
            status="BLOCKED", blocker=BLOCKER, next_action="Investigate dependency")
        self.database.commit()
        return self.task(task_id)

    def task(self, task_id):
        return routing.one(self.database, "SELECT * FROM workflow_tasks WHERE task_id=?", (task_id,))

    def recover(self, config=None, now=NOW, limit=100):
        return routing._recover_reply_scope_reviews(self.database, config or self.config, now, limit)

    def test_routing_entry_recovers_once_with_original_failure_and_budget(self):
        task = self.blocked_task()
        task_count = self.database.execute("SELECT count(*) FROM workflow_tasks").fetchone()[0]
        audit = list(map(tuple, self.database.execute("SELECT * FROM reply_worker_attempts")))
        request, unused_message, unused_holds = reply_worker._source(self.database, task, self.config, NOW)
        self.assertEqual([], request["job_scope"])
        result = routing.cycle(self.database, self.config, NOW)
        self.assertEqual([task["task_id"]], result["recovered_reply_reviews"])
        current = self.task(task["task_id"])
        self.assertEqual("QUEUED", current["status"])
        self.assertIsNone(current["blocker"])
        for field in task.keys() - {"status", "blocker", "next_action", "updated_at"}:
            self.assertEqual(task[field], current[field], field)
        self.assertEqual(audit, list(map(tuple, self.database.execute("SELECT * FROM reply_worker_attempts"))))
        event = routing.one(self.database, "SELECT * FROM workflow_task_events WHERE event='POLICY_GATE_REMOVED'")
        detail = json.loads(event["detail"])
        self.assertEqual(BLOCKER, detail["previous_blocker"])
        self.assertEqual(1, detail["original_failed_attempt"])
        self.assertFalse(detail["can_send"])
        self.database.commit()
        restarted = sqlite3.connect(":memory:")
        restarted.row_factory = sqlite3.Row
        self.database.backup(restarted)
        self.database.close()
        self.database = restarted
        self.assertEqual([], routing.cycle(self.database, self.config, NOW)["recovered_reply_reviews"])
        self.assertEqual(task_count, self.database.execute("SELECT count(*) FROM workflow_tasks").fetchone()[0])
        self.database.commit()
        claim = workflow_tasks.claim(self.database, task["task_id"], NOW)
        self.assertEqual("CLAIMED", claim["status"])
        self.assertEqual(2, self.task(task["task_id"])["attempts"])

    def test_research_review_remains_unchanged(self):
        task = self.blocked_task()
        stored = routing._snapshot(self.database, "rw_results", "synthetic-research", "invalid", {}, NOW)
        review = routing._review(self.database, stored, "RESEARCH_RESULT_CONTENT_ID_MISMATCH", NOW)
        prior = self.task(review["review_task_id"])
        self.assertEqual([task["task_id"]], self.recover())
        self.assertEqual(prior, self.task(review["review_task_id"]))

    def test_expired_deadline_attempt_count_and_retry_limit_are_not_reset(self):
        task = self.blocked_task()
        # The expired-deadline case is gone: reply review has no deadline (owner, 2026-09-28).
        for updates in ({"attempts": 0}, {"attempts": 2},
                        {"attempts": 3}, {"max_attempts": 2},
                        {"blocker": "CLASSIFICATION_SOURCE_MISSING"}, {"status": "EXHAUSTED"}):
            with self.subTest(updates=updates):
                self.database.execute("SAVEPOINT fixture")
                for field, value in updates.items():
                    self.database.execute("UPDATE workflow_tasks SET " + field + "=? WHERE task_id=?",
                        (value, task["task_id"]))
                before = self.task(task["task_id"])
                self.assertEqual([], self.recover())
                self.assertEqual(before, self.task(task["task_id"]))
                self.database.execute("ROLLBACK TO fixture")
                self.database.execute("RELEASE fixture")

    def test_immutable_input_or_result_prevents_recovery(self):
        task = self.blocked_task()
        for table, values in (
                ("reply_worker_inputs", (task["task_id"], "input-hash", "synthetic-input", "source-hash", "{}")),
                ("reply_worker_results", (task["task_id"], reply_worker.VERSION, "input-hash", "output-hash", "{}"))):
            with self.subTest(table=table):
                self.database.execute("SAVEPOINT fixture")
                self.database.execute("INSERT INTO " + table + " VALUES (?,?,?,?,?)", values)
                self.assertEqual([], self.recover())
                self.assertEqual(task, self.task(task["task_id"]))
                self.database.execute("ROLLBACK TO fixture")
                self.database.execute("RELEASE fixture")

    def test_additional_attempt_audit_prevents_recovery(self):
        task = self.blocked_task()
        self.database.execute("INSERT INTO reply_worker_attempts VALUES (?,?,?,?,?,?,?,?)",
            (task["task_id"], 2, NOW, None, None, None, "{}", BLOCKER))
        self.assertEqual([], self.recover())
        self.assertEqual(task, self.task(task["task_id"]))

    def test_actual_authentication_and_current_source_pointer_are_rechecked(self):
        task = self.blocked_task()
        wrong_account = {**self.config, "pipeline_routing": {"account_ids": [456]}}
        self.assertEqual([], self.recover(wrong_account))
        self.assertEqual(task, self.task(task["task_id"]))
        self.database.execute("UPDATE pipeline_routing_sources SET result=? WHERE source_table='engagement_events'",
            (routing.encoded({"review_task_id": "different-current-task"}),))
        self.assertEqual([], self.recover())
        self.assertEqual(task, self.task(task["task_id"]))

    def test_source_mutation_prevents_recovery(self):
        task = self.blocked_task()
        self.database.execute("UPDATE engagement_events SET raw='{}'")
        self.assertEqual([], self.recover())
        self.assertEqual(task, self.task(task["task_id"]))

    def test_disabled_worker_and_repeated_policy_reset_are_excluded(self):
        task = self.blocked_task()
        self.assertEqual([], self.recover({**self.config, "reply_worker": {"enabled": False}}))
        self.assertEqual([task["task_id"]], self.recover())
        self.database.execute("UPDATE workflow_tasks SET status='BLOCKED',blocker=? WHERE task_id=?",
            (BLOCKER, task["task_id"]))
        self.assertEqual([], self.recover())
        self.assertEqual(1, self.database.execute(
            "SELECT count(*) FROM workflow_task_events WHERE event='POLICY_GATE_REMOVED'").fetchone()[0])

    def test_caller_rollback_restores_block_and_removes_recovery_event(self):
        task = self.blocked_task()
        self.database.execute("BEGIN")
        self.assertEqual([task["task_id"]], routing.cycle(
            self.database, self.config, NOW)["recovered_reply_reviews"])
        self.database.rollback()
        self.assertEqual(task, self.task(task["task_id"]))
        self.assertEqual(0, self.database.execute(
            "SELECT count(*) FROM workflow_task_events WHERE event='POLICY_GATE_REMOVED'").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
