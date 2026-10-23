"""Start at login per OS (E5.T2): one login entry that runs
`sidedoor serve --start --no-browser --at-login --home <Sidedoor home>`.

E5.T1's decision (owner, 2026-10-22): when start at login is on, the OS restarts Sidedoor after a
crash, never after a deliberate Quit. So the entry restarts only on a failed exit, and Sidedoor's
deliberate stops (Ctrl-C, the OS stopping it with SIGTERM at log out or `bootout`, the tray's Quit
later) exit 0 (`clean_stop`).

- macOS: a LaunchAgent `~/Library/LaunchAgents/com.yoursidedoor.sidedoor.plist` (never another
  program's labels) with RunAtLoad and KeepAlive {SuccessfulExit: false}, loaded with
  `launchctl bootstrap gui/<uid> <plist>` and unloaded with `launchctl bootout gui/<uid>/<label>`.
  A file in ~/Library/LaunchAgents is also loaded by macOS at every login. The plist runs a short
  /bin/sh launcher (`LAUNCHER`) that checks the program is still there, appends the output to the
  log files and `exec`s it; when the program or its folder is gone it exits 1 twice and then 0, so
  launchd stops, and leaves a notice for the next open.
- Windows: a Task Scheduler task "Sidedoor" at logon that runs `sidedoor supervise ...` with the
  windowed program (pythonw.exe, or the windowed frozen build), so no console window appears.
  `supervise` starts `serve` with no console (CREATE_NO_WINDOW | DETACHED_PROCESS), its output
  appended to the log files, and starts it again after a non-zero exit (Task Scheduler's own
  RestartOnFailure covers only a task that fails to launch); exit 0 ends it. Not exercised on
  Windows yet.
- Linux: deferred (owner, 2026-10-22: macOS and Windows first). The box says start at login isn't
  available on this system yet, and Sidedoor still works when opened.

Every OS call goes through `runner` (`RUNNER`, default `_run`): a function of an argv list that
returns (exit code, output text).

The entry is written for this program, this code folder, the Sidedoor home and the saved Codex's
PATH. `refresh` rewrites it when any of them changed (at each dashboard start and when Codex is
found again); `status` reports an entry that still differs as needing an update.

Output of the login-started process goes to logs/sidedoor.{stdout,stderr}.log next to the job logs
(`scheduler_loop.spawn`), names `log_retention` rotates and prunes (STREAMS, ARCHIVE_NAME); the
process holds them open while it runs, so it rotates its own at start and then hourly
(`sweep_own_logs`, `rotate_hourly`); on Windows `supervise` rotates them before each start.

The `--at-login` argument marks the process the entry started (it sets SIDEDOOR_STARTED_AT_LOGIN=1,
which its job children inherit; `started_at_login`). Turning start at login off from inside that
process unregisters it without stopping it (no bootout): unticking a box never closes the page the
user is on; a later crash restart of that process exits 0 at once (`unregistered_login_launch`).

Notices for the next open (`login-notice` in the Sidedoor home, kept until a page has shown it):
port 47823 held by another program at login, the registered program gone, Sidedoor crashing again
and again (Windows supervisor).
"""

import json
import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from xml.sax.saxutils import escape

import run_locks
import log
import scheduler_loop
import user_profile
import paths
import portable

LOG = log.get(__name__)


LABEL = run_locks.LAUNCHD_LABEL_PREFIX + "sidedoor"  # com.yoursidedoor.sidedoor
TASK = "Sidedoor"
MARKER = "SIDEDOOR_STARTED_AT_LOGIN"
STDOUT = "sidedoor.stdout.log"
STDERR = "sidedoor.stderr.log"
ARGUMENTS = ("serve", "--start", "--no-browser", "--at-login")
THROTTLE_SECONDS = 30  # launchd: at most one restart this often after a crash
CALL_TIMEOUT = 30

# Injection points (tests replace them; the defaults are the real machine).
RUNNER = None     # None: _run
PLATFORM = None   # None: sys.platform
USER_HOME = None  # None: Path.home()
UID = None        # None: os.getuid()

ON = "Registered. Sidedoor starts when you log in and restarts by itself if it crashes."
OFF = "Off. Open Sidedoor yourself after you log in."
STALE = ("Start at login needs updating: Sidedoor moved or changed since it was registered. Click Update; Sidedoor "
         "still works when you open it.")
