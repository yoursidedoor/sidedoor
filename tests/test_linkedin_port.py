"""E3.T1: LinkedInPort contract (FakePort) and the HeyReach adapter over the existing fakes."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime
import random
import string
import unittest
import urllib.parse
import unittest.mock

import linkedin_port
from linkedin_port import FakePort, HeyReachPort, IncompletePage, SessionInvalid, UncertainWrite, Unsupported
import test_outreach_provider as heyreach_fixture
import unipile_port


ALICE = "https://www.linkedin.com/in/alice"


class Conformance(unittest.TestCase):
    def test_adapters_implement_every_port_method(self):
        for port in (FakePort(), HeyReachPort(object(), 1), unipile_port.UnipilePort(object())):
            self.assertIsInstance(port, linkedin_port.LinkedInPort)
            for name in linkedin_port.PORT_METHODS:
                self.assertTrue(callable(getattr(port, name)), (port.provider, name))

    def test_port_methods_is_the_protocol(self):
        import inspect
        declared = {name for name, value in vars(linkedin_port.LinkedInPort).items()
                    if inspect.isfunction(value) and not name.startswith("_")}
        self.assertEqual(set(linkedin_port.PORT_METHODS), declared)
        self.assertEqual(len(linkedin_port.PORT_METHODS), len(declared))

    def test_error_codes(self):
        codes = {linkedin_port.PortError: "PORT_ERROR", SessionInvalid: "SESSION_INVALID",
                 IncompletePage: "INCOMPLETE_PAGE", UncertainWrite: "UNCERTAIN_WRITE",
                 Unsupported: "UNSUPPORTED", linkedin_port.NotFound: "NOT_FOUND"}
        for error, code in codes.items():
            self.assertEqual(error.code, code)
            self.assertTrue(issubclass(error, linkedin_port.PortError))
            self.assertEqual(str(error()), code)                  # default message is the code
            self.assertEqual(str(error("detail")), "detail")


class FakePortContract(unittest.TestCase):
    def setUp(self):
        self.port = FakePort()
        self.alice = self.port.add_person(ALICE)

    def test_account_and_lookup(self):
        self.assertIs(self.port.account().valid, True)
        person = self.port.lookup(ALICE)
        self.assertEqual((person.provider_id, person.network_distance, person.pending_invitation),
                         (self.alice, 2, False))

    def test_invitation_then_acceptance(self):
        ack = self.port.send_invitation(self.alice, "Hello")
        self.assertEqual(ack.status, "ACKNOWLEDGED_UNCONFIRMED")
        self.assertIs(self.port.lookup(ALICE).pending_invitation, True)
        self.assertFalse(linkedin_port.invitation_absent(self.port, self.alice, ALICE))
        self.port.accept(self.alice)
        self.assertEqual(self.port.lookup(ALICE).network_distance, 1)
        self.assertEqual([person.provider_id for person in linkedin_port.all_items(self.port.relations)], [self.alice])
        self.assertFalse(linkedin_port.invitation_absent(self.port, self.alice, ALICE))

    def test_timeout_after_write_landed_is_never_absence(self):
        self.port.fail("send_invitation", "timeout_after_write")
        with self.assertRaises(UncertainWrite):
            self.port.send_invitation(self.alice, "Hello")
        self.assertFalse(linkedin_port.invitation_absent(self.port, self.alice, ALICE))
        self.assertFalse(linkedin_port.may_retry_invitation(1, False))

    def test_timeout_before_write_allows_a_retry_after_the_read(self):
        self.port.fail("send_invitation", "timeout")
        with self.assertRaises(UncertainWrite):
            self.port.send_invitation(self.alice, "Hello")
        absent = linkedin_port.invitation_absent(self.port, self.alice, ALICE)
        self.assertTrue(absent)
        self.assertTrue(linkedin_port.may_retry_invitation(1, absent))
        self.assertTrue(linkedin_port.may_retry_invitation(2, absent))
        self.assertFalse(linkedin_port.may_retry_invitation(3, absent))   # at most 3 attempts in all
        self.assertFalse(linkedin_port.may_retry_invitation(1, None))

    def test_invitation_fields_are_the_invitation_id_and_the_invitee(self):
        self.port.send_invitation(self.alice, "Hello")
        listed = linkedin_port.all_items(self.port.sent_invitations)[0]
        self.assertTrue(listed.invitation_id.startswith("inv-"))
        self.assertEqual((listed.invitee_provider_id, listed.invitee_profile), (self.alice, ALICE))

    def test_invitation_found_by_invitee_profile_in_any_spelling(self):
        port = FakePort()
        port.add_person(ALICE, provider_id="member-123")
        port.add_invitation(invitee_profile="https://www.linkedin.com/in/alice")   # no invitee id given
        for spelling in ("linkedin.com/in/alice", "https://www.linkedin.com/in/alice/",
                         "https://linkedin.com/in/ALICE", ALICE):
            self.assertFalse(linkedin_port.invitation_absent(port, None, spelling), spelling)

    def test_invitation_found_by_invitee_id_from_lookup(self):
        port = FakePort()
        port.add_person(ALICE, provider_id="member-123")
        port.add_invitation(invitee_provider_id="member-123")   # no profile given
        self.assertFalse(linkedin_port.invitation_absent(port, None, ALICE))

    def test_unidentifiable_invitation_is_never_absence(self):
        for profile in (None, "not a profile url"):
            port = FakePort()
            port.add_person(ALICE)
            port.add_invitation(invitee_profile=profile)
            with self.assertRaisesRegex(IncompletePage, "INVITATION_INVITEE_UNIDENTIFIED"):
                linkedin_port.invitation_absent(port, None, ALICE)

    def test_other_peoples_invitations_leave_this_person_absent(self):
        bob = self.port.add_person("https://www.linkedin.com/in/bob")
        self.port.send_invitation(bob, "Hi Bob")
        self.port.add_invitation(invitee_profile="https://www.linkedin.com/in/carol")
        self.assertTrue(linkedin_port.invitation_absent(self.port, self.alice, ALICE))

    def test_connected_person_is_never_absent_and_the_fake_carries_no_gate(self):
        self.port.accept(self.alice)
        self.assertFalse(linkedin_port.invitation_absent(self.port, self.alice, ALICE))
        self.port.send_invitation(self.alice, "n")   # adapters carry no gates; callers use invitation_absent

    def test_incomplete_page_raises_and_is_never_absence(self):
        for index in range(5):
            self.port.send_invitation(self.port.add_person(ALICE + str(index)), "n")
        self.port.fail("sent_invitations", "incomplete_page")
        with self.assertRaises(IncompletePage):
            linkedin_port.invitation_absent(self.port, self.alice, ALICE)
        self.assertEqual(len(linkedin_port.all_items(self.port.sent_invitations)), 5)   # paged, page_size 2

    def test_invalid_session(self):
        self.port.fail("chats", "invalid_session")
        with self.assertRaises(SessionInvalid):
            self.port.chats()
        self.assertIs(self.port.account().valid, False)
        for call in (lambda: self.port.lookup(ALICE), lambda: self.port.send_message("x", "y"),
                     lambda: self.port.sent_invitations()):
            with self.assertRaises(SessionInvalid):
                call()

    def test_chats_messages_send_and_start_chat(self):
        chat = self.port.add_chat(self.alice, [(False, "Hi there")])
        self.port.send_message(chat, "Thanks")
        thread = self.port.messages(chat)
        self.assertTrue(thread.complete)
        self.assertEqual([(m.is_sender, m.text) for m in thread.messages], [(False, "Hi there"), (True, "Thanks")])
        bob = self.port.add_person("https://www.linkedin.com/in/bob")
        started = self.port.start_chat(bob, "Hello Bob")
        newest = self.port.chats().items[0]
        self.assertEqual((newest.chat_id, newest.attendee_provider_id), (started.raw["chat_id"], bob))
        self.assertEqual([item.chat_id for item in self.port.chats(person=ALICE).items], [chat])

    def test_chats_person_is_a_profile_url_in_any_spelling(self):
        chat = self.port.add_chat(self.alice, [(False, "Hi")])
        for spelling in (ALICE, "https://linkedin.com/in/Alice/", "linkedin.com/in/alice"):
            page = self.port.chats(person=spelling)
            self.assertEqual(([item.chat_id for item in page.items], page.complete), ([chat], True), spelling)
        self.port.add_person("https://www.linkedin.com/in/carol")
        self.assertEqual(self.port.chats(person="https://www.linkedin.com/in/carol").items, ())   # truly no chat
        with self.assertRaises(linkedin_port.NotFound):          # unresolvable: never a complete empty page
            self.port.chats(person="https://www.linkedin.com/in/nobody")
        self.assertEqual(self.port.lookup("linkedin.com/in/ALICE").provider_id, self.alice)

    def test_member_id_url_is_not_equated_with_the_vanity_url(self):
        bob = self.port.add_person("https://www.linkedin.com/in/bob", provider_id="ACoAAbob")
        self.port.add_chat(bob, [(False, "Hi")])
        for call in (lambda: self.port.chats(person="https://www.linkedin.com/in/ACoAAbob"),
                     lambda: self.port.lookup("https://www.linkedin.com/in/ACoAAbob")):
            with self.assertRaises(linkedin_port.NotFound):
                call()

    def test_unknown_person_or_chat_raises_not_found(self):
        for call in (lambda: self.port.lookup("https://www.linkedin.com/in/nobody"),
                     lambda: self.port.messages("no-such-chat"),
                     lambda: self.port.send_message("no-such-chat", "x")):
            with self.assertRaises(linkedin_port.NotFound):
                call()
        with self.assertRaises(linkedin_port.NotFound):    # unknown is never "absent"
            linkedin_port.invitation_absent(self.port, "prov-x", "https://www.linkedin.com/in/nobody")

    def test_chats_are_ordered_by_latest_message_and_threads_oldest_first(self):
        older = self.port.add_chat(self.alice, [(False, "first")])
        newer = self.port.add_chat(self.port.add_person("https://www.linkedin.com/in/dan"), [(False, "hi")])
        self.assertEqual([chat.chat_id for chat in self.port.chats().items], [newer, older])
        self.port.send_message(older, "second")
        self.assertEqual([chat.chat_id for chat in self.port.chats().items], [older, newer])
        thread = self.port.messages(older)
        self.assertEqual([message.text for message in thread.messages], ["first", "second"])
        for message in thread.messages:
            self.assertIsNotNone(datetime.datetime.fromisoformat(message.sent_at).utcoffset())

    def test_message_timeout_after_write(self):
        chat = self.port.add_chat(self.alice)
        self.port.fail("send_message", "timeout_after_write")
        with self.assertRaises(UncertainWrite):
            self.port.send_message(chat, "Once")
        self.assertEqual([m.text for m in self.port.messages(chat).messages], ["Once"])


class PagedInvitations:
    """lookup() as given; sent_invitations() paged `page_size` at a time."""
    provider = "stub"

    def __init__(self, person, entries, page_size):
        self.person, self.entries, self.page_size = person, entries, page_size

    def lookup(self, profile_url):
        return self.person

    def sent_invitations(self, cursor=None):
        start = int(cursor or 0)
        chunk = self.entries[start:start + self.page_size]
        end = start + len(chunk)
        return linkedin_port.Page(tuple(chunk), str(end) if end < len(self.entries) else None, len(self.entries), True)


class StubInvitations:
    """lookup() and sent_invitations() exactly as given, so the listing alone decides."""
    provider = "stub"

    def __init__(self, person, entries):
        self.person, self.entries = person, entries

    def lookup(self, profile_url):
        return self.person

    def sent_invitations(self, cursor=None):
        return linkedin_port.Page(tuple(self.entries), None, len(self.entries), True)


def entry(invitee_provider_id=None, invitee_profile=None):
    return linkedin_port.Invitation("inv-1", invitee_provider_id, invitee_profile, None, None)


def member(name):
    """A synthetic id with the LinkedIn member-id shape ("ACoAA" + 34 URL-safe characters)."""
    return "ACoAA" + ("Synthetic" + name).ljust(34, "0")


ALICE_ID, BOB_ID = member("Alice"), member("Bob")
ALICE_MEMBER_URL = "https://www.linkedin.com/in/" + ALICE_ID


def person(provider_id=ALICE_ID, pending=False, distance=2, profile=ALICE):
    return linkedin_port.Person(profile, provider_id, distance, pending)


class InvitationAbsentDecisions(unittest.TestCase):
    """Review rounds 5-6: lookup says not pending, so only the listing decides."""

    def absent(self, entries, caller_id=None, profile=ALICE, found=None):
        return linkedin_port.invitation_absent(StubInvitations(found or person(), entries), caller_id, profile)

    def unknown(self, entries, **named):
        with self.assertRaisesRegex(linkedin_port.IncompletePage, "UNIDENTIFIED"):
            self.absent(entries, **named)

    def test_member_id_shape(self):
        self.assertTrue(linkedin_port._member_id(ALICE_ID))
        for value in (ALICE_ID.lower(), "ACoAAalice", ALICE_ID + "x", "acoakley-jones", "ACoAB" + ALICE_ID[5:], None, 5):
            self.assertFalse(linkedin_port._member_id(value), value)

    def test_listing_match_by_lookup_id(self):
        self.assertFalse(self.absent([entry(ALICE_ID)]))

    def test_listing_match_by_caller_id(self):
        self.assertFalse(self.absent([entry("caller-7")], caller_id="caller-7", found=person(provider_id=None)))

    def test_listing_match_by_member_id_url(self):
        self.assertFalse(self.absent([entry(invitee_profile=ALICE_MEMBER_URL)]))

    def test_listing_match_by_member_id_url_written_with_fullwidth_letters(self):
        fullwidth = "".join(chr(ord(c) + 0xFEE0) if c.isalnum() else c for c in ALICE_ID)
        self.assertFalse(self.absent([entry(invitee_profile="https://www.linkedin.com/in/" + fullwidth)]))

    def test_listing_match_by_vanity_spelling(self):
        for spelling in ("linkedin.com/in/alice", "https://linkedin.com/in/ALICE/", ALICE):
            self.assertFalse(self.absent([entry(invitee_profile=spelling)], found=person(provider_id=None)), spelling)

    def test_caller_member_id_url_matches_entry_id_and_never_equates_a_vanity_url(self):
        self.assertFalse(self.absent([entry(ALICE_ID)], profile=ALICE_MEMBER_URL, found=person(provider_id=None)))
        self.unknown([entry(invitee_profile=ALICE)], profile=ALICE_MEMBER_URL, found=person(provider_id=None))

    def test_someone_else_is_absent(self):
        self.assertTrue(self.absent([]))
        self.assertTrue(self.absent([entry(BOB_ID)]))
        self.assertTrue(self.absent([entry(invitee_profile="https://www.linkedin.com/in/" + BOB_ID)]))
        self.assertTrue(self.absent([entry(invitee_profile="https://www.linkedin.com/in/bob")]))

    def test_entries_that_cannot_be_told_raise(self):
        for case in (entry(), entry(""), entry("  "), entry(115), entry(invitee_profile="not a url"),
                     entry(invitee_profile="https://www.linkedin.com/in/" + ALICE_ID.lower())):   # case lost
            self.unknown([case])
        self.unknown([entry(invitee_profile="https://www.linkedin.com/in/" + BOB_ID)], found=person(provider_id=None))
        self.assertFalse(self.absent([entry(115, invitee_profile=ALICE_MEMBER_URL)]))

    def test_an_id_with_no_known_ids_is_not_someone_else(self):
        self.unknown([entry(BOB_ID)], found=person(provider_id=None))

    def test_member_like_vanity_is_never_someone_else(self):
        # Review round 6 F1: "acoakley-jones" is not a member id; the caller gave a member-id URL.
        port = FakePort()
        port.add_person(ALICE_MEMBER_URL, provider_id=ALICE_ID)
        port.add_invitation(invitee_profile="https://www.linkedin.com/in/acoakley-jones")
        with self.assertRaisesRegex(linkedin_port.IncompletePage, "UNIDENTIFIED"):
            linkedin_port.invitation_absent(port, ALICE_ID, ALICE_MEMBER_URL)
        self.unknown([entry(invitee_profile="https://www.linkedin.com/in/acoakley-jones")])

    def test_case_only_match_cannot_be_told(self):
        # Review round 6 F2: the caller's member URL lost its letter case (a stored profile key).
        folded_url = "https://www.linkedin.com/in/" + ALICE_ID.lower()
        self.unknown([entry(ALICE_ID)], profile=folded_url, found=person(provider_id=None))
        self.unknown([entry(invitee_profile=ALICE_MEMBER_URL)], profile=folded_url, found=person(provider_id=None))
        self.unknown([entry(ALICE_ID.lower())])

    def test_member_url_entry_with_only_a_non_member_id_known_cannot_be_told(self):
        self.unknown([entry(invitee_profile="https://www.linkedin.com/in/" + BOB_ID)], found=person(provider_id="115"))

    def test_casefolded_caller_url_with_another_id_known(self):
        folded_url = "https://www.linkedin.com/in/" + ALICE_ID.lower()
        self.unknown([entry(ALICE_ID)], profile=folded_url, found=person(provider_id="115"))

    def test_short_or_sales_navigator_slugs_are_member_like(self):
        for slug in ("ACoAAshort123", "ACwAA" + ALICE_ID[5:]):
            self.unknown([entry(invitee_profile="https://www.linkedin.com/in/" + slug)])

    def test_member_url_differing_from_a_known_id_in_letter_case_only(self):
        flipped = ALICE_ID[:5] + ALICE_ID[5].swapcase() + ALICE_ID[6:]
        self.assertTrue(linkedin_port._member_id(flipped))
        self.unknown([entry(invitee_profile="https://www.linkedin.com/in/" + flipped)])

    def test_a_non_profile_caller_url_raises(self):
        with self.assertRaises(ValueError):
            self.absent([], profile="https://www.linkedin.com/sales/lead/" + ALICE_ID)

    def test_review_repro_on_fake_port(self):
        port = FakePort()
        pid = port.add_person(ALICE, provider_id=ALICE_ID)
        port.add_invitation(invitee_profile=ALICE_MEMBER_URL)
        self.assertFalse(linkedin_port.invitation_absent(port, pid, ALICE))

    def test_match_on_a_later_page(self):
        entries = [entry(BOB_ID), entry(member("Carol")), entry(invitee_profile=ALICE)]
        port = PagedInvitations(person(), entries, page_size=2)
        self.assertFalse(linkedin_port.invitation_absent(port, None, ALICE))

    def test_a_partial_listing_is_never_absence(self):
        port = FakePort()
        port.add_person(ALICE)
        port.send_invitation(port.add_person("https://www.linkedin.com/in/bob"), "n")
        port.fail("sent_invitations", "partial_page")
        with self.assertRaises(IncompletePage):
            linkedin_port.invitation_absent(port, None, ALICE)

    def test_caller_id_and_lookup_id_both_count(self):
        # N30: the caller's stored id differs from the id lookup() returns.
        self.assertFalse(self.absent([entry(ALICE_ID)], caller_id="115"))

    def test_caller_member_slug_is_in_the_case_free_set(self):
        # N04: caller member URL, lookup gives no id, entry is the same URL with one letter's case flipped.
        flipped = ALICE_ID[:5] + ALICE_ID[5].swapcase() + ALICE_ID[6:]
        self.unknown([entry(invitee_profile="https://www.linkedin.com/in/" + flipped)],
                     profile=ALICE_MEMBER_URL, found=person(provider_id=None))

    def test_member_id_length_is_exact(self):
        # N05: "ACoAA" + 34; 33 or 35 is a vanity that merely looks like one.
        for extra, expected in ((33, False), (34, True), (35, False), (23, False)):
            self.assertEqual(linkedin_port._member_id("ACoAA" + "x" * extra), expected, extra)
        self.unknown([entry(invitee_profile="https://www.linkedin.com/in/ACoAA" + "longvanityname-of-alice")],
                     profile=ALICE_MEMBER_URL)

    def test_lookup_pending_or_connected_decides_before_the_listing(self):
        self.assertFalse(self.absent([], found=person(pending=True)))
        self.assertFalse(self.absent([], found=person(distance=1)))
        for found in (person(pending=None), person(distance=None), person(pending=0), person(pending="no"),
                      person(distance="FIRST_DEGREE"), person(distance=2.0), person(distance=True),
                      person(distance=0), person(distance=-1)):
            with self.assertRaisesRegex(IncompletePage, "PROFILE_INVITATION_STATE_UNKNOWN"):
                self.absent([], found=found)


class ListingAndRetryHelpers(unittest.TestCase):
    def test_a_listing_that_never_ends_raises(self):
        endless = lambda cursor: linkedin_port.Page((1,), str(int(cursor or 0) + 1), None, True)
        with self.assertRaisesRegex(IncompletePage, "PAGE_LIMIT"):
            linkedin_port.all_items(endless, limit_pages=3)

    def test_only_true_absence_allows_a_retry(self):
        for absent in (1, "yes", [1], None, False):
            self.assertFalse(linkedin_port.may_retry_invitation(1, absent), absent)
        self.assertTrue(linkedin_port.may_retry_invitation(0, True))


class FakePortUnknownPeople(unittest.TestCase):
    def test_writes_to_an_unknown_person_raise_not_found(self):
        port = FakePort()
        for call in (lambda: port.send_invitation("prov-unknown", "n"), lambda: port.start_chat("prov-unknown", "t")):
            with self.assertRaises(linkedin_port.NotFound):
                call()
        self.assertEqual((port.invitations, port.chat_log), ([], {}))


class HeyReachAdapter(unittest.TestCase):
    """Reuses OutreachProviderTests' HeyReach Mock client and room/account shapes."""

    def setUp(self):
        fixture = heyreach_fixture.OutreachProviderTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.client = fixture.client
        self.sender = fixture.config["sender_id"]
        self.port = HeyReachPort(self.client, self.sender)

    def test_reads_make_the_same_calls_and_keep_raw(self):
        account = self.port.account()
        self.assertEqual(account.raw, self.fixture.account)
        self.assertIs(account.valid, True)
        thread = self.port.messages("synthetic-room")
        self.assertEqual(thread.raw, self.fixture.room)
        self.assertTrue(thread.complete)
        page = self.port.chats(person=self.fixture.action["profile"])
        self.assertEqual(page.raw, self.fixture.responses["get_conversations_v2"])
        self.assertTrue(page.complete)
        self.assertIsNone(page.next_cursor)
        self.assertEqual(page.items[0].chat_id, "synthetic-room")
        self.port.chats(cursor="100", lead_id="saved-lead", limit=50)
        self.assertEqual(self.client.call.call_args_list, [
            unittest.mock.call("get_linked_in_account_by_id", {"accountId": self.sender}),
            unittest.mock.call("get_chatroom", {"accountId": self.sender, "conversationId": "synthetic-room"}),
            unittest.mock.call("get_conversations_v2", {"linkedInAccountIds": [self.sender],
                               "leadProfileUrl": self.fixture.action["profile"], "offset": 0, "limit": 100}),
            unittest.mock.call("get_conversations_v2", {"linkedInAccountIds": [self.sender],
                               "leadLinkedInId": "saved-lead", "offset": 100, "limit": 50})])

    def test_request_bodies_keep_their_key_order(self):
        import json
        self.port.chats(cursor=200, limit=100)
        self.port.chats(cursor=0, lead_id="7")
        bodies = [json.dumps(entry.args[1]) for entry in self.client.call.call_args_list]
        self.assertEqual(bodies, [
            json.dumps({"linkedInAccountIds": [self.sender], "limit": 100, "offset": 200}),
            json.dumps({"linkedInAccountIds": [self.sender], "leadLinkedInId": "7", "offset": 0, "limit": 100})])

    def test_lookup_reports_lead_id_and_unknown_invitation_state(self):
        self.fixture.responses["get_lead"] = {"profileUrl": ALICE, "linkedin_id": "115"}
        person = self.port.lookup(ALICE)
        self.assertEqual((person.provider_id, person.network_distance, person.pending_invitation), ("115", None, None))
        self.client.call.assert_called_with("get_lead", {"profileUrl": ALICE})

    def test_send_message_is_the_existing_request(self):
        ack = self.port.send_message("synthetic-room", "Hello", subject="")
        self.client._request.assert_called_once_with("send_message", {
            "conversationId": "synthetic-room", "linkedInAccountId": self.sender, "message": "Hello", "subject": ""})
        self.assertEqual(ack.raw, {"success": True, "opaque": {"retained": [1, 2]}})

    def test_client_errors_propagate_unchanged_and_odd_shapes_are_incomplete(self):
        self.fixture.responses["get_chatroom"] = RuntimeError("boom")
        with self.assertRaisesRegex(RuntimeError, "boom"):
            self.port.messages("synthetic-room")
        self.fixture.responses["get_conversations_v2"] = {"text": "prose", "parsed": False}
        page = self.port.chats()
        self.assertFalse(page.complete)
        self.assertEqual(page.raw, {"text": "prose", "parsed": False})

    def test_invitations_stay_on_the_campaign_path(self):
        for call in (lambda: self.port.send_invitation("1", "n"), lambda: self.port.sent_invitations(),
                     lambda: self.port.relations(), lambda: self.port.start_chat("1", "t")):
            with self.assertRaises(Unsupported):
                call()
        self.client._request.assert_not_called()



