"""Durable normalized LinkedIn events; no scheduler or implicit provider access.

Read adapters implement read_page(source, cursor) -> {events, next_cursor, done}.
Events require source, source_id, profile, kind, occurred_at, observed_at,
evidence and raw. Optional inference is stored separately from raw observations.
The adapter owns provider response normalization; unknown response schemas must
raise NotImplementedError. Cursors must be stable opaque strings, or None.
"""

import datetime as dt
import contextlib
import copy
import hashlib
import json
import re
from zoneinfo import ZoneInfo

import linkedin_port
import workflow_tasks
from orchestrator import name_key, profile_key


KINDS = {"inbound", "accepted", "initial_sent", "followup_sent", "outbound_observed", "opt_out", "interview"}


@contextlib.contextmanager
def transaction(database):
    nested = database.in_transaction
    database.execute("SAVEPOINT engagement_event" if nested else "BEGIN IMMEDIATE")
    try:
        yield
        if nested:
            database.execute("RELEASE SAVEPOINT engagement_event")
        else:
            database.commit()
    except BaseException:
        if nested:
            database.execute("ROLLBACK TO SAVEPOINT engagement_event")
            database.execute("RELEASE SAVEPOINT engagement_event")
        else:
            database.rollback()
        raise


def timestamp(value):
    parsed = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Timezone-aware timestamp required")
    return parsed.astimezone(dt.timezone.utc)


# HeyReach gives no per-message id, and one message's createdAt moves by seconds between reads
# (one contact's reply: 23:41:51.466 in the thread read and our receipt, then 23:41:54.548 and
# 23:41:54.342 at two later reads). Two observations of the same conversation, direction and exact
# text within this window are one message.
MESSAGE_TIME_DRIFT = dt.timedelta(minutes=10)


def same_instant(first, second):
    return abs(timestamp(first) - timestamp(second)) <= MESSAGE_TIME_DRIFT


def drift_alias(database, event):
    """The stored event this synthetic HeyReach observation is a drifted re-read of, or None.

    Same source, person, kind, conversation, sender and exact text, within MESSAGE_TIME_DRIFT,
    under another synthetic id. One real message therefore never becomes a second inbound event
    (a second reply slot and review) or a second outbound observation."""
    raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
    message = raw.get("message") if isinstance(raw.get("message"), dict) else None
    if event.get("source_id_kind") != "synthetic_content_sha256_v1" or message is None:
        return None
    for source_id, occurred in database.execute(
            """SELECT source_id, occurred_at FROM engagement_events WHERE source=? AND identity=? AND kind=?
            AND source_id<>? AND json_extract(raw,'$.conversation_id')=? AND json_extract(raw,'$.message.sender')=?
            AND json_extract(raw,'$.message.body')=?""",
            (event["source"], profile_key(event["profile"]), event["kind"], event["source_id"],
             raw.get("conversation_id"), message.get("sender"), message.get("body"))):
        if same_instant(occurred, event["occurred_at"]):
            return source_id
    return None


def initialize(database):
    workflow_tasks.initialize(database)
    database.executescript("""
        CREATE TABLE IF NOT EXISTS engagement_events (
            source TEXT NOT NULL, source_id TEXT NOT NULL, identity TEXT NOT NULL,
            kind TEXT NOT NULL, occurred_at TEXT NOT NULL, observed_at TEXT NOT NULL,
            raw TEXT NOT NULL, inference TEXT, payload TEXT NOT NULL,
            PRIMARY KEY(source, source_id));
        CREATE INDEX IF NOT EXISTS engagement_identity ON engagement_events(identity);
        CREATE INDEX IF NOT EXISTS engagement_occurred_at ON engagement_events(occurred_at);
        -- E6.T5: the Networking & Referrals page summarizes each person's events by kind (covering).
        CREATE INDEX IF NOT EXISTS engagement_kind_identity ON engagement_events(kind, identity, occurred_at);
        CREATE TABLE IF NOT EXISTS engagement_cursors (
            source TEXT PRIMARY KEY, cursor TEXT, generation INTEGER NOT NULL DEFAULT 0,
            checked_at TEXT, state TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS engagement_stops (
            identity TEXT PRIMARY KEY, state TEXT NOT NULL, source TEXT NOT NULL,
            source_id TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS engagement_decode_holds (
            source TEXT NOT NULL, conversation_id TEXT NOT NULL, identity TEXT,
            error_type TEXT NOT NULL, reason TEXT NOT NULL, first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL, failures INTEGER NOT NULL,
            PRIMARY KEY(source, conversation_id));
        CREATE TABLE IF NOT EXISTS engagement_chatroom_failures (
            source TEXT NOT NULL, conversation_id TEXT NOT NULL, identity TEXT,
            consecutive INTEGER NOT NULL, error_type TEXT NOT NULL, error TEXT NOT NULL,
            last_at TEXT NOT NULL, PRIMARY KEY(source, conversation_id));
        CREATE TABLE IF NOT EXISTS engagement_deep_page_failures (
            source TEXT PRIMARY KEY, cursor TEXT NOT NULL, consecutive INTEGER NOT NULL,
            error_type TEXT NOT NULL, error TEXT NOT NULL, last_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS engagement_skipped_pages (
            source TEXT NOT NULL, cursor TEXT NOT NULL, next_cursor TEXT, failures INTEGER NOT NULL,
            error_type TEXT NOT NULL, error TEXT NOT NULL, skipped_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS engagement_closures (
            identity TEXT PRIMARY KEY, closed_at TEXT NOT NULL, reason TEXT NOT NULL,
            source TEXT NOT NULL);
    """)


def _record(database, event):
    for field in ("source", "source_id", "evidence"):
        if not isinstance(event.get(field), str) or not event[field].strip():
            raise ValueError("Missing event " + field)
    if event.get("kind") not in KINDS or "raw" not in event:
        raise ValueError("Unsupported event or missing raw observation")
    identity = profile_key(event["profile"])
    occurred = timestamp(event["occurred_at"])
    observed = timestamp(event["observed_at"])
    if occurred > observed and event["kind"] != "interview":
        raise ValueError("Event occurs after observation")
    if event["kind"] in {"initial_sent", "followup_sent", "accepted"} and event.get("verified") is not True:
        raise ValueError("Delivery/acceptance requires independent evidence, not acknowledgement")
    inference = event.get("inference")
    if inference is not None and (not isinstance(inference, dict) or not inference.get("method")
                                  or inference.get("sentiment") not in {"positive", "neutral", "negative", "unknown", "ambiguous", "opt_out"}):
        raise ValueError("Sentiment inference requires method and explicit sentiment")
    payload = json.dumps(event, sort_keys=True)
    old = database.execute("SELECT payload FROM engagement_events WHERE source=? AND source_id=?",
                           (event["source"], event["source_id"])).fetchone()
    if not old and drift_alias(database, event):
        return False
    if old:
        previous = json.loads(old[0])
        comparison = copy.deepcopy(event)
        # A re-read differs only in when it was observed: the event's own observed_at and its
        # inbox envelope's authentication time (the Unipile reader and closure read build the same
        # event on every read; the HeyReach decoder replays the stored one instead).
        for value in (previous, comparison):
            value.pop("observed_at", None)
            inbox = value.get("inbox")
            if isinstance(inbox, dict) and isinstance(inbox.get("authentication"), dict):
                inbox["authentication"].pop("observed_at", None)
        if previous != comparison:
            raise ValueError("Source event identity reused with different content")
        return False
    database.execute("INSERT INTO engagement_events VALUES (?,?,?,?,?,?,?,?,?)",
                     (event["source"], event["source_id"], identity, event["kind"], occurred.isoformat(),
                      observed.isoformat(), json.dumps(event["raw"]), json.dumps(inference), payload))
    if event["kind"] in {"inbound", "opt_out"}:
        negative = event["kind"] == "opt_out" or (inference or {}).get("sentiment") in {"negative", "opt_out"}
        state = "OPTED_OUT" if negative else "REPLY_REQUIRES_CONTEXT"
        database.execute("""INSERT INTO engagement_stops VALUES (?,?,?,?)
            ON CONFLICT(identity) DO UPDATE SET
            state=CASE WHEN engagement_stops.state='OPTED_OUT' THEN 'OPTED_OUT' ELSE excluded.state END,
            source=excluded.source, source_id=excluded.source_id""",
                         (identity, state, event["source"], event["source_id"]))
        if database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='suppressions'").fetchone():
            database.execute("INSERT OR IGNORE INTO suppressions VALUES (?,?)",
                             (identity, "LinkedIn reply stops blind outreach: " + state))
        workflow_tasks.enqueue(database, "engagement_review", event["source"] + ":" + event["source_id"],
            "engagement:" + event["source"], [{"source": event["evidence"], "observed_at": event["observed_at"],
                "source_id": event["source_id"], "identity": identity, "stop_state": state}],
            (observed + dt.timedelta(days=1)).isoformat(), observed.isoformat(), contact_id=identity)
    return True


