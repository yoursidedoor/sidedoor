"""Portable locks and process control (E2.T3 option A; owner, 2026-10-22).

The one place that knows whether Sidedoor runs on POSIX (macOS, later Linux) or Windows. Callers
import this instead of `fcntl` and `resource`. The POSIX branch of every function is the code the
callers ran before, moved as it was, so macOS behaviour does not change. The Windows branch uses
the standard library only (`msvcrt`, `ctypes`):

- locks: `msvcrt.locking` on byte 0. It is `LockFile`, owned by the handle, so two opens of one
  lock file exclude each other even inside one process, as BSD flock does (E5.T1 C2/C4), and the
  OS releases it when the holder dies.
- workers: started suspended in a new process group with no window, put in a Job Object with
  kill-on-close, then resumed. Ending the job ends the whole tree (Codex, its MCP servers and the
  Chrome they drive); so does the death of the process that holds the job (owner, 2026-10-22:
  the stricter Windows behaviour is accepted).
- crash dialogs: `SetErrorMode`, so a windowless worker never waits on an invisible dialog.
- Chrome's profile owner: `<profile>\\lockfile`, which Chrome keeps open without sharing.
- log rotation: rename, or skip when another process holds the file (no lsof on Windows).

`WINDOWS`, `msvcrt` and `_kernel32` are module attributes so tests can take the Windows branch on
macOS with fakes.
"""

import os
import stat
import threading

try:
    import fcntl
except ImportError:          # Windows
    fcntl = None
try:
    import resource
except ImportError:          # Windows
    resource = None
try:
    import msvcrt
except ImportError:          # POSIX
    msvcrt = None

WINDOWS = os.name == "nt"

# Python UTF-8 mode (W1 a, owner 2026-10-23): on Windows every Sidedoor process runs with it, so
# open(), read_text() and pipes default to UTF-8, not the ANSI code page (cp1252), where a profile or
# resume with characters such as Polish or Japanese letters could not be written. Each Python child Sidedoor starts from source
# gets these interpreter flags; the frozen build has the mode compiled in (Nuitka run with -X utf8).
UTF8_FLAGS = ("-X", "utf8") if WINDOWS else ()


def ensure_utf8_mode(frozen=False, warn=None, call=None):
    """Windows, the entry point (W1 a): a process not in UTF-8 mode (`python orchestrator.py ...` typed
    by hand) runs itself again with -X utf8 and exits with that run's code; a frozen build lacking
    the mode logs `warn` and goes on. Children inherit PYTHONUTF8=1. Elsewhere: nothing. Returns the
    exit code to end with, or None to go on."""
    import sys
    if not WINDOWS:
        return None
    os.environ["PYTHONUTF8"] = "1"
    if sys.flags.utf8_mode:
        return None
    if frozen:
        if warn:
            warn("WINDOWS_UTF8_MODE_OFF: the build was not made with -X utf8; files use the ANSI code page")
        return None
    import subprocess
    argv = [sys.executable, *UTF8_FLAGS, *sys.orig_argv[1:]]
    process = (call or subprocess.Popen)(argv)
    while True:
        try:
            return process.wait()
        except KeyboardInterrupt:   # the console's Ctrl-C reaches the child too; wait for it to end
            continue

# Windows constants (winbase.h / winnt.h / tlhelp32.h).
CREATE_SUSPENDED = 0x00000004
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
SEM_FAILCRITICALERRORS = 0x0001
SEM_NOGPFAULTERRORBOX = 0x0002
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
TH32CS_SNAPTHREAD = 0x00000004
THREAD_SUSPEND_RESUME = 0x0002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
INVALID_HANDLE_VALUE = -1
ERROR_ACCESS_DENIED = 5


# ---------------------------------------------------------------------------------------------
# Locks (L1-L5)

