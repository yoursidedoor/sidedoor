"""Single-recipient list preparation and handoff to the existing dispatcher.

Parent/agent API: prepare_list(database, config, client, action, proof, now=None)
accepts an AI-approved PREPARE_CAMPAIGN action without list_id. Supply sequence
and sequence_source from invitation_sequence plus a supported observed example.
It returns a promotion context containing list_id/campaign_name/sequence/proof.
Use client.call for reads and client._request only for the two claimed list writes.
Campaign creation remains outreach's PREPARE_CAMPAIGN action. After independently
reconciling it to PREPARED, call handoff with fresh inspector proof. Only the
existing legacy dispatcher launches it.

Shared guard integration: reservation_gates(database, identity, action_id=None,
campaign_id=None) blocks every other route. Pass action_id for the exact owning
preparation and both action_id/campaign_id for its verified legacy handoff.
The parent must invoke this under its existing action/dispatch transactions.
Claims are never released. A write that FAILED (an error, not an acknowledgment that is not yet
visible) is retried up to three attempts in all, after HeyReach is read and shows no result
(owner, 2026-09-30); the third failure is terminal (WRITE_FAILED).
"""

import datetime as dt
import hashlib
import json
import secrets

import engagement
import outreach
import outreach_history
import outreach_promotion
import research_workflow as research


def _encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def initialize(database):
    _require(not database.in_transaction, "CAMPAIGN_INITIALIZE_OUTSIDE_TRANSACTION")
    database.executescript("""
        CREATE TABLE IF NOT EXISTS outreach_campaign_preparations (
            action_id TEXT PRIMARY KEY, identity TEXT UNIQUE NOT NULL,
            sender_id INTEGER NOT NULL, payload TEXT NOT NULL,
            list_name TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS outreach_campaign_intents (
            action_id TEXT NOT NULL, operation TEXT NOT NULL,
            arguments TEXT NOT NULL, started_at TEXT NOT NULL,
            state TEXT NOT NULL, result TEXT, preflight TEXT NOT NULL,
            PRIMARY KEY(action_id,operation));
        CREATE TRIGGER IF NOT EXISTS campaign_preparation_no_update
            BEFORE UPDATE ON outreach_campaign_preparations BEGIN
            SELECT RAISE(ABORT,'Immutable campaign preparation'); END;
        CREATE TRIGGER IF NOT EXISTS campaign_preparation_no_delete
            BEFORE DELETE ON outreach_campaign_preparations BEGIN
            SELECT RAISE(ABORT,'Immutable campaign preparation'); END;
        CREATE TRIGGER IF NOT EXISTS campaign_preparation_no_replace
            BEFORE INSERT ON outreach_campaign_preparations
            WHEN EXISTS(SELECT 1 FROM outreach_campaign_preparations
                WHERE action_id=NEW.action_id OR identity=NEW.identity) BEGIN
            SELECT RAISE(ABORT,'Immutable campaign preparation'); END;
        CREATE TRIGGER IF NOT EXISTS campaign_intent_no_rewrite
            BEFORE UPDATE OF action_id,operation,arguments,started_at,preflight
            ON outreach_campaign_intents BEGIN
            SELECT RAISE(ABORT,'Immutable campaign write intent'); END;
        CREATE TRIGGER IF NOT EXISTS campaign_intent_no_delete
            BEFORE DELETE ON outreach_campaign_intents BEGIN
            SELECT RAISE(ABORT,'Immutable campaign write intent'); END;
        CREATE TRIGGER IF NOT EXISTS campaign_intent_no_replace
            BEFORE INSERT ON outreach_campaign_intents
            WHEN EXISTS(SELECT 1 FROM outreach_campaign_intents
                WHERE action_id=NEW.action_id AND operation=NEW.operation) BEGIN
            SELECT RAISE(ABORT,'Immutable campaign write intent'); END;
    """)


def invitation_sequence(observed, message):
    _require(isinstance(observed, dict) and observed.get("nodeType") == "CONNECTION_REQUEST",
             "OBSERVED_CONNECTION_SEQUENCE_REQUIRED")
    _require(isinstance(message, str) and 0 < len(message.strip()) <= 300
             and "{" not in message and "}" not in message, "EXACT_INVITATION_REQUIRED")
    terminal = {"nodeType": "END", "actionDelay": 3, "actionDelayUnit": "HOUR"}
    return {"nodeType": "CONNECTION_REQUEST", "actionDelay": 0, "actionDelayUnit": "HOUR",
            "payload": {"messages": [message]}, "conditionalNode": dict(terminal),
            "unconditionalNode": dict(terminal)}


def _same_sequence(observed, expected):
    def normalized(sequence):
        sequence = json.loads(json.dumps(sequence))
        payload = sequence.get("payload")
        if isinstance(payload, dict):
            message = (payload.get("messages") or [None])[0]
            if payload.get("fallbackMessage") in (None, message):
                payload.pop("fallbackMessage", None)
            if payload.get("toBeWithdrawnAfterDays") is None:
                payload.pop("toBeWithdrawnAfterDays", None)
        for field in ("conditionalNode", "unconditionalNode"):
            node = sequence.get(field)
            if isinstance(node, dict):
                for optional in ("payload", "conditionalNode", "unconditionalNode"):
                    if node.get(optional) is None:
                        node.pop(optional, None)
        return sequence

    return isinstance(observed, dict) and normalized(observed) == normalized(expected)


def reservation_gates(database, identity, action_id=None, campaign_id=None, *, continuation=False):
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "outreach_campaign_preparations" not in tables:
        return []
    identities = engagement.identity_keys(database, identity)
    placeholders = ",".join("?" for unused in identities)
    rows = database.execute("SELECT action_id FROM outreach_campaign_preparations WHERE identity IN ("
                            + placeholders + ")", identities).fetchall()
    for row in rows:
        if row[0] != action_id:
            if continuation and {"outreach_prepared_campaigns", "outreach_attempts"} <= tables:
                completed = database.execute("""SELECT 1 FROM outreach_prepared_campaigns prepared
                    JOIN outreach_attempts attempts USING(campaign_id)
                    WHERE prepared.action_id=? AND attempts.state='CONFIRMED_SENT'""", (row[0],)).fetchone()
                if completed and outreach._confirmed_campaign(database, identity):
                    continue
            return ["CAMPAIGN_PREPARATION_OWNS_RECIPIENT"]
        if campaign_id is not None:
            matched = ("outreach_prepared_campaigns" in tables and database.execute(
                "SELECT 1 FROM outreach_prepared_campaigns WHERE campaign_id=? AND action_id=? AND identity IN ("
                + placeholders + ")", (campaign_id, action_id, *identities)).fetchone())
            if not matched:
                return ["CAMPAIGN_HANDOFF_OWNER_MISMATCH"]
    return []