class MalformedAnswersKeepPrePortOutcome(unittest.TestCase):
    """Review round 1: neutral shaping must never raise, so each moved call site sees the raw
    answer and decides exactly as before the port."""

    SENDER = 900001

    def odd_item(self):
        return {"id": "room-1", "correspondentProfile": "not-a-dict", "linkedInAccountId": self.SENDER}

    def odd_room(self):
        return {"id": "room-1", "totalMessages": 1, "messages": [{"sender": ["ME"], "body": ["x"], "createdAt": 5}]}

    def test_shaping_is_total(self):
        client = unittest.mock.Mock(spec=["call", "_request"])
        client.call.side_effect = lambda tool, arguments: (
            {"totalCount": 2, "items": [self.odd_item(), {"id": 7, "correspondentProfile": ["x"]}]}
            if tool == "get_conversations_v2" else self.odd_room())
        port = HeyReachPort(client, self.SENDER)
        page = port.chats()
        self.assertEqual([(chat.chat_id, chat.attendee_profile) for chat in page.items],
                         [("room-1", None), (None, None)])
        thread = port.messages("room-1")
        self.assertEqual((thread.messages[0].is_sender, thread.messages[0].text), (None, None))
        self.assertEqual(thread.raw, self.odd_room())

    def test_engagement_page_holds_only_the_bad_conversation(self):
        import engagement
        client = unittest.mock.Mock(spec=["call"])
        client.call.side_effect = lambda tool, arguments: (
            {"totalCount": 1, "items": [self.odd_item()]} if tool == "get_conversations_v2" else self.odd_room())

        def decode_conversation(item):
            return {"conversation_id": item["id"], "profile": "https://www.linkedin.com/in/someone"}

        def decode_chatroom(room, conversation):
            if not all(isinstance(message.get("sender"), str) for message in room["messages"]):
                raise ValueError("Unknown sender")
            return []

        adapter = engagement.HeyReachReadAdapter(client, self.SENDER, decode_conversation, decode_chatroom)
        page = adapter.read_page("heyreach", "0")
        self.assertEqual(len(page["failures"]), 1)
        self.assertNotEqual(page["failures"][0].get("stage"), "chatroom")   # the decoder refused it
        self.assertTrue(page["done"])

    def test_inactivity_lead_listing_records_the_raw_item(self):
        import inactivity_closure
        client = unittest.mock.Mock(spec=["call"])
        client.call.return_value = {"totalCount": 1, "items": [self.odd_item()]}
        observation = {"observations": []}
        reason = inactivity_closure._lead_listing_covers_rooms(
            {"sender_id": self.SENDER, "sender_profile": "https://www.linkedin.com/in/sender"},
            client, observation, "https://www.linkedin.com/in/someone", "115")
        self.assertEqual(reason, "LEAD_ID_CONVERSATION_NOT_READ")
        self.assertEqual(observation["observations"][0]["status"], "OBSERVED")
        self.assertEqual(observation["observations"][0]["raw"], {"totalCount": 1, "items": [self.odd_item()]})


