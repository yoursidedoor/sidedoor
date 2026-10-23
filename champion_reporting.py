"""Synchronize saved champion delivery facts; never queue or send a message.

The caller owns the transaction and shared orchestrator lock. Only existing
conversion tasks and their derived reporting handoffs change; summaries and
referral holds do not.
"""

import json
import re
import sqlite3

import conversion
import source_records
import workflow_tasks


STATES = {
    "DRAFT": "DRAFT", "PREPARED": "DRAFT", "PENDING": "QUEUED",
    "BLOCKED": "HELD", "AWAITING_APPROVAL": "HELD", "CANCELLED": "HELD", "CANCELLED_BEFORE_SEND": "HELD",
    "CLOSED": "HELD", "CONFIRMED_NOT_SENT": "HELD",
    "CLAIMED": "UNCERTAIN_NO_RETRY", "DISPATCHING": "UNCERTAIN_NO_RETRY",
    "REQUEST_STARTED": "UNCERTAIN_NO_RETRY",
    "ACKNOWLEDGED_UNCONFIRMED": "UNCERTAIN_NO_RETRY",
    "UNCERTAIN_NO_RETRY": "UNCERTAIN_NO_RETRY",
    "CONFIRMED_SENT": "UNCERTAIN_NO_RETRY",
}
PROVENANCE = {"reply_task_id", "action_id", "agreement", "role",
              "candidate_resume", "deadline", "next_action"}
NEXT_ACTIONS = {
    "DRAFT": "Review the saved champion draft and existing reply handoff",
    "QUEUED": "Await validation by the existing guarded REPLY sender",
    "HELD": "Review the saved champion handoff and existing sender gates",
    "UNCERTAIN_NO_RETRY": "Reconcile the existing send claim; do not repeat the send",
    "CONFIRMED_SENT": "Retain the independent delivery receipt and review the referral hold",
}
READ_STATES = {"READY", "READING", "RETRY_WAIT", "READ_RETRY_EXHAUSTED",
               "ADAPTER_UNAVAILABLE", "INVALID_PROOF", "COMPLETE"}
READ_ERRORS = {"INTERRUPTED_READ", "OUTREACH_AI_SHARED_BUDGET_EXHAUSTED",
               "UNSUPPORTED_READ_ADAPTER", "INDEPENDENT_RECEIPT_PENDING",
               "MALFORMED_INSPECTION_PROOF", "SOURCE_EVIDENCE_UNRESOLVED",
               "TimeoutError", "ConnectionError", "URLError", "HTTPError",
               "ConnectionResetError", "ConnectionRefusedError", "ConnectionAbortedError",
               "BrokenPipeError", "OSError", "SSLError", "ValueError", "TypeError",
               "KeyError", "AttributeError", "RuntimeError", "NotImplementedError"}


def read_retry_status(row):
    """Expose saved read progress with known codes, never provider error text."""
    error = row.get("last_error")
    safe_error = None if error is None else "UNRECOGNIZED_READ_ERROR"
    if isinstance(error, str) and (error in READ_ERRORS or re.fullmatch(r"HTTP_[1-5][0-9]{2}", error)):
        safe_error = error
    next_at = source_records.instant(row.get("next_at"))
    return {"phase": row["phase"] if row.get("phase") in {"INSPECTION", "RECONCILIATION"} else "UNKNOWN",
            "state": row["state"] if row.get("state") in READ_STATES else "UNKNOWN",
            "attempts": row["attempts"] if type(row.get("attempts")) is int and row["attempts"] >= 0 else None,
            "next_at": next_at.isoformat() if next_at else None, "last_error": safe_error}


def _owner_revised(database, action, payload, summary):
    """The owner's Revise and Approve (review mode, owner 2026-10-22) of this summary: a
    MESSAGE_REVISION of exactly this action's payload whose original text is the stored summary body
    and whose edited text is the action's message. The edited summary is then the bound one."""
    import approvals
    try:
        record = approvals.revision_of(database, action["action_id"], action["payload_hash"])
        return (record is not None and record.get("original_message") == summary["body"]
                and record.get("edited_message") == payload.get("message"))
    except Exception:
        return False


