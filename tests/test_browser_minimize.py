"""FG2 = a (owner, 2026-10-23): Sidedoor's session Chrome is minimized (not off-screen: macOS clamped it to a
visible strip), and a focus keeper hands the front back to the user's app whenever that Chrome activates
itself. Offline: CDP and the frontmost-app API are fakes; no Chrome, no lsappinfo, no Node."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import browser_guard  # noqa: E402
import browser_runtime  # noqa: E402
from test_browser_driver_review import scratch  # noqa: E402

CHROME, EVERYDAY_CHROME, MAIL, NOTES = 900, 800, 100, 200


class Desktop:
    """A fake frontmost-app API: `front` is the frontmost pid; activate(pid) makes it frontmost."""

    def __init__(self, front=MAIL):
        self.now, self.activated = front, []

    def front(self):
        return self.now

    def activate(self, pid):
        self.activated.append(pid)
        self.now = pid
        return True


class Keeper(unittest.TestCase):
    """Owner, 2026-10-24: no guessing; back to exactly the app the user was in, every time; windows
    minimized as soon as they show."""

    def keeper(self, desktop, shown=False):
        self.time, self.log, self.hides = [0.0], [], []
        self.flags = {"suspended": False, "shown": shown}
        return browser_guard.FocusKeeper(lambda: CHROME, desktop.front, desktop.activate,
                                         suspended=lambda: self.flags["suspended"],
                                         log=lambda **entry: self.log.append(entry), clock=lambda: self.time[0],
                                         chrome_shown=lambda pid: self.flags["shown"],
                                         hide=lambda: self.hides.append(self.time[0]), alive=lambda pid: True)

    def tick(self, keeper, seconds=browser_guard.FOCUS_POLL_SECONDS):
        self.time[0] += seconds
        keeper.poll()

    def test_the_poll_is_well_under_half_a_second(self):
        self.assertLessEqual(browser_guard.FOCUS_POLL_SECONDS, 0.05)
        for name in ("FOCUS_STAY_SECONDS", "FOCUS_RECLICK_SECONDS", "FOCUS_MAX_CORRECTIONS"):
            self.assertFalse(hasattr(browser_guard, name), name)   # no "user is watching" guesses

    def test_every_activation_is_undone_at_once_and_each_one_is_recorded(self):
        desktop = Desktop(MAIL)
        keeper = self.keeper(desktop, shown=True)
        self.tick(keeper)                    # the user is in Mail
        for _ in range(10):                  # Chrome comes forward again and again, the user clicking it too
            desktop.now = CHROME
            self.tick(keeper)
        self.assertEqual(desktop.activated, [MAIL] * 10)
        self.assertEqual(len(self.hides), 11)          # minimized each time (and once while the user was in Mail)
        self.assertEqual([e["decision"] for e in self.log], ["FOCUS_RESTORED"] * 10)
        self.assertEqual(self.log[0]["to_pid"], MAIL)
        self.assertEqual(self.log[0]["seen_ms"], 50.0)   # Mail was seen in front one poll before
        self.assertIn("correct_ms", self.log[0])

    def test_the_app_in_front_just_before_chrome_is_the_one_restored(self):
        desktop = Desktop(MAIL)
        keeper = self.keeper(desktop)
        self.tick(keeper)
        desktop.now = NOTES
        self.tick(keeper)
        desktop.now = CHROME
        self.tick(keeper)
        self.assertEqual(desktop.activated, [NOTES])

    def test_a_switch_back_that_does_not_take_is_retried_every_poll_and_logged_sparingly(self):
        desktop = Desktop(MAIL)
        desktop.activate = lambda pid: desktop.activated.append(pid) and False
        keeper = self.keeper(desktop)
        self.tick(keeper)
        desktop.now = CHROME
        for _ in range(40):                  # two seconds
            self.tick(keeper)
        self.assertEqual(desktop.activated, [MAIL] * 40)   # never gives up
        failed = [e for e in self.log if not e["restored"]]
        self.assertLessEqual(len(failed), 3)
        self.assertEqual(failed[0]["count"], 1)

    def test_a_window_that_shows_while_the_user_is_elsewhere_is_minimized_at_once(self):
        desktop = Desktop(MAIL)
        keeper = self.keeper(desktop)
        self.tick(keeper)
        self.assertEqual(self.hides, [])
        self.flags["shown"] = True           # Chrome un-minimized a window without coming forward
        self.tick(keeper)
        self.assertEqual(len(self.hides), 1)
        self.assertEqual(desktop.activated, [])

    def test_no_correction_during_the_captcha_wait_and_one_at_once_after_it(self):
        desktop = Desktop(MAIL)
        keeper = self.keeper(desktop, shown=True)
        self.tick(keeper)
        self.hides.clear()
        self.flags["suspended"] = True
        desktop.now = CHROME              # the CAPTCHA wait brings the window in front
        for _ in range(100):
            self.tick(keeper)
        desktop.now = NOTES               # the owner looks at Telegram meanwhile, then ticks
        self.tick(keeper)
        desktop.now = CHROME
        self.tick(keeper)
        self.assertEqual(desktop.activated, [])
        self.assertEqual(self.hides, [])  # never minimized during the wait
        self.flags["suspended"] = False   # the wait ended
        keeper.poll()
        self.assertEqual(desktop.activated, [NOTES])

    def test_the_everyday_chrome_is_never_corrected(self):
        desktop = Desktop(MAIL)
        keeper = self.keeper(desktop)
        self.tick(keeper)
        desktop.now = EVERYDAY_CHROME     # the user's own Chrome: a different process
        for _ in range(20):
            self.tick(keeper)
        self.assertEqual(desktop.activated, [])
        self.assertEqual(self.log, [])
        desktop.now = CHROME              # Sidedoor's Chrome then activates: back to the user's own Chrome
        self.tick(keeper)
        self.assertEqual(desktop.activated, [EVERYDAY_CHROME])

    def test_nothing_is_activated_without_an_app_to_return_to_or_a_known_chrome(self):
        desktop = Desktop(CHROME)
        keeper = self.keeper(desktop)
        self.tick(keeper)
        self.assertEqual(desktop.activated, [])
        self.assertEqual(self.hides, [])       # every window already minimized: no request
        keeper.chrome_pid = lambda: None   # Chrome not started yet, or the profile's lock names another program
        desktop.now = MAIL
        self.tick(keeper)
        desktop.now = CHROME
        self.tick(keeper)
        self.assertEqual(desktop.activated, [])

    def test_a_quit_app_falls_back_to_the_one_before_it_that_still_runs(self):
        desktop = Desktop(MAIL)
        keeper = self.keeper(desktop)
        running = {MAIL, NOTES}
        keeper.alive = lambda pid: pid in running
        self.tick(keeper)
        desktop.now = NOTES
        self.tick(keeper)
        running.discard(NOTES)            # the user quit Notes; Chrome comes forward
        desktop.now = CHROME
        self.tick(keeper)
        self.assertEqual(desktop.activated, [MAIL])
        self.assertIsNone(self.log[-1]["seen_ms"])
        running.clear()                   # nothing the user was in still runs: no activation, no guess
        desktop.now = CHROME
        self.tick(keeper)
        self.assertEqual(desktop.activated, [MAIL])

    def test_no_minimize_request_when_every_window_is_already_minimized(self):
        desktop = Desktop(CHROME)
        keeper = self.keeper(desktop, shown=False)
        for _ in range(20):               # Chrome in front, nothing to return to, windows minimized
            self.tick(keeper)
        self.assertEqual(self.hides, [])
        self.flags["shown"] = True
        self.tick(keeper)
        self.assertEqual(len(self.hides), 1)

    def test_a_failing_api_never_stops_the_guard(self):
        keeper = self.keeper(Desktop())
        keeper.front = mock.Mock(side_effect=RuntimeError("window server gone"))
        keeper.poll()
        self.assertEqual(self.log[-1]["decision"], "FOCUS_KEEPER_ERROR")
        keeper.log = mock.Mock(side_effect=OSError("disk full"))
        keeper.poll()                     # neither raises


class MacApi(unittest.TestCase):
    def test_lsappinfo_front_pid_and_setfront_by_pid(self):
        calls, state = [], {"front": "ASN:0x0-0x111:"}

        def run(argv, **kwargs):
            calls.append(argv)
            if argv[1] == "front":
                return subprocess.CompletedProcess(argv, 0, state["front"] + "\n", "")
            if argv[1] == "info":
                pid = {"ASN:0x0-0x111:": 900, "ASN:0x0-0x222:": 100}[argv[-1]]
                return subprocess.CompletedProcess(argv, 0, '"pid"=%d\n' % pid, "")
            if argv[1] == "setfront":
                state["front"] = "ASN:0x0-0x222:"
                return subprocess.CompletedProcess(argv, 0, "", "")
            raise AssertionError(argv)
        mac = browser_guard.MacFrontmost(run)
        self.assertEqual(mac.front(), 900)
        self.assertEqual(mac.front(), 900)
        self.assertEqual(sum(1 for argv in calls if argv[1] == "info"), 1)   # cached per ASN
        self.assertTrue(mac.activate(100))
        self.assertIn(["/usr/bin/lsappinfo", "setfront", "#100"], calls)
        self.assertEqual(mac.front(), 100)

    def test_failures_answer_none_or_false(self):
        def broken(argv, **kwargs):
            raise OSError("no lsappinfo")
        mac = browser_guard.MacFrontmost(broken)
        self.assertIsNone(mac.front())
        with mock.patch.dict(sys.modules, {"AppKit": None}):
            self.assertFalse(mac.activate(100))
        mac = browser_guard.MacFrontmost(lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, "", "x"))
        self.assertIsNone(mac.front())


def answer(windows):
    return {"result": {"content": [{"type": "text", "text": "### Result\n" + json.dumps(windows)}]}}


class Windows:
    def __init__(self):
        self.codes = []

    def __call__(self, method, params):
        code = params["arguments"]["code"]
        self.codes.append(code)
        if "windowState: 'minimized'" in code:
            return answer([{"windowId": 1, "state": "minimized", "was": "normal"}])
        return answer([{"windowId": 1, "left": 80, "top": 60}])

    def kinds(self):
        return ["min" if "windowState: 'minimized'" in code else
                ("front" if "bringToFront" in code else "on") for code in self.codes]


class GuardMinimizes(unittest.TestCase):
    def proxy(self, windows, child=("node",), **options):
        directory = scratch(self)
        with mock.patch.object(browser_guard.subprocess, "Popen"):
            proxy = browser_guard.Proxy("submit", list(child), audit=directory / "guard.jsonl", stdin=io.StringIO(""),
                                        stdout=io.StringIO(), offscreen=True, focus_keeper=False, **options)
        proxy.child_call = windows
        proxy.to_child = lambda message: None
        proxy.initialized = True
        proxy.audit_path = directory / "guard.jsonl"
        return proxy

    def audit(self, proxy):
        return [json.loads(line) for line in proxy.audit_path.read_text().splitlines()]

    def call(self, proxy, ident):
        proxy.handle({"jsonrpc": "2.0", "id": ident, "method": "tools/call",
                      "params": {"name": "browser_snapshot", "arguments": {}}})
        with proxy.idle:
            proxy.in_flight.discard(ident)

    def test_minimized_right_after_initialized(self):
        windows = Windows()
        proxy = self.proxy(windows)
        proxy.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(windows.kinds(), ["min"])
        self.call(proxy, 1)              # within the second: not again
        self.assertEqual(windows.kinds(), ["min"])
        self.assertEqual([e["decision"] for e in self.audit(proxy)], ["WINDOW_MINIMIZED", "FORWARDED"])

    def test_a_minimize_that_changes_nothing_is_logged_at_most_once_a_second(self):
        now = [10.0]

        def already(method, params):
            return answer([{"windowId": 1, "state": "minimized", "was": "minimized"}])
        proxy = self.proxy(already, clock=lambda: now[0])
        for _ in range(8):
            proxy.place_window()
            now[0] += 0.25
        entries = [e for e in self.audit(proxy) if e["decision"] == "WINDOW_MINIMIZED"]
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[1]["unchanged"], 4)

    def test_a_headed_launch_records_the_users_app_and_starts_the_keeper_before_chrome_starts(self):
        # Real Mac, 2026-10-24: `open -g` Chrome came to the front ~0.05-0.4 s after launch and the keeper,
        # started later, had nothing to return to.
        order = []

        class Keeper:
            def record_front(self_inner):
                order.append("record")

            def poll(self_inner):
                if "poll" not in order:
                    order.append("poll")
        keeper = Keeper()

        def launch(path):
            order.append("launch")
            return "http://127.0.0.1:53111"
        directory = scratch(self)
        with mock.patch.object(browser_guard.subprocess, "Popen"), \
                mock.patch.object(browser_guard, "FOCUS_POLL_SECONDS", 0.001):
            proxy = browser_guard.Proxy("submit", ["node"], audit=directory / "guard.jsonl", stdin=io.StringIO(""),
                                        stdout=io.StringIO(), offscreen=True, focus_keeper=keeper,
                                        background_chrome=str(directory), launch_background=launch)
        try:
            self.assertEqual(order[:2], ["record", "launch"])
            self.assertIsNotNone(proxy.keeper_thread)              # running before Chrome was started
            self.assertIs(proxy.start_focus_keeper(), proxy.keeper_thread)   # run() starts no second one
        finally:
            proxy.stopped.set()
            proxy.keeper_thread.join(2)

    def test_the_keepers_minimize_waits_for_the_browser_server(self):
        windows = Windows()
        proxy = self.proxy(windows)
        proxy.initialized = False
        with mock.patch.object(browser_guard.threading, "Thread") as thread:
            proxy.hide_now()
        thread.assert_not_called()
        self.assertEqual(windows.codes, [])

    def test_hide_code_leaves_minimized_windows_alone_and_never_brings_chrome_forward(self):
        code = browser_guard.hide_code(-32000, -32000, 1280, 900)
        self.assertIn("if (bounds.windowState === 'minimized') { placed.push", code)
        self.assertNotIn("bringToFront", code)
        self.assertNotIn("activateTarget", code)

    def test_the_keepers_minimize_runs_on_its_own_thread_once_at_a_time_and_never_during_the_wait(self):
        windows, now = Windows(), [10.0]
        proxy = self.proxy(windows, clock=lambda: now[0])
        threads = []
        with mock.patch.object(browser_guard.threading, "Thread",
                               side_effect=lambda target, daemon: threads.append(target) or mock.Mock()):
            proxy.hide_now()
            proxy.hide_now()                                  # one already running
            self.assertEqual(len(threads), 1)
            threads[0]()
            self.assertEqual(windows.kinds(), ["min"])
            proxy.hide_now()                                  # within HIDE_RETRY_SECONDS: not again
            now[0] += browser_guard.HIDE_RETRY_SECONDS
            proxy.hide_now()
            self.assertEqual(len(threads), 2)
            proxy.showing = True
            now[0] += 1.0
            proxy.hide_now()
            self.assertEqual(len(threads), 2)
        self.assertIn("ms", self.audit(proxy)[0])

    def test_the_captcha_wait_shows_the_window_and_the_keeper_is_suspended(self):
        windows = Windows()
        seen = []

        class Wait:
            def call(self_inner):
                seen.append((list(windows.kinds()), keeper.suspended()))
                return "RECHECK_NOW"
        proxy = self.proxy(windows, human_check_wait=Wait())
        desktop = Desktop(MAIL)
        keeper = browser_guard.FocusKeeper(lambda: CHROME, desktop.front, desktop.activate,
                                           suspended=lambda: proxy.showing, alive=lambda pid: True)
        proxy.focus_keeper = keeper
        threads = []
        with mock.patch.object(browser_guard.threading, "Thread",
                               side_effect=lambda target, daemon: threads.append(target) or mock.Mock()):
            proxy.call_human_check({"jsonrpc": "2.0", "id": 9})
        keeper.poll()
        desktop.now = CHROME
        keeper.poll()
        self.assertEqual(desktop.activated, [])            # suspended: no correction
        threads[0]()
        self.assertEqual(seen, [(["front"], True)])        # un-minimized on-screen and in front for the tick
        self.assertEqual(windows.kinds(), ["front", "min"])   # minimized again after the wait
        self.assertEqual(desktop.activated, [MAIL])        # and the front given back at once

    def test_the_session_chrome_is_found_by_its_profile_never_the_everyday_one(self):
        profile = scratch(self) / "profile"
        proxy = self.proxy(Windows(), child=("node", "cli.js", "--user-data-dir", str(profile), "--no-webmcp"))
        self.assertEqual(proxy.profile_path, str(profile))
        checks = []

        def holds(pid, path, run=None):
            checks.append(pid)
            return pid == CHROME
        with mock.patch.object(browser_runtime, "lock_holder", lambda path: CHROME), \
                mock.patch.object(browser_runtime, "holds_profile", holds):
            self.assertEqual(proxy.chrome_pid(), CHROME)
            self.assertEqual(proxy.chrome_pid(), CHROME)
        with mock.patch.object(browser_runtime, "lock_holder", lambda path: EVERYDAY_CHROME), \
                mock.patch.object(browser_runtime, "holds_profile", holds):
            self.assertIsNone(proxy.chrome_pid())          # a pid not on Sidedoor's profile is not it
            self.assertIsNone(proxy.chrome_pid())
        self.assertEqual(checks, [CHROME, EVERYDAY_CHROME])   # one ps per pid seen

    def test_the_keeper_runs_only_on_macos_with_the_flag(self):
        proxy = self.proxy(Windows())
        proxy.focus_keeper = None
        with mock.patch.object(browser_guard.sys, "platform", "win32"):
            self.assertIsNone(proxy.start_focus_keeper())
        proxy.offscreen = False
        with mock.patch.object(browser_guard.sys, "platform", "darwin"):
            self.assertIsNone(proxy.start_focus_keeper())
        proxy.offscreen, proxy.focus_keeper = True, mock.Mock()
        thread = proxy.start_focus_keeper()
        proxy.stopped.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
