"""Offline manual scenarios exercising the real parallel research coordinator."""

import json
import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent / "tests"))
from test_parallel_research import ParallelResearchTests
import research_recovery


def fixture_setup():
    fixture = ParallelResearchTests()
    temporary_directory = tempfile.TemporaryDirectory
    with patch("tempfile.TemporaryDirectory", side_effect=lambda: temporary_directory(dir=Path(__file__).resolve().parent)):
        fixture.setUp()
    return fixture


def synthetic_inability(config, *, no_alternative=False):
    return {"industry": "Synthetic industry", "day": "2026-09-17", "plan_id": "synthetic-only",
            "decision_number": 1, "prior_decisions": [], "failures": [], "shared_owners": [],
            "supported_methods": {} if no_alternative else research_recovery.research_recovery_methods(Path(__file__).parent, config),
            "failure": {"stage": "qualification", "error_class": "SOURCE_UNAVAILABLE",
                        "diagnostic": "SYNTHETIC TEST: one public directory is unavailable. Existing public search remains available. No company facts were verified.",
                        "worker_inability": {"attempted_methods": ["directory_read"], "progress": False}},
            "progress_evidence": {"summary": "No verified research results. This packet is synthetic, not a real employer."}}


def executor_scenario():
    import orchestrator
    fixture = fixture_setup()
    config = {"codex": "/MOCK_DO_NOT_EXECUTE/codex", "model": "synthetic-model"}
    expected = {"action": "RETRY", "method": "public_search", "instructions": "Search within the existing assignment",
                "reason": "Another readable public source can be investigated", "progress": True}
    calls = []

    class Worker:
        returncode = 0
        pid = 987654321

        def __init__(self, command, **kwargs):
            self.command = command
            calls.append((command, kwargs))

        def communicate(self, prompt, timeout):
            assert timeout <= 1
            assert 'web_search="disabled"' in self.command
            assert 'web_search="live"' not in self.command
            for setting in ('forced_login_method="chatgpt"', "features.shell_tool=false", "features.apps=false",
                            "features.unified_exec=false", "features.multi_agent=false", "mcp_servers={}"):
                assert setting in self.command
            output = Path(self.command[self.command.index("--output-last-message") + 1])
            output.write_text(json.dumps(expected))

        def wait(self):
            return 0

    try:
        # The fake worker has no process to contain or kill (portable: a process group on POSIX, a
        # Job Object on Windows).
        with patch.object(orchestrator.subprocess, "Popen", Worker), \
                patch.object(orchestrator.portable, "contain", lambda worker: None), \
                patch.object(orchestrator.portable, "kill_tree", lambda pid: None), \
                patch("signal.signal", side_effect=AssertionError("No off-thread signal handlers")):
            with ThreadPoolExecutor(max_workers=1) as executor:
                result = executor.submit(research_recovery.research_recovery_decision, fixture.root, config,
                                         synthetic_inability(config), 30, runtime=orchestrator).result()
        assert result == expected
        assert len(calls) == 1
        artifacts = list((fixture.root / "reports/research-recovery").glob("decision-*"))
        assert len(artifacts) == 1
        assert json.loads((artifacts[0] / "decision.json").read_text()) == expected
        assert (artifacts[0] / "recovery-schema.json").is_file()
        assert (artifacts[0] / "executor.log").is_file()
        assert fixture.count("worker_compute_reservations") == 0
        return {"scenario": "executable_callback_off_thread", "state": "PASS", "process_calls": 1, "live_actions": 0}
    finally:
        fixture.doCleanups()


def real_model_scenario(codex, model):
    root = Path(__file__).resolve().parent
    config = {"codex": codex, "model": model}
    for no_alternative in (False, True):
        packet = synthetic_inability(config, no_alternative=no_alternative)
        packet["artifacts"] = research_recovery.create_artifacts(root)
        with ThreadPoolExecutor(max_workers=1) as executor:
            decision = executor.submit(research_recovery.research_recovery_decision, root, config, packet, 30).result()
        research_recovery.validate_decision(decision, packet["supported_methods"], [])
        if no_alternative:
            assert decision["action"] == "ABANDON", decision
        print(json.dumps({"scenario": "real_model_synthetic_no_alternative" if no_alternative else "real_model_synthetic_alternative",
                          "decision": decision, "model": model, "artifacts": packet["artifacts"],
                          "research_executed": False, "business_writes": 0}), flush=True)


