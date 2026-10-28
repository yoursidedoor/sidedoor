"""Source-bound contextual replies through the existing durable outreach sender."""

import resources
import datetime as dt
import hashlib
import json
from pathlib import Path

import champion
import champion_composition
import engagement
import heyreach_schema
import linkedin_port
import outreach
import owner_seed
import outreach_agent
import outreach_provider
import pipeline_routing as routing
import reply_resume
import reply_worker
from orchestrator import name_key, profile_key


# Owner, 2026-09-30, about the replies drafted for the inbound messages already on file (20
# messages dated 2026-02-23 to 2026-09-19): "dont send these replies". Any reply decision about an
# inbound message that occurred before this release is never promoted or sent. Messages that
# arrive after it are answered.
BACKLOG_CUTOFF = dt.datetime(2026, 9, 30, 18, 0, tzinfo=dt.timezone.utc)
BACKLOG_BLOCKER = "REPLY_BACKLOG_BEFORE_2026_09_30_NOT_SENT"

# Provider errors on a reply send (owner, 2026-09-30: "retry on errors also. max 3 attempts with
# backoff"). The first attempt is the ordinary claimed send; each wait below follows a failed
# attempt, and the last one is how long to wait before the final HeyReach check that closes it.
MAX_SEND_ATTEMPTS = 3
RETRY_DELAYS = (dt.timedelta(minutes=10), dt.timedelta(hours=1), dt.timedelta(hours=1))
TERMINAL_FAILURE = "REPLY_SEND_FAILED_AFTER_3_ATTEMPTS"
# A thread that cannot be read completely is never taken as "not sent"; re-read with backoff. After
# this many unreadable checks in a row the reply is never sent again (reply_no_more_sends), but it
# stays in read-only reconciliation until a complete read settles it.
MAX_UNREADABLE_CHECKS = 6
UNREADABLE_CHECK_DELAY = dt.timedelta(minutes=10)
MAX_UNREADABLE_CHECK_DELAY = dt.timedelta(hours=12)

# The fields that tie a reply action to one account, thread, person and inbound message.
THREAD_BINDING_FIELDS = ("action_id", "purpose", "provider", "channel", "profile", "sender_id", "conversation_id",
                         "reply_task_id", "reply_source_key", "reply_source_sha256", "reply_source",
                         "reply_source_id", "reply_inbound_at", "resolved_profile")


def backlog(inbound_at):
    """True for an inbound message from before the 2026-09-30 release (owner: never answered).
    An unreadable date is treated as backlog: it can't be shown to be after the cutoff."""
    try:
        # HeyReach's createdAt drifts forward by seconds between reads, so a backlog message can
        # be recorded just after the cutoff; the drift window counts as backlog too.
        return engagement.timestamp(inbound_at) < BACKLOG_CUTOFF + engagement.MESSAGE_TIME_DRIFT
    except (ValueError, TypeError, AttributeError):
        return True


def without_dashes(text):
    """The outreach composer's em/en dash normalisation, keeping the reply's line breaks."""
    import outreach_promotion

    if not isinstance(text, str) or not any(character in text for character in outreach_promotion.DASH_CHARACTERS):
        return text
    return "\n".join(outreach_promotion.without_dashes(line) for line in text.split("\n"))


def _config(config):
    policy = dict(config, pipeline_routing=dict(config.get("pipeline_routing", {})))
    policy["pipeline_routing"]["account_ids"] = [config["sender_id"]]
    return policy


def context(database, config, task_id, now):
    result = routing.one(database, "SELECT * FROM reply_worker_results WHERE task_id=?", (task_id,))
    task = routing.one(database, "SELECT * FROM workflow_tasks WHERE task_id=?", (task_id,))
    if not result or not task or task["status"] != "COMPLETE":
        raise ValueError("REPLY_COMPLETE_DECISION_REQUIRED")
    handoff = json.loads(result["handoff"])
    source = routing.one(database, "SELECT * FROM pipeline_routing_sources WHERE source_key=?", (handoff["source_key"],))
    consumed = json.loads(source["result"] or "{}").get("reply_decisions", {}) if source else {}
    if consumed.get(task_id) != result["output_sha256"]:
        raise ValueError("REPLY_UNCONSUMED_DECISION")
    with engagement.transaction(database):
        routing.apply_reply_decision(database, task_id, _config(config), now.isoformat())
    saved = routing.one(database, "SELECT input FROM reply_worker_inputs WHERE task_id=?", (task_id,))
    request = json.loads(saved["input"].partition("\n\nUNTRUSTED_SOURCE_JSON:\n")[2])
    return request, handoff, source


def _recipient(database, profile):
    """Resolve the recipient's human name, or hold the send for investigation.

    The previous fallback substituted the profile key for the name. That value
    keys nothing in the ``name:`` suppression space, so every name-keyed
    suppression and both hardcoded exclusion sets missed at once while the send
    proceeded. The name is now resolved from every durable profile-anchored
    record. When it cannot be resolved the send fails CLOSED unconditionally:
    it waits behind an explicit blocker for AI investigation rather than
    proceeding on a name-shaped non-name. This is the blanket form of the gate.
    An unresolvable recipient is unknown history, and AGENTS.md requires that
    "Unknown history triggers AI investigation while the affected send waits" —
    not only when the database happens to hold a name-only exclusion today. The
    conditional form makes safety depend on the current contents of the
    suppression table, so a suppression row added, bound or removed later
    silently changes whether an identical send proceeds; worse, it is vacuously
    open on a database whose name-only exclusions are all profile-bound, which
    is exactly the state a future identity reconciliation produces.
    """
    identities = engagement.identity_keys(database, profile)
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "history" in tables:
        for row in database.execute("SELECT recipient,profile FROM history WHERE profile IS NOT NULL"):
            if profile_key(row["profile"]) in identities and not outreach.unresolved_recipient_name(row["recipient"]):
                return row["recipient"]
    resolved = sorted(outreach.recipient_names(database, identities))
    if len(resolved) == 1:
        return resolved[0]
    if resolved:
        raise ValueError("REPLY_RECIPIENT_IDENTITY_AMBIGUOUS")
    # No local human name (owner, 2026-09-30: replies to managed contacts "MUST be sent ... no
    # stupidity is blocking them"). Every person the owner messaged by hand is in this state. The
    # name is taken from HeyReach instead: the inspection reads this thread's correspondent and
    # returns it as proof.recipient_name, and outreach.action_gates / stop_gates check it against
    # owner_seed.excluded_names(), every name-keyed suppression and the opt-out records exactly as a
    # local name. With no HeyReach name, or two HeyReach names that disagree, the send still
    # holds (RECIPIENT_IDENTITY_UNRESOLVED / REPLY_RECIPIENT_NAME_CONFLICT). The profile stands
    # in as the recipient until then; it is never treated as a name.
    return profile


def sender_keys(snapshot, inbox=None):
    """The inbound sender's HeyReach lead (member) id and conversation id, from its recorded raw
    observation, for outreach.resolve_sender."""
    raw = (snapshot or {}).get("raw") or {}
    try:
        raw = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    inbox = inbox if isinstance(inbox, dict) else {}
    lead = raw.get("provider_lead_id") or raw.get("correspondent_id") or inbox.get("provider_lead_id")
    return lead, raw.get("conversation_id") or inbox.get("conversation_id")