def record_event(database, event):
    """Record one immutable source event and stop decision in one transaction."""
    with transaction(database):
        return {"inserted": _record(database, event), "identity": profile_key(event["profile"])}


def _completed_review_proof(database, owner, stored, event, now):
    import pipeline_routing as routing
    import reply_worker

    saved = routing.one(database, "SELECT * FROM reply_worker_inputs WHERE task_id=?", (owner["task_id"],))
    result = routing.one(database, "SELECT * FROM reply_worker_results WHERE task_id=?", (owner["task_id"],))
    if not saved or not result or result["output_version"] != reply_worker.VERSION:
        raise ValueError("ENGAGEMENT_COMPLETED_RESULT_MISSING")
    handoff = json.loads(result["handoff"])
    template, separator, request_json = saved["input"].partition("\n\nUNTRUSTED_SOURCE_JSON:\n")
    request = json.loads(request_json)
    provenance = json.loads(saved["provenance"])
    if (not separator or handoff["task_id"] != owner["task_id"]
            or handoff["source_key"] != stored["source_key"]
            or not saved["source_sha256"] == handoff["source_sha256"] == stored["content_sha256"]
            or not saved["input_sha256"] == result["input_sha256"] == handoff["input_sha256"] == reply_worker.sha(saved["input"])
            or result["output_sha256"] != handoff["output_sha256"]
            or handoff["model_provenance"] != provenance or provenance["prompt_sha256"] != reply_worker.sha(template)
            or request["source_snapshot"] != event or request["raw_message"] != json.loads(event["raw"])
            or request["source_key"] != stored["source_key"] or request["source_sha256"] != stored["content_sha256"]):
        raise ValueError("ENGAGEMENT_COMPLETED_RESULT_UNVERIFIED")
    attempts = routing.rows(database, """SELECT * FROM reply_worker_attempts
        WHERE task_id=? AND input_sha256=? AND output_sha256=?""",
        (owner["task_id"], result["input_sha256"], result["output_sha256"]))
    attempts = [attempt for attempt in attempts if attempt["output"] is not None
                and reply_worker.sha(attempt["output"]) == result["output_sha256"]
                and json.loads(attempt["provenance"]) == provenance
                and timestamp(attempt["observed_at"]) <= timestamp(now)]
    if len(attempts) != 1:
        raise ValueError("ENGAGEMENT_COMPLETED_ATTEMPT_UNVERIFIED")
    inbox = json.loads(event["payload"])["inbox"]
    holds = []
    for flag in ("quoted", "forwarded"):
        if type(inbox[flag]) is not bool:
            holds.append("UNKNOWN_" + flag.upper() + "_CONTEXT")
        elif inbox[flag]:
            holds.append(flag.upper() + "_CONTEXT_REQUIRES_ADJUDICATION")
    if request.get("context_issues"):
        holds.append("CONFLICTING_CONVERSATION_CONTEXT")
    try:
        checked = reply_worker._validate(attempts[0]["output"], request, inbox["text"], holds)
    except reply_worker.Held as error:
        raise ValueError("ENGAGEMENT_COMPLETED_OUTPUT_UNVERIFIED") from error
    checked.update({field: handoff[field] for field in
                    ("task_id", "input_sha256", "output_sha256", "source_key", "source_sha256", "model_provenance")})
    if checked != handoff or handoff.get("can_send") is not False:
        raise ValueError("ENGAGEMENT_COMPLETED_OUTPUT_UNVERIFIED")
    return {"reply_result_source": "reply_worker_results:" + owner["task_id"],
            "reply_output_sha256": result["output_sha256"], "reply_state_at_handoff": handoff["state"],
            "classification_label": handoff["classification"]["label"], "classification_holds": handoff["holds"]}


def _conversion_review_proof(database, stored, event):
    import pipeline_routing as routing

    result = json.loads(stored["result"] or "{}")
    if not result.get("conversion_event_id"):
        # Finished without a per-job record (owner, 2026-09-24/25): the reply AI closed it, or it
        # was a referral, which is now recorded on the person. This required a per-job
        # conversion event for every finished message, so those hand-offs were never retired.
        if result.get("referral_state"):
            import referrals
            referral = referrals.current(database, event["identity"])
            if (not referral or result.get("contact_id") != event["identity"]
                    or not any(entry["source_key"] == stored["source_key"] and entry["state"] == result["referral_state"]
                               for entry in referral["history"])):
                raise ValueError("ENGAGEMENT_REFERRAL_OWNER_UNVERIFIED")
        # "backlog": a message from before the reply cutoff; "cold_inbound": a sender the harness
        # does not track; "no_reply_basis": a tracked sender it may not answer. All finished with
        # no model call (owner, 2026-10-01; reply_worker.close_skipped).
        elif result.get("review_closed_by") not in {"model", "backlog", "cold_inbound", "no_reply_basis", "answered"}:
            raise ValueError("ENGAGEMENT_CONVERSION_OWNER_MISSING")
        return {"owner_task_id": None, "conversion_event_id": None, "routed_by": "pipeline_routing_sources:" + stored["source_key"],
                "referral_state_recorded": result.get("referral_state")}
    identifier = "routing-inbox:" + stored["source_key"]
    conversion_event = routing.one(database, "SELECT * FROM conversion_events WHERE event_id=?", (identifier,))
    evidence = routing.one(database, "SELECT * FROM conversion_evidence WHERE evidence_id=?", (identifier,))
    if not conversion_event or not evidence:
        raise ValueError("ENGAGEMENT_CONVERSION_OWNER_MISSING")
    record = json.loads(evidence["record"])
    facts = record["facts"]
    inbox = json.loads(event["payload"])["inbox"]
    job = routing.one(database, "SELECT * FROM application_jobs WHERE job_key=?", (result["job_key"],))
    if (not job or result["conversion_event_id"] != identifier or result["evidence_id"] != identifier
            or conversion_event["evidence_id"] != identifier or record["evidence_id"] != identifier
            or not result["contact_id"] == conversion_event["contact_id"] == facts["contact_id"] == event["identity"]
            or conversion_event["job_key"] != result["job_key"]
            or facts["employer"] != job["employer"] or facts["requisition"] != job["requisition"]
            or result["referral_state"] not in {"AGREED", "DECLINED"}
            or facts["state"] != result["referral_state"]
            or not conversion_event["kind"] == record["kind"] == "REFERRAL_" + result["referral_state"]
            or record["source_type"] != "contact_message"
            or record["source_sha256"] != stored["content_sha256"]
            or record["source_locator"] != "engagement_events:" + stored["source_id"]
            or record["source_ref"] != inbox["authentication"]["source"]
            or timestamp(record["occurred_at"]) != timestamp(event["occurred_at"])
            or timestamp(record["observed_at"]) != timestamp(event["observed_at"])
            or facts["routing_source_key"] != stored["source_key"]
            or facts["provider_source"] != event["source"] or facts["provider_source_id"] != event["source_id"]
            or facts["raw_message"] != json.loads(event["raw"]) or facts["authenticated_identity"] != inbox
            or facts["statement"] != inbox["text"]):
        raise ValueError("ENGAGEMENT_CONVERSION_OWNER_UNVERIFIED")
    return {"owner_task_id": None, "conversion_event_id": identifier, "conversion_evidence_id": identifier,
            "conversion_status_at_handoff": conversion_event["status"],
            "conversion_error_at_handoff": conversion_event["error"], "referral_state_recorded": facts["state"]}


