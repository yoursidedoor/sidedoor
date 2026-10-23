"""The three numbers that bound one browser session must agree.

Written 2026-09-22 after raising `application_browser.timeout_seconds` to 480 while
`MAX_SECONDS` was still 240. That is not a slow read - `consume` refuses
APPLICATION_BROWSER_BOUNDS_INVALID before claiming the browser, so EVERY job's read failed
instantly, and umbrella spent its third and last attempt on the refusal. Nothing caught it
because nothing asserted the config and the ceiling were consistent.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import inspect
import json
import unittest
from pathlib import Path

import application_browser
import application_connection
import desktop_adapter


ROOT = Path(__file__).resolve().parent.parent


class BrowserSessionBoundsTests(unittest.TestCase):
    def configured_timeout(self):
        config = json.loads((ROOT / "config.json").read_text())
        settings = config.get("application_browser", {})
        return settings.get("timeout_seconds", application_browser.MAX_SECONDS)

    def test_the_configured_timeout_is_within_the_hard_ceiling(self):
        timeout = self.configured_timeout()
        self.assertLessEqual(
            timeout, application_browser.MAX_SECONDS,
            "config.json application_browser.timeout_seconds=%s exceeds MAX_SECONDS=%s, so "
            "application_browser.consume raises APPLICATION_BROWSER_BOUNDS_INVALID and every "
            "read fails before the browser is claimed" % (timeout, application_browser.MAX_SECONDS))
        self.assertGreaterEqual(timeout, 1)

    def test_the_reservation_covers_a_full_length_session(self):
        reserved = inspect.signature(application_connection.HostedDesktopAdapter.reserve).parameters[
            "seconds_per_operation"].default
        self.assertGreaterEqual(
            reserved, application_browser.MAX_SECONDS,
            "HostedDesktopAdapter.reserve books %ss per operation while a session may run %ss, so a long "
            "read would consume more compute than it charged against the daily allowance"
            % (reserved, application_browser.MAX_SECONDS))

    def test_the_claim_lease_outlasts_a_full_length_session(self):
        """The lease, not the ceiling, was the real timeout until 2026-09-22.

        `consume` computes min(configured_budget, lease_remaining - 10). With a 300s lease and
        a 480s ceiling every read silently got 290s, so umbrella's form - which needs more
        than 240s - could never be read no matter how high the ceiling went.
        """
        self.assertGreater(
            desktop_adapter.LEASE_SECONDS, application_browser.MAX_SECONDS + 10,
            "LEASE_SECONDS=%s does not outlast a %ss session plus consume()'s 10s margin, so "
            "the lease caps every read below its configured budget"
            % (desktop_adapter.LEASE_SECONDS, application_browser.MAX_SECONDS))

    def test_a_request_too_stale_to_finish_is_refused_rather_than_half_run(self):
        """A request expires 900s after creation, so one that sat pending hands the next
        session a fraction of its budget. Umbrella got 207s of a 475s budget that way and
        spent an attempt failing at exactly 207s."""
        source = inspect.getsource(application_browser.consume)
        self.assertIn("APPLICATION_REQUEST_LIFE_TOO_SHORT_FOR_A_FULL_READ", source)
        self.assertIn("if timeout < budget:", source)

    def test_the_configured_timeout_is_actually_what_consume_enforces(self):
        """Pins the bound to the real code path rather than to a copy of the number."""
        source = inspect.getsource(application_browser.consume)
        self.assertIn("1 <= timeout <= MAX_SECONDS", source)
        self.assertIn('settings.get("timeout_seconds", MAX_SECONDS)', source)


if __name__ == "__main__":
    unittest.main()


class BrowserReservationRefundTests(unittest.TestCase):
    """A browser reservation books the ceiling up front; the tail it never used comes back.

    Before 2026-09-22 nothing credited a browser reservation, so raising the session ceiling
    doubled what every read spent whether it needed the time or not.
    """

    def setUp(self):
        import sqlite3
        import workflow_tasks
        self.workflow_tasks = workflow_tasks
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        workflow_tasks.initialize(self.database)
        import desktop_adapter
        desktop_adapter.initialize(self.database)
        self.addCleanup(self.database.close)

    def reserve(self, operations, seconds, at):
        self.database.execute("INSERT INTO worker_compute_reservations VALUES (?,?,?,?)",
                              ("application-browser:r1", "2026-09-22", seconds, at))
        self.workflow_tasks.track_browser_reservation(
            self.database, "application-browser:r1", "acme:1", operations, seconds, at)

    def commit(self, request_id, at):
        self.database.execute(
            "INSERT OR IGNORE INTO desktop_requests(request_id,binding,job_key,operation,payload,state,created_at,expires_at)"
            " VALUES (?,?,?,?,?,?,?,?)", (request_id, "b", "acme:1", "inspect", "{}", "DONE", at, at))
        self.database.execute("INSERT INTO desktop_events(request_id,at,event,detail) VALUES (?,?,?,?)",
                              (request_id, at, "BROWSER_CONSUMER_COMPLETED", "{}"))

    def test_a_short_read_credits_back_the_unused_tail(self):
        self.reserve(1, 480, "2026-09-22T10:00:00+00:00")
        self.commit("req-1", "2026-09-22T10:02:00+00:00")          # 120s of a 480s booking
        credited = self.workflow_tasks.settle_browser_reservations(
            self.database, "acme:1", "2026-09-22T10:02:00+00:00")
        self.assertEqual(credited, 360, "480 booked, 120 measured, 360 should return")

    def test_an_uncommitted_session_keeps_its_whole_charge(self):
        self.reserve(1, 480, "2026-09-22T10:00:00+00:00")
        credited = self.workflow_tasks.settle_browser_reservations(
            self.database, "acme:1", "2026-09-22T10:10:00+00:00")
        self.assertEqual(credited, 0,
                         "a timed-out or killed session commits nothing and must retain its charge")

    def test_a_two_operation_block_settles_only_once_both_commit(self):
        self.reserve(2, 960, "2026-09-22T10:00:00+00:00")
        self.commit("req-1", "2026-09-22T10:01:00+00:00")
        self.assertEqual(self.workflow_tasks.settle_browser_reservations(
            self.database, "acme:1", "2026-09-22T10:01:00+00:00"), 0,
            "refunding after the first operation would credit the second one's budget")
        self.commit("req-2", "2026-09-22T10:03:00+00:00")
        self.assertEqual(self.workflow_tasks.settle_browser_reservations(
            self.database, "acme:1", "2026-09-22T10:03:00+00:00"), 780)

    def test_settlement_is_idempotent(self):
        self.reserve(1, 480, "2026-09-22T10:00:00+00:00")
        self.commit("req-1", "2026-09-22T10:02:00+00:00")
        first = self.workflow_tasks.settle_browser_reservations(self.database, "acme:1", "2026-09-22T10:02:00+00:00")
        second = self.workflow_tasks.settle_browser_reservations(self.database, "acme:1", "2026-09-22T10:05:00+00:00")
        self.assertEqual((first, second), (360, 0))
