"""Headless sessions (owner, 2026-10-24: "sometimes the browser still comes to the front and doesn't
return me to my last screen", option A). Offline: the Chrome bundle is a fake folder with an Info.plist,
launches and CDP are fakes; no Chrome, no Node."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import datetime as dt
import io
import json
from pathlib import Path
import plistlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import application_browser  # noqa: E402
import browser_guard  # noqa: E402
import browser_runtime  # noqa: E402
import setup_steps  # noqa: E402
from test_browser_driver_review import scratch  # noqa: E402

UA_141 = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/141.0.0.0 Safari/537.36")


def fake_chrome(testcase, version="141.0.7390.123"):
    """A fake `Google Chrome.app` whose Info.plist names `version` (None: no plist); returns the binary."""
    app = scratch(testcase) / "Google Chrome.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    if version is not None:
        with open(app / "Contents" / "Info.plist", "wb") as stream:
            plistlib.dump({"CFBundleShortVersionString": version}, stream)
    return str(app / "Contents" / "MacOS" / "Google Chrome")


class Composition(unittest.TestCase):
    def setUp(self):
        for patcher in (mock.patch.object(browser_runtime, "BACKGROUND_ALLOWED", True),
                        mock.patch.object(browser_runtime.sys, "platform", "darwin")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.binary = fake_chrome(self)
        patcher = mock.patch.object(setup_steps, "chrome_path", lambda platform=None, exists=None: self.binary)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.directory = scratch(self)

    def compose(self, operation, **driver):
        config = {"browser_driver": dict(profile=str(self.directory / "profile"), **driver)}
        argv = browser_runtime.guard_command(config, self.directory, operation)
        guard, mcp = argv[:argv.index("--")], argv[argv.index("--") + 1:]
        launch = json.loads((self.directory / "browser-config.json").read_text())
        return guard, mcp, launch["browser"]["launchOptions"].get("args")

    def test_every_operation_runs_headless_by_default_with_the_installed_chromes_user_agent(self):
        self.assertEqual(browser_runtime.headed_user_agent(self.binary), UA_141)
        for operation in set(browser_guard.OPERATIONS) - set(browser_runtime.SHOWN_OPERATIONS):
            guard, mcp, args = self.compose(operation)
            self.assertEqual(guard[guard.index("--headless-user-agent") + 1], UA_141, operation)
            self.assertNotIn("--offscreen", guard)                # no minimizing, no focus keeper
            self.assertIn("--background-chrome", guard)           # the guard owns the Chrome (CAPTCHA reopen)
            self.assertNotIn("--user-data-dir", mcp)
            self.assertNotIn("--headless", mcp)                   # Playwright's own headless adds pointer tells
            self.assertEqual(args, list(browser_runtime.WINDOW_SWITCHES))   # H2: the fallback launch is headed
        background = set(browser_guard.OPERATIONS) - set(browser_runtime.SHOWN_OPERATIONS)
        self.assertEqual(set(browser_runtime.HEADLESS_OPERATIONS), background)
        shipped = json.loads((Path(browser_runtime.ROOT) / "config.json").read_text())
        self.assertEqual(sorted(browser_runtime.headless_operations(shipped)), sorted(background))

    def test_the_support_email_window_is_headed_on_screen_and_left_open(self):
        # Owner, 2026-10-24: the draft is the user's to send, so its window is shown, never hidden.
        guard, mcp, args = self.compose("support_email")
        self.assertNotIn("--headless-user-agent", guard)
        self.assertNotIn("--offscreen", guard)
        self.assertIn("--background-chrome", guard)
        self.assertIsNone(args)
        shown = browser_runtime.background_command("/Applications/Google Chrome.app", "/p", onscreen=True)
        self.assertIn("--window-position=%d,%d" % browser_runtime.ONSCREEN_POSITION, shown)
        self.assertNotIn(browser_runtime.WINDOW_SWITCHES[0], shown)
        with mock.patch.object(browser_runtime, "end_session") as end:
            browser_guard.Proxy.stop_background_chrome(
                mock.Mock(operation="support_email", background_chrome="/p"))
            end.assert_not_called()
            browser_guard.Proxy.stop_background_chrome(mock.Mock(operation="inspect", background_chrome="/p"))
            end.assert_called_once()

    def test_one_operation_can_go_back_to_the_headed_minimized_mode_by_config(self):
        guard, _, args = self.compose("linkedin", headless_operations=["inspect", "submit"])
        self.assertIn("--offscreen", guard)
        self.assertNotIn("--headless-user-agent", guard)
        self.assertEqual(args, list(browser_runtime.WINDOW_SWITCHES))
        guard, _, _ = self.compose("inspect", headless_operations=["inspect", "submit"])
        self.assertIn("--headless-user-agent", guard)
        guard, _, _ = self.compose("inspect", headless_operations="nonsense")   # not a list: the default
        self.assertIn("--headless-user-agent", guard)

    def test_an_unreadable_chrome_version_runs_headed_never_as_headlesschrome(self):
        self.binary = fake_chrome(self, version=None)
        guard, _, _ = self.compose("inspect")
        self.assertIn("--offscreen", guard)
        self.assertNotIn("--headless-user-agent", guard)

    def test_windows_and_tests_isolated_sessions_are_unchanged(self):
        with mock.patch.object(browser_runtime.sys, "platform", "win32"):
            guard, mcp, args = self.compose("inspect")
        self.assertNotIn("--headless-user-agent", guard)
        self.assertNotIn("--offscreen", guard)
        self.assertIn("--user-data-dir", mcp)
        self.assertIsNone(args)
        self.assertIsNone(browser_runtime.headless_user_agent({"browser_driver": {"isolated": True}}, "inspect"))

    def test_the_headless_command_line(self):
        command = browser_runtime.headless_command(self.binary, Path("/p"), UA_141)
        self.assertEqual(command[0], self.binary)              # Chrome itself, no LaunchServices
        self.assertIn("--user-data-dir=/p", command)           # end_session / holds_profile find it so
        self.assertIn("--remote-debugging-port=0", command)
        for switch in ("--headless=new", "--window-size=1280,900", "--user-agent=" + UA_141,
                       "--disable-blink-features=AutomationControlled", "--disable-renderer-backgrounding"):
            self.assertIn(switch, command)
        for absent in ("--use-mock-keychain", "--enable-automation", "--headless", "--hide-scrollbars"):
            self.assertNotIn(absent, command)
        self.assertEqual(command[-1], "about:blank")
        self.assertIn("--remote-debugging-port=53111",
                      browser_runtime.background_command(Path("/x.app"), Path("/p"), 53111))

    def test_launch_headless_starts_chrome_directly_and_reads_its_port(self):
        profile = self.directory / "profile"
        seen = []

        def popen(argv, **kwargs):
            seen.append((argv, kwargs))
            (profile / browser_runtime.DEVTOOLS_PORT_FILE).write_text("53111\n/devtools/browser/x\n")
        self.assertEqual(browser_runtime.launch_headless(self.binary, profile, UA_141, popen=popen),
                         "http://127.0.0.1:53111")
        self.assertIn("--headless=new", seen[0][0])
        self.assertTrue(seen[0][1]["start_new_session"])

    def test_window_words_follow_what_will_run(self):
        self.binary = fake_chrome(self, version=None)       # unreadable version: every session runs minimized
        self.assertEqual(browser_runtime.window_words({}), browser_runtime.OFFSCREEN_WORDS)
        self.assertEqual(browser_runtime.window_words({}, binary=fake_chrome(self)), browser_runtime.HEADLESS_WORDS)

    def test_window_words(self):
        self.assertEqual(browser_runtime.window_words({}), browser_runtime.HEADLESS_WORDS)
        self.assertEqual(browser_runtime.window_words({"browser_driver": {"headless_operations": []}}),
                         browser_runtime.OFFSCREEN_WORDS)
        self.assertEqual(browser_runtime.window_words({"browser_driver": {"headless_operations": ["inspect"]}}),
                         browser_runtime.MIXED_WORDS)


def answer(value):
    return {"result": {"content": [{"type": "text", "text": "### Result\n" + json.dumps(value)}]}}


class Browser:
    """Fake @playwright/mcp behind browser_run_code_unsafe: the headless page's state, the restore, and
    window placements."""

    def __init__(self):
        self.codes = []

    def __call__(self, method, params):
        if method == "initialize":           # the restarted browser server (H1)
            self.codes.append("initialize")
            return {"result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}}}
        code = params["arguments"]["code"]
        self.codes.append(code)
        if "setUserAgentOverride" in code:
            return answer([{"url": "about:blank"}])
        if code == browser_guard.STATE_CODE:
            return answer([{"url": "https://boards.example.com/apply/7", "cookies": [
                {"name": "sid", "value": "s", "domain": "boards.example.com", "path": "/", "expires": -1}]}])
        if "addCookies" in code:
            return answer([{"cookies": 1, "url": "https://boards.example.com/apply/7"}])
        if "windowState: 'minimized'" in code:
            return answer([{"windowId": 1, "state": "minimized", "was": "normal"}])
        return answer([{"windowId": 1, "left": 80, "top": 60}])

    def kinds(self):
        return [code if code == "initialize" else "hints" if "setUserAgentOverride" in code
                else "state" if code == browser_guard.STATE_CODE else "restore" if "addCookies" in code
                else "hide" if "windowState: 'minimized'" in code else "front" if "bringToFront" in code else "other"
                for code in self.codes]


class Wait:
    """A HumanCheckWait stand-in: begins (on_begun, as the real one does once the owner is notified) and
    returns the re-read instruction."""

    def __init__(self, begun=True):
        self.on_begun, self.begun = None, begun

    def call(self):
        if self.begun and self.on_begun is not None:
            self.on_begun()
        return "Waited for the owner. Re-read the page once."


class HeadlessGuard(unittest.TestCase):
    def proxy(self, browser, wait=None, relaunch=None, launched="http://127.0.0.1:53111"):
        directory = scratch(self)
        self.events = []
        with mock.patch.object(browser_guard.subprocess, "Popen") as popen:
            proxy = browser_guard.Proxy(
                "submit", ["node", "cli.js"], audit=directory / "guard.jsonl", stdin=io.StringIO(""),
                stdout=io.StringIO(), human_check_wait=wait, focus_keeper=False,
                background_chrome=str(directory / "profile"), headless_user_agent=UA_141,
                launch_headless=lambda path, ua: self.events.append(("headless", path, ua)) or launched,
                relaunch_headed=relaunch or (lambda path, port: self.events.append(("headed", path, port))),
                stop_chrome=lambda config: self.events.append(("stop", config["browser_driver"]["profile"])))
        self.child_argv = popen.call_args[0][0]
        proxy.spawn_child = mock.Mock                           # a respawned browser server is a fake too
        proxy.child_call = browser
        proxy.to_child = lambda message: None
        self.out = proxy.stdout
        proxy.audit_path = directory / "guard.jsonl"
        return proxy

    def audit(self, proxy):
        return [json.loads(line) for line in proxy.audit_path.read_text().splitlines()]

    def run_wait(self, proxy):
        threads = []
        with mock.patch.object(browser_guard.threading, "Thread",
                               side_effect=lambda target, daemon, **kw: threads.append(target) or mock.Mock()):
            proxy.call_human_check({"jsonrpc": "2.0", "id": 9})
            threads[0]()
        return json.loads(self.out.getvalue().splitlines()[-1])["result"]["content"][0]["text"]

    def test_a_headless_session_is_attached_and_never_placed_or_focus_kept(self):
        browser = Browser()
        proxy = self.proxy(browser)
        self.assertTrue(proxy.headless)
        self.assertFalse(proxy.offscreen)
        self.assertEqual(self.child_argv[-2:], ["--cdp-endpoint", "http://127.0.0.1:53111"])
        self.assertEqual(self.events[0][0], "headless")
        proxy.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        proxy.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": "browser_navigate", "arguments": {"url": "https://example.com"}}})
        self.assertEqual(browser.codes, [])                    # nothing minimized: there is no window
        proxy.focus_keeper = None
        self.assertIsNone(proxy.start_focus_keeper())          # no focus keeper for a windowless Chrome
        self.assertTrue(self.audit(proxy)[0]["headless"])

    def test_the_captcha_wait_reopens_the_profile_headed_on_the_same_port_and_page(self):
        browser = Browser()
        proxy = self.proxy(browser, wait=Wait())
        profile = proxy.background_chrome
        text = self.run_wait(proxy)
        self.assertEqual(self.events[1:], [("stop", profile), ("headed", profile, 53111)])
        self.assertEqual(browser.kinds(), ["state", "initialize", "restore", "front", "hide"])   # H1: a new server
        restore = browser.codes[2]
        self.assertIn('"name": "sid"', restore)                 # session cookies carried over
        self.assertIn('"https://boards.example.com/apply/7"', restore)
        self.assertLess(restore.index("addCookies"), restore.index("page.goto"))
        self.assertTrue(text.endswith(browser_guard.REOPENED_TEXT))
        self.assertFalse(proxy.headless)
        self.assertTrue(proxy.offscreen)                        # the rest of the session: headed, minimized
        decisions = [entry["decision"] for entry in self.audit(proxy)]
        self.assertIn("HEADED_FOR_HUMAN_CHECK", decisions)
        self.assertLess(decisions.index("BROWSER_SERVER_RESTARTED"), decisions.index("HEADED_FOR_HUMAN_CHECK"))
        self.assertLess(decisions.index("HEADED_FOR_HUMAN_CHECK"), decisions.index("WINDOW_ONSCREEN"))
        self.assertNotIn('"value"', proxy.audit_path.read_text())   # no cookie is written to the audit

    def test_the_users_app_is_recorded_before_the_reopen_and_restored_when_the_wait_ends(self):
        # Round 2, finding 1: a headless session has no keeper until the reopen; the app in front is
        # recorded before Chrome is reopened and brought forward, and gets the front back at the end.
        CHROME, CODE = 900, 300
        order = []

        class Desktop:
            now = CODE

            def front(self_inner):
                order.append(("front", self_inner.now))
                return self_inner.now

            def activate(self_inner, pid):
                order.append(("activate", pid))
                self_inner.now = pid
                return True
        desktop = Desktop()

        class FrontBrowser(Browser):
            def __call__(self_inner, method, params):
                answer_ = Browser.__call__(self_inner, method, params)
                if method != "initialize" and "bringToFront" in params["arguments"]["code"]:
                    desktop.now = CHROME      # the tick window comes forward
                return answer_
        browser = FrontBrowser()
        proxy = self.proxy(browser, wait=Wait(),
                           relaunch=lambda path, port: order.append(("relaunch", port)))
        proxy.focus_keeper = browser_guard.FocusKeeper(lambda: CHROME, desktop.front, desktop.activate,
                                                       suspended=lambda: proxy.showing,
                                                       chrome_shown=lambda pid: False, alive=lambda pid: True)
        proxy.start_focus_keeper = lambda headed=False: order.append(("keeper",))
        self.run_wait(proxy)
        self.assertEqual(order[0], ("front", CODE))                 # recorded first
        self.assertLess(order.index(("front", CODE)), order.index(("relaunch", 53111)))
        self.assertLess(order.index(("keeper",)), order.index(("relaunch", 53111)))   # polling before Chrome starts
        self.assertEqual(order[-1], ("activate", CODE))             # and restored when the wait ends
        self.assertEqual(desktop.now, CODE)

    def test_a_wait_that_never_begins_reopens_nothing(self):
        browser = Browser()
        proxy = self.proxy(browser, wait=Wait(begun=False))
        text = self.run_wait(proxy)
        self.assertEqual(browser.codes, [])
        self.assertEqual(len(self.events), 1)
        self.assertTrue(proxy.headless)
        self.assertNotIn(browser_guard.REOPENED_TEXT, text)

    def test_a_failed_reopen_is_recorded_and_the_wait_goes_on(self):
        def broken(path, port):
            raise OSError("Chrome wrote no DevTools port within 60 s")
        proxy = self.proxy(Browser(), wait=Wait(), relaunch=broken)
        text = self.run_wait(proxy)
        self.assertEqual(text, "Waited for the owner. Re-read the page once.")
        self.assertIn("HEADED_REOPEN_FAILED", [entry["decision"] for entry in self.audit(proxy)])

    def test_a_failed_headless_launch_runs_the_session_headed_and_minimized(self):
        # H2: `open -g` headed first, then Playwright's own launch; both by the --offscreen rules.
        directory = scratch(self)
        tried = []

        def fail(path, ua):
            raise OSError("Google Chrome not found")

        def headed(path):
            tried.append(path)
            return "http://127.0.0.1:53222"
        with mock.patch.object(browser_guard.subprocess, "Popen") as popen:
            proxy = browser_guard.Proxy("inspect", ["node"], audit=directory / "guard.jsonl", stdin=io.StringIO(""),
                                        stdout=io.StringIO(), focus_keeper=False, background_chrome=str(directory),
                                        headless_user_agent=UA_141, launch_headless=fail, launch_background=headed)
        self.assertFalse(proxy.headless)
        self.assertTrue(proxy.offscreen)
        self.assertEqual(tried, [str(directory)])
        self.assertEqual(popen.call_args[0][0][-2:], ["--cdp-endpoint", "http://127.0.0.1:53222"])

        def headed_fails(path):
            raise OSError("open -g failed")
        with mock.patch.object(browser_guard.subprocess, "Popen") as popen:
            proxy = browser_guard.Proxy("inspect", ["node"], audit=directory / "guard.jsonl", stdin=io.StringIO(""),
                                        stdout=io.StringIO(), focus_keeper=False, background_chrome=str(directory),
                                        headless_user_agent=UA_141, launch_headless=fail, launch_background=headed_fails)
        self.assertTrue(proxy.offscreen)
        self.assertEqual(popen.call_args[0][0][-2:], ["--user-data-dir", str(directory)])
        decisions = [json.loads(line)["decision"] for line in (directory / "guard.jsonl").read_text().splitlines()]
        self.assertIn("HEADLESS_LAUNCH_FAILED", decisions)
        self.assertIn("BACKGROUND_LAUNCH_FAILED", decisions)


class ClientHints(unittest.TestCase):
    """H3: the installed Chrome's client hints, on every page and every page opened later."""

    def test_metadata_comes_from_the_installed_chrome_and_this_mac(self):
        binary = fake_chrome(self)
        hints = browser_runtime.client_hints(binary, mac_version="15.6.0", machine="arm64")
        self.assertEqual(hints["fullVersion"], "141.0.7390.123")
        self.assertEqual({b["brand"]: b["version"] for b in hints["brands"]}["Google Chrome"], "141")
        self.assertEqual({b["brand"]: b["version"] for b in hints["fullVersionList"]}["Chromium"], "141.0.7390.123")
        self.assertEqual(len(hints["brands"]), 3)
        self.assertFalse(any("Headless" in b["brand"] for b in hints["brands"] + hints["fullVersionList"]))
        self.assertEqual((hints["platform"], hints["platformVersion"], hints["architecture"]), ("macOS", "15.6.0", "arm"))
        self.assertEqual(browser_runtime.client_hints(fake_chrome(self, "142.0.1.2"), "15", "arm64")["fullVersion"],
                         "142.0.1.2")                                   # never a fixed version
        self.assertIsNone(browser_runtime.client_hints(fake_chrome(self, None)))
        grease = [b for b in browser_runtime.brand_list("141", "141") if b["brand"].startswith("Not")][0]
        self.assertRegex(grease["brand"], r"^Not.A.Brand$")

    def test_the_guard_sets_the_override_on_open_pages_and_on_each_new_page(self):
        code = browser_guard.client_hints_code(UA_141, {"brands": []})
        self.assertIn("Emulation.setUserAgentOverride", code)
        self.assertIn("ctx.on('page'", code)                  # every target opened later gets it too
        self.assertIn("for (const p of ctx.pages())", code)
        self.assertNotIn("detach", code)                       # the session stays attached: the override lasts
        self.assertIn(json.dumps(UA_141), code)

    def test_sent_right_after_initialized_in_a_headless_session_only(self):
        browser = Browser()
        directory = scratch(self)
        with mock.patch.object(browser_guard.subprocess, "Popen"):
            proxy = browser_guard.Proxy("inspect", ["node"], audit=directory / "guard.jsonl", stdin=io.StringIO(""),
                                        stdout=io.StringIO(), focus_keeper=False, background_chrome=str(directory),
                                        headless_user_agent=UA_141, launch_headless=lambda p, ua: "http://127.0.0.1:1",
                                        client_hints={"brands": []})
        proxy.child_call, proxy.to_child, proxy.spawn_child = browser, (lambda message: None), mock.Mock
        proxy.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(browser.kinds(), ["hints"])
        proxy.headless = False                                  # a headed session reports Chrome's own hints
        proxy.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(browser.kinds(), ["hints"])
        with mock.patch.object(browser_runtime, "BACKGROUND_ALLOWED", True), \
                mock.patch.object(browser_runtime.sys, "platform", "darwin"), \
                mock.patch.object(setup_steps, "chrome_path", lambda platform=None, exists=None: fake_chrome(self)):
            argv = browser_runtime.guard_command({"browser_driver": {"profile": str(directory)}}, directory, "inspect")
        hints = json.loads(argv[argv.index("--headless-client-hints") + 1])
        self.assertEqual(hints["fullVersion"], "141.0.7390.123")


