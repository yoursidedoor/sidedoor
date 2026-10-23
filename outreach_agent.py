"""AI-owned outreach inspection and reconciliation over authenticated observations."""

import resources
import datetime as dt
import hashlib
import json
from pathlib import Path

import approvals
import engagement
import linkedin_port
import outreach
import outreach_history
import outreach_provider
import outreach_promotion
import provider_limits
import reply_worker
import research_workflow as research
import sequence
import workflow_tasks
from orchestrator import profile_key
import text_model
import paths


VERSION = "outreach-decision-v1"


class SharedBudgetExhausted(TimeoutError):
    code = "OUTREACH_AI_SHARED_BUDGET_EXHAUSTED"

    def __init__(self, config, now):
        super().__init__(self.code)
        self.reset_at = workflow_tasks.next_day_start(now, config.get("timezone", "America/Los_Angeles"),
                                                      workflow_tasks.day_start_hour(config)).astimezone(dt.timezone.utc)


def _task_summary(database, task_id):
    task = database.execute("SELECT * FROM workflow_tasks WHERE task_id=?", (task_id,)).fetchone()
    result = json.loads(task["result"]) if task["result"] else {}
    outcome = result.get("outcome")
    if outcome not in {"SUPPRESSED", "CAPABILITY_GATED"}:
        outcome = "READY" if task["status"] == "COMPLETE" and not task["blocker"] else "HELD"
    return {"state": task["status"], "task_id": task_id,
            "outcome": outcome,
            **{field: task[field] for field in ("blocker", "next_action", "next_at", "attempts", "max_attempts", "deadline")}}


def _defer_capacity(database, task_id, now, error):
    task = database.execute("SELECT * FROM workflow_tasks WHERE task_id=?", (task_id,)).fetchone()
    if task["status"] not in {"QUEUED", "RETRY_WAIT", "EXHAUSTED"}:
        return
    state = "RETRY_WAIT"
    next_at = error.reset_at.isoformat()
    next_action = "Retry authenticated history inspection after shared capacity resets at " + next_at + "; do not send"
    if task["status"] == "EXHAUSTED" or task["attempts"] >= task["max_attempts"]:
        state, next_at = "EXHAUSTED", None
        next_action = "Inspection attempt limit exhausted; review retained failures before any new investigation; do not send"
    values = (state, error.code, next_action, next_at)
    if values == tuple(task[field] for field in ("status", "blocker", "next_action", "next_at")):
        return
    changed = database.execute("""UPDATE workflow_tasks SET status=?,blocker=?,next_action=?,next_at=?,updated_at=?
        WHERE task_id=? AND status=? AND attempts=? AND updated_at=? AND token_hash IS NULL""",
        (*values, now.isoformat(), task_id, task["status"], task["attempts"], task["updated_at"]))
    if changed.rowcount:
        workflow_tasks.record(database, task_id, "CAPACITY_DEFERRED", now.isoformat(),
            {"blocker": error.code, "reset_at": error.reset_at.isoformat(), "state": state,
             "next_at": next_at, "next_action": next_action, "attempts": task["attempts"], "deadline": task["deadline"]})


# Prompt-size bound (owner, 2026-09-23: "that 100kb seems too small and unreasonable").
# Grounded in the 52 packets this system has actually built: 6 of them (11.5%) exceeded the old
# hardcoded 100,000 bytes, and INVALID_PROOF is terminal, so roughly one inspection in nine was
# killed outright rather than retried. After removing the duplicated read log the worst real
# packet is 207,284 bytes (~51k tokens), so 400,000 leaves ~1.9x headroom over anything observed
# while still catching a genuine runaway - a raw DOM dump would be megabytes, not kilobytes.
# Configurable so it never has to be edited in code again.
def prompt_limit(config):
    limit = (config.get("outreach_agent") or {}).get("max_prompt_bytes", 400000)
    return limit if type(limit) is int and 1000 <= limit <= 4000000 else 400000


def _defer_known_capacity(database, task_id, config, now, collector_seconds=0):
    timeout = config.get("outreach_agent", {}).get("timeout_seconds", 60)
    cap = config.get("daily_compute_seconds", 10800)
    if type(timeout) is not int or not 1 <= timeout <= 600 or not timeout <= cap <= 21000:
        raise ValueError("OUTREACH_AGENT_INPUT_OR_TIME_BOUND")
    if type(collector_seconds) is not int or not 0 <= collector_seconds <= 240:
        raise ValueError("HISTORY_COLLECTOR_COMPUTE_BOUND_INVALID")
    day = workflow_tasks.budget_day(now, config.get("timezone", "America/Los_Angeles"))
    database.execute("BEGIN IMMEDIATE")
    with database:
        task = database.execute("SELECT status FROM workflow_tasks WHERE task_id=?", (task_id,)).fetchone()
        reserved = workflow_tasks.compute_used(database, day)
        if task["status"] in {"QUEUED", "RETRY_WAIT"} and reserved + timeout + collector_seconds > cap:
            _defer_capacity(database, task_id, now, SharedBudgetExhausted(config, now))
            return True
    return False


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def initialize(database):
    database.execute("""CREATE TABLE IF NOT EXISTS outreach_agent_audit (
        id TEXT PRIMARY KEY, action_id TEXT NOT NULL, phase TEXT NOT NULL,
        kind TEXT NOT NULL, observed_at TEXT NOT NULL, record TEXT NOT NULL)""")
    # E6.T4: the Approvals page reads one action's DECISION rows (the judge's verdict on a card).
    database.execute("CREATE INDEX IF NOT EXISTS outreach_agent_audit_action ON outreach_agent_audit(action_id, kind)")
    for operation in ("UPDATE", "DELETE"):
        database.execute(f"CREATE TRIGGER IF NOT EXISTS outreach_agent_no_{operation.lower()} BEFORE {operation} ON outreach_agent_audit BEGIN SELECT RAISE(ABORT,'Immutable outreach agent evidence'); END")


def audit(database, action_id, phase, kind, observed_at, record):
    identifier = digest([action_id, phase, kind, record])
    with database:
        database.execute("INSERT OR IGNORE INTO outreach_agent_audit VALUES (?,?,?,?,?,?)",
                         (identifier, action_id, phase, kind, observed_at, encoded(record)))
    return identifier


def _direct_proof(action, config, now):
    contexts = config.get("outreach_promotion_contexts", {})
    if not isinstance(contexts, dict):
        return {}
    context = {}
    identity = profile_key(action["profile"])
    for key, value in contexts.items():
        try:
            if key == identity or profile_key(key) == identity:
                context = value
                break
        except (ValueError, TypeError):
            continue
    proof = context.get("proof", {}) if isinstance(context, dict) else {}
    if not isinstance(proof, dict):
        return {}
    if (proof.get("identity") != profile_key(action["profile"])
            or proof.get("sender_id") != config.get("sender_id") or not proof.get("evidence")
            or not outreach._fresh(proof.get("checked_at"), now, config.get("outreach_history_max_age_seconds", 3600))):
        return {}
    return proof


def _validate(raw, packet):
    output = json.loads(raw, object_pairs_hook=reply_worker._unique_object)
    fields = {"output_version", "source_sha256", "decision", "reason", "next_action", "findings",
              "conversation_id", "receipt_observation", "receipt_message"}
    if not isinstance(output, dict) or set(output) not in (fields, fields | {"route"}) or output["output_version"] != VERSION or output["source_sha256"] != digest(packet):
        raise ValueError("OUTREACH_DECISION_BINDING_INVALID")
    if "route" in output and output["route"] not in {"EXISTING_THREAD", "INVITATION", "UNKNOWN"}:
        raise ValueError("OUTREACH_ROUTE_INVALID")
    if output["decision"] not in {"READY", "BLOCKED", "UNKNOWN", "SENT", "NOT_SENT"}:
        raise ValueError("OUTREACH_DECISION_INVALID")
    for field in ("reason", "next_action"):
        if not isinstance(output[field], str) or not 1 <= len(output[field].strip()) <= 700:
            raise ValueError("OUTREACH_DECISION_EXPLANATION_REQUIRED")
    findings = output["findings"]
    if not isinstance(findings, dict) or set(findings) != {"prior_outreach", "has_reply", "negative_reply", "opt_out"}:
        raise ValueError("OUTREACH_FINDINGS_INVALID")
    if any(value is not None and type(value) is not bool for value in findings.values()):
        raise ValueError("OUTREACH_UNKNOWN_FACTS_MUST_REMAIN_UNKNOWN")
    if output["conversation_id"] is not None and (not isinstance(output["conversation_id"], str) or not output["conversation_id"]):
        raise ValueError("OUTREACH_THREAD_INVALID")
    for field in ("receipt_observation", "receipt_message"):
        if output[field] is not None and (type(output[field]) is not int or output[field] < 0):
            raise ValueError("OUTREACH_RECEIPT_REFERENCE_INVALID")
    return output


# Pre-send judge of a first note (INITIAL, FIRST_MESSAGE, PREPARE_CAMPAIGN and, in this product, the
# Unipile INVITE at INSPECTION: every purpose `_judge` treats as `initial`), owner
# 2026-10-13 ("Yes" to low reasoning for the simple yes/no judges): with config["text_model"] set
# and no injected executor it is one OpenRouter chat-completions call (text_model.complete) with
# reasoning effort "low" and a bounded max_tokens, not `codex exec`, which did not pass the effort
# through. Same prompt, schema (in a system message and as the strict response format codex's
# --output-schema sent), validation, repair turns, compute booking and
# settlement, timeout and attempt counting; a failure is the same retryable Held the codex run
# raises, carrying the HTTP status (never the key) as its executor record.
DIRECT_ROUTE = "openrouter_chat_completions"
DIRECT_EFFORT = "low"
DIRECT_MAX_TOKENS = 4000
# Only providers that honour response_format (require_parameters), cheapest first.
DIRECT_PROVIDER = {"sort": "price", "require_parameters": True}


