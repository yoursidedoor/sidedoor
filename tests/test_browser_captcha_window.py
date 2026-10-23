"""fg1-captcha (follow-up to FG1 b): the CAPTCHA wait's on-screen window is never hidden by a concurrent
call, a failed on-screen placement is retried during the wait, and one failing window never stops the
others. Offline: the windows and CDP are fakes; no Chrome, no Node."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import io
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import application_browser  # noqa: E402
import browser_guard  # noqa: E402
from test_browser_driver_review import scratch  # noqa: E402

OFF = "left: -32000, top: -32000"
ON = "left: 80, top: 60"


def answer(windows):
    return {"result": {"content": [{"type": "text", "text": "### Result\n" + json.dumps(windows)}]}}


class Windows:
    """Fake placements: `script` is a list of answers (lists of window entries, or None for a timeout),
    used in order; after it runs out every placement succeeds."""

    def __init__(self, script=()):
        self.script, self.codes = list(script), []

    def __call__(self, method, params):
        code = params["arguments"]["code"]
        self.codes.append(code)
        if self.script:
            entries = self.script.pop(0)
            return None if entries is None else answer(entries)
        if OFF in code:   # FG2 = a: hide_code parks at the off-screen bounds, then minimizes
            return answer([{"windowId": 1, "state": "minimized", "was": "normal"}])
        return answer([{"windowId": 1, "left": 80, "top": 60}])

    def kinds(self):
        return ["off" if OFF in code else "on" for code in self.codes]


class Base(unittest.TestCase):
    def proxy(self, windows, wait, sleeps=None):
        directory = scratch(self)
        with mock.patch.object(browser_guard.subprocess, "Popen"):
            proxy = browser_guard.Proxy("submit", ["node"], audit=directory / "guard.jsonl", stdin=io.StringIO(""),
                                        stdout=io.StringIO(), offscreen=True, human_check_wait=wait,
                                        sleep=(sleeps.append if sleeps is not None else lambda s: None))
        proxy.child_call = windows
        proxy.to_child = lambda message: None
        proxy.audit_path = directory / "guard.jsonl"
        return proxy

    def audit(self, proxy):
        return [json.loads(line) for line in proxy.audit_path.read_text().splitlines()]

    def start(self, proxy):
        """call_human_check with its threads captured: returns their targets, in start order."""
        threads = []
        with mock.patch.object(browser_guard.threading, "Thread",
                               side_effect=lambda target, daemon: threads.append(target) or mock.Mock()):
            proxy.call_human_check({"jsonrpc": "2.0", "id": 9})
        return threads

    def forward(self, proxy, ident):
        proxy.handle({"jsonrpc": "2.0", "id": ident, "method": "tools/call",
                      "params": {"name": "browser_snapshot", "arguments": {}}})
        with proxy.idle:
            proxy.in_flight.discard(ident)


class Moderate1NoRace(Base):
    def test_showing_is_set_before_the_wait_thread_runs_so_a_concurrent_call_never_hides_the_window(self):
        windows = Windows()
        wait = mock.Mock(call=lambda: "done")
        proxy = self.proxy(windows, wait)
        threads = self.start(proxy)
        self.assertTrue(proxy.showing)            # before the thread has run at all
        self.forward(proxy, 1)                    # a call handled in that gap
        self.assertEqual(windows.kinds(), [])     # nothing pushed the window off-screen
        threads[0]()
        self.assertEqual(windows.kinds(), ["on", "off"])

    def test_the_off_screen_placement_re_checks_under_the_lock(self):
        windows = Windows()
        proxy = self.proxy(windows, mock.Mock())
        entered, release = threading.Event(), threading.Event()

        def slow(method, params):          # a placement already holding the lock when the wait starts
            entered.set()
            release.wait(5)
            return windows(method, params)
        proxy.child_call = slow
        first = threading.Thread(target=proxy.place_window)
        first.start()
        entered.wait(5)
        proxy.showing = True                       # the wait starts meanwhile
        second = threading.Thread(target=proxy.place_window)   # e.g. keep_offscreen that passed its check
        second.start()
        release.set()
        first.join(5)
        second.join(5)
        self.assertEqual(windows.kinds(), ["off"])   # the second, now under the lock, does not run
        proxy.showing = False
        self.assertIsNone(proxy.place_window(onscreen=True))   # nor an on-screen one after the wait ended
        self.assertEqual(windows.kinds(), ["off"])


class Moderate2Retries(Base):
    def test_a_failed_on_screen_placement_is_retried_during_the_wait(self):
        windows = Windows([None, [{"error": "No target"}]])     # a timeout, then a window that throws
        sleeps = []
        proxy = self.proxy(windows, mock.Mock(call=lambda: "done"), sleeps)
        threads = self.start(proxy)
        threads_in_run = []
        with mock.patch.object(browser_guard.threading, "Thread",
                               side_effect=lambda target, daemon: threads_in_run.append(target) or mock.Mock()):
            # Run the wait's body up to the retry thread's start, with the wait itself still going.
            proxy.human_check_wait.call = lambda: (threads_in_run[0](), "done")[1]
            threads[0]()
        self.assertEqual(windows.kinds(), ["on", "on", "on", "off"])
        self.assertEqual(sleeps, list(browser_guard.ONSCREEN_RETRY_SECONDS[:2]))
        decisions = [entry["decision"] for entry in self.audit(proxy)]
        self.assertEqual(decisions.count("WINDOW_PLACEMENT_FAILED"), 2)
        self.assertIn("WINDOW_ONSCREEN", decisions)
        self.assertFalse(proxy.showing)

    def test_every_retry_failing_is_recorded_and_retries_stop_when_the_wait_ends(self):
        windows = Windows([None] * 10)
        proxy = self.proxy(windows, mock.Mock())
        proxy.showing = True
        proxy.retry_onscreen()
        self.assertEqual(len(windows.codes), len(browser_guard.ONSCREEN_RETRY_SECONDS))
        gave_up = [entry for entry in self.audit(proxy) if entry.get("gave_up")]
        self.assertEqual([entry["decision"] for entry in gave_up], ["WINDOW_PLACEMENT_FAILED"])
        windows = Windows([None] * 10)
        proxy = self.proxy(windows, mock.Mock())
        proxy.showing = True
        proxy.sleep = lambda seconds: setattr(proxy, "showing", False)   # the wait ends during the first pause
        proxy.retry_onscreen()
        self.assertEqual(windows.codes, [])

    def test_one_window_that_throws_never_stops_the_others(self):
        code = browser_guard.place_code(80, 60, 1280, 900, front=True)
        self.assertIn("catch (e)", code)
        self.assertLess(code.index("catch (e)"), code.index("bringToFront"))   # per window, inside the loop
        self.assertIn("try { await page.bringToFront(); }", code)
        windows = Windows([[{"error": "boom"}, {"windowId": 2, "left": 80, "top": 60},
                            {"error": "nofront", "front": True}]])
        proxy = self.proxy(windows, mock.Mock())
        proxy.showing = True
        self.assertEqual(proxy.place_window(onscreen=True), [{"windowId": 2, "left": 80, "top": 60}])
        entry = [e for e in self.audit(proxy) if e["decision"] == "WINDOW_ONSCREEN"][0]
        self.assertEqual(len(entry["errors"]), 2)

    def test_the_telegram_message_says_to_click_chrome_in_the_dock(self):
        sent = []
        root = scratch(self)
        context = root / "human_check_context.json"
        context.write_text(json.dumps({"root": str(root), "request_id": "r1", "directory": str(root),
                                       "job_key": "acme:1", "employer": "Acme", "role": "Engineer"}))
        wait = application_browser.HumanCheckWait(context)
        with mock.patch.object(application_browser.sqlite3, "connect"), \
                mock.patch.object(application_browser.desktop_adapter, "extend_for_human_check", return_value=True), \
                mock.patch.object(application_browser.applications, "notify",
                                  side_effect=lambda database, key, text, now: sent.append(text)), \
                mock.patch("notifications.dispatch"):
            wait.begin(wait.clock())
        self.assertIn("If you don't see the window, click Chrome in the Dock.", sent[0])


if __name__ == "__main__":
    unittest.main()
