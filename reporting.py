import datetime as dt
import json
import sqlite3
from collections import Counter
from zoneinfo import ZoneInfo

import applications
import champion_reporting
import source_records


def initialize(database):
    database.execute("""
        CREATE TABLE IF NOT EXISTS daily_reports (
            day TEXT PRIMARY KEY, generated_at TEXT NOT NULL,
            payload TEXT NOT NULL, notification_key TEXT NOT NULL)
    """)
    database.execute("""
        CREATE TABLE IF NOT EXISTS outcome_events (
            action_id TEXT PRIMARY KEY, kind TEXT NOT NULL, at TEXT NOT NULL,
            attributed INTEGER NOT NULL, segment TEXT NOT NULL, evidence TEXT NOT NULL)
    """)
    database.execute("""
        CREATE TABLE IF NOT EXISTS notification_outbox (
            event_key TEXT PRIMARY KEY, message TEXT NOT NULL, state TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, next_at TEXT, delivery TEXT,
            last_error TEXT, updated_at TEXT NOT NULL)
    """)


def record_outcome(database, action_id, kind, at, attributed, segment, evidence):
    if kind not in {"RECRUITER_CONVERSATION", "INTERVIEW", "REFERRAL", "REPLY_POSITIVE", "REPLY_NEGATIVE", "APPLICATION_RESPONSE"}:
        raise ValueError("Unknown outcome")
    if not evidence.get("source") or not evidence.get("observed_at") or evidence.get("verified") is not True:
        raise ValueError("Verified evidence required; attribution cannot be inferred")
    if attributed:
        raise ValueError("Attributed outcomes are not recorded; attribution needs a source-bound adapter")
    database.execute("INSERT OR IGNORE INTO outcome_events VALUES (?,?,?,?,?,?)", (action_id, kind, at, int(attributed is True), json.dumps(segment), json.dumps(evidence)))


def application_executor_gate(database, config, now):
    import desktop_adapter
    desktop = config.get("application_adapter") == "desktop"
    adapter = "desktop" if desktop else "command" if config.get("application_adapter_command") else "missing"
    table_exists = database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='desktop_requests'").fetchone() is not None
    requests = {"table_exists": table_exists, "pending": None, "claimed": None, "expired": None, "unknown": None,
                "expired_active": None}
    if table_exists:
        counts = {row["state"]: row["count"] for row in database.execute("SELECT state,COUNT(*) AS count FROM desktop_requests GROUP BY state")}
        requests.update({state.lower(): counts.get(state, 0) for state in ("PENDING", "CLAIMED", "EXPIRED", "UNKNOWN")})
        requests["expired_active"] = database.execute(
            "SELECT COUNT(*) FROM desktop_requests WHERE state IN ('PENDING','CLAIMED') AND julianday(expires_at)<=julianday(?)", (now,)).fetchone()[0]
    result = {"adapter": adapter, "adapter_configured": adapter != "missing", "desktop_adapter_configured": desktop,
            "state": "ADAPTER_MISSING" if adapter == "missing" else "CONFIGURED_EXECUTION_UNVERIFIED",
            "applications_enabled": bool(config.get("applications_enabled", False)),
            "execution_ownership_verified": bool(config.get("execution_ownership_verified", False)),
            "scheduled_host_execution": "UNVERIFIED", "desktop_requests": requests,
            "desktop_host": desktop_adapter.host_health(database, now),
            "detail": "Configuration and foreground read-only evidence do not prove scheduled-host execution. "
                      "Request counts are saved states; expired_active counts overdue PENDING/CLAIMED requests without changing them. "
                      "Unknown outcomes require reconciliation before retry; drafts and requests are not submissions."}
    if desktop and config.get("application_browser", {}).get("enabled") is True:
        result["browser_consumer_enabled"] = True
        result["state"] = "CONNECTED_AWAITING_BROWSER_OBSERVATION"
        result["desktop_host"].update(state="CONNECTED_NOT_YET_OBSERVED", code="APPLICATION_BROWSER_NOT_YET_OBSERVED",
            implementation="subscription_browser_mailbox_consumer",
            next_action="Await a guarded browser observation in this queue; see the release audit for isolated scheduled validation")
        events_exist = database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='desktop_events'").fetchone()
        if events_exist:
            observation = database.execute("SELECT at,detail FROM desktop_events WHERE event='BROWSER_CONSUMER_COMPLETED' ORDER BY id DESC LIMIT 1").fetchone()
            if observation:
                detail = json.loads(observation["detail"])
                result["last_browser_observation"] = {"at": observation["at"], **detail}
                if detail.get("observed_browser_calls", 0) > 0:
                    scheduled = detail.get("execution_context") == "launchd"
                    result["state"] = "SCHEDULED_BROWSER_OBSERVED" if scheduled else "BROWSER_OBSERVED"
                    result["scheduled_host_execution"] = "OBSERVED" if scheduled else "UNVERIFIED"
                    result["desktop_host"].update(state="OBSERVED", code=None,
                        unattended_ready=scheduled, can_submit=bool(config.get("applications_enabled") and config.get("execution_ownership_verified")),
                        implementation="subscription_browser_mailbox_consumer", observed_at=observation["at"],
                        next_action="Continue per-application guards; browser observation is not proof of a submitted application")
        result["detail"] = "Connected to the existing subscription browser through the shared mailbox and action guard. Saved browser observations do not establish current account access or a verified application submission."
    return result


def guarded_outreach_gate(config):
    mode = config.get("outreach_mode")
    return {"mode": mode, "state": "REQUIRES_PER_RECIPIENT_VALIDATION" if mode == "guarded" else "OUTREACH_NOT_ENABLED",
            "validation": "NOT_EVALUATED_BY_REPORT",
            "requirements": ["Verified new recipient and team relevance", "Local contact records show no prior contact (owner, 2026-09-29)",
                             "No prior contact, pending invitation, reply, exclusion or uncertain attempt; contact limits respected",
                             "Prepared single-contact connection-request campaign", "Live provider checks", "Durable pre-send claim"],
            "detail": "Only the deterministic guarded dispatcher may launch after all gates pass. "
                      "Research drafts and prepared campaigns are not execution or delivery. "
                      "Only recorded prior contact blocks sending; uncertain sends must not be retried. Email and automatic follow-ups are disabled."}


def message_executor_gate(database, config):
    actions_enabled = config.get("outreach_actions_enabled") is True
    connection_enabled = config.get("outreach_agent", {}).get("enabled") is True
    configured = config.get("outreach_allowed_purposes", [])
    scope = configured if isinstance(configured, list) and all(isinstance(value, str) for value in configured) else []
    supported = [purpose for purpose in scope if purpose in {"INITIAL", "FIRST_MESSAGE", "REPLY"}]
    state = "CONFIGURED_REQUIRES_PER_ACTION_VALIDATION"
    if not actions_enabled:
        state = "DISABLED"
    elif not connection_enabled:
        state = "OUTREACH_AGENT_CONNECTION_DISABLED"
    elif config.get("outreach_mode") != "guarded" or not supported:
        state = "NO_ENABLED_MESSAGE_SCOPE"
    tasks = section(database, "workflow_tasks", ("task_id", "worker", "status", "attempts", "max_attempts",
                                                 "deadline", "blocker", "next_action", "next_at", "updated_at"))
    if tasks["records"] is not None:
        tasks["records"] = [row for row in tasks["records"] if row.get("worker") == "outreach-draft-inspection"]
    retries = section(database, "outreach_read_retries", ("action_id", "phase", "state", "attempts",
                                                          "last_error", "next_at", "updated_at"))
    for row in retries["records"] or []:
        row.update(champion_reporting.read_retry_status(row))
    return {"state": state, "basis": "CONFIGURATION_AND_SAVED_STATE", "checked_by_this_report": False,
            "actions_enabled": actions_enabled, "connection_enabled": connection_enabled,
            "mode": config.get("outreach_mode"), "configured_purposes": scope, "supported_purposes": supported,
            "validation": "NOT_EVALUATED_BY_REPORT", "confirmed_not_sent_automatic_resend_supported": False,
            "read_retries": retries,
            "draft_investigations": tasks,
            "detail": "Configuration does not prove execution, sender entitlement or a fresh local-records history check. "
                      "No live check performed. Only an independent claim-bound receipt confirms delivery; "
                      "ACKNOWLEDGED_UNCONFIRMED and drafts are not receipts. Uncertain sends cannot be repeated. "
                      "CONFIRMED_NOT_SENT automatic resends are unsupported."}


def section(database, table, fields):
    records = source_records.rows(database, table)
    if records is None:
        return {"state": "UNKNOWN_TABLE_ABSENT", "basis": "SAVED", "records": None}
    available = source_records.columns(database, table)
    return {"state": "KNOWN_SAVED", "basis": "SAVED", "missing_fields": sorted(set(fields) - available),
            "records": [{field: row[field] for field in fields if field in row} for row in records]}


def reply_delivery_actions(database, now):
    states = {"PENDING": "QUEUED", "BLOCKED": "BLOCKED", "AWAITING_APPROVAL": "AWAITING_APPROVAL",
              "ACKNOWLEDGED_UNCONFIRMED": "ACKNOWLEDGED_UNCONFIRMED",
              "CLAIMED": "UNCERTAIN", "DISPATCHING": "UNCERTAIN",
              "REQUEST_STARTED": "UNCERTAIN", "UNCERTAIN_NO_RETRY": "UNCERTAIN",
              "CONFIRMED_SENT": "CONFIRMED_SENT", "CONFIRMED_NOT_SENT": "CONFIRMED_NOT_SENT",
              "CANCELLED_BEFORE_SEND": "CANCELLED_BEFORE_SEND", "CANCELLED": "CANCELLED", "CLOSED": "CLOSED"}
    claims = {row["action_id"]: row for row in source_records.rows(database, "outreach_action_claims", ("action_id",)) or []}
    actions = source_records.rows(database, "outreach_actions", ("action_id", "state", "payload"))
    grouped = {}
    for row in actions or []:
        action = source_records.decoded(row.get("payload"))
        if not isinstance(action, dict) or action.get("purpose") != "REPLY":
            continue
        binding = tuple(action.get(field) for field in ("reply_task_id", "reply_source_key", "reply_source_id"))
        if not all(isinstance(value, str) and value for value in binding):
            continue
        state = row["state"] if isinstance(row["state"], str) and row["state"] in states else "UNKNOWN"
        delivery = {"action_id": row["action_id"], "action_state": state,
                    "delivery_state": states.get(state, "UNKNOWN"), "receipt_reference": None}
        if state == "CONFIRMED_SENT":
            receipt = source_records.decoded(row.get("result"))
            claim = claims.get(row["action_id"], {})
            bound = (isinstance(receipt, dict) and bool(claim)
                     and all(row.get(field) == claim.get(field) for field in
                             ("payload", "payload_hash", "identity", "claimed_at"))
                     and receipt.get("sender_id") == claim.get("sender_id"))
            try:
                verified = bound and source_records.execution(database, {"table": "outreach_actions", "key": row["action_id"]}, now)
            except (TypeError, ValueError, KeyError, AttributeError):
                verified = False
            if verified and isinstance(receipt.get("receipt_id"), (str, int)) and not isinstance(receipt["receipt_id"], bool):
                delivery["receipt_reference"] = receipt["receipt_id"]
            else:
                delivery["delivery_state"] = "UNCERTAIN"
        grouped.setdefault(binding, []).append(delivery)
    return actions is not None, grouped


