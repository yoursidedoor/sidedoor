"""The scheduler's admission check, and proof that the wall clock cannot change its answer.

It used to read the real clock and then step 16 and 46 minutes forward, so in the 46 minutes
before the daily research rollover its later claims landed in a different research day than
its first and the terminal-abandonment assertion failed. Moving the rollover from 08:00 to
midnight would only have relocated that window to 23:14-00:00, so the fixture now pins its
own clock instead. These tests run it under simulated ambient times on both sides of both
the old and the new rollover and require an identical result every time.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import daily_schedule
import manual_service_headroom
import orchestrator


PACIFIC = ZoneInfo("America/Los_Angeles")
# 07:30 and 23:50 are the two the change is about: 07:30 sat inside the old broken window,
# 23:30/23:50 sit around where a naive "just move the hour" fix would have relocated it.
AMBIENT = [(0, 5), (7, 30), (7, 59), (8, 1), (12, 0), (23, 30), (23, 50), (23, 59)]


class ServiceHeadroomTests(unittest.TestCase):
    def test_scheduled_research_preserves_downstream_capacity(self):
        result = manual_service_headroom.validate()
        self.assertEqual(result["state"], "PASS")
        self.assertEqual(result["allocation"], {"daily_seconds": 10800, "research_admission_ceiling": 7200,
                                                "protected_service_seconds": 3600})

    def test_the_result_is_identical_at_every_ambient_wall_clock_time(self):
        baseline = manual_service_headroom.validate()
        for hour, minute in AMBIENT:
            ambient = manual_service_headroom.pinned_clock(
                dt.datetime(2026, 9, 18, hour, minute, tzinfo=PACIFIC))
            with self.subTest(ambient=f"{hour:02d}:{minute:02d} Pacific"), \
                    patch.object(manual_service_headroom, "dt", ambient), \
                    patch.object(daily_schedule, "dt", ambient), \
                    patch.object(orchestrator, "dt", ambient):
                self.assertEqual(manual_service_headroom.validate(), baseline)
