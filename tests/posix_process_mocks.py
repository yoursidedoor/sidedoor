"""Mocked worker processes on Windows (VM pass, 2026-10-23).

Tests that replace `subprocess.Popen` with a fake process check the worker's orchestration, not
process control. On Windows `portable.contain` puts the real process in a Job Object (it needs the
Popen handle a fake has not got) and `portable.kill_tree` ends that job instead of calling
`os.killpg`. `simulate(test)` gives those tests the POSIX behaviour their fakes were written for:
`contain` does nothing for a fake and `kill_tree` is `os.killpg(pid, SIGKILL)` (which the tests
patch), with ProcessLookupError ignored as on POSIX. Nothing changes on macOS or Linux."""
import os
import signal
from unittest.mock import patch

import portable

SIGKILL = getattr(signal, "SIGKILL", 9)   # Windows has no SIGKILL; the patched killpg only records it


def simulate(test):
    """`test`: a TestCase (enterContext) or an ExitStack (enter_context). A real process (a Popen
    with a handle) still gets the real Job Object, so a test that starts one never leaks it."""
    if os.name != "nt":
        return
    real_contain, real_kill_tree = portable.contain, portable.kill_tree

    def contain(worker):
        if hasattr(worker, "_handle") and isinstance(getattr(worker, "_handle"), int):
            real_contain(worker)

    def kill_tree(pid):
        if pid in portable._jobs:
            real_kill_tree(pid)
            return
        try:
            os.killpg(pid, SIGKILL)
        except (ProcessLookupError, AttributeError):   # AttributeError: a test that did not patch killpg
            pass

    enter = getattr(test, "enterContext", None) or test.enter_context
    enter(patch.object(portable, "contain", contain))
    enter(patch.object(portable, "kill_tree", kill_tree))
