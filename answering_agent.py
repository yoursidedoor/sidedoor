"""The AI answering step: map an employer's own questions to the owner's recorded facts.

`applications.resolve_answers` binds a form question to a recorded answer by exact string
equality on `semantic`, and the semantic is authored freely by the browser AI at inspect
time (`application_browser.result_contract` supplies only the placeholder
`"verified-answer-semantic"`). So every employer invents a different string and nothing
matches: the only real enumerated review asked
`office_attendance_2_3_days_per_week_in_springfield_or_austin`, which no fixed policy key can satisfy.
`AGENTS.md` assigns that mapping to the AI verbatim - *"Applications: navigate forms, map
questions to verified facts, choose appropriate wording"* - and Python was doing it.

This module gives the judgment back to the AI and keeps every consequence in Python. The
model is handed the employer's own question, with its own id and its own semantic, and
mints an answer against that exact pair; `resolve_answers` is unchanged and now always
matches, because the answer was minted for the string it matches on.

Owner, 2026-09-28: "Let AI solely give the answers based on my preferences ... I trust it more
than i trust Python." The AI decides every answer, work authorization, sponsorship, citizenship,
self-identification and agreements included, from the owner's recorded facts and preferences.
What Python still does is mechanical:

1.  **Fit the control.** `conform_value` checks the value is enterable in this control, and a
    boolean is rendered onto the employer's own option wording (`_boolean_literal`).
2.  **Files.** A file control gets whatever the AI gives: the resume path is attached as the
    file, cover-letter text is rendered to a PDF (`applications`).
3.  **Recorded values and the start date.** A recorded answer whose semantic matches is used
    as recorded, and the start date comes from the owner's application-date + 28 day rule.
4.  **Citations are recorded, not checked.**
5.  **Raw output is preserved** verbatim beside the normalized audit in the immutable
    `application_answering_attempts` table.
6.  **Fail safe.** Answering model unavailable, timed out, malformed, schema-invalid, coverage-invalid
    or not fitting the control: nothing is written and every affected question becomes `unknown`,
    which is exactly today's behaviour. It never degrades to a guess.
7.  **Nothing else loosens.** This step only fills answers. Admission, the action guard,
    duplicate protection, the verified-inspect precondition, the eligibility assessment, the
    designated-resume hash check and the one-shot arm are untouched, and
    `desktop_adapter.arm` still recomputes `resolve_answers` immediately before submission.

Off by default: `config["application_answering"]["enabled"]` must be exactly `True`, the
`reply_worker.enabled` convention. Disabled, `materialize` writes nothing and the caller
behaves exactly as it does today.

The answer judge (owner, 2026-10-21, option A: "There should be an AI judge for answering agent.
THAT CAN repair on multiple turns until no other progress is made. python should not handle
this."): after the answering call a separate AI judge reads every answer with the raw résumé,
the answering AI's own instructions and the owner's recorded answers and preferences, and
returns ACCEPT, REVISE (with its reason) or CANNOT_ANSWER for each. REVISE answers go back to the
answering AI with the judge's reasons; the judge reads the new answers; the rounds end when it
asks for no more revisions, with at most MAX_ROUNDS (7) as a runaway guard. Accepted answers are
written; so is the answering AI's latest fitting answer when the judge objects with no untrue fact
(J6 option B) or when a judge or repair call fails (G3, owner 2026-10-22: treated like that
objection, recorded `judge_failed`), never an answer the judge flagged untrue_fact true. Python
decides nothing about an answer's content.

Both calls are direct OpenRouter chat-completions calls (`text_model.complete`, reasoning "low",
a strict json_schema response format, provider routing `outreach_agent.DIRECT_PROVIDER`), the
judges' shape. They have no tools, so they cannot produce a side effect. Each call is booked
through `workflow_tasks.reserve_compute` as a consumer of the existing shared budget and settled
when it completes; a compute wait consumes nothing, writes no answer and fails nothing.

`adapter` (the sixth positional parameter) is an optional injected executor for offline
validation, used in place of the OpenRouter call for both the answering AI and the judge. It is
used when it is a plain callable, or an object exposing `answering_executor` / `run_answering`.
A browser adapter - `applications.CommandAdapter`, the desktop adapter, a test's mock ATS -
matches none of those and is ignored, so the seam can pass its own `adapter` straight through
without the browser ever being mistaken for the model. It is called with the prompt (and the
packet, when it takes two parameters) and may return the raw JSON text or a decoded object.
"""

import resources
import datetime as dt
from decimal import Decimal
import hashlib
import inspect
import json
import sqlite3
from pathlib import Path
import re
import uuid

import applications
import provider_limits
import text_model
import workflow_tasks


PACKET_VERSION = "application-answering-v1"
PROMPT_FILE = "answering-prompt.md"
SCHEMA_FILE = "answering-schema.json"
# The answer judge (owner, 2026-10-21, option A): a separate AI reviews every answer.
JUDGE_VERSION = "application-answering-judge-v1"
JUDGE_PROMPT_FILE = "answering-judge-prompt.md"
JUDGE_SCHEMA_FILE = "answering-judge-schema.json"
JUDGE_VERDICTS = ("ACCEPT", "REVISE", "CANNOT_ANSWER")
MAX_ROUNDS = 7  # judge/repair rounds, a runaway guard only (owner: "Make it max, 7 turns")
PACKET_MARKER = "\n\nUNTRUSTED_APPLICATION_PACKET_JSON:\n"
RULES_MARKER = "\n\nANSWERING_AI_INSTRUCTIONS (the rules every answer must follow):\n"
JUDGE_MARKER = "\n\nUNTRUSTED_ANSWER_REVIEW_JSON:\n"
# Both calls are direct OpenRouter calls, the judges' shape (owner, 2026-10-21: "sure, move to
# direct glm"). The answering output may carry several paragraphs (cover letters, motivation), so
# its ceiling covers MAX_OUTPUT_BYTES; the judge writes one short verdict per answer.
DIRECT_ROUTE = "openrouter_chat_completions"
DIRECT_EFFORT = "low"
# 24000: a 40-question form answers in about 40 entries x ~1,000 bytes plus a ~3,000-byte cover
# letter (~43 KB, ~13k tokens), plus low-effort reasoning; 24000 leaves over 50% headroom, and the
# answer's MAX_OUTPUT_BYTES (64 KB, ~18k tokens) is reached before this ceiling.
ANSWER_MAX_TOKENS = 24000
JUDGE_MAX_TOKENS = 8000
AI_SOURCE_PREFIX = "ai-answer:"
# Rows written before 2026-10-21 by the deleted Python routing of the owner's recorded
# self-identification (`explicit_declaration`); nothing writes this prefix now, and `materialize`
# still lets an AI answer replace such a row, as it did.
DECLARATION_SOURCE_PREFIX = "owner-declaration:"
DEFAULT_TIMEOUT = 60
DEFAULT_DAILY_SECONDS = 10800
MAX_PACKET_BYTES = 256 * 1024
MAX_OUTPUT_BYTES = 65536
MAX_TEXT_ANSWER = 2000

# Statuses the recorded jurisdiction map may carry (`work_authorization_by_jurisdiction`: ISO
# codes, "EU" for any member country, "*" for everywhere else). The AI reads the map itself
# (owner, 2026-10-21); these names are kept for the inspect read's verdict in `applications`.
AUTHORIZED_WITHOUT_SPONSORSHIP = "AUTHORIZED_NO_SPONSORSHIP_REQUIRED"
AUTHORIZED_WITH_SPONSORSHIP = "AUTHORIZED_SPONSORSHIP_REQUIRED"
NOT_AUTHORIZED = "NOT_AUTHORIZED"


COMPUTED_CLASSES = frozenset({"WORK_AUTHORIZATION_HELD", "SPONSORSHIP_REQUIREMENT",
                              "WORK_LOCATION_DECLARATION"})
WITHHELD_CLASSES = frozenset({"DEMOGRAPHIC_VOLUNTARY", "LEGAL_ASSERTION_OR_AGREEMENT"})
# The computed-experience class. It is NOT a quotation class and NOT an authorization class:
# the model says which dated resume sections bear on the question and whether the technology
# asked about is one of the owner's, and Python does the arithmetic and the bounding. The
# emitted literal is an integer Python computed from dates in the hash-bound resume; nothing
# the model writes in `value` is read, exactly as for the authorization classes. This is what
# lets "How many years of Kubernetes experience do you have?" be answered at all: the resume
# says "September 2016 - February 2021" and "managed Kubernetes", and no arrangement of those
# characters contains the answer, so the verbatim-quotation rule can never produce it.
EXPERIENCE_CLASSES = frozenset({"COMPUTED_EXPERIENCE"})
# The willingness classes. Unlike every other class here, the emitted truth is the model's own
# reading of the question against the owner's recorded `location_priority` (owner, 2026-09-22);
# Python still renders the literal off the employer's choices and still vetoes a "yes" for a
# role whose every recorded place the owner excluded. See `judged_preference`.
PREFERENCE_CLASSES = frozenset({"WORK_MODE_PREFERENCE", "LOCATION_PREFERENCE"})
CLASSES = frozenset({"IDENTITY_OR_CONTACT", "LOCATION_PREFERENCE", "WORK_MODE_PREFERENCE",
                     "COMPENSATION_EXPECTATION", "AVAILABILITY", "CANDIDATE_HISTORY_FACT",
                     "ROLE_SPECIFIC_FREE_TEXT", "OTHER"}) | COMPUTED_CLASSES | WITHHELD_CLASSES \
    | EXPERIENCE_CLASSES

