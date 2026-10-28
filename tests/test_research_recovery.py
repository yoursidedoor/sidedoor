import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import manual_research_recovery
import parallel_research
import research_recovery
import workflow_tasks
import orchestrator
from test_research_integration import completed, validate_schema_subset


class ResearchRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = manual_research_recovery.fixture_setup()
        self.addCleanup(self.fixture.doCleanups)
        self.decisions = []
        self.fixture.runtime.research_recovery_methods = lambda root, config: {"public_search": "Existing web search"}
        self.fixture.runtime.research_recovery_decision = self.decide

    def decide(self, root, config, inability, timeout):
        self.assertIsNot(threading.current_thread(), threading.main_thread())
        self.assertLessEqual(timeout, 30)
        self.decisions.append(inability)
        return {"action": "RETRY", "method": "public_search", "instructions": "Read another supported source",
                "reason": "Source route available", "progress": True}

    def fail_discovery(self, industry, phase):
        if industry == "Defense" and phase == "discovery":
            raise RuntimeError("Synthetic discovery source failed")

    def test_manual_scenarios_after_manual_validation(self):
        for mode in ("retry", "first_alternative", "abandon", "no_progress", "exhaustion", "orphan"):
            with self.subTest(mode=mode):
                self.assertEqual(manual_research_recovery.scenario(mode)["restart_worker_calls"], 0)

    def test_first_alternative_can_retry_without_fabricating_observed_progress(self):
        raw = {"action": "RETRY", "method": "public_search", "instructions": "Use another supported public source within the original assignment",
               "reason": "First supported alternative after initial source failure", "progress": False}
        self.fixture.runtime.research_recovery_decision = lambda root, config, packet, timeout: dict(raw)
        attempts = []

        def worker_hook(industry, phase):
            if industry == "Defense" and phase == "discovery":
                attempts.append(phase)
                if len(attempts) == 1:
                    raise RuntimeError("Synthetic initial source failure")

        self.fixture.worker_hook = worker_hook
        result = self.fixture.run_research()
        self.assertEqual(result["state"], "COMPLETE")
        self.assertEqual(len(attempts), 2)
        state = self.fixture.states(result)["Defense"]
        self.assertEqual(state["recovery_decisions"][0]["decision"], raw)
        self.assertEqual(len(state["failures"]), 1)
        self.fixture.calls.clear()
        self.assertEqual(self.fixture.run_research("restart")["state"], "COMPLETE")
        self.assertEqual(self.fixture.calls, [])

    def test_executable_callback_is_thread_safe_and_subscription_only(self):
        self.assertEqual(manual_research_recovery.executor_scenario()["state"], "PASS")

    def test_checkpoint_loss_or_regression_cannot_reuse_reserved_compute(self):
        for mode in ("missing", "corrupt", "rolled_back", "deadline_missing"):
            with self.subTest(mode=mode):
                result = manual_research_recovery.checkpoint_loss_scenario(mode)
                self.assertEqual(result["new_worker_calls"], 0)
                self.assertEqual(result["new_judgments"], 0)
                self.assertTrue(result["reservations_and_history_unchanged"])

    def test_reported_attempted_methods_are_excluded_before_recovery(self):
        for mode in ("same_method", "alternative_method", "all_attempted"):
            with self.subTest(mode=mode):
                self.assertEqual(manual_research_recovery.attempted_method_scenario(mode)["state"], "PASS")

    def test_failed_executor_retains_raw_log_schema_without_replay(self):
        config = {"codex": "/MOCK_DO_NOT_EXECUTE/codex", "model": "fixture"}
        packet = manual_research_recovery.synthetic_inability(config)
        artifacts = research_recovery.create_artifacts(self.fixture.root)
        packet["artifacts"] = artifacts

        def failed_worker(root, command, prompt, log, timeout):
            Path(artifacts["raw"]).write_text("{malformed synthetic result")
            log.write_text("synthetic timeout diagnostic")
            raise TimeoutError("Synthetic timeout")

        worker = Mock(side_effect=failed_worker)
        runtime = SimpleNamespace(research_command=orchestrator.research_command, run_research_worker=worker)
        with ThreadPoolExecutor(max_workers=1) as executor:
            with self.assertRaises(TimeoutError):
                executor.submit(research_recovery.research_recovery_decision, self.fixture.root, config, packet, 30,
                                runtime=runtime).result()
        self.assertEqual(Path(artifacts["raw"]).read_text(), "{malformed synthetic result")
        self.assertEqual(Path(artifacts["log"]).read_text(), "synthetic timeout diagnostic")
        self.assertEqual(json.loads(Path(artifacts["schema"]).read_text()), research_recovery.DECISION_SCHEMA)
        with self.assertRaisesRegex(ValueError, "overwritten or replayed"):
            research_recovery.research_recovery_decision(self.fixture.root, config, packet, 30, runtime=runtime)
        self.assertEqual(worker.call_count, 1)
        self.assertNotEqual(research_recovery.create_artifacts(self.fixture.root)["directory"], artifacts["directory"])

    def test_inability_is_required_but_nullable_and_typed_when_present(self):
        """Strict structured output: every declared property is required.

        This test used to assert that an output WITHOUT `inability` stays valid. That was
        the invariant the provider rejects: it returned 400 invalid_json_schema for the
        optional field on 2026-09-19 and no research worker reached a model. `inability`
        is now required and nullable - null on success, a typed object otherwise.
        """
        root = Path(__file__).resolve().parents[1]
        schema = json.loads((root / "research-schema.json").read_text())
        payload = completed()
        self.assertIsNone(payload["inability"])
        validate_schema_subset(payload, schema, schema)
        missing = {key: value for key, value in payload.items() if key != "inability"}
        with self.assertRaisesRegex(ValueError, "Missing required property"):
            validate_schema_subset(missing, schema, schema)
        payload["inability"] = {"error_class": "SOURCE_ACCESS", "diagnostic": "Unavailable", "attempted_methods": [],
                                "required_input": "", "progress": False}
        validate_schema_subset(payload, schema, schema)
        payload["inability"]["progress"] = "yes"
        with self.assertRaises(ValueError):
            validate_schema_subset(payload, schema, schema)

    def test_discovery_failure_emits_structured_packet_and_retries(self):
        attempts = []

        def payload(industry, phase, result):
            if industry == "Defense" and phase == "discovery":
                attempts.append(phase)
                if len(attempts) == 1:
                    result.update(discovery_status="FAILED", companies=[], blockers=["Unreadable directory"],
                                  inability={"error_class": "SOURCE_ACCESS", "diagnostic": "Directory unreadable",
                                             "attempted_methods": ["directory"], "required_input": "", "progress": False})
            return result

        self.fixture.payload_hook = payload
        result = self.fixture.run_research()
        self.assertEqual(result["state"], "COMPLETE")
        self.assertEqual(len(attempts), 2)
        self.assertEqual(self.decisions[0]["failure"]["worker_inability"]["error_class"], "SOURCE_ACCESS")
        self.assertEqual(self.decisions[0]["failure"]["owner"], "orchestrator")

    def test_recovery_wait_does_not_block_unrelated_ingestion(self):
        fast_ingested = threading.Event()
        original = self.decide

        def decide(root, config, inability, timeout):
            self.assertTrue(fast_ingested.wait(3), "Recovery blocked unrelated industry ingestion")
            return original(root, config, inability, timeout)

        self.fixture.runtime.research_recovery_decision = decide
        self.fixture.worker_hook = self.fail_discovery
        self.fixture.store_hook = lambda industry: fast_ingested.set() if len(self.fixture.ingested) == 7 else None
        result = self.fixture.run_research()
        self.assertEqual(sum(state["state"] == "COMPLETE" for state in result["industries"]), 7)
        self.assertEqual(self.fixture.states(result)["Defense"]["abandonment_reason"], "RECOVERY_DECISION_FAILED")
        self.assertEqual(len(self.decisions), 2)

    def test_interrupted_decision_is_not_replayed_after_restart(self):
        self.fixture.worker_hook = self.fail_discovery
        self.fixture.run_research()
        path = self.fixture.cache_path("Defense")
        cache = json.loads(path.read_text())
        cache.pop("terminal")
        cache["recovery_decisions"][-1]["state"] = "PENDING"
        cache["result"]["state"] = "RECOVERING"
        path.write_text(json.dumps(cache))
        self.fixture.calls.clear()
        self.decisions.clear()
        result = self.fixture.run_research("restart")
        self.assertEqual(self.fixture.calls, [])
        self.assertEqual(self.decisions, [])
        self.assertEqual(self.fixture.states(result)["Defense"]["abandonment_reason"], "INTERRUPTED_RECOVERY_DECISION_NOT_REPLAYED")

    def test_each_phase_reserves_compute_and_respects_reduced_cap(self):
        # Compute arithmetic below is written in 120-second sessions; the session sizes themselves
        # (300 s discovery, 120 + 1.5 x slow end x companies) are tested in test_parallel_research.
        for name, value in (("DISCOVERY_SECONDS", 120), ("qualification_seconds", lambda database, companies: 120)):
            sizes = patch.object(parallel_research, name, value)
            sizes.start()
            self.addCleanup(sizes.stop)
        self.fixture.config.update(research_timeout_seconds=600, daily_compute_seconds=9000)
        with self.fixture.database:
            workflow_tasks.reserve_compute(self.fixture.database, "service", self.fixture.utcnow(), 8700)
        result = self.fixture.run_research()
        self.assertEqual(result["state"], "PARTIAL")  # a pure compute wait is PARTIAL (owner, 2026-09-25)
        reservations = list(self.fixture.database.execute("SELECT seconds FROM worker_compute_reservations WHERE action_id!='service'"))
        self.assertTrue(reservations)
        self.assertTrue(all(0 < row[0] <= 120 for row in reservations))
        self.assertLessEqual(sum(row[0] for row in reservations) + 8700, 9000)
        self.assertEqual(self.fixture.count("application_attempts"), 0)

    def test_deadline_persists_without_restart_extension(self):
        self.fixture.worker_hook = self.fail_discovery
        self.fixture.run_research()
        path = self.fixture.cache_path("Defense")
        cache = json.loads(path.read_text())
        original_deadline = cache["deadline_at"]
        cache.pop("terminal")
        cache["recovery_decisions"] = []
        cache["result"]["state"] = "RUNNING"
        cache["deadline_at"] = self.fixture.clock_base.isoformat()
        path.write_text(json.dumps(cache))
        self.fixture.calls.clear()
        self.decisions.clear()
        result = self.fixture.run_research("restart")
        self.assertEqual(self.fixture.calls, [])
        self.assertEqual(self.decisions, [])
        self.assertEqual(self.fixture.states(result)["Defense"]["recovery_state"], "ABANDONED")
        self.assertNotEqual(json.loads(path.read_text())["deadline_at"], original_deadline)

    def test_unsupported_method_never_launches_worker(self):
        self.fixture.worker_hook = self.fail_discovery

        def decide(root, config, inability, timeout):
            return {"action": "RETRY", "method": "new_paid_service", "instructions": "Invented route",
                    "reason": "Unsupported proposal", "progress": True}

        self.fixture.runtime.research_recovery_decision = decide
        result = self.fixture.run_research()
        self.assertEqual(self.fixture.calls.count(("Defense", "discovery")), 1)
        state = self.fixture.states(result)["Defense"]
        self.assertEqual(state["recovery_state"], "ABANDONED")
        self.assertEqual(state["recovery_decisions"][0]["decision"]["method"], "new_paid_service")

    def test_no_executor_is_terminal_and_preserves_original_failure(self):
        del self.fixture.runtime.research_recovery_decision
        self.fixture.worker_hook = self.fail_discovery
        result = self.fixture.run_research()
        state = self.fixture.states(result)["Defense"]
        self.assertEqual(state["recovery_state"], "ABANDONED")
        self.assertEqual(state["failures"][0]["diagnostic"], "Synthetic discovery source failed")
        self.fixture.calls.clear()
        self.fixture.run_research("restart")
        self.assertEqual(self.fixture.calls, [])


if __name__ == "__main__":
    unittest.main()
