"""Offline manual acceptance, executed before adding new unit tests.

Uses synthetic research records, local contact records (the LinkedIn browser collector is
no longer called, 2026-09-29), mocked HeyReach and a mocked AI decision. It never loads runtime config or calls live tools.
"""

import copy
import datetime as dt
import json
from pathlib import Path
import sqlite3
import sys
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "tests"))

import engagement
import orchestrator
import outreach
import outreach_agent
import outreach_history_browser
import outreach_promotion
import outreach_provider
import research_workflow
from test_outreach_promotion import NOW, PROFILE, PromotionTests, evidence

PRIOR_PROFILE = "https://www.linkedin.com/in/prior-contact"


class Fixture:
    def __init__(self, case="clear"):
        self.seed = PromotionTests()
        self.seed.setUp()
        self.seed.history_config()
        self.database = self.seed.database
        self.config = dict(self.seed.config, model="mock", codex="NEVER", timezone="UTC",
                           outreach_agent={"enabled": True, "timeout_seconds": 1, "max_items": 2})
        self.now = dt.datetime.fromisoformat(NOW)
        self.case = case
        self.messages = []
        self.writes = []
        self.collects = 0
        self.client = Mock()
        self.client.call.side_effect = self.read
        self.client._request.side_effect = self.send
        self.collector = self.collect
        # Prior contact now comes from our own records (owner, 2026-09-29), so these cases seed a
        # real local record instead of a browser finding.
        identity = orchestrator.profile_key(PROFILE)
        with self.database:
            if case == "prior":
                # Contact history is checked once, before a draft is created (owner, 2026-10-01),
                # so the prior contact is a second person who has a local record and no draft yet.
                self.database.execute("CREATE TABLE IF NOT EXISTS history (identity TEXT PRIMARY KEY, recipient TEXT NOT NULL, company TEXT NOT NULL, profile TEXT, evidence TEXT NOT NULL)")
                self.database.execute("INSERT INTO history(identity,recipient,company,profile,evidence) VALUES (?,?,?,?,?)",
                                      (orchestrator.profile_key(PRIOR_PROFILE), "Prior Person", "Mock Company",
                                       PRIOR_PROFILE, "mock prior contact record"))
            if case == "pending":
                self.database.execute("CREATE TABLE IF NOT EXISTS outreach_attempts (identity TEXT PRIMARY KEY, campaign_id INTEGER UNIQUE NOT NULL, state TEXT NOT NULL, claimed_at TEXT NOT NULL, result TEXT)")
                self.database.execute("INSERT INTO outreach_attempts VALUES (?,?,?,?,?)",
                                      (identity, 900900, "CONFIRMED_SENT", NOW, "{}"))

    def close(self):
        self.seed.doCleanups()

    def restart(self):
        self.database.close()
        self.database = sqlite3.connect(self.seed.path)
        self.database.row_factory = sqlite3.Row
        self.seed.database = self.database

    def read(self, operation, arguments):
        account = {"id": self.config["sender_id"], "profileUrl": self.config["sender_profile"],
                   "authIsValid": True, "isActive": True,
                   "connectionRequestCooldown": False, "connectionNoteCooldown": False}
        room = {"id": "mock-thread", "groupChat": False, "linkedInAccountId": account["id"],
                "linkedInAccount": account, "correspondentProfile": {"profileUrl": PROFILE},
                "messages": copy.deepcopy(self.messages), "totalMessages": len(self.messages),
                "blockedByMe": False, "blockedByParticipant": False}
        if operation == "get_linked_in_account_by_id":
            return account
        if operation == "get_chatroom":
            return room
        if operation == "get_conversations_v2":
            return {"items": [], "totalCount": 0} if self.case == "cold" else {"items": [room], "totalCount": 1}
        if operation == "get_lead":
            if self.case == "cold":
                raise orchestrator.LeadNotFoundError("providerLeadNotFound")
            return {"profileUrl": PROFILE, "linkedin_id": "synthetic-lead"}
        return {"items": [], "totalCount": 0}

    def collect(self, request):
        self.collects += 1
        assert request["read_only"] is True and request["profile"] == PROFILE
        proof = self.seed.history_proof(self.now.isoformat())
        proof["entitlement_evidence"] = "mock-only-provider-access"
        if self.case == "prior":
            proof["prior_outreach"] = True
        if self.case == "pending":
            proof["pending_invitation"] = True
        return {"source": proof["evidence"], "authenticated": True,
                "sender_id": request["sender_id"], "profile": request["profile"],
                "observed_at": proof["checked_at"],
                "coverage": {"direct_messages": "ALL_TIME", "sent_invitations": "ALL_TIME",
                             "pending_invitations": "COMPLETE"},
                "raw": {"synthetic": True, "case": self.case, "sent_invitations_index": {"synthetic": True}}, "proof": proof}

    def judge(self, config, prompt, schema, timeout):
        packet = json.loads(prompt.split("UNTRUSTED_SOURCE_JSON:\n", 1)[1])
        source_hash = packet.pop("source_sha256")
        assert outreach_agent.digest(packet) == source_hash
        rooms = [(index, entry) for index, entry in enumerate(packet["observation"]["observations"])
                 if entry["operation"] == "get_chatroom" and entry["raw"].get("messages")]
        sent = packet["phase"] == "RECONCILIATION" and bool(rooms)
        findings = {field: packet["direct_history"].get(field) for field in
                    ("prior_outreach", "has_reply", "negative_reply", "opt_out")}
        result = {"output_version": outreach_agent.VERSION, "source_sha256": source_hash,
            "decision": "SENT" if sent else "BLOCKED" if findings["prior_outreach"] else "READY",
            "reason": "Synthetic source-bound judgment", "next_action": "Use the shared guard",
            "findings": findings, "conversation_id": "mock-thread",
            "receipt_observation": rooms[0][0] if sent else None, "receipt_message": 0 if sent else None}
        if self.case == "cold":
            result.update(route="INVITATION", conversation_id=None)
        return json.dumps(result)

    def prepare(self):
        with patch("outreach_provider._now", return_value=self.now.isoformat()):
            return outreach_agent.prepare_drafts(ROOT, self.database, self.config, self.client,
                self.judge, self.now, history_collector=self.collector)

    def promote(self, prepared):
        return outreach_promotion.cycle(self.database,
            dict(self.config, outreach_promotion_contexts=prepared["contexts"]), self.now)

    def execute(self):
        adapter = outreach_agent.Adapter(ROOT, self.database, self.config, self.client,
                                         self.judge, history_collector=self.collector)
        with patch("outreach_provider._now", return_value=self.now.isoformat()):
            return outreach.cycle(self.database, self.config, adapter, self.now)

    def send(self, operation, arguments):
        assert operation == "send_message"
        assert self.database.execute("SELECT COUNT(*) FROM outreach_action_claims").fetchone()[0] == 1
        row = self.database.execute("SELECT state,result FROM outreach_actions").fetchone()
        assert row["state"] == "REQUEST_STARTED"
        assert json.loads(row["result"])["provider_request"]["arguments"] == arguments
        self.writes.append((operation, arguments))
        if self.case == "ambiguous":
            raise TimeoutError("synthetic lost acknowledgment")
        return {"success": True}


