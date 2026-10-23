"""Applications run as their own job (owner, 2026-10-02).

"1. Run applications as their own job, like research, with its own schedule and lock. Outreach,
replies and the inbox would then run every hour regardless. The contact finder and applications
both use the same Chrome browser, so the two would need to take turns on it." - "I like 1."

The hourly cycle no longer runs application work (tests/test_service_isolation.py); these cover
the job's lock, its yield to daily research, and the one-browser-session-at-a-time lock.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import application_connection
import applications
import linkedin_contacts
import orchestrator
import run_locks
import test_part_c_read_gating as part_c


class ApplicationsJobLockTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "state").mkdir()
        (self.root / "config.json").write_text(json.dumps({"timezone": "America/Los_Angeles", "research_enabled": True}))

    def main(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"SIDEDOOR_HOME": str(self.root)}), patch.object(sys, "argv", ["orchestrator.py", "applications"]), \
                patch.object(orchestrator, "applications_service", side_effect=AssertionError("must not run")) as job, \
                patch.object(orchestrator, "single_instance", side_effect=AssertionError("never the runner lock")), \
                contextlib.redirect_stdout(output):
            code = orchestrator.main()
        return code, output.getvalue(), job

    def test_a_run_still_going_makes_the_next_one_skip(self):
        with run_locks.held(self.root, run_locks.APPLICATIONS) as held:
            self.assertTrue(held)
            code, output, job = self.main()
        self.assertEqual(code, 0)
        self.assertIn("Another applications run is active; skipped.", output)
        job.assert_not_called()
        # No record: Overview's Applications "Last run" never shows a skip (overview_page.applications_last).
        self.assertFalse((self.root / "reports" / "applications-service.json").exists())

    def test_the_runner_lock_does_not_stop_the_applications_job(self):
        """The hourly cycle holding state/runner.lock no longer stops applications, and the
        applications lock does not stop the hourly cycle: the two locks are independent."""
        with run_locks.held(self.root, run_locks.RUNNER) as runner:
            self.assertTrue(runner)
            with run_locks.held(self.root, run_locks.APPLICATIONS) as applications_lock:
                self.assertTrue(applications_lock)
        # An install holds both, all or none.
        with run_locks.held(self.root, run_locks.APPLICATIONS):
            with run_locks.held(self.root, run_locks.RUNNER, run_locks.APPLICATIONS) as both:
                self.assertFalse(both)
            with run_locks.held(self.root, run_locks.RUNNER) as runner:
                self.assertTrue(runner, "a refused all-or-none take must not keep the runner lock")


class ApplicationsJobResearchTurnTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for folder in ("state", "reports"):
            (self.root / folder).mkdir()
        self.database = orchestrator.connect(self.root)
        self.addCleanup(self.database.close)

    def test_yields_while_daily_research_has_its_turn(self):
        with patch.object(orchestrator, "research_holds_turn", return_value=True), \
                patch.object(orchestrator, "application_cycle") as cycle, \
                patch.object(orchestrator.applications, "settle_all_browser_reservations") as settle, \
                contextlib.redirect_stdout(io.StringIO()):
            result = orchestrator.applications_service(self.root, {}, self.database)
        self.assertEqual(result["state"], "YIELDED_TO_DAILY_RESEARCH")
        cycle.assert_not_called()
        # It still settles what earlier runs finished (owner, 2026-10-22), and reserves nothing.
        settle.assert_called_once()

    def test_runs_the_application_cycle_once_research_has_had_its_turn(self):
        with patch.object(orchestrator, "research_holds_turn", return_value=False), \
                patch.object(orchestrator, "application_cycle", return_value={"state": "COMPLETE"}) as cycle:
            result = orchestrator.applications_service(self.root, {}, self.database)
        self.assertEqual(result["state"], "COMPLETE")
        cycle.assert_called_once()
        self.assertEqual(self.database.execute(
            "SELECT status FROM workflow_tasks WHERE worker='applications' AND parent_run_id LIKE 'applications-job:%'").fetchone()[0],
            "COMPLETE")
        self.assertEqual(orchestrator.reporting.applications_job(self.database)["runs"][0]["status"], "COMPLETE")

    def test_research_running_holds_the_turn(self):
        config = {"timezone": "America/Los_Angeles", "research_enabled": True, "research_hour": 9, "day_start_hour": 9}
        orchestrator.daily_schedule.initialize(self.database, config["timezone"])
        day = orchestrator.daily_schedule.due_day(orchestrator.dt.datetime.now(orchestrator.dt.timezone.utc), config)
        with self.database:
            self.database.execute("INSERT INTO research_days(day,state,attempts) VALUES (?, 'RUNNING', 1)", (day,))
        self.assertTrue(orchestrator.research_holds_turn(config, self.database))
        with self.database:
            self.database.execute("UPDATE research_days SET state='COMPLETE' WHERE day=?", (day,))
        self.assertFalse(orchestrator.research_holds_turn(config, self.database))


class TwoWritersTests(unittest.TestCase):
    def test_a_savepoint_that_reads_then_writes_survives_the_other_process_committing(self):
        """A deferred SAVEPOINT that read before the other writer committed could not write at
        all ("database is locked" at once, no busy wait); seen in the offline
        two-process rehearsal. orchestrator.connect's outermost SAVEPOINT takes the lock first."""
        directory = self.enterContext(tempfile.TemporaryDirectory())  # a cleanup: runs after the connections the test closes (Windows)
        root = Path(directory)
        (root / "state").mkdir()
        first, second = orchestrator.connect(root), orchestrator.connect(root)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        with first:
            first.execute("CREATE TABLE sample(value)")
        first.execute("SAVEPOINT outer_change")
        first.execute("SELECT COUNT(*) FROM sample").fetchone()
        first.execute("SAVEPOINT inner_change")
        first.execute("INSERT INTO sample VALUES ('first')")
        first.execute("RELEASE inner_change")
        self.assertTrue(first.in_transaction, "an inner release must not commit")
        first.execute("RELEASE SAVEPOINT outer_change")
        self.assertFalse(first.in_transaction, "the outermost release commits, as before")
        first.execute("SAVEPOINT later_change")
        first.execute("SELECT COUNT(*) FROM sample").fetchone()
        # The other process writes meanwhile. Before, it committed and `first` then could not
        # write at all; now `first` already holds the write lock and the other one waits.
        other = sqlite3.connect(root / "state/orchestrator.sqlite3", timeout=0.2)
        self.addCleanup(other.close)
        with self.assertRaises(sqlite3.OperationalError):
            with other:
                other.execute("INSERT INTO sample VALUES ('other')")
        first.execute("INSERT INTO sample VALUES ('third')")
        first.execute("RELEASE later_change")
        with second:
            second.execute("INSERT INTO sample VALUES ('second')")
        self.assertEqual(second.execute("SELECT COUNT(*) FROM sample").fetchone()[0], 3)


