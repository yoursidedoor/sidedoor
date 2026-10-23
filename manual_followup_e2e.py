"""Offline manual scenarios: real queues/claims/receipts, synthetic boundaries.

Run before adding regression tests. Prints evidence to stdout; writes no report,
production state, config, or files. The injected adapter models the parent hooks;
passing this script does not certify an installed provider or scheduled entry.
"""

import contextlib
import copy
import datetime as dt
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import patch

import engagement
import followup_execution as followup
import outreach
import outreach_agent
import outreach_provider
import research_workflow
import workflow_tasks
from orchestrator import profile_key


ROOT = Path(__file__).resolve().parent
INITIAL = dt.datetime(2026, 9, 11, 16, tzinfo=dt.timezone.utc)
DUE = engagement.business_days_after(INITIAL, 3)
PROFILE = "https://www.linkedin.com/in/synthetic-followup-person"
RESUME = {"source": "synthetic-designated-resume", "source_sha256": "a" * 64,
          "page_count": 5, "text": "Synthetic full resume.\f" * 5, "text_sha256": "b" * 64}


def require(value, message):
    if not value:
        raise AssertionError(message)


@contextlib.contextmanager
def offline():
    with contextlib.ExitStack() as stack:
        for target in ("socket.socket.connect", "socket.getaddrinfo", "subprocess.Popen", "urllib.request.urlopen"):
            stack.enter_context(patch(target, side_effect=AssertionError("External execution forbidden")))
        stack.enter_context(patch("followup_execution.reply_resume.context", return_value=copy.deepcopy(RESUME)))
        yield