def _direct_execute(config, prompt, schema, timeout, calls):
    """reply_worker._execute's contract (text of the model's JSON answer, or a Held) over text_model.complete."""
    system = "Return only one JSON object, with no other text, that matches this JSON Schema:\n" + schema
    started = dt.datetime.now(dt.timezone.utc)

    def failed(code, error, retry=True):
        held = reply_worker.Held(code, retry=retry)
        held.executor = {"route": DIRECT_ROUTE, "http_status": getattr(error, "status", None),
                         "duration_seconds": round((dt.datetime.now(dt.timezone.utc) - started).total_seconds(), 1),
                         "error": error.record() if isinstance(error, text_model.TextModelError)
                         else type(error).__name__ + ": " + str(error)[:300]}
        calls.append({"failure": held.executor})
        return held

    try:
        # The strict schema codex sent with --output-schema: without it GLM's reason and
        # next_action run past their 700 characters (a third of the codex answers were cut at
        # exactly 700) and _validate refuses the answer.
        answer = text_model.complete(config, prompt, system=system, effort=DIRECT_EFFORT,
                                     max_tokens=DIRECT_MAX_TOKENS, timeout=timeout,
                                     response_format={"type": "json_schema", "json_schema": {
                                         "name": "outreach_decision", "strict": True, "schema": json.loads(schema)}},
                                     provider=DIRECT_PROVIDER)
    except TimeoutError as error:
        raise failed("CLASSIFICATION_TIMEOUT", error) from error
    except text_model.TextModelError as error:
        raise failed("SUBSCRIPTION_EXECUTOR_FAILED", error) from error
    except OSError as error:  # the key file could not be read
        raise failed("SUBSCRIPTION_EXECUTOR_UNAVAILABLE", error, retry=False) from None
    calls.append({"usage": answer["usage"], "finish_reason": answer["finish_reason"], "model": answer["model"]})
    content = answer["content"]
    try:
        json.loads(content)
        return content
    except ValueError:
        pass
    try:
        return json.dumps(text_model.json_object(content))
    except ValueError:
        return content  # _validate refuses it and the repair turn shows the model its answer


