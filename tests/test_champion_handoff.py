import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import unittest

import manual_champion_handoff as manual
import synthetic_resume  # noqa: F401 (synthetic designated resume; the owner's is not in the repo)


class ChampionHandoffEndToEndTests(unittest.TestCase):
    def run_scenario(self, name):
        with manual.isolated_boundaries(), manual.ChampionFixture() as fixture:
            manual.SCENARIOS[name](fixture)

    def test_automatic_summary_shared_reply_receipt_hold_and_restart(self):
        self.run_scenario("automatic_summary_shared_reply_and_restart")

    def test_provider_null_flags_direct_speaker_preserves_original_source(self):
        self.run_scenario("provider_null_flags_direct_speaker_success")

    def test_unknowns_decline_and_unbound_evidence_block(self):
        for name in ("AGREEMENT_INTENT", "ROLE_ASSOCIATION", "CANDIDATE_FACTS", "decline", "unbound",
                     "no_agreement", "stale_role"):
            with self.subTest(blocker=name):
                self.run_scenario(name)

    def test_disabled_handoff_and_due_deadline_block(self):
        for name in ("disabled", "deadline_due"):
            with self.subTest(blocker=name):
                self.run_scenario(name)

    def test_provider_speaker_unknowns_and_known_quotes_block(self):
        for name in ("provider_unknown_speaker", "provider_quoted_speaker", "provider_missing_speaker",
                     "provider_unknown_intent", "provider_known_quoted", "provider_known_forwarded",
                     "provider_false_flags_unknown_speaker"):
            with self.subTest(blocker=name):
                self.run_scenario(name)

    def test_source_agreement_must_be_applied_before_promotion(self):
        self.run_scenario("requires_applied_source_agreement")

    def test_eligible_ack_omission_retries_without_consuming_reply_slot(self):
        self.run_scenario("eligible_ack_omission_retries_without_slot_burn")

    def test_eligible_ack_omission_exhausts_bounded_inference_retries(self):
        self.run_scenario("eligible_ack_omission_retry_exhaustion")

    def test_legacy_eligible_ack_is_held_before_reply_slot(self):
        self.run_scenario("legacy_eligible_ack_promotion_guard")

    def test_unscoped_ordinary_ack_remains_allowed(self):
        self.run_scenario("unscoped_ordinary_ack_remains_allowed")

    def test_history_change_after_claim_no_longer_cancels(self):
        self.run_scenario("history_change_after_claim_no_longer_cancels")

    def test_closure_and_revocation_after_inspection_block_sender(self):
        for name in ("closed_final_guard", "revoked_final_guard", "agreement_revoked_final_guard",
                     "role_closed_final_guard"):
            with self.subTest(blocker=name):
                self.run_scenario(name)

    def test_same_inbound_acknowledgment_prevents_second_summary_slot(self):
        self.run_scenario("same_inbound_ack_blocks_summary")

    def test_transient_failure_reports_pending_and_recovers_when_due(self):
        self.run_scenario("transient_bounded_recovery")

    def test_transient_retry_exhaustion_is_reported_and_survives_restart(self):
        self.run_scenario("transient_retry_exhaustion")

    def test_ambiguous_write_reconciles_without_resending(self):
        self.run_scenario("ambiguous_reconciles_without_retry")

    def test_blocked_item_and_failed_notification_do_not_stop_other_handoffs(self):
        self.run_scenario("blocked_item_and_notification_isolation")

    def test_legacy_ordinary_output_unchanged_when_champion_enabled(self):
        self.run_scenario("legacy_ordinary_output_enabled_unchanged")


if __name__ == "__main__":
    unittest.main()
