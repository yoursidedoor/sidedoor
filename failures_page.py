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

Each failure has a plain-words headline and next step (an unknown worker or code gets generic ones).
Under it (owner, 2026-10-23: "why dont we display the actual failure reasons?") are its recorded
causes: plain words where a wording exists, the code, the recorded error (redacted, bounded), where it
applies, how often and when, and "See its log", which opens that run's own records and files
(`run_view`: task events, research step logs, a session folder's stderr / output / trace tail and the
stream lines tagged with the run, `log.tagged`). The contact finder's failed and unfinished runs are
listed too (`finder_failures`). Report a problem still sends the item codes only.
Indexes these reads use are created by the writers' schema setup (workflow_tasks.initialize,
applications.initialize, outreach.initialize, orchestrator.connect).
"""

import contextvars
import datetime as dt
import json
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
          job=None, moments=(), slots=(), code=None, reasons=None, refs=None, log_href=None):
    """One failure. `todo` always says what happens next or what to do; `fix` is {"label", "href"}.
    `job` names the scheduler job ("run", "research", "applications") whose exit this failure
    explains, and `moments` when each such failure happened (F1: one item per real failure). `code` is a
    short program code for the Report a problem summary (report_problem); it is never shown here.
    `reasons` (a Causes, 2026-10-23) are the recorded causes shown under the headline: at most
    REASON_LIMIT distinct ones, newest first, each {"words", "code", "count", "at", "where", "error",
    "log_href"}, plus `reasons_more` and `reasons_href` (the Logs view of that component). `refs` are
    the item's own ids (task, research run, finder run, job key, round slot) and `log_href` opens that
    run's own records and files (`run_view`)."""
    shown, more = reasons.top() if reasons is not None else ([], 0)
    return {"group": group, "text": text, "todo": todo, "at": at, "count": max(1, int(count or 1)), "retry_at": retry_at,
            "you": you, "fix": fix, "detail": detail, "log": log or [], "href": href, "job": job,
            "moments": [moment for moment in moments if moment is not None] or ([at] if job and at else []),
            "slots": list(slots), "code": code, "reasons": shown, "reasons_more": more,
            "reasons_href": reasons.more_href if reasons is not None else None, "refs": dict(refs or {}),
            "log_href": log_href or (shown[0]["log_href"] if shown else None)}


# --- Recorded causes (2026-10-23: "why dont we display the actual failure reasons?") ---------------

REASON_LIMIT = 3
ERROR_CHARACTERS = 300
# Plain words for codes the Applications page has no wording for; any other code shows alone.
CAUSE_WORDS = {
    "WORKER_EXCEPTION": "The step stopped with an error in Sidedoor's code.",
    "SUBSCRIPTION_EXECUTOR_FAILED": "The AI call failed.",
    "SUBSCRIPTION_EXECUTOR_UNAVAILABLE": "The AI program could not be started.",
    "CLASSIFICATION_TIMEOUT": "The AI took too long to answer.",
    "CLASSIFICATION_INTERRUPTED": "The AI call was interrupted (Sidedoor was closed or restarted).",
    "CLASSIFICATION_OUTPUT_MISSING": "The AI gave no answer.",
    "CLASSIFICATION_OUTPUT_EXCEEDS_LIMIT": "The AI's answer was too long.",
    "CLASSIFICATION_FIELD_TOO_LONG": "Part of the AI's answer was too long.",
    "CLASSIFICATION_LOCAL_VALIDATION_FAILED": "The AI's answer did not pass Sidedoor's checks.",
    "OUTREACH_AI_TRANSIENT_FAILURE": "The AI check could not finish.",
    "LINKEDIN_CONTACTS_INTERRUPTED": "The search was interrupted (Sidedoor was closed or restarted).",
    "LINKEDIN_CONTACTS_RESULT_BINDING_INVALID": "The search's answer did not match what was asked.",
    "LINKEDIN_CONTACTS_LIMITS_INVALID": "The search went past its limits.",
    "LINKEDIN_CONTACTS_CONTACT_CONTROL_CLICKED": "The search clicked a button that contacts someone, so its result was thrown away.",
    "LINKEDIN_CONTACTS_UNKNOWN": "The search could not finish on LinkedIn.",
    "FINDER_RETRIES_EXHAUSTED": "The search failed 3 times for this research run, so Sidedoor stopped trying.",
    "RESEARCH_DEADLINE_EXHAUSTED": "The run hit its time limit before this industry finished.",
    "RECOVERY_DECISIONS_EXHAUSTED": "Its steps kept failing and recovery gave up.",
    "RECOVERY_DECISION_FAILED": "Working out another way to search failed.",
    "NO_SUPPORTED_ALTERNATIVE": "No other way to search was left to try.",
    "NO_SUPPORTED_RECOVERY_EXECUTOR": "No way to recover was available.",
    "RECOVERY_UNAVAILABLE": "Recovery could not run.",
    "RESEARCH_INTERRUPTED": "It was stopped before it finished (Sidedoor was closed or restarted).",
    "RESEARCH_FAILED": "It stopped with an error.",
    "NOTIFICATION_RECONCILIATION_EXHAUSTED": "Sidedoor checked 5 times and could not confirm delivery.",
}


def cause_words(code):
    root = _code(code)
    return CAUSE_WORDS.get(root) or applications_page.CODE_WORDS.get(root)


def _bounded(text, secrets):
    if text is None or text == "":
        return None
    text = redact_text(text, secrets)
    return text if len(text) <= ERROR_CHARACTERS else text[:ERROR_CHARACTERS - 1] + "\u2026"


def run_href(kind, ident):
    return "/failures?" + urllib.parse.urlencode({"run": "%s:%s" % (kind, ident)}) + "#run"


_ZONE = contextvars.ContextVar("failures_page_zone", default=None)   # the user's zone, set by gather / the route


def local_day(moment, zone=None):
    """The date of `moment` in the user's configured time zone (the dates the page shows), else UTC."""
    zone = zone or _ZONE.get()
    return (moment.astimezone(zone) if zone else moment).date().isoformat()


def logs_href(component, moment=None):
    query = {"component": component}
    if moment is not None:
        query["day"] = local_day(moment)
    return "/failures?" + urllib.parse.urlencode(query) + "#logs"


class Causes:
    """Distinct recorded causes of one item, by code root and where (company, industry, person, job)."""

    def __init__(self, secrets=(), more_href=None):
        self.secrets, self.more_href, self.found = secrets, more_href, {}

    def add(self, code, at=None, error=None, where=None, log_href=None):
        root = _code(code)[:120] or "UNKNOWN"
        where = str(where)[:120] if where else None
        entry = self.found.setdefault((root, where), {"words": cause_words(root), "code": root, "count": 0, "at": None,
                                                      "where": where, "error": None, "log_href": None})
        entry["count"] += 1
        if entry["at"] is None or (at is not None and at >= entry["at"]):
            entry["at"] = at or entry["at"]
            entry["error"] = _bounded(error, self.secrets) or entry["error"]
            entry["log_href"] = log_href or entry["log_href"]

    def top(self, limit=REASON_LIMIT):
        oldest = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        ordered = sorted(self.found.values(), key=lambda entry: entry["at"] or oldest, reverse=True)
        return ordered[:limit], max(0, len(ordered) - limit)


def _error_text(value):
    """The recorded error text of a result or detail dict: its error, the last of its errors, or where
    Sidedoor's code raised (`orchestrator.failure_detail`) and the end of a failed AI call's stderr."""
    if not isinstance(value, dict):
        return None
    parts = []
    for key in ("error", "last_error", "message", "reason"):
        if isinstance(value.get(key), str) and value[key].strip():
            parts.append(value[key])
            break
    for key in ("errors", "launch_errors"):
        listed = value.get(key)
        if not parts and isinstance(listed, list) and listed and isinstance(listed[-1], dict):
            if isinstance(listed[-1].get("error"), str):
                parts.append(listed[-1]["error"])
    if value.get("error_type") and not parts:
        parts.append(str(value["error_type"]) + (" at " + str(value["raised_at"]) if value.get("raised_at") else ""))
    failure = value.get("executor_failure") if isinstance(value.get("executor_failure"), dict) else value.get("executor")
    if isinstance(failure, dict):
        tail = failure.get("stderr_tail") if isinstance(failure.get("stderr_tail"), list) else []
        bits = ["exit %s" % failure["exit_code"]] if failure.get("exit_code") is not None else []
        if failure.get("http_status") is not None:
            bits.append("HTTP %s" % failure["http_status"])
        if isinstance(failure.get("error"), str):
            bits.append(failure["error"])
        if tail:
            bits.append(str(tail[-1]))
        if bits:
            parts.append("AI call: " + "; ".join(bits))
    return " ".join(parts) or None


