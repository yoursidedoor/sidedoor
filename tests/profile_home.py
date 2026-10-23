"""Point user_profile at a temporary SIDEDOOR_HOME populated from profile.example.json.

Importing this module gives the test process a profile home holding the example user's
profile.json (the repo's default settings.json applies), and points the optional owner seed
(owner_seed.py) at the fictional tests/fixtures/owner-seed.json. A test that needs a different policy
calls `use(self, policy)`: a fresh home with that merged policy split into profile.json and
settings.json, restored when the test ends.

Every test starts on the example home: the manual_* fixtures some tests reuse point
SIDEDOOR_HOME at a home inside their own temporary root (user_profile.use_policy) and never
point it back, so `TestCase.run` is wrapped to reset it before each test.
"""

import atexit
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import browser_runtime
import owner_seed
import paths
import setup_steps
import user_profile


HOME = Path(tempfile.mkdtemp(prefix="sidedoor-test-home-"))
atexit.register(shutil.rmtree, HOME, True)
shutil.copyfile(user_profile.EXAMPLE_PROFILE, HOME / "profile.json")
os.environ[user_profile.HOME_VARIABLE] = str(HOME)
# No code-folder data to move into a test home (paths.migrate): never this checkout's own state/.
os.environ[paths.LEGACY_VARIABLE] = ""
SEED = Path(__file__).resolve().parent / "fixtures" / "owner-seed.json"
os.environ[owner_seed.VARIABLE] = str(SEED)

# Sidedoor's browser runtime (E7.T1) as empty stand-ins, and Sidedoor's Chrome profile inside the
# temporary home: tests run sessions through fake codex executables, never Node or Chrome, and never
# touch ~/.config/sidedoor/browser-profile. A test that needs the runtime missing, or the real one
# (tests/browser_fixtures.py), sets config["browser_driver"] itself.
# The real runtime this machine has, if any, for the real-browser tests (tests/browser_fixtures.py).
REAL_RUNTIME = os.environ.get(browser_runtime.RUNTIME_VARIABLE)
RUNTIME = HOME / "browser-runtime"
for stand_in in (RUNTIME / "node" / ("node.exe" if os.name == "nt" else "bin/node"), RUNTIME / browser_runtime.MCP_CLI):
    stand_in.parent.mkdir(parents=True, exist_ok=True)
    stand_in.write_text("")
os.environ[browser_runtime.RUNTIME_VARIABLE] = str(RUNTIME)
setup_steps.BROWSER_PROFILE = HOME / "browser-profile"

_run = unittest.TestCase.run


def _run_on_example_home(self, result=None):
    os.environ[user_profile.HOME_VARIABLE] = str(HOME)
    os.environ[owner_seed.VARIABLE] = str(SEED)
    os.environ[paths.LEGACY_VARIABLE] = ""
    return _run(self, result)


unittest.TestCase.run = _run_on_example_home


def use(testcase, policy):
    """Make `policy` (one merged dict) the loaded policy until `testcase` ends."""
    home = Path(tempfile.mkdtemp(prefix="sidedoor-test-home-"))
    testcase.addCleanup(shutil.rmtree, home, True)
    user_profile.write_home(policy, home)
    patcher = mock.patch.dict(os.environ, {user_profile.HOME_VARIABLE: str(home)})
    patcher.start()
    testcase.addCleanup(patcher.stop)
    return home
