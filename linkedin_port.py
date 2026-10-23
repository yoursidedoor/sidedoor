"""LinkedInPort: the thin outreach provider interface (E3.T1, owner decision 2026-10-15, option A).

The port mirrors Unipile's primitives (proven in E0.T1: profile lookup, invite with note,
sent-invitation list, chat list, chat messages, send in chat; start-chat proven 2026-10-15 for an
existing thread, where it returns that thread's chat id; a new thread not yet observed). It
carries no sequencing, pacing, claims or gates: the per-lead sequence (E3.T3) and pacing (E3.T4)
live in our code next to `outreach.claim_send`, so every guardrail has one home whatever the
provider.

Adapter duties (what every adapter does; adapters carry no gates):

- Reads never write. A listing either returns a complete page or raises `IncompletePage`; a page
  that does not add up is never treated as "absent". A session the provider reports invalid makes
  `account()` return `valid=False` and every other call raise `SessionInvalid`.
- A write (`send_invitation`, `send_message`, `start_chat`) makes exactly one provider request
  and returns an `Ack`, which is an acknowledgment, never confirmed delivery. Any exception from
  a write, a timeout included, means the outcome is UNKNOWN: the write may have landed.
- An unknown person or chat raises (`NotFound` in adapters that classify it); a read never returns
  a Person, Thread or empty page made of Nones for something it could not find. A member-id
  profile URL (linkedin.com/in/ACoAA...) is a different spelling from the person's vanity URL; the
  port does not equate the two unless the provider itself resolves it. Every other spelling of
  one /in/ profile (www or not, trailing slash, letter case, bare "linkedin.com/in/<slug>") is
  the same person.
- An `Invitation` names the invitation's own id (`invitation_id`) separately from the invitee
  (`invitee_provider_id`, `invitee_profile`); an adapter fills the invitee fields only from what
  the provider says about the invitee, None otherwise. Which Unipile invite/sent fields carry
  the invitee (invited user id, public id) is to be confirmed in E3.T2: E0.T1 recorded only the
  invitation's id, date and text.

Caller duties (outreach code above the port, E3.T3/E3.T4; the port does not enforce them):

- Claim-before-send is unchanged: `outreach_actions` REQUEST_STARTED, or the attempt/launch
  claim, is committed before any write, and one request consumes one claim.
- An uncertain invitation (owner, 2026-10-15) may be retried, at most `INVITATION_MAX_ATTEMPTS`
  (3) attempts in all, and each retry only after a read shows the earlier attempt absent:
  `invitation_absent` (the complete `sent_invitations()` listing and the profile's own
  `pending_invitation` field both show no invitation to that person) and `may_retry_invitation`.
  A read that cannot decide (incomplete page, invalid session, unknown person, an invitation
  whose invitee cannot be identified, error) is never absence.
- A person already connected (`network_distance == 1`) is never invited: `invitation_absent`
  returns False for them, so a caller that retries only on True never invites them.
- Messages keep "reconcile, never resend", except replies, which keep their 2026-09-30 rule
  (at most 3 attempts, each after a thread read shows ours absent; `reply_execution`).
- Inactivity closure rules are unchanged by this port (`inactivity_closure`).

- Every returned value keeps the provider's raw answer in `raw`, so callers that still reason
  over provider-shaped data (the HeyReach decoders and the AI send judge's envelope) see exactly
  what they saw before.

Adapters: `FakePort` (tests, injectable failures) and `HeyReachPort` (wraps today's HeyReach
client calls exactly; invitations stay on the campaign path behind `provider == "heyreach"`
until E3.T7). The Unipile adapter is E3.T2.
"""

import dataclasses
import datetime as dt
import itertools
import re
import unicodedata
import urllib.parse
from typing import Any, Optional, Protocol, runtime_checkable


INVITATION_MAX_ATTEMPTS = 3


class PortError(Exception):
    """Base for errors the port itself raises. `code` is a stable machine-readable string."""
    code = "PORT_ERROR"

    def __init__(self, message=None):
        super().__init__(message or self.code)


