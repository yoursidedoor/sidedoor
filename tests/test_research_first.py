"""Research runs once a day, runs FIRST, and gives back what it did not use.

One shared daily compute pool of 10,800 seconds (`workflow_tasks.reserve_compute`) feeds
research and every service consumer. Only research is capped below it, at
`daily_compute_seconds - research_service_reserve_seconds`, and that reserve protects the
services only if research draws first. These tests pin the three parts of that: the
reduced ceiling research is actually admitted against, the order of the day-opening tick,
and the single boundary the research day, the compute day and the checkpoint day now share.
The second class pins the research-only refund that turns the booking into a ceiling.

Everything is offline: temp roots, a fake worker, no project-root database, no service, no
network. socket/subprocess are patched to raise.
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
CONFIG = json.loads((ROOT / "config.json").read_text())
DAILY = CONFIG.get("daily_compute_seconds", 10800)
RESERVE = CONFIG["research_service_reserve_seconds"]
CEILING = DAILY - RESERVE
# Twelve seconds into the 2026-09-18 service day, which is also twelve seconds into the
# research day: both open at research_hour (== day_start_hour; 9:00 Pacific since 2026-09-30,
# midnight before). The live defect fired at the same offset into the OLD 08:00 research day,
# eight hours after the services had started spending.
HOUR = CONFIG["research_hour"]
assert HOUR == CONFIG.get("day_start_hour", 0), "research must open the compute day it draws from"
DAY_OPEN = dt.datetime(2026, 9, 18, HOUR, 0, 12, tzinfo=PACIFIC)
RESEARCH = orchestrator.research


def pinned(instant):
    class Pinned(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    return SimpleNamespace(datetime=Pinned, timedelta=dt.timedelta, timezone=dt.timezone)


class ResearchFixture(unittest.TestCase):
    """A real `parallel_research.run` behind a fake worker, reached the real way."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for directory in ("state", "reports", "logs"):
            (self.root / directory).mkdir()
        for filename in ("discovery-prompt.md",):
            shutil.copyfile(ROOT / filename, self.root / filename)
        self.policy = user_profile.load()
        self.industries = self.policy["industries"]
        self.database = orchestrator.connect(self.root)
        self.addCleanup(self.database.close)
        research_workflow.initialize(self.database)
        workflow_tasks.initialize(self.database)
        applications.initialize(self.database)
        daily_schedule.initialize(self.database, CONFIG["timezone"])
        self.pipeline = {"companies": [], "rules": {"max_unique_contacts_per_company": 3}}
        # The phase timeout is PINNED here. These cases are about the compute ceiling, and their
        # arithmetic ("exactly eight 120-second discoveries") is stated in 120-second phases. The
        # production default moved to 600 on 2026-09-23 because a real qualification worker opens
        # a dozen pages per company and was being killed at 120; pinning keeps these tests
        # measuring the ceiling rather than the default.
        self.config = dict(CONFIG, scope="job_search_e2e", research_timeout_seconds=600,
                           research_phase_timeout_seconds=120)
        self.started = time.monotonic()
        self.lock = threading.Lock()
        self.calls = []
        self.order = []
        self.qualification_status = "COMPLETE"
        for target in ("socket.socket.connect", "socket.create_connection", "subprocess.Popen",
                       "orchestrator.HeyReach"):
            guard = patch(target, side_effect=AssertionError("External calls prohibited in these tests"))
            guard.start()
            self.addCleanup(guard.stop)
        for name, replacement in (("utcnow", self.utcnow), ("run_research_worker", self.worker),
                                  ("research_inputs", self.research_inputs),
                                  ("research_command", lambda root, config, output, schema: (schema, output))):
            seam = patch.object(orchestrator, name, replacement)
            seam.start()
            self.addCleanup(seam.stop)
        clock = patch.object(orchestrator, "dt", pinned(DAY_OPEN))
        clock.start()
        self.addCleanup(clock.stop)

    # --- fixture plumbing ---------------------------------------------------------------

    def utcnow(self):
        return (DAY_OPEN + dt.timedelta(seconds=time.monotonic() - self.started)).isoformat()

    def research_inputs(self, root, config, database, pipeline, observation, output, *,
                        companies, industry, discovery):
        plan_id = "fixture-plan:" + parallel_research._day(self.utcnow()) + ":" + industry
        if config.get("_research_scope_id"):
            plan_id += ":scope:" + config["_research_scope_id"]
        research_workflow.seed_history(database, pipeline, history_complete=True)
        plan = research_workflow.plan_research(database, plan_id, companies, now=self.utcnow(), limit=60)
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
        if phase == "discovery":
            payload = {"discovery_status": "COMPLETE", "summary": "Synthetic industry search", "blockers": [],
                       "companies": [{"company": "Fixture " + industry, "industry": industry,
                                      "source": "https://www.nasa.gov/", "observed_at": self.utcnow(),
                                      "excerpt": "Fixture " + industry + " synthetic industry evidence",
                                      "excerpt_names_company": True}]}
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

    # --- helpers ------------------------------------------------------------------------

    def spend(self, seconds, action_id="services"):
        """Spend the shared allowance the way a service consumer does: the FULL 10,800 pool."""
        with self.database:
            self.assertTrue(workflow_tasks.reserve_compute(
                self.database, action_id, self.utcnow(), seconds, CONFIG["timezone"], DAILY))
            self.order.append(action_id)

    def reservations(self):
        return [tuple(row) for row in self.database.execute(
            "SELECT action_id,day,seconds FROM worker_compute_reservations ORDER BY created_at,action_id")]

    def research(self):
        # The unpatched entry point, captured at import: a test may patch orchestrator.research.
        return RESEARCH(self.root, self.config, self.database, self.pipeline, {})


