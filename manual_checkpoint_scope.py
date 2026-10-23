"""Offline scoped-checkpoint scenarios using the existing parallel producer."""

import datetime as dt
import hashlib
import json
from pathlib import Path
import sys

import orchestrator
import parallel_research
import research_workflow

sys.path.insert(0, str(Path(__file__).resolve().parent / "tests"))
from test_parallel_research import ParallelResearchTests
import profile_home
import user_profile


def setup():
    fixture = ParallelResearchTests()
    fixture.setUp()
    fixture.config["research_timeout_seconds"] = 30
    research_workflow.seed_history(fixture.database, fixture.pipeline, history_complete=True)
    fixture.database.commit()
    prior = {}
    failed = {"AI", "Defense", "FinTech and Finance"}
    for industry in fixture.industries:
        old_name = "Legacy-only " + industry
        plan = research_workflow.plan_research(fixture.database, "legacy-owner:" + industry,
                                               [old_name], now=fixture.utcnow())
        if industry in failed:
            research_workflow.process_research(fixture.database, {
                "research_status": "PARTIAL", "summary": "Historical raw qualification failure: official domain unverified",
                "blockers": ["Historical official source could not be authenticated"],
                "opportunities": [], "drafts": [], "company_findings": []},
                plan_id=plan["plan_id"], now=fixture.utcnow(), policy=fixture.policy)
        fixture.database.execute("INSERT INTO worker_compute_reservations VALUES (?,?,?,?)",
                                 ("legacy-budget:" + industry, "2026-09-17", 30, fixture.utcnow()))
        fixture.database.commit()
        payload = {"day": "2026-09-17", "industry": industry, "policy_hash": "old-full-policy-hash",
                   "plan_id": plan["plan_id"], "discovery_limit": 1,
                   "result": {"state": "FAILED" if industry in failed else "COMPLETE",
                              "summary": "Historical source failure: official vacancy page unavailable for " + old_name,
                              "blockers": ["Historical eligibility remains unresolved"],
                              "plan_id": plan["plan_id"], "companies": [old_name]}}
        path = fixture.root / "reports" / ("parallel-industry-2026-09-17-" + parallel_research._label(industry) + ".json")
        path.write_text(json.dumps(payload, indent=1))
        prior[path] = path.read_bytes()
    fixture.database.commit()
    preserved = {table: [tuple(row) for row in fixture.database.execute("SELECT * FROM " + table)]
                 for table in ("rw_plans", "rw_companies", "rw_slots", "worker_compute_reservations")}
    decisions = []
    fixture.runtime.research_recovery_methods = lambda root, config: {"public_search": "Existing public source search"}

    def decide(root, config, packet, timeout):
        decisions.append(packet)
        if packet["shared_owners"]:
            return {"action": "ABANDON", "method": "", "instructions": "",
                    "reason": "Synthetic AI: an unresolved existing owner cannot be reassigned", "progress": False}
        return {"action": "RETRY", "method": "public_search", "instructions": "Read another official source within the assignment",
                "reason": "Synthetic AI: supported public search can recover the failed page", "progress": False}

    fixture.runtime.research_recovery_decision = decide
    return fixture, prior, preserved, decisions


def verify_preserved(fixture, prior, preserved, handed_over=()):
    """Nothing prior is lost, except a dead plan's claim deliberately handed to a waiter.

    Owner, 2026-09-23: "5. Should never exist." When a shared owner never completes, its claim
    on exactly those companies is released so the waiting industry can research them itself: the
    rw_companies row is dropped and the dead plan's rw_slots reservation is zeroed. Those exact
    rows are named from what the run reported handing over, so any OTHER loss still fails here.
    """
    assert all(path.read_bytes() == content for path, content in prior.items())
    for table, original in preserved.items():
        current = [tuple(row) for row in fixture.database.execute("SELECT * FROM " + table)]
        moved = set(handed_over)
        expected = [row for row in original
                    if not (table == "rw_companies" and (row[1], row[2]) in moved)
                    and not (table == "rw_slots" and (row[1], row[0]) in moved)]
        assert all(row in current for row in expected), table
    assert fixture.count("application_attempts") == 0


