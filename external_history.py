import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import urllib.parse
import user_profile
import paths


KINDS = {"APPLICATION_CONFIRMED": "APPLICATION", "INTERVIEW_CONFIRMED": "INTERVIEW",
         "PROCESS_CLOSED": "APPLICATION_CLOSED"}


def initialize(database):
    database.executescript("""
        CREATE TABLE IF NOT EXISTS application_external_history (
            evidence_id TEXT PRIMARY KEY, employer TEXT NOT NULL, kind TEXT NOT NULL,
            requisition TEXT, role_title TEXT, occurred_at TEXT NOT NULL,
            observed_at TEXT NOT NULL, payload TEXT NOT NULL, content_hash TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS application_history_resolutions (
            evidence_id TEXT NOT NULL, job_key TEXT NOT NULL, relationship TEXT NOT NULL,
            evidence TEXT NOT NULL, observed_at TEXT NOT NULL,
            PRIMARY KEY(evidence_id,job_key));
        CREATE TABLE IF NOT EXISTS application_history_searches (
            search_id TEXT PRIMARY KEY, employer TEXT NOT NULL, mailbox_email TEXT NOT NULL,
            outcome TEXT NOT NULL, searched_at TEXT NOT NULL, observed_at TEXT NOT NULL,
            payload TEXT NOT NULL, content_hash TEXT NOT NULL);
    """)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


DAY_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")


def day_instant(value):
    """A Gmail date as a timezone-qualified instant, or the value unchanged.

    Gmail's list rows show a date without a time ("Apr 18"), so the browser step may only be
    able to report the day. Day resolution is enough for the recorded history, not a timestamp, so
    a date-only value becomes that day at 00:00 UTC and a trailing "Z" becomes +00:00. Anything
    else is returned untouched and still faces `validate`, which requires a dated, tz-aware
    instant (aperture, 2026-09-20: both acknowledgments were discarded for a null received_at).
    """
    if not isinstance(value, str) or not value.strip():
        return value
    text = value.strip()
    if DAY_ONLY.fullmatch(text):
        return text + "T00:00:00+00:00"
    if text.endswith("Z"):
        return text[:-1] + "+00:00"
    return text


def validate(record, candidate_email):
    import applications
    required = ("evidence_id", "employer", "kind", "source", "source_message_id",
                "sender", "mailbox_email", "recipient", "occurred_at", "observed_at", "summary")
    if any(not isinstance(record.get(key), str) or not record[key].strip() for key in required):
        raise ValueError("Complete sourced employer history required")
    if record["kind"] not in KINDS or record.get("evidence_kind") != "employer_email":
        raise ValueError("Only positive independent employer history is supported; search absence is not evidence")
    if record.get("verified") is not True or record.get("sender_authenticated") is not True or record.get("independent_read") is not True:
        raise ValueError("Read and authenticate the employer message before importing")
    if record["mailbox_email"].casefold() != candidate_email.casefold() or record.get("delivered_to_owner") is not True:
        raise ValueError("Candidate mailbox identity must match; do not adopt a recipient alias as a new answer")
    source = urllib.parse.urlsplit(record["source"])
    if source.scheme != "https" or source.hostname != "mail.google.com" or source.username or source.password:
        raise ValueError("Supported authenticated Gmail message source required")
    if source.fragment != "all/" + record["source_message_id"] or record["evidence_id"] != "gmail:" + record["source_message_id"]:
        raise ValueError("Stable source message identity mismatch")
    if "aiapply" in record["sender"].casefold() or "aiapply" in record.get("source_system", "").casefold():
        raise ValueError("AiApply is explicitly outside this project's scope")
    occurred = dt.datetime.fromisoformat(record["occurred_at"])
    observed = dt.datetime.fromisoformat(record["observed_at"])
    if occurred.tzinfo is None or observed.tzinfo is None or occurred > observed:
        raise ValueError("Dated historical source required")
    requisition = record.get("requisition")
    if requisition is not None:
        if not isinstance(requisition, str):
            raise ValueError("An exact requisition requires explicit mapping evidence; unknown is null")
        requisition = applications.normalized(requisition)
        if (not requisition or requisition == "*" or applications.weak_identity(requisition)
                or not record.get("requisition_source")):
            raise ValueError("An exact requisition requires explicit mapping evidence; unknown is null")
    record = dict(record, employer=applications.normalized(record["employer"]))
    if record.get("requisition") is not None:
        record["requisition"] = requisition
    return record


