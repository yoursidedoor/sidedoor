"""Strict decoding of the locally observed 2026-09-17 HeyReach text schema."""

import copy
import datetime as dt
import hashlib
import json
import re
from urllib.parse import urlsplit

from orchestrator import profile_key


ROOM_KEYS = frozenset({
    "id", "read", "groupChat", "blockedByMe", "blockedByParticipant",
    "lastMessageAt", "lastMessageText", "lastMessageType", "lastMessageSender",
    "totalMessages", "linkedInAccountId", "correspondentProfile", "linkedInAccount",
    "messages",
})
MESSAGE_KEYS = frozenset({
    "createdAt", "body", "subject", "postLink", "isInMail", "transcript",
    "isVoiceNote", "audioDurationMs", "sender",
})


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False)


def _text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Missing text identity")
    return value


NAME_KEYS = ("fullName", "firstName", "lastName")


def _profile_shaped(value):
    """True when a purported display name is really a profile URL or key.

    A profile substituted for a name keys nothing in the ``name:`` suppression
    space, so accepting one would let every name-keyed exclusion miss while the
    value still looked like a resolved name. The schema refuses to model it.
    """
    text = value.strip()
    if "://" in text or "linkedin.com/" in text.casefold():
        return True
    try:
        profile_key("https://" + text)
    except ValueError:
        return False
    return True


def _check_name_types(participant):
    """Every present correspondent name field must be a string or absent.

    This is the only part of the correspondent name that room decoding enforces.
    A wrongly typed field is an unsupported provider shape, exactly like every
    other field this module type-checks. Whether the decoded value is *usable*
    as a name is a separate question, answered by ``correspondent_name`` at the
    call sites that consume one — deliberately not here, because rejecting a
    whole chatroom over a cosmetic display name would be a NEW way for inbound
    observation to fail, and a dropped inbound read is lost stop-on-reply
    evidence. Nothing in this change may remove an inbound observation.
    """
    if not isinstance(participant, dict):
        raise ValueError("Missing correspondent")
    for field in NAME_KEYS:
        if field in participant and participant[field] is not None and not isinstance(participant[field], str):
            raise ValueError("Unsupported correspondent name")


def correspondent_name(participant):
    """The correspondent's observed display name, or None when absent.

    HeyReach reports the correspondent on every chatroom and normally carries a
    name with it. The schema previously modelled only ``profileUrl`` and
    ``linkedin_id``, so a name-keyed exclusion had nothing provider-observed to
    match and callers had to guess the field layout. This decodes the name
    strictly: each present field must be a string, and a profile-shaped value is
    rejected rather than passed on as a name. It is observed, mutable metadata —
    a person may rename themselves — so it is deliberately NOT part of the
    immutable message identity or of the conversation binding.
    """
    _check_name_types(participant)
    full = (participant.get("fullName") or "").strip()
    if not full:
        full = " ".join((participant.get(field) or "").strip() for field in ("firstName", "lastName")).strip()
    if not full:
        return None
    if _profile_shaped(full):
        raise ValueError("Correspondent profile supplied as a display name")
    return full


def _profile(value):
    _text(value)
    parsed = urlsplit(value)
    if parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment:
        raise ValueError("Ambiguous LinkedIn profile URL")
    return "https://" + profile_key(value)


TIMESTAMP = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,7}))?Z")


def _canonical_time(value):
    """One millisecond spelling per message time.

    Every stored HeyReach time has exactly three fractional digits. Should the provider ever
    send another number (none, fewer, or more digits), the fraction is cut or zero-padded to
    milliseconds, so the same message read in two spellings keeps one synthetic identity (and one
    reply slot). Every three-digit spelling is unchanged, so every stored message id and raw
    payload is too.
    """
    match = TIMESTAMP.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise ValueError("Unsupported provider timestamp")
    return match.group(1) + "." + (match.group(2) or "")[:3].ljust(3, "0") + "Z"


def _time(value):
    return dt.datetime.fromisoformat(_canonical_time(value))


# HeyReach has no per-message id and moves one message's createdAt by seconds between reads
# (a reply read at 23:41:51.466 was later 23:41:54.548, then 23:41:54.342). Same window as
# engagement.MESSAGE_TIME_DRIFT, which also keeps a drifted re-read from being recorded twice.
MESSAGE_TIME_DRIFT = dt.timedelta(minutes=10)


def _near(first, second):
    return abs(first - second) <= MESSAGE_TIME_DRIFT


def _source_id(identity):
    return "heyreach:synthetic:sha256:v1:" + hashlib.sha256(
        _json(identity).encode("utf-8")).hexdigest()


