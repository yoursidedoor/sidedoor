"""K2 a: each Sidedoor home has its own browser profile, `<home>/browser-profile`, read at use time;
an existing user's profile (default or legacy home) stays at ~/.config/sidedoor/browser-profile."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import browser_runtime
import setup_steps
import user_profile

NO_HOME = {user_profile.HOME_VARIABLE: "", user_profile.LEGACY_HOME_VARIABLE: ""}


class SetHome(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "other-home"
        self.home.mkdir()
        patcher = mock.patch.dict(os.environ, {user_profile.HOME_VARIABLE: str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.expected = self.home / "browser-profile"

    def test_every_consumer_uses_the_home_profile(self):
        self.assertEqual(user_profile.browser_profile(), self.expected)
        self.assertEqual(browser_runtime.profile({}), self.expected)               # live view, sessions
        self.assertEqual(setup_steps.BrowserSession(self.home).profile, self.expected)   # setup window
        mcp = browser_runtime.mcp_command({}, Path(self.temp.name) / "session", hidden=True)
        self.assertNotIn("--user-data-dir", mcp)   # hidden: the guard gets it with --background-chrome
        with mock.patch.object(browser_runtime, "background_launch", return_value=False):
            argv = browser_runtime.guard_command({}, Path(self.temp.name) / "session", "webmail")
        self.assertEqual(argv[argv.index("--user-data-dir") + 1], str(self.expected))   # browser guard argv
        seen = []
        setup_steps.run_email_check(self.home, read=lambda url, codex, profile: seen.append(profile) or None)
        self.assertEqual(seen, [self.expected])                                    # email check

    def test_read_at_use_time_not_import(self):
        later = Path(self.temp.name) / "later"
        session = setup_steps.BrowserSession(self.home)
        with mock.patch.dict(os.environ, {user_profile.HOME_VARIABLE: str(later)}):
            self.assertEqual(session.profile, later / "browser-profile")
            self.assertEqual(browser_runtime.profile({}), later / "browser-profile")


class ExistingUser(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.user = Path(self.temp.name)
        self.unchanged = self.user / ".config" / "sidedoor" / "browser-profile"
        patcher = mock.patch.object(Path, "home", return_value=self.user)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_unset_home_keeps_the_default_profile(self):
        (self.user / ".config" / "sidedoor").mkdir(parents=True)
        with mock.patch.dict(os.environ, NO_HOME):
            self.assertEqual(user_profile.browser_profile(), self.unchanged)
            self.assertEqual(browser_runtime.profile({}), self.unchanged)
        # The dashboard sets SIDEDOOR_HOME to the home it resolved: still the same profile.
        with mock.patch.dict(os.environ, {user_profile.HOME_VARIABLE: str(self.user / ".config" / "sidedoor")}):
            self.assertEqual(user_profile.browser_profile(), self.unchanged)

    def test_legacy_home_keeps_its_profile(self):
        legacy = self.user / ".config" / "jobagent"
        legacy.mkdir(parents=True)
        with mock.patch.dict(os.environ, NO_HOME):
            self.assertEqual(user_profile.home(), legacy)
            self.assertEqual(user_profile.browser_profile(), self.unchanged)
        with mock.patch.dict(os.environ, {user_profile.HOME_VARIABLE: str(legacy)}):
            self.assertEqual(user_profile.browser_profile(), self.unchanged)


if __name__ == "__main__":
    unittest.main()
