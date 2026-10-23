"""What the Failures and logs page shows (E6.T7; E6.T0 spec section 9). Display only, like Overview
(overview_page) and Applications (applications_page): every failure here is a record the product
already wrote, read through `overview_page.snapshot` (no -wal/-shm, nothing written), and the logs are
bounded, redacted tails of the files the scheduler already writes. Nothing here judges, writes, or
calls the network or a model.

Only real failures show: something was tried and did not work, and Sidedoor will try again (and
when), gave up, or needs the user. Waits that clear on their own (the shared compute budget, a
provider's usage limit, the browser being busy) and holds that are checks in progress are not
failures and are left out. Each failure says what happens next or what to do.

The window is the last 14 days (owner, D8), at most 50 items a section. One real failure is one item
(F1): a job child's non-zero exit, a failed hourly round and a crashed step of that round are one
failure, shown as the most specific of them (carrying the job's error log).

Sources (why each is a real failure):
- workflow tasks (`workflow_tasks`): a task counts only for its real failed tries in the window,
  counted from its own events (F8, F12): RETRY_WAIT (`finish`: a try failed, the next is booked at
  `next_at`), BLOCKED (with WORKER_EXCEPTION only, which `orchestrator.worker_step` records when a
  step raised; other BLOCKED tasks are holds), UNCERTAIN (a write whose lease expired mid-run,
  `claim`) and the EXHAUSTED that `finish` records on a last failed try. Waits are their own events
  (COMPUTE_WAIT, PROVIDER_USAGE_LIMIT_WAIT, CAPACITY_DEFERRED) or a RETRY_WAIT whose blocker is a
  budget/capacity code, and never count. EXHAUSTED with no failed try at all ran out of time
  (waiting for compute, F11). UNCERTAIN is never reconciled by `workflow_tasks`, so it says so (F6).
- the scheduler (`overview_page.loop_state`, the loop object's `last_error` and `job_failures`,
  read as Overview reads them): a job child that exited non-zero, and a problem keeping the
  schedule. The hourly cycle's own record (`runs`, state FAILED or INTERRUPTED) is a cycle that
  raised or whose process died.
- research (`research_days` RETRY_WAIT or EXHAUSTED, with the attempt's `research` row): a day whose
  attempt failed or was interrupted (`daily_schedule.finish`). A RETRY_WAIT whose attempt reported a
  pure compute wait (`compute_wait` true) is a wait and is left out; a day that later COMPLETEd is
  not listed.
- applications (`application_jobs`, F4): the Applications page's own classification. Owner holds
  (`overview_page._blocked`, the Overview needs-you count, minus jobs with a question on Approvals)
  are Needs you; a BLOCKED/RETRY_WAIT job not on route OWNER or RECONCILE (`applications.job_route`)
  is a failure only when its blocker is one of applications.py's failure sets
  (`application_failure_codes`); every other hold is a wait or a check in progress. Given-up jobs
  live under Applications' "Given up" only (owner, D5).
- outreach writes (`outreach_actions`, `outreach_attempts`): INVITE_RETRY_WAIT (an invitation that
  did not land, `sequence`), WRITE_RETRY_WAIT/WRITE_FAILED (campaign preparation, `outreach_campaign`),
  CONFIRMED_NOT_SENT (three tries, not sent), UNCERTAIN_NO_RETRY (a send whose outcome is unknown; a
  REPLY is re-checked and resent by `reply_execution.retry_reply` while tries remain, F2),
  LAUNCH_RETRY_WAIT/LAUNCH_FAILED (campaign launches, `outreach`).
- notifications (`notification_outbox`, `notifications._dispatch`): FAILED, UNCERTAIN (reconciled at
  most 5 times, then said to have stopped, F7), and PENDING rows carrying an error.

Codes never show: each failure has plain words, and an unknown worker or code gets generic ones.
Indexes these reads use are created by the writers' schema setup (workflow_tasks.initialize,
applications.initialize, outreach.initialize, orchestrator.connect).
"""

import datetime as dt
import os
import re
import stat
import urllib.parse
from pathlib import Path
from zoneinfo import ZoneInfo

import applications_page
import overview_page as overview
import workflow_tasks
import paths


RECENT_DAYS = 14   # owner, D8
LIST_LIMIT = 50
UNAVAILABLE = overview.UNAVAILABLE
GROUPS = (("research", "Research"), ("outreach", "Outreach"), ("applications", "Applications"),
          ("connections", "Connections"), ("other", "Other work"))
SUPPORT_EMAIL = "support@yoursidedoor.com"

# --- Workflow tasks ---------------------------------------------------------------------------------

WAIT_EVENTS = {"COMPUTE_WAIT", "PROVIDER_USAGE_LIMIT_WAIT", "CAPACITY_DEFERRED"}
WAIT_CODES = {"PROVIDER_USAGE_LIMIT_WAIT", "DAILY_COMPUTE_BUDGET_EXHAUSTED", "DAILY_COMPUTE_ALLOWANCE_EXHAUSTED",
              "OUTREACH_AI_SHARED_BUDGET_EXHAUSTED", "FOLLOWUP_SHARED_CAPACITY_EXHAUSTED",
              "APPLICATION_BROWSER_SHARED_BUDGET_EXHAUSTED", "APPLICATION_BROWSER_CYCLE_CAPACITY",
              "APPLICATION_BROWSER_BUSY", "APPLICATION_BROWSER_RUNTIME_MISSING", "APPLICATION_BROWSER_PROFILE_IN_USE",
              "LINKEDIN_CONTACTS_BROWSER_RUNTIME_MISSING", "LINKEDIN_CONTACTS_BROWSER_PROFILE_IN_USE",
              "COMPUTE_BUDGET_EXHAUSTED", "ANSWERING_COMPUTE_BUDGET_EXHAUSTED",
              "COMPUTE_BUDGET_WAIT", "BUDGET_WAIT", "DEFERRED_COMPUTE", "WAITING_COMPUTE", "READ_BUDGET_EXHAUSTED"}