def _review_owner(database, task, config, now):
    import pipeline_routing as routing
    import reply_worker

    inputs = json.loads(task["inputs"])
    if len(inputs) != 1 or task["effect"] != "READ":
        raise ValueError("ENGAGEMENT_REVIEW_BINDING_INVALID")
    bound = inputs[0]
    events = routing.rows(database, "SELECT * FROM engagement_events WHERE source_id=? AND identity=?",
                          (bound["source_id"], task["contact_id"]))
    events = [event for event in events if task["action_id"] == event["source"] + ":" + event["source_id"]]
    if len(events) != 1:
        raise ValueError("ENGAGEMENT_REVIEW_SOURCE_MISSING_OR_AMBIGUOUS")
    event = events[0]
    payload = json.loads(event["payload"])
    if (task["task_id"] != routing.digest(["engagement_review", task["action_id"]])
            or task["parent_run_id"] != "engagement:" + event["source"]
            or bound["identity"] != event["identity"] or bound["source"] != payload["evidence"]
            or bound["observed_at"] != event["observed_at"]):
        raise ValueError("ENGAGEMENT_REVIEW_BINDING_INVALID")
    try:
        routing._inbox(event, config.get("pipeline_routing", {}), now)
    except ValueError as error:
        if str(error) not in reply_worker.REASONS:
            raise
    if not isinstance(payload["inbox"]["text"], str) or not payload["inbox"]["text"].strip():
        raise ValueError("ENGAGEMENT_ACTUAL_MESSAGE_REQUIRED")
    source_id = routing.encoded([event["source"], event["source_id"]])
    source_key = routing.digest([routing.VERSION, "engagement_events", source_id, "message"])
    stored = routing.one(database, "SELECT * FROM pipeline_routing_sources WHERE source_key=?", (source_key,))
    if (not stored or stored["source_table"] != "engagement_events" or stored["source_id"] != source_id
            or json.loads(stored["snapshot"]) != event or stored["content_sha256"] != routing.digest(event)
            or routing.one(database, "SELECT 1 FROM pipeline_routing_conflicts WHERE source_key=?", (source_key,))):
        raise ValueError("ENGAGEMENT_ROUTING_SOURCE_UNVERIFIED")
    proof = {"source": "pipeline_routing_sources:" + source_key, "observed_at": now,
             "source_key": source_key, "source_sha256": stored["content_sha256"],
             "provider_source": event["source"], "message_id": event["source_id"],
             "account_id": payload["inbox"]["account_id"], "conversation_id": payload["inbox"]["conversation_id"],
             "routing_status_at_handoff": stored["status"], "routing_blocker_at_handoff": stored["blocker"],
             "classification_resolved": False, "can_send": False, "bookkeeping_only": True}
    if stored["status"] == "ROUTED":
        return {**proof, **_conversion_review_proof(database, stored, event)}
    result = json.loads(stored["result"] or "{}")
    owner_ids = list(dict.fromkeys([result[field] for field in
        ("reply_context_task_id", "reply_upgrade_task_id", "review_task_id") if result.get(field)]))
    if not owner_ids:
        owner_ids = list((result.get("reply_decisions") or {}).keys())[-1:]
    cancelled = []
    owner_id = None
    for candidate in owner_ids:
        candidate_task = routing.one(database, "SELECT * FROM workflow_tasks WHERE task_id=?", (candidate,))
        if candidate_task and candidate_task["status"] == "CANCELLED":
            cancelled.append(candidate)
            continue
        owner_id = candidate
        break
    owner = routing.one(database, "SELECT * FROM workflow_tasks WHERE task_id=?", (owner_id,))
    if (not owner or owner["worker"] != "pipeline_routing_review" or owner["effect"] != "READ"
            or owner["status"] not in {"QUEUED", "RUNNING", "BLOCKED", "RETRY_WAIT", "EXHAUSTED", "COMPLETE"}
            or owner["parent_run_id"] != "pipeline-routing:" + source_key):
        raise ValueError("ENGAGEMENT_ROUTING_OWNER_UNVERIFIED")
    owner_inputs = json.loads(owner["inputs"])
    if len(owner_inputs) != 1:
        raise ValueError("ENGAGEMENT_ROUTING_OWNER_UNVERIFIED")
    owner_bound = owner_inputs[0]
    if cancelled and not any(owner_bound.get(field) in cancelled for field in
                             ("context_revision_task_id", "upgrade_from_task_id", "reply_decision_task_id")):
        raise ValueError("ENGAGEMENT_CANCELLED_REPLACEMENT_UNVERIFIED")
    if (owner_bound["source_key"] != source_key or owner_bound["content_sha256"] != stored["content_sha256"]
            or owner_bound["source"] != "engagement_events:" + source_id
            or owner_bound["observed_at"] != stored["first_seen_at"]
            or owner["action_id"] != routing.reply_review_action(database, stored, owner_bound)
            or owner["task_id"] != routing.digest([owner["worker"], owner["action_id"]])):
        raise ValueError("ENGAGEMENT_ROUTING_OWNER_UNVERIFIED")
    completed = _completed_review_proof(database, owner, stored, event, now) if owner["status"] == "COMPLETE" else {}
    consumed_hash = (result.get("reply_decisions") or {}).get(owner["task_id"])
    if consumed_hash is not None and completed and consumed_hash != completed["reply_output_sha256"]:
        raise ValueError("ENGAGEMENT_COMPLETED_CONSUMPTION_MISMATCH")
    return {**proof, **completed,
            "owner_task_id": owner["task_id"], "owner_status_at_handoff": owner["status"],
            "owner_blocker_at_handoff": owner["blocker"], "replaced_cancelled_task_ids": cancelled}


def handoff_reviews(database, config, now=None):
    """Retire duplicate review bookkeeping only after exact persisted ownership proof.

    Call after routing with its normalized pipeline_routing.account_ids config.
    Downstream tasks, source facts and delivery records are never modified.
    """
    import pipeline_routing as routing
    import sqlite3

    now = timestamp(now or dt.datetime.now(dt.timezone.utc)).isoformat()
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    required = {"workflow_tasks", "workflow_task_events", "engagement_events",
                "pipeline_routing_sources", "pipeline_routing_conflicts"}
    if not required <= tables:
        return {"state": "BLOCKED", "blocker": "ENGAGEMENT_HANDOFF_TABLES_MISSING", "items": []}
    items = []
    with transaction(database):
        tasks = routing.rows(database, """SELECT * FROM workflow_tasks WHERE worker='engagement_review'
            AND status IN ('QUEUED','BLOCKED','RETRY_WAIT') AND token_hash IS NULL AND lease_until IS NULL
            ORDER BY task_id LIMIT 100""")
        for task in tasks:
            try:
                evidence = _review_owner(database, task, config, now)
            except (ValueError, KeyError, TypeError, AttributeError, IndexError, sqlite3.OperationalError):
                items.append({"task_id": task["task_id"], "state": "RETAINED",
                              "blocker": "EXACT_DOWNSTREAM_OWNERSHIP_UNVERIFIED"})
                continue
            owner_reference = evidence.get("owner_task_id") or evidence.get("conversion_event_id") or evidence["routed_by"]
            next_action = "Follow downstream owner " + owner_reference + "; classification and delivery remain separate"
            database.execute("""UPDATE workflow_tasks SET status='CANCELLED',blocker='DUPLICATE_REVIEW_HANDED_OFF',
                next_action=?,next_at=NULL,updated_at=? WHERE task_id=?""", (next_action, now, task["task_id"]))
            workflow_tasks.record(database, task["task_id"], "ENGAGEMENT_REVIEW_HANDED_OFF", now,
                                  dict(evidence, previous_status=task["status"], previous_blocker=task["blocker"],
                                       previous_next_action=task["next_action"], previous_next_at=task["next_at"]))
            items.append({"task_id": task["task_id"], "state": "HANDED_OFF", **evidence})
    return {"state": "COMPLETE", "items": items}


def identity_keys(database, profile):
    identity = profile_key(profile if "://" in profile else "https://" + profile)
    identities = {identity}
    if database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='rw_aliases'").fetchone():
        aliases = [tuple(profile_key(value if "://" in value else "https://" + value) for value in row) for row in database.execute(
            "SELECT alias,canonical FROM rw_aliases WHERE kind='contact'")]
        while True:
            previous = set(identities)
            for alias, canonical in aliases:
                if alias in identities or canonical in identities:
                    identities.update((alias, canonical))
            if previous == identities:
                break
    return sorted(identities)


def closure_status(database, profile):
    """Read the permanent lifecycle decision independently of delivery facts."""
    if not database.execute("SELECT 1 FROM sqlite_master WHERE name='engagement_closures' AND type='table'").fetchone():
        return None
    identities = identity_keys(database, profile)
    placeholders = ",".join("?" for unused in identities)
    row = database.execute("SELECT identity,closed_at,reason,source FROM engagement_closures WHERE identity IN ("
                           + placeholders + ") ORDER BY closed_at,identity LIMIT 1", identities).fetchone()
    return {"state": "CLOSED", "identity": row[0], "closed_at": row[1], "reason": row[2], "source": row[3]} if row else None