class SessionInvalid(PortError):
    code = "SESSION_INVALID"


class IncompletePage(PortError):
    code = "INCOMPLETE_PAGE"


class UncertainWrite(PortError):
    """A write whose outcome is unknown (e.g. a timeout after the request was sent)."""
    code = "UNCERTAIN_WRITE"


class InvitationExists(PortError, ValueError):
    """The provider refused an invitation and said explicitly why: one to this person already
    exists (`reason` ALREADY_INVITED) or the two are already connected (ALREADY_CONNECTED). Nothing
    was sent. Only such an explicit answer raises it; every other refusal or failure of a write is
    UncertainWrite (GL-I1, owner 2026-10-22)."""
    code = "INVITATION_EXISTS"

    def __init__(self, reason, message=None):
        super().__init__(message or reason)
        self.reason = reason


class Unsupported(PortError):
    code = "UNSUPPORTED"


class NotFound(PortError):
    """The person, profile or chat asked for is not known to the provider."""
    code = "NOT_FOUND"


@dataclasses.dataclass(frozen=True)
class AccountState:
    valid: Optional[bool]            # None: the provider read does not say
    account_id: Any
    own_profile: Optional[str]
    raw: Any = None


@dataclasses.dataclass(frozen=True)
class Person:
    profile: str
    provider_id: Optional[str]
    network_distance: Optional[int]  # 1 = connected; None = unknown. Unipile reports degrees as
                                     # names (FIRST_DEGREE ... OUT_OF_NETWORK); their int mapping,
                                     # OUT_OF_NETWORK's above all, is to be confirmed in E3.T2.
    pending_invitation: Optional[bool]  # None = the read does not say
    raw: Any = None


@dataclasses.dataclass(frozen=True)
class Invitation:
    invitation_id: Optional[str]          # the invitation's own id (Unipile: GET /users/invite/sent)
    invitee_provider_id: Optional[str]    # the invited person's provider id, when the provider gives it
    invitee_profile: Optional[str]        # the invited person's profile URL, when the provider gives it
    note: Optional[str]
    sent_at: Optional[str]
    raw: Any = None


@dataclasses.dataclass(frozen=True)
class Chat:
    chat_id: str
    attendee_provider_id: Optional[str]
    attendee_profile: Optional[str]
    raw: Any = None


@dataclasses.dataclass(frozen=True)
class Message:
    is_sender: Optional[bool]        # True: sent by our account
    text: Optional[str]
    sent_at: Optional[str]           # ISO 8601 UTC string (see LinkedInPort.messages)
    raw: Any = None


@dataclasses.dataclass(frozen=True)
class Thread:
    chat_id: str
    messages: tuple                  # oldest first (see LinkedInPort.messages)
    complete: bool                   # the full history was returned
    raw: Any = None


@dataclasses.dataclass(frozen=True)
class Page:
    items: tuple
    next_cursor: Optional[str]       # None: no further page
    total: Optional[int]
    complete: bool                   # this page is consistent with its listing
    raw: Any = None


@dataclasses.dataclass(frozen=True)
class Ack:
    operation: str
    request: dict
    raw: Any = None
    status: str = "ACKNOWLEDGED_UNCONFIRMED"