class ScreenMetrics(unittest.TestCase):
    """Real-site check, 2026-10-24: headless reported an 800x600 screen at ratio 1 against a 1280x900 window.
    The session reports the user's own main display (points, backing scale), never smaller than the window."""

    class Quartz:
        def __init__(self, width=1728, height=1117, pixels=3456, broken=False):
            self.width, self.height, self.pixels, self.broken = width, height, pixels, broken

        def CGMainDisplayID(self):
            if self.broken:
                raise RuntimeError("no window server")
            return 1

        def CGDisplayPixelsWide(self, display):
            return self.width

        def CGDisplayPixelsHigh(self, display):
            return self.height

        def CGDisplayCopyDisplayMode(self, display):
            return "mode"

        def CGDisplayModeGetPixelWidth(self, mode):
            return self.pixels

        def CGDisplayModeGetWidth(self, mode):
            return self.width

    def test_the_display_is_read_in_points_with_its_backing_scale_or_falls_back(self):
        self.assertEqual(browser_runtime.display(self.Quartz()), (1728, 1117, 2.0))
        self.assertEqual(browser_runtime.display(self.Quartz(1440, 900, 1440)), (1440, 900, 1.0))
        self.assertEqual(browser_runtime.display(self.Quartz(broken=True)), (1512, 982, 2))
        self.assertEqual(browser_runtime.display(self.Quartz(0, 0, 0)), (1512, 982, 2))

    def test_screen_is_never_smaller_than_the_window_and_the_ratio_matches(self):
        window_width, window_height = browser_runtime.WINDOW_SIZE
        for value in ((1728, 1117, 2.0), (1280, 800, 1.0), (1024, 768, 2.0)):
            screen = browser_runtime.screen_metrics(value)
            self.assertGreaterEqual(screen["width"], window_width)
            self.assertGreaterEqual(screen["height"], window_height)
            self.assertEqual(screen["scale"], value[2])
            command = browser_runtime.headless_command("/chrome", Path("/p"), UA_141, screen["scale"])
            self.assertIn("--force-device-scale-factor=%s" % browser_runtime._number(value[2]), command)
            self.assertIn("--window-size=1280,900", command)            # the viewport stays 1280 wide
            code = browser_guard.client_hints_code(UA_141, {"brands": []}, screen)
            metrics = json.loads(code.split("const metrics = ")[1].split(";\n")[0])
            self.assertEqual((metrics["screenWidth"], metrics["screenHeight"]), (screen["width"], screen["height"]))
            self.assertEqual((metrics["width"], metrics["height"], metrics["deviceScaleFactor"]), (0, 0, 0))
            self.assertIn("Emulation.setDeviceMetricsOverride", code)
            self.assertIn("ctx.on('page'", code)                         # new pages too
        self.assertEqual(browser_guard.parse_screen("1728x1117@2"), {"width": 1728, "height": 1117, "scale": 2.0})
        self.assertIsNone(browser_guard.parse_screen("nonsense"))

    def test_the_guard_gets_the_screen_and_uses_it_for_launch_and_every_page(self):
        directory = scratch(self)
        with mock.patch.object(browser_runtime, "BACKGROUND_ALLOWED", True), \
                mock.patch.object(browser_runtime.sys, "platform", "darwin"), \
                mock.patch.object(browser_runtime, "display", lambda quartz=None: (1728, 1117, 2.0)), \
                mock.patch.object(setup_steps, "chrome_path", lambda platform=None, exists=None: fake_chrome(self)):
            argv = browser_runtime.guard_command({"browser_driver": {"profile": str(directory)}}, directory, "inspect")
        screen = browser_guard.parse_screen(argv[argv.index("--headless-screen") + 1])
        self.assertEqual(screen, {"width": 1728, "height": 1117, "scale": 2.0})
        browser = Browser()
        launched = []
        with mock.patch.object(browser_guard.subprocess, "Popen"), \
                mock.patch.object(setup_steps, "chrome_path", lambda platform=None, exists=None: "/chrome"), \
                mock.patch.object(browser_runtime, "launch_headless",
                                  lambda binary, path, ua, scale=None: launched.append(scale) or "http://127.0.0.1:1"):
            proxy = browser_guard.Proxy("inspect", ["node"], audit=directory / "guard.jsonl", stdin=io.StringIO(""),
                                        stdout=io.StringIO(), focus_keeper=False, background_chrome=str(directory),
                                        headless_user_agent=UA_141, screen=screen)
        self.assertEqual(launched, [2.0])                                  # DPR = the display's backing scale
        proxy.child_call, proxy.to_child = browser, (lambda message: None)
        proxy.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(browser.kinds(), ["hints"])
        self.assertIn('"screenWidth": 1728', browser.codes[0])