class Round8Fidelity(unittest.TestCase):
    """Review round 8: each guarded line of the port and the fake has a test."""

    def test_entry_id_is_stripped(self):
        port = StubInvitations(person(), [entry(ALICE_ID + " ")])
        self.assertFalse(linkedin_port.invitation_absent(port, None, ALICE))

    def test_member_id_allows_underscore_and_dash(self):
        value = "ACoAA" + "Syn_the-tic".ljust(34, "0")
        self.assertTrue(linkedin_port._member_id(value))

    def test_all_items_page_limit_is_exact(self):
        def pages(count):
            return lambda cursor: linkedin_port.Page((1,), None if int(cursor or 0) + 1 >= count else str(int(cursor or 0) + 1),
                                                     None, True)
        self.assertEqual(len(linkedin_port.all_items(pages(3), limit_pages=3)), 3)
        with self.assertRaisesRegex(IncompletePage, "PAGE_LIMIT"):
            linkedin_port.all_items(pages(4), limit_pages=3)

    def test_relations_lists_only_connections(self):
        port = FakePort()
        connected = port.add_person(ALICE, distance=1)
        port.add_person("https://www.linkedin.com/in/bob", distance=2)
        self.assertEqual([item.provider_id for item in linkedin_port.all_items(port.relations)], [connected])

    def test_fake_read_timeout_is_a_timeout_error(self):
        port = FakePort()
        port.add_person(ALICE)
        port.fail("lookup", "timeout")
        with self.assertRaises(TimeoutError):
            port.lookup(ALICE)

    def test_fake_keeps_one_pending_invitation_per_person(self):
        port = FakePort()
        alice = port.add_person(ALICE)
        port.send_invitation(alice, "first")
        with self.assertRaisesRegex(ValueError, "INVITATION_ALREADY_PENDING"):
            port.send_invitation(alice, "second")
        listed = linkedin_port.all_items(port.sent_invitations)
        self.assertEqual([item.note for item in listed], ["first"])

    def test_fake_messages_honours_incomplete_page(self):
        port = FakePort()
        chat = port.add_chat(port.add_person(ALICE))
        port.fail("messages", "incomplete_page")
        with self.assertRaises(IncompletePage):
            port.messages(chat)

    def test_fake_is_sender_is_a_bool(self):
        port = FakePort()
        chat = port.add_chat(port.add_person(ALICE), [(1, "ours"), (0, "theirs")])
        self.assertEqual([(type(m.is_sender), m.is_sender) for m in port.messages(chat).messages],
                         [(bool, True), (bool, False)])

    def test_start_chat_reuses_an_existing_chat(self):
        port = FakePort()
        alice = port.add_person(ALICE)
        chat = port.add_chat(alice, [(False, "hi")])
        ack = port.start_chat(alice, "hello again")
        self.assertEqual(ack.raw["chat_id"], chat)
        self.assertEqual(list(port.chat_log), [chat])
        self.assertEqual([m.text for m in port.messages(chat).messages], ["hi", "hello again"])
        bob = port.add_person("https://www.linkedin.com/in/bob")
        new = port.start_chat(bob, "hello Bob").raw["chat_id"]
        self.assertNotEqual(new, chat)
        self.assertEqual([m.text for m in port.messages(new).messages], ["hello Bob"])


