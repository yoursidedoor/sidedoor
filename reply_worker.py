"""Bounded, subscription-only reply decisions; never a sender.

cycle owns its transactions: the caller must commit before calling. Consumers
read reply_worker_results.handoff for VERSION and recheck source bindings before
applying decisions. Workflow results/events contain only safe audit references.
Private exact prompts and outputs live exclusively in the immutable reply tables.
"""

import resources
import datetime as dt
import hashlib
import json
import traceback
import os
import re
from pathlib import Path
import signal
import subprocess
import tempfile
import time

import pipeline_routing as routing
import portable
import champion_composition
import provider_limits
import reply_resume
import reply_tools
import text_model
import workflow_tasks


VERSION = "reply-decision-v2"
REASONS = (
    "UNSUPPORTED_CONTEXTUAL_CLASSIFICATION_EXECUTOR:literal-whole-message-required",
    "BOUNDED_DIRECT_MESSAGE_CLASSIFIER_REQUIRED",
    "QUOTED_OR_FORWARDED_MESSAGE_REQUIRES_REVIEW",
)
# INTERVIEW and REJECTION added 2026-09-23 (owner). Without them an interview invitation
# could only be QUESTION or OTHER, and `conversion_interviews` could never be populated
# from a reply at all - the table is empty and was structurally unreachable.
LABELS = {"AGREEMENT", "DECLINE", "AMBIGUOUS", "QUESTION", "INTERVIEW", "REJECTION",
          "QUOTED_OR_FORWARDED", "OTHER"}
FACTS = {"SENDER_IDENTITY", "ROLE_ASSOCIATION", "QUOTED_CONTEXT", "FORWARDED_CONTEXT",
         "AGREEMENT_INTENT", "CANDIDATE_FACTS", "OTHER"}
# Raised from 65536 (owner, 2026-09-24). Three real conversations were held just over the old
# cap - their saved prompts were 63.6-64.2 KB and grew past 64 KB as context was added - so
# ordinary replies went unread. The cap still exists and still holds anything larger unread:
# it is what keeps an arbitrarily long message someone else wrote from reaching the model whole.
MAX_INPUT_BYTES = 131072
# Recent messages of the thread given to the reply AI besides the initial invitation (owner, 2026-10-22:
# "Lets make it last 16 messages + initial outreach message"; was 8).
CONTEXT_MESSAGES = 16
MAX_OUTPUT_BYTES = 49152
# Raised from 2000 and measured compact (2026-10-01: a post-cutoff review was BLOCKED on its
# first attempt). Of 84 recorded outputs the median was 1306 characters and the 90th percentile
# 1789. That one was 2720 as written (2758 re-serialised) because literal_quote and
# quote_span.text each repeated 668 characters of the message, and one backlog output was
# 13828 characters of which 12581 were whitespace padding
# (1443 compact). The size is now the compact JSON (padding is not content) and the cap is the
# schema's own field bounds with room to spare. Exceeding it, or any field bound below, is a
# retry with feedback to the model (OUTPUT_SIZE_FEEDBACK) within the task's attempts, never a
# terminal BLOCKED.
MAX_OUTPUT_CHARACTERS = 6000
MAX_QUOTE_CHARACTERS = 300
MAX_REASON_CHARACTERS = 500
SIZE_BLOCKERS = frozenset({"CLASSIFICATION_OUTPUT_EXCEEDS_LIMIT", "CLASSIFICATION_FIELD_TOO_LONG"})


class Held(Exception):
    def __init__(self, code, retry=False):
        self.code = code
        self.retry = retry
        super().__init__(code)


_SECRET_NAMES = (r"authorization|proxy-authorization|x-api-key|api[_-]?key|apikey|access[_-]?token"
                 r"|refresh[_-]?token|token|secret|password|passwd|key")
# A secret field in any spelling: bare, JSON or quoted name; ':' or '='; a quoted or bare value,
# optionally after a Bearer/Basic/Token scheme word.
# A quote may be backslash-escaped (JSON inside a string: {\"token\": \"...\"}, E6.T7 review F10).
_SECRET_FIELD = re.compile(r"(?i)((?:\\?[\"'])?\b(?:" + _SECRET_NAMES + r")\b(?:\\?[\"'])?\s*[:=]\s*)"
                           r"(?:(?:bearer|basic|token)\s+)?(?:\\\"(?:(?!\\\").)*(?:\\\")?|\\'(?:(?!\\').)*(?:\\')?"
                           r"|\"[^\"]*\"?|'[^']*'?|[^\s,;&\"'}\]]+)")
_SECRET_PROSE = re.compile(r"(?i)\b(password|passwd|passcode|secret|token|api[ _-]?key)(\s+is\s+)\S+")
_COOKIE = re.compile(r"(?i)\b((?:set-)?cookie\s*:\s*).*")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")
_SCHEME_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")
# A scheme-less host with a path or query ("openrouter.ai/settings/keys?k=...").
_BARE_URL = re.compile(r"(?i)\b(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d+)?(?:/\S*|\?\S*)")
# A line codex itself wrote at error/warning level: its "ERROR: ..." line, or its tracing log
# prefix ("2026-10-03T19:15:01.123Z ERROR ..."). A model's "Error ..."/"Warning ..." prose is not.
_CODEX_LEVEL = re.compile(r"^(?:(?:ERROR|WARN|WARNING):\s|\[?\d{4}-\d{2}-\d{2}T[0-9:.]+Z?\]?\s+(?:ERROR|WARN)\b)")
# codex exec's transcript markers: after "codex"/"thinking" comes the model's own output, which
# is never kept (after "user" comes the echoed prompt, dropped line by line and in the fallback).
_TRANSCRIPT_MARKERS = ("codex", "thinking")
_KEY_FRAGMENT = 8
_TAIL_CHARACTERS = 65536


def _redacted_lines(text, secrets=(), prompt=None):
    """A bounded, redacted tail of an executor's stderr: codex's own ERROR/WARN lines (else the
    last 5 lines before codex's "user" prompt echo), at most 8 lines of 300 characters.

    Never stored: a line of the prompt itself, a partial first line of the bounded tail, anything
    after the model transcript markers ("codex"/"thinking"), anything after the "user" prompt echo
    in the fallback, a line holding any 8-character fragment of a configured secret outside a URL
    (a key split across lines), URLs with or without a scheme, sk- keys, JWTs, cookies,
    Authorization/Bearer/Basic values and key/token/secret/password fields (bare, JSON or quoted,
    or "password is ...")."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    text = text or ""
    if len(text) > _TAIL_CHARACTERS:
        text = text[-_TAIL_CHARACTERS:]
        text = text.split("\n", 1)[1] if "\n" in text else ""  # the slice began mid-line
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    marker = next((index for index, line in enumerate(lines) if line in _TRANSCRIPT_MARKERS), None)
    if marker is not None:
        lines = lines[:marker]
    prompt_lines = {line.strip() for line in (prompt or "").splitlines() if line.strip()}
    lines = [line for line in lines if line not in prompt_lines]
    fragments = secret_fragments(secrets)
    chosen = [line for line in lines if _CODEX_LEVEL.match(line)]
    if not chosen:
        if "user" in lines:
            lines = lines[:lines.index("user")]
        chosen = lines[-5:]
    result = []
    for line in chosen:
        line = redact_line(line, secrets, fragments)
        if line is None:
            continue
        line = line[:300]
        if not result or result[-1] != line:
            result.append(line)
    return result[-8:]


def secret_fragments(secrets):
    """Every 8-character piece of each configured secret (`redact_line` drops a line holding one)."""
    return {secret[index:index + _KEY_FRAGMENT] for secret in secrets if secret and len(secret) >= _KEY_FRAGMENT
            for index in range(len(secret) - _KEY_FRAGMENT + 1)}


def redact_line(line, secrets=(), fragments=()):
    """One line with secrets removed, or None when it must be dropped whole: the configured
    `secrets` replaced, URLs with or without a scheme, cookies, JWTs, sk- keys, key/token/secret/
    password fields and Bearer/Basic values redacted; a line still holding any of `fragments`
    (`secret_fragments`) outside a URL is dropped. Shared with the dashboard's log view (E6.T7)."""
    for secret in secrets:
        if secret:
            line = line.replace(secret, "<redacted>")
    line = _SCHEME_URL.sub("<url>", line)
    line = _BARE_URL.sub("<url>", line)
    if any(fragment in line for fragment in fragments):
        return None  # part of a key outside a URL (e.g. split across lines): drop the line
    line = _COOKIE.sub(lambda match: match.group(1) + "<redacted>", line)
    line = _JWT.sub("<redacted-jwt>", line)
    line = re.sub(r"\bsk-[A-Za-z0-9_-]{6,}", "<redacted-key>", line)
    line = _SECRET_FIELD.sub(lambda match: match.group(1) + "<redacted>", line)
    line = _SECRET_PROSE.sub(lambda match: match.group(1) + match.group(2) + "<redacted>", line)
    return re.sub(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}", r"\1 <redacted>", line)


def executor_failure(returncode, seconds, stderr, config=None, prompt=None):
    """What a failed `codex exec` said, kept so the failure can be diagnosed (2026-10-02: the
    SUBSCRIPTION_EXECUTOR_FAILED runs of 10-03/04 left no record; the posting-judge logs, which
    keep stderr, show OpenRouter 402 Payment Required from 2026-10-03T19:15Z)."""
    secrets = []
    try:
        if text_model.settings(config):
            secrets.append(text_model.key(config))
    except (OSError, TypeError, AttributeError):
        pass
    return {"exit_code": returncode, "duration_seconds": round(seconds, 1),
            "stderr_tail": _redacted_lines(stderr, secrets, prompt)}


def executor_detail(error, depth=8):
    """The `executor_failure` record carried by `error` or its cause/context chain, else None."""
    seen = 0
    while error is not None and seen < depth:
        if isinstance(getattr(error, "executor", None), dict):
            return error.executor
        error = error.__cause__ or error.__context__
        seen += 1
    return None


def sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def initialize(database):
    statements = (
        "CREATE TABLE IF NOT EXISTS reply_worker_inputs (task_id TEXT PRIMARY KEY, input_sha256 TEXT NOT NULL, input TEXT NOT NULL, source_sha256 TEXT NOT NULL, provenance TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS reply_worker_attempts (task_id TEXT NOT NULL, attempt INTEGER NOT NULL, observed_at TEXT NOT NULL, input_sha256 TEXT, output TEXT, output_sha256 TEXT, provenance TEXT NOT NULL, blocker TEXT, PRIMARY KEY(task_id,attempt))",
        "CREATE TABLE IF NOT EXISTS reply_worker_results (task_id TEXT PRIMARY KEY, output_version TEXT NOT NULL, input_sha256 TEXT NOT NULL, output_sha256 TEXT NOT NULL, handoff TEXT NOT NULL)",
    )
    for statement in statements:
        database.execute(statement)
    for table in ("reply_worker_inputs", "reply_worker_attempts", "reply_worker_results"):
        for operation in ("UPDATE", "DELETE"):
            database.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, 'Immutable reply audit'); END")