def _delivery(database, summary, provenance, action, claim, now):
    delivery = {"action_state": None, "delivery_state": "DRAFT",
                "receipt_reference": None}
    if action is None:
        return delivery
    state = action["state"]
    delivery.update(action_state=state if state in STATES else "UNKNOWN",
                    delivery_state=STATES.get(state, "HELD"))
    payload = source_records.decoded(action["payload"])
    if (not isinstance(payload, dict) or payload.get("purpose") != "REPLY"
            or action.get("identity") != summary["contact_id"]
            or payload.get("action_id") != provenance["action_id"]
            or payload.get("reply_task_id") != provenance["reply_task_id"]
            or payload.get("message") != summary["body"] and not _owner_revised(database, action, payload, summary)):
        delivery["error"] = "ACTION_SUMMARY_BINDING_MISMATCH"
        delivery["delivery_state"] = (
            "UNCERTAIN_NO_RETRY" if delivery["delivery_state"] == "UNCERTAIN_NO_RETRY" else "HELD")
        return delivery
    if state != "CONFIRMED_SENT":
        return delivery
    receipt = source_records.decoded(action.get("result"))
    bound = (isinstance(receipt, dict) and bool(claim)
             and all(action.get(field) == claim.get(field) for field in
                     ("payload", "payload_hash", "identity", "claimed_at"))
             and receipt.get("sender_id") == claim.get("sender_id"))
    try:
        verified = bound and source_records.execution(
            database, {"table": "outreach_actions", "key": action["action_id"]}, now)
    except (ValueError, TypeError, KeyError, AttributeError):
        verified = False
    reference = receipt.get("receipt_id") if isinstance(receipt, dict) else None
    if (verified and isinstance(reference, (str, int)) and not isinstance(reference, bool)
            and reference):
        delivery.update(delivery_state="CONFIRMED_SENT", receipt_reference=reference)
    else:
        delivery["error"] = "INDEPENDENT_CLAIM_BOUND_RECEIPT_REQUIRED"
    return delivery


def sync(database, now=None):
    """Return privacy-limited state/items and sync existing delivery tasks.

    Delivery is based on stored shared-sender evidence, not a live inbox read.
    Missing tables create no schema. Malformed records are isolated per item.
    """
    return _collect(database, now, update_tasks=True)


def snapshot(database, now=None):
    """Read saved delivery evidence without changing tasks or creating tables."""
    return _collect(database, now, update_tasks=False)


def _complete_handoff(database, task, summary, delivery, now):
    if (not source_records.columns(database, "workflow_tasks")
            or not source_records.columns(database, "workflow_task_events")):
        return
    handoff = conversion._one(database, """SELECT * FROM workflow_tasks
        WHERE worker='conversion-handoff' AND action_id=?""", (task["task_id"],))
    if (not handoff or handoff["effect"] != "READ"
            or handoff["status"] not in {"QUEUED", "BLOCKED", "RETRY_WAIT", "EXHAUSTED"}
            or handoff["role_id"] != summary["job_key"]
            or handoff["contact_id"] != summary["contact_id"]):
        return
    inputs = source_records.decoded(handoff["inputs"])
    if (not isinstance(inputs, list) or not any(
            isinstance(source, dict) and source.get("source") == "conversion_tasks:" + task["task_id"]
            and source.get("kind") == "GUARDED_CHAMPION_DELIVERY" for source in inputs)):
        return
    result = {"state": "COMPLETE", "summary_id": summary["summary_id"],
              "action_id": delivery["action_id"], "delivery_state": "CONFIRMED_SENT",
              "receipt_reference": delivery["receipt_reference"],
              "basis": "DERIVED_REPORTING_ONLY; existing shared sender independently confirmed delivery"}
    evidence = source_records.decoded(handoff["evidence"])
    evidence = evidence if isinstance(evidence, list) else []
    evidence.append({"source": "outreach_actions:" + delivery["action_id"],
                     "observed_at": now, "receipt_reference": delivery["receipt_reference"],
                     "basis": "SAVED_INDEPENDENT_RECEIPT"})
    previous = {field: handoff[field] for field in
                ("status", "result", "evidence", "blocker", "next_at", "updated_at")}
    database.execute("""UPDATE workflow_tasks SET status='COMPLETE',result=?,evidence=?,
        blocker=NULL,next_at=NULL,updated_at=? WHERE task_id=?""",
                     (conversion.encoded(result), conversion.encoded(evidence), now, handoff["task_id"]))
    workflow_tasks.record(database, handoff["task_id"], "COMPLETE", now,
                          {"source": "champion_reporting", "previous": previous,
                           "result": result, "evidence": evidence})


