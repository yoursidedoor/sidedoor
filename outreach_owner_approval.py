"""Record an owner-approved first LinkedIn note and hand it to the guarded outreach path.

    python3.13 outreach_owner_approval.py APPROVAL.json [--db PATH] [--apply]
    python3.13 outreach_owner_approval.py APPROVAL.json --reinspect REASON [--apply]

APPROVAL.json (reviewable, hashed into the records):
  profile, recipient, company, title
  attestation: {instruction, attested_at, verified_identity: true, never_contacted: true}
  approval:    {instruction, approved_at, field: "invitation", text, facts: [trace]}
  correct_self_reference: true|false

In one transaction this (1) records the owner's identity/title attestation and the
approval of the EXACT text (sha256) as immutable outreach_owner_records, (2) checks
the text against the hard lines (300-character LinkedIn note cap, every cited fact
found in the designated resume / role / recipient / verified location, every number
cited), (3) optionally corrects a proven self-referential "historical" flag, (4)
appends a new HELD rw_drafts version superseding the research scaffold and (5)
enqueues its bounded outreach-draft-inspection task. It never contacts HeyReach,
LinkedIn or a model and never sends: the scheduled path still performs a fresh
local-records history check, single-contact campaign preparation and the guarded
dispatcher's final checks before any invitation.

Default is a dry run: everything is executed and validated inside a transaction
that is rolled back. --apply commits only when every gate passes.
"""

import resources
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

import engagement
import outreach
import outreach_history
import outreach_promotion as promotion
import research_workflow as research
import paths




def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _record(database, kind, identity, payload, now):
    encoded = research.encoded(payload)
    record_id = research.digest([kind, research.canonical(database, "contact", identity), payload])
    database.execute("INSERT OR IGNORE INTO outreach_owner_records VALUES (?,?,?,?,?,?)",
                     (record_id, kind, identity, now.isoformat(), encoded, hashlib.sha256(encoded.encode()).hexdigest()))
    _require(promotion.owner_record(database, record_id, kind, identity) == payload, "OWNER_RECORD_CONFLICT")
    return record_id


def _latest_scaffold(database, identity):
    rows = database.execute("SELECT * FROM rw_drafts WHERE identity=? ORDER BY version DESC", (identity,)).fetchall()
    _require(rows, "NO_RESEARCH_DRAFT_FOR_RECIPIENT")
    for row in rows:
        if not ({"composition", "owner_approval"} & set(json.loads(row["payload"]))):
            return rows[0], row
    raise ValueError("NO_RESEARCH_SCAFFOLD")


def independent_history(database, identity, recipient):
    """Every durable source of prior contact other than our own unsent research draft."""
    identities = engagement.identity_keys(database, identity)
    name = research.name_key(recipient)
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    found = []
    if "history" in tables:
        for row in database.execute("SELECT * FROM history"):
            if (row["identity"] in identities or research.name_key(row["recipient"]) == name
                    or (row["profile"] and outreach.profile_key(row["profile"]) in identities)):
                found.append("history:" + row["identity"])
    placeholders = ",".join("?" for unused in identities)
    for table in ("outreach_queue", "outreach_actions", "outreach_attempts", "outreach_action_claims"):
        if table in tables and database.execute(f"SELECT 1 FROM {table} WHERE identity IN ({placeholders})", identities).fetchone():
            found.append(table)
    if "suppressions" in tables and database.execute(f"SELECT 1 FROM suppressions WHERE identity IN ({placeholders},?)",
                                                     (*identities, "name:" + name)).fetchone():
        found.append("suppressions")
    if engagement.events_for(database, identity):
        found.append("engagement_events")
    if outreach_history.saved_positive(database, identity):
        found.append("outreach_agent_audit:positive_history")
    return found


