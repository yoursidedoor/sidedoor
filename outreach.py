import datetime as dt
import hashlib
import json
import secrets
import urllib.error

import engagement
import owner_seed
from orchestrator import name_key, profile_key, utcnow

# Contacts the user excluded and contacts who get no automatic outreach come from the optional
# owner seed (owner_seed.py; empty for a new install), never from names in the code (E1.T4).
# Suppressions that only record contact history (imported tracker/handoff). For a new contact
# (INITIAL, PREPARE_CAMPAIGN and its launch) they are read once, before the draft is created
# (research_workflow.reserve_contact -> prior_contact_record), and not again at send time (owner,
# 2026-10-01: "We shouldn't be checking at send time. that's too late. we should be checking at
# draft time or even before a draft is created."). User exclusions, reply stops and our own
# "Outreach reserved" reservation still refuse at send time.
HISTORY_SUPPRESSION_REASONS = frozenset({"Handoff historical outreach; never repeat initial contact",
                                         "Previously contacted; no repeat initial outreach"})

# Failed campaign writes and failed launches are retried (owner, 2026-09-30: "for campaign: a
# failed write can be retried. retry limit of 3 with retry back off." and "For Launch remove the
# any error become uncertain and never retries. use retry limit of 3 with backoff."). Three
# attempts in all: the second 15 minutes after the first failure, the third an hour after the
# second. Every retry first reads HeyReach and treats an already-present result as done, so a
# write that failed on our side but landed on HeyReach's is never written twice.
WRITE_ATTEMPT_LIMIT = 3
WRITE_RETRY_BACKOFF_MINUTES = (15, 60)
LAUNCH_RETRY_STATE = "LAUNCH_RETRY_WAIT"
LAUNCH_FAILED_STATE = "LAUNCH_FAILED"


def write_retry_at(failures, now):
    """When the next attempt may run after `failures` failed attempts, or None when none may."""
    if failures >= WRITE_ATTEMPT_LIMIT:
        return None
    minutes = WRITE_RETRY_BACKOFF_MINUTES[min(max(failures, 1), len(WRITE_RETRY_BACKOFF_MINUTES)) - 1]
    return (engagement.timestamp(now) + dt.timedelta(minutes=minutes)).isoformat()


def _error_detail(error):
    # The provider client sanitizes its own messages, so no credential URL reaches this string.
    return type(error).__name__ + ": " + str(error)[:300]


def unresolved_recipient_name(value):
    """True when a purported human name cannot key the name-keyed suppression space.

    A LinkedIn profile key or URL substituted for a display name produces
    ``"name:linkedin.com/in/<slug>"``, a key no suppression row and no exclusion
    literal can ever equal, so every name-keyed suppression, both hardcoded
    exclusion sets and the prior-contact scan silently miss at once. Such a
    value is UNRESOLVED identity, never a name: callers fail closed on it.
    """
    text = "" if value is None else str(value).strip()
    if not text:
        return True
    for candidate in (text, "https://" + text):
        try:
            profile_key(candidate)
        except ValueError:
            continue
        return True
    return "://" in text or "linkedin.com/" in text.casefold()


def _identity_of(profile):
    try:
        return profile_key(profile if "://" in profile else "https://" + profile)
    except (ValueError, TypeError):
        return None


def recipient_names(database, identities, *, extra=()):
    """Every usable human name durably bound to these profile identities.

    Read-only and strictly additive: it can only make a name-keyed suppression
    or exclusion MATCH, never bypass one. Profile-shaped values are rejected so
    a degenerate recipient string cannot re-enter the name space it broke.
    """
    keys = set(identities)
    names = set()

    def add(value):
        if not unresolved_recipient_name(value):
            names.add(name_key(value))

    for value in extra:
        add(value)
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "history" in tables:
        for identity, recipient, profile in database.execute("SELECT identity,recipient,profile FROM history"):
            if identity in keys or (profile and _identity_of(profile) in keys):
                add(recipient)
    if "outreach_queue" in tables:
        for identity, recipient in database.execute("SELECT identity,recipient FROM outreach_queue"):
            if identity in keys:
                add(recipient)
    if "rw_contacts" in tables:
        for identity, value in database.execute("SELECT identity,name FROM rw_contacts"):
            if identity in keys:
                add(value)
    for table in ("drafts", "outreach_actions", "rw_drafts"):
        if table not in tables:
            continue
        for identity, payload in database.execute("SELECT identity,payload FROM " + table):
            if identity not in keys:
                continue
            try:
                add(json.loads(payload).get("recipient"))
            except (ValueError, TypeError, AttributeError):
                continue
    return names


def unattributable_exclusion_names(database, *, reply=False):
    """Name-keyed suppressions no profile identity in this database resolves to.

    These are exactly the rows a failed name lookup can silently miss: the
    exclusion exists only as a name, and nothing binds that name to a profile,
    so profile-anchored matching cannot compensate for the missing name. When
    this set is non-empty an unresolvable recipient must fail CLOSED, because we
    cannot show the recipient is not one of these people. When it is empty there
    is provably nothing for a name lookup to miss.
    """
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "suppressions" not in tables:
        return set()
    bound = set()
    if "history" in tables:
        for recipient, profile in database.execute("SELECT recipient,profile FROM history WHERE profile IS NOT NULL"):
            if _identity_of(profile) and not unresolved_recipient_name(recipient):
                bound.add(name_key(recipient))
    if "rw_contacts" in tables:
        for identity, value in database.execute("SELECT identity,name FROM rw_contacts"):
            if not str(identity).startswith("name:") and not unresolved_recipient_name(value):
                bound.add(name_key(value))
    if "outreach_queue" in tables:
        for identity, recipient in database.execute("SELECT identity,recipient FROM outreach_queue"):
            if not str(identity).startswith("name:") and not unresolved_recipient_name(recipient):
                bound.add(name_key(recipient))
    at_risk = set()
    for identity, reason in database.execute("SELECT identity,reason FROM suppressions WHERE identity LIKE 'name:%'"):
        name = identity[len("name:"):]
        if unresolved_recipient_name(name) or name in bound:
            continue
        if reply and _reply_suppression_allowed(reason):
            continue
        at_risk.add(name)
    return at_risk


def suppression_keys(database, identities, recipient=None, *, names=None):
    """Identity keys plus every name key those identities can resolve to."""
    keys = list(identities)
    resolved = set(names if names is not None else recipient_names(database, identities, extra=(recipient,)))
    if recipient is not None and not unresolved_recipient_name(recipient):
        resolved.add(name_key(recipient))
    return keys + ["name:" + value for value in sorted(resolved)]


def initialize(database):
    database.executescript("""
        CREATE TABLE IF NOT EXISTS outreach_queue (
            identity TEXT PRIMARY KEY, campaign_id INTEGER UNIQUE NOT NULL,
            recipient TEXT NOT NULL, company TEXT NOT NULL,
            state TEXT NOT NULL, payload TEXT NOT NULL, gates TEXT);
        CREATE TABLE IF NOT EXISTS outreach_attempts (
            identity TEXT PRIMARY KEY, campaign_id INTEGER UNIQUE NOT NULL,
            state TEXT NOT NULL, claimed_at TEXT NOT NULL, result TEXT);
        CREATE TABLE IF NOT EXISTS outreach_launch_claims (
            campaign_id INTEGER PRIMARY KEY, identity TEXT NOT NULL,
            sender_id INTEGER, payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
            claimed_at TEXT NOT NULL);
        CREATE TRIGGER IF NOT EXISTS outreach_launch_no_update
            BEFORE UPDATE ON outreach_launch_claims BEGIN
            SELECT RAISE(ABORT,'Immutable campaign launch claim'); END;
        CREATE TRIGGER IF NOT EXISTS outreach_launch_no_delete
            BEFORE DELETE ON outreach_launch_claims BEGIN
            SELECT RAISE(ABORT,'Immutable campaign launch claim'); END;
        CREATE TRIGGER IF NOT EXISTS outreach_launch_no_replace
            BEFORE INSERT ON outreach_launch_claims
            WHEN EXISTS(SELECT 1 FROM outreach_launch_claims WHERE campaign_id=NEW.campaign_id) BEGIN
            SELECT RAISE(ABORT,'Immutable campaign launch claim'); END;
    """)
    with database:
        for attempt in owner_seed.items("prior_outreach_attempts"):
            database.execute("INSERT OR IGNORE INTO outreach_attempts VALUES (?,?,?,?,?)",
                             (attempt["identity"], attempt["campaign_id"], attempt.get("state", "ALREADY_SENT"),
                              utcnow(), json.dumps(attempt.get("evidence") or {})))


def enqueue(database, candidate):
    required = ("recipient", "company", "profile", "message")
    if not all(isinstance(candidate.get(field), str) and candidate[field].strip() for field in required):
        raise ValueError("Prepared campaign requires recipient, company, profile and exact message")
    if not isinstance(candidate.get("campaign_id"), int) or candidate["campaign_id"] <= 0:
        raise ValueError("A prepared provider campaign ID is required")
    identity = profile_key(candidate["profile"])
    if engagement.closure_status(database, identity):
        raise ValueError("OUTREACH_CLOSED")
    if unresolved_recipient_name(candidate["recipient"]):
        raise ValueError("RECIPIENT_IDENTITY_UNRESOLVED")
    keys = suppression_keys(database, engagement.identity_keys(database, identity), candidate["recipient"])
    if any(row[0] not in HISTORY_SUPPRESSION_REASONS for row in database.execute(
            "SELECT reason FROM suppressions WHERE identity IN (" + ",".join("?" for unused in keys) + ")", keys)):
        raise ValueError("Previously contacted or excluded recipient")
    if database.execute("SELECT 1 FROM outreach_attempts WHERE identity=? OR campaign_id=?", (identity, candidate["campaign_id"])).fetchone():
        raise ValueError("Campaign or recipient already attempted; no retry")
    with engagement.transaction(database):
        if engagement.closure_status(database, identity):
            raise ValueError("OUTREACH_CLOSED")
        gates = _new_path_reservations(database, identity, candidate)
        if gates:
            raise ValueError(";".join(gates))
        inserted = database.execute("INSERT OR IGNORE INTO outreach_queue VALUES (?,?,?,?,'PENDING_VALIDATION',?,NULL)",
                                    (identity, candidate["campaign_id"], candidate["recipient"], candidate["company"], json.dumps(candidate)))
    return {"state": "PENDING_VALIDATION" if inserted.rowcount else "ALREADY_QUEUED", "identity": identity}


def local_gates(database, config, pipeline, candidate, now, *, claimed=False, history_clock=None):
    gates = []
    identity = profile_key(candidate["profile"])
    import inactivity_closure
    gates.extend(inactivity_closure.send_gates(database, config, identity, now))
    gates.extend(_new_path_reservations(database, identity, candidate, claimed=claimed))
    if config.get("outreach_mode") != "guarded":
        gates.append("OUTREACH_NOT_ENABLED")
    identities = engagement.identity_keys(database, identity)
    names = recipient_names(database, identities, extra=(candidate["recipient"],))
    if unresolved_recipient_name(candidate["recipient"]) and not names:
        gates.append("RECIPIENT_IDENTITY_UNRESOLVED")
    if names & (owner_seed.excluded_names() | owner_seed.no_automatic_names()):
        gates.append("PRIOR_CONTACT_OR_EXCLUSION")
    keys = suppression_keys(database, identities, candidate["recipient"], names=names)
    for suppression in database.execute("SELECT reason FROM suppressions WHERE identity IN ("
                                        + ",".join("?" for unused in keys) + ")", keys):
        if not (claimed and suppression[0] == "Outreach reserved; never automatically retry"
                or suppression[0] in HISTORY_SUPPRESSION_REASONS):
            gates.append("PRIOR_CONTACT_OR_EXCLUSION")
    if not claimed and database.execute("SELECT 1 FROM outreach_attempts WHERE identity=? OR campaign_id=?", (identity, candidate["campaign_id"])).fetchone():
        gates.append("PRIOR_ATTEMPT_NO_RETRY")
    proof = candidate.get("verification", {})
    # Identity only. TEAM relevance is not a send condition (owner, 2026-09-23: "we should not
    # block an outreach on the receipients team or Job title. The research subsys has already
    # validated it for us no?" and "Remove it all. I dont care."). Whether a person's team is
    # the right one is a judgement research already made; re-demanding it here as a boolean, and
    # again as a source string, refused people who were positively identified.
    if proof.get("identity_verified") is not True:
        gates.append("RECIPIENT_IDENTITY_UNVERIFIED")
    if not proof.get("identity_source"):
        gates.append("RECIPIENT_IDENTITY_SOURCE_MISSING")
    # ONLY A SOLID YES BLOCKS (owner, 2026-09-23: "If a model cannot determine that there has
    # been no prior outreach then outreach should not block. We only block on a solid yes.
    # Unknown is not a block."). Both loops used to refuse on absence: `is not False` gated a
    # null finding, and the coverage flags gated whenever the read could not be completed - and
    # a completed read is exactly what LinkedIn withholds when its Sent-invitations list comes
    # back excerpted, which is how every recipient inspected on 2026-09-23 ended UNKNOWN. A
    # confirmed prior contact, pending invitation, reply or opt-out still refuses; not knowing
    # does not. What the read could not establish is recorded on the item, never as a gate.
    unresolved = [field for field in ("linkedin_messages_checked", "linkedin_sent_invitations_checked")
                  if proof.get(field) is not True]
    unresolved += [field for field in ("prior_outreach", "pending_invitation", "has_reply", "negative_reply")
                   if proof.get(field) is None]
    for field in ("prior_outreach", "pending_invitation", "has_reply", "negative_reply"):
        if proof.get(field) is True:
            gates.append(field.upper() + "_PRESENT")
    try:
        checked = dt.datetime.fromisoformat(proof["verified_at"])
        age = ((history_clock or now) - checked).total_seconds()
        if not 0 <= age <= config["outreach_history_max_age_seconds"]:
            gates.append("LINKEDIN_HISTORY_CHECK_EXPIRED")
    except (KeyError, TypeError, ValueError):
        gates.append("LINKEDIN_HISTORY_TIMESTAMP_MISSING")
    if not proof.get("history_evidence"):
        gates.append("LINKEDIN_HISTORY_EVIDENCE_MISSING")
    # The historical-recipient check (contact_usage candidate_historical) is not repeated at
    # launch: contact history was checked once, before the draft (owner, 2026-10-01).
    # COMPANY_CONTACT_LIMIT deleted (owner, 2026-09-28): "there should be no life time limit. The
    # only limit is at most 3 people per research run per company. provided we havent reached out
    # to them before". The per-run limit is applied where research admits people; this check
    # counted everyone ever contacted at the company. Prior contact is still refused just above.
    return gates