@runtime_checkable
class LinkedInPort(Protocol):
    provider: str

    def account(self) -> AccountState: ...

    def lookup(self, profile_url) -> Person:
        """The person behind a profile URL (spellings as in chats). An unknown profile raises
        NotFound."""
        ...

    def send_invitation(self, provider_id, note) -> Ack:
        """Invite one person with a note. LinkedIn keeps at most one pending invitation per
        person; what a second one does is the provider's (FakePort refuses it, see there)."""
        ...

    def sent_invitations(self, cursor=None) -> Page: ...

    def relations(self, cursor=None) -> Page: ...

    def chats(self, cursor=None, person=None) -> Page:
        """Chats ordered by latest message, newest first (the order expected from Unipile; to be
        confirmed in E3.T2, E0.T1 did not check it). `person`, when given,
        is a LinkedIn profile URL (any spelling of the same /in/ profile: with or without www, a
        trailing slash, letter case, or the bare "linkedin.com/in/<slug>" key; a member-id
        ACoAA... URL is another spelling, equated only when the provider resolves it); only that
        person's chats are listed. A person the port cannot resolve raises NotFound, never a
        complete empty page. An empty page means no chat with that person only because the page
        is complete for exactly that filter."""
        ...

    def messages(self, chat_id) -> Thread:
        """The chat's full history, oldest first, each Message.sent_at an ISO 8601 UTC string. An
        unknown chat raises NotFound."""
        ...

    def send_message(self, chat_id, text) -> Ack: ...

    def start_chat(self, provider_id, text) -> Ack:
        """Send `text` to a person by provider id. With an existing 1:1 chat with that person the
        message goes into that chat and the Ack's raw names its chat id (Unipile, E0.T1.S2c,
        2026-10-15); with none, a new chat is created (not yet observed on Unipile)."""
        ...


PORT_METHODS = ("account", "lookup", "send_invitation", "sent_invitations", "relations", "chats",
                "messages", "send_message", "start_chat")


def all_items(listing, limit_pages=50):
    """Every item of a cursor listing (`listing(cursor) -> Page`). Raises IncompletePage when a
    page is inconsistent or the listing does not end within `limit_pages`."""
    items, cursor = [], None
    for unused in range(limit_pages):
        page = listing(cursor)
        if not page.complete:
            raise IncompletePage()
        items.extend(page.items)
        if page.next_cursor is None:
            return items
        cursor = page.next_cursor
    raise IncompletePage("PAGE_LIMIT")


def _provider_id(value):
    """A usable provider id: a non-empty string (stripped). Anything else is "no id"."""
    return value.strip() if isinstance(value, str) and value.strip() else None


MEMBER_ID = re.compile(r"ACoAA[A-Za-z0-9_-]{34}")


def _member_id(value):
    """True only for the shape of a LinkedIn member id as it appears in member-id profile URLs
    (linkedin.com/in/ACoAA...): exactly "ACoAA" (letter case kept) and 34 more URL-safe base64
    characters, 39 in all. A live Unipile provider_id observed 2026-10-15 has this shape (39
    characters, "ACoAA" + 34 of [A-Za-z0-9_]); no real id is recorded in this repository.
    Sales Navigator's "ACwAA..." form is not a member id here: it is member-like (E3.T2: check
    whether Unipile ever reports it)."""
    return isinstance(value, str) and MEMBER_ID.fullmatch(value) is not None


def _member_like(value):
    """A slug that may be a member id whose letter case was lost or changed (e.g. a casefolded
    profile key), a Sales Navigator id ("ACwAA..."), or a vanity that merely starts with the same
    letters ("acoakley-jones"). Such a slug never shows someone else and is never equated with an
    id except exactly."""
    return (isinstance(value, str) and value.casefold().startswith(("acoa", "acwa"))
            and not _member_id(value))


def _profile(value):
    """(key, slug) of an /in/ profile URL, or (None, None) when it is not one. The key is
    normalised as _profile_key (letter case folded); the slug keeps its letter case (NFKC),
    because a member id is case-sensitive."""
    try:
        key = _profile_key(value)
    except (TypeError, ValueError):
        return None, None
    path = urllib.parse.urlsplit(value if "://" in value else "https://" + value).path
    slug = urllib.parse.unquote(path).strip("/").split("/")[1]
    return key, unicodedata.normalize("NFKC", slug)


