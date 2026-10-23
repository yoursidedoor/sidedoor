import ipaddress
import json
import re
import urllib.parse

import applications


def public_url(value):
    if not isinstance(value, str) or not value or any(character.isspace() for character in value):
        raise ValueError("UNSUPPORTED_OFFICIAL_DESTINATION")
    decoded = urllib.parse.unquote(value)
    if "aiapply" in decoded.casefold() or "\\" in decoded or any(ord(character) < 32 for character in decoded):
        raise ValueError("UNSUPPORTED_OFFICIAL_DESTINATION")
    try:
        parsed = urllib.parse.urlsplit(value)
        hostname = (parsed.hostname or "").rstrip(".").lower()
        if (parsed.scheme != "https" or not hostname or parsed.username is not None
                or parsed.password is not None or parsed.port not in {None, 443}
                or "." not in hostname or "%" in hostname
                or hostname.endswith((".localhost", ".local", ".internal", ".home.arpa"))):
            raise ValueError("UNSUPPORTED_OFFICIAL_DESTINATION")
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            if re.fullmatch(r"[0-9.xXa-fA-F]+", hostname) or hostname == "localhost":
                raise ValueError("UNSUPPORTED_OFFICIAL_DESTINATION")
        else:
            if not address.is_global:
                raise ValueError("UNSUPPORTED_OFFICIAL_DESTINATION")
        for name, _ in urllib.parse.parse_qsl(parsed.query):
            if re.search(r"password|passwd|secret|token|credential|api.?key|authorization|signature", name, re.I):
                raise ValueError("UNSUPPORTED_OFFICIAL_DESTINATION")
    except (TypeError, UnicodeError, ValueError) as error:
        raise ValueError("UNSUPPORTED_OFFICIAL_DESTINATION") from error
    return urllib.parse.urlunsplit(("https", hostname, parsed.path or "/", parsed.query, parsed.fragment))


def employer_key(value):
    """The stored `application_jobs.employer` form of an observed employer name.

    `applications.identity` stores `normalized(role["company"])`, so the tracker side is
    already folded; only the incoming side needs folding. Matches `applications.assess`
    and `application_browser.context`. A non-string never compares equal to a stored key.
    """
    return applications.normalized(value) if isinstance(value, str) else None


def words(value):
    """The word sequence of a title, with punctuation and symbols removed.

    `Staff Software Engineer — Developer Platform` and
    `Staff Software Engineer, Developer Platform` share one; `Staff Software Engineer` and
    `Other Role` do not, and neither do two titles differing by any actual word or by word
    order. `applications.normalized` still supplies NFKC folding, casefolding and
    whitespace collapse, so this adds punctuation and nothing else.
    """
    return applications.normalized(re.sub(r"[^\w\s]", " ", value)).split()


def same_posting(observed, stored):
    """True when `observed` is the stored posting itself or a sub-resource of it.

    Both sides go through `public_url`, so scheme, host and credential safety are already
    enforced. A real Ashby read returns `.../<requisition>/application` for a posting stored
    as `.../<requisition>`; that is the same posting seen one level down. The parent
    direction is deliberately NOT accepted: a listing page above the stored posting carries
    other roles' titles.
    """
    try:
        target, source = public_url(stored), public_url(observed)
    except ValueError:
        return False
    if target == source:
        return True
    target, source = urllib.parse.urlsplit(target), urllib.parse.urlsplit(source)
    return (target.netloc == source.netloc and not target.query and not target.fragment
            and source.path.startswith(target.path.rstrip("/") + "/"))


