import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import copy
import datetime as dt
import hashlib
import json
import socket
import unittest
from unittest import mock

import engagement
import orchestrator
import outreach
import research_workflow
import test_outreach as legacy_fixtures


class OutboundIntegrationSafetyTests(unittest.TestCase):
    def setUp(self):
        legacy_fixtures.OutreachTests.setUp(self)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(socket, "create_connection", side_effect=AssertionError("No network in outbound review tests")).start()
        mock.patch("subprocess.run", side_effect=AssertionError("No credentials or subprocess providers in outbound review tests")).start()
        outreach.initialize_actions(self.database)
        self.now = dt.datetime.now(dt.timezone.utc)
        self.config.update(outreach_actions_enabled=True)
        self.action = {
            "action_id": "mock-action", "recipient": "Test Person", "company": "Test Company",
            "profile": self.candidate["profile"], "message": "Exact test note", "purpose": "INITIAL",
            "provider": "heyreach", "channel": "linkedin", "conversation_id": "mock-conversation",
            "verification": {"content_verified": True, "content_sources": ["https://example.invalid/mock-claim"],
                             "identity_verified": True, "identity_source": "https://example.invalid/mock-person",
                             "team_relevance_verified": True, "team_source": "https://example.invalid/mock-team",
                             "verified_at": self.now.isoformat()},
        }
        self.proof = {
            "identity": orchestrator.profile_key(self.action["profile"]), "sender_id": self.config["sender_id"],
            "sender_ready": True, "entitlement_verified": True, "direct_messages_complete": True,
            "sent_invitations_complete": True, "pending_invitation": False, "has_reply": False,
            "negative_reply": False, "opt_out": False, "cooldown": False, "prior_outreach": False,
            "evidence": "mock-independent-direct-history", "checked_at": self.now.isoformat(),
            "company_contact_count": 0, "company_count_evidence": "mock-complete-company-history",
            "conversation_id": "mock-conversation", "followups_sent": 0, "initial_message_sent": False,
        }
        self.transport = mock.Mock(return_value={"acknowledged": True})
        self.inspector = mock.Mock(side_effect=lambda action, config: copy.deepcopy(self.proof))
        self.reconciler = mock.Mock(return_value={"acknowledged": True})
        self.adapter = outreach.HeyReachActionAdapter(self.transport, self.inspector, self.reconciler)

    def event(self, kind="initial_sent", source_id="mock-event", **changes):
        return {"source": "heyreach", "source_id": source_id, "kind": kind,
                "profile": self.action["profile"], "occurred_at": self.now.isoformat(),
                "observed_at": self.now.isoformat(), "verified": True,
                "evidence": "mock-independent-evidence", "raw": {"mock": True}, **changes}

    def queue_action(self):
        return outreach.enqueue_action(self.database, self.action)

    def run_action(self, database=None):
        return outreach.cycle(database or self.database, self.config, self.adapter, self.now)

    def action_row(self):
        return self.database.execute("SELECT * FROM outreach_actions WHERE action_id=?", (self.action["action_id"],)).fetchone()

    def restart(self):
        database = orchestrator.connect(self.root)
        self.addCleanup(database.close)
        return database

    def receipt(self, **changes):
        row = self.action_row()
        prepare = self.action["purpose"] == "PREPARE_CAMPAIGN"
        return {"independent": True, "action_id": row["action_id"], "identity": row["identity"],
                "payload_hash": row["payload_hash"], "operation": "create_campaign" if prepare else "send_message",
                "outcome": "DRAFT_CREATED" if prepare else "SENT", "receipt_id": "mock-receipt",
                "evidence": "mock-independent-provider-read", "occurred_at": self.now.isoformat(),
                "observed_at": self.now.isoformat(), "sender_id": self.config["sender_id"],
                "conversation_id": self.action.get("conversation_id"), "campaign_id": 778,
                **changes}

    def prepare_campaign(self):
        self.action.update(purpose="PREPARE_CAMPAIGN", list_id=321, campaign_name="Mock draft only",
                           sequence={"nodeType": "CONNECTION_REQUEST", "payload": {"messages": [self.action["message"]]},
                                     "conditionalNode": {"nodeType": "END", "actionDelayUnit": "DAY", "actionDelay": 3},
                                     "unconditionalNode": {"nodeType": "END", "actionDelayUnit": "DAY", "actionDelay": 3}})
        self.proof.update(single_contact_list_verified=True, list_id=321,
                          sequence_source="mock-observed-provider-sequence", sequence=copy.deepcopy(self.action["sequence"]))

    def history(self, suffix="old", profile=None, recipient=None):
        profile = profile or "https://www.linkedin.com/in/mock-" + suffix
        recipient = recipient or "Mock " + suffix
        with self.database:
            self.database.execute("INSERT INTO history VALUES (?,?,?,?,?)",
                                  (orchestrator.profile_key(profile), recipient, self.action["company"], profile,
                                   json.dumps({"source": "mock-independent-history"})))

    def test_no_automatic_identity_name_and_campaign_each_block_new_path(self):
        for changes in ({"profile": "https://www.linkedin.com/in/jrivera/"},
                        {"recipient": " Jamie  Rivera "}, {"campaign_id": 700001}):
            with self.subTest(changes=changes):
                action = dict(self.action, **changes)
                proof = dict(self.proof, identity=orchestrator.profile_key(action["profile"]))
                self.assertIn("NO_AUTOMATIC_OUTREACH_CONTACT", outreach.action_gates(self.database, self.config, action, proof, self.now))

    def test_history_only_contact_is_refused_before_a_draft(self):
        # Owner, 2026-10-01: contact history is checked once, before a draft is created.
        self.history(profile=self.action["profile"], recipient=self.action["recipient"])
        self.assertEqual(research_workflow.prior_contact_record(self.database, self.action["profile"],
                                                                self.action["recipient"]), "history")

    def test_legacy_dispatch_honors_engagement_initial_delivery(self):
        engagement.record_event(self.database, self.event())
        with self.assertRaisesRegex(ValueError, "VERIFIED_MESSAGE_ALREADY_SENT"):
            outreach.enqueue(self.database, self.candidate)
        result = outreach.dispatch(self.database, self.config, self.pipeline, self.client)
        self.assertEqual(result["launch_attempts"], 0, "Verified initial delivery in engagement ledger must prevent legacy repeat")
        self.client.start_claimed_campaign.assert_not_called()

    def test_legacy_claim_rechecks_cancelled_queue(self):
        outreach.enqueue(self.database, self.candidate)
        original = outreach.claim_send

        def cancel_before_claim(database, candidate, *args, **kwargs):
            with database:
                database.execute("UPDATE outreach_queue SET state='CANCELLED'")
            return original(database, candidate, *args, **kwargs)

        with mock.patch.object(outreach, "claim_send", side_effect=cancel_before_claim):
            result = outreach.dispatch(self.database, self.config, self.pipeline, self.client)
        self.assertEqual(result["launch_attempts"], 0, "Cancellation committed after gates must win at claim")
        self.client.start_claimed_campaign.assert_not_called()

    def test_reply_between_legacy_gate_and_claim_blocks_send(self):
        outreach.enqueue(self.database, self.candidate)
        original = outreach.claim_send

        def reply_before_claim(database, candidate, *args, **kwargs):
            engagement.record_event(database, self.event("inbound"))
            return original(database, candidate, *args, **kwargs)

        with mock.patch.object(outreach, "claim_send", side_effect=reply_before_claim):
            result = outreach.dispatch(self.database, self.config, self.pipeline, self.client)
        self.assertEqual(result["launch_attempts"], 0)
        self.client.start_claimed_campaign.assert_not_called()

    def test_reply_between_new_gate_and_claim_blocks_send(self):
        self.queue_action()
        original = outreach.action_gates
        calls = []

        def gate_then_reply(database, config, action, proof, now):
            result = original(database, config, action, proof, now)
            calls.append(True)
            if len(calls) == 1:
                engagement.record_event(database, self.event("inbound"))
            return result

        with mock.patch.object(outreach, "action_gates", side_effect=gate_then_reply):
            self.run_action()
        self.transport.assert_not_called()

    def test_cancel_between_new_gate_and_claim_blocks_send(self):
        self.queue_action()
        original = outreach.action_gates
        calls = []

        def gate_then_cancel(database, config, action, proof, now):
            result = original(database, config, action, proof, now)
            calls.append(True)
            if len(calls) == 1:
                with database:
                    database.execute("UPDATE outreach_actions SET state='CANCELLED'")
            return result

        with mock.patch.object(outreach, "action_gates", side_effect=gate_then_cancel):
            self.run_action()
        self.transport.assert_not_called()

    def test_reply_after_dispatch_claim_before_provider_call_blocks_send(self):
        self.queue_action()
        original = self.adapter.execute_claimed

        def reply_before_request(database, action_id, config, now=None):
            engagement.record_event(database, self.event("inbound"))
            return original(database, action_id, config, now=now)

        with mock.patch.object(self.adapter, "execute_claimed", side_effect=reply_before_request):
            self.run_action()
        self.assertEqual(self.database.execute("SELECT count(*) FROM engagement_stops").fetchone()[0], 1)
        self.transport.assert_not_called()

    def test_inspected_payload_cannot_be_replaced_before_claim(self):
        self.queue_action()

        def replace_after_inspection(action, config):
            changed = dict(action, message="Different unverified message")
            with self.database:
                self.database.execute("UPDATE outreach_actions SET payload=? WHERE action_id=?",
                                      (json.dumps(changed, sort_keys=True), action["action_id"]))
            return copy.deepcopy(self.proof)

        self.inspector.side_effect = replace_after_inspection
        self.run_action()
        self.transport.assert_not_called()

    def test_same_action_id_cannot_reauthorize_changed_message(self):
        self.queue_action()
        with self.assertRaises(ValueError):
            outreach.enqueue_action(self.database, dict(self.action, message="Different text"))

    def test_stale_direct_history_blocks_without_provider_write(self):
        self.proof["checked_at"] = (self.now - dt.timedelta(hours=2)).isoformat()
        self.queue_action()
        self.run_action()
        self.transport.assert_not_called()

    def test_stale_content_and_identity_facts_require_refresh(self):
        self.action["verification"]["verified_at"] = (self.now - dt.timedelta(days=400)).isoformat()
        self.queue_action()
        self.run_action()
        self.transport.assert_not_called()

    def test_verified_alias_cannot_evade_existing_contact_suppression(self):
        research_workflow.initialize(self.database)
        old_profile = "https://www.linkedin.com/in/mock-previous-identity"
        research_workflow.register_alias(self.database, "contact", self.action["profile"], old_profile,
                                         {"source": "mock-verified-identity-mapping"})
        self.history(profile=old_profile, recipient="Mock Previous Name")
        with self.database:
            self.database.execute("INSERT INTO suppressions VALUES (?,?)",
                                  (orchestrator.profile_key(old_profile), "Previously contacted mock recipient"))
        self.queue_action()
        self.run_action()
        self.transport.assert_not_called()

    def test_acknowledgment_is_not_delivery_and_restart_never_resends(self):
        self.queue_action()
        self.run_action()
        self.assertEqual(self.action_row()["state"], "ACKNOWLEDGED_UNCONFIRMED")
        self.run_action(self.restart())
        self.transport.assert_called_once()
        self.assertEqual(self.database.execute("SELECT count(*) FROM engagement_events").fetchone()[0], 0)

    def test_send_timeout_is_not_retried_after_restart(self):
        self.transport.side_effect = TimeoutError("Mock request may have completed")
        self.queue_action()
        self.run_action()
        self.run_action(self.restart())
        self.transport.assert_called_once()
        self.assertEqual(self.action_row()["state"], "UNCERTAIN_NO_RETRY")

    def test_independent_matching_receipt_applies_delivery_once(self):
        self.queue_action()
        self.run_action()
        self.reconciler.return_value = self.receipt()
        self.run_action()
        self.run_action(self.restart())
        self.transport.assert_called_once()
        self.assertEqual(self.action_row()["state"], "CONFIRMED_SENT")
        self.assertEqual(self.database.execute("SELECT count(*) FROM engagement_events").fetchone()[0], 1)

    def test_wrong_payload_receipt_never_becomes_delivery(self):
        self.queue_action()
        self.run_action()
        self.reconciler.return_value = self.receipt(payload_hash="wrong-payload")
        self.run_action()
        self.assertNotEqual(self.action_row()["state"], "CONFIRMED_SENT")
        self.assertEqual(self.database.execute("SELECT count(*) FROM engagement_events").fetchone()[0], 0)

    def test_receipt_from_other_sender_or_thread_never_becomes_delivery(self):
        self.queue_action()
        self.run_action()
        for mismatch in ({"sender_id": 987654}, {"conversation_id": "another-thread"}):
            with self.subTest(mismatch=mismatch):
                before = self.reconciler.call_count
                self.reconciler.return_value = self.receipt(**mismatch)
                self.run_action()
                self.assertEqual(self.reconciler.call_count, before + 1)
                self.assertNotEqual(self.action_row()["state"], "CONFIRMED_SENT")
                self.assertEqual(self.database.execute("SELECT count(*) FROM engagement_events").fetchone()[0], 0)
                self.assertTrue(outreach.resume_read(self.database, self.action["action_id"], "RECONCILIATION", self.now))

    def test_campaign_create_timeout_never_creates_again_or_launches(self):
        self.prepare_campaign()
        self.queue_action()
        self.transport.side_effect = TimeoutError("Mock campaign may exist")
        self.run_action()
        self.run_action(self.restart())
        self.transport.assert_called_once()
        self.assertEqual(self.transport.call_args.args[0], "create_campaign")
        self.assertEqual(self.action_row()["state"], "UNCERTAIN_NO_RETRY")

    def test_campaign_ack_requires_independent_draft_and_never_launches(self):
        self.prepare_campaign()
        self.queue_action()
        self.run_action()
        self.run_action()
        self.assertEqual(self.action_row()["state"], "ACKNOWLEDGED_UNCONFIRMED")
        self.reconciler.return_value = self.receipt()
        due = self.now + dt.timedelta(minutes=15)
        retry = self.database.execute("SELECT next_at,attempts FROM outreach_read_retries WHERE action_id=? AND phase='RECONCILIATION'",
                                      (self.action["action_id"],)).fetchone()
        self.assertEqual(tuple(retry), (due.isoformat(), 1))
        self.now = due - dt.timedelta(seconds=1)
        self.run_action(self.restart())
        self.reconciler.assert_called_once()
        self.assertEqual(self.action_row()["state"], "ACKNOWLEDGED_UNCONFIRMED")
        self.now = due
        self.run_action()
        self.assertEqual(self.reconciler.call_count, 2)
        self.assertEqual(self.action_row()["state"], "PREPARED")
        self.transport.assert_called_once()
        self.assertEqual(self.transport.call_args.args[0], "create_campaign")

    def test_prepared_receipt_cannot_reuse_campaign_attempted_for_another_person(self):
        with self.database:
            self.database.execute("INSERT INTO outreach_attempts VALUES ('linkedin.com/in/mock-other',778,'ALREADY_SENT',?,NULL)",
                                  (self.now.isoformat(),))
        self.prepare_campaign()
        self.queue_action()
        self.run_action()
        self.reconciler.return_value = self.receipt(campaign_id=778)
        self.run_action()
        self.assertNotEqual(self.action_row()["state"], "PREPARED")

    def test_legacy_campaign_claim_blocks_new_action_after_restart(self):
        outreach.enqueue(self.database, self.candidate)
        self.assertTrue(outreach.claim_send(self.database, self.candidate))
        self.queue_action()
        self.run_action(self.restart())
        self.transport.assert_not_called()

    def test_new_uncertain_action_blocks_legacy_campaign_after_restart(self):
        self.queue_action()
        self.transport.side_effect = TimeoutError()
        self.run_action()
        with self.assertRaises(ValueError):
            outreach.enqueue(self.restart(), self.candidate)
        self.client.start_claimed_campaign.assert_not_called()

    def test_unique_contact_is_not_double_counted_across_ledgers(self):
        self.history(profile=self.action["profile"], recipient=self.action["recipient"])
        outreach.enqueue(self.database, self.candidate)
        self.queue_action()
        usage = outreach.contact_usage(self.database, self.action["company"], self.action)
        self.assertEqual(usage["used"], 1)
        self.assertEqual(usage["projected"], 1)

    def test_same_name_different_profiles_still_consume_two_slots(self):
        self.history("one", recipient="Mock Common Name")
        self.history("two", recipient="Mock Common Name")
        usage = outreach.contact_usage(self.database, self.action["company"], self.action)
        self.assertEqual(usage["used"], 2)
        self.assertEqual(usage["projected"], 3)

    def test_name_only_history_matches_profile_once_without_freeing_unknown_slots(self):
        with self.database:
            self.database.execute("INSERT INTO history VALUES (?,?,?,?,?)",
                                  ("name:test person", self.action["recipient"], self.action["company"], None, "mock-history"))
        self.queue_action()
        pipeline = {"companies": [{"company": self.action["company"], "conservative_known_contact_count": 2}]}
        usage = outreach.contact_usage(self.database, self.action["company"], self.action, pipeline)
        self.assertEqual(usage["projected"], 2)

    def test_confirmed_action_and_research_reservation_count_as_one_historical_person(self):
        research_workflow.initialize(self.database)
        with self.database:
            self.database.execute("INSERT INTO rw_contacts VALUES (?,?,?,0,'mock-plan')",
                                  ("test company", research_workflow.profile_key(self.action["profile"]), "test person"))
            self.database.execute("INSERT INTO rw_baselines VALUES ('test company',1)")
        self.queue_action()
        with self.database:
            self.database.execute("UPDATE outreach_actions SET state='CONFIRMED_SENT'")
        usage = outreach.contact_usage(self.database, self.action["company"], self.action)
        self.assertEqual(usage["projected"], 1)

    def test_source_event_rebinding_rejected_without_removing_stop(self):
        observed = self.event("inbound")
        engagement.record_event(self.database, observed)
        with self.assertRaises(ValueError):
            engagement.record_event(self.database, dict(observed, profile="https://linkedin.com/in/mock-other"))
        self.assertEqual(self.database.execute("SELECT count(*) FROM engagement_stops").fetchone()[0], 1)

    def test_changed_raw_receipt_does_not_overwrite_source_observation(self):
        observed = self.event()
        engagement.record_event(self.database, observed)
        with self.assertRaises(ValueError):
            engagement.record_event(self.database, dict(observed, raw={"mock": "changed"}))
        self.assertEqual(json.loads(self.database.execute("SELECT raw FROM engagement_events").fetchone()[0]), observed["raw"])

    def test_alias_of_opted_out_person_blocks_without_history_mirror(self):
        research_workflow.initialize(self.database)
        canonical_profile = "https://www.linkedin.com/in/mock-canonical-stop"
        research_workflow.register_alias(self.database, "contact", self.action["profile"], canonical_profile,
                                         {"source": "mock-verified-identity-mapping"})
        engagement.record_event(self.database, self.event("opt_out", profile=canonical_profile))
        self.assertEqual(self.database.execute("SELECT count(*) FROM history").fetchone()[0], 0)
        self.queue_action()
        self.run_action()
        self.transport.assert_not_called()

    def test_alias_of_uncertain_action_blocks_second_initial(self):
        research_workflow.initialize(self.database)
        canonical_profile = "https://www.linkedin.com/in/mock-canonical-pending"
        research_workflow.register_alias(self.database, "contact", self.action["profile"], canonical_profile,
                                         {"source": "mock-verified-identity-mapping"})
        previous = dict(self.action, action_id="mock-earlier", profile=canonical_profile, recipient="Mock Earlier Name")
        outreach.enqueue_action(self.database, previous)
        with self.database:
            self.database.execute("UPDATE outreach_actions SET state='UNCERTAIN_NO_RETRY',claimed_at=? WHERE action_id='mock-earlier'",
                                  (self.now.isoformat(),))
        self.queue_action()
        self.run_action()
        self.transport.assert_not_called()

    def test_sender_rotation_cannot_rebind_claim_to_another_accounts_receipt(self):
        self.queue_action()
        self.run_action()
        self.transport.assert_called_once()
        self.config["sender_id"] = 987654
        self.reconciler.return_value = self.receipt(sender_id=987654)
        self.run_action()
        self.assertNotEqual(self.action_row()["state"], "CONFIRMED_SENT")
        self.assertEqual(self.database.execute("SELECT count(*) FROM engagement_events").fetchone()[0], 0)

    def test_payload_and_hash_replacement_after_claim_cannot_rebind_inspection(self):
        self.queue_action()
        original = self.adapter.execute_claimed

        def replace_after_claim(database, action_id, config, now=None):
            changed = dict(self.action, message="Uninspected replacement after claim")
            payload = json.dumps(changed, sort_keys=True)
            with database:
                database.execute("UPDATE outreach_actions SET payload=?,payload_hash=? WHERE action_id=?",
                                 (payload, hashlib.sha256(payload.encode()).hexdigest(), action_id))
            return original(database, action_id, config, now=now)

        with mock.patch.object(self.adapter, "execute_claimed", side_effect=replace_after_claim):
            self.run_action()
        self.transport.assert_not_called()

    def test_campaign_collision_committed_during_receipt_read_is_rejected(self):
        self.prepare_campaign()
        self.queue_action()
        self.run_action()

        def reconcile_then_race(action, config):
            receipt = self.receipt()
            with self.database:
                self.database.execute("INSERT INTO outreach_prepared_campaigns VALUES (778,'mock-other-owner','linkedin.com/in/mock-other')")
            return receipt

        self.reconciler.side_effect = reconcile_then_race
        self.run_action()
        self.assertNotEqual(self.action_row()["state"], "PREPARED")
        self.assertEqual(self.database.execute("SELECT action_id FROM outreach_prepared_campaigns WHERE campaign_id=778").fetchone()[0], "mock-other-owner")
        self.transport.assert_called_once()

    def test_partial_legacy_schema_keeps_real_canonical_stop_effective(self):
        with self.database:
            self.database.execute("DROP TABLE outreach_queue")
            self.database.execute("DROP TABLE outreach_attempts")
            self.database.execute("DROP TABLE history")
        engagement.record_event(self.database, self.event("opt_out"))
        self.queue_action()
        result = self.run_action()
        self.inspector.assert_called_once()
        self.assertIn("OPTED_OUT", result["items"][0]["gates"])
        self.assertEqual(self.action_row()["state"], "BLOCKED")
        self.transport.assert_not_called()

    def test_reconciliation_missing_sender_and_thread_does_not_supply_delivery(self):
        self.queue_action()
        self.run_action()
        receipt = self.receipt()
        receipt.pop("sender_id")
        receipt.pop("conversation_id")
        self.reconciler.return_value = receipt
        self.run_action()
        self.assertNotEqual(self.action_row()["state"], "CONFIRMED_SENT")
        self.assertEqual(self.database.execute("SELECT count(*) FROM engagement_events").fetchone()[0], 0)

    def retry_row(self, phase):
        return self.database.execute("SELECT * FROM outreach_read_retries WHERE action_id=? AND phase=?",
                                     (self.action["action_id"], phase)).fetchone()

    def test_review_inspection_budget_persists_boundaries_and_widens_without_sending(self):
        # Reads never exhaust (owner, 2026-09-21): each failure buys a later date, and the
        # backoff widens 15 -> 60 -> 240 and then holds at the ceiling, so an unreconcilable
        # action costs a bounded trickle instead of a spin. The boundaries below are exact:
        # one minute before each date nothing is spent, and on it exactly one read is.
        self.queue_action()
        self.inspector.side_effect = TimeoutError("mock private diagnostic")
        start = self.now
        due = {1: 15, 2: 75, 3: 315}
        for minutes, count in ((0, 1), (14, 1), (15, 2), (74, 2), (75, 3), (314, 3), (315, 4), (1500, 5)):
            self.now = start + dt.timedelta(minutes=minutes)
            self.run_action(self.restart())
            self.assertEqual(self.inspector.call_count, count)
            self.assertEqual(self.retry_row("INSPECTION")["attempts"], count)
            if count in due:
                self.assertEqual(self.retry_row("INSPECTION")["next_at"],
                                 (start + dt.timedelta(minutes=due[count])).isoformat())
        self.assertEqual(self.retry_row("INSPECTION")["state"], "RETRY_WAIT")
        self.assertIsNotNone(self.retry_row("INSPECTION")["next_at"])
        self.assertEqual(self.retry_row("INSPECTION")["last_error"], "TimeoutError")
        self.assertFalse(outreach.resume_read(self.database, self.action["action_id"], "INSPECTION", self.now))
        # However many times it reads, it never sends.
        self.transport.assert_not_called()

    def test_review_uncertain_create_widens_reads_without_a_second_create(self):
        # Reads never exhaust (owner, 2026-09-21); an unknown create stays reconcilable for
        # as long as it takes. The claim and the single create below are what must not move.
        self.prepare_campaign()
        self.queue_action()
        self.transport.side_effect = TimeoutError("mock creation outcome unknown")
        self.run_action()
        claim = dict(self.database.execute("SELECT * FROM outreach_action_claims").fetchone())
        self.reconciler.side_effect = [TimeoutError(), {"state": "UNKNOWN"}, ConnectionError(),
                                       ConnectionError(), ConnectionError()]
        start = self.now
        for minutes, count in ((0, 1), (14, 1), (15, 2), (74, 2), (75, 3), (314, 3), (315, 4), (1500, 5)):
            self.now = start + dt.timedelta(minutes=minutes)
            self.run_action(self.restart())
            self.assertEqual(self.reconciler.call_count, count)
            self.assertEqual(self.retry_row("RECONCILIATION")["attempts"], count)
        self.assertEqual(self.retry_row("RECONCILIATION")["state"], "RETRY_WAIT")
        self.assertEqual(self.action_row()["state"], "UNCERTAIN_NO_RETRY")
        self.assertEqual(dict(self.database.execute("SELECT * FROM outreach_action_claims").fetchone()), claim)
        self.assertFalse(outreach.resume_read(self.database, self.action["action_id"], "RECONCILIATION", self.now))
        self.transport.assert_called_once()
        self.assertEqual(self.transport.call_args.args[0], "create_campaign")

    def test_review_unsupported_inspection_preserves_spent_budget_until_explicit_repair(self):
        self.queue_action()
        self.inspector.side_effect = [TimeoutError(), NotImplementedError("mock adapter unavailable"), copy.deepcopy(self.proof)]
        self.run_action()
        self.now += dt.timedelta(minutes=15)
        self.run_action()
        held = self.retry_row("INSPECTION")
        self.assertEqual((held["state"], held["attempts"]), ("ADAPTER_UNAVAILABLE", 1))
        self.assertIsNone(held["next_at"])
        for unused in range(5):
            self.run_action(self.restart())
        self.assertEqual(self.inspector.call_count, 2)
        self.assertTrue(outreach.resume_read(self.database, self.action["action_id"], "INSPECTION", self.now))
        self.run_action()
        self.assertEqual(self.retry_row("INSPECTION")["attempts"], 2)
        self.assertEqual(self.retry_row("INSPECTION")["state"], "COMPLETE")
        self.transport.assert_called_once()

    def test_review_unsupported_reconciliation_never_spends_budget_or_releases_send(self):
        self.queue_action()
        self.run_action()
        self.reconciler.side_effect = NotImplementedError("mock unsupported receipt decoder")
        for unused in range(6):
            self.run_action(self.restart())
        self.reconciler.assert_called_once()
        self.assertEqual(self.retry_row("RECONCILIATION")["attempts"], 0)
        self.assertEqual(self.retry_row("RECONCILIATION")["state"], "ADAPTER_UNAVAILABLE")
        self.assertEqual(self.action_row()["state"], "ACKNOWLEDGED_UNCONFIRMED")
        self.transport.assert_called_once()

    def test_review_mixed_inspection_reconciliation_has_one_sixty_item_budget(self):
        def fixture_action(index, prefix):
            return dict(self.action, action_id=prefix + str(index).zfill(3), company="Mock Company " + prefix + str(index),
                        recipient="Mock Person " + prefix + str(index), profile="https://linkedin.com/in/mock-" + prefix + str(index))

        self.inspector.side_effect = lambda action, config: dict(self.proof, identity=orchestrator.profile_key(action["profile"]))
        for index in range(15):
            outreach.enqueue_action(self.database, fixture_action(index, "a"))
        self.run_action()
        self.assertEqual(self.transport.call_count, 15)
        self.inspector.reset_mock()
        self.inspector.side_effect = NotImplementedError()
        self.reconciler.side_effect = TimeoutError()
        for index in range(50):
            outreach.enqueue_action(self.database, fixture_action(index, "b"))
        self.config["outreach_max_items_per_cycle"] = 1000
        first = self.run_action()
        self.assertEqual(len(first["items"]), 60)
        self.assertEqual(self.inspector.call_count + self.reconciler.call_count, 60)
        second = self.run_action(self.restart())
        self.assertLessEqual(len(second["items"]), 60)
        self.assertEqual(self.inspector.call_count, 50)
        self.assertEqual(self.reconciler.call_count, 15)
        self.assertEqual(self.transport.call_count, 15)