def champion_deliveries(database, now=None):
    saved = champion_reporting.snapshot(database, now)
    # A summary's referral is the person's record (owner, 2026-09-25: referrals are tracked per
    # person, not per job); its review date replaces the old per-job hold deadline.
    referrals = {row["contact_id"]: row for row in source_records.rows(database, "referrals") or []}
    records = []
    for item in saved["items"]:
        record = {field: item.get(field) for field in (
            "summary_id", "job_key", "contact_id", "task_id", "reply_task_id", "action_id",
            "action_state", "delivery_state", "receipt_reference", "task_status", "error")}
        record["read_retries"] = item.get("read_retries", [])
        deadline = source_records.instant(item.get("deadline"))
        referral = referrals.get(item.get("contact_id"), {})
        hold_deadline = source_records.instant(referral.get("review_at"))
        record.update(deadline=deadline.isoformat() if deadline else None,
                      hold_deadline=hold_deadline.isoformat() if hold_deadline else None,
                      next_action=item["next_action"],
                      next_action_basis="DELIVERY_STATE; private saved instructions remain in the task",
                      delivered=item["delivery_state"] == "CONFIRMED_SENT", verified_business_state=None)
        records.append(record)
    return {"state": saved["state"], "records": records,
            "delivery_counts": dict(Counter(record["delivery_state"] for record in records)),
            "basis": "Saved shared REPLY sender claim and independent receipt; no live check. "
                     "Champion delivery does not verify referral submission."}


def reply_decisions(database, now=None):
    import engagement
    import reply_worker
    summary = section(database, "reply_worker_results", ("task_id", "output_version", "input_sha256", "output_sha256"))
    summary.update(basis="SAVED_AI_INTERPRETATION_NOT_CONFIRMED_REFERRAL_OR_DELIVERY",
                   checked_by_this_report=False, delivery_basis="SAVED_ACTION_STATE_AND_CLAIM_BOUND_RECEIPT")
    summary["champion_delivery"] = champion_deliveries(database, now)
    champions = {}
    for champion in summary["champion_delivery"]["records"]:
        champions.setdefault(champion.get("reply_task_id"), []).append(champion)
    actions_known, actions = reply_delivery_actions(database, now or dt.datetime.now(dt.timezone.utc).isoformat())
    sources = {row["source_key"]: row for row in source_records.rows(database, "pipeline_routing_sources", ("source_key", "snapshot")) or []}
    results = {row["task_id"]: row for row in source_records.rows(database, "reply_worker_results", ("task_id", "handoff")) or []}
    allowed_holds = {"UNKNOWN_FACT:" + fact for fact in reply_worker.FACTS} | {
        "CONTEXTUAL_AMBIGUOUS", "CONTEXTUAL_QUOTED_OR_FORWARDED", "CONTEXTUAL_OTHER",
        "UNKNOWN_QUOTED_CONTEXT", "UNKNOWN_FORWARDED_CONTEXT", "QUOTED_CONTEXT_REQUIRES_ADJUDICATION",
        "FORWARDED_CONTEXT_REQUIRES_ADJUDICATION", "CONFLICTING_CONVERSATION_CONTEXT"}
    for record in summary["records"] or []:
        handoff = source_records.decoded(results.get(record.get("task_id"), {}).get("handoff"))
        handoff = handoff if isinstance(handoff, dict) else {}
        classification = handoff.get("classification")
        classification = classification if isinstance(classification, dict) else {}
        source_key = handoff.get("source_key")
        source_key = source_key if isinstance(source_key, str) else None
        source = source_records.decoded(sources.get(source_key, {}).get("snapshot"))
        source = source if isinstance(source, dict) else {}
        identity = source.get("identity")
        closed = bool(identity and engagement.closure_status(database, identity))
        # `observations` carries what used to be `holds` (owner, 2026-09-23): the signals the
        # classifier recorded, none of which block. They are surfaced here BECAUSE they no longer
        # block - the daily report is now the place the owner sees why a conversation was not
        # advanced, instead of it being invisible in a table nobody reads.
        holds = handoff.get("holds", []) or []
        observations = handoff.get("observations", []) or []
        holds = sorted({hold if isinstance(hold, str) and hold in allowed_holds else "UNRECOGNIZED_HOLD"
                        for hold in (holds if isinstance(holds, list) else [None])})
        observations = sorted({item if isinstance(item, str) and item in allowed_holds else "UNRECOGNIZED_HOLD"
                               for item in (observations if isinstance(observations, list) else [None])})
        if closed:
            holds.append("OUTREACH_CLOSED")
        missing = classification.get("unknown_factual_requirements", [])
        missing = sorted({fact if isinstance(fact, str) and fact in reply_worker.FACTS else "OTHER"
                          for fact in (missing if isinstance(missing, list) else [None])})
        label = classification.get("label")
        label = label if isinstance(label, str) and label in reply_worker.LABELS else "UNKNOWN"
        state = handoff.get("state")
        state = state if state in ("DECISION_READY", "INVESTIGATION_REQUIRED") else "UNKNOWN"
        review_at = source_records.instant(classification.get("review_at"))
        advance = (handoff.get("can_advance_conversion") is True and not holds and bool(identity)
                   and state == "DECISION_READY" and label in {"AGREEMENT", "DECLINE"}
                   and record.get("output_version") == reply_worker.VERSION)
        binding = (record.get("task_id"), source_key, source.get("source_id"))
        matched = actions.get(binding, []) if all(isinstance(value, str) for value in binding) else []
        delivery_actions = sorted(matched,
                                  key=lambda action: action["action_id"])
        proposal = isinstance(classification.get("reply_proposal"), dict)
        delivery_state = (delivery_actions[0]["delivery_state"] if len(delivery_actions) == 1 else
                          "MULTIPLE_ACTIONS" if delivery_actions else
                          "UNKNOWN" if not actions_known else "UNSENT_PROPOSAL" if proposal else "NO_PROPOSAL")
        record.update(source_key=source_key, source=source.get("source"), source_id=source.get("source_id"),
                      identity=identity, ai_label=label, state="CLOSED" if closed else state,
                      missing_facts=missing, holds=holds, observations=observations,
                      next_action="Keep recipient outreach closed" if closed else
                                  "Revalidate source bindings before conversion review" if advance else
                                  "Investigate missing facts and review the private reply decision",
                      review_at=review_at.isoformat() if review_at else None,
                      unsent_draft_available=proposal and not delivery_actions,
                      delivery_state=delivery_state, delivery_actions=delivery_actions,
                      can_send=False, can_advance_conversion=advance, verified_business_state=None)
        delivery_next = {"QUEUED": "Await guarded reply action validation",
                         "BLOCKED": "Review saved reply action gates before execution",
                         "ACKNOWLEDGED_UNCONFIRMED": "Reconcile the existing claim; acknowledgment is not delivery",
                         "UNCERTAIN": "Reconcile the existing claim; do not repeat the send",
                         "CONFIRMED_SENT": "Retain the confirmed delivery receipt; do not repeat the send",
                         "CONFIRMED_NOT_SENT": "Review the terminal outcome; automatic resend is unsupported"}
        if not closed and delivery_state in delivery_next:
            record["next_action"] = delivery_next[delivery_state]
        record["champion_deliveries"] = champions.get(record.get("task_id"), [])
    return summary


def notification_status(database, event_key):
    records = source_records.rows(database, "notification_outbox") or []
    row = next((record for record in records if record["event_key"] == event_key), None)
    if not row:
        return {"state": "NOT_ENQUEUED", "human_read_verified": False}
    delivery = source_records.decoded(row.get("delivery"))
    return {"state": row["state"], "attempts": row.get("attempts"), "next_retry": row.get("next_at"),
            "last_error": row.get("last_error"), "updated_at": row.get("updated_at"),
            "send_error": row.get("send_error"), "reconcile_error": row.get("reconcile_error"),
            "telegram_message_id": delivery.get("message_id"),
            "telegram_api_receipt_recorded": delivery.get("provider") == "telegram" and bool(delivery.get("message_id")),
            "human_read_verified": False}


