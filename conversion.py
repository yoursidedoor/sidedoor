"""Evidence-backed R17–R19 coordinator; never performs network or send operations.

Initialize applications first, then initialize this module on the same SQLite
connection. The caller owns transactions and the existing orchestrator lock.
record_evidence is a trusted importer boundary: its caller must authenticate and
inspect the cited source. It stores extracted facts, a source digest and reviewer;
it does not fetch or independently verify sources. Bare verified=True flags are
not accepted as evidence. No live source or delivery adapter is implemented here.

enqueue_event persists inputs; cycle applies each item under a savepoint and
returns durable tasks, guarded message intents and reporting-compatible outcomes.
Referrals are not handled here: they are recorded per person in referrals.py,
independently of jobs and applications (owner, 2026-09-25).
All application mutations retain the original job_key and submission gates.
"""

import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import urllib.parse

import owner_seed


STAGES = {
    "RECRUITER_SCREEN": "Prepare verified experience, role questions and compensation questions.",
    "TECHNICAL_SCREEN": "Practice relevant technical reasoning; prepare verified implementation examples.",
    "SYSTEM_DESIGN": "Prepare requirements, tradeoffs, failure modes and scaling discussion.",
    "BEHAVIORAL_LEADERSHIP": "Prepare sourced leadership stories with actions and actual results.",
    "FINAL_ROUND": "Prepare unresolved fit questions and verified cross-team impact examples.",
    "OFFER": "Review the actual written terms and identify unanswered questions; do not accept commitments.",
}
SOURCES = {
    "ROLE_CONTEXT": {"official_role", "employer_record"},
    "INTERVIEW_STAGE": {"employer_record", "user_confirmation"},
    "DEBRIEF": {"user_confirmation"},
    "EMPLOYER_TIMELINE": {"employer_record"},
    "OUTCOME": {"employer_record", "referral_portal"},
    "CANDIDATE_CLAIM": {"designated_resume", "user_confirmation"},
    "CANDIDATE_PROFILE": {"designated_resume", "user_confirmation"},
    "ATTRIBUTION": {"employer_record", "contact_message"},
}
EVENT_KINDS = set(SOURCES) - {"CANDIDATE_CLAIM", "CANDIDATE_PROFILE", "ATTRIBUTION"}
PROTECTED = {"CLAIMED", "SUBMITTING", "RECONCILE", "SUBMISSION_UNKNOWN", "SUBMITTED", "DUPLICATE", "INELIGIBLE", "EXHAUSTED", "CLOSED", "ABANDONED"}
REPORTING_KINDS = {"INTERVIEW", "REFERRAL", "RECRUITER_CONVERSATION", "APPLICATION_RESPONSE"}


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def timestamp(value=None):
    moment = dt.datetime.fromisoformat(value) if isinstance(value, str) else value or dt.datetime.now(dt.timezone.utc)
    if not isinstance(moment, dt.datetime) or moment.tzinfo is None:
        raise ValueError("Timezone-aware timestamp required")
    return moment.astimezone(dt.timezone.utc).isoformat()


def _text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Nonempty evidence text required")
    return value


def _url(value):
    parsed = urllib.parse.urlsplit(_text(value))
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("HTTPS source destination without credentials required")
    return value


def _one(database, sql, args=()):
    cursor = database.execute(sql, args)
    row = cursor.fetchone()
    return dict(zip([column[0] for column in cursor.description], row)) if row is not None else None


