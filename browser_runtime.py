"""Where Sidedoor's browser driver lives and how Codex is pointed at it (E7.T1, option A).

Owner, 2026-10-22: bundled Node plus @playwright/mcp behind `sidedoor browser-guard`. Every browser job
(application inspect/investigate/submit/verify/reconcile, the LinkedIn contact finder, setup step 9's
email check) runs `codex exec` with ONE stdio MCP server named `browser`:

    <sidedoor> browser-guard --operation <op> --audit <dir>/guard.jsonl [--claim ..] [--human-check ..]
        -- <node> <runtime>/node_modules/@playwright/mcp/cli.js --browser chrome
           --user-data-dir <Sidedoor's profile> --no-webmcp --output-dir <dir>/browser --config <file>

`<sidedoor>` is `[<binary>, "browser-guard"]` (scheduler_loop.program) in a frozen build (the binary re-invoked with a
subcommand, as scheduler_loop.job_command does) and `[sys.executable, <orchestrator.py>, "browser-guard"]`
from source.

The runtime (nothing here downloads anything; the owner fetches and the build bundles it):
    <runtime>/node/bin/node (node.exe on Windows)            Node.js, pinned in NODE_VERSION
    <runtime>/node_modules/@playwright/mcp/cli.js            `npm ci` of browser-runtime/package-lock.json
<runtime> is config["browser_driver"]["runtime_dir"], else $SIDEDOOR_BROWSER_RUNTIME, else the
`browser-runtime` folder beside the frozen binary or beside this file. config["browser_driver"]["node"]
and ["mcp_cli"] name the two files directly when they live elsewhere.
"""

import json
import os
from pathlib import Path
import shutil
import sys

import log
import portable


ROOT = Path(__file__).resolve().parent
LOG = log.get(__name__)
ENTRY = ROOT / "orchestrator.py"
SERVER = "browser"
RUNTIME_VARIABLE = "SIDEDOOR_BROWSER_RUNTIME"
RUNTIME_FOLDER = "browser-runtime"
NODE_VERSION = "24.22.1"
PLAYWRIGHT_MCP_VERSION = "0.0.83"
MCP_CLI = Path("node_modules") / "@playwright" / "mcp" / "cli.js"
# The macOS cookie fix (FINDINGS §4b): Playwright's default --use-mock-keychain makes Chrome drop the
# cookies the profile saved with the real Keychain; --enable-automation shows the automation bar.
LAUNCH_CONFIG = {"browser": {"launchOptions": {"ignoreDefaultArgs": ["--enable-automation", "--use-mock-keychain"]}}}
STARTUP_TIMEOUT_SECONDS = 60
TOOL_TIMEOUT_SECONDS = 120
# The CAPTCHA wait answers after HUMAN_CHECK_SECONDS (600); live gave its server 30 s more.
HUMAN_CHECK_TOOL_TIMEOUT_SECONDS = 630
GUARD_AUDIT = "guard.jsonl"
OUTPUT_FOLDER = "browser"   # @playwright/mcp's --output-dir: snapshots, screenshots, find results
UPLOAD_FOLDER = "uploads"   # the staged résumé and cover letters: read-only, the guard's one MCP root
# Tools whose argument is code; the webmail read (setup's email check) neither lists nor approves them.
CODE_TOOLS = ("browser_evaluate", "browser_run_code_unsafe")
# The operations that are never offered the code tools: the webmail read, and the support email (owner,
# 2026-10-25: a body written into the page's DOM by code may lose its line breaks or never reach the draft).
CODE_FREE_OPERATIONS = ("webmail", "support_email")


class RuntimeMissing(Exception):
    """The bundled Node or @playwright/mcp is not where the configuration says."""


def frozen():
    """True in a frozen build: PyInstaller-style `sys.frozen`, or Nuitka's `__compiled__`."""
    import scheduler_loop
    return scheduler_loop.frozen()


def settings(config):
    value = (config or {}).get("browser_driver")
    return value if isinstance(value, dict) else {}


def runtime_dir(config=None):
    configured = settings(config).get("runtime_dir") or os.environ.get(RUNTIME_VARIABLE)
    if configured:
        return Path(configured).expanduser()
    import scheduler_loop
    base = Path(scheduler_loop.program()).resolve().parent if frozen() else ROOT
    return base / RUNTIME_FOLDER


def node_path(config=None):
    configured = settings(config).get("node")
    if configured:
        return Path(configured).expanduser()
    folder = runtime_dir(config) / "node"
    return folder / "node.exe" if os.name == "nt" else folder / "bin" / "node"


def mcp_cli(config=None):
    configured = settings(config).get("mcp_cli")
    return Path(configured).expanduser() if configured else runtime_dir(config) / MCP_CLI


def missing(config=None):
    """[] when Node and @playwright/mcp are both in place, else the paths that are not."""
    return [str(path) for path in (node_path(config), mcp_cli(config)) if not path.is_file()]


def verify(config=None, run=None):
    """The build's check of a bundled runtime (E9.T1 hook): [] when Node is NODE_VERSION and
    @playwright/mcp is PLAYWRIGHT_MCP_VERSION as browser-runtime/package-lock.json pins it, else the
    problems. Reads the files and runs `node --version`; never fetches anything."""
    import subprocess
    problems = ["missing: " + path for path in missing(config)]
    if problems:
        return problems
    try:
        version = json.loads((mcp_cli(config).parent / "package.json").read_text()).get("version")
    except (OSError, ValueError):
        version = None
    if version != PLAYWRIGHT_MCP_VERSION:
        problems.append("@playwright/mcp is %s, not %s" % (version, PLAYWRIGHT_MCP_VERSION))
    try:
        node = (run or subprocess.run)([str(node_path(config)), "--version"], capture_output=True, text=True,
                                       timeout=30, **portable.no_window()).stdout.strip()
    except (OSError, ValueError) as error:
        node = "unrunnable (%s)" % type(error).__name__
    if node != "v" + NODE_VERSION:
        problems.append("node is %s, not v%s" % (node, NODE_VERSION))
    return problems


def require(config=None):
    absent = missing(config)
    if absent:
        raise RuntimeMissing("Sidedoor's browser runtime is not installed: " + ", ".join(absent))


