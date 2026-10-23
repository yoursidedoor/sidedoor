"""E3.T2: UnipilePort over a scripted transport replaying synthetic recorded shapes
(tests/fixtures/unipile/responses.json). No network: the transport is a dict lookup."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import copy
import io
import json
import os
import tempfile
import unittest
import unittest.mock
import urllib.error
from pathlib import Path

import linkedin_port
import unipile_port
from linkedin_port import IncompletePage, NotFound, SessionInvalid, UncertainWrite
from unipile_port import UnipilePort, UnipileHTTPError


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "unipile" / "responses.json"
RESPONSES = json.loads(FIXTURE.read_text())
ALICE = "https://www.linkedin.com/in/alice"
ALICE_ID = RESPONSES["GET /users/alice"][1]["provider_id"]
BOB_ID = RESPONSES["GET /users/bob"][1]["provider_id"]
DAN_ID = RESPONSES["GET /users/dan"][1]["provider_id"]


class ScriptedTransport:
    """(method, path[, ?cursor=...]) -> (status, parsed) from the fixture; an Exception value is
    raised; every request is recorded."""

    def __init__(self, overrides=None):
        self.responses = {key: value for key, value in RESPONSES.items() if not key.startswith("_")}
        self.responses.update(overrides or {})
        self.calls = []

    def key(self, method, path, query):
        cursor = (query or {}).get("cursor")
        return method + " " + path + ("?cursor=" + str(cursor) if cursor is not None else "")

    def __call__(self, method, path, *, query=None, body=None, multipart=None, timeout=None):
        self.calls.append({"method": method, "path": path, "query": dict(query or {}), "body": body,
                           "multipart": multipart})
        key = self.key(method, path, query)
        if key not in self.responses and path.startswith("/users/"):     # vanity slugs are case-insensitive on LinkedIn
            key = next((known for known in self.responses if known.casefold() == key.casefold()), key)
        answer = self.responses[key]
        if isinstance(answer, Exception):
            raise answer
        status, parsed = answer
        return status, copy.deepcopy(parsed)

    def requests(self, method=None):
        return [call for call in self.calls if method is None or call["method"] == method]


def port(overrides=None, **named):
    transport = ScriptedTransport(overrides)
    return UnipilePort(transport, **named), transport


class Conformance(unittest.TestCase):
    def test_unipile_port_implements_the_protocol(self):
        adapter = UnipilePort(ScriptedTransport())
        self.assertIsInstance(adapter, linkedin_port.LinkedInPort)
        self.assertEqual(adapter.provider, "unipile")
        for name in linkedin_port.PORT_METHODS:
            self.assertTrue(callable(getattr(adapter, name)), name)
        self.assertEqual(UnipileHTTPError.code, "UNIPILE_HTTP_ERROR")
        self.assertTrue(issubclass(UnipileHTTPError, linkedin_port.PortError))


class Account(unittest.TestCase):
    def test_valid_account_and_own_profile(self):
        adapter, transport = port()
        state = adapter.account()
        self.assertEqual((state.valid, state.account_id, state.own_profile),
                         (True, "acct-synthetic-1", "https://www.linkedin.com/in/synthetic-owner"))
        self.assertEqual(state.raw, RESPONSES["GET /accounts"][1])
        self.assertEqual(adapter.account_id, "acct-synthetic-1")
        self.assertEqual(transport.calls[0]["query"], {})

    def account_item(self, **changes):
        item = copy.deepcopy(RESPONSES["GET /accounts"][1]["items"][0])
        item.update(changes)
        return {"GET /accounts": [200, {"object": "AccountList", "items": [item], "cursor": None}]}

    def test_statuses(self):
        for sources, status, expected in (
                ([{"id": "s", "status": "CREDENTIALS"}], None, False),
                ([{"id": "s", "status": "OK"}], "ERROR", False),
                ([{"id": "s", "status": "CONNECTING"}], None, False),
                ([{"id": "s", "status": "OK"}, {"id": "t", "status": "STOPPED"}], None, False),
                ([{"id": "s", "status": "OK"}], "OK", True),
                ([], None, None), (None, None, None), ("OK", None, None),
                ([{"id": "s", "status": 1}], None, None)):
            changes = {"sources": sources}
            if status is not None:
                changes["status"] = status
            adapter = port(self.account_item(**changes))[0]
            self.assertIs(adapter.account().valid, expected, (sources, status))

    def test_account_selection(self):
        item = RESPONSES["GET /accounts"][1]["items"][0]
        other = dict(item, id="acct-synthetic-2")
        mail = dict(item, id="acct-mail", type="MAIL")
        two = {"GET /accounts": [200, {"items": [item, other, mail], "cursor": None}]}
        with self.assertRaisesRegex(linkedin_port.PortError, "ACCOUNT_AMBIGUOUS"):
            port(two)[0].account()
        self.assertEqual(port(two, account_id="acct-synthetic-2")[0].account().account_id, "acct-synthetic-2")
        with self.assertRaisesRegex(NotFound, "LINKEDIN_ACCOUNT_NOT_FOUND"):
            port({"GET /accounts": [200, {"items": [mail], "cursor": None}]})[0].account()
        with self.assertRaisesRegex(NotFound, "LINKEDIN_ACCOUNT_NOT_FOUND"):
            port(two, account_id="acct-unknown")[0].account()
        for answer in (None, "prose", [], {"items": "x"}):
            with self.assertRaises(NotFound):
                port({"GET /accounts": [200, answer]})[0].account()

    def test_own_profile_and_id_shaping(self):
        state = port(self.account_item(connection_params={"im": {"id": "x"}}))[0].account()
        self.assertIsNone(state.own_profile)
        state = port(self.account_item(connection_params="odd"))[0].account()
        self.assertIsNone(state.own_profile)
        state = port(self.account_item(id=True))[0].account()
        self.assertIsNone(state.account_id)
        self.assertEqual(port(self.account_item(id=7))[0].account().account_id, 7)

    def test_invalid_session_blocks_every_other_call_until_the_account_reads_ok(self):
        adapter, transport = port(self.account_item(sources=[{"id": "s", "status": "CREDENTIALS"}]))
        self.assertIs(adapter.account().valid, False)
        for call in (lambda: adapter.lookup(ALICE), lambda: adapter.sent_invitations(), lambda: adapter.chats(),
                     lambda: adapter.messages("chat-synthetic-1"), lambda: adapter.send_invitation(ALICE_ID, "n"),
                     lambda: adapter.send_message("chat-synthetic-1", "t"), lambda: adapter.start_chat(ALICE_ID, "t"),
                     lambda: adapter.relations()):
            with self.assertRaisesRegex(SessionInvalid, "ACCOUNT_SESSION_INVALID"):
                call()
        self.assertEqual(len(transport.calls), 1)                      # nothing reached the network
        self.assertIs(adapter.account().valid, False)                 # the account read itself still runs
        transport.responses.update(RESPONSES_OK := {"GET /accounts": RESPONSES["GET /accounts"]})
        self.assertIs(adapter.account().valid, True)
        self.assertEqual(adapter.lookup(ALICE).provider_id, ALICE_ID)
        self.assertIsNotNone(RESPONSES_OK)

    def test_account_http_errors(self):
        for status, error in ((401, SessionInvalid), (403, SessionInvalid), (404, UnipileHTTPError),
                              (429, UnipileHTTPError), (500, UnipileHTTPError)):
            with self.assertRaises(error):
                port({"GET /accounts": [status, {"type": "errors/x", "title": "t"}]})[0].account()


class Lookup(unittest.TestCase):
    def test_person_fields_and_requests(self):
        adapter, transport = port()
        person = adapter.lookup(ALICE)
        self.assertEqual((person.profile, person.provider_id, person.network_distance, person.pending_invitation),
                         (ALICE, ALICE_ID, 2, False))
        self.assertEqual(person.raw, RESPONSES["GET /users/alice"][1])
        self.assertEqual([call["path"] for call in transport.calls],
                         ["/accounts", "/users/alice", "/users/invite/sent", "/users/invite/sent"])
        self.assertEqual(transport.calls[1]["query"], {"account_id": "acct-synthetic-1"})
        self.assertEqual(transport.calls[2]["query"], {"account_id": "acct-synthetic-1", "limit": 50})
        self.assertEqual(transport.calls[3]["query"], {"account_id": "acct-synthetic-1", "limit": 50, "cursor": "inv-page-2"})

    def test_distance_mapping(self):
        adapter = port()[0]
        self.assertEqual(adapter.lookup("https://www.linkedin.com/in/bob").network_distance, 1)
        self.assertEqual(adapter.lookup("linkedin.com/in/dan").network_distance, 3)
        self.assertIsNone(adapter.lookup("https://www.linkedin.com/in/carol").network_distance)
        for odd in (None, "DISTANCE_2", 2, ["SECOND_DEGREE"]):
            answer = dict(RESPONSES["GET /users/alice"][1], network_distance=odd)
            self.assertIsNone(port({"GET /users/alice": [200, answer]})[0].lookup(ALICE).network_distance, odd)

    def test_pending_from_the_profile_or_the_sent_listing(self):
        adapter, transport = port()
        self.assertIs(adapter.lookup("https://www.linkedin.com/in/bob").pending_invitation, True)     # profile: SENT PENDING
        self.assertEqual([call["path"] for call in transport.calls], ["/accounts", "/users/bob"])     # no listing read needed
        self.assertIs(adapter.lookup("https://www.linkedin.com/in/Dan/").pending_invitation, True)    # listing: id only, no public id
        self.assertEqual(transport.calls[-1]["path"], "/users/invite/sent")
        self.assertIs(adapter.lookup(ALICE).pending_invitation, False)
        for invitation in ({"type": "RECEIVED", "status": "PENDING"}, {"type": "SENT", "status": "ACCEPTED"},
                           {"type": "SENT"}, "SENT", [], None, {"type": "sent", "status": "pending"}):
            answer = dict(RESPONSES["GET /users/alice"][1], invitation=invitation)
            self.assertIs(port({"GET /users/alice": [200, answer]})[0].lookup(ALICE).pending_invitation, False, invitation)
        answer = dict(RESPONSES["GET /users/alice"][1], invitation={"type": "SENT", "status": "PENDING"})
        self.assertIs(port({"GET /users/alice": [200, answer], "GET /users/invite/sent": [500, None]})[0]
                      .lookup(ALICE).pending_invitation, True)
        by_public_id = copy.deepcopy(RESPONSES["GET /users/invite/sent"])
        by_public_id[1]["items"][0].update(invited_user_id=None, invited_user_public_id="ALICE")
        adapter = port({"GET /users/invite/sent": by_public_id})[0]
        self.assertIs(adapter.lookup("linkedin.com/in/alice").pending_invitation, True)
        self.assertIs(adapter.lookup("https://www.linkedin.com/in/carol").pending_invitation, False)   # listed nowhere

    def test_member_id_url_matches_a_listed_invitee_id(self):
        member_url = "https://www.linkedin.com/in/" + ALICE_ID
        listing = copy.deepcopy(RESPONSES["GET /users/invite/sent"])
        listing[1]["items"][0].update(invited_user_id=ALICE_ID, invited_user_public_id=None)
        answer = dict(RESPONSES["GET /users/alice"][1], provider_id=None)
        adapter = port({"GET /users/invite/sent": listing, "GET /users/" + ALICE_ID: [200, answer]})[0]
        self.assertIs(adapter.lookup(member_url).pending_invitation, True)
        self.assertIsNone(adapter.lookup(member_url).provider_id)

    def test_listing_failure_leaves_pending_unknown(self):
        for failure in ([500, {"type": "errors/x"}], [429, None], TimeoutError("read timed out"),
                        [200, {"items": [{"id": "x"}]}], [200, "prose"]):
            adapter = port({"GET /users/invite/sent": failure})[0]
            self.assertIsNone(adapter.lookup(ALICE).pending_invitation, failure)
        with self.assertRaises(SessionInvalid):
            port({"GET /users/invite/sent": [401, None]})[0].lookup(ALICE)

    def test_unknown_or_unreadable_profiles_raise_not_found(self):
        adapter = port()[0]
        for value in ("https://www.linkedin.com/in/nobody", "not a url", None, 5,
                      "https://www.linkedin.com/company/x"):
            with self.assertRaises(NotFound):
                adapter.lookup(value)
        with self.assertRaises(SessionInvalid):
            port({"GET /users/alice": [403, None]})[0].lookup(ALICE)
        with self.assertRaises(UnipileHTTPError):
            port({"GET /users/alice": [500, None]})[0].lookup(ALICE)

    def test_slug_is_quoted_in_the_path(self):
        adapter, transport = port({"GET /users/jos%C3%A9%20x": [404, {}]})
        with self.assertRaises(NotFound):
            adapter.lookup("https://www.linkedin.com/in/josé x")
        self.assertEqual(transport.calls[-1]["path"], "/users/jos%C3%A9%20x")

    def test_invitation_absent_through_the_port(self):
        adapter = port()[0]
        self.assertTrue(linkedin_port.invitation_absent(adapter, None, ALICE))              # bob and dan listed
        self.assertFalse(linkedin_port.invitation_absent(adapter, BOB_ID, "https://www.linkedin.com/in/bob"))   # connected, profile pending
        connected_only = dict(RESPONSES["GET /users/bob"][1], invitation=None)
        self.assertFalse(linkedin_port.invitation_absent(port({"GET /users/bob": [200, connected_only]})[0], None,
                                                         "https://www.linkedin.com/in/bob"))
        self.assertFalse(linkedin_port.invitation_absent(adapter, None, "https://www.linkedin.com/in/dan"))
        with self.assertRaisesRegex(IncompletePage, "PROFILE_INVITATION_STATE_UNKNOWN"):     # OUT_OF_NETWORK
            linkedin_port.invitation_absent(adapter, None, "https://www.linkedin.com/in/carol")
        incomplete = port({"GET /users/invite/sent?cursor=inv-page-2": [200, {"items": []}]})[0]
        with self.assertRaises(IncompletePage):                                              # pending None
            linkedin_port.invitation_absent(incomplete, None, ALICE)


class SentInvitations(unittest.TestCase):
    def test_fields_and_cursor_paging(self):
        adapter, transport = port()
        page = adapter.sent_invitations()
        self.assertEqual((page.next_cursor, page.total, page.complete), ("inv-page-2", None, True))
        self.assertEqual(page.raw, RESPONSES["GET /users/invite/sent"][1])
        first = page.items[0]
        self.assertEqual((first.invitation_id, first.invitee_provider_id, first.invitee_profile, first.note, first.sent_at),
                         ("inv-synthetic-1", BOB_ID, "https://www.linkedin.com/in/bob", "Hello Bob, synthetic note.",
                          "2026-10-15T10:00:00+00:00"))
        self.assertEqual(first.raw, RESPONSES["GET /users/invite/sent"][1]["items"][0])
        listed = linkedin_port.all_items(adapter.sent_invitations)
        self.assertEqual([item.invitation_id for item in listed], ["inv-synthetic-1", "inv-synthetic-2"])
        second = listed[1]
        self.assertEqual((second.invitee_provider_id, second.invitee_profile, second.note, second.sent_at),
                         (DAN_ID, None, None, "2026-10-13T06:30:00+00:00"))        # +02:00 re-expressed in UTC
        self.assertEqual([call["query"].get("cursor") for call in transport.requests("GET")[1:]], [None, None, "inv-page-2"])

    def test_listing_shape_is_complete_or_raises(self):
        for answer, code in (({"items": [{"id": "x"}]}, "LISTING_SHAPE_INVALID"),      # no cursor key: never "last page"
                             ({"items": "x", "cursor": None}, "LISTING_SHAPE_INVALID"),
                             ("prose", "LISTING_SHAPE_INVALID"), (None, "LISTING_SHAPE_INVALID"),
                             ({"items": [{"id": "x"}, "y"], "cursor": None}, "LISTING_ITEM_INVALID"),
                             ({"items": [], "cursor": ""}, "LISTING_CURSOR_INVALID"),
                             ({"items": [], "cursor": 5}, "LISTING_CURSOR_INVALID")):
            with self.assertRaisesRegex(IncompletePage, code):
                port({"GET /users/invite/sent": [200, answer]})[0].sent_invitations()
        empty = port({"GET /users/invite/sent": [200, {"object": "InvitationList", "items": [], "cursor": None}]})[0]
        page = empty.sent_invitations()
        self.assertEqual((page.items, page.next_cursor, page.complete), ((), None, True))

    def test_item_shaping_is_total(self):
        odd = {"id": 5, "invited_user_id": 7, "invited_user_public_id": " ", "invitation_text": ["x"], "parsed_datetime": 3}
        page = port({"GET /users/invite/sent": [200, {"items": [odd], "cursor": None}]})[0].sent_invitations()
        item = page.items[0]
        self.assertEqual((item.invitation_id, item.invitee_provider_id, item.invitee_profile, item.note, item.sent_at),
                         (None, None, None, None, None))
        self.assertEqual(item.raw, odd)

    def test_relations_are_connections(self):
        adapter = port()[0]
        page = adapter.relations()
        self.assertEqual(page.complete, True)
        person = page.items[0]
        self.assertEqual((person.profile, person.provider_id, person.network_distance, person.pending_invitation),
                         ("https://www.linkedin.com/in/bob", BOB_ID, 1, False))
        odd = {"member_id": 5, "public_identifier": "carol"}
        page = port({"GET /users/relations": [200, {"items": [odd], "cursor": None}]})[0].relations()
        self.assertEqual((page.items[0].profile, page.items[0].provider_id), ("https://www.linkedin.com/in/carol", None))

    def test_read_errors(self):
        with self.assertRaises(SessionInvalid):
            port({"GET /users/invite/sent": [403, None]})[0].sent_invitations()
        with self.assertRaises(UnipileHTTPError) as caught:
            port({"GET /users/invite/sent": [429, {"type": "errors/rate_limit", "title": "Too many", "token": "secret",
                                                   "detail": "d" * 500}]})[0].sent_invitations()
        self.assertEqual(caught.exception.status, 429)
        self.assertTrue(caught.exception.detail.startswith("errors/rate_limit | Too many | ddd"))
        self.assertEqual(len(caught.exception.detail), unipile_port.DETAIL_LIMIT)
        self.assertNotIn("secret", str(caught.exception))
        with self.assertRaises(UnipileHTTPError):
            port({"GET /users/invite/sent": [404, None]})[0].sent_invitations()     # a listing has no NotFound
        with self.assertRaises(TimeoutError):
            port({"GET /users/invite/sent": TimeoutError("read timed out")})[0].sent_invitations()
        with self.assertRaises(urllib.error.URLError):
            port({"GET /users/invite/sent": urllib.error.URLError("down")})[0].sent_invitations()


class Chats(unittest.TestCase):
    def test_order_is_kept_and_pages_follow_the_cursor(self):
        adapter, transport = port()
        page = adapter.chats()
        self.assertEqual([chat.chat_id for chat in page.items], ["chat-synthetic-2", "chat-synthetic-1"])
        self.assertEqual((page.items[0].attendee_provider_id, page.items[0].attendee_profile), (BOB_ID, None))
        self.assertEqual((page.next_cursor, page.total, page.complete), ("chats-page-2", None, True))
        self.assertEqual(page.raw, RESPONSES["GET /chats"][1])
        self.assertEqual([chat.chat_id for chat in linkedin_port.all_items(adapter.chats)],
                         ["chat-synthetic-2", "chat-synthetic-1", "chat-synthetic-0"])
        self.assertEqual(transport.calls[-1]["query"], {"account_id": "acct-synthetic-1", "limit": 50, "cursor": "chats-page-2"})

    def test_person_filter(self):
        adapter, transport = port()
        page = adapter.chats(person="linkedin.com/in/ALICE/")
        self.assertEqual(([chat.chat_id for chat in page.items], page.next_cursor, page.complete),
                         (["chat-synthetic-1"], "chats-page-2", True))
        self.assertEqual([call["path"] for call in transport.calls], ["/accounts", "/users/ALICE", "/chats"])
        page = adapter.chats(cursor="chats-page-2", person=ALICE)
        self.assertEqual((page.items, page.next_cursor), ((), None))     # no match on this page, listing ended
        self.assertEqual([chat.chat_id for chat in linkedin_port.all_items(lambda cursor: adapter.chats(cursor, person=ALICE))],
                         ["chat-synthetic-1"])
        self.assertEqual([chat.chat_id for chat in linkedin_port.all_items(
            lambda cursor: adapter.chats(cursor, person="https://www.linkedin.com/in/carol"))], [])
        with self.assertRaises(NotFound):
            adapter.chats(person="https://www.linkedin.com/in/nobody")
        with self.assertRaises(NotFound):
            adapter.chats(person="not a url")
        no_id = dict(RESPONSES["GET /users/alice"][1], provider_id=None)
        with self.assertRaisesRegex(NotFound, "PERSON_PROVIDER_ID_UNKNOWN"):
            port({"GET /users/alice": [200, no_id]})[0].chats(person=ALICE)

    def test_chat_without_id_is_incomplete(self):
        with self.assertRaisesRegex(IncompletePage, "CHAT_ID_MISSING"):
            port({"GET /chats": [200, {"items": [{"attendee_provider_id": "x"}], "cursor": None}]})[0].chats()
        with self.assertRaisesRegex(IncompletePage, "LISTING_SHAPE_INVALID"):
            port({"GET /chats": [200, {"items": []}]})[0].chats()
        odd = {"id": "c", "attendee_provider_id": 5}
        page = port({"GET /chats": [200, {"items": [odd], "cursor": None}]})[0].chats()
        self.assertEqual((page.items[0].chat_id, page.items[0].attendee_provider_id), ("c", None))


class Messages(unittest.TestCase):
    def test_thread_is_oldest_first_across_pages(self):
        adapter, transport = port()
        thread = adapter.messages("chat-synthetic-1")
        self.assertEqual([(m.is_sender, m.text, m.sent_at) for m in thread.messages], [
            (True, "Synthetic invitation note.", "2026-10-12T09:00:00+00:00"),
            (True, "Hi Alice, synthetic first message.", "2026-10-13T09:00:00+00:00"),
            (False, "Thanks, talk soon.", "2026-10-14T09:00:00+00:00")])
        self.assertEqual((thread.chat_id, thread.complete), ("chat-synthetic-1", True))
        self.assertEqual(thread.raw, [RESPONSES["GET /chats/chat-synthetic-1/messages"][1],
                                      RESPONSES["GET /chats/chat-synthetic-1/messages?cursor=msgs-page-2"][1]])
        self.assertEqual([(call["path"], call["query"]) for call in transport.calls], [
            ("/chats/chat-synthetic-1/messages", {"limit": 50}),
            ("/chats/chat-synthetic-1/messages", {"limit": 50, "cursor": "msgs-page-2"})])
        self.assertEqual([type(m.is_sender) for m in thread.messages], [bool, bool, bool])
        self.assertEqual(thread.messages[0].raw, RESPONSES["GET /chats/chat-synthetic-1/messages?cursor=msgs-page-2"][1]["items"][0])

    def test_equal_timestamps_keep_the_returned_order(self):
        items = [{"id": "a", "is_sender": 1, "timestamp": "2026-10-12T09:00:00.000Z", "text": "a"},
                 {"id": "b", "is_sender": 0, "timestamp": "2026-10-12T09:00:00.000Z", "text": "b"}]
        thread = port({"GET /chats/c/messages": [200, {"items": items, "cursor": None}]})[0].messages("c")
        self.assertEqual([m.text for m in thread.messages], ["a", "b"])

    def test_sender_and_text_shaping(self):
        items = [{"is_sender": True, "timestamp": "2026-10-12T09:00:00Z", "text": None},
                 {"is_sender": 2, "timestamp": "2026-10-12T09:00:01Z", "text": ["x"]},
                 {"is_sender": "1", "timestamp": "2026-10-12T09:00:02Z", "text": "t"}]
        thread = port({"GET /chats/c/messages": [200, {"items": items, "cursor": None}]})[0].messages("c")
        self.assertEqual([(m.is_sender, m.text) for m in thread.messages], [(True, None), (None, None), (None, "t")])

    def test_unreadable_thread_raises(self):
        with self.assertRaises(NotFound):
            port()[0].messages("no-such-chat")
        with self.assertRaisesRegex(IncompletePage, "MESSAGE_TIMESTAMP_UNREADABLE"):
            port({"GET /chats/c/messages": [200, {"items": [{"is_sender": 1, "text": "x", "timestamp": None}],
                                                  "cursor": None}]})[0].messages("c")
        for answer, code in (({"items": []}, "LISTING_SHAPE_INVALID"), ({"items": ["x"], "cursor": None}, "LISTING_ITEM_INVALID"),
                             ({"items": [], "cursor": ""}, "LISTING_CURSOR_INVALID"), ("prose", "LISTING_SHAPE_INVALID")):
            with self.assertRaisesRegex(IncompletePage, code):
                port({"GET /chats/c/messages": [200, answer]})[0].messages("c")
        endless = {"GET /chats/c/messages": [200, {"items": [], "cursor": "more"}],
                   "GET /chats/c/messages?cursor=more": [200, {"items": [], "cursor": "more"}]}
        with self.assertRaisesRegex(IncompletePage, "PAGE_LIMIT"):
            port(endless, limit_pages=3)[0].messages("c")
        with self.assertRaises(SessionInvalid):
            port({"GET /chats/c/messages": [401, None]})[0].messages("c")
        with self.assertRaises(UnipileHTTPError):
            port({"GET /chats/c/messages": [503, "<html>"]})[0].messages("c")
        with self.assertRaises(TimeoutError):
            port({"GET /chats/c/messages": TimeoutError()})[0].messages("c")

    def test_chat_id_is_quoted(self):
        adapter, transport = port({"GET /chats/a%2Fb/messages": [200, {"items": [], "cursor": None}]})
        self.assertEqual(adapter.messages("a/b").messages, ())
        self.assertEqual(transport.calls[-1]["path"], "/chats/a%2Fb/messages")


class Writes(unittest.TestCase):
    def test_send_invitation_is_one_request_with_an_ack(self):
        adapter, transport = port()
        ack = adapter.send_invitation(ALICE_ID, "Hello Alice")
        self.assertEqual((ack.operation, ack.status), ("send_invitation", "ACKNOWLEDGED_UNCONFIRMED"))
        self.assertEqual(ack.request, {"provider_id": ALICE_ID, "account_id": "acct-synthetic-1", "message": "Hello Alice"})
        self.assertEqual(ack.raw, {"object": "UserInvitationSent", "invitation_id": "inv-synthetic-3"})
        posts = transport.requests("POST")
        self.assertEqual(len(posts), 1)
        self.assertEqual((posts[0]["path"], posts[0]["body"], posts[0]["multipart"]), ("/users/invite", ack.request, None))

    def test_send_message_and_start_chat(self):
        adapter, transport = port()
        ack = adapter.send_message("chat-synthetic-1", "Thanks")
        self.assertEqual((ack.operation, ack.request, ack.raw["object"]),
                         ("send_message", {"chat_id": "chat-synthetic-1", "text": "Thanks"}, "MessageSent"))
        self.assertEqual((transport.calls[-1]["path"], transport.calls[-1]["body"]),
                         ("/chats/chat-synthetic-1/messages", {"text": "Thanks"}))
        ack = adapter.start_chat(ALICE_ID, "Hello again")
        self.assertEqual((ack.operation, ack.raw["chat_id"]), ("start_chat", "chat-synthetic-1"))   # existing thread reused
        self.assertEqual(ack.request, {"account_id": "acct-synthetic-1", "text": "Hello again", "attendees_ids": ALICE_ID})
        self.assertEqual((transport.calls[-1]["path"], transport.calls[-1]["multipart"], transport.calls[-1]["body"]),
                         ("/chats", ack.request, None))
        self.assertEqual(len(transport.requests("POST")), 2)

    def test_only_an_explicit_invitation_exists_refusal_is_not_uncertain(self):
        # GL-I1: Unipile's explicit refusals of an invitation say nothing was sent.
        for kind, reason in (("errors/already_invited_recently", "ALREADY_INVITED"),
                             ("errors/already_connected", "ALREADY_CONNECTED")):
            adapter, transport = port({"POST /users/invite": [422, {"type": kind, "title": "x"}],
                                       "POST /chats": [422, {"type": kind, "title": "x"}]})
            with self.assertRaises(linkedin_port.InvitationExists) as caught:
                adapter.send_invitation(ALICE_ID, "n")
            self.assertEqual((caught.exception.reason, caught.exception.status), (reason, 422))
            with self.assertRaises(UncertainWrite):   # only an invitation's refusal
                adapter.start_chat(ALICE_ID, "t")
        for answer in ([500, {"type": "errors/already_invited_recently"}], [422, {"type": "errors/cannot_resend_yet"}],
                       [422, {"type": "errors/already_invited_recently_x"}], TimeoutError("timed out")):
            with self.assertRaises(UncertainWrite) as caught:
                port({"POST /users/invite": answer})[0].send_invitation(ALICE_ID, "n")
            self.assertNotIsInstance(caught.exception, linkedin_port.InvitationExists)

    def test_any_failure_of_a_write_is_uncertain(self):
        cases = {"HTTP_422": [422, {"type": "errors/invalid_request", "title": "x"}], "HTTP_401": [401, None],
                 "HTTP_403": [403, None], "HTTP_429": [429, None], "HTTP_500": [500, "oops"], "HTTP_200": [200, "prose"],
                 "TimeoutError": TimeoutError("timed out"), "URLError": urllib.error.URLError("reset"),
                 "ConnectionResetError": ConnectionResetError()}
        for expected, answer in cases.items():
            adapter, transport = port({"POST /users/invite": answer, "POST /chats/chat-synthetic-1/messages": answer,
                                       "POST /chats": answer})
            for call in (lambda: adapter.send_invitation(ALICE_ID, "n"), lambda: adapter.send_message("chat-synthetic-1", "t"),
                         lambda: adapter.start_chat(ALICE_ID, "t")):
                with self.assertRaises(UncertainWrite) as caught:
                    call()
                self.assertTrue(str(caught.exception).startswith(expected), (expected, str(caught.exception)))
                self.assertEqual(caught.exception.status, answer[0] if isinstance(answer, list) else None)
            self.assertEqual(len(transport.requests("POST")), 3)      # one request per write, nothing repeated
        with self.assertRaises(UncertainWrite) as caught:
            port({"POST /users/invite": [422, {"type": "errors/invalid_request", "title": "Bad", "detail": "d"}]})[0]\
                .send_invitation(ALICE_ID, "n")
        self.assertEqual(str(caught.exception), "HTTP_422: errors/invalid_request | Bad | d")

    def test_account_resolution_failure_before_a_write_is_not_uncertain(self):
        for status, error in ((500, UnipileHTTPError), (401, SessionInvalid)):
            adapter, transport = port({"GET /accounts": [status, None]})
            with self.assertRaises(error):
                adapter.send_invitation(ALICE_ID, "n")
            with self.assertRaises(error):
                adapter.start_chat(ALICE_ID, "n")
            self.assertEqual(transport.requests("POST"), [])
        adapter, transport = port(account_id="acct-synthetic-1")
        adapter.send_invitation(ALICE_ID, "n")
        self.assertEqual([call["path"] for call in transport.calls], ["/users/invite"])    # configured id: no account read


class Helpers(unittest.TestCase):
    def test_iso_utc(self):
        self.assertEqual(unipile_port._iso_utc("2026-10-15T10:00:00.000Z"), "2026-10-15T10:00:00+00:00")
        self.assertEqual(unipile_port._iso_utc("2026-10-15T12:00:00+02:00"), "2026-10-15T10:00:00+00:00")
        self.assertEqual(unipile_port._iso_utc("2026-10-15T10:00:00.250Z"), "2026-10-15T10:00:00.250000+00:00")
        self.assertEqual(unipile_port._iso_utc("2026-10-15T10:00:00"), "2026-10-15T10:00:00+00:00")   # naive: UTC
        for value in (None, "", " ", 5, "yesterday", "2026-13-01T00:00:00Z"):
            self.assertIsNone(unipile_port._iso_utc(value), value)

    def test_detail_is_bounded_and_key_free(self):
        self.assertEqual(unipile_port._detail({"type": "a", "detail": "c", "title": "b", "token": "k"}), "a | b | c")
        self.assertEqual(unipile_port._detail("x" * 400), "x" * unipile_port.DETAIL_LIMIT)
        self.assertIsNone(unipile_port._detail({"token": "k"}))
        self.assertIsNone(unipile_port._detail(None))
        self.assertIsNone(unipile_port._detail(["a"]))

    def test_multipart_encoding(self):
        body, content_type = unipile_port.encode_multipart({"account_id": "a", "text": "hi there", "attendees_ids": "p"},
                                                           boundary="B0UNDARY")
        self.assertEqual(content_type, "multipart/form-data; boundary=B0UNDARY")
        self.assertEqual(body, (b'--B0UNDARY\r\nContent-Disposition: form-data; name="account_id"\r\n\r\na\r\n'
                                b'--B0UNDARY\r\nContent-Disposition: form-data; name="text"\r\n\r\nhi there\r\n'
                                b'--B0UNDARY\r\nContent-Disposition: form-data; name="attendees_ids"\r\n\r\np\r\n'
                                b'--B0UNDARY--\r\n'))
        self.assertIn(b"form-data", unipile_port.encode_multipart({"a": "b"})[0])

    def test_settings_reader_and_from_settings(self):
        with tempfile.TemporaryDirectory() as home:
            Path(home, "unipile.json").write_text(json.dumps({"dsn": "https://synthetic.example.test:13443/", "token": " t0ken "}))
            with unittest.mock.patch.dict(os.environ, {"SIDEDOOR_HOME": home}, clear=False):
                values = unipile_port.settings()
                self.assertEqual(values, {"base": "https://synthetic.example.test:13443/api/v1", "token": "t0ken"})
                self.assertEqual(unipile_port.config_path(), Path(home) / "unipile.json")
                adapter = UnipilePort.from_settings(account_id="acct", timeout=5, page_size=3)
                self.assertIsInstance(adapter.transport, unipile_port.HttpTransport)
                self.assertEqual((adapter.account_id, adapter.page_size, adapter.transport.timeout), ("acct", 3, 5))
                self.assertEqual(repr(adapter.transport), "HttpTransport(<redacted>)")
                self.assertNotIn("t0ken", repr(vars(adapter)))
            Path(home, "unipile.json").write_text(json.dumps({"dsn": "x"}))
            with self.assertRaises(KeyError):
                unipile_port.settings(Path(home, "unipile.json"))
        with self.assertRaises(FileNotFoundError):
            unipile_port.settings(Path(home, "unipile.json"))


class FakeResponse(io.BytesIO):
    def __init__(self, status, payload):
        super().__init__(payload)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()


class HttpTransportTest(unittest.TestCase):
    """urllib is patched: the request built and the (status, parsed) mapping, no network."""

    def test_get_builds_the_request_and_parses_json(self):
        transport = unipile_port.HttpTransport("https://synthetic.example.test/api/v1", "t0ken", timeout=7)
        with unittest.mock.patch("urllib.request.urlopen", return_value=FakeResponse(200, b'{"items": []}')) as opened:
            self.assertEqual(transport("GET", "/chats", query={"account_id": "a", "limit": 2, "cursor": "c d"}),
                             (200, {"items": []}))
        request, named = opened.call_args.args[0], opened.call_args.kwargs
        self.assertEqual(request.full_url, "https://synthetic.example.test/api/v1/chats?account_id=a&limit=2&cursor=c+d")
        self.assertEqual((request.get_method(), request.data, named), ("GET", None, {"timeout": 7}))
        self.assertEqual(request.get_header("X-api-key"), "t0ken")
        self.assertEqual(request.get_header("Accept"), "application/json")
        self.assertIsNone(request.get_header("Content-type"))

    def test_post_json_and_multipart(self):
        transport = unipile_port.HttpTransport("https://synthetic.example.test/api/v1", "t0ken")
        with unittest.mock.patch("urllib.request.urlopen", return_value=FakeResponse(201, b'{"object": "MessageSent"}')) as opened:
            self.assertEqual(transport("POST", "/chats/c/messages", body={"text": "hi"}, timeout=3), (201, {"object": "MessageSent"}))
        request = opened.call_args.args[0]
        self.assertEqual((request.data, request.get_header("Content-type"), opened.call_args.kwargs),
                         (b'{"text": "hi"}', "application/json", {"timeout": 3}))
        with unittest.mock.patch("urllib.request.urlopen", return_value=FakeResponse(201, b'{"object": "ChatStarted"}')) as opened:
            transport("POST", "/chats", multipart={"account_id": "a", "text": "t", "attendees_ids": "p"})
        request = opened.call_args.args[0]
        self.assertTrue(request.get_header("Content-type").startswith("multipart/form-data; boundary="))
        self.assertIn(b'name="attendees_ids"\r\n\r\np\r\n', request.data)

    def test_http_errors_and_unparsed_bodies_are_returned_not_raised(self):
        transport = unipile_port.HttpTransport("https://synthetic.example.test/api/v1", "t0ken")
        error = urllib.error.HTTPError("https://synthetic.example.test/api/v1/users/x", 404, "Not Found", {},
                                       io.BytesIO(b'{"type": "errors/resource_not_found"}'))
        with unittest.mock.patch("urllib.request.urlopen", side_effect=error):
            self.assertEqual(transport("GET", "/users/x"), (404, {"type": "errors/resource_not_found"}))
        with unittest.mock.patch("urllib.request.urlopen", return_value=FakeResponse(502, b"<html>" + b"x" * 3000)):
            status, text = transport("GET", "/chats")
            self.assertEqual((status, len(text)), (502, 2000))
        with unittest.mock.patch("urllib.request.urlopen", return_value=FakeResponse(204, b"")):
            self.assertEqual(transport("GET", "/chats"), (204, None))
        with unittest.mock.patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaises(TimeoutError):
                transport("GET", "/chats")


@unittest.skipUnless((Path(__file__).resolve().parents[1] / "spikes/unipile/unipile.py").is_file(),
                     "spikes/ is not part of this source tree")
class SpikeStillWorks(unittest.TestCase):
    def test_spike_call_delegates_to_the_shared_transport(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("unipile_spike", Path(__file__).resolve().parents[1] / "spikes/unipile/unipile.py")
        spike = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(spike)
        with unittest.mock.patch.object(unipile_port, "settings", return_value={"base": "https://s.example.test/api/v1", "token": "t"}):
            with unittest.mock.patch.object(unipile_port, "HttpTransport") as transport_class:
                answer = (200, {"items": [{"type": "LINKEDIN", "id": "a"}, {"type": "MAIL"}]})
                transport_class.return_value.return_value = answer
                self.assertEqual(spike.call("GET", "/accounts", query={"x": 1}), answer)
                self.assertEqual(spike.linkedin_account(), (200, [{"type": "LINKEDIN", "id": "a"}]))
                transport_class.assert_called_with("https://s.example.test/api/v1", "t", 60)
                transport_class.return_value.assert_any_call("GET", "/accounts", query={"x": 1}, body=None, multipart=None, timeout=60)
            self.assertEqual(spike.config_path(), unipile_port.config_path())


if __name__ == "__main__":
    unittest.main()