class Adapter(outreach.HeyReachActionAdapter):
    def __init__(self, root, database, config, client, execute=None, *, history_collector=None, port=None,
                 port_drafts=False):
        self.root = Path(root)
        self.database = database
        self.config = config
        self.provider = outreach_provider.Provider(database, config, client)
        self.execute = execute or reply_worker._execute
        self.injected_execute = execute is not None
        self.history_collector = history_collector
        self.active_action = None
        # E3.T3: `port_drafts` (prepare_drafts with config["linkedin_provider"] == "unipile") reads every
        # draft inspection through the port; otherwise only an INVITE or a Unipile INITIAL/FIRST_MESSAGE is.
        self.port_drafts = port_drafts
        initialize(database)
        database.commit()
        super().__init__(self._transport, self.inspect, self.reconcile, port=port)

    # --- the Unipile path (E3.T3): the same judgment, proof and final checks over the LinkedInPort ---
    def _port_read(self, action):
        return self.port is not None and (action.get("purpose") == sequence.INVITE
                                          or action.get("provider") == sequence.SOURCE or self.port_drafts)

    def _port_message(self, action):
        return (self.port is not None and action.get("purpose") in {"INITIAL", "FIRST_MESSAGE"}
                and action.get("provider") == sequence.SOURCE)

    def _port_observation(self, action):
        """The pre-send read over the port, in place of Provider.collect: the profile (connection
        distance, pending or received invitation) and the account and the person's chats with their
        messages (followup_execution.port_observation). A read that cannot decide raises."""
        import followup_execution
        person = self.port.lookup(action["profile"])
        observation = followup_execution.port_observation(self.port, action["profile"])
        observation["person"] = {"provider_id": person.provider_id, "network_distance": person.network_distance,
                                 "pending_invitation": person.pending_invitation,
                                 "invitation_received": sequence.received_invitation(person),
                                 "raw": sequence._jsonable(person.raw)}
        observation["operation_capabilities"] = {
            "INVITATION": {"supported": True, "operations": ["send_invitation"]},
            # A message over the port: INITIAL into the person's existing chat (as HeyReach's existing-thread
            # route), FIRST_MESSAGE into the chat or by start_chat when there is none.
            "EXISTING_THREAD": {"supported": self._port_message(action), "operations": ["send_message", "start_chat"]},
            "permission": "EXISTING_ACCOUNT_PROVIDER_ENFORCED_AT_REQUEST", "remaining_quota": "UNKNOWN_PROVIDER_ENFORCED"}
        return observation

    def judgment_context(self, action, config, now):
        return {}

    def collect(self, action, phase, claim=None, **named):
        """The provider read the judge sees: the port read on the Unipile path (reply_execution reads a
        Unipile REPLY over the port itself), else Provider.collect."""
        if self._port_read(action):
            return self._port_observation(action)
        return self.provider.collect(action, phase, claim, **named)

    def judgment_observation(self, action, observation):
        """Bound model context for new-contact inspection; unrelated network entries are not evidence.

        get_my_network_for_sender returns up to 100 unrelated connections (~70 KB twice over as
        raw/result), which pushed the packet past the 100 KB prompt bound
        (OUTREACH_AGENT_INPUT_OR_TIME_BOUND) on every inspection. Entries that could be the
        recipient (identity key or alias, the verified lead's LinkedIn id, or the lead's exact name)
        are kept, so positive history still reaches the AI; the original count and hash are kept.
        """
        if action.get("purpose") not in {"INITIAL", "FIRST_MESSAGE", "PREPARE_CAMPAIGN", "INVITE"}:
            return observation
        trimmed = json.loads(json.dumps(observation))
        try:
            identities = engagement.identity_keys(self.database, action["profile"])
        except (ValueError, TypeError, KeyError):
            identities = set()
        lead_ids, names = set(), set()
        for read in trimmed.get("observations", []):
            raw = read.get("raw")
            if read.get("operation") == "get_lead" and read.get("identity_matches") is True and isinstance(raw, dict):
                if raw.get("linkedin_id"):
                    lead_ids.add(str(raw["linkedin_id"]))
                name = " ".join(str(raw.get(field) or "").strip().lower() for field in ("firstName", "lastName")).strip()
                if " " in name:
                    names.add(name)

        def related(item):
            if not isinstance(item, dict):
                return True
            try:
                if profile_key(item.get("profileUrl")) in identities:
                    return True
            except (ValueError, TypeError, AttributeError):
                pass
            name = " ".join(str(item.get(field) or "").strip().lower() for field in ("firstName", "lastName")).strip()
            return str(item.get("linkedin_id") or "") in lead_ids or name in names

        for read in trimmed.get("observations", []):
            raw = read.get("raw")
            if read.get("operation") == "get_my_network_for_sender" and isinstance(raw, dict) and isinstance(raw.get("items"), list):
                read["original_item_count"] = len(raw["items"])
                read["original_raw_sha256"] = digest(raw)
                raw["items"] = [item for item in raw["items"] if related(item)]
                read["judgment_window"] = "only entries matching the recipient identity, verified lead id or name; unrelated connections omitted"
            if "result" in read:
                read["result"] = raw
        # The model-facing packet carries the read log ONCE (owner, 2026-09-23). The provider
        # envelope exposes it as both `observations` and `reads`; re-applying that alias here
        # sent the whole log twice to the model. It was 145,644 bytes in the largest
        # inspection packets, 41%% of the payload, and it is what pushed Robin Vale (903
        # bytes over) and Sasha Lind past the prompt bound. No prompt, schema or consumer
        # refers to the packet's `reads`; every reader in these modules uses `observations`.
        trimmed.pop("reads", None)
        return trimmed

    def _judge(self, action, phase, config, claim=None, request=None):
        initial = action.get("purpose") in {"INITIAL", "FIRST_MESSAGE", "PREPARE_CAMPAIGN", "INVITE"}
        observation = self.judgment_observation(action, self.collect(
            action, phase, claim, new_contact_history=initial and phase == "INSPECTION"))
        now = engagement.timestamp(observation["finished_at"])
        # History comes from our own records now (owner, 2026-09-29: the LinkedIn browser check is
        # removed); no browser read, no cache of one, no compute booked for it.
        history = (outreach_history.collect(self.database, config, action, now)
                   if initial and phase == "INSPECTION" else None)
        if history is not None:
            observation["history_collection"] = history
        packet = {"action": action, "action_sha256": digest(action), "phase": phase,
                  "claim": dict(claim) if claim is not None else None, "request": request, "observation": observation,
                  "direct_history": history["proof"] if history is not None else {} if initial else _direct_proof(action, config, now),
                  **self.judgment_context(action, config, now)}
        template = resources.read_text("outreach-agent-prompt.md")
        schema = resources.read_text("outreach-agent-schema.json")
        source_hash = digest(packet)
        prompt = template + "\n\nUNTRUSTED_SOURCE_JSON:\n" + encoded({"source_sha256": source_hash, **packet})
        provenance = {"model": text_model.model(config), "executable": config["codex"], "auth": text_model.auth(config),
                      "prompt_sha256": reply_worker.sha(template), "schema_sha256": reply_worker.sha(schema)}
        # The pre-send judge of a first note is a direct low-reasoning call (owner, 2026-10-13).
        direct = (initial and phase == "INSPECTION" and not self.injected_execute
                  and text_model.settings(config) is not None)
        calls = []
        execute = (lambda config, prompt, schema, timeout: _direct_execute(config, prompt, schema, timeout, calls)
                   ) if direct else self.execute
        if direct:
            provenance = dict(provenance, executable=None, route=DIRECT_ROUTE, reasoning_effort=DIRECT_EFFORT,
                              max_tokens=DIRECT_MAX_TOKENS)
        # The approved route (owner, 2026-10-22: "the approval should be enough"): an owner approval of
        # this exact payload stands in for the send judge. The reads above ran as always; no compute is
        # booked and no model is called. Reconciliation judgments are unchanged.
        approved = approvals.approval_for(self.database, action) if phase == "INSPECTION" else None
        if approved is not None:
            provenance = {"route": "OWNER_APPROVAL", "decision_source": "OWNER_APPROVAL:" + approved["record_id"]}
        reference = audit(self.database, action["action_id"], phase, "OBSERVATION", now.isoformat(),
                          {"packet": packet, "source_sha256": source_hash, "provenance": provenance})
        if approved is not None:
            decision = approvals.decision(action, approved)
            audit(self.database, action["action_id"], phase, "DECISION", now.isoformat(),
                  {"observation_id": reference, "decision": decision, "decision_source": decision["decision_source"]})
            return observation, packet["direct_history"], decision, reference
        settings = config.get("outreach_agent", {})
        timeout = settings.get("timeout_seconds", 60)
        if type(timeout) is not int or not 1 <= timeout <= 600 or len(prompt.encode()) > prompt_limit(config):
            raise ValueError("OUTREACH_AGENT_INPUT_OR_TIME_BOUND")
        try:
            with self.database:
                if not workflow_tasks.reserve_compute(self.database, "outreach-ai:" + reference, now.isoformat(), timeout,
                        config.get("timezone", "America/Los_Angeles"), config.get("daily_compute_seconds", 10800)):
                    raise SharedBudgetExhausted(config, now)
            raw = execute(config, prompt, schema, timeout)
            def _record(turn, reason):
                audit(self.database, action["action_id"], phase, "DECISION_REPAIR", now.isoformat(),
                      {"observation_id": reference, "turn": turn, "rejection_reason": reason})

            repairs = []

            def _reserve(turn):
                booking = "outreach-ai-repair:" + reference + ":" + str(turn)
                repairs.append((booking, outreach_provider._now()))
                with self.database:
                    return bool(workflow_tasks.reserve_compute(
                        self.database, booking, now.isoformat(),
                        timeout, config.get("timezone", "America/Los_Angeles"),
                        config.get("daily_compute_seconds", 10800)))

            decision, packet = _output_with_repair_turns(
                execute, config, template, schema, timeout, packet, raw, _validate,
                record=_record, reserve=_reserve)
            current = self.database.execute("SELECT payload FROM outreach_actions WHERE action_id=?", (action["action_id"],)).fetchone()
            if current and json.loads(current[0]) != action:
                raise ValueError("OUTREACH_ACTION_CHANGED_DURING_INVESTIGATION")
            audit(self.database, action["action_id"], phase, "DECISION", now.isoformat(),
                  {"observation_id": reference, "output": raw, "output_sha256": reply_worker.sha(raw), "decision": decision,
                   **({"route": DIRECT_ROUTE, "reasoning_effort": DIRECT_EFFORT, "max_tokens": DIRECT_MAX_TOKENS,
                       "calls": calls} if direct else {})})
            with self.database:
                workflow_tasks.settle_compute(self.database, "outreach-ai:" + reference, now.isoformat(),
                                              outreach_provider._now(),
                                              "outreach judgment completed; shared daily pool: unused tail of a cleanly completed reservation")
                # Repair turns of a completed judgment are settled too (2026-10-02): they were
                # booked at the full timeout and never credited, 6 x 600 s on 09-29..10-02.
                for booking, started in repairs:
                    workflow_tasks.settle_compute(self.database, booking, started, outreach_provider._now(),
                                                  "outreach judgment repair turn completed; shared daily pool: unused tail of a cleanly completed reservation")
            return observation, packet["direct_history"], decision, reference
        except Exception as error:
            audit(self.database, action["action_id"], phase, "FAILURE", now.isoformat(),
                  {"observation_id": reference, "error_type": type(error).__name__,
                   "code": getattr(error, "code", None), "error": str(error)[:300],
                   **({"next_at": error.reset_at.isoformat()} if isinstance(error, SharedBudgetExhausted) else {}),
                   **({"executor": reply_worker.executor_detail(error)} if reply_worker.executor_detail(error) else {})})
            if isinstance(error, reply_worker.Held) and error.retry:
                raise TimeoutError("OUTREACH_AI_TRANSIENT_FAILURE") from None
            raise

    def inspect(self, action, config):
        observation, direct, decision, reference = self._judge(action, "INSPECTION", config)
        thread = decision["conversation_id"]
        port = observation.get("route") == sequence.SOURCE
        if port:
            # The port's reads: the account, every chat with the person (complete or not) and the profile.
            account = {}
            rooms = [item for item in observation["observations"] if item.get("operation") == "messages"]
            selected = [item for item in rooms if item["chat_id"] == thread]
            messages = [message for item in rooms for message in item["messages"]]
            authenticated, own = observation["account"].get("valid") is True, observation["account"].get("own_profile")
        else:
            account = next((item["raw"] for item in observation["observations"]
                            if item["operation"] == "get_linked_in_account_by_id" and item["status"] == "OBSERVED"), {})
            rooms = [item for item in observation["observations"] if item["operation"] == "get_chatroom"
                     and item.get("identity_matches") is True and item["status"] == "OBSERVED"]
            selected = [item for item in rooms if item["raw"].get("id") == thread]
            messages = [message for item in rooms for message in item["raw"].get("messages", []) if isinstance(message, dict)]
            authenticated, own = account.get("authIsValid") is True, account.get("profileUrl")
        sender_matches = False
        try:
            # The port may not name the account's own profile (as followup_execution's port path).
            sender_matches = (port and own is None) or profile_key(own) == profile_key(config["sender_profile"])
        except (KeyError, ValueError, TypeError):
            pass
        findings = dict(decision["findings"])
        if any(message.get("sender") == "ME" for message in messages):
            findings["prior_outreach"] = True
        if any(message.get("sender") == "CORRESPONDENT" for message in messages):
            findings["has_reply"] = True
        proof = {"identity": profile_key(action["profile"]), "sender_id": config["sender_id"],
                 "sender_ready": bool(sender_matches and observation.get("identity_verified") is True
                    and authenticated),  # isActive only means a campaign is running
                 "entitlement_verified": direct.get("entitlement_verified") is True and bool(direct.get("entitlement_evidence")),
                 "direct_messages_complete": direct.get("direct_messages_complete") is True,
                 "sent_invitations_complete": direct.get("sent_invitations_complete") is True,
                 "pending_invitation": direct.get("pending_invitation"), "cooldown": direct.get("cooldown"),
                 "checked_at": direct.get("checked_at", observation["finished_at"]), "evidence": "outreach_agent_audit:" + reference,
                 "conversation_id": thread if len(selected) == 1 else None,
                 "company_contact_count": direct.get("company_contact_count"),
                 "company_count_evidence": direct.get("company_count_evidence"),
                 "followups_sent": direct.get("followups_sent"), "initial_message_sent": direct.get("initial_message_sent"),
                 "agent_decision": decision["decision"], "next_action": decision["next_action"], **findings}
        proof["route"] = decision.get("route", "EXISTING_THREAD" if thread else "UNKNOWN")
        provider_gates = []
        if action.get("purpose") in {"INITIAL", "FIRST_MESSAGE", "PREPARE_CAMPAIGN", "INVITE"}:
            capability = observation.get("operation_capabilities", {}).get(proof["route"], {})
            proof["entitlement_verified"] = bool(proof["sender_ready"] and capability.get("supported") is True)
            proof["entitlement_basis"] = "EXISTING_AUTHENTICATED_ACCOUNT_SUPPORTED_OPERATION_PROVIDER_ENFORCED"
            proof["remaining_quota"] = "UNKNOWN_PROVIDER_ENFORCED"
            proof["provider_transport_supported"] = capability.get("supported") is True
            restriction_fields = ("isLimited", "isBlocked")
            if proof["route"] == "EXISTING_THREAD":
                restriction_fields += ("messageCooldown", "messagingRestricted")
            restrictions = {field: account[field] for field in restriction_fields if field in account}
            restricted = any(value is not False for value in restrictions.values())
            proof["entitlement_verified"] = proof["entitlement_verified"] and not restricted
            proof.update(provider_restriction_fields=restrictions, provider_restriction_observed=restricted,
                         quota_remaining=None, quota_basis="UNKNOWN_PROVIDER_ENFORCED")
            if proof["route"] == "EXISTING_THREAD":
                room = selected[0]["raw"] if len(selected) == 1 and not port else {}
                # The port reports no block flags; a refused write raises instead.
                thread_fields = {} if port else {field: room.get(field) for field in ("blockedByMe", "blockedByParticipant")}
                restrictions.update(thread_fields)
                restricted = restricted or any(value is True for value in thread_fields.values())
                thread_verified = ((len(selected) == 1 and selected[0]["complete"] is True
                                    or thread is None and not rooms) if port else
                                   len(selected) == 1 and selected[0].get("message_coverage") == "OBSERVED_COUNT_MATCHES"
                                   and all(value is False for value in thread_fields.values()))
                proof["provider_restriction_observed"] = restricted
                proof["entitlement_verified"] = proof["entitlement_verified"] and thread_verified and not restricted
                proof["cooldown"] = True if restricted else False if thread_verified and proof["sender_ready"] else None
                proof["cooldown_basis"] = "OBSERVED_ACCOUNT_THREAD_RESTRICTIONS_ONLY_PROVIDER_ENFORCED"
                if not thread_verified:
                    provider_gates.append("EXISTING_THREAD_BLOCK_STATUS_OR_MESSAGE_COVERAGE_UNVERIFIED")
        if port and (proof["route"] == "INVITATION" or action.get("purpose") == "INVITE"):
            # Unipile reports no connection-request cooldown fields: unknown, provider-enforced (not
            # a gate, as no field could ever settle it). The send's own profile gates apply here too.
            person = observation["person"]
            proof["cooldown"] = True if proof.get("provider_restriction_observed") is True else None
            proof["cooldown_basis"] = "NOT_REPORTED_BY_UNIPILE_PROVIDER_ENFORCED"
            provider_gates.extend(sequence.invite_gates(linkedin_port.Person(
                action["profile"], person["provider_id"], person["network_distance"], person["pending_invitation"],
                person["raw"])))
        elif proof["route"] == "INVITATION" or action.get("purpose") in {"PREPARE_CAMPAIGN", "INVITE"}:
            cooldowns = [account.get(field) for field in ("connectionRequestCooldown", "connectionNoteCooldown")]
            proof.setdefault("provider_restriction_fields", {}).update({field: account[field] for field in
                ("connectionRequestCooldown", "connectionNoteCooldown") if field in account})
            if any(value is True for value in cooldowns) or proof.get("provider_restriction_observed") is True:
                proof["cooldown"] = True
            elif all(value is False for value in cooldowns):
                proof["cooldown"] = False
            else:
                proof["cooldown"] = None
                provider_gates.append("INVITATION_PROVIDER_COOLDOWN_FIELDS_UNVERIFIED")
            proof["cooldown_basis"] = "OBSERVED_CONNECTION_REQUEST_AND_NOTE_COOLDOWNS"
        proof["provider_snapshot_complete"] = (all(item["complete"] is True for item in rooms) if port else
            all(observation.get("coverage", {}).get(field, {}).get("status") == "BOUNDED_SCAN_COMPLETE"
                for field in ("get_conversations_v2", "chatrooms", "get_campaigns_for_lead")))
        if port and action.get("purpose") != "FIRST_MESSAGE" and observation["person"]["pending_invitation"] is True:
            proof["pending_invitation"] = True
        if port and self._port_message(action) and action["purpose"] == "FIRST_MESSAGE" and proof["provider_snapshot_complete"]:
            # What outreach.action_gates asks of a FIRST_MESSAGE (initial_message_sent False, followups_sent
            # equal to those recorded), as the complete read of the person's chats shows it.
            ours = any(message.get("sender") == "ME" for message in messages)
            proof.update(initial_message_sent=ours, followups_sent=None if ours else 0)
        if action.get("purpose") == "PREPARE_CAMPAIGN":
            lists = [item for item in observation["observations"] if item["operation"] == "get_leads_from_list"
                     and item["status"] == "OBSERVED" and item["arguments"].get("listId") == action.get("list_id")]
            single = False
            try:
                single = len(lists) == 1 and lists[0]["raw"]["totalCount"] == 1 and len(lists[0]["raw"]["items"]) == 1 and profile_key(
                    lists[0]["raw"]["items"][0]["profileUrl"]) in engagement.identity_keys(self.database, action["profile"])
            except (KeyError, TypeError, ValueError, IndexError):
                pass
            sequences = [item for item in observation["observations"] if item["operation"] == "get_campaign_sequence"
                         and item["status"] == "OBSERVED" and item["arguments"].get("campaignId") == action.get("sequence_reference_campaign_id")
                         and isinstance(item["raw"], dict) and item["raw"].get("nodeType") == "CONNECTION_REQUEST"]
            proof.update(list_id=action.get("list_id"), single_contact_list_verified=single,
                         sequence=action.get("sequence"), sequence_source="outreach_agent_audit:" + reference if len(sequences) == 1 else None)
        for field in findings:
            if direct.get(field) is True:
                proof[field] = True
            elif proof[field] is False and direct.get(field) is not False:
                # Kept as False (owner, 2026-09-23). This overwrote an explicit AI "no" with
                # "unknown" whenever the browser proof did not independently also say no - and
                # `outreach.action_gates` then treats unknown exactly like yes, so an AI that
                # correctly reported no cooldown and no pending invitation produced
                # COOLDOWN_PRESENT_OR_UNKNOWN. The model is asked to judge these; this threw the
                # judgement away. A direct proof saying True still wins, on the line above.
                pass
        if action.get("purpose") in {"INITIAL", "FIRST_MESSAGE", "PREPARE_CAMPAIGN", "INVITE"}:
            # Saved local findings are not re-read for INITIAL/PREPARE_CAMPAIGN: our records were
            # checked once, before the draft was created (owner, 2026-10-01).
            previous = (None if action.get("purpose") in outreach_history.PRE_DRAFT_CHECKED
                        else outreach_history.saved_positive(self.database, action["profile"]))
            if previous:
                for field, value in previous["findings"].items():
                    # A first message follows our own accepted invitation (owner, 2026-09-29).
                    if value is True and not (action.get("purpose") == "FIRST_MESSAGE"
                                              and field in {"prior_outreach", "pending_invitation"}):
                        proof[field] = True
            positive = {field: proof.get(field) for field in outreach_history.POSITIVE_FIELDS}
            history = observation.get("history_collection", {})
            proof["capability_gates"] = [*history.get("capability_gates", []), *provider_gates]
            if proof["capability_gates"]:
                proof["next_action"] = history["next_action"]
            # A Unipile FIRST_MESSAGE with no chat goes out by start_chat (outreach.action_gates).
            if (not proof.get("conversation_id") and proof["route"] != "INVITATION" and action.get("purpose") not in {"PREPARE_CAMPAIGN", "INVITE"}
                    and not (port and self._port_message(action) and action["purpose"] == "FIRST_MESSAGE")):
                proof["capability_gates"] = [*proof["capability_gates"], "SUPPORTED_NEW_CONTACT_THREAD_OR_PREPARED_CAMPAIGN_REQUIRED"]
            # A first message follows our own accepted invitation (owner, 2026-09-29): only a reply,
            # negative reply or opt-out suppresses it.
            suppressing = ({field: positive[field] for field in ("has_reply", "negative_reply", "opt_out")}
                           if action.get("purpose") == "FIRST_MESSAGE" else positive)
            proof["history_outcome"] = ("SUPPRESSED" if any(value is True for value in suppressing.values())
                                        else "CAPABILITY_GATED" if proof["capability_gates"] else "INVESTIGATED")
            audit(self.database, action["action_id"], "INSPECTION", "HISTORY", observation["finished_at"],
                  {"identity": profile_key(action["profile"]), "sender_id": config["sender_id"],
                   "findings": positive, "observation_id": reference,
                   "capability_gates": proof["capability_gates"], "outcome": proof["history_outcome"]})
        return proof

    def reconcile(self, action, config):
        row = self.database.execute("SELECT * FROM outreach_action_claims WHERE action_id=?", (action["action_id"],)).fetchone()
        if not row:
            raise ValueError("OUTREACH_RECONCILIATION_CLAIM_REQUIRED")
        claim = dict(row)
        if self._port_message(action):
            # Read the person's chats: our exact text shown after the request started is the receipt;
            # anything else stays unknown and is never resent (followup_execution.port_receipt).
            import followup_execution
            return followup_execution.port_receipt(self, action, "unipile-" + action["purpose"].lower().replace("_", "-") + ":")
        if action.get("purpose") == "PREPARE_CAMPAIGN":
            import outreach_campaign
            result = outreach_campaign.reconcile_prepared(self.database, config, self.provider.client,
                                                          action, claim, outreach_provider._now())
            audit(self.database, action["action_id"], "RECONCILIATION", "CAMPAIGN_RECEIPT",
                  outreach_provider._now(), result)
            return result
        saved = self.database.execute("SELECT result FROM outreach_actions WHERE action_id=?", (action["action_id"],)).fetchone()
        result = json.loads(saved[0]) if saved and saved[0] else {}
        request = result.get("provider_request") if isinstance(result, dict) else None
        observation, unused_direct, decision, reference = self._judge(action, "RECONCILIATION", config, claim, request)
        unknown = {"state": "UNKNOWN", "evidence": "outreach_agent_audit:" + reference,
                   "next_action": decision["next_action"], "agent_decision": decision["decision"]}
        if decision["decision"] != "SENT":
            if decision["decision"] == "NOT_SENT":
                unknown["next_action"] = "Obtain an authenticated terminal attempt rejection; this provider reader exposes no proof that an absent message cannot still execute"
            return unknown
        try:
            expected = {"conversationId": action["conversation_id"], "linkedInAccountId": claim["sender_id"],
                        "message": action["message"], "subject": action.get("subject", "")}
            if (not isinstance(request, dict) or request.get("operation") != "send_message"
                    or request.get("status") != "REQUEST_STARTED" or request.get("payload_hash") != claim["payload_hash"]
                    or request.get("arguments") != expected):
                raise ValueError()
            started = engagement.timestamp(request["started_at"])
            if started < engagement.timestamp(claim["claimed_at"]):
                raise ValueError()
            if decision["receipt_observation"] is None or decision["receipt_message"] is None:
                raise ValueError()
            receipt_read = observation["observations"][decision["receipt_observation"]]
            room = receipt_read["raw"]
            message = room["messages"][decision["receipt_message"]]
            if (receipt_read["operation"] != "get_chatroom" or receipt_read["status"] != "OBSERVED"
                    or receipt_read.get("authenticated") is not True or receipt_read.get("identity_matches") is not True
                    or room["id"] != action["conversation_id"] or decision["conversation_id"] != action["conversation_id"]
                    or message["sender"] != "ME" or message["body"] != action["message"]
                    or (message.get("subject") or "") != action.get("subject", "")):
                raise ValueError()
            occurred = engagement.timestamp(message["createdAt"])
            if not started <= occurred <= engagement.timestamp(receipt_read["finished_at"]):
                raise ValueError()
        except (KeyError, ValueError, TypeError, IndexError):
            return dict(unknown, next_action="Investigate the claim-bound issued request, sender, thread, exact message and request timing; no retry")
        return {"independent": True, "outcome": "SENT", "action_id": action["action_id"],
                "identity": claim["identity"], "payload_hash": claim["payload_hash"], "sender_id": claim["sender_id"],
                "operation": "send_message", "conversation_id": action["conversation_id"],
                "receipt_id": "heyreach-observed-sha256:" + digest([claim["sender_id"], action["conversation_id"], message]),
                "receipt_id_kind": "independently_observed_message_content_hash", "evidence": "outreach_agent_audit:" + reference,
                "observed_at": receipt_read["finished_at"], "occurred_at": occurred.isoformat(), "ai_decision": decision}

    def _transport(self, operation, arguments):
        if operation not in {"send_message", "create_campaign"} or not self.active_action:
            raise ValueError("ONLY_CLAIMED_IN_SCOPE_MESSAGE_TRANSPORT")
        record = self.database.execute("SELECT payload FROM outreach_action_claims WHERE action_id=?", (self.active_action,)).fetchone()
        action = json.loads(record[0])
        expected = {"conversationId": action["conversation_id"], "linkedInAccountId": self.config["sender_id"],
                    "message": action["message"], "subject": action.get("subject", "")} if operation == "send_message" else outreach_provider.campaign_arguments(action, self.config["sender_id"])
        if operation != ("create_campaign" if action.get("purpose") == "PREPARE_CAMPAIGN" else "send_message"):
            raise ValueError("TRANSPORT_OPERATION_DIFFERS_FROM_CLAIM")
        if arguments != expected:
            raise ValueError("TRANSPORT_ARGUMENTS_DIFFER_FROM_CLAIM")
        result = (self._send_port(self.active_action, self.config) if self._port_message(action)
                  else self.provider.send_claimed(self.active_action, self.config))
        audit(self.database, self.active_action, "SEND", "ACKNOWLEDGMENT", result["finished_at"], result)
        return result

    def _send_port(self, action_id, config):
        """outreach_provider.Provider.send_claimed for an INITIAL or FIRST_MESSAGE on the Unipile path: the
        same final checks, one request after REQUEST_STARTED is committed (send_message into the chat on
        file; a FIRST_MESSAGE with none uses start_chat, an INITIAL needs the chat as on HeyReach), and an
        exception from the write left uncertain for reconciliation, never resent.
        As followup_execution's port send, the provider id for start_chat is read first; a read that
        fails leaves the action uncertain (reconciled, never resent)."""
        if self.database.in_transaction:
            raise ValueError("Parent must commit REQUEST_STARTED before transport")
        preview = self.database.execute("""SELECT claim.payload,claim.payload_hash,claim.sender_id,action.result
            FROM outreach_action_claims claim JOIN outreach_actions action USING(action_id)
            WHERE claim.action_id=? AND action.state='REQUEST_STARTED'
            AND action.payload=claim.payload AND action.payload_hash=claim.payload_hash
            AND action.identity=claim.identity""", (action_id,)).fetchone()
        pending = json.loads(preview[3]) if preview and preview[3] is not None else {}
        if (not preview or hashlib.sha256(preview[0].encode()).hexdigest() != preview[1]
                or preview[2] != config.get("sender_id") or not isinstance(pending, dict) or "provider_request" in pending):
            raise ValueError("Message claim changed or request already consumed")
        action = json.loads(preview[0])
        check, provider_id = {"operation": "lookup", "started_at": outreach_provider._now()}, None
        if action["conversation_id"] is None:
            try:
                provider_id = self.port.lookup(action["profile"]).provider_id
                check["observed_at"] = outreach_provider._now()
            except Exception as error:
                check.update(error_type=type(error).__name__, observed_at=outreach_provider._now())
        import inactivity_closure
        from orchestrator import name_key
        self.database.execute("BEGIN IMMEDIATE")
        try:
            row = self.database.execute("SELECT payload,payload_hash,identity,state,result FROM outreach_actions WHERE action_id=?",
                                        (action_id,)).fetchone()
            claim = self.database.execute("SELECT payload,payload_hash,identity,sender_id,preflight FROM outreach_action_claims WHERE action_id=?",
                                          (action_id,)).fetchone()
            if not row or not claim or row[3] != "REQUEST_STARTED" or tuple(row[:3]) != tuple(claim[:3]) \
                    or config.get("sender_id") != claim[3] or self.config.get("sender_id") != claim[3]:
                raise ValueError("Committed REQUEST_STARTED and durable claim required")
            allowed = config.get("outreach_allowed_purposes")
            if (config.get("outreach_actions_enabled") is not True or config.get("outreach_mode") != "guarded"
                    or not isinstance(allowed, list) or action.get("purpose") not in allowed or action.get("action_id") != action_id
                    or action.get("purpose") not in {"INITIAL", "FIRST_MESSAGE"} or action.get("provider") != sequence.SOURCE
                    or action.get("purpose") == "INITIAL" and not action.get("conversation_id")
                    or action.get("channel") != "linkedin" or profile_key(action.get("profile")) != claim[2]
                    or action.get("sender_id", claim[3]) != claim[3]
                    or not isinstance(action.get("message"), str) or not action["message"].strip()):
                raise ValueError("Claimed send is outside explicit enabled scope")
            result = json.loads(row[4]) if row[4] is not None else {}
            if not isinstance(result, dict) or "provider_request" in result:
                raise ValueError("Request already consumed or unrecognized durable result")
            checked_at = outreach_provider._now()
            now = engagement.timestamp(checked_at)
            proof = json.loads(claim[4])
            if action["conversation_id"] is None:
                result["provider_lookup_check"] = check
                if "error_type" in check:
                    self.database.execute("UPDATE outreach_actions SET state='UNCERTAIN_NO_RETRY',result=? WHERE action_id=? AND state='REQUEST_STARTED'",
                                          (json.dumps(result, sort_keys=True), action_id))
                    self.database.commit()
                    raise ValueError("Final profile read unavailable; reconcile without retry")
            # The checks Provider.send_claimed makes before an INITIAL or FIRST_MESSAGE (an INITIAL's history
            # was checked before its draft: outreach_history.PRE_DRAFT_CHECKED).
            gates = list(inactivity_closure.send_gates(self.database, config, action["profile"], now))
            if (action["purpose"] not in outreach_history.PRE_DRAFT_CHECKED
                    and outreach_history.blocking_positive(self.database, action["profile"], action["purpose"])):
                gates.append("KNOWN_PRIOR_OUTREACH_OR_STOP")
            if not isinstance(proof, dict) or not outreach._fresh(proof.get("checked_at"), now, config.get("outreach_history_max_age_seconds", 3600)):
                gates.append("DIRECT_HISTORY_MISSING_OR_STALE")
            if not outreach._fresh(action.get("verification", {}).get("verified_at"), now, config.get("outreach_content_max_age_seconds", 604800)):
                gates.append("CONTENT_OR_IDENTITY_VERIFICATION_STALE")
            stopped = engagement.stop_state(self.database, action["profile"])
            if stopped:
                gates.append(stopped)
            own_suppressions = {claim[2], "name:" + name_key(action["recipient"])}
            identities = sorted(set(engagement.identity_keys(self.database, action["profile"])) | own_suppressions)
            if self.database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='suppressions'").fetchone():
                placeholders = ",".join("?" for unused in identities)
                for suppression in self.database.execute("SELECT identity,reason FROM suppressions WHERE identity IN (" + placeholders + ")", identities):
                    if (suppression[0] not in own_suppressions
                            or suppression[1] != "Handoff historical outreach; never repeat initial contact"):
                        gates.append("SUPPRESSED_CONTACT")
            if action["conversation_id"] is None and not provider_id:
                gates.append("PROVIDER_ID_UNKNOWN")
            if gates:
                result["provider_guard"] = {"checked_at": checked_at, "gates": sorted(set(gates))}
                self.database.execute("UPDATE outreach_actions SET state='CANCELLED_BEFORE_SEND',result=? WHERE action_id=? AND state='REQUEST_STARTED'",
                                      (json.dumps(result, sort_keys=True), action_id))
                self.database.commit()
                raise ValueError("Final provider stop or freshness check failed")
            operation, arguments = (("send_message", {"chat_id": action["conversation_id"], "text": action["message"]})
                                    if action["conversation_id"] else
                                    ("start_chat", {"provider_id": provider_id, "text": action["message"]}))
            request = {"operation": operation, "route": sequence.SOURCE, "arguments": arguments, "started_at": checked_at,
                       "payload_hash": claim[1], "status": "REQUEST_STARTED"}
            result["provider_request"] = request
            self.database.execute("UPDATE outreach_actions SET result=? WHERE action_id=? AND state='REQUEST_STARTED'",
                                  (json.dumps(result, sort_keys=True), action_id))
            self.database.commit()
        except BaseException:
            self.database.rollback()
            raise
        if operation == "send_message":
            ack = self.port.send_message(arguments["chat_id"], arguments["text"])
        else:
            ack = self.port.start_chat(arguments["provider_id"], arguments["text"])
        return {**request, "finished_at": outreach_provider._now(), "status": "ACKNOWLEDGED_UNCONFIRMED",
                "raw": sequence._jsonable(ack.raw)}

    def execute_claimed(self, database, action_id, config, now=None):
        if database is not self.database or self.active_action is not None:
            raise ValueError("OUTREACH_EXECUTOR_OWNERSHIP_MISMATCH")
        self.active_action = action_id
        try:
            return super().execute_claimed(database, action_id, config, now)
        finally:
            self.active_action = None


