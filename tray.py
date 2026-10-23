"""E5.T3: the tray icon (macOS menu bar, Windows notification area) of the Sidedoor process.

Icon `assets/brand/yoursidedoor-icon-512.png` (D33). Menu (E6.T0 sections 1 and 5):
- **Open dashboard**: the tokened link in the browser, the way `sidedoor serve` reopens a dashboard
  already running (`dashboard.announce`).
- **Pause** / **Resume**: the user's pause through the same function as Overview's buttons
  (`dashboard.set_user_pause` -> `scheduler_control.set_user_pause`), shown by Overview's rule:
  [Pause] whenever the user's pause is not set (it stays available under the license pause),
  [Resume] only while the user's pause is set and the license pause is not.
- **Quit**: the dashboard's normal stop (the same path as Ctrl-C and SIGTERM): the server and the
  scheduler loop stop, a running job finishes on its own (owner, E5.T1 (a)), exit code 0.

It runs in the dashboard process (`dashboard._serve`). pystray needs the main thread on macOS
(AppKit), so with a tray the web server runs in a thread and the tray owns the main thread. When the
tray cannot start (no display, a headless start, a missing library, an error from the backend) the
dashboard runs as before with no tray, and `tray-state.json` in the Sidedoor home says why, for
`sidedoor doctor` (`doctor.check_tray`).

The built app is a regular app with a Dock icon as well (owner, APP1 b; tools/build_macos.py
`--macos-app-mode=gui`). `dock_events` makes the Dock do what the tray does: clicking the Dock icon
while Sidedoor runs is Open dashboard, and the Dock's Quit (also Cmd-Q, `osascript ... quit`) is the
tray's Quit, the same clean stop with exit code 0. A quit that macOS sends for a log out, restart or
shut down starts that stop and lets macOS go on at once, so Sidedoor never holds up a log out.

From source (`python orchestrator.py serve`, not frozen) the process is Python's own app, so the Dock
would show "Python" with Python's icon (DK1 a). `brand_source_dock` sets the Dock icon to the brand
icon (`NSApplication.setApplicationIconImage_`, reliable while the process runs) and, best-effort,
CFBundleName "Sidedoor" in the main bundle's in-memory info dictionary before NSApplication starts.
That name reaches the application menu's title in many cases, but the Dock's label is read by the
Dock from the bundle on disk (Python.app), so from source the Dock label can still say "Python";
only the packaged app (its own Info.plist) shows "Sidedoor" there for sure. The packaged app is
left alone. Every failure is silent.
"""

import json
import os
import sys
import threading
from pathlib import Path

import resources
import log

LOG = log.get(__name__)


ICON = "yoursidedoor-icon-512.png"
STATE_FILE = "tray-state.json"
TITLE = "Your Sidedoor"
REFRESH_SECONDS = 3  # how often a pause set elsewhere (Overview, the license) reaches the menu


def icon_path():
    return resources.path("assets/brand") / ICON


def unavailable(platform=None, environ=None, gui_session=None):
    """Why no tray can be shown here, or None. Library checks come in `load`."""
    platform = platform or sys.platform
    environ = os.environ if environ is None else environ
    if platform.startswith("linux"):
        return "No tray icon on Linux yet."
    if platform == "darwin":
        session = gui_session if gui_session is not None else _mac_gui_session
        if not session():
            return "No desktop session (Sidedoor was started without a logged-in screen)."
    return None


def _mac_gui_session():
    """True when this process is in a logged-in window-server session (not ssh, not a daemon)."""
    try:
        import Quartz
        info = Quartz.CGSessionCopyCurrentDictionary()
    except Exception:
        return False
    return bool(info) and bool(info.get("kCGSSessionOnConsoleKey", True))


DOCK_NAME = "Sidedoor"


def _frozen():
    """True in a frozen build: PyInstaller-style `sys.frozen`, or Nuitka's `__compiled__`."""
    return bool(getattr(sys, "frozen", False)) or "__compiled__" in globals()