def _action(database, config, task_id, now):
    request, handoff, source = context(database, config, task_id, now)
    snapshot = request["source_snapshot"]
    inbox = json.loads(snapshot["payload"])["inbox"]
    decision = handoff["classification"]
    # Narrowed to match reply_worker (owner, 2026-09-23). `can_advance_conversion` used to
    # require an empty hold list, so an unscoped acknowledgment never reached this guard. Now
    # that a definite reading advances on its own, this would fire on conversations it never
    # touched before and stop the reply - the exact thing this round exists to prevent. It asks
    # for a summary only when the packet actually carries the role evidence one needs.
    summarisable = any((scope or {}).get("role_context", {}).get("evidence_id")
                       for scope in (request.get("job_scope") or []) if isinstance(scope, dict))
    if (config.get("conversion", {}).get("champion_handoff_enabled") is True
            and decision["label"] == "AGREEMENT" and handoff.get("can_advance_conversion") is True
            and decision.get("champion_handoff") is None and summarisable):
        raise ValueError("CHAMPION_SUMMARY_REQUIRED_BEFORE_REPLY_SLOT")
    # The conversation must be one the harness manages (owner, 2026-09-23). `engagement`
    # ingests the whole authenticated inbox, so without this a stranger's message is a
    # candidate for an automatic reply - and live data showed exactly that shape: 8 people with
    # inbound messages, none of them contacted by the harness, and the single reply ever sent
    # went to one of them. HARNESS_OUTREACH is derived from our own send records;
    # OWNER_CONTACTED is a recorded row for people the owner messaged by hand. Owner, 2026-10-01:
    # "People I've answered by hand who are also being tracked in the harness can get auto
    # replies in the future" - a tracked person whose thread shows our side's message is managed
    # too (TRACKED_OWNER_ANSWERED). A cold stranger is not, answered by hand or not.
    lead_id, thread = sender_keys(snapshot, inbox)
    action_id = "reply:" + routing.digest([inbox["account_id"], inbox["conversation_id"], snapshot["source"], snapshot["source_id"]])
    # The tracked person a member-id spelling resolves to (outreach.resolve_sender): their
    # opt-outs, closure and exclusions stop this reply too (stop_gates). It is fixed when the
    # reply is promoted and read back from the queued action afterwards, never derived again
    # (review, 2026-10-01: a re-derived value differed after promotion and the reply never sent).
    queued = database.execute("SELECT payload FROM outreach_actions WHERE action_id=?", (action_id,)).fetchone() \
        if database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='outreach_actions'").fetchone() else None
    if queued:
        pinned = json.loads(queued[0]).get("resolved_profile")
        resolved = outreach._identity_of(pinned) if pinned else None
        basis = (outreach.person_basis(database, inbox.get("sender_profile"), resolved) if resolved
                 else outreach.reply_conversation(database, inbox.get("sender_profile"), lead_id, thread))
    else:
        basis = outreach.reply_conversation(database, inbox.get("sender_profile"), lead_id, thread)
        resolved = outreach.resolve_sender(database, inbox.get("sender_profile"), lead_id, thread).get("person")
    if not basis:
        raise ValueError("REPLY_CONVERSATION_NOT_MANAGED")
    proposal = decision.get("reply_proposal")
    # Any label (owner, 2026-09-30): a decision with a written reply proposal is answered whatever
    # it is labelled, INTERVIEW, DECLINE and REJECTION included. The label list that stood here
    # left an interview invitation with a drafted answer unanswered. No proposal, no reply.
    if not proposal or not isinstance(proposal.get("text"), str) or not proposal["text"].strip():
        raise ValueError("REPLY_NO_ACTIONABLE_PROPOSAL")
    if request.get("context_issues"):
        raise ValueError("REPLY_CONTEXT_CONFLICT")
    profile = inbox["sender_profile"]
    recipient = _recipient(database, profile)
    attempts = routing.rows(database, "SELECT observed_at FROM reply_worker_attempts WHERE task_id=? AND output_sha256=?",
                            (task_id, handoff["output_sha256"]))
    employers = {scope.get("employer") for scope in request.get("job_scope") or [] if isinstance(scope, dict)}
    # A message read from the Unipile inbox is answered over the port (E3.T3), the same way.
    provider = engagement.UNIPILE if snapshot["source"] == engagement.UNIPILE else "heyreach"
    action = {"action_id": action_id, "purpose": "REPLY", "provider": provider, "channel": "linkedin",
            "profile": profile, "recipient": recipient,
            "company": employers.pop() if len(employers) == 1 and None not in employers else "Unassociated existing conversation",
            "sender_id": inbox["account_id"], "conversation_id": inbox["conversation_id"],
            # No em or en dash goes out (owner, 2026-09-30), normalised as outreach notes are.
            "message": without_dashes(proposal["text"]), "subject": "",
            "reply_task_id": task_id, "reply_source_key": source["source_key"],
            "reply_source_sha256": source["content_sha256"], "reply_source": snapshot["source"],
            "reply_source_id": snapshot["source_id"], "reply_inbound_at": snapshot["occurred_at"],
            **({"resolved_profile": "https://" + resolved}
               if resolved and resolved != outreach._identity_of(profile) else {}),
            "verification": {"verified_at": attempts[0]["observed_at"],
                             "content_sources": ["reply_worker_results:" + task_id],
                             "identity_source": "engagement_events:" + source["source_id"]}}
    return champion.attach(database, config, action, request, handoff, now)


def inbound_text(database, action):
    """The exact text of the inbound message this reply answers, from its recorded event."""
    row = database.execute("SELECT payload FROM engagement_events WHERE source=? AND source_id=?",
                           (action.get("reply_source"), action.get("reply_source_id"))).fetchone()
    if not row:
        return None
    event = json.loads(row[0])
    text = ((event.get("inbox") or {}).get("text") if isinstance(event.get("inbox"), dict) else None)
    if not isinstance(text, str):
        message = (event.get("raw") or {}).get("message") if isinstance(event.get("raw"), dict) else None
        text = message.get("body") if isinstance(message, dict) else None
    return text if isinstance(text, str) else None


def _same_inbound(action, text, body, occurred, conversation_id=None):
    """A drifted re-read of the inbound message this reply answers (owner, 2026-09-30, "Inbox
    reading"): HeyReach moves a message's createdAt by seconds between reads, so the same
    conversation, the person's exact text and a time within engagement.MESSAGE_TIME_DRIFT is the
    same message, never a newer one. A different text, or a time outside the window, is newer."""
    return (text is not None and body == text
            and conversation_id in (None, action.get("conversation_id"))
            and engagement.same_instant(occurred, action["reply_inbound_at"]))


def conversation_events(database, action):
    """Recorded events that bear on whether this reply's inbound message is still the one to
    answer: the sender's, the tracked person's a member-id spelling resolved to (resolved_profile),
    and any event in the same conversation, whoever it is filed under (review, 2026-10-01)."""
    events, seen = [], set()

    def add(event):
        key = (event.get("source"), event.get("source_id"))
        if key not in seen:
            seen.add(key)
            events.append(event)

    for person in (action.get("profile"), action.get("resolved_profile")):
        if person:
            for event in engagement.events_for(database, person):
                add(event)
    if action.get("conversation_id"):
        for (payload,) in database.execute("""SELECT payload FROM engagement_events WHERE json_valid(raw)
                AND json_extract(raw,'$.conversation_id')=? ORDER BY occurred_at,source,source_id""",
                                           (action["conversation_id"],)):
            add(json.loads(payload))
    return sorted(events, key=lambda event: (str(event.get("occurred_at")), str(event.get("source")),
                                             str(event.get("source_id"))))


# Messages from our side that answer an inbound message when sent after it. Our own invitation
# (an INITIAL initial_sent, or an outbound_observed that is that invitation) never answers one;
# a first direct message (outreach records a FIRST_MESSAGE send as initial_sent too) does.
OUR_ANSWER_KINDS = ("outbound_observed", "followup_sent", "initial_sent")


def _send_time(event):
    """When a message was sent: the provider's message time where recorded, else the event time.
    An invitation's initial_sent occurred_at is when reconciliation observed the send (one
    contact: invitation 14:04, initial_sent 17:21), which is why the invitation never judges "answered"."""
    raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
    message = raw.get("message") if isinstance(raw.get("message"), dict) else {}
    return engagement.timestamp(message.get("createdAt") or event["occurred_at"])


def _invitation_send(event):
    """Is this initial_sent the invitation (purpose INITIAL, or a Unipile INVITE), not a first direct
    message? One recorded without a purpose is the invitation when it carries its campaign."""
    if event.get("kind") != "initial_sent":
        return False
    purpose = event.get("purpose")
    if purpose is not None:
        return purpose in {"INITIAL", "INVITE"}
    raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
    return isinstance(raw.get("campaign"), dict)


