"""Manual synthetic validation of the scheduled guarded reply execution path.

Integration contract: reply_execution.promote(database, config, now=None),
reply_execution.Adapter(root, database, config, client, execute=None), and
orchestrator.reply_execution_cycle(root, database, config), called by service_cycle.
Only inference and external provider/notification boundaries are mocked. Immutable
reply inputs/results, action guards, claims, receipts, retries and scheduling run.
No production configuration, database, credentials, model subprocesses or network.
"""

import contextlib
import copy
import datetime as dt
import hashlib
import importlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import traceback
from unittest.mock import patch

import applications
import conversion
import engagement
import notifications
import orchestrator
import outreach
import outreach_agent
import outreach_provider
import pipeline_routing as routing
import reply_worker
import research_workflow
import workflow_tasks


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "tests"))
from test_pipeline_routing import message as inbound_message


BASE = dt.datetime(2026, 9, 17, 16, 0, 10, tzinfo=dt.timezone.utc)
PROFILE = "https://www.linkedin.com/in/mock-contact"
OTHER_PROFILE = "https://www.linkedin.com/in/other-synthetic-contact"
# A real correspondent is a person with a name. The fixture previously modelled
# a nameless one, which is not a shape the provider or the recovered tracker can
# produce, and which therefore hid every name-keyed suppression check from these
# scenarios. Each synthetic profile now carries the display name a real
# correspondent has, bound durably in `history` exactly as reconciliation.py:57
# binds a recovered-tracker contact, and observed on the provider room exactly as
# HeyReach reports `correspondentProfile`.
NAMES = {PROFILE: "Mock Contact", OTHER_PROFILE: "Other Synthetic Contact"}
# The opposite, equally real shape, observed live as `linkedin.com/in/morgan-hale`:
# an inbound-only correspondent who is in no durable record and whose provider room
# carries no name field. HeyReach is not contractually obliged to supply one, so this
# stays modelled deliberately — it is the case the identity gate exists for, not a
# fixture that forgot to fill a field.
UNNAMED_PROFILE = "https://www.linkedin.com/in/unbound-correspondent"
TEXT = "Which aspect would you like to discuss?"
REPORT = ROOT / "reports/reply-execution-manual.json"
INTERFACE = {
    "promotion": "reply_execution.promote(database, config, now=None)",
    "adapter": "reply_execution.Adapter(root, database, config, client, execute=None)",
    "scheduler": "orchestrator.service_cycle -> reply_execution_cycle -> outreach.cycle",
    "source": "Actual pipeline_routing and reply_worker immutable inputs/results; no fabricated result rows",
    "sender": "Actual shared action guard and Provider.send_claimed; mock client._request only",
    "receipt": "Separate authenticated mocked get_chatroom read with exact outbound message and request timing",
}


def _correspondent(profile, lead_id):
    """The provider's correspondent record, with the display name it carries.

    HeyReach reports the correspondent's name on every chatroom; modelling a
    correspondent without one made these scenarios unable to exercise any
    name-keyed exclusion, suppression or no-follow-up rule.
    """
    name = NAMES.get(profile)
    if not name:
        # No name is invented for a profile the fixture did not model as a named
        # person; an unmodelled correspondent stays unnamed, like UNNAMED_PROFILE.
        return {"profileUrl": profile, "linkedin_id": lead_id}
    first, unused, last = name.partition(" ")
    return {"profileUrl": profile, "linkedin_id": lead_id, "fullName": name,
            "firstName": first, "lastName": last}


def require(condition, message):
    if not condition:
        raise AssertionError(message)


@contextlib.contextmanager
def isolated_boundaries():
    if hasattr(reply_worker, "reply_resume"):
        reply_worker.reply_resume.context()
    with contextlib.ExitStack() as stack:
        for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.getaddrinfo",
                       "subprocess.Popen", "urllib.request.urlopen"):
            stack.enter_context(patch(target, side_effect=AssertionError("External execution forbidden")))
        # The synthetic inbound messages are dated before the 2026-09-30 backlog cutoff, which
        # exists only for the real backlog on file (owner: "dont send these replies").
        import reply_execution
        stack.enter_context(patch.object(reply_execution, "BACKLOG_CUTOFF", dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)))
        yield