def open_lock(path):
    """Open (create) a lock file for reading and writing and return its descriptor, refusing a
    symlink. The caller checks that it is a regular file with one link, as before (G10).

    POSIX: `O_NOFOLLOW` (a symlink raises OSError ELOOP). Windows has no O_NOFOLLOW: `lstat`
    refuses a symlink or other reparse point, and after the open the descriptor must be the same
    file the `lstat` saw (no swap in between)."""
    if not WINDOWS:
        return os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    import errno
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        before = None
    if before is not None and (stat.S_ISLNK(before.st_mode)
                               or getattr(before, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT):
        raise OSError(errno.ELOOP, "Lock file is a link", str(path))
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0), 0o600)
    try:
        after = os.lstat(path)
        opened = os.fstat(descriptor)
    except OSError:
        os.close(descriptor)
        raise
    if (stat.S_ISLNK(after.st_mode) or getattr(after, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
            or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
            or (before is not None and (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino))):
        os.close(descriptor)
        raise OSError(errno.ELOOP, "Lock file changed while it was opened", str(path))
    return descriptor


def _fd(lock):
    return lock if isinstance(lock, int) else lock.fileno()


def try_lock(lock):
    """One non-blocking try for an exclusive lock on `lock` (a descriptor or a file object).
    True when taken. POSIX: `flock(LOCK_EX | LOCK_NB)`. Windows: `LockFile` on byte 0 of this
    handle (the seek matters because `orchestrator.single_instance` opens "a+")."""
    if not WINDOWS:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False
    descriptor = _fd(lock)
    os.lseek(descriptor, 0, os.SEEK_SET)
    try:
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


def unlock(lock):
    """Release a lock taken by `try_lock`."""
    if not WINDOWS:
        fcntl.flock(lock, fcntl.LOCK_UN)
        return
    descriptor = _fd(lock)
    os.lseek(descriptor, 0, os.SEEK_SET)
    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)


def owned_by_me(info):
    """True when a stat result belongs to this user. Windows has no uid: the files sit under the
    user's own profile folder, so this is always True there (R3)."""
    if WINDOWS:
        return True
    return info.st_uid == os.getuid()


# ---------------------------------------------------------------------------------------------
# Worker processes (P1, P2)

def no_core():
    """POSIX: no core file for a worker (it could hold the model key or session tokens; G7). Runs
    in the child before exec (`preexec_fn`)."""
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


_error_mode_set = False


def no_crash_dialogs():
    """Windows: a crashing, windowless worker never waits on an invisible "stopped working" dialog
    (the Windows half of G7; children inherit the error mode). Once per process. POSIX: nothing."""
    global _error_mode_set
    if not WINDOWS or _error_mode_set:
        return
    _kernel32().SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX)
    _error_mode_set = True


def worker_spawn_options(core=True):
    """Popen options for a Codex worker whose whole tree is ended at the session's end.

    POSIX: its own session (one signal reaches Codex and its MCP children) and, with `core`, no
    core file. Windows: suspended, its own process group, no window; `contain` must follow."""
    if not WINDOWS:
        if core:
            return {"start_new_session": True, "preexec_fn": no_core}
        return {"start_new_session": True}
    no_crash_dialogs()
    return {"creationflags": CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW | CREATE_SUSPENDED}


def no_window():
    """Popen options for a console program (schtasks, codex, node) started from Sidedoor: on Windows no
    console window of its own (CREATE_NO_WINDOW; the windowed sidedoor.exe has no console to share, so
    each such child would otherwise flash one, WB3). Elsewhere: none."""
    return {"creationflags": CREATE_NO_WINDOW} if WINDOWS else {}


def direct_command(argv, environment=None):
    """E10, Windows: (argv, environment) to start when `argv[0]` is a package manager's .cmd shim
    (`claude.cmd`, `agy.cmd`, `codex.cmd` from npm, pnpm or yarn). A .cmd or .bat runs through cmd.exe,
    which reads quotes, %, &, ^ and ! in the arguments again and stops at 8,191 characters, and Sidedoor
    passes JSON schemas and long tool lists. Each manager writes its shims from a fixed template: npm's
    cmd-shim, and @zkochan/cmd-shim for pnpm and yarn (`_shim_program`). The program a recognised shim runs
    (node.exe next to it, else node on the PATH, with the package's script; or the package's own .exe) is
    started directly with `argv[1:]`, and the variables the shim sets (pnpm's and yarn's NODE_PATH) are set
    the same way on the child's `environment`. Anything else (POSIX, an .exe, a shim shape not recognised,
    no node found) is returned unchanged, so it runs as before. `environment`: the child's variables, whose
    PATH is searched for node (then the usual install folders, as for Codex's node)."""
    if not WINDOWS or not argv or not isinstance(argv[0], str) or not argv[0].lower().endswith((".cmd", ".bat")):
        return argv, environment
    found = _shim_program(argv[0], dict(environment if environment is not None else os.environ))
    if found is None:
        return argv, environment
    program, variables = found
    return program + list(argv[1:]), variables


def direct_argv(argv, environment=None):
    """The argv of `direct_command` alone."""
    return direct_command(argv, environment)[0]


# Every line of npm's cmd-shim .cmd before its last ones (cmd-shim 8, npm 11).
_NPM_HEAD = ["@ECHO off", "GOTO start", ":find_dp0", "SET dp0=%~dp0", "EXIT /b", ":start", "SETLOCAL",
             "CALL :find_dp0"]
