"""E5.T1 option B: the standard-library scheduler loop in the Sidedoor process (owner, 2026-10-22).

The five worked examples of the build brief, with a fake clock and a fake spawner (no real job
ever starts): a missed time fires once, a live child is skipped, Pause starts nothing and stops the
applications job between jobs without a signal, Quit stops the loop without touching children, and
the frozen binary re-invokes itself.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import portable
import contextlib
import datetime as dt
import importlib.util
import io
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import log_retention
import orchestrator
import scheduler_control
import scheduler_loop


ZONE = ZoneInfo("America/Los_Angeles")
CONFIG = {"timezone": "America/Los_Angeles", "research_dispatch_interval_seconds": 300, "applications_minute": 30}
DASHBOARD = all(importlib.util.find_spec(name) for name in ("fastapi", "httpx"))


def finished(home):
    """Setup finished and its Start clicked, as `serve --start` needs before it starts the loop."""
    import setup_steps
    setup_steps.update_state(lambda state: state.update(setup_finished_at="2026-10-22T00:00:00Z"), home)
    setup_steps.record(home, "start", "done", "Sidedoor is running.")  # and Setup's own Start succeeded


def at(hour, minute, day=22, month=10):
    return dt.datetime(2026, month, day, hour, minute, tzinfo=ZONE)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class FakeChild:
    def __init__(self, name):
        self.name = name
        self.alive = True
        self.terminate = mock.Mock()
        self.kill = mock.Mock()
        self.send_signal = mock.Mock()

    def poll(self):
        return None if self.alive else 0


class Spawner:
    def __init__(self):
        self.calls = []
        self.children = []

    def __call__(self, name):
        child = FakeChild(name)
        self.calls.append(name)
        self.children.append(child)
        return child

    def finish(self, *names):
        for child in self.children:
            if not names or child.name in names:
                child.alive = False

    def assert_never_signalled(self, case):
        for child in self.children:
            for method in (child.terminate, child.kill, child.send_signal):
                case.assertFalse(method.called, "a child was signalled")


class Base(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.clock = Clock(at(8, 50))
        self.spawner = Spawner()
        self.addCleanup(scheduler_loop.install, None)

    def loop(self, **options):
        options.setdefault("is_paused", lambda: False)
        return scheduler_loop.Loop(self.root, clock=self.clock, spawner=self.spawner, config=CONFIG, **options)


class MissedTimeTests(Base):
    def test_1_a_sleep_from_0850_to_1140_fires_each_job_once(self):
        loop = self.loop()
        self.assertEqual(sorted(loop.tick()["started"]), sorted(scheduler_loop.JOBS))  # first tick: all, as RunAtLoad
        self.spawner.finish()
        self.spawner.calls.clear()
        self.clock.now = at(11, 40)  # the machine slept through 09:00, 09:30, 10:00, 10:30, 11:00, 11:30
        result = loop.tick()
        self.assertEqual(sorted(self.spawner.calls), ["applications", "research", "run"])  # once each, not 2-3 times
        self.assertEqual(result["started"], ["research", "run", "applications"])  # research first
        self.assertEqual(loop.last_fired["run"], at(11, 0).timestamp())
        self.assertEqual(loop.last_fired["applications"], at(11, 30).timestamp())
        self.spawner.finish()
        self.clock.now = at(11, 41)
        self.assertEqual(loop.tick()["started"], [])
        self.assertEqual(len(self.spawner.calls), 3)

    def test_instants_stay_on_local_minutes_across_daylight_saving(self):
        for now in (at(14, 7), at(14, 7, day=3, month=11)):  # PDT and PST
            instants = scheduler_loop.latest_instants(now, CONFIG)
            self.assertEqual(instants["run"], now.replace(minute=0).timestamp())
            self.assertEqual(instants["applications"], (now.replace(minute=30) - dt.timedelta(hours=1)).timestamp())
            self.assertEqual(instants["research"], now.replace(minute=5).timestamp())


class OverlapTests(Base):
    def test_2_a_live_applications_child_is_skipped_and_the_hourly_job_still_starts(self):
        self.clock.now = at(10, 30)
        loop = self.loop()
        loop.tick()
        self.spawner.finish("research", "run")  # applications (started 10:30) is still alive
        self.clock.now = at(11, 0)
        self.assertIn("run", loop.tick()["started"])  # the 11:00 hourly cycle starts on time
        self.spawner.finish("research", "run")
        self.clock.now = at(11, 30)
        result = loop.tick()
        self.assertEqual(result["skipped_alive"], ["applications"])
        self.assertNotIn("applications", result["started"])
        self.assertEqual(self.spawner.calls.count("applications"), 1)
        self.spawner.finish()
        self.clock.now = at(11, 31)  # the skipped 11:30 instant is spent, as launchd's skip was
        self.assertNotIn("applications", loop.tick()["started"])
        self.spawner.assert_never_signalled(self)


class PauseTests(Base):
    def setUp(self):
        super().setUp()
        (self.root / "state").mkdir()
        (self.root / "reports").mkdir()
        self.database = orchestrator.connect(self.root)
        self.addCleanup(self.database.close)

    def should_stop_now(self):
        """What the applications job's should_stop answers before its next job."""
        seen = []

        def cycle(root, config, database, only=None, should_stop=None):
            seen.append(should_stop())
            return {"state": "COMPLETE"}
        with mock.patch.object(orchestrator, "research_holds_turn", return_value=False), \
                mock.patch.object(orchestrator, "application_cycle", side_effect=cycle), \
                contextlib.redirect_stdout(io.StringIO()):
            orchestrator.applications_service(self.root, {}, self.database)
        return seen[0]

    def test_3_pause_starts_nothing_signals_nothing_and_stops_applications_between_jobs(self):
        self.clock.now = at(10, 30)
        loop = scheduler_loop.Loop(self.root, clock=self.clock, spawner=self.spawner, config=CONFIG)  # real flags
        loop.tick()
        self.spawner.finish("research", "run")
        self.assertFalse(self.should_stop_now())
        scheduler_control.set_user_pause(self.database, True, reason="test")
        self.assertTrue(scheduler_loop.paused(self.root))
        self.clock.now = at(11, 0)
        result = loop.tick()
        self.assertTrue(result["paused"])
        self.assertEqual(result["started"], [])
        self.assertEqual(len(self.spawner.calls), 3)
        self.spawner.assert_never_signalled(self)
        self.assertTrue(self.should_stop_now())  # the running applications job stops before its next job
        scheduler_control.set_user_pause(self.database, False)
        self.assertFalse(self.should_stop_now())
        self.clock.now = at(11, 1)
        self.assertEqual(loop.tick()["started"], ["research", "run"])  # the 11:00 cycle, once, after Resume
        self.spawner.assert_never_signalled(self)

    def test_the_license_pause_is_a_second_flag(self):
        scheduler_control.set_license_pause(self.database, True, reason="license")
        self.assertTrue(scheduler_loop.paused(self.root))
        scheduler_control.set_user_pause(self.database, False)
        self.assertTrue(scheduler_loop.paused(self.root), "Resume does not clear the license pause")
        scheduler_control.set_license_pause(self.database, False)
        self.assertFalse(scheduler_loop.paused(self.root))

    def test_no_database_yet_is_not_paused(self):
        self.assertFalse(scheduler_loop.paused(self.root / "elsewhere"))


