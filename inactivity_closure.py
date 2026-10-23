import datetime as dt
import json
from types import SimpleNamespace

import engagement
import linkedin_port
from orchestrator import profile_key


DAYS = 27


def key(profile):
    return profile_key(profile if "://" in profile else "https://" + profile)


def enabled(config):
    value = config.get("outreach_inactivity_days")
    if value is None:
        return False
    if type(value) is not int or value != DAYS:
        raise ValueError("Authorized inactivity threshold is 27 calendar days")
    return True


def initialize(database):
    engagement.initialize(database)
    database.execute("""CREATE TABLE IF NOT EXISTS outreach_inactivity_checks (
        identity TEXT PRIMARY KEY, anchor_at TEXT NOT NULL, attempts INTEGER NOT NULL,
        checked_at TEXT NOT NULL, next_at TEXT, state TEXT NOT NULL, result TEXT NOT NULL)""")
    database.execute("""CREATE TABLE IF NOT EXISTS outreach_inactivity_audit (
        identity TEXT NOT NULL, checked_at TEXT NOT NULL, payload TEXT NOT NULL)""")
    initialize_anchors(database)
    database.commit()


def initialize_anchors(database):
    """Immutable conservative outreach anchors (see backfill_inactivity_anchors.py).

    anchor_at is an UPPER BOUND on the original outreach instant, never a claimed
    delivery time: using it can only delay closure, never bring it forward. Rows are
    insert-only; a later, different candidate never overwrites a stored anchor.
    Dated partial observations keep verified facts from reviews that were not
    complete enough to close (for example a quoted pending invitation)."""
    database.execute("""CREATE TABLE IF NOT EXISTS outreach_inactivity_anchors (
        identity TEXT PRIMARY KEY, anchor_at TEXT NOT NULL, basis TEXT NOT NULL,
        source TEXT NOT NULL, recorded_at TEXT NOT NULL, payload TEXT NOT NULL)""")
    database.execute("""CREATE TABLE IF NOT EXISTS outreach_inactivity_observations (
        identity TEXT NOT NULL, observed_at TEXT NOT NULL, kind TEXT NOT NULL,
        source TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(identity, source, kind, observed_at))""")


ANCHOR_BASES = {"SEND_DATE_UPPER_BOUND", "HEYREACH_SEND_OBSERVED_UPPER_BOUND"}


def stored_anchor(database, profile):
    """Earliest stored upper-bound anchor across verified aliases, or None."""
    if not database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='outreach_inactivity_anchors'").fetchone():
        return None
    identities = engagement.identity_keys(database, profile)
    placeholders = ",".join("?" for unused in identities)
    rows = database.execute("SELECT identity,anchor_at,basis,source,payload FROM outreach_inactivity_anchors WHERE identity IN ("
                            + placeholders + ")", identities).fetchall()
    anchors = []
    for identity, anchor_at, basis, source, payload in rows:
        if basis not in ANCHOR_BASES or not source:
            raise ValueError("Invalid stored outreach anchor")
        anchors.append((engagement.timestamp(anchor_at), identity, basis, source, json.loads(payload)))
    if not anchors:
        return None
    instant, identity, basis, source, payload = min(anchors, key=lambda anchor: anchor[:2])
    return {"identity": identity, "anchor_at": instant, "basis": basis, "source": source, "payload": payload}


def record_anchor(database, profile, anchor_at, basis, source, payload, recorded_at):
    """Insert-only. Returns INSERTED, UNCHANGED or CONFLICT (never overwrites)."""
    if basis not in ANCHOR_BASES or not isinstance(source, str) or not source.strip():
        raise ValueError("Anchor basis and source required")
    identity = key(profile)
    anchor = engagement.timestamp(anchor_at).isoformat()
    previous = database.execute("SELECT anchor_at,basis,source FROM outreach_inactivity_anchors WHERE identity=?",
                                (identity,)).fetchone()
    if previous:
        return "UNCHANGED" if tuple(previous) == (anchor, basis, source) else "CONFLICT"
    database.execute("INSERT INTO outreach_inactivity_anchors VALUES (?,?,?,?,?,?)",
                     (identity, anchor, basis, source, engagement.timestamp(recorded_at).isoformat(),
                      json.dumps(payload, sort_keys=True)))
    return "INSERTED"


def known_prior(database, profile):
    aliases = engagement.identity_keys(database, profile)
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    # Only records of outreach that actually happened. rw_contacts.historical=1 marks a person as
    # already known at the company, which includes reserved/pending contacts
    # whose drafts were never sent; treating it as outreach started 27-day checks on people the
    # owner never contacted (2026-09-19: a held draft; the owner confirmed no contact).
    for table, field, condition in (("history", "profile", ""), ("history", "identity", ""),
            ("outreach_attempts", "identity", " WHERE state IN ('ALREADY_SENT','CONFIRMED_SENT')"),
            ("outreach_actions", "identity", " WHERE state='CONFIRMED_SENT'")):
        if table not in tables:
            continue
        for row in database.execute("SELECT " + field + " FROM " + table + condition):
            try:
                if row[0] and key(row[0]) in aliases:
                    return True
            except ValueError:
                continue
    return False


