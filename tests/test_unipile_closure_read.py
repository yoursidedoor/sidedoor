"""The 27-day closure read of a Unipile person comes from the port (owner, 2026-10-17: "sure"):
a person who got a follow-up through Unipile (a saved Unipile chat id) closes at the due date on
one complete port read; a reply in the chat resets the clock; an incomplete chats or messages
read, or an invalid session, never closes. HeyReach is not read for such a person."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

import engagement
import inactivity_closure
import outreach_provider
from linkedin_port import FakePort

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_inactivity_read_completeness as completeness


PROFILE = completeness.PROFILE
NOW = completeness.NOW
SENT = "2026-08-01T00:00:00+00:00"
ACCEPTED = "2026-08-02T00:00:00+00:00"
FOLLOWED = "2026-08-10T00:00:00+00:00"


class UnipileClosureRead(unittest.TestCase):
    setUp = completeness.ZeroRoomReadCompleteness.setUp

    def followed_up(self, inbound_at=None, raw=None):
        with self.database:
            self.database.execute("DELETE FROM engagement_events")
        for source_id, kind, at, extra in (("invite:invite-1", "initial_sent", SENT, {"purpose": "INVITE", "invitation": True}),
                                           ("acceptance:alice", "accepted", ACCEPTED, {}),
                                           ("followup:f-1", "followup_sent", FOLLOWED, {"purpose": "FOLLOWUP"})):
            engagement.record_event(self.database, dict({"source": "unipile", "source_id": source_id, "profile": PROFILE,
                "kind": kind, "verified": True, "occurred_at": at, "observed_at": at,
                "evidence": "unipile-read:" + at, "raw": (raw or {"conversation_id": "chat-u1"}) if kind == "followup_sent" else {}}, **extra))
        self.port = FakePort()
        provider_id = self.port.add_person(PROFILE, distance=1)
        messages = [(True, "Hi again, following up.")] + ([(False, "Thanks, let's talk.")] if inbound_at else [])
        self.port.add_chat(provider_id, messages, chat_id="chat-u1")
        for message, at in zip(self.port.chat_log["chat-u1"]["messages"], [FOLLOWED, inbound_at]):
            message["sent_at"] = at

    def cycle(self, now):
        self.client = Mock()
        self.client.call.side_effect = AssertionError("HeyReach must not be read for a Unipile person")
        with patch.object(outreach_provider, "_now", return_value=now.isoformat()), \
                patch("socket.socket.connect", side_effect=AssertionError("NO NETWORK")):
            return inactivity_closure.cycle(self.database, completeness.CONFIG, self.client, now, port=self.port)

    def test_followed_up_person_closes_on_one_complete_port_read(self):
        self.followed_up()
        result = self.cycle(NOW)
        self.assertEqual(result["closed"], 1, result)
        self.assertIsNotNone(engagement.closure_status(self.database, PROFILE))
        self.assertEqual((self.port.calls.count("chats"), self.port.calls.count("messages")), (1, 1))
        self.client.call.assert_not_called()

    def test_new_inbound_in_the_chat_resets_the_clock(self):
        self.followed_up(inbound_at=(NOW - dt.timedelta(days=2)).isoformat())
        result = self.cycle(NOW)
        self.assertEqual(result["closed"], 0, result)
        self.assertIsNone(engagement.closure_status(self.database, PROFILE))
        inbound = [event for event in engagement.events_for(self.database, PROFILE) if event["kind"] == "inbound"]
        self.assertEqual([(event["source"], event["raw"]["text"]) for event in inbound], [("unipile", "Thanks, let's talk.")])
        self.assertEqual(inactivity_closure.plan(self.database, PROFILE, NOW)["state"], "ACTIVE")
        self.cycle(NOW + dt.timedelta(hours=2))  # the same message read again is one event
        self.assertEqual(sum(event["kind"] == "inbound" for event in engagement.events_for(self.database, PROFILE)), 1)

    def assert_never_closes(self, method, kind, blocker):
        self.followed_up()
        self.port.fail(method, kind, times=10)
        result = self.cycle(NOW)
        self.assertEqual(result["closed"], 0, result)
        self.assertEqual(result["items"][0]["blocker"], blocker, result)
        self.assertIsNone(engagement.closure_status(self.database, PROFILE))

    def test_incomplete_chat_listing_never_closes(self):
        self.assert_never_closes("chats", "partial_page", "UNIPILE_READ_INCOMPLETE_PAGE")

    def test_incomplete_messages_read_never_closes(self):
        self.assert_never_closes("messages", "incomplete_page", "UNIPILE_READ_INCOMPLETE_PAGE")

    def test_invalid_session_never_closes(self):
        self.assert_never_closes("account", "invalid_session", "UNIPILE_READ_SESSION_INVALID")

    def test_saved_chat_missing_from_the_read_never_closes(self):
        self.followed_up()
        del self.port.chat_log["chat-u1"]
        result = self.cycle(NOW)
        self.assertEqual(result["closed"], 0, result)
        self.assertEqual(result["items"][0]["blocker"], "SAVED_CONVERSATION_NOT_IN_READ", result)


    def test_start_chat_followup_chat_missing_from_the_read_never_closes(self):
        """Review 2B: a start_chat follow-up saves its chat as chat_id with conversation_id None."""
        self.followed_up(raw={"conversation_id": None, "chat_id": "chat-u1"})
        del self.port.chat_log["chat-u1"]
        result = self.cycle(NOW)
        self.assertEqual(result["closed"], 0, result)
        self.assertEqual(result["items"][0]["blocker"], "SAVED_CONVERSATION_NOT_IN_READ", result)


if __name__ == "__main__":
    unittest.main()
