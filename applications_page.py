"""What the Applications page shows (E6.T6; E6.T0 spec section 8). Display only, like Overview
(overview_page): every line is a record the application pipeline already wrote, read through
`overview_page.snapshot` (no -wal/-shm, nothing written), and every verdict shown is one the AI or
the pipeline already made. Nothing here judges, writes, or calls the network or a model.

Sources (why each is the record the product itself treats as that fact):
- the job list: `application_jobs` (one row per posting: state, blocker, next_at, receipt,
  updated_at). Company, role and link come from its `payload` (the role `ingest` recorded).
- buckets: the same code Overview counts with. "Blocked, needs you" is `overview_page._blocked`
  (BLOCKED jobs split by `applications.job_route`, plus the spent SUBMISSION_OUTCOME_UNKNOWN test,
  O19); "Waiting to retry" is `overview_page.retrying_keys` (every job whose badge says so: RETRY_WAIT
  not being read now, plus `_blocked`'s automatic waits); "Queued" is QUEUED only; "Waiting for confirmation" is SUBMITTING/RECONCILE
  (`applications.UNCERTAIN`); "Submitted today" is `application_events` SUBMISSION_VERIFIED in the
  service day (`overview_page.submitted_today`'s rows).
- Submitted: only state SUBMITTED, which `applications.mark_submitted` alone sets, with a verified
  receipt (a page confirmation the armed Submit captured, an employer confirmation email, the
  candidate's own sent application email, or an applicant-account record). A submit that was only
  acknowledged stays SUBMITTING/RECONCILE and shows as "Waiting for confirmation", never Submitted.
- reading: a QUEUED or held job whose inspect/investigate request is CLAIMED with a live lease
  (`desktop_requests`, the browser session reading it now).
- questions for you: `user_decisions` PENDING (`overview_page._pending_questions`).
- attempts: `application_attempts` (one row per submit attempt: started_at, state; its evidence holds
  the answers used and the résumé hash). History: `application_events` (state changes with their
  code, the eligibility check, the verified submission).
- eligibility verdict: the latest `application_reviews` row (decision code, the AI's scope_fit
  reasons).
- closing reasons: the AI's own words where it gave them: the scope verdicts of the review, the
  same-posting judge's reason (SAME_POSTING_LINKED), the recovery coordinator's reason
  (APPLICATION_ABANDONED).

Codes never show: each stored code has plain words here, and an unknown one shows words chosen by
where the job goes next (its route). Indexes these reads use are created by the writers' schema setup
(applications.initialize, desktop_adapter.initialize, decision_transport.initialize).
"""

import datetime as dt
import json
import math
import re
import urllib.parse
from pathlib import Path
from zoneinfo import ZoneInfo

import decision_transport
import overview_page as overview
import workflow_tasks


PAGE_SIZE = 50
SEARCH_LIMIT = 20000
UNAVAILABLE = overview.UNAVAILABLE

# Filters: key, label. The first five are Overview's buckets (same classification code).
FILTERS = (("all", "All"),
           ("needs_you", "Blocked, needs you"),
           ("retrying", "Waiting to retry"),
           ("confirming", "Waiting for confirmation"),
           ("submitted_today", "Submitted today"),
           ("submitted", "Submitted"),
           ("queued", "Queued"),
           ("waiting_browser", "Waiting for the browser"),
           ("reading", "Reading"),
           ("ineligible", "Not eligible"),
           ("duplicate", "Duplicate"),
           ("given_up", "Given up"))
FILTER_KEYS = {key for key, _ in FILTERS}
STATE_FILTERS = {"confirming": ("SUBMITTING", "RECONCILE"),
                 "submitted": ("SUBMITTED",),
                 "queued": ("QUEUED",),
                 "waiting_browser": ("WAITING_BROWSER",),
                 "ineligible": ("INELIGIBLE",),
                 "duplicate": ("DUPLICATE",),
                 "given_up": ("EXHAUSTED", "ABANDONED")}
READ_OPERATIONS = ("inspect", "investigate")
# Filters by state whose jobs badge "Reading now" instead while a browser reads them (`job_view`).
LIVE_STATE_FILTERS = ("confirming", "queued", "waiting_browser")

# Every state applications.py writes (QUEUED, BLOCKED, RETRY_WAIT, WAITING_BROWSER, SUBMITTING, RECONCILE
# and the five final ones) has words here or in job_view (BLOCKED), and a filter.
STATE_WORDS = {"QUEUED": "Queued", "RETRY_WAIT": "Waiting to retry", "WAITING_BROWSER": "Waiting for the browser",
               "SUBMITTING": "Waiting for confirmation",
               "RECONCILE": "Waiting for confirmation", "SUBMITTED": "Submitted", "INELIGIBLE": "Not eligible",
               "DUPLICATE": "Duplicate", "EXHAUSTED": "Given up", "ABANDONED": "Given up"}
STATE_TONES = {"SUBMITTED": "success", "SUBMITTING": "info", "RECONCILE": "info", "QUEUED": "muted",
               "RETRY_WAIT": "warning", "WAITING_BROWSER": "info", "INELIGIBLE": "muted", "DUPLICATE": "muted", "EXHAUSTED": "muted",
               "ABANDONED": "muted"}
FINAL = {"SUBMITTED", "DUPLICATE", "INELIGIBLE", "EXHAUSTED", "ABANDONED"}