def research_status(database, config, now):
    # The research day as daily_schedule labels it (it starts at research_hour, 9:00 since
    # 2026-09-30), so a report before 9:00 shows the research that is still in force.
    local = source_records.instant(now).astimezone(ZoneInfo(config.get("timezone", "America/Los_Angeles")))
    start = local.replace(hour=config.get("research_hour", 0), minute=0, second=0, microsecond=0)
    day = (start.date() - dt.timedelta(days=1 if local < start else 0)).isoformat()
    records = source_records.rows(database, "research_days", ("day", "state", "attempts", "attempt_id", "next_retry_at", "completed_at"))
    status = {"day": day, "state": "UNKNOWN", "basis": "SAVED", "source": "research_days",
              "checked_by_this_report": False, "attempts": None, "attempt_id": None,
              "next_retry_at": None, "completed_at": None,
              "latest_attempt": {"state": "UNKNOWN", "basis": "SAVED", "source": "research", "record": None}}
    if records is None:
        status["reason"] = "TABLE_ABSENT_OR_SCHEMA_UNAVAILABLE"
        return status
    saved = next((row for row in records if row["day"] == day), None)
    if saved is None:
        status["reason"] = "NO_SAVED_DAY"
        return status
    status.update({field: saved[field] for field in ("state", "attempts", "attempt_id", "next_retry_at", "completed_at")})
    attempts = source_records.rows(database, "research", ("id", "started_at", "state", "result"))
    attempt = next((row for row in attempts or [] if row["id"] == saved["attempt_id"]), None)
    if attempt is not None:
        status["latest_attempt"].update(state="KNOWN_SAVED", record={
            "id": attempt["id"], "started_at": attempt["started_at"], "state": attempt["state"],
            "result": source_records.decoded(attempt["result"]), "saved_result": attempt["result"]})
    else:
        status["latest_attempt"]["reason"] = "REFERENCED_ATTEMPT_UNAVAILABLE"
    return status


REOPEN_EVENT = "APPLICATION_REOPENED"
CLAIMS_EXHAUSTED_EVENT = "APPLICATION_RECOVERY_CLAIMS_EXHAUSTED"
HOLD_REVIEW_EVENT = "APPLICATION_RECOVERY_HOLD_REVIEWED"
HOLD_REVIEW_FAILED_EVENT = "APPLICATION_RECOVERY_HOLD_REVIEW_FAILED"


def authorized_reopens(database, now):
    """Current, truthful status of every explicitly authorized reopen of an abandoned job.

    Reads only saved rows. A reopen never erases its abandonment: the original
    `APPLICATION_ABANDONED` record stays in the ledger and keeps its own reporting
    record, and this section states the current saved state instead of implying the
    job is either still terminal or already successful. Missing grants, missing job
    rows and unparseable expiry stay explicitly UNKNOWN; nothing is inferred.
    """
    events = source_records.rows(database, "application_events", ("id", "job_key", "at", "event", "detail"))
    if events is None:
        return {"state": "UNKNOWN_TABLE_ABSENT", "basis": "SAVED", "records": None, "counts": None}
    current = source_records.instant(now)
    jobs = source_records.rows(database, "application_jobs", ("job_key", "state"))
    lookup = {row["job_key"]: row for row in jobs or []}
    grants = source_records.rows(database, "application_reopen_grants", ("authorization_id",))
    consumed = source_records.rows(database, "application_reopen_grant_jobs", ("authorization_id", "job_key"))
    recorded = {row["authorization_id"] for row in grants or []}
    granted = {(row["authorization_id"], row["job_key"]) for row in consumed or []}
    ordered = sorted(events, key=lambda row: row["id"])
    records = []
    for event in ordered:
        if event["event"] != REOPEN_EVENT:
            continue
        at = source_records.instant(event["at"])
        if at is not None and current is not None and at > current:
            continue
        detail = source_records.decoded(event["detail"])
        detail = detail if isinstance(detail, dict) else {}
        allowance = detail.get("allowance")
        allowance = allowance if isinstance(allowance, dict) else {}
        identifier = detail.get("authorization_id") if isinstance(detail.get("authorization_id"), str) else None
        # Reopen grants never expire (owner, 2026-09-28: application recovery has no deadline).
        state = "AUTHORIZATION_ACTIVE"
        basis = "application_reopen_grants" if identifier in recorded else (
            "UNKNOWN_GRANT_LEDGER_ABSENT" if grants is None else "event_detail")
        history = [{"event_id": row["id"], "at": row["at"],
                    "reason": (source_records.decoded(row["detail"]) or {}).get("reason")
                    if isinstance(source_records.decoded(row["detail"]), dict) else None}
                   for row in ordered
                   if row["job_key"] == event["job_key"] and row["id"] < event["id"]
                   and row["event"] == "APPLICATION_ABANDONED"]
        later = [{"event_id": row["id"], "at": row["at"], "event": row["event"]}
                 for row in ordered
                 if row["job_key"] == event["job_key"] and row["id"] > event["id"]
                 and row["event"] in {"APPLICATION_ABANDONED", REOPEN_EVENT}]
        job = lookup.get(event["job_key"])
        saved_state = job["state"] if job else None
        terminal = None if job is None else saved_state in applications.FINAL
        record = {
            "job_key": event["job_key"], "reopen_event_id": event["id"], "reopened_at": event["at"],
            "authorization_id": identifier, "authorized_by": detail.get("authorized_by"),
            "authorization_state": state,
            "authorization_basis": basis,
            "grant_recorded": None if consumed is None else bool(identifier and (identifier, event["job_key"]) in granted),
            "original_abandonment": detail.get("original_abandonment") if isinstance(detail.get("original_abandonment"), dict) else None,
            "original_abandonment_records": history,
            "abandonment_history_preserved": bool(history),
            "later_terminal_or_reopen_events": later,
            "current_state": saved_state, "current_state_basis": "application_jobs" if job else "UNKNOWN_JOB_ROW_ABSENT",
            "currently_terminal": terminal, "verified_submission": False,
        }
        if job is None:
            record["next_action"] = ("Saved job row unavailable; the current state of this reopened job is UNKNOWN "
                                     "and must not be reported as open, terminal or submitted")
        elif terminal:
            record["next_action"] = ("Reopened under recorded user authorization, then reached saved terminal state "
                                     + str(saved_state) + "; retain both the original abandonment and this outcome")
        else:
            record["next_action"] = ("Reopened by explicit recorded user authorization; the original abandonment stays "
                                     "in history and this reopen is not an application submission")
        records.append(record)
    counts = {"reopens": len(records),
              "authorization_active": sum(row["authorization_state"] == "AUTHORIZATION_ACTIVE" for row in records),
              "currently_terminal": sum(row["currently_terminal"] is True for row in records),
              "current_state_unknown": sum(row["currently_terminal"] is None for row in records)}
    return {"state": "KNOWN_SAVED" if jobs is not None else "PARTIAL_SAVED", "basis":
            "Saved reopen events and, when present, the durable reopen grant ledger. A reopen is not a submission "
            "and never removes the original abandonment; expired grants and later terminal states are reported as they are.",
            "checked_by_this_report": False, "records": records, "counts": counts}