def scenario(case):
    fixture = Fixture(case)
    try:
        prepared = fixture.prepare()
        if case == "cold":
            assert not prepared["contexts"]
            assert len(prepared["campaign_candidates"]) == 1, prepared
            handoff = prepared["campaign_candidates"][0]
            assert handoff["route"] == "INVITATION" and handoff["proof"]["agent_decision"] == "READY"
            assert handoff["draft"]["invitation"] == fixture.seed.draft["invitation"]
            assert handoff["draft_source"]["content_hash"]
            assert not fixture.writes
            fixture.restart()
            assert fixture.prepare()["campaign_candidates"] == [handoff]
            return {"case": case, "state": "READY_FOR_CAMPAIGN_PREPARER", "writes": 0}
        if case == "prior":
            # The one pre-draft check (owner, 2026-10-01): a person with a local record of contact
            # is never drafted, so nothing of theirs reaches promotion or a send.
            person = {"recipient": "Prior Person", "profile": PRIOR_PROFILE, "employer_matches_target": True,
                      "evidence": [evidence("identity", PRIOR_PROFILE), evidence("employment", "Mock Company"),
                                   evidence("team", "API team"), evidence("ability_to_help", "Runs team hiring"),
                                   evidence("contact_type", "hiring_manager")]}
            role = {"company": "Mock Company", "role": "Staff Software Engineer", "url": "https://example.test/jobs/123",
                    "requisition_id": "123", "rationale": "MOCK only", "people": [person],
                    "evidence": [evidence("team_need", "reliable APIs"), evidence("specialization", "backend")]}
            output = research_workflow.process_research(
                fixture.database, {"research_status": "COMPLETE", "summary": "Mock prior", "blockers": [],
                                   "drafts": [], "opportunities": [role]}, plan_id="plan", now=NOW)
            assert not output["drafts"], output
            assert any("HISTORICAL_OR_DUPLICATE_CONTACT" in blocker for blocker in output["action_blockers"]), output
            assert not fixture.database.execute("SELECT 1 FROM rw_drafts WHERE identity=?",
                                                (research_workflow.profile_key(PRIOR_PROFILE),)).fetchone()
            assert not fixture.writes
            return {"case": case, "state": "NOT_DRAFTED", "writes": 0}
        if case == "pending":
            # Our own earlier send attempt must stop the send, before and after a restart.
            assert not prepared["contexts"] and not prepared.get("campaign_candidates"), prepared
            fixture.restart()
            again = fixture.prepare()
            assert not again["contexts"] and not again.get("campaign_candidates"), again
            assert not fixture.promote(prepared)["queued"]
            assert not fixture.writes
            return {"case": case, "state": "SUPPRESSED", "writes": 0, "restart": True}
        assert prepared["items"][0]["outcome"] == "READY", prepared
        assert fixture.promote(prepared)["queued"] == 1
        fixture.execute()
        assert len(fixture.writes) == 1
        assert fixture.database.execute("SELECT state FROM outreach_actions").fetchone()[0] != "CONFIRMED_SENT"
        fixture.restart()
        fixture.execute()
        assert len(fixture.writes) == 1
        fixture.messages.append({"sender": "ME", "body": fixture.writes[0][1]["message"],
                                 "subject": "", "createdAt": fixture.now.isoformat()})
        fixture.now += dt.timedelta(minutes=16)
        fixture.execute()
        assert fixture.database.execute("SELECT state FROM outreach_actions").fetchone()[0] == "CONFIRMED_SENT"
        fixture.execute()
        assert len(fixture.writes) == 1
        return {"case": case, "state": "CONFIRMED_SENT", "writes": 1, "restart": True,
                "receipt": "independent authenticated mocked chatroom", "live_verified": False}
    finally:
        fixture.close()