def role_reconciliation(evidence, job, payload):
    """The ROLE_TITLE_RECONCILED detail when the read judged the page's role to be this job's role.

    Whether two titles name the same role is a judgement about language, so the read makes it
    (owner, 2026-10-07: "Python should not be doing any single Python comparison checks anymore
    that can stop a Job application"; "removed every single one of them and replace with AI
    instead"). The request carries the job's role; the prompt asks the session to set
    `role_matches_expected` when the page's role is that role under a different rendering, and
    Python records the reconciliation, exactly as `employer_accepted` does for employer names.
    The punctuation-only word comparison and the stored-URL condition that used to sit here are
    gone: Relecloud's "Senior Java - Telecom Developer" (page) against "Senior Java \u2013 Telecom
    Developer" (research) failed three investigations because the form lived outside the stored
    posting's path. The requisition, job key and official-page bindings in `validate_identity`
    are unchanged.
    """
    stored, observed = payload.get("role"), evidence.get("role")
    if (evidence.get("role_matches_expected") is not True or not isinstance(observed, str)
            or not observed.strip() or len(observed) > 300):
        return None
    return {"job_key": job["job_key"], "requisition": job["requisition"], "official_url": evidence.get("official_url"),
            "previous_role": stored, "role": observed, "basis": "READ_JUDGED_SAME_ROLE",
            "evidence": evidence, "eligibility_and_history_clearance": False}


def recorded_employer_name(job, observed):
    """Has a previous read already judged `observed` to name this job's employer?

    Reads `payload["employer_names"]`, written by `applications.persist_read_reconciliations`.
    Folded on both sides with `employer_key`, the same helper the exact-match test above uses,
    so this and that test share one definition of equality.
    """
    folded = employer_key(observed)
    if not folded:
        return False
    # `job` is a sqlite3.Row on the production paths but a bare dict in several callers and in
    # tests, and it need not carry `payload` at all - a missing key raises KeyError on a dict and
    # IndexError on a Row. A job with no recorded renderings simply has none, so every one of
    # these is "not recorded", never an error.
    try:
        raw = job["payload"]
    except (KeyError, IndexError, TypeError):
        return False
    try:
        payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    return folded in {employer_key(name) for name in payload.get("employer_names") or [] if isinstance(name, str)}


def employer_accepted(evidence, job, database=None, now=None):
    """Whether the evidence's employer binds to this job, reconciling a rendered name if needed.

    Deliberately the same three-step shape as `role_accepted` below - exact, then folded,
    then a recorded reconciliation - because it is the same problem one field over.

    `employer_key` folds case and whitespace, which is enough for "MOOBY" against "mooby" and
    not enough for anything else. Mooby, Inc.'s Ashby page renders the LEGAL name "Mooby, Inc."
    while the tracker stores the trade name "mooby" as it appeared on the job board, and no
    fold makes those equal (2026-09-21). Stripping "Inc./Ltd/LLC/GmbH" in Python is exactly
    the prose matching the owner has forbidden: it is a guess about language, it breaks a
    company genuinely named "Mooby Labs", and the next page will differ some other way.

    Whether two renderings name the same company is a judgement about language, so the read
    makes it: the request already carries `job.employer`, the prompt asks the session to set
    `employer_matches_expected` when the page's company is that company under a different
    rendering, and Python records the reconciliation instead of inventing the rule. A read
    that does not assert it is refused exactly as before.
    """
    observed = evidence.get("employer")
    if employer_key(observed) == job["employer"]:
        return True
    # A rendering this employer was ALREADY judged to have, read back instead of re-asked
    # (owner, 2026-09-22: "There should be no rederivation. We must absolutely reuse work.").
    # `applications.persist_read_reconciliations` writes it to `payload["employer_names"]` when a
    # read first makes the judgement. The payload is used rather than a table because this
    # function's hottest caller, `application_browser.normalize`, runs out of process with no
    # database handle - the payload is the only channel that reaches it. Before this, the event
    # was the only record and nothing read it, so "Lucerne Energy B.V." and "Mooby, Inc." were
    # re-judged on every single read and refused outright whenever the model omitted the flag.
    if recorded_employer_name(job, observed):
        evidence["employer_reconciliation"] = {"stored": job["employer"], "observed": observed,
                                               "basis": "RECORDED_EMPLOYER_NAME"}
        return True
    if evidence.get("employer_matches_expected") is not True or not isinstance(observed, str) or not observed.strip():
        return False
    detail = {"stored": job["employer"], "observed": observed, "basis": "READ_JUDGED_SAME_EMPLOYER"}
    evidence["employer_reconciliation"] = detail
    if database is not None:
        applications.event(database, job["job_key"], "EMPLOYER_NAME_RECONCILED", detail, now)
    return True


