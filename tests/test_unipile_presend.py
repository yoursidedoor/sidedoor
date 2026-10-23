"""E3.T3 remainder: the pre-send inspection reads the person through the port on the Unipile path, and a
FIRST_MESSAGE goes out over the port (start_chat or the chat on file) with claim-before-send and
"reconcile, never resend". FakePort only; HeyReach must never be read."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import Mock, patch

import applications
import conversion
import engagement
import orchestrator
import outreach
import outreach_agent
import pipeline_routing as routing
import research_workflow as research
import linkedin_port
from linkedin_port import FakePort
from orchestrator import profile_key


ROOT = Path(__file__).resolve().parents[1]
PROFILE = "https://www.linkedin.com/in/port-person"
OWNER = "https://www.linkedin.com/in/port-owner"


def now():
    return dt.datetime.now(dt.timezone.utc)


class Base(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        outreach.initialize_actions(self.db)
        self.db.commit()
        self.config = {"sender_id": 123, "sender_profile": OWNER, "linkedin_provider": "unipile",
                       "outreach_actions_enabled": True, "outreach_mode": "guarded",
                       "outreach_allowed_purposes": ["INITIAL", "FIRST_MESSAGE", "INVITE"], "model": "synthetic", "codex": "NEVER",
                       "timezone": "UTC", "daily_compute_seconds": 10800,
                       "outreach_agent": {"enabled": True, "timeout_seconds": 1}}
        self.port = FakePort(own_profile=OWNER)
        self.person = self.port.add_person(PROFILE)
        self.client = Mock()
        self.client.call.side_effect = AssertionError("HeyReach must not be read on the Unipile path")
        self.client._request.side_effect = AssertionError("HeyReach must not be written on the Unipile path")
        self.adapter = outreach_agent.Adapter(ROOT, self.db, self.config, self.client, self.judge, port=self.port)

    def judge(self, config, prompt, schema, timeout):
        packet = json.loads(prompt.split("UNTRUSTED_SOURCE_JSON:\n", 1)[1])
        observation = packet["observation"]
        self.assertEqual(observation["route"], "unipile")
        chats = [read["chat_id"] for read in observation["observations"]]
        invite = packet["action"]["purpose"] == "INVITE"
        return json.dumps({"output_version": outreach_agent.VERSION, "source_sha256": packet["source_sha256"],
                           "decision": "READY", "reason": "Synthetic.", "next_action": "Use the guard.",
                           "findings": {"prior_outreach": False, "has_reply": False, "negative_reply": False, "opt_out": False},
                           "conversation_id": None if invite or not chats else chats[0],
                           "route": "INVITATION" if invite else "EXISTING_THREAD",
                           "receipt_observation": None, "receipt_message": None})

    def action(self, purpose, **extra):
        verified = now().isoformat()
        action = {"action_id": purpose.lower() + "-1", "profile": PROFILE, "recipient": "Port Person",
                  "company": "Port Company", "purpose": purpose, "channel": "linkedin", "provider": "unipile",
                  "message": "Hi, thanks for connecting.",
                  "verification": {"content_verified": True, "content_sources": ["mock"], "identity_verified": True,
                                   "identity_source": "mock", "verified_at": verified}}
        action.update(extra)
        return action


class Inspection(Base):
    def test_invite_inspection_reads_only_the_port_and_passes_the_gates(self):
        action = self.action("INVITE", provider_id=self.person, note="Hi, a short note.")
        proof = self.adapter.inspect(action, self.config)
        self.assertEqual((proof["sender_ready"], proof["entitlement_verified"], proof["capability_gates"]), (True, True, []))
        self.assertIsNone(proof["cooldown"])
        self.assertEqual(outreach.action_gates(self.db, self.config, action, proof, now()), [])
        self.assertIn("lookup", self.port.calls)
        self.client.call.assert_not_called()

    def test_profile_state_and_chats_refuse_as_the_send_would(self):
        action = self.action("INVITE", provider_id=self.person, note="Hi, a short note.")
        self.port.add_invitation(self.person)
        proof = self.adapter.inspect(action, self.config)
        self.assertIn("INVITATION_ALREADY_PENDING", proof["capability_gates"])
        self.assertIn("PENDING_INVITATION_PRESENT", outreach.action_gates(self.db, self.config, action, proof, now()))
        self.port.accept(self.person)
        self.assertIn("ALREADY_CONNECTED", self.adapter.inspect(action, self.config)["capability_gates"])
        self.port.add_chat(self.person, [(False, "Hello there")])
        proof = self.adapter.inspect(action, self.config)
        self.assertTrue(proof["has_reply"])
        self.assertIn("HAS_REPLY_PRESENT", outreach.action_gates(self.db, self.config, action, proof, now()))
        self.assertEqual(self.port.calls.count("send_invitation"), 0)

    def test_an_unclear_profile_read_is_claimed_and_sent_as_live(self):
        # G1 (owner, 2026-10-22): an unclear pending/distance value decides nothing on a first send.
        real = self.port.lookup

        def unclear(profile):
            person = real(profile)
            return linkedin_port.Person(person.profile, person.provider_id, "?", None, person.raw)
        action = self.action("INVITE", provider_id=self.person, note="Hi, a short note.")
        with patch.object(self.port, "lookup", side_effect=unclear):
            proof = self.adapter.inspect(action, self.config)
            self.assertEqual(proof["capability_gates"], [])
            self.assertEqual(outreach.action_gates(self.db, self.config, action, proof, now()), [])
            outreach.enqueue_action(self.db, dict(action, message=action["note"]))
            self.db.commit()
            with patch("pacing.check", return_value={"go": True}):
                outreach.cycle(self.db, self.config, self.adapter, purpose="INVITE")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM outreach_action_claims").fetchone()[0], 1)
        self.assertEqual(self.port.calls.count("send_invitation"), 1)

    def test_heyreach_mode_keeps_the_heyreach_read(self):
        adapter = outreach_agent.Adapter(ROOT, self.db, self.config, self.client, self.judge)
        with self.assertRaises(Exception):
            adapter.inspect(self.action("INVITE", provider_id=self.person, note="Hi."), self.config)
        self.assertTrue(self.client.call.called)
        self.assertNotIn("lookup", self.port.calls)


class Initial(Base):
    """A 1st-degree new contact: the INITIAL note goes into the existing chat over the port, as HeyReach's
    existing-thread INITIAL; with no chat it is held exactly as HeyReach holds it."""

    def setUp(self):
        super().setUp()
        self.port.accept(self.person)

    def run_cycle(self):
        return outreach.cycle(self.db, self.config, self.adapter, purpose="INITIAL")

    def test_existing_chat_gets_the_initial_once_and_the_read_confirms_it(self):
        chat = self.port.add_chat(self.person, [])
        outreach.enqueue_action(self.db, self.action("INITIAL", conversation_id=chat))
        self.db.commit()
        self.run_cycle()
        self.run_cycle()
        self.run_cycle()
        self.assertEqual(self.db.execute("SELECT state FROM outreach_actions").fetchone()[0], "CONFIRMED_SENT")
        sent = [event for event in engagement.events_for(self.db, profile_key(PROFILE)) if event["kind"] == "initial_sent"]
        self.assertEqual([(event["source"], event["purpose"]) for event in sent], [("unipile", "INITIAL")])
        self.assertEqual((self.port.calls.count("send_message"), self.port.calls.count("start_chat"),
                          self.port.calls.count("send_invitation")), (1, 0, 0))
        self.client.call.assert_not_called()

    def test_reply_in_the_chat_of_a_port_initial_is_read_by_the_inbox(self):
        chat = self.port.add_chat(self.person, [])
        outreach.enqueue_action(self.db, self.action("INITIAL", conversation_id=chat))
        self.db.commit()
        for unused in range(3):
            self.run_cycle()
        self.port.chat_log[chat]["messages"].append({"is_sender": False, "text": "Thanks, happy to talk.", "sent_at": now().isoformat()})
        result = orchestrator.unipile_engagement_cycle(self.db, dict(self.config, engagement_enabled=True), self.port)
        item = result["items"][0]
        self.assertEqual((item["inserted"], item.get("unresolved_chats")), (2, None))
        inbound = [event for event in engagement.events_for(self.db, profile_key(PROFILE)) if event["kind"] == "inbound"]
        self.assertEqual([(event["source"], event["raw"]["conversation_id"]) for event in inbound], [("unipile", chat)])
        self.assertEqual(engagement.stop_state(self.db, PROFILE), "REPLY_REQUIRES_CONTEXT")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM workflow_tasks WHERE worker='engagement_review'").fetchone()[0], 1)
        for module in (applications, research, conversion, routing):
            module.initialize(self.db)
        routing.cycle(self.db, {"pipeline_routing": {"account_ids": [123], "designated_resume_sha256": "a" * 64}},
                      (now() + dt.timedelta(minutes=1)).isoformat())
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM workflow_tasks WHERE worker='pipeline_routing_review'").fetchone()[0], 1)
        # Our INITIAL, seen in the chat, reconciles to its own recorded send.
        self.assertEqual(engagement.unreconciled_outbound(self.db, engagement.events_for(self.db, profile_key(PROFILE))), [])

    def test_chat_of_a_port_initial_that_never_sent_stays_unresolved(self):
        chat = self.port.add_chat(self.person, [(False, "Hello from them.")])
        outreach.enqueue_action(self.db, self.action("INITIAL", conversation_id=chat))
        self.db.execute("UPDATE outreach_actions SET state='CANCELLED_BEFORE_SEND'")
        self.db.commit()
        item = orchestrator.unipile_engagement_cycle(self.db, dict(self.config, engagement_enabled=True), self.port)["items"][0]
        self.assertEqual((item["inserted"], item.get("unresolved_chats")), (0, 1))

    def test_no_chat_is_held_as_on_heyreach(self):
        action = self.action("INITIAL", conversation_id=None)
        proof = self.adapter.inspect(action, self.config)
        self.assertIn("SUPPORTED_NEW_CONTACT_THREAD_OR_PREPARED_CAMPAIGN_REQUIRED", proof["capability_gates"])
        self.assertIn("VERIFIED_CONVERSATION_MISSING", outreach.action_gates(self.db, self.config, action, proof, now()))


class FirstMessage(Base):
    def setUp(self):
        super().setUp()
        self.port.accept(self.person)
        accepted = (now() - dt.timedelta(hours=13)).isoformat()
        engagement.record_event(self.db, {"source": "unipile", "source_id": "acceptance:x", "profile": PROFILE,
            "kind": "accepted", "verified": True, "occurred_at": accepted, "observed_at": accepted,
            "evidence": "unipile-lookup:" + accepted, "raw": {}})
        self.db.commit()

    def queue(self, conversation_id=None):
        outreach.enqueue_action(self.db, self.action("FIRST_MESSAGE", conversation_id=conversation_id))
        self.db.commit()

    def run_cycle(self):
        return outreach.cycle(self.db, self.config, self.adapter, purpose="FIRST_MESSAGE")

    def state(self):
        return self.db.execute("SELECT state FROM outreach_actions").fetchone()[0]

    def initials(self):
        return [event for event in engagement.events_for(self.db, profile_key(PROFILE)) if event["kind"] == "initial_sent"]

    def test_start_chat_once_then_the_read_confirms_initial_sent(self):
        self.queue()
        self.run_cycle()
        self.assertEqual(self.state(), "ACKNOWLEDGED_UNCONFIRMED")
        self.assertEqual(self.initials(), [])  # an acknowledgement is not delivery
        self.run_cycle()
        self.assertEqual(self.state(), "CONFIRMED_SENT")
        sent = self.initials()
        self.assertEqual([(event["source"], event["purpose"]) for event in sent], [("unipile", "FIRST_MESSAGE")])
        self.run_cycle()
        self.assertEqual(self.port.calls.count("start_chat") + self.port.calls.count("send_message"), 1)
        self.client.call.assert_not_called()

    def test_existing_chat_is_messaged_in_place(self):
        chat = self.port.add_chat(self.person, [])
        self.queue(chat)
        self.run_cycle()
        self.assertEqual((self.port.calls.count("send_message"), self.port.calls.count("start_chat")), (1, 0))
        self.assertEqual(self.port.chat_log[chat]["messages"][-1]["text"], "Hi, thanks for connecting.")

    def test_uncertain_write_is_reconciled_and_never_resent(self):
        self.queue()
        self.port.fail("start_chat", "timeout_after_write")
        self.run_cycle()
        self.assertEqual(self.state(), "UNCERTAIN_NO_RETRY")
        self.run_cycle()
        self.assertEqual(self.state(), "CONFIRMED_SENT")  # the read shows our text: sent
        self.assertEqual(self.port.calls.count("start_chat"), 1)

    def test_write_that_did_not_land_stays_unknown_without_resending(self):
        self.queue()
        self.port.fail("start_chat", "timeout")
        for unused in range(3):
            self.run_cycle()
        self.assertEqual(self.state(), "UNCERTAIN_NO_RETRY")
        self.assertEqual(self.port.calls.count("start_chat"), 1)
        self.assertEqual(self.initials(), [])

    def test_final_check_refuses_a_stopped_person_before_any_write(self):
        self.queue()
        engagement.record_event(self.db, {"source": "unipile", "source_id": "inbound:x", "profile": PROFILE,
            "kind": "inbound", "occurred_at": now().isoformat(), "observed_at": now().isoformat(), "evidence": "t", "raw": {}})
        self.db.commit()
        with patch("outreach.action_gates", return_value=[]):  # reach the send's own final check
            self.run_cycle()
        self.assertEqual(self.state(), "CANCELLED_BEFORE_SEND")
        self.assertEqual(self.port.calls.count("start_chat"), 0)


if __name__ == "__main__":
    unittest.main()
