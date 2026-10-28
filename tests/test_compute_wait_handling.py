"""A shared-compute wait is a WAIT, not a failed investigation.

AGENTS.md: "Compute waits are not failed investigations", and abandonment is the AI's
judgment reached through durable, bounded attempts — never a deterministic reaction to an
empty budget. These tests pin the three cases apart: an exhausted allowance (retry when it
resets), a genuine unresolvable research failure (AI-judged abandonment, preserved), and a
real error. They also pin that no limit is raised and no usage or charge is erased.

Everything is offline: temp roots, a fake worker, a fake coordinator, and socket/subprocess
patched to raise. No project-root database, service, retry or side effect is touched.
"""

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
from zoneinfo import ZoneInfo

import applications
import daily_schedule
import orchestrator
import parallel_research
import research_workflow
import workflow_tasks
import profile_home
import user_profile


ROOT = Path(__file__).resolve().parents[1]
PACIFIC = ZoneInfo("America/Los_Angeles")
# 08:00:12 America/Los_Angeles on 2026-09-18: the exact live rollover at which the observed
# defect fired, eight hours into a compute day the services had already spent.
LIVE_ROLLOVER = dt.datetime(2026, 9, 18, 15, 0, 12, tzinfo=dt.timezone.utc)
# The next service-day start (config day_start_hour: 9:00 Pacific since 2026-09-30, so the
# 08:00:12 refusal now waits 48 minutes; it was the next midnight).
RESET_BOUNDARY = workflow_tasks.next_day_start(LIVE_ROLLOVER).isoformat()


class ComputeWaitTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        # Compute arithmetic below is written in 120-second sessions; the session sizes themselves
        # (300 s discovery, 120 + 1.5 x slow end x companies) are tested in test_parallel_research.
        for name, value in (("DISCOVERY_SECONDS", 120), ("qualification_seconds", lambda database, companies: 120)):
            sizes = patch.object(parallel_research, name, value)
            sizes.start()
            self.addCleanup(sizes.stop)
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
        self.addCleanup(self.database.close)
        research_workflow.initialize(self.database)
        workflow_tasks.initialize(self.database)
        applications.initialize(self.database)
        daily_schedule.initialize(self.database, "America/Los_Angeles")
        self.pipeline = {"companies": [], "rules": {"max_unique_contacts_per_company": 3}}
        self.config = {"timezone": "America/Los_Angeles", "research_hour": 8,
                       "research_max_attempts": 3, "research_retry_delays_seconds": [900, 3600],
                       "research_timeout_seconds": 600, "daily_compute_seconds": 9000}
        self.started = time.monotonic()
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.calls = []
        self.decisions = []
        self.plans = {}
        self.qualification_status = "COMPLETE"
        self.worker_hook = lambda industry, phase: None
        self.decision = {"action": "ABANDON", "method": "", "instructions": "",
                         "reason": "No supported alternative public source remains for this industry",
                         "progress": False}
        self.runtime = SimpleNamespace(
            RESEARCH_STOP=self.stop, utcnow=self.utcnow, write_json=self.write_json,
            research_command=lambda root, config, output, schema: (schema, output),
            research_inputs=self.research_inputs, run_research_worker=self.worker,
            store_research=self.store_research,
            research_recovery_methods=lambda root, config: {
                "public_search": "Fixture public search", "observed_links": "Fixture link reader"},
            research_recovery_decision=self.recovery_decision)
        for target in ("socket.socket.connect", "socket.create_connection", "subprocess.Popen",
                       "orchestrator.HeyReach"):
            guard = patch(target, side_effect=AssertionError("External calls prohibited in these tests"))
            guard.start()
            self.addCleanup(guard.stop)

    # --- fixture plumbing -------------------------------------------------------------

    def utcnow(self):
        return (LIVE_ROLLOVER + dt.timedelta(seconds=time.monotonic() - self.started)).isoformat()

    def write_json(self, path, payload):
        Path(path).write_text(json.dumps(payload))

    def research_inputs(self, root, config, database, pipeline, observation, output, *, companies, industry, discovery):
        plan_id = "fixture-plan:" + parallel_research._day(self.utcnow()) + ":" + industry
        if config.get("_research_scope_id"):
            plan_id += ":scope:" + config["_research_scope_id"]
        research_workflow.seed_history(database, pipeline, history_complete=True)
        plan = research_workflow.get_plan(database, plan_id, now=self.utcnow())
        if plan:
            research_workflow.hold_unreported_contact_slots(database, plan_id)
            plan = research_workflow.get_plan(database, plan_id, now=self.utcnow())
        else:
            plan = research_workflow.plan_research(database, plan_id, companies, now=self.utcnow(), limit=60)
        self.plans[industry] = plan
        context = {"industry": industry, "companies": [entry["company"] for entry in plan["assignments"]]}
        return plan, plan_id, json.dumps(context), ("research-schema.json", output)

    def worker(self, root, command, prompt, log, timeout):
        schema, output = command
        phase = "discovery" if schema == "discovery-schema.json" else "qualification"
        context = json.loads(prompt.rsplit("\n\n", 1)[-1])
        industry = context["industries_in_priority_order"][0] if phase == "discovery" else context["industry"]
        with self.lock:
            self.calls.append((industry, phase))
        Path(log).write_text("LOCAL FAKE WORKER; NO NETWORK")
        self.worker_hook(industry, phase)
        if phase == "discovery":
            name = "Fixture " + industry
            payload = {"discovery_status": "COMPLETE", "summary": "Synthetic industry search", "blockers": [],
                       "companies": [{"company": name, "industry": industry, "source": "https://www.nasa.gov/",
                                      "observed_at": self.utcnow(), "excerpt": name + " synthetic industry evidence", "excerpt_names_company": True}]}
        elif self.qualification_status == "COMPLETE":
            payload = {"research_status": "COMPLETE", "summary": "Synthetic qualification", "blockers": [],
                       "action_blockers": [], "drafts": [], "company_findings": [],
                       "scope_violation": {"unknown": False, "companies": []},
                       "opportunities": [self.role(name, industry) for name in context["companies"]]}
        else:
            payload = {"research_status": "FAILED", "summary": "No readable source for this industry",
                       "blockers": ["NO_READABLE_PUBLIC_SOURCE"], "action_blockers": [], "drafts": [],
                       "company_findings": [], "scope_violation": {"unknown": False, "companies": []},
                       "opportunities": []}
        Path(output).write_text(json.dumps(payload))

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
        return orchestrator.store_research(database, pipeline, result)

    def recovery_decision(self, root, config, packet, timeout):
        with self.lock:
            self.decisions.append(packet)
        return dict(self.decision)

    # --- helpers ----------------------------------------------------------------------

    def hold_all_qualifications(self):
        """Make phase reservations deterministic: every qualification worker reserves and
        starts before any result is processed, so no recovery reservation can jump the queue."""
        barrier = threading.Barrier(len(self.industries))

        def hold(industry, phase):
            if phase == "qualification":
                barrier.wait(timeout=5)

        self.worker_hook = hold

    def spend(self, seconds, action_id="services-before-0800"):
        """Spend the shared daily allowance the way the 00:00-08:00 service ticks do."""
        with self.database:
            self.assertTrue(workflow_tasks.reserve_compute(
                self.database, action_id, self.utcnow(), seconds, "America/Los_Angeles", 10800))

    def research(self, attempt_id, deadline=600):
        """One coordinator run, without the daily scheduler gate."""
        return parallel_research.run(self.root, self.config, self.database, self.pipeline, {}, attempt_id,
                                     runtime=self.runtime, deadline=time.monotonic() + deadline)

    def attempt(self, deadline=600):
        claim = daily_schedule.claim(self.database, self.config, dt.datetime.fromisoformat(self.utcnow()))
        self.assertEqual(claim["state"], "CLAIMED", claim)
        result = parallel_research.run(self.root, self.config, self.database, self.pipeline, {}, claim["id"],
                                       runtime=self.runtime, deadline=time.monotonic() + deadline)
        schedule = daily_schedule.finish(self.database, self.config, claim["id"], result,
                                         dt.datetime.fromisoformat(self.utcnow()))
        result["schedule"] = schedule
        return result

    def states(self, result):
        return {state["industry"]: state for state in result["industries"]}

    def reservations(self):
        return [tuple(row) for row in self.database.execute(
            "SELECT action_id,day,seconds FROM worker_compute_reservations ORDER BY action_id")]

    def day_row(self):
        row = self.database.execute("SELECT * FROM research_days WHERE day='2026-09-18'").fetchone()
        return dict(zip(row.keys(), row))

    # --- the live defect --------------------------------------------------------------

    def test_exhausted_allowance_records_a_retryable_compute_wait_not_abandonment(self):
        """The live 2026-09-18 ~15:00Z case: budget gone at the 08:00 Pacific rollover."""
        self.spend(9000)
        result = self.attempt()

        self.assertTrue(result["retryable"])
        self.assertEqual(result["stop_reason"], "COMPUTE_WAIT_ALL_UNRESOLVED_INDUSTRIES")
        self.assertIs(result["compute_wait"], True)
        self.assertEqual(result["next_retry_at"], RESET_BOUNDARY)
        self.assertEqual(result["abandoned_industries"], [])
        self.assertCountEqual(result["compute_waiting_industries"], self.industries)

        for industry, state in self.states(result).items():
            self.assertEqual(state["state"], "COMPUTE_WAIT", industry)
            self.assertNotIn("recovery_state", state)
            self.assertNotIn("abandonment_reason", state)
            self.assertEqual(state["next_retry_at"], RESET_BOUNDARY)
            self.assertEqual(state["failures"], [], "a wait is not a failed investigation")
            self.assertEqual(len(state["compute_waits"]), 1)
            wait = state["compute_waits"][0]
            self.assertEqual(wait["reason"], "DAILY_COMPUTE_ALLOWANCE_EXHAUSTED")
            self.assertEqual(wait["resume"], "RESEARCH")
            self.assertIs(wait["retryable"], True)
            self.assertEqual(wait["next_retry_at"], RESET_BOUNDARY)

        # No research ran, and the tools-disabled coordinator was never asked to judge an
        # empty budget: abandonment stays the AI's decision about research, not about compute.
        self.assertEqual(self.calls, [])
        self.assertEqual(self.decisions, [])

    def test_pure_compute_wait_does_not_consume_the_days_attempt_budget(self):
        self.spend(9000)
        waited = self.attempt()
        self.assertEqual(waited["schedule"]["state"], "RETRY_WAIT")
        self.assertEqual(waited["schedule"]["attempts"], 1)
        self.assertEqual(waited["schedule"]["compute_wait_attempts"], 1)
        self.assertEqual(waited["schedule"]["investigation_attempts"], 0)
        self.assertEqual(waited["schedule"]["next_retry_at"], RESET_BOUNDARY)

        # The three genuine investigation attempts the day is entitled to are all still there.
        at = dt.datetime.fromisoformat(RESET_BOUNDARY)
        failed = {"state": "FAILED", "retryable": True}
        for index, expected in enumerate(("RETRY_WAIT", "RETRY_WAIT", "EXHAUSTED")):
            claim = daily_schedule.claim(self.database, self.config, at)
            self.assertEqual(claim["state"], "CLAIMED", (index, claim))
            self.assertEqual(claim["attempt"], index + 2)
            schedule = daily_schedule.finish(self.database, self.config, claim["id"], failed, at)
            self.assertEqual(schedule["state"], expected, (index, schedule))
            self.assertEqual(schedule["investigation_attempts"], index + 1)
            self.assertEqual(schedule["compute_wait_attempts"], 1)
            at += dt.timedelta(hours=2)
        self.assertEqual(self.day_row()["attempts"], 4, "every attempt stays recorded, none erased")

    def test_compute_wait_resumes_and_completes_once_the_allowance_returns(self):
        """A wait must actually be waited out, not be a politer dead end."""
        self.spend(8040)                                   # leaves exactly eight 120s reservations
        first = self.research("wait-attempt")
        self.assertIs(first["compute_wait"], True)
        self.assertEqual(sorted(phase for industry, phase in self.calls), ["discovery"] * 8)
        for state in first["industries"]:
            self.assertEqual(state["state"], "COMPUTE_WAIT")
            self.assertEqual(state["compute_waits"][0]["stage"], "qualification")

        spent_before = self.reservations()
        with self.database:                                # the recorded shared-allowance reset
            workflow_tasks.reset_compute(self.database, "fixture-daily-reset", self.utcnow(),
                                         "FIXTURE: shared allowance reset at local midnight")
        self.calls.clear()
        second = self.research("resumed-attempt")
        self.assertEqual(second["state"], "COMPLETE")
        self.assertIs(second["compute_wait"], False)
        self.assertEqual(sorted(phase for industry, phase in self.calls), ["qualification"] * 8,
                         "the completed discovery is resumed, not replayed")
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM application_jobs").fetchone()[0], 8)
        self.assertLessEqual(set(spent_before), set(self.reservations()),
                             "the reset subtracts a baseline; it never deletes a charge")
        self.assertEqual(self.database.execute("SELECT prior_seconds FROM worker_compute_resets").fetchone()[0], 9000)

    def test_deferred_recovery_resumes_the_ai_coordinator_and_keeps_the_failure(self):
        """Genuine failure, then the allowance runs out before bounded coordinator recovery."""
        self.qualification_status = "FAILED"
        self.hold_all_qualifications()
        self.spend(7080)                                   # 8 discovery + 8 qualification, none left
        first = self.research("wait-attempt")
        self.assertIs(first["compute_wait"], True)
        self.assertTrue(first["retryable"])
        self.assertEqual(first["abandoned_industries"], [])
        self.assertEqual(self.decisions, [], "the coordinator cannot be asked without compute")
        failures = {}
        for industry, state in self.states(first).items():
            self.assertEqual(state["state"], "COMPUTE_WAIT", industry)
            self.assertNotIn("abandonment_reason", state)
            self.assertEqual(len(state["failures"]), 1, "the real failure is retained, not replaced")
            self.assertIn("NO_READABLE_PUBLIC_SOURCE", state["failures"][0]["diagnostic"])
            self.assertEqual(state["compute_waits"][0]["resume"], "RECOVERY")
            self.assertEqual(state["compute_waits"][0]["stage"], "recovery")
            failures[industry] = state["failures"]

        with self.database:
            workflow_tasks.reset_compute(self.database, "fixture-daily-reset", self.utcnow(),
                                         "FIXTURE: shared allowance reset at local midnight")
        second = self.research("resumed-attempt")
        self.assertEqual(len(self.decisions), 8, "recovery is judged by the AI once compute exists")
        for industry, state in self.states(second).items():
            self.assertEqual(state["recovery_state"], "ABANDONED", industry)
            self.assertEqual(state["abandonment_reason"], self.decision["reason"])
            self.assertEqual(state["failures"][0], failures[industry][0])
        self.assertIs(second["compute_wait"], False)
        self.assertFalse(second["retryable"])
        self.assertEqual(second["stop_reason"], "ALL_UNRESOLVED_INDUSTRIES_ABANDONED")

    # --- abandonment must stay reachable and AI-judged ---------------------------------

    def test_genuine_unresolvable_failure_is_still_ai_judged_abandoned_with_reason_and_evidence(self):
        self.qualification_status = "FAILED"
        result = self.attempt()

        self.assertEqual(len(self.decisions), 8, "abandonment came from the AI, not from Python")
        self.assertFalse(result["retryable"])
        self.assertIs(result["compute_wait"], False)
        self.assertIsNone(result["next_retry_at"])
        self.assertEqual(result["stop_reason"], "ALL_UNRESOLVED_INDUSTRIES_ABANDONED")
        self.assertCountEqual(result["abandoned_industries"], self.industries)
        self.assertEqual(result["schedule"]["state"], "EXHAUSTED")
        self.assertEqual(result["schedule"]["compute_wait_attempts"], 0)
        self.assertIsNone(self.day_row()["completed_at"], "abandonment is never success")

        for industry, state in self.states(result).items():
            self.assertEqual(state["state"], "FAILED", industry)
            self.assertEqual(state["recovery_state"], "ABANDONED")
            self.assertEqual(state["abandonment_reason"], self.decision["reason"])
            self.assertEqual(state["compute_waits"], [])
            self.assertEqual(len(state["failures"]), 1)
            failure = state["failures"][0]
            self.assertIn("NO_READABLE_PUBLIC_SOURCE", failure["diagnostic"])
            self.assertEqual(failure["stage"], "qualification")
            self.assertTrue(failure["log"] and Path(failure["log"]).exists())
            decision = state["recovery_decisions"][-1]
            self.assertEqual(decision["state"], "DECIDED")
            self.assertEqual(decision["decision"]["action"], "ABANDON")
            self.assertTrue(decision["packet"]["failures"], "the AI judged actual failure evidence")

    def test_a_real_error_is_still_a_failure_and_reaches_bounded_recovery(self):
        """A non-compute error must keep its existing failure/recovery path unchanged."""
        def broken(root, config, database, pipeline, observation, output, *, companies, industry, discovery):
            raise ValueError("Synthetic planning defect")

        self.runtime.research_inputs = broken
        result = self.attempt()
        self.assertFalse(result["retryable"])
        self.assertIs(result["compute_wait"], False)
        self.assertEqual(result["stop_reason"], "ALL_UNRESOLVED_INDUSTRIES_ABANDONED")
        for industry, state in self.states(result).items():
            self.assertEqual(state["state"], "FAILED", industry)
            self.assertEqual(state["recovery_state"], "ABANDONED")
            self.assertEqual(state["failures"][0]["error_class"], "ValueError")
            self.assertEqual(state["compute_waits"], [])
        self.assertEqual(len(self.decisions), 8)

    def test_mixed_abandonment_and_compute_wait_still_charges_a_failed_attempt(self):
        """Only a PURE wait is excused; a run that abandoned something is a failed attempt."""
        self.qualification_status = "FAILED"
        self.decision = {"action": "ABANDON", "method": "", "instructions": "",
                         "reason": "No supported alternative public source remains for this industry",
                         "progress": False}
        # 8 discoveries + 8 qualifications + exactly one 30s coordinator reservation.
        self.hold_all_qualifications()
        self.spend(7050)
        result = self.attempt()
        states = self.states(result)
        abandoned = [industry for industry, state in states.items() if state["state"] == "FAILED"]
        waiting = [industry for industry, state in states.items() if state["state"] == "COMPUTE_WAIT"]
        self.assertEqual(len(abandoned), 1)
        self.assertEqual(len(waiting), 7)
        self.assertIs(result["compute_wait"], False, "a mixed run is not a pure wait")
        self.assertTrue(result["retryable"])
        self.assertEqual(result["stop_reason"], "")
        self.assertEqual(result["schedule"]["state"], "RETRY_WAIT")
        self.assertEqual(result["schedule"]["compute_wait_attempts"], 0)
        self.assertEqual(result["schedule"]["investigation_attempts"], 1)

    # --- nothing is raised, nothing is erased -----------------------------------------

    def test_a_compute_wait_raises_no_limit_and_erases_no_usage_or_charge(self):
        self.qualification_status = "FAILED"
        self.hold_all_qualifications()
        self.spend(7080)
        first = self.research("wait-attempt")
        self.assertIs(first["compute_wait"], True)
        self.worker_hook = lambda industry, phase: None
        charges = self.reservations()
        failures = {industry: state["failures"] for industry, state in self.states(first).items()}
        self.assertTrue(all(len(entries) == 1 for entries in failures.values()))
        self.assertEqual(self.database.execute(
            "SELECT SUM(seconds) FROM worker_compute_reservations").fetchone()[0], 9000,
            "every charge is still recorded in full")

        # The eight discoveries completed cleanly, so their measured unused tails were credited
        # back to the SHARED pool at the end of the run; the eight failed qualifications kept
        # their whole charge. The credit is not a research entitlement, so let the services take
        # it, exactly as they would during the day, and the allowance is spent again.
        refunded = self.database.execute("SELECT COALESCE(SUM(seconds),0) FROM worker_compute_refunds").fetchone()[0]
        self.assertEqual(self.database.execute(
            "SELECT COUNT(*) FROM worker_compute_refunds WHERE reason='RESEARCH_DISCOVERY_COMPLETE'").fetchone()[0], 8)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM worker_compute_refunds").fetchone()[0], 8)
        self.assertTrue(0 < refunded <= 960, refunded)
        self.assertEqual(workflow_tasks.compute_used(self.database, "2026-09-18"), 9000 - refunded)
        if refunded:
            self.spend(refunded, "services-take-the-refunded-tail")
        charges = self.reservations()

        second = self.research("retry-attempt")             # retry while the allowance is STILL spent
        self.assertIs(second["compute_wait"], True)

        # Every charge survives byte for byte; the retry bought no extra allowance.
        self.assertEqual(self.reservations(), charges)
        self.assertEqual(workflow_tasks.compute_used(self.database, "2026-09-18"), 9000)
        self.assertEqual(self.config["daily_compute_seconds"], 9000, "the configured limit is untouched")
        with self.database:
            self.assertFalse(workflow_tasks.reserve_compute(
                self.database, "post-wait-probe", self.utcnow(), 1, "America/Los_Angeles", 9000),
                "the daily ceiling still refuses one more second")
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM worker_compute_resets").fetchone()[0], 0,
                         "no reset was performed and no usage baseline was moved")
        for industry, state in self.states(second).items():
            self.assertEqual(state["failures"], failures[industry], "original failed attempts preserved")

    def test_migration_preserves_existing_exhausted_days_without_reopening_them(self):
        """The live 2026-09-17 / 2026-09-18 EXHAUSTED rows must survive untouched."""
        directory = self.enterContext(tempfile.TemporaryDirectory())  # a cleanup: runs after the connections the test closes (Windows)
        legacy_root = Path(directory)
        (legacy_root / "state").mkdir()
        legacy = orchestrator.connect(legacy_root)
        self.addCleanup(legacy.close)
        legacy.executescript("""
            CREATE TABLE research_days (
                day TEXT PRIMARY KEY, state TEXT NOT NULL, attempts INTEGER NOT NULL,
                attempt_id TEXT, next_retry_at TEXT, completed_at TEXT);
            CREATE TABLE schedule_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO schedule_meta VALUES ('daily_migration','1');
            INSERT INTO research_days VALUES
                ('2026-09-16','COMPLETE',1,'c0000015',NULL,'2026-09-17T05:42:30.777123+00:00'),
                ('2026-09-17','EXHAUSTED',10,'c0000001',NULL,NULL),
                ('2026-09-18','EXHAUSTED',1,'c0000008',NULL,NULL);
        """)
        legacy.commit()
        before = [tuple(row) for row in legacy.execute(
            "SELECT day,state,attempts,attempt_id,next_retry_at,completed_at FROM research_days ORDER BY day")]

        daily_schedule.initialize(legacy, "America/Los_Angeles")

        self.assertEqual(before, [tuple(row) for row in legacy.execute(
            "SELECT day,state,attempts,attempt_id,next_retry_at,completed_at FROM research_days ORDER BY day")])
        self.assertEqual([row[0] for row in legacy.execute(
            "SELECT compute_wait_attempts FROM research_days ORDER BY day")], [0, 0, 0])
        for day, expected in (("2026-09-17", "EXHAUSTED"), ("2026-09-18", "EXHAUSTED"), ("2026-09-16", "COMPLETE")):
            now = dt.datetime.fromisoformat(day + "T12:00:00").replace(tzinfo=PACIFIC)
            self.assertEqual(daily_schedule.claim(legacy, self.config, now)["state"], expected, day)
        self.assertEqual(before, [tuple(row) for row in legacy.execute(
            "SELECT day,state,attempts,attempt_id,next_retry_at,completed_at FROM research_days ORDER BY day")])


if __name__ == "__main__":
    unittest.main()
