"""Reporting must describe the new durable application recovery events truthfully.

Three event names reach `reporting.bounded_recovery` from patches that were in flight when
this was written, and root's category map knew none of them:

* `APPLICATION_REOPENED` (H)                     - an explicitly authorized reopen of an
                                                   ABANDONED job;
* `APPLICATION_RECOVERY_CLAIMS_EXHAUSTED` (D)    - the bounded-exhaustion park: a reviewable
                                                   BLOCKED hold, never a terminal abandonment;
* `APPLICATION_RECOVERY_HOLD_REVIEWED` (I)       - one bounded AI review of that park, which
                                                   either RESUMES it or ABANDONS it, plus its
                                                   sibling `..._HOLD_REVIEW_FAILED`.

`ReopenReportingTests` covers the reopen. `RecoveryHoldEventReportingTests` covers the park,
its review, and the rule that a genuinely unknown future event name is skipped without
changing any count.

---

The owner's reopen tool (not part of the product) appended a
durable `APPLICATION_REOPENED` event, wrote a bounded expiring job-scoped grant, and flipped
the job row back to QUEUED. It never deleted the original `APPLICATION_ABANDONED` record.

These tests pin the reporting contract for that shape:

* the reopen is visible in current status and in the consolidated daily/rolling-week report;
* the original abandonment stays visible as history and is annotated, never erased;
* an expired grant, a job that has since become terminal again, and an absent job row are
  each reported as they actually are - never as success and never as an invented state;
* a fixture with no reopen events keeps every existing category, count and record key.

Nothing here writes outside its own temporary fixture. No adapter, provider or notification
send is exercised: `applications.notify` only inserts an outbox row.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import json
import unittest

import applications
import reporting
import test_applications_e2e as fixtures


NOW = fixtures.NOW                              # 2026-09-17T16:00:00+00:00
REPORT_AT = "2026-09-18T01:00:00+00:00"         # 18:00 America/Los_Angeles on 2026-09-17
ABANDONED_AT = "2026-09-17T15:00:00+00:00"
REOPENED_AT = "2026-09-17T15:30:00+00:00"
AUTHORIZATION = "user-authorized-reopen-example-1"
AUTHORIZED_BY = "user:explicit-instruction:2026-09-18"
EXPIRED_AT = "2026-09-17T15:45:00+00:00"        # already past at REPORT_AT
UNEXPIRED_AT = "2026-09-19T15:30:00+00:00"      # the ordinary 48-hour bound

GRANT_SCHEMA = """
CREATE TABLE IF NOT EXISTS application_reopen_grants (
    authorization_id TEXT PRIMARY KEY, authorized_by TEXT NOT NULL, job_keys TEXT NOT NULL,
    investigation_claims INTEGER NOT NULL, deadline_hours REAL NOT NULL,
    authorized_at TEXT NOT NULL, expires_at TEXT NOT NULL, authorization TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS application_reopen_grant_jobs (
    authorization_id TEXT NOT NULL, job_key TEXT NOT NULL, reopened_at TEXT NOT NULL,
    reopen_event_id INTEGER NOT NULL, defect_classes TEXT NOT NULL,
    original_abandoned_at TEXT NOT NULL, original_reason TEXT NOT NULL,
    original_attempts INTEGER NOT NULL, deadline TEXT NOT NULL,
    PRIMARY KEY(authorization_id,job_key));
"""

ROOT_RECORD_FIELDS = {
    "domain", "task_id", "job_key", "category", "state", "at", "time_basis", "reason",
    "attempts", "attempt_basis", "max_attempts", "deadline", "next_action", "evidence",
    "source", "source_id", "workflow_evidence", "unresolved_submission"}


class ReopenReportingTests(unittest.TestCase):
    setUp = fixtures.ApplicationEndToEndTests.setUp
    maxDiff = None

    # -- fixture helpers -------------------------------------------------------------

    def seed_abandoned(self, key="wingtip:role-1", employer="Wingtip", requisition="role-1",
                       reason="RECOVERY_ATTEMPTS_EXHAUSTED", attempts=3):
        self.database.execute(
            "INSERT INTO application_jobs(job_key,employer,requisition,payload,state,attempts,blocker,next_action,updated_at)"
            " VALUES (?,?,?,?,'ABANDONED',?,'APPLICATION_RECOVERY_ABANDONED',?,?)",
            (key, employer, requisition, json.dumps({"url": "https://example.invalid/" + requisition}), attempts,
             "Retain terminal abandonment and its evidence; do not automatically reopen", ABANDONED_AT))
        applications.event(self.database, key, "APPLICATION_ABANDONED", {
            "reason": reason, "attempts": attempts, "max_attempts": 3, "terminal": True,
            "submission_status": "NOT_SUBMITTED_BY_RECOVERY", "blocker": "ELIGIBILITY_UNKNOWN",
            "next_action": "Retain terminal abandonment and its evidence; do not automatically reopen"}, ABANDONED_AT)
        return key

    def seed_reopen(self, key, expires_at=UNEXPIRED_AT, state="QUEUED", blocker=None,
                    grant_tables=True, authorization=AUTHORIZATION, at=REOPENED_AT):
        detail = {
            "authorization_id": authorization, "authorized_by": AUTHORIZED_BY,
            "authorization": "User explicitly instructed on 2026-09-18 to reopen the deterministically abandoned jobs; "
                             "every original abandonment record, reason, evidence, compute charge and attempt count is retained.",
            "reason": {"defect_classes": ["CAP_VS_JUDGMENT_CONFLATION"]},
            "original_abandonment": {"at": ABANDONED_AT, "attempts": 3, "reason": "RECOVERY_ATTEMPTS_EXHAUSTED",
                                     "preserved": "retained verbatim; this reopen only appends"},
            "allowance": {"investigation_claims": 3, "deadline_hours": 48.0, "expires_at": expires_at},
            "previous_state": {"state": "ABANDONED", "blocker": "APPLICATION_RECOVERY_ABANDONED"},
            "new_state": {"state": state, "blocker": blocker},
            "submission_status": "NOT_SUBMITTED", "history_preserved": True}
        applications.event(self.database, key, "APPLICATION_REOPENED", detail, at)
        event_id = self.database.execute("SELECT max(id) FROM application_events").fetchone()[0]
        self.database.execute("UPDATE application_jobs SET state=?,blocker=?,next_action=?,updated_at=? WHERE job_key=?",
                              (state, blocker, "Resume ordinary application checks under the recorded reopen authorization", at, key))
        if grant_tables:
            self.database.executescript(GRANT_SCHEMA)
            self.database.execute("INSERT OR IGNORE INTO application_reopen_grants VALUES (?,?,?,?,?,?,?,?)",
                                  (authorization, AUTHORIZED_BY, json.dumps([key]), 3, 48.0, at, expires_at,
                                   "Explicit user authorization recorded 2026-09-18"))
            self.database.execute("INSERT OR IGNORE INTO application_reopen_grant_jobs VALUES (?,?,?,?,?,?,?,?,?)",
                                  (authorization, key, at, event_id, json.dumps(["CAP_VS_JUDGMENT_CONFLATION"]),
                                   ABANDONED_AT, "RECOVERY_ATTEMPTS_EXHAUSTED", 3, expires_at))
        return event_id

    def snapshot(self, now=REPORT_AT):
        return reporting.snapshot(self.database, {}, now)

    def recovery(self, now=REPORT_AT):
        return reporting.bounded_recovery(self.database, {}, now)

    def reopen_record(self, payload, key):
        return next(row for row in payload["authorized_reopens"]["records"] if row["job_key"] == key)

    # -- 1. the reopen is visible, and the abandonment is still history --------------

    def test_authorized_reopen_is_visible_in_status_daily_report_and_keeps_abandonment_history(self):
        key = self.seed_abandoned()
        self.seed_reopen(key)
        payload = reporting.daily(self.database, {}, REPORT_AT)

        # Current status: the reopen is stated explicitly, with its authorization.
        current = self.reopen_record(payload, key)
        self.assertEqual(current["authorization_id"], AUTHORIZATION)
        self.assertEqual(current["authorized_by"], AUTHORIZED_BY)
        self.assertEqual(current["authorization_state"], "AUTHORIZATION_ACTIVE")
        self.assertEqual(current["authorization_basis"], "application_reopen_grants")
        self.assertIs(current["grant_recorded"], True)
        self.assertEqual(current["current_state"], "QUEUED")
        self.assertIs(current["currently_terminal"], False)
        self.assertIs(current["verified_submission"], False)
        self.assertEqual(payload["application_states"], {"QUEUED": 1})
        self.assertEqual(payload["authorized_reopens"]["counts"],
                         {"reopens": 1, "authorization_active": 1, "currently_terminal": 0, "current_state_unknown": 0})

        # The original abandonment is still history: its event, its own record, its count.
        self.assertIs(current["abandonment_history_preserved"], True)
        self.assertEqual([row["reason"] for row in current["original_abandonment_records"]],
                         ["RECOVERY_ATTEMPTS_EXHAUSTED"])
        self.assertEqual(self.database.execute(
            "SELECT count(*) FROM application_events WHERE job_key=? AND event='APPLICATION_ABANDONED'",
            (key,)).fetchone()[0], 1)
        recovery = payload["bounded_recovery"]
        abandoned = [row for row in recovery["records"] if row["category"] == "abandoned"]
        self.assertEqual(len(abandoned), 1)
        self.assertEqual(abandoned[0]["reason"], "RECOVERY_ATTEMPTS_EXHAUSTED")
        self.assertEqual(recovery["daily"]["counts"]["application"]["abandoned"], 1)
        self.assertEqual(recovery["rolling_week"]["counts"]["application"]["abandoned"], 1)

        # ... and it is annotated as superseded, not silently left implying a dead job.
        self.assertEqual(abandoned[0]["superseded_by_authorized_reopen"]["authorization_id"], AUTHORIZATION)
        self.assertIn("retained as history", abandoned[0]["next_action"])
        self.assertIn("reopened under recorded user authorization", abandoned[0]["next_action"])

        # Consolidated daily/rolling-week recovery reporting carries the reopen itself.
        reopened = [row for row in recovery["records"] if row["category"] == "reopened"]
        self.assertEqual(len(reopened), 1)
        self.assertEqual(reopened[0]["job_key"], key)
        self.assertEqual(reopened[0]["state"], "APPLICATION_REOPENED")
        self.assertIn("authorization AUTHORIZATION_ACTIVE", reopened[0]["reason"])
        self.assertIn("the original abandonment record is retained", reopened[0]["reason"])
        self.assertEqual(recovery["authorized_reopens"]["daily"], 1)
        self.assertEqual(recovery["authorized_reopens"]["rolling_week"], 1)

        # A reopen is never counted as recovery success or a submission.
        self.assertEqual(recovery["daily"]["counts"]["application"]["recovered"], 0)
        self.assertEqual(payload["verified_submissions"], 0)
        self.assertEqual(payload["verified_submission_keys"], [])

        # The consolidated Telegram daily body states it, and still states the abandonment.
        body = reporting.full_report(self.database, "2026-09-17")
        self.assertIn("Authorized reopens of abandoned jobs: 1 saved (today 1; rolling 7 days 1)", body)
        self.assertIn("authorization active=1", body)
        self.assertIn("Each original abandonment record is retained; a reopen is not a submission.", body)
        self.assertIn("Reopen " + key + ": AUTHORIZATION_ACTIVE", body)
        self.assertIn("abandonment retained=True", body)
        self.assertIn("application abandoned=1", body)
        self.assertLessEqual(len(body.encode("utf-16-le")) // 2, 4096)

    def test_reopened_job_is_never_reported_as_successful_or_as_never_abandoned(self):
        key = self.seed_abandoned()
        self.seed_reopen(key)
        recovery = self.recovery()
        categories = {row["category"] for row in recovery["records"] if row["job_key"] == key}
        self.assertEqual(categories, {"abandoned", "reopened"})
        self.assertNotIn("recovered", categories)
        self.assertEqual(recovery["daily"]["counts"]["application"],
                         {"abandoned": 1, "failures": 0, "recovery_attempts": 0, "recovered": 0})
        self.assertEqual(self.snapshot()["executed_actions"]["verified_applications"], [])

    # -- 2. unexpired vs expired grants ---------------------------------------------

    def grant_case(self, expires_at, expected):
        key = self.seed_abandoned()
        self.seed_reopen(key, expires_at=expires_at)
        payload = reporting.daily(self.database, {}, REPORT_AT)
        record = self.reopen_record(payload, key)
        self.assertEqual(record["authorization_state"], expected)
        counts = payload["authorized_reopens"]["counts"]
        self.assertEqual(counts["authorization_active"], int(expected == "AUTHORIZATION_ACTIVE"))
        body = reporting.full_report(self.database, "2026-09-17")
        self.assertIn("Reopen " + key + ": " + expected, body)
        return record, body

    def test_an_unexpired_reopen_grant_is_reported_as_an_active_authorization(self):
        record, body = self.grant_case(UNEXPIRED_AT, "AUTHORIZATION_ACTIVE")
        self.assertIn("authorization active=1", body)
        self.assertIn("the original abandonment stays", record["next_action"])
        self.assertIn("not an application submission", record["next_action"])

    # -- 3. never fabricate state ----------------------------------------------------

    def test_a_job_terminal_again_after_its_reopen_is_reported_terminal_not_open(self):
        key = self.seed_abandoned()
        self.seed_reopen(key)
        applications.event(self.database, key, "APPLICATION_ABANDONED",
                           {"reason": "RECOVERY_DEADLINE_EXHAUSTED", "attempts": 3, "terminal": True},
                           "2026-09-17T15:50:00+00:00")
        self.database.execute("UPDATE application_jobs SET state='ABANDONED',blocker='APPLICATION_RECOVERY_ABANDONED' WHERE job_key=?", (key,))
        payload = self.snapshot()
        record = self.reopen_record(payload, key)
        self.assertEqual(record["current_state"], "ABANDONED")
        self.assertIs(record["currently_terminal"], True)
        self.assertEqual([row["event"] for row in record["later_terminal_or_reopen_events"]], ["APPLICATION_ABANDONED"])
        self.assertIn("reached saved terminal state ABANDONED", record["next_action"])
        self.assertIn("retain both the original abandonment and this outcome", record["next_action"])
        self.assertEqual(payload["authorized_reopens"]["counts"]["currently_terminal"], 1)
        self.assertEqual(payload["bounded_recovery"]["daily"]["counts"]["application"]["abandoned"], 1)
        self.assertEqual(len([row for row in payload["bounded_recovery"]["records"] if row["category"] == "abandoned"]), 2)

    def test_a_reopen_event_without_a_saved_job_row_keeps_the_current_state_unknown(self):
        key = self.seed_abandoned()
        self.seed_reopen(key)
        self.database.execute("DELETE FROM application_jobs WHERE job_key=?", (key,))
        record = self.reopen_record(self.snapshot(), key)
        self.assertIsNone(record["current_state"])
        self.assertIsNone(record["currently_terminal"])
        self.assertEqual(record["current_state_basis"], "UNKNOWN_JOB_ROW_ABSENT")
        self.assertIn("UNKNOWN", record["next_action"])
        self.assertIn("must not be reported as open, terminal or submitted", record["next_action"])

    def test_a_reopen_recorded_after_the_report_time_is_not_reported_yet(self):
        key = self.seed_abandoned()
        self.seed_reopen(key, at="2026-09-18T09:00:00+00:00")
        payload = self.snapshot()
        self.assertEqual(payload["authorized_reopens"]["records"], [])
        self.assertEqual(payload["bounded_recovery"]["authorized_reopens"]["daily"], 0)
        abandoned = [row for row in payload["bounded_recovery"]["records"] if row["category"] == "abandoned"]
        self.assertNotIn("superseded_by_authorized_reopen", abandoned[0])

    def test_an_abandonment_after_its_own_reopen_is_not_annotated_as_superseded_by_it(self):
        key = self.seed_abandoned()
        self.seed_reopen(key)
        applications.event(self.database, key, "APPLICATION_ABANDONED",
                           {"reason": "RECOVERY_DEADLINE_EXHAUSTED"}, "2026-09-17T15:50:00+00:00")
        records = [row for row in self.recovery()["records"] if row["category"] == "abandoned"]
        annotated = {row["reason"]: "superseded_by_authorized_reopen" in row for row in records}
        self.assertEqual(annotated, {"RECOVERY_ATTEMPTS_EXHAUSTED": True, "RECOVERY_DEADLINE_EXHAUSTED": False})

    # -- 4. nothing else changed -----------------------------------------------------

    def test_a_fixture_without_reopen_events_keeps_every_existing_category_count_and_field(self):
        key = self.seed_abandoned()
        applications.event(self.database, key, "APPLICATION_RECOVERY_STARTED",
                           {"max_attempts": 3, "deadline": "2026-09-19T15:00:00+00:00"}, NOW)
        applications.event(self.database, key, "APPLICATION_RECOVERY_INVESTIGATED",
                           {"attempts": 1, "reason": "Saved investigation"}, NOW)
        applications.event(self.database, key, "APPLICATION_RECOVERY_RESOLVED", {"reason": "Saved resolution"}, NOW)
        applications.event(self.database, key, "BLOCKED", {"code": "ELIGIBILITY_UNKNOWN"}, NOW)
        recovery = self.recovery()
        self.assertEqual(recovery["daily"]["counts"],
                         {"application": {"abandoned": 1, "failures": 1, "recovery_attempts": 1, "recovered": 1},
                          "research": {"abandoned": 0, "failures": 0, "recovery_attempts": 0, "recovered": 0}})
        self.assertEqual(recovery["rolling_week"]["counts"], recovery["daily"]["counts"])
        self.assertEqual(sorted(recovery["daily"]), ["counts", "end_inclusive", "start_inclusive"])
        self.assertEqual({row["category"] for row in recovery["records"]},
                         {"abandoned", "failures", "recovery_started", "recovery_attempts", "recovered"})
        for row in recovery["records"]:
            self.assertEqual(set(row), ROOT_RECORD_FIELDS)
        self.assertEqual(recovery["authorized_reopens"]["records"], [])
        self.assertEqual(recovery["authorized_reopens"]["daily"], 0)
        self.assertEqual(recovery["authorized_reopens"]["rolling_week"], 0)
        self.assertEqual(recovery["authorized_reopens"]["counts"]["reopens"], 0)
        body = reporting.telegram_body(dict(self.snapshot(), day="2026-09-17"))
        self.assertNotIn("Authorized reopen", body)
        self.assertNotIn("Reopen ", body)
        self.assertIn("Today saved recovery: application abandoned=1, failures=1, recovery_attempts=1, recovered=1", body)

    def test_absent_application_events_keep_the_reopen_section_unknown_without_writing(self):
        import sqlite3
        database = sqlite3.connect(":memory:")
        self.addCleanup(database.close)
        before = database.serialize()
        section = reporting.authorized_reopens(database, REPORT_AT)
        self.assertEqual(before, database.serialize())
        self.assertEqual(section["state"], "UNKNOWN_TABLE_ABSENT")
        self.assertIsNone(section["records"])
        self.assertIsNone(section["counts"])
        self.assertIsNone(reporting.bounded_recovery(database, {}, REPORT_AT)["authorized_reopens"]["daily"])

    def test_reporting_the_reopen_never_writes_to_the_saved_application_ledger(self):
        key = self.seed_abandoned()
        self.seed_reopen(key)
        before = [tuple(row) for row in self.database.execute("SELECT * FROM application_events ORDER BY id")]
        job = tuple(self.database.execute("SELECT * FROM application_jobs").fetchone())
        self.snapshot()
        self.recovery()
        self.assertEqual([tuple(row) for row in self.database.execute("SELECT * FROM application_events ORDER BY id")], before)
        self.assertEqual(tuple(self.database.execute("SELECT * FROM application_jobs").fetchone()), job)


class RecoveryHoldEventReportingTests(unittest.TestCase):
    """The parked hold, its AI review, and an unknown future event name."""

    setUp = fixtures.ApplicationEndToEndTests.setUp
    maxDiff = None

    KEY = "synthetic company:role-123"
    HOLD_BLOCKER = "APPLICATION_BROWSER_INVESTIGATION_EXHAUSTED"
    HOLD_ACTION = ("Orchestrator must review the saved recovery findings and supply new verified evidence, "
                   "an alternative official route, or an explicit abandonment; the three investigation claims "
                   "are spent and no further automatic investigation will be started")
    DEADLINE = "2026-09-19T16:33:00+00:00"

    def seed_job(self):
        self.database.execute(
            "INSERT INTO application_jobs(job_key,employer,requisition,payload,state,attempts,updated_at)"
            " VALUES (?,?,?,?,'QUEUED',0,?)",
            (self.KEY, "Synthetic Company", "role-123", json.dumps({"url": "https://example.invalid/role-123"}), NOW))
        applications.event(self.database, self.KEY, "APPLICATION_RECOVERY_STARTED",
                           {"blocker": "INSPECTION_INCOMPLETE", "started_at": NOW,
                            "deadline": self.DEADLINE, "max_attempts": 3}, NOW)

    def park(self, at="2026-09-17T17:24:20+00:00"):
        """D's shape: the BLOCKED transition plus the durable claims-exhausted hold event."""
        applications.event(self.database, self.KEY, "BLOCKED",
                           {"code": self.HOLD_BLOCKER, "next_action": self.HOLD_ACTION}, at)
        applications.event(self.database, self.KEY, "APPLICATION_RECOVERY_CLAIMS_EXHAUSTED", {
            "blocker": "INSPECTION_INCOMPLETE", "started_at": NOW, "deadline": self.DEADLINE,
            "max_attempts": 3, "attempts": 3, "reason": "RECOVERY_CLAIMS_EXHAUSTED_PENDING_REVIEW",
            "hold_blocker": self.HOLD_BLOCKER, "terminal": False, "remaining_deadline": self.DEADLINE,
            "submission_status": "NOT_SUBMITTED_BY_RECOVERY", "next_action": self.HOLD_ACTION}, at)
        self.database.execute("UPDATE application_jobs SET state='BLOCKED',blocker=?,next_action=?,next_at=? WHERE job_key=?",
                              (self.HOLD_BLOCKER, self.HOLD_ACTION, self.DEADLINE, self.KEY))
        self.database.execute("INSERT INTO application_blockers VALUES (?,?,?,?,?,?,?)",
                              (self.KEY, self.HOLD_BLOCKER, "orchestrator", self.HOLD_ACTION, 0, self.DEADLINE, at))

    def review(self, outcome, at="2026-09-17T17:56:40+00:00"):
        """I's shape: one bounded AI hold review, RESUMED or ABANDONED."""
        reason = ("The third investigation observed the official posting and its APPLY NOW link"
                  if outcome == "RESUMED" else
                  "No supported official destination exists for this requisition after three actual reads")
        detail = {
            "review": 1, "decision": {"action": "RETRY" if outcome == "RESUMED" else "ABANDON",
                                      "method": "authenticated_history" if outcome == "RESUMED" else "",
                                      "progress": outcome == "RESUMED", "reason": reason},
            "budget": {"review": 1, "max_hold_reviews": 1, "investigation_claims_spent": 3,
                       "max_investigation_claims": 3, "further_investigation_claims_granted": 0,
                       "deadline": self.DEADLINE, "deadline_extended": False,
                       "submission_entitlement": "NONE: submission still requires the existing action guard"},
            "blocker": "INSPECTION_INCOMPLETE", "hold_blocker": self.HOLD_BLOCKER, "job_key": self.KEY,
            "deadline": self.DEADLINE, "deadline_extended": False, "attempts": 3, "reason": reason,
            "submission_status": "NOT_SUBMITTED_BY_RECOVERY", "outcome": outcome,
            "terminal": outcome == "ABANDONED",
            "next_action": "Retain terminal abandonment and its evidence; do not automatically reopen"
                           if outcome == "ABANDONED" else "Resumed by an AI orchestrator hold review"}
        if outcome == "ABANDONED":
            applications.event(self.database, self.KEY, "APPLICATION_ABANDONED",
                               {"reason": reason, "attempts": 3, "terminal": True,
                                "submission_status": "NOT_SUBMITTED_BY_RECOVERY"}, at)
            self.database.execute("UPDATE application_jobs SET state='ABANDONED',blocker='APPLICATION_RECOVERY_ABANDONED' WHERE job_key=?", (self.KEY,))
            self.database.execute("DELETE FROM application_blockers WHERE job_key=?", (self.KEY,))
        else:
            self.database.execute("UPDATE application_jobs SET state='QUEUED',blocker=NULL,next_at=NULL WHERE job_key=?", (self.KEY,))
            self.database.execute("DELETE FROM application_blockers WHERE job_key=?", (self.KEY,))
        applications.event(self.database, self.KEY, "APPLICATION_RECOVERY_HOLD_REVIEWED", detail, at)

    def recovery(self, now=REPORT_AT):
        return reporting.bounded_recovery(self.database, {}, now)

    def rows(self, event):
        return [row for row in self.recovery()["records"] if row["state"] == event]

    # -- the park -------------------------------------------------------------------

    def test_a_parked_claims_exhausted_hold_reads_as_a_failure_never_as_an_abandonment(self):
        self.seed_job()
        self.park()
        recovery = self.recovery()
        parked = self.rows("APPLICATION_RECOVERY_CLAIMS_EXHAUSTED")
        self.assertEqual(len(parked), 1)
        self.assertEqual(parked[0]["category"], "failures")
        self.assertEqual(parked[0]["reason"], "RECOVERY_CLAIMS_EXHAUSTED_PENDING_REVIEW")
        self.assertEqual(parked[0]["next_action"], self.HOLD_ACTION)
        self.assertIs(parked[0]["evidence"]["terminal"], False)
        self.assertEqual(parked[0]["deadline"], self.DEADLINE)
        self.assertEqual([row["category"] for row in recovery["records"] if row["category"] == "abandoned"], [])
        # The park adds a record but never a count: it shares its job's existing failure.
        self.assertEqual(recovery["daily"]["counts"]["application"],
                         {"abandoned": 0, "failures": 1, "recovery_attempts": 0, "recovered": 0})
        self.assertEqual(reporting.snapshot(self.database, {}, REPORT_AT)["outstanding_actions"][0]["code"],
                         self.HOLD_BLOCKER)

    def test_the_park_alone_does_not_add_a_failure_count_its_job_already_has(self):
        self.seed_job()
        before = self.recovery()["daily"]["counts"]["application"]["failures"]
        self.park()
        self.assertEqual(before, 0)
        self.assertEqual(self.recovery()["daily"]["counts"]["application"]["failures"], 1)
        applications.event(self.database, self.KEY, "APPLICATION_RECOVERY_CLAIMS_EXHAUSTED",
                           {"reason": "RECOVERY_CLAIMS_EXHAUSTED_PENDING_REVIEW", "terminal": False},
                           "2026-09-17T18:00:00+00:00")
        self.assertEqual(self.recovery()["daily"]["counts"]["application"]["failures"], 1)

    # -- the review -----------------------------------------------------------------

    def test_a_resumed_hold_review_is_a_coordinator_decision_not_a_recovery_success(self):
        self.seed_job()
        self.park()
        self.review("RESUMED")
        reviewed = self.rows("APPLICATION_RECOVERY_HOLD_REVIEWED")
        self.assertEqual(len(reviewed), 1)
        self.assertEqual(reviewed[0]["category"], "coordinator_decision")
        self.assertEqual(reviewed[0]["hold_review"]["outcome"], "RESUMED")
        self.assertIs(reviewed[0]["hold_review"]["terminal"], False)
        self.assertEqual(reviewed[0]["hold_review"]["further_investigation_claims_granted"], 0)
        self.assertIs(reviewed[0]["hold_review"]["deadline_extended"], False)
        self.assertEqual(reviewed[0]["hold_review"]["submission_status"], "NOT_SUBMITTED_BY_RECOVERY")
        self.assertIn("AI hold review 1/1 of " + self.HOLD_BLOCKER + ": RESUMED", reviewed[0]["reason"])
        counts = self.recovery()["daily"]["counts"]["application"]
        self.assertEqual(counts, {"abandoned": 0, "failures": 1, "recovery_attempts": 0, "recovered": 0})
        # The park stays in history; the resumption does not erase it.
        self.assertEqual(len(self.rows("APPLICATION_RECOVERY_CLAIMS_EXHAUSTED")), 1)
        self.assertEqual(reporting.snapshot(self.database, {}, REPORT_AT)["application_states"], {"QUEUED": 1})
        self.assertEqual(reporting.snapshot(self.database, {}, REPORT_AT)["verified_submissions"], 0)

    def test_an_abandoning_hold_review_keeps_the_terminal_record_and_its_single_count(self):
        self.seed_job()
        self.park()
        self.review("ABANDONED")
        recovery = self.recovery()
        reviewed = self.rows("APPLICATION_RECOVERY_HOLD_REVIEWED")
        self.assertEqual(reviewed[0]["category"], "coordinator_decision")
        self.assertEqual(reviewed[0]["hold_review"]["outcome"], "ABANDONED")
        self.assertIs(reviewed[0]["hold_review"]["terminal"], True)
        abandoned = [row for row in recovery["records"] if row["category"] == "abandoned"]
        self.assertEqual(len(abandoned), 1)
        self.assertIn("No supported official destination exists", abandoned[0]["reason"])
        self.assertEqual(recovery["daily"]["counts"]["application"],
                         {"abandoned": 1, "failures": 1, "recovery_attempts": 0, "recovered": 0})
        self.assertNotIn("superseded_by_authorized_reopen", abandoned[0])
        body = reporting.telegram_body(dict(reporting.snapshot(self.database, {}, REPORT_AT), day="2026-09-17"))
        self.assertIn("application abandoned=1", body)
        self.assertNotIn("Authorized reopen", body)

    def test_a_failed_hold_review_is_a_failure_and_never_a_terminal_outcome(self):
        self.seed_job()
        self.park()
        applications.event(self.database, self.KEY, "APPLICATION_RECOVERY_HOLD_REVIEW_FAILED",
                           {"review": 1, "reason": "APPLICATION_RECOVERY_DECISION_INVALID",
                            "hold_retained": True, "terminal": False, "max_review_failures": 2,
                            "next_action": self.HOLD_ACTION}, "2026-09-17T17:56:40+00:00")
        failed = self.rows("APPLICATION_RECOVERY_HOLD_REVIEW_FAILED")
        self.assertEqual(failed[0]["category"], "failures")
        self.assertEqual(failed[0]["reason"], "APPLICATION_RECOVERY_DECISION_INVALID")
        self.assertIs(failed[0]["evidence"]["hold_retained"], True)
        self.assertEqual(self.recovery()["daily"]["counts"]["application"],
                         {"abandoned": 0, "failures": 1, "recovery_attempts": 0, "recovered": 0})

    def test_park_then_review_then_reopen_each_read_truthfully_and_none_erases_the_others(self):
        self.seed_job()
        self.park()
        self.review("ABANDONED")
        ReopenReportingTests.seed_reopen(self, self.KEY, at="2026-09-17T18:10:00+00:00")
        recovery = self.recovery()
        staged = {row["state"]: row["category"] for row in recovery["records"]}
        self.assertEqual({name: staged[name] for name in (
            "APPLICATION_RECOVERY_CLAIMS_EXHAUSTED", "APPLICATION_RECOVERY_HOLD_REVIEWED",
            "APPLICATION_ABANDONED", "APPLICATION_REOPENED")},
            {"APPLICATION_RECOVERY_CLAIMS_EXHAUSTED": "failures",
             "APPLICATION_RECOVERY_HOLD_REVIEWED": "coordinator_decision",
             "APPLICATION_ABANDONED": "abandoned", "APPLICATION_REOPENED": "reopened"})
        abandoned = [row for row in recovery["records"] if row["category"] == "abandoned"]
        self.assertEqual(abandoned[0]["superseded_by_authorized_reopen"]["authorization_id"], AUTHORIZATION)
        self.assertEqual(recovery["daily"]["counts"]["application"],
                         {"abandoned": 1, "failures": 1, "recovery_attempts": 0, "recovered": 0})
        self.assertEqual(recovery["authorized_reopens"]["daily"], 1)
        self.assertEqual(reporting.snapshot(self.database, {}, REPORT_AT)["authorized_reopens"]["records"][0]["current_state"], "QUEUED")

    # -- unknown future event names --------------------------------------------------

    def test_an_unknown_future_event_name_is_skipped_without_changing_any_count(self):
        self.seed_job()
        self.park()
        before = self.recovery()
        for name in ("APPLICATION_SOMETHING_NOBODY_HAS_WRITTEN_YET", "APPLICATION_RECOVERY_HOLD_UNREVIEWED"):
            applications.event(self.database, self.KEY, name,
                               {"reason": "synthetic", "terminal": True, "attempts": 99}, "2026-09-17T18:30:00+00:00")
        after = self.recovery()
        self.assertEqual(after["daily"]["counts"], before["daily"]["counts"])
        self.assertEqual(after["rolling_week"]["counts"], before["rolling_week"]["counts"])
        self.assertEqual([row["source_id"] for row in after["records"]], [row["source_id"] for row in before["records"]])
        self.assertEqual(json.dumps(after["records"], default=str), json.dumps(before["records"], default=str))
        # Skipped, not miscounted: it is neither an abandonment nor a recovery success.
        self.assertEqual(after["daily"]["counts"]["application"]["abandoned"], 0)
        self.assertEqual(after["daily"]["counts"]["application"]["recovered"], 0)

    def test_a_malformed_hold_review_detail_stays_unknown_and_is_never_invented(self):
        self.seed_job()
        self.park()
        applications.event(self.database, self.KEY, "APPLICATION_RECOVERY_HOLD_REVIEWED",
                           {"review": 1}, "2026-09-17T17:56:40+00:00")
        reviewed = self.rows("APPLICATION_RECOVERY_HOLD_REVIEWED")[0]
        self.assertEqual(reviewed["category"], "coordinator_decision")
        self.assertIsNone(reviewed["hold_review"]["outcome"])
        self.assertIsNone(reviewed["hold_review"]["terminal"])
        self.assertIsNone(reviewed["hold_review"]["max_hold_reviews"])
        self.assertIn("UNKNOWN_OUTCOME", reviewed["reason"])
        self.assertIn("UNKNOWN_HOLD", reviewed["reason"])
        self.assertEqual(self.recovery()["daily"]["counts"]["application"]["abandoned"], 0)


if __name__ == "__main__":
    unittest.main()