COMPOSE_VERSION = "outreach-compose-v1"
COMPOSE_WORKER = "outreach-draft-composition"


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def company_prior_notes(database, company, identity):
    """Notes already sent or composed for other people at this company (for variety)."""
    key = research.canonical(database, "company", company)
    notes = []
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "history" in tables:
        for recipient, row_company, evidence in database.execute("SELECT recipient,company,evidence FROM history"):
            try:
                message = json.loads(evidence).get("message")
            except (ValueError, TypeError, AttributeError):
                message = None
            if research.canonical(database, "company", row_company) == key and isinstance(message, str):
                notes.append({"recipient": recipient, "text": message, "proof_point": None, "state": "SENT_HISTORY"})
    for row in database.execute("SELECT identity,payload FROM rw_drafts AS draft WHERE version=(SELECT MAX(version) FROM rw_drafts WHERE identity=draft.identity)"):
        payload = json.loads(row[1])
        if (research.canonical(database, "contact", row[0]) != identity
                and research.canonical(database, "company", payload.get("company", "")) == key
                and ({"composition", "owner_approval"} & set(payload))):
            notes.append({"recipient": payload["recipient"], "text": payload.get("invitation"),
                          "proof_point": (payload.get("composition") or {}).get("proof_point"), "state": "DRAFTED"})
    return notes