# Worker -> (group, what it was doing). The hourly round's steps (`orchestrator.worker_step`) run
# again on the next round; every other worker retries on its own task.
WORKERS = {
    "provider-observation": ("outreach", "Checking LinkedIn for new activity"),
    "reply-monitor": ("outreach", "Reading your LinkedIn inbox"),
    "guarded-outreach": ("outreach", "Sending invitations"),
    "guarded-message-actions": ("outreach", "Sending messages"),
    "guarded-new-contact-execution": ("outreach", "Contacting new people"),
    "guarded-contextual-replies": ("outreach", "Sending replies"),
    "guarded-due-followups": ("outreach", "Sending follow-ups"),
    "reply-classification": ("outreach", "Reading replies"),
    "engagement_review": ("outreach", "Reviewing a reply"),
    "pipeline_routing_review": ("outreach", "Reviewing a reply"),
    "message_writer": ("outreach", "Writing a follow-up message"),
    "outreach-draft-inspection": ("outreach", "Checking a note before it is sent"),
    "outreach-draft-composition": ("outreach", "Writing an invitation note"),
    "research-draft-promotion": ("outreach", "Preparing notes for people research found"),
    "linkedin-contact-discovery": ("outreach", "Finding people to contact"),
    "inactivity-closure": ("outreach", "Closing quiet conversations"),
    "reply-referral-conversion": ("outreach", "Recording referrals"),
    "referral-interview-conversion": ("outreach", "Recording referrals and interviews"),
    "conversion": ("outreach", "Recording referrals and interviews"),
    "conversion-handoff": ("outreach", "Preparing a referral summary"),
    "applications": ("applications", "Working on applications"),
    "agent-investigation": ("applications", "Looking into an application problem"),
    "posting-judge": ("applications", "Checking for duplicate postings"),
    "decision-bindings": ("applications", "Applying your answers"),
    "decision-inbox": ("applications", "Reading your answers"),
    "notifications": ("connections", "Sending notifications"),
    "notification-dispatch": ("connections", "Sending notifications"),
    "daily-report": ("connections", "Building the daily report"),
}
WORKER_PREFIXES = {"application-public-evidence:": ("applications", "Looking up a posting")}
HOURLY_STEPS = {"provider-observation", "reply-monitor", "guarded-outreach", "guarded-message-actions",
                "guarded-new-contact-execution", "guarded-contextual-replies", "guarded-due-followups",
                "reply-classification", "research-draft-promotion", "linkedin-contact-discovery", "inactivity-closure",
                "reply-referral-conversion", "referral-interview-conversion", "conversion",
                "applications", "posting-judge", "decision-bindings", "decision-inbox", "notifications",
                "notification-dispatch", "daily-report", "engagement", "outreach", "observation"}

# --- Applications -----------------------------------------------------------------------------------

# Blockers that mean an attempt failed, from applications.py's own sets (F4): the capability
# blockers a failed session records (access blocks excepted: owner D6, not failures until the third
# closes the job), the upload failures and the incomplete reads, plus the stops a session reports
# when it could not finish. Everything else held BLOCKED/RETRY_WAIT is a wait or a check in progress
# (the Applications page's "Waiting to retry"), and route OWNER/RECONCILE is decided by
# `applications.job_route`, as the Applications page decides it.
EXTRA_FAILURE_CODES = {"SUBMISSION_REPORTED_FAILED", "CONDITIONAL_QUESTION_UNANSWERED", "FIELD_LIMIT_TOO_SHORT_FOR_FACT", "PREFLIGHT_TRANSIENT",
                       "MALFORMED_OR_UNAVAILABLE_EVIDENCE", "APPLICATION_BROWSER_INVESTIGATION_EXHAUSTED", "ANSWERING_RETRY"}


def application_failure_codes():
    import applications
    return ((set(applications.CAPABILITY_BLOCKERS) - set(applications.ACCESS_BLOCK_CODES))
            | set(applications.UPLOAD_FAILURE_CODES) | set(applications.INCOMPLETE_INSPECTION) | EXTRA_FAILURE_CODES)


def is_application_failure(code, state):
    """True when a held job's blocker is a failed attempt (not a wait, a check or an owner hold)."""
    if state not in ("BLOCKED", "RETRY_WAIT") or applications_page._route(code, state) in ("OWNER", "RECONCILE"):
        return False
    return _code(code) in application_failure_codes()


# --- Outreach writes and notifications --------------------------------------------------------------

ACTION_RETRYING = ("INVITE_RETRY_WAIT", "WRITE_RETRY_WAIT")
ACTION_FINAL = ("CONFIRMED_NOT_SENT", "WRITE_FAILED", "UNCERTAIN_NO_RETRY")
PURPOSE_WORDS = {"INVITE": ("invitation", "invitations"), "INITIAL": ("invitation", "invitations"),
                 "PREPARE_CAMPAIGN": ("invitation", "invitations"), "FIRST_MESSAGE": ("first message", "first messages"),
                 "FOLLOWUP": ("follow-up", "follow-ups"), "REPLY": ("reply", "replies")}
MESSAGE_WORDS = ("message", "messages")


def _plural(count, words):
    return "%d %s" % (count, words[0] if count == 1 else words[1])


def _item(group, text, todo, at=None, count=1, retry_at=None, you=False, fix=None, detail=None, log=None, href=None,
          job=None, moments=(), slots=()):
    """One failure. `todo` always says what happens next or what to do; `fix` is {"label", "href"}.
    `job` names the scheduler job ("run", "research", "applications") whose exit this failure
    explains, and `moments` when each such failure happened (F1: one item per real failure)."""
    return {"group": group, "text": text, "todo": todo, "at": at, "count": max(1, int(count or 1)), "retry_at": retry_at,
            "you": you, "fix": fix, "detail": detail, "log": log or [], "href": href, "job": job,
            "moments": [moment for moment in moments if moment is not None] or ([at] if job and at else []),
            "slots": list(slots)}


def _at(fmt, moment):
    """"at 10:30" for a clock time, "tomorrow 09:00" or "Wed 10:02" as they are."""
    text = fmt(moment)
    return "at " + text if text[:1].isdigit() else text


def _since(now):
    return now - dt.timedelta(days=RECENT_DAYS)


def _recent(rows, since, column):
    return [row for row in rows if (moment := overview._utc(row[column])) is not None and moment >= since]


def _code(value):
    return applications_page.code_root(value)


def worker_words(worker):
    worker = str(worker or "")
    if worker in WORKERS:
        return WORKERS[worker]
    for prefix, found in WORKER_PREFIXES.items():
        if worker.startswith(prefix):
            return found
    return ("other", "A background task")


TRY_EVENTS = ("RETRY_WAIT", "BLOCKED", "UNCERTAIN", "EXHAUSTED")