class Fixture:
    def __init__(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="reply-manual-", dir=ROOT)
        self.root = Path(self.temporary.name)
        (self.root / "state").mkdir()
        (self.root / "reports").mkdir()
        for filename in ("reply-prompt.md", "reply-schema.json", "outreach-agent-prompt.md",
                         "outreach-agent-schema.json"):
            shutil.copyfile(ROOT / filename, self.root / filename)
        self.database = orchestrator.connect(self.root)
        for module in (applications, conversion, engagement, research_workflow, routing,
                       workflow_tasks, reply_worker):
            module.initialize(self.database)
        outreach.initialize_actions(self.database)
        # Every correspondent this fixture models is one the harness is managing - that is the
        # whole premise of a reply scenario. Recorded explicitly since 2026-09-23, when the
        # owner scoped replies to conversations the harness started or he started by hand;
        # without a row here the send path correctly refuses with REPLY_CONVERSATION_NOT_MANAGED
        # and no scenario can build an action at all.
        outreach.initialize_managed(self.database)
        for profile in (PROFILE, OTHER_PROFILE, UNNAMED_PROFILE):
            outreach.record_managed(self.database, profile, "Synthetic managed conversation",
                                    BASE.isoformat())
        for profile, name in NAMES.items():
            self.database.execute("INSERT OR REPLACE INTO history VALUES (?,?,?,?,?)",
                                  ("name:" + orchestrator.name_key(name), name, "Mock Company", profile,
                                   json.dumps({"source": "synthetic recovered tracker contact"})))
        self.database.commit()
        self.now = BASE
        self.config = {
            "codex": "/synthetic/never-execute", "model": "synthetic-no-model-call", "timezone": "UTC",
            "sender_id": 123, "sender_profile": "https://www.linkedin.com/in/synthetic-sender",
            "pipeline_routing": {"account_ids": [123]},
            "reply_worker": {"enabled": True, "timeout_seconds": 1, "max_items": 20},
            "reply_execution": {"enabled": True},
            "outreach_agent": {"enabled": True, "timeout_seconds": 1},
            "outreach_actions_enabled": True, "outreach_mode": "guarded",
            "outreach_allowed_purposes": ["INITIAL", "FIRST_MESSAGE", "REPLY"],
            "outreach_history_max_age_seconds": 3600,
        }
        self.account = {"id": 123, "profileUrl": self.config["sender_profile"],
                        "authIsValid": True, "isActive": True}
        self.rooms = {}
        self.unknowns = {}
        self.requests = []
        self.reads = []
        self.inferences = []
        self.cycles = []
        self.receipts_visible = True
        self.lost_ack = False
        self.provider_denied = False
        self.pending_receipts = []
        self.read_failures = 0
        self.read_always_fails = False
        self.after_inspection = None
        self.receipt_mode = "valid"

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.database.close()
        self.temporary.cleanup()

    def restart(self):
        self.database.close()
        self.database = orchestrator.connect(self.root)

    def seed(self, profile=PROFILE, source_id="message-1", thread="thread-1", unknowns=None,
             body="Hello!\nCould you tell me more?"):
        event = inbound_message(body, source_id)
        event["profile"] = profile
        for record in (event["raw"], event["inbox"]):
            record.update(sender_profile=profile, conversation_id=thread)
        event["inbox"]["authentication"]["source"] = "https://www.linkedin.com/messaging/thread/" + thread
        engagement.record_event(self.database, event)
        routing.cycle(self.database, self.config, self.now.isoformat())
        self.database.commit()
        self.unknowns[source_id] = unknowns or []
        self.rooms[thread] = {
            "id": thread, "linkedInAccountId": 123, "linkedInAccount": copy.deepcopy(self.account),
            "blockedByMe": False, "blockedByParticipant": False,
            "groupChat": False, "correspondentProfile": _correspondent(profile, source_id),
            "messages": [{"id": source_id, "sender": "CORRESPONDENT", "body": event["raw"]["text"],
                          "createdAt": event["occurred_at"]}], "totalMessages": 1,
        }
        return event

    def call(self, operation, arguments):
        self.reads.append({"operation": operation, "arguments": copy.deepcopy(arguments)})
        if self.read_always_fails or self.read_failures:
            self.read_failures = max(0, self.read_failures - 1)
            raise TimeoutError("Synthetic transient preflight read")
        if operation == "get_linked_in_account_by_id":
            return copy.deepcopy(self.account)
        if operation == "get_conversations_v2":
            rooms = [room for room in self.rooms.values()
                     if room["correspondentProfile"]["profileUrl"] == arguments["leadProfileUrl"]]
            return {"items": copy.deepcopy(rooms), "totalCount": len(rooms)}
        if operation == "get_chatroom":
            return copy.deepcopy(self.rooms[arguments["conversationId"]])
        if operation == "get_campaigns_for_lead":
            return {"items": [], "totalCount": 0}
        raise AssertionError("Unexpected provider read: " + operation)

    def _request(self, operation, arguments):
        require(operation == "send_message", "Unexpected external operation")
        claims = list(self.database.execute("SELECT * FROM outreach_action_claims"))
        require(any(json.loads(row["payload"])["message"] == arguments["message"] for row in claims),
                "Transport reached without immutable send claim")
        self.requests.append({"operation": operation, "arguments": copy.deepcopy(arguments)})
        if self.provider_denied:
            raise RuntimeError("Synthetic provider denied message; remaining quota unreported")
        receipt = {"id": "independent-outbound-" + str(len(self.requests)), "sender": "ME",
                   "body": arguments["message"], "createdAt": self.now.isoformat(), "subject": ""}
        if self.receipt_mode == "stale":
            receipt["createdAt"] = (BASE - dt.timedelta(hours=1)).isoformat()
        elif self.receipt_mode == "wrong_text":
            receipt["body"] = "A different message"
        if self.receipts_visible:
            self.add_message(arguments["conversationId"], receipt)
        else:
            self.pending_receipts.append((arguments["conversationId"], receipt))
        if self.lost_ack:
            raise TimeoutError("Synthetic lost acknowledgment after provider request")
        return {"success": True, "acknowledged": True}

    def add_message(self, thread, message):
        self.rooms[thread]["messages"].append(message)
        self.rooms[thread]["totalMessages"] = len(self.rooms[thread]["messages"])

    def reveal_receipts(self):
        for thread, message in self.pending_receipts:
            self.add_message(thread, message)
        self.pending_receipts.clear()

    def execute(self, config, prompt, schema, timeout):
        packet = json.loads(prompt.split("UNTRUSTED_SOURCE_JSON:\n", 1)[1])
        self.inferences.append(packet)
        if "source_snapshot" in packet:
            inbox = json.loads(packet["source_snapshot"]["payload"])["inbox"]
            body = inbox["text"]
            quote = {"start": 0, "end": len(body), "text": body}
            return json.dumps({
                "output_version": reply_worker.VERSION, "source_sha256": packet["source_sha256"],
                "label": "QUESTION", "literal_quote": body, "quote_span": quote,
                "role_reference": None, "reason": "Synthetic contextual question interpretation.",
                "unknown_factual_requirements": self.unknowns.get(inbox["message_id"], []),
                # A draft is produced whether or not a fact is unknown (owner, 2026-09-23).
                "reply_proposal": {"text": TEXT, "basis_quote": quote},
                "next_action": "Answer this authenticated question using the grounded proposal.",
                "review_at": (BASE + dt.timedelta(hours=12)).isoformat(),
            })
        action = packet["action"]
        decision = "READY" if packet["phase"] == "INSPECTION" else "UNKNOWN"
        receipt_observation = receipt_message = None
        if packet["phase"] == "RECONCILIATION":
            for observation_index, observation in enumerate(packet["observation"]["observations"]):
                if observation["operation"] != "get_chatroom" or observation["status"] != "OBSERVED":
                    continue
                for message_index, message in enumerate(observation["raw"].get("messages", [])):
                    if message.get("sender") == "ME":
                        decision = "SENT"
                        receipt_observation, receipt_message = observation_index, message_index
        result = {"output_version": outreach_agent.VERSION, "source_sha256": packet["source_sha256"],
                  "decision": decision, "reason": "Synthetic source-bound inspector judgment.",
                  "next_action": "Inspect authenticated thread and reconcile this exact reply before retry.",
                  "findings": {"prior_outreach": False, "has_reply": True,
                               "negative_reply": False, "opt_out": False},
                  "conversation_id": action["conversation_id"],
                  "receipt_observation": receipt_observation, "receipt_message": receipt_message,
                  "route": "EXISTING_THREAD"}
        if packet["phase"] == "INSPECTION" and self.after_inspection:
            callback, self.after_inspection = self.after_inspection, None
            callback()
        return json.dumps(result)

    @contextlib.contextmanager
    def clock(self):
        current = self.now
        original = dt.datetime

        class FrozenDateTime(original):
            @classmethod
            def now(cls, tz=None):
                return cls.fromisoformat(current.isoformat()).astimezone(tz) if tz else current.replace(tzinfo=None)

        with patch.object(dt, "datetime", FrozenDateTime), patch.object(reply_worker.time, "monotonic", return_value=100):
            yield

    def classify(self):
        with self.clock(), patch.object(reply_worker, "_execute", side_effect=self.execute):
            result = orchestrator.reply_classification_cycle(self.root, self.database, self.config)
        require(self.database.execute("SELECT COUNT(*) FROM reply_worker_results").fetchone()[0] > 0,
                "Fixture did not produce an actual immutable reply worker result: " + repr(result))
        return result

    def cycle(self, seconds=0):
        self.now += dt.timedelta(seconds=seconds)
        with self.clock(), contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(orchestrator, "ROOT", self.root))
            stack.enter_context(patch.object(orchestrator, "HeyReach", return_value=self))
            stack.enter_context(patch.object(reply_worker, "_execute", side_effect=self.execute))
            stack.enter_context(patch.object(orchestrator.decision_transport, "cycle", return_value={"state": "MOCK_UNRELATED"}))
            stack.enter_context(patch.object(orchestrator, "conversion_cycle", return_value={"state": "MOCK_UNRELATED"}))
            stack.enter_context(patch.object(orchestrator, "draft_promotion_cycle", return_value={"state": "MOCK_UNRELATED"}))
            stack.enter_context(patch.object(notifications.Telegram, "send", side_effect=notifications.Unavailable("Synthetic transport unavailable")))
            # Telegram configured but unavailable (without one, the dashboard transport shows the row, E6.T2).
            stack.enter_context(patch.object(notifications, "telegram_configured", return_value=True))
            result = orchestrator.service_cycle(self.root, self.config, self.database)
        self.cycles.append(result)
        require("reply_execution" in result, "Actual service_cycle did not invoke reply execution")
        require(result["reply_execution"].get("state") != "FAILED", "Scheduled reply worker failed: " + repr(result))
        return result

    def actions(self):
        return [dict(row) for row in self.database.execute("SELECT * FROM outreach_actions ORDER BY action_id")]

    def snapshot(self):
        resumes = []
        for packet in self.inferences:
            resume = packet.get("candidate_resume") or packet.get("reply_context", {}).get("candidate_resume")
            if resume:
                text_bytes = resume["text"].encode()
                resumes.append({"phase": packet.get("phase", "CLASSIFICATION"),
                                "source": resume["source"], "source_sha256": resume["source_sha256"],
                                "page_count": resume["page_count"], "text_bytes": len(text_bytes),
                                "text_sha256": hashlib.sha256(text_bytes).hexdigest(),
                                "text_hash_matches": hashlib.sha256(text_bytes).hexdigest() == resume["text_sha256"],
                                "extraction": resume.get("extraction")})
        return {"cycles": self.cycles, "requests": self.requests, "provider_read_count": len(self.reads),
                "inference_count": len(self.inferences), "actions": self.actions(),
                "full_resume_input_evidence": resumes,
                "claims": [dict(row) for row in self.database.execute("SELECT * FROM outreach_action_claims")],
                "read_retries": [dict(row) for row in self.database.execute("SELECT * FROM outreach_read_retries")],
                "reply_results": self.database.execute("SELECT COUNT(*) FROM reply_worker_results").fetchone()[0]}


