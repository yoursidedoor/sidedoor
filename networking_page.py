"""The Networking & Referrals page (E6.T5; E6.T0 spec section 7, route /sent).

Owner, 2026-10-22: the tab is "Networking & Referrals", with ONE entry per contact: their invitation,
follow-ups and replies collapsed into one item, and a contact's action item (the reply AI's
flag_owner note) shown on that entry with the same [Mark done] as Approvals' "Notes for you".

Display only, like Overview: a page load (GET) reads through `overview_page.snapshot` (nothing
written, no -wal/-shm) and every line is a record the rest of the product already wrote. The one
write is [Mark done], `reply_tools.mark_done` (the function Approvals uses), so a note marked done
here is gone from Approvals and from Overview's "Needs you" count too.

Sources (why each is the record the product itself treats as that fact):
- the conversation: `engagement_events` kinds initial_sent, accepted, followup_sent, inbound,
  outbound_observed and opt_out. initial_sent/followup_sent are written only on confirmed sends
  (`engagement._record` refuses them without independent verification; `sequence.reconcile_invite`
  writes the invitation only once a read shows it pending or accepted). An invitation versus a
  message is Overview's rule (`overview_page._invitation`, O3). A REPLY receipt is outbound_observed
  with purpose REPLY. Any other outbound_observed is a message from our side seen in the LinkedIn
  inbox: hidden when it is one of our recorded sends (`engagement_outbound_reconciliations`, or the
  same text within `engagement.MESSAGE_TIME_DRIFT`), otherwise shown as seen in the inbox (e.g. the
  user writing by hand).
- the text of our messages: the invitation note in the event (`raw.note`, sequence._record_sent),
  else the action that sent it (`outreach_actions.payload` message/note, found by the event's
  `invite:<action_id>`, `preparation_action_id` or receipt `raw.action_id`); their messages: the
  inbox envelope's text (`inbox.text`, the Unipile reader and the HeyReach decoder).
- not yet confirmed: "Sending…" is `outreach_actions` INVITE/INITIAL/FIRST_MESSAGE/FOLLOWUP/URGENT/REPLY in
  REQUEST_STARTED, ACKNOWLEDGED_DELIVERY_UNCONFIRMED or INVITE_RETRY_WAIT (Overview's INVITE_WAITING,
  which Sidedoor still settles; an INVITE LinkedIn acknowledged reads "Invitation sent", owner 2026-10-24);
  its "Invitations sending" filter lists the INVITE ones, the ones
  Overview's "waiting for confirmation" counts. UNCERTAIN_NO_RETRY and unsettled HeyReach launches
  (`outreach_attempts` CLAIMED, DISPATCHING, ACKNOWLEDGED_DELIVERY_UNCONFIRMED, UNCERTAIN_NO_RETRY) are
  never sent again: "couldn't confirm it went out", not Waiting. "Did not go out": CONFIRMED_NOT_SENT
  (the action stays on record, so `research_workflow.prior_contact_record` never drafts the person
  again). "Couldn't send": `approvals.refused_after_approval` (the Approvals cards), which needs you.
- the judge's verdict and "approved by you": the newest DECISION row of `outreach_agent_audit` for the
  action (by action id, its index), not a reconciliation or composition one; a decision whose source is
  OWNER_APPROVAL is the user's approval (`approvals.decision`).
- who: `approvals_page.person_name` (outreach_actions, rw_drafts), else `rw_contacts`, looked up under
  both spellings (orchestrator's linkedin.com/in/x and research's https://www.linkedin.com/in/x); title and why
  this person: `approvals_page.research_reason` on the research draft (`rw_drafts`, the sent action's
  own source, else the latest version); company: the action, the draft or rw_contacts.
- referral: `referrals` (one row per person, `referrals.py`), read directly: `referrals.current`
  creates the table, and a GET writes nothing.
- notes: `reply_tools.open_flags`, tied to the person by `workflow_tasks.contact_id` of the task that
  flagged it (as Approvals does).
- closed / opted out: `engagement_closures` (permanent closure), `engagement_stops` OPTED_OUT and
  opt_out events.

One person, one entry: identities are joined through the rw_aliases contact rows (the closure
`engagement.identity_keys` computes, read once per request), e.g. a HeyReach-era spelling and the
Unipile one, and a LinkedIn member-id spelling ("ACoA...") joins the person `outreach.resolve_sender`'s
rule ties it to (its lead ids and conversation ids, the rule the reply path uses), with the sender links
and `outreach.tracked_person` read once per request (`Tracking`). A member-id spelling that resolves to
nobody, to several people, or that is tracked on its own stays its own entry: never wrongly merged.

Who is listed: anyone with a message of ours (sent, sending, failed or refused), an invitation still
queued (an INVITE action in PENDING; owner, 2026-10-24: the page shows everyone in the pipeline; in the
"Waiting" filter, not "Invitations sending", which is Overview's count), a referral or an open note. Someone who only wrote to the user (a cold stranger) is not listed, and Overview's "Replies
received" counts the listed people who wrote today (`replied_between`, the "Replied today" filter).

Indexes: the summary reads engagement_events through `engagement_kind_identity` (kind, identity,
occurred_at; created by engagement.initialize), a page's detail through the same index or
`engagement_identity`, outreach_actions by its state index and its (identity, slot) key, the audit by
`outreach_agent_audit_action`, rw_contacts by `rw_contacts_identity` (research_workflow.initialize).
One-row-per-person tables (referrals, engagement_closures, engagement_stops) and the frozen HeyReach
`outreach_attempts` are read whole; search reads the outreach_actions, rw_contacts and rw_drafts names.
"""


import datetime as dt
import json
import math
import re
import sqlite3
import urllib.parse
from pathlib import Path
from zoneinfo import ZoneInfo

import approvals
import engagement
import overview_page as overview
import workflow_tasks
import paths