# macOS and Windows first (owner, 2026-10-22: "We can do Macos and windows only bETA FIRST. Linux can
# come later"); Linux and any other system get this line and the box does nothing else.
UNAVAILABLE = "Start at login isn't available on this system yet; Sidedoor still works when you open it."
# Plain messages only (the OS's own words go to the log, `_log`). A failure offers [Try again].
MAC_REFUSED = ("Your Mac didn't let Sidedoor start at login. Turn Sidedoor on in System Settings > General > Login "
               "Items, then click Try again. Sidedoor still works when you open it.")
MAC_DISABLED = ("Your Mac has Sidedoor turned off in System Settings > General > Login Items. Turn it on there, "
                "then tick the box again. Sidedoor still works when you open it.")
WINDOWS_DISABLED = ("Sidedoor's start-at-login task is turned off in Task Scheduler. Turn it on there, then tick "
                    "the box again. Sidedoor still works when you open it.")
WINDOWS_REFUSED = ("Windows didn't let Sidedoor start at login. Click Try again; Sidedoor still works when you "
                   "open it.")
REMOVE_REFUSED = "Sidedoor couldn't turn off start at login. Click Try again."
SAVE_REFUSED = ("Sidedoor couldn't save its start-at-login entry on this computer. Click Try again; Sidedoor still "
                "works when you open it.")
# The login entry's PATH (L1): what the old install.py gave its jobs, plus the saved Codex's folder and
# the node next to it (an npm Codex is a node script: "env: node: No such file or directory" otherwise).
BASE_PATH = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")

# Giving up (owner, 2026-10-22): a login launch that cannot work stops the OS's restarts after a few
# tries and leaves a notice for the next open.
GIVE_UP_AFTER = 3
PORT_BUSY_LIMIT = GIVE_UP_AFTER
PORT_BUSY_FILE = "login-port-busy.json"
MISSING_FILE = "login-missing-count"
NOTICE_FILE = "login-notice"
# Launches more than this far apart belong to different logins: each login gets its own 3 tries.
SESSION_GAP_SECONDS = 600
NOTICES = {
    "port_busy": ("Sidedoor couldn't start when you logged in: another program was using port 47823. If it "
                  "happens again, close that program and open Sidedoor."),
    "missing": ("Sidedoor couldn't start when you logged in: it was registered from a place it is no longer in. "
                "Untick and tick Start at login in Settings to register it again."),
    "crashed": ("Sidedoor stopped when you logged in because it kept closing with an error. See Failures and "
                "logs; Sidedoor still works when you open it."),
}
# Windows supervisor: wait before a restart; a run this long resets the crash count.
RESTART_DELAY_SECONDS = 30
STABLE_SECONDS = 600
ROTATE_BYTES = 5 * 1024 * 1024
KEEP_ARCHIVES = 3
ROTATE_EVERY_SECONDS = 3600

# The macOS launcher: $1 Sidedoor home, $2 logs folder, then the command. Runs it when the program
# (and orchestrator.py, from source) is there, creating the logs folder if needed; otherwise counts
# (a count that is not all digits is read as 0, never evaluated) and gives up on the third.
LAUNCHER = r'''h="$1"; l="$2"; shift 2
if [ -x "$1" ] && { [ "$2" = serve ] || [ -f "$2" ]; }; then
  mkdir -p "$l" 2>/dev/null
  rm -f "$h/login-missing-count"
  exec "$@" >>"$l/sidedoor.stdout.log" 2>>"$l/sidedoor.stderr.log"
fi
n=$(cat "$h/login-missing-count" 2>/dev/null)
case "$n" in ""|*[!0-9]*) n=0 ;; esac
n=$((n + 1))
echo "$n" >"$h/login-missing-count" 2>/dev/null || exit 0
if [ "$n" -ge 3 ]; then echo missing >"$h/login-notice"; rm -f "$h/login-missing-count"; exit 0; fi
exit 1
'''


def _run(argv):
    """The real runner: (exit code, combined output). Never raises for a missing program."""
    try:
        # errors="replace": schtasks /Query /XML can print bytes that are not UTF-8 (a user name like
        # "Müller" in the console code page); undecodable bytes never raise here.
        done = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=CALL_TIMEOUT,
                              **portable.no_window())
    except FileNotFoundError:
        return 127, "%s: not found" % argv[0]
    except subprocess.TimeoutExpired:
        return 124, "%s: timed out" % argv[0]
    except OSError as error:
        return 126, "%s: %s" % (argv[0], error.strerror or type(error).__name__)
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def _runner(runner):
    return runner or RUNNER or _run