def _result_code(value, fallback):
    if isinstance(value, dict):
        for key in ("blocker", "code", "error_code", "blocker_code"):
            if isinstance(value.get(key), str) and value[key]:
                return value[key]
        not_sent = value.get("not_sent")
        if isinstance(not_sent, dict) and isinstance(not_sent.get("reason"), str):
            return not_sent["reason"]
        error = value.get("error")
        if isinstance(error, str) and re.match(r"[A-Za-z_][A-Za-z0-9_]*: ", error):
            return error.split(":", 1)[0]
    return fallback


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


def _counts(reader, row, since, out=None):
    """(failed in the window, failed ever, waits ever) from the task's own events (F8/F12).
    `workflow_tasks.finish` records the blocker with a RETRY_WAIT/EXHAUSTED event: one with a
    budget/capacity blocker is a wait, not a try. claim's own EXHAUSTED ({}) ended no try. An older
    event without the blocker counts as a failure, except the latest one when the task's current
    blocker is a wait (it is that wait). `out` (a list) collects (code, moment, error text) of each
    failed try in the window."""
    events = reader.rows("SELECT id,at,event,detail FROM workflow_task_events WHERE task_id=? ORDER BY id",
                         (row["task_id"],), "workflow_task_events")
    failed = ever = waited = 0
    causes = []
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
            result = (detail or {}).get("result") if isinstance((detail or {}).get("result"), dict) else {}
            code = (detail or {}).get("blocker") if isinstance(detail, dict) and detail.get("blocker") else None
            if code is None and kind == "UNCERTAIN":
                code = "LEASE_EXPIRED_MID_RUN"
            causes.append((code or _result_code(result, row["blocker"] or kind), moment, _error_text(result)))
    if out is not None:
        out.extend(causes)
    return failed, ever, waited


def task_failures(reader, now, fmt, secrets=()):
    """Tasks with a real failed try in the window (F8/F12: counted from the failure events
    themselves, never from waits), one item per worker and kind, with the tries' recorded causes."""
    since = _since(now)
    rows = reader.rows("""SELECT t.task_id, t.worker, t.status, t.blocker, t.next_at, t.updated_at, t.effect, t.parent_run_id
        FROM workflow_tasks t
        WHERE t.status IN ('RETRY_WAIT','EXHAUSTED','UNCERTAIN','BLOCKED') AND t.updated_at>=?
          AND (t.status!='BLOCKED' OR t.blocker='WORKER_EXCEPTION')""",
                       (overview._prefilter(since),), "workflow_tasks", "workflow_task_events")
    grouped = {}
    for row in _recent(rows, since, "updated_at"):
        status = row["status"]
        tries = []
        failed, ever, waited = _counts(reader, row, since, tries)
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
        component = "applications" if worker in ("applications", "agent-investigation") else "launchd"
        entry = grouped.setdefault(key, {"count": 0, "at": None, "retry_at": None, "moments": [], "job": None, "slots": [],
                                         "code": "%s:%s" % (kind.upper(), worker.split(":", 1)[0]), "tasks": [],
                                         "causes": Causes(secrets, logs_href(component))})
        entry["count"] += max(1, failed)
        moment = overview._utc(row["updated_at"])
        link = run_href("task", row["task_id"])
        for code, at, error in tries:
            entry["causes"].add(code, at, error, log_href=link)
        if not tries:   # ran out of time, or a crash recorded before events kept the cause
            entry["causes"].add(row["blocker"] or kind.upper(), moment, log_href=link)
        entry["tasks"].append((moment, row["task_id"]))
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
        oldest = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        newest = max(entry["tasks"], key=lambda pair: pair[0] or oldest)[1]
        found.append(_item(group, text, todo, entry["at"], entry["count"], entry["retry_at"], job=entry["job"],
                           moments=entry["moments"], slots=entry["slots"], code=entry["code"], reasons=entry["causes"],
                           refs={"task_ids": [task for _, task in entry["tasks"]][:20]},
                           log_href=run_href("task", newest)))
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
                               max(matching), len(matching), job="run", moments=matching, slots=slots, code="ROUND_" + state,
                               refs={"slots": slots}, log_href=logs_href("launchd", max(matching))))
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
        component = {"run": "launchd"}.get(job, job if job in ("research", "applications") else "sidedoor")
        entry = _item("scheduler", text, todo, moment, fix=fix, log=log, code="JOB_EXIT:" + str(job),
                      refs={"job": job}, log_href=logs_href(component, moment))
        entry["exited"] = job
        found.append(entry)
    if loop.get("last_error"):
        found.append(_item("scheduler", "Sidedoor ran into a problem keeping its schedule.",
                           "Nothing you need to do if it goes away on its own. If it stays, download the logs below and "
                           "send them to %s." % SUPPORT_EMAIL, detail=redact_text(loop["last_error"], secrets),
                           code="SCHEDULE_ERROR"))
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


def _research_label(industry):
    import parallel_research
    return parallel_research._label(industry)


def research_causes(result, attempt_id, at, secrets=()):
    """One cause per industry that did not complete (its state or abandonment reason, the code, its
    error), and the run's own error; each links to that industry's step logs (`run_view`)."""
    causes = Causes(secrets, logs_href("research", at))
    result = result if isinstance(result, dict) else {}
    industries = result.get("industries")
    grouped = {}   # (code, error) -> [(name, link)]: one line for industries that stopped the same way
    for industry in industries if isinstance(industries, list) else []:
        if not isinstance(industry, dict) or str(industry.get("state") or "").upper() in ("COMPLETE", "WAITING_SHARED_RESEARCH"):
            continue
        name = str(industry.get("industry") or "An industry")
        state = str(industry.get("state") or "UNKNOWN").upper()
        code = industry.get("abandonment_reason") or ("RESEARCH_" + state if state in ("INTERRUPTED", "FAILED") else state)
        label = None
        try:
            label = _research_label(name) if industry.get("industry") else None
        except Exception:
            pass
        link = run_href("research", "%s:%s" % (attempt_id, label) if label else attempt_id) if attempt_id else None
        grouped.setdefault((_code(code), str(industry.get("error") or "")), []).append((name, link))
    for (code, error), names in grouped.items():
        if len(names) == 1:
            causes.add(code, at, error or None, where=names[0][0], log_href=names[0][1])
        else:
            shown = ", ".join(name for name, _ in names[:6]) + (", ..." if len(names) > 6 else "")
            causes.add(code, at, error or None, where="%d industries (%s)" % (len(names), shown),
                       log_href=run_href("research", attempt_id) if attempt_id else None)
    if result.get("error"):
        causes.add("RESEARCH_" + str(result.get("state") or "FAILED").upper(), at, result.get("error"),
                   log_href=run_href("research", attempt_id) if attempt_id else None)
    return causes


def research_failures(reader, config, now, fmt, secrets=()):
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
        causes = research_causes(result, day["attempt_id"], at, secrets)
        extra = {"reasons": causes, "refs": {"research_id": day["attempt_id"], "day": day["day"]},
                 "log_href": run_href("research", day["attempt_id"]) if day["attempt_id"] else None}
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
            found.append(_item("research", text, todo, at, tries, retry, job="research", **extra))
        else:
            found.append(_item("research", "Research didn't finish on %s: %s." % (_day_label(day["day"]), reason),
                               "Nothing you need to do. That day is over; research runs again every day.", at, tries,
                               job="research", **extra))
    return found


# --- Applications ------------------------------------------------------------------------------------------

def _job_link(key):
    return "/applications?" + urllib.parse.urlencode({"job": key})


ANSWERING_ATTEMPTS = 3   # the latest answering attempts whose stuck questions are shown


def question_name(key):
    """A question key's readable part ("unidentified-question:office_attendance_2_3:529c..." ->
    "office attendance 2 3"): the name before its fingerprint, never the answer."""
    parts = str(key or "").split(":")
    name = parts[1] if len(parts) >= 3 else parts[0]
    return name.replace("_", " ").strip()[:80] or "a question"


def answering_causes(reader, job_key, limit=ANSWERING_ATTEMPTS):
    """[(question, why, at)] from the job's latest answering attempts (application_answering_attempts
    audit): each question left unknown with its judge or repair reason, and each refused one. Newest
    attempt first; one entry per question."""
    found, seen = [], set()
    rows = reader.rows("SELECT created_at,outcome,audit FROM application_answering_attempts WHERE job_key=? "
                       "ORDER BY created_at DESC LIMIT ?", (job_key, limit), "application_answering_attempts")
    for row in rows:
        audit = overview._json(row["audit"]) or {}
        at = overview._utc(row["created_at"])
        verdicts = {}
        for verdict in audit.get("verdicts") or []:
            if isinstance(verdict, dict) and verdict.get("question_key"):
                verdicts[verdict["question_key"]] = verdict
        refused = {item.get("question_key"): item.get("reason") for item in audit.get("refused") or []
                   if isinstance(item, dict)}
        for key in list(audit.get("unknown") or []) + list(refused):
            if not isinstance(key, str) or key in seen:
                continue
            seen.add(key)
            verdict = verdicts.get(key, {})
            judge = verdict.get("judge") if isinstance(verdict.get("judge"), dict) else {}
            why = (refused.get(key) or verdict.get("reason") or judge.get("reason") or verdict.get("outcome")
                   or verdict.get("status") or row["outcome"])
            found.append((question_name(key), str(why), at))
    return found