def _counts(reader, row, since):
    """(failed in the window, failed ever, waits ever) from the task's own events (F8/F12).
    `workflow_tasks.finish` records the blocker with a RETRY_WAIT/EXHAUSTED event: one with a
    budget/capacity blocker is a wait, not a try. claim's own EXHAUSTED ({}) ended no try. An older
    event without the blocker counts as a failure, except the latest one when the task's current
    blocker is a wait (it is that wait)."""
    events = reader.rows("SELECT id,at,event,detail FROM workflow_task_events WHERE task_id=? ORDER BY id",
                         (row["task_id"],), "workflow_task_events")
    failed = ever = waited = 0
    for index, event in enumerate(events):
        kind = event["event"]
        if kind in WAIT_EVENTS:
            waited += 1
            continue
        if kind not in TRY_EVENTS:
            continue
        detail = overview._json(event["detail"])
        if kind == "EXHAUSTED" and not (detail and "result" in detail):
            continue
        if kind in ("RETRY_WAIT", "EXHAUSTED"):
            if detail is not None and "blocker" in detail:
                blocker = detail["blocker"]
            else:   # recorded before finish kept the blocker
                blocker = row["blocker"] if index == len(events) - 1 else None
            if _code(blocker) in WAIT_CODES:
                waited += 1
                continue
        ever += 1
        moment = overview._utc(event["at"])
        if moment is not None and moment >= since:
            failed += 1
    return failed, ever, waited


def task_failures(reader, now, fmt):
    """Tasks with a real failed try in the window (F8/F12: counted from the failure events
    themselves, never from waits), one item per worker and kind."""
    since = _since(now)
    rows = reader.rows("""SELECT t.task_id, t.worker, t.status, t.blocker, t.next_at, t.updated_at, t.effect, t.parent_run_id
        FROM workflow_tasks t
        WHERE t.status IN ('RETRY_WAIT','EXHAUSTED','UNCERTAIN','BLOCKED') AND t.updated_at>=?
          AND (t.status!='BLOCKED' OR t.blocker='WORKER_EXCEPTION')""",
                       (overview._prefilter(since),), "workflow_tasks", "workflow_task_events")
    grouped = {}
    for row in _recent(rows, since, "updated_at"):
        status = row["status"]
        failed, ever, waited = _counts(reader, row, since)
        if status == "RETRY_WAIT":
            if not failed:
                continue   # only waits in the window: clears on its own
            kind = "retry"
        elif status == "EXHAUSTED":
            kind = "gave_up" if ever else ("out_of_time_waiting" if waited else "out_of_time")
        elif status == "UNCERTAIN":
            kind = "interrupted_write" if row["effect"] == "WRITE" else "interrupted"
        else:
            kind = "crashed"
        worker = str(row["worker"] or "")
        key = (worker_words(worker), kind, worker in HOURLY_STEPS)
        entry = grouped.setdefault(key, {"count": 0, "at": None, "retry_at": None, "moments": [], "job": None, "slots": []})
        entry["count"] += max(1, failed)
        moment = overview._utc(row["updated_at"])
        entry["at"] = max(filter(None, (entry["at"], moment)), default=None)
        if kind == "crashed" and worker in HOURLY_STEPS:
            entry["job"] = "applications" if worker == "applications" else "run"
            entry["moments"].append(moment)
            entry["slots"].append(str(row["parent_run_id"]))   # worker_step's parent run is the round's runs.slot
        retry = overview._utc(row["next_at"]) if row["next_at"] else None
        if retry is not None and retry > now:
            entry["retry_at"] = min(filter(None, (entry["retry_at"], retry)), default=None)
    found = []
    for ((group, name), kind, hourly), entry in grouped.items():
        if kind == "retry":
            text = "%s didn't work." % name
            todo = ("Nothing you need to do. Sidedoor will try again %s." % _at(fmt, entry["retry_at"]) if entry["retry_at"]
                    else "Nothing you need to do. Sidedoor will try again on its own.")
        elif kind == "gave_up":
            text = "%s kept failing, so Sidedoor stopped trying." % name
            todo = "Nothing you need to do. Sidedoor moves on to its other work."
        elif kind == "out_of_time_waiting":
            text = "%s ran out of time while waiting for compute, so Sidedoor stopped trying." % name
            todo = "Nothing you need to do. Sidedoor moves on to its other work."
        elif kind == "out_of_time":
            text = "%s ran out of time before it could run, so Sidedoor stopped trying." % name
            todo = "Nothing you need to do. Sidedoor moves on to its other work."
        elif kind == "interrupted_write" and hourly:
            text = "%s was interrupted before it finished." % name
            todo = ("Nothing you need to do. The next round runs it again; the interrupted run is not repeated, so "
                    "nothing is done twice.")
        elif kind == "interrupted_write":
            text = "%s was interrupted while making a change, and Sidedoor can't tell whether the change went through." % name
            todo = ("Sidedoor won't repeat it on its own, so nothing happens twice. If it was a message to someone, look "
                    "at that conversation on LinkedIn.")
        elif kind == "interrupted":
            text = "%s was interrupted before it finished." % name
            todo = "Nothing you need to do. Sidedoor will try again on its own."
        else:
            text = "%s stopped with an error." % name
            todo = ("Nothing you need to do. It runs again on Sidedoor's next round." if hourly
                    else "Nothing you need to do. Sidedoor moves on to its other work.")
        found.append(_item(group, text, todo, entry["at"], entry["count"], entry["retry_at"], job=entry["job"],
                           moments=entry["moments"], slots=entry["slots"]))
    return found


# --- Scheduler -----------------------------------------------------------------------------------------

JOB_NAMES = {"research": "Research", "run": "The hourly round", "applications": "Applications"}
CYCLE_SPAN = dt.timedelta(hours=6)       # an unfinished round's longest possible run (worker_step's lease)
JOB_SLACK = dt.timedelta(minutes=5)
# How long before an applications or research child's exit the failure that made it exit can have
# happened: an applications step's crash is recorded just before its child exits; a research
# attempt starts up to research_timeout before it.
JOB_REACH = {"applications": dt.timedelta(minutes=10), "research": dt.timedelta(hours=12)}


