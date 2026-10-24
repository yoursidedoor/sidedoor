import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import urllib.parse
import urllib.error
import urllib.request
import uuid
import portable
import user_profile
import log

LOG = log.get(__name__)  # through log.py's redaction even without configure (E2.T6 LG5)


# The reserved identity of the one request this transport carries that is NOT a form
# question. `applications.process` raises it when the application form itself could not
# be enumerated after its bounded self-help, so there is no question text to quote and
# none may be invented. The field id is deliberately not a form field: `applications`
# never loads a saved answer for it into the answer pool, so a reply can never become a
# submitted form answer. The dict is a module constant because `request_fact` dedupes on
# a hash of (role payload, question, policy) - a per-read payload would defeat that and
# ask the owner the same thing twice.
FORM_NOT_READABLE_FIELD = "__application_form_not_readable__"
FORM_NOT_READABLE_SEMANTIC = "application_form_not_readable"
# The source of an answer the owner gave on the dashboard's "Questions for you" that is also kept in
# profile.json for later jobs (owner, 2026-10-22, "a"; `setup_steps.save_owner_answer`). Such an
# answer is only ever applied by the answering AI, which judges whether it fits a later question
# (`applications.recorded_answer_for_ai`); Python never copies it onto another job's question.
OWNER_ANSWER_SOURCE = "owner-dashboard-answer:"
FORM_NOT_READABLE_QUESTION = {"id": FORM_NOT_READABLE_FIELD, "semantic": FORM_NOT_READABLE_SEMANTIC,
                              "answer_type": "text", "choices": []}


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def initialize(database):
    database.executescript("""
        CREATE TABLE IF NOT EXISTS user_decisions (
            id TEXT PRIMARY KEY, job_key TEXT NOT NULL, field TEXT NOT NULL,
            semantic TEXT NOT NULL, answer_type TEXT NOT NULL, choices TEXT NOT NULL,
            payload_hash TEXT NOT NULL, job_hash TEXT NOT NULL, policy_hash TEXT NOT NULL,
            expires_at INTEGER NOT NULL, state TEXT NOT NULL, summary TEXT NOT NULL,
            event_key TEXT NOT NULL, registered INTEGER NOT NULL DEFAULT 0,
            message_id INTEGER, raw_response TEXT, last_error TEXT);
        CREATE TABLE IF NOT EXISTS transport_inbox (
            seq INTEGER PRIMARY KEY, decision_id TEXT NOT NULL, payload TEXT NOT NULL,
            state TEXT NOT NULL, processed_at TEXT NOT NULL, acked INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS transport_cursor (
            id INTEGER PRIMARY KEY, seq INTEGER NOT NULL);
        INSERT OR IGNORE INTO transport_cursor VALUES (1,0);
    """)
    columns = {row[1] for row in database.execute("PRAGMA table_info(user_decisions)")}
    # `label`: the question as the form asks it (owner, 2026-10-24), shown on Approvals and kept with the
    # answer for Settings; NULL for older rows and for the operational escalation.
    for name, definition in {"kind": "TEXT NOT NULL DEFAULT 'fact'", "owner_id": "TEXT", "replaces_id": "TEXT",
                             "label": "TEXT"}.items():
        if name not in columns:
            database.execute(f"ALTER TABLE user_decisions ADD COLUMN {name} {definition}")
    database.execute("CREATE UNIQUE INDEX IF NOT EXISTS decision_replacement ON user_decisions(replaces_id) WHERE replaces_id IS NOT NULL")
    database.execute("CREATE INDEX IF NOT EXISTS user_decisions_state ON user_decisions(state, expires_at)")   # E6.T4
    database.commit()


