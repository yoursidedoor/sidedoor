"""Offline rw_drafts -> outreach_actions promotion, independent of applications.

cycle returns items and mappings; each mapping identifies the exact rw_drafts
identity/version/content_hash and its durable action_id. draft_id preserves the
current research/strategy ID (older schemas use a prefixed source identifier).
strategy_draft_id is explicit (None for legacy drafts); it is provenance only (the
learning feature that bound actions to it was removed, owner 2026-10-22).

Verified acceptance events can select FIRST_MESSAGE under engagement.next_action.
Optional config['outreach_promotion_contexts'] maps canonical profile URLs to
{purpose, proof, conversation_id} or {purpose='PREPARE_CAMPAIGN', proof, list_id,
campaign_name, sequence}. proof uses outreach.action_gates' existing inspector
contract. INITIAL needs a fresh local-records history proof. Preparation
needs an observed single-contact list/sequence; no provider identifiers are made
up. Missing execution proof continues to block the existing dispatcher.

Draft kinds: TEMPLATE (legacy research scaffold, rejected once
config['outreach_agent']['compose'] is true), AI_COMPOSED (outreach_agent.compose_draft)
and OWNER_APPROVED (outreach_owner_approval.py). The latter two supersede a research
scaffold, which still carries the source/reservation binding. Their cited facts are recorded,
not compared: the AI send judge is the only check on a note (owner, 2026-10-21, E4.T2:
`trace_gates` removed).

Only this module's unclaimed pending/blocked/cancelled actions can be refreshed.
Invalidated source versions cancel those actions until valid evidence resumes.
Claims and uncertain/terminal actions are never rewritten or released.
"""

import hashlib
import json
from pathlib import Path
import re
import sqlite3

import approvals
import engagement
import outreach
import owner_seed
import outreach_history
import research_workflow as research
import user_profile
import paths


MUTABLE_STATES = {"PENDING", "BLOCKED", "CANCELLED"}
ROOT = Path(__file__).resolve().parent
SOURCE_KIND = "rw_drafts"


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _source(row):
    source = {"kind": SOURCE_KIND, "identity": row["identity"],
              "version": row["version"], "content_hash": row["content_hash"]}
    try:
        payload = json.loads(row["payload"])
    except (ValueError, TypeError):
        payload = {}
    strategy_draft_id = payload.get("draft_id") if isinstance(payload, dict) else None
    # AI-composed and owner-approved versions are not template-experiment variants.
    experiment = isinstance(payload, dict) and not ({"composition", "owner_approval"} & set(payload))
    return dict(source, draft_id=strategy_draft_id or "rw_drafts:" + research.digest(source),
                strategy_draft_id=strategy_draft_id if experiment else None)


# ---------------------------------------------------------------------------
# Composition guards (owner decision 2026-09-19): outreach text is written by the
# AI outreach agent or approved verbatim by the owner; Python only enforces hard
# lines on the structured output. Nothing here judges meaning or tone.
# ---------------------------------------------------------------------------
INVITATION_LIMIT = 300          # LinkedIn connection-note cap; also outreach_campaign.invitation_sequence
FIRST_MESSAGE_LIMIT = 2000
RESUME_LINE_WINDOW = 8


def verified_location():
    """The user's verified current location: profile.json `answers.current_location.value`.

    None without a profile or a recorded location (then no location is offered as a fact).
    """
    try:
        policy = user_profile.load()
    except user_profile.ProfileMissing:
        return None
    record = (policy.get("answers") or {}).get("current_location") or {}
    value = record.get("value") if isinstance(record, dict) else None
    if isinstance(record, dict) and record.get("semantic"):
        # The answer in force for that semantic: one the owner kept from Approvals for it is newer
        # (`applications.answers_in_force`; owner, 2026-10-24, "every single worker").
        import applications
        value = applications.policy_answer(policy, record["semantic"], value)
    return value if isinstance(value, str) and value.strip() else None
OWNER_RECORD_KINDS = ("RECIPIENT_ATTESTATION", "DRAFT_APPROVAL", "SELF_REFERENCE_CORRECTION")


def composition_sources(database, draft, opportunity, resume, owner_attestation=None, posting_text=None):
    """Structured facts a note may cite: resume lines, verified claims, role, recipient, location."""
    claims = {}
    designated = database.execute("SELECT value FROM rw_meta WHERE key='designated_resume'").fetchone()
    for claim_id, payload in database.execute("SELECT id,payload FROM rw_claims ORDER BY id"):
        claim = json.loads(payload)
        if designated and claim.get("source_sha256") == designated[0] and claim.get("verified") is True:
            claims[claim_id] = [claim["text"], claim.get("invitation_mapping", {}).get("text", claim["text"])]
    facts = opportunity["review"]["facts"]
    # The packet shows this as `role.title` (outreach_agent.composition_packet), so `title` is
    # what the composer cites. The citable list only had `role`, so every note naming the role
    # was refused with FACT_n_SOURCE_REF_UNKNOWN (owner, 2026-09-23). Same value, both names.
    role = {"role": opportunity.get("role"), "title": opportunity.get("role"),
            "company": opportunity.get("company"),
            **{field: facts.get(field) for field in ("team_need", "level", "work_mode", "city", "specialization", "industry")}}
    evidence = research.current_evidence(draft.get("person_evidence", {}))
    recipient = {"name": draft["recipient"], "first_name": draft["recipient"].split()[0], "company": draft["company"],
                 "contact_type": evidence.get("contact_type", {}).get("value"),
                 # Every field research already verified about this person is citable by its own
                 # name (owner, 2026-09-23: "we should not block an outreach on the recipient's
                 # team or Job title. The research subsys has already validated it for us").
                 # `person_evidence` holds identity, employment, team and ability_to_help, each
                 # dated with a source URL. The composer was shown them as
                 # `recipient.verified_evidence.team` (since 2026-10-01 `recipient.evidence.team`) and had no way to cite them: this list had
                 # no such key, so any note mentioning the recipient's team was rejected forever.
                 # Three of five people died exactly there.
                 # Value AND excerpt (owner, 2026-09-23). The packet shows each verified fact as
                 # {value, excerpt, source, observed_at}, so the composer quotes the EXCERPT -
                 # "Senior DevOps Engineer at Pawnee Health" - while this offered only the
                 # value, and the quote was refused as FACT_n_EXCERPT_NOT_IN_SOURCE though it
                 # was a faithful quotation of recorded evidence. Both are the same dated,
                 # sourced observation, so both are citable and neither is invented.
                 **{field: " ".join(part for part in (record.get("value"), record.get("excerpt"))
                                    if isinstance(part, str) and part.strip())
                    for field, record in evidence.items()
                    if isinstance(record, dict) and isinstance(record.get("value"), str)},
                 "evidence": " ".join(str(record.get("excerpt", "")) for record in evidence.values() if isinstance(record, dict))}
    if owner_attestation:
        recipient.update(title=owner_attestation.get("title"), company=owner_attestation.get("company"))
    return {"resume": {"lines": resume["text"].split("\n"), "text": resume["text"],
                       "sha256": resume["source_sha256"]},
            "claim": claims,
            "role_fact": {key: value for key, value in role.items() if isinstance(value, str) and value.strip()},
            "recipient_fact": {key: value for key, value in recipient.items() if isinstance(value, str) and value.strip()},
            "candidate_fact": {key: value for key, value in {"location": verified_location()}.items() if value},
            "posting": {"posting": posting_text} if posting_text else {}}