def _platform(platform):
    value = platform or PLATFORM or sys.platform
    if value == "darwin":
        return "mac"
    if value in ("win32", "cygwin"):
        return "windows"
    return "other"


def _user_home(user_home):
    return Path(user_home or USER_HOME or Path.home())


def _uid(uid):
    if uid is not None:
        return uid
    return UID if UID is not None else os.getuid()


def _sidedoor_home(home):
    return Path(home) if home is not None else user_profile.home()


def _program():
    """[program] or [python, orchestrator.py], as `scheduler_loop.job_command` runs the jobs."""
    if scheduler_loop.frozen():
        return [scheduler_loop.program()]
    return [sys.executable, *portable.UTF8_FLAGS, str(scheduler_loop.ENTRY)]


MOVE_TO_APPLICATIONS = "Move Sidedoor to your Applications folder, then open it again."
TEMPORARY_PARTS = ("/AppTranslocation/", "/private/var/folders/", "/var/folders/")


def app_location_problem(program=None, platform=None, user_home=None, frozen=None):
    """macOS, the built app only (E9.T1 review AP4): MOVE_TO_APPLICATIONS when this copy runs from
    somewhere a start-at-login entry must never point: a translocated copy (macOS runs a downloaded
    app from a random read-only folder until it is moved), a temporary folder, or anywhere outside
    /Applications and ~/Applications (Downloads, a disk image). None when the location is fine, from
    source, and on other systems."""
    if _platform(platform) != "mac" or not (scheduler_loop.frozen() if frozen is None else frozen):
        return None
    where = str(program or scheduler_loop.program())
    if any(part in where for part in TEMPORARY_PARTS):
        return MOVE_TO_APPLICATIONS
    folders = (Path("/Applications"), _user_home(user_home) / "Applications")
    if not any(folder in Path(where).parents for folder in folders):
        return MOVE_TO_APPLICATIONS
    return None


def command(home=None):
    """What the login entry runs: the frozen binary itself (`[<binary>, "serve", ...]`, scheduler_loop.program), or this
    Python with orchestrator.py from source, with `--at-login` and the Sidedoor home it was
    registered for (a login launch must use the same home, item 4)."""
    return _program() + list(ARGUMENTS) + ["--home", str(_sidedoor_home(home))]


def started_at_login():
    """True in the process the login entry started (`--at-login`) and its children."""
    return os.environ.get(MARKER) == "1"


def mark_started_at_login():
    os.environ[MARKER] = "1"


def _log(what, argv, code, output):
    """The OS's own answer, for the log only (stderr: logs/sidedoor.stderr.log or the terminal)."""
    lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
    LOG.warning("start at login: %s: %s exited %s: %s", what, " ".join(argv[:2]), code, (lines[0][:300] if lines else ""))


def _result(enabled, ok, message, code=None):
    return {"enabled": enabled, "ok": ok, "message": message, "code": code}


def _write(path, data):
    """Write `data` (bytes) atomically; returns True when the file changed."""
    path = Path(path)
    try:
        if path.read_bytes() == data:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    with open(temporary, "wb") as handle:
        handle.write(data)
    os.replace(temporary, path)
    return True


def _read(path):
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


def _logs(root):
    logs = paths.logs(root)
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    return logs


# --- macOS ------------------------------------------------------------------------------------------

def plist_path(user_home=None):
    return _user_home(user_home) / "Library" / "LaunchAgents" / (LABEL + ".plist")


def search_path(codex_path=None):
    """PATH for the login-started process: the saved Codex's folder (and its real folder, after
    symlinks), the folder of the `node` next to it, then BASE_PATH. launchd gives a LaunchAgent only
    /usr/bin:/bin:/usr/sbin:/sbin."""
    folders = []
    if codex_path:
        codex = Path(codex_path)
        folders += [str(codex.parent), str(Path(os.path.realpath(codex)).parent)]
        node = shutil.which("node", path=os.pathsep.join([str(codex.parent), os.environ.get("PATH", "")]))
        if node:
            folders.append(str(Path(node).parent))
    return ":".join(dict.fromkeys(folders + list(BASE_PATH)))


def plist(root=None, codex_path=None, home=None):
    root = paths.data(root, _sidedoor_home(home))
    return {"Label": LABEL,
            "ProgramArguments": ["/bin/sh", "-c", LAUNCHER, "sidedoor-login", str(_sidedoor_home(home)),
                                 str(paths.logs(root)), *command(home)],
            "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}, "LimitLoadToSessionType": "Aqua",
            "ThrottleInterval": THROTTLE_SECONDS, "ProcessType": "Interactive",
            "EnvironmentVariables": {"PYTHONUNBUFFERED": "1", "PATH": search_path(codex_path)}}


