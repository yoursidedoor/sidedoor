import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock

import engagement
import orchestrator


NOW = dt.datetime(2026, 9, 21, 17, tzinfo=dt.timezone.utc)
PROFILE = "https://www.linkedin.com/in/mock-contact/"


def event(kind="inbound", source_id="message-1", occurred=None, **extra):
    return {"source": "heyreach", "source_id": source_id, "profile": PROFILE,
            "kind": kind, "occurred_at": (occurred or NOW).isoformat(), "observed_at": NOW.isoformat(),
            "evidence": "mock-provider-evidence", "raw": {"text": "Exact observed text"}, "verified": True, **extra}


class EngagementTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "state").mkdir()
        self.database = orchestrator.connect(self.root)
        self.addCleanup(self.database.close)
        engagement.initialize(self.database)

    def test_raw_and_inference_distinct_reply_stops_and_handoff_atomic(self):
        observed = event(inference={"sentiment": "positive", "method": "mock-classifier-v1"})
        engagement.record_event(self.database, observed)
        row = self.database.execute("SELECT raw,inference FROM engagement_events").fetchone()
        self.assertEqual(json.loads(row[0]), observed["raw"])
        self.assertEqual(json.loads(row[1]), observed["inference"])
        self.assertEqual(engagement.next_action(self.database, PROFILE, NOW)["blocker"], "REPLY_REQUIRES_CONTEXT")
        self.assertEqual(self.database.execute("SELECT count(*) FROM workflow_tasks").fetchone()[0], 1)
        self.assertFalse(engagement.record_event(self.database, observed)["inserted"])
        self.assertEqual(self.database.execute("SELECT count(*) FROM workflow_tasks").fetchone()[0], 1)

    def test_optout_persists_across_restart_and_later_positive_reply(self):
        engagement.record_event(self.database, event(inference={"sentiment": "negative", "method": "mock"}))
        engagement.record_event(self.database, event(source_id="later", inference={"sentiment": "positive", "method": "mock"}))
        restarted = orchestrator.connect(self.root)
        self.addCleanup(restarted.close)
        self.assertEqual(engagement.next_action(restarted, PROFILE, NOW)["blocker"], "OPTED_OUT")

    def test_page_failure_rolls_back_cursor_messages_and_handoffs(self):
        client = Mock()
        invalid = event(source_id="bad")
        invalid.pop("raw")
        client.read_page.return_value = {"events": [event(), invalid], "next_cursor": "2", "done": False}
        result = engagement.cycle(self.database, {"engagement_enabled": True}, client, NOW)
        self.assertEqual(result["items"][0]["state"], "BLOCKED")
        self.assertIsNone(self.database.execute("SELECT cursor FROM engagement_cursors").fetchone()[0])
        for table in ("engagement_events", "engagement_stops", "workflow_tasks"):
            self.assertEqual(self.database.execute("SELECT count(*) FROM " + table).fetchone()[0], 0)

    def test_pagination_cursor_dedup_restart_and_new_reply(self):
        client = Mock()
        client.read_page.return_value = {"events": [event()], "next_cursor": "page-2", "done": False}
        config = {"engagement_enabled": True, "engagement_max_pages": 1}
        engagement.cycle(self.database, config, client, NOW)
        restarted = orchestrator.connect(self.root)
        self.addCleanup(restarted.close)
        client.read_page.return_value = {"events": [event(), event(source_id="message-2")], "next_cursor": None, "done": True}
        result = engagement.cycle(restarted, config, client, NOW)
        client.read_page.assert_called_with("heyreach", "page-2")
        self.assertEqual(result["items"][0]["inserted"], 1)
        self.assertEqual(restarted.execute("SELECT count(*) FROM engagement_events").fetchone()[0], 2)

    def test_unknown_source_blocks_only_it_and_default_disabled(self):
        client = Mock()
        engagement.cycle(self.database, {}, client, NOW)
        client.read_page.assert_not_called()
        def read(source, cursor):
            if source == "bad":
                raise NotImplementedError()
            return {"events": [event()], "next_cursor": None, "done": True}
        client.read_page.side_effect = read
        result = engagement.cycle(self.database, {"engagement_enabled": True, "engagement_sources": ["bad", "heyreach"]}, client, NOW)
        self.assertEqual([item["state"] for item in result["items"]], ["BLOCKED", "COMPLETE"])

    def test_acceptance_window_and_no_double_first_message(self):
        accepted = NOW - dt.timedelta(hours=12)
        engagement.record_event(self.database, event("accepted", occurred=accepted))
        self.assertEqual(engagement.next_action(self.database, PROFILE, NOW - dt.timedelta(seconds=1))["state"], "WAITING")
        self.assertEqual(engagement.next_action(self.database, PROFILE, NOW)["purpose"], "FIRST_MESSAGE")
        self.assertEqual(engagement.next_action(self.database, PROFILE, NOW + dt.timedelta(hours=12))["state"], "DUE")
        self.assertEqual(engagement.next_action(self.database, PROFILE, NOW + dt.timedelta(hours=12, seconds=1))["blocker"], "ACCEPTANCE_WINDOW_EXPIRED")
        engagement.record_event(self.database, event("initial_sent", "sent"))
        self.assertEqual(engagement.next_action(self.database, PROFILE, NOW)["purpose"], "FOLLOWUP")

    def test_three_then_five_business_days_and_max_two(self):
        friday = dt.datetime(2026, 9, 18, 17, tzinfo=dt.timezone.utc)
        engagement.record_event(self.database, event("initial_sent", occurred=friday))
        wednesday = dt.datetime(2026, 9, 23, 17, tzinfo=dt.timezone.utc)
        self.assertEqual(engagement.next_action(self.database, PROFILE, wednesday - dt.timedelta(seconds=1))["state"], "WAITING")
        self.assertEqual(engagement.next_action(self.database, PROFILE, wednesday)["ordinal"], 1)
        engagement.record_event(self.database, event("followup_sent", "follow1", wednesday, observed_at=wednesday.isoformat()))
        second = wednesday + dt.timedelta(days=7)
        self.assertEqual(engagement.next_action(self.database, PROFILE, second - dt.timedelta(seconds=1))["state"], "WAITING")
        self.assertEqual(engagement.next_action(self.database, PROFILE, second)["ordinal"], 2)
        engagement.record_event(self.database, event("followup_sent", "follow2", second, observed_at=second.isoformat()))
        self.assertEqual(engagement.next_action(self.database, PROFILE, second)["blocker"], "FOLLOWUP_LIMIT")

    def test_urgent_conservative_48h_and_no_automatic_contact(self):
        initial = NOW - dt.timedelta(hours=48)
        engagement.record_event(self.database, event("initial_sent", occurred=initial))
        engagement.record_event(self.database, event("interview", "interview", NOW + dt.timedelta(hours=24)))
        self.assertEqual(engagement.next_action(self.database, PROFILE, NOW)["purpose"], "URGENT")
        self.assertNotEqual(engagement.next_action(self.database, PROFILE, NOW - dt.timedelta(hours=1))["purpose"], "URGENT")
        self.assertEqual(engagement.next_action(self.database, PROFILE, NOW, recipient="Jamie Rivera")["blocker"], "NO_AUTOMATIC_FOLLOWUP_CONTACT")

    def test_ack_not_delivery_and_changed_source_id_rejected(self):
        with self.assertRaises(ValueError):
            engagement.record_event(self.database, event("initial_sent", verified=False))
        engagement.record_event(self.database, event())
        with self.assertRaises(ValueError):
            engagement.record_event(self.database, event(raw={"text": "Changed"}))

    def test_heyreach_reader_requests_exact_schema_and_blocks_unknown_shape(self):
        client = Mock()
        client.call.return_value = {"totalCount": 0, "items": []}
        reader = engagement.HeyReachReadAdapter(client, 424242, page_size=2)
        self.assertTrue(reader.read_page("heyreach", None)["done"])
        client.call.assert_called_once_with("get_conversations_v2", {"linkedInAccountIds": [424242], "limit": 2, "offset": 0})
        client.call.return_value = {"totalCount": 1, "items": [{"unknown": "shape"}]}
        with self.assertRaises(NotImplementedError):
            reader.read_page("heyreach", None)

    def test_heyreach_reader_pages_and_gets_details_with_explicit_fixture_decoder(self):
        client = Mock()
        client.call.side_effect = [{"totalCount": 2, "items": [{"fixture_id": "thread-1"}]}, {"fixture_messages": [event()]}]
        reader = engagement.HeyReachReadAdapter(client, 424242,
            lambda item: {"conversation_id": item["fixture_id"], "profile": PROFILE},
            lambda room, conversation: room["fixture_messages"], page_size=1)
        page = reader.read_page("heyreach", None)
        self.assertEqual(page["next_cursor"], "1")
        self.assertFalse(page["done"])
        client.call.assert_called_with("get_chatroom", {"accountId": 424242, "conversationId": "thread-1"})

    def test_concurrent_inbox_consumers_commit_one_page_and_one_handoff(self):
        barrier = threading.Barrier(2)
        client = Mock()
        def read(source, cursor):
            barrier.wait(timeout=5)
            return {"events": [event()], "next_cursor": None, "done": True}
        client.read_page.side_effect = read
        def run():
            database = orchestrator.connect(self.root)
            try:
                return engagement.cycle(database, {"engagement_enabled": True}, client, NOW)
            finally:
                database.close()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda unused: run(), range(2)))
        self.assertEqual(sum(result["items"][0]["inserted"] for result in results), 1)
        self.assertEqual(self.database.execute("SELECT count(*) FROM workflow_tasks").fetchone()[0], 1)

    def test_event_does_not_commit_callers_transaction(self):
        self.database.execute("BEGIN IMMEDIATE")
        engagement.record_event(self.database, event())
        self.database.rollback()
        self.assertEqual(self.database.execute("SELECT count(*) FROM engagement_events").fetchone()[0], 0)

    def test_due_message_creates_durable_writer_handoff_once(self):
        engagement.record_event(self.database, event("accepted", occurred=NOW - dt.timedelta(hours=12)))
        client = Mock()
        client.read_page.return_value = {"events": [], "next_cursor": None, "done": True}
        for unused in range(2):
            result = engagement.cycle(self.database, {"engagement_enabled": True}, client, NOW)
            self.assertEqual(len(result["handoffs"]), 1)
        self.assertEqual(self.database.execute("SELECT count(*) FROM workflow_tasks WHERE worker='message_writer'").fetchone()[0], 1)
