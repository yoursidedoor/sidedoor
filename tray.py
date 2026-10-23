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

    def __init__(self, actions, modules=None, call_on_main=None):
        self.actions = actions
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
        with self.image.open(icon_path()) as picture:
            picture.load()
            self.icon = self.pystray.Icon("sidedoor", picture.copy(), TITLE, self.menu())

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