def plan(database, profile, now=None):
    current = engagement.timestamp(now or dt.datetime.now(dt.timezone.utc))
    closed = engagement.closure_status(database, profile)
    if closed:
        return closed
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "engagement_events" not in tables:
        return {"state": "NOT_STARTED", "identity": key(profile)}
    starts, responses, references = [], [], []
    try:
        events = engagement.events_for(database, profile)
    except (ValueError, KeyError, TypeError):
        return {"state": "UNKNOWN", "identity": key(profile), "blocker": "INVALID_SAVED_ACTIVITY_EVENT"}
    for event in events:
        if not isinstance(event, dict) or not all(event.get(field) for field in ("source", "source_id")):
            return {"state": "UNKNOWN", "identity": key(profile), "blocker": "INVALID_SAVED_ACTIVITY_EVENT"}
        if event.get("verified") is not True or event.get("kind") not in {
                "initial_sent", "outbound_observed", "inbound", "accepted"}:
            continue
        try:
            occurred = engagement.timestamp(event["occurred_at"])
            observed = engagement.timestamp(event["observed_at"])
            if occurred > observed or observed > current or not event.get("evidence"):
                raise ValueError("Invalid activity evidence")
        except (ValueError, KeyError, TypeError):
            return {"state": "UNKNOWN", "identity": key(profile), "blocker": "INVALID_ACTIVITY_TIMESTAMP_OR_EVIDENCE"}
        if event["kind"] in {"initial_sent", "outbound_observed"}:
            starts.append(occurred)
        else:
            responses.append((occurred, event["kind"]))
        references.append({"source": event["source"], "source_id": event["source_id"],
                           "kind": event["kind"], "occurred_at": event["occurred_at"]})
    try:
        anchor = stored_anchor(database, profile)
    except (ValueError, KeyError, TypeError):
        return {"state": "UNKNOWN", "identity": key(profile), "blocker": "INVALID_SAVED_OUTREACH_ANCHOR"}
    if anchor:
        # Conservative upper bound on the original outreach: never earlier than the truth.
        starts.append(anchor["anchor_at"])
        references.append({"source": "outreach_inactivity_anchors", "source_id": anchor["identity"],
                           "kind": "initial_sent_upper_bound", "occurred_at": anchor["anchor_at"].isoformat(),
                           "basis": anchor["basis"], "evidence": anchor["source"]})
    if not starts and not any(kind == "inbound" for unused, kind in responses):
        if known_prior(database, profile):
            return {"state": "UNKNOWN", "identity": key(profile), "anchor_at": "UNKNOWN", "due_at": None,
                    "blocker": "PRIOR_OUTREACH_TIMESTAMP_UNVERIFIED", "next_action": "Reconcile original outreach and current activity before inactivity closure"}
        return {"state": "NOT_STARTED", "identity": key(profile),
                "next_action": "Retain prior-contact suppression; reconcile a confirmed outreach or conversation timestamp"}
    stored = anchor
    initial = min(starts) if starts else min(instant for instant, kind in responses if kind == "inbound")
    anchor = max([initial] + [instant for instant, unused in responses])
    due = anchor + dt.timedelta(days=DAYS)
    result = {"state": "DUE" if current >= due else "ACTIVE", "identity": key(profile),
              "initial_at": initial.isoformat(), "anchor_at": anchor.isoformat(), "due_at": due.isoformat(),
              "days": DAYS, "sources": references}
    if stored and initial == stored["anchor_at"]:
        result["anchor_basis"] = stored["basis"]
    # Acceptance not observable (hand-sent invitation): the first complete clean read at the
    # due date starts one more 27-day window for this anchor (owner, 2026-09-30).
    first = _first_clean_read(database, profile, anchor)
    if first:
        due = first + dt.timedelta(days=DAYS)
        result.update(state="DUE" if current >= due else "ACTIVE", due_at=due.isoformat(),
                      second_window_from=first.isoformat())
    return result


FIRST_CLEAN_READ = "FIRST_CLEAN_READ_ACCEPTANCE_UNOBSERVABLE"


def _first_clean_read(database, profile, anchor):
    if not database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='outreach_inactivity_observations'").fetchone():
        return None
    identities = engagement.identity_keys(database, profile)
    row = database.execute("SELECT MIN(observed_at) FROM outreach_inactivity_observations WHERE kind=? AND source=? AND identity IN ("
                           + ",".join("?" for unused in identities) + ")",
                           [FIRST_CLEAN_READ, "inactivity-second-window:" + anchor.isoformat()] + identities).fetchone()
    return engagement.timestamp(row[0]) if row and row[0] else None


def send_gates(database, config, profile, now=None):
    if not enabled(config):
        return []
    state = plan(database, profile, now)["state"]
    # UNKNOWN no longer blocks (owner, 2026-09-23). This is the FIRST gate on every send, and
    # `plan` returns UNKNOWN whenever the prior-outreach timestamp cannot be reconstructed -
    # true for all 12 tracked contacts, every one of them anchor_at='UNKNOWN'. Not knowing when
    # someone was last contacted is absence of evidence, and it was stopping every message.
    # A DUE state - the 27-day policy actually having elapsed against a real date - still holds,
    # and an explicitly CLOSED conversation still holds.
    return ["OUTREACH_CLOSED"] if state == "CLOSED" else ["INACTIVITY_CLOSURE_REVIEW_REQUIRED"] if state == "DUE" else []


def candidates(database):
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    identities = set()
    for table, field in (("history", "identity"), ("outreach_actions", "identity"),
                         ("outreach_attempts", "identity"), ("rw_contacts", "identity")):
        if table in tables:
            identities.update(row[0] for row in database.execute("SELECT DISTINCT " + field + " FROM " + table)
                              if isinstance(row[0], str) and "linkedin.com/in/" in row[0])
    if "history" in tables:
        identities.update(row[0] for row in database.execute("SELECT profile FROM history")
                          if isinstance(row[0], str) and "linkedin.com/in/" in row[0])
    # Conversation-only people (HeyReach inbox events, no outreach record) are managed too
    # (owner, 2026-09-30).
    if "engagement_events" in tables:
        identities.update(row[0] for row in database.execute("SELECT DISTINCT identity FROM engagement_events")
                          if isinstance(row[0], str) and "linkedin.com/in/" in row[0])
    return sorted(identities)


def provider_id_only(aliases):
    """True when every known spelling is a HeyReach member-id profile spelling (linkedin.com/in/ACoAA...),
    which the profile-URL read cannot resolve once case-folded."""
    return all(alias.rsplit("/", 1)[-1].startswith("acoaa") for alias in aliases)