def provider_gates(client, config, candidate):
    campaign_id = candidate["campaign_id"]
    campaign = client.call("get_campaign", {"campaignId": campaign_id})
    sender = client.call("get_linked_in_account_by_id", {"accountId": config["sender_id"]})
    sequence = client.call("get_campaign_sequence", {"campaignId": campaign_id})
    stats = client.call("get_overall_stats", {"campaignIds": [campaign_id]})
    history = client.call("get_campaigns_for_lead", {"profileUrl": candidate["profile"], "limit": 100})
    inbox = client.call("get_conversations_v2", {"linkedInAccountIds": [config["sender_id"]], "leadProfileUrl": candidate["profile"], "limit": 100})
    gates = []
    if campaign.get("id") != campaign_id or campaign.get("campaignAccountIds") != [config["sender_id"]]:
        gates.append("CAMPAIGN_OR_SENDER_MISMATCH")
    if campaign.get("status") != "DRAFT" or campaign.get("startedAt"):
        gates.append("CAMPAIGN_ALREADY_STARTED_OR_NOT_DRAFT")
    exclusions = ("excludeInOtherCampaigns", "excludeHasOtherAccConversations", "excludeContactedFromSenderInOtherCampaign")
    if candidate.get("preparation_action_id"):
        exclusions += ("excludeFromCompanyBlacklist", "excludeFromLeadBlacklist")
    for field in exclusions:
        if campaign.get(field) is not True:
            gates.append("PROVIDER_DUPLICATE_EXCLUSION_DISABLED")
    # isActive tracks activeCampaigns > 0 (every stored read agrees), not account health
    if sender.get("id") != config["sender_id"] or sender.get("authIsValid") is not True:
        gates.append("SENDER_NOT_READY")
    if sender.get("connectionRequestCooldown") is not False or sender.get("connectionNoteCooldown") is not False:
        gates.append("SENDER_COOLDOWN_OR_UNKNOWN")
    if not campaign.get("linkedInUserListId"):
        gates.append("LEAD_LIST_MISSING")
    else:
        leads = client.call("get_leads_from_list", {"listId": campaign["linkedInUserListId"], "limit": 100})
        items = leads.get("items", [])
        if leads.get("totalCount") != 1 or len(items) != 1 or profile_key(items[0].get("profileUrl", "")) != profile_key(candidate["profile"]):
            gates.append("CAMPAIGN_MUST_CONTAIN_ONLY_VERIFIED_RECIPIENT")
    payload = sequence.get("payload") or {}
    if sequence.get("nodeType") != "CONNECTION_REQUEST" or payload.get("messages") != [candidate["message"]]:
        gates.append("SEQUENCE_OR_EXACT_MESSAGE_MISMATCH")
    if payload.get("fallbackMessage") not in (None, candidate["message"]) or payload.get("toBeWithdrawnAfterDays") is not None:
        gates.append("UNEXPECTED_FALLBACK_OR_WITHDRAWAL")
    for branch_name in ("conditionalNode", "unconditionalNode"):
        branch = sequence.get(branch_name) or {}
        if branch.get("nodeType") != "END" or branch.get("conditionalNode") or branch.get("unconditionalNode"):
            gates.append("AUTOMATIC_FOLLOWUP_NOT_ALLOWED")
    totals = stats.get("overallStats", {})
    if any(type(totals.get(field)) is not int or totals[field] != 0 for field in ("connectionsSent", "messagesSent", "inmailMessagesSent", "uniqueLeadsContacted")):
        gates.append("PRIOR_DELIVERY_OR_UNKNOWN_STATS")
    # A campaign HeyReach SHOWS for this lead, other than ours, refuses. A listing that came back
    # incomplete does not (owner, 2026-09-30: "assuming that the person hasnt been contacted
    # because a snapshot was incomplete is kind of dumb"): it is not evidence of prior contact,
    # and HeyReach's own duplicate exclusions (checked above) still apply to the launch.
    if any(entry.get("id") != campaign_id for entry in history.get("items") or []):
        gates.append("PRIOR_CAMPAIGN_PRESENT")
    # An OBSERVED conversation refuses; an inbox read that returned nothing readable does not
    # (owner, 2026-09-23: "We only block on a solid yes. Unknown is not a block."). `totalCount` absent made
    # `!= 0` true, so a failed or missing provider read blocked exactly like a real conversation.
    if (type(inbox.get("totalCount")) is int and inbox["totalCount"] > 0) or (inbox.get("items") or []):
        gates.append("CONVERSATION_OR_REPLY_PRESENT")
    return sorted(set(gates))


def claim_send(database, candidate, pipeline=None, config=None):
    identity = profile_key(candidate["profile"])
    database.execute("BEGIN IMMEDIATE")
    try:
        queued = database.execute("SELECT state,payload FROM outreach_queue WHERE identity=?", (identity,)).fetchone()
        if not queued or queued[0] not in {"PENDING_VALIDATION", "BLOCKED"} or json.loads(queued[1]) != candidate:
            database.rollback()
            return False
        # Review mode (review fix P1): a campaign launch needs the owner's approval of the PREPARED
        # action's exact payload; the mode is read here, in the transaction that would write the
        # launch claim. A held launch waits as AWAITING_APPROVAL (approvals.waiting lists it).
        import approvals
        held_by = approvals.launch_hold_reason(database, candidate)
        if held_by:
            approvals.mark_launch_held(database, identity, held_by)
            database.commit()
            return False
        if _new_path_reservations(database, identity, candidate):
            database.rollback()
            return False
        # candidate_historical and the history suppressions are not re-read here: contact history
        # was checked once, before the draft (owner, 2026-10-01). The company count refusal
        # (projected > 3) is deleted too: a leftover of the lifetime cap the owner removed on
        # 2026-09-28; the only cap is 3 new people per company per research run (rw_slots).
        if any(row[0] not in HISTORY_SUPPRESSION_REASONS for row in database.execute(
                "SELECT reason FROM suppressions WHERE identity IN (?,?)", (identity, "name:" + name_key(candidate["recipient"])))):
            database.rollback()
            return False
        claimed = database.execute("INSERT OR IGNORE INTO outreach_attempts VALUES (?,?,'CLAIMED',?,NULL)",
                                   (identity, candidate["campaign_id"], utcnow()))
        if claimed.rowcount != 1:
            database.rollback()
            return False
        payload = json.dumps(candidate, sort_keys=True)
        claimed_at = database.execute("SELECT claimed_at FROM outreach_attempts WHERE identity=?", (identity,)).fetchone()[0]
        database.execute("INSERT INTO outreach_launch_claims VALUES (?,?,?,?,?,?)",
                         (candidate["campaign_id"], identity, (config or {}).get("sender_id"), payload,
                          hashlib.sha256(payload.encode()).hexdigest(), claimed_at))
        for suppressed in (identity, "name:" + name_key(candidate["recipient"])):
            database.execute("INSERT OR IGNORE INTO suppressions VALUES (?,'Outreach reserved; never automatically retry')", (suppressed,))
        database.execute("UPDATE outreach_queue SET state='CLAIMED' WHERE identity=?", (identity,))
        database.commit()
        return True
    except BaseException:
        database.rollback()
        raise


def final_campaign_guard(database, config, client, campaign_id, now=None):
    """Read provider state, then check the exact unused immutable launch claim."""
    if database.in_transaction:
        raise ValueError("Commit campaign claim before final provider inspection")
    claim = database.execute("SELECT * FROM outreach_launch_claims WHERE campaign_id=?", (campaign_id,)).fetchone()
    if not claim or claim["sender_id"] != config.get("sender_id"):
        raise ValueError("APPROVED_CAMPAIGN_LAUNCH_CLAIM_REQUIRED")
    candidate = json.loads(claim["payload"])
    if hashlib.sha256(claim["payload"].encode()).hexdigest() != claim["payload_hash"]:
        raise ValueError("CAMPAIGN_LAUNCH_PAYLOAD_CHANGED")
    owner = _prepared_owner(database, candidate)
    if not owner or owner["sender_id"] != config["sender_id"]:
        raise ValueError("EXACT_PREPARED_CAMPAIGN_OWNER_REQUIRED")
    proof = candidate.get("launch_proof", {})
    current = engagement.timestamp(now or dt.datetime.now(dt.timezone.utc))
    # A retried launch runs under its original claim (owner, 2026-09-30), so its proof is judged
    # as of that claim; everything live (provider state, suppressions, stops, closure) is re-read
    # below exactly as for the first launch.
    previous = database.execute("SELECT result FROM outreach_attempts WHERE campaign_id=?", (campaign_id,)).fetchone()
    try:
        previous = json.loads(previous[0]) if previous and previous[0] else {}
    except ValueError:
        previous = {}
    retry = isinstance(previous, dict) and type(previous.get("launch_attempts")) is int and previous["launch_attempts"] >= 1
    proof_clock = engagement.timestamp(claim["claimed_at"]) if retry else current
    # provider_snapshot_complete is no longer required (owner, 2026-09-30): an incomplete HeyReach
    # read is not evidence of prior contact. What the read DID show still refuses below.
    if (proof.get("identity") not in engagement.identity_keys(database, claim["identity"])
            or proof.get("sender_id") != config["sender_id"] or proof.get("agent_decision") not in PROCEED_DECISIONS
            or proof.get("route") != "INVITATION"
            or proof.get("capability_gates") or not proof.get("evidence")
            or not _fresh(proof.get("checked_at"), proof_clock, config.get("outreach_history_max_age_seconds", 3600))
            # Owner, 2026-09-23: "If a model cannot determine that there has been no prior outreach then outreach should not block. We only block on a solid yes. Unknown is not a block."
            # The launch proof asked the same two absence questions as promotion: coverage
            # complete, and every finding explicitly False. Sender readiness and entitlement stay
            # required - those are about OUR account, not about what a read could establish.
            or any(proof.get(field) is not True for field in ("sender_ready", "entitlement_verified"))
            or any(proof.get(field) is True for field in
                   ("prior_outreach", "pending_invitation", "has_reply", "negative_reply", "opt_out", "cooldown"))):
        raise ValueError("FRESH_CAMPAIGN_LAUNCH_PROOF_REQUIRED")
    gates = provider_gates(client, config, candidate)
    if gates:
        raise ValueError(";".join(gates))
    current = engagement.timestamp(now or dt.datetime.now(dt.timezone.utc))
    with engagement.transaction(database):
        attempt = database.execute("SELECT identity,state,claimed_at FROM outreach_attempts WHERE campaign_id=?", (campaign_id,)).fetchone()
        queued = database.execute("SELECT payload,state FROM outreach_queue WHERE campaign_id=?", (campaign_id,)).fetchone()
        if (not attempt or attempt["state"] != "CLAIMED" or attempt["identity"] != claim["identity"]
                or attempt["claimed_at"] != claim["claimed_at"] or not queued or queued["state"] != "CLAIMED"
                or json.loads(queued["payload"]) != candidate):
            raise ValueError("UNUSED_EXACT_CAMPAIGN_LAUNCH_CLAIM_REQUIRED")
        # Judged now: suppressions, stops, closure, inactivity and prior contact. Only the claim's
        # frozen history timestamp (verification.verified_at) is judged at proof_clock: the launch
        # claim and its payload are immutable, so on a retry that timestamp cannot be refreshed and
        # would otherwise expire the third attempt (15 + 60 minutes > the 1-hour history age).
        gates = local_gates(database, config, config.get("pipeline", {"companies": [], "rules": {"max_unique_contacts_per_company": 3}}),
                            candidate, current, claimed=True, history_clock=proof_clock)
        if gates:
            raise ValueError(";".join(gates))
        result = dict(previous if isinstance(previous, dict) else {},
                      final_guard={"checked_at": current.isoformat(), "payload_hash": claim["payload_hash"],
                                   "sender_id": claim["sender_id"], "campaign_id": campaign_id})
        database.execute("UPDATE outreach_attempts SET result=? WHERE campaign_id=? AND state='CLAIMED'",
                         (json.dumps(result), campaign_id))
    return result["final_guard"]


def dispatch(database, config, pipeline, client, validate_only=False, *, campaign_ids=None):
    initialize(database)
    result = {"mode": config.get("outreach_mode"), "validation_only": validate_only,
              "launch_attempts": 0, "delivery_confirmed": 0, "items": [], "gates": []}
    import approvals
    approvals.release_unreadable_holds(database)
    queue = database.execute("SELECT * FROM outreach_queue WHERE state IN ('PENDING_VALIDATION','BLOCKED')").fetchall()
    if campaign_ids is not None:
        queue = [row for row in queue if row["campaign_id"] in campaign_ids]
    if not queue:
        result["state"] = "BLOCKED"
        result["gates"] = ["NO_VERIFIED_UNCONTACTED_RECIPIENT_WITH_PREPARED_SINGLE_CONTACT_CAMPAIGN"]
        result["details"] = "No eligible prepared campaign is queued. Consult current research qualification, history investigation and preparation results for each recipient; existing sends and suppressions remain protected."
        return result
    for row in queue:
        candidate = json.loads(row["payload"])
        provider_error = None
        gates = local_gates(database, config, pipeline, candidate, dt.datetime.now(dt.timezone.utc))
        if not gates:
            try:
                gates = provider_gates(client, config, candidate)
            except Exception as error:
                # Same swallow as the launch below: a provider read that failed for an
                # authentication reason and one that failed for a transport reason produced
                # the identical gate and no way to tell them apart afterwards.
                provider_error = type(error).__name__ + ": " + str(error)[:300]
                gates = ["LIVE_PROVIDER_RECONCILIATION_FAILED"]
        if not gates:
            gates = local_gates(database, config, pipeline, candidate, dt.datetime.now(dt.timezone.utc))
        item = {"recipient": candidate["recipient"], "campaign_id": candidate["campaign_id"], "gates": gates}
        if provider_error:
            item["provider_error"] = provider_error
        result["items"].append(item)
        if gates:
            with database:
                database.execute("UPDATE outreach_queue SET state='BLOCKED',gates=? WHERE identity=? AND state IN ('PENDING_VALIDATION','BLOCKED')", (json.dumps(gates), row["identity"]))
            item["state"] = "BLOCKED"
            continue
        if validate_only:
            item["state"] = "READY"
            continue
        if not claim_send(database, candidate, pipeline, config):
            held = database.execute("SELECT state FROM outreach_queue WHERE identity=?", (row["identity"],)).fetchone()
            item["state"] = approvals.AWAITING if held and held[0] == approvals.AWAITING else "ALREADY_CLAIMED_NO_RETRY"
            continue
        result["launch_attempts"] += 1
        _launch(database, client, row["identity"], candidate, item)
    result["state"] = "BLOCKED" if all(item.get("state") == "BLOCKED" for item in result["items"]) else "PROCESSED"
    return result


def _launch_history(database, campaign_id):
    row = database.execute("SELECT result FROM outreach_attempts WHERE campaign_id=?", (campaign_id,)).fetchone()
    try:
        saved = json.loads(row[0]) if row and row[0] else {}
    except ValueError:
        saved = {}
    saved = saved if isinstance(saved, dict) else {}
    attempts = saved.get("launch_attempts")
    errors = saved.get("launch_errors")
    return {"launch_attempts": attempts if type(attempts) is int and attempts >= 0 else 0,
            "launch_errors": errors if isinstance(errors, list) else []}


LAUNCH_UNREADABLE_CHECK_LIMIT = 6


def _observe_launch(client, campaign_id, now):
    """Read our campaign: ("STARTED" | "DRAFT" | "UNREADABLE", observation). Never raises."""
    fields = ("connectionsSent", "messagesSent", "inmailMessagesSent", "uniqueLeadsContacted")
    try:
        campaign = client.call("get_campaign", {"campaignId": campaign_id})
        stats = client.call("get_overall_stats", {"campaignIds": [campaign_id]})
    except Exception as error:
        return "UNREADABLE", {"error": _error_detail(error), "observed_at": engagement.timestamp(now).isoformat()}
    totals = stats.get("overallStats", {}) if isinstance(stats, dict) else {}
    totals = totals if isinstance(totals, dict) else {}
    campaign = campaign if isinstance(campaign, dict) else {}
    observed = {"campaign_status": campaign.get("status"), "started_at": campaign.get("startedAt"),
                "stats": {field: totals.get(field) for field in fields},
                "observed_at": engagement.timestamp(now).isoformat()}
    if (campaign.get("status") not in (None, "DRAFT") or campaign.get("startedAt")
            or any(type(totals.get(field)) is int and totals[field] > 0 for field in fields)):
        return "STARTED", observed
    if campaign.get("status") == "DRAFT" and all(type(totals.get(field)) is int for field in fields):
        return "DRAFT", observed
    return "UNREADABLE", observed


def _launch(database, client, identity, candidate, item):
    """One launch of a claimed campaign; a failure waits for a bounded retry, never a blind one.

    Owner, 2026-09-30: "For Launch remove the any error become uncertain and never retries. use
    retry limit of 3 with backoff." Any error used to become UNCERTAIN_NO_RETRY for good, even a
    guard refusal before HeyReach was ever called. Now a failed launch waits (LAUNCH_RETRY_WAIT)
    and retry_launches tries again under the same claim after reading HeyReach; the third failure
    is terminal (LAUNCH_FAILED) with every error kept, after one final _observe_launch read (a
    campaign shown started is recorded ACKNOWLEDGED_DELIVERY_UNCONFIRMED instead).
    """
    history = _launch_history(database, candidate["campaign_id"])
    number = history["launch_attempts"] + 1
    item["launch_attempts"] = number
    item["launch_errors"] = list(history["launch_errors"])
    try:
        acknowledgment = client.start_claimed_campaign(database, candidate["campaign_id"])
        item["state"] = "ACKNOWLEDGED_DELIVERY_UNCONFIRMED"
        item.pop("next_at", None)
        # Record what the provider actually SAID (owner, 2026-09-23: "do you have the heyreach
        # response?" - no, and that was the problem). start_campaign declares no output schema
        # and answers in prose, which is why json.loads used to throw on a successful send.
        # Storing the acknowledgment means the next send is evidence rather than inference.
        if isinstance(acknowledgment, dict):
            item["provider_acknowledgment"] = {key: str(value)[:300] for key, value in acknowledgment.items()}
        elif acknowledgment is not None:
            item["provider_acknowledgment"] = str(acknowledgment)[:300]
    except Exception as error:
        # Record WHY (owner, 2026-09-23): a launch that started the campaign and one that was
        # refused must stay distinguishable. The retry reads HeyReach before launching again.
        failed_at = dt.datetime.now(dt.timezone.utc).isoformat()
        item["error"] = _error_detail(error)
        item["error_at"] = failed_at
        item["launch_errors"].append({"attempt": number, "at": failed_at, "error": item["error"]})
        item["next_at"] = write_retry_at(number, failed_at)
        item["state"] = LAUNCH_RETRY_STATE if item["next_at"] else LAUNCH_FAILED_STATE
        if item["state"] == LAUNCH_FAILED_STATE and hasattr(client, "call"):
            # One last read before terminal: a third launch that timed out after HeyReach started
            # it is recorded for delivery reconciliation, not buried as failed.
            outcome, observed = _observe_launch(client, candidate["campaign_id"], failed_at)
            item["final_observation"] = observed
            if outcome == "STARTED":
                item["state"] = "ACKNOWLEDGED_DELIVERY_UNCONFIRMED"
                item.pop("next_at", None)
    with database:
        database.execute("UPDATE outreach_attempts SET state=?,result=? WHERE identity=?", (item["state"], json.dumps(item), identity))
        database.execute("UPDATE outreach_queue SET state=? WHERE identity=?", (item["state"], identity))
        if item["state"] != LAUNCH_RETRY_STATE:
            # A launch waiting to be retried is not yet history: this row marks the person as
            # contacted, which would refuse the retry itself.
            database.execute("INSERT OR IGNORE INTO history VALUES (?,?,?,?,?)", ("name:" + name_key(candidate["recipient"]), candidate["recipient"], candidate["company"], candidate["profile"], json.dumps(item)))
    return item