def _guard(database, config, action, proof, now, *, before_list=False):
    _require(action.get("purpose") == "PREPARE_CAMPAIGN", "CAMPAIGN_PURPOSE_REQUIRED")
    _require(proof.get("agent_decision") in outreach.PROCEED_DECISIONS and proof.get("route") == "INVITATION",
             "AI_INVITATION_DECISION_REQUIRED")
    # HISTORY_CAPABILITY_GATED deleted (owner, 2026-09-23). It refused on ANY capability gate
    # recorded by the history collector, including the coverage gate that no longer blocks at
    # any other stage - so preparation and dispatch disagreed and a recipient cleared by the
    # judge died here instead. Positive history, suppression and the immutable claim are
    # checked just below and in outreach.action_gates, unchanged.
    # PROVIDER_SNAPSHOT_INCOMPLETE deleted (owner, 2026-09-30: "assuming that the person hasnt
    # been contacted because a snapshot was incomplete is kind of dumb"). An incomplete HeyReach
    # read is not evidence of prior contact; what it did show, our own records and HeyReach's
    # duplicate exclusions still refuse.
    _require(type(config.get("sender_id")) is int and config["sender_id"] > 0,
             "APPROVED_SENDER_REQUIRED")
    own_claim = database.execute("SELECT payload FROM outreach_action_claims WHERE action_id=?", (action["action_id"],)).fetchone()
    if own_claim:
        frozen = json.loads(own_claim[0])
        _require(all(frozen.get(field) == value for field, value in action.items()), "IMMUTABLE_PREPARATION_PAYLOAD_CHANGED")
    gates = outreach.action_gates(database, config, action, proof, now)
    has_queue = database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='outreach_queue'").fetchone()
    queued = database.execute("SELECT payload,state FROM outreach_queue WHERE identity=?", (outreach.profile_key(action["profile"]),)).fetchone() if has_queue else None
    if queued and queued["state"] in {"PENDING_VALIDATION", "BLOCKED", "HELD_INSPECTION"}:
        own = outreach._prepared_owner(database, json.loads(queued["payload"]))
        if own and own["action_id"] == action["action_id"]:
            gates = [gate for gate in gates if gate != "LEGACY_QUEUE_RESERVATION"]
    if before_list:
        gates = [gate for gate in gates if gate != "SINGLE_CONTACT_LIST_UNVERIFIED"]
    gates.extend(reservation_gates(database, action["profile"], action["action_id"]))
    identities = engagement.identity_keys(database, action["profile"])
    placeholders = ",".join("?" for unused in identities)
    import inactivity_closure
    gates.extend(inactivity_closure.send_gates(database, config, action["profile"], now))
    if engagement.closure_status(database, action["profile"]):
        gates.append("OUTREACH_CLOSED")
    # Saved local history findings (KNOWN_PRIOR_OUTREACH_OR_STOP) are not re-read here: a new
    # contact's records were checked once, before the draft was created (owner, 2026-10-01).
    if database.execute("SELECT 1 FROM outreach_action_claims WHERE identity IN (" + placeholders
                        + ") AND action_id<>?", (*identities, action["action_id"])).fetchone():
        gates.append("IMMUTABLE_OUTREACH_CLAIM_EXISTS")
    if database.execute("SELECT 1 FROM outreach_actions WHERE identity IN (" + placeholders
                        + ") AND action_id<>? AND state NOT IN ('CANCELLED','BLOCKED')",
                        (*identities, action["action_id"])).fetchone():
        gates.append("OTHER_OUTREACH_ACTION_RESERVED")
    _require(not gates, ";".join(sorted(set(gates))))


# Intent states: REQUEST_STARTED -> ACKNOWLEDGED_UNCONFIRMED -> RECONCILED, or on an error
# RETRY_WAIT (attempts left) / WRITE_FAILED (terminal). The owner's retry rule applies going
# forward: a write earlier code left UNCERTAIN_NO_RETRY stays reconciliation-only, exactly like a
# launch earlier code left UNCERTAIN_NO_RETRY (which delivery reconciliation still reads).
RETRYABLE_WRITE_STATES = ("RETRY_WAIT",)


def _failures(result):
    """The failed attempts recorded in an intent's result: {attempts, errors, next_at}."""
    try:
        value = json.loads(result) if isinstance(result, str) else result
    except ValueError:
        value = None
    if isinstance(value, dict) and type(value.get("attempts")) is int:
        return {"attempts": value["attempts"], "errors": list(value.get("errors") or []), "next_at": value.get("next_at")}
    return {"attempts": 0, "errors": [], "next_at": None}


def write_retry_due(state, result, now):
    """A failed write with attempts left whose backoff has passed; never an acknowledged one."""
    if state not in RETRYABLE_WRITE_STATES:
        return False
    failures = _failures(result)
    return failures["attempts"] < outreach.WRITE_ATTEMPT_LIMIT and (
        not failures["next_at"] or engagement.timestamp(failures["next_at"]) <= engagement.timestamp(now))


def _record_failure(database, action_id, operation, arguments, proof, error, now):
    """Record one failed write attempt, inserting the intent if the attempt failed before it."""
    failed_at = engagement.timestamp(now or dt.datetime.now(dt.timezone.utc)).isoformat()
    with engagement.transaction(database):
        row = database.execute("SELECT state,result FROM outreach_campaign_intents WHERE action_id=? AND operation=?",
                               (action_id, operation)).fetchone()
        prior = _failures(row["result"]) if row and row["state"] in ("REQUEST_STARTED", *RETRYABLE_WRITE_STATES) else _failures(None)
        attempts = prior["attempts"] + 1
        next_at = outreach.write_retry_at(attempts, failed_at)
        record = {"attempts": attempts, "next_at": next_at,
                  "errors": prior["errors"] + [{"attempt": attempts, "at": failed_at, "error": outreach._error_detail(error)}]}
        state = "RETRY_WAIT" if next_at else "WRITE_FAILED"
        if row is None:
            database.execute("INSERT INTO outreach_campaign_intents VALUES (?,?,?,?,?,?,?)",
                             (action_id, operation, _encoded(arguments), failed_at, state, _encoded(record), _encoded(proof)))
        else:
            database.execute("UPDATE outreach_campaign_intents SET state=?,result=? WHERE action_id=? AND operation=?",
                             (state, _encoded(record), action_id, operation))
    return state


def _write_once(database, config, client, action, proof, operation, arguments, now, tool=None):
    """Issue one list write, or retry a failed one that is due.

    The caller reads HeyReach first and calls this only when the result is absent, so a retry
    never repeats a write that landed. An acknowledged write is never repeated here.
    """
    _require(not database.in_transaction, "PROVIDER_WRITE_REQUIRES_COMMITTED_INTENT")
    encoded = _encoded(arguments)
    with engagement.transaction(database):
        existing = database.execute("SELECT arguments,state,result FROM outreach_campaign_intents WHERE action_id=? AND operation=?",
                                    (action["action_id"], operation)).fetchone()
        if existing:
            _require(existing[0] == encoded, "CAMPAIGN_INTENT_PAYLOAD_CHANGED")
            if not write_retry_due(existing[1], existing[2], now):
                return
        _guard(database, config, action, proof, now, before_list=True)
        owner = database.execute("SELECT payload,sender_id FROM outreach_campaign_preparations WHERE action_id=?",
                                 (action["action_id"],)).fetchone()
        _require(owner and owner[0] == _encoded(action) and owner[1] == config["sender_id"],
                 "CAMPAIGN_PREPARATION_OWNER_CHANGED")
        if existing:
            prior = _failures(existing[2])
            changed = database.execute("UPDATE outreach_campaign_intents SET state='REQUEST_STARTED',result=? WHERE action_id=? AND operation=? AND state=?",
                                       (_encoded(prior), action["action_id"], operation, existing[1]))
            _require(changed.rowcount == 1, "CAMPAIGN_INTENT_CHANGED_BEFORE_RETRY")
        else:
            prior = None
            database.execute("INSERT INTO outreach_campaign_intents VALUES (?,?,?,?,'REQUEST_STARTED',NULL,?)",
                             (action["action_id"], operation, encoded, now.isoformat(), _encoded(proof)))
    try:
        result = client._request(tool or operation, arguments)
    except Exception as error:
        _record_failure(database, action["action_id"], operation, arguments, proof, error, now)
        return
    if prior:
        result = dict(result if isinstance(result, dict) else {"response": result},
                      write_attempts=prior["attempts"] + 1, failed_attempts=prior["errors"])
    with engagement.transaction(database):
        database.execute("UPDATE outreach_campaign_intents SET state='ACKNOWLEDGED_UNCONFIRMED',result=? WHERE action_id=? AND operation=?",
                         (_encoded(result), action["action_id"], operation))


