import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import unittest

import pipeline_routing as routing
import reply_worker as worker
import test_reply_worker_review as fixtures
import workflow_tasks
from backlog_fixture import setUpModule, tearDownModule  # noqa: F401 (fixture messages predate the reply cutoff)


class ReplyWorkerCapacityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ReplyWorkerCycleReview(methodName="runTest")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.database = self.fixture.database
        self.database.execute("DELETE FROM rw_results")
        self.database.commit()

    def test_full_utc_day_defers_without_claim_until_day_start_and_then_completes(self):
        # The budget day starts at day_start_hour (9:00 since 2026-09-30) in the configured zone,
        # here UTC; its label is the date on which it ends (workflow_tasks.budget_day).
        task_id = self.fixture.queue()
        original = self.fixture.task(task_id)
        self.assertTrue(workflow_tasks.reserve_compute(self.database, "full-day", fixtures.NOW, 10800, "UTC"))
        self.database.commit()
        result, execute = self.fixture.cycle()
        execute.assert_not_called()
        deferred = self.fixture.task(task_id)
        self.assertEqual("RETRY_WAIT", deferred["status"])
        self.assertEqual("DAILY_COMPUTE_BUDGET_EXHAUSTED", deferred["blocker"])
        self.assertEqual("2026-09-18T09:00:00+00:00", deferred["next_at"])
        for field in ("attempts", "deadline", "max_attempts"):
            self.assertEqual(original[field], deferred[field])
        for table in ("reply_worker_inputs", "reply_worker_attempts", "reply_worker_results"):
            self.assertEqual(0, self.database.execute("SELECT COUNT(*) FROM " + table).fetchone()[0])
        self.assertEqual(0, self.database.execute("SELECT COUNT(*) FROM workflow_task_events WHERE event='CLAIMED'").fetchone()[0])
        self.assertEqual(1, self.database.execute("SELECT COUNT(*) FROM workflow_task_events WHERE event='CAPACITY_DEFERRED'").fetchone()[0])
        result, execute = self.fixture.cycle("2026-09-17T23:59:59+00:00")
        execute.assert_not_called()
        self.assertEqual([], result["items"])
        result, execute = self.fixture.cycle(deferred["next_at"])
        execute.assert_called_once()
        self.assertEqual("COMPLETE", self.fixture.task(task_id)["status"])
        self.assertEqual(1, self.fixture.task(task_id)["attempts"])
        self.assertEqual(original["deadline"], self.fixture.task(task_id)["deadline"])
        self.assertEqual(10800, self.database.execute("SELECT SUM(seconds) FROM worker_compute_reservations WHERE day='2026-09-18'").fetchone()[0])
        self.assertEqual(1, self.database.execute("SELECT SUM(seconds) FROM worker_compute_reservations WHERE day='2026-09-19'").fetchone()[0])

    def test_los_angeles_reset_tracks_both_daylight_saving_transitions(self):
        self.fixture.config["timezone"] = "America/Los_Angeles"
        # 9:00 Pacific service-day start (2026-09-30; was midnight).
        for index, (now, reset) in enumerate((("2026-03-08T08:30:00+00:00", "2026-03-08T16:00:00+00:00"),
                                             ("2026-11-01T07:30:00+00:00", "2026-11-01T17:00:00+00:00"))):
            with self.subTest(now=now):
                task_id = self.fixture.queue(source_id="dst-" + str(index))
                self.database.execute("UPDATE workflow_tasks SET deadline='2026-11-03T00:00:00+00:00' WHERE task_id=?", (task_id,))
                self.assertTrue(workflow_tasks.reserve_compute(self.database, "dst-day-" + str(index), now,
                                                               10800, "America/Los_Angeles"))
                self.database.commit()
                result = worker._defer_capacity(self.database, task_id, self.fixture.config, now, 1, 10800)
                self.assertEqual("RETRY_WAIT", result["state"])
                self.assertEqual(reset, self.fixture.task(task_id)["next_at"])
                self.assertEqual(0, self.fixture.task(task_id)["attempts"])

    def test_preallocated_current_attempt_reuses_capacity_and_preserves_prior_failure(self):
        task_id = self.fixture.queue()
        result, execute = self.fixture.cycle(effect=worker.Held("CLASSIFICATION_TIMEOUT", retry=True))
        execute.assert_called_once()
        failed = routing.one(self.database, "SELECT * FROM reply_worker_attempts WHERE task_id=?", (task_id,))
        self.assertEqual("CLASSIFICATION_TIMEOUT", failed["blocker"])
        original = self.fixture.task(task_id)
        due = original["next_at"]
        action_id = "reply:" + task_id + ":2"
        self.assertTrue(workflow_tasks.reserve_compute(self.database, action_id, due, 1, "UTC"))
        self.assertTrue(workflow_tasks.reserve_compute(self.database, "other-workers", due, 10798, "UTC"))
        self.database.commit()
        reservations = routing.rows(self.database, "SELECT * FROM worker_compute_reservations ORDER BY action_id")
        result, execute = self.fixture.cycle(due)
        execute.assert_called_once()
        self.assertEqual("COMPLETE", self.fixture.task(task_id)["status"])
        self.assertEqual(2, self.fixture.task(task_id)["attempts"])
        for field in ("deadline", "max_attempts"):
            self.assertEqual(original[field], self.fixture.task(task_id)[field])
        self.assertEqual(failed, routing.one(self.database, "SELECT * FROM reply_worker_attempts WHERE task_id=? AND attempt=1", (task_id,)))
        self.assertEqual(reservations, routing.rows(self.database, "SELECT * FROM worker_compute_reservations ORDER BY action_id"))
        self.assertEqual(10800, self.database.execute("SELECT SUM(seconds) FROM worker_compute_reservations").fetchone()[0])
        self.assertEqual(2, self.database.execute("SELECT COUNT(*) FROM reply_worker_attempts").fetchone()[0])
        result, execute = self.fixture.cycle(due)
        execute.assert_not_called()
        self.assertEqual([], result["items"])
        self.assertEqual(reservations, routing.rows(self.database, "SELECT * FROM worker_compute_reservations ORDER BY action_id"))


if __name__ == "__main__":
    unittest.main()