def _same_invitee(entry, ids, vanity, loose):
    """Compare one sent-invitation entry with this person. Returns True (it is this person),
    False (it is shown to be someone else) or None (cannot be told either way).

    This person is known by `ids` (usable provider ids: the caller's, lookup()'s, and the slug of
    the caller's profile URL when it has the member-id shape), `vanity` (the normalised key of
    the caller's profile URL when its slug is neither member-id shaped nor member-like, else
    None) and `loose` (casefolded forms of every id and member-shaped or member-like caller slug,
    used only to recognise a match that differs in letter case alone).

    This person: the entry's usable invitee id is in `ids`; its invitee profile has a member-id
    slug equal to one of `ids` exactly; or its profile key equals `vanity`.
    Cannot tell: its id or slug equals a known id or slug only when letter case is ignored.
    Someone else, and only then: a usable invitee id while `ids` is known; a member-id-shaped slug
    while member-id-shaped ids are known, differing from all of them; or a plain vanity key (not
    member-like) different from `vanity` while `vanity` is known. Anything else cannot be told
    (no usable id and a profile that cannot be compared, a member-like slug, a member-id slug or
    id when this person has no comparable id or vanity)."""
    entry_id = _provider_id(entry.invitee_provider_id)
    key, slug = _profile(entry.invitee_profile)
    if entry_id is not None and entry_id in ids:
        return True
    if _member_id(slug) and slug in ids:
        return True
    if key is not None and vanity is not None and key == vanity:
        return True
    for value in (entry_id, slug):
        if value is not None and value.casefold() in loose:
            return None
    if entry_id is not None and ids:
        return False
    member_ids = {value for value in ids if _member_id(value)}
    if _member_id(slug) and member_ids:
        return False
    if key is not None and not _member_like(slug) and not _member_id(slug) and vanity is not None:
        return False
    return None


def invitation_absent(port, provider_id, profile_url):
    """True only when complete reads show no invitation to this person: lookup() of the profile
    says pending_invitation exactly False (a bool) and network_distance an int of 2 or more (1 is
    connected: False; 0, negatives and non-ints raise), and
    every entry of the whole sent_invitations() listing is shown to be someone else
    (`_same_invitee`; an entry that is this person: False). Raises, never True, when a read
    cannot decide: an unknown person, a pending or distance value of any other type
    (PROFILE_INVITATION_STATE_UNKNOWN), an incomplete listing, or an entry that cannot be told to
    be this person or someone else (IncompletePage "INVITATION_INVITEE_UNIDENTIFIED"). A member-id
    URL and a vanity URL are compared only through provider ids, never equated as strings.
    Callers treat a raise as "unknown", never as absent. Used before an uncertain invitation is
    retried."""
    person = port.lookup(profile_url)
    if (type(person.pending_invitation) is not bool or type(person.network_distance) is not int
            or person.network_distance < 1):
        raise IncompletePage("PROFILE_INVITATION_STATE_UNKNOWN")
    if person.pending_invitation or person.network_distance == 1:
        return False
    ids = {value for value in (_provider_id(provider_id), _provider_id(person.provider_id)) if value}
    key, slug = _profile(profile_url)
    if key is None:
        raise ValueError("A LinkedIn /in/ profile URL is required")
    vanity = None
    if _member_id(slug):
        ids.add(slug)
    elif not _member_like(slug):
        vanity = key
    loose = {value.casefold() for value in ids} | ({slug.casefold()} if _member_like(slug) else set())
    for invitation in all_items(port.sent_invitations):
        same = _same_invitee(invitation, ids, vanity, loose)
        if same is True:
            return False
        if same is None:
            raise IncompletePage("INVITATION_INVITEE_UNIDENTIFIED")
    return True


def may_retry_invitation(attempts_made, absent):
    """The owner's uncertain-invitation rule: another attempt only after a read showed the
    earlier one absent (`absent` is True, from `invitation_absent`) and fewer than
    INVITATION_MAX_ATTEMPTS attempts were made."""
    return absent is True and attempts_made < INVITATION_MAX_ATTEMPTS