class Fixture:
    def __init__(self, invitation=False):
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        outreach.initialize_actions(self.database)
        research_workflow.initialize(self.database)
        self.database.execute("CREATE TABLE IF NOT EXISTS suppressions (identity TEXT PRIMARY KEY, reason TEXT)")
        self.database.commit()
        self.now = DUE
        self.config = {"sender_id": 123, "sender_profile": "https://www.linkedin.com/in/synthetic-owner",
            "model": "synthetic", "codex": "NEVER", "timezone": "America/Los_Angeles",
            "followup_execution": {"enabled": True, "timeout_seconds": 1, "max_items": 4},
            "daily_compute_seconds": 10800, "outreach_actions_enabled": True, "outreach_mode": "guarded",
            "outreach_allowed_purposes": ["FOLLOWUP"]}
        self.candidate = {"profile": PROFILE, "recipient": "Synthetic Person", "company": "Synthetic Company",
            "conversation_id": "synthetic-thread", "verification": {"verified_at": DUE.isoformat(),
            "identity_verified": True, "team_relevance_verified": True,
            "identity_source": "https://example.test/person", "team_source": "https://example.test/team"}}
        self.room = {"id": "synthetic-thread", "linkedInAccountId": 123,
            "linkedInAccount": {"id": 123, "profileUrl": self.config["sender_profile"], "authIsValid": True, "isActive": True},
            "groupChat": False, "correspondentProfile": {"profileUrl": PROFILE, "fullName": "Synthetic Person"},
            "blockedByMe": False, "blockedByParticipant": False, "messages": [], "totalMessages": 0}
        self.reads, self.inferences, self.sends = [], [], []
        self.output_mode = "COMPOSE"
        self.lost_ack = False
        self.receipt_visible = True
        self.read_error = False
        self.after_ai = None
        self.before_send = None
        self.anchor(invitation=invitation)
        self.adapter = outreach.HeyReachActionAdapter(self.transport, self.inspect, self.reconcile)
        self.adapter.provider = SimpleNamespace(collect=self.collect)
        self.adapter.followup_contract = followup.VERSION

    def anchor(self, kind="initial_sent", at=INITIAL, body="I saw your team's platform work and would like to connect.", invitation=False):
        source_id = "source-" + str(self.database.execute("SELECT COUNT(*) FROM engagement_events").fetchone()[0])
        raw = {"message_id": source_id, "conversation_id": "synthetic-thread", "account_id": 123,
               "sender_profile": self.config["sender_profile"], "account_profile": self.config["sender_profile"],
               "profile": PROFILE, "direction": "outbound", "text": body, "quoted": None, "forwarded": None}
        if invitation:
            raw["message_type"] = "invitation"
        inbox = {**raw, "schema": "linkedin-inbox-v1", "raw_paths": {field: [field] for field in raw},
                 "authentication": {"method": "authenticated_provider_read", "source": "https://example.test/inbox",
                                    "verified_by": "synthetic-decoder", "observed_at": at.isoformat()}}
        event = {"source": "heyreach", "source_id": source_id, "profile": PROFILE, "kind": kind,
                 "occurred_at": at.isoformat(), "observed_at": at.isoformat(), "evidence": "https://example.test/inbox",
                 "verified": True, "raw": raw, "inbox": inbox}
        engagement.record_event(self.database, event)
        self.room["messages"].append({"id": source_id, "body": body, "createdAt": at.isoformat(), "sender": "ME"})
        self.room["totalMessages"] = len(self.room["messages"])
        self.database.commit()

    def event(self, kind):
        engagement.record_event(self.database, {"source": "linkedin", "source_id": kind,
            "profile": PROFILE, "kind": kind, "occurred_at": self.now.isoformat(), "observed_at": self.now.isoformat(),
            "evidence": "https://example.test/" + kind, "verified": True, "raw": {"kind": kind}})
        self.database.commit()

    def collect(self, action, phase):
        self.reads.append((action["action_id"], phase))
        if self.read_error:
            raise TimeoutError("synthetic read timeout")
        return {"finished_at": self.now.isoformat(), "observations": [{"operation": "get_chatroom",
            "status": "OBSERVED", "authenticated": True, "identity_matches": True, "raw": copy.deepcopy(self.room)}]}

    def execute(self, config, prompt, schema, timeout):
        packet = json.loads(prompt.split("UNTRUSTED_SOURCE_JSON:\n", 1)[1])
        self.inferences.append(packet)
        if self.after_ai:
            self.after_ai()
        if self.output_mode == "TIMEOUT":
            raise TimeoutError("synthetic model timeout")
        result = {"output_version": followup.VERSION, "source_sha256": packet["source_sha256"],
                  "decision": self.output_mode if self.output_mode in {"COMPOSE", "HOLD"} else "COMPOSE",
                  "message": "Following up on my platform note; happy to share relevant details." if self.output_mode != "HOLD" else None,
                  "reason": "Synthetic contextual judgment grounded in the supplied note.",
                  "next_action": "Use the existing fresh-history guard."}
        if self.output_mode == "WRONG_HASH":
            result["source_sha256"] = "wrong"
        return json.dumps(result)

    def inspect(self, action, config):
        require(not followup.binding_gates(self.database, config, action, self.now), "binding check failed")
        require(not followup.thread_gates(self.database, config, action, self.room, self.now), "thread check failed")
        follows = sum(event["kind"] == "followup_sent" for event in engagement.events_for(self.database, PROFILE))
        return {"identity": profile_key(action["profile"]), "sender_id": 123, "sender_ready": True,
            "entitlement_verified": True, "direct_messages_complete": True, "sent_invitations_complete": True,
            "pending_invitation": False, "negative_reply": False, "opt_out": False, "cooldown": False,
            # As FollowupAdapter.inspect does: a correspondent message in the thread is a reply.
            "has_reply": any(message.get("sender") == "CORRESPONDENT" for message in self.room["messages"]),
            "checked_at": self.now.isoformat(), "evidence": "synthetic independent direct-history read",
            "conversation_id": action["conversation_id"], "initial_message_sent": True, "followups_sent": follows,
            "agent_decision": "READY", "provider_snapshot_complete": True,
            "followup_transport_supported": True, "followup_context_verified": True, "followup_thread_unchanged": True,
            "provider_restriction_observed": False,
            "history_scope": "AUTHENTICATED_EXISTING_THREAD_AND_CANONICAL_LIFECYCLE",
            "followup_task_id": action["followup_task_id"], "followup_sources_sha256": action["followup_sources_sha256"],
            "followup_thread_sha256": action["followup_thread_sha256"]}

    def transport(self, operation, arguments):
        if self.before_send:
            self.before_send()
        row = self.database.execute("SELECT * FROM outreach_actions WHERE state='REQUEST_STARTED'").fetchone()
        action = json.loads(row["payload"])
        gates = followup.thread_gates(self.database, self.config, action, self.room, self.now)
        if gates:
            raise ValueError("synthetic final guard stopped request")
        self.sends.append(copy.deepcopy(arguments))
        self.room["messages"].append({"id": "sent-" + str(len(self.sends)), "sender": "ME",
            "body": arguments["message"], "createdAt": self.now.isoformat()})
        self.room["totalMessages"] = len(self.room["messages"])
        if self.lost_ack:
            raise TimeoutError("synthetic lost acknowledgement")
        return {"acknowledged": True}

    def reconcile(self, action, config):
        if not self.receipt_visible:
            return {"state": "UNKNOWN"}
        claim = self.database.execute("SELECT * FROM outreach_action_claims WHERE action_id=?", (action["action_id"],)).fetchone()
        matches = [message for message in self.room["messages"] if message["body"] == action["message"]
            and engagement.timestamp(message["createdAt"]) >= engagement.timestamp(claim["claimed_at"])]
        if not matches:
            return {"state": "UNKNOWN"}
        return {"independent": True, "outcome": "SENT", "action_id": action["action_id"], "identity": claim["identity"],
            "payload_hash": claim["payload_hash"], "sender_id": 123, "operation": "send_message",
            "conversation_id": action["conversation_id"], "receipt_id": matches[-1]["id"],
            "evidence": "synthetic-independent-thread-read", "observed_at": self.now.isoformat(),
            "occurred_at": matches[-1]["createdAt"]}

    def run(self, dispatch=False, candidates=None):
        self.config["followup_execution"]["dispatch_enabled"] = dispatch
        return followup.cycle(ROOT, self.database, self.config, self.adapter,
            candidates=[self.candidate] if candidates is None else candidates, execute=self.execute, now=self.now)

    def restart(self):
        content = self.database.serialize()
        self.database.close()
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        self.database.deserialize(content)

    def close(self):
        self.database.close()