class RestartedBrowserServer(unittest.TestCase):
    """H1 against the fake @playwright/mcp over real pipes: the guard replays the client's initialize to a
    fresh server and goes on relaying through it."""

    def test_respawn_replays_initialize_and_the_new_server_answers(self):
        import browser_fixtures
        directory = scratch(self)
        snapshot, calls = directory / "snapshot.yaml", directory / "calls.jsonl"
        snapshot.write_text("- generic [ref=e1]\n")
        out = io.StringIO()
        proxy = browser_guard.Proxy("inspect", browser_fixtures.fake_runtime(snapshot, calls),
                                    audit=directory / "guard.jsonl", stdin=io.StringIO(""), stdout=out,
                                    focus_keeper=False)
        import threading
        threading.Thread(target=proxy.pump_child, daemon=True).start()
        try:
            proxy.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "codex", "version": "1"}}})
            first = proxy.child
            proxy.respawn_child()
            self.assertIsNot(proxy.child, first)
            first.wait(10)                                      # the old server is closed
            answer = proxy.child_call("tools/call", {"name": "browser_snapshot", "arguments": {}})
            self.assertIn("ref=e1", json.dumps(answer))
            self.assertEqual(proxy.initialize_params["clientInfo"]["name"], "codex")
            self.assertTrue(proxy.requests.empty())             # the old server's end did not end the session
            relayed = [json.loads(line) for line in out.getvalue().splitlines()]
            self.assertEqual([m.get("id") for m in relayed], [1])   # only the client's own initialize answer
        finally:
            proxy.stop_child()


class HumanCheckWaitBegins(unittest.TestCase):
    def test_on_begun_runs_once_and_only_when_the_wait_began(self):
        for extended, expected in ((True, 1), (False, 0)):
            path = scratch(self) / "human-check.json"
            path.write_text("{}")
            wait = application_browser.HumanCheckWait(path, sleep=lambda s: None,
                                                      clock=lambda: dt.datetime(2026, 10, 24, tzinfo=dt.timezone.utc))
            calls = []
            wait.on_begun = lambda: calls.append(1)
            with mock.patch.object(application_browser.HumanCheckWait, "begin", lambda self, now: extended):
                wait.call()
                wait.call()
            self.assertEqual(len(calls), expected)


if __name__ == "__main__":
    unittest.main()
