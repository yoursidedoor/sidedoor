import copy
import datetime as dt
import json
from pathlib import Path
import os
import unittest

import manual_checkpoint_scope as manual
import manual_research_recovery
import parallel_research
import profile_home
import user_profile


class CheckpointScopeTests(unittest.TestCase):
    def test_manual_scenarios(self):
        # legacy_audit makes a file unreadable with chmod(0), which Windows ignores (read-only only).
        for mode in ("fresh_recovery", "shared_owner", "application_only", "new_scope", "deadline") + (
                () if os.name == "nt" else ("legacy_audit",)):
            with self.subTest(mode=mode):
                self.assertEqual("PASS", manual.run_scenario(mode)["state"])

    def test_checkpoint_loss_does_not_reset_scope_or_repeat_reads(self):
        for mode in ("missing", "corrupt", "rolled_back", "deadline_missing"):
            with self.subTest(mode=mode):
                self.assertEqual("PASS", manual_research_recovery.checkpoint_loss_scenario(mode)["state"])

    def live_policy(self):
        return user_profile.load()

    def test_research_constraints_and_unknown_settings_remain_in_fingerprint(self):
        policy = self.live_policy()
        original = copy.deepcopy(policy)
        first = parallel_research.scope_id(policy)
        variants = [dict(policy, funding_stages=["Seed"]), dict(policy, minimum_base_salary_usd=190001),
                    dict(policy, desired_base_salary_usd=[210000, 310000]),
                    dict(policy, industries=["Energy"]), dict(policy, levels=["Staff Software Engineer"]),
                    dict(policy, location_priority=["US remote"]),
                    dict(policy, unknown_future_constraint=True),
                    dict(policy, research_company_cap=4)]
        for field in ("path", "sha256"):
            changed = copy.deepcopy(policy)
            changed["resume_raw"][field] = "synthetic-" + field   # research reads the raw résumé (E4.T2)
            variants.append(changed)
        for answer, value in (("us_work_authorized", False), ("current_country", "Canada"),
                              ("current_state", "Washington"),
                              # research-prompt.md judges in_user_home_area from these two
                              ("current_location", "Seattle, WA, USA"), ("current_city", "Seattle")):
            changed = copy.deepcopy(policy)
            changed["answers"][answer]["value"] = value
            variants.append(changed)
        for variant in variants:
            self.assertNotEqual(first, parallel_research.scope_id(variant))
        self.assertEqual(original, policy)

    def test_application_only_policy_edits_keep_the_research_scope_stable(self):
        policy = self.live_policy()
        first = parallel_research.scope_id(policy)
        stable = []
        changed = copy.deepcopy(policy)
        changed["answers"]["current_location"]["source"] = "Reworded provenance, fieldwise reconciliation 2026-09-18"
        stable.append(("answer provenance prose", changed))
        changed = copy.deepcopy(policy)
        changed["source"] = "handoff/resolved-agent-config.yaml; fieldwise reconciliation 2026-09-18"
        stable.append(("top-level provenance prose", changed))
        changed = copy.deepcopy(policy)
        changed["answers"]["veteran_status"] = {"value": "Decline to self-identify",
                                                "semantic": "veteran_self_identification",
                                                "source": "Explicit user instruction"}
        stable.append(("new application-only answer key", changed))
        changed = copy.deepcopy(policy)
        for field, value in (("email", "other@example.org"), ("full_name", "A. Example"),
                             ("phone", "+1-202-555-0100"), ("linkedin", "https://www.linkedin.com/in/other/")):
            changed["answers"][field]["value"] = value
        stable.append(("resume identity answers", changed))
        changed = copy.deepcopy(policy)
        changed["referrals_block_applications"] = not policy["referrals_block_applications"]
        changed["preapproved_applications"] = not policy["preapproved_applications"]
        changed["max_attempts"] += 1
        changed["max_reconcile_attempts"] += 1
        changed["retry_delays_seconds"] = [1, 2, 3]
        stable.append(("application attempt and referral settings", changed))
        for label, variant in stable:
            with self.subTest(edit=label):
                self.assertEqual(first, parallel_research.scope_id(variant))

    def test_answer_fingerprint_is_an_allowlist_of_research_semantics(self):
        policy = {"industries": ["AI"],
                  "answers": {"email": {"semantic": "contact_email", "value": "before@example.org"},
                              "us_work_authorized": {"semantic": "currently_authorized_to_work_in_us", "value": True}},
                  "referrals_block_applications": True, "max_attempts": 3}
        changed = copy.deepcopy(policy)
        changed["answers"]["email"]["value"] = "after@example.org"
        changed["answers"]["email"]["semantic"] = "unknown_application_answer"
        changed["referrals_block_applications"] = False
        changed["max_attempts"] = 2
        self.assertEqual(parallel_research.scope_id(policy), parallel_research.scope_id(changed))
        changed["answers"]["us_work_authorized"]["value"] = False
        self.assertNotEqual(parallel_research.scope_id(policy), parallel_research.scope_id(changed))
        self.assertEqual({"currently_authorized_to_work_in_us": [True]},
                         parallel_research.research_policy(policy)["answers"])
        self.assertNotIn("answers", parallel_research.research_policy(policy)["unclassified"])

    @unittest.skipIf(os.name == "nt", "chmod(0) does not make a file unreadable on Windows")
    def test_unreadable_legacy_audit_file_does_not_abandon_a_fresh_industry(self):
        fixture, prior, preserved, unused_decisions = manual.setup()
        blocked = next(path for path in prior if parallel_research._label("FinTech and Finance") in path.name)
        try:
            blocked.chmod(0)
            result = fixture.run_research("unreadable-legacy-audit")
            state = fixture.states(result)["FinTech and Finance"]
            self.assertEqual("COMPLETE", state["state"])
            self.assertNotIn("recovery_state", state)
            self.assertEqual("COMPLETE", result["state"])
            audit = json.loads(fixture.cache_path("FinTech and Finance").read_text())["prior_checkpoints"]
            record = next(entry for entry in audit if entry.get("path") == str(blocked))
            self.assertEqual("PRIOR_CHECKPOINT_UNREADABLE", record["diagnostic"])
            self.assertEqual("PermissionError", record["error_type"])
            blocked.chmod(0o600)
            manual.verify_preserved(fixture, prior, preserved)
        finally:
            blocked.chmod(0o600)
            fixture.doCleanups()

    def test_prior_day_failure_context_reaches_the_current_day_audit(self):
        fixture, prior, preserved, unused_decisions = manual.setup()
        try:
            older = fixture.root / "reports" / ("parallel-industry-2026-09-16-"
                                                + parallel_research._label("Energy") + ".json")
            older.write_text(json.dumps({"day": "2026-09-16", "industry": "Energy",
                                         "result": {"state": "FAILED", "summary": "PRIOR DAY SOURCE OUTAGE"}}))
            self.assertEqual("COMPLETE", fixture.run_research("prior-day-context")["state"])
            audit = json.loads(fixture.cache_path("Energy").read_text())["prior_checkpoints"]
            excerpts = json.dumps(audit)
            self.assertIn("PRIOR DAY SOURCE OUTAGE", excerpts)
            self.assertLessEqual(len(audit), parallel_research.PRIOR_CHECKPOINT_FILES)
            self.assertTrue(all(entry["diagnostic"]["saved_result_excerpt"] is not None
                                and len(entry["diagnostic"]["saved_result_excerpt"]) <= 8000
                                for entry in audit if isinstance(entry.get("diagnostic"), dict)))
            manual.verify_preserved(fixture, prior, preserved)
        finally:
            fixture.doCleanups()

    def test_recovery_free_text_never_reaches_a_discovery_prompt(self):
        fixture, prior, preserved, decisions = manual.setup()
        try:
            failed = []

            def worker_hook(industry, phase):
                if industry == "AI" and phase == "discovery" and not failed:
                    failed.append(industry)
                    raise RuntimeError("Synthetic discovery source outage")

            def decide(root, config, packet, timeout):
                decisions.append(packet)
                return {"action": "RETRY", "method": "public_search",
                        "instructions": "Legacy-only AI was previously researched; start from that company name",
                        "reason": "Synthetic AI: another public source remains supported", "progress": False}

            fixture.worker_hook = worker_hook
            fixture.runtime.research_recovery_decision = decide
            result = fixture.run_research("directive-scrub")
            self.assertEqual("COMPLETE", result["state"])
            self.assertEqual(1, len(decisions))
            self.assertEqual(2, fixture.calls.count(("AI", "discovery")))
            retried = [prompt for industry, phase, prompt in fixture.prompts
                       if industry == "AI" and phase == "discovery"][-1]
            self.assertIn("ORCHESTRATOR RECOVERY DIRECTIVE", retried)
            self.assertIn("public_search", retried)
            self.assertNotIn("Legacy-only", retried)
            self.assertNotIn("start from that company name", retried)
            self.assertTrue(all("Legacy-only" not in prompt for industry, phase, prompt in fixture.prompts
                                if phase == "discovery"))
            manual.verify_preserved(fixture, prior, preserved)
        finally:
            fixture.doCleanups()

    def test_same_scope_saved_failure_supplies_actual_diagnostic_to_ai(self):
        fixture, prior, preserved, decisions = manual.setup()
        try:
            self.assertEqual("COMPLETE", fixture.run_research("initial")["state"])
            path = fixture.cache_path("AI")
            cache = json.loads(path.read_text())
            cache["result"].update(state="FAILED", summary="SAVED DOMAIN IDENTITY CONFLICT",
                                   blockers=["Cannot verify exact official employer"])
            path.write_text(json.dumps(cache))
            deadline = cache["deadline_at"]

            def abandon(root, config, packet, timeout):
                decisions.append(packet)
                return {"action": "ABANDON", "method": "", "instructions": "", "progress": False,
                        "reason": "Synthetic AI: conflicting company identity has no supported resolution"}

            fixture.runtime.research_recovery_decision = abandon
            fixture.calls.clear()
            result = fixture.run_research("resume-failed")
            self.assertEqual("COMPLETE", result["state"])
            self.assertEqual(1, len(decisions))
            self.assertIn("SAVED DOMAIN IDENTITY CONFLICT", decisions[0]["failure"]["prior_result"]["saved_result_excerpt"])
            self.assertEqual(deadline, json.loads(path.read_text())["deadline_at"])
            self.assertEqual([], fixture.calls)
            manual.verify_preserved(fixture, prior, preserved)
        finally:
            fixture.doCleanups()

    def test_diagnostics_are_bounded_and_do_not_embed_previous_packets(self):
        diagnostic = parallel_research._diagnostic({"state": "FAILED", "summary": "long" * 10000,
                                                    "recovery_decisions": [{"packet": "NOT_A_NEW_DIAGNOSTIC"}]})
        self.assertLessEqual(len(diagnostic["saved_result_excerpt"]), 8000)
        self.assertTrue(diagnostic["truncated"])
        self.assertNotIn("NOT_A_NEW_DIAGNOSTIC", diagnostic["saved_result_excerpt"])

    def test_same_scope_deadline_missing_without_compute_still_fails_closed(self):
        fixture, prior, preserved, unused_decisions = manual.setup()
        try:
            scope = parallel_research.scope_id(fixture.policy)
            path = fixture.cache_path("AI")
            content = json.dumps({"day": "2026-09-17", "industry": "AI", "scope_id": scope,
                                  "research_policy": parallel_research.research_policy(fixture.policy), "result": {}})
            path.write_text(content)
            result = fixture.run_research("missing-deadline")
            self.assertEqual("CHECKPOINT_RECONCILIATION_REQUIRED", fixture.states(result)["AI"]["abandonment_reason"])
            self.assertEqual(content, path.read_text())
            self.assertFalse(any(industry == "AI" for industry, phase in fixture.calls))
            manual.verify_preserved(fixture, prior, preserved)
        finally:
            fixture.doCleanups()

    def test_checkpoint_scope_binding_mismatch_does_not_launch_its_workers(self):
        fixture, prior, preserved, unused_decisions = manual.setup()
        try:
            scope = parallel_research.scope_id(fixture.policy)
            path = fixture.cache_path("AI")
            content = json.dumps({"day": "2026-09-17", "industry": "AI", "scope_id": scope,
                                  "research_policy": {}, "result": {},
                                  "deadline_at": (fixture.clock_base + dt.timedelta(seconds=30)).isoformat()})
            path.write_text(content)
            result = fixture.run_research("scope-tampered")
            self.assertEqual("CHECKPOINT_RECONCILIATION_REQUIRED", fixture.states(result)["AI"]["abandonment_reason"])
            self.assertEqual(content, path.read_text())
            self.assertFalse(any(industry == "AI" for industry, phase in fixture.calls))
            manual.verify_preserved(fixture, prior, preserved)
        finally:
            fixture.doCleanups()


if __name__ == "__main__":
    unittest.main()