def _options(config):
    settings = config.get("reply_worker", {})
    timeout = settings.get("timeout_seconds", 60)
    limit = settings.get("max_items", 4)
    budget = config.get("daily_compute_seconds", 10800)
    if (type(timeout) is not int or not 1 <= timeout <= 600 or type(limit) is not int
            or not 1 <= limit <= 20 or type(budget) is not int or not timeout <= budget <= 21000):
        raise Held("INVALID_REPLY_WORKER_BOUNDS")
    if any(not isinstance(config.get(field), str) or not config[field].strip() or config[field].startswith("-")
           for field in ("codex", "model")):
        raise Held("EXISTING_SUBSCRIPTION_CONFIGURATION_REQUIRED")
    return timeout, limit, budget


def _defer_capacity(database, task_id, config, now, timeout, budget):
    current = workflow_tasks.instant(now)
    zone = config.get("timezone", "America/Los_Angeles")
    day = workflow_tasks.budget_day(current, zone)
    reset = workflow_tasks.next_day_start(current, zone, workflow_tasks.day_start_hour(config)).astimezone(dt.timezone.utc)
    database.execute("BEGIN IMMEDIATE")
    with database:
        task = routing.one(database, "SELECT * FROM workflow_tasks WHERE task_id=?", (task_id,))
        if task["status"] not in {"QUEUED", "RETRY_WAIT"}:
            return None
        reservation = routing.one(database, "SELECT day,seconds FROM worker_compute_reservations WHERE action_id=?",
                                  ("reply:" + task_id + ":" + str(task["attempts"] + 1),))
        if reservation == {"day": day, "seconds": timeout}:
            return None
        reserved = workflow_tasks.compute_used(database, day)
        if reserved + timeout <= budget:
            return None
        # Only the tries end a reply review now; it has no deadline (owner, 2026-09-28).
        exhausted = task["attempts"] >= task["max_attempts"]
        state, next_at = ("EXHAUSTED", None) if exhausted else ("RETRY_WAIT", reset.isoformat())
        next_action = ("Attempt limit reached while waiting for shared capacity; retain failures for review"
                       if exhausted else "Resume read-only reply interpretation after shared capacity resets at " + next_at)
        database.execute("UPDATE workflow_tasks SET status=?,blocker='DAILY_COMPUTE_BUDGET_EXHAUSTED',next_action=?,next_at=?,updated_at=? WHERE task_id=?",
                         (state, next_action, next_at, now, task_id))
        workflow_tasks.record(database, task_id, "CAPACITY_DEFERRED", now,
                              {"reset_at": reset.isoformat(), "attempts": task["attempts"], "deadline": task["deadline"]})
        return {"task_id": task_id, "state": state, "blocker": "DAILY_COMPUTE_BUDGET_EXHAUSTED",
                "next_action": next_action, "next_at": next_at, "attempts": task["attempts"], "deadline": task["deadline"]}


def _source(database, task, config, now):
    current_task = routing.one(database, "SELECT * FROM workflow_tasks WHERE task_id=?", (task["task_id"],))
    if not current_task or any(current_task[field] != task[field] for field in
                               ("inputs", "action_id", "parent_run_id", "effect", "deadline", "max_attempts")):
        raise Held("CLASSIFICATION_TASK_CHANGED")
    inputs = json.loads(task["inputs"])
    # `<= 7`, matching the raised task ceiling (owner, 2026-09-23). This is the SECOND place a
    # hardcoded 3 turned into a silent filter: the candidate query at :656 selected the task and
    # then this rejected it as INVALID_CLASSIFICATION_TASK, so every 7-attempt review was picked
    # up and immediately blocked without the model ever being called.
    # No upper bound: the 7-try base grows by one per provider/compute-wait refund (2026-09-28).
    if len(inputs) != 1 or task["effect"] != "READ" or not 1 <= task["max_attempts"]:
        raise Held("INVALID_CLASSIFICATION_TASK")
    bound = inputs[0]
    stored = routing.one(database, "SELECT * FROM pipeline_routing_sources WHERE source_key=?", (bound["source_key"],))
    if not stored or stored["source_table"] != "engagement_events":
        raise Held("CLASSIFICATION_SOURCE_MISSING")
    if (bound["reason"] not in REASONS or stored["blocker"] != bound["reason"] or stored["status"] != "REVIEW"
            or task["action_id"] != routing.reply_review_action(database, stored, bound)
            or task["parent_run_id"] != "pipeline-routing:" + stored["source_key"]
            or bound["source"] != "engagement_events:" + stored["source_id"]
            or bound["observed_at"] != stored["first_seen_at"]):
        raise Held("CLASSIFICATION_SOURCE_BINDING_CHANGED")
    snapshot = json.loads(stored["snapshot"])
    if bound["content_sha256"] != stored["content_sha256"] or routing.digest(snapshot) != stored["content_sha256"]:
        raise Held("CLASSIFICATION_SOURCE_HASH_MISMATCH")
    source, source_id = json.loads(stored["source_id"])
    live = routing.one(database, "SELECT * FROM engagement_events WHERE source=? AND source_id=?", (source, source_id))
    if live != snapshot or routing.one(database, "SELECT 1 FROM pipeline_routing_conflicts WHERE source_key=?", (stored["source_key"],)):
        raise Held("CLASSIFICATION_SOURCE_CHANGED")
    request = routing.classification_request(database, source, source_id)
    if request.get("state") == "BLOCKED":
        raise Held("CLASSIFICATION_INPUT_EXCEEDS_LIMIT")
    if request["source_key"] != stored["source_key"] or request["source_sha256"] != bound["content_sha256"]:
        raise Held("CLASSIFICATION_REQUEST_BINDING_MISMATCH")
    try:
        routing._inbox(snapshot, config.get("pipeline_routing", {}), now)
    except ValueError as error:
        if str(error) not in REASONS:
            raise Held("AUTHENTICATED_MESSAGE_BINDING_REQUIRED") from None
    inbox = json.loads(snapshot["payload"])["inbox"]
    message = inbox["text"]
    if not isinstance(message, str) or not message.strip():
        raise Held("ACTUAL_MESSAGE_TEXT_REQUIRED")
    profile = routing.research.profile_key(inbox["sender_profile"])
    # A member-id sender spelling resolved to a tracked person (outreach.resolve_sender): that
    # person's roles, conversation, invitation and referral are this conversation's context too.
    people = [profile] + [person for person in [resolved_person(database, snapshot)] if person and person != profile]
    bindings, bound_jobs = [], set()
    for person in people:
        for binding in routing._bindings(database, routing.rows(database, "SELECT * FROM rw_results"), person, now):
            if binding["job"]["job_key"] not in bound_jobs:
                bound_jobs.add(binding["job"]["job_key"])
                bindings.append(binding)
    legacy_request = False
    previous = None
    if database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reply_worker_inputs'").fetchone():
        saved = routing.one(database, "SELECT * FROM reply_worker_inputs WHERE task_id=?", (task["task_id"],))
        if saved:
            try:
                previous = json.loads(saved["input"].split("\n\nUNTRUSTED_SOURCE_JSON:\n", 1)[1])
                if sha(saved["input"]) != saved["input_sha256"] or saved["source_sha256"] != request["source_sha256"]:
                    raise ValueError()
                legacy_request = "champion_handoff_enabled" not in previous
            except (ValueError, KeyError, IndexError, TypeError):
                raise Held("CLASSIFICATION_IMMUTABLE_INPUT_INVALID") from None
    request["champion_handoff_enabled"] = config.get("conversion", {}).get("champion_handoff_enabled") is True
    request["job_scope"] = sorted(
        [{"job_key": binding["job"]["job_key"], "employer": binding["job"]["employer"],
          "requisition": binding["job"]["requisition"], "role": binding["role"]["role"],
          "rw_result_id": binding["rw_result_id"],
          **({"role_context": champion_composition.role_context(database, binding, now)} if not legacy_request else {}),
          **({"candidate_profile": champion_composition.candidate_profile(config),
              "application_interview_context": champion_composition.history_context(database, binding["job"])}
             if request["champion_handoff_enabled"] and not legacy_request else {})}
         for binding in bindings], key=lambda item: item["job_key"])
    # The conversation's company's open jobs on file join the research-bound roles (owner,
    # 2026-09-30: the advocate-summary role-scope bug). Research bindings need this person on a
    # research role's `people` list, which no contacted person is on live data, so job_scope was
    # always empty. role_reference still has to name one of these entries.
    bound_keys = {item["job_key"] for item in request["job_scope"]}
    company_jobs = {}
    for person in people:
        for job_key, job in champion_composition.company_jobs(database, person).items():
            company_jobs.setdefault(job_key, job)
    for job_key, job in company_jobs.items():
        if job_key in bound_keys:
            continue
        try:
            payload = json.loads(job["payload"]) if job["payload"] else {}
        except (ValueError, TypeError):
            payload = {}
        scope = {"job_key": job_key, "employer": job["employer"], "requisition": job["requisition"],
                 "role": payload.get("role"), "rw_result_id": None, "scope_basis": "CONVERSATION_COMPANY_JOB_ON_FILE"}
        if not legacy_request:
            scope["role_context"] = champion_composition.company_role_context(database, job)
            if request["champion_handoff_enabled"]:
                scope["candidate_profile"] = champion_composition.candidate_profile(config)
                scope["application_interview_context"] = champion_composition.history_context(database, job)
        request["job_scope"].append(scope)
    request["job_scope"].sort(key=lambda item: item["job_key"])
    if previous is None or previous.get("application_referral_independent") is True:
        request["application_referral_independent"] = True
        saved_scopes = {scope["job_key"]: scope for scope in (previous or {}).get("job_scope", [])}
        for scope in request["job_scope"]:
            saved_context = saved_scopes.get(scope["job_key"], {}).get("application_status_context")
            scope["application_status_context"] = (saved_context if saved_context is not None else
                                                   champion_composition.application_context(database, scope))
            saved_history = saved_scopes.get(scope["job_key"], {}).get("application_interview_context")
            if saved_history is not None:
                scope["application_interview_context"] = saved_history
    if legacy_request:
        request.pop("champion_handoff_enabled")
    elif request["champion_handoff_enabled"] and (previous is None or "source_span_resolution" in previous):
        request["message_text"] = message
        request["message_full_span"] = {"start": 0, "end": len(message), "text": message}
        # Kept only as the marker that this packet carries message_text; spans are no longer
        # resolved or checked (owner, 2026-09-27).
        request["source_span_resolution"] = "EXACT_UNIQUE_QUOTE"
    request["decision_time"] = bound.get("review_at") or stored["first_seen_at"]
    # The judge's objection to the reply it rejected last time, so this turn fixes that exact
    # problem rather than writing the same thing again (owner, 2026-09-23: "do replies and
    # followups too"). None on a first attempt. Same shape as answering_agent's repair_feedback.
    import outreach_agent
    request["repair_feedback"] = outreach_agent.pending_objection(database, profile, "REPLY")
    request["conversation_context"] = []
    request["initial_invitation"] = None
    request["context_issues"] = []
    holds = []
    context_config = {**config.get("pipeline_routing", {}), "sender_profile": config.get("sender_profile")}
    for contextual in routing.rows(database, "SELECT * FROM engagement_events WHERE identity IN ("
                                   + ",".join("?" for unused in people) + ") AND kind IN ('inbound','outbound_observed',"
                                   "'initial_sent','followup_sent') ORDER BY occurred_at DESC,source,source_id",
                                   (snapshot["identity"], *people[1:])):
        if contextual == snapshot:
            continue
        try:
            try:
                unused_inbox, context_profile = routing._inbox(contextual, context_config, now,
                    direction="inbound" if contextual["kind"] == "inbound" else "outbound")
            except ValueError as error:
                if str(error) not in REASONS:
                    continue
                context_profile = routing.research.profile_key(json.loads(contextual["payload"])["profile"])
            contextual_inbox = json.loads(contextual["payload"])["inbox"]
            invitation = (contextual["kind"] != "inbound"
                and contextual_inbox.get("message_type") == "invitation"
                and routing._path(json.loads(contextual["raw"]),
                    contextual_inbox.get("raw_paths", {}).get("message_type")) == "invitation")
            if (contextual_inbox["account_id"] != inbox["account_id"]
                    or not invitation and contextual_inbox["conversation_id"] != inbox["conversation_id"]
                    or context_profile not in people):
                continue
            if not invitation and len(request["conversation_context"]) == CONTEXT_MESSAGES:
                continue
            context_key = routing.digest([routing.VERSION, "engagement_events",
                routing.encoded([contextual["source"], contextual["source_id"]]), "message"])
            context_source = routing.one(database, "SELECT * FROM pipeline_routing_sources WHERE source_key=?", (context_key,))
            if (routing.one(database, "SELECT 1 FROM pipeline_routing_conflicts WHERE source_key=?", (context_key,))
                    or context_source and (context_source["content_sha256"] != routing.digest(contextual)
                        or context_source["blocker"] == "SOURCE_IDENTITY_CONTENT_CHANGED")):
                holds.append("CONFLICTING_CONVERSATION_CONTEXT")
                request["context_issues"].append({"source_key": context_key, "blocker": "CONFLICTING_CONVERSATION_CONTEXT"})
                continue
            entry = {"snapshot": contextual, "content_sha256": routing.digest(contextual)}
            if invitation:
                request["initial_invitation"] = entry
            else:
                request["conversation_context"].append(entry)
        except (ValueError, KeyError, TypeError):
            continue
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    # This person's referral, whatever job it concerns or none (owner, 2026-09-25). It read the
    # per-job `conversion_referrals` rows for the jobs in job_scope, so a referral without a
    # researched job was invisible to the next reading of the same conversation.
    request["referral_context"] = []
    if "referrals" in tables:
        request["referral_context"] = routing.rows(database, """SELECT state,source_key,statement,next_action,
            review_at,updated_at FROM referrals WHERE contact_id IN (""" + ",".join("?" for unused in people) + ")",
            (snapshot["identity"], *people[1:]))
    for flag in ("quoted", "forwarded"):
        if type(inbox[flag]) is not bool:
            holds.append("UNKNOWN_" + flag.upper() + "_CONTEXT")
        elif inbox[flag]:
            holds.append(flag.upper() + "_CONTEXT_REQUIRES_ADJUDICATION")
    try:
        request["candidate_resume"] = reply_resume.context()
    except Exception as error:
        # Degrade, do not stop (owner, 2026-09-23). A resume hash or page-count mismatch used to
        # `Held` every classification, including replies that never touch the resume. The
        # unavailability is recorded in the packet so the model knows not to quote from it.
        request["candidate_resume"] = {"source": None, "text": None,
                                       "unavailable": str(error)[:160]}
    return request, message, holds


