import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import copy
import datetime as dt
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import engagement
import outreach
import outreach_provider
import reply_execution
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


class ReplySendGuardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "guard.sqlite3"
        self.database = self.connect()
        outreach.initialize_actions(self.database)
        self.database.execute("CREATE TABLE suppressions(identity TEXT PRIMARY KEY, reason TEXT NOT NULL)")
        self.database.commit()
        self.now = dt.datetime.now(dt.timezone.utc)
        self.config = {"sender_id": 901, "sender_profile": "https://www.linkedin.com/in/mock-sender",
                       "outreach_actions_enabled": True, "outreach_mode": "guarded",
                       "outreach_allowed_purposes": ["REPLY"], "reply_execution": {"enabled": True}}
        self.action = {"action_id": "mock-reply", "profile": "https://www.linkedin.com/in/mock-recipient",
                       "recipient": "Mock Recipient", "company": "Mock Company", "purpose": "REPLY",
                       "provider": "heyreach", "channel": "linkedin", "sender_id": 901,
                       "conversation_id": "mock-room", "message": "Thanks for clarifying.",
                       "reply_task_id": "mock-task", "reply_source_key": "mock-source-key",
                       "reply_source_sha256": "a" * 64, "reply_source": "heyreach",
                       "reply_source_id": "mock-inbound", "reply_inbound_at": self.now.isoformat(),
                       "verification": {"verified_at": self.now.isoformat(), "content_sources": ["mock-source-key"],
                                        "identity_source": "mock-source-key"}}
        self.proof = {field: self.action[field] for field in
                      ("reply_source_key", "reply_source_sha256", "reply_source_id", "reply_inbound_at")}
        self.proof.update(identity="linkedin.com/in/mock-recipient", sender_id=901, sender_ready=True,
                          reply_transport_supported=True, provider_restriction_observed=False,
                          quota_remaining=None, quota_status="UNKNOWN_PROVIDER_ENFORCED",
                          negative_reply=False, opt_out=False,
                          evidence="mock-authenticated-read", checked_at=self.now.isoformat(),
                          agent_decision="READY", provider_snapshot_complete=True,
                          conversation_id="mock-room", reply_latest_inbound=True,
                          reply_already_answered=False, reply_context_verified=True)
        self.binding = Mock(return_value=[])
        self.thread_binding = Mock(return_value=[])
        module_patch = patch.dict(sys.modules, {"reply_execution": SimpleNamespace(
            binding_gates=self.binding, thread_gates=self.thread_binding, stop_gates=reply_execution.stop_gates)})
        module_patch.start()
        self.addCleanup(module_patch.stop)
        self.client = Mock(spec=["call", "_request"])
        self.client.call.return_value = {"mock": "fresh room"}
        self.client._request.return_value = {"ack": True}
        self.provider = outreach_provider.Provider(self.database, self.config, self.client)
        self.before_send = None
        self.receipt = None
        self.adapter = outreach.HeyReachActionAdapter(
            self.transport, lambda action, config: copy.deepcopy(self.proof),
            lambda action, config: self.receipt)
        self.record_inbound()

    def connect(self):
        database = sqlite3.connect(self.path)
        database.row_factory = sqlite3.Row
        self.addCleanup(database.close)
        return database

    def record_inbound(self, source_id="mock-inbound", **fields):
        event = {"source": "heyreach", "source_id": source_id, "profile": self.action["profile"],
                 "kind": "inbound", "occurred_at": self.now.isoformat(), "observed_at": self.now.isoformat(),
                 "evidence": "mock-authenticated-read", "raw": {}}
        event.update(fields)
        engagement.record_event(self.database, event)

    def transport(self, operation, arguments):
        self.assertEqual(operation, "send_message")
        self.assertEqual(arguments["message"], self.action["message"])
        if self.before_send:
            self.before_send()
        return self.provider.send_claimed(self.action["action_id"], self.config)

    def cycle(self):
        return outreach.cycle(self.database, self.config, self.adapter, self.now)

    def gates(self, action=None, proof=None):
        return outreach.action_gates(self.database, self.config, action or self.action,
                                     self.proof if proof is None else proof, self.now)

    def queue(self):
        return outreach.enqueue_action(self.database, self.action)

    def row(self):
        return self.database.execute("SELECT * FROM outreach_actions WHERE action_id=?",
                                     (self.action["action_id"],)).fetchone()

    def test_contextual_send_needs_no_role_or_invitation_history(self):
        self.queue()
        self.assertEqual(self.gates(), [])
        result = self.cycle()
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(self.row()["state"], "ACKNOWLEDGED_UNCONFIRMED")
        self.client._request.assert_called_once_with("send_message", {
            "conversationId": "mock-room", "linkedInAccountId": 901,
            "message": self.action["message"], "subject": ""})
        # Inspection and claim only: nothing re-runs the binding or reads the thread right
        # before the send (owner, 2026-09-30).
        self.assertEqual(self.binding.call_count, 3)
        self.assertIn("provider_request", json.loads(self.row()["result"]))
        self.client.call.assert_not_called()
        self.thread_binding.assert_not_called()
        self.assertEqual(engagement.stop_state(self.database, self.action["profile"]), "REPLY_REQUIRES_CONTEXT")

    def test_source_binding_failures_and_missing_validator_hold_without_send(self):
        self.queue()
        for outcome in (["SOURCE_CHANGED"], ["RESULT_NOT_COMPLETE"], ["NEW_INBOUND"]):
            self.binding.return_value = outcome
            self.assertIn(outcome[0], self.gates())
        self.binding.side_effect = ImportError("validator unavailable")
        self.assertIn("REPLY_SOURCE_BINDING_UNAVAILABLE", self.gates())
        self.assertEqual(self.cycle()["attempts"], 0)
        self.client._request.assert_not_called()

    def test_exact_proof_fields_and_context_flags_are_required(self):
        for field in ("reply_source_key", "reply_source_sha256", "reply_source_id", "reply_inbound_at",
                      "reply_latest_inbound", "reply_already_answered", "reply_context_verified",
                      # negative_reply and opt_out dropped from the REQUIRED list (owner,
                      # 2026-09-23: "We only block on a solid yes. Unknown is not a block").
                      # An absent field is unknown, and unknown no longer refuses; a recorded
                      # opt-out still refuses through engagement_stops/stop_state.
                      "sender_ready", "reply_transport_supported", "provider_restriction_observed",
                      "conversation_id", "sender_id", "identity", "agent_decision", "evidence", "checked_at",
                      "provider_snapshot_complete"):
            with self.subTest(field=field):
                changed = dict(self.proof)
                changed.pop(field)
                self.assertTrue(self.gates(proof=changed))
        for field in ("content_sources", "identity_source"):
            with self.subTest(field=field):
                changed = copy.deepcopy(self.action)
                changed["verification"].pop(field)
                self.assertTrue(self.gates(action=changed))

    def test_unknown_quota_does_not_invent_entitlement_or_override_reported_restriction(self):
        self.assertNotIn("entitlement_verified", self.proof)
        self.assertNotIn("cooldown", self.proof)
        self.assertEqual(self.gates(), [])
        for restriction in (True, None):
            with self.subTest(restriction=restriction):
                self.assertIn("PROVIDER_RESTRICTION_OBSERVED_UNVERIFIED",
                              self.gates(proof=dict(self.proof, provider_restriction_observed=restriction)))
        self.assertIn("REPLY_TRANSPORT_SUPPORTED_UNVERIFIED",
                      self.gates(proof=dict(self.proof, reply_transport_supported=False)))

    def test_reply_requires_explicit_enabled_purpose(self):
        self.config["reply_execution"]["enabled"] = False
        self.assertIn("REPLY_EXECUTION_NOT_ENABLED", self.gates())
        self.config["reply_execution"]["enabled"] = True
        self.config.pop("outreach_allowed_purposes")
        self.assertIn("OUTREACH_PURPOSE_NOT_ALLOWED", self.gates())
        self.config["outreach_allowed_purposes"] = ["INITIAL", "FIRST_MESSAGE"]
        self.assertIn("OUTREACH_PURPOSE_NOT_ALLOWED", self.gates())

    def test_missing_source_fields_cannot_reserve_a_slot(self):
        for field in ("reply_task_id", "reply_source_key", "reply_source_sha256", "reply_source",
                      "reply_source_id", "reply_inbound_at", "sender_id", "conversation_id"):
            with self.subTest(field=field):
                changed = dict(self.action)
                changed.pop(field)
                with self.assertRaises(ValueError):
                    outreach.enqueue_action(self.database, changed)

    def test_inbound_slot_is_global_across_alias_and_restart(self):
        self.queue()
        self.assertEqual(self.queue()["state"], "ALREADY_QUEUED")
        reopened = self.connect()
        duplicate = dict(self.action, action_id="different-action", profile="https://www.linkedin.com/in/alias",
                         reply_source_key="different-import-key", reply_task_id="different-task")
        with self.assertRaises(ValueError):
            outreach.enqueue_action(reopened, duplicate)
        self.assertIn("REPLY_SLOT_ALREADY_RESERVED", self.gates(action=duplicate))
        with self.assertRaises(sqlite3.IntegrityError):
            with reopened:
                reopened.execute("INSERT INTO outreach_actions SELECT 'bypass', 'alias', slot, payload, payload_hash, state, claimed_at, result FROM outreach_actions")

    def test_initial_and_first_message_still_stop_on_inbound(self):
        for purpose in ("INITIAL", "FIRST_MESSAGE"):
            with self.subTest(purpose=purpose):
                gates = self.gates(action=dict(self.action, purpose=purpose))
                self.assertIn("REPLY_REQUIRES_CONTEXT", gates)
                # `HAS_REPLY_PRESENT_OR_UNKNOWN` dropped (owner, 2026-09-23): this fixture never
                # sets `has_reply`, so it was asserting that UNKNOWN blocks - absence of evidence.
                # That an inbound reply stops a cold INITIAL/FIRST_MESSAGE is still proven by
                # REPLY_REQUIRES_CONTEXT and SUPPRESSED_CONTACT below, both unchanged.
                # SENT_INVITATIONS_COMPLETE_MISSING and TEAM_RELEVANCE_VERIFIED_MISSING are gone
                # (Owner, 2026-09-23: "We only block on a solid yes. Unknown is not a block." and "we should not block an outreach on the
                # receipients team or Job title"). That an inbound reply stops a cold
                # INITIAL/FIRST_MESSAGE is proven by REPLY_REQUIRES_CONTEXT and SUPPRESSED_CONTACT,
                # both unchanged - the safety property does not depend on the removed gates.
                self.assertNotIn("SENT_INVITATIONS_COMPLETE_MISSING", gates)
                self.assertNotIn("TEAM_RELEVANCE_VERIFIED_MISSING", gates)
                self.assertIn("SUPPRESSED_CONTACT", gates)

    def test_closed_and_negative_recipients_remain_stopped(self):
        self.record_inbound("older-negative", inference={"method": "mock-review", "sentiment": "negative"})
        self.assertIn("OPTED_OUT", self.gates())
        with self.database:
            self.database.execute("DELETE FROM engagement_stops")
        self.assertIn("OPTED_OUT", self.gates())
        engagement.close_outreach(self.database, self.action["profile"], "explicit closure", self.now)
        self.assertIn("OUTREACH_CLOSED", self.gates())
        with self.assertRaisesRegex(ValueError, "OUTREACH_CLOSED"):
            self.queue()

    def test_historical_exclusions_and_uncertain_alias_claims_remain_stopped(self):
        with self.database:
            self.database.execute("INSERT INTO suppressions VALUES ('name:mock recipient','Explicit exclusion')")
        self.assertIn("SUPPRESSED_CONTACT", self.gates())
        with self.database:
            self.database.execute("DELETE FROM suppressions WHERE identity='name:mock recipient'")
            self.database.execute("CREATE TABLE rw_aliases(kind TEXT, alias TEXT, canonical TEXT)")
            self.database.execute("INSERT INTO rw_aliases VALUES ('contact','linkedin.com/in/alias','linkedin.com/in/mock-recipient')")
            self.database.execute("INSERT INTO outreach_actions VALUES ('uncertain','linkedin.com/in/alias','INITIAL','{}','hash','UNCERTAIN_NO_RETRY',NULL,NULL)")
        self.assertIn("UNRESOLVED_ACTION_NO_RETRY", self.gates())
        with self.database:
            self.database.execute("UPDATE outreach_actions SET state='CONFIRMED_NOT_SENT' WHERE action_id='uncertain'")
        self.assertIn("NOT_SENT_RETRY_UNSUPPORTED", self.gates())

    def test_a_reply_to_someone_whose_timed_out_invitation_was_ours_is_not_blocked(self):
        # GL-I1: a timeout on our invitation stays ours, so the person's reply is not blocked by
        # NOT_SENT_RETRY_UNSUPPORTED (it was, when such an invite was marked CONFIRMED_NOT_SENT).
        import hashlib
        import linkedin_port
        import sequence
        fake = linkedin_port.FakePort()
        provider_id = fake.add_person(self.action["profile"])
        payload = {"action_id": "invite-1", "purpose": "INVITE", "provider": "unipile", "channel": "linkedin",
                   "profile": self.action["profile"], "recipient": "Mock Recipient", "provider_id": provider_id,
                   "note": "Hi."}
        text = json.dumps(payload, sort_keys=True)
        with self.database:
            self.database.execute("INSERT INTO outreach_actions(action_id,identity,slot,payload,payload_hash,state) VALUES (?,?,?,?,?,?)",
                                  ("invite-1", "linkedin.com/in/mock-recipient", "INVITE", text,
                                   hashlib.sha256(text.encode()).hexdigest(), "REQUEST_STARTED"))
        real = fake.lookup

        def unclear(profile):
            person = real(profile)
            return linkedin_port.Person(person.profile, person.provider_id, "?", None, person.raw)
        fake.fail("send_invitation", "timeout_after_write")
        with patch.object(fake, "lookup", side_effect=unclear):
            sequence.send_invite(self.database, fake, "invite-1")
        self.assertEqual(sequence.reconcile_invite(self.database, fake, "invite-1"), sequence.CONFIRMED)
        self.assertNotIn("NOT_SENT_RETRY_UNSUPPORTED", self.gates())

    def test_late_source_change_no_longer_cancels_a_claimed_reply(self):
        # Owner, 2026-09-30: right before sending only opt-out/negative, closure, exclusion and
        # the one-slot rule apply; a changed source binding is not re-checked there.
        self.queue()
        self.before_send = lambda: setattr(self.binding, "return_value", ["SOURCE_CHANGED"])
        self.cycle()
        self.assertEqual(self.row()["state"], "ACKNOWLEDGED_UNCONFIRMED")
        self.client._request.assert_called_once()

    def test_late_closure_cancels_before_provider_request(self):
        self.queue()
        self.before_send = lambda: engagement.close_outreach(
            self.database, self.action["profile"], "late explicit closure", self.now)
        self.cycle()
        self.assertEqual(self.row()["state"], "CANCELLED_BEFORE_SEND")
        self.client._request.assert_not_called()

    def test_late_negative_inbound_cancels_before_provider_request(self):
        self.queue()
        self.before_send = lambda: self.record_inbound(
            "new-opt-out", inference={"method": "mock-review", "sentiment": "opt_out"})
        self.cycle()
        self.assertEqual(self.row()["state"], "CANCELLED_BEFORE_SEND")
        self.client._request.assert_not_called()

    def test_no_final_thread_read_before_a_reply_is_sent(self):
        # The fresh thread read and its checks are gone (owner, 2026-09-30: "we dont need to
        # reread it"); a failing thread reader cannot hold the reply.
        self.queue()
        self.client.call.side_effect = TimeoutError("thread reader must not be called")
        self.thread_binding.return_value = ["REPLY_NEWER_CONVERSATION_ACTIVITY"]
        self.cycle()
        self.assertEqual(self.row()["state"], "ACKNOWLEDGED_UNCONFIRMED")
        self.assertNotIn("provider_reply_check", json.loads(self.row()["result"]))
        self.client.call.assert_not_called()
        self.client._request.assert_called_once()

    def test_excluded_name_added_after_claim_still_cancels(self):
        self.queue()
        def exclude():
            with self.database:
                self.database.execute("INSERT OR REPLACE INTO suppressions VALUES ('name:mock recipient','Existing user exclusion')")
        self.before_send = exclude
        self.cycle()
        self.assertEqual(self.row()["state"], "CANCELLED_BEFORE_SEND")
        self.client._request.assert_not_called()

    def test_timeout_is_uncertain_and_never_blindly_retried(self):
        self.queue()
        self.client._request.side_effect = TimeoutError("mock lost acknowledgment")
        self.cycle()
        self.assertEqual(self.row()["state"], "UNCERTAIN_NO_RETRY")
        marker = json.loads(self.row()["result"])["provider_request"]
        reopened = self.connect()
        result = outreach.cycle(reopened, self.config, self.adapter, self.now)
        self.assertEqual(result["attempts"], 0)
        self.assertEqual(json.loads(self.row()["result"])["provider_request"], marker)
        self.client._request.assert_called_once()

    def test_interruption_after_request_marker_survives_restart(self):
        self.queue()
        self.client._request.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.cycle()
        self.assertEqual(self.row()["state"], "REQUEST_STARTED")
        reopened = self.connect()
        provider = outreach_provider.Provider(reopened, self.config, self.client)
        with self.assertRaisesRegex(ValueError, "already consumed"):
            provider.send_claimed(self.action["action_id"], self.config)
        self.assertEqual(outreach.cycle(reopened, self.config, self.adapter, self.now)["attempts"], 0)
        self.client._request.assert_called_once()

    def test_independent_receipt_records_reply_without_consuming_followup_slot(self):
        self.queue()
        self.cycle()
        claim = self.database.execute("SELECT * FROM outreach_action_claims").fetchone()
        self.receipt = {"independent": True, "outcome": "SENT", "action_id": self.action["action_id"],
                        "identity": claim["identity"], "payload_hash": claim["payload_hash"], "sender_id": 901,
                        "operation": "send_message", "conversation_id": "mock-room", "receipt_id": "mock-receipt",
                        "evidence": "mock-independent-read", "observed_at": self.now.isoformat(),
                        "occurred_at": self.now.isoformat()}
        self.cycle()
        self.assertEqual(self.row()["state"], "CONFIRMED_SENT")
        event = json.loads(self.database.execute("SELECT payload FROM engagement_events WHERE source_id='mock-receipt'").fetchone()[0])
        self.assertEqual((event["kind"], event["purpose"]), ("outbound_observed", "REPLY"))
        duplicate = dict(self.action, action_id="retry-after-confirmed")
        with self.assertRaises(ValueError):
            outreach.enqueue_action(self.database, duplicate)
        self.action = dict(self.action, action_id="second-reply", reply_source_id="second-inbound")
        self.proof["reply_source_id"] = "second-inbound"
        self.record_inbound("second-inbound")
        self.queue()
        self.assertEqual(self.gates(), [])
        self.assertEqual(self.cycle()["attempts"], 1)
        self.assertEqual(self.client._request.call_count, 2)