def profile(config=None):
    """Sidedoor's own Chrome profile (setup step 8), never the user's everyday Chrome profile."""
    configured = settings(config).get("profile")
    if configured:
        return Path(configured).expanduser()
    import setup_steps
    return setup_steps.BROWSER_PROFILE


def sidedoor_command():
    """How this program starts itself with a subcommand: never a .py path when frozen."""
    if frozen():
        import scheduler_loop
        return [scheduler_loop.program()]
    return [sys.executable, *portable.UTF8_FLAGS, str(ENTRY)]


def upload_folder(directory):
    return Path(directory) / UPLOAD_FOLDER


def stage(source, directory, name=None):
    """Copy a file the session must upload into the session's upload folder and return the copy's
    absolute path. @playwright/mcp reads files only under its output folder and its client's
    workspace root (FINDINGS §4f fix 8e0a20f); the guard announces this folder as that root
    (`--uploads`), and `seal` makes it read-only, so a screenshot, snapshot or find `filename` can
    never overwrite a staged file (review fix 4). The arm guard checks each copy's sha256 against
    its original (application_browser.ArmBinding) and the original résumé against the policy.
    `name` gives the copy a different file name (the résumé's human name; the original keeps its own)."""
    folder = upload_folder(directory)
    folder.mkdir(parents=True, exist_ok=True)
    source = Path(source)
    target = folder / (Path(name).name if name else source.name)
    if target.resolve() != source.resolve():
        shutil.copyfile(source, target)
    return str(target.resolve())


def seal(directory):
    """Make the upload folder and its files read-only for the session (files 0400, folder 0500)."""
    folder = upload_folder(directory)
    if not folder.is_dir():
        return None
    for path in folder.iterdir():
        os.chmod(path, 0o400)
    os.chmod(folder, 0o500)
    return folder


def unstage(directory):
    """Delete the session's staged copies when it ends (review fix 10). Never raises."""
    folder = upload_folder(directory)
    try:
        if folder.is_dir() and not folder.is_symlink():
            os.chmod(folder, 0o700)
            shutil.rmtree(folder)
            return True
    except OSError:
        pass
    return False


def file_sha256(path):
    import hashlib
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def lock_holder(path):
    """The pid in a Chrome profile's SingletonLock (`<host>-<pid>`), or None."""
    try:
        return int(os.readlink(os.path.join(str(path), "SingletonLock")).rsplit("-", 1)[1])
    except (OSError, ValueError, IndexError):
        return None


def lock_owner(path):
    """The (host, pid) Chrome wrote into a profile's SingletonLock (`<host>-<pid>`), or None."""
    try:
        host, pid = os.readlink(os.path.join(str(path), "SingletonLock")).rsplit("-", 1)
        return host, int(pid)
    except (OSError, ValueError):
        return None


def stale_lock(path):
    """True when the profile's SingletonLock was written on this machine by a process that is gone
    (a Chrome that died): Chrome itself then removes the Singleton* links and starts. Windows writes
    none (portable.profile_busy). A lock naming another host (macOS renames the host with the network)
    is still judged by `holds_profile`: in use only while a live Chrome on this profile holds that pid."""
    import socket
    owner = lock_owner(path)
    return (owner is not None and not portable.WINDOWS and owner[0] == socket.gethostname()
            and not portable.pid_alive(owner[1]))


def clear_stale_lock(path):
    """Remove the Singleton* links when stale_lock(path). True when it did."""
    if stale_lock(path):
        _unlink_singletons(path)
        return True
    return False


def holds_profile(pid, path, run=None):
    """True only when process `pid` is a Chrome started on THIS profile: its command line carries
    `--user-data-dir=<path>` (Playwright and Sidedoor's own window both pass it so). A stale lock
    whose number now belongs to another program, and the user's everyday Chrome, are never it
    (review fix 5)."""
    import subprocess
    if pid is None or portable.WINDOWS:     # Windows: portable.profile_busy (Chrome's lockfile)
        return False
    try:
        output = (run or subprocess.run)(["ps", "-ww", "-p", str(pid), "-o", "args="], capture_output=True,
                                         text=True, timeout=10).stdout
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    wanted = {str(Path(path)), str(Path(path).resolve())}
    return any(("--user-data-dir=" + value) in output for value in wanted)


def _unlink_singletons(path):
    import setup_steps
    for name in setup_steps.SINGLETONS:
        try:
            os.unlink(Path(path) / name)
        except OSError:
            pass


def prepare_profile(path, run=None):
    """Refuse when a Chrome on Sidedoor's profile is running; clear a lock whose holder is gone or
    is not a Chrome on this profile (the spike's preflight). Raises BrowserProfileBusy."""
    import setup_steps
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    clear_stale_lock(path)   # a dead Chrome's lock on this machine: cleared, as Chrome does
    busy = [name for name in setup_steps.SINGLETONS if os.path.lexists(path / name)]
    if portable.profile_busy(path) or busy and holds_profile(lock_holder(path), path, run):
        raise BrowserProfileBusy("BROWSER_PROFILE_IN_USE")
    if busy:
        _unlink_singletons(path)


def profile_in_use(config=None, run=None):
    """True when a Chrome holds Sidedoor's profile now (checked before a session claims anything)."""
    if settings(config).get("isolated"):
        return False
    path = profile(config)
    clear_stale_lock(path)   # a dead Chrome's lock on this machine never reads as "window open"
    return portable.profile_busy(path) or holds_profile(lock_holder(path), path, run)


def session_holds_lock(root=None):
    """True when a Sidedoor browser session holds state/browser.lock now (run_locks.browser_session).
    Probed with one non-blocking take, released at once: a session that tries in that instant waits
    (applications, 20 min) or defers to its next cycle (the contact finder). False when unreadable."""
    import run_locks
    try:
        with run_locks.held(root, run_locks.BROWSER, wait_seconds=0) as acquired:
            return not acquired
    except (OSError, RuntimeError):
        return False