def _heyreach_campaign_acceptance(config, client, profile, anchor):
    """Acceptance for a HeyReach-sent single-contact campaign bound by its stored anchor.

    Returns (state, reads) with state PENDING or ACCEPTED; raises on any scope change."""
    import outreach_campaign
    binding = anchor["payload"].get("heyreach") or {}
    campaign_id, list_id = binding.get("campaign_id"), binding.get("list_id")
    if type(campaign_id) is not int or type(list_id) is not int:
        raise ValueError("HEYREACH_ANCHOR_UNBOUND")
    reads = []
    for operation, arguments in (("get_campaign", {"campaignId": campaign_id}),
            ("get_leads_from_list", {"listId": list_id, "offset": 0, "limit": 100}),
            ("get_overall_stats", {"campaignIds": [campaign_id], "accountIds": [config["sender_id"]]})):
        reads.append({"operation": operation, "arguments": arguments, "raw": client.call(operation, arguments)})
    campaign, listing, stats = [read["raw"] for read in reads]
    leads = outreach_campaign._complete_listing(listing)
    totals = (stats or {}).get("overallStats", {})
    if (campaign.get("id") != campaign_id or campaign.get("campaignAccountIds") != [config["sender_id"]]
            or campaign.get("linkedInUserListId") != list_id or campaign.get("startedAt") != binding.get("started_at")
            or len(leads) != 1 or profile_key(leads[0].get("profileUrl")) not in anchor["aliases"]
            or type(totals.get("connectionsSent")) is not int or totals["connectionsSent"] != 1
            or type(totals.get("connectionsAccepted")) is not int or totals["connectionsAccepted"] not in {0, 1}):
        raise ValueError("HEYREACH_CAMPAIGN_ACCEPTANCE_SCOPE_UNVERIFIED")
    return ("ACCEPTED" if totals["connectionsAccepted"] else "PENDING"), reads


def _heyreach_read(database, config, client, profile):
    """The HeyReach closure read and its checks; returns the read, its time, completeness, the
    reasons it is incomplete, member-id issues, decoded events and every conversation id it read."""
    import heyreach_schema
    import outreach_provider

    observation = outreach_provider.Provider(database, config, client).collect(
        {"profile": profile, "action_id": "inactivity:" + profile_key(profile)}, "INSPECTION")
    observed = engagement.timestamp(observation["finished_at"])
    coverage = observation["coverage"]
    complete = (observation.get("identity_verified") is True
        and coverage.get("account", {}).get("status") == "OBSERVED"
        and coverage.get("get_conversations_v2", {}).get("status") == "BOUNDED_SCAN_COMPLETE"
        and coverage.get("chatrooms", {}).get("status") == "BOUNDED_SCAN_COMPLETE")
    reasons = []
    # An invalid HeyReach session stops the inbox and stats syncing (zero conversations, zero
    # acceptances), so only a valid one counts, as on every send path (review 2026-09-30).
    if not any(read["operation"] == "get_linked_in_account_by_id" and isinstance(read.get("raw"), dict)
               and read["raw"].get("authIsValid") is True for read in observation["observations"]):
        reasons.append("PROVIDER_AUTH_INVALID")
    # A zero-room read is only as good as the lead lookup behind it: get_lead, when
    # attempted, must be OBSERVED, and the conversation listing by the person's HeyReach lead id must
    # have run and completed (owner review, 2026-09-30).
    if "get_lead" in coverage and coverage["get_lead"].get("status") != "OBSERVED":
        reasons.append("PROVIDER_LEAD_UNOBSERVED")
    # HeyReach lead ids (numeric linkedin_id, not the ACoAA profile spelling) this read bound to
    # the person: the listing by lead id, and every chatroom it matched to the person.
    listed_ids = {read["arguments"].get("leadLinkedInId") for read in observation["observations"]
                  if read["operation"] == "get_conversations_v2" and read["arguments"].get("leadLinkedInId")}
    lead_ids = set(listed_ids)
    for read in observation["observations"]:
        if read["operation"] == "get_chatroom" and read.get("identity_matches") is True and isinstance(read.get("raw"), dict):
            correspondent = read["raw"].get("correspondentProfile")
            if isinstance(correspondent, dict) and correspondent.get("linkedin_id"):
                lead_ids.add(str(correspondent["linkedin_id"]))
    if (not any(read["operation"] == "get_chatroom" for read in observation["observations"])
            and (not coverage.get("get_conversations_v2", {}).get("provider_lead_alias_used") or len(listed_ids) != 1)):
        reasons.append("MEMBER_ID_CONVERSATION_LOOKUP_NOT_COMPLETE")
    # An unenriched import placeholder (imp_...) cannot list a conversation HeyReach filed under
    # the numeric id, so no closure read counts without a numeric lead id (review 2026-09-30).
    if any(not str(value).isdigit() for value in lead_ids):
        reasons.append("PERSON_LEAD_ID_NOT_ENRICHED")
    # Every closure read lists the person's conversations by lead id (review 2026-09-30): collect
    # does so only when no profile-URL conversation binds, and a second thread filed under the
    # member-id spelling with the same lead id would otherwise go unread.
    if any(read["operation"] == "get_chatroom" for read in observation["observations"]):
        if not lead_ids:
            reasons.append("PERSON_LEAD_ID_NOT_IN_READ")
        elif len(lead_ids) == 1 and not listed_ids:
            reason = _lead_listing_covers_rooms(config, client, observation, profile, next(iter(lead_ids)))
            if reason:
                reasons.append(reason)
    # Every read checks the other identities on file that carry a lead id against the person's.
    member_issues = ([{"identity": None, "reason": "PERSON_LEAD_ID_AMBIGUOUS:" + ",".join(sorted(lead_ids))}]
                     if len(lead_ids) > 1 else
                     _link_member_id_identities(database, profile, next(iter(lead_ids), None), observed))
    if member_issues:
        reasons.append("MEMBER_ID_IDENTITY_UNRESOLVED")

    def prior_event(source, source_id):
        row = database.execute("SELECT payload FROM engagement_events WHERE source=? AND source_id=?", (source, source_id)).fetchone()
        return json.loads(row[0]) if row else None

    decoder = heyreach_schema.Decoder(config["sender_id"], config["sender_profile"],
        clock=lambda: observed, prior_event=prior_event)
    events, rooms = [], []
    for read in observation["observations"]:
        if read["operation"] != "get_chatroom":
            continue
        if read.get("authenticated") is not True or read.get("identity_matches") is not True or read.get("message_coverage") != "OBSERVED_COUNT_MATCHES":
            complete = False
            reasons.append("CHATROOM_UNVERIFIED_OR_INCOMPLETE")
            continue
        try:
            binding = decoder.decode_conversation(read["raw"])
            events.extend(decoder.decode_chatroom(read["raw"], binding))
            rooms.append(read["raw"]["id"])
        except (ValueError, KeyError, TypeError):
            # e.g. HeyReach's room summary lagging its messages: a reply we cannot decode is
            # never read as silence.
            complete = False
            reasons.append("CHATROOM_UNDECODABLE")
    read_ids = set(rooms)
    for read in observation["observations"]:
        if read["operation"] == "get_conversations_v2" and read.get("status") == "OBSERVED":
            read_ids.update(item.get("id") for item in (read["raw"] or {}).get("items") or [] if isinstance(item, dict))
    return observation, observed, complete, reasons, member_issues, events, read_ids

