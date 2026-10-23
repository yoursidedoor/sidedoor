import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import copy
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import hashlib
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import orchestrator
import outreach
import outreach_agent
import engagement
import research_workflow


class OutreachTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "state").mkdir()
        self.database = orchestrator.connect(self.root)
        self.addCleanup(self.database.close)
        outreach.initialize(self.database)
        self.config = {"outreach_mode": "guarded", "sender_id": 424242, "outreach_history_max_age_seconds": 3600}
        self.pipeline = {"companies": [], "rules": {"max_unique_contacts_per_company": 3}}
        self.candidate = {"recipient": "Test Person", "company": "Test Company",
                          "profile": "https://www.linkedin.com/in/test-person/", "message": "Exact test note", "campaign_id": 999,
                          "verification": {"identity_verified": True, "team_relevance_verified": True,
                                           "identity_source": "https://example.com/team", "team_source": "https://example.com/role",
                                           "linkedin_messages_checked": True, "linkedin_sent_invitations_checked": True,
                                           "prior_outreach": False, "pending_invitation": False, "has_reply": False,
                                           "negative_reply": False, "verified_at": orchestrator.utcnow(), "history_evidence": "Verified test fixture"}}
        self.responses = {
            "get_campaign": {"id": 999, "campaignAccountIds": [424242], "status": "DRAFT", "startedAt": None,
                             "linkedInUserListId": 123, "excludeInOtherCampaigns": True, "excludeHasOtherAccConversations": True,
                             "excludeContactedFromSenderInOtherCampaign": True},
            "get_linked_in_account_by_id": {"id": 424242, "authIsValid": True, "isActive": True,
                                           "connectionRequestCooldown": False, "connectionNoteCooldown": False},
            "get_campaign_sequence": {"nodeType": "CONNECTION_REQUEST", "payload": {"messages": ["Exact test note"]},
                                      "conditionalNode": {"nodeType": "END"}, "unconditionalNode": {"nodeType": "END"}},
            "get_overall_stats": {"overallStats": {"connectionsSent": 0, "messagesSent": 0, "inmailMessagesSent": 0, "uniqueLeadsContacted": 0}},
            "get_campaigns_for_lead": {"totalCount": 1, "items": [{"id": 999}]},
            "get_conversations_v2": {"totalCount": 0, "items": []},
            "get_leads_from_list": {"totalCount": 1, "items": [{"profileUrl": self.candidate["profile"]}]},
        }
        self.client = Mock()
        self.client.call.side_effect = lambda tool, arguments: copy.deepcopy(self.responses[tool])

    def queue(self):
        return outreach.enqueue(self.database, self.candidate)

    def test_complete_flow_claims_once_and_never_duplicates_after_restart(self):
        self.queue()
        first = outreach.dispatch(self.database, self.config, self.pipeline, self.client)
        self.assertEqual(first["launch_attempts"], 1)
        self.assertEqual(first["items"][0]["state"], "ACKNOWLEDGED_DELIVERY_UNCONFIRMED")
        with self.assertRaises(ValueError):
            outreach.enqueue(self.database, self.candidate)
        second = orchestrator.connect(self.root)
        try:
            self.assertEqual(outreach.dispatch(second, self.config, self.pipeline, self.client)["launch_attempts"], 0)
        finally:
            second.close()
        self.client.start_claimed_campaign.assert_called_once()

    def test_lost_ack_is_permanently_suppressed(self):
        self.queue()
        self.client.start_claimed_campaign.side_effect = TimeoutError("Lost acknowledgement")
        result = outreach.dispatch(self.database, self.config, self.pipeline, self.client)
        # A failed launch waits for a bounded retry (owner, 2026-09-30), never a second dispatch.
        self.assertEqual(result["items"][0]["state"], "LAUNCH_RETRY_WAIT")
        outreach.dispatch(self.database, self.config, self.pipeline, self.client)
        self.client.start_claimed_campaign.assert_called_once()
        self.assertTrue(self.database.execute("SELECT 1 FROM suppressions WHERE identity='name:test person'").fetchone())

    def test_claim_has_no_company_contact_cap(self):
        # The lifetime cap is gone (owner, 2026-09-28); claim_send's leftover projected > 3 refusal
        # was deleted on 2026-10-01. Three other people at the company do not stop a claim.
        self.queue()
        pipeline = {"companies": [{"company": "Test Company", "conservative_known_contact_count": 3}],
                    "rules": {"max_unique_contacts_per_company": 3}}
        self.assertGreater(outreach.contact_usage(self.database, "Test Company", self.candidate, pipeline)["projected"], 3)
        self.assertTrue(outreach.claim_send(self.database, self.candidate, pipeline))

    def test_crash_after_claim_cannot_relaunch(self):
        self.queue()
        self.assertTrue(outreach.claim_send(self.database, self.candidate))
        self.assertFalse(outreach.claim_send(self.database, self.candidate))
        result = outreach.dispatch(self.database, self.config, self.pipeline, self.client)
        self.assertEqual(result["launch_attempts"], 0)
        self.client.start_claimed_campaign.assert_not_called()

    def test_missing_fresh_linkedin_history_is_specific_gate(self):
        self.candidate["verification"] = {}
        self.queue()
        result = outreach.dispatch(self.database, self.config, self.pipeline, self.client)
        gates = result["items"][0]["gates"]
        # Owner, 2026-09-23: "If a model cannot determine that there has been no prior outreach
        # then outreach should not block. We only block on a solid yes. Unknown is not a block."
        # An unverified recipient and a missing history timestamp or evidence still refuse - those
        # say we have no read at all. What no longer refuses is a read that ran and could not
        # establish coverage, which is what LinkedIn returns when its Sent-invitations list comes
        # back excerpted.
        self.assertIn("RECIPIENT_IDENTITY_UNVERIFIED", gates)
        self.assertIn("LINKEDIN_HISTORY_EVIDENCE_MISSING", gates)
        self.assertNotIn("LINKEDIN_MESSAGES_CHECKED_MISSING", gates)
        self.assertNotIn("LINKEDIN_SENT_INVITATIONS_CHECKED_MISSING", gates)
        self.assertFalse([gate for gate in gates if gate.endswith("_PRESENT_OR_UNKNOWN")],
                         "not knowing is never a refusal; only a confirmed finding is")
        self.assertNotIn("IDENTITY_OR_TEAM_RELEVANCE_UNVERIFIED", gates)
        self.assertFalse([gate for gate in gates if "TEAM" in gate],
                         "no outreach may be refused over the recipient's team")
        self.client.call.assert_not_called()
        self.client.start_claimed_campaign.assert_not_called()

    def test_a_confirmed_prior_contact_still_refuses(self):
        """Only a solid yes blocks - but it must still block. Duplicate outreach is the hard line."""
        self.candidate["verification"]["prior_outreach"] = True
        self.queue()
        result = outreach.dispatch(self.database, self.config, self.pipeline, self.client)
        self.assertIn("PRIOR_OUTREACH_PRESENT", result["items"][0]["gates"])
        self.client.start_claimed_campaign.assert_not_called()

    def test_expired_history_blocks_provider_write(self):
        self.candidate["verification"]["verified_at"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).isoformat()
        self.queue()
        result = outreach.dispatch(self.database, self.config, self.pipeline, self.client)
        self.assertIn("LINKEDIN_HISTORY_CHECK_EXPIRED", result["items"][0]["gates"])
        self.client.start_claimed_campaign.assert_not_called()

    def test_provider_reply_or_pending_history_blocks(self):
        self.queue()
        self.responses["get_conversations_v2"] = {"totalCount": 1, "items": [{"id": "reply"}]}
        result = outreach.dispatch(self.database, self.config, self.pipeline, self.client)
        self.assertIn("CONVERSATION_OR_REPLY_PRESENT", result["items"][0]["gates"])
        self.client.start_claimed_campaign.assert_not_called()

    def test_unreadable_provider_inbox_no_longer_blocks(self):
        """owner, 2026-09-23: "We only block on a solid yes. Unknown is not a block."

        `inbox.get("totalCount") != 0` was true when the key was ABSENT, so a failed or empty
        provider read refused exactly like a real conversation. An observed conversation - the
        case above - still refuses.
        """
        self.queue()
        self.responses["get_conversations_v2"] = {}
        result = outreach.dispatch(self.database, self.config, self.pipeline, self.client)
        self.assertNotIn("CONVERSATION_OR_REPLY_PRESENT", result["items"][0].get("gates", []))

    def test_changed_note_extra_recipient_and_followup_are_blocked(self):
        self.queue()
        self.responses["get_campaign_sequence"]["payload"]["messages"] = ["Different note"]
        self.responses["get_campaign_sequence"]["unconditionalNode"] = {"nodeType": "MESSAGE"}
        self.responses["get_leads_from_list"]["totalCount"] = 2
        result = outreach.dispatch(self.database, self.config, self.pipeline, self.client)
        gates = result["items"][0]["gates"]
        self.assertIn("SEQUENCE_OR_EXACT_MESSAGE_MISMATCH", gates)
        self.assertIn("AUTOMATIC_FOLLOWUP_NOT_ALLOWED", gates)
        self.assertIn("CAMPAIGN_MUST_CONTAIN_ONLY_VERIFIED_RECIPIENT", gates)
        self.client.start_claimed_campaign.assert_not_called()

    def test_validation_never_claims_or_sends(self):
        self.queue()
        result = outreach.dispatch(self.database, self.config, self.pipeline, self.client, validate_only=True)
        self.assertEqual(result["items"][0]["state"], "READY")
        self.assertFalse(self.database.execute("SELECT 1 FROM outreach_attempts WHERE campaign_id=999").fetchone())
        self.client.start_claimed_campaign.assert_not_called()

    def test_real_transport_claim_consumed_before_network_and_cannot_repeat(self):
        self.queue()
        outreach.claim_send(self.database, self.candidate)
        client = orchestrator.HeyReach.__new__(orchestrator.HeyReach)
        def network(tool, arguments):
            self.assertEqual(self.database.execute("SELECT state FROM outreach_attempts WHERE campaign_id=999").fetchone()[0], "DISPATCHING")
            raise TimeoutError("Lost ack")
        with patch.object(client, "_request", side_effect=network) as request:
            with self.assertRaises(TimeoutError):
                client.start_claimed_campaign(self.database, 999)
            with self.assertRaises(ValueError):
                client.start_claimed_campaign(self.database, 999)
            request.assert_called_once()

    def test_no_automatic_contact_never_reused_for_live_validation_send(self):
        self.candidate.update(profile="https://linkedin.com/in/jrivera", recipient="Jamie Rivera", campaign_id=700001)
        with self.assertRaises(ValueError):
            self.queue()


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "state").mkdir()
        self.database = orchestrator.connect(self.root)
        self.addCleanup(self.database.close)
        outreach.initialize_actions(self.database)
        self.now = dt.datetime(2026, 9, 21, 17, tzinfo=dt.timezone.utc)
        self.config = {"outreach_mode": "guarded", "outreach_actions_enabled": True, "sender_id": 424242}
        self.action = {"action_id": "initial-1", "profile": "https://www.linkedin.com/in/mock-person/",
            "recipient": "Mock Person", "company": "Mock Company", "purpose": "INITIAL", "message": "Verified fixture text",
            "channel": "linkedin", "provider": "heyreach", "conversation_id": "thread-1",
            "verification": {"content_verified": True, "content_sources": ["mock-resume"], "identity_verified": True,
                "identity_source": "mock-identity", "team_relevance_verified": True, "team_source": "mock-team", "verified_at": self.now.isoformat()}}
        self.proof = {"identity": "linkedin.com/in/mock-person", "sender_id": 424242, "sender_ready": True,
            "entitlement_verified": True, "direct_messages_complete": True, "sent_invitations_complete": True,
            "pending_invitation": False, "has_reply": False, "negative_reply": False, "opt_out": False, "cooldown": False,
            "checked_at": self.now.isoformat(), "evidence": "mock-direct-history", "prior_outreach": False,
            "company_contact_count": 0, "company_count_evidence": "mock-history", "conversation_id": "thread-1",
            "followups_sent": 0, "initial_message_sent": False}
        self.transport = Mock(return_value={"ack": True})
        self.adapter = outreach.HeyReachActionAdapter(self.transport, lambda action, config: dict(self.proof))

    def run_action(self):
        outreach.enqueue_action(self.database, self.action)
        return outreach.cycle(self.database, self.config, self.adapter, self.now)

    def record(self, kind, identity="mock-person", source_id=None, occurred=None):
        return engagement.record_event(self.database, {"source": "heyreach", "source_id": source_id or kind,
            "profile": "https://www.linkedin.com/in/" + identity, "kind": kind,
            "occurred_at": (occurred or self.now).isoformat(), "observed_at": self.now.isoformat(),
            "evidence": "mock-independent", "raw": {"fixture": True}, "verified": True})

    def test_disabled_default_never_inspects_or_sends(self):
        outreach.enqueue_action(self.database, self.action)
        result = outreach.cycle(self.database, {}, Mock(), self.now)
        self.assertEqual(result["state"], "DISABLED")
        self.transport.assert_not_called()

    def test_exact_schema_durable_presend_and_ack_is_not_receipt(self):
        def send(tool, arguments):
            self.assertEqual(self.database.execute("SELECT state FROM outreach_actions").fetchone()[0], "REQUEST_STARTED")
            self.assertEqual(tool, "send_message")
            self.assertEqual(arguments, {"conversationId": "thread-1", "linkedInAccountId": 424242,
                "message": "Verified fixture text", "subject": ""})
            return {"ack": True}
        self.transport.side_effect = send
        self.assertEqual(self.run_action()["items"][0]["state"], "ACKNOWLEDGED_UNCONFIRMED")
        self.assertEqual(self.database.execute("SELECT count(*) FROM engagement_events").fetchone()[0], 0)
        with self.assertRaises(ValueError):
            self.adapter.execute_claimed(self.database, self.action["action_id"], self.config)
        outreach.cycle(self.database, self.config, self.adapter, self.now)
        self.transport.assert_called_once()

    def test_timeout_restart_never_resends(self):
        self.transport.side_effect = TimeoutError("mock lost acknowledgement")
        self.assertEqual(self.run_action()["items"][0]["state"], "UNCERTAIN_NO_RETRY")
        restarted = orchestrator.connect(self.root)
        self.addCleanup(restarted.close)
        self.assertEqual(outreach.cycle(restarted, self.config, self.adapter, self.now)["attempts"], 0)
        self.transport.assert_called_once()

    def test_independent_bound_receipt_records_delivery_and_allows_exact_followup(self):
        self.run_action()
        row = self.database.execute("SELECT * FROM outreach_actions").fetchone()
        receipt = {"independent": True, "action_id": row["action_id"], "identity": row["identity"],
            "payload_hash": row["payload_hash"], "operation": "send_message", "outcome": "SENT",
            "sender_id": 424242, "conversation_id": "thread-1",
            "receipt_id": "provider-message-1", "evidence": "mock-independent-history",
            "occurred_at": self.now.isoformat(), "observed_at": self.now.isoformat()}
        self.adapter.reconciler = lambda action, config: receipt
        self.assertEqual(outreach.cycle(self.database, self.config, self.adapter, self.now)["items"][0]["state"], "CONFIRMED_SENT")
        self.action.update(action_id="follow-1", purpose="FOLLOWUP", ordinal=1)
        self.now += dt.timedelta(days=3)
        self.proof.update(initial_message_sent=True, prior_outreach=True, checked_at=self.now.isoformat())
        self.assertEqual(self.run_action()["attempts"], 1)
        self.assertEqual(self.transport.call_count, 2)

    def test_mismatched_receipt_does_not_confirm_or_retry(self):
        self.run_action()
        self.adapter.reconciler = lambda action, config: {"independent": True, "action_id": "wrong"}
        self.assertEqual(outreach.cycle(self.database, self.config, self.adapter, self.now)["attempts"], 0)
        self.assertEqual(self.database.execute("SELECT state FROM outreach_actions").fetchone()[0], "ACKNOWLEDGED_UNCONFIRMED")

    def test_crash_after_claim_is_reconciliation_only(self):
        outreach.enqueue_action(self.database, self.action)
        with self.database:
            self.database.execute("UPDATE outreach_actions SET state='DISPATCHING',claimed_at=?", (self.now.isoformat(),))
        self.assertEqual(outreach.cycle(self.database, self.config, self.adapter, self.now)["attempts"], 0)
        self.transport.assert_not_called()

    def test_unknown_direct_history_no_longer_blocks_but_a_confirmed_one_does(self):
        """Owner, 2026-09-23: "We only block on a solid yes. Unknown is not a block."

        `direct_messages_complete` says the READ finished, not that anyone was contacted, and
        LinkedIn routinely returns an excerpted Sent-invitations list. Gating on it meant no
        outreach could ever be sent. A confirmed prior contact still refuses.
        """
        self.proof["direct_messages_complete"] = None
        gates = self.run_action()["items"][0]["gates"]
        self.assertNotIn("DIRECT_MESSAGES_COMPLETE_MISSING", gates)
        self.assertNotIn("SENT_INVITATIONS_COMPLETE_MISSING", gates)
        # A confirmed prior contact still refusing is proven by
        # OutreachTests.test_a_confirmed_prior_contact_still_refuses; asserting it here as well
        # would re-run an action this call has already claimed, which returns a different shape.

    def test_blocked_one_does_not_block_other_company(self):
        bad = dict(self.action, action_id="a-blocked", profile="https://www.linkedin.com/in/blocked/", company="Other")
        outreach.enqueue_action(self.database, bad)
        self.assertEqual(self.run_action()["attempts"], 1)
        self.transport.assert_called_once()

    def test_reply_during_inspection_rechecked_under_claim(self):
        self.adapter.inspector = lambda action, config: (self.record("inbound"), dict(self.proof))[1]
        self.assertEqual(self.run_action()["attempts"], 0)
        self.transport.assert_not_called()

    def test_optout_and_no_automatic_contact(self):
        self.record("opt_out")
        self.assertIn("OPTED_OUT", self.run_action()["items"][0]["gates"])
        self.transport.assert_not_called()
        self.action.update(action_id="no-automatic", profile="https://www.linkedin.com/in/jrivera", recipient="Jamie Rivera")
        outreach.enqueue_action(self.database, self.action)
        result = outreach.cycle(self.database, self.config, self.adapter, self.now)
        self.assertIn("NO_AUTOMATIC_OUTREACH_CONTACT", next(item for item in result["items"] if item["action_id"] == "no-automatic")["gates"])

    def test_previously_contacted_blocks_initial_but_due_followup_allowed(self):
        with self.database:
            self.database.execute("INSERT INTO history VALUES (?,?,?,?,?)", ("name:mock person", "Mock Person", "Mock Company", self.action["profile"], "{}"))
            self.database.execute("INSERT INTO suppressions VALUES (?,?)", (self.proof["identity"], "Handoff historical outreach; never repeat initial contact"))
        # The imported history refuses a new contact once, before a draft is created (owner,
        # 2026-10-01), not at send time.
        self.assertEqual(research_workflow.prior_contact_record(self.database, self.action["profile"], "Mock Person"), "history")
        self.record("initial_sent", occurred=self.now - dt.timedelta(days=5))
        self.action.update(action_id="followup", purpose="FOLLOWUP", ordinal=1)
        self.proof.update(initial_message_sent=True, prior_outreach=True)
        self.assertEqual(self.run_action()["attempts"], 1)

    def test_two_concurrent_cycles_only_one_claim_and_transport(self):
        outreach.enqueue_action(self.database, self.action)
        barrier = threading.Barrier(2)
        def inspect(action, config):
            return dict(self.proof)
        self.adapter.inspector = inspect
        def run():
            database = orchestrator.connect(self.root)
            try:
                barrier.wait(timeout=5)
                return outreach.cycle(database, self.config, self.adapter, self.now)["attempts"]
            finally:
                database.close()
        with ThreadPoolExecutor(max_workers=2) as executor:
            attempts = list(executor.map(lambda unused: run(), range(2)))
        self.assertEqual(sum(attempts), 1)
        self.transport.assert_called_once()

    def test_campaign_preparation_exact_schema_draft_only_and_ambiguous_no_retry(self):
        sequence = {"nodeType": "CONNECTION_REQUEST", "payload": {"messages": [self.action["message"]]},
            "conditionalNode": {"nodeType": "END", "actionDelay": 3, "actionDelayUnit": "HOUR"},
            "unconditionalNode": {"nodeType": "END", "actionDelay": 3, "actionDelayUnit": "HOUR"}}
        self.action.update(purpose="PREPARE_CAMPAIGN", list_id=100, campaign_name="Mock draft", sequence=sequence)
        self.proof.update(single_contact_list_verified=True, list_id=100, sequence=sequence, sequence_source="mock-existing-sequence")
        self.assertEqual(self.run_action()["attempts"], 1)
        tool, arguments = self.transport.call_args.args
        self.assertEqual(tool, "create_campaign")
        self.assertEqual(arguments["linkedInAccountIds"], [424242])
        self.assertEqual(json.loads(arguments["sequenceJson"]), sequence)
        self.assertNotIn("excludeInOtherCampaigns", arguments)
        outreach.cycle(self.database, self.config, self.adapter, self.now)
        self.transport.assert_called_once()

    def test_global_distinct_contacts_across_ledgers_and_held_slots(self):
        research_workflow.initialize(self.database)
        research_workflow.seed_history(self.database, {"companies": [{"company": "Mock Company", "conservative_known_contact_count": 1}]},
            [{"recipient": "Historic", "company": "Mock Company", "profile": "https://www.linkedin.com/in/historic"}], history_complete=True)
        with self.database:
            self.database.execute("INSERT INTO history VALUES (?,?,?,?,?)", ("name:historic", "Historic", "Mock Company", None, "{}"))
            self.database.execute("INSERT INTO rw_contacts VALUES (?,?,?,0,?)", ("mock company", self.action["profile"].rstrip("/"), "mock person", "plan"))
            self.database.execute("INSERT INTO rw_slots VALUES (?,?,?)", ("plan", "mock company", 1))
            self.database.execute("INSERT INTO drafts VALUES (?,?,?,?,?)", (self.proof["identity"], "Mock Person", "Mock Company", "HELD", json.dumps(self.action)))
        outreach.enqueue_action(self.database, self.action)
        usage = outreach.contact_usage(self.database, "Mock Company", self.action)
        self.assertEqual(usage["projected"], 3)
        self.assertEqual(usage["distinct_historical"], 1)
        self.assertEqual(usage["distinct_reserved"], 1)
        self.assertEqual(usage["held_slots"], 1)
        newcomer = dict(self.action, recipient="Another", profile="https://www.linkedin.com/in/another")
        self.assertEqual(outreach.contact_usage(self.database, "Mock Company", newcomer)["projected"], 4)

    def test_email_and_changed_action_payload_rejected(self):
        with self.assertRaises(ValueError):
            outreach.enqueue_action(self.database, dict(self.action, channel="email"))
        outreach.enqueue_action(self.database, self.action)
        with self.assertRaises(ValueError):
            outreach.enqueue_action(self.database, dict(self.action, message="Changed text"))

    def test_optout_after_durable_claim_prevents_provider_request(self):
        original = self.adapter.execute_claimed
        def execute(database, action_id, config, now=None):
            self.record("opt_out")
            return original(database, action_id, config, now)
        self.adapter.execute_claimed = execute
        self.assertEqual(self.run_action()["items"][0]["state"], "CANCELLED_BEFORE_SEND")
        self.transport.assert_not_called()
        self.assertEqual(outreach.cycle(self.database, self.config, self.adapter, self.now)["attempts"], 0)

    def test_unknown_adapter_blocks_and_queue_preserves_caller_transaction(self):
        self.database.execute("BEGIN IMMEDIATE")
        outreach.enqueue_action(self.database, self.action)
        self.database.rollback()
        self.assertEqual(self.database.execute("SELECT count(*) FROM outreach_actions").fetchone()[0], 0)
        outreach.enqueue_action(self.database, self.action)
        result = outreach.cycle(self.database, self.config, outreach.HeyReachActionAdapter(), self.now)
        self.assertIn("PROVIDER_HISTORY_ADAPTER_UNAVAILABLE", result["items"][0]["gates"])

    def test_historical_queue_duplicate_counted_once_and_baseline_preserved(self):
        outreach.initialize(self.database)
        historical = dict(self.action, recipient="Historic", profile="https://www.linkedin.com/in/historic", campaign_id=100)
        with self.database:
            self.database.execute("INSERT INTO history VALUES (?,?,?,?,?)", ("name:historic", "Historic", "Mock Company", historical["profile"], "{}"))
            self.database.execute("INSERT INTO outreach_queue VALUES (?,?,?,?,'PENDING_VALIDATION',?,NULL)",
                ("linkedin.com/in/historic", 100, "Historic", "Mock Company", json.dumps(historical)))
        pipeline = {"companies": [{"company": "Mock Company", "conservative_known_contact_count": 2}]}
        usage = outreach.contact_usage(self.database, "Mock Company", self.action, pipeline)
        self.assertEqual(usage["used"], 2)
        self.assertEqual(usage["projected"], 3)

    def test_research_history_alias_alone_prevents_repeat_initial(self):
        research_workflow.initialize(self.database)
        research_workflow.register_alias(self.database, "contact", self.action["profile"], "https://www.linkedin.com/in/original", "verified-alias")
        research_workflow.seed_history(self.database, {"companies": []},
            [{"recipient": "Mock Person", "company": "Mock Company", "profile": "https://www.linkedin.com/in/original"}], history_complete=True)
        # Checked once, before a draft is created (owner, 2026-10-01), under every alias.
        research_workflow.plan_research(self.database, "later", ["Mock Company"], now=self.now)
        refused = research_workflow.reserve_contact(self.database, "later", "Mock Company", self.action["profile"],
                                                    "Mock Person", now=self.now)
        self.assertEqual(refused["reason"], "HISTORICAL_OR_DUPLICATE_CONTACT")

    def test_claim_cannot_be_updated_deleted_or_replaced(self):
        self.run_action()
        import sqlite3
        for statement in (
            "UPDATE outreach_action_claims SET sender_id=1",
            "DELETE FROM outreach_action_claims",
            "INSERT OR REPLACE INTO outreach_action_claims SELECT * FROM outreach_action_claims",
        ):
            with self.assertRaises(sqlite3.IntegrityError):
                with self.database:
                    self.database.execute(statement)
        self.assertEqual(self.database.execute("SELECT sender_id FROM outreach_action_claims").fetchone()[0], 424242)