class HeyReachNeutralFields(unittest.TestCase):
    """Neutral fields only: HeyReach requests and raw answers are covered by HeyReachAdapter."""

    def port(self, answer):
        client = unittest.mock.Mock(spec=["call", "_request"])
        client.call.return_value = answer
        client._request.return_value = {"ok": True}
        return HeyReachPort(client, 900001), client

    def page(self, answer, **named):
        return self.port(answer)[0].chats(**named)

    def test_chat_page_completeness(self):
        item = {"id": "c1"}
        self.assertFalse(self.page({"totalCount": 5, "items": []}).complete)            # short page
        self.assertTrue(self.page({"totalCount": 0, "items": []}).complete)             # truly empty
        self.assertFalse(self.page({"totalCount": 1, "items": [item, {"id": "c2"}]}).complete)   # over total
        self.assertFalse(self.page({"totalCount": 5, "items": [item, {"id": "c2"}]}, limit=1).complete)  # over limit
        self.assertFalse(self.page({"totalCount": 2, "items": [item, "not a dict"]}).complete)
        self.assertFalse(self.page({"totalCount": "2", "items": [item]}).complete)
        self.assertFalse(self.page({"totalCount": 5, "items": [item]}, cursor=5).complete)  # past the end

    def test_chat_page_cursor(self):
        items = [{"id": "c1"}, {"id": "c2"}]
        self.assertEqual(self.page({"totalCount": 3, "items": items}).next_cursor, "2")
        self.assertIsNone(self.page({"totalCount": 2, "items": items}).next_cursor)
        self.assertEqual(self.page({"totalCount": 5, "items": items}, cursor=2).next_cursor, "4")
        self.assertIsNone(self.page({"totalCount": 5, "items": []}).next_cursor)        # incomplete: no cursor

    def test_thread_completeness_and_senders(self):
        messages = [{"sender": "ME", "body": "a"}, {"sender": "CORRESPONDENT", "body": "b"}, {"sender": "SYSTEM"}]
        thread = self.port({"messages": messages, "totalMessages": 3})[0].messages("c1")
        self.assertTrue(thread.complete)
        self.assertEqual([m.is_sender for m in thread.messages], [True, False, None])
        for total in (2, 4, "3", None, 3.0):
            self.assertFalse(self.port({"messages": messages, "totalMessages": total})[0].messages("c1").complete, total)
        for total in (3, 4):   # a non-dict message never counts toward a complete thread
            self.assertFalse(self.port({"messages": messages + ["x"], "totalMessages": total})[0].messages("c1").complete)

    def test_account_validity_and_lookup_id_types(self):
        self.assertIs(self.port({"authIsValid": True})[0].account().valid, True)
        self.assertIs(self.port({"authIsValid": False})[0].account().valid, False)
        self.assertIsNone(self.port({"authIsValid": "true"})[0].account().valid)
        self.assertIsNone(self.port({"authIsValid": 1})[0].account().valid)
        self.assertEqual(self.port({"linkedin_id": "115"})[0].lookup(ALICE).provider_id, "115")
        self.assertIsNone(self.port({"linkedin_id": 115})[0].lookup(ALICE).provider_id)
        self.assertEqual(self.port({"profileUrl": "https://www.linkedin.com/in/me"})[0].account().own_profile,
                         "https://www.linkedin.com/in/me")
        self.assertIsNone(self.port({"profileUrl": ["x"]})[0].account().own_profile)

    def test_send_message_request_bytes_and_subject_default(self):
        import json
        port, client = self.port(None)
        ack = port.send_message("room-1", "Hello")
        expected = json.dumps({"conversationId": "room-1", "linkedInAccountId": 900001, "message": "Hello", "subject": ""})
        self.assertEqual(json.dumps(client._request.call_args.args[1]), expected)
        self.assertEqual(client._request.call_args.args[0], "send_message")
        self.assertEqual(json.dumps(ack.request), expected)


