"""E15.T5b (owner, 2026-11-12, option A): on Windows Sidedoor starts Chrome itself, without a window, for
every session, as on macOS. What the hidden Chrome tells a page (user agent, client hints, screen) is read
from this machine through the Windows API, faked here; nothing is a fixed or a Mac value. No Chrome starts."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import ctypes
import hashlib
import io
import json
from pathlib import Path
import platform
import plistlib
import unittest
from unittest import mock

import browser_guard
import browser_runtime
import fake_windows
import live_view
import portable
import setup_steps
import support_email
from test_browser_driver_review import scratch
from test_browser_headless import Browser

UA_157 = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/157.0.0.0 Safari/537.36")
VERSION = "157.0.8103.51"
# The Mac guard and Chrome command lines as main (a8c18e3) builds them, hashed (MacUnchanged.build). Re-pinned
# at the merge of main: main itself removed background_command's port (E16.T1), which changed that one line.
MAC_GOLDEN = "bcce9f9ff1d47bc774d3e1a5ec76f2ff05594cd1c2b7d246130a554a68d7e6d0"


class VersionApi:
    """version.dll: a file's version resource (VS_FIXEDFILEINFO), by path."""

    def __init__(self, files, signature=portable.VERSION_SIGNATURE):
        self.files, self.signature, self.kept = files, signature, []

    def GetFileVersionInfoSizeW(self, path, handle):
        return 64 if path in self.files else 0

    def GetFileVersionInfoW(self, path, handle, size, data):
        self.path = path
        return 1

    def VerQueryValueW(self, data, block, value, length):
        a, b, c, d = (int(part) for part in self.files[self.path].split("."))
        info = portable._fixed_file_info()(dwSignature=self.signature, dwFileVersionMS=a << 16 | b,
                                           dwFileVersionLS=c << 16 | d)
        self.kept.append(info)
        value._obj.value = ctypes.addressof(info)
        length._obj.value = ctypes.sizeof(info)
        return 1


class DisplayApi:
    """user32/shcore for the primary display: `real` pixels, the width this process is `told`, its `dpi`."""

    def __init__(self, real=(1920, 1080), told=1920, dpi=96, fail=None):
        self.real, self.told, self.dpi, self.fail = real, told, dpi, fail

    def EnumDisplaySettingsW(self, device, mode, out):
        if self.fail == "mode":
            return 0
        out._obj.dmPelsWidth, out._obj.dmPelsHeight = self.real
        return 1

    def GetSystemMetrics(self, index):
        return self.told if index == portable.SM_CXSCREEN else 0

    def MonitorFromWindow(self, window, flags):
        return 0x10001 if flags == portable.MONITOR_DEFAULTTOPRIMARY else 0

    def GetDpiForMonitor(self, monitor, kind, x, y):
        if self.fail == "dpi":
            return -2147467259   # E_FAIL
        x._obj.value = y._obj.value = self.dpi
        return 0


class MachineApi:
    """kernel32: the machine Windows itself runs on, whatever this process was built for."""

    def __init__(self, native=portable.PE_AMD64, works=True):
        self.native, self.works = native, works

    def GetCurrentProcess(self):
        return -1

    def IsWow64Process2(self, process, own, native):
        own._obj.value, native._obj.value = 0, self.native
        return 1 if self.works and process == -1 else 0


class NoTabs:
    """Stands in for browser_guard.TabDetails (its own tests: tests/test_windows_vm_round.py)."""

    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        return self

    def close(self):
        pass


def program(folder, machine=portable.PE_AMD64, name="chrome.exe"):
    """A file with a PE header for `machine` (None: not a program file); returns its path."""
    path = Path(folder) / name
    head = bytearray(64)
    head[:2], head[60:64] = b"MZ", (128).to_bytes(4, "little")
    body = bytes(head) + bytes(64) + b"PE\0\0" + (machine or 0).to_bytes(2, "little") + bytes(18)
    path.write_bytes(body if machine is not None else b"not a program")
    return str(path)


