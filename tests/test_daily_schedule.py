import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import concurrent.futures
import datetime as dt
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

import daily_schedule
import orchestrator


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "state").mkdir()
        self.database = orchestrator.connect(self.root)
        self.addCleanup(self.database.close)
        self.config = {"timezone": "America/Los_Angeles", "research_hour": 8,
                       "research_max_attempts": 3, "research_retry_delays_seconds": [900,3600],
                       "research_dispatch_interval_seconds": 300}
        daily_schedule.initialize(self.database, self.config["timezone"])

    def at(self, value):
        return dt.datetime.fromisoformat(value).replace(tzinfo=ZoneInfo("America/Los_Angeles"))

    def test_before_and_at_eight(self):
        self.assertEqual(daily_schedule.due_day(self.at("2026-09-17T07:59:59"), self.config), "2026-09-16")
        self.assertEqual(daily_schedule.due_day(self.at("2026-09-17T08:00:00"), self.config), "2026-09-17")

    def test_latest_day_only_after_multiday_sleep(self):
        claim = daily_schedule.claim(self.database, self.config, self.at("2026-09-21T12:00:00"))
        self.assertEqual(claim["day"], "2026-09-21")
        daily_schedule.finish(self.database, self.config, claim["id"], {"state": "COMPLETE"}, self.at("2026-09-21T12:05:00"))
        self.assertEqual(daily_schedule.claim(self.database, self.config, self.at("2026-09-21T13:00:00"))["state"], "COMPLETE")
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM research").fetchone()[0], 1)

    def test_failed_run_retries_with_backoff_and_bounded_attempts(self):
        now = self.at("2026-09-17T08:00:00")
        first = daily_schedule.claim(self.database, self.config, now)
        state = daily_schedule.finish(self.database, self.config, first["id"], {"state": "FAILED"}, now)
        self.assertEqual(state["state"], "RETRY_WAIT")
        self.assertIsNone(self.database.execute("SELECT completed_at FROM research_days").fetchone()[0])
        self.assertEqual(daily_schedule.claim(self.database, self.config, now + dt.timedelta(minutes=14))["state"], "RETRY_WAIT")
        second = daily_schedule.claim(self.database, self.config, now + dt.timedelta(minutes=15))
        daily_schedule.finish(self.database, self.config, second["id"], {"state": "FAILED"}, now + dt.timedelta(minutes=15))
        self.assertEqual(daily_schedule.claim(self.database, self.config, now + dt.timedelta(minutes=74))["state"], "RETRY_WAIT")
        third = daily_schedule.claim(self.database, self.config, now + dt.timedelta(minutes=75))
        state = daily_schedule.finish(self.database, self.config, third["id"], {"state": "FAILED"}, now + dt.timedelta(minutes=75))
        self.assertEqual(state["state"], "EXHAUSTED")
        self.assertEqual(daily_schedule.claim(self.database, self.config, now + dt.timedelta(hours=5))["state"], "EXHAUSTED")
        self.assertEqual(daily_schedule.claim(self.database, self.config, now + dt.timedelta(days=1))["attempt"], 1)

    # --- compute waits are not failed investigations (AGENTS.md) ----------------------

    def wait_result(self, retry_at):
        return {"state": "FAILED", "retryable": True, "compute_wait": True,
                "stop_reason": "COMPUTE_WAIT_ALL_UNRESOLVED_INDUSTRIES",
                "next_retry_at": retry_at.isoformat()}

    def test_compute_wait_defers_to_the_allowance_reset_without_charging_an_attempt(self):
        now = self.at("2026-09-18T08:00:12")
        reset = self.at("2026-09-19T00:00:00")
        claim = daily_schedule.claim(self.database, self.config, now)
        state = daily_schedule.finish(self.database, self.config, claim["id"], self.wait_result(reset), now)
        self.assertEqual(state["state"], "RETRY_WAIT")
        self.assertEqual(state["next_retry_at"], reset.isoformat())
        self.assertEqual(state["attempts"], 1)
        self.assertEqual(state["compute_wait_attempts"], 1)
        self.assertEqual(state["investigation_attempts"], 0)
        self.assertIsNone(self.database.execute("SELECT completed_at FROM research_days").fetchone()[0],
                          "a wait is never success")
        # The wait genuinely waits: nothing is claimable before the recorded reset.
        self.assertEqual(daily_schedule.claim(self.database, self.config, reset - dt.timedelta(seconds=1))["state"],
                         "RETRY_WAIT")
        # All three investigation attempts survive the wait.
        at = reset
        for index, expected in enumerate(("RETRY_WAIT", "RETRY_WAIT", "EXHAUSTED")):
            claim = daily_schedule.claim(self.database, self.config, at)
            self.assertEqual(claim["state"], "CLAIMED", (index, claim))
            state = daily_schedule.finish(self.database, self.config, claim["id"], {"state": "FAILED"}, at)
            self.assertEqual((state["state"], state["investigation_attempts"]), (expected, index + 1))
            at += dt.timedelta(hours=2)

    def test_compute_wait_excusals_are_bounded_and_the_day_still_ends(self):
        at = self.at("2026-09-18T08:00:12")
        excused = 0
        for index in range(8):
            claim = daily_schedule.claim(self.database, self.config, at)
            if claim["state"] != "CLAIMED":
                self.assertEqual(claim["state"], "EXHAUSTED")
                break
            state = daily_schedule.finish(self.database, self.config, claim["id"],
                                          self.wait_result(at + dt.timedelta(hours=1)), at)
            excused = state["compute_wait_attempts"]
            at += dt.timedelta(hours=2)
        else:
            self.fail("an unbroken run of compute waits must still terminate the day")
        self.assertEqual(excused, self.config["research_max_attempts"],
                         "at most research_max_attempts waits are excused")
        self.assertEqual(self.database.execute("SELECT attempts FROM research_days").fetchone()[0], 6)

    def test_an_unusable_compute_wait_retry_falls_back_to_the_normal_backoff(self):
        now = self.at("2026-09-18T08:00:12")
        naive = {"state": "FAILED", "retryable": True, "compute_wait": True,
                 "next_retry_at": "2026-09-19T00:00:00"}
        past = self.wait_result(now - dt.timedelta(minutes=1))
        distant = self.wait_result(now + dt.timedelta(days=2))
        missing = {"state": "FAILED", "retryable": True, "compute_wait": True}
        unclaimed = {"state": "FAILED", "retryable": False, "compute_wait": True,
                     "next_retry_at": (now + dt.timedelta(hours=1)).isoformat()}
        for index, result in enumerate((naive, past, distant, missing, unclaimed)):
            self.assertIsNone(daily_schedule.compute_wait_retry(result, now), index)
        claim = daily_schedule.claim(self.database, self.config, now)
        state = daily_schedule.finish(self.database, self.config, claim["id"], naive, now)
        self.assertEqual(state["state"], "RETRY_WAIT")
        self.assertEqual(state["compute_wait_attempts"], 0, "an unusable wait is charged as a failed attempt")
        self.assertEqual(state["next_retry_at"], (now + dt.timedelta(seconds=900)).isoformat())

    def test_success_after_failure_blocks_all_other_triggers(self):
        now = self.at("2026-09-17T08:00:00")
        first = daily_schedule.claim(self.database, self.config, now)
        daily_schedule.finish(self.database, self.config, first["id"], {"state": "FAILED"}, now)
        second = daily_schedule.claim(self.database, self.config, now + dt.timedelta(minutes=15))
        daily_schedule.finish(self.database, self.config, second["id"], {"state": "COMPLETE"}, now + dt.timedelta(minutes=20))
        for hours in (1, 4, 20):
            self.assertEqual(daily_schedule.claim(self.database, self.config, now + dt.timedelta(hours=hours))["state"], "COMPLETE")

    def test_crash_is_failure_and_uses_retry_budget(self):
        now = self.at("2026-09-17T08:00:00")
        first = daily_schedule.claim(self.database, self.config, now)
        daily_schedule.recover_interrupted(self.database, self.config, now + dt.timedelta(minutes=5))
        self.assertEqual(self.database.execute("SELECT state FROM research WHERE id=?", (first["id"],)).fetchone()[0], "INTERRUPTED")
        self.assertEqual(daily_schedule.claim(self.database, self.config, now + dt.timedelta(minutes=6))["state"], "RETRY_WAIT")

    def test_concurrent_dispatchers_claim_only_one_attempt(self):
        now = self.at("2026-09-17T08:00:00")
        def claim():
            connection = orchestrator.connect(self.root)
            try:
                return daily_schedule.claim(connection, self.config, now)["state"]
            finally:
                connection.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as workers:
            results = list(workers.map(lambda ignored: claim(), range(4)))
        self.assertEqual(results.count("CLAIMED"), 1)
        self.assertEqual(results.count("RUNNING"), 3)

    def test_dst_and_foreign_timezone_still_use_pacific_day(self):
        for value, offset in (("2026-03-08T08:00:00", -7), ("2026-11-01T08:00:00", -8)):
            now = self.at(value)
            self.assertEqual(now.utcoffset(), dt.timedelta(hours=offset))
            self.assertEqual(daily_schedule.due_day(now.astimezone(dt.timezone.utc), self.config), value[:10])
            previous = now - dt.timedelta(minutes=1)
            self.assertNotEqual(daily_schedule.due_day(previous, self.config), value[:10])

    def test_legacy_success_migration_preserves_records(self):
        self.database.execute("DELETE FROM schedule_meta")
        self.database.execute("INSERT INTO research VALUES ('legacy','2026-09-17T05:42:30+00:00','COMPLETE','{}')")
        self.database.execute("INSERT INTO research VALUES ('failure','2026-09-18T05:42:31+00:00','FAILED','{}')")
        self.database.commit()
        daily_schedule.initialize(self.database, self.config["timezone"])
        self.assertEqual(self.database.execute("SELECT day FROM research_days").fetchone()[0], "2026-09-16")
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM research").fetchone()[0], 2)