class Round9Fidelity(unittest.TestCase):
    def profile_only(self):
        port = FakePort()
        alice = port.add_person(ALICE)
        port.add_invitation(invitee_profile="linkedin.com/in/ALICE/")   # seeded by profile only, another spelling
        return port, alice

    def test_profile_only_invitation_is_pending_on_lookup(self):
        port, alice = self.profile_only()
        self.assertIs(port.lookup(ALICE).pending_invitation, True)

    def test_profile_only_invitation_blocks_a_second_one(self):
        port, alice = self.profile_only()
        with self.assertRaisesRegex(ValueError, "INVITATION_ALREADY_PENDING"):
            port.send_invitation(alice, "again")
        self.assertEqual(len(port.invitations), 1)

    def test_accept_removes_a_profile_only_invitation(self):
        port, alice = self.profile_only()
        port.add_invitation(invitee_profile="https://www.linkedin.com/in/bob")
        port.accept(alice)
        self.assertEqual([item["invitee_profile"] for item in port.invitations], ["https://www.linkedin.com/in/bob"])
        self.assertIs(port.lookup(ALICE).pending_invitation, False)

    def test_start_chat_uses_the_most_recently_active_chat(self):
        port = FakePort()
        alice = port.add_person(ALICE)
        first = port.add_chat(alice, [(False, "a")])
        second = port.add_chat(alice, [(False, "b")])
        self.assertEqual(port.start_chat(alice, "to newest").raw["chat_id"], second)
        port.send_message(first, "bump")
        self.assertEqual(port.start_chat(alice, "to bumped").raw["chat_id"], first)
        self.assertEqual(len(port.chat_log), 2)

    def test_percent_encoded_member_url_is_decoded(self):
        # N07: the slug is unquoted before the member-id comparison.
        underscored = "ACoAA" + "Synthetic_Alice".ljust(34, "0")
        encoded = "https://www.linkedin.com/in/" + underscored.replace("_", "%5F")
        port = StubInvitations(person(provider_id=underscored), [entry(invitee_profile=encoded)])
        self.assertFalse(linkedin_port.invitation_absent(port, None, ALICE))

    def test_bare_member_id_urls(self):
        # Review round 11 (Q02): a bare "linkedin.com/in/<id>" spelling is parsed like a full URL.
        bare_entry = "linkedin.com/in/" + "ACoAA" + "B" * 34
        self.assertEqual(linkedin_port._profile(bare_entry)[1], "ACoAA" + "B" * 34)
        port = StubInvitations(person(provider_id=None), [entry(invitee_profile=bare_entry)])
        with self.assertRaisesRegex(IncompletePage, "UNIDENTIFIED"):
            linkedin_port.invitation_absent(port, None, ALICE)
        port = StubInvitations(person(provider_id=None), [entry(ALICE_ID)])
        self.assertFalse(linkedin_port.invitation_absent(port, None, "linkedin.com/in/" + ALICE_ID))
        port = StubInvitations(person(provider_id=ALICE_ID), [entry(invitee_profile="linkedin.com/in/" + ALICE_ID + "/")])
        self.assertFalse(linkedin_port.invitation_absent(port, None, ALICE))

    def test_fail_times(self):
        port = FakePort()
        port.add_person(ALICE)
        port.fail("lookup", "timeout", times=2)
        for unused in range(2):
            with self.assertRaises(TimeoutError):
                port.lookup(ALICE)
        self.assertEqual(port.lookup(ALICE).profile, ALICE)

    def test_get_chatroom_request_bytes(self):
        import json
        client = unittest.mock.Mock(spec=["call", "_request"])
        client.call.return_value = {"messages": [], "totalMessages": 0}
        HeyReachPort(client, 900001).messages("room-1")
        self.assertEqual(client.call.call_args.args[0], "get_chatroom")
        self.assertEqual(json.dumps(client.call.call_args.args[1]),
                         json.dumps({"accountId": 900001, "conversationId": "room-1"}))

    def test_heyreach_person_filtered_empty_page_is_not_complete(self):
        client = unittest.mock.Mock(spec=["call", "_request"])
        client.call.return_value = {"totalCount": 0, "items": []}
        port = HeyReachPort(client, 900001)
        self.assertFalse(port.chats(person=ALICE).complete)
        self.assertTrue(port.chats().complete)
        self.assertTrue(port.chats(lead_id="115").complete)
        self.assertEqual(port.chats(person=ALICE).raw, {"totalCount": 0, "items": []})


