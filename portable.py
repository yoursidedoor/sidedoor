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