def _unipile_read(port, profile, now, sender_id=None, sender_profile=None):
    """The closure read of a Unipile person over the port: a valid session, every chat with the
    person (complete listing) and each chat's complete history; the person's messages become
    inbound events (one per message, content-keyed like the HeyReach decoder's, built by
    engagement.unipile_message_event exactly as the Unipile inbox reader builds them, so one message
    is one event whichever reads it first), so a reply resets the clock. No lead-id, member-id or
    chatroom-count checks apply here."""
    import followup_execution

    observed = now
    try:
        observation = followup_execution.port_observation(port, profile)
    except linkedin_port.PortError as error:
        # An invalid session, an incomplete page or an unknown person never counts as silence.
        reason = "UNIPILE_READ_" + error.code
        return ({"route": followup_execution.UNIPILE, "error": reason}, observed, False, [reason], [], [], set())
    observation["finished_at"] = observed.isoformat()
    reasons = []
    if observation["account"]["valid"] is not True:
        reasons.append("UNIPILE_SESSION_INVALID")
    events, read_ids = [], set()
    for read in observation["observations"]:
        read_ids.add(read["chat_id"])
        if read["complete"] is not True:
            reasons.append("UNIPILE_CHAT_HISTORY_INCOMPLETE")
            continue
        for message in read["messages"]:
            try:
                occurred = engagement.timestamp(message["createdAt"])
                if (message["sender"] not in {"ME", "CORRESPONDENT"} or not isinstance(message["body"], str)
                        or not message["body"].strip() or occurred > observed):
                    raise ValueError("UNIPILE_MESSAGE_INVALID")
            except (ValueError, KeyError, TypeError):
                # A reply we cannot read is never read as silence.
                reasons.append("UNIPILE_MESSAGE_UNDECODABLE")
                continue
            if message["sender"] != "CORRESPONDENT":
                continue
            events.append(engagement.unipile_message_event(read["chat_id"], message, profile, sender_id,
                                                           sender_profile, observed))
    return observation, observed, not reasons, reasons, [], events, read_ids