def _write_hold(database, action_id, operation, label):
    """Why a list step has no visible result yet: waiting to retry, terminal, or an unconfirmed ACK."""
    row = database.execute("SELECT state,result FROM outreach_campaign_intents WHERE action_id=? AND operation=?",
                           (action_id, operation)).fetchone()
    if row and row["state"] == "WRITE_FAILED":
        return {"state": "FAILED", "action_id": action_id, "blocker": label + "_WRITE_FAILED",
                "failures": _failures(row["result"])}
    if row and row["state"] in RETRYABLE_WRITE_STATES:
        failures = _failures(row["result"])
        return {"state": "RECONCILIATION_REQUIRED", "action_id": action_id, "blocker": label + "_RETRY_WAIT",
                "next_at": failures["next_at"], "failures": failures}
    return {"state": "RECONCILIATION_REQUIRED", "action_id": action_id,
            "blocker": label + "_OUTCOME_UNKNOWN_NO_RETRY"}


def _complete_listing(response):
    _require(isinstance(response, dict) and isinstance(response.get("items"), list)
             and type(response.get("totalCount")) is int
             and response["totalCount"] == len(response["items"]), "PROVIDER_LISTING_INCOMPLETE")
    return response["items"]


def _campaign_arguments(action, sender_id):
    return {"linkedInAccountIds": [sender_id], "linkedInUserListId": action["list_id"],
            "name": action["campaign_name"], "sequenceJson": json.dumps(action["sequence"]),
            "excludeContactedFromOtherCampaigns": True, "excludeContactedFromSenderInOtherCampaign": True,
            "excludeFromCompanyBlacklist": True, "excludeFromLeadBlacklist": True,
            "excludeHasOtherAccConversations": True}


def _matching_campaigns(client, action):
    items = _complete_listing(client.call("get_all_campaigns", {
        "keyword": action["campaign_name"], "offset": 0, "limit": 100}))
    return [item for item in items if item.get("name") == action["campaign_name"]]


def create_claimed(database, config, client, action_id, now=None, *, retry=False):
    """Concrete Adapter transport for the existing PREPARE_CAMPAIGN claim.

    Any failure after the claim is read is recorded on the create_campaign intent as one failed
    attempt (RETRY_WAIT, or WRITE_FAILED on the third), so retry_create can try again after
    reading HeyReach by the campaign's deterministic name (owner, 2026-09-30).
    """
    _require(not database.in_transaction, "COMMITTED_CAMPAIGN_CLAIM_REQUIRED")
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    _require({"outreach_campaign_preparations", "outreach_campaign_intents"} <= tables,
             "CAMPAIGN_PREPARATION_OWNER_REQUIRED")
    row = database.execute("SELECT * FROM outreach_actions WHERE action_id=?", (action_id,)).fetchone()
    claim = database.execute("SELECT * FROM outreach_action_claims WHERE action_id=?", (action_id,)).fetchone()
    _require(row and claim and row["state"] == "REQUEST_STARTED", "STARTED_CAMPAIGN_CLAIM_REQUIRED")
    action, proof = json.loads(claim["payload"]), json.loads(claim["preflight"])
    arguments = _campaign_arguments(action, claim["sender_id"])
    current = engagement.timestamp(now or dt.datetime.now(dt.timezone.utc))
    owned = []
    try:
        return _create_claimed(database, config, client, action_id, row, claim, action, proof, arguments, current, now, retry, owned)
    except Exception as error:
        # Only this attempt's own failure is recorded: never an acknowledged or in-flight write.
        recorded = database.execute("SELECT 1 FROM outreach_campaign_intents WHERE action_id=? AND operation='create_campaign'",
                                    (action_id,)).fetchone()
        if owned or not recorded:
            _record_failure(database, action_id, "create_campaign", arguments, proof, error, current)
        raise


def _create_claimed(database, config, client, action_id, row, claim, action, proof, arguments, current, now, retry, owned):
    _require(row["payload"] == claim["payload"] and row["payload_hash"] == claim["payload_hash"]
             == hashlib.sha256(row["payload"].encode()).hexdigest()
             and row["identity"] == claim["identity"] and claim["sender_id"] == config["sender_id"],
             "CAMPAIGN_CLAIM_MISMATCH")
    owner = database.execute("SELECT * FROM outreach_campaign_preparations WHERE action_id=?", (action_id,)).fetchone()
    # The owner's Revise and Approve (review fix P2) changes the note and its sequence after the
    # preparation was recorded: the binding then holds for the payload the owner revised.
    binding = json.loads(owner["payload"]) if owner else {}
    import approvals
    _require(owner and owner["sender_id"] == claim["sender_id"] and owner["list_name"] == action.get("campaign_name")
             and (all(action.get(key) == value for key, value in binding.items())
                  or approvals.revised_binding(database, action_id, action, binding)),
             "CAMPAIGN_PREPARATION_BINDING_CHANGED")
    added = database.execute("SELECT arguments,state FROM outreach_campaign_intents WHERE action_id=? AND operation IN (?,?) "
                             "ORDER BY operation=? DESC", (action_id, "add_leads_to_list_v2", FULL_LEAD_ADD, FULL_LEAD_ADD)).fetchone()
    _require(added and added["state"] == "RECONCILED" and json.loads(added["arguments"])["listId"] == action["list_id"],
             "SINGLE_CONTACT_LIST_NOT_RECONCILED")
    existing = database.execute("SELECT state,result FROM outreach_campaign_intents WHERE action_id=? AND operation='create_campaign'",
                                (action_id,)).fetchone()
    _require(not existing or (retry and write_retry_due(existing["state"], existing["result"], current)),
             "CAMPAIGN_CREATE_ALREADY_ATTEMPTED_NO_RETRY")
    if existing:
        owned.append(True)          # this retry is the attempt; any refusal from here counts
    # The idempotency read: a draft by this deterministic name means an earlier write landed.
    _require(not _matching_campaigns(client, action), "CAMPAIGN_NAME_ALREADY_EXISTS")
    leads = _complete_listing(client.call("get_leads_from_list", {"listId": action["list_id"], "offset": 0, "limit": 100}))
    _require(len(leads) == 1 and outreach.profile_key(leads[0].get("profileUrl")) == claim["identity"],
             "SINGLE_CONTACT_LIST_UNVERIFIED")
    request = {"operation": "create_campaign", "arguments": arguments, "status": "REQUEST_STARTED",
               "payload_hash": claim["payload_hash"], "started_at": current.isoformat()}
    with engagement.transaction(database):
        current_row = database.execute("SELECT state,payload,result FROM outreach_actions WHERE action_id=?", (action_id,)).fetchone()
        _require(current_row["state"] == "REQUEST_STARTED" and current_row["payload"] == claim["payload"],
                 "CAMPAIGN_CLAIM_CHANGED_BEFORE_WRITE")
        # A retry runs under the original claim, so the claim's proof is judged as of the claim;
        # suppressions, stops, closure and other claims are read live either way. A draft
        # campaign contacts nobody; the launch has its own fresh guard.
        _guard(database, config, action, proof, engagement.timestamp(claim["claimed_at"]) if existing else current)
        previous = json.loads(current_row["result"] or "{}")
        _require(isinstance(previous, dict) and ("provider_request" not in previous or existing is not None),
                 "CAMPAIGN_REQUEST_ALREADY_CONSUMED")
        if existing is None:
            database.execute("INSERT INTO outreach_campaign_intents VALUES (?,?,?,?,'REQUEST_STARTED',NULL,?)",
                             (action_id, "create_campaign", _encoded(arguments), current.isoformat(), _encoded(proof)))
        else:
            _require(json.loads(database.execute("SELECT arguments FROM outreach_campaign_intents WHERE action_id=? AND operation='create_campaign'",
                                                 (action_id,)).fetchone()[0]) == arguments, "CAMPAIGN_INTENT_PAYLOAD_CHANGED")
            changed = database.execute("UPDATE outreach_campaign_intents SET state='REQUEST_STARTED',result=? WHERE action_id=? "
                                       "AND operation='create_campaign' AND state=?",
                                       (_encoded(_failures(existing["result"])), action_id, existing["state"]))
            _require(changed.rowcount == 1, "CAMPAIGN_INTENT_CHANGED_BEFORE_RETRY")
            if "provider_request" in previous:
                previous = dict(previous, previous_provider_requests=[*previous.get("previous_provider_requests", []),
                                                                      previous.pop("provider_request")])
        database.execute("UPDATE outreach_actions SET result=? WHERE action_id=?",
                         (_encoded(dict(previous, provider_request=request)), action_id))
    owned.append(True)
    result = client._request("create_campaign", arguments)
    with engagement.transaction(database):
        database.execute("UPDATE outreach_campaign_intents SET state='ACKNOWLEDGED_UNCONFIRMED',result=? WHERE action_id=? AND operation='create_campaign'",
                         (_encoded(result), action_id))
    return {"request": request, "response": result,
            "finished_at": (engagement.timestamp(now) if now else dt.datetime.now(dt.timezone.utc)).isoformat()}


