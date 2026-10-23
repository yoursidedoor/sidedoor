import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import copy
import json
import unittest
from unittest.mock import Mock, patch

import engagement
import orchestrator
import test_engagement
import test_heyreach_schema as schema_fixture
import test_outreach_promotion as promotion_fixture


class ConnectorInboxRuntimeTests(unittest.TestCase):
    setUp = test_engagement.EngagementTests.setUp

    def configured(self):
        return {"sender_id": 424242, "sender_profile": schema_fixture.SENDER,
                "engagement_enabled": True, "engagement_max_pages": 1, "campaign_id": 700001}

    def room(self):
        return schema_fixture.room([schema_fixture.message("ME", "Synthetic existing message", "2026-01-01T12:00:00.000Z")])

    def test_actual_wiring_persists_replay_without_false_delivery(self):
        room = self.room()
        client = Mock()
        client.call.side_effect = lambda operation, arguments: (
            {"totalCount": 1, "items": [copy.deepcopy(room)]} if operation == "get_conversations_v2" else copy.deepcopy(room))
        with patch.object(orchestrator, "HeyReach", return_value=client):
            first = orchestrator.engagement_cycle(self.database, self.configured())
            again = orchestrator.engagement_cycle(self.database, self.configured())
        self.assertEqual(first["items"][0]["inserted"], 1)
        self.assertEqual(again["items"][0]["inserted"], 0)
        self.assertEqual(self.database.execute("SELECT kind FROM engagement_events").fetchone()[0], "outbound_observed")
        self.assertEqual(orchestrator.known_provider_lead(self.database, schema_fixture.PROFILE, 424242), "synthetic-member")
        self.assertIsNone(orchestrator.known_provider_lead(self.database, schema_fixture.PROFILE, 111))
        self.assertIsNone(orchestrator.known_provider_lead(self.database, "https://linkedin.com/in/someone-else", 424242))

    def test_authenticated_member_id_lookup_avoids_broken_url_filter(self):
        room = self.room()
        decoder = schema_fixture.heyreach_schema.Decoder(424242, schema_fixture.SENDER)
        observed = decoder.decode_chatroom(room, decoder.decode_conversation(room))[0]
        engagement.record_event(self.database, observed)
        with self.database:
            self.database.execute("INSERT INTO history VALUES (?,?,?,?,?)", (
                orchestrator.profile_key(schema_fixture.PROFILE), "Synthetic Person", "Mock Employer", schema_fixture.PROFILE, "MOCK"))
        client = Mock()
        client.call.return_value = {"totalCount": 1, "items": [room]}
        result = orchestrator.observe(self.database, self.configured(), client)
        client.call.assert_called_with("get_conversations_v2", {"linkedInAccountIds": [424242], "leadLinkedInId": "synthetic-member", "limit": 100})
        self.assertEqual(result["contacts"][0]["status"], "REVIEW_CONVERSATIONS")
        self.assertFalse(result["contacts"][0]["history_complete"])
        room["correspondentProfile"]["profileUrl"] = "https://linkedin.com/in/not-this-person"
        result = orchestrator.observe(self.database, self.configured(), client)
        self.assertEqual(result["contacts"][0]["status"], "UNKNOWN")

    def test_disabled_reader_does_not_access_credentials(self):
        with patch.object(orchestrator, "HeyReach") as provider:
            self.assertEqual(orchestrator.engagement_cycle(self.database, {})["state"], "DISABLED")
        provider.assert_not_called()


class ConnectorPromotionRuntimeTests(unittest.TestCase):
    setUp = promotion_fixture.PromotionTests.setUp
    make_draft = promotion_fixture.PromotionTests.make_draft
    accept = promotion_fixture.PromotionTests.accept

    def test_actual_caller_promotes_before_any_execution_and_restarts(self):
        self.accept()
        with patch.object(orchestrator, "utcnow", return_value=promotion_fixture.NOW):
            result = orchestrator.draft_promotion_cycle(self.database, {})
            again = orchestrator.draft_promotion_cycle(self.database, {})
        self.assertEqual((result["queued"], again["queued"]), (1, 0))
        self.assertEqual(again["mappings"], result["mappings"])
        self.assertNotIn("strategy_bindings", result)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM outreach_action_claims").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
