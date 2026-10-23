import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import orchestrator
from test_research_integration import validate_schema_subset
import profile_home


ROOT = Path(__file__).resolve().parents[1]
REAL_POPEN = orchestrator.subprocess.Popen


class ResearchEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("Network prohibited")))
        self.enterContext(patch.object(orchestrator, "HeyReach", side_effect=AssertionError("Provider prohibited")))
        self.enterContext(patch.dict(os.environ, {"JOB_SEARCH_FIXTURE_STATUS": "COMPLETE"}))
        self.enterContext(patch.dict(os.environ))
        os.environ.pop("JOB_SEARCH_FIXTURE_OPPORTUNITY", None)
        self.process = self.enterContext(patch.object(orchestrator.subprocess, "Popen", side_effect=self.local_worker))

    def local_worker(self, command, **kwargs):
        executable = Path(command[0])
        self.assertEqual(executable.name, "fake-codex")
        working = Path(kwargs["cwd"]).resolve()
        root = executable.parent.resolve()
        self.assertTrue(working == root or working.parent == root / "reports/research-recovery")
        self.assertEqual(executable.read_bytes(), (Path(__file__).parent / "fake_codex.py").read_bytes())
        return REAL_POPEN(command, **kwargs)

    def prepare_inputs(self, root):
        for filename in ("research-prompt.md", "research-schema.json", "discovery-prompt.md",
                         "discovery-schema.json"):
            shutil.copyfile(ROOT / filename, root / filename)

    def test_investigated_but_unresolved_research_counts_as_daily_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for folder in ("state", "reports", "logs"):
                (root / folder).mkdir()
            executable = root / "fake-codex"
            shutil.copyfile(Path(__file__).parent / "fake_codex.py", executable)
            executable.chmod(0o700)
            self.prepare_inputs(root)
            database = orchestrator.connect(root)
            config = {"timezone": "America/Los_Angeles", "research_hour": 8, "research_max_attempts": 3,
                      "research_retry_delays_seconds": [900, 3600], "research_timeout_seconds": 10,
                      "model": "fixture", "codex": str(executable), "research_company_candidates": ["Fixture"]}
            try:
                with patch.dict(os.environ, {"JOB_SEARCH_FIXTURE_STATUS": "PARTIAL"}):
                    result = orchestrator.research(root, config, database, {"companies": [], "rules": {"max_unique_contacts_per_company": 3}}, {})
                # Owner, 2026-09-28: research that ran and explained every company it could not
                # resolve is a successful research ("This is a successful research"), even with
                # no role found; nothing is abandoned and the day completes.
                self.assertEqual(result["state"], "COMPLETE")
                self.assertEqual(len(result["industries"]), 8)
                self.assertEqual(self.process.call_count, 16)
                for industry in result["industries"]:
                    with self.subTest(industry=industry["industry"]):
                        self.assertEqual(industry["worker_status"], "PARTIAL")
                        self.assertEqual(industry["state"], "COMPLETE")
                        self.assertIn("SYNTHETIC incomplete research", industry["blockers"])
                        self.assertEqual(json.loads(Path(industry["report"]).read_text())["research_status"], "PARTIAL")
                        self.assertFalse(industry.get("recovery_decisions"))
                self.assertEqual(result["schedule"]["state"], "COMPLETE")
                self.assertIsNotNone(database.execute("SELECT completed_at FROM research_days").fetchone()[0])
                self.assertEqual(orchestrator.research(root, config, database, {"companies": []}, {})["state"], "COMPLETE")
                self.assertEqual(self.process.call_count, 16)
            finally:
                database.close()

    def test_subprocess_output_persistence_and_restart_deduplication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for folder in ("state", "reports", "logs"):
                (root / folder).mkdir()
            executable = root / "fake-codex"
            shutil.copyfile(Path(__file__).parent / "fake_codex.py", executable)
            executable.chmod(0o700)
            self.prepare_inputs(root)
            database = orchestrator.connect(root)
            config = {"timezone": "America/Los_Angeles", "research_hour": 8, "research_max_attempts": 3,
                      "research_retry_delays_seconds": [900, 3600], "research_timeout_seconds": 10,
                      "model": "fixture", "codex": str(executable), "research_company_candidates": ["Fixture"]}
            pipeline = {"companies": [], "rules": {"max_unique_contacts_per_company": 3}}
            try:
                result = orchestrator.research(root, config, database, pipeline, {})
                self.assertEqual(result["state"], "COMPLETE")
                self.assertEqual(result["schedule"]["state"], "COMPLETE")
                saved = json.loads(Path(result["report"]).read_text())
                self.assertEqual(saved["industries"], result["industries"])
                self.assertEqual(len(result["industries"]), 8)
                self.assertEqual(self.process.call_count, 16)
                for industry in result["industries"]:
                    with self.subTest(industry=industry["industry"]):
                        self.assertEqual(industry["state"], "COMPLETE")
                        payload = json.loads(Path(industry["report"]).read_text())
                        self.assertEqual(payload["summary"], "End-to-end fixture")
                        for report, schema_name in ((industry["report"], "research-schema.json"),
                                                    (industry["discovery_report"], "discovery-schema.json")):
                            schema = json.loads((ROOT / schema_name).read_text())
                            validate_schema_subset(json.loads(Path(report).read_text()), schema, schema)
                for table in ("opportunities", "application_jobs", "rw_plans", "rw_results"):
                    self.assertEqual(database.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 8)
                self.assertEqual(database.execute("SELECT COUNT(*) FROM application_jobs WHERE state='QUEUED'").fetchone()[0], 8)
            finally:
                database.close()
            database = orchestrator.connect(root)
            try:
                with patch.object(orchestrator.subprocess, "Popen") as process:
                    self.assertEqual(orchestrator.research(root, config, database, pipeline, {})["state"], "COMPLETE")
                    process.assert_not_called()
                self.assertEqual(database.execute("SELECT COUNT(*) FROM research").fetchone()[0], 1)
                for table in ("opportunities", "application_jobs", "rw_plans", "rw_results"):
                    self.assertEqual(database.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 8)
            finally:
                database.close()
