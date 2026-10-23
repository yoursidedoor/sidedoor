"""The hourly `run` cycle yields the day-opening tick to the once-daily research.

Two launchd jobs run orchestrator.py against one `state/runner.lock`:

    com.sidedoor.orchestrator   StartCalendarInterval Minute => 0
    com.sidedoor.research       StartInterval 300

Both fire at 00:00:00, the instant the compute day opens. `research_service_tick` orders
research before any service cycle, and tests/test_research_first.py pins that. But the
hourly job does not call `research_service_tick` - it calls `run`, which records
`{"state": "SEPARATE_DAILY_SERVICE"}` and goes straight to spending. On 2026-09-19 it won
the lock at 00:00:00 and the research service skipped every tick behind it.

That matters because `workflow_tasks.reserve_compute` admits research against the WHOLE
day's spend, not a research-only bucket: services drawing first consume research's
ceiling directly. These tests pin the yield, and - just as importantly - pin that it is
bounded, so a deferral can never become a stall.

Offline: temp roots, no project database, no service, no network.
"""

import datetime as dt
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import applications
import daily_schedule
import orchestrator
import research_workflow
import workflow_tasks
import profile_home


ROOT = Path(__file__).resolve().parents[1]
PACIFIC = ZoneInfo("America/Los_Angeles")
CONFIG = json.loads((ROOT / "config.json").read_text())
# The day opens at research_hour (9:00 Pacific since 2026-09-30; was midnight).
DAY_OPEN = dt.datetime(2026, 9, 19, CONFIG["research_hour"], 0, 0, tzinfo=PACIFIC)
OPEN_SLOT = DAY_OPEN.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H")