def ingest(database, record, candidate_email):
    record = validate(record, candidate_email)
    content = {key: value for key, value in record.items() if key != "observed_at"}
    content_hash = hashlib.sha256(encoded(content).encode()).hexdigest()
    previous = database.execute("SELECT content_hash FROM application_external_history WHERE evidence_id=?", (record["evidence_id"],)).fetchone()
    if previous:
        if previous[0] != content_hash:
            raise ValueError("Conflicting history cannot overwrite an existing source observation")
        return False
    database.execute("INSERT INTO application_external_history VALUES (?,?,?,?,?,?,?,?,?)",
                     (record["evidence_id"], record["employer"], record["kind"], record.get("requisition"),
                      record.get("role_title"), record["occurred_at"], record["observed_at"], encoded(record), content_hash))
    if record.get("requisition"):
        database.execute("INSERT OR IGNORE INTO application_history VALUES (?,?,?,?)",
                         (record["employer"], record["requisition"], KINDS[record["kind"]], encoded(record)))
    return True


# ---------------------------------------------------------------------------------------
# Owner decision D1 (2026-09-19): history absence from the local tracker plus a Gmail search.
#
# A `gmail_history_search` record now enters only through this module's evidence-file import
# (`main`, operator-run). The runtime Gmail history search was deleted (owner, 2026-09-27):
# duplicates are decided from the local database.
SEARCH_OUTCOMES = {"NO_MATCH", "MATCH_FOUND", "FAILED"}
SEARCH_CATEGORIES = {"application_confirmation", "interview", "rejection"}


def validate_search(record, candidate_email):
    import applications
    if record.get("record_type") != "gmail_history_search":
        raise ValueError("Gmail history search record required")
    required = ("search_id", "employer", "mailbox_email", "outcome", "searched_at", "observed_at")
    if any(not isinstance(record.get(key), str) or not record[key].strip() for key in required):
        raise ValueError("Complete Gmail history search record required")
    if record["outcome"] not in SEARCH_OUTCOMES:
        raise ValueError("Unsupported Gmail history search outcome")
    if record["mailbox_email"].casefold() != candidate_email.casefold():
        raise ValueError("Candidate mailbox identity must match")
    if "aiapply" in encoded(record).casefold():
        raise ValueError("AiApply is explicitly outside this project's scope")
    searched = dt.datetime.fromisoformat(record["searched_at"])
    observed = dt.datetime.fromisoformat(record["observed_at"])
    if searched.tzinfo is None or observed.tzinfo is None or searched > observed:
        raise ValueError("Dated Gmail history search required")
    if record["outcome"] == "FAILED":
        if not isinstance(record.get("failure_reason"), str) or not record["failure_reason"].strip():
            raise ValueError("A failed search must say why it failed")
    else:
        source = urllib.parse.urlsplit(str(record.get("source") or ""))
        query = record.get("query")
        if (source.scheme != "https" or source.hostname != "mail.google.com" or source.username or source.password
                or not source.fragment.startswith("search/")):
            raise ValueError("Supported authenticated Gmail search source required")
        if (not isinstance(query, str) or applications.normalized(record["employer"]) not in applications.normalized(query)
                or record.get("performed") is not True or record.get("independent_read") is not True
                or not SEARCH_CATEGORIES <= set(record.get("searched_for") or ())):
            raise ValueError("An employer-scoped search that actually ran is required")
        count = record.get("result_count")
        if type(count) is not int or (count != 0) != (record["outcome"] == "MATCH_FOUND"):
            raise ValueError("Search outcome must agree with its result count")
    return dict(record, employer=applications.normalized(record["employer"]))


def ingest_search(database, record, candidate_email):
    record = validate_search(record, candidate_email)
    content = {key: value for key, value in record.items() if key != "observed_at"}
    content_hash = hashlib.sha256(encoded(content).encode()).hexdigest()
    previous = database.execute("SELECT content_hash FROM application_history_searches WHERE search_id=?", (record["search_id"],)).fetchone()
    if previous:
        if previous[0] != content_hash:
            raise ValueError("Conflicting search cannot overwrite an existing search record")
        return False
    database.execute("INSERT INTO application_history_searches VALUES (?,?,?,?,?,?,?,?)",
                     (record["search_id"], record["employer"], record["mailbox_email"], record["outcome"],
                      record["searched_at"], record["observed_at"], encoded(record), content_hash))
    return True


