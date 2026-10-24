import datetime as dt
import json
import uuid

import applications
import browser_runtime
import desktop_adapter
import provider_limits
import reply_resume
import run_locks
import workflow_tasks


# At most this many AI coordinator admissions per job per scope (see `coordinator_admissions`).
# Reaching it is an orchestrator-side limit, not an AI judgment: application_recovery parks
# the job on an orchestrator hold instead of counting it as a coordinator failure.
COORDINATOR_ADMISSION_CAP = 5
COORDINATOR_CAP_CODE = "APPLICATION_COORDINATOR_ATTEMPTS_EXHAUSTED"
# What one browser operation books against the daily allowance (equal to
# application_browser.MAX_SECONDS), and how many a submission books up front
# (`prepare_submission`: the submit session and its verify).
SECONDS_PER_OPERATION = 900
SUBMIT_OPERATIONS = 2


# A complete read (assessed ELIGIBLE and saved for reuse, `applications.process`) is recorded
# with this event; such a read never counts toward the three-inspect cap (owner, 2026-10-02).
# Only inconclusive reads count.
COMPLETE_READ_EVENT = applications.COMPLETE_READ_EVENT


# Chrome was held by another session (the contact finder) past `browser_wait_seconds`.
BROWSER_BUSY_CODE = "APPLICATION_BROWSER_BUSY"
# Capacity waits of Sidedoor's own browser (E7.T1): no try spent, no request, no reservation.
BROWSER_UNAVAILABLE_CODES = ("APPLICATION_BROWSER_RUNTIME_MISSING", "APPLICATION_BROWSER_PROFILE_IN_USE")
BROWSER_BUSY_RETRY_SECONDS = 300


class CapacityWait(applications.AdapterPending):
    def __init__(self, code, next_at):
        super().__init__(code, next_at)
        self.code = code
        self.next_action = "Resume the same guarded application when browser capacity is available; no submission retry is authorized"


# PermissionWait, release_restored_permission_hold and reconcile_inherited_denials are deleted
# (owner, 2026-09-30: no remnants of permission denials): a browser refusal is an ordinary
# failed or blocked read on the ordinary retry and recovery path.


def moment(value):
    """A timezone-aware datetime for an ISO timestamp, or None."""
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def inspect_scope_start(database, job_key, now):
    """Start of the job's current inspect scope: the latest of the newest owner reopen grant
    for the job (grants never expire, owner 2026-09-28), its last recovery supersession, and the current inspect
    contract revision. Structural (timestamps from durable records) only."""
    import application_recovery
    starts = [moment(applications.READ_CONTRACT_REVISION_AT)]
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if {"application_reopen_grants", "application_reopen_grant_jobs"} <= tables:
        current = moment(now)
        for (reopened_at,) in database.execute("""SELECT consumed.reopened_at
                FROM application_reopen_grant_jobs AS consumed JOIN application_reopen_grants AS granted
                ON granted.authorization_id=consumed.authorization_id WHERE consumed.job_key=?""", (job_key,)):
            reopened = moment(reopened_at)
            if reopened and current and reopened <= current:
                starts.append(reopened)
    if "application_events" in tables:
        for (at,) in database.execute("SELECT at FROM application_events WHERE job_key=? AND event=?",
                                      (job_key, application_recovery.SUPERSEDED_EVENT)):
            starts.append(moment(at))
        # A per-job, owner-authorized scope reset (owner, 2026-09-22). The cap asks "would
        # another read tell us anything new?" and answers it from the job and the contract
        # timestamp - it cannot see that the READING CODE changed. Nineteen patches landed on
        # 2026-09-21/22 (the answer key twice, the question schema, what the read must judge
        # and emit); abstergo resolved 7 of 10 required answers under the old code and
        # resolves 10 of 10 under this one, with the same input. Bumping
        # READ_CONTRACT_REVISION_AT is the global form of this and grants every job a fresh
        # read at once; this is the same statement scoped to one job the owner names. The
        # other two per-job resets could not be used honestly: a reopen grant requires a
        # fresh terminal decision, and SUPERSEDED asserts the blocker is no longer raised,
        # which is untrue for a job whose questions really are unanswered.
        for (at,) in database.execute("SELECT at FROM application_events WHERE job_key=? AND event=?",
                                      (job_key, "APPLICATION_INSPECT_SCOPE_RESET")):
            starts.append(moment(at))
    starts = [value for value in starts if value is not None]
    return max(starts) if starts else None