def run_scenario(mode="fresh_recovery"):
    fixture, prior, preserved, decisions = setup()
    try:
        failures = []
        blocked = None
        if mode == "shared_owner":
            fixture.names["Defense"] = ["Legacy-only Defense"]
        if mode == "legacy_audit":
            blocked = next(path for path in prior
                           if parallel_research._label("FinTech and Finance") in path.name)
            blocked.chmod(0)

        def payload(industry, phase, result):
            if mode == "fresh_recovery" and industry == "AI" and phase == "qualification" and not failures:
                failures.append(industry)
                result.update(research_status="PARTIAL", opportunities=[], blockers=["Synthetic current official page failure"])
            return result

        fixture.payload_hook = payload
        result = fixture.run_research("scope-first")
        if blocked:
            blocked.chmod(0o600)
            audit = json.loads(fixture.cache_path("FinTech and Finance").read_text())["prior_checkpoints"]
            record = next(entry for entry in audit if entry.get("path") == str(blocked))
            assert record["diagnostic"] == "PRIOR_CHECKPOINT_UNREADABLE", record
            assert record["error_type"] == "PermissionError", record
            assert fixture.states(result)["FinTech and Finance"]["state"] == "COMPLETE", result
            assert result["state"] == "COMPLETE", result
        # Whatever the run reports it handed over is exactly what may be missing, named from
        # the result rather than guessed, so an unexpected deletion still fails the check.
        handed_over = [(company, entry["owners"])
                       for state in fixture.states(result).values()
                       for entry in [state.get("shared_owner_released")] if entry
                       for company in entry["companies"]]
        verify_preserved(fixture, prior, preserved, handed_over)
        assert all("Legacy-only" not in json.dumps(context) for industry, phase, context in fixture.contexts if phase == "discovery")
        assert len([phase for industry, phase in fixture.calls if phase == "discovery"]) == 8
        if mode == "shared_owner":
            # Owner, 2026-09-23: "5. Should never exist. Fix it." Defense used to be sent into
            # recovery purely because a dead legacy plan held its only company, and the AI
            # abandoned it. The claim is handed over and Defense researches the company itself.
            assert result["state"] == "COMPLETE", result
            defense = fixture.states(result)["Defense"]
            assert defense["state"] == "COMPLETE", defense
            assert defense["shared_owner_released"]["owners"] == "legacy-owner:Defense", defense
            assert defense["shared_owner_released"]["companies"] == ["legacy only defense"], defense
            assert not decisions, "A waiter must not be sent into recovery for its owner's fault"
            assert ("Defense", "qualification") in fixture.calls
        elif True:
            assert result["state"] == "COMPLETE", result
        if mode == "fresh_recovery":
            assert len(decisions) == 1
            packet = decisions[0]
            assert "Synthetic current official page failure" in packet["failure"]["prior_result"]["saved_result_excerpt"]
            assert "Historical source failure" in packet["prior_checkpoints"][0]["diagnostic"]["saved_result_excerpt"]
            saved = packet["prior_checkpoints"][0]["saved_research_result"]
            assert saved["content_identity_valid"] is True
            assert "Historical raw qualification failure" in saved["diagnostic"]["saved_result_excerpt"]
            assert packet["prior_checkpoints"][0]["sha256"] == hashlib.sha256(prior[Path(packet["prior_checkpoints"][0]["path"])]).hexdigest()
            assert fixture.calls.count(("AI", "qualification")) == 2
        first_scope = result["scope_id"]
        scoped_files = {fixture.cache_path(industry): fixture.cache_path(industry).read_bytes() for industry in fixture.industries}
        deadlines = {industry: json.loads(fixture.cache_path(industry).read_text())["deadline_at"] for industry in fixture.industries}
        reservation_count = fixture.count("worker_compute_reservations")
        decision_count = len(decisions)
        fixture.database.close()
        fixture.database = orchestrator.connect(fixture.root)
        fixture.calls.clear()
        if mode == "application_only":
            policy = user_profile.load()
            policy["referrals_block_applications"] = not policy.get("referrals_block_applications", False)
            policy["max_attempts"] += 1
            policy["answers"]["email"]["value"] = "different-contact@example.org"
            policy["answers"]["current_location"]["source"] = "Reworded provenance for the same verified fact"
            policy["answers"]["veteran_status"] = {"value": "Decline to self-identify",
                                                   "semantic": "veteran_self_identification",
                                                   "source": "Application form answer only"}
            policy["source"] = "handoff/resolved-agent-config.yaml; reworded provenance"
            profile_home.use(fixture, policy)
        if mode == "new_scope":
            policy = user_profile.load()
            policy["minimum_base_salary_usd"] += 1000
            profile_home.use(fixture, policy)
            fixture.names = {industry: ["Second scope " + industry] for industry in fixture.industries}
            repeated = fixture.run_research("scope-second")
            assert repeated["state"] == "COMPLETE", repeated
            assert repeated["scope_id"] != first_scope
            assert all(path.read_bytes() == content for path, content in scoped_files.items())
            assert len(fixture.calls) == 16
        elif mode == "deadline":
            path = fixture.cache_path("AI")
            cached = json.loads(path.read_text())
            cached["result"]["state"] = "FAILED"
            cached["result"]["summary"] = "Synthetic interrupted source investigation"
            path.write_text(json.dumps(cached))
            fixture.clock_base += dt.timedelta(seconds=60)
            repeated = fixture.run_research("scope-expired")
            assert fixture.states(repeated)["AI"]["recovery_state"] == "ABANDONED"
            assert "deadline exhausted" in fixture.states(repeated)["AI"]["abandonment_reason"]
            assert not fixture.calls and len(decisions) == decision_count
            assert fixture.count("worker_compute_reservations") == reservation_count
            assert json.loads(path.read_text())["deadline_at"] == deadlines["AI"]
        else:
            repeated = fixture.run_research("scope-replay")
            assert repeated["state"] == result["state"]
            assert repeated["scope_id"] == first_scope
            assert not fixture.calls and len(decisions) == decision_count
            assert fixture.count("worker_compute_reservations") == reservation_count
            assert all(json.loads(fixture.cache_path(industry).read_text())["deadline_at"] == deadlines[industry]
                       for industry in fixture.industries)
        verify_preserved(fixture, prior, preserved, handed_over)
        return {"scenario": mode, "state": "PASS", "first_run": result["state"],
                "ai_decisions": len(decisions), "legacy_checkpoints_preserved": len(prior),
                "network_calls": 0, "application_attempts": 0}
    finally:
        if blocked:
            blocked.chmod(0o600)
        fixture.doCleanups()


if __name__ == "__main__":
    for scenario in ("fresh_recovery", "shared_owner", "application_only", "new_scope", "deadline", "legacy_audit"):
        print(json.dumps(run_scenario(scenario)), flush=True)