def _fact_source(sources, fact):
    kind, reference = fact.get("source_kind"), fact.get("source_ref")
    if kind == "resume":
        lines = sources["resume"]["lines"]
        match = re.fullmatch(r"L(\d+)", reference or "")
        if not match or not 1 <= int(match.group(1)) <= len(lines):
            return None
        start = int(match.group(1)) - 1
        return " ".join(lines[start:start + RESUME_LINE_WINDOW])
    if kind == "claim":
        return " ".join(sources["claim"].get(reference) or []) or None
    if kind in {"role_fact", "recipient_fact", "candidate_fact", "posting"}:
        available = sources[kind]
        if reference in available:
            return available[reference]
        # The packet shows these nested - `role.facts.team_need`, `recipient.evidence.team`
        # - while this list is flat, so the model cited exactly what it was shown and the lookup
        # missed every time (owner, 2026-09-23). The last path segment is the field name, and it
        # is matched against the SAME closed list as before: an unresolvable reference still
        # returns None and the fact is still refused.
        # Try EVERY segment, not just the last (owner, 2026-09-23). The packet nests these as
        # `recipient.evidence.team.excerpt`, so the final segment is "excerpt" - a word
        # that is not a field - and the lookup missed while the meaningful segment, "team", sat
        # one place to its left. The segments are still matched against the SAME closed list, so
        # an unresolvable reference still returns None and the fact is still refused.
        for segment in reversed(str(reference or "").split(".")):
            if segment in available:
                return available[segment]
        return None
    return None


def fit_text(text, limit):
    """Trim generated text to what the provider accepts instead of refusing the note.

    Owner, 2026-09-23: "remove every single piece of code that can cause the outreach to fail
    after AI generation ... Remove it all. I dont care." An invitation one character over
    LinkedIn's 300 spent an attempt and sent nothing. The limit belongs to the provider, so the
    text is cut to it and the note goes out.

    Owner, 2026-10-23 ("Please fix this"): a cut mid-sentence went out ("Would the first few months
    focus more on agents or", Lambert Nguyen). The composer is now asked to shorten an over-long
    note itself (`outreach_agent._validate_composition`); what still needs a cut here ends at the
    last complete sentence that fits, and only a text with no sentence end inside the limit is cut
    at a word boundary.
    """
    if not isinstance(text, str):
        return text
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit + 1]
    ends = [match.end() for match in SENTENCE_END.finditer(cut) if match.end() <= limit]
    # A sentence end that keeps less than half the note ("Hi Lambert.") is no better than a word cut,
    # which the send judge then hands back to the writer as unfinished.
    if ends and ends[-1] >= limit // 2:
        return cut[:ends[-1]].rstrip()
    cut = text[:limit]
    return (cut.rsplit(" ", 1)[0] if " " in cut[max(0, limit - 30):] else cut).rstrip(" ,;:-")


# A sentence ends at ".", "?" or "!" (closing quotes or brackets after it included) followed by the end
# of the text or a space and a capital letter or digit, so "e.g. ranking" or "Microsoft. on" is not one.
SENTENCE_END = re.compile(r"[.?!][\"')\]\u2019\u201d]*(?=$|\s+[A-Z0-9\"'\u201c])")


def text_gates(invitation, first_message):
    gates = []
    if not isinstance(invitation, str) or not invitation.strip():
        gates.append("INVITATION_TEXT_MISSING")
    elif len(invitation) > INVITATION_LIMIT:
        gates.append("INVITATION_EXCEEDS_300_CHARACTERS")
    if first_message is not None and (not isinstance(first_message, str) or not first_message.strip()
                                      or len(first_message) > FIRST_MESSAGE_LIMIT):
        gates.append("FIRST_MESSAGE_SIZE_INVALID")
    if any("{" in text or "}" in text for text in (invitation, first_message) if isinstance(text, str)):
        gates.append("PLACEHOLDER_TEXT")
    return gates


# People verified by the logged-in LinkedIn read (linkedin_contacts.py) carry this evidence
# kind. Owner session instruction 2026-09-19: their outreach is never sent on AI text alone;
# each note is held until the owner approves its exact text (outreach_owner_approval.py).
LINKEDIN_EVIDENCE_KIND = "linkedin_member_view"


def linkedin_verified(draft):
    evidence = draft.get("person_evidence") if isinstance(draft, dict) else None
    return isinstance(evidence, dict) and any(isinstance(record, dict) and record.get("kind") == LINKEDIN_EVIDENCE_KIND
                                              for record in evidence.values())


# Owner decision 2026-09-19 (AGENTS.md "Owner approval for outreach"): the owner approves each
# AI-written outreach note to a new person before it is sent; general pre-approval does not
# cover sending. Every rw_drafts note is a first contact to a new person, so an AI_COMPOSED
# version is sendable only when an immutable DRAFT_APPROVAL owner record for this recipient
# covers its exact text (sha256) for every text it carries. Otherwise it is HELD and listed in
# reports/outreach-pending-owner-approval.json; the owner approves with outreach_owner_approval.py.
OWNER_APPROVAL_REQUIRED = "OWNER_APPROVAL_REQUIRED"
# Owner decision later on 2026-09-19 (AGENTS.md): per-note approval is no longer required.
OWNER_APPROVAL_PER_NOTE = False
PENDING_APPROVAL_REPORT = paths.REPORTS + "/outreach-pending-owner-approval.json"   # under the data folder


def _draft_kind(draft):
    return "OWNER_APPROVED" if "owner_approval" in draft else "AI_COMPOSED" if "composition" in draft else "TEMPLATE"


