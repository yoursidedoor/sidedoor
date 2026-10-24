import datetime as dt
import hashlib
import json
import log
import math
from pathlib import Path
import re
import sqlite3
import subprocess
import traceback
import unicodedata
import urllib.parse
import uuid
from zoneinfo import ZoneInfo

import decision_transport
import portable
import provider_limits
import user_profile


FINAL = {"SUBMITTED", "DUPLICATE", "INELIGIBLE", "EXHAUSTED", "ABANDONED"}
UNCERTAIN = {"SUBMITTING", "RECONCILE"}
# `REQUIRED_QUESTIONS_UNKNOWN` belongs here for the same reason the adapter's own two
# codes do: it is this module's conclusion that the read did not produce the complete
# form, so the answer is another read, not a recovery investigation. `apply_resolution`
# accepts only identity/history/destination, so a recovery lifecycle started on an
# unreadable form can never resolve it - it would spend its three claims and park the
# job on the orchestrator-owned exhausted hold, where the owner would never see it.
INCOMPLETE_INSPECTION = {"INSPECTION_INCOMPLETE", "FORM_REVIEW_INCOMPLETE", "REQUIRED_QUESTIONS_UNKNOWN"}

# A form that cannot be enumerated is the owner's blocker once the orchestrator's own
# bounded self-help is spent. The escalated code keeps the existing
# `UNKNOWN_REQUIRED_ANSWERS:` prefix deliberately: that prefix is what `hold` below,
# `application_recovery.user_hold` and the owner's reopen tool already read as "this
# one belongs to the user", so the escalation is refused by automatic recovery and by
# authorized reopen without a single change to either. The suffix is a reserved
# non-field token, not a fabricated question id - the required answers are unknown
# precisely because the questions were never readable.
FORM_NOT_READABLE_BLOCKER = "UNKNOWN_REQUIRED_ANSWERS:" + decision_transport.FORM_NOT_READABLE_FIELD
FORM_NOT_READABLE_EVENT = "REQUIRED_QUESTIONS_UNREADABLE"
FORM_NOT_READABLE_ESCALATED = "REQUIRED_QUESTIONS_ESCALATED"
FORM_NOT_READABLE_ACTION = ("Make the application form readable - an authenticated session or a loadable page - "
                            "or leave this application held; the required questions were never readable and "
                            "no question or answer will be invented")


# The semantics a required question carries when it is the designated resume upload, and
# the semantics that mean "when could you start", kept apart from `notice_period` on
# purpose. The owner stated two different facts - two weeks' notice owed to the current
# employer, and an earliest start four weeks after the application - and a lookup that
# let one answer the other would tell an employer they can start in two weeks, which the
# owner never said. `resolve_answers` matches on exact semantic equality, so these sets
# never overlap and neither can satisfy the other's question.
RESUME_SEMANTICS = frozenset({"resume", "resume_cv", "cv", "resume_upload", "resume_attachment", "resume_or_cv"})
# Controls that take a file; the designated resume is carried out of band, anything else the
# AI wrote text for is rendered to a PDF and attached (cover_letter).
ANSWER_FILE_TYPES = frozenset({"file", "upload", "attachment", "file_upload", "document"})
# Pre-arm blockers the job itself cannot influence: the session lacked a capability it needed.
# A submit that ends on one of these is reconciled as not submitted and costs no attempt.
# FILE_UPLOAD_FAILED added 2026-09-22. It is the Chrome file-picker case this set was written
# for - the reconciliation comment at `reconcile` names it outright, "a session that could not
# use a capability it needed (Chrome's file picker, the arm guard, a killed session)" - and it
# was simply missing from the set. Umbrella's submit stopped before arming on 2026-09-22
# because the browser extension lacked "Allow access to file URLs", so the resume could not be
# attached. Nothing reached the employer, the job could not have influenced it, and it spent
# the last of its three attempts on a browser setting.
# BROWSER_PERMISSION_REQUIRED removed (owner, 2026-09-30). BROWSER_CAPABILITY_UNAVAILABLE is the
# submit session's own report that a tool it needed could not run in this session, before arming.
CAPABILITY_BLOCKERS = frozenset({"APPLICATION_GUARD_UNAVAILABLE", "BROWSER_CAPABILITY_UNAVAILABLE",
                                 "AUTHENTICATION_REQUIRED", "CAPTCHA_REQUIRED", "MFA_REQUIRED",
                                 "FILE_UPLOAD_FAILED"})
# Upload failures are bounded (owner, 2026-10-01: "it doesnt make sense for it to continue
# retrying till infinity", about Wonka). FILE_UPLOAD_FAILED stays a capability blocker, so one
# upload failure still spends no ordinary attempt, but a job whose submit sessions have stopped
# before arming on an upload failure UPLOAD_FAILURE_LIMIT times is given up: EXHAUSTED with
# blocker UPLOAD_GIVE_UP_CODE. The count is read from the durable submit requests themselves
# (never-armed submit requests whose result's stop names an upload failure), so it is
# retroactive. RESUME_UPLOAD_FAILED is the same stop under the name sessions used before
# FILE_UPLOAD_FAILED was the only upload code (Wonka, 2026-09-29).
UPLOAD_FAILURE_CODES = frozenset({"FILE_UPLOAD_FAILED", "RESUME_UPLOAD_FAILED"})
UPLOAD_FAILURE_LIMIT = 3
UPLOAD_GIVE_UP_CODE = "FILE_UPLOAD_FAILED_3_TIMES"
UPLOAD_GIVE_UP_EVENT = "APPLICATION_UPLOAD_FAILURES_SPENT"
# The one reopen defect class whose APPLICATION_REOPENED answers earlier upload failures
# (recorded by the owner's reopen tool under that defect class; owner, 2026-10-02).
UPLOAD_RESET_REOPEN_CLASS = "EMBEDDED_FRAME_UPLOAD_20261002"
# Access blocks: three strikes (owner, 2026-10-11: "if an application retries based on captcha
# or login required x number of times, then we just abandon it? x can be 3."). An inspect,
# investigate or submit session that stops on one of these is no longer an owner hold: the job
# is retried ACCESS_BLOCK_RETRY_SECONDS later (still a capability blocker, so no ordinary attempt
# is spent), and the ACCESS_BLOCK_LIMIT-th such session abandons it. The count is read from the
# durable sessions themselves (desktop_requests), so it is retroactive, and only sessions created
# after the job's latest APPLICATION_REOPENED count. Reconcile/verify reads are not counted: their
# access blocks count toward the 10 reconcile reads (RECONCILE_ACCESS_BLOCKED), unchanged.
ACCESS_BLOCK_CODES = frozenset({"AUTHENTICATION_REQUIRED", "CAPTCHA_REQUIRED", "MFA_REQUIRED"})
ACCESS_BLOCK_LIMIT = 3
# Due at the next hourly applications run: the hold is written when the session ends, often well
# into the run, so a full hour missed the next run and waited two (review, 2026-10-11). A run
# selects its jobs once, at its start, so a short delay never retries a job twice in one run.
ACCESS_BLOCK_RETRY_SECONDS = 25 * 60
ACCESS_GIVE_UP_CODE = "ACCESS_BLOCKED_3_TIMES"
ACCESS_GIVE_UP_EVENT = "APPLICATION_ACCESS_BLOCKS_SPENT"
EARLIEST_START_SEMANTICS = frozenset({"earliest_start_date", "earliest_available_start_date",
                                      "available_start_date", "availability_date", "start_date",
                                      "when_can_you_start"})
EARLIEST_START_RULE_SEMANTIC = "earliest_start_date_rule"
NOTICE_PERIOD_SEMANTIC = "notice_period"
COMPUTED_ANSWER_SOURCE_PREFIX = "policy-rule:"