def close_outreach(database, profile, reason, now=None, *, source="explicit"):
    """Explicit terminal closure; never withdraw, unsend or reconcile delivery.

    Initialize engagement storage before calling. Repeated closure keeps the
    original reason/time. Alias links must be verified in rw_aliases.
    """
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("Explicit outreach closure reason required")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("Explicit outreach closure source required")
    closed_at = timestamp(now or dt.datetime.now(dt.timezone.utc)).isoformat()
    with transaction(database):
        database.execute("CREATE TABLE IF NOT EXISTS suppressions (identity TEXT PRIMARY KEY, reason TEXT NOT NULL)")
        identities = identity_keys(database, profile)
        previous = closure_status(database, profile)
        if previous:
            closed_at, reason, source = previous["closed_at"], previous["reason"], previous["source"]
        placeholders = ",".join("?" for unused in identities)
        tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for identity in identities:
            database.execute("INSERT OR IGNORE INTO engagement_closures VALUES (?,?,?,?)", (identity, closed_at, reason, source))
            database.execute("INSERT OR IGNORE INTO suppressions VALUES (?,?)", (identity, "Outreach permanently closed: " + reason))
        if "outreach_actions" in tables:
            # A message waiting for the owner's approval (review mode) is closed too, with the reason,
            # so it leaves Approvals and Needs you (review fix P4).
            database.execute("UPDATE outreach_actions SET state='CLOSED',result=? WHERE identity IN (" + placeholders
                             + ") AND state='AWAITING_APPROVAL'",
                             (json.dumps({"closed_while_awaiting_approval": {"reason": "OUTREACH_CLOSED: " + reason,
                                                                            "closed_at": closed_at}}), *identities))
        for table, states in (("drafts", "'HELD_VERIFICATION','HELD_HISTORY_AND_USER_REVIEW'"),
                              ("outreach_queue", "'PENDING_VALIDATION','BLOCKED','AWAITING_APPROVAL'"),
                              ("outreach_actions", "'PENDING','BLOCKED'")):
            if table in tables:
                database.execute("UPDATE " + table + " SET state='CLOSED' WHERE identity IN (" + placeholders
                                 + ") AND state IN (" + states + ")", identities)
        tasks = database.execute("SELECT task_id FROM workflow_tasks WHERE worker='message_writer' AND contact_id IN ("
                                 + placeholders + ") AND status IN ('QUEUED','BLOCKED','RETRY_WAIT')", identities).fetchall()
        for task in tasks:
            database.execute("UPDATE workflow_tasks SET status='CANCELLED',blocker='OUTREACH_CLOSED',next_action=NULL,updated_at=? WHERE task_id=?",
                             (closed_at, task[0]))
            workflow_tasks.record(database, task[0], "CANCELLED", closed_at, {"reason": reason, "lifecycle": "CLOSED"})
        return closure_status(database, profile)


def stop_state(database, profile):
    if closure_status(database, profile):
        return "OUTREACH_CLOSED"
    identities = identity_keys(database, profile)
    placeholders = ",".join("?" for unused in identities)
    states = {row[0] for row in database.execute(
        "SELECT state FROM engagement_stops WHERE identity IN (" + placeholders + ")", identities)}
    return "OPTED_OUT" if "OPTED_OUT" in states else "REPLY_REQUIRES_CONTEXT" if states else None


def stop_label(database, profile):
    """stop_state, but naming an undecodable-conversation hold for what it is.

    Gates that let a reply through REPLY_REQUIRES_CONTEXT keep calling stop_state; this label is
    for holds shown to the owner (follow-up plans, outbound gates), so a person held only because
    their conversation could not be read is never reported as someone who replied."""
    stopped = stop_state(database, profile)
    if stopped == "REPLY_REQUIRES_CONTEXT":
        identities = identity_keys(database, profile)
        placeholders = ",".join("?" for unused in identities)
        states = {row[0] for row in database.execute(
            "SELECT state FROM engagement_stops WHERE identity IN (" + placeholders + ")", identities)}
        if states == {UNDECODABLE_STOP}:
            return "INBOX_CONVERSATION_UNDECODABLE"
    return stopped


def reconcile_observed_outbound(database, config, now):
    """Mark observed messages of ours that are one of our own recorded sends.

    The inbox reader records every message from our side as outbound_observed, including the
    invitation or message we already recorded as initial_sent/followup_sent. An outbound
    observation blocks follow-ups until its purpose is known, so each one is matched here, with
    the follow-up inspection's own rules (followup_execution._sources/_matches: same text, same
    conversation or the invitation, the recorded time or delivery interval), and a match is
    written to engagement_outbound_reconciliations. The event itself stays immutable. A message
    that matches none of our recorded sends (for example the owner writing by hand), or matches
    ambiguously, is left unreconciled and keeps blocking. Never raises; returns the count.
    """
    try:
        return _reconcile_observed_outbound(database, config, now)
    except Exception as error:
        if database.in_transaction:
            database.rollback()
        return {"state": "FAILED", "error_type": type(error).__name__, "error": diagnostic(error)}


def _reconcile_observed_outbound(database, config, now):
    import followup_execution

    database.execute("""CREATE TABLE IF NOT EXISTS engagement_outbound_reconciliations (
        source TEXT NOT NULL, source_id TEXT NOT NULL, matched_source TEXT NOT NULL,
        reconciled_at TEXT NOT NULL, PRIMARY KEY(source, source_id))""")
    database.commit()
    pending, failures, skipped = {}, [], []
    for row in database.execute("""SELECT event.identity, event.source, event.source_id, event.payload
            FROM engagement_events event WHERE event.kind='outbound_observed' AND NOT EXISTS (
            SELECT 1 FROM engagement_outbound_reconciliations done
            WHERE done.source=event.source AND done.source_id=event.source_id)""").fetchall():
        try:
            event = json.loads(row[3])
            message = (event.get("raw") or {}).get("message")
            if isinstance(message, dict) and isinstance(message.get("body"), str) and message.get("createdAt"):
                timestamp(message["createdAt"])
                pending.setdefault(row[0], []).append(event)
        except Exception as error:
            failures.append({"source_id": row[2], "error_type": type(error).__name__, "error": diagnostic(error)})
    reconciled = 0
    for identity, events in pending.items():
        try:
            try:
                sources = followup_execution._sources(database, config, "https://" + identity, timestamp(now))
            except Exception as error:
                # No reconcilable first message/follow-up (e.g. FOLLOWUP_INITIAL_HISTORY_UNRESOLVED);
                # our replies may still match. The reason is reported, not swallowed.
                sources = []
                skipped.append({"identity": identity,
                                 "error_type": type(error).__name__, "error": diagnostic(error)})
            sources += _reply_sources(database, identity)
            everything = [event for event in events_for(database, identity) if event["kind"] == "outbound_observed"
                          and isinstance((event.get("raw") or {}).get("message"), dict)]
            matches = []
            for event in events:
                found = [source for source in sources if followup_execution._matches(
                    source, event["raw"]["message"], event["raw"].get("conversation_id"))]
                # One recorded send is one message. A source that matches observations of two
                # different messages is ambiguous and reconciles neither; drifted re-reads of the
                # same message (same conversation and text within MESSAGE_TIME_DRIFT) are one.
                if len(found) == 1:
                    others = [other for other in everything if followup_execution._matches(
                        found[0], other["raw"]["message"], other["raw"].get("conversation_id"))]
                    if all(_same_observed_message(other, event) for other in others):
                        matches.append((event, found[0]))
            with transaction(database):
                for event, source in matches:
                    database.execute("INSERT OR IGNORE INTO engagement_outbound_reconciliations VALUES (?,?,?,?)",
                                     (event["source"], event["source_id"], source["source"], timestamp(now).isoformat()))
            reconciled += len(matches)
        except Exception as error:
            if database.in_transaction:
                database.rollback()
            failures.append({"identity": identity, "error_type": type(error).__name__, "error": diagnostic(error)})
    return {"state": "PROCESSED" if not failures else "PARTIAL", "reconciled": reconciled, "failures": failures[:20],
            "without_followup_sources": skipped[:20]}


def _same_observed_message(first, second):
    one, two = first["raw"], second["raw"]
    return (one.get("conversation_id") == two.get("conversation_id")
            and one["message"].get("sender") == two["message"].get("sender")
            and one["message"].get("body") == two["message"].get("body")
            and same_instant(one["message"]["createdAt"], two["message"]["createdAt"]))


