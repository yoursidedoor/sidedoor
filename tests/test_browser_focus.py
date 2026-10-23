"""Sidedoor's Chrome never takes the user's screen (owner, 2026-10-22: "is it possible to stop the
sidedoor browser from just snatching control of my screen?", option a). Offline."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import browser_guard  # noqa: E402
import browser_runtime  # noqa: E402
import setup_steps  # noqa: E402
from test_browser_driver_review import InProcessGuard, scratch  # noqa: E402

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
SNAPSHOT = "- button \"Apply\" [ref=e1] [cursor=pointer]\n"


class LaunchIsNeverActivated(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(browser_runtime, "BACKGROUND_ALLOWED", True)   # off for the rest of the suite
        patcher.start()
        self.addCleanup(patcher.stop)

    @unittest.skipIf(os.name == "nt", "macOS only (/usr/bin/open -g); Chrome's path is a POSIX path")
    def test_the_launch_command_is_open_dash_g(self):
        profile = Path("/private/tmp/sidedoor-profile")
        command = browser_runtime.background_command(browser_runtime.chrome_app(CHROME), profile)
        self.assertEqual(command[:6], ["/usr/bin/open", "-g", "-n", "-a", "/Applications/Google Chrome.app", "--args"])
        self.assertIn("--user-data-dir=" + str(profile), command)   # end_session finds it by this form
        self.assertIn("--remote-debugging-port=0", command)
        for switch in ("--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
                       "--disable-background-timer-throttling"):
            self.assertIn(switch, command)     # the page keeps working behind other windows
        for ignored in ("--use-mock-keychain", "--enable-automation"):   # LAUNCH_CONFIG's ignoreDefaultArgs
            self.assertNotIn(ignored, command)
        self.assertNotIn("--start-maximized", command)
        self.assertEqual(command[-1], "about:blank")

    def test_background_mode_is_macos_on_the_real_headed_profile_only(self):
        self.assertTrue(browser_runtime.background_launch({}, "darwin"))
        self.assertFalse(browser_runtime.background_launch({}, "win32"))
        self.assertFalse(browser_runtime.background_launch({"browser_driver": {"headless": True}}, "darwin"))
        self.assertFalse(browser_runtime.background_launch({"browser_driver": {"isolated": True}}, "darwin"))
        with mock.patch.object(browser_runtime, "BACKGROUND_ALLOWED", False):    # the test suite's setting
            self.assertFalse(browser_runtime.background_launch({}, "darwin"))

    def test_guard_command_hands_the_profile_to_the_guard_on_macos(self):
        directory = scratch(self)
        config = {"browser_driver": {"profile": str(directory / "profile")}}
        with mock.patch.object(browser_runtime.sys, "platform", "darwin"):
            argv = browser_runtime.guard_command(config, directory, "inspect")
        guard, mcp = argv[:argv.index("--")], argv[argv.index("--") + 1:]
        self.assertEqual(guard[guard.index("--background-chrome") + 1], str(directory / "profile"))
        self.assertNotIn("--user-data-dir", mcp)
        with mock.patch.object(browser_runtime.sys, "platform", "win32"):
            argv = browser_runtime.guard_command(config, directory, "inspect")
        self.assertNotIn("--background-chrome", argv)
        self.assertEqual(argv[argv.index("--user-data-dir") + 1], str(directory / "profile"))

    def test_launch_reads_the_devtools_port_chrome_writes(self):
        profile = scratch(self) / "profile"
        seen = []

        def run(argv, **kwargs):
            seen.append(argv)
            (profile / browser_runtime.DEVTOOLS_PORT_FILE).write_text("53111\n/devtools/browser/x\n")
            return subprocess.CompletedProcess(argv, 0, "", "")
        profile.mkdir()
        (profile / browser_runtime.DEVTOOLS_PORT_FILE).write_text("1\n")   # stale: removed before the launch
        self.assertEqual(browser_runtime.launch_background(Path("/Applications/Google Chrome.app"), profile, run),
                         "http://127.0.0.1:53111")
        self.assertEqual(seen[0][:3], ["/usr/bin/open", "-g", "-n"])
        failed = lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, "", "LSOpenURLs failed")
        with self.assertRaises(OSError):
            browser_runtime.launch_background(Path("/Applications/Google Chrome.app"), profile, failed)
        silent = lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "", "")
        clock = iter(range(0, 100, 10))
        stopped = []
        with self.assertRaises(OSError):
            browser_runtime.launch_background(Path("/x.app"), profile, silent, timeout=30,
                                              sleep=lambda s: None, clock=lambda: next(clock), stop=stopped.append)
        # CK2: the late Chrome `open` started on this profile is stopped before the fallback launch.
        self.assertEqual(stopped, [{"browser_driver": {"profile": str(profile)}}])

    def test_the_wait_is_long_and_polled(self):
        self.assertGreaterEqual(browser_runtime.BACKGROUND_LAUNCH_SECONDS, 60)
        profile = scratch(self) / "profile"
        profile.mkdir()
        port_file = profile / browser_runtime.DEVTOOLS_PORT_FILE
        writes = iter(["", "53", "53111\n", "53111\n/devtools/browser/x\n"])

        def sleep(seconds):     # Chrome writes the file a piece at a time between polls
            port_file.write_text(next(writes))
        stopped = []
        launched = lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "", "")
        endpoint = browser_runtime.launch_background(Path("/x.app"), profile, launched, sleep=sleep,
                                                     stop=stopped.append)
        # CK4: neither "53" nor a lone first line is used; only the complete two-line file.
        self.assertEqual(endpoint, "http://127.0.0.1:53111")
        self.assertEqual(stopped, [])

    def test_guard_attaches_to_the_background_chrome_and_falls_back_on_failure(self):
        out = io.StringIO()
        with mock.patch.object(browser_guard.subprocess, "Popen") as popen:
            proxy = browser_guard.Proxy("inspect", ["node", "cli.js"], stdin=io.StringIO(""), stdout=out,
                                        background_chrome="/p", launch_background=lambda path: "http://127.0.0.1:5")
            self.assertEqual(popen.call_args[0][0], ["node", "cli.js", "--cdp-endpoint", "http://127.0.0.1:5"])
            self.assertEqual(proxy.background_chrome, "/p")

            def broken(path):
                raise OSError("no port")
            proxy = browser_guard.Proxy("inspect", ["node", "cli.js"], stdin=io.StringIO(""), stdout=out,
                                        background_chrome="/p", launch_background=broken)
            self.assertEqual(popen.call_args[0][0], ["node", "cli.js", "--user-data-dir", "/p"])
            self.assertIsNone(proxy.background_chrome)   # Playwright's own Chrome: it closes it itself

    def test_the_guard_stops_the_chrome_it_started_on_its_profile_only(self):
        with mock.patch.object(browser_guard.subprocess, "Popen"), \
                mock.patch.object(browser_runtime, "end_session") as end:
            proxy = browser_guard.Proxy("inspect", ["node"], stdin=io.StringIO(""), stdout=io.StringIO(),
                                        background_chrome="/p", launch_background=lambda path: "http://127.0.0.1:5")
            proxy.stop_background_chrome()
        end.assert_called_once_with({"browser_driver": {"profile": "/p"}})


class GuardRunStopsItsChrome(unittest.TestCase):
    def test_run_stops_the_background_chrome_even_when_the_loop_fails(self):
        with mock.patch.object(browser_guard.subprocess, "Popen"):
            proxy = browser_guard.Proxy("inspect", ["node"], stdin=io.StringIO(""), stdout=io.StringIO(),
                                        background_chrome="/p", launch_background=lambda path: "http://127.0.0.1:5")
        calls = []
        with mock.patch.object(proxy, "loop", side_effect=RuntimeError("boom")), \
                mock.patch.object(proxy, "stop_child", side_effect=lambda: calls.append("child")), \
                mock.patch.object(proxy, "stop_background_chrome", side_effect=lambda: calls.append("chrome")), \
                mock.patch.object(browser_guard.threading, "Thread"):
            with self.assertRaises(RuntimeError):
                proxy.run()
        self.assertEqual(calls, ["child", "chrome"])


class NothingBringsChromeToFront(unittest.TestCase):
    def test_tab_select_is_allowed_and_bring_to_front_is_refused_in_every_operation(self):
        for operation in ("inspect", "submit", "linkedin", "webmail"):
            guard = InProcessGuard(self, operation, SNAPSHOT)
            # CK1: back to the application tab after an emailed code, sign-in popups, one-time Apply links.
            self.assertFalse(guard.client.call("browser_tabs", {"action": "select", "index": 1})[0], operation)
            if operation != "webmail":                         # the code tools are not offered there
                error, text = guard.client.call("browser_run_code_unsafe",
                                                {"code": "async (page) => { await page.bringToFront(); }"})
                self.assertTrue(error)
                self.assertIn(browser_guard.FOCUS_CODE, text)
            self.assertFalse(guard.client.call("browser_tabs", {"action": "new", "url": "https://example.com"})[0])
            self.assertFalse(guard.client.call("browser_tabs", {"action": "list"})[0])
            names = [(c.get("name"), (c.get("arguments") or {}).get("action")) for c in guard.forwarded()]
            # The fake runtime records the arguments only: the select reached @playwright/mcp.
            self.assertIn("select", [action for _, action in names])
            self.assertNotIn("bringToFront", json.dumps(guard.forwarded()))
            refused = [json.loads(line) for line in (guard.directory / "guard.jsonl").read_text().splitlines()
                       if json.loads(line).get("code") == browser_guard.FOCUS_CODE]
            self.assertEqual(bool(refused), operation != "webmail")


class SignInStillComesForward(unittest.TestCase):
    def test_step_8_opens_chrome_directly_so_it_comes_to_the_front(self):
        root = scratch(self)
        launched = []

        def launch(argv, **kwargs):
            launched.append(argv)
            return mock.Mock(pid=1, poll=lambda: None, wait=lambda: None)
        session = setup_steps.BrowserSession(root=root, profile=root / "profile", find_chrome=lambda: CHROME,
                                             launch=launch, platform="darwin")
        with mock.patch.object(setup_steps, "_Hold") as hold:
            hold.return_value.take.return_value = True
            self.assertEqual(session.open(), "opened")
        self.assertEqual(launched[0][0], CHROME)               # a direct exec activates: the user asked for it
        self.assertNotIn("-g", launched[0])


if __name__ == "__main__":
    unittest.main()