def _devtools_chrome(line, path):
    wanted = {str(Path(path)), str(Path(path).resolve())}
    return (any(("--user-data-dir=" + value) in line for value in wanted)
            and ("--remote-debugging-port" in line or "--remote-debugging-pipe" in line))


def _guard_running(listing):
    return any("browser-guard" in line and "--operation" in line for line in listing.splitlines())


def close_orphaned_session_chrome(path, root=None, run=None, kill=None, wait_seconds=5.0, poll_seconds=0.1):
    """Close a Chrome that a Sidedoor session started on profile `path` and that outlived it (the
    dashboard stopped while a session ran: the guard's `open -g -n` Chrome's parent is launchd, so it
    kept running and held the profile, and the owner was told to close a window he never opened).
    True when it closed one. Only when ALL hold: the profile's SingletonLock pid is a Chrome on this
    profile (`holds_profile`) whose command line carries a DevTools switch (--remote-debugging-port or
    --remote-debugging-pipe: Sidedoor's session launches only; setup step 8's sign-in window and a
    Chrome the user opened carry none); no other Sidedoor session holds state/browser.lock (this
    thread's own hold, a session checking before it starts Chrome, does not count); and no
    `browser-guard --operation` process runs. SIGTERM, up to `wait_seconds` for it to exit, then
    SIGKILL; then the stale lock is cleared. Logged as BROWSER_ORPHAN_CLOSED (pid only). Windows (no
    command-line check there, as `holds_profile`) and any failure: False. Never raises."""
    import signal
    import subprocess
    import time
    if portable.WINDOWS:
        return False
    try:
        pid = lock_holder(path)
        if pid is None or not holds_profile(pid, path, run):
            return False
        line = (run or subprocess.run)(["ps", "-ww", "-p", str(pid), "-o", "args="], capture_output=True,
                                       text=True, timeout=10).stdout
        if not _devtools_chrome(line, path):
            return False
        import run_locks
        if not run_locks.browser_held_here(root) and session_holds_lock(root):
            return False
        listing = (run or subprocess.run)(["ps", "-ww", "-axo", "args="], capture_output=True, text=True,
                                          timeout=10).stdout
        if _guard_running(listing):
            return False
        kill = kill or os.kill
        try:
            kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + wait_seconds
        while holds_profile(pid, path, run):
            if time.monotonic() >= deadline:
                try:
                    kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                break
            time.sleep(poll_seconds)
        clear_stale_lock(path)
        LOG.info("BROWSER_ORPHAN_CLOSED pid=%d", pid)
        return True
    except Exception as error:   # never stops the caller: the ordinary PROFILE_IN_USE path decides
        LOG.warning("BROWSER_ORPHAN_CLOSE_FAILED (%s)", type(error).__name__)
        return False


def close_orphan(config=None, root=None, run=None, kill=None):
    """`close_orphaned_session_chrome` on the configured profile (none for an isolated session)."""
    if settings(config).get("isolated"):
        return False
    return close_orphaned_session_chrome(profile(config), root, run, kill)


def guard_owns_profile(path, run=None):
    """True when the Chrome holding profile `path` was started for a Sidedoor session: its command line
    carries a DevTools switch (the guard's `open -g` launch: --remote-debugging-port; Playwright's
    fallback launch: --remote-debugging-pipe) and a `browser-guard` process for a session is running.
    Setup step 8's sign-in window and a Chrome the user opened on the profile carry neither."""
    import subprocess
    if portable.WINDOWS:
        return False
    try:
        listing = (run or subprocess.run)(["ps", "-ww", "-axo", "args="], capture_output=True, text=True,
                                          timeout=10).stdout
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    return any(_devtools_chrome(line, path) for line in listing.splitlines()) and _guard_running(listing)


def display_state(config=None, root=None, run=None, kill=None):
    """What the dashboard and doctor say about Sidedoor's browser (FG1, problem 2): None (fine),
    "RUNTIME_MISSING", "SESSION_BUSY" (a Sidedoor session owns the Chrome on the profile: never "close
    the window") or "PROFILE_IN_USE" (no Sidedoor session holds the browser lock and no running guard
    started it, yet a live Chrome holds the profile). Sessions themselves keep `unavailable`: they check
    inside the lock, before starting Chrome, where any Chrome on the profile is a real conflict."""
    if missing(config):
        return "RUNTIME_MISSING"
    close_orphan(config, root, run, kill)   # Sidedoor's own leftover Chrome never reads as "close the window"
    if not profile_in_use(config, run):
        return None
    if session_holds_lock(root) or guard_owns_profile(profile(config), run):
        return "SESSION_BUSY"
    return "PROFILE_IN_USE"


BUSY_WORDS = "Sidedoor's browser is busy with an application."


def unavailable(config=None, run=None, root=None, kill=None):
    """Why a browser session cannot start now, as a capacity-wait code suffix, or None (review fix 2):
    "RUNTIME_MISSING" or "PROFILE_IN_USE". Checked before any claim, request or reservation."""
    if missing(config):
        return "RUNTIME_MISSING"
    close_orphan(config, root, run, kill)   # Sidedoor's own leftover Chrome: closed, the session proceeds
    if profile_in_use(config, run):
        return "PROFILE_IN_USE"
    return None


class BrowserProfileBusy(Exception):
    pass


# Why browser sessions could not start, for Overview and Failures (confirming review fix 1): one row
# per code while it lasts, cleared by the next check that finds the browser available. Writing it spends
# no try and touches no job, request, reservation or finder run.
UNAVAILABLE_TABLE = """CREATE TABLE IF NOT EXISTS browser_unavailable (
    code TEXT PRIMARY KEY, source TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL)"""
UNAVAILABLE_WORDS = {
    "RUNTIME_MISSING": "Sidedoor's browser component is missing, so applications and the contact finder can't run.",
    "PROFILE_IN_USE": "Sidedoor's browser can't start because the Sidedoor Chrome window is open."}
UNAVAILABLE_TODO = {
    "RUNTIME_MISSING": "Reinstall Sidedoor. It tries again on its own once the browser component is back.",
    "PROFILE_IN_USE": "Close the Sidedoor Chrome window. Sidedoor tries again on its own."}


def _utc_now():
    import datetime as dt
    return dt.datetime.now(dt.timezone.utc).isoformat()


