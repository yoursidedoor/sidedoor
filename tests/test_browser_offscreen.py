"""FG1 (owner, 2026-10-22). Option b: Sidedoor's session Chrome runs off-screen and comes on-screen only for
the CAPTCHA wait. Problem 2: no "close the Sidedoor Chrome window" while a Sidedoor session owns that
Chrome. Offline: no Chrome, no `open`, no Node; the frontmost window and CDP are fakes."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import io
import json
import signal
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import browser_guard  # noqa: E402
import browser_runtime  # noqa: E402
import doctor  # noqa: E402
import run_locks  # noqa: E402
from test_browser_driver_review import scratch  # noqa: E402

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


class OffscreenLaunch(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(browser_runtime, "BACKGROUND_ALLOWED", True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_open_g_launch_starts_the_window_far_off_screen_at_a_normal_size(self):
        # FG2 = a keeps this as the launch position only (the guard then minimizes the window).
        command = browser_runtime.background_command(browser_runtime.chrome_app(CHROME), Path("/p"))
        self.assertIn("--window-position=-32000,-32000", command)
        self.assertIn("--window-size=1280,900", command)
        self.assertEqual(command[-1], "about:blank")

    def test_playwright_fallback_launch_and_guard_flag_on_macos_only(self):
        directory = scratch(self)
        config = {"browser_driver": {"profile": str(directory / "profile")}}
        with mock.patch.object(browser_runtime.sys, "platform", "darwin"):
            argv = browser_runtime.guard_command(config, directory, "inspect")
            launch = json.loads((directory / "browser-config.json").read_text())
        self.assertIn("--offscreen", argv[:argv.index("--")])
        self.assertEqual(launch["browser"]["launchOptions"]["args"], list(browser_runtime.WINDOW_SWITCHES))
        self.assertEqual(launch["browser"]["launchOptions"]["ignoreDefaultArgs"],
                         browser_runtime.LAUNCH_CONFIG["browser"]["launchOptions"]["ignoreDefaultArgs"])
        for other in ({"browser_driver": {"headless": True}}, {"browser_driver": {"isolated": True}}):
            with mock.patch.object(browser_runtime.sys, "platform", "darwin"):
                argv = browser_runtime.guard_command(other, directory, "inspect")
            self.assertNotIn("--offscreen", argv)
            self.assertEqual(json.loads((directory / "browser-config.json").read_text()), browser_runtime.LAUNCH_CONFIG)
        with mock.patch.object(browser_runtime.sys, "platform", "win32"):
            self.assertNotIn("--offscreen", browser_runtime.guard_command(config, directory, "inspect"))
        self.assertIsNot(browser_runtime.launch_config(config), browser_runtime.LAUNCH_CONFIG)   # never mutated


def answer(windows, error=False):
    return {"jsonrpc": "2.0", "id": "guard-1", "result": {
        "content": [{"type": "text", "text": "### Result\n" + json.dumps(windows)}], "isError": error}}


class FakeWindows:
    """Stands in for Chrome's windows behind browser_run_code_unsafe: records each placement the guard
    asks for and answers the read-back (optionally pulled back onto a display, as macOS may do)."""

    def __init__(self, clamp=False):
        self.calls, self.clamp = [], clamp

    def __call__(self, method, params):
        code = params["arguments"]["code"]
        self.calls.append((params["name"], code))
        if "windowState: 'minimized'" in code:   # FG2 = a: hide_code (clamp: the window would not minimize)
            return answer([{"windowId": 1, "state": "normal" if self.clamp else "minimized", "was": "normal"}])
        return answer([{"windowId": 1, "left": 80, "top": 60}])


class GuardKeepsTheWindowOffscreen(unittest.TestCase):
    def proxy(self, windows, offscreen=True, clock=None, **options):
        directory = scratch(self)
        with mock.patch.object(browser_guard.subprocess, "Popen"):
            proxy = browser_guard.Proxy("submit", ["node"], audit=directory / "guard.jsonl", stdin=io.StringIO(""),
                                        stdout=io.StringIO(), offscreen=offscreen, clock=clock, **options)
        proxy.child_call = windows
        proxy.to_child = lambda message: None
        proxy.audit_path = directory / "guard.jsonl"
        return proxy

    def audit(self, proxy):
        return [json.loads(line) for line in proxy.audit_path.read_text().splitlines()]

    def call(self, proxy, name, ident):
        proxy.handle({"jsonrpc": "2.0", "id": ident, "method": "tools/call", "params": {"name": name, "arguments": {}}})
        with proxy.idle:
            proxy.in_flight.discard(ident)

    def test_every_forwarded_call_is_preceded_by_a_placement_at_most_once_a_second(self):
        windows, now = FakeWindows(), [100.0]
        proxy = self.proxy(windows, clock=lambda: now[0])
        self.call(proxy, "browser_navigate", 1)
        self.call(proxy, "browser_snapshot", 2)          # 0 s later: not placed again
        now[0] += 1.5
        self.call(proxy, "browser_click", 3)
        self.assertEqual(len(windows.calls), 2)
        name, code = windows.calls[0]
        self.assertEqual(name, "browser_run_code_unsafe")
        self.assertIn("Browser.setWindowBounds", code)
        self.assertIn("windowState: 'minimized'", code)   # FG2 = a: minimized, parked off-screen first
        self.assertIn("left: -32000, top: -32000, width: 1280, height: 900", code)
        self.assertLess(code.index("left: -32000"), code.index("windowState: 'minimized'"))
        self.assertNotIn("bringToFront", code)            # minimizing never brings Chrome forward
        decisions = [entry["decision"] for entry in self.audit(proxy)]
        self.assertEqual(decisions.count("WINDOW_MINIMIZED"), 2)
        self.assertEqual(decisions.count("FORWARDED"), 3)
        self.assertNotIn("WINDOW_NOT_MINIMIZED", decisions)

    def test_a_window_pulled_back_on_screen_is_recorded_and_a_failure_never_stops_the_call(self):
        proxy = self.proxy(FakeWindows(clamp=True))
        self.call(proxy, "browser_navigate", 1)
        self.assertIn("WINDOW_NOT_MINIMIZED", [entry["decision"] for entry in self.audit(proxy)])
        proxy = self.proxy(lambda method, params: None)   # the placement call timed out
        self.call(proxy, "browser_navigate", 1)
        decisions = [entry["decision"] for entry in self.audit(proxy)]
        self.assertEqual(decisions[:2], ["WINDOW_PLACEMENT_FAILED", "FORWARDED"])
        proxy = self.proxy(lambda method, params: answer([], error=True))
        self.call(proxy, "browser_navigate", 1)
        self.assertIn("FORWARDED", [entry["decision"] for entry in self.audit(proxy)])

    def test_without_the_flag_nothing_is_placed(self):
        windows = FakeWindows()
        proxy = self.proxy(windows, offscreen=False)
        self.call(proxy, "browser_navigate", 1)
        self.assertEqual(windows.calls, [])

    def test_the_captcha_wait_shows_the_window_in_front_then_hides_it_again(self):
        windows, seen = FakeWindows(), []

        class Wait:
            def call(self_inner):
                seen.append(([code for _, code in windows.calls], proxy.showing))
                return "RECHECK_NOW"
        proxy = self.proxy(windows, human_check_wait=Wait())
        threads = []
        with mock.patch.object(browser_guard.threading, "Thread",
                               side_effect=lambda target, daemon: threads.append(target) or mock.Mock()):
            proxy.call_human_check({"jsonrpc": "2.0", "id": 9})
        threads[0]()
        during, showing = seen[0]
        self.assertTrue(showing)
        self.assertEqual(len(during), 1)
        self.assertIn("left: 80, top: 60", during[0])
        self.assertIn("page.bringToFront()", during[0])   # the owner has to tick the box
        self.assertEqual(len(windows.calls), 2)
        self.assertIn("windowState: 'minimized'", windows.calls[1][1])
        self.assertFalse(proxy.showing)
        # While the window is on-screen for the owner, a call (none is expected) does not hide it.
        proxy.showing = True
        self.call(proxy, "browser_snapshot", 10)
        self.assertEqual(len(windows.calls), 2)

    def test_the_window_goes_back_off_screen_even_when_the_wait_fails(self):
        windows = FakeWindows()

        class Broken:
            def call(self_inner):
                raise RuntimeError("no context")
        proxy = self.proxy(windows, human_check_wait=Broken())
        threads = []
        with mock.patch.object(browser_guard.threading, "Thread",
                               side_effect=lambda target, daemon: threads.append(target) or mock.Mock()):
            proxy.call_human_check({"jsonrpc": "2.0", "id": 9})
        threads[0]()
        self.assertIn("windowState: 'minimized'", windows.calls[-1][1])
        self.assertFalse(proxy.showing)

    def test_the_model_still_cannot_bring_chrome_forward(self):
        windows = FakeWindows()
        proxy = self.proxy(windows)
        sent = []
        proxy.to_client = sent.append
        proxy.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "browser_run_code_unsafe", "arguments": {"code": "async (page) => page.bringToFront()"}}})
        self.assertIn(browser_guard.FOCUS_CODE, sent[0]["result"]["content"][0]["text"])
        self.assertEqual(windows.calls, [])

    def test_placed_windows_reads_the_run_code_answer(self):
        self.assertEqual(browser_guard.placed_windows(answer([{"windowId": 3, "left": -32000, "top": -32000}])),
                         [{"windowId": 3, "left": -32000, "top": -32000}])
        self.assertIsNone(browser_guard.placed_windows(None))
        self.assertIsNone(browser_guard.placed_windows({"result": {"content": [{"type": "text", "text": "Error"}]}}))


def ps(lines):
    """A fake `ps`: `-p <pid>` answers the profile holder's line, `-axo` every process line."""
    def run(argv, **kwargs):
        if "-p" in argv:
            return subprocess.CompletedProcess(argv, 0, lines[0] + "\n", "")
        return subprocess.CompletedProcess(argv, 0, "\n".join(lines) + "\n", "")
    return run