def cycle_failures(reader, now, crashed_slots=()):
    """Hourly rounds that raised or died (`runs`). A round one of whose own steps crashed (that step's
    task carries the round's slot as its parent run, `orchestrator.worker_step`) is that step's
    failure, not a second one (F1). Matched by the round itself, never by a span of time (an
    INTERRUPTED round has no completed_at)."""
    since = _since(now)
    rows = reader.rows("SELECT slot,started_at,state,completed_at FROM runs WHERE started_at>=? AND state IN ('FAILED','INTERRUPTED')",
                       (overview._prefilter(since),), "runs")
    found = []
    for state, text in (("FAILED", "The hourly round stopped with an error."),
                        ("INTERRUPTED", "The hourly round was interrupted before it finished.")):
        matching, slots = [], []
        for row in _recent(rows, since, "started_at"):
            if row["state"] != state or str(row["slot"]) in crashed_slots:
                continue
            start = overview._utc(row["started_at"])
            matching.append(max(filter(None, (start, overview._utc(row["completed_at"])))))
            slots.append(str(row["slot"]))
        if matching:
            found.append(_item("scheduler", text, "Nothing you need to do. It runs again at the start of the next hour.",
                               max(matching), len(matching), job="run", moments=matching, slots=slots))
    return found


def round_ending_at(reader, exited):
    """The slot of the hourly round whose child exited at `exited`: the latest round started before
    the exit that completed at the exit (within JOB_SLACK) or never completed. None when no runs row
    is from that window."""
    rows = reader.rows("SELECT slot,started_at,completed_at FROM runs WHERE started_at>=? AND started_at<=?",
                       ((exited - CYCLE_SPAN).date().isoformat(), (exited + dt.timedelta(days=1)).date().isoformat()), "runs")
    started = [(overview._utc(row["started_at"]), row) for row in rows]
    started = [(moment, row) for moment, row in started if moment is not None and exited - CYCLE_SPAN <= moment <= exited]
    if not started:
        return None
    moment, row = max(started, key=lambda pair: pair[0])
    completed = overview._utc(row["completed_at"])
    if completed is None or abs(completed - exited) <= JOB_SLACK:
        return str(row["slot"])
    return None


def next_run(job, config, now):
    """When the loop next starts `job` (`scheduler_loop.schedule`: its latest instant plus its period)."""
    import scheduler_loop
    try:
        instants, periods = scheduler_loop.schedule(now, config)
        return dt.datetime.fromtimestamp(instants[job] + periods[job], dt.timezone.utc)
    except Exception:   # a schedule that cannot be computed only loses the time, never the page
        return None


def scheduler_failures(loop, config, now, fmt, log_tail=None, secrets=()):
    """The loop's own problems (`overview_page.loop_state`): a job child that exited non-zero, with the
    redacted tail of that job's error log, and a problem keeping the schedule."""
    import scheduler_loop
    found = []
    running = bool(loop.get("running"))
    for job, failure in sorted((loop.get("job_failures") or {}).items()):
        moment = overview._utc(failure.get("at")) if isinstance(failure, dict) else None
        name = JOB_NAMES.get(job, "A background task")
        text = "%s stopped with an error%s." % (name, " at " + fmt(moment) if moment else "")
        if running:
            upcoming = next_run(job, config, now) if job in JOB_NAMES else None
            todo = ("Nothing you need to do. It runs again %s." % _at(fmt, upcoming) if upcoming
                    else "Nothing you need to do. It runs again at its next scheduled time.")
            fix = None
        else:
            todo, fix = "It runs again once Sidedoor is running. Start it on Overview.", {"label": "Open Overview", "href": "/"}
        stem = scheduler_loop.LOG_NAMES.get(job)
        log = log_tail(stem + ".stderr.log") if log_tail and stem else None
        entry = _item("scheduler", text, todo, moment, fix=fix, log=log)
        entry["exited"] = job
        found.append(entry)
    if loop.get("last_error"):
        found.append(_item("scheduler", "Sidedoor ran into a problem keeping its schedule.",
                           "Nothing you need to do if it goes away on its own. If it stays, download the logs below and "
                           "send them to %s." % SUPPORT_EMAIL, detail=redact_text(loop["last_error"], secrets)))
    return found


# --- Research --------------------------------------------------------------------------------------------

def _research_reason(result):
    state = str((result or {}).get("state") or "").upper()
    if state == "INTERRUPTED":
        return "it was stopped before it finished (Sidedoor was closed or restarted)"
    if state == "FAILED" and (result or {}).get("error"):
        return "it ran into an error"
    if state == "FAILED":
        return "no industry finished its search"
    return "it ended before finishing"


def _day_label(day):
    try:
        date = dt.date.fromisoformat(str(day))
    except ValueError:
        return "an earlier day"
    return "%s %s %d" % (date.strftime("%a"), date.strftime("%b"), date.day)


def research_failures(reader, config, now, fmt):
    zone = config.get("timezone") or "America/Los_Angeles"
    hour = config.get("research_hour", workflow_tasks.day_start_hour(config))
    today = workflow_tasks.service_date(now, zone, hour)
    first = (now - dt.timedelta(days=RECENT_DAYS)).date().isoformat()
    days = reader.rows("SELECT day,state,attempts,attempt_id,next_retry_at,compute_wait_attempts FROM research_days "
                       "WHERE day>=? AND state IN ('RETRY_WAIT','EXHAUSTED') ORDER BY day DESC", (first,), "research_days")
    found = []
    for day in days:
        attempt = reader.rows("SELECT started_at,state,result FROM research WHERE id=?", (day["attempt_id"],), "research")
        result = overview._json(attempt[0]["result"]) if attempt else None
        if result is None and attempt:
            result = {"state": attempt[0]["state"]}
        if day["state"] == "RETRY_WAIT" and result and result.get("compute_wait") is True:
            continue   # waiting for the shared compute budget, not a failed run
        tries = max(1, (day["attempts"] or 0) - (day["compute_wait_attempts"] or 0))
        at = overview._utc(attempt[0]["started_at"]) if attempt else None
        reason = _research_reason(result)
        if day["day"] == today:
            text = "Research didn't finish today: %s." % reason
            retry = overview._utc(day["next_retry_at"]) if day["next_retry_at"] else None
            if day["state"] == "RETRY_WAIT" and retry is not None and retry > now:
                todo = "It will try again %s." % _at(fmt, retry)
            elif day["state"] == "RETRY_WAIT":
                todo = "It will try again on Sidedoor's next research check, within a few minutes while Sidedoor runs."
                retry = None
            else:
                retry = None
                todo = ("It used all of today's tries. Nothing you need to do: research runs again %s."
                        % _at(fmt, workflow_tasks.next_day_start(now, zone, hour)))
            found.append(_item("research", text, todo, at, tries, retry, job="research"))
        else:
            found.append(_item("research", "Research didn't finish on %s: %s." % (_day_label(day["day"]), reason),
                               "Nothing you need to do. That day is over; research runs again every day.", at, tries,
                               job="research"))
    return found