_SAFE_ARGUMENTS = r'[^"%^&!|<>]*?'
_PATHEXT = "SET PATHEXT=%PATHEXT:;.JS;=;%"


def _shim_program(shim, environment, depth=0):
    """([program, script...], environment) that the shim at `shim` runs, or None when it is not one of the
    templates. Every line must be the template's own: a shim that does anything else is left to cmd.exe."""
    import re
    try:
        with open(shim, encoding="utf-8", errors="replace") as stream:
            text = stream.read(64 * 1024)
    except OSError:
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    folder = os.path.dirname(os.path.abspath(shim))

    def place(quoted):
        """A quoted path of the shim: under %dp0% (its folder), or absolute; None if it holds a variable."""
        relative = re.fullmatch(r"%~?dp0%?\\(.+)", quoted)
        if relative:
            quoted = os.path.normpath(os.path.join(folder, *[part for part in relative.group(1).split("\\") if part]))
        return None if "%" in quoted else quoted

    if lines[:len(_NPM_HEAD)] == _NPM_HEAD:   # npm
        rest = lines[len(_NPM_HEAD):]
        if len(rest) == 1:   # no shebang: the package's own program
            found = re.fullmatch(r'"(%dp0%\\[^"]+)"\s*%\*', rest[0])
            return _own_program(place(found.group(1)) if found else None, environment, depth)
        named = re.fullmatch(r'IF EXIST "%dp0%\\([A-Za-z0-9_.-]+)\.exe" \(', rest[0]) if len(rest) == 7 else None
        if named is None:
            return None
        name = named.group(1)
        if rest[1:6] != ['SET "_prog=%%dp0%%\\%s.exe"' % name, ") ELSE (", 'SET "_prog=%s"' % name, _PATHEXT, ")"]:
            return None
        found = re.fullmatch(r'endLocal & goto #_undefined_# 2>NUL \|\| title %COMSPEC% & "%_prog%"\s+('
                             + _SAFE_ARGUMENTS + r')\s*"(%dp0%\\[^"]+)"\s*%\*', rest[6])
        if found is None:
            return None
        return _node_program(name, found.group(1), place(found.group(2)), "", folder, environment)

    # pnpm and yarn (@zkochan/cmd-shim): [@SETLOCAL] [NODE_PATH] then the long or the short form.
    if lines[:1] == ["@SETLOCAL"]:
        lines = lines[1:]
    if lines[:1] == ["@IF NOT DEFINED NODE_PATH ("] and len(lines) >= 5:
        value = re.fullmatch(r'@SET "NODE_PATH=([^"%]+)"', lines[1])
        if value is None or lines[2:5] != [") ELSE (", '@SET "NODE_PATH=%s;%%NODE_PATH%%"' % value.group(1), ")"]:
            return None
        before = environment.get("NODE_PATH")
        environment["NODE_PATH"] = value.group(1) + (";" + before if before else "")
        lines = lines[5:]
    elif lines[:1] and lines[0].startswith("@SET NODE_PATH="):   # yarn's older line
        value = lines[0][len("@SET NODE_PATH="):]
        if not value or "%" in value or '"' in value:
            return None
        environment["NODE_PATH"] = value
        lines = lines[1:]
    if len(lines) == 1:
        short = re.fullmatch(r'@"([^"]+)"\s*(.*?)\s*%\*', lines[0])
        if short is None:
            return None
        if not short.group(2):   # no shebang: the package's own program (or yarn's next shim)
            return _own_program(place(short.group(1)), environment, depth)
        found = re.fullmatch("(" + _SAFE_ARGUMENTS + r')\s*"([^"]+)"\s*(' + _SAFE_ARGUMENTS + ")", short.group(2))
        program = place(short.group(1))   # pnpm's nodeExecPath: node's own full path
        if found is None or program is None or not os.path.isfile(program):
            return None
        target = place(found.group(2))
        if target is None:
            return None
        return [program] + found.group(1).split() + [target] + found.group(3).split(), environment
    if len(lines) not in (6, 7):
        return None
    named = re.fullmatch(r'@IF EXIST "%~dp0\\([A-Za-z0-9_.-]+)\.exe" \(', lines[0])
    if named is None:
        return None
    name = named.group(1)
    tail = r'\s+(' + _SAFE_ARGUMENTS + r')\s*"([^"]+)"\s*(' + _SAFE_ARGUMENTS + r')\s*%\*'
    first = re.fullmatch(r'"%~dp0\\' + re.escape(name) + r'\.exe"' + tail, lines[1])
    other = re.fullmatch(re.escape(name) + tail, lines[-2])
    middle = lines[2:-2]
    if (first is None or other is None or first.groups() != other.groups() or lines[-1] != ")"
            or middle not in ([") ELSE (", "@" + _PATHEXT], [") ELSE (", "@SETLOCAL", "@" + _PATHEXT])):
        return None
    arguments, target, after = first.groups()
    return _node_program(name, arguments, place(target), after, folder, environment)


