"""Fixes from the first real Windows run of E15.T5b (2026-11-13, Windows 11 ARM64, Chrome 157). Fakes only.

1. A Chrome started on a fixed DevTools port writes no DevToolsActivePort file: the port itself is waited for.
2. The human check's window is raised by Windows itself; a refused foreground still puts it above the others.
3. No "Restore pages?" bubble: Chrome is asked to close on its own socket, and the window hides the bubble.
5. A shown window fits the work area (the screen less the taskbar).
6. A new tab has its details before its first document (TabDetails).
7. The client hints carry the form factor a window's Chrome sends.
Also: Chrome is found without any Program Files variable in the environment."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import io
import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import browser_guard
import browser_runtime
import portable
import setup_steps
from test_browser_driver_review import scratch
from test_browser_headless import Browser, UA_141
from test_windows_hidden import DisplayApi, NoTabs, WindowsCase


class Socket:
    def __init__(self):
        self.sent, self.closed = [], False

    def send(self, text):
        self.sent.append(json.loads(text))

    def recv(self, timeout=None):
        raise TimeoutError

    def close(self):
        self.closed = True


def version_page(url, timeout=None):
    return io.BytesIO(json.dumps({"Browser": "Chrome/157.0.8103.51",
                                  "webSocketDebuggerUrl": "ws://127.0.0.1:53111/devtools/browser/abc"}).encode())


class CleanClose(WindowsCase):
    def test_a_shown_window_hides_the_restore_bubble_and_changes_nothing_a_site_sees(self):
        command = browser_runtime.windows_command(self.binary, "/p")
        self.assertIn("--hide-crash-restore-bubble", command)
        for absent in ("--user-agent", "--headless", "--disable-blink-features", "--no-sandbox"):
            self.assertFalse([item for item in command if item.startswith(absent)], absent)
        with mock.patch.object(browser_runtime.sys, "platform", "darwin"):
            self.assertNotIn("--hide-crash-restore-bubble", browser_runtime.background_command(Path("/x.app"), Path("/p")))
            self.assertNotIn("--hide-crash-restore-bubble", browser_runtime.headless_command("/chrome", Path("/p"), "UA"))

    def test_chrome_is_asked_to_close_on_its_own_browser_socket(self):
        socket, dialled = Socket(), []
        self.assertTrue(browser_runtime.close_browser(
            "http://127.0.0.1:53111", connect=lambda url: dialled.append(url) or socket, opener=version_page))
        self.assertEqual(dialled, ["ws://127.0.0.1:53111/devtools/browser/abc"])
        self.assertEqual(socket.sent, [{"id": 1, "method": "Browser.close"}])
        self.assertTrue(socket.closed)

        def refused(url, timeout=None):
            raise OSError("refused")
        self.assertFalse(browser_runtime.close_browser("http://127.0.0.1:53111", connect=lambda url: socket, opener=refused))
        self.assertFalse(browser_runtime.close_browser("http://example.com:80", connect=lambda url: socket, opener=version_page))

    def test_the_hidden_chrome_is_closed_cleanly_before_the_window_opens_even_with_no_browser_server(self):
        events = []
        with mock.patch.object(browser_guard.subprocess, "Popen"):
            proxy = browser_guard.Proxy(
                "submit", ["node", "cli.js"], audit=self.directory / "guard.jsonl", stdin=io.StringIO(""),
                stdout=io.StringIO(), focus_keeper=False, background_chrome=str(self.directory / "profile"),
                headless_user_agent=UA_141, launch_headless=lambda path, ua: "http://127.0.0.1:53111")
        self.addCleanup(proxy.audit.close)
        proxy.child = mock.Mock(poll=lambda: 1)                        # the browser server is gone
        proxy.child_call = lambda method, params: events.append("server") or None
        with mock.patch.object(browser_runtime, "close_browser", lambda endpoint: events.append(("socket", endpoint)) or True), \
                mock.patch.object(portable, "wait_profile_free", lambda path, seconds: events.append("waited")):
            proxy.close_own_chrome()
        self.assertEqual(events, [("socket", "http://127.0.0.1:53111"), "waited"])


class WorkArea(unittest.TestCase):
    binary = "chrome.exe"

    def setUp(self):
        patcher = mock.patch.object(browser_runtime.sys, "platform", "win32")
        patcher.start()
        self.addCleanup(patcher.stop)

    class Api(DisplayApi):
        def __init__(self, real, told, dpi, area):
            super().__init__(real, told, dpi)
            self.area = area

        def SystemParametersInfoW(self, action, parameter, out, flags):
            if action != portable.SPI_GETWORKAREA or self.area is None:
                return 0
            out._obj.left, out._obj.top, out._obj.right, out._obj.bottom = self.area
            return 1

    def test_the_work_area_is_the_screen_less_the_taskbar_in_the_pages_own_pixels(self):
        api = self.Api((1024, 768), 1024, 96, (0, 0, 1024, 720))       # the VM: a 48 px taskbar at the bottom
        self.assertEqual(portable.work_area(api), (0, 0, 1024, 720))
        self.assertEqual(portable.work_area(self.Api((1920, 1080), 1920, 144, (0, 0, 1920, 1008))), (0, 0, 1280, 672))
        self.assertEqual(portable.work_area(self.Api((1920, 1080), 1280, 96, (0, 0, 1280, 672))), (0, 0, 1280, 672))
        self.assertEqual(portable.work_area(self.Api((1366, 768), 1366, 96, (0, 40, 1366, 768))), (0, 40, 1366, 728))
        self.assertIsNone(portable.work_area(self.Api((1024, 768), 1024, 96, None)))
        self.assertIsNone(portable.work_area())                        # not Windows

    def test_a_shown_window_lies_inside_the_work_area_not_under_the_taskbar(self):
        screen = {"width": 1024, "height": 768, "scale": 1.0}
        self.assertEqual(browser_runtime.visible_window(screen, area=(0, 0, 1024, 720)), (0, 0, 1024, 720))
        self.assertEqual(browser_runtime.visible_window(screen, area=(0, 40, 1366, 728)), (80, 40, 1280, 728))
        self.assertEqual(browser_runtime.visible_window(screen, area=(0, 0, 2560, 1392)), (80, 60, 1280, 900))
        with mock.patch.object(portable, "work_area", lambda api=None: (0, 0, 1024, 720)):
            left, top, width, height = browser_runtime.visible_window(screen)
            self.assertLessEqual(top + height, 720)                    # the VM's window sat 41 px under the taskbar
            command = browser_runtime.windows_command(self.binary, "/p", screen=screen)
            self.assertIn("--window-size=1024,720", command)
            self.assertIn("--window-position=0,0", command)
        with mock.patch.object(browser_runtime.sys, "platform", "darwin"), \
                mock.patch.object(portable, "work_area", lambda api=None: (0, 0, 1024, 720)):
            self.assertEqual(browser_runtime.visible_window(screen), (80, 60, 1280, 900))


class NewTabs(unittest.TestCase):
    """Item 6: Chrome holds a new tab until its details are set; only then does its first document start."""

    def details(self, screen={"width": 1024, "height": 768, "scale": 1.0}):
        self.socket, self.failed = Socket(), []
        self.timers = getattr(self, "timers", [])

        class Timer:
            def __init__(timer, seconds, function):
                timer.seconds, timer.fire, timer.cancelled, timer.daemon = seconds, function, False, False
                self.timers.append(timer)

            def start(timer):
                pass

            def cancel(timer):
                timer.cancelled = True
        tabs = browser_guard.TabDetails("http://127.0.0.1:53111", "UA", {"platform": "Windows"}, screen,
                                        lambda: self.failed.append(True), connect=lambda url: self.socket,
                                        opener=version_page, timer=Timer)
        with mock.patch.object(browser_guard.threading, "Thread"):
            tabs.start()
        return tabs

    def attached(self, session="S1", kind="page", held=True):
        return {"method": "Target.attachedToTarget", "params": {
            "sessionId": session, "waitingForDebugger": held, "targetInfo": {"type": kind, "url": ""}}}

    def test_chrome_is_asked_to_hold_every_new_tab(self):
        self.details()
        self.assertEqual(self.socket.sent, [{"id": 1, "method": "Target.setAutoAttach", "params": {
            "autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}}])

    def test_a_new_tab_is_released_right_after_its_details_are_sent_with_no_answer_awaited(self):
        # VM pass 2: a target=_blank link (a tab with no opener) answers the details calls only once it is
        # let run, so waiting for those answers held it for ever. The calls are sent in order on the tab's
        # own session (Chrome takes them in that order), then the release, as the browser server itself does.
        tabs = self.details()
        tabs.handle({"method": "Target.attachedToTarget", "params": {
            "sessionId": "S1", "waitingForDebugger": True,
            "targetInfo": {"type": "page", "url": "", "openerId": None}}})      # no opener, nothing loaded yet
        sent = self.socket.sent[1:]
        self.assertEqual([(m["method"], m["sessionId"]) for m in sent],
                         [("Emulation.setUserAgentOverride", "S1"), ("Emulation.setDeviceMetricsOverride", "S1"),
                          ("Runtime.runIfWaitingForDebugger", "S1")])              # no answer has come: not held
        self.assertEqual(sent[0]["params"], {"userAgent": "UA", "userAgentMetadata": {"platform": "Windows"}})
        self.assertEqual((sent[1]["params"]["screenWidth"], sent[1]["params"]["screenHeight"]), (1024, 768))
        self.assertEqual((sent[1]["params"]["width"], sent[1]["params"]["deviceScaleFactor"]), (0, 0))
        for message in sent:                                                       # the answers, afterwards
            tabs.handle({"id": message["id"], "result": {}, "sessionId": "S1"})
        self.assertEqual(len(self.socket.sent), 4)                                 # released once only
        self.assertEqual(self.failed, [])
        self.assertEqual(self.timers[0].cancelled, True)                           # answered in time

    def test_a_tab_with_an_opener_is_treated_the_same(self):
        tabs = self.details()
        tabs.handle(self.attached("S2"))
        self.assertEqual([m["method"] for m in self.socket.sent[1:]],
                         ["Emulation.setUserAgentOverride", "Emulation.setDeviceMetricsOverride",
                          "Runtime.runIfWaitingForDebugger"])

    def test_details_that_were_refused_or_never_answered_stop_the_session(self):
        # The owner's rule: never present as something a visible Chrome would not. A tab whose details did
        # not take is not left to go on: the session stops (as a failed first details call does).
        tabs = self.details()
        tabs.handle(self.attached())
        tabs.handle({"id": self.socket.sent[1]["id"], "error": {"message": "Target closed"}, "sessionId": "S1"})
        self.assertEqual(self.failed, [True])
        tabs = self.details()
        tabs.handle(self.attached())
        self.assertEqual(self.timers[-1].seconds, browser_guard.TabDetails.ANSWER_SECONDS)
        self.timers[-1].fire()                                                     # no answer in time
        self.assertEqual(self.failed, [True])
        tabs = self.details()
        tabs.handle(self.attached())
        tabs.handle({"method": "Target.detachedFromTarget", "params": {"sessionId": "S1"}})   # the tab closed first
        self.timers[-1].fire()
        self.assertEqual(self.failed, [])

    def test_other_targets_are_not_held_back_and_an_open_page_is_set_without_a_resume(self):
        tabs = self.details()
        tabs.handle(self.attached("W1", "service_worker"))
        self.assertEqual((self.socket.sent[-1]["method"], self.socket.sent[-1]["sessionId"]),
                         ("Runtime.runIfWaitingForDebugger", "W1"))
        tabs.handle(self.attached("P0", "page", held=False))           # the page open at the start
        self.assertEqual(self.socket.sent[-1]["method"], "Emulation.setDeviceMetricsOverride")   # nothing to resume

    def test_the_guard_arms_it_before_the_browser_server_starts_and_stops_on_a_failure(self):
        directory = scratch(self)
        made, order = [], []

        class Tabs(NoTabs):
            def __init__(self, endpoint, user_agent, metadata, screen, on_failed):
                made.append((endpoint, user_agent, metadata, screen, on_failed))

            def start(self):
                order.append("tabs armed")
                return self

            def close(self):
                order.append("tabs closed")
        hints, screen = {"platform": "Windows", "fullVersion": "157.0.8103.51"}, {"width": 1024, "height": 768, "scale": 1.0}
        with mock.patch.object(browser_runtime.sys, "platform", "win32"), \
                mock.patch("setup_steps.chrome_path", lambda platform=None, exists=None: "chrome.exe"), \
                mock.patch.object(browser_runtime, "launch_hidden_windows",
                                  lambda binary, path, user_agent, screen=None: "http://127.0.0.1:53111"), \
                mock.patch.object(browser_runtime, "running_version", lambda endpoint, opener=None: "157.0.8103.51"), \
                mock.patch.object(browser_guard.subprocess, "Popen",
                                  side_effect=lambda *a, **k: order.append("browser server") or mock.Mock()):
            proxy = browser_guard.Proxy(
                "inspect", ["node", "cli.js"], audit=directory / "guard.jsonl", stdin=io.StringIO(""),
                stdout=io.StringIO(), focus_keeper=False, background_chrome=str(directory / "profile"),
                headless_user_agent="UA", client_hints=hints, screen=screen, make_tab_details=Tabs)
            self.addCleanup(proxy.audit.close)
            self.assertEqual(order, ["tabs armed", "browser server"])
            self.assertEqual(made[0][:4], ("http://127.0.0.1:53111", "UA", hints, screen))
            made[0][4]()                                               # a tab's details could not be set
            self.assertEqual(proxy.stop_reason, "TAB_DETAILS_FAILED")
            self.assertTrue(proxy.closing)
            self.assertEqual(browser_runtime.guard_stopped(directory), "TAB_DETAILS_FAILED")

            class Unreachable(NoTabs):
                def start(self):
                    raise OSError("no browser socket")
            ended = []
            with mock.patch.object(browser_runtime, "end_session", lambda config: ended.append(config)):
                proxy = browser_guard.Proxy(
                    "inspect", ["node", "cli.js"], audit=directory / "guard.jsonl", stdin=io.StringIO(""),
                    stdout=io.StringIO(), focus_keeper=False, background_chrome=str(directory / "profile"),
                    headless_user_agent="UA", client_hints=hints, screen=screen, make_tab_details=Unreachable)
            self.addCleanup(proxy.audit.close)
            self.assertFalse(proxy.headless)                           # no hidden launch without it
            self.assertEqual(len(ended), 1)


class FormFactorsAndChromesPlace(WindowsCase):
    def test_the_hints_carry_the_form_factor_a_windows_chrome_sends(self):
        hints = browser_runtime.windows_client_hints(self.binary)
        self.assertEqual(hints["formFactors"], ["Desktop"])
        guard, _, _ = self.compose()
        self.assertEqual(json.loads(guard[guard.index("--headless-client-hints") + 1])["formFactors"], ["Desktop"])
        self.assertIn('"formFactors": ["Desktop"]', browser_guard.client_hints_code("UA", hints, None))

    def test_the_session_is_handed_the_program_folders_too(self):
        environ = {"PROGRAMFILES": "C:\\Program Files", "PROGRAMFILES(X86)": "C:\\Program Files (x86)",
                   "SYSTEMDRIVE": "C:", "SECRET_KEY": "x"}
        passed = browser_runtime.session_environment(environ)
        self.assertEqual(passed, {key: value for key, value in environ.items() if key != "SECRET_KEY"})


class ChromesPlace(unittest.TestCase):
    def test_chrome_is_found_with_no_program_files_variable(self):
        # The session's guard had only the session's own variables: no PROGRAMFILES, and Chrome was "not found".
        bare = {"USERPROFILE": "C:\\Users\\u", "SYSTEMROOT": "C:\\Windows", "LOCALAPPDATA": "C:\\Users\\u\\AppData\\Local"}
        usual = "C:/Program Files/Google/Chrome/Application/chrome.exe"
        with mock.patch.dict(setup_steps.os.environ, bare, clear=True), \
                mock.patch.object(portable, "app_path", lambda name, read=None: None):
            self.assertEqual(setup_steps.chrome_path("win32", exists=lambda path: path == usual), usual)
        registered = "D:\\Apps\\Chrome\\chrome.exe"
        with mock.patch.dict(setup_steps.os.environ, bare, clear=True), \
                mock.patch.object(portable, "app_path", lambda name, read=None: registered):
            self.assertEqual(setup_steps.chrome_path("win32", exists=lambda path: path == registered), registered)
        self.assertEqual(portable.app_path("chrome.exe", lambda name: '"D:\\Apps\\Chrome\\chrome.exe"'.strip('"')), registered)
        self.assertIsNone(portable.app_path("chrome.exe", lambda name: None))
        self.assertIsNone(portable.app_path("chrome.exe"))             # not Windows


class GuardsPackages(unittest.TestCase):
    """VM pass 2: the build venv had no `websockets`, which the guard's own DevTools client needs."""

    def test_both_builds_name_the_guards_socket_modules_and_refuse_a_venv_without_them(self):
        import sys
        tools = str(Path(browser_guard.__file__).resolve().parent / "tools")
        if tools not in sys.path:
            sys.path.insert(0, tools)
        import build_macos
        import build_windows
        for name in ("websockets.sync.client", "websockets.asyncio.client"):
            self.assertIn(name, build_macos.STRING_IMPORTS)                        # --include-module: a missing one fails Nuitka
            self.assertIn("--include-module=" + name, build_windows.nuitka_command("out", "icon.ico"))
            self.assertIn(name, build_macos.REQUIRED_MODULES)
        build_macos.check_build_packages()                                         # this venv has them
        with self.assertRaises(SystemExit) as refused:
            build_macos.check_build_packages(find=lambda name: None if name.startswith("websockets") else object())
        self.assertIn("websockets", str(refused.exception))
        for script in ("build_macos.py", "build_windows.py"):
            source = (Path(tools) / script).read_text()
            self.assertIn("check_build_packages()", source[source.index("def main("):], script)
        declared = (Path(tools).parent / "pyproject.toml").read_text()
        self.assertIn('"websockets==', declared)
        self.assertIn("websockets==", (Path(tools) / "build-requirements-macos.txt").read_text())


if __name__ == "__main__":
    unittest.main()
