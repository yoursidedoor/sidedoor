"""Offline research planning, evidence normalization and held draft generation.

Initialize once, seed reconciled history, then commit plan_research BEFORE any
research starts. All workers share the caller's SQLite database. Other mutations
join an existing transaction using savepoints; they never commit caller work.
Only trusted caller code may supply aliases, historical completeness or resume
text. Worker output cannot change those inputs or authorize external actions.
"""

import contextlib
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import unicodedata
import urllib.parse
import uuid
from zoneinfo import ZoneInfo

import applications
import engines
import owner_seed
import strategy_execution
import user_profile
import workflow_tasks
import paths


MAX_COMPANIES = 60
MAX_CONTACTS = 3
MAX_COMPUTE_MINUTES = 180
PROCESSOR_VERSION = 7
# Owner decision D-1 (2026-09-19, "way too strict", "relax"): three source tiers, named by
# the structured `kind` the worker reports - never inferred from a URL or page prose here.
#   official         employer careers page or employer-authorized ATS: proves a role open and
#                    is a valid application destination.
#   employer_mirror  a listing the employer itself posted on another board (VC portfolio
#                    boards such as Getro/Consider, Wellfound company listings, LinkedIn Jobs
#                    posted by the company page): proves a role open, but is never itself the
#                    application destination; the application stage resolves the employer ATS.
#   public           agencies, staffing firms and scraped aggregators: corroboration only.
MIRROR_RECENT_DAYS = 30
ROLE_FITS = ("STRONG", "PARTIAL", "NONE")
INVITATION_TEMPLATE_VERSION = 2


def designated_resume_sha256():
    """The user's raw uploaded résumé hash: profile.json `resume_raw.sha256` (None without one).

    Claims are keyed by the raw résumé (owner, 2026-10-21, E4.T2): a new upload has a new hash, so
    the old claims drop out of every reader."""
    try:
        return (user_profile.load().get("resume_raw") or {}).get("sha256")
    except user_profile.ProfileMissing:
        return None


def designated_claims():
    """Reviewed claim mappings for the designated résumé (owner seed `designated_claims`).

    Each is an exact résumé sentence with its page and the invitation wording reviewed for it.
    The product carries none: without a seed no claim is registered and drafts wait for one.
    """
    return owner_seed.items("designated_claims")


FACT_FIELDS = (
    "open", "funding_stage", "industry", "level", "specialization", "country",
    "city", "work_mode", "requirements_met", "authorization",
    "hiring_geography_includes_candidate", "citizenship_clearance_requirements_met",
)
# Field-neutral contact types (EW2, owner 2026-10-22): Sidedoor serves every field, so the types
# name a person's relation to the role, not an engineering title. Values stored before the rename
# are read through LEGACY_CONTACT_TYPES wherever a contact type is read, so nothing on file fails.
CONTACT_TYPES = ("functional_executive", "hiring_manager", "senior_peer", "peer", "recruiter")
LEGACY_CONTACT_TYPES = {"technical_executive": "functional_executive", "staff_engineer": "senior_peer",
                        "senior_engineer": "peer"}


def contact_type(value):
    """A stored contact type in the current vocabulary (an old engineering-named value mapped)."""
    return LEGACY_CONTACT_TYPES.get(value, value) if isinstance(value, str) else value


def current_evidence(evidence):
    """`person_evidence` with its contact_type record's value in the current vocabulary (a copy)."""
    if not isinstance(evidence, dict) or not isinstance(evidence.get("contact_type"), dict):
        return evidence
    record = evidence["contact_type"]
    return dict(evidence, contact_type=dict(record, value=contact_type(record.get("value"))))


DEFAULT_POLICY = {
    "minimum_base_salary_usd": 180000,
    "funding_stages": ["Seed", "Series A", "Series B", "Series C", "Series D"],
    "industries": ["FinTech and Finance", "Energy", "Defense", "AI", "Cybersecurity",
                   "Healthcare", "BioTech", "Telecom and Communications"],
    "levels": ["Senior Software Engineer", "Staff Software Engineer", "SDE III"],
}


def timestamp(now=None):
    value = now or dt.datetime.now(dt.timezone.utc)
    if isinstance(value, str):
        value = dt.datetime.fromisoformat(value)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timezone-aware timestamp required")
    return value.astimezone(dt.timezone.utc)


def name_key(value):
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", value).casefold()))


def usable_name(value):
    """A person's name key when it is a real name: at least two words and not LinkedIn's
    "LinkedIn Member" placeholder (the contact finder's rule, linkedin_contacts._name_of)."""
    key = name_key(value) if isinstance(value, str) else ""
    return key if len(key.split()) >= 2 and key != "linkedin member" else None


def profile_key(value):
    parsed = urllib.parse.urlsplit(value)
    hostname = (parsed.hostname or "").casefold()
    path = urllib.parse.unquote(parsed.path).strip("/").split("/")
    if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
            or not (hostname == "linkedin.com" or hostname.endswith(".linkedin.com"))
            or len(path) != 2 or path[0].casefold() != "in" or not path[1]):
        raise ValueError("Canonical LinkedIn person profile required")
    return "https://www.linkedin.com/in/" + unicodedata.normalize("NFKC", path[1]).casefold()


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