def _own_program(target, environment, depth):
    """The no-shebang form: the package's .exe, or (yarn's global bin) the next shim, read in turn."""
    if target is None or not os.path.isfile(target):
        return None
    if target.lower().endswith(".exe"):
        return [target], environment
    if target.lower().endswith((".cmd", ".bat")) and depth < 3:
        return _shim_program(target, environment, depth + 1)
    return None


def _node_program(name, arguments, target, after, folder, environment):
    """[<name>.exe next to the shim, else on the PATH, then the shebang's arguments, the script, the extra
    arguments], as the shim's `IF EXIST "%dp0%\\<name>.exe"` chooses."""
    import shutil
    if target is None:
        return None
    program = os.path.join(folder, name + ".exe")
    if not os.path.isfile(program):
        path = environment.get("PATH", environment.get("Path", ""))
        try:
            import setup_steps
            folders = setup_steps.install_folders()
        except Exception:
            folders = []
        search = os.pathsep.join([part for part in path.split(os.pathsep) if part] + folders)
        program = shutil.which(name + ".exe", path=search) or shutil.which(name, path=search)
        if not program:
            return None
    return [program] + arguments.split() + [target] + after.split(), environment


_jobs = {}
_jobs_lock = threading.Lock()


def contain(worker):
    """Put a worker started with `worker_spawn_options` under control. POSIX: nothing (its pid is
    its process group). Windows: a Job Object with kill-on-close takes the suspended worker, then
    its main thread is resumed, so nothing it starts can escape the job. If any step fails the
    worker is terminated and OSError is raised (an ordinary failed session)."""
    if not WINDOWS:
        return
    kernel32 = _kernel32()
    job = None
    try:
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise _last_error("CreateJobObjectW")
        info = _structures()["limits"]()
        info.BasicLimitInformation.LimitFlags = (JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                                                 | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION)
        import ctypes
        if not kernel32.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                                ctypes.addressof(info), ctypes.sizeof(info)):
            raise _last_error("SetInformationJobObject")
        if not kernel32.AssignProcessToJobObject(job, int(worker._handle)):
            raise _last_error("AssignProcessToJobObject")
        _resume(kernel32, worker.pid)
    except BaseException:
        if job:
            kernel32.TerminateJobObject(job, 1)
            kernel32.CloseHandle(job)
        try:
            worker.kill()
        except OSError:
            pass
        raise
    with _jobs_lock:
        _jobs[worker.pid] = job


def kill_tree(pid):
    """End a worker and everything it started. Never raises for a worker already gone.

    POSIX: SIGKILL to its process group, as before. Windows: terminate and close its Job Object
    (closing alone would do it too: kill-on-close)."""
    if not WINDOWS:
        import signal
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            # macOS: EPERM for a group whose only member is its leader, exited but not yet reaped (R1)
            pass
        return
    with _jobs_lock:
        job = _jobs.pop(pid, None)
    if job:
        kernel32 = _kernel32()
        kernel32.TerminateJobObject(job, 1)
        kernel32.CloseHandle(job)


def pid_alive(pid):
    """True when process `pid` is alive. Never signals it.

    POSIX: `os.kill(pid, 0)`, as before. Windows: `os.kill(pid, 0)` would be TerminateProcess,
    so OpenProcess + GetExitCodeProcess instead (B2). Access denied means it exists."""
    if not WINDOWS:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    import ctypes
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return _get_last_error() == ERROR_ACCESS_DENIED
    try:
        code = ctypes.c_uint32()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


# ---------------------------------------------------------------------------------------------
# Chrome's profile owner (B1, B2)

def profile_busy(path):
    """Windows: True when a Chrome has this profile open. Chrome keeps `<profile>\\lockfile` open
    without sharing while it runs, so an open of it failing with PermissionError means a Chrome
    holds the profile; the OS frees it when Chrome dies, so there is no stale case. The user's
    everyday Chrome uses its own User Data folder, so it is never taken for Sidedoor's.

    POSIX: False. Chrome there writes the SingletonLock symlink instead, which
    `browser_runtime.lock_holder` / `holds_profile` read."""
    if not WINDOWS:
        return False
    lockfile = os.path.join(str(path), "lockfile")
    try:
        descriptor = os.open(lockfile, os.O_RDWR | getattr(os, "O_BINARY", 0))
    except FileNotFoundError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    os.close(descriptor)
    return False