def application_failures(reader, now, fmt, secrets=()):
    """Owner holds (Needs you, as Overview counts them) and held jobs whose blocker is a failed
    attempt. Words and next steps are the Applications page's own (`applications_page.job_view`).
    Given-up jobs are not listed here: they live under Applications' "Given up" (owner, D5)."""
    since = _since(now)
    owner, _ = overview._blocked(reader)
    owner_keys = set(owner)
    asked = {row[0] for row in overview._pending_questions(reader, now)}
    codes = application_failure_codes()
    counts, recorded_causes = {}, {}
    for key, detail, moment in reader.rows("SELECT job_key,detail,at FROM application_events WHERE event IN ('BLOCKED','RETRY_WAIT') "
                                           "AND at>=?", (overview._prefilter(since),), "application_events"):
        recorded = overview._json(detail) or {}
        if _code(recorded.get("code")) in codes:   # F8: only failed attempts count, never waits or holds
            counts[key] = counts.get(key, 0) + 1
            recorded_causes.setdefault(key, []).append((recorded.get("code"), overview._utc(moment), _error_text(recorded)
                                                        or (recorded.get("next_action") if isinstance(recorded.get("next_action"), str) else None)))
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
        code = _code(row["blocker"]) or "APPLICATION_" + str(row["state"])
        causes = Causes(secrets, logs_href("applications", at))
        extra = {"reasons": causes, "refs": {"job_key": key}, "log_href": run_href("job", key)}
        for recorded, moment, error in recorded_causes.get(key, []):
            causes.add(recorded, moment, error, where=name, log_href=extra["log_href"])
        if code == "ANSWERING_RETRY":
            # Why each question is still open (the answering audit), not the event's next action.
            causes = Causes(secrets, causes.more_href)
            extra["reasons"] = causes
            for question, why, moment in answering_causes(reader, key):
                causes.add("ANSWERING_RETRY", moment, why, where="Question: " + question, log_href=extra["log_href"])
        if not causes.found:
            causes.add(row["blocker"] or code, at, where=name, log_href=extra["log_href"])
        if view["needs_you"]:
            found.append(_item("applications", "%s: %s" % (name, view["problem"]), view["todo"], at, you=True,
                               fix={"label": view["todo_label"], "href": view["todo_href"]} if view["todo_href"] else link,
                               href=_job_link(key), code=code, **extra))
        else:
            retry = view["next_at"]
            found.append(_item("applications", "%s: %s" % (name, view["problem"]), "Nothing you need to do. " + view["todo"],
                               at, counts.get(key, 1), retry if retry is not None and retry > now else None, fix=link,
                               href=_job_link(key), code=code, **extra))
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


def _intent_errors(reader, action_ids):
    """The failed campaign writes of these actions (`outreach_campaign._record_failure`): {action_id: result}."""
    if not action_ids:
        return {}
    ids = sorted(action_ids)[:500]
    rows = reader.rows("SELECT action_id,result FROM outreach_campaign_intents WHERE state IN ('RETRY_WAIT','WRITE_FAILED') "
                       "AND action_id IN (%s)" % ",".join("?" * len(ids)), ids, "outreach_campaign_intents")
    return {row["action_id"]: overview._json(row["result"]) or {} for row in rows}


def _person(payload, result=None):
    for value in (payload or {}, result or {}):
        for key in ("recipient", "name", "profile", "company"):
            if isinstance(value.get(key), str) and value[key].strip():
                return value[key]
    return None


def outreach_failures(reader, now, secrets=()):
    since = _since(now)
    found = []
    states = ACTION_RETRYING + ACTION_FINAL
    rows = reader.rows("SELECT action_id,state,payload,claimed_at,result FROM outreach_actions WHERE state IN (%s)"
                       % ",".join("?" * len(states)), states, "outreach_actions")
    writes = _intent_errors(reader, {row["action_id"] for row in rows if row["state"] in ("WRITE_RETRY_WAIT", "WRITE_FAILED")})
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
        entry = grouped.setdefault((state, words), {"count": 0, "at": None, "causes": Causes(secrets, logs_href("launchd", at)),
                                                    "ids": []})
        entry["count"] += 1
        entry["at"] = max(filter(None, (entry["at"], at)), default=None)
        result = writes.get(row["action_id"]) or overview._json(row["result"]) or {}
        entry["causes"].add(_result_code(result, state), at, _error_text(result), where=_person(payload, result))
        entry["ids"].append(row["action_id"])
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
        found.append(_item("outreach", text, todo, entry["at"], count, code=state, reasons=entry["causes"],
                           refs={"action_ids": entry["ids"][:20]}))
    launches = reader.rows("SELECT identity,state,claimed_at,result FROM outreach_attempts WHERE state IN ('LAUNCH_RETRY_WAIT','LAUNCH_FAILED')",
                           (), "outreach_attempts")
    for state, text, todo in (("LAUNCH_RETRY_WAIT", "didn't start", "Nothing you need to do. Sidedoor will try again on its own, up to 3 tries."),
                              ("LAUNCH_FAILED", "couldn't be started after 3 tries", "Nothing you need to do. Sidedoor won't try these again.")):
        matching = [row for row in launches if row["state"] == state]
        if state == "LAUNCH_FAILED":
            matching = _recent(matching, since, "claimed_at")
        if matching:
            at = max(filter(None, (overview._utc(row["claimed_at"]) for row in matching)), default=None)
            causes = Causes(secrets, logs_href("launchd", at))
            for row in matching:
                result = overview._json(row["result"]) or {}
                causes.add(_result_code(result, state), overview._utc(result.get("error_at")) or overview._utc(row["claimed_at"]),
                           _error_text(result), where=_person(result) or row["identity"])
            found.append(_item("outreach", "%s %s." % (_plural(len(matching), ("invitation campaign", "invitation campaigns")), text),
                               todo, at, len(matching), code=state, reasons=causes,
                               refs={"identities": [row["identity"] for row in matching][:20]}))
    return found


# --- Contact finder (linkedin_contacts, linkedin_contact_runs) ------------------------------------------------

def _finder_tries(reader):
    """(key_of, settled, tries) with the runtime's own grouping (`linkedin_contacts._finder_runs`): keyed
    by (research plan id, company), a run's research_result_id mapped to its rw_results plan, so a
    settled company never shows as failing and "N of 3" is the count FINDER_RETRIES_EXHAUSTED uses."""
    import linkedin_contacts
    plans = {row["id"]: row["plan_id"] for row in reader.rows("SELECT id,plan_id FROM rw_results", (), "rw_results")}

    def key_of(value, company):
        return (plans.get(value.get("research_result_id")) or value.get("research_plan_id"), company)

    if not reader.has("linkedin_contact_runs", "rw_results"):
        return key_of, set(), {}
    settled, tries, _ = linkedin_contacts._finder_runs(reader.database)
    return key_of, settled, tries


