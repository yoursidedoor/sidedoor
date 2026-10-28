"""Parallel industry discovery and qualification, with caller-thread DB ownership.

The caller owns scheduling, its global deadline, and worker signal cleanup.
research_inputs must accept industry= and discovery= and return (plan, plan_id, prompt, command).
It must suffix its plan identity with ':scope:' + config['_research_scope_id'].
research_company_limit defaults to 12 TOTAL across industries, not per industry.
"""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import datetime as dt
import hashlib
import inspect
import json
import queue
import math
from pathlib import Path
import re
import subprocess
import threading
import time

import industry_discovery
import log
import provider_limits
import research_workflow
import research_recovery
import research_tools
import workflow_tasks
import user_profile
import paths


TIMEZONE = "America/Los_Angeles"
LOG = log.get(__name__)

# Session time limits (owner, 2026-10-27, from his own data: discovery sessions took 19-46 s;
# qualification about 67 s per company at the median, 100 s at the p90 "slow end", 135 at most).
DISCOVERY_SECONDS = 300          # a discovery session, flat
SESSION_CEILING_SECONDS = 3600   # the code's hard ceiling on any one session
QUALIFICATION_BASE_SECONDS = 120
QUALIFICATION_SLOW_FACTOR = 1.5
DEFAULT_SLOW_END_SECONDS = 100   # seconds per company until the install has its own record
SLOW_END_SESSIONS = 20           # the install's last 20 recorded qualification sessions...
SLOW_END_MINIMUM = 5             # ...once at least 5 are recorded
SESSIONS_KEPT = 100              # rw_qualification_sessions keeps its newest 100 rows
RETRY_MARGIN_SECONDS = 300       # "plus 5 min" in the run deadline


def slow_end_seconds(database):
    """The install's own p90 seconds per company over its last 20 recorded qualification sessions
    (nearest rank); DEFAULT_SLOW_END_SECONDS until at least 5 are recorded."""
    try:
        rows = database.execute("SELECT seconds,companies FROM rw_qualification_sessions WHERE companies>0 "
                                "ORDER BY recorded_at DESC LIMIT ?", (SLOW_END_SESSIONS,)).fetchall()
    except Exception as error:
        LOG.warning("Could not read past research session times (%s); using %d seconds per company.",
                    type(error).__name__, DEFAULT_SLOW_END_SECONDS)
        rows = []
    if len(rows) < SLOW_END_MINIMUM:
        return DEFAULT_SLOW_END_SECONDS
    per_company = sorted(row[0] / row[1] for row in rows)
    return per_company[math.ceil(0.9 * len(per_company)) - 1]


def record_session(database, identity, at, industry, companies, seconds):
    """One qualification session's company count and wall seconds; the newest SESSIONS_KEPT are kept."""
    with database:
        database.execute("INSERT OR REPLACE INTO rw_qualification_sessions VALUES (?,?,?,?,?)",
                         (identity, at, industry, companies, seconds))
        database.execute("DELETE FROM rw_qualification_sessions WHERE id NOT IN (SELECT id FROM "
                         "rw_qualification_sessions ORDER BY recorded_at DESC LIMIT ?)", (SESSIONS_KEPT,))


def qualification_seconds(database, companies):
    """120 s + 1.5 x slow end x the companies in that session, within the 3600 s ceiling."""
    return min(SESSION_CEILING_SECONDS, math.ceil(QUALIFICATION_BASE_SECONDS + QUALIFICATION_SLOW_FACTOR
                                                  * slow_end_seconds(database) * max(1, int(companies))))


def initial_deadline_seconds():
    """The run's starting deadline: a discovery session plus one retry plus 5 minutes."""
    return 2 * DISCOVERY_SECONDS + RETRY_MARGIN_SECONDS


class ComputeWait(RuntimeError):
    """The shared daily compute allowance is spent: a WAIT, never a failed investigation.

    AGENTS.md: "Compute waits are not failed investigations." An empty budget is not
    evidence that research is unresolvable, so it must not reach the AI-judged
    abandonment path (which itself needs compute) and must not spend the day's
    investigation attempt budget. It is a RuntimeError subclass so that every existing
    caller that catches RuntimeError/Exception keeps its current behaviour; only the
    handlers that must distinguish a wait from a failure test for this type.
    """

    def __init__(self, message, stage="planning"):
        super().__init__(message)
        self.stage = stage      # the phase whose reservation was refused, not the last one run


def _day(value):
    """The research-day label: the local date on which the service day containing `value`
    started (workflow_tasks.service_date; day_start_hour 9 since 2026-09-30, the same hour
    research runs at, so it equals daily_schedule.due_day). Hour 0 is the local date."""
    return workflow_tasks.service_date(dt.datetime.fromisoformat(value.replace("Z", "+00:00")), TIMEZONE)


def next_compute_reset(value):
    """The next shared-compute reset (the next service-day start, 9:00 local since
    2026-09-30) strictly after `value`.

    Reservations are summed per service day (workflow_tasks.reserve_compute / budget_day),
    so the reset boundary is the earliest instant at which the allowance itself renews.
    """
    return workflow_tasks.next_day_start(dt.datetime.fromisoformat(value.replace("Z", "+00:00")), TIMEZONE).isoformat()


def _label(industry):
    name = re.sub(r"[^a-z0-9]+", "-", industry.casefold()).strip("-")[:60]
    return name + "-" + hashlib.sha256(industry.encode()).hexdigest()[:10]


# Research-scope fingerprint: an ALLOWLIST of application-policy fields that can change
# what research is valid. Editing an application-only field must never invalidate saved
# industry research; changing a research criterion must always invalidate it.
#
# INCLUDED research criteria (parallel_research/industry_discovery/research_workflow read
# every one of them, or orchestrator.research_inputs puts them in the research prompt):
#   industries               - the industry list drives discovery and the role industry gate
#                              (industry_discovery._scope, applications.eligibility).
#   funding_stages           - discovery search filter and role eligibility gate
#                              (industry_discovery.inputs, applications.eligibility).
#   levels                   - role eligibility gate (applications.eligibility).
#   minimum_base_salary_usd  - compensation floor for role eligibility.
#   desired_base_salary_usd  - targeting band carried in the qualification prompt policy.
#   location_priority        - the research-side expression of geographic targeting/ranking.
#   countries                - the accepted countries (ISO codes) research's policy_fit.location
#                              judges against (E1.T7; absent = no restriction).
#   resume {path, sha256}    - the designated resume produces the verified candidate claims
#                              used for fit assessment and contact relevance.
# INCLUDED candidate answers, matched by `semantic` and reduced to their value:
#   currently_authorized_to_work_in_us - drives authorization/eligibility research.
#   current_country                    - hiring_geography_includes_candidate is researched
#                                        relative to the candidate; a country move
#                                        genuinely invalidates that finding.
#   current_state_or_region            - kept deliberately (the review's recommendation):
#                                        hybrid/onsite geography is region-sensitive. Fails
#                                        safe; it never reuses research after a real move.
# INCLUDED fail-closed: every top-level key that is neither classified as a research
#   criterion nor as application-only lands in "unclassified" WITH its value, so a future
#   policy field (a new research constraint, a company/contact cap or scope rule added to
#   this file) rotates the scope until it is deliberately classified here.
#
# EXCLUDED, with reasons:
#   source                          - provenance prose about where the config came from.
#   answers.*.source                - provenance prose; rewording it is not a criteria change.
#   referrals_block_applications    - application/referral sequencing only (AGENTS.md).
#   target_specializations          - judged only by the application read (E1.T7).
#   resume                          - the application résumé (ATS file, E4.T2); research reads
#                                     the raw upload, `resume_raw`, which is a criterion.
#   preapproved_applications        - application authorization only.
#   max_attempts, max_reconcile_attempts, retry_delays_seconds - application attempt/retry
#                                     tuning; research attempts are bounded by config.json.
#   every other answers entry       - application form answers: full_name, email, phone,
#                                     linkedin, and any NEW
#                                     application-only answer key. Adding one must not
#                                     invalidate research; that is the reported defect.
# Consequence to keep in mind: a genuinely research-relevant NEW answer semantic must be
# added to RESEARCH_ANSWER_SEMANTICS, because unknown answer semantics are ignored here.
RESEARCH_CRITERIA = ("countries", "desired_base_salary_usd", "funding_stages", "industries", "levels",
                     "location_priority", "minimum_base_salary_usd", "resume_raw")
RESEARCH_ANSWER_SEMANTICS = ("current_city", "current_country", "current_location",
                             "current_state_or_region", "currently_authorized_to_work_in_us")
APPLICATION_ONLY_SETTINGS = ("answers", "max_attempts", "max_reconcile_attempts",
                             "preapproved_applications", "referrals_block_applications",
                             "retry_delays_seconds", "source", "target_specializations",
                             # E4.T2: the application résumé; research reads the raw one (resume_raw).
                             "resume",
                             # Setup's raw picks (E4.T1): research reads the same choices from
                             # `levels` and `location_priority`, so this copy never changes its scope.
                             "setup_picks")