def grounded_once(fixture):
    fixture.seed()
    fixture.cycle()
    require(len(fixture.requests) == 1, "Grounded proposal did not reach guarded sender exactly once")
    require(fixture.actions()[0]["state"] == "ACKNOWLEDGED_UNCONFIRMED", "Provider acknowledgment was treated as receipt")
    require(fixture.requests[0]["arguments"] == {"conversationId": "thread-1", "linkedInAccountId": 123,
                                                "message": TEXT, "subject": ""}, "Claimed exact message/account/thread changed")
    fixture.cycle(seconds=1)
    require(fixture.actions()[0]["state"] == "CONFIRMED_SENT", "Independent authenticated read did not reconcile")
    receipt = json.loads(fixture.actions()[0]["result"])
    require(receipt["independent"] is True and receipt["receipt_id_kind"] == "independently_observed_message_content_hash",
            "Receipt was not separate provider message evidence")
    audit_id = receipt["evidence"].removeprefix("outreach_agent_audit:")
    observation = json.loads(fixture.database.execute("SELECT record FROM outreach_agent_audit WHERE id=?", (audit_id,)).fetchone()[0])
    require(any(read["operation"] == "get_chatroom" and any(message.get("id") == "independent-outbound-1"
                and message.get("body") == TEXT for message in read["raw"].get("messages", []))
                for read in observation["packet"]["observation"]["observations"]), "No independent provider message in receipt audit")
    fixture.restart()
    fixture.cycle(seconds=1)
    require(len(fixture.requests) == 1 and len(fixture.actions()) == 1, "Restart duplicated reply action or send")
    require(len(fixture.snapshot()["claims"]) == 1, "Restart duplicated durable send claim")
    designated = reply_worker.reply_resume.context()
    observed_phases = set()
    for packet in fixture.inferences:
        resume = packet.get("candidate_resume") or packet.get("reply_context", {}).get("candidate_resume")
        require(resume == designated, "Full designated resume missing or changed in model input")
        observed_phases.add(packet.get("phase", "CLASSIFICATION"))
    require({"CLASSIFICATION", "INSPECTION", "RECONCILIATION"} <= observed_phases,
            "Full resume did not reach all actual model boundaries")