def note_unavailable(database, code, source, now=None):
    """Record (or keep) that `code` stopped a browser session from `source`. Never raises."""
    if database is None:
        return
    import sqlite3
    now = now or _utc_now()
    try:
        with database:
            database.execute(UNAVAILABLE_TABLE)
            database.execute("""INSERT INTO browser_unavailable VALUES (?,?,?,?)
                ON CONFLICT(code) DO UPDATE SET last_seen=excluded.last_seen, source=excluded.source""",
                             (code, source, now, now))
    except sqlite3.Error:
        pass


def clear_unavailable(database):
    """The browser is available again: forget what stopped it. Writes only when something is recorded."""
    if database is None:
        return
    import sqlite3
    try:
        if database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='browser_unavailable'").fetchone() \
                and database.execute("SELECT 1 FROM browser_unavailable LIMIT 1").fetchone():
            with database:
                database.execute("DELETE FROM browser_unavailable")
    except sqlite3.Error:
        pass


def check_available(database, config, prefix, source, now=None):
    """`unavailable` with its record kept: the full wait code (prefix + reason) or None."""
    reason = unavailable(config)
    if reason:
        note_unavailable(database, prefix + reason, source, now)
        return prefix + reason
    clear_unavailable(database)
    return None


def end_session(config=None, wait_seconds=5.0, run=None):
    """Stop the Chrome a finished browser session left on Sidedoor's profile. Never raises.

    Playwright starts Chrome in a process group of its own, so the session's SIGKILL of the codex
    group (application_browser.execute, reap_live_workers) does not reach it: on a timed-out or
    killed session Chrome kept running and held the profile. A session that ends normally closes
    Chrome itself; this is for the other endings. Only a process whose command line carries
    `--user-data-dir=<Sidedoor's profile>` is stopped, never the user's own Chrome; the Singleton
    files are removed once it is gone."""
    if settings(config).get("isolated"):
        return False
    import signal
    import time
    path = profile(config)
    started = _STARTED.pop(str(Path(path)), None)
    if started is not None:
        portable.kill_tree(started)   # Windows: the Chrome Sidedoor started (launch_windows), by its own job
    pid = lock_holder(path)
    if not holds_profile(pid, path, run):
        # Windows: the session's Chrome ended with its worker's Job Object (portable.kill_tree);
        # wait for it to let go of the profile, and never kill by pid there (B1).
        if portable.wait_profile_free(path, wait_seconds):
            _unlink_singletons(path)
            return True
        return False
    for number in (signal.SIGTERM, getattr(signal, "SIGKILL", signal.SIGTERM)):
        try:
            os.kill(pid, number)
        except (ProcessLookupError, PermissionError, OSError):
            break
        deadline = time.monotonic() + wait_seconds
        gone = False
        while time.monotonic() < deadline:
            if not holds_profile(pid, path, run):   # exited (or a zombie that no longer carries its arguments)
                gone = True
                break
            time.sleep(0.1)
        if gone:
            break
    else:
        return False
    _unlink_singletons(path)
    return True


# --- Chrome in the background (owner, 2026-10-22: "is it possible to stop the sidedoor browser from
# just snatching control of my screen?", option a) -------------------------------------------------
# Playwright starts Chrome by exec'ing its binary (with --remote-debugging-pipe), and a macOS app
# started that way activates itself: every browser job pulled Chrome in front of what the user was
# doing. On macOS Sidedoor now starts Sidedoor's own Chrome itself through LaunchServices with
# `open -g -n` (-g: never activate; -n: a new instance even while the user's everyday Chrome runs),
# headed and at its normal size, behind the user's current app, with a local DevTools port, and
# @playwright/mcp attaches to it (--cdp-endpoint). The pipe cannot be used: `open` passes no file
# descriptors. The user watches by clicking Chrome's Dock icon. Setup step 8's sign-in window
# (setup_steps.BrowserSession) is the user's own request and still comes to the front.
# The switches are the ones Playwright passed (chromiumSwitches in @playwright/mcp 0.0.83's
# playwright-core) minus LAUNCH_CONFIG's ignoreDefaultArgs, so the page behaves as before; the
# backgrounding/throttling switches matter more now, since the window is behind others.
BACKGROUND_DISABLED_FEATURES = (
    "AvoidUnnecessaryBeforeUnloadCheckSync", "DestroyProfileOnBrowserClose", "DialMediaRouteProvider",
    "GlobalMediaControls", "HttpsUpgrades", "LensOverlay", "MediaRouter", "PaintHolding",
    "ThirdPartyStoragePartitioning", "BlockOriginHeaderModificationOnRedirect", "Translate", "AutoDeElevate",
    "OptimizationHints", "NetworkTimeServiceQuerying", "AimEnabled", "msForceBrowserSignIn",
    "msEdgeUpdateLaunchServicesPreferredVersion")
BACKGROUND_SWITCHES = (
    "--disable-field-trial-config", "--disable-background-networking", "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows", "--disable-back-forward-cache", "--disable-breakpad",
    "--disable-client-side-phishing-detection", "--disable-component-extensions-with-background-pages",
    "--disable-component-update", "--no-default-browser-check", "--disable-default-apps", "--disable-dev-shm-usage",
    "--disable-edgeupdater", "--disable-extensions", "--disable-features=" + ",".join(BACKGROUND_DISABLED_FEATURES),
    "--enable-features=CDPScreenshotNewSurface", "--allow-pre-commit-input", "--disable-hang-monitor",
    "--disable-ipc-flooding-protection", "--disable-popup-blocking", "--disable-prompt-on-repost",
    "--disable-renderer-backgrounding", "--disable-updater-scheduler", "--force-color-profile=srgb",
    "--metrics-recording-only", "--no-first-run", "--password-store=basic", "--no-service-autorun",
    "--export-tagged-pdf", "--disable-search-engine-choice-screen", "--unsafely-disable-devtools-self-xss-warnings",
    "--edge-skip-compat-layer-relaunch", "--disable-infobars", "--disable-sync")