def refresh_activity(database, config, client, profile, now, port=None):
    import followup_execution
    import outreach_agent
    import sequence

    profile = "https://" + key(profile)
    now = engagement.timestamp(now)
    # The LinkedIn browser inactivity review is removed (owner, 2026-09-29: "Remove the
    # inactivity check too"). Activity comes from HeyReach reads and our own saved events;
    # whatever they cannot settle stays an explicit review hold, never invented absence.
    unipile = port is not None and followup_execution.unipile_person(database, profile)
    if unipile:
        # A Unipile person (E3.T3) is read over the port, not HeyReach: their chat ids never appear
        # in a HeyReach read (owner, 2026-10-17: "sure").
        observation, observed, complete, reasons, member_issues, events, read_ids = _unipile_read(
            port, profile, now, config.get("sender_id"), config.get("sender_profile"))
    else:
        observation, observed, complete, reasons, member_issues, events, read_ids = _heyreach_read(
            database, config, client, profile)
    observations = []
    saved = engagement.events_for(database, profile)
    # Every conversation we already have on file for this person (any alias) must be in the read. A
    # Unipile follow-up that opened its chat (start_chat) keeps that chat as raw.chat_id, its
    # conversation_id being None (owner-approved, review 2B): it must be in the read too.
    def saved_ids(raw):
        return {raw.get(key) for key in ("conversation_id", "chat_id") if raw.get(key)}
    if any(isinstance(event.get("raw"), dict) and saved_ids(event["raw"]) - set(read_ids) for event in saved):
        reasons.append("SAVED_CONVERSATION_NOT_IN_READ")
    complete = complete and not reasons
    anchor = stored_anchor(database, profile)
    accepted = any(event["kind"] == "accepted" and event.get("verified") is True for event in saved)
    acceptance = {"state": "VERIFIED_SAVED_ACCEPTANCE" if accepted else "UNKNOWN"}
    # Acceptance is observable only through a single-contact campaign (or a saved acceptance);
    # a hand-sent invitation has no such source and goes to the two-read rule in cycle().
    # A Unipile invitation (E3.T3) is observable too: the port reads it (sequence.acceptance).
    observable = accepted
    if not accepted:
        initial = next((event for event in saved if event["kind"] == "initial_sent" and event.get("preparation_action_id")), None)
        invite = port is not None and next((event for event in saved if followup_execution._unipile_invite(event)), None)
        observable = bool(initial) or bool(invite) or bool(anchor and anchor["basis"] == "HEYREACH_SEND_OBSERVED_UPPER_BOUND")
        if invite:
            try:
                state, person = sequence.acceptance(port, profile)
                # NOT_PENDING (declined, withdrawn or expired) is a verified state like PENDING
                # (owner, 2026-10-17: "Yes" to "Declined, withdrawn or expired invites close after
                # 27 days"); UNKNOWN never counts.
                if state not in {"ACCEPTED", "PENDING", "NOT_PENDING"}:
                    raise ValueError("UNIPILE_ACCEPTANCE_" + state)
                accepted = True
                acceptance = {"state": {"ACCEPTED": "ACCEPTED", "PENDING": "VERIFIED_PENDING_UNIPILE_INVITATION",
                                        "NOT_PENDING": "VERIFIED_NOT_PENDING_UNIPILE_INVITATION"}[state]}
                source_id = "acceptance:" + profile_key(profile)
                if state == "ACCEPTED":
                    events.append({"source": sequence.SOURCE, "source_id": source_id, "profile": profile,
                        "kind": "accepted", "verified": True, "occurred_at": observed.isoformat(),
                        "observed_at": observed.isoformat(), "evidence": "unipile-lookup:" + observed.isoformat(),
                        "raw": {"lookup": sequence._jsonable(person.raw),
                                "occurred_at_basis": "FIRST_INDEPENDENT_ACCEPTANCE_OBSERVATION_NOT_ACTUAL_ACCEPTANCE_TIME"}})
                elif state == "PENDING":
                    observations.append({"kind": "PENDING_INVITATION", "observed_at": observed.isoformat(),
                                         "source": "unipile-lookup:" + source_id, "lookup": sequence._jsonable(person.raw)})
            except Exception:
                acceptance = {"state": "UNKNOWN", "blocker": "UNIPILE_ACCEPTANCE_UNVERIFIED"}
        elif initial:
            try:
                followup_execution._campaign_source(database, config, initial)
                outreach_agent.initialize(database)
                database.commit()
                event = followup_execution._read_acceptance(database, config,
                    SimpleNamespace(provider=SimpleNamespace(client=client)),
                    {"profile": profile, "conversation_id": observation.get("selected_conversation_id")},
                    "inactivity:" + profile_key(profile), observed, saved)
                accepted = True
                acceptance = {"state": "ACCEPTED" if event else "VERIFIED_PENDING_SINGLE_CONTACT_CAMPAIGN"}
                if event:
                    events.append(event)
            except Exception:
                acceptance = {"state": "UNKNOWN", "blocker": "ACCEPTANCE_SCOPE_OR_STATS_UNVERIFIED"}
        elif anchor and anchor["basis"] == "HEYREACH_SEND_OBSERVED_UPPER_BOUND":
            # HeyReach first for HeyReach-sent contacts: its campaign stats settle
            # acceptance without a browser review.
            try:
                state, reads = _heyreach_campaign_acceptance(config, client, profile,
                    dict(anchor, aliases=engagement.identity_keys(database, profile)))
                accepted = True
                acceptance = {"state": "VERIFIED_PENDING_SINGLE_CONTACT_CAMPAIGN" if state == "PENDING" else "ACCEPTED",
                              "reads": reads}
                source_id = "campaign-acceptance:" + str(anchor["payload"]["heyreach"]["campaign_id"])
                if state == "ACCEPTED":
                    events.append({"source": "heyreach", "source_id": source_id, "profile": profile,
                        "kind": "accepted", "verified": True, "occurred_at": observed.isoformat(),
                        "observed_at": observed.isoformat(), "evidence": "heyreach-campaign-stats:" + source_id,
                        "raw": {"reads": reads, "occurred_at_basis": "FIRST_INDEPENDENT_ACCEPTANCE_OBSERVATION_NOT_ACTUAL_ACCEPTANCE_TIME"}})
                else:
                    observations.append({"kind": "PENDING_INVITATION", "observed_at": observed.isoformat(),
                                         "source": "heyreach-campaign-stats:" + source_id, "reads": reads})
            except Exception:
                acceptance = {"state": "UNKNOWN", "blocker": "HEYREACH_CAMPAIGN_ACCEPTANCE_UNVERIFIED"}
    if not observable:
        acceptance = {"state": "UNOBSERVABLE_NO_CAMPAIGN_SOURCE"}
    # Zero chatrooms is allowed (owner, 2026-09-30): a pending invitation has none, and on
    # LinkedIn nobody replies to an invitation without accepting it first.
    result = {"state": "COMPLETE" if complete and (accepted or not observable) else "UNKNOWN",
              "acceptance_observable": observable,
              "evidence": ("fresh-unipile-recipient-activity:" if unipile else "fresh-heyreach-recipient-activity:")
              + profile_key(profile),
              "observed_at": observed.isoformat(), "events": events, "observations": observations,
              "raw": {"provider": observation, "acceptance": acceptance},
              "blocker": reasons[0] if reasons else "CURRENT_INBOX_OR_ACCEPTANCE_COVERAGE_UNKNOWN",
              "incomplete_reasons": reasons, "member_id_issues": member_issues}
    return result