def _environment():
    allowed = {"HOME", "PATH", "CODEX_HOME", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT"}
    return {key: value for key, value in os.environ.items() if key in allowed}


# No core file for a worker (POSIX; None on Windows, where portable.no_crash_dialogs covers it).
resource = portable.resource
_limits = portable.no_core


TOOLS_OUTPUT_INSTRUCTIONS = ("When you are done with the tools, your final message is exactly one JSON object "
                             "and nothing else (no code fence, no text before or after it), valid against this "
                             "JSON Schema:")


def _command(config, schema_path, output_path):
    command = [config["codex"], "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
               "--skip-git-repo-check", "--sandbox", "read-only", "--model", config["model"]]
    for setting in ('approval_policy="never"', 'forced_login_method="chatgpt"', 'model_provider="openai"',
                    'web_search="disabled"', "features.shell_tool=false", "features.apps=false",
                    "features.skills=false", "features.apply_patch_freeform=false", "features.unified_exec=false",
                    "features.multi_agent=false", "mcp_servers={}"):
        command.extend(["-c", setting])
    if config.get("_reply_tools") is not None:
        # The reply AI's tools (owner, 2026-10-06). codex's --output-schema is left off: with it
        # the text model (GLM-5.3 through OpenRouter) never calls a tool, without it it does
        # (probe 2026-10-06). The schema goes in the prompt instead and `_validate` checks the
        # answer as it always has.
        index = command.index("mcp_servers={}")
        del command[index - 1:index + 1]
        for setting in reply_tools.options(Path(schema_path).parent):
            command.extend(["-c", setting])
        command.extend(["--output-last-message", str(output_path), "--color", "never", "-"])
        return text_model.command(config, command)
    command.extend(["--output-schema", str(schema_path), "--output-last-message", str(output_path), "--color", "never", "-"])
    return text_model.command(config, command)


def _execute(config, prompt, schema, timeout):
    with tempfile.TemporaryDirectory(prefix="reply-worker-") as directory:
        working = Path(directory)
        schema_path = working / "schema.json"
        schema_path.write_text(schema, encoding="utf-8")
        output_path = working / "output.json"
        tools = config.get("_reply_tools")
        if tools is not None:
            reply_tools.prepare(working, tools["root"], tools["task_id"])
        def interrupted(signum, frame):
            raise Held("CLASSIFICATION_INTERRUPTED", retry=True)

        previous_handler = signal.signal(signal.SIGTERM, interrupted)
        worker = None
        started = time.monotonic()
        try:
            try:
                # stderr is read into memory only (never persisted) so the provider's usage-limit
                # refusal can be told apart from a failed classification.
                worker = subprocess.Popen(_command(config, schema_path, output_path), stdin=subprocess.PIPE,
                                          stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                                          encoding="utf-8", cwd=working, env=text_model.environment(config, _environment()),
                                          **portable.worker_spawn_options())
                portable.contain(worker)
            except OSError:
                raise Held("SUBSCRIPTION_EXECUTOR_UNAVAILABLE") from None
            try:
                streams = worker.communicate(prompt, timeout=timeout)
            except subprocess.TimeoutExpired:
                raise Held("CLASSIFICATION_TIMEOUT", retry=True) from None
        finally:
            try:
                if worker is not None:
                    portable.kill_tree(worker.pid)
                    if worker.poll() is None:
                        worker.wait(timeout=5)
            finally:
                signal.signal(signal.SIGTERM, previous_handler)
                if tools is not None:
                    tools["calls"] = reply_tools.collect(working)
        if worker.returncode:
            stderr = streams[1] if isinstance(streams, tuple) and len(streams) == 2 else None
            failed = Held("SUBSCRIPTION_EXECUTOR_FAILED", retry=True)
            failed.executor = executor_failure(worker.returncode, time.monotonic() - started, stderr, config, prompt)
            raise failed from provider_limits.check_output(worker.returncode, stderr)
        if not output_path.is_file() or output_path.is_symlink():
            raise Held("CLASSIFICATION_OUTPUT_MISSING")
        with output_path.open("rb") as output:
            content = output.read(MAX_OUTPUT_BYTES + 1)
        if len(content) > MAX_OUTPUT_BYTES:
            # A retry with size feedback, not a terminal hold (owner, 2026-10-01).
            raise Held("CLASSIFICATION_OUTPUT_EXCEEDS_LIMIT", retry=True)
        try:
            text = content.decode("utf-8")
        except UnicodeError:
            raise Held("CLASSIFICATION_OUTPUT_ENCODING_INVALID") from None
        if tools is not None:
            # Without --output-schema the model sometimes wraps its JSON in a code fence.
            fenced = re.fullmatch(r"\s*```(?:json)?\s*\n(.*)\n\s*```\s*", text, re.DOTALL)
            if fenced:
                text = fenced.group(1)
        return text


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Held("CLASSIFICATION_OUTPUT_DUPLICATE_KEYS")
        result[key] = value
    return result


def compact_size(output):
    """Characters of the output as compact JSON: whitespace padding is not content."""
    return len(json.dumps(output, ensure_ascii=False, separators=(",", ":")))


def _too_long(output, quotes=True):
    """Fields over their bound, as {field: length}; empty when every field fits.

    The quotes are bounded in the prompt and schema only. They are recorded, never checked
    (owner, 2026-09-27), so a long quote never refuses an output (quotes=False when validating);
    it is named in the size feedback when the whole answer ran over.
    """
    found = {}
    if not isinstance(output, dict):
        return found
    champion = output.get("champion_handoff") is not None
    bounds = {"reason": MAX_REASON_CHARACTERS, "next_action": 500}
    if quotes:
        bounds["literal_quote"] = MAX_QUOTE_CHARACTERS
    for field, bound in bounds.items():
        if isinstance(output.get(field), str) and len(output[field].strip()) > bound:
            found[field] = len(output[field].strip())
    span = output.get("quote_span")
    if quotes and isinstance(span, dict) and isinstance(span.get("text"), str) and len(span["text"]) > MAX_QUOTE_CHARACTERS:
        found["quote_span.text"] = len(span["text"])
    proposal = output.get("reply_proposal")
    if isinstance(proposal, dict):
        limit = champion_composition.MAX_TEXT_CHARACTERS if champion else 500
        if isinstance(proposal.get("text"), str) and len(proposal["text"]) > limit:
            found["reply_proposal.text"] = len(proposal["text"])
        basis = proposal.get("basis_quote")
        if (quotes and isinstance(basis, dict) and isinstance(basis.get("text"), str)
                and len(basis["text"]) > MAX_QUOTE_CHARACTERS):
            found["reply_proposal.basis_quote.text"] = len(basis["text"])
    return found


def size_feedback(database, task_id):
    """The harness's note to the model after this task's last attempt was too long, or None.

    Derived only from the immutable attempt row (its blocker and saved output), so it is the same
    on every re-derivation. It is prepended to the executed prompt and never saved as the task's
    input: the saved input and its hash stay the request itself.
    """
    # The latest attempt that produced an answer or a size failure decides. Rows with neither
    # (a provider or compute wait, a timeout) are skipped, so a wait between a too-long answer
    # and its retry does not drop the feedback.
    last = routing.one(database, """SELECT blocker,output FROM reply_worker_attempts WHERE task_id=?
        AND (output IS NOT NULL OR blocker IN (""" + ",".join("?" for unused in SIZE_BLOCKERS) + """))
        ORDER BY attempt DESC LIMIT 1""", (task_id, *sorted(SIZE_BLOCKERS)))
    if not last or last["blocker"] not in SIZE_BLOCKERS:
        return None
    raw, output = last["output"], None
    if raw is not None:
        try:
            output = json.loads(raw)
        except (ValueError, RecursionError):
            output = None
    size = (str(compact_size(output)) + " characters" if output is not None
            else str(len(raw)) + " characters" if raw is not None else "more than " + str(MAX_OUTPUT_BYTES) + " bytes")
    fields = _too_long(output)
    detail = ("; ".join(field + " was " + str(length) + " characters" for field, length in sorted(fields.items()))
              if fields else None)
    return ("OUTPUT_SIZE_FEEDBACK (from this harness, not from the message): your previous answer to this "
            "same request was " + size + " and could not be used because it was too long"
            + (" (" + detail + ")" if detail else "") + ". The whole answer must be at most "
            + str(MAX_OUTPUT_CHARACTERS) + " characters (" + str(champion_composition.MAX_OUTPUT_CHARACTERS)
            + " with a champion handoff). Write the same decision again, concisely: compact JSON with no padding "
            "or blank space; literal_quote, quote_span.text and reply_proposal.basis_quote.text at most "
            + str(MAX_QUOTE_CHARACTERS) + " characters each (the few words the decision rests on, never the "
            "whole message); reason and next_action one or two sentences each, at most 500 characters; "
            "reply_proposal.text at most 500 characters (2500 for a champion summary). Keep the reply itself "
            "as complete as the answer needs.")


def _validate(raw, request, message, source_holds):
    try:
        output = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, RecursionError):
        raise Held("CLASSIFICATION_OUTPUT_MALFORMED") from None
    # Size is measured compact and is a retry with feedback, never a terminal hold (owner,
    # 2026-10-01). Field bounds come first so the feedback can name the field that ran long.
    if _too_long(output, quotes=False):
        raise Held("CLASSIFICATION_FIELD_TOO_LONG", retry=True)
    champion_size = isinstance(output, dict) and output.get("champion_handoff") is not None
    if compact_size(output) > (champion_composition.MAX_OUTPUT_CHARACTERS if champion_size else MAX_OUTPUT_CHARACTERS):
        raise Held("CLASSIFICATION_OUTPUT_EXCEEDS_LIMIT", retry=True)
    fields = {"output_version", "source_sha256", "label", "literal_quote", "quote_span", "role_reference",
              "reason", "unknown_factual_requirements", "reply_proposal", "next_action", "review_at"}
    if (not isinstance(output, dict) or not fields <= set(output)
            or set(output) - fields - {"champion_handoff", "speaker_context"}):
        raise Held("CLASSIFICATION_OUTPUT_SCHEMA_INVALID")
    if "speaker_context" in output and (not isinstance(output["speaker_context"], str)
            or output["speaker_context"] not in {"DIRECT_SPEAKER", "QUOTED_OR_FORWARDED", "UNKNOWN"}):
        raise Held("CLASSIFICATION_SPEAKER_CONTEXT_INVALID")
    champion = output.get("champion_handoff") is not None
    if output["output_version"] != VERSION or output["source_sha256"] != request["source_sha256"]:
        raise Held("CLASSIFICATION_OUTPUT_BINDING_MISMATCH")
    if (not isinstance(output["label"], str) or output["label"] not in LABELS
            or not isinstance(output["reason"], str) or not 1 <= len(output["reason"].strip()) <= 500):
        raise Held("CLASSIFICATION_OUTPUT_SCHEMA_INVALID")
    # literal_quote, quote_span and reply_proposal.basis_quote are recorded as the model gave
    # them and never checked against the message (owner, 2026-09-27: "all of these can happen
    # without depending on quotes").
    if output["role_reference"] is not None and (not isinstance(output["role_reference"], str)
            or output["role_reference"] not in {item["job_key"] for item in request.get("job_scope", [])}):
        raise Held("CLASSIFICATION_ROLE_OUTSIDE_VERIFIED_SCOPE")
    if not isinstance(output["next_action"], str) or not 1 <= len(output["next_action"].strip()) <= 500:
        raise Held("CLASSIFICATION_NEXT_ACTION_REQUIRED")
    # A null review_at is the model saying nothing about this message needs another look
    # (owner, 2026-09-24): it finishes the message instead of booking another review round.
    # That includes AGREEMENT and DECLINE (owner, 2026-09-24: "Let Agreements and declines be
    # closed with a blank date"); their referral record takes conversion's default deadline.
    if output["review_at"] is not None:
        try:
            review_at = dt.datetime.fromisoformat(output["review_at"])
            if review_at.tzinfo is None or review_at.utcoffset() is None:
                raise ValueError()
        except (ValueError, TypeError):
            raise Held("CLASSIFICATION_REVIEW_TIME_REQUIRED") from None
    unknowns = output["unknown_factual_requirements"]
    if not isinstance(unknowns, list) or len(unknowns) > len(FACTS) or any(not isinstance(item, str) or item not in FACTS for item in unknowns):
        raise Held("CLASSIFICATION_UNKNOWN_FACT_REQUIREMENTS_INVALID")
    proposal = output["reply_proposal"]
    if proposal is not None:
        proposal_limit = champion_composition.MAX_TEXT_CHARACTERS if champion else 500
        # Removed 2026-09-23 (owner). This was one half of a pincer: `reply-prompt.md` ordered
        # the model to leave reply_proposal null whenever a candidate fact was uncertain, and
        # this raised a hard Held if it wrote one anyway. It complied, so 44 of 47 decisions
        # carried no draft and `reply_execution._action` could never build a reply from them.
        # The grounding rules that stop a draft ASSERTING an unknown fact are untouched.
        if (not isinstance(proposal, dict) or set(proposal) != {"text", "basis_quote"}
                or not isinstance(proposal["text"], str)
                or not 1 <= len(proposal["text"].strip()) <= proposal_limit
                or ({"champion_handoff", "speaker_context"}.intersection(output) and len(proposal["text"]) > proposal_limit)):
            raise Held("CLASSIFICATION_REPLY_PROPOSAL_INVALID")
    resolved = ({"UNKNOWN_QUOTED_CONTEXT", "UNKNOWN_FORWARDED_CONTEXT"}
                if request.get("champion_handoff_enabled") is True and output.get("speaker_context") == "DIRECT_SPEAKER"
                and champion_composition.direct_intent(output, request) else set())
    # Owner, 2026-09-23: fix these without reintroducing anything that stops a reply going out
    # or stops conversion tracking. Every entry below used to be a HOLD, and `advance` required
    # an empty hold list, so all 47 live decisions were INVESTIGATION_REQUIRED and none ever
    # advanced. They are recorded as `observations` now - visible in the handoff, carried in the
    # audit, blocking nothing.
    #
    #  * ROLE_ASSOCIATION: `reply-prompt.md` ORDERS role_reference null when job_scope is empty,
    #    so holding on it punished the model for obeying the contract. It fired on 47 of 47.
    #  * QUOTED/FORWARDED context: HeyReach does not supply those flags at all, so this was
    #    absence of evidence. 46 of 47.
    #  * speaker UNKNOWN and the AMBIGUOUS/OTHER label class: the prompt tells the model to say
    #    AMBIGUOUS when it is uncertain and UNKNOWN when it cannot tell. Holding on an honest
    #    answer is the defect this project keeps removing. 39 of 47 were CONTEXTUAL_OTHER.
    #
    # The real safety property is untouched and lives elsewhere: `can_send` is False on every
    # path, `apply_reply_decision` re-verifies the full source binding, and the send path checks
    # exclusion, suppression, account, thread and managed-conversation scope.
    observations = [hold for hold in source_holds if hold not in resolved]
    observations.extend("UNKNOWN_FACT:" + item for item in unknowns if item != "CANDIDATE_FACTS")
    if output.get("speaker_context") == "QUOTED_OR_FORWARDED":
        observations.append("CONTEXTUAL_QUOTED_OR_FORWARDED")
    elif output.get("speaker_context") == "UNKNOWN":
        observations.append("UNKNOWN_SPEAKER_CONTEXT")
    if output["label"] in {"AMBIGUOUS", "QUOTED_OR_FORWARDED", "OTHER"}:
        observations.append("CONTEXTUAL_" + output["label"])
    if output["role_reference"] is None:
        observations.append("UNKNOWN_FACT:ROLE_ASSOCIATION")
    # `holds` keeps ONLY the integrity signals - a real problem with the source, such as an
    # unauthenticated message binding or a conflicting conversation context. Everything that
    # merely recorded uncertainty now sits in `observations` and blocks nothing.
    holds = [hold for hold in source_holds if hold not in resolved
             and not hold.startswith(("UNKNOWN_QUOTED", "UNKNOWN_FORWARDED"))]
    # A decision advances conversion state when the model reached a definite reading. INTERVIEW
    # and REJECTION join AGREEMENT/DECLINE so `conversion_interviews` can finally be populated
    # from a reply - it was structurally impossible before, because no label could carry it.
    # `advance` writes CONVERSION state - that a contact agreed to refer, that an interview was
    # offered. It therefore still requires the model's own attribution: DIRECT_SPEAKER means it
    # judged the correspondent actually said this, while UNKNOWN or QUOTED_OR_FORWARDED means it
    # could not tell whether they were quoting or forwarding someone else. Recording a referral
    # agreement from a message the model cannot attribute would record a fact that may be false,
    # which is the one line this project has never crossed.
    #
    # This is NOT a gate on replying. A reply goes out on the same grounded draft whatever the
    # attribution says - `reply_execution` never consults `advance` - so an unattributable
    # message is still answered, it just does not silently become a tracked referral.
    # A provider flag KNOWN TRUE is positive evidence that the message really is quoted or
    # forwarded, so it overrides the model's DIRECT_SPEAKER reading - that is not absence of
    # evidence and it stays binding. An ABSENT flag (UNKNOWN_*_CONTEXT) is absence and does not.
    # `champion_composition.direct_intent` already encodes this exact question and is the one
    # definition of it: the model must have judged DIRECT_SPEAKER, and the provider flags must
    # either both be an explicit False or be adjudicable from an exact verified quote. A flag
    # known TRUE, or malformed, is never adjudicable - that is positive evidence the message is
    # quoted or forwarded, not absence of evidence, so it still binds.
    try:
        inbox_flags = json.loads(request["source_snapshot"]["payload"])["inbox"]
        observable = isinstance(inbox_flags, dict)
    except (KeyError, TypeError, ValueError):
        observable = False
    # When provider flags are observable, `champion_composition.direct_intent` is the one
    # definition of "did this person actually say it" - a flag known TRUE or malformed is
    # positive evidence of quoting and still binds. When there is no snapshot to judge at all,
    # that is absence of evidence, so the model's own `speaker_context` stands (an absent field
    # is a legacy decision and is treated as direct, exactly as `direct_intent` does).
    attributable = (champion_composition.direct_intent(output, request) if observable
                    else output.get("speaker_context", "DIRECT_SPEAKER") == "DIRECT_SPEAKER")
    # Only the UNKNOWN_* provider-flag holds are absence of evidence. Everything else the source
    # reported is a real integrity problem - the message binding was not authenticated, the
    # conversation context conflicts - and still stops CONVERSION state, though never a reply.
    integrity = [hold for hold in source_holds if hold not in resolved
                 and not hold.startswith(("UNKNOWN_QUOTED", "UNKNOWN_FORWARDED"))]
    # An unknown that bears on the LABEL ITSELF still stops conversion state: if the model
    # reported AGREEMENT_INTENT unknown, it is saying it could not establish that they agreed,
    # and writing REFERRAL_AGREED from that would record a fact that may be false. ROLE_ASSOCIATION
    # is not such an unknown - it says which ROLE is unclear, not whether the person agreed - and
    # CANDIDATE_FACTS is about drafting a reply, not about what the correspondent said. Those two
    # are exactly the ones that held all 47 live decisions, and they no longer do.
    intent_unknowns = [item for item in unknowns if item not in {"CANDIDATE_FACTS", "ROLE_ASSOCIATION"}]
    # CONVERSION state is per-role - a referral agreement, an interview, a rejection are all
    # recorded against a job - so advancing needs `role_reference`, and
    # `pipeline_routing.apply_reply_decision` independently requires exactly one binding that
    # matches it before writing a row. That is why ROLE_ASSOCIATION still bears on `advance`.
    #
    # What changed (owner, 2026-09-23) is that NONE of this touches the reply any more. The 47
    # live decisions were stuck because these were HOLDS and a hold blocked everything: no
    # draft, no send, no reply at all. They are observations now, `reply_execution` never
    # consults `advance`, so an unattributable or role-less message is still answered - it just
    # does not silently become a tracked referral.
    # No job is required (owner, 2026-09-24: "remove the condition that an agreement ... must be
    # tied to a single Job. That's not a requirement"). This required role_reference, and
    # ROLE_ASSOCIATION counted as an intent unknown, so an agreement about a role the system had
    # not researched could never finish. apply_reply_decision records it against the job when
    # one is named and on the conversation when none is.
    advance = (attributable and not integrity and not intent_unknowns
               and output["label"] in {"AGREEMENT", "DECLINE", "INTERVIEW", "REJECTION"})
    # Was `raise Held("CHAMPION_SUMMARY_REQUIRED", retry=True)` (owner, 2026-09-23). It demanded
    # a summary whose validator needs `role_context.evidence_id`, i.e. a ROLE_CONTEXT row in
    # `conversion_evidence` - of which there are none - so any decision that ever reached it
    # would have burned every attempt and exhausted. A missing champion summary is now recorded
    # and the decision still advances: the summary is an extra the owner can forward, never the
    # thing that decides whether a reply or a conversion update happens.
    # Narrowed, not removed (owner, 2026-09-23). This raised `Held(retry=True)` whenever a
    # champion-enabled AGREEMENT arrived without a summary - but the summary's own validator
    # needs `role_context.evidence_id`, i.e. a ROLE_CONTEXT row in `conversion_evidence`, and
    # there are none in the live database. So the model was asked for something it could not
    # produce and the retry burned every attempt into EXHAUSTED. It is now asked only when the
    # evidence that makes a summary possible is actually in the packet; otherwise the decision
    # advances without one, because a missing forwardable extra must not cost the conversion
    # update it is attached to.
    summarisable = any((scope or {}).get("role_context", {}).get("evidence_id")
                       for scope in (request.get("job_scope") or []) if isinstance(scope, dict))
    if (request.get("champion_handoff_enabled") is True and output["label"] == "AGREEMENT"
            and advance and not unknowns and output.get("champion_handoff") is None and summarisable):
        raise Held("CHAMPION_SUMMARY_REQUIRED", retry=True)
    try:
        champion_composition.validate(output, request, advance)
    except ValueError as error:
        raise Held(str(error)) from None
    return {"output_version": VERSION, "state": "DECISION_READY" if advance else "INVESTIGATION_REQUIRED",
            "evidence_basis": "AI_JUDGMENT_ON_AUTHENTICATED_SOURCE", "can_advance_conversion": advance,
            "can_send": False, "verified_business_state": None, "holds": sorted(set(holds)),
            "observations": sorted(set(observations)),
            "required_next_action": output["next_action"],
            "classification": output}