def bounded_recovery(database, config, now):
    current = source_records.instant(now)
    timezone = config.get("timezone", "America/Los_Angeles")
    local = current.astimezone(ZoneInfo(timezone))
    daily_start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(dt.timezone.utc)
    weekly_start = current - dt.timedelta(days=7)
    sources = {
        "application_events": source_records.rows(database, "application_events", ("id", "job_key", "at", "event", "detail")),
        "application_jobs": source_records.rows(database, "application_jobs", ("job_key", "state")),
        "application_attempts": source_records.rows(database, "application_attempts", ("attempt_id", "job_key", "state")),
        "workflow_tasks": source_records.rows(database, "workflow_tasks", ("task_id", "worker", "status")),
        "research": source_records.rows(database, "research", ("id", "started_at", "result")),
    }
    tasks = {row["task_id"]: row for row in sources["workflow_tasks"] or []}
    jobs = {row["job_key"]: row for row in sources["application_jobs"] or []}
    events = [row for row in sources["application_events"] or []
              if source_records.instant(row["at"]) is None or source_records.instant(row["at"]) <= current]
    unresolved = {}
    for attempt in sources["application_attempts"] or []:
        if attempt["state"] in {"SUBMITTING", "UNCERTAIN", "UNKNOWN", "RECONCILE"}:
            unresolved.setdefault(attempt["job_key"], []).append(attempt["attempt_id"])
    for job in jobs.values():
        if job["state"] in {"SUBMITTING", "RECONCILE"} or job.get("blocker") == "SUBMISSION_OUTCOME_UNKNOWN":
            unresolved.setdefault(job["job_key"], [])
    records = []
    categories = {
        "ABANDONED": "abandoned", "APPLICATION_ABANDONED": "abandoned",
        "BLOCKED": "failures", "EXHAUSTED": "failures", "RETRY_WAIT": "failures",
        "APPLICATION_RECOVERY_STARTED": "recovery_started", "APPLICATION_RECOVERY_PREPARED": "recovery_pending",
        "APPLICATION_RECOVERY_INVESTIGATED": "recovery_attempts",
        "APPLICATION_RECOVERY_RESOLUTION_REJECTED": "failures", "APPLICATION_RECOVERY_RESOLVED": "recovered",
        "APPLICATION_RECOVERY_DECIDED": "coordinator_decision", "APPLICATION_RECOVERY_DECISION": "coordinator_decision",
        "APPLICATION_RECOVERY_DECISION_FAILED": "failures",
        # The bounded-exhaustion park is a reviewable hold, never a terminal abandonment,
        # so it joins the existing blocked-transition category and never "abandoned".
        CLAIMS_EXHAUSTED_EVENT: "failures", HOLD_REVIEW_FAILED_EVENT: "failures",
        # An AI review of that park is a coordinator judgment, like any other decision.
        # When it abandons, the separate APPLICATION_ABANDONED event still supplies the
        # terminal record and its count; this record only explains the review itself.
        HOLD_REVIEW_EVENT: "coordinator_decision",
        REOPEN_EVENT: "reopened",
    }
    reopens = authorized_reopens(database, now)
    reopened = {row["reopen_event_id"]: row for row in reopens["records"] or []}
    lifecycles = {}
    abandoned_events = set()
    for event in events:
        if event["event"] == "APPLICATION_ABANDONED":
            abandoned_events.add((event["job_key"], source_records.instant(event["at"])))
        if event["event"] == "APPLICATION_RECOVERY_STARTED":
            detail = source_records.decoded(event["detail"])
            if isinstance(detail, dict):
                lifecycles.setdefault(event["job_key"], detail)
    for event in events:
        category = categories.get(event["event"])
        if category is None or (event["event"] == "ABANDONED"
                                and (event["job_key"], source_records.instant(event["at"])) in abandoned_events):
            continue
        detail = source_records.decoded(event["detail"])
        detail = detail if isinstance(detail, dict) else {}
        decision = detail.get("decision")
        decision = decision if isinstance(decision, dict) else {}
        job = jobs.get(event["job_key"], {})
        task = tasks.get(detail.get("task_id"), {})
        lifecycle = lifecycles.get(event["job_key"], {})
        uncertain = event["job_key"] in unresolved
        records.append({
            "domain": "application", "task_id": detail.get("task_id") or event["job_key"],
            "job_key": event["job_key"], "category": category,
            "state": "UNRESOLVED_SUBMISSION" if uncertain else event["event"],
            "at": event["at"], "time_basis": "application_events.at",
            "reason": detail.get("reason") or decision.get("reason") or detail.get("code") or detail.get("blocker") or task.get("blocker") or job.get("blocker"),
            "attempts": detail.get("attempts", detail.get("attempt", task.get("attempts", job.get("attempts")))),
            "attempt_basis": "recovery" if "attempts" in detail or "attempt" in detail else
                             "workflow" if task else "application_job",
            "max_attempts": detail.get("max_attempts", lifecycle.get("max_attempts", task.get("max_attempts"))),
            "deadline": detail.get("deadline", lifecycle.get("deadline", task.get("deadline"))),
            "next_action": "Reconcile the existing submission claim before any retry" if uncertain else
                           detail.get("next_action") or decision.get("instructions") or task.get("next_action") or job.get("next_action") or
                           ("Resume ordinary application checks; this resolution is not a submission" if category == "recovered" else None),
            "evidence": detail, "source": "application_events", "source_id": event["id"],
            "workflow_evidence": source_records.decoded(task.get("evidence"), []),
            "unresolved_submission": uncertain,
        })
        if event["event"] == HOLD_REVIEW_EVENT:
            budget = detail.get("budget")
            budget = budget if isinstance(budget, dict) else {}
            outcome = detail.get("outcome") if isinstance(detail.get("outcome"), str) else None
            records[-1].update(
                reason="AI hold review " + str(detail.get("review", "?")) + "/"
                       + str(budget.get("max_hold_reviews", "?")) + " of "
                       + str(detail.get("hold_blocker") or "UNKNOWN_HOLD") + ": "
                       + (outcome or "UNKNOWN_OUTCOME") + "; " + str(detail.get("reason") or decision.get("reason") or "no saved reason"),
                hold_review={"review": detail.get("review"), "max_hold_reviews": budget.get("max_hold_reviews"),
                             "outcome": outcome, "terminal": detail.get("terminal"),
                             "hold_blocker": detail.get("hold_blocker"),
                             "further_investigation_claims_granted": budget.get("further_investigation_claims_granted"),
                             "deadline_extended": detail.get("deadline_extended", budget.get("deadline_extended")),
                             "submission_status": detail.get("submission_status"),
                             "basis": "Saved review event; a resumed hold is one ordinary application attempt, not a submission"})
        if category == "reopened":
            status = reopened.get(event["id"], {})
            records[-1].update(
                reason="Authorized reopen " + str(status.get("authorization_id") or "UNKNOWN_AUTHORIZATION")
                       + "; authorization " + str(status.get("authorization_state") or "UNKNOWN")
                       + "; current saved state " + str(status.get("current_state") or "UNKNOWN")
                       + "; the original abandonment record is retained",
                next_action=status.get("next_action"), attempts=status.get("original_abandonment", {}).get("attempts")
                if isinstance(status.get("original_abandonment"), dict) else None,
                authorized_reopen=status)
        result = detail.get("result")
        if category == "recovery_attempts" and isinstance(result, dict) and result.get("status") == "FAILED":
            records.append(dict(records[-1], category="failures"))
    for record in records:
        later = [row for row in reopened.values() if row["job_key"] == record["job_key"]
                 and row["reopen_event_id"] > record["source_id"]] if record["category"] == "abandoned" else []
        if later:
            record["superseded_by_authorized_reopen"] = later[-1]
            record["next_action"] = ("This abandonment is retained as history; the job was later reopened under "
                                     "recorded user authorization " + str(later[-1].get("authorization_id") or "UNKNOWN")
                                     + " (" + str(later[-1].get("authorization_state")) + ", current saved state "
                                     + str(later[-1].get("current_state") or "UNKNOWN") + ")")
    seen = {}
    research_unknown = []
    for saved in sorted(sources["research"] or [], key=lambda row: (source_records.instant(row["started_at"]) or current, row["id"])):
        if source_records.instant(saved["started_at"]) is not None and source_records.instant(saved["started_at"]) > current:
            continue
        result = source_records.decoded(saved["result"])
        industries = result.get("industries") if isinstance(result, dict) else None
        if not isinstance(industries, list):
            research_unknown.append(saved["started_at"])
            continue
        for industry in industries:
            if not isinstance(industry, dict) or not isinstance(industry.get("industry"), str):
                research_unknown.append(saved["started_at"])
                continue
            identity = str(result.get("day") or saved["started_at"][:10]) + ":" + str(industry.get("industry", "UNKNOWN"))
            failures = industry.get("failures") or []
            decisions = industry.get("recovery_decisions") or []
            if not isinstance(failures, list) or not isinstance(decisions, list):
                research_unknown.append(saved["started_at"])
                continue
            base = {"domain": "research", "task_id": identity, "state": industry.get("state"),
                    "attempts": len(failures), "max_attempts": None, "deadline": None,
                    "unresolved_submission": False, "source": "research", "source_id": saved["id"]}
            observations = []
            for failure in failures:
                if isinstance(failure, dict):
                    observations.append(("failures", failure.get("observed_at"), failure.get("diagnostic"), failure,
                                         "Investigate saved research failure within its original bounds", "failure.observed_at"))
            for decision in decisions:
                if isinstance(decision, dict):
                    decision_result = decision.get("decision")
                    observations.append(("recovery_attempts", decision.get("observed_at"),
                                         decision_result.get("reason") if isinstance(decision_result, dict) else None, decision,
                                         "Review the saved bounded recovery decision", "decision.observed_at"))
            if industry.get("recovery_state") == "ABANDONED":
                observations.append(("abandoned", industry.get("abandoned_at") or saved["started_at"],
                                     industry.get("abandonment_reason"), industry,
                                     "No automatic retry; retain evidence for review",
                                     "abandoned_at" if industry.get("abandoned_at") else "research.started_at proxy; terminal time unavailable"))
            elif industry.get("state") == "COMPLETE" and failures:
                observations.append(("recovered", industry.get("completed_at") or saved["started_at"],
                                     "Saved research completed after failure", industry,
                                     "Continue normal checks; research completion is not an application submission",
                                     "completed_at" if industry.get("completed_at") else "research.started_at proxy; completion time unavailable"))
            for category, at, reason, evidence, next_action, time_basis in observations:
                key = (identity, category, at if category in {"failures", "recovery_attempts"} else None,
                       evidence.get("attempt") if category == "failures" else
                       (evidence.get("packet") or {}).get("decision_number") if category == "recovery_attempts" else None)
                if key in seen:
                    records[seen[key]]["evidence"] = evidence
                    records[seen[key]]["reason"] = reason
                    continue
                seen[key] = len(records)
                records.append(dict(base, category=category, at=at, reason=reason, evidence=evidence,
                                    state="ABANDONED" if category == "abandoned" else base["state"],
                                    next_action=next_action, time_basis=time_basis))
    metrics = ("abandoned", "failures", "recovery_attempts", "recovered")

    def window(start):
        counts = {}
        for domain, source in (("application", "application_events"), ("research", "research")):
            known = sources[source] is not None and not (domain == "research" and any(
                source_records.instant(at) is None or start <= source_records.instant(at) <= current for at in research_unknown))
            selected = [row for row in records if row["domain"] == domain
                        and source_records.instant(row["at"]) is not None
                        and start <= source_records.instant(row["at"]) <= current
                        and not (row["category"] in {"abandoned", "recovered"} and row["unresolved_submission"])]
            counts[domain] = {}
            for metric in metrics:
                matching = [row for row in selected if row["category"] == metric]
                observed = len(matching) if metric == "recovery_attempts" else len({row["task_id"] for row in matching})
                dated = not any(row["domain"] == domain and row["category"] == metric and source_records.instant(row["at"]) is None
                                for row in records)
                counts[domain][metric] = observed if known and dated else None
        return {"start_inclusive": start.isoformat(), "end_inclusive": current.isoformat(), "counts": counts}

    def reopen_window(start):
        if reopens["records"] is None:
            return None
        return len({row["job_key"] for row in records if row["domain"] == "application" and row["category"] == "reopened"
                    and source_records.instant(row["at"]) is not None and start <= source_records.instant(row["at"]) <= current})

    reopens = dict(reopens, daily=reopen_window(daily_start), rolling_week=reopen_window(weekly_start),
                   window_basis="Distinct application jobs whose saved reopen event falls inside the window; "
                                "counted separately so existing recovery counts keep their meaning")
    visible = [row for row in records if source_records.instant(row["at"]) is None
               or weekly_start <= source_records.instant(row["at"]) <= current]
    return {"state": "KNOWN_SAVED" if all(value is not None for value in sources.values()) and not research_unknown else "PARTIAL_SAVED",
            "basis": "Distinct tasks per category/window, except recovery_attempts counts saved investigations/decisions. Failures include blocked transitions. Recovery is not submission success.",
            "checked_by_this_report": False, "timezone": timezone,
            "sources": {name: "KNOWN_SAVED" if value is not None else "UNKNOWN_TABLE_OR_SCHEMA_ABSENT" for name, value in sources.items()},
            "research_records_without_industry_evidence": len(research_unknown),
            "daily": window(daily_start), "rolling_week": window(weekly_start),
            "authorized_reopens": reopens,
            "records": sorted(visible, key=lambda row: source_records.instant(row["at"]) or current, reverse=True),
            "undated_records": sum(source_records.instant(row["at"]) is None for row in records),
            "unresolved_submissions": {
                "state": "KNOWN_SAVED" if sources["application_attempts"] is not None and sources["application_jobs"] is not None else "PARTIAL_SAVED",
                "count": len(unresolved) if sources["application_attempts"] is not None and sources["application_jobs"] is not None else None,
                "records": [dict(job_key=key, attempt_ids=attempt_ids, state="UNRESOLVED_SUBMISSION",
                                 saved_job_state=jobs.get(key, {}).get("state"),
                                 reason=jobs.get(key, {}).get("blocker"),
                                 next_action="Reconcile the existing submission claim before any retry")
                            for key, attempt_ids in sorted(unresolved.items())]}}


