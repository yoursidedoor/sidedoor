"""W1 a (owner, 2026-10-23): on Windows every Sidedoor process runs in Python UTF-8 mode, so a profile
or résumé with characters outside the ANSI code page (cp1252) is written and read back intact, with
no PYTHONUTF8 in the environment."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

import portable
import scheduler_loop
import user_profile

ROOT = Path(__file__).resolve().parents[1]
NAME = "Łódź – 東京"   # Ł and 東京 are outside cp1252


def child_code(guard):
    return textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(ROOT)!r})
        import portable
        if {guard!r}:
            code = portable.ensure_utf8_mode()
            if code is not None:
                sys.exit(code)
        import resume_intake, user_profile
        resume_intake._write_profile({{"name": {NAME!r}}})
        import json
        profile = json.loads(user_profile.profile_path().read_text())
        print(sys.flags.utf8_mode, profile["name"] == {NAME!r})
    """)


class Utf8ModeTest(unittest.TestCase):
    def run_child(self, argv):
        home = Path(tempfile.mkdtemp(prefix="utf8 home "))
        self.addCleanup(__import__("shutil").rmtree, home, True)
        environment = {key: value for key, value in os.environ.items() if key != "PYTHONUTF8"}
        environment[user_profile.HOME_VARIABLE] = str(home)
        done = subprocess.run(argv, env=environment, capture_output=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
        saved = json.loads((home / "profile.json").read_bytes().decode("utf-8"))
        self.assertEqual(saved["name"], NAME)
        return done.stdout.decode("ascii", "replace").split()

    @unittest.skipUnless(os.name == "nt", "Windows only")
    def test_a_child_started_like_a_job_round_trips_a_non_cp1252_profile(self):
        self.assertEqual(scheduler_loop.job_command("run")[1:3], ["-X", "utf8"])
        self.assertEqual(self.run_child([sys.executable, *portable.UTF8_FLAGS, "-c", child_code(False)]), ["1", "True"])

    @unittest.skipUnless(os.name == "nt", "Windows only")
    def test_the_entry_guard_runs_a_hand_started_process_again_in_utf8_mode(self):
        self.assertEqual(self.run_child([sys.executable, "-c", child_code(True)]), ["1", "True"])

    def test_nothing_changes_elsewhere(self):
        if os.name == "nt":
            self.skipTest("POSIX only")
        self.assertEqual(portable.UTF8_FLAGS, ())
        self.assertIsNone(portable.ensure_utf8_mode())
        self.assertNotIn("-X", scheduler_loop.job_command("run"))


if __name__ == "__main__":
    unittest.main()