# --- Applications ------------------------------------------------------------------------------------------

def _job_link(key):
    return "/applications?" + urllib.parse.urlencode({"job": key})


def application_failures(reader, now, fmt):
    """Owner holds (Needs you, as Overview counts them) and held jobs whose blocker is a failed
    attempt. Words and next steps are the Applications page's own (`applications_page.job_view`).
    Given-up jobs are not listed here: they live under Applications' "Given up" (owner, D5)."""
    since = _since(now)
    owner, _ = overview._blocked(reader)
    owner_keys = set(owner)
    asked = {row[0] for row in overview._pending_questions(reader, now)}
    codes = application_failure_codes()
    counts = {}
    for key, detail in reader.rows("SELECT job_key,detail FROM application_events WHERE event IN ('BLOCKED','RETRY_WAIT') "
                                   "AND at>=?", (overview._prefilter(since),), "application_events"):
        recorded = overview._json(detail) or {}
        if _code(recorded.get("code")) in codes:   # F8: only failed attempts count, never waits or holds
            counts[key] = counts.get(key, 0) + 1
    rows = reader.rows("SELECT job_key,employer,payload,state,blocker,next_at,updated_at,receipt FROM application_jobs "
                       "WHERE state IN ('BLOCKED','RETRY_WAIT') ORDER BY updated_at DESC", (), "application_jobs")
    found = []
    for row in rows:
        key = row["job_key"]
        if key in owner_keys and key in asked:
            continue   # the question is on Approvals; Overview counts it there, not here
        if key not in owner_keys and not is_application_failure(row["blocker"], row["state"]):
            continue
        view = applications_page.job_view(row, owner_keys, asked, set(), fmt)
        name = "%s, %s" % (view["company"], view["role"]) if view["role"] != "Role not recorded" else view["company"]
        link = {"label": "Open application", "href": _job_link(key)}
        at = overview._utc(row["updated_at"])
        if view["needs_you"]:
            found.append(_item("applications", "%s: %s" % (name, view["problem"]), view["todo"], at, you=True,
                               fix={"label": view["todo_label"], "href": view["todo_href"]} if view["todo_href"] else link,
                               href=_job_link(key)))
        else:
            retry = view["next_at"]
            found.append(_item("applications", "%s: %s" % (name, view["problem"]), "Nothing you need to do. " + view["todo"],
                               at, counts.get(key, 1), retry if retry is not None and retry > now else None, fix=link,
                               href=_job_link(key)))
    return found


# --- Outreach writes -----------------------------------------------------------------------------------------

def reply_retry_state(result):
    """What `reply_execution.retry_reply` does with a REPLY in UNCERTAIN_NO_RETRY (F2): it reads the
    thread each round and sends again while fewer than MAX_SEND_ATTEMPTS were made and the
    no-more-sends flag is not set ("REPLY_RETRYING"); after that it only keeps checking
    ("UNCERTAIN_NO_RETRY")."""
    import reply_execution
    retries = result.get("reply_retries")
    attempts = (1 if isinstance(result.get("provider_request"), dict) else 0) + (len(retries) if isinstance(retries, list) else 0)
    streak = result.get("reply_unreadable_streak")
    stopped = (result.get("reply_no_more_sends") is True
               or (isinstance(streak, int) and streak >= reply_execution.MAX_UNREADABLE_CHECKS))
    return "REPLY_RETRYING" if attempts < reply_execution.MAX_SEND_ATTEMPTS and not stopped else "UNCERTAIN_NO_RETRY"


def outreach_failures(reader, now):
    since = _since(now)
    found = []
    states = ACTION_RETRYING + ACTION_FINAL
    rows = reader.rows("SELECT state,payload,claimed_at,result FROM outreach_actions WHERE state IN (%s)" % ",".join("?" * len(states)),
                       states, "outreach_actions")
    grouped = {}
    for row in rows:
        at = overview._utc(row["claimed_at"]) if row["claimed_at"] else None
        payload = overview._json(row["payload"]) or {}
        state = row["state"]
        if state == "UNCERTAIN_NO_RETRY" and payload.get("purpose") == "REPLY":
            state = reply_retry_state(overview._json(row["result"]) or {})
        if state == "CONFIRMED_NOT_SENT":
            # The recorded reason (GL-L1): an invitation the owner had already made or a connection
            # that already existed is not a failed send.
            not_sent = (overview._json(row["result"]) or {}).get("not_sent")
            if isinstance(not_sent, dict) and not_sent.get("reason") == "ALREADY_CONTACTED_ELSEWHERE":
                state = "CONTACTED_ELSEWHERE"
        if (state in ACTION_FINAL or state == "CONTACTED_ELSEWHERE") and (at is None or at < since):
            continue
        words = PURPOSE_WORDS.get(str(payload.get("purpose") or ""), MESSAGE_WORDS)
        entry = grouped.setdefault((state, words), {"count": 0, "at": None})
        entry["count"] += 1
        entry["at"] = max(filter(None, (entry["at"], at)), default=None)
    for (state, words), entry in grouped.items():
        count = entry["count"]
        if state == "INVITE_RETRY_WAIT":
            text = "%s didn't go through." % _plural(count, words)
            todo = "Nothing you need to do. Sidedoor checks LinkedIn and tries again on its next round, up to 3 tries."
        elif state == "WRITE_RETRY_WAIT":
            text = "Preparing %s didn't work." % _plural(count, words)
            todo = "Nothing you need to do. Sidedoor will try again on its own, up to 3 tries."
        elif state == "CONFIRMED_NOT_SENT":
            text = "%s couldn't be sent after 3 tries." % _plural(count, words)
            todo = "Nothing you need to do. Sidedoor won't try these again."
        elif state == "CONTACTED_ELSEWHERE":
            text = ("You'd already invited or connected with them yourself, so Sidedoor didn't count this as its invitation."
                    if count == 1 else "You'd already invited or connected with %d people yourself, so Sidedoor didn't "
                    "count these as its invitations." % count)
            todo = "Nothing you need to do."
        elif state == "REPLY_RETRYING":
            text = "%s didn't go through." % _plural(count, words)
            todo = ("Nothing you need to do. Sidedoor checks the conversation and sends it again only if it isn't there, "
                    "up to 3 tries.")
        elif state == "WRITE_FAILED":
            text = "Preparing %s failed 3 times." % _plural(count, words)
            todo = "Nothing you need to do. Sidedoor won't try these again."
        else:
            text = "Sidedoor couldn't confirm whether %s went out." % _plural(count, words)
            todo = ("Sidedoor won't send %s again, so nobody gets a message twice. If you want to be sure, look at the "
                    "conversation on LinkedIn." % ("it" if count == 1 else "them"))
            if words == PURPOSE_WORDS["REPLY"]:
                todo = ("Nothing you need to do. Sidedoor keeps checking the conversation but won't send %s again, so "
                        "nobody gets a reply twice." % ("it" if count == 1 else "them"))
        found.append(_item("outreach", text, todo, entry["at"], count))
    launches = reader.rows("SELECT state,claimed_at FROM outreach_attempts WHERE state IN ('LAUNCH_RETRY_WAIT','LAUNCH_FAILED')",
                           (), "outreach_attempts")
    for state, text, todo in (("LAUNCH_RETRY_WAIT", "didn't start", "Nothing you need to do. Sidedoor will try again on its own, up to 3 tries."),
                              ("LAUNCH_FAILED", "couldn't be started after 3 tries", "Nothing you need to do. Sidedoor won't try these again.")):
        matching = [row for row in launches if row["state"] == state]
        if state == "LAUNCH_FAILED":
            matching = _recent(matching, since, "claimed_at")
        if matching:
            at = max(filter(None, (overview._utc(row["claimed_at"]) for row in matching)), default=None)
            found.append(_item("outreach", "%s %s." % (_plural(len(matching), ("invitation campaign", "invitation campaigns")), text),
                               todo, at, len(matching)))
    return found