def failed_create_state(database, action_id):
    """The action state for a failed draft-campaign write, or None when no failure was recorded."""
    if not database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='outreach_campaign_intents'").fetchone():
        return None
    row = database.execute("SELECT state FROM outreach_campaign_intents WHERE action_id=? AND operation='create_campaign'",
                           (action_id,)).fetchone()
    return {"RETRY_WAIT": "WRITE_RETRY_WAIT", "WRITE_FAILED": "WRITE_FAILED"}.get(row[0]) if row else None


def retry_create(database, config, client, action, claim, now=None):
    """Write the draft again under the same claim once HeyReach shows none by its name."""
    action_id = action["action_id"]
    current = engagement.timestamp(now or dt.datetime.now(dt.timezone.utc))
    intent = database.execute("SELECT state,result FROM outreach_campaign_intents WHERE action_id=? AND operation='create_campaign'",
                              (action_id,)).fetchone()
    if not intent or not write_retry_due(intent["state"], intent["result"], current):
        failures = _failures(intent["result"]) if intent else _failures(None)
        return {"state": "UNKNOWN", "blocker": "CAMPAIGN_CREATE_RETRY_WAIT", "next_at": failures["next_at"],
                "next_action": "Retry the failed draft write when due; HeyReach is read by name first"}
    with engagement.transaction(database):
        changed = database.execute("UPDATE outreach_actions SET state='REQUEST_STARTED' WHERE action_id=? "
                                   "AND state='WRITE_RETRY_WAIT'", (action_id,))
    if changed.rowcount != 1:
        return {"state": "UNKNOWN", "blocker": "CAMPAIGN_RETRY_CLAIM_CHANGED"}
    try:
        create_claimed(database, config, client, action_id, current, retry=True)
        state, error = "ACKNOWLEDGED_UNCONFIRMED", None
    except Exception as failure:
        state, error = failed_create_state(database, action_id) or "UNCERTAIN_NO_RETRY", outreach._error_detail(failure)
    with engagement.transaction(database):
        database.execute("UPDATE outreach_actions SET state=? WHERE action_id=? AND state='REQUEST_STARTED'", (state, action_id))
    return {"state": "UNKNOWN", "retried": True, "action_state": state, "error": error,
            "next_action": "Reconcile the retried draft by name"}


def reconcile_prepared(database, config, client, action, claim, now=None):
    """Independent draft existence receipt, bound to a committed unique request."""
    claim = dict(claim)
    _require(json.loads(claim["payload"]) == action and claim["sender_id"] == config["sender_id"]
             and hashlib.sha256(claim["payload"].encode()).hexdigest() == claim["payload_hash"],
             "CAMPAIGN_RECONCILIATION_CLAIM_MISMATCH")
    intent = database.execute("SELECT * FROM outreach_campaign_intents WHERE action_id=? AND operation='create_campaign'",
                              (action["action_id"],)).fetchone()
    _require(intent and json.loads(intent["arguments"]) == _campaign_arguments(action, claim["sender_id"]),
             "CAMPAIGN_CREATE_INTENT_REQUIRED")
    matches = _matching_campaigns(client, action)
    if not matches and intent["state"] in RETRYABLE_WRITE_STATES:
        # The write failed and HeyReach shows no draft by this name: retry it (owner, 2026-09-30).
        return retry_create(database, config, client, action, claim, now)
    if len(matches) != 1:
        return {"state": "UNKNOWN", "next_action": "Reconcile the unique claimed draft by name; an acknowledged creation is never repeated"}
    campaign_id = matches[0].get("id")
    _require(type(campaign_id) is int and campaign_id > 0, "CAMPAIGN_ID_UNVERIFIED")
    observed = client.call("get_campaign", {"campaignId": campaign_id})
    sequence = client.call("get_campaign_sequence", {"campaignId": campaign_id})
    leads = client.call("get_leads_from_list", {"listId": action["list_id"], "offset": 0, "limit": 100})
    items = _complete_listing(leads)
    _require(observed.get("id") == campaign_id and observed.get("name") == action["campaign_name"]
             and observed.get("linkedInUserListId") == action["list_id"] and observed.get("status") == "DRAFT"
             and not observed.get("startedAt") and observed.get("campaignAccountIds") == [claim["sender_id"]]
             and all(observed.get(field) is True for field in ("excludeInOtherCampaigns",
                "excludeContactedFromSenderInOtherCampaign", "excludeHasOtherAccConversations",
                "excludeFromCompanyBlacklist", "excludeFromLeadBlacklist"))
             and _same_sequence(sequence, action["sequence"]) and len(items) == 1
             and outreach.profile_key(items[0].get("profileUrl")) == claim["identity"],
             "OBSERVED_CAMPAIGN_DOES_NOT_MATCH_CLAIM")
    current = engagement.timestamp(now or dt.datetime.now(dt.timezone.utc))
    _require(engagement.timestamp(claim["claimed_at"]) <= engagement.timestamp(intent["started_at"]) <= current,
             "CAMPAIGN_REQUEST_TIME_MISMATCH")
    observation = {"campaign": observed, "sequence": sequence, "leads": leads, "observed_at": current.isoformat()}
    receipt_id = "campaign-observation:" + hashlib.sha256(_encoded(observation).encode()).hexdigest()
    with engagement.transaction(database):
        database.execute("UPDATE outreach_campaign_intents SET state='RECONCILED',result=? WHERE action_id=? AND operation='create_campaign'",
                         (_encoded(observation), action["action_id"]))
    return {"independent": True, "operation": "create_campaign", "outcome": "DRAFT_CREATED",
            "action_id": action["action_id"], "identity": claim["identity"], "payload_hash": claim["payload_hash"],
            "sender_id": claim["sender_id"], "campaign_id": campaign_id, "receipt_id": receipt_id,
            "evidence": "outreach_campaign_intents:" + action["action_id"] + ":create_campaign",
            "occurred_at": current.isoformat(), "observed_at": current.isoformat()}