def tracker_evidence(database, job, now, current_attempt=None):
    """Every local record that shows (or might show) an application/interview for this job.

    Uses the exact duplicate gates first (`applications.local_gate` covers application_history
    under the job's requisition and original key, and normalization-only identity matches; '*'
    rows and the 90-day employer window no longer block, owner 2026-10-06),
    then this job's own attempts and conversion interview/outcome records (the recovered tracker's
    employer-wide rows no longer count, 2026-10-06). A different requisition proven DISTINCT_ROLE is
    not evidence for this job, exactly as the existing gate treats it.
    """
    import applications
    found = []
    gate = applications.local_gate(database, job, now)
    if gate:
        found.append({"source": "local_gate", "code": gate[1]})
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for attempt_id, state in database.execute("SELECT attempt_id,state FROM application_attempts WHERE job_key=?", (job["job_key"],)):
        # The attempt being guarded right now is not prior history for itself.
        if state != "CONFIRMED_NOT_SUBMITTED" and attempt_id != current_attempt:
            found.append({"source": "application_attempts", "attempt_id": attempt_id, "state": state})
    for table in ("conversion_interviews", "conversion_outcomes"):
        if table in tables and database.execute("SELECT 1 FROM " + table + " WHERE job_key=? LIMIT 1", (job["job_key"],)).fetchone():
            found.append({"source": table})
    # The recovered tracker's rows for the EMPLOYER (any role) no longer count as this job's
    # history (owner, 2026-10-06: "never block employers unless the jobs are the same").
    return found


# The 2026-09-19 90-day employer window is deleted with all its code (owner, 2026-10-06:
# "completely remove it"). Emails that reached the owner's inbox through an alias (an
# address at another domain that forwards to it) count like any other.
# This never relaxes the same-job guards: exact application_history rows (this system's own
# receipts, SAME_ROLE mappings, imports carrying this job's requisition), attempts, submit
# claims and unknown-outcome reconciliation stay absolute.


# A local receipt - one of OUR OWN recorded applications or interviews - needs a stable id so
# a comparison against it can be recorded in `application_history_resolutions` exactly like a
# comparison against a Gmail confirmation. `application_history` has no id column, but its
# PRIMARY KEY is (employer, requisition, kind), so within an employer the pair identifies the
# row. The id is derived from what the row IS, never from anything a model reported.
#
# Ids are only ever recomputed and compared, never parsed back: a requisition may itself
# contain ':' (every URL identity does), so splitting this string would be ambiguous.
LOCAL_RECEIPT_PREFIX = "application_history:"


def local_receipt_id(requisition, kind):
    return LOCAL_RECEIPT_PREFIX + str(requisition) + ":" + str(kind)


def local_receipt(database, employer, evidence_id):
    """The employer's own recorded receipt this evidence id names, or None."""
    if not isinstance(evidence_id, str) or not evidence_id.startswith(LOCAL_RECEIPT_PREFIX):
        return None
    for requisition, kind, evidence in database.execute(
            "SELECT requisition,kind,evidence FROM application_history WHERE employer=? AND kind IN ('APPLICATION','INTERVIEW')",
            (employer,)):
        if local_receipt_id(requisition, kind) == evidence_id:
            return {"evidence_id": evidence_id, "employer": employer, "requisition": requisition,
                    "kind": kind, "evidence": evidence}
    return None


def absence_conclusion(database, job, now, current_attempt=None):
    """May this job's application/interview history be concluded absent right now?

    Owner, 2026-09-22: "remove the gmail history search completely. Let tracker db be source
    of truth." The tracker alone decides. Concluded absent when it shows nothing that blocks
    this job (owner, 2026-10-06: the 90-day employer window is deleted; history at the employer
    is recorded, never blocking).

    This used to additionally require a fresh, successful, employer-scoped Gmail search, and
    concluded NOTHING without one. That requirement is what actually stopped this product
    applying to anything: four of the last five searches failed with
    APPLICATION_UNOBSERVED_QUOTE - the verification layer looking for the session's quoted
    text verbatim in what the browser captured, and not finding it when Gmail's UI rendered
    differently. A failed search left history unknown, and unknown history then tripped nine
    separate downstream gates, every one of which had to be found and removed by hand on
    2026-09-22. The mailbox is still how a SUBMISSION is confirmed (the verify/receipt path,
    which is how Pawnee Health was verified on 2026-09-21); it is no longer how a PRIOR
    application is ruled out.
    """
    import applications
    tracker = tracker_evidence(database, job, now, current_attempt)
    base = {"concluded": False, "job_key": job["job_key"], "tracker_evidence": tracker}
    if tracker:
        return dict(base, reason="LOCAL_TRACKER_EVIDENCE_PRESENT")
    # History at the employer for another posting never withholds the conclusion (owner,
    # 2026-10-06: "never block employers unless the jobs are the same"; the 90-day window is gone).
    history = {"verified": True, "job_key": job["job_key"], "application": False, "interview": False,
               "source": "local-tracker", "observed_at": now,
               "basis": applications.HISTORY_ABSENCE_BASIS,
               "owner_authorization": dict(applications.HISTORY_ABSENCE_AUTHORIZATION),
               # What tracker_evidence and the gate actually read.
               "tracker_checked": ["application_history", "application_attempts", "conversion_interviews",
                                   "conversion_outcomes", "external_history.gate"],
               "inference": "The local tracker records no application, interview or rejection of this job"}
    return dict(base, concluded=True, reason=None, history=history)