DATABASE = Path(paths.STATE) / paths.DATABASE   # dashboard.DATABASE
PAGE_SIZE = 25
UNAVAILABLE = overview.UNAVAILABLE
FILTERS = (("all", "All"), ("replied", "Replied"), ("replied_today", "Replied today"), ("waiting", "Waiting"),
           ("sending", "Sending"), ("referral_agreed", "Referral agreed"), ("needs_you", "Needs you"),
           ("closed", "Closed"))
FILTER_KEYS = {key for key, _ in FILTERS}
KINDS = ("accepted", "followup_sent", "inbound", "initial_sent", "opt_out", "outbound_observed")
MESSAGE_PURPOSES = ("INVITE", "INITIAL", "FIRST_MESSAGE", "FOLLOWUP", "URGENT", "REPLY")
SENDING = tuple(overview.INVITE_WAITING)            # acknowledged or in flight; Sidedoor still settles these
UNCERTAIN = ("UNCERTAIN_NO_RETRY",)                 # never resent; kept for reconciliation only
NOT_SENT = ("CONFIRMED_NOT_SENT",)
QUEUED = ("PENDING",)
ACKNOWLEDGED = "ACKNOWLEDGED_DELIVERY_UNCONFIRMED"                               # an invitation not started yet (owner, 2026-10-24: list it)
CAMPAIGN_UNSETTLED = ("CLAIMED", "DISPATCHING", "ACKNOWLEDGED_DELIVERY_UNCONFIRMED", "UNCERTAIN_NO_RETRY")
AUDIT_SCAN = 50
SKIPPED_PHASES = ("RECONCILIATION", "COMPOSITION")
LINK_FIELDS = ("correspondent_id", "provider_lead_id", "linkedin_id")
CONVERSATION_FIELDS = ("conversation_id", "conversationId")
DOM = re.compile(r"^contact-[0-9a-f]{16}$")
SEEN_IN_INBOX = "Your message, seen in your LinkedIn inbox"

STATUS = {"closed": ("Closed", "muted"), "opted_out": ("Opted out", "muted"), "replied": ("Replied", "success"),
          "connected": ("Connected", "info"), "messaged": ("Messaged", "info"),
          "invited": ("Invited, waiting for acceptance", "info"), "sending": ("Sending…", "warning"),
          "invite_sent": ("Invitation sent", "info"),
          "queued": ("Invitation queued", "muted"),
          "uncertain": ("Couldn't confirm it went out", "warning"), "failed": ("Couldn't send", "error"),
          "none": ("Nothing sent yet", "muted")}
REFERRAL_WORDS = {"AGREED": ("Referral agreed", "success"), "DECLINED": ("Referral declined", "muted")}
PURPOSE_WORDS = {"INVITE": "Invitation note", "INITIAL": "Message", "FIRST_MESSAGE": "First message",
                 "FOLLOWUP": "Follow-up", "URGENT": "Follow-up", "REPLY": "Your reply"}
SENDING_WORDS = "Sending… LinkedIn took the request; Sidedoor has not seen it arrive yet."
UNCERTAIN_WORDS = "Sidedoor couldn't confirm this went out, and it won't send it again."
# A CONFIRMED_NOT_SENT action stays on record, and research_workflow.prior_contact_record refuses anyone
# with an outreach action, so Sidedoor never drafts this person again.
NOT_SENT_WORDS = {"INVITE": "Did not go out: Sidedoor checked and the invitation never arrived. Sidedoor won't invite "
                            "them again on its own; you can connect with them on LinkedIn yourself.",
                  None: "Did not go out: Sidedoor checked and the message never arrived. Sidedoor won't send it again "
                        "on its own; you can write to them on LinkedIn yourself."}
NOTED = {"1": ("success", "Marked done."), "0": ("success", "That note was already done.")}
BUSY = "Sidedoor is busy with another task. Try again in a moment."   # approvals_page.BUSY


def _object(text):
    try:
        value = json.loads(text) if isinstance(text, str) else None
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _key(value):
    import outreach   # the same key every outreach table is stored under
    return outreach._identity_of(value) if isinstance(value, str) and value.strip() else None


def _research_key(identity):
    """The spelling research's tables (rw_contacts, rw_drafts) store a person under
    (`research_workflow.profile_key`: https://www.linkedin.com/in/x)."""
    import research_workflow
    try:
        return research_workflow.profile_key("https://" + identity)
    except (ValueError, TypeError):
        return None


def _text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def _word(table, value, default):
    """table[value] for a string code; anything else (a list, a number, None) gets the default."""
    return table.get(value, default) if isinstance(value, str) else default


def _marks(values):
    return ",".join("?" * len(values))


def _pages():
    import approvals_page   # Approvals' readers; imported here so Overview can count without fastapi
    return approvals_page


# --- Who is on file -----------------------------------------------------------------------------

class Person:
    """What the list needs about one identity (one spelling of one person)."""

    def __init__(self):
        self.kinds = {}            # kind -> count
        self.last = None           # latest activity (UTC)
        self.last_inbound = None
        self.observed = 0          # our outbound_observed that may be shown as "seen in your inbox"
        self.direct = False        # a confirmed message that is not an invitation
        self.sending = False
        self.invite_sending = False
        self.sending_unacked = False   # a sending message other than an acknowledged invitation
        self.queued = False        # an INVITE action still PENDING
        self.uncertain = False
        self.failed = False
        self.refused = 0
        self.referral = None       # (state, updated_at)
        self.notes = 0
        self.closed = False
        self.opted_out = False

    def touch(self, value):
        moment = overview._utc(value) if value else None
        if moment is not None and (self.last is None or moment > self.last):
            self.last = moment
        return moment

    def listed(self):
        """On this page: a message of ours (sent, sending, queued invitation, failed, refused), a referral or an
        open note."""
        ours = any(self.kinds.get(kind) for kind in ("initial_sent", "followup_sent", "outbound_observed"))
        return bool(ours or self.sending or self.queued or self.uncertain or self.failed or self.refused or self.referral is not None
                    or self.notes)