def owner_approved_text(database, identity, field, text):
    """An intact DRAFT_APPROVAL record for this recipient approving exactly this text."""
    if not isinstance(text, str) or not database.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='outreach_owner_records'").fetchone():
        return False
    sha = hashlib.sha256(text.encode()).hexdigest()
    for (record_id,) in database.execute("SELECT record_id FROM outreach_owner_records WHERE kind='DRAFT_APPROVAL' "
                                         "ORDER BY record_id").fetchall():
        approval = owner_record(database, record_id, "DRAFT_APPROVAL", identity)
        if approval and approval.get("field") == field and approval.get("text") == text and approval.get("text_sha256") == sha:
            return True
    return False


def owner_approval_missing(database, draft, identity):
    """True for an AI-composed note to a new person without owner approval of its exact text.

    Superseded by the owner, 2026-09-19 ("send AI-written outreach without my approval" -
    "confirm"): AI-composed notes no longer wait for per-note approval. Every other guard
    (composition text gates, fresh history, no repeat contact, contact caps) is unchanged.
    """
    if not OWNER_APPROVAL_PER_NOTE:
        return False
    if _draft_kind(draft) != "AI_COMPOSED":
        return False
    texts = {field: draft.get(field) for field in ("invitation", "first_message") if draft.get(field) is not None}
    return not texts or not all(owner_approved_text(database, identity, field, text) for field, text in texts.items())


def owner_approval_code(draft):
    return "OWNER_APPROVAL_REQUIRED_LINKEDIN_VERIFIED" if linkedin_verified(draft) else OWNER_APPROVAL_REQUIRED


def pending_owner_approvals(database):
    """Ready-to-approve packets for every latest AI-composed draft still lacking owner approval."""
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "rw_drafts" not in tables:
        return []
    reviews = []
    for identity, version, payload in database.execute(
            "SELECT identity,version,payload FROM rw_drafts AS draft "
            "WHERE version=(SELECT MAX(version) FROM rw_drafts WHERE identity=draft.identity) ORDER BY identity").fetchall():
        try:
            draft = json.loads(payload)
            if not isinstance(draft, dict) or not owner_approval_missing(
                    database, draft, research.canonical(database, "contact", identity)):
                continue
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
        evidence = draft.get("person_evidence") or {}
        title = (evidence.get("team") or {}).get("value") if isinstance(evidence.get("team"), dict) else None
        invitation, first_message = draft.get("invitation"), draft.get("first_message")
        reviews.append({
            "state": "HELD_FOR_OWNER_APPROVAL", "hold_code": owner_approval_code(draft),
            "recipient": draft.get("recipient"), "company": draft.get("company"), "title": title,
            "profile": draft.get("profile"), "linkedin_verified": linkedin_verified(draft),
            "draft_id": draft.get("draft_id"), "version": version,
            "invitation": invitation, "invitation_characters": len(invitation or ""),
            "invitation_sha256": hashlib.sha256(invitation.encode()).hexdigest() if isinstance(invitation, str) else None,
            "first_message": first_message, "first_message_characters": len(first_message or ""),
            "first_message_sha256": hashlib.sha256(first_message.encode()).hexdigest() if isinstance(first_message, str) else None,
            "approve_with": "python3.13 outreach_owner_approval.py <approval_template saved as a file, instruction/"
                            "attested_at/approved_at filled in> (dry run), then again with --apply",
            "approval_template": {
                "profile": draft.get("profile"), "recipient": draft.get("recipient"), "company": draft.get("company"),
                "title": title,
                "attestation": {"instruction": None, "attested_at": None, "verified_identity": True, "never_contacted": True},
                "approval": {"instruction": None, "approved_at": None, "field": "invitation",
                             "text": invitation, "facts": (draft.get("composition") or {}).get("facts")},
                "correct_self_reference": False}})
    return reviews


def write_pending_owner_approvals(root, database):
    """reports/outreach-pending-owner-approval.json (all held first-contact notes); returns the list."""
    reviews = pending_owner_approvals(database)
    path = Path(root) / PENDING_APPROVAL_REPORT
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(reviews, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)
    return reviews


DASH_CHARACTERS = {"\u2014": "EM_DASH", "\u2013": "EN_DASH"}


def without_dashes(text):
    """Replace em/en dashes rather than refusing the note over them (owner, 2026-09-23).

    The owner's style rule of 2026-09-19 is that generated outreach carries no em or en dash.
    Refusing a whole composed note over one character spent an attempt and sent nothing; the
    rule is about the text that goes out, so the text is corrected and the note proceeds.
    """
    if not isinstance(text, str):
        return text
    for character in DASH_CHARACTERS:
        text = text.replace(character, ", " if character in "\u2014\u2013" else "-")
    return " ".join(text.split())


def dash_gates(texts):
    """Owner style rule (2026-09-19): AI-generated outreach never contains em/en dashes."""
    return [f"{name}_IN_{field.upper()}" for field, text in texts.items() if isinstance(text, str)
            for character, name in DASH_CHARACTERS.items() if character in text]


def initialize_owner_records(database):
    database.execute("""CREATE TABLE IF NOT EXISTS outreach_owner_records (
        record_id TEXT PRIMARY KEY, kind TEXT NOT NULL, identity TEXT NOT NULL,
        recorded_at TEXT NOT NULL, payload TEXT NOT NULL, payload_sha256 TEXT NOT NULL)""")
    # E6.T4: the Approvals page reads one person's decisions (approvals.decisions with identity).
    database.execute("CREATE INDEX IF NOT EXISTS outreach_owner_records_identity ON outreach_owner_records(identity, kind)")
    for operation in ("UPDATE", "DELETE"):
        database.execute(f"CREATE TRIGGER IF NOT EXISTS outreach_owner_records_no_{operation.lower()} BEFORE {operation} "
                         "ON outreach_owner_records BEGIN SELECT RAISE(ABORT,'Immutable owner record'); END")


def owner_record(database, record_id, kind, identity):
    if not database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='outreach_owner_records'").fetchone():
        return None
    row = database.execute("SELECT kind,identity,payload,payload_sha256 FROM outreach_owner_records WHERE record_id=?",
                           (record_id,)).fetchone()
    if not row or row[0] != kind or hashlib.sha256(row[2].encode()).hexdigest() != row[3]:
        return None
    if research.canonical(database, "contact", row[1]) != research.canonical(database, "contact", identity):
        return None
    return json.loads(row[2])