def _our_invitation(database, event):
    """Is this message of ours the invitation (connection note) itself?"""
    if event.get("kind") == "initial_sent":
        return _invitation_send(event)
    inbox = event.get("inbox") if isinstance(event.get("inbox"), dict) else {}
    if inbox.get("message_type") == "invitation":
        return True
    if not database.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                            "AND name='engagement_outbound_reconciliations'").fetchone():
        return False
    row = database.execute("SELECT matched_source FROM engagement_outbound_reconciliations WHERE source=? AND source_id=?",
                           (event.get("source"), event.get("source_id"))).fetchone()
    if not row:
        return False
    if "campaign-invitation:" in row[0]:
        return True
    try:
        source, source_id = json.loads(row[0].partition(":")[2])
    except (ValueError, TypeError):
        return False
    matched = database.execute("SELECT payload FROM engagement_events WHERE source=? AND source_id=?",
                               (source, source_id)).fetchone()
    try:
        return bool(matched) and _invitation_send(json.loads(matched[0]))
    except (ValueError, TypeError):
        return False


def newer_activity(database, action):
    """REPLY_NEWER_CONVERSATION_ACTIVITY when this reply's inbound message is no longer the one to
    answer, else None. action needs profile (and resolved_profile), conversation_id,
    reply_inbound_at, reply_source and reply_source_id.

    A newer inbound message gets its own decision and its own reply (one reply per latest inbound
    message), so this older one stands down; strictly newer, so two messages with one timestamp
    do not strand each other, and a drifted re-read of the same message is not newer. A message
    from our side sent at or after the inbound (the owner answering by hand, a first message, a
    follow-up, a reply) means it was already answered; its provider send time decides, and our own
    invitation never counts as an answer. Used by binding_gates and, before the reply AI, by
    reply_worker._reply_state (review, 2026-10-01)."""
    text = inbound_text(database, action)
    inbound_at = engagement.timestamp(action["reply_inbound_at"])
    for event in conversation_events(database, action):
        if (event.get("source"), event.get("source_id")) == (action.get("reply_source"), action.get("reply_source_id")):
            continue
        kind = event.get("kind")
        if kind == "inbound":
            occurred = engagement.timestamp(event["occurred_at"])
            raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
            message = raw.get("message") if isinstance(raw.get("message"), dict) else {}
            if _same_inbound(action, text, message.get("body"), occurred, raw.get("conversation_id")):
                continue        # the same inbound message, recorded again with a drifted time
            if occurred > inbound_at:
                return "REPLY_NEWER_CONVERSATION_ACTIVITY"
        elif kind in OUR_ANSWER_KINDS and not _our_invitation(database, event) and _send_time(event) >= inbound_at:
            return "REPLY_NEWER_CONVERSATION_ACTIVITY"
    return None


def binding_gates(database, config, action, now):
    """Is this queued reply still bound to its decision's account, thread, person and message?

    Owner, 2026-09-30. The byte-for-byte comparison of the queued action with a freshly rebuilt
    one (REPLY_ACTION_DECISION_MISMATCH on any difference) is gone, and so is the claim-time
    comparison of the application context (CHAMPION_CURRENT_CANDIDATE_OR_PIPELINE_CONTEXT_CHANGED).
    Only the fields that decide where the reply goes are compared, so a judge-requested rewrite
    of the text still sends in the same slot. Exclusions, the account and "somebody already
    answered this message" still hold.
    """
    try:
        if backlog(action.get("reply_inbound_at")):
            return [BACKLOG_BLOCKER]
        expected = _action(database, config, action["reply_task_id"], now)
        if any(action.get(field) != expected.get(field) for field in THREAD_BINDING_FIELDS):
            return ["REPLY_ACTION_DECISION_MISMATCH"]
        if config.get("sender_id") != action["sender_id"]:
            return ["REPLY_ACCOUNT_MISMATCH"]
        try:
            if routing._reply_excluded(database, action["profile"], []):
                return ["REPLY_EXCLUDED_OR_SUPPRESSED"]
        except ValueError as error:
            # No local name: the name-keyed exclusion check waits for the HeyReach name at
            # inspection (see _recipient); every profile-keyed exclusion and opt-out above ran.
            if str(error) != "REPLY_RECIPIENT_IDENTITY_UNRESOLVED":
                raise
        request, unused_handoff, unused_source = context(database, config, action["reply_task_id"], now)
        original = request["source_snapshot"]
        newer = newer_activity(database, dict(action, reply_source=original["source"],
                                              reply_source_id=original["source_id"]))
        return [newer] if newer else []
    except (ValueError, TypeError, KeyError, IndexError, reply_worker.Held) as error:
        code = str(error)
        return [code if code.startswith(("CHAMPION_", "REPLY_RECIPIENT_IDENTITY_")) else "REPLY_DECISION_BINDING_INVALID"]


def stop_gates(database, config, action, proof=None):
    """Everything that can still stop a reply right before it is sent (owner, 2026-09-30).

    "for 'right before sending' remove the fresh thread read. we dont need to reread it. and the
    other checks." What is left: an explicit opt-out or negative reply, a closed recipient, an
    excluded name, and one reply per inbound message. The thread read, its identity/restriction/
    already-answered checks, the re-run of the whole send guard, the history and content freshness
    windows and the inactivity check all ran at inspection and are not repeated here.
    """
    try:
        slot = outreach.reply_slot(action)
        people = [profile_key(action["profile"])]
        if action.get("resolved_profile"):
            # The tracked person a member-id spelling resolved to (review, 2026-10-01).
            people.append(profile_key(action["resolved_profile"]))
    except (ValueError, TypeError, KeyError):
        return ["REPLY_BINDING_INVALID"]
    gates = []
    if backlog(action.get("reply_inbound_at")):
        gates.append(BACKLOG_BLOCKER)
    for position, identity in enumerate(people):
        stop = engagement.stop_state(database, identity)
        if stop and stop != "REPLY_REQUIRES_CONTEXT":
            gates.append(stop)
        if any(event.get("kind") == "opt_out" or (event.get("inference") or {}).get("sentiment") in {"negative", "opt_out"}
               for event in engagement.events_for(database, identity)):
            gates.append("OPTED_OUT")
        identities = engagement.identity_keys(database, identity)
        extra = (action.get("recipient"), (proof or {}).get("recipient_name")) if position == 0 else ()
        names = outreach.recipient_names(database, identities, extra=extra)
        if (names & (owner_seed.excluded_names() | owner_seed.no_automatic_names())
                or set(identities) & owner_seed.no_automatic_profiles()):
            gates.append("EXCLUDED_CONTACT")
        lookup = outreach.suppression_keys(database, identities, action.get("recipient") if position == 0 else None,
                                           names=names)
        if database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='suppressions'").fetchone():
            placeholders = ",".join("?" for unused in lookup)
            if any(not outreach._reply_suppression_allowed(row[0]) for row in database.execute(
                    "SELECT reason FROM suppressions WHERE identity IN (" + placeholders + ")", tuple(lookup))):
                gates.append("SUPPRESSED_CONTACT")
    if database.execute("SELECT 1 FROM outreach_actions WHERE slot=? AND action_id<>?",
                        (slot, action["action_id"])).fetchone():
        gates.append("REPLY_SLOT_ALREADY_RESERVED")
    return sorted(set(gates))