def role_accepted(evidence, job, payload, database=None, now=None):
    """Whether the evidence's role title binds to this job, reconciling a page title if needed.

    Records `ROLE_TITLE_RECONCILED` when a database is supplied, mirroring
    `identity_reconciliation`'s `REQUISITION_RECONCILED` / `DESTINATION_RECONCILED`. The
    detail is also attached to the evidence so a caller without a database still persists
    the reconciliation alongside the evidence it validated.
    """
    stored, observed = payload.get("role"), evidence.get("role")
    if observed == stored:
        return True
    if isinstance(observed, str) and isinstance(stored, str) and applications.normalized(observed) == applications.normalized(stored):
        return True
    detail = role_reconciliation(evidence, job, payload)
    if detail is None:
        return False
    evidence["role_title_reconciliation"] = {key: value for key, value in detail.items() if key != "evidence"}
    if database is not None:
        applications.event(database, job["job_key"], "ROLE_TITLE_RECONCILED", detail, now)
    return True


def validate_identity(evidence, job, now, database=None):
    role = json.loads(job["payload"])
    # Normalized here, not demanded of the read (owner, 2026-09-21). This used to require
    # `requisition == requisition.strip().casefold()` - an assertion that the value ARRIVED
    # lowercased - which the reader cannot satisfy: it must also quote the requisition
    # VERBATIM from the page, and real postings render them in caps. The Bluth posting
    # says "Req ID:  R56"; the read reported "R56" with that exact quote and was rejected
    # four times for the capital R, and initrode, oscorp security, stark and tessier bio all
    # sit on the same unresolved-identity blocker. The invariant behind the check is real -
    # both sides must be in one form, because applications.identity stores job.requisition
    # casefolded - so it is kept and simply DONE here, exactly as the sibling
    # identity_reconciliation.reconcile_requisition has always done it.
    raw = evidence.get("requisition")
    requisition = raw.strip().casefold() if isinstance(raw, str) else None
    if (not applications.fresh(evidence, now, 3600) or evidence.get("independent_read") is not True
            or evidence.get("official_destination_verified") is not True
            or evidence.get("identity_basis") != "official_canonical_posting_id"
            or evidence.get("job_key") != job["job_key"] or not employer_accepted(evidence, job, database, now)
            or evidence.get("candidate_email") != job["candidate_email"]
            or not requisition
            or requisition == "*" or applications.weak_identity(requisition)
            or not applications.weak_identity(job["requisition"]) and requisition != job["requisition"]
            or not role_accepted(evidence, job, role, database, now)):
        raise ValueError("APPLICATION_OFFICIAL_IDENTITY_REQUIRED")
    return public_url(evidence.get("official_url"))


def destination_reached(observed, target):
    """Whether a read of `observed` is a read of `target`'s own application form.

    The form frequently lives BELOW the canonical posting rather than at it: Rippling serves
    pawnee health's at `<posting>/apply?jobBoardSlug=...&jobId=...`, and a session that
    correctly navigated there to see the questions was rejected for not quoting a URL byte
    identical to the stored posting (2026-09-21). Demanding that equality asked the read for a
    URL the form page cannot have.

    The guard this preserves is that the form belongs to THIS posting: same scheme and host,
    and a path that is the posting's path or extends it. A different host, or a sibling path
    such as `.../jobs/<other-id>`, is still refused, and the caller separately requires the
    requisition, employer, role and candidate email to match.
    """
    if not observed:
        return False
    reached = public_url(observed)
    if reached == target:
        return True
    here, there = urllib.parse.urlsplit(reached), urllib.parse.urlsplit(target)
    return (here.scheme == there.scheme and here.netloc == there.netloc
            and here.path.rstrip("/").startswith(there.path.rstrip("/") + "/"))