# --- What a saved decision is actually valid against -------------------------------------
#
# `user_decisions` stores a fingerprint of the application policy and of the job payload so
# that an answer given under one set of assumptions is never applied under different ones.
# Fingerprinting the WHOLE policy document made that guard fire on every unrelated edit:
# expanding `answers` from 9 to 24 entries on 2026-09-18 invalidated four outstanding
# operational questions whose answer cannot depend on the owner's notice period or salary
# band. This is
# the same defect `parallel_research.research_policy` fixed for research checkpoints, and it
# gets the same shape of fix: hash an ALLOWLISTED PROJECTION of the document, never the
# document. A denylist is not enough (that patch's hard-won lesson): it still lets any newly
# added field rotate the hash.
#
# Unlike research there is no single projection that is right for every decision, so the
# scope is derived from the decision's own kind/field/semantic:
#
# "transport"    kind='test' - the harmless transport smoke test. Its only valid reply is the
#                literal string "transport-ok"; no policy or job field can change that.
#                Projection: {} for both policy and job.
#
# "operational"  the reserved `__application_form_not_readable__` escalation. It asks the
#                owner to change the state of their own machine - make the form load - and
#                then reply. The answer is a fact about that machine
#                at a moment in time. Nothing in profile.json can make it true or
#                false, and nothing from the policy is quoted in the message the owner sees,
#                so no policy value can be an assumption the owner answered under. The reply
#                is also not a form answer: `__application_form_not_readable__` can never
#                match a form question in `applications.resolve_answers`, and its consumer
#                (`applications.process`) re-runs the whole gate chain - eligibility, history,
#                required answers, designated resume, preapproval, the action guard - against
#                the CURRENT policy before anything is submitted. Projection: {} for the
#                policy, posting identity for the job. The `browser_access_restored:*`
#                questions that were also here are deleted with the browser permission
#                handling (owner, 2026-09-30); any legacy one still pending now fails its
#                scope check and is marked STALE with its notification cancelled.
#
# "form_answer"  every other fact request: a required application answer the owner must
#                supply. What can invalidate it is the policy deciding the same question
#                differently, so the projection is exactly what `applications.resolve_answers`
#                consults for THIS question:
#                  - the `answers` entry filed under this decision's own field key;
#                  - every `answers` entry carrying this decision's exact semantic
#                    (`resolve_answers` falls back to exact-semantic matching);
#                  - for a resume question, `resume.path` - the only value an answer to a
#                    resume question is allowed to take;
#                  - for an earliest-start question, the `earliest_start_date_rule` answer,
#                    because `applications.materialize_computed_answers` validates the owner's
#                    date against the floor that rule computes.
#                Only `semantic`, `value` and whether a `source` exists are projected:
#                `resolve_answers` requires a truthy source but never reads the prose, so
#                rewording provenance is not a change of assumptions.
#
# EXCLUDED from every projection, with reasons:
#   source, answers.*.source            provenance prose, not an assumption.
#   industries, levels, funding_stages,
#   minimum_base_salary_usd,
#   desired_base_salary_usd,
#   location_priority,
#   salary_relaxation,
#   onsite_location_exclusions          role eligibility and targeting. `applications.process`
#                                       calls `assess(...)` with the CURRENT policy on every
#                                       pass, before answers are resolved and long before any
#                                       submission, so a tightened criterion stops the
#                                       application there. Carrying it here buys no safety and
#                                       costs exactly the reported defect. (Since 2026-10-07
#                                       funding_stages and onsite_location_exclusions are judged
#                                       by the read's scope_fit verdicts, so a change to them
#                                       reaches a job at its next fresh read, not on a saved one.) `salary_relaxation`
#                                       is the same category: it is read only by
#                                       `applications.compensation_assessment`, which is read
#                                       only by `assess`. It is listed explicitly because
#                                       leaving a NEW policy key unclassified invalidates every
#                                       outstanding required-answer question the moment the key
#                                       lands - which is this very defect, re-triggered.
#                                       `tests/test_fact_request_scope.py` fails loudly, naming
#                                       the key, if a future policy key is left unclassified.
#   resume (non-resume questions)       the designated resume is hash-checked against the
#                                       current policy immediately before submission
#                                       (DESIGNATED_RESUME_MISMATCH).
#   preapproved_applications,
#   referrals_block_applications        authorization and sequencing, re-read from the current
#                                       policy on every pass.
#   max_attempts, max_reconcile_attempts,
#   retry_delays_seconds                attempt tuning; never an assumption of an answer.
#   every OTHER answers entry           an application answer to a different question. Adding
#                                       one must not invalidate an outstanding question - that
#                                       is precisely the reported defect.
# FAIL-CLOSED: a top-level policy key in none of the lists above lands in "unclassified" WITH
#   its value for a form-answer decision, so a future policy field rotates the hash until
#   somebody deliberately classifies it here. The operational and transport scopes are
#   deliberately empty instead of fail-closed: `onsite_location_exclusions` was added by the
#   very edit this fixes, and a fail-closed bucket would have cancelled the operational
#   questions all over again. The claim there is categorical rather than a list - no policy
#   field can change whether a form could be read.
#
# Consequence to keep in mind, exactly as in `parallel_research`: a genuinely new coupling
# between a policy answer and a required question must be added here, because an answer
# carrying an unrelated semantic is now ignored.
SCOPE_VERSION = "decision-scope-v1"

# Mirrors of `applications.RESUME_SEMANTICS`, `applications.EARLIEST_START_SEMANTICS` and
# `applications.EARLIEST_START_RULE_SEMANTIC`. They are copied rather than imported because
# `applications` imports this module; `tests/test_fact_request_scope.py` asserts the copies
# still equal the originals, so a change there cannot silently widen a decision's scope.
RESUME_SEMANTICS = frozenset({"resume", "resume_cv", "cv", "resume_upload", "resume_attachment", "resume_or_cv"})
EARLIEST_START_SEMANTICS = frozenset({"earliest_start_date", "earliest_available_start_date",
                                      "available_start_date", "availability_date", "start_date",
                                      "when_can_you_start"})
EARLIEST_START_RULE_SEMANTIC = "earliest_start_date_rule"

# Top-level policy keys that have been deliberately classified above. Anything else is
# unknown and therefore fail-closed for a form-answer decision.
CLASSIFIED_POLICY_KEYS = frozenset({
    "answers", "desired_base_salary_usd", "funding_stages", "industries", "levels",
    # Owner, 2026-09-22. A scope-TARGETING input, exactly like `levels` and `industries` beside
    # it: it decides which postings are worth applying to, never what the owner would answer on
    # a form, so it must not rotate an outstanding required-answer question.
    "minimum_years_experience",
    "location_priority", "max_attempts", "max_reconcile_attempts", "minimum_base_salary_usd",
    "onsite_location_exclusions", "preapproved_applications", "referrals_block_applications",
    "resume", "retry_delays_seconds", "salary_relaxation", "source",
    # E4.T2: the raw résumé upload and its saved text, read as context, never a form answer.
    "resume_raw",
    # E1.T7: scope-targeting preferences like `levels`, never a form answer.
    "countries", "target_specializations",
    # E4.T1: Setup's raw picks, a copy of what `levels` and `location_priority` already carry,
    # kept only so Setup can show them again; never a form answer.
    "setup_picks"})