class HeyReachDecoder:
    """Bind callbacks to one independently configured account and sender profile.

    clock returns an aware datetime. Callbacks never perform provider or database
    access except the caller-supplied prior_event(source, source_id) lookup, which
    must return a persisted event dictionary or None for restart-safe replay.
    Inputs must come from authenticated provider reads. verified marks that read,
    not delivery or job relevance. Returned outbound_observed observations are
    never initial_sent/followup_sent delivery proof.
    """

    def __init__(self, sender_id, sender_profile, *, clock=None, prior_event=None):
        if type(sender_id) is not int or sender_id <= 0:
            raise ValueError("Positive integer sender ID required")
        self.sender_id = sender_id
        self.sender_profile = _profile(sender_profile)
        self.clock = clock if clock is not None else lambda: dt.datetime.now(dt.timezone.utc)
        self.prior_event = prior_event
        self._payloads = {}
        self._events = {}

    def _now(self):
        observed = self.clock()
        if not isinstance(observed, dt.datetime) or observed.utcoffset() is None:
            raise ValueError("Aware observation clock required")
        return observed.astimezone(dt.timezone.utc)

    def _room(self, room, observed, listing=False):
        """Decode one room. ``listing`` marks a get_conversations_v2 item: HeyReach may list a
        conversation without its messages (observed 2026-09-17: totalMessages 3, messages []),
        so a listing may carry fewer messages than totalMessages. The chatroom read that follows
        is always decoded with listing=False and must carry the complete history."""
        if not isinstance(room, dict) or set(room) != ROOM_KEYS:
            raise ValueError("Unsupported HeyReach room schema")
        conversation_id = _text(room["id"])
        if type(room["linkedInAccountId"]) is not int or room["linkedInAccountId"] != self.sender_id:
            raise ValueError("Room account mismatch")
        account = room["linkedInAccount"]
        if not isinstance(account, dict) or type(account.get("id")) is not int or account["id"] != self.sender_id:
            raise ValueError("Embedded account mismatch")
        if _profile(account.get("profileUrl")) != self.sender_profile:
            raise ValueError("Embedded sender profile mismatch")
        participant = room["correspondentProfile"]
        if not isinstance(participant, dict):
            raise ValueError("Missing correspondent")
        profile = _profile(participant.get("profileUrl"))
        correspondent_id = _text(participant.get("linkedin_id"))
        _check_name_types(participant)
        if profile == self.sender_profile:
            raise ValueError("Correspondent cannot be sender")
        for field in ("read", "groupChat", "blockedByMe", "blockedByParticipant"):
            if type(room[field]) is not bool:
                raise ValueError("Unsupported room flag")
        if room["groupChat"]:
            raise ValueError("Group conversations unsupported")
        messages = room["messages"]
        if (not isinstance(messages, list) or type(room["totalMessages"]) is not int
                or room["totalMessages"] <= 0 or len(messages) > room["totalMessages"]
                or not listing and len(messages) != room["totalMessages"]):
            raise ValueError("Empty or incomplete room history")
        complete = len(messages) == room["totalMessages"]
        if room["lastMessageType"] != "TEXT" or room["lastMessageSender"] not in {"ME", "CORRESPONDENT"}:
            raise ValueError("Unsupported latest message type or direction")
        _text(room["lastMessageText"])
        latest = _time(room["lastMessageAt"])
        events = []
        identities = set()
        for message in messages:
            if not isinstance(message, dict) or set(message) != MESSAGE_KEYS:
                raise ValueError("Unsupported HeyReach message schema")
            if message["sender"] not in {"ME", "CORRESPONDENT"}:
                raise ValueError("Unsupported message direction")
            created_at = _canonical_time(message["createdAt"])
            occurred = _time(created_at)
            if occurred > observed:
                raise ValueError("Message occurs after observation")
            _text(message["body"])
            if message["subject"] is not None and not isinstance(message["subject"], str):
                raise ValueError("Unsupported subject")
            if (message["isInMail"] is not False or message["isVoiceNote"] is not False
                    or any(message[field] is not None for field in
                           ("postLink", "transcript", "audioDurationMs"))):
                raise ValueError("Unobserved nontext message variant")
            identity = {
                "account_id": self.sender_id, "sender_profile": self.sender_profile,
                "conversation_id": conversation_id, "profile": profile,
                "correspondent_id": correspondent_id, "created_at": created_at,
                "direction": message["sender"], "body": message["body"], "subject": message["subject"],
            }
            source_id = _source_id(identity)
            if source_id in identities:
                raise ValueError("Duplicate or colliding synthetic message identity")
            identities.add(source_id)
            direction = "outbound" if message["sender"] == "ME" else "inbound"
            raw = {key: identity[key] for key in
                   ("account_id", "conversation_id", "profile", "correspondent_id")}
            raw.update({"provider_lead_id": correspondent_id, "account_profile": self.sender_profile,
                        "sender_profile": self.sender_profile if direction == "outbound" else profile,
                        "message_id": source_id, "direction": direction, "text": message["body"],
                        "quoted": None, "forwarded": None})
            raw["message"] = dict(copy.deepcopy(message), createdAt=created_at)
            source = "https://api.heyreach.io/api/public/inbox/GetChatroom"
            fields = ("message_id", "conversation_id", "account_id", "sender_profile",
                      "direction", "text", "quoted", "forwarded")
            inbox = {field: raw[field] for field in fields}
            inbox.update({"schema": "linkedin-inbox-v1",
                          "raw_paths": {field: [field] for field in fields},
                          "authentication": {"method": "authenticated_provider_read",
                                             "source": source,
                                             "verified_by": "heyreach-observed-schema-v1",
                                             "observed_at": observed.isoformat()}})
            events.append({
                "source": "heyreach", "source_id": source_id,
                "source_id_kind": "synthetic_content_sha256_v1", "profile": profile,
                "kind": "outbound_observed" if message["sender"] == "ME" else "inbound",
                "direction": direction,
                "occurred_at": occurred.isoformat(), "observed_at": observed.isoformat(),
                "evidence": "heyreach:get_chatroom:" + str(self.sender_id) + ":" + conversation_id,
                "raw": raw, "inbox": inbox, "verified": True, "job_search_relevance": "unknown",
                "full_linkedin_history_clearance": False,
            })
        # The summary must name a message in the complete history and never be newer than the
        # newest one (that would mean messages are missing). It may lag: right after a send
        # HeyReach has shown the new message in history with the summary still on the one before
        # (observed 2026-09-17), which is a complete history, not a contradiction.
        if complete and (latest > max(_time(message["createdAt"]) for message in messages) + MESSAGE_TIME_DRIFT or not any(
                _near(_time(message["createdAt"]), latest)
                and message["sender"] == room["lastMessageSender"]
                and message["body"] == room["lastMessageText"] for message in messages)):
            raise ValueError("Latest-message summary contradicts room history")
        return {"conversation_id": conversation_id, "profile": profile,
                "account_id": self.sender_id, "sender_profile": self.sender_profile,
                "correspondent_id": correspondent_id}, events

    def decode_conversation(self, item):
        """Validate a listing item (its messages may be omitted) and retain its immutable snapshot."""
        binding, unused_events = self._room(item, self._now(), listing=True)
        return {**binding, "_snapshot": _json(item)}

    def decode_chatroom(self, room, conversation):
        """Reconcile independently fetched room with listing; return observations."""
        observed = self._now()
        if not isinstance(conversation, dict) or not isinstance(conversation.get("_snapshot"), str):
            raise ValueError("Validated conversation context required")
        previous_binding, previous_events = self._room(json.loads(conversation["_snapshot"]), observed, listing=True)
        if conversation != {**previous_binding, "_snapshot": conversation["_snapshot"]}:
            raise ValueError("Conversation context binding mismatch")
        binding, events = self._room(room, observed)
        if binding != previous_binding:
            raise ValueError("Chatroom conversation/account/participant mismatch")
        payloads = {event["source_id"]: _json(event["raw"]) for event in events}
        for event in previous_events:
            if payloads.get(event["source_id"]) == _json(event["raw"]):
                continue
            listed = event["raw"]["message"]
            # The same message re-read with a drifted time is not lost.
            if not any(other["raw"]["message"]["sender"] == listed["sender"]
                       and other["raw"]["message"]["body"] == listed["body"]
                       and _near(_time(other["raw"]["message"]["createdAt"]), _time(listed["createdAt"]))
                       for other in events):
                raise ValueError("Chatroom lost or changed a listed message")
        for source_id, payload in payloads.items():
            if source_id in self._payloads and self._payloads[source_id] != payload:
                raise ValueError("Synthetic identity reused with changed immutable payload")
        retained = []
        for event in events:
            prior = self._events.get(event["source_id"])
            if prior is None and self.prior_event is not None:
                prior = self.prior_event("heyreach", event["source_id"])
            if prior is not None:
                prior = copy.deepcopy(prior)
                previous_observed = dt.datetime.fromisoformat(prior["observed_at"])
                if (previous_observed.utcoffset() is None or previous_observed > observed
                        or previous_observed < dt.datetime.fromisoformat(event["occurred_at"])
                        or prior["inbox"]["authentication"]["observed_at"] != prior["observed_at"]):
                    raise ValueError("Invalid prior authenticated observation time")
                comparison = copy.deepcopy(event)
                comparison["observed_at"] = prior["observed_at"]
                comparison["inbox"]["authentication"]["observed_at"] = prior["observed_at"]
                if _json(comparison) != _json(prior):
                    raise ValueError("Stored synthetic identity has different immutable payload")
                event = prior
            retained.append(event)
        self._payloads.update(payloads)
        self._events.update({event["source_id"]: copy.deepcopy(event) for event in retained})
        return sorted(retained, key=lambda event: (event["occurred_at"], event["source_id"]))


Decoder = HeyReachDecoder