def scenario(mode, *, first_decision=None):
    fixture = fixture_setup()
    decisions = []
    failure_counts = {}
    retry_prompts = []
    original_worker = fixture.runtime.run_research_worker

    def worker(root, command, prompt, log, timeout):
        if prompt.startswith("ORCHESTRATOR RECOVERY DIRECTIVE"):
            retry_prompts.append(prompt)
        return original_worker(root, command, prompt, log, timeout)

    fixture.runtime.run_research_worker = worker
    fixture.runtime.research_recovery_methods = lambda root, config: {
        "public_search": "Existing public web search and readable source pages",
        "observed_links": "Existing page reader following observed official links",
    }

    def decide(root, config, inability, timeout):
        assert 0 < timeout <= 30
        decisions.append(inability)
        if first_decision is not None and len(decisions) == 1:
            return dict(first_decision)
        if mode in {"abandon", "orphan"}:
            return {"action": "ABANDON", "method": "", "instructions": "",
                    "reason": "No supported alternative", "progress": False}
        return {"action": "RETRY", "method": "public_search" if len(decisions) == 1 else "observed_links",
                "instructions": "Use the supported alternative within original scope.",
                "reason": "Mock contextual recovery assessment",
                "progress": mode != "first_alternative" and (mode != "no_progress" or len(decisions) == 1)}

    fixture.runtime.research_recovery_decision = decide

    def payload(industry, phase, result):
        if industry == "Defense" and phase == "qualification":
            failure_counts[industry] = failure_counts.get(industry, 0) + 1
            if mode not in {"retry", "first_alternative"} or failure_counts[industry] == 1:
                result.update(research_status="PARTIAL", opportunities=[], blockers=["Unreadable source"],
                              inability={"error_class": "SOURCE_UNAVAILABLE", "diagnostic": "Mock source inaccessible",
                                         "attempted_methods": ["original_source"], "required_input": "",
                                         "progress": False})
        return result

    fixture.payload_hook = payload
    if mode == "orphan":
        import research_workflow
        research_workflow.plan_research(fixture.database, "orphan-owner", ["Shared orphan"], now=fixture.utcnow())
        fixture.names["Defense"] = ["Shared orphan"]
    try:
        result = fixture.run_research("manual-" + mode)
        state = fixture.states(result)["Defense"]
        assert len(fixture.ingested) >= 7, result
        assert state.get("failures"), state
        if mode in {"retry", "first_alternative"}:
            assert result["state"] == "COMPLETE", result
            assert len(decisions) == 1
            assert failure_counts["Defense"] == 2
            assert len(retry_prompts) == 1
            assert "public_search" in retry_prompts[0]
            assert state["plan_id"] == state["failures"][0]["plan_id"]
            if mode == "first_alternative":
                assert state["recovery_decisions"][0]["decision"]["progress"] is False
        else:
            assert result["state"] == "COMPLETE", result
            assert state["state"] == "FAILED" and state["recovery_state"] == "ABANDONED", state
            expected = 2 if mode in {"exhaustion", "no_progress"} else 1
            assert len(decisions) == expected, decisions
            assert failure_counts.get("Defense", 0) <= 3
            assert result["retryable"] is False
            assert result["stop_reason"] == "ALL_UNRESOLVED_INDUSTRIES_ABANDONED"
            if mode == "no_progress":
                assert failure_counts["Defense"] == 2
            if mode == "orphan":
                # Owner, 2026-09-23: "5. Should never exist. Fix it." Defense used to be sent
                # into recovery carrying its dead owner in the packet, and the AI abandoned it
                # for a fault that was never its own. The orphan plan's claim is handed over
                # first, so Defense researches the company itself and any later recovery is
                # about ITS OWN failure, with no shared owner attached.
                assert all(not entry.get("shared_owners") for entry in decisions), decisions
                assert state["shared_owner_released"]["owners"] == "orphan-owner", state
                assert state["shared_owner_released"]["companies"] == ["shared orphan"], state
                assert state["abandonment_reason"] == state["recovery_decisions"][0]["decision"]["reason"]
        original_failures = state["failures"]
        if first_decision is not None:
            assert state["recovery_decisions"][0]["decision"] == first_decision
        raw_failures = {failure["report"]: Path(failure["report"]).read_bytes()
                        for failure in original_failures if failure.get("report")}
        fixture.database.close()
        import orchestrator
        fixture.database = orchestrator.connect(fixture.root)
        fixture.calls.clear()
        decision_count = len(decisions)
        restarted = fixture.run_research("manual-restart-" + mode)
        assert not fixture.calls, fixture.calls
        assert len(decisions) == decision_count
        assert fixture.states(restarted)["Defense"]["failures"] == original_failures
        assert all(Path(path).read_bytes() == raw for path, raw in raw_failures.items())
        if mode == "orphan":
            # The dead owner's claim was released to the waiter; that is the fix, not data loss.
            assert fixture.database.execute("SELECT COUNT(*) FROM rw_companies WHERE plan_id='orphan-owner'").fetchone()[0] == 0
        assert fixture.count("application_attempts") == 0
        return {"scenario": mode, "state": result["state"], "industry_state": state["state"],
                "recovery_decisions": len(decisions), "failures_retained": len(original_failures),
                "restart_worker_calls": len(fixture.calls), "live_actions": 0}
    finally:
        fixture.doCleanups()