def recovery_summary(payload):
    recovery = payload.get("bounded_recovery")
    if not recovery:
        return []
    lines = []
    for field, label in (("daily", "Today"), ("rolling_week", "Rolling 7 days")):
        counts = recovery[field]["counts"]
        parts = []
        for domain in ("application", "research"):
            values = counts[domain]
            parts.append(domain + " " + ", ".join(name + "=" + (str(values[name]) if values[name] is not None else "unknown")
                                                   for name in ("abandoned", "failures", "recovery_attempts", "recovered")))
        lines.append(label + " saved recovery: " + "; ".join(parts))
    unresolved = recovery["unresolved_submissions"]["count"]
    lines.append(f"Unresolved submission claims: {unresolved if unresolved is not None else 'unknown'}; reconcile before retry. Abandonment/recovery is not submission success.")
    reopens = (recovery.get("authorized_reopens") or payload.get("authorized_reopens") or {})
    if reopens.get("records"):
        counts = reopens.get("counts") or {}
        windows = "; ".join(label + " " + str(reopens.get(field) if reopens.get(field) is not None else "unknown")
                            for field, label in (("daily", "today"), ("rolling_week", "rolling 7 days")))
        lines.append(f"Authorized reopens of abandoned jobs: {counts.get('reopens')} saved ({windows}); "
                     f"authorization active={counts.get('authorization_active')}; now terminal again={counts.get('currently_terminal')}, "
                     f"current state unknown={counts.get('current_state_unknown')}. "
                     f"Each original abandonment record is retained; a reopen is not a submission.")
        for row in reopens["records"][:2]:
            short = lambda value, limit: " ".join(str(value if value is not None else "unknown").split())[:limit]
            lines.append(f"Reopen {short(row['job_key'], 60)}: {row['authorization_state']}; authorized_by={short(row['authorized_by'], 50)};"
                         f" current saved state={row['current_state'] or 'UNKNOWN'};"
                         f" abandonment retained={row['abandonment_history_preserved']}; next={short(row['next_action'], 120)}")
    candidates = sorted(recovery["records"], key=lambda row: row["category"] != "abandoned")
    for row in candidates[:2]:
        short = lambda value, limit: " ".join(str(value if value is not None else "unknown").split())[:limit]
        lines.append(f"{row['domain']} {short(row['task_id'], 60)}: {row['state']}; reason={short(row['reason'], 110)}; attempts={row['attempts']}; evidence={row['source']}:{short(row['source_id'], 45)}; next={short(row['next_action'], 100)}")
    return lines


def snapshot(database, config, now):
    latest = None
    runs = sorted(source_records.rows(database, "runs") or [], key=lambda row: row.get("started_at", ""), reverse=True)
    for run in runs:
        result = source_records.decoded(run.get("result"))
        observation = result.get("observation", {}) if isinstance(result, dict) else {}
        if observation.get("observed_at") and "local-check" not in run.get("slot", ""):
            latest = observation
            break
    observed_at = (latest or {}).get("observed_at")
    parsed = source_records.instant(observed_at)
    age = (source_records.instant(now) - parsed).total_seconds() if parsed else None
    provider = {"observed_at": observed_at, "monitor_run_observed_at": observed_at, "errors_observed_at": observed_at,
                "basis": "SAVED", "checked_by_this_report": False,
                "age_seconds": age, "freshness": "UNKNOWN" if age is None or age < 0 else "RECENT_SAVED" if age <= config.get("report_provider_max_age_seconds", 3600) else "STALE_SAVED",
                "errors": (latest or {}).get("errors"), "contacts": (latest or {}).get("contacts")}
    provider["engagement_checks"] = section(database, "engagement_cursors", ("source", "checked_at", "state", "generation"))
    checks = [row for row in provider["engagement_checks"]["records"] or [] if source_records.instant(row.get("checked_at"))]
    if checks:
        latest_check = max(checks, key=lambda row: source_records.instant(row["checked_at"]))
        if parsed is None or source_records.instant(latest_check["checked_at"]) > parsed:
            observed_at = latest_check["checked_at"]
            age = (source_records.instant(now) - source_records.instant(observed_at)).total_seconds()
            provider.update(observed_at=observed_at, age_seconds=age,
                            freshness="UNKNOWN" if age < 0 else "RECENT_SAVED" if age <= config.get("report_provider_max_age_seconds", 3600) else "STALE_SAVED",
                            latest_check_source=latest_check["source"], latest_check_state=latest_check["state"])
    jobs = source_records.rows(database, "application_jobs")
    counts = dict(Counter(row["state"] for row in jobs)) if jobs is not None else None
    verified = [row["job_key"] for row in jobs or [] if source_records.execution(database, {"table": "application_jobs", "key": row["job_key"]}, now)]
    blockers = section(database, "application_blockers", ("job_key", "code", "owner", "required_input", "attempts", "next_retry", "updated_at"))
    job_lookup = {row["job_key"]: row for row in jobs or []}
    for blocker in blockers["records"] or []:
        job = job_lookup.get(blocker.get("job_key"), {})
        blocker.update({"action_id": blocker.get("job_key"), "stage": job.get("state"),
                        "next_action": job.get("next_action"), "deadline": job.get("next_at")})
    payload = {"generated_at": now, "evidence_basis": "Saved durable observations; this report performs no provider read",
               "research_daily": research_status(database, config, now),
               "bounded_recovery": bounded_recovery(database, config, now),
               "provider_observation": provider, "last_provider_check": observed_at, "provider_errors": provider["errors"],
               "application_states": counts, "application_count_scope": "All saved jobs as of generated_at",
               "verified_submissions": len(verified) if jobs is not None else None, "verified_submission_keys": verified,
               "application_executor_gate": application_executor_gate(database, config, now), "guarded_outreach_gate": guarded_outreach_gate(config),
               "message_executor_gate": message_executor_gate(database, config),
               "applications": section(database, "application_jobs", ("job_key", "employer", "requisition", "state", "attempts", "next_at", "blocker", "next_action", "updated_at")),
               "outstanding_actions": blockers["records"], "blockers": blockers,
               "referrals": section(database, "referrals", ("contact_id", "state", "statement", "next_action", "review_at", "updated_at")),
               "outreach_actions": section(database, "outreach_attempts", ("identity", "campaign_id", "state", "claimed_at")),
               "message_actions": section(database, "outreach_actions", ("action_id", "identity", "slot", "state", "claimed_at")),
               "outreach_closures": section(database, "engagement_closures", ("identity", "closed_at", "reason", "source")),
               "drafts": section(database, "drafts", ("identity", "recipient", "company", "state")),
               "decisions": section(database, "user_decisions", ("id", "job_key", "field", "kind", "summary", "state", "expires_at", "registered", "message_id", "last_error")),
               "notifications": section(database, "notification_outbox", ("event_key", "state", "attempts", "next_at", "last_error", "updated_at")),
               "worker_tasks": section(database, "workflow_tasks", ("task_id", "worker", "action_id", "status", "attempts", "deadline", "blocker", "next_action", "owner")),
               "cloud_reporting_ownership_verified": config.get("reporting_owner_confirmed", False), "human_read_verified": False}
    payload["authorized_reopens"] = payload["bounded_recovery"]["authorized_reopens"]
    for closure in payload["outreach_closures"]["records"] or []:
        closure.update(recipient_state="CLOSED", workflow_state="CLOSED")
    if config.get("outreach_inactivity_days") is not None:
        payload["inactivity_closure"] = dict(section(database, "outreach_inactivity_checks",
            ("identity", "anchor_at", "attempts", "checked_at", "next_at", "state")),
            days=config["outreach_inactivity_days"], basis="SAVED_ACTIVITY_REVIEWS")
    for row in payload["notifications"]["records"] or []:
        row.update(notification_status(database, row["event_key"]))
    payload["replies"] = section(database, "engagement_events", ("source", "source_id", "identity", "kind", "occurred_at", "observed_at", "inference"))
    if payload["replies"]["records"] is not None:
        payload["replies"]["records"] = [dict(row, inference=source_records.decoded(row.get("inference"))) for row in payload["replies"]["records"] if row.get("kind") in {"inbound", "opt_out"}]
    payload["contact_sentiment"] = {"basis": "RECORDED_INFERENCE_NOT_EMPLOYER_FACT", "records": payload["replies"]["records"], "state": payload["replies"]["state"]}
    payload["reply_decisions"] = reply_decisions(database, now)
    # Reply reviews held because the sender could not be tied to a person (review, 2026-10-01);
    # reported here and in `status`, never on Telegram.
    import reply_worker
    payload["held_reply_senders"] = reply_worker.held_senders(database)
    # Notes the reply AI left for the owner with its flag_owner tool (owner, 2026-10-06), last day.
    import reply_tools
    since = (dt.datetime.fromisoformat(now) - dt.timedelta(days=1)).isoformat() if isinstance(now, str) else ""
    payload["reply_owner_flags"] = reply_tools.owner_flags(database, since)
    # What happened in the last 24 hours, for the short Telegram summary (owner, 2026-10-06).
    payload["outcomes_24h"] = outcomes(database, since, now, config.get("timezone", "America/Los_Angeles")) if since else None
    # Applications run as their own job (owner, 2026-10-02); its latest runs, from saved records.
    payload["applications_job"] = applications_job(database)
    payload["champion_delivery"] = payload["reply_decisions"]["champion_delivery"]
    safe_actions = {row["task_id"]: row["next_action"] for row in payload["reply_decisions"]["records"] or []}
    source_actions = {"pipeline-routing:" + row["source_key"]: row["next_action"]
                      for row in payload["reply_decisions"]["records"] or [] if row.get("source_key")}
    for task in source_records.rows(database, "workflow_tasks", ("task_id", "parent_run_id")) or []:
        if task.get("parent_run_id") in source_actions:
            safe_actions[task["task_id"]] = source_actions[task["parent_run_id"]]
    for task in payload["worker_tasks"]["records"] or []:
        if task.get("task_id") in safe_actions:
            task["next_action"] = safe_actions[task["task_id"]]
    payload["interview_stages"] = section(database, "conversion_interviews", ("job_key", "stage_id", "stage", "evidence_id", "observed_at", "scheduled_at", "debrief_evidence_id", "timeline_evidence_id", "followup_at"))
    payload["baseline_priorities"] = section(database, "conversion_baselines", ("baseline_id", "company", "record"))
    local_day = source_records.instant(now).astimezone(ZoneInfo(config.get("timezone", "America/Los_Angeles"))).date().isoformat()
    if payload["baseline_priorities"]["records"] is not None:
        priorities = []
        for row in payload["baseline_priorities"]["records"]:
            saved = source_records.decoded(row.get("record"))
            date = saved.get("historical_date")
            priorities.append({"baseline_id": row["baseline_id"], "company": row["company"], "date": date,
                               "scheduled_at": saved.get("historical_scheduled_at"), "stage": saved.get("stage"),
                               "confirmation_freshness": "UNKNOWN", "basis": "SAVED_BASELINE", "attributed": False,
                               "date_status": "UPCOMING_SAVED_DATE" if date and date >= local_day else "PAST_OR_UNKNOWN_REQUIRES_RECONCILIATION",
                               "source": saved.get("source"), "source_sha256": saved.get("source_sha256"),
                               "saved_evidence_read_at": saved.get("saved_evidence_read_at"), "next_action": saved.get("next_action")})
        payload["baseline_priorities"]["records"] = priorities
    payload["interview_outcomes"] = section(database, "conversion_outcomes", ("action_id", "job_key", "kind", "at", "evidence"))
    payload["conversion_tasks"] = section(database, "conversion_tasks", ("task_id", "job_key", "kind", "payload", "status"))
    champions = {record["task_id"]: record for record in payload["champion_delivery"]["records"]}
    for task in payload["conversion_tasks"]["records"] or []:
        if task["kind"] == "GUARDED_CHAMPION_DELIVERY":
            champion = champions.get(task["task_id"], {})
            task["payload"] = json.dumps({
                "task_id": task["task_id"], "kind": task["kind"], "status": task["status"],
                "deadline": champion.get("deadline"),
                "next_action": champion.get("next_action", "Review the saved private champion task"),
                "inputs": {field: champion.get(field) for field in (
                    "summary_id", "contact_id", "action_id", "delivery_state", "action_state",
                    "receipt_reference", "deadline", "hold_deadline", "next_action", "read_retries")}})
    payload["followups_due"] = followups(database, now)
    # Conversations the inbox reader cannot decode hold their person from outreach until they do
    # (owner, 2026-09-30); some never will (InMail, attachments, group chats), so they are shown.
    skipped = section(database, "engagement_skipped_pages",
                      ("source", "cursor", "next_cursor", "failures", "error_type", "error", "skipped_at"))
    if skipped["records"]:
        skipped["records"] = sorted(skipped["records"], key=lambda row: row.get("skipped_at") or "", reverse=True)[:10]
    payload["inbox_skipped_pages"] = dict(skipped, basis="Deep inbox pages stepped over after 3 failed listing reads in a row; read again when the scan returns")
    payload["inbox_undecodable"] = dict(section(database, "engagement_decode_holds",
        ("conversation_id", "identity", "error_type", "reason", "first_seen", "last_seen", "failures")),
        basis="Held from first messages, follow-ups and campaigns until the conversation decodes; not a reply")
    payload["executed_actions"] = {"verified_applications": verified,
                                   "verified_outreach": [row["identity"] for row in source_records.rows(database, "outreach_attempts") or []
                                                         if source_records.execution(database, {"table": "outreach_attempts", "key": row["identity"]}, now)],
                                   "verified_message_actions": [row["action_id"] for row in source_records.rows(database, "outreach_actions") or []
                                                                if source_records.execution(database, {"table": "outreach_actions", "key": row["action_id"]}, now)],
                                   "basis": "Independent stored receipt required; drafts, claims and acknowledgments are excluded"}
    return payload


