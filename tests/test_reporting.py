import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import json
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

import reporting
import applications
import test_applications_e2e as fixtures


class ReportingTests(unittest.TestCase):
    setUp = fixtures.ApplicationEndToEndTests.setUp

    def seed_research_day(self, state, attempts, next_retry=None, completed_at=None):
        self.database.execute("CREATE TABLE research_days (day TEXT PRIMARY KEY, state TEXT, attempts INTEGER, attempt_id TEXT, next_retry_at TEXT, completed_at TEXT)")
        self.database.execute("INSERT INTO research_days VALUES (?,?,?,?,?,?)",
                              ("2026-09-17", state, attempts, "saved-attempt", next_retry, completed_at))

    def test_exhausted_research_is_visible_despite_complete_services_and_long_report(self):
        self.seed_research_day("EXHAUSTED", 3)
        evidence = {"state": "PARTIAL", "worker_status": "PARTIAL", "report": "saved fixture report",
                    "rejected_drafts": [], "summary": "saved incomplete research " + "😀" * 5000}
        self.database.execute("INSERT INTO research VALUES (?,?,?,?)",
                              ("saved-attempt", fixtures.NOW, "PARTIAL", json.dumps(evidence)))
        self.database.execute("INSERT INTO runs VALUES (?,?,?,?,?)",
                              ("saved-services", fixtures.NOW, "COMPLETE", fixtures.NOW,
                               json.dumps({"state": "COMPLETE", "daily_services": {"state": "COMPLETE"}})))
        before = [tuple(row) for row in self.database.execute("SELECT * FROM research_days")]
        for index in range(90):
            self.database.execute("INSERT INTO application_blockers VALUES (?,?,?,?,?,?,?)",
                                  (str(index), "MISSING_FACT", "user", "😀" * 100, 2, fixtures.NOW, fixtures.NOW))
        payload = reporting.daily(self.database, {}, "2026-09-18T01:00:00+00:00")
        saved = payload["research_daily"]
        self.assertEqual((saved["day"], saved["state"], saved["attempts"]), ("2026-09-17", "EXHAUSTED", 3))
        self.assertIsNone(saved["next_retry_at"])
        self.assertIsNone(saved["completed_at"])
        self.assertEqual(saved["latest_attempt"]["record"]["result"], evidence)
        self.assertEqual([tuple(row) for row in self.database.execute("SELECT * FROM research_days")], before)
        stored = json.loads(self.database.execute("SELECT payload FROM daily_reports").fetchone()[0])
        self.assertEqual(stored["research_daily"], saved)
        body = reporting.full_report(self.database, "2026-09-17")
        for detail in ("Saved research 2026-09-17: EXHAUSTED; attempts=3", "state=PARTIAL", "saved incomplete research",
                       "No new research verification", "Full unabridged report"):
            self.assertIn(detail, body)
        self.assertNotIn("[summary truncated]", body)
        for row in self.database.execute("SELECT message FROM notification_outbox"):
            self.assertLessEqual(len(row[0].encode("utf-16-le")) // 2, reporting.PART_LIMIT)

    def test_failed_research_keeps_saved_retry_and_attempt_evidence(self):
        retry = "2026-09-17T16:15:00+00:00"
        self.seed_research_day("RETRY_WAIT", 1, next_retry=retry)
        evidence = {"state": "FAILED", "error": "mock timeout"}
        self.database.execute("INSERT INTO research VALUES (?,?,?,?)",
                              ("saved-attempt", fixtures.NOW, "FAILED", json.dumps(evidence)))
        payload = reporting.daily(self.database, {}, "2026-09-18T01:00:00+00:00")
        saved = payload["research_daily"]
        self.assertEqual(saved["state"], "RETRY_WAIT")
        self.assertEqual(saved["attempts"], 1)
        self.assertEqual(saved["next_retry_at"], retry)
        self.assertIsNone(saved["completed_at"])
        self.assertEqual(saved["latest_attempt"]["record"]["started_at"], fixtures.NOW)
        self.assertEqual(saved["latest_attempt"]["record"]["result"], evidence)
        body = reporting.full_report(self.database, "2026-09-17")
        self.assertIn("RETRY_WAIT; attempts=1; next_retry=" + retry, body)
        self.assertIn("state=FAILED", body)

    def test_absent_research_days_is_unknown_without_creating_table(self):
        payload = reporting.daily(self.database, {}, "2026-09-18T01:00:00+00:00")
        saved = payload["research_daily"]
        self.assertEqual(saved["state"], "UNKNOWN")
        self.assertEqual(saved["reason"], "TABLE_ABSENT_OR_SCHEMA_UNAVAILABLE")
        self.assertIsNone(saved["attempts"])
        self.assertEqual(saved["basis"], "SAVED")
        self.assertFalse(saved["checked_by_this_report"])
        self.assertIsNone(self.database.execute("SELECT 1 FROM sqlite_master WHERE name='research_days'").fetchone())
        self.assertIn("Saved research 2026-09-17: UNKNOWN", reporting.full_report(self.database, "2026-09-17"))

    def test_missing_today_does_not_reuse_yesterdays_complete_research(self):
        self.seed_research_day("COMPLETE", 1, completed_at=fixtures.NOW)
        self.database.execute("UPDATE research_days SET day='2026-09-16'")
        saved = reporting.snapshot(self.database, {}, "2026-09-18T01:00:00+00:00")["research_daily"]
        self.assertEqual(saved["day"], "2026-09-17")
        self.assertEqual(saved["state"], "UNKNOWN")
        self.assertEqual(saved["reason"], "NO_SAVED_DAY")
        self.assertIsNone(saved["completed_at"])

    def test_missing_research_attempt_never_borrows_unrelated_success(self):
        self.seed_research_day("EXHAUSTED", 3)
        self.database.execute("INSERT INTO research VALUES (?,?,?,?)",
                              ("unrelated", fixtures.NOW, "COMPLETE", json.dumps({"state": "COMPLETE"})))
        saved = reporting.snapshot(self.database, {}, fixtures.NOW)["research_daily"]
        self.assertEqual(saved["state"], "EXHAUSTED")
        self.assertEqual(saved["latest_attempt"]["state"], "UNKNOWN")
        self.assertIsNone(saved["latest_attempt"]["record"])
        self.assertEqual(saved["attempt_id"], "saved-attempt")

    def test_completed_research_is_saved_not_new_verification(self):
        self.seed_research_day("COMPLETE", 2, completed_at=fixtures.NOW)
        self.database.execute("INSERT INTO research VALUES (?,?,?,?)",
                              ("saved-attempt", fixtures.NOW, "COMPLETE", json.dumps({"state": "COMPLETE"})))
        payload = reporting.daily(self.database, {}, "2026-09-18T01:00:00+00:00")
        saved = payload["research_daily"]
        self.assertEqual(saved["completed_at"], fixtures.NOW)
        self.assertEqual(saved["state"], "COMPLETE")
        self.assertEqual(saved["basis"], "SAVED")
        self.assertFalse(saved["checked_by_this_report"])
        body = reporting.full_report(self.database, "2026-09-17")
        self.assertIn("completed_at=" + fixtures.NOW, body)
        self.assertIn("No new research verification", body)

    def test_daily_report_is_once_per_day_and_waits_for_single_owner(self):
        config = {"timezone": "America/Los_Angeles"}
        self.assertEqual(reporting.daily(self.database, config, "2026-09-17T23:00:00+00:00")["state"], "NOT_DUE")
        first = reporting.daily(self.database, config, "2026-09-18T01:00:00+00:00")
        second = reporting.daily(self.database, config, "2026-09-18T02:00:00+00:00")
        self.assertEqual(first, second)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM daily_reports").fetchone()[0], 1)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0], 1)
        original_message = self.database.execute("SELECT message FROM notification_outbox").fetchone()[0]
        self.assertEqual(self.database.execute("SELECT state FROM notification_outbox").fetchone()[0], "WAITING_OWNERSHIP")
        released = reporting.daily(self.database, dict(config, reporting_owner_confirmed=True, application_adapter="desktop", outreach_mode="guarded"), "2026-09-18T02:01:00+00:00")
        self.assertEqual(released, first)
        self.assertEqual(json.loads(self.database.execute("SELECT payload FROM daily_reports").fetchone()[0]), first)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0], 1)
        self.assertEqual(self.database.execute("SELECT message FROM notification_outbox").fetchone()[0], original_message)
        self.assertEqual(self.database.execute("SELECT state FROM notification_outbox").fetchone()[0], "PENDING")

    def test_missing_adapter_and_desktop_table_are_reported_without_creating_table(self):
        config = {"timezone": "America/Los_Angeles", "outreach_mode": "guarded"}
        original_config = dict(config)
        result = reporting.daily(self.database, config, "2026-09-18T01:00:00+00:00")
        gate = result["application_executor_gate"]
        self.assertEqual(gate["state"], "ADAPTER_MISSING")
        self.assertEqual(gate["adapter"], "missing")
        self.assertFalse(gate["adapter_configured"])
        self.assertFalse(gate["desktop_adapter_configured"])
        self.assertEqual(gate["scheduled_host_execution"], "UNVERIFIED")
        self.assertEqual(gate["desktop_requests"], {"table_exists": False, "pending": None, "claimed": None,
                                                   "expired": None, "unknown": None, "expired_active": None})
        self.assertIsNone(self.database.execute("SELECT 1 FROM sqlite_master WHERE name='desktop_requests'").fetchone())
        self.assertEqual(config, original_config)
        outreach = result["guarded_outreach_gate"]
        self.assertEqual(outreach["mode"], "guarded")
        self.assertEqual(outreach["state"], "REQUIRES_PER_RECIPIENT_VALIDATION")
        self.assertEqual(outreach["validation"], "NOT_EVALUATED_BY_REPORT")
        self.assertIn("Durable pre-send claim", outreach["requirements"])
        self.assertIn("Live provider checks", outreach["requirements"])
        message = reporting.full_report(self.database, "2026-09-17")
        for detail in ("ADAPTER_MISSING", "desktop configured=False", "desktop_requests table absent",
                       "Scheduled-host execution: UNVERIFIED", "REQUIRES_PER_RECIPIENT_VALIDATION",
                       "drafts/prepared campaigns are not execution or delivery"):
            self.assertIn(detail, message)

    def test_desktop_request_counts_are_read_only_and_not_host_execution_proof(self):
        self.database.execute("CREATE TABLE desktop_requests (request_id TEXT PRIMARY KEY, state TEXT, expires_at TEXT)")
        self.database.executemany("INSERT INTO desktop_requests VALUES (?,?,?)", [
            ("pending", "PENDING", "2026-09-18T02:00:00+00:00"),
            ("overdue", "PENDING", "2026-09-17T18:00:00-07:00"),
            ("claimed", "CLAIMED", "2026-09-18T02:00:00+00:00"),
            ("expired", "EXPIRED", "2026-09-18T00:00:00+00:00"),
            ("unknown", "UNKNOWN", "2026-09-18T00:00:00+00:00"),
            ("foreground-read", "DONE", "2026-09-18T02:00:00+00:00"),
        ])
        before = [tuple(row) for row in self.database.execute("SELECT * FROM desktop_requests ORDER BY request_id")]
        config = {"timezone": "America/Los_Angeles", "application_adapter": "desktop",
                  "applications_enabled": True, "execution_ownership_verified": True, "reporting_owner_confirmed": True}
        result = reporting.daily(self.database, config, "2026-09-18T01:00:00+00:00")
        gate = result["application_executor_gate"]
        self.assertEqual(gate["state"], "CONFIGURED_EXECUTION_UNVERIFIED")
        self.assertEqual(gate["adapter"], "desktop")
        self.assertTrue(gate["adapter_configured"])
        self.assertTrue(gate["desktop_adapter_configured"])
        self.assertTrue(gate["applications_enabled"])
        self.assertTrue(gate["execution_ownership_verified"])
        self.assertEqual(gate["scheduled_host_execution"], "UNVERIFIED")
        self.assertEqual(gate["desktop_requests"], {"table_exists": True, "pending": 2, "claimed": 1,
                                                   "expired": 1, "unknown": 1, "expired_active": 1})
        self.assertEqual([tuple(row) for row in self.database.execute("SELECT * FROM desktop_requests ORDER BY request_id")], before)
        self.assertEqual(result["verified_submissions"], 0)
        message = reporting.full_report(self.database, "2026-09-17")
        self.assertIn("pending=2, claimed=1, expired=1, unknown=1, expired active=1", message)
        self.assertEqual(self.database.execute("SELECT state FROM notification_outbox").fetchone()[0], "PENDING")
        self.database.execute("UPDATE desktop_requests SET state='DONE'")
        self.assertEqual(reporting.daily(self.database, config, "2026-09-18T02:00:00+00:00"), result)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0], 1)
        following_day = reporting.daily(self.database, config, "2026-09-19T01:00:00+00:00")
        self.assertEqual(following_day["application_executor_gate"]["desktop_requests"],
                         {"table_exists": True, "pending": 0, "claimed": 0, "expired": 0, "unknown": 0, "expired_active": 0})
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM daily_reports").fetchone()[0], 2)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0], 2)

    def test_adapter_selection_matches_worker_without_exposing_command(self):
        for selector, command, expected in [(None, None, "missing"), ("desktop", None, "desktop"),
                                            (None, ["/private/mock-adapter"], "command"),
                                            ("desktop", ["/private/mock-adapter"], "desktop")]:
            with self.subTest(selector=selector, command=command):
                gate = reporting.application_executor_gate(self.database, {"application_adapter": selector,
                                                                           "application_adapter_command": command}, fixtures.NOW)
                self.assertEqual(gate["adapter"], expected)
                self.assertEqual(gate["scheduled_host_execution"], "UNVERIFIED")
                self.assertNotIn("/private/mock-adapter", json.dumps(gate))

    def test_outreach_is_not_enabled_without_guarded_mode(self):
        for mode in (None, "disabled", "drafts"):
            with self.subTest(mode=mode):
                gate = reporting.guarded_outreach_gate({"outreach_mode": mode})
                self.assertEqual(gate["state"], "OUTREACH_NOT_ENABLED")
                self.assertEqual(gate["validation"], "NOT_EVALUATED_BY_REPORT")

    def test_baseline_interviews_and_duplicate_events_not_newly_attributed(self):
        reporting.initialize(self.database)
        with self.database:
            reporting.record_outcome(self.database, "old", "INTERVIEW", fixtures.NOW, False, {}, fixtures.evidence())
            job_key = applications.ingest(self.database, fixtures.role(), fixtures.NOW)
            receipt = dict(fixtures.evidence(), status="SUBMITTED", independent_read=True, employer="example ai", requisition="role-123", receipt_id="mock-receipt")
            self.database.execute("UPDATE application_jobs SET state='SUBMITTED',receipt=?", (json.dumps(receipt),))
            for unused in range(2):
                applications.event(self.database, job_key, "REFERRAL", {"evidence": dict(fixtures.evidence(), provider_event_id="new")}, fixtures.NOW)
        result = reporting.daily(self.database, {"timezone": "America/Los_Angeles"}, "2026-09-18T01:00:00+00:00")
        # Learning's weekly attributed counts were removed with learning (owner, 2026-10-22).
        self.assertNotIn("weekly_attributable_referrals", result)

    def test_arbitrary_attribution_flag_rejected_even_with_verified_flag(self):
        reporting.initialize(self.database)
        with self.assertRaises(ValueError):
            reporting.record_outcome(self.database, "forged", "INTERVIEW", fixtures.NOW, True, {}, fixtures.evidence())

    def test_empty_optional_tables_are_unknown_and_desktop_host_is_blocked(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        result = reporting.daily(connection, {}, "2026-09-18T01:00:00+00:00")
        self.assertIsNone(result["application_states"])
        self.assertIsNone(result["verified_submissions"])
        self.assertIsNone(result["replies"]["records"])
        self.assertEqual(result["application_executor_gate"]["desktop_host"]["code"], "DESKTOP_UNATTENDED_HOST_UNAVAILABLE")
        self.assertFalse(result["application_executor_gate"]["desktop_host"]["can_submit"])

    def test_full_report_preserved_and_telegram_summary_sent_in_parts(self):
        for index in range(90):
            self.database.execute("INSERT INTO application_blockers VALUES (?,?,?,?,?,?,?)",
                                  (str(index), "MISSING_FACT", "user", "😀" * 100 + " exact saved question", 2, fixtures.NOW, fixtures.NOW))
        result = reporting.daily(self.database, {}, "2026-09-18T01:00:00+00:00")
        bodies = [row[0] for row in self.database.execute("SELECT message FROM notification_outbox ORDER BY rowid")]
        self.assertGreater(len(bodies), 1)
        for body in bodies:
            self.assertLessEqual(len(body.encode("utf-16-le")) // 2, reporting.PART_LIMIT)
        self.assertNotIn("[summary truncated]", "".join(bodies))
        self.assertEqual("".join(bodies).count(" exact saved question"), 90)
        self.assertIn("Full unabridged report", bodies[-1])
        self.assertEqual(len(result["outstanding_actions"]), 90)
        self.assertEqual(len(json.loads(self.database.execute("SELECT payload FROM daily_reports").fetchone()[0])["outstanding_actions"]), 90)

    def test_receipt_and_saved_provider_check_not_inferred_from_state_or_now(self):
        applications.ingest(self.database, fixtures.role(), fixtures.NOW)
        self.database.execute("UPDATE application_jobs SET state='SUBMITTED',receipt=?", (json.dumps({"acknowledged": True}),))
        self.database.execute("INSERT INTO runs VALUES (?,?,?,?,?)", ("mock-saved", fixtures.NOW, "COMPLETE", fixtures.NOW,
                                                                    json.dumps({"observation": {"observed_at": fixtures.NOW, "errors": ["saved error"], "contacts": []}})))
        result = reporting.daily(self.database, {}, "2026-09-18T01:00:00+00:00")
        self.assertEqual(result["application_states"]["SUBMITTED"], 1)
        self.assertEqual(result["verified_submissions"], 0)
        self.assertEqual(result["provider_observation"]["freshness"], "STALE_SAVED")
        self.assertFalse(result["provider_observation"]["checked_by_this_report"])
        self.assertEqual(result["provider_errors"], ["saved error"])
        self.database.execute("UPDATE notification_outbox SET state='SENT',delivery=?", (json.dumps({"provider": "telegram", "message_id": 7}),))
        status = reporting.notification_status(self.database, "daily-report:2026-09-17")
        self.assertTrue(status["telegram_api_receipt_recorded"])
        self.assertFalse(status["human_read_verified"])

    def test_newer_engagement_check_and_reply_inference_use_actual_schema(self):
        import engagement
        engagement.initialize(self.database)
        engagement.record_event(self.database, {"source": "mock", "source_id": "reply", "kind": "inbound",
                                               "profile": "https://www.linkedin.com/in/mock", "occurred_at": fixtures.NOW,
                                               "observed_at": fixtures.NOW, "evidence": "mock-saved-thread", "raw": {"text": "fixture"},
                                               "inference": {"method": "fixture-review", "sentiment": "ambiguous"}})
        checked = "2026-09-18T00:50:00+00:00"
        self.database.execute("INSERT INTO engagement_cursors(source,checked_at,state) VALUES ('mock',?,'COMPLETE')", (checked,))
        result = reporting.daily(self.database, {}, "2026-09-18T01:00:00+00:00")
        self.assertEqual(result["last_provider_check"], checked)
        self.assertEqual(result["provider_observation"]["freshness"], "RECENT_SAVED")
        self.assertFalse(result["provider_observation"]["checked_by_this_report"])
        self.assertEqual(result["replies"]["records"][0]["inference"]["sentiment"], "ambiguous")
        self.assertEqual(result["followups_due"]["records"][0]["blocker"], "REPLY_REQUIRES_CONTEXT")

    def test_four_saved_baseline_priorities_keep_year_unknown_freshness_and_no_attribution(self):
        import conversion
        conversion.initialize(self.database)
        conversion.seed_baseline_priorities(self.database, Path(__file__).resolve().parent / "fixtures/handoff/REQUIREMENTS.md", fixtures.NOW)
        result = reporting.daily(self.database, {}, "2026-09-18T01:00:00+00:00")
        priorities = result["baseline_priorities"]["records"]
        self.assertEqual(len(priorities), 4)
        self.assertTrue(all(row["date"].startswith("2026-") and row["confirmation_freshness"] == "UNKNOWN" and not row["attributed"] for row in priorities))
        body = reporting.full_report(self.database, "2026-09-17")
        for company in ("Northwind", "Contoso", "Fabrikam", "Globex"):
            self.assertIn("Saved baseline: " + company, body)
        self.assertLessEqual(len(body.encode("utf-16-le")) // 2, 4096)
        next_year = reporting.snapshot(self.database, {}, "2027-09-18T01:00:00+00:00")
        self.assertTrue(all(row["date_status"] == "PAST_OR_UNKNOWN_REQUIRES_RECONCILIATION" for row in next_year["baseline_priorities"]["records"]))

    def test_unverified_outcome_is_not_counted(self):
        reporting.initialize(self.database)
        with self.assertRaises(ValueError):
            reporting.record_outcome(self.database, "unknown", "INTERVIEW", fixtures.NOW, True, {}, {"source": "guess"})

    def test_message_executor_configuration_never_implies_live_validation(self):
        database = sqlite3.connect(":memory:")
        self.addCleanup(database.close)
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA query_only=ON")
        enabled = {"outreach_actions_enabled": True, "outreach_agent": {"enabled": True},
                   "outreach_mode": "guarded", "outreach_allowed_purposes": ["INITIAL", "FIRST_MESSAGE"]}
        variants = [({}, "DISABLED"),
                    ({"outreach_actions_enabled": True}, "OUTREACH_AGENT_CONNECTION_DISABLED"),
                    (dict(enabled, outreach_allowed_purposes=["PREPARE_CAMPAIGN"]), "NO_ENABLED_MESSAGE_SCOPE"),
                    (dict(enabled, outreach_allowed_purposes="INITIAL"), "NO_ENABLED_MESSAGE_SCOPE"),
                    (dict(enabled, outreach_mode="paused"), "NO_ENABLED_MESSAGE_SCOPE"),
                    (enabled, "CONFIGURED_REQUIRES_PER_ACTION_VALIDATION")]
        for config, expected in variants:
            with self.subTest(state=expected, config=config):
                before = json.dumps(config, sort_keys=True)
                gate = reporting.message_executor_gate(database, config)
                self.assertEqual(gate["state"], expected)
                self.assertFalse(gate["checked_by_this_report"])
                self.assertFalse(gate["confirmed_not_sent_automatic_resend_supported"])
                self.assertEqual(gate["validation"], "NOT_EVALUATED_BY_REPORT")
                self.assertIsNone(gate["read_retries"]["records"])
                self.assertIsNone(gate["draft_investigations"]["records"])
                self.assertEqual(json.dumps(config, sort_keys=True), before)
        self.assertEqual(database.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0], 0)

    def test_saved_message_blockers_and_draft_tasks_reach_report_without_raw_audit(self):
        import outreach
        import workflow_tasks
        outreach.initialize_actions(self.database)
        workflow_tasks.initialize(self.database)
        retry = "2026-09-18T01:15:00+00:00"
        deadline = "2026-09-19T01:00:00+00:00"
        self.database.execute("INSERT INTO outreach_read_retries VALUES (?,?,'RETRY_WAIT',1,?,?,NULL,NULL,?)",
                              ("mock-action", "RECONCILIATION", retry, "INDEPENDENT_RECEIPT_PENDING", fixtures.NOW))
        task_id = workflow_tasks.enqueue(self.database, "outreach-draft-inspection", "mock-draft", "mock-parent",
                                          [{"source": "PRIVATE_RAW_SENTINEL", "observed_at": fixtures.NOW}],
                                          deadline, fixtures.NOW)
        workflow_tasks.enqueue(self.database, "unrelated-worker", "other", "mock-parent",
                               [{"source": "mock", "observed_at": fixtures.NOW}], deadline, fixtures.NOW)
        self.database.execute("UPDATE workflow_tasks SET status='RETRY_WAIT',attempts=1,blocker='ENTITLEMENT_UNKNOWN',next_at=?,next_action='Investigate saved evidence',result=? WHERE task_id=?",
                              (retry, json.dumps({"raw": "PRIVATE_RAW_SENTINEL"}), task_id))
        self.database.execute("CREATE TABLE outreach_agent_audit (record TEXT)")
        self.database.execute("INSERT INTO outreach_agent_audit VALUES ('PRIVATE_RAW_SENTINEL')")
        config = {"outreach_actions_enabled": True, "outreach_agent": {"enabled": True},
                  "outreach_mode": "guarded", "outreach_allowed_purposes": ["INITIAL", "FIRST_MESSAGE"]}
        before = [tuple(row) for row in self.database.execute("SELECT * FROM outreach_read_retries")]
        with patch("orchestrator.HeyReach", side_effect=AssertionError("Report must not call provider")):
            payload = reporting.snapshot(self.database, config, "2026-09-18T01:00:00+00:00")
        gate = payload["message_executor_gate"]
        self.assertEqual(gate["supported_purposes"], ["INITIAL", "FIRST_MESSAGE"])
        self.assertEqual(gate["read_retries"]["records"][0]["last_error"], "INDEPENDENT_RECEIPT_PENDING")
        self.assertEqual(gate["read_retries"]["records"][0]["next_at"], retry)
        self.assertEqual(len(gate["draft_investigations"]["records"]), 1)
        self.assertEqual(gate["draft_investigations"]["records"][0]["task_id"], task_id)
        self.assertEqual(gate["draft_investigations"]["records"][0]["deadline"], deadline)
        self.assertNotIn("PRIVATE_RAW_SENTINEL", json.dumps(gate))
        self.assertEqual(before, [tuple(row) for row in self.database.execute("SELECT * FROM outreach_read_retries")])
        body = reporting.telegram_body(dict(payload, day="2026-09-17"))
        for detail in ("Message executor: CONFIGURED_REQUIRES_PER_ACTION_VALIDATION", "connection enabled=True",
                       "INDEPENDENT_RECEIPT_PENDING", "ENTITLEMENT_UNKNOWN", retry, deadline,
                       "No live check performed", "CONFIRMED_NOT_SENT automatic resends unsupported"):
            self.assertIn(detail, body)
        self.assertNotIn("PRIVATE_RAW_SENTINEL", body)
        self.assertLessEqual(len(body.encode("utf-16-le")) // 2, 4096)

    def test_old_saved_telegram_payload_has_unknown_message_executor(self):
        payload = reporting.snapshot(self.database, {}, fixtures.NOW)
        payload["day"] = "2026-09-17"
        payload.pop("message_executor_gate")
        body = reporting.telegram_body(payload)
        self.assertIn("Message executor: UNKNOWN_SAVED_PAYLOAD", body)
        self.assertIn("Saved message read records: unknown", body)
        self.assertIn("Saved draft investigation records: unknown", body)

    def test_ack_draft_and_unproven_sent_state_are_not_receipts(self):
        import outreach
        outreach.initialize_actions(self.database)
        for index, state in enumerate(("ACKNOWLEDGED_UNCONFIRMED", "CONFIRMED_SENT", "CONFIRMED_NOT_SENT")):
            self.database.execute("INSERT INTO outreach_actions VALUES (?,?,?,'{}','unverified',?,?,'{}')",
                                  ("mock-action-" + str(index), "mock-identity-" + str(index), "INITIAL", state, fixtures.NOW))
        self.database.execute("INSERT INTO drafts VALUES ('mock-draft','Mock','Mock Company','HELD_VERIFICATION','{}')")
        payload = reporting.snapshot(self.database, {}, fixtures.NOW)
        self.assertEqual(payload["executed_actions"]["verified_message_actions"], [])
        self.assertEqual(len(payload["message_actions"]["records"]), 3)
        self.assertFalse(payload["message_executor_gate"]["confirmed_not_sent_automatic_resend_supported"])
        body = reporting.telegram_body(dict(payload, day="2026-09-17"))
        self.assertIn("Only independent receipts confirm delivery; ACK/drafts do not", body)
