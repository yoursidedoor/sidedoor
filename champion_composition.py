"""Read-only champion context and structural checks, never a sender.

The summary's resume and role quotes are recorded, not checked against their sources
(owner, 2026-09-27). The existing fresh AI send assessment judges the message.
"""

import datetime as dt
import json
import sqlite3

import pipeline_routing as routing


MAX_TEXT_CHARACTERS = 2500
MAX_OUTPUT_CHARACTERS = 12000

INDEPENDENT_REFERRAL_INSTRUCTIONS = """
Applications and referrals progress independently. Do not instruct the candidate
to wait for a referral before applying. A submitted, duplicate, blocked, abandoned
or uncertain application does not by itself prevent an authenticated advocate
agreement, grounded champion summary or otherwise permitted referral follow-up.
Use the supplied application evidence truthfully: mention already applying only
when exact-role evidence supports it. A saved state alone is not a receipt, a
duplicate marker may describe a different prior process, and an uncertain claim
must remain unverified. Never imply no application exists from missing history.
A referral or summary is not a second application and never permits resubmission
of an existing or uncertain application. Keep the referral next action independent
of application readiness. Preserve sender authorization, recipient closure and
inactivity rules, timing/contact caps, and duplicate message guards. Prior contact
never becomes permission for fresh cold outreach; respond only in the authorized
existing conversation or an independently eligible follow-up slot.
"""


def application_context(database, job):
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    current = routing.one(database, "SELECT * FROM application_jobs WHERE job_key=?", (job["job_key"],)) if "application_jobs" in tables else None
    context = {"job_key": job["job_key"], "basis": "SAVED_APPLICATION_EVIDENCE_NOT_A_FRESH_PROVIDER_CHECK",
               "saved_state": current["state"] if current else "UNKNOWN", "blocker": (current or {}).get("blocker"),
               "receipt": None, "submission_claims": None,
               "application_blocks_referral": False, "referral_blocks_application": False}
    context["application_interview_history"] = history_context(database, current) if current else None
    if current and current.get("receipt"):
        try:
            context["receipt"] = json.loads(current["receipt"])
        except (ValueError, TypeError):
            context["receipt_state"] = "UNREADABLE_SAVED_EVIDENCE"
    if "application_attempts" in tables:
        context["submission_claims"] = routing.rows(database,
            "SELECT attempt_id,state,started_at,evidence FROM application_attempts WHERE job_key=? ORDER BY started_at,attempt_id",
            (job["job_key"],))
    return context


def role_context(database, binding, now):
    """Use only a current _bindings result and its matching conversion evidence."""
    proofs = binding["role_proofs"]
    job = binding["job"]
    source_key = routing.digest([routing.VERSION, "rw_results", binding["rw_result_id"],
                                 str(binding["opportunity_index"])])
    # Built by the same function that wrote the record, so the equality below cannot fail merely
    # because two copies of this dict were edited apart.
    facts = routing.role_facts(job, proofs, source_key)
    evidence_id = "routing-role:" + source_key
    try:
        record = routing.conversion._evidence(database, evidence_id, "ROLE_CONTEXT", now)
        if (record["facts"] != facts or record["source_ref"] != binding["role"]["url"]
                or routing.conversion._role_id(database, job) != evidence_id):
            evidence_id = None
    except (ValueError, KeyError, TypeError):
        evidence_id = None
    # The model's "same posting?" verdict (posting_judge, owner 2026-09-24). Only an explicit,
    # quoted DIFFERENT withholds the evidence: a referral summary must not be written from an
    # official page that describes another posting. No verdict yet, SAME and UNSURE change
    # nothing - the judgment never blocks on its own absence.
    if evidence_id is not None:
        import posting_judge
        try:
            if posting_judge.verdict_for(database, evidence_id) == "DIFFERENT":
                evidence_id = None
        except Exception:
            pass
    return {"facts": facts, "evidence_id": evidence_id}


# Jobs on file that are not real roles for the owner at that employer.
NOT_OPEN_JOB_STATES = frozenset({"INELIGIBLE", "DUPLICATE"})


