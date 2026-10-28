import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import log_retention


class LogRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / "logs").mkdir()
        (self.root / "state").mkdir()

    @unittest.skipIf(os.name == "nt", "POSIX-only (E2.T3: process groups, rlimits, flock, lsof or symlinks)")
    def test_failed_inventory_never_removes_logs(self):
        path = self.root / "logs/research.stdout.log"
        path.write_bytes(b"important" * 50)
        result = subprocess.CompletedProcess([], 1, "", "permission denied")
        with mock.patch.object(log_retention.subprocess, "run", return_value=result):
            with self.assertRaises(RuntimeError):
                log_retention.retain(self.root, rotate_bytes=1)
        self.assertEqual(path.read_bytes(), b"important" * 50)
        self.assertEqual(len(list((self.root / "logs").iterdir())), 1)

    def test_open_archive_survives_age_and_byte_limits(self):
        path = self.root / "logs" / ("research.stdout.log.retained." + "0" * 20 + "." + "a" * 32)
        path.write_bytes(b"still writing")
        os.utime(path, (0, 0))
        with path.open("ab"):
            outcome = log_retention.retain(self.root, retain_bytes=1, retain_seconds=1)
            self.assertEqual(outcome["skipped_open"], 1)
            self.assertTrue(path.exists())
        self.assertEqual(log_retention.retain(self.root, retain_bytes=1)["removed"], 1)

    def test_expired_worker_log_takes_its_engine_record(self):
        name = "0" * 8 + "-0000-0000-0000-" + "0" * 12 + ".ai.1.discovery.log"
        path, record = self.root / "logs" / name, self.root / "logs" / (name + ".engine.json")
        path.write_text("old")
        record.write_text('{"engine": "codex"}')
        os.utime(path, (0, 0))
        self.assertEqual(log_retention.retain(self.root, retain_seconds=1)["removed"], 1)
        self.assertFalse(path.exists() or record.exists())

    def test_new_archive_gets_one_sweep_grace(self):
        path = self.root / "logs/research.stdout.log"
        path.write_bytes(b"history" * 50)
        outcome = log_retention.retain(self.root, rotate_bytes=1, retain_bytes=1)
        self.assertEqual(outcome["rotated"], 1)
        archives = list((self.root / "logs").glob("*.retained.*"))
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].read_bytes(), b"history" * 50)
        self.assertEqual(log_retention.retain(self.root, retain_bytes=1)["removed"], 1)

    def test_archive_count_preserves_newest(self):
        paths = []
        for index in range(5):
            path = self.root / "logs" / (f"research.stdout.log.retained.{index:020d}." + "b" * 32)
            path.write_bytes(b"archive")
            os.utime(path, (time.time() - 10 + index, time.time() - 10 + index))
            paths.append(path)
        self.assertEqual(log_retention.retain(self.root)["removed"], 2)
        self.assertEqual([path.exists() for path in paths], [False, False, True, True, True])

    def test_startup_failure_is_redacted_and_does_not_stop_business_runner(self):
        output = io.StringIO()
        with mock.patch.object(log_retention, "retain", side_effect=OSError("private detail")):
            with contextlib.redirect_stderr(output):
                log_retention.startup(self.root)
        self.assertEqual(json.loads(output.getvalue()), {"log_retention": "DEFERRED", "error_type": "OSError"})

    def test_a_clean_up_that_only_skipped_open_files_says_nothing(self):
        errors, output = io.StringIO(), io.StringIO()
        with mock.patch.object(log_retention, "retain", return_value={"state": "COMPLETE", "rotated": 0, "removed": 0, "skipped_open": 13}):
            with contextlib.redirect_stderr(errors), contextlib.redirect_stdout(output):
                log_retention.startup(self.root)
        self.assertEqual((errors.getvalue(), output.getvalue()), ("", ""))

    def test_a_clean_up_that_did_something_is_output_not_an_error(self):
        errors, output = io.StringIO(), io.StringIO()
        result = {"state": "COMPLETE", "rotated": 1, "removed": 2, "skipped_open": 3}
        with mock.patch.object(log_retention, "retain", return_value=result):
            with contextlib.redirect_stderr(errors), contextlib.redirect_stdout(output):
                log_retention.startup(self.root)
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(json.loads(output.getvalue()), {"log_retention": result})

    def test_compact_output_omits_large_result_but_keeps_report_pointer(self):
        output = io.StringIO()
        result = {"state": "EXHAUSTED", "detail": "private detail" * 100000}
        with contextlib.redirect_stdout(output):
            log_retention.print_result(result, self.root / "reports/research-status.json")
        self.assertLess(len(output.getvalue()), 300)
        self.assertEqual(json.loads(output.getvalue()), {"state": "EXHAUSTED", "report": str(self.root / "reports/research-status.json")})
        self.assertEqual(len(result["detail"]), 1400000)

    @unittest.skipIf(os.name == "nt", "POSIX-only (E2.T3: process groups, rlimits, flock, lsof or symlinks)")
    def test_lsof_partial_matches_are_not_mistaken_for_no_open_files(self):
        path = self.root / "logs/research.stdout.log"
        path.write_bytes(b"active")
        result = subprocess.CompletedProcess([], 1, f"p999999\nf1\nn{path}\n", "")
        with mock.patch.object(log_retention.subprocess, "run", return_value=result):
            outcome = log_retention.retain(self.root, rotate_bytes=1)
        self.assertEqual(outcome["skipped_open"], 1)
        self.assertEqual(path.read_bytes(), b"active")

    @unittest.skipIf(os.name == "nt", "POSIX-only (E2.T3: process groups, rlimits, flock, lsof or symlinks)")
    def test_failed_replacement_restores_original_path(self):
        path = self.root / "logs/research.stdout.log"
        path.write_bytes(b"original")
        rename = Path.rename

        def fail_new_file(source, destination):
            if source.name.startswith(".retention-new-"):
                raise OSError("synthetic replacement failure")
            return rename(source, destination)

        with mock.patch.object(Path, "rename", fail_new_file):
            with self.assertRaises(OSError):
                log_retention.retain(self.root, rotate_bytes=1)
        self.assertEqual(path.read_bytes(), b"original")
        self.assertEqual(list((self.root / "logs").iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