# Plain words for stored codes (the part before ":" for codes that carry fields).
CODE_WORDS = {
    # Waits Sidedoor handles itself
    "AUTHENTICATION_REQUIRED": "The application site asked for a sign-in.",
    "CAPTCHA_REQUIRED": "The application site asked for a human check.",
    "MFA_REQUIRED": "The application site asked for a sign-in code.",
    "FILE_UPLOAD_FAILED": "The site did not take the résumé upload.",
    "RESUME_UPLOAD_FAILED": "The site did not take the résumé upload.",
    "APPLICATION_BROWSER_SHARED_BUDGET_EXHAUSTED": "Today's compute budget ran out.",
    "APPLICATION_BROWSER_BUSY": "The browser was busy with other work.",
    "APPLICATION_BROWSER_RUNTIME_MISSING": "Sidedoor's browser is not installed on this computer. Reinstall Sidedoor; it tries again on its own.",
    "APPLICATION_BROWSER_PROFILE_IN_USE": "Sidedoor's browser window was open, so it could not start its own. It tries again once that window is closed.",
    "LINKEDIN_CONTACTS_BROWSER_RUNTIME_MISSING": "Sidedoor's browser is not installed on this computer. Reinstall Sidedoor; it tries again on its own.",
    "LINKEDIN_CONTACTS_BROWSER_PROFILE_IN_USE": "Sidedoor's browser window was open, so it could not start its own. It tries again once that window is closed.",
    "DESKTOP_BROWSER_PENDING": "The browser has not picked this up yet.",
    "PROVIDER_USAGE_LIMIT_WAIT": "The AI provider's usage limit was reached until its reset.",
    "ANSWERING_RETRY": "Some form answers still need to be written.",
    "SAME_POSTING_CHECK_PENDING": "Sidedoor is checking that this is not a posting you already applied to.",
    "SAME_POSTING_AWAITING_SETTLEMENT": "It looks like the same posting as another application that is still being confirmed.",
    "EMAIL_APPLICATION_ADDRESS_UNVERIFIED": "The posting's application email address could not be confirmed.",
    "REQUISITION_MISMATCH": "The application page did not match this posting.",
    "REQUISITION_UNKNOWN": "The posting's job number could not be confirmed.",
    "IDENTITY_MISMATCH": "The posting moved to another page; Sidedoor is following it.",
    "INSPECTION_INCOMPLETE": "Sidedoor could not finish reading the posting.",
    "FORM_REVIEW_INCOMPLETE": "Sidedoor could not finish reading the application form.",
    "REQUIRED_QUESTIONS_UNKNOWN": "Sidedoor could not read all of the form's questions.",
    "MALFORMED_OR_UNAVAILABLE_EVIDENCE": "The page could not be read properly.",
    "ELIGIBILITY_UNVERIFIED": "Sidedoor is still checking whether the role fits your search.",
    "ELIGIBILITY_UNKNOWN": "Sidedoor is still checking whether the role fits your search.",
    "EMPLOYEE_COUNT_UNVERIFIED": "Sidedoor is still checking the company's size.",
    "SALARY_UNKNOWN": "Sidedoor is still checking the pay.",
    "CROSSING_BASE_BAND_UNRESOLVED": "Sidedoor is still checking the pay.",
    "UNSUPPORTED_OFFICIAL_DESTINATION": "Sidedoor is looking for the employer's own application page.",
    "APPLICATION_DESTINATION_OR_SCOPE_UNKNOWN": "Sidedoor is looking for the employer's own application page.",
    "APPLICATION_OFFICIAL_DESTINATION_REQUIRED": "Sidedoor is looking for the employer's own application page.",
    "APPLICATION_OFFICIAL_IDENTITY_REQUIRED": "Sidedoor is confirming which posting this is.",
    "APPLICATION_HISTORY_UNKNOWN": "Sidedoor is checking whether you applied here before.",
    "PRIOR_PROCESS_REQUISITION_UNRESOLVED": "Sidedoor is checking whether you applied here before.",
    "PRIOR_APPLICATION_IDENTITY_UNRESOLVED": "Sidedoor is checking whether you applied here before.",
    "APPLICATION_BROWSER_INVESTIGATION_EXHAUSTED": "Sidedoor ran out of tries reading this posting.",
    "BROWSER_CAPABILITY_UNAVAILABLE": "A browser tool Sidedoor needs was not available.",
    "CONDITIONAL_QUESTION_UNANSWERED": "The form asked a question the prepared answers did not cover.",
    "INVALID_SUPPLIED_ANSWER": "The application site didn't accept one of the answers.",
    "BROWSER_INTERACTION_FAILED": "The browser could not work the application form.",
    "FIELD_LIMIT_TOO_SHORT_FOR_FACT": "A form field was too short for one of your details.",
    "PREFLIGHT_TRANSIENT": "A temporary problem came up before submitting.",
    "SUBMISSION_OUTCOME_UNKNOWN": "Sidedoor could not confirm whether the application went through.",
    "SUBMISSION_REPORTED_FAILED": "The application page reported a problem after Submit.",
    "SUBMISSION_OBSERVATION_LOST": "Sidedoor lost track of the submit before seeing the result.",
    # Tries that failed and are retried (owner, 2026-10-25: counted on Failures)
    "APPLICATION_BROWSER_EXECUTOR_FAILED": "The AI running the browser stopped with an error before it finished.",
    "APPLICATION_BROWSER_FAILED": "The browser session stopped with an error before it finished.",
    "APPLICATION_TOOL_LIMIT": "The browser session used all of its allowed steps before it finished.",
    "FORM_UNAVAILABLE": "The application form could not be opened.",
    "RECONCILED_ABSENT": "The submit try did not go through. Sidedoor checked and found no application at the site.",
    # Holds that need you
    "REQUIRED_ELIGIBILITY_ANSWER": "Sidedoor is not sure you can work in this role's location.",
    "DESIGNATED_RESUME_MISMATCH": "Your résumé file changed since you set it.",
    "APPLICATION_AUTHORIZATION_REQUIRED": "Sidedoor is not set to submit applications for you.",
    "UNKNOWN_REQUIRED_ANSWERS": "The form asks something Sidedoor cannot answer for you.",
    "AWAITING_APPLICATION_APPROVAL": "Review mode: this application waits for your approval on Approvals.",
    "APPLICATION_APPROVED": "You approved this application.",
    "APPLICATION_REJECTED_BY_YOU": "You rejected this application on Approvals.",
    # Decisions that close a job
    "OUTSIDE_CONFIRMED_SCOPE": "Outside your search (level, field, funding stage or location).",
    "CLEARANCE_OR_CITIZENSHIP_REQUIRED": "The role needs US citizenship or a security clearance.",
    "BASE_SALARY_BELOW_RELAXED_FLOOR": "The pay is below your floor.",
    "SAME_POSTING_AS_EXISTING_JOB": "Same posting as another job Sidedoor already has.",
    "EXISTING_APPLICATION": "You already applied to this posting.",
    "EXISTING_INTERVIEW": "You are already interviewing for this posting.",
    "RETRIES_EXHAUSTED": "Sidedoor used all of its tries.",
    "ANSWER_REFUSED_TWICE": "The application site refused the same answer twice.",
    "ANSWERING_GAVE_UP": "Sidedoor's AI could not fill in the same answers in 7 tries.",
    "ANSWERING_FAILED": "Sidedoor's AI couldn't enter an answer on the form.",
    "DATE_ORDER_UNCLEAR": "The site's date field doesn't show whether it's day/month or month/day.",
    "FILE_UPLOAD_FAILED_3_TIMES": "The site refused the résumé upload three times.",
    "ACCESS_BLOCKED_3_TIMES": "The site asked for a sign-in or a human check three times.",
    "APPLICATION_RECOVERY_ABANDONED": "Sidedoor looked into a problem and decided it cannot apply.",
    "APPLICATION_COORDINATOR_ATTEMPTS_EXHAUSTED": "Sidedoor ran out of tries working out a problem.",
}
# The unreadable-form hold reuses the answers prefix, but nothing was asked: the form could not be read.
FORM_NOT_READABLE = "UNKNOWN_REQUIRED_ANSWERS:" + decision_transport.FORM_NOT_READABLE_FIELD
FORM_NOT_READABLE_WORDS = "Sidedoor couldn't read the application form (it may need you signed in, or the page didn't load)."
ROUTE_WORDS = {"OWNER": "This application needs you.",
               "RECONCILE": "Sidedoor is checking whether the application went through.",
               "RECOVER": "Sidedoor is looking into a problem with this posting.",
               "ORDINARY": "Sidedoor ran into a problem it will retry."}