# --- Notifications --------------------------------------------------------------------------------------------

def notification_failures(reader, now, fmt):
    since = _since(now)
    columns = {row[1] for row in reader.rows("PRAGMA table_info(notification_outbox)", (), "notification_outbox")}
    checks = "reconcile_attempts" if "reconcile_attempts" in columns else "0"
    rows = reader.rows("SELECT state,next_at,last_error,attempts,updated_at," + checks + " AS checks FROM notification_outbox "
                       "WHERE state IN ('FAILED','UNCERTAIN') OR (state='PENDING' AND last_error IS NOT NULL AND attempts>0)",
                       (), "notification_outbox")
    words = ("notification", "notifications")
    found = []
    failed = _recent([row for row in rows if row["state"] == "FAILED"], since, "updated_at")
    unsure = _recent([row for row in rows if row["state"] == "UNCERTAIN"], since, "updated_at")   # F7: the window
    # notifications._dispatch reconciles an UNCERTAIN row at most 5 times, then stops for good.
    gave_up = lambda row: (isinstance(row["checks"], int) and row["checks"] >= 5) or \
        row["last_error"] == "NOTIFICATION_RECONCILIATION_EXHAUSTED"
    stopped = [row for row in unsure if gave_up(row)]
    unsure = [row for row in unsure if not gave_up(row)]
    waiting = [row for row in rows if row["state"] == "PENDING"]
    if failed:
        found.append(_item("connections", "%s couldn't be delivered." % _plural(len(failed), words),
                           "Nothing you need to do. Sidedoor stopped trying; anything that needs you also shows on this dashboard.",
                           max(filter(None, (overview._utc(row["updated_at"]) for row in failed)), default=None), len(failed)))
    if unsure:
        found.append(_item("connections", "Sidedoor couldn't confirm whether %s arrived." % _plural(len(unsure), words),
                           "Nothing you need to do. Sidedoor checks before sending again, so nothing arrives twice.",
                           max(filter(None, (overview._utc(row["updated_at"]) for row in unsure)), default=None), len(unsure)))
    if stopped:
        found.append(_item("connections", "Sidedoor couldn't confirm whether %s arrived, and has stopped checking."
                           % _plural(len(stopped), words),
                           "Nothing you need to do. Sidedoor won't send %s again; anything that needs you also shows on "
                           "this dashboard." % ("it" if len(stopped) == 1 else "them"),
                           max(filter(None, (overview._utc(row["updated_at"]) for row in stopped)), default=None), len(stopped)))
    if waiting:
        retry = min(filter(lambda moment: moment is not None and moment > now,
                           (overview._utc(row["next_at"]) if row["next_at"] else None for row in waiting)), default=None)
        found.append(_item("connections", "%s didn't go through yet." % _plural(len(waiting), words),
                           "Nothing you need to do. Sidedoor will try again %s." % (_at(fmt, retry) if retry else "on its own"),
                           max(filter(None, (overview._utc(row["updated_at"]) for row in waiting)), default=None),
                           len(waiting), retry))
    return found


# --- The browser could not start ---------------------------------------------------------------------------------

BROWSER_UNAVAILABLE_AFTER = dt.timedelta(hours=1)
BROWSER_GROUPS = {"applications": "applications", "linkedin-contact-discovery": "outreach"}


def browser_failures(reader, now, fmt):
    """Sessions held back for over an hour because Sidedoor's browser could not start (E7.T1
    confirming review fix 1): the runtime missing never clears on its own, so it is listed like a
    failure, not a self-clearing wait. One item per code, from browser_runtime's record."""
    import browser_runtime
    found = []
    for row in reader.rows("SELECT code,source,first_seen,last_seen FROM browser_unavailable", (), "browser_unavailable"):
        since = overview._utc(row["first_seen"])
        if since is None or now - since < BROWSER_UNAVAILABLE_AFTER:
            continue
        reason = row["code"].split("_BROWSER_", 1)[-1]
        if reason not in browser_runtime.UNAVAILABLE_WORDS:
            continue
        found.append(_item(BROWSER_GROUPS.get(row["source"], "other"), browser_runtime.UNAVAILABLE_WORDS[reason],
                           browser_runtime.UNAVAILABLE_TODO[reason] + " It started %s." % fmt(since),
                           overview._utc(row["last_seen"]) or since, you=True, detail=row["code"]))
    return found


# --- Gathering ----------------------------------------------------------------------------------------------------