def retry_launches(database, config, client, now=None, *, limit=7):
    """Retry failed launches that are due, reading HeyReach first so nobody is invited twice.

    A campaign HeyReach shows as started, or with any send in its stats, is recorded as launched
    (ACKNOWLEDGED_DELIVERY_UNCONFIRMED) for the existing delivery reconciliation instead of being
    launched again. A read that fails spends no attempt: unreadable checks back off (15, 60, 240,
    then 720 minutes) and the LAUNCH_UNREADABLE_CHECK_LIMIT-th (6th) ends the launch LAUNCH_FAILED,
    which stays in delivery reconciliation. Otherwise the same claim is re-armed and
    launched through the same final guard as the first launch, which re-reads the lead's campaigns
    and conversations and refuses on any found.
    """
    initialize(database)
    results = []
    rows = database.execute("""SELECT attempts.identity,attempts.campaign_id,attempts.result,claims.payload
        FROM outreach_attempts attempts JOIN outreach_launch_claims claims USING(campaign_id)
        WHERE attempts.state=? ORDER BY attempts.claimed_at""", (LAUNCH_RETRY_STATE,)).fetchall()
    for row in rows:
        if len(results) >= limit:
            break
        current = engagement.timestamp(now or dt.datetime.now(dt.timezone.utc))
        try:
            saved = json.loads(row["result"] or "{}")
        except ValueError:
            saved = {}
        next_at = saved.get("next_at") if isinstance(saved, dict) else None
        if next_at and engagement.timestamp(next_at) > current:
            continue
        candidate = json.loads(row["payload"])
        campaign_id = row["campaign_id"]
        item = {"recipient": candidate.get("recipient"), "campaign_id": campaign_id, "retry": True}
        results.append(item)
        outcome, observed = _observe_launch(client, campaign_id, current)
        record = dict(saved if isinstance(saved, dict) else {})
        if outcome == "STARTED":
            record.update(state="ACKNOWLEDGED_DELIVERY_UNCONFIRMED", launch_observed_before_retry=observed)
            record.pop("next_at", None)
            with database:
                changed = database.execute("UPDATE outreach_attempts SET state='ACKNOWLEDGED_DELIVERY_UNCONFIRMED',result=? WHERE campaign_id=? AND state=?",
                                           (json.dumps(record), campaign_id, LAUNCH_RETRY_STATE))
                if changed.rowcount == 1:
                    database.execute("UPDATE outreach_queue SET state='ACKNOWLEDGED_DELIVERY_UNCONFIRMED' WHERE campaign_id=?", (campaign_id,))
                    database.execute("INSERT OR IGNORE INTO history VALUES (?,?,?,?,?)", ("name:" + name_key(candidate["recipient"]), candidate["recipient"], candidate["company"], candidate["profile"], json.dumps(record)))
            item.update(state="ACKNOWLEDGED_DELIVERY_UNCONFIRMED", observed=observed,
                        next_action="The earlier launch went through; delivery reconciliation records it. Not launched again.")
            continue
        if outcome == "UNREADABLE":
            # Never relaunch blind. Unreadable checks back off like reads (15, 60, 240, then 720
            # minutes) and spend no launch attempt; the sixth ends the launch terminal. A
            # LAUNCH_FAILED campaign stays in delivery reconciliation, so a late start is recorded.
            checks = record.get("unreadable_checks")
            checks = (checks if type(checks) is int and checks >= 0 else 0) + 1
            record.update(unreadable_checks=checks, last_unreadable=observed)
            terminal = checks >= LAUNCH_UNREADABLE_CHECK_LIMIT
            state = LAUNCH_FAILED_STATE if terminal else LAUNCH_RETRY_STATE
            record["next_at"] = None if terminal else _read_backoff(checks, current)
            record["state"] = state
            with database:
                changed = database.execute("UPDATE outreach_attempts SET state=?,result=? WHERE campaign_id=? AND state=?",
                                           (state, json.dumps(record), campaign_id, LAUNCH_RETRY_STATE))
                if changed.rowcount == 1:
                    database.execute("UPDATE outreach_queue SET state=? WHERE campaign_id=?", (state, campaign_id))
                    if terminal:
                        database.execute("INSERT OR IGNORE INTO history VALUES (?,?,?,?,?)", ("name:" + name_key(candidate["recipient"]), candidate["recipient"], candidate["company"], candidate["profile"], json.dumps(record)))
            item.update(state=state, blocker="LAUNCH_STATE_UNREADABLE", observed=observed,
                        unreadable_checks=checks, next_at=record["next_at"])
            continue
        with engagement.transaction(database):
            changed = database.execute("UPDATE outreach_attempts SET state='CLAIMED' WHERE campaign_id=? AND state=?",
                                       (campaign_id, LAUNCH_RETRY_STATE))
            if changed.rowcount == 1:
                database.execute("UPDATE outreach_queue SET state='CLAIMED' WHERE campaign_id=?", (campaign_id,))
        if changed.rowcount != 1:
            item.update(state="LAUNCH_RETRY_CLAIM_CHANGED")
            continue
        if not candidate.get("preparation_action_id"):
            # start_claimed_campaign runs the full final guard only for prepared campaigns, so the
            # live provider check runs here for any other; a refusal counts as a failed attempt.
            try:
                gates = provider_gates(client, config, candidate)
            except Exception as error:
                gates = ["LIVE_PROVIDER_RECONCILIATION_FAILED:" + type(error).__name__]
            if gates:
                _launch(database, _Refused(";".join(gates), client), row["identity"], candidate, item)
                continue
        _launch(database, client, row["identity"], candidate, item)
    return results


class _Refused:
    """A launch refused before HeyReach; reads still go to the real client."""

    def __init__(self, reason, client):
        self.reason = reason
        self.call = client.call

    def start_claimed_campaign(self, database, campaign_id):
        raise ValueError(self.reason)


def initialize_actions(database):
    """Opt-in action path; does not enable or replace the existing dispatcher."""
    engagement.initialize(database)
    database.executescript("""
        CREATE TABLE IF NOT EXISTS outreach_actions (
            action_id TEXT PRIMARY KEY, identity TEXT NOT NULL, slot TEXT NOT NULL,
            payload TEXT NOT NULL, payload_hash TEXT NOT NULL, state TEXT NOT NULL,
            claimed_at TEXT, result TEXT, UNIQUE(identity, slot));
        CREATE TABLE IF NOT EXISTS outreach_prepared_campaigns (
            campaign_id INTEGER PRIMARY KEY, action_id TEXT UNIQUE NOT NULL,
            identity TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS outreach_action_claims (
            action_id TEXT PRIMARY KEY, payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
            identity TEXT NOT NULL, sender_id INTEGER NOT NULL,
            preflight TEXT NOT NULL, claimed_at TEXT NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS outreach_reply_slot
            ON outreach_actions(slot) WHERE substr(slot,1,6)='REPLY:';
        CREATE INDEX IF NOT EXISTS outreach_actions_state ON outreach_actions(state);
        CREATE INDEX IF NOT EXISTS outreach_actions_claimed_at ON outreach_actions(claimed_at);
        CREATE TABLE IF NOT EXISTS outreach_read_retries (
            action_id TEXT NOT NULL, phase TEXT NOT NULL, state TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, next_at TEXT, last_error TEXT,
            token TEXT, lease_until TEXT, updated_at TEXT NOT NULL,
            PRIMARY KEY(action_id, phase));
        CREATE TRIGGER IF NOT EXISTS outreach_claim_no_update
            BEFORE UPDATE ON outreach_action_claims BEGIN
            SELECT RAISE(ABORT, 'Outreach claim is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS outreach_claim_no_delete
            BEFORE DELETE ON outreach_action_claims BEGIN
            SELECT RAISE(ABORT, 'Outreach claim is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS outreach_claim_no_replace
            BEFORE INSERT ON outreach_action_claims
            WHEN EXISTS (SELECT 1 FROM outreach_action_claims WHERE action_id=NEW.action_id)
            BEGIN SELECT RAISE(ABORT, 'Outreach claim is immutable'); END;
    """)


def _read_status(database, action_id, phase):
    row = database.execute("SELECT phase,state,attempts,next_at,last_error FROM outreach_read_retries WHERE action_id=? AND phase=?",
                           (action_id, phase)).fetchone()
    return dict(row) if row else None


def resume_read(database, action_id, phase, now=None):
    """Explicit recovery after an adapter/proof fix; never restores spent budget.

    A terminal exhaustion requires a reviewed replacement workflow, not an
    automatic budget reset. This operation cannot reset send claims or retries.
    """
    if phase not in {"INSPECTION", "RECONCILIATION"}:
        raise ValueError("Unsupported read phase")
    now = engagement.timestamp(now or dt.datetime.now(dt.timezone.utc)).isoformat()
    with engagement.transaction(database):
        changed = database.execute("""UPDATE outreach_read_retries
            SET state='READY',next_at=NULL,last_error=NULL,updated_at=?
            WHERE action_id=? AND phase=? AND state IN ('ADAPTER_UNAVAILABLE','INVALID_PROOF') AND attempts<3""",
            (now, action_id, phase))
    return changed.rowcount == 1


# Owner, 2026-09-21: "there should be no exhaustion on reads. Only next date, until it is
# reconciled." A read that only OBSERVES costs one read; giving up permanently costs the
# OUTCOME, forever. One contact's connection request was sent and finished on LinkedIn, but
# three delivery reads on 2026-09-19 ran while the HeyReach campaign had not yet executed
# its connection-request node, so each correctly said INDEPENDENT_DELIVERY_PENDING; the
# third wrote READ_RETRY_EXHAUSTED with next_at NULL and _claim_read then refused that row
# forever - no timer, no notification, and resume_read, whose docstring promises "a reviewed
# replacement workflow", has NO production caller anywhere, only tests. The read that kept
# failing is exactly the read that succeeds once the provider finishes sending.
#
# Reads now always get a next date instead. The backoff keeps widening and then holds at a
# ceiling, so an unreconcilable action costs a bounded trickle rather than a spin, and send
# claims and send retries are untouched: this reopens observation, never a second send.
READ_BACKOFF_MINUTES = (15, 60, 240)
READ_BACKOFF_CEILING_MINUTES = 720


def _read_backoff(attempts, now):
    index = min(max(int(attempts or 1), 1), len(READ_BACKOFF_MINUTES)) - 1
    minutes = READ_BACKOFF_MINUTES[index] if attempts <= len(READ_BACKOFF_MINUTES) else READ_BACKOFF_CEILING_MINUTES
    return (now + dt.timedelta(minutes=minutes)).isoformat()


def _claim_read(database, action_id, phase, now):
    with engagement.transaction(database):
        database.execute("INSERT OR IGNORE INTO outreach_read_retries(action_id,phase,state,updated_at) VALUES (?,?,'READY',?)",
                         (action_id, phase, now.isoformat()))
        row = database.execute("SELECT * FROM outreach_read_retries WHERE action_id=? AND phase=?", (action_id, phase)).fetchone()
        if row["state"] == "READING":
            if engagement.timestamp(row["lease_until"]) > now:
                return None
            database.execute("UPDATE outreach_read_retries SET state='RETRY_WAIT',next_at=?,last_error='INTERRUPTED_READ',token=NULL,lease_until=NULL,updated_at=? WHERE action_id=? AND phase=?",
                             (_read_backoff(row["attempts"], now), now.isoformat(), action_id, phase))
            return None
        if row["state"] in {"ADAPTER_UNAVAILABLE", "INVALID_PROOF", "COMPLETE"}:
            return None
        if row["state"] == "READ_RETRY_EXHAUSTED":
            # Written by earlier code; reads no longer exhaust, so resume it on its next date.
            database.execute("""UPDATE outreach_read_retries SET state='RETRY_WAIT',next_at=?,updated_at=?
                WHERE action_id=? AND phase=? AND state='READ_RETRY_EXHAUSTED'""",
                (row["next_at"] or _read_backoff(row["attempts"], now), now.isoformat(), action_id, phase))
            row = database.execute("SELECT * FROM outreach_read_retries WHERE action_id=? AND phase=?",
                                   (action_id, phase)).fetchone()
        if row["next_at"] and engagement.timestamp(row["next_at"]) > now:
            return None
        token = secrets.token_hex(16)
        database.execute("""UPDATE outreach_read_retries SET state='READING',attempts=attempts+1,
            token=?,lease_until=?,next_at=NULL,updated_at=? WHERE action_id=? AND phase=?""",
            (token, (now + dt.timedelta(seconds=600 if phase == "INSPECTION" else 300)).isoformat(), now.isoformat(), action_id, phase))
        return token


# Owner, 2026-10-23: "unknown should never block". A send judge's UNKNOWN (a fact it could not settle)
# goes ahead like READY; only BLOCKED holds a send. Every Python guard still applies (prior contact
# from our records, opt-out, stops, closure, exclusions, duplicate claims). Reconciliation's UNKNOWN
# (whether an attempt was sent) is a different question and still never allows a resend.
PROCEED_DECISIONS = frozenset({"READY", "UNKNOWN"})


def _finish_read(database, action_id, phase, token, now, outcome, error=None, retry_at=None):
    with engagement.transaction(database):
        row = database.execute("SELECT attempts,token,state,lease_until FROM outreach_read_retries WHERE action_id=? AND phase=?", (action_id, phase)).fetchone()
        if (not row or row["token"] != token or row["state"] != "READING"
                or not row["lease_until"] or engagement.timestamp(row["lease_until"]) <= now):
            return False
        attempts = row["attempts"]
        next_at = None
        state = outcome
        if outcome == "RETRY_WAIT" and error in _setup_hold_codes():
            # RB3: a setup hold (license, LinkedIn connection) spends no try and keeps the shortest
            # wait, so the read resumes within the hour of the fix.
            attempts -= 1
            next_at = _read_backoff(1, now)
        elif outcome == "RETRY_WAIT":
            # No exhaustion: a read that could not reconcile gets a later date, never a grave.
            next_at = _read_backoff(attempts, now)
            if (error == "OUTREACH_AI_SHARED_BUDGET_EXHAUSTED" and isinstance(retry_at, dt.datetime)
                    and retry_at.utcoffset() is not None and now < retry_at <= now + dt.timedelta(hours=26)):
                next_at = max(engagement.timestamp(next_at), retry_at).astimezone(dt.timezone.utc).isoformat()
        elif outcome in {"ADAPTER_UNAVAILABLE", "INVALID_PROOF"}:
            attempts -= 1
        elif outcome == "READY":
            attempts = 0
        database.execute("""UPDATE outreach_read_retries SET state=?,attempts=?,next_at=?,last_error=?,
            token=NULL,lease_until=NULL,updated_at=? WHERE action_id=? AND phase=? AND token=?""",
            (state, attempts, next_at, error, now.isoformat(), action_id, phase, token))
        return True


# Read failures that mean "the request did not fit", not "the answer was malformed".
INPUT_BOUND_READ_ERRORS = frozenset({"OUTREACH_AGENT_INPUT_OR_TIME_BOUND",
                                    "FOLLOWUP_INPUT_TOO_LARGE", "FOLLOWUP_JUDGE_BOUND_INVALID"})