class Readers(unittest.TestCase):
    def test_chromes_version_is_its_own_files_version(self):
        api = VersionApi({"C:/Chrome/chrome.exe": VERSION})
        self.assertEqual(portable.file_version("C:/Chrome/chrome.exe", api), VERSION)
        self.assertIsNone(portable.file_version("C:/other.exe", api))                        # no version resource
        self.assertIsNone(portable.file_version("C:/Chrome/chrome.exe", VersionApi({"C:/Chrome/chrome.exe": VERSION}, 1)))
        self.assertIsNone(portable.file_version("C:/Chrome/chrome.exe"))                     # not Windows: no API

    def test_what_chrome_was_built_for_is_read_from_its_header(self):
        folder = scratch(self)
        self.assertEqual(portable.pe_machine(program(folder)), portable.PE_AMD64)
        self.assertEqual(portable.pe_machine(program(folder, portable.PE_ARM64)), portable.PE_ARM64)
        self.assertIsNone(portable.pe_machine(program(folder, None)))
        self.assertIsNone(portable.pe_machine(folder / "missing.exe"))

    def test_the_machine_is_the_one_windows_runs_on(self):
        self.assertEqual(portable.native_machine(MachineApi()), portable.PE_AMD64)
        self.assertEqual(portable.native_machine(MachineApi(portable.PE_ARM64)), portable.PE_ARM64)   # also for an x64 program
        self.assertIsNone(portable.native_machine(MachineApi(works=False)))
        self.assertIsNone(portable.native_machine(object()))                                 # Windows without the call
        self.assertIsNone(portable.native_machine())                                         # not Windows

    def test_the_platform_version_is_the_api_contract_never_the_kernels_ten(self):
        self.assertEqual(portable.api_contract_version(lambda: 0x00130000), (19, 0))         # Windows 11 24H2
        self.assertEqual(portable.api_contract_version(lambda: 0x000F0000), (15, 0))         # Windows 11 22H2
        self.assertEqual(portable.api_contract_version(lambda: 0x000A0000), (10, 0))         # Windows 10 2004+

        def missing():
            raise FileNotFoundError("no such key")
        for read in (missing, lambda: 0, lambda: "19", lambda: None):
            self.assertIsNone(portable.api_contract_version(read))
        self.assertIsNone(portable.api_contract_version())                                   # not Windows

    def test_the_display_is_the_real_one_in_every_dpi_awareness_mode(self):
        # A 1366x768 laptop at 100% is 1366x768, never a 1920x1080 or a 1280x900 it does not have.
        self.assertEqual(portable.primary_display(DisplayApi((1366, 768), 1366, 96)), (1366, 768, 1.0))
        # 1920x1080 at 125%: a process that is not DPI aware is told 1536 and 96; an aware one 1920 and 120.
        self.assertEqual(portable.primary_display(DisplayApi((1920, 1080), 1536, 96)), (1536, 864, 1.25))
        self.assertEqual(portable.primary_display(DisplayApi((1920, 1080), 1920, 120)), (1536, 864, 1.25))
        self.assertEqual(portable.primary_display(DisplayApi((3840, 2160), 3840, 192)), (1920, 1080, 2.0))
        self.assertEqual(portable.primary_display(DisplayApi((2560, 1440), 2560, 144)), (1707, 960, 1.5))
        # V9: a custom scale (110%) is its own scale, never rounded to 100% or 125%.
        self.assertEqual(portable.primary_display(DisplayApi((1920, 1080), 1920, 106)), (1746, 982, 1.1))
        self.assertEqual(portable.primary_display(DisplayApi((1920, 1080), 1745, 96)), (1746, 982, 1.1))
        self.assertEqual(portable.primary_display(DisplayApi((1920, 1080), 1920, 168)), (1098, 618, 1.75))
        for broken in (DisplayApi(fail="mode"), DisplayApi(fail="dpi"), DisplayApi((0, 0)), DisplayApi(told=0)):
            self.assertIsNone(portable.primary_display(broken))
        self.assertIsNone(portable.primary_display())                                        # not Windows


class WindowsCase(unittest.TestCase):
    """A Windows machine: Chrome 157 (x64), Windows 11 24H2, a 1366x768 display at 100%."""

    def setUp(self):
        self.directory = scratch(self)
        self.binary = program(self.directory)
        self.machine = {"version": VERSION, "contract": (19, 0), "display": (1366, 768, 1.0),
                        "native": portable.PE_AMD64}
        for patcher in (
                mock.patch.object(browser_runtime, "BACKGROUND_ALLOWED", True),
                mock.patch.object(browser_runtime.sys, "platform", "win32"),
                mock.patch.object(setup_steps, "chrome_path", lambda platform=None, exists=None: self.binary),
                mock.patch.object(portable, "file_version", lambda path, api=None: self.machine["version"]),
                mock.patch.object(portable, "api_contract_version", lambda read=None: self.machine["contract"]),
                mock.patch.object(portable, "native_machine", lambda api=None: self.machine["native"]),
                mock.patch.object(portable, "primary_display", lambda api=None: self.machine["display"])):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.config = {"browser_driver": {"profile": str(self.directory / "profile")}}

    def compose(self, operation="inspect", config=None):
        argv = browser_runtime.guard_command(config or self.config, self.directory, operation)
        guard, mcp = argv[:argv.index("--")], argv[argv.index("--") + 1:]
        launch = json.loads((self.directory / "browser-config.json").read_text())
        return guard, mcp, launch["browser"]["launchOptions"]