def _reply_sources(database, identity):
    """Our own sent replies as reconciliation sources: the claim-bound CONFIRMED_SENT REPLY action
    and its independent receipt (text from the action, conversation and time from the receipt).
    The receipt event itself still blocks follow-ups exactly as before; this only keeps the inbox
    reader's second observation of the same reply from looking like a hand-written message."""
    if not database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='outreach_actions'").fetchone():
        return []
    sources = []
    for event in events_for(database, identity):
        receipt = event.get("raw") or {}
        if (event["kind"] != "outbound_observed" or event.get("purpose") != "REPLY"
                or receipt.get("outcome") != "SENT" or receipt.get("independent") is not True):
            continue
        action = database.execute("SELECT payload FROM outreach_actions WHERE action_id=? AND state='CONFIRMED_SENT'",
                                  (receipt.get("action_id"),)).fetchone()
        if not action:
            continue
        payload = json.loads(action[0])
        # The receipt event is ours (recorded when the send was confirmed, first time or on a
        # retry, whose saved result differs from the receipt); the text comes from the action.
        if (payload.get("purpose") == "REPLY" and isinstance(payload.get("message"), str)
                and payload.get("conversation_id") == receipt.get("conversation_id")
                and profile_key(payload.get("profile", "")) in identity_keys(database, identity)):
            sources.append({"source": "engagement_events:" + json.dumps([event["source"], event["source_id"]]),
                            "text": payload["message"], "conversation_id": receipt["conversation_id"],
                            "invitation": False, "occurred_at": receipt["occurred_at"]})
    return sources


def unreconciled_outbound(database, events):
    """The outbound_observed events that are not one of our own recorded sends."""
    observed = [event for event in events if event["kind"] == "outbound_observed"]
    if not observed or not database.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='engagement_outbound_reconciliations'").fetchone():
        return observed
    return [event for event in observed if not database.execute(
        "SELECT 1 FROM engagement_outbound_reconciliations WHERE source=? AND source_id=?",
        (event["source"], event["source_id"])).fetchone()]


# A conversation the inbox reader could not decode may hold a reply we have not seen, so it must
# never read as "no reply" (owner, 2026-09-30, "Inbox reading" in AGENTS.md). Its correspondent
# gets an engagement stop in this state until the conversation decodes; stop_state reports it as
# REPLY_REQUIRES_CONTEXT like any recorded reply, so first messages, follow-ups and campaign work
# hold exactly as they do for a reply, while replies to recorded messages are unaffected.
UNDECODABLE_STOP = "INBOX_UNDECODABLE"


def diagnostic(error):
    """Short, payload-free description of a failure for the run report.

    Decoder messages are constant strings; anything URL-shaped is blanked (a transport error
    must never carry the credential-bearing provider URL) and the text is bounded.
    """
    return re.sub(r"\S*://\S*", "<url>", " ".join(str(error).split()))[:160]


def _hold_undecodable(database, source, failure, now):
    conversation = failure.get("conversation_id")
    if not isinstance(conversation, str) or not conversation:
        conversation = "unidentified"
    identity = failure.get("identity") if isinstance(failure.get("identity"), str) else None
    database.execute("""INSERT INTO engagement_decode_holds VALUES (?,?,?,?,?,?,?,1)
        ON CONFLICT(source, conversation_id) DO UPDATE SET
        identity=COALESCE(excluded.identity, engagement_decode_holds.identity),
        error_type=excluded.error_type, reason=excluded.reason, last_seen=excluded.last_seen,
        failures=engagement_decode_holds.failures+1""",
                     (source, conversation, identity, str(failure.get("error_type") or "Exception")[:80],
                      diagnostic(failure.get("reason") or ""), now, now))
    identity = database.execute("SELECT identity FROM engagement_decode_holds WHERE source=? AND conversation_id=?",
                                (source, conversation)).fetchone()[0]
    if identity:
        database.execute("INSERT OR IGNORE INTO engagement_stops VALUES (?,?,?,?)",
                         (identity, UNDECODABLE_STOP, source, "conversation:" + conversation))
    return identity is not None


CHATROOM_FAILURE_LIMIT = 3


def _count_chatroom_failure(database, source, failure, now):
    """One more consecutive failed chatroom read of this conversation; returns the count."""
    conversation = failure.get("conversation_id") or "unidentified"
    database.execute("""INSERT INTO engagement_chatroom_failures VALUES (?,?,?,1,?,?,?)
        ON CONFLICT(source, conversation_id) DO UPDATE SET consecutive=consecutive+1,
        identity=COALESCE(excluded.identity, identity), error_type=excluded.error_type,
        error=excluded.error, last_at=excluded.last_at""",
                     (source, conversation, failure.get("identity"), str(failure.get("error_type") or "Exception")[:80],
                      diagnostic(failure.get("reason") or ""), now))
    return database.execute("SELECT consecutive FROM engagement_chatroom_failures WHERE source=? AND conversation_id=?",
                            (source, conversation)).fetchone()[0]


def _release_decoded(database, source, conversation):
    """A conversation that decodes again releases its hold; its events were just recorded."""
    row = database.execute("SELECT identity FROM engagement_decode_holds WHERE source=? AND conversation_id=?",
                           (source, conversation.get("conversation_id"))).fetchone()
    if not row:
        return
    database.execute("DELETE FROM engagement_decode_holds WHERE source=? AND conversation_id=?",
                     (source, conversation["conversation_id"]))
    for identity in {row[0], conversation.get("identity")} - {None}:
        if not database.execute("SELECT 1 FROM engagement_decode_holds WHERE identity=?", (identity,)).fetchone():
            database.execute("DELETE FROM engagement_stops WHERE identity=? AND state=?", (identity, UNDECODABLE_STOP))


def undecodable_conversations(database, profile):
    """Conversations of this person (any alias) the inbox reader has not been able to decode."""
    if not database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='engagement_decode_holds'").fetchone():
        return []
    identities = identity_keys(database, profile)
    placeholders = ",".join("?" for unused in identities)
    return [row[0] for row in database.execute(
        "SELECT conversation_id FROM engagement_decode_holds WHERE identity IN (" + placeholders + ")", identities)]


def events_for(database, identity):
    identities = identity_keys(database, identity)
    placeholders = ",".join("?" for unused in identities)
    return [json.loads(row[0]) for row in database.execute(
        "SELECT payload FROM engagement_events WHERE identity IN (" + placeholders + ") ORDER BY occurred_at,source,source_id", identities)]


def business_days_after(value, days, timezone="America/Los_Angeles"):
    result = timestamp(value).astimezone(ZoneInfo(timezone))
    remaining = days
    while remaining:
        result += dt.timedelta(days=1)
        if result.weekday() < 5:
            remaining -= 1
    return result.astimezone(dt.timezone.utc)


