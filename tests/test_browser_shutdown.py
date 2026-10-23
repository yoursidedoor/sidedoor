"""SIGTERM must reap the browser child instead of orphaning it.

Before 2026-09-22 the application path installed no signal handlers, so Python's default
SIGTERM action killed the process without running `execute`'s `finally` - leaving the model
subprocess and the Chrome it drives alive, still holding a private claim, still able to arm.
Job state survived that (the guard writes `armed_at` itself) but the machine did not.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import inspect
import signal
import tempfile
import os
import pathlib
import unittest
from unittest.mock import patch

import application_browser


class ReapTests(unittest.TestCase):
    def setUp(self):
        application_browser._LIVE_WORKER_GROUPS.clear()
        self.addCleanup(application_browser._LIVE_WORKER_GROUPS.clear)

    @unittest.skipIf(os.name == "nt", "POSIX-only (E2.T3: process groups, rlimits, flock, lsof or symlinks)")
    def test_reaping_kills_every_registered_group_and_empties_the_registry(self):
        application_browser._LIVE_WORKER_GROUPS.update({4242, 4243})
        killed = []
        with patch.object(application_browser.os, "killpg", side_effect=lambda pid, sig: killed.append((pid, sig))):
            application_browser.reap_live_workers()
        self.assertEqual(sorted(pid for pid, _ in killed), [4242, 4243])
        self.assertTrue(all(sig == signal.SIGKILL for _, sig in killed))
        self.assertEqual(application_browser._LIVE_WORKER_GROUPS, set())

    @unittest.skipIf(os.name == "nt", "POSIX-only (E2.T3: process groups, rlimits, flock, lsof or symlinks)")
    def test_reaping_never_raises_when_the_child_is_already_gone(self):
        application_browser._LIVE_WORKER_GROUPS.add(4242)
        with patch.object(application_browser.os, "killpg", side_effect=ProcessLookupError):
            application_browser.reap_live_workers()          # must not raise
        self.assertEqual(application_browser._LIVE_WORKER_GROUPS, set())

    @unittest.skipIf(os.name == "nt", "POSIX-only (E2.T3: process groups, rlimits, flock, lsof or symlinks)")
    def test_execute_registers_its_worker_group_and_discards_it_afterwards(self):
        source = inspect.getsource(application_browser.execute)
        self.assertIn("_LIVE_WORKER_GROUPS.add(worker.pid)", source)
        self.assertIn("_LIVE_WORKER_GROUPS.discard(worker.pid)", source)

    @unittest.skipIf(os.name == "nt", "POSIX-only (E2.T3: process groups, rlimits, flock, lsof or symlinks)")
    def test_the_worker_reaching_popen_is_its_own_session(self):
        """killpg only reaches the whole tree if the child is its own session: the kwargs that
        actually reach Popen must carry start_new_session=True (portable.worker_spawn_options)."""
        seen = {}

        class Stop(Exception):
            pass

        def fake_popen(*args, **kwargs):
            seen.update(kwargs)
            raise Stop()

        with tempfile.TemporaryDirectory() as root, \
                patch.object(application_browser.browser_runtime, "require"), \
                patch.object(application_browser.browser_runtime, "settings", return_value={"isolated": True}), \
                patch.object(application_browser, "command", return_value=["codex"]), \
                patch.object(application_browser.reply_worker, "_environment", return_value={}), \
                patch.object(application_browser.subprocess, "Popen", side_effect=fake_popen), \
                self.assertRaises(Stop):
            application_browser.execute({}, "prompt", pathlib.Path(root), 5)
        self.assertIs(seen.get("start_new_session"), True,
                      "killpg only reaches the whole tree if the child is its own session")

    def test_consume_installs_and_restores_shutdown_handlers(self):
        source = inspect.getsource(application_browser.consume)
        self.assertIn("signal.SIGTERM", source)
        self.assertIn("reap_live_workers()", source)
        self.assertIn("APPLICATION_SHUTDOWN_INTERRUPTED", source)
        self.assertIn("signal.signal(signum, handler)", source,
                      "handlers must be restored or they leak into the rest of the cycle")

    def test_a_rejected_config_installs_no_handler_to_leak(self):
        """Bounds are checked before any handler is installed, so an invalid config cannot
        leave a SIGTERM handler behind that would raise InterruptedError from anywhere."""
        before = signal.getsignal(signal.SIGTERM)
        # A real (empty) root: consume takes state/browser.lock before the bounds check (2026-10-02).
        with tempfile.TemporaryDirectory() as root, self.assertRaises(ValueError):
            application_browser.consume(root, None,
                                        {"application_browser": {"timeout_seconds": 0}}, "x")
        self.assertIs(signal.getsignal(signal.SIGTERM), before)

    def test_the_restore_runs_in_a_finally_so_no_path_leaks_a_handler(self):
        source = inspect.getsource(application_browser.consume)
        body = source[source.index("shutdown_handlers = {}"):]
        self.assertIn("finally:", body)
        restore = body.index("signal.signal(signum, handler)")
        self.assertLess(body.index("finally:", body.index("except Exception")), restore,
                        "the restore must sit in a finally after the except, not on the happy path")


if __name__ == "__main__":
    unittest.main()