def result_source(result):
    """The `source` a stored read result carries (it names the session's trace hash), or None."""
    try:
        value = json.loads(result) if isinstance(result, str) else result
    except ValueError:
        return None
    source = value.get("source") if isinstance(value, dict) else None
    return source if isinstance(source, str) and source else None


def complete_read_sources(database, job_key):
    """Sources of this job's reads recorded complete (COMPLETE_READ_EVENT)."""
    sources = set()
    for (detail,) in database.execute("SELECT detail FROM application_events WHERE job_key=? AND event=?",
                                      (job_key, COMPLETE_READ_EVENT)):
        try:
            source = json.loads(detail).get("source")
        except (TypeError, ValueError, AttributeError):
            continue
        if isinstance(source, str) and source:
            sources.add(source)
    return sources


def in_scope(created_at, since):
    created = moment(created_at)
    # An unparseable timestamp is counted: the cap errs towards fewer reads.
    return since is None or created is None or created >= since


class HostedDesktopAdapter(desktop_adapter.DesktopAdapter):
    # How long a session waits for Chrome while a contact-finder read holds it (owner,
    # 2026-10-02: the two "would need to take turns on it"); then a capacity wait, no try spent.
    browser_wait_seconds = run_locks.APPLICATION_BROWSER_WAIT_SECONDS

    def __init__(self, database, root, config, clock=applications.stamp, consumer=None):
        super().__init__(database, root, config, clock)
        import application_browser
        self.consumer = consumer or application_browser.consume
        settings = config.get("application_browser", {})
        # Owner, 2026-09-19: process every queued job each cycle, best effort; null means no
        # per-cycle cap (the daily compute budget still bounds the work).
        self.limit = settings.get("max_operations", 4)
        if self.limit is not None and (type(self.limit) is not int or self.limit < 1):
            raise ValueError("APPLICATION_BROWSER_OPERATION_LIMIT_INVALID")
        self.allocated = 0
        self.prepaid = {}
        workflow_tasks.initialize(database)

    def provider_gate(self, now):
        """Refuse new admissions while a recorded provider usage-limit wait is fresh.

        Each waiting item already carries the provider's reset as its next_at; this only stops
        new work from re-probing a refusing provider more than once per bounded backoff, so a
        restored subscription is noticed within the hour. Nothing is reserved or counted.
        """
        latest = self.database.execute("SELECT detail FROM application_events WHERE event=? ORDER BY id DESC LIMIT 1",
                                       (provider_limits.EVENT,)).fetchone()
        try:
            detail = json.loads(latest[0]) if latest else None
            resume = dt.datetime.fromisoformat(detail["resume_at"])
            probe = dt.datetime.fromisoformat(detail["observed_at"]) + dt.timedelta(seconds=provider_limits.BACKOFF_SECONDS)
        except (TypeError, KeyError, ValueError):
            return
        if dt.datetime.fromisoformat(now) < min(resume, probe):
            raise desktop_adapter.ProviderUsageWait("provider-usage-limit", detail["resume_at"])

    def provider_waited(self, job_key, detail, now, **extra):
        applications.event(self.database, job_key, provider_limits.EVENT, dict(detail, **extra), now)

    # 240 -> 480 with `application_browser.MAX_SECONDS` (owner, 2026-09-22). These two must
    # stay equal: this is what charges the daily allowance, and a session ceiling above it
    # would let a read run longer than the compute it booked.
    def reserve(self, operations, job_key, *, seconds_per_operation=SECONDS_PER_OPERATION, purpose="browser"):
        now = self.clock()
        self.provider_gate(now)
        if self.limit is not None and self.allocated + operations > self.limit:
            raise CapacityWait("APPLICATION_BROWSER_CYCLE_CAPACITY", desktop_adapter.later(now, 60))
        reservation = "application-browser:" + str(uuid.uuid4())
        with self.database:
            reserved = workflow_tasks.reserve_application_compute(self.database, reservation, job_key,
                now, operations * seconds_per_operation, self.config.get("timezone", "America/Los_Angeles"),
                daily_seconds=self.config.get("daily_compute_seconds", 10800))
            if reserved:
                # Remembered so the unused tail can be credited back once the operations this
                # booking covers have committed (`workflow_tasks.settle_browser_reservations`).
                # Only the browser purpose is tracked: the 30-second orchestrator-recovery
                # reservation is already smaller than any session it covers.
                if purpose == "browser":
                    workflow_tasks.track_browser_reservation(self.database, reservation, job_key,
                        operations, operations * seconds_per_operation, now)
                applications.event(self.database, job_key, "BROWSER_CAPACITY_RESERVED",
                    {"reservation": reservation, "operations": operations, "seconds": operations * seconds_per_operation,
                     "purpose": purpose,
                     "restart_policy": "Retain charge; never reuse a prior process's admission"}, now)
        if not reserved:
            reset = workflow_tasks.next_day_start(now, self.config.get("timezone", "America/Los_Angeles"),
                                                  workflow_tasks.day_start_hour(self.config))
            raise CapacityWait("APPLICATION_BROWSER_SHARED_BUDGET_EXHAUSTED", reset.astimezone(dt.timezone.utc).isoformat())
        self.allocated += operations

    def coordinator_admissions(self, job_key, now=None):
        """Coordinator admissions counted against the cap in the job's CURRENT scope.

        Same scope as the inspect cap (`inspect_scope_start`: newest unexpired owner reopen
        grant, last recovery supersession, current contract revision): admissions spent in
        an earlier lifecycle do not starve an owner-authorized reopen of every decision.
        Nothing is erased; the ledger keeps every admission.
        """
        import application_recovery
        since = inspect_scope_start(self.database, job_key, now or self.clock())
        admissions = sum(in_scope(row[0], since) for row in self.database.execute("""SELECT at FROM application_events
            WHERE job_key=? AND event='BROWSER_CAPACITY_RESERVED'
            AND json_extract(detail,'$.purpose')='orchestrator_recovery'""", (job_key,)))
        # Each coordinator call the provider refused for usage used one admission and made no
        # decision; it is not counted (recorded waits, plus failures recorded before the
        # refusal was classified whose saved executor log shows it).
        refused = sum(in_scope(row[0], since) for row in self.database.execute("""SELECT at FROM application_events
            WHERE job_key=? AND event=? AND json_extract(detail,'$.purpose')='orchestrator_recovery'""",
            (job_key, provider_limits.EVENT)))
        refused += sum(in_scope(row[0], since) and provider_limits.decision_limited(json.loads(row[1]))
                       for row in self.database.execute(
            "SELECT at,detail FROM application_events WHERE job_key=? AND event IN (?,?)",
            (job_key, "APPLICATION_RECOVERY_DECISION_FAILED", application_recovery.HOLD_REVIEW_FAILED_EVENT)))
        return admissions - refused

    def coordinator_cap_reached(self, job_key, now=None):
        return self.coordinator_admissions(job_key, now) >= COORDINATOR_ADMISSION_CAP

    def recovery_decision(self, packet):
        import application_recovery_decision
        job_key = packet["job"]["job_key"]
        if self.coordinator_cap_reached(job_key):
            raise RuntimeError(COORDINATOR_CAP_CODE)
        self.reserve(1, job_key, seconds_per_operation=30, purpose="orchestrator_recovery")
        try:
            # The coordinator sees the whole raw résumé text saved at upload (owner, 2026-10-21, E4.T2).
            try:
                resume = reply_resume.context(self.root)
            except ValueError as error:
                resume = {"state": "UNAVAILABLE", "reason": str(error)}
            return application_recovery_decision.decide(self.root, self.config, dict(packet, candidate_resume=resume),
                                                        timeout=30)
        except Exception as error:
            limit = provider_limits.wait_of(error)
            if not limit:
                raise
            with self.database:
                self.provider_waited(job_key, limit.detail(), self.clock(), purpose="orchestrator_recovery",
                                     artifacts=getattr(error, "artifacts", None))
            raise desktop_adapter.ProviderUsageWait("coordinator:" + job_key, limit.next_at) from error

    def prepare_submission(self, job):
        if self.prepaid.get(job["job_key"], 0) < SUBMIT_OPERATIONS:
            self.reserve(SUBMIT_OPERATIONS, job["job_key"])
            self.prepaid[job["job_key"]] = SUBMIT_OPERATIONS

    def call(self, operation, payload):
        # The browser permission check before each request (item 2) is deleted (owner,
        # 2026-09-27); the browser platform still enforces its own permissions.
        try:
            body = self.request_body(operation, payload)
        except ValueError as error:
            return {"blocked": {"code": str(error), "next_action": "Verify official ATS destination and canonical requisition"}}
        if self.cached_available(operation, payload):
            return super().call(operation, payload)
        job_key = payload["job"]["job_key"]
        if operation == "inspect":
            # Keyed on the posting being read (job key, stored payload and destination URL),
            # not on the policy hash: an profile.json edit does not change the page
            # and must not buy three more reads of it. The policy still binds where it matters,
            # at the submit guard (`desktop_adapter.submission_gate` refuses POLICY_CHANGED).
            # The cap is per scope: claims made before the current scope started (an owner
            # reopen grant, a recovery supersession, or the current inspect contract) were
            # spent under a lifecycle or contract that no longer applies. Within a scope the
            # cap stays three; nothing is erased or un-counted in the ledger.
            since = inspect_scope_start(self.database, job_key, self.clock())
            # A read the provider refused for usage was never a read and spends no claim. A
            # complete read (saved for reuse when it was assessed ELIGIBLE) spends none either
            # (owner, 2026-10-02): the cap bounds repeated inconclusive reads, and a good read
            # that a compute wait kept from its submit is not one (Cyberdyne, 2026-10-02).
            complete = complete_read_sources(self.database, job_key)
            reviewed = sum(row[1] for row in self.database.execute("""SELECT created_at,claims,result FROM desktop_requests
                WHERE job_key=? AND operation='inspect'
                AND json_extract(payload,'$.job.payload')=? AND json_extract(payload,'$.url')=?""",
                (job_key, body["job"]["payload"], body["url"]))
                if in_scope(row[0], since) and not provider_limits.stored_result_limited(row[2])
                and result_source(row[2]) not in complete)
            if reviewed >= 3:
                return {"blocked": {"code": "APPLICATION_BROWSER_INVESTIGATION_EXHAUSTED",
                    "next_action": "Orchestrator must review the saved browser findings and provide new verified evidence or an alternative official route; no repeated unchanged investigation"}}
        # One Chrome session at a time (state/browser.lock, owner 2026-10-02). Taken before any
        # compute is reserved or any request is created, and held until the session's result is
        # read, so a busy browser costs the job nothing: it waits as a capacity wait.
        with run_locks.browser_session(self.root, wait_seconds=self.browser_wait_seconds) as free:
            if not free:
                raise CapacityWait(BROWSER_BUSY_CODE, desktop_adapter.later(self.clock(), BROWSER_BUSY_RETRY_SECONDS))
            # Sidedoor's browser runtime missing, or a Chrome already on its profile (E7.T1 review fix
            # 2): a capacity wait like a busy browser, before any reservation or request.
            code = browser_runtime.check_available(self.database, self.config, "APPLICATION_BROWSER_", "applications")
            if code:
                raise CapacityWait(code, desktop_adapter.later(self.clock(), BROWSER_BUSY_RETRY_SECONDS))
            import setup_steps
            missing = setup_steps.codex_wait(self.config)
            if missing:
                # No usable Codex (2026-10-23): before any reservation or request, the job waits like a
                # provider wait (no try spent, nothing abandoned) and resumes after the backoff.
                raise desktop_adapter.ProviderUsageWait("codex:" + job_key, missing.next_at)
            if self.prepaid.get(job_key, 0):
                self.prepaid[job_key] -= 1
            else:
                self.reserve(1, job_key)
            try:
                return super().call(operation, payload)
            except applications.AdapterPending as pending:
                request = self.database.execute("SELECT operation,state,job_key,attempt_id FROM desktop_requests WHERE request_id=?",
                                                (pending.request_id,)).fetchone()
                if (not request or request["state"] != "PENDING" or request["operation"] != operation
                        or request["job_key"] != job_key or request["attempt_id"] != body["attempt_id"]):
                    raise
                self.consumer(self.root, self.database, self.config, pending.request_id)
                self.provider_refusal(pending.request_id, job_key)
                return super().call(operation, payload)

    def provider_refusal(self, request_id, job_key):
        """Record and raise the wait when the provider refused the consumed read for usage."""
        request = self.database.execute("SELECT state,result FROM desktop_requests WHERE request_id=?", (request_id,)).fetchone()
        resume = provider_limits.stored_wait(request["result"]) if request and request["state"] == "DONE" else None
        if not resume:
            return
        now = self.clock()
        detail = json.loads(request["result"])["error"]
        with self.database:
            self.database.execute("UPDATE desktop_requests SET state='CONSUMED' WHERE request_id=? AND state='DONE'", (request_id,))
            desktop_adapter.record(self.database, request_id, provider_limits.EVENT, now,
                                   {"resume_at": resume, "counts_as_read": False, "counts_as_failure": False})
            self.provider_waited(job_key, {key: detail.get(key) for key in (
                "code", "resume_at", "stated_reset", "observed_at", "provider_message", "counts_as_attempt", "counts_as_failure")},
                now, request_id=request_id, purpose="browser")
        raise desktop_adapter.ProviderUsageWait(request_id, resume)