def browser_sample():
    request = {"profile": PROFILE, "recipient": "Mock Person", "sender_id": 900001,
               "sender_profile": "https://www.linkedin.com/in/mock-sender", "max_reads": 24,
               "aliases": [outreach.profile_key(PROFILE)], "ledger_reconciled": True,
               "read_only": True, "timeout_seconds": 120}
    result = {"version": outreach_history_browser.VERSION, "source_sha256": outreach_history_browser.digest(request),
              "sender_profile": request["sender_profile"], "recipient_profile": PROFILE, "outcome": "CLEAR",
              "reason": "Synthetic full current UI and reconciled ledger", "next_action": "Apply provider guards",
              "coverage": {kind: True for kind in outreach_history_browser.KINDS[2:7]}, "evidence": []}
    trace = []
    for index, kind in enumerate(outreach_history_browser.KINDS[:7]):
        quote = kind + ": " + (request["sender_profile"] if kind == "ACCOUNT" else PROFILE if kind == "RECIPIENT"
                                else "Synthetic complete exact-recipient current UI search")
        result["evidence"].append({"kind": kind, "tool_call_id": None, "quote": quote})
        trace.append({"type": "item.completed", "item": {"id": "item_" + str(index),
                      "type": "mcp_tool_call", "server": "cua_repl", "tool": "js",
                      "arguments": {"code": "await tab.getAXState();"}, "status": "completed", "error": None,
                      "result": {"content": [{"type": "text", "text": quote}]}}})
    return request, result, "\n".join(json.dumps(event) for event in trace).encode()