def wait_profile_free(path, wait_seconds=5.0, poll_seconds=0.1):
    """Windows: when a Chrome holds the profile, wait up to `wait_seconds` for it to let go (the
    session's Chrome dies with its Job Object). True when it was busy and is now free. POSIX:
    False (browser_runtime.end_session stops that Chrome by pid)."""
    import time
    if not profile_busy(path):
        return False
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        time.sleep(poll_seconds)
        if not profile_busy(path):
            return True
    return False


# ---------------------------------------------------------------------------------------------
# What this Windows machine and its Chrome are (E15.T5b: the hidden Chrome reports what the same Chrome
# reports in a window). Each reader answers None when the value cannot be read: never a made-up one.

PE_AMD64, PE_ARM64 = 0x8664, 0xAA64
VERSION_SIGNATURE = 0xFEEF04BD
ENUM_CURRENT_SETTINGS = 0xFFFFFFFF
SM_CXSCREEN = 0
MONITOR_DEFAULTTOPRIMARY = 1
MDT_EFFECTIVE_DPI = 0
CONTRACTS_KEY = r"SOFTWARE\Microsoft\WindowsRuntime\WellKnownContracts"
CONTRACT_NAME = "Windows.Foundation.UniversalApiContract"


def _fixed_file_info():
    import ctypes

    class FixedFileInfo(ctypes.Structure):   # VS_FIXEDFILEINFO
        _fields_ = [(name, ctypes.c_uint32) for name in (
            "dwSignature", "dwStrucVersion", "dwFileVersionMS", "dwFileVersionLS", "dwProductVersionMS",
            "dwProductVersionLS", "dwFileFlagsMask", "dwFileFlags", "dwFileOS", "dwFileType", "dwFileSubtype",
            "dwFileDateMS", "dwFileDateLS")]
    return FixedFileInfo


def _devmode():
    import ctypes

    class DevMode(ctypes.Structure):   # DEVMODEW, the display layout of its union
        _fields_ = [("dmDeviceName", ctypes.c_wchar * 32), ("dmSpecVersion", ctypes.c_uint16),
                    ("dmDriverVersion", ctypes.c_uint16), ("dmSize", ctypes.c_uint16),
                    ("dmDriverExtra", ctypes.c_uint16), ("dmFields", ctypes.c_uint32),
                    ("dmPositionX", ctypes.c_int32), ("dmPositionY", ctypes.c_int32),
                    ("dmDisplayOrientation", ctypes.c_uint32), ("dmDisplayFixedOutput", ctypes.c_uint32),
                    ("dmColor", ctypes.c_int16), ("dmDuplex", ctypes.c_int16), ("dmYResolution", ctypes.c_int16),
                    ("dmTTOption", ctypes.c_int16), ("dmCollate", ctypes.c_int16),
                    ("dmFormName", ctypes.c_wchar * 32), ("dmLogPixels", ctypes.c_uint16),
                    ("dmBitsPerPel", ctypes.c_uint32), ("dmPelsWidth", ctypes.c_uint32),
                    ("dmPelsHeight", ctypes.c_uint32), ("dmDisplayFlags", ctypes.c_uint32),
                    ("dmDisplayFrequency", ctypes.c_uint32), ("dmICMMethod", ctypes.c_uint32),
                    ("dmICMIntent", ctypes.c_uint32), ("dmMediaType", ctypes.c_uint32),
                    ("dmDitherType", ctypes.c_uint32), ("dmReserved1", ctypes.c_uint32),
                    ("dmReserved2", ctypes.c_uint32), ("dmPanningWidth", ctypes.c_uint32),
                    ("dmPanningHeight", ctypes.c_uint32)]
    return DevMode


def _version_api():
    import ctypes
    api = ctypes.WinDLL("version", use_last_error=True)
    api.GetFileVersionInfoSizeW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p]
    api.GetFileVersionInfoSizeW.restype = ctypes.c_uint32
    api.GetFileVersionInfoW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    api.GetFileVersionInfoW.restype = ctypes.c_int
    api.VerQueryValueW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_void_p]
    api.VerQueryValueW.restype = ctypes.c_int
    return api