CLOSED_WORDS = {"INELIGIBLE": "Sidedoor judged this role does not fit your search.",
                "DUPLICATE": "Sidedoor matched this to an application you already have.",
                "EXHAUSTED": "Sidedoor stopped trying.", "ABANDONED": "Sidedoor stopped trying."}

ATTEMPT_WORDS = {"SUBMITTING": "Submitting, outcome being checked", "VERIFIED": "Submitted and confirmed",
                 "CONFIRMED_NOT_SUBMITTED": "Not submitted", "CONSUMED": "Ended", "SUPERSEDED_BY_FACT_REQUEST": "Stopped for a question"}
ELIGIBLE_WORDS = "Eligible: fits your search."

# Owner, 2026-10-24: a job held for the owner's answers is not "Blocked": while its question is open on
# Approvals it waits calmly for the answer; once answered it continues on the next run by itself.
WAITING_ANSWER_WORDS = "Waiting for your answer"
ANSWERED_WORDS = "Continues on the next run"
ANSWER_LABEL, ANSWER_HREF = "Answer on Approvals", "/approvals#questions"
# Review mode holds applications for approval (owner, 2026-10-24): `overview.approval_hold`.
HELD_WORDS, APPROVE_LABEL, APPROVE_HREF = "Waiting for your approval", "Approve on Approvals", "/approvals#applications"
APPROVED_WORDS = "Approved"

# The list's headings (owner, 2026-10-24), in this order; the newest-activity sort holds inside each.
GROUPS = (("needs_you", "Needs you"), ("progress", "In progress"), ("submitted", "Submitted"), ("closed", "Closed"))
GROUP_RANK = {key: rank for rank, (key, _) in enumerate(GROUPS)}
CLOSED_STATES = {"DUPLICATE", "INELIGIBLE", "EXHAUSTED", "ABANDONED"}
EARLIER_APPLICATION = "Same job as an earlier application"

# History lines: event -> words (state events add their code's words).
STATE_EVENTS = {"BLOCKED": "Held", "RETRY_WAIT": "Will try again", "WAITING_BROWSER": "Waiting for the browser", "RECONCILE": "Checking whether it went through",
                "INELIGIBLE": "Closed as not eligible", "DUPLICATE": "Closed as a duplicate",
                "EXHAUSTED": "Gave up", "ABANDONED": "Gave up", "QUEUED": "Queued again", "SUBMITTING": "Submitting"}
EVENT_WORDS = {"DISCOVERED": "Found the posting", "WORKER_SUBMISSION_STARTED": "Started submitting",
               "SUBMISSION_VERIFIED": "Submission confirmed",
               "SUBMISSION_REPORTED_FAILED": "The page reported a problem after Submit",
               "RECONCILE_READ": "Checked whether it went through",
               "APPLICATION_READ_COMPLETE": "Finished reading the posting",
               "SAME_POSTING_LINKED": "Matched to another posting", "APPLICATION_REOPENED": "Reopened"}
HISTORY_LIMIT = 50

RECEIPT_WORDS = {"page_confirmation": "The application page confirmed it",
                 "employer_confirmation_email": "Confirmation email from the employer",
                 "sent_application_email": "Sent by email from your mailbox",
                 "authenticated_ats_application_record": "Found in your applicant account"}


def code_root(code):
    return str(code or "").split(":", 1)[0]


def code_words(code, route=None):
    if code == FORM_NOT_READABLE:
        return FORM_NOT_READABLE_WORDS
    words = CODE_WORDS.get(code_root(code))
    return words or ROUTE_WORDS.get(route or "ORDINARY", ROUTE_WORDS["ORDINARY"])


def _route(code, state):
    import applications
    try:
        return applications.job_route(str(code or ""), state)
    except Exception:   # an unexpected stored code never breaks the page; it shows generic words
        return "ORDINARY"


def _safe_url(value):
    if not isinstance(value, str):
        return None
    parts = urllib.parse.urlsplit(value.strip())
    return value.strip() if parts.scheme in ("http", "https") and parts.netloc else None


def _text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def _listed(reader, sql, parameters, *tables):
    return [row[0] for row in reader.rows(sql, parameters, *tables)]


# --- Buckets: the same classification Overview counts with ---------------------------------------

def reading_keys(reader, now):
    """Jobs a browser session is reading right now (inspect/investigate claimed, lease live)."""
    return set(_listed(reader, "SELECT job_key FROM desktop_requests WHERE state='CLAIMED' AND lease_until>? "
                               "AND operation IN (?,?)", (now.isoformat(),) + READ_OPERATIONS, "desktop_requests"))


def submitted_today_keys(reader, start, end):
    """Job keys of today's verified submissions, newest first: the rows Overview's Submitted counts."""
    rows = reader.rows("SELECT at,job_key FROM application_events WHERE event='SUBMISSION_VERIFIED' AND at>=?",
                       (overview._prefilter(start),), "application_events")
    within = sorted(overview._within(rows, start, end), key=lambda row: overview._utc(row[0]), reverse=True)
    keys = []
    for row in within:
        if row[1] not in keys:
            keys.append(row[1])
    return keys, len(within)


def counts(reader, now, start, end):
    """How many jobs each filter holds. Overview's buckets come from Overview's own functions."""
    by_state = dict(reader.rows("SELECT state,COUNT(*) FROM application_jobs GROUP BY state", (), "application_jobs"))
    found = {key: sum(by_state.get(state, 0) for state in states) for key, states in STATE_FILTERS.items()}
    reading_states = _states_being_read(reader, now)
    for key in LIVE_STATE_FILTERS:
        found[key] -= sum(1 for state in reading_states if state in STATE_FILTERS[key])
    found.update(all=sum(by_state.values()), needs_you=len(overview.blocked_on_owner_keys(reader, now)), retrying=len(overview.retrying_keys(reader, now)),
                 submitted_today=overview.submitted_today(reader, start, end))
    found["reading"] = len(reading_now_keys(reader, now))
    return found


def _states_being_read(reader, now):
    """The states of the jobs being read right now (one per job)."""
    reading = reading_keys(reader, now)
    return [row["state"] for row in _rows_for(reader, sorted(reading))] if reading else []


def state_count(reader, show, now):
    """How many jobs a state filter's badge names (a job being read right now badges "Reading now")."""
    states = STATE_FILTERS[show]
    total = reader.count("SELECT COUNT(*) FROM application_jobs WHERE state IN (%s)" % ",".join("?" * len(states)),
                         states, "application_jobs")
    if show in LIVE_STATE_FILTERS:
        total -= sum(1 for state in _states_being_read(reader, now) if state in states)
    return total


def reading_now_keys(reader, now):
    """Jobs badged "Reading now" (`job_view`): being read, not final, and not an owner hold (an owner
    hold's badge says "Blocked, needs you" or "Waiting for your answer" first)."""
    reading = reading_keys(reader, now)
    if not reading:
        return []
    owner = set(overview._blocked(reader)[0])
    return [key for key in _jobs_in(reader, reading) if key not in owner]