def _company_key(database, value):
    try:
        if database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='rw_aliases'").fetchone():
            return routing.research.canonical(database, "company", value)
        return routing.research.name_key(value)
    except (ValueError, TypeError, AttributeError):
        return None


def conversation_companies(database, profile):
    """The employers this person is on file for: research contacts, history and our own sends.

    Owner, 2026-09-30 ("Fix the bug in 'advocate summaries' the role scope bug"). job_scope was
    built only from research roles whose `people` list names this exact profile. On live data no
    contacted person appears in any such list (the 28 people we invited were found by the contact
    finder and recorded in rw_contacts), so every reply packet had an empty job_scope and no
    advocate summary could ever be written. The company is what we actually record per person.
    """
    import engagement

    try:
        identities = sorted(engagement.identity_keys(database, profile))
    except (ValueError, TypeError):
        return set()
    if not identities:
        return set()
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    placeholders = ",".join("?" for unused in identities)
    companies = set()
    if "rw_contacts" in tables:
        companies.update(row[0] for row in database.execute(
            "SELECT company FROM rw_contacts WHERE identity IN (" + placeholders + ")", identities))
    if "history" in tables:
        for company, identity, profile_url in database.execute("SELECT company,identity,profile FROM history"):
            try:
                linked = identity in identities or (profile_url and routing.research.profile_key(profile_url) in identities)
            except (ValueError, TypeError):
                linked = False
            if linked:
                companies.add(company)
    if "outreach_actions" in tables:
        for (payload,) in database.execute(
                "SELECT payload FROM outreach_actions WHERE identity IN (" + placeholders + ")", identities):
            try:
                action = json.loads(payload)
            except (ValueError, TypeError):
                continue
            if action.get("purpose") != "REPLY":
                companies.add(action.get("company"))
    return {company for company in companies if isinstance(company, str) and company.strip()}


def company_role_context(database, job):
    """Role facts for a job on file: its conversion ROLE_CONTEXT evidence when one exists."""
    try:
        payload = json.loads(job["payload"]) if job.get("payload") else {}
    except (ValueError, TypeError):
        payload = {}
    facts = {"employer": job["employer"], "requisition": job["requisition"],
             "title": payload.get("role"), "url": payload.get("url"), "source": "application_jobs"}
    evidence_id = None
    try:
        role = routing.one(database, "SELECT evidence_id FROM conversion_roles WHERE job_key=?", (job["job_key"],))
        if role:
            record = routing.conversion._evidence(database, role["evidence_id"], "ROLE_CONTEXT")
            facts, evidence_id = record["facts"], role["evidence_id"]
            import posting_judge
            try:
                if posting_judge.verdict_for(database, evidence_id) == "DIFFERENT":
                    evidence_id = None
            except Exception:
                pass
    except (ValueError, KeyError, TypeError, sqlite3.Error):
        pass
    return {"facts": facts, "evidence_id": evidence_id}


def company_jobs(database, profile):
    """Open jobs on file at the employers this conversation belongs to, keyed by job_key."""
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "application_jobs" not in tables:
        return {}
    wanted = {_company_key(database, company) for company in conversation_companies(database, profile)} - {None, ""}
    if not wanted:
        return {}
    return {job["job_key"]: job for job in routing.rows(database, "SELECT * FROM application_jobs ORDER BY job_key")
            if job["state"] not in NOT_OPEN_JOB_STATES and _company_key(database, job["employer"]) in wanted}


def candidate_profile(config):
    profile = config.get("sender_profile")
    try:
        if not isinstance(profile, str):
            return None
        routing.research.profile_key(profile)
    except ValueError:
        return None
    return {"profile_url": profile, "identity_provenance": "config.sender_profile"}


def history_context(database, job):
    """Read saved identity history; missing entries do not prove no history."""
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    history = {"evidence_basis": "SAVED_IDENTITY_RECORDS_NOT_A_FRESH_HISTORY_CHECK",
               "application_history": [], "interviews": []}
    if "application_history" in tables:
        for record in routing.conversion._identity_records(database, "application_history", job):
            record["evidence"] = json.loads(record["evidence"])
            history["application_history"].append(record)
    if "conversion_interviews" in tables:
        for record in routing.rows(database, "SELECT * FROM conversion_interviews WHERE job_key=? ORDER BY stage_id",
                                   (job["job_key"],)):
            record["source_evidence"] = []
            for field in ("evidence_id", "debrief_evidence_id", "timeline_evidence_id"):
                if record.get(field):
                    record["source_evidence"].append(routing.conversion._evidence(database, record[field]))
            history["interviews"].append(record)
    return history


