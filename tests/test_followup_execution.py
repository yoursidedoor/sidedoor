"""Regression coverage added after the offline manual walkthrough passed."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import copy
import datetime as dt
import json
import sqlite3
import unittest
from unittest.mock import patch

import engagement
import followup_execution as followup
import manual_followup_e2e as manual
import research_workflow


class FollowupManualRegressionTests(unittest.TestCase):
    pass


def regression(name):
    def run_scenario(self):
        with manual.offline():
            manual.scenario(name)
    return run_scenario


for scenario_name in manual.SCENARIOS:
    setattr(FollowupManualRegressionTests, "test_" + scenario_name, regression(scenario_name))


class FollowupProviderEndToEndTests(unittest.TestCase):
    def test_composed_followup_has_independent_receipt_and_no_replay(self):
        with manual.offline():
            result = manual.provider_e2e()
        self.assertEqual(result["state"], "CONFIRMED_SENT")
        self.assertEqual(result["mock_provider_requests"], 1)
        self.assertEqual(result["ai_phases"], ["COMPOSITION", "INSPECTION", "RECONCILIATION"])
        self.assertTrue(result["independent_receipt"]["independent"])

    def test_lost_acknowledgment_reconciles_without_resend(self):
        with manual.offline():
            result = manual.provider_e2e(lost_ack=True)
        self.assertEqual(result["state"], "CONFIRMED_SENT")
        self.assertEqual(result["mock_provider_requests"], 1)

    def test_late_inbound_after_claim_stops_final_provider_write(self):
        with manual.offline():
            result = manual.provider_e2e(late_change="reply")
        self.assertEqual(result["state"], "CANCELLED_BEFORE_SEND")
        self.assertEqual(result["mock_provider_requests"], 0)
        # The thread-shape rule is removed (owner, 2026-09-30); the late reply changes the thread,
        # so the final thread check still stops the write.
        self.assertIn("FOLLOWUP_THREAD_CHANGED", result["provider_result"]["provider_guard"]["gates"])

    def test_late_closure_after_claim_stops_final_provider_write(self):
        with manual.offline():
            result = manual.provider_e2e(late_change="closure")
        self.assertEqual(result["state"], "CANCELLED_BEFORE_SEND")
        self.assertEqual(result["mock_provider_requests"], 0)
        self.assertIn("OUTREACH_CLOSED", result["provider_result"]["provider_guard"]["gates"])

    def test_missing_receipt_stays_uncertain_without_count_or_retry(self):
        with manual.offline():
            result = manual.provider_e2e(lost_ack=True, receipt_visible=False)
        self.assertEqual(result["state"], "UNCERTAIN_NO_RETRY")
        self.assertEqual(result["mock_provider_requests"], 1)
        self.assertEqual(result["independent_receipt"], "UNKNOWN")


class FollowupCampaignReadinessTests(unittest.TestCase):
    def test_default_campaign_discovery_composes_claims_and_verifies(self):
        with manual.offline():
            result = manual.campaign_followup_e2e("confirmed")
        self.assertEqual(result["mock_followup_requests"], 1)
        self.assertEqual(result["ai_phases"], ["COMPOSITION", "INSPECTION", "RECONCILIATION"])
        self.assertTrue(result["receipts"][0]["independent"])

    def test_observed_invitation_matches_conservative_delivery_interval(self):
        with manual.offline():
            result = manual.campaign_followup_e2e("invitation_in_thread")
        self.assertEqual(result["mock_followup_requests"], 1)

    def test_two_followups_keep_original_historical_verification(self):
        with manual.offline():
            result = manual.campaign_followup_e2e("second_old_context")
        self.assertEqual(result["mock_followup_requests"], 2)
        self.assertEqual(len(result["receipts"]), 2)
        self.assertEqual(result["initial_verified_at"], "2026-09-17T16:00:00+00:00")

    def test_pending_today_acceptance_tomorrow_resumes_without_reset(self):
        with manual.offline():
            result = manual.campaign_followup_e2e("pending_then_accepted")
        self.assertEqual(result["mock_followup_requests"], 1)
        self.assertTrue(result["receipts"][0]["independent"])

    def test_shared_suppression_allowance_is_narrow(self):
        with manual.offline():
            fixture = manual.CampaignFollowupFixture(accepted=False)
            try:
                allowed = followup.outreach._followup_suppression_allowed
                reason = "Outreach reserved; never automatically retry"
                self.assertTrue(allowed(fixture.database, fixture.candidate["profile"], reason))
                self.assertFalse(allowed(fixture.database, fixture.candidate["profile"], "Opted out"))
                self.assertFalse(allowed(fixture.database, "https://www.linkedin.com/in/unconfirmed", reason))
            finally:
                fixture.close()

    def test_pending_acceptance_is_not_a_composition_failure(self):
        with manual.offline():
            result = manual.campaign_followup_e2e("unaccepted")
        self.assertEqual(result["result"]["composition"]["items"][0]["state"], "WAIT_ACCEPTANCE")

    def test_acceptance_read_failures_never_park(self):
        with manual.offline():
            result = manual.campaign_followup_e2e("acceptance_read_failures_keep_polling")
        self.assertEqual(result["technical_attempts"], 4)

    def test_no_observed_thread_is_not_fabricated(self):
        with manual.offline():
            result = manual.campaign_followup_e2e("no_thread")
        self.assertEqual(result["mock_followup_requests"], 0)
        self.assertEqual(result["result"]["composition"]["items"][0]["blocker"], "FOLLOWUP_OBSERVED_THREAD_REQUIRED")

    def test_campaign_stops_and_threshold_precede_provider_reads(self):
        for case in ("before_due", "unconfirmed", "no_automatic", "closure"):
            with self.subTest(case=case), manual.offline():
                self.assertEqual(manual.campaign_followup_e2e(case)["mock_followup_requests"], 0)


class FollowupContractTests(unittest.TestCase):
    def setUp(self):
        self.boundaries = manual.offline()
        self.boundaries.__enter__()
        self.addCleanup(self.boundaries.__exit__, None, None, None)
        self.fixture = manual.Fixture()
        self.addCleanup(self.fixture.close)

    def action(self):
        return json.loads(self.fixture.database.execute("SELECT payload FROM outreach_actions").fetchone()[0])

    def test_disabled_does_not_read_compose_or_send(self):
        self.fixture.config["followup_execution"]["enabled"] = False
        result = self.fixture.run(True)
        self.assertEqual(result["composition"]["state"], "DISABLED")
        self.assertEqual(result["execution"]["state"], "DISABLED")
        self.assertFalse(self.fixture.reads or self.fixture.inferences or self.fixture.sends)

    def test_default_dispatch_off_keeps_unsent_action(self):
        result = self.fixture.run()
        self.assertEqual(result["execution"]["state"], "DISABLED")
        self.assertEqual(self.action()["purpose"], "FOLLOWUP")
        self.assertFalse(self.fixture.sends)

    def observe(self, messages):
        """The inbox reader (real decoder) reads the thread holding these (sender, body, at) messages."""
        import heyreach_schema
        from unittest.mock import Mock
        stamp = lambda at: at.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        room = {"id": "synthetic-thread", "read": True, "groupChat": False, "blockedByMe": False,
                "blockedByParticipant": False, "totalMessages": len(messages), "linkedInAccountId": 123,
                "linkedInAccount": {"id": 123, "profileUrl": self.fixture.config["sender_profile"]},
                "correspondentProfile": {"linkedin_id": "synthetic-member", "profileUrl": manual.PROFILE},
                "messages": [{"createdAt": stamp(at), "body": body, "subject": None, "postLink": None,
                              "isInMail": False, "transcript": None, "isVoiceNote": False,
                              "audioDurationMs": None, "sender": sender} for sender, body, at in messages]}
        room.update(lastMessageAt=room["messages"][-1]["createdAt"], lastMessageText=messages[-1][1],
                    lastMessageType="TEXT", lastMessageSender=messages[-1][0])
        decoder = heyreach_schema.Decoder(123, self.fixture.config["sender_profile"], clock=lambda: self.fixture.now)
        client = Mock()
        client.call.side_effect = [{"totalCount": 1, "items": [room]}, room]
        reader = engagement.HeyReachReadAdapter(client, 123, decoder.decode_conversation, decoder.decode_chatroom)
        return engagement.cycle(self.fixture.database, dict(self.fixture.config, engagement_enabled=True),
                                reader, self.fixture.now)

    def test_our_own_observed_initial_does_not_stop_the_followup(self):
        body = "I saw your team's platform work and would like to connect."
        result = self.observe([("ME", body, manual.INITIAL)])
        self.assertEqual(result["items"][0]["inserted"], 1)
        kinds = [event["kind"] for event in engagement.events_for(self.fixture.database, manual.PROFILE)]
        self.assertEqual(sorted(kinds), ["initial_sent", "outbound_observed"])
        self.assertEqual(followup.plan(self.fixture.database, self.fixture.candidate, self.fixture.now)["state"], "DUE")
        self.fixture.run()
        self.assertEqual(self.action()["purpose"], "FOLLOWUP")
        gates = followup.outreach.action_gates(self.fixture.database, self.fixture.config, self.action(), {}, self.fixture.now)
        self.assertNotIn("OBSERVED_OUTBOUND_REQUIRES_PURPOSE_RECONCILIATION", gates)
        # A message written by hand afterwards stops it at send time too.
        self.observe([("ME", body, manual.INITIAL),
                      ("ME", "Written by hand in LinkedIn.", manual.INITIAL + dt.timedelta(hours=2))])
        gates = followup.outreach.action_gates(self.fixture.database, self.fixture.config, self.action(), {}, self.fixture.now)
        self.assertIn("OBSERVED_OUTBOUND_REQUIRES_PURPOSE_RECONCILIATION", gates)

    def test_matching_takes_heyreach_time_drift_and_no_more(self):
        sent = manual.INITIAL
        source = {"text": "Note", "conversation_id": "synthetic-thread", "invitation": False,
                  "occurred_at": sent.isoformat()}
        at = lambda delta: {"body": "Note", "createdAt": (sent + delta).isoformat()}
        self.assertTrue(followup._matches(source, at(dt.timedelta(0)), "synthetic-thread"))
        self.assertTrue(followup._matches(source, at(dt.timedelta(seconds=3.1)), "synthetic-thread"))
        self.assertFalse(followup._matches(source, at(dt.timedelta(minutes=11)), "synthetic-thread"))
        self.assertFalse(followup._matches(source, at(dt.timedelta(0)), "other-thread"))
        interval = dict(source, invitation=True, conversation_id=None, sent_not_before=sent.isoformat(),
                        occurred_at=(sent + dt.timedelta(hours=2)).isoformat(),
                        occurred_at_basis="CONSERVATIVE_FIRST_INDEPENDENT_DELIVERY_OBSERVATION")
        self.assertTrue(followup._matches(interval, at(dt.timedelta(hours=1)), "any"))
        self.assertTrue(followup._matches(interval, at(-dt.timedelta(minutes=5)), "any"))
        self.assertFalse(followup._matches(interval, at(-dt.timedelta(minutes=11)), "any"))
        self.assertFalse(followup._matches(interval, at(dt.timedelta(hours=2, minutes=11)), "any"))

    def test_followup_anchor_found_with_a_drifted_thread_time(self):
        for message in self.fixture.room["messages"]:
            message["createdAt"] = (dt.datetime.fromisoformat(message["createdAt"]) + dt.timedelta(seconds=3.1)).isoformat()
        self.fixture.run()
        self.assertEqual(self.action()["purpose"], "FOLLOWUP")

    def test_hand_written_observed_message_still_stops_the_followup(self):
        body = "I saw your team's platform work and would like to connect."
        self.observe([("ME", body, manual.INITIAL),
                      ("ME", "Written by hand in LinkedIn.", manual.INITIAL + dt.timedelta(hours=2))])
        planned = followup.plan(self.fixture.database, self.fixture.candidate, self.fixture.now)
        self.assertEqual(planned["blocker"], "OBSERVED_OUTBOUND_REQUIRES_PURPOSE_RECONCILIATION")

    def test_wrong_content_and_source_binding_are_rejected(self):
        self.fixture.run()
        action = self.action()
        for field, value in (("message", "Substituted message"), ("ordinal", 2), ("sender_id", 999),
                             ("followup_sources_sha256", "changed"), ("followup_action_ref", "missing")):
            with self.subTest(field=field):
                changed = dict(action, **{field: value})
                self.assertTrue(followup.binding_gates(self.fixture.database, self.fixture.config, changed, self.fixture.now))

    def test_no_capacity_does_not_repeat_deferral_event_or_extend_deadline(self):
        with self.fixture.database:
            followup.workflow_tasks.reserve_compute(self.fixture.database, "filled", self.fixture.now.isoformat(), 10800)
        self.fixture.run()
        before = dict(self.fixture.database.execute("SELECT * FROM workflow_tasks").fetchone())
        self.fixture.restart()
        self.fixture.now += dt.timedelta(minutes=1)
        self.fixture.run()
        after = dict(self.fixture.database.execute("SELECT * FROM workflow_tasks").fetchone())
        self.assertEqual((before["attempts"], before["deadline"]), (after["attempts"], after["deadline"]))
        self.assertEqual(self.fixture.database.execute("SELECT COUNT(*) FROM workflow_task_events WHERE event='CAPACITY_DEFERRED'").fetchone()[0], 1)

    def test_resume_failure_holds_without_model_or_send(self):
        with patch("followup_execution.reply_resume.context", side_effect=ValueError("REPLY_RESUME_HASH_MISMATCH")):
            result = self.fixture.run(True)
        self.assertEqual(result["composition"]["items"][0]["blocker"], "REPLY_RESUME_HASH_MISMATCH")
        self.assertFalse(self.fixture.inferences or self.fixture.sends)

    def test_ai_hold_remains_a_hold_after_restart(self):
        self.fixture.output_mode = "HOLD"
        self.fixture.run()
        self.fixture.restart()
        result = self.fixture.run()
        self.assertEqual(result["composition"]["items"][0]["state"], "HELD")
        self.assertEqual(result["composition"]["items"][0]["blocker"], "FOLLOWUP_AI_HOLD")
        self.assertEqual(len(self.fixture.inferences), 1)

    def test_alias_does_not_create_another_slot(self):
        self.fixture.run()
        alias = "https://www.linkedin.com/in/new-synthetic-alias"
        research_workflow.register_alias(self.fixture.database, "contact", alias, manual.PROFILE, "synthetic independent alias proof")
        candidate = copy.deepcopy(self.fixture.candidate)
        candidate["profile"] = alias
        self.fixture.run(candidates=[candidate])
        self.assertEqual(self.fixture.database.execute("SELECT COUNT(*) FROM outreach_actions").fetchone()[0], 1)
        self.assertEqual(len(self.fixture.inferences), 1)

    def test_audit_preserves_null_flags_and_refuses_changes(self):
        self.fixture.run()
        context = followup.judgment_context(self.fixture.database, self.action())
        source = context["followup_context"]["sources"][0]
        self.assertIsNone(source["quoted"])
        self.assertIsNone(source["forwarded"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.fixture.database.execute("DELETE FROM outreach_agent_audit")
        self.fixture.database.rollback()

    def test_budget_bounds_reject_booleans_and_unbounded_time(self):
        for timeout in (True, 0, 601):
            with self.subTest(timeout=timeout):
                self.fixture.config["followup_execution"]["timeout_seconds"] = timeout
                with self.assertRaisesRegex(ValueError, "FOLLOWUP_BOUNDS_INVALID"):
                    self.fixture.run()
        self.assertFalse(self.fixture.reads or self.fixture.inferences)

    def test_default_candidates_reuse_confirmed_sender_history(self):
        self.fixture.run(True)
        self.fixture.run(True)
        self.fixture.now = engagement.business_days_after(manual.DUE, 5)
        self.fixture.config["outreach_content_max_age_seconds"] = 14 * 86400
        result = followup.cycle(manual.ROOT, self.fixture.database, self.fixture.config, self.fixture.adapter,
                                execute=self.fixture.execute, now=self.fixture.now)
        self.assertEqual(len(self.fixture.sends), 2, result)
        self.assertEqual(self.fixture.inferences[-1]["plan"]["ordinal"], 2)


if __name__ == "__main__":
    unittest.main()