def _jobs_in(reader, keys):
    keys = list(keys)
    found = []
    for index in range(0, len(keys), 500):
        chunk = keys[index:index + 500]
        found += _listed(reader, "SELECT job_key FROM application_jobs WHERE job_key IN (%s) "
                                 "AND state NOT IN ('SUBMITTED','DUPLICATE','INELIGIBLE','EXHAUSTED','ABANDONED')"
                         % ",".join("?" * len(chunk)), tuple(chunk), "application_jobs")
    return found


def ordered_keys(reader, show, now, start, end, company=None, query=None):
    """The job keys the filter selects, newest activity first."""
    clauses, parameters = [], []
    if show in STATE_FILTERS:
        states = STATE_FILTERS[show]
        clauses.append("state IN (%s)" % ",".join("?" * len(states)))
        parameters += states
    elif show == "needs_you":
        clauses.append("state='BLOCKED'")
    elif show == "retrying":
        clauses.append("state IN ('BLOCKED','RETRY_WAIT')")
    elif show == "reading":
        clauses.append("state NOT IN (%s)" % ",".join("?" * len(FINAL)))
        parameters += sorted(FINAL)
    if company:
        clauses.append("employer=?")
        parameters.append(company)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    # Search tests the parsed company and role in Python (a LIKE on the stored JSON misses escaped
    # non-ASCII text and quotes), over at most the SEARCH_LIMIT most recently active jobs.
    select = "job_key,employer,payload" if query else "job_key"
    limit = " LIMIT %d" % SEARCH_LIMIT if query else ""
    rows = reader.rows("SELECT %s FROM application_jobs%s ORDER BY updated_at DESC%s" % (select, where, limit),
                       tuple(parameters), "application_jobs")
    if query:
        needle = query.casefold()
        rows = [row for row in rows if any(needle in value.casefold() for value in _names(row[1], row[2]))]
    keys = [row[0] for row in rows]
    if show in ("needs_you", "retrying"):
        wanted = set(overview.blocked_on_owner_keys(reader, now) if show == "needs_you" else overview.retrying_keys(reader, now))
        keys = [key for key in keys if key in wanted]
    elif show == "submitted_today":
        today, _ = submitted_today_keys(reader, start, end)
        selected = set(keys)
        keys = [key for key in today if key in selected]
    elif show in LIVE_STATE_FILTERS:
        live = reading_keys(reader, now)
        keys = [key for key in keys if key not in live]
    elif show == "reading":
        live = set(reading_now_keys(reader, now))
        keys = [key for key in keys if key in live]
    return keys


def _names(employer, payload_text):
    payload = overview._json(payload_text) or {}
    names = [str(employer or "")]
    names += [value for value in (payload.get("company"), payload.get("role"), payload.get("title")) if isinstance(value, str)]
    return names


# --- One job ----------------------------------------------------------------------------------------

def _receipt(state, text):
    """The verified receipt of a SUBMITTED job, in plain words; None for any other state."""
    if state != "SUBMITTED":
        return None
    receipt = overview._json(text)
    kind = receipt.get("evidence_kind") if receipt is not None else None
    if receipt is None or not (kind is None or isinstance(kind, str)):
        return {"words": "Receipt not readable", "confirmation": None, "at": None, "to": None}
    moment = receipt.get("sent_at") or receipt.get("armed_at") or receipt.get("observed_at")
    found = {"words": RECEIPT_WORDS.get(kind, "Submission confirmed"),
             "confirmation": _text(receipt.get("confirmation_text")) if kind == "page_confirmation" else None,
             "at": overview._utc(moment) if isinstance(moment, str) else None, "to": None}
    recipients = receipt.get("email_to")
    if kind == "sent_application_email" and isinstance(recipients, list):
        found["to"] = ", ".join(value for value in recipients if isinstance(value, str)) or None
    return found


# Owner, 2026-10-24: "I only need to apply by myself if it requires authentication or captcha, thats all."
# Shown only once Sidedoor has stopped on a sign-in or human check; while it still retries, the retry
# wording stands, so the user is never told to apply by hand while Sidedoor may still submit.
APPLY_YOURSELF = ("The site needs a sign-in or a human check that Sidedoor can't do. If you want this job, open the "
                  "posting and apply yourself.")


def _awaits_answers(row, owner_hold):
    """An owner hold on the form's questions (UNKNOWN_REQUIRED_ANSWERS), not the unreadable-form note."""
    return bool(owner_hold) and overview.answers_hold(row["blocker"])


def group(state, needs_owner):
    """Which heading a job sits under: Needs you (`overview.blocked_on_owner`, the one needs-you rule),
    In progress, Submitted or Closed."""
    if state == "SUBMITTED":
        return "submitted"
    if state in CLOSED_STATES:
        return "closed"
    return "needs_you" if needs_owner else "progress"


def _what_to_do(row, company, route, owner_hold, asked, fmt):
    """(text, href, label): what happens next, or what the user does, in plain words. Only a sign-in or
    human check that made Sidedoor stop ever tells the user to apply by hand (owner 2026-10-24)."""
    state, code = row["state"], code_root(row["blocker"])
    if state in ("EXHAUSTED", "ABANDONED") and code == "ACCESS_BLOCKED_3_TIMES":
        return (APPLY_YOURSELF, None, None)
    next_at = overview._utc(row["next_at"]) if row["next_at"] else None
    retry = "Will try again automatically" + (" " + fmt(next_at) if next_at else "") + "."
    if owner_hold and overview.approval_hold(row["blocker"]):
        email = (_route_of(row) == "email")
        return (("Sidedoor wrote this email application%s. It is sent once you approve it on Approvals."
                 % (" to " + _route_of(row, "destination") if _route_of(row, "destination") else "")) if email else
                "Sidedoor filled this application. It is submitted once you approve it on Approvals.",
                APPROVE_HREF, APPROVE_LABEL)
    if state == "BLOCKED" and code == "APPLICATION_APPROVED":
        return ("You approved this application. Sidedoor submits it on its next applications run.", None, None)
    if owner_hold:
        if code == "SUBMISSION_OUTCOME_UNKNOWN":
            return ("Check your applicant account or confirmation email at %s to see whether it went through. "
                    "Sidedoor will not submit it again on its own." % company, None, None)
        if row["blocker"] == FORM_NOT_READABLE:
            return ("Sidedoor couldn't read this form's questions and will not guess them, so it holds this application"
                    + (". Answer its note on Approvals." if asked else " and asks you on Approvals."),
                    "/approvals" if asked else None, "Open Approvals" if asked else None)
        if asked and _awaits_answers(row, owner_hold):
            return ("Answer Sidedoor's question about this application on Approvals.", ANSWER_HREF, ANSWER_LABEL)
        if asked:
            return ("Answer Sidedoor's question about this application on Approvals.", "/approvals", "Open Approvals")
        if code == "UNKNOWN_REQUIRED_ANSWERS":
            # No open question: the owner answered (or a card expired and is asked again). The job is due
            # again and continues on its own; it must never tell the owner to apply by hand (2026-10-24).
            return ("Your answers are in. Sidedoor continues this application on its next run, and asks again on "
                    "Approvals if anything is still open.", None, None)
        if code == "REQUIRED_ELIGIBILITY_ANSWER":
            return ("Check the posting. If you can work there, update your work authorization in Settings.",
                    "/settings#profile", "Open Settings")
        if code == "DESIGNATED_RESUME_MISMATCH":
            return ("Upload your résumé again in Settings.", "/settings#resume", "Open Settings")
        if code == "APPLICATION_AUTHORIZATION_REQUIRED":
            # The hold is the `preapproved_applications` setting being off; no dashboard control sets it.
            return ("Sidedoor's settings do not allow it to submit applications, and there is no switch for this on "
                    "the dashboard yet. Contact support@yoursidedoor.com to turn it on.", None, None)
        return ("Sidedoor is holding this application for the reason above.", None, None)
    if state == "BLOCKED" or state == "RETRY_WAIT":
        return (retry, None, None)
    if state == "WAITING_BROWSER":
        return ("Sidedoor will continue when the browser is free" + (", next check " + fmt(next_at) if next_at else "") + ".",
                None, None)
    if state in ("SUBMITTING", "RECONCILE") and "never_armed" in row.keys() and row["never_armed"]:
        return ("Sidedoor stopped before sending this application, so nothing was sent. It will try again "
                "automatically. Nothing to do.", None, None)
    if state in ("SUBMITTING", "RECONCILE"):
        return ("Sidedoor is checking whether the application went through. Nothing to do.", None, None)
    if state == "QUEUED":
        return ("Sidedoor reads the posting on its next applications run.", None, None)
    return (None, None, None)