def self_reference_correction(database, identity, scaffold, now):
    """rw_contacts.historical=1 set only because the orchestrator mirrored our own research draft."""
    company = research.canonical(database, "company", scaffold["company"])
    row = database.execute("SELECT historical,plan_id FROM rw_contacts WHERE identity=? AND company=?", (identity, company)).fetchone()
    _require(row is not None and row["plan_id"] == scaffold["plan_id"], "RESERVATION_MISSING")
    if not row["historical"]:
        return None
    others = independent_history(database, identity, scaffold["recipient"])
    _require(not others, "INDEPENDENT_HISTORY_EXISTS:" + ",".join(others))
    mirror = database.execute("SELECT payload FROM drafts WHERE identity=?", (outreach.profile_key(identity),)).fetchone()
    _require(mirror is not None and json.loads(mirror["payload"]) == scaffold, "HISTORICAL_SOURCE_NOT_OWN_DRAFT_MIRROR")
    historical = database.execute("SELECT COUNT(*) FROM rw_contacts WHERE company=? AND historical=1", (company,)).fetchone()[0]
    baseline = database.execute("SELECT count FROM rw_baselines WHERE company=?", (company,)).fetchone()
    _require(baseline is None or baseline[0] <= historical, "COMPANY_BASELINE_NOT_EXPLAINED_BY_FLAGS")
    return {"identity": identity, "company": company, "rw_contacts_historical": [1, 0],
            "rw_baselines_count": [baseline[0] if baseline else None, historical - 1 if baseline else None],
            "reason": "Research draft mirrored into the drafts table was re-imported by seed_history as prior contact",
            "independent_history": [], "checked_at": now.isoformat()}


MAX_REINSPECTION_GRANTS = 2


def reinspect_grant(database, config, latest, identity, reason, now):
    """Owner grant of one fresh inspection task for an approved note whose inspection is EXHAUSTED.

    The grant never bypasses anything: the new task runs the same local-records history check
    and AI decision, and every later guard is unchanged. It only replaces a spent task budget.
    """
    import outreach_agent
    draft, checked_identity, _verification = promotion._validate(database, latest, now, require_composition=True)
    promotion._local_gates(database, config, draft, checked_identity)
    source = promotion._source(latest)
    current = outreach_agent.enqueue_inspection(database, latest, draft, source, now)
    status = database.execute("SELECT status FROM workflow_tasks WHERE task_id=?", (current,)).fetchone()[0]
    _require(status == "EXHAUSTED", "REINSPECTION_REQUIRES_EXHAUSTED_INSPECTION:" + status)
    granted = [row for row in database.execute("SELECT payload FROM outreach_owner_records WHERE kind='REINSPECTION_GRANT'")
               if json.loads(row[0]).get("draft_id") == source["draft_id"]]
    _require(len(granted) < MAX_REINSPECTION_GRANTS, "REINSPECTION_GRANTS_EXHAUSTED")
    grant_id = _record(database, "REINSPECTION_GRANT", identity, {
        "kind": "REINSPECTION_GRANT", "draft_id": source["draft_id"], "previous_task_id": current,
        "reason": reason, "granted_at": now.isoformat(), "grant_number": len(granted) + 1}, now)
    task_id = outreach_agent.enqueue_inspection(database, latest, draft, source, now)
    _require(task_id != current, "REINSPECTION_TASK_NOT_CREATED")
    return {"state": "REINSPECTION_GRANTED", "grant": grant_id, "previous_task": current, "inspection_task": task_id}