def _revise_legacy_assets(database, task, config, now, provenance):
    """Upgrade one unchanged legacy read within its original remaining budget."""
    if config.get("conversion", {}).get("champion_handoff_enabled") is not True:
        return None
    saved = routing.one(database, "SELECT * FROM reply_worker_inputs WHERE task_id=?", (task["task_id"],))
    if not saved or routing.one(database, "SELECT 1 FROM reply_worker_results WHERE task_id=?", (task["task_id"],)):
        return None
    try:
        previous_provenance = json.loads(saved["provenance"])
        template, separator, packet = saved["input"].partition("\n\nUNTRUSTED_SOURCE_JSON:\n")
        previous = json.loads(packet)
        bound = json.loads(task["inputs"])[0]
        review_round = bound.get("review_round", 0)
        remaining = task["max_attempts"] - task["attempts"]
        # Any in-flight read whose prompt or schema changed is upgraded to a successor under the
        # current assets (owner, 2026-09-24: "requeue the 4"). This was limited to legacy packets
        # predating the champion handoff, so a later prompt change left a task with a saved input
        # on CLASSIFICATION_IMMUTABLE_INPUT_CHANGED with nothing to continue it.
        if (not separator
                or previous_provenance == provenance
                or not 0 <= task["attempts"] < task["max_attempts"]
                or task["status"] not in {"QUEUED", "RETRY_WAIT", "RUNNING"}
                or task["status"] == "RUNNING" and workflow_tasks.instant(task["lease_until"]) > workflow_tasks.instant(now)
                or sha(template) != previous_provenance["prompt_sha256"]
                or sha(saved["input"]) != saved["input_sha256"]
                # No round limit (owner, 2026-09-24): the upper bound of 2 is deleted; rounds must still follow on by one.
                or type(review_round) is not int or review_round < 0
                or remaining < 1
                or {key: value for key, value in previous_provenance.items() if key not in {"prompt_sha256", "schema_sha256"}}
                != {key: value for key, value in provenance.items() if key not in {"prompt_sha256", "schema_sha256"}}):
            return None
        current, unused_message, unused_holds = _source(database, task, config, now)
        # The output instructions are assets, not the message: the 4 round-2 reads of 2026-09-23
        # carried the pre-reconciliation contract (6 labels, 2000 characters) and were refused
        # here although their message was byte-identical. repair_feedback is per-attempt guidance
        # (the judge's last objection), not the message. The message must still match exactly.
        instructions = {"output_contract", "max_output_characters", "repair_feedback"}
        if ({key: value for key, value in current.items() if key not in instructions}
                != {key: value for key, value in previous.items() if key not in instructions}
                or saved["source_sha256"] != current["source_sha256"]):
            return None
        stored = routing.one(database, "SELECT * FROM pipeline_routing_sources WHERE source_key=?", (bound["source_key"],))
        result = json.loads(stored["result"]) if stored["result"] else {}
        if result.get("review_task_id") != task["task_id"]:
            return None
        transition = {"prompt_sha256": provenance["prompt_sha256"], "schema_sha256": provenance["schema_sha256"],
                      "predecessor_provenance_sha256": routing.digest(previous_provenance)}
        followup = {key: value for key, value in bound.items()
                    if key not in {"reply_decision_task_id", "reply_decision_output_sha256"}}
        followup.update(context_revision_task_id=task["task_id"], context_revision_input_sha256=saved["input_sha256"],
                        context_revision_sha256=routing.digest(transition), review_round=review_round + 1,
                        champion_asset_revision=transition)
        action = routing.reply_review_action(database, stored, followup)
    except (Held, ValueError, KeyError, TypeError):
        return None
    with routing.transaction(database):
        successor = workflow_tasks.enqueue(database, "pipeline_routing_review", action, task["parent_run_id"],
            [followup], task["deadline"], now, effect="READ", max_attempts=max(1, min(remaining, 7)))
        result["review_task_id"] = successor
        database.execute("UPDATE pipeline_routing_sources SET result=? WHERE source_key=?",
                         (routing.encoded(result), stored["source_key"]))
        database.execute("""UPDATE workflow_tasks SET status='CANCELLED',blocker='CHAMPION_ASSET_REVISION',
            next_action=?,next_at=NULL,token_hash=NULL,lease_until=NULL,updated_at=? WHERE task_id=?""",
            ("Continue the same source-bound read in task " + successor, now, task["task_id"]))
        workflow_tasks.record(database, task["task_id"], "CHAMPION_ASSET_REVISION", now,
                              {"task_id": successor, "input_sha256": saved["input_sha256"],
                               "deadline": task["deadline"], "remaining_attempts": remaining, **transition})
        return successor


