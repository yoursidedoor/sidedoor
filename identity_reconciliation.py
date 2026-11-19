import json
import urllib.parse

import applications
import application_destination


def reconcile_requisition(database, job_key, requisition, evidence, now):
    job = database.execute("SELECT * FROM application_jobs WHERE job_key=?", (job_key,)).fetchone()
    if not job:
        raise ValueError("Existing canonical application task required")
    requisition = str(requisition).strip().casefold()
    if not requisition or requisition == "*" or applications.weak_identity(requisition):
        raise ValueError("Exact canonical requisition identity required")
    payload = json.loads(job["payload"])
    parsed = urllib.parse.urlsplit(payload["url"])
    browser_identity = evidence.get("official_url") is not None
    if browser_identity:
        application_destination.validate_identity(
            evidence, dict(job, candidate_email=evidence.get("candidate_email")), now, database)
        # No quote evidence is required any more (owner, 2026-09-27, item 1).
        if not evidence.get("candidate_email") or not evidence.get("source", "").startswith("application-browser:"):
            raise ValueError("Fresh source-bound browser identity required")
    # `application_jobs.employer` is `applications.normalized(role["company"])` by construction
    # (`applications.identity`), so only the observed side needs folding; `employer_key` is
    # `application_destination`'s own helper, so this file and `validate_identity` agree on one
    # definition. A non-`str` employer yields `None`, which never equals a stored key, so it stays
    # a `ValueError` rather than a `TypeError` out of `unicodedata.normalize` — `apply_resolution`
    # rolls back on the exception and must not see an unexpected type escape.
    #
    # `role_accepted` is `application_destination`'s reconciliation of a page-rendered title against
    # the stored research title: identical words in identical order once punctuation is stripped,
    # plus the job's requisition, plus the title actually quoted from a successful read of this
    # posting (or a sub-resource of it). Anything looser is refused there, not here. It is evaluated
    # last so a packet failing any other binding never reaches it, and without a database because the
    # `ROLE_TITLE_RECONCILED` record belongs to the `validate_identity` call above: role
    # reconciliation requires `official_url`, and `official_url` is exactly what makes this the
    # browser path, so that call always runs first and records it once.
    if (not applications.fresh(evidence, now) or evidence.get("independent_read") is not True
            # Reconciled, not raw (owner, 2026-09-21). ED gave validate_identity,
            # submission_destination and assess the employer_accepted path and missed THIS
            # site, so Mooby, Inc.'s resolution passed every other gate and was still thrown
            # out here with "Fresh independent official posting identity evidence required" -
            # the employer_key fold turns the page's legal name into "mooby, inc." against a
            # tracker holding "mooby". A fix at one layer is not the fix.
            # No `database` (owner, 2026-09-22). `validate_identity` above already ran
            # `employer_accepted` with the database and recorded EMPLOYER_NAME_RECONCILED for
            # this exact evidence; passing it again wrote a byte-identical second row at the
            # same timestamp. Four such pairs are in the ledger (mooby x2, lucerne energy x2). This
            # is the same de-duplication `role_accepted` below already applies, for the same
            # reason, five lines down.
            or not application_destination.employer_accepted(evidence, job)
            or evidence.get("job_key", job_key) != job_key
            or not browser_identity and evidence.get("source") != payload["url"]
            # `requisition` was casefolded on entry (line 12) but this compared it against the
            # RAW evidence value, so a page reading "R56" never matched its own normalized form
            # and the correction could not be recorded even once validate_identity accepted it
            # (owner, 2026-09-21). Both sides are normalized now.
            or str(evidence.get("requisition") or "").strip().casefold() != requisition
            or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or evidence.get("identity_basis") != "official_canonical_posting_id"
            or not application_destination.role_accepted(evidence, job, payload)):
        raise ValueError("Fresh independent official posting identity evidence required")
    if job["requisition"] == requisition:
        return {"state": "ALREADY_RECONCILED", "job_key": job_key}
    if not applications.weak_identity(job["requisition"]):
        raise ValueError("Do not replace a known requisition with a different role")
    # This demanded "independent outcome reconciliation" from any job with attempts>0 or ANY
    # row in application_attempts - including rows already reconciled to
    # CONFIRMED_NOT_SUBMITTED, which ARE that reconciliation. Pawnee Health had eight such
    # rows, every one confirmed not submitted, and was still refused and then abandoned
    # (2026-09-22T05:42:15Z). A job that has tried and provably not submitted is exactly the
    # job whose identity most needs correcting. application_recovery.outstanding is the
    # precise question - an UNCERTAIN state, SUBMISSION_OUTCOME_UNKNOWN, a live SUBMITTING
    # row, or an unconsumed submit request - and it is what actually protects a real
    # in-flight submission (owner, 2026-09-21).
    import application_recovery
    if job["state"] not in {"QUEUED", "BLOCKED"} or application_recovery.outstanding(database, job):
        raise ValueError("An outstanding submission requires independent outcome reconciliation first")
    new_role = dict(payload, requisition_id=requisition)
    resolved_key = applications.identity(new_role)[2]
    # Another job holding this posting (owner's picks D1/D2, 2026-11-18; a CoreWeave listing with no id
    # resolved to the posting a job the user had rejected already held, and this used to refuse every
    # resolution until the investigations ran out): a holder that applied, may have, or is open raises
    # `PostingOwned` (the caller closes this job as its copy); holders closed without applying, including
    # jobs the user rejected or abandoned, hand the posting over, as `rebind_listing_link` does.
    # The page's own URL identity counts only on the browser path, where it is registered to this job below.
    page = dict(new_role, url=evidence["official_url"]) if browser_identity else new_role
    target = application_destination.public_url(page["url"]) if browser_identity else None
    url_key = applications.identity(dict(page, requisition_id=""))[2] if browser_identity else None
    source_key = applications.posting_source_key(page) if browser_identity else None
    claim_posting(database, job_key, job["employer"], requisition, target, resolved_key, url_key, source_key)
    database.execute("SAVEPOINT requisition_identity")
    try:
        released = claim_posting(database, job_key, job["employer"], requisition, target, resolved_key, url_key, source_key)
        moved = take_over_keys(database, job_key, released, (resolved_key, url_key), source_key, evidence)
        database.execute("INSERT OR IGNORE INTO application_aliases VALUES (?,?,?)",
                         (resolved_key, job_key, json.dumps(evidence)))
        alias = database.execute("SELECT canonical_key FROM application_aliases WHERE alias_key=?", (resolved_key,)).fetchone()
        if not alias or alias[0] != job_key:
            raise ValueError("Identity collision during reconciliation; no task is rebound")
        claim_posting(database, job_key, job["employer"], requisition, target, resolved_key, url_key, source_key)
        # attempts is compared to the value READ, not to 0, and the NOT EXISTS is scoped to
        # SUBMITTING rather than to ANY attempt row. Both clauses are optimistic concurrency,
        # not a second copy of the "never reconcile an attempted job" rule DS removed above:
        # an attempt row inserted AFTER the guard ran does not change application_jobs.attempts,
        # so without the SUBMITTING clause a reconciliation could commit while a real
        # submission was starting.
        changed = database.execute("""UPDATE application_jobs SET requisition=?,payload=?,next_at=NULL,updated_at=?
            WHERE job_key=? AND requisition=? AND payload=? AND state=? AND attempts=?
            AND NOT EXISTS (SELECT 1 FROM application_attempts WHERE job_key=? AND state='SUBMITTING')""",
                                   (requisition, json.dumps(new_role), now, job_key, job["requisition"], job["payload"], job["state"], job["attempts"], job_key))
        if changed.rowcount != 1:
            raise ValueError("Application changed during identity reconciliation")
        # The verified official page's own URL identity belongs to this job too (owner,
        # 2026-09-30), even when the resolution names no destination change, so a later copy
        # found by that URL resolves here instead of becoming a second application.
        if browser_identity and url_key in released:
            # That key is a released job's own key: moved above, so `add_alias` (which refuses it) is skipped.
            if source_key:
                database.execute("INSERT OR IGNORE INTO application_source_keys VALUES (?,?)", (source_key, job_key))
        elif browser_identity:
            applications.register_posting_keys(database, page, job_key,
                                               {"source": "REQUISITION_RECONCILED", "url": evidence["official_url"]})
        record_takeover(database, job_key, released, moved, target, requisition, now)
        applications.event(database, job_key, "REQUISITION_RECONCILED",
                           {"previous_requisition": job["requisition"], "requisition": requisition, "evidence": evidence,
                            "released_copies": released, "eligibility_and_history_clearance": False}, now)
        database.execute("RELEASE SAVEPOINT requisition_identity")
    except BaseException:
        database.execute("ROLLBACK TO SAVEPOINT requisition_identity")
        database.execute("RELEASE SAVEPOINT requisition_identity")
        raise
    return {"state": "IDENTITY_RECONCILED_GATES_UNCHANGED", "job_key": job_key, "requisition": requisition}