class QuitTests(Base):
    def test_4_quit_stops_the_loop_and_leaves_children_alone(self):
        loop = self.loop(tick_seconds=0.01)
        loop.start()
        deadline = time.monotonic() + 5
        while not self.spawner.calls and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(loop.running())
        loop.stop()
        self.assertFalse(loop.running())
        self.assertFalse(loop._thread.is_alive())
        self.assertTrue(all(child.alive for child in self.spawner.children))
        self.spawner.assert_never_signalled(self)

    def test_children_start_in_their_own_session(self):
        with mock.patch.object(scheduler_loop.subprocess, "Popen") as popen:
            scheduler_loop.spawn("applications", self.root)
        arguments, options = popen.call_args
        self.assertEqual(arguments[0][-2:], ["job", "applications"])
        if scheduler_loop.os.name != "nt":
            self.assertTrue(options["start_new_session"])
        self.assertTrue((self.root / "logs/applications.stdout.log").exists())

    @unittest.skipUnless(DASHBOARD, "dashboard packages (fastapi, httpx) are not installed")
    def test_4_quitting_the_dashboard_stops_the_loop_without_killing_a_child(self):
        import dashboard
        loop = self.loop(tick_seconds=0.01)
        spawned = threading.Event()
        original = loop.spawner

        def spawner(name):
            spawned.set()
            return original(name)
        loop.spawner = spawner

        def server_runs(*args, **kwargs):  # the dashboard runs until the user quits
            self.assertTrue(spawned.wait(5))
        finished(self.root)  # serve --start waits for Setup (owner, 2026-10-22; tests/test_login_item.py)
        with mock.patch.object(dashboard, "bind", return_value=mock.Mock()), \
                mock.patch.object(dashboard, "running_here", return_value=False), \
                mock.patch.object(dashboard.uvicorn.Server, "run", side_effect=server_runs), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(dashboard.serve(open_browser=False, home=self.root, start=True, scheduler=loop), 0)
        self.assertFalse(loop.running())
        self.assertIsNone(scheduler_loop.current(), "a stopped dashboard leaves no scheduler to start")
        self.assertFalse(scheduler_loop.start())
        self.assertTrue(self.spawner.children)
        self.assertTrue(all(child.alive for child in self.spawner.children))
        self.spawner.assert_never_signalled(self)

    @unittest.skipUnless(DASHBOARD, "dashboard packages (fastapi, httpx) are not installed")
    def test_serve_without_start_waits_for_the_start_button(self):
        import dashboard
        loop = self.loop()
        with mock.patch.object(dashboard, "bind", return_value=mock.Mock()), \
                mock.patch.object(dashboard.uvicorn.Server, "run",
                                  side_effect=lambda *a, **k: self.assertFalse(scheduler_loop.running())), \
                contextlib.redirect_stdout(io.StringIO()):
            dashboard.serve(open_browser=False, home=self.root, scheduler=loop)
        self.assertEqual(self.spawner.calls, [])