def _our_reply(room, action, text=None):
    """Classify the thread after the inbound message: "SENT", "OTHER_ANSWER" or None.

    The retry's idempotency read (owner, 2026-09-30: "make sure the retry is idempotent by
    checking heyreach to make sure the attempt wasnt successful"). SENT is our exact reply text,
    whitespace aside, from our side after the inbound message; OTHER_ANSWER is any other message
    from our side after it, which a resend would duplicate; NEWER_INBOUND is a later message from
    the person, which gets its own decision and reply, so this one stands down. An incomplete or
    malformed thread raises: it is never evidence that our reply is absent.
    """
    if not isinstance(room, dict) or room.get("id", action["conversation_id"]) != action["conversation_id"]:
        raise ValueError("REPLY_RETRY_THREAD_MISMATCH")
    messages = room.get("messages")
    if not isinstance(messages, list):
        raise ValueError("REPLY_RETRY_THREAD_UNREADABLE")
    # A partial thread could be missing the very reply we are looking for, and a resend on that
    # basis would answer twice. Only a complete read counts; anything else is "unavailable".
    if type(room.get("totalMessages")) is not int or len(messages) != room["totalMessages"]:
        raise ValueError("REPLY_RETRY_THREAD_INCOMPLETE")
    if any(not isinstance(message, dict) or message.get("sender") not in {"ME", "CORRESPONDENT"}
           or not message.get("createdAt") for message in messages):
        raise ValueError("REPLY_RETRY_THREAD_UNREADABLE")
    inbound = engagement.timestamp(action["reply_inbound_at"])
    ours = " ".join(str(action["message"]).split())
    other = newer = None
    for message in messages:
        occurred = engagement.timestamp(message["createdAt"])
        if message["sender"] == "CORRESPONDENT":
            if occurred > inbound and not _same_inbound(action, text, message.get("body"), occurred):
                newer = newer or ("NEWER_INBOUND", message)
            continue
        if occurred < inbound:
            continue
        if " ".join(str(message.get("body") or "").split()) == ours:
            return "SENT", message
        other = other or ("OTHER_ANSWER", message)
    return other or newer or (None, None)


def _name_excluded(database, name):
    """A name-keyed suppression that a contextual reply may not ignore."""
    if not database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='suppressions'").fetchone():
        return False
    return any(not outreach._reply_suppression_allowed(row[0]) for row in
               database.execute("SELECT reason FROM suppressions WHERE identity=?", ("name:" + name,)))


def promote(database, config, now=None):
    if config.get("reply_execution", {}).get("enabled") is not True:
        return {"state": "DISABLED", "items": []}
    now = engagement.timestamp(now or dt.datetime.now(dt.timezone.utc))
    outreach.initialize_actions(database)
    reply_worker.initialize(database)
    database.commit()
    items = []
    candidates = routing.rows(database, """SELECT result.task_id,
            json_extract(source.snapshot,'$.occurred_at') AS inbound_at FROM reply_worker_results result
        JOIN workflow_tasks task ON task.task_id=result.task_id
        LEFT JOIN pipeline_routing_sources source ON source.source_key=json_extract(result.handoff,'$.source_key')
        WHERE task.status='COMPLETE'
        AND NOT EXISTS (SELECT 1 FROM outreach_actions action
            WHERE json_extract(action.payload,'$.reply_task_id')=result.task_id)
        ORDER BY task.updated_at DESC""")
    # The backlog is handled by not sending it (owner, 2026-09-30: "dont send these replies"): it is
    # reported and skipped here, so it neither sends nor takes one of the 60 places below.
    skipped = [candidate["task_id"] for candidate in candidates if backlog(candidate["inbound_at"])]
    candidates = [candidate for candidate in candidates if not backlog(candidate["inbound_at"])][:60]
    for task_id in skipped:
        items.append({"task_id": task_id, "state": "BACKLOG_NOT_SENT", "blocker": BACKLOG_BLOCKER})
    for candidate in candidates:
        task_id = candidate["task_id"]
        try:
            with engagement.transaction(database):
                action = _action(database, config, task_id, now)
                gates = binding_gates(database, config, action, now)
                if gates:
                    raise ValueError(gates[0])
                item = outreach.enqueue_action(database, action)
                request, handoff, unused_source = context(database, config, task_id, now)
                champion.persist(database, config, action, request, handoff, now)
            items.append({"task_id": task_id, **item})
        except (ValueError, TypeError, KeyError, IndexError, reply_worker.Held) as error:
            items.append({"task_id": task_id, "state": "HELD", "blocker": error.code if isinstance(error, reply_worker.Held) else str(error)})
    return {"state": "PROCESSED", "items": items}


def _unipile_room(thread):
    """A port Thread in the room shape `_our_reply` reads: complete means every message was returned."""
    messages = [engagement.port_message(thread.chat_id, message) for message in thread.messages]
    return {"id": thread.chat_id, "messages": messages, "totalMessages": len(messages) if thread.complete is True else None}


