import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import unittest

import manual_unknown_inspection_recovery as rehearsal


class UnknownInspectionRecoveryTests(unittest.TestCase):
    def test_manually_validated_scenarios(self):
        for name in rehearsal.SCENARIOS:
            with self.subTest(scenario=name):
                result = rehearsal.run([name])
                self.assertEqual(result["passed"], [name])
                self.assertEqual(result["external_effects"], 0)

    def test_incomplete_inspection_is_reread_before_recovery(self):
        """Headline: no longer one inspection followed by an irreversible ABANDONED."""
        result = rehearsal.run(["instrumented"])
        self.assertEqual(result["passed"], ["instrumented"])
        self.assertEqual(result["measurements"]["instrumented"], {
            "inspect_requests": 3, "investigate_claims": 3, "inspection_reread_scheduled": 2,
            "recovery_started": 1, "job_attempts": 0, "abandoned": 0, "claims_exhausted_holds": 1,
            "final_state": "BLOCKED", "final_blocker": "APPLICATION_BROWSER_INVESTIGATION_EXHAUSTED"})

    def test_empty_resolution_is_rejected(self):
        result = rehearsal.run(["empty_resolution"])
        self.assertEqual(result["passed"], ["empty_resolution"])

    def test_verified_destination_resolution_advances_the_job(self):
        result = rehearsal.run(["resolved_destination", "eligibility_after_recovery"])
        self.assertEqual(result["passed"], ["resolved_destination", "eligibility_after_recovery"])

    def test_exhausted_claims_after_a_retry_decision_hold_for_review(self):
        """Cap exhaustion with an AI RETRY yields a reviewable hold, no loop, no fourth claim."""
        result = rehearsal.run(["claims_exhausted_review_hold"])
        self.assertEqual(result["passed"], ["claims_exhausted_review_hold"])
        self.assertEqual(result["measurements"]["claims_exhausted_review_hold"], {
            "investigate_claims": 3, "coordinator_decisions": 3, "abandoned": 0,
            "claims_exhausted_holds": 1, "final_state": "BLOCKED",
            "final_blocker": "APPLICATION_BROWSER_INVESTIGATION_EXHAUSTED"})

    def test_ai_decided_abandon_is_still_terminal(self):
        result = rehearsal.run(["ai_abandon_terminal"])
        self.assertEqual(result["passed"], ["ai_abandon_terminal"])

    def test_existing_terminal_abandonments_are_untouched(self):
        result = rehearsal.run(["existing_terminal_records_untouched"])
        self.assertEqual(result["passed"], ["existing_terminal_records_untouched"])

    def test_code_outside_the_allowlist_is_held_not_abandoned(self):
        result = rehearsal.run(["outside_allowlist"])
        self.assertEqual(result["passed"], ["outside_allowlist"])


if __name__ == "__main__":
    unittest.main()