class ResearchFirstTests(ResearchFixture):
    def test_research_is_admitted_against_the_reduced_ceiling_not_the_shared_pool(self):
        """7,200, traced from config.json through orchestrator.research into the real admission."""
        self.assertEqual((DAILY, RESERVE, CEILING), (21000, 9000, 12000))
        self.spend(CEILING)                                # research's whole ceiling, by the services
        result = self.research()

        self.assertIs(result["compute_wait"], True)
        self.assertEqual(result["compute_allocation"],
                         {"daily_seconds": 21000, "research_admission_ceiling": 12000,
                          "protected_service_seconds": 9000})
        self.assertCountEqual(result["compute_waiting_industries"], self.industries)
        self.assertEqual(self.calls, [], "no worker ran: the ceiling refused the first reservation")
        self.assertEqual([action for action, _, _ in self.reservations()], ["services"])
        # 3,600 of the shared pool is still there. Research was refused at 7,200, so the cap it
        # was admitted against was neither 9,000 (the old reserve) nor the full 10,800.
        with self.database:
            self.assertTrue(workflow_tasks.reserve_compute(
                self.database, "services-reserve", self.utcnow(), RESERVE, CONFIG["timezone"], DAILY))
            self.assertFalse(workflow_tasks.reserve_compute(
                self.database, "one-second-more", self.utcnow(), 1, CONFIG["timezone"], DAILY))

    def test_the_ceiling_is_the_only_thing_that_stops_research(self):
        """6,240 spent leaves exactly eight 120-second discoveries inside the 7,200 ceiling."""
        self.spend(CEILING - 960)
        result = self.research()
        self.assertEqual(sorted(phase for _, phase in self.calls), ["discovery"] * 8)
        research_reservations = [seconds for action, _, seconds in self.reservations()
                                 if action.startswith("research:")]
        self.assertEqual(sum(research_reservations), 960, "research spent its ceiling exactly, and stopped")
        self.assertIs(result["compute_wait"], True)
        for state in result["industries"]:
            self.assertEqual(state["compute_waits"][0]["stage"], "qualification")

    def test_day_opening_tick_claims_research_before_any_service_reservation(self):
        """The whole point: on the tick that opens the compute day, research draws first."""
        order = []

        def service_cycle(root, config, database):
            with database:
                workflow_tasks.reserve_compute(database, "service-cycle-" + str(len(order)),
                                               self.utcnow(), 60, CONFIG["timezone"], DAILY)
            order.append("service_cycle")
            return {"applications": {"state": "OK"}}

        def research(root, config, database, pipeline, observation):
            order.append("research")
            return self.research()

        with patch.object(orchestrator, "service_cycle", service_cycle), \
                patch.object(orchestrator, "research", research), \
                patch.object(orchestrator, "log_retention", SimpleNamespace(print_result=lambda *args: None)):
            code = orchestrator.research_service_tick(self.root, self.config, self.database, self.pipeline)

        self.assertEqual(code, 0)
        # Owner, 2026-09-23: "separate it so that research only runs research alone and the
        # other runs the full cycle (excluding researh)." The research job used to run research
        # and then TWO service cycles, so loading it alone still submitted applications. The
        # hourly `run` job owns the services and already excludes research.
        self.assertEqual(order, ["research"], "the research job runs research and nothing else")
        actions = [action for action, _, _ in self.reservations()]
        self.assertTrue(actions[0].startswith("research:"), actions)
        self.assertFalse([action for action in actions if action.startswith("service-cycle")],
                         "the research job reserves nothing on behalf of the services")

    def test_e2_e3_an_exhausted_day_or_a_shutdown_is_not_a_new_error(self):
        """A tick that finds the day already EXHAUSTED, or a SIGTERM shutdown, exits 0; a failed run exits 1."""
        codes = {}
        for state in ("EXHAUSTED", "INTERRUPTED", "FAILED"):
            with patch.object(orchestrator, "research", lambda *args, state=state: {"state": state, "day": "d"}), \
                    patch.object(orchestrator, "log_retention", SimpleNamespace(print_result=lambda *args: None)):
                codes[state] = orchestrator.research_service_tick(self.root, self.config, self.database, self.pipeline)
        self.assertEqual(codes, {"EXHAUSTED": 0, "INTERRUPTED": 0, "FAILED": 1})

    def test_an_ordinary_tick_costs_nothing_and_the_ordering_is_a_no_op(self):
        """Once the day is recorded, research reserves nothing: the reorder is free."""
        first = self.research()
        self.assertEqual(first["state"], "COMPLETE")
        before = self.reservations()
        self.calls.clear()
        order = []

        def service_cycle(root, config, database):
            order.append("service_cycle")
            return {"applications": {"state": "OK"}}

        with patch.object(orchestrator, "service_cycle", service_cycle), \
                patch.object(orchestrator, "log_retention", SimpleNamespace(print_result=lambda *args: None)):
            orchestrator.research_service_tick(self.root, self.config, self.database, self.pipeline)

        self.assertEqual(order, [], "the research job never runs a service cycle")
        self.assertEqual(self.calls, [], "no research worker ran on an ordinary tick")
        self.assertEqual(self.reservations(), before, "not one second was reserved by the reorder")
        self.assertEqual(self.database.execute("SELECT attempts FROM research_days").fetchone()[0], 1)

    def test_research_day_compute_day_and_checkpoint_day_share_one_boundary(self):
        """The 00:00-08:00 divergence is gone: every minute of the day agrees."""
        opening = dt.datetime(2026, 9, 18, HOUR, 0, tzinfo=PACIFIC)
        budget = workflow_tasks.budget_day(opening)
        for minutes in range(0, 24 * 60, 7):
            instant = opening + dt.timedelta(minutes=minutes)
            self.assertEqual(daily_schedule.due_day(instant, CONFIG), "2026-09-18", instant)
            self.assertEqual(parallel_research._day(instant.isoformat()), "2026-09-18", instant)
            # One compute window per research day (labelled by its ending date since 2026-09-30).
            self.assertEqual(workflow_tasks.budget_day(instant), budget, instant)
        eve = opening - dt.timedelta(seconds=1)
        self.assertEqual(daily_schedule.due_day(eve, CONFIG), parallel_research._day(eve.isoformat()))
        self.assertNotEqual(workflow_tasks.budget_day(eve), budget)
        self.assertEqual(daily_schedule.next_daily_time(opening, CONFIG),
                         dt.datetime(2026, 9, 19, HOUR, 0, tzinfo=PACIFIC).isoformat())
        # The compute-wait retry instant is the same boundary, so a deferred run resumes as the
        # next research day with a fresh pool, never inside the day that just ended.
        self.assertEqual(parallel_research.next_compute_reset(opening.isoformat()),
                         daily_schedule.next_daily_time(opening, CONFIG))

    def test_a_compute_wait_defers_to_the_reset_and_consumes_no_attempt(self):
        """Patch V, with the boundary moved: still retryable, still not an investigation."""
        self.spend(CEILING)
        result = self.research()
        schedule = result["schedule"]

        self.assertIs(result["compute_wait"], True)
        self.assertTrue(result["retryable"])
        self.assertEqual(result["abandoned_industries"], [])
        self.assertEqual(result["next_retry_at"], dt.datetime(2026, 9, 19, HOUR, 0, tzinfo=PACIFIC).isoformat())
        self.assertEqual(schedule["state"], "RETRY_WAIT")
        self.assertEqual(schedule["next_retry_at"], result["next_retry_at"])
        self.assertEqual(schedule["compute_wait_attempts"], 1)
        self.assertEqual(schedule["investigation_attempts"], 0, "a wait is not a failed investigation")
        self.assertIsNone(self.database.execute("SELECT completed_at FROM research_days").fetchone()[0])
        # The retry instant opens the next research day: a fresh pool and a fresh day row, never
        # a resumed attempt inside the day whose allowance is gone.
        self.assertEqual(daily_schedule.due_day(dt.datetime.fromisoformat(schedule["next_retry_at"]), CONFIG),
                         "2026-09-19")

    def test_once_per_day_still_holds_across_the_moved_boundary(self):
        self.assertEqual(self.research()["state"], "COMPLETE")
        row = dict(self.database.execute("SELECT * FROM research_days").fetchone())
        for _ in range(3):
            self.assertEqual(self.research()["state"], "COMPLETE")
        self.assertEqual(dict(self.database.execute("SELECT * FROM research_days").fetchone()), row)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM research_days").fetchone()[0], 1)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM research").fetchone()[0], 1)


