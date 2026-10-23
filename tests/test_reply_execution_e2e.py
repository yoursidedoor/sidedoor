import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import unittest

import manual_reply_execution as manual
import synthetic_resume  # noqa: F401 (synthetic designated resume; the owner's is not in the repo)


def setUpModule():
    # Fixture inbound messages are dated before the 2026-09-30 backlog cutoff, which applies
    # only to the real backlog on file (owner, 2026-09-30: "dont send these replies").
    global _BACKLOG_PATCH
    import datetime as _dt
    from unittest import mock as _mock
    import reply_execution as _reply_execution
    _BACKLOG_PATCH = _mock.patch.object(_reply_execution, "BACKLOG_CUTOFF",
                                        _dt.datetime(2000, 1, 1, tzinfo=_dt.timezone.utc))
    _BACKLOG_PATCH.start()


def tearDownModule():
    _BACKLOG_PATCH.stop()


class ReplyExecutionEndToEndTests(unittest.TestCase):
    def run_scenario(self, name):
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            manual.SCENARIOS[name](fixture)

    def test_grounded_reply_independent_receipt_and_restart(self):
        self.run_scenario("grounded_reply_independent_receipt_restart")

    def test_ordinary_question_without_verified_role_reaches_execution(self):
        self.run_scenario("ordinary_unscoped_question")

    def test_existing_reply_transport_needs_no_direct_history_or_invented_quota(self):
        self.run_scenario("no_direct_history_context_unknown_quota")

    def test_known_provider_message_cooldown_blocks_reply(self):
        self.run_scenario("known_message_cooldown_blocks")

    def test_provider_error_is_retried_three_times_after_thread_checks_then_terminal(self):
        self.run_scenario("provider_error_retried_three_times_then_terminal")

    def test_closure_after_claim_blocks_final_provider_boundary(self):
        self.run_scenario("closure_after_claim_before_provider")

    def test_new_inbound_after_inspection_is_not_reread_before_send(self):
        self.run_scenario("new_inbound_after_inspection_not_reread_before_send")

    def test_previous_response_after_inspection_is_not_reread_before_send(self):
        self.run_scenario("previous_response_after_inspection_not_reread_before_send")

    def test_account_restriction_after_inspection_is_not_reread_before_send(self):
        self.run_scenario("account_restriction_after_inspection_not_reread_before_send")

    def test_interrupted_claim_reconciles_without_sending(self):
        self.run_scenario("interrupted_claim_reconciles_without_send")

    def test_recipient_identity_and_thread_holds_survive_restart(self):
        # "candidate_facts" removed 2026-09-23 (owner): that gate no longer exists, so the case
        # would assert something untrue. The six real controls below are unchanged.
        for name in ("closed", "excluded", "wrong_account", "wrong_thread", "new_inbound",
                     "previous_response"):
            with self.subTest(blocker=name):
                self.run_scenario(name)

    def test_ambiguous_send_requires_independent_reconciliation(self):
        self.run_scenario("ambiguous_send_reconciles_without_resend")

    def test_stale_receipt_does_not_confirm_or_allow_resend(self):
        self.run_scenario("stale_receipt_never_authorizes_retry")

    def test_different_message_does_not_confirm_or_allow_resend(self):
        self.run_scenario("wrong_receipt_text_never_confirms")

    def test_preflight_reads_have_bounded_retry_budget_after_restart(self):
        self.run_scenario("transient_preflight_bounded_retries")

    def test_transient_preflight_recovers_at_scheduled_retry(self):
        self.run_scenario("transient_preflight_recovers")

    def test_closed_recipient_does_not_stop_other_reply_or_erase_notification(self):
        self.run_scenario("blocked_recipient_isolation_pending_notification")


if __name__ == "__main__":
    unittest.main()