class Adapter(outreach_agent.Adapter):
    """Unipile (E3.T3, owner 2026-10-21: "build it but add no new guard or blockers"): a REPLY whose
    inbound came from the Unipile inbox (provider 'unipile') is read, sent, confirmed and retried over
    the LinkedInPort with the HeyReach path's own checks; the ones that name HeyReach-only fields
    (account id and restriction flags, thread block flags, correspondent names) have no port
    equivalent and are left out."""

    def _unipile_reply(self, action):
        return action.get("purpose") == "REPLY" and action.get("provider") == engagement.UNIPILE

    def collect(self, action, phase, claim=None, **named):
        if self._unipile_reply(action):
            import followup_execution
            return followup_execution.port_observation(self.port, action["profile"])
        return super().collect(action, phase, claim, **named)

    def judgment_context(self, action, config, now):
        if action.get("purpose") != "REPLY":
            return super().judgment_context(action, config, now)
        request, handoff, unused_source = context(self.database, config, action["reply_task_id"], now)
        request = dict(request, candidate_resume=reply_resume.context(), conversation_context=[])
        result = {"reply_context": request, "reply_decision": handoff,
                  "decision_time": now.isoformat()}
        result["application_referral_policy"] = champion_composition.INDEPENDENT_REFERRAL_INSTRUCTIONS
        result["current_application_context"] = [champion_composition.application_context(self.database, scope)
                                                  for scope in request.get("job_scope", [])]
        return result

    def judgment_observation(self, action, observation):
        if action.get("purpose") != "REPLY":
            return observation
        trimmed = json.loads(json.dumps(observation))
        if trimmed.get("route") == engagement.UNIPILE:
            for read in trimmed["observations"]:
                read["messages"] = read["messages"][-9:] if read.get("chat_id") == action["conversation_id"] else []
                read["judgment_window"] = "current message plus eight recent messages; original coverage retained"
            return trimmed
        for read in trimmed["observations"]:
            raw = read.get("raw")
            if read["operation"] == "get_chatroom" and isinstance(raw, dict) and isinstance(raw.get("messages"), list):
                raw["messages"] = (sorted(raw["messages"], key=lambda message: engagement.timestamp(message["createdAt"]))[-9:]
                                   if raw.get("id") == action["conversation_id"] else [])
                if raw.get("id") != action["conversation_id"]:
                    raw.pop("lastMessageText", None)
                read["judgment_window"] = "current message plus eight recent messages; original coverage retained"
            elif read["operation"] == "get_conversations_v2" and isinstance(raw, dict):
                for room in raw.get("items", []):
                    if isinstance(room, dict) and isinstance(room.get("messages"), list):
                        room["messages"] = []
                    if isinstance(room, dict) and room.get("id") != action["conversation_id"]:
                        room.pop("lastMessageText", None)
            if "result" in read:
                read["result"] = raw
        # The model-facing packet carries the read log ONCE (owner, 2026-09-23). The provider
        # envelope exposes it as both `observations` and `reads`; re-applying that alias here
        # sent the whole log twice to the model. It was 145,644 bytes in the largest
        # inspection packets, 41%% of the payload, and it is what pushed Robin Vale (903
        # bytes over) and Sasha Lind past the prompt bound. No prompt, schema or consumer
        # refers to the packet's `reads`; every reader in these modules uses `observations`.
        trimmed.pop("reads", None)
        return trimmed

    def inspect(self, action, config):
        if action.get("purpose") != "REPLY":
            return super().inspect(action, config)
        observation, direct, decision, reference = self._judge(action, "INSPECTION", config)
        unipile = observation.get("route") == engagement.UNIPILE
        if unipile:
            # The port read is the account's own session and the person's own chats; the chat on file
            # stands for the HeyReach chatroom, its complete read for the matching message count.
            account = {}
            rooms = [{"raw": {"id": read["chat_id"], "messages": read["messages"]},
                      "message_coverage": "OBSERVED_COUNT_MATCHES" if read.get("complete") is True else None}
                     for read in observation["observations"]
                     if read.get("operation") == "messages" and read.get("chat_id") == action["conversation_id"]]
        else:
            account = next((read["raw"] for read in observation["observations"]
                            if read["operation"] == "get_linked_in_account_by_id" and read["status"] == "OBSERVED"), {})
            rooms = [read for read in observation["observations"] if read["operation"] == "get_chatroom"
                     and read["status"] == "OBSERVED" and read.get("authenticated") is True
                     and read.get("identity_matches") is True and read["raw"].get("id") == action["conversation_id"]]
        request, unused_handoff, unused_source = context(self.database, config, action["reply_task_id"],
                                                        engagement.timestamp(observation["finished_at"]))
        audit = routing.one(self.database, """SELECT record FROM outreach_agent_audit
            WHERE id=? AND action_id=? AND phase='INSPECTION' AND kind='OBSERVATION'""", (reference, action["action_id"]))
        judged_context = json.loads(audit["record"])["packet"]["current_application_context"]
        context_hash = routing.digest(judged_context)
        current_context = [champion_composition.application_context(self.database, scope)
                           for scope in request.get("job_scope", [])]
        if routing.digest(current_context) != context_hash:
            raise TimeoutError("REPLY_APPLICATION_CONTEXT_CHANGED_DURING_INSPECTION")
        inbox = json.loads(request["source_snapshot"]["payload"])["inbox"]
        room = rooms[0]["raw"] if len(rooms) == 1 else {}
        messages = room.get("messages", [])
        # The inbound message as this read shows it: its time may have drifted by seconds since
        # it was recorded (engagement.MESSAGE_TIME_DRIFT), so "latest" and "answered" are judged
        # against the time in this same read. The person may also have sent the same text twice
        # within the window (recorded once, engagement.drift_alias): every copy is this message,
        # the newest copy must be the newest message in the thread, and "answered" means a
        # message of ours after that newest copy. The person still gets a reply.
        matching = sorted((message for message in messages if message.get("sender") == "CORRESPONDENT"
                           and _same_inbound(action, inbox["text"], message.get("body"), message["createdAt"])),
                          key=lambda item: engagement.timestamp(item["createdAt"]))
        newest_at = engagement.timestamp(matching[-1]["createdAt"]) if matching else None
        latest = bool(matching and sum(engagement.timestamp(item["createdAt"]) >= newest_at for item in messages) == 1)
        answered = any(message.get("sender") == "ME" and engagement.timestamp(message["createdAt"])
                       >= (newest_at or engagement.timestamp(action["reply_inbound_at"])) for message in messages)
        # `isActive` is not required (owner, 2026-09-30): it tracks whether a campaign is running,
        # not whether the account can message, so it held every reply between campaigns.
        sender_ready = ((unipile or account.get("id") == config["sender_id"] and account.get("authIsValid") is True)
                        and observation.get("identity_verified") is True)
        provider_ready = (len(rooms) == 1 and rooms[0].get("message_coverage") == "OBSERVED_COUNT_MATCHES"
                          and (unipile or room.get("blockedByMe") is False and room.get("blockedByParticipant") is False))
        # Every name HeyReach reports for this person in this read: the thread's correspondent and
        # the same person in the conversation listing. Two that disagree are a genuine conflict.
        observed_names = set()
        correspondents = [room.get("correspondentProfile", {})]
        for read in observation["observations"]:
            raw = read.get("raw") if read.get("status") == "OBSERVED" else None
            if read.get("operation") == "get_conversations_v2" and isinstance(raw, dict):
                correspondents.extend(item.get("correspondentProfile") or {} for item in raw.get("items") or []
                                      if isinstance(item, dict) and item.get("id") == action["conversation_id"])
        for correspondent in correspondents:
            try:
                fullname = heyreach_schema.correspondent_name(correspondent) or ""
            except (ValueError, TypeError, AttributeError):
                fullname = ""
            if not outreach.unresolved_recipient_name(fullname):
                observed_names.add(name_key(fullname))
        name_conflict = len(observed_names) > 1
        observed_name = None if name_conflict or not observed_names else next(iter(observed_names))
        excluded = any(name in owner_seed.excluded_names() | owner_seed.no_automatic_names()
                       or _name_excluded(self.database, name) for name in observed_names)
        restrictions = {field: account.get(field) for field in
                        ("messageCooldown", "messagingRestricted", "isLimited", "isBlocked") if field in account}
        restricted = any(value is not False for value in restrictions.values())
        if direct.get("cooldown") is True or direct.get("entitlement_verified") is False:
            restricted = True
        return {"identity": profile_key(action["profile"]), "sender_id": config["sender_id"],
                "recipient_name": observed_name, "recipient_name_conflict": name_conflict,
                "sender_ready": sender_ready, "reply_transport_supported": bool(sender_ready and provider_ready),
                "provider_restriction_observed": restricted, "provider_restriction_fields": restrictions,
                "quota_remaining": None, "quota_basis": "UNKNOWN_PROVIDER_ENFORCED",
                "negative_reply": decision["findings"]["negative_reply"],
                "opt_out": True if excluded else decision["findings"]["opt_out"], "has_reply": bool(matching),
                "checked_at": observation["finished_at"], "evidence": "outreach_agent_audit:" + reference,
                "conversation_id": room.get("id"), "agent_decision": decision["decision"],
                "provider_snapshot_complete": provider_ready, "next_action": decision["next_action"],
                "reply_source_key": action["reply_source_key"], "reply_source_sha256": action["reply_source_sha256"],
                "reply_source_id": action["reply_source_id"], "reply_inbound_at": action["reply_inbound_at"],
                "reply_application_context_sha256": context_hash,
                "reply_latest_inbound": latest, "reply_already_answered": answered,
                "reply_context_verified": not binding_gates(self.database, config, action, engagement.timestamp(observation["finished_at"]))}

    def retry_reply(self, row, action, config, now):
        """Retry a reply whose send hit an error, at most 3 attempts in all (owner, 2026-09-30).

        Every cycle, and so before every retry and once more after the last failure, the thread is
        read from HeyReach. Our exact text after the inbound message means an earlier attempt went through:
        it is recorded as sent from that read and never resent. Any other message from our side
        after the inbound message ends the retries too, since a resend would answer twice. After
        three failed attempts with the reply still absent, the action ends CANCELLED with
        REPLY_SEND_FAILED_AFTER_3_ATTEMPTS: a recorded terminal failure, not a loop. Its reply slot
        stays taken, so the same inbound message is never answered by a second action.
        """
        database = self.database
        claim = database.execute("SELECT * FROM outreach_action_claims WHERE action_id=?", (row["action_id"],)).fetchone()
        if not claim or claim["payload"] != row["payload"] or claim["payload_hash"] != row["payload_hash"]:
            return {"state": row["state"], "blocker": "REPLY_RETRY_CLAIM_REQUIRED"}
        result = json.loads(row["result"]) if row["result"] else {}
        if not isinstance(result, dict):
            result = {"previous_result": result}
        retries = result.get("reply_retries", [])
        attempts = ([result["provider_request"]] if isinstance(result.get("provider_request"), dict) else []) + retries
        started = [engagement.timestamp(attempt["started_at"]) for attempt in attempts if attempt.get("started_at")]
        last = max(started) if started else engagement.timestamp(claim["claimed_at"])
        due = last + (RETRY_DELAYS[min(len(attempts), MAX_SEND_ATTEMPTS) - 1] if attempts else dt.timedelta(0))

        def finish(state, **fields):
            result.update(fields)
            with database:
                moved = database.execute("""UPDATE outreach_actions SET state=?,result=?
                    WHERE action_id=? AND state=? AND payload_hash=?""",
                    (state, json.dumps(result, sort_keys=True), row["action_id"], row["state"],
                     row["payload_hash"])).rowcount
            if moved != 1:
                current = database.execute("SELECT state FROM outreach_actions WHERE action_id=?", (row["action_id"],)).fetchone()
                return {"state": current[0] if current else row["state"], "blocker": "REPLY_RETRY_STATE_CHANGED"}
            return {"state": state, "attempts": len(attempts), **{key: value for key, value in fields.items()
                                                                   if key in {"reply_terminal", "blocker"}}}

        # Unreadable reads back off (10 min doubling, at most every 12 h). A good complete read
        # resets the streak. After MAX_UNREADABLE_CHECKS in a row the reply is never sent again
        # (reply_no_more_sends, never cleared) but stays in UNCERTAIN_NO_RETRY and keeps being
        # read, so a later complete read still records it CONFIRMED_SENT if our text is there, or
        # ends it as a recorded failure if it is not.
        failed_checks = [check for check in result.get("reply_retry_checks", []) if check.get("error_type")]
        streak = result.get("reply_unreadable_streak", len(failed_checks))
        # A row that reached the limit before the flag existed is treated the same (never resent).
        no_more_sends = result.get("reply_no_more_sends") is True or streak >= MAX_UNREADABLE_CHECKS
        if streak:
            wait = min(UNREADABLE_CHECK_DELAY * (2 ** (streak - 1)), MAX_UNREADABLE_CHECK_DELAY)
            recheck = engagement.timestamp(failed_checks[-1]["checked_at"]) + wait if failed_checks else now
            if now < recheck:
                return {"state": row["state"], "blocker": "REPLY_RETRY_THREAD_CHECK_UNAVAILABLE",
                        "next_at": recheck.isoformat(), "unreadable_checks": streak,
                        "no_more_sends": no_more_sends}
        arguments = {"accountId": claim["sender_id"], "conversationId": action["conversation_id"]}
        checked_at = outreach_provider._now()
        unipile = self._unipile_reply(action)
        try:
            if unipile:
                room = _unipile_room(self.port.messages(action["conversation_id"]))
            else:
                room = outreach_provider._snapshot(linkedin_port.HeyReachPort(
                    self.provider.client, claim["sender_id"]).messages(action["conversation_id"]).raw)
            outcome, message = _our_reply(room, action, inbound_text(database, action))
        except Exception as error:
            result.setdefault("reply_retry_checks", []).append(
                {"checked_at": checked_at, "error_type": type(error).__name__, "error": str(error)[:120]})
            result["reply_unreadable_streak"] = streak + 1
            if streak + 1 >= MAX_UNREADABLE_CHECKS:
                result["reply_no_more_sends"] = True
                no_more_sends = True
            with database:
                database.execute("UPDATE outreach_actions SET result=? WHERE action_id=? AND state=? AND payload_hash=?",
                                 (json.dumps(result, sort_keys=True), row["action_id"], row["state"], row["payload_hash"]))
            return {"state": row["state"], "blocker": "REPLY_RETRY_THREAD_CHECK_UNAVAILABLE",
                    "unreadable_checks": streak + 1, "no_more_sends": no_more_sends}
        if streak:
            # A good complete read ends the unreadable streak; the no-more-sends flag stays.
            result["reply_unreadable_streak"] = 0
            with database:
                database.execute("UPDATE outreach_actions SET result=? WHERE action_id=? AND state=? AND payload_hash=?",
                                 (json.dumps(result, sort_keys=True), row["action_id"], row["state"], row["payload_hash"]))
        if outcome is None:
            # The same stand-down rule as promote: a newer inbound message already on record.
            inbound_at = engagement.timestamp(action["reply_inbound_at"])
            text = inbound_text(database, action)
            if any(event.get("kind") == "inbound" and engagement.timestamp(event["occurred_at"]) > inbound_at
                   and not (event.get("source"), event.get("source_id")) == (action.get("reply_source"), action.get("reply_source_id"))
                   and not _same_inbound(action, text, ((event.get("raw") or {}).get("message") or {}).get("body"),
                                         event["occurred_at"], (event.get("raw") or {}).get("conversation_id"))
                   for event in conversation_events(database, action)):
                outcome, message = "NEWER_INBOUND", None
        check = {"checked_at": checked_at, "outcome": outcome or "ABSENT"}
        if outcome == "SENT":
            occurred = engagement.timestamp(message["createdAt"])
            receipt = {"independent": True, "outcome": "SENT", "action_id": row["action_id"],
                       "identity": claim["identity"], "payload_hash": claim["payload_hash"], "sender_id": claim["sender_id"],
                       "operation": "send_message", "conversation_id": action["conversation_id"],
                       "receipt_id": ("unipile" if unipile else "heyreach") + "-observed-sha256:"
                       + routing.digest([claim["sender_id"], action["conversation_id"], message]),
                       "receipt_id_kind": "independently_observed_message_content_hash",
                       "evidence": ("unipile:messages:" if unipile else "heyreach:get_chatroom:" + str(claim["sender_id"]) + ":")
                       + action["conversation_id"],
                       "observed_at": checked_at, "occurred_at": occurred.isoformat(), "basis": "REPLY_RETRY_THREAD_CHECK"}
            with database:
                current = database.execute("SELECT state FROM outreach_actions WHERE action_id=?", (row["action_id"],)).fetchone()
                if not current or current[0] != row["state"]:
                    return {"state": current[0] if current else row["state"], "blocker": "REPLY_RETRY_STATE_CHANGED"}
                engagement._record(database, {"source": engagement.UNIPILE if unipile else "heyreach", "source_id": receipt["receipt_id"],
                    "profile": action["profile"], "kind": "outbound_observed", "occurred_at": receipt["occurred_at"],
                    "observed_at": checked_at, "verified": True, "evidence": receipt["evidence"], "raw": receipt,
                    "purpose": "REPLY"})
                database.execute("UPDATE outreach_actions SET state='CONFIRMED_SENT',result=? WHERE action_id=?",
                                 (json.dumps(dict(receipt, reply_retry_history=result), sort_keys=True), row["action_id"]))
            return {"state": "CONFIRMED_SENT", "attempts": len(attempts), "basis": "REPLY_RETRY_THREAD_CHECK"}
        if outcome is None and no_more_sends:
            result.setdefault("reply_retry_checks", []).append(check)
            return finish("CANCELLED", reply_terminal="REPLY_NOT_DELIVERED_NO_MORE_SENDS",
                          blocker="REPLY_NOT_DELIVERED_NO_MORE_SENDS")
        if outcome is None and now < due:
            # Checked every cycle so a delivered reply is confirmed at once; resent only when due.
            return {"state": row["state"], "blocker": "REPLY_RETRY_WAIT", "next_at": due.isoformat(),
                    "attempts": len(attempts)}
        result.setdefault("reply_retry_checks", []).append(check)
        if outcome == "OTHER_ANSWER":
            return finish("CANCELLED", reply_terminal="REPLY_THREAD_ALREADY_ANSWERED_BY_ANOTHER_MESSAGE",
                          blocker="REPLY_THREAD_ALREADY_ANSWERED_BY_ANOTHER_MESSAGE")
        if outcome == "NEWER_INBOUND":
            return finish("CANCELLED", reply_terminal="REPLY_NEWER_CONVERSATION_ACTIVITY",
                          blocker="REPLY_NEWER_CONVERSATION_ACTIVITY")
        if len(attempts) >= MAX_SEND_ATTEMPTS:
            return finish("CANCELLED", reply_terminal=TERMINAL_FAILURE, blocker=TERMINAL_FAILURE)
        proof = json.loads(claim["preflight"]) if claim["preflight"] else {}
        gates = stop_gates(database, config, action, proof if isinstance(proof, dict) else {})
        if config.get("reply_execution", {}).get("enabled") is not True:
            gates.append("REPLY_EXECUTION_NOT_ENABLED")
        if config.get("sender_id") != claim["sender_id"]:
            gates.append("REPLY_SENDER_MISMATCH")
        if gates:
            return finish("CANCELLED_BEFORE_SEND", provider_guard={"checked_at": checked_at, "gates": gates},
                          blocker=gates[0])
        send = {"conversationId": action["conversation_id"], "linkedInAccountId": claim["sender_id"],
                "message": action["message"], "subject": action.get("subject", "")}
        entry = {"attempt": len(attempts) + 1, "operation": "send_message", "arguments": send,
                 "started_at": outreach_provider._now(), "payload_hash": claim["payload_hash"]}
        with database:
            result["reply_retries"] = retries + [entry]
            moved = database.execute("""UPDATE outreach_actions SET state='REQUEST_STARTED',result=?
                WHERE action_id=? AND state=? AND payload_hash=?""",
                (json.dumps(result, sort_keys=True), row["action_id"], row["state"], row["payload_hash"])).rowcount
        if moved != 1:
            current = database.execute("SELECT state FROM outreach_actions WHERE action_id=?", (row["action_id"],)).fetchone()
            return {"state": current[0] if current else row["state"], "blocker": "REPLY_RETRY_STATE_CHANGED"}
        try:
            sent = outreach_provider._snapshot(send)
            if unipile:
                raw = self.port.send_message(sent["conversationId"], sent["message"]).raw
            else:
                raw = linkedin_port.HeyReachPort(self.provider.client, sent["linkedInAccountId"]).send_message(
                    sent["conversationId"], sent["message"], subject=sent["subject"]).raw
            entry.update(status="ACKNOWLEDGED_UNCONFIRMED", finished_at=outreach_provider._now(),
                         raw=outreach_provider._snapshot(raw))
            state = "ACKNOWLEDGED_UNCONFIRMED"
        except Exception as error:
            entry.update(status="ERROR", finished_at=outreach_provider._now(), error_type=type(error).__name__,
                         diagnostic=str(error)[:300])
            state = "UNCERTAIN_NO_RETRY"
        with database:
            result["reply_retries"] = retries + [entry]
            database.execute("UPDATE outreach_actions SET state=?,result=? WHERE action_id=? AND state='REQUEST_STARTED'",
                             (state, json.dumps(result, sort_keys=True), row["action_id"]))
        return {"state": state, "attempts": len(attempts) + 1}


    # --- Unipile REPLY: send_claimed and the receipt read over the port --------------------------
    def _transport(self, operation, arguments):
        if self.active_action:
            record = self.database.execute("SELECT payload FROM outreach_action_claims WHERE action_id=?",
                                           (self.active_action,)).fetchone()
            action = json.loads(record[0]) if record else {}
            if record and self._unipile_reply(action):
                expected = {"conversationId": action["conversation_id"], "linkedInAccountId": self.config["sender_id"],
                            "message": action["message"], "subject": action.get("subject", "")}
                if operation != "send_message":
                    raise ValueError("TRANSPORT_OPERATION_DIFFERS_FROM_CLAIM")
                if arguments != expected:
                    raise ValueError("TRANSPORT_ARGUMENTS_DIFFER_FROM_CLAIM")
                result = self._send_unipile_reply(self.active_action, self.config)
                outreach_agent.audit(self.database, self.active_action, "SEND", "ACKNOWLEDGMENT", result["finished_at"], result)
                return result
        return super()._transport(operation, arguments)

    def _send_unipile_reply(self, action_id, config):
        """outreach_provider.Provider.send_claimed for a REPLY, over the port: the same claim, scope,
        argument and stop checks (reply_execution.stop_gates), the request committed before the one
        write, and an error from the write left to outreach.cycle and retry_reply."""
        if self.database.in_transaction:
            raise ValueError("Parent must commit REQUEST_STARTED before transport")
        preview = self.database.execute("""SELECT claim.payload,claim.payload_hash,claim.sender_id,action.result
            FROM outreach_action_claims claim JOIN outreach_actions action USING(action_id)
            WHERE claim.action_id=? AND action.state='REQUEST_STARTED'
            AND action.payload=claim.payload AND action.payload_hash=claim.payload_hash
            AND action.identity=claim.identity""", (action_id,)).fetchone()
        pending = json.loads(preview[3]) if preview and preview[3] is not None else {}
        if (not preview or hashlib.sha256(preview[0].encode()).hexdigest() != preview[1]
                or preview[2] != config.get("sender_id") or preview[2] != self.config.get("sender_id")
                or not isinstance(pending, dict) or "provider_request" in pending):
            raise ValueError("Reply claim changed or request already consumed")
        self.database.execute("BEGIN IMMEDIATE")
        try:
            row = self.database.execute("SELECT payload,payload_hash,identity,state,result FROM outreach_actions WHERE action_id=?",
                                        (action_id,)).fetchone()
            claim = self.database.execute("SELECT payload,payload_hash,identity,sender_id,preflight FROM outreach_action_claims WHERE action_id=?",
                                          (action_id,)).fetchone()
            if not row or not claim or row[3] != "REQUEST_STARTED":
                raise ValueError("Committed REQUEST_STARTED and durable claim required")
            if (tuple(row[:3]) != tuple(claim[:3]) or hashlib.sha256(claim[0].encode()).hexdigest() != claim[1]
                    or type(claim[3]) is not int or claim[3] <= 0
                    or config.get("sender_id") != claim[3] or self.config.get("sender_id") != claim[3]
                    or outreach_provider._profile(config.get("sender_profile")) != outreach_provider._profile(self.config.get("sender_profile"))):
                raise ValueError("Action no longer matches immutable claim/account/hash")
            action = json.loads(claim[0])
            allowed = config.get("outreach_allowed_purposes")
            if (config.get("outreach_actions_enabled") is not True or config.get("outreach_mode") != "guarded"
                    or not isinstance(allowed, list) or "REPLY" not in allowed or action.get("purpose") != "REPLY"
                    or action.get("action_id") != action_id or action.get("provider") != engagement.UNIPILE
                    or action.get("channel") != "linkedin"
                    or profile_key(outreach_provider._profile(action.get("profile"))) != claim[2]
                    or action.get("sender_id", claim[3]) != claim[3]
                    or action.get("operation", "send_message") != "send_message"):
                raise ValueError("Claimed send is outside explicit enabled scope")
            arguments = {"conversationId": action.get("conversation_id"), "linkedInAccountId": claim[3],
                         "message": action.get("message"), "subject": action.get("subject", "")}
            if (any(not isinstance(arguments[field], str) or not arguments[field].strip()
                    for field in ("conversationId", "message")) or not isinstance(arguments["subject"], str)
                    or "arguments" in action and action["arguments"] != arguments):
                raise ValueError("Claimed send arguments missing or inconsistent")
            result = json.loads(row[4]) if row[4] is not None else {}
            if not isinstance(result, dict) or "provider_request" in result:
                raise ValueError("Request already consumed or unrecognized durable result")
            checked_at = outreach_provider._now()
            proof = json.loads(claim[4])
            gates = stop_gates(self.database, config, action, proof if isinstance(proof, dict) else {})
            if gates:
                result["provider_guard"] = {"checked_at": checked_at, "gates": sorted(set(gates))}
                self.database.execute("UPDATE outreach_actions SET state='CANCELLED_BEFORE_SEND',result=? WHERE action_id=? AND state='REQUEST_STARTED'",
                                      (json.dumps(result, sort_keys=True), action_id))
                self.database.commit()
                raise ValueError("Final provider stop or freshness check failed")
            request = {"operation": "send_message", "route": engagement.UNIPILE, "arguments": arguments,
                       "started_at": checked_at, "payload_hash": claim[1], "status": "REQUEST_STARTED"}
            result["provider_request"] = request
            self.database.execute("UPDATE outreach_actions SET result=? WHERE action_id=? AND state='REQUEST_STARTED'",
                                  (json.dumps(result, sort_keys=True), action_id))
            self.database.commit()
        except BaseException:
            self.database.rollback()
            raise
        sent = outreach_provider._snapshot(request["arguments"])
        raw = self.port.send_message(sent["conversationId"], sent["message"]).raw
        return {**request, "finished_at": outreach_provider._now(), "status": "ACKNOWLEDGED_UNCONFIRMED",
                "raw": outreach_provider._snapshot(raw)}

    def reconcile(self, action, config):
        """outreach_agent.Adapter.reconcile for a Unipile REPLY: the claim-bound request, then our exact
        message in the reply's chat, sent between the request and the read. The read is the person's
        chats over the port (followup_execution.port_observation), checked directly as the follow-up's
        port receipt is, so no judge turn names the message."""
        if not self._unipile_reply(action):
            return super().reconcile(action, config)
        import followup_execution
        row = self.database.execute("SELECT * FROM outreach_action_claims WHERE action_id=?", (action["action_id"],)).fetchone()
        if not row:
            raise ValueError("OUTREACH_RECONCILIATION_CLAIM_REQUIRED")
        claim = dict(row)
        saved = self.database.execute("SELECT result FROM outreach_actions WHERE action_id=?", (action["action_id"],)).fetchone()
        result = json.loads(saved[0]) if saved and saved[0] else {}
        request = result.get("provider_request") if isinstance(result, dict) else None
        observation = followup_execution.port_observation(self.port, action["profile"])
        reference = outreach_agent.audit(self.database, action["action_id"], "RECONCILIATION", "OBSERVATION",
                                         observation["finished_at"], {"original_observation": observation, "request": request})
        unknown = {"state": "UNKNOWN", "evidence": "outreach_agent_audit:" + reference,
                   "next_action": "Investigate the claim-bound issued request, sender, thread, exact message and request timing; no retry"}
        expected = {"conversationId": action["conversation_id"], "linkedInAccountId": claim["sender_id"],
                    "message": action["message"], "subject": action.get("subject", "")}
        try:
            if (not isinstance(request, dict) or request.get("operation") != "send_message"
                    or request.get("status") != "REQUEST_STARTED" or request.get("payload_hash") != claim["payload_hash"]
                    or request.get("arguments") != expected):
                raise ValueError()
            started = engagement.timestamp(request["started_at"])
            finished = engagement.timestamp(observation["finished_at"])
            if started < engagement.timestamp(claim["claimed_at"]) or observation["account"].get("valid") is not True:
                raise ValueError()
            reads = [read for read in observation["observations"]
                     if read.get("chat_id") == action["conversation_id"] and read.get("complete") is True]
            shown = [message for read in reads for message in read["messages"]
                     if message.get("sender") == "ME" and message.get("body") == action["message"]
                     and started <= engagement.timestamp(message["createdAt"]) <= finished]
            if len(reads) != 1 or len(shown) != 1:
                raise ValueError()
            message = shown[0]
        except (KeyError, ValueError, TypeError):
            return unknown
        return {"independent": True, "outcome": "SENT", "action_id": action["action_id"],
                "identity": claim["identity"], "payload_hash": claim["payload_hash"], "sender_id": claim["sender_id"],
                "operation": "send_message", "conversation_id": action["conversation_id"], "route": engagement.UNIPILE,
                "receipt_id": "unipile-observed-sha256:" + routing.digest([claim["sender_id"], action["conversation_id"], message]),
                "receipt_id_kind": "independently_observed_message_content_hash", "evidence": "outreach_agent_audit:" + reference,
                "observed_at": observation["finished_at"], "occurred_at": engagement.timestamp(message["createdAt"]).isoformat()}

