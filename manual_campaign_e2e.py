"""Offline manual invitation acceptance: real queues/claims, mocked provider only."""

import copy
import datetime as dt
import json
from pathlib import Path
import sqlite3
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "tests"))

import orchestrator
import engagement
import outreach
import outreach_campaign as campaign
import outreach_promotion
import research_workflow as research
from test_outreach_promotion import NOW, PROFILE, PromotionTests


class Fixture:
    def __init__(self, failure=None, applied=True):
        self.seed = PromotionTests()
        self.seed.setUp()
        self.seed.history_config()
        self.database = self.seed.database
        self.config = dict(self.seed.config, outreach_allowed_purposes=["PREPARE_CAMPAIGN"],
                           pipeline={"companies": [], "rules": {"max_unique_contacts_per_company": 3}})
        outreach.initialize(self.database)
        self.database.executescript("""
            CREATE TABLE IF NOT EXISTS suppressions(identity TEXT PRIMARY KEY,reason TEXT);
            CREATE TABLE IF NOT EXISTS history(identity TEXT PRIMARY KEY,recipient TEXT,company TEXT,profile TEXT,raw TEXT);
        """)
        self.now = dt.datetime.fromisoformat(NOW)
        row = self.database.execute("SELECT * FROM rw_drafts").fetchone()
        draft, identity, verification = outreach_promotion._validate(self.database, row, self.now)
        self.action = {"action_id": "research-outreach:" + research.digest([identity, "PREPARE_CAMPAIGN"]),
            "profile": identity, "recipient": draft["recipient"], "company": draft["company"],
            "purpose": "PREPARE_CAMPAIGN", "message": draft["invitation"], "channel": "linkedin",
            "provider": "heyreach", "verification": verification, "source": outreach_promotion._source(row)}
        self.action["sequence"] = campaign.invitation_sequence({"nodeType": "CONNECTION_REQUEST"}, draft["invitation"])
        self.proof = dict(self.seed.history_proof(), route="INVITATION", conversation_id=None,
                          sequence=self.action["sequence"], sequence_source="mock:observed-provider-sequence")
        self.failure, self.applied = failure, applied
        self.lists, self.leads, self.campaigns, self.writes = {}, {}, {}, []
        self.delivered = False

    def close(self):
        self.seed.doCleanups()

    def restart(self):
        self.database.close()
        self.database = sqlite3.connect(self.seed.path)
        self.database.row_factory = sqlite3.Row
        self.seed.database = self.database

    def call(self, operation, arguments):
        if operation == "get_all_lists":
            items = [value for value in self.lists.values() if value["name"] == arguments["keyword"]]
            return {"totalCount": len(items), "items": copy.deepcopy(items)}
        if operation == "get_list_by_id":
            return copy.deepcopy(self.lists[arguments["listId"]])
        if operation == "get_leads_from_list":
            items = self.leads[arguments["listId"]]
            return {"items": copy.deepcopy(items), "totalCount": len(items)}
        if operation == "get_campaign":
            return copy.deepcopy(self.campaigns[arguments["campaignId"]])
        if operation == "get_all_campaigns":
            if "accountIds" in arguments:
                return {"items": [{"id": 900, "name": "mock-observed-example"}], "totalCount": 1}
            items = [value for value in self.campaigns.values() if value["name"] == arguments["keyword"]]
            return {"items": copy.deepcopy(items), "totalCount": len(items)}
        if operation == "get_campaign_sequence":
            return copy.deepcopy(self.action["sequence"])
        if operation == "get_linked_in_account_by_id":
            return {"id": self.config["sender_id"], "authIsValid": True, "isActive": True,
                    "connectionRequestCooldown": False, "connectionNoteCooldown": False}
        if operation == "get_overall_stats":
            return {"overallStats": {"connectionsSent": int(self.delivered), "uniqueLeadsContacted": int(self.delivered),
                                     "messagesSent": 0, "inmailMessagesSent": 0}}
        if operation in {"get_campaigns_for_lead", "get_conversations_v2"}:
            return {"items": [], "totalCount": 0}
        raise AssertionError("Unexpected read " + operation)

    def _request(self, operation, arguments):
        assert not self.database.in_transaction
        if operation in {"create_empty_list", "add_leads_to_list_v2"}:
            intent = self.database.execute("SELECT state,arguments FROM outreach_campaign_intents WHERE action_id=? AND operation=?",
                                           (self.action["action_id"], operation)).fetchone()
            assert intent[0] == "REQUEST_STARTED" and json.loads(intent[1]) == arguments
        elif operation == "create_campaign":
            row = self.database.execute("SELECT state FROM outreach_actions WHERE action_id=?", (self.action["action_id"],)).fetchone()
            assert row[0] == "REQUEST_STARTED"
            assert self.database.execute("SELECT COUNT(*) FROM outreach_action_claims").fetchone()[0] == 1
            assert arguments["linkedInAccountIds"] == [self.config["sender_id"]]
            assert all(arguments[key] is True for key in ("excludeContactedFromOtherCampaigns",
                "excludeContactedFromSenderInOtherCampaign", "excludeHasOtherAccConversations",
                "excludeFromCompanyBlacklist", "excludeFromLeadBlacklist"))
            assert json.loads(arguments["sequenceJson"]) == self.action["sequence"]
        elif operation == "start_campaign":
            assert self.database.execute("SELECT state FROM outreach_attempts WHERE campaign_id=?",
                                          (arguments["campaignId"],)).fetchone()[0] == "DISPATCHING"
        else:
            raise AssertionError("Unexpected write " + operation)
        self.writes.append((operation, copy.deepcopy(arguments)))
        fail = self.failure == operation
        if not fail or self.applied:
            if operation == "create_empty_list":
                self.lists[101] = {"id": 101, "name": arguments["listName"], "listType": "USER_LIST"}
                self.leads[101] = []
            elif operation == "add_leads_to_list_v2":
                self.leads[arguments["listId"]] = copy.deepcopy(arguments["leads"])
            elif operation == "create_campaign":
                self.campaigns[201] = {"id": 201, "name": arguments["name"], "status": "DRAFT",
                    "campaignAccountIds": arguments["linkedInAccountIds"], "startedAt": None,
                    "linkedInUserListId": arguments["linkedInUserListId"], "excludeInOtherCampaigns": True,
                    "excludeHasOtherAccConversations": True, "excludeContactedFromSenderInOtherCampaign": True,
                    "excludeFromCompanyBlacklist": True, "excludeFromLeadBlacklist": True}
            elif operation == "start_campaign":
                self.campaigns[arguments["campaignId"]]["status"] = "IN_PROGRESS"
                self.campaigns[arguments["campaignId"]]["startedAt"] = self.now.isoformat()
        if fail:
            raise TimeoutError("Mock uncertain provider outcome")
        return {"acknowledged": True}

    def start_claimed_campaign(self, database, campaign_id):
        transport = orchestrator.HeyReach.__new__(orchestrator.HeyReach)
        transport.config, transport.call, transport._request = self.config, self.call, self._request
        return transport.start_claimed_campaign(database, campaign_id)

    def prepare(self):
        return campaign.prepare_list(self.database, self.config, self, self.action, self.proof, self.now)

    def prepare_scheduled(self):
        candidate = {"profile": self.action["profile"], "route": "INVITATION", "proof": self.proof,
                     "draft_source": self.action["source"], "draft": self.seed.draft,
                     "verification": self.action["verification"]}
        result = campaign.prepare_candidates(self.database, self.config, self, [candidate], self.now)
        return result["contexts"].get(self.action["profile"], result["items"][0])

    def reconcile(self, action, config):
        return campaign.reconcile_prepared(self.database, config, self, action, config["outreach_claim"], self.now)

    def create_transport(self, operation, arguments):
        assert operation == "create_campaign"
        return campaign.create_claimed(self.database, self.config, self, self.action["action_id"], self.now)

    def create(self, context):
        result = outreach_promotion.cycle(self.database,
            dict(self.config, outreach_promotion_contexts={self.action["profile"]: context}), self.now)
        assert result["queued"] == 1, result
        self.proof = context["proof"]
        adapter = outreach.HeyReachActionAdapter(self.create_transport, lambda action, config: self.proof, self.reconcile)
        first = outreach.cycle(self.database, self.config, adapter, self.now, purpose="PREPARE_CAMPAIGN")
        assert first["attempts"] == 1, first
        self.restart()
        result = outreach.cycle(self.database, self.config, adapter, self.now, purpose="PREPARE_CAMPAIGN")
        if self.failure == "create_campaign" and not self.applied:
            assert self.database.execute("SELECT state FROM outreach_actions").fetchone()[0] == "WRITE_RETRY_WAIT"
            return {"state": "RECONCILIATION_REQUIRED"}
        assert self.database.execute("SELECT state FROM outreach_actions").fetchone()[0] == "PREPARED", result
        return campaign.handoff(self.database, self.config, self, self.action["action_id"], self.proof, self.now)

    def dispatch(self):
        moment = self.now

        class Clock(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                instant = cls.fromisoformat(moment.isoformat())
                return instant if tz else instant.replace(tzinfo=None)

        with patch("outreach.dt.datetime", Clock):
            return outreach.dispatch(self.database, self.config, self.config["pipeline"], self)

    def run_campaign_cycle(self):
        moment = self.now

        class Clock(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                instant = cls.fromisoformat(moment.isoformat())
                return instant if tz else instant.replace(tzinfo=None)

        adapter = outreach.HeyReachActionAdapter(self.create_transport, inspector=lambda action, config: self.proof)
        with patch("outreach.dt.datetime", Clock):
            return campaign.cycle(self.database, self.config, self, adapter, self.now.isoformat())


def scenario(operation=None, applied=True):
    fixture = Fixture(operation, applied)
    try:
        context = fixture.prepare_scheduled()
        fixture.restart()
        replay = fixture.prepare_scheduled()
        assert len(fixture.writes) <= 2
        if operation in {"create_empty_list", "add_leads_to_list_v2"} and not applied:
            assert replay["state"] == context["state"] == "RECONCILIATION_REQUIRED"
            expected = 1 if operation == "create_empty_list" else 2
            assert len(fixture.writes) == expected
            return {"failure": operation, "applied": applied, "state": replay["state"], "writes": expected}
        assert replay["state"] == "READY", replay
        assert campaign.reservation_gates(fixture.database, PROFILE)
        assert not campaign.reservation_gates(fixture.database, PROFILE, fixture.action["action_id"])
        result = fixture.create(context)
        if operation == "create_campaign" and not applied:
            assert result["state"] == "RECONCILIATION_REQUIRED" and len(fixture.writes) == 3
            return {"failure": operation, "applied": applied, "state": result["state"], "writes": 3}
        assert result["state"] == "PENDING_VALIDATION", result
        assert campaign.handoff(fixture.database, fixture.config, fixture,
            fixture.action["action_id"], fixture.proof, fixture.now)["state"] == "ALREADY_QUEUED"
        dispatched = fixture.dispatch()
        assert dispatched["launch_attempts"] == 1, dispatched
        fixture.restart()
        fixture.dispatch()
        assert [entry[0] for entry in fixture.writes] == ["create_empty_list", "add_leads_to_list_v2", "create_campaign", "start_campaign"]
        assert fixture.database.execute("SELECT state FROM outreach_actions").fetchone()[0] == "PREPARED"
        return {"failure": operation, "applied": applied, "state": dispatched["items"][0]["state"],
                "writes": 4, "restart_and_replay": "NO_DUPLICATE", "delivery_confirmed": False}
    finally:
        fixture.close()


def history_gate(field, value):
    fixture = Fixture()
    try:
        fixture.proof[field] = value
        try:
            fixture.prepare()
            raise AssertionError("Expected preparation to fail")
        except ValueError as error:
            assert not fixture.writes
            return {"gate": field, "reason": str(error), "writes": 0}
    finally:
        fixture.close()


def scheduled_delivery_case():
    fixture = Fixture()
    try:
        fixture.create(fixture.prepare_scheduled())
        fixture.now += dt.timedelta(hours=2)
        fixture.proof["checked_at"] = fixture.now.isoformat()
        first = fixture.run_campaign_cycle()
        assert first["dispatch"]["launch_attempts"] == 1, first
        assert first["delivery"][0]["state"] == "UNKNOWN", first
        assert not engagement.events_for(fixture.database, PROFILE)
        fixture.restart()
        fixture.delivered = True
        fixture.now += dt.timedelta(minutes=16)
        second = fixture.run_campaign_cycle()
        assert second["delivery"][0]["state"] == "CONFIRMED_SENT", second
        events = engagement.events_for(fixture.database, PROFILE)
        assert len(events) == 1 and events[0]["kind"] == "initial_sent"
        assert events[0]["occurred_at"] == fixture.now.isoformat()
        assert not campaign.reservation_gates(fixture.database, PROFILE, continuation=True)
        assert campaign.reservation_gates(fixture.database, PROFILE)
        fixture.run_campaign_cycle()
        assert len(fixture.writes) == 4 and len(engagement.events_for(fixture.database, PROFILE)) == 1
        return {"case": "scheduled_refresh_launch_and_delivery", "state": "CONFIRMED_SENT",
                "initial_events": 1, "writes": 4, "replay": "NO_DUPLICATE"}
    finally:
        fixture.close()


def main():
    with patch("socket.socket.connect", side_effect=AssertionError("NO NETWORK")), \
            patch("socket.getaddrinfo", side_effect=AssertionError("NO NETWORK")), \
            patch("subprocess.Popen", side_effect=AssertionError("NO EXECUTOR")):
        cases = [scenario(), *[scenario(operation, applied) for operation in
            ("create_empty_list", "add_leads_to_list_v2") for applied in (False, True)],
            scenario("create_campaign", False), scenario("create_campaign"), scenario("start_campaign")]
        cases.extend(history_gate(field, value) for field, value in
            # direct_messages_complete is no longer a send condition (owner, 2026-09-23: "Unknown is
            # not a block."); its case failed here since then and is removed.
            (("prior_outreach", True), ("pending_invitation", True),
             ("checked_at", "2026-09-01T00:00:00+00:00"), ("agent_decision", "BLOCKED"),
             ("sender_id", 777), ("route", "EXISTING_THREAD")))
        cases.append(scheduled_delivery_case())
    print(json.dumps({"evidence_level": "MOCK_TESTED", "live_writes": 0, "cases": cases}, indent=2))


if __name__ == "__main__":
    main()