def file_version(path, api=None):
    """The version in a Windows program file's own version resource ("157.0.8103.51"), or None. For
    chrome.exe this is the version of the Chrome that file starts; Chrome is never started for it."""
    import ctypes
    try:
        if api is None:
            if not WINDOWS:
                return None
            api = _version_api()
        size = api.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return None
        data = ctypes.create_string_buffer(size)
        if not api.GetFileVersionInfoW(str(path), 0, size, data):
            return None
        value, length = ctypes.c_void_p(), ctypes.c_uint32()
        structure = _fixed_file_info()
        if not api.VerQueryValueW(data, "\\", ctypes.byref(value), ctypes.byref(length)) or not value.value \
                or length.value < ctypes.sizeof(structure):
            return None
        info = structure.from_address(value.value)
        if info.dwSignature != VERSION_SIGNATURE:
            return None
        return "%d.%d.%d.%d" % (info.dwFileVersionMS >> 16, info.dwFileVersionMS & 0xFFFF,
                                info.dwFileVersionLS >> 16, info.dwFileVersionLS & 0xFFFF)
    except (OSError, AttributeError, ValueError):
        return None


def pe_machine(path):
    """The machine a Windows program file was built for (its PE header: PE_AMD64, PE_ARM64, ...), or None."""
    try:
        with open(path, "rb") as stream:
            head = stream.read(64)
            if len(head) < 64 or head[:2] != b"MZ":
                return None
            stream.seek(int.from_bytes(head[60:64], "little"))
            header = stream.read(6)
    except OSError:
        return None
    if len(header) < 6 or header[:4] != b"PE\0\0":
        return None
    return int.from_bytes(header[4:6], "little")


class _MachineApi:
    def __init__(self):
        import ctypes
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        self.kernel32.IsWow64Process2.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        self.kernel32.IsWow64Process2.restype = ctypes.c_int

    def GetCurrentProcess(self):
        return self.kernel32.GetCurrentProcess()

    def IsWow64Process2(self, process, own, native):
        return self.kernel32.IsWow64Process2(process, own, native)


def native_machine(api=None):
    """The machine this Windows itself runs on (IsWow64Process2's native machine: PE_AMD64, PE_ARM64, ...),
    whatever this process or Chrome was built for (an x64 program on Windows-on-ARM still reads PE_ARM64),
    or None (Windows before 10 1709 has no such call)."""
    import ctypes
    try:
        if api is None:
            if not WINDOWS:
                return None
            api = _MachineApi()
        own, native = ctypes.c_uint16(), ctypes.c_uint16()
        if not api.IsWow64Process2(api.GetCurrentProcess(), ctypes.byref(own), ctypes.byref(native)):
            return None
        return native.value or None
    except (OSError, AttributeError, ValueError):
        return None


def _read_contract():
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, CONTRACTS_KEY, 0,
                        winreg.KEY_QUERY_VALUE | winreg.KEY_WOW64_64KEY) as key:
        return winreg.QueryValueEx(key, CONTRACT_NAME)[0]


def api_contract_version(read=None):
    """(major, minor) of this Windows' UniversalApiContract, from the registry value Chrome itself reads for
    its platform version (HKLM CONTRACTS_KEY, CONTRACT_NAME: a DWORD, major in the high word), or None.
    Windows 11 24H2 has 19.0; the kernel's "10.0" is never used for this."""
    try:
        if read is None:
            if not WINDOWS:
                return None
            read = _read_contract
        value = read()
    except (OSError, ImportError, ValueError):
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 2 ** 32 or not value >> 16:
        return None
    return value >> 16, value & 0xFFFF


class _DisplayApi:
    def __init__(self):
        import ctypes
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.shcore = ctypes.WinDLL("shcore", use_last_error=True)
        self.user32.EnumDisplaySettingsW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_void_p]
        self.user32.EnumDisplaySettingsW.restype = ctypes.c_int
        self.user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        self.user32.GetSystemMetrics.restype = ctypes.c_int
        self.user32.MonitorFromWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self.user32.MonitorFromWindow.restype = ctypes.c_void_p
        self.shcore.GetDpiForMonitor.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
        self.shcore.GetDpiForMonitor.restype = ctypes.c_long

    def EnumDisplaySettingsW(self, device, mode, out):
        return self.user32.EnumDisplaySettingsW(device, mode, out)

    def GetSystemMetrics(self, index):
        return self.user32.GetSystemMetrics(index)

    def MonitorFromWindow(self, window, flags):
        return self.user32.MonitorFromWindow(window, flags)

    def GetDpiForMonitor(self, monitor, kind, x, y):
        return self.shcore.GetDpiForMonitor(monitor, kind, x, y)

    def SystemParametersInfoW(self, action, parameter, out, flags):
        self.user32.SystemParametersInfoW.argtypes = [ctypes_module().c_uint32, ctypes_module().c_uint32,
                                                      ctypes_module().c_void_p, ctypes_module().c_uint32]
        self.user32.SystemParametersInfoW.restype = ctypes_module().c_int
        return self.user32.SystemParametersInfoW(action, parameter, out, flags)