class Round10TypeChecks(unittest.TestCase):
    """Every type check on a neutral field: bool is never an int, a non-str is never a str, a
    non-dict / non-list answer is never shaped."""

    def port(self, answer):
        client = unittest.mock.Mock(spec=["call", "_request"])
        client.call.return_value = answer
        return HeyReachPort(client, 900001)

    def test_bool_counts_are_not_ints(self):
        page = self.port({"totalCount": True, "items": [{"id": "c1"}]}).chats()
        self.assertFalse(page.complete)
        with self.assertRaises(IncompletePage):
            linkedin_port.all_items(self.port({"totalCount": True, "items": [{"id": "c1"}]}).chats)
        thread = self.port({"messages": [{"sender": "ME"}], "totalMessages": True}).messages("c1")
        self.assertFalse(thread.complete)

    def test_container_types(self):
        for answer in (None, "prose", [], {"totalCount": 0, "items": {}}, {"totalCount": 0}):
            self.assertFalse(self.port(answer).chats().complete, answer)
        for answer in (None, "prose", [], {"messages": {}, "totalMessages": 0}, {"totalMessages": 0}):
            self.assertFalse(self.port(answer).messages("c1").complete, answer)
            self.assertEqual(self.port(answer).messages("c1").messages, (), answer)
        for answer in (None, "prose", []):
            state = self.port(answer).account()
            self.assertEqual((state.valid, state.account_id, state.own_profile), (None, None, None), answer)
            self.assertIsNone(self.port(answer).lookup(ALICE).provider_id, answer)

    def test_account_id_types(self):
        for raw_id, expected in ((7, 7), ("acct", "acct"), (True, None), ([1], None), (None, None), (7.0, None)):
            self.assertEqual(self.port({"id": raw_id}).account().account_id, expected, raw_id)

    def test_chat_fields_are_strings_or_none(self):
        good = {"id": "c1", "correspondentProfile": {"linkedin_id": "115", "profileUrl": ALICE}}
        bad = {"id": 1, "correspondentProfile": {"linkedin_id": 115, "profileUrl": ["x"]}}
        page = self.port({"totalCount": 2, "items": [good, bad]}).chats()
        self.assertEqual([(c.chat_id, c.attendee_provider_id, c.attendee_profile) for c in page.items],
                         [("c1", "115", ALICE), (None, None, None)])
        for profile in ("not-a-dict", ["x"], None):
            item = {"id": "c1", "correspondentProfile": profile}
            chat = self.port({"totalCount": 1, "items": [item]}).chats().items[0]
            self.assertEqual((chat.attendee_provider_id, chat.attendee_profile), (None, None), profile)

    def test_message_fields_are_strings_or_none(self):
        messages = [{"sender": "ME", "body": "hi", "createdAt": "2026-10-01T00:00:00.000Z"},
                    {"sender": ["ME"], "body": ["x"], "createdAt": 5},
                    {"sender": 1, "body": None, "createdAt": None}]
        thread = self.port({"messages": messages, "totalMessages": 3}).messages("c1")
        self.assertEqual([(m.is_sender, m.text, m.sent_at) for m in thread.messages],
                         [(True, "hi", "2026-10-01T00:00:00.000Z"), (None, None, None), (None, None, None)])

    def test_helper_type_checks(self):
        for value in (None, 5, b"ACoAA", ["ACoAA"]):
            self.assertFalse(linkedin_port._member_id(value), value)
            self.assertFalse(linkedin_port._member_like(value), value)
            self.assertIsNone(linkedin_port._provider_id(value), value)
            self.assertIsNone(linkedin_port._text(value), value)
        for value in (None, 5, b"https://www.linkedin.com/in/alice"):
            with self.assertRaisesRegex(TypeError, "profile URL string is required"):
                linkedin_port._profile_key(value)
        self.assertEqual(linkedin_port._profile(None), (None, None))
        self.assertEqual(linkedin_port._profile("not a url"), (None, None))

    def test_fake_tolerates_unreadable_profiles(self):
        port = FakePort()
        alice = port.add_person(ALICE)
        port.add_invitation(invitee_profile="not a url")
        port.add_invitation(invitee_profile=None)
        self.assertIs(port.lookup(ALICE).pending_invitation, False)
        port.send_invitation(alice, "n")
        for bad in ("not a url", None, 5):
            with self.assertRaises(linkedin_port.NotFound):
                port.lookup(bad)