def people(reader):
    """{identity: Person} from every record that puts someone on this page, plus the open notes
    ({note_id: (identity, note)}) and refused cards ({action_id: (identity, item)}) found on the way."""
    found = {}

    def get(identity):
        key = _key(identity)
        return found.setdefault(key, Person()) if key is not None else None

    for kind, identity, count, last in reader.rows(
            "SELECT kind,identity,COUNT(*),MAX(occurred_at) FROM engagement_events WHERE kind IN (%s) "
            "GROUP BY kind,identity" % _marks(KINDS), KINDS, "engagement_events"):
        person = get(identity)
        if person is not None:
            person.kinds[kind] = person.kinds.get(kind, 0) + count
            moment = person.touch(last)
            if kind == "inbound":
                person.last_inbound = moment
    for identity, text in reader.rows("SELECT identity,payload FROM engagement_events WHERE kind='initial_sent'", (),
                                      "engagement_events"):
        person, payload = get(identity), _object(text)
        if person is not None and payload is not None and not overview._invitation("initial_sent", payload):
            person.direct = True
    reconciled = {tuple(row) for row in reader.rows("SELECT source,source_id FROM engagement_outbound_reconciliations", (),
                                                    "engagement_outbound_reconciliations")}
    for identity, source, source_id, text in reader.rows(
            "SELECT identity,source,source_id,payload FROM engagement_events WHERE kind='outbound_observed'", (),
            "engagement_events"):
        payload = _object(text)
        if payload is not None and payload.get("purpose") != "REPLY" and (source, source_id) not in reconciled:
            if (person := get(identity)) is not None:
                person.observed += 1
    states = SENDING + UNCERTAIN + NOT_SENT + QUEUED
    for identity, text, state, claimed_at in reader.rows(
            "SELECT identity,payload,state,claimed_at FROM outreach_actions WHERE state IN (%s)" % _marks(states), states,
            "outreach_actions"):
        payload = _object(text)
        if payload is None or payload.get("purpose") not in MESSAGE_PURPOSES or (person := get(identity)) is None:
            continue
        if state in QUEUED:
            if payload.get("purpose") != "INVITE":
                continue
            person.queued = True
        elif state in SENDING:
            person.sending = True
            person.invite_sending = person.invite_sending or payload.get("purpose") == "INVITE"
            # LinkedIn took the invitation request (owner, 2026-10-24, N2: the chip says "Invitation
            # sent", as the sequence line does); anything else in flight is still "Sending…".
            person.sending_unacked = person.sending_unacked or not (
                payload.get("purpose") == "INVITE" and state == ACKNOWLEDGED)
        elif state in UNCERTAIN:
            person.uncertain = True
        else:
            person.failed = True
        person.touch(claimed_at)
    for identity, claimed_at in reader.rows("SELECT identity,claimed_at FROM outreach_attempts WHERE state IN (%s)"
                                            % _marks(CAMPAIGN_UNSETTLED), CAMPAIGN_UNSETTLED, "outreach_attempts"):
        if (person := get(identity)) is not None:
            person.uncertain = True
            person.touch(claimed_at)
    for identity, state, updated_at in reader.rows("SELECT contact_id,state,updated_at FROM referrals", (), "referrals"):
        if (person := get(identity)) is not None:
            if person.referral is None or str(updated_at or "") >= str(person.referral[1] or ""):
                person.referral = (state, updated_at)
            person.touch(updated_at)
    for (identity,) in reader.rows("SELECT identity FROM engagement_closures", (), "engagement_closures"):
        if (person := get(identity)) is not None:
            person.closed = True
    for (identity,) in reader.rows("SELECT identity FROM engagement_stops WHERE state='OPTED_OUT'", (), "engagement_stops"):
        if (person := get(identity)) is not None:
            person.opted_out = True
    notes, refused = {}, {}
    if reader.database is not None:
        import reply_tools
        for note in reply_tools.open_flags(reader.database):
            row = reader.rows("SELECT contact_id FROM workflow_tasks WHERE task_id=?", (note["task_id"],), "workflow_tasks")
            identity = _key(row[0][0]) if row and row[0][0] else None
            if identity is not None:
                person = get(identity)
                person.notes += 1
                person.touch(note["flagged_at"])
                notes[note["note_id"]] = (identity, note)
        for item in approvals.refused_after_approval(reader.database, 500):
            identity = _key(item["profile"])
            if identity is not None:
                get(identity).refused += 1
                refused[item["action_id"]] = (identity, item)
    return found, notes, refused


# --- One person, one entry ------------------------------------------------------------------------

class Joins:
    """Union-find over identities, with the rw_aliases closure read once per request (what
    `engagement.identity_keys` computes per identity, without re-reading every alias for each)."""

    def __init__(self, reader, identities):
        self.parent = {identity: identity for identity in identities}
        self.size = dict.fromkeys(identities, 1)
        reader.alias_rows = reader.rows("SELECT alias,canonical FROM rw_aliases WHERE kind='contact'", (), "rw_aliases") \
            if reader.has("rw_aliases") else None   # kept for sequence_view (engagement.aliases_read_once)
        for alias, canonical in reader.alias_rows or ():
            one, two = _key(alias), _key(canonical)
            if one and two:
                self.join(one, two)

    def root(self, identity):
        parent = self.parent
        if identity not in parent:
            parent[identity] = identity
            self.size[identity] = 1
        while parent[identity] != identity:
            parent[identity] = parent[parent[identity]]
            identity = parent[identity]
        return identity

    def join(self, one, two):
        one, two = self.root(one), self.root(two)
        if one != two:
            self.parent[one] = two
            self.size[two] += self.size.pop(one)

    def alone(self, identity):
        return self.size[self.root(identity)] == 1

    def groups(self):
        found = {}
        for identity in list(self.parent):
            found.setdefault(self.root(identity), []).append(identity)
        return [sorted(members) for members in found.values()]


class Tracking:
    """`outreach.resolve_sender`'s inputs, read once per request: the sender links (lead ids and
    conversation ids our own records carry, `outreach._sender_links`) and `outreach.tracked_person`
    per identity, cached."""

    def __init__(self, database):
        self.database = database
        self._links = None
        self._tracked = {}

    def links(self):
        if self._links is None:
            import outreach
            self._links = outreach._sender_links(self.database)
        return self._links

    def tracked(self, identity):
        if identity not in self._tracked:
            import outreach
            self._tracked[identity] = bool(outreach.tracked_person(self.database, "https://" + identity))
        return self._tracked[identity]