def _plist_bytes(root, codex_path, home):
    return plistlib.dumps(plist(root, codex_path, home))


def _mac_loaded(run, uid):
    return run(["launchctl", "print", "gui/%d/%s" % (uid, LABEL)])[0] == 0


def _mac_disabled(run, uid):
    """True when launchd lists the label as disabled (`launchctl print-disabled gui/<uid>`; e.g. turned
    off in Login Items, or an earlier `launchctl disable`)."""
    code, output = run(["launchctl", "print-disabled", "gui/%d" % uid])
    if code != 0:
        return False
    for line in output.splitlines():
        if '"%s"' % LABEL in line:
            value = line.split("=>", 1)[-1].strip().lower()
            return value in ("disabled", "true")
    return False


def _mac_enable(run, user_home, root, uid, codex_path, home):
    path = plist_path(user_home)
    _logs(paths.data(root, _sidedoor_home(home)))
    _write(path, _plist_bytes(root, codex_path, home))
    service = "gui/%d/%s" % (uid, LABEL)
    code, output = run(["launchctl", "enable", service])  # as the old install.py did (L2)
    if code != 0:
        _log("enable", ["launchctl", "enable"], code, output)
    argv = ["launchctl", "bootstrap", "gui/%d" % uid, str(path)]
    code, output = run(argv)
    # Loaded already (a second click, or macOS loaded it at login): the file is in place and the new
    # contents apply at the next login. Never booted out here: that would stop a running Sidedoor.
    if code == 0 or "already" in output.lower() or _mac_loaded(run, uid):
        return _result(True, True, ON)
    _log("enable", argv, code, output)
    path.unlink(missing_ok=True)
    return _result(False, False, MAC_REFUSED, "BOOTSTRAP_FAILED")


def _mac_disable(run, user_home, uid):
    plist_path(user_home).unlink(missing_ok=True)
    if not started_at_login():
        run(["launchctl", "bootout", "gui/%d/%s" % (uid, LABEL)])  # "not loaded" is fine: the file is gone
    return _result(False, True, OFF)


# --- Windows (not exercised on Windows yet) --------------------------------------------------------

TASK_COPY = "login-task.xml"  # the definition last given to schtasks, in the Sidedoor home


def windowed_program():
    """The program Task Scheduler starts: the frozen build itself (built windowed, E9.T1), or
    pythonw.exe next to this Python, so no console window opens at logon."""
    if scheduler_loop.frozen():
        return scheduler_loop.program()
    windowed = Path(sys.executable).with_name("pythonw.exe")
    return str(windowed) if windowed.exists() else sys.executable


def windows_user():
    """This user as DOMAIN\\name, the way Windows itself resolves it (GetUserNameExW,
    NameSamCompatible), for the task's <UserId>. %USERDOMAIN% is not reliable: in an SSH session on
    the Windows VM it was WORKGROUP, and schtasks then refused the task ("No mapping between account
    names and security IDs was done"). Falls back to %USERDOMAIN%\\%USERNAME% off Windows (tests) or
    if the call fails."""
    if os.name == "nt":
        try:
            import ctypes
            secur32 = ctypes.WinDLL("secur32")
            size = ctypes.c_ulong(0)
            secur32.GetUserNameExW(2, None, ctypes.byref(size))   # NameSamCompatible; sets the size
            buffer = ctypes.create_unicode_buffer(max(size.value, 1))
            if size.value and secur32.GetUserNameExW(2, buffer, ctypes.byref(size)) and buffer.value:
                return buffer.value
        except (OSError, AttributeError, ValueError):
            pass
    return "\\".join(part for part in (os.environ.get("USERDOMAIN"), os.environ.get("USERNAME")) if part)


