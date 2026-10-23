"""Compute meter settlement (owner, 2026-10-22: "compute budget still says 2 h 18 min of 5 h 50 min
used today").

Callers of `workflow_tasks.settle_browser_reservations`: `applications.settle_all_browser_reservations`
(start of `applications.cycle`, first/last in `orchestrator.applications_service`, and now its
research yield and the hourly round, `orchestrator.settle_idle_browser_reservations`) and the
per-job settle at the end of each job in `applications.cycle`. Settling earlier must never
refund a session still running, nor a queued request that may yet run.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import applications
import desktop_adapter
import orchestrator
import run_locks
import workflow_tasks

BOOKED = "2026-10-22T10:00:00+00:00"
LATER = "2026-10-22T12:00:00+00:00"   # well past the 30-minute cycle window


class SettleMeterTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "state").mkdir()
        (self.root / "reports").mkdir()
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        applications.initialize(self.database)
        workflow_tasks.initialize(self.database)
        desktop_adapter.initialize(self.database)
        self.database.commit()
        self.addCleanup(self.database.close)

    def book(self, reservation, job_key, seconds=1800):
        self.database.execute("INSERT INTO worker_compute_reservations VALUES (?,?,?,?)",
                              (reservation, "2026-10-22", seconds, BOOKED))
        workflow_tasks.track_browser_reservation(self.database, reservation, job_key, 1, seconds, BOOKED)

    def request(self, request_id, job_key, state, claims, expires_at, committed_at=None):
        self.database.execute(
            "INSERT INTO desktop_requests(request_id,binding,job_key,operation,payload,state,created_at,expires_at,claims)"
            " VALUES (?,?,?,?,?,?,?,?,?)", (request_id, "b", job_key, "inspect", "{}", state, "2026-10-22T10:01:00+00:00",
                                           expires_at, claims))
        if committed_at:
            self.database.execute("INSERT INTO desktop_events(request_id,at,event,detail) VALUES (?,?,?,?)",
                                  (request_id, committed_at, "RESULT_COMMITTED", "{}"))

    def credited(self, reservation):
        row = self.database.execute("SELECT seconds FROM worker_compute_refunds WHERE action_id=?", (reservation,)).fetchone()
        return row[0] if row else 0

    def test_hourly_round_settles_finished_bookings_but_never_a_running_or_queued_session(self):
        self.book("application-browser:done", "a:1")
        self.request("done-1", "a:1", "CONSUMED", 1, "2026-10-22T10:16:00+00:00", "2026-10-22T10:05:00+00:00")
        self.book("application-browser:running", "b:1")
        self.request("running-1", "b:1", "CLAIMED", 1, "2026-10-22T10:16:00+00:00")       # claimed, no commit
        self.book("application-browser:queued", "c:1")
        self.request("queued-1", "c:1", "PENDING", 0, "2026-10-22T13:00:00+00:00")        # unexpired, may yet run
        self.database.commit()
        with patch.object(applications, "stamp", return_value=LATER):
            orchestrator.settle_idle_browser_reservations(self.root, self.database)
        self.assertEqual(self.credited("application-browser:done"), 1800 - 300)
        self.assertEqual(self.credited("application-browser:running"), 0)
        self.assertEqual(self.credited("application-browser:queued"), 0)
        unsettled = {row[0] for row in self.database.execute(
            "SELECT reservation FROM application_browser_reservations WHERE settled_at IS NULL")}
        self.assertEqual(unsettled, {"application-browser:running", "application-browser:queued"})

    def test_hourly_round_leaves_a_running_applications_job_alone(self):
        self.book("application-browser:done", "a:1")
        self.request("done-1", "a:1", "CONSUMED", 1, "2026-10-22T10:16:00+00:00", "2026-10-22T10:05:00+00:00")
        self.database.commit()
        with run_locks.held(self.root, run_locks.APPLICATIONS) as held:
            self.assertTrue(held)
            self.assertIsNone(orchestrator.settle_idle_browser_reservations(self.root, self.database))
        self.assertEqual(self.credited("application-browser:done"), 0)

    def test_a_yielded_applications_job_still_gets_settled(self):
        self.book("application-browser:done", "a:1")
        self.request("done-1", "a:1", "CONSUMED", 1, "2026-10-22T10:16:00+00:00", "2026-10-22T10:05:00+00:00")
        self.database.commit()
        with patch.object(orchestrator, "research_holds_turn", return_value=True), \
                patch.object(applications, "stamp", return_value=LATER):
            result = orchestrator.applications_service(self.root, {}, self.database)
        self.assertEqual(result["state"], "YIELDED_TO_DAILY_RESEARCH")
        self.assertEqual(self.credited("application-browser:done"), 1500)
        # And the hourly round itself settles, even while research holds the turn.
        self.book("application-browser:later", "d:1")
        self.request("later-1", "d:1", "CONSUMED", 1, "2026-10-22T10:16:00+00:00", "2026-10-22T10:10:00+00:00")
        self.database.commit()
        with patch.object(orchestrator, "research_turn_pending", return_value=True), \
                patch.object(applications, "stamp", return_value=LATER):
            self.assertEqual(orchestrator.run(self.root, {}, self.database, None), 0)
        self.assertEqual(self.credited("application-browser:later"), 1800 - 600)


class FinderFailureRefundTests(unittest.TestCase):
    def test_a_failed_finder_run_gets_its_unused_tail_back(self):
        import linkedin_contacts as lc
        import test_linkedin_contacts as finder
        database = finder.fixture()
        lc.initialize(database)
        database.commit()
        item = lc.schedule(database, finder.CONFIG, finder.NOW)["due"][0]
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)

        def failing(*unused):
            raise RuntimeError("session failed fast")

        summary = lc._run_one(Path(directory.name), database, finder.CONFIG, item, finder.NOW, executor=failing)
        self.assertEqual(summary["state"], "FAILED")
        row = database.execute("SELECT seconds,reserved_seconds FROM worker_compute_refunds WHERE action_id=?",
                               ("linkedin-contacts:" + summary["run_id"],)).fetchone()
        self.assertIsNotNone(row, "a run that started and failed in seconds keeps its 900 s booking")
        self.assertGreater(row[0], lc.COMPUTE_SECONDS - 60)


if __name__ == "__main__":
    unittest.main()