def finder_failures(reader, now, fmt, secrets=()):
    """Contact-finder runs in the window that failed (error, interrupted, refused results) or could not
    finish (outcome UNKNOWN, with the model's own reason and its research_failure_diagnosis), one item
    per company and research run, with the tries used out of 3. Waits never get a run row
    (DEFERRED_BROWSER_BUSY, DEFERRED_COMPUTE) or are PROVIDER_WAIT rows and stay out; FOUND and
    NONE_FOUND settle a company and are not failures."""
    import linkedin_contacts
    since = _since(now)
    rows = reader.rows("SELECT run_id,company,started_at,state,finished_at,result FROM linkedin_contact_runs WHERE started_at>=? "
                       "AND state IN ('FAILED','COMPLETE')", (overview._prefilter(since),), "linkedin_contact_runs")
    key_of, settled_keys, tries = _finder_tries(reader) if rows else (lambda value, company: (None, company), set(), {})
    grouped = {}
    for row in _recent(rows, since, "started_at"):
        value = overview._json(row["result"]) or {}
        if row["state"] == "COMPLETE" and value.get("outcome") != "UNKNOWN":
            continue
        if row["state"] == "FAILED" and linkedin_contacts._provider_refused(row):
            continue   # the provider refused for usage: a wait
        key = key_of(value, row["company"])
        name = str(value.get("company") or row["company"])
        at = overview._utc(row["finished_at"]) or overview._utc(row["started_at"])
        entry = grouped.setdefault(key, {"name": name, "at": None, "count": 0, "runs": [],
                                         "causes": Causes(secrets, logs_href("launchd", at))})
        entry["count"] += 1
        entry["at"] = max(filter(None, (entry["at"], at)), default=None)
        entry["runs"].append((at, row["run_id"]))
        link = run_href("finder", row["run_id"])
        if row["state"] == "COMPLETE":
            reason = " ".join(str(part) for part in (value.get("reason"), ("Why research could not: %s" % value[
                "research_failure_diagnosis"]) if value.get("research_failure_diagnosis") else None) if part)
            entry["causes"].add("LINKEDIN_CONTACTS_UNKNOWN", at, reason, where=name, log_href=link)
        else:
            said = " ".join(str(part) for part in (value.get("error_type"), value.get("error"),
                                                    "at " + value["raised_at"] if value.get("raised_at") else None) if part)
            entry["causes"].add(value.get("blocker") or "LINKEDIN_CONTACTS_" + str(value.get("error_type") or "FAILED"), at,
                                said or None, where=name, log_href=link)
    found = []
    for key, entry in grouped.items():
        settled = key in settled_keys
        used = tries.get(key, 0) if key[0] is not None else entry["count"]   # no plan: the runtime never schedules it
        newest = max(entry["runs"], key=lambda pair: pair[0] or dt.datetime.min.replace(tzinfo=dt.timezone.utc))[1]
        if settled:
            continue   # a later run found people (or found nobody relevant): settled
        if used >= linkedin_contacts.MAX_FINDER_TRIES:
            entry["causes"].add("FINDER_RETRIES_EXHAUSTED", entry["at"], where=entry["name"])
            text = "Finding people to contact at %s failed %d of %d tries, so Sidedoor stopped trying." % (
                entry["name"], min(used, linkedin_contacts.MAX_FINDER_TRIES), linkedin_contacts.MAX_FINDER_TRIES)
            todo = "Nothing you need to do. The next research run that covers this company tries again."
        else:
            text = "Finding people to contact at %s didn't work (%d of %d tries used)." % (
                entry["name"], used, linkedin_contacts.MAX_FINDER_TRIES)
            todo = "Nothing you need to do. Sidedoor tries again on a later hourly round."
        found.append(_item("outreach", text, todo, entry["at"], entry["count"], code="FINDER_FAILED", reasons=entry["causes"],
                           refs={"finder_run_ids": [run for _, run in entry["runs"]], "company": key[1],
                                 "research_plan_id": key[0]}, log_href=run_href("finder", newest)))
    return found


# --- Notifications --------------------------------------------------------------------------------------------

def notification_failures(reader, now, fmt, secrets=()):
    since = _since(now)
    columns = {row[1] for row in reader.rows("PRAGMA table_info(notification_outbox)", (), "notification_outbox")}
    checks = "reconcile_attempts" if "reconcile_attempts" in columns else "0"
    transport = ",".join(("%s AS %s" % (name, name)) if name in columns else "NULL AS %s" % name
                         for name in ("send_error", "reconcile_error"))
    rows = reader.rows("SELECT state,next_at,last_error,attempts,updated_at," + transport + "," + checks + " AS checks FROM notification_outbox "
                       "WHERE state IN ('FAILED','UNCERTAIN') OR (state='PENDING' AND last_error IS NOT NULL AND attempts>0)",
                       (), "notification_outbox")
    words = ("notification", "notifications")
    found = []

    def causes(rows):
        found = Causes(secrets, logs_href("launchd"))
        for row in rows:
            # The transport's own code first (notifications._dispatch: BRIDGE_HTTP_400, RELAY_SEND_...).
            found.add(row["reconcile_error"] or row["send_error"] or row["last_error"] or row["state"],
                      overview._utc(row["updated_at"]))
        return found
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
                           max(filter(None, (overview._utc(row["updated_at"]) for row in failed)), default=None), len(failed),
                           code="NOTIFICATION_FAILED", reasons=causes(failed)))
    if unsure:
        found.append(_item("connections", "Sidedoor couldn't confirm whether %s arrived." % _plural(len(unsure), words),
                           "Nothing you need to do. Sidedoor checks before sending again, so nothing arrives twice.",
                           max(filter(None, (overview._utc(row["updated_at"]) for row in unsure)), default=None), len(unsure),
                           code="NOTIFICATION_UNCERTAIN", reasons=causes(unsure)))
    if stopped:
        found.append(_item("connections", "Sidedoor couldn't confirm whether %s arrived, and has stopped checking."
                           % _plural(len(stopped), words),
                           "Nothing you need to do. Sidedoor won't send %s again; anything that needs you also shows on "
                           "this dashboard." % ("it" if len(stopped) == 1 else "them"),
                           max(filter(None, (overview._utc(row["updated_at"]) for row in stopped)), default=None), len(stopped),
                           code="NOTIFICATION_UNCONFIRMED", reasons=causes(stopped)))
    if waiting:
        retry = min(filter(lambda moment: moment is not None and moment > now,
                           (overview._utc(row["next_at"]) if row["next_at"] else None for row in waiting)), default=None)
        found.append(_item("connections", "%s didn't go through yet." % _plural(len(waiting), words),
                           "Nothing you need to do. Sidedoor will try again %s." % (_at(fmt, retry) if retry else "on its own"),
                           max(filter(None, (overview._utc(row["updated_at"]) for row in waiting)), default=None),
                           len(waiting), retry, code="NOTIFICATION_RETRY", reasons=causes(waiting)))
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
    rows = reader.rows("SELECT code,source,first_seen,last_seen FROM browser_unavailable", (), "browser_unavailable")
    if rows and any(row["code"].endswith("_BROWSER_PROFILE_IN_USE") for row in rows):
        try:   # a record of "close the window" whose window is gone (Sidedoor's own leftover, now closed) is stale
            open_now = browser_runtime.display_state() == "PROFILE_IN_USE"
        except Exception:
            open_now = True
        if not open_now:
            rows = [row for row in rows if not row["code"].endswith("_BROWSER_PROFILE_IN_USE")]
    for row in rows:
        since = overview._utc(row["first_seen"])
        if since is None or now - since < BROWSER_UNAVAILABLE_AFTER:
            continue
        reason = row["code"].split("_BROWSER_", 1)[-1]
        if reason not in browser_runtime.UNAVAILABLE_WORDS:
            continue
        found.append(_item(BROWSER_GROUPS.get(row["source"], "other"), browser_runtime.UNAVAILABLE_WORDS[reason],
                           browser_runtime.UNAVAILABLE_TODO[reason] + " It started %s." % fmt(since),
                           overview._utc(row["last_seen"]) or since, you=True, detail=row["code"], code=row["code"]))
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


def codex_failures(home=None, finished=False):
    """No usable Codex (setup_steps.with_codex, 2026-10-23): AI work waits until Codex is found again."""
    import provider_limits
    found = provider_limits.codex_missing(home)
    if not found:
        return []
    words, label, href = provider_limits.codex_fix(finished)
    return [_item("scheduler", words, "Sidedoor's AI work waits, spending no tries, and carries on by itself once Codex "
                  "is found.", overview._utc(found.get("observed_at")), you=True, fix={"label": label, "href": href},
                  code="CODEX_NOT_FOUND")]


NEEDS_YOU = "needs-you"