def replay_real_decisions():
    source = Path(__file__).resolve().parent / "reports/real-research-recovery.jsonl"
    records = [json.loads(line) for line in source.read_text().splitlines() if line.strip()]
    modes = {"real_model_synthetic_alternative": "first_alternative", "real_model_synthetic_no_alternative": "abandon"}
    assert {record["scenario"] for record in records} == set(modes)
    for record in records:
        original = Path(record["artifacts"]["raw"]).read_bytes()
        decision = json.loads(original)
        assert decision == record["decision"]
        result = scenario(modes[record["scenario"]], first_decision=decision)
        assert Path(record["artifacts"]["raw"]).read_bytes() == original
        print(json.dumps(dict(result, actual_ai_decision_replayed=True, judgment_source=record["artifacts"]["raw"])), flush=True)


def checkpoint_loss_scenario(mode):
    import orchestrator
    fixture = fixture_setup()
    decisions = []
    fixture.runtime.research_recovery_methods = lambda root, config: {"public_search": "Existing public search"}

    def decide(root, config, packet, timeout):
        decisions.append(packet)
        return {"action": "ABANDON", "method": "", "instructions": "", "reason": "Synthetic checkpoint loss", "progress": False}

    fixture.runtime.research_recovery_decision = decide
    try:
        assert fixture.run_research()["state"] == "COMPLETE"
        tables = ("worker_compute_reservations", "rw_companies", "rw_results")
        before = {table: [tuple(row) for row in fixture.database.execute("SELECT * FROM " + table)] for table in tables}
        path = fixture.cache_path("Defense")
        if mode == "missing":
            path.unlink()
        elif mode == "corrupt":
            path.write_text("{unreadable checkpoint")
        else:
            checkpoint = json.loads(path.read_text())
            if mode == "rolled_back":
                checkpoint["compute_sequence"] = 0
            elif mode == "deadline_missing":
                checkpoint.pop("deadline_at")
            path.write_text(json.dumps(checkpoint))
        broken = path.read_bytes() if path.exists() else None
        fixture.database.close()
        fixture.database = orchestrator.connect(fixture.root)
        fixture.calls.clear()
        for number in range(2):
            result = fixture.run_research("checkpoint-loss-" + mode + "-" + str(number))
            assert fixture.calls == [], fixture.calls
            assert decisions == [], decisions
            assert result["state"] == "COMPLETE"
            assert fixture.states(result)["Defense"]["recovery_state"] == "ABANDONED"
            assert sum(state["state"] == "COMPLETE" for state in result["industries"]) == 7
            assert (path.read_bytes() if path.exists() else None) == broken
            assert {table: [tuple(row) for row in fixture.database.execute("SELECT * FROM " + table)] for table in tables} == before
        return {"scenario": "checkpoint_" + mode, "state": "PASS", "new_worker_calls": 0,
                "new_judgments": 0, "reservations_and_history_unchanged": True}
    finally:
        fixture.doCleanups()