def explain_exits(sections, loop_items, reader=None):
    """One item per real failure (F1): a job child that exited non-zero is shown on its own only when
    a more specific failure of that very run is not already listed; otherwise that failure carries
    the job's error log. The hourly round's exit is matched to the round that ended at that exit
    (`round_ending_at`) and to that round's own crashed steps or runs row only; an exit with no runs
    row from its window stands alone."""
    specific = [entry for section in sections.values() for entry in section["entries"] if entry["job"]]
    for entry in loop_items:
        job, exited = entry.get("exited"), entry["at"]
        matches = []
        if job == "run" and exited is not None and reader is not None:
            slot = overview.section(reader, round_ending_at, exited)
            if slot and slot != UNAVAILABLE:
                matches = [other for other in specific if other["job"] == "run" and slot in other["slots"]]
        elif job in JOB_REACH and exited is not None:
            matches = [other for other in specific if other["job"] == job
                       and any(exited - JOB_REACH[job] <= moment <= exited + JOB_SLACK for moment in other["moments"])]
        if not matches:
            sections["scheduler"]["entries"].append(entry)
            continue
        newest = max(matches, key=lambda other: max(other["moments"]))
        if entry["log"] and not newest["log"]:
            newest["log"] = entry["log"]


def gather(database, config, loop, now=None, log_tail=None, secrets=()):
    """Everything the Failures page shows. `database` is a read-only connection or None (no file
    yet); `loop` is the scheduler loop's state (`overview_page.loop_state`); `log_tail(name)` returns
    the redacted tail of logs/<name>. A source that cannot be read marks its group unavailable and
    the rest still render."""
    now = now or dt.datetime.now(dt.timezone.utc)
    zone = ZoneInfo(config.get("timezone") or "America/Los_Angeles")
    fmt = lambda moment: overview.when(moment, now, zone)
    reader = overview.Reader(database)
    sections = {key: {"key": key, "title": title, "entries": [], "unavailable": False}
                for key, title in (("scheduler", "Sidedoor itself"),) + GROUPS}

    def add(read, *arguments, groups=()):
        found = overview.section(reader, read, *arguments)
        if found == UNAVAILABLE:
            for key in groups:
                sections[key]["unavailable"] = True
            return
        for entry in found:
            sections[entry["group"]]["entries"].append(entry)

    add(task_failures, now, fmt, groups=("outreach", "applications", "connections", "other"))
    crashed = {slot for section in sections.values() for entry in section["entries"] if entry["job"] == "run"
               for slot in entry["slots"]}
    add(cycle_failures, now, crashed, groups=("scheduler",))
    add(research_failures, config, now, fmt, groups=("research",))
    add(application_failures, now, fmt, groups=("applications",))
    add(outreach_failures, now, groups=("outreach",))
    add(notification_failures, now, fmt, groups=("connections",))
    add(browser_failures, now, fmt, groups=("applications", "outreach"))
    explain_exits(sections, scheduler_failures(loop if isinstance(loop, dict) else {}, config, now, fmt, log_tail, secrets),
                  None if reader.broken else reader)
    if reader.broken:
        for section in sections.values():
            section["unavailable"] = section["key"] != "scheduler"
    oldest = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    newest_first = lambda entries: sorted(entries, key=lambda entry: entry["at"] or oldest, reverse=True)
    # What needs the user is listed once, under Needs you (Overview's needs-you link lands there).
    needs = newest_first(entry for section in sections.values() for entry in section["entries"] if entry["you"])
    for section in sections.values():
        section["entries"] = newest_first(entry for entry in section["entries"] if not entry["you"])
        section["more"] = max(0, len(section["entries"]) - LIST_LIMIT)
        section["entries"] = section["entries"][:LIST_LIMIT]
    ordered = [sections["scheduler"]] + [sections[key] for key, _ in GROUPS]
    shown = [section for section in ordered if section["entries"] or section["unavailable"]]
    return {"sections": shown, "needs_you": needs[:LIST_LIMIT], "nothing": not shown and not needs, "unreadable": reader.broken,
            "when": fmt, "recent_days": RECENT_DAYS}


# --- Logs ---------------------------------------------------------------------------------------------------------

LOG_TAIL_BYTES = 32768    # read from the end of each file
LOG_LINES = 200           # lines shown per file
JOB_LOG_LINES = 20        # lines shown under a failed scheduler job
LINE_CHARACTERS = 500
SEARCH_CHARACTERS = 200
COMPONENTS = (("launchd", "Hourly round"), ("research", "Research"), ("applications", "Applications"),
              ("sidedoor", "Sidedoor"))
LEVELS = (("stderr", "Errors"), ("stdout", "Output"))
_ESCAPES = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|[\x00-\x08\x0b-\x1f\x7f]")


class Secrets(list):
    """Values never shown, longest first. `fragments` come from the keys and tokens only: a line holding
    an 8-character piece of one is dropped (`reply_worker.redact_line`); host names are only replaced,
    or every line naming the provider would be dropped."""

    def __init__(self, keys=(), hosts=()):
        import reply_worker
        keys = [value for value in keys if isinstance(value, str) and value]
        hosts = [value for value in hosts if isinstance(value, str) and value]
        super().__init__(sorted(set(keys + hosts), key=len, reverse=True))
        self.fragments = reply_worker.secret_fragments(keys)


def _read(path):
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None


def secrets_of(home, config, token=None):
    """The values never shown: the dashboard token, the license key, the OpenRouter key, the Unipile
    token and DSN (`<home>/unipile.json`, `unipile_port.settings`, read only under the owner's
    `"linkedin_transport": "local-dev"`, RL1: a user's install goes through the relay and has no
    such file) and the HeyReach endpoint (`config["credential_config"]`, as `orchestrator.HeyReach`
    reads it), with their bare hosts. Reads only; a file that is missing or unreadable is skipped."""
    keys, hosts = [token], []
    home = Path(home) if home is not None else None
    paths = [home / "license.key", home / "openrouter.key", home / "dashboard-token"] if home is not None else []
    try:
        import text_model
        chosen = text_model.settings(config)
        if chosen and chosen.get("key_file"):
            paths.append(Path(os.path.expanduser(chosen["key_file"])))
    except Exception:
        pass
    keys += [_read(path) for path in paths]
    try:
        import unipile_port
        if not unipile_port.local_dev(config):
            raise LookupError("relay install: no unipile.json")
        unipile = unipile_port.settings(home / "unipile.json" if home is not None else None)
        dsn = unipile["base"].removeprefix("https://").removesuffix("/api/v1")
        keys.append(unipile["token"])
        hosts += [dsn, dsn.split(":", 1)[0]]
    except Exception:   # no Unipile set up, or an unreadable file: nothing to hide
        pass
    try:
        import tomllib
        with Path(config["credential_config"]).expanduser().open("rb") as source:
            url = tomllib.load(source)["mcp_servers"]["heyreach"]["url"]
        parts = urllib.parse.urlsplit(url)
        hosts += [url, parts.netloc, parts.hostname or ""]
        credential = (parts.path.lstrip("/") + ("?" + parts.query if parts.query else ""))
        if len(credential) >= 16:
            keys.append(credential)
    except Exception:   # no HeyReach set up
        pass
    return Secrets(keys, hosts)