def _links(reader, identity):
    """The lead ids and conversation ids this identity's own events carry (as outreach._sender_links reads them)."""
    import outreach
    leads, conversations = set(), set()
    for (text,) in reader.rows("SELECT payload FROM engagement_events WHERE identity=?", (identity,), "engagement_events"):
        found = []
        outreach._walk(_object(text), found)
        for item in found:
            leads.update(str(item[field]) for field in LINK_FIELDS if item.get(field) not in (None, ""))
            conversations.update(str(item[field]) for field in CONVERSATION_FIELDS if item.get(field) not in (None, ""))
    return sorted(leads), sorted(conversations)


def member_id_person(reader, identity, joins, tracking, listed):
    """The one person `outreach.resolve_sender`'s rule ties this member-id spelling to, or None (it stays
    its own entry). resolve_sender's decision, on the per-request caches: tracked on its own -> its own
    entry; exactly one tracked person among those our records tie its lead ids and conversation ids to ->
    that person; none or several -> apart. A spelling that cannot end up on the page (not listed itself
    and tied to nobody listed) is not resolved at all."""
    leads, conversations = _links(reader, identity)
    links = tracking.links()
    candidates = set()
    for kind, keys in (("lead", leads), ("conversation", conversations)):
        for key in keys:
            candidates |= links.get((kind, key), set())
    own = joins.root(identity)
    candidates = {candidate for candidate in candidates if joins.root(candidate) != own}
    if not candidates:
        return None
    if identity not in listed and not any(joins.root(candidate) in listed for candidate in candidates):
        return None
    if tracking.tracked(identity):
        return None
    tracked = {candidate for candidate in sorted(candidates) if tracking.tracked(candidate)}
    return next(iter(tracked)) if len(tracked) == 1 else None


def groups(reader, everyone):
    """Lists of identities, one per person."""
    import outreach
    joins = Joins(reader, set(everyone))
    listed = {joins.root(identity) for identity, person in everyone.items() if person.listed()}
    listed |= {identity for identity, person in everyone.items() if person.listed()}
    tracking = Tracking(reader.database)
    for identity in sorted(everyone):
        if not identity.split("/in/", 1)[-1].startswith(outreach.MEMBER_ID_PREFIX):
            continue
        if not joins.alone(identity):
            continue                 # already joined through an alias
        person = member_id_person(reader, identity, joins, tracking, listed)
        if person:
            joins.join(identity, person)
    return joins.groups()


class Contact:
    def __init__(self, members, everyone, reader):
        self.ids = members
        self.reader = reader
        own = [everyone[identity] for identity in members if identity in everyone]
        self.kinds = {}
        for person in own:
            for kind, count in person.kinds.items():
                self.kinds[kind] = self.kinds.get(kind, 0) + count
        self.last = max((person.last for person in own if person.last), default=None)
        self.last_inbound = max((person.last_inbound for person in own if person.last_inbound), default=None)
        self.observed = sum(person.observed for person in own)
        self.direct = any(person.direct for person in own)
        self.sending = any(person.sending for person in own)
        self.invite_sending = any(person.invite_sending for person in own)
        self.sending_unacked = any(person.sending_unacked for person in own)
        self.queued = any(person.queued for person in own)
        self.uncertain = any(person.uncertain for person in own)
        self.failed = any(person.failed for person in own)
        self.refused = sum(person.refused for person in own)
        referrals = [person.referral for person in own if person.referral]
        self.referral = max(referrals, key=lambda value: str(value[1] or "")) if referrals else None
        self.notes = sum(person.notes for person in own)
        self.closed = any(person.closed for person in own)
        self.opted_out = any(person.opted_out for person in own) or bool(self.kinds.get("opt_out"))
        self.listed = any(person.listed() for person in own)
        self._status = None

    def _seen_in_inbox(self):
        """A message from our side the conversation view shows as seen in the inbox (not one of our
        recorded sends seen again)."""
        if not self.observed:
            return False
        items = conversation(self.reader, self.ids, _actions(self.reader, self.ids))
        return any(item["label"] == SEEN_IN_INBOX for item in items)

    def status(self):
        if self._status is None:
            kinds = self.kinds
            if self.closed:
                self._status = "closed"
            elif self.opted_out:
                self._status = "opted_out"
            elif kinds.get("inbound"):
                self._status = "replied"
            elif kinds.get("accepted") or kinds.get("followup_sent") or self.direct:
                self._status = "connected"
            elif self._seen_in_inbox():
                self._status = "messaged"
            elif kinds.get("initial_sent"):
                self._status = "invited"
            elif self.sending:
                self._status = "sending" if self.sending_unacked else "invite_sent"
            elif self.queued:
                self._status = "queued"
            elif self.uncertain:
                self._status = "uncertain"
            elif self.failed or self.refused:
                self._status = "failed"
            else:
                self._status = "none"
        return self._status

    def matches(self, show, start=None, end=None):
        if show == "replied":
            return bool(self.kinds.get("inbound"))
        if show == "replied_today":
            return self.last_inbound is not None and start is not None and start <= self.last_inbound < end
        if show == "waiting":
            return self.status() in ("invited", "sending", "invite_sent", "queued")
        if show == "sending":
            return self.status() == "sending"   # the "Sending…" badge (owner 2026-10-24: counts by label)
        if show == "referral_agreed":
            return bool(self.referral and self.referral[0] == "AGREED")
        if show == "needs_you":
            return self.notes > 0 or self.refused > 0
        if show == "closed":
            return self.status() in ("closed", "opted_out")
        return True


def contacts(reader):
    """Every listed contact, newest activity first, with the open notes and refused cards by identity."""
    everyone, notes, refused = people(reader)
    found = [Contact(members, everyone, reader) for members in groups(reader, everyone)]
    found = [contact for contact in found if contact.listed]
    oldest = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    found.sort(key=lambda contact: (contact.last or oldest, contact.ids), reverse=True)
    return found, notes, refused