def browser_scenario():
    request, result, trace = browser_sample()
    proof = outreach_history_browser.normalize(request, result, trace, NOW, "mock:trace")
    assert proof["proof"]["direct_messages_complete"] is True
    assert proof["raw"]["source_excerpts"][0]["tool_call_id"] == "item_0"
    events = [json.loads(line) for line in trace.splitlines()]
    for event in events:
        content = event["item"]["result"]["content"][0]
        content["text"] = "Synthetic page header\n" + "." * 10000 + content["text"] + "." * 10000
    expanded = b"\n".join(json.dumps(event).encode() for event in events)
    bounded = outreach_history_browser.normalize(request, result, expanded, NOW, "mock:trace")
    assert len(outreach_history_browser.encoded(bounded["raw"]["observed_text"]).encode()) < 65536
    for observed, event, evidence in zip(bounded["raw"]["observed_text"], events, result["evidence"]):
        original = event["item"]["result"]["content"][0]["text"]
        # BI: the Sent-invitations read (and later reads, within budget) is included whole
        assert not observed["complete_text_included"] or (
            evidence["kind"] == "SENT_INVITATIONS" and observed["windows"][0]["text"] == original)
        assert all(window["text"] == original[window["start"]:window["end"]] for window in observed["windows"])
        assert any(evidence["quote"] in window["text"] for window in observed["windows"])
    result["coverage"]["MESSAGE_SPAM"] = False
    result["outcome"] = "UNKNOWN"
    proof = outreach_history_browser.normalize(request, result, trace, NOW, "mock:trace")
    assert proof["proof"]["direct_messages_complete"] is False
    assert proof["proof"]["prior_outreach"] is None
    result["evidence"][0]["tool_call_id"] = "invented-id"
    try:
        outreach_history_browser.normalize(request, result, trace, NOW, "mock:trace")
    except ValueError as error:
        assert str(error) == "BROWSER_HISTORY_UNOBSERVED_QUOTE"
    else:
        raise AssertionError("Wrong supplied trace ID accepted")
    return {"case": "browser_trace", "state": "MOCK_TESTED", "unique_quote_resolution": True,
            "partial_folder_held": True, "wrong_trace_id_rejected": True}


def browser_response(request, observed_at):
    unused_request, result, trace = browser_sample()
    result["source_sha256"] = outreach_history_browser.digest(request)
    return outreach_history_browser.normalize(request, result, trace, observed_at, "mock:browser-only-history")


def browser_fields_scenario(route, restriction=None):
    fixture = Fixture("cold" if route == "INVITATION" else "clear")
    try:
        def collect(request):
            fixture.collects += 1
            response = browser_response(request, fixture.now.isoformat())
            assert not {"cooldown", "entitlement_verified", "entitlement_evidence", "company_contact_count",
                        "company_count_evidence"}.intersection(response["proof"])
            return response

        original = fixture.read

        def read(operation, arguments):
            response = original(operation, arguments)
            if restriction == "account" and operation == "get_linked_in_account_by_id":
                response["isLimited"] = True
            if restriction == "thread" and operation == "get_chatroom":
                response["blockedByParticipant"] = True
            if restriction == "missing_thread_status" and operation == "get_chatroom":
                response.pop("blockedByParticipant")
            if restriction == "missing_invitation_status" and operation == "get_linked_in_account_by_id":
                response.pop("connectionNoteCooldown")
            return response

        fixture.collector = collect
        fixture.client.call.side_effect = read
        prepared = fixture.prepare()
        candidates = prepared["campaign_candidates"] if route == "INVITATION" else list(prepared["contexts"].values())
        if restriction:
            assert not candidates, prepared
            assert not fixture.writes
            return {"case": "browser_only_provider_fields", "route": route, "restriction": restriction,
                    "state": "HELD", "gate": prepared["items"][0]["blocker"], "writes": 0}
        assert len(candidates) == 1, prepared
        proof = candidates[0]["proof"]
        assert proof["entitlement_verified"] and proof["cooldown"] is False
        assert proof["company_count_evidence"].startswith("canonical-ledger:")
        assert proof["remaining_quota"] == "UNKNOWN_PROVIDER_ENFORCED" and proof["quota_remaining"] is None
        if route == "EXISTING_THREAD":
            assert fixture.promote(prepared)["queued"] == 1
            fixture.execute()
            assert len(fixture.writes) == 1
        return {"case": "browser_only_provider_fields", "route": route, "state": "READY",
                "quota_basis": proof["quota_basis"], "cooldown_basis": proof["cooldown_basis"],
                "writes": len(fixture.writes), "live_verified": False}
    finally:
        fixture.close()