def research_policy(policy):
    """Return the research-validity projection of the application policy, unmutated."""
    if not isinstance(policy, dict):
        return {"criteria": {}, "answers": {}, "unclassified": {"policy": policy}}
    answers = {}
    supplied = policy.get("answers")
    if isinstance(supplied, dict):
        for key in sorted(supplied):
            answer = supplied[key]
            if isinstance(answer, dict) and answer.get("semantic") in RESEARCH_ANSWER_SEMANTICS:
                answers.setdefault(answer["semantic"], []).append(answer.get("value"))
    return {"criteria": {key: policy[key] for key in RESEARCH_CRITERIA if key in policy},
            "answers": answers,
            "unclassified": {key: value for key, value in policy.items()
                             if key not in RESEARCH_CRITERIA and key not in APPLICATION_ONLY_SETTINGS}}


def scope_id(policy):
    return hashlib.sha256(json.dumps(["research-scope-v2", research_policy(policy)], sort_keys=True).encode()).hexdigest()


def checkpoint_path(root, day, industry, scope):
    return paths.reports(root) / ("parallel-industry-" + day + "-" + _label(industry)
                                    + ".scope-" + scope + ".json")


def _diagnostic(result):
    fields = ("state", "worker_status", "research_status", "discovery_status", "phase", "summary", "error", "error_type", "blockers",
              "worker_inability", "shared_owners", "plan_id", "result_id", "completed_at",
              "recovery_state", "abandonment_reason", "inability", "scope_violation")
    value = {key: result[key] for key in fields if key in result}
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True)
    return {"saved_result_excerpt": encoded[:8000], "truncated": len(encoded) > 8000}


PRIOR_CHECKPOINT_DAYS = 7
PRIOR_CHECKPOINT_FILES = 8


def _mtime(path):
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _prior_checkpoint_paths(reports, day, industry, current):
    """Bounded look-back over this industry's own earlier checkpoint files, newest day first."""
    label = _label(industry)
    start = dt.date.fromisoformat(day)
    paths = []
    for offset in range(PRIOR_CHECKPOINT_DAYS):
        basename = "parallel-industry-" + (start - dt.timedelta(days=offset)).isoformat() + "-" + label
        legacy = reports / (basename + ".json")
        if legacy != current and legacy.exists():
            paths.append(legacy)
        paths.extend(sorted((path for path in reports.glob(basename + ".scope-*.json") if path != current),
                            key=_mtime, reverse=True))
        if len(paths) >= PRIOR_CHECKPOINT_FILES:
            break
    return paths[:PRIOR_CHECKPOINT_FILES]


def _prior_checkpoints(database, reports, day, industry, current):
    """Audit-only legacy context for the AI judge. Never raises and never blocks research.

    This read exists purely to give the tools-disabled coordinator prior failure context.
    Any filesystem, database or decoding failure degrades to 'no context available': the
    caller runs inside the checkpoint try/except that terminally abandons the industry,
    so an unreadable old audit file must not be able to reach it.
    """
    try:
        paths = _prior_checkpoint_paths(reports, day, industry, current)
    except Exception as error:
        return [{"purpose": "PRIOR_AUDIT_ONLY_NOT_DISCOVERY_SEEDS",
                 "diagnostic": "PRIOR_AUDIT_UNAVAILABLE", "error_type": type(error).__name__}]
    audit = []
    for path in paths:
        record = {"path": str(path), "purpose": "PRIOR_AUDIT_ONLY_NOT_DISCOVERY_SEEDS"}
        try:
            with path.open("rb") as stream:
                raw = stream.read(1048577)
            if len(raw) > 1048576:
                record["diagnostic"] = "PRIOR_CHECKPOINT_EXCEEDS_READ_LIMIT"
            else:
                record["sha256"] = hashlib.sha256(raw).hexdigest()
                try:
                    cached = json.loads(raw)
                    record.update(policy_hash=cached.get("policy_hash"), scope_id=cached.get("scope_id"),
                                  diagnostic=_diagnostic(cached.get("result", {})))
                    if isinstance(cached.get("plan_id"), str):
                        saved = database.execute("SELECT id,raw,observed_at FROM rw_results WHERE plan_id=? ORDER BY observed_at DESC,id LIMIT 1",
                                                 (cached["plan_id"],)).fetchone()
                        if saved:
                            result = json.loads(saved[1])
                            record["saved_research_result"] = {
                                "source": "rw_results:" + saved[0], "observed_at": saved[2],
                                "raw_sha256": hashlib.sha256(saved[1].encode()).hexdigest(),
                                "content_identity_valid": saved[0] == research_workflow.digest(
                                    [research_workflow.PROCESSOR_VERSION, cached["plan_id"], result]),
                                "purpose": "UNTRUSTED_HISTORICAL_DIAGNOSTIC_NOT_CURRENT_QUALIFICATION",
                                "diagnostic": _diagnostic(result)}
                except (ValueError, TypeError, AttributeError):
                    record["diagnostic"] = "PRIOR_CHECKPOINT_UNREADABLE"
        except Exception as error:
            record["diagnostic"] = "PRIOR_CHECKPOINT_UNREADABLE"
            record["error_type"] = type(error).__name__
        audit.append(record)
    return audit