class ProviderFixture(Fixture):
    """Actual FollowupAdapter and Provider; only network and inference are fake."""

    def __init__(self):
        super().__init__(invitation=True)
        self.config["outreach_agent"] = {"enabled": True, "timeout_seconds": 1}
        self.judge_decision = "READY"
        self.after_judge = None
        self.final_guard_change = None
        self.provider_reads = []
        self.adapter = followup.FollowupAdapter(ROOT, self.database, self.config, self, execute=self.execute)

    def call(self, operation, arguments):
        self.provider_reads.append(operation)
        if operation == "get_linked_in_account_by_id":
            return copy.deepcopy(self.room["linkedInAccount"])
        if operation == "get_conversations_v2":
            return {"items": [copy.deepcopy(self.room)], "totalCount": 1}
        if operation == "get_chatroom":
            issued_claim = self.database.execute("SELECT 1 FROM outreach_actions WHERE state='REQUEST_STARTED'").fetchone()
            if issued_claim and self.final_guard_change:
                callback, self.final_guard_change = self.final_guard_change, None
                callback()
            return copy.deepcopy(self.room)
        if operation == "get_campaigns_for_lead":
            return {"items": [], "totalCount": 0}
        raise AssertionError("Unexpected synthetic provider read: " + operation)

    def _request(self, operation, arguments):
        require(operation == "send_message", "Unexpected external mutation")
        claims = list(self.database.execute("SELECT * FROM outreach_action_claims"))
        require(any(json.loads(claim["payload"])["message"] == arguments["message"] for claim in claims), "Missing immutable send claim")
        rows = list(self.database.execute("SELECT result FROM outreach_actions WHERE state='REQUEST_STARTED'"))
        require(any(json.loads(row[0]).get("provider_request", {}).get("arguments") == arguments for row in rows), "Missing committed provider request")
        self.sends.append(copy.deepcopy(arguments))
        if self.receipt_visible:
            self.room["messages"].append({"id": "observed-" + str(len(self.sends)), "sender": "ME",
                "body": arguments["message"], "subject": "", "createdAt": self.now.isoformat()})
            self.room["totalMessages"] = len(self.room["messages"])
        if self.lost_ack:
            raise TimeoutError("Synthetic acknowledgment lost")
        return {"acknowledged": True}

    def execute(self, config, prompt, schema, timeout):
        packet = json.loads(prompt.split("UNTRUSTED_SOURCE_JSON:\n", 1)[1])
        if "action" not in packet:
            return super().execute(config, prompt, schema, timeout)
        self.inferences.append(packet)
        decision = self.judge_decision if packet["phase"] == "INSPECTION" else "UNKNOWN"
        observation_index, message_index = None, None
        if packet["phase"] == "RECONCILIATION":
            for current_observation, observation in enumerate(packet["observation"]["observations"]):
                if observation["operation"] != "get_chatroom" or observation["status"] != "OBSERVED":
                    continue
                for current_message, message in enumerate(observation["raw"].get("messages", [])):
                    if message.get("body") == packet["action"]["message"] and message.get("sender") == "ME":
                        observation_index, message_index, decision = current_observation, current_message, "SENT"
        if packet["phase"] == "INSPECTION" and self.after_judge:
            callback, self.after_judge = self.after_judge, None
            callback()
        return json.dumps({"output_version": outreach_agent.VERSION, "source_sha256": packet["source_sha256"],
            "decision": decision, "reason": "Synthetic independent contextual assessment of the exact follow-up.",
            "next_action": "Use the existing guard or independently reconcile this attempt.",
            "conversation_id": packet["action"]["conversation_id"],
            "receipt_observation": observation_index, "receipt_message": message_index, "route": "EXISTING_THREAD",
            "findings": {"prior_outreach": True, "has_reply": False, "negative_reply": False, "opt_out": False}})

    def run(self, dispatch=False, candidates=None):
        with patch("outreach_provider._now", side_effect=lambda: self.now.isoformat()), \
                patch("followup_execution._now", side_effect=lambda value=None: engagement.timestamp(self.now if value is None else value)):
            return super().run(dispatch, candidates)

    def restart(self):
        super().restart()
        self.adapter = followup.FollowupAdapter(ROOT, self.database, self.config, self, execute=self.execute)


def provider_e2e(lost_ack=False, late_change=None, receipt_visible=True):
    fixture = ProviderFixture()
    try:
        fixture.lost_ack = lost_ack
        fixture.receipt_visible = receipt_visible
        if late_change == "reply":
            def incoming():
                fixture.room["messages"].append({"id": "late-inbound", "sender": "CORRESPONDENT", "body": "No thanks.", "createdAt": fixture.now.isoformat()})
                fixture.room["totalMessages"] = len(fixture.room["messages"])
            fixture.final_guard_change = incoming
        elif late_change == "closure":
            fixture.final_guard_change = lambda: engagement.close_outreach(fixture.database, fixture.candidate["profile"], "closed after durable send claim", fixture.now)
        first = fixture.run(True)
        if late_change:
            require(not fixture.sends, "Late change bypassed final provider guard")
            require(fixture.final_guard_change is None, "Late-change scenario never reached the final provider thread read")
            claim = fixture.database.execute("SELECT action_id,payload_hash,sender_id,claimed_at FROM outreach_action_claims").fetchone()
            require(claim is not None, "Final guard scenario had no durable send claim")
            saved = fixture.database.execute("SELECT state,result FROM outreach_actions").fetchone()
            return {"late_change": late_change, "mock_provider_requests": 0, "real_network": 0,
                    "state": saved["state"], "claim": dict(claim), "provider_result": json.loads(saved["result"])}
        require(len(fixture.sends) == 1, "Parent shared guard/provider integration required: " + json.dumps(first))
        require(fixture.database.execute("SELECT state FROM outreach_actions").fetchone()[0] != "CONFIRMED_SENT", "Acknowledgment became delivery")
        fixture.restart()
        fixture.run(True)
        fixture.run(True)
        state = fixture.database.execute("SELECT state FROM outreach_actions").fetchone()[0]
        if not receipt_visible:
            require(state != "CONFIRMED_SENT" and len(fixture.sends) == 1, "Uncertain delivery was retried or called confirmed")
            require(not any(event["kind"] == "followup_sent" for event in engagement.events_for(fixture.database, PROFILE)), "Uncertain delivery incremented follow-up count")
            return {"state": state, "mock_provider_requests": 1, "real_network": 0, "independent_receipt": "UNKNOWN",
                    "claim": dict(fixture.database.execute("SELECT action_id,payload_hash,sender_id,claimed_at FROM outreach_action_claims").fetchone())}
        require(state == "CONFIRMED_SENT" and len(fixture.sends) == 1, "Independent receipt/replay failed")
        require({packet.get("phase") for packet in fixture.inferences} >= {"INSPECTION", "RECONCILIATION"}, "Independent AI path missing")
        saved = fixture.database.execute("SELECT result FROM outreach_actions").fetchone()
        claim = fixture.database.execute("SELECT action_id,payload_hash,sender_id,claimed_at FROM outreach_action_claims").fetchone()
        return {"state": state, "mock_provider_requests": len(fixture.sends), "real_network": 0, "lost_ack": lost_ack,
                "claim": dict(claim), "independent_receipt": json.loads(saved["result"]),
                "ai_phases": [packet.get("phase", "COMPOSITION") for packet in fixture.inferences]}
    finally:
        fixture.close()