def _bound_opportunity(database, origin, need, claim, *, require_topic=False):
    """Stored research opportunities for a draft, found by job identity.

    Owner, 2026-09-23: "doing a word for word python comparison here is guaranteed failure,
    no?" - yes. Two comparisons were removed:

    1. `origin not in processed["drafts"]` compared the ENTIRE draft dictionary, field for
       field, against the stored copy. Any difference at all - a draft rebuilt through a
       different route, a field added by a later patch - meant no match and the note died with
       DRAFT_SOURCE_BINDING_UNVERIFIED. It also scoped the search to the draft's own plan.
    2. The team-need evidence had to be equal as well, a second whole-dict comparison of a
       fact that is optional in the first place.

    What identifies a job is its job_key, its URL and its employer. Those three agreeing
    across two records means they ARE the same posting, so they are the whole binding now.
    require_topic keeps the legacy template rule (the role's recorded specialization is one of
    the scaffold claim's tags); AI-composed and owner-approved notes do not need it.
    """
    # Provenance still comes first: the draft's own plan, newest observation first, then any
    # other result carrying the same job. Scanning in plain id order would have bound today's
    # draft to a week-old copy of the same posting whenever both existed.
    for result in database.execute("SELECT processed FROM rw_results ORDER BY (plan_id=?) DESC, id DESC",
                                   (origin["plan_id"],)):
        processed = json.loads(result[0])
        for opportunity in processed.get("opportunities", []):
            if (opportunity.get("job_key") == origin["job_key"] and opportunity.get("url") == origin["source_url"]
                    and opportunity.get("company") == origin["company"]
                    and (not require_topic or opportunity["review"]["facts"].get("specialization") in claim["tags"])):
                yield opportunity


def _scaffold(database, row, draft):
    """The research draft an AI/owner version supersedes; it carries the source binding."""
    previous = database.execute("SELECT version,content_hash,payload FROM rw_drafts WHERE identity=? AND version<? ORDER BY version DESC",
                                (row["identity"], row["version"])).fetchall()
    for version, content_hash, payload in previous:
        origin = json.loads(payload)
        if origin.get("draft_id") == draft.get("supersedes"):
            _require(not ({"composition", "owner_approval"} & set(origin)), "SUPERSEDED_SOURCE_NOT_RESEARCH_DRAFT")
            for field in ("profile", "company", "recipient", "plan_id", "job_key", "source_url", "claim_id",
                          "candidate_claim", "invitation_claim_mapping", "team_need_evidence", "person_evidence"):
                _require(origin.get(field) == draft.get(field), "SUPERSEDED_SOURCE_MISMATCH")
            return origin
    raise ValueError("SUPERSEDED_SOURCE_MISSING")


def composition_required(config):
    """With outreach_agent.compose enabled, template text is never sendable."""
    return (config.get("outreach_agent") or {}).get("compose") is True


def resume_context():
    import reply_resume
    return reply_resume.context()


SCAFFOLD_ONLY_FIELDS = {"message", "first_message", "invitation", "invitation_blocker", "invitation_state",
                        "first_message_state", "first_message_limit", "provider_size_gate", "strategy_execution",
                        "segment", "template_version", "version", "created_at", "draft_id"}


def superseding_payload(scaffold, *, invitation, first_message, composition=None, owner_approval=None):
    """A new HELD version that keeps the scaffold's source binding but none of its template text."""
    _require((composition is None) != (owner_approval is None), "ONE_TEXT_AUTHORITY_REQUIRED")
    payload = {key: value for key, value in scaffold.items() if key not in SCAFFOLD_ONLY_FIELDS}
    payload.update(state="HELD_VERIFICATION", purpose="initial_linkedin_message", supersedes=scaffold["draft_id"],
                   invitation=invitation, first_message=first_message, message=first_message,
                   first_message_limit=FIRST_MESSAGE_LIMIT, invitation_state="HELD_VERIFICATION",
                   first_message_state="HELD_VERIFICATION" if first_message else "NOT_COMPOSED")
    if composition is not None:
        payload["composition"] = composition
    else:
        payload["owner_approval"] = owner_approval
    return payload


def insert_version(database, identity, payload, now):
    """Append the next immutable rw_drafts version (caller owns the transaction)."""
    content_hash = research.digest({field: value for field, value in payload.items() if field != "plan_id"})
    version = database.execute("SELECT COALESCE(MAX(version),0)+1 FROM rw_drafts WHERE identity=?", (identity,)).fetchone()[0]
    payload = dict(payload, version=version, created_at=research.timestamp(now).isoformat(),
                   draft_id=research.digest([identity, version, content_hash]))
    database.execute("INSERT INTO rw_drafts VALUES (?,?,?,?)", (identity, version, content_hash, research.encoded(payload)))
    return payload