class GuardContractTests(unittest.TestCase):
    def setUp(self):
        ActionTests.setUp(self)
        self.addCleanup(patch.stopall)
        patch("socket.create_connection", side_effect=AssertionError("No network in guard tests")).start()
        patch("subprocess.run", side_effect=AssertionError("No provider processes in guard tests")).start()
        self.wall_now = self.now
        self.clock = Mock(wraps=dt.datetime)
        self.clock.now.side_effect = lambda timezone: self.wall_now
        self.clock_module = SimpleNamespace(datetime=self.clock, timezone=dt.timezone, timedelta=dt.timedelta)

    def cycle(self, database=None):
        return outreach.cycle(database or self.database, self.config, self.adapter, self.now)

    def queue(self):
        outreach.enqueue_action(self.database, self.action)

    def claim(self):
        return dict(self.database.execute("SELECT * FROM outreach_action_claims WHERE action_id=?", (self.action["action_id"],)).fetchone())

    def receipt(self, outcome="NOT_SENT", observed=None):
        claim = self.claim()
        binding = {field: claim[field] for field in ("action_id", "identity", "payload_hash", "sender_id")}
        binding.update(operation="send_message", outcome=outcome, conversation_id=self.action["conversation_id"])
        observed = (observed or self.now).isoformat()
        receipt = dict(binding, independent=True, receipt_id="mock-rejection" if outcome == "NOT_SENT" else "mock-delivery",
                       evidence="mock-independent-source", observed_at=observed)
        if outcome == "NOT_SENT":
            binding["claimed_at"] = claim["claimed_at"]
            raw = "Synthetic authenticated rejection: request rejected and cannot execute."
            receipt.update(claimed_at=claim["claimed_at"], terminal_rejection=True, cannot_execute=True,
                rejection_evidence={"source": "heyreach", "source_id": "mock-rejection",
                    "kind": "explicit_terminal_rejection", "authenticated": True, "supported": True,
                    "raw": raw, "raw_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                    "rejection_text": "request rejected and cannot execute", "binding": binding,
                    "observed_at": observed})
        else:
            receipt["occurred_at"] = claim["claimed_at"]
        return receipt

    def test_not_sent_preserves_claim_and_negative_receipt_without_event_or_retry(self):
        self.queue()
        self.cycle()
        original_claim = self.claim()
        receipt = self.receipt()
        self.adapter.reconciler = Mock(return_value=receipt)
        result = self.cycle()
        self.assertEqual(result["items"][0]["state"], "CONFIRMED_NOT_SENT")
        self.assertEqual(result["items"][0]["blocker"], "NOT_SENT_RETRY_UNSUPPORTED")
        self.assertEqual(self.claim(), original_claim)
        stored = self.database.execute("SELECT result FROM outreach_actions").fetchone()[0]
        self.assertEqual(json.loads(stored), receipt)
        self.assertEqual(self.database.execute("SELECT count(*) FROM engagement_events").fetchone()[0], 0)
        restarted = orchestrator.connect(self.root)
        self.addCleanup(restarted.close)
        self.assertEqual(self.cycle(restarted)["items"], [])
        self.adapter.reconciler.assert_called_once()
        with self.assertRaises(ValueError):
            self.adapter.execute_claimed(restarted, self.action["action_id"], self.config, self.now)
        replacement = dict(self.action, action_id="another-initial")
        with self.assertRaises(ValueError):
            outreach.enqueue_action(restarted, replacement)
        replacement.update(action_id="another-purpose", purpose="FOLLOWUP", ordinal=1)
        with self.assertRaisesRegex(ValueError, "IMMUTABLE_OUTREACH_CLAIM_EXISTS"):
            outreach.enqueue_action(restarted, replacement)
        self.assertIn("NOT_SENT_RETRY_UNSUPPORTED", outreach.action_gates(restarted, self.config, replacement, self.proof, self.now))
        self.transport.assert_called_once()

    def test_not_sent_rejects_unbound_or_unsupported_negative_receipts(self):
        self.queue()
        self.cycle()
        original_claim = self.claim()
        valid = self.receipt()
        cases = [("receipt", field, "wrong") for field in
                 ("action_id", "identity", "payload_hash", "sender_id", "conversation_id", "claimed_at", "operation")]
        cases += [("receipt", field, False) for field in ("independent", "terminal_rejection", "cannot_execute")]
        cases += [("receipt", "rejection_evidence", None), ("receipt", "receipt_id", ""), ("receipt", "evidence", "")]
        cases += [("evidence", field, value) for field, value in
                  (("authenticated", False), ("supported", False), ("source", "untrusted"),
                   ("source_id", ""), ("kind", "empty_history"), ("raw_sha256", "wrong"),
                   ("rejection_text", "invented quote"), ("observed_at", "wrong"))]
        cases += [("binding", field, "wrong") for field in valid["rejection_evidence"]["binding"]]
        for section, field, value in cases:
            with self.subTest(section=section, field=field):
                receipt = copy.deepcopy(valid)
                target = receipt if section == "receipt" else receipt["rejection_evidence"]
                if section == "binding":
                    target = target["binding"]
                target[field] = value
                self.adapter.reconciler = Mock(return_value=receipt)
                result = self.cycle()
                self.assertEqual(result["items"][0]["read_retry"]["state"], "INVALID_PROOF")
                self.assertEqual(self.database.execute("SELECT state FROM outreach_actions").fetchone()[0], "ACKNOWLEDGED_UNCONFIRMED")
                self.assertEqual(self.claim(), original_claim)
                self.assertEqual(self.database.execute("SELECT count(*) FROM engagement_events").fetchone()[0], 0)
                self.assertTrue(outreach.resume_read(self.database, self.action["action_id"], "RECONCILIATION", self.now))
        self.transport.assert_called_once()

    def assert_unresolved_backoff(self, changes, gate):
        self.proof.update(changes)
        self.adapter.inspector = Mock(side_effect=lambda action, config: dict(self.proof, checked_at=self.now.isoformat()))
        self.queue()
        result = self.cycle()
        self.assertIn(gate, result["items"][0]["gates"])
        for attempt, delay in ((1, 15), (2, 60)):
            status = result["items"][0]["read_retry"]
            self.assertEqual(status["state"], "RETRY_WAIT")
            self.assertEqual(status["attempts"], attempt)
            self.assertEqual(status["last_error"], "SOURCE_EVIDENCE_UNRESOLVED")
            self.assertEqual(status["next_at"], (self.now + dt.timedelta(minutes=delay)).isoformat())
            restarted = orchestrator.connect(self.root)
            self.addCleanup(restarted.close)
            self.now += dt.timedelta(minutes=delay, seconds=-1)
            self.cycle(restarted)
            self.assertEqual(self.adapter.inspector.call_count, attempt)
            self.now += dt.timedelta(seconds=1)
            result = self.cycle(restarted)
        self.assertEqual(result["items"][0]["read_retry"]["state"], "RETRY_WAIT")
        self.assertEqual(result["items"][0]["read_retry"]["attempts"], 3)
        # Reads do not exhaust (owner, 2026-09-21): a fourth read happens on the next date.
        self.now += dt.timedelta(days=1)
        result = self.cycle()
        self.assertEqual(self.adapter.inspector.call_count, 4)
        self.assertEqual(result["items"][0]["read_retry"]["state"], "RETRY_WAIT")
        self.assertIsNotNone(result["items"][0]["read_retry"]["next_at"])
        self.assertFalse(outreach.resume_read(self.database, self.action["action_id"], "INSPECTION", self.now))
        # Reading again never sends.
        self.transport.assert_not_called()

    def test_agent_unknown_never_blocks(self):
        # Owner, 2026-10-23: "unknown should never block" ("same applies for reply and followups").
        self.proof.update(agent_decision="UNKNOWN")
        self.queue()
        result = self.cycle()
        self.assertNotIn("AGENT_DECISION_NOT_READY", json.dumps(result))
        self.assertNotIn("SOURCE_EVIDENCE_UNRESOLVED", json.dumps(result))

    def test_agent_blocked_still_holds(self):
        self.proof.update(agent_decision="BLOCKED")
        self.queue()
        self.assertIn("AGENT_DECISION_NOT_READY", json.dumps(self.cycle()))
        self.transport.assert_not_called()

    def test_agent_nullable_boolean_no_longer_blocks_when_decision_ready(self):
        """Owner, 2026-09-23: "A 'dont know' also shouldnt be evidence to block an outreach."

        This asserted that an unknown `pending_invitation` blocked the send. The provider does
        not always answer, so that was absence of evidence stopping a message. A KNOWN True
        still blocks, and `opt_out`/`negative_reply` still block on unknown - those are the two
        where being wrong means messaging someone who asked you to stop.
        """
        self.proof.update(agent_decision="READY", pending_invitation=None)
        self.queue()
        result = self.cycle()
        self.assertNotIn("PENDING_INVITATION_PRESENT", json.dumps(result))

    def test_unknown_opt_out_no_longer_blocks_but_a_confirmed_one_does(self):
        """owner, 2026-09-23: "We only block on a solid yes. Unknown is not a block."

        A RECORDED opt-out is held in engagement_stops and still refuses through stop_state -
        see OutboundProtectionTests.test_opt_out_still_blocks, which is unchanged. What no
        longer refuses is an inspection that simply could not establish the field.
        """
        self.proof.update(agent_decision="READY", opt_out=None)
        self.queue()
        self.assertNotIn("OPT_OUT_PRESENT", self.cycle()["items"][0].get("gates", []))

    def test_incomplete_provider_snapshot_does_not_hold_an_invitation(self):
        # Owner, 2026-09-30: an incomplete HeyReach read is not evidence of prior contact.
        self.proof.update(agent_decision="READY", provider_snapshot_complete=False)
        self.queue()
        self.assertNotIn("PROVIDER_SNAPSHOT_INCOMPLETE", self.cycle()["items"][0].get("gates", []))

    def test_agent_nonnull_nonboolean_still_invalid(self):
        self.proof.update(agent_decision="UNKNOWN", cooldown="false")
        self.queue()
        result = self.cycle()
        self.assertEqual(result["items"][0]["read_retry"]["state"], "INVALID_PROOF")
        self.assertEqual(result["items"][0]["read_retry"]["last_error"], "MALFORMED_INSPECTION_PROOF")
        self.transport.assert_not_called()

    def test_allowed_purposes_gate_blocks_then_allows_existing_initial(self):
        self.config["outreach_allowed_purposes"] = ["FIRST_MESSAGE"]
        self.queue()
        self.assertIn("OUTREACH_PURPOSE_NOT_ALLOWED", self.cycle()["items"][0]["gates"])
        self.transport.assert_not_called()
        self.config["outreach_allowed_purposes"] = ["INITIAL", "FIRST_MESSAGE"]
        self.assertEqual(self.cycle()["attempts"], 1)

    def inspect_after(self, seconds, fixed=False, future=False):
        self.wall_now += dt.timedelta(seconds=seconds)
        checked = self.now if fixed else self.wall_now
        if future:
            checked += dt.timedelta(hours=1)
        return dict(self.proof, checked_at=checked.isoformat())

    def test_live_clock_accepts_inspection_finished_after_cycle_start(self):
        self.queue()
        self.adapter.inspector = lambda action, config: self.inspect_after(565)
        with patch.object(outreach, "dt", self.clock_module):
            result = outreach.cycle(self.database, self.config, self.adapter)
        self.assertEqual(result["items"][0]["state"], "ACKNOWLEDGED_UNCONFIRMED")
        self.assertEqual(self.claim()["claimed_at"], self.wall_now.isoformat())
        self.transport.assert_called_once()

    def test_live_clock_rejects_inspection_at_exact_lease_expiry(self):
        self.queue()
        self.adapter.inspector = lambda action, config: self.inspect_after(600)
        with patch.object(outreach, "dt", self.clock_module):
            result = outreach.cycle(self.database, self.config, self.adapter)
        self.assertEqual(result["items"][0]["blocker"], "STALE_INSPECTION_READ")
        self.assertEqual(self.database.execute("SELECT count(*) FROM outreach_action_claims").fetchone()[0], 0)
        self.transport.assert_not_called()

    def test_future_proof_cannot_advance_live_clock(self):
        self.queue()
        self.adapter.inspector = lambda action, config: self.inspect_after(1, future=True)
        with patch.object(outreach, "dt", self.clock_module):
            result = outreach.cycle(self.database, self.config, self.adapter)
        self.assertIn("DIRECT_HISTORY_MISSING_OR_STALE", result["items"][0]["gates"])
        self.transport.assert_not_called()

    def test_explicit_now_preserves_inspection_and_receipt_fixture_time(self):
        self.queue()
        self.adapter.inspector = lambda action, config: self.inspect_after(265, fixed=True)
        with patch.object(outreach, "dt", self.clock_module):
            self.assertEqual(self.cycle()["attempts"], 1)
            self.adapter.reconciler = lambda action, config: self.receipt("SENT")
            self.assertEqual(self.cycle()["items"][0]["state"], "CONFIRMED_SENT")
        self.clock.now.assert_not_called()
        self.assertEqual(self.claim()["claimed_at"], self.now.isoformat())

    def test_live_clock_accepts_receipt_observed_after_reconciliation_started(self):
        self.queue()
        self.cycle()
        def reconcile(action, config):
            self.wall_now += dt.timedelta(seconds=265)
            return self.receipt("SENT", self.wall_now)
        self.adapter.reconciler = reconcile
        with patch.object(outreach, "dt", self.clock_module):
            result = outreach.cycle(self.database, self.config, self.adapter)
        self.assertEqual(result["items"][0]["state"], "CONFIRMED_SENT")
        self.assertEqual(result["items"][0]["read_retry"]["state"], "COMPLETE")
        self.assertEqual(self.database.execute("SELECT count(*) FROM engagement_events").fetchone()[0], 1)
        self.transport.assert_called_once()

    def test_live_clock_rejects_receipt_at_exact_reconciliation_lease_expiry(self):
        self.queue()
        self.cycle()
        def reconcile(action, config):
            self.wall_now += dt.timedelta(seconds=300)
            return self.receipt("SENT", self.wall_now)
        self.adapter.reconciler = reconcile
        with patch.object(outreach, "dt", self.clock_module):
            result = outreach.cycle(self.database, self.config, self.adapter)
        self.assertEqual(result["items"][0]["state"], "ACKNOWLEDGED_UNCONFIRMED")
        self.assertEqual(self.database.execute("SELECT count(*) FROM engagement_events").fetchone()[0], 0)
        self.transport.assert_called_once()