def task_xml(root=None, user=None, home=None):
    """The Task Scheduler definition: at logon of this user, the windowed program runs
    `supervise ...` (no cmd.exe, no console); no time limit; RestartOnFailure for a failed launch."""
    root = paths.data(root, _sidedoor_home(home))
    arguments = command(home)[1:]
    arguments[arguments.index("serve")] = "supervise"
    user = user if user is not None else windows_user()
    trigger_user = "<UserId>%s</UserId>" % escape(user) if user else ""
    return ('<?xml version="1.0" encoding="UTF-16"?>\n'
            '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
            '  <RegistrationInfo><Description>Starts Sidedoor when you log in.</Description></RegistrationInfo>\n'
            '  <Triggers><LogonTrigger><Enabled>true</Enabled>%s</LogonTrigger></Triggers>\n'
            '  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType>'
            '<RunLevel>LeastPrivilege</RunLevel></Principal></Principals>\n'
            '  <Settings>\n'
            '    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n'
            '    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n'
            '    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n'
            '    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n'
            '    <RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure>\n'
            '    <Enabled>true</Enabled>\n'
            '  </Settings>\n'
            '  <Actions Context="Author"><Exec><Command>%s</Command><Arguments>%s</Arguments>'
            '<WorkingDirectory>%s</WorkingDirectory></Exec></Actions>\n'
            '</Task>\n') % (trigger_user, escape(windowed_program()), escape(subprocess.list2cmdline(arguments)),
                            escape(str(root)))


def _windows_registered(run):
    return run(["schtasks", "/Query", "/TN", TASK])[0] == 0


def _windows_task(run):
    """The real task (Q2): None when Task Scheduler has none, else {"enabled": bool} from its own
    definition (`schtasks /Query /XML`: the task's <Settings><Enabled>)."""
    try:
        code, output = run(["schtasks", "/Query", "/TN", TASK, "/XML"])
    except ValueError as error:  # e.g. UnicodeDecodeError from a runner that decodes strictly
        _log("query", ["schtasks", "/Query"], type(error).__name__, str(error))
        return None
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    if code != 0:
        return None
    settings = re.search(r"<Settings>(.*?)</Settings>", output or "", re.S)
    enabled = re.search(r"<Enabled>\s*(true|false)\s*</Enabled>", settings.group(1) if settings else "", re.I)
    return {"enabled": not (enabled and enabled.group(1).lower() == "false")}


def _windows_create(run, xml):
    descriptor, name = tempfile.mkstemp(prefix="sidedoor-task-", suffix=".xml")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(xml.encode("utf-16"))
        return run(["schtasks", "/Create", "/TN", TASK, "/XML", name, "/F"])
    finally:
        Path(name).unlink(missing_ok=True)


def _windows_enable(run, root, home):
    _logs(paths.data(root, _sidedoor_home(home)))
    xml = task_xml(root, home=home)
    code, output = _windows_create(run, xml)
    if code == 0:
        _write(_sidedoor_home(home) / TASK_COPY, xml.encode())
        return _result(True, True, ON)
    _log("enable", ["schtasks", "/Create"], code, output)
    return _result(False, False, WINDOWS_REFUSED, "SCHTASKS_FAILED")


def _windows_disable(run, home):
    code, output = run(["schtasks", "/Delete", "/TN", TASK, "/F"])
    if code == 0 or not _windows_registered(run):
        (_sidedoor_home(home) / TASK_COPY).unlink(missing_ok=True)
        return _result(False, True, OFF)
    _log("disable", ["schtasks", "/Delete"], code, output)
    return _result(True, False, REMOVE_REFUSED, "SCHTASKS_FAILED")


# --- the calls ----------------------------------------------------------------------------------

def _stale(system, user_home, root, codex_path, home):
    """True when the registered entry differs from what would be written now (another program,
    code folder, Sidedoor home or Codex PATH)."""
    if system == "mac":
        return _read(plist_path(user_home)) != _plist_bytes(root, codex_path, home)
    if system == "windows":
        saved = _read(_sidedoor_home(home) / TASK_COPY)
        return saved is not None and saved != task_xml(root, home=home).encode()
    return False


def status(runner=None, platform=None, user_home=None, uid=None, home=None, codex_path=None, root=None):
    """{"enabled", "supported", "disabled_by_system", "stale"}: whether Sidedoor will start at login.
    macOS: the plist is there and launchd does not list the label as disabled (L2). "stale": the
    entry no longer matches this program, folder, home or Codex PATH (`refresh` fixes it)."""
    system = _platform(platform)
    state = {"enabled": False, "supported": system in ("mac", "windows"), "disabled_by_system": False, "stale": False}
    if system == "mac":
        if not plist_path(user_home).is_file():
            return state
        disabled = _mac_disabled(_runner(runner), _uid(uid))
        state.update(enabled=not disabled, disabled_by_system=disabled)
    elif system == "windows":
        task = _windows_task(_runner(runner))
        disabled = task is not None and not task["enabled"]
        state.update(enabled=bool(task) and not disabled, disabled_by_system=disabled)
    if state["disabled_by_system"]:
        state["disabled_message"] = MAC_DISABLED if system == "mac" else WINDOWS_DISABLED
    if state["enabled"]:
        state["stale"] = _stale(system, user_home, root, codex_path, home)
    return state