# `job_hash` had the same shape of problem and gets the same treatment. A job payload is a
# research record: `assessment`, `eligibility`, `evidence`, `people`, `ranking`, `review`,
# `unknowns`, `rationale` and `source` are rewritten by reconciliation and re-reads, and none
# of them changes what the form asks or what the owner answered - a re-scrape must not
# cancel an outstanding question. What `job_hash`
# is actually for is catching the posting being REPLACED under the same job_key, so it projects
# the identity of the posting only. The decision is already bound to the job by `job_key`, and
# every summary the owner sees is built from these fields plus the job row's own
# employer/requisition columns.
JOB_IDENTITY_FIELDS = ("company", "job_key", "requisition_id", "role", "title", "url")


def decision_scope(kind, field, semantic):
    """Which validity scope this decision belongs to: transport, operational or form_answer."""
    if kind != "fact":
        return "transport"
    if field == FORM_NOT_READABLE_FIELD or semantic == FORM_NOT_READABLE_SEMANTIC:
        return "operational"
    return "form_answer"


def decision_policy(kind, field, semantic, policy):
    """The projection of profile.json this decision's answer actually depends on."""
    if decision_scope(kind, field, semantic) != "form_answer":
        return {}
    if not isinstance(policy, dict):
        return {"unclassified": {"policy": policy}}
    supplied = policy.get("answers")
    supplied = supplied if isinstance(supplied, dict) else {}
    answers = {}
    for key in sorted(supplied):
        answer = supplied[key]
        if not isinstance(answer, dict):
            if key == field:
                answers[key] = answer
            continue
        if not (key == field or answer.get("semantic") == semantic
                or (semantic in EARLIEST_START_SEMANTICS
                    and answer.get("semantic") == EARLIEST_START_RULE_SEMANTIC)):
            continue
        answers[key] = {"semantic": answer.get("semantic"), "value": answer.get("value"),
                        "sourced": bool(answer.get("source"))}
    projection = {"answers": answers,
                  "unclassified": {key: value for key, value in policy.items()
                                   if key not in CLASSIFIED_POLICY_KEYS}}
    if semantic in RESUME_SEMANTICS:
        projection["resume_path"] = (policy.get("resume") or {}).get("path")
    return projection


def decision_job(kind, field, semantic, payload):
    """The projection of the job payload this decision's answer actually depends on."""
    if decision_scope(kind, field, semantic) == "transport":
        return {}
    if not isinstance(payload, dict):
        return {"payload": payload}
    return {key: payload[key] for key in JOB_IDENTITY_FIELDS if key in payload}


def scoped_policy_hash(kind, field, semantic, policy):
    return digest([SCOPE_VERSION, decision_scope(kind, field, semantic),
                   decision_policy(kind, field, semantic, policy)])


def scoped_job_hash(kind, field, semantic, payload):
    return digest([SCOPE_VERSION, decision_scope(kind, field, semantic),
                   decision_job(kind, field, semantic, payload)])


def row_policy_hash(decision, policy):
    return scoped_policy_hash(decision["kind"], decision["field"], decision["semantic"], policy)


def row_job_hash(decision, payload):
    return scoped_job_hash(decision["kind"], decision["field"], decision["semantic"], payload)


def unreadable_form_summary(job, expires_at):
    """What the owner is told when the form itself could not be read.

    Every concrete detail here comes from the job record the orchestrator already has -
    employer, requisition, the role title and url in the stored payload. No question text
    is quoted, because none was ever readable; the message says exactly that instead of
    naming a field the form may not even ask for. `reissue_fact` rebuilds this text from
    the same job row, so a correction reads identically.
    """
    role = json.loads(job["payload"])
    deadline = dt.datetime.fromtimestamp(expires_at / 1000, dt.timezone.utc).isoformat()
    title = str(role.get("role") or role.get("title") or "").strip()
    return (f"{job['employer']} / {job['requisition']}: I cannot read the complete application form"
            + (f" for {title}" if title else "") + f" at {role.get('url')}. "
            "Repeated complete read attempts returned a page without the full list of required questions, "
            "so I do not know what the form asks. I have not guessed or invented any answer and nothing has "
            "been submitted. It may need an authenticated session in the existing browser profile, or the page "
            "may not be readable there. What you can do: make the form readable - sign in to the employer's "
            "application site in that browser profile, or open the posting and check that the form loads - then "
            "reply to this exact message with any text and I will read the form again on the next cycle. If it "
            "cannot be made readable, do nothing: this application stays held and unsubmitted with its evidence. "
            f"Application attempts: {job['attempts']}. Owner: user. Request expires at: {deadline}. "
            "This is the response deadline, not a verified employer closing date. "
            "No application will be sent until all gates pass.")


def supersede_fact(database, job_key, field, reason, now):
    """Retire an outstanding request for `field` because its cause no longer holds.

    Mirrors the `SUPERSEDED_BY_FACT_REQUEST` notification rule at the required-answer
    hold: an obsolete request is replaced, never left beside its successor. Only PENDING
    rows for this exact job and field move, so an answered, expired or unrelated decision
    - including another job's - is untouched, and a late reply to a superseded request is
    still recorded by `apply_event` as REJECTED_STALE_OR_MISMATCH rather than applied.
    """
    superseded = [row[0] for row in database.execute(
        "SELECT event_key FROM user_decisions WHERE job_key=? AND field=? AND state='PENDING'", (job_key, field))]
    if not superseded:
        return []
    database.execute("UPDATE user_decisions SET state='SUPERSEDED',last_error=? WHERE job_key=? AND field=? AND state='PENDING'",
                     (reason, job_key, field))
    database.executemany("UPDATE notification_outbox SET state='CANCELLED',updated_at=? WHERE event_key=? AND state IN ('PENDING','WAITING_TRANSPORT','WAITING_OWNER')",
                         [(now, event_key) for event_key in superseded])
    return superseded