class HeyReachReadAdapter:
    """Actual documented reads, with explicit unsupported response decoding.

    client.call uses the existing HeyReach read transport. decode_conversation
    must map a verified item shape to {conversation_id, profile}; decode_chatroom
    must return normalized events after checking complete message history. Tool
    metadata exposes no chatroom message pagination arguments, so a truncated
    chatroom must raise rather than silently mark its messages consumed.

    Offset scans restart at zero after completion to catch replies in existing
    conversations; source message IDs provide deduplication. Mutable offset
    pagination alone never grants sending clearance; that comes from our own local
    contact records (owner, 2026-09-29). No provider calls occur during construction.
    """

    def __init__(self, client, sender_id, decode_conversation=None, decode_chatroom=None, page_size=100):
        if type(sender_id) is not int or sender_id <= 0 or type(page_size) is not int or not 1 <= page_size <= 100:
            raise ValueError("Valid sender and bounded page size required")
        self.client = client
        self.sender_id = sender_id
        self.decode_conversation = decode_conversation
        self.decode_chatroom = decode_chatroom
        self.page_size = page_size

    def skip_cursor(self, cursor):
        """The cursor of the page after `cursor`, used to step over a page whose listing keeps
        failing (engagement.DEEP_PAGE_FAILURE_LIMIT)."""
        return str(int(cursor or "0") + self.page_size)

    def read_page(self, source, cursor):
        if source != "heyreach":
            raise NotImplementedError("Only existing HeyReach inbox supported")
        offset = int(cursor or "0")
        if offset < 0:
            raise ValueError("Negative inbox offset")
        port = linkedin_port.HeyReachPort(self.client, self.sender_id)
        page = port.chats(cursor=offset, limit=self.page_size).raw
        if not isinstance(page, dict) or type(page.get("totalCount")) is not int or page["totalCount"] < 0 or not isinstance(page.get("items"), list):
            raise ValueError("Unknown HeyReach conversation response")
        items = page["items"]
        if len(items) > self.page_size or (not items and offset < page["totalCount"]):
            raise ValueError("Incomplete HeyReach conversation page")
        if items and (self.decode_conversation is None or self.decode_chatroom is None):
            raise NotImplementedError("NONEMPTY_HEYREACH_RESPONSE_DECODER_UNVERIFIED")
        events, failures, decoded_conversations = [], [], []
        for item in items:
            # One conversation that fails to read or decode no longer drops the page (owner,
            # 2026-09-30): it comes back as a failure, which the cycle records as a durable hold
            # on its correspondent, and it is read again when the offset scan returns to it.
            conversation = None
            try:
                conversation = self.decode_conversation(item)
                if not isinstance(conversation.get("conversation_id"), str) or not conversation["conversation_id"]:
                    raise ValueError("Missing verified conversation ID")
                identity = profile_key(conversation["profile"])
            except Exception as error:
                failures.append(_conversation_failure(item, conversation, error))
                continue
            # A failed chatroom read (provider error, prose answer, timeout) is returned as a
            # "chatroom" failure; the cycle retries the page and holds the person only when the
            # same conversation fails on CHATROOM_FAILURE_LIMIT runs in a row. The listing call
            # above still fails the whole page, as an account-wide failure did before.
            try:
                room = port.messages(conversation["conversation_id"]).raw
                if not isinstance(room, dict) or room.get("parsed") is False:
                    raise ValueError("Unreadable HeyReach chatroom response")
            except Exception as error:
                failures.append(dict(_conversation_failure(item, conversation, error), stage="chatroom"))
                continue
            try:
                decoded = self.decode_chatroom(room, conversation)
                if not isinstance(decoded, list):
                    raise ValueError("Unknown or incomplete chatroom messages")
                for event in decoded:
                    if event.get("source") != source or profile_key(event["profile"]) != identity:
                        raise ValueError("Chatroom recipient/source mismatch")
            except Exception as error:
                failures.append(_conversation_failure(item, conversation, error))
                continue
            events.extend(decoded)
            decoded_conversations.append({"conversation_id": conversation["conversation_id"], "identity": identity})
        next_offset = offset + len(items)
        done = next_offset >= page["totalCount"]
        return {"events": events, "next_cursor": None if done else str(next_offset), "done": done,
                "failures": failures, "decoded_conversations": decoded_conversations}


def _conversation_failure(item, conversation, error):
    """Conversation id, correspondent and reason for one conversation that could not be read.

    The validated listing binding wins; otherwise the raw item's own id and correspondent
    profile are used when well formed, so the person can still be held. Never the payload.
    """
    conversation_id, identity = None, None
    for candidate in (conversation, item):
        if not isinstance(candidate, dict):
            continue
        value = candidate.get("conversation_id", candidate.get("id"))
        if conversation_id is None and isinstance(value, str) and value.strip():
            conversation_id = value
        profile = candidate.get("profile")
        if profile is None and isinstance(candidate.get("correspondentProfile"), dict):
            profile = candidate["correspondentProfile"].get("profileUrl")
        if identity is None and isinstance(profile, str):
            try:
                identity = profile_key(profile)
            except ValueError:
                pass
    if conversation_id is None:
        # No id to key it by: one row per distinct item (a digest, never the payload), so two
        # unidentifiable conversations never collapse into one.
        stable = {"account": item.get("linkedInAccountId") if isinstance(item, dict) else None,
                  "correspondent": identity or ({key: item["correspondentProfile"].get(key)
                      for key in ("linkedin_id", "profileUrl")}
                      if isinstance(item, dict) and isinstance(item.get("correspondentProfile"), dict) else None)}
        conversation_id = "unidentified:sha256:" + hashlib.sha256(
            json.dumps(stable, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    return {"conversation_id": conversation_id, "identity": identity,
            "error_type": type(error).__name__, "reason": diagnostic(error)}


# --- Unipile inbox (E3.T3 option A) ---------------------------------------------------------------
UNIPILE = "unipile"
# Documented endpoint, never the account's own dsn (which is not printed or stored).
UNIPILE_READ_SOURCE = "https://api.unipile.com/api/v1/chats/messages"


def port_message(chat_id, message):
    """A port Message in the {sender, body, createdAt} shape the HeyReach rooms and follow-up checks use."""
    sender = "ME" if message.is_sender is True else "CORRESPONDENT" if message.is_sender is False else None
    return {"chat_id": chat_id, "sender": sender, "body": message.text, "createdAt": message.sent_at}


def unipile_message_event(chat_id, message, profile, sender_id, sender_profile, observed):
    """One message of a Unipile chat as an engagement event: inbound from the person, outbound_observed
    from our side, with the same authenticated linkedin-inbox-v1 envelope the HeyReach decoder writes, so
    reply review (pipeline_routing) takes it unchanged. `message` is `port_message`'s shape. The id is
    content-keyed (chat, direction, text, time) and shared with the inactivity closure read, so one
    message is one event whichever reads it first. `sender_id` is the configured sending identity
    (config["sender_id"], as for every Unipile action; the caller has checked the chat is on the port's
    account). Raises ValueError for a message that cannot be read as text."""
    occurred = timestamp(message["createdAt"])
    observed = timestamp(observed)
    # One spelling for the person whichever reader builds the event (the closure read passes
    # "https://" + key, the inbox reader the INVITE's own spelling), as the HeyReach decoder does.
    profile = "https://" + profile_key(profile)
    if (message.get("sender") not in {"ME", "CORRESPONDENT"} or not isinstance(message.get("body"), str)
            or not message["body"].strip() or occurred > observed):
        raise ValueError("UNIPILE_MESSAGE_INVALID")
    direction = "outbound" if message["sender"] == "ME" else "inbound"
    source_id = "unipile-message:" + hashlib.sha256(
        json.dumps([chat_id, message["sender"], message["body"], occurred.isoformat()]).encode()).hexdigest()
    raw = {"conversation_id": chat_id, "profile": profile, "direction": direction, "text": message["body"],
           "message": dict(message, createdAt=occurred.isoformat()), "message_id": source_id,
           "account_id": sender_id, "account_profile": sender_profile,
           "sender_profile": sender_profile if direction == "outbound" else profile,
           "quoted": None, "forwarded": None}
    fields = ("message_id", "conversation_id", "account_id", "sender_profile", "direction", "text", "quoted", "forwarded")
    inbox = {field: raw[field] for field in fields}
    inbox.update({"schema": "linkedin-inbox-v1", "raw_paths": {field: [field] for field in fields},
                  "authentication": {"method": "authenticated_provider_read", "source": UNIPILE_READ_SOURCE,
                                     "verified_by": "unipile-port-v1", "observed_at": observed.isoformat()}})
    return {"source": UNIPILE, "source_id": source_id, "source_id_kind": "synthetic_content_sha256_v1",
            "profile": profile, "kind": "outbound_observed" if direction == "outbound" else "inbound",
            "direction": direction, "occurred_at": occurred.isoformat(), "observed_at": observed.isoformat(),
            "evidence": "unipile:messages:" + chat_id, "raw": raw, "inbox": inbox, "verified": True}


def unipile_attendee_profile(database, provider_id, chat_id=None):
    """The person behind a chat, from our own records: the Unipile actions that carry the provider id the
    port's lookup of that profile returned, or that a read confirmed sent into this chat (CONFIRMED_SENT
    with this stored conversation_id, e.g. a port INITIAL into an existing chat; the chat id is written at
    enqueue, so an action that never sent names nobody). None when no record names anyone;
    ValueError when two people are named."""
    if not (provider_id or chat_id) or not database.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='outreach_actions'").fetchone():
        return None
    found = {}
    for identity, profile in database.execute("""SELECT identity, json_extract(payload,'$.profile') FROM outreach_actions
            WHERE json_extract(payload,'$.provider')=? AND (json_extract(payload,'$.provider_id')=?
            OR json_extract(payload,'$.conversation_id')=? AND state='CONFIRMED_SENT')""", (UNIPILE, provider_id, chat_id)):
        found.setdefault(identity, profile)
    if len(found) > 1:
        raise ValueError("UNIPILE_ATTENDEE_AMBIGUOUS")
    return next(iter(found.values()), None)


class UnipileReadAdapter:
    """The Unipile twin of HeyReachReadAdapter: read_page("unipile", cursor) over the LinkedInPort
    (`GET /chats`, then each chat's `GET /chats/{id}/messages`), returning the same normalized page.

    The account read must say the session is valid, and its own profile (when it names one) must be
    the configured sender; a chat on another account is a conversation failure. A chat's attendee is
    the person `resolve(provider_id, chat_id)` names from our own records; a chat it names nobody for is left
    unread and counted (`unresolved`). A failed messages read is a "chatroom" failure (retried, held
    after CHATROOM_FAILURE_LIMIT runs); a message that is not text fails its conversation (held, as an
    undecodable HeyReach room). The cursor is Unipile's opaque continuation token; there is no
    skip_cursor, so a deep page that keeps failing restarts the deep scan."""

    def __init__(self, port, sender_id, sender_profile, resolve, clock=None):
        self.port = port
        self.sender_id = sender_id
        self.sender_profile = sender_profile
        self.resolve = resolve
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))

    def read_page(self, source, cursor):
        if source != UNIPILE:
            raise NotImplementedError("Only the Unipile inbox supported")
        account = self.port.account()
        if account.valid is not True:
            raise ValueError("Unipile session not valid")
        if account.own_profile and profile_key(account.own_profile) != profile_key(self.sender_profile):
            raise ValueError("Unipile account profile is not the configured sender")
        page = self.port.chats(cursor)
        if not page.complete:
            raise ValueError("Incomplete Unipile chat page")
        events, failures, decoded_conversations, unresolved = [], [], [], []
        for chat in page.items:
            item = chat.raw if isinstance(chat.raw, dict) else {"id": chat.chat_id}
            conversation = None
            try:
                profile = self.resolve(chat.attendee_provider_id, chat.chat_id)
                if profile is None:
                    unresolved.append(chat.chat_id)
                    continue
                conversation = {"conversation_id": chat.chat_id, "profile": profile}
                identity = profile_key(profile)
                if item.get("account_id") != account.account_id:
                    raise ValueError("Chat account mismatch")
            except Exception as error:
                failures.append(_conversation_failure(item, conversation, error))
                continue
            try:
                thread = self.port.messages(chat.chat_id)
                if thread.complete is not True:
                    raise ValueError("Incomplete Unipile chat history")
            except Exception as error:
                failures.append(dict(_conversation_failure(item, conversation, error), stage="chatroom"))
                continue
            # Observed once this chat's read is done, as the HeyReach decoder does per chatroom.
            observed = timestamp(self.clock())
            try:
                decoded = [unipile_message_event(chat.chat_id, port_message(chat.chat_id, message), profile,
                                                 self.sender_id, self.sender_profile, observed)
                           for message in thread.messages]
            except Exception as error:
                failures.append(_conversation_failure(item, conversation, error))
                continue
            events.extend(decoded)
            decoded_conversations.append({"conversation_id": chat.chat_id, "identity": identity})
        done = page.next_cursor is None
        return {"events": events, "next_cursor": page.next_cursor, "done": done, "failures": failures,
                "decoded_conversations": decoded_conversations, "unresolved": unresolved}


