import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import orchestrator
import parallel_research
import research_workflow
import workflow_tasks
import synthetic_resume  # noqa: F401 (synthetic designated resume; the owner's is not in the repo)
import profile_home
import user_profile


ROOT = Path(__file__).resolve().parents[1]
REAL_DATETIME = dt.datetime
NOW = "2026-09-17T16:00:00+00:00"


class FrozenDateTime(REAL_DATETIME):
    current = REAL_DATETIME.fromisoformat(NOW)

    @classmethod
    def now(cls, timezone=None):
        return cls.current.astimezone(timezone) if timezone else cls.current.replace(tzinfo=None)


def evidence(field, value):
    return {"field": field, "value": value, "source": "https://employer.example/jobs/123",
            "observed_at": NOW, "effective_at": None, "excerpt": "MOCK official supporting text",
            "verified": True, "kind": "official"}


def opportunity(company="Fixture"):
    facts = {"company": company, "role_title": "Staff Software Engineer",
             "open": True, "funding_stage": "Series B", "industry": "AI", "level": "Staff Software Engineer",
             "specialization": "backend", "country": "US", "city": "Seattle", "work_mode": "remote",
             "requirements_met": True, "authorization": "US_PERMANENT_RESIDENT_ACCEPTED",
             "hiring_geography_includes_candidate": True, "citizenship_clearance_requirements_met": True,
             "employee_count": "51–200", "official_destination": "https://employer.example/jobs/123",
             "team_need": "Node.js/Postgres services",
             "compensation": {"minimum": 200000, "maximum": 250000, "currency": "USD", "period": "annual_base"}}
    return {"company": company, "role": "Staff Software Engineer", "url": "https://employer.example/jobs/123",
            "rationale": "MOCK integration fixture", "requisition_id": "123", "people": [],
            "role_fit": {"fit": "STRONG", "reason": "MOCK Staff backend role matches the target levels"},
            "policy_fit": {"funding_stage": {"in_scope": True, "reason": "MOCK"}, "location": {"in_scope": True, "reason": "MOCK"}},
            "in_user_home_area": None,
            "evidence": [evidence(field, value) for field, value in facts.items()]}


def leave_company_unresolved(payload):
    """Owner decision D-2 (2026-09-19): an industry fails only when every assigned company is
    unresolved. A worker's PARTIAL label alone no longer fails an industry whose company was
    resolved, so failure fixtures must actually leave the company unresolved: here the
    role's `open` fact is only corroborated by a public source, which never proves it open."""
    for item in payload["opportunities"]:
        for entry in item["evidence"]:
            if entry["field"] == "open":
                entry["kind"] = "public"
    return payload


def person():
    profile = "https://www.linkedin.com/in/mock-alexandra"
    return {"recipient": "Alexandra Montgomery", "profile": profile, "employer_matches_target": True,
            "evidence": [evidence("identity", profile), evidence("employment", "Fixture"),
                         evidence("team", "API engineering"), evidence("ability_to_help", "Runs the team's technical hiring sessions"),
                         evidence("contact_type", "senior_peer")]}


def completed(item=None):
    return {"research_status": "COMPLETE", "summary": "Mock bounded research completed", "blockers": [], "inability": None,
            "action_blockers": [], "scope_violation": {"unknown": False, "companies": []},
            "opportunities": [item or opportunity()], "company_findings": [], "drafts": []}


def validate_schema_subset(value, schema, root):
    if "$ref" in schema:
        target = root
        for component in schema["$ref"].removeprefix("#/").split("/"):
            target = target[component]
        return validate_schema_subset(value, target, root)
    if "anyOf" in schema:
        for branch in schema["anyOf"]:
            try:
                validate_schema_subset(value, branch, root)
                return
            except ValueError:
                continue
        raise ValueError("No matching schema branch")
    actual = {dict: "object", list: "array", str: "string", bool: "boolean", int: "number", float: "number", type(None): "null"}.get(type(value))
    expected = schema.get("type")
    if expected and actual not in (expected if isinstance(expected, list) else [expected]):
        raise ValueError("Invalid type")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("Invalid enum")
    if actual == "object":
        if not set(schema.get("required", [])).issubset(value):
            raise ValueError("Missing required property")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            raise ValueError("Additional property")
        for key, child in value.items():
            if key in properties:
                validate_schema_subset(child, properties[key], root)
    if actual == "array":
        for child in value:
            validate_schema_subset(child, schema["items"], root)


class ResearchIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.cleanup = contextlib.ExitStack()
        self.addCleanup(self.cleanup.close)
        self.root = Path(self.cleanup.enter_context(tempfile.TemporaryDirectory())).resolve()
        for folder in ("state", "reports", "logs"):
            (self.root / folder).mkdir()
        for filename in ("research-prompt.md", "research-schema.json",
                         "discovery-prompt.md", "discovery-schema.json"):
            shutil.copyfile(ROOT / filename, self.root / filename)
        policy = user_profile.load()
        policy["industries"] = ["AI"]
        profile_home.use(self, policy)
        self.database = orchestrator.connect(self.root)
        self.addCleanup(lambda: self.database.close())
        self.config = {"timezone": "America/Los_Angeles", "research_hour": 8, "research_max_attempts": 3,
                       "research_retry_delays_seconds": [900, 3600], "research_timeout_seconds": 10,
                       "model": "MOCK_MODEL_NEVER_CALLED", "codex": "/MOCK_DO_NOT_EXECUTE/codex",
                       "research_company_candidates": ["Legacy Seed Must Not Be Used"],
                       "execution_ownership_verified": True}
        self.pipeline = {"companies": [], "rules": {"max_unique_contacts_per_company": 3}}
        self.payload = completed()
        self.contexts = []
        self.discovery_contexts = []
        self.discovered_companies = ["Fixture"]
        self.commands = []
        self.missing_output = False
        FrozenDateTime.current = REAL_DATETIME.fromisoformat(NOW)
        self.cleanup.enter_context(patch.object(orchestrator.dt, "datetime", FrozenDateTime))
        self.process = self.cleanup.enter_context(patch.object(orchestrator.subprocess, "Popen", side_effect=self.fake_process))
        self.cleanup.enter_context(patch.object(orchestrator.os, "killpg", create=True))
        self.cleanup.enter_context(patch("socket.socket.connect", side_effect=AssertionError("Network prohibited in this test")))
        self.cleanup.enter_context(patch.object(orchestrator, "HeyReach", side_effect=AssertionError("Provider prohibited in this test")))
        self.cleanup.enter_context(patch.object(orchestrator, "research_recovery_methods", create=True,
                                               return_value={"public_search": "Existing public search"}))
        self.recovery_decision = self.cleanup.enter_context(patch.object(orchestrator, "research_recovery_decision", create=True,
            return_value={"action": "ABANDON", "method": "", "instructions": "", "reason": "Mock no useful alternative", "progress": False}))

    def fake_process(self, command, **kwargs):
        self.commands.append((command, kwargs))
        test = self

        class Worker:
            returncode = 0
            pid = 987654321

            def communicate(self, prompt, timeout):
                path = Path(command[command.index("--output-last-message") + 1])
                schema = Path(command[command.index("--output-schema") + 1]).name
                if schema == "discovery-schema.json":
                    context = json.loads(prompt.rsplit("\n\n", 1)[1])
                    test.discovery_contexts.append(context)
                    path.write_text(json.dumps({
                        "discovery_status": "COMPLETE", "summary": "MOCK industry discovery", "blockers": [],
                        "companies": [{"company": company, "industry": context["industries_in_priority_order"][0],
                                       "source": "https://fixture-discovery.org/companies", "observed_at": context["now"],
                                       "excerpt": company + " is a MOCK industry company.",
                                       "excerpt_names_company": True}
                                      for company in test.discovered_companies]}))
                    return
                test.assertEqual(schema, "research-schema.json")
                test.contexts.append(json.loads(prompt.rsplit("\n\nDATA:\n", 1)[1]))
                if not test.missing_output:
                    path.write_text(test.payload if isinstance(test.payload, str) else json.dumps(test.payload))

            def poll(self):
                return 0

            def wait(self):
                return 0

        return Worker()

    def run_research(self):
        return orchestrator.research(self.root, self.config, self.database, self.pipeline, {})

    def daily(self):
        return dict(self.database.execute("SELECT * FROM research_days ORDER BY day DESC LIMIT 1").fetchone())

    def assert_failed_daily(self, state="FAILED"):
        self.assertNotEqual(self.daily()["state"], "COMPLETE")
        self.assertNotEqual(self.daily()["state"], "RUNNING")
        self.assertIsNone(self.daily()["completed_at"])
        self.assertEqual(self.database.execute("SELECT state FROM research ORDER BY started_at DESC LIMIT 1").fetchone()[0], state)

    def assert_only_discovery(self):
        self.assertEqual(self.process.call_count, 1)
        self.assertEqual(len(self.discovery_contexts), 1)
        self.assertEqual(self.contexts, [])

    def install_resume(self):
        destination = self.root / synthetic_resume.SOURCE
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(synthetic_resume.PATH, destination)
        shutil.copyfile(synthetic_resume.TEXT_PATH, self.root / synthetic_resume.TEXT_SOURCE)

    def test_prompt_read_failure_finishes_claimed_daily_attempt(self):
        original = Path.read_text

        def read(path, *args, **kwargs):
            if Path(path).name == "research-prompt.md":   # the bundled prompt (resources.py)
                raise OSError("MOCK unavailable prompt")
            return original(path, *args, **kwargs)

        with patch.object(Path, "read_text", read):
            try:
                outcome = self.run_research()
            except Exception as error:
                self.fail(f"Startup exception escaped: {type(error).__name__}; durable daily state={self.daily()['state']}")
        self.assertEqual(outcome["state"], "FAILED")
        self.assert_only_discovery()
        self.assertIn("MOCK unavailable prompt", outcome["industries"][0]["error"])
        self.assert_failed_daily()

    def test_history_initialization_failure_finishes_claimed_daily_attempt(self):
        with patch.object(research_workflow, "seed_history", side_effect=ValueError("MOCK inconsistent history")):
            try:
                outcome = self.run_research()
            except Exception as error:
                self.fail(f"Startup exception escaped: {type(error).__name__}; durable daily state={self.daily()['state']}")
        self.assertEqual(outcome["state"], "FAILED")
        self.assert_only_discovery()
        self.assertIn("MOCK inconsistent history", outcome["industries"][0]["error"])
        self.assert_failed_daily()

    def test_worker_start_failure_finishes_daily_attempt(self):
        self.process.side_effect = FileNotFoundError("MOCK executable missing")
        outcome = self.run_research()
        self.assertEqual(outcome["state"], "FAILED")
        self.assertIn("MOCK executable missing", outcome["industries"][0]["error"])
        self.assertEqual(self.process.call_count, 1)
        self.assert_failed_daily()

    def test_empty_output_never_daily_success(self):
        self.payload = ""
        self.assertEqual(self.run_research()["state"], "FAILED")
        self.assertEqual(len(self.contexts), 1)
        self.assert_failed_daily()

    def test_missing_output_never_daily_success(self):
        self.missing_output = True
        self.assertEqual(self.run_research()["state"], "FAILED")
        self.assertEqual(len(self.contexts), 1)
        self.assert_failed_daily()

    def test_invalid_json_never_daily_success(self):
        self.payload = "{not valid JSON"
        self.assertEqual(self.run_research()["state"], "FAILED")
        self.assertEqual(len(self.contexts), 1)
        self.assert_failed_daily()

    def test_empty_complete_object_never_daily_success(self):
        self.payload = {"research_status": "COMPLETE"}
        self.assertEqual(self.run_research()["state"], "FAILED")
        self.assertEqual(len(self.contexts), 1)
        self.assert_failed_daily()

    def test_schema_valid_empty_complete_never_daily_success(self):
        self.payload = dict(completed(), summary="", opportunities=[])
        self.assertEqual(self.run_research()["state"], "FAILED")
        self.assertEqual(len(self.contexts), 1)
        self.assert_failed_daily()

    def test_verified_negative_company_research_completes_without_applications(self):
        self.payload = {"research_status": "COMPLETE", "summary": "Official careers page reports no open positions.",
                        "opportunities": [], "drafts": [], "blockers": [],
                        "action_blockers": [], "scope_violation": {"unknown": False, "companies": []},
                        "company_findings": [{"company": "Fixture", "status": "NO_OPEN_ROLES",
                                              "source": "https://employer.example/careers", "observed_at": NOW,
                                              "excerpt": "There are currently no open positions.", "verified": True, "kind": "official"}]}
        outcome = self.run_research()
        self.assertEqual(outcome["state"], "COMPLETE")
        self.assertEqual(self.daily()["state"], "COMPLETE")
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM application_jobs").fetchone()[0], 0)
        stored = json.loads(self.database.execute("SELECT processed FROM rw_results").fetchone()[0])
        self.assertEqual(len(stored["verified_company_findings"]), 1)

    def located(self, city, work_mode, state=None):
        """The fixture opportunity, moved to a place, through the real research entry point."""
        item = opportunity()
        item["evidence"] = [entry for entry in item["evidence"] if entry["field"] not in {"city", "work_mode", "state"}]
        item["evidence"].append(evidence("city", city))
        item["evidence"].append(evidence("work_mode", work_mode))
        if state:
            item["evidence"].append(evidence("state", state))
        self.payload = completed(item)
        self.run_research()
        after = self.database.execute("SELECT COUNT(*) FROM rw_results").fetchone()[0]
        # A completed research day is not replayed, so a second call in one test would read
        # the first call's stored row and quietly assert nothing. One run per test.
        self.assertEqual(after, 1, "research did not actually run")
        stored = json.loads(self.database.execute("SELECT processed FROM rw_results ORDER BY rowid DESC LIMIT 1").fetchone()[0])
        return stored["opportunities"][0]

    def test_a_recorded_state_excludes_a_washington_city_outside_the_named_set(self):
        self.assertEqual(self.located("Issaquah", "hybrid", state="WA")["ranking"]["location_priority"], 99)

    def test_us_remote_is_unaffected_inside_an_excluded_state(self):
        self.assertEqual(self.located("Bellevue", "remote")["ranking"]["location_priority"], 2)

    def test_other_us_onsite_keeps_its_existing_rank(self):
        self.assertEqual(self.located("New York", "onsite")["ranking"]["location_priority"], 3)

    def test_out_of_scope_is_persisted_rejected_and_not_an_application(self):
        self.payload = completed(opportunity("Unplanned Company"))
        outcome = self.run_research()
        self.assertEqual(outcome["state"], "FAILED")
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM application_jobs").fetchone()[0], 0)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0], 0)
        record = self.database.execute("SELECT raw,processed FROM rw_results").fetchone()
        self.assertIn("Unplanned Company", record[0])
        self.assertTrue(any("OUTSIDE_RESERVED_SCOPE" in blocker for blocker in json.loads(record[1])["blockers"]))
        self.assertEqual(research_workflow.company_usage(self.database, "2026-09-17"), 2)

    def test_terminal_partial_preserves_stable_plan_and_remaining_slots_without_replay(self):
        self.payload["research_status"] = "PARTIAL"
        leave_company_unresolved(self.payload)
        first = self.run_research()
        self.assertEqual(first["state"], "COMPLETE")
        plan = self.contexts[-1]["research_plan"]
        discovery_evidence = self.contexts[-1]["discovery_evidence"]
        plan_id = plan["plan_id"]
        self.assertEqual(self.database.execute("SELECT remaining FROM rw_slots WHERE plan_id=?", (plan_id,)).fetchone()[0], 0)
        FrozenDateTime.current += dt.timedelta(minutes=16)
        self.payload = completed()
        self.assertEqual(self.run_research()["state"], "COMPLETE")
        retry_plan = research_workflow.get_plan(self.database, plan_id, now=FrozenDateTime.current.isoformat())
        self.assertEqual(len(self.contexts), 1)
        self.assertEqual(self.contexts[-1]["discovery_evidence"], discovery_evidence)
        self.assertEqual(retry_plan["plan_id"], plan_id)
        self.assertEqual(len(self.database.execute("SELECT * FROM rw_plans").fetchall()), 1)
        self.assertEqual(retry_plan["assignments"][0]["contact_slots"], 0,
                         "Retry restored original payload instead of persisted remaining slots")
        self.assertEqual(self.daily()["attempts"], 1)

    def test_research_inputs_preserves_only_assigned_discovery_records(self):
        own = {"company": "Fixture", "industry": "AI", "source": "https://fixture-discovery.org/companies",
               "observed_at": "2026-09-17T08:30:00-07:00", "excerpt": "Fixture builds procurement software.",
               "excerpt_names_company": True}
        shared = dict(own, company="Shared Fixture", excerpt="Shared Fixture belongs to another plan.")
        discovery = {"discovery_status": "COMPLETE", "summary": "Fixture and Shared Fixture discovery",
                     "companies": [own, shared], "blockers": []}
        original = json.dumps(discovery)
        arguments = (self.root, self.config, self.database, self.pipeline, {}, self.root / "unused.json")
        plan, plan_id, prompt, command = orchestrator.research_inputs(
            *arguments, companies=["Fixture"], industry="AI", discovery=discovery)
        context = json.loads(prompt.rsplit("\n\nDATA:\n", 1)[1])
        self.assertEqual(context["discovery_evidence"], [own])
        self.assertEqual(context["source_observations"], [])
        self.assertNotIn("Shared Fixture", json.dumps(context))
        self.assertEqual(json.dumps(discovery), original)
        legacy = orchestrator.research_inputs(*arguments, companies=["Fixture"], industry="AI")
        self.assertEqual(json.loads(legacy[2].rsplit("\n\nDATA:\n", 1)[1])["discovery_evidence"], [])
        self.process.assert_not_called()

    def test_research_inputs_rejects_foreign_industry_discovery_before_reservations(self):
        own = {"company": "Fixture", "industry": "AI", "source": "https://fixture-discovery.org/companies",
               "observed_at": NOW, "excerpt": "Fixture builds procurement software.", "excerpt_names_company": True}
        foreign = dict(own, company="Energy Fixture", industry="Energy", excerpt="Energy Fixture builds batteries.")
        discovery = {"discovery_status": "COMPLETE", "summary": "Mixed industry evidence",
                     "companies": [own, foreign], "blockers": []}
        changes = self.database.total_changes
        with self.assertRaisesRegex(ValueError, "target industry"):
            orchestrator.research_inputs(self.root, self.config, self.database, self.pipeline, {},
                                         self.root / "unused.json", companies=["Fixture"], industry="AI",
                                         discovery=discovery)
        self.assertEqual(self.database.total_changes, changes)
        self.process.assert_not_called()

    def test_research_inputs_rejects_missing_or_future_dated_discovery_evidence(self):
        record = {"company": "Other Fixture", "industry": "AI", "source": "https://fixture-discovery.org/companies",
                  "observed_at": NOW, "excerpt": "Other Fixture builds procurement software.",
                  "excerpt_names_company": True}
        discovery = {"discovery_status": "COMPLETE", "summary": "Different assigned identity",
                     "companies": [record], "blockers": []}
        arguments = (self.root, self.config, self.database, self.pipeline, {}, self.root / "unused.json")
        with self.assertRaisesRegex(ValueError, "assignment lacks discovery evidence"):
            orchestrator.research_inputs(*arguments, companies=["Fixture"], industry="AI", discovery=discovery)
        record.update(company="Fixture", excerpt="Fixture builds procurement software.",
                      observed_at="2026-09-15T16:00:00+00:00")
        plan, plan_id, prompt, command = orchestrator.research_inputs(
            *arguments, companies=["Fixture"], industry="AI", discovery=discovery)
        self.assertEqual(json.loads(prompt.rsplit("\n\nDATA:\n", 1)[1])["discovery_evidence"], [record],
                         "A page read days ago is still a real observation and must be reused")
        record["observed_at"] = "2026-09-30T16:00:00+00:00"
        with self.assertRaisesRegex(ValueError, "future-dated"):
            orchestrator.research_inputs(*arguments, companies=["Fixture"], industry="AI", discovery=discovery)
        self.process.assert_not_called()

    def test_catchup_reserves_actual_la_day_not_yesterdays_due_day(self):
        FrozenDateTime.current = REAL_DATETIME.fromisoformat("2026-09-18T07:05:00+00:00")
        self.payload["research_status"] = "PARTIAL"
        self.run_research()
        self.assertEqual(self.daily()["day"], "2026-09-17")
        plan = self.contexts[-1]["research_plan"]
        # Since 2026-09-30 the plan and company budget follow the service day, which opens at
        # day_start_hour: at 00:05 Pacific with a 9:00 start that is still 2026-09-17's day.
        expected = workflow_tasks.service_date("2026-09-18T07:05:00+00:00")
        self.assertEqual(plan["day"], expected)
        self.assertEqual(research_workflow.company_usage(self.database, expected), 1)

    def test_exhausted_actual_day_does_not_spawn_worker(self):
        research_workflow.initialize(self.database)
        research_workflow.plan_research(self.database, "other-worker", [f"Used {index}" for index in range(60)], now=NOW, limit=60)
        self.assertEqual(self.run_research()["state"], "FAILED")
        self.process.assert_not_called()
        self.assert_failed_daily()

    def test_cross_restart_drafts_contacts_and_applications_do_not_duplicate(self):
        self.payload["research_status"] = "PARTIAL"
        leave_company_unresolved(self.payload)
        self.payload["opportunities"][0]["people"] = [person()]
        research_workflow.initialize(self.database)
        research_workflow.register_claims(self.database, [{"text": "Built backend services", "tags": ["backend"]}],
                                          designated_source_text="Built backend services", source_sha256="a" * 64, designated_sha256="a" * 64)
        self.assertEqual(self.run_research()["state"], "COMPLETE")
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM drafts").fetchone()[0], 1)
        self.database.close()
        self.database = orchestrator.connect(self.root)
        FrozenDateTime.current += dt.timedelta(minutes=16)
        self.payload["summary"] = "New retry observation, same person"
        self.run_research()
        for table in ("drafts", "rw_contacts", "rw_drafts", "application_jobs"):
            with self.subTest(table=table):
                self.assertEqual(self.database.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 1)

    def test_real_resume_proof_reaches_worker_context(self):
        self.install_resume()
        self.assertEqual(self.run_research()["state"], "COMPLETE")
        context = self.contexts[-1]
        self.assertEqual(context["resume_proof"]["claims"], 3)
        self.assertEqual(len(context["verified_candidate_claims"]), 3)
        self.assertNotIn("strategy", context)  # learning's strategy left with learning (2026-10-22)
        self.assertFalse(context["research_plan"]["tool_scope_enforced"])
        command, unused = self.commands[-1]
        self.assertIn("--ignore-user-config", command)
        self.assertIn("features.apps=false", command)

    def test_paid_api_environment_is_not_inherited_by_worker(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "MOCK_ONLY_NEVER_CALL", "CODEX_API_KEY": "MOCK_ONLY_NEVER_CALL",
                                         user_profile.HOME_VARIABLE: os.environ[user_profile.HOME_VARIABLE]}, clear=True):
            self.run_research()
            command, kwargs = self.commands[-1]
            effective_environment = kwargs.get("env", os.environ)
            self.assertFalse(effective_environment.get("OPENAI_API_KEY"), "Worker inherits potential API billing credentials")
            self.assertFalse(effective_environment.get("CODEX_API_KEY"), "Worker inherits potential API billing credentials")
        self.assertEqual(command[0], "/MOCK_DO_NOT_EXECUTE/codex")

    def test_default_compute_ceiling_is_three_hours_and_exhaustion_blocks_launch(self):
        orchestrator.workflow_tasks.initialize(self.database)
        self.assertTrue(orchestrator.workflow_tasks.reserve_compute(self.database, "other-worker", NOW, 10800))
        self.assertEqual(self.run_research()["state"], "PARTIAL")  # a pure compute wait is PARTIAL (owner, 2026-09-25)
        self.process.assert_not_called()
        self.assert_failed_daily("PARTIAL")

    def test_reserved_plan_is_usable_without_fabricating_network_enforcement(self):
        outcome = self.run_research()
        self.assertEqual(outcome["state"], "COMPLETE")
        self.assertEqual(self.process.call_count, 2)
        plan = self.contexts[-1]["research_plan"]
        self.assertTrue(plan["scope_enforced"])
        self.assertIs(plan["tool_scope_enforced"], False)
        self.assertEqual([item["company"] for item in plan["assignments"]], ["Fixture"])
        self.assertEqual(self.discovery_contexts[0]["industries_in_priority_order"], ["AI"])
        self.assertNotIn("Legacy Seed Must Not Be Used", json.dumps(self.discovery_contexts))
        self.assertEqual(self.contexts[-1]["industry"], "AI")
        self.assertEqual(research_workflow.company_usage(self.database, "2026-09-17"), 1)
        self.assertLessEqual(plan["assignments"][0]["contact_slots"], 3)
        persisted = json.loads(self.database.execute("SELECT payload FROM rw_plans").fetchone()[0])
        self.assertIs(persisted["tool_scope_enforced"], False)
        self.assertIn("web_search=\"live\"", self.commands[-1][0])

    def test_missing_plan_blocks_worker_and_finishes_attempt(self):
        with patch.object(research_workflow, "plan_research", return_value=None):
            self.assertEqual(self.run_research()["state"], "FAILED")
        self.assert_only_discovery()
        self.assert_failed_daily()

    def test_empty_discovery_blocks_qualification_worker(self):
        self.discovered_companies = []
        self.assertEqual(self.run_research()["state"], "FAILED")
        self.assert_only_discovery()
        self.assert_failed_daily()
        self.assertEqual(research_workflow.company_usage(self.database, "2026-09-17"), 0)

    def test_expired_committed_plan_is_not_silently_refreshed(self):
        research_workflow.initialize(self.database)
        plan_id = "industry-research-plan:2026-09-17:" + hashlib.sha256(b"AI").hexdigest()[:12]
        plan_id += ":scope:" + parallel_research.scope_id(user_profile.load())
        research_workflow.plan_research(self.database, plan_id, ["Fixture"], now="2026-09-16T16:00:00+00:00")
        before = tuple(self.database.execute("SELECT * FROM rw_plans WHERE id=?", (plan_id,)).fetchone())
        self.assertEqual(self.run_research()["state"], "FAILED")
        self.assert_only_discovery()
        self.assert_failed_daily()
        self.assertEqual(before, tuple(self.database.execute("SELECT * FROM rw_plans WHERE id=?", (plan_id,)).fetchone()))
        self.assertEqual(research_workflow.company_usage(self.database, "2026-09-17"), 0)

    def test_abandoned_partial_is_immediately_exhausted_after_prompt_fix_and_restart(self):
        self.payload = dict(completed(), research_status="PARTIAL", opportunities=[],
                            summary="MOCK reproduction: worker did not perform research.",
                            blockers=["MOCK prior tool-scope interpretation"])
        times = ("2026-09-17T15:05:00+00:00", "2026-09-17T15:20:00+00:00", "2026-09-17T16:21:00+00:00")
        for index, instant in enumerate(times, start=1):
            FrozenDateTime.current = REAL_DATETIME.fromisoformat(instant)
            outcome = self.run_research()
            self.assertEqual(outcome["state"], "FAILED" if index == 1 else "EXHAUSTED")
            self.assertEqual(self.daily()["attempts"], 1)
            self.assertEqual(self.daily()["state"], "EXHAUSTED")
            self.assertIsNone(self.daily()["completed_at"])
        self.assertEqual(self.process.call_count, 2)
        self.assertEqual(len(self.discovery_contexts), 1)
        self.assertEqual(len(self.contexts), 1)
        self.assertEqual(self.recovery_decision.call_count, 1)
        tables = ("research_days", "research", "rw_plans", "rw_companies", "rw_slots", "rw_results", "worker_compute_reservations")
        before = {table: [tuple(row) for row in self.database.execute("SELECT * FROM " + table)] for table in tables}
        self.database.close()
        self.database = orchestrator.connect(self.root)
        self.payload = completed()
        self.process.reset_mock()
        for instant in ("2026-09-17T22:00:00+00:00", "2026-09-18T06:59:00+00:00", "2026-09-18T07:05:00+00:00"):
            FrozenDateTime.current = REAL_DATETIME.fromisoformat(instant)
            self.assertEqual(self.run_research()["state"], "EXHAUSTED")
            self.assertEqual(self.daily()["attempts"], 1)
            self.assertIsNone(self.daily()["completed_at"])
        self.process.assert_not_called()
        after = {table: [tuple(row) for row in self.database.execute("SELECT * FROM " + table)] for table in tables}
        self.assertEqual(before, after, "Prompt/code changes must not reset an exhausted day or replace its evidence")


