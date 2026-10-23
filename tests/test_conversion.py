import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import hashlib
import json
from pathlib import Path
import sqlite3
import unittest

import applications
import conversion


NOW = "2026-09-17T16:00:00+00:00"
LATER = "2026-09-19T16:00:00+00:00"
ROOT = Path(__file__).resolve().parents[1]


class ConversionTests(unittest.TestCase):
    def setUp(self):
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        applications.initialize(self.database)
        conversion.initialize(self.database)
        self.key = applications.ingest(self.database, {
            "company": "Mock Company", "requisition_id": "req-1", "role": "Staff Engineer",
            "url": "https://jobs.ashbyhq.com/mock/req-1"}, NOW)
        self.config = {}
        self.addCleanup(self.database.close)

    def job(self):
        return self.database.execute("SELECT * FROM application_jobs WHERE job_key=?", (self.key,)).fetchone()

    def evidence(self, identity, kind, facts, observed_at=NOW, source_type=None, occurred_at=None):
        record = {"evidence_id": identity, "kind": kind, "facts": facts,
                  "source_type": source_type or sorted(conversion.SOURCES[kind])[0],
                  "source_ref": "https://mock.example/evidence/" + identity, "source_locator": "Mock source paragraph 1",
                  "source_sha256": hashlib.sha256(conversion.encoded(facts).encode()).hexdigest(),
                  "verified_by": "mock-authenticated-importer", "observed_at": observed_at,
                  "occurred_at": occurred_at or observed_at}
        conversion.record_evidence(self.database, record)
        return record

    def event(self, identity, kind, facts=None, contact=None, now=NOW, source_type=None):
        facts = dict(facts or {}, employer=self.job()["employer"], requisition=self.job()["requisition"])
        if contact:
            facts["contact_id"] = contact
        self.evidence(identity, kind, facts, now, source_type)
        conversion.enqueue_event(self.database, "event:" + identity, kind, self.key, identity, contact)

    def cycle(self, now=NOW):
        return conversion.cycle(self.database, self.config, now)

    def champion_inputs(self):
        self.event("role", "ROLE_CONTEXT", {"title": "Staff Engineer", "team": "Platform", "needs": [
            {"text": "Distributed service reliability", "topic": "reliability"}]})
        for identity, category, text in [("claim-1", "achievement", "Built the mock service."),
                                         ("claim-2", "achievement", "Reduced mock recovery time."),
                                         ("claim-3", "leadership", "Led mock incident reviews.")]:
            self.evidence(identity, "CANDIDATE_CLAIM", {"category": category, "text": text, "topics": ["reliability"]}, source_type="designated_resume")
        self.evidence("profile", "CANDIDATE_PROFILE", {"name": "Mock Candidate", "profile_url": "https://www.linkedin.com/in/mock/",
                                                       "designated_resume_reference": "mock-designated-resume.pdf"})
        self.config = {"conversion": {"champion_profiles": {self.key: {
            "claim_evidence_ids": ["claim-1", "claim-2", "claim-3"], "profile_evidence_id": "profile"}}}}

    def test_existing_interview_never_creates_a_duplicate_application(self):
        self.event("screen", "INTERVIEW_STAGE", {"stage_id": "screen-1", "stage": "RECRUITER_SCREEN"})
        self.cycle()
        self.assertEqual("DUPLICATE", self.job()["state"])
        self.assertEqual("EXISTING_INTERVIEW", self.job()["blocker"])
        self.assertTrue(self.database.execute("SELECT 1 FROM application_history WHERE kind='INTERVIEW'").fetchone())
        self.assertEqual(1, self.database.execute("SELECT COUNT(*) FROM application_jobs").fetchone()[0])

    def test_evidence_rejects_a_bare_verified_flag(self):
        with self.assertRaises(ValueError):
            conversion.record_evidence(self.database, {"evidence_id": "fake", "kind": "CANDIDATE_CLAIM", "verified": True})

    def test_all_interview_stages_dates_and_debriefs_are_source_bound(self):
        for index, stage in enumerate(conversion.STAGES):
            self.event("stage-" + str(index), "INTERVIEW_STAGE", {"stage_id": str(index), "stage": stage})
        result = self.cycle()
        self.assertEqual(6, self.database.execute("SELECT COUNT(*) FROM conversion_interviews").fetchone()[0])
        self.assertTrue(all(row[0] is None for row in self.database.execute("SELECT scheduled_at FROM conversion_interviews")))
        self.assertEqual(6, len([task for task in result["tasks"] if task["kind"] == "INTERVIEW_PREPARATION"]))
        self.assertFalse(result["outcomes"])
        self.event("actual-debrief", "DEBRIEF", {"stage_id": "1", "notes": "I felt it went well; discussed the actual mock outage.", "objections": ["They asked about operational experience."]}, source_type="user_confirmation")
        result = self.cycle()
        self.assertFalse(result["outcomes"])
        objection = next(task for task in result["tasks"] if task["kind"] == "CLARIFY_REPORTED_OBJECTION")
        self.assertEqual("user_report", objection["inputs"]["assessment_source"])

    def test_employer_timeline_followup_and_terminal_outcome(self):
        self.event("stage", "INTERVIEW_STAGE", {"stage_id": "system-1", "stage": "SYSTEM_DESIGN"})
        self.event("timeline", "EMPLOYER_TIMELINE", {"stage_id": "system-1", "employer_statement": "Please follow up at the specified time", "followup_at": LATER})
        self.assertFalse(any(task["kind"] == "EMPLOYER_TIMELINE_FOLLOWUP" for task in self.cycle()["tasks"]))
        due = self.cycle(LATER)
        self.assertEqual(1, len([task for task in due["tasks"] if task["kind"] == "EMPLOYER_TIMELINE_FOLLOWUP"]))
        self.event("rejection", "OUTCOME", {"outcome": "REJECTED", "observation": "independent_outcome_read", "statement": "Mock employer explicitly declined."}, now=LATER)
        result = self.cycle(LATER)
        self.assertFalse(any(task["kind"] == "EMPLOYER_TIMELINE_FOLLOWUP" for task in result["tasks"]))
        self.assertEqual("REJECTED", result["outcomes"][0]["kind"])

    def test_unknown_timeline_does_not_invent_due_date(self):
        self.event("stage", "INTERVIEW_STAGE", {"stage_id": "final", "stage": "FINAL_ROUND"})
        self.event("timeline", "EMPLOYER_TIMELINE", {"stage_id": "final", "employer_statement": "We will contact you soon"})
        result = self.cycle(LATER)
        self.assertFalse(any(task["kind"] == "EMPLOYER_TIMELINE_FOLLOWUP" for task in result["tasks"]))
        self.assertTrue(any(task["kind"] == "CLARIFY_EMPLOYER_TIMELINE" for task in result["tasks"]))

    def test_true_outcome_deduplication_baseline_and_no_stage_inflation(self):
        for identity in ("interview-first", "interview-repeat"):
            self.event(identity, "OUTCOME", {"outcome": "INTERVIEW", "observation": "independent_outcome_read", "statement": "Mock employer confirmed interview.", "baseline": True})
        self.event("self-assessment", "OUTCOME", {"outcome": "OFFER", "observation": "self_assessment", "statement": "I performed strongly"})
        result = self.cycle()
        self.assertEqual(1, len(result["outcomes"]))
        self.assertFalse(result["outcomes"][0]["attributed"])
        self.assertEqual(1, len(result["reporting_events"]))
        self.assertEqual("INTERVIEW", result["reporting_events"][0]["kind"])

    def test_attribution_requires_verified_execution_and_source_bound_origin(self):
        receipt = {"source": "mock-independent-receipt", "verified": True, "independent_read": True, "status": "SUBMITTED",
                   "receipt_id": "receipt", "employer": self.job()["employer"], "requisition": self.job()["requisition"], "observed_at": NOW}
        self.database.execute("UPDATE application_jobs SET state='SUBMITTED',receipt=? WHERE job_key=?", (json.dumps(receipt), self.key))
        self.evidence("attribution", "ATTRIBUTION", {"employer": self.job()["employer"], "requisition": self.job()["requisition"],
                      "outcome_evidence_id": "generated", "origin": {"table": "application_jobs", "key": self.key}}, LATER)
        self.event("generated", "OUTCOME", {"outcome": "INTERVIEW", "observation": "independent_outcome_read", "statement": "Interview in response to this application", "attribution_evidence_id": "attribution"}, now=LATER)
        result = self.cycle(LATER)
        self.assertTrue(result["outcomes"][0]["attributed"])
        self.assertEqual({"table": "application_jobs", "key": self.key}, result["normalized_outcomes"][0]["origin"])
        import reporting
        reporting.initialize(self.database)
        with self.assertRaises(ValueError):
            reporting.record_outcome(self.database, **result["reporting_events"][0])
        import source_records
        self.assertIsNotNone(source_records.execution(self.database, result["normalized_outcomes"][0]["origin"], LATER))

    def test_event_and_evidence_identity_immutable_and_cycle_repeatable(self):
        screen = {"stage_id": "screen-1", "stage": "RECRUITER_SCREEN"}
        self.event("screen", "INTERVIEW_STAGE", screen)
        self.cycle()
        self.event("screen", "INTERVIEW_STAGE", screen)
        self.assertFalse(self.cycle()["applied"])
        with self.assertRaises(ValueError):
            self.event("screen", "INTERVIEW_STAGE", dict(screen, stage="FINAL_ROUND"))
        with self.assertRaises(ValueError):
            conversion.enqueue_event(self.database, "event:screen", "DEBRIEF", self.key, "screen")
        with self.assertRaises(sqlite3.IntegrityError):
            self.database.execute("DELETE FROM conversion_evidence")

    def test_future_observation_and_wrong_job_block_only_their_item(self):
        self.event("future", "INTERVIEW_STAGE", {"stage_id": "future", "stage": "RECRUITER_SCREEN"}, now=LATER)
        self.evidence("wrong", "INTERVIEW_STAGE", {"employer": "wrong", "requisition": "req-1", "stage_id": "wrong", "stage": "RECRUITER_SCREEN"})
        conversion.enqueue_event(self.database, "wrong", "INTERVIEW_STAGE", self.key, "wrong")
        self.event("good", "INTERVIEW_STAGE", {"stage_id": "good", "stage": "RECRUITER_SCREEN"})
        result = self.cycle()
        self.assertEqual(["event:good"], result["applied"])

    def test_real_saved_r09_baselines_stage_without_new_applications_or_false_outcomes(self):
        records = conversion.seed_baseline_priorities(self.database, ROOT / "tests/fixtures/handoff/REQUIREMENTS.md", NOW)
        self.assertEqual(4, len(records))
        self.assertEqual(4, len(conversion.seed_baseline_priorities(self.database, ROOT / "tests/fixtures/handoff/REQUIREMENTS.md", LATER)))
        self.assertEqual(4, self.database.execute("SELECT COUNT(*) FROM conversion_baselines").fetchone()[0])
        self.assertEqual(1, self.database.execute("SELECT COUNT(*) FROM application_jobs").fetchone()[0])
        self.assertTrue(all(record["confirmation_freshness"] == "UNKNOWN" and record["job_key"] is None for record in records))
        self.assertEqual("2026-09-24T09:15:00-07:00", records[2]["historical_scheduled_at"])
        self.assertEqual("2026-09-24T13:45:00-07:00", records[3]["historical_scheduled_at"])
        result = self.cycle()
        self.assertFalse(result["outcomes"])
        self.assertEqual(4, len([task for task in result["tasks"] if task["kind"] == "RECONCILE_BASELINE_INTERVIEW"]))
        self.assertTrue(all(task["job_key"] is None for task in result["tasks"]))

    def test_new_stage_supersedes_old_timeline_and_uses_sourced_prep(self):
        self.champion_inputs()
        self.event("stage", "INTERVIEW_STAGE", {"stage_id": "screen", "stage": "RECRUITER_SCREEN"})
        self.event("timeline", "EMPLOYER_TIMELINE", {"stage_id": "screen", "employer_statement": "Follow up today", "followup_at": NOW})
        first = self.cycle()
        prep = next(task for task in first["tasks"] if task["kind"] == "INTERVIEW_PREPARATION")
        self.assertEqual("SOURCED", prep["inputs"]["role_specific_material_status"])
        self.assertEqual(3, len(prep["inputs"]["verified_story_material"]))
        self.assertTrue(any(task["kind"] == "EMPLOYER_TIMELINE_FOLLOWUP" for task in first["tasks"]))
        self.event("new-stage", "INTERVIEW_STAGE", {"stage_id": "technical", "stage": "TECHNICAL_SCREEN"}, now=LATER)
        second = self.cycle(LATER)
        self.assertFalse(any(task["kind"] == "EMPLOYER_TIMELINE_FOLLOWUP" for task in second["tasks"]))

    def test_missing_dependency_can_be_retried_without_rebinding_input(self):
        conversion.enqueue_event(self.database, "missing", "INTERVIEW_STAGE", self.key, "late-source")
        self.assertFalse(self.cycle()["applied"])
        self.evidence("late-source", "INTERVIEW_STAGE", {"employer": self.job()["employer"], "requisition": self.job()["requisition"],
                                                          "stage_id": "screen", "stage": "RECRUITER_SCREEN"})
        self.assertTrue(conversion.retry_event(self.database, "missing"))
        self.assertIn("missing", self.cycle()["applied"])
        self.assertFalse(conversion.retry_event(self.database, "missing"))

    def resolved_identity(self, historical_kind=None):
        import identity_reconciliation
        role = {"company": "Alias Employer", "role": "Staff Engineer", "url": "https://employer.example/jobs/posting"}
        self.key = applications.ingest(self.database, role, NOW)
        original = self.job()["requisition"]
        if historical_kind:
            self.database.execute("INSERT INTO application_history VALUES (?,?,?,?)", (self.job()["employer"], original, historical_kind, '{"source":"original positive history"}'))
        identity_reconciliation.reconcile_requisition(self.database, self.key, "known-req", {
            "source": role["url"], "verified": True, "independent_read": True, "observed_at": NOW,
            "employer": self.job()["employer"], "role": role["role"], "requisition": "known-req",
            "identity_basis": "official_canonical_posting_id", "job_key": self.key}, NOW)
        return original

    def test_original_positive_history_still_blocks_after_requisition_resolution(self):
        original = self.resolved_identity("INTERVIEW")
        self.cycle()
        self.assertTrue(conversion._duplicate(self.database, dict(self.job())))
        record = self.database.execute("SELECT requisition,evidence FROM application_history WHERE employer=?", (self.job()["employer"],)).fetchone()
        self.assertEqual(original, record["requisition"])
        self.assertEqual('{"source":"original positive history"}', record["evidence"])

if __name__ == "__main__":
    unittest.main()