def direct_intent(decision, request):
    """Distinguish saved AI speaker judgment from unchanged provider metadata."""
    if "speaker_context" in decision and decision["speaker_context"] != "DIRECT_SPEAKER":
        return False
    try:
        inbox = json.loads(request["source_snapshot"]["payload"])["inbox"]
    except (ValueError, KeyError, TypeError):
        return False
    flags = (inbox.get("quoted"), inbox.get("forwarded"))
    if all(flag is False for flag in flags):
        return True
    # The quote-span match that stood here was removed (owner, 2026-09-27); the speaker
    # judgment below is the model's, bound to this exact source.
    if not request.get("source_sha256") or decision.get("source_sha256") != request["source_sha256"]:
        return False
    return (all(flag is None or flag is False for flag in flags)
            and request.get("champion_handoff_enabled") is True
            and decision.get("speaker_context") == "DIRECT_SPEAKER"
            and decision.get("label") in {"AGREEMENT", "DECLINE"}
            and not request.get("context_issues")
            and not {"QUOTED_CONTEXT", "FORWARDED_CONTEXT", "AGREEMENT_INTENT"}.intersection(
                decision.get("unknown_factual_requirements", [])))


def validate(decision, request, can_advance_conversion):
    """Validate optional metadata without modifying legacy immutable decisions."""
    champion = decision.get("champion_handoff")
    if champion is None:
        return
    if (not isinstance(champion, dict) or set(champion) != {"job_key", "resume_quotes", "role_quotes"}
            or not isinstance(champion["job_key"], str) or not champion["job_key"].strip()):
        raise ValueError("CHAMPION_HANDOFF_SCHEMA_INVALID")
    if request.get("champion_handoff_enabled") is not True:
        raise ValueError("CHAMPION_HANDOFF_DISABLED")
    if (decision["label"] != "AGREEMENT" or can_advance_conversion is not True
            or decision["unknown_factual_requirements"]
            or decision["role_reference"] != champion["job_key"]):
        raise ValueError("CHAMPION_EXACT_AGREEMENT_REQUIRED")
    matches = [item for item in request.get("job_scope", []) if item["job_key"] == champion["job_key"]]
    if len(matches) != 1:
        raise ValueError("CHAMPION_SINGLE_VERIFIED_ROLE_REQUIRED")
    if not direct_intent(decision, request):
        raise ValueError("CHAMPION_DIRECT_MESSAGE_REQUIRED")
    role = matches[0].get("role_context") or {}
    if not isinstance(role.get("evidence_id"), str) or not role["evidence_id"].strip():
        raise ValueError("CHAMPION_ROLE_EVIDENCE_REQUIRED")
    proposal = decision["reply_proposal"]
    if (not isinstance(proposal, dict) or not isinstance(proposal.get("text"), str)
            or not proposal["text"].strip() or len(proposal["text"]) > MAX_TEXT_CHARACTERS):
        raise ValueError("CHAMPION_REPLY_PROPOSAL_REQUIRED")
    try:
        decision_time = dt.datetime.fromisoformat(request["decision_time"])
        review_at = dt.datetime.fromisoformat(decision["review_at"])
        if (decision_time.tzinfo is None or review_at.tzinfo is None
                or not decision_time < review_at <= decision_time + dt.timedelta(days=7)):
            raise ValueError()
    except (ValueError, KeyError, TypeError, OverflowError):
        raise ValueError("CHAMPION_BOUNDED_REVIEW_TIME_REQUIRED") from None
    if not isinstance(decision.get("next_action"), str) or not 1 <= len(decision["next_action"].strip()) <= 500:
        raise ValueError("CHAMPION_NEXT_ACTION_REQUIRED")