# Records that show the harness or the owner actually wrote to a person: sends and launches
# (outreach_attempts, outreach_launch_claims), the imported contact history, conversations the
# owner started by hand (managed_conversations), and claimed message sends other than campaign
# preparation (outreach_action_claims). A prepared, unlaunched campaign is not contact.
CONTACT_TABLES = ("outreach_attempts", "outreach_launch_claims", "managed_conversations")
OUR_MESSAGE_KINDS = ("initial_sent", "followup_sent", "outbound_observed")


def contacted(database, profile):
    """True when our own records show the harness or the owner contacted this person (any alias).

    Owner, 2026-10-01: a stuck-reply alert is only for people the harness or the owner actually
    contacted; a stranger's cold message never alerts."""
    import engagement
    from orchestrator import profile_key

    identities = set(engagement.identity_keys(database, profile))
    placeholders = ",".join("?" for unused in identities)
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "engagement_events" in tables and database.execute(
            "SELECT 1 FROM engagement_events WHERE identity IN (" + placeholders + ") AND kind IN ("
            + ",".join("?" for unused in OUR_MESSAGE_KINDS) + ")", (*identities, *OUR_MESSAGE_KINDS)).fetchone():
        return True
    for table in CONTACT_TABLES:
        if table in tables and database.execute(
                "SELECT 1 FROM " + table + " WHERE identity IN (" + placeholders + ")", tuple(identities)).fetchone():
            return True
    if {"outreach_action_claims", "outreach_actions"} <= tables and database.execute(
            """SELECT 1 FROM outreach_action_claims claim JOIN outreach_actions action ON action.action_id=claim.action_id
            WHERE claim.identity IN (""" + placeholders + """)
            AND COALESCE(json_extract(action.payload,'$.purpose'),'')!='PREPARE_CAMPAIGN'""", tuple(identities)).fetchone():
        return True
    if "history" in tables:
        for identity, recorded in database.execute("SELECT identity,profile FROM history"):
            try:
                keyed = bool(recorded) and profile_key(recorded) in identities
            except (ValueError, TypeError):
                keyed = False
            if identity in identities or keyed:
                return True
    return False


