"""E3.T3 remainder: the Unipile inbox read into engagement events (reply review)."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import dataclasses
import datetime as dt
import json

import applications
import conversion
import engagement
import inactivity_closure
import orchestrator
import outreach
import pipeline_routing as routing
import research_workflow as research
import sequence
from orchestrator import profile_key
from test_sequence import ALICE, Base
from linkedin_port import FakePort
import manual_reply_execution as manual
import unittest
import unittest.mock
import synthetic_resume  # noqa: F401 (synthetic designated resume)


ME = "https://www.linkedin.com/in/me"
CONFIG = {"engagement_enabled": True, "sender_id": 7, "sender_profile": ME, "linkedin_provider": "unipile"}
NOTE = "Hi Alice, a short note."


class Fixture(Base):
    def invited(self):
        """Alice's INVITE confirmed by a read, then accepted; her chat holds our note and her reply."""
        claimed = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)).isoformat()
        action_id = self.action()
        self.db.execute("UPDATE outreach_actions SET claimed_at=? WHERE action_id=?", (claimed, action_id))
        self.db.commit()
        sequence.send_invite(self.db, self.port, action_id)
        sequence.reconcile_invite(self.db, self.port, action_id)
        self.port.accept(self.alice)
        sequence.record_acceptance(self.db, self.port, ALICE)
        return self.port.add_chat(self.alice, [(True, NOTE), (False, "Thanks Alice here, happy to chat.")])

    def read(self):
        return orchestrator.unipile_engagement_cycle(self.db, CONFIG, self.port)


class UnipileInbox(Fixture):
    def test_inbox_records_reply_under_the_invited_person_and_stops_outreach(self):
        chat = self.invited()
        stranger = self.port.add_person("https://www.linkedin.com/in/stranger")
        self.port.add_chat(stranger, [(False, "Hello from a stranger")])
        result = self.read()["items"][0]
        self.assertEqual((result["state"], result["inserted"], result.get("unresolved_chats")), ("COMPLETE", 2, 1))
        kinds = [(event["kind"], event["raw"].get("conversation_id")) for event in engagement.events_for(self.db, ALICE)
                 if event["source_id"].startswith("unipile-message:")]
        self.assertEqual(sorted(kinds), [("inbound", chat), ("outbound_observed", chat)])
        self.assertEqual(engagement.stop_state(self.db, ALICE), "REPLY_REQUIRES_CONTEXT")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM workflow_tasks WHERE worker='engagement_review'").fetchone()[0], 1)
        # Our invitation note in the chat is reconciled to the INVITE, so it does not hold follow-ups.
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM engagement_outbound_reconciliations").fetchone()[0], 1)
        # A later read of the same messages records nothing new (only the observation time differs).
        self.assertEqual(self.read()["items"][0]["inserted"], 0)
        self.assertEqual(sequence.sequence_view(self.db, ALICE)["stage"], "REPLIED")
        # Her reply has the same basis as a campaign invitee's (the reply worker's skip check and send gate).
        self.assertEqual(outreach.reply_conversation(self.db, ALICE), "HARNESS_OUTREACH")

    def closure_read(self):
        # The spelling refresh_activity passes: "https://" + inactivity_closure.key(profile).
        read = inactivity_closure._unipile_read(self.port, "https://" + inactivity_closure.key(ALICE),
                                                dt.datetime.now(dt.timezone.utc), 7, ME)
        for event in read[5]:
            engagement.record_event(self.db, event)

    def inbound(self):
        return [event for event in engagement.events_for(self.db, ALICE) if event["kind"] == "inbound"]

    def test_closure_read_then_inbox_read_make_one_event(self):
        chat = self.invited()
        self.closure_read()
        result = self.read()["items"][0]
        self.assertEqual((result["state"], result["inserted"]), ("COMPLETE", 1))  # only our note is new
        self.assertEqual([event["raw"]["conversation_id"] for event in self.inbound()], [chat])

    def test_inbox_read_then_closure_read_make_one_event(self):
        chat = self.invited()
        self.assertEqual(self.read()["items"][0]["inserted"], 2)
        self.closure_read()  # records nothing new and does not raise
        self.assertEqual([event["raw"]["conversation_id"] for event in self.inbound()], [chat])

    def test_message_created_during_the_page_read_is_recorded_not_held(self):
        chat = self.invited()
        read = self.port.messages

        def late(chat_id):
            if chat_id == chat:
                self.port.chat_log[chat]["messages"].append(
                    {"is_sender": False, "text": "One more thing.", "sent_at": dt.datetime.now(dt.timezone.utc).isoformat()})
            return read(chat_id)
        self.port.messages = late
        result = self.read()["items"][0]
        self.assertEqual((result["inserted"], result.get("undecodable")), (3, None))
        self.assertEqual(len(self.inbound()), 2)

    def test_reply_reaches_reply_review(self):
        self.invited()
        self.read()
        for module in (applications, research, conversion, routing):
            module.initialize(self.db)
        later = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=1)).isoformat()
        result = routing.cycle(self.db, {"pipeline_routing": {"account_ids": [7], "designated_resume_sha256": "a" * 64}}, later)
        # The same refusal a HeyReach message meets (quoted/forwarded unknown): every check before it passed.
        self.assertIn("QUOTED_OR_FORWARDED_MESSAGE_REQUIRES_REVIEW", json.dumps(result["items"]))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM workflow_tasks WHERE worker='pipeline_routing_review'").fetchone()[0], 1)

    def test_chat_on_another_account_or_invalid_session_is_not_read(self):
        self.invited()
        listing = self.port.chats
        self.port.chats = lambda cursor=None, person=None: dataclasses.replace(listing(cursor, person), items=tuple(
            dataclasses.replace(chat, raw=dict(chat.raw, account_id="other")) for chat in listing(cursor, person).items))
        self.assertEqual(self.read()["items"][0]["undecodable"][0]["held"], True)
        self.port.chats = listing
        self.port.fail("account", "invalid_session")
        self.assertEqual(self.read()["items"][0]["state"], "BLOCKED")
        self.assertEqual([event for event in engagement.events_for(self.db, ALICE) if event["kind"] == "inbound"], [])