class CampaignFollowupFixture(ProviderFixture):
    def __init__(self, accepted=True, delivered=True, invitation_in_thread=False):
        import manual_campaign_e2e

        super().__init__()
        connect = sqlite3.connect
        with patch("tempfile.TemporaryDirectory", return_value=SimpleNamespace(name="unused-in-memory", cleanup=lambda: None)), \
                patch("sqlite3.connect", side_effect=lambda unused: connect(":memory:")):
            self.campaign = manual_campaign_e2e.Fixture()
        self.campaign.restart = lambda: None
        self.campaign.create(self.campaign.prepare_scheduled())
        require(self.campaign.dispatch()["launch_attempts"] == 1, "Campaign was not actually claimed and launched")
        self.database.close()
        self.database = self.campaign.database
        self.config.update(self.campaign.config)
        self.config["outreach_allowed_purposes"] = ["FOLLOWUP", "PREPARE_CAMPAIGN"]
        self.config["followup_execution"] = {"enabled": True, "dispatch_enabled": True, "timeout_seconds": 1, "max_items": 1}
        self.config["outreach_agent"] = {"enabled": True, "timeout_seconds": 1}
        self.campaign.delivered = delivered
        self.campaign.now += dt.timedelta(minutes=20)
        manual_campaign_e2e.campaign.reconcile_delivery(self.database, self.config, self.campaign, 201, self.campaign.now)
        self.initial = self.campaign.now
        self.now = engagement.business_days_after(self.initial, 3)
        self.candidate = dict(self.campaign.action, conversation_id=None)
        self.room.update(linkedInAccountId=self.config["sender_id"], messages=[], totalMessages=0)
        self.room["linkedInAccount"].update(id=self.config["sender_id"], profileUrl=self.config["sender_profile"])
        self.room["correspondentProfile"].update(profileUrl=self.candidate["profile"], fullName=self.candidate["recipient"])
        if invitation_in_thread:
            self.room["messages"] = [{"id": "observed-invitation", "sender": "ME", "body": self.candidate["message"],
                "createdAt": (self.initial - dt.timedelta(minutes=10)).isoformat()}]
            self.room["totalMessages"] = 1
        if accepted:
            engagement.record_event(self.database, {"source": "mock-authenticated-acceptance", "source_id": "accepted-201",
                "profile": self.candidate["profile"], "kind": "accepted", "verified": True,
                "occurred_at": (self.initial + dt.timedelta(hours=1)).isoformat(), "observed_at": self.now.isoformat(),
                "evidence": "mock:independently-observed-acceptance", "raw": {"accepted": True}})
            self.database.commit()
        self.adapter = followup.FollowupAdapter(ROOT, self.database, self.config, self, execute=self.execute)

    def call(self, operation, arguments):
        if operation in {"get_campaign", "get_campaign_sequence", "get_leads_from_list", "get_overall_stats"}:
            self.provider_reads.append(operation)
            result = self.campaign.call(operation, arguments)
            if operation == "get_overall_stats":
                result["overallStats"]["connectionsAccepted"] = int(getattr(self, "accepted_in_provider", True))
            return result
        if operation == "get_lead":
            self.provider_reads.append(operation)
            return {}
        return super().call(operation, arguments)

    def run(self, dispatch=True, candidates=None):
        self.config["followup_execution"]["dispatch_enabled"] = dispatch
        with patch("outreach_provider._now", side_effect=lambda: self.now.isoformat()), \
                patch("followup_execution._now", side_effect=lambda value=None: engagement.timestamp(self.now if value is None else value)):
            return followup.cycle(ROOT, self.database, self.config, self.adapter, execute=self.execute, now=self.now)

    def close(self):
        self.campaign.close()
        self.database.close()