def _collect(database, now, update_tasks):
    now = conversion.timestamp(now)
    summaries = source_records.rows(database, "conversion_summaries",
                              ("summary_id", "job_key", "contact_id", "body", "provenance"))
    tasks = source_records.rows(database, "conversion_tasks", ("task_id", "job_key", "kind", "payload", "status"))
    result = {"state": "EMPTY", "items": []}
    if summaries is None or tasks is None:
        result["state"] = "UNAVAILABLE"
        return result
    actions = {row["action_id"]: row for row in source_records.rows(
        database, "outreach_actions", ("action_id", "state", "payload")) or []}
    claims = {row["action_id"]: row for row in source_records.rows(
        database, "outreach_action_claims", ("action_id",)) or []}
    retries = {}
    for row in source_records.rows(database, "outreach_read_retries", ("action_id", "phase", "state")) or []:
        retries.setdefault(row["action_id"], []).append(read_retry_status(row))
    indexed_tasks = {}
    for task in tasks:
        if task["kind"] != "GUARDED_CHAMPION_DELIVERY":
            continue
        payload = source_records.decoded(task["payload"])
        inputs = payload.get("inputs") if isinstance(payload, dict) else None
        if not isinstance(inputs, dict) or not isinstance(inputs.get("summary_id"), str):
            result["items"].append({"task_id": task["task_id"], "delivery_state": "HELD",
                                    "next_action": NEXT_ACTIONS["HELD"],
                                    "error": "MALFORMED_DELIVERY_TASK"})
            continue
        indexed_tasks.setdefault(inputs["summary_id"], []).append((task, payload))
    for summary in summaries:
        try:
            provenance = json.loads(summary["provenance"])
        except (ValueError, TypeError):
            provenance = None
        if isinstance(provenance, dict) and "reply_task_id" not in provenance:
            continue
        item = {"summary_id": summary["summary_id"], "job_key": summary["job_key"],
                "contact_id": summary["contact_id"], "delivery_state": "HELD",
                "next_action": NEXT_ACTIONS["HELD"],
                "action_state": None, "receipt_reference": None, "read_retries": []}
        result["items"].append(item)
        try:
            if (not isinstance(provenance, dict) or not PROVENANCE <= provenance.keys()
                    or not all(provenance[field] for field in PROVENANCE)
                    or not all(isinstance(provenance[field], str) for field in
                               ("reply_task_id", "action_id", "deadline", "next_action"))):
                raise ValueError("MALFORMED_SUMMARY_PROVENANCE")
            conversion.timestamp(provenance["deadline"])
            item.update(action_id=provenance["action_id"], reply_task_id=provenance["reply_task_id"],
                        deadline=provenance["deadline"])
            item["read_retries"] = sorted(retries.get(provenance["action_id"], []), key=lambda row: row["phase"])
            matching = indexed_tasks.get(summary["summary_id"], [])
            if len(matching) != 1:
                raise ValueError("DELIVERY_TASK_MISSING_OR_AMBIGUOUS")
            task, payload = matching[0]
            inputs = payload["inputs"]
            item["task_id"] = task["task_id"]
            if (task["job_key"] != summary["job_key"]
                    or inputs.get("contact_id") != summary["contact_id"]
                    or inputs.get("action_id") != provenance["action_id"]
                    or inputs.get("exact_message") != summary["body"]
                    or inputs.get("deadline") != provenance["deadline"]
                    or inputs.get("next_action") != provenance["next_action"]):
                raise ValueError("TASK_SUMMARY_BINDING_MISMATCH")
            delivery = _delivery(database, summary, provenance,
                                 actions.get(provenance["action_id"]),
                                 claims.get(provenance["action_id"]), now)
            inspection = next((row for row in item["read_retries"] if row["phase"] == "INSPECTION"), {})
            if (delivery["delivery_state"] in {"DRAFT", "QUEUED"}
                    and inspection.get("state") not in {None, "READY", "COMPLETE"}):
                delivery["delivery_state"] = "HELD"
            item.update(delivery)
            item["next_action"] = NEXT_ACTIONS[delivery["delivery_state"]]
            if delivery["delivery_state"] == "HELD" and inspection.get("state") == "RETRY_WAIT":
                item["next_action"] = "Await the existing sender's scheduled inspection read"
            elif delivery["delivery_state"] == "HELD" and inspection.get("state") == "READ_RETRY_EXHAUSTED":
                item["next_action"] = "Review the exhausted sender inspection; automatic read retries are exhausted"
            inputs.update({field: delivery[field] for field in
                           ("delivery_state", "action_state", "receipt_reference")})
            status = "COMPLETE" if delivery["delivery_state"] == "CONFIRMED_SENT" else "PENDING"
            if task["status"] in {"SUPERSEDED", "CANCELLED", "CLOSED"}:
                status = task["status"]
            payload["status"] = status
            item["task_status"] = status
            serialized = conversion.encoded(payload)
            if update_tasks:
                database.execute("SAVEPOINT champion_reporting_item")
                try:
                    if serialized != task["payload"] or status != task["status"]:
                        database.execute("UPDATE conversion_tasks SET payload=?,status=? WHERE task_id=?",
                                         (serialized, status, task["task_id"]))
                    if status == "COMPLETE":
                        _complete_handoff(database, task, summary, item, now)
                    database.execute("RELEASE champion_reporting_item")
                except Exception:
                    database.execute("ROLLBACK TO champion_reporting_item")
                    database.execute("RELEASE champion_reporting_item")
                    raise
        except (ValueError, TypeError, KeyError, AttributeError, sqlite3.DatabaseError):
            item["error"] = "MALFORMED_OR_UNAVAILABLE_DELIVERY_EVIDENCE"
    if result["items"]:
        result["state"] = "PARTIAL" if any(item.get("error") for item in result["items"]) else "PROCESSED"
    return result