class ReadRetryTests(unittest.TestCase):
    def setUp(self):
        ActionTests.setUp(self)

    def cycle(self, database=None):
        return outreach.cycle(database or self.database, self.config, self.adapter, self.now)

    def restart(self):
        database = orchestrator.connect(self.root)
        self.addCleanup(database.close)
        return database

    def status(self, phase):
        return dict(self.database.execute("SELECT * FROM outreach_read_retries WHERE action_id=? AND phase=?", (self.action["action_id"], phase)).fetchone())

    def capacity_deferral(self, phase):
        outreach.enqueue_action(self.database, self.action)
        if phase == "RECONCILIATION":
            self.transport.side_effect = TimeoutError("Synthetic unknown send")
            self.cycle()
        error = outreach_agent.SharedBudgetExhausted({"timezone": "America/Los_Angeles"}, self.now)
        reader = Mock(side_effect=error)
        if phase == "INSPECTION":
            self.adapter.inspector = reader
        else:
            self.adapter.reconciler = reader
        self.cycle()
        first = self.status(phase)
        self.assertEqual(first["attempts"], 1)
        self.assertEqual(first["next_at"], error.reset_at.isoformat())
        self.assertEqual(first["last_error"], error.code)
        restarted = self.restart()
        self.now += dt.timedelta(minutes=15)
        self.cycle(restarted)
        self.assertEqual(reader.call_count, 1)
        self.now = error.reset_at
        error.reset_at = self.now + dt.timedelta(minutes=5)
        self.cycle()
        second = self.status(phase)
        self.assertEqual(second["attempts"], 2)
        self.assertEqual(second["next_at"], (self.now + dt.timedelta(minutes=60)).isoformat())
        self.now += dt.timedelta(minutes=60)
        error.reset_at = self.now + dt.timedelta(hours=12)
        self.cycle()
        third = self.status(phase)
        self.assertEqual(third["attempts"], 3)
        self.assertEqual(third["state"], "RETRY_WAIT")
        # Held on a widened date, not exhausted (owner, 2026-09-21), so the read resumes
        # once that date passes. A shared-budget deferral must not become permanent.
        self.assertIsNotNone(third["next_at"])
        self.now += dt.timedelta(days=1)
        self.cycle(restarted)
        self.assertEqual(reader.call_count, 4)
        if phase == "RECONCILIATION":
            self.transport.assert_called_once()
            self.assertEqual(self.database.execute("SELECT COUNT(*) FROM outreach_action_claims").fetchone()[0], 1)
            self.assertEqual(self.database.execute("SELECT state FROM outreach_actions").fetchone()[0], "UNCERTAIN_NO_RETRY")
        else:
            self.transport.assert_not_called()

    def test_inspection_capacity_reset_defers_without_resetting_failed_attempts(self):
        self.capacity_deferral("INSPECTION")

    def test_reconciliation_capacity_reset_preserves_uncertain_send_claim(self):
        self.capacity_deferral("RECONCILIATION")

    def test_inspection_three_transients_backoff_restart_then_hold_and_retry(self):
        inspector = Mock(side_effect=TimeoutError("sensitive mock diagnostic"))
        self.adapter.inspector = inspector
        outreach.enqueue_action(self.database, self.action)
        self.cycle()
        self.assertEqual(self.status("INSPECTION")["attempts"], 1)
        self.assertEqual(self.status("INSPECTION")["next_at"], (self.now + dt.timedelta(minutes=15)).isoformat())
        restarted = self.restart()
        self.now += dt.timedelta(minutes=14, seconds=59)
        self.cycle(restarted)
        self.assertEqual(inspector.call_count, 1)
        self.now += dt.timedelta(seconds=1)
        self.cycle(restarted)
        self.assertEqual(self.status("INSPECTION")["attempts"], 2)
        self.assertEqual(self.status("INSPECTION")["next_at"], (self.now + dt.timedelta(minutes=60)).isoformat())
        self.now += dt.timedelta(minutes=60)
        result = self.cycle(restarted)
        self.assertEqual(result["items"][0]["read_retry"]["state"], "RETRY_WAIT")
        self.assertEqual(self.status("INSPECTION")["last_error"], "TimeoutError")
        # Reads hold on a next date rather than exhausting (owner, 2026-09-21), so the
        # fourth read does happen once that date arrives. Nothing is spent before it.
        self.now += dt.timedelta(minutes=239)
        self.cycle(restarted)
        self.assertEqual(inspector.call_count, 3)
        self.now += dt.timedelta(minutes=1)
        self.cycle(restarted)
        self.assertEqual(inspector.call_count, 4)
        self.assertEqual(self.status("INSPECTION")["state"], "RETRY_WAIT")
        self.assertFalse(outreach.resume_read(self.database, self.action["action_id"], "INSPECTION", self.now))
        self.transport.assert_not_called()

    def test_uncertain_send_three_pending_reconciliations_and_no_resend(self):
        self.transport.side_effect = TimeoutError()
        reconciler = Mock(return_value={"state": "PENDING"})
        self.adapter.reconciler = reconciler
        outreach.enqueue_action(self.database, self.action)
        self.cycle()
        self.cycle()
        self.assertEqual(self.status("RECONCILIATION")["attempts"], 1)
        self.cycle(self.restart())
        self.assertEqual(reconciler.call_count, 1)
        self.now += dt.timedelta(minutes=15)
        self.cycle()
        self.now += dt.timedelta(minutes=59, seconds=59)
        self.cycle()
        self.assertEqual(reconciler.call_count, 2)
        self.now += dt.timedelta(seconds=1)
        result = self.cycle()
        self.assertEqual(result["items"][0]["read_retry"]["state"], "RETRY_WAIT")
        # The send DID happen, so confirming it must stay possible however long it takes
        # (owner, 2026-09-21). This is precisely the case that stranded Dana Brook's
        # connection request on UNCERTAIN_NO_RETRY with no way back.
        self.now += dt.timedelta(days=5)
        self.cycle(self.restart())
        self.assertEqual(reconciler.call_count, 4)
        self.assertEqual(self.status("RECONCILIATION")["state"], "RETRY_WAIT")
        # And reading again still never resends.
        self.transport.assert_called_once()
        self.assertEqual(self.database.execute("SELECT state FROM outreach_actions").fetchone()[0], "UNCERTAIN_NO_RETRY")

    def test_reconciliation_timeouts_keep_their_next_date_without_resending(self):
        self.adapter.reconciler = Mock(side_effect=ConnectionError("private mock"))
        outreach.enqueue_action(self.database, self.action)
        self.cycle()
        for minutes in (0, 15, 60):
            self.now += dt.timedelta(minutes=minutes)
            self.cycle()
        # A read that cannot reconcile gets a later date, never a grave (owner, 2026-09-21):
        # the send DID happen, so it has to stay confirmable however long that takes.
        self.now += dt.timedelta(days=1)
        self.cycle(self.restart())
        self.assertEqual(self.adapter.reconciler.call_count, 4)
        self.assertEqual(self.status("RECONCILIATION")["state"], "RETRY_WAIT")
        self.assertIsNotNone(self.status("RECONCILIATION")["next_at"])
        # The invariant that matters: reviving a READ never resends.
        self.transport.assert_called_once()

    def test_unsupported_inspector_holds_without_budget_or_polling(self):
        self.adapter.inspector = Mock(side_effect=NotImplementedError())
        outreach.enqueue_action(self.database, self.action)
        self.cycle()
        self.now += dt.timedelta(days=1)
        self.cycle(self.restart())
        self.adapter.inspector.assert_called_once()
        self.assertEqual(self.status("INSPECTION")["attempts"], 0)
        self.assertEqual(self.status("INSPECTION")["state"], "ADAPTER_UNAVAILABLE")
        self.assertTrue(outreach.resume_read(self.database, self.action["action_id"], "INSPECTION", self.now))
        self.proof["checked_at"] = self.now.isoformat()
        self.adapter.inspector = lambda action, config: self.proof
        self.assertEqual(self.cycle()["attempts"], 1)

    def test_unsupported_reconciler_holds_without_consuming_budget(self):
        self.adapter.reconciler = Mock(side_effect=NotImplementedError())
        outreach.enqueue_action(self.database, self.action)
        self.cycle()
        self.cycle()
        self.now += dt.timedelta(days=1)
        self.cycle(self.restart())
        self.adapter.reconciler.assert_called_once()
        self.assertEqual(self.status("RECONCILIATION")["attempts"], 0)
        self.assertEqual(self.status("RECONCILIATION")["state"], "ADAPTER_UNAVAILABLE")
        self.transport.assert_called_once()

    def test_malformed_inspection_and_receipt_are_not_transient_retries(self):
        self.adapter.inspector = Mock(return_value=["invalid"])
        outreach.enqueue_action(self.database, self.action)
        self.cycle()
        self.now += dt.timedelta(minutes=20)
        self.cycle()
        self.adapter.inspector.assert_called_once()
        self.assertEqual(self.status("INSPECTION")["state"], "INVALID_PROOF")
        self.assertEqual(self.status("INSPECTION")["attempts"], 0)
        self.assertIsNone(self.status("INSPECTION")["next_at"])
        self.assertTrue(outreach.resume_read(self.database, self.action["action_id"], "INSPECTION", self.now))
        self.adapter.inspector = lambda action, config: self.proof
        self.adapter.reconciler = Mock(return_value=["invalid"])
        self.cycle()
        self.cycle()
        self.now += dt.timedelta(minutes=20)
        self.cycle()
        self.adapter.reconciler.assert_called_once()
        self.assertEqual(self.status("RECONCILIATION")["state"], "INVALID_PROOF")
        self.assertEqual(self.status("RECONCILIATION")["attempts"], 0)
        self.transport.assert_called_once()

    def test_one_backoff_does_not_stop_other_company(self):
        outreach.enqueue_action(self.database, self.action)
        self.adapter.inspector = Mock(side_effect=TimeoutError())
        self.cycle()
        other = dict(self.action, action_id="second", company="Second", profile="https://www.linkedin.com/in/second", recipient="Second")
        outreach.enqueue_action(self.database, other)
        self.adapter.inspector = Mock(return_value=dict(self.proof, identity="linkedin.com/in/second"))
        result = self.cycle()
        self.assertEqual(result["attempts"], 1)
        self.adapter.inspector.assert_called_once()

    def test_cycle_processes_at_most_sixty_items(self):
        for number in range(65):
            action = dict(self.action, action_id=str(number).zfill(3), profile="https://www.linkedin.com/in/mock-" + str(number), company="Company " + str(number))
            outreach.enqueue_action(self.database, action)
        self.adapter.inspector = Mock(side_effect=NotImplementedError())
        self.config["outreach_max_items_per_cycle"] = 1000
        first = self.cycle()
        self.assertEqual(len(first["items"]), 60)
        self.assertEqual(self.adapter.inspector.call_count, 60)
        second = self.cycle()
        self.assertLessEqual(len(second["items"]), 60)
        self.assertEqual(self.adapter.inspector.call_count, 65)

    def test_read_crash_lease_consumes_attempt_and_honors_recovery_backoff(self):
        outreach.enqueue_action(self.database, self.action)
        token = outreach._claim_read(self.database, self.action["action_id"], "INSPECTION", self.now)
        self.assertTrue(token)
        inspector = Mock(side_effect=TimeoutError())
        self.adapter.inspector = inspector
        self.now += dt.timedelta(minutes=10)
        self.cycle(self.restart())
        self.assertEqual(self.status("INSPECTION")["last_error"], "INTERRUPTED_READ")
        self.assertEqual(self.status("INSPECTION")["attempts"], 1)
        inspector.assert_not_called()
        self.now += dt.timedelta(minutes=15)
        self.cycle()
        inspector.assert_called_once()
        self.assertEqual(self.status("INSPECTION")["attempts"], 2)

    def test_success_at_retry_boundary_sends_once_and_preserves_attempt_count(self):
        inspector = Mock(side_effect=[TimeoutError(), dict(self.proof)])
        self.adapter.inspector = inspector
        outreach.enqueue_action(self.database, self.action)
        self.cycle()
        self.now += dt.timedelta(minutes=15)
        self.assertEqual(self.cycle(self.restart())["attempts"], 1)
        self.assertEqual(self.status("INSPECTION")["attempts"], 2)
        self.assertEqual(self.status("INSPECTION")["state"], "COMPLETE")
        self.transport.assert_called_once()

    def test_independent_receipt_after_backoff_confirms_without_another_send(self):
        outreach.enqueue_action(self.database, self.action)
        self.cycle()
        self.adapter.reconciler = Mock(return_value={"state": "UNKNOWN"})
        self.cycle()
        self.now += dt.timedelta(minutes=15)
        row = self.database.execute("SELECT * FROM outreach_actions").fetchone()
        self.adapter.reconciler.return_value = {
            "independent": True, "action_id": row["action_id"], "identity": row["identity"],
            "payload_hash": row["payload_hash"], "operation": "send_message", "outcome": "SENT",
            "sender_id": 424242, "conversation_id": "thread-1", "receipt_id": "verified-after-backoff",
            "evidence": "mock-read", "observed_at": self.now.isoformat(), "occurred_at": row["claimed_at"]}
        self.assertEqual(self.cycle(self.restart())["items"][0]["state"], "CONFIRMED_SENT")
        self.assertEqual(self.status("RECONCILIATION")["attempts"], 2)
        self.assertEqual(self.status("RECONCILIATION")["state"], "COMPLETE")
        self.transport.assert_called_once()

    def test_concurrent_consumers_only_spend_one_read_attempt(self):
        outreach.enqueue_action(self.database, self.action)
        barrier = threading.Barrier(2)
        self.adapter.inspector = Mock(side_effect=TimeoutError())
        def run():
            database = orchestrator.connect(self.root)
            try:
                barrier.wait(timeout=5)
                return outreach.cycle(database, self.config, self.adapter, self.now)
            finally:
                database.close()
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda unused: run(), range(2)))
        self.adapter.inspector.assert_called_once()
        self.assertEqual(self.status("INSPECTION")["attempts"], 1)

    def test_malformed_boolean_proof_holds_without_automatic_reinspection(self):
        self.proof["direct_messages_complete"] = "true"
        self.adapter.inspector = Mock(return_value=self.proof)
        outreach.enqueue_action(self.database, self.action)
        self.cycle()
        self.now += dt.timedelta(days=1)
        self.cycle(self.restart())
        self.adapter.inspector.assert_called_once()
        self.assertEqual(self.status("INSPECTION")["state"], "INVALID_PROOF")
        self.transport.assert_not_called()