def _route_of(row, key="route"):
    """The held application's route ("ats" or "email") or destination, from its card (`row["approval"]`)."""
    snapshot = overview._json(row["approval"]) if "approval" in row.keys() and row["approval"] else None
    value = (snapshot or {}).get(key)
    return value if isinstance(value, str) else None


def job_view(row, owner_keys, asked, live_reading, fmt):
    payload = overview._json(row["payload"]) or {}
    company = _text(payload.get("company")) or _text(row["employer"]) or "Unknown company"
    role = _text(payload.get("role")) or _text(payload.get("title")) or "Role not recorded"
    state, code = row["state"], row["blocker"]
    route = _route(code, state)
    owner_hold = row["job_key"] in owner_keys
    reading = row["job_key"] in live_reading and state not in FINAL
    asked_here = row["job_key"] in asked
    answers = _awaits_answers(row, owner_hold)
    answered = answers and not asked_here
    needs = overview.blocked_on_owner(owner_hold, asked_here, code)
    if owner_hold and overview.approval_hold(code):
        words, tone = HELD_WORDS, "info"   # counted as an application to approve only (owner 2026-10-24)
    elif state == "BLOCKED" and code == "APPLICATION_APPROVED":
        words, tone = APPROVED_WORDS, "info"
    elif answers:
        words, tone = (ANSWERED_WORDS, "info") if answered else (WAITING_ANSWER_WORDS, "info")
    elif owner_hold and asked_here:
        words, tone = WAITING_ANSWER_WORDS, "info"   # counted as a question only (owner 2026-10-24)
    elif owner_hold:
        words, tone = "Blocked, needs you", "error"
    elif reading:
        words, tone = "Reading now", "info"
    elif state == "BLOCKED" or (state in ("SUBMITTING", "RECONCILE") and "never_armed" in row.keys() and row["never_armed"]):
        # A submit that stopped before arming sent nothing: it waits to retry, not for a confirmation.
        words, tone = "Waiting to retry", "warning"
    else:
        words, tone = STATE_WORDS.get(state, "In progress"), STATE_TONES.get(state, "muted")
    todo, href, label = _what_to_do(row, company, route, owner_hold, asked_here, fmt)
    problem = None
    if overview.approval_hold(code) or code == "APPLICATION_APPROVED":
        pass   # nothing went wrong: the to-do line says what happens
    elif state in ("BLOCKED", "RETRY_WAIT", "WAITING_BROWSER") or (state in ("SUBMITTING", "RECONCILE") and code):
        problem = code_words(code, "OWNER" if owner_hold else route)
    closed = None
    if state in CLOSED_WORDS:
        closed = CODE_WORDS.get(code_root(code)) or CLOSED_WORDS[state]
    next_at = overview._utc(row["next_at"]) if row["next_at"] and state not in FINAL else None
    receipt = _receipt(state, row["receipt"])
    # `needs_you` and `attention` (shown in red) are the one needs-you rule (`overview.blocked_on_owner`):
    # never for a job with a question on Approvals, or whose answers are in.
    return {"attention": needs, "group": group(state, needs),
            "submitted_at": receipt["at"] if receipt else None,
            "given_up": state in ("EXHAUSTED", "ABANDONED"), "duplicate": state == "DUPLICATE","key": row["job_key"], "company": company, "employer": row["employer"], "role": role,
            "url": _safe_url(payload.get("url")), "state": words, "reading": reading, "tone": tone, "needs_you": needs,
            "problem": problem, "todo": todo, "todo_href": href, "todo_label": label, "closed": closed,
            "updated": overview._utc(row["updated_at"]), "next_at": next_at,
            "receipt": receipt, "attempts": []}


def attempts(reader, key, full=False, current_resume=None):
    """Submit attempts, newest first: time and outcome; with `full`, the answers used and the résumé
    version (the evidence holds the whole read, so the list does not load it)."""
    found = []
    for row in reader.rows("SELECT state,started_at%s FROM application_attempts WHERE job_key=? "
                           "ORDER BY started_at DESC" % (",evidence" if full else ""), (key,), "application_attempts"):
        item = {"at": overview._utc(row["started_at"]), "outcome": ATTEMPT_WORDS.get(str(row["state"] or ""), "Ended")}
        if full:
            evidence = overview._json(row["evidence"]) or {}
            item.update(answers=_answers(evidence), resume=_resume(evidence.get("resume_sha256"), current_resume))
        found.append(item)
    return found


def _answers(evidence):
    import applications
    answers = evidence.get("answers")
    if not isinstance(answers, dict):
        return []
    review = evidence.get("review") if isinstance(evidence.get("review"), dict) else {}
    labels = {}
    questions = review.get("questions")
    for question in questions if isinstance(questions, list) else []:
        if isinstance(question, dict):
            try:
                labels[applications.question_field(question)] = _text(question.get("label"))
            except Exception:   # a malformed stored question never breaks the page
                continue
    return [{"question": labels.get(field) or "A form question", "answer": value if isinstance(value, str) else json.dumps(value)}
            for field, value in answers.items()]


def _resume(sha, current):
    if not isinstance(sha, str) or not sha:
        return None
    if current and sha == current:
        return "Your current résumé"
    return "An earlier résumé (version %s)" % sha[:8]