def _validate(database, row, now, *, require_composition=False):
    draft = json.loads(row["payload"])
    _require(isinstance(draft, dict), "INVALID_DRAFT_SCHEMA")
    _require(draft.get("version") == row["version"] and type(row["version"]) is int,
             "DRAFT_VERSION_MISMATCH")
    content = {key: value for key, value in draft.items() if key not in {"plan_id", "version", "created_at", "draft_id"}}
    _require(research.digest(content) == row["content_hash"], "DRAFT_HASH_MISMATCH")
    if "draft_id" in draft:
        _require(draft["draft_id"] == research.digest([row["identity"], row["version"], row["content_hash"]]),
                 "DRAFT_ID_MISMATCH")
    _require(draft.get("state") == "HELD_VERIFICATION"
             and draft.get("purpose") == "initial_linkedin_message", "UNSUPPORTED_DRAFT_STATE_OR_PURPOSE")
    _require(research.timestamp(draft["created_at"]) <= now, "FUTURE_DRAFT")
    kind = _draft_kind(draft)
    _require(kind != "TEMPLATE" or not require_composition, "AI_COMPOSITION_REQUIRED")
    _require(kind != "AI_COMPOSED" or not linkedin_verified(draft) or not OWNER_APPROVAL_PER_NOTE,
             "OWNER_APPROVAL_REQUIRED_LINKEDIN_VERIFIED")
    identity = research.canonical(database, "contact", draft["profile"])
    _require(identity == research.canonical(database, "contact", row["identity"]), "DRAFT_IDENTITY_MISMATCH")
    # Held before any history-check compute or action (owner approval for outreach).
    _require(not owner_approval_missing(database, draft, identity), OWNER_APPROVAL_REQUIRED)
    company = research.canonical(database, "company", draft["company"])
    reserved = database.execute("SELECT historical,plan_id FROM rw_contacts WHERE identity=? AND company=?",
                                (identity, company)).fetchone()
    # RESERVATION_MISSING_OR_HISTORICAL deleted (owner, 2026-09-23). It demanded a live
    # rw_contacts row whose plan_id equalled the draft's, so any draft that outlived its plan
    # day, or was rebuilt through another route, became permanently unsendable - this is what
    # held one contact. The per-company limit it was standing in for is enforced where it
    # belongs: rw_slots at reservation time, 3 new people per research run (owner, 2026-09-28).
    seeded = database.execute("SELECT value FROM rw_meta WHERE key='history_seeded'").fetchone()
    _require(seeded and seeded[0] == "true", "SAVED_HISTORY_UNKNOWN")
    registered = database.execute("SELECT payload FROM rw_claims WHERE id=?", (draft["claim_id"],)).fetchone()
    _require(registered is not None, "CANDIDATE_CLAIM_UNKNOWN")
    claim = json.loads(registered[0])
    designated = database.execute("SELECT value FROM rw_meta WHERE key='designated_resume'").fetchone()
    _require(designated and claim.get("source_sha256") == designated[0], "DESIGNATED_RESUME_CHANGED")
    _require(claim == draft["candidate_claim"] and claim.get("verified") is True
             and claim.get("source") == "designated_resume" and research.digest(claim) == draft["claim_id"],
             "CANDIDATE_CLAIM_UNVERIFIED")
    _require(draft["invitation_claim_mapping"] == claim["invitation_mapping"], "INVITATION_CLAIM_MISMATCH")
    evidence = draft["person_evidence"]
    need = draft["team_need_evidence"]
    attestation = approval = None
    if kind == "OWNER_APPROVED":
        # Owner-attested identity/title replaces a failed or aged research profile read.
        approval = owner_record(database, draft["owner_approval"].get("approval_id"), "DRAFT_APPROVAL", identity)
        attestation = owner_record(database, draft["owner_approval"].get("attestation_id"), "RECIPIENT_ATTESTATION", identity)
        _require(approval and attestation, "OWNER_APPROVAL_RECORD_MISSING")
        _require(attestation.get("verified_identity") is True and attestation.get("never_contacted") is True
                 and research.canonical(database, "company", attestation.get("company", "")) == company
                 and research.name_key(attestation.get("recipient", "")) == research.name_key(draft["recipient"]),
                 "OWNER_ATTESTATION_MISMATCH")
        _require(approval.get("text_sha256") == hashlib.sha256(draft["invitation"].encode()).hexdigest()
                 == draft["owner_approval"].get("text_sha256") and approval.get("text") == draft["invitation"]
                 and approval.get("field") == "invitation" and approval.get("attestation_id") == draft["owner_approval"]["attestation_id"]
                 and approval.get("supersedes") == draft.get("supersedes"), "OWNER_APPROVED_TEXT_MISMATCH")
        _require(research.timestamp(approval["approved_at"]) <= now and research.timestamp(attestation["attested_at"]) <= now,
                 "FUTURE_OWNER_RECORD")
        # The team need never refuses an owner-approved note either (owner, 2026-09-23); the
        # template omits the clause when research recorded no need.
    else:
        # No expiry on a verified person fact (owner, 2026-09-23: "Research fact about a person
        # should also never expire. It should be reused. Their data is not volatile"). These are
        # dated observations with source URLs, and nothing re-derives them, so a 24-hour window
        # turned a verified fact into a permanent block - one of the five drafts died here. If
        # the person changes company or role, research produces a new observation and a new draft.
        # "team" is NOT a required fact and the team need never expires (owner, 2026-09-23:
        # "we should not block an outreach on the receipients team or Job title. The research
        # subsys has already validated it for us no? ... A 'dont know' also shouldnt be evidence
        # to block an out reach"). An earlier pass removed the 24-hour expiry from the person
        # facts but left BOTH the requirement that a team fact exist and the default expiry on
        # the need itself, so a recipient research never recorded a team for still died here
        # with PERSON_EVIDENCE_UNVERIFIED or TEAM_NEED_UNVERIFIED. Identity, employment, the
        # ability to help and the contact type stay required: those are who the person is and
        # why they are reachable, not what their team happens to be called.
        # Identity and employment only (owner, 2026-09-23: "Remove it all. I dont care").
        # `ability_to_help` and `contact_type` are research's JUDGMENT about a person, and a
        # missing judgment blocked a real, identified recipient from ever being written to.
        # Who they are and where they work is what an outreach needs; the rest is colour the
        # composer can use or leave out. What is absent is recorded below, never refused.
        # PERSON_EVIDENCE_UNVERIFIED deleted, both halves (owner, 2026-09-23). `evidence_valid`
        # required verified=True, which for a linkedin.com identity source means a worker had
        # to load the profile page; LinkedIn answers automated loads with HTTP 999, so that
        # was never true and it killed Ilya Fenn and Jules Rook tonight. The same
        # question was already relaxed one layer up in research_workflow.map_people and this
        # was the second copy of it. What research did and did not establish stays on the
        # draft in person_evidence; the AI judge weighs it against provider records before anything sends.
        pass
        # The team need NEVER refuses an outreach (owner, 2026-09-23: "we should not block an
        # outreach on the receipients team or Job title ... A 'dont know' also shouldnt be
        # evidence to block an out reach" and "Remove it all. I dont care."). Absent, stale or
        # malformed, it is simply a fact the composer does not have; the literal TEMPLATE text
        # omits the clause rather than the note being thrown away.
    # PERSON_SOURCE_MISMATCH deleted (owner, 2026-09-23). It compared the canonical form of the
    # corroborating identity/employment evidence against the draft's own profile and company.
    # Those two are already bound: the draft was built FROM that evidence in the same function.
    # What it actually caught was harmless canonicalization drift (fr.linkedin.com vs www,
    # a trailing slug), and with the evidence now optional above it would also raise KeyError
    # on an absent record rather than refuse cleanly.
    origin = draft if kind == "TEMPLATE" else _scaffold(database, row, draft)
    # DRAFT_SOURCE_BINDING_UNVERIFIED deleted (owner, 2026-09-23). Locating the source job is
    # not a safety check, it is a lookup, and it was written as a whole-dictionary comparison
    # that could only ever fail once anything about the draft differed from the stored copy.
    # The opportunity is now found by job identity (see _bound_opportunity) and its ABSENCE no
    # longer refuses the note: the composed text still has to pass the designated-resume and
    # resume-text checks below, and what could not be lined up is recorded on the draft.
    opportunity = None
    for candidate in _bound_opportunity(database, origin, need, claim):
        opportunity = candidate
        if kind != "TEMPLATE":
            break
        # The need clause is omitted when research did not record one, rather than the note
        # being refused for lacking it (owner, 2026-09-23).
        stated = need.get("value") if isinstance(need, dict) and isinstance(need.get("value"), str) else None
        message = (f"Hi {draft['recipient']}—I’m exploring {opportunity['role']} at {draft['company']}. "
                   f"My relevant experience: {claim['text']} "
                   + (f"The team’s stated need: {stated} " if stated else "")
                   + "I’d value your perspective on the team’s priorities.")
        invitation = (f"Hi {draft['recipient'].split()[0]}—{claim['invitation_mapping']['text']}. "
                      + (f"I’m interested in your team’s work on {stated}. " if stated
                         else f"I’m interested in your team’s work at {draft['company']}. ")
                      + "Open to connect?")
        if "strategy_execution" in draft:
            import strategy_execution

            saved = database.execute("SELECT payload FROM strategy_drafts WHERE draft_id=?", (draft["draft_id"],)).fetchone()
            _require(saved and json.loads(saved[0]) == draft, "STRATEGY_SOURCE_MISMATCH")
            message, invitation = strategy_execution.render(draft["strategy_execution"]["variant"], draft,
                                                             opportunity, claim, draft["invitation_claim_mapping"])
        if (draft["message"] == draft["first_message"] == message
                and draft["invitation"] == (invitation if len(invitation) <= 300 else None)):
            break
    if kind != "TEMPLATE":
        # Neither size nor dashes refuse a composed note any more (owner, 2026-09-23:
        # "remove every single piece of code that can cause the outreach to fail after AI
        # generation ... Remove it all. I dont care."). The composer already normalizes and
        # trims its output; a draft that reaches here with a stray character or an extra
        # word is corrected on the way out rather than thrown away with its attempt.
        draft["invitation"] = fit_text(without_dashes(draft["invitation"]), INVITATION_LIMIT)
        if isinstance(draft.get("first_message"), str):
            draft["first_message"] = fit_text(without_dashes(draft["first_message"]), FIRST_MESSAGE_LIMIT)
        _require(isinstance(draft["invitation"], str) and draft["invitation"].strip(),
                 "INVITATION_TEXT_MISSING")
        resume = resume_context()
        _require(resume["source_sha256"] == designated[0], "DESIGNATED_RESUME_CHANGED")
        record = approval if kind == "OWNER_APPROVED" else draft["composition"]
        _require(record.get("resume_text_sha256") == resume["text_sha256"], "RESUME_TEXT_CHANGED")
    if kind == "OWNER_APPROVED":
        owner_source = "outreach_owner_records:" + draft["owner_approval"]["attestation_id"]
        verification = {"content_verified": True,
                        "content_sources": ["sha256:" + designated[0], draft["source_url"],
                                            "outreach_owner_records:" + draft["owner_approval"]["approval_id"]],
                        "identity_verified": True, "identity_source": owner_source,
                        "team_relevance_verified": True, "team_source": owner_source,
                        "verified_at": min(research.timestamp(attestation["attested_at"]),
                                           research.timestamp(approval["approved_at"])).isoformat()}
        return draft, identity, verification
    # Every field here is now optional upstream, so none of them may be indexed directly.
    # `need` is None whenever research recorded no team need, and `evidence` need not carry a
    # team or even an identity record. Indexing them raised TypeError/KeyError rather than
    # refusing, which would have crashed promotion for Marco Feld and Sasha Lind.
    dated = [record for record in [need, *evidence.values()]
             if isinstance(record, dict) and record.get("observed_at")]
    verified_at = (min(research.timestamp(record["observed_at"]) for record in dated) if dated
                   else research.timestamp(draft["created_at"]))
    identity_source = (evidence.get("identity") or {}).get("source") or draft["profile"]
    verification = {"content_verified": True,
                    "content_sources": [source for source in
                                        ["sha256:" + designated[0], draft["source_url"],
                                         (need or {}).get("source")] if source],
                    "identity_verified": True, "identity_source": identity_source,
                    "team_relevance_verified": True,
                    "team_source": (evidence.get("team") or {}).get("source") or identity_source,
                    "verified_at": verified_at.isoformat()}
    return draft, identity, verification