def replied_between(reader, start, end):
    """How many people this page lists whose latest message to the user falls in [start, end): Overview's
    "Replies received" (the same people the Replied today filter lists)."""
    return overview_counts(reader, start, end)["replies"]


def overview_counts(reader, start, end):
    """Overview's Outreach numbers that are lists on this page, counted by the same per-contact
    `matches` the chips use: "replies" (Replied today) and "sending" (contacts badged "Sending…")."""
    found = contacts(reader)[0]
    return {"replies": sum(1 for contact in found if contact.matches("replied_today", start, end)),
            "sending": sum(1 for contact in found if contact.matches("sending"))}


def _search_texts(reader):
    """{identity: [name, company, ...]} for search: outreach_actions, rw_contacts and the research drafts."""
    texts = {}

    def add(identity, *values):
        key = _key(identity)
        if key is not None:
            texts.setdefault(key, []).extend(value.casefold() for value in values if isinstance(value, str))
    for identity, recipient, company in reader.rows(
            "SELECT identity,json_extract(payload,'$.recipient'),json_extract(payload,'$.company') FROM outreach_actions "
            "WHERE json_valid(payload)", (), "outreach_actions"):
        add(identity, recipient, company)
    for identity, name, company in reader.rows("SELECT identity,name,company FROM rw_contacts", (), "rw_contacts"):
        add(identity, name, company)
    for identity, recipient, company in reader.rows(
            "SELECT identity,json_extract(payload,'$.recipient'),json_extract(payload,'$.company') FROM rw_drafts "
            "WHERE json_valid(payload)", (), "rw_drafts"):
        add(identity, recipient, company)
    return texts


# --- One entry in full ---------------------------------------------------------------------------

def _actions(reader, ids):
    return {row["action_id"]: dict(row, action=_object(row["payload"]) or {}) for row in reader.rows(
        "SELECT action_id,identity,payload,state,claimed_at FROM outreach_actions WHERE identity IN (%s)" % _marks(ids),
        tuple(ids), "outreach_actions")}


def _action(reader, actions, action_id):
    if not isinstance(action_id, str) or not action_id:
        return None
    if action_id in actions:
        return actions[action_id]
    row = reader.rows("SELECT action_id,identity,payload,state,claimed_at FROM outreach_actions WHERE action_id=?",
                      (action_id,), "outreach_actions")
    if not row:
        return None
    actions[action_id] = dict(row[0], action=_object(row[0]["payload"]) or {})
    return actions[action_id]


def judge(reader, action_id):
    """The send judge's verdict on this action: the newest DECISION that is not a reconciliation or
    composition one. {"verdict", "reason", "approved"} or None."""
    if not action_id:
        return None
    for phase, record in reader.rows("SELECT phase,record FROM outreach_agent_audit WHERE action_id=? AND kind='DECISION' "
                                     "ORDER BY observed_at DESC LIMIT ?", (action_id, AUDIT_SCAN), "outreach_agent_audit"):
        if phase in SKIPPED_PHASES:
            continue
        record = _object(record) or {}
        decision = record.get("decision") if isinstance(record.get("decision"), dict) else {}
        verdict = _pages().VERDICT_WORDS.get(str(decision.get("decision") or "").upper())
        if verdict is None:
            continue
        source = record.get("decision_source") or decision.get("decision_source")
        approved = isinstance(source, str) and source.startswith("OWNER_APPROVAL")
        reason = decision.get("reason") if isinstance(decision.get("reason"), str) and not approved else None
        return {"verdict": verdict, "reason": reason, "approved": approved}
    return None


def _message_text(payload, action):
    inbox = payload.get("inbox") if isinstance(payload.get("inbox"), dict) else {}
    raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
    message = raw.get("message") if isinstance(raw.get("message"), dict) else {}
    for value in (inbox.get("text"), raw.get("text"), message.get("body"), raw.get("note")):
        if _text(value):
            return value
    for field in ("note", "message"):
        if action and _text(action.get(field)):
            return action[field]
    return None


def _sent_action_id(payload):
    source_id = payload.get("source_id")
    if isinstance(source_id, str) and source_id.startswith("invite:"):
        return source_id.split(":", 1)[1]
    if isinstance(payload.get("preparation_action_id"), str):
        return payload["preparation_action_id"]
    raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
    return raw.get("action_id") if isinstance(raw.get("action_id"), str) else None


def _reconciled(reader, source, source_id):
    return bool(reader.rows("SELECT 1 FROM engagement_outbound_reconciliations WHERE source=? AND source_id=?",
                            (source, source_id), "engagement_outbound_reconciliations"))