def ordinary_unscoped(fixture):
    fixture.seed(body="Could you tell me more?")
    fixture.cycle()
    require(len(fixture.requests) == 1, "Ordinary unscoped question never reached classifier/guarded sender")
    fixture.cycle(seconds=1)
    require(fixture.actions()[0]["state"] == "CONFIRMED_SENT", "Ordinary unscoped reply not independently confirmed")


def no_direct_history_context(fixture):
    grounded_once(fixture)
    require("outreach_promotion_contexts" not in fixture.config, "Scenario accidentally supplied direct-history clearance")
    proof = json.loads(fixture.snapshot()["claims"][0]["preflight"])
    require(proof.get("reply_transport_supported") is True, "Existing-thread transport support missing")
    require(proof.get("provider_restriction_observed") is False, "No-reported-restriction observation missing")
    require("quota_remaining" in proof and proof["quota_remaining"] is None,
            "Remaining provider quota was fabricated or not recorded unknown")
    require("UNKNOWN_PROVIDER_ENFORCED" in proof.values(), "Unknown quota status not explicitly retained")
    require(proof.get("entitlement_verified") is not True, "Unobserved entitlement incorrectly called verified")
    require(proof.get("cooldown") is not False, "Unreported cooldown incorrectly called verified clear")


def known_message_cooldown(fixture):
    fixture.seed()
    fixture.account["messageCooldown"] = True
    fixture.cycle()
    require(not fixture.requests and not fixture.snapshot()["claims"], "Known messageCooldown allowed a send claim")
    require(any(read["operation"] == "get_linked_in_account_by_id" for read in fixture.reads),
            "Known-cooldown scenario never inspected account")
    require(any(packet.get("phase") == "INSPECTION" for packet in fixture.inferences),
            "Known-cooldown scenario never reached actual reply preflight")
    require(fixture.actions() and fixture.actions()[0]["state"] == "BLOCKED", "Known cooldown not durably blocked")
    fixture.restart()
    fixture.cycle(seconds=1)
    require(not fixture.requests, "Restart bypassed reported message cooldown")