def campaign_followup_e2e(case="confirmed"):
    fixture = CampaignFollowupFixture(accepted=False, delivered=case != "unconfirmed",
                                     invitation_in_thread=case == "invitation_in_thread")
    try:
        original_verification = json.loads(fixture.database.execute("SELECT payload FROM outreach_launch_claims").fetchone()[0])["verification"]
        fixture.accepted_in_provider = case != "unaccepted"
        if case == "pending_then_accepted":
            fixture.accepted_in_provider = False
            for unused in range(4):
                waiting = fixture.run()
                require(waiting["composition"]["items"][0]["state"] == "WAIT_ACCEPTANCE", "Pending is not a business wait")
                reads = len(fixture.provider_reads)
                fixture.run()
                require(len(fixture.provider_reads) == reads, "Acceptance was polled before its backoff")
                require(not fixture.database.execute("SELECT 1 FROM workflow_tasks WHERE json_extract(inputs,'$[0].followup_version')=?", (followup.VERSION,)).fetchone(), "Waiting acceptance spent composition attempts")
                wait = dt.datetime.fromisoformat(waiting["composition"]["items"][0]["next_at"]) - fixture.now
                require(wait == dt.timedelta(hours=(1, 2, 4, 8)[unused]), "Acceptance poll backoff is not 1h, 2h, 4h, 8h")
                fixture.now += wait
            fixture.restart()
            fixture.now += dt.timedelta(days=1)
            fixture.accepted_in_provider = True
        elif case == "acceptance_read_failures_keep_polling":
            original_call = fixture.call
            def unavailable(operation, arguments):
                if operation == "get_overall_stats":
                    fixture.provider_reads.append(operation)
                    raise TimeoutError("Mock temporary provider failure")
                return original_call(operation, arguments)
            fixture.call = unavailable
            for hours in (1, 2, 4):
                waiting = fixture.run()
                require(waiting["composition"]["items"][0]["blocker"] == "FOLLOWUP_ACCEPTANCE_READ_FAILED", "Technical failure was not retained")
                reads = fixture.provider_reads.count("get_overall_stats")
                fixture.now += dt.timedelta(hours=hours) - dt.timedelta(minutes=1)
                fixture.run()
                require(fixture.provider_reads.count("get_overall_stats") == reads, "A failed read was retried before its backoff")
                fixture.now += dt.timedelta(minutes=1)
            waiting = fixture.run()
            require(waiting["composition"]["items"][0]["blocker"] == "FOLLOWUP_ACCEPTANCE_READ_FAILED", "Failed reads must keep polling, never park")
            require(fixture.provider_reads.count("get_overall_stats") == 4 and not fixture.inferences and not fixture.sends, "Failed read sent or skipped")
            require([followup.poll_wait(n) for n in (1, 2, 3, 9, 50)] == [dt.timedelta(hours=1), dt.timedelta(hours=2),
                    dt.timedelta(hours=4), dt.timedelta(days=7), dt.timedelta(days=7)], "Backoff is not 1h doubling, capped at 7 days")
            return {"case": case, "technical_attempts": 4, "mock_followup_requests": 0}
        if case == "before_due":
            fixture.now -= dt.timedelta(microseconds=1)
        elif case == "no_thread":
            original_call = fixture.call
            fixture.call = lambda operation, arguments: {"items": [], "totalCount": 0} if operation == "get_conversations_v2" else original_call(operation, arguments)
        elif case == "no_automatic":
            with fixture.database:
                research_workflow.register_alias(fixture.database, "contact", "https://www.linkedin.com/in/jrivera", fixture.candidate["profile"], "mock:confirmed-alias")
        elif case == "closure":
            engagement.close_outreach(fixture.database, fixture.candidate["profile"], "mock-closed", fixture.now)
        first = fixture.run()
        if case in {"before_due", "unaccepted", "unconfirmed", "no_thread", "no_automatic", "closure"}:
            require(not fixture.sends and not fixture.inferences, "Blocked campaign composed or sent: " + json.dumps(first))
            if case not in {"no_thread", "unaccepted"}:
                require(not fixture.provider_reads, "Blocked campaign performed provider reads")
            expected = {"before_due": None, "unaccepted": None,
                        "no_thread": "FOLLOWUP_OBSERVED_THREAD_REQUIRED", "no_automatic": "NO_AUTOMATIC_FOLLOWUP_CONTACT",
                        "closure": "OUTREACH_CLOSED"}
            if case in expected:
                require(first["composition"]["items"][0].get("blocker") == expected[case], "Wrong blocked path: " + json.dumps(first))
            if case == "unaccepted":
                require(first["composition"]["items"][0]["state"] == "WAIT_ACCEPTANCE", "Pending acceptance became a failure")
            return {"case": case, "result": first, "mock_followup_requests": 0}
        require(len(fixture.sends) == 1, "Default campaign discovery did not send: " + json.dumps(first))
        require(fixture.inferences[0]["candidate"]["conversation_id"] == fixture.room["id"], "Conversation was not bound to provider observation")
        require(fixture.inferences[0]["initial_invitation"]["text"] == fixture.candidate["message"], "Actual campaign invitation missing")
        accepted = next(event for event in engagement.events_for(fixture.database, fixture.candidate["profile"]) if event["kind"] == "accepted")
        require(accepted["source_id"] == "campaign-acceptance:201" and accepted["occurred_at"] == fixture.now.isoformat(), "Acceptance was fabricated or backdated")
        fixture.restart()
        fixture.run()
        receipt = fixture.database.execute("SELECT result FROM outreach_actions WHERE state='CONFIRMED_SENT'").fetchone()
        require(receipt and json.loads(receipt[0])["independent"] is True, "First follow-up missing independent receipt")
        if case == "second_old_context":
            first_sent = next(event for event in engagement.events_for(fixture.database, fixture.candidate["profile"]) if event["kind"] == "followup_sent")
            fixture.now = engagement.business_days_after(first_sent["occurred_at"], 5)
            second = fixture.run()
            require(len(fixture.sends) == 2, "Second follow-up failed with untouched original timestamps: " + json.dumps(second))
            composition = [packet for packet in fixture.inferences if "action" not in packet][-1]
            require(composition["historical_context"]["age_seconds"] > 604800, "Historical context was falsely refreshed")
            require(composition["historical_context"]["verification"] == original_verification, "Original verification was changed")
            fixture.restart()
            fixture.run()
        fixture.now += dt.timedelta(days=10)
        if case == "second_old_context":
            fixture.run()
            require(len(fixture.sends) == 2, "Campaign exceeded two follow-ups")
        return {"case": case, "mock_followup_requests": len(fixture.sends), "initial_verified_at": original_verification["verified_at"],
            "initial_event": next(event for event in engagement.events_for(fixture.database, fixture.candidate["profile"]) if event["kind"] == "initial_sent"),
            "receipts": [json.loads(row[0]) for row in fixture.database.execute("SELECT result FROM outreach_actions WHERE state='CONFIRMED_SENT'")],
            "ai_phases": [packet.get("phase", "COMPOSITION") for packet in fixture.inferences]}
    finally:
        fixture.close()