def attempted_method_scenario(mode):
    fixture = fixture_setup()
    decisions = []
    attempts = []
    methods = {"public_search": "Existing public search", "observed_links": "Existing page reader"}
    fixture.runtime.research_recovery_methods = lambda root, config: {"public_search": methods["public_search"]} if mode == "all_attempted" else methods

    def payload(industry, phase, result):
        if industry == "Defense" and phase == "qualification":
            attempts.append(phase)
            if len(attempts) == 1:
                result.update(research_status="PARTIAL", opportunities=[], blockers=["Public search attempted without useful results"],
                              inability={"error_class": "SOURCE_UNAVAILABLE", "diagnostic": "Synthetic public search failed",
                                         "attempted_methods": ["public_search"], "required_input": "", "progress": False})
        return result

    def decide(root, config, packet, timeout):
        decisions.append(packet)
        return {"action": "RETRY", "method": "observed_links" if mode == "alternative_method" else "public_search",
                "instructions": "Stay within the original assignment", "reason": "Synthetic method selection", "progress": False}

    fixture.payload_hook = payload
    fixture.runtime.research_recovery_decision = decide
    try:
        result = fixture.run_research("attempted-" + mode)
        assert all("public_search" not in packet["supported_methods"] for packet in decisions)
        assert len(attempts) == (2 if mode == "alternative_method" else 1), attempts
        assert len(decisions) == (0 if mode == "all_attempted" else 1)
        assert result["state"] == "COMPLETE"
        fixture.calls.clear()
        decision_count = len(decisions)
        fixture.run_research("attempted-restart-" + mode)
        assert fixture.calls == [] and len(decisions) == decision_count
        return {"scenario": mode, "state": "PASS", "qualification_calls": len(attempts), "judgments": len(decisions)}
    finally:
        fixture.doCleanups()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-model", action="store_true", help="Run only the tools-disabled synthetic subscription judge; no research or business actions")
    parser.add_argument("--replay-real-decisions", action="store_true", help="Replay retained real-model decisions through the mocked parallel coordinator; no model call")
    parser.add_argument("--audit-regressions", action="store_true", help="Mock checkpoint-loss and already-attempted method recovery")
    parser.add_argument("--codex")
    parser.add_argument("--model")
    options = parser.parse_args()
    if options.real_model and options.replay_real_decisions:
        parser.error("Choose either a new synthetic judgment or replay of retained judgments")
    if options.audit_regressions:
        for mode in ("missing", "corrupt", "rolled_back", "deadline_missing"):
            print(json.dumps(checkpoint_loss_scenario(mode)), flush=True)
        for mode in ("same_method", "alternative_method", "all_attempted"):
            print(json.dumps(attempted_method_scenario(mode)), flush=True)
    elif options.replay_real_decisions:
        replay_real_decisions()
    elif options.real_model:
        if not options.codex or not options.model:
            parser.error("--real-model requires explicit --codex and --model")
        real_model_scenario(options.codex, options.model)
    else:
        print(json.dumps(executor_scenario()), flush=True)
        for mode in ("retry", "first_alternative", "abandon", "no_progress", "exhaustion", "orphan"):
            print(json.dumps(scenario(mode)), flush=True)