def composition_packet(database, draft, identity, root=None, repair_feedback=None):
    import job_description

    opportunity = next(outreach_promotion._bound_opportunity(database, draft, draft["team_need_evidence"],
                                                             draft["candidate_claim"]), None)
    _require(opportunity is not None, "DRAFT_SOURCE_BINDING_UNVERIFIED")
    resume = outreach_promotion.resume_context()
    posting = job_description.job_description(None, paths.data(root), opportunity=opportunity)
    recorded = posting.get("text") if posting["state"] == "BROWSER_READ" else None
    sources = outreach_promotion.composition_sources(database, draft, opportunity, resume, posting_text=recorded)
    evidence = research.current_evidence(draft["person_evidence"])
    packet = {
        "task": "COMPOSE_FIRST_LINKEDIN_CONTACT",
        "recipient": {"name": draft["recipient"], "first_name": draft["recipient"].split()[0],
                      "profile": draft["profile"], "company": draft["company"],
                      "contact_type": evidence.get("contact_type", {}).get("value"),
                      # Research's people are drafted without verified evidence (owner, 2026-10-01),
                      # so each record says whether it was verified; the prompt keeps unverified
                      # ones from being stated as fact. Recorded, never a gate.
                      "evidence": {field: dict({key: record.get(key) for key in ("value", "excerpt", "source", "observed_at")},
                                               verified=record.get("verified") is True)
                                   for field, record in evidence.items() if isinstance(record, dict)}},
        "role": {"title": opportunity.get("role"), "company": opportunity.get("company"), "source_url": opportunity.get("url"),
                 "facts": {field: opportunity["review"]["facts"].get(field) for field in
                           ("team_need", "level", "work_mode", "city", "country", "specialization", "industry", "funding_stage")},
                 "posting": {key: posting.get(key) for key in ("state", "source", "url", "observed_at", "sha256", "text")}},
        "candidate": {"resume_sha256": resume["source_sha256"], "resume_text_sha256": resume["text_sha256"],
                      "resume_lines": [f"L{number}: {line.rstrip()}" for number, line in enumerate(sources["resume"]["lines"], 1) if line.strip()],
                      "verified_claims": [{"id": claim_id, "text": texts[0], "short_text": texts[1]}
                                          for claim_id, texts in sources["claim"].items()],
                      "verified_location": outreach_promotion.verified_location()},
        "company_prior_notes": company_prior_notes(database, draft["company"], identity),
        "limits": {"invitation_max_chars": outreach_promotion.INVITATION_LIMIT,
                   "first_message_max_chars": outreach_promotion.FIRST_MESSAGE_LIMIT},
    }
    if repair_feedback:
        # The judge's exact objection to the previous note, for a repair turn. Same shape and
        # same purpose as answering_agent.build_packet's `repair_feedback` (owner, 2026-09-23:
        # "do the same repair turns just like answering agent"). No citation points into it.
        packet["repair_feedback"] = repair_feedback
    return packet, opportunity, resume, sources, posting