class PostingOwned(ValueError):
    """The destination posting is another job's, so the re-record is refused and this job is a copy of
    it (owner's pick D1, 2026-11-18). `owner` names that job; `applied` is True when it submitted or may
    have, False when it is still open and has not (it carries the application)."""

    def __init__(self, owner, applied):
        super().__init__("Posting already recorded under another job")
        self.owner = owner
        self.applied = applied


LISTING_LINK_EVENT = "LISTING_LINK_RECONCILED"
LISTING_LINK_TAKEOVER_EVENT = "LISTING_LINK_POSTING_TAKEN_OVER"
# What another job that holds the destination is, for `rebind_listing_link`.
APPLIED, OPEN, RELEASED = "APPLIED", "OPEN", "RELEASED"


def may_have_applied(database, row):
    """True when this job submitted or anything on record may have reached the employer."""
    return (row["state"] == "SUBMITTED" or bool(row["receipt"])
            or applications.uncertain_submission(database, row["job_key"])
            or bool(database.execute("SELECT 1 FROM application_attempts WHERE job_key=? "
                                     "AND state<>'CONFIRMED_NOT_SUBMITTED' LIMIT 1", (row["job_key"],)).fetchone()))


def _same_as(database, key):
    row = database.execute("SELECT detail FROM application_events WHERE job_key=? AND event='SAME_POSTING_LINKED' "
                           "ORDER BY id DESC LIMIT 1", (key,)).fetchone()
    try:
        same_as = json.loads(row[0]).get("same_as") if row else None
    except (TypeError, ValueError, AttributeError):
        return None
    return same_as if isinstance(same_as, str) else None


