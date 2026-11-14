"""Where Sidedoor's browser driver lives and how Codex is pointed at it (E7.T1, option A).

Owner, 2026-10-22: bundled Node plus @playwright/mcp behind `sidedoor browser-guard`. Every browser job
(application inspect/investigate/submit/verify/reconcile, the LinkedIn contact finder, the setup browser step's
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
# The long tool-call allowance, for a session given a hold (`human_check`, guard_command / codex_options,
# and the Claude and Antigravity commands): a held call may answer after up to 600 s, plus 30 s. Ordinary
# sessions are not given one (E16.T1) and keep TOOL_TIMEOUT_SECONDS; today only support_email passes it.
HUMAN_CHECK_TOOL_TIMEOUT_SECONDS = 630
START_URL_CONFIG_KEY = "_application_start_url"   # an attended submit's start page (application_browser.consume)
GUARD_AUDIT = "guard.jsonl"
# Written by the guard beside its audit when it stops its own session (browser_guard.Proxy.stop_session:
# the page could not be told the installed Chrome's own user agent, client hints and screen). The session's
# launcher ends the session as a failed one on it (application_browser: SETUP_STOPPED_CODE, with its own
# sentence on the Applications page; outreach_history_browser).
GUARD_STOPPED = "guard-stopped.json"
SETUP_STOPPED_CODE = "APPLICATION_BROWSER_SETUP_STOPPED"
OUTPUT_FOLDER = "browser"   # @playwright/mcp's --output-dir: snapshots, screenshots, find results
UPLOAD_FOLDER = "uploads"   # the staged résumé and cover letters: read-only, the guard's one MCP root
# Tools whose argument is code; the webmail read (setup's email check) neither lists nor approves them.
CODE_TOOLS = ("browser_evaluate",)   # browser_run_code_unsafe is no session's (browser_guard.REMOVED_TOOL)
# The operations that are never offered the code tools: the webmail read, and the support email (owner,
# 2026-10-25: a body written into the page's DOM by code may lose its line breaks or never reach the draft).
CODE_FREE_OPERATIONS = ("webmail", "support_email")


def guard_stopped(directory):
    """The reason the guard stopped the session run in `directory` (GUARD_STOPPED), or None."""
    try:
        value = json.loads((Path(directory) / GUARD_STOPPED).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    reason = value.get("reason") if isinstance(value, dict) else None
    return reason if isinstance(reason, str) and reason else "STOPPED"


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
    "RUNTIME_MISSING": "Sidedoor's browser component is missing.",
    "PROFILE_IN_USE": "The Sidedoor Chrome window is open."}
UNAVAILABLE_TODO = {
    "RUNTIME_MISSING": "Reinstall Sidedoor.",
    "PROFILE_IN_USE": "Close the Sidedoor Chrome window."}


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
# lay out at WINDOW_SIZE. Nothing un-minimizes it (E16.T1).
OFFSCREEN_POSITION = (-32000, -32000)
ONSCREEN_POSITION = (80, 60)
WINDOW_SIZE = (1280, 900)
WINDOW_SWITCHES = ("--window-position=%d,%d" % OFFSCREEN_POSITION, "--window-size=%d,%d" % WINDOW_SIZE)
# E16.T1: no window appears for a human check; the job waits under Needs you (access_outcomes). Said
# only of a check met before the application is sent. One met while confirming a sent application has
# a card too (confirm_read), but a check that fires as Submit is pressed has none until a confirming
# read meets it, so the sentence does not claim every state.
CHECK_WORDS = "When a site asks for a human check before an application is sent, the job waits for you under Needs you."
OFFSCREEN_WORDS = "Sidedoor's browser runs minimized in the Dock. " + CHECK_WORDS
HEADLESS_WORDS = "Sidedoor's browser runs without a window. " + CHECK_WORDS
MIXED_WORDS = "Sidedoor's browser runs without a window (some jobs run it minimized in the Dock). " + CHECK_WORDS
# Headless (owner, 2026-10-24: "sometimes the browser still comes to the front and doesn't return me to my
# last screen", option A): on macOS each operation named in config["browser_driver"]["headless_operations"]
# (default: every one) runs Sidedoor's Chrome in Chrome's own headless mode (--headless=new) on the same
# profile, so it has no window and cannot come to the front; the minimized window and the focus keeper
# (FG2 = a) run only for the operations left out (the per-operation fallback). No session is reopened in
# a window (E16.T1: a human check ends the session; the reopen for the owner's tick is retired). The page
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
# Chrome's reduced user agent on Windows (Chrome itself freezes the OS part at "Windows NT 10.0; Win64; x64"
# on every Windows it runs on, and the minor versions at 0.0.0); only the major version is the installed one.
WINDOWS_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/%s.0.0.0 Safari/537.36")
# False in the test suite (tests/profile_home.py): a test's guard never starts a real Chrome.
BACKGROUND_ALLOWED = True


def windows_host(platform=None):
    return (platform or sys.platform).startswith("win")


def hidden_launch(config=None, platform=None):
    """Windows (owner, 2026-11-12, option A): True when Sidedoor may start the session's Chrome itself,
    without a window, as macOS does: the real profile, headed (tests' isolated or headless sessions keep
    Playwright's own launch). Windows has no minimized mode: a session is hidden (`headless_user_agent`)
    or Playwright starts Chrome in a window as before."""
    driver = settings(config)
    return (BACKGROUND_ALLOWED and windows_host(platform) and not driver.get("isolated")
            and not driver.get("headless"))


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


def chrome_version(binary, platform=None):
    """The installed Chrome's version ("141.0.7390.123") from its bundle's Info.plist (Windows: from
    chrome.exe's own version resource), or None. Read from the file only: Chrome is never started for it."""
    if windows_host(platform):
        version = portable.file_version(binary) if binary else None
        return version if isinstance(version, str) and version.split(".")[0].isdigit() else None
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


def headed_user_agent(binary, platform=None):
    """The user agent the installed Chrome sends headed (this system's), or None when its version is unknown."""
    version = chrome_version(binary, platform)
    template = WINDOWS_USER_AGENT if windows_host(platform) else MAC_USER_AGENT
    return template % version.split(".")[0] if version else None


def headless_user_agent(config, operation, platform=None, binary=None):
    """The user agent a headless session of `operation` presents, or None when the operation runs headed:
    not macOS's background launch, not in `headless_operations`, or the installed Chrome's version cannot
    be read (then it runs headed and minimized as before, never as "HeadlessChrome")."""
    if hidden_launch(config, platform):
        return (windows_told(config, operation, platform, binary) or {}).get("user_agent")
    if operation in SHOWN_OPERATIONS:
        if not (background_launch(config, platform) and (config or {}).get(HEADLESS_PHASE)):
            return None
    elif not background_launch(config, platform) or operation not in headless_operations(config):
        return None
    if binary is None:
        import setup_steps
        binary = setup_steps.chrome_path(platform)
    return headed_user_agent(binary)


def windows_told(config, operation, platform=None, binary=None):
    """Windows: what a hidden session of `operation` tells a page, read from this machine once:
    {user_agent, client_hints, screen}; or None when the session is not hidden: not Windows' hidden launch,
    the operation is left out (or is the user's draft window outside its first phase), or any of the three
    cannot be read. Playwright then starts Chrome in a window as before. `guard_command` decides with one
    call, so the guard and @playwright/mcp can never disagree about who opens the profile (V10)."""
    if not hidden_launch(config, platform):
        return None
    if operation in SHOWN_OPERATIONS:
        if not (config or {}).get(HEADLESS_PHASE):   # the user's draft window; its first phase is hidden
            return None
    elif operation not in headless_operations(config):
        return None
    if binary is None:
        import setup_steps
        binary = setup_steps.chrome_path(platform)
    if not binary:
        return None
    user_agent, hints, screen = headed_user_agent(binary, "win32"), windows_client_hints(binary), windows_display()
    if not (user_agent and hints and screen):
        return None
    return {"user_agent": user_agent, "client_hints": hints, "screen": screen}


def minimized(config=None, operation=None, platform=None):
    """True when the session's Chrome is headed and kept minimized with the focus keeper (FG2 = a): macOS's
    background launch for an operation that does not run headless (the per-operation fallback)."""
    return background_launch(config, platform) and headless_user_agent(config, operation, platform) is None


def headless_switches(user_agent, scale=None, window=None):
    """Chrome's switches for a headless session: new headless at the normal window size, presenting
    `user_agent`, with navigator.webdriver false as headed, and (given `scale`) the real display's
    device pixel ratio (--force-device-scale-factor: the CSS viewport stays WINDOW_SIZE wide).
    `window` (Windows, `hidden_window`): a window that fits the real display."""
    switches = (HEADLESS_SWITCH, "--window-size=%d,%d" % (window or WINDOW_SIZE), "--user-agent=" + user_agent,
                AUTOMATION_SWITCH)
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


def windows_display(read=None):
    """Windows: {width, height, scale} of the real primary display as a Chrome window on it reports them
    (portable.primary_display), or None when it cannot be read. Never a fallback size and never enlarged
    to the window: on a display smaller than WINDOW_SIZE the hidden window is made to fit (`hidden_window`),
    as a visible window would be."""
    value = (read or portable.primary_display)()
    if not value:
        return None
    width, height, scale = value
    return {"width": int(width), "height": int(height), "scale": scale}


def hidden_window(screen):
    """Windows: the hidden Chrome's window size, WINDOW_SIZE or the real display where that is smaller."""
    if not screen:
        return WINDOW_SIZE
    return min(WINDOW_SIZE[0], int(screen["width"])), min(WINDOW_SIZE[1], int(screen["height"]))


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


# Test sessions only (review E1): an isolated session (the in-memory profile every real-browser test and
# manual fake-site harness uses: tests/browser_fixtures.real_runtime) reaches no host but this computer's,
# and its Chrome uses the mock keychain, never the user's login keychain. A user's session is never
# isolated (it runs on Sidedoor's profile), so its launch is unchanged. Under SIDEDOOR_NO_REAL_BROWSER
# no real Chrome starts at all (real_runtime is None).
TEST_SWITCHES = ("--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost",
                 "--use-mock-keychain", "--password-store=basic")


def test_session(config=None):
    """True for a test session: isolated (the in-memory profile tests use)."""
    return bool(settings(config).get("isolated"))


def launch_config(config=None, operation=None):
    """@playwright/mcp's --config: LAUNCH_CONFIG, plus the off-screen window for its fallback launch. The
    fallback is headed and minimized for every operation (H2): the guard reaches it only when its own
    launch failed, and then runs the session by the --offscreen rules (browser_guard). A test session
    (`test_session`) gets TEST_SWITCHES and keeps Playwright's own mock keychain."""
    value = json.loads(json.dumps(LAUNCH_CONFIG))
    if sys.platform == "darwin":
        # E7: Playwright's channel "chrome" looks only in /Applications; the Chrome Setup found (also
        # ~/Applications) is named outright. Windows and Linux keep Playwright's own lookup.
        import setup_steps
        chrome = setup_steps.chrome_path()
        if chrome:
            value["browser"]["launchOptions"]["executablePath"] = chrome
    if offscreen(config) and operation not in SHOWN_OPERATIONS:
        value["browser"]["launchOptions"]["args"] = list(WINDOW_SWITCHES)
    if test_session(config):
        options = value["browser"]["launchOptions"]
        options["ignoreDefaultArgs"] = [arg for arg in options["ignoreDefaultArgs"] if arg != "--use-mock-keychain"]
        options["args"] = list(options.get("args") or []) + list(TEST_SWITCHES)
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


def visible_window(screen, platform=None, area=None):
    """(left, top, width, height) of the window a person is shown. Windows: wholly inside the display's
    work area (`area`, portable.work_area: the screen less the taskbar; when that cannot be read, the whole
    display `screen`): WINDOW_SIZE at ONSCREEN_POSITION where that fits, else made smaller and moved up and
    left. macOS, or nothing read: the usual ONSCREEN_POSITION and WINDOW_SIZE."""
    if not windows_host(platform):
        return (*ONSCREEN_POSITION, *WINDOW_SIZE)
    area = area or portable.work_area() or ((0, 0, int(screen["width"]), int(screen["height"])) if screen else None)
    if not area:
        return (*ONSCREEN_POSITION, *WINDOW_SIZE)
    left, top, wide, high = area
    width, height = min(WINDOW_SIZE[0], wide), min(WINDOW_SIZE[1], high)
    return (left + max(0, min(ONSCREEN_POSITION[0], wide - width)),
            top + max(0, min(ONSCREEN_POSITION[1], high - height)), width, height)


def windows_client_hints(binary, contract=None, machine=None):
    """Windows: CDP userAgentMetadata for the installed Chrome in a window on this machine, or None when any
    part cannot be read (the session then is not hidden). Brands and versions from chrome.exe's own version;
    platformVersion as Chrome derives it on Windows 10 1809 and later, the UniversalApiContract version from
    the registry ("19.0.0" on Windows 11 24H2; never the kernel's "10.0"); architecture from the machine
    Windows itself runs on (`machine`, portable.native_machine: ARM64 "arm", x64 "x86", so the x64 Chrome on
    Windows-on-ARM says "arm"; owner, V8); bitness "64". A chrome.exe that is not a 64-bit program (its
    header) is not answered for."""
    version = chrome_version(binary, "win32")
    contract = contract if contract is not None else portable.api_contract_version()
    machine = machine if machine is not None else portable.native_machine()
    architecture = {portable.PE_AMD64: "x86", portable.PE_ARM64: "arm"}.get(machine)
    if portable.pe_machine(binary) not in (portable.PE_AMD64, portable.PE_ARM64):
        return None
    if not version or not contract or architecture is None:
        return None
    major = version.split(".")[0]
    return {"brands": brand_list(major, major), "fullVersionList": brand_list(major, version),
            "fullVersion": version, "platform": "Windows", "platformVersion": "%d.%d.0" % tuple(contract),
            "architecture": architecture, "bitness": "64", "model": "", "mobile": False, "wow64": False,
            "formFactors": ["Desktop"]}   # what Chrome in a window on a PC sends (sec-ch-ua-form-factors)


def local_open(url, timeout):
    """Open `url` on this computer (Chrome's own DevTools port) with no proxy: never the system proxy or
    HTTP_PROXY, which do not exempt 127.0.0.1 and would send the request off this computer and fail it."""
    import urllib.request
    return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=timeout)


def running_version(endpoint, opener=None):
    """The version the Chrome on DevTools `endpoint` (this computer's, http://127.0.0.1:<port>) reports
    itself (`/json/version`, its "Browser" value: "Chrome/157.0.8103.51"), or None when it cannot be read."""
    import re
    if not re.fullmatch(r"http://127\.0\.0\.1:\d{1,5}", endpoint or ""):
        return None
    try:
        with (opener or local_open)(endpoint + "/json/version", timeout=5) as answer:
            value = json.loads(answer.read(65536))
    except (OSError, ValueError):
        return None
    product = value.get("Browser") if isinstance(value, dict) else None
    match = re.fullmatch(r"[A-Za-z]+/(\d+\.\d+\.\d+\.\d+)", product) if isinstance(product, str) else None
    return match.group(1) if match else None


def windows_retold(hints, version):
    """Windows: (user agent, client hints) for Chrome `version`, everything else of `hints` kept: what a
    hidden session tells a page when the running Chrome is not the version read from chrome.exe before
    the launch (an update that took effect as it started)."""
    major = version.split(".")[0]
    return WINDOWS_USER_AGENT % major, dict(hints, brands=brand_list(major, major),
                                            fullVersionList=brand_list(major, version), fullVersion=version)


def chrome_app(binary):
    """The .app bundle a Chrome binary path lives in (`.../Google Chrome.app/Contents/MacOS/...`), or None."""
    path = Path(binary)
    for parent in path.parents:
        if parent.suffix == ".app":
            return parent
    return None


def background_command(app, path, onscreen=False, url=None):
    """`open -g -n -a <Chrome.app> --args ...`: Chrome on `path`, never activated, DevTools on a free port.
    `onscreen` (SHOWN_OPERATIONS): the window opens at
    ONSCREEN_POSITION instead of the off-screen one. `url` (support_email's saved draft) is the first page
    instead of about:blank."""
    position = ("--window-position=%d,%d" % ONSCREEN_POSITION, WINDOW_SWITCHES[1]) if onscreen else WINDOW_SWITCHES
    return ["/usr/bin/open", "-g", "-n", "-a", str(app), "--args", "--user-data-dir=" + str(path),
            # Live parity (CK3, accepted): Playwright's own launch also opens a local DevTools port.
            "--remote-debugging-port=0", *BACKGROUND_SWITCHES, *position, url or "about:blank"]


def headless_command(binary, path, user_agent, scale=None, window=None):
    """Chrome's own binary, headless, on `path` with DevTools on a free port. Started directly (no
    LaunchServices): a windowless Chrome has nothing to activate."""
    return [str(binary), "--user-data-dir=" + str(path), "--remote-debugging-port=0", *BACKGROUND_SWITCHES,
            *headless_switches(user_agent, scale, window), "about:blank"]


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


def windows_command(binary, path, url=None, screen=None):
    """chrome.exe on `path`, on-screen at the normal size, DevTools on a free port, with Playwright's
    switches, as background_command. `screen` (the real display): the window is made to lie wholly inside
    its work area (`visible_window`)."""
    left, top, width, height = visible_window(screen, "win32")
    return [str(binary), "--user-data-dir=" + str(path), "--remote-debugging-port=0", *BACKGROUND_SWITCHES,
            RESTORE_BUBBLE_SWITCH,
            "--window-position=%d,%d" % (left, top), "--window-size=%d,%d" % (width, height), url or "about:blank"]


# Chrome's own switch for its "Restore pages? Chrome didn't shut down correctly" bubble: a window Sidedoor
# shows a person never carries it (the Chrome before it on this profile was Sidedoor's own, ended by
# Sidedoor). It changes nothing a site can see.
RESTORE_BUBBLE_SWITCH = "--hide-crash-restore-bubble"


def launch_windows(binary, path, popen=None, timeout=BACKGROUND_LAUNCH_SECONDS, sleep=None, clock=None, url=None,
                   command=None, screen=None):
    """Windows: start Chrome on profile `path` from this process, in a Job Object of its own (never the
    worker's); return its DevTools endpoint (DevToolsActivePort, as launch_background). Raises OSError when
    it cannot start or writes no port within `timeout` (that Chrome is then stopped). `command`: the
    command line instead of `windows_command` (launch_hidden_windows)."""
    import subprocess
    path = Path(path)
    port_file = _fresh_port_file(path)
    if command is None:   # a window: fitted to the real display (given, or read now)
        command = windows_command(binary, path, url, screen if screen is not None else windows_display())
    chrome = (popen or subprocess.Popen)(command,
                                         stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         **portable.worker_spawn_options(core=False))
    portable.contain(chrome)
    previous = _STARTED.pop(str(path), None)
    if previous is not None:
        portable.kill_tree(previous)   # an earlier one's job (that Chrome is gone: prepare_profile checked)
    _STARTED[str(path)] = chrome.pid
    return _await_port(port_file, path, timeout, sleep, clock)


def launch_hidden_windows(binary, path, user_agent, screen=None, popen=None, timeout=BACKGROUND_LAUNCH_SECONDS,
                          sleep=None, clock=None):
    """Windows (owner, 2026-11-12, option A): start Chrome without a window on profile `path`
    (`headless_command`, the window fitted to the real display), from this process and in a Job Object of
    its own, as `launch_windows`: it ends with `end_session` here, when this process ends (the job's handle
    closes: kill-on-close), and, started by a session's guard, with that session's own job. Returns its
    DevTools endpoint; raises OSError as launch_windows does."""
    command = headless_command(binary, Path(path), user_agent, (screen or {}).get("scale"),
                               hidden_window(screen) if screen else None)
    return launch_windows(binary, path, popen=popen, timeout=timeout, sleep=sleep, clock=clock, command=command)


def chrome_running(path):
    """True while a Chrome holds profile `path`: Windows, Chrome's lockfile (portable.profile_busy);
    elsewhere a SingletonLock pid whose command line carries this profile."""
    if portable.WINDOWS:
        return portable.profile_busy(path)
    pid = lock_holder(path)
    return pid is not None and holds_profile(pid, path)


def launch_background(app, path, run=None, timeout=BACKGROUND_LAUNCH_SECONDS, sleep=None, clock=None, stop=None,
                      onscreen=False, url=None):
    """Start Chrome on profile `path` without activating it; return its DevTools endpoint
    (`http://127.0.0.1:<port>`, from the DevToolsActivePort file Chrome writes into the profile).
    Raises OSError when `open` fails or no port appears within `timeout`; before that last raise the
    Chrome `open` started on this profile is stopped (`end_session`: only a Chrome whose command line
    carries --user-data-dir=<path>), so it cannot clash with Playwright's fallback launch (CK2)."""
    import subprocess
    path = Path(path)
    port_file = _fresh_port_file(path)
    done = (run or subprocess.run)(background_command(app, path, onscreen, url), stdin=subprocess.DEVNULL,
                                   capture_output=True, text=True, timeout=timeout)
    if done.returncode != 0:
        raise OSError("open -g failed: " + (done.stderr or "").strip()[:200])
    return _await_port(port_file, path, timeout, sleep, clock, stop)


CLOSE_WAIT_SECONDS = 10.0   # how long a Chrome asked to close itself is given before it is stopped


def wait_chrome_gone(path, wait_seconds=CLOSE_WAIT_SECONDS, poll_seconds=0.1, clock=None, sleep=None):
    """macOS and Linux: wait up to `wait_seconds` for the Chrome on profile `path` to exit by itself (it was
    asked to close). True when none holds the profile any more. Never raises."""
    import time
    clock, sleep = clock or time.monotonic, sleep or time.sleep
    deadline = clock() + wait_seconds
    try:
        while chrome_running(path):
            if clock() >= deadline:
                return False
            sleep(poll_seconds)
    except Exception:
        return False
    return True


CLEAN_CLOSE_VARIABLE = "SIDEDOOR_GUARD_CLEAN_CLOSE"   # set for a guard whose own exit may wait for Chrome (the live view's)


def launched_endpoint(directory):
    """The DevTools endpoint of the Chrome this session's guard started (its BACKGROUND_LAUNCHED record), or None."""
    try:
        lines = (Path(directory) / GUARD_AUDIT).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("decision") == "BACKGROUND_LAUNCHED" and isinstance(row.get("endpoint"), str):
            return row["endpoint"]
    return None


def close_wait(deadline, clock=None):
    """How long a session's starter waits for Chrome to close itself: up to CLOSE_WAIT_SECONDS, but only out
    of the time the session did not use (`deadline` is its own, on the monotonic clock). A session that
    ended at its time limit gets no wait, so everything the caller does next (handing in the result under
    its lease) is no later than it was before this wait existed."""
    import time
    return max(0.0, min(CLOSE_WAIT_SECONDS, deadline - (clock or time.monotonic)()))


def close_cleanly(config, directory, wait_seconds=CLOSE_WAIT_SECONDS):
    """macOS and Linux, after a browser session has ended and its outcome is decided: ask the Chrome that
    session's guard started to close itself and wait up to `wait_seconds` for it to exit, so what the site
    set in the session's last seconds (a sign-in, a new account) is written to the profile for the next
    session. The caller then stops whatever is left exactly as before (`end_session`). The guard has
    normally asked already, without waiting and without signalling (it leaves Chrome to this process);
    asking again is harmless, and a guard that was stopped before it could ask is covered here.

    It is called from a session's `finally`, outside its time limit and after the engine's exit was judged,
    and it never raises and returns nothing a caller acts on: it cannot turn a finished session into a
    timeout or a failure (a submission the site accepted is never retried because of it). Not for the
    user's draft window (SHOWN_OPERATIONS), a test's isolated profile, or Windows (there the guard asks
    before its Job Object ends Chrome). True when Chrome exited by itself."""
    try:
        operation = (config or {}).get("_browser_operation") or (config or {}).get("_application_operation")
        if windows_host() or settings(config).get("isolated") or operation in SHOWN_OPERATIONS:
            return False
        path = profile(config)
        if not chrome_running(path):
            return False
        # Asked here too, whatever the answer: the guard has normally asked already, and a Chrome that is
        # shutting down may no longer take the request (its DevTools port closes first). Chrome is still
        # running, so it is waited for either way: one that was asked by anyone is writing its cookies, and
        # one nobody managed to ask simply gets the old stop after the wait.
        endpoint = launched_endpoint(directory)
        if endpoint:
            # The session's last site-session capture, BEFORE the request to close (after it Chrome no
            # longer answers). Its time comes out of the same wait.
            import time
            began = time.monotonic()
            if last_capture(config, directory, endpoint, wait_seconds):
                wait_seconds = max(0.0, wait_seconds - (time.monotonic() - began))
            try:
                close_browser(endpoint)
            except Exception:
                pass
        return wait_chrome_gone(path, wait_seconds)
    except Exception:
        return False


LAST_CAPTURE_SECONDS = 3.0   # at most this long, out of the starter's wait for Chrome to close


def devtools_cookies(endpoint, seconds=LAST_CAPTURE_SECONDS, connect=None, opener=None):
    """The cookies the Chrome on DevTools `endpoint` holds (CDP Storage.getCookies on its own browser
    socket, no proxy), as Chrome gives them. Raises when Chrome does not answer in `seconds`. The values
    are in the answer only: the connection's logger can never write, and nothing here logs."""
    import logging
    import re
    import time
    if not re.fullmatch(r"http://127\.0\.0\.1:\d{1,5}", endpoint or ""):
        raise ValueError("endpoint")
    deadline = time.monotonic() + seconds
    with (opener or local_open)(endpoint + "/json/version", timeout=max(0.1, seconds)) as answer:
        url = json.loads(answer.read(65536)).get("webSocketDebuggerUrl")
    if not isinstance(url, str) or not url.startswith("ws://127.0.0.1:"):
        raise ValueError("endpoint")
    if connect is None:
        from websockets.sync.client import connect as dial
        silent = logging.getLogger("sidedoor-devtools-wire")   # the library logs every frame at DEBUG: never
        silent.disabled, silent.propagate = True, False
        connect = lambda address: dial(address, proxy=None, open_timeout=max(0.1, deadline - time.monotonic()),   # noqa: E731
                                       close_timeout=1, max_size=64 * 1024 * 1024, logger=silent)
    socket = connect(url)
    try:
        socket.send(json.dumps({"id": 1, "method": "Storage.getCookies"}))
        while True:
            answer = json.loads(socket.recv(timeout=max(0.1, deadline - time.monotonic())))
            if isinstance(answer, dict) and answer.get("id") == 1:
                cookies = (answer.get("result") or {}).get("cookies")
                if not isinstance(cookies, list):
                    raise ValueError("cookies")
                return cookies
    finally:
        try:
            socket.close()
        except Exception:
            pass


def last_capture(config, directory, endpoint, seconds, read=None):
    """A session that restores and captures a site's session (site_sessions.CONFIG_KEY), at its end: what
    Chrome holds for that site now is kept as the session's capture (site_sessions.save_last), by this
    process, because the guard's own last capture does not run when its engine ends the guard first. The
    result is written to the session's guard audit as the guard writes its own captures (a count of sites,
    or the kind of failure; never a value). No capture when the session left no time (`seconds`). True
    when Chrome was asked. Never raises."""
    try:
        import time
        import site_sessions
        site = (config or {}).get(site_sessions.CONFIG_KEY)
        if not isinstance(site, str) or not site_sessions.SITE.fullmatch(site) or seconds <= 0:
            return False
        failed, asked = [], []

        def cookies():
            asked.append(True)
            try:
                return (read or devtools_cookies)(endpoint, min(seconds, LAST_CAPTURE_SECONDS))
            except Exception as error:
                failed.append(type(error).__name__)   # the kind only: never the text
                return []
        kept = site_sessions.save_last(site, cookies)
        if failed:
            row = {"decision": "SITE_SESSION_FAILED", "step": "capture", "reason": failed[0]}
        elif isinstance(kept, str):
            row = {"decision": "SITE_SESSION_FAILED", "step": "capture", "reason": kept}
        elif kept:
            row = {"decision": "SITE_SESSION_CAPTURED", "site": site, "sites": kept, "final": True}
        else:
            return bool(asked)   # nothing to keep is not recorded (as the guard)
        try:
            with open(Path(directory) / GUARD_AUDIT, "a", encoding="utf-8") as audit:
                audit.write(json.dumps(dict(row, by="starter", at=time.time())) + "\n")
        except OSError:
            pass
        return bool(asked)
    except Exception:
        return False


def close_browser(endpoint, connect=None, opener=None):
    """Ask the Chrome on DevTools `endpoint` to close itself (CDP Browser.close on its own browser socket,
    no proxy), the way a person closing it ends it: its profile then records a clean exit. True when asked."""
    import re
    if not re.fullmatch(r"http://127\.0\.0\.1:\d{1,5}", endpoint or ""):
        return False
    try:
        with (opener or local_open)(endpoint + "/json/version", timeout=5) as answer:
            url = json.loads(answer.read(65536)).get("webSocketDebuggerUrl")
        if not isinstance(url, str) or not url.startswith("ws://127.0.0.1:"):
            return False
        if connect is None:
            from websockets.sync.client import connect as dial
            connect = lambda address: dial(address, proxy=None, open_timeout=5, close_timeout=1)   # noqa: E731
        socket = connect(url)
        try:
            socket.send(json.dumps({"id": 1, "method": "Browser.close"}))
            try:
                socket.recv(timeout=2)   # its answer, or the socket closing: either way Chrome has the call
            except Exception:
                pass
        finally:
            try:
                socket.close()
            except Exception:
                pass
        return True
    except Exception:
        return False


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


def mcp_command(config, directory, operation=None, hidden=None):
    """The @playwright/mcp command line (bundled Node, absolute paths: no npx shim). In background
    mode it carries no profile: the guard starts Chrome and adds `--cdp-endpoint` (browser_guard).
    `hidden` (Windows): guard_command's one decision that the guard starts Chrome; None: decided here."""
    if hidden is None:
        hidden = bool(hidden_launch(config) and headless_user_agent(config, operation))
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
    elif not background_launch(config) and not hidden:
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
        start = (config or {}).get(START_URL_CONFIG_KEY)
        if operation == "submit" and claim and isinstance(start, str) and start.startswith(("http://", "https://")) \
                and not any(mark.isspace() for mark in start):
            # E16.T2: an attended submit's guard opens the application's own address itself, at once.
            command.extend(["--start-url", start])
    import site_sessions
    site = (config or {}).get(site_sessions.CONFIG_KEY)
    if isinstance(site, str) and site_sessions.SITE.fullmatch(site):
        # E15.T4: the site whose saved session the guard restores and captures (a host name, never a secret).
        command.extend(["--site-session", site])
    import browser_guard
    if (config or {}).get(browser_guard.NO_CODE_CONFIG_KEY) is True:
        command.append("--no-page-code")   # Sidedoor's own steps: the tool that ends a session is not offered
    import saved_sign_ins
    site = (config or {}).get(saved_sign_ins.CONFIG_KEY)
    if isinstance(site, str) and site_sessions.SITE.fullmatch(site):
        # E15.T7: the session holds the guard's fill tool for this site's saved sign-in (a host name only).
        command.extend(["--saved-sign-in", site])
        email = (config or {}).get(saved_sign_ins.CREATE_CONFIG_KEY)
        if isinstance(email, str) and email.strip() and not email.startswith("-"):
            command.extend(["--create-account", email.strip()])   # E15.T8: this session may create the account
        recorded = (config or {}).get(saved_sign_ins.RESET_CONFIG_KEY)
        if isinstance(recorded, str) and recorded.strip() and not recorded.startswith("-"):
            command.extend(["--reset-recorded", recorded.strip()])   # the step record permits a reset here
        if (config or {}).get(saved_sign_ins.ACCESS_CONFIG_KEY) is True:
            command.append("--access-step")
            import browser_guard
            if (config or {}).get(browser_guard.SSO_CONFIG_KEY) is True:
                command.append("--single-sign-on")   # E15.T9: the step may press the provider's button
    # Windows: one read of the machine decides hidden or not, for the guard and for @playwright/mcp (V10).
    told = windows_told(config, operation)
    hidden = told is not None
    user_agent = told["user_agent"] if hidden else None if hidden_launch(config) else headless_user_agent(config, operation)
    if background_launch(config) or hidden:
        command.extend(["--background-chrome", str(profile(config))])
    if user_agent:
        command.extend(["--headless-user-agent", user_agent])   # headless (owner, 2026-10-24, option A)
        import setup_steps
        hints = told["client_hints"] if hidden else client_hints(setup_steps.chrome_path())
        if hints:
            command.extend(["--headless-client-hints", json.dumps(hints, sort_keys=True, separators=(",", ":"))])
        screen = told["screen"] if hidden else screen_metrics()
        command.extend(["--headless-screen", "%dx%d@%s" % (screen["width"], screen["height"], _number(screen["scale"]))])
    elif offscreen(config) and operation not in SHOWN_OPERATIONS:
        command.append("--offscreen")
    return command + ["--"] + mcp_command(config, directory, operation, hidden=hidden)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


# The user-session variables Chrome needs to find the user's own login keychain (macOS: the "Chrome Safe
# Storage" key the profile's cookies are encrypted with) or profile folders (Windows). Codex runs with a
# small allow-list and starts MCP servers with a filtered environment, so without these Chrome cannot find
# the keychain and asks on every launch. Values come from this (the parent) process; never a secret.
SESSION_VARIABLES = ("HOME", "USER", "LOGNAME", "TMPDIR", "SHELL", "__CF_USER_TEXT_ENCODING",
                     "USERPROFILE", "USERNAME", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP",
                     "HOMEDRIVE", "HOMEPATH", "SYSTEMROOT",
                     # Windows: where programs are installed (Chrome is looked for there, by Sidedoor's guard
                     # and by @playwright/mcp's own launch).
                     "SYSTEMDRIVE", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432")


STARTER_VARIABLE = "SIDEDOOR_SESSION_STARTER"   # the pid of the process that started a session and stops its Chrome


def session_environment(environ=None):
    """The parent's values of SESSION_VARIABLES that are set (an explicit allow-list: no keys pass)."""
    environ = os.environ if environ is None else environ
    return {name: environ[name] for name in SESSION_VARIABLES if environ.get(name)}


def guard_environment(environ=None):
    """What every engine starts the browser guard with: `session_environment`, and this process's own pid as
    STARTER_VARIABLE. This process starts the session and stops its Chrome at the end; the guard leaves
    Chrome to it while it is alive (browser_guard.Proxy.stop_background_chrome)."""
    return dict(session_environment(environ), **{STARTER_VARIABLE: str(os.getpid())})


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
    tools.extend(browser_guard.held_step_tools(argv))   # E15.T7 to T9: the access step's tools this guard holds
    options = ["mcp_servers." + SERVER + ".command=" + encoded(argv[0]),
               "mcp_servers." + SERVER + ".args=" + encoded(argv[1:]),
               "mcp_servers." + SERVER + ".enabled=true",
               "mcp_servers." + SERVER + ".startup_timeout_sec=" + str(STARTUP_TIMEOUT_SECONDS),
               # Codex has one timeout per server. It is long enough for support_email's sign-in wait when that
               # tool is there; every forwarded browser tool is answered within browser_guard.CALL_TIMEOUT
               # by the guard itself (review fix 6), so only the wait can take longer.
               "mcp_servers." + SERVER + ".tool_timeout_sec="
               + str(HUMAN_CHECK_TOOL_TIMEOUT_SECONDS if human_check else TOOL_TIMEOUT_SECONDS),
               # The arm guard loads the user's policy (desktop_adapter.submission_gate); codex starts
               # MCP servers with a filtered environment, so the profile directory is passed explicitly.
               # The user's session variables go with it, so Chrome finds the real login keychain.
               "mcp_servers." + SERVER + ".env={" + ",".join(
                   encoded(name) + "=" + encoded(value) for name, value in sorted(
                       dict(guard_environment(), **{user_profile.HOME_VARIABLE: str(user_profile.home())}).items()))
               + "}"]
    options.extend("mcp_servers." + SERVER + ".tools." + tool + '.approval_mode="approve"' for tool in tools)
    return options