def _setup_hold_codes():
    import unipile_port
    return {cls.code for cls in (unipile_port.LicenseRequired, unipile_port.LicenseRefused,
                                 unipile_port.LinkedInNotConnected, unipile_port.SenderNotConfigured)}


def _read_error(error):
    if isinstance(error, TimeoutError) and getattr(error, "code", None) == "OUTREACH_AI_SHARED_BUDGET_EXHAUSTED":
        return "RETRY_WAIT", "OUTREACH_AI_SHARED_BUDGET_EXHAUSTED"
    import unipile_port
    if isinstance(error, unipile_port.SetupHold):
        # RB3 (RL1): the license or the LinkedIn connection, not the proof. A wait that clears by
        # itself once the user fixes it; `_finish_read` spends no try on it.
        return "RETRY_WAIT", error.code
    if isinstance(error, NotImplementedError):
        return "ADAPTER_UNAVAILABLE", "UNSUPPORTED_READ_ADAPTER"
    if isinstance(error, urllib.error.HTTPError):
        transient = error.code in {408, 425, 429} or 500 <= error.code <= 599
        return ("RETRY_WAIT" if transient else "INVALID_PROOF"), "HTTP_" + str(error.code)
    if isinstance(error, (TimeoutError, ConnectionError, urllib.error.URLError)):
        return "RETRY_WAIT", type(error).__name__
    if isinstance(error, ValueError) and str(error) in INPUT_BOUND_READ_ERRORS:
        # An oversized prompt is a TRANSIENT input bound, not a malformed proof (owner,
        # 2026-09-23). Both used to fall through to INVALID_PROOF, which is deliberately
        # terminal - see test_malformed_inspection_and_receipt_are_not_transient_retries, whose
        # contract is that a structurally invalid proof must not burn another live read. That
        # contract is right and is untouched. But these errors mean the packet did not FIT, and
        # the packet varies between runs as the history evidence changes: Robin Vale measured
        # 100,903 bytes on one read and 86,205 on another. Grouping them with malformed proofs
        # buried him and Sasha Lind in a state only a manual resume_read could leave.
        return "RETRY_WAIT", str(error)
    # Keep the MESSAGE, not just the class (owner, 2026-09-23). Every unclassified read failure
    # was stored as the bare word "ValueError", which is why Robin Vale sat at INVALID_PROOF
    # for days with no way to see what actually went wrong. The outcome code the state machine
    # switches on is unchanged; only the recorded detail grows. The provider client sanitizes
    # its own messages, so no credential URL reaches this string.
    detail = str(error).strip()
    return "INVALID_PROOF", type(error).__name__ + (": " + detail[:300] if detail else "")


def _prepared_owner(database, candidate):
    action_id = (candidate or {}).get("preparation_action_id")
    if not action_id:
        return None
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"outreach_actions", "outreach_action_claims", "outreach_prepared_campaigns"} <= tables:
        return None
    row = database.execute("""SELECT actions.state,claims.payload,claims.payload_hash,claims.identity,
        claims.sender_id,prepared.campaign_id FROM outreach_action_claims claims
        JOIN outreach_actions actions ON actions.action_id=claims.action_id
        JOIN outreach_prepared_campaigns prepared ON prepared.action_id=claims.action_id
        WHERE claims.action_id=? AND actions.payload=claims.payload
        AND actions.payload_hash=claims.payload_hash AND actions.identity=claims.identity
        AND prepared.identity=claims.identity""", (action_id,)).fetchone()
    if not row or row[0] != "PREPARED" or row[5] != candidate.get("campaign_id"):
        return None
    action = json.loads(row[1])
    if (action.get("purpose") != "PREPARE_CAMPAIGN" or row[2] != candidate.get("preparation_payload_hash")
            or hashlib.sha256(row[1].encode()).hexdigest() != row[2]
            or row[3] != profile_key(candidate["profile"])
            or any(action.get(field) != candidate.get(field) for field in ("recipient", "company", "message", "list_id"))):
        return None
    return {"action_id": action_id, "action": action, "sender_id": row[4]}


def _confirmed_campaign(database, identity):
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"outreach_attempts", "engagement_events"} <= tables:
        return False
    identities = engagement.identity_keys(database, identity)
    placeholders = ",".join("?" for unused in identities)
    return bool(database.execute("SELECT 1 FROM outreach_attempts WHERE identity IN (" + placeholders
        + ") AND state='CONFIRMED_SENT'", identities).fetchone()
        and any(event["kind"] == "initial_sent" and event.get("verified") is True
                for event in engagement.events_for(database, identity)))


def _followup_suppression_allowed(database, identity, reason):
    return reason in {"Handoff historical outreach; never repeat initial contact",
                      "Previously contacted; no repeat initial outreach"} or (
        reason == "Outreach reserved; never automatically retry" and _confirmed_campaign(database, identity))


def _immutable_claim_gates(database, identity, action_id=None, *, continuation=False, prepared_owner=None, reply=False):
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "outreach_action_claims" not in tables:
        return []
    identities = engagement.identity_keys(database, identity)
    placeholders = ",".join("?" for unused in identities)
    for row in database.execute("""SELECT claims.action_id,actions.state,claims.payload
            FROM outreach_action_claims claims LEFT JOIN outreach_actions actions USING(action_id)
            WHERE claims.identity IN (""" + placeholders + ")", identities):
        if row[0] in {action_id, prepared_owner}:
            continue
        delivered_preparation = (row[1] == "PREPARED" and {"outreach_prepared_campaigns", "outreach_attempts"} <= tables
            and database.execute("""SELECT 1 FROM outreach_prepared_campaigns prepared
                JOIN outreach_attempts attempts USING(campaign_id)
                WHERE prepared.action_id=? AND attempts.state='CONFIRMED_SENT'""", (row[0],)).fetchone()
            and _confirmed_campaign(database, identity))
        if continuation and (row[1] == "CONFIRMED_SENT" or delivered_preparation):
            continue
        # A newer inbound message gets its own reply even when an earlier REPLY to the person ended
        # CANCELLED (owner-approved fix, E3.T7 part 2). Only for a REPLY, only an earlier REPLY
        # claim, only CANCELLED. The cancelled action itself is never resent: dispatch takes only
        # PENDING/BLOCKED rows and retry_reply only UNCERTAIN_NO_RETRY ones.
        if continuation and reply and row[1] == "CANCELLED" and _claim_purpose(row[2]) == "REPLY":
            continue
        return ["IMMUTABLE_OUTREACH_CLAIM_EXISTS"]
    return []


def _claim_purpose(payload):
    try:
        claimed = json.loads(payload)
    except (TypeError, ValueError):
        return None
    return claimed.get("purpose") if isinstance(claimed, dict) else None


def _new_path_reservations(database, identity, candidate=None, *, claimed=False):
    if engagement.closure_status(database, identity):
        return ["OUTREACH_CLOSED"]
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    identities = engagement.identity_keys(database, identity)
    placeholders = ",".join("?" for unused in identities)
    owner = _prepared_owner(database, candidate)
    if candidate and candidate.get("preparation_action_id") and not owner:
        return ["CAMPAIGN_HANDOFF_OWNER_MISMATCH"]
    import outreach_campaign
    gates = outreach_campaign.reservation_gates(database, identity,
        owner["action_id"] if owner else None, candidate.get("campaign_id") if owner else None)
    gates.extend(_immutable_claim_gates(database, identity, prepared_owner=owner["action_id"] if owner else None))
    if gates:
        return gates
    if "outreach_attempts" in tables:
        for attempt in database.execute("SELECT identity,campaign_id,state FROM outreach_attempts WHERE identity IN ("
                                        + placeholders + ")", identities):
            if not (claimed and candidate and attempt[0] == identity and attempt[1] == candidate["campaign_id"]
                    and attempt[2] == "CLAIMED"):
                return ["LEGACY_ATTEMPT_REQUIRES_RECONCILIATION"]
    if "suppressions" in tables:
        for row in database.execute("SELECT reason FROM suppressions WHERE identity IN (" + placeholders + ")", identities):
            if not (claimed and row[0] == "Outreach reserved; never automatically retry"
                    or row[0] in HISTORY_SUPPRESSION_REASONS):
                return ["PRIOR_CONTACT_OR_EXCLUSION"]
    if "engagement_stops" in tables and engagement.stop_state(database, identity):
        return ["REPLY_STOPS_OUTREACH"]
    if "engagement_events" in tables and database.execute("SELECT 1 FROM engagement_events WHERE identity IN (" + placeholders + ") AND kind IN ('initial_sent','followup_sent','outbound_observed')", identities).fetchone():
        return ["VERIFIED_MESSAGE_ALREADY_SENT"]
    if "outreach_actions" in tables and database.execute("SELECT 1 FROM outreach_actions WHERE identity IN (" + placeholders
            + ") AND action_id<>? AND state NOT IN ('CANCELLED','BLOCKED')",
            (*identities, owner["action_id"] if owner else "")).fetchone():
        return ["DURABLE_ACTION_RESERVATION"]
    return []


def contact_usage(database, company, candidate=None, pipeline=None, provider_count=0):
    """Distinct identities across research/history/drafts/queues plus unnamed slots.

    Historical lower bounds are never reduced. A name-only historical row joins
    a profile only when that name maps to one profile; different profile IDs
    with the same display name remain distinct. Research aliases are honored.
    Returned projected includes the candidate exactly once.
    """
    import research_workflow

    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    def company_key(value):
        return research_workflow.canonical(database, "company", value) if "rw_aliases" in tables else research_workflow.name_key(value)
    def person_key(value):
        if not value:
            return None
        url = value if "://" in value else "https://" + value
        canonical = research_workflow.canonical(database, "contact", url) if "rw_aliases" in tables else url
        return profile_key(canonical)
    company = company_key(company)
    entries = []
    def add(value, historical=False):
        if company_key(value["company"]) == company:
            entries.append((person_key(value.get("profile")), research_workflow.name_key(value.get("recipient", "")), historical))
    baseline = provider_count
    for entry in (pipeline or {}).get("companies", []):
        if company_key(entry["company"]) == company:
            additional = [recipient for recipient in (entry.get("additional_recruiting_names") or "").split(",") if recipient.strip()]
            baseline = max(baseline, entry["conservative_known_contact_count"] + len(additional))
            for recipient in additional:
                if recipient.strip():
                    add({"company": company, "recipient": recipient}, True)
    if "history" in tables:
        for row in database.execute("SELECT * FROM history"):
            add(dict(row), True)
    for table in ("drafts", "outreach_queue", "outreach_actions", "rw_drafts"):
        if table not in tables:
            continue
        for row in database.execute("SELECT * FROM " + table):
            values = dict(row)
            if values.get("state") not in {"CANCELLED", "SUPPRESSED"}:
                add(json.loads(values["payload"]), values.get("state") == "CONFIRMED_SENT")
    held = 0
    if "rw_contacts" in tables:
        for row in database.execute("SELECT * FROM rw_contacts"):
            value = dict(row)
            add({"company": value["company"], "recipient": value["name"],
                 "profile": None if value["identity"].startswith("name:") else value["identity"]}, bool(value["historical"]))
        # The baseline floor stays: it is the owner's real prior-contact count from seed_history.
        # The defect was a FAILED research worker accumulating into the same table (owner,
        # 2026-09-23), which is fixed at the source in research_workflow.hold_unreported_contact_slots.
        for row in database.execute("SELECT company,count FROM rw_baselines"):
            if company_key(row[0]) == company:
                baseline = max(baseline, row[1])
        for row in database.execute("SELECT company,remaining FROM rw_slots"):
            if company_key(row[0]) == company:
                held += row[1]
    if candidate:
        candidate_entry = (person_key(candidate.get("profile")), research_workflow.name_key(candidate["recipient"]), False)
    names = {}
    for identity, recipient, historical in entries + ([candidate_entry] if candidate else []):
        if identity and recipient:
            names.setdefault(recipient, set()).add(identity)
    def resolved(entry):
        identity, recipient, historical = entry
        matches = names.get(recipient, set())
        return identity or (next(iter(matches)) if len(matches) == 1 else "name:" + recipient)
    historical_ids = {resolved(entry) for entry in entries if entry[2]}
    all_ids = {resolved(entry) for entry in entries}
    used = max(baseline, len(historical_ids)) + len(all_ids - historical_ids) + held
    return {"company": company, "used": used, "projected": used + int(bool(candidate) and resolved(candidate_entry) not in all_ids),
            "candidate_historical": bool(candidate) and resolved(candidate_entry) in historical_ids,
            "historical_lower_bound": baseline, "distinct_historical": len(historical_ids),
            "distinct_reserved": len(all_ids - historical_ids), "held_slots": held, "limit": 3}


MANAGED_BASES = ("HARNESS_OUTREACH", "OWNER_CONTACTED")


def initialize_managed(database):
    """Conversations the harness is allowed to manage.

    Owner, 2026-09-23: "i REACHED OUT to them manually and i want the harness to manage their
    conversations. But after that, only scope to outreach."

    Before this there was no such fence. `engagement` ingests the whole authenticated LinkedIn
    inbox and the only scope notion anywhere was `job_scope`, which means a research-verified
    person/employer/requisition binding - a different question entirely. Live proof of the gap:
    8 distinct people had inbound messages in the pipeline and NONE of them was someone the
    harness had contacted, while the two it had contacted had no inbound events at all. The one
    reply ever sent went to a correspondent in that first group.

    Membership is either basis in MANAGED_BASES: HARNESS_OUTREACH is derived live from the
    harness's own send records, so it needs no row and cannot drift; OWNER_CONTACTED is a
    recorded row, because nothing in this database can otherwise know the owner messaged
    someone by hand.
    """
    database.execute("""CREATE TABLE IF NOT EXISTS managed_conversations (
        identity TEXT PRIMARY KEY, basis TEXT NOT NULL, note TEXT, recorded_at TEXT NOT NULL)""")


def managed_conversation(database, profile):
    """Is this correspondent one the harness may act on? Returns the basis, or None.

    A bare ("linkedin.com/in/x") and a URL profile are the same person (review, 2026-10-01)."""
    identity = _identity_of(profile) if isinstance(profile, str) and profile else None
    if not identity:
        return None
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ("outreach_attempts", "outreach_queue"):
        if table in tables and database.execute(
                "SELECT 1 FROM %s WHERE identity=? LIMIT 1" % table, (identity,)).fetchone():
            return "HARNESS_OUTREACH"
    # A Unipile invitation (E3.T3) is an INVITE action, not an outreach_attempts row; once a read
    # showed it, the person is the harness's outreach as a campaign invitee is.
    if "outreach_actions" in tables and database.execute(
            """SELECT 1 FROM outreach_actions WHERE identity=? AND state='CONFIRMED_SENT'
            AND json_extract(payload,'$.purpose')='INVITE' LIMIT 1""", (identity,)).fetchone():
        return "HARNESS_OUTREACH"
    if "managed_conversations" in tables:
        row = database.execute("SELECT basis FROM managed_conversations WHERE identity=?", (identity,)).fetchone()
        if row:
            return row[0]
    return None


# Records that make a person "tracked in the harness" (owner, 2026-10-01: "cold strangers should be
# fully skipped"). Each table's `identity` column holds a profile (bare or URL form).
TRACKING_TABLES = ("managed_conversations", "outreach_queue", "outreach_attempts", "outreach_actions",
                   "outreach_action_claims", "outreach_launch_claims", "outreach_campaign_preparations",
                   "outreach_prepared_campaigns", "outreach_owner_records", "drafts", "strategy_drafts",
                   "outreach_inactivity_anchors", "rw_contacts")
# A reply's own action and claim rows are written under whatever spelling HeyReach filed the
# sender under; they answer a message and never make anyone tracked (review, 2026-10-01: once a
# reply to a member-id spelling was promoted, its own rows made that spelling "tracked" and its
# resolved person vanished).
_NOT_REPLY = {table: " WHERE NOT (json_valid(payload) AND json_extract(payload,'$.purpose')='REPLY')"
              for table in ("outreach_actions", "outreach_action_claims")}
# Our sends recorded by the harness. outbound_observed is not here: a message the owner wrote by
# hand to a stranger who wrote first does not make that stranger tracked.
HARNESS_SEND_KINDS = ("initial_sent", "followup_sent")
# The suppression every inbound message writes for its own sender; it is not tracking.
INBOUND_SUPPRESSION_PREFIX = "LinkedIn reply stops blind outreach"


SENT_SUPPRESSION = "Handoff historical outreach; never repeat initial contact"


