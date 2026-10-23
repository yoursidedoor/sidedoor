import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import unittest

import inactivity_closure
import manual_inactivity_closure as manual


class InactivityClosureTests(unittest.TestCase):
    def setUp(self):
        self.database, self.root = self.enterContext(manual.fixture())

    def check(self, scenario):
        scenario(inactivity_closure, self.database, self.root)

    def test_acceptance_and_followup_alone_do_not_start_clock(self):
        self.check(manual.check_unstarted)

    def test_exact_27_calendar_day_boundary_is_inclusive(self):
        self.check(manual.check_exact_boundary)

    def test_verified_acceptance_resets_clock(self):
        self.check(manual.check_acceptance_reset)

    def test_latest_inbound_resets_clock_despite_out_of_order_acceptance(self):
        self.check(manual.check_inbound_reset)

    def test_own_followup_and_outbound_do_not_reset_clock(self):
        self.check(manual.check_outbound_does_not_reset)

    def test_verified_observed_outbound_starts_clock(self):
        self.check(manual.check_verified_outbound_starts)

    def test_earliest_verified_outbound_is_original_anchor(self):
        self.check(manual.check_earliest_verified_outbound)

    def test_observation_and_replay_times_do_not_reset_clock(self):
        self.check(manual.check_observation_is_not_anchor)

    def test_calendar_days_and_equivalent_timezone_offsets(self):
        self.check(manual.check_calendar_and_timezone)

    def test_verified_aliases_share_anchor_after_restart(self):
        self.check(manual.check_aliases_and_restart)

    def test_closure_survives_late_engagement_alias_archival_and_restart(self):
        self.check(manual.check_closed_never_reopens)

    def test_unverified_initial_delivery_is_rejected(self):
        self.check(manual.check_unverified_delivery_rejected)

    def test_inbound_only_managed_conversation_has_anchor(self):
        self.check(manual.check_inbound_only_anchor)

    def test_unverified_observations_neither_start_nor_reset_clock(self):
        self.check(manual.check_unverified_events_do_not_anchor_or_reset)

    def test_unknown_activity_does_not_block_send_and_does_not_close(self):
        self.check(manual.check_unknown_activity_does_not_hold_send)

    def test_due_send_gate_respects_exact_boundary_and_configuration(self):
        self.check(manual.check_due_send_gate)

    def test_canonical_identity_without_scheme_is_supported(self):
        self.check(manual.check_canonical_identity)

    def test_existing_message_and_invitation_guards_hold_due_recipient(self):
        self.check(manual.check_existing_outreach_send_gates)

    def test_cycle_closes_after_refresh_and_retains_audit_across_restart(self):
        self.check(manual.check_cycle_closes_only_after_refresh)

    def test_cycle_replans_fresh_inbound_before_closure(self):
        self.check(manual.check_cycle_replans_fresh_inbound)

    def test_cycle_replans_fresh_acceptance_before_closure(self):
        self.check(manual.check_cycle_replans_fresh_acceptance)

    def test_cycle_fresh_outbound_does_not_delay_closure(self):
        self.check(manual.check_cycle_fresh_outbound_does_not_delay)

    def test_cycle_is_disabled_without_explicit_configuration(self):
        self.check(manual.check_cycle_default_disabled)

    def test_cycle_does_not_refresh_active_recipient(self):
        self.check(manual.check_cycle_active_no_refresh)

    def test_three_refresh_attempts_and_both_audits_survive_restart(self):
        self.check(manual.check_cycle_failure_bound_survives_restart)

    def test_refresh_failure_does_not_block_independent_candidate(self):
        self.check(manual.check_cycle_failures_are_isolated)

    def test_closure_preserves_claims_delivery_facts_and_uncertainty(self):
        self.check(manual.check_cycle_preserves_delivery_claims)

    def test_retry_schedule_is_15_then_60_minutes_without_outbound_reset(self):
        self.check(manual.check_cycle_retry_schedule)

    def test_cycle_closes_managed_inbound_only_conversation(self):
        self.check(manual.check_cycle_managed_inbound_only)

    def test_cycle_includes_conversation_only_identity(self):
        self.check(manual.check_cycle_conversation_only_identity_is_candidate)

    def test_cycle_reports_unmapped_provider_id_identity_without_reading(self):
        self.check(manual.check_cycle_provider_id_identity_is_unknown)

    def test_unobservable_acceptance_closes_on_second_clean_read_27_days_later(self):
        self.check(manual.check_cycle_unobservable_acceptance_needs_two_clean_reads)

    def test_inbound_during_second_window_resets_clock(self):
        self.check(manual.check_cycle_inbound_in_second_window_resets)

    def test_cycle_checks_at_most_two_candidates_and_advances_remaining(self):
        self.check(manual.check_cycle_candidate_bound)

    def test_cycle_deduplicates_aliases_and_closes_both(self):
        self.check(manual.check_cycle_alias_deduplication)

    def test_refresh_recipient_mismatch_rolls_back_entire_event_packet(self):
        self.check(manual.check_cycle_refresh_atomic_on_invalid_recipient)

    def test_policy_rejects_unapproved_thresholds_and_types(self):
        self.check(manual.check_cycle_rejects_unauthorized_threshold)

    def test_capacity_wait_preserves_attempts_and_resumes_after_restart(self):
        self.check(manual.check_cycle_capacity_wait_preserves_attempts)

    def test_unknown_coverage_retains_positive_engagement_without_closure(self):
        self.check(manual.check_cycle_unknown_coverage_retains_positive_engagement)

    def test_undated_prior_history_is_not_read_until_dated(self):
        self.check(manual.check_cycle_undated_history_remains_unknown)

    def test_service_entry_runs_real_closure_and_persists_worker_result(self):
        self.check(manual.check_orchestrator_service_integration)

    def test_legacy_final_guard_blocks_due_and_unknown_without_consuming_claim(self):
        self.check(manual.check_legacy_final_guard_preserves_claim)

    def test_malformed_event_holds_affected_recipient_while_healthy_candidate_closes(self):
        self.check(manual.check_malformed_event_does_not_starve_candidates)

    def test_historical_identity_without_optional_profile_is_not_read(self):
        self.check(manual.check_history_identity_without_profile_recovers)


if __name__ == "__main__":
    unittest.main()
