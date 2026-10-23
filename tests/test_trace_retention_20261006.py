"""Browser traces and recovery records live 30 days; the daily report runs once a day (owner, 2026-10-06)."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

import log_retention
import reporting


class TraceRetentionTests(unittest.TestCase):
    def test_only_folders_whose_newest_file_is_past_the_ttl_go(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / "reports/application-browser"
            old, recent, mixed = base / "request-old", base / "request-new", base / "request-mixed"
            for folder in (old, recent, mixed):
                folder.mkdir(parents=True)
                (folder / "trace.jsonl").write_text("x")
            (mixed / "late.json").write_text("y")
            outside = root / "keep"
            outside.mkdir()
            (base / "request-link").symlink_to(outside)
            now = time.time()
            stale = now - 31 * 86400
            for path in (old / "trace.jsonl", old, mixed / "trace.jsonl", mixed):
                os.utime(path, (stale, stale))
            result = log_retention.prune_traces(root, now)
            self.assertEqual(1, result["removed"])
            self.assertFalse(old.exists())
            self.assertTrue(recent.exists() and mixed.exists() and outside.exists())


class DailyOnceTests(unittest.TestCase):
    def test_due_after_the_report_hour_until_the_day_is_saved(self):
        database = sqlite3.connect(":memory:")
        config = {"timezone": "America/Los_Angeles", "daily_report_hour": 18}
        self.assertFalse(reporting.daily_due(database, config, "2026-10-06T20:00:00+00:00"))  # 13:00 local
        self.assertTrue(reporting.daily_due(database, config, "2026-10-07T01:30:00+00:00"))  # 18:30 local
        database.execute("CREATE TABLE daily_reports (day TEXT, created_at TEXT, payload TEXT, notification_key TEXT)")
        database.execute("INSERT INTO daily_reports VALUES ('2026-10-06','x','{}','k')")
        self.assertFalse(reporting.daily_due(database, config, "2026-10-07T02:30:00+00:00"))


if __name__ == "__main__":
    unittest.main()