def ctypes_module():
    import ctypes
    return ctypes


SPI_GETWORKAREA = 0x0030


def _rect():
    import ctypes

    class Rect(ctypes.Structure):   # RECT
        _fields_ = [("left", ctypes.c_int32), ("top", ctypes.c_int32), ("right", ctypes.c_int32),
                    ("bottom", ctypes.c_int32)]
    return Rect


def work_area(api=None):
    """(left, top, width, height) of the primary display's work area (the screen less the taskbar) in the
    CSS pixels `primary_display` reports, or None. SystemParametersInfoW(SPI_GETWORKAREA) answers in the
    same units as GetSystemMetrics' width for this process, so both are scaled by the same factor."""
    import ctypes
    import math
    try:
        if api is None:
            if not WINDOWS:
                return None
            api = _DisplayApi()
        display = primary_display(api)
        told = int(api.GetSystemMetrics(SM_CXSCREEN))
        rect = _rect()()
        if not display or told <= 0 or not api.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
            return None
        factor = display[0] / told
        left, top = math.ceil(rect.left * factor - 1e-9), math.ceil(rect.top * factor - 1e-9)
        right, bottom = math.floor(rect.right * factor + 1e-9), math.floor(rect.bottom * factor + 1e-9)
        if left < 0 or top < 0 or right <= left or bottom <= top or right > display[0] or bottom > display[1]:
            return None
        return left, top, right - left, bottom - top
    except (OSError, AttributeError, ValueError, ZeroDivisionError):
        return None


APP_PATHS_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"


def _read_app_path(name):
    import winreg
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, APP_PATHS_KEY + "\\" + name) as key:
                value = winreg.QueryValueEx(key, None)[0]
            if isinstance(value, str) and value.strip():
                return value.strip().strip('"')
        except OSError:
            continue
    return None


def app_path(name, read=None):
    """Windows: where program `name` ("chrome.exe") is installed, as its installer registered it (App Paths,
    the user's then the machine's), or None. Needs no environment variable."""
    try:
        if read is None:
            if not WINDOWS:
                return None
            read = _read_app_path
        value = read(name)
    except (OSError, ImportError, ValueError):
        return None
    return value if isinstance(value, str) and value else None


def primary_display(api=None):
    """(width, height, scale) of the primary display as a Chrome window on it reports them: the size in
    CSS pixels (the real pixels over the scale) and the display scale (1.25 for 125%), or None.

    Real pixels: EnumDisplaySettingsW (never scaled for the calling process). Scale: what this process is
    told the width is (GetSystemMetrics) and the monitor's DPI as this process sees it (GetDpiForMonitor)
    differ with the process's DPI awareness, but real / told * dpi / 96 is the display's scale in every
    awareness mode (unaware: told is already divided by the scale and the DPI reads 96; aware: told is the
    real width and the DPI is the real one)."""
    import ctypes
    import math
    try:
        if api is None:
            if not WINDOWS:
                return None
            api = _DisplayApi()
        mode = _devmode()()
        mode.dmSize = ctypes.sizeof(mode)
        if not api.EnumDisplaySettingsW(None, ENUM_CURRENT_SETTINGS, ctypes.byref(mode)):
            return None
        width, height = int(mode.dmPelsWidth), int(mode.dmPelsHeight)
        told = int(api.GetSystemMetrics(SM_CXSCREEN))
        monitor = api.MonitorFromWindow(None, MONITOR_DEFAULTTOPRIMARY)
        x, y = ctypes.c_uint32(), ctypes.c_uint32()
        if not monitor or api.GetDpiForMonitor(monitor, MDT_EFFECTIVE_DPI, ctypes.byref(x), ctypes.byref(y)) != 0:
            return None
        if width <= 0 or height <= 0 or told <= 0 or not x.value:
            return None
        scale = round(width / told * x.value / 96, 2)   # 1.25 for 125%; a custom scale such as 110% is 1.1
        if not 1 <= scale <= 5:
            return None
        return math.ceil(width / scale - 1e-9), math.ceil(height / scale - 1e-9), scale
    except (OSError, AttributeError, ValueError, ZeroDivisionError):
        return None


# ---------------------------------------------------------------------------------------------
# Logs (R1, R2)

def fsync_path(path):
    """Flush a file already written and closed to disk. POSIX: fsync on a read-only descriptor, as
    before. Windows: FlushFileBuffers needs write access (fsync of an "rb" handle is EBADF), so the
    file is opened for appending, which never changes it."""
    with open(path, "ab" if WINDOWS else "rb") as stream:
        os.fsync(stream.fileno())