def provider_denial_uncertain(fixture):
    """A provider error is retried after a thread check, 3 attempts in all, then ends recorded.

    Owner, 2026-09-30: "retry on errors also. max 3 attempts with backoff. make sure the retry is
    idempotent by checking heyreach to make sure the attempt wasnt successful."
    """
    fixture.seed()
    fixture.provider_denied = True
    fixture.cycle()
    require(len(fixture.requests) == 1 and fixture.actions()[0]["state"] == "UNCERTAIN_NO_RETRY",
            "Provider denial did not preserve the failed attempt")
    fixture.restart()
    fixture.cycle(seconds=1)
    require(len(fixture.requests) == 1, "Reply retried before its 10-minute backoff")
    reads = len(fixture.reads)
    fixture.cycle(seconds=600)
    require(len(fixture.requests) == 2, "Second attempt did not follow the 10-minute backoff")
    require(any(read["operation"] == "get_chatroom" for read in fixture.reads[reads:]),
            "Retry was not preceded by a HeyReach thread check")
    fixture.cycle(seconds=600)
    require(len(fixture.requests) == 2, "Third attempt did not wait for the 1-hour backoff")
    fixture.cycle(seconds=3000)
    require(len(fixture.requests) == 3, "Third attempt did not follow the 1-hour backoff")
    fixture.cycle(seconds=3600)
    action = fixture.actions()[0]
    require(len(fixture.requests) == 3 and action["state"] == "CANCELLED"
            and json.loads(action["result"]).get("reply_terminal") == "REPLY_SEND_FAILED_AFTER_3_ATTEMPTS",
            "Three failed attempts did not end in a recorded terminal failure: " + repr(action))
    fixture.restart()
    fixture.cycle(seconds=86400)
    require(len(fixture.requests) == 3, "A terminally failed reply was sent again")


# `unsafe_candidate_proposal` was REMOVED here on 2026-09-23 (owner: "Remove the test is what
# im saying. we dont need python deciding rn").
#
# It proved that `reply_worker.py:399` refused any draft whose decision also reported
# CANDIDATE_FACTS. That refusal is gone, so the case could only have been kept by rewriting it
# to assert the opposite of its own name, which is not a test.
#
# What it actually guarded, stated plainly so the decision is not lost: NOTHING now inspects a
# draft's TEXT for claims about the owner. `basis_quote` is recorded but no longer checked
# against the correspondent's message (owner, 2026-09-27), and the prompt still says not to
# invent candidate facts - but that is an instruction, not a check. With `REPLY` in `outreach_allowed_purposes`
# and `reply_execution.enabled` true, a draft that asserts an availability the owner never gave
# can now reach a real person. The owner's position is that Python deciding this is what broke
# the applications worker repeatedly, and that the judgement belongs to the model.
#
# If that is ever revisited, the shape to build is an attestation - the model states its draft
# asserts nothing from its own `unknown_factual_requirements`, and Python enforces only that
# answer - not a blanket refusal to draft.