class Composition(WindowsCase):
    def test_every_session_is_hidden_and_tells_the_page_what_this_machine_is(self):
        for operation in browser_runtime.HEADLESS_OPERATIONS:
            guard, mcp, launch = self.compose(operation)
            self.assertEqual(guard[guard.index("--background-chrome") + 1], str(self.directory / "profile"))
            self.assertEqual(guard[guard.index("--headless-user-agent") + 1], UA_157, operation)
            self.assertNotIn("--offscreen", guard)                    # Windows has no minimized mode
            self.assertNotIn("--user-data-dir", mcp)                  # the guard attaches it (--cdp-endpoint)
            self.assertNotIn("--headless", mcp)
            self.assertNotIn("args", launch)                          # the fallback: no off-screen switches
            self.assertNotIn("executablePath", launch)
        hints = json.loads(guard[guard.index("--headless-client-hints") + 1])
        self.assertEqual(hints, {
            "brands": browser_runtime.brand_list("157", "157"), "fullVersionList": browser_runtime.brand_list("157", VERSION),
            "fullVersion": VERSION, "platform": "Windows", "platformVersion": "19.0.0", "architecture": "x86",
            "bitness": "64", "model": "", "mobile": False, "wow64": False, "formFactors": ["Desktop"]})
        self.assertIn({"brand": "Google Chrome", "version": VERSION}, hints["fullVersionList"])
        self.assertNotIn("Mac", json.dumps(guard))
        self.assertEqual(guard[guard.index("--headless-screen") + 1], "1366x768@1")
        self.assertTrue(live_view.supported(self.config))

    def test_the_architecture_is_the_machines_not_what_chrome_was_built_for(self):
        # V8: the x64 Chrome on Windows-on-ARM (emulated) says "arm", as the ARM64 Chrome there does.
        self.machine.update(native=portable.PE_ARM64, contract=(15, 0))
        for built in (portable.PE_AMD64, portable.PE_ARM64):
            hints = browser_runtime.windows_client_hints(program(self.directory, built))
            self.assertEqual((hints["architecture"], hints["bitness"], hints["platformVersion"]), ("arm", "64", "15.0.0"))
        guard, _, _ = self.compose()
        self.assertEqual(json.loads(guard[guard.index("--headless-client-hints") + 1])["architecture"], "arm")
        self.machine["native"] = portable.PE_AMD64
        self.assertEqual(browser_runtime.windows_client_hints(self.binary)["architecture"], "x86")
        self.assertIsNone(browser_runtime.windows_client_hints(program(self.directory, 0x014C)))   # 32-bit: refused
        self.machine["native"] = 0x014C                               # a 32-bit Windows: not guessed
        self.assertIsNone(browser_runtime.windows_client_hints(self.binary))

    def test_one_read_decides_for_the_guard_and_the_browser_server_the_profile_is_always_the_users(self):
        # V10: a read that fails for one and works for the other once left no profile at all, so
        # @playwright/mcp would have used a profile of its own.
        profile = str(self.directory / "profile")
        for pattern in ((None, "ok"), ("ok", None), (None, None), ("ok", "ok"), (None, "ok", None, "ok")):
            answers = list(pattern)

            def display(api=None):
                value = answers.pop(0) if answers else pattern[-1]
                return (1366, 768, 1.0) if value else None
            with mock.patch.object(portable, "primary_display", display):
                guard, mcp, _ = self.compose()
            by_guard = "--background-chrome" in guard and guard[guard.index("--background-chrome") + 1] == profile
            by_server = "--user-data-dir" in mcp and mcp[mcp.index("--user-data-dir") + 1] == profile
            self.assertEqual((by_guard, by_server).count(True), 1, pattern)       # exactly one opens it
            self.assertEqual(by_guard, "--headless-user-agent" in guard, pattern)
            self.assertEqual(by_guard, "--headless-screen" in guard and "--headless-client-hints" in guard, pattern)

    def test_what_cannot_be_read_is_never_made_up_the_session_runs_as_before(self):
        for key, value in (("version", None), ("contract", None), ("display", None), ("native", None)):
            with mock.patch.dict(self.machine, {key: value}):
                guard, mcp, launch = self.compose()
                self.assertNotIn("--background-chrome", guard, key)
                self.assertNotIn("--headless-user-agent", guard, key)
                self.assertNotIn("--headless-screen", guard, key)
                self.assertEqual(mcp[mcp.index("--user-data-dir") + 1], str(self.directory / "profile"))
                self.assertNotIn("--headless", mcp, key)               # Playwright's ordinary window, as in 0.4.3
                self.assertNotIn("--cdp-endpoint", mcp, key)
                self.assertNotIn("args", launch)
                self.assertFalse(live_view.supported(self.config))
        self.binary = program(self.directory, 0x014C)                 # a 32-bit Chrome: not guessed
        self.assertIsNone(browser_runtime.windows_client_hints(self.binary))
        self.assertNotIn("--background-chrome", self.compose()[0])
        self.assertIsNone(browser_runtime.windows_display(lambda: None))

    def test_the_users_draft_window_tests_and_left_out_operations_are_unchanged(self):
        guard, mcp, _ = self.compose("support_email")                 # the draft window itself: the user's
        self.assertNotIn("--background-chrome", guard)
        self.assertIn("--user-data-dir", mcp)
        self.assertNotIn("--headless", mcp)
        self.assertNotIn("--cdp-endpoint", mcp)
        guard, mcp, _ = self.compose(config={"browser_driver": {"isolated": True}})
        self.assertNotIn("--background-chrome", guard)
        self.assertIn("--isolated", mcp)
        self.assertNotIn("--headless", mcp)
        self.assertNotIn("--cdp-endpoint", mcp)
        left_out = {"browser_driver": dict(self.config["browser_driver"], headless_operations=["submit"])}
        guard, mcp, _ = self.compose(config=left_out)
        self.assertNotIn("--background-chrome", guard)
        self.assertIn("--user-data-dir", mcp)
        self.assertNotIn("--headless", mcp)
        self.assertNotIn("--cdp-endpoint", mcp)
        self.assertFalse(browser_runtime.offscreen(self.config))      # the Mac's Dock words are not Windows'
        self.assertFalse(browser_runtime.background_launch(self.config))

    def test_the_profile_folder_is_the_one_playwright_used(self):
        guard, _, _ = self.compose()
        self.assertEqual(guard[guard.index("--background-chrome") + 1], str(browser_runtime.profile(self.config)))
        with mock.patch.dict(self.machine, {"display": None}):
            _, mcp, _ = self.compose()
        self.assertEqual(mcp[mcp.index("--user-data-dir") + 1], str(browser_runtime.profile(self.config)))


