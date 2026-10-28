"""BH: a task refunded by a usage-limit wait (max_attempts + 1) can still be re-enqueued idempotently."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import sqlite3
import unittest

import workflow_tasks
from test_be_provider_usage_limit import RESET, STDERR  # noqa: F401  (same fixtures as BE)
import provider_limits
import test_part_c_read_gating as part_c


class ReenqueueAfterUsageLimitTests(unittest.TestCase):
    setUp = part_c.PartCTests.setUp

    def test_reenqueue_matches_refunded_allowance(self):
        wait = provider_limits.check_output(1, STDERR, command=["codex"])
        workflow_tasks.initialize(self.database)
        args = ("outreach-draft-inspection", "a1", "parent", [{"source": "s", "observed_at": part_c.NOW}], part_c.at(86400))
        with self.database:
            task = workflow_tasks.enqueue(self.database, *args, part_c.NOW, max_attempts=3)
        claim = workflow_tasks.claim(self.database, task, part_c.at(10))
        with self.database:
            workflow_tasks.provider_wait(self.database, task, claim["token"], part_c.at(11), wait)
        with self.database:
            self.assertEqual(workflow_tasks.enqueue(self.database, *args, part_c.at(20), max_attempts=3), task)
            with self.assertRaises(ValueError):  # a genuinely different allowance is still refused
                workflow_tasks.enqueue(self.database, *args, part_c.at(21), max_attempts=2)


if __name__ == "__main__":
    unittest.main()