def _lead_listing_covers_rooms(config, client, observation, profile, lead_id):
    """List the account's conversations with this lead id (bounded pages, as collect does) and
    return None only when the listing is complete, holds every chatroom this read bound, and
    every conversation in it is one of those chatrooms; the reads are appended to the
    observation for the audit."""
    import outreach_provider
    sender_id = config["sender_id"]
    read_rooms = {read["raw"].get("id") for read in observation["observations"]
                  if read["operation"] == "get_chatroom" and read.get("identity_matches") is True
                  and isinstance(read.get("raw"), dict)}
    items, total, seen = [], None, set()
    for unused in range(5):
        arguments = {"linkedInAccountIds": [sender_id], "leadLinkedInId": lead_id, "offset": len(items), "limit": 100}
        read = {"operation": "get_conversations_v2", "tool": "get_conversations_v2", "arguments": arguments,
                "source": "heyreach:get_conversations_v2", "status": "UNKNOWN", "authenticated": False,
                "raw": None, "purpose": "INACTIVITY_LEAD_ID_LISTING"}
        observation["observations"].append(read)
        try:
            raw = outreach_provider._snapshot(linkedin_port.HeyReachPort(client, sender_id).chats(
                cursor=len(items), lead_id=lead_id).raw)
        except Exception as error:
            read["reason"] = "PROVIDER_READ_FAILED:" + type(error).__name__
            return "LEAD_ID_LISTING_INCOMPLETE"
        read.update(status="OBSERVED", authenticated=True, raw=raw)
        if (not isinstance(raw, dict) or type(raw.get("totalCount")) is not int or not isinstance(raw.get("items"), list)
                or total is not None and raw["totalCount"] != total or len(items) + len(raw["items"]) > raw["totalCount"]
                or not raw["items"] and len(items) < raw["totalCount"]):
            return "LEAD_ID_LISTING_INCOMPLETE"
        total = raw["totalCount"]
        for item in raw["items"]:
            signature = json.dumps(item, sort_keys=True, ensure_ascii=False)
            if signature in seen:  # as collect's REPEATED_LISTING_ITEM: a repeat hides another thread
                read["reason"] = "REPEATED_LISTING_ITEM"
                return "LEAD_ID_LISTING_INCOMPLETE"
            seen.add(signature)
        items.extend(raw["items"])
        if len(items) == total:
            break
    else:
        return "LEAD_ID_LISTING_INCOMPLETE"
    # Both ways: every bound room must be in the listing (an empty or partial listing proves
    # nothing), and every listed conversation must be one of them.
    if not read_rooms <= {item.get("id") for item in items if isinstance(item, dict)}:
        return "LEAD_ID_LISTING_INCOMPLETE"
    person = outreach_provider._profile(profile)
    sender_profile = outreach_provider._profile(config["sender_profile"])
    for item in items:
        if (not isinstance(item, dict) or item.get("id") not in read_rooms
                or not outreach_provider._binding(item, sender_id, sender_profile, person, lead_id=lead_id)):
            return "LEAD_ID_CONVERSATION_NOT_READ"
    return None


def _link_member_id_identities(database, profile, lead_id, observed):
    """Events stored under another spelling (usually a member-id profile spelling,
    linkedin.com/in/ACoAA..., but any spelling) belong to this person when their HeyReach lead id
    (provider_lead_id / correspondent_id, the linkedin_id) equals the lead id this read bound to
    the person; link them through the existing verified-alias table. Every other identity on file
    that carries a lead id is ruled out only by two numeric ids that differ. Returns the
    identities that could not be ruled out or linked, each with its reason; any entry makes the
    read incomplete, so nobody closes on an unresolved identity. A member-id identity with no lead
    id on file blocks every person's read, and a person whose own lead id is not numeric (imp_
    placeholder) is blocked by every identity carrying a lead id (fail-safe)."""
    import research_workflow
    own = set(engagement.identity_keys(database, profile))
    members = {}
    for identity, payload in database.execute("SELECT identity,payload FROM engagement_events"):
        if identity in own:
            continue
        raw = json.loads(payload).get("raw") or {}
        members.setdefault(identity, set()).update(
            str(raw[field]) for field in ("provider_lead_id", "correspondent_id") if raw.get(field))
    # A profile-URL spelling with no lead id is readable on its own; only a member-id spelling
    # with none cannot be told apart from anyone.
    members = {identity: ids for identity, ids in members.items() if ids or provider_id_only([identity])}
    has_aliases = database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='rw_aliases'").fetchone()
    issues = []
    for identity, lead_ids in sorted(members.items()):
        if not lead_ids:
            issues.append({"identity": identity, "reason": "MEMBER_ID_EVENTS_CARRY_NO_LEAD_ID"})
        elif lead_id is None:
            issues.append({"identity": identity, "reason": "PERSON_LEAD_ID_NOT_OBSERVED"})
        elif str(lead_id) in lead_ids:
            if len(lead_ids) != 1 or not has_aliases:
                issues.append({"identity": identity, "reason": "MEMBER_ID_LEAD_IDS_AMBIGUOUS" if has_aliases
                               else "ALIAS_TABLE_MISSING"})
                continue
            try:
                research_workflow.register_alias(database, "contact", "https://" + identity, profile,
                    {"source": "inactivity-heyreach-lead-id", "lead_id": str(lead_id), "observed_at": observed.isoformat()})
            except ValueError as error:
                issues.append({"identity": identity, "reason": "ALIAS_REFUSED:" + str(error)})
        elif not (str(lead_id).isdigit() and all(value.isdigit() for value in lead_ids)):
            # Only two numeric HeyReach linkedin_ids prove different people. An unenriched
            # import placeholder (imp_...) may be the same person HeyReach later filed under
            # the member-id spelling with a numeric id (review 2026-09-30).
            issues.append({"identity": identity, "reason": "LEAD_IDS_NOT_COMPARABLE"})
    return issues


RETRY_MINUTES = (15, 60)
IDENTICAL_BLOCKER_BACKOFF = dt.timedelta(hours=24)