def enable(runner=None, platform=None, user_home=None, root=None, uid=None, codex_path=None, home=None):
    """Register (idempotent). {"enabled", "ok", "message", "code"}; never raises for an OS refusal."""
    run, system = _runner(runner), _platform(platform)
    if app_location_problem(platform=platform, user_home=user_home):
        return _result(False, False, MOVE_TO_APPLICATIONS, "APP_NOT_IN_APPLICATIONS")
    try:
        if system == "mac":
            return _mac_enable(run, user_home, root, _uid(uid), codex_path, home)
        if system == "windows":
            return _windows_enable(run, root, home)
    except OSError as error:
        _log("enable", ["write", str(plist_path(user_home))], error.errno, error.strerror or type(error).__name__)
        return _result(False, False, SAVE_REFUSED, "WRITE_FAILED")
    return _result(False, False, UNAVAILABLE, "UNSUPPORTED_SYSTEM")


def disable(runner=None, platform=None, user_home=None, uid=None, home=None):
    """Unregister (idempotent); a process started at login keeps running."""
    run, system = _runner(runner), _platform(platform)
    try:
        if system == "mac":
            return _mac_disable(run, user_home, _uid(uid))
        if system == "windows":
            return _windows_disable(run, home)
    except OSError as error:
        _log("disable", ["remove", str(plist_path(user_home))], error.errno, error.strerror or type(error).__name__)
        return _result(True, False, REMOVE_REFUSED, "WRITE_FAILED")
    return _result(False, True, OFF)


def set_enabled(enabled, **options):
    if enabled:
        return enable(**options)
    return disable(**{key: value for key, value in options.items() if key not in ("root", "codex_path")})


def refresh(runner=None, platform=None, user_home=None, root=None, codex_path=None, home=None):
    """Rewrite a registered entry whose content would differ now (item 5): macOS rewrites the plist
    only (no bootout or bootstrap: launchd reads it at the next login, and a running Sidedoor is never
    stopped); Windows re-creates the task (`/F`; a running task is not stopped). Returns True when it
    rewrote. Never raises. A copy running from a temporary or Downloads location never repoints an
    entry (AP4): the entry keeps naming the copy in Applications."""
    system = _platform(platform)
    if app_location_problem(platform=platform, user_home=user_home):
        return False
    try:
        if system == "mac":
            path = plist_path(user_home)
            return path.is_file() and _write(path, _plist_bytes(root, codex_path, home))
        if system == "windows":
            if _read(_sidedoor_home(home) / TASK_COPY) is None or not _stale(system, user_home, root, codex_path, home):
                return False
            task = _windows_task(_runner(runner))
            if task is None or not task["enabled"]:  # deleted or turned off in Task Scheduler: left as the user set it
                return False
            return _windows_enable(_runner(runner), root, home)["ok"]
    except (OSError, ValueError) as error:
        _log("refresh", ["write", str(plist_path(user_home))], getattr(error, "errno", None),
             getattr(error, "strerror", None) or type(error).__name__)
    return False


def unregistered_login_launch(platform=None, user_home=None, home=None):
    """L3: True in a process the login entry started after the user turned start at login off.
    Turning it off from inside that process removes the entry without `bootout` (which would close
    the page), so on macOS the job stays loaded for the session and KeepAlive would restart it after
    a crash; such a restart finds no entry and exits 0 at once (`serve`), which ends the restarts."""
    if not started_at_login():
        return False
    system = _platform(platform)
    if system == "mac":
        return not plist_path(user_home).is_file()
    if system == "windows":
        return not (_sidedoor_home(home) / TASK_COPY).is_file()
    return False


# --- giving up, and the notice for the next open ----------------------------------------------------

def _load_json(path):
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def note_port_busy(home, now=None):
    """A login launch found port 47823 held by another program: count it and return the count. The
    count starts over at a new login (item 7: launches more than SESSION_GAP_SECONDS apart). On the
    third, the notice for the next open is left."""
    now = time.time() if now is None else now
    path = Path(home) / PORT_BUSY_FILE
    saved = _load_json(path)
    count = saved.get("count") if isinstance(saved.get("count"), int) else 0
    last = saved.get("at") if isinstance(saved.get("at"), (int, float)) else None
    if last is None or now - last > SESSION_GAP_SECONDS:
        count = 0
    count += 1
    Path(home).mkdir(parents=True, exist_ok=True, mode=0o700)
    _write(path, json.dumps({"count": count, "at": now}).encode())
    if count >= PORT_BUSY_LIMIT:
        leave_notice(home, "port_busy")
    return count