def late_closure(fixture):
    fixture.seed()
    original = outreach_provider.Provider.send_claimed

    def close_before_provider(provider, action_id, config):
        engagement.close_outreach(fixture.database, PROFILE, "Synthetic late closure", fixture.now.isoformat())
        return original(provider, action_id, config)

    with patch.object(outreach_provider.Provider, "send_claimed", new=close_before_provider):
        fixture.cycle()
    require(len(fixture.snapshot()["claims"]) == 1, "Late-closure scenario never acquired guard claim")
    require(not fixture.requests, "Closure after claiming escaped final provider gate")
    require(fixture.actions()[0]["state"] == "CANCELLED_BEFORE_SEND", "Late closure not durably canceled")
    fixture.restart()
    fixture.cycle(seconds=1)
    require(not fixture.requests, "Restart sent late-closed reply")


def late_thread_change(fixture, sender):
    """No fresh thread read right before sending (owner, 2026-09-30: "we dont need to reread it")."""
    fixture.seed()

    def newer_message():
        fixture.add_message("thread-1", {"id": "late-thread-change", "sender": sender,
                                        "body": "New activity after AI inspection", "createdAt": fixture.now.isoformat()})

    fixture.after_inspection = newer_message
    fixture.cycle()
    require(len(fixture.snapshot()["claims"]) == 1, "Late-thread scenario did not acquire preflight claim")
    require(len(fixture.requests) == 1, "Reply was held by a thread change after its inspection")
    require(sum(read["operation"] == "get_chatroom" for read in fixture.reads) == 1,
            "A final thread read ran before the reply was sent")


def late_account_restriction(fixture):
    """An account flag that appears after inspection is not re-read before sending (owner, 2026-09-30)."""
    fixture.seed()

    def restrict_account():
        fixture.rooms["thread-1"]["linkedInAccount"]["messageCooldown"] = True

    fixture.after_inspection = restrict_account
    fixture.cycle()
    claims = fixture.snapshot()["claims"]
    require(len(claims) == 1, "Late-account scenario did not acquire preflight claim")
    proof = json.loads(claims[0]["preflight"])
    require(proof["reply_transport_supported"] is True and proof["provider_restriction_observed"] is False,
            "Late-account scenario did not begin with supported unrestricted preflight")
    require(len(fixture.requests) == 1, "Reply was held by a final account re-read")
    require("provider_reply_check" not in json.loads(fixture.actions()[0]["result"]),
            "A final thread read ran before the reply was sent")


def interrupted_claim(fixture):
    fixture.seed()
    execution = importlib.import_module("reply_execution")
    try:
        with patch.object(execution.Adapter, "execute_claimed", side_effect=KeyboardInterrupt("Synthetic process death")):
            fixture.cycle()
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("Interruption scenario never entered claimed executor")
    require(fixture.actions()[0]["state"] == "DISPATCHING" and not fixture.requests,
            "Interrupted claim was lost or reached provider unexpectedly")
    fixture.restart()
    fixture.cycle(seconds=901)
    require(not fixture.requests, "Interrupted durable claim was resent without reconciliation")
    require(any(packet.get("phase") == "RECONCILIATION" for packet in fixture.inferences),
            "Restart did not independently investigate interrupted send claim")
    require(fixture.actions()[0]["state"] != "CONFIRMED_SENT", "Unissued interrupted claim falsely confirmed")


def prevented(fixture, blocker):
    fixture.seed(unknowns=["CANDIDATE_FACTS"] if blocker == "candidate_facts" else None)
    fixture.classify()
    if blocker == "closed":
        engagement.close_outreach(fixture.database, PROFILE, "Synthetic explicit closure", fixture.now)
    elif blocker == "excluded":
        with fixture.database:
            fixture.database.execute("INSERT OR REPLACE INTO suppressions VALUES (?,?)", (orchestrator.profile_key(PROFILE), "Explicit user exclusion"))
    elif blocker == "wrong_account":
        fixture.account["id"] = 456
    elif blocker == "wrong_thread":
        fixture.rooms["thread-1"]["id"] = "wrong-thread"
    elif blocker in {"new_inbound", "previous_response"}:
        fixture.add_message("thread-1", {"id": "newer-message", "sender": "CORRESPONDENT" if blocker == "new_inbound" else "ME",
                                        "body": "A newer thread message", "createdAt": fixture.now.isoformat()})
    fixture.cycle()
    require(not fixture.requests, blocker + " reached the sender")
    require(not fixture.snapshot()["claims"], blocker + " acquired a send claim")
    fixture.restart()
    fixture.cycle(seconds=1)
    require(not fixture.requests, blocker + " became sendable after restart")