def followups(database, now):
    records = source_records.rows(database, "engagement_events", ("identity", "payload"))
    if records is None or not source_records.columns(database, "engagement_stops"):
        return {"state": "UNKNOWN_ADAPTER_OR_TABLE_ABSENT", "basis": "SAVED", "records": None}
    import engagement
    plans = []
    seen = set()
    for row in records:
        if row["identity"] in seen:
            continue
        seen.add(row["identity"])
        event = source_records.decoded(row["payload"])
        try:
            plan = engagement.next_action(database, event.get("profile", row["identity"]), now,
                                          recipient=event.get("recipient", ""), campaign_id=event.get("campaign_id"))
        except (TypeError, ValueError, KeyError):
            plan = {"state": "UNKNOWN", "blocker": "INVALID_SAVED_EVENT"}
        plans.append(dict(plan, identity=row["identity"], executed=False))
    return {"state": "KNOWN_SAVED", "basis": "Planning from saved events; dispatcher must revalidate history", "records": plans,
            "due_count": sum(plan.get("state") == "DUE" for plan in plans)}


def telegram_lines(payload):
    executor = payload["application_executor_gate"]
    outreach = payload["guarded_outreach_gate"]
    messages = payload.get("message_executor_gate") or {}
    requests = executor["desktop_requests"]
    desktop = (f"pending={requests['pending']}, claimed={requests['claimed']}, expired={requests['expired']}, unknown={requests['unknown']}, expired active={requests['expired_active']}"
               if requests["table_exists"] else "desktop_requests table absent")
    research = payload.get("research_daily", {})
    attempt = research.get("latest_attempt", {}).get("record") or {}
    result = attempt.get("result") or {}
    detail = (result.get("error") or result.get("blocker") or result.get("summary", "")) if isinstance(result, dict) else ""
    detail = " ".join(str(detail).split())[:180]
    lines = [f"Job-search daily report {payload['day']}",
             *recovery_summary(payload),
             f"Saved research {research.get('day', payload['day'])}: {research.get('state', 'UNKNOWN')}; attempts={research.get('attempts')}; next_retry={research.get('next_retry_at')}; completed_at={research.get('completed_at')}. No new research verification.",
             f"Saved latest research attempt: {attempt.get('id', 'UNKNOWN')}; state={attempt.get('state', 'UNKNOWN')}; started_at={attempt.get('started_at', 'UNKNOWN')}; {detail}",
             f"Saved provider check: {payload['last_provider_check'] or 'unknown'} ({payload['provider_observation']['freshness']}); no new check by report.",
             f"Applications by state: {json.dumps(payload['application_states'])}; independently verified submissions: {payload['verified_submissions']}",
             f"Application executor: {executor['state']}; adapter={executor['adapter']}; desktop configured={executor['desktop_adapter_configured']}",
             f"Scheduled-host execution: {executor['scheduled_host_execution']}; {desktop}",
             f"Desktop host: {executor['desktop_host']['code']}; can_submit={executor['desktop_host']['can_submit']}",
             f"Outreach: {outreach['state']}; drafts/prepared campaigns are not execution or delivery. Acknowledgment is not receipt.",
             f"Message executor: {messages.get('state', 'UNKNOWN_SAVED_PAYLOAD')}; connection enabled={messages.get('connection_enabled', 'unknown')}; scope={json.dumps(messages.get('supported_purposes'))}. No live check performed. Only independent receipts confirm delivery; ACK/drafts do not. CONFIRMED_NOT_SENT automatic resends unsupported."]
    for flag in payload.get("reply_owner_flags") or []:
        lines.insert(1, f"For you (reply AI, {flag.get('flagged_at')}): {flag.get('reason')}")
    champions = payload.get("champion_delivery") or {}
    if champions.get("records"):
        lines.append(f"Champion delivery: {json.dumps(champions.get('delivery_counts', {}))}. Only independent receipts confirm delivery; this does not verify referral submission.")
        for champion in champions["records"][:2]:
            lines.append(f"Champion {champion.get('summary_id')}: {champion['delivery_state']}; hold deadline={champion.get('hold_deadline')}; task deadline={champion.get('deadline')}; next={champion['next_action']}.")
            for retry in champion.get("read_retries", []):
                lines.append(f"Champion read: {retry['phase']} {retry['state']}; attempts={retry['attempts']}; next={retry['next_at']}; error={retry['last_error']}.")
    for field, label in (("read_retries", "Message read"), ("draft_investigations", "Draft investigation")):
        records = (messages.get(field) or {}).get("records")
        lines.append(f"Saved {label.lower()} records: {len(records) if records is not None else 'unknown'}.")
        for row in (records or [])[:3]:
            lines.append(f"{label}: {row.get('action_id', row.get('task_id'))}; {row.get('phase', '')} {row.get('state', row.get('status'))}; blocker={row.get('last_error', row.get('blocker'))}; next={row.get('next_at')}; deadline={row.get('deadline')}.")
    closures = payload.get("outreach_closures", {}).get("records")
    lines.append(f"Permanently closed outreach identities: {len(closures) if closures is not None else 'unknown'}; recipient and workflow terminal. Delivery facts remain separate.")
    if "inactivity_closure" in payload:
        inactivity = payload["inactivity_closure"]
        states = dict(Counter(row["state"] for row in inactivity.get("records") or []))
        lines.append(f"Inactivity closure: {inactivity['days']} days without recipient engagement; saved reviews={json.dumps(states)}. Unknown activity is held for verification, not assumed inactive.")
    decisions = payload.get("reply_decisions", {}).get("records")
    labels = dict(Counter(row["ai_label"] for row in decisions or []))
    lines.append(f"Saved AI reply interpretations: {len(decisions) if decisions is not None else 'unknown'}; {json.dumps(labels)}; unsent drafts={sum(row['unsent_draft_available'] for row in decisions or [])}. Not confirmed referrals or delivery; no sending authority.")
    deliveries = dict(Counter(row.get("delivery_state", "UNKNOWN") for row in decisions or []))
    lines.append(f"Saved reply delivery states: {json.dumps(deliveries)}. Confirmed sent requires a claim-bound independent receipt; acknowledgments remain unconfirmed.")
    undecodable = (payload.get("inbox_undecodable") or {}).get("records")
    if undecodable:
        lines.append(f"Inbox conversations that cannot be read: {len(undecodable)}; each person is held from first messages, follow-ups and campaigns until it decodes (not a reply).")
        for hold in sorted(undecodable, key=lambda row: row.get("first_seen") or "")[:5]:
            lines.append(f"Unreadable: {hold.get('identity') or 'unknown person'}; {hold.get('error_type')}: {hold.get('reason')}; first seen {hold.get('first_seen')}; failed reads={hold.get('failures')}.")
    for field in ("replies", "referrals", "interview_outcomes", "followups_due", "decisions", "notifications"):
        data = payload[field]
        records = data["records"]
        states = dict(Counter(row.get("state", row.get("kind", "UNKNOWN")) for row in records or []))
        lines.append(f"{field}: {data['state']}; {len(records) if records is not None else 'unknown'} records; {json.dumps(states)}")
    for priority in payload["baseline_priorities"]["records"] or []:
        lines.append(f"Saved baseline: {priority['company']} {priority['scheduled_at'] or priority['date']}; freshness UNKNOWN; {priority['date_status']}; not a newly attributed interview.")
    lines.append(f"Blockers: {len(payload['outstanding_actions']) if payload['outstanding_actions'] is not None else 'unknown'}")
    for blocker in payload["outstanding_actions"] or []:
        lines.append(f"{blocker.get('job_key')}: {blocker.get('code')}; owner={blocker.get('owner')}; attempts={blocker.get('attempts')}; input={blocker.get('required_input')}; retry={blocker.get('next_retry')}; next={blocker.get('next_action')}")
    return lines


