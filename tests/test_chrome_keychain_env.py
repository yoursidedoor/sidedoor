"""Chrome finds the user's real login keychain: the browser server gets the parent's session variables."""
import io
import json
import os
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_home  # noqa: E402,F401 (a temporary SIDEDOOR_HOME, never the real one)

import browser_guard  # noqa: E402
import browser_runtime  # noqa: E402
import user_profile  # noqa: E402

PARENT = {"HOME": "/Users/alex", "USER": "alex", "LOGNAME": "alex", "TMPDIR": "/var/folders/xy/T/",
          "__CF_USER_TEXT_ENCODING": "0x1F5:0x0:0x0", "PATH": "/usr/bin",
          "CODEX_API_KEY": "sk-secret", "OPENROUTER_API_KEY": "sk-or-secret", "OPENAI_API_KEY": "sk-x"}
SECRETS = ("CODEX_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY")


def server_env(options):
    line = next(option for option in options if option.startswith("mcp_servers.browser.env="))
    return tomllib.loads("env=" + line.split("=", 1)[1])["env"]


class BrowserServerEnvironment(unittest.TestCase):
    def test_server_env_carries_the_parent_session_variables_and_no_secret(self):
        home = os.environ[user_profile.HOME_VARIABLE]
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, dict(PARENT, **{user_profile.HOME_VARIABLE: home}), clear=True):
            env = server_env(browser_runtime.codex_options({}, directory, "inspect"))
        for name in ("HOME", "USER", "LOGNAME", "TMPDIR", "__CF_USER_TEXT_ENCODING"):
            self.assertEqual(PARENT[name], env[name])
        self.assertEqual(str(user_profile.home()), env[user_profile.HOME_VARIABLE])
        for name in SECRETS + ("PATH",):
            self.assertNotIn(name, env)
        self.assertFalse([value for value in env.values() if "secret" in value])

    def test_windows_profile_variables_pass_and_unset_ones_are_left_out(self):
        windows = {"USERPROFILE": r"C:\Users\alex", "APPDATA": r"C:\Users\alex\AppData\Roaming",
                   "LOCALAPPDATA": r"C:\Users\alex\AppData\Local", "TEMP": r"C:\Temp", "CODEX_API_KEY": "k"}
        self.assertEqual({key: windows[key] for key in ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP")},
                         browser_runtime.session_environment(windows))


class GuardForwardsEnvironment(unittest.TestCase):
    def test_the_node_child_gets_the_guard_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "env.json"
            child = [sys.executable, "-c", "import json, os, sys; json.dump(dict(os.environ), open(sys.argv[1], 'w'));"
                     " sys.stdin.read()", str(out)]
            session = {name: PARENT[name] for name in ("HOME", "USER", "LOGNAME", "TMPDIR", "__CF_USER_TEXT_ENCODING")}
            with mock.patch.dict(os.environ, session, clear=False):
                proxy = browser_guard.Proxy("inspect", child, stdin=io.StringIO(""), stdout=io.StringIO())
                proxy.stop_child(grace=5)
            seen = json.loads(out.read_text())
        for name, value in session.items():
            self.assertEqual(value, seen[name])


if __name__ == "__main__":
    unittest.main()