def _find_list(client, name):
    items = _complete_listing(client.call("get_all_lists", {
        "keyword": name, "listType": "USER_LIST", "offset": 0, "limit": 100}))
    matches = [item for item in items if item.get("name") == name]
    _require(len(matches) <= 1, "AMBIGUOUS_LIST_IDENTITY_NO_RETRY")
    if not matches:
        return None
    found = matches[0]
    _require(type(found.get("id")) is int and found["id"] > 0, "LIST_ID_UNVERIFIED")
    detail = client.call("get_list_by_id", {"listId": found["id"]})
    _require(detail.get("id") == found["id"] and detail.get("name") == name
             and detail.get("listType", detail.get("type")) == "USER_LIST", "LIST_BINDING_UNVERIFIED")
    return detail


FULL_LEAD_ADD = "add_leads_to_list_v2:full"


def _lead(action, url):
    """The lead HeyReach actually imports: profile URL plus the verified name and company."""
    first, _, last = str(action.get("recipient") or "").strip().partition(" ")
    return {"profileUrl": url, "firstName": first or None, "lastName": last.strip() or None,
            "companyName": action.get("company") or None}


def _prepare_list(database, config, client, action, proof, now=None):
    """Return a fresh promotion context or a truthful reconciliation hold."""
    realtime = now is None
    current = engagement.timestamp(now or dt.datetime.now(dt.timezone.utc))
    initialize(database)
    _guard(database, config, action, proof, current, before_list=True)
    with engagement.transaction(database):
        _guard(database, config, action, proof, current, before_list=True)
        owner = database.execute("SELECT * FROM outreach_campaign_preparations WHERE action_id=?",
                                 (action["action_id"],)).fetchone()
        if owner is None:
            database.execute("INSERT INTO outreach_campaign_preparations VALUES (?,?,?,?,?,?)",
                (action["action_id"], outreach.profile_key(action["profile"]), config["sender_id"],
                 _encoded(action), "outreach-" + secrets.token_hex(16), current.isoformat()))
            owner = database.execute("SELECT * FROM outreach_campaign_preparations WHERE action_id=?",
                                     (action["action_id"],)).fetchone()
        _require(owner["payload"] == _encoded(action) and owner["sender_id"] == config["sender_id"],
                 "CAMPAIGN_PREPARATION_OWNER_CHANGED")
    action_id, name = action["action_id"], owner["list_name"]
    created = database.execute("SELECT state,result FROM outreach_campaign_intents WHERE action_id=? AND operation='create_empty_list'",
                               (action_id,)).fetchone()
    detail = _find_list(client, name)
    if not created:
        _require(detail is None, "LIST_NAME_ALREADY_EXISTS")
    if detail is None and (not created or write_retry_due(created["state"], created["result"],
                                                          dt.datetime.now(dt.timezone.utc) if realtime else current)):
        # The first write, or a failed one retried because HeyReach shows no list by this name.
        _write_once(database, config, client, action, proof, "create_empty_list",
                    {"listName": name, "listType": "USER_LIST"}, dt.datetime.now(dt.timezone.utc) if realtime else current)
        detail = _find_list(client, name)
    if detail is None:
        return _write_hold(database, action_id, "create_empty_list", "LIST_CREATE")
    list_id = detail["id"]
    leads = _complete_listing(client.call("get_leads_from_list", {"listId": list_id, "offset": 0, "limit": 100}))
    added = database.execute("SELECT arguments,state,result FROM outreach_campaign_intents WHERE action_id=? AND operation='add_leads_to_list_v2'",
                            (action_id,)).fetchone()
    url = "https://www." + outreach.profile_key(action["profile"]) + "/"
    arguments = {"listId": list_id, "leads": [_lead(action, url)]}
    if added and added[0] == _encoded({"listId": list_id, "leads": [{"profileUrl": url}]}):
        # HeyReach acknowledges a profile-URL-only lead with 0 added / 0 updated / 0 failed and adds
        # nothing (seen 2026-09-19). Acknowledged zero counts plus a fresh empty list is a
        # known non-application, not an uncertain write: one full-lead write, recorded separately.
        counts = json.loads(added[2] or "{}")
        _require(added[1] in {"ACKNOWLEDGED_UNCONFIRMED", "RECONCILED"}, "LIST_ADD_OUTCOME_UNKNOWN_NO_RETRY")
        if not leads:
            _require(all(counts.get(field) == 0 for field in ("addedLeadsCount", "updatedLeadsCount", "failedLeadsCount")),
                     "LIST_ADD_OUTCOME_UNKNOWN_NO_RETRY")
            _write_once(database, config, client, action, proof, FULL_LEAD_ADD, arguments,
                        dt.datetime.now(dt.timezone.utc) if realtime else current, tool="add_leads_to_list_v2")
            leads = _complete_listing(client.call("get_leads_from_list", {"listId": list_id, "offset": 0, "limit": 100}))
    elif added:
        _require(added[0] == _encoded(arguments), "LIST_ADD_BINDING_CHANGED")
        if not leads and write_retry_due(added[1], added[2], dt.datetime.now(dt.timezone.utc) if realtime else current):
            # A failed add retried because the list still shows nobody (owner, 2026-09-30).
            _write_once(database, config, client, action, proof, "add_leads_to_list_v2", arguments,
                        dt.datetime.now(dt.timezone.utc) if realtime else current)
            leads = _complete_listing(client.call("get_leads_from_list", {"listId": list_id, "offset": 0, "limit": 100}))
    else:
        _require(not leads, "UNEXPECTED_LIST_MEMBERSHIP")
        _write_once(database, config, client, action, proof, "add_leads_to_list_v2", arguments,
                    dt.datetime.now(dt.timezone.utc) if realtime else current)
        leads = _complete_listing(client.call("get_leads_from_list", {"listId": list_id, "offset": 0, "limit": 100}))
    if not leads:
        full = database.execute("SELECT 1 FROM outreach_campaign_intents WHERE action_id=? AND operation=?",
                                (action_id, FULL_LEAD_ADD)).fetchone()
        return _write_hold(database, action_id, FULL_LEAD_ADD if full else "add_leads_to_list_v2", "LIST_ADD")
    _require(len(leads) == 1 and outreach.profile_key(leads[0].get("profileUrl"))
             == outreach.profile_key(action["profile"]), "SINGLE_CONTACT_LIST_UNVERIFIED")
    with engagement.transaction(database):
        database.execute("UPDATE outreach_campaign_intents SET state='RECONCILED' WHERE action_id=?", (action_id,))
    return {"state": "READY", "purpose": "PREPARE_CAMPAIGN", "action_id": action_id,
            "list_id": list_id, "campaign_name": name, "sequence": action["sequence"],
            "proof": dict(proof, list_id=list_id, single_contact_list_verified=True)}