def run(root, config, database, pipeline, observation, research_id, *, runtime, deadline=None):
    """Return aggregate COMPLETE when every industry completed, or when every industry that did
    not was abandoned and at least one completed (owner, 2026-09-28).

    An industry is complete whenever its research ran, even if no company had a fitting role;
    its unresolved companies are listed individually, so a day whose industries completed with
    unresolved companies is a successful day.

    Only run_research_worker is submitted to the executor. Plans, history/contact
    reservations, compute charges, processing, and queue ingestion stay here.
    Successful day/industry checkpoints avoid replaying completed qualifications;
    failed attempts and their raw outputs remain in the attempt reports/logs.
    """
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("Parallel research requires main-thread database ownership")
    root = Path(root)
    policy = user_profile.load()
    industry_discovery.inputs(root, policy, runtime.utcnow(), 0)
    industries = policy["industries"]
    policy_hash = hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()
    scope = scope_id(policy)
    config = dict(config, _research_scope_id=scope)
    day = _day(runtime.utcnow())
    # No fixed whole-run cap (owner, 2026-10-27; it was research_timeout_seconds, 2000 s). The run
    # starts with a discovery session plus a retry plus 5 minutes, and each qualification launch
    # extends it to cover that session, one retry and 5 minutes (extend_deadline). A caller's
    # `deadline` replaces only the starting value.
    deadline = time.monotonic() + initial_deadline_seconds() if deadline is None else deadline
    batch_limit = max(0, min(60, int(config.get("research_company_limit", 12))))
    compute_limit = min(21000, int(config.get("daily_compute_seconds", 10800)))
    reports = paths.reports(root)
    logs = paths.logs(root)
    reports.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    workflow_tasks.initialize(database)
    research_workflow.initialize(database)
    states = {}
    caches = {}
    charges = {}
    refundable = []
    pending = {}
    ready = []
    recovery_ready = []
    sessions = {}   # industry -> (monotonic launch, companies, sequence, timeout) of its qualification session
    started = queue.SimpleQueue()   # (token, limit, monotonic start) put by on_start from worker threads
    queued = set()                  # tokens of sessions submitted but not yet started (waiting for a slot)
    tokens = {}                     # future -> (industry, compute sequence)
    run_over = threading.Event()    # set when this run ends: a session still waiting for a slot stops waiting
    try:
        worker_options = {"on_start", "stop"} <= set(inspect.signature(runtime.run_research_worker).parameters)
    except (TypeError, ValueError):
        worker_options = False

    def session_done(industry, session, seconds):
        started, companies, sequence, _ = session
        record_session(database, research_id + ":" + _label(industry) + ":" + str(sequence), runtime.utcnow(),
                       industry, companies, seconds)
    wall = {"at": (dt.datetime.fromisoformat(runtime.utcnow())
                   + dt.timedelta(seconds=max(0, deadline - time.monotonic()))).isoformat()}

    def deadline_at():
        return wall["at"]

    def extend_deadline(industry, seconds, at=None, span=None):
        """At a qualification launch, and again when a session really starts (`at`, after its pacing
        slot; review R2): the run lasts at least until then + 2 x that session's limit + 5 minutes (one
        retry plus 5 min). Industries bound by this run's deadline move with it."""
        nonlocal deadline
        now = time.monotonic()
        wanted = (now if at is None else at) + (2 * seconds + RETRY_MARGIN_SECONDS if span is None else span)
        if wanted <= deadline:
            return
        previous = wall["at"]
        deadline = wanted
        wall["at"] = (dt.datetime.fromisoformat(runtime.utcnow()) + dt.timedelta(seconds=wanted - now)).isoformat()
        for name, cache in caches.items():
            if name == industry or cache.get("deadline_at") == previous:
                cache["deadline_at"] = wall["at"]
        states[industry]["deadline_extended_to"] = wall["at"]

    def note_dropped(industry, stage, entries):
        """Each entry left out on its own (owner, 2026-10-27): kept in the industry state for the
        Failures page, and one plain line in the research log for the Logs page."""
        for entry in entries:
            # `source` (the posting or record's link) is part of what the entry IS, so two different
            # postings with the same company, role and reason stay two entries; it is never shown.
            record = {"stage": stage, "company": entry.get("company"), "role": entry.get("role"),
                      "reason": entry.get("reason") or "it was malformed", "source": entry.get("source")}
            listed = states[industry].setdefault("dropped_entries", [])
            if record in listed:   # the same entry dropped again on a later try that day is listed once
                continue
            listed.append(record)
            name = (record["company"] or "An entry") + (", " + record["role"] if record["role"] else "")
            LOG.info("%s (%s research): skipped this entry because %s. The rest of the results were kept.",
                     name, industry, record["reason"])

    def persist(industry):
        """Record an industry's state to disk. A write failure is noted, never raised.

        Owner, 2026-09-23: "Fix 6. Should never exist. gracefully handle the file writing
        error." These two write_json calls run inside every fail/abandon/defer path, so a
        disk-full, permission or serialization error while merely RECORDING one industry's
        outcome used to escape into the scheduling loop and take the whole run down with it.
        The in-memory state is authoritative for the rest of this run either way; losing the
        on-disk copy costs a checkpoint, not the research.
        """
        state = states[industry]
        state["failures"] = caches[industry].get("failures", [])
        state["recovery_decisions"] = caches[industry].get("recovery_decisions", [])
        state["compute_waits"] = caches[industry].get("compute_waits", [])
        cache = caches[industry]
        cache["result"] = state.copy()
        for path, payload, label in ((reports / (research_id + "." + _label(industry) + ".industry.json"), state, "industry"),
                                     (checkpoint_path(root, day, industry, scope), cache, "checkpoint")):
            if label == "checkpoint" and cache.get("checkpoint_unusable"):
                continue
            try:
                runtime.write_json(path, payload)
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as error:
                state.setdefault("persist_errors", []).append(
                    {"target": label, "path": str(path), "error_class": type(error).__name__,
                     "diagnostic": str(error)[:300], "observed_at": runtime.utcnow()})
                if label == "checkpoint":
                    cache["checkpoint_unusable"] = True

    def abandon(industry, reason):
        states[industry].update(state="FAILED", recovery_state="ABANDONED", abandonment_reason=reason)
        caches[industry]["terminal"] = True
        persist(industry)

    def defer(industry, error, resume, refused=None, shown=None):
        """Record a compute WAIT: retryable, with a next retry, and no failed-attempt charge.

        `resume` says what the next attempt must do once allowance exists: RESEARCH
        continues the normal discovery/qualification flow, RECOVERY re-enters the bounded
        AI coordinator for a genuine failure that is already recorded in cache['failures'].
        Nothing is appended to 'failures' and no recovery decision is spent: a wait is not
        an investigation, so it cannot erode either bounded budget.
        """
        state = states[industry]
        cache = caches[industry]
        observed = runtime.utcnow()
        # The provider refusing the turn for usage is the same kind of WAIT, resuming at the
        # provider's stated reset instead of the next service-day start.
        limit = provider_limits.wait_of(error)
        wait = {"action_id": research_id + ":" + industry,
                "reason": provider_limits.CODE if limit else "DAILY_COMPUTE_ALLOWANCE_EXHAUSTED",
                "stage": getattr(error, "stage", None) or state.get("phase", "planning"),
                "error_class": type(error).__name__, "diagnostic": str(error), "observed_at": observed,
                "owner": "orchestrator", "resume": resume, "retryable": True,
                "next_retry_at": limit.next_at if limit else next_compute_reset(observed),
                "wait": len(cache.setdefault("compute_waits", [])) + 1}
        if limit:
            wait["provider"] = limit.detail()
        if refused is not None:
            wait["refused_recovery_decision"] = refused
        cache["compute_waits"].append(wait)
        state.update(state="COMPUTE_WAIT", compute_wait=wait, next_retry_at=wait["next_retry_at"],
                     error=shown or str(error), error_type=type(error).__name__)
        persist(industry)

    def fail(industry, error, shown=None):
        """`shown` (optional): the plain words the user sees for this failure; the raw error stays
        in the failure record's diagnostic and the log (review R1 S6)."""
        if isinstance(error, ComputeWait):
            defer(industry, error, "RESEARCH", shown=shown)
            return
        if provider_limits.wait_of(error):
            # The provider refused the turn before any work: return the measured unused tail of
            # the reservation and wait; nothing is recorded as a failed attempt.
            completed_cleanly(industry, provider_limits.CODE)
            defer(industry, error, "RESEARCH", shown=shown)
            return
        state = states[industry]
        cache = caches[industry]
        inability = state.pop("worker_inability", None)
        failure = {"action_id": research_id + ":" + industry, "stage": state.get("phase", "planning"),
                   "error_class": type(error).__name__, "diagnostic": str(error), "observed_at": runtime.utcnow(),
                   "owner": "orchestrator", "required_input": (inability or {}).get("required_input", ""), "next_retry": None,
                   "attempt": len(cache.setdefault("failures", [])) + 1,
                   "worker_inability": inability,
                   "report": state.get("report") if state.get("phase") == "qualification" else state.get("discovery_report"),
                   "log": state.get("log"), "plan_id": cache.get("plan_id"), "prior_result": _diagnostic(state)}
        cache["failures"].append(failure)
        state.update(state="RECOVERY_PENDING", error=shown or str(error), error_type=type(error).__name__)
        persist(industry)
        if len(cache.get("recovery_decisions", [])) >= 2:
            abandon(industry, "RECOVERY_DECISIONS_EXHAUSTED")
        elif industry not in recovery_ready:
            recovery_ready.append(industry)

    def remaining(industry=None):
        if runtime.RESEARCH_STOP.is_set():
            raise InterruptedError("Research interrupted by service shutdown")
        if _day(runtime.utcnow()) != day:
            raise RuntimeError("Service day changed; reserve a new daily run")
        seconds = deadline - time.monotonic()
        if industry and caches[industry].get("deadline_at"):
            seconds = min(seconds, (dt.datetime.fromisoformat(caches[industry]["deadline_at"])
                                   - dt.datetime.fromisoformat(runtime.utcnow())).total_seconds())
        if seconds <= 0:
            raise TimeoutError("Global research deadline exhausted")
        return seconds

    def reserve(industry, phase, maximum):
        seconds = min(remaining(industry), maximum)
        cache = caches[industry]
        sequence = cache.get("compute_sequence", 0) + 1
        cache["compute_sequence"] = sequence
        persist(industry)
        identity = "research:" + day + ":" + _label(industry) + ":scope:" + scope + ":" + str(sequence)
        started_at = runtime.utcnow()
        with database:
            reserved = workflow_tasks.reserve_compute(
                database, identity, started_at, math.ceil(seconds), TIMEZONE, compute_limit)
        if not reserved:
            raise ComputeWait("Daily existing-subscription compute allocation exhausted", phase)
        # Opens the measured window for a possible refund. The instant is stamped HERE, by the
        # orchestrator, one step before the operation starts; no worker, prompt or model output
        # can report its own usage. The small setup gap this includes can only shrink a refund.
        charges[industry] = {"action_id": identity, "started_at": started_at, "phase": phase}
        states[industry]["compute_reserved_seconds"] = states[industry].get("compute_reserved_seconds", 0) + math.ceil(seconds)
        persist(industry)
        return seconds

    def completed_cleanly(industry, reason):
        """Close this industry's open measured window; the run settles it at the end.

        Called ONLY where the operation finished with an unambiguous success (or the provider
        refused the turn before any work). A research failure, timeout, interruption,
        abandonment, compute wait or uncertain outcome never reaches here and keeps its whole
        charge: only the contact finder refunds a failed run's tail (owner, 2026-10-22, CM1 b),
        and committed browser sessions (2026-09-29).

        Settlement is deferred to settle_refunds() rather than applied here so that a
        reservation stays fully spent for the whole of the run that made it: research never
        recycles its own booking mid-run, and every existing mid-run admission boundary --
        including the compute waits at the reduced research ceiling -- behaves exactly as
        before. The stranded tail is returned to the shared pool for the remaining hours of
        the compute day, which is what it was doing nothing for.
        """
        charge = charges.pop(industry, None)
        if charge:
            refundable.append(dict(charge, industry=industry, completed_at=runtime.utcnow(), reason=reason))

    def settle_refunds():
        """Credit each cleanly completed reservation's unused tail back to the shared pool."""
        for charge in refundable:
            with database:
                credited = workflow_tasks.refund_compute(database, charge["action_id"], charge["started_at"],
                                                         charge["completed_at"], runtime.utcnow(), charge["reason"])
            if credited:
                industry = charge["industry"]
                states[industry]["compute_refunded_seconds"] = states[industry].get("compute_refunded_seconds", 0) + credited
                persist(industry)

    def submit(executor, industry, phase, command, prompt, output, companies=0):
        # A whole-run cap never bounds one worker (the old research_timeout_seconds); the
        # per-industry allowance in reserve() and the research compute ceiling still bound spend.
        # Session limits by formula (owner, 2026-10-27): discovery 300 s flat; qualification
        # 120 s + 1.5 x the install's slow end x its companies, set here from the actual count.
        maximum = DISCOVERY_SECONDS if phase == "discovery" else qualification_seconds(database, companies)
        if phase == "qualification":
            extend_deadline(industry, maximum)
        seconds = reserve(industry, phase, min(SESSION_CEILING_SECONDS, maximum))
        cache = caches[industry]
        directive = cache.pop("retry_directive", None)
        if directive:
            if phase == "discovery":
                # Discovery must stay seed-free deterministically, not by prompt text: only the
                # catalogued method name (a trusted research_recovery_methods key) crosses into a
                # discovery prompt. The coordinator's free text can quote prior-checkpoint prose,
                # which can name historical companies; that text reaches qualification only, where
                # the committed plan already bounds which companies may be researched.
                directive = {"method": directive.get("method", ""),
                             "instructions": "WITHHELD: DISCOVERY_REMAINS_SEED_FREE_USE_THE_METHOD_WITHIN_THE_ASSIGNED_INDUSTRY"}
            prompt = "ORCHESTRATOR RECOVERY DIRECTIVE (original scope and constraints still apply):\n" + json.dumps(directive) + "\n\n" + prompt
        log = logs / (research_id + "." + _label(industry) + "." + str(cache["compute_sequence"]) + "." + phase + ".log")
        if phase in ("discovery", "qualification"):
            # Save as it goes (R1, owner 2026-10-27): the session's own save tool and journal.
            limit = min(20, cache.get("discovery_limit") or 0) if phase == "discovery" else 0
            command = research_tools.attach(command, research_tools.prepare(output, phase, industry, limit, policy, config))
        states[industry].update(state="RUNNING", phase=phase, log=str(log))
        persist(industry)
        seconds = min(seconds, remaining(industry))
        token = (industry, cache["compute_sequence"])
        extra = {}
        if worker_options:
            # Under provider pacing a session may first wait for a slot (review R2, 2026-10-27): its own
            # clock and the run deadline count from when it really starts, and a wait still going when
            # the run ends stops there instead of starting a session whose result would be discarded.
            # A discovery's run time counts from its real start like the run's start did (discovery plus
            # a retry plus 5 minutes); a qualification's is its limit twice plus 5 minutes.
            span = initial_deadline_seconds() if phase == "discovery" else 2 * maximum + RETRY_MARGIN_SECONDS
            extra = {"on_start": lambda at, token=token, span=span: started.put((token, span, at)),
                     "stop": run_over.is_set}
            queued.add(token)
        future = executor.submit(runtime.run_research_worker, root, command, prompt, log, seconds, **extra)
        pending[future] = (industry, phase, output)
        tokens[future] = token
        if phase == "qualification":
            sessions[industry] = (time.monotonic(), companies, cache["compute_sequence"], seconds)

    def apply_starts():
        """Sessions that really started since the last look: extend the deadline from that start."""
        while True:
            try:
                (industry, sequence), span, at = started.get_nowait()
            except queue.Empty:
                return
            queued.discard((industry, sequence))
            extend_deadline(industry, None, at, span)
            session = sessions.get(industry)
            if session and session[2] == sequence:
                sessions[industry] = (at,) + session[1:]

    def waiting_for_slot():
        return any(tokens.get(future) in queued for future in pending)

    def discover(executor, industry):
        cache = caches[industry]
        output = reports / (research_id + "." + _label(industry) + "." + str(cache.get("compute_sequence", 0)) + ".discovery.json")
        states[industry]["discovery_report"] = str(output)
        prompt = industry_discovery.inputs(root, {**policy, "industries": [industry]}, runtime.utcnow(), cache["discovery_limit"])
        command = runtime.research_command(root, config, output, "discovery-schema.json")
        submit(executor, industry, "discovery", command, prompt, output)

    def recover(executor, industry):
        cache = caches[industry]
        callback = getattr(runtime, "research_recovery_decision", None)
        catalog = getattr(runtime, "research_recovery_methods", None)
        if not callback or not catalog:
            abandon(industry, "NO_SUPPORTED_RECOVERY_EXECUTOR")
            return
        try:
            methods = catalog(root, config)
            if (not isinstance(methods, dict) or not methods
                    or any(not isinstance(method, str) or not method.strip()
                           or not isinstance(description, str) or not description.strip()
                           for method, description in methods.items())):
                abandon(industry, "NO_SUPPORTED_ALTERNATIVE")
                return
            attempted = {method for failure in cache.get("failures", [])
                         for method in (failure.get("worker_inability") or {}).get("attempted_methods", [])}
            methods = {method: description for method, description in methods.items() if method not in attempted}
            if not methods:
                abandon(industry, "NO_SUPPORTED_ALTERNATIVE")
                return
            seconds = reserve(industry, "recovery", 30)
            history = cache.setdefault("recovery_decisions", [])
            artifacts = research_recovery.create_artifacts(root)
            packet = {"industry": industry, "day": day, "plan_id": cache.get("plan_id"),
                      "deadline_at": cache.get("deadline_at"), "failure": cache["failures"][-1],
                      "failures": list(cache["failures"]), "prior_decisions": list(history),
                      "supported_methods": methods, "shared_owners": cache.get("shared_owners", []),
                      "progress_evidence": cache.get("progress_evidence", {}),
                      "prior_checkpoints": cache.get("prior_checkpoints", []),
                      "artifacts": artifacts,
                      "decision_number": len(history) + 1}
            history.append({"state": "PENDING", "packet": packet, "artifacts": artifacts, "observed_at": runtime.utcnow()})
            states[industry]["state"] = "RECOVERING"
            persist(industry)
            pending[executor.submit(callback, root, config, packet, seconds)] = (industry, "recovery", None)
        except ComputeWait as error:
            # Bounded coordinator recovery itself needs compute. An empty shared allowance is a
            # wait, not "recovery is unavailable": abandonment is the AI's judgment (see the
            # ABANDON branch below and NO_SUPPORTED_ALTERNATIVE above), never a deterministic
            # reaction to an empty budget. The recorded failure stays; recovery resumes on retry.
            defer(industry, error, "RECOVERY")
        except Exception as error:
            abandon(industry, "RECOVERY_UNAVAILABLE: " + str(error))

    def qualify(executor, industry):
        remaining(industry)
        cache = caches[industry]
        state = states[industry]
        names = industry_discovery.validate(cache["discovery"], {**policy, "industries": [industry]},
                                            runtime.utcnow(), cache["discovery_limit"])
        if not names:
            raise RuntimeError("Discovery returned no companies; empty discovery is not completed research")
        own_names, shared = [], []
        for name in names:
            company = research_workflow.canonical(database, "company", name)
            owner = database.execute("SELECT plan_id FROM rw_companies WHERE day=? AND company=?", (day, company)).fetchone()
            if owner and owner[0] != cache.get("plan_id"):
                shared.append({"company": name, "company_key": company, "plan_id": owner[0]})
            else:
                own_names.append(name)
        cache["shared_owners"] = shared
        state["shared_owners"] = shared
        if not own_names:
            state.update(state="WAITING_SHARED_RESEARCH", phase="shared_research", companies=names)
            persist(industry)
            return
        # No day-wide refusal on another worker's recorded violation (owner, 2026-09-23: "Fix 3.
        # That should never exist. its a systemic blocker."). Any unscoped charge for the day used
        # to raise here for EVERY industry, so one worker's scope loss failed all 8 before they
        # planned. Violations are recorded against the offending result and no longer charged
        # (research_workflow.record_unplanned_attempt), so there is nothing here to cascade.
        if not cache.get("plan_id") and research_workflow.company_usage(database, day) >= 60:
            raise RuntimeError("Daily company research allowance exhausted before planning")
        output = reports / (research_id + "." + _label(industry) + "." + str(cache.get("compute_sequence", 0)) + ".qualification.json")
        remaining()
        # After a cut-off session the retry covers only the companies not yet done (owner, 2026-10-27, "3: a").
        done = {"done_companies": list(cache["saved_done"])} if cache.get("saved_done") else {}
        plan, plan_id, prompt, command = runtime.research_inputs(
            root, config, database, pipeline, observation, output, companies=own_names, industry=industry,
            discovery=cache["discovery"], **done)
        if not plan_id.endswith(":scope:" + scope):
            raise ValueError("RESEARCH_PRODUCER_SCOPED_PLAN_REQUIRED")
        saved_today = research_workflow.saved_companies(database, plan_id)   # an earlier run's cut-offs (S9)
        if saved_today:
            cache["saved_done"] = list(dict.fromkeys(list(cache.get("saved_done") or []) + saved_today))
        # "Industry plan exceeds its validated discovery scope" is DELETED (owner, 2026-09-23:
        # "Remove this ... Im sure this is a python comparison that happens and blocks"). It was:
        # the plan day (already enforced by get_plan's "Expired assignment"), an assignment-count
        # against discovery_limit (arithmetic that refused a reused plan of two companies when
        # today's allocation was one), and a company-membership test that asked, all or nothing,
        # the same per-company provenance question research_inputs now answers by dropping only
        # the assignments discovery cannot vouch for. Keeping it would have re-blocked exactly
        # what that fix unblocks. research_inputs owns this question; nothing re-asks it here.
        if not plan["assignments"]:
            raise RuntimeError("Discovery produced no available company assignments")
        cache["plan_id"] = plan_id
        # Carry what research_inputs already decided, so the dropped companies are not lost from
        # the durable state (owner asked, 2026-09-23). This is a plain record of a value computed
        # elsewhere: no comparison, no condition, nothing that can refuse the industry.
        state.update(plan_id=plan_id, report=str(output),
                     companies=[entry["company"] for entry in plan["assignments"]],
                     assignments_without_discovery_evidence=plan.get("assignments_without_discovery_evidence", []))
        if database.in_transaction:
            raise RuntimeError("Industry reservations must be committed before launching qualification")
        submit(executor, industry, "qualification", command, prompt, output, len(plan["assignments"]))

    def release_dead_shared_owners(industry):
        """Hand a waiting industry the companies its FAILED owners claimed. True if any moved.

        Owner, 2026-09-23: "5. Should never exist. Fix it." An industry parks in
        WAITING_SHARED_RESEARCH only because another industry's plan claimed the same company
        first. When that owner failed, the waiter was failed with SHARED_RESEARCH_OWNER_INCOMPLETE
        and then abandoned terminally, throwing away its own valid discovery for a fault that was
        never its own.

        Releasing the waiter's state is not enough: qualify() recomputes ownership from
        rw_companies, so while the dead owner's rows are still there the waiter parks again on the
        next pass and spins until the global deadline. The dead plan's claim on exactly those
        companies is dropped - it never researched them, and its rw_companies rows are also the
        day's budget count - so the waiter can plan them itself. An owner that DID complete keeps
        its claim, and a waiter with no releasable owner still fails rather than silently idling.
        """
        cache = caches[industry]
        shared_owners = cache.get("shared_owners", [])
        owners = ", ".join(entry["plan_id"] for entry in shared_owners)
        released = []
        with research_workflow.transaction(database):
            for entry in shared_owners:
                owner_state = next((item for item in states.values()
                                    if item.get("plan_id") == entry["plan_id"]), None)
                if owner_state is not None and owner_state.get("state") == "COMPLETE":
                    continue
                database.execute("DELETE FROM rw_companies WHERE day=? AND company=? AND plan_id=?",
                                 (day, entry["company_key"], entry["plan_id"]))
                database.execute("UPDATE rw_slots SET remaining=0 WHERE plan_id=? AND company=?",
                                 (entry["plan_id"], entry["company_key"]))
                released.append(entry["company_key"])
        if not released:
            return False
        cache.pop("shared_owners", None)
        state = states[industry]
        state.pop("shared_owners", None)
        state["shared_owner_released"] = {"owners": owners, "companies": released}
        state["phase"] = "qualification"
        state.update(state="PENDING", worker_status=None)
        persist(industry)
        return True

    def reconcile_shared():
        waiting = [industry for industry in industries if states[industry]["state"] == "WAITING_SHARED_RESEARCH"]
        while waiting:
            resolved = []
            for industry in waiting:
                owners = [owner for shared in caches[industry]["shared_owners"]
                          for owner in states.values() if owner.get("plan_id") == shared["plan_id"]
                          and owner["state"] == "COMPLETE" and owner.get("completed_at")]
                if len(owners) != len(caches[industry]["shared_owners"]):
                    continue
                state = states[industry]
                state.update(state="COMPLETE", worker_status="COMPLETE", shared_completed_at=runtime.utcnow(),
                             summary=state.get("summary", "Shared companies researched by their owning industries."))
                state.setdefault("completed_at", state["shared_completed_at"])
                persist(industry)
                resolved.append(industry)
            if not resolved:
                break
            waiting = [industry for industry in waiting if industry not in resolved]
        for industry in waiting:
            if release_dead_shared_owners(industry):
                ready.append(industry)
            else:
                owners = ", ".join(entry["plan_id"] for entry in caches[industry].get("shared_owners", []))
                fail(industry, RuntimeError("SHARED_RESEARCH_OWNER_INCOMPLETE: owner failed or unavailable: " + owners))

    for industry in industries:
        label = _label(industry)
        cache_path = checkpoint_path(root, day, industry, scope)
        states[industry] = {"industry": industry, "day": day, "state": "PENDING", "research_id": research_id,
                            "scope_id": scope, "checkpoint": str(cache_path)}
        caches[industry] = {"day": day, "industry": industry, "policy_hash": policy_hash,
                            "scope_id": scope, "research_policy": research_policy(policy),
                            "deadline_at": deadline_at()}
        reservation_prefix = "research:" + day + ":" + label + ":scope:" + scope + ":"
        prior_reservations = [row[0] for row in database.execute(
            # The prefix carries the research day; the row's `day` column is the budget day
            # (workflow_tasks.budget_day), a different label since 2026-09-30.
            "SELECT action_id FROM worker_compute_reservations WHERE action_id LIKE ?",
            (reservation_prefix + "%",))]
        try:
            if not cache_path.exists():
                if prior_reservations:
                    raise ValueError("Research checkpoint missing for existing durable reservations")
                caches[industry]["prior_checkpoints"] = _prior_checkpoints(database, reports, day, industry, cache_path)
                continue
            cached = json.loads(cache_path.read_text())
            if (not isinstance(cached, dict) or not isinstance(cached.get("result", {}), dict)
                    or any(not isinstance(cached.get(field, []), list)
                           or any(not isinstance(entry, dict) for entry in cached.get(field, []))
                           for field in ("failures", "recovery_decisions", "compute_waits"))):
                raise ValueError("Research checkpoint structure invalid")
            if prior_reservations:
                sequence = cached.get("compute_sequence")
                if type(sequence) is not int or sequence < max(int(identity[len(reservation_prefix):]) for identity in prior_reservations):
                    raise ValueError("Research checkpoint reservation sequence missing or regressed")
            saved_deadline = dt.datetime.fromisoformat(cached.get("deadline_at", ""))
            if saved_deadline.utcoffset() is None:
                raise ValueError("Research checkpoint deadline missing or invalid")
            if ((cached.get("day"), cached.get("industry"), cached.get("scope_id")) != (day, industry, scope)
                    or cached.get("research_policy") != research_policy(policy)):
                raise ValueError("Research checkpoint scope evidence mismatches its identity")
            reopen = cached.get("reopen")
            if cached.get("terminal") and isinstance(reopen, dict) and reopen.get("state") == "AUTHORIZED":
                # Owner-authorized reopen of a terminally abandoned industry (AGENTS.md: never
                # reopen automatically). The checkpoint is NOT deleted - the guard above refuses
                # a missing checkpoint while this day's reservations exist - so the industry
                # restarts from a fresh cache that keeps what the guards need: the same scope
                # identity and the compute_sequence, so new reservations continue after this
                # morning's rather than colliding with them. Everything the abandoned attempt
                # recorded moves, unchanged, into reopened_history. The marker is consumed here,
                # so a restart re-reads the new checkpoint instead of reopening again.
                if not isinstance(reopen.get("authorization"), str) or not reopen["authorization"].strip():
                    raise ValueError("Industry reopen requires an explicit owner authorization")
                archived = {key: value for key, value in cached.items()
                            if key not in {"reopen", "reopened_history"}}
                caches[industry] = {**caches[industry],
                                    "compute_sequence": cached.get("compute_sequence", 0),
                                    "prior_checkpoints": cached.get("prior_checkpoints", []),
                                    "reopened_history": cached.get("reopened_history", []) + [
                                        {"reopened_at": runtime.utcnow(), "research_id": research_id,
                                         "authorization": reopen["authorization"], "checkpoint": archived}],
                                    "reopen": {**reopen, "state": "CONSUMED", "research_id": research_id,
                                               "consumed_at": runtime.utcnow()}}
                # Release the ABANDONED plan's claim on this day's companies (owner, 2026-09-23).
                # A reopen restarts the industry from a fresh cache with no plan_id, so it
                # rediscovers the same companies - and plan_research then refuses every one of
                # them as ALREADY_ASSIGNED to the plan that was just abandoned, leaving the new
                # plan empty and failing with "Research assignment lacks discovery evidence".
                # The industry was blocking its own reopen. The abandoned plan never researched
                # these companies (that is why it was abandoned) and its rw_companies rows are
                # also the day's budget count, so the claim goes with the attempt it belonged to.
                # Same rule as release_dead_shared_owners, applied to an industry's own corpse.
                # EVERY abandoned plan this industry has had today, not just the current one. A
                # reopen resets the cache, so `cached["plan_id"]` is already gone on the SECOND
                # reopen while attempt 1's plan still holds the claim - it survives only inside
                # reopened_history. Checking the current plan alone released nothing and the
                # industry stayed blocked by its own corpse (observed 2026-09-23, attempt 6:
                # every industry reported released=[] and failed on discovery evidence again).
                stale = {cached.get("plan_id")}
                for entry in cached.get("reopened_history", []):
                    if isinstance(entry, dict) and isinstance(entry.get("checkpoint"), dict):
                        stale.add(entry["checkpoint"].get("plan_id"))
                # The PLAN goes with its claims. Releasing the companies but keeping the rw_plans
                # row left an inconsistent state that was worse than either: `plan_id` is
                # deterministic per day+industry+scope, so research_inputs reused a plan whose
                # assignments named companies that were no longer claimed AND no longer in the
                # fresh discovery - zero overlap, so even the per-company filter had nothing to
                # keep and the industry failed on "lacks discovery evidence" again (attempt 8,
                # five industries). Dropping the row lets plan_research build a fresh plan from
                # what discovery actually returned. A plan that reserved real CONTACTS is kept:
                # those are people, not claims, and must not be orphaned.
                released, dropped_plans = [], []
                if any(stale):
                    with research_workflow.transaction(database):
                        for plan_id in sorted(identifier for identifier in stale if identifier):
                            contacts = database.execute(
                                "SELECT COUNT(*) FROM rw_contacts WHERE plan_id=?", (plan_id,)).fetchone()[0]
                            for row in database.execute(
                                    "SELECT company FROM rw_companies WHERE day=? AND plan_id=?",
                                    (day, plan_id)).fetchall():
                                released.append(row[0])
                            database.execute("DELETE FROM rw_companies WHERE day=? AND plan_id=?",
                                             (day, plan_id))
                            if contacts:
                                # Contacts are people, not claims: the plan stays, so its slots
                                # stay with it and are merely emptied.
                                database.execute("UPDATE rw_slots SET remaining=0 WHERE plan_id=?", (plan_id,))
                            else:
                                # The slot rows go WITH the plan. Zeroing them while deleting the
                                # plan and its companies left a third table still keyed on the
                                # dead plan, so plan_research's fresh INSERT collided:
                                # "UNIQUE constraint failed: rw_slots.plan_id, rw_slots.company"
                                # (attempt 9, Healthcare and Telecom). Release all three together.
                                database.execute("DELETE FROM rw_slots WHERE plan_id=?", (plan_id,))
                                database.execute("DELETE FROM rw_saved_companies WHERE plan_id=?", (plan_id,))   # review T5
                                database.execute("DELETE FROM rw_plans WHERE id=?", (plan_id,))
                                dropped_plans.append(plan_id)
                states[industry]["reopened"] = True
                states[industry]["reopened_companies_released"] = released
                states[industry]["reopened_plans_dropped"] = dropped_plans
                persist(industry)
                continue
            caches[industry] = cached
            prior = cached.get("result", {})
            if cached.get("terminal"):
                states[industry] = {**prior, "state": "FAILED", "recovery_state": "ABANDONED", "reused": True, "research_id": research_id}
                persist(industry)
                continue
            decisions = cached.get("recovery_decisions", [])
            if any(entry.get("state") == "PENDING" for entry in decisions):
                states[industry].update(prior)
                abandon(industry, "INTERRUPTED_RECOVERY_DECISION_NOT_REPLAYED")
                continue
            waiting = prior.get("compute_wait") if prior.get("state") == "COMPUTE_WAIT" else None
            if waiting:
                # A recorded compute wait resumes; it is neither an interrupted attempt nor a
                # failed investigation, so it must not enter the "no blind replay" branch below.
                # The per-run wall-clock deadline is this run's, exactly as for a fresh industry.
                caches[industry]["deadline_at"] = deadline_at()
                states[industry]["resumed_compute_wait"] = waiting
                if waiting.get("resume") == "RECOVERY" and cached.get("failures"):
                    states[industry].update({key: value for key, value in prior.items()
                                             if key not in {"state", "compute_wait", "next_retry_at"}},
                                            research_id=research_id, state="RECOVERY_PENDING")
                    persist(industry)
                    if len(decisions) >= 2:
                        abandon(industry, "RECOVERY_DECISIONS_EXHAUSTED")
                    elif industry not in recovery_ready:
                        recovery_ready.append(industry)
                    continue
            if prior.get("state") in {"RUNNING", "RECOVERING", "RECOVERY_PENDING", "FAILED", "INTERRUPTED"}:
                states[industry].update(prior, research_id=research_id)
                fail(industry, RuntimeError("Prior attempt interrupted or failed; no blind replay"))
                continue
            if "discovery" not in cached:
                continue
            names = industry_discovery.validate(cached["discovery"], {**policy, "industries": [industry]},
                                                runtime.utcnow(), cached["discovery_limit"])
            if cached["discovery"]["discovery_status"] != "COMPLETE" and not names:
                raise ValueError("Only successful discovery can be reused")
            prior = cached.get("result", {})
            if cached.get("plan_id"):
                plan = research_workflow.get_plan(database, cached["plan_id"], now=runtime.utcnow())
                if not plan:
                    raise ValueError("Industry checkpoint has no persisted plan")
            # industry_status (D-2) is authoritative; checkpoints written before it existed
            # carry only worker_status/research_status, which meant the same thing then.
            own_complete = (prior.get("industry_status") or prior.get("worker_status")) == "COMPLETE" and prior.get("result_id")
            if own_complete:
                row = database.execute("SELECT observed_at,processed FROM rw_results WHERE id=? AND plan_id=?",
                                       (prior.get("result_id"), cached.get("plan_id"))).fetchone()
                saved = json.loads(row[1]) if row else {}
                if (not names or not prior.get("completed_at") or not row or _day(row[0]) != day
                        or saved.get("industry_status", saved.get("research_status")) != "COMPLETE"):
                    raise ValueError("Completed industry checkpoint lacks a valid same-day result")
                states[industry] = {**prior, "reused": True, "research_id": research_id}
                states[industry]["state"] = "WAITING_SHARED_RESEARCH" if cached.get("shared_owners") else "COMPLETE"
                states[industry].pop("error", None)
                states[industry].pop("error_type", None)
                persist(industry)
            elif prior.get("state") == "COMPLETE" and not cached.get("shared_owners"):
                raise ValueError("Completed industry checkpoint lacks a valid same-day result")
            elif prior.get("state") == "COMPLETE":
                states[industry] = {**prior, "state": "WAITING_SHARED_RESEARCH", "reused": True,
                                    "research_id": research_id}
            else:
                states[industry]["discovery_reused"] = True
                states[industry]["discovery_report"] = prior.get("discovery_report")
        except Exception as error:
            caches[industry].update(checkpoint_unusable=True, deadline_at=None)
            states[industry]["checkpoint_reconciliation"] = {
                "checkpoint": str(cache_path), "prior_compute_reservations": prior_reservations,
                "deadline": "UNKNOWN_DO_NOT_RENEW"}
            fail(industry, error)
            abandon(industry, "CHECKPOINT_RECONCILIATION_REQUIRED")

    # The day's skipped entries carry across its attempts (review R6, 2026-10-27): a later try that
    # finishes the day still lists what an earlier try skipped, each entry once.
    for industry in industries:
        saved = caches[industry].get("result")
        earlier = saved.get("dropped_entries") if isinstance(saved, dict) else None
        listed = states[industry].setdefault("dropped_entries", [])
        for entry in earlier if isinstance(earlier, list) else []:
            if isinstance(entry, dict) and entry not in listed:
                listed.append(entry)
        if not listed:
            states[industry].pop("dropped_entries")
        # The same for the day's cut-off sessions (review T3): each listed once across tries.
        earlier = saved.get("session_cut_offs") if isinstance(saved, dict) else None
        cut_offs = states[industry].setdefault("session_cut_offs", [])
        for entry in earlier if isinstance(earlier, list) else []:
            if isinstance(entry, dict) and not any(same_cut_off(entry, listed) for listed in cut_offs):
                cut_offs.append(entry)
        if not cut_offs:
            states[industry].pop("session_cut_offs")

    allocated = sum(cache.get("discovery_limit", 0) for cache in caches.values() if "discovery" in cache)
    fresh = [industry for industry in industries
             if states[industry]["state"] == "PENDING" and "discovery" not in caches[industry]]
    allowance = max(0, min(batch_limit - allocated, 60 - research_workflow.company_usage(database, day)))
    share, extra = divmod(allowance, len(fresh)) if fresh else (0, 0)
    for position, industry in enumerate(fresh):
        caches[industry]["discovery_limit"] = min(20, share + (position < extra))

    def saved_entries(output, phase):
        """What the session saved as it went (R1): one entry per identity, read after it ended."""
        return research_tools.read_journal(research_tools.journal_path(output), phase) if output else []

    def same_cut_off(one, other):
        """Two cut-off notes are one when they came from the same session (review U1); a note without a
        session (older) falls back to the whole entry."""
        if one.get("session") or other.get("session"):
            return one.get("session") == other.get("session")
        return one == other

    def note_cut_off(industry, stage, note, output):
        # `session` says which session it was (never shown): two cut-offs with the same words stay two.
        entry = {"stage": stage, "note": note, "session": Path(output).name if output else None}
        listed = states[industry].setdefault("session_cut_offs", [])
        if not any(same_cut_off(entry, other) for other in listed):
            listed.append(entry)

    def accept_discovery(industry, result, cut_off=None, note=None, output=None):
        """`cut_off`: the error that ended the session early, when it did (R1). A provider wait (429)
        is still waited out before qualification launches (review R1 S3)."""
        cache, state = caches[industry], states[industry]
        dropped = []
        result = industry_discovery.screen(result, {**policy, "industries": [industry]},
                                           runtime.utcnow(), cache["discovery_limit"], dropped)
        note_dropped(industry, "discovery", dropped)
        persist(industry)
        # A PARTIAL discovery with at least one valid company goes on to
        # qualification with those companies (owner, 2026-10-27: Airia and
        # WitnessAI were discarded when CB Insights returned 403); its blockers are
        # kept as a noted problem. A FAILED or empty one is retried as before.
        if not result["companies"]:
            raise RuntimeError("Industry discovery incomplete; raw output retained for bounded retry")
        if result["discovery_status"] != "COMPLETE":
            state["discovery_blockers"] = list(result["blockers"])
        cache["discovery"] = result
        if cut_off is None:   # a cut-off session keeps its whole charge, like any session that did not finish
            completed_cleanly(industry, "RESEARCH_DISCOVERY_COMPLETE")
        else:
            note_cut_off(industry, "discovery", note, output)
        persist(industry)
        if cut_off is not None and provider_limits.wait_of(cut_off):
            fail(industry, cut_off, shown=note)   # the short provider wait; it resumes at qualification
            return
        ready.append(industry)

    def accept_research(industry, result, output, session, finished_at, final_used, cut_off=None, note=None):
        cache, state = caches[industry], states[industry]
        done = list(cache.get("saved_done") or [])
        processed = research_workflow.process_research(database, result, plan_id=cache["plan_id"],
                                                       policy=policy, now=runtime.utcnow(),
                                                       **({"done": done} if done else {}))
        cache.setdefault("progress_evidence", {})["company_coverage"] = processed.get("company_coverage", {})
        processed_path = output.with_suffix(".processed.json")
        runtime.write_json(processed_path, processed)
        note_dropped(industry, "qualification", processed.get("dropped_entries", []))
        final_used.append(True)
        stored = runtime.store_research(database, pipeline, processed)
        rejected = [entry for entry in stored if not entry.get("dropped")]
        note_dropped(industry, "saving", [entry for entry in stored if entry.get("dropped")])
        state["completed_at"] = runtime.utcnow()
        coverage = processed.get("company_coverage", {})
        if cut_off is not None:
            # A cut-off session is a PARTIAL result that keeps what it saved (owner, 2026-10-27, "3: a");
            # the companies it covered are done, and only the rest go to the retry.
            cache["saved_done"] = list(dict.fromkeys(done + list(coverage.get("covered", []))))
            # Kept for the day, so a later run of the same plan skips them too (review R1 S9).
            research_workflow.record_saved_companies(database, cache["plan_id"], coverage.get("covered", []),
                                                     runtime.utcnow())
            note_cut_off(industry, "qualification", note, output)
        left = cut_off is not None and bool(coverage.get("uncovered"))
        if not left:
            research_workflow.release_unused_slots(database, cache["plan_id"], completed_read_only=True)
        # The industry completes whenever its research ran (owner, 2026-09-28);
        # unresolved companies are recorded individually with their diagnostics
        # instead of failing the whole industry.
        industry_status = processed.get("industry_status", "FAILED")
        if session and industry_status == "COMPLETE" and cut_off is None:
            # Only a session that actually checked its companies is a timing sample; a
            # fast empty exit would drag the slow end down (review R1, 2026-10-27).
            session_done(industry, session, finished_at - session[0])
        resolution = processed.get("company_resolution", {})
        state.update(state="COMPLETE" if industry_status == "COMPLETE" else "FAILED",
                     industry_status=industry_status,
                     worker_status=processed["research_status"], summary=processed["summary"],
                     result_id=processed["result_id"], processed_report=str(processed_path),
                     rejected_drafts=rejected,
                     blockers=["DISCOVERY_PARTIAL: " + blocker for blocker in (cache["discovery"].get("blockers") or [])]
                     + processed["blockers"],
                     # Companies an earlier cut-off session of this plan already finished stay resolved.
                     resolved_companies=list(dict.fromkeys(
                         (cache.get("saved_done") or []) + [entry["company"] for entry in resolution.get("resolved", [])])),
                     unresolved_companies=resolution.get("unresolved", []),
                     rejected_opportunities=processed.get("rejected_opportunities", []))
        if state["state"] == "COMPLETE" and cache.get("shared_owners"):
            state["state"] = "WAITING_SHARED_RESEARCH"
        if industry_status == "COMPLETE" and not left:
            state.pop("error", None)
            state.pop("error_type", None)
            if cut_off is None:
                completed_cleanly(industry, "RESEARCH_QUALIFICATION_COMPLETE")
        persist(industry)
        if left:
            fail(industry, cut_off, shown=note)
        elif industry_status != "COMPLETE":
            fail(industry, RuntimeError("Research incomplete: nothing was researched or the result is invalid: "
                                        + "; ".join(processed["blockers"])))

    def take_final(industry, phase, output, session, finished_at, final_used):
        """A finished session's final output, merged with what it saved (R1), through the normal path."""
        result = json.loads(output.read_text())
        cache = caches[industry]
        state = states[industry]
        state["worker_inability"] = research_recovery.worker_inability(result.get("inability"))
        cache["progress_evidence"] = {"summary": result.get("summary"), "blockers": result.get("blockers"),
                                      "status": result.get("discovery_status", result.get("research_status"))}
        if phase == "discovery":
            result = {key: value for key, value in result.items() if key != "inability"}
            # The final output wins; a saved company is added only when it has no valid copy there (R1, S4).
            result = research_tools.merge_discovery(result, saved_entries(output, phase), cache["discovery_limit"],
                                                    industries=[industry], now=runtime.utcnow())
            accept_discovery(industry, result)
            return
        if (not isinstance(result, dict) or not isinstance(result.get("summary"), str)
                or not result["summary"].strip()
                or any(not isinstance(result.get(field), list) for field in ("opportunities", "drafts", "blockers"))
                or result.get("research_status") not in {"COMPLETE", "PARTIAL", "FAILED"}):
            raise ValueError("Research output is missing its substantive result envelope")
        # The final output wins; saved roles are added only for companies it does not cover (R1, S4).
        result = research_tools.merge_research(result, saved_entries(output, phase), now=runtime.utcnow(), policy=policy)
        accept_research(industry, result, output, session, finished_at, final_used)

    def salvage(industry, phase, output, session, error):
        """A session that ended without a usable final answer: keep what it saved (R1). True when it
        saved something and that was taken in; False leaves the failure to fail() exactly as before."""
        if phase not in ("discovery", "qualification"):
            return False
        saved = saved_entries(output, phase)
        if not saved:
            return False
        cache, state = caches[industry], states[industry]
        note = research_tools.cut_off_note(error, saved, phase, answered=bool(output) and Path(output).is_file())
        cache["progress_evidence"] = {"summary": note, "blockers": [note], "status": "PARTIAL"}
        if phase == "discovery":
            try:
                accept_discovery(industry, research_tools.merge_discovery(
                    None, saved, cache["discovery_limit"], note), cut_off=error, note=note, output=output)
            except (ValueError, RuntimeError):
                return False
            return True
        state.pop("worker_inability", None)
        try:
            accept_research(industry, research_tools.merge_research(None, saved, note), output, session, None,
                            [], cut_off=error, note=note)
        except Exception as problem:   # the session's own failure is then recorded by fail() as before
            LOG.warning("%s research: could not keep the roles its cut-off session saved (%s: %s)",
                        industry, type(problem).__name__, problem)
            return False
        return True

    # Every industry runs at once (owner, 2026-10-27: "Remove the parallel cap"; it was 8).
    executor = ThreadPoolExecutor(max_workers=max(1, len(industries)), thread_name_prefix="industry-research")
    try:
        for industry in industries:
            if runtime.RESEARCH_STOP.is_set():
                break
            if states[industry]["state"] != "PENDING":
                continue
            try:
                cache = caches[industry]
                limit = cache["discovery_limit"]
                states[industry]["discovery_limit"] = limit
                if limit <= 0:
                    raise RuntimeError("No company allowance allocated to this industry")
                if "discovery" in cache:
                    ready.append(industry)
                else:
                    discover(executor, industry)
            except Exception as error:
                fail(industry, error)

        while pending or ready or recovery_ready or any(
                state["state"] == "WAITING_SHARED_RESEARCH" for state in states.values()):
            apply_starts()
            # A session still waiting for its pacing slot is not out of time: its clock has not started.
            if runtime.RESEARCH_STOP.is_set() or time.monotonic() >= deadline and not waiting_for_slot():
                break
            finished, _ = wait(pending, timeout=0 if ready or recovery_ready else min(1, max(0.05, deadline - time.monotonic())),
                               return_when=FIRST_COMPLETED)
            finished.update(future for future in pending if future.done())
            apply_starts()   # a session that started and finished since the last look extends first
            for future in sorted(finished, key=lambda item: pending[item][1] != "qualification"):
                industry, phase, output = pending.pop(future)
                session = sessions.pop(industry, None) if phase == "qualification" else None
                final_used = []   # set once the final output reaches the store: no salvage after that
                try:
                    try:
                        returned = future.result()
                    except subprocess.TimeoutExpired:
                        # Cut off at its limit: recorded as having taken at least that limit, so the
                        # slow end can grow back after a run of fast sessions (review R1, 2026-10-27).
                        if session:
                            session_done(industry, session, max(time.monotonic() - session[0], session[3]))
                        raise
                    finished_at = time.monotonic()
                    remaining(industry)
                    if phase == "recovery":
                        cache = caches[industry]
                        entry = cache["recovery_decisions"][-1]
                        entry.update(state="DECIDED", decision=returned)
                        persist(industry)
                        directive = research_recovery.validate_decision(returned, entry["packet"]["supported_methods"], cache["recovery_decisions"][:-1])
                        completed_cleanly(industry, "RESEARCH_RECOVERY_DECISION_COMPLETE")
                        if (directive["action"] == "ABANDON"
                                or len(cache["recovery_decisions"]) > 1 and not directive["progress"]):
                            abandon(industry, directive["reason"])
                        elif states[industry].get("phase") == "shared_research":
                            # Same rule as reconcile_shared (owner, 2026-09-23: "5. Should never
                            # exist"). This abandoned the waiter TERMINALLY, which was the second
                            # way an industry died for its owner's fault. Try the handover first.
                            if release_dead_shared_owners(industry):
                                ready.append(industry)
                            else:
                                abandon(industry, "SHARED_OWNER_RECONCILIATION_REQUIRED")
                        else:
                            cache["retry_directive"] = directive
                            persist(industry)
                            if "discovery" in cache:
                                ready.append(industry)
                            else:
                                discover(executor, industry)
                        continue
                    take_final(industry, phase, output, session, finished_at, final_used)
                except Exception as error:
                    if phase == "recovery" and provider_limits.wait_of(error):
                        # No decision was made: the pending entry is not a spent recovery
                        # decision. It is kept with the wait, and the coordinator runs again
                        # for the same recorded failure after the provider's reset.
                        refused = caches[industry]["recovery_decisions"].pop()
                        completed_cleanly(industry, provider_limits.CODE)
                        defer(industry, error, "RECOVERY", dict(refused, state="PROVIDER_USAGE_LIMIT_WAIT"))
                    elif phase == "recovery":
                        entry = caches[industry]["recovery_decisions"][-1]
                        entry.update(state="FAILED", error_type=type(error).__name__, error=str(error))
                        abandon(industry, "RECOVERY_DECISION_FAILED")
                    elif not final_used and salvage(industry, phase, output, session, error):
                        pass   # the saved entries were kept (R1); salvage queued any retry
                    else:
                        fail(industry, error)
            if ready:
                industry = ready.pop(0)
                try:
                    qualify(executor, industry)
                except Exception as error:
                    fail(industry, error)
            while recovery_ready:
                industry = recovery_ready.pop(0)
                if states[industry]["state"] == "RECOVERY_PENDING":
                    recover(executor, industry)
            if not pending and not ready and not recovery_ready:
                reconcile_shared()
    except (KeyboardInterrupt, SystemExit):
        # Only a genuine interruption stops every industry (owner, 2026-09-23: "Fix 6. Should
        # never exist. gracefully handle the file writing error."). This caught BaseException,
        # so ANY error in the scheduling loop - including a disk-full or permission failure in
        # the write_json that merely RECORDS one industry's outcome - set the SIGTERM channel,
        # interrupted all eight industries and reported the run as INTERRUPTED, indistinguishable
        # from the owner stopping it. An ordinary exception now propagates as the failure it is.
        runtime.RESEARCH_STOP.set()
        raise
    finally:
        run_over.set()
        for future in pending:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)

    # Sessions still going when the run ended were stopped with it; what they saved is kept (R1).
    for future, (industry, phase, output) in list(pending.items()):
        if phase in ("discovery", "qualification") and future.done() and not future.cancelled():
            error = future.exception()
            if error is None:
                # It finished but was not taken in before the run ended: its final output goes through
                # the normal path; only when that is unusable are its saves kept as a cut-off (review R1 S8).
                used = []
                try:
                    take_final(industry, phase, output, sessions.get(industry), time.monotonic(), used)
                    continue
                except Exception as problem:
                    if used:
                        continue
                    error = problem
            salvage(industry, phase, output, sessions.get(industry), error)

    for industry in industries:
        if states[industry]["state"] in {"PENDING", "RUNNING", "RECOVERING", "RECOVERY_PENDING"}:
            if runtime.RESEARCH_STOP.is_set():
                states[industry].update(state="INTERRUPTED", error="Research interrupted by service shutdown")
                persist(industry)
            else:
                fail(industry, TimeoutError("Global research deadline exhausted"))
                abandon(industry, "RESEARCH_DEADLINE_EXHAUSTED")

    for industry in [name for name in industries if states[name]["state"] == "WAITING_SHARED_RESEARCH"]:
        if runtime.RESEARCH_STOP.is_set():
            states[industry].update(state="INTERRUPTED", error="Shared research interrupted before owner completion")
            persist(industry)
        else:
            fail(industry, TimeoutError("Global research deadline exhausted before shared-owner review"))
            abandon(industry, "RESEARCH_DEADLINE_EXHAUSTED")

    settle_refunds()
    completed = sum(state["state"] == "COMPLETE" for state in states.values())
    unresolved = [industry for industry in industries if states[industry]["state"] != "COMPLETE"]
    waiting = [industry for industry in industries if states[industry]["state"] == "COMPUTE_WAIT"]
    terminal = bool(unresolved) and all(caches[industry].get("terminal") for industry in unresolved)
    # A PURE compute wait: every unresolved industry is waiting on the shared allowance and none
    # was abandoned. A mixed run still charges the day a genuine failed attempt.
    compute_wait = bool(waiting) and not terminal and set(waiting) == set(unresolved)
    # Retry later the SAME day once refunds free the allowance (owner, 2026-09-25: "yup ... do
    # it"). A wait was always parked until the next local midnight, because reservations were
    # once never released inside a compute day. Refunds now return each finished industry's
    # unused tail at settle_refunds() above, so the allowance a waiting industry was refused
    # mid-run can be free a minute later: on 2026-09-25 Healthcare was refused at 07:07 while
    # 7,135 of the 11,520 reserved seconds came back when the run ended, and it sat until the
    # next day. Only a wait on OUR allowance moves; a provider limit keeps its stated reset.
    # The retry is taken only when a phase reservation fits right now, measured with the same
    # arithmetic reserve_compute admits against, so it cannot turn into a same-day wait loop.
    # The size of the session each waiting industry resumes with (the limits submit() reserves).
    def resume_seconds(industry):
        try:
            return qualification_seconds(database, len(industry_discovery.validate(
                caches[industry]["discovery"], {**policy, "industries": [industry]}, runtime.utcnow(),
                caches[industry].get("discovery_limit", 20))))
        except (KeyError, TypeError, ValueError):
            return DISCOVERY_SECONDS
    phase_seconds = max([resume_seconds(industry) for industry in waiting] or [DISCOVERY_SECONDS])
    if (compute_wait and all(states[industry]["compute_wait"]["reason"] == "DAILY_COMPUTE_ALLOWANCE_EXHAUSTED"
                             for industry in waiting)
            and compute_limit - workflow_tasks.compute_used(database, workflow_tasks.budget_day(runtime.utcnow(), TIMEZONE)) >= phase_seconds):
        soon = (dt.datetime.fromisoformat(runtime.utcnow()) + dt.timedelta(minutes=5)).isoformat()
        for industry in waiting:
            states[industry]["compute_wait"]["next_retry_at"] = soon
            states[industry]["next_retry_at"] = soon
            persist(industry)
    # D-2: companies left unresolved inside COMPLETE industries, listed per industry.
    open_companies = {industry: states[industry]["unresolved_companies"] for industry in industries
                      if states[industry]["state"] == "COMPLETE" and states[industry].get("unresolved_companies")}
    # One industry that could not finish no longer fails the day (owner, 2026-09-28: "please
    # remove this rule entirely. This is a successful research."): once every unfinished
    # industry has been abandoned, the day is COMPLETE if any industry completed. A day in which
    # nothing completed is still FAILED, and a pure compute wait is still PARTIAL.
    final = {"state": "COMPLETE" if completed == len(industries) or (terminal and completed)
             else "PARTIAL" if compute_wait else "FAILED",
             "research_id": research_id,
             "scope_id": scope, "policy_hash": policy_hash,
             "compute_reserved_seconds": sum(state.get("compute_reserved_seconds", 0) for state in states.values()),
             "compute_refunded_seconds": sum(state.get("compute_refunded_seconds", 0) for state in states.values()),
             "retryable": bool(unresolved) and not terminal,
             "compute_wait": compute_wait,
             "next_retry_at": min((states[industry]["compute_wait"]["next_retry_at"] for industry in waiting),
                                  key=dt.datetime.fromisoformat, default=None) if compute_wait else None,
             "compute_waiting_industries": waiting,
             "stop_reason": ("ALL_UNRESOLVED_INDUSTRIES_ABANDONED" if terminal else
                             "COMPUTE_WAIT_ALL_UNRESOLVED_INDUSTRIES" if compute_wait else ""),
             "abandoned_industries": [industry for industry in industries if caches[industry].get("terminal")],
             "day": day, "industries": list(states.values()), "research_company_limit_total": batch_limit,
             "company_usage": research_workflow.company_usage(database, day),
             "unresolved_companies": open_companies,
             "summary": (f"{completed}/{len(industries)} industries completed; results advanced independently."
                         + (f" {sum(map(len, open_companies.values()))} companies in completed industries remain"
                            " unresolved and are listed individually." if open_companies else "")
                         + (f" {len(waiting)} waiting on the shared daily compute allowance; not a failed"
                            " investigation." if waiting else "")),
             "report": str(reports / (research_id + ".parallel.json"))}
    if runtime.RESEARCH_STOP.is_set():
        final["state"] = "INTERRUPTED"
    runtime.write_json(Path(final["report"]), final)
    return final