def browser_control_scenario(control_matches=True):
    request, result, trace = browser_sample()
    events = [json.loads(line) for line in trace.splitlines()]
    events = [event for event in events if event["item"]["id"] != "item_5"]
    result["evidence"] = [entry for entry in result["evidence"] if entry["kind"] != "MESSAGE_OTHER"]
    preview = "Synthetic team announcement"
    control = "Synthetic Control Person"
    observations = [
        "Messaging folder: Other\n" + control + " | Dec 25, 2025 | InMail | " + preview,
        "Search messages: " + control + "\nGlobal results\n" + control + " | Dec 25, 2025 | "
        + (preview if control_matches else "Different conversation preview"),
        "Search messages: Mock Person\nGlobal results\nWe didn't find anything with Mock Person",
    ]
    for index, text in enumerate(observations):
        events.append({"type": "item.completed", "item": {
            "id": "control_" + str(index), "type": "mcp_tool_call", "server": "cua_repl", "tool": "js",
            "arguments": {"code": "await tab.getAXState();"}, "status": "completed", "error": None,
            "result": {"content": [{"type": "text", "text": text}]}}})
        result["evidence"].append({"kind": "MESSAGE_OTHER", "tool_call_id": None, "quote": text})
    if not control_matches:
        result.update(outcome="UNKNOWN", reason="Synthetic AI assessment: Other control preview differs",
                      next_action="Resolve the actual search scope in a later bounded investigation")
        result["coverage"]["MESSAGE_OTHER"] = False
    else:
        result["reason"] = "Synthetic AI assessment: same Other row date and preview in global control; target search empty"
    trace = b"\n".join(json.dumps(event).encode() for event in events)
    response = outreach_history_browser.normalize(request, result, trace, NOW, "mock:control-trace")
    excerpts = [entry for entry in response["raw"]["source_excerpts"] if entry["kind"] == "MESSAGE_OTHER"]
    assert len(excerpts) == 3
    assert [entry["tool_call_id"] for entry in excerpts] == ["control_0", "control_1", "control_2"]
    assert response["proof"]["direct_messages_complete"] is control_matches
    assert response["proof"]["prior_outreach"] is (False if control_matches else None)
    for text in observations:
        assert any(text in window["text"] for entry in response["raw"]["observed_text"] for window in entry["windows"])
    events = [event for event in events if event["item"]["id"] != "control_1"]
    try:
        outreach_history_browser.normalize(request, result, b"\n".join(json.dumps(event).encode() for event in events), NOW, "mock:missing-control")
    except ValueError as error:
        assert str(error) == "BROWSER_HISTORY_QUOTE_ABSENT_OR_AMBIGUOUS"
    else:
        raise AssertionError("Unobserved positive control accepted")
    return {"case": "browser_other_control", "control_matches": control_matches,
            "state": result["outcome"], "source_bound_observations": 3,
            "missing_control_source_rejected": True, "semantic_judgment": "MOCKED_AI", "live_verified": False}