def clear_port_busy(home):
    """A start that bound the port: the count is spent (the notice, if any, stays until shown)."""
    (Path(home) / PORT_BUSY_FILE).unlink(missing_ok=True)


def leave_notice(home, kind):
    Path(home).mkdir(parents=True, exist_ok=True, mode=0o700)
    _write(Path(home) / NOTICE_FILE, (kind + "\n").encode())


def take_notice(home):
    """The pending notice's text, removed as it is taken: a page shows it once (item 8), whichever
    process serves that page."""
    path = Path(home) / NOTICE_FILE
    try:
        kind = path.read_text().strip()
    except OSError:
        return None
    path.unlink(missing_ok=True)
    return NOTICES.get(kind)


# --- Windows supervisor (`sidedoor supervise`, item 1) -------------------------------------------

def _rotate_plain(logs, now=None):
    """Windows (the supervisor's own logs; no process rotates its own fds there, E2.T3 R2): before each start, a log over ROTATE_BYTES is
    renamed to an archive (log_retention.ARCHIVE_NAME's shape) and only KEEP_ARCHIVES stay."""
    for name in (STDOUT, STDERR):
        path = Path(logs) / name
        try:
            if path.stat().st_size < ROTATE_BYTES:
                continue
            stamp = "%020d" % (time.time_ns() if now is None else now)
            path.rename(path.with_name("%s.retained.%s.%s" % (name, stamp, uuid.uuid4().hex)))
        except OSError:
            continue
        archives = sorted(Path(logs).glob(name + ".retained.*"))
        for old in archives[:-KEEP_ARCHIVES]:
            old.unlink(missing_ok=True)


class _LogFile:
    """One log the supervisor writes for `serve` (Q6): it owns the file, so it can rotate it while
    serve runs (Windows cannot rename a file another process holds open): over ROTATE_BYTES the file
    is closed, archived (`_rotate_plain`) and opened again."""

    def __init__(self, logs, name):
        self.logs, self.path, self.handle = Path(logs), Path(logs) / name, None
        self.dropped = 0
        self.lock = threading.Lock()

    def write(self, data):
        """Never raises: a failed write (a full disk, a sharing violation from antivirus or a tail
        tool) drops that chunk and the file is opened again for the next one, so the pump keeps
        draining serve's pipe and serve never blocks on a full pipe."""
        with self.lock:
            try:
                if self.handle is None:
                    self.handle = open(self.path, "ab")
                self.handle.write(data)
                self.handle.flush()
                if self.handle.tell() >= ROTATE_BYTES:
                    self.handle.close()
                    self.handle = None
                    _rotate_plain(self.logs)
            except (OSError, ValueError):
                self.dropped += len(data)
                try:
                    if self.handle is not None:
                        self.handle.close()
                except (OSError, ValueError):
                    pass
                self.handle = None

    def close(self):
        with self.lock:
            if self.handle is not None:
                self.handle.close()
                self.handle = None


def _pump(stream, log):
    """Drain one of serve's pipes into its log until serve closes it. Only a failed read of the pipe
    ends it; a failed write never does (`_LogFile.write` drops the chunk)."""
    read = stream.read1 if hasattr(stream, "read1") else stream.read
    while True:
        try:
            chunk = read(65536)
        except (OSError, ValueError):
            return
        if not chunk:
            return
        try:
            log.write(chunk)
        except Exception:  # never stop draining: a stopped pump would block serve on a full pipe
            continue


def supervise(child, logs, home, popen=None, sleep=time.sleep, clock=time.monotonic, windows=True):
    """Run `child` (the `serve` command) until it exits 0: a non-zero exit starts it again after
    RESTART_DELAY_SECONDS; GIVE_UP_AFTER exits in a row each within STABLE_SECONDS of its start end it
    with the "crashed" notice. Each start has no console (scheduler_loop.detach_options); its output
    comes through pipes and the supervisor appends it to the log files, rotating them by size while
    serve runs (Q6). Returns 0 (Task Scheduler then does nothing more)."""
    popen = popen or subprocess.Popen
    logs = Path(logs)
    failures = 0
    while True:
        logs.mkdir(parents=True, exist_ok=True)
        _rotate_plain(logs)
        started = clock()
        process = popen(child, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        close_fds=True, **scheduler_loop.detach_options(windows))
        files = (_LogFile(logs, STDOUT), _LogFile(logs, STDERR))
        pumps = [threading.Thread(target=_pump, args=(stream, log), daemon=True)
                 for stream, log in ((process.stdout, files[0]), (process.stderr, files[1]))]
        for pump in pumps:
            pump.start()
        code = process.wait()
        for pump in pumps:
            pump.join()
        for log in files:
            log.close()
        if code == 0:
            return 0
        failures = failures + 1 if clock() - started < STABLE_SECONDS else 1
        if failures >= GIVE_UP_AFTER:
            leave_notice(home, "crashed")
            return 0
        sleep(RESTART_DELAY_SECONDS)