class ResearchPromptScopeTests(unittest.TestCase):
    def test_prompt_authorizes_reserved_research_and_discloses_soft_tool_boundary(self):
        prompt = " ".join((ROOT / "research-prompt.md").read_text().split())
        self.assertNotIn("Main must gate unsupported tool scope", prompt,
                         "This blanket gate caused real workers to decline valid reserved research")
        marker = prompt.index("tool_scope_enforced=false")
        explanation = prompt[max(0, marker - 700):marker + 1400].lower()
        self.assertRegex(explanation, r"(?:authorized|permitted|proceed|may use|allowed)",
                         "Explain that a committed named plan permits bounded public research")
        self.assertRegex(explanation, r"(?:not a blocker|does not block|not a reason|does not prohibit|not a blanket|not.*prohibit)",
                         "False tool scope must not be interpreted as blanket read denial")
        self.assertIn("60 unique companies", prompt)
        self.assertIn("at most 3 new contacts per company per research run", prompt)
        self.assertRegex(prompt, r"If absent, expired,? (?:or )?empty(?: or spanning multiple industries)?, do NO research")
        self.assertIn("ONE assigned industry", prompt)
        self.assertIn("history/reconciliation inputs, never discovery seeds", prompt)
        self.assertIn("RESEARCH_PLAN_REQUIRED", prompt)
        self.assertIn("unplanned", prompt.lower())


