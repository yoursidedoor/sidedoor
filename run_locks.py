"""Process locks shared by the scheduled jobs (owner, 2026-10-02).

Owner, 2026-10-02, on the hourly cycle that spent 10:23-11:36 in applications and so skipped the
11:00 cycle: "1. Run applications as their own job, like research, with its own schedule and
lock. Outreach, replies and the inbox would then run every hour regardless. The contact finder
and applications both use the same Chrome browser, so the two would need to take turns on it."
- "I like 1. lets not do 2." (2 was a per-cycle time cap on applications.)

Three lock files under state/, each taken with flock and O_NOFOLLOW (on Windows msvcrt.locking on
byte 0 of a handle, refusing links; `portable`, E2.T3):
  runner.lock        the hourly cycle (`orchestrator.py run`) and the daily research job, as before
  applications.lock  the applications job (`orchestrator.py applications`), the only runner of
                     application work
  browser.lock       one Chrome browser session at a time (an application session, or one contact
                     finder read), held for the session only, never for a whole job

Installs and tools that change application records take runner.lock AND
applications.lock (`held(root, RUNNER, APPLICATIONS)`), all or nothing, so they never hold one
lock while waiting on the other.
"""

import contextlib
import os
from pathlib import Path
import sqlite3
import stat
import threading
import time

import portable
import paths


RUNNER = "runner.lock"
APPLICATIONS = "applications.lock"
BROWSER = "browser.lock"
# launchd label prefix of Sidedoor's one login entry (login_item.LABEL, com.yoursidedoor.sidedoor,
# E5.T2); a process whose XPC_SERVICE_NAME starts with it was started by launchd (the job children
# of a login-started Sidedoor inherit it).
LAUNCHD_LABEL_PREFIX = "com.yoursidedoor."

# Two processes now write the one database (the hourly cycle and the applications job). Every
# connection that writes waits this long for the other's write transaction instead of failing
# with "database is locked" after the old 30 seconds. No step holds a write transaction across a
# model call or a browser session; this only covers ordinary bookkeeping transactions.
BUSY_TIMEOUT_SECONDS = 120

# The application session's own short-lived servers write the database inside an MCP tool call,
# which codex times out (the arm tool after 15 s, the human-check tool 30 s past its wait). Their
# lock wait stays well under that, so a busy database gives the clean "never armed" path instead
# of a tool timeout after which the write may still land.
SESSION_TOOL_BUSY_TIMEOUT_SECONDS = 10

# How long an application browser session waits for the browser while a contact-finder read
# holds it: one finder session (linkedin_contacts.COMPUTE_SECONDS, 900 s) plus a margin. After
# that the application waits as a capacity wait (no try spent, no request created).
APPLICATION_BROWSER_WAIT_SECONDS = 1200


class WriteFirstConnection(sqlite3.Connection):
    """A connection whose outermost SAVEPOINT takes the write lock first.

    With two writing processes (owner, 2026-10-02) a busy timeout is not enough on its own. A
    SAVEPOINT opened outside a transaction starts a DEFERRED one; if it reads and then writes
    after the other process has committed, SQLite refuses at once with "database is locked"
    (a stale WAL snapshot cannot be upgraded, and the busy timeout is never consulted). Seen in
    the offline two-process rehearsal (at a step since removed). So an outermost SAVEPOINT is preceded by
    BEGIN IMMEDIATE (which does wait out the busy timeout), and the release of that outermost
    savepoint commits, exactly as releasing an outermost savepoint did before. Nested savepoints
    and every other statement pass through unchanged; Python's own implicit transactions already
    begin at their first write.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._savepoints = None  # names opened since this connection's own BEGIN IMMEDIATE

    def execute(self, sql, *args, **kwargs):
        words = sql.split(None, 3) if isinstance(sql, str) else []
        verb = words[0].upper() if words else ""
        if verb == "SAVEPOINT" and len(words) == 2:
            if not self.in_transaction:
                super().execute("BEGIN IMMEDIATE")
                self._savepoints = []
            if self._savepoints is not None:
                self._savepoints.append(words[1])
            return super().execute(sql, *args, **kwargs)
        if verb == "RELEASE" and self._savepoints is not None:
            name = words[2] if len(words) > 2 and words[1].upper() == "SAVEPOINT" else words[1] if len(words) > 1 else None
            cursor = super().execute(sql, *args, **kwargs)
            if name in self._savepoints:
                # RELEASE x releases x and every savepoint opened after it.
                del self._savepoints[len(self._savepoints) - 1 - self._savepoints[::-1].index(name):]
            if not self._savepoints:
                self._savepoints = None
                if self.in_transaction:
                    self.commit()
            return cursor
        return super().execute(sql, *args, **kwargs)

    def commit(self):
        self._savepoints = None
        return super().commit()

    def rollback(self):
        self._savepoints = None
        return super().rollback()


def _open(root, name):
    directory = paths.state(root)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = portable.open_lock(directory / name)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(descriptor)
        raise RuntimeError("UNSAFE_LOCK_FILE: " + name)
    return descriptor


def _take(descriptor):
    return portable.try_lock(descriptor)


@contextlib.contextmanager
def held(root, *names, wait_seconds=0, poll_seconds=2):
    """Hold every named lock, all or none. Yields True when held, False when they could not all
    be taken within `wait_seconds` (0: one non-blocking try). Never holds one lock while waiting
    for another."""
    descriptors = []
    try:
        for name in names:
            descriptors.append(_open(root, name))
        deadline = time.monotonic() + max(0, wait_seconds)
        while True:
            taken = []
            for descriptor in descriptors:
                if not _take(descriptor):
                    break
                taken.append(descriptor)
            if len(taken) == len(descriptors):
                break
            for descriptor in taken:
                portable.unlock(descriptor)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(poll_seconds, remaining))
        if len(taken) != len(descriptors):
            yield False
            return
        try:
            yield True
        finally:
            for descriptor in descriptors:
                portable.unlock(descriptor)
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


_local = threading.local()


@contextlib.contextmanager
def browser_session(root, wait_seconds=0):
    """Hold state/browser.lock for one Chrome session. Yields True when held.

    Re-entrant within one thread for the same root: the application adapter takes it before it
    reserves compute and creates the request, and `application_browser.consume` (which also takes
    it, for callers that reach it directly) then runs inside the same hold.
    """
    key = str(Path(root).resolve())
    current = getattr(_local, "browser", None)
    if current is None:
        current = _local.browser = {}
    if current.get(key):
        current[key] += 1
        try:
            yield True
        finally:
            current[key] -= 1
        return
    with held(root, BROWSER, wait_seconds=wait_seconds) as acquired:
        if not acquired:
            yield False
            return
        current[key] = 1
        try:
            yield True
        finally:
            current.pop(key, None)