def prepare_list(database, config, client, action, proof, now=None):
    """Prepare once; uncertain list writes receive at most three scheduled reads."""
    initialize(database)
    current = engagement.timestamp(now or dt.datetime.now(dt.timezone.utc))
    failed = database.execute("SELECT operation,result FROM outreach_campaign_intents WHERE action_id=? AND state='WRITE_FAILED' "
                              "AND operation IN ('create_empty_list','add_leads_to_list_v2','add_leads_to_list_v2:full')",
                              (action["action_id"],)).fetchone()
    if failed:
        # Three failed attempts: terminal, with every error kept. Never written again.
        return {"state": "FAILED", "action_id": action["action_id"], "operation": failed[0],
                "blocker": "LIST_WRITE_FAILED", "failures": _failures(failed[1])}
    _guard(database, config, action, proof, current, before_list=True)
    pending = database.execute("SELECT 1 FROM outreach_campaign_intents WHERE action_id=? AND operation IN ('create_empty_list','add_leads_to_list_v2','add_leads_to_list_v2:full') AND state<>'RECONCILED'",
                               (action["action_id"],)).fetchone()
    read_id = "campaign-list:" + action["action_id"]
    token = outreach._claim_read(database, read_id, "RECONCILIATION", current) if pending else None
    if pending and token is None:
        return {"state": "RECONCILIATION_REQUIRED", "action_id": action["action_id"],
                "blocker": "LIST_WRITE_OUTCOME_UNKNOWN_NO_RETRY",
                "read_retry": outreach._read_status(database, read_id, "RECONCILIATION")}
    try:
        result = _prepare_list(database, config, client, action, proof, now)
    except Exception as error:
        if token:
            outcome, safe_error = outreach._read_error(error)
            outreach._finish_read(database, read_id, "RECONCILIATION", token,
                engagement.timestamp(now or dt.datetime.now(dt.timezone.utc)), outcome, safe_error)
        raise
    if token:
        finished = engagement.timestamp(now or dt.datetime.now(dt.timezone.utc))
        outreach._finish_read(database, read_id, "RECONCILIATION", token, finished,
                             "COMPLETE" if result["state"] == "READY" else "RETRY_WAIT", result.get("blocker"))
    return result


def prepare_candidates(database, config, client, candidates, now=None):
    """Consume prepare_drafts.campaign_candidates and return promotion contexts."""
    current = engagement.timestamp(now or dt.datetime.now(dt.timezone.utc))
    # Ceiling 4 -> 7 (owner, 2026-09-23: "one outreach per cycle is terrible for throughput.
    # Lets do a batch of 7."). outreach_agent.py was raised to 7 at the time and these two were
    # not, so the SAME config key passed one reader and raised in the other: campaign preparation
    # and the campaign cycle both failed outright and no outreach could be prepared or sent at all.
    limit = config.get("outreach_agent", {}).get("max_items", 2)
    _require(type(limit) is int and 1 <= limit <= 7, "CAMPAIGN_PREPARATION_LIMIT_INVALID")
    result = {"items": [], "contexts": {}}
    example = None
    for candidate in candidates[:limit]:
        item = {"profile": candidate.get("profile"), "state": "HELD"}
        result["items"].append(item)
        try:
            _require(candidate.get("route") == "INVITATION", "AI_INVITATION_DECISION_REQUIRED")
            row = database.execute("SELECT * FROM rw_drafts WHERE identity=? ORDER BY version DESC LIMIT 1",
                                   (candidate["draft_source"]["identity"],)).fetchone()
            _require(row and outreach_promotion._source(row) == candidate["draft_source"], "CAMPAIGN_SOURCE_CHANGED")
            draft, identity, verification = outreach_promotion._validate(
                database, row, current, require_composition=outreach_promotion.composition_required(config))
            _require(draft == candidate["draft"] and verification == candidate["verification"], "CAMPAIGN_DRAFT_CHANGED")
            outreach_promotion._local_gates(database, config, draft, identity)
            if example is None:
                listed = client.call("get_all_campaigns", {"accountIds": [config["sender_id"]], "offset": 0, "limit": 100})
                _require(isinstance(listed, dict) and isinstance(listed.get("items"), list), "SEQUENCE_EXAMPLE_LIST_UNAVAILABLE")
                for reference in listed["items"][:4]:
                    _require(type(reference.get("id")) is int and reference["id"] > 0, "SEQUENCE_EXAMPLE_ID_INVALID")
                    observed = client.call("get_campaign_sequence", {"campaignId": reference["id"]})
                    if isinstance(observed, dict) and observed.get("nodeType") == "CONNECTION_REQUEST":
                        example = {"campaign_id": reference["id"], "sequence": observed}
                        break
            _require(example is not None, "OBSERVED_CONNECTION_SEQUENCE_REQUIRED")
            sequence = invitation_sequence(example["sequence"], draft["invitation"])
            action = {"action_id": "research-outreach:" + research.digest([identity, "PREPARE_CAMPAIGN"]),
                      "profile": research.profile_key(draft["profile"]), "recipient": draft["recipient"],
                      "company": draft["company"], "purpose": "PREPARE_CAMPAIGN", "message": draft["invitation"],
                      "channel": "linkedin", "provider": "heyreach", "verification": verification,
                      "source": candidate["draft_source"], "sequence": sequence}
            proof = dict(candidate["proof"], sequence=sequence, sequence_observation=example,
                         sequence_source="heyreach:get_campaign_sequence:" + str(example["campaign_id"]))
            context = prepare_list(database, config, client, action, proof, now)
            item.update(state=context["state"], action_id=action["action_id"])
            if context["state"] == "READY":
                result["contexts"][identity] = context
            else:
                item["blocker"] = context["blocker"]
        except Exception as error:
            item.update(state="HELD", blocker=str(error) if isinstance(error, ValueError) else type(error).__name__)
    return result