def ambiguous_reconciliation(fixture):
    fixture.seed()
    fixture.receipts_visible = False
    fixture.lost_ack = True
    fixture.cycle()
    require(len(fixture.requests) == 1 and fixture.actions()[0]["state"] == "UNCERTAIN_NO_RETRY", "Lost ack did not preserve uncertainty")
    fixture.restart()
    fixture.cycle(seconds=1)
    require(len(fixture.requests) == 1, "Uncertain action was resent before reconciliation")
    require(fixture.actions()[0]["state"] != "CONFIRMED_SENT", "Absent receipt falsely confirmed delivery")
    fixture.reveal_receipts()
    fixture.cycle(seconds=901)
    require(fixture.actions()[0]["state"] == "CONFIRMED_SENT", "Due reconciliation did not find independent receipt")
    require(len(fixture.requests) == 1, "Reconciliation duplicated send")


def stale_receipt(fixture):
    fixture.seed()
    fixture.receipt_mode = "stale"
    fixture.cycle()
    require(len(fixture.requests) == 1, "Stale-receipt scenario never reached guarded transport")
    fixture.cycle(seconds=1)
    require(fixture.actions()[0]["state"] != "CONFIRMED_SENT", "Old matching message incorrectly confirmed this attempt")
    fixture.restart()
    fixture.cycle(seconds=901)
    require(len(fixture.requests) == 1, "Stale receipt allowed repeat send")
    require(fixture.actions()[0]["state"] != "CONFIRMED_SENT", "Restart accepted stale receipt")


def wrong_receipt_text(fixture):
    fixture.seed()
    fixture.receipt_mode = "wrong_text"
    fixture.cycle()
    require(len(fixture.requests) == 1, "Wrong-receipt scenario did not send once")
    fixture.cycle(seconds=1)
    require(fixture.actions()[0]["state"] != "CONFIRMED_SENT", "Different outbound message accepted as receipt")
    fixture.restart()
    fixture.cycle(seconds=901)
    require(len(fixture.requests) == 1 and fixture.actions()[0]["state"] != "CONFIRMED_SENT",
            "Wrong-message receipt permitted confirmation or duplicate send")


def bounded_preflight(fixture):
    fixture.seed()
    fixture.read_always_fails = True
    fixture.cycle()
    require(not fixture.requests, "Failed preflight sent")
    first = len(fixture.reads)
    fixture.cycle(seconds=1)
    require(len(fixture.reads) == first, "Preflight retry ignored next_at")
    fixture.cycle(seconds=900)
    fixture.cycle(seconds=3600)
    retries = fixture.snapshot()["read_retries"]
    # Reads hold on a next date instead of exhausting (owner, 2026-09-21). The restart check
    # below is the one that matters: nothing is read or sent before that date arrives.
    require(any(row["phase"] == "INSPECTION" and row["attempts"] == 3 and row["state"] == "RETRY_WAIT" and row["next_at"] for row in retries),
            "Transient preflight did not hold on a next date at three attempts: " + repr(retries))
    reads = len(fixture.reads)
    fixture.restart()
    fixture.cycle(seconds=3600)
    require(len(fixture.reads) == reads and not fixture.requests, "Held preflight retried or sent after restart")


def transient_recovery(fixture):
    fixture.seed()
    fixture.read_always_fails = True
    fixture.cycle()
    require(not fixture.requests, "Transient failure sent")
    fixture.read_always_fails = False
    fixture.cycle(seconds=901)
    require(len(fixture.requests) == 1, "Due recovered preflight did not send")
    fixture.cycle(seconds=1)
    require(fixture.actions()[0]["state"] == "CONFIRMED_SENT", "Recovered send lacked independent receipt")


def isolation_and_notification(fixture):
    fixture.seed()
    fixture.seed(OTHER_PROFILE, "message-2", "thread-2")
    fixture.classify()
    engagement.close_outreach(fixture.database, PROFILE, "Synthetic recipient closure", fixture.now)
    with fixture.database:
        applications.notify(fixture.database, "candidate-user-fact", "A genuine candidate fact needs your answer.", fixture.now.isoformat())
    fixture.cycle()
    require(len(fixture.requests) == 1 and fixture.requests[0]["arguments"]["conversationId"] == "thread-2",
            "Blocked recipient prevented independent recipient execution")
    notification = fixture.database.execute("SELECT * FROM notification_outbox WHERE event_key='candidate-user-fact'").fetchone()
    require(notification is not None and notification["state"] == "PENDING" and notification["next_at"] is not None,
            "Genuine failed notification was deleted or lost retry schedule")
    require(notification["presend_attempts"] > 0, "Notification failure boundary was not exercised")
    fixture.restart()
    fixture.cycle(seconds=1)
    require(len(fixture.requests) == 1, "Isolated recipient repeated after restart")
    notification = fixture.database.execute("SELECT * FROM notification_outbox WHERE event_key='candidate-user-fact'").fetchone()
    require(notification["state"] == "PENDING", "Pending genuine user notification not retained on restart")