def _output_with_repair_turns(execute, config, template, schema, timeout, packet, raw, validate,
                             record=None, reserve=None):
    """Re-prompt the model with its own rejected output and the exact structural reason.

    Owner, 2026-09-23: "do the same repair turns just like answering agent", for a malformed
    output as well as a rejected note. This is answering_agent._repair_turn's contract applied to
    the two validators here (_validate for the judge, _validate_composition for the drafter): the
    SAME model is handed what it produced plus the precise reason Python could not accept it, and
    the new output faces the identical checks. Nothing is relaxed - a turn that still fails is
    still a failure - the model simply gets told what was wrong instead of the attempt dying.

    Termination is progress, as in repair_round: a turn that returns output already rejected ends
    the round and the last error is raised so the caller records it. The packet grows by
    `repair_feedback`, which changes digest(packet), so the prompt is rebuilt each turn and the
    model must echo the new source_sha256 - the binding stays exact.
    """
    feedback, seen = [], set()
    while True:
        try:
            return validate(raw, packet), packet
        except (ValueError, KeyError, TypeError) as error:
            text = raw if isinstance(raw, str) else str(raw)
            fingerprint = reply_worker.sha(text)
            if fingerprint in seen:
                raise
            seen.add(fingerprint)
            feedback = feedback + [{"previous_output": text[:4000],
                                    "rejection_reason": str(error) or type(error).__name__}]
            if record is not None:
                record(len(feedback), feedback[-1]["rejection_reason"])
            candidate = dict(packet, repair_feedback=feedback)
            prompt = template + "\n\nUNTRUSTED_SOURCE_JSON:\n" + encoded({"source_sha256": digest(candidate), **candidate})
            if len(prompt.encode()) > prompt_limit(config):
                raise
            if reserve is not None and not reserve(len(feedback)):
                raise
            packet = candidate
            raw = execute(config, prompt, schema, timeout)


def _validate_composition(raw, packet):
    output = json.loads(raw, object_pairs_hook=reply_worker._unique_object)
    fields = {"output_version", "source_sha256", "decision", "reason", "invitation", "first_message",
              "proof_point", "question", "facts"}
    if (not isinstance(output, dict) or set(output) != fields or output["output_version"] != COMPOSE_VERSION
            or output["source_sha256"] != digest(packet)):
        raise ValueError("OUTREACH_COMPOSITION_BINDING_INVALID")
    if output["decision"] not in {"COMPOSED", "INSUFFICIENT_FACTS"}:
        raise ValueError("OUTREACH_COMPOSITION_DECISION_INVALID")
    return output


def composition_observations(database, output, packet, sources):
    """What Python noticed about a composed note. RECORDED, never blocking.

    Owner, 2026-09-23: "remove every single piece of code that can cause the outreach to fail
    after AI generation ... remove every single piece of python code that compares things and
    forces the OUTREACH TO FAIL." These comparisons blocked every note for days while every
    cited fact was true: the composer cited `role.title` and `verified_evidence.team.excerpt`
    exactly as the packet showed them, and the checker looked them up in a differently named
    flat list, so a faithful quotation of recorded evidence was refused as unverifiable.

    The composer's cited facts stay on the decision as given; they are not compared with their
    sources (owner, 2026-10-21, E4.T2: `trace_gates` removed, the AI send judge is the only check).
    """
    if output["decision"] != "COMPOSED":
        return []
    observations = []
    used = {note["proof_point"] for note in packet["company_prior_notes"] if note.get("proof_point")}
    if isinstance(output.get("proof_point"), str) and output["proof_point"] in used:
        observations.append("PROOF_POINT_REUSED_AT_COMPANY")
    if outreach_promotion.dash_gates({field: output.get(field) for field in ("invitation", "first_message", "question")}):
        observations.append("DASHES_NORMALIZED")
    return observations


def composition_gates(database, output, packet, sources):
    """The ONLY refusal left is the model's own decision not to compose.

    Owner, 2026-09-23: "remove every single piece of python code that compares things and forces
    the OUTREACH TO FAIL ... Remove it all. I dont care." Every Python judgment of the generated
    note is gone: fact traceability, proof-point reuse, placeholder text, dashes and size. The
    text is normalized and trimmed to the provider's limits instead of being refused, and what
    Python noticed is recorded by composition_observations for audit. `AI_INSUFFICIENT_FACTS` is
    not a comparison - it is the composer reporting it had nothing truthful to say.

    Duplicate-send and identity protection lives in outreach.action_gates and is deliberately
    untouched: the owner's standing rule is never to contact the same person twice.
    """
    return [] if output["decision"] == "COMPOSED" else ["AI_INSUFFICIENT_FACTS"]


def _judge_text_rejection(proof):
    """The judge's objection when it rejected the NOTE, not the history or the identity.

    Owner, 2026-09-23: "why didnt the judge push back on the draft AI, and try to repair the
    draft. Using repair turns etc...? Instead of just blocking and Failing." This returns the
    feedback for one repair turn, in the same shape answering_agent uses, or None when the
    rejection was not about the text and recomposing could not possibly help.
    """
    if not isinstance(proof, dict) or proof.get("agent_decision") not in {"BLOCKED", "UNKNOWN"}:
        return None
    # No history revalidation here (owner, 2026-09-23: "Theres no need for us to revalidate.
    # we've done this earlier in the pipeline."). Rewriting text cannot widen who may be
    # contacted: outreach.action_gates still refuses PREVIOUSLY_CONTACTED_NO_INITIAL, OPTED_OUT,
    # SUPPRESSED_CONTACT and the engagement stop state on the repaired note exactly as on the
    # rejected one, and every one of those checks runs after this.
    reason = proof.get("agent_reason") or proof.get("reason")
    correction = proof.get("next_action")
    if not isinstance(reason, str) and not isinstance(correction, str):
        return None
    return {"rejection_reason": reason if isinstance(reason, str) else "UNSPECIFIED",
            "required_correction": correction if isinstance(correction, str) else None,
            "agent_decision": proof["agent_decision"]}


def record_judge_objection(database, action, proof, now):
    """Store the dispatch judge's objection to a composed message, for one repair turn.

    Owner, 2026-09-23: "do replies and followups too". The outreach composer repairs in-run
    (_draft_repair_turn), but a FOLLOWUP or REPLY message is written by a different producer in
    an earlier stage, so its objection has to be left where that producer will find it. This is
    that record: immutable, bound to the exact recipient and the exact rejected text. No history
    is revalidated here - action_gates still applies every do-not-contact rule to whatever the
    producer writes next.
    """
    objection = _judge_text_rejection(proof)
    if not objection or not isinstance(action, dict):
        return None
    return audit(database, action.get("action_id", "unknown"), "INSPECTION", "JUDGE_OBJECTION",
                 now.isoformat() if hasattr(now, "isoformat") else str(now),
                 {"identity": profile_key(action["profile"]), "purpose": action.get("purpose"),
                  **objection, "previous_message": action.get("message"),
                  "previous_invitation": action.get("invitation")})


def pending_objection(database, identity, purpose=None):
    """The newest unanswered judge objection for this recipient, as repair_feedback.

    Returns a one-item list shaped exactly like answering_agent's repair_feedback, or None.
    """
    if not database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='outreach_agent_audit'").fetchone():
        return None
    try:
        key = profile_key(identity)
    except (ValueError, TypeError):
        return None
    for row in database.execute("SELECT record FROM outreach_agent_audit WHERE kind='JUDGE_OBJECTION' "
                                "ORDER BY rowid DESC LIMIT 20"):
        record = json.loads(row[0])
        if record.get("identity") != key:
            continue
        if purpose and record.get("purpose") and record["purpose"] != purpose:
            continue
        return [record]
    return None


def _draft_repair_turn(root, database, config, row, draft, proof, execute, now):
    """One repair turn on a rejected note: recompose with the judge's objection, never raise.

    Mirrors answering_agent._repair_turn - the same model is handed its previous output and the
    exact reason it was rejected, and the result is re-validated by the same checks. Returns the
    new rw_drafts row when the text actually changed, otherwise None, which ends the round the
    way a turn that fixes nothing ends answering_agent's.
    """
    objection = _judge_text_rejection(proof)
    if not objection:
        return None
    feedback = [dict(objection, previous_invitation=draft.get("invitation"),
                     previous_first_message=draft.get("first_message"))]
    try:
        outcome = compose_draft(root, database, config, row, execute, now, repair=feedback)
    except Exception as error:
        audit(database, "outreach-compose:" + outreach_promotion._source(row)["draft_id"], "COMPOSITION",
              "REPAIR_FAILED", now.isoformat(),
              {"error_type": type(error).__name__, "objection": objection})
        return None
    if outcome.get("state") != "COMPOSED":
        return None
    repaired = database.execute("SELECT * FROM rw_drafts WHERE identity=? ORDER BY version DESC LIMIT 1",
                               (row["identity"],)).fetchone()
    if not repaired or repaired["content_hash"] == row["content_hash"]:
        return None      # the turn changed nothing; the round ends here
    return repaired


