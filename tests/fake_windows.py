"""Fakes that let `portable`'s Windows branch run on macOS (E2.T3; Windows CI comes in E2.T2).

FakeMsvcrt.locking keeps msvcrt's contract (byte 0 at the handle's position, LK_NBLCK raises
OSError EACCES when another handle holds it, LK_UNLCK) and implements it with flock on the same
descriptor: flock is per open file description, as LockFile is per handle, and the kernel
releases both when the holder dies. The real per-handle behaviour is proven on the Windows VM.

FakeKernel32 records every call and answers from a small table, for the Job Object, thread
resume, OpenProcess and SetErrorMode plumbing.
"""

import contextlib
import errno
import os
from unittest import mock

try:
    import fcntl
except ImportError:  # on Windows the real msvcrt is used instead
    fcntl = None

import portable


class FakeMsvcrt:
    LK_UNLCK = 0
    LK_LOCK = 1
    LK_NBLCK = 2
    calls = []

    @classmethod
    def locking(cls, descriptor, mode, nbytes):
        cls.calls.append((mode, nbytes, os.lseek(descriptor, 0, os.SEEK_CUR)))
        if nbytes != 1 or os.lseek(descriptor, 0, os.SEEK_CUR) != 0:
            raise AssertionError("portable must lock exactly byte 0")
        if mode == cls.LK_NBLCK:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise PermissionError(errno.EACCES, "Permission denied") from None
        elif mode == cls.LK_UNLCK:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        else:
            raise AssertionError("unexpected locking mode %r" % mode)


@contextlib.contextmanager
def windows(kernel32=None, last_error=0):
    """Take portable's Windows branch for the duration."""
    FakeMsvcrt.calls = []
    locking = FakeMsvcrt if fcntl is not None else portable.msvcrt     # the real one on Windows
    with mock.patch.object(portable, "WINDOWS", True), mock.patch.object(portable, "msvcrt", locking), \
            mock.patch.object(portable, "_kernel32", lambda: kernel32), \
            mock.patch.object(portable, "_get_last_error", lambda: last_error), \
            mock.patch.object(portable, "_error_mode_set", False):
        yield


def install():
    """For a child process: take the Windows branch for the rest of its life."""
    portable.WINDOWS = True
    portable.msvcrt = FakeMsvcrt


class FakeKernel32:
    def __init__(self, threads=(), exit_code=portable.STILL_ACTIVE, open_process=0x50, fail=None):
        self.calls = []
        self.threads = list(threads)          # (thread id, owner pid)
        self.exit_code = exit_code
        self.open_process = open_process
        self.fail = fail or set()
        self.limits = None
        self.next_handle = 0x100

    def _handle(self):
        self.next_handle += 4
        return self.next_handle

    def _record(self, name, *args):
        self.calls.append((name,) + args)
        return name not in self.fail

    def names(self):
        return [call[0] for call in self.calls]

    def CreateJobObjectW(self, attributes, name):
        return self._handle() if self._record("CreateJobObjectW") else 0

    def SetInformationJobObject(self, job, kind, address, size):
        import ctypes
        self.limits = portable._structures()["limits"].from_address(address).BasicLimitInformation.LimitFlags
        return self._record("SetInformationJobObject", job, kind, size == ctypes.sizeof(portable._structures()["limits"]))

    def AssignProcessToJobObject(self, job, process):
        return self._record("AssignProcessToJobObject", job, process)

    def TerminateJobObject(self, job, code):
        return self._record("TerminateJobObject", job, code)

    def CloseHandle(self, handle):
        return self._record("CloseHandle", handle)

    def CreateToolhelp32Snapshot(self, flags, pid):
        self._record("CreateToolhelp32Snapshot", flags)
        self._cursor = 0
        return 0x77

    def _fill(self, pointer):
        if self._cursor >= len(self.threads):
            return False
        entry = pointer._obj
        entry.th32ThreadID, entry.th32OwnerProcessID = self.threads[self._cursor]
        self._cursor += 1
        return True

    def Thread32First(self, snapshot, pointer):
        self._record("Thread32First")
        return self._fill(pointer)

    def Thread32Next(self, snapshot, pointer):
        return self._fill(pointer)

    def OpenThread(self, access, inherit, thread):
        return thread + 0x1000 if self._record("OpenThread", access, thread) else 0

    def ResumeThread(self, thread):
        return 1 if self._record("ResumeThread", thread) else 0xFFFFFFFF

    def OpenProcess(self, access, inherit, pid):
        self._record("OpenProcess", access, pid)
        return self.open_process

    def GetExitCodeProcess(self, handle, pointer):
        pointer._obj.value = self.exit_code
        return self._record("GetExitCodeProcess", handle)

    def SetErrorMode(self, mode):
        self._record("SetErrorMode", mode)
        return 0