# The jurisdiction vocabulary lives in `applications`, because `resolve_answers` needs the
# same one for its own scope guard and a second copy is a second thing to forget to update.
# `tests/test_answering_agent.py` asserts these are the same objects, not equal copies.
JURISDICTION_TOKENS = applications.JURISDICTION_TOKENS
jurisdictions_named = applications.jurisdictions_named
jurisdiction_of = applications.jurisdiction_of
question_text = applications.question_text

# The literals Python renders a boolean onto for a bounded control.
AFFIRMATIVE_LITERALS = applications.AFFIRMATIVE_LITERALS
NEGATIVE_LITERALS = applications.NEGATIVE_LITERALS
employer_wording = applications.employer_wording
reads_as_affirmative = applications.reads_as_affirmative
reads_as_negative = applications.reads_as_negative
bounded_assertion = applications.bounded_assertion

# The willingness / present-fact / onsite / remote / relocation marker nets that used to admit
# a question into `computed_preference` were deleted on 2026-09-22. They were English-only nets
# deciding whether Python was allowed to read an employer's question, and they existed only to
# guard a Python computation that no longer happens: the model now reads these questions itself
# (`judged_preference`). Nothing else consulted them.
START_DATE_MARKER_PATTERN = re.compile(
    r"\b(start\s+date|earliest\s+\w*\s*(start|date)|available\s+from|availability\s+date"
    r"|when\s+(can|could)\s+you\s+start|date\s+available|available\s+to\s+start"
    r"|start\s+availability|commence|joining\s+date)\b", re.IGNORECASE)
NOTICE_PERIOD_MARKER_PATTERN = re.compile(r"\b(notice\s+period|period\s+of\s+notice|notice)\b",
                                          re.IGNORECASE)
BOOLEAN_AFFIRMATIVE = frozenset({"yes", "y", "true"})
BOOLEAN_NEGATIVE = frozenset({"no", "n", "false"})
ANSWER_TYPES = {
    "boolean": "boolean", "bool": "boolean", "checkbox": "boolean", "yes_no": "boolean",
    "yesno": "boolean", "choice": "choice", "select": "choice", "radio": "choice",
    "dropdown": "choice", "multiple_choice": "choice", "single_select": "choice",
    "number": "number", "integer": "number", "numeric": "number", "int": "number",
    "float": "number", "currency": "number",
    "text": "text", "string": "text", "textarea": "text", "long_text": "text",
    "email": "text", "url": "text", "tel": "text", "phone": "text", "date": "text",
}


