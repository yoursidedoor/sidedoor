"""E3.T3 piece 2B: acceptance, follow-ups and inactivity closure for Unipile-invited people over
FakePort, with a fake clock. HeyReach people keep their own tests (test_followup_execution and the
inactivity tests), which run unchanged."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import contextlib
import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import Mock, patch

import engagement
import followup_execution as followup
import inactivity_closure
import manual_followup_e2e as manual
import outreach
import outreach_agent
import outreach_provider
import research_workflow
import sequence
from linkedin_port import FakePort
from orchestrator import profile_key

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_inactivity_read_completeness as completeness


ROOT = Path(__file__).resolve().parent.parent
PROFILE = "https://www.linkedin.com/in/unipile-followup-person"
OWNER = "https://www.linkedin.com/in/unipile-owner"
INVITED = dt.datetime(2026, 10, 5, 16, tzinfo=dt.timezone.utc)  # a Monday



class Base(unittest.TestCase):

    def setUp(self):
        self.enterContext(manual.offline())
        self.now = INVITED
        self.enterContext(self.clock())
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        outreach.initialize_actions(self.db)
        research_workflow.initialize(self.db)
        self.db.execute("CREATE TABLE IF NOT EXISTS suppressions (identity TEXT PRIMARY KEY, reason TEXT)")
        self.db.commit()
        self.config = {"linkedin_provider": "unipile", "sender_id": 123, "sender_profile": OWNER, "model": "synthetic", "codex": "NEVER",
                       "timezone": "America/Los_Angeles", "daily_compute_seconds": 10800,
                       "followup_execution": {"enabled": True, "dispatch_enabled": True, "timeout_seconds": 1, "max_items": 4},
                       "outreach_agent": {"enabled": True, "timeout_seconds": 1},
                       "outreach_actions_enabled": True, "outreach_mode": "guarded", "outreach_allowed_purposes": ["FOLLOWUP"]}
        self.port = FakePort(own_profile=OWNER)
        self.person = self.port.add_person(PROFILE)
        self.compositions = []
        self.invite()
        self.adapter = followup.FollowupAdapter(ROOT, self.db, self.config, Mock(), execute=self.execute, port=self.port)

    @contextlib.contextmanager
    def clock(self):
        stamp = lambda: self.now.isoformat()
        with patch("followup_execution._now", side_effect=lambda value=None: engagement.timestamp(self.now if value is None else value)), \
                patch("outreach_provider._now", side_effect=stamp), patch("sequence._now", side_effect=stamp), \
                patch("linkedin_port._now", side_effect=stamp):
            yield

    def invite(self):
        payload = {"action_id": "invite-1", "purpose": "INVITE", "provider": "unipile", "channel": "linkedin",
                   "profile": PROFILE, "recipient": "Unipile Person", "company": "Synthetic Company",
                   "provider_id": self.person, "note": "Hi, I would like to connect about your platform work.",
                   "verification": {"verified_at": INVITED.isoformat(), "identity_verified": True,
                                    "team_relevance_verified": True, "identity_source": "https://example.test/person",
                                    "team_source": "https://example.test/team"}}
        text = json.dumps(payload, sort_keys=True)
        self.db.execute("INSERT INTO outreach_actions(action_id,identity,slot,payload,payload_hash,state) VALUES (?,?,?,?,?,?)",
                        ("invite-1", profile_key(PROFILE), "INITIAL", text, hashlib.sha256(text.encode()).hexdigest(),
                         "REQUEST_STARTED"))
        self.db.commit()
        self.assertEqual(sequence.send_invite(self.db, self.port, "invite-1"), sequence.ACKED)
        self.assertEqual(sequence.reconcile_invite(self.db, self.port, "invite-1"), sequence.CONFIRMED)

    def execute(self, config, prompt, schema, timeout):
        packet = json.loads(prompt.split("UNTRUSTED_SOURCE_JSON:\n", 1)[1])
        if "action" not in packet:
            ordinal = packet["plan"]["ordinal"]
            self.compositions.append(ordinal)
            return json.dumps({"output_version": followup.VERSION, "source_sha256": packet["source_sha256"],
                               "decision": "COMPOSE", "message": "Follow-up number %d about the platform role." % ordinal,
                               "reason": "Grounded in the invitation.", "next_action": "Send through the guard."})
        return json.dumps({"output_version": outreach_agent.VERSION, "source_sha256": packet["source_sha256"],
                           "decision": "READY", "reason": "The follow-up fits the thread.", "next_action": "Send it.",
                           "conversation_id": packet["action"]["conversation_id"], "receipt_observation": None,
                           "receipt_message": None,
                           "findings": {"prior_outreach": True, "has_reply": False, "negative_reply": False, "opt_out": False}})

    def run_cycle(self):
        return followup.cycle(ROOT, self.db, self.config, self.adapter, execute=self.execute, now=self.now)

    def kinds(self, kind):
        return [event for event in engagement.events_for(self.db, PROFILE) if event["kind"] == kind]

    def ours(self):
        return [message["text"] for chat in self.port.chat_log.values() for message in chat["messages"] if message["is_sender"]]

    def state(self, ordinal):
        row = self.db.execute("SELECT state FROM outreach_actions WHERE slot=?", ("FOLLOWUP:" + str(ordinal),)).fetchone()
        return row[0] if row else None

    def first_followup(self):
        """Due at three business days; acceptance read PENDING, then ACCEPTED; sent via start_chat."""
        self.now = engagement.business_days_after(INVITED, 3)
        self.run_cycle()
        self.assertEqual(self.kinds("accepted"), [])
        self.assertIsNone(self.state(1))
        self.assertNotIn("start_chat", self.port.calls)
        self.port.accept(self.person)
        self.now += dt.timedelta(hours=1, minutes=1)
        self.run_cycle()
        self.assertEqual(len(self.kinds("accepted")), 1)
        self.assertEqual(self.port.calls.count("start_chat"), 1)

    def settle(self):
        self.now += dt.timedelta(minutes=20)
        self.run_cycle()



class StartChat(Base):
    """No chat after acceptance: the first follow-up starts one (start_chat), the second goes into it."""

    def test_full_sequence_three_then_five_business_days(self):
        self.first_followup()
        self.assertEqual(self.state(1), "ACKNOWLEDGED_UNCONFIRMED")
        self.assertEqual(self.kinds("followup_sent"), [])  # an acknowledgement is not delivery
        self.settle()
        self.assertEqual(self.state(1), "CONFIRMED_SENT")
        self.assertEqual(len(self.kinds("followup_sent")), 1)
        chat_id = next(iter(self.port.chat_log))
        # Second follow-up: five business days after the actual first, into the same chat.
        self.now = engagement.business_days_after(self.kinds("followup_sent")[0]["occurred_at"], 5)
        self.run_cycle()
        self.assertEqual(self.port.calls.count("send_message"), 1)
        self.assertEqual(self.port.calls.count("start_chat"), 1)
        self.assertEqual(len(self.port.chat_log), 1)
        self.settle()
        self.assertEqual(self.state(2), "CONFIRMED_SENT")
        self.assertEqual(len(self.kinds("followup_sent")), 2)
        self.assertEqual(self.ours(), ["Follow-up number 1 about the platform role.",
                                       "Follow-up number 2 about the platform role."])
        self.assertEqual(json.loads(self.db.execute("SELECT result FROM outreach_actions WHERE slot='FOLLOWUP:2'").fetchone()[0])["chat_id"],
                         chat_id)
        self.assertEqual(self.compositions, [1, 2])

    def test_reply_in_chat_stops_the_second_followup(self):
        self.first_followup()
        self.settle()
        self.now = engagement.business_days_after(self.kinds("followup_sent")[0]["occurred_at"], 2)
        chat = next(iter(self.port.chat_log.values()))
        chat["messages"].append({"is_sender": False, "text": "Thanks, happy to talk.", "sent_at": self.now.isoformat()})
        self.now = engagement.business_days_after(self.kinds("followup_sent")[0]["occurred_at"], 5)
        result = self.run_cycle()
        self.assertEqual(self.port.calls.count("send_message"), 0)
        self.assertEqual(len(self.kinds("followup_sent")), 1)
        self.assertEqual(self.state(2), "BLOCKED", result)
        gates = json.loads(self.db.execute("SELECT result FROM outreach_actions WHERE slot='FOLLOWUP:2'").fetchone()[0])
        self.assertIn("HAS_REPLY_PRESENT", gates)

    def test_uncertain_send_that_landed_is_recorded_not_resent(self):
        self.port.fail("start_chat", "timeout_after_write")
        self.first_followup()
        self.assertEqual(self.state(1), "UNCERTAIN_NO_RETRY")
        self.assertEqual(self.kinds("followup_sent"), [])
        self.settle()
        self.assertEqual(self.state(1), "CONFIRMED_SENT")
        self.assertEqual(self.port.calls.count("start_chat"), 1)
        self.assertEqual(len(self.ours()), 1)
        self.assertEqual(len(self.kinds("followup_sent")), 1)

    def test_uncertain_send_that_did_not_land_is_never_resent(self):
        self.port.fail("start_chat", "timeout")
        self.first_followup()
        self.assertEqual(self.state(1), "UNCERTAIN_NO_RETRY")
        self.settle()
        self.settle()
        self.assertEqual(self.state(1), "UNCERTAIN_NO_RETRY")
        self.assertEqual(self.port.calls.count("start_chat"), 1)
        self.assertEqual(self.kinds("followup_sent"), [])

    def test_unreadable_chats_wait_without_sending(self):
        self.now = engagement.business_days_after(INVITED, 3)
        self.port.accept(self.person)
        self.port.fail("chats", "incomplete_page")
        result = self.run_cycle()
        self.assertEqual(result["composition"]["items"][0]["blocker"], "FOLLOWUP_UNIPILE_READ_INCOMPLETE_PAGE")
        self.assertNotIn("start_chat", self.port.calls)

    def test_without_a_port_nothing_is_read_or_sent(self):
        self.adapter = followup.FollowupAdapter(ROOT, self.db, self.config, Mock(), execute=self.execute)
        self.now = engagement.business_days_after(INVITED, 3)
        calls = len(self.port.calls)
        result = self.run_cycle()
        self.assertEqual(len(self.port.calls), calls)
        self.assertEqual(result["composition"]["items"][0]["state"], "HELD")
        self.assertIsNone(self.state(1))


class ExistingChat(Base):
    """LinkedIn may show the accepted invitation's note as the first message of a chat; then both
    follow-ups go into that chat with send_message, and no hook outside this piece is needed."""

    def accept_with_note_in_chat(self):
        self.port.accept(self.person)
        due, self.now = self.now, INVITED  # the note keeps the invitation's own time in the chat
        self.port.add_chat(self.person, [(True, "Hi, I would like to connect about your platform work.")])
        self.now = due

    def test_both_followups_go_into_the_invitation_chat(self):
        self.now = engagement.business_days_after(INVITED, 3)
        self.accept_with_note_in_chat()
        self.run_cycle()
        self.settle()
        self.assertEqual(self.state(1), "CONFIRMED_SENT")
        self.now = engagement.business_days_after(self.kinds("followup_sent")[0]["occurred_at"], 5)
        self.run_cycle()
        self.settle()
        self.assertEqual(self.state(2), "CONFIRMED_SENT")
        self.assertEqual(self.port.calls.count("send_message"), 2)
        self.assertNotIn("start_chat", self.port.calls)
        self.assertEqual(len(self.ours()), 3)

    def test_unrecorded_message_of_ours_holds_the_followup(self):
        self.now = engagement.business_days_after(INVITED, 3)
        self.accept_with_note_in_chat()
        next(iter(self.port.chat_log.values()))["messages"].append(
            {"is_sender": True, "text": "A message the owner wrote by hand.", "sent_at": self.now.isoformat()})
        result = self.run_cycle()
        self.assertEqual(result["composition"]["items"][0]["blocker"], "FOLLOWUP_UNRECONCILED_OUTBOUND")
        self.assertNotIn("send_message", self.port.calls)


class AfterPortInitial(Base):
    """Owner, 2026-10-21: a person whose INITIAL went over the port (1st degree, existing chat) is
    followed up over the port exactly as a Unipile INVITE person; HeyReach is never called."""

    def invite(self):
        self.port.accept(self.person)
        self.chat = self.port.add_chat(self.person, [])
        self.config["outreach_allowed_purposes"] = ["INITIAL", "FOLLOWUP"]
        client = Mock()
        client.call.side_effect = AssertionError("HeyReach must not be read")
        self.heyreach = client

        def judge(config, prompt, schema, timeout):
            packet = json.loads(prompt.split("UNTRUSTED_SOURCE_JSON:\n", 1)[1])
            return json.dumps({"output_version": outreach_agent.VERSION, "source_sha256": packet["source_sha256"],
                               "decision": "READY", "reason": "Synthetic.", "next_action": "Send it.",
                               "conversation_id": self.chat, "route": "EXISTING_THREAD", "receipt_observation": None,
                               "receipt_message": None, "findings": {"prior_outreach": False, "has_reply": False,
                                                                     "negative_reply": False, "opt_out": False}})
        action = {"action_id": "initial-1", "purpose": "INITIAL", "provider": "unipile", "channel": "linkedin",
                  "profile": PROFILE, "recipient": "Unipile Person", "company": "Synthetic Company",
                  "conversation_id": self.chat, "message": "Hi, I would like to talk about your platform work.",
                  "verification": {"verified_at": INVITED.isoformat(), "content_verified": True, "content_sources": ["mock"],
                                   "identity_verified": True, "team_relevance_verified": True,
                                   "identity_source": "https://example.test/person", "team_source": "https://example.test/team"}}
        outreach.enqueue_action(self.db, action)
        self.db.commit()
        adapter = outreach_agent.Adapter(ROOT, self.db, self.config, client, judge, port=self.port)
        for unused in range(2):
            outreach.cycle(self.db, self.config, adapter, self.now, purpose="INITIAL")
            self.now += dt.timedelta(minutes=20)
        self.assertEqual(self.db.execute("SELECT state FROM outreach_actions").fetchone()[0], "CONFIRMED_SENT")

    def test_followup_one_goes_into_the_chat_over_the_port(self):
        self.adapter.provider.client = self.heyreach
        self.now = engagement.business_days_after(self.kinds("initial_sent")[0]["occurred_at"], 3) + dt.timedelta(minutes=1)
        self.run_cycle()
        self.settle()
        self.assertEqual(self.state(1), "CONFIRMED_SENT")
        self.assertEqual(len(self.kinds("followup_sent")), 1)
        self.assertEqual((self.port.calls.count("send_message"), self.port.calls.count("start_chat")), (2, 0))
        self.assertEqual(len(self.port.chat_log[self.chat]["messages"]), 2)
        self.heyreach.call.assert_not_called()


class UnipileInactivity(unittest.TestCase):
    """The acceptance part of the 27-day closure read for a Unipile person is sequence.acceptance,
    so acceptance is observable and one complete read closes (no two-read rule)."""

    setUp = completeness.ZeroRoomReadCompleteness.setUp
    call = completeness.ZeroRoomReadCompleteness.call

    def unipile_person(self, pending=True):
        with self.database:
            self.database.execute("DELETE FROM engagement_events")
        engagement.record_event(self.database, {"source": "unipile", "source_id": "invite:invite-1",
            "profile": completeness.PROFILE, "kind": "initial_sent", "purpose": "INVITE", "invitation": True,
            "verified": True, "occurred_at": "2026-08-01T00:00:00+00:00", "observed_at": "2026-08-01T00:00:00+00:00",
            "evidence": "unipile-read:2026-08-01", "raw": {}})
        self.port = FakePort()
        provider_id = self.port.add_person(completeness.PROFILE)
        if pending:
            self.port.add_invitation(provider_id)

    def port_cycle(self, now, port):
        client = Mock()
        client.call.side_effect = self.call
        with patch.object(outreach_provider, "_now", return_value=now.isoformat()), \
                patch("socket.socket.connect", side_effect=AssertionError("NO NETWORK")):
            return inactivity_closure.cycle(self.database, completeness.CONFIG, client, now, port=port)

    def test_pending_unipile_invitation_closes_on_one_complete_read(self):
        self.unipile_person()
        self.assertEqual(self.port_cycle(completeness.NOW, self.port)["closed"], 1)

    def test_unreadable_unipile_acceptance_never_closes(self):
        self.unipile_person()
        self.port.fail("lookup", "timeout", times=10)
        self.port_cycle(completeness.NOW, self.port)
        self.port_cycle(completeness.NOW + dt.timedelta(days=27), self.port)
        self.assertIsNone(engagement.closure_status(self.database, completeness.PROFILE))

    def test_without_a_port_the_two_read_rule_still_applies(self):
        self.unipile_person()
        first = self.port_cycle(completeness.NOW, None)
        self.assertEqual(first["closed"], 0, first)


if __name__ == "__main__":
    unittest.main()