def _reply_send_suppressions(database, tables):
    """The suppression keys a REPLY send writes for its own recipient (execute_claimed writes
    SENT_SUPPRESSION for the profile and the recipient's name key). Like the reply's own action
    rows they answer a message and do not make the sender spelling tracked."""
    keys = set()
    if "outreach_actions" not in tables:
        return keys
    for (payload,) in database.execute("""SELECT payload FROM outreach_actions WHERE json_valid(payload)
            AND json_extract(payload,'$.purpose')='REPLY'"""):
        try:
            action = json.loads(payload)
            keys.add(profile_key(action["profile"]))
            keys.add("name:" + name_key(action["recipient"]))
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    return keys


def tracked_person(database, profile):
    """Why our own records track this person (any alias), or None for a cold stranger.

    Owner, 2026-10-01: "cold strangers should be fully skipped. Dont generate draft for me." A
    person is tracked when any local record names them: a managed (owner-contacted) conversation,
    the harness's outreach (queue, attempts, actions and claims, launch claims, campaign
    preparations, drafts, owner records, inactivity anchors), the harness's
    own recorded sends (initial_sent/followup_sent), the imported contact history, a research
    contact (rw_contacts), a referral, or a suppression other than the one their own inbound
    message writes. An owner's hand message alone (outbound_observed) is not tracking. Read-only.
    """
    import engagement as engagement_records
    try:
        identities = set(engagement_records.identity_keys(database, profile))
    except (ValueError, TypeError, AttributeError):
        return None
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in TRACKING_TABLES:
        if table in tables and "identity" in {row[1] for row in database.execute("PRAGMA table_info(" + table + ")")}:
            for (value,) in database.execute("SELECT DISTINCT identity FROM " + table + _NOT_REPLY.get(table, "")):
                if isinstance(value, str) and (value in identities or _identity_of(value) in identities):
                    return table
    if "referrals" in tables:
        for (value,) in database.execute("SELECT contact_id FROM referrals"):
            if isinstance(value, str) and (value in identities or _identity_of(value) in identities):
                return "referrals"
    if "engagement_events" in tables:
        placeholders = ",".join("?" for unused in identities)
        if database.execute("SELECT 1 FROM engagement_events WHERE identity IN (" + placeholders + ") AND kind IN (?,?)",
                            (*identities, *HARNESS_SEND_KINDS)).fetchone():
            return "engagement_events"
    if "history" in tables:
        for identity, recorded in database.execute("SELECT identity,profile FROM history"):
            if identity in identities or (isinstance(recorded, str) and _identity_of(recorded) in identities):
                return "history"
    if "suppressions" in tables:
        names = {"name:" + name for name in recipient_names(database, identities)}
        written_by_replies = _reply_send_suppressions(database, tables)
        for identity, reason in database.execute("SELECT identity,reason FROM suppressions"):
            if not isinstance(identity, str) or str(reason or "").startswith(INBOUND_SUPPRESSION_PREFIX):
                continue
            if reason == SENT_SUPPRESSION and identity in written_by_replies:
                continue
            bare = identity[len("name:"):] if identity.startswith("name:") else identity
            if identity in identities or identity in names or _identity_of(bare) in identities:
                return "suppressions"
    return None


def hand_answered(database, profile):
    """True when a message from our side was observed in this person's conversation (any alias):
    the owner answering by hand, or one of our own sends seen in the inbox."""
    import engagement as engagement_records
    return any(event.get("kind") == "outbound_observed" for event in engagement_records.events_for(database, profile))


def _own_basis(database, profile):
    """managed_conversation for this profile or any of its rw_aliases aliases."""
    import engagement as engagement_records
    basis = managed_conversation(database, profile)
    if basis:
        return basis
    for identity in engagement_records.identity_keys(database, profile):
        basis = managed_conversation(database, "https://" + identity)
        if basis:
            return basis
    return None


def reply_conversation(database, profile, lead_id=None, conversation_id=None):
    """The basis on which the harness may reply in this conversation, or None.

    managed_conversation as before (HARNESS_OUTREACH, OWNER_CONTACTED), plus (owner, 2026-10-01:
    "People I've answered by hand who are also being tracked in the harness can get auto replies
    in the future") TRACKED_OWNER_ANSWERED: a tracked person (tracked_person) whose conversation
    shows a message from our side. A stranger the owner answered by hand stays untracked. A sender
    HeyReach files under another spelling (an ACoA member id) is judged as the tracked person
    resolve_sender finds for it by lead id or conversation."""
    try:
        basis = _own_basis(database, profile)
        if basis:
            return basis
        if tracked_person(database, profile) and hand_answered(database, profile):
            return "TRACKED_OWNER_ANSWERED"
        resolved = resolve_sender(database, profile, lead_id, conversation_id)
    except (ValueError, TypeError, AttributeError):
        return None
    person = resolved.get("person")
    if resolved["state"] != "TRACKED" or not person or person == _identity_of(profile):
        return None
    return person_basis(database, profile, person)


def person_basis(database, profile, person):
    """The reply basis for a sender spelling answered as the tracked person it resolved to (a
    bare or URL identity), or None: that person's managed basis, or TRACKED_OWNER_ANSWERED when
    either spelling's conversation shows a message from our side."""
    person = _identity_of(person) if isinstance(person, str) else None
    if not person:
        return None
    try:
        basis = _own_basis(database, "https://" + person)
        if basis:
            return basis
        if tracked_person(database, "https://" + person) and (
                hand_answered(database, profile) or hand_answered(database, "https://" + person)):
            return "TRACKED_OWNER_ANSWERED"
    except (ValueError, TypeError, AttributeError):
        return None
    return None


# HeyReach sometimes files a person under their LinkedIn member id ("ACoAA..." profile URL)
# instead of the vanity URL our records use (review, 2026-10-01).
MEMBER_ID_PREFIX = "acoa"
LEAD_RECORDS = (("engagement_events", ("raw",)), ("outreach_attempts", ("result",)),
                ("outreach_actions", ("payload", "result")), ("outreach_launch_claims", ("payload",)),
                ("outreach_action_claims", ("payload",)))


def _walk(value, found):
    if isinstance(value, dict):
        found.append(value)
        for item in value.values():
            _walk(item, found)
    elif isinstance(value, list):
        for item in value:
            _walk(item, found)


def _sender_links(database):
    """{("lead", LinkedIn member number) | ("conversation", id): {identity}} from our own records.

    A HeyReach lead object (linkedin_id with profileUrl) names its own profile; a correspondent or
    provider lead id and a conversation id name the record's person (its identity column)."""
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    links = {}

    def add(kind, key, identity):
        if key not in (None, "") and identity:
            links.setdefault((kind, str(key)), set()).add(identity)

    for table, columns in LEAD_RECORDS:
        if table not in tables:
            continue
        for row in database.execute("SELECT identity," + ",".join(columns) + " FROM " + table + _NOT_REPLY.get(table, "")):
            owner = _identity_of(row[0]) if isinstance(row[0], str) else None
            for text in row[1:]:
                try:
                    value = json.loads(text) if isinstance(text, str) else None
                except ValueError:
                    continue
                found = []
                _walk(value, found)
                for item in found:
                    if item.get("linkedin_id") is not None:
                        named = _identity_of(item["profileUrl"]) if isinstance(item.get("profileUrl"), str) else None
                        add("lead", item["linkedin_id"], named or owner)
                    for field in ("correspondent_id", "provider_lead_id"):
                        add("lead", item.get(field), owner)
                    for field in ("conversation_id", "conversationId"):
                        add("conversation", item.get(field), owner)
    return links


def resolve_sender(database, profile, lead_id=None, conversation_id=None):
    """Is an inbound sender tracked in the harness? {"state", "person", "basis"}.

    TRACKED: tracked_person matches the sender (any alias), or exactly one tracked person our own
    records tie to the sender's HeyReach lead id or conversation (person is that identity). COLD:
    neither, for a sender with an ordinary profile. UNRESOLVED: no usable identity, an identity
    that cannot be keyed, a member-id spelling (ACoA...) nothing ties to a tracked person, or two
    different tracked people. An UNRESOLVED sender is held, never treated as a stranger."""
    import engagement as engagement_records
    if not isinstance(profile, str) or not profile.strip():
        return {"state": "UNRESOLVED", "person": None, "basis": "SENDER_IDENTITY_MISSING"}
    try:
        identities = set(engagement_records.identity_keys(database, profile))
    except (ValueError, TypeError, AttributeError):
        return {"state": "UNRESOLVED", "person": None, "basis": "SENDER_IDENTITY_INVALID"}
    own = _identity_of(profile)
    basis = tracked_person(database, profile)
    if basis:
        return {"state": "TRACKED", "person": own, "basis": basis}
    links = _sender_links(database)
    candidates = set()
    for kind, key in (("lead", lead_id), ("conversation", conversation_id)):
        if key not in (None, ""):
            candidates |= links.get((kind, str(key)), set())
    tracked = {}
    for candidate in sorted(candidates - identities):
        found = tracked_person(database, "https://" + candidate)
        if found:
            tracked[candidate] = found
    if len(tracked) == 1:
        person, found = next(iter(tracked.items()))
        return {"state": "TRACKED", "person": person, "basis": found}
    if len(tracked) > 1:
        return {"state": "UNRESOLVED", "person": None, "basis": "SENDER_MATCHES_SEVERAL_TRACKED_PEOPLE"}
    if own.split("/in/", 1)[1].startswith(MEMBER_ID_PREFIX):
        return {"state": "UNRESOLVED", "person": None, "basis": "SENDER_MEMBER_ID_UNMATCHED"}
    return {"state": "COLD", "person": None, "basis": None}


def record_managed(database, profile, note, now, basis="OWNER_CONTACTED"):
    """Record that the owner contacted this person by hand. Append-only; never overwrites."""
    import orchestrator
    identity = orchestrator.profile_key(profile) if profile else None
    if not identity or basis not in MANAGED_BASES:
        raise ValueError("MANAGED_CONVERSATION_IDENTITY_AND_BASIS_REQUIRED")
    initialize_managed(database)
    database.execute("INSERT OR IGNORE INTO managed_conversations VALUES (?,?,?,?)",
                     (identity, basis, note, now))
    return identity


def reply_slot(action):
    """One durable response entitlement per account/thread/inbound, across aliases."""
    for field in ("reply_task_id", "reply_source_key", "reply_source_sha256", "reply_source",
                  "reply_source_id", "reply_inbound_at", "conversation_id"):
        if not isinstance(action.get(field), str) or not action[field].strip():
            raise ValueError("Missing reply binding " + field)
    if type(action.get("sender_id")) is not int or action["sender_id"] <= 0:
        raise ValueError("Reply requires a positive sender_id")
    engagement.timestamp(action["reply_inbound_at"])
    binding = json.dumps([action["sender_id"], action["conversation_id"], action["reply_source_id"]],
                         ensure_ascii=False, separators=(",", ":"))
    return "REPLY:" + hashlib.sha256(binding.encode()).hexdigest()


def _reply_gates(database, config, action, proof, now):
    try:
        slot = reply_slot(action)
    except (ValueError, TypeError):
        return ["REPLY_BINDING_INVALID"]
    gates = []
    if config.get("reply_execution", {}).get("enabled") is not True:
        gates.append("REPLY_EXECUTION_NOT_ENABLED")
    if action["sender_id"] != config.get("sender_id"):
        gates.append("REPLY_SENDER_MISMATCH")
    if database.execute("SELECT 1 FROM outreach_actions WHERE slot=? AND action_id<>?",
                        (slot, action["action_id"])).fetchone():
        gates.append("REPLY_SLOT_ALREADY_RESERVED")
    for field in ("reply_source_key", "reply_source_sha256", "reply_source_id", "reply_inbound_at"):
        if proof.get(field) != action[field]:
            gates.append("REPLY_PROOF_BINDING_MISMATCH")
    for field, expected in (("reply_latest_inbound", True), ("reply_already_answered", False),
                            ("reply_context_verified", True), ("reply_transport_supported", True),
                            ("provider_restriction_observed", False)):
        if proof.get(field) is not expected:
            gates.append(field.upper() + "_UNVERIFIED")
    if proof.get("agent_decision") not in PROCEED_DECISIONS:
        gates.append("AGENT_DECISION_NOT_READY")
    if proof.get("provider_snapshot_complete") is not True:
        gates.append("PROVIDER_SNAPSHOT_INCOMPLETE")
    if proof.get("recipient_name_conflict") is True:
        # HeyReach reported two different names for this correspondent (owner, 2026-09-30).
        gates.append("REPLY_RECIPIENT_NAME_CONFLICT")
    try:
        import reply_execution
        gates.extend(reply_execution.binding_gates(database, config, action, now))
    except Exception as error:
        gates.append("REPLY_SOURCE_BINDING_UNAVAILABLE")
        gates.append("REPLY_SOURCE_BINDING_ERROR:" + type(error).__name__)
    return gates


def _reply_suppression_allowed(reason):
    # "Outreach reserved; never automatically retry" is our own invitation's reservation
    # (owner, 2026-09-30): it stops a second invitation, never an answer to the person who wrote
    # back. It was missing here, so nobody we had invited could be replied to. User exclusions
    # and every other reason still block.
    return reason in {"Previously contacted; no repeat initial outreach",
                      "Handoff historical outreach; never repeat initial contact",
                      "LinkedIn reply stops blind outreach: REPLY_REQUIRES_CONTEXT",
                      "Outreach reserved; never automatically retry"}


def _never_dispatched(database, action_id):
    """True when this action exists, was never claimed, and so has never been sent.

    Same condition outreach_promotion._owned_mutable already uses to rewrite its own unclaimed
    actions (outreach_promotion.py: UPDATE outreach_actions SET payload=...). A claim is the
    record that an attempt was issued, so anything claimed is immutable here and forever.
    """
    row = database.execute("SELECT state,claimed_at FROM outreach_actions WHERE action_id=?", (action_id,)).fetchone()
    # AWAITING_APPROVAL (review mode, approvals.py) is unclaimed too: the owner's Revise and Approve
    # replaces its text with this same compare-and-swap.
    return (row is not None and row[0] in {"PENDING", "BLOCKED", "AWAITING_APPROVAL"} and row[1] is None
            and not database.execute("SELECT 1 FROM outreach_action_claims WHERE action_id=?", (action_id,)).fetchone())