@unittest.skipUnless(DASHBOARD, "dashboard packages (fastapi, httpx) are not installed")
class DashboardRouteTests(Base):
    def test_start_pause_and_resume(self):
        import dashboard
        from fastapi.testclient import TestClient
        import setup_steps
        secret, port = "s" * 43, 47823
        origin = {"origin": f"http://127.0.0.1:{port}"}
        (self.root / "state").mkdir()
        loop = scheduler_loop.install(self.loop(tick_seconds=60))
        self.addCleanup(loop.stop)
        app = dashboard.create_app(secret, port, home=self.root, root=self.root, config={}, browser=mock.Mock())
        client = TestClient(app, base_url=f"http://127.0.0.1:{port}", follow_redirects=False)
        client.get("/", params={"t": secret})
        self.assertFalse(setup_steps.scheduler_running(self.root))
        client.post("/start", headers=origin)
        self.assertTrue(setup_steps.scheduler_running(self.root))
        self.assertEqual(setup_steps.load_state(self.root)["steps"]["start"]["status"], "done")
        self.assertEqual(client.post("/pause", headers=origin).status_code, 200)  # overview.html is another builder's
        self.assertTrue(scheduler_loop.status(self.root)["paused"])
        self.assertEqual(client.post("/resume", headers=origin).status_code, 200)
        self.assertFalse(scheduler_loop.status(self.root)["paused"])
        self.assertTrue(scheduler_loop.status(self.root)["running"])