def next_action(database, profile, now=None, recipient="", campaign_id=None):
    """Plan only. Weekdays exclude weekends; no holiday calendar is assumed.

    Acceptance window is inclusive 12–24h; missed windows require review.
    Urgent default is conservatively 48h elapsed and 23–25h before interview.
    Follow-up two is five business days after the actual first follow-up.
    """
    now = timestamp(now or dt.datetime.now(dt.timezone.utc))
    identity = profile_key(profile)
    closed = closure_status(database, identity)
    if closed:
        return {**closed, "blocker": "OUTREACH_CLOSED"}
    import owner_seed
    if owner_seed.no_automatic(identity_keys(database, identity), {name_key(recipient)}, campaign_id):
        return {"state": "BLOCKED", "blocker": "NO_AUTOMATIC_FOLLOWUP_CONTACT"}
    stopped = stop_label(database, identity)
    if stopped:
        return {"state": "BLOCKED", "blocker": stopped}
    events = events_for(database, identity)
    if unreconciled_outbound(database, events):
        return {"state": "BLOCKED", "blocker": "OBSERVED_OUTBOUND_REQUIRES_PURPOSE_RECONCILIATION"}
    initials = [event for event in events if event["kind"] == "initial_sent"]
    follows = [event for event in events if event["kind"] == "followup_sent"]
    if len(follows) >= 2:
        return {"state": "BLOCKED", "blocker": "FOLLOWUP_LIMIT"}
    if not initials:
        accepts = [event for event in events if event["kind"] == "accepted"]
        if not accepts:
            return {"state": "BLOCKED", "blocker": "VERIFIED_ACCEPTANCE_OR_INITIAL_MISSING"}
        accepted = timestamp(accepts[0]["occurred_at"])
        due, expires = accepted + dt.timedelta(hours=12), accepted + dt.timedelta(hours=24)
        return {"state": "WAITING" if now < due else "DUE" if now <= expires else "BLOCKED",
                "purpose": "FIRST_MESSAGE", "due_at": due.isoformat(), "expires_at": expires.isoformat(),
                "blocker": "ACCEPTANCE_WINDOW_EXPIRED" if now > expires else None}
    initial = timestamp(initials[0]["occurred_at"])
    interviews = [event for event in events if event["kind"] == "interview"]
    if not follows and interviews:
        interview = timestamp(max(interviews, key=lambda event: timestamp(event["observed_at"]))["occurred_at"])
        if initial + dt.timedelta(hours=48) <= now and dt.timedelta(hours=23) <= interview - now <= dt.timedelta(hours=25):
            return {"state": "DUE", "purpose": "URGENT", "ordinal": 1,
                    "due_at": max(initial + dt.timedelta(hours=48), interview - dt.timedelta(hours=25)).isoformat(),
                    "expires_at": (interview - dt.timedelta(hours=23)).isoformat()}
    due = business_days_after(follows[-1]["occurred_at"], 5) if follows else business_days_after(initial, 3)
    return {"state": "DUE" if now >= due else "WAITING", "purpose": "FOLLOWUP",
            "ordinal": len(follows) + 1, "due_at": due.isoformat()}


