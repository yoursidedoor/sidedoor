import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import copy
import datetime as dt
import json
import sqlite3
import unittest
from unittest.mock import Mock, patch

import engagement
import heyreach_schema
import pipeline_routing


NOW = dt.datetime(2026, 9, 17, 18, tzinfo=dt.timezone.utc)
SENDER = "https://www.linkedin.com/in/synthetic-account"
PROFILE = "https://www.linkedin.com/in/synthetic-correspondent"


def message(sender="CORRESPONDENT", body="Synthetic unrelated sales message.", stamp="2026-09-17T17:00:00.000Z"):
    return {"createdAt": stamp, "body": body, "subject": None, "postLink": None,
            "isInMail": False, "transcript": None, "isVoiceNote": False,
            "audioDurationMs": None, "sender": sender}


def room(messages=None):
    messages = copy.deepcopy(messages if messages is not None else [message()])
    latest = max(messages, key=lambda item: item["createdAt"])
    return {"id": "synthetic-thread", "read": False, "groupChat": False,
            "blockedByMe": False, "blockedByParticipant": False,
            "lastMessageAt": latest["createdAt"], "lastMessageText": latest["body"],
            "lastMessageType": "TEXT", "lastMessageSender": latest["sender"],
            "totalMessages": len(messages), "linkedInAccountId": 424242,
            "linkedInAccount": {"id": 424242, "profileUrl": SENDER},
            "correspondentProfile": {"linkedin_id": "synthetic-member", "profileUrl": PROFILE},
            "messages": messages}