def compose_draft(root, database, config, row, execute=None, now=None, *, repair=None):
    """AI composes one first contact; Python enforces hard lines; nothing is sent.

    A passing composition becomes a new HELD rw_drafts version superseding the
    research scaffold. It still needs promotion, local contact records (owner, 2026-09-29) and
    the guarded dispatcher. Returns {"state": "COMPOSED"|"REJECTED", ...}.
    """
    now = engagement.timestamp(now or dt.datetime.now(dt.timezone.utc))
    execute = execute or reply_worker._execute
    initialize(database)
    database.commit()
    draft, identity, unused = outreach_promotion._validate(database, row, now)
    # A repair turn composes AGAIN for a note the judge rejected (owner, 2026-09-23), so the
    # latest version legitimately already carries a composition. The text is rebuilt from the
    # SCAFFOLD, never from the rejected wording, and the new version supersedes the same
    # scaffold the rejected one did - so the source binding, reservation and claim are unchanged.
    if repair:
        scaffold = outreach_promotion._scaffold(database, row, draft)
    else:
        _require("composition" not in draft and "owner_approval" not in draft, "ALREADY_COMPOSED")
        scaffold = draft
    outreach_promotion._local_gates(database, config, scaffold, identity)
    packet, opportunity, resume, sources, posting = composition_packet(database, scaffold, identity, root,
                                                                      repair_feedback=repair)
    template = resources.read_text("outreach-compose-prompt.md")
    schema = resources.read_text("outreach-compose-schema.json")
    source_hash = digest(packet)
    prompt = template + "\n\nUNTRUSTED_SOURCE_JSON:\n" + encoded({"source_sha256": source_hash, **packet})
    timeout = config.get("outreach_agent", {}).get("timeout_seconds", 60)
    _require(type(timeout) is int and 1 <= timeout <= 600 and len(prompt.encode()) <= prompt_limit(config), "OUTREACH_AGENT_INPUT_OR_TIME_BOUND")
    key = "outreach-compose:" + scaffold["draft_id"]
    provenance = {"model": text_model.model(config), "prompt_sha256": reply_worker.sha(template), "schema_sha256": reply_worker.sha(schema)}
    reference = audit(database, key, "COMPOSITION", "OBSERVATION", now.isoformat(),
                      {"packet": packet, "source_sha256": source_hash, "provenance": provenance})
    # The booking carries the budget day: the observation (and so `reference`) is identical when
    # the same scaffold is retried, and a retry on a later day reused the earlier day's key, which
    # reserve_compute refuses ("Compute reservation identity cannot change"), spending a try with
    # no model call (2026-10-02). Same-day retries still share one booking, as before.
    day = workflow_tasks.budget_day(now, config.get("timezone", "America/Los_Angeles"))
    booking = "outreach-compose:" + reference + ":" + day
    with database:
        if not workflow_tasks.reserve_compute(database, booking, now.isoformat(), timeout,
                config.get("timezone", "America/Los_Angeles"), config.get("daily_compute_seconds", 10800)):
            raise SharedBudgetExhausted(config, now)
    try:
        raw = execute(config, prompt, schema, timeout)
        def _record(turn, reason):
            audit(database, key, "COMPOSITION", "REPAIR", now.isoformat(),
                  {"observation_id": reference, "turn": turn, "rejection_reason": reason})

        repairs = []

        def _reserve(turn):
            repair_booking = "outreach-compose-repair:" + reference + ":" + day + ":" + str(turn)
            repairs.append((repair_booking, outreach_provider._now()))
            with database:
                return bool(workflow_tasks.reserve_compute(
                    database, repair_booking, now.isoformat(),
                    timeout, config.get("timezone", "America/Los_Angeles"),
                    config.get("daily_compute_seconds", 10800)))

        output, packet = _output_with_repair_turns(
            execute, config, template, schema, timeout, packet, raw, _validate_composition,
            record=_record, reserve=_reserve)
        with database:
            workflow_tasks.settle_compute(database, booking, now.isoformat(),
                                          outreach_provider._now(),
                                          "outreach composition completed; shared daily pool: unused tail of a cleanly completed reservation")
            for repair_booking, started in repairs:
                workflow_tasks.settle_compute(database, repair_booking, started, outreach_provider._now(),
                                              "outreach composition repair turn completed; shared daily pool: unused tail of a cleanly completed reservation")
    except Exception as error:
        # The reason is recorded, not just the class: "Held" alone hid whether the model call
        # timed out, failed at the provider or returned nothing (2026-09-28).
        audit(database, key, "COMPOSITION", "FAILURE", now.isoformat(),
              {"observation_id": reference, "error_type": type(error).__name__,
               "code": getattr(error, "code", None), "error": str(error)[:300],
               "cause": repr(error.__cause__)[:300] if error.__cause__ else None,
               **({"executor": reply_worker.executor_detail(error)} if reply_worker.executor_detail(error) else {})})
        raise
    observations = composition_observations(database, output, packet, sources)
    # Correct the text rather than refuse it (owner, 2026-09-23): strip the dashes the owner's
    # style rule bars, and trim to the provider's own limits at a word boundary.
    limits = {"invitation": outreach_promotion.INVITATION_LIMIT,
              "first_message": outreach_promotion.FIRST_MESSAGE_LIMIT}
    for field in ("invitation", "first_message", "question"):
        if isinstance(output.get(field), str):
            output[field] = outreach_promotion.without_dashes(output[field])
            if field in limits:
                output[field] = outreach_promotion.fit_text(output[field], limits[field])
    gates = composition_gates(database, output, packet, sources)
    decision = audit(database, key, "COMPOSITION", "DECISION", now.isoformat(),
                     {"observation_id": reference, "output": raw, "output_sha256": reply_worker.sha(raw),
                      "gates": gates, "trace_observations": observations})
    if gates:
        return {"state": "REJECTED", "gates": gates, "evidence": "outreach_agent_audit:" + decision}
    composition = {"version": COMPOSE_VERSION, "facts": output["facts"], "proof_point": output["proof_point"],
                   "question": output["question"], "reason": output["reason"], "evidence": "outreach_agent_audit:" + decision,
                   "resume_text_sha256": resume["text_sha256"], **provenance,
                   "posting": ({key: posting[key] for key in ("state", "source", "url", "observed_at", "sha256")}
                               if posting["state"] == "BROWSER_READ" else None)}
    with research.transaction(database):
        latest = database.execute("SELECT * FROM rw_drafts WHERE identity=? ORDER BY version DESC LIMIT 1", (row["identity"],)).fetchone()
        _require(latest is not None and tuple(latest) == tuple(row), "DRAFT_CHANGED_DURING_COMPOSITION")
        payload = outreach_promotion.superseding_payload(scaffold, invitation=output["invitation"],
                                                         first_message=output["first_message"], composition=composition)
        created = outreach_promotion.insert_version(database, row["identity"], payload, now)
    return {"state": "COMPOSED", "draft_id": created["draft_id"], "version": created["version"],
            "evidence": composition["evidence"]}


def _compose_step(root, database, config, row, execute, now):
    """Bounded composition task per research scaffold version: 7 tries, no deadline (owner, 2026-10-02)."""
    source = outreach_promotion._source(row)
    draft = json.loads(row["payload"])
    with database:
        task_id = workflow_tasks.enqueue(database, COMPOSE_WORKER, digest([COMPOSE_VERSION, source]),
            "outreach-compose:" + source["draft_id"], [{"source": "rw_drafts:" + row["identity"],
            "observed_at": draft["created_at"], "content_sha256": row["content_hash"], "version": row["version"]}],
            (now + dt.timedelta(hours=24)).isoformat() if not database.execute(
                "SELECT 1 FROM workflow_tasks WHERE worker=? AND action_id=?", (COMPOSE_WORKER, digest([COMPOSE_VERSION, source]))).fetchone()
            else database.execute("SELECT deadline FROM workflow_tasks WHERE worker=? AND action_id=?",
                                  (COMPOSE_WORKER, digest([COMPOSE_VERSION, source]))).fetchone()[0],
            now.isoformat(), company_id=draft["company"], contact_id=draft["profile"], effect="READ", max_attempts=7)
        # No deadline for note writing, only its tries (owner, 2026-10-02); one its old deadline
        # ended with tries left is reopened here, keeping its attempts and failures.
        workflow_tasks.reopen_without_deadline(database, task_id, now.isoformat())
    # Lease outlasts the composition call (now up to 600 s, owner 2026-09-28) plus repair turns.
    lease = config.get("outreach_agent", {}).get("timeout_seconds", 60) * 2 + 120
    claim = workflow_tasks.claim(database, task_id, now.isoformat(), lease_seconds=lease)
    if claim["status"] != "CLAIMED":
        return dict(_task_summary(database, task_id), attempted=False)
    blocker, result, capacity, provider = None, {}, None, None
    try:
        result = compose_draft(root, database, config, row, execute, now)
        if result["state"] != "COMPOSED":
            blocker = "AI_COMPOSITION_REJECTED:" + ",".join(result["gates"])[:600]
    except SharedBudgetExhausted as error:
        blocker, capacity = error.code, error
    except Exception as error:
        provider = provider_limits.wait_of(error)
        blocker = str(error)[:600] if isinstance(error, ValueError) else "OUTREACH_COMPOSITION_" + type(error).__name__
    finished = now.isoformat()
    if provider:
        with database:
            workflow_tasks.provider_wait(database, task_id, claim["token"], finished, provider)
        return dict(_task_summary(database, task_id), attempted=True, composition=None)
    if capacity:
        # Shared compute ran out after the claim: a wait until the reset, not a try (2026-09-28).
        with database:
            workflow_tasks.compute_wait(database, task_id, claim["token"], finished, capacity.reset_at.isoformat(),
                                        capacity.code, result)
        return dict(_task_summary(database, task_id), attempted=True, composition=None)
    with database:
        workflow_tasks.finish(database, task_id, claim["token"], finished, result,
            [{"source": result.get("evidence", "workflow_tasks:" + task_id), "observed_at": finished}],
            status="COMPLETE" if not blocker else "RETRY_WAIT", blocker=blocker,
            next_action=None if not blocker else "Recompose from verified facts within the bounded attempts; do not send template text")
    return dict(_task_summary(database, task_id), attempted=True, composition=result.get("state"))


def reinspection_grant(database, draft_id):
    """Newest owner REINSPECTION_GRANT for this draft (outreach_owner_approval.py --reinspect), else None."""
    if not database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='outreach_owner_records'").fetchone():
        return None
    for record_id, payload in database.execute("SELECT record_id,payload FROM outreach_owner_records "
                                               "WHERE kind='REINSPECTION_GRANT' ORDER BY recorded_at DESC, record_id"):
        if json.loads(payload).get("draft_id") == draft_id:
            return record_id
    return None


def enqueue_inspection(database, row, draft, source, now):
    """The first guarded step for a draft: bounded local-records history inspection."""
    grant = reinspection_grant(database, source["draft_id"])
    task_action = digest([VERSION, source] + ([grant] if grant else []))
    existing = database.execute("SELECT task_id,deadline FROM workflow_tasks WHERE worker='outreach-draft-inspection' AND action_id=?",
                                (task_action,)).fetchone()
    task_id = workflow_tasks.enqueue(database, "outreach-draft-inspection", task_action,
        "outreach-draft:" + source["draft_id"], [{"source": "rw_drafts:" + row["identity"],
        "observed_at": draft["created_at"], "content_sha256": row["content_hash"], "version": row["version"]}],
        existing["deadline"] if existing else (now + dt.timedelta(hours=24)).isoformat(), now.isoformat(),
        company_id=draft["company"], contact_id=research.canonical(database, "contact", draft["profile"]),
        # 7 attempts on the growing backoff, not 3 then a grave (owner, 2026-09-23). Three
        # failures made the task EXHAUSTED with next_at NULL and nothing re-arms it: 6 of 7
        # outreach tasks are in that state and 4 of 5 people became permanently uncontactable.
        effect="READ", max_attempts=7)
    # No deadline for this check, only its 7 tries (owner, 2026-09-28); one its old deadline
    # ended with tries left is reopened here, keeping its attempts and failures.
    workflow_tasks.reopen_without_deadline(database, task_id, now.isoformat())
    return task_id