COLD_BLOCKER = "REPLY_COLD_INBOUND_SKIPPED"
NO_BASIS_BLOCKER = "REPLY_NO_REPLY_BASIS_SKIPPED"
ANSWERED_BLOCKER = "REPLY_ALREADY_ANSWERED_SKIPPED"
UNRESOLVED_BLOCKER = "REPLY_SENDER_IDENTITY_UNRESOLVED"
# How each kind of unanswered inbound message is closed: blocker, workflow event, item state and
# the next action recorded on the task.
SKIPPED = {
    "backlog": (None, "REPLY_BACKLOG_CLOSED", "BACKLOG_NOT_SENT",
                "Inbound message from before the 2026-09-30 reply cutoff: never answered (owner); no reply AI ran"),
    "cold_inbound": (COLD_BLOCKER, "REPLY_COLD_INBOUND_CLOSED", "COLD_INBOUND_SKIPPED",
                     "Inbound message from a person the harness does not track: skipped (owner, 2026-10-01); "
                     "no reply AI ran and no draft was made"),
    "answered": (ANSWERED_BLOCKER, "REPLY_ALREADY_ANSWERED_CLOSED", "ALREADY_ANSWERED_SKIPPED",
                 "Inbound message already answered from our side (e.g. by hand) or followed by a newer "
                 "message of theirs, which gets its own review: skipped (owner, 2026-10-01: \"Dont "
                 "generate draft for me\"); no reply AI ran"),
    "no_reply_basis": (NO_BASIS_BLOCKER, "REPLY_NO_REPLY_BASIS_CLOSED", "NO_REPLY_BASIS_SKIPPED",
                       "Inbound message from a tracked person the harness may not answer (not a managed "
                       "conversation and not answered by hand): skipped (owner, 2026-10-01: \"Dont generate "
                       "draft for me\"); no reply AI ran and no draft was made"),
    # Held, not closed: the task stays BLOCKED, its routing source untouched, and it is checked
    # again every cycle (close_skipped).
    "unresolved": (UNRESOLVED_BLOCKER, "REPLY_SENDER_IDENTITY_HELD", "SENDER_IDENTITY_UNRESOLVED",
                   "Inbound sender could not be tied to a person: no usable profile, an unreadable record, or "
                   "a LinkedIn member-id spelling (ACoA...) no record of ours links by lead id or conversation; "
                   "held, not skipped, and checked again every cycle"),
}


def _sender(stored):
    """(snapshot, sender identity, HeyReach lead id, conversation id) of a routing source.

    Raises ValueError/TypeError/AttributeError on an unreadable snapshot or raw observation."""
    import reply_execution

    snapshot = json.loads(stored["snapshot"])
    raw = snapshot.get("raw") or {}
    raw = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(raw, dict):
        raise ValueError("REPLY_SENDER_RAW_UNREADABLE")
    person = snapshot.get("identity") or raw.get("sender_profile")
    lead_id, thread = reply_execution.sender_keys(snapshot)
    return snapshot, person, lead_id, thread


def _reply_state(database, person, lead_id, thread, snapshot=None):
    """REPLY, COLD, NO_BASIS, ANSWERED or UNRESOLVED for an inbound sender.

    outreach.resolve_sender decides tracked / cold / unresolved; a tracked sender is answered only
    with outreach.reply_conversation's basis, the same check reply_execution._action applies before
    a send, so what this skips and what the sender refuses agree. With the inbound's snapshot,
    ANSWERED is reply_execution.newer_activity, the check binding_gates applies: our side already
    answered it, or a newer message of theirs gets its own review."""
    import outreach
    import reply_execution

    resolved = outreach.resolve_sender(database, person, lead_id, thread)
    if resolved["state"] != "TRACKED":
        return resolved["state"]
    if not outreach.reply_conversation(database, person, lead_id, thread):
        return "NO_BASIS"
    if snapshot is not None:
        own = outreach._identity_of(person)
        action = {"profile": person, "conversation_id": thread, "reply_inbound_at": snapshot.get("occurred_at"),
                  "reply_source": snapshot.get("source"), "reply_source_id": snapshot.get("source_id"),
                  **({"resolved_profile": resolved["person"]} if resolved["person"] and resolved["person"] != own else {})}
        if reply_execution.newer_activity(database, action):
            return "ANSWERED"
    return "REPLY"


def resolved_person(database, snapshot):
    """The tracked person a member-id sender spelling resolves to (bare identity), or None when it
    is the sender itself or nothing resolves. Never raises."""
    import outreach
    import reply_execution

    try:
        person = snapshot.get("identity")
        lead_id, thread = reply_execution.sender_keys(snapshot)
        found = outreach.resolve_sender(database, person, lead_id, thread)
        if found["state"] == "TRACKED" and found["person"] and found["person"] != outreach._identity_of(person):
            return found["person"]
    except Exception:
        pass
    return None


def _skip_kind(database, task):
    """(routing source, kind) for a reply review that is never answered or is held, else None.

    "backlog" (reply_execution.backlog: before BACKLOG_CUTOFF plus the drift window, or an
    unreadable time; owner, 2026-09-30); "cold_inbound": a sender no local record tracks
    (outreach.resolve_sender COLD; owner, 2026-10-01: "cold strangers should be fully skipped. Dont
    generate draft for me"); "no_reply_basis": a tracked sender the harness may not answer
    (_reply_state NO_BASIS); "unresolved": a sender that cannot be keyed, an unreadable snapshot,
    or a member-id spelling nothing ties to a tracked person, which is held rather than taken for a
    stranger. No reply AI runs on any of them. Database errors propagate to the caller."""
    import reply_execution

    try:
        bound = json.loads(task["inputs"])[0]
    except (ValueError, KeyError, TypeError, IndexError):
        return None
    if not isinstance(bound, dict) or bound.get("reason") not in REASONS:
        return None
    stored = routing.one(database, "SELECT * FROM pipeline_routing_sources WHERE source_key=?", (bound.get("source_key"),))
    if not stored or stored["source_table"] != "engagement_events":
        return None
    try:
        snapshot, person, lead_id, thread = _sender(stored)
    except (ValueError, TypeError, AttributeError):
        return stored, "unresolved"
    if reply_execution.backlog(snapshot.get("occurred_at")):
        return stored, "backlog"
    state = _reply_state(database, person, lead_id, thread, snapshot)
    return {"COLD": (stored, "cold_inbound"), "NO_BASIS": (stored, "no_reply_basis"),
            "ANSWERED": (stored, "answered"), "UNRESOLVED": (stored, "unresolved")}.get(state)


def _held(task):
    return task["status"] == "BLOCKED" and task["blocker"] == UNRESOLVED_BLOCKER


def close_skipped(database, task_id, now):
    """Finish a queued reply review of a backlog, cold or no-basis inbound message without a model
    call, hold an unresolved sender, and re-check a held one.

    No compute is reserved, no attempt is spent, no decision or draft is made and nobody is
    notified. Backlog, cold and no-basis tasks end CANCELLED with their blocker and their routing
    source is finished (ROUTED, closed by the kind), so no later round is booked; such a message is
    never revisited, even if its sender is tracked or answered later (a later inbound message gets
    its own review). An unresolved sender's task is held BLOCKED with UNRESOLVED_BLOCKER, its
    routing source untouched; every cycle it is checked again: still unresolved, it stays as it
    is (no write); resolved to a sender to answer, it is re-queued (REPLY_SENDER_IDENTITY_RESOLVED)
    and runs normally; otherwise it is closed by its new kind. The inbound's own stop/suppression
    rows, written when it was recorded, are untouched. Returns the item, or None when nothing
    applies."""
    import reply_execution

    database.execute("BEGIN IMMEDIATE")
    with database:
        task = routing.one(database, "SELECT * FROM workflow_tasks WHERE task_id=?", (task_id,))
        if (not task or task["worker"] != "pipeline_routing_review" or task["effect"] != "READ"
                or not (task["status"] in {"QUEUED", "RETRY_WAIT", "RUNNING"} or _held(task))
                or task["status"] == "RUNNING" and not (task["lease_until"] and task["lease_until"] <= now)):
            return None
        found = _skip_kind(database, task)
        if found is None:
            if not _held(task):
                return None
            database.execute("""UPDATE workflow_tasks SET status='QUEUED',blocker=NULL,next_action=NULL,next_at=NULL,
                updated_at=? WHERE task_id=?""", (now, task_id))
            workflow_tasks.record(database, task_id, "REPLY_SENDER_IDENTITY_RESOLVED", now,
                                  {"previous_blocker": UNRESOLVED_BLOCKER, "attempts": task["attempts"]})
            return {"task_id": task_id, "state": "SENDER_IDENTITY_RESOLVED", "workflow_status": "QUEUED",
                    "attempts": task["attempts"]}
        stored, kind = found
        if kind == "unresolved" and _held(task):
            return None
        blocker, event, state, next_action = SKIPPED[kind]
        blocker = blocker or reply_execution.BACKLOG_BLOCKER
        try:
            occurred = json.loads(stored["snapshot"]).get("occurred_at")
        except (ValueError, TypeError, AttributeError):
            occurred = None
        status = "BLOCKED" if kind == "unresolved" else "CANCELLED"
        database.execute("""UPDATE workflow_tasks SET status=?,blocker=?,next_action=?,next_at=NULL,
            token_hash=NULL,lease_until=NULL,updated_at=? WHERE task_id=?""",
                         (status, blocker, next_action, now, task_id))
        workflow_tasks.record(database, task_id, event, now,
                              {"source_key": stored["source_key"], "inbound_at": occurred,
                               "previous_status": task["status"], "attempts": task["attempts"],
                               "blocker": blocker, "can_send": False})
        if kind != "unresolved" and stored["status"] != "ROUTED":
            result = json.loads(stored["result"]) if stored["result"] else {}
            result.update(review_closed_by=kind, backlog_blocker=blocker,
                          backlog_task_id=task_id, next_action=next_action, review_at=None, can_send=False)
            database.execute("UPDATE pipeline_routing_sources SET status='ROUTED',blocker=NULL,result=? WHERE source_key=?",
                             (routing.encoded(result), stored["source_key"]))
        return {"task_id": task_id, "state": state, "workflow_status": status,
                "blocker": blocker, "attempts": task["attempts"]}