def conversation(reader, ids, actions):
    """Every message of the conversation, oldest first: ours (sent, sending, failed) and theirs."""
    items, ours = [], []
    followups = 0
    rows = reader.rows("SELECT source,source_id,kind,occurred_at,payload FROM engagement_events WHERE identity IN (%s) "
                       "AND kind IN (%s) ORDER BY occurred_at,source,source_id" % (_marks(ids), _marks(KINDS)),
                       (*ids, *KINDS), "engagement_events")
    observed = []
    for source, source_id, kind, occurred_at, text in rows:
        payload = _object(text)
        if payload is None:
            continue
        at = overview._utc(occurred_at)
        if kind == "outbound_observed" and payload.get("purpose") != "REPLY":
            observed.append((source, source_id, at, payload))
            continue
        item = {"at": at, "who": "them" if kind in ("inbound", "accepted", "opt_out") else "you", "text": None,
                "state": None, "judge": None}
        if kind == "accepted":
            item["label"] = "Accepted your invitation"
        elif kind == "opt_out":
            item["label"] = "Asked not to be contacted"
        elif kind == "inbound":
            item.update(label="Their message", text=_message_text(payload, None))
        else:
            sent = _action(reader, actions, _sent_action_id(payload))
            action = sent["action"] if sent else {}
            invitation = kind == "initial_sent" and overview._invitation(kind, payload)
            if kind == "followup_sent":
                followups += 1
                item["label"] = "Follow-up %d" % followups
            elif invitation:
                item["label"] = "Invitation note"
            elif kind == "initial_sent":
                purpose = payload.get("purpose") if isinstance(payload.get("purpose"), str) else action.get("purpose")
                item["label"] = _word(PURPOSE_WORDS, purpose, "Message")
            else:
                item["label"] = "Your reply"
            item.update(text=_message_text(payload, action), state="Sent", judge=judge(reader, sent["action_id"] if sent else None))
            if invitation and not item["text"]:
                item["label"] = "Invitation (no note)"
            ours.append(item)
        items.append(item)
    for source, source_id, at, payload in observed:
        text = _message_text(payload, None)
        if _reconciled(reader, source, source_id) or (text and any(
                item["text"] == text and item["at"] and at and abs(item["at"] - at) <= engagement.MESSAGE_TIME_DRIFT
                for item in ours)):
            continue
        items.append({"at": at, "who": "you", "label": SEEN_IN_INBOX, "text": text, "state": None, "judge": None})
    for row in actions.values():
        action, state = row["action"], row["state"]
        purpose = action.get("purpose")
        if purpose not in MESSAGE_PURPOSES or state not in SENDING + UNCERTAIN + NOT_SENT:
            continue
        words = SENDING_WORDS if state in SENDING else UNCERTAIN_WORDS if state in UNCERTAIN else \
            NOT_SENT_WORDS.get(purpose if purpose == "INVITE" else None)
        items.append({"at": overview._utc(row["claimed_at"]) if row["claimed_at"] else None, "who": "you",
                      "label": _word(PURPOSE_WORDS, purpose, "Message"),
                      "text": _text(action.get("note")) or _text(action.get("message")), "state": words,
                      "pending": state in SENDING, "judge": judge(reader, row["action_id"])})
    for identity, state, claimed_at in reader.rows(
            "SELECT identity,state,claimed_at FROM outreach_attempts WHERE identity IN (%s) AND state IN (%s)"
            % (_marks(ids), _marks(CAMPAIGN_UNSETTLED)), (*ids, *CAMPAIGN_UNSETTLED), "outreach_attempts"):
        if not any(item.get("label") in ("Invitation note", "Invitation (no note)") for item in items):
            items.append({"at": overview._utc(claimed_at) if claimed_at else None, "who": "you", "label": "Invitation",
                          "text": None, "state": UNCERTAIN_WORDS, "pending": False, "judge": None})
    oldest = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    items.sort(key=lambda item: item["at"] or oldest)
    return items


def referral(reader, ids):
    rows = reader.rows("SELECT state,statement,next_action,review_at,updated_at FROM referrals WHERE contact_id IN (%s) "
                       "ORDER BY updated_at DESC LIMIT 1" % _marks(ids), tuple(ids), "referrals")
    if not rows:
        return None
    state, statement, next_action, review_at, updated_at = rows[0]
    words, tone = REFERRAL_WORDS.get(str(state or "").upper(), ("Referral in progress", "info"))
    return {"words": words, "tone": tone, "statement": _text(statement), "next": _text(next_action),
            "review": overview._utc(review_at) if review_at else None, "at": overview._utc(updated_at) if updated_at else None}


def _who(reader, ids, actions):
    import outreach
    pages = _pages()
    database = reader.database
    primary = next((identity for identity in ids if not identity.split("/in/", 1)[-1].startswith(outreach.MEMBER_ID_PREFIX)),
                   ids[0])
    research = [key for key in dict.fromkeys(_research_key(identity) for identity in ids) if key]
    lookups = list(dict.fromkeys(ids + research))     # orchestrator's spelling and research's
    name = None
    for identity in [primary] + [other for other in ids if other != primary]:
        name = pages.person_name(database, _research_key(identity) or "https://" + identity)
        if name:
            break
    company = next((row["action"].get("company") for row in actions.values() if _text(row["action"].get("company"))), None)
    title, why = None, []
    sources = [row["action"].get("source") for row in sorted(actions.values(), key=lambda row: str(row["claimed_at"] or ""),
                                                            reverse=True) if isinstance(row["action"].get("source"), dict)]
    drafts = reader.rows("SELECT identity,version,payload FROM rw_drafts WHERE identity IN (%s) ORDER BY version DESC"
                         % _marks(lookups), tuple(lookups), "rw_drafts")
    sources += [{"kind": approvals.RESEARCH_SOURCE, "identity": row[0], "version": row[1]} for row in drafts]
    for source in sources:
        title, why = pages.research_reason(database, {"source": source})
        if title or why:
            break
    for row in drafts:
        draft = _object(row[2]) or {}
        company = company or _text(draft.get("company"))
        name = name or _text(draft.get("recipient"))
    for found_name, found_company in reader.rows("SELECT name,company FROM rw_contacts WHERE identity IN (%s)"
                                                 % _marks(lookups), tuple(lookups), "rw_contacts"):
        name = name or _text(found_name)
        company = company or _text(found_company)
    return {"name": name or "Someone on LinkedIn", "company": _text(company), "title": title,
            "role": role_line(title, company), "why": why or [],
            "linkedin": pages.linkedin_url("https://" + primary)}


def _whole_prefix(text, prefix):
    """`prefix` starts `text` and ends at a word boundary (case aside)."""
    text, prefix = text.casefold(), prefix.casefold()
    return text.startswith(prefix) and (len(text) == len(prefix) or not text[len(prefix)].isalnum())


def role_line(title, company):
    """"Title at Company" when the title is known, "Works at Company" when only the company is (owner,
    2026-10-24, N1: research often records the employer itself as the person's employment, which
    showed "Upwind · Upwind"), the title alone when no company is on record, else None. A title that
    repeats the company ("Together AI founder and CEO", "Co-Founder and CTO, Cognition") loses it."""
    title, company = _text(title), _text(company)
    if title and company:
        if _whole_prefix(company, title):
            title = None                        # "InsForge" for "InsForge (InstaCloud)"
        else:
            if _whole_prefix(title, company):
                title = title[len(company):]
            for tail in (", " + company, " at " + company, " @ " + company):
                if title.casefold().endswith(tail.casefold()):
                    title = title[:-len(tail)]
            title = title.strip(" ,·-") or None
    if title and title[0].islower():
        title = title[0].upper() + title[1:]
    if title and company:
        return title + " at " + company
    if company:
        return "Works at " + company
    return title


