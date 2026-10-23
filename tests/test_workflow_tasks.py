import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
import sqlite3
import tempfile
import unittest
from pathlib import Path

import workflow_tasks


NOW = "2026-09-17T16:00:00+00:00"
DEADLINE = "2026-09-18T16:00:00+00:00"
EVIDENCE = [{"source": "mock-observation", "observed_at": NOW}]


class WorkflowTaskTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "tasks.sqlite3"
        self.database = sqlite3.connect(self.path)
        self.database.row_factory = sqlite3.Row
        self.addCleanup(self.database.close)
        workflow_tasks.initialize(self.database)

    def enqueue(self, action="one", effect="READ"):
        with self.database:
            return workflow_tasks.enqueue(self.database, "worker", action, "parent", EVIDENCE, DEADLINE, NOW, effect=effect)

    def test_identity_and_payload_are_immutable(self):
        task_id = self.enqueue()
        self.assertEqual(self.enqueue(), task_id)
        with self.assertRaises(ValueError):
            with self.database:
                workflow_tasks.enqueue(self.database, "worker", "one", "parent", [], DEADLINE, NOW)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM workflow_tasks").fetchone()[0], 1)

    def test_restart_and_two_connections_cannot_double_claim(self):
        task_id = self.enqueue()
        first = workflow_tasks.claim(self.database, task_id, NOW)
        with sqlite3.connect(self.path) as restarted:
            restarted.row_factory = sqlite3.Row
            self.assertEqual(workflow_tasks.claim(restarted, task_id, NOW)["status"], "RUNNING")
        with self.database:
            workflow_tasks.finish(self.database, task_id, first["token"], NOW, {"saved": True}, EVIDENCE)
        self.assertEqual(workflow_tasks.claim(self.database, task_id, NOW)["status"], "COMPLETE")

    def test_expired_write_claim_is_uncertain_never_reissued(self):
        task_id = self.enqueue(effect="WRITE")
        first = workflow_tasks.claim(self.database, task_id, NOW, lease_seconds=1)
        later = "2026-09-17T16:00:06+00:00"
        self.assertEqual(workflow_tasks.claim(self.database, task_id, later)["status"], "UNCERTAIN")
        with self.assertRaises(ValueError):
            workflow_tasks.finish(self.database, task_id, first["token"], later, {}, EVIDENCE)
        self.assertEqual(self.database.execute("SELECT attempts FROM workflow_tasks").fetchone()[0], 1)

    def test_read_retries_are_bounded_and_backed_off(self):
        task_id = self.enqueue()
        now = dt.datetime.fromisoformat(NOW)
        for attempt in range(3):
            claim = workflow_tasks.claim(self.database, task_id, now.isoformat())
            self.assertEqual(claim["status"], "CLAIMED")
            with self.database:
                state = workflow_tasks.finish(self.database, task_id, claim["token"], now.isoformat(), {}, [],
                                              status="RETRY_WAIT", blocker="OFFLINE", next_action="Retry read")
            if attempt < 2:
                self.assertEqual(workflow_tasks.claim(self.database, task_id, now.isoformat())["status"], "RETRY_WAIT")
                now += dt.timedelta(seconds=[900, 3600][attempt])
            else:
                self.assertEqual(state, "EXHAUSTED")
                self.assertEqual(workflow_tasks.claim(self.database, task_id, now.isoformat())["status"], "EXHAUSTED")

    def test_child_handoff_is_atomic_with_parent_completion(self):
        parent = self.enqueue()
        claim = workflow_tasks.claim(self.database, parent, NOW)
        child = {"worker": "next", "action_id": "child", "inputs": EVIDENCE, "deadline": DEADLINE}
        with self.assertRaises(ValueError):
            workflow_tasks.handoff(self.database, parent, "stale", NOW, [child], {}, EVIDENCE)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM workflow_tasks").fetchone()[0], 1)
        with self.database:
            children = workflow_tasks.handoff(self.database, parent, claim["token"], NOW, [child], {}, EVIDENCE)
        self.assertEqual(self.database.execute("SELECT parent_run_id FROM workflow_tasks WHERE task_id=?", (children[0],)).fetchone()[0], parent)

    def test_compute_budget_is_global_and_reserved_before_execution(self):
        self.assertTrue(workflow_tasks.reserve_compute(self.database, "first", NOW, 6000))
        self.assertTrue(workflow_tasks.reserve_compute(self.database, "first", NOW, 6000))
        self.assertFalse(workflow_tasks.reserve_compute(self.database, "second", NOW, 6000))
        self.assertTrue(workflow_tasks.reserve_compute(self.database, "third", NOW, 4800))
        self.assertFalse(workflow_tasks.reserve_compute(self.database, "fourth", NOW, 1))
        self.assertTrue(workflow_tasks.reserve_compute(self.database, "next-day", DEADLINE, 6000))


if __name__ == "__main__":
    unittest.main()