class UnipileReplyFixture(manual.Fixture):
    """The scheduled reply path (manual_reply_execution.Fixture) with the inbound read from a Unipile chat
    and the reply sent over a FakePort; the HeyReach client must never be asked to send."""

    def __init__(self):
        super().__init__()
        self.config = dict(self.config, linkedin_provider="unipile")
        self.port = FakePort(own_profile=self.config["sender_profile"])
        self.contact = self.port.add_person(manual.PROFILE, distance=1)

    def seed(self, body="Hello!\nCould you tell me more?"):
        with self.clock():
            self.chat = self.port.add_chat(self.contact, [(False, body)])
            thread = self.port.messages(self.chat)
            event = engagement.unipile_message_event(self.chat, engagement.port_message(self.chat, thread.messages[0]),
                                                     manual.PROFILE, 123, self.config["sender_profile"], self.now.isoformat())
        engagement.record_event(self.database, event)
        routing.cycle(self.database, self.config, self.now.isoformat())
        self.database.commit()
        self.unknowns[event["source_id"]] = []
        return event

    def cycle(self, seconds=0):
        with unittest.mock.patch.object(orchestrator, "linkedin_port_for", return_value=self.port):
            return super().cycle(seconds)

    def ours(self):
        return [message for message in self.port.chat_log[self.chat]["messages"] if message["is_sender"]]


class UnipileReply(unittest.TestCase):
    def test_reply_is_inspected_sent_and_confirmed_over_the_port(self):
        with manual.isolated_boundaries(), UnipileReplyFixture() as fixture:
            fixture.seed()
            fixture.cycle()
            action = fixture.actions()[0]
            self.assertEqual((json.loads(action["payload"])["provider"], action["state"]),
                             ("unipile", "ACKNOWLEDGED_UNCONFIRMED"))
            fixture.cycle(seconds=60)
            action = fixture.actions()[0]
            self.assertEqual(action["state"], "CONFIRMED_SENT")
            self.assertEqual([message["text"] for message in fixture.ours()], [manual.TEXT])
            self.assertEqual((fixture.port.calls.count("send_message"), fixture.requests, fixture.reads), (1, [], []))
            receipt = json.loads(action["result"])
            self.assertTrue(receipt["receipt_id"].startswith("unipile-observed-sha256:"))
            self.assertEqual(engagement.events_for(fixture.database, manual.PROFILE)[-1]["source"], "unipile")

    def test_errored_send_that_landed_is_confirmed_from_the_chat_not_resent(self):
        with manual.isolated_boundaries(), UnipileReplyFixture() as fixture:
            fixture.seed()
            fixture.port.fail("send_message", "timeout_after_write")
            fixture.cycle()
            self.assertEqual(fixture.actions()[0]["state"], "UNCERTAIN_NO_RETRY")
            fixture.cycle(seconds=3600)
            action = fixture.actions()[0]
            self.assertEqual((action["state"], json.loads(action["result"])["basis"]),
                             ("CONFIRMED_SENT", "REPLY_RETRY_THREAD_CHECK"))
            self.assertEqual(len(fixture.ours()), 1)

    def test_errored_send_absent_from_the_chat_is_resent_once_due(self):
        with manual.isolated_boundaries(), UnipileReplyFixture() as fixture:
            fixture.seed()
            fixture.port.fail("send_message", "timeout")
            fixture.cycle()
            fixture.cycle(seconds=599)
            self.assertEqual(fixture.port.calls.count("send_message"), 1)
            fixture.cycle(seconds=1)
            self.assertEqual((fixture.port.calls.count("send_message"), len(fixture.ours())), (2, 1))
            fixture.cycle(seconds=1)
            self.assertEqual(fixture.actions()[0]["state"], "CONFIRMED_SENT")


if __name__ == "__main__":
    unittest.main()
