"""Offline scheduler admission check; no model or provider calls.

Deterministic by construction. Every instant this check uses is derived from
SCENARIO_START, and the orchestrator's own clock is pinned to it while
`orchestrator.research` runs, so the check behaves identically whatever the ambient
wall clock says. It previously read the real clock and then stepped 16 and 46 minutes
forward, so whenever the real time sat inside the 46 minutes before the daily research
rollover the two later claims landed in a different research day than the first and the
terminal-abandonment assertion failed. Moving the rollover to midnight would only have
relocated that window to 23:14-00:00; pinning the clock removes it.
"""

import datetime as dt
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import orchestrator
import daily_schedule
import workflow_tasks


PACIFIC = ZoneInfo("America/Los_Angeles")
# 01:00 Pacific on 2026-09-18: one hour into the research day of this scenario's config
# (research_hour 0). The live service day opens at 09:00 (2026-09-30); nothing here crosses it. 23 hours remain before the next rollover, so the
# 46-minute retry sequence below stays inside one research day, one compute day and one
# checkpoint day.
SCENARIO_START = dt.datetime(2026, 9, 18, 1, 0, tzinfo=PACIFIC)
DAILY_SECONDS = 10800
SERVICE_RESERVE = 3600
RESEARCH_CEILING = DAILY_SECONDS - SERVICE_RESERVE


def pinned_clock(instant):
    """A stand-in for the `datetime` module whose `now()` is `instant`.

    Nothing else changes: `Pinned` is a real `dt.datetime` subclass, so `fromisoformat`,
    arithmetic and comparisons behave exactly as before.
    """
    class Pinned(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    return SimpleNamespace(datetime=Pinned, timedelta=dt.timedelta, timezone=dt.timezone)


def validate():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "state").mkdir()
        (root / "reports").mkdir()
        database = orchestrator.connect(root)
        workflow_tasks.initialize(database)
        config = {"scope": "job_search_e2e", "timezone": "America/Los_Angeles",
                  "research_hour": 0, "research_max_attempts": 3,
                  "research_retry_delays_seconds": [900, 3600],
                  "research_timeout_seconds": 600,
                  "research_service_reserve_seconds": SERVICE_RESERVE}
        now = SCENARIO_START.isoformat()
        admissions = []

        def research(root, settings, database, *args, **kwargs):
            ceiling = settings["daily_compute_seconds"]
            with database:
                admissions.append(workflow_tasks.reserve_compute(database, "research-budget", now,
                                                                  RESEARCH_CEILING, daily_seconds=ceiling))
                admissions.append(workflow_tasks.reserve_compute(database, "research-overflow", now, 1,
                                                                  daily_seconds=ceiling))
            return {"state": "FAILED", "reason": "Synthetic worker failure; no research findings"}

        with patch("parallel_research.run", side_effect=research), \
                patch.object(orchestrator, "dt", pinned_clock(SCENARIO_START)):
            result = orchestrator.research(root, config, database, {}, {})
        # The ceiling research was admitted against is exactly daily - reserve: the whole of
        # it fits and not one second more. 9,000 or 10,800 would have admitted the overflow.
        assert admissions == [True, False], admissions
        assert result["compute_allocation"] == {"daily_seconds": DAILY_SECONDS,
                                                "research_admission_ceiling": RESEARCH_CEILING,
                                                "protected_service_seconds": SERVICE_RESERVE}, result["compute_allocation"]
        assert result["state"] == "FAILED"
        assert result["schedule"]["state"] == "RETRY_WAIT"
        assert daily_schedule.due_day(SCENARIO_START, config) == "2026-09-18"
        assert database.execute("SELECT completed_at FROM research_days").fetchone()[0] is None
        with database:
            # The reserve survived research untouched, and the 10,800 hard ceiling still holds.
            assert workflow_tasks.reserve_compute(database, "downstream-services", now, SERVICE_RESERVE)
            assert not workflow_tasks.reserve_compute(database, "daily-overflow", now, 1)
        before = [tuple(row) for row in database.execute("SELECT * FROM worker_compute_reservations ORDER BY action_id")]
        database.close()
        database = orchestrator.connect(root)
        with database:
            assert workflow_tasks.reserve_compute(database, "downstream-services", now, SERVICE_RESERVE)
        assert before == [tuple(row) for row in database.execute("SELECT * FROM worker_compute_reservations ORDER BY action_id")]
        after_backoff = SCENARIO_START + dt.timedelta(minutes=16)
        claim = daily_schedule.claim(database, config, after_backoff)
        assert claim["state"] == "CLAIMED"
        abandoned = {"state": "FAILED", "retryable": False,
                     "stop_reason": "ALL_UNRESOLVED_INDUSTRIES_ABANDONED"}
        finished = daily_schedule.finish(database, config, claim["id"], abandoned, after_backoff)
        assert finished["state"] == "EXHAUSTED" and finished["next_retry_at"] is None
        assert database.execute("SELECT completed_at FROM research_days").fetchone()[0] is None
        assert daily_schedule.claim(database, config, after_backoff + dt.timedelta(minutes=30))["state"] == "EXHAUSTED"
        assert database.execute("SELECT COUNT(*) FROM research_days").fetchone()[0] == 1
        database.close()
        return {"state": "PASS", "synthetic_only": True, "live_calls": 0,
                "checks": ["scheduled research receives reduced admission ceiling",
                           "research cannot consume service headroom", "services retain normal shared ceiling",
                           "no daily cap increase", "failed research not success", "restart retains charges",
                           "reservation replay adds no charge", "terminal abandonment stops daily retries without success",
                           "entire sequence stays inside one research day at any ambient wall-clock time"],
                "allocation": result["compute_allocation"]}


if __name__ == "__main__":
    print(json.dumps(validate(), indent=2))