SCENARIOS = {
    "grounded_reply_independent_receipt_restart": grounded_once,
    "ordinary_unscoped_question": ordinary_unscoped,
    "no_direct_history_context_unknown_quota": no_direct_history_context,
    "known_message_cooldown_blocks": known_message_cooldown,
    "provider_error_retried_three_times_then_terminal": provider_denial_uncertain,
    "closure_after_claim_before_provider": late_closure,
    "new_inbound_after_inspection_not_reread_before_send": lambda fixture: late_thread_change(fixture, "CORRESPONDENT"),
    "previous_response_after_inspection_not_reread_before_send": lambda fixture: late_thread_change(fixture, "ME"),
    "account_restriction_after_inspection_not_reread_before_send": late_account_restriction,
    "interrupted_claim_reconciles_without_send": interrupted_claim,
    # "candidate_facts" dropped from this list on 2026-09-23 (owner). It asserted that a
    # decision reporting CANDIDATE_FACTS prevented a send; that gate was removed deliberately,
    # so the case now asserts something untrue. The six that remain are the real controls and
    # are untouched: explicit closure, suppression/exclusion, wrong account, wrong thread, a
    # newer inbound message, and a previous response in the thread.
    **{name: (lambda fixture, blocker=name: prevented(fixture, blocker)) for name in
       ("closed", "excluded", "wrong_account", "wrong_thread", "new_inbound", "previous_response")},
    "ambiguous_send_reconciles_without_resend": ambiguous_reconciliation,
    "stale_receipt_never_authorizes_retry": stale_receipt,
    "wrong_receipt_text_never_confirms": wrong_receipt_text,
    "transient_preflight_bounded_retries": bounded_preflight,
    "transient_preflight_recovers": transient_recovery,
    "blocked_recipient_isolation_pending_notification": isolation_and_notification,
}


def validate(names=None, report_path=REPORT):
    report = {"state": "WAITING_FOR_INTEGRATION", "synthetic_only": True, "network_calls": 0,
              "live_sends": 0, "interface": INTERFACE, "scenarios": [],
              "limitations": ["Mocks do not establish live provider behavior or model judgment quality.",
                              "Unrelated conversion, research promotion and decision transport cycles are stubbed.",
                              "Actual designated stage resume is locally extracted and hash-verified before subprocess isolation; no model process executes."]}
    if report_path and report_path.exists():
        previous = json.loads(report_path.read_text())
        report["prior_runs"] = previous.get("prior_runs", []) + [{
            "state": previous["state"], "runtime_sha256": previous.get("runtime_sha256"),
            "results": [{key: value for key, value in item.items() if key != "evidence"}
                        for item in previous.get("scenarios", [])],
        }]
    if not (ROOT / "reply_execution.py").exists() or not hasattr(orchestrator, "reply_execution_cycle"):
        report["blocker"] = "Parent reply_execution.py and scheduled integration not ready"
        if report_path:
            report_path.write_text(json.dumps(report, indent=2) + "\n")
        return report
    importlib.import_module("reply_execution")
    report["state"] = "PASS"
    with isolated_boundaries():
        for name in names or SCENARIOS:
            with Fixture() as fixture:
                result = {"name": name, "state": "PASS"}
                try:
                    SCENARIOS[name](fixture)
                except Exception as error:
                    report["state"] = "FAIL"
                    result.update(state="FAIL", error_type=type(error).__name__, error=str(error), traceback=traceback.format_exc())
                    print("FAIL " + name + ": " + str(error), flush=True)
                else:
                    print("PASS " + name, flush=True)
                result["evidence"] = fixture.snapshot()
                report["scenarios"].append(result)
                if report_path:
                    report_path.write_text(json.dumps(report, indent=2) + "\n")
    report["passed"] = sum(item["state"] == "PASS" for item in report["scenarios"])
    report["failed"] = sum(item["state"] == "FAIL" for item in report["scenarios"])
    report["runtime_sha256"] = {filename: hashlib.sha256((ROOT / filename).read_bytes()).hexdigest()
                                 for filename in ("reply_execution.py", "orchestrator.py", "outreach.py", "outreach_provider.py",
                                                  "outreach_agent.py", "pipeline_routing.py", "reply_worker.py", "reply_resume.py",
                                                  "outreach-agent-prompt.md", "outreach-agent-schema.json", "reply-prompt.md", "reply-schema.json")}
    if report_path:
        report_path.write_text(json.dumps(report, indent=2) + "\n")
    return report


if __name__ == "__main__":
    result = validate(sys.argv[1:] or None)
    print(json.dumps({key: value for key, value in result.items() if key not in {"scenarios", "interface", "prior_runs"}}, indent=2))
    raise SystemExit(0 if result["state"] == "PASS" else 2)