def prepare_drafts(root, database, config, client, execute=None, now=None, *, history_collector=None, port=None):
    """`port`: the LinkedInPort when config["linkedin_provider"] is "unipile" (E3.T3); every draft
    inspection then reads the person through it instead of HeyReach."""
    if config.get("outreach_agent", {}).get("enabled") is not True:
        return {"state": "DISABLED", "items": [], "contexts": {}}
    if database.in_transaction:
        raise ValueError("Commit before outreach source investigations")
    realtime = now is None
    now = engagement.timestamp(now or dt.datetime.now(dt.timezone.utc))
    # Ceiling 4 -> 7 (owner, 2026-09-23: "one outreach per cycle is terrible for throughput.
    # Lets do a batch of 7"). The per-cycle bound is a throughput knob, not a safety control -
    # what actually protects a recipient is the do-not-contact set, the suppression table, the
    # one-send-per-recipient claim and the prior-contact refusal, all unchanged.
    limit = config.get("outreach_agent", {}).get("max_items", 2)
    if type(limit) is not int or not 1 <= limit <= 7:
        raise ValueError("OUTREACH_INVESTIGATION_LIMIT_INVALID")
    compose = outreach_promotion.composition_required(config)
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "rw_drafts" not in tables:
        return {"state": "NO_DRAFTS", "items": [], "contexts": {}}
    adapter = Adapter(root, database, config, client, execute, history_collector=history_collector,
                      port=port, port_drafts=port is not None)
    output = {"state": "COMPLETE", "items": [], "contexts": {}, "campaign_candidates": []}
    rows = database.execute("SELECT * FROM rw_drafts WHERE version=(SELECT MAX(version) FROM rw_drafts latest WHERE latest.identity=rw_drafts.identity) ORDER BY identity").fetchall()
    attempts = 0
    # One repair turn per recipient per cycle. A repaired version is appended to `rows` so it is
    # judged in this same run; this set stops a rejected-again note from recomposing forever.
    repaired_identities = set()
    for row in rows:
        item = {"draft_id": outreach_promotion._source(row)["draft_id"]}
        output["items"].append(item)
        try:
            if compose and not ({"composition", "owner_approval"} & set(json.loads(row["payload"]))):
                # Template text is a research scaffold only: the AI writes the outreach.
                if attempts >= limit:
                    item["state"] = "DEFERRED_CYCLE_BOUND"
                    continue
                step = _compose_step(root, database, config, row, execute, now)
                attempts += int(step.pop("attempted"))
                item.update(step, stage="COMPOSITION")
                continue
            draft, identity, verification = outreach_promotion._validate(database, row, now, require_composition=compose)
            outreach_promotion._local_gates(database, config, draft, identity, now)
            queued = {found[0] for found in database.execute("SELECT action_id FROM outreach_actions WHERE identity=?",
                                                            (profile_key(identity),))}
            # Actions the owner rejected and that never went out do not count (owner, 2026-10-22).
            if queued - approvals.rejected_unsent_ids(database, {profile_key(identity)}):
                item["state"] = "ALREADY_QUEUED"
                continue
            source = outreach_promotion._source(row)
            action_id = outreach_promotion.action_id_for(database, identity, "INITIAL")
            planned = engagement.next_action(database, identity, now, draft["recipient"])
            purpose = "FIRST_MESSAGE" if planned.get("purpose") == "FIRST_MESSAGE" else "INITIAL"
            candidate = {"action_id": action_id, "profile": identity, "recipient": draft["recipient"],
                         "company": draft["company"], "purpose": purpose, "message": draft["first_message"] or draft["invitation"],
                         "invitation": draft["invitation"],
                         "channel": "linkedin", "verification": verification, "source": source,
                         "provider": "unipile" if port is not None else "heyreach"}
            with database:
                task_id = enqueue_inspection(database, row, draft, source, now)
            task = database.execute("SELECT * FROM workflow_tasks WHERE task_id=?", (task_id,)).fetchone()
            if task["status"] == "COMPLETE":
                result = json.loads(task["result"])
                if result.get("outcome") == "SUPPRESSED":
                    item.update(state="SUPPRESSED", task_id=task_id, blocker="KNOWN_PRIOR_OUTREACH_OR_STOP")
                    continue
                if outreach._fresh(result["proof"].get("checked_at"), now, config.get("outreach_history_max_age_seconds", 3600)):
                    if result.get("route") == "INVITATION":
                        output["campaign_candidates"].append(result)
                    else:
                        output["contexts"][identity] = result
                    item.update(state="OBSERVED", task_id=task_id)
                    continue
                with database:
                    changed = database.execute("UPDATE workflow_tasks SET status='RETRY_WAIT',blocker='HISTORY_EVIDENCE_EXPIRED',next_action=?,next_at=?,updated_at=? WHERE task_id=? AND status='COMPLETE' AND result=?",
                        ("Refresh expired history within the original 7-try investigation budget", now.isoformat(), now.isoformat(), task_id, task["result"]))
                    if changed.rowcount:
                        workflow_tasks.record(database, task_id, "EVIDENCE_EXPIRED", now.isoformat(), {"previous_result": result})
            # No browser history read any more (owner, 2026-09-29), so nothing is budgeted for one.
            if _defer_known_capacity(database, task_id, config, now, 0):
                item.update(_task_summary(database, task_id))
                continue
            if attempts >= limit:
                item["state"] = "DEFERRED_CYCLE_BOUND"
                item.update({key: value for key, value in _task_summary(database, task_id).items() if key != "state"})
                continue
            claim = workflow_tasks.claim(database, task_id, now.isoformat(), lease_seconds=600)
            if claim["status"] != "CLAIMED":
                item.update(_task_summary(database, task_id))
                continue
            attempts += 1
            proof = None
            blocker = None
            capacity_error = None
            provider = None
            try:
                proof = adapter.inspect(candidate, config)
                if realtime:
                    now = dt.datetime.now(dt.timezone.utc)
                latest = database.execute("SELECT * FROM rw_drafts WHERE identity=? ORDER BY version DESC LIMIT 1", (row["identity"],)).fetchone()
                if not latest or tuple(latest) != tuple(row):
                    raise ValueError("DRAFT_CHANGED_DURING_HISTORY_INVESTIGATION")
                check = dict(candidate, conversation_id=proof.get("conversation_id"))
                gates = [*proof.get("capability_gates", []), *outreach.action_gates(database, config, check, proof, now)]
                if proof.get("route") == "INVITATION" and purpose == "INITIAL":
                    gates = [gate for gate in gates if gate != "VERIFIED_CONVERSATION_MISSING"]
                    if not isinstance(draft.get("invitation"), str) or not 0 < len(draft["invitation"]) <= 300:
                        gates.append("INVITATION_TEXT_MISSING_OR_TOO_LONG")
                blocker = ";".join(gates) if gates else None
            except SharedBudgetExhausted as error:
                capacity_error = error
                blocker = error.code
            except Exception as error:
                provider = provider_limits.wait_of(error)
                blocker = "OUTREACH_INVESTIGATION_" + type(error).__name__
                if isinstance(error, ValueError) and str(error):
                    # Validation codes are diagnosable; other messages may carry provider URLs.
                    blocker += ":" + str(error)[:300]
            finished = dt.datetime.now(dt.timezone.utc).isoformat() if realtime else now.isoformat()
            if provider:
                # The provider refused the history read or the AI judgment for usage: a
                # capacity wait until its reset, not a failed inspection attempt.
                with database:
                    workflow_tasks.provider_wait(database, task_id, claim["token"], finished, provider,
                                                 {"purpose": purpose, "draft_source": source, "profile": identity,
                                                  "outcome": provider_limits.CODE})
                item.update(_task_summary(database, task_id), outcome="WAITING")
                continue
            if capacity_error:
                # Shared compute ran out after the claim: a wait until the reset, not a try (2026-09-28).
                with database:
                    workflow_tasks.compute_wait(database, task_id, claim["token"], finished,
                                                capacity_error.reset_at.isoformat(), capacity_error.code,
                                                {"purpose": purpose, "draft_source": source, "profile": identity,
                                                 "outcome": capacity_error.code},
                                                next_action="Retry authenticated history inspection after shared capacity resets at "
                                                + capacity_error.reset_at.isoformat() + "; do not send")
                item.update(_task_summary(database, task_id), outcome="HELD")
                continue
            result = {"purpose": purpose, "proof": proof or {}, "conversation_id": (proof or {}).get("conversation_id"),
                      "draft_source": source, "outcome": (proof or {}).get("history_outcome", "INVESTIGATED"),
                      "route": (proof or {}).get("route"), "profile": identity,
                      "draft": draft, "verification": verification}
            suppressed = result["outcome"] == "SUPPRESSED"
            evidence = [{"source": (proof or {}).get("evidence", "workflow_tasks:" + task_id), "observed_at": finished}]
            with database:
                workflow_tasks.finish(database, task_id, claim["token"], finished, result, evidence,
                    status="COMPLETE" if suppressed or not blocker else "RETRY_WAIT", blocker=blocker,
                    next_action=(proof or {}).get("next_action", "Retry bounded authenticated history inspection; do not send") if blocker else None)
            item.update(_task_summary(database, task_id))
            item["outcome"] = "SUPPRESSED" if suppressed else "CAPABILITY_GATED" if (proof or {}).get("capability_gates") else "READY" if not blocker else "HELD"
            if blocker and not suppressed and row["identity"] not in repaired_identities:
                # The judge rejected the note, so hand its objection back to the composer instead
                # of stopping here (owner, 2026-09-23). One turn per identity per cycle; the new
                # version is appended to this run's work list so it is judged in the SAME run,
                # which is what makes this a repair round rather than a next-cycle retry. A turn
                # that changes nothing returns None and the round ends, exactly as in
                # answering_agent.repair_round.
                repair = _draft_repair_turn(root, database, config, row, draft, proof, execute, now)
                if repair is not None:
                    repaired_identities.add(row["identity"])
                    rows.append(repair)
                    item["repair"] = {"state": "RECOMPOSED", "version": repair["version"],
                                      "rejection": (proof or {}).get("agent_decision")}
            if not blocker and not suppressed:
                if result["route"] == "INVITATION":
                    output["campaign_candidates"].append(result)
                else:
                    output["contexts"][identity] = result
        except (ValueError, KeyError, TypeError) as error:
            item.update(state="HELD", blocker=str(error))
    return output