class AnsweringUnavailable(RuntimeError):
    """The model could not be consulted. Never a reason to guess; always a reason to ask."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def settings(config):
    raw = (config or {}).get("application_answering")
    raw = raw if isinstance(raw, dict) else {}
    timeout = raw.get("timeout_seconds")
    daily = raw.get("daily_seconds")
    # The shared daily cap is the owner's configured daily_compute_seconds (21,000 since BM).
    cap = (config or {}).get("daily_compute_seconds", DEFAULT_DAILY_SECONDS)
    cap = cap if isinstance(cap, int) and not isinstance(cap, bool) and cap >= 1 else DEFAULT_DAILY_SECONDS
    return {
        "enabled": raw.get("enabled") is True,
        "timeout_seconds": timeout if isinstance(timeout, int) and not isinstance(timeout, bool)
                           and 1 <= timeout <= 600 else DEFAULT_TIMEOUT,
        "daily_seconds": daily if isinstance(daily, int) and not isinstance(daily, bool)
                         and 1 <= daily <= cap else cap,
    }


def initialize(database):
    """The immutable audit table. Raw model output and normalized verdict, never rewritten."""
    database.executescript("""
        CREATE TABLE IF NOT EXISTS application_answering_attempts (
            attempt_id TEXT PRIMARY KEY, job_key TEXT NOT NULL, created_at TEXT NOT NULL,
            packet_sha256 TEXT NOT NULL, prompt_sha256 TEXT, raw TEXT, raw_sha256 TEXT,
            model TEXT, outcome TEXT NOT NULL, audit TEXT NOT NULL);
        CREATE TRIGGER IF NOT EXISTS application_answering_attempts_no_update
            BEFORE UPDATE ON application_answering_attempts
            BEGIN SELECT RAISE(ABORT, 'Immutable application answering audit'); END;
        CREATE TRIGGER IF NOT EXISTS application_answering_attempts_no_delete
            BEFORE DELETE ON application_answering_attempts
            BEGIN SELECT RAISE(ABORT, 'Retain application answering audit'); END;
        CREATE TRIGGER IF NOT EXISTS application_answering_attempts_no_replace
            BEFORE INSERT ON application_answering_attempts
            WHEN EXISTS (SELECT 1 FROM application_answering_attempts WHERE attempt_id=NEW.attempt_id)
            BEGIN SELECT RAISE(ABORT, 'Retain application answering audit'); END;
    """)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def at_pointer(value, pointer):
    """RFC 6901 resolution, the same traversal `application_browser.at_pointer` performs.

    `tests/test_answering_agent.py` asserts the two agree, so the attribution check and the
    browser's own context references cannot drift into two different pointer languages.
    """
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError("ANSWER_ATTRIBUTION_POINTER_INVALID")
    for component in pointer[1:].split("/"):
        component = component.replace("~1", "/").replace("~0", "~")
        value = value[int(component)] if isinstance(value, list) else value[component]
    return value


def answer_type_of(question):
    raw = question.get("answer_type")
    if isinstance(raw, str):
        normalized = ANSWER_TYPES.get(applications.normalized(raw).replace(" ", "_"))
        if normalized:
            return normalized
    choices = question.get("choices")
    if isinstance(choices, list) and choices:
        return "choice"
    return "text"


def latest_review(database, job_key):
    """The latest stored review, falling back to the persisted per-requisition record.

    `application_reviews` is written only once a read reaches assessment; a read that
    enumerated the form but stayed inconclusive lives in `application_requisition_records`
    (Part C2), which is what lets answers be drafted from a question set already captured.
    """
    try:
        row = database.execute("SELECT evidence FROM application_reviews WHERE job_key=? "
                               "ORDER BY rowid DESC LIMIT 1", (job_key,)).fetchone()
        if not row:
            row = database.execute("SELECT review FROM application_requisition_records WHERE job_key=?",
                                   (job_key,)).fetchone()
    except Exception:
        return {}
    if not row:
        return {}
    try:
        value = json.loads(row[0])
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def packet_policy(policy):
    """The policy the model sees: recorded facts and targeting, with three exclusions.

    `minimum_base_salary_usd` and `salary_relaxation` are the owner's internal floor. They
    are not merely uncitable, they are absent, so no answer can disclose 180,000 when the
    desired band is 200,000-300,000. And any answer whose source begins `ai-answer:` - a
    previous derivation for this job - is dropped, so an attribution chain must terminate in
    a recorded fact rather than in another derivation.
    """
    answers = {}
    # Only the answer in force per semantic (owner, 2026-10-24; `applications.answers_for_model`).
    for key, value in applications.answers_for_model((policy or {}).get("answers")).items():
        if not isinstance(value, dict) or not value.get("source"):
            continue
        if str(value.get("source")).startswith(AI_SOURCE_PREFIX):
            continue
        answers[key] = {"semantic": value.get("semantic"), "value": value.get("value"),
                        "source": value.get("source")}
    result = {"answers": answers}
    for key in ("location_priority", "onsite_location_exclusions", "desired_base_salary_usd", "levels",
                "industries", "funding_stages"):
        if key in (policy or {}):
            result[key] = policy[key]
    return result


def packet_review(review):
    review = review if isinstance(review, dict) else {}
    facts = review.get("facts")
    return {"facts": facts if isinstance(facts, dict) else {},
            "compensation": review.get("compensation") if isinstance(review.get("compensation"), dict) else {},
            "official_url": review.get("official_url"), "role": review.get("role"),
            "form_revision": review.get("form_revision"),
            "eligible_location": review.get("eligible_location") if isinstance(review.get("eligible_location"), dict) else None}


def max_characters(question):
    """The character limit the read reported for a free-text field, or None (owner, 2026-10-01).

    Passed to the model so it writes within the limit; Python never shortens or rejects text
    by length. A value the read did not give as a positive whole number is no limit.
    """
    value = question.get("max_characters")
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def packet_question(question, key):
    return {"question_key": key, "id": question.get("id"), "semantic": question.get("semantic"),
            "label": question.get("label"), "required": bool(question.get("required")),
            "answer_type": answer_type_of(question),
            "choices": question.get("choices") if isinstance(question.get("choices"), list) else [],
            "max_characters": max_characters(question),
            **({"earliest_start_date": question["earliest_start_date"]} if question.get("earliest_start_date") else {})}


def build_packet(job, askable, policy, review, root, repair_feedback=None):
    packet = {
        "packet_version": PACKET_VERSION,
        "job": {"job_key": job["job_key"], "employer": job["employer"],
                "requisition": job["requisition"]},
        "questions": [packet_question(question, key) for key, question in askable],
        "policy": packet_policy(policy),
        "review": packet_review(review),
    }
    if repair_feedback:
        # Why each question was left open on the previous cycle (the judge's reasons, or the
        # failure), carried into this cycle's first answering call.
        packet["repair_feedback"] = repair_feedback
    try:
        import reply_resume
        packet["candidate_resume"] = reply_resume.context(root)
    except Exception:
        packet["candidate_resume"] = None
    encoded = json.dumps(packet, sort_keys=True, ensure_ascii=False)
    if len(encoded.encode("utf-8")) > MAX_PACKET_BYTES and packet["candidate_resume"] is not None:
        packet["candidate_resume"] = None
        encoded = json.dumps(packet, sort_keys=True, ensure_ascii=False)
    if len(encoded.encode("utf-8")) > MAX_PACKET_BYTES:
        raise AnsweringUnavailable("ANSWERING_PACKET_EXCEEDS_LIMIT")
    packet["packet_sha256"] = digest({key: value for key, value in packet.items()})
    return packet


def prompt_for(root, packet):
    template = resources.read_text(PROMPT_FILE)
    return template + PACKET_MARKER + json.dumps(packet, sort_keys=True, ensure_ascii=False)


def judge_prompt_for(root, review_packet):
    """The judge's prompt: its own instructions, the answering AI's instructions verbatim (the rules
    the answers must follow), then the answers under review with their packet."""
    template = resources.read_text(JUDGE_PROMPT_FILE)
    rules = resources.read_text(PROMPT_FILE)
    return (template + RULES_MARKER + rules + JUDGE_MARKER
            + json.dumps(review_packet, sort_keys=True, ensure_ascii=False))


def injected_executor(adapter):
    """The injected executor in `adapter`, or None when `adapter` is not one.

    Offline validation only (tests and the manual tools). A browser adapter exposes
    `call`/`prepare_submission`, never these names, so the seam can hand its own adapter straight
    through and the browser is never mistaken for the model. The same executor receives the
    answering prompt and the judge's prompt (it carries `JUDGE_MARKER`); it is called with the
    prompt, or with the prompt and its packet when it takes two parameters.
    """
    if adapter is None:
        return None
    for name in ("answering_executor", "run_answering"):
        candidate = getattr(adapter, name, None)
        if callable(candidate):
            return candidate
    return adapter if callable(adapter) else None


def run_model(root, config, prompt, timeout, adapter=None, packet=None, *, role="answer", calls=None):
    """Raw model output as text. Every failure raises AnsweringUnavailable.

    `role` is "answer" (the answering AI, `answering-schema.json`) or "judge" (the answer
    reviewer, `answering-judge-schema.json`). Without an injected executor this is one direct
    OpenRouter call (owner, 2026-10-21: "sure, move to direct glm"); `calls` collects its record.
    """
    executor = injected_executor(adapter)
    if executor is not None:
        raw = _run_injected(executor, prompt, packet)
    else:
        raw = _run_direct(root, config, prompt, timeout, role, calls if calls is not None else [])
    if not isinstance(raw, str) or not raw.strip():
        raise AnsweringUnavailable("ANSWERING_OUTPUT_MISSING")
    if len(raw.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise AnsweringUnavailable("ANSWERING_OUTPUT_EXCEEDS_LIMIT")
    return raw


def _run_direct(root, config, prompt, timeout, role, calls):
    """One `text_model.complete` call in the outreach send judge's shape: reasoning "low", the schema
    in a system message and as a strict json_schema response format, provider routing
    `outreach_agent.DIRECT_PROVIDER`. A failure raises the AnsweringUnavailable the codex run
    raised for the same failure, so the caller's retry path (ANSWERING_RETRY) is unchanged."""
    import outreach_agent
    if not text_model.settings(config):
        raise AnsweringUnavailable("ANSWERING_EXECUTOR_NOT_CONFIGURED")
    schema_text = resources.read_text(JUDGE_SCHEMA_FILE if role == "judge" else SCHEMA_FILE)
    max_tokens = JUDGE_MAX_TOKENS if role == "judge" else ANSWER_MAX_TOKENS
    system = "Return only one JSON object, with no other text, that matches this JSON Schema:\n" + schema_text
    started = dt.datetime.now(dt.timezone.utc)

    def failed(code, error):
        calls.append({"role": role, "route": DIRECT_ROUTE, "http_status": getattr(error, "status", None),
                      "duration_seconds": round((dt.datetime.now(dt.timezone.utc) - started).total_seconds(), 1),
                      "error": error.record() if isinstance(error, text_model.TextModelError)
                      else type(error).__name__ + ": " + str(error)[:300]})
        return AnsweringUnavailable(code)

    try:
        answer = text_model.complete(
            config, prompt, system=system, effort=DIRECT_EFFORT, max_tokens=max_tokens, timeout=timeout,
            response_format={"type": "json_schema", "json_schema": {
                "name": "application_answer_review" if role == "judge" else "application_answers",
                "strict": True, "schema": json.loads(schema_text)}},
            provider=outreach_agent.DIRECT_PROVIDER, content_limit=MAX_OUTPUT_BYTES)
    except TimeoutError as error:
        raise failed("ANSWERING_TIMEOUT", error) from None
    except text_model.TextModelResponseError as error:
        unavailable = failed("ANSWERING_OUTPUT_EXCEEDS_LIMIT" if error.content_limit_exceeded or error.finish_reason == "length"
                             else "ANSWERING_OUTPUT_MISSING" if error.finish_reason is not None
                             else "ANSWERING_EXECUTOR_FAILED", error)
        unavailable.settle = error.finish_reason == "length"  # cut at max_tokens: settled by duration (J8)
        raise unavailable from None
    except text_model.TextModelError as error:
        limit = provider_limits.wait_of(error)
        if limit:   # OpenRouter out of credit (HTTP 402): a capacity wait, as _run_injected treats one
            raise failed(provider_limits.CODE, error) from limit
        raise failed("ANSWERING_EXECUTOR_FAILED", error) from None
    except OSError as error:  # the key file could not be read
        raise failed("ANSWERING_EXECUTOR_UNAVAILABLE", error) from None
    calls.append({"role": role, "route": DIRECT_ROUTE, "reasoning_effort": DIRECT_EFFORT, "max_tokens": max_tokens,
                  "model": answer["model"], "usage": answer["usage"], "finish_reason": answer["finish_reason"],
                  "duration_seconds": round((dt.datetime.now(dt.timezone.utc) - started).total_seconds(), 1)})
    if answer["finish_reason"] == "length":
        # Cut at max_tokens: the answer is incomplete, so it is too long, not malformed. The call
        # itself completed, so it is settled by its duration like one (J8).
        error = AnsweringUnavailable("ANSWERING_OUTPUT_EXCEEDS_LIMIT")
        error.settle = True
        raise error
    content = answer["content"]
    try:
        json.loads(content)
        return content
    except ValueError:
        pass
    try:
        return json.dumps(text_model.json_object(content))
    except ValueError:
        return content  # parse_output refuses it (ANSWERING_OUTPUT_NOT_JSON)


def _arity(executor):
    try:
        return len(inspect.signature(executor).parameters)
    except (TypeError, ValueError):
        return 1


def _run_injected(executor, prompt, packet):
    try:
        value = executor(prompt, packet) if _arity(executor) >= 2 else executor(prompt)
    except AnsweringUnavailable:
        raise
    except TimeoutError:
        raise AnsweringUnavailable("ANSWERING_TIMEOUT") from None
    except Exception as error:
        limit = provider_limits.wait_of(error)
        if limit:
            raise AnsweringUnavailable(provider_limits.CODE) from limit
        raise AnsweringUnavailable("ANSWERING_EXECUTOR_FAILED") from None
    return value if isinstance(value, str) or value is None else json.dumps(value)


def _entry_shape(entry):
    required = {"question_key", "question_semantic", "question_class", "status", "answer_type",
                "value", "jurisdiction", "assertion", "affirmative_value", "negative_value",
                "attribution", "derivation", "unknown_reason"}
    if not isinstance(entry, dict) or set(entry) != required:
        return False
    if not isinstance(entry.get("question_key"), str) or not entry["question_key"].strip():
        return False
    if entry.get("question_class") not in CLASSES:
        return False
    if entry.get("status") not in {"ANSWERED", "UNKNOWN", "WITHHELD_BY_POLICY"}:
        return False
    if not isinstance(entry.get("attribution"), list):
        return False
    for citation in entry["attribution"]:
        if (not isinstance(citation, dict) or not set(citation) <= {"pointer", "quote"}
                or not isinstance(citation.get("pointer"), str)):
            return False
    return True