def holder_status(database, key, job_key, seen=None):
    """(status, carrier) for another job `key` that holds the destination of `job_key`'s re-record:
    APPLIED (it, or the job it is a copy of, submitted or may have: `carrier` is that job), OPEN (not
    final and nothing sent) or RELEASED (closed without applying: INELIGIBLE, ABANDONED, EXHAUSTED, or a
    DUPLICATE copy of `job_key` itself or of a released job). A DUPLICATE with no recorded link (the
    history gate's: an application already on record) counts as APPLIED, so nothing is sent twice."""
    seen = set(seen or ()) | {key}
    row = database.execute("SELECT * FROM application_jobs WHERE job_key=?", (key,)).fetchone()
    if not row:
        return RELEASED, key
    if may_have_applied(database, row):
        return APPLIED, key
    if row["state"] not in applications.FINAL:
        return OPEN, key
    if row["state"] != "DUPLICATE":
        return RELEASED, key
    same_as = _same_as(database, key)
    if same_as is None:
        return APPLIED, key
    if same_as == job_key:
        return RELEASED, key
    if same_as in seen:
        return APPLIED, key   # a loop of copies: never treated as free
    return holder_status(database, same_as, job_key, seen)


def _listing_address(url):
    """A listing URL's scheme, host and path (trailing slash ignored), after `public_url`: a tracking query
    or a fragment on the URL the read reports never makes it another listing (FU11 review I4)."""
    parsed = urllib.parse.urlsplit(application_destination.public_url(url))
    return parsed.scheme, parsed.hostname, parsed.path.rstrip("/")


