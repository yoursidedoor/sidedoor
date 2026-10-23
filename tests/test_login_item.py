"""E5.T2 start at login (macOS and Windows; Linux later, owner 2026-10-22).

Every OS call goes through a fake runner: no launchctl, schtasks or other program runs, nothing is
written under the real ~/Library/LaunchAgents (checked before and after every test), and any use of
the real runner fails the test.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import portable
import contextlib
import importlib.util
import io
import json
import os
import plistlib
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import log_retention
import login_item
import scheduler_loop
import setup_steps
import user_profile

DASHBOARD = all(importlib.util.find_spec(name) for name in ("fastapi", "httpx"))
if DASHBOARD:
    from fastapi.testclient import TestClient
    import dashboard

ROOT = Path(__file__).resolve().parent.parent
REAL_PLIST = Path.home() / "Library" / "LaunchAgents" / (login_item.LABEL + ".plist")
PORT = 47823
SECRET = "test-token-value"
ORIGIN = {"origin": f"http://127.0.0.1:{PORT}"}
HTMX = dict(ORIGIN, **{"hx-request": "true"})


class FakeRunner:
    """Records every argv; answers from `answers` (first matching prefix), else (0, "")."""

    def __init__(self, answers=()):
        self.calls = []
        self.answers = list(answers)

    def __call__(self, argv):
        self.calls.append(list(argv))
        for prefix, answer in self.answers:
            if list(argv[:len(prefix)]) == list(prefix):
                return answer(argv) if callable(answer) else answer
        return 0, ""


def real_runner_used(argv):
    raise AssertionError("the real runner ran: %r" % (argv,))


class Base(unittest.TestCase):
    def setUp(self):
        self.real_plist_before = REAL_PLIST.exists() and REAL_PLIST.read_bytes()
        self.directory = tempfile.TemporaryDirectory()
        base = Path(self.directory.name)
        self.user_home, self.root, self.home = base / "user", base / "root", base / "home"
        for path in (self.user_home, self.root, self.home):
            path.mkdir()
        self.runner = FakeRunner()
        for name, value in (("RUNNER", self.runner), ("USER_HOME", self.user_home), ("UID", 501),
                            ("PLATFORM", "darwin"), ("_run", real_runner_used)):
            patcher = mock.patch.object(login_item, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target in ("run", "Popen", "call", "check_call", "check_output"):
            patcher = mock.patch.object(subprocess, target, side_effect=AssertionError("a real process started"))
            patcher.start()
            self.addCleanup(patcher.stop)
        root = mock.patch.object(scheduler_loop, "ROOT", self.root)
        root.start()
        self.addCleanup(root.stop)
        environment = mock.patch.dict(os.environ, {user_profile.HOME_VARIABLE: str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop(login_item.MARKER, None)

    def tearDown(self):
        self.directory.cleanup()
        after = REAL_PLIST.exists() and REAL_PLIST.read_bytes()
        self.assertEqual(after, self.real_plist_before, "the real ~/Library/LaunchAgents entry changed")

    @property
    def plist(self):
        return self.user_home / "Library" / "LaunchAgents" / "com.yoursidedoor.sidedoor.plist"


class MacTest(Base):
    def test_1_enable_writes_the_plist_and_bootstraps_disable_boots_out_and_removes(self):
        result = login_item.enable()
        self.assertTrue(result["ok"] and result["enabled"])
        written = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(written["Label"], "com.yoursidedoor.sidedoor")
        arguments = written["ProgramArguments"]
        self.assertEqual(arguments[:2], ["/bin/sh", "-c"])
        self.assertEqual(arguments[4:6], [str(self.home), str(self.home / "logs")])  # launcher: home, logs folder
        self.assertEqual(arguments[arguments.index("serve"):],
                         ["serve", "--start", "--no-browser", "--at-login", "--home", str(self.home)])
        self.assertEqual(written["KeepAlive"], {"SuccessfulExit": False})
        self.assertTrue(written["RunAtLoad"])
        self.assertTrue((self.home / "logs").is_dir(), "launchd needs the log folder to exist")
        self.assertEqual(self.runner.calls, [["launchctl", "enable", "gui/501/com.yoursidedoor.sidedoor"],
                                             ["launchctl", "bootstrap", "gui/501", str(self.plist)]])
        self.assertTrue(login_item.status()["enabled"])

        self.runner.calls.clear()
        result = login_item.disable()
        self.assertTrue(result["ok"])
        self.assertFalse(result["enabled"])
        self.assertEqual(self.runner.calls, [["launchctl", "bootout", "gui/501/com.yoursidedoor.sidedoor"]])
        self.assertFalse(self.plist.exists())
        self.assertFalse(login_item.status()["enabled"])

    def test_2_enable_twice_is_idempotent_already_loaded_is_success(self):
        self.assertTrue(login_item.enable()["ok"])
        self.runner.answers = [(["launchctl", "bootstrap"], (5, "Bootstrap failed: 5: Input/output error\n"
                                                                "service already loaded"))]
        result = login_item.enable()
        self.assertTrue(result["ok"] and result["enabled"])
        self.assertEqual(list(self.plist.parent.iterdir()), [self.plist])

    def test_already_loaded_without_the_word_is_read_from_launchctl_print(self):
        self.runner.answers = [(["launchctl", "bootstrap"], (5, "Bootstrap failed: 5: Input/output error")),
                               (["launchctl", "print"], (0, "state = running"))]
        self.assertTrue(login_item.enable()["ok"])
        self.assertTrue(self.plist.exists())

    def test_a_refused_bootstrap_says_so_and_leaves_it_off(self):
        self.runner.answers = [(["launchctl", "bootstrap"], (5, "Bootstrap failed: 5: Input/output error")),
                               (["launchctl", "print"], (113, "Could not find service"))]
        result = login_item.enable()
        self.assertFalse(result["ok"] or result["enabled"])
        self.assertIn("System Settings > General > Login Items", result["message"])
        self.assertIn("Sidedoor still works when you open it", result["message"])
        self.assertFalse(self.plist.exists())

    def test_turning_it_off_inside_the_login_started_process_does_not_stop_it(self):
        login_item.enable()
        self.runner.calls.clear()
        with mock.patch.dict(os.environ, {login_item.MARKER: "1"}):
            self.assertTrue(login_item.disable()["ok"])
        self.assertEqual(self.runner.calls, [], "no bootout: it would stop the Sidedoor the user is using")
        self.assertFalse(self.plist.exists())

    def test_4_frozen_bundle_registers_itself(self):
        tail = ["--start", "--no-browser", "--at-login", "--home", str(self.home)]
        with mock.patch.object(sys, "frozen", True, create=True):
            self.assertEqual(login_item.plist()["ProgramArguments"][6:], [sys.executable, "serve"] + tail)
            self.assertIn("<Command>%s</Command><Arguments>supervise --start" % sys.executable, login_item.task_xml(user=""))
        with mock.patch.object(sys, "frozen", False, create=True):
            self.assertEqual(login_item.plist()["ProgramArguments"][6:],
                             [sys.executable, *portable.UTF8_FLAGS, str(scheduler_loop.ENTRY), "serve"] + tail)

    @unittest.skipIf(os.name == "nt", "macOS plist PATH (':'-joined): a Windows path's drive colon splits it")
    def test_L1_the_entry_has_a_path_with_homebrew_and_the_saved_codex_and_its_node(self):
        bin_dir = self.user_home / "nvm" / "bin"
        bin_dir.mkdir(parents=True)
        for name in ("codex", "node"):
            (bin_dir / name).write_text("")
        setup_steps.update_state(lambda state: state.update(codex_path=str(bin_dir / "codex")), self.home)
        setup_steps.start_at_login(True, self.home)
        folders = plistlib.loads(self.plist.read_bytes())["EnvironmentVariables"]["PATH"].split(":")
        self.assertIn("/opt/homebrew/bin", folders)
        self.assertIn(str(bin_dir), folders)
        self.assertLess(folders.index(str(bin_dir)), folders.index("/usr/bin"), "Codex's node comes first")

    def test_L3_a_restart_after_unticking_inside_the_session_exits_0_at_once(self):
        with mock.patch.dict(os.environ, {login_item.MARKER: "1"}):
            self.assertTrue(login_item.unregistered_login_launch())
            login_item.enable()
            self.assertFalse(login_item.unregistered_login_launch())
        self.assertFalse(login_item.unregistered_login_launch(), "a manual open is never stopped")

    def test_the_label_is_never_the_harnesss(self):
        # Sidedoor's own prefix, never the harness's labels.
        self.assertTrue(login_item.LABEL.startswith("com.yoursidedoor."))
        self.assertFalse((ROOT / "install.py").exists(), "the product never installs the three old launchd jobs")


class OtherSystemsTest(Base):
    def test_3_linux_is_not_available_yet_and_runs_nothing(self):
        with mock.patch.object(login_item, "PLATFORM", "linux"):
            result = login_item.enable()
            self.assertEqual(login_item.status(), {"enabled": False, "supported": False, "disabled_by_system": False,
                                                   "stale": False})
        self.assertFalse(result["ok"])
        self.assertEqual(result["message"],
                         "Start at login isn't available on this system yet; Sidedoor still works when you open it.")
        self.assertEqual(self.runner.calls, [])
        self.assertFalse((self.user_home / ".config").exists())

    def test_windows_creates_queries_and_deletes_the_logon_task(self):
        seen = {}

        def create(argv):
            path = Path(argv[argv.index("/XML") + 1])
            seen["xml"] = path.read_bytes().decode("utf-16")
            seen["path"] = path
            return 0, "SUCCESS"
        self.runner.answers = [(["schtasks", "/Create"], create)]
        with mock.patch.object(login_item, "PLATFORM", "win32"):
            self.assertTrue(login_item.enable()["ok"])
            self.assertTrue(login_item.status()["enabled"])
            self.assertTrue(login_item.disable()["ok"])
        self.assertEqual(self.runner.calls[0][:3], ["schtasks", "/Create", "/TN"])
        self.assertEqual(self.runner.calls[1], ["schtasks", "/Query", "/TN", "Sidedoor", "/XML"])
        self.assertEqual(self.runner.calls[2], ["schtasks", "/Delete", "/TN", "Sidedoor", "/F"])
        self.assertFalse(seen["path"].exists(), "the task file is a temporary file, removed after")
        self.assertIn("<LogonTrigger>", seen["xml"])
        self.assertIn("<RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure>", seen["xml"])
        self.assertIn("supervise --start --no-browser --at-login --home", seen["xml"])
        self.assertNotIn("cmd.exe", seen["xml"], "no console window at logon (item 2)")


class LogsTest(Base):
    def test_the_login_logs_are_retention_handled(self):
        self.assertIn("sidedoor.stdout.log", log_retention.STREAMS)
        self.assertIn("sidedoor.stderr.log", log_retention.STREAMS)
        self.assertTrue(log_retention.ARCHIVE_NAME.fullmatch("sidedoor.stdout.log.retained." + "1" * 20 + "." + "a" * 32))

    @unittest.skipIf(os.name == "nt", "POSIX only: on Windows `supervise` rotates the login logs (login_item.sweep_own_logs)")
    def test_the_login_started_process_sweeps_its_own_logs_at_start(self):
        with mock.patch.object(log_retention, "retain", return_value={"state": "COMPLETE"}) as retain:
            self.assertIsNone(login_item.sweep_own_logs(self.root))
            retain.assert_not_called()
            with mock.patch.dict(os.environ, {login_item.MARKER: "1"}):
                login_item.sweep_own_logs(self.root)
            retain.assert_called_once_with(self.root)


class QuitTest(Base):
    def test_sigterm_while_serving_is_a_normal_stop(self):
        with login_item.clean_stop():
            handler = signal.getsignal(signal.SIGTERM)
            with self.assertRaises(KeyboardInterrupt):
                handler(signal.SIGTERM, None)
        self.assertIsNot(signal.getsignal(signal.SIGTERM), handler)

    @unittest.skipUnless(DASHBOARD, "dashboard packages (fastapi, httpx) are not installed")
    def test_serve_returns_0_on_quit_and_a_crash_exits_non_zero(self):
        loop = scheduler_loop.Loop(self.root, spawner=lambda name: self.fail("no job starts"), is_paused=lambda: True)

        def uvicorn_after_sigterm(*args, **kwargs):  # what uvicorn does: shut down, raise the signal again
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        for stop in (uvicorn_after_sigterm, KeyboardInterrupt):
            with mock.patch.object(dashboard, "bind", return_value=mock.Mock()), \
                    mock.patch.object(dashboard.uvicorn.Server, "run", side_effect=stop), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(dashboard.serve(open_browser=False, home=self.home, scheduler=loop), 0)
        import orchestrator
        with mock.patch.object(dashboard, "bind", return_value=mock.Mock()), \
                mock.patch.object(dashboard.uvicorn.Server, "run", side_effect=RuntimeError("boom")), \
                mock.patch.object(sys, "argv", ["sidedoor", "serve", "--no-browser"]), \
                mock.patch.object(dashboard, "make_tray", return_value=None), \
                mock.patch.object(orchestrator.portable, "ensure_utf8_mode", return_value=None), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as stopped:
                orchestrator.cli()   # in this process (on Windows the UTF-8 re-run is patched out)
        self.assertEqual(stopped.exception.code, 1)
        self.assertIsNone(scheduler_loop.current())

    @unittest.skipUnless(DASHBOARD, "dashboard packages (fastapi, httpx) are not installed")
    def test_L3_serve_exits_0_before_binding_when_the_entry_was_removed(self):
        with mock.patch.dict(os.environ, {login_item.MARKER: "1"}), \
                mock.patch.object(dashboard, "bind", side_effect=AssertionError("must not bind")):
            self.assertEqual(dashboard.serve(open_browser=False, home=self.home, start=True), 0)

    @unittest.skipUnless(DASHBOARD, "dashboard packages (fastapi, httpx) are not installed")
    def test_L4_sigterm_before_serving_is_still_a_normal_stop(self):
        def sigterm_now(root=None):
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        with mock.patch.object(dashboard, "bind", return_value=mock.Mock()), \
                mock.patch.object(login_item, "sweep_own_logs", sigterm_now), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(dashboard.serve(open_browser=False, home=self.home), 0)
        self.assertIsNone(scheduler_loop.current())

    @unittest.skipUnless(DASHBOARD, "dashboard packages (fastapi, httpx) are not installed")
    def test_owner2_item6_serve_start_waits_for_setups_start(self):
        setup_steps.update_state(lambda state: state.update(setup_finished_at="2026-10-22T00:00:00Z"), self.home)
        for done in (False, True):  # steps 1-9 done throughout; only Setup's Start (step 11) differs
            if done:
                setup_steps.record(self.home, "start", "done", "Sidedoor is running.")
            loop = mock.Mock()
            with mock.patch.object(dashboard, "bind", return_value=mock.Mock()), \
                    mock.patch.object(dashboard.uvicorn.Server, "run", return_value=None), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(dashboard.serve(open_browser=False, home=self.home, start=True, scheduler=loop), 0)
            self.assertEqual(loop.start.called, done)
        with mock.patch.object(dashboard, "bind", side_effect=OSError(48, "in use")), \
                mock.patch.object(dashboard, "running_here", return_value=True), \
                mock.patch.object(dashboard, "start_there", return_value=True) as start_there, \
                mock.patch.object(setup_steps, "started", lambda home=None: False), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(dashboard.serve(open_browser=False, home=self.home, start=True), 0)
        start_there.assert_not_called()

    @unittest.skipUnless(DASHBOARD, "dashboard packages (fastapi, httpx) are not installed")
    def test_owner3_port_held_by_another_program_stops_after_3_and_the_next_open_says_why(self):
        codes = []
        with mock.patch.dict(os.environ, {login_item.MARKER: "1"}):
            login_item.enable()  # registered, so the launch is not an unregistered one
            for _ in range(3):
                with mock.patch.object(dashboard, "bind", side_effect=OSError(48, "in use")), \
                        mock.patch.object(dashboard, "running_here", return_value=False), \
                        contextlib.redirect_stderr(io.StringIO()):
                    codes.append(dashboard.serve(open_browser=False, home=self.home, start=True))
        self.assertEqual(codes, [1, 1, 0], "exit 0 on the third, so launchd stops restarting")
        with mock.patch.object(dashboard, "bind", return_value=mock.Mock()), \
                mock.patch.object(dashboard.uvicorn.Server, "run", return_value=None), \
                contextlib.redirect_stdout(io.StringIO()):
            dashboard.serve(open_browser=False, home=self.home)
        self.assertFalse((self.home / login_item.PORT_BUSY_FILE).exists())
        app = dashboard.create_app(SECRET, PORT, home=self.home, root=self.root, config={},
                                   browser=mock.Mock(is_open=mock.Mock(return_value=False)))
        client = TestClient(app, base_url=f"http://127.0.0.1:{PORT}", follow_redirects=False)
        client.get("/", params={"t": SECRET})
        self.assertIn("another program was using port 47823", client.get("/settings").text)

    @unittest.skipUnless(DASHBOARD, "dashboard packages (fastapi, httpx) are not installed")
    def test_item5_each_dashboard_start_refreshes_the_entry(self):
        setup_steps.start_at_login(True, self.home)
        moved = self.user_home / "moved" / "codex"
        setup_steps.update_state(lambda state: state.update(codex_path=str(moved)), self.home)
        with mock.patch.object(dashboard, "bind", return_value=mock.Mock()), \
                mock.patch.object(dashboard.uvicorn.Server, "run", return_value=None), \
                contextlib.redirect_stdout(io.StringIO()):
            dashboard.serve(open_browser=False, home=self.home)
        self.assertIn(str(moved.parent), plistlib.loads(self.plist.read_bytes())["EnvironmentVariables"]["PATH"])

    @unittest.skipUnless(DASHBOARD, "dashboard packages (fastapi, httpx) are not installed")
    def test_a_login_launch_finding_sidedoor_open_leaves_it_alone(self):
        with mock.patch.object(dashboard, "bind", side_effect=OSError(48, "in use")), \
                mock.patch.object(dashboard, "running_here", return_value=True), \
                mock.patch.object(dashboard, "start_there") as start_there, \
                mock.patch.dict(os.environ, {login_item.MARKER: "1"}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(dashboard.serve(open_browser=False, home=self.home, start=True), 0)
        start_there.assert_not_called()


class Child:
    def __init__(self, code, output=b""):
        self.code = code
        self.stdout, self.stderr = io.BytesIO(output), io.BytesIO(b"")

    def wait(self):
        return self.code


class FakePopen:
    """Records each start; each child exits with the next code; the clock moves `ran` seconds per run."""

    def __init__(self, codes, clock, ran=1, output=b""):
        self.codes, self.clock, self.ran, self.calls, self.output = list(codes), clock, ran, [], output

    def __call__(self, argv, **options):
        self.calls.append((argv, options))
        self.clock.now += self.ran
        return Child(self.codes.pop(0), self.output)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class WindowsSupervisorTest(Base):
    """Items 1 and 2: the Windows task runs `supervise`, which restarts `serve` after a crash with no
    console, gives up after 3 quick crashes, and stops on exit 0 (Quit)."""

    def supervise(self, codes, ran=1, output=b""):
        clock, sleeps = Clock(), []
        popen = FakePopen(codes, clock, ran, output)
        code = login_item.supervise(["sidedoor", "serve"], self.root / "logs", self.home, popen=popen,
                                    sleep=sleeps.append, clock=clock)
        return code, popen.calls, sleeps

    def test_item1_a_crash_is_restarted_and_quit_is_not(self):
        code, calls, sleeps = self.supervise([1, 0])
        self.assertEqual((code, len(calls), sleeps), (0, 2, [login_item.RESTART_DELAY_SECONDS]))
        code, calls, sleeps = self.supervise([0])
        self.assertEqual((code, len(calls), sleeps), (0, 1, []), "exit 0 (Quit) is never restarted")

    def test_item1_three_quick_crashes_give_up_with_a_notice_a_long_run_resets(self):
        code, calls, _ = self.supervise([1, 1, 1, 1])
        self.assertEqual((code, len(calls)), (0, 3))
        self.assertIn("kept closing with an error", login_item.take_notice(self.home))
        code, calls, _ = self.supervise([1, 1, 1, 1, 0], ran=login_item.STABLE_SECONDS + 1)
        self.assertEqual(len(calls), 5, "crashes after long runs are each restarted")

    def test_item2_no_console_for_serve_or_the_jobs(self):
        _, calls, _ = self.supervise([0])
        flags = calls[0][1]["creationflags"]
        for flag in (scheduler_loop.CREATE_NO_WINDOW, scheduler_loop.DETACHED_PROCESS,
                     scheduler_loop.CREATE_NEW_PROCESS_GROUP):
            self.assertTrue(flags & flag)
        self.assertEqual(calls[0][1]["stdout"], subprocess.PIPE, "output comes to the supervisor, not through cmd.exe")
        with mock.patch.object(scheduler_loop.subprocess, "Popen") as popen:
            scheduler_loop.spawn("run", self.root, windows=True)
        self.assertEqual(popen.call_args.kwargs["creationflags"],
                         scheduler_loop.CREATE_NO_WINDOW | scheduler_loop.DETACHED_PROCESS
                         | scheduler_loop.CREATE_NEW_PROCESS_GROUP)
        self.assertNotIn("start_new_session", popen.call_args.kwargs)

    def test_Q6_the_supervisor_rotates_serves_log_while_it_runs(self):
        with mock.patch.object(login_item, "ROTATE_BYTES", 100):
            self.supervise([0], output=b"x" * 250)
        logs = self.root / "logs"
        archives = sorted(logs.glob("sidedoor.stdout.log.retained.*"))
        self.assertTrue(archives, "rotated during the run")
        current = logs / "sidedoor.stdout.log"
        kept = sum(path.stat().st_size for path in archives) + (current.stat().st_size if current.exists() else 0)
        self.assertEqual(kept, 250, "nothing lost")

    def test_R1_a_failed_log_write_never_stops_the_pump(self):
        log = login_item._LogFile(self.root, "sidedoor.stdout.log")
        real_open, failures = open, [OSError(28, "No space left on device")]

        def flaky_open(path, mode="r", *args, **kwargs):
            if failures and str(path).endswith("sidedoor.stdout.log"):
                raise failures.pop()
            return real_open(path, mode, *args, **kwargs)

        class Pipe:  # serve's output in three reads, then end of file
            def __init__(self):
                self.chunks = [b"first\n", b"second\n", b"third\n", b""]

            def read1(self, size):
                return self.chunks.pop(0)
        pipe = Pipe()
        with mock.patch("builtins.open", flaky_open):
            login_item._pump(pipe, log)
        log.close()
        self.assertEqual(pipe.chunks, [], "the pipe was drained to the end")
        self.assertEqual((self.root / "sidedoor.stdout.log").read_bytes(), b"second\nthird\n",
                         "the failed chunk is dropped and the file retried on the next")
        self.assertEqual(log.dropped, len(b"first\n"))

    def test_R1_a_write_that_raises_keeps_the_pump_draining(self):
        class Raising:
            def __init__(self):
                self.seen = []

            def write(self, data):
                self.seen.append(data)
                raise OSError(32, "sharing violation")
        log = Raising()
        login_item._pump(io.BytesIO(b"x" * 200000), log)
        self.assertEqual(sum(len(chunk) for chunk in log.seen), 200000)

    def test_R2_undecodable_schtasks_output_gives_a_status_and_never_crashes_serve(self):
        def strict(argv):
            if argv[:2] == ["schtasks", "/Query"] and "/XML" in argv:
                b"M\xfcller".decode("utf-8")
            return 0, ""
        with mock.patch.object(login_item, "PLATFORM", "win32"), mock.patch.object(login_item, "RUNNER", strict):
            setup_steps.start_at_login(True, self.home)
            (self.home / login_item.TASK_COPY).write_bytes(b"old")
            self.assertFalse(login_item.status(home=self.home)["enabled"])
            self.assertFalse(login_item.refresh(home=self.home))
            with mock.patch.object(login_item, "refresh", side_effect=UnicodeDecodeError("utf-8", b"\xfc", 0, 1, "bad")), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertFalse(setup_steps.refresh_login_entry(self.home), "the dashboard start goes on")

    def test_item2_the_task_starts_the_windowed_program_without_cmd(self):
        python = self.root / "python.exe"
        windowed = self.root / "pythonw.exe"
        python.write_text("")
        windowed.write_text("")
        with mock.patch.object(sys, "executable", str(python)):
            xml = login_item.task_xml(user="")
        self.assertIn("<Command>%s</Command>" % windowed, xml)
        self.assertNotIn("cmd.exe", xml)

    def test_item1_3_4_the_cli_runs_supervise_with_the_marker_and_home(self):
        import orchestrator
        with mock.patch.dict(os.environ, {}), mock.patch.object(login_item, "supervise", return_value=0) as run:
            os.environ.pop("SIDEDOOR_HOME", None)
            os.environ.pop(login_item.MARKER, None)
            self.assertEqual(orchestrator.main(["supervise", "--start", "--no-browser", "--at-login",
                                                "--home", str(self.home)]), 0)
            self.assertTrue(login_item.started_at_login())
            self.assertEqual(os.environ["SIDEDOOR_HOME"], str(self.home))
        child = run.call_args[0][0]
        self.assertEqual(child[child.index("serve"):], ["serve", "--start", "--no-browser", "--at-login",
                                                        "--home", str(self.home)])


class EntryTest(Base):
    def test_item5_a_changed_codex_marks_the_entry_stale_and_refresh_rewrites_only_the_file(self):
        setup_steps.start_at_login(True, self.home)
        self.assertFalse(login_item.status(home=self.home)["stale"])
        moved = self.user_home / "elsewhere" / "codex"
        state_change = lambda state: state.update(codex_path=str(moved))
        setup_steps.update_state(state_change, self.home)
        self.assertTrue(login_item.status(home=self.home, codex_path=str(moved))["stale"])
        self.runner.calls.clear()
        self.assertTrue(setup_steps.refresh_login_entry(self.home))
        self.assertEqual([call for call in self.runner.calls if call[1] != "print-disabled"], [],
                         "no bootout or bootstrap: only the file")
        self.assertIn(str(moved.parent), plistlib.loads(self.plist.read_bytes())["EnvironmentVariables"]["PATH"])
        self.assertFalse(login_item.status(home=self.home, codex_path=str(moved))["stale"])

    @unittest.skipIf(os.name == "nt", "macOS plist PATH (':'-joined): a Windows path's drive colon splits it")
    def test_item5_codex_found_again_rewrites_the_entry(self):
        setup_steps.start_at_login(True, self.home)
        codex = self.user_home / "bin2" / "codex"
        codex.parent.mkdir(parents=True)
        codex.write_text("#!/bin/sh\n")
        codex.chmod(0o755)
        ran = mock.Mock(return_value=mock.Mock(returncode=0, stdout="Logged in using ChatGPT", stderr=""))
        setup_steps.check_codex(self.home, run=ran, search_path=str(codex.parent))
        self.assertIn(str(codex.parent), plistlib.loads(self.plist.read_bytes())["EnvironmentVariables"]["PATH"])

    def test_item5_windows_refresh_recreates_the_task(self):
        with mock.patch.object(login_item, "PLATFORM", "win32"):
            setup_steps.start_at_login(True, self.home)
            self.assertFalse(login_item.refresh(home=self.home))
            (self.home / login_item.TASK_COPY).write_bytes(b"old")
            self.runner.calls.clear()
            self.assertTrue(login_item.refresh(home=self.home))
        self.assertEqual([call[:2] for call in self.runner.calls[:2]], [["schtasks", "/Query"], ["schtasks", "/Create"]],
                         "the real task is read first (Q2), then re-created")

    def test_Q2_windows_refresh_leaves_a_deleted_or_disabled_task_alone_and_status_says_so(self):
        disabled_xml = ("<Task><Triggers><LogonTrigger><Enabled>true</Enabled></LogonTrigger></Triggers>"
                        "<Settings><Enabled>false</Enabled></Settings></Task>")
        with mock.patch.object(login_item, "PLATFORM", "win32"):
            setup_steps.start_at_login(True, self.home)
            (self.home / login_item.TASK_COPY).write_bytes(b"old")  # stale
            for answer in ((1, "ERROR: The system cannot find the file specified."), (0, disabled_xml)):
                self.runner.answers = [(["schtasks", "/Query", "/TN", "Sidedoor", "/XML"], answer)]
                self.runner.calls.clear()
                self.assertFalse(login_item.refresh(home=self.home))
                self.assertNotIn("/Create", [call[1] for call in self.runner.calls])
            current = login_item.status(home=self.home)
        self.assertEqual((current["enabled"], current["disabled_by_system"]), (False, True))
        self.assertEqual(current["disabled_message"], login_item.WINDOWS_DISABLED)
        with mock.patch.object(login_item, "PLATFORM", "win32"):
            view = setup_steps.login_view({"status": "done"}, self.home)
        self.assertEqual((view["on"], view["message"]), (False, login_item.WINDOWS_DISABLED))

    def test_item7_each_login_gets_three_tries(self):
        counts = [login_item.note_port_busy(self.home, now=1000 + step) for step in (0, 30, 60)]
        self.assertEqual(counts, [1, 2, 3])
        self.assertEqual(login_item.note_port_busy(self.home, now=1000 + 3600), 1, "a new login starts over")

    def test_item8_the_notice_waits_for_a_page_even_after_a_later_good_login(self):
        for step in (0, 30, 60):
            login_item.note_port_busy(self.home, now=step)
        login_item.clear_port_busy(self.home)  # the next login started fine
        self.assertIn("port 47823", login_item.take_notice(self.home))
        self.assertIsNone(login_item.take_notice(self.home), "shown once")

    @unittest.skipIf(os.name == "nt", "POSIX only: on Windows `supervise` rotates the login logs (login_item.sweep_own_logs)")
    def test_item12_the_login_process_rotates_its_log_every_hour(self):
        swept, stop = [], threading.Event()

        def sweep(root):
            swept.append(root)
            if len(swept) == 3:
                stop.set()
        login_item.rotate_hourly(self.root, stop, interval=0.001, sweep=sweep)
        self.assertEqual(swept, [self.root] * 3)
        self.assertEqual(login_item.ROTATE_EVERY_SECONDS, 3600)
        with mock.patch.dict(os.environ, {login_item.MARKER: "1"}), \
                mock.patch.object(login_item, "rotate_hourly") as rotate, \
                mock.patch.object(login_item.threading, "Thread") as thread:
            self.assertIsNotNone(login_item.start_hourly_rotation(self.root))
        self.assertIs(thread.call_args.kwargs["target"], rotate)


@unittest.skipIf(os.name == "nt", "the macOS launcher runs under /bin/sh")
class LauncherTest(unittest.TestCase):
    """Item 5: the macOS launcher (/bin/sh, run for real on temporary paths; nothing is registered)."""

    def run_launcher(self, home, logs, program):
        script = login_item.LAUNCHER
        return subprocess.run(["/bin/sh", "-c", script, "sidedoor-login", str(home), str(logs), program, "serve"],
                              capture_output=True, text=True, timeout=10).returncode

    def test_a_gone_program_gives_up_after_3_with_a_notice_and_a_present_one_runs(self):
        with tempfile.TemporaryDirectory() as base:
            home, logs = Path(base) / "home", Path(base) / "logs"
            home.mkdir()
            logs.mkdir()
            codes = [self.run_launcher(home, logs, str(Path(base) / "gone")) for _ in range(3)]
            self.assertEqual(codes, [1, 1, 0], "launchd stops after the third")
            self.assertEqual(login_item.take_notice(home), login_item.NOTICES["missing"])
            self.assertEqual(self.run_launcher(home, logs, "/bin/echo"), 0)
            self.assertEqual((logs / "sidedoor.stdout.log").read_text(), "serve\n", "output appended to the log")

    def test_Q3_a_missing_logs_folder_is_created_not_counted(self):
        with tempfile.TemporaryDirectory() as base:
            home, logs = Path(base) / "home", Path(base) / "logs"
            home.mkdir()
            self.assertEqual(self.run_launcher(home, logs, "/bin/echo"), 0)
            self.assertEqual((logs / "sidedoor.stdout.log").read_text(), "serve\n")
            self.assertFalse((home / "login-missing-count").exists())

    def test_Q4_a_bad_count_is_read_as_0_never_evaluated(self):
        with tempfile.TemporaryDirectory() as base:
            home, logs = Path(base) / "home", Path(base) / "logs"
            home.mkdir()
            logs.mkdir()
            gone = str(Path(base) / "gone")
            (home / "login-missing-count").write_text("1 2\n")
            self.assertEqual(self.run_launcher(home, logs, gone), 1, "not 127 forever")
            self.assertEqual((home / "login-missing-count").read_text().strip(), "1")
            crafted = Path(base) / "crafted"
            (home / "login-missing-count").write_text("a[$(touch %s)]" % crafted)
            self.assertEqual(self.run_launcher(home, logs, gone), 1)
            self.assertFalse(crafted.exists(), "the count is never run as a command")


class RunnerDecodingTest(unittest.TestCase):
    """R2: the real runner (here on a Python child, never schtasks) replaces bytes that are not UTF-8."""

    def test_undecodable_output_is_replaced_not_raised(self):
        code, output = login_item._run([sys.executable, "-c",
                                        "import sys; sys.stdout.buffer.write(b'M\\x81ller <Enabled>true</Enabled>')"])
        self.assertEqual(code, 0)
        self.assertIn("\ufffd", output, "undecodable bytes are replaced, not raised")

class OwnLogRotationTest(unittest.TestCase):
    """Q1: a real child process whose stdout is sidedoor.stdout.log rotates it while this test holds
    state/runner.lock (as the :00 cycle does); nothing is registered."""

    @unittest.skipIf(os.name == "nt", "POSIX-only (E2.T3: process groups, rlimits, flock, lsof or symlinks)")
    def test_rotation_happens_with_runner_lock_held(self):
        import fcntl
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            (root / "logs").mkdir()
            (root / "state").mkdir()
            log = root / "logs" / "sidedoor.stdout.log"
            script = textwrap.dedent(f"""
                import sys, json; sys.path.insert(0, {str(ROOT)!r})
                import log_retention, login_item
                print("x" * 50, flush=True)
                busy = log_retention.retain({str(root)!r})["state"]
                rotated = login_item.rotate_own_streams({str(root)!r}, rotate_bytes=10)
                print("after", flush=True)
                sys.stderr.write(json.dumps({{"busy": busy, "rotated": rotated}}))
            """)
            with open(root / "state" / "runner.lock", "w") as lock, open(log, "ab") as out:
                fcntl.flock(lock, fcntl.LOCK_EX)
                done = subprocess.run([sys.executable, "-c", script], stdout=out, stderr=subprocess.PIPE,
                                      text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(json.loads(done.stderr.strip().splitlines()[-1]), {"busy": "BUSY", "rotated": 1})
            archives = list((root / "logs").glob("sidedoor.stdout.log.retained.*"))
            self.assertEqual(len(archives), 1)
            self.assertIn("x" * 50, archives[0].read_text())
            self.assertEqual(log.read_text(), "after\n", "the process writes on into the new file")

    def test_the_hourly_timer_uses_the_lock_free_rotation(self):
        import inspect
        self.assertIs(inspect.signature(login_item.rotate_hourly).parameters["sweep"].default, None)
        with mock.patch.object(login_item, "rotate_own_streams") as rotate, \
                mock.patch.object(login_item, "sweep_own_logs") as sweep:
            stop = threading.Event()
            rotate.side_effect = lambda root: stop.set()
            worker = threading.Thread(target=login_item.rotate_hourly, args=("r", stop), kwargs={"interval": 0.001})
            worker.start()
            worker.join(2)
            stop.set()
            worker.join(2)
        rotate.assert_called_with("r")
        sweep.assert_not_called()


@unittest.skipUnless(DASHBOARD, "dashboard packages (fastapi, httpx) are not installed")
class RealProcessQuitTest(unittest.TestCase):
    """A real `serve` (temporary home, a free port, real uvicorn; no OS registration) stopped with
    SIGTERM exits 0, as a deliberate Quit must for the OS not to restart it."""

    @unittest.skipIf(os.name == "nt", "Windows has no SIGTERM delivery to another process (send_signal is TerminateProcess)")
    def test_sigterm_exits_0(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with tempfile.TemporaryDirectory() as home:
            script = textwrap.dedent(f"""
                import sys; sys.path.insert(0, {str(ROOT)!r})
                import dashboard
                original = dashboard.bind
                dashboard.bind = lambda: original({port})
                dashboard.PORT = {port}
                sys.exit(dashboard.serve(open_browser=False, home={home!r}))
            """)
            environment = dict(os.environ)
            environment.pop(login_item.MARKER, None)
            child = subprocess.Popen([sys.executable, "-c", script], cwd=home, env=environment,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), timeout=0.5):
                        break
                    time.sleep(0.1)
                else:
                    self.fail("serve did not start")
                time.sleep(0.3)
                child.send_signal(signal.SIGTERM)
                self.assertEqual(child.wait(timeout=20), 0)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()


@unittest.skipUnless(DASHBOARD, "dashboard packages (fastapi, httpx) are not installed")
class PagesTest(Base):
    def setUp(self):
        super().setUp()
        browser = mock.Mock(is_open=mock.Mock(return_value=False))
        app = dashboard.create_app(SECRET, PORT, home=self.home, root=self.root,
                                   config={"text_model": {"model": "test/model"}}, browser=browser,
                                   relay_url="https://relay.test")
        self.client = TestClient(app, base_url=f"http://127.0.0.1:{PORT}", follow_redirects=False)
        self.client.get("/", params={"t": SECRET})

    def step10(self, text):
        start = text.index('id="step-10"')
        return text[start:text.index("</li>", start)]

    def test_setup_step_10_saves_when_the_box_changes(self):
        # Owner, 2026-10-22: "simply checking or unchecking the box should do" (no Save click).
        step = self.step10(self.client.get("/setup").text)
        self.assertIn('hx-trigger="change"', step)
        self.assertIn("<noscript><button", step)

    def test_setup_step_10_shows_and_toggles_the_real_registration(self):
        on = self.step10(self.client.post("/setup/login", data={"enabled": "on"}, headers=HTMX).text)
        self.assertIn(login_item.ON, on)
        self.assertIn('name="enabled" checked', on)
        self.assertTrue(self.plist.exists())
        self.assertIn('name="enabled" checked', self.step10(self.client.get("/setup").text))
        off = self.step10(self.client.post("/setup/login", data={}, headers=HTMX).text)
        self.assertNotIn("checked", off)
        self.assertIn("Open Sidedoor yourself after you log in", off)
        self.assertFalse(self.plist.exists())
        self.assertEqual([call[1] for call in self.runner.calls if call[1] != "print-disabled"],
                         ["enable", "bootstrap", "bootout"])

    def test_the_file_decides_not_the_saved_record(self):
        self.client.post("/setup/login", data={"enabled": "on"}, headers=HTMX)
        self.plist.unlink()  # removed outside Sidedoor
        self.assertNotIn("checked", self.step10(self.client.get("/setup").text))

    # --- owner settings and review fixes (2026-10-22) --------------------------------------------

    def test_owner1_step_10_starts_ticked_and_registers_only_on_save_or_start(self):
        step = self.step10(self.client.get("/setup").text)
        self.assertIn('name="enabled" checked', step)
        self.assertIn(">Save</button>", step)
        self.assertEqual([call for call in self.runner.calls if call[1] != "print-disabled"], [])
        self.assertFalse(self.plist.exists(), "showing the ticked default registers nothing")
        self.assertNotIn('name="enabled" checked', self.client.get("/settings").text, "Settings shows the real state")
        setup_steps.login_on_continue(self.home)  # Setup's Start, continuing past step 10
        self.assertTrue(self.plist.exists())
        self.client.post("/setup/login", data={}, headers=HTMX)  # the user unticks and saves: their choice stands
        self.assertIsNone(setup_steps.login_on_continue(self.home))
        self.assertFalse(self.plist.exists())

    def test_owner1_start_registers_the_untouched_default(self):
        real = setup_steps.progress
        with mock.patch.object(setup_steps, "progress", lambda home=None: dict(real(home), locked={})), \
                mock.patch.object(setup_steps, "start_scheduler", lambda home=None: None):
            response = self.client.post("/setup/start", headers=HTMX)
        self.assertTrue(self.plist.exists(), "Start continues past the ticked step 10")
        self.assertIn(login_item.ON, response.text, "step 10 is re-sent with the new state")

    def test_item5_a_stale_entry_says_needs_updating_with_an_update_button(self):
        self.client.post("/setup/login", data={"enabled": "on"}, headers=HTMX)
        setup_steps.update_state(lambda state: state.update(codex_path=str(self.user_home / "moved" / "codex")), self.home)
        step = self.step10(self.client.get("/setup").text)
        self.assertIn("needs updating", step)
        self.assertIn(">Update</button>", step)
        self.assertIn(">Update</button>", self.client.get("/settings").text)
        step = self.step10(self.client.post("/setup/login", data={"enabled": "on"}, headers=HTMX).text)
        self.assertIn(login_item.ON, step)

    def test_L2_disabled_by_the_mac_shows_off_with_the_login_items_pointer(self):
        self.client.post("/setup/login", data={"enabled": "on"}, headers=HTMX)
        self.runner.answers = [(["launchctl", "print-disabled"],
                                (0, 'disabled services = {\n\t"com.yoursidedoor.sidedoor" => disabled\n}'))]
        current = login_item.status()
        self.assertEqual((current["enabled"], current["disabled_by_system"], current["disabled_message"]),
                         (False, True, login_item.MAC_DISABLED))
        step = self.step10(self.client.get("/setup").text)
        self.assertNotIn('name="enabled" checked', step)
        self.assertIn("System Settings &gt; General &gt; Login Items", step)
        self.assertIn("Login Items", self.client.get("/settings").text)

    def test_L5_L7_a_refusal_is_plain_and_try_again_retries(self):
        self.runner.answers = [(["launchctl", "bootstrap"], (5, "Bootstrap failed: 5: Input/output error")),
                               (["launchctl", "print"], (113, "Could not find service"))]
        with contextlib.redirect_stderr(io.StringIO()) as log:
            step = self.step10(self.client.post("/setup/login", data={"enabled": "on"}, headers=HTMX).text)
        self.assertNotIn("Input/output", step)
        self.assertIn("Input/output error", log.getvalue(), "the OS's words go to the log")
        self.assertIn('<input type="hidden" name="enabled" value="on">', step)
        self.runner.answers = []
        step = self.step10(self.client.post("/setup/login", data={"enabled": "on"}, headers=HTMX).text)
        self.assertIn(login_item.ON, step)

    def test_L8_status_is_read_off_the_event_loop(self):
        import asyncio
        seen = []
        real = login_item.status

        def status(*args, **kwargs):
            try:
                asyncio.get_running_loop()
                seen.append("event loop")
            except RuntimeError:
                seen.append("thread")
            return real(*args, **kwargs)
        with mock.patch.object(login_item, "status", status):
            self.client.post("/setup/login", data={"enabled": "on"}, headers=HTMX)
            self.client.post("/settings/login", data={}, headers=HTMX)
            self.client.get("/setup")
            self.client.get("/settings")
        self.assertTrue(seen)
        self.assertNotIn("event loop", seen)

    def test_a_refusal_shows_a_way_forward_and_the_box_stays(self):
        self.runner.answers = [(["launchctl", "bootstrap"], (5, "Bootstrap failed: 5: Input/output error")),
                               (["launchctl", "print"], (113, "Could not find service"))]
        step = self.step10(self.client.post("/setup/login", data={"enabled": "on"}, headers=HTMX).text)
        self.assertIn("Failed", step)
        self.assertIn("Try again", step)
        self.assertIn('type="checkbox" name="enabled"', step)

    def test_3_linux_says_not_available_yet_without_a_dead_box(self):
        with mock.patch.object(login_item, "PLATFORM", "linux"):
            step = self.step10(self.client.get("/setup").text)
            settings = self.client.get("/settings").text
        line = "Start at login isn&#39;t available on this system yet; Sidedoor still works when you open it."
        self.assertIn(line, step)
        self.assertNotIn('type="checkbox"', step)
        self.assertIn(line, settings)
        self.assertEqual(self.runner.calls, [])

    def test_settings_section_shows_and_toggles_it(self):
        page = self.client.get("/settings").text
        self.assertIn('href="#login"', page)
        self.assertIn('id="login"', page)
        on = self.client.post("/settings/login", data={"enabled": "on"}, headers=HTMX).text
        self.assertIn(login_item.ON, on)
        self.assertIn('name="enabled" checked', on)
        off = self.client.post("/settings/login", data={}, headers=HTMX).text
        self.assertNotIn('name="enabled" checked', off)
        self.assertFalse(self.plist.exists())
        plain = self.client.post("/settings/login", data={"enabled": "on"}, headers=ORIGIN)
        self.assertEqual((plain.status_code, plain.headers["location"]), (303, "/settings#login"))


if __name__ == "__main__":
    unittest.main()