def fact_summary(job, question, expires_at):
    if question["id"] == FORM_NOT_READABLE_FIELD:
        return unreadable_form_summary(job, expires_at)
    deadline = dt.datetime.fromtimestamp(expires_at / 1000, dt.timezone.utc).isoformat()
    answer_type = question.get("answer_type", "text")
    required = "yes or no" if answer_type == "boolean" else "the factual answer"
    if answer_type == "choice":
        required = "exactly one of: " + encoded(question.get("choices", []))
    return (f"{job['employer']} / {job['requisition']}: required answer for {question.get('label', question['id'])}. "
            f"Application attempts: {job['attempts']}. Owner: user. "
            f"Reply to this exact message with {required}. Request expires at: {deadline}. "
            "This is the response deadline, not a verified employer closing date. "
            "No application will be sent until all gates pass.")


def reissue_fact(database, decision, now):
    if decision["kind"] != "fact":
        return None
    existing = database.execute("SELECT id FROM user_decisions WHERE replaces_id=?", (decision["id"],)).fetchone()
    if existing:
        return existing[0]
    if decision["expires_at"] <= int(dt.datetime.fromisoformat(now).timestamp() * 1000):
        return None
    identifier = uuid.uuid4().hex
    label = decision["label"] if "label" in decision.keys() else None
    question = {"id": decision["field"], "answer_type": decision["answer_type"], "choices": json.loads(decision["choices"]),
                **({"label": label} if label else {})}
    job = database.execute("SELECT * FROM application_jobs WHERE job_key=?", (decision["job_key"],)).fetchone()
    if not job or job["state"] != "BLOCKED" or row_job_hash(decision, json.loads(job["payload"])) != decision["job_hash"]:
        return None
    summary = ("Correction needed: the previous reply was not a valid factual answer. "
               + fact_summary(job, question, decision["expires_at"]))
    database.execute("""INSERT INTO user_decisions(
        id,job_key,field,semantic,answer_type,choices,payload_hash,job_hash,policy_hash,
        expires_at,state,summary,event_key,kind,owner_id,replaces_id,label)
        VALUES (?,?,?,?,?,?,?,?,?,?,'PENDING',?,?,'fact',?,?,?)""",
        (identifier, decision["job_key"], decision["field"], decision["semantic"], decision["answer_type"],
         decision["choices"], decision["payload_hash"], decision["job_hash"], decision["policy_hash"],
         decision["expires_at"], summary, "decision:" + identifier, decision["owner_id"], decision["id"], label))
    database.execute("INSERT INTO notification_outbox(event_key,message,state,updated_at) VALUES (?,?,'WAITING_TRANSPORT',?)",
                     ("decision:" + identifier, summary, now))
    return identifier


def request_fact(database, job, question, policy, now):
    # The dedupe payload is scoped for the same reason the stored hashes are: it is what stops
    # the owner being asked the same thing twice, and an unrelated policy edit or a re-scraped
    # rationale must not turn an outstanding question into a "new" one.
    field, semantic = question["id"], question.get("semantic")
    role = json.loads(job["payload"])
    job_hash = scoped_job_hash("fact", field, semantic, role)
    policy_hash = scoped_policy_hash("fact", field, semantic, policy)
    payload = {"job": decision_job("fact", field, semantic, role), "question": question,
               "policy": decision_policy("fact", field, semantic, policy)}
    payload_hash = digest(payload)
    milliseconds = int(dt.datetime.fromisoformat(now).timestamp() * 1000)
    existing = database.execute("SELECT id FROM user_decisions WHERE job_key=? AND field=? AND payload_hash=? AND state='PENDING' AND expires_at>?", (job["job_key"], question["id"], payload_hash, milliseconds)).fetchone()
    if existing:
        return existing[0]
    identifier = uuid.uuid4().hex
    expires_at = milliseconds + 7 * 86400000
    summary = fact_summary(job, question, expires_at)
    event_key = "decision:" + identifier
    label = question.get("label") if isinstance(question.get("label"), str) and question["label"].strip() else None
    database.execute("INSERT INTO user_decisions(id,job_key,field,semantic,answer_type,choices,payload_hash,job_hash,policy_hash,expires_at,state,summary,event_key,label) VALUES (?,?,?,?,?,?,?,?,?,?,'PENDING',?,?,?)",
                     (identifier, job["job_key"], question["id"], question["semantic"], question.get("answer_type", "text"), encoded(question.get("choices", [])), payload_hash, job_hash, policy_hash, expires_at, summary, event_key, label))
    database.execute("INSERT OR IGNORE INTO notification_outbox(event_key,message,state,updated_at) VALUES (?,?,'WAITING_TRANSPORT',?)", (event_key, summary, now))
    return identifier