def handoff(database, config, client, action_id, proof, now=None, *, read_token=None):
    """Enqueue only an independently reconciled, exact draft; never launch."""
    current = engagement.timestamp(now or dt.datetime.now(dt.timezone.utc))
    _require(not database.in_transaction, "HANDOFF_REQUIRES_OUTER_TRANSACTION_OWNERSHIP")
    row = database.execute("SELECT * FROM outreach_actions WHERE action_id=?", (action_id,)).fetchone()
    claim = database.execute("SELECT * FROM outreach_action_claims WHERE action_id=?", (action_id,)).fetchone()
    prepared = database.execute("SELECT campaign_id,identity FROM outreach_prepared_campaigns WHERE action_id=?", (action_id,)).fetchone()
    _require(row and claim and prepared and row["state"] == "PREPARED", "CAMPAIGN_NOT_RECONCILED")
    _require(row["payload"] == claim["payload"] and row["payload_hash"] == claim["payload_hash"]
             == hashlib.sha256(row["payload"].encode()).hexdigest()
             and row["identity"] == claim["identity"] == prepared["identity"]
             and claim["sender_id"] == config["sender_id"], "CAMPAIGN_CLAIM_MISMATCH")
    action = json.loads(claim["payload"])
    campaign_id = prepared["campaign_id"]
    existing = database.execute("SELECT payload,state FROM outreach_queue WHERE identity=? OR campaign_id=?",
                                (row["identity"], campaign_id)).fetchone()
    if existing:
        candidate = json.loads(existing[0])
        _require(candidate.get("preparation_action_id") == action_id and candidate.get("campaign_id") == campaign_id
                 and candidate.get("preparation_payload_hash") == claim["payload_hash"], "LEGACY_HANDOFF_CONFLICT")
        if existing["state"] not in {"PENDING_VALIDATION", "BLOCKED", "HELD_INSPECTION"}:
            return {"state": "ALREADY_QUEUED", "identity": row["identity"], "campaign_id": campaign_id}
    _guard(database, config, action, proof, current)
    candidate = {key: action[key] for key in ("profile", "recipient", "company", "message", "verification")}
    candidate.update(campaign_id=campaign_id, preparation_action_id=action_id,
                     preparation_payload_hash=claim["payload_hash"], list_id=action["list_id"], launch_proof=proof)
    candidate["verification"] = dict(action["verification"],
        linkedin_messages_checked=proof["direct_messages_complete"],
        linkedin_sent_invitations_checked=proof["sent_invitations_complete"],
        verified_at=proof["checked_at"], history_evidence=proof["evidence"],
        **{field: proof[field] for field in outreach_history.POSITIVE_FIELDS})
    campaign = client.call("get_campaign", {"campaignId": campaign_id})
    _require(campaign.get("linkedInUserListId") == action["list_id"]
             and campaign.get("name") == action["campaign_name"], "PREPARED_CAMPAIGN_BINDING_CHANGED")
    gates = outreach.provider_gates(client, config, candidate)
    _require(not gates, ";".join(gates))
    with engagement.transaction(database):
        current = engagement.timestamp(now or dt.datetime.now(dt.timezone.utc))
        if read_token is not None:
            reading = database.execute("SELECT state,token,lease_until FROM outreach_read_retries WHERE action_id=? AND phase='INSPECTION'",
                                       ("campaign-handoff:" + action_id,)).fetchone()
            _require(reading and reading["state"] == "READING" and reading["token"] == read_token
                     and engagement.timestamp(reading["lease_until"]) > current, "CAMPAIGN_HANDOFF_LEASE_EXPIRED")
        _guard(database, config, action, proof, current, before_list=True)
        if existing:
            _require(not database.execute("SELECT 1 FROM outreach_attempts WHERE campaign_id=?", (campaign_id,)).fetchone(),
                     "CAMPAIGN_LAUNCH_ALREADY_CLAIMED")
            changed = database.execute("UPDATE outreach_queue SET payload=?,state='PENDING_VALIDATION',gates=NULL WHERE campaign_id=? AND payload=? AND state IN ('PENDING_VALIDATION','BLOCKED','HELD_INSPECTION')",
                                       (json.dumps(candidate), campaign_id, existing["payload"]))
            _require(changed.rowcount == 1, "CAMPAIGN_QUEUE_CHANGED_DURING_REFRESH")
            result = {"state": "ALREADY_QUEUED", "identity": row["identity"]}
        else:
            result = outreach.enqueue(database, candidate)
    return dict(result, campaign_id=campaign_id)


def preparation_proof(database, client, action, proof):
    """Attach only independently observed single-list/sequence binding to proof."""
    leads = _complete_listing(client.call("get_leads_from_list", {"listId": action["list_id"], "offset": 0, "limit": 100}))
    _require(len(leads) == 1 and outreach.profile_key(leads[0].get("profileUrl"))
             == outreach.profile_key(action["profile"]), "SINGLE_CONTACT_LIST_UNVERIFIED")
    registered = database.execute("SELECT campaign_id FROM outreach_prepared_campaigns WHERE action_id=?", (action["action_id"],)).fetchone()
    if registered:
        sequence = client.call("get_campaign_sequence", {"campaignId": registered[0]})
        _require(_same_sequence(sequence, action["sequence"]), "PREPARED_SEQUENCE_CHANGED")
        source = "heyreach:get_campaign_sequence:" + str(registered[0])
    else:
        intent = database.execute("SELECT preflight FROM outreach_campaign_intents WHERE action_id=? AND operation='create_empty_list'",
                                  (action["action_id"],)).fetchone()
        _require(intent is not None, "OBSERVED_SEQUENCE_MISSING")
        prior = json.loads(intent[0])
        _require(prior.get("sequence") == action["sequence"] and prior.get("sequence_source"), "OBSERVED_SEQUENCE_MISSING")
        source = prior["sequence_source"]
    return dict(proof, sequence=action["sequence"], sequence_source=source,
                single_contact_list_verified=True, list_id=action["list_id"])


def reconcile_delivery(database, config, client, campaign_id, now=None, *, read_token=None):
    """Read single-recipient campaign delivery; an ACK or running status is insufficient."""
    claim = database.execute("SELECT * FROM outreach_launch_claims WHERE campaign_id=?", (campaign_id,)).fetchone()
    _require(claim and claim["sender_id"] == config["sender_id"]
             and hashlib.sha256(claim["payload"].encode()).hexdigest() == claim["payload_hash"],
             "IMMUTABLE_LAUNCH_CLAIM_REQUIRED")
    candidate = json.loads(claim["payload"])
    owner = outreach._prepared_owner(database, candidate)
    _require(owner and owner["sender_id"] == config["sender_id"], "EXACT_PREPARED_CAMPAIGN_OWNER_REQUIRED")
    action = owner["action"]
    campaign = client.call("get_campaign", {"campaignId": campaign_id})
    sequence = client.call("get_campaign_sequence", {"campaignId": campaign_id})
    leads = client.call("get_leads_from_list", {"listId": action["list_id"], "offset": 0, "limit": 100})
    items = _complete_listing(leads)
    _require(campaign.get("id") == campaign_id and campaign.get("campaignAccountIds") == [claim["sender_id"]]
             and campaign.get("linkedInUserListId") == action["list_id"] and campaign.get("name") == action["campaign_name"]
             and _same_sequence(sequence, action["sequence"]) and len(items) == 1
             and outreach.profile_key(items[0].get("profileUrl")) == claim["identity"], "DELIVERY_SCOPE_CHANGED")
    stats = client.call("get_overall_stats", {"campaignIds": [campaign_id], "accountIds": [claim["sender_id"]]})
    totals = stats.get("overallStats", {})
    expected = {"connectionsSent": 1, "messagesSent": 0, "inmailMessagesSent": 0, "uniqueLeadsContacted": 1}
    if any(type(totals.get(field)) is not int or totals[field] != value for field, value in expected.items()):
        return {"state": "UNKNOWN", "blocker": "INDEPENDENT_SINGLE_RECIPIENT_DELIVERY_REQUIRED"}
    current = engagement.timestamp(now or dt.datetime.now(dt.timezone.utc))
    _require(engagement.timestamp(claim["claimed_at"]) <= engagement.timestamp(campaign.get("startedAt")) <= current,
             "DELIVERY_START_TIME_UNVERIFIED")
    evidence = {"campaign": campaign, "sequence": sequence, "leads": leads, "stats": stats,
                "launch_payload_hash": claim["payload_hash"], "observed_at": current.isoformat(),
                "sender_id": claim["sender_id"], "profile": candidate["profile"], "message": candidate["message"],
                "preparation_action_id": owner["action_id"], "preparation_payload_hash": candidate["preparation_payload_hash"],
                "occurred_at_basis": "CONSERVATIVE_FIRST_INDEPENDENT_DELIVERY_OBSERVATION"}
    with engagement.transaction(database):
        if read_token is not None:
            reading = database.execute("SELECT state,token,lease_until FROM outreach_read_retries WHERE action_id=? AND phase='RECONCILIATION'",
                                       ("campaign-delivery:" + str(campaign_id),)).fetchone()
            _require(reading and reading["state"] == "READING" and reading["token"] == read_token
                     and engagement.timestamp(reading["lease_until"]) > current, "DELIVERY_RECONCILIATION_LEASE_EXPIRED")
        attempt = database.execute("SELECT identity,state,claimed_at FROM outreach_attempts WHERE campaign_id=?", (campaign_id,)).fetchone()
        _require(attempt and attempt["identity"] == claim["identity"] and attempt["claimed_at"] == claim["claimed_at"],
                 "DELIVERY_ATTEMPT_BINDING_CHANGED")
        if attempt["state"] == "CONFIRMED_SENT":
            return {"state": "CONFIRMED_SENT", "campaign_id": campaign_id}
        _require(attempt["state"] in {"CLAIMED", "DISPATCHING", "ACKNOWLEDGED_DELIVERY_UNCONFIRMED", "UNCERTAIN_NO_RETRY",
                                      outreach.LAUNCH_FAILED_STATE},
                 "DELIVERY_ATTEMPT_STATE_CHANGED")
        engagement._record(database, {"source": "heyreach", "source_id": "campaign-invitation:" + str(campaign_id),
            "profile": candidate["profile"], "kind": "initial_sent", "purpose": "INITIAL", "campaign_id": campaign_id,
            "origin": {"table": "outreach_attempts", "key": claim["identity"]},
            "segment": action.get("segment", {}), "preparation_action_id": owner["action_id"],
            "preparation_payload_hash": candidate["preparation_payload_hash"], "launch_payload_hash": claim["payload_hash"],
            "occurred_at": current.isoformat(), "observed_at": current.isoformat(), "verified": True,
            "evidence": "heyreach:single-recipient-campaign-stats:" + str(campaign_id), "raw": evidence})
        database.execute("UPDATE outreach_attempts SET state='CONFIRMED_SENT',result=? WHERE campaign_id=?",
                         (_encoded(evidence), campaign_id))
        database.execute("UPDATE outreach_queue SET state='CONFIRMED_SENT' WHERE campaign_id=?", (campaign_id,))
    return {"state": "CONFIRMED_SENT", "campaign_id": campaign_id,
            "clock_basis": evidence["occurred_at_basis"], "delivery_confirmed": True}