class FromTheWindowsApi(unittest.TestCase):
    def test_the_real_display_and_versions_flow_from_the_api_to_chromes_arguments(self):
        directory = scratch(self)
        binary = program(directory)
        launched = []

        def popen(argv, **options):
            launched.append((argv, options))
            (directory / "profile" / browser_runtime.DEVTOOLS_PORT_FILE).write_text("53999\n/devtools/browser/x")
            return mock.Mock(pid=777, _handle=0x77)
        kernel32 = fake_windows.FakeKernel32(threads=[(1, 777)])
        with fake_windows.windows(kernel32), \
                mock.patch.object(browser_runtime, "BACKGROUND_ALLOWED", True), \
                mock.patch.object(browser_runtime.sys, "platform", "win32"), \
                mock.patch.object(setup_steps, "chrome_path", lambda platform=None, exists=None: binary), \
                mock.patch.object(portable, "_version_api", lambda: VersionApi({binary: VERSION})), \
                mock.patch.object(portable, "_read_contract", lambda: 0x00130000), \
                mock.patch.object(portable, "_MachineApi", lambda: MachineApi()), \
                mock.patch.object(browser_runtime, "running_version", lambda endpoint, opener=None: VERSION), \
                mock.patch.object(portable, "_DisplayApi", lambda: DisplayApi((1920, 1080), 1536, 96)), \
                mock.patch.object(browser_guard.subprocess, "Popen", popen):   # Chrome, then the browser server
            self.addCleanup(browser_runtime._STARTED.clear)
            argv = browser_runtime.guard_command({"browser_driver": {"profile": str(directory / "profile")}},
                                                 directory, "inspect")
            guard = argv[:argv.index("--")]
            self.assertEqual(guard[guard.index("--headless-screen") + 1], "1536x864@1.25")
            hints = json.loads(guard[guard.index("--headless-client-hints") + 1])
            self.assertEqual((hints["fullVersion"], hints["platformVersion"], hints["platform"]),
                             (VERSION, "19.0.0", "Windows"))
            proxy = browser_guard.Proxy(
                "inspect", ["node", "cli.js"], audit=directory / "guard.jsonl", stdin=io.StringIO(""),
                stdout=io.StringIO(), focus_keeper=False,
                background_chrome=guard[guard.index("--background-chrome") + 1],
                headless_user_agent=guard[guard.index("--headless-user-agent") + 1], client_hints=hints,
                make_tab_details=NoTabs,
                screen=browser_guard.parse_screen(guard[guard.index("--headless-screen") + 1]))
            self.addCleanup(proxy.audit.close)
            chrome, options = launched[0]
            self.assertEqual(chrome[:3], [binary, "--user-data-dir=" + str(directory / "profile"),
                                          "--remote-debugging-port=0"])
            for switch in ("--headless=new", "--window-size=1280,864", "--force-device-scale-factor=1.25",
                           "--user-agent=" + UA_157, browser_runtime.AUTOMATION_SWITCH):
                self.assertIn(switch, chrome)
            self.assertFalse([switch for switch in chrome if switch.startswith("--window-position")])
            self.assertNotIn("--no-sandbox", chrome)
            # In a Job Object of its own, held by the guard: suspended, taken by the job, then resumed.
            self.assertEqual(options["creationflags"],
                             portable.CREATE_NEW_PROCESS_GROUP | portable.CREATE_NO_WINDOW | portable.CREATE_SUSPENDED)
            names = kernel32.names()
            self.assertLess(names.index("AssignProcessToJobObject"), names.index("ResumeThread"))
            self.assertTrue(kernel32.limits & portable.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
            self.assertTrue(proxy.headless)
            self.assertEqual(launched[1][0][-2:], ["--cdp-endpoint", "http://127.0.0.1:53999"])
            browser = Browser()
            proxy.child_call, proxy.to_child = browser, (lambda message: None)
            proxy.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
            self.assertEqual(browser.kinds(), ["hints"])                # nothing is placed or minimized
            self.assertIn('"screenWidth": 1536', browser.codes[0])
            self.assertIn('"screenHeight": 864', browser.codes[0])
            self.assertIn('"platformVersion": "19.0.0"', browser.codes[0])
            # The session's end: that Chrome's job is ended, never a process by its pid.
            with mock.patch.object(browser_runtime.os, "kill") as kill:
                proxy.stop_background_chrome()
            kill.assert_not_called()
            self.assertIn("TerminateJobObject", kernel32.names())
            self.assertEqual(browser_runtime._STARTED, {})


class Guard(WindowsCase):
    def proxy(self, **settings):
        with mock.patch.object(browser_guard.subprocess, "Popen") as popen:
            proxy = browser_guard.Proxy(
                "submit", ["node", "cli.js"], audit=self.directory / "guard.jsonl", stdin=io.StringIO(""),
                stdout=io.StringIO(), focus_keeper=False, background_chrome=str(self.directory / "profile"),
                headless_user_agent=UA_157, **dict({"make_tab_details": NoTabs}, **settings))
        self.child_argv = popen.call_args[0][0]
        self.addCleanup(proxy.audit.close)
        proxy.spawn_child = mock.Mock
        proxy.to_child = lambda message: None
        return proxy

    def decisions(self):
        return [json.loads(line)["decision"] for line in (self.directory / "guard.jsonl").read_text().splitlines()]

    def test_a_failed_hidden_launch_gives_playwrights_ordinary_window(self):
        def fail(binary, path, user_agent, screen=None):
            raise OSError("Chrome wrote no DevTools port within 60 s")
        hints, screen = browser_runtime.windows_client_hints(self.binary), browser_runtime.windows_display()
        with mock.patch.object(browser_runtime, "launch_hidden_windows", fail), \
                mock.patch.object(browser_runtime, "launch_background") as mac_launch:
            proxy = self.proxy(client_hints=hints, screen=screen)
        mac_launch.assert_not_called()                                # no `open -g`, no minimized mode
        self.assertEqual(self.child_argv, ["node", "cli.js", "--user-data-dir", str(self.directory / "profile")])   # the whole command: no --headless, no --cdp-endpoint
        self.assertFalse(proxy.headless)
        self.assertFalse(proxy.offscreen)
        self.assertIsNone(proxy.background_chrome)                    # Playwright's Chrome is Playwright's to close
        self.assertEqual(self.decisions(), ["HEADLESS_LAUNCH_FAILED"])
        self.assertIsNone(proxy.make_focus_keeper(headed=True))

    def launched(self, running):
        """A guard whose hidden Chrome reports `running` (a list: one version per launch) on its DevTools
        port; returns (proxy, the launches' user agents, the profiles ended)."""
        hints, screen = browser_runtime.windows_client_hints(self.binary), browser_runtime.windows_display()
        versions, launches, ended = list(running), [], []

        def launch(binary, path, user_agent, screen=None):
            launches.append(user_agent)
            return "http://127.0.0.1:%d" % (53000 + len(launches))
        with mock.patch.object(browser_runtime, "launch_hidden_windows", launch), \
                mock.patch.object(browser_runtime, "running_version", lambda endpoint, opener=None: versions.pop(0)), \
                mock.patch.object(browser_runtime, "end_session",
                                  lambda config: ended.append(config["browser_driver"]["profile"])):
            proxy = self.proxy(client_hints=hints, screen=screen)
        return proxy, launches, ended

    def test_the_running_chromes_own_version_is_what_is_told_never_a_stale_one(self):
        # A Chrome update can swap chrome.exe as it starts: what was read before the launch is then old.
        proxy, launches, ended = self.launched([VERSION])                       # the same: nothing changes
        self.assertEqual((launches, ended), ([UA_157], []))
        self.assertEqual(proxy.client_hints, browser_runtime.windows_client_hints(self.binary))
        self.assertEqual(self.child_argv, ["node", "cli.js", "--cdp-endpoint", "http://127.0.0.1:53001"])
        self.assertNotIn("CHROME_VERSION_CHANGED", self.decisions())

        newer = "158.0.8200.7"
        proxy, launches, ended = self.launched([newer, newer])
        ua_158 = UA_157.replace("Chrome/157.", "Chrome/158.")
        self.assertEqual(launches, [UA_157, ua_158])                            # started again, told the truth
        self.assertEqual(ended, [str(self.directory / "profile")])              # the first one is ended first
        self.assertEqual(self.child_argv, ["node", "cli.js", "--cdp-endpoint", "http://127.0.0.1:53002"])
        self.assertTrue(proxy.headless)
        self.assertEqual(proxy.headless_user_agent, ua_158)
        hints = proxy.client_hints
        self.assertEqual(hints["fullVersion"], newer)
        self.assertEqual(hints["brands"], browser_runtime.brand_list("158", "158"))
        self.assertEqual(hints["fullVersionList"], browser_runtime.brand_list("158", newer))
        self.assertEqual((hints["platform"], hints["platformVersion"], hints["architecture"]), ("Windows", "19.0.0", "x86"))
        code = browser_guard.client_hints_code(proxy.headless_user_agent, proxy.client_hints, proxy.screen)
        self.assertNotIn("157", code)                                           # nothing stale reaches a page
        changed = [json.loads(line) for line in (self.directory / "guard.jsonl").read_text().splitlines()
                   if "CHROME_VERSION_CHANGED" in line][-1]
        self.assertEqual((changed["told"], changed["running"]), (VERSION, newer))
        launched = [json.loads(line) for line in (self.directory / "guard.jsonl").read_text().splitlines()
                    if "BACKGROUND_LAUNCHED" in line][-1]
        self.assertEqual(launched["endpoint"], "http://127.0.0.1:53002")        # the live view's endpoint

    def test_a_version_that_cannot_be_confirmed_is_never_presented(self):
        for running in ([None], ["158.0.8200.7", "159.0.1.1"], ["158.0.8200.7", None]):
            proxy, launches, ended = self.launched(running)
            self.assertFalse(proxy.headless, running)
            self.assertIsNone(proxy.background_chrome)
            self.assertEqual(self.child_argv, ["node", "cli.js", "--user-data-dir", str(self.directory / "profile")])   # the whole command: no --headless, no --cdp-endpoint
            self.assertEqual(len(ended), len(running))                          # every Chrome it started is ended
            self.assertEqual(self.decisions()[-1], "HEADLESS_LAUNCH_FAILED")

    def test_the_running_version_is_read_from_chromes_own_devtools_port(self):
        import io as _io
        seen = []

        def opener(url, timeout=None):
            seen.append(url)
            return _io.BytesIO(json.dumps({"Browser": "Chrome/157.0.8103.51", "User-Agent": "x"}).encode())
        self.assertEqual(browser_runtime.running_version("http://127.0.0.1:53001", opener), VERSION)
        self.assertEqual(seen, ["http://127.0.0.1:53001/json/version"])
        for body in (b"not json", b'{"Browser": "Chrome"}', b'{"Browser": 7}', b"[]"):
            self.assertIsNone(browser_runtime.running_version("http://127.0.0.1:1", lambda url, timeout=None: _io.BytesIO(body)))
        headless = lambda url, timeout=None: _io.BytesIO(b'{"Browser": "HeadlessChrome/157.0.8103.51"}')
        self.assertEqual(browser_runtime.running_version("http://127.0.0.1:1", headless), VERSION)

        def refused(url, timeout=None):
            raise OSError("connection refused")
        self.assertIsNone(browser_runtime.running_version("http://127.0.0.1:1", refused))
        self.assertIsNone(browser_runtime.running_version("https://example.com", opener))   # this computer only

    def test_a_guard_given_no_hints_or_screen_never_starts_chrome_hidden(self):
        with mock.patch.object(browser_runtime, "launch_hidden_windows") as launch:
            self.proxy(client_hints=None, screen={"width": 1366, "height": 768, "scale": 1.0})
            self.assertEqual(self.child_argv, ["node", "cli.js", "--user-data-dir", str(self.directory / "profile")])   # the whole command: no --headless, no --cdp-endpoint
            self.proxy(client_hints={"platform": "Windows"}, screen=None)
            self.assertEqual(self.child_argv, ["node", "cli.js", "--user-data-dir", str(self.directory / "profile")])   # the whole command: no --headless, no --cdp-endpoint
        launch.assert_not_called()

    def test_chrome_is_asked_to_close_before_it_is_stopped(self):
        asked = []

        def call(method, params):
            asked.append(params["arguments"]["code"])
            return {"result": {"content": [{"type": "text", "text": '### Result\n[{"closing": true}]'}]}}
        proxy = self.proxy(launch_headless=lambda path, ua: "http://127.0.0.1:53111")
        proxy.child, proxy.child_call = mock.Mock(poll=lambda: None), call
        waited = []
        with mock.patch.object(portable, "wait_profile_free", lambda path, seconds: waited.append(path)):
            proxy.close_own_chrome()
            self.assertEqual(asked, [browser_guard.CLOSE_CODE])
            self.assertEqual(waited, [str(self.directory / "profile")])
            self.assertIn("Browser.close", browser_guard.CLOSE_CODE)
            proxy.child = mock.Mock(poll=lambda: 0)                   # the browser server is gone: nothing to ask
            proxy.close_own_chrome()
            proxy.child = mock.Mock(poll=lambda: None)
            proxy.operation, proxy.headless = "support_email", False  # the user's draft window is never closed here
            proxy.close_own_chrome()
            proxy.headless = True
            proxy.operation = "submit"
            # macOS too (2026-11-13: a sign-in made seconds before the end was not in the profile afterwards):
            # asked the same way, then waited for by the profile's own lock, never by Windows' lockfile.
            gone = []
            with mock.patch.object(browser_runtime.sys, "platform", "darwin"), \
                    mock.patch.object(browser_runtime, "wait_chrome_gone", lambda path, seconds: gone.append((path, seconds))):
                # A session an engine started: asked, and nothing waits here (the engine may wait for the guard,
                # inside the session's time limit); its starter does the waiting (browser_runtime.close_cleanly).
                with mock.patch.dict(browser_guard.os.environ, {}, clear=False):
                    browser_guard.os.environ.pop(browser_runtime.CLEAN_CLOSE_VARIABLE, None)
                    proxy.close_own_chrome()
                    self.assertEqual((len(asked), gone), (2, []))
                # The live view's guard (its starter says so): asked, then waited for.
                with mock.patch.dict(browser_guard.os.environ, {browser_runtime.CLEAN_CLOSE_VARIABLE: "1"}):
                    proxy.close_own_chrome()
        self.assertEqual(len(asked), 3)
        self.assertEqual(gone, [(str(self.directory / "profile"), browser_runtime.CLOSE_WAIT_SECONDS)])
        self.assertEqual(len(waited), 1)


class LocalReadsUseNoProxy(WindowsCase):
    """Round 5: a read of this computer's own Chrome (its DevTools port) never goes through a proxy. On a PC
    with a system proxy or HTTP_PROXY it used to go there, fail, and leave every session in a window."""

    def setUp(self):
        super().setUp()
        import http.server
        import socket
        import threading
        import urllib.request

        class Chrome(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"Browser": "Chrome/" + VERSION} if self.path == "/json/version" else
                                  [{"type": "page"}, {"type": "page"}, {"type": "service_worker"}]).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass
        self.server = http.server.HTTPServer(("127.0.0.1", 0), Chrome)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.endpoint = "http://127.0.0.1:%d" % self.server.server_address[1]
        with socket.socket() as closed:                   # an address nothing listens on
            closed.bind(("127.0.0.1", 0))
            self.dead = "http://127.0.0.1:%d" % closed.getsockname()[1]
        self.addCleanup(urllib.request.install_opener, None)

    def proxied(self, how):
        """`how`: "environment" (HTTP_PROXY) or "system" (the Windows system proxy, whose bypass list does
        not exempt 127.0.0.1). urllib's cached default opener is dropped so the setting takes effect."""
        import contextlib
        import os
        import urllib.request
        stack = contextlib.ExitStack()
        clean = {key: value for key, value in os.environ.items() if key.lower() not in ("http_proxy", "no_proxy", "all_proxy")}
        if how == "environment":
            stack.enter_context(mock.patch.dict(os.environ, dict(clean, HTTP_PROXY=self.dead, http_proxy=self.dead), clear=True))
        else:
            stack.enter_context(mock.patch.dict(os.environ, clean, clear=True))
            stack.enter_context(mock.patch.object(urllib.request, "getproxies", lambda: {"http": self.dead}))
        stack.enter_context(mock.patch.object(urllib.request, "proxy_bypass", lambda host, proxies=None: False))
        # sys.platform is faked as Windows on a Mac: building any opener would look for Windows' certificate
        # store. No https is read here.
        import ssl
        stack.enter_context(mock.patch.object(ssl.SSLContext, "load_default_certs", lambda self, *args: None))
        urllib.request.install_opener(None)
        return stack

    def test_control_the_proxy_setting_really_breaks_an_ordinary_read(self):
        import urllib.error
        import urllib.request
        for how in ("environment", "system"):
            with self.proxied(how), self.assertRaises((urllib.error.URLError, OSError)):
                urllib.request.urlopen(self.endpoint + "/json/version", timeout=5).read()

    def test_the_version_read_succeeds_and_the_session_stays_hidden(self):
        hints, screen = browser_runtime.windows_client_hints(self.binary), browser_runtime.windows_display()
        for how in ("environment", "system"):
            with self.proxied(how):
                self.assertEqual(browser_runtime.running_version(self.endpoint), VERSION, how)
                with mock.patch.object(browser_runtime, "launch_hidden_windows",
                                       lambda binary, path, user_agent, screen=None: self.endpoint), \
                        mock.patch.object(browser_runtime, "end_session") as ended, \
                        mock.patch.object(browser_guard.subprocess, "Popen") as popen:
                    proxy = browser_guard.Proxy(
                        "inspect", ["node", "cli.js"], audit=self.directory / "guard.jsonl", stdin=io.StringIO(""),
                        stdout=io.StringIO(), focus_keeper=False, background_chrome=str(self.directory / "profile"),
                        headless_user_agent=UA_157, client_hints=hints, screen=screen, make_tab_details=NoTabs)
                    self.addCleanup(proxy.audit.close)
                self.assertTrue(proxy.headless, how)
                self.assertEqual(popen.call_args[0][0][-2:], ["--cdp-endpoint", self.endpoint])
                ended.assert_not_called()

    def test_the_draft_windows_page_count_is_read_without_a_proxy(self):
        profile = self.directory / "profile"
        profile.mkdir()
        (profile / browser_runtime.DEVTOOLS_PORT_FILE).write_text("%d\n/devtools/browser/x" % self.server.server_address[1])
        for how in ("environment", "system"):
            with self.proxied(how):
                self.assertEqual(support_email.open_pages(profile), 2, how)