class HeyReachSchemaTests(unittest.TestCase):
    def setUp(self):
        self.clock = Mock(return_value=NOW)
        self.decoder = heyreach_schema.Decoder(424242, SENDER, clock=self.clock)

    def decode(self, value, decoder=None):
        decoder = decoder or self.decoder
        return decoder.decode_chatroom(value, decoder.decode_conversation(value))

    def test_directions_synthetic_ids_and_no_job_or_delivery_claim(self):
        outgoing = message("ME", "Synthetic personal response.", "2026-09-17T17:01:00.000Z")
        events = self.decode(room([message(), outgoing]))
        self.assertEqual([event["kind"] for event in events], ["inbound", "outbound_observed"])
        for event in events:
            self.assertTrue(event["source_id"].startswith("heyreach:synthetic:sha256:v1:"))
            self.assertEqual(event["source_id_kind"], "synthetic_content_sha256_v1")
            self.assertEqual(event["job_search_relevance"], "unknown")
            self.assertFalse(event["full_linkedin_history_clearance"])
            self.assertNotIn(event["kind"], {"initial_sent", "followup_sent", "accepted"})
        self.assertEqual(events[0]["raw"]["sender_profile"], "https://linkedin.com/in/synthetic-correspondent")
        self.assertEqual(events[1]["raw"]["sender_profile"], "https://linkedin.com/in/synthetic-account")
        self.assertEqual(events[1]["raw"]["message"], outgoing)
        self.assertEqual(events[1]["raw"]["provider_lead_id"], "synthetic-member")
        self.assertEqual(events[1]["raw"]["account_id"], 424242)
        self.assertEqual(events[1]["raw"]["profile"], "https://linkedin.com/in/synthetic-correspondent")

    def test_canonical_inbox_paths_and_unknown_quote_status_require_review(self):
        event = self.decode(room())[0]
        for field, path in event["inbox"]["raw_paths"].items():
            self.assertEqual(pipeline_routing._path(event["raw"], path), event["inbox"][field])
        row = {"source": "heyreach", "source_id": event["source_id"], "kind": "inbound",
               "raw": json.dumps(event["raw"]), "payload": json.dumps(event),
               "occurred_at": event["occurred_at"], "observed_at": event["observed_at"],
               "identity": "linkedin.com/in/synthetic-correspondent"}
        with self.assertRaisesRegex(ValueError, "QUOTED_OR_FORWARDED_MESSAGE_REQUIRES_REVIEW"):
            pipeline_routing._inbox(row, {"account_ids": [424242]}, NOW.isoformat())

    def test_sender_constructor_rejects_invalid_binding(self):
        for account in (True, "424242", 0, -1):
            with self.subTest(account=account), self.assertRaises(ValueError):
                heyreach_schema.Decoder(account, SENDER)
        with self.assertRaises(ValueError):
            heyreach_schema.Decoder(424242, "https://example.com/in/account")

    def test_both_account_ids_sender_and_participant_are_required(self):
        mutations = [
            lambda value: value.update(linkedInAccountId=123),
            lambda value: value.update(linkedInAccountId=True),
            lambda value: value["linkedInAccount"].update(id=123),
            lambda value: value["linkedInAccount"].update(profileUrl=PROFILE),
            lambda value: value["correspondentProfile"].update(profileUrl=SENDER),
            lambda value: value["correspondentProfile"].update(linkedin_id=""),
            lambda value: value["correspondentProfile"].update(profileUrl=PROFILE + "?identity=other"),
        ]
        for index, mutate in enumerate(mutations):
            value = room()
            mutate(value)
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.decode(value)

    def test_independent_room_must_match_listing_binding(self):
        original = room()
        context = self.decoder.decode_conversation(original)
        for field, replacement in (("id", "other-thread"), ("linkedInAccountId", 123)):
            changed = copy.deepcopy(original)
            changed[field] = replacement
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.decoder.decode_chatroom(changed, context)
        for field, replacement in (("profileUrl", PROFILE + "-other"), ("linkedin_id", "other-member")):
            changed = copy.deepcopy(original)
            changed["correspondentProfile"][field] = replacement
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.decoder.decode_chatroom(changed, context)
        context["profile"] = PROFILE + "-tampered"
        with self.assertRaises(ValueError):
            self.decoder.decode_chatroom(original, context)

    def test_empty_truncated_unknown_and_summary_mismatch_rejected(self):
        mutations = [
            lambda value: value.update(messages=[], totalMessages=0),
            lambda value: value.update(totalMessages=2),
            lambda value: value.update(totalMessages=True),
            lambda value: value.update(hasMoreMessages=True),
            lambda value: value.update(groupChat=True),
            lambda value: value.update(lastMessageText="Different summary"),
            lambda value: value.update(lastMessageAt="2026-09-17T16:00:00.000Z"),
            lambda value: value.update(lastMessageSender="UNKNOWN"),
            lambda value: value.update(lastMessageType="SYSTEM"),
            lambda value: value.pop("messages"),
        ]
        for index, mutate in enumerate(mutations):
            value = room()
            mutate(value)
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.decode(value)

    def test_strict_message_variants(self):
        changes = [("sender", "me"), ("sender", "OTHER"), ("createdAt", "2026-09-17T17:00:00"),
                   ("createdAt", "2026-09-99T17:00:00.000Z"), ("createdAt", "2026-09-18T17:00:00.000Z"),
                   ("body", ""), ("body", 3), ("subject", False), ("isInMail", True),
                   ("isVoiceNote", True), ("isVoiceNote", 0), ("transcript", "text"),
                   ("postLink", "https://example.com"), ("audioDurationMs", 1), ("id", "unexpected")]
        for field, replacement in changes:
            value = room()
            value["messages"][0][field] = replacement
            with self.subTest(field=field, replacement=replacement), self.assertRaises(ValueError):
                self.decode(value)

    def test_duplicate_messages_rejected_instead_of_fabricated_ordinals(self):
        with self.assertRaises(ValueError):
            self.decode(room([message(), message()]))

    def test_new_messages_and_reordering_between_reads_supported_without_loss(self):
        original = room()
        context = self.decoder.decode_conversation(original)
        newer = message("ME", "Another synthetic reply", "2026-09-17T17:01:00.000Z")
        updated = room([newer, message()])
        self.assertEqual(len(self.decoder.decode_chatroom(updated, context)), 2)
        with self.assertRaisesRegex(ValueError, "lost or changed"):
            self.decoder.decode_chatroom(room([newer]), context)

    def test_content_address_binds_each_identity_component(self):
        original = self.decode(room())[0]["source_id"]
        for field, replacement in (("body", "Different synthetic text"), ("subject", ""),
                                   ("sender", "ME"), ("createdAt", "2026-09-17T16:00:00.000Z")):
            changed = message()
            changed[field] = replacement
            with self.subTest(field=field):
                self.assertNotEqual(self.decode(room([changed]))[0]["source_id"], original)
        for field, replacement in (("id", "other-thread"), ("linkedInAccountId", 123)):
            changed = room()
            changed[field] = replacement
            decoder = self.decoder
            if field == "linkedInAccountId":
                changed["linkedInAccount"]["id"] = 123
                decoder = heyreach_schema.Decoder(123, SENDER, clock=self.clock)
            self.assertNotEqual(self.decode(changed, decoder)[0]["source_id"], original)

    def test_synthetic_hash_collision_fails_within_room_and_across_reads(self):
        with patch.object(heyreach_schema, "_source_id", return_value="synthetic-collision"):
            self.decode(room())
            with self.assertRaisesRegex(ValueError, "immutable payload"):
                self.decode(room([message(body="Other exact content")]))
            with self.assertRaisesRegex(ValueError, "Duplicate or colliding"):
                self.decode(room([message(), message(body="Different content")]))

    def test_mutable_room_metadata_and_observation_do_not_change_replayed_event(self):
        value = room()
        first = self.decode(value)[0]
        value["read"] = True
        value["blockedByMe"] = True
        value["correspondentProfile"]["firstName"] = "Changed display name"
        self.clock.return_value = NOW + dt.timedelta(hours=1)
        second = self.decode(value)[0]
        self.assertEqual(first, second)
        self.assertNotIn("read", second["raw"])
        self.assertNotIn("observed_at", second["raw"])
        self.assertNotIn("totalMessages", second["raw"])
        second["raw"]["message"]["body"] = "Caller mutation"
        self.assertEqual(self.decode(value)[0], first)

    def test_restart_uses_persisted_first_observation_and_detects_payload_conflict(self):
        first = self.decode(room())[0]
        prior_event = Mock(return_value=first)
        restarted = heyreach_schema.Decoder(424242, SENDER, clock=lambda: NOW + dt.timedelta(hours=1),
                                           prior_event=prior_event)
        self.assertEqual(self.decode(room(), restarted)[0], first)
        prior_event.assert_called_once_with("heyreach", first["source_id"])
        corrupt = copy.deepcopy(first)
        corrupt["raw"]["message"]["subject"] = "Changed"
        restarted = heyreach_schema.Decoder(424242, SENDER, clock=self.clock,
                                           prior_event=lambda source, source_id: corrupt)
        with self.assertRaisesRegex(ValueError, "different immutable payload"):
            self.decode(room(), restarted)

    def test_adapter_integration_and_durable_inbound_replay(self):
        value = room()
        client = Mock()
        client.call.side_effect = [{"totalCount": 1, "items": [value]}, value]
        reader = engagement.HeyReachReadAdapter(client, 424242, self.decoder.decode_conversation,
                                               self.decoder.decode_chatroom)
        page = reader.read_page("heyreach", None)
        self.assertTrue(page["done"])
        self.assertFalse(page["events"][0]["full_linkedin_history_clearance"])
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        self.addCleanup(database.close)
        engagement.initialize(database)
        event = page["events"][0]
        self.assertTrue(engagement.record_event(database, event)["inserted"])
        def prior_event(source, source_id):
            row = database.execute("SELECT payload FROM engagement_events WHERE source=? AND source_id=?",
                                   (source, source_id)).fetchone()
            return json.loads(row[0]) if row else None
        restarted = heyreach_schema.Decoder(424242, SENDER, clock=lambda: NOW + dt.timedelta(hours=1),
                                           prior_event=prior_event)
        self.assertFalse(engagement.record_event(database, self.decode(value, restarted)[0])["inserted"])

    def test_other_fraction_spellings_decode_to_one_millisecond_identity(self):
        # Stored HeyReach times all carry 3 digits; any other spelling of the same millisecond is
        # the same message (one event, one reply slot), and a 3-digit spelling is unchanged.
        baseline = self.decode(room([message(stamp="2026-09-17T16:59:59.641Z")]))[0]
        self.assertEqual(baseline["raw"]["message"]["createdAt"], "2026-09-17T16:59:59.641Z")
        for stamp in ("2026-09-17T16:59:59.6415Z", "2026-09-17T16:59:59.6410000Z", "2026-09-17T16:59:59.6419999Z"):
            decoded = self.decode(room([message(stamp=stamp)]), heyreach_schema.Decoder(424242, SENDER, clock=self.clock))[0]
            self.assertEqual((decoded["source_id"], decoded["raw"]), (baseline["source_id"], baseline["raw"]))
        short = self.decode(room([message(stamp="2026-09-17T17:00:00Z")]),
                            heyreach_schema.Decoder(424242, SENDER, clock=self.clock))[0]
        self.assertEqual(short["source_id"], self.decode(room())[0]["source_id"])
        mixed = room([message(stamp="2026-09-17T17:00:00.63Z")])
        mixed["lastMessageAt"] = "2026-09-17T17:00:00.630Z"
        event = self.decode(mixed, heyreach_schema.Decoder(424242, SENDER, clock=self.clock))[0]
        self.assertEqual(event["raw"]["message"]["createdAt"], "2026-09-17T17:00:00.630Z")
        for stamp in ("2026-09-17T17:00:00.12345678Z", "2026-09-17T17:00:00.Z", "2026-09-17T17:00:00+00:00"):
            with self.subTest(stamp=stamp), self.assertRaises(ValueError):
                self.decode(room([message(stamp=stamp)]))

    def test_listing_without_messages_and_lagging_summary_decode(self):
        # Observed 2026-09-17: the listing omits messages; the chatroom summary lags our new send.
        full = room([message(), message("ME", "Synthetic answer", "2026-09-17T17:05:00.000Z")])
        full.update(lastMessageAt="2026-09-17T17:00:00.000Z", lastMessageText=message()["body"],
                    lastMessageSender="CORRESPONDENT")
        listing = dict(copy.deepcopy(full), messages=[])
        events = self.decoder.decode_chatroom(full, self.decoder.decode_conversation(listing))
        self.assertEqual([event["kind"] for event in events], ["inbound", "outbound_observed"])
        with self.assertRaises(ValueError):   # the chatroom itself must stay complete
            self.decoder.decode_chatroom(listing, self.decoder.decode_conversation(listing))
        ahead = copy.deepcopy(full)
        ahead["messages"] = ahead["messages"][:1]
        ahead.update(totalMessages=1, lastMessageAt="2026-09-17T17:05:00.000Z",
                     lastMessageText="Synthetic answer", lastMessageSender="ME")
        with self.assertRaises(ValueError):   # a summary newer than the history means loss
            self.decode(ahead)

    def test_one_undecodable_conversation_holds_its_person_and_not_the_page(self):
        good, bad = room(), room([message(stamp="2026-09-17T17:00:00.000Z", body="Other")])
        bad["id"], bad["correspondentProfile"] = "bad-thread", {"linkedin_id": "m2",
            "profileUrl": "https://www.linkedin.com/in/synthetic-held"}
        bad["lastMessageType"] = "ATTACHMENT"
        client = Mock()
        client.call.side_effect = [{"totalCount": 2, "items": [bad, good]}, good]
        reader = engagement.HeyReachReadAdapter(client, 424242, self.decoder.decode_conversation,
                                               self.decoder.decode_chatroom)
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        self.addCleanup(database.close)
        result = engagement.cycle(database, {"engagement_enabled": True}, reader, NOW)
        item = result["items"][0]
        self.assertEqual((item["state"], item["inserted"]), ("COMPLETE", 1))
        self.assertEqual(item["undecodable"], [{"conversation_id": "bad-thread", "error_type": "ValueError",
            "reason": "Unsupported latest message type or direction", "held": True}])
        held = "https://www.linkedin.com/in/synthetic-held"
        self.assertEqual(engagement.next_action(database, held, NOW)["blocker"], "INBOX_CONVERSATION_UNDECODABLE")
        self.assertEqual(engagement.stop_state(database, held), "REPLY_REQUIRES_CONTEXT")
        self.assertEqual(engagement.undecodable_conversations(database, held), ["bad-thread"])
        # The next read that decodes it releases the hold (a recorded reply keeps its own stop).
        bad["lastMessageType"] = "TEXT"
        client.call.side_effect = [{"totalCount": 1, "items": [bad]}, bad]
        engagement.cycle(database, {"engagement_enabled": True}, reader, NOW)
        self.assertEqual(engagement.undecodable_conversations(database, held), [])
        self.assertEqual(engagement.stop_state(database, held), "REPLY_REQUIRES_CONTEXT")
        self.assertEqual(database.execute("SELECT state FROM engagement_stops WHERE identity=?",
                                          ("linkedin.com/in/synthetic-held",)).fetchone()[0], "REPLY_REQUIRES_CONTEXT")

    def reader(self, client):
        return engagement.HeyReachReadAdapter(client, 424242, self.decoder.decode_conversation,
                                              self.decoder.decode_chatroom)

    def database(self):
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        self.addCleanup(database.close)
        return database

    def test_undecodable_outbound_only_conversation_hold_is_released(self):
        good = room([message("ME", "Synthetic note")])
        bad = copy.deepcopy(good)
        bad["messages"][0]["isInMail"] = True        # a real decoder refusal, on the chatroom read
        client = Mock()
        client.call.side_effect = [{"totalCount": 1, "items": [good]}, bad]
        database = self.database()
        item = engagement.cycle(database, {"engagement_enabled": True}, self.reader(client), NOW)["items"][0]
        self.assertEqual(item["undecodable"][0]["reason"], "Unobserved nontext message variant")
        self.assertEqual(engagement.stop_label(database, PROFILE), "INBOX_CONVERSATION_UNDECODABLE")
        client.call.side_effect = [{"totalCount": 1, "items": [good]}, good]
        engagement.cycle(database, {"engagement_enabled": True}, self.reader(client), NOW)
        self.assertIsNone(engagement.stop_state(database, PROFILE))

    def test_release_keeps_the_hold_while_another_conversation_fails(self):
        first, second = room([message("ME", "Synthetic note")]), room([message("ME", "Second thread")])
        second["id"] = "synthetic-thread-2"
        broken = copy.deepcopy(second)
        broken["messages"][0]["postLink"] = "https://example.test/post"
        client = Mock()
        database = self.database()
        client.call.side_effect = [{"totalCount": 2, "items": [first, second]}, dict(first, groupChat=True), broken]
        engagement.cycle(database, {"engagement_enabled": True}, self.reader(client), NOW)
        self.assertEqual(sorted(engagement.undecodable_conversations(database, PROFILE)),
                         ["synthetic-thread", "synthetic-thread-2"])
        # Only the first thread is on this page and it decodes; the second's earlier hold alone
        # must keep the person held.
        client.call.side_effect = [{"totalCount": 1, "items": [first]}, first]
        engagement.cycle(database, {"engagement_enabled": True}, self.reader(client), NOW)
        self.assertEqual(engagement.undecodable_conversations(database, PROFILE), ["synthetic-thread-2"])
        self.assertEqual(database.execute("SELECT failures FROM engagement_decode_holds").fetchone()[0], 1)
        self.assertEqual(engagement.stop_label(database, PROFILE), "INBOX_CONVERSATION_UNDECODABLE")
        client.call.side_effect = [{"totalCount": 1, "items": [second]}, second]
        engagement.cycle(database, {"engagement_enabled": True}, self.reader(client), NOW)
        self.assertIsNone(engagement.stop_state(database, PROFILE))

    def test_conversations_without_id_are_keyed_on_stable_fields(self):
        one, two = room(), room([message(body="Other")])
        two["correspondentProfile"] = {"linkedin_id": "m2", "profileUrl": "https://www.linkedin.com/in/synthetic-other"}
        for value in (one, two):
            value["id"] = 7
        client = Mock()
        client.call.side_effect = [{"totalCount": 2, "items": [one, two]}]
        database = self.database()
        item = engagement.cycle(database, {"engagement_enabled": True}, self.reader(client), NOW)["items"][0]
        keys = [held["conversation_id"] for held in item["undecodable"]]
        self.assertEqual(len(set(keys)), 2)
        self.assertTrue(all(key.startswith("unidentified:sha256:") for key in keys))
        # A re-read with changed volatile fields (read flag, new message) is the same row.
        again = room([message(body="Newer", stamp="2026-09-17T17:30:00.000Z")])
        again.update(id=7, read=True)
        client.call.side_effect = [{"totalCount": 1, "items": [again]}]
        engagement.cycle(database, {"engagement_enabled": True}, self.reader(client), NOW)
        self.assertEqual(database.execute("SELECT count(*) FROM engagement_decode_holds").fetchone()[0], 2)
        self.assertEqual(database.execute("SELECT max(failures) FROM engagement_decode_holds").fetchone()[0], 2)

    def two_rooms(self):
        stuck = room([message(body="Huge thread")])
        stuck.update(id="stuck-thread", correspondentProfile={"linkedin_id": "m2",
                     "profileUrl": "https://www.linkedin.com/in/synthetic-stuck"})
        return stuck, room()

    def test_repeated_chatroom_failure_holds_after_three_runs_and_page_continues(self):
        stuck, good = self.two_rooms()
        database = self.database()
        held = "https://www.linkedin.com/in/synthetic-stuck"
        for run in (1, 2, 3):
            client = Mock()
            error = TimeoutError("timed out at https://mcp.example.test/secret") if run != 2 else {"text": "No", "parsed": False}
            client.call.side_effect = [{"totalCount": 2, "items": [stuck, good]}, error, good]
            decoder = heyreach_schema.Decoder(424242, SENDER, clock=lambda: NOW)
            reader = engagement.HeyReachReadAdapter(client, 424242, decoder.decode_conversation, decoder.decode_chatroom)
            item = engagement.cycle(database, {"engagement_enabled": True}, reader, NOW)["items"][0]
            if run < 3:
                # One or two failures: nobody held, the other conversation is recorded, page re-read.
                self.assertEqual(item["state"], "CHATROOM_RETRY")
                self.assertEqual(item["chatroom_retry"][0]["consecutive"], run)
                self.assertNotIn("secret", json.dumps(item))
                self.assertIsNone(engagement.stop_state(database, held))
                self.assertIsNone(database.execute("SELECT cursor FROM engagement_cursors").fetchone()[0])
            else:
                self.assertEqual(item["state"], "COMPLETE")
                self.assertTrue(item["undecodable"][0]["held"])
                self.assertEqual(engagement.stop_label(database, held), "INBOX_CONVERSATION_UNDECODABLE")
        self.assertEqual(database.execute("SELECT count(*) FROM engagement_events").fetchone()[0], 1)
        # It reads again later: the hold and the failure count go.
        client = Mock()
        client.call.side_effect = [{"totalCount": 1, "items": [stuck]}, stuck]
        engagement.cycle(database, {"engagement_enabled": True}, self.reader(client), NOW)
        self.assertEqual(engagement.undecodable_conversations(database, held), [])
        self.assertEqual(engagement.stop_label(database, held), "REPLY_REQUIRES_CONTEXT")   # its message, now read
        self.assertEqual(database.execute("SELECT count(*) FROM engagement_chatroom_failures").fetchone()[0], 0)

    def test_single_transient_chatroom_failure_resets(self):
        stuck, good = self.two_rooms()
        database = self.database()
        for side_effect in ([{"totalCount": 2, "items": [stuck, good]}, ConnectionError("reset"), good],
                            [{"totalCount": 2, "items": [stuck, good]}, stuck, good]):
            client = Mock()
            client.call.side_effect = side_effect
            engagement.cycle(database, {"engagement_enabled": True}, self.reader(client), NOW)
        self.assertEqual(database.execute("SELECT count(*) FROM engagement_chatroom_failures").fetchone()[0], 0)
        self.assertEqual(database.execute("SELECT count(*) FROM engagement_decode_holds").fetchone()[0], 0)

    def inbox(self, rooms, failing=(), listing_fails=()):
        """A fake HeyReach: rooms in listing order, page size 1; chatroom reads of `failing` ids
        and listings at `listing_fails` offsets raise."""
        def call(tool, arguments):
            if tool == "get_conversations_v2":
                if arguments["offset"] in listing_fails:
                    raise RuntimeError("Provider returned an error")
                calls.append(arguments["offset"])
                return {"totalCount": len(rooms), "items": rooms[arguments["offset"]:arguments["offset"] + 1]}
            if arguments["conversationId"] in failing:
                raise TimeoutError("timed out")
            return next(value for value in rooms if value["id"] == arguments["conversationId"])
        calls = []
        client = Mock()
        client.call.side_effect = call
        decoder = heyreach_schema.Decoder(424242, SENDER, clock=lambda: NOW)
        return engagement.HeyReachReadAdapter(client, 424242, decoder.decode_conversation, decoder.decode_chatroom,
                                              page_size=1), calls

    def three_rooms(self):
        rooms = []
        for index in range(3):
            value = room([message(body="Message " + str(index))])
            value.update(id="thread-" + str(index), correspondentProfile={"linkedin_id": "m" + str(index),
                         "profileUrl": "https://www.linkedin.com/in/synthetic-" + str(index)})
            rooms.append(value)
        return rooms

    def test_every_run_reads_offset_0_then_one_deep_page(self):
        database = self.database()
        rooms = self.three_rooms()
        offsets = []
        for unused in range(4):
            reader, calls = self.inbox(rooms)
            engagement.cycle(database, {"engagement_enabled": True}, reader, NOW)
            offsets.append(calls)
        self.assertEqual(offsets, [[0], [0, 1], [0, 2], [0]])
        self.assertEqual(database.execute("SELECT count(*) FROM engagement_events").fetchone()[0], 3)

    def test_deep_page_chatroom_retry_never_blocks_offset_0(self):
        database = self.database()
        rooms = self.three_rooms()
        stuck = "https://www.linkedin.com/in/synthetic-1"
        reader, calls = self.inbox(rooms, failing={"thread-1"})
        engagement.cycle(database, {"engagement_enabled": True}, reader, NOW)       # offset 0; deep scan starts at 1
        for run in (1, 2, 3):
            reader, calls = self.inbox(rooms, failing={"thread-1"})
            item = engagement.cycle(database, {"engagement_enabled": True}, reader, NOW)["items"][0]
            self.assertEqual(calls, [0, 1])                     # offset 0 is read on every run
            if run < 3:
                self.assertEqual(item["state"], "CHATROOM_RETRY")
                self.assertEqual(database.execute("SELECT cursor FROM engagement_cursors").fetchone()[0], "1")
                self.assertIsNone(engagement.stop_state(database, stuck))
        self.assertEqual(engagement.stop_label(database, stuck), "INBOX_CONVERSATION_UNDECODABLE")
        self.assertEqual(database.execute("SELECT cursor FROM engagement_cursors").fetchone()[0], "2")

    def test_chatroom_failure_counts_once_per_run_when_the_listing_shifts(self):
        database = self.database()
        rooms = self.three_rooms()
        engagement.initialize(database)
        database.execute("INSERT INTO engagement_cursors(source,cursor,generation,state) VALUES ('heyreach','1',3,'MORE_PENDING')")
        database.commit()
        for run in (1, 2):
            listings = iter([[rooms[0]], [rooms[0]]])        # thread-0 listed on both passes this run

            def call(tool, arguments):
                if tool == "get_conversations_v2":
                    return {"totalCount": 3, "items": next(listings)}
                if arguments["conversationId"] == "thread-0":
                    raise TimeoutError("timed out")
                raise AssertionError("unexpected chatroom read")
            client = Mock()
            client.call.side_effect = call
            decoder = heyreach_schema.Decoder(424242, SENDER, clock=lambda: NOW)
            reader = engagement.HeyReachReadAdapter(client, 424242, decoder.decode_conversation,
                                                    decoder.decode_chatroom, page_size=1)
            engagement.cycle(database, {"engagement_enabled": True}, reader, NOW)
            self.assertEqual(database.execute("SELECT consecutive FROM engagement_chatroom_failures").fetchone()[0], run)
        self.assertIsNone(engagement.stop_state(database, "https://www.linkedin.com/in/synthetic-0"))

    def test_deep_page_whose_listing_keeps_failing_is_skipped_after_three_runs(self):
        database = self.database()
        rooms = self.three_rooms()
        engagement.initialize(database)
        database.execute("INSERT INTO engagement_cursors(source,cursor,generation,state) VALUES ('heyreach','1',3,'MORE_PENDING')")
        database.commit()
        for run in (1, 2, 3):
            reader, calls = self.inbox(rooms, listing_fails={1})
            item = engagement.cycle(database, {"engagement_enabled": True}, reader, NOW)["items"][0]
            self.assertEqual(calls, [0])                                       # offset 0 every run
            if run < 3:
                self.assertNotIn("skipped_pages", item)
                self.assertEqual(database.execute("SELECT cursor FROM engagement_cursors").fetchone()[0], "1")
        self.assertEqual(item["skipped_pages"], [{"cursor": "1", "next_cursor": "2", "failures": 3,
                                                  "error_type": "RuntimeError", "error": "Provider returned an error"}])
        self.assertEqual(database.execute("SELECT cursor FROM engagement_cursors").fetchone()[0], "2")
        reader, calls = self.inbox(rooms)
        engagement.cycle(database, {"engagement_enabled": True}, reader, NOW)
        self.assertEqual(calls, [0, 2])

    def test_skip_cursor_moves_one_page_of_its_size(self):
        reader = engagement.HeyReachReadAdapter(Mock(), 424242, page_size=20)
        self.assertEqual((reader.skip_cursor("40"), reader.skip_cursor(None)), ("60", "20"))

    def seed_deep(self, database, cursor="1"):
        engagement.initialize(database)
        database.execute("INSERT INTO engagement_cursors(source,cursor,generation,state) VALUES ('heyreach',?,3,'MORE_PENDING')", (cursor,))
        database.commit()

    def deep_failures(self, database):
        row = database.execute("SELECT cursor,consecutive FROM engagement_deep_page_failures").fetchone()
        return tuple(row) if row else None

    def test_successful_deep_read_resets_the_failure_count(self):
        database = self.database()
        rooms = self.three_rooms()
        self.seed_deep(database)
        for unused in range(2):
            reader, unused_calls = self.inbox(rooms, listing_fails={1})
            engagement.cycle(database, {"engagement_enabled": True}, reader, NOW)
        self.assertEqual(self.deep_failures(database), ("1", 2))
        reader, unused_calls = self.inbox(rooms)
        engagement.cycle(database, {"engagement_enabled": True}, reader, NOW)
        self.assertIsNone(self.deep_failures(database))

    def test_failures_at_different_cursors_do_not_accumulate(self):
        database = self.database()
        rooms = self.three_rooms()
        self.seed_deep(database)
        reader, unused_calls = self.inbox(rooms, listing_fails={1})
        engagement.cycle(database, {"engagement_enabled": True}, reader, NOW)
        database.execute("UPDATE engagement_cursors SET cursor='2'")        # the scan moved on meanwhile
        database.commit()
        for unused in range(2):
            reader, unused_calls = self.inbox(rooms, listing_fails={2})
            item = engagement.cycle(database, {"engagement_enabled": True}, reader, NOW)["items"][0]
        self.assertNotIn("skipped_pages", item)
        self.assertEqual(self.deep_failures(database), ("2", 2))

    def test_recording_errors_never_skip_a_deep_page(self):
        database = self.database()
        rooms = self.three_rooms()
        self.seed_deep(database)
        from unittest.mock import patch
        original = engagement._record

        def broken(database, event):
            if event["profile"].endswith("synthetic-1"):
                raise sqlite3.OperationalError("database is locked")
            return original(database, event)
        with patch.object(engagement, "_record", broken):
            for unused in range(4):
                reader, unused_calls = self.inbox(rooms)
                item = engagement.cycle(database, {"engagement_enabled": True}, reader, NOW)["items"][0]
                self.assertEqual((item["state"], item["error_type"]), ("BLOCKED", "OperationalError"))
        self.assertNotIn("skipped_pages", item)
        self.assertIsNone(self.deep_failures(database))
        self.assertEqual(database.execute("SELECT cursor FROM engagement_cursors").fetchone()[0], "1")

    def test_skip_bookkeeping_errors_never_stop_the_cycle(self):
        from unittest.mock import patch
        database = self.database()
        rooms = self.three_rooms()
        self.seed_deep(database)
        reader, unused_calls = self.inbox(rooms, listing_fails={1})
        with patch.object(engagement, "_deep_page_failed", side_effect=sqlite3.OperationalError("database is locked")):
            result = engagement.cycle(database, {"engagement_enabled": True}, reader, NOW)
        self.assertEqual(result["state"], "PROCESSED")
        self.assertIn("outbound_reconciliation", result)
        self.assertEqual(result["items"][0]["deep_page_count_error"]["error_type"], "OperationalError")

    def test_skipped_pages_are_kept_and_reported(self):
        import reporting
        database = self.database()
        rooms = self.three_rooms()
        self.seed_deep(database)
        for unused in range(3):
            reader, unused_calls = self.inbox(rooms, listing_fails={1})
            engagement.cycle(database, {"engagement_enabled": True}, reader, NOW)
        rows = database.execute("SELECT cursor,next_cursor,failures,error_type FROM engagement_skipped_pages").fetchall()
        self.assertEqual([tuple(row) for row in rows], [("1", "2", 3, "RuntimeError")])
        reporting.initialize(database)
        payload = reporting.snapshot(database, {"timezone": "America/Los_Angeles"}, NOW.isoformat())
        self.assertEqual(payload["inbox_skipped_pages"]["records"][0]["cursor"], "1")

    def test_offset_0_chatroom_retry_does_not_stop_the_deep_page(self):
        database = self.database()
        rooms = self.three_rooms()
        engagement.initialize(database)
        database.execute("INSERT INTO engagement_cursors(source,cursor,generation,state) VALUES ('heyreach','1',3,'MORE_PENDING')")
        database.commit()
        reader, calls = self.inbox(rooms, failing={"thread-0"})
        item = engagement.cycle(database, {"engagement_enabled": True}, reader, NOW)["items"][0]
        self.assertEqual(calls, [0, 1])
        self.assertEqual([one["state"] for one in item["passes"]], ["CHATROOM_RETRY", "MORE_PENDING"])
        self.assertEqual(item["state"], "CHATROOM_RETRY")
        self.assertEqual(database.execute("SELECT cursor FROM engagement_cursors").fetchone()[0], "2")

    def test_people_without_followup_sources_are_reported(self):
        database = self.database()
        engagement.initialize(database)
        event = self.decode(room([message("ME", "Synthetic note")]))[0]
        engagement.record_event(database, event)
        result = engagement.reconcile_observed_outbound(database, {}, NOW)
        self.assertEqual(result["state"], "PROCESSED")
        self.assertEqual(result["without_followup_sources"][0]["identity"], "linkedin.com/in/synthetic-correspondent")
        self.assertTrue(result["without_followup_sources"][0]["error"])

    def test_failure_count_survives_the_conversation_moving_between_pages(self):
        database = self.database()
        rooms = self.three_rooms()
        reader, unused = self.inbox(rooms, failing={"thread-0"})              # on offset 0
        engagement.cycle(database, {"engagement_enabled": True}, reader, NOW)
        moved = [rooms[1], rooms[0], rooms[2]]                                 # now on the deep page
        reader, unused = self.inbox(moved, failing={"thread-0"})
        engagement.cycle(database, {"engagement_enabled": True}, reader, NOW)
        self.assertEqual(database.execute("SELECT consecutive FROM engagement_chatroom_failures").fetchone()[0], 2)

    def test_listing_failures_keep_the_deep_cursor_and_offset_0_is_still_read(self):
        database = self.database()
        rooms = self.three_rooms()
        engagement.initialize(database)
        database.execute("INSERT INTO engagement_cursors(source,cursor,generation,state) VALUES ('heyreach','2',58,'MORE_PENDING')")
        database.commit()
        reader, calls = self.inbox(rooms, listing_fails={2})
        item = engagement.cycle(database, {"engagement_enabled": True}, reader, NOW)["items"][0]
        self.assertEqual((item["state"], item["error_type"], calls), ("BLOCKED", "RuntimeError", [0]))
        self.assertEqual(database.execute("SELECT cursor FROM engagement_cursors").fetchone()[0], "2")
        self.assertEqual(database.execute("SELECT count(*) FROM engagement_events").fetchone()[0], 1)
        reader, calls = self.inbox(rooms, listing_fails={0})
        item = engagement.cycle(database, {"engagement_enabled": True}, reader, NOW)["items"][0]
        self.assertEqual((item["state"], calls), ("BLOCKED", []))           # account-wide: no deep page
        self.assertEqual(database.execute("SELECT count(*) FROM engagement_decode_holds").fetchone()[0], 0)

    def test_drifting_message_time_is_one_event_and_one_review(self):
        # Morgan Hale's shape: one message read at .51.466, then 54.548, then 54.342.
        database = self.database()
        for stamps in (("2026-09-17T17:00:00.000Z", "2026-09-17T17:24:51.466Z"),
                       ("2026-09-17T17:00:06.100Z", "2026-09-17T17:24:54.548Z"),
                       ("2026-09-17T17:00:01.900Z", "2026-09-17T17:24:54.342Z")):
            value = room([message(stamp=stamps[0]), message("ME", "Synthetic reply", stamps[1])])
            client = Mock()
            client.call.side_effect = [{"totalCount": 1, "items": [value]}, value]
            decoder = heyreach_schema.Decoder(424242, SENDER, clock=lambda: NOW)
            reader = engagement.HeyReachReadAdapter(client, 424242, decoder.decode_conversation, decoder.decode_chatroom)
            item = engagement.cycle(database, {"engagement_enabled": True}, reader, NOW)["items"][0]
            self.assertEqual(item["state"], "COMPLETE")
        kinds = [row[0] for row in database.execute("SELECT kind FROM engagement_events ORDER BY kind")]
        self.assertEqual(kinds, ["inbound", "outbound_observed"])
        self.assertEqual(database.execute("SELECT count(*) FROM workflow_tasks WHERE worker='engagement_review'").fetchone()[0], 1)

    def test_listing_and_chatroom_with_drifted_time_decode(self):
        listing = room([message(stamp="2026-09-17T17:00:00.000Z")])
        chat = room([message(stamp="2026-09-17T17:00:07.080Z")])
        events = self.decoder.decode_chatroom(chat, self.decoder.decode_conversation(listing))
        self.assertEqual(len(events), 1)

    def test_summary_newer_than_history_beyond_drift_is_refused(self):
        value = room()
        value["lastMessageAt"] = "2026-09-17T17:00:07.000Z"       # drift: accepted
        self.assertEqual(len(self.decode(value)), 1)
        value["lastMessageAt"] = "2026-09-17T17:30:00.000Z"       # names the newest text, 30 min later
        with self.assertRaisesRegex(ValueError, "summary contradicts"):
            self.decode(value, heyreach_schema.Decoder(424242, SENDER, clock=self.clock))

    def test_reconciliation_ambiguity_rule(self):
        import followup_execution
        from unittest.mock import patch
        database = self.database()
        engagement.initialize(database)
        body = "Synthetic invitation note"
        for stamp in ("2026-09-17T16:00:00.000Z", "2026-09-17T16:00:03.000Z", "2026-09-17T17:00:00.000Z"):
            event = self.decode(room([message("ME", body, stamp)]), heyreach_schema.Decoder(424242, SENDER, clock=self.clock))[0]
            database.execute("INSERT INTO engagement_events VALUES (?,?,?,?,?,?,?,?,?)",
                (event["source"], event["source_id"], "linkedin.com/in/synthetic-correspondent", event["kind"],
                 event["occurred_at"], event["observed_at"], json.dumps(event["raw"]), "null", json.dumps(event)))
        database.commit()
        source = {"source": "invitation", "text": body, "conversation_id": None, "invitation": True,
                  "occurred_at": "2026-09-17T17:00:00+00:00", "sent_not_before": "2026-09-17T15:00:00+00:00",
                  "occurred_at_basis": "CONSERVATIVE_FIRST_INDEPENDENT_DELIVERY_OBSERVATION"}
        with patch.object(followup_execution, "_sources", return_value=[source]):
            result = engagement.reconcile_observed_outbound(database, {}, NOW)
            # 16:00:00 and 16:00:03 are one drifted message, 17:00 another: two messages, so neither.
            self.assertEqual(result["reconciled"], 0)
            database.execute("DELETE FROM engagement_events WHERE occurred_at LIKE '%17:00:00%'")
            database.commit()
            self.assertEqual(engagement.reconcile_observed_outbound(database, {}, NOW)["reconciled"], 2)
        self.assertEqual(engagement.unreconciled_outbound(database, engagement.events_for(database, PROFILE)), [])

    def test_sent_reply_reconciles_its_drifted_observations(self):
        import outreach
        database = self.database()
        outreach.initialize_actions(database)
        payload = {"purpose": "REPLY", "message": "Synthetic reply", "conversation_id": "synthetic-thread", "profile": PROFILE}
        database.execute("INSERT INTO outreach_actions VALUES (?,?,?,?,?,?,?,?)",
            ("reply:1", "linkedin.com/in/synthetic-correspondent", "REPLY:1", json.dumps(payload), "h", "CONFIRMED_SENT",
             NOW.isoformat(), json.dumps({"retry": "confirmed later, differs from the receipt"})))
        engagement.record_event(database, {"source": "heyreach", "source_id": "receipt-1", "profile": PROFILE,
            "kind": "outbound_observed", "purpose": "REPLY", "verified": True, "evidence": "synthetic",
            "occurred_at": "2026-09-17T17:24:51.466+00:00", "observed_at": "2026-09-17T17:24:53+00:00",
            "raw": {"action_id": "reply:1", "outcome": "SENT", "independent": True,
                    "conversation_id": "synthetic-thread", "occurred_at": "2026-09-17T17:24:51.466+00:00"}})
        for stamp in ("2026-09-17T17:24:54.548Z", "2026-09-17T17:24:54.342Z"):   # both already stored
            event = self.decode(room([message("ME", "Synthetic reply", stamp)]),
                                heyreach_schema.Decoder(424242, SENDER, clock=self.clock))[0]
            database.execute("INSERT INTO engagement_events VALUES (?,?,?,?,?,?,?,?,?)",
                (event["source"], event["source_id"], "linkedin.com/in/synthetic-correspondent", event["kind"],
                 event["occurred_at"], event["observed_at"], json.dumps(event["raw"]), "null", json.dumps(event)))
        database.commit()
        self.assertEqual(engagement.reconcile_observed_outbound(database, {}, NOW)["reconciled"], 2)
        left = engagement.unreconciled_outbound(database, engagement.events_for(database, PROFILE))
        self.assertEqual([event["source_id"] for event in left], ["receipt-1"])   # the receipt itself, as before
        # Not a reply source unless the action is CONFIRMED_SENT and in the same conversation.
        self.assertEqual(len(engagement._reply_sources(database, "linkedin.com/in/synthetic-correspondent")), 1)
        database.execute("UPDATE outreach_actions SET state='UNCERTAIN_NO_RETRY'")
        self.assertEqual(engagement._reply_sources(database, "linkedin.com/in/synthetic-correspondent"), [])
        database.execute("UPDATE outreach_actions SET state='CONFIRMED_SENT',payload=?",
                         (json.dumps(dict(payload, conversation_id="another-thread")),))
        self.assertEqual(engagement._reply_sources(database, "linkedin.com/in/synthetic-correspondent"), [])

    def test_reconciliation_wrapper_never_raises(self):
        from unittest.mock import patch
        database = self.database()
        engagement.initialize(database)
        client = Mock()
        client.read_page.return_value = {"events": [], "next_cursor": None, "done": True}
        with patch.object(engagement, "_reconcile_observed_outbound", side_effect=RuntimeError("boom")):
            result = engagement.cycle(database, {"engagement_enabled": True}, client, NOW)
        self.assertEqual(result["state"], "PROCESSED")
        self.assertEqual(result["outbound_reconciliation"], {"state": "FAILED", "error_type": "RuntimeError", "error": "boom"})

    def test_bad_saved_payload_never_stops_followup_planning(self):
        database = self.database()
        engagement.initialize(database)
        database.execute("INSERT INTO engagement_events VALUES (?,?,?,?,?,?,?,?,?)",
            ("heyreach", "bad", "linkedin.com/in/synthetic-correspondent", "outbound_observed", NOW.isoformat(),
             NOW.isoformat(), "{}", "null", json.dumps({"source": "heyreach", "source_id": "bad", "kind": "outbound_observed",
                 "raw": {"message": {"body": "x", "createdAt": "not a time"}}})))
        database.commit()
        client = Mock()
        client.read_page.return_value = {"events": [], "next_cursor": None, "done": True}
        result = engagement.cycle(database, {"engagement_enabled": True}, client, NOW)
        self.assertEqual(result["state"], "PROCESSED")
        self.assertEqual(result["outbound_reconciliation"]["failures"][0]["error_type"], "ValueError")

    def test_diagnostic_blanks_urls_and_bounds_text(self):
        text = engagement.diagnostic(ValueError("POST https://mcp.example.test/secret-token?key=1 failed\n" + "x" * 400))
        self.assertNotIn("secret-token", text)
        self.assertIn("<url>", text)
        self.assertLessEqual(len(text), 160)

    def test_inactivity_closure_waits_for_an_undecodable_conversation(self):
        import inactivity_closure
        database = self.database()
        inactivity_closure.initialize(database)
        sent = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)
        engagement.record_event(database, {"source": "heyreach", "source_id": "sent", "profile": PROFILE,
            "kind": "initial_sent", "occurred_at": sent.isoformat(), "observed_at": sent.isoformat(),
            "evidence": "synthetic", "raw": {}, "verified": True})
        database.execute("CREATE TABLE history (identity TEXT, profile TEXT)")
        database.execute("INSERT INTO history VALUES (?,?)", ("linkedin.com/in/synthetic-correspondent", PROFILE))
        engagement._hold_undecodable(database, "heyreach", {"conversation_id": "held", "error_type": "ValueError",
            "reason": "Unobserved nontext message variant", "identity": "linkedin.com/in/synthetic-correspondent"},
            NOW.isoformat())
        database.commit()
        config = {"outreach_inactivity_days": 27}
        refresh = lambda profile, observed: {"state": "COMPLETE", "evidence": "synthetic", "events": []}
        # Acceptance unobservable: a read while a conversation is undecodable must not record the
        # two-read rule's first clean read either.
        unobservable = lambda profile, observed: {"state": "COMPLETE", "evidence": "synthetic", "events": [],
                                                  "acceptance_observable": False}
        result = inactivity_closure.cycle(database, config, None, NOW, refresh=unobservable)
        self.assertEqual(result["items"][0]["blocker"], "INBOX_CONVERSATION_UNDECODABLE")
        self.assertEqual(database.execute("SELECT count(*) FROM outreach_inactivity_observations WHERE kind=?",
                                          (inactivity_closure.FIRST_CLEAN_READ,)).fetchone()[0], 0)
        database.execute("DELETE FROM outreach_inactivity_checks")
        database.commit()
        result = inactivity_closure.cycle(database, config, None, NOW, refresh=refresh)
        self.assertEqual(result["items"][0]["blocker"], "INBOX_CONVERSATION_UNDECODABLE")
        self.assertIsNone(engagement.closure_status(database, PROFILE))
        database.execute("DELETE FROM engagement_decode_holds")
        database.execute("DELETE FROM outreach_inactivity_checks")
        database.commit()
        inactivity_closure.cycle(database, config, None, NOW, refresh=refresh)
        self.assertEqual(engagement.closure_status(database, PROFILE)["state"], "CLOSED")

    def test_page_failure_records_error_type(self):
        client = Mock()
        client.call.side_effect = [{"totalCount": 1}]
        reader = engagement.HeyReachReadAdapter(client, 424242, self.decoder.decode_conversation,
                                               self.decoder.decode_chatroom)
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        self.addCleanup(database.close)
        item = engagement.cycle(database, {"engagement_enabled": True}, reader, NOW)["items"][0]
        self.assertEqual((item["blocker"], item["error_type"], item["error"]),
                         ("INBOX_READ_OR_EVENT_INVALID", "ValueError", "Unknown HeyReach conversation response"))

    def test_outbound_observation_records_without_initial_outreach_or_followup(self):
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        self.addCleanup(database.close)
        engagement.initialize(database)
        event = self.decode(room([message("ME", "Synthetic personal reply")]))[0]
        self.assertTrue(engagement.record_event(database, event)["inserted"])
        self.assertEqual(database.execute("SELECT kind FROM engagement_events").fetchone()[0], "outbound_observed")
        self.assertEqual(engagement.next_action(database, PROFILE, NOW)["state"], "BLOCKED")
        self.assertEqual(database.execute("SELECT count(*) FROM workflow_tasks").fetchone()[0], 0)

    def test_prior_observation_future_or_before_message_rejected(self):
        original = self.decode(room())[0]
        for invalid in (NOW + dt.timedelta(hours=1), NOW - dt.timedelta(days=1)):
            prior = copy.deepcopy(original)
            prior["observed_at"] = invalid.isoformat()
            prior["inbox"]["authentication"]["observed_at"] = invalid.isoformat()
            decoder = heyreach_schema.Decoder(424242, SENDER, clock=self.clock,
                                             prior_event=lambda source, source_id: prior)
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.decode(room(), decoder)

    def test_later_room_growth_keeps_original_event_immutable(self):
        original = self.decode(room())[0]
        self.clock.return_value = NOW + dt.timedelta(hours=1)
        newer = message("ME", "Synthetic new reply", "2026-09-17T18:01:00.000Z")
        events = self.decode(room([message(), newer]))
        self.assertEqual(events[0], original)
        self.assertEqual(events[1]["observed_at"], self.clock.return_value.isoformat())


if __name__ == "__main__":
    unittest.main()
