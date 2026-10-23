"""E3.T4 option A: caps, warm-up, working hours and jitter over INVITE request records."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
import json
import sqlite3
import unittest

import outreach
import pacing


CONFIG = {"timezone": "America/Los_Angeles"}
# Wednesday 2026-10-14 10:00 Pacific = 17:00 UTC.
WED_10AM = dt.datetime(2026, 10, 14, 17, 0, tzinfo=dt.timezone.utc)


class Pacing(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        outreach.initialize_actions(self.db)
        self.count = 0

    def invite(self, started):
        self.count += 1
        payload = json.dumps({"purpose": "INVITE", "provider": "unipile"})
        self.db.execute("INSERT INTO outreach_actions(action_id,identity,slot,payload,payload_hash,state,result) VALUES (?,?,?,?,?,?,?)",
                        (f"a{self.count}", f"p{self.count}", "INVITE", payload, "h", "CONFIRMED_SENT",
                         json.dumps({"attempts": [{"started_at": started.isoformat()}]})))

    def test_first_invite_goes_inside_working_hours(self):
        self.assertEqual(pacing.check(self.db, CONFIG, WED_10AM), {"go": True})

    def test_outside_working_hours_waits_for_next_weekday_morning(self):
        saturday = dt.datetime(2026, 10, 17, 19, 0, tzinfo=dt.timezone.utc)
        out = pacing.check(self.db, CONFIG, saturday)
        self.assertEqual(out["reason"], "OUTSIDE_WORKING_HOURS")
        self.assertEqual(out["wait_until"], "2026-10-19T16:00:00+00:00")  # Monday 09:00 Pacific
        evening = dt.datetime(2026, 10, 15, 2, 0, tzinfo=dt.timezone.utc)  # Wed 19:00 Pacific
        self.assertEqual(pacing.check(self.db, CONFIG, evening)["wait_until"], "2026-10-15T16:00:00+00:00")

    def test_jitter_spaces_requests_4_to_12_minutes(self):
        self.invite(WED_10AM)
        out = pacing.check(self.db, CONFIG, WED_10AM + dt.timedelta(minutes=3))
        self.assertEqual(out["reason"], "JITTER")
        gap = pacing._utc(out["wait_until"]) - WED_10AM
        self.assertTrue(dt.timedelta(minutes=4) <= gap <= dt.timedelta(minutes=12), gap)
        self.assertEqual(pacing.check(self.db, CONFIG, WED_10AM + dt.timedelta(minutes=13)), {"go": True})

    def test_warm_up_ramps_the_daily_cap(self):
        first = WED_10AM - dt.timedelta(days=30)
        self.invite(first)
        for weeks, expected in ((0, 20), (1, 25), (2, 25), (9, 25)):
            self.assertEqual(pacing.daily_cap(pacing._settings(CONFIG), [first], first + dt.timedelta(weeks=weeks, hours=1)),
                             expected)

    def test_daily_cap_in_first_week_waits_until_the_oldest_leaves_the_window(self):
        for minutes in range(0, 20 * 12, 12):  # 20 requests, 12 minutes apart, on day one
            self.invite(WED_10AM + dt.timedelta(minutes=minutes))
        now = WED_10AM + dt.timedelta(hours=5)
        out = pacing.check(self.db, CONFIG, now)
        self.assertEqual((out["reason"], out["wait_until"]), ("DAILY_CAP", (WED_10AM + dt.timedelta(hours=24)).isoformat()))

    def test_weekly_cap(self):
        start = WED_10AM - dt.timedelta(days=60)
        self.invite(start)  # an old first request: warm-up done
        for index in range(100):
            self.invite(WED_10AM - dt.timedelta(days=6, hours=-index))  # 100 within the last 7 days
        out = pacing.check(self.db, {**CONFIG, "pacing": {"daily_cap": 500}}, WED_10AM)
        self.assertEqual(out["reason"], "WEEKLY_CAP")


if __name__ == "__main__":
    unittest.main()
