import datetime as dt
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import applications
import orchestrator
import parallel_research
import research_workflow
import workflow_tasks
import profile_home
import user_profile


ROOT = Path(__file__).resolve().parents[1]


class ParallelResearchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "state").mkdir()
        (self.root / "reports").mkdir()
        for filename in ("discovery-prompt.md",):
            shutil.copyfile(ROOT / filename, self.root / filename)
        self.policy = user_profile.load()
        self.industries = self.policy["industries"]
        self.database = orchestrator.connect(self.root)
        self.addCleanup(lambda: self.database.close())
        research_workflow.initialize(self.database)
        workflow_tasks.initialize(self.database)
        applications.initialize(self.database)
        self.pipeline = {"companies": [], "rules": {"max_unique_contacts_per_company": 3}}
        self.config = {"research_timeout_seconds": 10, "daily_compute_seconds": 10800}
        self.clock_base = dt.datetime(2026, 9, 17, 18, 0, tzinfo=dt.timezone.utc)
        self.started = time.monotonic()
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.calls = []
        self.contexts = []
        self.prompts = []
        self.ingested = []
        self.plans = {}
        self.names = {}
        self.fill_allowance = False
        self.worker_hook = lambda industry, phase: None
        self.payload_hook = lambda industry, phase, payload: payload
        self.store_hook = lambda industry: None
        self.runtime = SimpleNamespace(
            RESEARCH_STOP=self.stop, utcnow=self.utcnow, write_json=self.write_json,
            research_command=lambda root, config, output, schema: (schema, output),
            research_inputs=self.research_inputs, run_research_worker=self.worker,
            store_research=self.store_research)
        for target in ("socket.socket.connect", "socket.create_connection", "subprocess.Popen",
                       "orchestrator.HeyReach"):
            guard = patch(target, side_effect=AssertionError("External calls prohibited in coordinator tests"))
            guard.start()
            self.addCleanup(guard.stop)

    def utcnow(self):
        return (self.clock_base + dt.timedelta(seconds=time.monotonic() - self.started)).isoformat()

    def write_json(self, path, payload):
        self.assertIs(threading.current_thread(), threading.main_thread())
        Path(path).write_text(json.dumps(payload))

    def research_inputs(self, root, config, database, pipeline, observation, output, *, companies, industry, discovery):
        self.assertIs(threading.current_thread(), threading.main_thread())
        self.assertEqual(discovery, json.loads(self.cache_path(industry).read_text())["discovery"])
        self.assertEqual(discovery["discovery_status"], "COMPLETE")
        self.assertTrue(all(record["industry"] == industry for record in discovery["companies"]))
        research_workflow.seed_history(database, pipeline, history_complete=True)
        plan_id = "test-plan:" + parallel_research._day(self.utcnow()) + ":" + industry
        if config.get("_research_scope_id"):
            plan_id += ":scope:" + config["_research_scope_id"]
        plan = research_workflow.get_plan(database, plan_id, now=self.utcnow())
        if plan:
            research_workflow.hold_unreported_contact_slots(database, plan_id)
            plan = research_workflow.get_plan(database, plan_id, now=self.utcnow())
        else:
            plan = research_workflow.plan_research(database, plan_id, companies, now=self.utcnow(), limit=60)
        self.assertFalse(database.in_transaction)
        self.plans[industry] = plan
        assigned = {entry["company_key"] for entry in plan["assignments"]}
        records = [record for record in discovery["companies"]
                   if research_workflow.canonical(database, "company", record["company"]) in assigned]
        self.assertCountEqual([research_workflow.canonical(database, "company", record["company"])
                               for record in records], assigned)
        context = {"industry": industry, "companies": [entry["company"] for entry in plan["assignments"]],
                   "discovery_evidence": records}
        return plan, plan_id, json.dumps(context), ("research-schema.json", output)

    def worker(self, root, command, prompt, log, timeout):
        self.assertIsNot(threading.current_thread(), threading.main_thread())
        self.assertGreater(timeout, 0)
        schema, output = command
        phase = "discovery" if schema == "discovery-schema.json" else "qualification"
        context = json.loads(prompt.rsplit("\n\n", 1)[-1])
        industry = context["industries_in_priority_order"][0] if phase == "discovery" else context["industry"]
        with self.lock:
            self.calls.append((industry, phase))
            self.contexts.append((industry, phase, context))
            self.prompts.append((industry, phase, prompt))
        log.write_text("LOCAL FAKE WORKER; NO NETWORK")
        self.worker_hook(industry, phase)
        if self.stop.is_set():
            raise InterruptedError("Local fake observed stop")
        if phase == "discovery":
            count = context["maximum_companies"] if self.fill_allowance else 1
            names = self.names.get(industry, [f"Fixture {industry} {number}" for number in range(count)])
            payload = {"discovery_status": "COMPLETE", "summary": "Synthetic industry search", "blockers": [],
                       "companies": [{"company": name, "industry": industry, "source": "https://www.nasa.gov/",
                                      "observed_at": self.utcnow(), "excerpt": name + " synthetic industry evidence", "excerpt_names_company": True}
                                     for name in names]}
        else:
            self.assertCountEqual([record["company"] for record in context["discovery_evidence"]], context["companies"])
            payload = {"research_status": "COMPLETE", "summary": "Synthetic qualification", "blockers": [],
                       "action_blockers": [], "drafts": [], "company_findings": [],
                       "scope_violation": {"unknown": False, "companies": []},
                       "opportunities": [self.role(name, industry) for name in context["companies"]]}
        output.write_text(json.dumps(self.payload_hook(industry, phase, payload)))

    def role(self, company, industry):
        requisition = hashlib.sha256(company.encode()).hexdigest()[:12]
        url = "https://careers.example/jobs/" + requisition
        facts = {"company": company, "role_title": "Staff Software Engineer", "open": True,
                 "funding_stage": "Series B", "industry": industry, "level": "Staff Software Engineer",
                 "specialization": "backend", "country": "US", "city": "Arlington", "work_mode": "remote",
                 "requirements_met": True, "authorization": "US_PERMANENT_RESIDENT_ACCEPTED",
                 "hiring_geography_includes_candidate": True, "citizenship_clearance_requirements_met": True,
                 "employee_count": "51-200", "official_destination": url,
                 "compensation": {"minimum": 200000, "maximum": 250000, "currency": "USD", "period": "annual_base"}}
        return {"company": company, "role": "Staff Software Engineer", "url": url, "requisition_id": requisition,
                "rationale": "SYNTHETIC TEST ONLY", "people": [],
                "evidence": [{"field": field, "value": value, "source": url, "observed_at": self.utcnow(),
                              "effective_at": None, "excerpt": "Synthetic fixture evidence",
                              "verified": True, "kind": "official"} for field, value in facts.items()]}

    def store_research(self, database, pipeline, result):
        self.assertIs(threading.current_thread(), threading.main_thread())
        rejected = orchestrator.store_research(database, pipeline, result)
        industry = next(industry for industry, plan in self.plans.items() if plan["plan_id"] == result["plan_id"])
        self.ingested.append(industry)
        self.store_hook(industry)
        return rejected

    def run_research(self, attempt="first", deadline=None):
        return parallel_research.run(self.root, self.config, self.database, self.pipeline, {}, attempt,
                                     runtime=self.runtime, deadline=deadline if deadline is not None else time.monotonic() + 600)

    def states(self, result):
        return {state["industry"]: state for state in result["industries"]}

    def count(self, table):
        return self.database.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]

    def cache_path(self, industry):
        policy = user_profile.load()
        return parallel_research.checkpoint_path(self.root, parallel_research._day(self.utcnow()),
                                                 industry, parallel_research.scope_id(policy))

    def test_eight_workers_advance_six_before_slow_owner_and_preserve_terminal_failure(self):
        barrier = threading.Barrier(8)
        fast_ingested = threading.Event()

        def worker_hook(industry, phase):
            if phase == "discovery":
                barrier.wait(timeout=3)
                if industry == "Defense":
                    raise RuntimeError("Synthetic discovery failure")
            elif industry == "Energy":
                self.assertTrue(fast_ingested.wait(3), "Fast industries waited for the slow industry")

        def store_hook(industry):
            if len(self.ingested) == 6:
                self.assertNotIn("Energy", self.ingested)
                self.assertEqual(self.count("application_jobs"), 6)
                fast_ingested.set()

        self.worker_hook = worker_hook
        self.store_hook = store_hook
        first = self.run_research()
        states = self.states(first)
        self.assertEqual(first["state"], "COMPLETE")
        self.assertEqual(sum(state["state"] == "COMPLETE" for state in states.values()), 7)
        self.assertEqual(states["Defense"]["state"], "FAILED")
        for industry in set(self.industries) - {"Energy", "Defense"}:
            self.assertLess(states[industry]["completed_at"], states["Energy"]["completed_at"])
        self.assertEqual(len([call for call in self.calls if call[1] == "discovery"]), 8)
        self.assertEqual(self.count("application_jobs"), 7)
        self.worker_hook = lambda industry, phase: None
        self.store_hook = lambda industry: None
        self.calls.clear()
        retry = self.run_research("retry")
        self.assertEqual(retry["state"], "COMPLETE")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.states(retry)["Defense"]["recovery_state"], "ABANDONED")
        self.assertEqual(sum(state.get("reused", False) for state in retry["industries"]), 8)
        self.database.close()
        self.database = orchestrator.connect(self.root)
        self.assertEqual(self.count("application_jobs"), 7)
        self.assertEqual(self.count("application_attempts"), 0)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM application_jobs WHERE state='SUBMITTED'").fetchone()[0], 0)

    def test_terminal_qualification_failure_preserves_discovery_and_unknown_contact_reservations(self):
        def fail_qualification(industry, phase):
            if industry == "Defense" and phase == "qualification":
                raise RuntimeError("Synthetic qualification failure")

        self.worker_hook = fail_qualification
        first = self.run_research()
        state = self.states(first)["Defense"]
        raw = Path(state["discovery_report"]).read_bytes()
        self.assertEqual(first["state"], "COMPLETE")
        self.assertGreater(self.database.execute("SELECT SUM(remaining) FROM rw_slots WHERE plan_id=?", (state["plan_id"],)).fetchone()[0], 0)
        self.worker_hook = lambda industry, phase: None
        self.calls.clear()
        retry = self.run_research("retry")
        self.assertEqual(retry["state"], "COMPLETE")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.states(retry)["Defense"]["failures"], state["failures"])
        self.assertEqual(Path(state["discovery_report"]).read_bytes(), raw)
        self.assertEqual(self.count("rw_companies"), 8)
        self.assertGreater(self.database.execute("SELECT SUM(remaining) FROM rw_slots WHERE plan_id=?", (state["plan_id"],)).fetchone()[0], 0)

    def test_shared_company_has_one_plan_one_job_and_cache_only_retry_has_no_compute(self):
        self.names.update({"AI": ["Shared Fixture"], "Cybersecurity": ["Shared Fixture"]})
        first = self.run_research()
        self.assertEqual(first["state"], "COMPLETE")
        self.assertEqual(self.count("rw_plans"), 7)
        self.assertEqual(self.count("application_jobs"), 7)
        shared = next(state for state in first["industries"] if state.get("shared_owners"))
        self.assertNotIn("plan_id", shared)
        self.assertNotIn("result_id", shared)
        owner = next(state for state in first["industries"] if state.get("plan_id") == shared["shared_owners"][0]["plan_id"])
        self.assertLessEqual(owner["completed_at"], shared["completed_at"])
        reservations = self.count("worker_compute_reservations")
        self.calls.clear()
        retry = self.run_research("retry")
        self.assertEqual(retry["state"], "COMPLETE")
        self.assertEqual(self.calls, [])
        self.assertTrue(all(state.get("reused") for state in retry["industries"]))
        self.assertEqual(self.count("worker_compute_reservations"), reservations)
        self.assertEqual(self.count("application_jobs"), 7)

    def test_a_failed_shared_owner_hands_its_companies_to_the_waiting_industry(self):
        """Owner, 2026-09-23: "5. Should never exist. Fix it."

        An industry parks in WAITING_SHARED_RESEARCH only because another industry's plan
        claimed the same company first. When that owner failed, the waiter was failed with
        SHARED_RESEARCH_OWNER_INCOMPLETE and then abandoned terminally, throwing away its own
        valid discovery for a fault that was never its own. The dead owner's claim on exactly
        those companies is now dropped so the waiter can research them itself.
        """
        self.names.update({"AI": ["Shared Fixture"], "Cybersecurity": ["Shared Fixture"]})

        def fail_shared(industry, phase):
            if industry in {"AI", "Cybersecurity"} and phase == "qualification":
                raise RuntimeError("Synthetic owner failure")

        self.worker_hook = fail_shared
        first = self.run_research()
        self.assertEqual(sum(state["state"] == "COMPLETE" for state in first["industries"]), 6)
        dependents = [state for state in first["industries"] if state.get("shared_owner_released")]
        self.assertTrue(dependents, "The waiter must be handed the dead owner's companies")
        for state in dependents:
            self.assertTrue(state["shared_owner_released"]["companies"])
            self.assertNotIn("SHARED_RESEARCH_OWNER_INCOMPLETE", state.get("error") or "",
                             "A waiter must not inherit its owner's failure")
        self.assertFalse(any(state.get("shared_owners") for state in first["industries"]),
                         "No industry is left parked on a dead owner")
        reservations = self.count("worker_compute_reservations")
        self.worker_hook = lambda industry, phase: None
        self.calls.clear()
        retry = self.run_research("retry")
        self.assertEqual(self.calls, [], "A completed industry is never replayed on retry")
        self.assertEqual(self.count("worker_compute_reservations"), reservations,
                         "The retry reserves no further compute for work already done")

    def test_mixed_shared_discovery_qualifies_only_unique_assignments(self):
        owner_started = threading.Event()
        self.names.update({"FinTech and Finance": ["Shared Fixture"], "Energy": ["Shared Fixture", "Unique Energy"]})

        def order_workers(industry, phase):
            if industry == "FinTech and Finance" and phase == "qualification":
                owner_started.set()
            if industry == "Energy" and phase == "discovery":
                self.assertTrue(owner_started.wait(3))

        self.worker_hook = order_workers
        result = self.run_research()
        self.assertEqual(result["state"], "COMPLETE")
        self.assertEqual(self.plans["Energy"]["assignments"][0]["company"], "Unique Energy")
        self.assertEqual(len(self.plans["Energy"]["assignments"]), 1)
        self.assertEqual(len(self.states(result)["Energy"]["shared_owners"]), 1)
        self.assertEqual(self.count("application_jobs"), 8)

    def test_default_twelve_total_and_existing_reservations_share_global_budgets(self):
        self.config["research_timeout_seconds"] = 600
        self.fill_allowance = True
        research_workflow.plan_research(self.database, "old-serial", [f"Old {number}" for number in range(22)],
                                        now=self.utcnow(), limit=60)
        with self.database:
            workflow_tasks.reserve_compute(self.database, "old-compute", self.utcnow(), 4200)
        old_cache = self.root / "reports/industry-discovery-2026-09-17.json"
        old_cache.write_text('{"companies": ["OLD_SERIAL_SEED_NEVER_USE"]}')
        result = self.run_research()
        self.assertEqual(result["state"], "COMPLETE")
        self.assertEqual(result["company_usage"], 34)
        self.assertEqual(sorted(state["discovery_limit"] for state in result["industries"]), [1, 1, 1, 1, 2, 2, 2, 2])
        reservations = list(self.database.execute("SELECT action_id,seconds FROM worker_compute_reservations WHERE action_id LIKE 'research:%'"))
        self.assertEqual(len(reservations), 16)
        self.assertTrue(all(0 < row[1] <= 120 for row in reservations))
        self.assertLessEqual(self.database.execute("SELECT SUM(seconds) FROM worker_compute_reservations").fetchone()[0], 9000)
        for industry, phase, context in self.contexts:
            if phase == "discovery":
                self.assertEqual(context["industries_in_priority_order"], [industry])
                self.assertNotIn("companies", context)
                self.assertNotIn("pipeline", context)
                self.assertNotIn("history", context)
                self.assertNotIn("OLD_SERIAL_SEED_NEVER_USE", json.dumps(context))

    def test_mixed_legacy_owner_failure_preserves_own_ingestion_and_never_repeats_it(self):
        research_workflow.plan_research(self.database, "failed-serial-owner", ["Legacy Shared Fixture"],
                                        now=self.utcnow())
        self.names["Defense"] = ["Legacy Shared Fixture", "Unique Defense Fixture"]
        first = self.run_research()
        defense = self.states(first)["Defense"]
        # Owner, 2026-09-23: "5. Should never exist. Fix it." Defense researched its own unique
        # company perfectly well and was still failed, because an ORPHAN plan from an earlier run
        # held the claim on the other name. The dead plan's claim is handed over instead.
        self.assertEqual(first["state"], "COMPLETE")
        self.assertEqual(sum(state["state"] == "COMPLETE" for state in first["industries"]), 8)
        self.assertEqual(defense["state"], "COMPLETE")
        self.assertIsNone(defense.get("error"))
        self.assertEqual(defense["shared_owner_released"]["owners"], "failed-serial-owner")
        self.assertEqual(defense["shared_owner_released"]["companies"], ["legacy shared fixture"])
        self.assertEqual(defense["companies"], ["Unique Defense Fixture"])
        self.assertTrue(defense["completed_at"])
        self.assertEqual(self.count("application_jobs"), 8)
        processed = self.database.execute("SELECT processed FROM rw_results WHERE id=?", (defense["result_id"],)).fetchone()
        self.assertEqual(json.loads(processed[0])["research_status"], "COMPLETE")
        reservations = self.count("worker_compute_reservations")
        self.database.close()
        self.database = orchestrator.connect(self.root)
        self.calls.clear()
        retry = self.run_research("retry")
        retried_defense = self.states(retry)["Defense"]
        self.assertTrue(retried_defense["reused"], "Completed work is reused, never repeated")
        self.assertEqual(retried_defense["result_id"], defense["result_id"])
        self.assertEqual(retried_defense["completed_at"], defense["completed_at"])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.count("worker_compute_reservations"), reservations)
        self.assertEqual(self.count("application_jobs"), 8)
        self.assertEqual(self.count("rw_plans"), 9)
        self.assertEqual(self.count("application_attempts"), 0)

    def test_missing_source_identity_prevents_worker_claim_of_complete(self):
        def omit_source_identity(industry, phase, payload):
            if industry == "Defense" and phase == "qualification":
                for role in payload["opportunities"]:
                    role["evidence"] = [item for item in role["evidence"]
                                        if item["field"] not in {"company", "role_title", "open"}]
            return payload

        self.payload_hook = omit_source_identity
        result = self.run_research()
        defense = self.states(result)["Defense"]
        self.assertEqual(result["state"], "COMPLETE")
        self.assertEqual(sum(state["state"] == "COMPLETE" for state in result["industries"]), 8)
        self.assertEqual(defense["worker_status"], "PARTIAL")
        self.assertTrue(any(blocker.startswith("COMPANY_COVERAGE_INCOMPLETE") for blocker in defense["blockers"]))
        raw = json.loads(Path(defense["report"]).read_text())
        self.assertEqual(raw["research_status"], "COMPLETE")
        processed = json.loads(Path(defense["processed_report"]).read_text())
        self.assertIsNone(processed["opportunities"][0]["review"]["facts"]["open"])
        self.assertEqual(self.count("application_attempts"), 0)

    def test_remaining_company_capacity_is_a_ceiling_and_skipped_industries_fail(self):
        research_workflow.plan_research(self.database, "existing", [f"Old {number}" for number in range(57)],
                                        now=self.utcnow(), limit=60)
        result = self.run_research()
        self.assertEqual(result["state"], "COMPLETE")
        self.assertEqual(result["company_usage"], 60)
        self.assertEqual(sum(state["state"] == "COMPLETE" for state in result["industries"]), 3)
        self.assertEqual(len(self.calls), 6)
        self.assertEqual(self.count("worker_compute_reservations"), 6)

    def test_configured_batch_can_allocate_more_than_three_per_industry(self):
        self.config["research_company_limit"] = 40
        self.fill_allowance = True
        result = self.run_research()
        self.assertEqual(result["state"], "COMPLETE")
        self.assertTrue(all(state["discovery_limit"] == 5 for state in result["industries"]))
        self.assertEqual(result["company_usage"], 40)

    def test_compute_ceiling_blocks_new_phases_without_refunding_completed_discovery(self):
        self.config.update(research_timeout_seconds=600, daily_compute_seconds=25000)
        with self.database:
            workflow_tasks.reserve_compute(self.database, "prior", self.utcnow(), 20400, daily_seconds=21000)
        result = self.run_research()
        self.assertEqual(result["state"], "PARTIAL")  # a pure compute wait is PARTIAL (owner, 2026-09-25)
        self.assertEqual(sum(state["state"] == "COMPLETE" for state in result["industries"]), 0)
        self.assertEqual(len(self.calls), 5)
        self.assertTrue(all(phase == "discovery" for industry, phase in self.calls))
        self.assertLessEqual(self.database.execute("SELECT SUM(seconds) FROM worker_compute_reservations").fetchone()[0], 21000)
        # AGENTS.md: a spent allowance is a WAIT. The ceiling must never abandon an industry
        # or burn the day; every blocked industry stays retryable with a recorded next retry.
        self.assertTrue(result["retryable"])
        self.assertIs(result["compute_wait"], True)
        self.assertEqual(result["stop_reason"], "COMPUTE_WAIT_ALL_UNRESOLVED_INDUSTRIES")
        self.assertEqual(result["abandoned_industries"], [])
        self.assertCountEqual(result["compute_waiting_industries"], self.industries)
        for state in result["industries"]:
            self.assertEqual(state["state"], "COMPUTE_WAIT")
            self.assertNotIn("recovery_state", state)
            self.assertTrue(state["next_retry_at"] > state["compute_waits"][0]["observed_at"])
            self.assertEqual(state["compute_waits"][0]["reason"], "DAILY_COMPUTE_ALLOWANCE_EXHAUSTED")

    def test_compute_wait_never_spends_a_recovery_decision_on_an_empty_budget(self):
        """The observed 2026-09-18 chain: planning raised, recovery could not reserve, all abandoned."""
        decisions = []
        self.runtime.research_recovery_methods = lambda root, config: {"public_search": "fixture"}
        self.runtime.research_recovery_decision = lambda root, config, packet, timeout: decisions.append(packet)
        self.config.update(research_timeout_seconds=600, daily_compute_seconds=10800)
        with self.database:
            workflow_tasks.reserve_compute(self.database, "prior", self.utcnow(), 10800)
        result = self.run_research()
        self.assertEqual(decisions, [], "an empty budget is never an AI abandonment judgment")
        self.assertEqual(self.calls, [])
        self.assertTrue(result["retryable"])
        self.assertIs(result["compute_wait"], True)
        self.assertEqual(result["abandoned_industries"], [])
        for state in result["industries"]:
            self.assertEqual(state["state"], "COMPUTE_WAIT")
            self.assertEqual(state["failures"], [])
            self.assertNotIn("abandonment_reason", state)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM worker_compute_reservations").fetchone()[0], 1)

    def test_empty_and_over_limit_discoveries_never_count_as_completed_research(self):
        self.names["Energy"] = []
        self.names["Defense"] = ["Too Many One", "Too Many Two", "Too Many Three"]
        result = self.run_research()
        self.assertEqual(result["state"], "COMPLETE")
        self.assertEqual(sum(state["state"] == "COMPLETE" for state in result["industries"]), 6)
        self.assertNotIn("Energy", self.plans)
        self.assertNotIn("Defense", self.plans)
        self.assertTrue(Path(self.states(result)["Defense"]["discovery_report"]).exists())

    def test_completed_cache_requires_persisted_same_day_result(self):
        first = self.run_research()
        self.assertEqual(first["state"], "COMPLETE")
        result_id = self.states(first)["AI"]["result_id"]
        with self.database:
            self.database.execute("DELETE FROM rw_results WHERE id=?", (result_id,))
        self.calls.clear()
        retry = self.run_research("retry")
        self.assertEqual(retry["state"], "COMPLETE")
        self.assertEqual(self.states(retry)["AI"]["state"], "FAILED")
        self.assertEqual(sum(state["state"] == "COMPLETE" for state in retry["industries"]), 7)
        self.assertEqual(self.calls, [])

    def test_new_la_day_discovers_again_and_does_not_reuse_old_day_results(self):
        self.assertEqual(self.run_research()["state"], "COMPLETE")
        self.clock_base += dt.timedelta(days=1)
        self.calls.clear()
        result = self.run_research("tomorrow")
        self.assertEqual(result["state"], "COMPLETE")
        self.assertEqual(len(self.calls), 16)
        self.assertEqual(result["company_usage"], 8)
        self.assertEqual(self.count("rw_plans"), 16)
        self.assertEqual(self.count("application_jobs"), 8)

    def test_unknown_scope_does_not_stop_the_other_industries_planning(self):
        violation_stored = threading.Event()

        def wait_for_violation(industry, phase):
            if industry != "AI" and phase == "discovery":
                self.assertTrue(violation_stored.wait(3))

        def violate(industry, phase, payload):
            if industry == "AI" and phase == "qualification":
                payload["scope_violation"]["unknown"] = True
            return payload

        self.worker_hook = wait_for_violation
        self.payload_hook = violate
        self.store_hook = lambda industry: violation_stored.set()
        result = self.run_research()
        # Owner, 2026-09-23: "Fix 3. That should never exist. its a systemic blocker." One
        # industry reporting an unaccountable scope used to charge the whole remaining day and
        # then raise for EVERY other industry before it planned, so a single worker's scope loss
        # failed all eight. The violation is recorded against AI; the rest still get to work.
        self.assertLess(result["company_usage"], 60,
                        "A scope violation must not consume the shared daily company allowance")
        self.assertGreater(len(set(self.plans)), 1,
                           "Other industries must still plan after another industry's scope violation")
        others = [state for state in result["industries"] if state["industry"] != "AI"]
        self.assertFalse(all(state["state"] == "FAILED" for state in result["industries"]),
                         "One industry's scope violation must not fail every industry")
        self.assertFalse(any("Unknown research scope" in (state.get("error") or "") for state in others),
                         "No industry may be refused for another industry's recorded violation")

    def test_known_scope_violation_stops_only_own_industry_and_prevents_retry(self):
        def violate(industry, phase, payload):
            if industry == "AI" and phase == "qualification":
                payload["scope_violation"]["companies"] = ["Unplanned Fixture"]
            return payload

        self.payload_hook = violate
        first = self.run_research()
        self.assertEqual(sum(state["state"] == "COMPLETE" for state in first["industries"]), 7)
        self.assertEqual(first["company_usage"], 9)
        self.calls.clear()
        retry = self.run_research("retry")
        self.assertEqual(retry["state"], "COMPLETE")
        self.assertIn("RESEARCH_SCOPE_VIOLATION", self.states(retry)["AI"]["error"])
        self.assertEqual(self.states(retry)["AI"]["failures"], self.states(first)["AI"]["failures"])
        self.assertEqual(self.calls, [])

    def test_stop_before_launch_reserves_nothing(self):
        self.stop.set()
        result = self.run_research()
        self.assertEqual(result["state"], "INTERRUPTED")
        self.assertTrue(all(state["state"] == "INTERRUPTED" for state in result["industries"]))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.count("worker_compute_reservations"), 0)
        self.assertEqual(self.count("rw_plans"), 0)

    def test_running_workers_observe_stop_and_wait_polling_is_bounded(self):
        barrier = threading.Barrier(8, action=self.stop.set)
        self.worker_hook = lambda industry, phase: barrier.wait(timeout=3)
        started = time.monotonic()
        with patch.object(parallel_research, "wait", wraps=parallel_research.wait) as waiting:
            result = self.run_research()
        self.assertEqual(result["state"], "INTERRUPTED")
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(len(self.calls), 8)
        self.assertTrue(all(call.kwargs["timeout"] <= 1 for call in waiting.call_args_list))
        self.assertEqual(self.count("rw_plans"), 0)

    def test_expired_deadline_launches_no_workers(self):
        result = self.run_research(deadline=time.monotonic() - 1)
        self.assertEqual(result["state"], "FAILED")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.count("worker_compute_reservations"), 0)

    def test_a_persistence_error_is_recorded_and_never_stops_the_other_industries(self):
        """Owner, 2026-09-23: "Fix 6. Should never exist. gracefully handle the file writing error."

        write_json runs inside every fail/abandon/defer path, so a disk-full or permission error
        while merely RECORDING one industry's outcome used to escape into the scheduling loop,
        where a bare `except BaseException` set the SIGTERM channel. That interrupted all eight
        industries and reported the run as INTERRUPTED - indistinguishable from the owner
        stopping it - for what was a local I/O fault. The failure is now noted on the state it
        could not write, and the run continues.
        """
        def broken_write(path, payload):
            if isinstance(payload, dict) and payload.get("industry") == "Energy":
                raise OSError("Synthetic persistent report failure")
            self.write_json(path, payload)

        self.runtime.write_json = broken_write
        result = self.run_research()
        self.assertFalse(self.stop.is_set(), "A file-writing error must not raise the global stop")
        self.assertNotEqual(result["state"], "INTERRUPTED",
                            "A local I/O fault must not be reported as an operator interruption")
        energy = next(state for state in result["industries"] if state["industry"] == "Energy")
        self.assertTrue(energy.get("persist_errors"), "The write failure is recorded on the industry it hit")
        self.assertEqual(energy["persist_errors"][0]["error_class"], "OSError")
        others = [state for state in result["industries"] if state["industry"] != "Energy"]
        self.assertTrue(any(state["state"] != "INTERRUPTED" for state in others),
                        "Other industries keep working through another industry's write failure")
        self.assertGreater(self.count("application_jobs"), 0,
                           "The run still produces its ordinary output despite the write failure")


if __name__ == "__main__":
    unittest.main()