def repair_rejected(root, database, config, adapter, action, proof, now=None):
    """Rewrite a judge-rejected reply once, for the same inbound message and slot (owner, 2026-09-30).

    The reply writer is asked again with the judge's objection (outreach_agent.pending_objection,
    already recorded by outreach.cycle) and its previous text; only the new reply_proposal text is
    taken. The saved decision stays as it was. The rewritten text replaces the queued action's
    message by compare-and-swap on the rejected payload (outreach.enqueue_action(replacing=...)),
    so the action_id and the one reply slot for this inbound message are unchanged and nothing
    claimed is touched. An advocate summary is never rewritten here: it is bound to its saved
    summary record. Returns the action_id, or None. Never raises.
    """
    try:
        if action.get("purpose") != "REPLY" or action.get("champion_summary_id"):
            return None
        current = engagement.timestamp(now or dt.datetime.now(dt.timezone.utc))
        if backlog(action.get("reply_inbound_at")):
            return None
        stored = database.execute("SELECT payload_hash,state,claimed_at FROM outreach_actions WHERE action_id=?",
                                  (action["action_id"],)).fetchone()
        if stored is None or stored[1] not in {"PENDING", "BLOCKED"} or stored[2] is not None:
            return None
        # One rewrite per reply in total, not per cycle: the rewrite's audit row is the record.
        if database.execute("SELECT 1 FROM outreach_agent_audit WHERE action_id=? AND kind='REPLY_REWRITE'",
                            (action["action_id"],)).fetchone():
            return None
        request, handoff, unused_source = context(database, config, action["reply_task_id"], current)
        objection = outreach_agent.pending_objection(database, action["profile"], "REPLY")
        if not objection:
            return None
        packet = dict(request, repair_feedback=objection,
                      previous_reply_proposal={"text": action["message"], "rejected_by": "fresh send judge"})
        template = resources.read_text("reply-prompt.md")
        schema = resources.read_text("reply-schema.json")
        prompt = template + "\n\nUNTRUSTED_SOURCE_JSON:\n" + json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        timeout = config.get("reply_worker", {}).get("timeout_seconds", 60)
        if type(timeout) is not int or not 1 <= timeout <= 600:
            return None
        reference = "reply-repair:" + routing.digest([action["action_id"], action["message"], objection])
        import workflow_tasks
        with database:
            if not workflow_tasks.reserve_compute(database, reference, current.isoformat(), timeout,
                    config.get("timezone", "America/Los_Angeles"), config.get("daily_compute_seconds", 10800)):
                return None
        # The reply repair is a direct OpenRouter call (owner, 2026-10-27), unless an executor is injected.
        execute = ((adapter.text_execute("reply_decision", max_tokens=reply_worker.REPLY_MAX_TOKENS)
                    if hasattr(adapter, "text_execute") else getattr(adapter, "execute", None))
                   or reply_worker.text_executor("reply_decision", max_tokens=reply_worker.REPLY_MAX_TOKENS))
        try:
            raw = execute(config, prompt, schema, timeout)
        finally:
            try:
                with database:
                    workflow_tasks.settle_compute(database, reference, current.isoformat(), outreach_provider._now(),
                                                  "reply repair turn finished; unused tail of its reservation")
            except Exception:
                pass
        inbox = json.loads(request["source_snapshot"]["payload"])["inbox"]
        output = reply_worker._validate(raw, request, inbox["text"], [])
        proposal = (output.get("classification") or output).get("reply_proposal") if isinstance(output, dict) else None
        text = without_dashes(proposal.get("text")) if isinstance(proposal, dict) else None
        if not isinstance(text, str) or not text.strip() or text == action["message"]:
            return None
        audit_id = outreach_agent.audit(database, action["action_id"], "INSPECTION", "REPLY_REWRITE", current.isoformat(),
            {"identity": profile_key(action["profile"]), "objection": objection, "previous_message": action["message"],
             "message": text, "output": raw, "output_sha256": reply_worker.sha(raw), "reply_task_id": action["reply_task_id"]})
        rewritten = dict(action, message=text,
                         verification=dict(action.get("verification", {}),
                                           verified_at=current.isoformat(),
                                           content_sources=list(action.get("verification", {}).get("content_sources", []))
                                           + ["outreach_agent_audit:" + audit_id]))
        with engagement.transaction(database):
            outreach.enqueue_action(database, rewritten, replacing=stored[0])
        database.commit()
        return action["action_id"]
    except Exception:
        return None
