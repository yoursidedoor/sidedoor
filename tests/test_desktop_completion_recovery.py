import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import json
import unittest
from unittest.mock import Mock, patch

import applications
import desktop_adapter
import orchestrator
import test_desktop_adapter_safety as fixtures


class DesktopCompletionRecoveryTests(unittest.TestCase):
    setUp = fixtures.DesktopAdapterSafetyTests.setUp
    connect = fixtures.DesktopAdapterSafetyTests.connect
    write_config = fixtures.DesktopAdapterSafetyTests.write_config
    job = fixtures.DesktopAdapterSafetyTests.job
    review = fixtures.DesktopAdapterSafetyTests.review

    def cycle(self):
        return applications.cycle(self.database, self.config, self.root,
                                  adapter=self.adapter, now=self.now)

    def advance(self):
        self.now = desktop_adapter.later(self.now, 60)

    def claim(self, operation):
        request = desktop_adapter.claim(self.database, self.root, "recovery-fixture", self.now)
        self.assertIsNotNone(request)
        self.assertEqual(request["payload"]["operation"], operation)
        return request

    def complete(self, request, result):
        accepted = desktop_adapter.complete(self.database, dict(request, result=result), self.now)
        self.assertTrue(accepted["accepted"])

    def receipt(self):
        return {
            "status": "SUBMITTED", "source": "synthetic ATS account record",
            "verified": True, "observed_at": self.now, "independent_read": True,
            "employer": "synthetic company", "requisition": "role-123",
            "candidate_email": "candidate@example.invalid", "receipt_id": "fixture-receipt",
            "evidence_kind": "authenticated_ats_application_record",
            "provider_message_or_application_id": "fixture-application",
        }

    def armed_submission(self):
        self.cycle()
        self.complete(self.claim("inspect"), self.review())
        self.advance()
        self.cycle()
        request = self.claim("submit")
        desktop_adapter.arm(self.database, self.root,
                            dict(request, observed_form_revision="fixture-form-v1"), self.now)
        return request

    def pending_receipt(self):
        # The submit page confirmed nothing, so the job waits on an independent read. (An armed
        # submit whose page confirmed the application is itself the verified submission, owner
        # 2026-10-06, and never reaches reconcile.)
        self.complete(self.armed_submission(), {"acknowledged": False})
        self.advance()
        self.cycle()
        return self.claim("reconcile")

    def succeed(self):
        request = self.pending_receipt()
        stale_job = self.job()
        self.complete(request, self.receipt())
        self.advance()
        self.cycle()
        self.assertEqual(self.job()["state"], "SUBMITTED")
        self.assertEqual(self.database.execute(
            "SELECT state FROM application_attempts").fetchone()[0], "VERIFIED")
        return stale_job

    def other_worker(self):
        database = self.connect()
        self.addCleanup(database.close)
        adapter = desktop_adapter.DesktopAdapter(database, self.root, self.config, lambda: self.now)
        return database, adapter

    def assert_success_preserved(self, completed, notification=None):
        current = self.job()
        fields = ("state", "attempts", "reconcile_attempts", "receipt", "blocker",
                  "next_at", "next_action", "updated_at")
        changes = {field: {"expected": completed[field], "actual": current[field]}
                   for field in fields if current[field] != completed[field]}
        self.assertEqual(changes, {}, "A completed application was changed by stale work")
        self.assertEqual(self.database.execute(
            "SELECT count(*) FROM application_blockers").fetchone()[0], 0)
        self.assertEqual(self.database.execute(
            "SELECT count(*) FROM application_attempts").fetchone()[0], 1)
        self.assertEqual(self.database.execute(
            "SELECT count(*) FROM desktop_requests WHERE operation='submit'").fetchone()[0], 1)
        self.assertEqual(self.database.execute(
            "SELECT count(*) FROM application_history WHERE kind='APPLICATION'").fetchone()[0], 1)
        self.assertEqual(self.database.execute(
            "SELECT count(*) FROM notification_outbox WHERE event_key LIKE ?",
            (notification or self.key + ":submitted",)).fetchone()[0], 1)

    def test_stale_queued_worker_after_verified_completion_preserves_submitted(self):
        stale_job = self.job()
        database, adapter = self.other_worker()
        self.succeed()
        completed = self.job()
        applications.process(database, stale_job, adapter, self.policy, self.root, self.now)
        self.assert_success_preserved(completed)

    def test_stale_reconcile_worker_after_verified_completion_does_not_reopen_job(self):
        database, adapter = self.other_worker()
        stale_job = self.succeed()
        completed = self.job()
        request_count = self.database.execute("SELECT count(*) FROM desktop_requests").fetchone()[0]
        applications.process(database, stale_job, adapter, self.policy, self.root, self.now)
        self.assert_success_preserved(completed)
        self.assertEqual(self.database.execute(
            "SELECT count(*) FROM desktop_requests").fetchone()[0], request_count)

    def delayed_inspect(self, result=None, error=None, use_cycle=False):
        database = self.connect()
        self.addCleanup(database.close)
        stale_job = self.job()
        snapshots = []

        def return_after_completion(operation, payload):
            self.assertEqual(operation, "inspect")
            self.succeed()
            snapshots.append(self.job())
            if error:
                raise error
            return result

        adapter = Mock(can_submit=True)
        adapter.call.side_effect = return_after_completion
        if use_cycle:
            applications.cycle(database, self.config, self.root, adapter=adapter, now=self.now)
        else:
            applications.process(database, stale_job, adapter, self.policy, self.root, self.now)
        adapter.call.assert_called_once()
        self.assert_success_preserved(snapshots[0])

    def test_delayed_inspect_error_cannot_schedule_retry_after_verified_completion(self):
        self.delayed_inspect(error=ConnectionError("synthetic delayed inspect failure"))

    def test_delayed_inspect_blocker_cannot_replace_verified_completion(self):
        self.delayed_inspect(result={"blocked": {
            "code": "AUTHENTICATION_REQUIRED", "next_action": "Synthetic stale login result"}})

    def test_delayed_malformed_inspect_cannot_replace_success_via_cycle_error_handler(self):
        self.delayed_inspect(result={"blocked": {"code": "AUTHENTICATION_REQUIRED"}}, use_cycle=True)

    def pause(self):
        self.config["applications_enabled"] = False
        self.write_config()

    def assert_paused_cycle_preserves_results(self):
        before = list(self.database.iterdump())
        with patch.object(orchestrator.notifications, "dispatch") as notifications, \
                patch.object(orchestrator.decision_transport, "cycle") as decisions:
            result = orchestrator.application_cycle(self.root, self.config, self.database)
        self.assertEqual(result["state"], "PAUSED_FOR_REVISED_REQUIREMENTS")
        notifications.assert_not_called()
        decisions.assert_not_called()
        self.assertEqual(list(self.database.iterdump()), before)

    def test_armed_submit_ack_arriving_during_pause_is_retained_for_resume(self):
        request = self.armed_submission()
        self.pause()
        self.complete(request, {"acknowledged": True, "status": "SUBMITTED"})
        self.assert_paused_cycle_preserves_results()
        result = json.loads(self.database.execute(
            "SELECT result FROM desktop_requests WHERE request_id=?", (request["request_id"],)
        ).fetchone()[0])
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertTrue(result["requires_independent_receipt"])
        self.assertNotEqual(self.job()["state"], "SUBMITTED")
        self.config["applications_enabled"] = True
        self.write_config()
        self.advance()
        self.cycle()
        # The retained page confirmation settles the job on resume, with no reconcile read
        # (owner, 2026-10-06).
        self.assertEqual(self.job()["state"], "SUBMITTED")
        self.assertEqual(json.loads(self.job()["receipt"])["evidence_kind"], "page_confirmation")
        self.assertIsNone(desktop_adapter.claim(self.database, self.root, "recovery-fixture", self.now))
        # Settled by the cycle's opening pass, which sends one message for the pass, not one per job.
        self.assert_success_preserved(self.job(), "settled-page-confirmations:%")
        self.assertEqual(self.database.execute("SELECT count(*) FROM notification_outbox WHERE event_key=?",
                                               (self.key + ":submitted",)).fetchone()[0], 0)

    def test_independent_receipt_arriving_during_pause_survives_restart_and_resume(self):
        request = self.pending_receipt()
        self.pause()
        self.complete(request, self.receipt())
        self.assert_paused_cycle_preserves_results()
        self.assertEqual(self.database.execute(
            "SELECT state FROM desktop_requests WHERE request_id=?", (request["request_id"],)
        ).fetchone()[0], "DONE")
        self.database.close()
        self.database = self.connect()
        self.addCleanup(self.database.close)
        self.adapter = desktop_adapter.DesktopAdapter(self.database, self.root, self.config, lambda: self.now)
        self.config["applications_enabled"] = True
        self.write_config()
        self.advance()
        self.cycle()
        self.assertEqual(self.job()["state"], "SUBMITTED")
        completed = self.job()
        self.advance()
        self.cycle()
        self.assert_success_preserved(completed)
        self.assertEqual(self.database.execute(
            "SELECT count(*) FROM application_events WHERE event='SUBMISSION_VERIFIED'"
        ).fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
