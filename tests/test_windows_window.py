"""Round 8 on E15.T5b. (1) Windows: the window a person sees (the human check, the support email draft) lies
wholly inside the real screen, as the hidden window already fits it; the Mac's placement is unchanged.
(2) A hidden launch that failed is told to the live view at once: the card says it could not open, nothing
is left running. Fakes only: no Chrome starts."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import io
import json
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import browser_guard
import browser_runtime
import live_view
import live_view_fakes as fakes
import paths
import portable
import run_locks
import test_dashboard_overview as base
from test_browser_driver_review import scratch
from test_browser_headless import Browser, UA_141

SMALL = {"width": 1366, "height": 768, "scale": 1.0}        # a 1366x768 laptop at 100%
SCALED = {"width": 1280, "height": 720, "scale": 1.5}       # 1920x1080 at 150%
LARGE = {"width": 2560, "height": 1440, "scale": 1.0}
URL = "https://jobs.example.test/apply"


def inside(screen, left, top, width, height):
    return left >= 0 and top >= 0 and left + width <= screen["width"] and top + height <= screen["height"]


def bounds(code):
    """(left, top, width, height) a placement code sets."""
    return tuple(int(value) for value in re.search(
        r"bounds: \{ left: (-?\d+), top: (-?\d+), width: (\d+), height: (\d+) \}", code).groups())


def switches(command):
    position = next(item for item in command if item.startswith("--window-position=")).split("=")[1].split(",")
    size = next(item for item in command if item.startswith("--window-size=")).split("=")[1].split(",")
    return int(position[0]), int(position[1]), int(size[0]), int(size[1])


class VisibleWindow(unittest.TestCase):
    def setUp(self):
        for patcher in (mock.patch.object(browser_runtime, "BACKGROUND_ALLOWED", True),
                        mock.patch.object(browser_runtime.sys, "platform", "win32")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_window_lies_wholly_inside_the_real_screen(self):
        for screen in (SMALL, SCALED):
            box = browser_runtime.visible_window(screen)
            self.assertTrue(inside(screen, *box), (screen, box))
        self.assertEqual(browser_runtime.visible_window(SMALL), (80, 0, 1280, 768))
        self.assertEqual(browser_runtime.visible_window(SCALED), (0, 0, 1280, 720))
        # A large display, or one that could not be read, keeps the usual place and size.
        usual = (*browser_runtime.ONSCREEN_POSITION, *browser_runtime.WINDOW_SIZE)
        self.assertEqual(browser_runtime.visible_window(LARGE), usual)
        self.assertEqual(browser_runtime.visible_window(None), usual)

    def test_chrome_is_started_in_a_window_that_fits(self):
        for screen in (SMALL, SCALED):
            self.assertTrue(inside(screen, *switches(browser_runtime.windows_command("chrome.exe", "/p", screen=screen))))
        self.assertEqual(switches(browser_runtime.windows_command("chrome.exe", "/p", screen=LARGE)), (80, 60, 1280, 900))
        self.assertEqual(switches(browser_runtime.windows_command("chrome.exe", "/p")), (80, 60, 1280, 900))
        # The support email's window: the display is read when the window is started.
        profile = scratch(self) / "profile"
        seen = []

        def popen(argv, **options):
            seen.append(argv)
            (profile / browser_runtime.DEVTOOLS_PORT_FILE).write_text("53999\n/devtools/browser/x")
            return mock.Mock(pid=777)
        self.addCleanup(browser_runtime._STARTED.clear)
        with mock.patch.object(portable, "primary_display", lambda api=None: (1280, 720, 1.5)), \
                mock.patch.object(portable, "contain", lambda chrome: None), \
                mock.patch.object(portable, "kill_tree", lambda pid: None):
            browser_runtime.launch_windows("chrome.exe", profile, popen=popen, url="https://mail.example.test/draft")
        self.assertTrue(inside(SCALED, *switches(seen[0])))
        self.assertEqual(seen[0][-1], "https://mail.example.test/draft")

    def test_the_mac_keeps_its_usual_place_and_size(self):
        with mock.patch.object(browser_runtime.sys, "platform", "darwin"):
            self.assertEqual(browser_runtime.visible_window(SMALL), (80, 60, 1280, 900))


class LiveViewLearnsAtOnce(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home, self.profile = Path(self.temp.name) / "home", Path(self.temp.name) / "profile"
        self.home.mkdir()
        self.chrome = fakes.FakeChrome({"MAIN": {"targetId": "MAIN", "type": "page", "url": URL, "title": "Apply"}}).start()
        self.addCleanup(self.chrome.stop)

    def lock_free(self):
        found = []

        def probe():
            with run_locks.browser_session(self.home) as held:
                found.append(held)
        thread = threading.Thread(target=probe)
        thread.start()
        thread.join()
        return bool(found and found[0])

    async def test_a_failed_hidden_launch_is_answered_promptly_and_nothing_is_left_running(self):
        base.database_with_schema(self.home).close()
        view = live_view.LiveView(
            root=self.home, config={"browser_driver": {"profile": str(self.profile)}},
            guard_command=fakes.guard_command(self.chrome, "--hidden-failed"), is_supported=lambda config: True,
            leave_grace_seconds=30, lock_retry_seconds=0.2, endpoint_wait_seconds=60)
        began = time.monotonic()
        # A Chrome still on the profile when the guard has stopped (there should be none) is ended too.
        with mock.patch.object(browser_runtime, "chrome_running", return_value=True), \
                mock.patch.object(browser_runtime, "end_session", return_value=True) as end_session:
            refused = await view.start(view.offer("job-1", URL, "sign_in"))
        self.assertLess(time.monotonic() - began, 20)                  # not the 60 s wait for an endpoint
        self.assertEqual((refused.reason, refused.words, refused.final),
                         (live_view.NOT_SHOWN, live_view.NO_BACKGROUND_WORDS, True))
        self.assertEqual(view.guard_command.starts, 1)                 # no second try that could open a window
        self.assertEqual(self.chrome.methods(), [])                    # no browser call was ever made
        end_session.assert_called()                                    # whatever Chrome there was is ended
        self.assertTrue(self.lock_free())                              # and the browser is free again
        audit = paths.reports(self.home) / live_view.FOLDER / "session" / browser_runtime.GUARD_AUDIT
        rows = [json.loads(line)["decision"] for line in audit.read_text().splitlines()]
        self.assertEqual(rows, ["HEADLESS_LAUNCH_FAILED", "STOPPED"])  # the guard itself was stopped
        self.assertNotIn("FORWARDED", rows)                            # so @playwright/mcp was never asked for a page

    def test_the_guards_record_of_a_failed_hidden_launch_means_no_endpoint_will_come(self):
        folder = Path(self.temp.name)
        guard = live_view.Guard(["guard"], folder)
        self.assertIsNone(guard.endpoint())                            # nothing yet: keep waiting
        (folder / browser_runtime.GUARD_AUDIT).write_text(json.dumps({"decision": "HEADLESS_LAUNCH_FAILED"}) + "\n")
        self.assertIs(guard.endpoint(), False)
        (folder / browser_runtime.GUARD_AUDIT).write_text(json.dumps(
            {"decision": "BACKGROUND_LAUNCHED", "endpoint": "http://127.0.0.1:5", "headless": True}) + "\n")
        self.assertEqual(guard.endpoint(), "http://127.0.0.1:5")       # control: a hidden Chrome is attached to


if __name__ == "__main__":
    unittest.main()