def history(reader, key):
    found = []
    rows = reader.rows("SELECT at,event,detail FROM application_events WHERE job_key=? ORDER BY id DESC LIMIT ?",
                       (key, HISTORY_LIMIT), "application_events")
    # Where each answering failure has its own "Try N failed" line, the retry hold's "Held: Some form
    # answers still need to be written" repeats it (and reads as pending work once the job closed).
    per_try = any(row["event"] == "ANSWERING_FAILED" for row in rows)
    for row in rows:
        detail = overview._json(row["detail"]) or {}
        event = str(row["event"] or "")
        if per_try and event in STATE_EVENTS and code_root(detail.get("code")) == "ANSWERING_RETRY":
            continue
        if event in STATE_EVENTS:
            code = detail.get("code")
            route = detail.get("route") if isinstance(detail.get("route"), str) and detail["route"] in ROUTE_WORDS else None
            text = STATE_EVENTS[event] + (": " + code_words(code, route) if code else "")
        elif event == "ELIGIBILITY_CHECKED":
            decision = detail.get("decision")
            text = "Read the posting: " + (ELIGIBLE_WORDS if decision == "ELIGIBLE" else code_words(decision))
        elif event == "ANSWERING_FAILED":
            # One line per answering failure (a repeated retry hold writes no new BLOCKED event).
            streak = detail.get("streak")
            text = ("Try %d failed: " % streak if isinstance(streak, int) and not isinstance(streak, bool)
                    else "A try failed: ") + "Sidedoor's AI couldn't enter an answer on the form"
        elif event in EVENT_WORDS:
            text = EVENT_WORDS[event]
        else:
            continue
        found.append({"at": overview._utc(row["at"]), "text": text})
    return found


def verdict(reader, key):
    """The latest eligibility check: plain verdict, the AI's scope reasons, its warnings."""
    rows = reader.rows("SELECT observed_at,eligibility,evidence FROM application_reviews WHERE job_key=? "
                       "ORDER BY observed_at DESC LIMIT 1", (key,), "application_reviews")
    if not rows:
        return None
    review = overview._json(rows[0]["evidence"]) or {}
    decision = rows[0]["eligibility"]
    reasons = []
    fit = review.get("scope_fit") if isinstance(review.get("scope_fit"), dict) else {}
    for dimension in ("level", "specialization", "funding_stage", "location"):
        judged = fit.get(dimension)
        if isinstance(judged, dict) and isinstance(judged.get("in_scope"), bool) and _text(judged.get("reason")):
            reasons.append({"topic": dimension.replace("_", " ").capitalize(), "fits": judged["in_scope"],
                            "reason": judged["reason"].strip()})
    return {"at": overview._utc(rows[0]["observed_at"]),
            "words": ELIGIBLE_WORDS if decision == "ELIGIBLE" else code_words(decision), "reasons": reasons}


def closing_reason(reader, key, state):
    """The AI's own words for a closing decision, where it gave them."""
    if state == "DUPLICATE":
        rows = reader.rows("SELECT detail FROM application_events WHERE job_key=? AND event='SAME_POSTING_LINKED' "
                           "ORDER BY id DESC LIMIT 1", (key,), "application_events")
        detail = overview._json(rows[0][0]) if rows else None
        if detail:
            same_as = detail.get("same_as")
            same = reader.rows("SELECT employer,payload FROM application_jobs WHERE job_key=?", (same_as,),
                               "application_jobs") if isinstance(same_as, str) else []
            other = None
            if same:
                payload = overview._json(same[0]["payload"]) or {}
                other = ", ".join(value for value in (_text(payload.get("company")) or _text(same[0]["employer"]),
                                                      _text(payload.get("role")) or _text(payload.get("title"))) if value)
            return {"other": other, "other_key": same_as if same else None, "reason": _text(detail.get("reason"))}
    if state == "ABANDONED":
        rows = reader.rows("SELECT detail FROM application_events WHERE job_key=? AND event='APPLICATION_ABANDONED' "
                           "ORDER BY id DESC LIMIT 1", (key,), "application_events")
        detail = overview._json(rows[0][0]) if rows else None
        if detail and _text(detail.get("reason")):
            return {"other": None, "other_key": None, "reason": _text(detail.get("reason"))}
    return None



# --- The specific reason a closed job closed (owner, 2026-10-24) ------------------------------------
# Shown under the category words on every closed card: the AI's own words where it gave them, else the
# host's recorded note in plain words. Never a raw code or a path.
HOST_INSTRUCTION = re.compile(r"\s*\b(Retain|Continue the existing|Inspect failure|Resolve the failure)\b.*$", re.S)
RAW_TEXT = re.compile(r"\b[A-Z]+_[A-Z0-9_]+\b|/|\\")
ACCESS_NOUNS = {"AUTHENTICATION_REQUIRED": "a sign-in", "MFA_REQUIRED": "a sign-in code", "CAPTCHA_REQUIRED": "a human check"}
GEOGRAPHY_POINTER = "/facts/hiring_geography_includes_candidate"


def _latest(reader, sql, key, table="application_events"):
    rows = reader.rows(sql, (key,), table)
    return rows[0][0] if rows else None


def _sentence(text):
    text = _text(text)
    if not text:
        return None
    text = text.rstrip()
    return text if text[-1] in ".!?\"\u201d" else text + "."


def _host_note(note):
    """A host note in the user's voice, its instruction to the operator removed; None if nothing is left."""
    text = HOST_INSTRUCTION.sub("", _text(note) or "").strip().rstrip(";").strip()
    text = text.replace("the owner's", "your").replace("The owner's", "Your").replace("the owner", "you")
    return _sentence(text) if text and not RAW_TEXT.search(text) else None


def _scope_reason(review):
    import applications
    for dimension in applications.SCOPE_DIMENSIONS:
        judged = applications.scope_verdict(review, dimension)
        if judged and judged["in_scope"] is False:
            return _sentence(judged["reason"].strip())
    facts = review.get("facts") if isinstance(review.get("facts"), dict) else {}
    if facts.get("open") is False:
        return "The posting says the role is no longer open."
    if facts.get("hiring_geography_includes_candidate") is False:
        quote = next((_text(item.get("quote")) for item in review.get("browser_evidence") or []
                      if isinstance(item, dict) and item.get("pointer") == GEOGRAPHY_POINTER and _text(item.get("quote"))), None)
        if quote:
            return "The posting says: \u201c%s\u201d Sidedoor judged that doesn't include you." % quote.strip()
        return "The posting says where candidates must be based, and Sidedoor judged that doesn't include you."
    options = applications.location_options(facts)
    places = [option.get("country") for option in options] if options is not None else [facts.get("country")]
    places = [place for place in places if isinstance(place, str) and place.strip()]
    if places:
        return "The role is in %s, outside the countries in your search." % ", ".join(places)
    return None