def request_transport_test(database, policy, now):
    existing = database.execute("SELECT id FROM user_decisions WHERE kind='test' AND state IN ('PENDING','TEST_CONFIRMED') ORDER BY expires_at DESC LIMIT 1").fetchone()
    if existing:
        return existing[0]
    identifier = uuid.uuid4().hex
    milliseconds = int(dt.datetime.fromisoformat(now).timestamp() * 1000)
    summary = "Harmless job-search transport test only. Please reply to THIS Telegram message with: transport-ok. Your reply only verifies the connection; it cannot send outreach or submit any application."
    payload_hash = digest({"type": "transport_test", "id": identifier})
    database.execute("INSERT INTO user_decisions(id,job_key,field,semantic,answer_type,choices,payload_hash,job_hash,policy_hash,expires_at,state,summary,event_key,kind) VALUES (?,'system:transport-smoke','transport_ok','harmless_transport_test','text','[]',?,?,?,?, 'PENDING',?,?,'test')",
                     (identifier, payload_hash,
                      scoped_job_hash("test", "transport_ok", "harmless_transport_test", {"type": "transport_test"}),
                      scoped_policy_hash("test", "transport_ok", "harmless_transport_test", policy),
                      milliseconds + 86400000, summary, "decision:" + identifier))
    database.execute("INSERT INTO notification_outbox(event_key,message,state,updated_at) VALUES (?,?,'WAITING_TRANSPORT',?)", ("decision:" + identifier, summary, now))
    return identifier


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, url):
        return None


class PreSendUnavailable(RuntimeError):
    pass


class Bridge:
    def __init__(self, settings):
        self.settings = settings

    def call(self, operation, body=None):
        parsed = urllib.parse.urlsplit(self.settings.get("url", ""))
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or not self.settings.get("owner_id"):
            raise PreSendUnavailable("BRIDGE_AUTHENTICATED_ENDPOINT_NOT_CONFIGURED")
        service, account = self.settings.get("keychain_service"), self.settings.get("keychain_account")
        if self.settings.get("credential_file"):
            path = Path(self.settings["credential_file"]).expanduser()
            try:
                metadata = path.stat(follow_symlinks=False)
            except OSError:
                raise PreSendUnavailable("BRIDGE_CREDENTIAL_FILE_UNAVAILABLE") from None
            # Windows has no uid and no group/other mode bits (st_mode is always 0o666 there): the
            # owner check alone (portable.owned_by_me) decides. POSIX is unchanged (E2.T3 P3).
            if path.is_symlink() or not portable.owned_by_me(metadata) or (not portable.WINDOWS and metadata.st_mode & 0o077):
                raise PreSendUnavailable("BRIDGE_CREDENTIAL_FILE_PERMISSIONS_INVALID")
            try:
                token = path.read_text().strip()
            except (OSError, UnicodeError):
                raise PreSendUnavailable("BRIDGE_CREDENTIAL_FILE_UNAVAILABLE") from None
        else:
            if not service or not account:
                raise PreSendUnavailable("BRIDGE_SCOPED_MACHINE_CREDENTIAL_NOT_CONFIGURED")
            try:
                credential = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", service, "-a", account, "-w"], capture_output=True, text=True, timeout=15)
            except (OSError, subprocess.SubprocessError):
                raise PreSendUnavailable("BRIDGE_KEYCHAIN_CREDENTIAL_UNAVAILABLE") from None
            if credential.returncode or not credential.stdout.strip():
                raise PreSendUnavailable("BRIDGE_KEYCHAIN_CREDENTIAL_UNAVAILABLE")
            token = credential.stdout.strip()
        if not token or any(character.isspace() for character in token):
            raise PreSendUnavailable("BRIDGE_CREDENTIAL_INVALID")
        request = urllib.request.Request(self.settings["url"].rstrip("/") + "/api/transport/" + operation,
                                         data=encoded(body).encode() if body is not None else None,
                                         headers={"Authorization": "Bearer " + token, "Content-Type": "application/json", "User-Agent": "job-search-orchestrator/1.0"})
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
                return json.loads(response.read(1_000_000))
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"BRIDGE_HTTP_{error.code}") from None
        except urllib.error.URLError as error:
            raise RuntimeError("BRIDGE_NETWORK_" + type(error.reason).__name__.upper()) from None
        except Exception:
            raise RuntimeError("BRIDGE_AUTHENTICATED_EXCHANGE_UNAVAILABLE") from None


def apply_event(database, event, settings, policy, now):
    sequence = event["seq"]
    if type(sequence) is not int or sequence < 1:
        raise ValueError("Invalid event sequence")
    if database.execute("SELECT 1 FROM transport_inbox WHERE seq=?", (sequence,)).fetchone():
        return
    decision = database.execute("SELECT * FROM user_decisions WHERE id=?", (event["decision_id"],)).fetchone()
    if decision and decision["kind"] == "test":
        response = event.get("response", {})
        response = response if isinstance(response, dict) else {}
        valid = (decision["registered"] and decision["message_id"] and decision["state"] == "PENDING"
                 and decision["expires_at"] > int(dt.datetime.fromisoformat(now).timestamp() * 1000)
                 and event.get("owner") == decision["owner_id"] == settings.get("owner_id") and bool(decision["owner_id"])
                 and event.get("action_id") == decision["job_key"]
                 and event.get("payload_hash") == decision["payload_hash"] and response.get("status") == "answered"
                 and isinstance(response.get("raw"), str) and response["raw"].strip() == "transport-ok")
        state = "TEST_CONFIRMED" if valid else "TEST_REJECTED"
        if valid:
            database.execute("UPDATE user_decisions SET state='TEST_CONFIRMED',raw_response=? WHERE id=?", (encoded(response), decision["id"]))
        database.execute("INSERT INTO transport_inbox VALUES (?,?,?,?,?,0)", (sequence, event["decision_id"], encoded(event), state, now))
        return
    job = database.execute("SELECT * FROM application_jobs WHERE job_key=?", (decision["job_key"],)).fetchone() if decision else None
    milliseconds = int(dt.datetime.fromisoformat(now).timestamp() * 1000)
    valid = (decision and job and decision["registered"] and decision["message_id"] and decision["state"] == "PENDING" and decision["expires_at"] > milliseconds
             and event.get("owner") == decision["owner_id"] == settings.get("owner_id") and bool(decision["owner_id"])
             and event.get("action_id") == decision["job_key"]
             and event.get("payload_hash") == decision["payload_hash"]
             and decision["job_hash"] == row_job_hash(decision, json.loads(job["payload"]))
             and decision["policy_hash"] == row_policy_hash(decision, policy) and job["state"] == "BLOCKED")
    state = "REJECTED_STALE_OR_MISMATCH"
    if valid:
        state = _apply_answer(database, decision, job, event.get("response", {}), now)
    database.execute("INSERT INTO transport_inbox VALUES (?,?,?,?,?,0)", (sequence, event["decision_id"], encoded(event), state, now))