def brand_source_dock(appkit=None, foundation=None, frozen=None):
    """macOS, from source only: the Dock shows the brand icon and (best-effort) the name Sidedoor
    instead of Python's. Call before NSApplication starts (the name is read when it does). Returns
    True when the icon was set; never raises."""
    if _frozen() if frozen is None else frozen:
        return False
    try:
        foundation = foundation or __import__("Foundation")
        info = foundation.NSBundle.mainBundle().infoDictionary()
        if info is not None:
            info["CFBundleName"] = DOCK_NAME
    except Exception:
        pass
    try:
        appkit = appkit or __import__("AppKit")
        picture = appkit.NSImage.alloc().initWithContentsOfFile_(str(icon_path()))
        if picture is None:
            return False
        appkit.NSApplication.sharedApplication().setApplicationIconImage_(picture)
        return True
    except Exception:
        return False


def load():
    """(pystray module, PIL.Image module) or raises ImportError."""
    import pystray
    from PIL import Image
    return pystray, Image


def write_state(home, state, reason=None):
    """Record what the tray did at this start (`doctor.check_tray`). Never raises."""
    try:
        path = Path(home) / STATE_FILE
        temporary = path.with_name(path.name + ".%d.tmp" % os.getpid())
        temporary.write_text(json.dumps({"state": state, "reason": reason, "pid": os.getpid()}), encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except Exception:
        pass


def read_state(home):
    try:
        value = json.loads((Path(home) / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def menu_visibility(flags):
    """(show Pause, show Resume) from the pause flags (None: unreadable). Overview's rule."""
    flags = flags if isinstance(flags, dict) else {}
    user, license_ = bool(flags.get("user_paused_at")), bool(flags.get("license_paused_at"))
    return (not user, user and not license_)


class Tray:
    """The icon and its menu. `actions` has open_dashboard(), pause(), resume(), quit() and
    flags() (the pause flags or None). `modules` is (pystray, PIL.Image), injectable for tests;
    `call_on_main` runs a callable on the tray's thread (AppKit on macOS)."""

    def __init__(self, actions, modules=None, call_on_main=None, dock=None):
        self.actions = actions
        # The Dock's reopen and quit (macOS, the real backend only; tests inject fake modules).
        self.dock = dock if dock is not None else (modules is None and sys.platform == "darwin")
        self._dock_delegate = None
        self.pystray, self.image = modules if modules is not None else load()
        self.call_on_main = call_on_main or _call_on_main
        self.icon = None
        self._flags = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._ready = False

    def _visible(self, which):
        def visible(item):
            return menu_visibility(self._flags)[which]
        return visible

    def _do(self, action):
        def handler(icon, item=None):
            try:
                action()
            except Exception as error:  # a busy database: the next click tries again
                LOG.warning("Tray: %s failed (%s).", getattr(action, "__name__", "action"), type(error).__name__)
            self._refresh_flags()
        return handler

    def _quit_now(self):
        """The Dock's or the system's quit (AP5): the stop itself, nothing else. No pause-flag refresh
        (a database read) runs inside applicationShouldTerminate_, so a log out is never delayed."""
        try:
            self.actions.quit()
        except Exception as error:
            print("Tray: quit failed (%s)." % type(error).__name__, file=sys.stderr, flush=True)

    def _refresh_flags(self):
        try:
            self._flags = self.actions.flags()
        except Exception:
            self._flags = None
        return self._flags

    def menu(self):
        pystray = self.pystray
        Item = pystray.MenuItem
        return pystray.Menu(
            Item("Open dashboard", self._do(self.actions.open_dashboard), default=True),
            Item("Pause", self._do(self.actions.pause), visible=self._visible(0)),
            Item("Resume", self._do(self.actions.resume), visible=self._visible(1)),
            Item("Quit", self._do(self.actions.quit)))

    def run(self, on_ready=None):
        """Show the icon and block until `stop` (call on the main thread). Raises when the backend
        fails to start; `dashboard._serve` then goes on without a tray."""
        if self._stop.is_set():
            return
        self._refresh_flags()
        if self.dock:  # before pystray's Icon starts NSApplication
            brand_source_dock()
        with self.image.open(icon_path()) as picture:
            picture.load()
            self.icon = self.pystray.Icon("sidedoor", picture.copy(), TITLE, self.menu())
        if self.dock:
            try:
                opening = self._do(lambda: self.actions.open_dashboard())
                self._dock_delegate = dock_events(lambda: opening(self.icon), self._quit_now)
            except Exception as error:  # the tray still works; the Dock icon then only shows
                print("Tray: the Dock's Open and Quit are not connected (%s)." % type(error).__name__,
                      file=sys.stderr, flush=True)

        def setup(icon):
            icon.visible = True
            with self._lock:
                self._ready = True
                stopped = self._stop.is_set()
            if stopped:  # stop() came before the icon could take it
                self.call_on_main(icon.stop)
                return
            threading.Thread(target=self._watch, name="sidedoor-tray-refresh", daemon=True).start()
            if on_ready is not None:
                on_ready()
        try:
            self.icon.run(setup=setup)
        finally:
            self._stop.set()

    def _watch(self):
        """Keep the menu in step with a pause set elsewhere (Overview, the license)."""
        while not self._stop.wait(REFRESH_SECONDS):
            before = self._flags
            after = self._refresh_flags()
            if menu_visibility(before) != menu_visibility(after) and self.icon is not None:
                self.call_on_main(self.icon.update_menu)

    def stop(self):
        """From any thread: end `run`."""
        with self._lock:
            self._stop.set()
            ready = self._ready
        if ready and self.icon is not None:
            self.call_on_main(self.icon.stop)


# kAEQuitReason values a quit carries when macOS logs out, restarts or shuts down (a quit from the
# Dock, Cmd-Q or AppleScript carries none).
SYSTEM_QUIT_REASONS = {b"logo", b"rlgo", b"rrst", b"rsdn", b"rest", b"shut"}
_DELEGATE_CLASS = []


def system_quit():
    """True when the quit AppKit is handling now came from a log out, restart or shut down. An Apple
    event whose reason cannot be read counts as one (AP8): never risk holding up a log out."""
    import Foundation
    event = Foundation.NSAppleEventManager.sharedAppleEventManager().currentAppleEvent()
    if event is None:
        return False
    try:
        reason = event.attributeDescriptorForKeyword_(int.from_bytes(b"why?", "big"))
        return reason is not None and reason.typeCodeValue().to_bytes(4, "big") in SYSTEM_QUIT_REASONS
    except Exception:
        return True


def dock_events(open_dashboard, quit, app=None):
    """macOS: become NSApp's delegate (on the main thread, before its run loop) so a Dock click calls
    `open_dashboard` and the Dock's Quit calls `quit`. Returns the delegate; keep it (NSApp holds its
    delegate weakly). A user's quit is cancelled for AppKit (NSTerminateCancel) because `quit` is the
    clean stop that ends the run loop itself; a system quit answers NSTerminateNow after starting it."""
    import AppKit
    import Foundation
    if not _DELEGATE_CLASS:
        class SidedoorAppDelegate(Foundation.NSObject):
            def applicationShouldHandleReopen_hasVisibleWindows_(self, application, visible):
                self.open_dashboard()
                return False

            def applicationShouldTerminate_(self, application):
                system = False
                try:
                    system = system_quit()
                except Exception:
                    pass
                self.quit()
                return AppKit.NSTerminateNow if system else AppKit.NSTerminateCancel
        _DELEGATE_CLASS.append(SidedoorAppDelegate)
    delegate = _DELEGATE_CLASS[0].alloc().init()
    delegate.open_dashboard, delegate.quit = open_dashboard, quit
    (app or AppKit.NSApplication.sharedApplication()).setDelegate_(delegate)
    return delegate


def _call_on_main(function):
    """macOS: AppKit calls belong on the main thread (the run loop executes it); elsewhere at once."""
    if sys.platform == "darwin" and threading.current_thread() is not threading.main_thread():
        try:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(function)
            return
        except Exception:
            pass
    function()


def mac_signals(handler, signals):
    """macOS: while AppKit's run loop owns the main thread, Python signal handlers run only when
    Python code does; PyObjC's Mach signals wake the run loop instead. Call on the run loop (after
    pystray's own SIGINT hook, which would end the app without Sidedoor's cleanup). A later
    `signal.signal` for the same signal puts Python's handling back. No-op elsewhere."""
    if sys.platform != "darwin":
        return
    from PyObjCTools import MachSignals
    for number in signals:
        MachSignals.signal(number, handler)