def closed_reason(reader, key, state, code, closing=None):
    """The specific recorded reason a closed job closed, in plain words, or None."""
    import applications
    if state not in CLOSED_STATES:
        return None
    if closing and _text(closing.get("reason")):
        return _sentence(closing["reason"])        # the AI's own words (same-posting match, recovery)
    root = code_root(code)
    if root == applications.APPLICATION_REJECTED:
        # Rejected on Approvals (owner, 2026-10-24): the user's own optional reason, as written.
        detail = overview._json(_latest(reader, "SELECT detail FROM application_events WHERE job_key=? AND "
                                        "event='APPLICATION_REJECTED' ORDER BY id DESC LIMIT 1", key)) or {}
        return ("Your reason: " + _sentence(detail["reason"])) if _text(detail.get("reason")) else None
    if root == "OUTSIDE_CONFIRMED_SCOPE":
        evidence = _latest(reader, "SELECT evidence FROM application_reviews WHERE job_key=? ORDER BY observed_at DESC LIMIT 1",
                           key, "application_reviews")
        found = _scope_reason(overview._json(evidence) or {})
        if found:
            return found
        # Closed on research's own verdict before any read (owner, 2026-10-25): its words.
        payload = overview._json(_latest(reader, "SELECT payload FROM application_jobs WHERE job_key=?", key,
                                          "application_jobs")) or {}
        found = applications.research_scope_reason(payload)
        if found:
            return "Research judged: " + _sentence(found)
    if root == applications.ACCESS_GIVE_UP_CODE:
        detail = overview._json(_latest(reader, "SELECT detail FROM application_events WHERE job_key=? AND event='%s' "
                                        "ORDER BY id DESC LIMIT 1" % applications.ACCESS_GIVE_UP_EVENT, key)) or {}
        blocks = [block for block in detail.get("blocks") or [] if isinstance(block, dict)]
        if blocks:
            nouns = sorted({ACCESS_NOUNS.get(block.get("code"), "a sign-in or a human check") for block in blocks})
            return ("The application site asked for %s each of the %d times Sidedoor tried, so nothing was submitted."
                    % (" or ".join(nouns), len(blocks)))
    if root == applications.UPLOAD_GIVE_UP_CODE:
        detail = overview._json(_latest(reader, "SELECT detail FROM application_events WHERE job_key=? AND event='%s' "
                                        "ORDER BY id DESC LIMIT 1" % applications.UPLOAD_GIVE_UP_EVENT, key)) or {}
        count = len(detail.get("failures") or []) or applications.UPLOAD_FAILURE_LIMIT
        return ("The site did not take the résumé or a required file in %d tries, each stopped before anything "
                "was submitted." % count)
    if root == applications.REFUSED_TWICE_CODE:
        detail = overview._json(_latest(reader, "SELECT detail FROM application_events WHERE job_key=? AND event='%s' "
                                        "ORDER BY id DESC LIMIT 1" % applications.REFUSED_TWICE_EVENT, key)) or {}
        label = _text(detail.get("label"))
        return ("The application site wouldn't accept the answer to %s twice, so nothing was submitted. "
                "Apply by hand if you want this one." % ("\u201c%s\u201d" % " ".join(label.split()) if label else "one question"))
    if root == applications.ANSWERING_GAVE_UP_CODE:
        return "Apply by hand if you want this one."   # the headline already says why (owner pick, 2026-10-26)
    if root == applications.DATE_ORDER_CODE:
        detail = overview._json(_latest(reader, "SELECT detail FROM application_events WHERE job_key=? AND event='%s' "
                                        "ORDER BY id DESC LIMIT 1" % applications.DATE_ORDER_EVENT, key)) or {}
        label = _text(detail.get("label"))
        return ("The site's date field for %s doesn't show whether it's day/month or month/day, so Sidedoor didn't "
                "guess and nothing was submitted. Apply by hand if you want this one."
                % ("\u201c%s\u201d" % " ".join(label.split()) if label else "one question"))
    note = _host_note(_latest(reader, "SELECT next_action FROM application_jobs WHERE job_key=?", key, "application_jobs"))
    if note:
        return note
    if state in ("EXHAUSTED", "ABANDONED"):
        return last_try_reason(reader, key)
    return None


def last_try_reason(reader, key):
    """The real stop of a closed job's last try, in plain words, or None (owner, 2026-10-24): a
    submit's own stop is held as RECONCILE, and the RETRY_WAIT after it only says the try is over
    (RECONCILED_ABSENT), so that wrapper is skipped; a closed job is never described with "will
    retry" words."""
    for (detail,) in reader.rows("SELECT detail FROM application_events WHERE job_key=? AND event IN "
                                 "('BLOCKED','RETRY_WAIT','RECONCILE') ORDER BY id DESC LIMIT 20", (key,),
                                 "application_events"):
        last = overview._json(detail) or {}
        code = _text(last.get("code"))
        if not code or code_root(code) in RETRY_ONLY_CODES:
            continue
        if code_root(code) == "INVALID_SUPPLIED_ANSWER":
            refused = refused_question(reader, key)
            if refused:
                return refused
        words = CODE_WORDS.get(code_root(code)) if code != FORM_NOT_READABLE else FORM_NOT_READABLE_WORDS
        return "Its last try stopped on this: " + (words or CLOSED_PROBLEM_WORDS)
    return None


# Codes that only say a try ended and another may follow, never why it stopped.
RETRY_ONLY_CODES = frozenset({"RECONCILED_ABSENT"})
CLOSED_PROBLEM_WORDS = "Sidedoor ran into a problem it could not get past."
NEEDS_WORDS = {"date": "a date", "number": "a number", "choice": "one of its options", "boolean": "yes or no"}


def refused_question(reader, key):
    """'...didn't accept your answer to "<label>" (it needs a date).' from the structured field the
    submit session named (its blocked.field) and that question's label and type as the form was read;
    None when the session named no field or the question is not on record."""
    import applications
    field = None
    for (result,) in reader.rows("SELECT result FROM desktop_requests WHERE job_key=? AND operation='submit' "
                                 "ORDER BY created_at DESC LIMIT 5", (key,), "desktop_requests"):
        blocked = (overview._json(result) or {}).get("blocked")
        if isinstance(blocked, dict) and blocked.get("code") == "INVALID_SUPPLIED_ANSWER":
            field = _text(blocked.get("field"))
            break
    if not field:
        return None
    record = reader.rows("SELECT questions,conditional_questions FROM application_requisition_records WHERE job_key=?",
                         (key,), "application_requisition_records")
    questions = []
    for text in (record[0] if record else ()):
        try:
            listed = json.loads(text)
        except (TypeError, ValueError):
            continue
        questions += [item for item in listed if isinstance(item, dict)] if isinstance(listed, list) else []
    question = next((item for item in questions if applications.question_field(item) == field), None)
    label = _text(question.get("label")) if question else None
    if not label:
        return None
    needs = NEEDS_WORDS.get(str(question.get("answer_type") or ""))
    return ("Its last try stopped because the application site didn't accept your answer to \u201c%s\u201d%s."
            % (" ".join(label.split()), " (it needs %s)" % needs if needs else ""))