def _valid_against_job(decision, job, policy, milliseconds):
    """The checks a reply must pass whatever carried it (the bridge or the dashboard): the question
    is still PENDING and unexpired, its job still BLOCKED, and neither the posting nor the policy
    it was asked under has changed."""
    return bool(decision and job and decision["state"] == "PENDING" and decision["expires_at"] > milliseconds
                and decision["job_hash"] == row_job_hash(decision, json.loads(job["payload"]))
                and decision["policy_hash"] == row_policy_hash(decision, policy) and job["state"] == "BLOCKED")


def _apply_answer(database, decision, job, response, now):
    """A validated reply's effect, shared by `apply_event` (Telegram) and `answer_locally`
    (dashboard): the answer is checked against the question's type and written to
    application_answers (source authenticated-owner-decision:<id>), and the job is due again; an
    answer of the wrong type is INVALID_ANSWER and the question is asked again (`reissue_fact`)."""
    response = response if isinstance(response, dict) else {}
    raw = response.get("raw")
    valid_answer = response.get("status") == "answered" and isinstance(raw, str) and 0 < len(raw.strip()) <= 4000
    value = raw.strip() if valid_answer else None
    if valid_answer and decision["answer_type"] == "boolean":
        valid_answer = value.casefold() in {"yes", "no", "true", "false"}
        value = value.casefold() in {"yes", "true"}
    elif valid_answer and decision["answer_type"] == "choice":
        valid_answer = value in json.loads(decision["choices"])
    elif decision["answer_type"] != "text":
        valid_answer = False
    if valid_answer:
        database.execute("INSERT INTO application_answers VALUES (?,?,?,?,?) ON CONFLICT(job_key,field) DO UPDATE SET semantic=excluded.semantic,value=excluded.value,source=excluded.source", (decision["job_key"], decision["field"], decision["semantic"], encoded(value), "authenticated-owner-decision:" + decision["id"]))
        database.execute("UPDATE user_decisions SET state='ANSWERED',raw_response=? WHERE id=?", (encoded(response), decision["id"]))
        database.execute("UPDATE application_jobs SET next_at=NULL WHERE job_key=?", (job["job_key"],))
        return "APPLIED"
    database.execute("UPDATE user_decisions SET state='INVALID_ANSWER',raw_response=?,last_error='Exact factual answer required, approval is not an answer' WHERE id=?", (encoded(response), decision["id"]))
    reissue_fact(database, decision, now)
    return "INVALID_ANSWER"


DASHBOARD_REFUSALS = {
    "QUESTION_NOT_FOUND": "This question is no longer on record. The list below is up to date.",
    "QUESTION_CHANGED": "This question changed since it was shown. Review it again below.",
    "QUESTION_NOT_PENDING": "This question was already answered or closed. The list below is up to date.",
    "QUESTION_EXPIRED": "This question expired. The application stays held; Sidedoor asks again if it still needs the answer.",
    "PROFILE_MISSING": "Your profile is missing, so this answer cannot be checked. Finish Profile and search in Settings, then answer again.",
    "QUESTION_STALE": ("The application or your settings changed since this was asked, so this answer would no longer "
                       "be about the same thing. Sidedoor asks again if it still needs it."),
}