def requisition_identities(job):
    """The exact requisition keys duplicate lookups use, plus their canonical forms."""
    import applications
    prefix = job["employer"] + ":"
    original = job["job_key"][len(prefix):] if job["job_key"].startswith(prefix) else job["requisition"]
    exact = {job["requisition"], original}
    return exact, {applications.normalized(value) for value in exact}


def unmatched_identity_history(database, job):
    """Recorded history for this employer that matches this job only after normalization.

    The exact-key duplicate lookups (applications.local_gate, conversion._duplicate)
    cannot see such a row, so prior-application existence is indeterminate: block.
    A genuinely different requisition does not normalize into this job's identities
    and is deliberately left alone.
    """
    import applications
    exact, canonical = requisition_identities(job)
    for requisition, kind in database.execute(
            "SELECT requisition, kind FROM application_history WHERE employer=? ORDER BY requisition", (job["employer"],)):
        if requisition == "*" or requisition in exact:
            continue
        if applications.normalized(requisition) in canonical:
            return requisition, kind
    return None


def gate(database, job, now=None):
    # The 90-day employer window (PRIOR_APPLICATION_WITHIN_90_DAYS, 2026-09-19) is deleted (owner,
    # 2026-10-06: "we should never block employers unless the jobs are the same, hence dedupe").
    # Recorded history of THIS job (applications.local_gate), a near-identical requisition below
    # and the same-posting judgment (before the first read and before every submit) are the dedupe.
    unmatched = unmatched_identity_history(database, job)
    if unmatched:
        return ("BLOCKED", "PRIOR_APPLICATION_IDENTITY_UNRESOLVED",
                "Recorded " + unmatched[1] + " history for this employer is stored under requisition '" + unmatched[0]
                + "', which matches this job only after normalization; verify whether it is the same role and re-record the"
                + " canonical requisition before submitting. Do not submit while prior-application existence is indeterminate", None)
    return None


def resolve(database, evidence_id, job_key, relationship, evidence, now):
    import applications
    if relationship not in {"SAME_ROLE", "DISTINCT_ROLE"} or not applications.fresh(evidence, now):
        raise ValueError("Dated verified requisition comparison required")
    if evidence.get("evidence_id") != evidence_id or evidence.get("job_key") != job_key or not evidence.get("comparison"):
        raise ValueError("Resolution must bind the exact source and job and explain the comparison")
    job = database.execute("SELECT * FROM application_jobs WHERE job_key=?", (job_key,)).fetchone()
    record = database.execute("SELECT * FROM application_external_history WHERE evidence_id=?", (evidence_id,)).fetchone()
    # A comparison may bind one of OUR OWN receipts as well as a Gmail confirmation (owner,
    # 2026-09-24). Both are scoped to the job's employer; the local one carries no message
    # payload, so `kind` is already APPLICATION/INTERVIEW rather than an external verdict.
    receipt = local_receipt(database, job["employer"], evidence_id) if job else None
    if not job or not (record or receipt) or record and record["employer"] != job["employer"]:
        raise ValueError("Canonical employer/job matching required")
    # The refusal of a weak `job["requisition"]` that used to sit on that line is gone with
    # approach A. A posting identified by the hash of its canonical URL is now submittable, so
    # it must be able to settle its own prior-application question; refusing to record the
    # verdict left every such job stuck behind the 90-day window with no way out.
    previous = database.execute("SELECT relationship FROM application_history_resolutions WHERE evidence_id=? AND job_key=?", (evidence_id, job_key)).fetchone()
    if previous:
        if previous[0] != relationship:
            raise ValueError("Conflicting resolution needs explicit audit, not replacement")
        return False
    database.execute("INSERT INTO application_history_resolutions VALUES (?,?,?,?,?)",
                     (evidence_id, job_key, relationship, encoded(evidence), now))
    if relationship == "SAME_ROLE":
        # SAME_ROLE means this job IS the process that record describes, so the employer/role
        # pair gains a local application/interview row and no second submission can follow.
        # Identical for either source; only where the kind and the historical detail come from
        # differs, since a local receipt is already an APPLICATION/INTERVIEW and has no message.
        kind = KINDS[record["kind"]] if record else receipt["kind"]
        source = json.loads(record["payload"]) if record else json.loads(receipt["evidence"])
        database.execute("INSERT OR IGNORE INTO application_history VALUES (?,?,?,?)",
                         (job["employer"], job["requisition"], kind, encoded({"historical_source": source, "mapping": evidence})))
    database.execute("UPDATE application_jobs SET next_at=NULL WHERE job_key=?", (job_key,))
    return True