def _all(database, sql, args=()):
    cursor = database.execute(sql, args)
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def initialize(database):
    database.executescript("""
        CREATE TABLE IF NOT EXISTS conversion_evidence (
            evidence_id TEXT PRIMARY KEY, kind TEXT NOT NULL, observed_at TEXT NOT NULL,
            record TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS conversion_events (
            event_id TEXT PRIMARY KEY, kind TEXT NOT NULL, job_key TEXT NOT NULL,
            contact_id TEXT, evidence_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING',
            error TEXT, applied_at TEXT);
        CREATE TABLE IF NOT EXISTS conversion_roles (
            job_key TEXT PRIMARY KEY, evidence_id TEXT NOT NULL, observed_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS conversion_interviews (
            job_key TEXT NOT NULL, stage_id TEXT NOT NULL, stage TEXT NOT NULL,
            evidence_id TEXT NOT NULL, observed_at TEXT NOT NULL, scheduled_at TEXT,
            debrief_evidence_id TEXT, timeline_evidence_id TEXT, followup_at TEXT,
            PRIMARY KEY(job_key,stage_id));
        CREATE TABLE IF NOT EXISTS conversion_summaries (
            summary_id TEXT PRIMARY KEY, job_key TEXT NOT NULL, contact_id TEXT NOT NULL,
            version INTEGER NOT NULL, body TEXT NOT NULL, provenance TEXT NOT NULL,
            created_at TEXT NOT NULL, UNIQUE(job_key,contact_id,version));
        CREATE TABLE IF NOT EXISTS conversion_tasks (
            task_id TEXT PRIMARY KEY, job_key TEXT NOT NULL, kind TEXT NOT NULL,
            payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING');
        CREATE TABLE IF NOT EXISTS conversion_outcomes (
            action_id TEXT PRIMARY KEY, job_key TEXT NOT NULL, kind TEXT NOT NULL,
            at TEXT NOT NULL, attributed INTEGER NOT NULL, evidence TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS conversion_audit (
            audit_id TEXT PRIMARY KEY, job_key TEXT NOT NULL, at TEXT NOT NULL, detail TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS conversion_baselines (
            baseline_id TEXT PRIMARY KEY, company TEXT NOT NULL, record TEXT NOT NULL);
    """)
    for table in ("conversion_evidence", "conversion_summaries", "conversion_outcomes", "conversion_audit", "conversion_baselines"):
        for operation in ("UPDATE", "DELETE"):
            database.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()}
                BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, 'Immutable conversion provenance'); END""")
    database.execute("""CREATE TRIGGER IF NOT EXISTS conversion_events_immutable_input
        BEFORE UPDATE OF event_id,kind,job_key,contact_id,evidence_id ON conversion_events
        BEGIN SELECT RAISE(ABORT, 'Immutable conversion event input'); END""")
    database.execute("""CREATE TRIGGER IF NOT EXISTS conversion_events_no_delete
        BEFORE DELETE ON conversion_events BEGIN SELECT RAISE(ABORT, 'Immutable conversion event input'); END""")


def seed_baseline_priorities(database, requirements_path, now=None):
    """Stage R09 saved priorities for confirmation, without inventing job identity.

    Reads only the handoff requirements text, never a provider, resume or tracker.
    Dates are historical baseline context, not a fresh employer confirmation.
    The exact R09 text and file hash are retained. No application job is created.
    """
    now = timestamp(now)
    # The saved interviews and the text that must still state them come from the optional owner
    # seed (owner_seed.py, `baseline_priorities`); a new install has none and stages nothing.
    seeded = owner_seed.mapping("baseline_priorities")
    if not seeded.get("entries"):
        return []
    path = Path(requirements_path)
    content = path.read_bytes()
    match = re.search(r"^\*\*R09[^\n]+", content.decode("utf-8"), re.MULTILINE)
    if not match:
        raise ValueError("R09 saved baseline source missing")
    paragraph = match.group(0)
    if not re.search(seeded.get("pattern") or "", paragraph):
        raise ValueError("Baseline source changed; review dates instead of assuming recurring interviews")
    entries = [tuple(entry) for entry in seeded["entries"]]
    digest = hashlib.sha256(content).hexdigest()
    result = []
    for company, date, scheduled in entries:
        identity = "baseline:" + hashlib.sha256(encoded([digest, company]).encode()).hexdigest()
        record = {"baseline_id": identity, "company": company, "historical_date": date, "historical_scheduled_at": scheduled,
                  "stage": None, "job_key": None, "confirmation_freshness": "UNKNOWN", "origin": "baseline",
                  "attributed": False, "source": str(path), "source_sha256": digest, "source_locator": "R09",
                  "source_text": paragraph, "saved_evidence_read_at": now, "observed_at": None,
                  "next_action": "Reconcile newer employer confirmation, exact role/requisition and stage; prepare from known facts"}
        previous = _one(database, "SELECT record FROM conversion_baselines WHERE baseline_id=?", (identity,))
        if previous:
            record = json.loads(previous["record"])
        else:
            database.execute("INSERT INTO conversion_baselines VALUES (?,?,?)", (identity, company, encoded(record)))
        stub = {"job_key": identity, "employer": company, "requisition": None}
        _task(database, identity, stub, "RECONCILE_BASELINE_INTERVIEW", record, scheduled,
              blocker="CURRENT_CONFIRMATION_AND_CANONICAL_ROLE_REQUIRED")
        result.append(record)
    return result


def record_evidence(database, record):
    """Import authenticated source facts; required keys are validated below.

    Facts must be extracted from source_ref at source_locator by verified_by.
    source_sha256 binds the inspected source bytes, not a generated summary.
    Job facts also carry exact employer/requisition, and contact facts contact_id.
    Candidate claim facts: text, category (achievement/leadership), topics (list).
    Profile facts: name, profile_url, optional designated_resume_reference.
    """
    record = json.loads(encoded(record))
    for key in ("evidence_id", "kind", "source_type", "source_ref", "source_locator", "source_sha256", "verified_by"):
        _text(record.get(key))
    if record["kind"] not in SOURCES or record["source_type"] not in SOURCES[record["kind"]]:
        raise ValueError("Unsupported evidence source adapter/kind")
    digest = record["source_sha256"]
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("Inspected source SHA-256 required")
    if not isinstance(record.get("facts"), dict) or not record["facts"]:
        raise ValueError("Extracted source facts required")
    record["observed_at"] = timestamp(record["observed_at"])
    record["occurred_at"] = timestamp(record["occurred_at"])
    if record["occurred_at"] > record["observed_at"]:
        raise ValueError("Occurrence cannot follow source observation")
    stored = _one(database, "SELECT record FROM conversion_evidence WHERE evidence_id=?", (record["evidence_id"],))
    if stored:
        if stored["record"] != encoded(record):
            raise ValueError("Evidence identity cannot be reused for different facts")
        return record["evidence_id"]
    database.execute("INSERT INTO conversion_evidence VALUES (?,?,?,?)",
                     (record["evidence_id"], record["kind"], record["observed_at"], encoded(record)))
    return record["evidence_id"]


def enqueue_event(database, event_id, kind, job_key, evidence_id, contact_id=None):
    """Persist a source reference, never caller-supplied verification booleans.

    All event facts include employer/requisition.
    INTERVIEW_STAGE: stage_id, stage, optional scheduled_at; absence stays unknown.
    DEBRIEF: stage_id, notes, optional objections list. EMPLOYER_TIMELINE: stage_id,
    employer_statement, optional followup_at taken from the employer's timeline.
    OUTCOME: outcome, statement, observation=independent_outcome_read, optional
    attribution_evidence_id; a REFERRAL outcome also needs contact_id/receipt_id, recruiter
    conversation needs conversation_id. Dates must be timezone-aware when known.
    """
    for value in (event_id, job_key, evidence_id):
        _text(value)
    if kind not in EVENT_KINDS:
        raise ValueError("Unsupported conversion event")
    values = (event_id, kind, job_key, contact_id, evidence_id)
    previous = database.execute("SELECT event_id,kind,job_key,contact_id,evidence_id FROM conversion_events WHERE event_id=?", (event_id,)).fetchone()
    if previous:
        if tuple(previous) != values:
            raise ValueError("Event identity conflict")
        return event_id
    database.execute("INSERT INTO conversion_events(event_id,kind,job_key,contact_id,evidence_id) VALUES (?,?,?,?,?)", values)
    return event_id


def retry_event(database, event_id):
    """Retry unchanged blocked input after a missing dependency is reconciled."""
    return database.execute("UPDATE conversion_events SET status='PENDING',error=NULL WHERE event_id=? AND status='BLOCKED'", (event_id,)).rowcount == 1


def _evidence(database, evidence_id, kind=None, now=None):
    row = _one(database, "SELECT record FROM conversion_evidence WHERE evidence_id=?", (evidence_id,))
    if not row:
        raise ValueError("Stored source evidence required")
    record = json.loads(row["record"])
    if kind and record["kind"] != kind:
        raise ValueError("Evidence kind mismatch")
    if now and record["observed_at"] > now:
        raise ValueError("Future observation cannot support current action")
    return record


def _job(database, job_key):
    import applications

    job = _one(database, "SELECT * FROM application_jobs WHERE job_key=?", (job_key,))
    if not job or job["requisition"] == "*" or applications.weak_identity(job["requisition"]):
        raise ValueError("Existing canonical durable application job required")
    return job


def _key(value):
    """Canonical identity form: exactly applications.normalized, the stored form.

    Stored employer/requisition columns are normalized by construction; evidence
    facts arrive verbatim from the trusted importer. Fold both sides so only
    case, unicode form and whitespace are ignored. A non-string never matches.
    """
    import applications
    return applications.normalized(value) if isinstance(value, str) else None


def _bound(record, job, contact_id=None):
    facts = record["facts"]
    if _key(facts.get("employer")) != _key(job["employer"]) or _key(facts.get("requisition")) != _key(job["requisition"]):
        raise ValueError("Evidence must identify the exact employer and requisition")
    if contact_id is not None and _key(facts.get("contact_id")) != _key(contact_id):
        raise ValueError("Evidence contact mismatch")
    return facts


def _task(database, identity, job, kind, inputs, deadline=None, owner="orchestrator", blocker=None):
    task_id = "conversion:" + identity
    payload = {"task_id": task_id, "parent_run_id": None, "job_key": job["job_key"] if job["requisition"] else None, "company": job["employer"],
               "requisition": job["requisition"], "requirement_revision": 2, "kind": kind, "inputs": inputs,
               "deadline": deadline, "status": "BLOCKED" if blocker else "PENDING", "attempts": 0,
               "result": None, "blocker": blocker, "next_action": kind, "owner": owner}
    database.execute("INSERT OR IGNORE INTO conversion_tasks(task_id,job_key,kind,payload) VALUES (?,?,?,?)",
                     (task_id, job["job_key"], kind, encoded(payload)))
    return task_id


def _history(database, job, kind, record):
    database.execute("INSERT OR IGNORE INTO application_history(employer,requisition,kind,evidence) VALUES (?,?,?,?)",
                     (job["employer"], job["requisition"], kind, encoded(record)))


def _duplicate(database, job):
    return bool(_identity_records(database, "application_history", job))


def _indeterminate_history(database, job):
    """Stored history matching this job only after normalization, or None.

    The exact-key lookups here and in applications.local_gate cannot see such a
    row, so prior-application existence is indeterminate rather than absent.
    Deliberately NOT folded into _identity_records: _release hashes those rows
    into the reconciliation binding and build_champion_summary digests them.
    """
    if _identity_records(database, "application_history", job):
        return None
    try:
        import external_history
    except ImportError as error:
        raise ValueError("PRIOR_APPLICATION_IDENTITY_ADAPTER_UNSUPPORTED") from error
    return external_history.unmatched_identity_history(database, job)


def _require_determinate_history(database, job):
    """Never mutate an application job while prior-application existence is unknown."""
    unmatched = _indeterminate_history(database, job)
    if unmatched:
        raise ValueError("PRIOR_APPLICATION_IDENTITY_UNRESOLVED: recorded " + unmatched[1]
                         + " history for this employer is stored under requisition '" + unmatched[0]
                         + "', which matches this job only after normalization; verify whether it is the same role"
                         + " and re-record the canonical requisition. Do not release or resubmit while"
                         + " prior-application existence is indeterminate")


def _requisitions(job):
    prefix = job["employer"] + ":"
    original = job["job_key"][len(prefix):] if job["job_key"].startswith(prefix) else job["requisition"]
    return sorted({job["requisition"], original})


def _identity_records(database, table, job):
    if table != "application_history":
        raise ValueError("Unsupported identity history table")
    requisitions = _requisitions(job) + ["*"]
    placeholders = ",".join("?" for unused in requisitions)
    return _all(database, f"SELECT * FROM {table} WHERE employer=? AND requisition IN ({placeholders}) ORDER BY requisition", (job["employer"], *requisitions))


def _uncertain(database, job):
    return job.get("blocker") == "SUBMISSION_OUTCOME_UNKNOWN" or database.execute(
        "SELECT 1 FROM application_attempts WHERE job_key=? AND state='SUBMITTING' LIMIT 1", (job["job_key"],)).fetchone() is not None


def _outcome(database, job, record, kind, discriminator="", attribution_id=None):
    if kind not in REPORTING_KINDS | {"OFFER", "REJECTED", "WITHDRAWN"}:
        raise ValueError("Unsupported verified outcome")
    identity = "conversion-outcome:" + hashlib.sha256(encoded([job["job_key"], kind, discriminator]).encode()).hexdigest()
    attribution = None
    if attribution_id:
        attribution = _evidence(database, attribution_id, "ATTRIBUTION", record["observed_at"])
        facts = _bound(attribution, job)
        origin = facts.get("origin") or {"table": "conversion_events", "key": facts.get("origin_event_id")}
        if facts.get("outcome_evidence_id") != record["evidence_id"] or record["facts"].get("baseline") is True:
            raise ValueError("Attribution must reference this non-baseline outcome")
        import source_records
        action = source_records.execution(database, origin, record["observed_at"])
        if not action or timestamp(action["at"]) >= record["occurred_at"]:
            raise ValueError("Attribution requires an earlier independently verified execution")
        if action.get("employer") != job["employer"] or action.get("requisition") != job["requisition"]:
            raise ValueError("Attribution execution must match employer/requisition")
        if origin["table"] == "application_jobs" and origin["key"] != job["job_key"]:
            raise ValueError("Attribution must preserve the exact application job")
    evidence = {"source": record["source_ref"], "observed_at": record["observed_at"], "verified": True,
                "source_evidence_id": record["evidence_id"], "provider_event_id": record["evidence_id"], "provenance": record,
                "attribution_evidence": attribution, "evidence_basis": "saved authenticated import; no live check"}
    database.execute("INSERT OR IGNORE INTO conversion_outcomes VALUES (?,?,?,?,?,?)",
                     (identity, job["job_key"], kind, record["occurred_at"], int(attribution is not None), encoded(evidence)))


def _interview(database, event, job, record, now):
    facts = _bound(record, job)
    stage_id = _text(facts.get("stage_id"))
    if event["kind"] == "INTERVIEW_STAGE":
        if facts.get("stage") not in STAGES:
            raise ValueError("Supported confirmed interview stage required")
        prior = _one(database, "SELECT * FROM conversion_interviews WHERE job_key=? AND stage_id=?", (job["job_key"], stage_id))
        if prior and record["observed_at"] < prior["observed_at"]:
            raise ValueError("Stale interview stage confirmation")
        if prior and prior["stage"] != facts["stage"]:
            raise ValueError("A different stage requires its own stable stage_id")
        scheduled = timestamp(facts["scheduled_at"]) if facts.get("scheduled_at") else None
        database.execute("""INSERT INTO conversion_interviews(job_key,stage_id,stage,evidence_id,observed_at,scheduled_at)
            VALUES (?,?,?,?,?,?) ON CONFLICT(job_key,stage_id) DO UPDATE SET evidence_id=excluded.evidence_id,
            observed_at=excluded.observed_at,scheduled_at=excluded.scheduled_at""",
                         (job["job_key"], stage_id, facts["stage"], record["evidence_id"], record["observed_at"], scheduled))
        _history(database, job, "INTERVIEW", record)
        if job["state"] not in PROTECTED and not _uncertain(database, job):
            database.execute("UPDATE application_jobs SET state='DUPLICATE',blocker='EXISTING_INTERVIEW',next_at=NULL,next_action=?,updated_at=? WHERE job_key=?",
                             ("Continue the existing interview; do not resubmit", now, job["job_key"]))
        role_id = _role_id(database, job)
        role = _evidence(database, role_id, "ROLE_CONTEXT", now) if role_id else None
        role_facts = _bound(role, job) if role else {}
        topics = {need["topic"] for need in role_facts.get("needs", [])}
        stories = []
        for claim in _all(database, "SELECT evidence_id FROM conversion_evidence WHERE kind='CANDIDATE_CLAIM'"):
            proof = _evidence(database, claim["evidence_id"], "CANDIDATE_CLAIM")
            if proof["observed_at"] <= now and topics.intersection(proof["facts"].get("topics", [])):
                stories.append({"evidence_id": proof["evidence_id"], "text": proof["facts"]["text"]})
        _task(database, event["event_id"] + ":prep", job, "INTERVIEW_PREPARATION",
              {"stage_id": stage_id, "stage": facts["stage"], "evidence_id": record["evidence_id"],
               "guidance": STAGES[facts["stage"]], "role_evidence_id": role_id,
               "role_title": role_facts.get("title"), "role_needs": role_facts.get("needs", []),
               "verified_story_material": stories, "role_specific_material_status": "SOURCED" if role and stories else "MISSING_EVIDENCE",
               "questions": ["What does success look like for this role at this stage?", "Is any part of my experience a concern to clarify?", "What is the expected next step and timeline?"]}, scheduled)
        _task(database, event["event_id"] + ":debrief", job, "CAPTURE_ACTUAL_DEBRIEF",
              {"stage_id": stage_id, "evidence_id": record["evidence_id"], "required_input": "Actual discussion, objections and next steps; user assessment remains self-report"}, owner="user")
        if scheduled is None:
            _task(database, event["event_id"] + ":date", job, "CONFIRM_INTERVIEW_DATE", {"stage_id": stage_id})
        return
    stage = _one(database, "SELECT * FROM conversion_interviews WHERE job_key=? AND stage_id=?", (job["job_key"], stage_id))
    if not stage:
        raise ValueError("Confirmed interview stage required")
    if event["kind"] == "DEBRIEF":
        _text(facts.get("notes"))
        if not isinstance(facts.get("objections", []), list) or any(not isinstance(item, str) or not item.strip() for item in facts.get("objections", [])):
            raise ValueError("Only actually reported objection text is accepted")
        if stage["debrief_evidence_id"] and record["observed_at"] < _evidence(database, stage["debrief_evidence_id"])["observed_at"]:
            raise ValueError("Stale debrief")
        database.execute("UPDATE conversion_interviews SET debrief_evidence_id=? WHERE job_key=? AND stage_id=?", (record["evidence_id"], job["job_key"], stage_id))
        database.execute("UPDATE conversion_tasks SET status='COMPLETE' WHERE job_key=? AND kind='CAPTURE_ACTUAL_DEBRIEF' AND json_extract(payload,'$.inputs.stage_id')=?", (job["job_key"], stage_id))
        for index, objection in enumerate(facts.get("objections", [])):
            _task(database, event["event_id"] + ":objection:" + str(index), job, "CLARIFY_REPORTED_OBJECTION",
                  {"stage_id": stage_id, "objection": objection, "evidence_id": record["evidence_id"], "assessment_source": "user_report"})
    else:
        _text(facts.get("employer_statement"))
        if stage["timeline_evidence_id"] and record["observed_at"] < _evidence(database, stage["timeline_evidence_id"])["observed_at"]:
            raise ValueError("Stale employer timeline")
        due = timestamp(facts["followup_at"]) if facts.get("followup_at") else None
        database.execute("UPDATE conversion_interviews SET timeline_evidence_id=?,followup_at=? WHERE job_key=? AND stage_id=?",
                         (record["evidence_id"], due, job["job_key"], stage_id))
        database.execute("UPDATE conversion_tasks SET status='SUPERSEDED' WHERE job_key=? AND kind='EMPLOYER_TIMELINE_FOLLOWUP' AND json_extract(payload,'$.inputs.stage_id')=?", (job["job_key"], stage_id))
        if due is None:
            _task(database, event["event_id"] + ":timeline", job, "CLARIFY_EMPLOYER_TIMELINE", {"stage_id": stage_id, "evidence_id": record["evidence_id"]})


def _role_id(database, job):
    row = _one(database, "SELECT evidence_id FROM conversion_roles WHERE job_key=?", (job["job_key"],))
    return row["evidence_id"] if row else None


def _apply(database, event, now, policy):
    job = _job(database, event["job_key"])
    record = _evidence(database, event["evidence_id"], event["kind"], now)
    facts = _bound(record, job)
    kind = event["kind"]
    if kind == "ROLE_CONTEXT":
        _text(facts.get("title"))
        previous = _one(database, "SELECT observed_at FROM conversion_roles WHERE job_key=?", (job["job_key"],))
        if previous and record["observed_at"] < previous["observed_at"]:
            raise ValueError("Stale role context")
        database.execute("INSERT INTO conversion_roles VALUES (?,?,?) ON CONFLICT(job_key) DO UPDATE SET evidence_id=excluded.evidence_id,observed_at=excluded.observed_at",
                         (job["job_key"], record["evidence_id"], record["observed_at"]))
    elif kind in {"INTERVIEW_STAGE", "DEBRIEF", "EMPLOYER_TIMELINE"}:
        _interview(database, event, job, record, now)
    elif kind == "OUTCOME":
        outcome = facts.get("outcome")
        if facts.get("observation") != "independent_outcome_read":
            raise ValueError("Acknowledgment or self-assessment is not an employer outcome")
        _text(facts.get("statement"))
        discriminator = ""
        if outcome == "REFERRAL":
            _text(facts.get("receipt_id"))
            discriminator = _text(facts.get("contact_id"))
        if outcome == "RECRUITER_CONVERSATION":
            discriminator = _text(facts.get("conversation_id"))
        _outcome(database, job, record, outcome, discriminator, facts.get("attribution_evidence_id"))
        if outcome in {"INTERVIEW", "OFFER"}:
            _history(database, job, "INTERVIEW", record)
        if outcome in {"OFFER", "REJECTED", "WITHDRAWN"}:
            database.execute("UPDATE conversion_tasks SET status='SUPERSEDED' WHERE job_key=? AND kind='EMPLOYER_TIMELINE_FOLLOWUP'", (job["job_key"],))
        if outcome in {"REJECTED", "WITHDRAWN"}:
            database.execute("UPDATE conversion_tasks SET status='SUPERSEDED' WHERE job_key=? AND kind='GUARDED_CHAMPION_DELIVERY'", (job["job_key"],))


def normalized_outcomes(database):
    """Read-only outcome listing; unknown/baseline origins are explicitly null.

    Returns event_id, kind, occurred_at, evidence, origin, segment, attributed.
    origin is {table, key} only when source-bound attribution passed the verified
    execution adapter. Consumers must recheck execution before counting outcomes.
    """
    result = []
    for row in _all(database, "SELECT * FROM conversion_outcomes ORDER BY at,action_id"):
        evidence = json.loads(row["evidence"])
        attribution = evidence.get("attribution_evidence")
        result.append({"event_id": row["action_id"], "kind": row["kind"], "occurred_at": row["at"],
                       "evidence": evidence, "origin": (attribution["facts"].get("origin") or {"table": "conversion_events", "key": attribution["facts"].get("origin_event_id")}) if attribution else None,
                       "segment": {"job_key": row["job_key"]}, "attributed": bool(row["attributed"])})
    return result


def cycle(database, config, now=None, application_policy=None):
    """Apply saved events independently and return repeatable, idempotent handoffs.

    Config conversion.champion_profiles maps job_key to claim_evidence_ids and
    profile_evidence_id. Missing facts create blockers, never fabricated summaries.
    Output reporting_events uses the reporting.record_outcome envelope. Attributed
    outcomes are never inserted directly (reporting.record_outcome refuses them).
    Caller deduplicates action_id and persists the shared task envelope/run ID.
    """
    now = timestamp(now)
    policy = dict(config.get("conversion", {}))
    result = {"state": "COMPLETE", "applied": [], "blocked": [], "tasks": [], "messages": [], "outcomes": [], "reporting_events": [],
              "adapters": {"source_verification": "TRUSTED_IMPORT_ONLY", "champion_delivery": "UNSUPPORTED", "timeline_followup_delivery": "UNSUPPORTED"}}
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"application_jobs", "application_attempts", "application_history", "conversion_events"}.issubset(tables):
        result.update(state="BLOCKED", blocked=[{"code": "INITIALIZE_APPLICATIONS_AND_CONVERSION_FIRST"}])
        return result
    for event in _all(database, "SELECT * FROM conversion_events WHERE status='PENDING' ORDER BY rowid"):
        database.execute("SAVEPOINT conversion_item")
        try:
            _apply(database, event, now, policy)
            database.execute("UPDATE conversion_events SET status='APPLIED',error=NULL,applied_at=? WHERE event_id=?", (now, event["event_id"]))
        except (ValueError, KeyError, TypeError, sqlite3.IntegrityError) as error:
            database.execute("ROLLBACK TO conversion_item")
            database.execute("UPDATE conversion_events SET status='BLOCKED',error=? WHERE event_id=?", (str(error), event["event_id"]))
        finally:
            database.execute("RELEASE conversion_item")
        status = _one(database, "SELECT status,error FROM conversion_events WHERE event_id=?", (event["event_id"],))
        if status["status"] == "APPLIED":
            result["applied"].append(event["event_id"])
    for stage in _all(database, "SELECT * FROM conversion_interviews WHERE followup_at IS NOT NULL"):
        terminal = database.execute("SELECT 1 FROM conversion_outcomes WHERE job_key=? AND kind IN ('OFFER','REJECTED','WITHDRAWN')", (stage["job_key"],)).fetchone()
        current_stage = _one(database, "SELECT stage_id FROM conversion_interviews WHERE job_key=? ORDER BY observed_at DESC,rowid DESC LIMIT 1", (stage["job_key"],))
        if terminal or current_stage["stage_id"] != stage["stage_id"]:
            database.execute("UPDATE conversion_tasks SET status='SUPERSEDED' WHERE job_key=? AND kind='EMPLOYER_TIMELINE_FOLLOWUP' AND json_extract(payload,'$.inputs.stage_id')=?", (stage["job_key"], stage["stage_id"]))
        elif stage["followup_at"] <= now:
            job = _job(database, stage["job_key"])
            _task(database, "timeline:" + stage["timeline_evidence_id"], job, "EMPLOYER_TIMELINE_FOLLOWUP",
                  {"stage_id": stage["stage_id"], "evidence_id": stage["timeline_evidence_id"],
                   "debrief_evidence_id": stage["debrief_evidence_id"], "status": "TIMELINE_ELAPSED_RESPONSE_UNKNOWN",
                   "next_step": "Reconcile employer response before drafting a contextual guarded follow-up"}, stage["followup_at"])
    result["blocked"].extend(_all(database, "SELECT event_id,job_key,error FROM conversion_events WHERE status='BLOCKED'"))
    if policy.get("champion_handoff_enabled") is True:
        result["adapters"]["champion_delivery"] = "SOURCE_BOUND_REPLY_SHARED_GUARD"
    result["normalized_outcomes"] = normalized_outcomes(database)
    for task in _all(database, "SELECT payload FROM conversion_tasks WHERE status='PENDING' ORDER BY rowid"):
        payload = json.loads(task["payload"])
        result["tasks"].append(payload)
        if payload["kind"] == "GUARDED_CHAMPION_DELIVERY":
            import referrals
            referral = referrals.current(database, payload["inputs"]["contact_id"])
            if referral and referral["state"] in {"AGREED", "SUBMITTED"}:
                result["messages"].append(payload)
    for outcome in _all(database, "SELECT * FROM conversion_outcomes ORDER BY at,action_id"):
        outcome["evidence"] = json.loads(outcome["evidence"])
        outcome["attributed"] = bool(outcome["attributed"])
        result["outcomes"].append(outcome)
        if outcome["kind"] in REPORTING_KINDS:
            result["reporting_events"].append({key: outcome[key] for key in ("action_id", "kind", "at", "attributed", "evidence")} | {"segment": {"job_key": outcome["job_key"]}})
    return result