def answer_locally(database, decision_id, raw, *, shown_payload_hash, policy=None, now=None):
    """An owner question answered on the dashboard's Approvals page (E6.T2 S3; owner, 2026-10-22: the
    dashboard is the only transport). The dashboard session is the authentication (E6.T1: install
    token cookie, Origin check), so the bridge's identity fields (registered, message_id, owner id)
    are not asked for; every other check of `apply_event` is the same (`_valid_against_job`), and so
    is the effect (`_apply_answer`). `shown_payload_hash` is the question the page showed.

    The reply is recorded in transport_inbox like a Telegram reply, under a negative seq with acked=1
    and payload via "dashboard": `cycle` never sends it to the relay (only acked=0 rows are acked)
    and never moves the relay cursor for it. One transaction. Returns {"state": APPLIED |
    INVALID_ANSWER | REFUSED, "reason", "message"}; a refusal writes only its inbox row."""
    now = now or dt.datetime.now(dt.timezone.utc).isoformat()
    if policy is None:
        try:
            policy = user_profile.load()
        except user_profile.ProfileMissing:
            return {"state": "REFUSED", "reason": "PROFILE_MISSING", "message": DASHBOARD_REFUSALS["PROFILE_MISSING"]}
    milliseconds = int(dt.datetime.fromisoformat(now).timestamp() * 1000)
    response = {"status": "answered", "raw": raw if isinstance(raw, str) else "", "via": "dashboard"}
    if not database.in_transaction:
        database.execute("BEGIN IMMEDIATE")   # the write lock before anything is read (E6.T4 review 20)
    with database:
        decision = database.execute("SELECT * FROM user_decisions WHERE id=? AND kind='fact'", (decision_id,)).fetchone()
        if decision is None:
            return {"state": "REFUSED", "reason": "QUESTION_NOT_FOUND", "message": DASHBOARD_REFUSALS["QUESTION_NOT_FOUND"]}
        job = database.execute("SELECT * FROM application_jobs WHERE job_key=?", (decision["job_key"],)).fetchone()
        if decision["payload_hash"] != shown_payload_hash:
            reason = "QUESTION_CHANGED"
        elif decision["state"] != "PENDING":
            reason = "QUESTION_NOT_PENDING"
        elif decision["expires_at"] <= milliseconds:
            reason = "QUESTION_EXPIRED"
        elif not _valid_against_job(decision, job, policy, milliseconds):
            reason = "QUESTION_STALE"
        else:
            reason = None
        state = "REJECTED_STALE_OR_MISMATCH" if reason else _apply_answer(database, decision, job, response, now)
        payload = {"via": "dashboard", "decision_id": decision["id"], "action_id": decision["job_key"],
                   "payload_hash": shown_payload_hash, "response": response}
        # The next negative seq is taken by the INSERT itself (one statement), never read beforehand.
        database.execute("INSERT INTO transport_inbox SELECT MIN(0, COALESCE(MIN(seq),0))-1,?,?,?,?,1 FROM transport_inbox",
                         (decision["id"], encoded(payload), state, now))
    if reason:
        return {"state": "REFUSED", "reason": reason, "message": DASHBOARD_REFUSALS[reason]}
    if state == "INVALID_ANSWER":
        return {"state": state, "reason": state,
                "message": "That answer does not fit this question. Sidedoor asks it again: reload this page to answer the new one."}
    return {"state": state, "reason": None, "message": "Answer saved. The application goes on at its next turn."}


def refresh_stale(database, policy, now):
    """Close every PENDING form question that no longer holds against its job or the current policy
    (job no longer BLOCKED, posting replaced, or the policy it was asked under changed, e.g. by an
    answer kept from Approvals for the same semantic) as STALE, cancel its notification, and make a
    still-BLOCKED job due again, so its next applications pass re-evaluates it: the answering AI
    answers from the new policy, or a fresh question is asked. Needs no relay (E6.T4 review 3).
    Returns the closed decision ids."""
    closed = []
    for decision in list(database.execute("SELECT * FROM user_decisions WHERE state='PENDING' AND kind='fact'")):
        job = database.execute("SELECT * FROM application_jobs WHERE job_key=?", (decision["job_key"],)).fetchone()
        try:
            current = (job and job["state"] == "BLOCKED" and decision["job_hash"] == row_job_hash(decision, json.loads(job["payload"]))
                       and decision["policy_hash"] == row_policy_hash(decision, policy))
        except Exception as error:
            # Any record that cannot be read (bad JSON, the wrong shape): this question is left as it
            # is and logged; the others are still refreshed (E6.T4 A4, item 7).
            LOG.warning("refresh_stale skipped question %s: its job record could not be read (%s)",
                        decision["id"], type(error).__name__)
            continue
        if current:
            continue
        with database:
            database.execute("UPDATE user_decisions SET state='STALE',last_error='FACT_REQUEST_REQUIRES_FRESH_JOB_REVIEW' WHERE id=? AND state='PENDING'", (decision["id"],))
            database.execute("UPDATE notification_outbox SET state='CANCELLED' WHERE event_key=? AND state IN ('PENDING','WAITING_TRANSPORT','WAITING_OWNER')", (decision["event_key"],))
            if job and job["state"] == "BLOCKED":
                database.execute("UPDATE application_jobs SET next_at=NULL WHERE job_key=? AND state='BLOCKED'", (job["job_key"],))
        closed.append(decision["id"])
    return closed