def campaign_scenario(scheduled=False):
    import manual_campaign_e2e
    import outreach_campaign
    provider_class = orchestrator.HeyReach

    class ColdFixture(manual_campaign_e2e.Fixture):
        collects = 0
        missing_lead_reads = 0

        def call(self, operation, arguments):
            if operation == "get_linked_in_account_by_id":
                return dict(super().call(operation, arguments), profileUrl=self.config["sender_profile"])
            if operation == "get_lead":
                if not any(self.leads.values()):
                    self.missing_lead_reads += 1
                    raise orchestrator.LeadNotFoundError("providerLeadNotFound")
                return {"profileUrl": PROFILE, "linkedin_id": "synthetic-cold-lead"}
            if operation == "get_campaigns_for_lead":
                return {"items": list(self.campaigns.values()), "totalCount": len(self.campaigns)}
            if operation in {"get_lists_for_lead", "get_my_network_for_sender"}:
                return {"items": [], "totalCount": 0}
            return super().call(operation, arguments)

        def collect(self, request):
            self.collects += 1
            return browser_response(request, self.now.isoformat())

        def judge(self, config, prompt, schema, timeout):
            packet = json.loads(prompt.split("UNTRUSTED_SOURCE_JSON:\n", 1)[1])
            return json.dumps({"output_version": outreach_agent.VERSION, "source_sha256": packet["source_sha256"],
                "decision": "READY", "reason": "Synthetic clear cold recipient and grounded invitation",
                "next_action": "Prepare the claimed campaign", "route": "INVITATION", "conversation_id": None,
                "receipt_observation": None, "receipt_message": None,
                "findings": {field: False for field in ("prior_outreach", "has_reply", "negative_reply", "opt_out")}})

        def adapter(self):
            return outreach_agent.Adapter(ROOT, self.database, self.config, self, self.judge, history_collector=self.collect)

        def start_claimed_campaign(self, database, campaign_id):
            transport = provider_class.__new__(provider_class)
            transport.config, transport.call, transport._request = self.config, self.call, self._request
            return transport.start_claimed_campaign(database, campaign_id)

    fixture = ColdFixture()
    try:
        fixture.config.update(model="mock", codex="NEVER", timezone="UTC",
                              outreach_allowed_purposes=["INITIAL", "PREPARE_CAMPAIGN"],
                              outreach_agent={"enabled": True, "timeout_seconds": 1, "max_items": 2})
        if scheduled:
            class Clock(dt.datetime):
                @classmethod
                def now(cls, tz=None):
                    instant = cls.fromisoformat(fixture.now.isoformat())
                    return instant if tz else instant.replace(tzinfo=None)

            with patch("orchestrator.HeyReach", return_value=fixture), \
                    patch("outreach_history_browser.Collector", return_value=fixture.collect), \
                    patch("reply_worker._execute", side_effect=fixture.judge), \
                    patch("outreach.dt.datetime", Clock), \
                    patch("orchestrator.utcnow", side_effect=lambda: fixture.now.isoformat()):
                promoted = orchestrator.draft_promotion_cycle(fixture.database, fixture.config)
                assert promoted["queued"] == 1, promoted
                assert fixture.missing_lead_reads > 0
                first = orchestrator.new_contact_execution_cycle(ROOT, fixture.database, fixture.config)
                assert first["actions"]["PREPARE_CAMPAIGN"]["attempts"] == 1, first
                fixture.restart()
                second = orchestrator.new_contact_execution_cycle(ROOT, fixture.database, fixture.config)
                assert fixture.database.execute("SELECT state FROM outreach_actions").fetchone()[0] == "PREPARED", second
                assert second["campaigns"]["dispatch"]["launch_attempts"] == 1, second
                attempt = dict(fixture.database.execute("SELECT identity,state FROM outreach_attempts WHERE campaign_id=201").fetchone())
                assert attempt["state"] == "ACKNOWLEDGED_DELIVERY_UNCONFIRMED", (attempt, second)
                fixture.restart()
                fixture.now += dt.timedelta(minutes=20)
                fixture.delivered = True
                replay = orchestrator.new_contact_execution_cycle(ROOT, fixture.database, fixture.config)
                assert replay["campaigns"]["dispatch"]["launch_attempts"] == 0, replay
                assert fixture.database.execute("SELECT state FROM outreach_attempts WHERE campaign_id=201").fetchone()[0] == "CONFIRMED_SENT", replay
                again = orchestrator.draft_promotion_cycle(fixture.database, fixture.config)
                assert again["queued"] == 0, again
                assert [entry[0] for entry in fixture.writes] == ["create_empty_list", "add_leads_to_list_v2", "create_campaign", "start_campaign"]
                assert fixture.collects == 0, fixture.collects  # no LinkedIn browser read (owner, 2026-09-29)
                return {"case": "scheduled_cold_orchestrator", "state": "CONFIRMED_SENT",
                        "guarded_writes": 4, "collector_calls": fixture.collects,
                        "missing_provider_lead_reads": fixture.missing_lead_reads,
                        "receipt": "independent mocked campaign statistics", "live_verified": False,
                        "restart": "NO_DUPLICATE"}
        with patch("outreach_provider._now", side_effect=lambda: fixture.now.isoformat()):
            investigated = outreach_agent.prepare_drafts(ROOT, fixture.database, fixture.config, fixture,
                fixture.judge, fixture.now, history_collector=fixture.collect)
            assert len(investigated["campaign_candidates"]) == 1, investigated
            prepared = outreach_campaign.prepare_candidates(fixture.database, fixture.config, fixture,
                investigated["campaign_candidates"], fixture.now)
            assert len(prepared["contexts"]) == 1, prepared
            promoted = outreach_promotion.cycle(fixture.database,
                dict(fixture.config, outreach_promotion_contexts=prepared["contexts"]), fixture.now)
            assert promoted["queued"] == 1, promoted
            result = outreach.cycle(fixture.database, fixture.config, fixture.adapter(), fixture.now, purpose="PREPARE_CAMPAIGN")
            assert result["attempts"] == 1, result
            fixture.restart()
            result = outreach.cycle(fixture.database, fixture.config, fixture.adapter(), fixture.now, purpose="PREPARE_CAMPAIGN")
            assert fixture.database.execute("SELECT state FROM outreach_actions").fetchone()[0] == "PREPARED", result
            action = json.loads(fixture.database.execute("SELECT payload FROM outreach_actions").fetchone()[0])
            proof = fixture.adapter().inspect(action, fixture.config)
            handoff = outreach_campaign.handoff(fixture.database, fixture.config, fixture, action["action_id"], proof, fixture.now)
            assert handoff["state"] == "PENDING_VALIDATION", handoff
            sent = fixture.dispatch()
            assert sent["launch_attempts"] == 1, sent
            fixture.restart()
            assert fixture.dispatch()["launch_attempts"] == 0
            assert [entry[0] for entry in fixture.writes] == ["create_empty_list", "add_leads_to_list_v2", "create_campaign", "start_campaign"]
            return {"case": "full_cold_campaign", "state": "ACKNOWLEDGED_DELIVERY_UNCONFIRMED",
                    "guarded_writes": 4, "delivery_confirmed": False, "restart": "NO_DUPLICATE"}
    finally:
        fixture.close()