class HourlyNeverSkippedTests(Base):
    """E6: a research instant on the hourly :00 would take state/runner.lock first and make the
    hourly cycle skip; only 9:00 (day_start_hour) keeps both, research first."""

    def day(self, interval):
        """Ticks every 30 s for 24 h from 00:00:10; returns (hours the hourly cycle started,
        hours research shared its tick, research start times, applications starts)."""
        config = dict(CONFIG, research_dispatch_interval_seconds=interval)
        clock, spawner = Clock(at(0, 0, day=22).replace(second=10)), Spawner()
        loop = scheduler_loop.Loop(self.root, clock=clock, spawner=spawner, is_paused=lambda: False, config=config)
        loop.tick()
        spawner.finish()
        run_hours, shared, research = [], [], []
        applications = 0
        for step in range(1, 24 * 120 + 1):
            clock.now = at(0, 0, day=22).replace(second=10) + dt.timedelta(seconds=30 * step)
            started = loop.tick()["started"]
            local = clock.now.astimezone(ZONE)
            if "run" in started:
                self.assertEqual(local.minute, 0)
                run_hours.append(local.hour)
                if "research" in started:
                    shared.append(local.hour)
            if "research" in started:
                research.append(clock.now)
            applications += started.count("applications")
            spawner.finish()
        return run_hours, shared, research, applications

    def test_over_24_hours_run_starts_every_hour_and_shares_a_tick_with_research_only_at_9(self):
        run_hours, shared, research, applications = self.day(300)
        self.assertEqual(len(run_hours), 24)
        self.assertEqual(shared, [9])
        self.assertEqual(applications, 24)
        self.assertEqual(len(research), 24 * 12)  # every 5 min; a :00 one moved to :01, none lost

    def test_e10_hour_multiples_still_give_research_its_turn(self):
        for interval in (3600, 7200):
            run_hours, shared, research, _ = self.day(interval)
            self.assertEqual(len(run_hours), 24, interval)
            self.assertIn(shared, ([], [9]), interval)  # only the 9:00 instant, research first
            self.assertEqual(len(research), 24 * 3600 // interval, interval)
            gaps = [(later - earlier).total_seconds() for earlier, later in zip(research, research[1:])]
            self.assertTrue(all(interval - 120 <= gap <= interval + 120 for gap in gaps), (interval, gaps))


class ClockCorrectionTests(Base):
    """E1: a wall clock that went back must not leave every job waiting for a time already past."""

    def test_a_year_fast_clock_corrected_resumes_at_the_next_hour(self):
        self.clock.now = dt.datetime(2027, 10, 22, 17, 0, 10, tzinfo=dt.timezone.utc)
        loop = self.loop()
        loop.tick()
        self.spawner.finish()
        self.clock.now = dt.datetime(2026, 10, 22, 17, 0, 10, tzinfo=dt.timezone.utc)
        self.assertEqual(loop.tick()["started"], [])  # the correction itself fires nothing
        self.assertIn("clock moved back", loop.last_error)
        self.clock.now = dt.datetime(2026, 10, 22, 18, 0, 10, tzinfo=dt.timezone.utc)
        self.assertIn("run", loop.tick()["started"])

    def test_two_hours_fast_then_corrected_loses_no_cycle_after_the_current_one(self):
        self.clock.now = at(12, 0)  # true time 10:00
        loop = self.loop()
        loop.tick()
        self.spawner.finish()
        self.clock.now = at(10, 5)  # corrected
        loop.tick()
        self.spawner.finish()
        for hour in (11, 12, 13):
            self.clock.now = at(hour, 0)
            self.assertIn("run", loop.tick()["started"], hour)
            self.spawner.finish()


class BadConfigTests(Base):
    """E2: one bad value affects only its own job."""

    def test_each_bad_value_leaves_research_and_run_running(self):
        for bad in ({"applications_minute": None}, {"applications_minute": "x"}, {"timezone": "PST"}):
            spawner = Spawner()
            loop = scheduler_loop.Loop(self.root, clock=Clock(at(10, 31)), spawner=spawner,
                                       is_paused=lambda: False, config=dict(CONFIG, **bad))
            self.assertEqual(loop.tick()["started"], ["research", "run", "applications"], bad)
            self.assertIn(next(iter(bad)), loop.last_error)
        instants = scheduler_loop.latest_instants(at(10, 31), dict(CONFIG, applications_minute="x"))
        self.assertEqual(instants["applications"], at(10, 30).timestamp())  # the default minute 30
        instants = scheduler_loop.latest_instants(at(10, 31), dict(CONFIG, timezone="PST"))
        self.assertEqual(instants["run"], at(10, 0).timestamp())  # the default zone


class LastErrorTests(Base):
    """E11: last_error says what is wrong now, not what was wrong once."""

    def test_a_fixed_config_value_clears_at_once(self):
        config = dict(CONFIG, applications_minute="x")
        loop = scheduler_loop.Loop(self.root, clock=self.clock, spawner=self.spawner, is_paused=lambda: False, config=config)
        loop.tick()
        self.assertIn("applications_minute", loop.last_error)
        config["applications_minute"] = 30
        self.clock.now = at(8, 51)
        loop.tick()
        self.assertIsNone(loop.last_error)

    def test_a_clock_correction_shows_for_an_hour(self):
        self.clock.now = at(12, 0)
        loop = self.loop()
        loop.tick()
        self.spawner.finish()
        self.clock.now = at(10, 5)
        loop.tick()
        self.assertIn("clock moved back", loop.last_error)
        self.clock.now = at(11, 4)
        self.assertIn("clock moved back", loop.last_error)
        self.clock.now = at(11, 6)
        loop.tick()
        self.assertIsNone(loop.last_error)

    def test_a_failed_start_clears_when_that_job_starts(self):
        loop = self.loop()
        failing = {"on": True}

        def spawner(name):
            if failing["on"] and name == "applications":
                raise OSError("no binary")
            return self.spawner(name)
        loop.spawner = spawner
        with mock.patch.object(scheduler_loop.traceback, "print_exc"):
            loop.tick()
        self.assertIn("start applications failed", loop.last_error)
        failing["on"] = False
        self.clock.now = at(8, 51)
        loop.tick()
        self.assertIsNone(loop.last_error)


class LoopRecordTests(Base):
    def test_e4_a_base_exception_stops_the_loop_with_its_reason(self):
        class Fatal(BaseException):
            pass

        def spawner(name):
            raise Fatal("boom")
        loop = scheduler_loop.install(self.loop(tick_seconds=0.01))
        loop.spawner = spawner
        loop.start()
        loop._thread.join(5)
        self.assertFalse(loop._thread.is_alive())
        status = scheduler_loop.status(self.root)
        self.assertFalse(status["running"])
        self.assertIn("scheduler stopped: Fatal: boom", status["last_error"])

    def test_e5_a_child_exiting_non_zero_is_recorded_per_job(self):
        loop = scheduler_loop.install(self.loop())
        loop.tick()
        for child in self.spawner.children:
            child.alive = False
        self.spawner.children[2].poll = lambda: 3  # applications crashed
        self.clock.now = at(8, 51)
        loop.tick()
        self.assertEqual(scheduler_loop.status(self.root)["job_failures"],
                         {"applications": {"exit_code": 3, "at": at(8, 51).isoformat(timespec="seconds"),
                                           "started": at(8, 50).isoformat(timespec="seconds")}})

    def test_ov_a_failure_carries_its_childs_start_not_a_passed_over_instant(self):
        # A round still running at the next :00 is passed over (last_fired moves on) and exits
        # non-zero later: its failure names the start of THAT child, which Overview compares with
        # the runs row (overview_page._crashed), never the passed-over instant.
        loop = scheduler_loop.install(self.loop())
        loop.tick()                                   # 8:50: the 8:00 run starts
        self.clock.now = at(9, 1)
        self.assertIn("run", loop.tick()["skipped_alive"])
        self.assertEqual(loop.last_fired["run"], at(9, 0).timestamp())
        self.spawner.children[1].alive = False
        self.spawner.children[1].poll = lambda: 1
        self.clock.now = at(9, 20)
        loop.tick()
        self.assertEqual(loop.job_failures["run"]["started"], at(8, 50).isoformat(timespec="seconds"))

    def test_o26_a_clean_exit_clears_the_jobs_failure(self):
        loop = self.loop()

        def ended(code):
            child = FakeChild("research")
            child.poll = lambda: code
            return child
        for code, expected in ((1, {"exit_code": 1}), (2, {"exit_code": 2}), (0, None)):
            loop.children["research"] = ended(code)
            loop._reap(at(9, code))
            failure = loop.job_failures.get("research")
            self.assertEqual(failure and {"exit_code": failure["exit_code"]}, expected, code)
        self.assertEqual(loop.job_failures, {})
        self.spawner.assert_never_signalled(self)

    def test_e9_a_failing_start_prints_one_traceback_an_hour_per_job(self):
        def spawner(name):
            raise OSError("no binary")
        loop = self.loop()
        loop.spawner = spawner
        with mock.patch.object(scheduler_loop.traceback, "print_exc") as printed:
            for minute in range(10):
                self.clock.now = at(8, 50 + minute)
                loop.tick()
        self.assertEqual(printed.call_count, 3)  # one per job
        self.assertIn("no binary", loop.last_error)

    def test_e9_child_logs_are_names_retention_already_rotates(self):
        for stem in scheduler_loop.LOG_NAMES.values():
            for stream in ("stdout", "stderr"):
                name = "%s.%s.log" % (stem, stream)
                self.assertIn(name, log_retention.STREAMS)
                self.assertTrue(log_retention.ARCHIVE_NAME.fullmatch(name + ".retained." + "0" * 20 + "." + "a" * 32))


@unittest.skipUnless(DASHBOARD, "dashboard packages (fastapi, httpx) are not installed")
class ServeStartTests(Base):
    SECRET, PORT = "s" * 43, 47823

    def instance(self):
        """A fake running Sidedoor: its app, served through the TestClient instead of a socket."""
        import dashboard
        from fastapi.testclient import TestClient
        loop = scheduler_loop.install(self.loop(tick_seconds=60))
        self.addCleanup(loop.stop)
        app = dashboard.create_app(self.SECRET, self.PORT, home=self.root, root=self.root, config={}, browser=mock.Mock())
        client = TestClient(app, base_url=f"http://127.0.0.1:{self.PORT}", follow_redirects=False)

        class Connection:
            def __init__(self, host, port, timeout=None):
                self.response = None

            def request(self, method, path, headers=None):
                self.response = client.request(method, path, headers=headers)

            def getresponse(self):
                response = self.response
                return mock.Mock(status=response.status_code, read=lambda size: response.content)

            def close(self):
                pass
        return loop, client, Connection

    def test_e3_serve_start_starts_the_loop_of_the_sidedoor_already_running(self):
        import dashboard
        loop, client, Connection = self.instance()
        finished(self.root)
        with mock.patch.object(dashboard, "bind", side_effect=OSError(48, "in use")), \
                mock.patch.object(dashboard, "running_here", return_value=True), \
                mock.patch.object(dashboard, "token", return_value=self.SECRET), \
                mock.patch.object(dashboard.http.client, "HTTPConnection", Connection), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(loop.running())
            self.assertEqual(dashboard.serve(open_browser=False, home=self.root, start=True), 0)
        self.assertTrue(loop.running())

    def test_e3_the_start_call_needs_the_token_and_a_fresh_nonce(self):
        import dashboard
        loop, client, _ = self.instance()
        origin = {"origin": f"http://127.0.0.1:{self.PORT}"}
        nonce = "a" * 32
        self.assertEqual(client.post(dashboard.START_PATH, params={"n": nonce, "p": dashboard.start_proof("x" * 43, nonce)},
                                     headers=origin).status_code, 403)
        self.assertFalse(loop.running())
        good = {"n": nonce, "p": dashboard.start_proof(self.SECRET, nonce)}
        self.assertEqual(client.post(dashboard.START_PATH, params=good).status_code, 403)  # no Origin
        self.assertEqual(client.post(dashboard.START_PATH, params=good, headers=origin).status_code, 200)
        self.assertTrue(loop.running())
        self.assertEqual(client.post(dashboard.START_PATH, params=good, headers=origin).status_code, 403)  # replay

    def test_e7_a_dashboard_that_fails_to_start_leaves_no_loop_running(self):
        import dashboard
        loop = self.loop(tick_seconds=0.01)
        with mock.patch.object(dashboard, "bind", return_value=mock.Mock()), \
                mock.patch.object(dashboard, "create_app", side_effect=RuntimeError("bad app")), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                dashboard.serve(open_browser=False, home=self.root, start=True, scheduler=loop)
        self.assertFalse(loop.running())
        self.assertEqual(self.spawner.calls, [])
        self.assertIsNone(scheduler_loop.current())


class FrozenBinaryTests(unittest.TestCase):
    def test_5_job_command_re_invokes_this_program(self):
        with mock.patch.object(sys, "frozen", True, create=True):
            self.assertEqual(scheduler_loop.job_command("run"), [sys.executable, "job", "run"])
        with mock.patch.object(sys, "frozen", False, create=True):
            self.assertEqual(scheduler_loop.job_command("run"),
                             [sys.executable, *portable.UTF8_FLAGS, str(Path(orchestrator.__file__).resolve()), "job", "run"])
        with self.assertRaises(ValueError):
            scheduler_loop.job_command("serve")

    def test_job_subcommands_enter_the_existing_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "state").mkdir()
            (root / "config.json").write_text('{"timezone": "America/Los_Angeles"}')
            with mock.patch.dict(os.environ, {"SIDEDOOR_HOME": str(root)}), \
                    mock.patch.object(orchestrator, "applications_service", return_value={"state": "COMPLETE"}) as job, \
                    mock.patch.object(orchestrator.log_retention, "print_result"):
                self.assertEqual(orchestrator.main(["job", "applications"]), 0)
            job.assert_called_once()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(orchestrator.main(["job", "dashboard"]), 2)


if __name__ == "__main__":
    unittest.main()