def _fragments(secrets):
    import reply_worker
    found = getattr(secrets, "fragments", None)
    return found if found is not None else reply_worker.secret_fragments(secrets)


def redact_text(text, secrets=()):
    """A short problem line with secrets removed (`reply_worker.redact_line`)."""
    import reply_worker
    lines = []
    for line in _ESCAPES.sub("", str(text or "")).splitlines() or [""]:
        kept = reply_worker.redact_line(line, secrets, _fragments(secrets))
        if kept is not None:
            lines.append(kept[:LINE_CHARACTERS])
    return " ".join(lines) or "Not shown: it may contain a secret."


def read_tail(path, limit=LOG_TAIL_BYTES):
    """(state, text): "ok" with the last `limit` bytes (a partial first line dropped), "missing" or
    "unreadable". Never follows a symlink and reads only a regular file; writes nothing."""
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return "unreadable", ""   # a FIFO, device, directory or symlink is never opened (F14)
    except FileNotFoundError:
        return "missing", ""
    except OSError:
        return "unreadable", ""
    # O_NONBLOCK as well: a file swapped for a FIFO after the check still cannot block the page.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return "missing", ""
    except OSError:
        return "unreadable", ""
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return "unreadable", ""
        start = max(0, info.st_size - limit)
        os.lseek(descriptor, start, os.SEEK_SET)
        data = b""
        while len(data) < limit:
            chunk = os.read(descriptor, limit - len(data))
            if not chunk:
                break
            data += chunk
    except OSError:
        return "unreadable", ""
    finally:
        os.close(descriptor)
    text = data.decode("utf-8", "replace")
    if start > 0:
        text = text.split("\n", 1)[1] if "\n" in text else ""
    return "ok", text


def redacted_lines(text, secrets, limit=LOG_LINES):
    import reply_worker
    fragments = _fragments(secrets)
    kept = []
    for line in text.splitlines():
        line = _ESCAPES.sub("", line).rstrip()
        if not line.strip():
            continue
        line = reply_worker.redact_line(line, secrets, fragments)
        if line is not None:
            kept.append(line[:LINE_CHARACTERS])
    return kept[-limit:]


def tail(logs, name, secrets, limit=LOG_LINES):
    """The redacted last lines of logs/<name>, or [] when it is missing or unreadable."""
    state, text = read_tail(Path(logs) / name)
    return redacted_lines(text, secrets, limit) if state == "ok" else []


def _choice(value, options, default="all"):
    return value if value in {key for key, _ in options} else default


def logs_view(logs, secrets, component="all", level="all", query=""):
    """Every stream under logs/ (log_retention.STREAMS), filtered by component and level, searched
    case-insensitively in the redacted lines."""
    component, level = _choice(component, COMPONENTS), _choice(level, LEVELS)
    query = (query or "").strip()[:SEARCH_CHARACTERS]
    needle = query.casefold()
    streams = []
    for key, label in COMPONENTS:
        if component not in ("all", key):
            continue
        for stream, level_label in LEVELS:
            if level not in ("all", stream):
                continue
            name = "%s.%s.log" % (key, stream)
            state, text = read_tail(Path(logs) / name)
            lines = redacted_lines(text, secrets) if state == "ok" else []
            if needle:
                lines = [line for line in lines if needle in line.casefold()]
            streams.append({"name": name, "label": "%s: %s" % (label, level_label.lower()), "state": state,
                            "lines": lines, "id": "log-%s-%s" % (key, stream)})
    return {"streams": streams, "component": component, "level": level, "query": query,
            "components": COMPONENTS, "levels": LEVELS, "tail_bytes": LOG_TAIL_BYTES, "line_limit": LOG_LINES}


def download_text(logs, secrets, now=None):
    """The redacted tails of every stream as one text file, for a support issue."""
    now = now or dt.datetime.now(dt.timezone.utc)
    parts = ["Sidedoor logs (the last %d KB of each file, secrets removed), %s"
             % (LOG_TAIL_BYTES // 1024, now.isoformat(timespec="seconds")), ""]
    for stream in logs_view(logs, secrets)["streams"]:
        parts.append("== %s ==" % stream["name"])
        if stream["state"] == "missing":
            parts.append("(no file yet)")
        elif stream["state"] == "unreadable":
            parts.append("(could not be read)")
        else:
            parts.extend(stream["lines"] or ["(empty)"])
        parts.append("")
    return "\n".join(parts)


# --- Routes ---------------------------------------------------------------------------------------------------------

def register(app, templates, layout, home, root, config, token, database, connect_read_only):
    """GET /failures and GET /failures/logs.txt. Both read only: the database through
    `overview_page.snapshot`, the logs through `read_tail`."""
    from fastapi import Request
    from fastapi.responses import PlainTextResponse
    import setup_steps

    logs = paths.logs(root)

    @app.get("/failures", name="failures")
    def failures(request: Request, component: str = "all", level: str = "all", q: str = ""):
        context = layout("failures", "Failures and logs")
        secrets = secrets_of(home, config, token)
        loop = overview.loop_state(setup_steps.scheduler_running(home))
        context["view"] = overview.snapshot(
            database, lambda connection: gather(connection, config, loop, secrets=secrets,
                                                log_tail=lambda name: tail(logs, name, secrets, JOB_LOG_LINES)),
            connect_read_only)
        context["logs"] = logs_view(logs, secrets, component, level, q)
        return templates.TemplateResponse(request, "failures.html", context)

    @app.get("/failures/logs.txt")
    def failures_download(request: Request):
        text = download_text(logs, secrets_of(home, config, token))
        return PlainTextResponse(text, headers={"Content-Disposition": 'attachment; filename="sidedoor-logs.txt"'})
