"""Due-only composition; external writes belong to the existing outreach guard.

Parent supplies the shared adapter with provider.collect and enables dispatch only
after implementing binding_gates and thread_gates in its final send boundary.
Candidates use existing action metadata. Confirmed shared-sender actions and
independently delivered prepared campaigns are discovered automatically. Campaign
invitations additionally require verified acceptance and a fresh observed thread;
acceptance never creates an initial or resets its conservative delivery clock.

Integration: FollowupAdapter(root, database, config, client, execute=None), then
cycle(root, database, config, adapter, candidates=None). Feature settings are
followup_execution.enabled, dispatch_enabled (default false), timeout_seconds
(default 60, max 120), max_items (composition attempts per cycle; default 4, max 20). Existing guarded sender must
also explicitly allow FOLLOWUP. Parent action guard uses proof_gates and its
FOLLOWUP-specific boolean proof contract; provider calls thread_gates on a fresh
authenticated room immediately before consuming the existing send request.
Run manual_followup_e2e.py --provider-e2e after installing those parent hooks.

Unipile people (E3.T3 option A, owner 2026-10-17: "Lets go with A"): a person whose initial_sent is
a Unipile INVITE (`sequence`) is read and written through the LinkedInPort the adapter carries
(`FollowupAdapter(..., port=...)`; None keeps the HeyReach-only behaviour). Timing, composition,
claims and gates are the same; only the reads and the write differ: acceptance is
`sequence.acceptance`, the thread is the person's chats (`chats(person=profile)` then `messages`),
the follow-up goes out with `send_message(chat_id)` into the newest chat or `start_chat(provider_id)`
when there is none, and followup_sent is recorded only when a later read shows our message.
"""

import resources
import datetime as dt
import hashlib
import json
from pathlib import Path

import approvals
import engagement
import linkedin_port
import outreach
import owner_seed
import outreach_agent
import outreach_provider
import pipeline_routing as routing
import provider_limits
import reply_resume
import reply_worker
import reply_execution
import sequence
import workflow_tasks
from orchestrator import name_key, profile_key
import text_model


VERSION = "followup-composition-v1"
PHASE = "FOLLOWUP_COMPOSITION"


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _now(value=None):
    return engagement.timestamp(value or dt.datetime.now(dt.timezone.utc))


def _aliases(database, profile):
    return sorted(engagement.identity_keys(database, profile))


UNIPILE = sequence.SOURCE


def _unipile_invite(event):
    return (event.get("kind") == "initial_sent" and event.get("source") == sequence.SOURCE
            and event.get("purpose") == sequence.INVITE)


def _unipile_initial(event):
    """An initial_sent from a Unipile INVITE, or from an INITIAL or FIRST_MESSAGE sent over the port
    (outreach_agent.Adapter._send_port; owner, 2026-10-21: followed up exactly as an INVITE person)."""
    return _unipile_invite(event) or (event.get("kind") == "initial_sent" and event.get("source") == sequence.SOURCE
                                      and event.get("purpose") in {"INITIAL", "FIRST_MESSAGE"})


def unipile_person(database, profile):
    """True when this person's initial_sent went over Unipile (an INVITE, INITIAL or FIRST_MESSAGE)."""
    return any(_unipile_initial(event) for event in engagement.events_for(database, profile))


def _unipile_route(adapter, database, profile):
    """The Unipile branch applies only with a port; without one nothing changes."""
    return getattr(adapter, "port", None) is not None and unipile_person(database, profile)


def _message(chat_id, message):
    sender = "ME" if message.is_sender is True else "CORRESPONDENT" if message.is_sender is False else None
    return {"chat_id": chat_id, "sender": sender, "body": message.text, "createdAt": message.sent_at}


def port_observation(port, profile):
    """One read of the person's chats over the port, shaped for the same thread checks."""
    account = port.account()
    chats = linkedin_port.all_items(lambda cursor: port.chats(cursor, person=profile))
    reads = []
    for chat in chats:
        thread = port.messages(chat.chat_id)
        reads.append({"operation": "messages", "chat_id": chat.chat_id, "complete": thread.complete,
                      "messages": [_message(chat.chat_id, message) for message in thread.messages]})
    return {"route": UNIPILE, "finished_at": _now().isoformat(), "identity_verified": account.valid is True,
            "account": {"valid": account.valid, "account_id": account.account_id, "own_profile": account.own_profile},
            "selected_conversation_id": chats[0].chat_id if chats else None, "observations": reads}


def _port_messages(observation, now):
    """The Unipile twin of _room: the account is valid, every chat was read in full, every message
    is well formed and not in the future."""
    _require(observation.get("route") == UNIPILE and observation.get("account", {}).get("valid") is True,
             "FOLLOWUP_ACCOUNT_OR_THREAD_RESTRICTED")
    messages = []
    for read in observation.get("observations", []):
        _require(read.get("operation") == "messages" and read.get("complete") is True, "FOLLOWUP_THREAD_COVERAGE_UNKNOWN")
        messages.extend(read["messages"])
    for message in messages:
        _require(isinstance(message.get("body"), str) and bool(message["body"].strip())
                 and engagement.timestamp(message["createdAt"]) <= now, "FOLLOWUP_THREAD_MESSAGE_INVALID")
    return sorted(messages, key=lambda message: (engagement.timestamp(message["createdAt"]), routing.encoded(message)))


def _collect(adapter, database, action, phase):
    if _unipile_route(adapter, database, action["profile"]):
        try:
            return port_observation(adapter.port, action["profile"])
        except linkedin_port.PortError as error:
            # A read that cannot decide (invalid session, incomplete page, unknown person) waits.
            raise reply_worker.Held("FOLLOWUP_UNIPILE_READ_" + error.code, retry=True) from None
    return adapter.provider.collect(action, phase)


def plan(database, candidate, now=None):
    """Never returns acceptance-first-message or interview-urgent permission."""
    now = _now(now)
    result = engagement.next_action(database, candidate["profile"], now,
                                    candidate["recipient"], candidate.get("campaign_id"))
    if result.get("purpose") != "FOLLOWUP":
        return dict(result, state="HELD", blocker=result.get("blocker") or "FOLLOWUP_ONLY")
    if name_key(candidate["recipient"]) in owner_seed.excluded_names():
        return {"state": "HELD", "blocker": "EXCLUDED_CONTACT"}
    aliases = _aliases(database, candidate["profile"])
    aliases.append("name:" + name_key(candidate["recipient"]))
    if database.execute("SELECT 1 FROM sqlite_master WHERE name='suppressions' AND type='table'").fetchone():
        placeholders = ",".join("?" for unused in aliases)
        if any(not outreach._followup_suppression_allowed(database, candidate["profile"], row[0]) for row in database.execute(
                "SELECT reason FROM suppressions WHERE identity IN (" + placeholders + ")", aliases)):
            return {"state": "HELD", "blocker": "SUPPRESSED_CONTACT"}
    return result


def _campaign_source(database, config, event):
    import outreach_campaign

    receipt = event["raw"]
    campaign_id = event["campaign_id"]
    claim = database.execute("SELECT * FROM outreach_launch_claims WHERE campaign_id=?", (campaign_id,)).fetchone()
    attempt = database.execute("SELECT * FROM outreach_attempts WHERE campaign_id=?", (campaign_id,)).fetchone()
    _require(claim and attempt and attempt["state"] == "CONFIRMED_SENT"
        and attempt["identity"] == claim["identity"] == profile_key(event["profile"])
        and attempt["claimed_at"] == claim["claimed_at"] and json.loads(attempt["result"]) == receipt
        and claim["sender_id"] == receipt.get("sender_id") == config["sender_id"]
        and hashlib.sha256(claim["payload"].encode()).hexdigest() == claim["payload_hash"]
        == receipt.get("launch_payload_hash") == event.get("launch_payload_hash")
        and event["kind"] == "initial_sent" and event.get("purpose") == "INITIAL"
        and event["source"] == "heyreach" and event["source_id"] == "campaign-invitation:" + str(campaign_id),
        "FOLLOWUP_CAMPAIGN_RECEIPT_UNBOUND")
    candidate = json.loads(claim["payload"])
    owner = outreach._prepared_owner(database, candidate)
    _require(owner and owner["sender_id"] == config["sender_id"]
        and owner["action_id"] == receipt.get("preparation_action_id") == event.get("preparation_action_id")
        and candidate["preparation_payload_hash"] == receipt.get("preparation_payload_hash") == event.get("preparation_payload_hash")
        and candidate["campaign_id"] == campaign_id and candidate["message"] == receipt.get("message")
        and profile_key(receipt["profile"]) == claim["identity"], "FOLLOWUP_CAMPAIGN_OWNER_MISMATCH")
    action, campaign = owner["action"], receipt["campaign"]
    leads = outreach_campaign._complete_listing(receipt["leads"])
    totals = receipt["stats"].get("overallStats", {})
    _require(campaign.get("id") == campaign_id and campaign.get("campaignAccountIds") == [config["sender_id"]]
        and campaign.get("linkedInUserListId") == action["list_id"] and campaign.get("name") == action["campaign_name"]
        and outreach_campaign._same_sequence(receipt["sequence"], action["sequence"])
        and len(leads) == 1 and profile_key(leads[0].get("profileUrl")) == claim["identity"]
        and all(type(totals.get(field)) is int and totals[field] == value for field, value in
                {"connectionsSent": 1, "uniqueLeadsContacted": 1, "messagesSent": 0, "inmailMessagesSent": 0}.items())
        and receipt.get("occurred_at_basis") == "CONSERVATIVE_FIRST_INDEPENDENT_DELIVERY_OBSERVATION"
        and engagement.timestamp(claim["claimed_at"]) <= engagement.timestamp(campaign["startedAt"])
        <= engagement.timestamp(receipt["observed_at"]) == engagement.timestamp(event["occurred_at"]),
        "FOLLOWUP_CAMPAIGN_DELIVERY_UNVERIFIED")
    return {"text": candidate["message"], "conversation_id": None, "invitation": True,
            "quoted": None, "forwarded": None, "sent_not_before": campaign["startedAt"],
            "occurred_at_basis": receipt["occurred_at_basis"]}