def cycle(database, config, root, client=None, now=None):
    now = now or dt.datetime.now(dt.timezone.utc).isoformat()
    initialize(database)
    settings = config.get("bridge_transport", {})
    client = client or Bridge(settings)
    policy = user_profile.load()
    try:
        refresh_stale(database, policy, now)   # before anything that needs the relay
        for invalid in list(database.execute("SELECT * FROM user_decisions WHERE state='INVALID_ANSWER' AND kind='fact'")):
            if not invalid["owner_id"]:
                saved = database.execute("SELECT payload FROM transport_inbox WHERE decision_id=? AND state='INVALID_ANSWER' ORDER BY seq DESC LIMIT 1", (invalid["id"],)).fetchone()
                evidence = json.loads(saved[0]) if saved else {}
                if (evidence.get("owner") and evidence.get("owner") == settings.get("owner_id")
                        and evidence.get("action_id") == invalid["job_key"]
                        and evidence.get("payload_hash") == invalid["payload_hash"]):
                    with database:
                        database.execute("UPDATE user_decisions SET owner_id=? WHERE id=?", (evidence["owner"], invalid["id"]))
                    invalid = database.execute("SELECT * FROM user_decisions WHERE id=?", (invalid["id"],)).fetchone()
            if (invalid["policy_hash"] == row_policy_hash(invalid, policy) and invalid["owner_id"] == settings.get("owner_id")
                    and invalid["owner_id"]):
                with database:
                    reissue_fact(database, invalid, now)
        for decision in list(database.execute("SELECT * FROM user_decisions WHERE state='PENDING'")):
            if decision["expires_at"] <= int(dt.datetime.fromisoformat(now).timestamp() * 1000):
                with database:
                    database.execute("UPDATE user_decisions SET state='EXPIRED' WHERE id=?", (decision["id"],))
                    database.execute("UPDATE notification_outbox SET state='CANCELLED' WHERE event_key=? AND state IN ('PENDING','WAITING_TRANSPORT','WAITING_OWNER')", (decision["event_key"],))
                continue
            if not settings.get("owner_id"):
                raise PreSendUnavailable("BRIDGE_AUTHENTICATED_ENDPOINT_NOT_CONFIGURED")
            if decision["owner_id"] and decision["owner_id"] != settings["owner_id"]:
                with database:
                    database.execute("UPDATE user_decisions SET last_error='BRIDGE_DECISION_OWNER_CHANGED' WHERE id=?", (decision["id"],))
                    database.execute("UPDATE notification_outbox SET state='WAITING_OWNER' WHERE event_key=? AND state IN ('PENDING','WAITING_TRANSPORT')", (decision["event_key"],))
                continue
            with database:
                database.execute("UPDATE user_decisions SET owner_id=? WHERE id=? AND owner_id IS NULL", (settings["owner_id"], decision["id"]))
                database.execute("UPDATE notification_outbox SET state=? WHERE event_key=? AND state='WAITING_OWNER'",
                                 ("PENDING" if decision["registered"] else "WAITING_TRANSPORT", decision["event_key"]))
            if decision["kind"] == "fact":
                job = database.execute("SELECT * FROM application_jobs WHERE job_key=?", (decision["job_key"],)).fetchone()
                if (not job or job["state"] != "BLOCKED"
                        or decision["job_hash"] != row_job_hash(decision, json.loads(job["payload"]))
                        or decision["policy_hash"] != row_policy_hash(decision, policy)):
                    with database:
                        database.execute("UPDATE user_decisions SET state='STALE',last_error='FACT_REQUEST_REQUIRES_FRESH_JOB_REVIEW' WHERE id=?", (decision["id"],))
                        database.execute("UPDATE notification_outbox SET state='CANCELLED' WHERE event_key=? AND state IN ('PENDING','WAITING_TRANSPORT','WAITING_OWNER')", (decision["event_key"],))
                    continue
            delivery = database.execute("SELECT delivery FROM notification_outbox WHERE event_key=? AND state='DELIVERED'", (decision["event_key"],)).fetchone()
            message_id = json.loads(delivery[0])["message_id"] if delivery else None
            if not decision["registered"] or message_id and not decision["message_id"]:
                body = {"id": decision["id"], "action_id": decision["job_key"], "payload_hash": decision["payload_hash"], "kind": decision["kind"], "summary": decision["summary"], "expires_at": decision["expires_at"]}
                if message_id:
                    body["telegram_message_id"] = message_id
                receipt = client.call("decisions", body)
                if receipt.get("id") != decision["id"] or message_id and receipt.get("telegram_message_id") != message_id:
                    raise RuntimeError("BRIDGE_DECISION_REGISTRATION_UNVERIFIED")
                with database:
                    database.execute("UPDATE user_decisions SET registered=1,message_id=?,last_error=NULL WHERE id=?", (message_id, decision["id"]))
                    database.execute("UPDATE notification_outbox SET state='PENDING' WHERE event_key=? AND state='WAITING_TRANSPORT'", (decision["event_key"],))
        pending_acks = [row[0] for row in database.execute("SELECT seq FROM transport_inbox WHERE acked=0 ORDER BY seq LIMIT 100")]
        if pending_acks:
            receipt = client.call("acks", {"consumer_id": "local-mac", "event_ids": pending_acks})
            if receipt.get("acknowledged") != pending_acks:
                raise RuntimeError("BRIDGE_ACK_UNVERIFIED")
            with database:
                database.executemany("UPDATE transport_inbox SET acked=1 WHERE seq=?", [(sequence,) for sequence in pending_acks])
        cursor = database.execute("SELECT seq FROM transport_cursor WHERE id=1").fetchone()[0]
        page = client.call(f"events?after={cursor}&limit=50")
        events = page["events"]
        if not isinstance(events, list) or len(events) > 50 or page["next_cursor"] != (events[-1]["seq"] if events else cursor):
            raise ValueError("Invalid bridge event page")
        with database:
            previous = cursor
            for event in events:
                if event["seq"] <= previous:
                    raise ValueError("Unordered bridge event page")
                apply_event(database, event, settings, policy, now)
                previous = event["seq"]
            database.execute("UPDATE transport_cursor SET seq=? WHERE id=1", (page["next_cursor"],))
        return {"state": "EXCHANGE_COMPLETE", "events_received": len(events), "cursor": page["next_cursor"], "ack_on_next_cycle": bool(events)}
    except Exception as error:
        safe = str(error) if isinstance(error, RuntimeError) and str(error).startswith("BRIDGE_") else "BRIDGE_EXCHANGE_FAILED"
        return {"state": "BLOCKED", "gate": safe, "pending_events_and_decisions_preserved": True}
