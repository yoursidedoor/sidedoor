import json
import sqlite3

import applications
import application_recovery
import reporting
import workflow_tasks


NOW = "2026-09-18T01:00:00+00:00"
CONFIG = {"timezone": "America/Los_Angeles", "reporting_owner_confirmed": True}


def fixture():
    database = sqlite3.connect(":memory:")
    database.row_factory = sqlite3.Row
    applications.initialize(database)
    workflow_tasks.initialize(database)
    reporting.initialize(database)
    database.execute("CREATE TABLE research (id TEXT PRIMARY KEY, started_at TEXT, state TEXT, result TEXT)")
    for key in ("abandoned", "uncertain", "failed", "recovered"):
        database.execute("""INSERT INTO application_jobs
            (job_key,employer,requisition,payload,state,attempts,blocker,next_action,updated_at)
            VALUES (?,?,?,'{}',?,2,?,?,?)""",
            (key, "Mock employer", key, "RECONCILE" if key == "uncertain" else "ABANDONED" if key == "abandoned" else "BLOCKED",
             "MOCK_SOURCE_UNAVAILABLE", "Review saved mock evidence", NOW))
    database.execute("INSERT INTO application_attempts (attempt_id,job_key,idempotency_key,state,started_at) VALUES ('uncertain-attempt','uncertain','uncertain','SUBMITTING',?)", (NOW,))
    events = [
        ("failed", "2026-09-11T00:59:59+00:00", "BLOCKED"),
        ("failed", "2026-09-11T01:00:00+00:00", "BLOCKED"),
        ("failed", "2026-09-17T06:59:59+00:00", "BLOCKED"),
        ("failed", "2026-09-17T07:00:00+00:00", "RETRY_WAIT"),
        ("abandoned", NOW, "ABANDONED"),
        ("abandoned", NOW, "APPLICATION_ABANDONED"),
        ("uncertain", NOW, "ABANDONED"),
        ("recovered", NOW, "APPLICATION_RECOVERY_INVESTIGATED"),
        ("recovered", NOW, "APPLICATION_RECOVERY_RESOLVED"),
        ("failed", "2026-09-18T01:00:01+00:00", "BLOCKED"),
    ]
    for key, at, event in events:
        applications.event(database, key, event, {"reason": "Mock route exhausted", "attempts": 2,
            "evidence": {"source": "mock-log", "observed_at": at}, "next_action": "Review mock evidence before any new work"}, at)
    failure = {"observed_at": "2026-09-17T15:00:00+00:00", "attempt": 1,
               "diagnostic": "Mock research source unavailable", "report": "mock-private-report"}
    result = {"day": "2026-09-17", "industries": [{"industry": "Mock industry", "state": "FAILED",
        "recovery_state": "ABANDONED", "abandonment_reason": "NO_SUPPORTED_ALTERNATIVE", "failures": [failure],
        "recovery_decisions": [{"observed_at": "2026-09-17T16:00:00+00:00",
                                "decision": {"action": "ABANDON", "reason": "Mock alternatives exhausted"}}]}]}
    for identifier, at in (("research-original", "2026-09-17T16:00:00+00:00"), ("research-replay", NOW)):
        database.execute("INSERT INTO research VALUES (?,?,'FAILED',?)", (identifier, at, json.dumps(result)))
    database.commit()
    return database