class SupportEmail(WindowsCase):
    """V7 (owner, 2026-11-12): the support email's first phase is hidden on Windows, as on the Mac."""

    def test_the_first_phase_is_hidden_and_the_draft_window_is_the_users(self):
        self.assertTrue(support_email.headless_first(self.config, "Gmail"))
        self.assertFalse(support_email.headless_first(self.config, "Yahoo Mail"))      # no reopenable draft
        phase = dict(self.config, **{browser_runtime.HEADLESS_PHASE: True})
        guard, mcp, launch = self.compose("support_email", phase)
        self.assertEqual(guard[guard.index("--background-chrome") + 1], str(self.directory / "profile"))
        self.assertEqual(guard[guard.index("--headless-user-agent") + 1], UA_157)
        self.assertEqual(guard[guard.index("--headless-screen") + 1], "1366x768@1")
        self.assertIn("--headless-client-hints", guard)
        self.assertNotIn("--user-data-dir", mcp)
        self.assertNotIn("args", launch)
        guard, mcp, _ = self.compose("support_email")                 # the visible phase: unchanged
        self.assertNotIn("--background-chrome", guard)

    def test_a_machine_that_cannot_be_read_keeps_the_visible_path_from_the_start(self):
        with mock.patch.dict(self.machine, {"display": None}):
            self.assertFalse(support_email.headless_first(self.config, "Gmail"))
        self.assertFalse(support_email.headless_first({"browser_driver": {"isolated": True}}, "Gmail"))

    def test_the_saved_draft_reopens_in_a_window_sidedoor_starts(self):
        opened = []
        with mock.patch.object(browser_runtime, "launch_windows",
                               lambda binary, path, url=None: opened.append((binary, path, url))), \
                mock.patch.object(browser_runtime, "launch_background") as mac_launch:
            support_email.reopen(self.directory / "profile", "https://mail.google.com/mail/u/0/#drafts?compose=x")
        mac_launch.assert_not_called()
        self.assertEqual(opened, [(self.binary, self.directory / "profile",
                                   "https://mail.google.com/mail/u/0/#drafts?compose=x")])

    def test_the_guard_ends_the_hidden_phases_chrome_but_never_the_users_window(self):
        Proxy = browser_guard.Proxy
        with mock.patch.object(browser_runtime, "end_session") as end:
            hidden = mock.Mock(operation="support_email", background_chrome="/p", headless=True)
            hidden.users_window = lambda: Proxy.users_window(hidden)
            Proxy.stop_background_chrome(hidden)
            end.assert_called_once()                                  # only this guard's job can end it
            window = mock.Mock(operation="support_email", background_chrome="/p", headless=False)
            window.users_window = lambda: Proxy.users_window(window)
            Proxy.stop_background_chrome(window)
            end.assert_called_once()
            with mock.patch.object(browser_runtime.sys, "platform", "darwin"):
                Proxy.stop_background_chrome(hidden)                  # the Mac: support_email ends it, as before
            end.assert_called_once()


