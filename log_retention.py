"""Startup log retention, serialized with the existing orchestrator runner lock.

macOS (and later Linux) finds the files another live process writes with lsof and rotates its own
stdout/stderr in place (rename + dup2). Windows has neither: there a rename or remove of a file
another process holds open fails with a sharing violation, so the attempt itself is the check
(`portable.try_rotate` / `try_remove`, skipped and retried later), and a job's own two stream files
are rotated by `scheduler_loop.spawn` just before the next child of that job opens them
(`rotate_for_spawn`; E2.T3, owner 2026-10-22).
"""

import argparse
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import uuid

import portable
import paths


STREAMS = ("launchd.stdout.log", "launchd.stderr.log", "research.stdout.log", "research.stderr.log",
           "applications.stdout.log", "applications.stderr.log",
           "sidedoor.stdout.log", "sidedoor.stderr.log")  # the login-started Sidedoor (login_item, E5.T2)
ROTATE_BYTES = 5 * 1024 * 1024
RETAIN_BYTES = 64 * 1024 * 1024
RETAIN_FILES = 200
RETAIN_SECONDS = 7 * 86400
RETAIN_ARCHIVES = 3
WORKER_NAME = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}(?:\.[a-z0-9.-]+)?\.log\Z")
ARCHIVE_NAME = re.compile(r"(launchd|research|applications|sidedoor)\.(stdout|stderr)\.log\.retained\.[0-9]{20}\.[0-9a-f]{32}\Z")


def _regular(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not portable.owned_by_me(info):
        raise ValueError("Unsafe log file")
    return info


def _same(path, original):
    current = _regular(path)
    return (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) == (
        original.st_dev, original.st_ino, original.st_size, original.st_mtime_ns)


def _open_files(directory):
    result = subprocess.run(["/usr/sbin/lsof", "-nP", "-F", "pfn", "+d", str(directory)],
                            capture_output=True, text=True, timeout=15)
    if result.returncode not in (0, 1) or result.stderr:
        raise RuntimeError("Cannot verify open log descriptors")
    opened = {}
    process = descriptor = None
    for line in result.stdout.splitlines():
        if line.startswith("p"):
            process = int(line[1:])
            descriptor = None
        elif line.startswith("f"):
            descriptor = line[1:]
        elif line.startswith("n"):
            if process is None or descriptor is None:
                raise RuntimeError("Incomplete open-file inventory")
            opened.setdefault(line[1:], set()).add((process, descriptor))
        else:
            raise RuntimeError("Unexpected open-file inventory")
    return opened


def _archive(path):
    return path.with_name(path.name + ".retained." + f"{time.time_ns():020d}" + "." + uuid.uuid4().hex)


def rotate_for_spawn(paths, rotate_bytes=ROTATE_BYTES):
    """Windows (R2): archive each of a job's stream files over `rotate_bytes` before the job's next
    child opens it. A file another process still holds (a child left by an earlier Sidedoor) is
    left in place, nothing lost, and tried again at a later spawn. Rotation never blocks the spawn:
    any other OSError (the file renamed by another process in between, an antivirus transient)
    skips that file too. `retain` keeps its own handling. Returns the number rotated."""
    rotated = 0
    for path in paths:
        path = Path(path)
        try:
            info = _regular(path)
        except (ValueError, OSError):
            continue
        if info.st_size < rotate_bytes:
            continue
        try:
            done = portable.try_rotate(path, _archive(path))
        except OSError:
            continue
        if done:
            rotated += 1
    return rotated


def _rotate(path, descriptors):
    for descriptor in descriptors:
        stream = sys.stdout if descriptor == 1 else sys.stderr
        stream.flush()
    original = _regular(path)
    archive = _archive(path)
    temporary = path.with_name(".retention-new-" + uuid.uuid4().hex)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        if not _same(path, original):
            raise RuntimeError("Log changed before rotation")
        path.rename(archive)
        try:
            temporary.rename(path)
        except OSError:
            archive.rename(path)
            raise
        for destination in descriptors:
            os.dup2(descriptor, destination)
    finally:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)