class InvitationAbsentProperty(unittest.TestCase):
    """Deterministic property sweep (from the reviewers' sweep.py), single provider-id namespace:
    every provider id is a member id. Invariant: invitation_absent returns True only when the
    lookup says pending exactly False, distance an int >= 2, and no listed entry is this person
    (generated ground truth); with an entry for this person it returns False or raises."""

    SEEDS = (1111, 2222, 3333)
    CASES = 1500
    ALPHABET = string.ascii_letters + string.digits + "_-"
    VANITIES = ["alice", "bob-smith", "acoakley-jones", "Acwa-design", "jean-luc", "zhang3", "ACoAAshort",
                "mary_ann", "%E5%BC%A0", "o-brien-12", "ACoAAlongvanityname-of-alice"]
    PREFIXES = ["https://www.linkedin.com/in/", "https://linkedin.com/in/", "linkedin.com/in/",
                "http://uk.linkedin.com/in/", "www.linkedin.com/in/"]

    def member(self, rng, prefix="ACoAA"):
        return prefix + "".join(rng.choice(self.ALPHABET) for unused in range(34))

    def spell(self, rng, slug, case=True):
        if case and rng.random() < .3:
            slug = slug.upper() if rng.random() < .5 else slug.capitalize()
        if rng.random() < .2:
            slug = urllib.parse.unquote(slug) if "%" in slug else urllib.parse.quote(slug)
        elif "%" not in slug and rng.random() < .25:     # one character percent-encoded
            index = rng.randrange(len(slug) if rng.random() < .5 else min(5, len(slug)))
            slug = slug[:index] + "%%%02X" % ord(slug[index]) + slug[index + 1:]
        elif "%" not in slug and rng.random() < .15:     # full-width letters and digits (NFKC folds them)
            slug = "".join(chr(ord(c) + 0xFEE0) if c.isascii() and c.isalnum() else c for c in slug)
        return rng.choice(self.PREFIXES) + slug + rng.choice(["", "/", "?trk=x", "/#a"])

    def entry_for(self, rng, who):
        kind = rng.choice(["member", None, None, "case", "pad", "empty"])
        invitee_id = {None: None, "member": who["member"], "pad": " " + who["member"] + " ",
                      "case": who["member"].lower() if rng.random() < .5 else who["member"].swapcase(),
                      "empty": rng.choice(["", "  "])}[kind]
        shape = rng.choice([None, "vanity", "vanity", "member", "salesnav", "junk"])
        profile = {None: None, "vanity": self.spell(rng, who["vanity"]),
                   "member": self.spell(rng, who["member"], case=False),
                   "salesnav": self.spell(rng, who["salesnav"], case=False),
                   "junk": rng.choice(["not a url", "https://www.linkedin.com/company/x",
                                       "https://www.linkedin.com/in/" + who["vanity"] + "/details"])}[shape]
        return linkedin_port.Invitation("inv", invitee_id, profile, None, None)

    def people(self, rng):
        while True:
            crowd = [{"vanity": rng.choice(self.VANITIES) + ("" if rng.random() < .5 else "-" + str(index)),
                      "member": self.member(rng), "salesnav": self.member(rng, "ACwAA")}
                     for index in range(rng.randint(1, 5))]
            keys = {linkedin_port._profile_key("https://www.linkedin.com/in/" + urllib.parse.unquote(p["vanity"]))
                    for p in crowd}
            if len(keys) == len(crowd):
                return crowd

    def test_absent_only_when_nothing_is_this_person(self):
        decided = {True: 0, False: 0, "raise": 0}
        for seed in self.SEEDS:
            rng = random.Random(seed)
            for unused in range(self.CASES):
                crowd = self.people(rng)
                me = crowd[0]
                caller = rng.choice(["vanity", "vanity", "member", "memberlower"])
                caller_url = {"vanity": self.spell(rng, me["vanity"]), "member": self.spell(rng, me["member"], case=False),
                              "memberlower": "https://www.linkedin.com/in/" + me["member"].lower()}[caller]
                caller_id = rng.choice([None, me["member"]])
                lookup_id = rng.choice([None, me["member"], me["member"]])
                pending = rng.choice([False] * 6 + [True, None, 0, "no"])
                distance = rng.choice([2] * 6 + [3, 1, 0, -1, True, 2.0, None, "FIRST_DEGREE"])
                entries, mine = [], False
                for unused in range(rng.randint(0, 4)):
                    who = rng.choice(crowd)
                    entries.append(self.entry_for(rng, who))
                    mine = mine or who is me
                port = PagedInvitations(linkedin_port.Person(caller_url, lookup_id, distance, pending),
                                        entries, page_size=rng.choice([1, 2, 3]))
                try:
                    result = linkedin_port.invitation_absent(port, caller_id, caller_url)
                except (IncompletePage, ValueError):
                    result = "raise"
                decided[result] += 1
                if result is True:
                    case = (seed, caller_url, caller_id, lookup_id, pending, distance,
                            [(e.invitee_provider_id, e.invitee_profile) for e in entries])
                    self.assertIs(pending, False, case)
                    self.assertIs(type(distance), int, case)
                    self.assertGreaterEqual(distance, 2, case)
                    self.assertFalse(mine, case)
        self.assertTrue(all(decided.values()), decided)   # the sweep reaches every outcome

if __name__ == "__main__":
    unittest.main()
