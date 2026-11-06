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
                           "receipt_message": None, "route": "EXISTING_THREAD",
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

    # Check before resend (owner, 2026-11-06). A start_chat that timed out names no chat, so absence
    # cannot be read in it (owner pick, review Q1 a): never resent; after 3 checks it ends for good.
    def test_start_chat_with_no_known_chat_is_never_resent_and_ends_after_three_checks(self):
        self.port.fail("start_chat", "timeout")
        self.first_followup()
        self.assertEqual(self.state(1), "UNCERTAIN_NO_RETRY")
        for unused in range(8):
            self.now += dt.timedelta(hours=5)
            self.run_cycle()
        self.assertEqual(self.port.calls.count("start_chat"), 1)
        self.assertEqual(self.state(1), "UNCERTAIN_NO_RETRY")
        result = json.loads(self.db.execute("SELECT result FROM outreach_actions WHERE slot='FOLLOWUP:1'").fetchone()[0])
        self.assertEqual(result["check_final"]["checks"], 3)
        self.assertFalse(outreach.checking_before_resend({"purpose": "FOLLOWUP"}, "UNCERTAIN_NO_RETRY", result))
        read = self.db.execute("SELECT state FROM outreach_read_retries WHERE phase='RECONCILIATION'").fetchone()[0]
        self.assertEqual(read, "COMPLETE")   # final: never read or sent again

    def test_absence_is_not_read_before_linkedin_had_time_to_catch_up(self):
        self.port.fail("start_chat", "timeout")
        self.first_followup()
        self.now += dt.timedelta(minutes=5)
        self.run_cycle()
        self.assertEqual(self.port.calls.count("start_chat"), 1)
        self.assertEqual(self.state(1), "UNCERTAIN_NO_RETRY")

    def test_a_message_of_ours_with_other_text_since_the_request_counts_as_sent(self):
        self.port.fail("start_chat", "timeout")
        self.first_followup()
        self.port.add_chat(self.person, [(True, "A message the owner wrote by hand.")])
        for unused in range(3):
            self.now += dt.timedelta(hours=5)
            self.run_cycle()
        self.assertEqual(self.port.calls.count("start_chat"), 1)
        self.assertEqual(self.state(1), "CONFIRMED_SENT")

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

    # Check before resend (owner, 2026-11-06) for a follow-up sent into a known chat (send_message).
    def unclear_first(self, times=1):
        self.now = engagement.business_days_after(INVITED, 3)
        self.accept_with_note_in_chat()
        self.port.fail("send_message", "timeout", times=times)
        self.run_cycle()
        self.assertEqual(self.state(1), "UNCERTAIN_NO_RETRY")

    def test_not_landed_is_checked_then_sent_once_more(self):
        self.unclear_first()
        self.settle()
        self.assertEqual(self.port.calls.count("send_message"), 2)
        self.assertEqual(self.state(1), "ACKNOWLEDGED_UNCONFIRMED")
        self.settle()
        self.assertEqual(self.state(1), "CONFIRMED_SENT")
        self.assertEqual(self.port.calls.count("send_message"), 2)
        self.assertEqual(len(self.ours()), 2)   # the invitation note and one follow-up
        self.assertEqual(len(self.kinds("followup_sent")), 1)

    def test_landed_is_found_and_never_sent_twice(self):
        self.now = engagement.business_days_after(INVITED, 3)
        self.accept_with_note_in_chat()
        self.port.fail("send_message", "timeout_after_write")
        self.run_cycle()
        for unused in range(3):
            self.settle()
        self.assertEqual(self.state(1), "CONFIRMED_SENT")
        self.assertEqual(self.port.calls.count("send_message"), 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM outreach_action_claims WHERE action_id=("
                                         "SELECT action_id FROM outreach_actions WHERE slot='FOLLOWUP:1')").fetchone()[0], 1)

    def test_a_failed_check_sends_nothing_and_is_read_again_later(self):
        self.unclear_first()
        self.port.fail("chats", "timeout")
        self.settle()
        self.assertEqual(self.port.calls.count("send_message"), 1)
        self.settle()   # the next read, 15 minutes on (the claim's proof is still fresh)
        self.assertEqual(self.port.calls.count("send_message"), 2)

    def test_an_incomplete_listing_never_shows_absence_and_is_read_again(self):
        self.unclear_first()
        self.port.fail("chats", "incomplete_page")
        self.settle()
        self.assertEqual(self.port.calls.count("send_message"), 1)
        read = self.db.execute("SELECT state FROM outreach_read_retries WHERE phase='RECONCILIATION'").fetchone()[0]
        self.assertEqual(read, "RETRY_WAIT")   # read again later, not held for good
        self.settle()   # the next read, 15 minutes on (the claim's proof is still fresh)
        self.assertEqual(self.port.calls.count("send_message"), 2)

    def test_attempt_three_is_sent_after_a_complete_absence_read_past_75_minutes(self):
        # Review N1: the resend's history is its own fresh complete read, not the claim's (1 hour old).
        claimed = self.now = engagement.business_days_after(INVITED, 3)
        self.accept_with_note_in_chat()
        self.port.fail("send_message", "timeout", times=2)
        self.run_cycle()
        for unused in range(6):
            self.settle()
        self.assertEqual(self.port.calls.count("send_message"), 3)
        third = json.loads(self.db.execute("SELECT result FROM outreach_actions WHERE slot='FOLLOWUP:1'").fetchone()[0])
        self.assertGreater(engagement.timestamp(third["message_retries"][-1]["started_at"]) - claimed, dt.timedelta(minutes=75))
        self.now += dt.timedelta(hours=5)   # the next read on the read backoff
        self.run_cycle()
        self.assertEqual(self.state(1), "CONFIRMED_SENT")
        self.assertEqual(len(self.kinds("followup_sent")), 1)

    def test_never_written_row_delayed_past_75_minutes_is_sent_on_its_own_fresh_read(self):
        # Review F1: a row whose request was never written is sent later with its own complete chats read
        # as the history, so the delay never makes it stale.
        self.unclear_first()
        row = self.db.execute("SELECT action_id,result FROM outreach_actions WHERE slot='FOLLOWUP:1'").fetchone()
        result = json.loads(row["result"])
        result.pop("provider_request")
        with self.db:
            self.db.execute("UPDATE outreach_actions SET result=? WHERE action_id=?", (json.dumps(result), row["action_id"]))
        self.now += dt.timedelta(minutes=90)
        self.run_cycle()
        self.assertEqual(self.port.calls.count("send_message"), 2)
        self.assertEqual(self.state(1), "ACKNOWLEDGED_UNCONFIRMED")

    def test_never_written_followup_after_the_owner_wrote_by_hand_is_not_sent(self):
        # Review L1, follow-up: the same rule on the fresh_history path.
        self.unclear_first()
        row = self.db.execute("SELECT action_id,result FROM outreach_actions WHERE slot='FOLLOWUP:1'").fetchone()
        result = json.loads(row["result"])
        result.pop("provider_request")
        with self.db:
            self.db.execute("UPDATE outreach_actions SET result=? WHERE action_id=?", (json.dumps(result), row["action_id"]))
        self.now += dt.timedelta(minutes=30)
        next(iter(self.port.chat_log.values()))["messages"].append(
            {"is_sender": True, "text": "Hi, writing myself.", "sent_at": self.now.isoformat()})
        self.now += dt.timedelta(minutes=60)
        self.run_cycle()
        self.assertEqual(self.port.calls.count("send_message"), 1)
        self.assertEqual(self.state(1), "CANCELLED_BEFORE_SEND")
        result = json.loads(self.db.execute("SELECT result FROM outreach_actions WHERE slot='FOLLOWUP:1'").fetchone()[0])
        self.assertIn("VERIFIED_MESSAGE_ALREADY_SENT", result["provider_guard"]["gates"])
        self.assertIs(result["check_before_resend_cancelled"], True)

    def test_contact_replied_during_the_wait_ends_it_cancelled(self):
        self.unclear_first()
        chat = next(iter(self.port.chat_log))
        self.port.chat_log[chat]["messages"].append({"is_sender": False, "text": "Thanks, not now.", "sent_at": self.now.isoformat()})
        self.settle()
        self.assertEqual(self.port.calls.count("send_message"), 1)
        self.assertEqual(self.state(1), "CANCELLED_BEFORE_SEND")

    def test_three_attempts_then_it_stops(self):
        self.unclear_first(times=3)
        for unused in range(8):
            self.now += dt.timedelta(hours=5)
            self.run_cycle()
        self.assertEqual(self.port.calls.count("send_message"), 3)
        self.assertEqual(self.state(1), "CONFIRMED_NOT_SENT")
        self.assertEqual(self.kinds("followup_sent"), [])

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
        self.initial_adapter = adapter = outreach_agent.Adapter(ROOT, self.db, self.config, client, judge, port=self.port)
        self.initial_cycles()

    def initial_cycles(self):
        adapter = self.initial_adapter
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