def retain(root, *, rotate_bytes=ROTATE_BYTES, retain_bytes=RETAIN_BYTES,
           retain_files=RETAIN_FILES, retain_seconds=RETAIN_SECONDS,
           retain_archives=RETAIN_ARCHIVES):
    """Skip busy writers; thresholds apply at successful startup sweeps, not continuously."""
    if min(rotate_bytes, retain_bytes, retain_files, retain_seconds, retain_archives) <= 0:
        raise ValueError("Retention limits must be positive")
    root = Path(root).resolve()
    directory = paths.logs(root)
    if directory.is_symlink() or not directory.is_dir() or paths.state(root).is_symlink():
        raise ValueError("Expected real logs and state directories")
    lock_descriptor = portable.open_lock(paths.state(root) / "runner.lock")
    with os.fdopen(lock_descriptor, "r+") as lock:
        if os.fstat(lock.fileno()).st_nlink != 1 or not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
            raise ValueError("Unsafe runner lock")
        if not portable.try_lock(lock):
            return {"state": "BUSY", "rotated": 0, "removed": 0}
        # Windows: no lsof; the remove or rename below is itself the check (R1).
        opened = {} if portable.WINDOWS else _open_files(directory)
        now = time.time()
        candidates = []
        streams = []
        result = {"state": "COMPLETE", "rotated": 0, "removed": 0, "skipped_open": 0}
        for path in sorted(directory.iterdir()):
            is_stream = path.name in STREAMS
            if not (is_stream or WORKER_NAME.fullmatch(path.name) or ARCHIVE_NAME.fullmatch(path.name)):
                continue
            try:
                info = _regular(path)
            except (ValueError, FileNotFoundError):
                continue
            holders = opened.get(str(path), set())
            if is_stream:
                if info.st_size < rotate_bytes:
                    continue
                descriptors = []
                for descriptor in (1, 2):
                    try:
                        current = os.fstat(descriptor)
                    except OSError:
                        continue
                    if (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino):
                        descriptors.append(descriptor)
                if holders - {(os.getpid(), str(descriptor)) for descriptor in descriptors}:
                    result["skipped_open"] += 1
                    continue
                streams.append((path, info, descriptors))
            elif holders:
                result["skipped_open"] += 1
            else:
                candidates.append((path, info))
        retained_bytes = retained_files = 0
        archive_counts = {}
        for path, info in sorted(candidates, key=lambda item: (item[1].st_mtime_ns, item[0].name), reverse=True):
            stream_name = path.name.split(".retained.")[0] if ARCHIVE_NAME.fullmatch(path.name) else None
            expired = now - info.st_mtime > retain_seconds
            excess = retained_bytes + info.st_size > retain_bytes or retained_files >= retain_files
            excess_archive = stream_name and archive_counts.get(stream_name, 0) >= retain_archives
            if expired or excess or excess_archive:
                if _same(path, info):
                    if portable.try_remove(path):
                        result["removed"] += 1
                        # A research worker log's engine record (engines.record_run) goes with it.
                        engine_record = path.with_name(path.name + ".engine.json")
                        if WORKER_NAME.fullmatch(path.name) and engine_record.is_file() and not engine_record.is_symlink():
                            portable.try_remove(engine_record)
                    else:
                        result["skipped_open"] += 1
            else:
                retained_bytes += info.st_size
                retained_files += 1
                if stream_name:
                    archive_counts[stream_name] = archive_counts.get(stream_name, 0) + 1
        for path, info, descriptors in streams:
            if not _same(path, info):
                continue
            if not portable.WINDOWS:
                _rotate(path, descriptors)
                result["rotated"] += 1
            elif portable.try_rotate(path, _archive(path)):   # no dup2 of our own fds on Windows (R2)
                result["rotated"] += 1
            else:
                result["skipped_open"] += 1
        return result


# Browser traces and recovery records are kept 30 days (owner, 2026-10-06: "StaLE/Old browswer
# traces need to be removed. Anything >1 month old. that should be the ttl", and the application
# recovery records likewise). A session folder goes once its newest file is older than that.
TRACE_TTL_SECONDS = 30 * 86400
TRACE_DIRECTORIES = (("application-browser", "request-*"), ("application-connection-manual", "*"),
                     ("outreach-history", "*"), ("linkedin-contacts", "run-*"), ("application-recovery", "*"),
                     ("email-check", "check-*"))   # setup step 9's mailbox reads (E7.T1)


def _newest(path):
    newest = path.lstat().st_mtime
    for directory, folders, files in os.walk(path, followlinks=False):
        for name in folders + files:
            try:
                newest = max(newest, os.lstat(os.path.join(directory, name)).st_mtime)
            except OSError:
                pass
    return newest


def prune_traces(root, now=None, ttl_seconds=TRACE_TTL_SECONDS):
    """Remove trace and recovery folders whose newest file is older than the TTL."""
    import shutil

    cutoff = (time.time() if now is None else now) - ttl_seconds
    reports = paths.reports(root).resolve()
    removed, freed = [], 0
    for directory, pattern in TRACE_DIRECTORIES:
        base = reports / directory
        if not base.is_dir() or base.is_symlink():
            continue
        for entry in base.glob(pattern):
            # Never follow a link out, never touch anything outside this folder, and skip cheaply
            # when the folder itself changed inside the TTL.
            if entry.is_symlink() or not entry.is_dir() or entry.resolve().parent != base.resolve():
                continue
            if entry.lstat().st_mtime >= cutoff or _newest(entry) >= cutoff:
                continue
            size = sum(os.lstat(os.path.join(d, name)).st_size for d, _unused, names in os.walk(entry) for name in names)
            shutil.rmtree(entry)
            removed.append(str(entry.relative_to(reports)))
            freed += size
    return {"removed": len(removed), "freed_bytes": freed, "examples": removed[:5]}


def startup(root):
    try:
        traces = prune_traces(root)
        if traces["removed"]:
            # Machine contract (E2.T6): a JSON status line (parsed by tests and reports); stays print.
            # A clean-up that worked is output, not an error (owner, 2026-10-27).
            print(json.dumps({"trace_retention": traces}))
    except OSError as error:
        # Machine contract (E2.T6): a JSON status line (parsed by tests and reports); stays print.
        print(json.dumps({"trace_retention": "DEFERRED", "error_type": type(error).__name__}), file=sys.stderr)
    try:
        result = retain(root)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        # Machine contract (E2.T6): a JSON status line (parsed by tests and reports); stays print.
        print(json.dumps({"log_retention": "DEFERRED", "error_type": type(error).__name__}), file=sys.stderr)
        return
    if result["rotated"] or result["removed"]:
        # Machine contract (E2.T6): a JSON status line (parsed by tests and reports); stays print.
        # Only when something was rotated or removed, and as output: files skipped because a running
        # job still writes them are normal, and this line used to fill the Errors logs every run
        # (owner, 2026-10-27). A clean-up that could not run (DEFERRED above) stays an error.
        print(json.dumps({"log_retention": result}))


def print_result(result, report_path):
    # Machine contract (E2.T6): a JSON status line (parsed by tests and reports); stays print.
    print(json.dumps({"state": result.get("state"), "report": str(report_path)}, separators=(",", ":")))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    arguments = parser.parse_args()
    # Machine contract (E2.T6): a JSON status line (parsed by tests and reports); stays print.
    print(json.dumps(retain(arguments.root), sort_keys=True))