def note_view(note):
    return {"dom": "netnote-" + _digest(note["note_id"]), "note_id": note["note_id"], "reason": note["reason"],
            "at": overview._utc(note["flagged_at"])}


def _digest(key):
    import hashlib   # approvals_page.dom's form, so ids match without importing the page
    return hashlib.sha1(str(key).encode()).hexdigest()[:16]


def sequence_line(reader, contact, now, zone):
    """The contact's place in the outreach sequence ("Invited 10-21 · Accepted 10-22 · Follow-up due
    10-27"), from `sequence.sequence_view` of each of the contact's spellings merged (one entry per
    person); None when nothing is on record or the records cannot be read (the rest of the entry still
    shows)."""
    import sequence
    try:
        views = [sequence.sequence_view(reader.database, identity, now) for identity in contact.ids]
    except Exception:
        return None
    steps, seen = [], set()
    for step in sorted((step for view in views for step in view["steps"]), key=lambda step: str(step.get("at") or "")):
        key = (step["label"], step.get("at")) if step["label"] == "Follow-up" else step["label"]
        if key in seen:   # spellings joined by rw_aliases read the same events
            continue
        seen.add(key)
        steps.append(step)
    due = next((view["due"] for view in views if view.get("due")), None)
    stage = next((view["stage"] for view in views if view["stage"].startswith("INVITE_")), None)
    return sequence.position({"steps": steps, "due": due, "stage": stage}, zone) or None


# When a queued invitation goes (owner, 2026-10-24, N3): the next invitation step (`overview_page.next_run`,
# which knows a pause, a stopped scheduler and research's turn: the hourly round or, on a Unipile install,
# the 15-minute invitation tick at :02:30/:17:30/:32:30/:47:30, `scheduler_loop.INVITE_JOB`) at or after
# the time pacing next allows an invitation (`pacing.check`, the sender's own rule). Nothing is promised
# that is not computed.
HELD_WORDS = {"Paused": "on hold while Sidedoor is paused",
              "Not running": "goes out once Sidedoor is running again",
              "Waiting to move your data": "goes out once your data has moved",
              "after today's research": "goes out after today's research"}


def queued_timing(reader, config, now, zone, running=True):
    """The words after "Invitation queued · ", or None when they cannot be worked out."""
    import pacing
    fmt = lambda moment: overview.when(moment, now, zone)
    try:
        upcoming = overview.next_run(reader, "run", config, now, {"running": running}, overview.pause_flags(reader))
    except Exception:
        return None
    if "label" in upcoming:
        return HELD_WORDS.get(upcoming["label"])
    if reader.database is None:
        return None
    try:
        pace = pacing.check(reader.database, config, now)
    except Exception:
        return None
    at = upcoming["at"]
    if not pace["go"]:
        until = overview._utc(pace["wait_until"])
        if until is None:
            return None
        if pace["reason"] == "DAILY_CAP":
            return "after today's limit resets " + fmt(until)
        if pace["reason"] == "WEEKLY_CAP":
            return "after this week's limit resets " + fmt(until)
        if until > at:
            at = next_invitation_step(until, config)
    return "goes out around " + fmt(at)


def next_invitation_step(moment, config):
    """The first instant at or after `moment` that sends invitations: the hourly round, or on a Unipile
    install the 15-minute invitation tick (`scheduler_loop.jobs`), whichever comes first."""
    import scheduler_loop
    instants, periods = scheduler_loop.schedule(moment, config)
    names = ["run"] + ([scheduler_loop.INVITE_JOB] if scheduler_loop.INVITE_JOB in scheduler_loop.jobs(config) else [])
    found = []
    for name in names:
        start = dt.datetime.fromtimestamp(instants[name], dt.timezone.utc)
        found.append(start if start >= moment else start + dt.timedelta(seconds=periods[name]))
    return min(found)


def queued_line(timing, queued):
    if timing is None:
        return None
    if queued > 1 and timing.startswith("goes out around "):
        return "Invitation queued · the next of %d queued invitations %s" % (queued, timing)
    return "Invitation queued · " + timing


def entry(reader, contact, notes, refused, now=None, zone=None, queued=None):
    pages = _pages()
    actions = _actions(reader, contact.ids)
    status, tone = STATUS[contact.status()]
    found = dict(_who(reader, contact.ids, actions), status=status, tone=tone, last=contact.last,
                 conversation=conversation(reader, contact.ids, actions), referral=referral(reader, contact.ids),
                 notes=[note_view(note) for identity, note in notes.values() if identity in contact.ids],
                 dom="contact-" + _digest(contact.ids[0]),
                 sequence=sequence_line(reader, contact, now, zone or ZoneInfo("America/Los_Angeles")))
    if contact.status() == "queued":
        found["sequence"] = queued   # when it goes, when that is computed; else the chip says it all
    elif found["sequence"] == status:
        found["sequence"] = None
    found["refused"] = [{"kind": _word(pages.PURPOSE_WORDS, item["purpose"], "Message"),
                         "text": item["message"] if isinstance(item["message"], str) else None,
                         "reasons": list(dict.fromkeys(pages.GATE_WORDS.get(gate, pages.GENERIC_GATE)
                                                       for gate in item["gates"])) or [pages.GENERIC_GATE]}
                        for identity, item in refused.values() if identity in contact.ids]
    return found


# --- The page ------------------------------------------------------------------------------------