def enqueue_action(database, action, *, replacing=None):
    """Queue exact verified text, not an authorization to execute.

    Required: action_id, profile, recipient, company, purpose, message,
    channel='linkedin', provider='heyreach', verification (content_verified,
    content_sources, identity_verified, identity_source, team_relevance_verified,
    team_source). FIRST_MESSAGE uses a verified acceptance; FOLLOWUP/URGENT
    require ordinal 1/2. PREPARE_CAMPAIGN only creates a draft from a separately
    verified single-contact list and observed sequence. REPLY requires the
    source/task/account/thread fields validated by reply_slot and the parent
    reply_execution.binding_gates contract. No implicit launch. INVITE
    (provider='unipile') carries provider_id and note (= message), slot INVITE.
    """
    if not database.in_transaction:
        initialize_actions(database)
    for field in ("action_id", "profile", "recipient", "company", "message"):
        if not isinstance(action.get(field), str) or not action[field].strip():
            raise ValueError("Missing action " + field)
    purpose = action.get("purpose")
    # INVITE (E3.T3 option A) is the one Unipile purpose: a connection invitation sent through
    # sequence.send_invite after the same claim and gates as any new-contact send.
    # A follow-up to a Unipile-invited person, a reply to a message read from the Unipile inbox, and an
    # initial or first message on Unipile go over the port too (provider 'unipile').
    providers = {"unipile"} if purpose == "INVITE" else {"heyreach", "unipile"} if purpose in {"FOLLOWUP", "URGENT", "REPLY", "FIRST_MESSAGE", "INITIAL"} else {"heyreach"}
    if action.get("channel") != "linkedin" or action.get("provider") not in providers:
        raise ValueError("Only existing LinkedIn/HeyReach integration is authorized")
    if purpose not in {"INITIAL", "FIRST_MESSAGE", "FOLLOWUP", "URGENT", "PREPARE_CAMPAIGN", "REPLY", "INVITE"}:
        raise ValueError("Unsupported outbound purpose")
    if purpose == "INVITE" and (not isinstance(action.get("provider_id"), str) or not action["provider_id"]
                                or action.get("note") != action["message"]):
        raise ValueError("Invitation provider ID and note required")
    if purpose in {"FOLLOWUP", "URGENT"} and (type(action.get("ordinal")) is not int or action["ordinal"] not in (1, 2)):
        raise ValueError("Followup ordinal must be 1 or 2")
    if purpose == "PREPARE_CAMPAIGN" and (type(action.get("list_id")) is not int or action["list_id"] <= 0
            or not isinstance(action.get("campaign_name"), str) or not 1 <= len(action["campaign_name"]) <= 50):
        raise ValueError("Prepared list ID and campaign name required")
    identity = profile_key(action["profile"])
    slot = "INITIAL" if purpose in {"INITIAL", "FIRST_MESSAGE"} else "FOLLOWUP:" + str(action["ordinal"]) if purpose in {"FOLLOWUP", "URGENT"} else "PREPARE_CAMPAIGN"
    if purpose == "INVITE":
        slot = "INVITE"
    if purpose == "REPLY":
        slot = reply_slot(action)
    payload = json.dumps(action, sort_keys=True)
    payload_hash = hashlib.sha256(payload.encode()).hexdigest()
    with engagement.transaction(database):
        if engagement.closure_status(database, identity):
            raise ValueError("OUTREACH_CLOSED")
        import outreach_campaign
        continuation = purpose in {"FIRST_MESSAGE", "FOLLOWUP", "URGENT", "REPLY"}
        reservations = outreach_campaign.reservation_gates(database, identity, action["action_id"], continuation=continuation)
        reservations.extend(_immutable_claim_gates(database, identity, action["action_id"], continuation=continuation,
                                                   reply=purpose == "REPLY"))
        if reservations:
            raise ValueError(";".join(reservations))
        existing = database.execute("SELECT action_id,payload_hash FROM outreach_actions WHERE action_id=? OR (slot=? AND (identity=? OR ?='REPLY'))",
                                    (action["action_id"], slot, identity, purpose)).fetchone()
        if existing:
            if (replacing is not None and existing[0] == action["action_id"]
                    and existing[1] == replacing and existing[1] != payload_hash
                    and _never_dispatched(database, action["action_id"])):
                # A repair turn rewriting the note it was asked to rewrite (owner, 2026-09-23: "i
                # want it in-run"). Deliberately a compare-and-swap, not a general allowance:
                # `replacing` is the exact payload_hash the caller saw rejected, so this cannot be
                # reached by accident and an ordinary enqueue still raises below - which is what
                # test_same_action_id_cannot_reauthorize_changed_message protects. The caller must
                # supply verification rebuilt for the NEW text, as promote() does, so the text
                # that was verified stays the text that sends. Nothing claimed is ever touched.
                database.execute("UPDATE outreach_actions SET payload=?,payload_hash=?,state='PENDING',result=NULL WHERE action_id=?",
                                 (payload, payload_hash, action["action_id"]))
                return {"action_id": action["action_id"], "state": "REPLACED"}
            if existing[0] != action["action_id"] or existing[1] != payload_hash:
                raise ValueError("Action identity or policy slot already reserved for different content")
            return {"action_id": action["action_id"], "state": "ALREADY_QUEUED"}
        database.execute("INSERT INTO outreach_actions VALUES (?,?,?,?,?,'PENDING',NULL,NULL)",
                         (action["action_id"], identity, slot, payload, payload_hash))
    return {"action_id": action["action_id"], "state": "PENDING"}


def _fresh(value, now, maximum_age):
    try:
        return 0 <= (now - engagement.timestamp(value)).total_seconds() <= maximum_age
    except (ValueError, TypeError):
        return False


def action_gates(database, config, action, proof, now):
    """Proof is a normalized fresh inspector result, never an inbox-empty guess.

    REPLY requires an authenticated supported thread route and no reported
    provider restriction. Remaining quota is unknown and provider-enforced;
    these checks do not assert a subscription entitlement or guaranteed send.
    """
    gates = []
    identity = profile_key(action["profile"])
    import inactivity_closure
    gates.extend(inactivity_closure.send_gates(database, config, identity, now))
    identities = engagement.identity_keys(database, identity)
    placeholders = ",".join("?" for unused in identities)
    recipient = name_key(action["recipient"])
    unresolved = unresolved_recipient_name(action["recipient"])
    names = recipient_names(database, identities, extra=(action["recipient"], proof.get("recipient_name")))
    if unresolved and not names:
        gates.append("RECIPIENT_IDENTITY_UNRESOLVED")
    purpose = action["purpose"]
    reply = purpose == "REPLY"
    contextual_followup = purpose == "FOLLOWUP" and ("followup_action_ref" in action
        or config.get("followup_execution", {}).get("enabled") is True)
    continuation = purpose in {"FIRST_MESSAGE", "FOLLOWUP", "URGENT", "REPLY"}
    confirmed_campaign = continuation and _confirmed_campaign(database, identity)
    import outreach_campaign
    gates.extend(outreach_campaign.reservation_gates(database, identity, action["action_id"], continuation=continuation))
    gates.extend(_immutable_claim_gates(database, identity, action["action_id"], continuation=continuation, reply=reply))
    if "agent_decision" in proof and proof["agent_decision"] not in PROCEED_DECISIONS:
        gates.append("AGENT_DECISION_NOT_READY")
    # An incomplete HeyReach snapshot no longer holds an invitation (owner, 2026-09-30: "assuming
    # that the person hasnt been contacted because a snapshot was incomplete is kind of dumb.
    # please fix this. you have unnecessarily blocked [a contact]."). What the snapshot DID show still
    # refuses (a found conversation or reply sets the findings checked below, and the launch
    # refuses any other campaign HeyReach shows for the lead), as do our own records and
    # HeyReach's own duplicate exclusions. Other purposes keep the check.
    if (purpose not in {"INITIAL", "PREPARE_CAMPAIGN", "INVITE"} and "provider_snapshot_complete" in proof
            and proof["provider_snapshot_complete"] is not True):
        gates.append("PROVIDER_SNAPSHOT_INCOMPLETE")
    if owner_seed.no_automatic(identities, names | {recipient}, action.get("campaign_id")):
        gates.append("NO_AUTOMATIC_OUTREACH_CONTACT")
    excluded = owner_seed.excluded_names()
    if recipient in excluded or names & excluded:
        gates.append("EXCLUDED_CONTACT")
    if config.get("outreach_actions_enabled") is not True or config.get("outreach_mode") != "guarded":
        gates.append("ACTION_PATH_NOT_ENABLED")
    if "outreach_allowed_purposes" in config:
        allowed = config["outreach_allowed_purposes"]
        if not isinstance(allowed, list) or any(not isinstance(value, str) for value in allowed):
            gates.append("INVALID_OUTREACH_ALLOWED_PURPOSES")
        elif purpose not in allowed:
            gates.append("OUTREACH_PURPOSE_NOT_ALLOWED")
    elif reply:
        gates.append("OUTREACH_PURPOSE_NOT_ALLOWED")
    unipile = config.get("linkedin_provider") == "unipile"
    providers = {"unipile"} if purpose == "INVITE" and unipile else \
        {"heyreach", "unipile"} if purpose in {"FOLLOWUP", "URGENT", "REPLY", "FIRST_MESSAGE", "INITIAL"} and unipile else {"heyreach"}
    if action.get("provider") not in providers or action.get("channel") != "linkedin":
        gates.append("UNAUTHORIZED_CHANNEL")
    verification = action.get("verification", {})
    # TEAM is not a send condition (owner, 2026-09-23: "we should not block an outreach on the
    # receipients team or Job title. The research subsys has already validated it for us no?").
    # `team_relevance_verified` and `team_source` were removed from both lists; content and
    # identity stay, because those are what the note says and who it goes to.
    for field in (() if reply else ("content_verified", "identity_verified")):
        if verification.get(field) is not True:
            gates.append(field.upper() + "_MISSING")
    for field in ("content_sources", "identity_source"):
        if not verification.get(field):
            gates.append(field.upper() + "_MISSING")
    # No 7-day window for a reply (owner, 2026-09-30: "You can remove the deadline that a reply
    # must be sent within 7 days"); its verified_at is when the decision was made.
    # The owner's approval of this exact text counts as the fresh check (owner, 2026-10-22).
    import approvals
    if not reply and not _fresh(approvals.verified_at(database, action), now, config.get("outreach_content_max_age_seconds", 604800)):
        gates.append("CONTENT_OR_IDENTITY_VERIFICATION_STALE")
    if proof.get("identity") not in identities or proof.get("sender_id") != config.get("sender_id"):
        gates.append("PROVIDER_IDENTITY_OR_SENDER_MISMATCH")
    # Coverage completeness is NOT a send condition (owner, 2026-09-23: "If a model cannot
    # determine that there has been no prior outreach then outreach should not block. We only
    # block on a solid yes. Unknown is not a block."). `direct_messages_complete` and
    # `sent_invitations_complete` say the read finished, not that anyone was contacted - and
    # LinkedIn returns an excerpted Sent-invitations list as a matter of course, which is why
    # every recipient inspected on 2026-09-23 ended UNKNOWN and nothing was ever sent. What the
    # read could not establish is recorded below; a CONFIRMED finding still refuses.
    for field in (("sender_ready",) if reply or contextual_followup else
                  ("sender_ready", "entitlement_verified")):
        if proof.get(field) is not True:
            gates.append(field.upper() + "_MISSING")
    # Deliberately NOT recorded on `action`: this function must stay pure. The action's payload
    # hash is compared for immutability at claim time, so mutating it here invalidates the claim.
    # What the read could not establish already lives on the inspection record in
    # outreach_agent_audit, which is where it is auditable.
    # "Don't know" is no longer evidence to block (owner, 2026-09-23). The test was
    # `is not False`, so an absent provider answer counted as a yes: unknown cooldown meant
    # "in cooldown", unknown pending invitation meant "already invited". HeyReach does not
    # always answer, so that blocked sends on absence of evidence.
    #
    # `opt_out` and `negative_reply` deliberately keep the strict test. Those are the two where
    # being wrong causes the actual harm - messaging someone who asked you to stop - so unknown
    # still blocks there. A KNOWN True on any field blocks everywhere, exactly as before.
    # The `strict` carve-out that kept negative_reply and opt_out blocking on UNKNOWN is GONE
    # (owner, 2026-09-23: "We only block on a solid yes. Unknown is not a block."). That carve-out was
    # self-invented reasoning laid on top of an explicit instruction, and it was still refusing
    # every recipient whose inspection returned null for those fields - which is all of them.
    # A recorded opt-out or negative reply is held in engagement_stops and still refuses below
    # via stop_state/OPTED_OUT; this loop no longer refuses for not knowing.
    for field in (("negative_reply", "opt_out") if reply else ("has_reply", "negative_reply", "opt_out") if contextual_followup else
                  ("pending_invitation", "has_reply", "negative_reply", "opt_out", "cooldown")):
        if proof.get(field) is True:
            gates.append(field.upper() + "_PRESENT")
    if not proof.get("evidence") or not _fresh(proof.get("checked_at"), now, config.get("outreach_history_max_age_seconds", 3600)):
        gates.append("DIRECT_HISTORY_MISSING_OR_STALE")
    stop = engagement.stop_state(database, identity)
    if stop and not (reply and stop == "REPLY_REQUIRES_CONTEXT"):
        gates.append(engagement.stop_label(database, identity))
    if reply:
        gates.extend(_reply_gates(database, config, action, proof, now))
    if contextual_followup:
        import followup_execution
        gates.extend(followup_execution.proof_gates(database, config, action, proof, now))
        if proof.get("agent_decision") not in PROCEED_DECISIONS or proof.get("provider_snapshot_complete") is not True:
            gates.append("FOLLOWUP_FRESH_SEND_JUDGMENT_REQUIRED")
    if database.execute("SELECT 1 FROM outreach_actions WHERE identity IN (" + placeholders + ") AND action_id<>? AND state IN ('CLAIMED','DISPATCHING','REQUEST_STARTED','ACKNOWLEDGED_UNCONFIRMED','UNCERTAIN_NO_RETRY','WRITE_RETRY_WAIT')",
                        (*identities, action["action_id"])).fetchone():
        gates.append("UNRESOLVED_ACTION_NO_RETRY")
    if database.execute("SELECT 1 FROM outreach_actions WHERE identity IN (" + placeholders + ") AND state='CONFIRMED_NOT_SENT'", identities).fetchone():
        gates.append("NOT_SENT_RETRY_UNSUPPORTED")
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    # A new contact's (INITIAL/PREPARE_CAMPAIGN) imported history and history suppressions were
    # checked once, before the draft was created (research_workflow.reserve_contact ->
    # prior_contact_record; owner, 2026-10-01: "We shouldn't be checking at send time. that's too
    # late."), so they are not re-read here. What can change after the draft still refuses: our
    # own claims, actions, attempts and send events, inbound/outbound messages, stops, closure,
    # user exclusions and our own "Outreach reserved" reservation, and what HeyReach shows.
    new_contact = purpose in {"INITIAL", "PREPARE_CAMPAIGN", "INVITE"}
    suppression_lookup = suppression_keys(database, identities, action["recipient"], names=names)
    if "suppressions" in tables:
        for row in database.execute("SELECT reason FROM suppressions WHERE identity IN ("
                                    + ",".join("?" for unused in suppression_lookup) + ")", suppression_lookup):
            if not ((_reply_suppression_allowed(row[0]) if reply else
                    _followup_suppression_allowed(database, identity, row[0]) if purpose == "FOLLOWUP" else
                    row[0] in HISTORY_SUPPRESSION_REASONS if new_contact else
                    row[0] == "Handoff historical outreach; never repeat initial contact")
                    or confirmed_campaign and row[0] == "Outreach reserved; never automatically retry"):
                gates.append("SUPPRESSED_CONTACT")
    if "outreach_attempts" in tables and database.execute("SELECT 1 FROM outreach_attempts WHERE identity IN (" + placeholders
            + ") AND (?=0 OR state<>'CONFIRMED_SENT')", (*identities, int(confirmed_campaign))).fetchone():
        gates.append("LEGACY_ATTEMPT_REQUIRES_RECONCILIATION")
    if "outreach_queue" in tables and database.execute("SELECT 1 FROM outreach_queue WHERE identity IN (" + placeholders
            + ") AND state NOT IN ('CANCELLED','SUPPRESSED') AND (?=0 OR state<>'CONFIRMED_SENT')",
            (*identities, int(confirmed_campaign))).fetchone():
        gates.append("LEGACY_QUEUE_RESERVATION")
    events = engagement.events_for(database, identity)
    if reply and any(event["kind"] == "opt_out" or (event.get("inference") or {}).get("sentiment")
                     in {"negative", "opt_out"} for event in events):
        gates.append("OPTED_OUT")
    if not reply and engagement.unreconciled_outbound(database, events):
        gates.append("OBSERVED_OUTBOUND_REQUIRES_PURPOSE_RECONCILIATION")
    initials = [event for event in events if event["kind"] == "initial_sent"]
    follows = [event for event in events if event["kind"] == "followup_sent"]
    if new_contact:
        # `prior_outreach is not False` refused on null - not knowing - which is every
        # inspection whose Sent-invitations read came back truncated (owner, 2026-09-23: "We only block on a solid yes. Unknown is not a block.").
        # Our own recorded initial send or a confirmed True (what HeyReach showed) refuses. The
        # imported history (`contacted`) and the historical-recipient ledger were checked once,
        # before the draft (owner, 2026-10-01), and are not re-read here.
        if initials or proof.get("prior_outreach") is True:
            gates.append("PREVIOUSLY_CONTACTED_NO_INITIAL")
        # The lifetime COMPANY_CONTACT_LIMIT is deleted (owner, 2026-09-28: "there should be no life
        # time limit"); the only limit is 3 new people per company per research run, applied where
        # research admits them. Prior contact is still refused per person.
    if purpose in {"FIRST_MESSAGE", "FOLLOWUP", "URGENT"}:
        planned = engagement.next_action(database, action["profile"], now, action["recipient"], action.get("campaign_id"))
        if planned.get("state") != "DUE" or planned.get("purpose") != purpose or planned.get("ordinal") != action.get("ordinal"):
            gates.append(planned.get("blocker") or "EXACT_FOLLOWUP_POLICY_NOT_DUE")
        if type(proof.get("followups_sent")) is not int or proof["followups_sent"] != len(follows):
            gates.append("FOLLOWUP_HISTORY_NOT_RECONCILED")
    if purpose == "FIRST_MESSAGE" and proof.get("initial_message_sent") is not False:
        gates.append("INITIAL_ALREADY_SENT_OR_UNKNOWN")
    if purpose in {"FOLLOWUP", "URGENT"} and proof.get("initial_message_sent") is not True:
        gates.append("INITIAL_DELIVERY_NOT_VERIFIED")
    if purpose == "INVITE":
        # An invitation goes to a person, not a thread: the note limit is the only shape check.
        if len(action["message"]) > 300:
            gates.append("INVITATION_NOTE_TOO_LONG")
    elif purpose != "PREPARE_CAMPAIGN":
        # A Unipile follow-up or first message with no chat yet goes out by start_chat: the read that showed no chat
        # is its verified conversation (conversation_id None in both the action and the proof).
        # An INITIAL needs its observed chat, as HeyReach's existing-thread route does.
        start_chat = action.get("provider") == "unipile" and purpose != "INITIAL" and action.get("conversation_id") is None
        if ((not start_chat and not action.get("conversation_id"))
                or proof.get("conversation_id") != action.get("conversation_id")):
            gates.append("VERIFIED_CONVERSATION_MISSING")
        if len(action["message"]) > 8000:
            gates.append("MESSAGE_TOO_LONG")
    else:
        if proof.get("single_contact_list_verified") is not True or proof.get("list_id") != action.get("list_id"):
            gates.append("SINGLE_CONTACT_LIST_UNVERIFIED")
        if not proof.get("sequence_source") or proof.get("sequence") != action.get("sequence"):
            gates.append("OBSERVED_SEQUENCE_MISSING")
        sequence = action.get("sequence", {})
        payload = sequence.get("payload") or {}
        if sequence.get("nodeType") != "CONNECTION_REQUEST" or payload.get("messages") != [action["message"]]:
            gates.append("PREPARATION_SEQUENCE_MISMATCH")
        if len(action["message"]) > 300:
            gates.append("INVITATION_NOTE_TOO_LONG")
        if payload.get("fallbackMessage") not in (None, action["message"]) or payload.get("toBeWithdrawnAfterDays") is not None:
            gates.append("UNEXPECTED_FALLBACK_OR_WITHDRAWAL")
        for branch_name in ("conditionalNode", "unconditionalNode"):
            branch = sequence.get(branch_name) or {}
            if branch.get("nodeType") != "END" or branch.get("conditionalNode") or branch.get("unconditionalNode"):
                gates.append("AUTOMATIC_CAMPAIGN_FOLLOWUP_FORBIDDEN")
            if branch.get("actionDelayUnit") not in {"HOUR", "DAY"} or type(branch.get("actionDelay")) is not int or not 3 <= branch["actionDelay"] <= 100:
                gates.append("INVALID_OBSERVED_SEQUENCE_DELAY")
    return sorted(set(gates))


