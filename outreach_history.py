"""New-contact history from our own local records (owner, 2026-09-29).

"Remove the LinkedIn history check for outreach all together ... Lets outreach depend on our
own local data storage to assert what it needs." No browser read runs. `local_findings` reads
the imported contact history, suppressions, send attempts, managed conversations, engagement events and saved findings; `collect` turns that into the proof the outreach
judge and gates consume. For INITIAL and PREPARE_CAMPAIGN the local records were checked once,
before the draft was created (owner, 2026-10-01), and `collect` does not read them again.
`error_code` is still used by outreach_history_browser.
"""

import hashlib
import json
import re

import engagement
import outreach
from orchestrator import profile_key


POSITIVE_FIELDS = ("prior_outreach", "pending_invitation", "has_reply", "negative_reply", "opt_out")


def error_code(error):
    message = str(error)
    if isinstance(error, ValueError) and re.fullmatch(r"(?:BROWSER_HISTORY|HISTORY|COLLECTOR)_[A-Z_]{1,100}", message):
        return message
    return type(error).__name__


def local_findings(database, profile, recipient=None, purpose=None):
    """What our own records say about contact with this person (owner, 2026-09-29).

    Owner: "Remove the LinkedIn history check for outreach all together ... Lets outreach depend
    on our own local data storage to assert what it needs." Sources: the imported contact history
    (recovered tracker and handoff), suppressions, send attempts, engagement events (invitations
    sent, acceptances, messages, replies), stop state, and positive findings saved from earlier
    reads. Live action/claim rows are guarded separately by outreach.action_gates.
    """
    identities = engagement.identity_keys(database, profile)
    placeholders = ",".join("?" for unused in identities)
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    sources = []
    if "history" in tables:
        import research_workflow
        name = research_workflow.name_key(recipient) if recipient else None
        for row in database.execute("SELECT identity,recipient,profile FROM history"):
            try:
                keyed = row[2] and profile_key(row[2]) in identities
            except (ValueError, TypeError):
                keyed = False
            if row[0] in identities or keyed or (name and research_workflow.name_key(row[1]) == name):
                sources.append("history:" + str(row[0]))
    if "suppressions" in tables and database.execute(
            "SELECT 1 FROM suppressions WHERE identity IN (" + placeholders + ")", identities).fetchone():
        sources.append("suppressions")
    attempted = "outreach_attempts" in tables and bool(database.execute(
        "SELECT 1 FROM outreach_attempts WHERE identity IN (" + placeholders + ")", identities).fetchone())
    if attempted:
        sources.append("outreach_attempts")
    # Only earlier conversations: the queue and launch claims hold THIS send's own rows just before
    # launch (outreach.action_gates already checks them with that exclusion).
    for table in ("managed_conversations",):
        columns = {row[1] for row in database.execute("PRAGMA table_info(" + table + ")")} if table in tables else set()
        if "identity" in columns and database.execute(
                "SELECT 1 FROM " + table + " WHERE identity IN (" + placeholders + ")", identities).fetchone():
            sources.append(table)
    kinds = {event.get("kind") for event in engagement.events_for(database, profile) if isinstance(event, dict)}
    invited = attempted or "initial_sent" in kinds
    previous = saved_positive(database, profile)
    findings = {"prior_outreach": bool(sources) or bool(kinds & {"initial_sent", "outbound_observed"}),
                "pending_invitation": invited and "accepted" not in kinds,
                "has_reply": "inbound" in kinds,
                "negative_reply": False,
                "opt_out": engagement.stop_state(database, profile) == "OPTED_OUT"}
    # The first message after an accepted invitation follows OUR invitation by design, so that
    # invitation is not a blocking prior contact, and whether it is still pending is read from
    # the acceptance on record now, never from an older saved finding.
    first_message = purpose == "FIRST_MESSAGE"
    if first_message:
        findings["prior_outreach"] = None
    if previous:
        for field, value in previous["findings"].items():
            if value is True and not (first_message and field in {"prior_outreach", "pending_invitation"}):
                findings[field] = True
        sources.append(previous["evidence"])
    return findings, sources + sorted("engagement_events:" + kind for kind in kinds if kind)


