"""The note scaffold research drafts start from: one fixed renderer and its saved drafts.

Research drafts a person's note from this scaffold (`render`), records it in `strategy_drafts`
(`record_draft`, `prior_draft`), and the composer builds the sendable note on it. The learning
feature (experiments, cohort selection and action binding) was removed (owner, 2026-10-22: "lets
remove learning totally. its wasteful."): `select` always returns the fixed baseline, and nothing
binds an outreach action to a draft any more. The `strategy_action_bindings` table and the
learning_* tables stay on existing disks, unread; `initialize` drops the bound-action triggers the
binding left on outreach_actions so a legacy bound row is an ordinary action again.
"""

import hashlib
import json


BASELINE = "claim_first_v1"
SUPPORTED_VARIANTS = frozenset({BASELINE, "need_first_v1"})
BOUND_ACTION_TRIGGERS = ("strategy_bound_action_no_update", "strategy_bound_action_no_update_v2",
                         "strategy_bound_action_no_update_v3", "strategy_bound_action_no_delete",
                         "strategy_bound_action_no_replace")


def text_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def initialize(database):
    database.execute("""CREATE TABLE IF NOT EXISTS strategy_drafts (
        draft_id TEXT PRIMARY KEY, identity TEXT NOT NULL, input_hash TEXT NOT NULL,
        version INTEGER NOT NULL, payload TEXT NOT NULL,
        UNIQUE(identity,input_hash), UNIQUE(identity,version))""")
    for operation in ("UPDATE", "DELETE"):
        database.execute(f"CREATE TRIGGER IF NOT EXISTS strategy_drafts_no_{operation.lower()} BEFORE {operation} ON strategy_drafts BEGIN SELECT RAISE(ABORT, 'Immutable strategy provenance'); END")
    database.execute("CREATE TRIGGER IF NOT EXISTS strategy_drafts_no_replace BEFORE INSERT ON strategy_drafts WHEN EXISTS (SELECT 1 FROM strategy_drafts WHERE draft_id=NEW.draft_id OR (identity=NEW.identity AND (input_hash=NEW.input_hash OR version=NEW.version))) BEGIN SELECT RAISE(ABORT, 'Immutable strategy provenance'); END")
    for trigger in BOUND_ACTION_TRIGGERS:
        database.execute("DROP TRIGGER IF EXISTS " + trigger)


def prior_draft(database, identity, input_hash):
    initialize(database)
    row = database.execute("SELECT payload FROM strategy_drafts WHERE identity=? AND input_hash=?",
                           (identity, input_hash)).fetchone()
    return json.loads(row[0]) if row else None


def segment_for(person, opportunity, claim_id):
    import research_workflow  # research_workflow imports this module
    facts = opportunity["review"]["facts"]
    evidence = opportunity["review"]["fact_evidence"]
    return {"industry": facts.get("industry") or "UNKNOWN",
            "funding_stage": facts.get("funding_stage") or "UNKNOWN",
            "role_level": facts.get("level") or "UNKNOWN",
            "company_size": evidence.get("employee_count", {}).get("value", "UNKNOWN"),
            "contact_type": research_workflow.contact_type(
                person["evidence_by_field"].get("contact_type", {}).get("value", "UNKNOWN")),
            "location_tier": opportunity.get("ranking", {}).get("location_priority", "UNKNOWN"),
            "evidence_module": claim_id, "followup_timing": "NONE"}


def select(database, identity, segment, now):
    """The fixed baseline scaffold; there are no experiments."""
    return {"renderer_version": 1, "variant": BASELINE, "experiment_id": None,
            "arm": None, "policy_version": 0, "assigned_at": now,
            "reason": "NO_APPROVED_SUPPORTED_EXPERIMENT"}


def render(variant, person, opportunity, claim, invitation_mapping):
    if variant not in SUPPORTED_VARIANTS:
        raise ValueError("Unsupported message variant")
    # An absent team need renders the scaffold without that clause rather than raising
    # (owner, 2026-09-23); research_workflow._create_draft allows the same absence.
    need = opportunity["review"]["facts"].get("team_need")
    greeting = f"Hi {person['recipient']}—I’m exploring {opportunity['role']} at {opportunity['company']}. "
    experience = f"My relevant experience: {claim['text']} "
    team = f"The team’s stated need: {need} " if need else ""
    closing = "I’d value your perspective on the team’s priorities."
    first_name = person["recipient"].split()[0]
    invitation_claim = invitation_mapping["text"] + ". "
    invitation_need = f"I’m interested in your team’s work on {need or opportunity['role']}. "
    if variant == BASELINE:
        return (greeting + experience + team + closing,
                f"Hi {first_name}—" + invitation_claim + invitation_need + "Open to connect?")
    return (greeting + team + experience + closing,
            f"Hi {first_name}—" + invitation_need + invitation_claim + "Open to connect?")


def record_draft(database, identity, input_hash, payload):
    database.execute("INSERT INTO strategy_drafts VALUES (?,?,?,?,?)",
                     (payload["draft_id"], identity, input_hash, payload["version"], json.dumps(payload, sort_keys=True, separators=(",", ":"))))
