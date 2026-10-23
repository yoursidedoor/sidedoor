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
    def keeper(self, desktop, busy=False, suspended=None):
        self.time, self.log = [0.0], []
        flags = {"busy": busy, "suspended": False} if suspended is None else suspended
        self.flags = flags
        return browser_guard.FocusKeeper(lambda: CHROME, desktop.front, desktop.activate,
                                         busy=lambda now: flags["busy"], suspended=lambda: flags["suspended"],
                                         log=lambda **entry: self.log.append(entry), clock=lambda: self.time[0])

    def tick(self, keeper, seconds=browser_guard.FOCUS_POLL_SECONDS):
        self.time[0] += seconds
        keeper.poll()

    def test_chrome_activating_itself_is_undone_within_one_poll(self):
        desktop = Desktop(MAIL)
        keeper = self.keeper(desktop, busy=True)
        self.tick(keeper)                    # the user is in Mail
        desktop.now = CHROME                 # a new page activates Sidedoor's Chrome
        self.tick(keeper)
        self.assertEqual(desktop.activated, [MAIL])
        self.assertEqual(desktop.now, MAIL)
        self.assertEqual([e["decision"] for e in self.log], ["FOCUS_RESTORED"])
        self.assertEqual(self.log[0]["to_pid"], MAIL)
        # Repeated activations during calls (a page created or selected every second) are undone each
        # time; the log is rate-limited.
        for _ in range(5):
            desktop.now = CHROME
            self.tick(keeper, 1.0)
        self.assertEqual(desktop.activated, [MAIL] * 6)
        self.assertFalse(keeper.watching)
        self.assertEqual([e["decision"] for e in self.log], ["FOCUS_RESTORED"])
        self.time[0] += browser_guard.FOCUS_LOG_SECONDS - 5.0
        desktop.now = CHROME
        keeper.poll()
        self.assertEqual(self.log[-1]["count"], 6)   # the five unlogged ones plus this one

    def test_the_last_app_the_user_used_is_the_one_restored(self):
        desktop = Desktop(MAIL)
        keeper = self.keeper(desktop, busy=True)
        self.tick(keeper)
        desktop.now = NOTES
        self.tick(keeper)
        desktop.now = CHROME
        self.tick(keeper)
        self.assertEqual(desktop.activated, [NOTES])

    def test_a_user_who_stays_on_chrome_is_not_fought(self):
        desktop = Desktop(MAIL)
        desktop.activate = lambda pid: desktop.activated.append(pid) and False   # the switch back does not take
        keeper = self.keeper(desktop, busy=True)
        self.tick(keeper)
        desktop.now = CHROME
        self.tick(keeper)
        self.assertEqual(desktop.activated, [MAIL])
        for _ in range(int(browser_guard.FOCUS_STAY_SECONDS / browser_guard.FOCUS_POLL_SECONDS) + 1):
            self.tick(keeper)
        self.assertTrue(keeper.watching)
        self.assertEqual(desktop.activated, [MAIL])   # one correction, never a fight
        self.assertIn("FOCUS_USER_WATCHING", [e["decision"] for e in self.log])
        for _ in range(20):
            self.tick(keeper)
        self.assertEqual(desktop.activated, [MAIL])
        # The user switches away: the keeper resumes.
        desktop.now = NOTES
        self.tick(keeper)
        self.assertFalse(keeper.watching)
        self.assertEqual(self.log[-1]["decision"], "FOCUS_KEEPER_RESUMED")
        desktop.now = CHROME
        self.tick(keeper)
        self.assertEqual(desktop.activated, [MAIL, NOTES])

    def test_corrections_that_do_not_hold_stop_instead_of_fighting(self):
        desktop = Desktop(MAIL)
        desktop.activate = lambda pid: desktop.activated.append(pid) or True   # accepted, yet Chrome stays
        keeper = self.keeper(desktop, busy=True)
        self.tick(keeper)
        desktop.now = CHROME
        for _ in range(10):
            self.tick(keeper)
        self.assertEqual(len(desktop.activated), browser_guard.FOCUS_MAX_CORRECTIONS)
        self.assertTrue(keeper.watching)

    def test_a_user_clicking_chrome_again_after_a_correction_keeps_it(self):
        desktop = Desktop(MAIL)
        keeper = self.keeper(desktop, busy=False)
        self.tick(keeper)
        desktop.now = CHROME              # the user clicks Chrome in the Dock
        self.tick(keeper)
        self.assertEqual(desktop.activated, [MAIL])
        self.tick(keeper, 1.0)
        desktop.now = CHROME              # and clicks it again: no call running, so it is the user
        self.tick(keeper)
        self.assertTrue(keeper.watching)
        self.assertEqual(desktop.activated, [MAIL])
        # Chrome's own repeated activation while a call runs is not taken for the user.
        desktop2 = Desktop(MAIL)
        keeper2 = self.keeper(desktop2, busy=True)
        self.tick(keeper2)
        for _ in range(3):
            desktop2.now = CHROME
            self.tick(keeper2)
        self.assertFalse(keeper2.watching)
        self.assertEqual(desktop2.activated, [MAIL] * 3)

    def test_no_correction_during_the_captcha_wait_and_one_after_it(self):
        desktop = Desktop(MAIL)
        keeper = self.keeper(desktop, busy=False)
        self.tick(keeper)
        self.flags["suspended"] = True
        desktop.now = CHROME              # the CAPTCHA wait brings the window in front
        for _ in range(100):              # 20 s with the owner on the window
            self.tick(keeper)
        self.assertEqual(desktop.activated, [])
        self.assertFalse(keeper.watching)
        self.flags["suspended"] = False   # the wait ended
        self.tick(keeper)
        self.assertEqual(desktop.activated, [MAIL])

    def test_the_everyday_chrome_is_never_corrected(self):
        desktop = Desktop(MAIL)
        keeper = self.keeper(desktop, busy=True)
        self.tick(keeper)
        desktop.now = EVERYDAY_CHROME     # the user's own Chrome: a different process
        for _ in range(20):
            self.tick(keeper)
        self.assertEqual(desktop.activated, [])
        self.assertEqual(self.log, [])
        desktop.now = CHROME              # Sidedoor's Chrome then activates: back to the user's own Chrome
        self.tick(keeper)
        self.assertEqual(desktop.activated, [EVERYDAY_CHROME])

    def test_nothing_is_done_without_an_app_to_return_to_or_a_known_chrome(self):
        desktop = Desktop(CHROME)
        keeper = self.keeper(desktop, busy=True)
        self.tick(keeper)
        self.assertEqual(desktop.activated, [])
        keeper.chrome_pid = lambda: None   # Chrome not started yet, or the profile's lock names another program
        desktop.now = MAIL
        self.tick(keeper)
        desktop.now = CHROME
        self.tick(keeper)
        self.assertEqual(desktop.activated, [])

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

    def test_hide_code_leaves_minimized_windows_alone_and_never_brings_chrome_forward(self):
        code = browser_guard.hide_code(-32000, -32000, 1280, 900)
        self.assertIn("if (bounds.windowState === 'minimized') { placed.push", code)
        self.assertNotIn("bringToFront", code)
        self.assertNotIn("activateTarget", code)

    def test_a_user_watching_gets_the_window_on_screen_once_and_never_minimized(self):
        windows = Windows()
        keeper = mock.Mock(watching=True)
        proxy = self.proxy(windows)
        proxy.focus_keeper = keeper
        self.call(proxy, 1)
        self.call(proxy, 2)
        self.assertEqual(windows.kinds(), ["on"])            # on-screen, not in front: the user chose it
        self.assertIn("WINDOW_SHOWN_FOR_USER", [e["decision"] for e in self.audit(proxy)])
        keeper.watching = False                             # the user switched away
        self.call(proxy, 3)
        self.assertEqual(windows.kinds(), ["on", "min"])

    def test_the_captcha_wait_shows_the_window_and_the_keeper_is_suspended(self):
        windows = Windows()
        seen = []

        class Wait:
            def call(self_inner):
                seen.append((list(windows.kinds()), keeper.suspended()))
                return "RECHECK_NOW"
        proxy = self.proxy(windows, human_check_wait=Wait())
        desktop = Desktop(MAIL)
        keeper = browser_guard.FocusKeeper(lambda: CHROME, desktop.front, desktop.activate, busy=proxy.busy,
                                           suspended=lambda: proxy.showing)
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

    def test_busy_while_a_call_runs_and_just_after(self):
        now = [10.0]
        proxy = self.proxy(Windows(), clock=lambda: now[0])
        self.assertFalse(proxy.busy(now[0]))
        proxy.in_flight.add(1)
        self.assertTrue(proxy.busy(now[0]))
        proxy.in_flight.clear()
        proxy.last_answer_at = now[0]
        now[0] += 1.0
        self.assertTrue(proxy.busy(now[0]))
        now[0] += 1.0
        self.assertFalse(proxy.busy(now[0]))


if __name__ == "__main__":
    unittest.main()