# --- The page ---------------------------------------------------------------------------------------

def current_resume_sha(home):
    """The résumé hash in the user's profile, or None (never an error)."""
    import user_profile
    try:
        folder = Path(home) if home is not None else user_profile.home()
        profile = json.loads((folder / "profile.json").read_text())
        sha = user_profile.application_resume(profile).get("sha256")   # the file applications upload
        return sha if isinstance(sha, str) else None
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def _rows_for(reader, keys):
    rows = {}
    for index in range(0, len(keys), 500):
        chunk = keys[index:index + 500]
        # Named columns: an older table without one fails here as a DatabaseError (the section degrades).
        approval = ("(SELECT snapshot FROM application_approvals a WHERE a.job_key=application_jobs.job_key AND a.state='WAITING')"
                    if reader.has("application_approvals") else "NULL")
        # The job's current submit session ended without the arm guard ever opening: submit is reachable
        # only through the arm guard, so nothing was sent (`applications._never_armed_submission`).
        never_armed = ("(SELECT request.armed_at IS NULL AND request.state IN ('DONE','CONSUMED','UNKNOWN','FAILED','EXPIRED') "
                       "FROM desktop_requests AS request JOIN application_attempts AS attempt "
                       "ON attempt.attempt_id=request.attempt_id AND attempt.job_key=request.job_key "
                       "WHERE request.job_key=application_jobs.job_key AND request.operation='submit' "
                       "AND attempt.state='SUBMITTING' ORDER BY attempt.started_at DESC,request.rowid DESC LIMIT 1)"
                       if reader.has("desktop_requests", "application_attempts") else "NULL")
        for row in reader.rows("SELECT job_key,employer,payload,state,blocker,next_at,receipt,updated_at,%s AS approval,"
                               "%s AS never_armed FROM application_jobs WHERE job_key IN (%s)"
                               % (approval, never_armed, ",".join("?" * len(chunk))),
                               tuple(chunk), "application_jobs"):
            rows[row["job_key"]] = row
    return [rows[key] for key in keys if key in rows]


def _grouped(reader, keys, owner, asked):
    """The keys under their headings (Needs you, In progress, Submitted, Closed), the given order kept
    inside each, so paging walks the headings in order."""
    found = {}
    for index in range(0, len(keys), 500):
        chunk = keys[index:index + 500]
        for row in reader.rows("SELECT job_key,state,blocker FROM application_jobs WHERE job_key IN (%s)"
                               % ",".join("?" * len(chunk)), tuple(chunk), "application_jobs"):
            hold = row["job_key"] in owner
            found[row["job_key"]] = GROUP_RANK[group(row["state"], overview.blocked_on_owner(
                hold, row["job_key"] in asked, row["blocker"]))]
    keys = sorted(keys, key=lambda key: found.get(key, GROUP_RANK["progress"]))
    # Each heading's number counts the whole filtered list, not just this page (owner 2026-10-24).
    totals = {key: 0 for key, _ in GROUPS}
    rank_key = {rank: key for key, rank in GROUP_RANK.items()}
    for key in keys:
        totals[rank_key[found.get(key, GROUP_RANK["progress"])]] += 1
    return keys, totals


def listing(reader, show, page, company, query, now, start, end, fmt):
    owner, _ = overview._blocked(reader)
    owner = set(owner)
    asked = {row[0] for row in overview._pending_questions(reader, now)}
    keys, totals = _grouped(reader, ordered_keys(reader, show, now, start, end, company, query), owner, asked)
    pages = max(1, math.ceil(len(keys) / PAGE_SIZE))
    page = min(max(1, page), pages)
    window = keys[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
    live = reading_keys(reader, now)
    jobs = []
    for row in _rows_for(reader, window):
        view = job_view(row, owner, asked, live, fmt)
        view["attempts"] = attempts(reader, row["job_key"])
        if row["state"] in ("DUPLICATE", "ABANDONED"):
            view["closing"] = closing_reason(reader, row["job_key"], row["state"])
        view["reason"] = closed_reason(reader, row["job_key"], row["state"], row["blocker"], view.get("closing"))
        jobs.append(view)
    groups = [{"key": key, "label": label, "count": totals.get(key, 0), "jobs": [view for view in jobs if view["group"] == key]}
              for key, label in GROUPS]
    return {"jobs": jobs, "groups": [item for item in groups if item["jobs"]], "total": len(keys), "page": page, "pages": pages}


def detail(reader, key, now, fmt, current_resume):
    rows = _rows_for(reader, [key])
    if not rows:
        return None
    owner, _ = overview._blocked(reader)
    asked = {row[0] for row in overview._pending_questions(reader, now)}
    view = job_view(rows[0], set(owner), asked, reading_keys(reader, now), fmt)
    view["attempts"] = attempts(reader, key, True, current_resume)
    view["history"] = history(reader, key)
    view["verdict"] = verdict(reader, key)
    view["closing"] = closing_reason(reader, key, rows[0]["state"])
    view["reason"] = closed_reason(reader, key, rows[0]["state"], rows[0]["blocker"], view["closing"])
    return view


def _int(value, default=1):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def gather(database, config, home=None, show="all", page=1, company=None, query=None, job=None, now=None):
    """Everything the Applications page shows. `database` is a read-only connection or None."""
    now = now or dt.datetime.now(dt.timezone.utc)
    zone_name = config.get("timezone") or "America/Los_Angeles"
    zone = ZoneInfo(zone_name)
    hour = workflow_tasks.day_start_hour(config)
    start = workflow_tasks.service_day_start(now, zone_name, hour).astimezone(dt.timezone.utc)
    end = workflow_tasks.next_day_start(now, zone_name, hour).astimezone(dt.timezone.utc)
    fmt = lambda moment: overview.when(moment, now, zone)
    show = show if show in FILTER_KEYS else "all"
    company = _text(company)
    query = _text(query)
    query = query[:200] if query else None
    reader = overview.Reader(database)

    def link(**changes):
        """This page's address with the current filter, company and search, changed as given."""
        values = dict({"show": show, "company": company, "q": query}, **changes)
        pairs = [(name, value) for name, value in values.items() if value and not (name == "show" and value == "all")
                 and not (name == "page" and value == 1)]
        return "/applications" + ("?" + urllib.parse.urlencode(pairs) if pairs else "")

    found = {"show": show, "link": link, "company": company, "query": query, "filters": FILTERS, "when": fmt,
             "unreadable": reader.broken, "unavailable": UNAVAILABLE, "job": None, "detail_missing": False}
    found["counts"] = overview.section(reader, counts, now, start, end)
    if job:
        found["job"] = overview.section(reader, detail, str(job), now, fmt, current_resume_sha(home))
        found["detail_missing"] = found["job"] is None
        return found
    found["list"] = overview.section(reader, listing, show, _int(page), company, query, now, start, end, fmt)
    if found["list"] != UNAVAILABLE and company:
        names = [item["company"] for item in found["list"]["jobs"]]
        found["company_name"] = names[0] if names else company
    return found
