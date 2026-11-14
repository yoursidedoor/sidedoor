"""Owner, 2026-11-13 ("E1: A"): an attended submit the user started from its card goes ahead even while
today's research is pending or running. Only that job; every other job yields exactly as before."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import attended_submit  # noqa: E402
import orchestrator  # noqa: E402
import test_applications_job as jobs  # noqa: E402


class WhileResearchHoldsTheTurn(unittest.TestCase):
    setUp = jobs.ApplicationsJobResearchTurnTests.setUp

    def ask(self, *keys):
        import applications
        attended_submit.initialize(self.database)
        with self.database:
            for index, key in enumerate(keys):                                 # asked for, and the card is watched now
                self.database.execute("INSERT INTO %s(job_key,state,requested_at,human_check,polled_at) VALUES (?,?,?,'{}',?)"
                                      % attended_submit.TABLE, (key, attended_submit.REQUESTED, applications.stamp(), applications.stamp()))

    def run_job(self, cycle_result=None):
        with patch.object(orchestrator, "research_holds_turn", return_value=True), \
                patch.object(orchestrator, "application_cycle", return_value=cycle_result or {"state": "COMPLETE"}) as cycle, \
                contextlib.redirect_stdout(io.StringIO()):
            return orchestrator.applications_service(self.root, {}, self.database), cycle

    def test_with_nothing_asked_for_the_job_yields_as_before(self):
        result, cycle = self.run_job()
        self.assertEqual(result["state"], "YIELDED_TO_DAILY_RESEARCH")
        cycle.assert_not_called()
        self.ask()                                                             # the table exists, no request
        result, cycle = self.run_job()
        self.assertEqual(result["state"], "YIELDED_TO_DAILY_RESEARCH")
        cycle.assert_not_called()

    def test_an_attended_submit_that_was_asked_for_runs_and_only_that_job(self):
        self.ask("job-a", "job-b")
        with self.database:                                                    # one that already started is not asked for
            self.database.execute("UPDATE %s SET state=? WHERE job_key='job-b'" % attended_submit.TABLE, (attended_submit.STARTED,))
        result, cycle = self.run_job()
        self.assertNotEqual(result.get("state"), "YIELDED_TO_DAILY_RESEARCH")
        cycle.assert_called_once()
        self.assertEqual(cycle.call_args.kwargs["only"], {"job-a"})
        self.assertIsNone(cycle.call_args.kwargs["should_stop"])               # its one job is not cut off
        self.assertEqual(result["attended_during_research"], ["job-a"])

    def test_the_card_no_longer_names_research_as_being_in_the_way(self):
        self.assertFalse(hasattr(attended_submit, "RESEARCH_WORDS"))
        import inspect
        self.assertNotIn("research_holds_turn(", inspect.getsource(attended_submit.in_the_way))


class ThePressStartsTheRoundAtOnce(unittest.TestCase):
    """Owner, 2026-11-13: "18 seconds wait to far too long". A tick is up to 30 s away; the press does not wait
    for one."""

    def loop(self, started, paused=False):
        import datetime as dt
        import tempfile
        from unittest import mock
        import scheduler_loop
        root = Path(tempfile.mkdtemp())
        loop = scheduler_loop.Loop(root, clock=lambda: dt.datetime(2026, 10, 22, 9, 40, tzinfo=dt.timezone.utc),
                                   spawner=lambda name: started.append(name) or mock.Mock(poll=lambda: 0),
                                   is_paused=lambda: paused, config={})
        scheduler_loop.install(loop)
        self.addCleanup(scheduler_loop.install, None)
        return loop, scheduler_loop

    def test_run_now_starts_the_job_in_the_call_itself_when_the_scheduler_is_running(self):
        started = []
        loop, scheduler_loop = self.loop(started)
        loop.tick()
        del started[:]
        with patch.object(loop, "running", return_value=True):
            self.assertTrue(scheduler_loop.run_now("applications"))
        self.assertEqual(started, ["applications"])                           # no tick was waited for

    def test_run_now_starts_nothing_when_the_scheduler_is_stopped_or_paused(self):
        started = []
        loop, scheduler_loop = self.loop(started)
        loop.tick()
        del started[:]
        self.assertFalse(scheduler_loop.run_now("applications"))              # not running: nothing started here
        self.assertEqual(started, [])
        started = []
        loop, scheduler_loop = self.loop(started, paused=True)
        with patch.object(loop, "running", return_value=True):
            scheduler_loop.run_now("applications")
        self.assertEqual(started, [])                                         # a pause starts nothing
        scheduler_loop.install(None)
        self.assertFalse(scheduler_loop.run_now("applications"))


if __name__ == "__main__":
    unittest.main()
