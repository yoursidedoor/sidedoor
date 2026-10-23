"""E8.T3: the license pause and "Refused now" from job children (license_clock, scheduler_control,
scheduler_loop, unipile_port). Offline: every relay call goes to `license_fixtures.FakeRelay`."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import datetime as dt
import os
import sqlite3
import threading
import time
from unittest import mock

import license_clock
import scheduler_control
import scheduler_loop
import setup_steps
import unipile_port
from test_license_clock import KEY, ClockCase, at


class LicensePause(ClockCase):
    def setUp(self):
        super().setUp()
        self.root = self.home / "root"
        (self.root / "state").mkdir(parents=True)
        database = sqlite3.connect(self.root / scheduler_loop.DATABASE)
        scheduler_control.initialize(database)
        database.commit()
        database.close()

    def flags(self):
        return scheduler_loop.pause_state(self.root)

    def sync(self):
        return scheduler_loop.sync_license_pause(self.root, self.home, self.local)

    def loop(self, started):
        clock = {"now": at("2026-10-22T10:00:00")}
        loop = scheduler_loop.Loop(self.root, clock=lambda: clock["now"], spawner=lambda name: started.append(name) or
                                   mock.Mock(poll=lambda: 0), config={"timezone": "UTC"},
                                   license_sync=self.sync,
                                   license_reason=lambda: scheduler_loop.license_pause_reason(self.home, self.local))
        return loop, clock

    def test_expired_sets_the_license_pause_and_a_renewal_clears_it_without_a_restart(self):
        self.activate()
        self.relay.status = "canceled"                       # the signed valid: false
        self.assertEqual(self.heartbeat(), "ok")
        self.assertEqual(license_clock.pause_reason(self.view()), "expired")
        started = []
        loop, clock = self.loop(started)
        self.assertTrue(loop.tick()["paused"])               # paused before the next job
        self.assertEqual(started, [])
        flags = self.flags()
        self.assertEqual(flags["license_pause_reason"], "expired")
        self.assertIsNone(flags["user_paused_at"])            # the user's pause is separate
        # A renewal: a new key (here the same relay answering valid again), no restart.
        self.relay.status = "active"
        state = setup_steps.activate_license(KEY, self.home, "https://relay.test", post=self.relay)
        self.assertEqual(state["steps"]["license"]["status"], "done")
        license_clock.store_signed(self.home, license_clock._load(self.home)[0], self.local)
        clock["now"] += dt.timedelta(minutes=1)
        result = loop.tick()                                  # the next tick syncs and starts work
        self.assertFalse(result["paused"])
        self.assertEqual(sorted(started), ["applications", "research", "run"])
        self.assertIsNone(self.flags()["license_paused_at"])

    def test_grace_spent_pauses_on_the_heartbeat_step_and_the_next_good_check_clears_only_it(self):
        self.activate()
        database = sqlite3.connect(self.root / scheduler_loop.DATABASE)
        scheduler_control.set_user_pause(database, True, reason="dashboard")
        database.close()
        self.relay.mode = "down"
        clock = {"mono": 0.0}
        timer = license_clock.Heartbeat(self.home, "https://relay.test", self.relay, clock=lambda: self.local,
                                        monotonic=lambda: clock["mono"], after_tick=self.sync)
        self.assertEqual(timer.tick(force=True), "offline")
        self.assertIsNone(self.flags()["license_paused_at"])  # inside the 72 hours: nothing paused
        self.true += dt.timedelta(hours=73)
        clock["mono"] += 60
        self.assertIsNone(timer.tick())                       # no check due, yet the pause is synced
        self.assertEqual(self.flags()["license_pause_reason"], "grace_spent")
        self.relay.mode = "ok"
        self.assertEqual(timer.tick(force=True), "ok")        # [Check again] or the hourly retry
        flags = self.flags()
        self.assertIsNone(flags["license_paused_at"])
        self.assertTrue(flags["user_paused_at"])              # still paused by the user

    def test_403_pauses_only_once_valid_until_passes_and_401_before_it_never_does(self):
        self.activate()
        self.relay.mode = "machine"
        self.heartbeat()
        self.sync()
        self.assertIsNone(self.flags()["license_paused_at"])
        self.true += dt.timedelta(hours=73)
        self.heartbeat()
        self.assertEqual(self.view()["action"]["label"], "Activate")  # banner 3 stays the action
        self.sync()
        self.assertEqual(self.flags()["license_pause_reason"], "machine")
        # [Activate] then a good heartbeat clears it.
        self.relay.mode = "ok"
        license_clock.activate(KEY, self.home, "https://relay.test", post=self.relay, now=self.local)
        self.sync()
        self.assertIsNone(self.flags()["license_paused_at"])
        self.relay.mode = "invalid"
        self.heartbeat()
        self.assertIsNone(license_clock.pause_reason(self.view()))  # 401 inside valid_until: banner 2 only

    def test_refused_now_from_a_job_child_wakes_the_dashboards_heartbeat(self):
        self.activate()
        license_clock._last_check["mono"] = None  # the child's own process: no check ran there
        # The child: no heartbeat in its process; a relay 401 LICENSE_INVALID leaves a request.
        with mock.patch.object(license_clock, "_current", None):
            with mock.patch.object(setup_steps, "_home", lambda home=None: self.home):
                unipile_port.refused_now()
        self.assertTrue((self.home / license_clock.CHECK_REQUEST_FILE).exists())
        # The dashboard's heartbeat takes it at its next step and checks at once.
        clock = {"mono": 0.0}
        timer = license_clock.Heartbeat(self.home, "https://relay.test", self.relay, clock=lambda: self.local,
                                        monotonic=lambda: clock["mono"])
        timer.tick(force=True)
        (self.home / license_clock.CHECK_REQUEST_FILE).write_text("x")
        license_clock._last_check["mono"] = None
        count = len(self.relay.calls)
        clock["mono"] += 60
        self.assertEqual(timer.tick(), "ok")                  # not due for a day, but requested
        self.assertEqual(len(self.relay.calls), count + 1)
        self.assertFalse((self.home / license_clock.CHECK_REQUEST_FILE).exists())

    def test_any_relay_call_answering_401_license_invalid_calls_refused_now(self):
        transport = unipile_port.RelayTransport("https://relay.test", KEY)
        error = unipile_port.urllib.error.HTTPError("https://relay.test/accounts", 401, "x", {}, None)
        error.read = lambda: b'{"error": "LICENSE_INVALID"}'
        with mock.patch.object(transport._opener, "open", side_effect=error), \
                mock.patch.object(license_clock, "request_check") as request:
            self.assertEqual(transport("GET", "/accounts"), (401, {"error": "LICENSE_INVALID"}))
        request.assert_called_once()


class ReviewFixes(LicensePause):
    def test_pw2_a_sync_that_cannot_write_still_starts_no_job_while_expired(self):
        self.activate()
        self.relay.status = "canceled"
        self.heartbeat()
        started = []
        loop, _ = self.loop(started)

        def broken():
            raise sqlite3.OperationalError("database is locked")
        loop.license_sync = broken
        self.assertTrue(loop.tick()["paused"])
        self.assertEqual(started, [])
        self.assertIsNone(self.flags()["license_paused_at"])   # nothing was written, still closed
        loop.license_reason = None                              # no file check given: closed
        loop.last_fired.clear()
        self.assertIsNone(loop.tick()["paused"])
        self.assertEqual(started, [])
        loop.license_reason = lambda: 1 / 0                     # an unreadable license: closed too
        loop.last_fired.clear()
        self.assertIsNone(loop.tick()["paused"])
        self.assertEqual(started, [])

    def test_pw7_no_database_is_created_and_the_loop_still_stops_on_the_files(self):
        self.activate()
        self.relay.status = "canceled"
        self.heartbeat()
        os.remove(self.root / scheduler_loop.DATABASE)
        self.assertIsNone(self.sync())
        self.assertFalse((self.root / scheduler_loop.DATABASE).exists())
        started = []
        loop, _ = self.loop(started)
        self.assertTrue(loop.tick()["paused"])
        self.assertEqual(started, [])

    def test_pw4_the_sync_waits_for_an_entitlement_being_stored(self):
        self.activate()
        done = threading.Event()
        with license_clock._write_lock:                          # a renewal writing its files
            worker = threading.Thread(target=lambda: (self.sync(), done.set()))
            worker.start()
            self.assertFalse(done.wait(0.3))
        self.assertTrue(done.wait(5))
        worker.join()

    def test_pw5_pw7_a_refused_request_stays_pending_until_a_check_runs_on_the_timers_clock(self):
        self.activate()
        clock = {"mono": 0.0}
        timer = license_clock.Heartbeat(self.home, "https://relay.test", self.relay, clock=lambda: self.local,
                                        monotonic=lambda: clock["mono"])
        timer.tick(force=True)
        license_clock._last_check["mono"] = None
        (self.home / license_clock.CHECK_REQUEST_FILE).write_text("x")
        clock["mono"] += 10                                       # 10 s after its check: refused
        self.assertIsNone(timer.tick())
        self.assertTrue((self.home / license_clock.CHECK_REQUEST_FILE).exists())
        clock["mono"] += 60
        self.assertEqual(timer.tick(), "ok")
        self.assertFalse((self.home / license_clock.CHECK_REQUEST_FILE).exists())

    def test_pw6_a_child_writes_at_most_one_request_a_minute(self):
        self.activate()
        path = self.home / license_clock.CHECK_REQUEST_FILE
        license_clock._last_check["mono"] = None
        with mock.patch.object(license_clock, "_current", None):
            self.assertTrue(license_clock.request_check(self.home))
            old = time.time() - 30
            os.utime(path, (old, old))
            self.assertTrue(license_clock.request_check(self.home))   # shared, not rewritten
            self.assertAlmostEqual(path.stat().st_mtime, old, delta=1)
            older = time.time() - 120
            os.utime(path, (older, older))
            self.assertTrue(license_clock.request_check(self.home))
            self.assertGreater(path.stat().st_mtime, older + 60)