def listing(reader, show, page, query, start, end, now=None, zone=None, config=None, running=True):
    found, notes, refused = contacts(reader)
    waiting = sum(1 for contact in found if contact.status() == "queued")
    if query:
        texts, needle = _search_texts(reader), query.casefold()
        found = [contact for contact in found if any(
            needle in identity.split("/in/", 1)[-1] or any(needle in text for text in texts.get(identity, ()))
            for identity in contact.ids)]
    counts = {key: sum(1 for contact in found if contact.matches(key, start, end)) for key in FILTER_KEYS}
    chosen = [contact for contact in found if contact.matches(show, start, end)]
    pages = max(1, math.ceil(len(chosen) / PAGE_SIZE))
    page = min(max(1, page), pages)
    window = chosen[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]
    zone = zone or ZoneInfo("America/Los_Angeles")
    queued = queued_line(queued_timing(reader, config or {}, now or dt.datetime.now(dt.timezone.utc), zone, running),
                         waiting) if any(contact.status() == "queued" for contact in window) else None
    with engagement.aliases_read_once(reader.database, getattr(reader, "alias_rows", None)):   # sequence_view
        shown = [entry(reader, contact, notes, refused, now, zone, queued) for contact in window]
    return {"contacts": shown, "total": len(chosen),
            "page": page, "pages": pages, "counts": counts}


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 1


def gather(database, config, show="all", page=1, query=None, now=None, running=True):
    """Everything the page shows. `database` is a read-only connection or None (no file yet)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    zone_name = config.get("timezone") or "America/Los_Angeles"
    zone = ZoneInfo(zone_name)
    hour = workflow_tasks.day_start_hour(config)
    start = workflow_tasks.service_day_start(now, zone_name, hour).astimezone(dt.timezone.utc)
    end = workflow_tasks.next_day_start(now, zone_name, hour).astimezone(dt.timezone.utc)
    show = show if show in FILTER_KEYS else "all"
    query = _text(query)
    query = query[:200] if query else None
    reader = overview.Reader(database)

    def link(**changes):
        values = dict({"show": show, "q": query}, **changes)
        pairs = [(name, value) for name, value in values.items() if value and not (name == "show" and value == "all")
                 and not (name == "page" and value == 1)]
        return "/sent" + ("?" + urllib.parse.urlencode(pairs) if pairs else "")

    return {"show": show, "query": query, "filters": FILTERS, "link": link, "unavailable": UNAVAILABLE,
            "unreadable": reader.broken, "list": overview.section(reader, listing, show, _int(page), query, start, end, now, zone,
                                                                       config, running),
            "when": lambda moment: overview.when(moment, now, zone) if moment else None}


def safe_back(value):
    """The page to return to after [Mark done]: this page with its own filter, search and page only."""
    if not isinstance(value, str) or not value.startswith("/sent") or "//" in value or "\\" in value or "#" in value:
        return "/sent"
    parts = urllib.parse.urlsplit(value)
    if parts.path != "/sent":
        return "/sent"
    query = urllib.parse.parse_qs(parts.query)
    kept = [(name, query[name][0]) for name in ("show", "q", "page") if query.get(name)]
    return "/sent" + ("?" + urllib.parse.urlencode(kept) if kept else "")


def register(app, templates, layout, *, home, root, config):
    """The page's routes (the dashboard packages are imported here, so `gather` runs without them)."""
    from fastapi import Request
    from fastapi.responses import HTMLResponse, RedirectResponse
    from starlette.concurrency import run_in_threadpool

    import dashboard

    def read(use):
        return overview.snapshot(paths.database(root), use, lambda: dashboard.read_connection(root))

    def view(request, show="all", page="1", q="", flash=None):
        context = layout("sent", "Networking & Referrals")
        import setup_steps
        running = setup_steps.scheduler_running(home)
        context["view"] = read(lambda database: gather(database, config, show, page, q, running=running))
        context["flash"] = flash
        return templates.TemplateResponse(request, "networking.html", context)

    @app.get("/sent", name="sent")
    def networking(request: Request, show: str = "all", page: str = "1", q: str = "", noted: str = ""):
        tone_text = NOTED.get(noted)
        return view(request, show, page, q, {"tone": tone_text[0], "text": tone_text[1]} if tone_text else None)

    def write(change):
        """`change(database)` on a write connection; None when the database stayed locked."""
        paths.state(root).mkdir(exist_ok=True, mode=0o700)
        try:
            database = dashboard.write_connection(root)
            try:
                return change(database)
            finally:
                database.close()
        except sqlite3.OperationalError as error:
            if "locked" in str(error) or "busy" in str(error):
                return None
            raise

    @app.post("/sent/notes/done")
    async def note_done(request: Request):
        """[Mark done] on a contact's note: the same `reply_tools.mark_done` as Approvals."""
        import reply_tools
        form = await request.form()
        note_id = form.get("note_id") or ""
        back = safe_back(form.get("back"))
        anchor = form.get("anchor") if isinstance(form.get("anchor"), str) and DOM.match(form.get("anchor")) else None
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        changed = await run_in_threadpool(write, lambda database: reply_tools.mark_done(database, note_id, now))
        htmx = request.headers.get("hx-request") == "true"
        dom = "netnote-" + _digest(note_id)
        if changed is None:
            flash = {"tone": "error", "text": BUSY}
            if htmx:
                found = read(lambda database: [note for note in reply_tools.open_flags(database)
                                               if str(note["note_id"]) == str(note_id)] if database is not None else [])
                if found:
                    return HTMLResponse(templates.get_template("networking_note.html").render(
                        n=note_view(found[0]), f=flash, back=back, anchor=anchor,
                        v={"when": gather(None, config)["when"]}))
                return HTMLResponse(templates.get_template("approvals_done.html").render(dom=dom, f=flash))
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(back).query)
            first = lambda name, default: query.get(name, [default])[0]
            return view(request, first("show", "all"), first("page", "1"), first("q", ""), flash)
        if htmx:
            return HTMLResponse(templates.get_template("approvals_done.html").render(
                dom=dom, f={"tone": "success", "text": "Marked done." if changed else "That note was already done."}))
        target = back + ("&" if "?" in back else "?") + "noted=" + ("1" if changed else "0")
        return RedirectResponse(target + ("#" + anchor if anchor else ""), status_code=303)