# New-contact purposes whose local-records check ran once, before the draft was created
# (research_workflow.reserve_contact -> prior_contact_record), and is not repeated at send time.
PRE_DRAFT_CHECKED = frozenset({"INITIAL", "PREPARE_CAMPAIGN", "INVITE"})


def collect(database, config, action, now, collector=None, *, clock=None):
    """Local-records history for a new-contact action. The LinkedIn browser read is removed
    (owner, 2026-09-29); `collector` is ignored and kept only so callers need not change.

    INITIAL and PREPARE_CAMPAIGN (owner, 2026-10-01: "We shouldn't be checking at send time.
    that's too late. we should be checking at draft time or even before a draft is created."):
    our records were checked once, before the draft was created, so no local history is read
    here and the findings state what that check found (nothing, or there would be no draft).
    What can change after the draft is still guarded at send time by outreach.action_gates (our
    own claims, actions, attempts and send events, inbound and outbound messages, stops, closure,
    exclusions) and by what HeyReach shows. FIRST_MESSAGE keeps the local read.
    """
    if action.get("purpose") in PRE_DRAFT_CHECKED:
        findings = {field: False for field in POSITIVE_FIELDS}
        sources = ["pre-draft-check:research_workflow.prior_contact_record"]
    else:
        findings, sources = local_findings(database, action["profile"], action.get("recipient"), action.get("purpose"))
    evidence = "local-records:" + hashlib.sha256(json.dumps(
        {"identity": profile_key(action["profile"]), "findings": findings, "sources": sources},
        sort_keys=True).encode()).hexdigest()
    proof = {"identity": profile_key(action["profile"]), "sender_id": config["sender_id"],
             "checked_at": now.isoformat(), "evidence": evidence,
             "direct_messages_complete": True, "sent_invitations_complete": True,
             "history_basis": "PRE_DRAFT_LOCAL_CHECK" if action.get("purpose") in PRE_DRAFT_CHECKED else "LOCAL_RECORDS",
             "local_sources": sources, **findings}
    usage = outreach.contact_usage(database, action["company"], action, config.get("pipeline"))
    proof.update(company_contact_count=max(usage["historical_lower_bound"], usage["distinct_historical"]),
                 company_count_evidence="canonical-ledger:" + hashlib.sha256(json.dumps(usage, sort_keys=True).encode()).hexdigest())
    return {"request": {"profile": action["profile"], "sender_id": config["sender_id"], "source": "local-records"},
            "proof": proof, "capability_gates": [], "company_ledger_observation": usage,
            "next_action": "Apply fresh AI judgment and the existing shared action guard"}


def saved_positive(database, profile):
    if not database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='outreach_agent_audit'").fetchone():
        return None
    identities = engagement.identity_keys(database, profile)
    placeholders = ",".join("?" for unused in identities)
    rows = database.execute("SELECT id,record FROM outreach_agent_audit WHERE kind='HISTORY' "
                            "AND json_extract(record,'$.identity') IN (" + placeholders + ")", identities)
    # Every positive row is combined (2026-09-29): with the FIRST_MESSAGE exception a first row
    # saying only prior_outreach must not hide a later reply or opt-out.
    combined, evidence = {}, None
    for row in rows:
        findings = json.loads(row[1]).get("findings", {})
        positive = {field: True for field in POSITIVE_FIELDS if findings.get(field) is True}
        if positive:
            combined.update(positive)
            evidence = evidence or "outreach_agent_audit:" + row[0]
    return {"evidence": evidence, "findings": combined} if combined else None


def blocking_positive(database, profile, purpose=None):
    """A saved positive finding that blocks this purpose, or None.

    For the first message after an accepted invitation, our own invitation (prior_outreach /
    pending_invitation) is expected and does not block; a reply, negative reply or opt-out does.
    """
    previous = saved_positive(database, profile)
    if not previous or purpose != "FIRST_MESSAGE":
        return previous
    if any(previous["findings"].get(field) is True for field in ("has_reply", "negative_reply", "opt_out")):
        return previous
    return None