def run(database, spec, config, now, apply=False, spec_sha256=None, reinspect=None):
    import outreach_agent

    report = {"mode": "APPLY" if apply else "DRY_RUN_ROLLED_BACK"}
    database.commit()
    database.execute("BEGIN IMMEDIATE")
    try:
        promotion.initialize_owner_records(database)   # transactional DDL: rolled back by a dry run
        identity = research.canonical(database, "contact", spec["profile"])
        latest, scaffold_row = _latest_scaffold(database, identity)
        scaffold = json.loads(scaffold_row["payload"])
        _require(research.name_key(scaffold["recipient"]) == research.name_key(spec["recipient"])
                 and research.canonical(database, "company", scaffold["company"]) == research.canonical(database, "company", spec["company"]),
                 "APPROVAL_DOES_NOT_MATCH_RESEARCH_DRAFT")
        attestation, approval = spec["attestation"], spec["approval"]
        _require(attestation.get("verified_identity") is True and attestation.get("never_contacted") is True
                 and approval.get("field") == "invitation" and isinstance(approval.get("text"), str)
                 and all(isinstance(value, str) and value.strip() for value in
                         (attestation.get("instruction"), approval.get("instruction"), spec.get("title"))),
                 "OWNER_ATTESTATION_OR_APPROVAL_INCOMPLETE")
        text = approval["text"]
        opportunity = next(promotion._bound_opportunity(database, scaffold, scaffold["team_need_evidence"],
                                                        scaffold["candidate_claim"]), None)
        _require(opportunity is not None, "DRAFT_SOURCE_BINDING_UNVERIFIED")
        resume = promotion.resume_context()
        attestation_payload = {"kind": "RECIPIENT_ATTESTATION", "profile": research.profile_key(spec["profile"]),
                               "recipient": spec["recipient"], "company": spec["company"], "title": spec["title"],
                               "verified_identity": True, "never_contacted": True,
                               "instruction": attestation["instruction"], "attested_at": attestation["attested_at"],
                               "supersedes_research_evidence": scaffold["draft_id"]}
        sources = promotion.composition_sources(database, scaffold, opportunity, resume, attestation_payload)
        gates = promotion.text_gates(text, None)
        report.update(text=text, text_sha256=hashlib.sha256(text.encode()).hexdigest(), characters=len(text),
                      limit=promotion.INVITATION_LIMIT, text_gates=gates,
                      trace=[dict(fact, source_text=" ".join(promotion._fact_source(sources, fact).split())[:240]
                                  if promotion._fact_source(sources, fact) else None) for fact in approval.get("facts", [])])
        # Recorded in `text_gates` above, never a refusal (owner, 2026-09-23: "Remove it all.
        # I dont care."). This text is the OWNER'S OWN, approved by them for this recipient, so
        # refusing it over a traceability comparison was Python overruling the person it was
        # meant to protect. The text is trimmed to the provider's limit instead.
        text = promotion.fit_text(text, promotion.INVITATION_LIMIT)
        report.update(text=text, text_sha256=hashlib.sha256(text.encode()).hexdigest(), characters=len(text))
        _require(bool(text.strip()), "APPROVED_TEXT_EMPTY")
        if "owner_approval" in json.loads(latest["payload"]):
            previous = json.loads(latest["payload"])["owner_approval"]
            _require(previous.get("text_sha256") == report["text_sha256"], "A_DIFFERENT_OWNER_APPROVAL_IS_CURRENT")
            report["state"] = "ALREADY_APPROVED"
            if reinspect:
                report.update(reinspect_grant(database, config, latest, identity, reinspect, now))
                if apply and report["state"] == "REINSPECTION_GRANTED":
                    database.commit()
                    return report
            database.rollback()
            return report
        correction = self_reference_correction(database, identity, scaffold, now)
        report["self_reference_correction"] = correction
        if correction:
            _require(spec.get("correct_self_reference") is True, "SELF_REFERENCE_HISTORICAL_FLAG_REQUIRES_CORRECTION")
            report["correction_record"] = _record(database, "SELF_REFERENCE_CORRECTION", identity, correction, now)
            database.execute("UPDATE rw_contacts SET historical=0 WHERE identity=? AND company=?", (identity, correction["company"]))
            if correction["rw_baselines_count"][0] is not None:
                database.execute("UPDATE rw_baselines SET count=? WHERE company=?",
                                 (correction["rw_baselines_count"][1], correction["company"]))
        attestation_id = _record(database, "RECIPIENT_ATTESTATION", identity, attestation_payload, now)
        approval_payload = {"kind": "DRAFT_APPROVAL", "field": "invitation", "text": text,
                            "text_sha256": report["text_sha256"], "facts": approval["facts"],
                            "instruction": approval["instruction"], "approved_at": approval["approved_at"],
                            "attestation_id": attestation_id, "supersedes": scaffold["draft_id"],
                            "resume_text_sha256": resume["text_sha256"], "resume_sha256": resume["source_sha256"],
                            "approval_file_sha256": spec_sha256}
        approval_id = _record(database, "DRAFT_APPROVAL", identity, approval_payload, now)
        payload = promotion.superseding_payload(scaffold, invitation=text, first_message=None, owner_approval={
            "approval_id": approval_id, "attestation_id": attestation_id, "text_sha256": report["text_sha256"]})
        created = promotion.insert_version(database, scaffold_row["identity"], payload, now)
        row = database.execute("SELECT * FROM rw_drafts WHERE identity=? AND version=?",
                               (scaffold_row["identity"], created["version"])).fetchone()
        draft, checked_identity, verification = promotion._validate(database, row, now, require_composition=True)
        promotion._local_gates(database, config, draft, checked_identity)
        source = promotion._source(row)
        task_id = outreach_agent.enqueue_inspection(database, row, draft, source, now)
        probe = {"action_id": "research-outreach:" + research.digest([checked_identity, "PREPARE_CAMPAIGN"]),
                 "profile": research.profile_key(draft["profile"]), "recipient": draft["recipient"], "company": draft["company"],
                 "purpose": "PREPARE_CAMPAIGN", "message": draft["invitation"], "channel": "linkedin",
                 "provider": "heyreach", "verification": verification, "source": source}
        report.update(state="APPROVED_AND_QUEUED_FOR_GUARDED_PATH" if apply else "WOULD_APPROVE_AND_QUEUE",
                      records={"attestation": attestation_id, "approval": approval_id},
                      superseded_draft=scaffold["draft_id"], new_draft={"draft_id": created["draft_id"], "version": created["version"],
                                                                        "invitation": created["invitation"]},
                      inspection_task=task_id, verification=verification,
                      contact_usage=outreach.contact_usage(database, draft["company"], draft, config.get("pipeline")),
                      send_guard_without_fresh_history=outreach.action_gates(database, config, probe, {}, now),
                      guarded_path=["outreach-draft-inspection: local-records history (owner, 2026-09-29; "
                                    "max age outreach_history_max_age_seconds) and AI READY/route decision",
                                    "outreach_campaign.prepare_candidates: single-contact list for route INVITATION",
                                    "outreach_promotion.cycle: PREPARE_CAMPAIGN action with this exact text (<=300)",
                                    "new_contact_execution: claimed DRAFT campaign creation, then handoff with fresh inspector proof",
                                    "outreach.dispatch: provider gates + claim_send immediately before launch"])
        if apply:
            database.commit()
        else:
            database.rollback()
        return report
    except BaseException as error:
        database.rollback()
        report.update(state="REFUSED", blocker=str(error) if isinstance(error, ValueError) else type(error).__name__)
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("approval")
    parser.add_argument("--db", default=str(paths.database()))
    parser.add_argument("--config", default=str(resources.path("config.json")))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--now")
    parser.add_argument("--reinspect", metavar="REASON", help="grant one fresh inspection for an approved note whose inspection is EXHAUSTED")
    arguments = parser.parse_args(argv)
    spec = json.loads(Path(arguments.approval).read_text())
    config = json.loads(Path(arguments.config).read_text())
    now = research.timestamp(arguments.now) if arguments.now else dt.datetime.now(dt.timezone.utc)
    spec_sha256 = hashlib.sha256(Path(arguments.approval).read_bytes()).hexdigest()
    database = sqlite3.connect(arguments.db, timeout=30)
    database.row_factory = sqlite3.Row
    try:
        report = run(database, spec, config, now, apply=arguments.apply, spec_sha256=spec_sha256, reinspect=arguments.reinspect)
    finally:
        database.close()
    report["approval_file_sha256"] = spec_sha256
    # Machine contract (E2.T6): the JSON report; stays print.
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
    return 0 if report["state"] not in {"REFUSED"} else 2


if __name__ == "__main__":
    sys.exit(main())