def fsync_directory(path):
    """Flush a directory's entries (new names) to disk. POSIX: fsync on the directory, as before.
    Windows: a directory cannot be opened with os.open and NTFS journals its entries; nothing to do."""
    if WINDOWS:
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def try_rotate(path, archive):
    """Rename `path` to `archive`. True when done. Windows: False when another process holds the
    file (a sharing violation): the attempt itself is the check, with no gap before the act. POSIX:
    errors raise, as a rename always did there (lsof is the check)."""
    try:
        os.replace(path, archive)
        return True
    except PermissionError:
        if not WINDOWS:
            raise
        return False


def try_remove(path):
    """Remove `path`. True when done. Windows: False when another process holds it. POSIX: errors
    raise, as before."""
    try:
        os.unlink(path)
        return True
    except PermissionError:
        if not WINDOWS:
            raise
        return False


# ---------------------------------------------------------------------------------------------
# Windows plumbing (ctypes, every function declared: handles are 64-bit)

_kernel = None


def _kernel32():
    global _kernel
    if _kernel is None:
        _kernel = _declare()
    return _kernel


def _get_last_error():
    import ctypes
    return ctypes.get_last_error()


def _last_error(what):
    code = _get_last_error()
    return OSError(code, "%s failed (Windows error %d)" % (what, code))


def _resume(kernel32, pid):
    """Resume every thread of the suspended worker `pid` (a new process has one)."""
    import ctypes
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if not snapshot or snapshot in (INVALID_HANDLE_VALUE, 2 ** 64 - 1, 2 ** 32 - 1):
        raise _last_error("CreateToolhelp32Snapshot")
    resumed = 0
    try:
        entry = _structures()["thread"]()
        entry.dwSize = ctypes.sizeof(entry)
        more = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while more:
            if entry.th32OwnerProcessID == pid:
                thread = kernel32.OpenThread(THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                if not thread:
                    raise _last_error("OpenThread")
                try:
                    if kernel32.ResumeThread(thread) == 0xFFFFFFFF:
                        raise _last_error("ResumeThread")
                finally:
                    kernel32.CloseHandle(thread)
                resumed += 1
            more = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    if not resumed:
        raise OSError(0, "No thread of the suspended worker was found")


_structs = {}


def _structures():
    """The ctypes structures (plain ctypes: they build on any OS, so tests can use them)."""
    if _structs:
        return _structs
    import ctypes
    DWORD = ctypes.c_uint32

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", DWORD), ("SchedulingClass", DWORD)]

    class Counters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", Counters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    class ThreadEntry(ctypes.Structure):
        _fields_ = [("dwSize", DWORD), ("cntUsage", DWORD), ("th32ThreadID", DWORD),
                    ("th32OwnerProcessID", DWORD), ("tpBasePri", ctypes.c_int32),
                    ("tpDeltaPri", ctypes.c_int32), ("dwFlags", DWORD)]

    _structs.update(limits=Extended, thread=ThreadEntry)
    return _structs


def _declare():
    import ctypes
    from ctypes import wintypes

    ThreadEntry = _structures()["thread"]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": (wintypes.HANDLE, [ctypes.c_void_p, wintypes.LPCWSTR]),
        "SetInformationJobObject": (wintypes.BOOL, [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]),
        "AssignProcessToJobObject": (wintypes.BOOL, [wintypes.HANDLE, wintypes.HANDLE]),
        "TerminateJobObject": (wintypes.BOOL, [wintypes.HANDLE, wintypes.UINT]),
        "CloseHandle": (wintypes.BOOL, [wintypes.HANDLE]),
        "CreateToolhelp32Snapshot": (wintypes.HANDLE, [wintypes.DWORD, wintypes.DWORD]),
        "Thread32First": (wintypes.BOOL, [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]),
        "Thread32Next": (wintypes.BOOL, [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]),
        "OpenThread": (wintypes.HANDLE, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]),
        "ResumeThread": (wintypes.DWORD, [wintypes.HANDLE]),
        "OpenProcess": (wintypes.HANDLE, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]),
        "GetExitCodeProcess": (wintypes.BOOL, [wintypes.HANDLE, ctypes.POINTER(ctypes.c_uint32)]),
        "SetErrorMode": (wintypes.UINT, [wintypes.UINT]),
    }
    for name, (restype, argtypes) in signatures.items():
        function = getattr(kernel32, name)
        function.restype, function.argtypes = restype, argtypes
    return kernel32