class ResearchSchemaTests(unittest.TestCase):
    def test_strict_closed_object_shape_and_nullable_evidence(self):
        schema = json.loads((ROOT / "research-schema.json").read_text())

        def inspect(node):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertIs(node.get("additionalProperties"), False)
                    # No carve-outs: strict structured output rejects a declared property
                    # that is not required. `inability` was carved out here and production
                    # 400ed on it; it is now required and nullable instead.
                    self.assertEqual(set(node.get("required", [])), set(node.get("properties", {})))
                for value in node.values():
                    inspect(value)
            elif isinstance(node, list):
                for child in node:
                    inspect(child)

        inspect(schema)
        payload = completed()
        payload["opportunities"][0]["evidence"].append(evidence("unknown_fact", None))
        payload["company_findings"] = [{"company": "Other", "status": "UNRESOLVED", "source": "",
                                        "observed_at": NOW, "excerpt": "", "verified": False, "kind": "unknown"}]
        validate_schema_subset(payload, schema, schema)
        payload["opportunities"][0]["evidence"][-1]["verified"] = None
        with self.assertRaises(ValueError):
            validate_schema_subset(payload, schema, schema)

    def test_schema_rejects_injected_properties_and_incomplete_objects(self):
        schema = json.loads((ROOT / "research-schema.json").read_text())
        for payload in ({"research_status": "COMPLETE"}, dict(completed(), paid_model_authorized=True)):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    validate_schema_subset(payload, schema, schema)

    def test_action_blockers_and_scope_violation_are_required_and_typed(self):
        schema = json.loads((ROOT / "research-schema.json").read_text())
        payload = completed()
        payload["action_blockers"] = ["Fixture: fresh history needed"]
        payload["scope_violation"] = {"unknown": True, "companies": ["Unplanned"]}
        validate_schema_subset(payload, schema, schema)
        for field in ("action_blockers", "scope_violation"):
            with self.subTest(missing=field):
                missing = completed()
                missing.pop(field)
                with self.assertRaises(ValueError):
                    validate_schema_subset(missing, schema, schema)
        invalid = [("action_blockers", "Unstructured hold"), ("action_blockers", [False]),
                   ("scope_violation", None), ("scope_violation", {"unknown": False}),
                   ("scope_violation", {"unknown": "false", "companies": []}),
                   ("scope_violation", {"unknown": False, "companies": [1]}),
                   ("scope_violation", {"unknown": False, "companies": [], "authorized": True})]
        for field, value in invalid:
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    validate_schema_subset(dict(completed(), **{field: value}), schema, schema)


def _excluded_onsite_case(city, mode):
    def test(self):
        self.assertEqual(self.located(city, mode)["ranking"]["location_priority"], 99, city)
    test.__doc__ = ("Onsite/hybrid in " + city + " is excluded. Bellevue, Redmond, Kirkland, Tacoma and "
                    "Everett all ranked as ordinary US onsite roles while the exclusion was the literal "
                    "set {\"seattle\", \"san francisco\"}.")
    return test


for _city, _mode in [("Seattle", "onsite"), ("Bellevue", "onsite"), ("Redmond", "hybrid"),
                     ("Kirkland", "onsite"), ("Tacoma", "hybrid"), ("Everett", "onsite"),
                     ("San Francisco", "onsite")]:
    setattr(ResearchIntegrationTests,
            "test_onsite_in_" + _city.replace(" ", "_").lower() + "_is_deprioritized",
            _excluded_onsite_case(_city, _mode))