def posting_holders(database, job_key, employer, requisition, target, resolved_key, url_key, source_key):
    """Every other job that holds a posting, each once: the same employer and id (`requisition` None skips
    this), the owner of its id alias or URL alias, a job whose own key is one of those, the owner of its
    source key, and a job whose own posting address is `target`, whatever its id or employer spelling (FU11
    review I2: a structured URL compared by exact equality after `public_url`; the destination a job was
    moved to is its payload URL, so this covers it)."""
    keys = [row[0] for row in database.execute(
        "SELECT job_key FROM application_jobs WHERE employer=? AND requisition=? AND job_key<>?",
        (employer, requisition, job_key))] if requisition else []
    for alias_key in (resolved_key, url_key):
        if not alias_key:
            continue
        row = database.execute("SELECT canonical_key FROM application_aliases WHERE alias_key=?", (alias_key,)).fetchone()
        keys += [row[0]] if row else []
        keys += [alias_key] if database.execute("SELECT 1 FROM application_jobs WHERE job_key=?", (alias_key,)).fetchone() else []
    if source_key:
        row = database.execute("SELECT job_key FROM application_source_keys WHERE source_key=?", (source_key,)).fetchone()
        keys += [row[0]] if row else []
    if target:
        for key, raw_payload in database.execute("SELECT job_key,payload FROM application_jobs WHERE job_key<>?", (job_key,)):
            try:
                if application_destination.public_url(json.loads(raw_payload).get("url")) == target:
                    keys.append(key)
            except (TypeError, ValueError, AttributeError):
                continue
    return [key for key in dict.fromkeys(keys) if key != job_key]


def claim_posting(database, job_key, employer, requisition, target, resolved_key, url_key, source_key):
    """The one rule for a job re-recorded onto a posting other jobs may hold (owner's picks D1/D2,
    2026-11-18). Raises `PostingOwned` when a holder applied or may have (applied=True, checked first so
    the words name the job that may have reached the employer) or is open (applied=False): either way this
    job is a copy of it. Returns the holders closed without applying (`holder_status` RELEASED, including
    jobs you rejected or abandoned): this job takes their posting over."""
    statuses = [(key,) + holder_status(database, key, job_key)
                for key in posting_holders(database, job_key, employer, requisition, target, resolved_key, url_key, source_key)]
    for key, status, carrier in statuses:
        if status == APPLIED:
            raise PostingOwned(carrier, True)
    for key, status, carrier in statuses:
        if status == OPEN:
            raise PostingOwned(carrier, False)
    return [key for key, _, _ in statuses]


def take_over_keys(database, job_key, released, alias_keys, source_key, evidence):
    """Move a posting's alias and source keys from jobs closed without applying to `job_key`, never
    silently: the caller records each move on both jobs (`record_takeover`). `add_alias` refuses to alias a
    key that is an existing job's own key ("explicit ledger reconciliation"); this is that reconciliation,
    so the rows are written here directly. Returns what moved."""
    moved = []
    for alias_key in alias_keys:
        if not alias_key:
            continue
        row = database.execute("SELECT canonical_key FROM application_aliases WHERE alias_key=?", (alias_key,)).fetchone()
        if row and row[0] in released:
            database.execute("UPDATE application_aliases SET canonical_key=?,evidence=? WHERE alias_key=?",
                             (job_key, json.dumps(evidence), alias_key))
            moved.append({"alias_key": alias_key, "from": row[0]})
        elif not row and alias_key in released:
            database.execute("INSERT INTO application_aliases VALUES (?,?,?)", (alias_key, job_key, json.dumps(evidence)))
            moved.append({"alias_key": alias_key, "from": alias_key})
    if source_key:
        row = database.execute("SELECT job_key FROM application_source_keys WHERE source_key=?", (source_key,)).fetchone()
        if row and row[0] in released:
            database.execute("UPDATE application_source_keys SET job_key=? WHERE source_key=?", (job_key, source_key))
            moved.append({"source_key": source_key, "from": row[0]})
    return moved