# The daily report goes out as several Telegram messages (owner, 2026-10-06: "fix daily
# report"). One message capped at Telegram's own 4096 never arrived: all 16 daily reports from
# 2026-09-17 to 2026-10-05 were exactly 4096 characters and the bridge never stored one (its
# reconcile read says NOT_FOUND), while every delivered notification was at most 1218. Each part
# is at most PART_LIMIT UTF-16 units, well under 4096, leaving room for any framing the bridge
# adds (the bridge's own limit is not known on this machine; the length cause is unproven). Lines are kept whole; only a single line longer than a part is cut across parts. Content
# past MAX_PARTS parts is cut with "[summary truncated]"; the full report stays in daily_reports.
# MAX_PARTS 20 holds the 2026-10-05 report whole (48,651 units in 16 parts, 42,327 of them in
# its 93 blocker lines) with room to grow.
def _short(value, limit=160):
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _local_day(stamp, timezone):
    """The owner's local calendar day of a timestamp (review round 28: a 21:33 PT submit read as
    the next day in UTC)."""
    try:
        return dt.datetime.fromisoformat(str(stamp)).astimezone(ZoneInfo(timezone)).date().isoformat()
    except (TypeError, ValueError):
        return str(stamp)[:10]


def _before(stamp, since):
    try:
        return dt.datetime.fromisoformat(str(stamp)) < dt.datetime.fromisoformat(str(since))
    except (TypeError, ValueError):
        return False


def outcomes(database, since, now, timezone="America/Los_Angeles"):
    """What changed in [since, now] from saved records: the short daily summary's data.

    Owner, 2026-10-06: "short summary. Leave in some key details if there are any load bearing
    decisions". Decisions that close a door (a job judged out of scope, given up, merged as a
    duplicate) carry their reason, so the owner can overrule them.
    """
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def rows(sql, parameters=()):
        try:
            cursor = database.execute(sql, parameters)
            names = [column[0] for column in cursor.description]
            return [dict(zip(names, row)) for row in cursor.fetchall()]
        except Exception:
            return []

    def role(job_key):
        row = rows("SELECT employer,payload FROM application_jobs WHERE job_key=?", (job_key,))
        if not row:
            return job_key
        payload = source_records.decoded(row[0]["payload"]) or {}
        payload = payload if isinstance(payload, dict) else {}
        return f"{payload.get('company') or row[0]['employer']}, {payload.get('role') or 'role unknown'}"

    result = {"since": since, "until": now}
    events = rows("SELECT job_key,at,event,detail FROM application_events WHERE at>=? AND at<=? ORDER BY at", (since, now)) \
        if "application_events" in tables else []
    result["submitted"] = []
    for e in events:
        if e["event"] != "SUBMISSION_VERIFIED":
            continue
        detail = source_records.decoded(e["detail"]) or {}
        armed = detail.get("armed_at") if isinstance(detail, dict) else None
        # A submission made before the window and only confirmed in it (the page-confirmation
        # settlement of 2026-10-06 confirmed 16 armed 09-23..09-30) is shown with its own date,
        # not as one made in the last 24 hours (review round 11).
        result["submitted"].append({"job": role(e["job_key"]), "at": e["at"],
                                    **({"submitted_at": armed, "submitted_day": _local_day(armed, timezone)}
                                       if _before(armed, since) else {})})
    discovered = [e for e in events if e["event"] == "DISCOVERED"]
    result["discovered"] = dict(Counter((source_records.decoded(e["detail"]) or {}).get("source", "research") for e in discovered))
    decisions = []
    for e in events:
        if e["event"] not in {"INELIGIBLE", "EXHAUSTED", "ABANDONED", "DUPLICATE"}:
            continue
        detail = source_records.decoded(e["detail"]) or {}
        detail = detail if isinstance(detail, dict) else {}
        decisions.append({"job": role(e["job_key"]), "decision": e["event"], "code": detail.get("code"),
                          "why": _short(detail.get("reason") if isinstance(detail.get("reason"), str) else detail.get("next_action"), 240)})
    result["decisions"] = decisions
    result["submissions_unconfirmed"] = rows("""SELECT COUNT(*) AS n FROM application_jobs WHERE state='RECONCILE'""")[0]["n"] \
        if "application_jobs" in tables else None
    result["owner_blockers"] = [{"job": role(row["job_key"]), "code": row["code"], "needs": _short(row["required_input"])}
                                for row in rows("SELECT job_key,code,required_input FROM application_blockers WHERE owner!='orchestrator' AND updated_at>=?", (since,))] \
        if "application_blockers" in tables else []
    result["invitations"] = rows("SELECT identity,state FROM outreach_attempts WHERE claimed_at>=? AND claimed_at<=? ORDER BY claimed_at",
                                 (since, now)) if "outreach_attempts" in tables else []
    engagement = rows("""SELECT identity,kind,occurred_at FROM engagement_events WHERE observed_at>=? AND observed_at<=?
                         AND kind IN ('inbound','accepted','followup_sent','opt_out') ORDER BY occurred_at""", (since, now)) \
        if "engagement_events" in tables else []
    result["accepted"] = [e["identity"] for e in engagement if e["kind"] == "accepted"]
    result["followups_sent"] = [e["identity"] for e in engagement if e["kind"] == "followup_sent"]
    result["opt_outs"] = [e["identity"] for e in engagement if e["kind"] == "opt_out"]
    # Who wrote and when, never what: message text stays out of saved reports and Telegram.
    result["inbound"] = [{"from": e["identity"], "at": e["occurred_at"]} for e in engagement if e["kind"] == "inbound"]
    result["replies_sent"] = rows("""SELECT identity,claimed_at FROM outreach_actions WHERE action_id LIKE 'reply:%'
                                     AND state='CONFIRMED_SENT' AND claimed_at>=? AND claimed_at<=?""", (since, now)) \
        if "outreach_actions" in tables else []
    queued = rows("""SELECT detail FROM workflow_task_events WHERE event='REPLY_TOOL_QUEUE_JOB' AND at>=? AND at<=?""", (since, now)) \
        if "workflow_task_events" in tables else []
    result["queued_by_reply"] = [source_records.decoded(row["detail"]) for row in queued]
    runs = rows("SELECT state FROM runs WHERE started_at>=? AND started_at<=?", (since, now)) if "runs" in tables else []
    result["hourly_cycles"] = dict(Counter(row["state"] for row in runs))
    result["compute_waits"] = rows("""SELECT COUNT(*) AS n FROM workflow_task_events WHERE event IN ('COMPUTE_WAIT','CAPACITY_DEFERRED')
                                      AND at>=? AND at<=?""", (since, now))[0]["n"] if "workflow_task_events" in tables else None
    return result


def summary_lines(payload):
    """The short daily summary sent on Telegram (owner, 2026-10-06); the full report stays saved."""
    data = payload.get("outcomes_24h") or {}
    research = payload.get("research_daily", {})
    lines = [f"Job search, last 24 hours ({payload['day']})"]
    for flag in payload.get("reply_owner_flags") or []:
        lines.append(f"FOR YOU: {_short(flag.get('reason'), 300)}")
    for blocker in data.get("owner_blockers") or []:
        lines.append(f"FOR YOU: {blocker['job']}: {blocker['code']}. {blocker['needs']}")
    submitted = data.get("submitted") or []
    earlier = [item for item in submitted if item.get("submitted_at")]
    lines.append(f"Applications submitted: {len(submitted) - len(earlier)} (total verified {payload.get('verified_submissions')})")
    lines += [f"- {item['job']}" for item in submitted if not item.get("submitted_at")]
    if earlier:
        lines.append(f"Earlier applications confirmed: {len(earlier)}")
        lines += [f"- {item['job']} (submitted {item.get('submitted_day') or item['submitted_at'][:10]})" for item in earlier]
    discovered = data.get("discovered") or {}
    lines.append(f"New jobs found: {sum(discovered.values())}"
                 + (f" ({', '.join(f'{count} from {source}' for source, count in sorted(discovered.items()))})" if discovered else ""))
    for item in data.get("queued_by_reply") or []:
        if isinstance(item, dict):
            lines.append(f"- queued from a conversation: {item.get('url')} ({item.get('state')})")
    decisions = data.get("decisions") or []
    if decisions:
        lines.append(f"Decisions that close a job ({len(decisions)}); overrule any you disagree with:")
        names = {"INELIGIBLE": "not eligible", "EXHAUSTED": "given up", "ABANDONED": "abandoned", "DUPLICATE": "duplicate"}
        lines += [f"- {item['job']}: {names[item['decision']]}. {item['why']}" for item in decisions[:12]]
        if len(decisions) > 12:
            lines.append(f"- and {len(decisions) - 12} more (full report)")
    if data.get("submissions_unconfirmed"):
        lines.append(f"Waiting to confirm whether a submission went through: {data['submissions_unconfirmed']} jobs")
    invitations = data.get("invitations") or []
    lines.append(f"Invitations sent: {len(invitations)}; accepted: {len(data.get('accepted') or [])}; "
                 f"follow-ups sent: {len(data.get('followups_sent') or [])}")
    inbound = data.get("inbound") or []
    lines.append(f"Messages received: {len(inbound)}; replies sent: {len(data.get('replies_sent') or [])}")
    senders = Counter(item["from"] for item in inbound)
    lines += [f"- from {sender}" + (f" ({count} messages)" if count > 1 else "") for sender, count in list(senders.items())[:8]]
    if data.get("opt_outs"):
        lines.append(f"Opted out: {', '.join(data['opt_outs'])}")
    lines.append(f"Research {research.get('day', '')}: {research.get('state', 'UNKNOWN')}")
    problems = []
    cycles = data.get("hourly_cycles") or {}
    ran = sum(cycles.values())
    if ran < 24 or set(cycles) - {"COMPLETE"}:
        states = ", ".join(f"{count} {state}" for state, count in sorted(cycles.items()))
        problems.append(f"hourly cycles {ran}/24" + (f" ({states})" if states else ""))
    if data.get("compute_waits"):
        problems.append(f"{data['compute_waits']} tasks waited for compute")
    undecodable = (payload.get("inbox_undecodable") or {}).get("records") or []
    if undecodable:
        problems.append(f"{len(undecodable)} inbox conversations unreadable")
    if problems:
        lines.append("Problems: " + "; ".join(problems))
    return lines