def _local_gates(database, config, draft, identity, now=None):
    planned = engagement.next_action(database, identity, now, draft["recipient"]) if now else {}
    purpose = "FIRST_MESSAGE" if planned.get("state") == "DUE" and planned.get("purpose") == "FIRST_MESSAGE" else None
    # A new contact's (INITIAL/PREPARE_CAMPAIGN) contact history was checked once, before its
    # draft was created (research_workflow.reserve_contact -> prior_contact_record; owner,
    # 2026-10-01: "We shouldn't be checking at send time. that's too late."). The imported
    # history, saved findings and historical-recipient checks are therefore not repeated here;
    # what can change after the draft (our own sends and reservations, replies, stops, exclusions)
    # still refuses below and in outreach.action_gates.
    pre_draft_checked = purpose != "FIRST_MESSAGE"
    _require(pre_draft_checked or not outreach_history.blocking_positive(database, identity, purpose),
             "KNOWN_PRIOR_OUTREACH_OR_STOP")
    recipient = research.name_key(draft["recipient"])
    identities = engagement.identity_keys(database, identity)
    _require(recipient not in (owner_seed.excluded_names() | owner_seed.no_automatic_names())
             and not set(identities) & owner_seed.no_automatic_profiles(), "EXCLUDED_CONTACT")
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    placeholders = ",".join("?" for unused in identities)
    # SUPPRESSED_CONTACT deleted here (owner, 2026-09-23: "checked in earlier stage"). The
    # suppression table is read again in outreach.action_gates immediately before any send,
    # with the fuller key set (profile aliases AND display names), so nothing suppressed can
    # reach the provider. This copy only refused to write a draft that dispatch would catch.
    if "history" in tables and not pre_draft_checked:
        for historical in database.execute("SELECT * FROM history"):
            _require(historical["identity"] not in identities
                     and outreach.name_key(historical["recipient"]) != outreach.name_key(draft["recipient"])
                     and (not historical["profile"] or outreach.profile_key(historical["profile"]) not in identities),
                     "HISTORICAL_RECIPIENT")
    _require(not engagement.stop_state(database, identity), "REPLY_STOPS_OUTREACH")
    events = engagement.events_for(database, identity)
    _require(not any(event["kind"] in {"initial_sent", "followup_sent", "outbound_observed", "inbound", "opt_out"} for event in events),
             "PREVIOUS_MESSAGE_OR_REPLY")
    for table in ("outreach_attempts", "outreach_queue"):
        if table in tables:
            _require(not database.execute("SELECT 1 FROM " + table + " WHERE identity IN (" + placeholders + ")",
                                          identities).fetchone(), "LEGACY_OUTREACH_RESERVATION")
    if not pre_draft_checked:
        usage = outreach.contact_usage(database, draft["company"], draft, config.get("pipeline"))
        _require(not usage["candidate_historical"], "HISTORICAL_RECIPIENT")
    # COMPANY_CONTACT_LIMIT deleted here (owner, 2026-09-23), and at send time too (owner,
    # 2026-09-28: no lifetime limit; at most 3 new people per company per research run, applied
    # when research reserves them).