def _read_pass(database, client, source, role, page_cursor, now, item, outcome, counted=None):
    """Read one page and commit it with the cursor it implies.

    "top" is offset 0: it never moves a running deep scan, and starts one (at its next page)
    when none is saved. "deep" advances the saved cursor, or keeps it when a conversation's
    chatroom read failed below CHATROOM_FAILURE_LIMIT so that page is read again next run.
    """
    current = database.execute("SELECT cursor,generation FROM engagement_cursors WHERE source=?", (source,)).fetchone()
    # Only a failure up to here (the listing read and the page it returned) is a failure of the
    # page itself; one while recording it is not, and never counts toward skipping the page.
    outcome["stage"] = "listing"
    page = client.read_page(source, page_cursor)
    if not isinstance(page, dict) or not isinstance(page.get("events"), list) or type(page.get("done")) is not bool:
        raise ValueError("Unsupported normalized page")
    next_cursor = page.get("next_cursor")
    if next_cursor is not None and not isinstance(next_cursor, str):
        raise ValueError("Invalid opaque cursor")
    if not page["done"] and (not next_cursor or next_cursor == page_cursor):
        raise ValueError("Nonadvancing page")
    outcome["stage"] = "record"
    database.execute("BEGIN IMMEDIATE")
    try:
        fresh = database.execute("SELECT cursor,generation FROM engagement_cursors WHERE source=?", (source,)).fetchone()
        if tuple(fresh) != tuple(current) or (role == "deep" and fresh[0] != page_cursor):
            database.rollback()
            outcome["state"] = "CONCURRENT_PROGRESS"
            return
        inserted = 0
        for event in page["events"]:
            if event.get("source") != source:
                raise ValueError("Source mismatch")
            inserted += _record(database, event)
        held, retry = [], []
        for conversation in page.get("decoded_conversations") or []:
            _release_decoded(database, source, conversation)
            database.execute("DELETE FROM engagement_chatroom_failures WHERE source=? AND conversation_id=?",
                             (source, conversation.get("conversation_id")))
        for failure in page.get("failures") or []:
            if failure.get("stage") == "chatroom":
                # One count per run: a conversation listed on both pages of a run (the listing
                # shifted between the passes) is counted once.
                key = failure.get("conversation_id") or "unidentified"
                if counted is not None and key in counted:
                    count = database.execute("SELECT consecutive FROM engagement_chatroom_failures WHERE source=? AND conversation_id=?",
                                             (source, key)).fetchone()
                    count = count[0] if count else 1
                else:
                    count = _count_chatroom_failure(database, source, failure, now.isoformat())
                    if counted is not None:
                        counted.add(key)
                if count < CHATROOM_FAILURE_LIMIT:
                    retry.append({"conversation_id": failure.get("conversation_id"), "consecutive": count,
                                  "error_type": failure.get("error_type"), "reason": failure.get("reason")})
                    continue
                failure = dict(failure, reason="Chatroom read failed on " + str(count)
                               + " runs in a row: " + str(failure.get("reason") or ""))
            else:
                database.execute("DELETE FROM engagement_chatroom_failures WHERE source=? AND conversation_id=?",
                                 (source, failure.get("conversation_id")))
            held.append({"conversation_id": failure.get("conversation_id"),
                         "error_type": failure.get("error_type"), "reason": failure.get("reason"),
                         "held": _hold_undecodable(database, source, failure, now.isoformat())})
        state = "COMPLETE" if page["done"] else "MORE_PENDING"
        if role == "top":
            saved = current[0] if current[0] else (None if page["done"] else next_cursor)
        else:
            saved = page_cursor if retry else (None if page["done"] else next_cursor)
        if retry:
            state = "CHATROOM_RETRY"
        database.execute("UPDATE engagement_cursors SET cursor=?,generation=generation+1,checked_at=?,state=? WHERE source=?",
                         (saved, now.isoformat(), state, source))
        database.commit()
    except BaseException:
        database.rollback()
        raise
    outcome["state"] = state
    item.update(inserted=item["inserted"] + inserted, pages=item["pages"] + 1)
    if held:
        item.setdefault("undecodable", []).extend(held)
    if retry:
        item.setdefault("chatroom_retry", []).extend(retry)
    if page.get("unresolved"):
        item["unresolved_chats"] = item.get("unresolved_chats", 0) + len(page["unresolved"])


DEEP_PAGE_FAILURE_LIMIT = 3


def _deep_page_failed(database, client, source, page_cursor, error, now):
    """Count a failed deep page; on the DEEP_PAGE_FAILURE_LIMIT-th run in a row at the same
    cursor, step over it (the adapter's skip_cursor, else restart the deep scan) and return what
    was skipped for the report. Offset 0 is never skipped."""
    with database:
        row = database.execute("SELECT cursor,consecutive FROM engagement_deep_page_failures WHERE source=?", (source,)).fetchone()
        consecutive = row[1] + 1 if row and row[0] == page_cursor else 1
        database.execute("INSERT OR REPLACE INTO engagement_deep_page_failures VALUES (?,?,?,?,?,?)",
                         (source, page_cursor, consecutive, type(error).__name__, diagnostic(error), now.isoformat()))
        if consecutive < DEEP_PAGE_FAILURE_LIMIT:
            return None
        skip = getattr(client, "skip_cursor", None)
        following = skip(page_cursor) if callable(skip) else None
        updated = database.execute("UPDATE engagement_cursors SET cursor=?,generation=generation+1,checked_at=?,state='DEEP_PAGE_SKIPPED' "
                                   "WHERE source=? AND cursor=?", (following, now.isoformat(), source, page_cursor)).rowcount
        database.execute("DELETE FROM engagement_deep_page_failures WHERE source=?", (source,))
        if updated:
            # Durable: a skipped page is kept on record for the report and `status`.
            database.execute("INSERT INTO engagement_skipped_pages VALUES (?,?,?,?,?,?,?)",
                             (source, page_cursor, following, consecutive, type(error).__name__,
                              diagnostic(error), now.isoformat()))
    if not updated:
        return None
    return {"cursor": page_cursor, "next_cursor": following, "failures": consecutive,
            "error_type": type(error).__name__, "error": diagnostic(error)}


def cycle(database, config, client, now=None):
    """Ingest bounded pages atomically. Disabled unless engagement_enabled=True.

    Incomplete pages never assert complete history. Main must supply an adapter with
    stable paging semantics; outbound work is authorized from our own local contact
    records (owner, 2026-09-29), not from these pages alone.
    """
    initialize(database)
    if config.get("engagement_enabled") is not True:
        return {"state": "DISABLED", "items": []}
    now = timestamp(now or dt.datetime.now(dt.timezone.utc))
    results = []
    for source in config.get("engagement_sources", ["heyreach"]):
        with database:
            database.execute("INSERT OR IGNORE INTO engagement_cursors(source,state) VALUES (?,'PENDING')", (source,))
        item = {"source": source, "inserted": 0, "pages": 0, "state": "PENDING", "passes": []}
        results.append(item)
        # Every run reads offset 0 (the newest conversations) first, then one page of the deep
        # scan at the saved cursor: at most two pages a run, and a failing deep page never keeps
        # offset 0 from being read. (New replies surface on offset 0 only if get_conversations_v2
        # lists by latest activity; that ordering is not verified, so the deep scan still covers
        # every conversation in turn.) engagement_max_pages no longer applies.
        deep = database.execute("SELECT cursor FROM engagement_cursors WHERE source=?", (source,)).fetchone()[0]
        counted = set()
        for role, page_cursor in (("top", None),) + ((("deep", deep),) if deep else ()):
            outcome = {"pass": role, "cursor": page_cursor}
            item["passes"].append(outcome)
            try:
                _read_pass(database, client, source, role, page_cursor, now, item, outcome, counted)
            except Exception as error:
                if database.in_transaction:
                    database.rollback()
                outcome.update(state="BLOCKED", error_type=type(error).__name__, error=diagnostic(error))
                item.update(state="BLOCKED", blocker="UNSUPPORTED_READ_ADAPTER" if isinstance(error, (NotImplementedError, AttributeError)) else "INBOX_READ_OR_EVENT_INVALID",
                            error_type=type(error).__name__, error=diagnostic(error))
                # A failed offset-0 read is account-wide: no deep page this run. A failed deep page
                # keeps its cursor and is read again next run, after offset 0, until it has failed
                # DEEP_PAGE_FAILURE_LIMIT runs in a row; then it is stepped over and reported.
                if role == "deep" and outcome.get("stage") == "listing":
                    try:
                        skipped = _deep_page_failed(database, client, source, page_cursor, error, now)
                    except Exception as failure:      # e.g. database locked: report, never stop the cycle
                        if database.in_transaction:
                            database.rollback()
                        item["deep_page_count_error"] = {"error_type": type(failure).__name__, "error": diagnostic(failure)}
                        skipped = None
                    if skipped:
                        item.setdefault("skipped_pages", []).append(skipped)
                break
            if role == "deep" and outcome.get("state") != "CONCURRENT_PROGRESS":
                try:
                    with database:
                        database.execute("DELETE FROM engagement_deep_page_failures WHERE source=?", (source,))
                except Exception as failure:
                    item["deep_page_count_error"] = {"error_type": type(failure).__name__, "error": diagnostic(failure)}
            if outcome["state"] == "CONCURRENT_PROGRESS":
                item["state"] = "CONCURRENT_PROGRESS"
                break
            if item["state"] != "BLOCKED":
                item["state"] = outcome["state"] if item["state"] in {"PENDING", "COMPLETE", "MORE_PENDING"} else item["state"]
    reconciliation = reconcile_observed_outbound(database, config, now)
    planned = []
    for row in database.execute("SELECT DISTINCT identity FROM engagement_events").fetchall():
        identity = row[0]
        action = next_action(database, "https://" + identity, now)
        if action.get("state") != "DUE":
            continue
        action_id = identity + ":" + action["purpose"] + ":" + str(action.get("ordinal", 0))
        deadline = action.get("expires_at") or (timestamp(action["due_at"]) + dt.timedelta(days=30)).isoformat()
        with database:
            task_id = workflow_tasks.enqueue(database, "message_writer", action_id, "engagement:timing",
                [{"source": "engagement_events:" + identity, "observed_at": action["due_at"], "policy": action}],
                deadline, now.isoformat(), contact_id=identity)
        planned.append({"task_id": task_id, "identity": identity, **action})
    return {"state": "PROCESSED", "items": results, "handoffs": planned, "outbound_reconciliation": reconciliation}
