"""Every worker gives back its unused compute tail however it ended (owner T2, 2026-11-19).

Finished cleanly, failed or interrupted before its limit: the measured unused tail goes back at
once. Hit its limit: ~0 comes back. Never twice for one booking; a retry books its own.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import answering_agent
import backlog_fixture
import desktop_adapter
import test_reply_worker
import workflow_tasks

BOOKED = "2026-11-19T10:00:00+00:00"


def database():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    workflow_tasks.initialize(connection)
    connection.commit()
    return connection


def credited(connection, booking):
    row = connection.execute("SELECT seconds FROM worker_compute_refunds WHERE action_id=?", (booking,)).fetchone()
    return row[0] if row else 0


class SharedSettlementTests(unittest.TestCase):
    def setUp(self):
        self.database = database()
        self.addCleanup(self.database.close)
        with self.database:
            self.assertTrue(workflow_tasks.reserve_compute(self.database, "reply:T1:1", BOOKED, 600))

    def test_failure_after_40_s_of_a_600_s_booking_credits_560(self):
        self.assertEqual(workflow_tasks.give_back_compute(
            self.database, "reply:T1:1", BOOKED, "2026-11-19T10:00:40+00:00", "failed"), 560)
        self.assertEqual(credited(self.database, "reply:T1:1"), 560)

    def test_a_run_that_hit_its_limit_credits_nothing(self):
        self.assertEqual(workflow_tasks.give_back_compute(
            self.database, "reply:T1:1", BOOKED, "2026-11-19T10:10:00+00:00", "timed out"), 0)
        self.assertEqual(credited(self.database, "reply:T1:1"), 0)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM worker_compute_refunds").fetchone()[0], 1,
                         "the zero-credit settlement is recorded")

    def test_a_second_settle_credits_nothing_more(self):
        workflow_tasks.give_back_compute(self.database, "reply:T1:1", BOOKED, "2026-11-19T10:00:40+00:00", "failed")
        workflow_tasks.give_back_compute(self.database, "reply:T1:1", BOOKED, "2026-11-19T10:00:01+00:00", "again")
        self.assertEqual(self.database.execute("SELECT COUNT(*), SUM(seconds) FROM worker_compute_refunds").fetchone()[:],
                         (1, 560))

    def test_a_retry_after_a_run_that_hit_its_limit_books_its_own(self):
        """Owner Z1 a: a zero-credit settlement still marks the booking used, so the retry is charged."""
        self.assertEqual(workflow_tasks.unsettled_booking(self.database, "reply:T1:1"), "reply:T1:1")
        workflow_tasks.give_back_compute(self.database, "reply:T1:1", BOOKED, "2026-11-19T10:10:00+00:00", "timed out")
        retry = workflow_tasks.unsettled_booking(self.database, "reply:T1:1")
        self.assertEqual(retry, "reply:T1:1#2")
        with self.database:
            self.assertTrue(workflow_tasks.reserve_compute(self.database, retry, "2026-11-19T10:11:00+00:00", 600))
        self.assertEqual(workflow_tasks.compute_used(self.database, workflow_tasks.budget_day(BOOKED, "America/Los_Angeles")), 1200)

    def test_a_retry_on_a_refunded_booking_books_its_own(self):
        self.assertEqual(workflow_tasks.unsettled_booking(self.database, "reply:T1:1"), "reply:T1:1")
        workflow_tasks.give_back_compute(self.database, "reply:T1:1", BOOKED, "2026-11-19T10:00:40+00:00", "failed")
        self.assertEqual(workflow_tasks.unsettled_booking(self.database, "reply:T1:1"), "reply:T1:1#2")

    def test_a_refund_that_cannot_be_recorded_never_raises(self):
        self.assertEqual(workflow_tasks.give_back_compute(self.database, "missing", BOOKED, BOOKED, "x"), 0)
        self.assertEqual(workflow_tasks.give_back_compute(
            self.database, "reply:T1:1", BOOKED, "2026-11-19T09:00:00+00:00", "end before start"), 0)


class BrowserSessionTests(unittest.TestCase):
    """An uncommitted (killed, interrupted) session is refunded the tail after its lease ran out."""

    def setUp(self):
        self.database = database()
        desktop_adapter.initialize(self.database)
        self.addCleanup(self.database.close)
        self.database.execute("INSERT INTO worker_compute_reservations VALUES (?,?,?,?)",
                              ("application-browser:r1", "2026-11-19", 1800, BOOKED))
        workflow_tasks.track_browser_reservation(self.database, "application-browser:r1", "acme:1", 1, 1800, BOOKED)
        self.database.execute(
            "INSERT INTO desktop_requests(request_id,binding,job_key,operation,payload,state,created_at,expires_at,claims,lease_until)"
            " VALUES ('q1','b','acme:1','inspect','{}','CLAIMED','2026-11-19T10:01:00+00:00',"
            "'2026-11-19T10:25:00+00:00',1,'2026-11-19T10:17:00+00:00')")

    def test_a_session_inside_its_lease_waits(self):
        self.assertEqual(workflow_tasks.settle_browser_reservations(
            self.database, "acme:1", "2026-11-19T10:10:00+00:00"), 0)

    def test_an_uncommitted_session_gets_the_tail_after_its_lease(self):
        self.assertEqual(workflow_tasks.settle_browser_reservations(
            self.database, "acme:1", "2026-11-19T12:00:00+00:00"), 1800 - 17 * 60)
        self.assertEqual(self.database.execute("SELECT reason FROM worker_compute_refunds").fetchone()[0],
                         "BROWSER_SESSION_ENDED_UNCOMMITTED")


class HandedBackRequestTests(unittest.TestCase):
    """K1 a: a first claim lapsed uncommitted, a second was refused and handed back."""

    def test_it_ends_at_the_hand_back_and_settles_once(self):
        connection = database()
        desktop_adapter.initialize(connection)
        self.addCleanup(connection.close)
        connection.execute("INSERT INTO worker_compute_reservations VALUES (?,?,?,?)",
                           ("application-browser:r1", "2026-11-19", 1800, BOOKED))
        workflow_tasks.track_browser_reservation(connection, "application-browser:r1", "acme:1", 1, 1800, BOOKED)
        # Hand-back at 10:20 (first lease ran out at 10:17): state EXPIRED, claims 1, lease cleared.
        connection.execute(
            "INSERT INTO desktop_requests(request_id,binding,job_key,operation,payload,state,created_at,expires_at,claims,lease_until)"
            " VALUES ('q1','b','acme:1','inspect','{}','EXPIRED','2026-11-19T10:01:00+00:00',"
            "'2026-11-19T10:20:00+00:00',1,NULL)")
        self.assertEqual(workflow_tasks.settle_browser_reservations(
            connection, "acme:1", "2026-11-19T12:00:00+00:00"), 1800 - 20 * 60)
        self.assertEqual(workflow_tasks.settle_browser_reservations(
            connection, "acme:1", "2026-11-19T13:00:00+00:00"), 0)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM worker_compute_refunds").fetchone()[0], 1)


class AnsweringCallTests(unittest.TestCase):
    def setUp(self):
        self.database = database()
        self.addCleanup(self.database.close)

    def call(self, effect):
        result = {"audit": {"calls": []}}
        with patch.object(answering_agent, "run_model", side_effect=effect):
            return answering_agent._booked_call(
                self.database, "answering:", {"job_key": "acme:1"}, {"packet_sha256": "a" * 64}, "prompt", None,
                {"timezone": "UTC"}, {"timeout_seconds": 300, "daily_seconds": 10800}, None, BOOKED, result, "answer")

    def booking(self):
        return self.database.execute("SELECT action_id FROM worker_compute_reservations ORDER BY rowid DESC").fetchone()[0]

    def test_success_failure_and_interruption_all_give_back_the_tail(self):
        self.assertEqual(self.call(["{}"]), "{}")
        self.assertGreater(credited(self.database, self.booking()), 290)
        with self.assertRaises(answering_agent.AnsweringUnavailable):
            self.call(answering_agent.AnsweringUnavailable("ANSWERING_MODEL_FAILED"))
        self.assertTrue(self.booking().endswith("#2"), "the retry booked its own reservation")
        self.assertGreater(credited(self.database, self.booking()), 290)
        with self.assertRaises(KeyboardInterrupt):
            self.call(KeyboardInterrupt())
        self.assertTrue(self.booking().endswith("#3"))
        self.assertGreater(credited(self.database, self.booking()), 290)


class AgentWorkerFailureTests(unittest.TestCase):
    def setUp(self):
        import test_agent_worker
        self.base = test_agent_worker.AgentWorkerTests
        self.base.setUp(self)

    def test_a_failed_investigation_gives_back_its_booking(self):
        self.base.enqueue(self)

        def failing(config, prompt, directory, timeout):
            raise RuntimeError("EXECUTOR_FAILED")

        self.base.cycle(self, execute=failing)
        rows = self.database.execute("SELECT action_id, seconds FROM worker_compute_refunds").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], 10, "the clock did not move, so the whole 10 s booking goes back")


class ReplyWorkerFailureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):   # the fixture's messages predate the reply cutoff
        backlog_fixture.setUpModule()
        cls.addClassCleanup(backlog_fixture.tearDownModule)

    def setUp(self):
        test_reply_worker.ReplyWorkerTests.setUp(self)

    def __getattr__(self, name):   # the base fixture's helpers (enqueue, run_cycle, ...)
        if name.startswith("__"):
            raise AttributeError(name)
        return getattr(test_reply_worker.ReplyWorkerTests, name).__get__(self)

    def test_an_unreadable_answer_gives_back_the_booking_once(self):
        """The worked example: a 10 s booking whose call fails at once credits ~10 s, once."""
        import reply_worker
        self.enqueue()
        with patch.object(reply_worker, "execute_text", side_effect=ValueError("unreadable answer")):
            self.run_cycle()
        booked = [tuple(row) for row in self.database.execute("SELECT action_id, seconds FROM worker_compute_reservations")]
        refunds = [tuple(row) for row in self.database.execute("SELECT action_id, seconds FROM worker_compute_refunds")]
        self.assertEqual(len(booked), 1)
        self.assertEqual([action for action, unused in refunds], [booked[0][0]])
        self.assertGreaterEqual(refunds[0][1], booked[0][1] - 1)


class FinderInterruptionTests(unittest.TestCase):
    def test_an_interrupted_finder_run_gets_its_unused_tail_back(self):
        import linkedin_contacts as lc
        import test_linkedin_contacts as finder
        connection = finder.fixture()
        lc.initialize(connection)
        connection.commit()
        item = lc.schedule(connection, finder.CONFIG, finder.NOW)["due"][0]
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)

        def interrupted(*unused):
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            lc._run_one(Path(directory.name), connection, finder.CONFIG, item, finder.NOW, executor=interrupted)
        row = connection.execute("SELECT seconds FROM worker_compute_refunds WHERE action_id LIKE 'linkedin-contacts:%'").fetchone()
        self.assertIsNotNone(row)
        self.assertGreater(row[0], lc.COMPUTE_SECONDS - 60)


if __name__ == "__main__":
    unittest.main()
