"""Saved-record helpers and the independently verified execution check.

`execution(database, origin, now)` says whether a recorded application or outreach send has
independent proof (a SUBMITTED receipt, a verified send receipt or a read showing the invitation);
reporting, champion delivery and conversion attribution count only those. These were part of
learning.py, which was removed with the learning feature (owner, 2026-10-22: "lets remove learning
totally. its wasteful."); the checks are unchanged.
"""

import datetime as dt
import hashlib
import json
from contextlib import contextmanager

import application_destination

def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def decoded(value, default=None):
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return {} if default is None else default


def instant(value):
    try:
        parsed = dt.datetime.fromisoformat(value)
        return parsed.astimezone(dt.timezone.utc) if parsed.tzinfo else None
    except (ValueError, TypeError):
        return None


def columns(database, table):
    return {row[1] for row in database.execute('PRAGMA table_info("' + table + '")')}


def rows(database, table, required=()):
    available = columns(database, table)
    if not available or not set(required) <= available:
        return None
    cursor = database.execute('SELECT * FROM "' + table + '"')
    names = [item[0] for item in cursor.description]
    return [dict(zip(names, row)) for row in cursor]


@contextmanager
def transaction(database):
    database.execute("SAVEPOINT record_change")
    try:
        yield
        database.execute("RELEASE record_change")
    except BaseException:
        database.execute("ROLLBACK TO record_change")
        database.execute("RELEASE record_change")
        raise


def verified(evidence, now):
    if not isinstance(evidence, dict):
        return False
    observed = instant(evidence.get("observed_at"))
    return bool(evidence.get("source") and evidence.get("verified") is True and observed
                and observed <= instant(now))

def campaign_provenance(database, attempt, payload):
    import applications
    import outreach

    launch = next((record for record in rows(database, "outreach_launch_claims") or []
                   if record.get("campaign_id") == attempt.get("campaign_id")), None)
    if not launch:
        return None
    candidate = decoded(launch.get("payload"))
    if (attempt.get("state") != "CONFIRMED_SENT" or launch.get("identity") != attempt["identity"]
            or decoded(attempt.get("result")) != payload.get("raw")
            or launch.get("claimed_at") != attempt.get("claimed_at")
            or hashlib.sha256(launch["payload"].encode()).hexdigest() != launch.get("payload_hash")
            or payload.get("launch_payload_hash") != launch["payload_hash"]
            or any(payload.get(field) != candidate.get(field) for field in
                   ("campaign_id", "preparation_action_id", "preparation_payload_hash"))):
        return None
    owner = outreach._prepared_owner(database, candidate)
    if not owner or owner["sender_id"] != launch.get("sender_id"):
        return None
    action = owner["action"]
    return {"preparation_action_id": owner["action_id"],
            "preparation_payload_hash": candidate["preparation_payload_hash"],
            "launch_payload_hash": launch["payload_hash"], "segment": action.get("segment", {}),
            "employer": applications.normalized(action.get("company", "")) or None,
            "requisition": action.get("requisition", action.get("requisition_id"))}


def invite_delivery(database, row, action, now):
    """A Unipile INVITE's delivered send (owner, 2026-10-17: an invite counts as delivered once a
    read shows it): the verified initial_sent that sequence records only
    after a read shows the invitation (never the acknowledgement), bound like a campaign invitation
    to the action's own immutable payload (its note)."""
    import applications

    key = row["action_id"]
    claimed = instant(row.get("claimed_at"))
    if hashlib.sha256(row["payload"].encode()).hexdigest() != row.get("payload_hash") or not claimed:
        return None
    for event in rows(database, "engagement_events", ("identity", "source", "source_id", "payload")) or []:
        payload = decoded(event["payload"])
        raw = payload.get("raw")
        occurred, observed = instant(event.get("occurred_at")), instant(event.get("observed_at"))
        if (event["source"] == "unipile" and event["source_id"] == "invite:" + key
                and event["identity"] == row["identity"] and event.get("kind") == "initial_sent"
                and payload.get("verified") is True and payload.get("purpose") == "INVITE"
                and payload.get("invitation") is True and payload.get("evidence") and isinstance(raw, dict)
                and raw.get("note") == action.get("note") and not payload.get("baseline") and not payload.get("imported")
                and occurred and observed and claimed <= occurred <= observed <= instant(now)):
            proof = {"source": event["source"], "observed_at": event["observed_at"], "verified": True,
                     "independent_read": True, "provider_message_id": event["source_id"], "identity": row["identity"],
                     "sent_at": event["occurred_at"], "status": "SENT", "source_evidence": payload["evidence"],
                     "segment": action.get("segment", {}), "invite_action_id": key,
                     "invite_payload_hash": row["payload_hash"]}
            return {"kind": "OUTREACH_SENT", "at": event["occurred_at"], "evidence": proof,
                    "segment": action.get("segment", {}),
                    "employer": applications.normalized(action.get("company", "")) or None,
                    "requisition": action.get("requisition", action.get("requisition_id"))}
    return None


