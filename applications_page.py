"""What the Applications page shows (E6.T6; E6.T0 spec section 8). Display only, like Overview
(overview_page): every line is a record the application pipeline already wrote, read through
`overview_page.snapshot` (no -wal/-shm, nothing written), and every verdict shown is one the AI or
the pipeline already made. Nothing here judges, writes, or calls the network or a model.

Sources (why each is the record the product itself treats as that fact):
- the job list: `application_jobs` (one row per posting: state, blocker, next_at, receipt,
  updated_at). Company, role and link come from its `payload` (the role `ingest` recorded).
- buckets: the same code Overview counts with. "Blocked, needs you" and "Waiting to retry" are
  `overview_page._blocked` (BLOCKED jobs split by `applications.job_route`, plus the spent
  SUBMISSION_OUTCOME_UNKNOWN test, O19); "Waiting for confirmation" is SUBMITTING/RECONCILE
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
                 "queued": ("QUEUED", "RETRY_WAIT"),
                 "waiting_browser": ("WAITING_BROWSER",),
                 "ineligible": ("INELIGIBLE",),
                 "duplicate": ("DUPLICATE",),
                 "given_up": ("EXHAUSTED", "ABANDONED")}
READ_OPERATIONS = ("inspect", "investigate")

# Every state applications.py writes (QUEUED, BLOCKED, RETRY_WAIT, WAITING_BROWSER, SUBMITTING, RECONCILE
# and the five final ones) has words here or in job_view (BLOCKED), and a filter.
STATE_WORDS = {"QUEUED": "Queued", "RETRY_WAIT": "Waiting to retry", "WAITING_BROWSER": "Waiting for the browser",
               "SUBMITTING": "Submitting",
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
    "FIELD_LIMIT_TOO_SHORT_FOR_FACT": "A form field was too short for one of your details.",
    "PREFLIGHT_TRANSIENT": "A temporary problem came up before submitting.",
    "SUBMISSION_OUTCOME_UNKNOWN": "Sidedoor could not confirm whether the application went through.",
    "SUBMISSION_REPORTED_FAILED": "The application page reported a problem after Submit.",
    "SUBMISSION_OBSERVATION_LOST": "Sidedoor lost track of the submit before seeing the result.",
    # Holds that need you
    "REQUIRED_ELIGIBILITY_ANSWER": "Sidedoor is not sure you can work in this role's location.",
    "DESIGNATED_RESUME_MISMATCH": "Your résumé file changed since you set it.",
    "APPLICATION_AUTHORIZATION_REQUIRED": "Sidedoor is not set to submit applications for you.",
    "UNKNOWN_REQUIRED_ANSWERS": "The form asks something Sidedoor cannot answer for you.",
    # Decisions that close a job
    "OUTSIDE_CONFIRMED_SCOPE": "Outside your search (level, field, funding stage or location).",
    "CLEARANCE_OR_CITIZENSHIP_REQUIRED": "The role needs US citizenship or a security clearance.",
    "BASE_SALARY_BELOW_RELAXED_FLOOR": "The pay is below your floor.",
    "SAME_POSTING_AS_EXISTING_JOB": "Same posting as another job Sidedoor already has.",
    "EXISTING_APPLICATION": "You already applied to this posting.",
    "EXISTING_INTERVIEW": "You are already interviewing for this posting.",
    "RETRIES_EXHAUSTED": "Sidedoor used all of its tries.",
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
    owner, waiting = overview._blocked(reader)
    by_state = dict(reader.rows("SELECT state,COUNT(*) FROM application_jobs GROUP BY state", (), "application_jobs"))
    found = {key: sum(by_state.get(state, 0) for state in states) for key, states in STATE_FILTERS.items()}
    found.update(all=sum(by_state.values()), needs_you=len(owner), retrying=len(waiting),
                 submitted_today=overview.submitted_today(reader, start, end))
    reading = reading_keys(reader, now)
    found["reading"] = len(_jobs_in(reader, reading)) if reading else 0
    return found


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
    elif show in ("needs_you", "retrying"):
        clauses.append("state='BLOCKED'")
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
        owner, waiting = overview._blocked(reader)
        wanted = set(owner if show == "needs_you" else waiting)
        keys = [key for key in keys if key in wanted]
    elif show == "submitted_today":
        today, _ = submitted_today_keys(reader, start, end)
        selected = set(keys)
        keys = [key for key in today if key in selected]
    elif show == "reading":
        live = reading_keys(reader, now)
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


def _what_to_do(row, company, route, owner_hold, asked, fmt):
    """(text, href, label): what happens next, or what the user does, in plain words."""
    state, code = row["state"], code_root(row["blocker"])
    next_at = overview._utc(row["next_at"]) if row["next_at"] else None
    retry = "Will try again automatically" + (" " + fmt(next_at) if next_at else "") + "."
    if owner_hold:
        if code == "SUBMISSION_OUTCOME_UNKNOWN":
            return ("Check your applicant account or confirmation email at %s to see whether it went through. "
                    "Sidedoor will not submit it again on its own." % company, None, None)
        if row["blocker"] == FORM_NOT_READABLE:
            return ("Sidedoor will not guess the questions, so it holds this application. If you want this job, open the "
                    "posting and apply yourself.", "/approvals" if asked else None, "Open Approvals" if asked else None)
        if asked:
            return ("Answer Sidedoor's question about this application on Approvals.", "/approvals", "Open Approvals")
        if code == "UNKNOWN_REQUIRED_ANSWERS":
            return ("The form asks something only you can answer. If you want this job, open the posting and apply "
                    "yourself.", None, None)
        if code == "REQUIRED_ELIGIBILITY_ANSWER":
            return ("Check the posting. If you can work there, update your work authorization in Settings.",
                    "/settings#profile", "Open Settings")
        if code == "DESIGNATED_RESUME_MISMATCH":
            return ("Upload your résumé again in Settings.", "/settings#resume", "Open Settings")
        if code == "APPLICATION_AUTHORIZATION_REQUIRED":
            # The hold is the `preapproved_applications` setting being off; no dashboard control sets it.
            return ("Sidedoor's settings do not allow it to submit applications, and there is no switch for this on "
                    "the dashboard yet. Contact support@yoursidedoor.com to turn it on.", None, None)
        return ("Open the posting and decide whether to apply yourself.", None, None)
    if state == "BLOCKED" or state == "RETRY_WAIT":
        return (retry, None, None)
    if state == "WAITING_BROWSER":
        return ("Sidedoor will continue when the browser is free" + (", next check " + fmt(next_at) if next_at else "") + ".",
                None, None)
    if state in ("SUBMITTING", "RECONCILE"):
        return ("Sidedoor is checking whether the application went through. Nothing to do.", None, None)
    if state == "QUEUED":
        return ("Sidedoor reads the posting on its next applications run.", None, None)
    return (None, None, None)


def job_view(row, owner_keys, asked, live_reading, fmt):
    payload = overview._json(row["payload"]) or {}
    company = _text(payload.get("company")) or _text(row["employer"]) or "Unknown company"
    role = _text(payload.get("role")) or _text(payload.get("title")) or "Role not recorded"
    state, code = row["state"], row["blocker"]
    route = _route(code, state)
    owner_hold = row["job_key"] in owner_keys
    reading = row["job_key"] in live_reading and state not in FINAL
    if owner_hold:
        words, tone = "Blocked, needs you", "error"
    elif reading:
        words, tone = "Reading now", "info"
    elif state == "BLOCKED":
        words, tone = "Waiting to retry", "warning"
    else:
        words, tone = STATE_WORDS.get(state, "In progress"), STATE_TONES.get(state, "muted")
    todo, href, label = _what_to_do(row, company, route, owner_hold, row["job_key"] in asked, fmt)
    problem = None
    if state in ("BLOCKED", "RETRY_WAIT", "WAITING_BROWSER") or (state in ("SUBMITTING", "RECONCILE") and code):
        problem = code_words(code, "OWNER" if owner_hold else route)
    closed = None
    if state in CLOSED_WORDS:
        closed = CODE_WORDS.get(code_root(code)) or CLOSED_WORDS[state]
    next_at = overview._utc(row["next_at"]) if row["next_at"] and state not in FINAL else None
    return {"key": row["job_key"], "company": company, "employer": row["employer"], "role": role,
            "url": _safe_url(payload.get("url")), "state": words, "reading": reading, "tone": tone, "needs_you": owner_hold,
            "problem": problem, "todo": todo, "todo_href": href, "todo_label": label, "closed": closed,
            "updated": overview._utc(row["updated_at"]), "next_at": next_at,
            "receipt": _receipt(state, row["receipt"]), "attempts": []}


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
    for row in reader.rows("SELECT at,event,detail FROM application_events WHERE job_key=? ORDER BY id DESC LIMIT ?",
                           (key, HISTORY_LIMIT), "application_events"):
        detail = overview._json(row["detail"]) or {}
        event = str(row["event"] or "")
        if event in STATE_EVENTS:
            code = detail.get("code")
            route = detail.get("route") if isinstance(detail.get("route"), str) and detail["route"] in ROUTE_WORDS else None
            text = STATE_EVENTS[event] + (": " + code_words(code, route) if code else "")
        elif event == "ELIGIBILITY_CHECKED":
            decision = detail.get("decision")
            text = "Read the posting: " + (ELIGIBLE_WORDS if decision == "ELIGIBLE" else code_words(decision))
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
                other = " ".join(value for value in (_text(payload.get("company")) or _text(same[0]["employer"]),
                                                     _text(payload.get("role")) or _text(payload.get("title"))) if value)
            return {"other": other, "reason": _text(detail.get("reason"))}
    if state == "ABANDONED":
        rows = reader.rows("SELECT detail FROM application_events WHERE job_key=? AND event='APPLICATION_ABANDONED' "
                           "ORDER BY id DESC LIMIT 1", (key,), "application_events")
        detail = overview._json(rows[0][0]) if rows else None
        if detail and _text(detail.get("reason")):
            return {"other": None, "reason": _text(detail.get("reason"))}
    return None


# --- The page ---------------------------------------------------------------------------------------

def current_resume_sha(home):
    """The résumé hash in the user's profile, or None (never an error)."""
    import user_profile
    try:
        folder = Path(home) if home is not None else user_profile.home()
        profile = json.loads((folder / "profile.json").read_text())
        sha = (profile.get("resume") or {}).get("sha256")
        return sha if isinstance(sha, str) else None
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def _rows_for(reader, keys):
    rows = {}
    for index in range(0, len(keys), 500):
        chunk = keys[index:index + 500]
        # Named columns: an older table without one fails here as a DatabaseError (the section degrades).
        for row in reader.rows("SELECT job_key,employer,payload,state,blocker,next_at,receipt,updated_at "
                               "FROM application_jobs WHERE job_key IN (%s)" % ",".join("?" * len(chunk)),
                               tuple(chunk), "application_jobs"):
            rows[row["job_key"]] = row
    return [rows[key] for key in keys if key in rows]


def listing(reader, show, page, company, query, now, start, end, fmt):
    keys = ordered_keys(reader, show, now, start, end, company, query)
    pages = max(1, math.ceil(len(keys) / PAGE_SIZE))
    page = min(max(1, page), pages)
    window = keys[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
    owner, _ = overview._blocked(reader)
    owner = set(owner)
    asked = {row[0] for row in overview._pending_questions(reader, now)}
    live = reading_keys(reader, now)
    jobs = []
    for row in _rows_for(reader, window):
        view = job_view(row, owner, asked, live, fmt)
        view["attempts"] = attempts(reader, row["job_key"])
        if row["state"] in ("DUPLICATE", "ABANDONED"):
            view["closing"] = closing_reason(reader, row["job_key"], row["state"])
        jobs.append(view)
    return {"jobs": jobs, "total": len(keys), "page": page, "pages": pages}


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