def page_of(entries, number, size=LIST_LIMIT):
    """(entries, paging) for page `number` (clamped) of `size` each: paging is {page, pages, total}."""
    pages = max(1, -(-len(entries) // size))
    number = min(max(1, number or 1), pages)
    return entries[(number - 1) * size:number * size], {"page": number, "pages": pages, "total": len(entries)}


def gather(database, config, loop, now=None, log_tail=None, secrets=(), home=None, finished=False, section=None, page=1):
    """Everything the Failures page shows. `database` is a read-only connection or None (no file
    yet); `loop` is the scheduler loop's state (`overview_page.loop_state`); `log_tail(name)` returns
    the redacted tail of logs/<name>. A source that cannot be read marks its group unavailable and
    the rest still render. Each list shows its first LIST_LIMIT entries and how many more there are;
    `section` (a section key or NEEDS_YOU) shows page `page` of that list instead, with its paging."""
    now = now or dt.datetime.now(dt.timezone.utc)
    zone = ZoneInfo(config.get("timezone") or "America/Los_Angeles")
    fmt = lambda moment: overview.when(moment, now, zone)
    _ZONE.set(zone)
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

    add(task_failures, now, fmt, secrets, groups=("outreach", "applications", "connections", "other"))
    crashed = {slot for section in sections.values() for entry in section["entries"] if entry["job"] == "run"
               for slot in entry["slots"]}
    add(cycle_failures, now, crashed, groups=("scheduler",))
    add(research_failures, config, now, fmt, secrets, groups=("research",))
    add(application_failures, now, fmt, secrets, groups=("applications",))
    add(outreach_failures, now, secrets, groups=("outreach",))
    add(finder_failures, now, fmt, secrets, groups=("outreach",))
    add(notification_failures, now, fmt, secrets, groups=("connections",))
    add(browser_failures, now, fmt, groups=("applications", "outreach"))
    for entry in codex_failures(home, finished):
        sections[entry["group"]]["entries"].append(entry)
    explain_exits(sections, scheduler_failures(loop if isinstance(loop, dict) else {}, config, now, fmt, log_tail, secrets),
                  None if reader.broken else reader)
    if reader.broken:
        for section in sections.values():
            section["unavailable"] = section["key"] != "scheduler"
    oldest = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    newest_first = lambda entries: sorted(entries, key=lambda entry: entry["at"] or oldest, reverse=True)
    # What needs the user is listed once, under Needs you (Overview's needs-you link lands there).
    needs = newest_first(entry for section in sections.values() for entry in section["entries"] if entry["you"])
    focus = section
    for section in sections.values():
        section["entries"] = newest_first(entry for entry in section["entries"] if not entry["you"])
        section["total"] = len(section["entries"])
        section["entries"], section["paging"] = page_of(section["entries"], page if section["key"] == focus else 1)
        section["more"] = 0 if section["key"] == focus else max(0, section["total"] - LIST_LIMIT)
        section["paging"] = section["paging"] if section["key"] == focus else None
    needs_total = len(needs)
    needs, needs_paging = page_of(needs, page if focus == NEEDS_YOU else 1)
    ordered = [sections["scheduler"]] + [sections[key] for key, _ in GROUPS]
    shown = [section for section in ordered if section["entries"] or section["unavailable"]]
    return {"sections": shown, "needs_you": needs, "nothing": not shown and not needs, "unreadable": reader.broken,
            "when": fmt, "recent_days": RECENT_DAYS, "needs_paging": needs_paging if focus == NEEDS_YOU else None,
            "needs_more": 0 if focus == NEEDS_YOU else max(0, needs_total - LIST_LIMIT), "needs_total": needs_total}


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
    """The values never shown: the dashboard token, the license key, the OpenRouter key, the OpenAI API key (E0.T8), the Unipile
    token and DSN (`<home>/unipile.json`, `unipile_port.settings`, read only under the owner's
    `"linkedin_transport": "local-dev"`, RL1: a user's install goes through the relay and has no
    such file) and the HeyReach endpoint (`config["credential_config"]`, as `orchestrator.HeyReach`
    reads it), with their bare hosts. Reads only; a file that is missing or unreadable is skipped."""
    keys, hosts = [token], []
    home = Path(home) if home is not None else None
    paths = [home / "license.key", home / "openrouter.key", home / "dashboard-token"] if home is not None else []
    if home is not None:
        import credentials
        keys += credentials.values(home)   # E2.T4: the keys in the OS store too
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


def _read_bytes(path, end=None, limit=LOG_TAIL_BYTES):
    """(state, data, start, size): the bytes [start, end) of a regular file, `end` clamped to its size
    (its size when None) and start = end - limit (at least 0). Never follows a symlink and reads only a
    regular file; writes nothing. `end` only ever reaches lseek, never a path."""
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return "unreadable", b"", 0, 0   # a FIFO, device, directory or symlink is never opened (F14)
    except FileNotFoundError:
        return "missing", b"", 0, 0
    except OSError:
        return "unreadable", b"", 0, 0
    # O_NONBLOCK as well: a file swapped for a FIFO after the check still cannot block the page.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return "missing", b"", 0, 0
    except OSError:
        return "unreadable", b"", 0, 0
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return "unreadable", b"", 0, 0
        size = info.st_size
        end = size if end is None else min(max(0, int(end)), size)
        start = max(0, end - limit)
        os.lseek(descriptor, start, os.SEEK_SET)
        data = b""
        while len(data) < end - start:
            chunk = os.read(descriptor, end - start - len(data))
            if not chunk:
                break
            data += chunk
    except OSError:
        return "unreadable", b"", 0, 0
    finally:
        os.close(descriptor)
    return "ok", data, start, size


def read_tail(path, limit=LOG_TAIL_BYTES):
    """(state, text): "ok" with the last `limit` bytes (a partial first line dropped), "missing" or
    "unreadable". Never follows a symlink and reads only a regular file; writes nothing."""
    state, data, start, _ = _read_bytes(path, None, limit)
    if state != "ok":
        return state, ""
    text = data.decode("utf-8", "replace")
    if start > 0:
        text = text.split("\n", 1)[1] if "\n" in text else ""
    return "ok", text


def read_range(path, end=None, limit=LOG_TAIL_BYTES):
    """The whole lines of the `limit` bytes ending at byte `end` (the end of the file when None), as
    {"state", "lines": [(byte offset, text)], "start", "begin", "end", "size"}: a partial first line is
    dropped (as `read_tail` drops it) and so is a partial last line when `end` falls inside one, so a
    secret cut in two is never shown in halves. `begin` is where the first whole line starts (`start`
    when none does). Lines split as str.splitlines splits them, so each is the line `read_tail`'s text
    gives."""
    state, data, start, size = _read_bytes(path, end, limit)
    end = start + len(data)
    if state == "ok" and end < size and not data.endswith(b"\n"):
        data = data[:data.rfind(b"\n") + 1]
    begin = start
    if state == "ok" and start > 0:
        cut = data.find(b"\n")
        data, begin = (data[cut + 1:], start + cut + 1) if cut >= 0 else (b"", start)
    pieces = data.split(b"\n") if data else []
    if data.endswith(b"\n"):
        pieces.pop()
    lines, offset = [], begin
    for piece in pieces:
        for line in piece.decode("utf-8", "replace").splitlines() or [""]:
            lines.append((offset, line))
        offset += len(piece) + 1
    return {"state": state, "lines": lines, "start": start, "begin": begin, "end": end, "size": size}


OFFSET = re.compile(r"[0-9]{1,18}\Z")


def offset_of(value):
    """A byte offset from the query string (digits only, at most 18 of them), else None."""
    return int(value) if isinstance(value, str) and OFFSET.match(value) else None


def _redacted_pairs(pairs, secrets):
    """`redacted_lines` keeping each line's byte offset: [(offset, line)]."""
    import reply_worker
    fragments = _fragments(secrets)
    kept = []
    for offset, line in pairs:
        line = _ESCAPES.sub("", line).rstrip()
        if not line.strip():
            continue
        line = reply_worker.redact_line(line, secrets, fragments)
        if line is not None:
            kept.append((offset, line[:LINE_CHARACTERS]))
    return kept


def _shown(kept, limit, chunk):
    """(lines, older): the last `limit` of `kept` and the byte offset the next older chunk ends at,
    None once the start of the file is shown. Lines cut by `limit` are in that older chunk."""
    shown = kept[-limit:]
    older = chunk["begin"]
    if len(kept) > limit:
        older = next((offset for offset, _ in shown if offset is not None), older)
    if older >= chunk["end"]:
        older = chunk["start"]
    return [line for _, line in shown], (older if older > 0 else None)


def stream_chunk(path, secrets, end=None, limit=LOG_LINES):
    """One log stream's lines ending at byte `end` (its tail when None): {"state", "lines", "older"}."""
    chunk = read_range(path, end)
    if chunk["state"] != "ok":
        return {"state": chunk["state"], "lines": [], "older": None}
    lines, older = _shown(_redacted_pairs(chunk["lines"], secrets), limit, chunk)
    return {"state": "ok", "lines": lines, "older": older}


def redacted_lines(text, secrets, limit=LOG_LINES):
    return [line for _, line in _redacted_pairs(((None, line) for line in text.splitlines()), secrets)][-limit:]


def tail(logs, name, secrets, limit=LOG_LINES):
    """The redacted last lines of logs/<name>, or [] when it is missing or unreadable."""
    state, text = read_tail(Path(logs) / name)
    return redacted_lines(text, secrets, limit) if state == "ok" else []


def _choice(value, options, default="all"):
    return value if value in {key for key, _ in options} else default


_DAY = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


_STAMP = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))")


def _line_day(line, zone):
    match = _STAMP.match(line)
    if not match:
        return None
    try:
        return local_day(dt.datetime.fromisoformat(match.group(1).replace("Z", "+00:00")), zone)
    except ValueError:
        return None


def stream_id(name):
    """The page id of stream `name` ("research.stderr.log" -> "log-research-stderr")."""
    return "log-" + name[:-len(".log")].replace(".", "-")


def stream_names():
    return ["%s.%s.log" % (key, stream) for key, _ in COMPONENTS for stream, _ in LEVELS]


def filter_lines(lines, query="", day="", zone=None):
    """The lines holding `query` (case-insensitive) and, with `day`, stamped on that local day."""
    needle = (query or "").strip()[:SEARCH_CHARACTERS].casefold()
    day = day if isinstance(day, str) and _DAY.match(day) else ""
    if needle:
        lines = [line for line in lines if needle in line.casefold()]
    if day:   # the user's local date, as the page shows times (lines are stamped in UTC)
        lines = [line for line in lines if _line_day(line, zone) == day]
    return lines