# Onsite/hybrid location exclusions come only from the profile's `onsite_location_exclusions`
# block (E1.T7, owner decision a, 2026-10-14). A profile without one excludes nothing: the rule
# below has the block's shape and no modes or places, so a new user never inherits another
# user's places. `profile.example.json` shows the shape with a full set of values.
#
# In a block, `states` are matched anywhere in the recorded location strings because a USPS code
# is unambiguous; `state_names` are matched only against a dedicated state/region field, because
# a state's name can also be a city's (Washington, DC must never match the state of
# Washington). `state_cities` is the best available approximation used when a role records no
# state at all.
NO_ONSITE_LOCATION_EXCLUSIONS = {"note": "No onsite/hybrid exclusions recorded in the profile.",
                                 "work_modes": [], "states": [], "state_names": [], "cities": [],
                                 "state_cities": []}


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def normalized(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def location_phrases(value):
    """Every one-to-four-word phrase in a recorded location string.

    Real reviews do not record a tidy city. They record `"Seattle, WA"`,
    `"Greater Seattle Area"`, `"Bellevue, WA (hybrid)"`. Matching a whole normalised
    string against a city set - which is what both hardcoded sites did - misses all three.
    Phrases are whole words, so a city name never matches a fragment of a longer word; a
    compound place name that contains an excluded city ("South San Francisco") does match,
    which is the conservative direction: it can only ever exclude more, never admit a
    location the owner ruled out.
    """
    words = [word for word in "".join(
        character if character.isalnum() or character.isspace() else " "
        for character in normalized(str(value or ""))).split() if word]
    return {" ".join(words[start:end])
            for start in range(len(words))
            for end in range(start + 1, min(start + 4, len(words)) + 1)}


def onsite_location_exclusions(policy):
    rule = (policy or {}).get("onsite_location_exclusions")
    return rule if isinstance(rule, dict) else NO_ONSITE_LOCATION_EXCLUSIONS


def excluded_onsite_location(facts, policy=None):
    """Is this role's place one the owner excluded for onsite/hybrid work?

    The owner's rule is the whole of Washington state plus the city of San Francisco, and
    it applies to onsite and hybrid roles only - US remote is unaffected anywhere. Both
    previous call sites hardcoded `{"seattle", "san francisco"}`, so Bellevue, Redmond,
    Kirkland, Tacoma and Everett all passed.

    A state rule needs a state. `research_workflow.normalize_opportunity` already carries
    `facts["state"]` through from sourced evidence, so when research recorded one it is
    used directly and the rule is exact. When it did not, two approximations stand in: the
    unambiguous abbreviation "WA" is matched anywhere in the recorded city/metro/region
    strings (this is what catches `"Seattle, WA"` and `"Bellevue, WA"`), and a named set of
    Washington cities is matched as a phrase (this is what catches `"Greater Seattle Area"`
    and a bare `"Bellevue"`). The residual gap is a Washington city outside that set whose
    role records no state; it is named in the fix report rather than hidden.

    The bare word "washington" is deliberately NOT matched against the city, because it is
    also the name of a city (Washington, DC). It is matched only against a dedicated
    state/region field, where "Washington" unambiguously means the state.
    """
    facts = facts or {}
    rule = onsite_location_exclusions(policy)
    if str(facts.get("work_mode") or "").casefold() not in {mode.casefold() for mode in rule.get("work_modes", ("onsite", "hybrid"))}:
        return None
    city_phrases = location_phrases(facts.get("city")) | location_phrases(facts.get("metro"))
    field_phrases = location_phrases(facts.get("state")) | location_phrases(facts.get("region"))
    anywhere = city_phrases | field_phrases
    for state in rule.get("states", ()):
        if normalized(str(state)) in anywhere:
            return "excluded_state:" + str(state)
    for state in rule.get("state_names", ()):
        if normalized(str(state)) in field_phrases:
            return "excluded_state:" + str(state)
    for city in rule.get("cities", ()):
        if normalized(str(city)) in city_phrases:
            return "excluded_city:" + str(city)
    for city in rule.get("state_cities", ()):
        if normalized(str(city)) in city_phrases:
            return "excluded_city_in_excluded_state:" + str(city)
    return None


# Owner rule (2026-09-19): Washington state and San Francisco are excluded for onsite/hybrid
# work, but a role that can be done from an allowed place is NOT excluded. The browser read
# reports each distinct place the role can be done from as facts.location_options
# [{city (verbatim), state (USPS code or null), country (ISO alpha-2)}]; Python decides per
# option on those structured fields and excludes the role only when EVERY option is ruled
# out. A legacy read that carries only one combined free-text city naming several places is
# never finalized on an excluded mention inside it: it is held for scope review instead.
LOCATION_SEPARATORS = re.compile(r"[;|/&\n]|,|\bor\b|\band\b", re.IGNORECASE)
LOCATION_QUALIFIERS = frozenset({"california", "washington", "washington state", "united states", "usa",
                                 "us", "u s", "area", "greater area", "bay area"})


def location_options(facts):
    """The structured location options of a read, or None when it reported none usable."""
    options = (facts or {}).get("location_options")
    if not isinstance(options, list) or not options or not all(isinstance(item, dict) for item in options):
        return None
    return options


def option_verdict(option, mode, policy):
    """ALLOWED, EXCLUDED (reason) or UNKNOWN for one place, from its structured fields only."""
    code = option.get("country") if isinstance(option.get("country"), str) and ISO_ALPHA2.fullmatch(option["country"]) else None
    state = option.get("state") if isinstance(option.get("state"), str) and ISO_ALPHA2.fullmatch(option["state"]) else None
    if code is None:
        return "UNKNOWN", None
    countries = target_countries(policy)
    if countries and code not in countries:
        return "EXCLUDED", "country " + code
    if code in US_LOCATION_CODES:
        # The city phrase logic runs within this ONE option's city; the state is a code.
        excluded = excluded_onsite_location({"city": option.get("city"), "state": state, "work_mode": mode}, policy)
        if excluded:
            return "EXCLUDED", excluded
        if not state and not str(option.get("city") or "").strip() and excluded_mode(mode, policy):
            # Owner decision 2026-09-19: a missing city blocks only when the owner exclusion
            # (Washington state, San Francisco) cannot be evaluated at all.
            return "UNKNOWN", "onsite/hybrid US place with no city or state"
    return "ALLOWED", None


def legacy_multiple_places(city, policy):
    """True when one free-text city names a place other than the excluded one it mentions."""
    text = re.sub(r"\([^)]*\)", " ", str(city or ""))
    parts = LOCATION_SEPARATORS.split(text)
    if len(parts) < 2:
        return False
    for part in parts:
        phrase = normalized(" ".join("".join(ch if ch.isalnum() else " " for ch in part).split()))
        if not phrase or len(phrase) <= 2 or phrase in LOCATION_QUALIFIERS:
            continue
        if not excluded_onsite_location({"city": phrase, "work_mode": "onsite"}, policy):
            return True
    return False


def excluded_mode(mode, policy=None):
    rule = onsite_location_exclusions(policy)
    return str(mode or "").casefold() in {item.casefold() for item in rule.get("work_modes", ("onsite", "hybrid"))}


def location_exclusion(facts, policy=None):
    """None, ("EXCLUDED", reason) - final - or ("REVIEW", reason) - a non-final scope hold."""
    facts = facts or {}
    rule = onsite_location_exclusions(policy)
    mode = str(facts.get("work_mode") or "")
    options = location_options(facts)
    if options is not None:
        verdicts = [option_verdict(option, mode, policy) for option in options]
        if any(verdict == "ALLOWED" for verdict, _ in verdicts):
            return None
        if all(verdict == "EXCLUDED" for verdict, _ in verdicts):
            return "EXCLUDED", "every location option excluded: " + "; ".join(reason for _, reason in verdicts)
        return "REVIEW", "no location option is known to be allowed: " + "; ".join(
            verdict + (":" + reason if reason else "") for verdict, reason in verdicts)
    if mode.casefold() not in {item.casefold() for item in rule.get("work_modes", ("onsite", "hybrid"))}:
        return None
    excluded = excluded_onsite_location(facts, policy)
    if (not excluded and country_code(facts) in US_LOCATION_CODES
            and not str(facts.get("city") or "").strip() and not str(facts.get("state") or "").strip()):
        # Owner decision 2026-09-19: a missing city does not block when a state code or the
        # location options decide the owner exclusion; with neither there is nothing to
        # evaluate Washington state / San Francisco against, so hold (never final).
        return "REVIEW", "onsite/hybrid US role with no city, state or location options to evaluate the owner exclusion"
    if not excluded:
        return None
    if legacy_multiple_places(facts.get("city"), policy):
        return "REVIEW", ("location text " + repr(str(facts.get("city")))[:120]
                          + " names several places and no structured location options were read (" + excluded + ")")
    return "EXCLUDED", excluded


def initialize(database):
    database.executescript("""
        CREATE TABLE IF NOT EXISTS application_jobs (
            job_key TEXT PRIMARY KEY, employer TEXT NOT NULL, requisition TEXT NOT NULL,
            payload TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            reconcile_attempts INTEGER NOT NULL DEFAULT 0, next_at TEXT,
            blocker TEXT, next_action TEXT, receipt TEXT, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS application_attempts (
            attempt_id TEXT PRIMARY KEY, job_key TEXT NOT NULL, idempotency_key TEXT NOT NULL,
            state TEXT NOT NULL, started_at TEXT NOT NULL, evidence TEXT);
        CREATE TABLE IF NOT EXISTS application_history (
            employer TEXT NOT NULL, requisition TEXT NOT NULL, kind TEXT NOT NULL,
            evidence TEXT NOT NULL, PRIMARY KEY(employer,requisition,kind));
        CREATE TABLE IF NOT EXISTS application_events (
            id INTEGER PRIMARY KEY, job_key TEXT NOT NULL, at TEXT NOT NULL,
            event TEXT NOT NULL, detail TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS application_events_event_at ON application_events(event, at);
        -- The dashboard's Applications page (E6.T6): its list, filters and per-job detail.
        CREATE INDEX IF NOT EXISTS application_events_job ON application_events(job_key);
        CREATE INDEX IF NOT EXISTS application_jobs_updated ON application_jobs(updated_at);
        CREATE INDEX IF NOT EXISTS application_jobs_state ON application_jobs(state, updated_at);
        CREATE INDEX IF NOT EXISTS application_jobs_employer ON application_jobs(employer, updated_at);
        CREATE INDEX IF NOT EXISTS application_attempts_job ON application_attempts(job_key, started_at);
        CREATE TABLE IF NOT EXISTS application_reviews (
            id TEXT PRIMARY KEY, job_key TEXT NOT NULL, observed_at TEXT NOT NULL,
            eligibility TEXT NOT NULL, evidence TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS application_reviews_job ON application_reviews(job_key, observed_at);
        CREATE TABLE IF NOT EXISTS notification_outbox (
            event_key TEXT PRIMARY KEY, message TEXT NOT NULL, state TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, next_at TEXT, delivery TEXT,
            last_error TEXT, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS application_aliases (
            alias_key TEXT PRIMARY KEY, canonical_key TEXT NOT NULL, evidence TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS application_source_keys (
            source_key TEXT PRIMARY KEY, job_key TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS application_blockers (
            job_key TEXT PRIMARY KEY, code TEXT NOT NULL, owner TEXT NOT NULL,
            required_input TEXT NOT NULL, attempts INTEGER NOT NULL,
            next_retry TEXT, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS application_answers (
            job_key TEXT NOT NULL, field TEXT NOT NULL, semantic TEXT NOT NULL,
            value TEXT NOT NULL, source TEXT NOT NULL, PRIMARY KEY(job_key,field));
        CREATE TABLE IF NOT EXISTS application_requisition_records (
            job_key TEXT PRIMARY KEY, employer TEXT NOT NULL, requisition TEXT NOT NULL,
            identity_sha256 TEXT NOT NULL, contract_revision TEXT NOT NULL,
            questions TEXT NOT NULL, form_fingerprint TEXT,
            conditional_questions TEXT NOT NULL DEFAULT '[]', review TEXT NOT NULL,
            complete INTEGER NOT NULL, blocker TEXT, missing_fields TEXT NOT NULL,
            input_sha256 TEXT NOT NULL, observed_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS application_company_facts (
            employer TEXT PRIMARY KEY, facts TEXT NOT NULL, facts_sha256 TEXT NOT NULL,
            updated_at TEXT NOT NULL);
    """)
    if "result" not in {row[1] for row in database.execute("PRAGMA table_info(application_attempts)")}:
        database.execute("ALTER TABLE application_attempts ADD COLUMN result TEXT")
        database.commit()
    if "reconcile_attempts" not in {row[1] for row in database.execute("PRAGMA table_info(notification_outbox)")}:
        database.execute("ALTER TABLE notification_outbox ADD COLUMN reconcile_attempts INTEGER NOT NULL DEFAULT 0")
        database.commit()
    import decision_transport
    decision_transport.initialize(database)
    import external_history
    external_history.initialize(database)


# One definition of "this requisition is not a canonical employer/ATS identifier".
# `unresolved:<hash>` has always meant that; `url-identity:<hash>` joins it (owner, 2026-09-24,
# approach A: the canonical posting URL becomes the identity when an employer publishes no
# requisition). Every conservative check in the system asks this question, and asking it in one
# place is what stops a weak identity ever being mistaken for proof of a DIFFERENT posting -
# which is the one way approach A could cause a second application to a role already applied to.
WEAK_IDENTITY_PREFIXES = ("unresolved:", "url-identity:")


def weak_identity(requisition):
    return isinstance(requisition, str) and requisition.startswith(WEAK_IDENTITY_PREFIXES)


def identity(role):
    employer = normalized(role["company"])
    parsed = urllib.parse.urlsplit(role["url"])
    parts = [part for part in parsed.path.split("/") if part]
    requisition = str(role.get("requisition_id") or "").strip().casefold()
    if not requisition:
        if parsed.hostname == "jobs.ashbyhq.com" and len(parts) >= 2:
            requisition = parts[1].casefold()
        elif parsed.hostname in {"job-boards.greenhouse.io", "boards.greenhouse.io"} and "jobs" in parts:
            index = parts.index("jobs") + 1
            if index < len(parts):
                requisition = parts[index].casefold()
        elif parsed.hostname == "jobs.lever.co" and len(parts) >= 2:
            requisition = parts[1].casefold()
    if not requisition:
        canonical = urllib.parse.urlunsplit(("https", parsed.netloc.lower(), parsed.path.rstrip("/"), "", ""))
        requisition = "unresolved:" + hashlib.sha256(canonical.encode()).hexdigest()[:24]
    return employer, requisition, employer + ":" + requisition


def event(database, key, kind, detail, now):
    database.execute("INSERT INTO application_events(job_key,at,event,detail) VALUES (?,?,?,?)",
                     (key, now, kind, json.dumps(detail)))


def notify(database, key, message, now):
    database.execute("INSERT OR IGNORE INTO notification_outbox(event_key,message,state,updated_at) VALUES (?,?,'PENDING',?)",
                     (key, message, now))


def posting_source_key(role):
    """The posting's URL-derived identity (host/board/id), or None for a weak identity."""
    parsed = urllib.parse.urlsplit(role["url"])
    inferred = identity(dict(role, requisition_id=""))[1]
    if weak_identity(inferred):
        return None
    hostname = "greenhouse.io" if parsed.hostname in {"boards.greenhouse.io", "job-boards.greenhouse.io"} else parsed.hostname
    board = parsed.path.strip("/").split("/")[0].casefold()
    return f"{hostname}/{board}/{inferred}"


def ingest(database, role, now=None, source="research"):
    """Record a posting as a job to apply to; an existing job for the same posting is returned.

    `source` names who found it: research, or the reply AI queueing a link a contact sent
    (owner, 2026-10-06: a job the reply agent queues is applied to automatically).
    """
    now = now or stamp()
    employer, requisition, key = identity(role)
    source_key = posting_source_key(role)
    if source_key:
        existing_source = database.execute("SELECT job_key FROM application_source_keys WHERE source_key=?", (source_key,)).fetchone()
        if existing_source:
            return existing_source[0]
    alias = database.execute("SELECT canonical_key FROM application_aliases WHERE alias_key=?", (key,)).fetchone()
    if alias:
        return alias[0]
    row = database.execute("INSERT OR IGNORE INTO application_jobs(job_key,employer,requisition,payload,state,updated_at) VALUES (?,?,?,?,'QUEUED',?)",
                           (key, employer, requisition, json.dumps(role), now))
    if row.rowcount:
        event(database, key, "DISCOVERED", {"url": role["url"], "source": source}, now)
    if source_key:
        database.execute("INSERT OR IGNORE INTO application_source_keys VALUES (?,?)", (source_key, key))
    return key


def register_posting_keys(database, role, job_key, evidence):
    """Record that `job_key` now owns the posting at role["url"] (owner, 2026-09-30).

    A job re-recorded onto the employer's ATS page (identity_reconciliation) must own that
    page's URL-derived identity exactly as `ingest` would have given it, so a later copy found
    by that URL resolves to this job instead of becoming a second application. Refuses when
    another job already owns it: that is a real conflict, not something to overwrite.
    """
    url_key = identity(dict(role, requisition_id=""))[2]
    source_key = posting_source_key(role)
    if source_key is None:
        return
    owner = database.execute("SELECT job_key FROM application_source_keys WHERE source_key=?", (source_key,)).fetchone()
    if owner and owner[0] != job_key:
        raise ValueError("Posting already recorded under another job")
    if url_key != job_key:
        add_alias(database, dict(role, requisition_id=""), job_key, evidence)
    database.execute("INSERT OR IGNORE INTO application_source_keys VALUES (?,?)", (source_key, job_key))


def add_alias(database, alias_role, canonical_key, evidence):
    alias_key = identity(alias_role)[2]
    if not evidence or not database.execute("SELECT 1 FROM application_jobs WHERE job_key=?", (canonical_key,)).fetchone():
        raise ValueError("Verified canonical job and alias evidence required")
    prior = database.execute("SELECT canonical_key FROM application_aliases WHERE alias_key=?", (alias_key,)).fetchone()
    if prior and prior[0] != canonical_key:
        raise ValueError("Conflicting identity alias")
    existing = database.execute("SELECT * FROM application_jobs WHERE job_key=?", (alias_key,)).fetchone()
    if existing and alias_key != canonical_key:
        raise ValueError("Existing job identity needs explicit ledger reconciliation, not silent alias replacement")
    database.execute("INSERT OR IGNORE INTO application_aliases VALUES (?,?,?)", (alias_key, canonical_key, json.dumps(evidence)))


# Where a held job goes next, DECLARED rather than inferred (owner, 2026-09-22).
#   ORDINARY  - the inspect/answer/submit path
#   RECOVER   - application_recovery.recover, the bounded investigation lifecycle
#   RECONCILE - establish the outcome of a submission that may have happened
#   OWNER     - nothing automatic can proceed; the owner must act
ROUTES = frozenset({"ORDINARY", "RECOVER", "RECONCILE", "OWNER"})


# Every outcome a submit can leave behind, all of them worth telling the owner about.
UNCERTAIN_SUBMISSION_CODES = frozenset({"SUBMISSION_OUTCOME_UNKNOWN", "SUBMISSION_REPORTED_FAILED",
                                        "SUBMISSION_OBSERVATION_LOST"})
# ...but only these two must ALWAYS reconcile. SUBMISSION_REPORTED_FAILED is deliberately absent:
# while the attempt is live, `job_route`'s own `outstanding` term already forces RECONCILE, and
# once reconciliation has resolved it the job SHOULD reach recovery so the coordinator can pursue
# the route the page named. Listing it here too would contradict its RECOVERABLE membership -
# test_recoverable_codes_route_to_recovery_when_nothing_is_outstanding catches exactly that.
ALWAYS_RECONCILE_CODES = frozenset({"SUBMISSION_OUTCOME_UNKNOWN", "SUBMISSION_OBSERVATION_LOST"})


def job_route(code, state, outstanding=False):
    """THE single authority for where a held job goes next. One function, two inputs.

    Owner, 2026-09-22. `process` used to make this decision in three separate places, each
    testing a different half of the signal - `RECOVERABLE` membership here, `UNCERTAIN` and
    SUBMISSION_OUTCOME_UNKNOWN there, `user_hold` somewhere else - so no one place knew where a
    job was going and three subsystems could bounce it between them. Umbrella went ordinary
    -> reconcile -> recover -> ordinary -> recover in one afternoon.

    A survey of all ~40 blocker-setting call sites showed why a per-CODE table cannot work: 21
    of them build the code at runtime (`gate[1]`, `blocker["code"]`, `str(error)`,
    `decision[1]`), and the same code legitimately routes differently depending on where it was
    set - AUTHENTICATION_REQUIRED is RECONCILE from the reconcile path and ORDINARY from the
    review path (OWNER before 2026-10-11: access blocks are retried, then abandoned). What disambiguates them is the STATE held alongside the code, which the old
    conditions already used without ever saying so. So the route is a function of the pair, and
    this is that function.

    `outstanding` is a live SUBMITTING attempt or an unconsumed submit request: a job that may
    already have reached an employer must establish that before anything else, whatever its
    blocker says.
    """
    import application_recovery
    # SUBMISSION_REPORTED_FAILED and SUBMISSION_OBSERVATION_LOST split out of
    # SUBMISSION_OUTCOME_UNKNOWN on 2026-09-24 and carry exactly its routing: a submit that may
    # have reached an employer reconciles before anything else, whatever the page said.
    if outstanding or state in UNCERTAIN or code in ALWAYS_RECONCILE_CODES:
        return "RECONCILE"
    if application_recovery.user_hold(code):
        return "OWNER"
    if code in application_recovery.RECOVERABLE:
        return "RECOVER"
    return "ORDINARY"


def inferred_route(code, state):
    """Back-compat alias used by `hold` when a caller declares no route."""
    return job_route(code, state)


# A route-cycle detector was built here on 2026-09-22 and removed the same day. Two versions
# were tried: counting route flips at an unchanged attempt count, and bounding the elapsed time
# across them. Both fired on healthy behaviour. The flip counter hit normal recovery, whose
# real trace is ORDINARY, RECOVER, ORDINARY, RECOVER, ORDINARY, RECOVER - alternation is how
# recovery works, and investigations do not spend attempts. The time bound hit every rehearsal
# that simulates hours or days, which is most of them.
#
# The lesson is that this system ALREADY bounds circulation - recovery deadlines, the attempt
# limit, reconcile backoff with its own max - and umbrella was not missing a bound. It was
# that one could not fire: coordinator suppression was checked before the deadline branch, so
# an expired lifecycle could neither finish nor be reopened. That is fixed in FD. A fourth,
# cruder bound on top would halt healthy jobs, which is the defect class this project spent
# 2026-09-22 removing. If circulation needs watching, watch it in the REPORT, where a false
# positive costs a line of text instead of an application.


def hold(database, job, code, action, now, state="BLOCKED", next_at=None, route=None):
    previous = database.execute("SELECT state,blocker FROM application_jobs WHERE job_key=?", (job["job_key"],)).fetchone()
    if not previous or previous["state"] in FINAL:
        return False
    # An explicit route is the caller saying where this job goes; omitting it falls back to the
    # old inference so unconverted sites keep working AND start recording what they meant.
    if route is None:
        route = inferred_route(code, state)
    elif route not in ROUTES:
        raise ValueError("UNKNOWN_ROUTE:" + str(route))
    changed = database.execute("UPDATE application_jobs SET state=?,blocker=?,next_action=?,next_at=?,updated_at=? WHERE job_key=? AND state NOT IN ('SUBMITTED','DUPLICATE','INELIGIBLE','EXHAUSTED','ABANDONED')",
                               (state, code, action, next_at, now, job["job_key"]))
    if changed.rowcount != 1:
        return False
    if previous["state"] != state or previous["blocker"] != code:
        # `route` and `previous_route` ride along so a flip between subsystems is in the ledger
        # instead of being reconstructed by hand from four tables, which is what an afternoon
        # of umbrella cost on 2026-09-22.
        event(database, job["job_key"], state, {"code": code, "next_action": action,
                                                "route": route, "attempts": job["attempts"]}, now)
    owner = "user" if code.startswith("UNKNOWN_REQUIRED_ANSWERS:") or code in {"REQUIRED_ELIGIBILITY_ANSWER", "DESIGNATED_RESUME_MISMATCH", "AUTHENTICATION_REQUIRED", "CAPTCHA_REQUIRED", "MFA_REQUIRED", "APPLICATION_AUTHORIZATION_REQUIRED"} else "orchestrator"
    database.execute("INSERT INTO application_blockers VALUES (?,?,?,?,?,?,?) ON CONFLICT(job_key) DO UPDATE SET code=excluded.code,owner=excluded.owner,required_input=excluded.required_input,attempts=excluded.attempts,next_retry=excluded.next_retry,updated_at=excluded.updated_at",
                     (job["job_key"], code, owner, action, job["attempts"], next_at, now))
    if owner == "user" or state == "EXHAUSTED" or code in UNCERTAIN_SUBMISSION_CODES:
        notify(database, job["job_key"] + ":" + code,
               f"Job search: {job['employer']} / {job['requisition']}: {code}. Attempts: {job['attempts']}. Owner: {owner}. Next: {action}. Deadline: {next_at or 'before this application can proceed'}.", now)
    return True


def fresh(record, now, max_age=86400):
    try:
        age = (dt.datetime.fromisoformat(now) - dt.datetime.fromisoformat(record["observed_at"])).total_seconds()
        return bool(record["source"]) and -60 <= age <= max_age and record.get("verified") is True
    except (KeyError, TypeError, ValueError):
        return False


# What an observed posting or form has to say before a jurisdiction the owner is not
# authorized in stops being worth applying to. Umbrella's own form says it verbatim -
# "At this time, Umbrella is unable to provide employment sponsorship" - so this is an
# observation, not an inference.
SPONSORSHIP_UNAVAILABLE = frozenset({"unavailable", "not available", "not_available", "none", "no",
                                     "false", "not offered", "not_offered", "no sponsorship",
                                     "no_sponsorship", "unable to sponsor", "unable_to_sponsor",
                                     "sponsorship_unavailable", "will not sponsor", "will_not_sponsor"})


# The jurisdiction vocabulary, owned by the deterministic layer because two independent
# gates read it: `resolve_answers` below, and the AI answering step. Keeping one copy is the
# point - a second copy is a second thing to forget to update, and this table is the only
# thing standing between "authorized in the US" and a form that asked about Germany.
#
# It is a closed vocabulary of place names, not a reader of propositions. Nothing here tries
# to decide what a question is asking; it only answers "which jurisdictions does this text
# name", which is a question about a fixed list.
JURISDICTION_TOKENS = {
    "united states": "US", "united states of america": "US", "usa": "US", "u s": "US",
    "u s a": "US", "us": "US", "america": "US", "american": "US", "stateside": "US",
    "european union": "EU", "eu": "EU", "e u": "EU", "europe": "EU", "european": "EU",
    "eea": "EU", "european economic area": "EU", "schengen": "EU",
    "germany": "DE", "german": "DE", "deutschland": "DE",
    "spain": "ES", "spanish": "ES", "espana": "ES",
    "france": "FR", "french": "FR",
    "ireland": "IE", "irish": "IE", "republic of ireland": "IE",
    "netherlands": "NL", "dutch": "NL", "holland": "NL", "the netherlands": "NL",
    "united kingdom": "GB", "uk": "GB", "u k": "GB", "great britain": "GB", "britain": "GB",
    "british": "GB", "england": "GB", "scotland": "GB", "wales": "GB", "northern ireland": "GB",
    "switzerland": "CH", "swiss": "CH",
    "norway": "NO", "norwegian": "NO",
    "canada": "CA", "canadian": "CA",
    "portugal": "PT", "poland": "PL", "denmark": "DK", "sweden": "SE", "italy": "IT",
    "belgium": "BE", "austria": "AT", "finland": "FI", "greece": "GR", "romania": "RO",
    "czechia": "CZ", "czech republic": "CZ", "hungary": "HU", "estonia": "EE",
    "australia": "AU", "new zealand": "NZ", "india": "IN", "singapore": "SG", "japan": "JP",
    "mexico": "MX", "brazil": "BR", "israel": "IL", "south africa": "ZA",
}

# Which jurisdiction each recorded authorization answer is *about*. This is the owner's own
# declaration of scope, not an inference from an employer's wording, and it is what makes the
# guard in `resolve_answers` a statement about the answer rather than a guess about the
# question. `outside_us` is the one scope expressed as a complement.
JURISDICTION_SCOPED_SEMANTICS = {
    "currently_authorized_to_work_in_us": frozenset({"US"}),
    "requires_visa_sponsorship_now_or_future_in_us": frozenset({"US"}),
    "currently_authorized_to_work_in_eu": frozenset({"EU"}),
    "currently_authorized_to_work_in_uk": frozenset({"GB"}),
    "currently_authorized_to_work_in_canada": frozenset({"CA"}),
}
OUTSIDE_US_SCOPED_SEMANTICS = frozenset({"requires_visa_sponsorship_now_or_future_outside_us"})
# A recorded semantic that reads as a right-to-work claim but is not in the scope map above is
# UNCLASSIFIED, and an unclassified jurisdictional record does not bind - the same fail-closed
# discipline `decision_transport.CLASSIFIED_POLICY_KEYS` applies to policy keys. A future
# `currently_authorized_to_work_in_japan` must be classified deliberately rather than inherit
# a permissive default. The willingness semantics below read as authorization-adjacent and are
# classified explicitly as not being claims of authorization at all.
JURISDICTIONAL_SEMANTIC_PATTERN = re.compile(
    r"(authoriz|authoris|work_permit|right_to_work|sponsor|visa|citizen|passport|residen"
    r"|immigration|nationality|green_card)", re.IGNORECASE)
NON_JURISDICTIONAL_AUTHORIZATION_SEMANTICS = frozenset({
    "open_to_employer_visa_sponsorship",
    "willing_to_work_remotely_from_country_of_residence_for_foreign_employer",
})
# A recorded object that is a lookup table or a rule, never a value to type into a form.
NEVER_A_FORM_ANSWER_SEMANTICS = frozenset({"work_authorization_by_jurisdiction",
                                           "work_location_declaration_rule"})


# The literals that read as a bare assertion, and the wording that negates one. Both live
# here rather than in `answering_agent` because `resolve_answers` needs them too: an answer
# the answering step withholds must not be submittable through the matcher, and that is only
# true if both consult the same check.
AFFIRMATIVE_LITERALS = frozenset({"yes", "y", "true", "1", "agree", "i agree", "accept",
                                  "authorized", "authorised", "eligible", "permitted",
                                  "confirmed", "correct", "affirmative", "yes i am",
                                  "yes i do", "yes i have", "yes i will", "of course"})
NEGATIVE_LITERALS = frozenset({"no", "n", "false", "0", "decline", "i decline", "disagree",
                               "not authorized", "not authorised", "negative", "no i am not",
                               "no i do not", "none"})
BOOLEAN_ANSWER_TYPES = frozenset({"boolean", "bool", "checkbox", "yes_no", "yesno"})
def employer_wording(question):
    """The employer's own words for this question, falling back to the authored semantic.

    The `label` is preferred deliberately. The `semantic` is authored freely by the browser
    AI and is the string that misleads: `currently_authorized_to_work_in_us` contains the
    token `us`, so letting it vote on "which jurisdiction does this question name" would make
    the guard agree with the very claim it exists to refuse. It is used only when the read
    recorded no label at all, where refusing too often is the safe direction.
    """
    question = question if isinstance(question, dict) else {}
    label = str(question.get("label") or "").strip()
    if label:
        return label + " " + str(question.get("question") or "")
    return str(question.get("semantic") or "").replace("_", " ")


def reads_as_affirmative(value):
    if isinstance(value, bool):
        return value is True
    if isinstance(value, (int, float)):
        return value == 1
    if not isinstance(value, str):
        return False
    return normalized(value).rstrip(".!") in AFFIRMATIVE_LITERALS


def reads_as_negative(value):
    if isinstance(value, bool):
        return value is False
    if isinstance(value, (int, float)):
        return value == 0
    if not isinstance(value, str):
        return False
    return normalized(value).rstrip(".!") in NEGATIVE_LITERALS


def bounded_assertion(value, question):
    """Is this answer an assertion the employer reads as yes-or-no?

    Used only to decide whether polarity matters, so it errs towards "yes, it is an
    assertion": a control with a choice list, a boolean value, or a literal that reads either
    way all count. A name or an email address does not, which is why a Spanish name field is
    not caught by the English-only polarity net.
    """
    question = question if isinstance(question, dict) else {}
    if isinstance(value, bool) or reads_as_affirmative(value) or reads_as_negative(value):
        return True
    raw = str(question.get("answer_type") or "").strip().casefold().replace(" ", "_")
    if raw in BOOLEAN_ANSWER_TYPES:
        return True
    # A choice control counts only when its own options are a yes/no pair. A country dropdown
    # is a choice control too, and "United States" typed into it is a quotation of a recorded
    # fact, not an assertion whose polarity could invert.
    choices = question.get("choices") if isinstance(question.get("choices"), list) else []
    return (any(reads_as_affirmative(choice) for choice in choices)
            and any(reads_as_negative(choice) for choice in choices))


def fold_accents(value):
    """Casefolded and accent-stripped, so "Espana" and "Espana" are one token."""
    return "".join(character for character in unicodedata.normalize("NFKD", str(value or ""))
                   if not unicodedata.combining(character))


def jurisdictions_named(*texts):
    """Every jurisdiction the supplied text names, as recorded-map keys."""
    found = set()
    for text in texts:
        if not isinstance(text, str) or not text.strip():
            continue
        for phrase in location_phrases(fold_accents(text).replace("_", " ")):
            code = JURISDICTION_TOKENS.get(phrase)
            if code:
                found.add(code)
    return found


def jurisdiction_of(value):
    """The map key for a recorded country name or code, or None if unrecognisable."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = normalized(fold_accents(value))
    if len(text) == 2 and text.upper() in set(JURISDICTION_TOKENS.values()):
        return text.upper()
    for phrase in location_phrases(fold_accents(value)):
        code = JURISDICTION_TOKENS.get(phrase)
        if code:
            return code
    return None


def question_text(question):
    """Everything the read recorded about what this question says."""
    question = question if isinstance(question, dict) else {}
    return " ".join(str(question.get(field) or "").replace("_", " ")
                    for field in ("label", "semantic", "question"))


def recorded_answer_for_ai(saved, question=None):
    """Is this recorded fact one the answering AI must apply, rather than Python by semantic?

    Owner, 2026-10-07 (item 6): "removed every single one of them and replace with AI instead".
    Whether a recorded answer fits another question - its polarity, its jurisdiction - is a
    judgement about the employer's wording, which the regex and the jurisdiction lexicon in
    `recorded_answer_conflict` used to make. Python now copies a recorded fact onto a question
    by semantic only when the fact is quoted, not asserted (name, email, phone, location,
    notice period). Everything else goes to the answering AI, which has every recorded fact:
    - a yes/no value of any form (a boolean, 1/0, "Yes"/"No"), or any value put to a yes/no
      control (`bounded_assertion`, which reads the control's type and options, not its words);
    - an answer the AI itself gave to another question ("ai-answer:"), e.g. an employer's choice
      literal: it was written for that question's wording (review round 3);
    - any authorization, sponsorship or jurisdiction fact, and the owner's rule objects;
    - a value for a menu (a control with options) that is not exactly one of its options (owner,
      2026-10-22, J1: a recorded "Male" on "Man / Woman / Decline to state"): the AI picks the
      option from the recorded value and the judge checks it. No synonym or fuzzy matching; the
      exact-equals test only decides whether the AI is needed.
    """
    saved = saved if isinstance(saved, dict) else {}
    semantic, value = saved.get("semantic"), saved.get("value")
    if str(saved.get("source") or "").startswith(decision_transport.OWNER_ANSWER_SOURCE):
        # An answer the owner gave to one job's question on the dashboard, kept for later jobs
        # (owner, 2026-10-22, "a"): the AI judges whether it fits another question ("relocate to
        # Austin for this role" is not copied blindly); Python never applies it by semantic.
        return True
    choices = question.get("choices") if isinstance(question, dict) else None
    if isinstance(choices, list) and choices and value not in choices:
        return True
    if (bounded_assertion(value, question) or semantic in NEVER_A_FORM_ANSWER_SEMANTICS
            or str(saved.get("source") or "").startswith("ai-answer:")):
        return True
    if semantic in JURISDICTION_SCOPED_SEMANTICS or semantic in OUTSIDE_US_SCOPED_SEMANTICS:
        return True
    return (isinstance(semantic, str) and semantic not in NON_JURISDICTIONAL_AUTHORIZATION_SEMANTICS
            and bool(JURISDICTIONAL_SEMANTIC_PATTERN.search(semantic)))


def kept_answer(answer):
    """An answer the owner gave on the dashboard and kept for later jobs (setup_steps.save_owner_answer)."""
    return isinstance(answer, dict) and str(answer.get("source") or "").startswith(decision_transport.OWNER_ANSWER_SOURCE)


def answers_in_force(answers):
    """The recorded answers with the newer one winning per semantic (owner, 2026-10-22, D1): a kept
    answer was given after every answer that has no recorded time (Setup's, the imported ones), so it
    supersedes them for its semantic, and among kept answers the latest `recorded_at` wins. A Setup save
    that changes a semantic's value removes the kept answers it contradicts (setup_steps.profile_document),
    so a later Setup change wins in turn. Answers for one job (application_answers, an authenticated
    owner decision for that question) are never superseded. Order is kept."""
    return _answers_in_force(answers)[0]


def with_current_kept_answers(policy):
    """`policy` with the answers the owner kept from Approvals as profile.json holds them NOW
    (setup_steps.save_owner_answer; owner, 2026-10-24: "saved answers need to reach every single
    worker that actually consumes my packet"). The applications job loads the profile once per run
    and a run can last longer than an hour, so an answer saved on Approvals during a run never
    reached a later job of that run (blueprints ai, 2026-10-24: saved 09:17, asked again 09:33 by
    the run that started 09:15). Only the kept answers are replaced, added or removed; every other
    recorded answer and setting is as given. A profile that cannot be read leaves `policy` as it is.
    Returns a new dict; `policy` is not changed."""
    try:
        document = json.loads(user_profile.profile_path().read_text())
    except Exception:
        return policy
    current = document.get("answers") if isinstance(document, dict) else None
    if not isinstance(policy, dict) or not isinstance(current, dict):
        return policy
    answers = {key: answer for key, answer in (policy.get("answers") or {}).items() if not kept_answer(answer)}
    answers.update((key, answer) for key, answer in current.items() if kept_answer(answer))
    return dict(policy, answers=answers)


def kept_answers_of(policy):
    """The kept Approvals answers in `policy`, for telling whether they changed."""
    return {key: answer for key, answer in ((policy or {}).get("answers") or {}).items() if kept_answer(answer)}


# Answers recorded for one job (application_answers): never superseded per semantic (`answers_in_force`).
PER_JOB_ANSWER_SOURCES = ("ai-answer:", "owner-declaration:", COMPUTED_ANSWER_SOURCE_PREFIX, "authenticated-owner-decision:")


def answers_for_model(answers):
    """The recorded answers a model is handed: only the answer in force per semantic (newest wins,
    `answers_in_force`, the rule `policy_answer` applies for eligibility), so a Setup entry and a
    newer answer kept from Approvals for the same semantic never both reach it with nothing saying
    which one holds (owner, 2026-10-24). Answers without a semantic, and a job's own answers
    (PER_JOB_ANSWER_SOURCES), are passed as they are. Order is kept."""
    answers = answers if isinstance(answers, dict) else {}
    in_force = answers_in_force(answers)
    return {key: answer for key, answer in answers.items() if key in in_force or (
        isinstance(answer, dict) and str(answer.get("source") or "").startswith(PER_JOB_ANSWER_SOURCES))}


def policy_for_model(policy):
    """`policy` with its answers narrowed by `answers_for_model`; a new dict."""
    if not isinstance(policy, dict) or not isinstance(policy.get("answers"), dict):
        return policy
    return dict(policy, answers=answers_for_model(policy["answers"]))


NOT_USED_NEWER = "NEWER_KEPT_ANSWER"           # a newer kept answer for the same semantic is used
NOT_USED_NOT_YES_NO = "NOT_YES_NO"             # not a yes/no, and the profile records a yes/no for it


def kept_answer_status(answers):
    """{kept key: None (in force) or why it is not used (NOT_USED_NEWER, NOT_USED_NOT_YES_NO)}, for
    Settings (E6.T4 item 5)."""
    return _answers_in_force(answers)[1]


def _answers_in_force(answers):
    answers = answers if isinstance(answers, dict) else {}
    kept = {key: answer for key, answer in answers.items() if kept_answer(answer)}
    if not kept:
        return answers, {}
    boolean_semantics = {answer.get("semantic") for answer in answers.values()
                         if isinstance(answer, dict) and not kept_answer(answer) and isinstance(answer.get("value"), bool)}
    status, newest = {}, {}
    for key, answer in kept.items():
        semantic = answer.get("semantic")
        # A kept value that is not a yes/no never supersedes a recorded yes/no for its semantic (E6.T4
        # A1): the eligibility reads test `is True` / `is False`. Then the newest kept BOOLEAN for that
        # semantic is the one in force (item 1), if there is one.
        if semantic in boolean_semantics and not isinstance(answer.get("value"), bool):
            status[key] = NOT_USED_NOT_YES_NO
            continue
        if semantic not in newest or str(answer.get("recorded_at") or "") >= str(newest[semantic][1].get("recorded_at") or ""):
            if semantic in newest:
                status[newest[semantic][0]] = NOT_USED_NEWER
            newest[semantic] = (key, answer)
            status[key] = None
        else:
            status[key] = NOT_USED_NEWER
    dropped = {key for key, reason in status.items() if reason == NOT_USED_NOT_YES_NO}
    in_force = {key: answer for key, answer in answers.items() if key not in dropped
                and not (isinstance(answer, dict) and answer.get("semantic") in newest and newest[answer["semantic"]][0] != key
                         and not str(answer.get("source") or "").startswith("authenticated-owner-decision:"))}
    return in_force, status


def policy_answer(policy, semantic, default=None):
    """The recorded value for a semantic, looked up by semantic rather than by key.

    The key a preference happens to be filed under is an editorial choice; the semantic is
    the contract `resolve_answers` already matches on. Looking up by semantic means a
    rename of the key cannot silently turn a recorded owner position into a default.
    """
    # Kept dashboard answers count here too (owner, 2026-10-22, D1), the newer answer winning per
    # semantic (`answers_in_force`).
    for answer in answers_in_force((policy or {}).get("answers")).values():
        if isinstance(answer, dict) and answer.get("semantic") == semantic and answer.get("source"):
            return answer.get("value")
    return default


def employer_refuses_sponsorship(review):
    """Did the read actually observe the employer saying it will not sponsor?

    Only an observation counts. Absence of the observation is not a refusal, and is not
    treated as one: the owner's instruction is to pursue these roles and let the employer
    decide.
    """
    facts = review.get("facts", {}) or {}
    if stated_requirements(review) & SPONSORSHIP_REFUSALS:
        return True
    for value in (facts.get("employer_sponsorship"), facts.get("employer_sponsorship_available"),
                  review.get("employer_sponsorship"), review.get("employer_sponsorship_available")):
        if value is False:
            return True
        # A free-text value is no longer matched against a list of phrasings (owner,
        # 2026-10-07, item 8): the read states a refusal through the structured
        # stated_eligibility_requirements above or a false flag.
    return False


def pursue_without_authorization(review, policy):
    """May a role in a jurisdiction the owner is not authorized in still be applied to?

    A user may hold a US work permit and no EU citizenship or work permit (as the example
    profile records), and the owner explicitly said to keep targeting EU roles anyway, because an EU employer can sponsor.
    The gate this replaces required *verified* country-specific authorization for every
    non-US role, so every EU role was `REQUIRED_ELIGIBILITY_ANSWER` and never reached a
    single form question. Whether to sponsor is the employer's decision; refusing to apply
    pre-empts it.

    This decides only whether to *apply*. It asserts nothing. No authorization answer, no
    sponsorship answer and no declaration anywhere reads this function: those are answered
    from the recorded default-deny map in `profile.json`, which says
    NOT_AUTHORIZED for every jurisdiction that is not explicitly granted. Willingness to be
    sponsored is a separate recorded fact and can never make an authorization answer
    affirmative.

    Two things still block. An employer that was observed stating it cannot sponsor is a
    dead end, and blocks. `citizenship_clearance_requirements_met` is a different gate - a
    clearance or citizenship requirement is not something sponsorship fixes - and is left
    untouched by this function; the caller makes such a job INELIGIBLE (owner, 2026-09-29).
    """
    if policy_answer(policy, "open_to_employer_visa_sponsorship") is not True:
        return False
    return not employer_refuses_sponsorship(review)


# --------------------------------------------------------------------------------------
# Compensation: informative, not terminal.
#
# The owner's instruction, verbatim: "Please dont be too strict on salary. because some
# salaries are in pounds or euros. Relax this a bit." It arrived together with enabling
# EU/UK targeting, and the two are the same problem: the salary gate was written for US
# postings that publish a USD annual base band, and it terminated every posting that did
# not look like one.
#
# What is recorded here is the reasoning, because the numbers below are the only part a
# future reader can check.
#
# 1. An amount we cannot convert is not evidence about the owner's floor. Nothing on the
#    application path has ever supplied `compensation["fx"]`:
#    `application_browser.result_contract("inspect")` declares compensation as
#    {verified, currency, minimum, maximum, period} with no fx key, and
#    `desktop-worker-prompt.md` does not mention salary at all. So a EUR or GBP posting
#    could never satisfy the old requirement, no matter how well it was read. It was an
#    unconditional refusal wearing the clothes of a verification step.
# 2. Therefore the gate applies only where the figure is genuinely knowable: a fresh,
#    sourced, verified annual-base band, in USD or converted by dated FX evidence that
#    the read actually supplied. Everywhere else the compensation is recorded and the
#    application proceeds.
# 3. Where it is knowable, the floor is relaxed rather than removed - see
#    `relaxed_floor_usd` - and it is compared against the top of the band rather than the
#    bottom, so a role is refused only when its best case is far below what the owner
#    said they want. "Relax a bit" is not "ignore".
# 4. Nothing is thrown away. `compensation_assessment` always returns what the posting
#    stated - including "it stated nothing" - and `process` writes it into the stored
#    review, so the owner can see the compensation of every role applied to.

# Rough, deliberately imprecise USD-per-unit reference rates. These exist so a EUR or GBP
# band can be given a sense of scale in the record; they are NEVER used to refuse a role,
# and they are never presented as verified.
#
# They are rounded to one decimal place on purpose. A rate like "1.1" cannot be mistaken
# for a quoted market rate, whereas "1.0873" would look authoritative and be wrong within
# the week. `as_of` and `stale_after_days` are carried with them so a reader - and the
# code - can tell how old the guess is; past the horizon the approximate numbers are
# dropped from the record entirely rather than quietly rotting in place.
APPROXIMATE_USD_RATES = {
    "as_of": "2026-09-18",
    "confidence": "indicative",
    "precision": "rounded to one decimal place; an order-of-magnitude aid, not a rate",
    "source": "recorded by this product as a rough reference; not sourced from a market feed and not verified",
    "stale_after_days": 180,
    "usd_per_unit": {"USD": 1.0, "EUR": 1.1, "GBP": 1.3, "CHF": 1.2, "CAD": 0.7,
                     "AUD": 0.7, "SEK": 0.1, "NOK": 0.1, "DKK": 0.1, "PLN": 0.3},
}

# How far below the owner's recorded minimum a knowable figure may sit and still be
# applied to. 0.25 of a 180000 floor is a 135000 relaxed floor: a role whose band tops out
# under that is not a 180000-300000 role under any reading, and staying silent about it
# would be ignoring the owner's figures rather than relaxing them. Overridable from
# `profile.json` via a `salary_relaxation` block, which this task does not own.
DEFAULT_SALARY_RELAXATION = {"tolerance_below_minimum": 0.25}


def salary_relaxation(policy):
    settings = dict(DEFAULT_SALARY_RELAXATION)
    override = (policy or {}).get("salary_relaxation")
    if isinstance(override, dict):
        settings.update(override)
    try:
        tolerance = float(settings["tolerance_below_minimum"])
    except (KeyError, TypeError, ValueError):
        tolerance = DEFAULT_SALARY_RELAXATION["tolerance_below_minimum"]
    if not math.isfinite(tolerance) or not 0.0 <= tolerance < 1.0:
        tolerance = DEFAULT_SALARY_RELAXATION["tolerance_below_minimum"]
    settings["tolerance_below_minimum"] = tolerance
    return settings


def stated_compensation(review):
    """Exactly what the posting said about pay, and nothing derived.

    Returns None when the read recorded no compensation at all, which is the normal case
    for European and UK postings. None is a fact worth storing: it is the difference
    between "they did not publish a salary" and "we did not look".
    """
    compensation = (review or {}).get("compensation")
    if not isinstance(compensation, dict) or not compensation:
        return None
    fields = ("currency", "minimum", "maximum", "period", "target",
              "target_verified_for_candidate", "source", "observed_at", "verified")
    stated = {field: compensation[field] for field in fields if field in compensation}
    conversion = compensation.get("fx")
    if isinstance(conversion, dict):
        stated["fx"] = dict(conversion)
    return stated or None


def approximate_conversion(currency, now):
    """An openly approximate USD-per-unit rate, or None.

    None whenever we should not be putting a number next to the posting's own figure:
    unknown currency, or a reference table older than its stated horizon. Rates move, and
    a stale guess presented as a conversion is worse than no conversion.
    """
    rate = APPROXIMATE_USD_RATES["usd_per_unit"].get(str(currency or "").upper())
    if rate is None:
        return None
    try:
        age = (dt.datetime.fromisoformat(now).date() - dt.date.fromisoformat(APPROXIMATE_USD_RATES["as_of"])).days
    except (TypeError, ValueError):
        return None
    # Only being too old matters. A table recorded slightly after the review's clock is an
    # ordinary consequence of updating the constant, not a reason to withhold a number.
    if age > APPROXIMATE_USD_RATES["stale_after_days"]:
        return None
    return rate


def compensation_assessment(review, policy, now):
    """What the posting said about pay, what we can and cannot conclude from it, and
    whether that conclusion is strong enough to refuse the role.

    Never raises, and never returns nothing. Every branch - including a posting that
    published no salary at all - produces a record that is stored with the review.

    `blocks` is true in exactly one situation: the band is a fresh, sourced, verified
    annual-base band; it is in USD or carries dated FX evidence, so the USD figure is
    genuinely known rather than guessed; and even the top of that band (or a verified
    candidate-specific target, when one exists, since that is the more specific fact)
    falls below the relaxed floor.
    """
    floor = (policy or {}).get("minimum_base_salary_usd")
    relaxation = salary_relaxation(policy)
    record = {
        "stated": stated_compensation(review),
        "stated_by_employer": stated_compensation(review) is not None,
        "recorded_minimum_base_salary_usd": floor,
        "tolerance_below_minimum": relaxation["tolerance_below_minimum"],
        "relaxed_floor_usd": None,
        "basis": "NONE",
        "usd_equivalent": None,
        "comparable_usd": None,
        "finding": "SALARY_UNKNOWN",
        "warnings": [],
        "blocks": False,
        "note": "",
    }
    try:
        record["relaxed_floor_usd"] = float(floor) * (1.0 - relaxation["tolerance_below_minimum"])
    except (TypeError, ValueError):
        record["relaxed_floor_usd"] = None

    compensation = (review or {}).get("compensation")
    # A read that returns the compensation shape with every value null (the usual result for a
    # posting without pay) published nothing either; only its source/verified bookkeeping is set.
    if (not isinstance(compensation, dict) or not compensation
            or all(compensation.get(field) is None for field in ("currency", "minimum", "maximum", "period", "target", "fx"))):
        record["stated_by_employer"] = False
        record["warnings"] = ["SALARY_UNKNOWN", "SALARY_NOT_PUBLISHED"]
        record["note"] = ("The posting published no base compensation. Recorded as unknown; "
                          "no figure was inferred and the application is not held for it.")
        return record
    # A published band is a STABLE posting fact, not a volatile one: an employer that printed
    # "$157,000 - $184,000 USD" has not changed it since yesterday, and re-reading the page
    # cannot make the figure firmer (owner, 2026-09-22 - reuse anything not stale or expired).
    # The 24h default treated it as perishable and re-warned SALARY_UNKNOWN on a band already
    # read, dated, sourced and verified. It ages on the same seven-day record window everything
    # else reused from a recorded read now uses; an undated or unverified figure still fails
    # here exactly as before, because `fresh` also requires `source` and `verified is True`.
    if not fresh(compensation, now, RECORD_MAX_AGE_SECONDS):
        record["warnings"] = ["SALARY_UNKNOWN", "SALARY_EVIDENCE_NOT_DATED_OR_VERIFIED"]
        record["note"] = ("A compensation value was read but is not dated, sourced and verified, "
                          "so it is not firm enough to measure against the owner's floor. Recorded as stated.")
        return record

    currency = str(compensation.get("currency") or "").upper() or None
    try:
        minimum = float(compensation["minimum"])
        maximum = float(compensation["maximum"])
        usable = (all(math.isfinite(value) and value > 0 for value in (minimum, maximum))
                  and minimum <= maximum and currency is not None)
    except (KeyError, TypeError, ValueError):
        minimum = maximum = None
        usable = False
    if not usable:
        record["warnings"] = ["SALARY_UNKNOWN", "SALARY_BAND_UNREADABLE"]
        record["note"] = ("The compensation the posting stated could not be read as a band "
                          "(missing, non-numeric, inverted or without a currency). Recorded verbatim as stated.")
        return record
    if compensation.get("period") != "annual_base":
        record["finding"] = "NOT_ANNUAL_BASE"
        record["warnings"] = ["SALARY_UNKNOWN", "SALARY_NOT_ANNUAL_BASE"]
        record["note"] = ("The stated band is " + json.dumps(compensation.get("period")) + ", not an annual base "
                          "salary, so it cannot be compared with a base-salary floor. Recorded as stated.")
        return record

    exact = None
    if currency == "USD":
        exact, record["basis"] = 1.0, "USD"
    else:
        conversion_evidence = compensation.get("fx")
        if isinstance(conversion_evidence, dict) and fresh(conversion_evidence, now):
            try:
                rate = float(conversion_evidence["usd_per_unit"])
                if math.isfinite(rate) and rate > 0:
                    exact, record["basis"] = rate, "DATED_FX_EVIDENCE"
            except (KeyError, TypeError, ValueError):
                exact = None
    if exact is None:
        rate = approximate_conversion(currency, now)
        record["basis"] = "INDICATIVE_REFERENCE_RATE" if rate is not None else "NO_CONVERSION_AVAILABLE"
        record["warnings"].append("NON_USD_WITHOUT_DATED_FX_EVIDENCE")
        record["finding"] = "NOT_COMPARABLE_IN_USD"
        if rate is not None:
            record["usd_equivalent"] = {
                "minimum": round(minimum * rate), "maximum": round(maximum * rate),
                "approximate": True, "verified": False, "usd_per_unit": rate,
                "as_of": APPROXIMATE_USD_RATES["as_of"],
                "confidence": APPROXIMATE_USD_RATES["confidence"],
                "source": APPROXIMATE_USD_RATES["source"],
                "caveat": "APPROXIMATE - informational only, never used to accept or refuse a role",
            }
            record["note"] = ("Stated in " + currency + " with no dated FX evidence. Roughly "
                              + "{:,}".format(round(minimum * rate)) + "-" + "{:,}".format(round(maximum * rate))
                              + " USD at an approximate " + str(rate) + " USD/" + currency + " recorded "
                              + APPROXIMATE_USD_RATES["as_of"] + ". That approximation is indicative only; "
                              "the floor was not applied and the role proceeds.")
        else:
            record["note"] = ("Stated in " + str(currency) + ", which this product holds no rate for "
                              "(or whose reference rate is past its recorded horizon). No USD equivalent was "
                              "invented; the floor was not applied and the role proceeds.")
        return record

    usd_minimum, usd_maximum = minimum * exact, maximum * exact
    record["usd_equivalent"] = {"minimum": usd_minimum, "maximum": usd_maximum, "approximate": False,
                               "verified": True, "usd_per_unit": exact,
                               "basis": "stated in USD" if record["basis"] == "USD" else "dated FX evidence recorded by the read"}
    comparable, comparable_kind = usd_maximum, "top of the stated band"
    target = compensation.get("target")
    if target is not None:
        try:
            target = float(target)
        except (TypeError, ValueError):
            target = None
    if target is not None and compensation.get("target_verified_for_candidate") is True and minimum <= target <= maximum:
        comparable, comparable_kind = target * exact, "verified candidate-specific target"
        record["candidate_target_usd"] = target * exact
    record["comparable_usd"] = comparable
    record["comparable_kind"] = comparable_kind

    try:
        floor_usd = float(floor)
        relaxed = float(record["relaxed_floor_usd"])
    except (TypeError, ValueError):
        record["finding"] = "NO_RECORDED_MINIMUM"
        record["warnings"].append("POLICY_HAS_NO_MINIMUM_BASE_SALARY")
        record["note"] = "The policy records no minimum base salary, so nothing was compared."
        return record

    # `CROSSING_BASE_BAND_UNRESOLVED` survives, in the form its own words always implied.
    # The blocker's message said "crossing band alone proves neither eligibility nor
    # ineligibility" and then blocked on it anyway. It is still worth telling the owner -
    # the advertised band straddles their minimum, so what they would actually be offered
    # is genuinely unresolved - but an unresolved question is a thing to record, not a
    # reason to refuse to apply. Where a verified candidate-specific target exists it is
    # still used, and still resolves the question.
    if usd_minimum < floor_usd <= usd_maximum and comparable_kind == "top of the stated band":
        record["warnings"].append("CROSSING_BASE_BAND_UNRESOLVED")
    if comparable >= floor_usd:
        record["finding"] = "AT_OR_ABOVE_RECORDED_MINIMUM"
        record["note"] = ("The " + comparable_kind + " is at or above the owner's recorded minimum of "
                          + "{:,}".format(round(floor_usd)) + " USD.")
    elif comparable >= relaxed:
        record["finding"] = "BELOW_RECORDED_MINIMUM_WITHIN_TOLERANCE"
        record["warnings"].append("BASE_SALARY_BELOW_RECORDED_MINIMUM")
        record["note"] = ("The " + comparable_kind + " is " + "{:,}".format(round(comparable))
                          + " USD, below the owner's recorded minimum of " + "{:,}".format(round(floor_usd))
                          + " USD but at or above the relaxed floor of " + "{:,}".format(round(relaxed))
                          + " USD. Recorded as a warning; the role proceeds and the owner can see the number.")
    else:
        record["finding"] = "BELOW_RELAXED_FLOOR"
        record["warnings"].append("BASE_SALARY_BELOW_RELAXED_FLOOR")
        record["blocks"] = True
        record["note"] = ("The " + comparable_kind + " is " + "{:,}".format(round(comparable))
                          + " USD, below even the relaxed floor of " + "{:,}".format(round(relaxed))
                          + " USD (the owner's recorded minimum of " + "{:,}".format(round(floor_usd))
                          + " USD less a " + str(round(relaxation["tolerance_below_minimum"] * 100))
                          + "% tolerance). This figure is known, not guessed.")
    return record


# --------------------------------------------------------------------------------------
# Scope (owner decision, 2026-09-19): "AI should normalize the country names too; using
# Python might be brittle." The browser read reports the country as an ISO 3166-1 alpha-2
# code it judged from the posting's own location text (cited verbatim as evidence), and
# judges level and specialization fit against the owner's targets as a structured verdict
# {in_scope, reason}. Python compares codes and enforces verdicts; it never compares a
# title, a specialization phrase or a country name. Only an explicit AI verdict, an
# observed structured fact (open=false, hiring geography false) or a code outside the
# owner's location rules can end a job INELIGIBLE. A missing or unrepresentable value is
# held for the orchestrator as SCOPE_REVIEW_REQUIRED, never made final.
SCOPE_REVIEW_REQUIRED = "SCOPE_REVIEW_REQUIRED"
SCOPE_FACTS = frozenset({"country", "level", "specialization"})
SCOPE_DIMENSIONS = ("level", "specialization", "funding_stage", "location")
# The dimensions research's role_fit stands in for; funding stage and location are judged on
# their own (owner, 2026-10-07, items 4/5).
ROLE_FIT_DIMENSIONS = ("level", "specialization")
WORK_MODES = frozenset({"remote", "hybrid", "onsite"})
ISO_ALPHA2 = re.compile(r"^[A-Z]{2}$")
# Geographic facts, not preferences: the US, and the European countries (EU/EEA, the UK and
# Switzerland). They drive the legal side of eligibility (the US work permit, a silent
# posting's hiring geography). WHICH countries the user accepts is the profile's `countries`
# (`target_countries`), and which specializations the profile's `target_specializations`
# (E1.T7, owner decision a, 2026-10-14; they were the owner's US + Europe and backend,
# distributed systems, infrastructure, platform, data engineering, hard-coded here).
US_LOCATION_CODES = frozenset({"US"})
EUROPE_REMOTE_CODES = frozenset({
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV",
    "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE", "IS", "LI", "NO", "GB", "CH"})


def target_countries(policy):
    """The ISO alpha-2 codes the profile's `countries` accepts; empty means no restriction."""
    value = (policy or {}).get("countries")
    if not isinstance(value, list):
        return frozenset()
    return frozenset(item for item in value if isinstance(item, str) and ISO_ALPHA2.fullmatch(item))


def target_specializations(policy):
    """The profile's `target_specializations`; empty means the user named none."""
    value = (policy or {}).get("target_specializations")
    return [item for item in value if isinstance(item, str) and item.strip()] if isinstance(value, list) else []


def country_code(facts):
    """The AI-reported ISO alpha-2 country code, or None when absent or not a code."""
    value = (facts or {}).get("country")
    return value if isinstance(value, str) and ISO_ALPHA2.fullmatch(value) else None


def scope_verdict(review, dimension):
    """The AI's {in_scope, reason} verdict for one scope dimension, or None if not usable."""
    verdict = ((review or {}).get("scope_fit") or {}).get(dimension) if isinstance((review or {}).get("scope_fit"), dict) else None
    if (isinstance(verdict, dict) and isinstance(verdict.get("in_scope"), bool)
            and isinstance(verdict.get("reason"), str) and verdict["reason"].strip()):
        return verdict
    return None


def scope_refusal(review, policy):
    """A final refusal, only from an explicit AI verdict or an observed structured fact."""
    facts = review.get("facts", {})
    out = "Retain evidence; do not submit outside the resolved search policy"
    for dimension in SCOPE_DIMENSIONS:
        verdict = scope_verdict(review, dimension)
        if verdict and verdict["in_scope"] is False:
            return ("INELIGIBLE", "OUTSIDE_CONFIRMED_SCOPE",
                    "AI judged " + dimension + " out of scope: " + verdict["reason"].strip()[:500] + ". " + out)
    if facts.get("open") is False:
        return "INELIGIBLE", "OUTSIDE_CONFIRMED_SCOPE", "The read observed the role is not open. " + out
    if facts.get("hiring_geography_includes_candidate") is False:
        return "INELIGIBLE", "OUTSIDE_CONFIRMED_SCOPE", "The read observed hiring geography excludes the candidate. " + out
    code, mode = country_code(facts), facts.get("work_mode")
    # Every place the read KNOWS is outside the user's `countries`: out of scope
    # whether or not the posting stated a work mode (owner, 2026-09-27). This used to need a
    # stated remote/hybrid/onsite mode, so a Tel Aviv role with no stated mode (oscorp security)
    # was caught only by the missing-facts hold that item 14 deleted, and would have been
    # applied to. A place with no readable country code decides nothing.
    options = location_options(facts)
    # A profile without `countries` restricts no country (E1.T7).
    places = ([option.get("country") for option in options] if options is not None else [code])
    countries = target_countries(policy)
    if countries and places and all(isinstance(place, str) and ISO_ALPHA2.fullmatch(place) for place in places) and not any(
            place in countries for place in places):
        return ("INELIGIBLE", "OUTSIDE_CONFIRMED_SCOPE",
                "Location " + ", ".join(places) + " is outside the user's countries. " + out)
    # The owner's onsite/hybrid exclusions (Washington state, San Francisco) are judged by the AI
    # in scope_fit.location above, no longer by matching city and state text (owner, 2026-10-07,
    # item 5).
    return None


def scope_review(review, policy, dimensions=None):
    """Nothing left to refuse here (the funding stage is the AI's scope_fit verdict now).

    The missing-value holds that used to live here (no country code, an unrecognised work
    mode, a missing level or specialization verdict) are deleted (owner, 2026-09-27, item 14):
    a value the read did not report decides nothing.
    """
    # The funding-stage list comparison is gone (owner, 2026-10-07, item 4): the AI judges the
    # stage against the policy in scope_fit.funding_stage, which `scope_refusal` reads.
    return None


def reused_observation(review):
    """Is this review one whose age is deliberately carried forward rather than re-observed?

    Two cases, and NEITHER back-dates or falsifies `observed_at` - the recorded instant stays
    exactly what it was, and only the tolerance applied to it widens (owner, 2026-09-22):

    - `REUSED_READ_MARKER`: `reusable_read` returned the stored COMPLETE record for this exact
      identity, under this contract revision, with unchanged inputs.
    - `source` beginning "saved-context:": the read itself satisfied a factual leaf from a
      prior observation rather than re-observing it, and `application_browser.normalize`
      honestly dated the review to the OLDEST instant it relied on. Under the flat 24h rule
      that made reuse self-defeating - a session that correctly reused a recorded fact
      produced a review `assess` then rejected as stale, so the only way to pass was to
      re-observe everything. The incentive ran backwards; this removes it without pretending
      the observation is newer than it is.
    """
    if review.get(REUSED_READ_MARKER):
        return True
    return isinstance(review.get("source"), str) and review["source"].startswith("saved-context:")


def assess(review, job, policy, now):
    # Deleted (owner, 2026-09-27), item 14 "all of it": the 24-hour freshness rule, the employer and
    # requisition text comparison (IDENTITY_MISMATCH), the official-page confirmation
    # (APPLICATION_DESTINATION_OR_SCOPE_UNKNOWN) and the all-facts-required rule
    # (ELIGIBILITY_UNKNOWN, and SCOPE_REVIEW_REQUIRED for a missing value). Deduplication is the
    # local-database gate that runs before this. What remains is fit and preferences: an
    # explicit out-of-scope verdict or observed fact, an owner location exclusion, a known
    # funding stage outside the policy, a known salary below the floor, and a KNOWN
    # authorization or citizenship conflict. A value the read did not report decides nothing.
    facts = review.get("facts", {})
    scope = scope_refusal(review, policy)
    if scope:
        return scope
    # Owner decision 2026-09-19: funding stage, headcount, industry and city inform rather
    # than block. Unknown values are recorded as warnings (`eligibility_warnings`); the policy
    # has no employee-count rule, and a known funding stage outside the policy is still held
    # for review in `scope_review` below.
    # Scope before authorization: a role that is out of scope, or whose scope is still under
    # review, must never raise a user-owned eligibility question.
    scope = scope_review(review, policy)
    if scope:
        return scope
    # Only a KNOWN conflict holds (owner, 2026-09-27): an unreported country, authorization or
    # citizenship value is not a reason to stop, so each is read with .get and decides only
    # when present.
    country = facts.get("country")
    # Per-location authorization (owner, 2026-10-23, J1 A): the read judges the owner's map for
    # each location option; the role passes when ANY option in the user's countries passes the
    # per-country rule, and that option is recorded as the place the owner applies for.
    # An option where the owner holds authorization is chosen over one applied to without it
    # (a user authorized in the EU, offered CA, US and DE, is matched to DE); then a known country;
    # then the posting's order.
    candidates = authorization_candidates(facts, policy)
    ranked = sorted(((country_authorized(option["country"], option["authorization"], review, policy, now), option)
                     for option in candidates), key=lambda pair: (-pair[0], pair[1]["country"] is None))
    chosen = ranked[0][1] if ranked and ranked[0][0] else None
    authorization = chosen is not None
    if chosen is not None and chosen["country"] is not None and chosen.get("option") is not None:
        review["eligible_location"] = chosen["option"]
    else:
        review.pop("eligible_location", None)
    stated = stated_requirements(review)
    if facts.get("citizenship_clearance_requirements_met") is False:
        # Owner, 2026-09-29: a posting that requires US citizenship or a security clearance is out
        # of scope (the read judged the user does not meet it; clearances generally require citizenship).
        names = {"SECURITY_CLEARANCE_REQUIRED": ("eligibility for a US security clearance" if country == "US"
                                                 else "a security clearance"),
                 "US_CITIZENSHIP_REQUIRED": "US citizenship"}
        needs = [names[item] for item in sorted(stated & CITIZENSHIP_CLEARANCE_REQUIREMENTS)]
        return ("INELIGIBLE", "CLEARANCE_OR_CITIZENSHIP_REQUIRED",
                ("The posting requires " + " and ".join(needs) + ", which the owner's recorded answers do not meet"
                 if needs else "The posting has a citizenship or clearance requirement the owner does not meet")
                + ". Retain evidence; do not submit.")
    if not authorization:
        # Owner, 2026-09-29: the hold states its actual cause - for every location option.
        reasons = [authorization_hold_reason(option["country"], option["authorization"], review, policy, stated)
                   for option in candidates]
        if len(candidates) == 1:
            reason = reasons[0]
        else:
            reason = "No location option fits the owner's work authorization: " + "; ".join(
                (str(option["option"].get("city") or option["country"]) + " (" + str(option["country"]) + "): " + text)
                for option, text in zip(candidates, reasons))
        return "BLOCKED", "REQUIRED_ELIGIBILITY_ANSWER", reason + ". Retain evidence; do not submit."
    # Owner, 2026-09-19 ("submit an application at all cost"): an AI judgment that the resume
    # does not meet every listed requirement is recorded as a warning, not a block.
    # Salary informs; it does not terminate. Unknown, unpublished, non-annual, unreadable
    # and unconvertible compensation all reach `blocks=False` and are recorded instead.
    # The one refusal left needs a figure that is actually known - see
    # `compensation_assessment`.
    salary = compensation_assessment(review, policy, now)
    if salary["blocks"]:
        # Named for what it is rather than hidden inside the general scope refusal, so the
        # owner can see that the pay - not the industry, level, geography or an onsite
        # exclusion - is the reason, and can raise the tolerance if they disagree.
        return ("INELIGIBLE", "BASE_SALARY_BELOW_RELAXED_FLOOR", salary["note"]
                + " Retain evidence; raise salary_relaxation.tolerance_below_minimum or the recorded minimum to pursue roles at this level")
    return None


def authorization_candidates(facts, policy):
    """The places whose authorization decides the role: one per location option, else facts.country.

    Each is {"country", "authorization", "option"}. An option without its own verdict (an older
    read) whose country is facts.country takes facts.authorization, which was judged for it; any
    other option without a verdict has none. An option whose known country is outside the user's
    `countries` is never a candidate, so it can never be the eligible location.
    """
    options = location_options(facts)
    if options is None:
        return [{"country": facts.get("country"), "authorization": facts.get("authorization"), "option": None}]
    countries = target_countries(policy)
    candidates = []
    for option in options:
        code = option.get("country") if isinstance(option.get("country"), str) and ISO_ALPHA2.fullmatch(option["country"]) else None
        if countries and code is not None and code not in countries:
            continue
        verdict = option.get("authorization")
        if verdict not in OPTION_AUTHORIZATION_VALUES:
            verdict = facts.get("authorization") if code is not None and code == facts.get("country") else None
        if code == "US" and verdict != STATED_REQUIREMENT_NOT_MET and verdict not in US_AUTHORIZATION_LABELS:
            # A US option's raw status, or none at all, is labelled from the recorded answers as
            # derive_silent_eligibility_facts labels facts.country: a permanent resident is
            # authorized; a user without US authorization still applies (owner, 2026-10-14).
            verdict = us_authorization_label(policy, verdict if verdict in JURISDICTION_STATUSES else None)
        candidates.append({"country": code, "authorization": verdict, "option": option})
    if not candidates:
        return [{"country": facts.get("country"), "authorization": facts.get("authorization"), "option": None}]
    return candidates


AUTHORIZED, APPLIES_ANYWAY = 2, 1


def country_authorized(country, verdict, review, policy, now):
    """The per-country authorization rule `assess` applies to one place.

    AUTHORIZED (2) when the owner holds authorization there, APPLIES_ANYWAY (1) when the role
    is applied to without it (US: the employer decides sponsorship, owner 2026-10-14; elsewhere
    `pursue_without_authorization`), 0 when it does not pass. A country the read did not report
    decides nothing (1).
    """
    if country is None:
        return APPLIES_ANYWAY
    if country == "US":
        # A user without US work authorization still applies; sponsorship is the employer's
        # decision (owner, 2026-10-14: "Let them apply and leave sponsorship to employer").
        # Only the read's own stated conflict holds.
        if verdict in US_AUTHORIZATION_LABELS:
            return AUTHORIZED
        return APPLIES_ANYWAY if verdict is None else 0
    # The read's judgment that the owner's recorded map authorizes them in the place's country
    # without sponsorship is authorization (owner, 2026-10-21).
    if ((verdict == "COUNTRY_SPECIFIC_AUTHORIZATION_VERIFIED"
         and fresh(review.get("country_authorization_evidence", {}), now))
            or verdict in {"AUTHORIZED_NO_SPONSORSHIP_REQUIRED", "JURISDICTION_MAP:AUTHORIZED_NO_SPONSORSHIP_REQUIRED"}):
        return AUTHORIZED
    return APPLIES_ANYWAY if pursue_without_authorization(review, policy) else 0


def authorization_hold_reason(country, verdict, review, policy, stated):
    """Why one place does not pass `country_authorized` (owner, 2026-09-29: the hold states its cause)."""
    if country == "US" and "OTHER_COUNTRY_AUTHORIZATION_REQUIRED" in stated:
        return "The posting for this US role requires work authorization in another country, which the owner does not hold"
    if country == "US" and verdict == STATED_REQUIREMENT_NOT_MET:
        return "The read reported that the owner does not meet this US role's stated work-authorization requirement"
    if country == "US":
        return ("The read reported work authorization for this US role as " + repr(verdict)[:80]
                + ", not as met by the owner's permanent residence")
    if "OTHER_COUNTRY_AUTHORIZATION_REQUIRED" in stated:
        return ("The role is in " + str(country) + " and the posting requires an existing right to work there, "
                "which the owner does not hold")
    if employer_refuses_sponsorship(review):
        return ("The role is in " + str(country) + ", where the owner holds no work authorization, and the "
                "posting says the employer will not sponsor a visa")
    if policy_answer(policy, "open_to_employer_visa_sponsorship") is not True:
        return ("The role is outside the US (country code " + str(country) + "), where the owner holds no work "
                "authorization, and the owner's policy does not record openness to employer visa sponsorship")
    return "The role is in " + str(country) + ", where the owner holds no recorded work authorization"


US_PERMANENT_RESIDENT_ACCEPTED = "US_PERMANENT_RESIDENT_ACCEPTED"
US_WORK_AUTHORIZATION_ACCEPTED = "US_WORK_AUTHORIZATION_ACCEPTED"
US_AUTHORIZATION_LABELS = frozenset({US_PERMANENT_RESIDENT_ACCEPTED, US_WORK_AUTHORIZATION_ACCEPTED})


def us_authorization_label(policy, status=None):
    """The authorization a US role is taken to accept for this user, from the recorded answers.

    US_PERMANENT_RESIDENT_ACCEPTED when the profile records permanent residence
    (`us_lawful_permanent_resident` true); US_WORK_AUTHORIZATION_ACCEPTED when it records US
    work authorization without sponsorship otherwise (`status`, the inspect read's own judgment
    of the jurisdiction map for the role's country, or, with none, `currently_authorized_to_work_in_us`
    true and `requires_visa_sponsorship_now_or_future_in_us` false); None when it records neither.
    Until E1.T7 every user was assumed to be a US permanent resident.
    """
    import answering_agent
    if policy_answer(policy, "us_lawful_permanent_resident") is True:
        return US_PERMANENT_RESIDENT_ACCEPTED
    if status not in JURISDICTION_STATUSES:
        authorized = (policy_answer(policy, "currently_authorized_to_work_in_us") is True
                      and policy_answer(policy, "requires_visa_sponsorship_now_or_future_in_us") is False)
    else:
        authorized = status == answering_agent.AUTHORIZED_WITHOUT_SPONSORSHIP
    return US_WORK_AUTHORIZATION_ACCEPTED if authorized else None


SILENT_US_AUTHORIZATION_BASIS = "POSTING_SILENT_US_ROLE_OWNER_US_PERMIT"
STATED_US_AUTHORIZATION_BASIS = "POSTING_STATED_US_REQUIREMENT_OWNER_US_PERMIT"
SILENT_NON_US_AUTHORIZATION_BASIS = "POSTING_SILENT_NON_US_ROLE_JURISDICTION_MAP"
# The statuses the owner's jurisdiction map records. The inspect read reports one of them in
# facts.authorization: its own judgment of the map for the role's country, the map's "EU" line
# covering every member country (owner, 2026-10-21: "remove those blockers"; Python's
# exact-code lookup with the "*" fallback could not, and wrongly skipped EU-authorized users).
JURISDICTION_STATUSES = frozenset({"AUTHORIZED_NO_SPONSORSHIP_REQUIRED", "AUTHORIZED_SPONSORSHIP_REQUIRED",
                                   "NOT_AUTHORIZED"})
STATED_REQUIREMENT_BASIS = "POSTING_STATED_REQUIREMENT"
SILENT_CLEARANCE_BASIS = "POSTING_SILENT_NO_CLEARANCE_REQUIREMENT"
SILENT_US_GEOGRAPHY_BASIS = "POSTING_SILENT_US_ROLE"
SILENT_EUROPE_GEOGRAPHY_BASIS = "POSTING_SILENT_EUROPE_ROLE_OWNER_RELOCATES"
AI_REQUIREMENTS_BASIS = "AI_JUDGED_AGAINST_RESUME"
SPONSORSHIP_FACTS = ("employer_sponsorship", "employer_sponsorship_available")
# Owner decision 2026-09-19 (silent postings). The browser AI reports WHICH authorization,
# sponsorship, citizenship or clearance requirement the posting states, as enum values in
# facts.stated_eligibility_requirements, each with its verbatim quote. The previous boolean
# "requirement stated" markers let any quote (e.g. "5+ years writing production-quality
# code") switch the owner's silent-posting rule off. NONE_STATED,
# an empty list, a missing field or a value outside the enum is silence.
STATED_REQUIREMENTS_FIELD = "stated_eligibility_requirements"
NONE_STATED = "NONE_STATED"
STATED_REQUIREMENTS = frozenset({
    "US_WORK_AUTHORIZATION_REQUIRED", "NO_VISA_SPONSORSHIP", "SPONSORSHIP_AVAILABLE",
    "US_CITIZENSHIP_REQUIRED", "SECURITY_CLEARANCE_REQUIRED", "OTHER_COUNTRY_AUTHORIZATION_REQUIRED",
    NONE_STATED})
CITIZENSHIP_CLEARANCE_REQUIREMENTS = frozenset({"US_CITIZENSHIP_REQUIRED", "SECURITY_CLEARANCE_REQUIRED"})
# Stated requirements a candidate who needs sponsorship cannot meet outside the US.
SPONSORSHIP_REFUSALS = frozenset({"NO_VISA_SPONSORSHIP", "OTHER_COUNTRY_AUTHORIZATION_REQUIRED"})
# A stated requirement the owner's US permit satisfies on a US role (per the jurisdiction map).
US_PERMIT_SATISFIES = frozenset({"US_WORK_AUTHORIZATION_REQUIRED", "NO_VISA_SPONSORSHIP", "SPONSORSHIP_AVAILABLE"})
STATED_REQUIREMENT_NOT_MET = "STATED_REQUIREMENT_NOT_MET"
OPTION_AUTHORIZATION_VALUES = JURISDICTION_STATUSES | {STATED_REQUIREMENT_NOT_MET}


def stated_requirements(review):
    """The valid stated-requirement enum values of a read, NONE_STATED excluded."""
    facts = (review or {}).get("facts") if isinstance((review or {}).get("facts"), dict) else {}
    value = facts.get(STATED_REQUIREMENTS_FIELD)
    items = value if isinstance(value, list) else [value]
    return {item for item in items if isinstance(item, str) and item in STATED_REQUIREMENTS and item != NONE_STATED}


def role_codes(facts):
    """ISO codes of every place the role can be done from (facts.country and each option)."""
    codes = {country_code(facts)} if country_code(facts) else set()
    for option in location_options(facts) or []:
        value = option.get("country")
        if isinstance(value, str) and ISO_ALPHA2.fullmatch(value):
            codes.add(value)
    return codes


def derive_silent_eligibility_facts(review, policy):
    """Owner decision 2026-09-19: a posting's silence is not an unknown.

    Only a verified browser read is completed here, and only facts the read left null; a
    value the read reported decides exactly as before. What the posting states comes from
    the structured enum list facts.stated_eligibility_requirements, never from prose and
    never from which pointers happen to carry a quote:

    - authorization: the read judges the owner's recorded jurisdiction map for the role's
      country itself and reports the status in facts.authorization (JURISDICTION_STATUSES;
      owner, 2026-10-21). Python no longer looks the country up. US role: labelled as accepted
      when the owner holds US authorization without sponsorship; a US_WORK_AUTHORIZATION_REQUIRED
      / NO_VISA_SPONSORSHIP statement is met by that permit. A stated requirement for another
      country's authorization is the read's to judge against the map (STATED_REQUIREMENT_NOT_MET). Non-US role: authorized without sponsorship is
      authorization; otherwise `assess` applies the owner's sponsorship rule, which a stated
      NO_VISA_SPONSORSHIP or OTHER_COUNTRY_AUTHORIZATION_REQUIRED refuses.
    - citizenship/clearance: false when US_CITIZENSHIP_REQUIRED or SECURITY_CLEARANCE_REQUIRED
      is stated, true when nothing is.
    - hiring geography: true when silent and the role has a US option (or a Europe option:
      the owner relocates for Europe roles, sponsorship still required).

    Quotes no longer gate any of this (owner, 2026-09-27): a stated requirement counts as the AI
    reported it, whether or not its quote matched. `requirements_met` is the
    read's own AI judgment against the resume and is only labelled. Each derived fact records
    its basis. The owner's authorization/sponsorship form ANSWERS are not touched: the
    answering AI reads the map for them.
    """
    if not isinstance(review, dict) or not isinstance(review.get("facts"), dict):
        return review
    facts = review["facts"]
    evidence = review.get("browser_evidence")
    if (review.get("verified") is not True or not isinstance(evidence, list)
            or not review.get("observed_browser_calls")):
        return review
    basis = review.setdefault("eligibility_fact_basis", {})
    stated = stated_requirements(review)
    # A stated requirement counts as the AI reported it, whether or not its quote matched
    # (owner, 2026-09-27: no quote dependency). It used to be ignored when its quote was rejected.
    code = country_code(facts)
    # The read's own judgment of the owner's map for the role's country (owner, 2026-10-21).
    status = facts.get("authorization") if facts.get("authorization") in JURISDICTION_STATUSES else None
    if (facts.get("authorization") is None or status) and code:
        if code in US_LOCATION_CODES:
            # A stated requirement for another country's authorization is judged by the read
            # against the map (it reports STATED_REQUIREMENT_NOT_MET itself; owner, 2026-10-21),
            # never set here from the stated list alone.
            if us_authorization_label(policy, status):
                facts["authorization"] = us_authorization_label(policy, status)
                basis["authorization"] = {
                    "basis": STATED_US_AUTHORIZATION_BASIS if stated & US_PERMIT_SATISFIES else SILENT_US_AUTHORIZATION_BASIS,
                    "jurisdiction": "US", "owner_status": status, "stated": sorted(stated),
                    "derived_from": "inspect read of work_authorization_by_jurisdiction"}
            elif status:
                # No US permit recorded: the user still applies and sponsorship is the
                # employer's decision (owner, 2026-10-14), so the read's status is kept as basis only.
                facts["authorization"] = None
                basis["authorization"] = {"basis": SILENT_US_AUTHORIZATION_BASIS, "jurisdiction": "US",
                                          "owner_status": status, "stated": sorted(stated)}
        elif status is not None:
            # An owner authorized there without sponsorship meets a no-sponsorship statement;
            # otherwise `assess` decides by the owner's sponsorship rule and the employer's
            # stated refusal.
            facts["authorization"] = (STATED_REQUIREMENT_NOT_MET
                                      if stated & SPONSORSHIP_REFUSALS and status != "AUTHORIZED_NO_SPONSORSHIP_REQUIRED"
                                      else "JURISDICTION_MAP:" + status)
            basis["authorization"] = {"basis": STATED_REQUIREMENT_BASIS if stated & SPONSORSHIP_REFUSALS
                                      else SILENT_NON_US_AUTHORIZATION_BASIS,
                                      "jurisdiction": code, "owner_status": status, "stated": sorted(stated)}
    if facts.get("citizenship_clearance_requirements_met") is None:
        if stated & CITIZENSHIP_CLEARANCE_REQUIREMENTS:
            facts["citizenship_clearance_requirements_met"] = False
            basis["citizenship_clearance_requirements_met"] = {
                "basis": STATED_REQUIREMENT_BASIS, "stated": sorted(stated & CITIZENSHIP_CLEARANCE_REQUIREMENTS)}
        else:
            facts["citizenship_clearance_requirements_met"] = True
            basis["citizenship_clearance_requirements_met"] = {
                "basis": SILENT_CLEARANCE_BASIS, "derived_from": "posting states no citizenship or clearance requirement"}
    if facts.get("hiring_geography_includes_candidate") is None:
        codes = role_codes(facts)
        if codes & US_LOCATION_CODES:
            facts["hiring_geography_includes_candidate"] = True
            basis["hiring_geography_includes_candidate"] = {"basis": SILENT_US_GEOGRAPHY_BASIS, "codes": sorted(codes)}
        elif codes & EUROPE_REMOTE_CODES:
            facts["hiring_geography_includes_candidate"] = True
            basis["hiring_geography_includes_candidate"] = {"basis": SILENT_EUROPE_GEOGRAPHY_BASIS, "codes": sorted(codes),
                                                            "sponsorship_required": True}
    reason = facts.get("requirements_met_reason")
    if (isinstance(facts.get("requirements_met"), bool) and isinstance(reason, str) and reason.strip()
            and "/facts/requirements_met" not in {str(entry.get("pointer")) for entry in evidence if isinstance(entry, dict)}):
        basis["requirements_met"] = {"basis": AI_REQUIREMENTS_BASIS, "reason": reason.strip()[:1000]}
    if not basis:
        review.pop("eligibility_fact_basis")
    return review


def eligibility_warnings(review, now):
    """Owner decision 2026-09-19: unknown funding stage, headcount and industry inform only."""
    facts = review.get("facts") if isinstance(review.get("facts"), dict) else {}
    warnings = []
    if facts.get("funding_stage") is None:
        warnings.append("FUNDING_STAGE_UNKNOWN")
    count = review.get("employee_count_evidence") if isinstance(review.get("employee_count_evidence"), dict) else {}
    if not count.get("value") or not (fresh(count, now) or count.get("basis") == RESEARCH_BASIS
                                      and fresh(count, now, RESEARCH_FACT_MAX_AGE_SECONDS)):
        warnings.append("HEADCOUNT_UNKNOWN")
    if facts.get("industry") is None:
        warnings.append("INDUSTRY_UNKNOWN")
    if not str(facts.get("city") or "").strip():
        warnings.append("CITY_UNKNOWN")
    if facts.get("requirements_met") is not True:
        warnings.append("LISTED_REQUIREMENTS_NOT_ALL_MET")
    return warnings


def local_gate(database, job, now, policy=None):
    prefix = job["employer"] + ":"
    original_requisition = job["job_key"][len(prefix):] if job["job_key"].startswith(prefix) else job["requisition"]
    # Only a record of THIS job blocks (owner, 2026-10-06: "never block employers unless the jobs
    # are the same"); a '*' row (a role we never resolved, e.g. the imported interviews at
    # Globodyne, Ollivanders, Gotham Bank and Moneypenny) no longer holds every job at the employer.
    history = database.execute("SELECT * FROM application_history WHERE employer=? AND requisition IN (?, ?) LIMIT 1",
                               (job["employer"], job["requisition"], original_requisition)).fetchone()
    if history:
        return "DUPLICATE", "EXISTING_" + history["kind"], "Continue the existing application/interview; do not submit again", None
    # The pending interview/outcome event hold (PIPELINE_EVENT_RECONCILIATION_REQUIRED) is deleted
    # (owner, 2026-09-30: "exclude the interview checks. This harness is not supposed to manage
    # interviews."). A recorded application or interview of this job in application_history above
    # still blocks a second application; the 90-day employer window is deleted (2026-10-06).
    # The REQUISITION_UNKNOWN refusal that used to sit here is gone (owner, 2026-09-24,
    # approach A: "for jobs that genuinely don't publish a requisition, we need a way to still
    # accept them into the system"). It refused every posting whose employer publishes no
    # requisition id anywhere in its URL - a company careers page, a VC portfolio board, a
    # LinkedIn or YC listing - and sent it to recovery to resolve an id that does not exist.
    # Four live jobs sat here, and nothing downstream could ever release them.
    #
    # `unresolved:<hash>` is not an absent identity. `applications.identity` mints it from the
    # SHA-256 of the canonical https://host/path of the posting, so it already identifies this
    # posting exactly, and two reads of the same URL always agree. What it cannot do is prove
    # that a DIFFERENT url is a different role, which is why it stays weak_identity() and why
    # every conservative check keeps treating it as unproven.
    #
    # Duplicate protection is same-job only (owner, 2026-10-06: "never block employers unless the
    # jobs are the same, hence dedupe"): the recorded history above, external_history.gate's
    # near-identical-requisition hold below, and the same-posting judgment before the first read
    # and before every submit (same_posting_duplicate). The 90-day employer window is deleted.
    import external_history
    gate = external_history.gate(database, job, now)
    if gate:
        return gate
    # The wait on another job at this employer with an unsettled submission
    # (EMPLOYER_SUBMISSION_UNSETTLED, 2026-09-30) is deleted (owner, 2026-10-06: "Okay remove that Blcok
    # employers constraint. Theres is no need to do this since we have deduping functionality
    # already created. completely remove it.").
    return None


def attachment_uploads(questions, answers, resume):
    """{field: text} of the file-control answers rendered to a PDF (a cover letter) for this attempt."""
    return {question_field(question): answers[question_field(question)]
            for question in questions if isinstance(question, dict)
            and str(question.get("answer_type") or "").strip().casefold() in ANSWER_FILE_TYPES
            and question.get("semantic") not in RESUME_SEMANTICS
            and isinstance(answers.get(question_field(question)), str)
            # An answer naming the resume file (a certificate or other upload answered with it)
            # is never printed into a PDF: it stays an answer, and the submit session uploads
            # the designated resume to that control (application_browser submit prompt).
            and answers[question_field(question)].strip()
            and Path(answers[question_field(question)].strip()).name.casefold() != resume.name.casefold()}


def question_field(question):
    """The durable key a required question is answered under.

    Real forms return `"id": null`. Four of the five questions in the only real enumerated
    review - Vandelay Security's - do. That null reached three places and broke all of them:
    `",".join(missing)` raised `TypeError`; every matched null-id question collided on the
    single `answers[None]` key, so one question's answer silently overwrote another's; and
    `decision_transport.request_fact` would have written it into `user_decisions.field`,
    which is `TEXT NOT NULL`, so the owner could never have been asked at all.

    A substitute is derived from what the read actually contains - the semantic, the label,
    the answer type and the choices - and never from the question's position, so a re-read
    of the same form yields the same key and an owner's saved answer still matches it on a
    later cycle. Two questions that differ in any of those fields get different keys; two
    that differ in none of them are the same question and correctly collapse to one.

    The id is NOT used, and that is the point (owner, 2026-09-21). An earlier note here
    said the real fix was "a non-null, stable, unique id per enumerated question". Real
    forms then showed the id is not only nullable but neither stable nor unique, which is
    strictly worse than null because it is silently believed:

    - Pawnee Health, 2026-09-21: one inspection enumerated the form twice with the ids
      shifted by four, so "First name" was field-8 and also field-12, while field-12 was
      ALSO "Last name". One key carried two different questions. Keyed by id, one answer
      would have been typed into the wrong control of a live employer's form.
    - Dunder AI: the same "Locations" question was read as id_9000001 on one cycle and
      id_9000002 on the next, orphaning the first answer so it had to be re-derived.

    The derived key has neither failure: it depends only on what the question IS, so the
    same question always collapses to one key however many times it is enumerated and
    whatever ids the read invents, and two genuinely different questions stay apart even
    when a read hands them the same id. It is the path every null-id form already took,
    including Dharma's - the one independently verified submission this product has made.

    The key's `unidentified-question:` prefix is now a misnomer for a question that does
    carry an id. It is kept deliberately: changing it would orphan every answer and owner
    decision already stored under it, for a cosmetic gain.
    """
    question = question if isinstance(question, dict) else {}
    semantic = normalized(question["semantic"]) if isinstance(question.get("semantic"), str) else ""
    label = normalized(question["label"]) if isinstance(question.get("label"), str) else ""
    # Only semantic and label (owner, 2026-09-21). DU took the id out of the key because the
    # read reports it inconsistently, then kept answer_type and choices - which the read ALSO
    # reports inconsistently, so the same bug survived one layer down. Pawnee Health's next
    # inspection proved it: both passes gave "First name"/given_name, but the first said
    # answer_type "text" and the second said null, so one question still produced two keys and
    # had to be answered twice. What a question IS is its meaning and its wording; how the page
    # happens to render it is not part of its identity.
    #
    # The fallback keeps answer_type and choices ONLY when there is neither semantic nor label,
    # where they are all that is left to tell two questions apart - and a question with neither
    # cannot be answered anyway, so it holds rather than submits.
    identity = [semantic, label] if (semantic or label) else [
        semantic, label, question.get("answer_type"), question.get("choices")]
    fingerprint = hashlib.sha256(json.dumps(
        identity, sort_keys=True, default=str).encode()).hexdigest()[:12]
    slug = "_".join((semantic or label or "unidentified").split())[:48] or "unidentified"
    return "unidentified-question:" + slug + ":" + fingerprint


def earliest_start_rule(policy):
    """The recorded rule for the earliest start date, or None if none is recorded.

    Stored as a rule and never as a date. A concrete date written into the policy file is
    correct on the day it is written and wrong for every application submitted afterwards,
    and `resolve_answers` would hand it to an employer verbatim. The rule's semantic ends in
    `_rule` precisely so the exact-semantic lookup can never match a form question and
    submit the rule object itself as an answer.
    """
    rule = policy_answer(policy, EARLIEST_START_RULE_SEMANTIC)
    if (isinstance(rule, dict) and rule.get("basis") == "application_date"
            and isinstance(rule.get("offset_days"), int) and not isinstance(rule.get("offset_days"), bool)
            and rule["offset_days"] >= 0):
        return rule
    return None


def earliest_start_date(application_date, policy):
    """Application date plus the recorded offset, as a plain ISO date.

    Deterministic arithmetic on a recorded rule, computed in Python rather than chosen by a
    model, in the same spirit as the default-deny authorization map: nothing here is a
    judgment, so nothing here is delegated. Returns None when no rule is recorded or the
    date cannot be parsed, and None means the AI answering step answers it instead.
    """
    rule = earliest_start_rule(policy)
    if not rule or not application_date:
        return None
    try:
        day = dt.datetime.fromisoformat(str(application_date)).date()
    except (TypeError, ValueError):
        return None
    return (day + dt.timedelta(days=rule["offset_days"])).isoformat()


def earliest_start_floor(now, policy):
    """The recorded floor, as the later of the local and UTC readings of `now`.

    ONE rule, defined once (owner, 2026-09-22). `earliest_start_date` parses the local date
    carried in `now`'s offset, so a timestamp written with a negative offset (America/Los_Angeles)
    yields a floor one day EARLIER than the UTC day the application is actually made on. Taking
    the later of the two can only move the floor forwards, which is the safe direction.

    Until now this max() lived only in `answering_agent.earliest_start_floor`, while the value
    actually PERSISTED by `materialize_computed_answers` used the bare local reading. On a day the
    two disagreed the stored answer failed `answering_agent.admit`'s `date_before_floor`, the
    correct later floor was recomputed there - and then discarded, because `materialize` only
    persists AI and declaration sources. `resolve_answers` then re-read the old, lower value and
    submitted it. A start date below the owner's own floor reached employers that way.
    `answering_agent.earliest_start_floor` now delegates here so there is one rule and one writer.
    """
    stamps = [now]
    try:
        parsed = dt.datetime.fromisoformat(str(now))
    except (TypeError, ValueError):
        parsed = None
    if parsed is not None and parsed.tzinfo is not None:
        stamps.append(parsed.astimezone(dt.timezone.utc).isoformat())
    found = [value for value in (earliest_start_date(stamp, policy) for stamp in stamps if stamp) if value]
    return max(found) if found else None


def persist_read_reconciliations(database, job, review, now):
    """Write a read's employer/role reconciliations onto the job, not just into an event.

    Owner, 2026-09-22: "There should be no rederivation. We must absolutely reuse work."

    `application_destination.employer_accepted` and `role_accepted` each make a judgement the
    model had to be asked for, then recorded it ONLY as an `EMPLOYER_NAME_RECONCILED` /
    `ROLE_TITLE_RECONCILED` event. Neither event has a single reader anywhere in the repo, so
    every later read re-asked the identical question: lucerne energy and mooby each resolved
    "Lucerne Energy B.V." / "Mooby, Inc." four times, and aperture's title five times while
    `payload["role"]` still held the em-dash form that caused the mismatch.

    `reconcile_requisition` and `reconcile_destination` already do this correctly - they write
    the corrected value to a real `application_jobs` column AND emit the event. This is the
    same discipline for the two identity fields that were events only. The payload is the right
    home because it is the one channel that reaches EVERY consumer, including
    `application_browser.normalize`, which runs out of process with no database handle at all
    and is exactly where the "Mooby, Inc." refusal happened.

    Returns the updated job row, or None when there was nothing to write.
    """
    payload = json.loads(job["payload"])
    changed = False
    reconciliation = review.get("employer_reconciliation")
    if isinstance(reconciliation, dict) and isinstance(reconciliation.get("observed"), str):
        folded = normalized(reconciliation["observed"])
        known = [name for name in payload.get("employer_names") or [] if isinstance(name, str)]
        if folded and folded != job["employer"] and folded not in known:
            payload["employer_names"] = known + [folded]
            changed = True
    title = (review.get("role_title_reconciliation") or {}).get("observed") if isinstance(
        review.get("role_title_reconciliation"), dict) else None
    if isinstance(title, str) and title.strip() and title != payload.get("role"):
        payload["role_titles"] = [name for name in payload.get("role_titles") or []
                                  if isinstance(name, str) and name != payload.get("role")] + [payload.get("role")]
        payload["role"] = title
        changed = True
    if not changed:
        return None
    database.execute("UPDATE application_jobs SET payload=?,updated_at=? WHERE job_key=?",
                     (json.dumps(payload), now, job["job_key"]))
    event(database, job["job_key"], "READ_RECONCILIATIONS_PERSISTED",
          {"employer_names": payload.get("employer_names"), "role": payload.get("role"),
           "basis": "REUSE_RECORDED_JUDGEMENT"}, now)
    return database.execute("SELECT * FROM application_jobs WHERE job_key=?", (job["job_key"],)).fetchone()


def recorded_authorization_facts(policy):
    """The owner's RECORDED authorization and sponsorship facts, for the submit session.

    Owner, 2026-09-22 (option C). A conditional authorization control is not visible at inspect
    time, so `_boolean_literal` has no `choices` to render onto and the packet carries a bare
    "Yes". Massive Dynamic's menu offered "Yes, and I will not require sponsorship now or in the
    future" against "Yes, but I will require sponsorship...", and the session correctly refused
    to pick: choosing asserts a SPONSORSHIP fact, and the packet supplied none for that
    question. This supplies them, so the choice becomes a lookup instead of an inference.

    It is deliberately a JURISDICTION MAP, not a flat yes/no. The adversarial case this project
    has already seen (F11) took the US entry and put it in a UK right-to-work field: real text,
    correctly cited, and a false legal assertion. Keyed this way, an entry answers a question
    about its own jurisdiction, the "EU" entry a question about any EU member state that has no
    entry of its own (owner, 2026-10-21: the session applies it), and "*" covers the rest, so a
    jurisdiction nobody listed reads as "*" records rather than falling through to a permissive one.

    Nothing here is computed or inferred - every value is read straight out of the owner's
    recorded answers.
    """
    answers = (policy or {}).get("answers") or {}

    def recorded(key):
        # The answer in force for the semantic recorded under this key (`answers_in_force`), so an
        # answer the owner kept from Approvals for the same semantic is the one the submit session
        # gets (owner, 2026-10-24); else the value recorded under the key, as before.
        entry = answers.get(key)
        if not isinstance(entry, dict):
            return None
        return policy_answer(policy, entry.get("semantic"), entry.get("value")) if entry.get("semantic") else entry.get("value")

    return {"work_authorization_by_jurisdiction": recorded("work_authorization_by_jurisdiction"),
            "requires_visa_sponsorship_us": recorded("requires_visa_sponsorship_us"),
            "requires_visa_sponsorship_outside_us": recorded("requires_visa_sponsorship_outside_us"),
            "open_to_employer_visa_sponsorship": recorded("open_to_visa_sponsorship"),
            # Owner, 2026-09-28: a recorded permanent-resident status means a green card. For the
            # citizenship / permanent-resident / export-control menus a submit reveals.
            "us_lawful_permanent_resident": recorded("us_permanent_resident"),
            "use": "Recorded owner facts. Answer a question about a country from that country's "
                   "own entry; with none, from the 'EU' entry when the country is a European "
                   "Union member state; '*' applies only where no entry covers it. Never claim "
                   "an authorization the map does not record."}


def materialize_computed_answers(database, job, questions, scoped_policy, now):
    """Write the per-job answers that are a rule rather than a stored value.

    Today that is exactly one: the earliest start date, recorded by the owner as "four
    weeks from the date of the job application". It is written into `application_answers` -
    the same table an authenticated owner decision writes to - so that every later reader
    sees an identical value. That matters concretely: `desktop_adapter.arm` re-runs
    `resolve_answers` immediately before arming a submission and refuses the attempt if the
    answers differ from the ones the attempt recorded. A value recomputed from the wall
    clock at each call site would differ across a midnight boundary and refuse a legitimate
    submission; a value persisted once does not.

    Two rules are enforced here, not left to a caller:

    - An authenticated owner answer is never overwritten. It is only checked against the
      floor the owner's own rule sets, and a value earlier than that floor - or one that is
      not a date at all - is reported as a violation so the caller can hold. An answered
      start date can never be earlier than application date plus the recorded offset.
    - The computed row is refreshed whenever the floor has moved past it, so a job held for
      weeks and then submitted does not carry a start date computed from a stale day.

    Notice period is deliberately not touched here. It is a fixed recorded value with its
    own semantic, and the two must never satisfy each other's question.
    """
    violations = []
    floor = earliest_start_floor(now, scoped_policy)
    if floor is None:
        return violations
    rule = earliest_start_rule(scoped_policy)
    source = (COMPUTED_ANSWER_SOURCE_PREFIX + EARLIEST_START_RULE_SEMANTIC
              + ":application_date=" + str(now)[:10] + "+" + str(rule["offset_days"]) + "d")
    for question in questions:
        if not isinstance(question, dict) or not question.get("required"):
            continue
        if question.get("semantic") not in EARLIEST_START_SEMANTICS:
            continue
        if question.get("choices"):
            # A start-date question shown as a menu ("Right away / In about 2 weeks / In over a
            # month / Open", Woodgrove 2026-10-06) takes none of these dates: the answering AI
            # picks the option that fits the floor (`answering_agent.materialize`), and its answer
            # is kept as given. A computed date here would come back after
            # `record_conditional_questions` dropped it and hide the menu from the AI again, and
            # one computed before the menu was known is removed (review round 8: woodgrove:9000000008
            # holds "2026-11-03" from a read that listed the question without its options).
            field = question_field(question)
            saved = scoped_policy["answers"].get(field)
            if saved and str(saved.get("source") or "").startswith(COMPUTED_ANSWER_SOURCE_PREFIX):
                database.execute("DELETE FROM application_answers WHERE job_key=? AND field=? AND source LIKE ?",
                                 (job["job_key"], field, COMPUTED_ANSWER_SOURCE_PREFIX + "%"))
                scoped_policy["answers"].pop(field, None)
            continue
        field = question_field(question)
        saved = scoped_policy["answers"].get(field)
        if saved and not str(saved.get("source") or "").startswith(COMPUTED_ANSWER_SOURCE_PREFIX):
            value = saved.get("value")
            if not isinstance(value, str) or len(value) < 10 or value[:10] < floor:
                violations.append(field)
            continue
        if saved and saved.get("value") == floor:
            continue
        record = {"semantic": question["semantic"], "value": floor, "source": source}
        database.execute("""INSERT INTO application_answers VALUES (?,?,?,?,?)
            ON CONFLICT(job_key,field) DO UPDATE SET semantic=excluded.semantic,value=excluded.value,source=excluded.source""",
            (job["job_key"], field, record["semantic"], json.dumps(floor), record["source"]))
        scoped_policy["answers"][field] = record
    return violations


def recorded_fact_for_ai(question, policy):
    """Does the owner have a recorded fact for this question that only the answering AI applies?

    Matched the way `resolve_answers` matches a general recorded fact - by the question's
    semantic, with no answer keyed to the question's own field - so an optional question the
    owner answered in advance is still put to the AI instead of being left blank.
    """
    answers = (policy or {}).get("answers") or {}
    if not isinstance(question, dict) or answers.get(question_field(question)) is not None:
        return False
    semantic = question.get("semantic")
    # Only a fact the owner recorded (in the answering packet) counts; an answer the AI wrote for
    # another question is not in the packet, so it alone does not put an optional question to the AI.
    return any(isinstance(answer, dict) and answer.get("source") and answer.get("semantic") == semantic
               and not str(answer["source"]).startswith("ai-answer:")
               and recorded_answer_for_ai(answer, question) for answer in answers.values())


def resolve_answers(questions, policy):
    answers = {}
    missing = []
    seen = set()
    for question in questions:
        # Owner, 2026-09-21: an OPTIONAL question is resolved too when a recorded answer exists,
        # so an SMS/text consent or a voluntary self-identification control the owner has already
        # decided is actually filled instead of left blank. Pawnee's form uploaded both files and
        # populated every required field, and Apply stayed disabled because the consent radios
        # were never answered - they were skipped right here, before the answering step ever saw
        # them. What an optional question may NOT do is join `missing`: that list holds the job
        # (ANSWERING_RETRY, or the owner-question hold) and refuses the submit gate, so a voluntary field
        # nobody recorded an answer for would park every form forever. (`missing` is retried by the
        # AI as ANSWERING_RETRY, or asked on Approvals when only the owner can answer it: see
        # `owner_questions` in `process`.)
        optional = not question.get("required")
        field = question_field(question)
        semantic = question.get("semantic")
        saved = policy["answers"].get(field)
        if saved is None:
            # A computed start date never stands in for a menu question (review round 9: a submit
            # session's label drift gave the menu a field of its own, and another field's date
            # bound to it, so the menu never reached the answering AI).
            # The newer answer per semantic (`answers_in_force`, D1): a kept dashboard answer and the
            # Setup answer it superseded are not two conflicting matches (E6.T4 review 9).
            matches = [answer for answer in answers_in_force(policy["answers"]).values() if answer.get("source") and answer.get("semantic") == semantic
                       and not (question.get("choices") and str(answer["source"]).startswith(COMPUTED_ANSWER_SOURCE_PREFIX))]
            if matches and len({json.dumps(answer["value"], sort_keys=True) for answer in matches}) == 1:
                # This is the branch that matches a *general recorded fact* onto a question by
                # the semantic the employer's browser AI authored, and it is the branch every
                # jurisdiction-confusion finding ran through: a recorded answer about the US
                # binding to a question about Germany. It is gated by the check the answering
                # step shares, so an answer that step withholds is not submittable here either.
                #
                # The branch above is not gated, deliberately: an answer keyed to this
                # question's own field was recorded for THIS question - by an authenticated
                # owner decision, by the computed start-date rule, or by the answering step
                # after it passed the same gate - so there is no question of it being about
                # something else.
                if not recorded_answer_for_ai(matches[0], question):
                    saved = matches[0]
        if saved and semantic in RESUME_SEMANTICS and saved.get("value") != (policy.get("resume") or {}).get("path"):
            # The main resume control takes the designated resume, carried out of band and
            # hash-checked (other file controls may take a generated cover letter). An answer for
            # the resume question is therefore allowed to be exactly one thing: a pointer to
            # that same designated file. Anything else - an owner-supplied path, a stale
            # path from an earlier policy - is refused rather than attached.
            saved = None
        if not saved or not saved.get("source") or semantic != saved.get("semantic"):
            if optional:
                continue
            if field not in seen:
                seen.add(field)
                missing.append(field)
        else:
            answers[field] = saved["value"]
    return answers, missing


class AdapterUnavailable(RuntimeError):
    pass


class AdapterPending(RuntimeError):
    def __init__(self, request_id, next_at):
        self.request_id = request_id
        self.next_at = next_at
        super().__init__("DESKTOP_BROWSER_PENDING")


def wait_for_adapter(database, job, pending, now, state):
    with database:
        hold(database, job, getattr(pending, "code", "DESKTOP_BROWSER_PENDING"),
             getattr(pending, "next_action", "Existing desktop browser must service request " + pending.request_id),
             now, getattr(pending, "state", state), pending.next_at)


class CommandAdapter:
    def __init__(self, config):
        self.command = config.get("application_adapter_command")
        self.can_submit = config.get("execution_ownership_verified", False)

    def call(self, operation, payload):
        if not self.command:
            raise AdapterUnavailable("ATS_BROWSER_ADAPTER_NOT_CONFIGURED")
        if not isinstance(self.command, list) or not Path(self.command[0]).is_absolute():
            raise AdapterUnavailable("ATS_ADAPTER_REQUIRES_ABSOLUTE_EXECUTABLE")
        result = subprocess.run(self.command + [operation], input=json.dumps(payload), text=True,
                                capture_output=True, timeout=120, check=True, **portable.no_window())
        return json.loads(result.stdout)


def finish_receipt(database, job, receipt, now):
    import desktop_adapter
    desktop = desktop_adapter.submission_request(database, job["job_key"])
    attempt_id = None
    if desktop or receipt.get("_desktop_request"):
        attempt_id = desktop_adapter.acknowledge_result(database, job, receipt, now)
    if not verified_receipt(receipt, job, now):
        raise ValueError("VERIFIED_RECEIPT_REQUIRED")
    mark_submitted(database, job, receipt, attempt_id, now)


def mark_submitted(database, job, receipt, attempt_id, now, notify_owner=True):
    updated = database.execute("UPDATE application_jobs SET state='SUBMITTED',receipt=?,blocker=NULL,next_action='Monitor verified application for response',next_at=NULL,updated_at=? WHERE job_key=? AND state NOT IN ('SUBMITTED','ABANDONED')",
                               (json.dumps(receipt), now, job["job_key"]))
    if updated.rowcount != 1:
        return False
    database.execute("DELETE FROM application_blockers WHERE job_key=?", (job["job_key"],))
    database.execute("INSERT OR IGNORE INTO application_history VALUES (?,?, 'APPLICATION',?)",
                     (job["employer"], job["requisition"], json.dumps(receipt)))
    database.execute("UPDATE application_attempts SET state='VERIFIED',result=? WHERE job_key=? AND state='SUBMITTING' AND (? IS NULL OR attempt_id=?)",
                     (json.dumps(receipt), job["job_key"], attempt_id, attempt_id))
    event(database, job["job_key"], "SUBMISSION_VERIFIED", receipt, now)
    if notify_owner:
        notify(database, job["job_key"] + ":submitted", f"Application verified: {job['employer']} / {job['requisition']}. Receipt: {receipt['receipt_id']}", now)
    return True


def settle_page_confirmation(database, job, now, root=None, notify_owner=True):
    """A captured page confirmation is the verified submission (owner, 2026-10-06: "If the agent
    captures a page confirmation, that should be useed as a verified submission. no need for any
    additional python comparisons").

    The job's open attempt has an armed submit (the one Submit went through the arm guard) whose
    session reported the page acknowledged it: the job is SUBMITTED, the receipt being what the
    page said. The page text is recorded, never compared. Returns True when settled.
    """
    import desktop_adapter

    request = desktop_adapter.submission_request(database, job["job_key"])
    if not request or not request["armed_at"]:
        return False
    # A page confirmation only: an email application keeps its own reconcile (the Sent folder).
    # Decided from the attempt's own saved review, as the Sent-folder check does (the request
    # payload does not carry the email target).
    attempt = database.execute("SELECT evidence FROM application_attempts WHERE attempt_id=?", (request["attempt_id"],)).fetchone()
    try:
        review = json.loads(attempt[0] or "{}").get("review") if attempt else None
    except (TypeError, ValueError, AttributeError):
        review = None
    if isinstance(review, dict) and review.get("application_method") == EMAIL_METHOD:
        return False
    # A request whose session lost its lease has no stored result; its own report may still hold
    # the confirmation (review, 2026-10-06).
    try:
        result = json.loads(request["result"]) if request["result"] else {}
    except (ValueError, TypeError):
        result = {}
    if not isinstance(result, dict):
        return False
    confirmation = result.get("confirmation_text")
    if root is not None and (result.get("acknowledged") is not True or not confirmation):
        # What the agent itself reported is in the session folder (output.json). Checks since
        # deleted (the quote binding and the post-arm trace rule, removed 2026-09-27/28) dropped
        # some of those reports and stored acknowledged false: Hanso x2, Lucerne Energy, Massive Dynamic and
        # Primatech x2 each reported the page's confirmation and sat unconfirmed. Submits recorded
        # before 2026-10-06 also kept only `acknowledged`, so the page's words come from here.
        reported = session_report(root, request["request_id"])
        if reported.get("acknowledged") is True:
            result = dict(result, acknowledged=True)
            import application_browser
            confirmation = confirmation or application_browser.page_confirmation(reported)
    if result.get("acknowledged") is not True:
        return False
    receipt = {"status": "SUBMITTED", "evidence_kind": "page_confirmation", "receipt_id": request["request_id"],
               "job_key": job["job_key"], "attempt_id": request["attempt_id"], "armed_at": request["armed_at"],
               "confirmation_text": confirmation, "observed_at": now,
               "basis": "The submit page acknowledged the application after the armed Submit"}
    if not database.in_transaction:
        database.execute("BEGIN IMMEDIATE")
    with database:
        return mark_submitted(database, job, receipt, request["attempt_id"], now, notify_owner)


_RESCANNED_FOR = set()


def session_report(root, request_id):
    """The submit session's own report (its output.json `result_json`, with the evidence beside it),
    or {} when there is none."""
    import application_browser
    try:
        folder = application_browser.session_folders(root).get(request_id)
        if not folder and request_id not in _RESCANNED_FOR:
            # The folder map is built once per process; a session that started after that is
            # found by one fresh scan (at most once per request and process).
            _RESCANNED_FOR.add(request_id)
            application_browser._SESSION_FOLDERS.clear()
            folder = application_browser.session_folders(root).get(request_id)
        if not folder:
            return {}
        output = json.loads((Path(folder) / "output.json").read_text())
        reported = json.loads(output.get("result_json") or "{}") if isinstance(output, dict) else {}
        if not isinstance(reported, dict):
            return {}
        if isinstance(output.get("evidence"), list):
            reported.setdefault("browser_evidence", output["evidence"])
        return reported
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def _named(job):
    """'Company, role' from the job's own listing, as the daily summary names it."""
    try:
        payload = json.loads(job["payload"] or "{}")
    except (TypeError, ValueError):
        payload = {}
    payload = payload if isinstance(payload, dict) else {}
    company = payload.get("company") if isinstance(payload.get("company"), str) and payload["company"].strip() else job["employer"]
    role = payload.get("role") if isinstance(payload.get("role"), str) and payload["role"].strip() else job["requisition"]
    return company + ", " + role


def settle_page_confirmations(database, now, root=None, only=None):
    """Settle every job waiting on a submission whose armed submit captured a page confirmation."""
    settled = []
    # Also a job held after its reconcile reads ran out (BLOCKED/SUBMISSION_OUTCOME_UNKNOWN): its
    # session's own report may still hold the confirmation.
    for job in list(database.execute("""SELECT * FROM application_jobs WHERE state IN ('SUBMITTING','RECONCILE')
            OR state='BLOCKED' AND blocker='SUBMISSION_OUTCOME_UNKNOWN' ORDER BY job_key""")):
        if only is not None and job["job_key"] not in only:
            continue
        # One job's failure (a locked database, a damaged record) is that job's: the rest of the
        # cycle goes on, and the job is settled on a later pass or by its own reconcile.
        try:
            if settle_page_confirmation(database, job, now, root, notify_owner=False):
                settled.append(job)
        except Exception as error:
            if database.in_transaction:
                database.rollback()
            # Recorded, never swallowed: the job stays as it was and the next pass tries again.
            try:
                with database:
                    event(database, job["job_key"], "PAGE_CONFIRMATION_SETTLE_FAILED",
                          {"error_type": type(error).__name__, "error": str(error)[:300]}, now)
            except sqlite3.Error:
                pass
    if settled:
        # One message for the pass, not one per job (on the first run after 2026-10-06 about 21
        # waiting jobs settle at once).
        # The jobs are already SUBMITTED; a failed notification (a database locked past its
        # timeout) loses only this message, never the cycle: the daily summary lists every
        # verified application from its SUBMISSION_VERIFIED event.
        try:
            with database:
                notify(database, "settled-page-confirmations:" + now,
                       f"Applications verified by the page's own confirmation ({len(settled)}): "
                       + "; ".join(_named(job) for job in settled[:25])
                       + (f"; and {len(settled) - 25} more" if len(settled) > 25 else ""), now)
        except sqlite3.Error:
            pass
    return [job["job_key"] for job in settled]


def __ad():
    import application_destination
    return application_destination


def verified_receipt(receipt, job, now):
    if not isinstance(receipt, dict):
        return False
    metadata = receipt.get("_desktop_request")
    evidence_time = metadata.get("accepted_at", now) if isinstance(metadata, dict) else now
    if not (fresh(receipt, evidence_time)
            and receipt.get("status") == "SUBMITTED" and receipt.get("independent_read") is True
            and __ad().employer_accepted(receipt, job)
            and receipt.get("candidate_email") == job["candidate_email"]
            and bool(receipt.get("receipt_id"))):
        return False
    # `.get(key, "")` returns None when the key is PRESENT and null, and str(None) is "none" -
    # a truthy string that sent a null requisition down the strict branch and compared "none"
    # against the real id. An email receipt reports requisition null, so this rejected exactly
    # the receipts CX exists to accept (dharma, 2026-09-21 09:10Z).
    reported = receipt.get("requisition")
    requisition = str(reported).casefold() if reported is not None else ""
    if requisition:
        return requisition == job["requisition"]
    # Owner, 2026-09-21. An employer confirmation email is one of the two proofs this product
    # asks for, and Ashby's ("Thank you for applying to the Member of Technical Staff - Systems
    # position at Dharma!") names the ROLE and never the requisition id. Demanding one rejected a
    # real receipt for lacking a field the evidence cannot contain, and dharma's verified
    # submission sat at SUBMISSION_OUTCOME_UNKNOWN because of it. The email path instead binds to
    # what an email CAN prove and Python can check without trusting the model: the job_key and
    # attempt_id it echoes must equal the ones this product issued, and the provider's own
    # message id must be present. An ATS record still carries a requisition and is unchanged.
    # The candidate's own sent message (owner, 2026-09-24) binds the same way an employer's
    # confirmation does: by the job_key this product issued, with no requisition to carry. Its
    # recipient and send time are checked against the attempt in validate_read_result.
    return (receipt.get("evidence_kind") in {"employer_confirmation_email", "sent_application_email"}
            and receipt.get("job_key") == job["job_key"])


def retry(database, job, policy, now, reason):
    attempts = database.execute("SELECT attempts FROM application_jobs WHERE job_key=?", (job["job_key"],)).fetchone()[0]
    if attempts >= policy["max_attempts"]:
        hold(database, job, "RETRIES_EXHAUSTED", "Inspect failure evidence and explicitly requeue only after resolution", now, "EXHAUSTED")
    else:
        delay = policy["retry_delays_seconds"][min(max(attempts - 1, 0), len(policy["retry_delays_seconds"]) - 1)]
        next_at = (dt.datetime.fromisoformat(now) + dt.timedelta(seconds=delay)).isoformat()
        hold(database, job, reason, "Retry after bounded backoff; submission absence is established", now, "RETRY_WAIT", next_at)


def stop_code(result):
    """The stop a submit result states, wherever it states it (`blocked`/`blocker`, dict or string)."""
    if not isinstance(result, dict):
        return None
    for key in ("blocked", "blocker"):
        value = result.get(key)
        if isinstance(value, dict):
            code = value.get("code") or value.get("blocker_code")
            if isinstance(code, str) and code.strip():
                return code.strip()
        elif isinstance(value, str) and value.strip():
            return value.strip()
    return None


def upload_failures(database, job_key):
    """Never-armed submit requests for this job that stopped on an upload failure, oldest first.

    A reopen recorded under UPLOAD_RESET_REOPEN_CLASS (the owner-approved embedded-form reopen,
    2026-10-02) answers the upload failures before it: only requests created after the latest
    such APPLICATION_REOPENED count, so the reopened job is not given up again on them. A reopen
    under any other defect class resets nothing.
    """
    if not database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='desktop_requests'").fetchone():
        return []
    since = None
    for at, detail in database.execute("SELECT at,detail FROM application_events WHERE job_key=? "
                                       "AND event='APPLICATION_REOPENED' ORDER BY id DESC", (job_key,)):
        try:
            classes = (json.loads(detail).get("reason") or {}).get("defect_classes") or []
        except (TypeError, ValueError, AttributeError):
            continue
        if isinstance(classes, list) and UPLOAD_RESET_REOPEN_CLASS in classes:
            since = dt.datetime.fromisoformat(at)
            break
    failures = []
    for request_id, created_at, raw in database.execute(
            "SELECT request_id,created_at,result FROM desktop_requests WHERE job_key=? AND operation='submit' "
            "AND armed_at IS NULL AND result IS NOT NULL ORDER BY rowid", (job_key,)):
        try:
            code = stop_code(json.loads(raw))
            if since is not None and dt.datetime.fromisoformat(created_at) <= since:
                continue
        except (TypeError, ValueError):
            continue
        if code in UPLOAD_FAILURE_CODES:
            failures.append({"request_id": request_id, "created_at": created_at, "code": code})
    return failures


def uncertain_submission(database, job_key):
    """True while anything recorded for this job may have reached the employer.

    A live SUBMITTING attempt, an armed submit request whose attempt is not settled as
    CONFIRMED_NOT_SUBMITTED, or an uncertain-submission blocker. Such a job is never given up.
    """
    if database.execute("SELECT 1 FROM application_attempts WHERE job_key=? AND state='SUBMITTING' LIMIT 1",
                        (job_key,)).fetchone():
        return True
    row = database.execute("SELECT blocker FROM application_jobs WHERE job_key=?", (job_key,)).fetchone()
    if row and (row[0] in UNCERTAIN_SUBMISSION_CODES or row[0] in ALWAYS_RECONCILE_CODES):
        return True
    if database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='desktop_requests'").fetchone():
        if database.execute("""SELECT 1 FROM desktop_requests AS request
                LEFT JOIN application_attempts AS attempt ON attempt.attempt_id=request.attempt_id
                WHERE request.job_key=? AND request.operation='submit' AND request.armed_at IS NOT NULL
                AND COALESCE(attempt.state,'') <> 'CONFIRMED_NOT_SUBMITTED' LIMIT 1""", (job_key,)).fetchone():
            return True
    return False


def give_up_on_uploads(database, job, now):
    """EXHAUSTED after UPLOAD_FAILURE_LIMIT upload failures; never with an uncertain submission.

    Call inside a transaction. Returns True when the job was given up.
    """
    failures = upload_failures(database, job["job_key"])
    if len(failures) < UPLOAD_FAILURE_LIMIT or uncertain_submission(database, job["job_key"]):
        return False
    action = ("Given up: the resume or a required file could not be uploaded in %d submit sessions "
              "(each stopped before arming, so nothing was submitted); requeue only after uploads "
              "work on this site" % len(failures))
    if not hold(database, job, UPLOAD_GIVE_UP_CODE, action, now, "EXHAUSTED"):
        return False
    event(database, job["job_key"], UPLOAD_GIVE_UP_EVENT,
          {"failures": failures, "limit": UPLOAD_FAILURE_LIMIT, "submission_status": "NOT_SUBMITTED",
           "reason": "upload failed %d times" % len(failures), "next_action": action}, now)
    return True


def access_blocks(database, job_key):
    """Inspect, investigate and never-armed submit sessions of this job that stopped on an access
    block (ACCESS_BLOCK_CODES), oldest first; only those created after its latest reopen."""
    if not database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='desktop_requests'").fetchone():
        return []
    reopened = database.execute("SELECT at FROM application_events WHERE job_key=? AND event='APPLICATION_REOPENED' "
                                "ORDER BY id DESC LIMIT 1", (job_key,)).fetchone()
    blocks = []
    for request_id, operation, created_at, raw in database.execute(
            "SELECT request_id,operation,created_at,result FROM desktop_requests WHERE job_key=? "
            "AND operation IN ('inspect','investigate','submit') AND (operation<>'submit' OR armed_at IS NULL) "
            "AND result IS NOT NULL ORDER BY rowid", (job_key,)):
        try:
            code = stop_code(json.loads(raw))
            if reopened and dt.datetime.fromisoformat(created_at) <= dt.datetime.fromisoformat(reopened[0]):
                continue
        except (TypeError, ValueError):
            continue
        if code in ACCESS_BLOCK_CODES:
            blocks.append({"request_id": request_id, "operation": operation, "created_at": created_at, "code": code})
    return blocks


def give_up_on_access_blocks(database, job, now):
    """ABANDONED after ACCESS_BLOCK_LIMIT access blocks; never with an uncertain submission or while
    the job is in RECONCILE. Call inside a transaction. Returns True when the job was abandoned."""
    blocks = access_blocks(database, job["job_key"])
    if len(blocks) < ACCESS_BLOCK_LIMIT or uncertain_submission(database, job["job_key"]):
        return False
    current = database.execute("SELECT state FROM application_jobs WHERE job_key=?", (job["job_key"],)).fetchone()
    if not current or current[0] == "RECONCILE":
        return False
    action = ("Abandoned: %d application sessions stopped on a login, verification-code or CAPTCHA wall "
              "(%s; each before arming, so nothing was submitted); requeue only after access works on this site"
              % (len(blocks), ", ".join(sorted({block["code"] for block in blocks}))))
    if not hold(database, job, ACCESS_GIVE_UP_CODE, action, now, "ABANDONED"):
        return False
    event(database, job["job_key"], ACCESS_GIVE_UP_EVENT,
          {"blocks": blocks, "limit": ACCESS_BLOCK_LIMIT, "submission_status": "NOT_SUBMITTED",
           "reason": "access blocked %d times" % len(blocks), "next_action": action}, now)
    return True


def hold_access_block(database, job, code, action, now):
    """An access block on the ordinary path: retried in an hour, abandoned on the third.
    Call inside a transaction."""
    next_at = (dt.datetime.fromisoformat(now) + dt.timedelta(seconds=ACCESS_BLOCK_RETRY_SECONDS)).isoformat()
    hold(database, job, code, action, now, "BLOCKED", next_at, route="ORDINARY")
    give_up_on_access_blocks(database, job, now)


def hold_submission_blocker(database, job, result, now):
    import desktop_adapter
    blocker = result.get("blocked")
    if not isinstance(blocker, dict) or any(not isinstance(blocker.get(key), str) or not blocker[key].strip()
                                          for key in ("code", "next_action")):
        raise ValueError("STRUCTURED_SUBMISSION_BLOCKER_REQUIRED")
    if not database.in_transaction:
        database.execute("BEGIN IMMEDIATE")
    submit = desktop_adapter.submission_request(database, job["job_key"])
    if submit:
        if (submit["state"] != "DONE" or submit["armed_at"] or json.loads(submit["result"]) != result
                or result.get("prearm_blocked") is not True):
            raise ValueError("PREARM_BLOCKER_BINDING_MISMATCH")
        database.execute("UPDATE desktop_requests SET state='CONSUMED' WHERE request_id=?", (submit["request_id"],))
        desktop_adapter.record(database, submit["request_id"], "PREARM_BLOCKER_DISPOSED", now)
    if isinstance(result.get("observed_questions"), list):
        # Conditional questions the guarded session saw appear before arming join the
        # per-requisition question set, so the next derivation asks for their answers.
        record_conditional_questions(database, job, result["observed_questions"], now)
    next_at = (dt.datetime.fromisoformat(now) + dt.timedelta(seconds=900)).isoformat()
    hold(database, job, blocker["code"], blocker["next_action"], now, "RECONCILE", next_at)


# A confirmation email usually lands in minutes and sometimes takes hours: Pawnee's Rippling
# receipt arrived in the same minute, Abstergo's had not arrived a minute after Submit. The
# reconcile read is a MAILBOX read - it sends nothing, cannot duplicate a submission, and its
# only cost is one read - so it widens rather than giving up (owner, 2026-09-22). It used to
# retry three times at a flat 15 minutes and then jump straight to once a DAY, which is the
# wrong shape twice over: too impatient for an ATS that takes an hour, then far too slow for
# one that would have answered by the next check. Widening and holding at a ceiling covers
# both, and there is still no expiry - an application that WAS submitted stays confirmable.
RECONCILE_FIRST_MINUTES = 15
RECONCILE_GROWTH = 4
RECONCILE_MAX_MINUTES = 7 * 24 * 60
# Owner, 2026-09-22: "max retry should be 10. so it doesnt run adinfinitum". Schedule
# (reconcile_after_read): 3 reads 15 min apart, then 16h, ~2.7d, then 7 days, 10 reads total,
# after which the job stops re-reading and holds for the owner instead of polling forever.
RECONCILE_MAX_ATTEMPTS = 10
RECONCILE_SPENT_EVENT = "APPLICATION_RECONCILE_READS_SPENT"


def reconcile_next_at(attempts, now):
    """When to re-read the mailbox after `attempts` inconclusive reconcile reads.

    Widens 15m, 1h, 4h, 16h, ~2.7d and then holds at the 7-day ceiling - a real backoff to a
    maximum delay, not a flat poll. The previous shape retried three times at a flat 15
    minutes and then jumped to once a day, which was both too impatient for an ATS that takes
    an hour and, once past that, far too coarse; a 12-hour ceiling was no better, because a
    twice-daily read forever is not a backoff either (owner, 2026-09-22).

    This only computes the wait; the read count is bounded by reconcile_after_read, which
    stops after RECONCILE_MAX_ATTEMPTS (10) reads and holds the job for the owner (owner,
    2026-09-30). The read is a MAILBOX read: it sends nothing and cannot duplicate a
    submission, so the ceiling bounds the cost of each wait.
    """
    minutes = RECONCILE_FIRST_MINUTES * (RECONCILE_GROWTH ** max(int(attempts or 0), 0))
    return (dt.datetime.fromisoformat(now) + dt.timedelta(minutes=min(minutes, RECONCILE_MAX_MINUTES))).isoformat()


def reconcile_after_read(reads, policy, now):
    """When to read again after `reads` inconclusive reconcile reads (owner, 2026-09-30: a 10-read
    limit with backoff). The first `max_reconcile_attempts` reads are 15 minutes apart, then the
    wait widens (reconcile_next_at: 16h, ~2.7d, then 7 days). After the last read next_at is NULL,
    so the next cycle records the spent hold at once instead of waiting a week to say so."""
    if reads >= RECONCILE_MAX_ATTEMPTS:
        return None
    if reads < policy["max_reconcile_attempts"]:
        return (dt.datetime.fromisoformat(now) + dt.timedelta(seconds=900)).isoformat()
    return reconcile_next_at(reads, now)


SUBMIT_REQUEST_NEVER_CREATED = "SUBMIT_REQUEST_NEVER_CREATED"


def submit_request_never_created(database, adapter, job_key, attempt_id):
    """True when this attempt provably reached no employer: under the desktop adapter a submission
    happens only in a session that claims this attempt's submit request and arms it, and there is
    no such request row. 2026-09-30: Yoyodyne, Ingen, Cogswell, Encom and Bluesun raised
    KeyError('questions') after claiming SUBMITTING and before `adapter.call("submit")`, and sat in
    RECONCILE spending reconcile reads on a submission that never existed."""
    import desktop_adapter
    return (isinstance(adapter, desktop_adapter.DesktopAdapter)
            and not database.execute("SELECT 1 FROM desktop_requests WHERE job_key=? AND operation='submit' AND attempt_id=?",
                                     (job_key, attempt_id)).fetchone())


def release_uncreated_submission(database, job, attempt_ids, cause, now, *, free):
    """Close attempts whose submit request was never created as not submitted. Caller holds the
    transaction and then decides the wait or retry. `free` returns the try: only for a compute wait
    raised before the row existed (owner, 2026-09-28: work no session picked up is never a try) and
    for the reconcile release of historical rows. Any other failure before the row spends the try,
    so a deterministic bug reaches EXHAUSTED through the ordinary backoff instead of looping."""
    import application_recovery
    closed = 0
    for attempt_id in attempt_ids:
        closed += database.execute("UPDATE application_attempts SET state='CONFIRMED_NOT_SUBMITTED',result=? WHERE job_key=? AND state='SUBMITTING' AND attempt_id=?",
                                   (json.dumps({"status": "NOT_SUBMITTED", "basis": SUBMIT_REQUEST_NEVER_CREATED}), job["job_key"], attempt_id)).rowcount
    if free and closed:
        database.execute("UPDATE application_jobs SET attempts=MAX(attempts-?,0) WHERE job_key=?", (closed, job["job_key"]))
    event(database, job["job_key"], SUBMIT_REQUEST_NEVER_CREATED,
          {"attempts": attempt_ids, "closed": closed, "try_spent": not free,
           "cause": application_recovery.exception_failure(cause) if isinstance(cause, BaseException) else cause}, now)
    return closed


def _never_armed_submission(database, job, now):
    """True when this job's submit request provably never reached the employer.

    Submit is reachable only through the arm guard (`desktop_adapter.arm`), so a request with
    no `armed_at` cannot have sent anything. This says so only for a session that has ENDED -
    the same set `reconcile` uses below - because a live session may still arm.
    """
    import desktop_adapter
    submit = desktop_adapter.submission_request(database, job["job_key"])
    if not submit or submit["armed_at"]:
        return False
    return (submit["state"] in {"DONE", "CONSUMED", "UNKNOWN", "FAILED", "EXPIRED"}
            or (submit["state"] == "CLAIMED" and submit["lease_until"]
                and submit["lease_until"] <= now))


def reconcile(database, job, adapter, policy, now, clock=None, config=None):
    import desktop_adapter
    current = database.execute("SELECT state FROM application_jobs WHERE job_key=?", (job["job_key"],)).fetchone()
    if not current or current["state"] in FINAL:
        return
    # The submit page already confirmed it (owner, 2026-10-06): nothing left to reconcile.
    if settle_page_confirmation(database, job, now):
        return
    # A submit request that was never armed is CONCLUSIVE: submit is reachable only through the
    # arm guard, so nothing was sent, whatever any session afterwards reported. That evidence
    # used to be reachable only when `recover_result` returned None, so an ERRORED verify read
    # masked it and the job held as SUBMISSION_OUTCOME_UNKNOWN for ever. Vandelay Security on
    # 2026-09-22: its submit was refused at the gate (SUBMIT_GATE_BLOCKED, armed_at NULL) and
    # its verify session then died with APPLICATION_UNSOURCED_FIELD; that error was consumed
    # first, so the never-armed branch below never ran and the job could not retry. Reading the
    # request before the result puts the conclusive evidence first. Nothing is loosened: the
    # branches below still require the session to have ENDED and still never treat an armed
    # request this way.
    if _never_armed_submission(database, job, now):
        result = None
    else:
        result = desktop_adapter.recover_result(database, job, now)
    if result is None:
        submit = desktop_adapter.submission_request(database, job["job_key"])
        if submit and submit["state"] == "DONE" and not submit["armed_at"]:
            submission_result = json.loads(submit["result"])
            if submission_result.get("prearm_blocked") is True:
                with database:
                    hold_submission_blocker(database, job, submission_result, now)
                return
    if result is None:
        submit = desktop_adapter.submission_request(database, job["job_key"])
        # Submit is reachable only through the arm guard: an ended session that was never armed
        # cannot have submitted anything (Aperture/Pawnee/Dharma 2026-09-19, each blocked at the
        # resume upload). That is a known outcome, not an uncertain one, so the attempt is closed
        # and the job retries instead of holding as SUBMISSION_OUTCOME_UNKNOWN.
        # A submit request still CLAIMED after its lease expired has no consumer left: that session
        # ended (aperture, 2026-09-20, killed by a validation error), so it is an ended session
        # like the terminal states beside it, and the same never-armed reasoning applies.
        ended = submit and (submit["state"] in {"DONE", "CONSUMED", "UNKNOWN", "FAILED", "EXPIRED"}
                            or (submit["state"] == "CLAIMED" and submit["lease_until"]
                                and submit["lease_until"] <= now))
        # The same proof for attempts left SUBMITTING before the submit path resolved them itself:
        # no submit request bound to the attempt, under the desktop adapter, and older than a
        # request's whole lifetime so none can still be on its way. Free the first time for a job
        # (then paid), unlike the submit path's paid release: it catches rows our own bug left
        # behind (the 2026-09-30 KeyError) and a process that died between the claim and the
        # request, neither the job's doing.
        if not submit:
            stale = dt.datetime.fromisoformat(now) - dt.timedelta(seconds=desktop_adapter.REQUEST_SECONDS)
            if not database.in_transaction:
                database.execute("BEGIN IMMEDIATE")
            with database:
                orphans = [row[0] for row in database.execute(
                    "SELECT attempt_id,started_at FROM application_attempts WHERE job_key=? AND state='SUBMITTING'", (job["job_key"],))
                    if dt.datetime.fromisoformat(row[1]) <= stale
                    and submit_request_never_created(database, adapter, job["job_key"], row[0])]
                if orphans:
                    # Free once per job, then paid: a job that keeps leaving attempts without a
                    # request is failing for its own reason and must reach EXHAUSTED.
                    earlier = database.execute("SELECT 1 FROM application_events WHERE job_key=? AND event=? "
                                               "AND json_extract(detail,'$.cause')='RECONCILED_NO_SUBMIT_REQUEST' "
                                               "AND json_extract(detail,'$.try_spent')=0 LIMIT 1",
                                               (job["job_key"], SUBMIT_REQUEST_NEVER_CREATED)).fetchone()
                    release_uncreated_submission(database, job, orphans, "RECONCILED_NO_SUBMIT_REQUEST", now, free=not earlier)
                    retry(database, job, policy, now, SUBMIT_REQUEST_NEVER_CREATED)
            if orphans:
                return
        if submit and not submit["armed_at"] and ended:
            # A session that could not use a capability it needed (Chrome's file picker, the arm
            # guard, a killed session) failed for a reason the job could not influence, so it does
            # not spend one of the job's three attempts - the same rule provider usage limits get.
            # Aperture went EXHAUSTED on 2026-09-20 that way. Any other pre-arm blocker still counts.
            try:
                blocked = (json.loads(submit["result"]) or {}).get("blocked") or {}
            except (TypeError, ValueError):
                blocked = {}
            # CK taught `ended` above that a CLAIMED request past its lease is an ended session;
            # this test kept the older set and so refunded nothing for one. A session killed
            # mid-flight records no result at all, which is why `blocked` is empty here - it is
            # an environment failure by exactly the reasoning in the comment above, not a failed
            # application. Pawnee health went EXHAUSTED that way on 2026-09-21, on a
            # session that never reached the employer. The two tests now agree: ended, never
            # armed, and no recorded outcome of its own means the attempt is not spent.
            environment = (submit["state"] in {"UNKNOWN", "FAILED", "EXPIRED"}
                           or (submit["state"] == "CLAIMED" and not submit["result"])
                           or str(blocked.get("code") or "") in CAPABILITY_BLOCKERS)
            with database:
                if environment:
                    database.execute("UPDATE application_jobs SET attempts=MAX(attempts-1,0) WHERE job_key=?", (job["job_key"],))
                    event(database, job["job_key"], "ATTEMPT_NOT_COUNTED_CAPABILITY_BLOCKED",
                          {"request_id": submit["request_id"], "request_state": submit["state"],
                           "code": blocked.get("code")}, now)
                database.execute("UPDATE application_attempts SET state='CONFIRMED_NOT_SUBMITTED',result=? "
                                 "WHERE job_key=? AND state='SUBMITTING' AND attempt_id=?",
                                 (json.dumps({"status": "NOT_SUBMITTED", "basis": "GUARD_NEVER_ARMED",
                                              "request_id": submit["request_id"], "request_state": submit["state"]}),
                                  job["job_key"], submit["attempt_id"]))
                event(database, job["job_key"], "RECONCILED_GUARD_NEVER_ARMED",
                      {"request_id": submit["request_id"], "request_state": submit["state"]}, now)
                # Upload failures are free one at a time but bounded in total (owner, 2026-10-01).
                if not give_up_on_uploads(database, job, now):
                    if str(blocked.get("code") or "") in ACCESS_BLOCK_CODES:
                        # A login/2FA/CAPTCHA stop before arming: an hour, then the third abandons
                        # (owner, 2026-10-11).
                        hold_access_block(database, job, blocked["code"], str(blocked.get("next_action") or "")
                                          or "Retry the application after the access block", now)
                    else:
                        retry(database, job, policy, now, "RECONCILED_ABSENT")
            return
    recovery_enabled = (config or {}).get("application_recovery", {}).get("enabled") is True
    spent = job["reconcile_attempts"] >= RECONCILE_MAX_ATTEMPTS
    if (result is None and job["reconcile_attempts"] >= policy["max_reconcile_attempts"]
            and (spent or not recovery_enabled)):
        # Past the fast attempts the job keeps re-reading, each read's own hold setting the
        # widening wait (reconcile_after_read), until RECONCILE_MAX_ATTEMPTS; then it stops and
        # holds for the owner. This branch used to fire for every job past the fast attempts and
        # re-hold it WITHOUT reading, so reads really stopped at max_reconcile_attempts (3) and
        # the 10-read bound was never reached (owner, 2026-09-30: "Uncertain submission checks
        # should have a 10 retry limit (with retry backoff)"). Recovery disabled is unchanged:
        # no further reads, held with next_at NULL.
        with database:
            next_at = None
            action = ("Automatic reconciliation is spent after %d reads%s; check the ATS account or "
                      "confirmation inbox yourself and record the outcome before any new submission"
                      % (job["reconcile_attempts"], "" if recovery_enabled else " (application recovery is disabled)"))
            # Say it ONCE, and only on the transition (owner, 2026-09-22). `hold` writes
            # nothing when the state and blocker already match, and a spent job keeps
            # `next_at` NULL - which the job loop SELECTS - so every later cycle re-entered
            # reconcile, re-held identically and emitted nothing at all. That is the silent
            # no-op that made five capped jobs look healthy while doing nothing for three
            # days; recording the transition is what makes this one visible instead.
            already = database.execute("SELECT 1 FROM application_events WHERE job_key=? AND event=? LIMIT 1",
                                       (job["job_key"], RECONCILE_SPENT_EVENT)).fetchone()
            if not already:
                event(database, job["job_key"], RECONCILE_SPENT_EVENT,
                      {"reconcile_attempts": job["reconcile_attempts"], "bound": RECONCILE_MAX_ATTEMPTS,
                       "recovery_enabled": recovery_enabled, "next_action": action}, now)
            hold(database, job, "SUBMISSION_OUTCOME_UNKNOWN", action, now, "BLOCKED", next_at)
        return
    if result is None:
        try:
            result = adapter.call("reconcile", {"job": job, "idempotency_key": job["job_key"]})
        except AdapterPending as pending:
            wait_for_adapter(database, job, pending, now, "RECONCILE")
            return
        except Exception as error:
            import application_recovery
            with database:
                event(database, job["job_key"], "RECONCILE_CALL_EXCEPTION",
                      {"exception": application_recovery.exception_failure(error)}, now)
            result = {}
    if clock:
        now = clock()
    with database:
        desktop = desktop_adapter.submission_request(database, job["job_key"])
        attempt_id = None
        if (desktop or result.get("_desktop_request")) and result.get("status") != "SUBMITTED":
            if result.get("_desktop_request"):
                attempt_id = desktop_adapter.acknowledge_result(database, job, result, now)
            elif result.get("status") == "NOT_SUBMITTED":
                result = {}
        blocker = result.get("blocked")
        if (isinstance(blocker, dict)
                and blocker.get("code") in {"AUTHENTICATION_REQUIRED", "CAPTCHA_REQUIRED", "MFA_REQUIRED"}
                and isinstance(blocker.get("next_action"), str) and blocker["next_action"].strip()
                and result.get("status") not in {"SUBMITTED", "NOT_SUBMITTED"}
                and (attempt_id is not None or not desktop and not result.get("_desktop_request"))):
            # An access-blocked read is an inconclusive read like any other: it counts toward
            # RECONCILE_MAX_ATTEMPTS on the same backoff (owner, 2026-09-30). It used to return
            # before the count, so it repeated forever every 15 minutes while the count was 0.
            updated = database.execute("UPDATE application_jobs SET reconcile_attempts=reconcile_attempts+1 WHERE job_key=? AND state NOT IN ('SUBMITTED','DUPLICATE','INELIGIBLE','EXHAUSTED','ABANDONED')", (job["job_key"],))
            if updated.rowcount != 1:
                return
            next_at = reconcile_after_read(job["reconcile_attempts"] + 1, policy, now)
            hold(database, job, blocker["code"], blocker["next_action"], now, "RECONCILE", next_at)
            event(database, job["job_key"], "RECONCILE_ACCESS_BLOCKED", blocker, now)
            return
        updated = database.execute("UPDATE application_jobs SET state='RECONCILE',reconcile_attempts=reconcile_attempts+1 WHERE job_key=? AND state NOT IN ('SUBMITTED','DUPLICATE','INELIGIBLE','EXHAUSTED','ABANDONED')", (job["job_key"],))
        if updated.rowcount != 1:
            return
        event(database, job["job_key"], "RECONCILE_READ", {}, now)
        if verified_receipt(result, job, now):
            finish_receipt(database, job, result, now)
        elif (fresh(result, now) and result.get("status") == "NOT_SUBMITTED"
              and result.get("authoritative_absence") is True and result.get("no_inflight") is True
              and result.get("job_key") == job["job_key"]):
            database.execute("UPDATE application_attempts SET state='CONFIRMED_NOT_SUBMITTED',result=? WHERE job_key=? AND state='SUBMITTING' AND (? IS NULL OR attempt_id=?)", (json.dumps(result), job["job_key"], attempt_id, attempt_id))
            retry(database, job, policy, now, "RECONCILED_ABSENT")
        else:
            # Three outcomes, not one (owner, 2026-09-24). This branch used to be a catch-all that
            # called everything SUBMISSION_OUTCOME_UNKNOWN, so a page that reported a FAILURE was
            # indistinguishable from one that acknowledged success and from a browser that died
            # having observed nothing. Rekall is the case: it clicked submit, the page failed,
            # and the job went to RECONCILE to re-check an outcome that was already known, while
            # the Ashby link the page offered was never followed. `acknowledged` is the only
            # outcome-shaped fact a submit may produce, and nothing here had ever read it.
            #
            # The result is recorded on the attempt in every branch: without that, nothing
            # downstream - including the recovery coordinator - can see what the page said.
            import desktop_adapter

            verdict = desktop_adapter.submission_verdict(database, job["job_key"], attempt_id)
            database.execute("UPDATE application_attempts SET result=? WHERE job_key=? AND state='SUBMITTING' AND (? IS NULL OR attempt_id=?)",
                             (json.dumps({"reconcile_read": result, "submission_verdict": verdict}), job["job_key"], attempt_id, attempt_id))
            if verdict is not None and not verdict["acknowledged"] and verdict["reported_failure"]:
                # A WITNESSED failure: the page said so. That is a known negative, not an unknown,
                # so it goes to recovery to act on it rather than waiting to re-discover it. It is
                # still not an authoritative absence, so it never permits a blind resubmission -
                # recovery must establish a route, and the action guard still applies.
                # Still RECONCILE, not RECOVER: the attempt is outstanding, and `job_route`
                # rightly sends any job that may have reached an employer to reconciliation
                # first. A reported failure is a known negative but NOT an authoritative
                # absence, so it cannot shortcut that guard. What changes is that the failure
                # and the route the page offered are recorded, so the reconcile read and - once
                # the attempt is resolved - the recovery coordinator can act on them instead of
                # rediscovering the failure from scratch.
                event(database, job["job_key"], "SUBMISSION_REPORTED_FAILED",
                      {"reported_failure": verdict["reported_failure"],
                       "observed_alternatives": verdict["observed_alternatives"],
                       "next_action": "Reconcile against the observed alternative route; an authoritative absence is still required before any resubmission"}, now)
                next_at = reconcile_after_read(job["reconcile_attempts"] + 1, policy, now)
                hold(database, job, "SUBMISSION_REPORTED_FAILED",
                     "The submit page reported a failure; reconcile against the observed alternative route and never blindly retry",
                     now, "RECONCILE", next_at)
            elif verdict is not None and verdict["acknowledged"]:
                # The page acknowledged the submission and no confirmation has been seen yet. This
                # is what SUBMISSION_OUTCOME_UNKNOWN now means, and only this.
                next_at = reconcile_after_read(job["reconcile_attempts"] + 1, policy, now)
                hold(database, job, "SUBMISSION_OUTCOME_UNKNOWN",
                     "The page acknowledged the submission; reconcile the confirmation independently and never blindly retry",
                     now, "RECONCILE", next_at)
            else:
                # Nothing was observed at all - no acknowledgment and no reported failure. A submit
                # may still have landed, so this keeps exactly the protection the catch-all had:
                # reconcile, never a blind retry.
                next_at = reconcile_after_read(job["reconcile_attempts"] + 1, policy, now)
                hold(database, job, "SUBMISSION_OBSERVATION_LOST",
                     "The submit produced no observation; reconcile independently and never blindly retry a potentially accepted submission",
                     now, "RECONCILE", next_at)


# --------------------------------------------------------------------------------------
# Part C (owner-approved plan, 2026-09-19): one read per unchanged input state, a durable
# per-requisition record, and a verification path a public form can actually reach.
#
# The loop this closes: an inconclusive inspect (FORM_REVIEW_INCOMPLETE and friends) was
# followed by another identical inspect, or by a coordinator call choosing an investigation
# whose result can never be applied to the fields that were missing (`apply_resolution`
# applies identity, destination and prior-history comparisons only). Nothing remembered what
# was missing, so every cycle, every policy edit and every reworded instruction bought
# another read of the same page.

REQUIRED_FACTS = ("open", "funding_stage", "industry", "level", "specialization", "country", "city",
                  "work_mode", "requirements_met", "authorization", "hiring_geography_includes_candidate",
                  "citizenship_clearance_requirements_met")
# Owner decision 2026-09-19: these are read and recorded but never block when unknown.
# funding_stage/industry: warnings (a KNOWN funding stage outside the policy still holds);
# city: only needed when nothing else can evaluate the owner's onsite exclusion
# (`location_exclusion` holds that case for scope review, never finally).
# work_mode joins them 2026-09-22 (owner: the AI should not block on this). A posting that
# never says remote/hybrid/onsite is not evidence of anything, and holding for it is the
# absence-of-evidence block this project keeps removing. Nothing downstream needed it to be
# KNOWN: `scope_refusal` already returns None for a mode outside WORK_MODES, so an unknown
# mode never produced a refusal - only this hold. The location exclusion it feeds applies to
# onsite/hybrid, and a mode that was never stated cannot be either; the geography question is
# carried by `hiring_geography_includes_candidate`, which the read judges directly and which
# still refuses finally when False. Lucerne Energy sat here with that fact already True.
INFORMATIONAL_FACTS = frozenset({"funding_stage", "industry", "city", "work_mode"})
BLOCKING_FACTS = tuple(field for field in REQUIRED_FACTS if field not in INFORMATIONAL_FACTS)
# Bumped whenever the inspect contract or what the host derives in code changes. A record read
# under an older contract is not evidence that a read under this one would be inconclusive,
# so a change here is "new input" and permits exactly one fresh read per job.
# 2026-09-22: bumped for the first time since it was introduced, and it was overdue. The
# contract changed roughly ten times in between - the answer key formula, the question schema
# and field echo, employer and requisition reconciliation, what the read must judge, and now
# the removal of the Gmail history search and the history rules in the prompt - while this
# string stayed on 2026-09-19. Every one of those changes left stale read suppressions and
# spent inspect claims in force against code that no longer behaved the way they recorded.
# Bumping it is what makes a read taken under the old contract stop counting.
# Bumped again the same day for FJ/FK: the browser prompt was one 28.4KB block handed to every
# operation, of which 16.2KB was instructions for other operations, and it is now assembled per
# operation - an inspect reads 13.1KB. That is a change to what a read is told to do, so a read
# taken under the old contract is not evidence that a read under this one would be inconclusive,
# which is precisely what this constant governs.
# 2026-09-27: bumped with the owner's four-step application workflow. The host no longer
# derives identity, official-destination, freshness, missing-fact or on-page-history holds from
# a read, so a read recorded as inconclusive under the old contract says nothing about this one.
# 2026-10-23: bumped with per-location authorization verdicts (J1 A), so a saved read without
# them is not reused and every job is read again with one verdict per location option.
READ_CONTRACT_REVISION = "inspect-contract-2026-10-23-per-location-authorization"
# When READ_CONTRACT_REVISION last changed (at or before its install). Inspect claims made
# before it were read under a superseded contract and do not count toward the per-scope
# inspect cap (`application_connection.inspect_scope_start`). Bump both together.
READ_CONTRACT_REVISION_AT = "2026-10-23T00:00:00+00:00"
# Seven days, deliberately (owner, 2026-09-22, after trying two). This is how long an
# inconclusive read suppresses the next one. Shortening it treats the symptom: the suppression
# is keyed on the job's INPUT and cannot see that the reading code changed, so a shorter window
# only bounds how long a stale suppression lasts rather than preventing one. The real lever is
# READ_CONTRACT_REVISION - bump it when the inspect contract changes, or derive it from a hash
# of the prompt so it bumps itself - and a stale record is cleared per job when one is found.
RECORD_MAX_AGE_SECONDS = 7 * 86400
FORM_FINGERPRINT_PREFIX = "questions-sha256:"
CODE_FORM_REVISION_BASIS = "CODE_COMPUTED_REQUIRED_QUESTION_FINGERPRINT"
CANDIDATE_BINDING_DEFERRED = "DEFERRED_TO_GUARDED_SUBMIT_SESSION"
RESEARCH_BASIS = "RESEARCH_STORED"
# Company-level research facts (funding stage, headcount band) move on the scale of funding
# rounds and quarterly hiring, not days. 30 days keeps one research observation useful to every
# job at the employer for a month while bounding how stale a stated stage can be.
RESEARCH_FACT_MAX_AGE_SECONDS = 30 * 86400
INCONCLUSIVE_EVENT = "APPLICATION_READ_INCONCLUSIVE"
# A fresh read assessed ELIGIBLE and saved complete for reuse (owner, 2026-10-02); such a read
# never counts toward the three-inspect cap (application_connection.call).
COMPLETE_READ_EVENT = "APPLICATION_READ_COMPLETE"
READ_SUPPRESSED_EVENT = "APPLICATION_READ_SUPPRESSED"
COORDINATOR_SUPPRESSED_EVENT = "APPLICATION_RECOVERY_COORDINATOR_SUPPRESSED"
HISTORY_INFERRED_EVENT = "APPLICATION_HISTORY_ABSENCE_INFERRED"
# Owner decision D1, recorded verbatim. It changes AGENTS.md's fresh-history-absence rule for
# applications only: absence may be concluded when the local tracker AND a Gmail search that
# actually ran both found nothing. A search that failed or never ran concludes nothing.
HISTORY_ABSENCE_BASIS = "NO_EVIDENCE_IN_TRACKER_OR_GMAIL"
HISTORY_ABSENCE_AUTHORIZATION = {
    "text": ("The local tracker + Gmail search. If not found, AI must infer that I haven't applied before. "
             "No need for the strictness."),
    "date": "2026-09-19", "decision": "D1"}
GMAIL_SEARCH_MAX_AGE_SECONDS = 24 * 3600
# The 2026-09-19 90-day employer window (PRIOR_WINDOW_AUTHORIZATION) is deleted (owner,
# 2026-10-06: "completely remove it"); history at an employer is recorded, never blocking.


# `history_gates_submission` lived here briefly on 2026-09-22. It narrowed the submit gates'
# one-hour history freshness window so that only a conclusive history gated. The owner then
# removed that window outright from both gates, so nothing calls it and it is gone rather than
# left behind for someone to reach for.
EMAIL_SEMANTICS = frozenset({"email", "contact_email", "email_address", "candidate_email", "primary_email"})
# What each read-only recovery method can possibly change. `apply_resolution` accepts
# identity, destination and prior-history comparisons and nothing else, so no investigation
# can supply a form question, an eligibility fact, an employee count, a signed-in candidate
# email or history absence, whatever its instructions say. Within that ceiling: every method
# reads the current posting (so can pin identity) with the saved history records in its
# context (so can support a comparison); only the posting and link reads can move the
# destination.
METHOD_SUPPLIES = {
    "official_posting": frozenset({"identity", "destination", "prior_history_comparison"}),
    "observed_links": frozenset({"identity", "destination", "prior_history_comparison"}),
    "authenticated_history": frozenset({"identity", "prior_history_comparison"}),
}
BLOCKER_FIELDS = {
    "REQUISITION_UNKNOWN": {"identity"}, "REQUISITION_MISMATCH": {"identity"},
    "IDENTITY_MISMATCH": {"identity"}, "APPLICATION_OFFICIAL_IDENTITY_REQUIRED": {"identity"},
    "UNSUPPORTED_OFFICIAL_DESTINATION": {"destination"},
    "APPLICATION_DESTINATION_OR_SCOPE_UNKNOWN": {"destination"},
    "APPLICATION_OFFICIAL_DESTINATION_REQUIRED": {"destination"},
    "PRIOR_PROCESS_REQUISITION_UNRESOLVED": {"prior_history_comparison"},
    "PRIOR_APPLICATION_IDENTITY_UNRESOLVED": {"prior_history_comparison"},
}


def digest_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                     default=str).encode()).hexdigest()


def question_label(question):
    return " ".join(unicodedata.normalize("NFKC", str(question.get("label") or "")).split())


# Owner, 2026-09-24: "system should be able to apply by email". Some postings have no form at
# all - Genco's Infrastructure Engineer role applies by mailto:careers@genco.example, and was
# abandoned for having no form to fill. An email application is a first-class destination: the
# posting is still read and verified the same way, the recipient must be quoted from that live
# read, and everything downstream that keys on "this application" gets a stable identity from
# the address and subject exactly as a form gets one from its questions.
EMAIL_METHOD = "EMAIL"
EMAIL_APPLICATION_READY = "EMAIL_APPLICATION_READY"
EMAIL_ADDRESS = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+")


def email_application(review):
    """The validated email target of a review that applies by email, or None."""
    if not isinstance(review, dict) or review.get("application_method") != EMAIL_METHOD:
        return None
    target = review.get("email_application")
    if not isinstance(target, dict):
        return None
    address, subject = target.get("address"), target.get("subject")
    if not isinstance(address, str) or not EMAIL_ADDRESS.fullmatch(address.strip()):
        return None
    if subject is not None and (not isinstance(subject, str) or len(subject) > 300):
        return None
    return {"address": address.strip(), "subject": subject.strip() if isinstance(subject, str) else None}


# An email application has no form, but it still has things to write: the message, and a
# subject when the posting names none. They are posed as required questions so the SAME
# answering AI writes them under the owner's rules (nothing invented, every claim traceable to
# the resume), stores them as recorded answers, and every existing "these are exactly the
# recorded answers" check covers the email just as it covers a form. Their labels are ours, not
# page text, which is why they are host-derived here rather than read.
EMAIL_BODY_SEMANTIC = "application_email_body"
EMAIL_SUBJECT_SEMANTIC = "application_email_subject"


def email_questions(target):
    questions = [{"id": "email-application-body", "required": True,
                  "semantic": EMAIL_BODY_SEMANTIC, "answer_type": "text",
                  "label": "Email message applying for this role, sent from the candidate's own mailbox "
                           "with the designated resume attached"}]
    if target["subject"] is None:
        questions.append({"id": "email-application-subject",
                          "required": True, "semantic": EMAIL_SUBJECT_SEMANTIC, "answer_type": "text",
                          "label": "Subject line for this email application (the posting names none)"})
    return questions


def email_applications_enabled(config):
    """Owner's switch for the email route: until it is on, an email job waits, it never sends."""
    return (config or {}).get("email_applications_enabled") is True


def email_fingerprint(target):
    return "email:" + hashlib.sha256(json.dumps([target["address"].casefold(), target["subject"]],
                                                 separators=(",", ":")).encode()).hexdigest()


def form_fingerprint(questions):
    """The form's identity, computed here from the enumerated required questions.

    A Greenhouse or Ashby form shows no revision string, so asking the browser to quote one
    guaranteed the review could never verify. The fingerprint is a hash of each required
    question's own id (or null) and its verbatim label, whitespace-folded. Items are sorted so
    a conditional question revealed mid-form hashes identically wherever it is inserted; the
    submit session sends what it actually sees to the arm guard, which recomputes this and
    refuses on any difference.
    """
    items = []
    for question in questions or ():
        if not isinstance(question, dict) or question.get("required") is not True:
            continue
        identifier = question.get("id")
        identifier = str(identifier).strip() if isinstance(identifier, (str, int)) and not isinstance(identifier, bool) and str(identifier).strip() else None
        items.append([identifier or "", question_label(question)])
    return FORM_FINGERPRINT_PREFIX + digest_json(sorted(items))


def form_enumerated(review):
    """Complete enough to proceed: every required question, or every visible one with text.

    A question a form reveals only after an earlier answer cannot be read without filling the
    form, which a read-only inspection may not do. Those are enumerated inside the guarded
    submit session, which stops before arming if one appears that has no supplied answer.
    """
    questions = review.get("questions") if isinstance(review, dict) else None
    if not isinstance(questions, list):
        return False
    if review.get("all_required_questions_enumerated") is True:
        return True
    required = [question for question in questions if isinstance(question, dict) and question.get("required") is True]
    return (review.get("visible_required_questions_enumerated") is True and bool(required)
            and all(question_label(question) for question in required))


def email_question(question):
    if not isinstance(question, dict) or question.get("required") is not True:
        return False
    return (question.get("semantic") in EMAIL_SEMANTICS
            or bool(re.search(r"\be-?mail\b", question_label(question), re.IGNORECASE)))


def bind_code_derived_form(review, job):
    """Replace the two fields a public read can never quote with host-derived ones.

    Idempotent and database-free, so `application_browser.normalize` and `process` agree.
    `form_revision` becomes the fingerprint above (the model's own quote, if any, is kept as
    `observed_form_revision`). A null `candidate_email` on a form that asks for an email is
    bound to the owner's recorded address with an explicit DEFERRED basis: the guarded
    submit session types that exact address and must report it back to the arm guard, and
    `desktop_adapter.submission_gate` refuses unless the email answer is that address.
    """
    if not isinstance(review, dict):
        return review
    target = email_application(review)
    if target is not None:
        # No form exists, so its identity is the email target itself, and the sender is the
        # owner's own mailbox - bound the same deferred way as a form that asks for an email.
        review["form_revision"] = email_fingerprint(target)
        review["form_revision_basis"] = CODE_FORM_REVISION_BASIS
        review["questions"] = email_questions(target)
        review["all_required_questions_enumerated"] = True
        if not review.get("candidate_email") and job.get("candidate_email"):
            review["candidate_email"] = job["candidate_email"]
            review["candidate_email_binding"] = CANDIDATE_BINDING_DEFERRED
        return review
    questions = review.get("questions")
    if isinstance(questions, list) and any(isinstance(item, dict) and item.get("required") is True for item in questions):
        if review.get("form_revision") and review.get("form_revision_basis") != CODE_FORM_REVISION_BASIS:
            review["observed_form_revision"] = review["form_revision"]
        review["form_revision"] = form_fingerprint(questions)
        review["form_revision_basis"] = CODE_FORM_REVISION_BASIS
        if review.get("all_required_questions_enumerated") is not True and form_enumerated(review):
            review["conditional_questions_deferred"] = True
        if (not review.get("candidate_email") and job.get("candidate_email")
                and any(email_question(item) for item in questions)):
            review["candidate_email"] = job["candidate_email"]
            review["candidate_email_binding"] = CANDIDATE_BINDING_DEFERRED
    return review


def research_facts(database, job, now):
    """Company-level funding stage and headcount recorded by research for this employer.

    Only dated, sourced, verified research evidence stored in this employer's own role
    payloads (`application_jobs`, `opportunities`) is used - never a value from another
    company's record, never an undated value, never one older than
    RESEARCH_FACT_MAX_AGE_SECONDS. The most recent effective observation wins.
    """
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    payloads = [row[0] for row in database.execute("SELECT payload FROM application_jobs WHERE employer=?", (job["employer"],))]
    if "opportunities" in tables:
        payloads += [row[0] for row in database.execute("SELECT payload FROM opportunities")]
    found = {}
    for raw in payloads:
        try:
            role = json.loads(raw)
            if normalized(str(role.get("company") or "")) != job["employer"]:
                continue
            entries = [entry for entry in role.get("evidence") or () if isinstance(entry, dict)]
            for holder in (role, role.get("review") or {}):
                if isinstance(holder.get("employee_count_evidence"), dict):
                    entries.append(dict(holder["employee_count_evidence"], field="employee_count"))
        except (TypeError, ValueError, AttributeError):
            continue
        for entry in entries:
            field = entry.get("field")
            if field not in {"funding_stage", "employee_count"} or entry.get("verified") is not True or not entry.get("value"):
                continue
            # A headcount marked historical (e.g. "over 140 employees" in 2021) is not a current
            # size. A funding announcement is historical by nature and defines the stage until
            # superseded, so it is kept, with its effective date carried alongside.
            if field == "employee_count" and entry.get("kind") == "historical":
                continue
            source = urllib.parse.urlsplit(str(entry.get("source") or ""))
            if source.scheme != "https" or not source.hostname or "aiapply" in str(entry.get("source")).casefold():
                continue
            try:
                observed = dt.datetime.fromisoformat(str(entry["observed_at"]).replace("Z", "+00:00"))
                if observed.tzinfo is None:
                    continue
                if now is not None:
                    age = (dt.datetime.fromisoformat(now) - observed).total_seconds()
                    if not -60 <= age <= RESEARCH_FACT_MAX_AGE_SECONDS:
                        continue
            except (KeyError, TypeError, ValueError):
                continue
            rank = (str(entry.get("effective_at") or ""), observed.isoformat())
            if field not in found or rank > found[field][0]:
                found[field] = (rank, {"value": entry["value"], "source": entry["source"], "observed_at": observed.isoformat(),
                                       "effective_at": entry.get("effective_at"), "kind": entry.get("kind"),
                                       "excerpt": entry.get("excerpt"), "verified": True, "basis": RESEARCH_BASIS})
    return {field: value for field, (rank, value) in sorted(found.items())}


def company_facts(database, employer):
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "application_company_facts" not in tables:
        return None
    row = database.execute("SELECT facts FROM application_company_facts WHERE employer=?", (employer,)).fetchone()
    return json.loads(row[0]) if row else None


def refresh_company_facts(database, job, now):
    """One shared record per employer: every job at that employer reads the same facts.

    Rebuilt from research's stored evidence each time (cheap, local, no model), written only
    when it changed. Returns the facts still inside the shelf life at `now`.
    """
    stored = research_facts(database, job, None)
    encoded_facts = json.dumps(stored, sort_keys=True)
    row = database.execute("SELECT facts_sha256 FROM application_company_facts WHERE employer=?", (job["employer"],)).fetchone()
    if not row or row[0] != hashlib.sha256(encoded_facts.encode()).hexdigest():
        database.execute("""INSERT INTO application_company_facts VALUES (?,?,?,?) ON CONFLICT(employer) DO UPDATE SET
            facts=excluded.facts,facts_sha256=excluded.facts_sha256,updated_at=excluded.updated_at""",
            (job["employer"], encoded_facts, hashlib.sha256(encoded_facts.encode()).hexdigest(), now))
    current = {}
    for field, value in stored.items():
        age = (dt.datetime.fromisoformat(now) - dt.datetime.fromisoformat(value["observed_at"])).total_seconds()
        if -60 <= age <= RESEARCH_FACT_MAX_AGE_SECONDS:
            current[field] = value
    return current


# Who can supply each missing field. Python owns this mapping so a structurally unfixable
# gap is routed to its owner instead of being re-read. "browser_read" fields are the only
# ones another inspect can change.
FIELD_OWNERS = {
    "identity": "recovery:official_posting|observed_links", "destination": "recovery:official_posting|observed_links",
    "prior_history_comparison": "recovery:authenticated_history",
    "facts/funding_stage": "research:company_facts", "employee_count": "research:company_facts",
    "history": "gmail_history_search:external_history import (no runtime Gmail search exists)",
    "candidate_email": "user:decision_transport (account/email binding)",
}


def field_owner(field):
    if field.startswith("answers/"):
        return "user:decision_transport"
    return FIELD_OWNERS.get(field, "browser_read")


def requisition_record(database, job_key):
    row = database.execute("SELECT * FROM application_requisition_records WHERE job_key=?", (job_key,)).fetchone()
    return dict(row) if row else None


def identity_fingerprint(job):
    try:
        url = json.loads(job["payload"]).get("url")
    except (TypeError, ValueError):
        url = None
    return digest_json([job["job_key"], job["employer"], job["requisition"], job["payload"], url])


def read_inputs(database, job):
    """Everything outside this read that could change what the read finds or concludes.

    Deliberately NOT the policy file: an edit to profile.json that does not add a
    job-scoped owner decision is not new evidence about this posting, and letting it count
    is exactly how the old inspect cap was reset by unrelated edits. The submit guard still
    binds the policy hash at arm time. Computed answers (the start-date rule, rewritten daily)
    and AI-minted answers are our own derivations, not external input.
    """
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def rows(table, query, parameters):
        return [list(row) for row in database.execute(query, parameters)] if table in tables else None
    key, employer = job["job_key"], job["employer"]
    record = requisition_record(database, key) if "application_requisition_records" in tables else None
    answers = rows("application_answers", "SELECT field,semantic,value,source FROM application_answers WHERE job_key=? ORDER BY field", (key,)) or []
    return {
        "contract_revision": READ_CONTRACT_REVISION,
        "identity": identity_fingerprint(job),
        "owner_answers": [row for row in answers if not str(row[3]).startswith(
            (COMPUTED_ANSWER_SOURCE_PREFIX, "ai-answer:", "owner-declaration:"))],
        "history": rows("application_history", "SELECT requisition,kind,evidence FROM application_history WHERE employer=? ORDER BY requisition,kind", (employer,)),
        "external_history": rows("application_external_history", "SELECT evidence_id,content_hash FROM application_external_history WHERE employer=? ORDER BY evidence_id", (employer,)),
        "resolutions": rows("application_history_resolutions", "SELECT evidence_id,relationship,observed_at FROM application_history_resolutions WHERE job_key=? ORDER BY evidence_id", (key,)),
        "history_searches": rows("application_history_searches", "SELECT search_id,content_hash FROM application_history_searches WHERE employer=? ORDER BY search_id", (employer,)),
        # Kept only so every job's fingerprint stays the same: removing this key would change
        # all of them and re-open every remembered read. The table is no longer written
        # (referrals are per person since 2026-09-25): [] on the live database, None where the table never existed.
        "referrals": rows("application_referrals", "SELECT requisition,state,deadline FROM application_referrals WHERE employer=? ORDER BY requisition", (employer,)),
        # A fixed placeholder (the referrals precedent above): the browser-permission records it
        # used to read are no longer consulted (owner, 2026-09-30: no remnants of permission
        # denials). [] is what the query returned for every job without such a record, so 47 of
        # the 52 open jobs keep their fingerprint; the 5 that had one read afresh at most once.
        "access_restorations": [],
        "conditional_questions": json.loads(record["conditional_questions"]) if record else [],
        "research_facts": research_facts(database, job, None),
    }


def input_fingerprint(database, job):
    return digest_json(read_inputs(database, job))


def complete_review(database, job, review, now):
    """Host-side completion of a read (or a partial one): form identity, research facts, history.

    Never invents a fact: every value added is either computed from what the read recorded,
    or copied with its source from a dated local record, and each carries its basis.
    """
    if not isinstance(review, dict):
        return review
    record = requisition_record(database, job["job_key"])
    if record and record["identity_sha256"] == identity_fingerprint(job) and isinstance(review.get("questions"), list):
        # Keyed by question_field, not by (id, label) (owner, 2026-09-21). The id is reported
        # by the read and drifts between reads - that is why question_field stopped using it -
        # and question_label folds whitespace but neither case nor a required-marker asterisk.
        # A drifted id or a reworded label made a stored conditional question look absent, so
        # it was appended to every fresh review as a SECOND required question that no control
        # on the live form answers; resolve_answers then reports it missing and the job holds
        # on a question that is not there.
        present = {question_field(item): item for item in review["questions"] if isinstance(item, dict)}
        for question in json.loads(record["conditional_questions"]):
            field = question_field(question)
            if field not in present:
                review["questions"].append(dict(question, conditional=True, source="guarded-submit-session"))
            elif question.get("choices") and not present[field].get("choices"):
                # The menu the submit session saw, for a question the read listed without one.
                present[field]["choices"] = question["choices"]
                if question.get("answer_type"):
                    present[field]["answer_type"] = question["answer_type"]
    bind_code_derived_form(review, job)
    facts = review.setdefault("facts", {}) if isinstance(review.get("facts", {}), dict) else {}
    research = refresh_company_facts(database, job, now)
    if facts.get("funding_stage") is None and "funding_stage" in research:
        facts["funding_stage"] = research["funding_stage"]["value"]
        review.setdefault("research_sourced_facts", {})["funding_stage"] = research["funding_stage"]
    count = review.get("employee_count_evidence")
    if (not isinstance(count, dict) or not count.get("value")) and "employee_count" in research:
        review["employee_count_evidence"] = dict(research["employee_count"])
        review.setdefault("research_sourced_facts", {})["employee_count"] = research["employee_count"]
    # The "already applied?" inference from mailbox records is deleted with item 16 (owner,
    # 2026-09-27): duplicates are decided only by local_gate over the local database.
    return review


def remember_read(database, job, review, blocker, now, extra_missing=(), complete=False, method="inspect"):
    """Persist the per-requisition record and, when inconclusive, one ledger entry.

    Caller holds the transaction. The ledger entry is what `read_suppression` and
    `coordinator_suppression` consult: (method, missing set, input fingerprint).
    """
    review = review if isinstance(review, dict) else {}
    # Only what the four-step workflow still needs can be "missing" (owner, 2026-09-27): the
    # answers the AI has yet to write. `missing_fields` listed identity, official destination,
    # the full question set, every fact, scope verdicts, the candidate email and on-page history
    # - all checks that were deleted - and a non-answer entry suppresses the next read for up to
    # seven days, so a job waiting only on its answers would have gone unread for a week.
    missing = sorted(set(extra_missing)) if not complete else []
    previous = requisition_record(database, job["job_key"])
    conditional = previous["conditional_questions"] if previous and previous["identity_sha256"] == identity_fingerprint(job) else "[]"
    inputs = input_fingerprint(database, job)
    questions = review.get("questions") if isinstance(review.get("questions"), list) else []
    database.execute("""INSERT INTO application_requisition_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(job_key) DO UPDATE SET employer=excluded.employer,requisition=excluded.requisition,
        identity_sha256=excluded.identity_sha256,contract_revision=excluded.contract_revision,
        questions=excluded.questions,form_fingerprint=excluded.form_fingerprint,
        conditional_questions=excluded.conditional_questions,review=excluded.review,complete=excluded.complete,
        blocker=excluded.blocker,missing_fields=excluded.missing_fields,input_sha256=excluded.input_sha256,
        observed_at=excluded.observed_at,updated_at=excluded.updated_at""",
        (job["job_key"], job["employer"], job["requisition"], identity_fingerprint(job), READ_CONTRACT_REVISION,
         json.dumps(questions), review.get("form_revision") if review.get("form_revision_basis") == CODE_FORM_REVISION_BASIS else None,
         conditional, json.dumps(review, default=str), int(bool(complete)), blocker, json.dumps(missing), inputs, now, now))
    if not complete:
        event(database, job["job_key"], INCONCLUSIVE_EVENT, {
            "method": method, "blocker": blocker, "missing_fields": missing, "input_sha256": inputs,
            "identity_sha256": identity_fingerprint(job), "contract_revision": READ_CONTRACT_REVISION,
            "form_fingerprint": review.get("form_revision") if review.get("form_revision_basis") == CODE_FORM_REVISION_BASIS else None}, now)
    return missing


def record_conditional_questions(database, job, questions, now):
    """Questions the guarded submit session saw appear; they join the record's question set."""
    record = requisition_record(database, job["job_key"])
    if not record or not isinstance(questions, list):
        return False
    known = json.loads(record["conditional_questions"])
    # Same key as complete_review above, for the same reason: an id that drifted between
    # sessions re-recorded a question already known, growing the stored set on every pass and
    # flipping the record's fingerprint, which in turn invalidated read suppression.
    everything = [item for item in json.loads(record["questions"]) + known if isinstance(item, dict)]
    stored = {question_field(item) for item in everything}
    with_choices = {question_field(item) for item in everything if item.get("choices")}
    conditional_index = {question_field(item): position for position, item in enumerate(known) if isinstance(item, dict)}
    added, chosen = [], []
    for question in questions:
        if not (isinstance(question, dict) and question_label(question) and question.get("required") is True):
            continue
        entry = {key: question.get(key) for key in ("id", "label", "semantic", "required", "answer_type", "choices", "max_characters") if key in question}
        field = question_field(question)
        if field not in stored:
            added.append(entry)
            stored.add(field)
        elif question.get("choices") and field not in with_choices:
            # A question already on file whose menu the first read did not record (Woodgrove
            # 2026-10-06: "When could you start a new role?" answered "4 weeks from
            # application date", a menu of Right away / In about 2 weeks / In over a month /
            # Open; the answer was reused on every retry until the tries ran out). Its options
            # are recorded and its stored answer dropped, so the answering AI chooses from them.
            if field in conditional_index:
                # Recorded earlier by a submit session without its menu: that entry gains it.
                known[conditional_index[field]] = dict(known[conditional_index[field]], choices=question["choices"],
                                                       choices_observed=True,
                                                       **({"answer_type": question["answer_type"]} if question.get("answer_type") else {}))
            # Only an answer the AI wrote or the start rule computed is dropped; an owner's own
            # answer (authenticated decision or declaration) is never deleted (review round 9).
            dropped = database.execute("""DELETE FROM application_answers WHERE job_key=? AND field=?
                AND (source LIKE 'ai-answer:%' OR source LIKE ?)""",
                (job["job_key"], field, COMPUTED_ANSWER_SOURCE_PREFIX + "%")).rowcount
            chosen.append(dict(entry, choices_observed=True, answer_dropped=bool(dropped)))
            with_choices.add(field)
    if not added and not chosen:
        return False
    database.execute("UPDATE application_requisition_records SET conditional_questions=?,updated_at=? WHERE job_key=?",
                     (json.dumps(known + added + [item for item in chosen if question_field(item) not in conditional_index]),
                      now, job["job_key"]))
    if added:
        event(database, job["job_key"], "CONDITIONAL_QUESTIONS_RECORDED", {"questions": added, "source": "guarded-submit-session"}, now)
    if chosen:
        event(database, job["job_key"], "QUESTION_CHOICES_RECORDED",
              {"questions": chosen, "source": "guarded-submit-session",
               "answers_dropped": [question_field(item) for item in chosen if item.get("answer_dropped")]}, now)
    return True


def tried_methods(database, job, missing, inputs, now):
    """Methods already spent on exactly this (missing set, input state) within the record window."""
    identity = identity_fingerprint(job)
    methods = set()
    for at, detail in database.execute("SELECT at,detail FROM application_events WHERE job_key=? AND event=?",
                                       (job["job_key"], INCONCLUSIVE_EVENT)):
        try:
            detail = json.loads(detail)
            recent = (dt.datetime.fromisoformat(now) - dt.datetime.fromisoformat(at)).total_seconds() <= RECORD_MAX_AGE_SECONDS
        except (TypeError, ValueError):
            continue
        if (recent and detail.get("input_sha256") == inputs and detail.get("identity_sha256") == identity
                and sorted(detail.get("missing_fields") or []) == sorted(missing)):
            methods.add(detail.get("method"))
    return methods


REUSED_READ_MARKER = "reused_from_record"


def reusable_read(database, job, now):
    """The stored review of this exact posting, when re-reading it would learn nothing new.

    Owner, 2026-09-22: "A successful read should not have to reread again for 7 days ...
    existing information should be reused."

    Returns the recorded review when ALL hold: the last read of this posting was COMPLETE, it
    is of this exact identity (`identity_sha256`), it was taken under the current read
    contract, it is younger than `RECORD_MAX_AGE_SECONDS`, and no input it depends on has
    changed since (`input_sha256` - owner answers, tracker history, resolutions, referrals,
    conditional questions, research facts). Otherwise None, and the
    caller takes a live read.

    Why this is safe to act on rather than merely cheap: the facts it carries are the STABLE
    ones - employer, requisition, official_url, role, the stated eligibility requirements, the
    published band. The two things that genuinely go stale are whether the posting is still
    open and whether the form changed, and neither is trusted from here in any case: the
    guarded submit session re-reads the live form on its own
    (`application_browser.OPERATION_SECTIONS["submit"] == ("inspect", "submit")`) and is told
    to STOP if the required questions or the account changed, the page is for another employer or role, or it is another page than the job's own URL
    that shows a real id (placeholder job, IDENTITY_MISMATCH) or does not show the job's real id
    (REQUISITION_MISMATCH), and
    `desktop_adapter.arm` re-derives the local gate, eligibility and the answers
    immediately before arming. Reusing the inspect does not remove a liveness check; it
    removes a SECOND one that duplicated the submit session's own.
    """
    record = requisition_record(database, job["job_key"])
    if not record or not record["complete"] or record["identity_sha256"] != identity_fingerprint(job):
        return None
    if record["contract_revision"] != READ_CONTRACT_REVISION:
        return None
    try:
        age = (dt.datetime.fromisoformat(now) - dt.datetime.fromisoformat(record["observed_at"])).total_seconds()
    except (TypeError, ValueError):
        return None
    if not -60 <= age <= RECORD_MAX_AGE_SECONDS:
        return None
    if record["input_sha256"] != input_fingerprint(database, job):
        return None
    try:
        review = json.loads(record["review"])
    except (TypeError, ValueError):
        return None
    if not isinstance(review, dict) or review.get("verified") is not True:
        return None
    review[REUSED_READ_MARKER] = record["observed_at"]
    return review


def read_suppression(database, job, now):
    """Why a fresh inspect of this job would repeat a read already made, or None.

    A read is refused only when ALL hold: the last read of this exact posting (same job key,
    payload, URL and requisition) was inconclusive, it is younger than seven days, no input
    has changed since (owner answers, tracker/Gmail history, resolutions, referrals,
    recorded conditional questions, research facts, the inspect contract), and
    an inspect was already spent on that exact state. Anything else permits the read.
    """
    # Owner, 2026-09-22: "A successful read should not have to reread again for 7 days. A
    # failed read should." This function did the exact opposite. It fired ONLY on an
    # INCOMPLETE record (`record["complete"]` returned None), so a read that FAILED locked the
    # job out for seven days while a read that SUCCEEDED suppressed nothing and was redone on
    # the very next cycle. Umbrella sat on this on 2026-09-22: a suppressed read pushed
    # next_at an hour forward and emitted no event at all.
    #
    # Suppression of failed reads is now off. Reuse of SUCCESSFUL reads is handled by
    # `reusable_read` below, which is the half that actually saves the browser session. The
    # function, `hold_suppressed` and the ledger shape are kept: they are still the right
    # mechanism if a specific repeated-failure case ever needs bounding again, and deleting
    # them would silently drop the APPLICATION_READ_SUPPRESSED history.
    return None
    record = requisition_record(database, job["job_key"])
    if not record or record["complete"] or record["identity_sha256"] != identity_fingerprint(job):
        return None
    try:
        if (dt.datetime.fromisoformat(now) - dt.datetime.fromisoformat(record["observed_at"])).total_seconds() > RECORD_MAX_AGE_SECONDS:
            return None
    except (TypeError, ValueError):
        return None
    missing = json.loads(record["missing_fields"])
    if missing and all(str(field).startswith("answers/") for field in missing):
        # Only form answers are missing: the AI answers them (BR/BS), not the owner, so the job
        # proceeds to answering instead of waiting for owner input that is never requested.
        return None
    inputs = input_fingerprint(database, job)
    if not missing or record["input_sha256"] != inputs or "inspect" not in tried_methods(database, job, missing, inputs, now):
        return None
    return {"blocker": record["blocker"], "missing_fields": missing, "input_sha256": inputs,
            "field_owners": {field: field_owner(field) for field in missing},
            "record_observed_at": record["observed_at"],
            "rereads_at": (dt.datetime.fromisoformat(record["observed_at"]) + dt.timedelta(seconds=RECORD_MAX_AGE_SECONDS)).isoformat()}


def hold_suppressed(database, job, suppressed, now):
    action = ("No new input since the last inconclusive read of this exact posting; still missing: "
              + ", ".join(field + " (" + suppressed["field_owners"][field] + ")" for field in suppressed["missing_fields"])
              + ". A fresh read happens when an owner answer, history record, "
              "Gmail history search, resolution or posting change arrives, or after "
              + suppressed["rereads_at"] + ". No read or compute is spent meanwhile")
    blocker = suppressed["blocker"] or "APPLICATION_READ_AWAITING_NEW_INPUT"
    next_at = min(suppressed["rereads_at"], (dt.datetime.fromisoformat(now) + dt.timedelta(hours=1)).isoformat())
    hold(database, job, blocker, action, now, "BLOCKED", next_at)
    latest = database.execute("SELECT detail FROM application_events WHERE job_key=? AND event=? ORDER BY id DESC LIMIT 1",
                              (job["job_key"], READ_SUPPRESSED_EVENT)).fetchone()
    if not latest or json.loads(latest[0]).get("input_sha256") != suppressed["input_sha256"]:
        event(database, job["job_key"], READ_SUPPRESSED_EVENT, dict(suppressed, operation="inspect"), now)


def coordinator_suppression(database, job, blocker, now):
    """Why a recovery coordinator call cannot yield a useful read, or None.

    The missing set is the blocker's own fields plus the latest inconclusive record's. A call
    is refused when no supported method can supply any missing field, or when every method
    that could has already been spent on this unchanged (missing set, input) state. When the
    missing set is unknown (a transient failure, say) the coordinator is not second-guessed.
    """
    missing = set(BLOCKER_FIELDS.get(blocker, ()))
    record = requisition_record(database, job["job_key"])
    if record and not record["complete"] and record["identity_sha256"] == identity_fingerprint(job):
        missing.update(json.loads(record["missing_fields"]))
    if not missing:
        return None
    inputs = input_fingerprint(database, job)
    capable = {method for method, supplies in METHOD_SUPPLIES.items() if supplies & missing}
    tried = tried_methods(database, job, sorted(missing), inputs, now)
    untried = capable - tried
    if untried:
        return None
    return {"blocker": blocker, "missing_fields": sorted(missing), "capable_methods": sorted(capable),
            "methods_tried_in_this_state": sorted(method for method in tried if method), "input_sha256": inputs,
            "reason": "NO_SUPPORTED_METHOD_CAN_SUPPLY_MISSING_FIELDS" if not capable else "EVERY_CAPABLE_METHOD_ALREADY_TRIED_WITHOUT_NEW_INPUT"}


def untried_capable_methods(database, job, blocker, now):
    missing = set(BLOCKER_FIELDS.get(blocker, ()))
    record = requisition_record(database, job["job_key"])
    if record and not record["complete"] and record["identity_sha256"] == identity_fingerprint(job):
        missing.update(json.loads(record["missing_fields"]))
    if not missing:
        return None, sorted(missing)
    inputs = input_fingerprint(database, job)
    capable = {method for method, supplies in METHOD_SUPPLIES.items() if supplies & missing}
    return capable - tried_methods(database, job, sorted(missing), inputs, now), sorted(missing)


PREDRAFT_EVENT = "APPLICATION_ANSWERS_PREDRAFTED"


def predraft_answers(database, job, policy, root, config, adapter, now):
    """Draft answers once per form fingerprint from the persisted question set.

    Uses the unchanged answering step (`answering_agent.materialize`, off unless configured,
    non-raising; the AI answers every question, 2026-09-28) so the submit session
    only has to confirm the fingerprint. Bounded to one attempt per fingerprint.
    """
    record = requisition_record(database, job["job_key"])
    if not record or record["identity_sha256"] != identity_fingerprint(job) or not record["form_fingerprint"]:
        return None
    review = json.loads(record["review"])
    # Drafting is not submitting: any labelled required question already captured is worth an
    # answer now. Only a later verified review with the same fingerprint can use it.
    questions = [question for question in review.get("questions") or () if isinstance(question, dict)
                 and question.get("required") is True and question_label(question)]
    if not questions:
        return None
    done = database.execute("SELECT 1 FROM application_events WHERE job_key=? AND event=? AND json_extract(detail,'$.form_fingerprint')=?",
                            (job["job_key"], PREDRAFT_EVENT, record["form_fingerprint"])).fetchone()
    if done:
        return None
    policy = with_current_kept_answers(policy)   # answers kept on Approvals since the run began
    scoped_policy = dict(policy, answers=dict(policy["answers"]))
    for saved in database.execute("SELECT * FROM application_answers WHERE job_key=? AND field<>?",
                                  (job["job_key"], decision_transport.FORM_NOT_READABLE_FIELD)):
        scoped_policy["answers"][saved["field"]] = {"semantic": saved["semantic"], "value": json.loads(saved["value"]), "source": saved["source"]}
    with database:
        materialize_computed_answers(database, job, questions, scoped_policy, now)
    try:
        import answering_agent
        outcome = answering_agent.materialize(database, job, questions, scoped_policy, now,
                                              adapter=adapter, root=root, config=config, review=review)
    except Exception:
        outcome = {}
    answers, missing = resolve_answers(questions, scoped_policy)
    if ((outcome or {}).get("audit") or {}).get("provider_wait"):
        # The provider refused the turn for usage: nothing was drafted, so this form is not
        # marked as pre-drafted and the drafting runs again after the reset.
        return {"answered": sorted(answers), "missing": missing, "provider_wait": outcome["audit"]["provider_wait"]}
    with database:
        event(database, job["job_key"], PREDRAFT_EVENT, {"form_fingerprint": record["form_fingerprint"],
              "answered": sorted(answers), "still_missing": missing,
              "answering_outcome": ((outcome or {}).get("audit") or {}).get("outcome")}, now)
    return {"answered": sorted(answers), "missing": missing}


def reread_inspection(database, job, policy, code, schedule=False):
    """An incomplete read is an inspection problem: re-read it before starting recovery.

    Uses only the existing bounded inspection path - `retry` with the existing
    `policy["retry_delays_seconds"]` backoff, bounded by `policy["max_attempts"]`
    through the durable INSPECTION_REREAD_SCHEDULED ledger. The recovery
    lifecycle, its three actual investigation claims and its original deadline
    are untouched, and `job["attempts"]` is deliberately not consumed so a later
    verified identity/destination resolution can still reconcile the job.

    `schedule=True` asks whether one more fresh re-read may be scheduled;
    the default asks only whether a pending read should be preferred over
    starting recovery. Both exclude user holds, FINAL states, outstanding or
    uncertain submissions and an already active recovery lifecycle.
    """
    import application_recovery
    if code not in INCOMPLETE_INSPECTION or job["state"] in FINAL:
        return False
    if schedule and len(application_recovery.records(database, job["job_key"], "INSPECTION_REREAD_SCHEDULED")) >= max(policy["max_attempts"] - 1, 0):
        return False
    return (not application_recovery.user_hold(job["blocker"])
            and not application_recovery.active(database, job["job_key"])
            and not application_recovery.outstanding(database, job))


def unreadable_form_reads(database, job_key):
    """Genuine complete reads that still could not enumerate the form.

    The bound is counted in the durable event ledger, not in `application_jobs.attempts`,
    for two reasons. A read that fails to enumerate a form is not a submission attempt and
    must not consume one. And only a read that actually happened is recorded: every wait -
    `AdapterPending`, shared compute exhaustion, per-cycle browser capacity - returns from
    `process` long before the enumeration check, so a wait can never spend the allowance.
    """
    import application_recovery
    return len(application_recovery.records(database, job_key, FORM_NOT_READABLE_EVENT))


def unreadable_form_evidence(job, review):
    """Exactly what the read did and did not contain. No inferred question text."""
    role = json.loads(job["payload"])
    questions = review.get("questions")
    return {"url": role.get("url"), "role": role.get("role") or role.get("title"),
            "source": review.get("source"), "form_revision": review.get("form_revision"),
            "all_required_questions_enumerated": review.get("all_required_questions_enumerated"),
            "questions_type": type(questions).__name__,
            "questions_read": len(questions) if isinstance(questions, list) else None,
            "questions_invented": False}


def escalate_unreadable_form(database, job, policy, review, now, reads):
    """Turn the spent private hold into one user-visible request through the existing path.

    Not called today: the all-questions-found check that fed it was deleted (owner, 2026-09-27,
    item 17), so `process` never escalates an unreadable form; a legacy pending request is
    superseded once the form is read (FORM_NOW_ENUMERATED). Kept for the record and its tests.

    `hold` records the user-owned blocker and its notification; `decision_transport.
    request_fact` - the same call `ask_owner` makes for a required question only the owner can
    answer (`UNKNOWN_REQUIRED_ANSWERS:<fields>`) - carries the actual message, so the
    notification is superseded rather than sent beside it. `request_fact` returns the existing
    identifier while one is PENDING, so repeated cycles re-assert the same single request
    instead of queuing another.
    """
    evidence = unreadable_form_evidence(job, review)
    hold(database, job, FORM_NOT_READABLE_BLOCKER, FORM_NOT_READABLE_ACTION, now)
    identifier = decision_transport.request_fact(database, job, decision_transport.FORM_NOT_READABLE_QUESTION, policy, now)
    database.execute("UPDATE notification_outbox SET state='SUPERSEDED_BY_FACT_REQUEST' WHERE event_key=? AND state='PENDING'",
                     (job["job_key"] + ":" + FORM_NOT_READABLE_BLOCKER,))
    if job["blocker"] != FORM_NOT_READABLE_BLOCKER:
        event(database, job["job_key"], FORM_NOT_READABLE_ESCALATED,
              dict(evidence, decision_id=identifier, reads=reads, bound=policy["max_attempts"],
                   previous_blocker=job["blocker"]), now)
    return identifier



OWNER_QUESTION_EVENT = "OWNER_QUESTIONS_ASKED"
OWNER_QUESTION_ACTION = ("Answer the application's questions on Approvals (Questions for you); the application "
                         "goes on at its next turn after the last answer")


def owner_questions(outcome, questions, missing):
    """{field: question} of the still-missing REQUIRED questions whose last answering verdict is that
    no truthful answer exists in the owner's records: the judge returned CANNOT_ANSWER (InsForge's
    "GitHub profile", 2026-10-23), or the answering AI said UNKNOWN and the judge accepted that
    (ASI's export-control status). A verdict from a failed judge or repair call, a round limit or a
    value that does not fit its control carries no judge verdict of that kind and stays the AI's
    retry. Python reads only the verdict codes, never the question text."""
    verdicts = {}
    for verdict in ((outcome if isinstance(outcome, dict) else {}).get("audit") or {}).get("verdicts") or ():
        if isinstance(verdict, dict) and verdict.get("question_key"):
            verdicts[verdict["question_key"]] = verdict   # the last verdict per question wins
    by_field = {}
    for question in questions:
        if isinstance(question, dict) and question.get("required"):
            by_field.setdefault(question_field(question), question)
    asked = {}
    for field in missing:
        verdict, question = verdicts.get(field) or {}, by_field.get(field)
        judge = verdict.get("judge") if isinstance(verdict.get("judge"), dict) else {}
        if question is None or verdict.get("outcome") != "UNKNOWN" or verdict.get("judge_failed"):
            continue
        if (judge.get("verdict") == "CANNOT_ANSWER"
                or (judge.get("verdict") == "ACCEPT" and verdict.get("status") in ("UNKNOWN", "WITHHELD_BY_POLICY"))):
            asked[field] = question
    return asked


BOOLEAN_ANSWER_TYPES = frozenset({"boolean", "bool", "yes_no", "yesno"})


def owner_question(field, question):
    """The question as `decision_transport.request_fact` files it: the form's label and its own
    options. A question with options is a choice (the card shows them as radio buttons); a yes/no
    control without options is yes/no; anything else is free text."""
    choices = [choice.strip() for choice in question.get("choices") or () if isinstance(choice, str) and choice.strip()]
    kind = str(question.get("answer_type") or "").strip().casefold()
    answer_type = "choice" if choices else "boolean" if kind in BOOLEAN_ANSWER_TYPES else "text"
    label = question.get("label") if isinstance(question.get("label"), str) and question["label"].strip() else None
    return {"id": field, "semantic": question.get("semantic") or field, "label": label,
            "answer_type": answer_type, "choices": list(dict.fromkeys(choices))}


def ask_owner(database, job, asked, policy, now):
    """One "Questions for you" card per question (`request_fact` returns the open one when it is
    already asked, so a re-run never asks twice). A question of this job whose deadline passed is
    closed EXPIRED first, so it is asked again with a fresh one. Caller holds the transaction.
    The hold's deadline is read back with `open_cards`."""
    milliseconds = int(dt.datetime.fromisoformat(now).timestamp() * 1000)
    for field in asked:
        for (event_key,) in database.execute("SELECT event_key FROM user_decisions WHERE job_key=? AND field=? "
                                             "AND state='PENDING' AND expires_at<=?", (job["job_key"], field, milliseconds)).fetchall():
            database.execute("UPDATE user_decisions SET state='EXPIRED' WHERE event_key=? AND state='PENDING'", (event_key,))
            database.execute("UPDATE notification_outbox SET state='CANCELLED' WHERE event_key=? "
                             "AND state IN ('PENDING','WAITING_TRANSPORT','WAITING_OWNER')", (event_key,))
    for field, question in asked.items():
        identifier = decision_transport.request_fact(database, job, owner_question(field, question), policy, now)
        # A re-read whose options drifted gives the same question a new card: the older card for the
        # same question is superseded, never left beside it.
        _close_cards(database, job["job_key"], "QUESTION_REASKED", now,
                     "field=? AND id<>?", (field, identifier))


def _close_cards(database, job_key, reason, now, where, parameters):
    """Supersede this job's PENDING owner-question cards matching `where` (the form-not-readable
    note excepted) and cancel their notifications. Caller holds the transaction."""
    rows = database.execute("SELECT id,event_key FROM user_decisions WHERE job_key=? AND kind='fact' AND state='PENDING' "
                            "AND field<>? AND " + where, (job_key, decision_transport.FORM_NOT_READABLE_FIELD) + tuple(parameters)).fetchall()
    for identifier, event_key in rows:
        database.execute("UPDATE user_decisions SET state='SUPERSEDED',last_error=? WHERE id=? AND state='PENDING'", (reason, identifier))
        database.execute("UPDATE notification_outbox SET state='CANCELLED',updated_at=? WHERE event_key=? "
                         "AND state IN ('PENDING','WAITING_TRANSPORT','WAITING_OWNER')", (now, event_key))


def close_settled_cards(database, job, missing, now):
    """Q2 (review round 1): a card whose question is no longer missing - answered on a later pass, or
    gone from (or reworded on) the re-read form - is superseded, so Approvals shows only what is still
    needed. Caller holds the transaction."""
    marks = ",".join("?" * len(missing))
    _close_cards(database, job["job_key"], "QUESTION_NO_LONGER_MISSING", now,
                 ("field NOT IN (%s)" % marks) if missing else "1=1", missing)


def open_cards(database, job, missing, now):
    """{field: deadline ms} of this job's unexpired PENDING owner-question cards among `missing`."""
    if not missing:
        return {}
    milliseconds = int(dt.datetime.fromisoformat(now).timestamp() * 1000)
    rows = database.execute("SELECT field,MIN(expires_at) FROM user_decisions WHERE job_key=? AND kind='fact' AND state='PENDING' "
                            "AND expires_at>? AND field IN (%s) GROUP BY field" % ",".join("?" * len(missing)),
                            (job["job_key"], milliseconds) + tuple(missing)).fetchall()
    return {field: expires for field, expires in rows}


def same_posting_duplicate(database, job, adapter, root, config, now, before_submit=False):
    """Same posting, another listing (owner, 2026-09-27): when the company has other jobs on file
    (before a submit: jobs already applied to or being submitted), the model judges whether this
    listing is one of them, and only a SAME naming one of them attaches it as a duplicate. Asked before the first read and, since 2026-10-06, before every submit.

    Before the first read, anything but SAME (DIFFERENT, UNSURE, no compute, a failure) leaves the
    job as it was. Before a submit the judgment is the only guard against applying to the same
    posting twice (the employer-wide holds are deleted, owner 2026-10-06), and the owner chose
    "Hold on failure only": one that cannot be made holds the job (SAME_POSTING_CHECK_PENDING) and
    is asked again an hour later; UNSURE goes on. Returns True when the job was attached or held.
    """
    import posting_judge
    try:
        nothing_to_ask = before_submit and posting_judge.same_job_packet(database, job, before_submit=True) is None
    except Exception:
        # Reading the candidates failed: before a submit, a judgment that could not be made.
        if not before_submit:
            return False
        if database.in_transaction:
            database.rollback()
        return _hold_same_posting_pending(database, job, now)
    if nothing_to_ask:
        return False
    verdict = _judge_same_posting(database, job, adapter, root, config, now, before_submit)
    if verdict is _FAILED:
        return _hold_same_posting_pending(database, job, now)
    if not verdict or verdict["verdict"] != "SAME" or not verdict.get("same_as"):
        return False
    return _attach_same_posting(database, job, adapter, root, config, now, before_submit, verdict)


_FAILED = object()


def _unsettled(database, job_key):
    state = database.execute("SELECT state FROM application_jobs WHERE job_key=?", (job_key,)).fetchone()
    return bool(state) and (state[0] in {"SUBMITTING", "RECONCILE"} or bool(database.execute(
        "SELECT 1 FROM application_attempts WHERE job_key=? AND state='SUBMITTING' LIMIT 1", (job_key,)).fetchone()))


def _was_read(database, job):
    """The job has had a browser request (its first read is behind it)."""
    if not database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='desktop_requests'").fetchone():
        return False
    return bool(database.execute("SELECT 1 FROM desktop_requests WHERE job_key=? LIMIT 1", (job["job_key"],)).fetchone())


def _awaited(database, job):
    """The job named by this job's latest SAME_POSTING_AWAITING_SETTLEMENT event, or None."""
    row = database.execute("""SELECT detail FROM application_events WHERE job_key=? AND event='SAME_POSTING_AWAITING_SETTLEMENT'
        ORDER BY at DESC, id DESC LIMIT 1""", (job["job_key"],)).fetchone()
    try:
        same_as = json.loads(row[0]).get("same_as") if row else None
    except (TypeError, ValueError, AttributeError):
        return None
    return same_as if isinstance(same_as, str) else None


def _judge_same_posting(database, job, adapter, root, config, now, before_submit):
    """The verdict; _FAILED before a submit when none could be made (no compute, no model, a
    failure, an exception booking it); None when there was nothing to ask."""
    import posting_judge
    try:
        verdict = posting_judge.same_job(root, database, config, job, now, executor=getattr(adapter, "same_job_executor", None),
                                         before_submit=before_submit)
    except Exception:
        # Booking its compute or recording its attempt failed (e.g. a locked database). Before the
        # first read that holds nothing (2026-09-27); before a submit it is a failed judgment.
        if database.in_transaction:
            database.rollback()
        verdict = None
    if before_submit and verdict is None and posting_judge.same_job_packet(database, job, True) is not None:
        return _FAILED
    return verdict


def _hold_same_posting_pending(database, job, now, cause="could not be made (no compute, no model or a failure)"):
    with database:
        hold(database, job, SAME_POSTING_CHECK_PENDING,
             "The same-posting check before submitting " + cause + "; ask it again before applying", now, "BLOCKED",
             (dt.datetime.fromisoformat(now) + dt.timedelta(minutes=55)).isoformat())
    return True


def _attach_same_posting(database, job, adapter, root, config, now, before_submit, verdict):
    import posting_judge
    same_as = verdict["same_as"]
    existing = database.execute("SELECT state,blocker FROM application_jobs WHERE job_key=?", (same_as,)).fetchone()
    if existing is None:
        # Validation passes only jobs that were shown, so this one is gone since: before a submit,
        # a judgment that could not be used.
        return _hold_same_posting_pending(database, job, now, "named a job no longer on file") if before_submit else False
    if existing[0] == "DUPLICATE" and existing[1] == posting_judge.SAME_JOB_BLOCKER and not before_submit:
        # It became a copy of another listing since it was shown (perhaps of this very job): this job
        # proceeds, so a posting is never left with every listing a copy (Cognition, 2026-10-23).
        return False
    if existing[0] in {"ABANDONED", "EXHAUSTED"}:
        # A copy of a given-up job proceeds (owner, 2026-09-27).
        return _hold_same_posting_pending(database, job, now, "named a job given up since it was shown") if before_submit else False
    if _unsettled(database, same_as):
        # SAME means DUPLICATE or wait (owner, 2026-10-06): the job it copies may not have been
        # submitted after all, so this one waits and is asked again on its next pass.
        with database:
            waiting = job["blocker"] == SAME_POSTING_AWAITING_SETTLEMENT
            hold(database, job, SAME_POSTING_AWAITING_SETTLEMENT,
                 "Same posting as " + same_as + ", whose submission is not settled yet; ask again once it is",
                 now, "BLOCKED", (dt.datetime.fromisoformat(now) + dt.timedelta(minutes=55)).isoformat())
            if not waiting or _awaited(database, job) != same_as:
                event(database, job["job_key"], "SAME_POSTING_AWAITING_SETTLEMENT",
                      {"same_as": same_as, "same_as_state": existing[0], "before_submit": before_submit,
                       "reason": verdict["reason"]}, now)
        return True
    why = ("; that job carries this application" if existing[0] in {"SUBMITTED", "DUPLICATE"}
           else "; this posting is handled there (state " + existing[0] + ")")
    with database:
        hold(database, job, posting_judge.SAME_JOB_BLOCKER, "Same posting as " + same_as + why, now, "DUPLICATE")
        event(database, job["job_key"], "SAME_POSTING_LINKED",
              {"same_as": same_as, "existing_state": existing[0], "before_submit": before_submit,
               "reason": verdict["reason"], "quotes": verdict["quotes"], "basis": "AI_JUDGED_QUOTED"}, now)
    return True


SAME_POSTING_CHECK_PENDING = "SAME_POSTING_CHECK_PENDING"
SAME_POSTING_AWAITING_SETTLEMENT = "SAME_POSTING_AWAITING_SETTLEMENT"


def process(database, job, adapter, policy, root, now, clock=None, config=None):
    import application_recovery
    config = config or {}
    current = database.execute("SELECT * FROM application_jobs WHERE job_key=?", (job["job_key"],)).fetchone()
    if not current or current["state"] in FINAL:
        return
    job = dict(current)
    policy = with_current_kept_answers(policy)
    job["candidate_email"] = policy["answers"]["email"]["value"]
    # Same posting, another listing (owner, 2026-09-27). Before a new job's first read, when its
    # company already has other jobs on file, the model judges whether this listing is one of
    # them; only a SAME naming one of them attaches it, as a duplicate, and it is never read.
    # Anything else - DIFFERENT, UNSURE, no compute, a failure - leaves the job as it was.
    if job["state"] == "QUEUED" and root is not None and same_posting_duplicate(database, job, adapter, root, config, now):
        return
    # A job waiting on the before-submit judgment asks it again first, before any read: during an
    # outage it waits on the judge alone and spends no browser read (review round 4, 2026-10-06:
    # each hourly retry re-read an unverified page and ran into the three-read cap).
    # A copy held before its first read (waiting on a job it was judged the same as) is asked as
    # before a first read again: the company's jobs on file, and a failure holds nothing (review
    # round 12).
    if (job["blocker"] in {SAME_POSTING_CHECK_PENDING, SAME_POSTING_AWAITING_SETTLEMENT} and root is not None
            and same_posting_duplicate(database, job, adapter, root, config, now, before_submit=_was_read(database, job))):
        return
    # History first: a job the owner's prior-application window or a recorded same-job
    # application/interview already holds is held by that gate before anything below can raise
    # a user-owned hold or send the owner a question (required answers).
    # Uncertain submissions are excluded: their reconciliation always comes first.
    if (job["state"] not in UNCERTAIN and job["blocker"] != "SUBMISSION_OUTCOME_UNKNOWN"
            and not database.execute("SELECT 1 FROM application_attempts WHERE job_key=? AND state='SUBMITTING' LIMIT 1",
                                     (job["job_key"],)).fetchone()):
        gate = local_gate(database, job, now, policy)
        if gate and gate[0] == "DUPLICATE":
            with database:
                hold(database, job, gate[1], gate[2], now, gate[0], gate[3])
            return
    # The browser permission check (item 2) is deleted (owner, 2026-09-27).
    outstanding = database.execute("SELECT 1 FROM application_attempts WHERE job_key=? AND state='SUBMITTING' LIMIT 1", (job["job_key"],)).fetchone()
    # One question, asked once: where does this job go? Every branch below reads this answer
    # instead of re-deriving its own from a different half of the signal.
    route = job_route(job["blocker"], job["state"], bool(outstanding))
    if route == "RECONCILE":
        reconcile(database, job, adapter, policy, now, clock, config)
        return
    # A job whose uploads already failed UPLOAD_FAILURE_LIMIT times is given up before another
    # read or submit (owner, 2026-10-01). The count comes from the record, so it also ends jobs
    # that reached the limit before this rule existed.
    with database:
        if give_up_on_uploads(database, job, now):
            return
        # Likewise a job whose sessions stopped on an access block ACCESS_BLOCK_LIMIT times
        # (owner, 2026-10-11), counted from the record.
        if give_up_on_access_blocks(database, job, now):
            return
    gate = local_gate(database, job, now, policy)
    if gate and gate[0] != "DUPLICATE" and route == "OWNER":
        return
    recovery_code = gate[1] if gate else job["blocker"]
    recovery_action = gate[2] if gate else job["next_action"]
    recovery_checked = False
    # The gate can substitute its own code, so the route is re-asked for the code actually in
    # play rather than assumed from the job's stored blocker - same function, same two inputs.
    #
    # NOTE, 2026-09-22: an ACTIVE lifecycle below still pulls a job into recovery past this
    # answer, even one sitting QUEUED with blocker NULL. Narrowing that to jobs WITH a blocker
    # was tried and reverted: it broke the rule that an expired allowance restores the
    # preserved terminal outcome, letting such a job escape into the ordinary path. The real
    # gap is that recovery can only RETRY (investigate) or ABANDON - it cannot say "this needs
    # a submission attempt, not another read", which is exactly what umbrella needed when the
    # coordinator abandoned it saying "no concrete different supported read is evidenced".
    recovery_route = job_route(recovery_code, gate[0] if gate else job["state"])
    if (not gate or gate[0] == "BLOCKED") and route != "OWNER":
        if ((recovery_route == "RECOVER" or application_recovery.active(database, job["job_key"]))
                and not reread_inspection(database, job, policy, recovery_code)):
            recovery_checked = True
            if application_recovery.recover(database, job, adapter, config, recovery_code, recovery_action, now, clock, policy):
                return
            job = dict(database.execute("SELECT * FROM application_jobs WHERE job_key=?", (job["job_key"],)).fetchone())
            job["candidate_email"] = policy["answers"]["email"]["value"]
            gate = local_gate(database, job, now, policy)
    if gate:
        with database:
            hold(database, job, gate[1], gate[2], now, gate[0], gate[3])
        return
    if job["attempts"] >= policy["max_attempts"]:
        with database:
            hold(database, job, "RETRIES_EXHAUSTED", "Resolve the failure before explicitly requeuing", now, "EXHAUSTED")
        return
    # Menus a blocked submit already saw (recorded before 2026-10-06 they were dropped for a
    # question already on file): applied before answering, so the answer is chosen from them
    # without spending another submit (Woodgrove 9000000009 had one try left).
    latest = database.execute("""SELECT result FROM desktop_requests WHERE job_key=? AND operation='submit'
        AND armed_at IS NULL AND result IS NOT NULL ORDER BY created_at DESC LIMIT 1""", (job["job_key"],)).fetchone() \
        if database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='desktop_requests'").fetchone() else None
    try:
        observed = json.loads(latest[0]).get("observed_questions") if latest else None
    except (ValueError, TypeError, AttributeError):
        observed = None
    if isinstance(observed, list):
        with database:
            record_conditional_questions(database, job, observed, now)
    # Part C2 reuse: answers are drafted once per persisted form fingerprint (including
    # question sets backfilled from earlier partial reads) before any new read is decided.
    predraft_answers(database, job, policy, root, config, adapter, now)
    suppressed = read_suppression(database, job, now)
    if suppressed:
        # Part C1: the last read of this exact posting was inconclusive and nothing it could
        # depend on has changed. Spend no browser compute; hold until new input or expiry.
        with database:
            hold_suppressed(database, job, suppressed, now)
        return
    reused = reusable_read(database, job, now)
    if reused is not None:
        # The recorded read of this exact posting still stands and nothing it depends on has
        # changed, so no browser session is spent (owner, 2026-09-22).
        with database:
            event(database, job["job_key"], "APPLICATION_READ_REUSED",
                  {"record_observed_at": reused[REUSED_READ_MARKER],
                   "contract_revision": READ_CONTRACT_REVISION,
                   "expires_at": (dt.datetime.fromisoformat(reused[REUSED_READ_MARKER])
                                  + dt.timedelta(seconds=RECORD_MAX_AGE_SECONDS)).isoformat(),
                   "basis": "COMPLETE_READ_OF_THIS_IDENTITY_WITH_UNCHANGED_INPUTS"}, now)
    try:
        review = reused if reused is not None else adapter.call("inspect", {"job": job, "read_only": True})
        if clock:
            now = clock()
    except AdapterPending as pending:
        wait_for_adapter(database, job, pending, now, "WAITING_BROWSER")
        return
    except AdapterUnavailable as error:
        with database:
            hold(database, job, str(error), "Connect a tested authenticated ATS/browser adapter supporting inspect, submit, verify and reconcile", now)
        return
    except Exception as error:
        if clock:
            now = clock()
        with database:
            updated = database.execute("UPDATE application_jobs SET attempts=attempts+1 WHERE job_key=? AND state=? AND attempts=? AND updated_at=?",
                                       (job["job_key"], job["state"], job["attempts"], job["updated_at"]))
            if updated.rowcount != 1:
                return
            failure = application_recovery.exception_failure(error)
            event(database, job["job_key"], "APPLICATION_PREFLIGHT_FAILED", {
                "reason": failure["code"] or failure["exception_type"], "exception": failure,
                "operation": "inspect", "previous_execution_failures": application_recovery.previous_execution_failures(database, job["job_key"], root)}, now)
            # A stored read that failed deterministic validation is not a transient
            # preflight failure: keep its real code (e.g. APPLICATION_UNOBSERVED_QUOTE) so
            # it is neither mislabelled nor routed into investigations that cannot fix it.
            stored = failure["code"] if getattr(error, "stored_read_failure", False) and failure["code"] else None
            if stored and not application_recovery.settings(config):
                retry(database, job, policy, now, stored)
            elif stored:
                delays = policy["retry_delays_seconds"]
                next_at = (dt.datetime.fromisoformat(now) + dt.timedelta(
                    seconds=delays[min(max(job["attempts"], 0), len(delays) - 1)])).isoformat()
                hold(database, job, stored, "The saved read failed validation with " + stored
                     + "; a fresh bounded read follows after backoff within the unchanged attempt limit", now, "BLOCKED", next_at)
            elif application_recovery.settings(config):
                hold(database, job, "PREFLIGHT_TRANSIENT", "Investigate the saved preflight failure through a supported alternative", now)
            else:
                retry(database, job, policy, now, "PREFLIGHT_TRANSIENT")
        return
    current = database.execute("SELECT state,attempts,updated_at FROM application_jobs WHERE job_key=?", (job["job_key"],)).fetchone()
    if not current or any(current[key] != job[key] for key in ("state", "attempts", "updated_at")):
        return
    if review.get("blocked"):
        # normalize gives every stop a string code and next_action; a stored or foreign result may
        # not, so both are read defensively here (the code of an incomplete read, never invented text).
        stated = review["blocked"] if isinstance(review["blocked"], dict) else {"code": review["blocked"]}
        code = stated.get("code") if isinstance(stated.get("code"), str) and stated["code"].strip() else "INSPECTION_INCOMPLETE"
        action = (stated.get("next_action") if isinstance(stated.get("next_action"), str) and stated["next_action"].strip()
                  else "The read stopped with " + code + "; act on its saved partial review")
        blocker = dict(stated, code=code, next_action=action)
        if code in ACCESS_BLOCK_CODES:
            # Not an owner hold any more (owner, 2026-10-11): retried in an hour, the third abandons.
            with database:
                hold_access_block(database, job, code, action, now)
            return
        if (not application_recovery.user_hold(blocker["code"])
                and blocker["code"] != "APPLICATION_BROWSER_INVESTIGATION_EXHAUSTED"
                and (isinstance(review.get("partial_review"), dict) or review.get("observed_browser_calls"))):
            partial = review.get("partial_review") if isinstance(review.get("partial_review"), dict) else {}
            with database:
                complete_review(database, job, partial, now)
                remember_read(database, job, partial, blocker["code"], now,
                              # The page itself stopped the read; that is what is missing
                              # (owner, 2026-09-27: the deleted checks are no longer listed).
                              extra_missing=["read/" + blocker["code"]])
        if not recovery_checked and job_route(blocker["code"], "BLOCKED") == "RECOVER":
            if reread_inspection(database, job, policy, blocker["code"], schedule=True):
                with database:
                    current = database.execute("SELECT state,attempts,updated_at FROM application_jobs WHERE job_key=?", (job["job_key"],)).fetchone()
                    if not current or any(current[key] != job[key] for key in ("state", "attempts", "updated_at")):
                        return
                    event(database, job["job_key"], "INSPECTION_REREAD_SCHEDULED", {
                        "code": blocker["code"], "next_action": blocker["next_action"],
                        "max_attempts": policy["max_attempts"], "attempts_unchanged": job["attempts"]}, now)
                    retry(database, job, policy, now, blocker["code"])
                return
            if application_recovery.recover(database, job, adapter, config, blocker["code"], blocker["next_action"], now, clock, policy):
                return
            if application_recovery.settings(config) and not coordinator_suppression(database, job, blocker["code"], now):
                return
        with database:
            hold(database, job, blocker["code"], blocker["next_action"], now)
        return
    with database:
        complete_review(database, job, review, now)
        job = persist_read_reconciliations(database, job, review, now) or job
    derive_silent_eligibility_facts(review, policy)
    decision = assess(review, job, policy, now)
    # Relaxing the gate must not mean losing the information. Whatever the outcome - and
    # especially when the posting published nothing - the review that is stored, and that
    # later travels into `application_attempts.evidence`, carries what the employer stated
    # and what was concluded from it. `review["compensation"]` is left exactly as read.
    review["compensation_assessment"] = compensation_assessment(review, policy, now)
    review["eligibility_warnings"] = eligibility_warnings(review, now)
    with database:
        database.execute("INSERT INTO application_reviews VALUES (?,?,?,?,?)", (str(uuid.uuid4()), job["job_key"], now, decision[1] if decision else "ELIGIBLE", json.dumps(review)))
        event(database, job["job_key"], "ELIGIBILITY_CHECKED", {"decision": decision[1] if decision else "ELIGIBLE", "source": review.get("source"),
              "compensation": {field: review["compensation_assessment"][field] for field in ("stated", "stated_by_employer", "basis", "finding", "warnings", "note")},
              "eligibility_warnings": review["eligibility_warnings"], "eligibility_fact_basis": review.get("eligibility_fact_basis")}, now)
    if decision:
        with database:
            hold(database, job, decision[1], decision[2], now, decision[0])
            if decision[0] != "INELIGIBLE":
                remember_read(database, job, review, decision[1], now)
        return
    # Only a verified read (the kind `reusable_read` returns) is saved here. An unverified one is
    # never reused, so it stays an ordinary read that counts toward the inspect cap. An email
    # application with no valid address is not complete either: it is held below
    # (EMAIL_APPLICATION_ADDRESS_UNVERIFIED) for a fresh read, which a saved read would prevent.
    complete_read = reused is not None or (
        review.get("verified") is True
        and not (review.get("application_method") == EMAIL_METHOD and email_application(review) is None))
    if reused is None and complete_read:
        # A fresh read assessed ELIGIBLE is saved complete now, before answering and before the
        # submission's compute is booked (owner, 2026-10-02). It was saved only right before the
        # SUBMITTING claim, so a pass that stopped earlier (Cyberdyne: the submit booking hit the
        # shared budget five nights running) left nothing to reuse and the next pass read again,
        # each read spending one of the three inspect claims until recovery abandoned the job.
        # The next pass now reuses it (APPLICATION_READ_REUSED), and the event marks this read
        # complete so it never counts toward the inspect cap (application_connection.call).
        with database:
            remember_read(database, job, review, None, now, complete=True)
            event(database, job["job_key"], COMPLETE_READ_EVENT,
                  {"source": review.get("source"), "browser_trace": review.get("browser_trace"),
                   "observed_at": review.get("observed_at"), "basis": "ASSESSED_ELIGIBLE"}, now)
    # Deleted (owner, 2026-09-27): item 16, the read's own "already applied" signals and the
    # mailbox-derived absence inference - duplicates are decided only from the local database,
    # by local_gate before the read and again at the claim below; and item 17, the
    # all-questions-found check (and the unreadable-form escalation it fed). The form is filled
    # with whatever questions the read found.
    history = review.get("history") if isinstance(review.get("history"), dict) else {}
    questions = review.get("questions") if isinstance(review.get("questions"), list) else []
    if database.execute("SELECT 1 FROM user_decisions WHERE job_key=? AND field=? AND state='PENDING'",
                        (job["job_key"], decision_transport.FORM_NOT_READABLE_FIELD)).fetchone():
        with database:
            decision_transport.supersede_fact(database, job["job_key"], decision_transport.FORM_NOT_READABLE_FIELD,
                                              "FORM_NOW_ENUMERATED", now)
    policy = with_current_kept_answers(policy)   # answers kept on Approvals since the run began
    scoped_policy = dict(policy, answers=dict(policy["answers"]))
    # The escalation's own reply is never an application answer: it says the form could
    # not be read, not what the form asks. Excluding the reserved field here is what makes
    # "no invented answer" structural rather than a naming convention.
    for saved in database.execute("SELECT * FROM application_answers WHERE job_key=? AND field<>?",
                                  (job["job_key"], decision_transport.FORM_NOT_READABLE_FIELD)):
        scoped_policy["answers"][saved["field"]] = {"semantic": saved["semantic"], "value": json.loads(saved["value"]), "source": saved["source"]}
    # The computed answers are still written; the start-date rule's hold (item 19) is deleted
    # (owner, 2026-09-27).
    with database:
        materialize_computed_answers(database, job, questions, scoped_policy, now)
    # The AI answering step. `resolve_answers` binds a question to an answer by exact string
    # equality on a semantic the employer's own browser AI authored freely, so a fixed policy
    # key can never satisfy `office_attendance_2_3_days_per_week_in_springfield_or_austin` and the next
    # employer invents a different string for the same question. AGENTS.md assigns that
    # mapping to the AI - "Applications: navigate forms, map questions to verified facts" -
    # so the step below hands the AI the employer's own question and writes back an answer
    # minted for that exact semantic, which `resolve_answers` then matches unchanged.
    #
    # It is off unless `config["application_answering"]["enabled"] is True`, it is
    # contractually non-raising, and every failure - disabled, compute exhausted, timeout,
    # malformed output - writes nothing, so the question stays open and the ANSWERING_RETRY
    # hold below retries it on a later cycle; the owner is asked only a required question the
    # AI and its judge found no truthful answer to (`owner_questions`, owner 2026-10-24). `adapter` is the
    # browser adapter and is passed through only so an offline validation harness can inject
    # an executor; `answering_agent.injected_executor` ignores anything that is not one.
    outcome = {}
    try:
        import answering_agent
        outcome = answering_agent.materialize(database, job, questions, scoped_policy, now,
                                              adapter=adapter, root=root, config=config, review=review)
    except Exception:
        pass
    answers, missing = resolve_answers(questions, scoped_policy)
    provider_wait = ((outcome if isinstance(outcome, dict) else {}).get("audit") or {}).get("provider_wait")
    if missing and provider_wait:
        # The answering model was never consulted (provider usage limit): a capacity wait,
        # not unanswerable questions, so the owner is not asked and nothing is counted.
        with database:
            hold(database, job, provider_limits.CODE, provider_limits.NEXT_ACTION, now, "BLOCKED", provider_wait["resume_at"])
            event(database, job["job_key"], provider_limits.EVENT, dict(provider_wait, step="answering",
                  still_missing=missing), now)
        return
    if missing:
        # A required question the AI and its judge agree has no truthful answer in the owner's
        # records (owner, 2026-10-24: "If the required question has 'options' ... Those should be
        # displayed too, so a user can pass in their answer") is asked on Approvals' "Questions for
        # you", with the form's own choices. Everything else the answering step left open - a
        # failed or malformed call, a round limit, an answer that does not fit its control - is
        # the AI's to retry on a later cycle (ANSWERING_RETRY below), and the owner is not asked.
        asked = owner_questions(outcome, questions, missing)
        with database:
            close_settled_cards(database, job, missing, now)
            if asked:
                ask_owner(database, job, asked, policy, now)
            cards = open_cards(database, job, missing, now)
        if cards:
            # While any of this job's questions waits for the owner, the whole job is the owner's
            # hold (review round 1, Q1), even when the AI could retry other questions: no try is
            # spent, the read (saved complete above) is not marked inconclusive, so nothing counts
            # toward the inspect cap, and the job is not picked up again until the owner answers
            # (`decision_transport._apply_answer` makes it due; the next pass answers the rest as
            # usual) or the questions expire, when the next pass reads, answers and asks again.
            blocker = "UNKNOWN_REQUIRED_ANSWERS:" + ",".join(field for field in missing if field in cards)
            expires = dt.datetime.fromtimestamp(min(cards.values()) / 1000, dt.timezone.utc).isoformat()
            with database:
                hold(database, job, blocker, OWNER_QUESTION_ACTION, now, "BLOCKED", expires, route="OWNER")
                database.execute("UPDATE notification_outbox SET state='SUPERSEDED_BY_FACT_REQUEST' WHERE event_key=? "
                                 "AND state IN ('PENDING','WAITING_TRANSPORT')", (job["job_key"] + ":" + blocker,))
                event(database, job["job_key"], OWNER_QUESTION_EVENT,
                      {"questions": sorted(cards), "ai_retry": sorted(set(missing) - set(cards)), "expires_at": expires}, now)
            # An answer kept on Approvals while this job was being answered (owner, 2026-10-24) was
            # not in the packet: every open card it may cover (same semantic, the scope of a card's
            # policy hash) is closed and its job made due again, so the answering AI runs with it
            # (`decision_transport.refresh_stale`); if it still cannot answer, a card is raised anew.
            latest = with_current_kept_answers(policy)
            if kept_answers_of(latest) != kept_answers_of(policy):
                decision_transport.refresh_stale(database, latest, now)
            return
        blocker = "ANSWERING_RETRY:" + ",".join(missing)
        retry_at = (dt.datetime.fromisoformat(now) + dt.timedelta(hours=1)).isoformat()
        with database:
            hold(database, job, blocker, "Re-run the AI answering step on the next cycle; the owner is not asked",
                 now, "BLOCKED", retry_at)
            remember_read(database, job, review, blocker, now, extra_missing=["answers/" + field for field in missing])
        return
    with database:
        close_settled_cards(database, job, [], now)   # every question answered: no card stays open (Q2)
    resume = root / policy["resume"]["path"]
    if not resume.is_file() or hashlib.sha256(resume.read_bytes()).hexdigest() != policy["resume"]["sha256"]:
        with database:
            hold(database, job, "DESIGNATED_RESUME_MISMATCH", "Restore the designated resume or obtain explicit designation of its replacement", now)
        return
    if not policy.get("preapproved_applications"):
        with database:
            hold(database, job, "APPLICATION_AUTHORIZATION_REQUIRED", "Restore explicit application authorization before submission", now)
        return
    # Dedupe before every submit (owner, 2026-10-06: "we should never block employers unless the
    # jobs are the same, hence dedupe"). The employer-wide holds are gone, so a job read before
    # the same-posting judgment existed (Vandelay Security's board and LinkedIn copies of a role already
    # submitted) is judged here before it can be submitted; a verdict for unchanged inputs is reused.
    if root is not None and same_posting_duplicate(database, job, adapter, root, config, now, before_submit=True):
        return
    if hasattr(adapter, "prepare_submission"):
        try:
            adapter.prepare_submission(job)
        except AdapterPending as pending:
            wait_for_adapter(database, job, pending, now, "WAITING_BROWSER")
            return
    attempt_id = str(uuid.uuid4())
    database.execute("BEGIN IMMEDIATE")
    if isinstance(review, dict) and review.get("application_method") == EMAIL_METHOD:
        # Recognised and kept, never handed to the form filler (there is no form). The guarded
        # send route replaces this hold; until it exists the job waits here, non-terminal and
        # orchestrator-owned, instead of being abandoned for having nothing to fill. A review
        # that says EMAIL but whose address did not survive validation is held too, never
        # allowed to fall through to the form path as if it had a form.
        target = email_application(review)
        if target is None:
            with database:
                hold(database, job, "EMAIL_APPLICATION_ADDRESS_UNVERIFIED",
                     "The posting applies by email but no recipient address was quoted from a live read of it; "
                     "re-read the official posting", now)
            return
        if not email_applications_enabled(config):
            with database:
                hold(database, job, EMAIL_APPLICATION_READY,
                     "Ready to apply by email to " + target["address"] + "; the email route is switched off "
                     "(email_applications_enabled) until the owner turns it on", now)
            return
        # Switched on: from here an email application takes exactly the path a form does - the
        # same SUBMITTING claim, attempt row, arm and reconciliation. Only the browser's work
        # differs, and the verified target rides along so the arm can check the live To field.
    gate = local_gate(database, job, now, policy)
    claimed = None if gate else database.execute("UPDATE application_jobs SET state='SUBMITTING',attempts=attempts+1,blocker=NULL,updated_at=? WHERE job_key=? AND state=? AND attempts=? AND updated_at=? AND state NOT IN ('SUBMITTING','RECONCILE','SUBMITTED','DUPLICATE','EXHAUSTED','INELIGIBLE','ABANDONED') AND NOT EXISTS (SELECT 1 FROM application_attempts WHERE job_key=? AND state='SUBMITTING')",
                                                 (now, job["job_key"], job["state"], job["attempts"], job["updated_at"], job["job_key"]))
    if gate or claimed.rowcount != 1:
        database.rollback()
        return
    # `.get`, not `[...]`: since EP (2026-09-22) an unconfirmed history no longer blocks the
    # job, and the unconfirmed path sets `history = {}`. This line then raised KeyError
    # 'source' at the moment the job had already claimed SUBMITTING - Vandelay Security died here
    # on every run, reported only as "error type KeyError". The field beside it on the same
    # line was already using `.get`.
    event(database, job["job_key"], "HISTORY_ANSWERS_RESUME_CLEARED", {"history_source": history.get("source"), "resume_sha256": policy["resume"]["sha256"],
          "history_basis": history.get("basis")}, now)
    remember_read(database, job, review, None, now, complete=True)
    answer_sources = {question_field(question): [saved["source"] for saved in scoped_policy["answers"].values()
                                                 if saved.get("source") and saved.get("semantic") == question.get("semantic")
                                                 and saved.get("value") == answers[question_field(question)]]
                      for question in questions if question_field(question) in answers}
    database.execute("INSERT INTO application_attempts(attempt_id,job_key,idempotency_key,state,started_at,evidence) VALUES (?,?,?,'SUBMITTING',?,?)", (attempt_id, job["job_key"], job["job_key"], now, json.dumps({"review": review, "answers": answers, "answer_sources": answer_sources, "resume_sha256": policy["resume"]["sha256"], "requirement_revision": 2})))
    event(database, job["job_key"], "WORKER_SUBMISSION_STARTED", {"attempt_id": attempt_id, "preapproved": True}, now)
    database.commit()
    stage = "prepare"
    try:
        # Owner, 2026-09-19: a form that requires a cover letter as a FILE gets one generated for
        # this job. The AI already wrote the text as the answer to that question; it is rendered to
        # a PDF here and carried as an attachment instead of being typed into a file control.
        import cover_letter
        uploads = attachment_uploads(questions, answers, resume)
        attachments = cover_letter.prepare(root, job["job_key"],
                                           {key: {"value": value, "attachment": True} for key, value in uploads.items()})
        answers = {key: value for key, value in answers.items() if key not in attachments}
        submission = {"job": job, "attempt_id": attempt_id, "idempotency_key": job["job_key"], "answers": answers, "resume": str(resume), "form_revision": review.get("form_revision"), "no_extra_answers": True,
                      "recorded_facts": recorded_authorization_facts(policy)}
        if isinstance(review.get("eligible_location"), dict):
            # The location option `assess` found the owner authorized for (owner, 2026-10-23, J1 A).
            submission["eligible_location"] = review["eligible_location"]
        if email_application(review) is not None:
            submission["email_application"] = email_application(review)
        if attachments:
            submission["attachments"] = attachments
        if review.get("form_revision_basis") == CODE_FORM_REVISION_BASIS:
            # The submit session re-reads the live form and must report what it sees to the
            # arm guard; these are what it compares against. Deferred conditional questions
            # and the deferred email binding are enforced there, before any Submit.
            # Each question carries the FIELD its answer is stored under (owner, 2026-09-21).
            # The submit session used to re-describe every question from its own reading of the
            # page, and the arm guard re-derived the key from that description - so two
            # independent transcriptions of one form had to agree byte for byte or a filled
            # answer looked missing. A required-marker asterisk in the label was enough to
            # refuse a complete application. The session now echoes this exact field back
            # instead of re-describing, which turns a transcription into a lookup. Only a
            # question that is genuinely NEW - revealed by filling the form, so not in this
            # list - is reported without a field, and only that one is derived.
            submission.update(questions=[dict({key: question.get(key) for key in ("id", "label", "semantic", "required")},
                                              field=question_field(question))
                                         for question in questions if isinstance(question, dict) and question.get("required") is True],
                              conditional_questions_deferred=review.get("conditional_questions_deferred") is True,
                              candidate_email_binding=review.get("candidate_email_binding"))
        stage = "submit"
        result = adapter.call("submit", submission)
        if result.get("blocked"):
            if submit_request_never_created(database, adapter, job["job_key"], attempt_id):
                # The adapter refused the request before creating it (e.g. request_body's
                # destination/requisition ValueError): nothing can have been sent, and the try is spent.
                with database:
                    release_uncreated_submission(database, job, [attempt_id], {"blocked": result["blocked"]}, now, free=False)
                    retry(database, job, policy, now, SUBMIT_REQUEST_NEVER_CREATED)
                return
            with database:
                hold_submission_blocker(database, job, result, clock() if clock else now)
            return
        # A page confirmation captured by the submit session settles the job now; no verify read
        # is spent on it (owner, 2026-10-06).
        if result.get("acknowledged") is True and settle_page_confirmation(database, job, clock() if clock else now, root):
            return
        stage = "verify"
        receipt = adapter.call("verify", {"job": job, "result": result, "independent_read_required": True})
    except AdapterPending as pending:
        if submit_request_never_created(database, adapter, job["job_key"], attempt_id):
            # Refused before its request row existed (no browser time left, a provider wait): a
            # compute wait, never a try (owner, 2026-09-28), and nothing to reconcile.
            with database:
                release_uncreated_submission(database, job, [attempt_id], pending, now, free=True)
                hold(database, job, getattr(pending, "code", "DESKTOP_BROWSER_PENDING"),
                     "Wait for browser time, then apply again; this attempt never reached a browser and is closed as not submitted",
                     now, "WAITING_BROWSER", pending.next_at)
            return
        wait_for_adapter(database, job, pending, now, "SUBMITTING")
        return
    except Exception as error:
        import application_recovery
        if submit_request_never_created(database, adapter, job["job_key"], attempt_id):
            with database:
                release_uncreated_submission(database, job, [attempt_id], error, now, free=False)
                retry(database, job, policy, now, SUBMIT_REQUEST_NEVER_CREATED)
            return
        with database:
            event(database, job["job_key"], "SUBMIT_PATH_EXCEPTION",
                  {"stage": stage, "attempt_id": attempt_id, "exception": application_recovery.exception_failure(error)}, now)
        receipt = {}
    if clock:
        now = clock()
    with database:
        if verified_receipt(receipt, job, now):
            finish_receipt(database, job, receipt, now)
        else:
            hold(database, job, "SUBMISSION_OUTCOME_UNKNOWN", "Independently reconcile the receipt before retrying", now, "RECONCILE", now)


def build_adapter(database, config, root, clock=None):
    """The one adapter a scheduled cycle owns.

    Extracted unchanged from `cycle` so a caller that must act before the job loop shares
    the same instance, and with it the same per-cycle browser capacity counter, instead of
    constructing a second one.
    """
    clock = clock or stamp
    if config.get("application_adapter") == "desktop":
        if config.get("application_browser", {}).get("enabled") is True:
            import application_connection
            return application_connection.HostedDesktopAdapter(database, root, config, clock=clock)
        import desktop_adapter
        return desktop_adapter.DesktopAdapter(database, root, config, clock=clock)
    return CommandAdapter(config)


def settle_all_browser_reservations(database, now=None):
    """Settle every job's unsettled browser reservations. Returns the seconds credited.

    Owner, 2026-09-28: "everything that needs to be refunded must refund before shutdown. And
    everything that hasnt refunded should refund right after startup." Called first and last by
    the applications job (`orchestrator.applications_service`, owner 2026-10-02), the only maker
    of these reservations, and at the start of each application cycle, not only for the jobs a
    cycle happens to process. Settling is idempotent and never raises.
    """
    import workflow_tasks
    now = now or stamp()
    total = 0
    try:
        keys = [row[0] for row in database.execute(
            "SELECT DISTINCT job_key FROM application_browser_reservations WHERE settled_at IS NULL")]
    except sqlite3.Error:
        return 0
    for key in keys:
        try:
            with database:
                credited = workflow_tasks.settle_browser_reservations(database, key, now)
                if credited:
                    event(database, key, "BROWSER_CAPACITY_CREDITED",
                          {"seconds": credited, "basis": "settled: unused or never-started booking"}, now)
            total += credited or 0
        except Exception:
            continue
    return total


def cycle(database, config, root, adapter=None, now=None, only=None, should_stop=None):
    """One application cycle. `only` restricts it to an explicit set of job keys.

    `should_stop` (the applications job, owner 2026-10-02) is asked before each job; when it says
    so the cycle ends after the job in hand, e.g. when daily research takes its turn.

    Owner, 2026-09-21 ("ONLY Kelber means only kebler"): driving a single job was previously
    faked by pushing every other job's next_at into the future, which cannot work - the Gmail
    history sweep selects employers by job STATE, not by schedule (external_history
    history_search_candidates), so another employer's mailbox search ran anyway. Scope is now
    a real parameter honoured by both the job loop and the history sweep.
    """
    clock = stamp if now is None else None
    now = now or stamp()
    initialize(database)
    policy = user_profile.load()
    # Deleted (owner, 2026-09-27): filling in missing requisition records each cycle, and the
    # Gmail history search - duplicates are checked against the local database only.
    if adapter is None:
        adapter = build_adapter(database, config, root, clock or (lambda: now))
    import workflow_tasks
    settle_all_browser_reservations(database, now)
    # Jobs waiting on a submission the submit page already confirmed are settled first, with no
    # browser read (owner, 2026-10-06).
    settle_page_confirmations(database, now, root, only)
    local_day = workflow_tasks.budget_day(now, config.get("timezone", "America/Los_Angeles"))
    try:
        budget_room = config.get("daily_compute_seconds", 10800) - workflow_tasks.compute_used(database, local_day)
    except sqlite3.Error:
        budget_room = 0
    # A job parked on the claim-exhaustion hold waits for a REVIEW, not for a clock (owner,
    # 2026-09-24: recovery deadlines removed). That hold used to carry the recovery deadline as
    # its next_at, which is what kept the ordinary loop off it; with no deadline its next_at is
    # NULL, and `next_at IS NULL` means due now, so the loop would immediately pull the job back
    # out of the park it was just placed in. Only application_recovery.review_exhausted_holds may
    # act on it, and it runs first in the same cycle, so a resumed job still progresses here
    # immediately - it simply no longer carries this blocker once resumed.
    rows = list(database.execute("""SELECT * FROM application_jobs WHERE state NOT IN ('SUBMITTED','DUPLICATE','INELIGIBLE','EXHAUSTED','ABANDONED')
        AND blocker IS NOT 'APPLICATION_BROWSER_INVESTIGATION_EXHAUSTED'
        AND (next_at IS NULL OR next_at<=? OR blocker='APPLICATION_BROWSER_SHARED_BUDGET_EXHAUSTED'
             OR blocker='APPLICATION_HISTORY_UNKNOWN' OR blocker='PRIOR_APPLICATION_WITHIN_90_DAYS')
        ORDER BY updated_at,job_key""", (now,)))
    if only is not None:
        rows = [job for job in rows if job["job_key"] in only]
    ready = []
    for job in rows:
        if job["next_at"] and job["next_at"] > now and job["blocker"] != "PRIOR_APPLICATION_WITHIN_90_DAYS":
            import workflow_tasks
            available = getattr(workflow_tasks, "application_recovery_available", None)
            # A job waiting on the shared budget comes back as soon as today's budget has room
            # again (refunds free it mid-day), not only at the next reset (owner, 2026-09-28).
            budget_wait = job["blocker"] == "APPLICATION_BROWSER_SHARED_BUDGET_EXHAUSTED"
            if not budget_wait or (budget_room < 240 and (not available or not available(database, job["job_key"], now, seconds=240))):
                continue
        ready.append(job)
        if len(ready) >= 60:
            break
    stopped = None
    for job in ready:
        if should_stop is not None and should_stop():
            stopped = "YIELDED_TO_DAILY_RESEARCH"
            break
        try:
            with log.tagged("job", job["job_key"]):   # its lines carry the job (Failures page, 2026-10-23)
                process(database, job, adapter, policy, root, now, clock, config)
        except Exception as error:
            database.rollback()
            current = database.execute("SELECT * FROM application_jobs WHERE job_key=?", (job["job_key"],)).fetchone()
            # "error type KeyError" is not a diagnosis. The traceback is the only record of
            # where the cycle actually died, and it was being discarded at the one point where
            # something had already gone wrong - the same blindness as the answering audit
            # dropping its repair turns. Recorded as an event so it survives the run, bounded
            # so a pathological trace cannot bloat the ledger.
            trace = "".join(traceback.format_exception(type(error), error, error.__traceback__))
            with database:
                event(database, job["job_key"], "APPLICATION_CYCLE_EXCEPTION",
                      {"error_type": type(error).__name__, "error": str(error)[:400],
                       "state_at_failure": current["state"], "traceback": trace[-4000:]}, now)
                hold(database, current, "SUBMISSION_OUTCOME_UNKNOWN" if current["state"] in UNCERTAIN else "MALFORMED_OR_UNAVAILABLE_EVIDENCE",
                     "Inspect adapter evidence; error type " + type(error).__name__, now,
                     "RECONCILE" if current["state"] in UNCERTAIN else "BLOCKED")
        # Credit back whatever this job's finished browser reservations did not use (owner,
        # 2026-09-22). Runs for a failed job too: `settle_browser_reservations` settles a
        # reservation once its sessions all committed a result, a failure included (2026-09-29);
        # only a session that never committed anything keeps its whole charge. Outside the try/except above deliberately - it is
        # contractually non-raising, and a compute credit must never decide a job's outcome.
        import workflow_tasks
        # The transaction wraps the SETTLE, not just the event. `settle_browser_reservations`
        # writes `settled_at` for every reservation it settles, including one whose session used
        # its whole booking and so credits nothing back. Committing only `if credited` left that
        # write in an open implicit transaction, and the next job's
        # browser pre-request check (since deleted) then died on "cannot start a transaction
        # within a transaction" - three jobs on 2026-09-24 (rekall, dinoco, cambridge
        # aerospace), each immediately after a batch of zero-credit settles. The event is still
        # recorded only when something was actually credited.
        with database:
            credited = workflow_tasks.settle_browser_reservations(database, job["job_key"], now)
            if credited:
                event(database, job["job_key"], "BROWSER_CAPACITY_CREDITED",
                      {"seconds": credited, "basis": "measured elapsed, unused tail of a committed reservation"}, now)
    result = report(database, now)
    if stopped:
        result["stopped"] = stopped
    return result


def report(database, now=None):
    return {"observed_at": now or stamp(), "execution_owner": "local-mac",
            "blockers": [dict(row) for row in database.execute("SELECT * FROM application_blockers ORDER BY job_key")],
            "applications": [dict(row) for row in database.execute("SELECT job_key,state,attempts,reconcile_attempts,blocker,next_action,next_at,receipt FROM application_jobs ORDER BY job_key")],
            "notifications": [dict(row) for row in database.execute("SELECT event_key,state,attempts,last_error,delivery FROM notification_outbox ORDER BY event_key")],
            "verified_submissions": database.execute("SELECT COUNT(*) FROM application_jobs WHERE state='SUBMITTED'").fetchone()[0],
            "drafts_are_not_submissions": True}