def cycle(database, config, client, adapter, now=None):
    """Refresh prepared drafts, hand off, dispatch, then bounded delivery reads."""
    outreach.initialize(database)
    outreach.initialize_actions(database)
    initialize(database)
    current = engagement.timestamp(now or dt.datetime.now(dt.timezone.utc))
    # Ceiling 4 -> 7 (owner, 2026-09-23: "one outreach per cycle is terrible for throughput.
    # Lets do a batch of 7."). outreach_agent.py was raised to 7 at the time and these two were
    # not, so the SAME config key passed one reader and raised in the other: campaign preparation
    # and the campaign cycle both failed outright and no outreach could be prepared or sent at all.
    limit = config.get("outreach_agent", {}).get("max_items", 2)
    _require(type(limit) is int and 1 <= limit <= 7, "CAMPAIGN_CYCLE_LIMIT_INVALID")
    result = {"state": "PROCESSED", "items": [], "delivery": []}
    ready = []
    # A launch waiting for the owner (review mode) or rejected by the owner is not inspected again.
    rows = database.execute("""SELECT actions.* FROM outreach_actions actions
        JOIN outreach_prepared_campaigns prepared USING(action_id)
        WHERE actions.state='PREPARED' AND NOT EXISTS
        (SELECT 1 FROM outreach_attempts attempts WHERE attempts.campaign_id=prepared.campaign_id)
        AND NOT EXISTS (SELECT 1 FROM outreach_queue queue WHERE queue.campaign_id=prepared.campaign_id
            AND queue.state IN ('AWAITING_APPROVAL','REJECTED_BY_OWNER'))
        ORDER BY actions.action_id""").fetchall()
    inspections = 0
    for row in rows:
        if inspections >= limit:
            break
        checked = engagement.timestamp(now or dt.datetime.now(dt.timezone.utc))
        read_id = "campaign-handoff:" + row["action_id"]
        token = outreach._claim_read(database, read_id, "INSPECTION", checked)
        if token is None:
            continue
        inspections += 1
        item = {"action_id": row["action_id"], "state": "HELD"}
        result["items"].append(item)
        try:
            action = json.loads(row["payload"])
            proof = preparation_proof(database, client, action, adapter.inspect_action(action, config))
            item.update(handoff(database, config, client, row["action_id"], proof, now, read_token=token))
            completed = outreach._finish_read(database, read_id, "INSPECTION", token,
                engagement.timestamp(now or dt.datetime.now(dt.timezone.utc)), "READY")
            _require(completed, "CAMPAIGN_HANDOFF_LEASE_EXPIRED")
            ready.append(item["campaign_id"])
        except Exception as error:
            item.update(state="HELD", blocker=str(error) if isinstance(error, ValueError) else type(error).__name__)
            finished = engagement.timestamp(now or dt.datetime.now(dt.timezone.utc))
            if getattr(error, "code", None) == "OUTREACH_AI_SHARED_BUDGET_EXHAUSTED":
                with engagement.transaction(database):
                    database.execute("""UPDATE outreach_read_retries SET state='RETRY_WAIT',attempts=MAX(0,attempts-1),
                        next_at=?,last_error=?,token=NULL,lease_until=NULL,updated_at=?
                        WHERE action_id=? AND phase='INSPECTION' AND token=? AND state='READING'""",
                        (error.reset_at.isoformat(), error.code, finished.isoformat(), read_id, token))
            else:
                outcome, safe_error = outreach._read_error(error)
                outreach._finish_read(database, read_id, "INSPECTION", token, finished, outcome, safe_error)
            with engagement.transaction(database):
                database.execute("""UPDATE outreach_queue SET state='HELD_INSPECTION',gates=?
                    WHERE campaign_id IN (SELECT campaign_id FROM outreach_prepared_campaigns WHERE action_id=?)
                    AND state IN ('PENDING_VALIDATION','BLOCKED','HELD_INSPECTION')""",
                    (_encoded([item["blocker"]]), row["action_id"]))
    result["dispatch"] = outreach.dispatch(database, config,
        config.get("pipeline", {"companies": [], "rules": {"max_unique_contacts_per_company": 3}}), client, campaign_ids=ready)
    # Failed launches that are due, read on HeyReach first (owner, 2026-09-30).
    result["launch_retries"] = outreach.retry_launches(database, config, client, now, limit=limit)
    attempts = database.execute("""SELECT attempts.campaign_id FROM outreach_attempts attempts
        JOIN outreach_launch_claims claims USING(campaign_id)
        WHERE attempts.state IN ('CLAIMED','DISPATCHING','ACKNOWLEDGED_DELIVERY_UNCONFIRMED','UNCERTAIN_NO_RETRY','LAUNCH_FAILED')
        ORDER BY attempts.claimed_at""").fetchall()
    reconciliations = 0
    for attempt in attempts:
        if reconciliations >= limit:
            break
        campaign_id = attempt["campaign_id"]
        read_id = "campaign-delivery:" + str(campaign_id)
        checked = engagement.timestamp(now or dt.datetime.now(dt.timezone.utc))
        token = outreach._claim_read(database, read_id, "RECONCILIATION", checked)
        if token is None:
            continue
        reconciliations += 1
        try:
            observation = reconcile_delivery(database, config, client, campaign_id, now, read_token=token)
            outcome = "COMPLETE" if observation["state"] == "CONFIRMED_SENT" else "RETRY_WAIT"
            error = None if outcome == "COMPLETE" else "INDEPENDENT_DELIVERY_PENDING"
        except Exception as failure:
            outcome, error = outreach._read_error(failure)
            observation = {"campaign_id": campaign_id, "state": "UNKNOWN", "blocker": error}
        finished = engagement.timestamp(now or dt.datetime.now(dt.timezone.utc))
        outreach._finish_read(database, read_id, "RECONCILIATION", token, finished, outcome, error)
        result["delivery"].append(observation)
    return result