def logs_view(logs, secrets, component="all", level="all", query="", day="", zone=None, older=None):
    """Every stream under logs/ (log_retention.STREAMS), filtered by component and level, searched
    case-insensitively in the redacted lines; `day` (YYYY-MM-DD, UTC as the lines are stamped) keeps
    only that day's lines. `older` (stream name, byte offset) shows that stream's chunk ending there
    instead of its tail. Each stream's `older` is where its next older chunk ends (None at the start
    of the file); search and day apply to the lines shown."""
    component, level = _choice(component, COMPONENTS), _choice(level, LEVELS)
    query = (query or "").strip()[:SEARCH_CHARACTERS]
    day = day if isinstance(day, str) and _DAY.match(day) else ""
    streams = []
    for key, label in COMPONENTS:
        if component not in ("all", key):
            continue
        for stream, level_label in LEVELS:
            if level not in ("all", stream):
                continue
            name = "%s.%s.log" % (key, stream)
            end = older[1] if older and older[0] == name else None
            found = stream_chunk(Path(logs) / name, secrets, end)
            lines = filter_lines(found["lines"], query, day, zone)
            streams.append({"name": name, "label": "%s: %s" % (label, level_label.lower()), "state": found["state"],
                            "lines": lines, "id": stream_id(name), "older": found["older"], "before": end})
    return {"streams": streams, "component": component, "level": level, "query": query, "day": day,
            "components": COMPONENTS, "levels": LEVELS, "tail_bytes": LOG_TAIL_BYTES, "line_limit": LOG_LINES}


# --- One run's own records and files (2026-10-23: "so users dont have to start running code") -----------

RUN_LINES = 80          # lines shown per file of a run
RUN_FILES = 12          # files per page of a run
RUN_EVENTS = 40         # records per page of a run
RUNS_LISTED = 30        # per component per page on the Runs list
# Files of a session folder that are shown: what the run reported and printed. Never the prompt or the
# request it was given (prompt.txt, request.json, schema.json), which carry the user's profile.
RUN_FILE_NAMES = ("stderr.log", "executor.log", "output.json", "decision.json", "trace.jsonl")
_RESEARCH_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# Any printable id (a job key can hold spaces and parentheses): task, finder and job ids only reach SQL
# as parameters. Ids that become part of a file path keep their strict checks in `run_view` (a research
# uuid and label, a dir: folder name).
_RUN = re.compile(r"(task|research|finder|job|dir):([^\x00-\x1f\x7f]{1,300})\Z")
_FOLDER_NAME = re.compile(r"[A-Za-z0-9._-]{1,120}\Z")
FOLDER_COMPONENTS = {"application-browser": "applications", "application-recovery": "applications",
                     "application-connection-manual": "applications", "email-check": "applications",
                     "linkedin-contacts": "launchd", "outreach-history": "launchd"}
FOLDER_WORDS = {"application-browser": "Browser session", "application-recovery": "Application recovery decision",
                "application-connection-manual": "Manual check", "email-check": "Mailbox check",
                "linkedin-contacts": "Contact finder run", "outreach-history": "Outreach history check"}


# Per-session browser traces (owner, 2026-10-24): not listed under Runs. Each stays reachable from its
# own run: an application's run view (job:) shows its last browser sessions, a contact finder run's
# view (finder:) its session folder, and the dir: route still opens one.
BROWSER_SESSION_FOLDERS = {"application-browser", "linkedin-contacts"}


def _inside(path, base):
    """True when `path` is a real (not linked) entry directly or deeper inside `base`."""
    try:
        return not path.is_symlink() and path.resolve().is_relative_to(base.resolve()) and path.resolve() != base.resolve()
    except (OSError, ValueError):
        return False


PROMPT_WITHHELD = "(The prompt Sidedoor sent and the AI's working are not shown: they hold your profile.)"


def codex_safe(text, truncated):
    """A codex run's printed log without its prompt echo and transcript (as `reply_worker._redacted_lines`
    keeps its stderr): the header before codex's "user" marker, then only codex's own ERROR/WARN lines
    (`reply_worker._CODEX_LEVEL`). A tail that began past the header (`truncated`) and shows no marker
    may be inside the echo, so only those level lines are kept from it too. Other text is unchanged."""
    pairs = [(None, line) for line in text.splitlines()]
    kept = codex_pairs(pairs, truncated)
    return text if kept is pairs else "\n".join(line for _, line in kept)


def codex_pairs(pairs, truncated):
    """`codex_safe` over [(byte offset, line)], keeping each kept line's offset (None for a note);
    returns `pairs` itself when it keeps everything."""
    import reply_worker
    lines = [line for _, line in pairs]
    marker = next((index for index, line in enumerate(lines) if line.strip() == "user"), None)
    gap = next((index for index, line in enumerate(lines) if line.strip() == GAP), None)
    if marker is not None and gap is not None and marker > gap:
        marker = None   # the header's own marker is not in the head: nothing before it is a known header
    if marker is None and not truncated:
        return pairs
    head = pairs[:marker] if marker is not None else []
    rest = pairs[marker or 0:]
    # Inside the echoed prompt (after "user" until codex's own "thinking"/"codex"/"exec" marker; and a
    # truncated tail with no marker, which may be inside it) only codex's ERROR:/WARN: lines are kept:
    # the prompt holds Sidedoor's instructions and the user's own text, some lines starting "Error".
    kept, transcript, echo = [], False, True
    for index, (offset, line) in enumerate(rest):
        bare = line.strip()
        if bare == GAP:
            kept.append((offset, line))
            transcript, echo = False, True      # the tail may begin anywhere: inside the echo until a marker
            continue
        if bare in reply_worker._TRANSCRIPT_MARKERS:
            transcript, echo = True, False      # the model's own text follows: never kept
        elif bare == "exec":
            transcript, echo = False, False
        elif bare == "user":
            transcript, echo = False, True
        if reply_worker._CODEX_LEVEL.match(bare):
            kept.append((offset, line))
        elif not echo and _CAUSE_LINE.match(bare) and (not transcript or index >= len(rest) - 3):
            kept.append((offset, line))      # "Error: ...", a fatal or exit line: codex's own cause, or its last words
    return head + [(None, PROMPT_WITHHELD)] + kept


_CAUSE_LINE = re.compile(r"^(?:Error|error|fatal|Fatal|FATAL|panicked|thread '[^']*' panicked)\b[:\s]|^(?:exit|Exit|exited)\b")
TRACE_NOTE = "(Only this session's error events are shown: its reasoning, messages and tool calls hold your details.)"


def trace_errors(text):
    """A codex --json trace's error events only (type error, turn.failed, or an item that failed), each
    "<type>: <message>"; reasoning, agent messages and tool arguments and results are dropped."""
    return "\n".join(line for _, line in trace_pairs([(None, line) for line in text.splitlines()]))


def trace_pairs(pairs):
    """`trace_errors` over [(byte offset, line)], each kept event with its line's offset."""
    kept = [(None, TRACE_NOTE)]
    for offset, line in pairs:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = str(event.get("type") or "")
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        message = None
        if kind == "error":
            message = event.get("message")
        elif kind == "turn.failed":
            message = (event.get("error") or {}).get("message") if isinstance(event.get("error"), dict) else event.get("error")
        elif item and (str(item.get("status") or "").lower() in ("failed", "error") or item.get("error")):
            error = item.get("error")
            message = (error.get("message") if isinstance(error, dict) else error) or "failed (%s)" % item.get("type")
            kind = "%s %s" % (kind, item.get("type") or "item")
        if message is not None:
            kept.append((offset, "%s: %s" % (kind, str(message)[:ERROR_CHARACTERS])))
    return kept


HEAD_BYTES = 4096
GAP = "(... the middle of this file is not shown ...)"