class Orphan:
    """A fake Chrome on the profile: `ps` shows it (plus `others`) until a signal it obeys ends it."""

    def __init__(self, line, others=(), obeys=(signal.SIGTERM, signal.SIGKILL)):
        self.line, self.others, self.obeys, self.signals, self.alive = line, list(others), obeys, [], True

    def run(self, argv, **kwargs):
        mine = [self.line] if self.alive else []
        if "-p" in argv:
            return subprocess.CompletedProcess(argv, 0, "".join(line + "\n" for line in mine), "")
        return subprocess.CompletedProcess(argv, 0, "\n".join(mine + self.others) + "\n", "")

    def kill(self, pid, number):
        assert pid == 4242, pid
        self.signals.append(number)
        if number in self.obeys:
            self.alive = False


class OrphanedSessionChrome(unittest.TestCase):
    """Sidedoor stopped mid-session: the guard's `open -g -n` Chrome outlived it and held the profile."""

    def setUp(self):
        self.root = scratch(self)
        self.profile = self.root / "profile"
        self.profile.mkdir()
        self.config = {"browser_driver": {"profile": str(self.profile)}}
        for name, value in (("missing", lambda config=None: []), ("clear_stale_lock", lambda path: None),
                            ("lock_holder", lambda path: 4242)):
            patcher = mock.patch.object(browser_runtime, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def chrome(self, *extra):
        return " ".join([CHROME, "--user-data-dir=" + str(self.profile), *extra])

    def test_an_orphan_is_closed_and_the_session_proceeds(self):
        for switch in ("--remote-debugging-port=0", "--remote-debugging-pipe"):
            orphan = Orphan(self.chrome(switch))
            self.assertIsNone(browser_runtime.unavailable(self.config, orphan.run, self.root, orphan.kill), switch)
            self.assertEqual(orphan.signals, [signal.SIGTERM])
        # The session's own check runs inside its browser lock: its own hold does not protect the orphan.
        orphan = Orphan(self.chrome("--remote-debugging-port=0"))
        with run_locks.browser_session(self.root) as held:
            self.assertTrue(held)
            self.assertIsNone(browser_runtime.unavailable(self.config, orphan.run, self.root, orphan.kill))
        self.assertEqual(orphan.signals, [signal.SIGTERM])

    def test_an_orphan_with_a_guard_running_is_not_closed(self):
        orphan = Orphan(self.chrome("--remote-debugging-port=0"),
                        others=["/x/sidedoor browser-guard --operation inspect -- node cli.js"])
        self.assertEqual(browser_runtime.display_state(self.config, self.root, orphan.run, orphan.kill), "SESSION_BUSY")
        self.assertFalse(browser_runtime.close_orphaned_session_chrome(self.profile, self.root, orphan.run, orphan.kill))
        self.assertEqual(orphan.signals, [])

    def test_an_orphan_is_not_closed_while_another_session_holds_the_lock(self):
        orphan = Orphan(self.chrome("--remote-debugging-port=0"))
        with run_locks.held(self.root, run_locks.BROWSER) as held:   # another process's session
            self.assertTrue(held)
            self.assertEqual(browser_runtime.display_state(self.config, self.root, orphan.run, orphan.kill),
                             "SESSION_BUSY")
            self.assertFalse(browser_runtime.close_orphaned_session_chrome(self.profile, self.root, orphan.run,
                                                                           orphan.kill))
        self.assertEqual(orphan.signals, [])

    def test_a_sign_in_or_user_chrome_is_never_closed(self):
        user = Orphan(self.chrome())     # no DevTools switch: setup step 8's window or the user's own
        self.assertEqual(browser_runtime.display_state(self.config, self.root, user.run, user.kill), "PROFILE_IN_USE")
        self.assertEqual(browser_runtime.unavailable(self.config, user.run, self.root, user.kill), "PROFILE_IN_USE")
        self.assertEqual(user.signals, [])

    def test_an_orphan_that_ignores_sigterm_is_killed(self):
        orphan = Orphan(self.chrome("--remote-debugging-port=0"), obeys=(signal.SIGKILL,))
        self.assertTrue(browser_runtime.close_orphaned_session_chrome(self.profile, self.root, orphan.run, orphan.kill,
                                                                      wait_seconds=0.2, poll_seconds=0.05))
        self.assertEqual(orphan.signals, [signal.SIGTERM, signal.SIGKILL])

    def test_windows_is_a_no_op(self):
        orphan = Orphan(self.chrome("--remote-debugging-port=0"))
        with mock.patch.object(browser_runtime.portable, "WINDOWS", True):
            self.assertFalse(browser_runtime.close_orphaned_session_chrome(self.profile, self.root, orphan.run,
                                                                           orphan.kill))
        self.assertEqual(orphan.signals, [])


class ProfileOwnedBySidedoor(unittest.TestCase):
    """Problem 2: who says "close the window", and when."""

    def setUp(self):
        self.root = scratch(self)
        self.profile = self.root / "profile"
        self.profile.mkdir()
        self.config = {"browser_driver": {"profile": str(self.profile)}}
        for name, value in (("missing", lambda config=None: []), ("clear_stale_lock", lambda path: None),
                            ("lock_holder", lambda path: 4242)):
            patcher = mock.patch.object(browser_runtime, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def chrome(self, *extra):
        return " ".join([CHROME, "--user-data-dir=" + str(self.profile), *extra])

    def test_a_session_holding_the_browser_lock_owns_the_chrome(self):
        user_window = ps([self.chrome()])     # no DevTools switch, no guard: it looks like the user's window
        with run_locks.browser_session(self.root) as held:
            self.assertTrue(held)
            self.assertEqual(browser_runtime.display_state(self.config, self.root, user_window), "SESSION_BUSY")
            line = doctor.check_browser_runtime(self.config, user_window, self.root)
        self.assertEqual(line["status"], doctor.OK)
        self.assertIn(browser_runtime.BUSY_WORDS, line["line"])
        self.assertNotIn("Close", line["line"] + line["todo"])
        # The session's own check, inside its lock before Chrome starts, is unchanged: a conflict there is real.
        self.assertEqual(browser_runtime.unavailable(self.config, user_window), "PROFILE_IN_USE")

    def test_a_running_guard_and_its_devtools_chrome_own_the_profile_without_the_lock(self):
        for switch in ("--remote-debugging-port=0", "--remote-debugging-pipe"):
            run = ps([self.chrome(switch), "/x/sidedoor browser-guard --operation submit --offscreen -- node cli.js"])
            self.assertEqual(browser_runtime.display_state(self.config, self.root, run), "SESSION_BUSY", switch)
        # A leftover DevTools Chrome with no guard running is Sidedoor's orphan: closed, never "close the window".
        orphan = Orphan(self.chrome("--remote-debugging-port=0"))
        self.assertIsNone(browser_runtime.display_state(self.config, self.root, orphan.run, orphan.kill))
        self.assertEqual(orphan.signals, [signal.SIGTERM])

    def test_close_the_window_only_with_no_session_and_a_live_chrome_on_the_profile(self):
        run = ps([self.chrome()])
        self.assertEqual(browser_runtime.display_state(self.config, self.root, run), "PROFILE_IN_USE")
        line = doctor.check_browser_runtime(self.config, run, self.root)
        self.assertEqual(line["status"], doctor.WARN)
        self.assertEqual(line["todo"], browser_runtime.UNAVAILABLE_TODO["PROFILE_IN_USE"])
        other = ps(["/usr/bin/vim"])          # the holder pid is no Chrome on this profile: not in use
        self.assertIsNone(browser_runtime.display_state(self.config, self.root, other))
        with mock.patch.object(browser_runtime, "missing", lambda config=None: ["node"]):
            self.assertEqual(browser_runtime.display_state(self.config, self.root, run), "RUNTIME_MISSING")

    def test_doctor_carries_the_offscreen_note_on_macos(self):
        with mock.patch.object(browser_runtime, "offscreen", lambda config=None, platform=None: True):
            line = doctor.check_browser_runtime(self.config, ps(["/usr/bin/vim"]), self.root)
        self.assertIn(browser_runtime.OFFSCREEN_WORDS, line["line"])
        self.assertEqual(browser_runtime.OFFSCREEN_WORDS,
                         "Sidedoor's browser runs minimized in the Dock; it appears when it needs you.")


import setup_steps  # noqa: E402
import test_dashboard_overview  # noqa: E402


class OverviewSaysNothingWhileASessionRuns(unittest.TestCase):
    setUp = test_dashboard_overview.GatherTest.setUp
    tearDown = test_dashboard_overview.GatherTest.tearDown
    gather = test_dashboard_overview.GatherTest.gather

    def test_busy_shows_no_close_the_window_and_the_offscreen_note_shows(self):
        test_dashboard_overview.finished_home(self.home)
        with mock.patch.object(setup_steps, "chrome_path", return_value="/chrome"), \
                mock.patch.object(browser_runtime, "display_state", lambda config=None, root=None, run=None: "SESSION_BUSY"), \
                mock.patch.object(browser_runtime, "offscreen", lambda config=None, platform=None: True):
            health = {entry["name"]: entry for entry in self.gather()["health"]}
        texts = json.dumps(health, default=str)
        self.assertNotIn(browser_runtime.UNAVAILABLE_WORDS["PROFILE_IN_USE"], texts)
        self.assertNotIn("Close the Sidedoor Chrome window", texts)
        self.assertEqual(health["Browser window"]["text"], browser_runtime.OFFSCREEN_WORDS)
        with mock.patch.object(setup_steps, "chrome_path", return_value="/chrome"):
            self.assertNotIn("Browser window", {entry["name"] for entry in self.gather()["health"]})   # tests: off


if __name__ == "__main__":
    unittest.main()