def parse_output(raw, packet, askable_keys):
    """Schema, packet binding and coverage. Any failure rejects the whole output."""
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        raise AnsweringUnavailable("ANSWERING_OUTPUT_NOT_JSON") from None
    # The answers are bound to their packet by the call itself, so a mistyped echo of
    # packet_sha256 no longer throws away the whole answer set (owner, 2026-10-07, item 10;
    # Ingen 09-29 and Tricell 10-05 lost correct answers to it). The echo may be present or not.
    if not isinstance(value, dict) or "answers" not in value or not set(value) <= {"packet_sha256", "answers"}:
        raise AnsweringUnavailable("ANSWERING_OUTPUT_ENVELOPE_INVALID")
    entries = value.get("answers")
    if not isinstance(entries, list) or any(not _entry_shape(entry) for entry in entries):
        raise AnsweringUnavailable("ANSWERING_OUTPUT_SCHEMA_INVALID")
    _repair_echoed_keys(entries, askable_keys)
    keys = [entry["question_key"] for entry in entries]
    if len(keys) != len(set(keys)) or set(keys) != set(askable_keys):
        raise AnsweringUnavailable("ANSWERING_COVERAGE_INVALID")
    return {entry["question_key"]: entry for entry in entries}


def _repair_echoed_keys(entries, askable_keys):
    """Map a mistyped echo of a supplied key back to that key (2026-10-06).

    The model echoes each question_key it was given, and only supplied keys are ever accepted.
    Woodgrove (2026-10-06) echoed `desired_base_salary:aabbb000111` for the supplied
    `desired_base_salary:aaabbb000111` (one letter of the fingerprint dropped), and the whole
    answer set, five correct answers, was thrown away. A returned key that was not supplied is
    taken as the one supplied key still unanswered whose name (the part before the fingerprint)
    is the same, when exactly one such key exists; otherwise it is left to fail coverage.
    """
    def name(key):
        return key.rsplit(":", 1)[0] if isinstance(key, str) and ":" in key else None

    supplied = set(askable_keys)
    unanswered = supplied - {entry["question_key"] for entry in entries}
    for entry in entries:
        if entry["question_key"] in supplied:
            continue
        candidates = [key for key in unanswered if name(key) is not None and name(key) == name(entry["question_key"])]
        if len(candidates) == 1:
            entry["question_key"] = candidates[0]
            unanswered.discard(candidates[0])


# Single-value numeric fitting (owner, 2026-10-01: "If the field needs only a single value, use
# the lower limit"). A strict allow-list: the WHOLE answer must be one of these shapes, so any
# other word (a unit, a magnitude word, "+ equity", "/hr") refuses rather than being dropped.
ISO_4217_CODES = frozenset("""
AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BRL BSD BTN BWP BYN
BZD CAD CDF CHF CLP CNY COP CRC CUC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL
GHS GIP GMD GNF GTQ GYD HKD HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW
KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MYR MZN
NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG SEK
SGD SHP SLE SOS SRD SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX USD UYU UZS
VES VND VUV WST XAF XCD XCG XOF XPF YER ZAR ZMW ZWG ZWL
""".split())
_APPROX = r"\s*(?:~\s*|(?i:approx\.|approximately)\s+)?"
# ASCII digits only: \d would also take Arabic-Indic or full-width digits.
_NUMBER = r"([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.([0-9]{1,2}))?(?![.,]?[0-9])\s*([kK])?"
_CURRENCY_BEFORE = r"(?:([A-Z]{3})\s*|[$€£]\s*)?"
_CURRENCY_AFTER = r"(?:\s*([A-Z]{3}))?"
_SEPARATOR = r"\s*(?:-|–|—|(?i:to))\s*"
_SALARY_WORD = r"(?i:annually|annual|base\s+salary|base|salary|per\s+year|per\s+annum|/\s*yr|/\s*year|a\s+year)"
# At most two salary word groups, in any order ("annual base salary", "base salary annually").
_SALARY_WORDS = r"(?P<words>\s*" + _SALARY_WORD + r"(?:\s*" + _SALARY_WORD + r")?)?\s*"
_AMOUNT = _CURRENCY_BEFORE + _NUMBER + _CURRENCY_AFTER
_RANGE_PATTERN = re.compile(_APPROX + _AMOUNT + _SEPARATOR + _AMOUNT + _SALARY_WORDS)
_SINGLE_PATTERN = re.compile(_APPROX + _CURRENCY_BEFORE + r"(-)?" + _NUMBER + _CURRENCY_AFTER + _SALARY_WORDS)
# Retirement-plan names read like amounts with a suffix; they are words, not salaries.
_PLAN_NAMES = re.compile(r"\s*(?:401|403|457)\s*\(?\s*[kKbB]\s*\)?\s*")


def _amount(whole, fraction, thousands):
    """Exact decimal arithmetic ("2.01k" is 2010, never 2009.9999999999998): an int when
    whole, else a number with at most two decimals."""
    number = Decimal(whole.replace(",", "") + ("." + fraction if fraction else ""))
    if thousands:
        number *= 1000
    if number == number.to_integral_value():
        return int(number)
    return float(number.quantize(Decimal("0.01")))


def _small_bare(whole, thousands):
    """Under 1000 with no k and no comma: with a salary word, likely dropped thousands."""
    return not thousands and "," not in whole and int(whole) < 1000


def _amount_refused(whole, fraction, thousands, *codes):
    """A leading zero ("02139"), a k on more than 999 ("1,500k"), or a currency code that is
    not ISO 4217 ("LPA", "TSD") makes the answer unusable."""
    if len(whole) > 1 and whole.startswith("0"):
        return True
    if thousands and ("," in whole or int(whole.replace(",", "")) > 999):
        return True
    return any(code is not None and code not in ISO_4217_CODES for code in codes)


def single_number(text):
    """The number a single-value numeric control takes for this text answer, or None.

    The WHOLE text must be one of two shapes (anything else is None, refused as before):
    - a range: [~ | approx. | approximately] [currency] amount [k] [currency], then a hyphen,
      en/em dash or "to", then [currency] amount [k] [currency], then at most two salary word
      groups in any order from: annual, base, base salary, salary, per year, per annum,
      annually, /yr, /year, a year (any case). Digits are ASCII 0-9 only. With a salary word,
      an answer whose every amount is under 1000 with no k and no comma is refused ("180-250
      base salary", "150 per year": the thousands were likely dropped).
      A currency is an ISO 4217 code (upper case) before or after an amount, or $, €, £
      before it. It becomes its smaller end ("250k-180k" -> 180000). A k on the high end only
      also applies to a low end written without a comma ("180-250k" -> 180000). ",000" written
      once applies to a low end of 10-999 with no comma, decimal or k when the high end is
      exactly one comma group ending ",000" and the rescaled low end is below it
      ("100-150,000" -> 100000). Then the range is refused when one end has a k or a comma
      group and the other has neither and is smaller ("180k-250", "500-1,000"), or when one end
      is ten times the other or more ("180000-25"). A bare "180-250" (no salary word) stays 180.
    - one amount, the same shape with an optional minus sign (kept): "USD 180,000", "185k", "-5".
    Also refused: a leading zero, k on more than 999, a three-letter code that is not ISO 4217,
    and the retirement-plan names 401k/403b/457b.
    """
    if not isinstance(text, str) or _PLAN_NAMES.fullmatch(text):
        return None
    match = _RANGE_PATTERN.fullmatch(text)
    if match:
        (low_code, low_whole, low_fraction, low_k, low_after,
         high_code, high_whole, high_fraction, high_k, high_after, words) = match.groups()
        if (_amount_refused(low_whole, low_fraction, low_k, low_code, low_after)
                or _amount_refused(high_whole, high_fraction, high_k, high_code, high_after)):
            return None
        if words and _small_bare(low_whole, low_k) and _small_bare(high_whole, high_k):
            return None  # "180-250 base salary": the thousands were likely dropped
        if high_k and not low_k and "," not in low_whole:
            low_k = high_k  # "180-250k": the k written once covers both ends
        low = _amount(low_whole, low_fraction, low_k)
        high = _amount(high_whole, high_fraction, high_k)
        if ("," not in low_whole and not low_fraction and not low_k and not high_k
                and not high_fraction and re.fullmatch(r"[0-9]{1,3},000", high_whole)
                and 10 <= low < 1000 and low * 1000 < high):
            low *= 1000  # "100-150,000": the ",000" written once covers both ends
            low_whole = low_whole + ",000"
        low_marked = bool(low_k) or "," in low_whole
        high_marked = bool(high_k) or "," in high_whole
        if low_marked != high_marked:
            bare, marked = (high, low) if low_marked else (low, high)
            if bare < marked:
                return None
        if max(low, high) >= 10 * min(low, high):  # also refuses a zero end
            return None
        if low > high and not low_marked and not high_marked:
            return None  # "555-1234", "12-31", "20001-4321": an identifier, not a reversed range
        return min(low, high)
    match = _SINGLE_PATTERN.fullmatch(text)
    if match:
        code, sign, whole, fraction, thousands, after, words = match.groups()
        if _amount_refused(whole, fraction, thousands, code, after):
            return None
        if words and _small_bare(whole, thousands):
            return None  # "150 per year": the thousands were likely dropped
        number = _amount(whole, fraction, thousands)
        return -number if sign else number
    return None


