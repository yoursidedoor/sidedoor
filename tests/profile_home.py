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


# E2.T4 (C1): no test ever touches a real keychain or Credential Manager. The OS store is off in
# this process through credentials.use_backend (no environment change can undo it; the keys stay in
# 0600 files, as with no store, K4), and through the environment for every child it starts. The
# real backends also refuse while unittest is loaded. Tests of the store install a fake.
os.environ["SIDEDOOR_CREDENTIAL_STORE"] = "file"
import credentials  # noqa: E402
credentials.use_backend(credentials.NO_STORE)

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
# No test starts a real Chrome in the background (browser_runtime.background_launch); the tests of
# that launch patch this back on and fake the `open` call.
browser_runtime.BACKGROUND_ALLOWED = False

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


# Windows (VM pass, 2026-10-23): a file that is still open cannot be deleted there. A temporary
# folder's removal first collects garbage (a sqlite3 connection only reachable through a reference
# cycle, e.g. a caught exception's traceback, is closed only then) and retries for up to 2 seconds
# (an antivirus or indexer briefly holding a new file); a file a test really leaves open still fails.
if os.name == "nt":
    import gc
    import time

    _real_rmtree = tempfile.TemporaryDirectory._rmtree.__func__

    def _rmtree_windows(cls, name, ignore_errors=False, repeated=False):
        for attempt in range(20):
            try:
                return _real_rmtree(cls, name, ignore_errors=ignore_errors, repeated=repeated)
            except PermissionError:
                if attempt == 19:
                    raise
                gc.collect()
                time.sleep(0.1)

    tempfile.TemporaryDirectory._rmtree = classmethod(_rmtree_windows)
