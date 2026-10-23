import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
from unittest import mock
import unittest

import workflow_tasks
import test_reply_worker_review as fixtures
from backlog_fixture import setUpModule, tearDownModule  # noqa: F401 (fixture messages predate the reply cutoff)


class OneReplyTaskCannotStopTheOthers(unittest.TestCase):
    """The reply worker failed on every cycle from 2026-09-25T02 with IntegrityError.

    A bulk re-queue on 2026-09-23 at 08:00:09 reset 38 review tasks to attempts=0 while their
    attempt rows stayed. A re-run then reused attempt number 1, the INSERT collided, and because
    the error escaped the loop it stopped every reply in the batch, not just that task.
    """

    def setUp(self):
        self.case = fixtures.ReplyWorkerCycleReview("test_success_persists_audit_once_without_changing_source_or_business_tables")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.first = self.case.queue()
        self.second = self.case.queue(source_id="second-message")

    def attempts(self, task_id):
        return [row[0] for row in self.case.database.execute(
            "SELECT attempt FROM reply_worker_attempts WHERE task_id=? ORDER BY attempt", (task_id,))]

    def test_a_task_whose_count_was_reset_takes_the_next_free_number_and_history_survives(self):
        with self.case.database:
            for number in (1, 2, 3):
                self.case.database.execute("INSERT INTO reply_worker_attempts VALUES (?,?,?,?,?,?,?,?)",
                                           (self.first, number, fixtures.NOW, None, None, None, "{}", "HISTORICAL"))
            self.case.database.execute("UPDATE workflow_tasks SET attempts=0 WHERE task_id=?", (self.first,))
        result, unused = self.case.cycle()
        self.assertEqual("COMPLETE", self.case.task(self.first)["status"])
        self.assertEqual([1, 2, 3, 4], self.attempts(self.first))
        # The second message is from the same person. Once the first records their referral
        # (per person since 2026-09-25) the second's context has changed, so its decision is
        # re-read rather than applied. What matters here is that it ran despite the first.
        second = next(item for item in result["items"] if item["task_id"] == self.second)
        self.assertEqual("DECISION_READY", second["state"])
        self.assertEqual([1], self.attempts(self.second))
        self.assertEqual("COMPLETE", result["state"])

    def test_a_save_failure_is_that_tasks_alone(self):
        real = workflow_tasks.finish

        def finish(database, task_id, *arguments, **options):
            if task_id == self.first:
                raise RuntimeError("synthetic save failure")
            return real(database, task_id, *arguments, **options)

        with mock.patch.object(workflow_tasks, "finish", side_effect=finish):
            result, unused = self.case.cycle()
        failed = next(item for item in result["items"] if item["task_id"] == self.first)
        self.assertEqual("PERSISTENCE_FAILED", failed["state"])
        self.assertIn("finish", failed["raised_at"])
        self.assertEqual([], self.attempts(self.first))
        self.assertEqual("COMPLETE", self.case.task(self.second)["status"])


if __name__ == "__main__":
    unittest.main()