def record_takeover(database, job_key, released, moved, target, requisition, now):
    """LISTING_LINK_POSTING_TAKEN_OVER on both jobs for each released holder, once per pair and decision: a
    recovery resolution re-records the id and then the address at the same moment, and the second step
    finds the same holder. A later takeover (another decision, another moment) is recorded again (H3)."""
    for key in released:
        mine = [item for item in moved if item["from"] == key or item.get("alias_key") == key]
        if not mine and any(json.loads(row[0]).get("from") == key for row in database.execute(
                "SELECT detail FROM application_events WHERE job_key=? AND event=? AND at=?",
                (job_key, LISTING_LINK_TAKEOVER_EVENT, now))):
            continue
        detail = {"from": key, "to": job_key, "url": target, "requisition": requisition, "moved": mine}
        applications.event(database, job_key, LISTING_LINK_TAKEOVER_EVENT, detail, now)
        applications.event(database, key, LISTING_LINK_TAKEOVER_EVENT, detail, now)


def taken_over_by(database, job_key):
    """The job that took this job's posting over and still holds it (applied, may have, or open), else
    None. Try again on this job is refused while there is one (review H1, 2026-11-18): both would be open
    on one posting. Read from the latest LISTING_LINK_POSTING_TAKEN_OVER that names this job as `from`."""
    for (raw,) in database.execute("SELECT detail FROM application_events WHERE job_key=? AND event=? ORDER BY id DESC",
                                   (job_key, LISTING_LINK_TAKEOVER_EVENT)):
        try:
            detail = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(detail, dict) and detail.get("from") == job_key and isinstance(detail.get("to"), str):
            status, carrier = holder_status(database, detail["to"], job_key)
            return None if status == RELEASED else carrier
    return None


