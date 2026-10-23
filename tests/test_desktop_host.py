import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import desktop_adapter
import test_desktop_adapter_safety as fixtures


class DesktopHostHealthTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = sqlite3.connect(":memory:")
        self.addCleanup(self.database.close)
        desktop_adapter.initialize(self.database)

    def insert(self, operation, state, expiry, lease=None):
        with self.database:
            self.database.execute("""
                INSERT INTO desktop_requests
                (request_id,binding,job_key,operation,payload,state,created_at,expires_at,lease_until)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (str(self.database.total_changes), "synthetic-binding", "synthetic-job",
                  operation, '{"private":"do-not-report"}', state,
                  fixtures.NOW, expiry, lease))

    def test_capability_check_never_opens_database_config_or_process(self):
        with patch("sqlite3.connect", side_effect=AssertionError("No database access")), \
                patch.object(Path, "read_text", side_effect=AssertionError("No config access")), \
                patch("subprocess.run", side_effect=AssertionError("No executor")):
            result = desktop_adapter.host_health(now=fixtures.NOW)
        self.assertEqual(result["code"], desktop_adapter.HOST_BLOCKER)
        self.assertFalse(result["unattended_ready"])
        self.assertFalse(result["can_submit"])
        self.assertEqual(result["mailbox"]["state"], "NOT_CHECKED")

    def test_health_counts_expired_and_unknown_without_mutating_or_leaking(self):
        past = desktop_adapter.later(fixtures.NOW, -1)
        future = desktop_adapter.later(fixtures.NOW, desktop_adapter.LEASE_SECONDS)
        for operation, state, expiry, lease in [
            ("inspect", "PENDING", past, None),
            ("inspect", "CLAIMED", future, fixtures.NOW),
            ("submit", "CLAIMED", future, fixtures.NOW),
            ("submit", "PENDING", past, None),
            ("submit", "UNKNOWN", future, None),
            ("verify", "CLAIMED", future, future),
            ("inspect", "DONE", future, None),
        ]:
            self.insert(operation, state, expiry, lease)
        before = list(self.database.iterdump())
        result = desktop_adapter.host_health(self.database, fixtures.NOW)
        mailbox = result["mailbox"]
        self.assertEqual(mailbox["expired_read_requests"], 2)
        self.assertEqual(mailbox["expired_submit_requests"], 2)
        self.assertEqual(mailbox["unknown_submit_requests"], 1)
        self.assertEqual(mailbox["active_claims"], 1)
        self.assertTrue(mailbox["requires_reconciliation"])
        self.assertFalse(mailbox["submission_retry_authorized"])
        self.assertFalse(result["unattended_ready"])
        self.assertEqual(list(self.database.iterdump()), before)
        self.assertNotIn("synthetic-job", json.dumps(result))
        self.assertNotIn("do-not-report", json.dumps(result))

    def test_empty_healthy_mailbox_does_not_establish_executor(self):
        result = desktop_adapter.host_health(self.database, fixtures.NOW)
        self.assertEqual(result["mailbox"]["state"], "READABLE")
        self.assertEqual(result["mailbox"]["request_counts"], {})
        self.assertFalse(result["unattended_ready"])

    def test_missing_or_malformed_mailbox_fails_closed_without_initializing(self):
        result = desktop_adapter.host_health_at(self.root, fixtures.NOW)
        self.assertEqual(result["mailbox"]["state"], "UNAVAILABLE")
        self.assertEqual(list(self.root.iterdir()), [])
        (self.root / "state").mkdir()
        path = self.root / "state/orchestrator.sqlite3"
        path.write_bytes(b"synthetic invalid database")
        result = desktop_adapter.host_health_at(self.root, fixtures.NOW)
        self.assertEqual(result["mailbox"]["state"], "UNAVAILABLE")
        self.assertEqual(path.read_bytes(), b"synthetic invalid database")

    def test_existing_mailbox_is_opened_read_only(self):
        (self.root / "state").mkdir()
        path = self.root / "state/orchestrator.sqlite3"
        database = sqlite3.connect(path)
        desktop_adapter.initialize(database)
        database.close()
        before = path.read_bytes()
        original_connect = sqlite3.connect
        with patch("sqlite3.connect", wraps=original_connect) as connect:
            result = desktop_adapter.host_health_at(self.root, fixtures.NOW)
        self.assertTrue(connect.call_args.args[0].endswith("?mode=ro"))
        self.assertTrue(connect.call_args.kwargs["uri"])
        self.assertEqual(result["mailbox"]["state"], "READABLE")
        self.assertEqual(path.read_bytes(), before)

    def test_health_cli_subprocess_is_nondestructive_and_blocked(self):
        script = Path(desktop_adapter.__file__).resolve()
        for options in ([], ["--mailbox"]):
            with self.subTest(options=options):
                result = subprocess.run(
                    [sys.executable, "-B", str(script), "host-health", "--root", str(self.root), *options],
                    stdin=subprocess.DEVNULL, capture_output=True, text=True,
                    timeout=10, check=False, cwd=self.root,
                    env={"PYTHONDONTWRITEBYTECODE": "1"})
                self.assertEqual(result.returncode, 2, result.stderr)
                health = json.loads(result.stdout)
                self.assertEqual(health["code"], desktop_adapter.HOST_BLOCKER)
                self.assertFalse(health["unattended_ready"])
                self.assertEqual(list(self.root.iterdir()), [])


class DesktopHostFailureIntegrationTests(unittest.TestCase):
    setUp = fixtures.DesktopAdapterSafetyTests.setUp
    connect = fixtures.DesktopAdapterSafetyTests.connect
    write_config = fixtures.DesktopAdapterSafetyTests.write_config
    job = fixtures.DesktopAdapterSafetyTests.job
    review = fixtures.DesktopAdapterSafetyTests.review
    prepare_submit = fixtures.DesktopAdapterSafetyTests.prepare_submit
    claim = fixtures.DesktopAdapterSafetyTests.claim
    arm = fixtures.DesktopAdapterSafetyTests.arm

    def test_host_failure_does_not_renew_or_reissue_armed_submission(self):
        self.prepare_submit()
        envelope = self.claim()
        self.arm(envelope)
        self.now = envelope["lease_until"]
        before = list(self.database.iterdump())
        result = desktop_adapter.host_health(self.database, self.now)
        self.assertEqual(result["mailbox"]["expired_submit_requests"], 1)
        self.assertEqual(list(self.database.iterdump()), before)
        with self.assertRaisesRegex(ValueError, "STALE_OR_MISMATCHED"):
            desktop_adapter.complete(self.database, dict(envelope, result={"acknowledged": True}), self.now)
        self.assertIsNone(self.claim())
        self.assertEqual(self.database.execute("SELECT state FROM desktop_requests").fetchone()[0], "UNKNOWN")
        self.assertFalse(desktop_adapter.host_health(self.database, self.now)["mailbox"]["submission_retry_authorized"])

    def test_completed_submit_ack_does_not_establish_host_or_receipt(self):
        self.prepare_submit()
        envelope = self.claim()
        self.arm(envelope)
        desktop_adapter.complete(self.database, dict(envelope, result={
            "acknowledged": True, "status": "SUBMITTED", "verified": True}), self.now)
        result = json.loads(self.database.execute("SELECT result FROM desktop_requests").fetchone()[0])
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertTrue(result["requires_independent_receipt"])
        health = desktop_adapter.host_health(self.database, self.now)
        self.assertFalse(health["unattended_ready"])
        self.assertEqual(health["mailbox"]["request_counts"], {"DONE": 1})


if __name__ == "__main__":
    unittest.main()
