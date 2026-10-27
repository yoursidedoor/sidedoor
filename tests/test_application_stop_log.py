"""Owner, 2026-10-25: the applications job writes one plain line per stop (a failed try or a give-up)."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import json
import sqlite3
import unittest

import applications

NOW = "2026-10-25T18:00:00+00:00"


class StopLogTests(unittest.TestCase):
    def setUp(self):
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        applications.initialize(self.database)
        self.database.execute("INSERT INTO application_jobs(job_key,employer,requisition,payload,state,updated_at) "
                              "VALUES ('together ai:1','together ai','1',?,'QUEUED',?)",
                              (json.dumps({"company": "Together AI", "role": "Senior Backend Engineer"}), NOW))

    def job(self):
        return dict(self.database.execute("SELECT * FROM application_jobs WHERE job_key='together ai:1'").fetchone())

    def lines(self, code, state, next_at=None, action="Inspect failure evidence"):
        with self.assertLogs("sidedoor.applications", "INFO") as captured:
            applications.hold(self.database, self.job(), code, action, NOW, state, next_at)
            applications.LOG.info("end")
        return [record.getMessage() for record in captured.records][:-1]

    def test_a_failed_try_and_the_give_up_each_write_one_plain_line(self):
        self.assertEqual(self.lines("APPLICATION_TOOL_LIMIT", "BLOCKED", "2026-10-25T19:00:00+00:00"),
                         ["Together AI, Senior Backend Engineer: this try failed. The browser session used all of its "
                          "allowed steps before it finished. Sidedoor will try again at 2026-10-25 19:00 UTC."])
        self.assertEqual(len(self.lines("APPLICATION_TOOL_LIMIT", "BLOCKED")), 1)   # the same stop again: a line again
        self.assertEqual(self.lines("RETRIES_EXHAUSTED", "EXHAUSTED"),
                         ["Together AI, Senior Backend Engineer: Sidedoor gave up on this application. Sidedoor used all "
                          "of its tries. Its last try stopped on this: The browser session used all of its allowed steps "
                          "before it finished."])

    def test_an_answering_give_up_says_why_once_then_what_to_do(self):
        self.assertEqual(self.lines("ANSWERING_GAVE_UP:unidentified-question:x:1", "EXHAUSTED"),
                         ["Together AI, Senior Backend Engineer: Sidedoor gave up on this application. Sidedoor's AI "
                          "could not fill in the same answers in 7 tries. Apply by hand if you want this one."])

    def test_holds_waits_and_rejections_write_nothing(self):
        self.assertEqual(self.lines("CAPTCHA_REQUIRED", "BLOCKED"), [])            # owner D6: not a failure while retrying
        self.assertEqual(self.lines("APPLICATION_BROWSER_BUSY", "BLOCKED"), [])    # a wait
        self.assertEqual(self.lines(applications.APPLICATION_REJECTED, "ABANDONED", action="You rejected it: my secret"), [])


if __name__ == "__main__":
    unittest.main()