class ResearchRefundTests(ResearchFixture):
    """`workflow_tasks.refund_compute`: measured, bounded, appended, clean completions only."""

    def reservation(self, seconds=120, action_id="research:probe", at=None):
        at = at or self.utcnow()
        with self.database:
            self.assertTrue(workflow_tasks.reserve_compute(self.database, action_id, at, seconds,
                                                           CONFIG["timezone"], DAILY))
        return action_id, at

    def refunds(self):
        return [dict(row) for row in self.database.execute(
            "SELECT action_id,seconds,reserved_seconds,elapsed_seconds,reason FROM worker_compute_refunds")]

    def test_refund_is_reserved_minus_measured_elapsed(self):
        action_id, start = self.reservation(120)
        end = (dt.datetime.fromisoformat(start) + dt.timedelta(seconds=31)).isoformat()
        with self.database:
            credited = workflow_tasks.refund_compute(self.database, action_id, start, end, end, "CLEAN")
        self.assertEqual(credited, 89)
        self.assertEqual(self.refunds(), [{"action_id": action_id, "seconds": 89, "reserved_seconds": 120,
                                           "elapsed_seconds": 31, "reason": "CLEAN"}])

    def test_a_refund_never_exceeds_the_reservation_and_never_goes_negative(self):
        action_id, start = self.reservation(120)
        over = (dt.datetime.fromisoformat(start) + dt.timedelta(seconds=900)).isoformat()
        with self.database:                                   # overran its booking: nothing back
            self.assertEqual(workflow_tasks.refund_compute(self.database, action_id, start, over, over, "CLEAN"), 0)
        self.assertEqual(self.refunds(), [])
        with self.database:                                   # and a window cannot run backwards
            with self.assertRaises(ValueError):
                workflow_tasks.refund_compute(self.database, action_id, over, start, over, "CLEAN")
        with self.assertRaises(ValueError):                   # nor can a refund invent a reservation
            workflow_tasks.refund_compute(self.database, "never-reserved", start, start, start, "CLEAN")

    def test_the_original_reservation_row_is_never_touched(self):
        action_id, start = self.reservation(120)
        before = self.reservations()
        end = (dt.datetime.fromisoformat(start) + dt.timedelta(seconds=20)).isoformat()
        with self.database:
            self.assertEqual(workflow_tasks.refund_compute(self.database, action_id, start, end, end, "CLEAN"), 100)
            # Idempotent by action_id: a replay credits nothing further and rewrites nothing.
            self.assertEqual(workflow_tasks.refund_compute(self.database, action_id, start, end, end, "CLEAN"), 100)
        self.assertEqual(self.reservations(), before)
        self.assertEqual(len(self.refunds()), 1)
        self.assertEqual(self.database.execute("SELECT SUM(seconds) FROM worker_compute_reservations").fetchone()[0],
                         120, "the original charge is still recorded in full; nothing was erased")
        self.assertEqual(workflow_tasks.compute_used(self.database, workflow_tasks.budget_day(DAY_OPEN)), 20,
                         "only the measured 20 seconds still count against the day")
        with self.assertRaises(Exception):
            self.database.execute("UPDATE worker_compute_refunds SET seconds=9999")

    def test_the_daily_ceiling_cannot_be_breached_through_refunds(self):
        self.spend(DAILY - 120, "services")
        action_id, start = self.reservation(120, "research:tail")
        with self.database:
            self.assertFalse(workflow_tasks.reserve_compute(self.database, "probe", self.utcnow(), 1,
                                                            CONFIG["timezone"], DAILY))
        end = (dt.datetime.fromisoformat(start) + dt.timedelta(seconds=20)).isoformat()
        with self.database:
            self.assertEqual(workflow_tasks.refund_compute(self.database, action_id, start, end, end, "CLEAN"), 100)
        with self.database:                                   # exactly the 100 seconds back, not one more
            self.assertTrue(workflow_tasks.reserve_compute(self.database, "services-take-it", self.utcnow(), 100,
                                                           CONFIG["timezone"], DAILY))
            self.assertFalse(workflow_tasks.reserve_compute(self.database, "probe-2", self.utcnow(), 1,
                                                            CONFIG["timezone"], DAILY))
        self.assertEqual(sum(seconds for _, _, seconds in self.reservations()), DAILY + 100)

    def test_a_clean_research_run_returns_its_measured_tail(self):
        """The real path: config.json -> orchestrator.research -> parallel_research -> the ledger."""
        clean = self.research()
        self.assertEqual(clean["state"], "COMPLETE")
        booked = clean["compute_reserved_seconds"]
        refunded = clean["compute_refunded_seconds"]
        self.assertGreater(booked, 0)
        self.assertGreater(refunded, 0)
        self.assertLessEqual(refunded, booked)
        self.assertEqual(refunded, sum(row["seconds"] for row in self.refunds()))
        self.assertEqual({row["reason"] for row in self.refunds()},
                         {"RESEARCH_DISCOVERY_COMPLETE", "RESEARCH_QUALIFICATION_COMPLETE"})
        self.assertEqual(len(self.refunds()), 16, "one credit per cleanly completed phase")

    def test_a_failed_research_phase_keeps_its_whole_charge(self):
        self.qualification_status = "FAILED"
        result = self.research()
        self.assertEqual(result["state"], "FAILED")
        self.assertEqual({row["reason"] for row in self.refunds()}, {"RESEARCH_DISCOVERY_COMPLETE"},
                         "the eight failed qualifications were refunded nothing")
        self.assertEqual(len(self.refunds()), 8)

    def test_refunded_seconds_are_genuinely_available_to_a_service_reservation(self):
        self.spend(DAILY - CEILING, "services-reserve")       # the protected 3,600
        clean = self.research()
        self.assertEqual(clean["state"], "COMPLETE")
        refunded = clean["compute_refunded_seconds"]
        used = sum(seconds for _, _, seconds in self.reservations())
        with self.database:
            self.assertTrue(workflow_tasks.reserve_compute(self.database, "service-after-research",
                                                           self.utcnow(), DAILY - used + refunded,
                                                           CONFIG["timezone"], DAILY))
            self.assertFalse(workflow_tasks.reserve_compute(self.database, "one-more", self.utcnow(), 1,
                                                            CONFIG["timezone"], DAILY))


if __name__ == "__main__":
    unittest.main()