def _sources(database, config, profile, now):
    sources = []
    aliases = _aliases(database, profile)
    events = engagement.events_for(database, profile)
    for event in events:
        if event["kind"] not in {"initial_sent", "followup_sent"}:
            continue
        _require(event.get("verified") is True and bool(event.get("evidence")), "FOLLOWUP_UNCONFIRMED_ANCHOR")
        row = dict(database.execute("SELECT * FROM engagement_events WHERE source=? AND source_id=?",
                                    (event["source"], event["source_id"])).fetchone())
        campaign_source = None
        if event.get("preparation_action_id"):
            campaign_source = _campaign_source(database, config, event)
            text, conversation, invitation = campaign_source["text"], None, True
            quoted, forwarded = None, None
        elif _unipile_invite(event):
            saved = database.execute("SELECT payload FROM outreach_actions WHERE action_id=? AND state='CONFIRMED_SENT'",
                                     (event["source_id"].split("invite:", 1)[-1],)).fetchone()
            invite = json.loads(saved["payload"]) if saved else {}
            _require(event["source_id"].startswith("invite:") and invite.get("purpose") == sequence.INVITE
                and invite.get("provider") == sequence.SOURCE and profile_key(invite.get("profile", "")) in aliases
                and event["raw"].get("note") == invite.get("note"), "FOLLOWUP_INVITE_RECEIPT_UNBOUND")
            text, conversation, invitation = invite.get("note"), None, True
            quoted, forwarded = None, None
            # The invitation went out between the request start and the read that showed it.
            campaign_source = {"sent_not_before": event["occurred_at"], "sent_not_after": event["observed_at"]}
        elif event.get("inbox"):
            try:
                routing._inbox(row, {"account_ids": [config["sender_id"]],
                    "sender_profile": config["sender_profile"]}, now.isoformat(), direction="outbound")
            except ValueError as error:
                if str(error) not in reply_worker.REASONS:
                    raise
            inbox = event["inbox"]
            invitation = (inbox.get("message_type") == "invitation"
                and routing._path(event["raw"], inbox.get("raw_paths", {}).get("message_type")) == "invitation")
            text, conversation = inbox["text"], inbox["conversation_id"]
            quoted, forwarded = inbox["quoted"], inbox["forwarded"]
            _require(isinstance(text, str) and bool(text.strip()), "FOLLOWUP_ACTUAL_TEXT_REQUIRED")
            tables = {entry[0] for entry in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "pipeline_routing_conflicts" in tables:
                source_key = routing.digest([routing.VERSION, "engagement_events",
                    routing.encoded([row["source"], row["source_id"]]), "message"])
                _require(not database.execute("SELECT 1 FROM pipeline_routing_conflicts WHERE source_key=?",
                                              (source_key,)).fetchone(), "FOLLOWUP_SOURCE_CONFLICT")
        else:
            receipt = event["raw"]
            saved = database.execute("SELECT * FROM outreach_actions WHERE action_id=? AND state='CONFIRMED_SENT'",
                                     (receipt.get("action_id"),)).fetchone()
            claim = database.execute("SELECT * FROM outreach_action_claims WHERE action_id=?",
                                     (receipt.get("action_id"),)).fetchone()
            _require(saved and claim and json.loads(saved["result"]) == receipt
                and saved["payload"] == claim["payload"] and saved["payload_hash"] == claim["payload_hash"]
                and hashlib.sha256(claim["payload"].encode()).hexdigest() == claim["payload_hash"]
                and receipt.get("independent") is True and receipt.get("outcome") == "SENT"
                and receipt.get("sender_id") == claim["sender_id"] == config["sender_id"]
                and receipt.get("identity") in aliases and receipt.get("payload_hash") == claim["payload_hash"]
                and engagement.timestamp(receipt["occurred_at"]) == engagement.timestamp(event["occurred_at"]),
                "FOLLOWUP_CLAIM_BOUND_RECEIPT_REQUIRED")
            previous = json.loads(saved["payload"])
            _require(profile_key(previous["profile"]) in aliases
                and previous["conversation_id"] == receipt.get("conversation_id")
                and previous["purpose"] in ({"INITIAL", "FIRST_MESSAGE"} if event["kind"] == "initial_sent" else {"FOLLOWUP"}),
                "FOLLOWUP_RECEIPT_PURPOSE_MISMATCH")
            text, conversation, invitation = previous["message"], previous["conversation_id"], False
            quoted, forwarded = None, None
        _require(engagement.timestamp(event["occurred_at"]) <= engagement.timestamp(event["observed_at"]) <= now,
                 "FOLLOWUP_SOURCE_TIME_MISMATCH")
        sources.append({"source": "engagement_events:" + routing.encoded([event["source"], event["source_id"]]),
                        "sha256": routing.digest(row), "kind": event["kind"], "text": text,
                        "conversation_id": conversation, "invitation": invitation,
                        "occurred_at": event["occurred_at"], "quoted": quoted, "forwarded": forwarded})
        if campaign_source:
            sources[-1].update(campaign_source)
        if _unipile_invite(event) and sequence.deferred_invite(event):
            sources[-1]["note_deferred"] = True   # its note went later, as the FIRST_MESSAGE (owner, 2026-10-27)
    # A not-Premium invitation and the note sent after acceptance are one first contact: the follow-up
    # clock starts at the note (engagement.next_action).
    initials = [item for item in sources if item["kind"] == "initial_sent"]
    _require(sum(not item.get("note_deferred") for item in initials) == 1 or len(initials) == 1,
             "FOLLOWUP_INITIAL_HISTORY_UNRESOLVED")
    if any(_unipile_initial(event) for event in events):
        for item in sources:
            item["route"] = UNIPILE
    return sources


def _room(config, candidate, room, now):
    _require(outreach_provider._binding(room, config["sender_id"],
        outreach_provider._profile(config["sender_profile"]), outreach_provider._profile(candidate["profile"]),
        candidate["conversation_id"]), "FOLLOWUP_THREAD_IDENTITY_MISMATCH")
    account = room["linkedInAccount"]
    # `isActive` dropped (owner, 2026-09-23): it tracks activeCampaigns > 0, not account
    # health - see outreach_agent.py:305 and outreach.py:255. Blocking a follow-up because
    # no campaign is currently running is not an account restriction.
    _require(account.get("authIsValid") is True
        and all(account[field] is False for field in
                ("messageCooldown", "messagingRestricted", "isLimited", "isBlocked") if field in account)
        and room.get("blockedByMe") is False and room.get("blockedByParticipant") is False,
        "FOLLOWUP_ACCOUNT_OR_THREAD_RESTRICTED")
    correspondent = room["correspondentProfile"]
    recipient = correspondent.get("fullName") or " ".join(str(correspondent.get(field) or "") for field in ("firstName", "lastName"))
    _require(name_key(recipient) not in (owner_seed.excluded_names() | owner_seed.no_automatic_names()),
             "EXCLUDED_CONTACT")
    messages = room.get("messages")
    _require(isinstance(messages, list) and type(room.get("totalMessages")) is int
             and len(messages) == room["totalMessages"], "FOLLOWUP_THREAD_COVERAGE_UNKNOWN")
    # "The thread must contain only our own messages" is removed (owner, 2026-09-30: "for follow
    # ups: Remove 'the thread must contain only our own messages'"). Stop-on-reply is unchanged: a
    # recorded reply, negative reply or opt-out (engagement_stops, via plan/next_action) and a
    # reply the inspection observes (has_reply) still refuse the follow-up.
    _require(all(isinstance(message, dict) for message in messages), "FOLLOWUP_THREAD_MESSAGE_INVALID")
    for message in messages:
        _require(isinstance(message.get("body"), str) and bool(message["body"].strip())
                 and engagement.timestamp(message["createdAt"]) <= now, "FOLLOWUP_THREAD_MESSAGE_INVALID")
    return sorted(messages, key=lambda message: (engagement.timestamp(message["createdAt"]), routing.encoded(message)))


def _matches(source, message, conversation_id):
    if source.get("route") == UNIPILE:
        # A Unipile person's messages are matched by text and time in any of their chats: our
        # first follow-up usually starts the chat, so its record names no conversation.
        if source["text"] != message["body"]:
            return False
        occurred = engagement.timestamp(message["createdAt"])
        if source["invitation"]:
            return (engagement.timestamp(source["sent_not_before"]) - engagement.MESSAGE_TIME_DRIFT <= occurred
                    <= engagement.timestamp(source["sent_not_after"]) + engagement.MESSAGE_TIME_DRIFT)
        return engagement.same_instant(occurred, source["occurred_at"])
    if source["text"] != message["body"] or not (source["invitation"] or source["conversation_id"] == conversation_id):
        return False
    occurred = engagement.timestamp(message["createdAt"])
    # HeyReach's createdAt for one message moves by seconds between reads (a reply read at
    # 23:41:51.466 was later 23:41:54.548 and 23:41:54.342), so times match within
    # engagement.MESSAGE_TIME_DRIFT, never exactly.
    drift = engagement.MESSAGE_TIME_DRIFT
    if source.get("occurred_at_basis") == "CONSERVATIVE_FIRST_INDEPENDENT_DELIVERY_OBSERVATION":
        return (engagement.timestamp(source["sent_not_before"]) - drift <= occurred
                <= engagement.timestamp(source["occurred_at"]) + drift)
    return engagement.same_instant(occurred, source["occurred_at"])


ACCEPTANCE_POLL_MAX_WAIT = dt.timedelta(days=7)


def poll_wait(polls):
    """Wait after the latest of `polls` acceptance reads: 1h, 2h, 4h ... at most 7 days."""
    return min(dt.timedelta(hours=1) * 2 ** min(max(polls - 1, 0), 20), ACCEPTANCE_POLL_MAX_WAIT)


def _acceptance(database, config, adapter, candidate, action_id, now):
    events = engagement.events_for(database, candidate["profile"])
    accepted = next((event for event in events if event["kind"] == "accepted" and event.get("verified") is True and event.get("evidence")
           and engagement.timestamp(event["occurred_at"]) <= engagement.timestamp(event["observed_at"]) <= now), None)
    if accepted:
        return {"state": "READY", "event": accepted}
    with engagement.transaction(database):
        records = list(database.execute("SELECT id,kind,observed_at,record FROM outreach_agent_audit WHERE action_id=? AND phase=? AND kind IN ('ACCEPTANCE_POLL','ACCEPTANCE_POLL_RESULT')",
                                        (action_id, PHASE)))
        polls = [row for row in records if row["kind"] == "ACCEPTANCE_POLL"]
        latest = max((engagement.timestamp(row["observed_at"]) for row in polls), default=None)
        if latest and now < latest + poll_wait(len(polls)):
            return {"state": "WAIT_ACCEPTANCE", "next_at": (latest + poll_wait(len(polls))).isoformat()}
        # No read limit (owner, 2026-09-30: "There shouldn't be a thing like running out of tries
        # whether she accepted or not"). Three failed reads in a row - counted over the whole
        # history, even between successful ones - used to park the person for good
        # (FOLLOWUP_ACCEPTANCE_READ_EXHAUSTED; one contact since 2026-09-25). A failed read now
        # waits for the next poll like any other; polling ends when the person accepts or the
        # outreach is closed (27-day inactivity, reply, opt-out). Polls back off (owner,
        # 2026-09-30: "we can space it out with a backoff ... with a max wait time. e.g 7 days"):
        # see poll_wait.
        record = {"started_at": now.isoformat(), "profile": candidate["profile"]}
        reference = routing.digest([action_id, PHASE, "ACCEPTANCE_POLL", record])
        database.execute("INSERT INTO outreach_agent_audit VALUES (?,?,?,?,?,?)",
                         (reference, action_id, PHASE, "ACCEPTANCE_POLL", now.isoformat(), routing.encoded(record)))
    try:
        event = _read_acceptance(database, config, adapter, candidate, action_id, now, events)
        state = "READY" if event else "WAIT_ACCEPTANCE"
    except Exception as error:
        outreach_agent.audit(database, action_id, PHASE, "ACCEPTANCE_POLL_RESULT", now.isoformat(),
                             {"poll_ref": reference, "state": "ERROR", "error_type": type(error).__name__})
        raise reply_worker.Held("FOLLOWUP_ACCEPTANCE_READ_FAILED") from None
    outreach_agent.audit(database, action_id, PHASE, "ACCEPTANCE_POLL_RESULT", now.isoformat(),
                         {"poll_ref": reference, "state": state})
    return {"state": state, "event": event, "next_at": None if event else (now + poll_wait(len(polls) + 1)).isoformat()}


def _read_acceptance(database, config, adapter, candidate, action_id, now, events):
    import outreach_campaign

    if any(_unipile_invite(event) for event in events):
        return _read_port_acceptance(database, adapter, candidate, action_id, now)

    initial = next(event for event in events if event["kind"] == "initial_sent" and event.get("preparation_action_id"))
    receipt = initial["raw"]
    campaign_id = initial["campaign_id"]
    client = adapter.provider.client
    reads = []
    for operation, arguments in (
            ("get_campaign", {"campaignId": campaign_id}),
            ("get_campaign_sequence", {"campaignId": campaign_id}),
            ("get_leads_from_list", {"listId": receipt["campaign"]["linkedInUserListId"], "offset": 0, "limit": 100}),
            ("get_overall_stats", {"campaignIds": [campaign_id], "accountIds": [config["sender_id"]]})):
        raw = client.call(operation, arguments)
        reads.append({"operation": operation, "arguments": arguments, "raw": raw, "authenticated": True})
    campaign, sequence, listing, stats = [read["raw"] for read in reads]
    leads = outreach_campaign._complete_listing(listing)
    _require(all(campaign.get(field) == receipt["campaign"].get(field) for field in
        ("id", "campaignAccountIds", "linkedInUserListId", "name", "startedAt"))
        and outreach_campaign._same_sequence(sequence, receipt["sequence"])
        and len(leads) == 1 and profile_key(leads[0].get("profileUrl")) == profile_key(candidate["profile"]),
        "FOLLOWUP_ACCEPTANCE_SCOPE_CHANGED")
    totals = stats.get("overallStats", {})
    observed = _now(now).isoformat()
    reference = outreach_agent.audit(database, action_id, PHASE, "ACCEPTANCE_OBSERVATION", observed, {"reads": reads, "observed_at": observed})
    _require(all(type(totals.get(field)) is int and totals[field] == value for field, value in
               {"connectionsSent": 1, "uniqueLeadsContacted": 1, "messagesSent": 0, "inmailMessagesSent": 0}.items())
        and type(totals.get("connectionsAccepted")) is int and totals["connectionsAccepted"] in {0, 1},
        "FOLLOWUP_ACCEPTANCE_STATS_UNKNOWN")
    if totals["connectionsAccepted"] == 0:
        return None
    event = {"source": "heyreach", "source_id": "campaign-acceptance:" + str(campaign_id),
        "profile": candidate["profile"], "kind": "accepted", "campaign_id": campaign_id, "verified": True,
        "occurred_at": observed, "observed_at": observed, "evidence": "outreach_agent_audit:" + reference,
        "raw": {"reads": reads, "conversation_id": candidate["conversation_id"], "sender_id": config["sender_id"],
                "occurred_at_basis": "FIRST_INDEPENDENT_ACCEPTANCE_OBSERVATION_NOT_ACTUAL_ACCEPTANCE_TIME"}}
    engagement.record_event(database, event)
    database.commit()
    return event


def _read_port_acceptance(database, adapter, candidate, action_id, now):
    """Acceptance of a Unipile invitation over the port (sequence.record_acceptance); None while not
    accepted (PENDING, NOT_PENDING or UNKNOWN all wait for the next poll)."""
    port = getattr(adapter, "port", None)
    _require(port is not None, "FOLLOWUP_UNIPILE_PORT_REQUIRED")
    state = sequence.record_acceptance(database, port, candidate["profile"])
    observed = _now(now).isoformat()
    outreach_agent.audit(database, action_id, PHASE, "ACCEPTANCE_OBSERVATION", observed,
                         {"route": UNIPILE, "state": state, "observed_at": observed})
    database.commit()
    if state != "ACCEPTED":
        return None
    return next(event for event in engagement.events_for(database, candidate["profile"])
                if event["kind"] == "accepted" and event["source"] == sequence.SOURCE)


def _packet(root, database, config, candidate, planned, observation, now):
    sources = _sources(database, config, candidate["profile"], now)
    _require(outreach._fresh(observation.get("finished_at"), now, config.get("outreach_history_max_age_seconds", 3600)),
             "FOLLOWUP_OBSERVATION_STALE")
    unipile = observation.get("route") == UNIPILE
    if unipile:
        messages = _port_messages(observation, now)
    else:
        rooms = [read["raw"] for read in observation.get("observations", [])
                 if read.get("operation") == "get_chatroom" and read.get("status") == "OBSERVED"
                 and read.get("authenticated") is True and read.get("identity_matches") is True
                 and isinstance(read.get("raw"), dict) and read["raw"].get("id") == candidate["conversation_id"]]
        _require(len(rooms) == 1, "FOLLOWUP_AUTHENTICATED_THREAD_REQUIRED")
        messages = _room(config, candidate, rooms[0], now)
    for message in messages:
        # Every message of OURS (or of unknown direction) must still be one we recorded; a message
        # from the correspondent is not outbound and is not reconciled against our sends.
        if message.get("sender") == "CORRESPONDENT":
            continue
        _require(any(_matches(source, message, candidate["conversation_id"]) for source in sources), "FOLLOWUP_UNRECONCILED_OUTBOUND")
    for source in sources:
        _require(sum(_matches(source, message, candidate["conversation_id"]) for message in messages) <= 1,
                 "FOLLOWUP_DUPLICATE_OUTBOUND_UNRESOLVED")
        if unipile:
            _require(source["invitation"] or any(_matches(source, message, candidate["conversation_id"])
                                                 for message in messages), "FOLLOWUP_ANCHOR_NOT_IN_THREAD")
            continue
        _require(source["invitation"] or source["conversation_id"] == candidate["conversation_id"]
            and any(message["body"] == source["text"]
                and engagement.same_instant(message["createdAt"], source["occurred_at"])
                for message in messages), "FOLLOWUP_ANCHOR_NOT_IN_THREAD")
    invitation = next((source for source in sources if source["invitation"]), None)
    recent = [message for message in messages if not invitation or not _matches(invitation, message, candidate["conversation_id"])][-8:]
    historical = candidate["verification"].get("historical_context", candidate["verification"])
    return {"candidate": candidate, "plan": planned, "sources": sources,
            # Who this person is to the role (E17.T2), named as the first-contact packet names it.
            # Context for the writer, never a gate: "UNKNOWN" when research recorded no label.
            "recipient": {"contact_type": reply_worker.recipient_contact_type(
                database, [candidate["profile"], *_aliases(database, candidate["profile"])])},
            "initial_invitation": invitation, "conversation_context": recent,
            "historical_context": {"scope": "HISTORICAL_TEAM_AND_ROLE_NOT_CURRENT_HIRING",
                "verification": historical,
                "age_seconds": (now - engagement.timestamp(historical["verified_at"])).total_seconds()},
            "identity_observed_at": observation["finished_at"],
            "thread_sha256": routing.digest(messages), "decision_time": now.isoformat(),
            "candidate_resume": reply_resume.context(root),
            # The judge's objection to the follow-up it rejected last time, so this turn rewrites
            # that specific problem instead of reproducing it (owner, 2026-09-23: "do replies and
            # followups too"). Absent on a first attempt. Same shape as answering_agent's.
            "repair_feedback": outreach_agent.pending_objection(database, candidate["profile"], "FOLLOWUP")}


def _validate(raw, packet):
    _require(isinstance(raw, str) and len(raw.encode()) <= 16000, "FOLLOWUP_OUTPUT_TOO_LARGE")
    result = json.loads(raw, object_pairs_hook=reply_worker._unique_object)
    _require(isinstance(result, dict) and set(result) ==
        {"output_version", "source_sha256", "decision", "message", "reason", "next_action"}
        and result["output_version"] == VERSION and result["source_sha256"] == routing.digest(packet)
        and result["decision"] in {"COMPOSE", "HOLD"}, "FOLLOWUP_DECISION_BINDING_INVALID")
    for field in ("reason", "next_action"):
        _require(isinstance(result[field], str) and 1 <= len(result[field].strip()) <= 700, "FOLLOWUP_REASON_REQUIRED")
    _require(result["message"] is None if result["decision"] == "HOLD" else
        isinstance(result["message"], str) and 1 <= len(result["message"].strip()) <= 2000,
        "FOLLOWUP_MESSAGE_INVALID")
    return result


def binding_gates(database, config, action, now=None):
    try:
        now = _now(now)
        _require(action.get("purpose") == "FOLLOWUP" and action.get("sender_id") == config["sender_id"],
                 "FOLLOWUP_SCOPE_MISMATCH")
        saved = database.execute("SELECT record FROM outreach_agent_audit WHERE id=? AND action_id=? AND phase=? AND kind='ACTION'",
                                 (action["followup_action_ref"], action["action_id"], PHASE)).fetchone()
        expected = json.loads(saved[0]) if saved else None
        compared = {key: value for key, value in action.items() if key != "followup_action_ref"}
        _require(expected == compared, "FOLLOWUP_IMMUTABLE_ACTION_MISMATCH")
        planned = plan(database, action, now)
        _require(planned.get("state") == "DUE" and planned.get("ordinal") == action["ordinal"]
                 and planned.get("due_at") == action["followup_due_at"], "FOLLOWUP_NOT_DUE_OR_STOPPED")
        _require(routing.digest(_sources(database, config, action["profile"], now)) == action["followup_sources_sha256"],
                 "FOLLOWUP_SOURCE_CHANGED")
        # The owner's approval of this exact text counts as the fresh check (owner, 2026-10-22).
        _require(outreach._fresh(approvals.verified_at(database, action), now,
            config.get("outreach_content_max_age_seconds", 604800)), "FOLLOWUP_CONTENT_STALE")
        return []
    except (ValueError, KeyError, TypeError, IndexError):
        return ["FOLLOWUP_BINDING_OR_DUE_GATE_FAILED"]


def thread_gates(database, config, action, room, now=None):
    gates = binding_gates(database, config, action, now)
    try:
        if isinstance(room, dict) and room.get("route") == UNIPILE:
            messages = _port_messages(room, _now(now))
            _require(room.get("selected_conversation_id") == action["conversation_id"], "FOLLOWUP_THREAD_CHANGED")
        else:
            messages = _room(config, action, room, _now(now))
        _require(routing.digest(messages) == action["followup_thread_sha256"], "FOLLOWUP_THREAD_CHANGED")
    except (ValueError, KeyError, TypeError, IndexError) as error:
        gates.append(str(error) if isinstance(error, ValueError) else "FOLLOWUP_THREAD_INVALID")
    return sorted(set(gates))


def judgment_context(database, action, output_ref=None):
    """Parent shared adapter supplies this to its independent send judge.

    `output_ref` (E17, type_fit_turn): the writer output behind a rewrite that is not saved yet, in
    place of the composition task's result; every other binding is checked the same way."""
    rewrite = output_ref is not None
    if not rewrite:
        task = database.execute("SELECT status,result FROM workflow_tasks WHERE task_id=?", (action["followup_task_id"],)).fetchone()
        _require(task and task["status"] == "COMPLETE", "FOLLOWUP_COMPOSITION_INCOMPLETE")
        result = json.loads(task["result"])
        _require(result.get("decision") == "COMPOSE" and result.get("action_id") == action["action_id"], "FOLLOWUP_COMPOSITION_MISMATCH")
        output_ref, rewrite = result["output_ref"], result.get("type_fit_rewrite") is True
    output = database.execute("SELECT record FROM outreach_agent_audit WHERE id=? AND action_id=? AND phase=? AND kind='OUTPUT'",
                              (output_ref, action["action_id"], PHASE)).fetchone()
    _require(output is not None, "FOLLOWUP_OUTPUT_MISSING")
    output = json.loads(output[0])
    source = database.execute("SELECT record FROM outreach_agent_audit WHERE id=? AND action_id=? AND phase=? AND kind='INPUT'",
                              (output["input_ref"], action["action_id"], PHASE)).fetchone()
    _require(source is not None, "FOLLOWUP_INPUT_MISSING")
    packet = json.loads(source[0])["packet"]
    decision = _validate(output["raw"], packet)
    # The judge sees the dash-free text that is queued and sent, as replies do. A follow-up queued
    # before dashes were removed (2026-10-27) holds the raw text, which is the same proposal.
    raw_message = decision["message"]
    decision["message"] = reply_execution.without_dashes(raw_message)
    if action["message"] == raw_message:
        decision["message"] = raw_message
    _require(decision["message"] == action["message"], "FOLLOWUP_PROPOSAL_CHANGED")
    # This text is the one rewrite a type-fit note bought: the judge raises type fit no more.
    return {"followup_context": packet, "followup_decision": decision, **({"type_fit_rewrite": True} if rewrite else {})}


# The follow-up send judge answers the shared outreach-decision-v1 object plus one field of its own
# (owner, 2026-11-12: "The judge says which kind of block it is ... the problem is the message, or
# the problem is the situation"). Its own schema file, so the first-note judge's strict schema, its
# validator and its judgment-reuse key are untouched (FollowupAdapter._judge reads the file).
def _validate_judgment(raw, packet):
    """outreach_agent._validate on the shared fields, then `block_about`: SITUATION or MESSAGE for a
    BLOCKED decision, None for any other. An answer without the field (one saved or in flight from
    before it existed, an injected executor) or with anything but SITUATION is a MESSAGE block,
    which is what every block was before. Only this enum is ever read; never the reason's wording."""
    output = json.loads(raw, object_pairs_hook=reply_worker._unique_object)
    about = output.pop("block_about", None) if isinstance(output, dict) else None
    decision = outreach_agent._validate(json.dumps(output), packet)
    decision["block_about"] = (None if decision["decision"] != "BLOCKED" else
                               "SITUATION" if about == "SITUATION" else "MESSAGE")
    return decision


def judgment_observation(action, observation):
    """Limit model context; retain original complete raw reads in the audit."""
    trimmed = json.loads(json.dumps(observation))
    for read in trimmed.get("observations", []):
        raw = read.get("raw")
        if read.get("operation") == "get_chatroom" and isinstance(raw, dict):
            raw["messages"] = sorted(raw.get("messages", []), key=lambda message: engagement.timestamp(message["createdAt"]))[-8:] if raw.get("id") == action["conversation_id"] else []
            raw.pop("lastMessageText", None)
            read["judgment_window"] = "eight recent messages; authenticated invitation supplied separately"
        elif read.get("operation") == "get_conversations_v2" and isinstance(raw, dict):
            for room in raw.get("items", []):
                room["messages"] = []
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


def _candidates(database):
    candidates = [json.loads(row[0]) for row in database.execute("""SELECT payload FROM outreach_actions
        WHERE state='CONFIRMED_SENT' AND json_extract(payload,'$.purpose') IN ('INITIAL','FIRST_MESSAGE','FOLLOWUP','INVITE')
        ORDER BY claimed_at DESC""")]
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if {"outreach_launch_claims", "outreach_attempts", "outreach_prepared_campaigns"} <= tables:
        for row in database.execute("""SELECT claims.payload FROM outreach_launch_claims claims
                JOIN outreach_attempts attempts USING(campaign_id)
                JOIN outreach_prepared_campaigns prepared USING(campaign_id)
                JOIN outreach_actions actions ON actions.action_id=prepared.action_id
                WHERE attempts.state='CONFIRMED_SENT' AND actions.state='PREPARED'
                ORDER BY claims.claimed_at DESC"""):
            candidate = json.loads(row[0])
            candidate["conversation_id"] = None
            candidates.append(candidate)
    return candidates


def _defer_capacity(database, task_id, config, now, timeout, budget):
    zone = config.get("timezone", "America/Los_Angeles")
    reset = workflow_tasks.next_day_start(now, zone, workflow_tasks.day_start_hour(config)).astimezone(dt.timezone.utc)
    with engagement.transaction(database):
        task = database.execute("SELECT * FROM workflow_tasks WHERE task_id=?", (task_id,)).fetchone()
        used = workflow_tasks.compute_used(database, workflow_tasks.budget_day(now, zone))
        if task["status"] not in {"QUEUED", "RETRY_WAIT"} or used + timeout <= budget:
            return None
        # Only the tries end a follow-up writer task; it has no deadline (owner, 2026-09-28).
        state = "EXHAUSTED" if task["attempts"] >= task["max_attempts"] else "RETRY_WAIT"
        next_at = reset.isoformat() if state == "RETRY_WAIT" else None
        blocker = "FOLLOWUP_SHARED_CAPACITY_EXHAUSTED"
        next_action = "Preserve attempts; resume only when existing shared capacity is available"
        if (state, next_at, blocker) != (task["status"], task["next_at"], task["blocker"]):
            database.execute("UPDATE workflow_tasks SET status=?,next_at=?,blocker=?,next_action=?,updated_at=? WHERE task_id=?",
                             (state, next_at, blocker, next_action, now.isoformat(), task_id))
            workflow_tasks.record(database, task_id, "CAPACITY_DEFERRED", now.isoformat(),
                                  {"attempts": task["attempts"], "deadline": task["deadline"], "reset_at": reset.isoformat()})
        return {"state": state, "next_at": next_at, "blocker": blocker, "attempts": task["attempts"], "deadline": task["deadline"]}


# Plan answers that are waits, not ends: our own message seen in the inbox and not matched to its
# send yet, a conversation the inbox reader could not decode yet, a first message still expected, and
# another purpose having its turn (plan's FOLLOWUP_ONLY, e.g. the day before an interview). The
# follow-up can be due again after them, so its writer task is kept. So is the task of a person the
# user excluded or suppressed (plan's EXCLUDED_CONTACT, SUPPRESSED_CONTACT): the user can lift that,
# and the rewrite then resumes as it did before this close existed. While it stands the plan is not
# DUE, so nothing is written, judged or sent.
PLAN_HOLDS_THAT_CLEAR = frozenset({"OBSERVED_OUTBOUND_REQUIRES_PURPOSE_RECONCILIATION", "INBOX_CONVERSATION_UNDECODABLE",
                                   "NOTE_WAITS_FOR_ACCEPTANCE", "FOLLOWUP_ONLY", "EXCLUDED_CONTACT", "SUPPRESSED_CONTACT"})


def _close_unneeded(database, aliases, planned, now):
    """Close this person's follow-up writer tasks that wait for another try once the plan no longer
    asks for a follow-up (a reply, an opt-out, an exclusion, the limit, the 14-day window; a closure
    cancels them itself in engagement.close_outreach, and this is the same close). A task left in
    RETRY_WAIT would never run again and Failures would keep saying Sidedoor will try again."""
    if planned.get("state") in {"DUE", "WAITING"} or planned.get("blocker") in PLAN_HOLDS_THAT_CLEAR:
        return
    placeholders = ",".join("?" for unused in aliases)
    waiting = database.execute(
        "SELECT task_id FROM workflow_tasks WHERE worker='message_writer' AND contact_id IN (" + placeholders
        + ") AND json_extract(inputs,'$[0].followup_version')=? AND status='RETRY_WAIT'", (*aliases, VERSION)).fetchall()
    if not waiting:
        return
    reason = planned.get("blocker") or "FOLLOWUP_NOT_DUE"
    with engagement.transaction(database):
        for (task_id,) in waiting:
            changed = database.execute("""UPDATE workflow_tasks SET status='CANCELLED',blocker=?,next_action=NULL,next_at=NULL,
                updated_at=? WHERE task_id=? AND status='RETRY_WAIT'""", (reason, now.isoformat(), task_id))
            if changed.rowcount == 1:
                workflow_tasks.record(database, task_id, "CANCELLED", now.isoformat(), {"reason": reason, "lifecycle": "FOLLOWUP_NOT_DUE"})


def repair_rejected(root, database, config, adapter, action, proof, now=None, execute=None):
    """Rewrite a judge-rejected follow-up inside the same run (owner, 2026-09-23: "i want it in-run").

    The judge runs after this module has already composed and queued its message, so the only way
    to repair without waiting a cycle is to be called back from outreach.cycle. This reopens the
    follow-up's own message_writer task and re-runs the ordinary promotion path for that single
    recipient. The objection reaches the model through _packet's repair_feedback
    (outreach_agent.pending_objection), so the rewrite answers it instead of repeating it.

    Each rewrite is one try of that task (max_attempts, 3 with the first writing). Every rejection
    is recorded on the task as a failed try, so once the tries are spent `workflow_tasks.claim`
    ends it EXHAUSTED and Failures lists it with the rejections as its causes; the rejected text
    stays queued, unclaimed, and is never judged or sent: a text the judge blocked is not put to it
    again (outreach.cycle, outreach_agent.judge_blocked_text), so three writings get at most three
    verdicts. A task with no writing left, or whose writer held on the objection, is not reopened.
    `now` is the calling cycle's own clock (None: the real one), never the instant the judge
    answered: the rewrite's fresh thread read is stamped after that instant (FU1).

    Returns the action_id whose text was rewritten, or None. Never raises: a failed repair turn
    must not fail the cycle that called it.
    """
    try:
        current = _now(now)
        identity = profile_key(action["profile"])
        originals = [item for item in _candidates(database)
                     if profile_key(item["profile"]) == identity]
        if not originals:
            return None
        aliases = _aliases(database, action["profile"])
        placeholders = ",".join("?" for unused in aliases)
        reopened = database.execute(
            "SELECT task_id,status,result FROM workflow_tasks WHERE worker='message_writer' AND contact_id IN ("
            + placeholders + ") AND json_extract(inputs,'$[0].followup_version')=? AND json_extract(inputs,'$[0].ordinal')=? LIMIT 1",
            (*aliases, VERSION, action.get("ordinal"))).fetchone()
        if reopened is None or reopened["status"] in {"BLOCKED", "CANCELLED", "EXHAUSTED"}:
            return None          # no writing left: the rejected text stays queued, unsent
        if reopened["status"] == "COMPLETE":
            if json.loads(reopened["result"] or "{}").get("decision") != "COMPOSE":
                return None      # the writer held on the objection: that ends it, never another rewrite
            # Same reopen the expired-history path already performs a few lines below in promote.
            with database:
                changed = database.execute("""UPDATE workflow_tasks SET status='RETRY_WAIT',blocker='FOLLOWUP_JUDGE_REJECTED',
                    next_action=?,next_at=?,updated_at=? WHERE task_id=? AND status='COMPLETE'""",
                    ("Rewrite the follow-up to answer the judge's objection; do not resend the rejected text",
                     current.isoformat(), current.isoformat(), reopened["task_id"]))
                if changed.rowcount == 1:
                    # The failed try, where failures_page._counts reads it (as workflow_tasks.finish records one).
                    workflow_tasks.record(database, reopened["task_id"], "RETRY_WAIT", current.isoformat(),
                                          {"blocker": "FOLLOWUP_JUDGE_REJECTED"})
        stored = database.execute("SELECT payload_hash FROM outreach_actions WHERE action_id=?",
                                  (action["action_id"],)).fetchone()
        if stored is None:
            return None
        promote(root, database, config, adapter, candidates=originals, execute=execute, now=now,
                replacing={action["action_id"]: stored[0]})
        return action["action_id"]
    except Exception:
        return None


def _writer(adapter, execute=None):
    """The follow-up writer: a direct OpenRouter call (owner, 2026-10-27), unless an executor is injected."""
    if execute is not None:
        return execute
    return (adapter.text_execute("followup", effort=outreach_agent.COMPOSE_EFFORT,
                                 max_tokens=outreach_agent.COMPOSE_MAX_TOKENS)
            if hasattr(adapter, "text_execute") else getattr(adapter, "execute", None)
            ) or reply_worker.text_executor("followup", effort=outreach_agent.COMPOSE_EFFORT,
                                            max_tokens=outreach_agent.COMPOSE_MAX_TOKENS)


def _writer_prompt():
    return (resources.read_text("followup-prompt.md").split("\nFOLLOWUP_SEND_JUDGE:\n", 1)[0],
            resources.read_text("followup-schema.json"))


def _write(root, database, config, adapter, execute, template, schema, timeout, candidate, planned, sources,
           verification, acceptance, task_id, action_id, ordinal, attempt, now, *, repair_feedback=None, wrote=None):
    """The writing half of promote: read the thread, build the packet, call the model, validate the
    answer and bind the action to its records. Queues nothing and finishes no task. `repair_feedback`
    replaces the packet's pending objection; `wrote` runs once the answer is valid. Returns
    (decision, the action or None on HOLD, output_ref, the time of the final checks)."""
    observation = _collect(adapter, database, dict(candidate, action_id=action_id, purpose="FOLLOWUP", ordinal=ordinal,
        sender_id=config["sender_id"]), "INSPECTION")
    current = _now(now)
    _require(plan(database, candidate, current) == planned, "FOLLOWUP_CHANGED_DURING_READ")
    if observation.get("route") == UNIPILE:
        # The person's newest chat, or none yet (start_chat then creates it).
        candidate = dict(candidate, conversation_id=observation.get("selected_conversation_id"))
    elif candidate["conversation_id"] is None:
        conversation_id = observation.get("selected_conversation_id")
        if not isinstance(conversation_id, str) or not conversation_id:
            raise reply_worker.Held("FOLLOWUP_OBSERVED_THREAD_REQUIRED", retry=True)
        candidate = dict(candidate, conversation_id=conversation_id)
    packet = _packet(root, database, config, candidate, planned, observation, current)
    if repair_feedback is not None:
        packet["repair_feedback"] = repair_feedback
    elif packet["repair_feedback"]:
        # The pending objection is the person's newest one. It is handed back only for the text it
        # rejected, while that text is still the queued one: a first writing (this slot is empty,
        # e.g. the second follow-up after a repaired first) is not a rewrite of an older message.
        queued = database.execute("SELECT payload FROM outreach_actions WHERE action_id=?", (action_id,)).fetchone()
        if queued is None or json.loads(queued[0]).get("message") != packet["repair_feedback"][0].get("previous_message"):
            packet["repair_feedback"] = None
    if acceptance:
        packet["acceptance"] = acceptance
    packet["task_id"] = task_id
    prompt = template + "\n\nUNTRUSTED_SOURCE_JSON:\n" + routing.encoded({"source_sha256": routing.digest(packet), **packet})
    _require(len(prompt.encode()) <= outreach_agent.prompt_limit(config), "FOLLOWUP_INPUT_TOO_LARGE")
    reference = outreach_agent.audit(database, action_id, PHASE, "INPUT", current.isoformat(),
        {"packet": packet, "observation": observation, "prompt_sha256": reply_worker.sha(template),
         "schema_sha256": reply_worker.sha(schema), "attempt": attempt})
    raw = execute(config, prompt, schema, timeout)
    output_ref = outreach_agent.audit(database, action_id, PHASE, "OUTPUT", current.isoformat(), {"input_ref": reference, "raw": raw})
    try:
        decision = _validate(raw, packet)
    except ValueError as error:
        # An answer that breaks the schema's rule (e.g. COMPOSE with no message) gets the
        # task's ordinary retry, within its attempt limit, instead of ending BLOCKED.
        raise reply_worker.Held(str(error) if error.args and isinstance(error.args[0], str)
                                and error.args[0].startswith("FOLLOWUP_") else "FOLLOWUP_OUTPUT_INVALID",
                                retry=True) from None
    if wrote is not None:
        wrote()
    current = _now(now)
    _require(plan(database, candidate, current) == planned and routing.digest(_sources(database, config, candidate["profile"], current)) == routing.digest(sources),
             "FOLLOWUP_CHANGED_DURING_COMPOSITION")
    action = None
    if decision["decision"] == "COMPOSE":
        action = dict(candidate, action_id=action_id, purpose="FOLLOWUP", ordinal=ordinal, sender_id=config["sender_id"],
            provider="unipile" if _unipile_route(adapter, database, candidate["profile"]) else "heyreach", channel="linkedin", subject="",
            # No em or en dash goes out (owner style rule), normalised as replies are.
            message=reply_execution.without_dashes(decision["message"]),
            verification=dict(verification, content_verified=True, content_sources=["outreach_agent_audit:" + output_ref]),
            followup_task_id=task_id, followup_due_at=planned["due_at"],
            followup_sources_sha256=routing.digest(sources), followup_thread_sha256=packet["thread_sha256"])
        action["verification"].update(verified_at=current.isoformat(),
            verification_scope="CURRENT_IDENTITY_AND_COMPOSED_CONTENT_ONLY",
            identity_source="outreach_agent_audit:" + reference,
            identity_observed_at=packet["identity_observed_at"],
            team_evidence_scope=packet["historical_context"]["scope"],
            historical_context=packet["historical_context"]["verification"])
        action["followup_action_ref"] = outreach_agent.audit(database, action_id, PHASE, "ACTION", current.isoformat(), action)
    return decision, action, output_ref, current


def type_fit_turn(root, database, config, adapter, row, action, proof, now=None, execute=None):
    """E17, follow-ups: the one rewrite a type-fit note buys, tried on the side. Never raises.

    Called by outreach.cycle for a queued follow-up its send judge just called READY with every gate
    clear, before the claim. When that decision carries the judge's advisory note and this follow-up
    has not had its attempt, the marker is written first (one attempt ever), then the writer is
    called with the note in its repair hand-back shape and the rewrite is validated, bound to its
    own records and judged, all without touching the queued action or the composition task. Only a
    rewrite judged READY with every gate clear replaces the original, in ONE transaction: the queued
    payload (a compare-and-swap on the hash the judge saw, refused once anything was claimed) and the
    composition task's result move together, so at every instant one text is queued and it is the
    text its proof and task result describe.

    Returns (row, action, proof) of the rewrite once it is the queued message, else None: the
    original goes on to its claim exactly as it would have.
    """
    try:
        note = adapter.type_fit_note(action) if hasattr(adapter, "type_fit_note") else None
        if (not note or action.get("purpose") != "FOLLOWUP" or proof.get("agent_decision") != "READY"
                or outreach_agent.type_fit_attempted(database, action["action_id"])):
            return None
        current = _now(now)
        outreach_agent.audit(database, action["action_id"], PHASE, outreach_agent.TYPE_FIT_ATTEMPT, current.isoformat(),
                             {"payload_hash": row["payload_hash"]})
    except Exception:
        return None
    reservation = None

    def unused(reason):
        # Why the original stood, for the record only: not a failure, a hold or a try of anything.
        try:
            outreach_agent.audit(database, outreach_agent.TYPE_FIT_AUDIT + action["action_id"], PHASE, "TYPE_FIT_UNUSED",
                                 current.isoformat(), {"reason": reason})
            if reservation:
                with database:
                    workflow_tasks.settle_compute(database, reservation, current.isoformat(), _now(None).isoformat(),
                                                  "followup type-fit rewrite not used; shared daily pool: unused tail")
        except Exception:
            pass
        return None

    try:
        timeout = config.get("followup_execution", {}).get("timeout_seconds", 60)
        budget = config.get("daily_compute_seconds", 10800)
        task_id = action["followup_task_id"]
        task = database.execute("SELECT * FROM workflow_tasks WHERE task_id=?", (task_id,)).fetchone()
        _require(task is not None and task["status"] == "COMPLETE", "FOLLOWUP_COMPOSITION_INCOMPLETE")
        bound = json.loads(task["inputs"])[0]
        candidate, ordinal = bound["candidate"], bound["ordinal"]
        planned = plan(database, candidate, current)
        sources = _sources(database, config, candidate["profile"], current)
        _require(planned.get("state") == "DUE" and ordinal == planned.get("ordinal") == action["ordinal"]
                 and planned.get("due_at") == action["followup_due_at"]
                 and routing.digest(sources) == action["followup_sources_sha256"], "FOLLOWUP_NOT_DUE_OR_SOURCE_CHANGED")
        # The acceptance the original was written with (a read of its own; not repeated here).
        acceptance = judgment_context(database, action)["followup_context"].get("acceptance")
        template, schema = _writer_prompt()
        with database:
            reserved = workflow_tasks.reserve_compute(database, "followup-type-fit:" + action["action_id"], current.isoformat(),
                                                      timeout, config.get("timezone", "America/Los_Angeles"), budget)
        if not reserved:
            return unused("NO_COMPUTE")
        reservation = "followup-type-fit:" + action["action_id"]
        feedback = outreach_agent.type_fit_feedback(note, identity=profile_key(action["profile"]), purpose="FOLLOWUP",
                                                    previous_message=action["message"], previous_invitation=None)
        decision, rewrite, output_ref, checked = _write(
            Path(root), database, config, adapter, _writer(adapter, execute), template, schema, timeout, candidate, planned,
            sources, candidate["verification"], acceptance, task_id, action["action_id"], ordinal, "type-fit", now,
            repair_feedback=feedback)
        if rewrite is None:
            return unused("WRITER_HOLD")
        if rewrite["message"] == action["message"]:
            return unused("WRITER_CHANGED_NOTHING")
        side_proof = adapter.inspect(rewrite, config, type_fit_side={"output_ref": output_ref, "queued": action})
        verdict = adapter._judged[1]
        required = ("sender_ready", "followup_transport_supported", "followup_context_verified",
                    "followup_thread_unchanged", "provider_restriction_observed", "has_reply", "negative_reply", "opt_out")
        if (not isinstance(side_proof, dict) or side_proof.get("agent_decision") != "READY"
                or any(type(side_proof.get(field)) is not bool and side_proof.get(field) is not None for field in required)
                or outreach.action_gates(database, config, rewrite, side_proof, _now(now))):
            return unused("JUDGE_" + str(side_proof.get("agent_decision") if isinstance(side_proof, dict) else None))
        result = {"decision": decision["decision"], "action_id": action["action_id"], "output_ref": output_ref,
                  "type_fit_rewrite": True}
        evidence = [{"source": "outreach_agent_audit:" + output_ref, "observed_at": checked.isoformat()}]
        with engagement.transaction(database):
            _require(not binding_gates(database, config, rewrite, checked), "FOLLOWUP_BINDING_CHANGED")
            queued = outreach.enqueue_action(database, rewrite, replacing=row["payload_hash"])
            _require(queued["state"] == "REPLACED", "FOLLOWUP_TYPE_FIT_NOT_REPLACED")
            changed = database.execute("""UPDATE workflow_tasks SET result=?,evidence=?,updated_at=? WHERE task_id=?
                AND status='COMPLETE' AND result=?""", (workflow_tasks.encoded(result), workflow_tasks.encoded(evidence),
                                                        checked.isoformat(), task_id, task["result"]))
            _require(changed.rowcount == 1, "FOLLOWUP_TASK_CHANGED_DURING_TYPE_FIT")
            workflow_tasks.record(database, task_id, "TYPE_FIT_REWRITE", checked.isoformat(),
                                  {"result": result, "previous_result": json.loads(task["result"])})
        swapped = database.execute("SELECT * FROM outreach_actions WHERE action_id=?", (action["action_id"],)).fetchone()
    except Exception as error:
        return unused(error.code if isinstance(error, reply_worker.Held) else type(error).__name__)
    try:
        with database:
            workflow_tasks.settle_compute(database, reservation, current.isoformat(), _now(None).isoformat(),
                                          "followup type-fit rewrite completed; shared daily pool: unused tail of a cleanly completed reservation")
        # The verdict the rewrite passed, under the message's own id, where the pages read it.
        outreach_agent.audit(database, action["action_id"], "INSPECTION", "DECISION", side_proof["checked_at"],
                             {"observation_id": str(side_proof.get("evidence", "")).partition(":")[2], "decision": verdict,
                              "route": "TYPE_FIT_REWRITE"})
    except Exception:
        pass
    return swapped, rewrite, side_proof


def promote(root, database, config, adapter, *, candidates=None, execute=None, now=None, replacing=None):
    """Compose eligible work once per durable recipient/ordinal; never send here."""
    settings = config.get("followup_execution", {})
    if settings.get("enabled") is not True:
        return {"state": "DISABLED", "items": []}
    timeout, limit = settings.get("timeout_seconds", 60), settings.get("max_items", 4)
    budget = config.get("daily_compute_seconds", 10800)
    _require(type(timeout) is int and 1 <= timeout <= 600 and type(limit) is int and 1 <= limit <= 20
             and type(budget) is int and timeout <= budget <= 21000, "FOLLOWUP_BOUNDS_INVALID")
    _require(not database.in_transaction, "FOLLOWUP_CALLER_MUST_COMMIT")
    outreach.initialize_actions(database)
    outreach_agent.initialize(database)
    database.commit()
    execute = _writer(adapter, execute)
    root = Path(root)
    template, schema = _writer_prompt()
    items, seen, started = [], set(), 0
    for original in _candidates(database) if candidates is None else candidates:
        if started >= limit:
            break
        current = _now(now)
        task_id, claim, followup_reservation = None, None, None
        item = {"state": "HELD"}
        try:
            candidate = {field: original[field] for field in ("profile", "recipient", "company", "verification")}
            candidate["conversation_id"] = original.get("conversation_id")
            if original.get("campaign_id") is not None:
                candidate["campaign_id"] = original["campaign_id"]
            aliases = _aliases(database, candidate["profile"])
            if seen.intersection(aliases):
                continue
            seen.update(aliases)
            items.append(item)
            item["identity"] = profile_key(candidate["profile"])
            planned = plan(database, candidate, current)
            if planned["state"] != "DUE":
                item.update(planned)
                _close_unneeded(database, aliases, planned, current)
                continue
            ordinal = planned["ordinal"]
            placeholders = ",".join("?" for unused in aliases)
            slot = database.execute("SELECT action_id,state,payload_hash FROM outreach_actions WHERE identity IN (" + placeholders + ") AND slot=?",
                                    (*aliases, "FOLLOWUP:" + str(ordinal))).fetchone()
            existing = database.execute("SELECT * FROM workflow_tasks WHERE worker='message_writer' AND contact_id IN (" + placeholders
                + ") AND json_extract(inputs,'$[0].followup_version')=? AND json_extract(inputs,'$[0].ordinal')=? LIMIT 1",
                (*aliases, VERSION, ordinal)).fetchone()
            rejected = None
            if slot:
                # An occupied slot is done, except the one case of a rewrite (FU1): the queued text is
                # the one the judge rejected and its writer task is open again. Then this pass writes
                # the rewrite and swaps it into that same row (enqueue_action's compare-and-swap on the
                # rejected text's hash; nothing claimed is ever touched). The task is open only through
                # repair_rejected's reopen, so this also resumes a rewrite a stopped run left behind
                # (the reopen and the swap are two transactions), on the next round.
                rejected = (replacing or {}).get(slot[0]) or (
                    slot[2] if existing is not None and existing["status"] in {"QUEUED", "RETRY_WAIT", "RUNNING"}
                    and outreach._never_dispatched(database, slot[0]) else None)
                if rejected is None:
                    item.update(action_id=slot[0], state=slot[1])
                    continue
            verification = candidate["verification"]
            _require(all(verification.get(field) is True for field in ("identity_verified", "team_relevance_verified"))
                and all(verification.get(field) for field in ("identity_source", "team_source"))
                and engagement.timestamp(verification["verified_at"]) <= current,
                "FOLLOWUP_IDENTITY_OR_TEAM_EVIDENCE_REQUIRED")
            sources = _sources(database, config, candidate["profile"], current)
            action_id = "followup:" + routing.digest([config["sender_id"], aliases[0], ordinal])
            acceptance = None
            if any(source.get("sent_not_before") for source in sources):
                readiness = _acceptance(database, config, adapter, candidate, action_id, current)
                if readiness["state"] != "READY":
                    item.update(readiness)
                    continue
                acceptance = readiness["event"]
            inputs = [{"source": sources[0]["source"], "observed_at": current.isoformat(), "followup_version": VERSION,
                       "candidate": candidate, "ordinal": ordinal, "sources_sha256": routing.digest(sources)}]
            with database:
                task_id = existing["task_id"] if existing else workflow_tasks.enqueue(database, "message_writer", action_id,
                    "followup:" + action_id, inputs, (current + dt.timedelta(hours=24)).isoformat(), current.isoformat(),
                    contact_id=aliases[0], effect="READ", max_attempts=3)
            task = database.execute("SELECT * FROM workflow_tasks WHERE task_id=?", (task_id,)).fetchone()
            action_id = task["action_id"]
            item.update(task_id=task_id, action_id=action_id)
            bound = json.loads(task["inputs"])[0]
            _require(bound["candidate"] == candidate and bound["sources_sha256"] == routing.digest(sources), "FOLLOWUP_TASK_SOURCE_CHANGED")
            if task["status"] in {"COMPLETE", "BLOCKED", "CANCELLED", "EXHAUSTED"}:
                item.update(state=task["status"], blocker=task["blocker"])
                if task["status"] == "COMPLETE" and json.loads(task["result"]).get("decision") == "HOLD":
                    item.update(state="HELD", blocker="FOLLOWUP_AI_HOLD")
                continue
            deferred = _defer_capacity(database, task_id, config, current, timeout, budget)
            if deferred:
                item.update(deferred)
                continue
            claim = workflow_tasks.claim(database, task_id, current.isoformat(), lease_seconds=min(3600, timeout + 300))
            if claim["status"] != "CLAIMED":
                item.update(state=claim["status"])
                claim = None
                continue
            started += 1
            followup_reservation = "followup:" + task_id + ":" + str(task["attempts"] + 1)
            followup_reserved_at = current.isoformat()
            with database:
                reserved = workflow_tasks.reserve_compute(database, followup_reservation,
                    followup_reserved_at, timeout, config.get("timezone", "America/Los_Angeles"), budget)
            if not reserved:
                # Compute ran out between the capacity check and the claim: a wait, not a try.
                reset = workflow_tasks.next_day_start(current, config.get("timezone", "America/Los_Angeles"),
                                                      workflow_tasks.day_start_hour(config)).astimezone(dt.timezone.utc)
                with database:
                    workflow_tasks.compute_wait(database, task_id, claim["token"], current.isoformat(), reset.isoformat(),
                                                "FOLLOWUP_SHARED_CAPACITY_EXHAUSTED")
                item.update(state="RETRY_WAIT", blocker="FOLLOWUP_SHARED_CAPACITY_EXHAUSTED", next_at=reset.isoformat())
                claim = None
                continue
            def settled():
                with database:
                    workflow_tasks.settle_compute(database, followup_reservation, followup_reserved_at,
                                                  _now(None).isoformat(), "followup judgment completed; shared daily pool: unused tail of a cleanly completed reservation")

            decision, action, output_ref, current = _write(
                root, database, config, adapter, execute, template, schema, timeout, candidate, planned, sources,
                verification, acceptance, task_id, action_id, ordinal, task["attempts"] + 1, now, wrote=settled)
            with engagement.transaction(database):
                if action:
                    _require(not binding_gates(database, config, action, current), "FOLLOWUP_BINDING_CHANGED")
                    # `rejected` is set only for a rewrite, and only to the exact hash of the note
                    # the judge rejected. verification above was rebuilt for this new text, so the
                    # verified text and the sent text stay the same thing.
                    item.update(outreach.enqueue_action(database, action, replacing=rejected))
                else:
                    item.update(state="HELD", blocker="FOLLOWUP_AI_HOLD", next_action=decision["next_action"])
                workflow_tasks.finish(database, task_id, claim["token"], current.isoformat(),
                    {"decision": decision["decision"], "action_id": action_id, "output_ref": output_ref},
                    [{"source": "outreach_agent_audit:" + output_ref, "observed_at": current.isoformat()}])
            if item.get("state") == "REPLACED":
                # While its task was open the rejected text could not be judged (a stopped run, a
                # failed rewrite try), and that read is parked at INVALID_PROOF. The rewrite is a new
                # text with its own writer output: its inspection starts again.
                outreach.resume_read(database, action_id, "INSPECTION", current, new_text=True)
        except (ValueError, KeyError, TypeError, IndexError, OSError, TimeoutError, reply_worker.Held) as error:
            if item not in items:
                items.append(item)
            if followup_reservation:
                # The unused tail of a failed attempt is refunded too (owner, 2026-09-29); an
                # already settled reservation credits 0.
                with database:
                    workflow_tasks.settle_compute(database, followup_reservation, followup_reserved_at,
                                                  _now(None).isoformat(), "followup judgment failed; shared daily pool: unused tail of a failed attempt")
            item.update(state="HELD", blocker=error.code if isinstance(error, reply_worker.Held) else
                        str(error) if isinstance(error, ValueError) else type(error).__name__)
            provider = provider_limits.wait_of(error)
            if provider:
                item.update(state="WAITING", blocker=provider_limits.CODE)
            if claim and claim.get("status") == "CLAIMED" and provider:
                try:
                    with database:
                        workflow_tasks.provider_wait(database, task_id, claim["token"], _now(now).isoformat(), provider)
                except ValueError:
                    item["claim_state"] = "STALE_OR_CHANGED"
            elif claim and claim.get("status") == "CLAIMED":
                try:
                    with database:
                        workflow_tasks.finish(database, task_id, claim["token"], _now(now).isoformat(), {}, [],
                            status="RETRY_WAIT" if isinstance(error, (TimeoutError, OSError)) or isinstance(error, reply_worker.Held) and error.retry else "BLOCKED",
                            blocker=item["blocker"], next_action="Reconcile retained follow-up evidence within this task's attempts; do not send")
                except ValueError:
                    item["claim_state"] = "STALE_OR_CHANGED"
    return {"state": "PROCESSED", "items": items}


def cycle(root, database, config, adapter, *, candidates=None, execute=None, now=None):
    result = {"composition": promote(root, database, config, adapter, candidates=candidates, execute=execute, now=now)}
    settings = config.get("followup_execution", {})
    if settings.get("enabled") is not True or settings.get("dispatch_enabled") is not True:
        result["execution"] = {"state": "DISABLED", "attempts": 0}
    elif getattr(adapter, "followup_contract", None) != VERSION:
        result["execution"] = {"state": "HELD", "blocker": "FOLLOWUP_SHARED_ADAPTER_INTEGRATION_REQUIRED", "attempts": 0}
    else:
        # In-run repair: a judge rejection calls back here to rewrite the follow-up and the
        # rewritten action is re-judged in this same cycle (owner, 2026-09-23).
        result["execution"] = outreach.cycle(
            database, config, adapter, now=now, purpose="FOLLOWUP",
            # Both turns keep this cycle's own clock (None: the real one), as promote does: their fresh reads
            # are stamped by the provider clock and must not be later than the time they are checked at.
            repair=lambda action, proof, at: repair_rejected(root, database, config, adapter, action, proof, now, execute),
            type_fit=lambda row, action, proof, at: type_fit_turn(root, database, config, adapter, row, action, proof,
                                                                  now, execute))
    return result


def proof_gates(database, config, action, proof, now=None):
    """Parent action guard calls this for FOLLOWUP instead of cold-contact gates."""
    gates = binding_gates(database, config, action, now)
    settings = config.get("followup_execution", {})
    if settings.get("enabled") is not True or settings.get("dispatch_enabled") is not True:
        gates.append("FOLLOWUP_EXECUTION_NOT_ENABLED")
    for field in ("sender_ready", "followup_transport_supported", "followup_context_verified", "followup_thread_unchanged"):
        if proof.get(field) is not True:
            gates.append(field.upper() + "_MISSING")
    # owner, 2026-09-23: "We only block on a solid yes. Unknown is not a block." The follow-up path carried the
    # same `is not False` test as the outreach path and refused on a null the read could not
    # establish. A restriction, reply, negative reply or opt-out that IS observed still refuses.
    for field in ("provider_restriction_observed", "has_reply", "negative_reply", "opt_out"):
        if proof.get(field) is True:
            gates.append(field.upper() + "_PRESENT")
    if (proof.get("history_scope") != "AUTHENTICATED_EXISTING_THREAD_AND_CANONICAL_LIFECYCLE"
            or proof.get("followup_task_id") != action.get("followup_task_id")
            or proof.get("followup_sources_sha256") != action.get("followup_sources_sha256")
            or proof.get("followup_thread_sha256") != action.get("followup_thread_sha256")):
        gates.append("FOLLOWUP_PROOF_BINDING_MISMATCH")
    return sorted(set(gates))


def final_send_gates(database, config, action, identity, proof, now):
    """The Unipile follow-up's final checks after its thread read (`thread_gates`): closure, freshness,
    stops, the action guard and suppressions. Used by the first send (`_send_unipile`) and by a
    resend after a check (`_resend_after_check`)."""
    import inactivity_closure
    gates = list(inactivity_closure.send_gates(database, config, action["profile"], now))
    if not isinstance(proof, dict) or not outreach._fresh(proof.get("checked_at"), now, config.get("outreach_history_max_age_seconds", 3600)):
        gates.append("DIRECT_HISTORY_MISSING_OR_STALE")
    if not outreach._fresh(action.get("verification", {}).get("verified_at"), now, config.get("outreach_content_max_age_seconds", 604800)):
        gates.append("CONTENT_OR_IDENTITY_VERIFICATION_STALE")
    stopped = engagement.stop_state(database, action["profile"])
    if stopped:
        gates.append(stopped)
    gates.extend(outreach.action_gates(database, config, action, proof if isinstance(proof, dict) else {}, now))
    own_suppressions = {identity, "name:" + name_key(action["recipient"])}
    identities = sorted(set(engagement.identity_keys(database, action["profile"])) | own_suppressions)
    if database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='suppressions'").fetchone():
        placeholders = ",".join("?" for unused in identities)
        for suppression in database.execute("SELECT identity,reason FROM suppressions WHERE identity IN (" + placeholders + ")", identities):
            if outreach._followup_suppression_allowed(database, action["profile"], suppression[1]):
                continue
            if (suppression[0] not in own_suppressions
                    or suppression[1] != "Handoff historical outreach; never repeat initial contact"):
                gates.append("SUPPRESSED_CONTACT")
    return gates


class FollowupAdapter(reply_execution.Adapter):
    """Same provider, send claims, and receipt reconciler; scoped thread inspector."""

    followup_contract = VERSION
    port = None

    def __init__(self, root, database, config, client, execute=None, *, history_collector=None, port=None):
        # `port`: the LinkedInPort for Unipile people (E3.T3); None keeps the HeyReach-only path.
        super().__init__(root, database, config, client, execute, history_collector=history_collector, port=port)

    def _unipile(self, action):
        return action.get("purpose") == "FOLLOWUP" and _unipile_route(self, self.database, action["profile"])

    def judgment_context(self, action, config, now, output_ref=None):
        if action.get("purpose") != "FOLLOWUP":
            return super().judgment_context(action, config, now)
        context = judgment_context(self.database, action, output_ref)
        _require(context["followup_context"]["candidate_resume"] == reply_resume.context(self.root), "FOLLOWUP_DESIGNATED_RESUME_CHANGED")
        return dict(context, decision_time=now.isoformat())

    def judgment_observation(self, action, observation):
        if action.get("purpose") != "FOLLOWUP":
            return super().judgment_observation(action, observation)
        if observation.get("route") == UNIPILE:
            # Same window as a HeyReach room: the eight most recent messages of each chat.
            trimmed = json.loads(json.dumps(observation))
            for read in trimmed["observations"]:
                read["messages"] = read["messages"][-8:]
                read["judgment_window"] = "eight recent messages; authenticated invitation supplied separately"
            return trimmed
        return judgment_observation(action, observation)

    def _judge(self, action, phase, config, claim=None, request=None, *, type_fit_side=None, **first_note):
        """`type_fit_side` (E17, type_fit_turn): `action` is a rewrite judged before it is queued. Its
        context comes from its own writer output, the queued action must still be the original, and
        its records go under their own audit id so a rewrite that is thrown away never shows as the
        verdict on the message that goes out."""
        if action.get("purpose") != "FOLLOWUP":
            return super()._judge(action, phase, config, claim, request, **first_note)
        side = type_fit_side or {}
        audit_id = (outreach_agent.TYPE_FIT_AUDIT if type_fit_side else "") + action["action_id"]
        original = port_observation(self.port, action["profile"]) if self._unipile(action) else self.provider.collect(action, phase, claim)
        observation = self.judgment_observation(action, original)
        now = _now(observation["finished_at"])
        # The approved route (owner, 2026-10-22; approvals.py): the same reads, no judge. The judge's
        # own context (the composer's proposal, which an owner revision no longer equals) is not built.
        approved = approvals.approval_for(self.database, action) if phase == "INSPECTION" else None
        packet = {"action": action, "action_sha256": outreach_agent.digest(action), "phase": phase,
            "claim": dict(claim) if claim is not None else None, "request": request, "observation": observation,
            "direct_history": {"scope": "AUTHENTICATED_EXISTING_THREAD_AND_CANONICAL_LIFECYCLE",
                               "all_time_new_contact_clearance": None},
            **({"decision_time": now.isoformat()} if approved is not None
               else self.judgment_context(action, config, now, side.get("output_ref")))}
        if approved is not None:
            decision = approvals.decision(action, approved)
            reference = outreach_agent.audit(self.database, audit_id, phase, "OBSERVATION", now.isoformat(),
                {"packet": packet, "original_observation": original, "source_sha256": outreach_agent.digest(packet),
                 "provenance": {"route": "OWNER_APPROVAL", "decision_source": decision["decision_source"]}})
            outreach_agent.audit(self.database, audit_id, phase, "DECISION", now.isoformat(),
                {"observation_id": reference, "decision": decision, "decision_source": decision["decision_source"]})
            return observation, packet["direct_history"], decision, reference
        template = resources.read_text("followup-prompt.md").split("\nFOLLOWUP_SEND_JUDGE:\n", 1)[1]
        schema = resources.read_text("followup-judge-schema.json")
        prompt = template + "\n\nUNTRUSTED_SOURCE_JSON:\n" + outreach_agent.encoded(
            {"source_sha256": outreach_agent.digest(packet), **packet})
        timeout = config.get("outreach_agent", {}).get("timeout_seconds", 60)
        _require(type(timeout) is int and 1 <= timeout <= 600 and len(prompt.encode()) <= outreach_agent.prompt_limit(config), "FOLLOWUP_JUDGE_BOUND_INVALID")
        provenance = {"model": text_model.model(config), "executable": config["codex"], "auth": text_model.auth(config),
                      "prompt_sha256": reply_worker.sha(template), "schema_sha256": reply_worker.sha(schema)}
        if not self.injected_execute:   # the follow-up send judge is a direct call (owner, 2026-10-27)
            provenance.update(executable=None, route=outreach_agent.DIRECT_ROUTE,
                              reasoning_effort=reply_worker.DIRECT_EFFORT, max_tokens=reply_worker.DIRECT_MAX_TOKENS)
        reference = outreach_agent.audit(self.database, audit_id, phase, "OBSERVATION", now.isoformat(),
            {"packet": packet, "original_observation": original, "source_sha256": outreach_agent.digest(packet),
             "provenance": provenance})
        try:
            with self.database:
                if not workflow_tasks.reserve_compute(self.database, "outreach-ai:" + reference, now.isoformat(), timeout,
                        config.get("timezone", "America/Los_Angeles"), config.get("daily_compute_seconds", 10800)):
                    raise outreach_agent.SharedBudgetExhausted(config, now)
            raw = self.text_execute("followup_send_judge")(config, prompt, schema, timeout)
            output_ref = outreach_agent.audit(self.database, audit_id, phase, "RAW_OUTPUT", now.isoformat(),
                                               {"observation_id": reference, "output": raw})
            decision = _validate_judgment(raw, packet)
            saved = self.database.execute("SELECT payload FROM outreach_actions WHERE action_id=?", (action["action_id"],)).fetchone()
            _require(saved and json.loads(saved[0]) == side.get("queued", action), "FOLLOWUP_ACTION_CHANGED_DURING_JUDGMENT")
            outreach_agent.audit(self.database, audit_id, phase, "DECISION", now.isoformat(),
                {"observation_id": reference, "output_ref": output_ref, "decision": decision})
            with self.database:
                workflow_tasks.settle_compute(self.database, "outreach-ai:" + reference, now.isoformat(),
                                              _now(None).isoformat(), "followup judgment completed; shared daily pool: unused tail of a cleanly completed reservation")
            return observation, packet["direct_history"], decision, reference
        except Exception as error:
            outreach_agent.audit(self.database, audit_id, phase, "FAILURE", now.isoformat(),
                                  {"observation_id": reference, "error_type": type(error).__name__,
                                   **({"executor": reply_worker.executor_detail(error)} if reply_worker.executor_detail(error) else {})})
            if isinstance(error, reply_worker.Held) and error.retry:
                raise TimeoutError("FOLLOWUP_JUDGE_TRANSIENT_FAILURE") from None
            raise

    def inspect(self, action, config, *, type_fit_side=None):
        if action.get("purpose") != "FOLLOWUP":
            return super().inspect(action, config)
        _require(not binding_gates(self.database, config, action, outreach_provider._now()), "FOLLOWUP_NOT_DUE_OR_STOPPED")
        observation, unused_direct, decision, reference = self._judge(
            action, "INSPECTION", config, **({"type_fit_side": type_fit_side} if type_fit_side else {}))
        self._judged = (outreach_agent.digest(action), decision)
        now = _now(observation["finished_at"])
        saved = self.database.execute("SELECT record FROM outreach_agent_audit WHERE id=?", (reference,)).fetchone()
        original = json.loads(saved[0])["original_observation"]
        if original.get("route") == UNIPILE:
            return self._inspect_unipile(action, config, original, decision, reference, now)
        rooms = [read["raw"] for read in original["observations"] if read.get("operation") == "get_chatroom"
            and read.get("status") == "OBSERVED" and read.get("authenticated") is True and read.get("identity_matches") is True
            and isinstance(read.get("raw"), dict) and read["raw"].get("id") == action["conversation_id"]]
        account = next((read["raw"] for read in original["observations"] if read.get("operation") == "get_linked_in_account_by_id"
                        and read.get("status") == "OBSERVED" and read.get("authenticated") is True), {})
        sender_ready = (account.get("id") == config["sender_id"] and account.get("authIsValid") is True
            and original.get("identity_verified") is True
            and profile_key(account.get("profileUrl", "")) == profile_key(config["sender_profile"]))
        room = rooms[0] if len(rooms) == 1 else {}
        unchanged = len(rooms) == 1 and not thread_gates(self.database, config, action, room, now)
        messages = room.get("messages", [])
        findings = decision["findings"]
        restriction_fields = {field: account[field] for field in
            ("messageCooldown", "messagingRestricted", "isLimited", "isBlocked") if field in account}
        return {"identity": profile_key(action["profile"]), "sender_id": config["sender_id"], "sender_ready": sender_ready,
            "followup_transport_supported": bool(sender_ready and len(rooms) == 1 and room.get("blockedByMe") is False
                and room.get("blockedByParticipant") is False),
            "followup_context_verified": not binding_gates(self.database, config, action, now),
            "followup_thread_unchanged": unchanged,
            "provider_restriction_observed": any(value is not False for value in restriction_fields.values()),
            "provider_restriction_fields": restriction_fields, "quota_remaining": None, "quota_basis": "UNKNOWN_PROVIDER_ENFORCED",
            "history_scope": "AUTHENTICATED_EXISTING_THREAD_AND_CANONICAL_LIFECYCLE",
            "has_reply": True if any(message.get("sender") == "CORRESPONDENT" for message in messages) else findings["has_reply"],
            "negative_reply": findings["negative_reply"], "opt_out": findings["opt_out"], "prior_outreach": True,
            "followups_sent": action["ordinal"] - 1 if unchanged else None, "initial_message_sent": True if unchanged else None,
            "checked_at": original["finished_at"], "evidence": "outreach_agent_audit:" + reference,
            "conversation_id": decision["conversation_id"] if decision["conversation_id"] == action["conversation_id"] else None,
            "provider_snapshot_complete": unchanged, "agent_decision": decision["decision"], "next_action": decision["next_action"],
            # The judge's reason, for the writer's hand-back (outreach_agent._judge_text_rejection). A proof
            # saved before this key reads "UNSPECIFIED" there, as it always did; nothing binds to it.
            "agent_reason": decision.get("reason"),
            # What a BLOCKED is about (_validate_judgment): only SITUATION is read, by outreach.cycle.
            "agent_block_about": decision.get("block_about"),
            "followup_task_id": action["followup_task_id"], "followup_sources_sha256": action["followup_sources_sha256"],
            "followup_thread_sha256": action["followup_thread_sha256"]}

    # --- Unipile people (E3.T3): the same proof, final checks and receipt rules over the port ---
    def _inspect_unipile(self, action, config, original, decision, reference, now):
        try:
            messages, readable = _port_messages(original, now), True
        except (ValueError, KeyError, TypeError):
            messages, readable = [], False
        own = original["account"].get("own_profile")
        sender_ready = (original.get("identity_verified") is True
                        and (own is None or profile_key(own) == profile_key(config["sender_profile"])))
        unchanged = readable and not thread_gates(self.database, config, action, original, now)
        findings = decision["findings"]
        return {"identity": profile_key(action["profile"]), "sender_id": config["sender_id"], "sender_ready": sender_ready,
            "followup_transport_supported": bool(sender_ready and readable),
            "followup_context_verified": not binding_gates(self.database, config, action, now),
            "followup_thread_unchanged": unchanged,
            # The port reports no messaging restriction fields; a refused write raises instead.
            "provider_restriction_observed": False, "provider_restriction_fields": {},
            "quota_remaining": None, "quota_basis": "UNKNOWN_PROVIDER_ENFORCED",
            "history_scope": "AUTHENTICATED_EXISTING_THREAD_AND_CANONICAL_LIFECYCLE",
            "has_reply": True if any(message.get("sender") == "CORRESPONDENT" for message in messages) else findings["has_reply"],
            "negative_reply": findings["negative_reply"], "opt_out": findings["opt_out"], "prior_outreach": True,
            "followups_sent": action["ordinal"] - 1 if unchanged else None, "initial_message_sent": True if unchanged else None,
            "checked_at": original["finished_at"], "evidence": "outreach_agent_audit:" + reference,
            "conversation_id": action["conversation_id"] if decision["conversation_id"] == action["conversation_id"] else None,
            "provider_snapshot_complete": unchanged, "agent_decision": decision["decision"], "next_action": decision["next_action"],
            # The judge's reason, for the writer's hand-back (outreach_agent._judge_text_rejection). A proof
            # saved before this key reads "UNSPECIFIED" there, as it always did; nothing binds to it.
            "agent_reason": decision.get("reason"),
            # What a BLOCKED is about (_validate_judgment): only SITUATION is read, by outreach.cycle.
            "agent_block_about": decision.get("block_about"),
            "followup_task_id": action["followup_task_id"], "followup_sources_sha256": action["followup_sources_sha256"],
            "followup_thread_sha256": action["followup_thread_sha256"]}

    def _transport(self, operation, arguments):
        if self.active_action:
            record = self.database.execute("SELECT payload FROM outreach_action_claims WHERE action_id=?", (self.active_action,)).fetchone()
            action = json.loads(record[0]) if record else {}
            if record and self._unipile(action):
                expected = {"conversationId": action["conversation_id"], "linkedInAccountId": self.config["sender_id"],
                            "message": action["message"], "subject": action.get("subject", "")}
                if operation != "send_message" or arguments != expected:
                    raise ValueError("TRANSPORT_ARGUMENTS_DIFFER_FROM_CLAIM")
                result = self._send_unipile(self.active_action, self.config)
                outreach_agent.audit(self.database, self.active_action, "SEND", "ACKNOWLEDGMENT", result["finished_at"], result)
                return result
        return super()._transport(operation, arguments)

    def _send_unipile(self, action_id, config, fresh_history=False):
        """outreach_provider.Provider.send_claimed for a FOLLOWUP, over the port: the same final thread
        read and stops, one request after REQUEST_STARTED is committed, and an exception from the
        write left uncertain for reconciliation (checked before any resend: port_receipt)."""
        if self.database.in_transaction:
            raise ValueError("Parent must commit REQUEST_STARTED before transport")
        preview = self.database.execute("""SELECT claim.payload,claim.payload_hash,claim.sender_id,action.result
            FROM outreach_action_claims claim JOIN outreach_actions action USING(action_id)
            WHERE claim.action_id=? AND action.state='REQUEST_STARTED'
            AND action.payload=claim.payload AND action.payload_hash=claim.payload_hash
            AND action.identity=claim.identity""", (action_id,)).fetchone()
        pending = json.loads(preview[3]) if preview and preview[3] is not None else {}
        if (not preview or hashlib.sha256(preview[0].encode()).hexdigest() != preview[1]
                or preview[2] != config.get("sender_id") or not isinstance(pending, dict) or "provider_request" in pending):
            raise ValueError("Follow-up claim changed or request already consumed")
        action = json.loads(preview[0])
        check = {"operation": "chats+messages", "started_at": outreach_provider._now(), "payload_hash": preview[1],
                 "authenticated": False}
        provider_id = None
        try:
            observation = port_observation(self.port, action["profile"])
            if action["conversation_id"] is None:
                provider_id = self.port.lookup(action["profile"]).provider_id
            check.update(raw=observation, authenticated=True, observed_at=outreach_provider._now())
            check["gates"] = thread_gates(self.database, config, action, observation, outreach_provider._now())
            if action["conversation_id"] is None and not provider_id:
                check["gates"].append("PROVIDER_ID_UNKNOWN")
        except Exception as error:
            check.update(error_type=type(error).__name__, observed_at=outreach_provider._now())
        self.database.execute("BEGIN IMMEDIATE")
        try:
            row = self.database.execute("SELECT payload,payload_hash,identity,state,result FROM outreach_actions WHERE action_id=?",
                                        (action_id,)).fetchone()
            claim = self.database.execute("SELECT payload,payload_hash,identity,sender_id,preflight FROM outreach_action_claims WHERE action_id=?",
                                          (action_id,)).fetchone()
            if not row or not claim or row[3] != "REQUEST_STARTED" or tuple(row[:3]) != tuple(claim[:3]) \
                    or config.get("sender_id") != claim[3] or self.config.get("sender_id") != claim[3]:
                raise ValueError("Committed REQUEST_STARTED and durable claim required")
            allowed = config.get("outreach_allowed_purposes")
            if (config.get("outreach_actions_enabled") is not True or config.get("outreach_mode") != "guarded"
                    or not isinstance(allowed, list) or "FOLLOWUP" not in allowed or action.get("action_id") != action_id
                    or profile_key(action.get("profile")) != claim[2] or action.get("sender_id", claim[3]) != claim[3]):
                raise ValueError("Claimed send is outside explicit enabled scope")
            result = json.loads(row[4]) if row[4] is not None else {}
            if not isinstance(result, dict) or "provider_request" in result:
                raise ValueError("Request already consumed or unrecognized durable result")
            checked_at = outreach_provider._now()
            now = engagement.timestamp(checked_at)
            proof = json.loads(claim[4])
            if fresh_history and "raw" in check and ours_since(check["raw"], proof if isinstance(proof, dict) else {}):
                # Review L1: the user wrote to this person since this message was approved; never sent.
                check.setdefault("gates", []).append(ALREADY_SENT_GATE)
            if fresh_history and isinstance(proof, dict) and check.get("observed_at"):
                # Review F1: a never-written row's first send (followup_execution._first_send) takes its own
                # complete chats read above as the history, so an outage delay never makes it stale.
                proof = dict(proof, checked_at=check["observed_at"])
            result["provider_followup_check"] = check
            if "error_type" in check:
                self.database.execute("UPDATE outreach_actions SET state='UNCERTAIN_NO_RETRY',result=? WHERE action_id=? AND state='REQUEST_STARTED'",
                                      (json.dumps(result, sort_keys=True), action_id))
                self.database.commit()
                raise ValueError("Final follow-up thread read unavailable; reconcile without retry")
            gates = list(check["gates"]) + final_send_gates(self.database, config, action, claim[2], proof, now)
            if gates:
                result["provider_guard"] = {"checked_at": checked_at, "gates": sorted(set(gates))}
                self.database.execute("UPDATE outreach_actions SET state='CANCELLED_BEFORE_SEND',result=? WHERE action_id=? AND state='REQUEST_STARTED'",
                                      (json.dumps(result, sort_keys=True), action_id))
                self.database.commit()
                raise ValueError("Final provider stop or freshness check failed")
            # A chat on file: send into it; none yet: start_chat, which creates (or reuses) the 1:1 chat.
            operation, arguments = (("send_message", {"chat_id": action["conversation_id"], "text": action["message"]})
                                    if action["conversation_id"] else
                                    ("start_chat", {"provider_id": provider_id, "text": action["message"]}))
            request = {"operation": operation, "route": UNIPILE, "arguments": arguments, "started_at": checked_at,
                       "payload_hash": claim[1], "status": "REQUEST_STARTED"}
            result["provider_request"] = request
            self.database.execute("UPDATE outreach_actions SET result=? WHERE action_id=? AND state='REQUEST_STARTED'",
                                  (json.dumps(result, sort_keys=True), action_id))
            self.database.commit()
        except BaseException:
            self.database.rollback()
            raise
        try:
            if operation == "send_message":
                ack = self.port.send_message(arguments["chat_id"], arguments["text"])
            else:
                ack = self.port.start_chat(arguments["provider_id"], arguments["text"])
        except Exception as error:
            if outreach.never_forwarded(error):   # E18 B8: held, nothing sent: no try spent
                outreach.unwrite_held_request(self.database, action_id, error)
            raise
        return {**request, "finished_at": outreach_provider._now(), "status": "ACKNOWLEDGED_UNCONFIRMED",
                "raw": sequence._jsonable(ack.raw)}

    def reconcile(self, action, config):
        if not self._unipile(action):
            return super().reconcile(action, config)
        return port_receipt(self, action, "unipile-followup:", config)


# Check before resend (owner, 2026-11-06: "the right thing is to check the message was sent first
# before deciding to retry"). A message whose send was unclear (UNCERTAIN_NO_RETRY: timeout, dropped
# connection, 5xx; ACKNOWLEDGED_UNCONFIRMED not shown yet; REQUEST_STARTED left by a crash) is read
# again: a message of ours since the request -> sent. Not shown, with the target chat read in full,
# once LinkedIn had time to catch up (sequence.retry_due_at: the draft campaigns' 15/60-minute
# backoff) -> every first-send check again, then sent again under the same claim, at most
# outreach.WRITE_ATTEMPT_LIMIT (3) attempts in all; then CONFIRMED_NOT_SENT. A read that fails or
# cannot be complete decides nothing and is read again later; it never sends. A start_chat whose chat
# is not known (no acknowledgement named it) is never resent: after UNLOCATED_CHECKS checks that find
# no message of ours it ends for good (owner pick, review Q1 a).
MESSAGE_CHECK_STATES = ("UNCERTAIN_NO_RETRY", "ACKNOWLEDGED_UNCONFIRMED", "REQUEST_STARTED")
UNLOCATED_CHECKS = 3
FINAL = "UNCONFIRMED_FINAL"
# Review round 2 (D-3, D-4): an unclear send still undecided this long after its first attempt ends for
# good, never resent ("Couldn't confirm it went out"); a row with nothing written ends CONFIRMED_NOT_SENT.
UNDECIDED_LIMIT = dt.timedelta(days=7)


ALREADY_SENT_GATE = "VERIFIED_MESSAGE_ALREADY_SENT"   # approvals_page.GATE_WORDS: "A message to this person was already sent."


def ours_since(observation, proof):
    """Review L1: True when a fresh read shows a message from the user's own account to this person after
    the inspection that approved this message (the original proof's checked_at). A message of ours whose
    time cannot be read counts, and so does one whose sender cannot be read (it may be the user's own);
    messages from the contact never do."""
    try:
        approved = engagement.timestamp(proof.get("checked_at"))
    except (AttributeError, TypeError, ValueError):
        approved = None
    for read in (observation or {}).get("observations", []):
        for message in read.get("messages") or []:
            if message.get("sender") not in ("ME", None):
                continue
            try:
                if approved is None or engagement.timestamp(message["createdAt"]) > approved:
                    return True
            except (KeyError, TypeError, ValueError):
                return True
    return False


def _first_at(claim, request):
    """When the first attempt started (the request), else the claim."""
    for value in ((request or {}).get("started_at") if isinstance(request, dict) else None, claim["claimed_at"]):
        try:
            return engagement.timestamp(value)
        except (TypeError, ValueError):
            continue
    return None


def _end_unconfirmed(database, action, state, raw, result, now, reason):
    """The final "couldn't confirm" end: UNCERTAIN_NO_RETRY with check_final, never resent; the read is
    then COMPLETE (outreach._reconcile_action). None when the row changed meanwhile."""
    result["check_final"] = {"at": now.isoformat(), "reason": reason,
                             **({"checks": result["unlocated_checks"]} if "unlocated_checks" in result else {})}
    if _move(database, action, state, "UNCERTAIN_NO_RETRY", result, raw) is None:
        return None
    return {"state": FINAL, "final_state": "UNCERTAIN_NO_RETRY"}


def _ack_chat(raw):
    """The chat id an acknowledgement names (Unipile's ChatStarted {chat_id, message_id}), else None."""
    value = raw.get("chat_id") if isinstance(raw, dict) else None
    return value if isinstance(value, str) and value.strip() else None


def _target_chat(database, action_id, request, result):
    """The chat the message was sent into: send_message's chat_id; for start_chat the chat an earlier
    attempt's acknowledgement named (a retry's saved raw, or the first send's SEND/ACKNOWLEDGMENT audit)."""
    if request.get("operation") == "send_message":
        return _ack_chat(request.get("arguments"))
    for entry in reversed(result.get("message_retries") or []):
        if _ack_chat(entry.get("raw")):
            return _ack_chat(entry["raw"])
    for (record,) in database.execute("""SELECT record FROM outreach_agent_audit WHERE action_id=? AND phase='SEND'
            AND kind='ACKNOWLEDGMENT' ORDER BY observed_at DESC""", (action_id,)).fetchall():
        try:
            found = _ack_chat(json.loads(record).get("raw"))
        except (ValueError, AttributeError):
            found = None
        if found:
            return found
    return None


def _complete_reads(observation):
    """{chat_id: messages} for each chat read in full whose every message has a readable time; a chat
    read only in part, or with an unreadable message time, maps to None."""
    reads = {}
    for read in observation.get("observations", []):
        if read.get("operation") != "messages":
            continue
        messages = None
        if read.get("complete") is True:
            try:
                messages = [dict(message, at=engagement.timestamp(message["createdAt"])) for message in read["messages"]]
            except (KeyError, TypeError, ValueError):
                messages = None
        reads[read.get("chat_id")] = messages
    return reads


def port_receipt(adapter, action, receipt_prefix, config=None):
    """The claim-bound receipt of a message sent over the port (a Unipile FOLLOWUP, or INITIAL/FIRST_MESSAGE
    in outreach_agent): one read of the person's chats. Any message of ours since the request started
    (within the drift window) is SENT: the earliest exact copy, else the earliest message of ours (its text
    may have been altered). Otherwise UNKNOWN; `_resend_after_check` may then send it again, and a row
    whose request was never written is sent as a first attempt (`_first_send`)."""
    claim = adapter.database.execute("SELECT * FROM outreach_action_claims WHERE action_id=?", (action["action_id"],)).fetchone()
    if not claim:
        raise ValueError("OUTREACH_RECONCILIATION_CLAIM_REQUIRED")
    saved = adapter.database.execute("SELECT state,result FROM outreach_actions WHERE action_id=?", (action["action_id"],)).fetchone()
    result = json.loads(saved[1]) if saved and saved[1] else {}
    result = result if isinstance(result, dict) else {}
    if result.get("check_final"):
        # Review S3: ended for good; never acted on again.
        return {"state": FINAL, "final_state": saved[0]}
    request = result.get("provider_request")
    if request is None and saved and saved[0] in ("UNCERTAIN_NO_RETRY", "REQUEST_STARTED"):
        # Nothing was written (the request is committed before the one write): a normal first attempt.
        return _first_send(adapter, action, saved[0])
    first_at = _first_at(claim, request)

    def undecided(answer, now):
        # D-3: still undecided 7 days after the first attempt -> final, never resent.
        if (saved[0] in MESSAGE_CHECK_STATES and first_at is not None and now - first_at > UNDECIDED_LIMIT):
            return _end_unconfirmed(adapter.database, action, saved[0], saved[1], result, now, "UNDECIDED_AFTER_7_DAYS") or answer
        return answer

    try:
        observation = port_observation(adapter.port, action["profile"])
    except linkedin_port.IncompletePage:
        # A listing or thread that could hold more than was read never shows absence: read again later.
        return undecided({"state": "UNKNOWN", "next_action": "The chats read was incomplete; read them again later; never resend"},
                         _now())
    except Exception:
        ended = undecided(None, _now())
        if ended is None:
            raise   # a failed check is read again later (outreach.cycle); it never sends
        return ended
    now = _now(observation["finished_at"])
    reference = outreach_agent.audit(adapter.database, action["action_id"], "RECONCILIATION", "OBSERVATION", now.isoformat(),
                                     {"original_observation": observation, "request": request})
    unknown = {"state": "UNKNOWN", "evidence": "outreach_agent_audit:" + reference,
               "next_action": "Read the person's chats again; never resend"}
    if (not isinstance(request, dict) or request.get("route") != UNIPILE or request.get("status") != "REQUEST_STARTED"
            or request.get("payload_hash") != claim["payload_hash"]
            or engagement.timestamp(request["started_at"]) < engagement.timestamp(claim["claimed_at"])):
        return unknown
    if observation.get("account", {}).get("valid") is not True:
        return undecided(unknown, now)
    started = engagement.timestamp(request["started_at"]) - engagement.MESSAGE_TIME_DRIFT
    reads = _complete_reads(observation)
    since = sorted((message for messages in reads.values() if messages for message in messages if message["at"] >= started),
                   key=lambda message: (message["at"], routing.encoded({key: message[key] for key in ("chat_id", "body")})))
    ours = [message for message in since if message.get("sender") == "ME"]
    exact = [message for message in ours if message.get("body") == action["message"]]
    if ours:
        message = (exact or ours)[0]
        return {"independent": True, "action_id": action["action_id"], "identity": claim["identity"],
                "payload_hash": claim["payload_hash"], "operation": "send_message", "sender_id": claim["sender_id"],
                "outcome": "SENT", "conversation_id": action["conversation_id"], "chat_id": message["chat_id"],
                "route": UNIPILE, "receipt_id": receipt_prefix + action["action_id"],
                "evidence": "outreach_agent_audit:" + reference, "observed_at": observation["finished_at"],
                "occurred_at": message["createdAt"], "basis": "EXACT_TEXT" if exact else "OUR_MESSAGE_TEXT_DIFFERS"}
    if any(message.get("sender") not in ("ME", "CORRESPONDENT") for message in since):
        return undecided(unknown, now)   # a message whose sender the read does not say may be ours
    attempts = [request] + list(result.get("message_retries") or [])
    due = sequence.retry_due_at(attempts)
    if saved[0] not in MESSAGE_CHECK_STATES or due is None or now < due:
        return unknown
    target = _target_chat(adapter.database, action["action_id"], request, result)
    if target is None:
        if request.get("operation") != "start_chat":
            return undecided(unknown, now)
        # Owner pick (review Q1 a): the chat is not known, so absence cannot be read; never resent.
        result["unlocated_checks"] = int(result.get("unlocated_checks") or 0) + 1
        if result["unlocated_checks"] < UNLOCATED_CHECKS:
            _move(adapter.database, action, saved[0], saved[0], result, saved[1])
            return unknown
        return _end_unconfirmed(adapter.database, action, saved[0], saved[1], result, now, "START_CHAT_CHAT_UNKNOWN") or unknown
    if reads.get(target) is None or any(messages is None for messages in reads.values()):
        # The target chat, or any other chat of the person (review S1), was not read in full: absence is not shown.
        return undecided(unknown, now)
    _resend_after_check(adapter, action, dict(claim), saved[0], saved[1], result, request, observation, now)
    return unknown


def _first_send(adapter, action, state):
    """A row whose request was never written (UNCERTAIN_NO_RETRY or REQUEST_STARTED with no
    provider_request): nothing was sent, so it is sent as attempt 1 through the first send itself
    (`FollowupAdapter._send_unipile` / `outreach_agent.Adapter._send_port`, every first-send check)."""
    database = adapter.database
    claim = database.execute("SELECT preflight,claimed_at FROM outreach_action_claims WHERE action_id=?", (action["action_id"],)).fetchone()
    saved = database.execute("SELECT result FROM outreach_actions WHERE action_id=?", (action["action_id"],)).fetchone()
    result = json.loads(saved[0]) if saved and saved[0] else {}
    result = result if isinstance(result, dict) else {"previous_result": result}
    now = _now(outreach_provider._now())
    first_at = _first_at(claim, None) if claim else None
    if first_at is not None and now - first_at > UNDECIDED_LIMIT:
        # D-4: nothing was ever written and it still could not go out after 7 days: did not go out.
        result["not_sent"] = {"at": now.isoformat(), "attempts": 0, "basis": "FIRST_SEND_NOT_MADE_IN_7_DAYS"}
        _move(database, action, state, "CONFIRMED_NOT_SENT", result, saved[0] if saved else None)
        return {"state": "UNKNOWN", "first_send": "CONFIRMED_NOT_SENT"}
    fresh_at = None
    if action.get("purpose") != "FOLLOWUP":
        # Review F1: a fresh complete history read of the person first (the inspection's own read,
        # outreach_agent.Adapter._port_observation); its finish time is the history's checked_at. A read
        # that fails or is incomplete sends nothing now; the row is read again later.
        try:
            observation = adapter._port_observation(action)
            if (observation.get("account", {}).get("valid") is not True
                    or any(read.get("complete") is not True for read in observation.get("observations", []))):
                raise linkedin_port.IncompletePage("FRESH_HISTORY_INCOMPLETE")
            fresh_at = observation["finished_at"]
        except Exception as error:
            return {"state": "UNKNOWN", "first_send": "FRESH_HISTORY_READ_FAILED", "error_type": type(error).__name__,
                    "next_action": "Read the person's history again later; nothing was sent"}
        # The action guard execute_claimed runs before the port send (a follow-up's send runs it itself).
        approved = json.loads(claim[0]) if claim and claim[0] else {}
        approved = approved if isinstance(approved, dict) else {}
        proof = dict(approved, checked_at=fresh_at)
        gates = outreach.action_gates(database, adapter.config, action, proof, now)
        if ours_since(observation, approved):
            # Review L1: the user wrote to this person since this message was approved; never sent.
            gates.append(ALREADY_SENT_GATE)
        if gates:
            result["provider_guard"] = {"checked_at": now.isoformat(), "gates": sorted(set(gates))}
            result["check_before_resend_cancelled"] = True
            _move(database, action, state, "CANCELLED_BEFORE_SEND", result, saved[0] if saved else None)
            return {"state": "UNKNOWN", "first_send": "CANCELLED_BEFORE_SEND"}
    if state != "REQUEST_STARTED" and _move_state(database, action, state, "REQUEST_STARTED") is None:
        return {"state": "UNKNOWN", "next_action": "The action changed; read it again"}
    try:
        if action.get("purpose") == "FOLLOWUP":
            acknowledged = adapter._send_unipile(action["action_id"], adapter.config, fresh_history=True)
        else:
            acknowledged = adapter._send_port(action["action_id"], adapter.config, fresh_checked_at=fresh_at)
        outreach_agent.audit(database, action["action_id"], "SEND", "ACKNOWLEDGMENT", acknowledged["finished_at"], acknowledged)
        after = "ACKNOWLEDGED_UNCONFIRMED"
    except Exception:
        after = "UNCERTAIN_NO_RETRY"   # a refused check already ended it (CANCELLED_BEFORE_SEND); a write is unknown
    _move_state(database, action, "REQUEST_STARTED", after)
    _mark_cancelled(database, action)
    return {"state": "UNKNOWN", "first_send": after, "next_action": "Read the person's chats for the first attempt"}


def _mark_cancelled(database, action):
    """Mark a CANCELLED_BEFORE_SEND the first send's own checks reached from here, so Networking shows
    it ("Not sent: <reason>")."""
    row = database.execute("SELECT result FROM outreach_actions WHERE action_id=? AND state='CANCELLED_BEFORE_SEND'",
                           (action["action_id"],)).fetchone()
    result = json.loads(row[0]) if row and row[0] else None
    if isinstance(result, dict) and not result.get("check_before_resend_cancelled"):
        result["check_before_resend_cancelled"] = True
        _move(database, action, "CANCELLED_BEFORE_SEND", "CANCELLED_BEFORE_SEND", result, row[0])


def resend_gates(adapter, action, claim, observation, now):
    """Every first-send check, against the live configuration and account (adapter.config), before
    a resend: the action guard and, for a follow-up, its thread read and final checks; for an INITIAL or
    FIRST_MESSAGE the port send's checks. The history is this resend's own complete read (review N1):
    the claim's proof with checked_at = that observation's finished_at."""
    database, live = adapter.database, adapter.config
    proof = json.loads(claim["preflight"]) if claim.get("preflight") else {}
    proof = dict(proof if isinstance(proof, dict) else {}, checked_at=observation["finished_at"])
    gates = []
    if live.get("sender_id") != claim["sender_id"]:
        gates.append("SENDER_CHANGED")
    if engagement.closure_status(database, action["profile"]):
        gates.append("OUTREACH_CLOSED")
    if action.get("purpose") == "FOLLOWUP":
        gates.extend(thread_gates(database, live, action, observation, now))
        gates.extend(final_send_gates(database, live, action, claim["identity"], proof, now))
    else:
        gates.extend(outreach.action_gates(database, live, action, proof, now))
        gates.extend(outreach_agent.port_send_gates(database, live, action, claim["identity"], proof, now))
    return sorted(set(gates))


def _resend_after_check(adapter, action, claim, state, raw, result, request, observation, now):
    """Send an unclear message again after a complete read of its chat showed nothing of ours since its
    request (the caller's absence, once due); or end it CONFIRMED_NOT_SENT after the last allowed
    attempt. Every first-send check runs first (`resend_gates`; refused -> CANCELLED_BEFORE_SEND). The
    same claim and the same request (operation and arguments) as the first attempt; the request is
    committed (REQUEST_STARTED, CAS on the state and the stored result read, so two readers never both
    send) before the one write. Returns the new state,
    or None when nothing changed."""
    database = adapter.database
    retries = result.get("message_retries") or []
    attempts = [request] + retries
    if len(attempts) >= outreach.WRITE_ATTEMPT_LIMIT:
        result["not_sent"] = {"at": now.isoformat(), "attempts": len(attempts), "basis": "CHECK_BEFORE_RESEND"}
        return _move(database, action, state, "CONFIRMED_NOT_SENT", result, raw)
    gates = resend_gates(adapter, action, claim, observation, now)
    if gates:
        result["provider_guard"] = {"checked_at": now.isoformat(), "gates": gates}
        result["check_before_resend_cancelled"] = True
        return _move(database, action, state, "CANCELLED_BEFORE_SEND", result, raw)
    entry = {"attempt": len(attempts) + 1, "operation": request["operation"], "arguments": request["arguments"],
             "started_at": outreach_provider._now(), "payload_hash": claim["payload_hash"]}
    result["message_retries"] = retries + [entry]
    if _move(database, action, state, "REQUEST_STARTED", result, raw) is None:
        return None
    try:
        arguments = request["arguments"]
        if request["operation"] == "send_message":
            ack = adapter.port.send_message(arguments["chat_id"], arguments["text"])
        else:
            ack = adapter.port.start_chat(arguments["provider_id"], arguments["text"])
        entry.update(status="ACKNOWLEDGED_UNCONFIRMED", finished_at=outreach_provider._now(), raw=sequence._jsonable(ack.raw))
        after = "ACKNOWLEDGED_UNCONFIRMED"
    except Exception as error:   # a write's exception is UNKNOWN: it may have landed; checked again first
        if outreach.never_forwarded(error):
            # E18 B8: held before any request. Not an attempt: the row returns to the state it was read in.
            result["message_retries"] = retries
            outreach.held_write_note(result, error, request["operation"])
            return _move(database, action, "REQUEST_STARTED", state, result)
        entry.update(status="ERROR", finished_at=outreach_provider._now(), error_type=type(error).__name__,
                     diagnostic=str(error)[:300])
        after = "UNCERTAIN_NO_RETRY"
    return _move(database, action, "REQUEST_STARTED", after, result)


_ANY = object()


def _move(database, action, expected, state, result, expected_result=_ANY):
    """CAS on the state (and, when given, the stored result text as read)."""
    with database:
        if expected_result is _ANY:
            moved = database.execute("UPDATE outreach_actions SET state=?,result=? WHERE action_id=? AND state=?",
                                     (state, json.dumps(result, sort_keys=True), action["action_id"], expected)).rowcount
        else:
            moved = database.execute("UPDATE outreach_actions SET state=?,result=? WHERE action_id=? AND state=? AND result IS ?",
                                     (state, json.dumps(result, sort_keys=True), action["action_id"], expected,
                                      expected_result)).rowcount
    return state if moved == 1 else None


def _move_state(database, action, expected, state):
    with database:
        moved = database.execute("UPDATE outreach_actions SET state=? WHERE action_id=? AND state=?",
                                 (state, action["action_id"], expected)).rowcount
    return state if moved == 1 else None