class HeyReachActionAdapter:
    """Injected transport only; no credentials, discovery calls, or live defaults.

    Request keys match tools metadata discovered on 2026-09-17. Inspect and
    reconcile callbacks must normalize independently read provider/direct-history
    evidence. Provider response schemas were not available in tool metadata;
    consequently no default history, receipt, or inbox parser is implemented.
    """

    def __init__(self, transport=None, inspector=None, reconciler=None, port=None):
        self.transport = transport
        self.inspector = inspector
        self.reconciler = reconciler
        self.port = port  # a linkedin_port.LinkedInPort, used only for INVITE

    def inspect_action(self, action, config):
        if self.inspector is None or self.transport is None:
            raise NotImplementedError("UNSUPPORTED_PROVIDER_HISTORY_ADAPTER")
        return self.inspector(action, config)

    def reconcile_action(self, action, config):
        if self.reconciler is None:
            raise NotImplementedError("UNSUPPORTED_PROVIDER_RECEIPT_ADAPTER")
        return self.reconciler(action, config)

    def execute_claimed(self, database, action_id, config, now=None):
        database.execute("BEGIN IMMEDIATE")
        try:
            row = database.execute("SELECT payload,result,payload_hash FROM outreach_actions WHERE action_id=? AND state='DISPATCHING'", (action_id,)).fetchone()
            if not row:
                raise ValueError("A fresh durable dispatch claim is required")
            claim = database.execute("SELECT * FROM outreach_action_claims WHERE action_id=?", (action_id,)).fetchone()
            if not claim or row[0] != claim["payload"] or row[2] != claim["payload_hash"] or config.get("sender_id") != claim["sender_id"]:
                raise ValueError("Action no longer matches its immutable claim")
            action = json.loads(row[0])
            if hashlib.sha256(row[0].encode()).hexdigest() != row[2]:
                raise ValueError("Claimed action payload was changed")
            proof = json.loads(claim["preflight"])
            if action.get("purpose") == "REPLY":
                # Right before a reply is sent only the stops the owner kept apply (2026-09-30);
                # the full guard ran at inspection moments ago. See reply_execution.stop_gates.
                import reply_execution
                gates = reply_execution.stop_gates(database, config, action, proof)
            else:
                gates = action_gates(database, config, action, proof, engagement.timestamp(now or dt.datetime.now(dt.timezone.utc)))
            if gates:
                database.execute("UPDATE outreach_actions SET state='CANCELLED_BEFORE_SEND',result=? WHERE action_id=?", (json.dumps(gates), action_id))
                database.commit()
                raise ValueError("Outbound conditions changed before provider request")
            database.execute("UPDATE outreach_actions SET state='REQUEST_STARTED' WHERE action_id=?", (action_id,))
            if action["purpose"] != "PREPARE_CAMPAIGN" and database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='suppressions'").fetchone():
                for suppressed in (profile_key(action["profile"]), "name:" + name_key(action["recipient"])):
                    database.execute("INSERT OR IGNORE INTO suppressions VALUES (?,'Handoff historical outreach; never repeat initial contact')", (suppressed,))
            database.commit()
        except BaseException:
            database.rollback()
            raise
        if action["purpose"] == "INVITE":
            # Only the transport differs for INVITE: the committed REQUEST_STARTED row goes to the port.
            if self.port is None:
                raise NotImplementedError("UNSUPPORTED_LINKEDIN_PORT")
            import sequence
            return sequence.send_invite(database, self.port, action_id)
        if self.transport is None:
            raise NotImplementedError("UNSUPPORTED_PROVIDER_WRITE_ADAPTER")
        if action["purpose"] == "PREPARE_CAMPAIGN":
            return self.transport("create_campaign", {
                "linkedInAccountIds": [claim["sender_id"]], "linkedInUserListId": action["list_id"],
                "name": action["campaign_name"], "sequenceJson": json.dumps(action["sequence"]),
                "excludeContactedFromOtherCampaigns": True, "excludeContactedFromSenderInOtherCampaign": True,
                "excludeFromCompanyBlacklist": True, "excludeFromLeadBlacklist": True,
                "excludeHasOtherAccConversations": True})
        return self.transport("send_message", {"conversationId": action["conversation_id"],
            "linkedInAccountId": claim["sender_id"], "message": action["message"], "subject": action.get("subject", "")})