def supervise_main(home, root=None):
    """`sidedoor supervise --start --no-browser --at-login --home H`: what the Windows task runs."""
    child = command(home)
    return supervise(child, paths.logs(root, _sidedoor_home(home)), _sidedoor_home(home))


# --- the process started at login ----------------------------------------------------------------

def rotate_own_streams(root=None, rotate_bytes=None):
    """Q1: rotate this process's own sidedoor.*.log (its fd 1 and 2) once over the size limit, without
    state/runner.lock (the hourly cycle holds it at :00) and without lsof: these files are this
    process's alone (job children write their own logs), so no other writer rotates them. Keeps the
    newest log_retention.RETAIN_ARCHIVES archives of each. Returns how many it rotated; never raises."""
    if os.name == "nt":
        return 0
    try:
        import log_retention
        limit = log_retention.ROTATE_BYTES if rotate_bytes is None else rotate_bytes
        logs = paths.logs(root)
        rotated = 0
        for name in (STDOUT, STDERR):
            path = logs / name
            try:
                info = path.stat()
            except OSError:
                continue
            descriptors = []
            for descriptor in (1, 2):
                try:
                    current = os.fstat(descriptor)
                except OSError:
                    continue
                if (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino):
                    descriptors.append(descriptor)
            if not descriptors or info.st_size < limit:
                continue
            log_retention._rotate(path, descriptors)
            rotated += 1
            archives = sorted(logs.glob(name + ".retained.*"))
            for old in archives[:-log_retention.RETAIN_ARCHIVES]:
                old.unlink(missing_ok=True)
        return rotated
    except Exception as error:
        LOG.warning("log rotation deferred: %s", type(error).__name__)
        return 0


def sweep_own_logs(root=None):
    """In the process started at login, at its start: rotate its own logs (`rotate_own_streams`, no
    lock needed), then the shared retention sweep of logs/ (log_retention, best effort: it skips when
    a job holds state/runner.lock). Never raises; does nothing elsewhere or on Windows (`supervise`
    rotates there)."""
    if not started_at_login() or os.name == "nt":
        return None
    rotate_own_streams(root)
    try:
        import log_retention
        return log_retention.retain(paths.data(root))
    except Exception as error:  # a retention problem never stops Sidedoor from starting
        LOG.warning("log retention deferred: %s", type(error).__name__)
        return None


def rotate_hourly(root=None, stop=None, interval=ROTATE_EVERY_SECONDS, sweep=None):
    """Item 12: in the process started at login, `sweep_own_logs` every `interval` seconds until
    `stop` is set, so a long session never grows its own log without bound. Runs in a daemon thread
    (`start_hourly_rotation`)."""
    stop = stop or threading.Event()
    sweep = sweep or rotate_own_streams  # Q1: never needs runner.lock, so never skipped at :00
    while not stop.wait(interval):
        sweep(root)


def start_hourly_rotation(root=None):
    if not started_at_login() or os.name == "nt":
        return None
    stop = threading.Event()
    threading.Thread(target=rotate_hourly, args=(root, stop), name="sidedoor-log-rotation", daemon=True).start()
    return stop


@contextmanager
def clean_stop():
    """While serving: SIGTERM (log out, `launchctl bootout`, `kill`) is a deliberate
    stop, handled like Ctrl-C. uvicorn catches it while serving, shuts down, then raises the signal
    again under the handler it found: this one, which raises KeyboardInterrupt, which `serve` already
    treats as a normal stop (return 0), so the OS does not restart Sidedoor. A SIGTERM before or after
    uvicorn's own handler takes the same path. Without it the process would die by SIGTERM, which
    launchd's SuccessfulExit=false counts as a crash."""
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "SIGTERM"):
        yield
        return
    previous = signal.signal(signal.SIGTERM, _interrupt)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _interrupt(signum, frame):
    raise KeyboardInterrupt