def run():
    database = fixture()
    try:
        before = database.total_changes
        saved = reporting.bounded_recovery(database, CONFIG, NOW)
        assert database.total_changes == before
        assert saved["daily"]["counts"]["application"] == {"abandoned": 1, "failures": 1, "recovery_attempts": 1, "recovered": 1}
        assert saved["rolling_week"]["counts"]["application"]["failures"] == 1
        assert saved["daily"]["counts"]["research"] == {"abandoned": 1, "failures": 1, "recovery_attempts": 1, "recovered": 0}
        assert saved["unresolved_submissions"]["count"] == 1
        uncertain = next(row for row in saved["records"] if row.get("job_key") == "uncertain")
        assert uncertain["state"] == "UNRESOLVED_SUBMISSION"
        assert uncertain["evidence"]["reason"] == "Mock route exhausted"
        assert "before any retry" in uncertain["next_action"]
        payload = reporting.daily(database, CONFIG, NOW)
        assert payload["verified_submissions"] == 0
        frozen = tuple(database.execute("SELECT payload FROM daily_reports").fetchone())
        sent = database.execute("SELECT message FROM notification_outbox WHERE event_key='daily-report:2026-09-17'").fetchone()[0]
        message = reporting.full_report(database, "2026-09-17")
        assert "Rolling 7 days" in message and "Mock route exhausted" in message
        assert len(message.encode("utf-16-le")) // 2 <= 4096
        database.execute("UPDATE notification_outbox SET state='RETRY_WAIT',attempts=1,last_error='mock delivery unavailable' WHERE event_key='daily-report:2026-09-17'")
        applications.event(database, "failed", "ABANDONED", {"reason": "After the frozen report", "next_action": "Retain mock evidence"}, "2026-09-18T02:00:00+00:00")
        database.commit()
        assert reporting.daily(database, CONFIG, "2026-09-18T03:00:00+00:00") == payload
        assert tuple(database.execute("SELECT payload FROM daily_reports").fetchone()) == frozen
        assert database.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 1
        notification = reporting.notification_status(database, "daily-report:2026-09-17")
        assert notification["state"] == "RETRY_WAIT" and notification["last_error"] == "mock delivery unavailable"
        assert database.execute("SELECT message FROM notification_outbox").fetchone()[0] == sent
        current = reporting.snapshot(database, CONFIG, "2026-09-18T03:00:00+00:00")
        assert current["bounded_recovery"]["daily"]["counts"]["application"]["abandoned"] == 2
        midnight = reporting.bounded_recovery(database, CONFIG, "2026-09-18T07:00:00+00:00")
        assert midnight["daily"]["counts"]["application"]["abandoned"] == 0
        assert midnight["rolling_week"]["counts"]["application"]["abandoned"] == 2
        following = reporting.daily(database, CONFIG, "2026-09-19T01:00:00+00:00")
        assert following["bounded_recovery"]["rolling_week"]["counts"]["application"]["abandoned"] == 2
        assert database.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 2
        following_message = database.execute("SELECT message FROM notification_outbox WHERE event_key='daily-report:2026-09-18'").fetchone()[0]
        assert "After the frozen report" in following_message
        for now, start in (("2026-03-09T01:00:00+00:00", "2026-03-08T08:00:00+00:00"),
                           ("2026-11-02T02:00:00+00:00", "2026-11-01T07:00:00+00:00")):
            result = reporting.bounded_recovery(database, CONFIG, now)
            assert result["daily"]["start_inclusive"] == start
        empty = sqlite3.connect(":memory:")
        try:
            unknown = reporting.bounded_recovery(empty, CONFIG, NOW)
            assert unknown["daily"]["counts"]["application"]["abandoned"] is None
            assert unknown["rolling_week"]["counts"]["research"]["recovered"] is None
            assert unknown["unresolved_submissions"]["count"] is None
            assert not empty.execute("SELECT name FROM sqlite_master").fetchall()
        finally:
            empty.close()
        lifecycle_database = fixture()
        try:
            class MockInvestigator:
                def recovery_decision(self, packet):
                    if packet["recovery"]["previous_attempts"]:
                        return {"action": "ABANDON", "reason": "Mock supported routes exhausted", "progress": False}
                    return {"action": "RETRY", "method": "official_posting", "instructions": "Read mock official posting",
                            "reason": "Mock AI coordinator chooses an untried supported read", "progress": False}

                def call(self, operation, packet):
                    assert operation == "investigate" and packet["read_only"] is True
                    return {"status": "ABANDON", "reason": "Mock supported routes exhausted",
                            "evidence": {"source": "mock-official-page", "observed_at": NOW,
                                         "verified": True, "independent_read": True, "job_key": "failed"}}

            job = dict(lifecycle_database.execute("SELECT * FROM application_jobs WHERE job_key='failed'").fetchone(),
                       candidate_email="mock@example.invalid")
            config = dict(CONFIG, application_recovery={"enabled": True, "max_attempts": 3})
            application_recovery.recover(lifecycle_database, job, MockInvestigator(), config,
                                         "REQUISITION_MISMATCH", "Read mock official posting", NOW)
            job = dict(lifecycle_database.execute("SELECT * FROM application_jobs WHERE job_key='failed'").fetchone(),
                       candidate_email="mock@example.invalid")
            application_recovery.recover(lifecycle_database, job, MockInvestigator(), config,
                                         "REQUISITION_MISMATCH", "Review mock findings", NOW)
            integrated = reporting.bounded_recovery(lifecycle_database, CONFIG, NOW)
            assert integrated["daily"]["counts"]["application"]["abandoned"] == 2
            assert integrated["daily"]["counts"]["application"]["recovery_attempts"] == 2
            abandonment = next(row for row in integrated["records"] if row.get("job_key") == "failed" and row["category"] == "abandoned")
            assert abandonment["attempts"] == 1 and abandonment["max_attempts"] == 3
            assert abandonment["reason"] == "Mock supported routes exhausted"
            coordinator = next(row for row in integrated["records"] if row["category"] == "coordinator_decision")
            assert coordinator["reason"] == "Mock AI coordinator chooses an untried supported read"
            applications.event(lifecycle_database, "recovered", "APPLICATION_RECOVERY_DECISION_FAILED", {
                "attempt": 2, "reason": "Mock coordinator unavailable", "deadline": "2026-09-20T01:00:00+00:00"}, NOW)
            failures = reporting.bounded_recovery(lifecycle_database, CONFIG, NOW)
            assert any(row["reason"] == "Mock coordinator unavailable" and row["category"] == "failures" for row in failures["records"])
        finally:
            lifecycle_database.close()
        return {"state": "PASS", "evidence": "In-memory synthetic fixtures only; no network or production writes",
                "validated": ["daily and rolling 168-hour boundaries", "local midnight and both DST transitions",
                              "reason, attempts, evidence, next action", "research replay deduplication",
                              "unresolved claims remain visible", "no invented submission success",
                              "immutable daily payload and message", "pending notification preserved",
                              "current local snapshot and next scheduled report", "missing tables stay unknown",
                              "actual lifecycle event contract; duplicate abandonment transitions counted once"],
                "daily": saved["daily"], "rolling_week": saved["rolling_week"]}
    finally:
        database.close()


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