def _reconcile_action(database, row, action, config, client, now, read_token=None, *, refresh_now=False):
    """Return the resolved state, or False while independent proof is missing.

    NOT_SENT additionally binds claimed_at and requires terminal_rejection and
    cannot_execute to be true. rejection_evidence is an adapter-authenticated
    HeyReach observation with kind='explicit_terminal_rejection', supported=True,
    authenticated=True, source_id, raw, raw_sha256, rejection_text, observed_at,
    and binding matching every expected receipt field. The adapter must obtain
    it through a supported authenticated reader, never infer it from absence.
    """
    claim = database.execute("SELECT * FROM outreach_action_claims WHERE action_id=?", (row["action_id"],)).fetchone()
    if (not claim or row["payload"] != claim["payload"] or row["payload_hash"] != claim["payload_hash"]
            or row["identity"] != claim["identity"]
            or hashlib.sha256(claim["payload"].encode()).hexdigest() != claim["payload_hash"]):
        raise ValueError("Immutable claim missing or changed")
    receipt = client.reconcile_action(action, dict(config, sender_id=claim["sender_id"], outreach_claim=dict(claim)))
    if refresh_now:
        now = dt.datetime.now(dt.timezone.utc)
    operation = "create_campaign" if action["purpose"] == "PREPARE_CAMPAIGN" else "send_message"
    if receipt is None or isinstance(receipt, dict) and receipt.get("state") in {"PENDING", "UNKNOWN"}:
        return False
    if not isinstance(receipt, dict):
        raise ValueError("Malformed reconciliation response")
    if receipt.get("independent") is not True:
        if receipt.get("acknowledged") is True or receipt.get("ack") is True:
            return False
        raise ValueError("Independent receipt proof missing")
    not_sent = receipt.get("outcome") == "NOT_SENT"
    expected = {"action_id": row["action_id"], "identity": row["identity"], "payload_hash": row["payload_hash"],
                "operation": operation, "sender_id": claim["sender_id"],
                "outcome": "NOT_SENT" if not_sent else "DRAFT_CREATED" if operation == "create_campaign" else "SENT"}
    if operation == "send_message":
        expected["conversation_id"] = action["conversation_id"]
    if not_sent:
        expected["claimed_at"] = claim["claimed_at"]
    if any(receipt.get(field) != value for field, value in expected.items()) or not receipt.get("receipt_id") or not receipt.get("evidence"):
        raise ValueError("Receipt identity, sender, thread or payload mismatch")
    engagement.timestamp(receipt["observed_at"])
    if not _fresh(receipt.get("observed_at"), now, config.get("outreach_history_max_age_seconds", 3600)):
        return False
    if not_sent:
        evidence = receipt.get("rejection_evidence")
        if (receipt.get("terminal_rejection") is not True or receipt.get("cannot_execute") is not True
                or not isinstance(evidence, dict) or evidence.get("source") != "heyreach"
                or evidence.get("kind") != "explicit_terminal_rejection"
                or evidence.get("authenticated") is not True or evidence.get("supported") is not True
                or not isinstance(evidence.get("source_id"), str) or not evidence["source_id"].strip()
                or not isinstance(evidence.get("raw"), str) or not evidence["raw"].strip()
                or not isinstance(evidence.get("rejection_text"), str) or not evidence["rejection_text"].strip()
                or evidence["rejection_text"] not in evidence["raw"]
                or evidence.get("raw_sha256") != hashlib.sha256(evidence["raw"].encode()).hexdigest()
                or not isinstance(evidence.get("binding"), dict)
                or any(evidence["binding"].get(field) != value for field, value in expected.items())
                or evidence.get("observed_at") != receipt["observed_at"]):
            raise ValueError("Authenticated claim-bound terminal rejection evidence required")
        if engagement.timestamp(receipt["observed_at"]) < engagement.timestamp(claim["claimed_at"]):
            return False
    else:
        occurred = engagement.timestamp(receipt["occurred_at"])
        if not engagement.timestamp(claim["claimed_at"]) <= occurred <= engagement.timestamp(receipt["observed_at"]):
            return False
    if not not_sent and operation == "create_campaign" and (type(receipt.get("campaign_id")) is not int or receipt["campaign_id"] <= 0 or receipt["campaign_id"] in owner_seed.no_automatic_campaigns()):
        raise ValueError("Invalid prepared campaign identity")
    resolved_state = "CONFIRMED_NOT_SENT" if not_sent else "PREPARED" if operation == "create_campaign" else "CONFIRMED_SENT"
    database.execute("BEGIN IMMEDIATE")
    try:
        if refresh_now:
            now = dt.datetime.now(dt.timezone.utc)
        if read_token is not None:
            reading = database.execute("SELECT state,token,lease_until FROM outreach_read_retries WHERE action_id=? AND phase='RECONCILIATION'", (row["action_id"],)).fetchone()
            if (not reading or reading["state"] != "READING" or reading["token"] != read_token
                    or not reading["lease_until"] or engagement.timestamp(reading["lease_until"]) <= now):
                database.rollback()
                return False
        current = database.execute("SELECT state,payload,payload_hash,identity FROM outreach_actions WHERE action_id=?", (row["action_id"],)).fetchone()
        if current[1] != claim["payload"] or current[2] != claim["payload_hash"] or current[3] != claim["identity"]:
            database.rollback()
            return False
        if current[0] in {"CONFIRMED_SENT", "CONFIRMED_NOT_SENT", "PREPARED"}:
            database.rollback()
            return current[0]
        if not not_sent and operation == "create_campaign":
            tables = {entry[0] for entry in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ("outreach_attempts", "outreach_queue", "outreach_prepared_campaigns"):
                if table in tables and database.execute("SELECT 1 FROM " + table + " WHERE campaign_id=?", (receipt["campaign_id"],)).fetchone():
                    database.rollback()
                    return False
            database.execute("INSERT INTO outreach_prepared_campaigns VALUES (?,?,?)", (receipt["campaign_id"], action["action_id"], row["identity"]))
        if not not_sent and operation == "send_message":
            engagement._record(database, {"source": "unipile" if action.get("provider") == "unipile" else "heyreach",
                "source_id": str(receipt["receipt_id"]),
                "profile": action["profile"], "kind": "outbound_observed" if action["purpose"] == "REPLY" else "followup_sent" if action["purpose"] in {"FOLLOWUP", "URGENT"} else "initial_sent",
                "occurred_at": occurred.isoformat(), "observed_at": receipt["observed_at"], "verified": True,
                "evidence": receipt["evidence"], "raw": receipt, "purpose": action["purpose"]})
        database.execute("UPDATE outreach_actions SET state=?,result=? WHERE action_id=?",
                         (resolved_state, json.dumps(receipt), row["action_id"]))
        database.commit()
    except BaseException:
        database.rollback()
        raise
    return resolved_state


def _repair_in_run(database, row, action, proof, now, repair, repaired):
    """Let the producer rewrite a judge-rejected FOLLOWUP or REPLY inside this same run.

    Owner, 2026-09-23: "i want it in-run". The outreach composer already repairs in its own loop
    (outreach_agent._draft_repair_turn); a follow-up or reply is written by a different worker, so
    the only way to repair it without waiting for the next cycle is to call that worker back from
    here. `repair(action, proof, now)` returns the action_id it rewrote, and the row is re-judged
    in this run only if its payload actually changed - a producer that changed nothing ends the
    round, exactly as a no-progress turn ends answering_agent.repair_round.

    Nothing that was ever claimed is touched. A claim is the record that an attempt was issued,
    so replacing the text behind one could send a message the judge never saw. One repair per
    recipient per cycle.
    """
    if repair is None or action.get("purpose") not in {"FOLLOWUP", "REPLY"}:
        return None
    try:
        identity = profile_key(action["profile"])
    except (ValueError, TypeError, KeyError):
        return None
    if identity in repaired:
        return None
    # A reply is checked against its OWN claim (owner, 2026-09-30): everyone we reply to after
    # inviting them has the invitation's claim on file, so the identity-wide test made a
    # judge-rejected reply unrepairable for exactly the people the harness manages.
    claimed = (database.execute("SELECT 1 FROM outreach_action_claims WHERE action_id=?", (row["action_id"],)).fetchone()
               if action.get("purpose") == "REPLY" else
               database.execute("SELECT 1 FROM outreach_action_claims WHERE identity=?", (identity,)).fetchone())
    if row["claimed_at"] is not None or claimed:
        return None
    repaired.add(identity)
    try:
        action_id = repair(action, proof, now)
    except Exception:
        return None          # a failed repair turn is not a failed cycle
    if not action_id:
        return None
    replacement = database.execute("SELECT * FROM outreach_actions WHERE action_id=?", (action_id,)).fetchone()
    if replacement is None or replacement["payload_hash"] == row["payload_hash"]:
        return None
    return replacement


def cycle(database, config, client, now=None, *, purpose=None, repair=None):
    """Guarded opt-in executor; all uncertain/claimed actions reconcile, never retry.

    Main must integrate only after review, with outreach_actions_enabled=True
    and outreach_mode='guarded'. inspect_action returns the normalized fields
    checked by action_gates. execute_claimed consumes a durable claim before
    transport. reconcile_action requires independent evidence bound to the
    action ID, identity, payload hash, operation and receipt ID. No ack is sent.

    Each cycle handles at most 60 actions. outreach_read_retries stores separate
    INSPECTION/RECONCILIATION attempts, next_at, last_error and exclusive read
    leases. Transient reads retry at 15 and 60 minutes, never a fourth attempt.
    Successful gated inspections end that transient series. Unknown receipts
    share the reconciliation budget; exhausted reads never release send claims.
    Unsupported adapters hold without spending budget until resume_read after
    repair. Malformed proofs hold INVALID_PROOF without automatic retry.
    An INVITE action is inspected and claimed here like any new contact; once claimed it is
    settled only by sequence.reconcile_invite (orchestrator.new_contact_execution_cycle).
    """
    initialize_actions(database)
    if purpose not in {None, "INITIAL", "FIRST_MESSAGE", "FOLLOWUP", "URGENT", "PREPARE_CAMPAIGN", "REPLY", "INVITE"}:
        raise ValueError("Unsupported action purpose filter")
    if config.get("outreach_actions_enabled") is not True:
        return {"state": "DISABLED", "items": [], "attempts": 0}
    refresh_now = now is None
    now = engagement.timestamp(now or dt.datetime.now(dt.timezone.utc))
    result = {"state": "PROCESSED", "items": [], "attempts": 0}
    # Review mode housekeeping (approvals.py): a waiting message to a closed or opted-out person is
    # closed (P4), and a row an earlier version held only because the mode could not be read
    # (UNREADABLE_MODE) is released, whatever the mode (G4: an unreadable mode no longer holds; the
    # hold below holds it again only for a readable Review). Neither writes anything when nothing is waiting.
    import approvals
    closed = approvals.close_stopped(database, now)
    released = approvals.release_unreadable_holds(database)
    if closed or released:
        result["approvals"] = {"closed": closed, "released": released}
    limit = config.get("outreach_max_items_per_cycle", 60)
    if type(limit) is not int or not 1 <= limit <= 60:
        limit = 60
    # Only the INVITE pass of a cycle that holds a port may inspect or claim an INVITE; any other
    # cycle (no port, another purpose) leaves it alone (review 2A round 1).
    invites = int(purpose == "INVITE" and getattr(client, "port", None) is not None)
    rows = database.execute("""SELECT actions.* FROM outreach_actions AS actions
        LEFT JOIN outreach_read_retries AS reads ON reads.action_id=actions.action_id
        AND reads.phase=CASE WHEN actions.state IN ('PENDING','BLOCKED') THEN 'INSPECTION' ELSE 'RECONCILIATION' END
        WHERE actions.state NOT IN ('CONFIRMED_SENT','CONFIRMED_NOT_SENT','PREPARED','CANCELLED_BEFORE_SEND','CANCELLED','CLOSED','WRITE_FAILED','AWAITING_APPROVAL')
        AND (? IS NULL OR json_extract(actions.payload,'$.purpose')=?)
        AND (COALESCE(json_extract(actions.payload,'$.purpose'),'')<>'INVITE' OR (? AND actions.state IN ('PENDING','BLOCKED')))
        ORDER BY CASE WHEN reads.state IN ('ADAPTER_UNAVAILABLE','INVALID_PROOF','READ_RETRY_EXHAUSTED','COMPLETE')
            OR reads.next_at>? THEN 1 ELSE 0 END, COALESCE(reads.updated_at,''), actions.action_id LIMIT ?""",
        (purpose, purpose, invites, now.isoformat(), limit)).fetchall()
    # One in-run repair per recipient; see _repair_in_run.
    repaired = set()
    for row in rows:
        # Contain EVERY per-action failure (owner, 2026-09-23: "Fix 7. must never happen").
        # Fixing only the claim transaction was not enough: sixteen further calls in this body -
        # several of them database writes - could still raise straight out of the loop, which
        # abandons the remaining actions AND, because the orchestrator drives the purposes in one
        # dict comprehension, skips FIRST_MESSAGE, PREPARE_CAMPAIGN and the whole campaign cycle.
        # One action's fault now costs that action only. Deliberate control flow (continue) and
        # real interruption still pass through untouched.
        item = {"action_id": row["action_id"], "state": row["state"]}
        try:
            if refresh_now:
                now = dt.datetime.now(dt.timezone.utc)
            action = json.loads(row["payload"])
            result["items"].append(item)
            if row["state"] in {"PENDING", "BLOCKED"} and engagement.closure_status(database, action["profile"]):
                with database:
                    database.execute("UPDATE outreach_actions SET state='CLOSED' WHERE action_id=? AND state IN ('PENDING','BLOCKED')", (row["action_id"],))
                item.update(state="CLOSED", blocker="OUTREACH_CLOSED")
                continue
            if (action.get("purpose") == "REPLY" and row["state"] == "UNCERTAIN_NO_RETRY"
                    and hasattr(client, "retry_reply")):
                # A reply send that hit an error is retried, at most 3 attempts in all, each after a
                # HeyReach read proving the earlier one did not go through (owner, 2026-09-30).
                item.update(client.retry_reply(row, action, config, now))
                continue
            if action.get("purpose") == "INVITE" and row["state"] in {"PENDING", "BLOCKED"}:
                # E3.T4 option A (owner, 2026-10-17): pacing decides only when, checked before the
                # inspection (review 2A round 1): a waiting invite is skipped untouched, with no read,
                # no judge call, no claim and no gate.
                import pacing
                pace = pacing.check(database, config, now)
                if not pace["go"]:
                    item.update(state="PACING_WAIT", reason=pace["reason"], wait_until=pace["wait_until"])
                    continue
            phase = "INSPECTION" if row["state"] in {"PENDING", "BLOCKED"} else "RECONCILIATION"
            read_token = _claim_read(database, row["action_id"], phase, now)
            if read_token is None:
                item["read_retry"] = _read_status(database, row["action_id"], phase)
                item["blocker"] = item["read_retry"]["state"]
                continue
            if row["state"] not in {"PENDING", "BLOCKED"}:
                try:
                    resolved_state = _reconcile_action(database, row, action, config, client, now, read_token, refresh_now=refresh_now)
                    if refresh_now:
                        now = dt.datetime.now(dt.timezone.utc)
                    if resolved_state:
                        item["state"] = resolved_state
                        if resolved_state == "CONFIRMED_NOT_SENT":
                            item["blocker"] = "NOT_SENT_RETRY_UNSUPPORTED"
                        _finish_read(database, row["action_id"], phase, read_token, now, "COMPLETE")
                    else:
                        item["blocker"] = "INDEPENDENT_RECEIPT_REQUIRED_NO_RETRY"
                        _finish_read(database, row["action_id"], phase, read_token, now, "RETRY_WAIT", "INDEPENDENT_RECEIPT_PENDING")
                except Exception as error:
                    if refresh_now:
                        now = dt.datetime.now(dt.timezone.utc)
                    outcome, safe_error = _read_error(error)
                    _finish_read(database, row["action_id"], phase, read_token, now, outcome, safe_error,
                                 retry_at=getattr(error, "reset_at", None) if safe_error == "OUTREACH_AI_SHARED_BUDGET_EXHAUSTED" else None)
                    item["blocker"] = "RECONCILIATION_UNAVAILABLE_NO_RETRY" if outcome == "ADAPTER_UNAVAILABLE" else safe_error
                item["read_retry"] = _read_status(database, row["action_id"], phase)
                continue
            try:
                proof = client.inspect_action(action, config)
                if refresh_now:
                    now = dt.datetime.now(dt.timezone.utc)
                if not isinstance(proof, dict):
                    raise ValueError("Malformed inspection proof")
                gates = action_gates(database, config, action, proof, now)
                count = proof.get("company_contact_count")
                item["contact_usage"] = contact_usage(database, action["company"], action, config.get("pipeline"), count if type(count) is int and count >= 0 else 0)
                required_booleans = ("sender_ready", "entitlement_verified", "direct_messages_complete", "sent_invitations_complete",
                                     "pending_invitation", "has_reply", "negative_reply", "opt_out", "cooldown")
                if action["purpose"] == "REPLY":
                    required_booleans = ("sender_ready", "reply_transport_supported", "provider_restriction_observed", "negative_reply", "opt_out",
                                         "reply_latest_inbound", "reply_already_answered", "reply_context_verified")
                elif action["purpose"] == "FOLLOWUP" and ("followup_action_ref" in action
                        or config.get("followup_execution", {}).get("enabled") is True):
                    required_booleans = ("sender_ready", "followup_transport_supported", "followup_context_verified",
                                         "followup_thread_unchanged", "provider_restriction_observed", "has_reply", "negative_reply", "opt_out")
                agent_proof = "agent_decision" in proof
                invalid_proof = any(type(proof.get(field)) is not bool
                                    and not (agent_proof and proof.get(field) is None)
                                    for field in required_booleans)
                unresolved_proof = agent_proof and any(proof.get(field) is None for field in required_booleans)
                if "provider_snapshot_complete" in proof and proof["provider_snapshot_complete"] is not True:
                    unresolved_proof = True
                if invalid_proof:
                    gates.append("INVALID_INSPECTION_PROOF")
            except Exception as error:
                if refresh_now:
                    now = dt.datetime.now(dt.timezone.utc)
                outcome, safe_error = _read_error(error)
                _finish_read(database, row["action_id"], phase, read_token, now, outcome, safe_error,
                             retry_at=getattr(error, "reset_at", None) if safe_error == "OUTREACH_AI_SHARED_BUDGET_EXHAUSTED" else None)
                gates = ["PROVIDER_HISTORY_ADAPTER_UNAVAILABLE" if outcome == "ADAPTER_UNAVAILABLE" else "INSPECTION_" + outcome]
                with database:
                    database.execute("UPDATE outreach_actions SET state='BLOCKED',result=? WHERE action_id=? AND state IN ('PENDING','BLOCKED')", (json.dumps(gates), row["action_id"]))
                item.update(state="BLOCKED", gates=gates, read_retry=_read_status(database, row["action_id"], phase))
                continue
            if gates:
                outcome = "INVALID_PROOF" if invalid_proof else "RETRY_WAIT" if unresolved_proof else "READY"
                error = "MALFORMED_INSPECTION_PROOF" if invalid_proof else "SOURCE_EVIDENCE_UNRESOLVED" if unresolved_proof else None
                if not _finish_read(database, row["action_id"], phase, read_token, now, outcome, error):
                    item.update(blocker="STALE_INSPECTION_READ", read_retry=_read_status(database, row["action_id"], phase))
                    continue
                if "AGENT_DECISION_NOT_READY" in gates:
                    # Record the objection FIRST: the producer reads it back through
                    # outreach_agent.pending_objection when it rewrites, so the order matters.
                    import outreach_agent
                    outreach_agent.record_judge_objection(database, action, proof, now)
                    # Then ask that producer to rewrite it now, in this same run (owner,
                    # 2026-09-23: "i want it in-run"). The record is what carries the reason; the
                    # callback is what makes the rewrite happen without waiting a cycle.
                    replacement = _repair_in_run(database, row, action, proof, now, repair, repaired)
                    if replacement is not None:
                        rows.append(replacement)
                        item["repair"] = {"state": "RECOMPOSED", "action_id": replacement["action_id"]}
                with database:
                    database.execute("UPDATE outreach_actions SET state='BLOCKED',result=? WHERE action_id=? AND state IN ('PENDING','BLOCKED')", (json.dumps(gates), row["action_id"]))
                item.update(state="BLOCKED", gates=gates, read_retry=_read_status(database, row["action_id"], phase))
                continue
            try:
                database.execute("BEGIN IMMEDIATE")
                if refresh_now:
                    now = dt.datetime.now(dt.timezone.utc)
                current = database.execute("SELECT state,payload,payload_hash FROM outreach_actions WHERE action_id=?", (row["action_id"],)).fetchone()
                gates = action_gates(database, config, action, proof, now)
                if current[1] != row["payload"] or current[2] != row["payload_hash"] or hashlib.sha256(current[1].encode()).hexdigest() != current[2]:
                    gates.append("ACTION_PAYLOAD_CHANGED_AFTER_INSPECTION")
                if current[0] not in {"PENDING", "BLOCKED"} or gates:
                    _finish_read(database, row["action_id"], phase, read_token, now, "READY")
                    database.commit()
                    item.update(state="BLOCKED" if gates else current[0], gates=gates)
                    continue
                # Review mode (owner, 2026-10-22; approvals.py): point H. The mode is read here, in the
                # transaction that would write the claim; a held row gets no claim, so nothing after
                # this point can send it, and the cycle never selects AWAITING_APPROVAL again.
                import approvals
                held_by = approvals.hold_reason(database, row)
                if held_by:
                    if not _finish_read(database, row["action_id"], phase, read_token, now, "READY"):
                        database.rollback()
                        item.update(blocker="STALE_INSPECTION_READ", read_retry=_read_status(database, row["action_id"], phase))
                        continue
                    if not approvals.mark_held(database, row, proof, now, held_by):
                        # Nothing changed (review fix P9): report what is actually on record, unclaimed.
                        database.rollback()
                        state = database.execute("SELECT state FROM outreach_actions WHERE action_id=?", (row["action_id"],)).fetchone()
                        item.update(state=state[0] if state else None, blocker="APPROVAL_HOLD_NOT_RECORDED")
                        continue
                    database.commit()
                    item.update(state=approvals.AWAITING, held_by=held_by)
                    continue
                if not _finish_read(database, row["action_id"], phase, read_token, now, "COMPLETE"):
                    database.rollback()
                    item.update(blocker="STALE_INSPECTION_READ", read_retry=_read_status(database, row["action_id"], phase))
                    continue
                database.execute("UPDATE outreach_actions SET state='DISPATCHING',claimed_at=?,result=? WHERE action_id=?", (now.isoformat(), json.dumps({"preflight": proof}), row["action_id"]))
                database.execute("INSERT INTO outreach_action_claims VALUES (?,?,?,?,?,?,?)",
                    (row["action_id"], row["payload"], row["payload_hash"], row["identity"], config["sender_id"], json.dumps(proof), now.isoformat()))
                database.commit()
            except (KeyboardInterrupt, SystemExit):
                database.rollback()
                raise
            except Exception as error:
                # Contain the failure to THIS action (owner, 2026-09-23: "Fix 7. must never
                # happen."). This re-raised, so one action's database error aborted the rest of the
                # loop - up to 60 actions - and, because the orchestrator drives the purposes in a
                # single dict comprehension, also skipped FIRST_MESSAGE, PREPARE_CAMPAIGN and the
                # whole campaign cycle. Every other failure in this loop is already contained
                # per-action; this path was the exception. BEGIN IMMEDIATE moved inside the try as
                # well, so an SQLITE_BUSY there is rolled back rather than escaping unrolled.
                try:
                    database.rollback()
                except Exception:
                    pass
                item.update(state="BLOCKED", blocker="OUTREACH_CLAIM_FAILED",
                            error_class=type(error).__name__, diagnostic=str(error)[:300],
                            next_action="Reconcile this action's claim record; other actions were unaffected")
                result.setdefault("claim_failures", []).append(
                    {"action_id": row["action_id"], "error_class": type(error).__name__, "diagnostic": str(error)[:300]})
                continue
            result["attempts"] += 1
            item["read_retry"] = _read_status(database, row["action_id"], phase)
            try:
                client.execute_claimed(database, row["action_id"], config, now=None if refresh_now else now)
                state = "ACKNOWLEDGED_UNCONFIRMED"
            except Exception as error:
                # The action-path twin of the dispatch swallow (owner, 2026-09-23). An
                # execute that actually reached the provider and one that never left this
                # process both landed on UNCERTAIN_NO_RETRY with nothing recorded, so the
                # only way to tell them apart was to go and read the provider by hand.
                state = "UNCERTAIN_NO_RETRY"
                if action.get("purpose") == "PREPARE_CAMPAIGN":
                    # A failed draft-campaign write waits for a bounded retry (owner, 2026-09-30:
                    # "for campaign: a failed write can be retried"); the reconciliation read
                    # looks the draft up by name first and retries only when it is absent.
                    import outreach_campaign
                    state = outreach_campaign.failed_create_state(database, row["action_id"]) or state
                item.update(error_class=type(error).__name__, diagnostic=str(error)[:300],
                            error_at=dt.datetime.now(dt.timezone.utc).isoformat())
            with database:
                database.execute("UPDATE outreach_actions SET state=? WHERE action_id=? AND state IN ('DISPATCHING','REQUEST_STARTED')", (state, row["action_id"]))
                if state == "UNCERTAIN_NO_RETRY" and action.get("purpose") == "REPLY":
                    # Keep the reply's send error with the action; the retry reads it back.
                    saved = database.execute("SELECT result FROM outreach_actions WHERE action_id=? AND state='UNCERTAIN_NO_RETRY'",
                                             (row["action_id"],)).fetchone()
                    if saved is not None:
                        recorded = json.loads(saved[0]) if saved[0] else {}
                        if isinstance(recorded, dict):
                            recorded.setdefault("reply_send_errors", []).append(
                                {key: item[key] for key in ("error_class", "diagnostic", "error_at") if key in item})
                            database.execute("UPDATE outreach_actions SET result=? WHERE action_id=?",
                                             (json.dumps(recorded, sort_keys=True), row["action_id"]))
            item["state"] = database.execute("SELECT state FROM outreach_actions WHERE action_id=?", (row["action_id"],)).fetchone()[0]
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as error:
            try:
                database.rollback()
            except Exception:
                pass
            if item not in result["items"]:
                result["items"].append(item)
            item.update(blocker="OUTREACH_ACTION_FAILED", error_class=type(error).__name__,
                        diagnostic=str(error)[:300],
                        next_action="Reconcile this action; every other action in the cycle was unaffected")
            result.setdefault("action_failures", []).append(
                {"action_id": row["action_id"], "error_class": type(error).__name__,
                 "diagnostic": str(error)[:300]})
            continue
    return result
