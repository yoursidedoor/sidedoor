"""The service day starts at day_start_hour (owner, 2026-09-30: 9:00 Pacific, was midnight)."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
import unittest
from zoneinfo import ZoneInfo

import workflow_tasks


PACIFIC = ZoneInfo("America/Los_Angeles")


class ServiceDayTests(unittest.TestCase):
    def test_budget_day_is_labelled_by_the_date_it_ends(self):
        before = dt.datetime(2026, 9, 30, 8, 59, 59, tzinfo=PACIFIC)
        after = dt.datetime(2026, 9, 30, 9, 0, tzinfo=PACIFIC)
        self.assertEqual(workflow_tasks.budget_day(before, hour=9), "2026-09-30")
        self.assertEqual(workflow_tasks.budget_day(after, hour=9), "2026-10-01")
        self.assertEqual(workflow_tasks.next_day_start(before, hour=9), after)
        self.assertEqual(workflow_tasks.next_day_start(after, hour=9), after + dt.timedelta(days=1))
        self.assertEqual(workflow_tasks.service_date(before, hour=9), "2026-09-29")
        self.assertEqual(workflow_tasks.service_date(after, hour=9), "2026-09-30")

    def test_hour_zero_is_the_local_calendar_date(self):
        start = dt.datetime(2026, 3, 7, 0, 0, tzinfo=PACIFIC)
        for minutes in range(0, 3 * 24 * 60, 13):   # spans the 2026-03-08 DST change
            moment = (start + dt.timedelta(minutes=minutes)).astimezone(dt.timezone.utc)
            local = moment.astimezone(PACIFIC)
            self.assertEqual(workflow_tasks.budget_day(moment.isoformat(), hour=0), local.date().isoformat())
            self.assertEqual(workflow_tasks.service_date(moment, hour=0), local.date().isoformat())
            self.assertEqual(workflow_tasks.next_day_start(moment, hour=0),
                             dt.datetime.combine(local.date() + dt.timedelta(days=1), dt.time(0), PACIFIC))


if __name__ == "__main__":
    unittest.main()