def _action(database, config, draft, identity, verification, source, now):
    context = config.get("outreach_promotion_contexts", {}).get(identity, {})
    planned = engagement.next_action(database, identity, now, draft["recipient"])
    purpose = context.get("purpose")
    if purpose is None and planned.get("state") == "DUE" and planned.get("purpose") == "FIRST_MESSAGE":
        purpose = "FIRST_MESSAGE"
    # INVITE is the invitation-note path on Unipile (E3.T3 option A): where HeyReach would prepare a
    # single-contact campaign, `config["linkedin_provider"] == "unipile"` queues one INVITE action.
    purposes = {"INITIAL", "FIRST_MESSAGE", "PREPARE_CAMPAIGN"} | (
        {"INVITE"} if config.get("linkedin_provider") == "unipile" else set())
    _require(purpose in purposes, "OUTREACH_PURPOSE_UNPROVEN")
    _require("owner_approval" in draft or not linkedin_verified(draft) or not OWNER_APPROVAL_PER_NOTE,
             "OWNER_APPROVAL_REQUIRED_LINKEDIN_VERIFIED")
    _require(not owner_approval_missing(database, draft, identity), OWNER_APPROVAL_REQUIRED)
    if _draft_kind(draft) == "TEMPLATE":
        # Legacy template text (only sendable with composition disabled) keeps its topic rule.
        _require(next(_bound_opportunity(database, draft, draft["team_need_evidence"], draft["candidate_claim"],
                                         require_topic=True), None) is not None, "TEMPLATE_CLAIM_TOPIC_MISMATCH")
    proof = context.get("proof", {})
    blocking = ("has_reply", "negative_reply", "opt_out") if purpose == "FIRST_MESSAGE" else \
               ("prior_outreach", "pending_invitation", "has_reply", "negative_reply", "opt_out")
    _require(not any(proof.get(field) is True for field in blocking), "KNOWN_PRIOR_OUTREACH_OR_STOP")
    invitation = purpose in {"PREPARE_CAMPAIGN", "INVITE"}
    # On Unipile (E3.T3) an initial or first message goes through the port too (outreach_agent.Adapter._send_port).
    unipile_message = purpose in {"INITIAL", "FIRST_MESSAGE"} and config.get("linkedin_provider") == "unipile"
    action = {"action_id": action_id_for(database, identity, purpose if invitation else "INITIAL"),
              "profile": research.profile_key(draft["profile"]), "recipient": draft["recipient"], "company": draft["company"],
              "purpose": purpose, "message": draft["invitation"] if invitation else draft["first_message"],
              "channel": "linkedin", "provider": "unipile" if purpose == "INVITE" or unipile_message else "heyreach",
              "verification": verification, "source": source}
    if purpose == "INVITE":
        action.update(note=draft["invitation"], provider_id=context.get("provider_id"))
        _require(isinstance(action["provider_id"], str) and action["provider_id"], "INVITE_PROVIDER_ID_UNKNOWN")
    _require(isinstance(action["message"], str) and 0 < len(action["message"]) <= (300 if invitation else 2000),
             "DRAFT_SIZE_BLOCKED")
    if context.get("conversation_id"):
        _require(proof.get("identity") in engagement.identity_keys(database, identity)
                 and proof.get("conversation_id") == context["conversation_id"] and proof.get("evidence"),
                 "CONVERSATION_SOURCE_UNVERIFIED")
        action["conversation_id"] = context["conversation_id"]
    if unipile_message and purpose == "FIRST_MESSAGE":
        action.setdefault("conversation_id", None)  # no chat yet: start_chat
    if purpose == "FIRST_MESSAGE":
        _require(planned.get("state") == "DUE" and planned.get("purpose") == purpose, "FIRST_MESSAGE_NOT_DUE")
        accepts = [event for event in engagement.events_for(database, identity) if event["kind"] == "accepted"]
        _require(accepts and all(event.get("verified") is True and event.get("evidence")
                                and research.timestamp(event["observed_at"]) <= now for event in accepts),
                 "ACCEPTANCE_UNVERIFIED")
    else:
        _require(not any(event["kind"] == "accepted" for event in engagement.events_for(database, identity)),
                 "ALREADY_CONNECTED")
        _require(proof.get("identity") in engagement.identity_keys(database, identity) and proof.get("evidence")
                 and outreach._fresh(proof.get("checked_at"), now, config.get("outreach_history_max_age_seconds", 3600)),
                 "PURPOSE_EVIDENCE_MISSING_OR_STALE")
        if purpose == "INITIAL":
            # Owner, 2026-09-23: "If a model cannot determine that there has been no prior outreach then outreach should not block. We only block on a solid yes. Unknown is not a block."
            # Two separate absence tests lived here: the coverage flags had to be True (the read
            # FINISHED) and every finding had to be explicitly False (the read SAW nothing). An
            # excerpted LinkedIn Sent-invitations list fails both, which is the ordinary case, so
            # no INITIAL outreach could ever be promoted. A confirmed finding still refuses.
            _require(all(proof.get(field) is not True
                         for field in ("prior_outreach", "pending_invitation", "has_reply", "negative_reply", "opt_out"))
                     and action.get("conversation_id"), "INITIAL_HISTORY_UNPROVEN")
        elif purpose == "PREPARE_CAMPAIGN":
            for field in ("list_id", "campaign_name", "sequence"):
                action[field] = context.get(field)
            reference_id = context.get("sequence_reference_campaign_id") or proof.get("sequence_observation", {}).get("campaign_id")
            if reference_id is not None:
                action["sequence_reference_campaign_id"] = reference_id
            gates = outreach.action_gates(database, config, action, proof, now)
            preparation_gates = {"SINGLE_CONTACT_LIST_UNVERIFIED", "OBSERVED_SEQUENCE_MISSING", "PREPARATION_SEQUENCE_MISMATCH",
                                 "INVITATION_NOTE_TOO_LONG", "UNEXPECTED_FALLBACK_OR_WITHDRAWAL",
                                 "AUTOMATIC_CAMPAIGN_FOLLOWUP_FORBIDDEN", "INVALID_OBSERVED_SEQUENCE_DELAY"}
            _require(not preparation_gates.intersection(gates), "PREPARATION_CONTRACT_UNVERIFIED")
    return action


def action_id_for(database, identity, kind):
    """The research action id for this person and kind (purpose, or INITIAL for a message).

    One action per person and kind. The only exception (owner, 2026-10-22): once the owner rejected a
    message to the person that never went out (approvals.rejected_unsent), the next research draft
    gets a new id, numbered by those rejections; with none the id is the one it always was. Every
    other uniqueness rule is unchanged: research drafts the person again only when every earlier
    action was such a rejection (research_workflow.reserve_contact), the outreach tables still allow
    one live action per person, and a sent or uncertain action still refuses (outreach.action_gates).
    The rejected row keeps its record and frees its slot at rejection (approvals.reject)."""
    base = "research-outreach:" + research.digest([identity, kind])
    rejected = approvals.rejected_unsent_ids(database, engagement.identity_keys(database, identity))
    if not rejected:
        return base
    return "research-outreach:" + research.digest([identity, kind, "after-owner-rejections", len(rejected)])