@contextlib.contextmanager
def transaction(database):
    nested = database.in_transaction
    savepoint = "research_" + uuid.uuid4().hex
    database.execute("SAVEPOINT " + savepoint if nested else "BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        if nested:
            database.execute("ROLLBACK TO " + savepoint)
            database.execute("RELEASE " + savepoint)
        else:
            database.rollback()
        raise
    else:
        if nested:
            database.execute("RELEASE " + savepoint)
        else:
            database.commit()


def initialize(database):
    statements = (
        "CREATE TABLE IF NOT EXISTS rw_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        # One row per finished qualification session (owner, 2026-10-27): its company count and
        # wall seconds, read by parallel_research.slow_end_seconds for the session time limit.
        "CREATE TABLE IF NOT EXISTS rw_qualification_sessions (id TEXT PRIMARY KEY, recorded_at TEXT NOT NULL, "
        "industry TEXT NOT NULL, companies INTEGER NOT NULL, seconds REAL NOT NULL)",
        # Companies a cut-off research session of a plan already finished (R1, review S9): a later run
        # of the same plan (same day, industry and scope) researches only the rest.
        "CREATE TABLE IF NOT EXISTS rw_saved_companies (plan_id TEXT, company TEXT, saved_at TEXT NOT NULL, PRIMARY KEY(plan_id,company))",
        "CREATE TABLE IF NOT EXISTS rw_aliases (kind TEXT, alias TEXT, canonical TEXT NOT NULL, evidence TEXT NOT NULL, PRIMARY KEY(kind,alias))",
        "CREATE TABLE IF NOT EXISTS rw_baselines (company TEXT PRIMARY KEY, count INTEGER NOT NULL)",
        "CREATE TABLE IF NOT EXISTS rw_contacts (company TEXT, identity TEXT, name TEXT, historical INTEGER NOT NULL, plan_id TEXT, PRIMARY KEY(company,identity))",
        "CREATE TABLE IF NOT EXISTS rw_plans (id TEXT PRIMARY KEY, day TEXT NOT NULL, request_hash TEXT NOT NULL, payload TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS rw_companies (day TEXT, company TEXT, plan_id TEXT NOT NULL, PRIMARY KEY(day,company))",
        "CREATE TABLE IF NOT EXISTS rw_unscoped (id TEXT PRIMARY KEY, day TEXT NOT NULL, charged INTEGER NOT NULL, evidence TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS rw_slots (plan_id TEXT, company TEXT, remaining INTEGER NOT NULL, PRIMARY KEY(plan_id,company))",
        "CREATE TABLE IF NOT EXISTS rw_claims (id TEXT PRIMARY KEY, payload TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS rw_drafts (identity TEXT, version INTEGER, content_hash TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(identity,version), UNIQUE(identity,content_hash))",
        "CREATE TABLE IF NOT EXISTS rw_results (id TEXT PRIMARY KEY, plan_id TEXT, observed_at TEXT NOT NULL, raw TEXT NOT NULL, processed TEXT NOT NULL)",
        # E6.T5: the Networking & Referrals page reads a contact's research name and company by identity.
        "CREATE INDEX IF NOT EXISTS rw_contacts_identity ON rw_contacts(identity)",
    )
    with transaction(database):
        for statement in statements:
            database.execute(statement)


def record_saved_companies(database, plan_id, companies, now):
    """Record company keys a cut-off session of `plan_id` finished (R1, review S9)."""
    with transaction(database):
        # Kept a week, then cleaned (review T5): only the same day's plan ever reads them.
        database.execute("DELETE FROM rw_saved_companies WHERE saved_at < ?",
                         ((timestamp(now) - dt.timedelta(days=7)).isoformat(),))
        for company in companies:
            database.execute("INSERT OR IGNORE INTO rw_saved_companies VALUES (?,?,?)", (plan_id, company, now))


def saved_companies(database, plan_id):
    """Company keys an earlier cut-off session of `plan_id` already finished, in the order saved."""
    if not plan_id:
        return []
    return [row[0] for row in database.execute(
        "SELECT company FROM rw_saved_companies WHERE plan_id=? ORDER BY saved_at, rowid", (plan_id,))]


def canonical(database, kind, value):
    key = name_key(value) if kind == "company" else profile_key(value)
    row = database.execute("SELECT canonical FROM rw_aliases WHERE kind=? AND alias=?", (kind, key)).fetchone()
    return row[0] if row else key


def register_alias(database, kind, alias, target, evidence):
    """Register caller-verified aliases before reservation; refuse ledger merges."""
    if kind not in {"company", "contact"} or not evidence:
        raise ValueError("Verified alias evidence and supported kind required")
    alias_key = name_key(alias) if kind == "company" else profile_key(alias)
    target_key = canonical(database, kind, target)
    if not alias_key or not target_key:
        raise ValueError("Empty identity")
    with transaction(database):
        previous = database.execute("SELECT canonical FROM rw_aliases WHERE kind=? AND alias=?", (kind, alias_key)).fetchone()
        if previous and previous[0] != target_key:
            raise ValueError("Conflicting alias")
        column = "company" if kind == "company" else "identity"
        used = database.execute(f"SELECT 1 FROM rw_contacts WHERE {column}=?", (alias_key,)).fetchone()
        if kind == "company":
            used = used or database.execute("SELECT 1 FROM rw_companies WHERE company=? UNION SELECT 1 FROM rw_baselines WHERE company=?", (alias_key, alias_key)).fetchone()
        referenced = database.execute("SELECT 1 FROM rw_aliases WHERE kind=? AND canonical=?", (kind, alias_key)).fetchone()
        if alias_key != target_key and (used or referenced):
            raise ValueError("Existing identity needs explicit ledger reconciliation")
        database.execute("INSERT OR IGNORE INTO rw_aliases VALUES (?,?,?,?)", (kind, alias_key, target_key, encoded(evidence)))
        if kind == "contact":
            import engagement
            closed = engagement.closure_status(database, alias)
            if closed:
                engagement.close_outreach(database, alias, closed["reason"], closed["closed_at"], source=closed["source"])


def seed_history(database, pipeline, contacts=(), *, history_complete=False):
    """Seed conservative historical counts/identities, never prove live absence.

    history_complete means the caller reconciled all SAVED history and pending
    contacts into these inputs, not that provider history is fresh. False blocks
    new contact slots. Re-importing cannot lower historical counts.
    """
    with transaction(database):
        previous_baselines = dict(database.execute("SELECT company,count FROM rw_baselines"))
        promoted = {}
        for company in pipeline.get("companies", []):
            key = canonical(database, "company", company["company"])
            count = company.get("conservative_known_contact_count", 0)
            if type(count) is not int or count < 0:
                raise ValueError("Nonnegative historical count required")
            database.execute("INSERT INTO rw_baselines VALUES (?,?) ON CONFLICT(company) DO UPDATE SET count=MAX(count,excluded.count)", (key, count))
        for contact in contacts:
            company = canonical(database, "company", contact["company"])
            name = name_key(contact["recipient"])
            identity = canonical(database, "contact", contact["profile"]) if contact.get("profile") else "name:" + name
            existing = database.execute("SELECT identity,historical FROM rw_contacts WHERE company=? AND (identity=? OR name=?)", (company, identity, name)).fetchone()
            if existing:
                if existing[1] == 0:
                    promoted[company] = promoted.get(company, 0) + 1
                    preserved = previous_baselines.get(company, 0) + promoted[company]
                    database.execute("INSERT INTO rw_baselines VALUES (?,?) ON CONFLICT(company) DO UPDATE SET count=MAX(count,excluded.count)", (company, preserved))
                database.execute("UPDATE rw_contacts SET historical=1 WHERE company=? AND identity=?", (company, existing[0]))
            else:
                database.execute("INSERT INTO rw_contacts VALUES (?,?,?,1,NULL)", (company, identity, name))
        for company, count in database.execute("SELECT company,COUNT(*) FROM rw_contacts WHERE historical=1 GROUP BY company").fetchall():
            database.execute("INSERT INTO rw_baselines VALUES (?,?) ON CONFLICT(company) DO UPDATE SET count=MAX(count,excluded.count)", (company, count))
        database.execute("INSERT INTO rw_meta VALUES ('history_seeded',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("true" if history_complete else "false",))


def contact_usage(database, company):
    """People known or reserved at this company: the historical floor, plus new, plus held.

    The rw_baselines floor STAYS. It is the owner's real prior-contact count, written by
    seed_history with MAX(), and dropping it would let outreach exceed three real people at a
    company the owner already knows. What was wrong is that a FAILED research worker also wrote
    into the same table, accumulating with +=, so capacity was spent on nobody and never came
    back (owner, 2026-09-23: "1. SHOULD never exist"). That write is gone; see
    hold_unreported_contact_slots, which now releases the reservations instead.
    """
    baseline = database.execute("SELECT count FROM rw_baselines WHERE company=?", (company,)).fetchone()
    historical = database.execute("SELECT COUNT(*) FROM rw_contacts WHERE company=? AND historical=1", (company,)).fetchone()[0]
    new = database.execute("SELECT COUNT(*) FROM rw_contacts WHERE company=? AND historical=0", (company,)).fetchone()[0]
    held = database.execute("SELECT COALESCE(SUM(remaining),0) FROM rw_slots WHERE company=?", (company,)).fetchone()[0]
    return max(baseline[0] if baseline else 0, historical) + new + held


def plan_research(database, task_id, companies, *, now=None, contact_slots=3, limit=20):
    """Commit an immutable named assignment before work; no unbounded discovery.

    A company is assigned once per service day (starts 09:00 America/Los_Angeles) across all workers.
    Retry the same task_id and exact inputs. Reservations are not auto-released
    on crash, since an interrupted worker might already have done the research.
    Discovery adapters must reserve each named company before inspecting it.
    """
    if database.in_transaction:
        raise ValueError("Plan must commit before launching worker; finish caller transaction first")
    if not task_id or type(contact_slots) is not int or not 0 <= contact_slots <= 3:
        raise ValueError("Stable task identity and 0..3 contact slots required")
    if type(limit) is not int or not 0 <= limit <= MAX_COMPANIES:
        raise ValueError("Company limit must be 0..60")
    current = timestamp(now)
    day = workflow_tasks.service_date(current, "America/Los_Angeles")
    request_hash = digest([companies, contact_slots, limit])
    with transaction(database):
        prior = database.execute("SELECT day,request_hash,payload FROM rw_plans WHERE id=?", (task_id,)).fetchone()
        if prior:
            if prior[1] != request_hash:
                raise ValueError("Task identity reused with different assignment")
            if prior[0] != day:
                raise ValueError("Expired assignment; reserve against the current service day")
            return get_plan(database, task_id, now=current)
        seeded = database.execute("SELECT value FROM rw_meta WHERE key='history_seeded'").fetchone()
        capacity = max(0, MAX_COMPANIES - company_usage(database, day))
        assignments, rejected, seen = [], [], set()
        for item in companies:
            name = item if isinstance(item, str) else item["company"]
            company = canonical(database, "company", name)
            if not company or company in seen:
                continue
            seen.add(company)
            used = database.execute("SELECT 1 FROM rw_companies WHERE day=? AND company=?", (day, company)).fetchone()
            if used or len(assignments) >= min(capacity, limit):
                rejected.append({"company": name, "reason": "ALREADY_ASSIGNED" if used else "COMPANY_BUDGET"})
                continue
            # At most MAX_CONTACTS new people per company per research run, and no lifetime limit
            # (owner, 2026-09-28: "there should be no life time limit. The only limit is at most 3
            # people per research run per company. provided we havent reached out to them before").
            # People contacted before are refused per person by the history checks, not by a count.
            slots = min(contact_slots, MAX_CONTACTS) if seeded and seeded[0] == "true" else 0
            database.execute("INSERT INTO rw_companies VALUES (?,?,?)", (day, company, task_id))
            database.execute("INSERT INTO rw_slots VALUES (?,?,?)", (task_id, company, slots))
            assignments.append({"company": name, "company_key": company, "contact_slots": slots})
        result = {"plan_id": task_id, "day": day, "assignments": assignments, "rejected": rejected,
                  "scope_enforced": True, "max_compute_minutes_per_day": MAX_COMPUTE_MINUTES,
                  "enforcement_level": "COMMITTED_ASSIGNMENTS_AND_INGESTION_ONLY",
                  "tool_scope_enforced": False,
                  "enforcement_limit": "No hard network filter exists. Research of committed named assignments is permitted; remain within those assignments and contact slots.",
                  "compute_budget_owner": "orchestrator", "subscription_only": True,
                  "discovery_adapter": "INDUSTRY_DISCOVERY_THEN_RESERVED_COMPANY_RESEARCH"}
        database.execute("INSERT INTO rw_plans VALUES (?,?,?,?)", (task_id, day, request_hash, encoded(result)))
        return get_plan(database, task_id, now=current)


def get_plan(database, plan_id, *, now=None):
    """Restore same-day retry scope, including remaining slots and known people."""
    row = database.execute("SELECT day,payload FROM rw_plans WHERE id=?", (plan_id,)).fetchone()
    # A recorded scope violation is CARRIED, not a refusal (owner, 2026-09-23: "Fix 3. That
    # should never exist. its a systemic blocker."). This raised for the rest of the LA day and
    # nothing anywhere could satisfy the "reconciliation" it demanded - a gate with no producer.
    # Telecom recorded one at 08:01 when LinkedIn dropped the employer filter, and was dead for
    # the day on every later attempt. The violation is already recorded against the result that
    # caused it, the companies actually touched are already counted, and record_unplanned_attempt
    # no longer charges the shared day, so nothing further is protected by refusing the plan.
    prior_violations = []
    for previous in database.execute("SELECT processed FROM rw_results WHERE plan_id=?", (plan_id,)):
        prior_violations += [str(blocker) for blocker in json.loads(previous[0]).get("blockers", [])
                             if str(blocker).startswith(("RESEARCH_SCOPE_VIOLATION", "OUTSIDE_RESERVED_SCOPE",
                                                         "OUTSIDE_RESERVED_FINDING_SCOPE"))]
    if not row:
        return None
    if row[0] != workflow_tasks.service_date(timestamp(now), "America/Los_Angeles"):
        raise ValueError("Expired assignment")
    plan = json.loads(row[1])
    for assignment in plan["assignments"]:
        remaining = database.execute("SELECT remaining FROM rw_slots WHERE plan_id=? AND company=?", (plan_id, assignment["company_key"])).fetchone()
        assignment["contact_slots"] = remaining[0] if remaining else 0
        assignment["reserved_contacts"] = [row[0] for row in database.execute("SELECT identity FROM rw_contacts WHERE company=? AND plan_id=?", (assignment["company_key"], plan_id))]
    if prior_violations:
        plan["prior_scope_violations"] = sorted(set(prior_violations))
    return plan


def release_unused_slots(database, plan_id, *, completed_read_only=False):
    """Release only after caller confirms all read-only workers finished.

    Interrupted/unknown workers retain reservations; allocated identities never
    disappear. Caller must not release while any worker for this plan is running.
    """
    if completed_read_only is not True:
        raise ValueError("Confirmed completion/reconciliation required")
    with transaction(database):
        database.execute("UPDATE rw_slots SET remaining=0 WHERE plan_id=?", (plan_id,))


def company_usage(database, day):
    known = database.execute("SELECT COUNT(*) FROM rw_companies WHERE day=?", (day,)).fetchone()[0]
    unknown = database.execute("SELECT COALESCE(SUM(charged),0) FROM rw_unscoped WHERE day=?", (day,)).fetchone()[0]
    return known + unknown


PLAN_ID_PATTERN = re.compile(r"industry-research-plan:(\d{4}-\d{2}-\d{2}):[0-9a-f]{12}(?::scope:[0-9a-f]+)?")
# Provider request rejections that happen before any model turn (the request itself is refused).
PRE_MODEL_REJECTIONS = {("invalid_request_error", "invalid_json_schema", 400)}


def _sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def worker_never_reached_model(log_path, output_path):
    """Structural proof that a worker process never reached a model, else None.

    Proof needs all of: no output file (or an empty one), a readable log holding the
    provider's pre-model request-rejection error object, and none of the lines the engine
    writes once a model turn runs. Anything missing or unreadable is not proof.
    """
    try:
        log = Path(log_path)
        text = log.read_text(errors="replace")
        output = Path(output_path) if output_path else None
        output_state = "ABSENT" if output is None or not output.exists() else (
            "EMPTY" if output.stat().st_size == 0 else "PRESENT")
    except (OSError, TypeError, ValueError):
        return None
    if output_state == "PRESENT":
        return None
    try:   # the engine that wrote the log (<log>.engine.json; none: Codex), which knows its model-turn lines
        engine = engines.for_run(log)
    except engines.EngineRecordError:
        return None
    found = engine.pre_model_rejection(text, PRE_MODEL_REJECTIONS)
    if found:
        error, value = found
        return {"log": str(log), "log_sha256": _sha256_file(log), "log_bytes": log.stat().st_size,
                "output": str(output) if output else None, "output_state": output_state,
                "error_type": error["type"], "error_code": error["code"], "error_status": value["status"]}
    return None


def plan_worker_runs(root, plan_id):
    """Every recorded qualification worker run for plan_id, from the industry checkpoints.

    Returns (runs, latest_logs). A run is a checkpoint failure entry for this plan; it is
    'NEVER_REACHED_MODEL' only when the worker exited nonzero and its files prove it
    (worker_never_reached_model). latest_logs are the checkpoints' current qualification
    logs for this plan, so a run that is not (yet) recorded as a failure is never missed.
    """
    match = PLAN_ID_PATTERN.fullmatch(plan_id or "")
    if not match or root is None:
        return [], set()
    runs, latest_logs = [], set()
    for path in sorted(paths.reports(root).glob("parallel-industry-" + match.group(1) + "-*.json")):
        try:
            saved = json.loads(path.read_text())
        except (OSError, ValueError):
            runs.append({"checkpoint": str(path), "classification": "CHECKPOINT_UNREADABLE"})
            continue
        caches = [saved] + [entry.get("checkpoint") for entry in saved.get("reopened_history", []) if isinstance(entry, dict)]
        for cache in caches:
            if not isinstance(cache, dict):
                continue
            result = cache.get("result") if isinstance(cache.get("result"), dict) else {}
            if cache.get("plan_id") == plan_id and result.get("phase") == "qualification" and result.get("log"):
                latest_logs.add(result["log"])
            for failure in cache.get("failures", []):
                if not isinstance(failure, dict) or failure.get("plan_id") != plan_id or failure.get("stage") != "qualification":
                    continue
                exited = (failure.get("error_class") == "RuntimeError"
                          and str(failure.get("diagnostic", "")).startswith("Research worker exited "))
                proof = worker_never_reached_model(failure.get("log"), failure.get("report")) if exited else None
                runs.append({"checkpoint": str(path), "attempt": failure.get("attempt"),
                             "error_class": failure.get("error_class"), "log": failure.get("log"),
                             "output": failure.get("report"),
                             "classification": "NEVER_REACHED_MODEL" if proof else "REACHED_OR_UNPROVEN",
                             "proof": proof})
    return runs, latest_logs


def plan_workers_never_reached_model(runs, latest_logs):
    proven = {run["log"] for run in runs if run["classification"] == "NEVER_REACHED_MODEL"}
    return bool(runs) and len(proven) == len({run.get("log") for run in runs}) and all(
        run["classification"] == "NEVER_REACHED_MODEL" for run in runs) and latest_logs <= proven


def hold_unreported_contact_slots(database, plan_id, *, root=None):
    """Hold a re-entered plan's remaining slots when prior workers may have seen contacts.

    Not held when every recorded worker run of this plan provably never reached a model
    (their slots stay with the plan for the retry). Otherwise the plan's remaining slots are
    set to 0 and the run evidence is recorded in rw_meta (`unreported_contacts:<plan>`) for
    audit only. Nothing in the product releases it: reconcile_contact_holds was removed (E2.T3).
    """
    root = paths.data(root)
    runs, latest_logs = plan_worker_runs(root, plan_id)
    unreached = plan_workers_never_reached_model(runs, latest_logs)
    with transaction(database):
        rows = database.execute("SELECT company,remaining FROM rw_slots WHERE plan_id=? AND remaining>0", (plan_id,)).fetchall()
        if rows and unreached:
            database.execute("INSERT OR REPLACE INTO rw_meta VALUES (?,?)",
                             ("unreported_contacts_not_held:" + plan_id,
                              encoded({"reason": "Every prior worker of this plan provably never reached a model",
                                       "kept_slots": [list(row) for row in rows], "worker_runs": runs,
                                       "observed_at": dt.datetime.now(dt.timezone.utc).isoformat()})))
            return
        # NO permanent baseline charge (owner, 2026-09-23: "1. SHOULD never exist. Fix that
        # systemic blocker and clean it up."). This used to write the unreleased slots into
        # rw_baselines, a table keyed by COMPANY ALONE - no day, no plan, no expiry - and both
        # research and outreach floor their per-company usage at it. A research worker that
        # failed, timed out or was abandoned therefore consumed the human-facing 3-contacts-per-
        # company cap FOREVER, having contacted nobody: 14 companies were permanently
        # un-contactable and 8 of them had never been contacted once, Dharma and Umbrella and
        # Wernham among them. Reading a profile is not messaging a person, so an inspection that
        # was never reported cannot spend contact capacity. The run evidence is still recorded
        # below for audit; the reservations are released instead of being made permanent.
        if rows:
            database.execute("INSERT OR REPLACE INTO rw_meta VALUES (?,?)",
                             ("unreported_contacts:" + plan_id, encoded({"reason": "Prior research may have inspected unreported contacts; recorded for audit, capacity released", "held": [list(row) for row in rows],
                                                                         "worker_runs": runs, "latest_logs": sorted(latest_logs),
                                                                         "held_at": dt.datetime.now(dt.timezone.utc).isoformat()})))
            database.execute("UPDATE rw_slots SET remaining=0 WHERE plan_id=?", (plan_id,))


def record_unplanned_attempt(database, action_id, company_names=(), *, unknown_scope=False, now=None):
    """Record observed unplanned work. The violation is never charged to the shared day.

    This records a violation, never retroactively authorizes research. Persisted
    candidate names from discovery may be planned later, but not called fully
    researched. Unknown unseen web activity cannot be reconstructed from output.

    Until 2026-09-23 an unaccountable tool scope consumed the whole remaining day, and
    parallel_research then refused every other industry while any such charge existed. One
    worker's scope loss therefore cost all eight industries their day. The companies actually
    touched are counted once in rw_companies as usual; nothing further is taken from anyone else.
    """
    day = workflow_tasks.service_date(timestamp(now), "America/Los_Angeles")
    with transaction(database):
        prior = database.execute("SELECT charged FROM rw_unscoped WHERE id=?", (action_id,)).fetchone()
        if prior:
            return {"state": "UNPLANNED_SCOPE", "charged_unknown": prior[0]}
        for name in company_names:
            company = canonical(database, "company", name)
            if company:
                database.execute("INSERT OR IGNORE INTO rw_companies VALUES (?,?,?)", (day, company, "unplanned:" + action_id))
        # A violation is RECORDED, never charged against the shared day (owner, 2026-09-23:
        # "Fix 3. That should never exist. its a systemic blocker."). This used to charge
        # MAX_COMPANIES - company_usage, i.e. THE ENTIRE REMAINING DAY, whenever a worker's
        # tool scope could not be accounted for. On 2026-09-23 one worker's LinkedIn scope loss
        # returned 7 wrong-region names and cost 41 companies, and parallel_research then
        # refused all 8 industries for the rest of the day. The companies the worker actually
        # touched are inserted into rw_companies just above, so the real work is already counted
        # once; the extra charge was pure penalty stacked on top of accounted work.
        charged = 0
        database.execute("INSERT INTO rw_unscoped VALUES (?,?,?,?)", (action_id, day, charged, encoded(list(company_names))))
        return {"state": "UNPLANNED_SCOPE", "charged_unknown": charged, "company_usage": company_usage(database, day)}


PRIOR_CONTACT_TABLES = ("suppressions", "outreach_attempts", "outreach_actions", "outreach_queue",
                        "outreach_action_claims", "outreach_campaign_preparations", "outreach_prepared_campaigns",
                        "outreach_launch_claims", "outreach_owner_records", "drafts",
                        "managed_conversations", "engagement_events", "engagement_stops", "engagement_closures")


LOCAL_HISTORY_UNREADABLE = "LOCAL_HISTORY_UNREADABLE"


def record_keys(database, identity):
    """Every key a record about this profile may be stored under: each alias (engagement.identity_keys)
    in both the outreach form (linkedin.com/in/x) and this module's form (https://www.linkedin.com/in/x)."""
    import engagement  # local: engagement imports orchestrator, which imports this module
    keys = {identity}
    for key in engagement.identity_keys(database, identity):
        keys |= {key, "https://www." + key if key.startswith("linkedin.com/") else key}
    return keys


def prior_contact_record(database, identity, name):
    """The first local record of contact with this person, else None.

    THE one pre-draft check (owner, 2026-10-01: "We shouldn't be checking at send time. that's too
    late. we should be checking at draft time or even before a draft is created." and "We will
    strictly rely on our local DB for existing outreach check"). Called only by reserve_contact,
    before a draft is written. Our own records: the imported contact history (by profile or by
    name), suppressions (by profile or name key), send attempts, outreach actions, queue rows,
    action/launch claims, campaign preparations, prepared campaigns, owner records, the legacy
    drafts mirror, managed conversations, engagement events, stops and closures, under every alias
    of the profile. The outreach tables also make it the per-person uniqueness check: a person who
    already has an outreach action, claim or preparation is never drafted again. An unreadable
    record is never evidence of no contact: it refuses that person for now (fails closed).
    """
    try:
        import engagement  # local: engagement imports orchestrator, which imports this module
        keys = record_keys(database, identity)
        tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        name = name_key(name or "")
        if "history" in tables:
            for row_identity, recipient, profile in database.execute("SELECT identity,recipient,profile FROM history"):
                linked = row_identity in keys or (name and row_identity == "name:" + name) or (
                    name and name_key(recipient or "") == name)
                if not linked and profile:
                    with contextlib.suppress(ValueError, TypeError):
                        linked = bool(keys & set(engagement.identity_keys(database, profile)))
                if linked:
                    return "history"
        placeholders = ",".join("?" for unused in keys)
        # A message the owner rejected in review mode and that was never sent is not contact (owner,
        # 2026-10-22: "A later research run that finds the same person again may draft them anew"):
        # that action, and the owner's decision records on it, are left out here. Anything else
        # about the person, a sent or claimed action included, still refuses.
        import approvals  # local: approvals imports engagement, which imports orchestrator
        rejected = approvals.rejected_unsent_ids(database, keys)
        for table in PRIOR_CONTACT_TABLES:
            if table not in tables:
                continue
            if rejected and table == "outreach_actions":
                if any(row[0] not in rejected for row in database.execute(
                        "SELECT action_id FROM outreach_actions WHERE identity IN (" + placeholders + ")", sorted(keys))):
                    return table
                continue
            if rejected and table == "outreach_owner_records":
                for kind, payload in database.execute(
                        "SELECT kind,payload FROM outreach_owner_records WHERE identity IN (" + placeholders + ")", sorted(keys)):
                    try:
                        about = json.loads(payload).get("action_id")
                    except (TypeError, ValueError, AttributeError):
                        about = None
                    if not (kind in approvals.DECISION_KINDS and about in rejected):
                        return table
                continue
            if database.execute(
                    "SELECT 1 FROM " + table + " WHERE identity IN (" + placeholders + ") LIMIT 1", sorted(keys)).fetchone():
                return table
        if name and "suppressions" in tables and database.execute(
                "SELECT 1 FROM suppressions WHERE identity=?", ("name:" + name,)).fetchone():
            return "suppressions"
    except Exception as error:  # an unreadable record is never evidence of no contact
        return LOCAL_HISTORY_UNREADABLE + ": " + type(error).__name__
    return None


def reserve_contact(database, plan_id, company, profile, recipient, *, now=None):
    """Reserve a person for a draft: the run's slot (rule 2) and the one pre-draft check (rule 3).

    Contact finder and research drafting (owner, 2026-10-01): a person is drafted only after this
    check of our local records (prior_contact_record, which also refuses anyone who already has an
    outreach action, claim or preparation) and the per-person uniqueness of the research ledger
    (one reservation per profile; a drafted person is never reserved again). Nothing re-checks
    contact history at send time for a new contact.
    """
    company = canonical(database, "company", company)
    identity = canonical(database, "contact", profile)
    name = name_key(recipient)
    with transaction(database):
        plan = database.execute("SELECT day FROM rw_plans WHERE id=?", (plan_id,)).fetchone()
        day = workflow_tasks.service_date(timestamp(now), "America/Los_Angeles")
        if not plan or plan[0] != day:
            return {"state": "BLOCKED", "reason": "MISSING_OR_EXPIRED_PLAN"}
        if not database.execute("SELECT 1 FROM rw_slots WHERE plan_id=? AND company=?", (plan_id, company)).fetchone():
            return {"state": "BLOCKED", "reason": "OUTSIDE_RESERVED_SCOPE"}
        rows = database.execute("SELECT identity,historical,plan_id,company FROM rw_contacts WHERE identity=? OR (company=? AND name=?)", (identity, company, name)).fetchall()
        # One definition (owner, 2026-09-23). This was a SIXTH hardcoded copy of the do-not-contact
        # set and it had drifted furthest: it omitted "rafa", so the same person was excluded from
        # outreach and replies but could still be PLANNED as a research contact here. That one
        # name is the only behaviour change, and it excludes someone already excluded everywhere
        # else - it blocks planning a contact, never a reply.
        import owner_seed  # local: a module-level import here is circular via orchestrator
        excluded = name in owner_seed.excluded_names() or not name
        # The contact finder's stops (linkedin_held_verifications: CONTACT_OBSERVED,
        # HELD_VERIFICATION_STOPPED, name-key stops) are deleted (owner, 2026-10-01: "There are too
        # many fucking Bans and rules."). The one pre-draft check is prior_contact_record below.
        if rows:
            previous = rows[0]
            same = all(row[1] == 0 and row[0] == identity and row[3] == company for row in rows)
            if same and previous[2] == plan_id:
                return {"state": "RESERVED", "identity": previous[0], "reason": "RETRY"}
            # Held research contacts (owner, 2026-09-30: "something needs to be able to go back
            # and verify a held contact and possibly write their draft"). A person an earlier run
            # reserved at this same company but never drafted is still OUR new contact, not a
            # historical one: a later run (or the contact finder's re-verification) may draft
            # them on the reservation they already hold, spending no new slot. Anyone with a
            # draft, a historical row, another identity under the same name, an exclusion or a
            # record of prior contact stays refused.
            # A drafted person whose every message the owner rejected (never sent) may be drafted
            # again (owner, 2026-10-22); prior_contact_record below still checks everything else.
            import approvals  # local, as above
            if same and not excluded and (not database.execute(
                    "SELECT 1 FROM rw_drafts WHERE identity=?", (identity,)).fetchone()
                    or approvals.only_rejected(database, record_keys(database, identity))):
                prior = prior_contact_record(database, identity, name)
                if prior and prior.startswith(LOCAL_HISTORY_UNREADABLE):
                    return {"state": "BLOCKED", "identity": previous[0], "reason": LOCAL_HISTORY_UNREADABLE}
                if not prior:
                    return {"state": "RESERVED", "identity": previous[0], "reason": "PRIOR_UNDRAFTED"}
            return {"state": "BLOCKED", "identity": previous[0], "reason": "HISTORICAL_OR_DUPLICATE_CONTACT"}
        if excluded:
            return {"state": "BLOCKED", "reason": "EXCLUDED_OR_UNKNOWN_IDENTITY"}
        # A person never reserved is still checked against our own records of contact (profile
        # suppression, send attempts, history by name under another URL, ...): nobody contacted
        # before is contacted again. Unreadable: refused for now (fails closed).
        prior = prior_contact_record(database, identity, name)
        if prior and prior.startswith(LOCAL_HISTORY_UNREADABLE):
            return {"state": "BLOCKED", "identity": identity, "reason": LOCAL_HISTORY_UNREADABLE}
        if prior:
            return {"state": "BLOCKED", "identity": identity, "reason": "HISTORICAL_OR_DUPLICATE_CONTACT"}
        # HISTORICAL_BASELINE_EXCEEDS_BUDGET deleted (owner, 2026-09-23). It refused a NEW
        # contact because the HISTORICAL count at the company already exceeded 3, which
        # permanently zeroed 8 companies that had old contacts on record. The live per-plan
        # cap below (rw_slots.remaining) is what enforces 3 new people per company per research
        # run; there is no lifetime limit (owner, 2026-09-28).
        changed = database.execute("UPDATE rw_slots SET remaining=remaining-1 WHERE plan_id=? AND company=? AND remaining>0", (plan_id, company))
        if changed.rowcount != 1:
            return {"state": "BLOCKED", "reason": "CONTACT_BUDGET_OR_UNSEEDED_HISTORY"}
        database.execute("INSERT INTO rw_contacts VALUES (?,?,?,0,?)", (company, identity, name, plan_id))
        return {"state": "RESERVED", "identity": identity, "reason": "NEW"}


def effective_timestamp(value):
    """A source's effective time; a date-only value (a posting date) means 00:00 UTC that day."""
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return dt.datetime.combine(dt.date.fromisoformat(value), dt.time(0), dt.timezone.utc)
    return timestamp(value)


def evidence_valid(record, now, max_age=86400):
    """`max_age=None` means the observation does not expire (owner, 2026-09-23).

    A verified person fact - who they are, where they work, what team, whether they can help -
    is a dated observation with a source URL, not a volatile reading. Nothing in the system
    re-derives one, so an age limit turned a verified fact into a permanent block rather than a
    prompt to re-check. A change of company or role produces a NEW research observation and a
    new draft, which is where that is caught. Every other check below still applies in full:
    verified, https source, no credentials in the URL, effective date not after the observation,
    and a non-empty excerpt.
    """
    try:
        source = urllib.parse.urlsplit(record["source"])
        age = (timestamp(now) - timestamp(record["observed_at"])).total_seconds()
        if max_age is None:
            max_age = age if age >= 0 else -1
        effective = effective_timestamp(record.get("effective_at") or record["observed_at"])
        return (record.get("verified") is True and source.scheme == "https" and bool(source.hostname)
                and not source.username and not source.password and 0 <= age <= max_age
                and effective <= timestamp(record["observed_at"])
                and isinstance(record.get("excerpt"), str) and bool(record["excerpt"].strip()))
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def observations(records, now, max_age=86400):
    grouped = {}
    for record in records if isinstance(records, list) else []:
        if isinstance(record, dict) and isinstance(record.get("field"), str):
            grouped.setdefault(record["field"], []).append(record)
    selected, unknowns = {}, []
    for field, entries in grouped.items():
        valid = [entry for entry in entries if evidence_valid(entry, now, max_age)]
        if not valid:
            unknowns.append(field + ": missing, stale or undated evidence")
            continue
        latest = max(timestamp(entry["observed_at"]) for entry in valid)
        newest = [entry for entry in valid if timestamp(entry["observed_at"]) == latest]
        if len({encoded(entry.get("value")) for entry in newest}) != 1:
            unknowns.append(field + ": conflicting evidence")
            continue
        selected[field] = newest[0]
    return selected, unknowns


def open_proof(evidence, now):
    """Classify how a role's `open` fact is proven, from structured evidence kinds only.

    Returns (basis, source_record). basis is OFFICIAL, EMPLOYER_MIRROR_RECENT,
    EMPLOYER_MIRROR_LIVE, or a NOT_PROVEN_* reason. A mirror listing proves the role open
    when no dated employer (official) evidence says the role is closed and it shows a
    posting/update date (effective_at) within MIRROR_RECENT_DAYS of the
    observation, or - undated or older - when the worker observed it live (verified) and
    no dated employer (official) evidence says the role is closed. Public/agency evidence
    never proves open. Nothing here reads excerpts or page text.
    """
    sourced, _ = observations(evidence, now)
    record = sourced.get("open")
    if not record:
        return "NOT_PROVEN_NO_VALID_OPEN_EVIDENCE", None
    if record.get("value") is not True:
        return "NOT_PROVEN_OPEN_NOT_TRUE", record
    if record.get("kind") == "official":
        return "OFFICIAL", record
    if record.get("kind") != "employer_mirror":
        return "NOT_PROVEN_SOURCE_KIND_" + str(record.get("kind")).upper(), record
    # The employer's own dated "closed" outranks any mirror, however recent: mirrors lag
    # the employer's ATS, never the other way round.
    contrary = any(isinstance(entry, dict) and entry.get("field") == "open" and entry.get("kind") == "official"
                   and entry.get("value") is False and evidence_valid(entry, now)
                   for entry in evidence if isinstance(evidence, list))
    if contrary:
        return "NOT_PROVEN_MIRROR_CONTRADICTED_BY_EMPLOYER", record
    if record.get("effective_at"):
        age = timestamp(record["observed_at"]) - effective_timestamp(record["effective_at"])
        if age <= dt.timedelta(days=MIRROR_RECENT_DAYS):
            return "EMPLOYER_MIRROR_RECENT", record
    return "EMPLOYER_MIRROR_LIVE", record


def normalize_opportunity(role, *, now=None, policy=None):
    now = timestamp(now).isoformat()
    if not all(isinstance(role.get(field), str) and role[field].strip() for field in ("company", "role", "url", "rationale")):
        raise ValueError("Opportunity identity and rationale required")
    if urllib.parse.urlsplit(role["url"]).scheme != "https":
        raise ValueError("HTTPS opportunity required")
    employer, requisition, key = applications.identity(role)
    sourced, unknowns = observations(role.get("evidence", []), now)
    facts = {field: sourced.get(field, {}).get("value") for field in FACT_FIELDS}
    boolean_fields = {"open", "requirements_met", "hiring_geography_includes_candidate", "citizenship_clearance_requirements_met"}
    for field, value in facts.items():
        valid_type = type(value) is bool if field in boolean_fields else isinstance(value, str)
        if value is not None and not valid_type:
            facts[field] = None
            unknowns.append(field + ": invalid fact type")
    for field in ("region", "state", "metro", "team_need"):
        facts[field] = sourced.get(field, {}).get("value")
    for field in FACT_FIELDS:
        if facts[field] is None:
            unknowns.append(field + ": unknown")
    official = sourced.get("official_destination", {})
    basis, open_record = open_proof(role.get("evidence", []), now)
    destination_verified = official.get("value") == role["url"] and official.get("kind") == "official"
    # The review is anchored on the official destination read when there is one; a role
    # proven open only by an employer-posted mirror is anchored on that listing instead, so
    # the application gates see a fresh verified role whose destination is still unresolved
    # (APPLICATION_DESTINATION_OR_SCOPE_UNKNOWN) rather than failing research over it.
    anchor = official or (open_record if not basis.startswith("NOT_PROVEN") else {})
    review = {"employer": employer, "requisition": requisition, "source": anchor.get("source", ""),
              "observed_at": anchor.get("observed_at", now), "verified": bool(anchor),
              "official_destination_verified": destination_verified,
              "open_basis": basis,
              "destination_status": "RESOLVED" if destination_verified else "UNRESOLVED",
              "mirror_source": open_record.get("source") if basis.startswith("EMPLOYER_MIRROR") else None,
              "action_scope": "application_only", "facts": facts,
              "employee_count_evidence": sourced.get("employee_count", {}),
              "country_authorization_evidence": sourced.get("country_authorization", {}),
              "compensation": {}, "fact_evidence": sourced}
    # Scope is judged by AI, never by comparing titles (owner decision, 2026-09-19). At
    # research time that judgment is the qualification worker's role_fit (D-3), made against
    # the candidate profile and the policy target levels; it stands in for both scope_fit
    # dimensions here. Without one, `applications.assess` holds the role for scope review.
    fit = role.get("role_fit")
    if isinstance(fit, dict) and fit.get("fit") in ROLE_FITS and isinstance(fit.get("reason"), str) and fit["reason"].strip():
        verdict = {"in_scope": fit["fit"] != "NONE", "reason": "research role_fit " + fit["fit"] + ": " + fit["reason"].strip()[:900]}
        review["scope_fit"] = {dimension: dict(verdict) for dimension in applications.ROLE_FIT_DIMENSIONS}
    # Funding stage and the owner's onsite location exclusions are research's own AI verdicts
    # (owner, 2026-10-07, items 4/5), no longer Python's comparison of the reported text.
    policy_fit = role.get("policy_fit") if isinstance(role.get("policy_fit"), dict) else {}
    for dimension in ("funding_stage", "location"):
        verdict = policy_fit.get(dimension)
        if (isinstance(verdict, dict) and isinstance(verdict.get("in_scope"), bool)
                and isinstance(verdict.get("reason"), str) and verdict["reason"].strip()):
            review.setdefault("scope_fit", {})[dimension] = {
                "in_scope": verdict["in_scope"], "reason": "research policy_fit: " + verdict["reason"].strip()[:900]}
    if review["country_authorization_evidence"].get("value") is not True:
        review["country_authorization_evidence"] = {}
    compensation = sourced.get("compensation", {})
    if isinstance(compensation.get("value"), dict):
        review["compensation"] = {field: compensation["value"].get(field) for field in ("minimum", "maximum", "currency", "period")}
        review["compensation"].update({field: compensation[field] for field in ("source", "observed_at", "verified")})
        conversion = sourced.get("fx", {})
        if conversion:
            review["compensation"]["fx"] = dict(conversion, usd_per_unit=conversion.get("value"))
        target = sourced.get("candidate_base_target", {})
        if target and target.get("kind") == "candidate_specific":
            review["compensation"].update(target=target.get("value"), target_verified_for_candidate=True, target_evidence=target)
    for field in ("employee_count", "compensation", "official_destination"):
        if field not in sourced:
            unknowns.append(field + ": unknown")
    job = {"employer": employer, "requisition": requisition}
    resolved_policy = policy or DEFAULT_POLICY
    assessment = applications.assess(review, job, resolved_policy, now)
    review["compensation_assessment"] = applications.compensation_assessment(review, resolved_policy, now)
    # An absent compensation is still reported in `unknowns` - the owner should see that
    # the posting published no salary - but it no longer holds the role as unresolved
    # research. European and UK postings routinely publish nothing, and the owner asked
    # for that to stop being a refusal. Every other unknown still holds, unchanged.
    blocking = [unknown for unknown in unknowns if unknown != "compensation: unknown"]
    if not assessment and blocking:
        assessment = ("BLOCKED", "RESEARCH_EVIDENCE_UNRESOLVED", "Resolve conflicting or missing evidence")
    country, mode = facts["country"], facts["work_mode"]
    priority = 99
    home_area = mode in {"hybrid", "onsite"} and role.get("in_user_home_area") is True
    if home_area or (country == "US" and mode in {"hybrid", "onsite"}):
        # Research's own judgment that the role is within commuting distance of the user's
        # recorded location ranks first, in any country (owner, 2026-10-14); a US role judged
        # false or null ranks 3.
        priority = 1 if home_area else 3
        # One shared rule, not a second copy of a city set: the profile's onsite/hybrid
        # exclusions (none when it records none). The literal
        # `{"seattle", "san francisco"}` that used to sit here let Bellevue, Redmond,
        # Kirkland, Tacoma and Everett rank as ordinary US onsite roles. `facts["state"]` is
        # already carried through from sourced evidence a few lines above, so where research
        # recorded a state the rule is exact rather than a city list.
        if applications.excluded_onsite_location(facts, resolved_policy):
            priority = 99
    elif country == "US" and mode == "remote":
        priority = 2
    elif facts.get("region") == "Europe" and mode == "remote":
        priority = 4
    interview = sourced.get("interview_at", {})
    upcoming = False
    if interview:
        try:
            upcoming = timestamp(interview["value"]) >= timestamp(now)
        except (ValueError, TypeError):
            unknowns.append("interview_at: absolute timestamp required")
    return dict(role, review=review, job_key=key, unknowns=sorted(set(unknowns)),
                assessment=list(assessment) if assessment else None,
                eligibility="ELIGIBLE_FOR_APPLICATION_GATES" if assessment is None else assessment[0],
                ranking={"location_priority": priority, "upcoming_interview": upcoming,
                         "interview_at": interview.get("value") if upcoming else None,
                         "existing_pipeline_only": bool(interview), "inference": True})


def pipeline_priorities(interviews, *, now=None):
    """Preserve absolute historical interview dates without refreshing confirmation."""
    day = timestamp(now).astimezone(ZoneInfo("America/Los_Angeles")).date()
    output = []
    for interview in interviews:
        try:
            date = dt.date.fromisoformat(interview["date"])
        except (KeyError, ValueError, TypeError):
            continue
        output.append(dict(interview, upcoming_by_saved_date=date >= day,
                           confirmation_freshness="UNKNOWN", existing_pipeline_only=True,
                           missing_details_do_not_block_sourcing=True))
    return sorted(output, key=lambda item: (not item["upcoming_by_saved_date"], item["date"]))


def register_claims(database, claims, *, designated_source_text, source_sha256, designated_sha256):
    """Record claims of the raw résumé (`designated_source_text`, its saved text).

    Owner, 2026-10-21 (E4.T2): no Python check or comparison; a claim is not compared with the
    résumé text and its outreach wording is not compared with the claim. `resume_intake`
    registers every claim its extraction call returns (no judge, "its not necessary").
    The wording's kind is recorded as given:
    `mapping_kind` (e.g. `user_reviewed` for a wording the user edited in Settings), else
    `reviewed_paraphrase` when an `invitation_text` was supplied (the owner seed), else
    `exact_excerpt` (the wording is the claim itself). Returns the claim ids.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", source_sha256) or source_sha256 != designated_sha256:
        raise ValueError("Designated resume hash mismatch")
    ids = []
    with transaction(database):
        for claim in claims:
            text = claim.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("Nonempty claim text required")
            if not isinstance(claim.get("tags"), list) or not claim["tags"]:
                raise ValueError("Caller-verified relevance tags required")
            payload = {"text": text, "tags": claim["tags"], "source_sha256": source_sha256,
                       "source": "designated_resume", "page": claim.get("page"), "verified": True}
            invitation_text = claim.get("invitation_text", text)
            if not isinstance(invitation_text, str) or not invitation_text.strip():
                raise ValueError("Nonempty invitation claim required")
            mapping_kind = claim.get("mapping_kind") or ("reviewed_paraphrase" if "invitation_text" in claim
                                                         else "exact_excerpt")
            payload["invitation_mapping"] = {"text": invitation_text, "kind": mapping_kind,
                                             "source_claim_sha256": digest(text), "version": 1}
            claim_id = digest(payload)
            ids.append(claim_id)
            database.execute("INSERT OR IGNORE INTO rw_claims VALUES (?,?)", (claim_id, encoded(payload)))
        database.execute("INSERT INTO rw_meta VALUES ('designated_resume',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (source_sha256,))
    return ids


def current_claims(database):
    """[(claim_id, claim)] registered for the current designated (raw) résumé hash."""
    designated = database.execute("SELECT value FROM rw_meta WHERE key='designated_resume'").fetchone()
    rows = [(row[0], json.loads(row[1])) for row in database.execute("SELECT id,payload FROM rw_claims ORDER BY id")]
    return [(claim_id, claim) for claim_id, claim in rows if designated and claim.get("source_sha256") == designated[0]]


def seed_designated_claims(database, root=None):
    """Register the owner seed's reviewed claims (`designated_claims`) for the raw résumé.

    Reads the raw résumé's saved text (`reply_resume.context`); the product carries no seed, so
    this registers nothing without one. No excerpt or page comparison (owner, 2026-10-21, E4.T2).
    """
    import reply_resume

    try:
        resume = reply_resume.context(root)
    except ValueError as error:
        return {"state": "BLOCKED", "reason": str(error)}
    claims = designated_claims()
    register_claims(database, claims, designated_source_text=resume["text"],
                    source_sha256=resume["source_sha256"], designated_sha256=resume["source_sha256"])
    return {"state": "VERIFIED_FROM_DESIGNATED_RESUME", "claims": len(claims), "sha256": resume["source_sha256"]}


def map_people(people, company, *, now=None):
    output = []
    for person in people:
        # Person facts do not expire (evidence_valid, owner 2026-09-23): a verified identity or
        # employer is a dated observation, and the 24-hour default meant a person reserved one
        # day and mapped the next was "stale" with nothing ever re-deriving the fact.
        sourced, unknowns = observations(person.get("evidence", []), timestamp(now).isoformat(), max_age=None)
        # Owner, 2026-09-23: team and ability-to-help must never block ("i thought i asked you to
        # completely remove that so it never blocks"). That decision was applied in
        # outreach_promotion.promote() but THIS is the site that decides whether a draft is ever
        # written, and it was missed: on 2026-09-23 all four researched contacts were reserved
        # here and then discarded, three of them over an unreachable linkedin.com (HTTP 999) that
        # left identity evidence verified=False, and Sasha Lind over nothing but a missing team.
        # Every field is still recorded in evidence_by_field and unknowns; only `blocking` decides.
        # A field blocks when its absence leaves no addressable recipient, or when the evidence
        # positively contradicts the proposed contact - never because a source was unreachable.
        # Research is trusted for who a person is (owner, 2026-10-01: "If research is able to find
        # a linkedln url, then that contact needs to be auto promoted for outreach. Research is
        # not going to hallucinate a name"). A person research returned for the company with a
        # LinkedIn /in/ profile and a name is drafted: unverified or missing employment, team or
        # ability-to-help evidence, conflicting identity evidence and a missing employer verdict
        # are recorded in unknowns and never block. Only these block: no usable /in/ profile, no
        # name, and an explicit employer_matches_target False (research's own statement that the
        # person works elsewhere, not a verification failure).
        blocking = []
        for field in ("identity", "employment", "team", "ability_to_help"):
            if not sourced.get(field, {}).get("value"):
                unknowns.append(field + ": unknown")
        # The draft carries whatever evidence research had: a field with no valid (verified,
        # dated, sourced) record keeps research's own last text record for it as given (verified
        # False, undated, ...), so the composer and judge still see it. A field whose valid
        # records conflict keeps none.
        conflicting = {item.split(":")[0] for item in unknowns if item.endswith(": conflicting evidence")}
        research_only = {}
        for record in person.get("evidence") if isinstance(person.get("evidence"), list) else []:
            if (isinstance(record, dict) and isinstance(record.get("field"), str)
                    and isinstance(record.get("value"), str) and record["value"].strip()
                    and record["field"] not in sourced and record["field"] not in conflicting):
                research_only[record["field"]] = record
        sourced = dict(research_only, **sourced)
        if not usable_name(person.get("recipient")):
            unknowns.append("identity: name missing")
            blocking.append("identity: name missing")
        # Whether the person's current employer IS the target company is the AI's structured
        # verdict (owner, 2026-09-30), never a Python comparison of names: the old exact
        # name_key(employment) == name_key(company) test refused "Nakatomi co-founder and CEO"
        # and "Founder and CEO, Wonka Health" and passed only a bare "Humongous". An explicit
        # False (works elsewhere) is not drafted: a note to someone at another company is
        # pointless. A missing verdict is recorded and decides nothing (owner, 2026-10-01: the
        # "not judged" blocker went with the other contact-finder rules). False is research's
        # (or the finder's) own statement, not a failed verification, so it still skips.
        verdict = person.get("employer_matches_target")
        if verdict is False:
            unknowns.append("employment: employer mismatch")
            blocking.append("employment: employer mismatch")
        elif verdict is not True:
            unknowns.append("employment: employer match not judged")
        identity = None
        try:
            identity = profile_key(person.get("profile", ""))
        except ValueError:
            unknowns.append("identity: invalid profile")
            blocking.append("identity: invalid profile")
        # "identity: profile mismatch" is RECORDED, never blocking (owner, 2026-09-23). The
        # profile the draft is addressed to is person["profile"]; the identity evidence is a
        # corroborating search result whose canonical form often differs harmlessly (fr. vs
        # www. subdomain, trailing slug). The mismatch stays visible in unknowns and the AI
        # judge weighs it against provider records before anything sends.
        claimed = sourced.get("identity", {}).get("value", "")
        if identity and claimed:
            try:
                if profile_key(claimed) != identity:
                    unknowns.append("identity: profile mismatch")
            except ValueError:
                unknowns.append("identity: unparseable identity evidence")
        kind = contact_type(sourced.get("contact_type", {}).get("value"))
        rank = {name: index for index, name in enumerate(CONTACT_TYPES, 1)}.get(kind, 6)
        output.append(dict(person, identity=identity, unknowns=unknowns, blocking_unknowns=blocking,
                           evidence_by_field=sourced,
                           mapping_verified=not blocking, rank=rank, history_status="UNKNOWN"))
    return sorted(output, key=lambda person: (not person["mapping_verified"], person["rank"]))


def research_eligible(opportunity):
    """Did research's qualification pass this role? INELIGIBLE (applications.assess: out of scope,
    excluded location, funding stage, clearance/citizenship, salary floor) is research's own
    verdict; a BLOCKED hold (missing evidence, an eligibility question) is not a verdict."""
    return isinstance(opportunity, dict) and opportunity.get("eligibility") != "INELIGIBLE"


def create_draft(database, person, opportunity, *, plan_id, now=None):
    """Generate versioned HELD text from one trusted resume/need connection."""
    with transaction(database):
        return _create_draft(database, person, opportunity, plan_id=plan_id, now=now)


def _create_draft(database, person, opportunity, *, plan_id, now=None):
    # Only an unaddressable person (no /in/ profile, no name) or one research says works elsewhere
    # is refused here (map_people); unverified person evidence never is (owner, 2026-10-01).
    # PERSON_EVIDENCE_MISSING went with it.
    if not person.get("mapping_verified"):
        return {"state": "BLOCKED", "reason": "PERSON_NOT_DRAFTABLE: " + "; ".join(
            person.get("blocking_unknowns") or ["not mapped"])}
    need = opportunity["review"]["facts"].get("team_need")
    topic = opportunity["review"]["facts"].get("specialization")
    # An absent team need is not a blocker (owner, 2026-09-23). The scaffold is only a source
    # binding: the AI composer writes the actual note from the full resume and posting, and
    # role_fact simply omits a field it does not have. Requiring the worker to have emitted one
    # team_need record killed Marco Feld (Krusty Health) and Sasha Lind (Dinoco) outright
    # on 2026-09-23 - both contacts reserved, both roles fully researched, neither draftable.
    # Malformed evidence is still refused; only absence is allowed through.
    if need is not None and (not isinstance(need, str) or not need.strip() or len(need) > 200):
        return {"state": "BLOCKED", "reason": "TEAM_NEED_UNVERIFIED"}
    designated = database.execute("SELECT value FROM rw_meta WHERE key='designated_resume'").fetchone()
    claims = [(row[0], json.loads(row[1])) for row in database.execute("SELECT id,payload FROM rw_claims ORDER BY id")]
    claims = [(claim_id, claim) for claim_id, claim in claims if designated and claim["source_sha256"] == designated[0] and len(claim["text"]) <= 240]
    # The scaffold claim is only a source binding: the AI composer writes the note from the
    # full resume and job description and the AI send judge checks it (owner decisions
    # 2026-09-19 and 2026-10-21), so a role without a recorded specialization still gets a scaffold. A claim
    # tagged with the recorded specialization is preferred; the legacy template text is only
    # sendable with a topic match (outreach_promotion TEMPLATE_CLAIM_TOPIC_MISMATCH).
    claims = [item for item in claims if topic in item[1]["tags"]] or claims
    if not claims:
        return {"state": "BLOCKED", "reason": "NO_VERIFIED_RELEVANT_CANDIDATE_CLAIM"}
    claim_id, claim = min(claims, key=lambda item: (len(item[1].get("invitation_mapping", {}).get("text", item[1]["text"])), item[0]))
    reservation = reserve_contact(database, plan_id, opportunity["company"], person["profile"], person["recipient"], now=now)
    if reservation["state"] != "RESERVED":
        return reservation
    interest = need or opportunity["role"]
    message = (f"Hi {person['recipient']}—I’m exploring {opportunity['role']} at {opportunity['company']}. "
               f"My relevant experience: {claim['text']} "
               + (f"The team’s stated need: {need} " if need else "")
               + "I’d value your perspective on the team’s priorities.")
    first_name = person["recipient"].split()[0]
    invitation_mapping = claim.get("invitation_mapping", {"text": claim["text"], "kind": "exact_excerpt", "source_claim_sha256": digest(claim["text"]), "version": 1})
    invitation = f"Hi {first_name}—{invitation_mapping['text']}. I’m interested in your team’s work on {interest}. Open to connect?"
    invitation_blocker = None
    if len(invitation) > 300:
        invitation = None
        invitation_blocker = "INVITATION_EXCEEDS_300_CHARACTERS: choose a shorter verified claim/need; never truncate"
    payload = {"recipient": person["recipient"], "company": opportunity["company"], "profile": person["profile"],
               "message": message, "source_url": opportunity["url"], "claim_id": claim_id,
               "state": "HELD_VERIFICATION", "history_status": "UNKNOWN", "purpose": "initial_linkedin_message",
               "team_need_evidence": opportunity["review"]["fact_evidence"].get("team_need"),
               "person_evidence": person["evidence_by_field"], "plan_id": plan_id,
               "job_key": opportunity["job_key"], "template_version": INVITATION_TEMPLATE_VERSION,
               "candidate_claim": claim, "invitation_claim_mapping": invitation_mapping}
    payload.update(invitation=invitation, invitation_blocker=invitation_blocker,
                   first_message=message, first_message_limit=2000,
                   invitation_state="HELD_VERIFICATION" if invitation else "BLOCKED_SIZE",
                   first_message_state="HELD_VERIFICATION" if len(message) <= 2000 else "BLOCKED_SIZE",
                   provider_size_gate="PASSED_LOCAL_LIMITS_RECHECK_PROVIDER" if len(message) <= 2000 else "BLOCKED_MESSAGE_TOO_LONG")
    identity = reservation["identity"]
    segment = strategy_execution.segment_for(person, opportunity, claim_id)
    input_hash = digest({"draft": {field: value for field, value in payload.items() if field != "plan_id"},
                         "segment": segment})
    previous = strategy_execution.prior_draft(database, identity, input_hash)
    if previous:
        return previous
    selection = strategy_execution.select(database, identity, segment, timestamp(now).isoformat())
    message, invitation = strategy_execution.render(selection["variant"], person, opportunity, claim, invitation_mapping)
    if len(invitation) > 300:
        invitation = None
    payload.update(message=message, first_message=message, invitation=invitation,
                   strategy_execution=selection, segment=dict(segment, message_variant=selection["variant"]))
    content_hash = digest({field: value for field, value in payload.items() if field != "plan_id"})
    previous = database.execute("SELECT payload FROM rw_drafts WHERE identity=? AND content_hash=?", (identity, content_hash)).fetchone()
    if previous:
        return json.loads(previous[0])
    version = database.execute("SELECT COALESCE(MAX(version),0)+1 FROM rw_drafts WHERE identity=?", (identity,)).fetchone()[0]
    payload.update(version=version, created_at=timestamp(now).isoformat(),
                   draft_id=digest([identity, version, content_hash]))
    database.execute("INSERT INTO rw_drafts VALUES (?,?,?,?)", (identity, version, content_hash, encoded(payload)))
    strategy_execution.record_draft(database, identity, input_hash, payload)
    return payload


def normalize_company_finding(finding, *, now=None):
    """Validate a worker's dated official negative finding, not a live reread."""
    fields = {"company", "status", "source", "observed_at", "excerpt", "verified", "kind"}
    if not isinstance(finding, dict) or set(finding) != fields:
        raise ValueError("Complete closed company-finding evidence required")
    if not isinstance(finding["company"], str) or not finding["company"].strip():
        raise ValueError("Company identity required")
    if finding["status"] not in {"NO_OPEN_ROLES", "NO_ELIGIBLE_ROLES"}:
        raise ValueError("Unresolved findings cannot prove completed research")
    if finding["kind"] != "official" or not evidence_valid(finding, timestamp(now).isoformat()):
        raise ValueError("Dated verified official source and supporting excerpt required")
    return dict(finding)


def _dropped(entry, reason):
    entry = entry if isinstance(entry, dict) else {}
    text = lambda value: " ".join(value.split())[:200] if isinstance(value, str) and value.strip() else None
    source = next((entry[key] for key in ("url", "source") if isinstance(entry.get(key), str) and entry[key].strip()), None)
    # `source` says which posting or record this is (never shown): two different ones stay apart.
    return {"company": text(entry.get("company")), "role": text(entry.get("role")), "reason": reason,
            "source": source[:2048] if source else None}


def check_role(role, *, now=None, policy=None):
    """One role's own checks (its opportunity and its people), with no database: the same code runs
    when the AI saves the role as it goes (research_tools.save_role) and here in process_research.
    Raises KeyError, TypeError or ValueError for an entry that cannot be kept."""
    opportunity = normalize_opportunity(role, now=now, policy=policy)
    people = map_people(role.get("people", []), role["company"], now=now)
    return opportunity, people


def role_fit_of(role):
    """The worker's role_fit as process_research keeps it: {fit, reason}, or fit None when not assessed."""
    fit = role.get("role_fit")
    if isinstance(fit, dict) and fit.get("fit") in ROLE_FITS and isinstance(fit.get("reason"), str):
        return {"fit": fit["fit"], "reason": fit["reason"]}
    return {"fit": None, "reason": "ROLE_FIT_NOT_ASSESSED"}


def role_sourced(role):
    """A researched role (a normalized opportunity): employer and title evidenced, open proven by an
    official or employer-posted mirror source. An unresolved destination does not prevent it."""
    return (role["review"]["verified"]
            and name_key(str(role["review"]["fact_evidence"].get("company", {}).get("value", ""))) == name_key(role["company"])
            and role["review"]["fact_evidence"].get("role_title", {}).get("value") == role["role"]
            and not role["review"]["open_basis"].startswith("NOT_PROVEN"))


def finding_conflicts(finding, opportunities, key):
    """A verified company finding contradicted by a kept role at the same company (`key`: company -> key)."""
    company = key(finding["company"])
    return any(key(role["company"]) == company
               and ((finding["status"] == "NO_OPEN_ROLES" and role["review"]["facts"].get("open") is True)
                    or (finding["status"] == "NO_ELIGIBLE_ROLES" and role["assessment"] is None))
               for role in opportunities)


def covered_companies(result, *, now=None, policy=None):
    """Company name keys a research result covers by the store path's own rule (process_research): a
    sourced role (fit or not) or a verified, uncontradicted company finding. No database (R1 review T1)."""
    current = timestamp(now).isoformat()
    roles, kept = [], []
    for role in result.get("opportunities") or []:
        try:
            opportunity, _ = check_role(role, now=current, policy=policy)
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        roles.append(opportunity)
        if role_fit_of(role)["fit"] != "NONE":   # the store path's own test (review U4)
            kept.append(opportunity)
    covered = {name_key(role["company"]) for role in roles if role_sourced(role)}
    for finding in result.get("company_findings") or []:
        try:
            finding = normalize_company_finding(finding, now=current)
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if not finding_conflicts(finding, kept, name_key):
            covered.add(name_key(finding["company"]))
    return covered


def process_research(database, result, *, plan_id=None, now=None, policy=None, done=()):
    """Store raw evidence and return safe records for main's store_research.

    No applications/outreach tables are changed. Main ingests opportunities and
    persists their reviews under its shared task envelope in the same transaction.
    Sparse legacy output is retained but unplanned results cannot create drafts
    or fully eligible reviews. All external execution remains separately gated.
    """
    current = timestamp(now).isoformat()
    # `done`: company keys an earlier, cut-off session of this plan already stored (R1, owner
    # 2026-10-27, "3: a"); a retry's coverage is judged over the companies not yet done.
    done = set(done or ())
    result_id = digest([PROCESSOR_VERSION, plan_id, result] + ([sorted(done)] if done else []))
    with transaction(database):
        prior = database.execute("SELECT processed FROM rw_results WHERE id=?", (result_id,)).fetchone()
        if prior:
            return json.loads(prior[0])
        plan = database.execute("SELECT payload FROM rw_plans WHERE id=?", (plan_id,)).fetchone() if plan_id else None
        plan = json.loads(plan[0]) if plan else None
        permitted = {entry["company_key"] for entry in plan["assignments"]} if plan else set()
        output = {"research_status": result.get("research_status", "PARTIAL"), "summary": result.get("summary", ""),
                  "opportunities": [], "drafts": [], "people": [], "blockers": list(result.get("blockers", [])),
                  "action_blockers": list(result.get("action_blockers", [])),
                  "company_findings": [], "verified_company_findings": [],
                  "result_id": result_id, "plan_id": plan_id, "adapter_status": "OFFLINE_PROCESSOR_ONLY"}
        violation = result.get("scope_violation", {"unknown": False, "companies": []})
        if (not isinstance(violation, dict) or type(violation.get("unknown")) is not bool
                or not isinstance(violation.get("companies"), list)
                or any(not isinstance(company, str) or not company.strip() for company in violation["companies"])):
            # A MALFORMED field is not evidence of a violation (owner, 2026-09-23). This used
            # to default to unknown=True, so a worker that merely returned the wrong shape for
            # this one field was treated as having gone off-scope. Paired with the whole-day
            # charge above, a typo in one field cost all eight industries their day. The
            # malformation is surfaced as a blocker below; it is not converted into a finding.
            violation = {"unknown": False, "companies": [], "malformed": True}
        whole_result_invalid = bool(violation["unknown"] or violation["companies"] or not plan)  # malformed field alone does not invalidate
        if violation["unknown"] or violation["companies"]:
            record_unplanned_attempt(database, result_id + ":scope", violation["companies"],
                                     unknown_scope=violation["unknown"], now=current)
            output["blockers"].append("RESEARCH_SCOPE_VIOLATION: reconcile recorded scope before more research")
        elif violation.get("malformed"):
            output["blockers"].append("RESEARCH_SCOPE_FIELD_MALFORMED: scope_violation had the wrong shape; "
                                      "recorded as unreadable, not as a violation")
        if not plan:
            output["blockers"].append("RESEARCH_PLAN_MISSING: legacy evidence only; reserve assignments before researching")
        if plan and plan["day"] != workflow_tasks.service_date(timestamp(current), "America/Los_Angeles"):
            output["blockers"].append("EXPIRED_RESEARCH_PLAN: replan before further research")
            permitted = set()
            whole_result_invalid = True
        findings = result.get("company_findings", [])
        if not isinstance(findings, list):
            output["blockers"].append("INVALID_COMPANY_FINDINGS: array required")
            findings = []
        # Per-company diagnostics for owner decision D-2: one unresolved company is recorded
        # individually and never fails the whole industry by itself.
        diagnostics = {}
        unfit_roles = []
        output["rejected_opportunities"] = []
        # One bad entry is dropped on its own, the rest kept (owner, 2026-10-27); each drop is
        # listed with its company, role and a plain reason for the Failures and Logs pages.
        output["dropped_entries"] = []
        for finding in findings:
            try:
                if not isinstance(finding, dict) or not isinstance(finding.get("company"), str):
                    raise ValueError("Company finding identity required")
                company = canonical(database, "company", finding["company"])
                if company not in permitted:
                    # Only this entry is dropped (owner, 2026-10-27, review R4); it is never stored.
                    output["blockers"].append("OUTSIDE_RESERVED_FINDING_SCOPE: " + finding["company"])
                    output["dropped_entries"].append(_dropped(finding, "it was outside today's planned companies"))
                    record_unplanned_attempt(database, result_id + ":finding:" + company, [finding["company"]], now=current)
                    continue
                if finding.get("status") == "UNRESOLVED":
                    # The worker's structured record of an ambiguous company: a diagnostic,
                    # not an invalid finding.
                    diagnostics.setdefault(company, []).append({"type": "WORKER_UNRESOLVED_FINDING", "finding": finding})
                    continue
                try:
                    normalized = normalize_company_finding(finding, now=current)
                except (KeyError, TypeError, ValueError) as error:
                    diagnostics.setdefault(company, []).append({"type": "INVALID_COMPANY_FINDING", "reason": str(error), "finding": finding})
                    raise
                output["company_findings"].append(normalized)
            except (KeyError, TypeError, ValueError):
                output["blockers"].append("INVALID_COMPANY_FINDING: dated official negative evidence required")
                output["dropped_entries"].append(_dropped(finding, "the company finding did not have a dated official source"))
        for role in result.get("opportunities", []):
            # One role is one unit (review R3, 2026-10-27): its opportunity, people, drafts and contact
            # reservations are kept together or undone together when the role is dropped.
            marks = {key: len(output[key]) for key in ("opportunities", "drafts", "people", "action_blockers",
                                                       "blockers", "rejected_opportunities")}
            unfit_mark = len(unfit_roles)
            try:
                with transaction(database):
                    company = canonical(database, "company", role["company"])
                    if plan and company not in permitted:
                        output["blockers"].append("OUTSIDE_RESERVED_SCOPE: " + role["company"])
                        output["dropped_entries"].append(_dropped(role, "it was outside today's planned companies"))
                        record_unplanned_attempt(database, result_id + ":" + company, [role["company"]], now=current)
                        continue
                    opportunity, people = check_role(role, now=current, policy=policy)
                    if not plan:
                        opportunity["review"]["verified"] = False
                        opportunity["eligibility"] = "BLOCKED"
                        opportunity["assessment"] = ["BLOCKED", "RESEARCH_PLAN_MISSING", "Reserve before researching"]
                    # Owner decision D-3: the worker judges fit against the candidate profile;
                    # Python enforces it. NONE is never stored; STRONG/PARTIAL are.
                    opportunity["role_fit"] = role_fit_of(role)
                    output["people"].extend(people)
                    if opportunity["role_fit"]["fit"] == "NONE":
                        output["rejected_opportunities"].append({
                            "company": role["company"], "role": role["role"], "url": role["url"],
                            "reason": "ROLE_FIT_NONE", "role_fit": opportunity["role_fit"],
                            "open_basis": opportunity["review"]["open_basis"], "job_key": opportunity["job_key"]})
                        unfit_roles.append(opportunity)
                        if plan:
                            # Inspected people still occupy their lifetime ledger slots.
                            for person in people:
                                if person.get("identity") and person.get("recipient"):
                                    reserve_contact(database, plan_id, role["company"], person["profile"], person["recipient"], now=current)
                        continue
                    output["opportunities"].append(opportunity)
                    # Research's own ineligibility verdict on the role (owner, 2026-10-01: "or if
                    # research says the company is not eligible. e.g outside my preferred location,
                    # or funding stage, etc..."): its people are not drafted. Not a new check:
                    # `applications.assess` already returned INELIGIBLE for it in
                    # normalize_opportunity (out of scope, location, funding stage, clearance, salary).
                    if plan and not research_eligible(opportunity):
                        for person in people:
                            output["action_blockers"].append(role["company"] + " / " + str(person.get("recipient") or "unknown contact")
                                                             + ": RESEARCH_INELIGIBLE_ROLE: " + str((opportunity.get("assessment") or [None, None])[1]))
                        continue
                    if plan:
                        for person in people:
                            if not person["mapping_verified"]:
                                # No /in/ profile, no name, or research says they work elsewhere:
                                # skipped before a reservation, so no slot is spent on them.
                                output["action_blockers"].append(role["company"] + " / " + str(person.get("recipient") or "unknown contact")
                                                                 + ": PERSON_NOT_DRAFTABLE: " + "; ".join(person["blocking_unknowns"]))
                                continue
                            if person.get("identity") and person.get("recipient"):
                                reservation = reserve_contact(database, plan_id, role["company"], person["profile"], person["recipient"], now=current)
                                if reservation["state"] != "RESERVED":
                                    ordinary_hold = reservation["reason"] in {"HISTORICAL_OR_DUPLICATE_CONTACT", "EXCLUDED_OR_UNKNOWN_IDENTITY", "HISTORICAL_BASELINE_EXCEEDS_BUDGET",
                                                                              LOCAL_HISTORY_UNREADABLE}
                                    output["action_blockers" if ordinary_hold else "blockers"].append(
                                        role["company"] + " / " + person["recipient"] + ": " + reservation["reason"])
                                    continue
                            draft = create_draft(database, person, opportunity, plan_id=plan_id, now=current)
                            if draft["state"] == "HELD_VERIFICATION":
                                output["drafts"].append(draft)
                            else:
                                output["action_blockers"].append(role["company"] + " / " + person.get("recipient", "unknown contact") + ": " + draft["reason"])
            except (KeyError, TypeError, ValueError) as error:
                for key, mark in marks.items():
                    del output[key][mark:]
                del unfit_roles[unfit_mark:]
                output["blockers"].append("INVALID_RESEARCH_ITEM: " + type(error).__name__)
                output["dropped_entries"].append(_dropped(role, "the role entry was incomplete or malformed"))
                if isinstance(role, dict) and isinstance(role.get("company"), str):
                    diagnostics.setdefault(canonical(database, "company", role["company"]), []).append(
                        {"type": "INVALID_RESEARCH_ITEM", "role": role.get("role"), "error": type(error).__name__})
        conflicted = set()
        for finding in output["company_findings"]:
            company = canonical(database, "company", finding["company"])
            if finding_conflicts(finding, output["opportunities"], lambda value: canonical(database, "company", value)):
                output["blockers"].append("CONFLICTING_COMPANY_FINDING: " + finding["company"])
                conflicted.add(company)
                diagnostics.setdefault(company, []).append({"type": "CONFLICTING_COMPANY_FINDING", "finding": finding})
            elif finding not in output["verified_company_findings"]:
                output["verified_company_findings"].append(finding)
        if result.get("drafts"):
            output["blockers"].append("UNVERIFIED_WORKER_DRAFTS_HELD: regenerate from designated claims and person evidence")
        output["opportunities"].sort(key=lambda role: (not role["ranking"]["upcoming_interview"], role["ranking"]["interview_at"] or "", role["ranking"]["location_priority"], role["job_key"]))
        sourced = role_sourced
        sourced_roles = [role for role in output["opportunities"] + unfit_roles if sourced(role)]
        for role in output["opportunities"] + unfit_roles:
            if not sourced(role):
                diagnostics.setdefault(canonical(database, "company", role["company"]), []).append(
                    {"type": "ROLE_NOT_PROVEN_OPEN", "role": role["role"], "url": role["url"],
                     "open_basis": role["review"]["open_basis"]})
        coverage = {canonical(database, "company", item["company"])
                    for item in sourced_roles + output["verified_company_findings"]} - conflicted
        permitted = permitted - done
        output["company_coverage"] = {"covered": sorted(coverage & permitted), "uncovered": sorted(permitted - coverage)}
        output["company_resolution"] = {
            "resolved": [{"company": company, "status": "RESOLVED",
                          "roles": [role["role"] for role in sourced_roles
                                    if canonical(database, "company", role["company"]) == company],
                          "findings": [finding["status"] for finding in output["verified_company_findings"]
                                       if canonical(database, "company", finding["company"]) == company]}
                         for company in sorted(coverage & permitted)],
            "unresolved": [{"company": company, "status": "UNRESOLVED",
                            "diagnostics": diagnostics.get(company) or [{"type": "NO_RESEARCH_EVIDENCE_RETURNED"}]}
                           for company in sorted(permitted - coverage)],
            "worker_inability": result.get("inability")}
        if not output["opportunities"] and not output["verified_company_findings"]:
            output["blockers"].append("NO_SUBSTANTIVE_RESEARCH_EVIDENCE: roles or verified company findings required")
        if output["research_status"] == "COMPLETE" and permitted - coverage:
            output["blockers"].append("COMPANY_COVERAGE_INCOMPLETE: " + ", ".join(sorted(permitted - coverage)))
        if not isinstance(output["summary"], str) or not output["summary"].strip():
            output["blockers"].append("RESEARCH_SUMMARY_MISSING")
        if output["research_status"] not in {"COMPLETE", "PARTIAL", "FAILED"} or output["blockers"]:
            output["research_status"] = "PARTIAL"
        # The industry completes whenever its research actually ran: a company resolved, or the
        # worker investigated the companies it could not resolve and said why - even when no
        # company had a fitting role (owner, 2026-09-28, of a day whose Telecom companies had no
        # current openings: "please remove this rule entirely. This is a successful research.").
        # It fails only when nothing was researched (an empty result, where every company carries
        # only the NO_RESEARCH_EVIDENCE_RETURNED placeholder) or the result is untrustworthy as a
        # whole (no/expired plan, scope breach).
        investigated = bool(output["company_resolution"]["resolved"]) or any(
            diagnostics.get(company) for company in permitted - coverage)
        output["industry_status"] = "COMPLETE" if investigated and not whole_result_invalid else "FAILED"
        database.execute("INSERT INTO rw_results VALUES (?,?,?,?,?)", (result_id, plan_id, current, encoded(result), encoded(output)))
        return output
