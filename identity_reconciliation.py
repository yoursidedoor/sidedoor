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
    collision = database.execute("SELECT job_key FROM application_jobs WHERE employer=? AND requisition=? AND job_key<>?",
                                 (job["employer"], requisition, job_key)).fetchone()
    alias = database.execute("SELECT canonical_key FROM application_aliases WHERE alias_key=?", (resolved_key,)).fetchone()
    if collision or alias and alias[0] != job_key:
        raise ValueError("Identity collision requires reconciliation, not a second submission task")
    database.execute("SAVEPOINT requisition_identity")
    try:
        database.execute("INSERT OR IGNORE INTO application_aliases VALUES (?,?,?)",
                         (resolved_key, job_key, json.dumps(evidence)))
        alias = database.execute("SELECT canonical_key FROM application_aliases WHERE alias_key=?", (resolved_key,)).fetchone()
        collision = database.execute("SELECT job_key FROM application_jobs WHERE employer=? AND requisition=? AND job_key<>?",
                                     (job["employer"], requisition, job_key)).fetchone()
        if not alias or alias[0] != job_key or collision:
            raise ValueError("Identity collision during reconciliation; no task is rebound")
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
        if browser_identity:
            applications.register_posting_keys(database, dict(new_role, url=evidence["official_url"]), job_key,
                                               {"source": "REQUISITION_RECONCILED", "url": evidence["official_url"]})
        applications.event(database, job_key, "REQUISITION_RECONCILED",
                           {"previous_requisition": job["requisition"], "requisition": requisition, "evidence": evidence,
                            "eligibility_and_history_clearance": False}, now)
        database.execute("RELEASE SAVEPOINT requisition_identity")
    except BaseException:
        database.execute("ROLLBACK TO SAVEPOINT requisition_identity")
        database.execute("RELEASE SAVEPOINT requisition_identity")
        raise
    return {"state": "IDENTITY_RECONCILED_GATES_UNCHANGED", "job_key": job_key, "requisition": requisition}


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
    database.execute("SAVEPOINT application_destination_identity")
    try:
        updated = dict(payload, url=target)
        # Optimistic concurrency against the values read, as in reconcile_requisition above.
        changed = database.execute("""UPDATE application_jobs SET payload=?,next_at=NULL,updated_at=?
            WHERE job_key=? AND payload=? AND requisition=? AND state=? AND attempts=?
            AND NOT EXISTS (SELECT 1 FROM application_attempts WHERE job_key=? AND state='SUBMITTING')""",
            (json.dumps(updated), now, job_key, job["payload"], job["requisition"], job["state"], job["attempts"], job_key))
        if changed.rowcount != 1:
            raise ValueError("Application changed during destination reconciliation")
        # The new page's own URL identity now belongs to this job (owner, 2026-09-30), so a
        # later copy of the posting found by that URL is this job, not a second application.
        applications.register_posting_keys(database, updated, job_key, {"source": "DESTINATION_RECONCILED",
                                                                         "url": target})
        applications.event(database, job_key, "DESTINATION_RECONCILED", {
            "previous_url": payload["url"], "url": target, "evidence": evidence,
            "eligibility_and_history_clearance": False}, now)
        database.execute("RELEASE SAVEPOINT application_destination_identity")
    except BaseException:
        database.execute("ROLLBACK TO SAVEPOINT application_destination_identity")
        database.execute("RELEASE SAVEPOINT application_destination_identity")
        raise
    return {"state": "DESTINATION_RECONCILED_GATES_UNCHANGED", "job_key": job_key, "url": target}