DEVTOOLS_PORT_FILE = "DevToolsActivePort"
BACKGROUND_LAUNCH_SECONDS = 60
# Hidden window. FG1 = b (owner, 2026-10-22) put the session's window far off-screen; macOS clamps a
# window to keep a strip on a display (live, request-609_iyj7: -32000,-32000 became left=-1240, top=38,
# WINDOW_NOT_OFFSCREEN on every placement), so FG2 = a (owner, 2026-10-23) minimizes it instead: the
# guard sets every window to windowState "minimized" over CDP after launch and before each forwarded
# call (browser_guard.hide_code), and a focus keeper hands the front back to the user's app whenever
# this Chrome activates itself (browser_guard.FocusKeeper). OFFSCREEN_POSITION stays as the launch
# position and as the "normal" bounds the guard parks a window at before minimizing it: whenever Chrome
# un-minimizes a window on its own (a new tab or a tab select activates it) only the clamped strip
# shows until the next call minimizes it again, never a full window over the user's work. Pages still
# lay out at WINDOW_SIZE. The CAPTCHA wait un-minimizes it on-screen and in front, then minimizes it.
OFFSCREEN_POSITION = (-32000, -32000)
ONSCREEN_POSITION = (80, 60)
WINDOW_SIZE = (1280, 900)
WINDOW_SWITCHES = ("--window-position=%d,%d" % OFFSCREEN_POSITION, "--window-size=%d,%d" % WINDOW_SIZE)
OFFSCREEN_WORDS = "Sidedoor's browser runs minimized in the Dock; it appears when it needs you."
HEADLESS_WORDS = "Sidedoor's browser runs without a window; one appears only when a CAPTCHA needs you."
MIXED_WORDS = ("Sidedoor's browser runs without a window (some jobs run it minimized in the Dock); "
               "it appears when it needs you.")
# Headless (owner, 2026-10-24: "sometimes the browser still comes to the front and doesn't return me to my
# last screen", option A): on macOS each operation named in config["browser_driver"]["headless_operations"]
# (default: every one) runs Sidedoor's Chrome in Chrome's own headless mode (--headless=new) on the same
# profile, so it has no window and cannot come to the front; the minimized window and the focus keeper
# (FG2 = a) run only for the operations left out (the per-operation fallback) and for the CAPTCHA wait,
# which reopens the same profile headed for the owner's tick (browser_guard.Proxy.show_headed). The page
# sees the installed Chrome's own headed user agent (`headed_user_agent`, from its version: new headless
# would otherwise say "HeadlessChrome"), and navigator.webdriver stays false as in the headed launch
# (Chrome sets it under --headless; Blink's documented AutomationControlled switch turns it off).
HEADLESS_OPERATIONS = ("linkedin", "webmail", "inspect", "investigate", "submit", "verify", "reconcile")
# The one session whose window is for the user (owner, 2026-10-24: "Email support" on Failures and logs):
# the AI starts a support email in the user's webmail and leaves the draft open for the user to send.
# Its window is headed and on-screen (never minimized; kept behind the user's app until the draft is ready
# or the user must sign in), after a first phase that runs headless (HEADLESS_PHASE, owner 2026-10-25),
# and the session's end leaves it running (browser_guard.Proxy.stop_background_chrome,
# outreach_history_browser.execute); support_email brings it to the front and closes it once the user
# has closed the window.
SHOWN_OPERATIONS = ("support_email",)
# Owner, 2026-10-25 ("B + C"): support_email's first phase runs headless (the AI makes and saves the draft
# out of sight); run_session sets this config key for that phase only (support_email.run_session).
HEADLESS_PHASE = "_browser_headless_phase"
HEADLESS_SWITCH = "--headless=new"
AUTOMATION_SWITCH = "--disable-blink-features=AutomationControlled"
# Chrome's reduced user agent on macOS (the OS part is frozen at 10_15_7 and the minor versions at 0.0.0
# by Chrome itself); only the major version comes from the installed Chrome.
MAC_USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/%s.0.0.0 Safari/537.36")
# False in the test suite (tests/profile_home.py): a test's guard never starts a real Chrome.
BACKGROUND_ALLOWED = True


def background_launch(config=None, platform=None):
    """True when Sidedoor starts the session's Chrome itself, in the background: macOS, the real
    profile, headed (tests' isolated or headless sessions keep Playwright's own launch)."""
    driver = settings(config)
    return (BACKGROUND_ALLOWED and (platform or sys.platform) == "darwin" and not driver.get("isolated")
            and not driver.get("headless"))


def offscreen(config=None, platform=None):
    """True when session Chrome is kept out of the user's way: the same sessions as `background_launch`
    (macOS, the real profile), headless or minimized (doctor, the overview). Which of the two a session
    gets is `minimized` / `headless_user_agent`."""
    return background_launch(config, platform)


def headless_operations(config=None):
    """The operations that run headless (config["browser_driver"]["headless_operations"]; default all).
    A value that is not a list keeps the default."""
    value = settings(config).get("headless_operations")
    if not isinstance(value, list):
        return HEADLESS_OPERATIONS
    return tuple(item for item in value if isinstance(item, str))


def chrome_version(binary):
    """The installed Chrome's version ("141.0.7390.123") from its bundle's Info.plist, or None. Read
    from the file only: Chrome is never started for it."""
    import plistlib
    app = chrome_app(binary or "")
    if app is None:
        return None
    try:
        with open(app / "Contents" / "Info.plist", "rb") as stream:
            version = plistlib.load(stream).get("CFBundleShortVersionString")
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    return version if isinstance(version, str) and version.split(".")[0].isdigit() else None


def headed_user_agent(binary):
    """The user agent the installed Chrome sends headed (macOS), or None when its version is unknown."""
    version = chrome_version(binary)
    return MAC_USER_AGENT % version.split(".")[0] if version else None


def headless_user_agent(config, operation, platform=None, binary=None):
    """The user agent a headless session of `operation` presents, or None when the operation runs headed:
    not macOS's background launch, not in `headless_operations`, or the installed Chrome's version cannot
    be read (then it runs headed and minimized as before, never as "HeadlessChrome")."""
    if operation in SHOWN_OPERATIONS:
        if not (background_launch(config, platform) and (config or {}).get(HEADLESS_PHASE)):
            return None
    elif not background_launch(config, platform) or operation not in headless_operations(config):
        return None
    if binary is None:
        import setup_steps
        binary = setup_steps.chrome_path(platform)
    return headed_user_agent(binary)