def scenario(name):
    fixture = Fixture(invitation=name in {"invitation", "acceptance", "missing_invitation", "invitation_not_in_thread"})
    try:
        if name == "before_due":
            fixture.now = DUE - dt.timedelta(microseconds=1)
            result = fixture.run(True)
            require(result["composition"]["items"][0]["state"] == "WAITING" and not fixture.reads and not fixture.inferences and not fixture.sends, name)
        elif name in {"invitation", "acceptance", "missing_invitation", "invitation_not_in_thread"}:
            if name == "acceptance":
                fixture.now = INITIAL + dt.timedelta(hours=13)
                fixture.event("accepted")
                fixture.run(True)
                require(not fixture.inferences and not fixture.sends, "Acceptance restarted note outreach")
            else:
                if name == "missing_invitation":
                    fixture = replace_fixture(fixture, Fixture())
                if name == "invitation_not_in_thread":
                    fixture.room.update(messages=[], totalMessages=0)
                fixture.run()
                packet = fixture.inferences[0]
                require(bool(packet["initial_invitation"]) == (name != "missing_invitation"), "Invitation incorrectly inferred")
                require(len(packet["conversation_context"]) <= 8 and packet["candidate_resume"]["page_count"] == 5, name)
        elif name in {"reply", "opt_out", "closure", "excluded", "no_automatic", "suppression", "unknown_outbound", "urgent"}:
            if name in {"reply", "opt_out", "unknown_outbound"}:
                fixture.event({"reply": "inbound", "opt_out": "opt_out", "unknown_outbound": "outbound_observed"}[name])
            elif name == "closure":
                engagement.close_outreach(fixture.database, PROFILE, "explicit synthetic closure", fixture.now)
            elif name == "excluded":
                fixture.candidate["recipient"] = "Taylor Quinn"
            elif name == "no_automatic":
                fixture.candidate["campaign_id"] = 700001
            elif name == "suppression":
                fixture.database.execute("INSERT INTO suppressions VALUES (?,?)", (profile_key(PROFILE), "Excluded by owner"))
                fixture.database.commit()
            else:
                fixture.now = INITIAL + dt.timedelta(hours=48)
                engagement.record_event(fixture.database, {"source": "linkedin", "source_id": "interview",
                    "profile": PROFILE, "kind": "interview", "occurred_at": (fixture.now + dt.timedelta(hours=24)).isoformat(),
                    "observed_at": fixture.now.isoformat(), "evidence": "synthetic interview schedule", "verified": True, "raw": {}})
                fixture.database.commit()
                require(engagement.next_action(fixture.database, PROFILE, fixture.now)["purpose"] == "URGENT", "Fixture did not exercise urgent bypass")
            fixture.run(True)
            require(not fixture.inferences and not fixture.sends, name)
        elif name in {"wrong_account", "missing_thread", "fresh_reply", "unreconciled_outbound", "partial_thread", "raw_mismatch", "wrong_hash", "ai_hold", "changed_during_ai"}:
            if name == "wrong_account":
                fixture.room["linkedInAccountId"] = 999
            elif name == "missing_thread":
                fixture.room["id"] = "unrelated-thread"
            elif name == "fresh_reply":
                fixture.room["messages"][0]["sender"] = "CORRESPONDENT"
            elif name == "unreconciled_outbound":
                fixture.room["messages"][0]["body"] = "Unknown outbound"
            elif name == "partial_thread":
                fixture.room["totalMessages"] += 1
            elif name == "raw_mismatch":
                row = fixture.database.execute("SELECT payload FROM engagement_events").fetchone()
                payload = json.loads(row[0])
                payload["inbox"]["text"] = "Invented text"
                fixture.database.execute("UPDATE engagement_events SET payload=?", (json.dumps(payload),))
                fixture.database.commit()
            elif name == "wrong_hash":
                fixture.output_mode = "WRONG_HASH"
            elif name == "ai_hold":
                fixture.output_mode = "HOLD"
            else:
                fixture.after_ai = lambda: fixture.event("inbound")
            fixture.run(True)
            if name == "fresh_reply":
                # A correspondent message no longer fails the thread shape (owner, 2026-09-30:
                # "Remove 'the thread must contain only our own messages'"); stop-on-reply still
                # holds the send: the inspection observes the reply and refuses (HAS_REPLY_PRESENT).
                require(not fixture.sends, name)
                states = [row[0] for row in fixture.database.execute("SELECT state FROM outreach_actions")]
                require(all(state in {"PENDING", "BLOCKED"} for state in states), name)
            else:
                require(not fixture.sends and fixture.database.execute("SELECT COUNT(*) FROM outreach_actions").fetchone()[0] == 0, name)
        elif name == "capacity":
            with fixture.database:
                workflow_tasks.reserve_compute(fixture.database, "synthetic-full", fixture.now.isoformat(), 10800)
            fixture.run(True)
            fixture.restart()
            fixture.run(True)
            require(not fixture.reads and not fixture.inferences and fixture.database.execute("SELECT SUM(attempts) FROM workflow_tasks").fetchone()[0] == 0, name)
        elif name == "bounded_retry":
            fixture.output_mode = "TIMEOUT"
            fixture.run()
            fixture.now += dt.timedelta(minutes=15)
            fixture.run()
            fixture.now += dt.timedelta(hours=1)
            fixture.run()
            fixture.now += dt.timedelta(hours=1)
            fixture.run()
            task = fixture.database.execute("SELECT * FROM workflow_tasks").fetchone()
            require(task["attempts"] == 3 and task["status"] == "EXHAUSTED" and len(fixture.inferences) == 3, name)
        elif name == "expired_composition_claim":
            def elapsed():
                fixture.now += dt.timedelta(minutes=10)
            fixture.after_ai = elapsed
            with patch("followup_execution._now", side_effect=lambda value=None: fixture.now):
                first = fixture.run()
            require(first["composition"]["items"][0].get("claim_state") == "STALE_OR_CHANGED"
                    and fixture.database.execute("SELECT COUNT(*) FROM outreach_actions").fetchone()[0] == 0,
                    "Expired composer committed a proposal")
            fixture.after_ai = None
            fixture.restart()
            fixture.run()
            task = fixture.database.execute("SELECT * FROM workflow_tasks").fetchone()
            require(task["attempts"] == 2 and task["status"] == "COMPLETE", "Expired read claim failed bounded restart recovery")
        elif name == "adapter_missing":
            del fixture.adapter.followup_contract
            result = fixture.run(True)
            require(result["execution"]["blocker"] == "FOLLOWUP_SHARED_ADAPTER_INTEGRATION_REQUIRED" and not fixture.sends, name)
        elif name == "blocked_candidate_isolation":
            fixture.config["followup_execution"]["max_items"] = 1
            blocked = dict(fixture.candidate, profile="https://www.linkedin.com/in/other-unreconciled-person")
            result = fixture.run(candidates=[blocked, fixture.candidate])
            require(len(fixture.inferences) == 1 and result["composition"]["items"][-1]["state"] == "PENDING", "Unready candidate starved due work")
        elif name == "judgment_context":
            fixture.run()
            action = json.loads(fixture.database.execute("SELECT payload FROM outreach_actions").fetchone()[0])
            context = followup.judgment_context(fixture.database, action)
            require(context["followup_decision"]["message"] == action["message"] and context["followup_context"]["candidate_resume"] == RESUME, name)
            observation = fixture.collect(action, "INSPECTION")
            before = copy.deepcopy(observation)
            trimmed = followup.judgment_observation(action, observation)
            require(observation == before and len(trimmed["observations"][0]["raw"]["messages"]) <= 8, "Judge trimming mutated source")
        elif name in {"provider_inspection", "provider_judge_blocked", "provider_restricted"}:
            fixture = replace_fixture(fixture, ProviderFixture())
            fixture.run()
            fixture.config["followup_execution"]["dispatch_enabled"] = True
            action = json.loads(fixture.database.execute("SELECT payload FROM outreach_actions").fetchone()[0])
            if name == "provider_judge_blocked":
                fixture.judge_decision = "BLOCKED"
            elif name == "provider_restricted":
                fixture.room["linkedInAccount"]["messagingRestricted"] = True
            with patch("outreach_provider._now", side_effect=lambda: fixture.now.isoformat()):
                proof = fixture.adapter.inspect(action, fixture.config)
            require("direct_messages_complete" not in proof and "sent_invitations_complete" not in proof
                and "entitlement_verified" not in proof, "Cold-contact clearance fabricated")
            require(proof["quota_remaining"] is None and proof["quota_basis"] == "UNKNOWN_PROVIDER_ENFORCED", name)
            if name == "provider_inspection":
                require(proof["followup_thread_unchanged"] and not followup.proof_gates(fixture.database, fixture.config, action, proof, fixture.now), name)
            elif name == "provider_judge_blocked":
                require(proof["agent_decision"] == "BLOCKED", name)
            else:
                require(proof["provider_restriction_observed"] and followup.proof_gates(fixture.database, fixture.config, action, proof, fixture.now), name)
        elif name in {"replay", "uncertain", "second_threshold", "max_two", "final_reply", "final_closure", "multiple_companies", "alias_closure"}:
            if name == "alias_closure":
                research_workflow.register_alias(fixture.database, "contact", "https://www.linkedin.com/in/other-alias", PROFILE, "synthetic verified alias")
                engagement.close_outreach(fixture.database, "https://www.linkedin.com/in/other-alias", "synthetic closure", fixture.now)
                fixture.run(True)
                require(not fixture.sends and not fixture.inferences, name)
                return
            if name == "uncertain":
                fixture.lost_ack, fixture.receipt_visible = True, False
            if name in {"final_reply", "final_closure"}:
                fixture.before_send = (lambda: fixture.room["messages"].append({"body": "No thanks", "sender": "CORRESPONDENT", "createdAt": fixture.now.isoformat()})) if name == "final_reply" else lambda: engagement.close_outreach(fixture.database, PROFILE, "close before transport", fixture.now)
            fixture.run(True)
            if name in {"final_reply", "final_closure"}:
                require(not fixture.sends, name)
                return
            require(len(fixture.sends) == 1, "Expected first guarded send: " + name)
            fixture.restart()
            fixture.run(True)
            fixture.run(True)
            require(len(fixture.sends) == 1 and len(fixture.inferences) == 1, "Replay repeated composition/send")
            if name == "uncertain":
                require(not any(event["kind"] == "followup_sent" for event in engagement.events_for(fixture.database, PROFILE)), "Acknowledgement became delivery")
            elif name in {"second_threshold", "max_two"}:
                fixture.now = engagement.business_days_after(DUE, 5) - dt.timedelta(microseconds=1)
                fixture.candidate["verification"]["verified_at"] = fixture.now.isoformat()
                fixture.run(True)
                require(len(fixture.sends) == 1, "Second sent before five business days")
                fixture.now += dt.timedelta(microseconds=1)
                fixture.run(True)
                require(len(fixture.sends) == 2, "Second not sent when due")
                fixture.run(True)
                fixture.now += dt.timedelta(days=10)
                fixture.run(True)
                require(len(fixture.sends) == 2, "Third follow-up sent")
            elif name == "multiple_companies":
                candidate = copy.deepcopy(fixture.candidate)
                candidate["company"] = "Another Synthetic Company"
                fixture.run(True, [fixture.candidate, candidate])
                require(len(fixture.sends) == 1, "Company change reset lifecycle")
        else:
            raise AssertionError("Unknown manual scenario")
    finally:
        fixture.close()


