"""version.py and pyproject.toml's [project] version must agree (E9.T3 UD1)."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import tomllib
import unittest
from pathlib import Path

import update_check
import version

ROOT = Path(__file__).resolve().parent.parent


class VersionTest(unittest.TestCase):
    def test_matches_pyproject(self):
        with open(ROOT / "pyproject.toml", "rb") as handle:
            self.assertEqual(version.current(), tomllib.load(handle)["project"]["version"])

    def test_is_plain_release_and_used_by_update_check(self):
        self.assertIsNotNone(update_check.parse_version(version.current()))
        self.assertEqual(update_check.installed_version(), version.current())


if __name__ == "__main__":
    unittest.main()