def _profile_key(value):
    """One key per LinkedIn /in/ profile, as orchestrator.profile_key; a bare
    "linkedin.com/in/<slug>" key is accepted too."""
    from orchestrator import profile_key
    if not isinstance(value, str):
        raise TypeError("A profile URL string is required")
    return profile_key(value if "://" in value else "https://" + value)


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


class FakePort:
    """In-memory LinkedIn for tests: people, invitations and chats, with injectable failures.

    `fail(method, kind, times=1)` arms a failure for the next calls of `method`:
    - "timeout_after_write": the write lands, then UncertainWrite is raised;
    - "timeout": UncertainWrite (writes) / TimeoutError (reads) raised before anything happens;
    - "incomplete_page": a listing raises IncompletePage;
    - "partial_page": a listing returns an empty page marked complete=False (as HeyReachPort
      may), so callers must check `complete`;
    - "invalid_session": the session becomes invalid (account().valid False, other calls raise).

    One pending invitation per person, as LinkedIn. An invitation's invitee is the person whose
    provider id equals its invitee_provider_id or whose profile key equals its invitee_profile's
    (`_invites`), so an entry seeded by profile only counts too: lookup() reports it pending, a
    second `send_invitation` raises ValueError("INVITATION_ALREADY_PENDING") and creates nothing,
    and accept() removes it. `start_chat` reuses an existing 1:1 chat with the person, as Unipile
    does; with several, the fake uses the most recently active one (Unipile's choice there is
    unobserved).
    """

    provider = "fake"

    def __init__(self, own_profile="https://www.linkedin.com/in/me", page_size=2):
        self.own_profile = own_profile
        self.page_size = page_size
        self.session_valid = True
        self.people = {}             # profile key -> dict(profile, provider_id, distance)
        self.invitations = []        # dicts: invitation_id, invitee_provider_id, invitee_profile, note, sent_at
        self.chat_log = {}           # chat_id -> dict(attendee, activity, messages[list of dict])
        self.failures = {}           # method -> [kind, ...]
        self.calls = []
        self._ids = itertools.count(1)
        self._activity = itertools.count(1)   # latest-message order, independent of clock ties

    # -- test setup -------------------------------------------------------------------------
    def add_person(self, profile, provider_id=None, distance=2):
        provider_id = provider_id or "prov-" + str(next(self._ids))
        self.people[_profile_key(profile)] = {"profile": profile, "provider_id": provider_id,
                                              "distance": distance}
        return provider_id

    def add_chat(self, provider_id, messages=(), chat_id=None):
        chat_id = chat_id or "chat-" + str(next(self._ids))
        self.chat_log[chat_id] = {"attendee": provider_id, "activity": next(self._activity), "messages": [
            {"is_sender": bool(sender), "text": text, "sent_at": _now()} for sender, text in messages]}
        return chat_id

    def add_invitation(self, invitee_provider_id=None, invitee_profile=None, note=None):
        """Put an entry in the sent-invitation listing exactly as given (e.g. one whose invitee
        the provider does not identify)."""
        record = {"invitation_id": "inv-" + str(next(self._ids)), "invitee_provider_id": invitee_provider_id,
                  "invitee_profile": invitee_profile, "note": note, "sent_at": _now()}
        self.invitations.append(record)
        return record["invitation_id"]

    def _invites(self, item, provider_id):
        """True when the listed invitation `item` is to the person with this provider id."""
        if item["invitee_provider_id"] == provider_id:
            return True
        try:
            invitee = _profile_key(item["invitee_profile"])
        except (TypeError, ValueError):
            return False
        person = self.people.get(invitee)
        return person is not None and person["provider_id"] == provider_id

    def accept(self, provider_id):
        self.invitations = [item for item in self.invitations if not self._invites(item, provider_id)]
        for person in self.people.values():
            if person["provider_id"] == provider_id:
                person["distance"] = 1

    def fail(self, method, kind, times=1):
        self.failures.setdefault(method, []).extend([kind] * times)

    # -- helpers ----------------------------------------------------------------------------
    def _enter(self, method, write=False):
        self.calls.append(method)
        kinds = self.failures.get(method) or []
        kind = kinds.pop(0) if kinds else None
        if kind == "invalid_session":
            self.session_valid = False
        if not self.session_valid and method != "account":
            raise SessionInvalid()
        if kind == "timeout":
            raise UncertainWrite("TIMEOUT") if write else TimeoutError("read timed out")
        return kind

    def _page(self, records, cursor, kind):
        if kind == "incomplete_page":
            raise IncompletePage()
        if kind == "partial_page":
            return Page(items=(), next_cursor=None, total=len(records), complete=False, raw=None)
        start = int(cursor or 0)
        items = tuple(records[start:start + self.page_size])
        end = start + len(items)
        return Page(items=items, next_cursor=str(end) if end < len(records) else None,
                    total=len(records), complete=True, raw={"start": start})

    def _person(self, profile_url):
        try:
            person = self.people.get(_profile_key(profile_url))
        except (TypeError, ValueError):
            person = None
        if person is None:
            raise NotFound("PROFILE_NOT_FOUND")
        return person

    def _chat(self, chat_id):
        if chat_id not in self.chat_log:
            raise NotFound("CHAT_NOT_FOUND")
        return self.chat_log[chat_id]

    def _known(self, provider_id):
        if not any(person["provider_id"] == provider_id for person in self.people.values()):
            raise NotFound("PERSON_NOT_FOUND")

    def _profile_of(self, provider_id):
        return next((person["profile"] for person in self.people.values()
                     if person["provider_id"] == provider_id), None)

    def _write(self, method, effect, request):
        kind = self._enter(method, write=True)
        value = effect()
        if kind == "timeout_after_write":
            raise UncertainWrite("TIMEOUT_AFTER_WRITE")
        return Ack(operation=method, request=request, raw=value)

    # -- port ---------------------------------------------------------------------------------
    def account(self):
        self._enter("account")
        return AccountState(valid=self.session_valid, account_id="fake-account",
                            own_profile=self.own_profile, raw={"valid": self.session_valid})

    def lookup(self, profile_url):
        self._enter("lookup")
        person = self._person(profile_url)
        pending = any(self._invites(item, person["provider_id"]) for item in self.invitations)
        return Person(profile=profile_url, provider_id=person["provider_id"],
                      network_distance=person["distance"], pending_invitation=pending, raw=dict(person))

    def send_invitation(self, provider_id, note):
        def effect():
            self._known(provider_id)
            if any(self._invites(item, provider_id) for item in self.invitations):
                raise InvitationExists("ALREADY_INVITED", "INVITATION_ALREADY_PENDING")
            invitation_id = self.add_invitation(provider_id, self._profile_of(provider_id), note)
            return dict(self.invitations[-1], invitation_id=invitation_id)
        return self._write("send_invitation", effect, {"provider_id": provider_id, "note": note})

    def sent_invitations(self, cursor=None):
        kind = self._enter("sent_invitations")
        records = [Invitation(item["invitation_id"], item["invitee_provider_id"], item["invitee_profile"],
                              item["note"], item["sent_at"], dict(item)) for item in self.invitations]
        return self._page(records, cursor, kind)

    def relations(self, cursor=None):
        kind = self._enter("relations")
        records = [Person(person["profile"], person["provider_id"], 1, False, dict(person))
                   for person in self.people.values() if person["distance"] == 1]
        return self._page(records, cursor, kind)

    def chats(self, cursor=None, person=None):
        kind = self._enter("chats")
        wanted = self._person(person)["provider_id"] if person is not None else None
        ordered = sorted(self.chat_log.items(), key=lambda entry: entry[1]["activity"], reverse=True)
        records = [Chat(chat_id, chat["attendee"], self._profile_of(chat["attendee"]),
                        {"id": chat_id, "account_id": "fake-account"})
                   for chat_id, chat in ordered if person is None or chat["attendee"] == wanted]
        return self._page(records, cursor, kind)

    def messages(self, chat_id):
        kind = self._enter("messages")
        if kind == "incomplete_page":
            raise IncompletePage()
        chat = self._chat(chat_id)
        return Thread(chat_id=chat_id, complete=True, raw=[dict(item) for item in chat["messages"]],
                      messages=tuple(Message(item["is_sender"], item["text"], item["sent_at"], dict(item))
                                     for item in chat["messages"]))

    def send_message(self, chat_id, text):
        def effect():
            chat = self._chat(chat_id)
            chat["messages"].append({"is_sender": True, "text": text, "sent_at": _now()})
            chat["activity"] = next(self._activity)
            return {"chat_id": chat_id}
        return self._write("send_message", effect, {"chat_id": chat_id, "text": text})

    def start_chat(self, provider_id, text):
        def effect():
            self._known(provider_id)
            existing = [chat_id for chat_id, chat in self.chat_log.items() if chat["attendee"] == provider_id]
            if existing:
                latest = max(existing, key=lambda chat_id: self.chat_log[chat_id]["activity"])
                chat = self.chat_log[latest]
                chat["messages"].append({"is_sender": True, "text": text, "sent_at": _now()})
                chat["activity"] = next(self._activity)
                return {"chat_id": latest}
            chat_id = self.add_chat(provider_id, [(True, text)])
            return {"chat_id": chat_id}
        return self._write("start_chat", effect, {"provider_id": provider_id, "text": text})


