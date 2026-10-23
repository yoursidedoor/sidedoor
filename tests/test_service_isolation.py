import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import applications
import orchestrator
import profile_home


class ServiceIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for folder in ("state", "reports"):
            (self.root / folder).mkdir()
        self.database = orchestrator.connect(self.root)
        self.addCleanup(self.database.close)
        applications.initialize(self.database)
        self.config = {"timezone": "America/Los_Angeles", "applications_enabled": False}

    def test_paused_applications_do_not_pause_transport_or_reporting(self):
        with patch.object(orchestrator.decision_transport, "cycle", return_value={"state": "COMPLETE"}) as inbox, \
                patch.object(orchestrator.reporting, "daily_due", return_value=True), \
                patch.object(orchestrator.reporting, "daily", return_value={"state": "NOT_DUE"}) as report, \
                patch.object(orchestrator.notifications, "dispatch", return_value={"state": "COMPLETE"}) as notifications, \
                patch.object(orchestrator.applications, "cycle") as worker:
            result = orchestrator.service_cycle(self.root, self.config, self.database)
        self.assertEqual(result["applications"]["state"], orchestrator.APPLICATIONS_JOB_STATE)
        worker.assert_not_called()
        self.assertEqual(inbox.call_count, 2)
        report.assert_called_once()
        notifications.assert_called_once()
        # 11 worker steps complete: the posting-judge step joined the service cycle on
        # 2026-09-24, the applications step left it on 2026-10-02 (its own job) and the learning
        # step (source-bound-outcomes) was removed on 2026-10-22.
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM workflow_tasks WHERE status='COMPLETE'").fetchone()[0], 11)
        self.assertEqual(result["new_contact_execution"]["state"], "DISABLED")
        self.assertEqual(result["due_followups"]["state"], "DISABLED")

    def test_fresh_root_first_cycle_records_no_step_failure(self):
        """E0.T6: before research has created its rw_* tables, the contact finder's first cycle
        failed (`no such table: rw_meta`) and showed on the Failures page."""
        config = dict(self.config, linkedin_contacts={"enabled": True})
        with patch.object(orchestrator.decision_transport, "cycle", return_value={"state": "COMPLETE"}), \
                patch.object(orchestrator.reporting, "daily_due", return_value=False), \
                patch.object(orchestrator.notifications, "dispatch", return_value={"state": "COMPLETE"}):
            result = orchestrator.service_cycle(self.root, config, self.database)
        self.assertEqual(result["linkedin_contacts"]["state"], "COMPLETE")
        self.assertEqual(self.database.execute(
            "SELECT worker FROM workflow_tasks WHERE status!='COMPLETE'").fetchall(), [])

    def test_hourly_cycle_runs_no_application_work(self):
        """Owner, 2026-10-02: applications run as their own job; the hourly cycle never calls
        the application cycle, the application browser, or settles application reservations."""
        profile_home.use(self, {})
        self.config["applications_enabled"] = True
        with patch.object(orchestrator.decision_transport, "cycle", return_value={"state": "COMPLETE"}), \
                patch.object(orchestrator.reporting, "daily_due", return_value=True), \
                patch.object(orchestrator.reporting, "daily", return_value={"state": "NOT_DUE"}), \
                patch.object(orchestrator.notifications, "dispatch", return_value={"state": "COMPLETE"}) as notifications, \
                patch.object(orchestrator.applications, "cycle") as worker, \
                patch.object(orchestrator.applications, "settle_all_browser_reservations") as settle:
            result = orchestrator.service_cycle(self.root, self.config, self.database)
        worker.assert_not_called()
        settle.assert_not_called()
        self.assertEqual(result["applications"]["state"], orchestrator.APPLICATIONS_JOB_STATE)
        notifications.assert_called_once()
        self.assertIsNone(self.database.execute("SELECT 1 FROM workflow_tasks WHERE worker='applications'").fetchone())

    def test_application_exception_in_the_applications_job_is_contained(self):
        profile_home.use(self, {})
        self.config["applications_enabled"] = True
        with patch.object(orchestrator.applications, "cycle", side_effect=RuntimeError("private-token-must-not-leak")), \
                patch.object(orchestrator.applications, "settle_all_browser_reservations", return_value=0) as settle:
            result = orchestrator.applications_service(self.root, self.config, self.database)
        self.assertEqual(result["applications"]["state"], "FAILED")
        self.assertEqual(result["state"], "FAILED")
        self.assertNotIn("private-token", str(result))
        self.assertEqual(settle.call_count, 2)  # first and last (owner, 2026-09-28)
        self.assertEqual(self.database.execute("SELECT status FROM workflow_tasks WHERE worker='applications'").fetchone()[0], "BLOCKED")

    def test_failed_transport_and_report_are_isolated_from_dispatch(self):
        with patch.object(orchestrator.decision_transport, "cycle", side_effect=ConnectionError("offline")), \
                patch.object(orchestrator.reporting, "daily_due", return_value=True), \
                patch.object(orchestrator.reporting, "daily", side_effect=ValueError("bad report")), \
                patch.object(orchestrator.notifications, "dispatch", return_value={"state": "COMPLETE"}) as notifications:
            result = orchestrator.service_cycle(self.root, self.config, self.database)
        self.assertEqual(result["decision_transport"]["state"], "FAILED")
        self.assertEqual(result["daily_report"]["state"], "FAILED")
        self.assertEqual(result["notifications"]["state"], "COMPLETE")
        notifications.assert_called_once()

    def test_connector_failures_do_not_stop_application_reporting_or_notifications(self):
        with patch.object(orchestrator.decision_transport, "cycle", return_value={"state": "COMPLETE"}), \
                patch.object(orchestrator, "draft_promotion_cycle", side_effect=ValueError("private-draft")), \
                patch.object(orchestrator, "reply_classification_cycle", side_effect=ValueError("private-message")), \
                patch.object(orchestrator.reporting, "daily_due", return_value=True), \
                patch.object(orchestrator.reporting, "daily", return_value={"state": "NOT_DUE"}) as report, \
                patch.object(orchestrator.notifications, "dispatch", return_value={"state": "COMPLETE"}) as notify:
            result = orchestrator.service_cycle(self.root, self.config, self.database)
        self.assertEqual(result["draft_promotion"]["state"], "FAILED")
        self.assertEqual(result["reply_classification"]["state"], "FAILED")
        self.assertEqual(result["applications"]["state"], orchestrator.APPLICATIONS_JOB_STATE)
        self.assertNotIn("private-", str(result))
        report.assert_called_once()
        notify.assert_called_once()

    def test_monitor_scope_does_not_generate_or_classify_new_work(self):
        self.config["scope"] = "monitor_existing_pipeline"
        with patch.object(orchestrator.decision_transport, "cycle", return_value={"state": "COMPLETE"}), \
                patch.object(orchestrator, "draft_promotion_cycle") as promote, \
                patch.object(orchestrator, "reply_classification_cycle") as classify, \
                patch.object(orchestrator.reporting, "daily_due", return_value=True), \
                patch.object(orchestrator.reporting, "daily", return_value={"state": "NOT_DUE"}), \
                patch.object(orchestrator.notifications, "dispatch", return_value={"state": "COMPLETE"}):
            orchestrator.service_cycle(self.root, self.config, self.database)
        promote.assert_not_called()
        classify.assert_not_called()

    def test_reply_decisions_are_converted_before_application_worker(self):
        order = []
        with patch.object(orchestrator.decision_transport, "cycle", return_value={"state": "COMPLETE"}), \
                patch.object(orchestrator, "conversion_cycle", side_effect=lambda *args: order.append("conversion") or {"state": "COMPLETE"}), \
                patch.object(orchestrator, "reply_classification_cycle", side_effect=lambda *args: order.append("reply") or {"state": "COMPLETE", "decisions": [{"state": "ROUTED"}]}), \
                patch.object(orchestrator, "application_cycle", side_effect=lambda *args: order.append("application") or {"state": "COMPLETE"}), \
                patch.object(orchestrator.reporting, "daily_due", return_value=True), \
                patch.object(orchestrator.reporting, "daily", return_value={"state": "NOT_DUE"}), \
                patch.object(orchestrator.notifications, "dispatch", return_value={"state": "COMPLETE"}):
            result = orchestrator.service_cycle(self.root, self.config, self.database)
        self.assertEqual(["conversion", "reply", "conversion"], order)  # no application step (2026-10-02)
        self.assertEqual("COMPLETE", result["reply_conversion"]["state"])


if __name__ == "__main__":
    unittest.main()