def replace_fixture(previous, replacement):
    previous.close()
    return replacement


SCENARIOS = (
    "before_due", "invitation", "acceptance", "missing_invitation", "invitation_not_in_thread",
    "reply", "opt_out", "closure", "excluded", "no_automatic", "suppression", "unknown_outbound", "urgent",
    "wrong_account", "missing_thread", "fresh_reply", "unreconciled_outbound", "partial_thread", "raw_mismatch",
    "wrong_hash", "ai_hold", "changed_during_ai", "capacity", "bounded_retry", "adapter_missing",
    "replay", "uncertain", "second_threshold", "max_two", "final_reply", "final_closure", "multiple_companies", "alias_closure", "judgment_context",
    "provider_inspection", "provider_judge_blocked", "provider_restricted", "blocked_candidate_isolation", "expired_composition_claim",
)


PROVIDER_SCENARIOS = (
    ("provider_confirmed", {}),
    ("provider_lost_ack", {"lost_ack": True}),
    ("provider_final_inbound", {"late_change": "reply"}),
    ("provider_final_closure", {"late_change": "closure"}),
    ("provider_uncertain", {"lost_ack": True, "receipt_visible": False}),
)


CAMPAIGN_SCENARIOS = ("confirmed", "invitation_in_thread", "second_old_context", "pending_then_accepted", "acceptance_read_failures_keep_polling",
                      "before_due", "unaccepted", "unconfirmed", "no_thread", "no_automatic", "closure")