def _text(value):
    return value if isinstance(value, str) else None


def _heyreach_chat(item):
    """Neutral Chat from one listing item. Total: any malformed field becomes None and the raw
    item passes through untouched, so the caller's own validation of the raw answer decides
    exactly as it did before the port (review round 1)."""
    correspondent = item.get("correspondentProfile")
    if not isinstance(correspondent, dict):
        correspondent = {}
    return Chat(_text(item.get("id")), _text(correspondent.get("linkedin_id")),
                _text(correspondent.get("profileUrl")), item)


def _heyreach_message(item):
    """Neutral Message from one chatroom message; total, as _heyreach_chat."""
    sender = item.get("sender")
    is_sender = {"ME": True, "CORRESPONDENT": False}.get(sender) if isinstance(sender, str) else None
    return Message(is_sender, _text(item.get("body")), _text(item.get("createdAt")), item)


class HeyReachPort:
    """The port over today's HeyReach MCP client (`orchestrator.HeyReach` or a test fake with
    the same `call` / `_request`). Each method makes exactly the HeyReach call the code made
    before it, with the same arguments, and returns the raw answer in `raw` unchanged; neutral
    fields are filled only when the answer has the expected shape. Errors from the client
    propagate unchanged and this adapter raises none of its own on reads, so callers that still
    validate the raw HeyReach answer behave exactly as before (a migration exception to
    "complete-or-raise": check `complete`). In particular it never raises NotFound: an unknown
    lead or chat raises whatever the client raises (`orchestrator.HeyReach` turns a missing lead
    into RuntimeError "Lead is not present in HeyReach"; test clients may raise
    LeadNotFoundError), and an answer that does not name the person gives a Person whose
    provider_id is None (callers check `raw`, as before). Order and times are HeyReach's own:
    `chats` passes get_conversations_v2's order, which is not verified to be latest activity
    (AGENTS.md, inbox reading), and `messages` passes the chatroom's messages in the order
    returned with each `createdAt` string unchanged (e.g. "2026-09-30T23:41:51.466Z").

    Invitations are not available here: HeyReach invites only through the claimed campaign path
    (`outreach_campaign` + `outreach.claim_send` / `start_claimed_campaign`), which stays behind
    `provider == "heyreach"` until E3.T7."""

    provider = "heyreach"

    def __init__(self, client, sender_id):
        self.client = client
        self.sender_id = sender_id

    def account(self):
        raw = self.client.call("get_linked_in_account_by_id", {"accountId": self.sender_id})
        shaped = isinstance(raw, dict)
        valid = raw.get("authIsValid") if shaped and isinstance(raw.get("authIsValid"), bool) else None
        account_id = raw.get("id") if shaped and type(raw.get("id")) in (int, str) else None
        return AccountState(valid=valid, account_id=account_id,
                            own_profile=_text(raw.get("profileUrl")) if shaped else None, raw=raw)

    def lookup(self, profile_url):
        raw = self.client.call("get_lead", {"profileUrl": profile_url})
        provider_id = raw.get("linkedin_id") if isinstance(raw, dict) else None
        return Person(profile=profile_url, provider_id=provider_id if isinstance(provider_id, str) else None,
                      network_distance=None, pending_invitation=None, raw=raw)

    def send_invitation(self, provider_id, note):
        raise Unsupported("HEYREACH_INVITATIONS_USE_CAMPAIGN_PATH")

    def sent_invitations(self, cursor=None):
        raise Unsupported("HEYREACH_SENT_INVITATIONS_UNAVAILABLE")

    def relations(self, cursor=None):
        raise Unsupported("HEYREACH_RELATIONS_UNAVAILABLE")

    def chats(self, cursor=None, person=None, *, lead_id=None, limit=100):
        """One page of get_conversations_v2 for the sender. `person` filters by profile URL
        (leadProfileUrl), `lead_id` by HeyReach lead id (leadLinkedInId); the cursor is the
        offset. HeyReach cannot tell "no chat with this person" from "this spelling matched no
        lead", so an empty page filtered by `person` is marked complete=False (neutral field
        only; request and raw unchanged) rather than reporting a complete "no chat"."""
        arguments = {"linkedInAccountIds": [self.sender_id]}
        if person is not None:
            arguments["leadProfileUrl"] = person
        if lead_id is not None:
            arguments["leadLinkedInId"] = lead_id
        offset = int(cursor or 0)
        # Key order is the exact request each caller sent before the port (the request body is
        # unchanged byte for byte): the account-wide inbox page sent limit then offset
        # (engagement), the filtered listings offset then limit (collect, inactivity).
        if person is None and lead_id is None:
            arguments.update(limit=limit, offset=offset)
        else:
            arguments.update(offset=offset, limit=limit)
        raw = self.client.call("get_conversations_v2", arguments)
        items, total, complete = (), None, False
        if isinstance(raw, dict) and type(raw.get("totalCount")) is int and isinstance(raw.get("items"), list):
            total = raw["totalCount"]
            items = tuple(_heyreach_chat(item) for item in raw["items"] if isinstance(item, dict))
            complete = (len(items) == len(raw["items"]) and len(items) <= limit
                        and offset + len(items) <= total and (bool(items) or offset >= total)
                        and (bool(items) or person is None))
        end = offset + len(items)
        return Page(items=items, next_cursor=str(end) if complete and end < total else None,
                    total=total, complete=complete, raw=raw)

    def messages(self, chat_id):
        raw = self.client.call("get_chatroom", {"accountId": self.sender_id, "conversationId": chat_id})
        found, complete = (), False
        if isinstance(raw, dict) and isinstance(raw.get("messages"), list):
            found = tuple(_heyreach_message(item) for item in raw["messages"] if isinstance(item, dict))
            complete = (type(raw.get("totalMessages")) is int and len(found) == len(raw["messages"])
                        and raw["totalMessages"] == len(found))
        return Thread(chat_id=chat_id, messages=found, complete=complete, raw=raw)

    def send_message(self, chat_id, text, *, subject=""):
        """HeyReach send_message in an existing conversation (the only HeyReach message write)."""
        request = {"conversationId": chat_id, "linkedInAccountId": self.sender_id,
                   "message": text, "subject": subject}
        raw = self.client._request("send_message", request)
        return Ack(operation="send_message", request=request, raw=raw)

    def start_chat(self, provider_id, text):
        raise Unsupported("HEYREACH_START_CHAT_UNAVAILABLE")
