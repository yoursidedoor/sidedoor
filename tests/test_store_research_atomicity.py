import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import copy
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import applications
import orchestrator
import research_workflow


NOW = "2026-09-17T19:44:48+00:00"


class StoreResearchAtomicityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="store-research-atomicity-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "state").mkdir()
        self.database = orchestrator.connect(self.root)
        self.addCleanup(self.database.close)
        self.pipeline = {"companies": [], "rules": {"max_unique_contacts_per_company": 3}}
        self.raw = {"research_status": "PARTIAL", "summary": "Synthetic transaction fixture",
                    "blockers": [], "drafts": [], "opportunities": [
                        {"company": "Fixture", "role": "Software Engineer", "requisition_id": "123",
                         "url": "https://employer.example/jobs/123", "rationale": "Synthetic fixture",
                         "people": [], "evidence": []}]}

    def initialize(self):
        applications.initialize(self.database)
        research_workflow.initialize(self.database)
        research_workflow.plan_research(self.database, "atomicity", ["Fixture"], now=NOW, contact_slots=0)

    def process(self):
        return research_workflow.process_research(self.database, self.raw, plan_id="atomicity", now=NOW)

    def test_post_ingest_assertion_rolls_back_processor_and_storage(self):
        self.initialize()
        before = list(self.database.iterdump())
        with self.assertRaisesRegex(AssertionError, "forced post-ingest failure"):
            with research_workflow.transaction(self.database):
                processed = self.process()
                with patch.object(applications, "initialize", side_effect=AssertionError("nested initializer")):
                    self.assertEqual(orchestrator.store_research(self.database, self.pipeline, processed), [])
                self.assertTrue(self.database.in_transaction)
                self.assertEqual(self.database.execute("SELECT COUNT(*) FROM application_jobs").fetchone()[0], 1)
                raise AssertionError("forced post-ingest failure")
        self.assertEqual(list(self.database.iterdump()), before)

    def test_successful_commit_and_replay_preserve_jobs_and_events(self):
        self.initialize()
        with research_workflow.transaction(self.database):
            processed = self.process()
            orchestrator.store_research(self.database, self.pipeline, processed)
            self.assertTrue(self.database.in_transaction)
        with sqlite3.connect(self.root / "state/orchestrator.sqlite3") as observer:
            self.assertEqual(observer.execute("SELECT job_key FROM application_jobs").fetchall(), [("fixture:123",)])
            self.assertEqual(observer.execute("SELECT COUNT(*) FROM rw_results").fetchone()[0], 1)
        jobs = [tuple(row) for row in self.database.execute("SELECT * FROM application_jobs")]
        events = [tuple(row) for row in self.database.execute("SELECT * FROM application_events")]
        with research_workflow.transaction(self.database):
            replay = self.process()
            self.assertEqual(replay, processed)
            orchestrator.store_research(self.database, self.pipeline, replay)
        self.assertEqual(jobs, [tuple(row) for row in self.database.execute("SELECT * FROM application_jobs")])
        self.assertEqual(events, [tuple(row) for row in self.database.execute("SELECT * FROM application_events")])
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM rw_results").fetchone()[0], 1)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0], 1)

    def test_storage_failure_rolls_back_savepoint_without_discarding_caller_work(self):
        self.initialize()
        with research_workflow.transaction(self.database):
            processed = self.process()
            invalid = copy.deepcopy(processed)
            invalid["opportunities"].append(dict(invalid["opportunities"][0], url="http://invalid.example/job"))
            with self.assertRaisesRegex(ValueError, "HTTPS source"):
                orchestrator.store_research(self.database, self.pipeline, invalid)
            self.assertTrue(self.database.in_transaction)
            self.assertEqual(self.database.execute("SELECT COUNT(*) FROM rw_results").fetchone()[0], 1)
            self.assertEqual(self.database.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0], 0)
            self.assertEqual(self.database.execute("SELECT COUNT(*) FROM application_jobs").fetchone()[0], 0)
            self.assertEqual(self.database.execute("SELECT COUNT(*) FROM application_events").fetchone()[0], 0)
            orchestrator.store_research(self.database, self.pipeline, processed)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM application_jobs").fetchone()[0], 1)

    def test_missing_nested_schema_fails_closed_without_committing_caller_work(self):
        before = list(self.database.iterdump())
        with self.assertRaisesRegex(ValueError, "Initialize research storage before the caller transaction"):
            with research_workflow.transaction(self.database):
                self.database.execute("INSERT INTO suppressions VALUES ('fixture', 'caller work')")
                with patch.object(applications, "initialize", side_effect=AssertionError("nested initializer")):
                    orchestrator.store_research(self.database, self.pipeline, self.raw)
        self.assertEqual(list(self.database.iterdump()), before)

    def test_standalone_storage_initializes_and_commits(self):
        self.assertFalse(self.database.in_transaction)
        self.assertEqual(orchestrator.store_research(self.database, self.pipeline, self.raw), [])
        self.assertFalse(self.database.in_transaction)
        with sqlite3.connect(self.root / "state/orchestrator.sqlite3") as observer:
            self.assertEqual(observer.execute("SELECT job_key FROM application_jobs").fetchall(), [("fixture:123",)])


if __name__ == "__main__":
    unittest.main()