class BrowserTurnTests(unittest.TestCase):
    setUp = part_c.PartCTests.setUp
    job = part_c.PartCTests.job

    def adapter(self, consumer):
        config = dict(part_c.RECOVERY, application_browser={"enabled": True, "timeout_seconds": 60},
                      timezone="America/Los_Angeles")
        return application_connection.HostedDesktopAdapter(self.database, self.root, config,
                                                           clock=lambda: part_c.NOW, consumer=consumer)

    def test_an_application_session_holds_the_browser_for_its_duration(self):
        seen = []

        class Stop(Exception):
            pass

        def consumer(root, database, config, request_id):
            with run_locks.held(self.root, run_locks.BROWSER) as other:
                seen.append(other)
            raise Stop()

        with self.assertRaises(Stop):
            self.adapter(consumer).call("inspect", {"job": self.job(), "read_only": True})
        self.assertEqual(seen, [False], "another session could take Chrome mid-session")
        with run_locks.held(self.root, run_locks.BROWSER) as after:
            self.assertTrue(after, "the browser lock outlived the session")

    def test_a_busy_browser_is_a_capacity_wait_that_spends_nothing(self):
        adapter = self.adapter(lambda *args: self.fail("no session may start"))
        adapter.browser_wait_seconds = 0
        with run_locks.held(self.root, run_locks.BROWSER):
            with self.assertRaises(application_connection.CapacityWait) as raised:
                adapter.call("inspect", {"job": self.job(), "read_only": True})
        self.assertEqual(raised.exception.code, application_connection.BROWSER_BUSY_CODE)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM desktop_requests").fetchone()[0], 0)
        self.assertEqual(self.database.execute(
            "SELECT COUNT(*) FROM application_events WHERE event='BROWSER_CAPACITY_RESERVED'").fetchone()[0], 0)

    def test_the_contact_finder_defers_without_spending_a_try(self):
        executor = []
        item = {"company": "Gate Systems", "company_key": "gate systems", "slots": 3}
        with run_locks.held(self.root, run_locks.BROWSER):
            # database=None: a deferred read touches no record at all (no run row, no compute).
            summary = linkedin_contacts.run_one(self.root, None, {}, item, None, executor=executor.append)
        self.assertEqual(summary, {"company": "Gate Systems", "state": linkedin_contacts.BROWSER_BUSY})
        self.assertEqual(executor, [])


if __name__ == "__main__":
    unittest.main()
