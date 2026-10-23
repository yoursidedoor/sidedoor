"""A SingletonLock left by a Chrome that died on this machine is stale: cleared, never "window open"."""
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_home  # noqa: E402,F401 (a temporary SIDEDOOR_HOME, never the real one)
import browser_runtime  # noqa: E402
import portable  # noqa: E402
import setup_steps  # noqa: E402


def dead_pid():
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


@unittest.skipIf(portable.WINDOWS, "SingletonLock is a POSIX symlink")
class StaleSingletonLock(unittest.TestCase):
    def profile(self, host, pid):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(directory)]))
        profile = directory / "profile"
        profile.mkdir()
        os.symlink("%s-%d" % (host, pid), profile / "SingletonLock")
        os.symlink("/nonexistent/socket", profile / "SingletonSocket")
        os.symlink("12345", profile / "SingletonCookie")
        return profile

    def links(self, profile):
        return [name for name in setup_steps.SINGLETONS if os.path.lexists(profile / name)]

    def test_a_dead_pid_on_this_host_is_stale_everywhere(self):
        pid = dead_pid()
        ps = mock.Mock(side_effect=AssertionError("a dead pid needs no ps"))
        profile = self.profile(socket.gethostname(), pid)
        self.assertFalse(browser_runtime.profile_in_use({"browser_driver": {"profile": str(profile)}}, run=ps))
        self.assertEqual(self.links(profile), [])
        profile = self.profile(socket.gethostname(), pid)
        browser_runtime.prepare_profile(profile, run=ps)        # no BrowserProfileBusy
        self.assertEqual(self.links(profile), [])
        profile = self.profile(socket.gethostname(), pid)
        self.assertFalse(setup_steps.lock_holder_alive(str(profile)))
        self.assertEqual(self.links(profile), [])

    def test_a_live_chrome_on_this_profile_is_in_use(self):
        profile = self.profile(socket.gethostname(), os.getpid())
        holds = lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, "Google Chrome --user-data-dir=%s\n" % profile, "")
        self.assertTrue(browser_runtime.profile_in_use({"browser_driver": {"profile": str(profile)}}, run=holds))
        with self.assertRaises(browser_runtime.BrowserProfileBusy):
            browser_runtime.prepare_profile(profile, run=holds)
        self.assertTrue(setup_steps.lock_holder_alive(str(profile)))
        self.assertEqual(len(self.links(profile)), 3)

    def test_another_host_with_no_live_chrome_on_this_profile_is_not_in_use(self):
        # macOS renames the host with the network, so a lock naming another host is judged by its pid.
        profile = self.profile("some-other-host", dead_pid())
        ps = lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "", "")
        self.assertFalse(browser_runtime.profile_in_use({"browser_driver": {"profile": str(profile)}}, run=ps))
        browser_runtime.prepare_profile(profile, run=ps)
        self.assertEqual(self.links(profile), [])

if __name__ == "__main__":
    unittest.main()