def pinned(instant):
    class Pinned(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    return SimpleNamespace(datetime=Pinned, timedelta=dt.timedelta, timezone=dt.timezone)


class HourlyRunFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for directory in ("state", "reports", "logs"):
            (self.root / directory).mkdir()
        self.database = orchestrator.connect(self.root)
        self.addCleanup(self.database.close)
        research_workflow.initialize(self.database)
        workflow_tasks.initialize(self.database)
        applications.initialize(self.database)
        daily_schedule.initialize(self.database, CONFIG["timezone"])
        self.config = dict(CONFIG, scope="job_search_e2e")
        self.pipeline = {"companies": [], "rules": {"max_unique_contacts_per_company": 3}}
        self.steps = []
        for target in ("socket.socket.connect", "socket.create_connection", "subprocess.Popen",
                       "orchestrator.HeyReach"):
            guard = patch(target, side_effect=AssertionError("External calls prohibited in these tests"))
            guard.start()
            self.addCleanup(guard.stop)

    def execute(self, instant=DAY_OPEN, **kwargs):
        """Run one hourly cycle with every spending step replaced by a recorder."""
        def worker_step(database, slot, name, action):
            self.steps.append(name)
            return {"state": "COMPLETE"}

        def service_cycle(root, config, database):
            self.steps.append("service_cycle")
            return {"applications": {"state": "OK"}}

        def observe(*args, **columns):
            self.steps.append("observe")
            return {"state": "OBSERVED"}

        with patch.object(orchestrator, "dt", pinned(instant)), \
                patch.object(orchestrator, "worker_step", worker_step), \
                patch.object(orchestrator, "service_cycle", service_cycle), \
                patch.object(orchestrator, "observe", observe), \
                patch.object(orchestrator, "log_retention", SimpleNamespace(print_result=lambda *args: None)):
            return orchestrator.run(self.root, self.config, self.database, self.pipeline, **kwargs)

    def reservations(self):
        return self.database.execute("SELECT COUNT(*) FROM worker_compute_reservations").fetchone()[0]

    def runs(self):
        return [tuple(row) for row in self.database.execute("SELECT slot,state FROM runs")]

    def record_day(self, state, day="2026-09-19"):
        with self.database:
            self.database.execute(
                "INSERT INTO research_days(" + daily_schedule.DAY_COLUMNS + ") VALUES (?,?,1,'attempt',NULL,NULL,0)",
                (day, state))


class HourlyRunYieldsToDailyResearch(HourlyRunFixture):
    def test_the_day_opening_hour_yields_and_spends_nothing(self):
        """00:00:00 with no research row: the cycle returns without touching the budget."""
        self.assertEqual(self.execute(), 0)
        self.assertEqual(self.steps, [], "not one spending step ran")
        self.assertEqual(self.reservations(), 0, "not one second was reserved")
        self.assertEqual(self.runs(), [], "no hourly slot was consumed by the yield")

    def test_the_yield_lasts_exactly_one_tick(self):
        """The bound: once research has ANY recorded outcome, the hour runs normally."""
        for state in ("RUNNING", "COMPLETE", "RETRY_WAIT", "EXHAUSTED"):
            with self.subTest(state=state):
                self.database.execute("DELETE FROM research_days")
                self.database.execute("DELETE FROM runs")
                self.database.commit()
                self.steps.clear()
                self.record_day(state)
                self.assertEqual(self.execute(), 0)
                self.assertIn("service_cycle", self.steps,
                              "a recorded research outcome must not keep deferring the hour")
                self.assertEqual([slot for slot, _ in self.runs()], [OPEN_SLOT])

    def test_a_day_with_no_research_service_never_stalls(self):
        """If nothing will ever write the row, yielding would be a permanent stall."""
        for config in (dict(self.config, research_enabled=False),
                       dict(self.config, scope="monitor_existing_pipeline")):
            with self.subTest(scope=config["scope"], enabled=config["research_enabled"]):
                self.database.execute("DELETE FROM research_days")
                self.database.execute("DELETE FROM runs")
                self.database.commit()
                self.steps.clear()
                self.config = config
                self.assertEqual(self.execute(), 0)
                self.assertIn("service_cycle", self.steps, "this cycle can only ever run itself")

    def test_a_local_check_is_never_deferred(self):
        """`check` spends no compute at all, so it has nothing to yield."""
        self.assertEqual(self.execute(local_only=True), 0)
        self.assertEqual([slot for slot, _ in self.runs()], [OPEN_SLOT + ":local-check"])

    def test_a_later_hour_of_the_same_day_still_yields_until_research_has_run(self):
        """The window is 'research has had no turn', not 'it is midnight'."""
        self.assertEqual(self.execute(dt.datetime(2026, 9, 19, 3, 0, tzinfo=PACIFIC)), 0)
        self.assertEqual(self.steps, [])
        self.assertEqual(self.reservations(), 0)

    def test_yesterdays_exhausted_day_does_not_satisfy_todays_turn(self):
        """A terminal row for the PREVIOUS day is not today's research having run."""
        self.record_day("EXHAUSTED", day="2026-09-18")
        self.assertEqual(self.execute(), 0)
        self.assertEqual(self.steps, [], "the 09-18 row must not release the 09-19 boundary")

    def test_the_yield_uses_the_research_day_not_the_utc_day(self):
        """23:59 Pacific is still the old research day, whose row already exists."""
        self.record_day("COMPLETE", day="2026-09-19")
        self.assertEqual(self.execute(dt.datetime(2026, 9, 19, 23, 59, tzinfo=PACIFIC)), 0)
        self.assertIn("service_cycle", self.steps)


if __name__ == "__main__":
    unittest.main()


class EveryUncertaintyResolvesTowardRunning(HourlyRunFixture):
    """Deferral is the only action here that can stall, so it needs positive grounds."""

    def test_an_incomplete_configuration_runs_rather_than_waits(self):
        for config in ({}, {"research_enabled": True}, {"research_enabled": True, "timezone": "America/Los_Angeles"},
                       {"research_enabled": True, "research_hour": 0}):
            with self.subTest(config=sorted(config)):
                self.database.execute("DELETE FROM research_days")
                self.database.execute("DELETE FROM runs")
                self.database.commit()
                self.assertIs(orchestrator.research_turn_pending(config, self.database), False,
                              "a configuration that does not describe a daily research cannot be waited on")

    def test_the_live_configuration_does_describe_one(self):
        """The negative above is only meaningful if the real config is positive."""
        self.assertIs(orchestrator.research_turn_pending(self.config, self.database), True)


class PendingOwnerRetryIsAlsoResearchsTurn(HourlyRunFixture):
    def test_an_unconsumed_retry_yields_and_a_consumed_one_does_not(self):
        """A reopened day comes with a restored allowance; services must not take it first."""
        self.record_day("EXHAUSTED")
        for state, yields in (("AUTHORIZED", True), ("CONSUMED", False)):
            with self.subTest(state=state):
                with self.database:
                    self.database.execute("INSERT INTO schedule_meta(key,value) VALUES (?,?) ON CONFLICT(key) "
                                          "DO UPDATE SET value=excluded.value",
                                          ("forced_research_retry:2026-09-19", json.dumps({"state": state})))
                self.database.execute("DELETE FROM runs"); self.database.commit()
                self.steps.clear()
                self.assertEqual(self.execute(), 0)
                self.assertEqual(self.steps == [], yields, self.steps)