def minimized(config=None, operation=None, platform=None):
    """True when the session's Chrome is headed and kept minimized with the focus keeper (FG2 = a): macOS's
    background launch for an operation that does not run headless (the per-operation fallback)."""
    return background_launch(config, platform) and headless_user_agent(config, operation, platform) is None


def headless_switches(user_agent, scale=None):
    """Chrome's switches for a headless session: new headless at the normal window size, presenting
    `user_agent`, with navigator.webdriver false as headed, and (given `scale`) the real display's
    device pixel ratio (--force-device-scale-factor: the CSS viewport stays WINDOW_SIZE wide)."""
    switches = (HEADLESS_SWITCH, "--window-size=%d,%d" % WINDOW_SIZE, "--user-agent=" + user_agent, AUTOMATION_SWITCH)
    return switches + (("--force-device-scale-factor=%s" % _number(scale),) if scale else ())


def _number(value):
    return str(int(value)) if float(value).is_integer() else str(value)


# A headless Chrome reports an 800x600 screen at ratio 1 (real-site check, 2026-10-24), against a 1280x900
# window: the session reports the user's own main display instead (`display`, read once from Quartz in
# points with its backing scale), never smaller than the window. Unreadable: a common MacBook display.
FALLBACK_DISPLAY = (1512, 982, 2)


def display(quartz=None):
    """(width, height, scale) of the main display: size in points (CGDisplayPixelsWide/High), scale =
    the current mode's pixel width over its point width. FALLBACK_DISPLAY when it cannot be read."""
    try:
        if quartz is None:
            import Quartz as quartz
        main = quartz.CGMainDisplayID()
        width, height = int(quartz.CGDisplayPixelsWide(main)), int(quartz.CGDisplayPixelsHigh(main))
        mode = quartz.CGDisplayCopyDisplayMode(main)
        scale = quartz.CGDisplayModeGetPixelWidth(mode) / quartz.CGDisplayModeGetWidth(mode)
        if width <= 0 or height <= 0 or not 1 <= scale <= 4:
            return FALLBACK_DISPLAY
        return width, height, round(scale * 4) / 4
    except Exception:
        return FALLBACK_DISPLAY


def screen_metrics(value=None):
    """The headless session's screen: {width, height, scale}, the display never smaller than the window."""
    width, height, scale = value or display()
    return {"width": max(width, WINDOW_SIZE[0]), "height": max(height, WINDOW_SIZE[1]), "scale": scale}


def window_words(config=None, binary=None):
    """What the overview and doctor say about where the browser is (macOS background launch only): what
    will actually run, so "without a window" only when the config lists the operation AND the installed
    Chrome's headed user agent can be derived (`headless_user_agent`; otherwise it runs minimized)."""
    if binary is None:
        import setup_steps
        binary = setup_steps.chrome_path()
    operations = set(headless_operations(config)) & set(HEADLESS_OPERATIONS)
    if not operations or headed_user_agent(binary) is None:
        return OFFSCREEN_WORDS
    return HEADLESS_WORDS if operations == set(HEADLESS_OPERATIONS) else MIXED_WORDS


def launch_config(config=None, operation=None):
    """@playwright/mcp's --config: LAUNCH_CONFIG, plus the off-screen window for its fallback launch. The
    fallback is headed and minimized for every operation (H2): the guard reaches it only when its own
    launch failed, and then runs the session by the --offscreen rules (browser_guard)."""
    value = json.loads(json.dumps(LAUNCH_CONFIG))
    if offscreen(config) and operation not in SHOWN_OPERATIONS:
        value["browser"]["launchOptions"]["args"] = list(WINDOW_SWITCHES)
    return value


# Chrome's GREASE brand (components/embedder_support user_agent_utils: GenerateBrandVersionList), seeded by
# the major version, so the brand list matches the installed headed Chrome's exactly.
GREASE_CHARACTERS = (" ", "(", ":", "-", ".", "/", ")", ";", "=", "?", "_")
GREASE_VERSIONS = ("8", "99", "24")
GREASE_ORDERS = ((0, 1, 2), (0, 2, 1), (1, 0, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0))


def brand_list(major, version):
    """[{brand, version}] for Chrome `major` (`version`: "141" for brands, the full one for the full list)."""
    seed = int(major)
    grease = {"brand": "Not" + GREASE_CHARACTERS[seed % 11] + "A" + GREASE_CHARACTERS[(seed + 1) % 11] + "Brand",
              "version": GREASE_VERSIONS[seed % 3] + ("" if version == major else ".0.0.0")}
    order = GREASE_ORDERS[seed % 6]
    brands = [None, None, None]
    brands[order[0]] = grease
    brands[order[1]] = {"brand": "Chromium", "version": version}
    brands[order[2]] = {"brand": "Google Chrome", "version": version}
    return brands


def client_hints(binary, mac_version=None, machine=None):
    """CDP userAgentMetadata for the installed Chrome headed on this Mac (H3), or None when its version is
    unknown: brands, fullVersionList, platform, its version, architecture, all from the installed Chrome
    and this machine, never a fixed version."""
    import platform as host
    version = chrome_version(binary)
    if not version:
        return None
    major = version.split(".")[0]
    machine = machine or host.machine()
    return {"brands": brand_list(major, major), "fullVersionList": brand_list(major, version),
            "fullVersion": version, "platform": "macOS",
            "platformVersion": mac_version if mac_version is not None else (host.mac_ver()[0] or ""),
            "architecture": "arm" if machine.startswith("arm") else "x86", "bitness": "64", "model": "",
            "mobile": False, "wow64": False}


def chrome_app(binary):
    """The .app bundle a Chrome binary path lives in (`.../Google Chrome.app/Contents/MacOS/...`), or None."""
    path = Path(binary)
    for parent in path.parents:
        if parent.suffix == ".app":
            return parent
    return None