def conform_value(value, question):
    """Is this literal enterable in this control? Returns the value, or raises ValueError."""
    kind = answer_type_of(question)
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().casefold() in (BOOLEAN_AFFIRMATIVE | BOOLEAN_NEGATIVE):
            return value
        raise ValueError("VALUE_NOT_BOOLEAN")
    if kind == "choice":
        choices = question.get("choices") if isinstance(question.get("choices"), list) else []
        if choices and value not in choices:
            raise ValueError("VALUE_NOT_IN_CHOICES")
        if not choices and not isinstance(value, (str, bool, int, float)):
            raise ValueError("VALUE_NOT_ENTERABLE")
        return value
    if kind == "number":
        if isinstance(value, str):
            # Owner, 2026-10-01: "If the field needs only a single value, use the lower limit."
            # A single-number control gets a range answer's lower limit as a plain number.
            fitted = single_number(value)
            if fitted is not None:
                value = fitted
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("VALUE_NOT_NUMBER")
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("VALUE_NOT_FINITE")
        return value
    if isinstance(value, bool):
        # A Python-computed yes/no truth (authorization, relocation) is written into a
        # free-text control as the plain word; the value itself is unchanged.
        return "Yes" if value else "No"
    if not isinstance(value, str) or not value.strip():
        raise ValueError("VALUE_NOT_TEXT")
    if len(value) > MAX_TEXT_ANSWER:
        raise ValueError("VALUE_TEXT_TOO_LONG")
    return value


def _boolean_literal(question, truth):
    """The literal this control takes for `truth`, read off the control's own choices.

    The employer supplies the wording of its own Yes/No pair; Python picks the member that
    matches the computed truth. No model-supplied string is consulted, here or anywhere
    else, so there is no field in which a model can write the affirmative.
    """
    choices = question.get("choices") if isinstance(question.get("choices"), list) else []
    wanted = AFFIRMATIVE_LITERALS if truth else NEGATIVE_LITERALS
    for choice in choices:
        if isinstance(choice, str) and applications.normalized(choice).rstrip(".!") in wanted:
            return choice
    if choices:
        return None
    if answer_type_of(question) in {"boolean", "choice"}:
        return "Yes" if truth else "No"
    return truth


def evaluate(entry, question, key, policy, packet, attempt_id, now):
    """One model entry, validated. Returns (record, verdict). record is None for unknown.

    The model's `value` is the answer for every class (owner, 2026-09-28), work authorization
    and sponsorship included (owner, 2026-10-21). Python only fits it to the control.
    `affirmative_value` / `negative_value` are never read.
    """
    verdict = {"question_key": key, "question_class": entry["question_class"],
               "status": entry["status"], "derivation": entry.get("derivation")}
    if entry.get("question_semantic") != question.get("semantic"):
        return None, dict(verdict, outcome="UNKNOWN", reason="SEMANTIC_MISMATCH")
    if entry["status"] == "WITHHELD_BY_POLICY":
        return None, dict(verdict, outcome="UNKNOWN", reason="WITHHELD_BY_POLICY")
    if entry["status"] == "UNKNOWN":
        return None, dict(verdict, outcome="UNKNOWN",
                          reason=entry.get("unknown_reason") or "MODEL_REPORTED_UNKNOWN")
    # Owner, 2026-09-28: "remove this remaining python check. Let AI solely give the answers based
    # on my preferences. ... I trust it more than i trust Python." Every class the AI answers is
    # taken as answered: no question-type rule, no allowed-sources rule, no citation check.
    # Python only fits the value to the control. Work authorization and sponsorship included:
    # the deferral to a Python reading of the jurisdiction map (`computed_authorization`, its
    # exact-code lookup with the "*" fallback and its JURISDICTION_CONTRADICTS_QUESTION /
    # JURISDICTION_NOT_NAMED_AND_NOT_THE_ROLE_COUNTRY / QUESTION_NAMES_MULTIPLE_JURISDICTIONS
    # refusals) was removed (owner, 2026-10-21: "remove those blockers"): it could not read the
    # map's "EU" line as covering Germany or France, so it answered those questions untruly.
    value, attribution, basis, reason = model_value(entry, question)
    if reason:
        return None, dict(verdict, outcome="UNKNOWN", reason=reason)
    attribution = list(entry.get("attribution") or []) + list(attribution or [])
    source = (AI_SOURCE_PREFIX + attempt_id + " basis=" + basis + " class=" + entry["question_class"]
              + " cites=" + ",".join(str(citation.get("pointer")) for citation in attribution if isinstance(citation, dict)))
    record = {"value": value, "semantic": question.get("semantic"), "attribution": attribution,
              "question_id": question.get("id"), "question_key": key, "source": source,
              "question_class": entry["question_class"], "basis": basis,
              "derivation": entry.get("derivation")}
    return record, dict(verdict, outcome="ANSWERED", reason=None, value=value, basis=basis,
                        cites=[citation.get("pointer") for citation in attribution if isinstance(citation, dict)])


def model_value(entry, question):
    """The model's own answer, fitted to the control. No judgement about the question.

    A boolean is rendered through the employer's own `choices` (`_boolean_literal`), so the
    model decides the truth and the form receives the employer's wording, not the model's.
    Anything else is passed through `conform_value`, which is the same check every producer
    in this module has always passed: does this value fit this control.
    """
    value = entry.get("value")
    if isinstance(value, bool):
        value = _boolean_literal(question, value)
    try:
        value = conform_value(value, question)
    except ValueError as error:
        return None, None, "REFUSED", "MODEL_" + str(error)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, None, "REFUSED", "MODEL_SUPPLIED_NO_VALUE"
    return value, [], "MODEL_ANSWERED", None



# The question-place parser (`PLACE_PREPOSITION_PATTERN`, `PLACE_ALIASES`, `bound_place`,
# `PLACE_ALTERNATIVE_PATTERN`, `question_places`, `question_place`) was deleted on 2026-09-22.
# It read place names out of the employer's English to decide a willingness answer, and it got
# Vandelay Security wrong: "Our offices are in Springfield, IL and Austin" joined alternatives on "or" and "/" only, so
# "and" truncated the pair to "Springfield", "Springfield" did not equal the role's recorded city, and an
# unrecognised place was reported as recorded guidance not deciding the question - while
# `location_priority` line 3 decides it plainly. The model reads the question now
# (`judged_preference`). `role_place_excluded` went with them: its structured branch read
# `location_options`, which `judged_preference` now calls directly, and its fallback branch
# phrase-matched free-text city strings - the same defect one layer down.

# `relocation_destination`, `willing_to_work_onsite`, `binding_jurisdiction`,
# `willing_to_relocate` and `willing_to_work_remotely` were deleted on 2026-09-22 with the
# parser above: each one existed solely to compute a preference literal that the model now
# supplies. Their only caller was `computed_preference`, which `judged_preference` replaced.

def earliest_start_floor(now, policy):
    """The recorded floor, taken as the later of the local and UTC readings of `now`.

    `applications.earliest_start_date` parses the local date carried in `now`'s offset. A
    timestamp written with a negative offset therefore yields a floor one day earlier than
    the UTC day the application is actually made on. Taking the later of the two can only
    move the floor forwards, which is the safe direction.
    """
    # Delegates to `applications.earliest_start_floor` (owner, 2026-09-22): the max() below used
    # to live only here, while the value `materialize_computed_answers` actually PERSISTED used the
    # bare local reading, so the two disagreed and the corrected value was computed and dropped.
    return applications.earliest_start_floor(now, policy)