close_backlog = close_skipped


def _close_skipped_reported(database, task_id, now):
    """close_skipped, with any failure (a database error included) reported against the task
    (where it failed, never what it said) instead of stopping the cycle; the task stays as it
    was, no reply AI runs on it this cycle, and it is tried again next cycle. A skipped task
    close_skipped leaves alone (another runner holds or finished it) is reported as
    BACKLOG_SKIPPED, so it too never reaches the reply AI. None for a task to be answered, and for
    a held sender that is still unresolved."""
    try:
        task = routing.one(database, "SELECT * FROM workflow_tasks WHERE task_id=?", (task_id,))
        if not task:
            return None
        held = _held(task)
        if not held and _skip_kind(database, task) is None:
            return None
        closed = close_skipped(database, task_id, now)
    except Exception as error:
        if database.in_transaction:
            database.rollback()
        frame = traceback.extract_tb(error.__traceback__)[-1] if error.__traceback__ else None
        return {"task_id": task_id, "state": "SKIP_CHECK_FAILED", "error_type": type(error).__name__,
                "raised_at": (os.path.basename(frame.filename) + ":" + str(frame.lineno) + " " + frame.name)
                if frame else None}
    if closed is not None or held:
        return closed
    return {"task_id": task_id, "state": "BACKLOG_SKIPPED", "workflow_status": task["status"]}


def held_senders(database):
    """Reply reviews held because their sender could not be tied to a person (read-only), for the
    status command and the daily report. No Telegram line is sent for them."""
    import reply_execution

    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"workflow_tasks", "pipeline_routing_sources"} <= tables:
        return []
    held = []
    for task in database.execute("""SELECT task_id,inputs,updated_at FROM workflow_tasks
            WHERE worker='pipeline_routing_review' AND status='BLOCKED' AND blocker=? ORDER BY updated_at,task_id""",
                                 (UNRESOLVED_BLOCKER,)).fetchall():
        entry = {"task_id": task[0], "held_since": task[2], "sender": None, "provider_lead_id": None,
                 "conversation_id": None, "inbound_at": None}
        try:
            key = json.loads(task[1])[0]["source_key"]
            row = database.execute("SELECT snapshot FROM pipeline_routing_sources WHERE source_key=?", (key,)).fetchone()
            snapshot = json.loads(row[0])
            entry["sender"] = snapshot.get("identity")
            entry["inbound_at"] = snapshot.get("occurred_at")
            entry["provider_lead_id"], entry["conversation_id"] = reply_execution.sender_keys(snapshot)
        except Exception:
            pass
        held.append(entry)
    return held


def notify_failed_attempt(database, task, request, blocker, state, now, continued=None):
    """Tell the owner on Telegram when a reply to someone we contacted is stuck.

    Owner, 2026-09-23: "Confirm it send a telegram message on failed attempts." Owner,
    2026-09-25: "tell me on telegram when a reply is stuck, regardless of if its tied to a job
    or not" - and no messages asking for approvals. So this fires only when a reply has
    stopped (BLOCKED or EXHAUSTED) and nothing continues it; a retry that will run again on its
    own ("will retry") no longer notifies. It used to stay silent unless the message was linked
    to a job, which in practice meant almost never.

    Keyed on (task, state, blocker) through INSERT OR IGNORE, so the same stoppage is reported
    once. Never raises: a notification problem must not fail the work that produced it.

    Narrowed (owner, 2026-10-01): only for a person the harness or the owner actually contacted
    (`contacted`), and never for a backlog message (reply_execution.backlog), which is never
    answered. Recruiters who cold-messaged the owner were getting "A reply is stuck" alerts.
    """
    if state not in {"BLOCKED", "EXHAUSTED"} or continued:
        return None
    try:
        import applications
        import reply_execution
        snapshot = (request or {}).get("source_snapshot")
        if snapshot is None:
            key = json.loads(task["inputs"])[0]["source_key"]
            stored = routing.one(database, "SELECT snapshot FROM pipeline_routing_sources WHERE source_key=?", (key,))
            snapshot = json.loads(stored["snapshot"]) if stored else {}
        raw = snapshot.get("raw") or {}
        raw = json.loads(raw) if isinstance(raw, str) else raw
        person = snapshot.get("identity") or raw.get("sender_profile") or raw.get("profile")
        resolved = resolved_person(database, snapshot)
        if (reply_execution.backlog(snapshot.get("occurred_at")) or not person
                or not (contacted(database, person) or resolved and contacted(database, resolved))):
            return None
        who = str(raw.get("sender_profile") or raw.get("profile") or "an unknown contact").rstrip("/").rsplit("/", 1)[-1]
        said = " ".join(str(raw.get("text") or "").split())[:120]
        reason = str(blocker or "unknown reason").replace("_", " ").lower()[:120]
        event = "reply:" + str(task["task_id"])[:24] + ":" + str(state) + ":" + str(blocker or "UNKNOWN")[:60]
        message = ("A reply is stuck for " + who + (' (their message: "' + said + '")' if said else "")
                   + ". Reason: " + reason + ". No reply was sent.")
        applications.notify(database, event, message, now)
        return event
    except Exception:
        return None