class MacUnchanged(unittest.TestCase):
    def build(self):
        root = scratch(self)
        app = root / "Google Chrome.app"
        (app / "Contents" / "MacOS").mkdir(parents=True)
        with open(app / "Contents" / "Info.plist", "wb") as stream:
            plistlib.dump({"CFBundleShortVersionString": "141.0.7390.123"}, stream)
        binary = str(app / "Contents" / "MacOS" / "Google Chrome")
        out = {}
        with mock.patch.object(browser_runtime, "BACKGROUND_ALLOWED", True), \
                mock.patch.object(browser_runtime.sys, "platform", "darwin"), \
                mock.patch.object(platform, "mac_ver", lambda: ("15.6.1", ("", "", ""), "arm64")), \
                mock.patch.object(platform, "machine", lambda: "arm64"), \
                mock.patch.object(browser_runtime, "display", lambda quartz=None: (1728, 1117, 2.0)), \
                mock.patch.object(setup_steps, "chrome_path", lambda platform=None, exists=None: binary):
            for operation in ("inspect", "submit", "linkedin", "support_email"):
                for name, driver in (("default", {}), ("minimized", {"headless_operations": []})):
                    directory = root / operation / name
                    argv = browser_runtime.guard_command({"browser_driver": dict(profile="/p/profile", **driver)},
                                                         directory, operation)
                    argv = argv[argv.index("browser-guard"):]
                    text = json.dumps(argv).replace(str(directory), "<DIR>").replace(
                        str(profile_home.HOME), "<HOME>").replace(str(root), "<ROOT>")
                    launch = (directory / "browser-config.json").read_text().replace(str(root), "<ROOT>")
                    out[operation + "/" + name] = [text, launch]
            out["headless_command"] = browser_runtime.headless_command("/chrome", Path("/p"), "UA", 2.0)
            out["background_command"] = browser_runtime.background_command(Path("/x.app"), Path("/p"))
        return out

    def test_the_mac_guard_and_chrome_arguments_are_byte_for_byte_what_they_were(self):
        out = self.build()
        self.assertEqual(hashlib.sha256(json.dumps(out, sort_keys=True).encode()).hexdigest(), MAC_GOLDEN)
        guard = out["inspect/default"][0]
        self.assertIn("Macintosh", guard)
        self.assertIn("macOS", guard)
        self.assertIn("1728x1117@2", guard)
        self.assertNotIn("Windows", json.dumps(out))


if __name__ == "__main__":
    unittest.main()