def _utc_stamp(now):
    try:
        return dt.datetime.fromisoformat(str(now)).astimezone(dt.timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


def _policy_record(policy, question, key, value, recorded):
    pointer_key = None
    for candidate, entry in ((policy or {}).get("answers") or {}).items():
        if entry is recorded:
            pointer_key = candidate
            break
    attribution = ([{"pointer": "/policy/answers/" + pointer_key + "/value",
                     "quote": value if isinstance(value, str) else json.dumps(value, sort_keys=True)}]
                   if pointer_key else [])
    return {"value": value, "semantic": question.get("semantic"), "attribution": attribution,
            "question_id": question.get("id"), "question_key": key,
            "source": recorded.get("source"), "question_class": "RECORDED_POLICY_ANSWER",
            "basis": "RECORDED_POLICY_ANSWER", "derivation": None}


def answer(database, job, questions, policy, now, adapter=None, *, root=None, config=None,
           review=None):
    """Map a form's required questions to the owner's recorded facts.

    Returns a dict with at least:
      "answers":  {question_key: {"value", "semantic", "attribution", "question_id", "judge"}}
      "unknown":  [question_key, ...]   # retried on a later cycle (ANSWERING_RETRY), or asked on
                                        # Approvals when the verdicts say only the owner can answer
                                        # (`applications.owner_questions`)
      "refused":  [{"question_key", "reason"}]  # the resume, attached out of band
      "audit":    {...}                 # raw model output preserved verbatim

    `answers`, `unknown` and `refused` partition the form's required questions exactly once
    each, keyed by `applications.question_field` - the same null-safe derived key
    `resolve_answers`, the missing-answer blocker and `decision_transport.request_fact`
    already use, so an answer written here is found by the matcher and an owner's saved
    answer for the same question still matches on a later cycle.

    The answering AI answers; a separate AI judge reviews every answer (`_review_rounds`); only
    answers the judge accepts are returned in `answers` (owner, 2026-10-21, option A), or, when
    the judge objects with no untrue fact or a judge/repair call fails, the answering AI's latest
    answer that fits its control (`_over_judge`, `_round_failed`; never one flagged untrue_fact true).

    This function never raises for a model problem. Every failure of the answering call -
    unconfigured, exhausted compute, timeout, malformed output, failed validation - leaves the affected questions in
    `unknown`, which the ANSWERING_RETRY hold retries on a later cycle; the owner is not asked
    about a failure, only about a question the judge agrees has no truthful answer in the packet
    (CANNOT_ANSWER, or an UNKNOWN it accepted; `applications.owner_questions`).
    """
    root = Path(root).resolve() if root is not None else Path(__file__).resolve().parent
    config = config if isinstance(config, dict) else _config(root)
    options = settings(config)
    review = review if isinstance(review, dict) else latest_review(database, job["job_key"])
    result = {"answers": {}, "unknown": [], "refused": [], "computed": {},
              "audit": {"packet_version": PACKET_VERSION, "enabled": options["enabled"],
                        "outcome": "NO_QUESTIONS", "raw": None, "raw_sha256": None,
                        "packet_sha256": None, "prompt_sha256": None, "attempt_id": None,
                        "verdicts": [], "rounds": [], "calls": [],
                        "model": text_model.model(config) if options["enabled"] else None,
                        "job_key": job["job_key"], "created_at": now}}

    resolved, _unused = applications.resolve_answers(questions, policy)
    seen, askable = set(), []
    for question in questions:
        # An optional question is asked too when the owner recorded a yes/no or authorization fact
        # for it (owner, 2026-09-21: a recorded optional answer is filled; since 2026-10-07 the AI
        # applies those facts, not Python - `applications.recorded_answer_for_ai`).
        if not isinstance(question, dict) or not (
                question.get("required") or applications.recorded_fact_for_ai(question, policy)):
            continue
        key = applications.question_field(question)
        if key in seen:
            continue
        seen.add(key)
        # Self-identification and agreement questions are no longer routed by a Python reading of
        # their wording (owner, 2026-10-21): the owner's recorded declarations are in the packet's
        # /policy/answers and the AI answers these questions like any other.
        if key in resolved:
            # `resolve_answers` fills by semantic only facts that are quoted, not asserted
            # (`applications.recorded_answer_for_ai`); yes/no and right-to-work facts come here
            # unresolved and the AI answers them.
            result["answers"][key] = _policy_record(policy, question, key, resolved[key],
                                                    recorded=_recorded_for(policy, question, key))
            continue
        if question.get("semantic") in applications.RESUME_SEMANTICS:
            result["refused"].append({"question_key": key,
                                      "reason": "DESIGNATED_RESUME_ATTACHED_OUT_OF_BAND"})
            continue
        if question.get("semantic") in applications.EARLIEST_START_SEMANTICS:
            floor = earliest_start_floor(now, policy)
            if question.get("choices") and floor:
                # A start-date menu (owner, 2026-10-06, Woodgrove): the AI picks the option that fits
                # the owner's earliest start date, which goes with the question.
                askable.append((key, dict(question, earliest_start_date=floor)))
                continue
            if floor:
                record = _start_date_record(policy, question, key, floor, now)
                result["answers"][key] = record
                result["computed"][key] = record
            else:
                result["unknown"].append(key)
            continue
        askable.append((key, question))

    if not askable:
        return result
    if not options["enabled"]:
        result["audit"]["outcome"] = "DISABLED"
        result["unknown"].extend(key for key, _unused in askable)
        return result

    attempt_id = str(uuid.uuid4())
    result["audit"]["attempt_id"] = attempt_id
    try:
        # Why the last cycle left each still-outstanding question open, so this one does not repeat
        # a mistake already recorded (owner, 2026-09-22).
        carried = prior_rejections(database, job["job_key"], askable)
        packet = build_packet(job, askable, policy, review, root, repair_feedback=carried or None)
        result["audit"]["packet_sha256"] = packet["packet_sha256"]
        result["audit"]["carried_rejections"] = [item["question_key"] for item in carried]
        prompt = prompt_for(root, packet)
        result["audit"]["prompt_sha256"] = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        raw = _booked_call(database, "application-answering:", job, packet, prompt, root, config, options,
                           adapter, now, result, "answer")
        result["audit"]["raw"] = raw
        result["audit"]["raw_sha256"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        entries = parse_output(raw, packet, [key for key, _unused in askable])
    except AnsweringUnavailable as error:
        _unavailable(result, error)
        result["unknown"].extend(key for key, _unused in askable)
        _record_attempt(database, job, result, now)
        return result
    except Exception as error:
        result["audit"]["outcome"] = "ANSWERING_LOCAL_FAILURE:" + type(error).__name__
        result["unknown"].extend(key for key, _unused in askable)
        _record_attempt(database, job, result, now)
        return result

    base = {name: value for name, value in packet.items() if name not in ("repair_feedback", "packet_sha256")}
    pending = {key: (question, entries[key], attempt_id) for key, question in askable}
    result["audit"]["outcome"] = _review_rounds(database, job, pending, base, policy, root, config, options,
                                                adapter, now, result)
    _record_attempt(database, job, result, now)
    return result


def _booked_call(database, prefix, job, packet, prompt, root, config, options, adapter, now, result, role):
    """One model call under its own shared-compute booking, settled when it completes cleanly.

    A booking the pool cannot hold is a compute wait (ANSWERING_COMPUTE_BUDGET_EXHAUSTED); a call
    that fails keeps its whole booking, as for every other worker. Raises AnsweringUnavailable.
    """
    timezone = config.get("timezone", "America/Los_Angeles")
    with database:
        # Idempotent CREATE TABLE IF NOT EXISTS. The reservation table belongs to the shared
        # budget, not to this step; this only guarantees it exists on a database that has not yet
        # run a compute-reserving worker.
        workflow_tasks.initialize(database)
        booking = (prefix + job["job_key"] + ":" + packet["packet_sha256"][:16] + ":"
                   + workflow_tasks.budget_day(now, timezone))
        reserved = workflow_tasks.reserve_compute(database, booking, now, options["timeout_seconds"], timezone,
                                                  options["daily_seconds"])
    if not reserved:
        raise AnsweringUnavailable("ANSWERING_COMPUTE_BUDGET_EXHAUSTED")
    # Each call is settled by its own measured duration, stamped around the call itself, not
    # from the answering step's start (a later round would otherwise be charged every earlier one).
    started = dt.datetime.now(dt.timezone.utc).isoformat()

    def settle(reason):
        with database:
            workflow_tasks.settle_compute(database, booking, started, dt.datetime.now(dt.timezone.utc).isoformat(),
                                          "application answering " + role + " " + reason + "; shared daily pool: "
                                          "unused tail of a cleanly completed reservation")
    try:
        raw = run_model(root, config, prompt, options["timeout_seconds"], adapter, packet, role=role,
                        calls=result["audit"]["calls"])
    except AnsweringUnavailable as error:
        if getattr(error, "settle", False):
            # Cut off at max_tokens (J8): the call ran to its end, so it is charged its duration
            # and still reported ANSWERING_OUTPUT_EXCEEDS_LIMIT. Every other failure keeps its booking.
            settle("cut off at max_tokens")
        raise
    settle("completed")
    return raw


def _unavailable(result, error):
    result["audit"]["outcome"] = error.code
    limit = provider_limits.wait_of(error)
    if limit:
        # A capacity wait, not an unanswerable question: the caller holds the job until the
        # provider's reset instead of asking the owner.
        result["audit"]["provider_wait"] = limit.detail()
    return error.code


def _review_rounds(database, job, pending, base, policy, root, config, options, adapter, now, result):
    """The answer judge and the answering AI's repairs (owner, 2026-10-21, option A).

    Each round the judge sees the answers still under review (with the raw résumé, the answering
    AI's own instructions and the owner's recorded answers and preferences) and returns ACCEPT,
    REVISE or CANNOT_ANSWER for each. An accepted answer that fits its control is kept; REVISE
    answers go back to the answering AI with the judge's reasons, and its new answers are judged
    in the next round. The judge alone decides when nothing more can be fixed: the rounds end when
    it returns no REVISE. MAX_ROUNDS is only a runaway guard ("Make it max, 7 turns"); a REVISE in
    the last round is left unanswered, unless the judge flagged it untrue_fact false
    (`_over_judge`, J6 option B: then the answering AI's latest answer is used, as for a
    CANNOT_ANSWER flagged false). A judge or repair call that fails keeps what was already
    accepted and uses the answering AI's latest answer for the rest, as for an objection with no
    untrue fact (`_round_failed`, G3, owner 2026-10-22), except an answer the judge flagged
    untrue_fact true. Returns the audit outcome.
    """
    accepted, earlier, flagged = [], {}, {}
    for number in range(1, MAX_ROUNDS + 1):
        record = {"round": number, "judge": {"attempt_id": str(uuid.uuid4()), "outcome": None}}
        result["audit"]["rounds"].append(record)
        assessed = {key: evaluate(entry, question, key, policy, base, minted_by, now)
                    for key, (question, entry, minted_by) in pending.items()}
        raw = None
        try:
            review_packet = _review_packet(base, pending, assessed, accepted, earlier, number)
            raw = _booked_call(database, "application-answering-judge:", job, review_packet,
                               judge_prompt_for(root, review_packet), root, config, options, adapter, now,
                               result, "judge")
            record["judge"]["raw"] = raw
            _flag_raw(raw, pending, assessed, flagged)
            verdicts, unbound = parse_judgement(raw, list(pending))
        except Exception as error:
            if raw is not None:
                _flag_raw(raw, pending, assessed, flagged)  # GL3: even an output that fails coverage
            return _round_failed(record["judge"], error, pending, assessed, result, number, flagged)
        record["judge"]["outcome"] = "COMPLETE"
        if unbound:
            record["judge"]["unbound"] = unbound
        record["judge"]["verdicts"] = [verdicts[key] for key in pending]
        revise = []
        for key, (question, entry, _minted_by) in pending.items():
            judged = verdicts[key]
            earlier.setdefault(key, []).append(dict(judged, round=number))
            answered, verdict = assessed[key]
            if judged.get("untrue_fact") is True:
                flagged.setdefault(key, []).append(_answer_marks(entry, answered))
            if judged["verdict"] == "REVISE":
                revise.append(key)
                continue
            if judged["verdict"] == "ACCEPT" and answered is not None:
                answered = dict(answered, judge={"verdict": "ACCEPT", "round": number, "reason": judged["reason"]})
                result["answers"][key] = answered
                accepted.append({"question_key": key, "label": question.get("label"), "value": answered["value"]})
                result["audit"]["verdicts"].append(dict(verdict, judge=judged, round=number))
                continue
            if judged["verdict"] == "CANNOT_ANSWER" and _over_judge(key, question, judged, number, assessed, result):
                continue
            # CANNOT_ANSWER, or an accepted non-answer (UNKNOWN, or a value the control cannot take).
            reason = ("JUDGE_CANNOT_ANSWER: " + judged["reason"] if judged["verdict"] == "CANNOT_ANSWER"
                      else verdict.get("reason") or "MODEL_REPORTED_UNKNOWN")
            result["unknown"].append(key)
            result["audit"]["verdicts"].append(dict(verdict, outcome="UNKNOWN", reason=reason, judge=judged,
                                                    round=number))
        if not revise:
            return "COMPLETE"
        if number == MAX_ROUNDS:
            for key in revise:
                if _over_judge(key, pending[key][0], verdicts[key], number, assessed, result):
                    continue
                result["unknown"].append(key)
                result["audit"]["verdicts"].append(dict(assessed[key][1], outcome="UNKNOWN", round=number,
                                                        judge=verdicts[key],
                                                        reason="JUDGE_ROUND_LIMIT: " + verdicts[key]["reason"]))
            return "COMPLETE"
        repair = {"attempt_id": str(uuid.uuid4()), "outcome": None,
                  "feedback": [{"question_key": key, "previous_answer": _feedback_entry(pending[key][1], base),
                                "reviewer_reason": verdicts[key]["reason"],
                                "reviewer_correction": verdicts[key]["correction"]} for key in revise]}
        record["repair"] = repair
        try:
            packet = dict(base, questions=[question for question in base["questions"]
                                           if question["question_key"] in revise],
                          repair_feedback=repair["feedback"])
            packet["packet_sha256"] = digest(packet)
            raw = _booked_call(database, "application-answering-repair:", job, packet, prompt_for(root, packet),
                               root, config, options, adapter, now, result, "answer")
            repair["raw"] = raw
            entries = parse_output(raw, packet, revise)
        except Exception as error:
            return _round_failed(repair, error, {key: pending[key] for key in revise}, assessed, result, number,
                                 flagged, verdicts)
        repair["outcome"] = "COMPLETE"
        pending = {key: (pending[key][0], entries[key], repair["attempt_id"]) for key in revise}
    return "COMPLETE"


def _over_judge(key, question, judged, number, assessed, result):
    """J6 option B (owner, 2026-10-22: "option B is the correct one"): when the rounds end on a
    REVISE or CANNOT_ANSWER that the judge itself flagged untrue_fact false, the answering AI's
    latest answer is used. Not when the flag is true or missing (an unjudged verdict), and not when
    that latest answer is UNKNOWN or does not fit its control (nothing to use). Returns True when used.
    """
    answered, verdict = assessed[key]
    if judged.get("untrue_fact") is not False or answered is None:
        return False
    result["answers"][key] = dict(answered, judge={"verdict": judged["verdict"], "round": number,
                                                   "reason": judged["reason"], "used_over_judge": True})
    result["audit"]["verdicts"].append(dict(verdict, judge=judged, round=number, used_over_judge=True))
    return True


def _answer_marks(entry, answered):
    """What identifies an answer the judge flagged: its raw value and the value fitted to the control."""
    marks = {json.dumps(entry.get("value"), sort_keys=True, default=str)}
    if answered is not None:
        marks.add(json.dumps(answered["value"], sort_keys=True, default=str))
    return marks


# Not a failed call: the judge or repair call never ran (the shared pool could not hold its
# booking, or the provider refused the turn for usage). The job waits for capacity and the judge
# runs then; the answers are not used without it.
CAPACITY_WAITS = {"ANSWERING_COMPUTE_BUDGET_EXHAUSTED", provider_limits.CODE}


def _truthy_flag(value):
    return value is True or (type(value) is int and value == 1) or (
        isinstance(value, str) and value.strip().lower() in {"true", "yes", "1"})


def raw_untrue_flags(raw, keys):
    """GL3: the supplied keys any raw judge item flags untrue_fact truthy (true, "true"), read from
    every item with a string question_key, including items `parse_judgement` refuses (an extra key,
    a string flag, a duplicate) and an output that binds no verdict at all. A misspelled key binds
    as `_repair_echoed_keys` binds it. Never raises: an unreadable output flags nothing."""
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return set()
    items = value.get("verdicts") if isinstance(value, dict) else None
    if not isinstance(items, list):
        return set()
    marked = [{"question_key": item["question_key"]} for item in items
              if isinstance(item, dict) and isinstance(item.get("question_key"), str)
              and _truthy_flag(item.get("untrue_fact"))]
    _repair_echoed_keys([item for item in marked if item["question_key"] not in keys], keys)
    return {item["question_key"] for item in marked if item["question_key"] in keys}


def _flag_raw(raw, pending, assessed, flagged):
    for key in raw_untrue_flags(raw, list(pending)):
        marks = _answer_marks(pending[key][1], assessed[key][0])
        if marks not in flagged.setdefault(key, []):
            flagged[key].append(marks)


def _round_failed(record, error, pending, assessed, result, number, flagged, verdicts=None):
    """A judge or repair call that failed (ANSWERING_JUDGE_*, an executor error, a timeout).

    G3 (owner, 2026-10-22): treated like the judge objecting with no untrue fact (`_over_judge`,
    J6 option B): the answering AI's latest answer is used when it is ANSWERED and fits its
    control, and the audit records that it went through because the judge call failed. Not used:
    an answer the judge flagged untrue_fact true in this or an earlier round (`flagged`, matched
    by value, so a repair that repeats it still blocks), an UNKNOWN or non-fitting answer, a
    capacity wait (CAPACITY_WAITS: no call was made) and a local failure (our own code, not the
    call). When a repair call fails, `verdicts` are this round's verdicts on the answers sent for
    repair: one whose flag is not exactly false (an unjudged verdict, untrue_fact None) is refused,
    as `_over_judge` refuses it (GL3). What is not used stays unanswered for the next cycle
    (ANSWERING_RETRY)."""
    code = (_unavailable(result, error) if isinstance(error, AnsweringUnavailable)
            else "ANSWERING_LOCAL_FAILURE:" + type(error).__name__)
    record["outcome"] = code
    usable = (isinstance(error, AnsweringUnavailable) and code not in CAPACITY_WAITS
              and not result["audit"].get("provider_wait"))
    for key, (_question, entry, _minted_by) in pending.items():
        answered, verdict = assessed[key] if assessed else (None, {"question_key": key})
        if (usable and answered is not None
                and (verdicts is None or verdicts[key].get("untrue_fact") is False)
                and not any(_answer_marks(entry, answered) & marks for marks in flagged.get(key, []))):
            result["answers"][key] = dict(answered, judge={"verdict": None, "round": number, "judge_failed": code,
                                                           "used_over_judge": True})
            result["audit"]["verdicts"].append(dict(verdict, round=number, judge_failed=code, used_over_judge=True))
            continue
        result["unknown"].append(key)
        result["audit"]["verdicts"].append(dict(verdict, outcome="UNKNOWN", reason=code))
    return code


def _review_packet(base, pending, assessed, accepted, earlier, number):
    """What the judge reads: the fixed context (the raw résumé, the owner's recorded answers and
    preferences, the role) and each answer under review with its question, its control and how it
    fits the control (`form_fit`, a fact for the judge, not a refusal)."""
    questions = {question["question_key"]: question for question in base["questions"]}
    reviewed = []
    for key, (_question, entry, _minted_by) in pending.items():
        answered, verdict = assessed[key]
        if answered is not None:
            fit = {"fits": True, "value_entered": answered["value"], "problem": None}
        elif entry.get("status") == "ANSWERED":
            fit = {"fits": False, "value_entered": None, "problem": verdict.get("reason")}
        else:
            fit = {"fits": None, "value_entered": None, "problem": None}
        reviewed.append({"question": questions[key],
                         "answer": {field: entry.get(field) for field in (
                             "status", "question_class", "value", "derivation", "attribution", "unknown_reason")},
                         "form_fit": fit, "earlier_reviews": earlier.get(key, [])})
    packet = {"review_version": JUDGE_VERSION, "round": number, "max_rounds": MAX_ROUNDS,
              "job": base["job"], "policy": base["policy"], "review": base["review"],
              "candidate_resume": base.get("candidate_resume"),
              "answers_to_review": reviewed, "accepted_answers": list(accepted)}
    packet["packet_sha256"] = digest(packet)
    return packet


UNJUDGED_REASON = "The reviewer returned no usable verdict for this answer; answer it again (keep it if it is right)."


def parse_judgement(raw, keys):
    """The judge's verdicts by question key, and the items that bound to none.

    Structure only, like `parse_output`, but one malformed or unbound verdict does not discard the
    round (owner, 2026-10-22, J3): every well-formed verdict whose key binds to a supplied key (the
    first one for a key, after `_repair_echoed_keys`) is kept. A supplied key left without one is
    returned as REVISE with no correction, so the existing loop sends it back to the answering AI
    and the judge reads it in the next round. Only an output that binds no verdict at all fails.
    """
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        raise AnsweringUnavailable("ANSWERING_JUDGE_OUTPUT_NOT_JSON") from None
    items = value.get("verdicts") if isinstance(value, dict) else None
    if not isinstance(items, list):
        raise AnsweringUnavailable("ANSWERING_JUDGE_SCHEMA_INVALID")
    shaped = [item for item in items
              if isinstance(item, dict) and set(item) == {"question_key", "verdict", "reason", "correction", "untrue_fact"}
              and isinstance(item["question_key"], str) and item["verdict"] in JUDGE_VERDICTS
              and isinstance(item["untrue_fact"], bool)
              and isinstance(item["reason"], str)
              and (item["correction"] is None or isinstance(item["correction"], str))]
    _repair_echoed_keys(shaped, keys)
    verdicts = {}
    for item in shaped:
        if item["question_key"] in keys and item["question_key"] not in verdicts:
            verdicts[item["question_key"]] = item
    if not verdicts:
        raise AnsweringUnavailable("ANSWERING_JUDGE_COVERAGE_INVALID")
    unbound = [item for item in items if not any(item is kept for kept in verdicts.values())]
    for key in keys:
        if key not in verdicts:
            verdicts[key] = {"question_key": key, "verdict": "REVISE", "reason": UNJUDGED_REASON,
                             "correction": None, "untrue_fact": None, "unjudged": True}
    return verdicts, unbound


def _feedback_entry(entry, packet):
    """The previous answer, with only citations that resolve inside the packet the model saw."""
    kept = []
    for citation in entry.get("attribution") or []:
        try:
            at_pointer(packet, citation["pointer"])
            kept.append(citation)
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return dict(entry, attribution=kept)


def prior_rejections(database, job_key, askable):
    """The last cycle's reason for each question still unanswered.

    Owner, 2026-09-22: reuse anything not stale or expired. The last attempt's final verdicts
    (the judge's reason, or why the call failed) are carried into this cycle's packet for the
    questions still outstanding, so the answering AI does not repeat a mistake already named.
    """
    outstanding = {key for key, _question in askable}
    if not outstanding:
        return []
    try:
        row = database.execute("SELECT audit FROM application_answering_attempts WHERE job_key=? "
                               "ORDER BY rowid DESC LIMIT 1", (job_key,)).fetchone()
    except sqlite3.Error:
        # The table is created by this module's own `initialize`; a caller that has not run it
        # simply has no prior cycle to carry. Never fail the answering step over an optimisation.
        return []
    if not row:
        return []
    try:
        audit = json.loads(row["audit"] if isinstance(row, sqlite3.Row) else row[0])
    except (TypeError, ValueError, IndexError, KeyError):
        return []
    reasons = {}
    for verdict in audit.get("verdicts") or []:
        key = verdict.get("question_key")
        if key in outstanding and verdict.get("outcome") != "ANSWERED" and verdict.get("reason"):
            reasons[key] = str(verdict["reason"])
    for repair in audit.get("repairs") or []:  # rows written before the judge (2026-10-21)
        for item in repair.get("feedback") or []:
            key = item.get("question_key")
            if key in outstanding and item.get("rejection_reason"):
                reasons[key] = str(item["rejection_reason"])
    return [{"question_key": key, "rejection_reason": reason,
             "from": "a previous cycle; the answer it refused is not carried, only why"}
            for key, reason in sorted(reasons.items())]


def _recorded_for(policy, question, key):
    answers = applications.answers_for_model((policy or {}).get("answers"))   # the one in force
    recorded = answers.get(key)
    if isinstance(recorded, dict):
        return recorded
    for entry in answers.values():
        if isinstance(entry, dict) and entry.get("semantic") == question.get("semantic") and entry.get("source"):
            return entry
    return {}


def _start_date_record(policy, question, key, floor, now):
    rule = applications.earliest_start_rule(policy) or {}
    pointer_key = None
    for candidate, entry in ((policy or {}).get("answers") or {}).items():
        if isinstance(entry, dict) and entry.get("semantic") == applications.EARLIEST_START_RULE_SEMANTIC:
            pointer_key = candidate
            break
    return {"value": floor, "semantic": question.get("semantic"),
            "attribution": [{"pointer": "/policy/answers/" + pointer_key + "/value/offset_days",
                             "quote": str(rule.get("offset_days"))}] if pointer_key else [],
            "question_id": question.get("id"), "question_key": key,
            "source": applications.COMPUTED_ANSWER_SOURCE_PREFIX
                      + applications.EARLIEST_START_RULE_SEMANTIC + ":application_date="
                      + str(now)[:10] + "+" + str(rule.get("offset_days")) + "d",
            "question_class": "AVAILABILITY", "basis": "POLICY_RULE_COMPUTED", "derivation": None}


def _record_attempt(database, job, result, now):
    audit = result["audit"]
    try:
        with database:
            initialize(database)
            database.execute(
                "INSERT INTO application_answering_attempts VALUES (?,?,?,?,?,?,?,?,?,?)",
                (audit["attempt_id"] or str(uuid.uuid4()), job["job_key"], now,
                 audit["packet_sha256"] or "", audit["prompt_sha256"], audit["raw"],
                 audit["raw_sha256"], audit["model"], audit["outcome"],
                 json.dumps({"verdicts": audit["verdicts"],
                             "answered": sorted(result["answers"]),
                             "unknown": sorted(result["unknown"]),
                             # Every judge and repair round, with its raw output, and every
                             # call's usage or failure (a hand-picked key list once dropped the
                             # repair turns, so Vandelay Security's 2026-09-22 refusal could not
                             # be diagnosed from its own audit row).
                             "rounds": audit.get("rounds"),
                             "calls": audit.get("calls"),
                             "refused": result["refused"]}, sort_keys=True)))
    except Exception:
        # The audit is evidence, never a gate. A job must not fail because a row could not
        # be written; the caller still decides from `result` (retry, or ask the owner).
        pass


def _config(root):
    try:
        value = resources.read_json("config.json")
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def materialize(database, job, questions, scoped_policy, now, adapter=None, *, root=None,
                config=None, review=None):
    """Write the AI-derived answers into `application_answers` and into `scoped_policy`.

    Contractually non-raising: the application cycle must behave exactly as it does today if
    anything here fails. An existing answer with any other provenance - an authenticated
    owner decision, the computed start date - is never overwritten.
    """
    try:
        result = answer(database, job, questions, scoped_policy, now, adapter, root=root,
                        config=config, review=review)
    except Exception:
        return {"answers": {}, "unknown": [], "refused": [], "computed": {},
                "audit": {"outcome": "ANSWERING_STEP_FAILED", "raw": None, "verdicts": []}}
    written = []
    try:
        with database:
            for key, record in result["answers"].items():
                source = str(record.get("source") or "")
                if not source.startswith((AI_SOURCE_PREFIX, DECLARATION_SOURCE_PREFIX)):
                    continue
                existing = scoped_policy["answers"].get(key)
                if isinstance(existing, dict) and existing.get("source") and not str(
                        existing["source"]).startswith((AI_SOURCE_PREFIX, DECLARATION_SOURCE_PREFIX)):
                    continue
                database.execute(
                    """INSERT INTO application_answers VALUES (?,?,?,?,?)
                       ON CONFLICT(job_key,field) DO UPDATE SET semantic=excluded.semantic,
                       value=excluded.value,source=excluded.source""",
                    (job["job_key"], key, record["semantic"], json.dumps(record["value"]),
                     record["source"]))
                scoped_policy["answers"][key] = {"semantic": record["semantic"],
                                                 "value": record["value"],
                                                 "source": record["source"]}
                written.append(key)
    except Exception:
        return result
    result["written"] = written
    return result