def background_command(app, path, port=0, onscreen=False, url=None):
    """`open -g -n -a <Chrome.app> --args ...`: Chrome on `path`, never activated, DevTools on a free port
    (or on `port`: the CAPTCHA wait reopens a headless session's profile headed on the port
    @playwright/mcp is attached to). `onscreen` (SHOWN_OPERATIONS): the window opens at
    ONSCREEN_POSITION instead of the off-screen one. `url` (support_email's saved draft) is the first page
    instead of about:blank."""
    position = ("--window-position=%d,%d" % ONSCREEN_POSITION, WINDOW_SWITCHES[1]) if onscreen else WINDOW_SWITCHES
    return ["/usr/bin/open", "-g", "-n", "-a", str(app), "--args", "--user-data-dir=" + str(path),
            # Live parity (CK3, accepted): Playwright's own launch also opens a local DevTools port.
            "--remote-debugging-port=%d" % port, *BACKGROUND_SWITCHES, *position, url or "about:blank"]


def headless_command(binary, path, user_agent, scale=None):
    """Chrome's own binary, headless, on `path` with DevTools on a free port. Started directly (no
    LaunchServices): a windowless Chrome has nothing to activate."""
    return [str(binary), "--user-data-dir=" + str(path), "--remote-debugging-port=0", *BACKGROUND_SWITCHES,
            *headless_switches(user_agent, scale), "about:blank"]