def main():
    failures = []
    scenarios = () if "--provider-e2e" in sys.argv else SCENARIOS
    with offline():
        for name in scenarios:
            try:
                scenario(name)
                print(json.dumps({"scenario": name, "result": "PASS"}), flush=True)
            except Exception as error:
                failures.append(name)
                print(json.dumps({"scenario": name, "result": "FAIL", "error": str(error), "type": type(error).__name__}), flush=True)
        for name, arguments in PROVIDER_SCENARIOS:
            try:
                result = provider_e2e(**arguments)
                print(json.dumps({"scenario": name, "result": "PASS", "artifact": result}), flush=True)
            except Exception as error:
                failures.append(name)
                print(json.dumps({"scenario": name, "result": "FAIL", "error": str(error), "type": type(error).__name__}), flush=True)
        for name in CAMPAIGN_SCENARIOS:
            try:
                result = campaign_followup_e2e(name)
                print(json.dumps({"scenario": "campaign_" + name, "result": "PASS", "artifact": result}), flush=True)
            except Exception as error:
                failures.append("campaign_" + name)
                print(json.dumps({"scenario": "campaign_" + name, "result": "FAIL", "error": str(error), "type": type(error).__name__}), flush=True)
    print(json.dumps({"manual_first": True, "scenarios": len(scenarios) + len(PROVIDER_SCENARIOS) + len(CAMPAIGN_SCENARIOS), "failures": failures,
                      "live_sends": 0, "scope": "mocked provider I/O and AI; real shared guard, provider transport and receipt handling; not scheduled/live verification"}))
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