class UnclearPortInitial(AfterPortInitial):
    """Check before resend for a direct message into an existing chat (INITIAL, send_message over
    outreach_agent): the same read-first rule as the follow-up."""

    def invite(self):
        self.port.fail("send_message", "timeout")
        super().invite()

    def initial_cycles(self):
        outreach.cycle(self.db, self.config, self.initial_adapter, self.now, purpose="INITIAL")
        self.assertEqual(self.initial_state(), "UNCERTAIN_NO_RETRY")

    def initial_state(self):
        return self.db.execute("SELECT state FROM outreach_actions WHERE action_id='initial-1'").fetchone()[0]

    def test_followup_one_goes_into_the_chat_over_the_port(self):
        pass   # covered by AfterPortInitial

    def drive(self, minutes):
        self.now += dt.timedelta(minutes=minutes)
        return outreach.cycle(self.db, self.config, self.initial_adapter, self.now, purpose="INITIAL")

    def test_not_shown_after_the_wait_is_sent_once_more_then_confirmed(self):
        self.assertEqual(self.port.calls.count("send_message"), 1)
        self.assertEqual(self.port.chat_log[self.chat]["messages"], [])
        self.drive(60)
        self.assertEqual(self.port.calls.count("send_message"), 2)
        self.drive(60)
        self.assertEqual(self.initial_state(), "CONFIRMED_SENT")
        self.assertEqual(len(self.port.chat_log[self.chat]["messages"]), 1)
        self.assertEqual(self.port.calls.count("send_message"), 2)

    def test_landed_send_is_found_and_never_sent_twice(self):
        # The first attempt reached LinkedIn after all (the chat shows it): confirmed by the read alone.
        self.port.chat_log[self.chat]["messages"].append(
            {"is_sender": True, "text": "Hi, I would like to talk about your platform work.", "sent_at": self.now.isoformat()})
        self.drive(60)
        self.drive(60)
        self.assertEqual(self.initial_state(), "CONFIRMED_SENT")
        self.assertEqual(self.port.calls.count("send_message"), 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM outreach_action_claims WHERE action_id='initial-1'").fetchone()[0], 1)


    TEXT = "Hi, I would like to talk about your platform work."

    def writes(self):
        return self.port.calls.count("send_message")

    def guard(self):
        result = json.loads(self.db.execute("SELECT result FROM outreach_actions WHERE action_id='initial-1'").fetchone()[0])
        return result["provider_guard"]["gates"]

    def chat_messages(self):
        return self.port.chat_log[self.chat]["messages"]

    def test_contact_wrote_a_neutral_message_and_ours_is_absent_resent_once(self):
        self.chat_messages().append({"is_sender": False, "text": "Hello there.", "sent_at": self.now.isoformat()})
        self.drive(20)
        self.assertEqual(self.writes(), 2)
        self.drive(20)
        self.assertEqual(self.initial_state(), "CONFIRMED_SENT")
        self.assertEqual([item["text"] for item in self.chat_messages() if item["is_sender"]], [self.TEXT])

    def test_contact_replied_during_the_wait_is_cancelled_not_stuck(self):
        self.chat_messages().append({"is_sender": False, "text": "Please stop.", "sent_at": self.now.isoformat()})
        engagement.record_event(self.db, engagement.unipile_message_event(
            self.chat, {"sender": "CORRESPONDENT", "body": "Please stop.", "createdAt": self.now.isoformat()},
            PROFILE, self.config["sender_id"], OWNER, self.now.isoformat()))
        self.drive(20)
        self.assertEqual(self.initial_state(), "CANCELLED_BEFORE_SEND")
        self.assertEqual(self.writes(), 1)
        self.assertIn("REPLY_REQUIRES_CONTEXT", self.guard())

    def test_two_copies_shown_count_as_sent(self):
        for unused in range(2):
            self.chat_messages().append({"is_sender": True, "text": self.TEXT, "sent_at": self.now.isoformat()})
        self.drive(20)
        self.drive(20)
        self.assertEqual(self.initial_state(), "CONFIRMED_SENT")
        self.assertEqual(self.writes(), 1)

    def test_no_saved_request_is_sent_once_through_the_first_send(self):
        result = json.loads(self.db.execute("SELECT result FROM outreach_actions WHERE action_id='initial-1'").fetchone()[0])
        result.pop("provider_request")   # nothing was written: the request is saved before the one write
        with self.db:
            self.db.execute("UPDATE outreach_actions SET result=? WHERE action_id='initial-1'", (json.dumps(result),))
        with patch("outreach_agent.port_send_gates", wraps=outreach_agent.port_send_gates) as gates:
            self.drive(20)
        gates.assert_called_once()
        self.assertEqual(self.writes(), 2)
        self.assertEqual(self.initial_state(), "ACKNOWLEDGED_UNCONFIRMED")
        self.drive(20)
        self.assertEqual(self.initial_state(), "CONFIRMED_SENT")
        self.assertEqual(self.writes(), 2)

    def test_never_written_row_waits_out_an_outage_then_is_sent_once(self):
        # Review F1: the fresh history read fails during the outage -> nothing sent, read again; after
        # it, more than 75 minutes on, it is sent once (not cancelled as stale).
        import unipile_port
        result = json.loads(self.db.execute("SELECT result FROM outreach_actions WHERE action_id='initial-1'").fetchone()[0])
        result.pop("provider_request")
        with self.db:
            self.db.execute("UPDATE outreach_actions SET result=? WHERE action_id='initial-1'", (json.dumps(result),))
        with patch.object(self.port, "chats", side_effect=unipile_port.UnipileHTTPError(503)):
            for unused in range(5):
                self.drive(20)
        self.assertEqual(self.writes(), 1)
        self.assertEqual(self.initial_state(), "UNCERTAIN_NO_RETRY")
        self.drive(4 * 60)   # the next read on the read backoff, after the outage
        self.assertEqual(self.writes(), 2)
        self.assertEqual(self.initial_state(), "ACKNOWLEDGED_UNCONFIRMED")

    def test_never_written_row_after_the_owner_wrote_by_hand_is_not_sent_and_shown(self):
        # Review L1: the fresh read shows a message from the user's own account since the approval.
        import unipile_port
        result = json.loads(self.db.execute("SELECT result FROM outreach_actions WHERE action_id='initial-1'").fetchone()[0])
        result.pop("provider_request")
        with self.db:
            self.db.execute("UPDATE outreach_actions SET result=? WHERE action_id='initial-1'", (json.dumps(result),))
        with patch.object(self.port, "chats", side_effect=unipile_port.UnipileHTTPError(503)):
            self.drive(20)
            self.chat_messages().append({"is_sender": True, "text": "Hi, writing myself.", "sent_at": self.now.isoformat()})
            self.drive(20)
        self.drive(4 * 60)
        self.assertEqual(self.writes(), 1)
        self.assertEqual(self.initial_state(), "CANCELLED_BEFORE_SEND")
        self.assertEqual(self.guard(), ["VERIFIED_MESSAGE_ALREADY_SENT"])
        result = json.loads(self.db.execute("SELECT result FROM outreach_actions WHERE action_id='initial-1'").fetchone()[0])
        self.assertIs(result["check_before_resend_cancelled"], True)

    def test_another_chat_read_only_in_part_shows_no_absence(self):
        # Review S1: any chat of the person not read in full, not only the target, means no resend.
        import linkedin_port
        other = self.port.add_chat(self.person, [(False, "An older thread.")])
        original = self.port.messages

        def partial(chat_id):
            thread = original(chat_id)
            return linkedin_port.Thread(chat_id, thread.messages, False, thread.raw) if chat_id == other else thread

        with patch.object(self.port, "messages", side_effect=partial):
            for unused in range(3):
                self.drive(20)
        self.assertEqual(self.writes(), 1)
        self.drive(60)   # the next read on the read backoff, every chat read in full
        self.assertEqual(self.writes(), 2)

    def test_an_ended_row_is_never_read_or_acted_on_again(self):
        # Review S3: a row with check_final returns its final state without a read or a send.
        result = json.loads(self.db.execute("SELECT result FROM outreach_actions WHERE action_id='initial-1'").fetchone()[0])
        result["check_final"] = {"reason": "UNDECIDED_AFTER_7_DAYS"}
        with self.db:
            self.db.execute("UPDATE outreach_actions SET result=? WHERE action_id='initial-1'", (json.dumps(result),))
        action = json.loads(self.db.execute("SELECT payload FROM outreach_actions WHERE action_id='initial-1'").fetchone()[0])
        calls = len(self.port.calls)
        answer = followup.port_receipt(self.initial_adapter, action, "unipile-initial:", self.config)
        self.assertEqual(answer, {"state": followup.FINAL, "final_state": "UNCERTAIN_NO_RETRY"})
        self.assertEqual(len(self.port.calls), calls)

    def test_a_403_during_the_check_sends_nothing_and_is_read_again(self):
        import unipile_port
        with patch.object(self.port, "chats", side_effect=unipile_port.UnipileHTTPError(403)):
            self.drive(20)
        self.assertEqual(self.writes(), 1)
        read = self.db.execute("SELECT state FROM outreach_read_retries WHERE phase='RECONCILIATION'").fetchone()[0]
        self.assertEqual(read, "RETRY_WAIT")
        self.drive(20)
        self.assertEqual(self.writes(), 2)

    def test_sender_changed_is_cancelled(self):
        self.initial_adapter.config = dict(self.config, sender_id=456)
        self.drive(20)
        self.assertEqual(self.initial_state(), "CANCELLED_BEFORE_SEND")
        self.assertEqual(self.writes(), 1)
        guard = json.loads(self.db.execute("SELECT result FROM outreach_actions WHERE action_id='initial-1'").fetchone()[0])
        self.assertIn("SENDER_CHANGED", guard["provider_guard"]["gates"])

    def test_suppression_added_during_the_wait_is_cancelled(self):
        with self.db:
            # The send left its own handoff record on this identity; the user now excludes the person.
            self.db.execute("INSERT OR REPLACE INTO suppressions VALUES (?,?)", (profile_key(PROFILE), "Excluded by the user"))
        self.drive(20)
        self.assertEqual(self.initial_state(), "CANCELLED_BEFORE_SEND")
        self.assertEqual(self.writes(), 1)
        self.assertEqual(self.guard(), ["SUPPRESSED_CONTACT"])
        result = json.loads(self.db.execute("SELECT result FROM outreach_actions WHERE action_id='initial-1'").fetchone()[0])
        self.assertIs(result["check_before_resend_cancelled"], True)   # shown on Networking as "Not sent: ..."

    def read_row(self):
        return self.db.execute("SELECT state,next_at FROM outreach_read_retries WHERE action_id='initial-1' "
                               "AND phase='RECONCILIATION'").fetchone()

    def test_a_held_legacy_read_is_reopened_once_and_then_checked(self):
        # Review D-2: a read earlier code held at INVALID_PROOF is reopened; the reopen itself sends nothing.
        self.drive(5)   # too soon to resend: the read is now on record
        with self.db:
            self.db.execute("""UPDATE outreach_read_retries SET state='INVALID_PROOF',attempts=0,next_at=NULL,
                last_error='IncompletePage: INCOMPLETE_PAGE' WHERE action_id='initial-1' AND phase='RECONCILIATION'""")
            # Review T2: a held HeyReach INITIAL read and a held REPLY read are not reopened.
            for action_id, payload in (("heyreach-1", {"action_id": "heyreach-1", "purpose": "INITIAL", "provider": "heyreach"}),
                                       ("reply-1", {"action_id": "reply-1", "purpose": "REPLY", "provider": "unipile"})):
                self.db.execute("INSERT INTO outreach_actions(action_id,identity,slot,payload,payload_hash,state,result) "
                                "VALUES (?,?,?,?,?,'UNCERTAIN_NO_RETRY',?)",
                                (action_id, "linkedin.com/in/" + action_id, action_id, json.dumps(payload), "h",
                                 json.dumps({"reply_send_errors": []})))
                self.db.execute("INSERT INTO outreach_read_retries(action_id,phase,state,updated_at) "
                                "VALUES (?,'RECONCILIATION','INVALID_PROOF',?)", (action_id, self.now.isoformat()))
        self.assertEqual(outreach.reopen_check_before_resend_reads(self.db, self.now), ["initial-1"])
        held = dict(self.db.execute("SELECT action_id,state FROM outreach_read_retries WHERE action_id IN ('heyreach-1','reply-1')"))
        self.assertEqual(held, {"heyreach-1": "INVALID_PROOF", "reply-1": "INVALID_PROOF"})
        self.assertEqual(outreach.reopen_check_before_resend_reads(self.db, self.now), [])
        self.assertEqual(tuple(self.read_row()), ("RETRY_WAIT", None))
        self.assertEqual(self.writes(), 1)
        with self.db:
            self.db.execute("UPDATE outreach_read_retries SET state='ADAPTER_UNAVAILABLE' WHERE action_id='initial-1'")
        self.drive(20)
        self.assertEqual(self.writes(), 2)

    def test_still_undecided_after_seven_days_ends_without_a_resend(self):
        import unipile_port
        with patch.object(self.port, "chats", side_effect=unipile_port.UnipileHTTPError(503)):
            for unused in range(17):
                self.drive(12 * 60)
        self.assertEqual(self.writes(), 1)
        self.assertEqual(self.initial_state(), "UNCERTAIN_NO_RETRY")
        result = json.loads(self.db.execute("SELECT result FROM outreach_actions WHERE action_id='initial-1'").fetchone()[0])
        self.assertEqual(result["check_final"]["reason"], "UNDECIDED_AFTER_7_DAYS")
        self.assertEqual(self.read_row()[0], "COMPLETE")
        self.drive(12 * 60)
        self.assertEqual(self.writes(), 1)

    def test_nothing_written_is_checking_then_did_not_go_out_after_seven_days(self):
        # Review D-4: no request was ever written; it shows as checking and ends CONFIRMED_NOT_SENT.
        result = json.loads(self.db.execute("SELECT result FROM outreach_actions WHERE action_id='initial-1'").fetchone()[0])
        result.pop("provider_request")
        with self.db:
            self.db.execute("UPDATE outreach_actions SET result=? WHERE action_id='initial-1'", (json.dumps(result),))
        action = json.loads(self.db.execute("SELECT payload FROM outreach_actions WHERE action_id='initial-1'").fetchone()[0])
        self.assertTrue(outreach.checking_before_resend(action, "UNCERTAIN_NO_RETRY", result))
        self.drive(8 * 24 * 60)
        self.assertEqual(self.initial_state(), "CONFIRMED_NOT_SENT")
        self.assertEqual(self.writes(), 1)

    def test_two_readers_of_one_request_started_row_send_once(self):
        # Review X-1: the resend's CAS includes the stored result, so a second reader holding the same
        # snapshot never sends too.
        self.now += dt.timedelta(minutes=20)
        with self.db:
            self.db.execute("UPDATE outreach_actions SET state='REQUEST_STARTED' WHERE action_id='initial-1'")
        state, raw = self.db.execute("SELECT state,result FROM outreach_actions WHERE action_id='initial-1'").fetchone()
        claim = dict(self.db.execute("SELECT * FROM outreach_action_claims WHERE action_id='initial-1'").fetchone())
        action = json.loads(claim["payload"])
        observation = followup.port_observation(self.port, PROFILE)
        request = json.loads(raw)["provider_request"]
        original, inner = self.port.send_message, []

        def send_while_another_reader_runs(chat_id, text):
            if not inner:
                inner.append(followup._resend_after_check(self.initial_adapter, action, claim, state, raw, json.loads(raw),
                                                          request, observation, self.now))
            return original(chat_id, text)

        with patch.object(self.port, "send_message", side_effect=send_while_another_reader_runs):
            followup._resend_after_check(self.initial_adapter, action, claim, state, raw, json.loads(raw), request,
                                         observation, self.now)
        self.assertEqual(inner, [None])
        self.assertEqual(self.writes(), 2)   # the first send and exactly one resend
        self.assertEqual([item["text"] for item in self.chat_messages() if item["is_sender"]], [self.TEXT])

    def test_target_chat_missing_from_the_listing_is_never_resent(self):
        del self.port.chat_log[self.chat]
        for unused in range(4):
            self.drive(20)
        self.assertEqual(self.writes(), 1)
        self.assertEqual(self.initial_state(), "UNCERTAIN_NO_RETRY")


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


class OursSince(unittest.TestCase):
    """Review L1/L3: a fresh read shows a message the user may have written since the approval."""
    proof = {"checked_at": "2026-11-06T10:00:00Z"}

    def read(self, sender, at="2026-11-06T11:00:00Z"):
        return {"observations": [{"messages": [{"sender": sender, "createdAt": at}]}]}

    def test_ours_after_the_approval_counts(self):
        self.assertTrue(followup.ours_since(self.read("ME"), self.proof))

    def test_a_message_whose_sender_cannot_be_read_counts(self):
        self.assertTrue(followup.ours_since(self.read(None), self.proof))

    def test_the_contacts_message_and_older_messages_do_not(self):
        self.assertFalse(followup.ours_since(self.read("CORRESPONDENT"), self.proof))
        self.assertFalse(followup.ours_since(self.read("ME", "2026-11-06T09:00:00Z"), self.proof))