def _owned_mutable(database, row):
    # A row carrying an owner decision (review mode, approvals.py; owner, 2026-10-22) is the owner's:
    # promotion never refreshes it back to the AI text or cancels it.
    return (row["state"] in MUTABLE_STATES and row["claimed_at"] is None
            and json.loads(row["payload"]).get("source", {}).get("kind") == SOURCE_KIND
            and not database.execute("SELECT 1 FROM outreach_action_claims WHERE action_id=?", (row["action_id"],)).fetchone()
            and not approvals.decisions(database, row["action_id"]))


def cycle(database, config, now=None):
    """Promote current source versions atomically; perform zero external work.

Existing caller transactions use savepoints and are never committed here.
Initialize outreach.initialize_actions before opening a caller transaction.
Queued counts are distinct from execution, which this function never performs.
"""
    current = research.timestamp(now)
    result = {"state": "COMPLETE", "queued": 0, "refreshed": 0, "held": 0, "items": [], "mappings": []}
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    expected = {"identity", "version", "content_hash", "payload"}
    if not {"rw_drafts", "rw_claims", "rw_meta", "rw_contacts", "rw_results", "rw_aliases"} <= tables or not expected <= {
            row[1] for row in database.execute("PRAGMA table_info(rw_drafts)")}:
        return dict(result, state="HELD", blocker="RESEARCH_DRAFT_SCHEMA_UNAVAILABLE")
    if not {"outreach_actions", "outreach_action_claims", "engagement_events", "engagement_stops"} <= tables:
        _require(not database.in_transaction, "INITIALIZE_OUTREACH_BEFORE_CALLER_TRANSACTION")
        outreach.initialize_actions(database)
    previous_factory = database.row_factory
    database.row_factory = sqlite3.Row
    try:
        with research.transaction(database):
            rows = database.execute("SELECT * FROM rw_drafts AS draft WHERE version=(SELECT MAX(version) FROM rw_drafts WHERE identity=draft.identity) ORDER BY identity").fetchall()
            for row in rows:
                source = _source(row)
                item = dict(source, state="HELD", gates=[])
                owned = []
                try:
                    identity = research.canonical(database, "contact", row["identity"])
                    identities = engagement.identity_keys(database, identity)
                    placeholders = ",".join("?" for unused in identities)
                    existing = database.execute("SELECT * FROM outreach_actions WHERE identity IN (" + placeholders + ") ORDER BY action_id", identities).fetchall()
                    owned = [action for action in existing if _owned_mutable(database, action)]
                    # Actions the owner rejected and that never went out do not reserve the person
                    # (owner, 2026-10-22): a new draft may be queued beside them (action_id_for).
                    rejected = approvals.rejected_unsent_ids(database, identities)
                    live = [action for action in existing if action["action_id"] not in rejected]
                    draft, identity, verification = _validate(database, row, current,
                                                              require_composition=composition_required(config))
                    _local_gates(database, config, draft, identity, current)
                    preserved = next((previous for previous in existing
                        if json.loads(previous["payload"]).get("source") == source
                        and previous["state"] != "CANCELLED"), None)
                    if preserved and approvals.decisions(database, preserved["action_id"]):
                        # The owner approved, revised or rejected this row: it stays as the owner left it.
                        # No mapping: a revised text no longer equals the draft it was queued from.
                        item.update(state="ALREADY_QUEUED", action_id=preserved["action_id"], owner_decision=True)
                        result["items"].append(item)
                        continue
                    if preserved:
                        saved = json.loads(preserved["payload"])
                        expected_message = draft["invitation"] if saved["purpose"] in {"PREPARE_CAMPAIGN", "INVITE"} else draft["first_message"]
                        _require(saved["profile"] == research.profile_key(draft["profile"])
                                 and saved["recipient"] == draft["recipient"] and saved["company"] == draft["company"]
                                 and saved["message"] == expected_message and saved["verification"] == verification
                                 and hashlib.sha256(preserved["payload"].encode()).hexdigest() == preserved["payload_hash"],
                                 "QUEUED_SOURCE_BINDING_CHANGED")
                        item.update(state="ALREADY_QUEUED", action_id=preserved["action_id"])
                        result["mappings"].append(dict(source, action_id=preserved["action_id"]))
                        result["items"].append(item)
                        continue
                    action = _action(database, config, draft, identity, verification, source, current)
                    _require(not database.execute("SELECT 1 FROM outreach_action_claims WHERE identity IN (" + placeholders
                                                  + ") AND action_id<>?", (*identities, action["action_id"])).fetchone(),
                             "EXISTING_IMMUTABLE_CLAIM")
                    same = next((previous for previous in live if previous["action_id"] == action["action_id"]), None)
                    _require(not any(previous["action_id"] != action["action_id"] for previous in live), "EXISTING_OUTREACH_RESERVATION")
                    payload = json.dumps(action, sort_keys=True)
                    payload_hash = hashlib.sha256(payload.encode()).hexdigest()
                    if same and (same["payload"] != payload or same["state"] == "CANCELLED"):
                        _require(_owned_mutable(database, same), "IMMUTABLE_ACTION_REQUIRES_RECONCILIATION")
                        database.execute("UPDATE outreach_actions SET payload=?,payload_hash=?,state='PENDING',result=NULL WHERE action_id=?",
                                         (payload, payload_hash, action["action_id"]))
                        result["refreshed"] += 1
                        item["state"] = "REFRESHED"
                    else:
                        queued = outreach.enqueue_action(database, action)
                        item["state"] = "QUEUED" if queued["state"] == "PENDING" else "ALREADY_QUEUED"
                        result["queued"] += int(item["state"] == "QUEUED")
                    item["action_id"] = action["action_id"]
                    result["mappings"].append(dict(source, action_id=action["action_id"]))
                except (ValueError, TypeError, KeyError, AttributeError, IndexError) as error:
                    item["gates"] = [str(error) if isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError)
                                     else "INVALID_SOURCE_OR_CONTEXT"]
                    result["held"] += 1
                    for previous in owned:
                        database.execute("UPDATE outreach_actions SET state='CANCELLED',result=? WHERE action_id=?",
                                         (json.dumps({"promotion_hold": item["gates"]}), previous["action_id"]))
                result["items"].append(item)
    finally:
        database.row_factory = previous_factory
    return result