def rebind_listing_link(database, job_key, verdict, now):
    """Re-record a REAL-id job under the posting its listing's own Apply link leads to (owner, 2026-11-17,
    FU11: Y Combinator lists Beam's role as CnrHNaM, its Apply link opens Work at a Startup posting 98389).

    `verdict` is the inspect read's blocked LISTING_LINK_IDENTITY object, structured values only:
    destination_url, destination_requisition, reached_from, employer_matches_expected and
    role_matches_expected. Honoured only when reached_from is the job's own URL (the listing on
    file; query and fragment aside), both judgments are exactly True, the destination is another page
    with a different real id, and no submission is outstanding. The job keeps its key, history, events
    and answers; its requisition and URL become the destination's, and the destination's identity key
    becomes an alias of this job (as `reconcile_requisition` does for a placeholder job), so a later
    copy of either posting resolves here.

    Another job holds the destination when it has the same employer and id, owns its alias or source
    key, or has its URL as its own posting address (exact equality after `public_url`, whatever its id
    or employer spelling). Raises `PostingOwned` when such a job applied or may have (applied=True) or
    is open (applied=False). Jobs closed without applying (`holder_status` RELEASED) do not hold it:
    they stay closed, and the destination's alias and source key move to this job, recorded with
    LISTING_LINK_POSTING_TAKEN_OVER on both. Any other refusal is a plain ValueError and changes
    nothing. The job is queued for its next read at the new URL."""
    job = database.execute("SELECT * FROM application_jobs WHERE job_key=?", (job_key,)).fetchone()
    if not job:
        raise ValueError("Existing canonical application task required")
    if applications.weak_identity(job["requisition"]):
        raise ValueError("A placeholder job is re-recorded through IDENTITY_MISMATCH, not a listing link")
    if not isinstance(verdict, dict):
        raise ValueError("Structured listing-link verdict required")
    if verdict.get("employer_matches_expected") is not True or verdict.get("role_matches_expected") is not True:
        raise ValueError("The read did not judge the destination to be this job's employer and role")
    payload = json.loads(job["payload"])
    listing = application_destination.public_url(payload["url"])
    if _listing_address(verdict.get("reached_from")) != _listing_address(listing):
        raise ValueError("The destination was not reached from the listing on file")
    target = application_destination.public_url(verdict.get("destination_url"))
    if target == listing:
        raise ValueError("The destination is the listing itself")
    raw = verdict.get("destination_requisition")
    requisition = raw.strip().casefold() if isinstance(raw, str) else ""
    if not requisition or requisition == "*" or applications.weak_identity(requisition):
        raise ValueError("Exact destination requisition identity required")
    if requisition == job["requisition"]:
        raise ValueError("The destination shows this job's own id; no re-record is needed")
    import application_recovery
    if job["state"] not in {"QUEUED", "BLOCKED", "RETRY_WAIT"} or application_recovery.outstanding(database, job):
        raise ValueError("An outstanding submission requires independent outcome reconciliation first")
    new_role = dict(payload, requisition_id=requisition, url=target)
    resolved_key = applications.identity(new_role)[2]
    url_key = applications.identity(dict(new_role, requisition_id=""))[2]
    source_key = applications.posting_source_key(new_role)
    evidence = {"source": LISTING_LINK_EVENT, "url": target, "reached_from": listing, "verdict": verdict, "observed_at": now}

    claim_posting(database, job_key, job["employer"], requisition, target, resolved_key, url_key, source_key)
    database.execute("SAVEPOINT listing_link_identity")
    try:
        released = claim_posting(database, job_key, job["employer"], requisition, target, resolved_key, url_key, source_key)
        moved = take_over_keys(database, job_key, released, (resolved_key, url_key), source_key, evidence)
        database.execute("INSERT OR IGNORE INTO application_aliases VALUES (?,?,?)",
                         (resolved_key, job_key, json.dumps(evidence)))
        claim_posting(database, job_key, job["employer"], requisition, target, resolved_key, url_key, source_key)
        # Optimistic concurrency against the values read, as in reconcile_requisition.
        changed = database.execute("""UPDATE application_jobs SET requisition=?,payload=?,state='QUEUED',blocker=NULL,
            next_action=NULL,next_at=NULL,updated_at=?
            WHERE job_key=? AND requisition=? AND payload=? AND state=? AND attempts=?
            AND NOT EXISTS (SELECT 1 FROM application_attempts WHERE job_key=? AND state='SUBMITTING')""",
                                   (requisition, json.dumps(new_role), now, job_key, job["requisition"], job["payload"],
                                    job["state"], job["attempts"], job_key))
        if changed.rowcount != 1:
            raise ValueError("Application changed during identity reconciliation")
        database.execute("DELETE FROM application_blockers WHERE job_key=?", (job_key,))
        # The destination's own URL identity belongs to this job too (owner, 2026-09-30), as for a
        # re-recorded placeholder job.
        if url_key in released:
            if source_key:
                database.execute("INSERT OR IGNORE INTO application_source_keys VALUES (?,?)", (source_key, job_key))
        else:
            applications.register_posting_keys(database, new_role, job_key, evidence)
        record_takeover(database, job_key, released, moved, target, requisition, now)
        applications.event(database, job_key, LISTING_LINK_EVENT,
                           {"previous_requisition": job["requisition"], "requisition": requisition,
                            "previous_url": payload["url"], "url": target, "alias_key": resolved_key,
                            "released_copies": released, "verdict": verdict,
                            "eligibility_and_history_clearance": False}, now)
        database.execute("RELEASE SAVEPOINT listing_link_identity")
    except BaseException:
        database.execute("ROLLBACK TO SAVEPOINT listing_link_identity")
        database.execute("RELEASE SAVEPOINT listing_link_identity")
        raise
    return {"state": "LISTING_LINK_RECONCILED", "job_key": job_key, "requisition": requisition, "url": target}