def _rearm_at(planned, current):
    """Next 27-day boundary after an exhausted review of a dated recipient.

    An undated (UNKNOWN) recipient has no due date: it re-arms only when a dated
    anchor is recorded, which changes the attempt key."""
    if planned.get("due_at") in {None, "UNKNOWN"}:
        return None
    due = engagement.timestamp(planned["due_at"])
    periods = max(1, (current - due) // dt.timedelta(days=DAYS) + 1)
    return (due + periods * dt.timedelta(days=DAYS)).isoformat()


def _persist_observations(database, identity, observation):
    for entry in observation.get("observations") or []:
        if not isinstance(entry, dict) or not entry.get("kind") or not entry.get("source"):
            continue
        observed = engagement.timestamp(entry["observed_at"]).isoformat()
        database.execute("INSERT OR IGNORE INTO outreach_inactivity_observations VALUES (?,?,?,?,?)",
                         (identity, observed, entry["kind"], entry["source"], json.dumps(entry, sort_keys=True)))


def _unipile_readable(database, identity, port):
    """True when this person is read over the port (refresh_activity's Unipile branch)."""
    import followup_execution
    return port is not None and bool(followup_execution.unipile_person(database, "https://" + key(identity)))


def cycle(database, config, client, now=None, *, refresh=None, port=None):
    if not enabled(config):
        return {"state": "DISABLED", "items": []}
    if database.in_transaction:
        raise ValueError("Commit before inactivity review")
    initialize(database)
    current = engagement.timestamp(now or dt.datetime.now(dt.timezone.utc))
    refresh = refresh or (lambda profile, observed: refresh_activity(database, config, client, profile, observed, port))
    items, due = [], []
    seen = set()
    for candidate in candidates(database):
        try:
            aliases = engagement.identity_keys(database, candidate)
        except (ValueError, TypeError):
            items.append({"state": "UNKNOWN", "identity": candidate, "blocker": "INVALID_SAVED_RECIPIENT_IDENTITY"})
            continue
        if seen.intersection(aliases):
            continue
        seen.update(aliases)
        # candidates() returns raw stored column values, which are written in several
        # spellings (research_workflow.profile_key keeps "https://www.", orchestrator
        # .profile_key does not). Key every inactivity record in the same canonical
        # space that engagement.identity_keys, engagement_closures and suppressions
        # already use, so the check row written below is the row read back next cycle.
        identity = key(candidate)
        if provider_id_only([identity]):
            # Read a linked member-id person by their profile URL, never by the ACoAA spelling.
            identity = next((alias for alias in aliases if not provider_id_only([alias])), identity)
        planned = plan(database, identity, current)
        # Only DUE recipients are read. ACTIVE ones are not due yet; undated ones
        # (PRIOR_OUTREACH_TIMESTAMP_UNVERIFIED) have no due date until delivery
        # reconciliation dates them (owner, 2026-09-30).
        if planned["state"] != "DUE":
            if planned["state"] in {"UNKNOWN", "NOT_STARTED"}:
                items.append(planned)
            continue
        if provider_id_only(aliases):
            items.append(dict(planned, state="UNKNOWN", blocker="PROVIDER_ID_IDENTITY_UNMAPPED",
                              reason="Known only by a HeyReach member-id profile spelling (ACoAA...); it cannot be read by profile URL and no verified alias links it to one",
                              next_action="Stays DUE (outbound held) until an owner-verified alias to the person's profile URL is registered (research_workflow.register_alias, kind contact)"))
            continue
        if getattr(client, "heyreach_available", True) is False and not _unipile_readable(database, identity, port):
            # RB5 (RL1): a Unipile install has no HeyReach to read a HeyReach-history person with.
            # Skipped, no attempt spent: they stay unclosed (outbound stays held), the fail-safe.
            items.append(dict(planned, state="UNKNOWN", blocker="HEYREACH_PERSON_NOT_READABLE_ON_UNIPILE",
                              next_action="Stays DUE (outbound held): only a HeyReach read could settle this person"))
            continue
        placeholders = ",".join("?" for unused in aliases)
        previous = database.execute("SELECT anchor_at,attempts,checked_at,next_at,state,identity,result FROM outreach_inactivity_checks WHERE identity IN ("
                                    + placeholders + ") ORDER BY checked_at DESC LIMIT 1", aliases).fetchone()
        # Attempts are keyed on (identity, anchor/due date); an exhausted dated review
        # re-arms at the next 27-day boundary instead of waiting forever.
        same_key = bool(previous) and previous[0] == planned["anchor_at"]
        rearmed = (same_key and previous[4] == "EXHAUSTED" and previous[3] is not None
                   and engagement.timestamp(previous[3]) <= current)
        attempts = previous[1] if same_key and not rearmed else 0
        if attempts >= 3 or same_key and not rearmed and previous[3] and engagement.timestamp(previous[3]) > current:
            items.append(dict(planned, state="EXHAUSTED" if attempts >= 3 else "RETRY_WAIT", attempts=attempts,
                              next_at=previous[3], blocker="INACTIVITY_FRESH_ACTIVITY_REVIEW_REQUIRED"))
            continue
        last_blocker = None
        if same_key and not rearmed and previous[4] == "RETRY_WAIT":
            try:
                last_blocker = json.loads(previous[6]).get("blocker")
            except (ValueError, TypeError, AttributeError):
                last_blocker = None
        # stored: keep updating the one existing row for this recipient, even when an
        # alias link later changes which candidate spelling wins, so a second row can
        # never restart the three-attempt cap for the same person.
        due.append((previous[2] if previous else "", identity, planned, attempts, previous[5] if previous else identity, last_blocker))
    for unused, identity, planned, attempts, stored, last_blocker in sorted(due, key=lambda entry: entry[:2])[:2]:
        attempts += 1
        next_at = (current + dt.timedelta(minutes=RETRY_MINUTES[0 if attempts == 1 else 1])).isoformat()
        with database:
            database.execute("INSERT INTO outreach_inactivity_checks VALUES (?,?,?,?,?,?,?) ON CONFLICT(identity) DO UPDATE SET "
                "anchor_at=excluded.anchor_at,attempts=excluded.attempts,checked_at=excluded.checked_at,next_at=excluded.next_at,state=excluded.state,result=excluded.result",
                (stored, planned["anchor_at"], attempts, current.isoformat(), next_at, "CHECKING", json.dumps(planned)))
        observation = None
        try:
            observation = refresh(identity, current)
            if (not isinstance(observation, dict) or observation.get("state") not in {"COMPLETE", "UNKNOWN"}
                    or observation["state"] == "COMPLETE" and not observation.get("evidence")):
                raise ValueError("INCOMPLETE_CURRENT_ACTIVITY_READ")
            reviewed_at = current if now is not None else dt.datetime.now(dt.timezone.utc)
            with engagement.transaction(database):
                for event in observation.get("events", []):
                    if profile_key(event["profile"]) not in engagement.identity_keys(database, identity):
                        raise ValueError("ACTIVITY_RECIPIENT_MISMATCH")
                    engagement.record_event(database, event)
                # Verified partial facts (e.g. a quoted pending invitation) survive an
                # otherwise incomplete review as dated observations.
                _persist_observations(database, identity, observation)
                checked = plan(database, identity, reviewed_at)
                record = {"policy": "27_CALENDAR_DAYS_WITHOUT_RECIPIENT_ENGAGEMENT", "plan": checked, "observation": observation}
                database.execute("INSERT INTO outreach_inactivity_audit VALUES (?,?,?)", (identity, reviewed_at.isoformat(), json.dumps(record)))
            with engagement.transaction(database):
                checked = plan(database, identity, reviewed_at)
                if checked["state"] in {"DUE", "UNKNOWN"}:
                    if observation["state"] != "COMPLETE":
                        if observation.get("capacity_wait") is True:
                            item = dict(checked, state="BUDGET_WAIT", attempts=attempts - 1,
                                next_at=observation["next_at"], blocker=observation["blocker"])
                            database.execute("UPDATE outreach_inactivity_checks SET attempts=?,state=?,next_at=?,result=? WHERE identity=?",
                                (attempts - 1, item["state"], item["next_at"], json.dumps(record), stored))
                            items.append(item)
                            continue
                        raise ValueError(observation.get("blocker", "INCOMPLETE_CURRENT_ACTIVITY_READ"))
                    if checked["state"] == "UNKNOWN":
                        # The refreshed record itself is unusable (e.g. an event observed after now).
                        raise ValueError(checked.get("blocker") or "INACTIVITY_PLAN_UNKNOWN_AFTER_REFRESH")
                    # A conversation the inbox reader could not decode may hold their reply: no
                    # closure, and no first clean read that would start the second window.
                    if engagement.undecodable_conversations(database, identity):
                        raise ValueError("INBOX_CONVERSATION_UNDECODABLE")
                    if observation.get("acceptance_observable") is False and not checked.get("second_window_from"):
                        # Two-read rule (owner, 2026-09-30): acceptance cannot be observed, so this
                        # first complete clean read starts one more 27-day window; a second one closes.
                        database.execute("INSERT OR IGNORE INTO outreach_inactivity_observations VALUES (?,?,?,?,?)",
                            (identity, reviewed_at.isoformat(), FIRST_CLEAN_READ,
                             "inactivity-second-window:" + checked["anchor_at"],
                             json.dumps({"anchor_at": checked["anchor_at"], "evidence": observation.get("evidence"),
                                         "observed_at": observation.get("observed_at")}, sort_keys=True)))
                        item = plan(database, identity, reviewed_at)
                        database.execute("UPDATE outreach_inactivity_checks SET attempts=0,state=?,next_at=NULL,result=? WHERE identity=?",
                                         (item["state"], json.dumps(record), stored))
                        items.append(item)
                        continue
                    reason = "27 calendar days without verified recipient engagement; last activity " + checked["anchor_at"]
                    if checked.get("second_window_from"):
                        reason += "; acceptance not observable, second clean read 27 days after " + checked["second_window_from"]
                    item = engagement.close_outreach(database, identity, reason, reviewed_at, source="inactivity-policy-27-days")
                else:
                    item = checked
                database.execute("UPDATE outreach_inactivity_checks SET state=?,next_at=NULL,result=? WHERE identity=?",
                                 (item["state"], json.dumps(record), stored))
        except Exception as error:
            database.rollback()
            blocker = str(error)
            if attempts >= 3:
                state, retry_at = "EXHAUSTED", _rearm_at(planned, current)
            else:
                state = "RETRY_WAIT"
                # The same failure twice in a row will not fix itself in minutes.
                retry_at = ((current + IDENTICAL_BLOCKER_BACKOFF).isoformat()
                            if last_blocker is not None and blocker == last_blocker else next_at)
            item = dict(planned, state=state, attempts=attempts, blocker=blocker, next_at=retry_at,
                        next_action="Reconcile current recipient activity; outbound contact remains held, not permanently closed on an unknown read")
            if isinstance(observation, dict):
                item.update({field: observation[field] for field in ("incomplete_reasons", "member_id_issues")
                             if observation.get(field)})
            with database:
                database.execute("UPDATE outreach_inactivity_checks SET state=?,next_at=?,result=? WHERE identity=?",
                                 (item["state"], item["next_at"], json.dumps(item), stored))
                database.execute("INSERT INTO outreach_inactivity_audit VALUES (?,?,?)", (identity, current.isoformat(), json.dumps(item)))
        items.append(item)
    return {"state": "PROCESSED", "days": DAYS, "items": items,
            "closed": sum(item["state"] == "CLOSED" for item in items),
            "unresolved_identities": [{"identity": item["identity"], "blocker": item["blocker"],
                                       "detail": item.get("member_id_issues") or item.get("reason")}
                                      for item in items if item.get("blocker") in {"PROVIDER_ID_IDENTITY_UNMAPPED", "MEMBER_ID_IDENTITY_UNRESOLVED"}]}
