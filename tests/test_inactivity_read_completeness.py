"""A zero-room HeyReach read closes nobody unless the lead lookup, saved conversations and
member-id spellings are all accounted for (review of the 2026-09-30 inactivity change)."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import engagement
import inactivity_closure
import orchestrator
import outreach_provider
import research_workflow

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_heyreach_schema import PROFILE, SENDER, message, room as schema_room


NOW = dt.datetime(2026, 9, 18, tzinfo=dt.timezone.utc)
CONFIG = {"outreach_inactivity_days": 27, "sender_id": 424242, "sender_profile": SENDER,
          "timezone": "America/Los_Angeles"}
MEMBER = "https://www.linkedin.com/in/ACoAAsyntheticMember"
LEAD = "115000001"


def room(messages):
    """The schema fixture's room with an enriched (numeric) HeyReach lead id."""
    raw = schema_room(messages)
    raw["correspondentProfile"] = dict(raw["correspondentProfile"], linkedin_id=LEAD)
    return raw


def event(profile, kind, occurred, source_id, raw):
    return {"source": "heyreach", "source_id": source_id, "profile": profile, "kind": kind,
            "occurred_at": occurred, "observed_at": occurred, "verified": True,
            "evidence": "synthetic heyreach read", "raw": raw}


class ZeroRoomReadCompleteness(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(directory)
        (root / "state").mkdir()
        self.database = orchestrator.connect(root)
        self.addCleanup(self.database.close)
        inactivity_closure.initialize(self.database)
        research_workflow.initialize(self.database)
        with self.database:
            self.database.execute("INSERT INTO history VALUES (?,?,?,?,?)",
                                  (orchestrator.profile_key(PROFILE), "Synthetic", "Synthetic", PROFILE, "hand-sent"))
        engagement.record_event(self.database, event(PROFILE, "initial_sent", "2026-08-01T00:00:00+00:00",
                                                     "initial", {}))
        self.lead = {"profileUrl": PROFILE, "linkedin_id": "115000001"}
        self.thread = None
        self.stats_fail = False
        self.auth_valid = True
        self.lead_threads = None

    def call(self, operation, arguments):
        if operation == "get_linked_in_account_by_id":
            return {"id": 424242, "profileUrl": SENDER, "isActive": True, "authIsValid": self.auth_valid}
        if operation == "get_conversations_v2" and self.thread:
            if "leadLinkedInId" in arguments and self.lead_threads is not None:
                if self.lead_threads == "fail":
                    raise RuntimeError("listing unavailable")
                if callable(self.lead_threads):
                    return self.lead_threads(arguments)
                return {"totalCount": len(self.lead_threads), "items": self.lead_threads}
            return {"totalCount": 1, "items": [self.thread]}
        if operation == "get_chatroom" and self.thread:
            return self.thread
        if operation in {"get_conversations_v2", "get_campaigns_for_lead"}:
            return {"totalCount": 0, "items": []}
        if operation == "get_lead":
            if self.lead is None:
                raise RuntimeError("transient 502")
            return self.lead
        if operation == "get_campaign":
            return {"id": 7, "campaignAccountIds": [424242], "linkedInUserListId": 8,
                    "startedAt": "2026-08-01T00:00:00Z"}
        if operation == "get_leads_from_list":
            return {"totalCount": 1, "items": [{"profileUrl": PROFILE}]}
        if operation == "get_overall_stats":
            if self.stats_fail:
                raise RuntimeError("stats unavailable")
            return {"overallStats": {"connectionsSent": 1, "connectionsAccepted": 0}}
        raise AssertionError(operation)

    def campaign_person(self):
        """A HeyReach single-contact campaign invitation, still pending (acceptance observable)."""
        with self.database:
            self.database.execute("DELETE FROM engagement_events")
        inactivity_closure.record_anchor(self.database, PROFILE, "2026-08-01T00:00:00+00:00",
            "HEYREACH_SEND_OBSERVED_UPPER_BOUND", "heyreach-campaign:7",
            {"heyreach": {"campaign_id": 7, "list_id": 8, "started_at": "2026-08-01T00:00:00Z"}}, NOW)
        self.database.commit()

    def cycle(self, now):
        client = Mock()
        client.call.side_effect = self.call
        with patch.object(outreach_provider, "_now", return_value=now.isoformat()), \
                patch("socket.socket.connect", side_effect=AssertionError("NO NETWORK")):
            return inactivity_closure.cycle(self.database, CONFIG, client, now)

    def two_reads(self):
        first = self.cycle(NOW)
        second = self.cycle(NOW + dt.timedelta(days=27))
        return first, second

    def test_clean_zero_room_read_closes_on_the_second_read(self):
        first, second = self.two_reads()
        self.assertEqual(first["items"][0]["state"], "ACTIVE", first)
        self.assertEqual(second["closed"], 1, second)

    def test_failed_get_lead_never_closes(self):
        self.lead = None
        first, second = self.two_reads()
        self.assertEqual(first["items"][0]["blocker"], "PROVIDER_LEAD_UNOBSERVED", first)
        self.assertIsNone(engagement.closure_status(self.database, PROFILE))

    def test_saved_conversation_missing_from_the_read_never_closes(self):
        engagement.record_event(self.database, event(PROFILE, "outbound_observed", "2026-08-01T00:00:00+00:00",
                                                     "old-message", {"conversation_id": "old-thread"}))
        first, second = self.two_reads()
        self.assertEqual(first["items"][0]["blocker"], "SAVED_CONVERSATION_NOT_IN_READ", first)
        self.assertIsNone(engagement.closure_status(self.database, PROFILE))

    def test_member_id_events_are_linked_and_move_the_anchor(self):
        inbound = "2026-09-10T00:00:00+00:00"
        engagement.record_event(self.database, event(MEMBER, "inbound", inbound, "member-inbound",
                                                     {"provider_lead_id": "115000001"}))
        result = self.cycle(NOW)
        self.assertIsNone(engagement.closure_status(self.database, PROFILE))
        planned = inactivity_closure.plan(self.database, PROFILE, NOW)
        self.assertEqual((planned["state"], planned["anchor_at"]), ("ACTIVE", inbound), (planned, result))
        self.assertIn(orchestrator.profile_key(MEMBER), engagement.identity_keys(self.database, PROFILE))

    def test_bound_room_still_links_a_member_id_identity_with_the_same_lead_id(self):
        # The profile-URL listing binds a room (lead id LEAD in its correspondent);
        # a separate member-id identity with that lead id and a newer inbound must move the anchor.
        self.thread = room([message("ME", "Original note", "2026-08-01T00:00:00.000Z")])
        inbound = "2026-09-12T00:00:00+00:00"
        engagement.record_event(self.database, event(MEMBER, "inbound", inbound, "member-inbound",
                                                     {"provider_lead_id": LEAD}))
        first, second = self.two_reads()
        self.assertIn(orchestrator.profile_key(MEMBER), engagement.identity_keys(self.database, PROFILE))
        self.assertEqual(inactivity_closure.plan(self.database, PROFILE, NOW)["anchor_at"], inbound)
        self.assertIsNone(engagement.closure_status(self.database, PROFILE), (first, second))

    def test_uncomparable_member_id_identity_never_closes(self):
        engagement.record_event(self.database, event(MEMBER, "inbound", "2026-08-02T00:00:00+00:00",
                                                     "member-inbound", {}))
        first, second = self.two_reads()
        self.assertIsNone(engagement.closure_status(self.database, PROFILE))
        self.assertIn("MEMBER_ID_IDENTITY_UNRESOLVED", [item.get("blocker") for item in first["items"]], first)
        self.assertIn({"identity": orchestrator.profile_key(MEMBER), "reason": "MEMBER_ID_EVENTS_CARRY_NO_LEAD_ID"},
                      [issue for entry in first["unresolved_identities"] for issue in entry["detail"] or []
                       if isinstance(issue, dict)], first)

    def test_pending_campaign_invitation_closes_on_one_complete_read(self):
        self.campaign_person()
        self.assertEqual(self.cycle(NOW)["closed"], 1)

    def test_campaign_person_whose_stats_read_fails_never_closes(self):
        self.campaign_person()
        self.stats_fail = True
        first, second = self.two_reads()
        self.assertIsNone(engagement.closure_status(self.database, PROFILE), (first, second))

    def test_placeholder_lead_id_never_rules_out_a_numeric_member_id(self):
        # Elio's shape: get_lead gives an unenriched import placeholder (imp_...), while a
        # member-id identity carries a numeric HeyReach id. They may be the same person.
        self.campaign_person()
        self.lead = {"profileUrl": PROFILE, "linkedin_id": "imp_QUFFSYNTHETIC"}
        engagement.record_event(self.database, event(MEMBER, "inbound", "2026-09-10T00:00:00+00:00",
                                                     "member-inbound", {"provider_lead_id": "12345"}))
        result = self.cycle(NOW)
        self.assertEqual(result["closed"], 0, result)
        self.assertIsNone(engagement.closure_status(self.database, PROFILE))

    def test_two_numeric_lead_ids_that_differ_are_different_people(self):
        self.campaign_person()
        self.lead = {"profileUrl": PROFILE, "linkedin_id": "67890"}
        engagement.record_event(self.database, event(MEMBER, "inbound", "2026-09-10T00:00:00+00:00",
                                                     "member-inbound", {"provider_lead_id": "12345"}))
        self.assertEqual(self.cycle(NOW)["closed"], 1)

    def test_refused_alias_makes_the_read_incomplete(self):
        engagement.record_event(self.database, event(MEMBER, "inbound", "2026-08-02T00:00:00+00:00",
                                                     "member-inbound", {"provider_lead_id": "115000001"}))
        with self.database:  # an existing ledger identity makes register_alias refuse the merge
            self.database.execute("INSERT INTO rw_contacts VALUES (?,?,?,?,?)",
                                  ("synthetic", research_workflow.profile_key(MEMBER), "member", 1, None))
        first, second = self.two_reads()
        self.assertIsNone(engagement.closure_status(self.database, PROFILE), (first, second))
        details = [issue["reason"] for entry in first["unresolved_identities"] for issue in entry["detail"] or []
                   if isinstance(issue, dict)]
        self.assertTrue(any(reason.startswith("ALIAS_REFUSED:") for reason in details), first)

    def test_second_thread_under_the_member_id_spelling_is_never_missed(self):
        # Thread A binds by profile URL; thread B, filed under the ACoAA spelling with the same
        # lead id, holds a newer reply. collect never lists by lead id here, so the closure read must.
        self.thread = room([message("ME", "Original note", "2026-08-01T00:00:00.000Z")])
        other = room([message("CORRESPONDENT", "Recent reply", "2026-09-15T00:00:00.000Z")])
        other["id"] = "2-OTHER"
        other["correspondentProfile"] = dict(other["correspondentProfile"], profileUrl=MEMBER)
        self.lead_threads = [self.thread, other]
        first, second = self.two_reads()
        self.assertEqual(first["items"][0]["blocker"], "LEAD_ID_CONVERSATION_NOT_READ", first)
        self.assertIsNone(engagement.closure_status(self.database, PROFILE))
        self.lead_threads = "fail"
        self.assertEqual(self.cycle(NOW + dt.timedelta(days=28))["items"][0]["blocker"], "LEAD_ID_LISTING_INCOMPLETE")
        self.assertIsNone(engagement.closure_status(self.database, PROFILE))

    def test_lead_id_listing_needs_every_conversation_read_and_bound(self):
        self.thread = room([message("ME", "Original note", "2026-08-01T00:00:00.000Z")])
        unread = room([message("ME", "Original note", "2026-08-01T00:00:00.000Z")])
        unread["id"] = "2-UNREAD"  # binds to the person, but this read never opened it
        self.lead_threads = [self.thread, unread]
        self.assertEqual(self.cycle(NOW)["items"][0]["blocker"], "LEAD_ID_CONVERSATION_NOT_READ")
        rebound = dict(self.thread, correspondentProfile=dict(self.thread["correspondentProfile"], profileUrl=MEMBER))
        self.lead_threads = [rebound]  # a read room's id, but the listing binds it to another spelling
        self.assertEqual(self.cycle(NOW + dt.timedelta(minutes=15))["items"][0]["blocker"], "LEAD_ID_CONVERSATION_NOT_READ")
        self.assertIsNone(engagement.closure_status(self.database, PROFILE))

    def test_lead_id_listing_must_be_complete_and_hold_every_bound_room(self):
        # Five bound, read rooms: each case would otherwise pass (every listed conversation is
        # one of them), so only the check it names stops it; the call count shows it stops at once.
        rooms = [dict(room([message("ME", "Original note", "2026-08-01T00:00:00.000Z")]), id="r" + str(index))
                 for index in range(5)]
        a, b = rooms[0], rooms[1]

        def by_offset(*responses):
            return lambda arguments: responses[min(arguments["offset"], len(responses) - 1)]

        cases = {  # name: (listing by offset, bound rooms, provider calls before it stops)
            "empty listing omits the bound room": (by_offset({"totalCount": 0, "items": []}), [a], 1),
            "listing omits one bound room": (by_offset({"totalCount": 1, "items": [a]}), [a, b], 1),
            "repeated item on page two": (by_offset({"totalCount": 2, "items": [a]}, {"totalCount": 2, "items": [a]}), [a], 2),
            "total changed between pages": (by_offset({"totalCount": 5, "items": [a]}, {"totalCount": 2, "items": [b]}), [a, b], 2),
            "more items than the total": (by_offset({"totalCount": 1, "items": [a, b]}), [a, b], 1),
            "empty page before the total": (by_offset({"totalCount": 2, "items": [a]}, {"totalCount": 2, "items": []}), [a], 2),
            "page limit reached": (lambda arguments: {"totalCount": 600, "items": [rooms[arguments["offset"]]]}, rooms, 5),
            "malformed page": (by_offset({"items": [a]}), [a], 1),
        }
        for name, (listing, bound, calls) in cases.items():
            with self.subTest(name):
                client = Mock()
                client.call.side_effect = lambda operation, arguments: listing(arguments)
                observed = envelope(*[dict(chatroom(item["id"], LEAD), raw=item) for item in bound])
                self.assertEqual(inactivity_closure._lead_listing_covers_rooms(
                    CONFIG, client, observed, PROFILE, LEAD), "LEAD_ID_LISTING_INCOMPLETE")
                self.assertEqual(client.call.call_count, calls)
        client = Mock()
        client.call.side_effect = lambda operation, arguments: {"totalCount": 2, "items": [a, b]}
        self.assertIsNone(inactivity_closure._lead_listing_covers_rooms(
            CONFIG, client, envelope(*[dict(chatroom(item["id"], LEAD), raw=item) for item in (a, b)]), PROFILE, LEAD))

    def test_placeholder_lead_id_on_a_bound_room_never_closes(self):
        self.thread = room([message("ME", "Original note", "2026-08-01T00:00:00.000Z")])
        self.thread["correspondentProfile"]["linkedin_id"] = "imp_QUFFSYNTHETIC"
        self.lead_threads = [self.thread]
        first, second = self.two_reads()
        self.assertEqual(first["items"][0]["blocker"], "PERSON_LEAD_ID_NOT_ENRICHED", first)
        self.assertIsNone(engagement.closure_status(self.database, PROFILE))

    def test_undecodable_room_with_a_recent_reply_never_closes(self):
        # A reply we cannot decode is never read as silence. (A lagging room summary, the case
        # this test first used, decodes since the inbox-reading change of 2026-09-30; an InMail
        # reply is still refused.)
        engagement.record_event(self.database, event(PROFILE, "accepted", "2026-08-01T00:00:00+00:00", "accepted", {}))
        reply = message("CORRESPONDENT", "Yes, interested!", "2026-09-17T00:00:00.000Z")
        reply["isInMail"] = True
        self.thread = room([message("ME", "Original", "2026-08-01T00:00:00.000Z"), reply])
        result = self.cycle(NOW)
        self.assertEqual(result["closed"], 0, result)
        self.assertIn("CHATROOM_UNDECODABLE", result["items"][0]["incomplete_reasons"], result)

    def test_unusable_record_after_a_complete_read_never_closes(self):
        future = (NOW + dt.timedelta(days=1)).isoformat()

        def refresh(profile, now):  # a complete read whose event was observed after now
            return {"state": "COMPLETE", "evidence": "synthetic", "acceptance_observable": True,
                    "events": [dict(event(PROFILE, "inbound", "2026-08-05T00:00:00+00:00", "late", {}), observed_at=future)]}

        result = inactivity_closure.cycle(self.database, CONFIG, Mock(), NOW, refresh=refresh)
        self.assertEqual(result["items"][0]["blocker"], "INVALID_ACTIVITY_TIMESTAMP_OR_EVIDENCE", result)
        self.assertIsNone(engagement.closure_status(self.database, PROFILE))

    def test_campaign_acceptance_needs_one_lead_and_one_sent_invitation(self):
        self.campaign_person()
        anchor = dict(inactivity_closure.stored_anchor(self.database, PROFILE),
                      aliases=engagement.identity_keys(self.database, PROFILE))
        client = Mock()
        client.call.side_effect = self.call
        self.assertEqual(inactivity_closure._heyreach_campaign_acceptance(CONFIG, client, PROFILE, anchor)[0], "PENDING")
        for name, operation, response in (
                ("two leads", "get_leads_from_list", {"totalCount": 2, "items": [{"profileUrl": PROFILE}, {"profileUrl": MEMBER}]}),
                ("two invitations", "get_overall_stats", {"overallStats": {"connectionsSent": 2, "connectionsAccepted": 0}})):
            with self.subTest(name):
                client = Mock()
                client.call.side_effect = lambda op, arguments, op_=operation, r=response: r if op == op_ else self.call(op, arguments)
                with self.assertRaises(ValueError):
                    inactivity_closure._heyreach_campaign_acceptance(CONFIG, client, PROFILE, anchor)

    def test_invalid_heyreach_session_never_closes(self):
        # An invalid session stops inbox and stats syncing: zero conversations, zero acceptances.
        self.campaign_person()
        self.auth_valid = False
        result = self.cycle(NOW)
        self.assertEqual(result["closed"], 0, result)
        self.assertEqual(result["items"][0]["blocker"], "PROVIDER_AUTH_INVALID", result)

    def test_unenriched_lead_id_with_no_chatroom_never_closes(self):
        self.lead = {"profileUrl": PROFILE, "linkedin_id": "imp_QUFFSYNTHETIC"}
        first, second = self.two_reads()
        self.assertEqual(first["items"][0]["blocker"], "PERSON_LEAD_ID_NOT_ENRICHED", first)
        self.assertIsNone(engagement.closure_status(self.database, PROFILE))

    def test_new_inbound_decoded_from_a_bound_chatroom_moves_the_anchor(self):
        self.thread = room([message("ME", "Original note", "2026-08-01T00:00:00.000Z"),
                            message("CORRESPONDENT", "Thanks, happy to talk", "2026-09-15T00:00:00.000Z")])
        result = self.cycle(NOW)
        self.assertEqual(result["closed"], 0, result)
        planned = inactivity_closure.plan(self.database, PROFILE, NOW)
        self.assertEqual((planned["state"], engagement.timestamp(planned["anchor_at"])),
                         ("ACTIVE", dt.datetime(2026, 9, 15, tzinfo=dt.timezone.utc)), planned)

    def test_linked_member_id_person_is_read_by_profile_url(self):
        engagement.record_event(self.database, event(MEMBER, "inbound", "2026-08-02T00:00:00+00:00",
                                                     "member-inbound", {"provider_lead_id": "115000001"}))
        research_workflow.register_alias(self.database, "contact", MEMBER, PROFILE, {"source": "synthetic"})
        with self.database:  # the member-id spelling is then the only candidate spelling
            self.database.execute("DELETE FROM history")
            self.database.execute("DELETE FROM engagement_events WHERE identity=?", (orchestrator.profile_key(PROFILE),))
        self.assertEqual(inactivity_closure.candidates(self.database), [orchestrator.profile_key(MEMBER)])
        calls = []

        def refresh(profile, now):
            calls.append(profile)
            return {"state": "UNKNOWN", "blocker": "synthetic"}

        inactivity_closure.cycle(self.database, CONFIG, Mock(), NOW, refresh=refresh)
        self.assertEqual(calls, [orchestrator.profile_key(PROFILE)])


def envelope(*reads, alias_used=True):
    return {"finished_at": NOW.isoformat(), "identity_verified": True, "selected_conversation_id": None,
            "coverage": {"account": {"status": "OBSERVED"},
                         "get_conversations_v2": {"status": "BOUNDED_SCAN_COMPLETE", "provider_lead_alias_used": alias_used},
                         "chatrooms": {"status": "BOUNDED_SCAN_COMPLETE"}},
            "observations": [{"operation": "get_linked_in_account_by_id", "arguments": {}, "status": "OBSERVED",
                              "raw": {"id": 424242, "profileUrl": SENDER, "authIsValid": True}}] + list(reads)}


def chatroom(thread_id, lead_id, matches=True):
    raw = room([message("ME", "Original note", "2026-08-01T00:00:00.000Z")])
    raw["id"] = thread_id
    raw["correspondentProfile"] = dict(raw["correspondentProfile"], linkedin_id=lead_id)
    return {"operation": "get_chatroom", "arguments": {}, "authenticated": True, "identity_matches": matches,
            "message_coverage": "OBSERVED_COUNT_MATCHES", "raw": raw, "status": "OBSERVED"}


def lead_listing(lead_id):
    return {"operation": "get_conversations_v2", "arguments": {"leadLinkedInId": lead_id},
            "status": "OBSERVED", "raw": {"totalCount": 0, "items": []}}


class LeadIdBinding(unittest.TestCase):
    """refresh_activity against a fixed provider envelope (the read's own checks)."""

    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(directory)
        (root / "state").mkdir()
        self.database = orchestrator.connect(root)
        self.addCleanup(self.database.close)
        inactivity_closure.initialize(self.database)
        research_workflow.initialize(self.database)
        engagement.record_event(self.database, event(PROFILE, "initial_sent", "2026-08-01T00:00:00+00:00",
                                                     "initial", {}))

    def read(self, observed, client=None):
        provider = Mock()
        provider.return_value.collect.return_value = observed
        with patch.object(outreach_provider, "Provider", provider):
            return inactivity_closure.refresh_activity(self.database, CONFIG, client or Mock(), PROFILE, NOW)

    def test_each_completeness_condition_alone_keeps_the_read_incomplete(self):
        base = envelope(lead_listing("111"))
        self.assertEqual(self.read(base)["state"], "COMPLETE")
        for name, change in {
                "identity unverified": lambda observed: observed.update(identity_verified=False),
                "account unobserved": lambda observed: observed["coverage"]["account"].update(status="UNKNOWN"),
                "conversation scan incomplete": lambda observed: observed["coverage"]["get_conversations_v2"].update(status="UNKNOWN"),
                "chatroom scan incomplete": lambda observed: observed["coverage"]["chatrooms"].update(status="UNKNOWN")}.items():
            with self.subTest(name):
                observed = envelope(lead_listing("111"))
                change(observed)
                self.assertEqual(self.read(observed)["state"], "UNKNOWN")

    def test_chatroom_read_must_be_authenticated_and_count_matched(self):
        def covering(room_read):
            client = Mock()
            client.call.side_effect = lambda operation, arguments: {"totalCount": 1, "items": [room_read["raw"]]}
            return client

        good = chatroom("t1", "111")
        self.assertEqual(self.read(envelope(good), covering(good))["state"], "COMPLETE")
        for field, value in (("authenticated", False), ("message_coverage", "UNKNOWN")):
            with self.subTest(field):
                bad = dict(chatroom("t1", "111"), **{field: value})
                result = self.read(envelope(bad), covering(bad))
                self.assertEqual(result["state"], "UNKNOWN")
                self.assertIn("CHATROOM_UNVERIFIED_OR_INCOMPLETE", result["incomplete_reasons"])

    def test_non_dict_listing_item_is_a_conversation_not_read(self):
        good = chatroom("t1", "111")
        client = Mock()
        client.call.side_effect = lambda operation, arguments: {"totalCount": 2, "items": [good["raw"], "junk"]}
        self.assertEqual(inactivity_closure._lead_listing_covers_rooms(CONFIG, client, envelope(good), PROFILE, "111"),
                         "LEAD_ID_CONVERSATION_NOT_READ")

    def member(self, raw, profile=MEMBER):
        engagement.record_event(self.database, event(profile, "inbound", "2026-08-02T00:00:00+00:00",
                                                     "inbound-" + profile.rsplit("/", 1)[-1], raw))

    def test_lead_ids_come_only_from_rooms_matched_to_the_person(self):
        result = self.read(envelope(chatroom("t1", "111"), chatroom("t2", "222", matches=False)))
        self.assertEqual(result["member_id_issues"], [], result["incomplete_reasons"])

    def test_two_lead_ids_for_one_person_are_ambiguous(self):
        result = self.read(envelope(chatroom("t1", "111"), chatroom("t2", "222")))
        self.assertTrue(result["member_id_issues"][0]["reason"].startswith("PERSON_LEAD_ID_AMBIGUOUS"), result)
        self.assertEqual(result["state"], "UNKNOWN")

    def test_member_id_with_two_lead_ids_is_never_linked(self):
        self.member({"provider_lead_id": "111", "correspondent_id": "999"})
        result = self.read(envelope(chatroom("t1", "111")))
        self.assertEqual(result["member_id_issues"][0]["reason"], "MEMBER_ID_LEAD_IDS_AMBIGUOUS", result)
        self.assertNotIn(orchestrator.profile_key(MEMBER), engagement.identity_keys(self.database, PROFILE))

    def test_zero_room_read_without_the_lead_id_listing_is_incomplete(self):
        result = self.read(envelope(alias_used=False))
        self.assertIn("MEMBER_ID_CONVERSATION_LOOKUP_NOT_COMPLETE", result["incomplete_reasons"], result)
        self.assertEqual(result["state"], "UNKNOWN")

    def test_zero_room_read_with_two_lead_id_listings_is_incomplete(self):
        result = self.read(envelope(lead_listing("111"), lead_listing("222")))
        self.assertIn("MEMBER_ID_CONVERSATION_LOOKUP_NOT_COMPLETE", result["incomplete_reasons"], result)

    def test_saved_conversation_counts_when_an_observed_listing_shows_it(self):
        self.member({"conversation_id": "old-thread"}, profile=PROFILE)
        listing = dict(lead_listing("111"), raw={"totalCount": 1, "items": [{"id": "old-thread"}]})
        result = self.read(envelope(listing))
        self.assertNotIn("SAVED_CONVERSATION_NOT_IN_READ", result["incomplete_reasons"], result)
        failed = dict(listing, status="UNKNOWN")
        result = self.read(envelope(failed))
        self.assertIn("SAVED_CONVERSATION_NOT_IN_READ", result["incomplete_reasons"], result)

    def test_member_id_identity_with_no_lead_for_the_person_is_unresolved(self):
        self.member({"provider_lead_id": "111"})
        result = self.read(envelope(alias_used=False))
        self.assertEqual(result["member_id_issues"][0]["reason"], "PERSON_LEAD_ID_NOT_OBSERVED", result)

    def test_bound_room_without_a_lead_id_is_incomplete(self):
        result = self.read(envelope(chatroom("t1", None)))
        self.assertIn("PERSON_LEAD_ID_NOT_IN_READ", result["incomplete_reasons"], result)

    def test_placeholder_lead_id_in_a_bound_room_is_not_comparable(self):
        self.member({"provider_lead_id": "111"})
        result = self.read(envelope(chatroom("t1", "imp_QUFFSYNTHETIC")))
        self.assertEqual(result["member_id_issues"][0]["reason"], "LEAD_IDS_NOT_COMPARABLE", result)

    def test_any_spelling_with_the_same_lead_id_is_linked(self):
        old = "https://www.linkedin.com/in/old-vanity-spelling"
        self.member({"provider_lead_id": "111"}, profile=old)
        result = self.read(envelope(chatroom("t1", "111")))
        self.assertEqual(result["member_id_issues"], [], result)
        self.assertIn(orchestrator.profile_key(old), engagement.identity_keys(self.database, PROFILE))

    def test_any_spelling_with_a_placeholder_lead_id_is_not_comparable(self):
        self.member({"provider_lead_id": "imp_OTHER"}, profile="https://www.linkedin.com/in/imported-person")
        result = self.read(envelope(chatroom("t1", "111")))
        self.assertEqual(result["member_id_issues"][0]["reason"], "LEAD_IDS_NOT_COMPARABLE", result)

    def test_ordinary_profile_identities_never_stall_the_member_id_check(self):
        self.member({}, profile="https://www.linkedin.com/in/someone-else")
        result = self.read(envelope(lead_listing("111")))
        self.assertEqual(result["member_id_issues"], [], result)
        self.assertEqual(result["state"], "COMPLETE", result)


if __name__ == "__main__":
    unittest.main()
