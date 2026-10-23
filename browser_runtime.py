"""Where Sidedoor's browser driver lives and how Codex is pointed at it (E7.T1, option A).

Owner, 2026-10-22: bundled Node plus @playwright/mcp behind `sidedoor browser-guard`. Every browser job
(application inspect/investigate/submit/verify/reconcile, the LinkedIn contact finder, setup step 9's
email check) runs `codex exec` with ONE stdio MCP server named `browser`:

    <sidedoor> browser-guard --operation <op> --audit <dir>/guard.jsonl [--claim ..] [--human-check ..]
        -- <node> <runtime>/node_modules/@playwright/mcp/cli.js --browser chrome
           --user-data-dir <Sidedoor's profile> --no-webmcp --output-dir <dir>/browser --config <file>

`<sidedoor>` is `[sys.executable, "browser-guard"]` in a frozen build (the binary re-invoked with a
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

import portable


ROOT = Path(__file__).resolve().parent
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
    base = Path(sys.executable).resolve().parent if frozen() else ROOT
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
                                       timeout=30).stdout.strip()
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
        return [sys.executable]
    return [sys.executable, str(ENTRY)]


def upload_folder(directory):
    return Path(directory) / UPLOAD_FOLDER


def stage(source, directory):
    """Copy a file the session must upload into the session's upload folder and return the copy's
    absolute path. @playwright/mcp reads files only under its output folder and its client's
    workspace root (FINDINGS §4f fix 8e0a20f); the guard announces this folder as that root
    (`--uploads`), and `seal` makes it read-only, so a screenshot, snapshot or find `filename` can
    never overwrite a staged file (review fix 4). The arm guard checks each copy's sha256 against
    its original (application_browser.ArmBinding) and the original résumé against the policy."""
    folder = upload_folder(directory)
    folder.mkdir(parents=True, exist_ok=True)
    source = Path(source)
    target = folder / source.name
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
    return portable.profile_busy(path) or holds_profile(lock_holder(path), path, run)


def unavailable(config=None, run=None):
    """Why a browser session cannot start now, as a capacity-wait code suffix, or None (review fix 2):
    "RUNTIME_MISSING" or "PROFILE_IN_USE". Checked before any claim, request or reservation."""
    if missing(config):
        return "RUNTIME_MISSING"
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


def mcp_command(config, directory):
    """The @playwright/mcp command line (bundled Node, absolute paths: no npx shim)."""
    directory = Path(directory)
    output = directory / OUTPUT_FOLDER
    output.mkdir(parents=True, exist_ok=True)
    launch = directory / "browser-config.json"
    launch.write_text(json.dumps(LAUNCH_CONFIG, indent=1) + "\n")
    command = [str(node_path(config)), str(mcp_cli(config)), "--browser", settings(config).get("browser", "chrome")]
    if settings(config).get("isolated"):
        command.append("--isolated")  # tests only: an in-memory profile
    else:
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
    return command + ["--"] + mcp_command(config, directory)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def codex_options(config, directory, operation, claim=None, human_check=None):
    """The `-c` settings that give a codex session the guarded browser as its one browser server."""
    import browser_guard
    import user_profile
    argv = guard_command(config, directory, operation, claim, human_check)
    tools = [tool for tool in browser_guard.TOOLS if operation != "webmail" or tool not in CODE_TOOLS]
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
               "mcp_servers." + SERVER + ".env={" + user_profile.HOME_VARIABLE + "="
               + encoded(str(user_profile.home())) + "}"]
    options.extend("mcp_servers." + SERVER + ".tools." + tool + '.approval_mode="approve"' for tool in tools)
    return options