def read_head(path, limit=HEAD_BYTES):
    """The first `limit` bytes of a regular file (never a link), up to its last whole line, else ""."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return ""
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return ""
        data = os.read(descriptor, limit)
    except OSError:
        return ""
    finally:
        os.close(descriptor)
    text = data.decode("utf-8", "replace")
    return text.rsplit("\n", 1)[0] if len(data) >= limit and "\n" in text else text


def _file(path, secrets, title=None, older=None):
    """One file of a run: its redacted last RUN_LINES lines, or, when `older` is (its name, a byte offset),
    the chunk ending there. A .log is a codex log: its prompt echo and transcript never show
    (`codex_pairs`); an older chunk that does not begin the file may begin inside the echo, so it is
    read as a truncated tail is (only codex's own level lines until a marker says otherwise)."""
    name = title or path.name
    end = older[1] if older and older[0] == name else None
    chunk = read_range(path, end)
    state, pairs = chunk["state"], chunk["lines"]
    if state == "ok" and path.name == "trace.jsonl":
        pairs = trace_pairs(pairs)
    elif state == "ok" and path.suffix == ".log":   # research step logs, a session's stderr / executor log
        truncated = chunk["start"] > 0
        if end is not None:
            # An older chunk: never a head. One that starts mid-file starts in an unknown place, which the
            # gap line marks as inside the echo; the gap itself is not shown.
            kept = codex_pairs([(None, GAP)] + pairs if truncated else pairs, True)
            pairs = [(offset, line) for offset, line in kept if not (offset is None and line == GAP)]
        else:
            if truncated:   # codex's header is at the start: a bounded head, the gap, then the tail
                pairs = [(None, line) for line in read_head(path).splitlines()] + [(None, GAP)] + pairs
            pairs = codex_pairs(pairs, truncated)
    try:
        at = dt.datetime.fromtimestamp(os.lstat(path).st_mtime, dt.timezone.utc)
    except OSError:
        at = None
    lines, older_end = _shown(_redacted_pairs(pairs, secrets), RUN_LINES, chunk) if state == "ok" else ([], None)
    return {"name": name, "state": state, "at": at, "lines": lines, "older": older_end, "before": end}


def _folder_files(folder, secrets, older=None):
    return [_file(folder / name, secrets, folder.name + "/" + name, older) for name in RUN_FILE_NAMES
            if (folder / name).is_file() and not (folder / name).is_symlink()]


def _folder_of(root, value, base):
    """A session folder a stored result names (`directory` or an error's `trace_path`), inside reports/<base>."""
    folder_root = paths.reports(root) / base
    for candidate in (value.get("directory"), (value.get("error") or {}).get("trace_path") if isinstance(value.get("error"), dict) else None,
                      value.get("trace_path")):
        if isinstance(candidate, str) and candidate:
            try:
                path = Path(paths.relocated(candidate))
            except Exception:
                path = Path(candidate)
            path = path if path.is_dir() else path.parent
            if _inside(path, folder_root):
                return path
    return None


def run_view(reader, root, logs, run, secrets, fmt=str, older=None, records_page=1, files_page=1):
    """What one run recorded, for a failure's "See its log": its records in the database (task events,
    application events, a finder run's result) and its own files (research step logs and reports, a
    session folder's stderr / executor log / output / trace tail), redacted, bounded, newest first, plus
    the stream lines tagged with it (`log.tagged`). None for a value that is not a run reference.
    `older` (a file's shown name, a byte offset) shows that file's chunk ending there (`_file`). Records
    show RUN_EVENTS and files RUN_FILES a page (`records_page`, `files_page`, with their paging)."""
    import log as logging_module
    match = _RUN.match(run or "")
    if not match:
        return None
    kind, ident = match.groups()
    records, files, title, tag = [], [], None, None

    def record(at, *parts):
        records.append({"at": overview._utc(at) if isinstance(at, str) else at,
                        "text": redact_text(" ".join(str(part) for part in parts if part not in (None, "")), secrets)[:LINE_CHARACTERS]})

    if kind == "task":
        tag = logging_module.run_tag("task", ident)
        task = reader.rows("SELECT worker,status,blocker,result FROM workflow_tasks WHERE task_id=?", (ident,), "workflow_tasks")
        title = "%s (task %s)" % (worker_words(task[0]["worker"])[1], ident[:12]) if task else "Task %s" % ident[:12]
        for event in reader.rows("SELECT at,event,detail FROM workflow_task_events WHERE task_id=? ORDER BY id DESC",
                                 (ident,), "workflow_task_events"):
            detail = overview._json(event["detail"]) or {}
            result = detail.get("result") if isinstance(detail.get("result"), dict) else detail
            record(event["at"], event["event"], detail.get("blocker") or _result_code(result, None), _error_text(result))
        if task:
            failure = (overview._json(task[0]["result"]) or {}).get("executor_failure")
            for line in (failure or {}).get("stderr_tail") or []:
                record(None, "AI call stderr:", line)
    elif kind == "research":
        attempt, _, label = ident.partition(":")
        if not _RESEARCH_ID.fullmatch(attempt) or (label and not re.fullmatch(r"[a-z0-9-]{1,80}", label)):
            return None   # not a research run id (daily_schedule.claim: a uuid4): "not found"
        tag = logging_module.run_tag("research", attempt)
        title = "Research run %s%s" % (attempt[:8], " (%s)" % label.rsplit("-", 1)[0] if label else "")
        prefix = attempt + "." + (label + "." if label else "")
        for folder, suffix in ((Path(logs), ".log"), (paths.reports(root), ".json")):
            try:
                names = sorted(entry for entry in os.listdir(folder) if entry.startswith(prefix) and entry.endswith(suffix))
            except OSError:
                names = []
            files += [_file(folder / name, secrets, None, older) for name in names if not (folder / name).is_symlink()]
        row = reader.rows("SELECT started_at,state,result FROM research WHERE id=?", (attempt,), "research")
        if row:
            result = overview._json(row[0]["result"]) or {}
            record(row[0]["started_at"], "Run", row[0]["state"], result.get("error"))
            industries = result.get("industries")
            for industry in industries if isinstance(industries, list) else []:
                if isinstance(industry, dict) and (not label or _research_label(str(industry.get("industry"))) == label):
                    record(None, industry.get("industry"), industry.get("state"), industry.get("abandonment_reason"),
                           industry.get("error"))
    elif kind == "finder":
        tag = logging_module.run_tag("finder", ident)
        row = reader.rows("SELECT company,started_at,state,result FROM linkedin_contact_runs WHERE run_id=?", (ident,),
                          "linkedin_contact_runs")
        value = overview._json(row[0]["result"]) if row else None
        title = "Contact finder run at %s" % ((value or {}).get("company") or (row[0]["company"] if row else ident[:12]))
        if row:
            value = value or {}
            record(row[0]["started_at"], row[0]["state"], value.get("outcome"), value.get("blocker"), value.get("error"),
                   value.get("reason"))
            if value.get("research_failure_diagnosis"):
                record(None, "Why research could not:", value["research_failure_diagnosis"])
            folder = _folder_of(root, value, "linkedin-contacts")
            files += _folder_files(folder, secrets, older) if folder else []
    elif kind == "job":
        tag = logging_module.run_tag("job", ident)
        title = "Application %s" % ident
        for event in reader.rows("SELECT at,event,detail FROM application_events WHERE job_key=? ORDER BY id DESC",
                                 (ident,), "application_events"):
            detail = overview._json(event["detail"]) or {}
            record(event["at"], event["event"], detail.get("code"), _error_text(detail))
        for question, why, moment in answering_causes(reader, ident):
            record(moment, "Question still open:", question, "-", why)
        for request in reader.rows("SELECT request_id,operation,state,created_at,result FROM desktop_requests WHERE job_key=? "
                                   "ORDER BY created_at DESC LIMIT 3", (ident,), "desktop_requests"):
            value = overview._json(request["result"]) or {}
            record(request["created_at"], "Browser %s request %s:" % (request["operation"], request["request_id"][:12]),
                   request["state"], _result_code(value.get("error") if isinstance(value.get("error"), dict) else value, None))
            folder = _folder_of(root, value, "application-browser")
            files += _folder_files(folder, secrets, older) if folder else []
    else:
        base, _, name = ident.partition("/")
        if base not in FOLDER_WORDS or not _FOLDER_NAME.match(name) or name in (".", ".."):
            return None
        folder = paths.reports(root) / base / name
        if not _inside(folder, paths.reports(root) / base) or not folder.is_dir():
            return None
        title = "%s %s" % (FOLDER_WORDS[base], name)
        files = _folder_files(folder, secrets, older)
    oldest = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    files, files_paging = page_of(sorted(files, key=lambda entry: entry["at"] or oldest, reverse=True), files_page, RUN_FILES)
    records, records_paging = page_of(records, records_page, RUN_EVENTS)
    tagged = []
    if tag:
        for stream in log_retention_streams():
            state, text = read_tail(Path(logs) / stream)
            tagged += [stream + ": " + line for line in (redacted_lines(text, secrets) if state == "ok" else [])
                       if "[" + tag + "]" in line]
    return {"run": run, "title": title, "records": records, "files": files, "tagged": tagged[-LOG_LINES:], "tag": tag,
            "records_paging": records_paging, "files_paging": files_paging,
            "when": fmt}


def log_retention_streams():
    import log_retention
    return log_retention.STREAMS


def runs_list(root, logs, component="all", day="", zone=None):
    """The per-run files retention keeps, newest first, by component: research runs (their step logs)
    and session folders (log_retention.TRACE_DIRECTORIES). Each opens its `run_view`. The first
    RUNS_LISTED of each component (`runs_paged` pages them)."""
    return runs_paged(root, logs, component, day, zone)[0]


def runs_paged(root, logs, component="all", day="", zone=None, part=None, page=1):
    """(runs, parts): each component's first RUNS_LISTED runs, or page `page` of component `part`;
    parts is [{component, label, more, paging}] for each component with more than one page."""
    import log_retention
    found = []
    try:
        names = os.listdir(logs)
    except OSError:
        names = []
    research = {}
    for name in names:
        if log_retention.WORKER_NAME.fullmatch(name) and not (Path(logs) / name).is_symlink():
            try:
                at = dt.datetime.fromtimestamp(os.lstat(Path(logs) / name).st_mtime, dt.timezone.utc)
            except OSError:
                continue
            attempt = name[:36]
            research[attempt] = max(filter(None, (research.get(attempt), at)))
    found += [{"component": "research", "label": "Research run %s" % attempt[:8], "at": at,
               "href": run_href("research", attempt)} for attempt, at in research.items()]
    for base, pattern in log_retention.TRACE_DIRECTORIES:
        folder = paths.reports(root) / base
        if base in BROWSER_SESSION_FOLDERS or base not in FOLDER_WORDS or not folder.is_dir() or folder.is_symlink():
            continue
        for entry in folder.glob(pattern):
            if entry.is_symlink() or not entry.is_dir():
                continue
            try:
                at = dt.datetime.fromtimestamp(entry.lstat().st_mtime, dt.timezone.utc)
            except OSError:
                continue
            found.append({"component": FOLDER_COMPONENTS[base], "label": "%s %s" % (FOLDER_WORDS[base], entry.name),
                          "at": at, "href": run_href("dir", base + "/" + entry.name)})
    if component != "all":
        found = [entry for entry in found if entry["component"] == component]
    if isinstance(day, str) and _DAY.match(day):
        found = [entry for entry in found if local_day(entry["at"], zone) == day]
    found.sort(key=lambda entry: entry["at"], reverse=True)
    labels = dict(COMPONENTS)
    by_part = {}
    for entry in found:
        entry["component_label"] = labels.get(entry["component"], entry["component"])
        by_part.setdefault(entry["component"], []).append(entry)
    kept, parts = [], []
    for key, entries in by_part.items():
        shown, paging = page_of(entries, page if key == part else 1, RUNS_LISTED)
        kept += shown
        if paging["pages"] > 1:
            parts.append({"component": key, "label": labels.get(key, key), "paging": paging if key == part else None,
                          "more": 0 if key == part else paging["total"] - RUNS_LISTED})
    kept.sort(key=lambda entry: entry["at"], reverse=True)
    return kept, parts


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

DETAILS_HIDDEN = ("Failure details are hidden while your keychain is locked, so no saved key can show by "
                  "accident. They come back once it unlocks.")


def _redaction_complete(home):
    """A4: False while the keychain is waiting and a saved key's value is not known to this process."""
    try:
        import credentials
        return credentials.redaction_complete(home)
    except Exception:
        return False


KEPT_PARAMS = ("component", "level", "q", "day", "run", "section", "page", "log", "before", "file", "file_before",
               "records_page", "files_page", "runs_part", "runs_page")
PAGE_NUMBER = re.compile(r"[0-9]{1,6}\Z")


def href(params, path="/failures", anchor="", **changes):
    """A Failures link keeping the page's own choices (`KEPT_PARAMS` only: never the dashboard token)
    with `changes` applied (None removes one)."""
    values = {key: str(params.get(key)) for key in KEPT_PARAMS if params.get(key) not in (None, "")}
    for key, value in changes.items():
        if value is None:
            values.pop(key, None)
        else:
            values[key] = str(value)
    for key in ("component", "level"):
        if values.get(key) == "all":
            values.pop(key)
    query = urllib.parse.urlencode(values)
    return path + ("?" + query if query else "") + ("#" + anchor if anchor else "")


def older_links(entry, params, anchor, file=None):
    """A stream's (or, with `file`, a run file's) Older lines links: the full page without JS, the
    chunk alone for htmx, and the way back to the newest lines."""
    if file is None:
        changes, back = {"log": entry["name"], "before": entry["older"]}, {"log": None, "before": None}
    else:
        changes, back = {"file": file, "file_before": entry["older"]}, {"file": None, "file_before": None}
    entry["older_href"] = href(params, anchor=anchor, **changes) if entry["older"] is not None else None
    entry["older_hx"] = href(params, "/failures/older", **changes) if entry["older"] is not None else None
    entry["newest_href"] = href(params, anchor=anchor, **back)
    return entry


def register(app, templates, layout, home, root, config, token, database, connect_read_only):
    """GET /failures and GET /failures/logs.txt. Both read only: the database through
    `overview_page.snapshot`, the logs through `read_tail`."""
    from fastapi import Request
    from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
    import setup_steps

    logs = paths.logs(root)

    def choices(request):
        params = request.query_params
        stream = params.get("log") if params.get("log") in stream_names() else None
        before = offset_of(params.get("before")) if stream else None
        file = params.get("file") if isinstance(params.get("file"), str) and params.get("file") else None
        file_before = offset_of(params.get("file_before")) if file else None
        return params, (stream, before) if before is not None else None, (file, file_before) if file_before is not None else None

    def number(params, name):
        value = params.get(name)
        return int(value) if isinstance(value, str) and PAGE_NUMBER.match(value) else 1

    def find_run(run, secrets, older=None, params=None):
        zone = ZoneInfo(config.get("timezone") or "America/Los_Angeles")
        now = dt.datetime.now(dt.timezone.utc)
        params = params or {}
        found = overview.snapshot(database, lambda connection: run_view(
            overview.Reader(connection), root, logs, run, secrets, lambda moment: overview.when(moment, now, zone), older,
            number(params, "records_page"), number(params, "files_page")),
            connect_read_only)
        return found if isinstance(found, dict) else None

    @app.get("/failures", name="failures")
    def failures(request: Request, component: str = "all", level: str = "all", q: str = "", day: str = "", run: str = "",
                 section: str = "", page: str = "1"):
        context = layout("failures", "Failures and logs")
        if not _redaction_complete(home):
            context["hidden"] = DETAILS_HIDDEN
            return templates.TemplateResponse(request, "failures.html", context)
        params, stream_older, file_older = choices(request)
        secrets = secrets_of(home, config, token)
        loop = overview.loop_state(setup_steps.scheduler_running(home))
        focus = section if section in {key for key, _ in GROUPS} | {"scheduler", NEEDS_YOU} else None
        section_page = int(page) if isinstance(page, str) and PAGE_NUMBER.match(page) else 1
        context["view"] = overview.snapshot(
            database, lambda connection: gather(connection, config, loop, secrets=secrets, home=home,
                                                finished=setup_steps.progress(home)["finished"],
                                                log_tail=lambda name: tail(logs, name, secrets, JOB_LOG_LINES),
                                                section=focus, page=section_page),
            connect_read_only)
        context["link"] = lambda anchor="", changes=None, more=None, **kw: href(
            params, anchor=anchor, **{**(changes or {}), **(more or {}), **kw})
        zone = ZoneInfo(config.get("timezone") or "America/Los_Angeles")
        context["logs"] = logs_view(logs, secrets, component, level, q, day, zone, stream_older)
        for stream in context["logs"]["streams"]:
            older_links(stream, params, stream["id"])
        context["runs"], context["runs_parts"] = runs_paged(
            root, logs, context["logs"]["component"], context["logs"]["day"], zone, params.get("runs_part"),
            number(params, "runs_page"))
        if run:
            found = find_run(run, secrets, file_older, params)
            for entry in (found or {}).get("files") or []:
                older_links(entry, params, "run", file=entry["name"])
            context["run_view"] = found or {"run": run, "missing": True}
        return templates.TemplateResponse(request, "failures.html", context)

    @app.get("/failures/older")
    def failures_older(request: Request, q: str = "", day: str = "", run: str = ""):
        """The older chunk of one stream (log, before) or one run file (run, file, file_before), for htmx:
        the next Older lines control and the chunk's lines, to replace the control it came from. Without
        htmx, the full page showing that chunk."""
        params, stream_older, file_older = choices(request)
        if request.headers.get("hx-request") != "true":
            return RedirectResponse(href(params, anchor="run" if file_older else (
                stream_id(stream_older[0]) if stream_older else "logs")), status_code=303)
        if not _redaction_complete(home):
            return HTMLResponse(templates.get_template("failures_older.html").render(fragment={"note": DETAILS_HIDDEN}))
        secrets = secrets_of(home, config, token)
        entry = None
        if stream_older:
            entry = stream_chunk(logs / stream_older[0], secrets, stream_older[1])
            entry.update(name=stream_older[0])
            matched = filter_lines(entry["lines"], q, day, ZoneInfo(config.get("timezone") or "America/Los_Angeles"))
            if entry["lines"] and not matched:
                entry["note"] = "No matching lines in this older part."
            entry["lines"] = matched
            older_links(entry, params, stream_id(stream_older[0]))
        elif file_older and run:
            found = find_run(run, secrets, file_older, params)
            entry = next((item for item in (found or {}).get("files") or [] if item["name"] == file_older[0]), None)
            if entry is not None:
                older_links(entry, params, "run", file=entry["name"])
        if entry is None:
            entry = {"note": "Nothing older can be shown here."}
        elif not entry["lines"] and not entry.get("note"):
            entry["note"] = "Nothing in this older part can be shown."
        return HTMLResponse(templates.get_template("failures_older.html").render(fragment=entry))

    @app.get("/failures/logs.txt")
    def failures_download(request: Request):
        text = DETAILS_HIDDEN + "\n" if not _redaction_complete(home) else download_text(logs, secrets_of(home, config, token))
        return PlainTextResponse(text, headers={"Content-Disposition": 'attachment; filename="sidedoor-logs.txt"'})