def lead_identity_scenario():
    fixture = Fixture("cold")
    try:
        provider = outreach_provider.Provider(fixture.database, fixture.config, fixture.client)
        observed = provider.collect({"profile": PROFILE, "purpose": "INITIAL"}, "INSPECTION", new_contact_history=True)
        assert observed["coverage"]["get_lead"]["status"] == "NOT_PRESENT_WITHIN_PROVIDER"
        assert not any(blocker.startswith("get_lead:") for blocker in observed["blockers"])
        assert observed["full_linkedin_history_clearance"] is False
        original = fixture.read
        fixture.client.call.side_effect = lambda operation, arguments: (
            {"profileUrl": "https://www.linkedin.com/in/mock-wrong-person", "linkedin_id": "wrong"}
            if operation == "get_lead" else original(operation, arguments))
        for case in ("cold", "clear"):
            fixture.case = case
            observed = provider.collect({"profile": PROFILE, "purpose": "INITIAL"}, "INSPECTION", new_contact_history=True)
            assert "LEAD_UNVERIFIED_OR_MISMATCHED" in observed["identity_issues"]
            assert not observed["identity_verified"]
        return {"case": "provider_lead_identity", "missing_is_not_history_clearance": True,
                "mismatched_observed_lead_blocks": True, "writes": 0}
    finally:
        fixture.close()


def main():
    with patch("socket.socket.connect", side_effect=AssertionError("NO NETWORK")), \
            patch("socket.getaddrinfo", side_effect=AssertionError("NO NETWORK")), \
            patch("subprocess.Popen", side_effect=AssertionError("NO EXECUTOR")):
        results = [scenario(case) for case in ("clear", "cold", "prior", "pending", "ambiguous")]
        results.append(browser_scenario())
        results.extend(browser_control_scenario(matched) for matched in (True, False))
        results.extend(browser_fields_scenario(route, restriction) for route, restriction in (
            ("INVITATION", None), ("EXISTING_THREAD", None), ("INVITATION", "account"),
            ("EXISTING_THREAD", "account"), ("EXISTING_THREAD", "thread"),
            ("EXISTING_THREAD", "missing_thread_status"), ("INVITATION", "missing_invitation_status")))
        results.append(campaign_scenario())
        results.append(campaign_scenario(scheduled=True))
        results.append(lead_identity_scenario())
    print(json.dumps({"evidence_level": "MOCK_TESTED", "cases": results}, indent=2))


if __name__ == "__main__":
    main()