def execution(database, origin, now):
    import applications
    if not isinstance(origin, dict):
        return None
    table, key = origin.get("table"), origin.get("key")
    if table == "application_jobs":
        candidates = rows(database, table, ("job_key", "state", "receipt", "payload")) or []
        job = next((row for row in candidates if row["job_key"] == key), None)
        if not job or job["state"] != "SUBMITTED":
            return None
        proof = decoded(job["receipt"])
        if not (verified(proof, now) and proof.get("independent_read") is True
                and proof.get("status") == "SUBMITTED" and proof.get("receipt_id")
                and application_destination.employer_accepted(proof, job)
                and str(proof.get("requisition") or "").strip().casefold() == job["requisition"].strip().casefold()):
            return None
        payload = decoded(job["payload"])
        return {"kind": "APPLICATION_SUBMITTED", "at": proof["observed_at"], "evidence": proof,
                "segment": payload.get("segment", {}), "employer": job["employer"], "requisition": job["requisition"]}
    if table == "outreach_attempts":
        attempts = rows(database, table, ("identity", "state", "result")) or []
        attempt = next((row for row in attempts if row["identity"] == key), None)
        if not attempt or attempt["state"] in {"ALREADY_SENT", "PENDING_VALIDATION", "BLOCKED"}:
            return None
        proof = decoded(attempt["result"])
        direct = (attempt["state"] in {"SENT", "DELIVERED", "DELIVERY_CONFIRMED"}
                and verified(proof, now) and proof.get("independent_read") is True
                and proof.get("provider_message_id") and proof.get("identity") == key
                and proof.get("status") in {"SENT", "DELIVERED"})
        if direct:
            sent_at = instant(proof.get("sent_at", proof["observed_at"]))
            claimed_at = instant(attempt.get("claimed_at"))
            direct = bool(sent_at and claimed_at and claimed_at <= sent_at <= instant(proof["observed_at"]))
        if not direct:
            for row in rows(database, "engagement_events", ("identity", "source", "source_id", "payload")) or []:
                payload = decoded(row["payload"])
                occurred = instant(row.get("occurred_at"))
                claimed = instant(attempt.get("claimed_at"))
                if (row["identity"] == key and row.get("kind") == "initial_sent" and payload.get("verified") is True
                        and payload.get("evidence") and payload.get("raw") and not payload.get("baseline")
                        and not payload.get("imported") and payload.get("campaign_id") == attempt.get("campaign_id")
                        and payload.get("campaign_id") and claimed and occurred and claimed <= occurred
                        and instant(row.get("observed_at")) and occurred <= instant(row["observed_at"]) <= instant(now)):
                    managed = any(record.get("campaign_id") == attempt.get("campaign_id")
                                  for record in rows(database, "outreach_launch_claims") or [])
                    provenance = campaign_provenance(database, attempt, payload) if managed else None
                    if (managed or payload.get("preparation_action_id")) and not provenance:
                        continue
                    proof = {"source": row["source"], "observed_at": row["observed_at"], "verified": True,
                             "independent_read": True, "provider_message_id": row["source_id"], "identity": key,
                             "sent_at": row["occurred_at"], "status": "SENT", "source_evidence": payload["evidence"],
                             "segment": payload.get("segment", {})}
                    proof.update(provenance or {})
                    break
            else:
                return None
        return {"kind": "OUTREACH_SENT", "at": proof.get("sent_at", proof["observed_at"]),
                "evidence": proof, "segment": proof.get("segment", {}), "employer": proof.get("employer"),
                "requisition": proof.get("requisition")}
    if table == "outreach_actions":
        row = next((record for record in rows(database, table) or [] if record.get("action_id") == key), None)
        if not row or row.get("state") != "CONFIRMED_SENT":
            return None
        action, receipt = decoded(row.get("payload")), decoded(row.get("result"))
        if action.get("purpose") == "INVITE" and action.get("provider") == "unipile":
            return invite_delivery(database, row, action, now)
        occurred, observed, claimed = instant(receipt.get("occurred_at")), instant(receipt.get("observed_at")), instant(row.get("claimed_at"))
        expected = {"action_id": key, "identity": row.get("identity"), "payload_hash": row.get("payload_hash"),
                    "operation": "send_message", "outcome": "SENT", "conversation_id": action.get("conversation_id")}
        if (receipt.get("independent") is not True or not receipt.get("receipt_id") or not receipt.get("evidence")
                or not receipt.get("sender_id") or not action.get("conversation_id") or action.get("purpose") == "PREPARE_CAMPAIGN"
                or any(receipt.get(field) != value for field, value in expected.items())
                or hashlib.sha256(row["payload"].encode()).hexdigest() != row.get("payload_hash")
                or not occurred or not observed or not claimed or not claimed <= occurred <= observed <= instant(now)):
            return None
        bound_sender = row.get("claimed_sender_id", row.get("sender_id", action.get("sender_id")))
        if bound_sender is not None and receipt["sender_id"] != bound_sender:
            return None
        proof = {"source": action.get("provider", "heyreach"), "verified": True, "observed_at": receipt["observed_at"],
                 "independent_read": True, "provider_message_id": receipt["receipt_id"], "receipt": receipt,
                 "identity": row["identity"], "sender_binding": "BOUND" if bound_sender is not None else "SAVED_WORKER_RECONCILIATION"}
        return {"kind": "OUTREACH_SENT", "at": receipt["occurred_at"], "evidence": proof,
                "segment": action.get("segment", {}), "employer": " ".join(action.get("company", "").casefold().split()) or None,
                "requisition": action.get("requisition", action.get("requisition_id"))}
    return None