# ---------------------------------------------------------------------------------------
# Part E (owner-approved 2026-09-19): the runtime Gmail history search.
#


# Owner (2026-09-19): "opening my mail in browser should support account switch since i have
# multiple accounts logged in." /mail/u/0/ is whichever Google account Chrome signed in first,
# not necessarily the candidate mailbox, so every Gmail URL this project builds addresses the
# mailbox by its EMAIL (authuser), which Gmail resolves to that already-signed-in account. The
# URL only selects the account: the mailbox identity is still verified from what the page shows.
# Stored records keep whatever URL they were written with; the validators check host and
# fragment only, so the legacy /mail/u/0/#all/<id> form and this one are both accepted.
def gmail_url(mailbox_email, fragment):
    """https://mail.google.com/mail/?authuser=<email>#<fragment> for the named signed-in account."""
    if not isinstance(mailbox_email, str) or "@" not in mailbox_email or any(ch.isspace() for ch in mailbox_email):
        raise ValueError("Candidate mailbox email required to address Gmail")
    return ("https://mail.google.com/mail/?authuser=" + urllib.parse.quote(mailbox_email.strip(), safe="@.+-_")
            + "#" + fragment)


def gmail_account_url(source, mailbox_email):
    """A stored Gmail URL (e.g. legacy /mail/u/0/#all/<id>) re-addressed to the candidate account.

    Returns None for anything that is not a plain https://mail.google.com/ URL with a fragment.
    """
    try:
        parsed = urllib.parse.urlsplit(str(source or ""))
        if (parsed.scheme != "https" or parsed.hostname != "mail.google.com" or parsed.username
                or parsed.password or not parsed.fragment):
            return None
        return gmail_url(mailbox_email, parsed.fragment)
    except ValueError:
        return None


def main():
    import applications
    import orchestrator
    parser = argparse.ArgumentParser(description="Append independent employer history; dry-run unless --apply")
    parser.add_argument("evidence_file", type=Path)
    parser.add_argument("--root", type=Path, default=paths.data())
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    records = json.loads(args.evidence_file.read_text())
    if not isinstance(records, list):
        raise ValueError("Evidence file must contain a list")
    policy = user_profile.load()
    email = policy["answers"]["email"]["value"]
    searches = [validate_search(record, email) for record in records if record.get("record_type") == "gmail_history_search"]
    records = [validate(record, email) for record in records if record.get("record_type") != "gmail_history_search"]
    if not args.apply:
        # Machine contract (E2.T6): the command's JSON result; stays print.
        print(encoded({"dry_run": True, "records": len(records), "gmail_history_searches": len(searches),
                       "unresolved_requisitions": sum(not record.get("requisition") for record in records)}))
        return
    import run_locks
    # Application records belong to the applications job too (owner, 2026-10-02).
    with run_locks.held(args.root, run_locks.RUNNER, run_locks.APPLICATIONS) as acquired:
        if not acquired:
            raise RuntimeError("Existing worker is active; retry import later")
        with orchestrator.connect(args.root) as database:
            applications.initialize(database)
            database.execute("BEGIN IMMEDIATE")
            added = sum(ingest(database, record, email) for record in records)
            searched = sum(ingest_search(database, record, email) for record in searches)
            for job in database.execute("SELECT * FROM application_jobs WHERE state NOT IN ('SUBMITTED','DUPLICATE','INELIGIBLE','EXHAUSTED','SUBMITTING','RECONCILE')").fetchall():
                decision = applications.local_gate(database, job, applications.stamp())
                if decision:
                    applications.hold(database, job, decision[1], decision[2], applications.stamp(), decision[0], decision[3])
            database.commit()
            # Machine contract (E2.T6): the command's JSON result; stays print.
            print(encoded({"imported": added, "existing": len(records) - added, "gmail_history_searches_imported": searched,
                           "network_actions": 0}))


if __name__ == "__main__":
    main()