def reconcile_destination(database, job_key, url, evidence, now):
    job = database.execute("SELECT * FROM application_jobs WHERE job_key=?", (job_key,)).fetchone()
    if not job:
        raise ValueError("Existing canonical application task required")
    # No quote evidence is required any more (owner, 2026-09-27, item 1).
    if not evidence.get("candidate_email") or not evidence.get("source", "").startswith("application-browser:"):
        raise ValueError("Fresh source-bound browser destination required")
    target = application_destination.validate_identity(evidence, dict(job, candidate_email=evidence["candidate_email"]), now)
    # The requisition equality that used to sit on this line was UNSATISFIABLE for exactly
    # the jobs that need a destination correction (owner, 2026-09-21). validate_identity
    # above already enforces the right rule, and enforces it more precisely: evidence may
    # never carry an 'unresolved:' requisition, and when the JOB's requisition is resolved
    # the evidence must equal it, while an unresolved job is deliberately allowed to be
    # given a different, resolved one (application_destination.py:182-183). Demanding
    # job["requisition"] == evidence["requisition"] here contradicted the line above it: an
    # unresolved job's requisition starts with 'unresolved:' and the evidence's never can,
    # so the two could never be equal and the correction could never be recorded.
    if target != application_destination.public_url(url):
        raise ValueError("Exact canonical destination identity required")
    # Deleted (owner, 2026-09-27 item 2 and 2026-09-30): no destination is refused for an
    # earlier browser refusal; the browser platform alone enforces its own rules.
    payload = json.loads(job["payload"])
    if application_destination.public_url(payload["url"]) == target:
        return {"state": "ALREADY_RECONCILED", "job_key": job_key}
    # Same narrowing as in reconcile_requisition above: a reconciled attempt row is proof the
    # outcome IS known, not a reason to refuse the correction (owner, 2026-09-21).
    import application_recovery
    if job["state"] not in {"QUEUED", "BLOCKED"} or application_recovery.outstanding(database, job):
        raise ValueError("An outstanding submission requires independent outcome reconciliation first")
    updated = dict(payload, url=target)
    # The same rule as `reconcile_requisition` (owner's picks D1/D2, 2026-11-18), by the page's address only.
    url_key = applications.identity(dict(updated, requisition_id=""))[2]
    source_key = applications.posting_source_key(updated)
    claim_posting(database, job_key, job["employer"], None, target, None, url_key, source_key)
    database.execute("SAVEPOINT application_destination_identity")
    try:
        released = claim_posting(database, job_key, job["employer"], None, target, None, url_key, source_key)
        moved = take_over_keys(database, job_key, released, (url_key,), source_key, evidence)
        # Optimistic concurrency against the values read, as in reconcile_requisition above.
        changed = database.execute("""UPDATE application_jobs SET payload=?,next_at=NULL,updated_at=?
            WHERE job_key=? AND payload=? AND requisition=? AND state=? AND attempts=?
            AND NOT EXISTS (SELECT 1 FROM application_attempts WHERE job_key=? AND state='SUBMITTING')""",
            (json.dumps(updated), now, job_key, job["payload"], job["requisition"], job["state"], job["attempts"], job_key))
        if changed.rowcount != 1:
            raise ValueError("Application changed during destination reconciliation")
        # The new page's own URL identity now belongs to this job (owner, 2026-09-30), so a
        # later copy of the posting found by that URL is this job, not a second application.
        if url_key in released:
            if source_key:
                database.execute("INSERT OR IGNORE INTO application_source_keys VALUES (?,?)", (source_key, job_key))
        else:
            applications.register_posting_keys(database, updated, job_key, {"source": "DESTINATION_RECONCILED",
                                                                             "url": target})
        record_takeover(database, job_key, released, moved, target, job["requisition"], now)
        applications.event(database, job_key, "DESTINATION_RECONCILED", {
            "previous_url": payload["url"], "url": target, "evidence": evidence,
            "released_copies": released, "eligibility_and_history_clearance": False}, now)
        database.execute("RELEASE SAVEPOINT application_destination_identity")
    except BaseException:
        database.execute("ROLLBACK TO SAVEPOINT application_destination_identity")
        database.execute("RELEASE SAVEPOINT application_destination_identity")
        raise
    return {"state": "DESTINATION_RECONCILED_GATES_UNCHANGED", "job_key": job_key, "url": target}