def cycle(root, database, config, now=None):
    """Process eligible queued reads and due retries; return private-data-free IDs.

    config supplies the existing codex/model plus pipeline_routing.account_ids.
    reply_worker.enabled defaults to false and must be exactly true to execute.
    Optional bounds are timeout_seconds (1..120), max_items (1..20).
    No provider/config discovery, paid fallback, prompts for approval, or effects.
    A COMPLETE task means the decision is persisted, never a message was sent.
    """
    if database.in_transaction:
        raise ValueError("Reply worker requires committed caller transactions")
    if config.get("reply_worker", {}).get("enabled") is not True:
        return {"state": "DISABLED", "output_version": VERSION, "items": []}
    now = now or dt.datetime.now(dt.timezone.utc).isoformat()
    if isinstance(now, dt.datetime):
        now = now.isoformat()
    base_time = workflow_tasks.instant(now)
    now = base_time.isoformat()
    started = time.monotonic()

    def current():
        return (base_time + dt.timedelta(seconds=time.monotonic() - started)).isoformat()

    try:
        timeout, limit, budget = _options(config)
        root = Path(root)
        prompt_template = resources.read_text("reply-prompt.md")
        prompt_template += "\n" + champion_composition.INDEPENDENT_REFERRAL_INSTRUCTIONS
        schema = resources.read_text("reply-schema.json")
        # The run has tools, so codex's --output-schema is off (see _command); the schema is part
        # of the saved prompt instead, which stays exactly what the model was given.
        prompt_template += "\n" + TOOLS_OUTPUT_INSTRUCTIONS + "\n" + schema
    except (Held, OSError, ValueError, TypeError) as error:
        return {"state": "BLOCKED", "blocker": error.code if isinstance(error, Held) else "REPLY_WORKER_ASSETS_UNAVAILABLE", "items": []}
    initialize(database)
    database.commit()
    provenance = {"model": text_model.model(config), "executable": config["codex"], "auth": text_model.auth(config),
                  "model_source": "configured_cli_argument", "output_version": VERSION,
                  "prompt_sha256": sha(prompt_template), "schema_sha256": sha(schema)}

    def revise_context(task, stamp):
        saved = routing.one(database, "SELECT provenance FROM reply_worker_inputs WHERE task_id=?", (task["task_id"],))
        if saved and saved["provenance"] == routing.encoded(provenance):
            return routing.revise_reply_context(database, task, config, stamp)
        return None

    # `max_attempts >= 1`. It was 1 AND 3, then 1 AND 7 (owner, 2026-09-23), and each upper bound
    # silently became a selection filter: a task whose tries grew past it (a provider or compute
    # wait refunds one, 2026-09-28) would have been invisible to its own worker and never run.
    placeholders = ",".join("?" for unused in REASONS)
    items = []
    # Backlog, cold and no-basis inbound messages are finished first, with no model call, compute or
    # attempt (owner, 2026-10-01), so they never take one of the `limit` places a reply needs; held
    # senders are checked again here and re-queued once they resolve.
    for waiting in routing.rows(database, f"""SELECT task_id FROM workflow_tasks WHERE worker='pipeline_routing_review'
            AND (status IN ('QUEUED','RETRY_WAIT','RUNNING') OR status='BLOCKED' AND blocker=?) AND effect='READ'
            AND json_valid(inputs) AND json_extract(inputs,'$[0].reason') IN ({placeholders})
            AND (status!='RUNNING' OR lease_until<=?) ORDER BY updated_at,task_id LIMIT 500""",
            (UNRESOLVED_BLOCKER, *REASONS, now)):
        closed = _close_skipped_reported(database, waiting["task_id"], current())
        if closed is not None:
            items.append(closed)
    candidates = routing.rows(database, f"""SELECT * FROM workflow_tasks WHERE worker='pipeline_routing_review'
        AND status IN ('QUEUED','RETRY_WAIT','RUNNING') AND effect='READ' AND max_attempts >= 1
        AND json_valid(inputs) AND json_extract(inputs,'$[0].reason') IN ({placeholders})
        AND (status!='RUNNING' OR lease_until<=?) AND (next_at IS NULL OR next_at<=?)
        ORDER BY updated_at,task_id LIMIT ?""", (*REASONS, now, now, limit))
    for task in candidates:
        stamp = current()
        closed = _close_skipped_reported(database, task["task_id"], stamp)
        if closed is not None:
            # A skipped or held task (closed now, left alone because another runner holds it, or
            # whose check failed): no reply AI runs on it in any of these cases.
            items.append(closed)
            continue
        database.execute("BEGIN IMMEDIATE")
        with database:
            task = routing.one(database, "SELECT * FROM workflow_tasks WHERE task_id=?", (task["task_id"],))
            successor = _revise_legacy_assets(database, task, config, stamp, provenance)
        if successor is not None:
            items.append({"task_id": task["task_id"], "state": "CONTEXT_REVISED", "workflow_status": "CANCELLED",
                          "context_revision_task_id": successor, "attempts": task["attempts"], "deadline": task["deadline"]})
            continue
        deferred = _defer_capacity(database, task["task_id"], config, stamp, timeout, budget)
        if deferred is not None:
            items.append(deferred)
            continue
        claim = workflow_tasks.claim(database, task["task_id"], stamp, lease_seconds=timeout + 15)
        if claim["status"] != "CLAIMED":
            items.append({"task_id": task["task_id"], "state": claim["status"]})
            continue
        task = routing.one(database, "SELECT * FROM workflow_tasks WHERE task_id=?", (task["task_id"],))
        # The number this run records its attempt under: the claim's own count, unless that number
        # is already on record for this task, in which case one past the highest recorded. A bulk
        # re-queue on 2026-09-23 at 08:00:09 reset 38 review tasks to attempts=0 while their
        # attempt rows - permanent history - stayed, so a re-run reused number 1, the INSERT below
        # raised IntegrityError, and that one task stopped the whole reply worker every cycle.
        # History is untouched; the new attempt gets a free number, and the compute booking, its
        # settlement, the evidence reference and the saved row all use this one number.
        recorded = database.execute("SELECT COALESCE(MAX(attempt),0) FROM reply_worker_attempts WHERE task_id=?",
                                    (task["task_id"],)).fetchone()[0]
        attempt = max(task["attempts"], recorded + 1)
        input_hash = None
        raw = None
        handoff = None
        failure = None
        reserved_at = None
        feedback = None
        tools = {"root": str(root), "task_id": task["task_id"], "calls": []}
        try:
            request, message, holds = _source(database, task, config, stamp)
            prompt = prompt_template + "\n\nUNTRUSTED_SOURCE_JSON:\n" + routing.encoded(request)
            if len(prompt.encode("utf-8")) > MAX_INPUT_BYTES:
                raise Held("CLASSIFICATION_INPUT_EXCEEDS_LIMIT")
            input_hash = sha(prompt)
            saved = routing.one(database, "SELECT * FROM reply_worker_inputs WHERE task_id=?", (task["task_id"],))
            if saved and (saved["input_sha256"] != input_hash or sha(saved["input"]) != input_hash
                          or saved["provenance"] != routing.encoded(provenance)):
                raise Held("CLASSIFICATION_IMMUTABLE_INPUT_CHANGED")
            database.execute("INSERT OR IGNORE INTO reply_worker_inputs VALUES (?,?,?,?,?)",
                             (task["task_id"], input_hash, prompt, request["source_sha256"], routing.encoded(provenance)))
            remaining = (workflow_tasks.instant(claim["lease_until"]) - workflow_tasks.instant(current())).total_seconds()
            if remaining < timeout + 5:
                raise Held("CLASSIFICATION_DEADLINE_TOO_CLOSE")
            if not workflow_tasks.reserve_compute(database, "reply:" + task["task_id"] + ":" + str(attempt),
                                                   stamp, timeout, config.get("timezone", "America/Los_Angeles"), budget):
                raise Held("DAILY_COMPUTE_BUDGET_EXHAUSTED", retry=True)
            database.commit()
            reserved_at = stamp
            # After a too-long answer the model is told so and asked to be concise (owner,
            # 2026-10-01). The note goes in front of the saved prompt, which stays the task's
            # immutable input (its hash binds the attempt rows and the decision).
            feedback = size_feedback(database, task["task_id"])
            raw = _execute(dict(config, _reply_tools=tools), feedback + "\n\n" + prompt if feedback else prompt, schema, timeout)
            handoff = _validate(raw, request, message, holds)
            checked, unused_message, unused_holds = _source(database, task, config, current())
            if routing.encoded(checked) != routing.encoded(request):
                raise Held("CLASSIFICATION_SOURCE_CHANGED")
        except Held as error:
            failure = error
        except Exception:
            failure = Held("CLASSIFICATION_LOCAL_VALIDATION_FAILED")
        interrupted = failure is not None and failure.code == "CLASSIFICATION_INTERRUPTED"
        stamp = current()
        if workflow_tasks.instant(stamp) >= workflow_tasks.instant(claim["lease_until"]):
            database.rollback()
            items.append({"task_id": task["task_id"], "state": "LEASE_EXPIRED"})
            if interrupted:
                return {"state": "INTERRUPTED", "output_version": VERSION, "items": items}
            continue
        provider = provider_limits.wait_of(failure)
        blocker = provider_limits.CODE if provider else failure.code if failure else (handoff["holds"][0] if handoff["holds"] else None)
        result = {"output_version": VERSION, "task_id": task["task_id"], "input_sha256": input_hash,
                  "output_sha256": sha(raw) if raw is not None else None,
                  "can_advance_conversion": handoff["can_advance_conversion"] if handoff and not failure else False,
                  "state": "HELD" if failure else handoff["state"]}
        if feedback:
            result["output_size_feedback_sha256"] = sha(feedback)
        if failure and failure.code in SIZE_BLOCKERS and raw is not None:
            result["output_characters"] = len(raw)
        if executor_detail(failure):
            result["executor_failure"] = executor_detail(failure)
        if tools["calls"]:
            result["tool_calls"] = tools["calls"]
        evidence = [{"source": "reply_worker_attempts:" + task["task_id"] + ":" + str(attempt), "observed_at": stamp}]
        # One task's failure to save stays that task's problem. It used to escape this loop and
        # stop every other reply in the batch; now it is reported against this task, its writes
        # roll back with the transaction, and its claim is left to lapse - claim() then re-queues
        # it the way it re-queues any interrupted attempt. Nothing new is held or recorded.
        try:
            with database:
                database.execute("INSERT INTO reply_worker_attempts VALUES (?,?,?,?,?,?,?,?)",
                                 (task["task_id"], attempt, stamp, input_hash, raw, result["output_sha256"], routing.encoded(provenance), blocker))
                if handoff is not None and failure is None:
                    handoff.update({"task_id": task["task_id"], "input_sha256": input_hash,
                                    "output_sha256": result["output_sha256"], "source_key": request["source_key"],
                                    "source_sha256": request["source_sha256"], "model_provenance": provenance})
                    database.execute("INSERT INTO reply_worker_results VALUES (?,?,?,?,?)",
                                     (task["task_id"], VERSION, input_hash, result["output_sha256"], routing.encoded(handoff)))
                    result["handoff_source"] = "reply_worker_results:" + task["task_id"]
                    # The tools' writes happen with the decision they belong to, never mid-run
                    # (owner, 2026-10-06; see reply_tools). A failed run's intents are dropped.
                    effects = reply_tools.apply_intents(database, task["task_id"], request["source_key"], tools["calls"], stamp)
                    if effects:
                        result["tool_effects"] = effects
                status = "RETRY_WAIT" if failure and failure.retry else "BLOCKED" if failure else "COMPLETE"
                if provider:
                    # The provider refused the turn for usage: a wait until its reset, not an attempt.
                    state = workflow_tasks.provider_wait(database, task["task_id"], claim["token"], stamp, provider, result)
                elif failure and failure.code == "DAILY_COMPUTE_BUDGET_EXHAUSTED":
                    # Shared compute ran out after the claim: a wait, not a try (2026-09-28).
                    reset = workflow_tasks.next_day_start(stamp, config.get("timezone", "America/Los_Angeles"),
                                                          workflow_tasks.day_start_hour(config))
                    state = workflow_tasks.compute_wait(database, task["task_id"], claim["token"], stamp,
                                                        reset.astimezone(dt.timezone.utc).isoformat(), failure.code, result)
                else:
                    # Measured from the booking, not from `stamp` (re-read after the call, which
                    # credited nearly the whole booking back, 2026-10-22). A failure keeps its charge.
                    if status == "COMPLETE" and reserved_at is not None:
                        workflow_tasks.settle_compute(database, "reply:" + task["task_id"] + ":" + str(attempt),
                                                      reserved_at, current(), "reply classification completed; shared daily pool: unused tail of a cleanly completed reservation")
                    state = workflow_tasks.finish(database, task["task_id"], claim["token"], stamp, result, evidence,
                                                  status=status, blocker=blocker,
                                                  next_action=handoff["required_next_action"] if handoff and not failure
                                                  else "Read the reply again with feedback to keep the answer concise" if failure and failure.code in SIZE_BLOCKERS
                                                  else "Investigate the recorded source or execution dependency" if blocker else None)
                if failure and failure.code in {"CLASSIFICATION_IMMUTABLE_INPUT_CHANGED", "CLASSIFICATION_SOURCE_CHANGED"}:
                    result["context_revision_task_id"] = revise_context(task, stamp)
                notify_failed_attempt(database, task, locals().get("request"), blocker, state, stamp,
                                      result.get("context_revision_task_id"))
        except Exception as error:
            # Where it failed, never what it said: a module, line and function, the same
            # code-identity-only record the orchestrator keeps for a failed worker.
            frame = traceback.extract_tb(error.__traceback__)[-1] if error.__traceback__ else None
            items.append({"task_id": task["task_id"], "state": "PERSISTENCE_FAILED", "error_type": type(error).__name__,
                          "raised_at": (os.path.basename(frame.filename) + ":" + str(frame.lineno) + " " + frame.name)
                          if frame else None})
            if interrupted:
                return {"state": "INTERRUPTED", "output_version": VERSION, "items": items}
            continue
        items.append({**result, "workflow_status": state, "blocker": blocker})
        if interrupted:
            return {"state": "INTERRUPTED", "output_version": VERSION, "items": items}
    decisions = []
    for saved in routing.rows(database, """SELECT result.task_id FROM reply_worker_results result
            JOIN workflow_tasks task ON task.task_id=result.task_id
            JOIN pipeline_routing_sources source ON source.source_key=json_extract(result.handoff,'$.source_key')
            WHERE result.output_version=? AND source.status!='ROUTED' AND task.status='COMPLETE'
            AND NOT EXISTS (SELECT 1 FROM json_each(source.result,'$.reply_decisions') consumed WHERE consumed.key=result.task_id)
            ORDER BY task.updated_at LIMIT ?""", (VERSION, limit)):
        try:
            database.execute("BEGIN IMMEDIATE")
            with database:
                decisions.append(routing.apply_reply_decision(database, saved["task_id"], config, current()))
        except (Held, ValueError, KeyError, TypeError) as error:
            failure = {"task_id": saved["task_id"], "state": "BLOCKED",
                       "blocker": error.code if isinstance(error, Held) else str(error)}
            with database:
                database.execute("""UPDATE workflow_tasks SET status='BLOCKED',blocker=?,next_action=?,updated_at=?
                    WHERE task_id=? AND status='COMPLETE'""", (failure["blocker"],
                    "Reconcile the saved reply decision with its authenticated source before applying it", current(), saved["task_id"]))
                workflow_tasks.record(database, saved["task_id"], "REPLY_DECISION_REJECTED", current(), failure)
                if failure["blocker"] == "REPLY_DECISION_CONTEXT_CHANGED":
                    task = routing.one(database, "SELECT * FROM workflow_tasks WHERE task_id=?", (saved["task_id"],))
                    failure["context_revision_task_id"] = revise_context(task, current())
                notify_failed_attempt(database, routing.one(database, "SELECT * FROM workflow_tasks WHERE task_id=?",
                                      (saved["task_id"],)), None, failure["blocker"], "BLOCKED", current(),
                                      failure.get("context_revision_task_id"))
            decisions.append(failure)
    return {"state": "COMPLETE", "output_version": VERSION, "items": items, "decisions": decisions}