class ReplySendBindingIntegrationTests(unittest.TestCase):
    def test_real_complete_worker_result_binds_exact_reply_and_rechecks_task(self):
        import reply_worker
        import test_reply_worker
        from test_pipeline_routing import NOW

        fixture = test_reply_worker.ReplyWorkerTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with patch.object(reply_worker.reply_resume, "context", return_value={
                "source": "mock-resume", "text": "Mock verified background"}):
            # The correspondent must be a conversation the harness manages (owner, 2026-09-23).
            # This case is about the SEND BINDING, not about scope, so the conversation is
            # recorded as one the owner contacted by hand - the same shape as his eight real
            # ones - and the binding assertions below are unchanged. Recorded before the reply
            # worker runs: a tracked person with no reply basis is skipped before the reply AI
            # (owner, 2026-10-01: "Dont generate draft for me").
            outreach.initialize_managed(fixture.database)
            outreach.record_managed(fixture.database, "https://www.linkedin.com/in/mock-contact",
                                    "Synthetic managed conversation", NOW)
            fixture.database.commit()
            task_id = fixture.enqueue("Could you explain the role?")
            fixture.mutate_output = lambda output: output.update(reply_proposal={
                "text": "Which aspect would you like to discuss?", "basis_quote": output["quote_span"]})
            fixture.run_cycle()
            self.assertEqual(fixture.task(task_id)["status"], "COMPLETE")
            fixture.config.update(sender_id=123, outreach_actions_enabled=True, outreach_mode="guarded",
                                  outreach_allowed_purposes=["REPLY"], reply_execution={"enabled": True})
            now = dt.datetime.fromisoformat(NOW) + dt.timedelta(minutes=1)
            outreach.initialize_actions(fixture.database)
            action = reply_execution._action(fixture.database, fixture.config, task_id, now)
            self.assertEqual(reply_execution.binding_gates(fixture.database, fixture.config, action, now), [])
            outreach.enqueue_action(fixture.database, action)
            proof = {field: action[field] for field in ("reply_source_key", "reply_source_sha256", "reply_source_id",
                                                       "reply_inbound_at", "conversation_id", "sender_id")}
            proof.update(identity="linkedin.com/in/mock-contact", sender_ready=True, reply_transport_supported=True,
                         provider_restriction_observed=False, quota_remaining=None,
                         negative_reply=False, opt_out=False, evidence="mock-authenticated-read",
                         checked_at=now.isoformat(), agent_decision="READY", provider_snapshot_complete=True,
                         reply_latest_inbound=True, reply_already_answered=False, reply_context_verified=True)
            self.assertEqual(outreach.action_gates(fixture.database, fixture.config, action, proof, now), [])
            # A rewritten text is allowed (the judge's repair turn); a different thread is not.
            self.assertEqual(outreach.action_gates(
                fixture.database, fixture.config, dict(action, message="rewritten"), proof, now), [])
            self.assertIn("REPLY_ACTION_DECISION_MISMATCH", outreach.action_gates(
                fixture.database, fixture.config, dict(action, conversation_id="another-room"),
                dict(proof, conversation_id="another-room"), now))
            with fixture.database:
                fixture.database.execute("UPDATE workflow_tasks SET status='BLOCKED' WHERE task_id=?", (task_id,))
            self.assertIn("REPLY_DECISION_BINDING_INVALID", outreach.action_gates(
                fixture.database, fixture.config, action, proof, now))


if __name__ == "__main__":
    unittest.main()
