"""E16.T3, the read path (owner, 2026-11-10 and 2026-11-13): "If the page times out after the user's step:
one retry in a new session with the saved result, without spending one of the job's attempts. If the site
asks again: the card comes back." For a check that gates the site the user's step is the live view on the
card, and the next session is a read."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import access_outcomes  # noqa: E402
import application_connection  # noqa: E402
import applications  # noqa: E402
import desktop_adapter  # noqa: E402
import test_access_outcomes_e15_t1 as walls  # noqa: E402
import test_part_c_read_gating as part_c  # noqa: E402

TIMED_OUT = {"error": {"code": "APPLICATION_BROWSER_TIMEOUT"}}


class TheFreeRead(unittest.TestCase):
    for _name in ("setUp", "connect", "write_config", "review", "job", "cycle", "advance", "run_until_submit", "row",
                  "requests", "waits", "events", "blocked_session", "assert_waiting", "second_job"):
        locals()[_name] = getattr(walls.AccessOutcomeTests, _name)
    del _name

    def after_the_step(self, code="CAPTCHA_REQUIRED"):
        """A read met the wall, the job waited, and the user did their step (the wait is released)."""
        self.blocked_session(code)
        with self.database:
            self.assertEqual(access_outcomes.resume(self.database, self.key, self.now), [self.key])
        self.advance(60)

    def read_ends(self, result):
        self.cycle()
        request = desktop_adapter.claim(self.database, self.root, "access-fixture", self.now)
        self.assertIsNotNone(request, "no read was started")
        self.assertEqual(request["payload"]["operation"], "inspect")
        self.assertTrue(desktop_adapter.complete(self.database, dict(request, result=result), self.now)["accepted"])
        self.advance(60)
        self.cycle()
        return request["request_id"]

    def test_the_first_read_after_the_step_that_times_out_is_retried_once_and_spends_nothing(self):
        self.after_the_step()
        first = self.read_ends(TIMED_OUT)
        row = self.row()
        self.assertEqual(row["attempts"], 0)                                   # no try spent
        self.assertNotEqual(row["blocker"], "PREFLIGHT_TRANSIENT")             # no recovery opened
        self.assertFalse(self.events("APPLICATION_PREFLIGHT_FAILED"))
        self.assertEqual([item["request_id"] for item in self.events(access_outcomes.FREE_READ_EVENT)], [first])
        self.assertEqual(access_outcomes.free_reads(self.database, self.key), {first})   # no read counted
        # The retry is a new session. A second timeout is an ordinary failed read: the free one is used.
        second = self.read_ends(TIMED_OUT)
        self.assertNotEqual(second, first)
        self.assertEqual(self.row()["attempts"], 1)
        self.assertEqual(len(self.events(access_outcomes.FREE_READ_EVENT)), 1)
        self.assertTrue(self.events("APPLICATION_PREFLIGHT_FAILED"))

    def test_if_the_site_asks_again_on_the_retry_the_card_comes_back(self):
        self.after_the_step()
        self.read_ends(TIMED_OUT)
        self.read_ends(walls.stop("CAPTCHA_REQUIRED"))
        self.assert_waiting("HUMAN_CHECK", "CAPTCHA_REQUIRED")                   # attempts still 0
        self.assertEqual([wait["state"] for wait in self.waits()], [access_outcomes.RESOLVED, access_outcomes.WAITING])

    def test_only_a_timeout_and_only_after_a_human_check_step(self):
        self.after_the_step()
        self.read_ends({"error": {"code": "APPLICATION_BROWSER_EXECUTOR_FAILED"}})   # another failure: as ever
        self.assertEqual(self.row()["attempts"], 1)
        self.assertFalse(self.events(access_outcomes.FREE_READ_EVENT))
        self.setUp()
        with patch.object(access_outcomes, "effective_setting", return_value=access_outcomes.ASK):
            self.after_the_step("AUTHENTICATION_REQUIRED")                          # a sign-in step: not this rule
            self.read_ends(TIMED_OUT)
        self.assertEqual(self.row()["attempts"], 1)
        self.assertFalse(self.events(access_outcomes.FREE_READ_EVENT))

    def test_a_timeout_with_no_step_before_it_is_an_ordinary_failed_read(self):
        self.read_ends(TIMED_OUT)
        self.assertEqual(self.row()["attempts"], 1)
        self.assertFalse(self.events(access_outcomes.FREE_READ_EVENT))


class TheReadLimit(unittest.TestCase):
    setUp = part_c.PartCTests.setUp
    job = part_c.PartCTests.job

    def test_the_free_read_is_not_one_of_the_three(self):
        later = part_c.at(4 * 3600)
        adapter = application_connection.HostedDesktopAdapter(self.database, self.root, {"application_browser": {"enabled": True}},
                                                              clock=lambda: later)
        body = adapter.request_body("inspect", {"job": self.job()})
        with self.database:
            for index in range(3):
                self.database.execute("""INSERT INTO desktop_requests(request_id,binding,job_key,operation,payload,state,
                    created_at,expires_at,claims,result) VALUES (?,?,?,?,?,'CONSUMED',?,?,1,?)""",
                    ("r" + str(index), "b" + str(index), self.key, "inspect", desktop_adapter.encode(body), later,
                     part_c.at(4 * 3600 + 900), json.dumps(TIMED_OUT)))
        allowed = {"reviewed": True}
        with patch.object(application_connection.HostedDesktopAdapter, "reserve", lambda *a, **k: None), \
                patch.object(desktop_adapter.DesktopAdapter, "call", lambda *a, **k: allowed):
            exhausted = adapter.call("inspect", {"job": self.job(), "read_only": True})
            self.assertEqual(exhausted["blocked"]["code"], "APPLICATION_BROWSER_INVESTIGATION_EXHAUSTED")
            with self.database:
                applications.event(self.database, self.key, access_outcomes.FREE_READ_EVENT,
                                   {"wait": 1, "request_id": "r0", "code": "APPLICATION_BROWSER_TIMEOUT"}, later)
            self.assertIs(adapter.call("inspect", {"job": self.job(), "read_only": True}), allowed)


if __name__ == "__main__":
    unittest.main()