PART_LIMIT = 3500
MAX_PARTS = 20


def _units(text):
    return len(text.encode("utf-16-le")) // 2


def _cut(text, units):
    """The longest prefix of text within units UTF-16 units, never splitting a surrogate pair."""
    return text.encode("utf-16-le")[:units * 2].decode("utf-16-le", errors="ignore")


def telegram_parts(payload, lines=None):
    """The daily report as the messages sent, in order: each at most PART_LIMIT UTF-16 units,
    at most MAX_PARTS of them, each headed "(part i/n)" when there is more than one. `lines`
    defaults to the full report; `daily` sends the short summary (`summary_lines`)."""
    lines = telegram_lines(payload) if lines is None else lines
    suffix = f"Full unabridged report: daily_reports[{payload['day']}].payload. Telegram API receipt does not prove human read."
    marker = "[summary truncated]"
    # Room for the "(part i/n)" header; the title line itself is the first line of part 1.
    budget = PART_LIMIT - _units(f"(part {MAX_PARTS}/{MAX_PARTS})\n")
    pieces = []
    for line in lines:
        while _units(line) > budget:
            head = _cut(line, budget)
            pieces.append(head)
            line = line[len(head):]
        pieces.append(line)
    pieces.append(suffix)
    parts, current = [], []
    for piece in pieces:
        if current and _units("\n".join(current + [piece])) > budget:
            parts.append(current)
            current = []
        current.append(piece)
    parts.append(current)
    if len(parts) > MAX_PARTS:
        kept = parts[:MAX_PARTS]
        last = kept[-1]
        tail = [marker, suffix]
        while last and _units("\n".join(last + tail)) > budget:
            last.pop()
        kept[-1] = last + tail
        parts = kept
    texts = ["\n".join(part) for part in parts]
    if len(texts) > 1:
        texts = [f"(part {index}/{len(texts)})\n{text}" for index, text in enumerate(texts, 1)]
    return texts


def full_report(database, day):
    """The full report for a saved day, as text (it is saved, not sent: Telegram gets the summary)."""
    row = database.execute("SELECT payload FROM daily_reports WHERE day=?", (day,)).fetchone()
    return telegram_body(json.loads(row[0])) if row else None


def stored_payload(payload):
    """The day's report as saved (owner, 2026-10-06: "trim/remove all the waste ... Application
    recovery list should also be removed"). The saved report was ~12 MB a day: 9.4 MB the listing
    of every workflow task ever (`worker_tasks`) and 2 MB the full recovery listing. Kept: every
    count, and the records the full report actually prints (the first two recovery records,
    abandoned first, and the first two reopens)."""
    stored = dict(payload)
    tasks = payload.get("worker_tasks")
    # Compacting a payload twice changes nothing (an already compacted listing keeps its counts).
    if isinstance(tasks, dict) and isinstance(tasks.get("records"), list) and "omitted_records" not in tasks:
        counts = Counter((row.get("worker"), row.get("status")) for row in tasks["records"])
        stored["worker_tasks"] = dict({key: value for key, value in tasks.items() if key != "records"},
                                      records=None, omitted_records=len(tasks["records"]),
                                      counts=[{"worker": worker, "status": status, "count": count}
                                              for (worker, status), count in sorted(counts.items(), key=str)])
    recovery = payload.get("bounded_recovery")
    if isinstance(recovery, dict):
        recovery = dict(recovery)
        if isinstance(recovery.get("records"), list) and "omitted_records" not in recovery:
            recovery["omitted_records"] = len(recovery["records"])
            recovery["records"] = sorted(recovery["records"], key=lambda row: row.get("category") != "abandoned")[:2]
        stored["bounded_recovery"] = recovery
        for holder in (recovery, stored):
            reopens = holder.get("authorized_reopens")
            if isinstance(reopens, dict) and isinstance(reopens.get("records"), list) and "omitted_records" not in reopens:
                holder["authorized_reopens"] = dict(reopens, records=reopens["records"][:2], omitted_records=len(reopens["records"]))
    return stored


def telegram_body(payload):
    """Every part of the daily report joined, as the owner reads it across the messages."""
    return "\n\n".join(telegram_parts(payload))


def part_key(day, index):
    """Part 1 keeps the historical key daily-report:<day>; later parts are daily-report:<day>:<n>."""
    return "daily-report:" + day + ("" if index == 1 else f":{index}")


def part_keys(database, day):
    first = part_key(day, 1)
    return [first] + [row[0] for row in database.execute(
        "SELECT event_key FROM notification_outbox WHERE event_key LIKE ? ORDER BY rowid", (first + ":%",))]


def applications_job(database, limit=3):
    """The applications job's latest runs (owner, 2026-10-02), newest first, from the durable
    worker step each run records (`orchestrator.applications_service`). Runs that skipped (the
    previous run still going, or yielding to daily research) record no step; they are in the
    job's own log and in reports/applications-service.json."""
    try:
        rows = database.execute("""SELECT task.task_id, task.status, task.updated_at, task.result, task.blocker,
                (SELECT MIN(at) FROM workflow_task_events WHERE task_id=task.task_id) AS started_at
            FROM workflow_tasks AS task WHERE task.worker='applications' AND task.parent_run_id LIKE 'applications-job:%'
            ORDER BY task.updated_at DESC LIMIT ?""", (limit,)).fetchall()
    except Exception:
        return {"state": "UNREADABLE", "runs": []}
    runs = []
    for row in rows:
        result = source_records.decoded(row[3]) if row[3] else None
        result = result if isinstance(result, dict) else {}
        runs.append({"task_id": row[0], "status": row[1], "started_at": row[5],
                     "finished_at": row[2] if row[1] != "RUNNING" else None,
                     "state": result.get("state") or row[4], "stopped": result.get("stopped"),
                     "verified_submissions": result.get("verified_submissions")})
    return {"state": "OBSERVED" if runs else "NO_RUN_RECORDED", "runs": runs}


def daily_due(database, config, now=None):
    """True once a day: the report hour has passed and today's report is not saved yet (owner,
    2026-10-06: "there is no reason why daily report should run hourly. It should run once a day")."""
    now = now or applications.stamp()
    local = dt.datetime.fromisoformat(now).astimezone(ZoneInfo(config.get("timezone", "America/Los_Angeles")))
    if local.hour < config.get("daily_report_hour", 18):
        return False
    try:
        return database.execute("SELECT 1 FROM daily_reports WHERE day=?", (local.date().isoformat(),)).fetchone() is None
    except sqlite3.OperationalError:
        return True


def daily(database, config, now=None):
    initialize(database)
    now = now or applications.stamp()
    local = dt.datetime.fromisoformat(now).astimezone(ZoneInfo(config.get("timezone", "America/Los_Angeles")))
    hour = config.get("daily_report_hour", 18)
    if local.hour < hour:
        return {"state": "NOT_DUE", "default_send_hour_local": hour}
    day = local.date().isoformat()
    existing = database.execute("SELECT payload FROM daily_reports WHERE day=?", (day,)).fetchone()
    if existing:
        if config.get("reporting_owner_confirmed", False):
            with source_records.transaction(database):
                for key in part_keys(database, day):
                    database.execute("UPDATE notification_outbox SET state='PENDING',last_error=NULL WHERE event_key=? AND state='WAITING_OWNERSHIP'", (key,))
        return json.loads(existing[0])
    payload = snapshot(database, config, now)
    payload["day"] = day
    event_key = part_key(day, 1)
    bodies = telegram_parts(payload, summary_lines(payload))
    with source_records.transaction(database):
        # One outbox row per part, inserted in order (dispatch sends equal updated_at by rowid),
        # so each part is claimed, retried and reconciled on its own key exactly as before.
        for index, body in enumerate(bodies, 1):
            applications.notify(database, part_key(day, index), body, now)
            if not config.get("reporting_owner_confirmed", False):
                database.execute("UPDATE notification_outbox SET state='WAITING_OWNERSHIP',last_error='CLOUD_DAILY_REPORT_NOT_CONFIRMED_DISABLED' WHERE event_key=?", (part_key(day, index),))
        payload["report_delivery"] = notification_status(database, event_key)
        payload["report_parts"] = [{"event_key": part_key(day, index), "characters": _units(body),
                                    "state": notification_status(database, part_key(day, index))["state"]}
                                   for index, body in enumerate(bodies, 1)]
        database.execute("INSERT OR IGNORE INTO daily_reports VALUES (?,?,?,?)", (day, now, json.dumps(stored_payload(payload)), event_key))
    return json.loads(database.execute("SELECT payload FROM daily_reports WHERE day=?", (day,)).fetchone()[0])