def _fresh_port_file(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    port_file = path / DEVTOOLS_PORT_FILE
    try:
        port_file.unlink()   # a stale file names a port that is gone
    except OSError:
        pass
    return port_file


def launch_headless(binary, path, user_agent, popen=None, timeout=BACKGROUND_LAUNCH_SECONDS, sleep=None,
                    clock=None, stop=None, scale=None):
    """Start Chrome headless on profile `path` (`headless_command`); return its DevTools endpoint, as
    `launch_background` does (same port file, same stop of this profile's Chrome on a timeout)."""
    import subprocess
    port_file = _fresh_port_file(path)
    (popen or subprocess.Popen)(headless_command(binary, path, user_agent, scale), stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return _await_port(port_file, path, timeout, sleep, clock, stop)


# Windows (owner, 2026-10-25): a session's Chrome that Playwright launches is a child of the Codex worker,
# inside the worker's Job Object (portable.contain), so the session's end (portable.kill_tree) ends it. For
# SHOWN_OPERATIONS (support_email's draft window, which must outlive the session) Sidedoor starts Chrome
# itself instead, outside the worker's job, the way macOS's background launch does, and @playwright/mcp
# attaches to it over CDP (CDP_ENDPOINT, mcp_command). That Chrome goes in a Job Object of its own, held by
# this process, so end_session can stop it (kill_tree) and it never outlives Sidedoor.
CDP_ENDPOINT = "_browser_cdp_endpoint"
_STARTED = {}   # profile path -> pid of the Chrome launch_windows started (its job is in portable._jobs)


def windows_launch(config=None, operation=None):
    """True when Sidedoor starts the session's Chrome itself on Windows: a shown operation on the real
    profile, headed (tests' isolated or headless sessions keep Playwright's own launch)."""
    driver = settings(config)
    return (BACKGROUND_ALLOWED and portable.WINDOWS and operation in SHOWN_OPERATIONS
            and not driver.get("isolated") and not driver.get("headless"))


def windows_command(binary, path, url=None):
    """chrome.exe on `path`, on-screen at the normal size, DevTools on a free port (Playwright's switches,
    as background_command)."""
    return [str(binary), "--user-data-dir=" + str(path), "--remote-debugging-port=0", *BACKGROUND_SWITCHES,
            "--window-position=%d,%d" % ONSCREEN_POSITION, WINDOW_SWITCHES[1], url or "about:blank"]


def launch_windows(binary, path, popen=None, timeout=BACKGROUND_LAUNCH_SECONDS, sleep=None, clock=None, url=None):
    """Windows: start Chrome on profile `path` from this process, in a Job Object of its own (never the
    worker's); return its DevTools endpoint (DevToolsActivePort, as launch_background). Raises OSError when
    it cannot start or writes no port within `timeout` (that Chrome is then stopped)."""
    import subprocess
    path = Path(path)
    port_file = _fresh_port_file(path)
    chrome = (popen or subprocess.Popen)(windows_command(binary, path, url), stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         **portable.worker_spawn_options(core=False))
    portable.contain(chrome)
    previous = _STARTED.pop(str(path), None)
    if previous is not None:
        portable.kill_tree(previous)   # an earlier one's job (that Chrome is gone: prepare_profile checked)
    _STARTED[str(path)] = chrome.pid
    return _await_port(port_file, path, timeout, sleep, clock)


def chrome_running(path):
    """True while a Chrome holds profile `path`: Windows, Chrome's lockfile (portable.profile_busy);
    elsewhere a SingletonLock pid whose command line carries this profile."""
    if portable.WINDOWS:
        return portable.profile_busy(path)
    pid = lock_holder(path)
    return pid is not None and holds_profile(pid, path)


def launch_background(app, path, run=None, timeout=BACKGROUND_LAUNCH_SECONDS, sleep=None, clock=None, stop=None,
                      port=0, onscreen=False, url=None):
    """Start Chrome on profile `path` without activating it; return its DevTools endpoint
    (`http://127.0.0.1:<port>`, from the DevToolsActivePort file Chrome writes into the profile).
    Raises OSError when `open` fails or no port appears within `timeout`; before that last raise the
    Chrome `open` started on this profile is stopped (`end_session`: only a Chrome whose command line
    carries --user-data-dir=<path>), so it cannot clash with Playwright's fallback launch (CK2)."""
    import subprocess
    path = Path(path)
    port_file = _fresh_port_file(path)
    done = (run or subprocess.run)(background_command(app, path, port, onscreen, url), stdin=subprocess.DEVNULL,
                                   capture_output=True, text=True, timeout=timeout)
    if done.returncode != 0:
        raise OSError("open -g failed: " + (done.stderr or "").strip()[:200])
    return _await_port(port_file, path, timeout, sleep, clock, stop)


def _await_port(port_file, path, timeout, sleep=None, clock=None, stop=None):
    import time
    clock, sleep = clock or time.monotonic, sleep or time.sleep
    deadline = clock() + timeout
    while clock() < deadline:
        try:
            # Chrome writes "<port>\n<browser path>"; only a file with both lines is complete (CK4).
            lines = port_file.read_text().split("\n")
            port = int(lines[0].strip())
            if len(lines) > 1 and lines[1].strip() and 0 < port < 65536:
                return "http://127.0.0.1:%d" % port
        except (OSError, ValueError):
            pass
        sleep(0.2)
    (stop or end_session)({"browser_driver": {"profile": str(path)}})
    raise OSError("Chrome wrote no DevTools port within %d s" % timeout)


def mcp_command(config, directory, operation=None):
    """The @playwright/mcp command line (bundled Node, absolute paths: no npx shim). In background
    mode it carries no profile: the guard starts Chrome and adds `--cdp-endpoint` (browser_guard)."""
    directory = Path(directory)
    output = directory / OUTPUT_FOLDER
    output.mkdir(parents=True, exist_ok=True)
    launch = directory / "browser-config.json"
    launch.write_text(json.dumps(launch_config(config, operation), indent=1) + "\n")
    command = [str(node_path(config)), str(mcp_cli(config)), "--browser", settings(config).get("browser", "chrome")]
    if settings(config).get("isolated"):
        command.append("--isolated")  # tests only: an in-memory profile
    elif (config or {}).get(CDP_ENDPOINT):
        command.extend(["--cdp-endpoint", str(config[CDP_ENDPOINT])])   # Windows: the Chrome Sidedoor started
    elif not background_launch(config):
        command.extend(["--user-data-dir", str(profile(config))])
    if settings(config).get("headless"):
        command.append("--headless")  # tests only: the product runs headed
    return command + ["--no-webmcp", "--output-dir", str(output), "--config", str(launch)]


def guard_command(config, directory, operation, claim=None, human_check=None):
    """`sidedoor browser-guard ... -- <@playwright/mcp>`: the argv Codex starts as the `browser` server."""
    command = sidedoor_command() + ["browser-guard", "--operation", operation,
                                    "--audit", str(Path(directory) / GUARD_AUDIT)]
    if upload_folder(directory).is_dir():
        command.extend(["--uploads", str(upload_folder(directory).resolve())])
    if claim:
        command.extend(["--claim", str(claim)])
    if human_check:
        command.extend(["--human-check", str(human_check)])
    if background_launch(config):
        command.extend(["--background-chrome", str(profile(config))])
    user_agent = headless_user_agent(config, operation)
    if user_agent:
        command.extend(["--headless-user-agent", user_agent])   # headless (owner, 2026-10-24, option A)
        import setup_steps
        hints = client_hints(setup_steps.chrome_path())
        if hints:
            command.extend(["--headless-client-hints", json.dumps(hints, sort_keys=True, separators=(",", ":"))])
        screen = screen_metrics()
        command.extend(["--headless-screen", "%dx%d@%s" % (screen["width"], screen["height"], _number(screen["scale"]))])
    elif offscreen(config) and operation not in SHOWN_OPERATIONS:
        command.append("--offscreen")
    return command + ["--"] + mcp_command(config, directory, operation)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


# The user-session variables Chrome needs to find the user's own login keychain (macOS: the "Chrome Safe
# Storage" key the profile's cookies are encrypted with) or profile folders (Windows). Codex runs with a
# small allow-list and starts MCP servers with a filtered environment, so without these Chrome cannot find
# the keychain and asks on every launch. Values come from this (the parent) process; never a secret.
SESSION_VARIABLES = ("HOME", "USER", "LOGNAME", "TMPDIR", "SHELL", "__CF_USER_TEXT_ENCODING",
                     "USERPROFILE", "USERNAME", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP",
                     "HOMEDRIVE", "HOMEPATH", "SYSTEMROOT")


def session_environment(environ=None):
    """The parent's values of SESSION_VARIABLES that are set (an explicit allow-list: no keys pass)."""
    environ = os.environ if environ is None else environ
    return {name: environ[name] for name in SESSION_VARIABLES if environ.get(name)}


def codex_options(config, directory, operation, claim=None, human_check=None):
    """The `-c` settings that give a codex session the guarded browser as its one browser server."""
    import browser_guard
    import user_profile
    argv = guard_command(config, directory, operation, claim, human_check)
    tools = [tool for tool in browser_guard.TOOLS if operation not in CODE_FREE_OPERATIONS or tool not in CODE_TOOLS]
    if claim:
        tools.append(browser_guard.ARM_TOOL)
    if human_check:
        tools.append(browser_guard.HUMAN_CHECK_TOOL)
    options = ["mcp_servers." + SERVER + ".command=" + encoded(argv[0]),
               "mcp_servers." + SERVER + ".args=" + encoded(argv[1:]),
               "mcp_servers." + SERVER + ".enabled=true",
               "mcp_servers." + SERVER + ".startup_timeout_sec=" + str(STARTUP_TIMEOUT_SECONDS),
               # Codex has one timeout per server. It is long enough for the CAPTCHA wait when that tool
               # is there; every forwarded browser tool is answered within browser_guard.CALL_TIMEOUT
               # by the guard itself (review fix 6), so only the wait can take longer.
               "mcp_servers." + SERVER + ".tool_timeout_sec="
               + str(HUMAN_CHECK_TOOL_TIMEOUT_SECONDS if human_check else TOOL_TIMEOUT_SECONDS),
               # The arm guard loads the user's policy (desktop_adapter.submission_gate); codex starts
               # MCP servers with a filtered environment, so the profile directory is passed explicitly.
               # The user's session variables go with it, so Chrome finds the real login keychain.
               "mcp_servers." + SERVER + ".env={" + ",".join(
                   encoded(name) + "=" + encoded(value) for name, value in sorted(
                       dict(session_environment(), **{user_profile.HOME_VARIABLE: str(user_profile.home())}).items()))
               + "}"]
    options.extend("mcp_servers." + SERVER + ".tools." + tool + '.approval_mode="approve"' for tool in tools)
    return options
