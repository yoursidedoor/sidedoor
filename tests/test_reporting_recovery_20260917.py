import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
import json
import sqlite3
import unittest

import applications
import manual_reporting_recovery_20260917 as manual
import reporting


class BoundedRecoveryReportingTests(unittest.TestCase):
    def setUp(self):
        self.database = manual.fixture()
        self.addCleanup(self.database.close)

    def snapshot(self, now=manual.NOW, config=manual.CONFIG):
        return reporting.bounded_recovery(self.database, config, now)

    def test_manually_validated_lifecycle_reporting_and_immutable_delivery(self):
        self.assertEqual(manual.run()["state"], "PASS")

    def test_rolling_week_is_168_hours_and_local_day_uses_configured_timezone(self):
        self.database.execute("DELETE FROM application_events")
        for key, at in (("outside", "2026-09-11T00:59:59+00:00"),
                        ("boundary", "2026-09-11T01:00:00+00:00"),
                        ("utc-today", "2026-09-18T00:00:00+00:00"),
                        ("now", manual.NOW), ("future", "2026-09-18T01:00:01+00:00")):
            applications.event(self.database, key, "APPLICATION_ABANDONED", {"reason": key}, at)
        result = self.snapshot(config={"timezone": "Asia/Tokyo"})
        self.assertEqual(result["daily"]["start_inclusive"], "2026-09-17T15:00:00+00:00")
        self.assertEqual(result["daily"]["counts"]["application"]["abandoned"], 2)
        self.assertEqual(result["rolling_week"]["counts"]["application"]["abandoned"], 3)
        for now in ("2026-03-09T01:00:00+00:00", "2026-11-02T02:00:00+00:00"):
            window = self.snapshot(now)["rolling_week"]
            duration = dt.datetime.fromisoformat(window["end_inclusive"]) - dt.datetime.fromisoformat(window["start_inclusive"])
            self.assertEqual(duration.total_seconds(), 168 * 3600)

    def test_abandonment_keeps_rich_evidence_and_unresolved_claims_are_separate(self):
        result = self.snapshot()
        rows = [row for row in result["records"] if row.get("job_key") == "abandoned"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reason"], "Mock route exhausted")
        self.assertEqual(rows[0]["evidence"]["evidence"]["source"], "mock-log")
        self.assertEqual(rows[0]["attempts"], 2)
        self.assertEqual(result["unresolved_submissions"]["records"][0]["attempt_ids"], ["uncertain-attempt"])
        self.assertEqual(result["daily"]["counts"]["application"]["abandoned"], 1)
        self.assertEqual(result["daily"]["counts"]["research"]["recovered"], 0)

    def test_new_section_is_read_only_and_missing_optional_schemas_stay_unknown(self):
        for schema in (None, "CREATE TABLE application_events (id INTEGER)"):
            with self.subTest(schema=schema):
                database = sqlite3.connect(":memory:")
                self.addCleanup(database.close)
                if schema:
                    database.execute(schema)
                before = database.serialize()
                result = reporting.bounded_recovery(database, manual.CONFIG, manual.NOW)
                self.assertEqual(before, database.serialize())
                self.assertIsNone(result["rolling_week"]["counts"]["application"]["abandoned"])
                self.assertIsNone(result["daily"]["counts"]["research"]["failures"])
                self.assertIsNone(result["unresolved_submissions"]["count"])

    def test_replayed_research_decision_does_not_double_count_after_result_changes(self):
        saved = json.loads(self.database.execute("SELECT result FROM research WHERE id='research-replay'").fetchone()[0])
        saved["industries"][0]["recovery_decisions"][0]["decision"]["reason"] = "Updated saved decision diagnostic"
        saved["industries"][0]["reused"] = True
        self.database.execute("UPDATE research SET result=? WHERE id='research-replay'", (json.dumps(saved),))
        result = self.snapshot()
        self.assertEqual(result["daily"]["counts"]["research"]["recovery_attempts"], 1)
        self.assertEqual(result["daily"]["counts"]["research"]["abandoned"], 1)
        decision = next(row for row in result["records"] if row["domain"] == "research" and row["category"] == "recovery_attempts")
        self.assertEqual(decision["reason"], "Updated saved decision diagnostic")

    def test_old_research_without_industry_schema_only_affects_its_window(self):
        self.database.execute("INSERT INTO research VALUES ('old','2026-08-01T01:00:00+00:00','FAILED','{}')")
        self.assertEqual(self.snapshot()["daily"]["counts"]["research"]["abandoned"], 1)
        self.database.execute("INSERT INTO research VALUES ('unknown',?,'FAILED','{}')", (manual.NOW,))
        self.assertIsNone(self.snapshot()["daily"]["counts"]["research"]["abandoned"])

    def test_failed_investigation_and_rejected_resolution_never_invent_recovery(self):
        self.database.execute("DELETE FROM application_events")
        applications.event(self.database, "failed", "APPLICATION_RECOVERY_INVESTIGATED",
                           {"attempt": 1, "reason": "Mock timeout", "result": {"status": "FAILED"}}, manual.NOW)
        applications.event(self.database, "failed", "APPLICATION_RECOVERY_RESOLUTION_REJECTED",
                           {"attempt": 2, "reason": "UNVERIFIED_RESOLUTION"}, manual.NOW)
        counts = self.snapshot()["daily"]["counts"]["application"]
        self.assertEqual(counts, {"abandoned": 0, "failures": 1, "recovery_attempts": 1, "recovered": 0})

    def test_missing_event_time_is_visible_but_not_reported_as_zero_abandonments(self):
        applications.event(self.database, "undated", "APPLICATION_ABANDONED", {"reason": "Missing saved timestamp"}, "invalid")
        result = self.snapshot()
        self.assertIsNone(result["daily"]["counts"]["application"]["abandoned"])
        self.assertIsNone(result["rolling_week"]["counts"]["application"]["abandoned"])
        self.assertEqual(result["undated_records"], 1)
        self.assertIn("Missing saved timestamp", json.dumps(result["records"]))

    def test_coordinator_decision_reason_and_failure_remain_distinct_from_success(self):
        self.database.execute("DELETE FROM application_events")
        applications.event(self.database, "failed", "APPLICATION_RECOVERY_DECIDED", {
            "attempt": 1, "decision": {"action": "RETRY", "reason": "Mock new evidence route",
                                        "instructions": "Inspect the mock official page", "source": "mock-ai-audit"}}, manual.NOW)
        applications.event(self.database, "failed", "APPLICATION_RECOVERY_DECISION_FAILED", {
            "attempt": 2, "reason": "Mock coordinator unavailable", "deadline": "2026-09-20T01:00:00+00:00"}, manual.NOW)
        result = self.snapshot()
        records = [row for row in result["records"] if row["domain"] == "application"]
        self.assertEqual(len(records), 2)
        decision = next(row for row in records if row["category"] == "coordinator_decision")
        self.assertEqual(decision["reason"], "Mock new evidence route")
        self.assertEqual(decision["next_action"], "Inspect the mock official page")
        self.assertEqual(decision["evidence"]["decision"]["source"], "mock-ai-audit")
        self.assertEqual(result["daily"]["counts"]["application"],
                         {"abandoned": 0, "failures": 1, "recovery_attempts": 0, "recovered": 0})

    def test_future_research_snapshot_cannot_change_present_reason_or_counts(self):
        saved = json.loads(self.database.execute("SELECT result FROM research WHERE id='research-original'").fetchone()[0])
        saved["industries"][0]["abandonment_reason"] = "Future result"
        self.database.execute("INSERT INTO research VALUES ('future','2026-09-19T01:00:00+00:00','FAILED',?)", (json.dumps(saved),))
        result = self.snapshot()
        self.assertNotIn("Future result", json.dumps(result))
        self.assertEqual(result["daily"]["counts"]["research"]["abandoned"], 1)

    def test_telegram_keeps_counts_and_actionable_reason_before_long_other_sections(self):
        payload = reporting.snapshot(self.database, manual.CONFIG, manual.NOW)
        payload["day"] = "2026-09-17"
        payload["outstanding_actions"] = [{"job_key": "mock-long", "required_input": "😀" * 8000}]
        parts = reporting.telegram_parts(payload)
        for detail in ("Today saved recovery", "Rolling 7 days", "Unresolved submission claims: 1",
                       "Mock route exhausted", "attempts=2", "evidence=application_events:", "next="):
            self.assertIn(detail, parts[0])
        self.assertIn("Full unabridged report", parts[-1])
        self.assertNotIn("[summary truncated]", "".join(parts))
        for part in parts:
            self.assertLessEqual(len(part.encode("utf-16-le")) // 2, reporting.PART_LIMIT)


if __name__ == "__main__":
    unittest.main()
