"""E3.T3 piece 2A: with linkedin_provider "unipile" the invitation-note path is one INVITE action, claimed
and gated as today's new-contact send, sent and reconciled through the port (FakePort)."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import engagement
import orchestrator
import outreach
import outreach_agent
import outreach_promotion as promotion
import research_workflow as research
from linkedin_port import FakePort


ROOT = Path(__file__).resolve().parents[1]
PROFILE = "https://www.linkedin.com/in/mock-invitee"
IDENTITY = outreach.profile_key(PROFILE)


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def evidence(field, value, at):
    return {"field": field, "value": value, "source": "https://example.test/jobs/123",
            "observed_at": at, "verified": True, "excerpt": "MOCK fixture only", "kind": "official"}


class InvitePathTests(unittest.TestCase):
    def setUp(self):
        # Pacing has its own tests (test_pacing); here it always says go, whatever the wall clock.
        self.enterContext(patch("pacing.check", return_value={"go": True}))
        # Every cycle step that builds a port gets the fake one (never real settings).
        self.enterContext(patch("orchestrator.linkedin_port_for", side_effect=lambda config: self.port))
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = sqlite3.connect(Path(self.directory.name) / "fixture.sqlite")
        self.addCleanup(lambda: self.database.close())
        self.database.row_factory = sqlite3.Row
        at = now()
        research.initialize(self.database)
        outreach.initialize_actions(self.database)
        research.seed_history(self.database, {"companies": []}, history_complete=True)
        research.register_claims(self.database, [{"text": "Built reliable APIs", "tags": ["backend"]}],
                                 designated_source_text="Built reliable APIs", source_sha256="a" * 64,
                                 designated_sha256="a" * 64)
        research.plan_research(self.database, "plan", ["Mock Company"], now=at)
        person = {"recipient": "Mock Invitee", "profile": PROFILE, "employer_matches_target": True, "evidence": [
            evidence("identity", PROFILE, at), evidence("employment", "Mock Company", at),
            evidence("team", "API team", at), evidence("ability_to_help", "Runs team hiring", at),
            evidence("contact_type", "hiring_manager", at)]}
        role = {"company": "Mock Company", "role": "Staff Software Engineer", "url": "https://example.test/jobs/123",
                "requisition_id": "123", "rationale": "MOCK only", "people": [person],
                "evidence": [evidence("team_need", "reliable APIs", at), evidence("specialization", "backend", at)]}
        self.draft = research.process_research(self.database, {"research_status": "COMPLETE", "summary": "Mock",
            "blockers": [], "opportunities": [role], "drafts": []}, plan_id="plan", now=at)["drafts"][0]
        self.config = {"sender_id": 900001, "sender_profile": "https://www.linkedin.com/in/mock-sender",
                       "linkedin_provider": "unipile", "outreach_actions_enabled": True, "outreach_mode": "guarded",
                       "outreach_allowed_purposes": ["INITIAL", "FIRST_MESSAGE", "INVITE"],
                       "outreach_history_max_age_seconds": 3600,
                       "outreach_agent": {"enabled": True, "max_items": 2}}
        self.database.commit()
        self.port = FakePort()
        self.provider_id = self.port.add_person(PROFILE)
        self.client = Mock(spec=["call", "_request"])

    def proof(self):
        return {"identity": IDENTITY, "sender_id": 900001, "checked_at": now(), "evidence": "mock:authenticated-history",
                "sender_ready": True, "entitlement_verified": True, "direct_messages_complete": True,
                "sent_invitations_complete": True, "pending_invitation": False, "cooldown": False,
                "prior_outreach": False, "has_reply": False, "negative_reply": False, "opt_out": False,
                "conversation_id": None, "route": "INVITATION", "company_contact_count": 0,
                "company_count_evidence": "mock:company-history", "followups_sent": 0, "initial_message_sent": False,
                "agent_decision": "READY", "provider_snapshot_complete": True, "next_action": "Use the shared action guard"}

    def inspected(self):
        return patch.object(outreach_agent.Adapter, "inspect", side_effect=lambda action, config: self.proof())

    def promote(self):
        with patch.object(orchestrator, "HeyReach", return_value=self.client), self.inspected(), \
                patch("outreach_campaign.prepare_candidates", side_effect=AssertionError("No HeyReach campaign")):
            return orchestrator.draft_promotion_cycle(self.database, self.config, port=self.port)

    def execute(self):
        with patch.object(orchestrator, "HeyReach", return_value=self.client), self.inspected():
            return orchestrator.new_contact_execution_cycle(ROOT, self.database, self.config, port=self.port)

    def invite(self):
        row = self.database.execute("SELECT * FROM outreach_actions").fetchone()
        return row, json.loads(row["payload"])

    def kinds(self):
        return [event["kind"] for event in engagement.events_for(self.database, IDENTITY)]

    def test_promoted_contact_becomes_invite_claimed_sent_once_and_confirmed(self):
        self.assertEqual(self.promote()["queued"], 1)
        row, action = self.invite()
        self.assertEqual((action["purpose"], action["provider"], action["channel"], row["slot"]),
                         ("INVITE", "unipile", "linkedin", "INVITE"))
        self.assertEqual((action["provider_id"], action["note"], action["message"]),
                         (self.provider_id, self.draft["invitation"], self.draft["invitation"]))
        self.assertTrue(action["verification"]["identity_verified"])
        first = self.execute()
        self.assertEqual(first["actions"]["INVITE"]["attempts"], 1)
        self.assertEqual(self.invite()[0]["state"], "ACKNOWLEDGED_DELIVERY_UNCONFIRMED")
        claim = self.database.execute("SELECT * FROM outreach_action_claims").fetchone()
        self.assertEqual((claim["action_id"], claim["sender_id"]), (action["action_id"], 900001))
        self.assertEqual(self.kinds(), [])  # an acknowledgement is not delivery
        second = self.execute()
        self.assertEqual(second["invites"]["items"], [{"action_id": action["action_id"], "state": "CONFIRMED_SENT"}])
        self.assertEqual(self.kinds(), ["initial_sent"])
        self.execute()
        self.assertEqual(self.port.calls.count("send_invitation"), 1)
        self.assertEqual(self.port.invitations[0]["note"], self.draft["invitation"])

    def test_existing_claim_still_refuses_the_invite(self):
        self.promote()
        self.database.execute("INSERT INTO outreach_action_claims VALUES (?,?,?,?,?,?,?)",
                              ("other-action", "{}", "x", IDENTITY, 900001, "{}", now()))
        self.database.commit()
        item = self.execute()["actions"]["INVITE"]["items"][0]
        self.assertEqual(item["state"], "BLOCKED")
        self.assertIn("IMMUTABLE_OUTREACH_CLAIM_EXISTS", item["gates"])
        self.assertNotIn("send_invitation", self.port.calls)

    def test_uncertain_invite_is_retried_through_the_cycle_at_most_three_sends(self):
        self.promote()
        self.port.fail("send_invitation", "timeout", times=5)
        states = []
        for unused in range(5):
            self.execute()
            states.append(self.invite()[0]["state"])
        self.assertEqual(states, ["INVITE_RETRY_WAIT"] * 3 + ["CONFIRMED_NOT_SENT"] * 2)
        self.assertEqual(self.port.calls.count("send_invitation"), 3)
        self.assertEqual(self.kinds(), [])

    def test_a_cycle_without_the_port_never_claims_an_invite(self):
        """Review 2A round 1: the hourly guarded-message step (no port, no purpose filter) runs first."""
        self.promote()
        with patch.object(orchestrator, "HeyReach", return_value=self.client), self.inspected():
            orchestrator.extended_outreach_cycle(self.database, self.config)
        self.assertEqual(self.invite()[0]["state"], "PENDING")
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM outreach_action_claims").fetchone()[0], 0)
        self.execute()
        self.assertEqual(self.port.calls.count("send_invitation"), 1)

    def test_pacing_wait_leaves_the_invite_queued_unclaimed(self):
        self.promote()
        with patch("pacing.check", return_value={"go": False, "reason": "DAILY_CAP", "wait_until": "2026-10-18T16:00:00+00:00"}):
            self.execute()
        self.assertEqual(self.invite()[0]["state"], "PENDING")
        self.assertEqual(self.port.calls.count("send_invitation"), 0)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM outreach_action_claims").fetchone()[0], 0)

    def test_real_draft_and_send_inspections_read_the_port_not_heyreach(self):
        """E3.T3 remainder: with no inspection patched, both pre-send inspections (the draft worker's and
        the one at claim time) read the person through the port and the judge sees that read."""
        observations = []

        def judge(config, prompt, schema, timeout):
            packet = json.loads(prompt.split("UNTRUSTED_SOURCE_JSON:\n", 1)[1])
            observations.append(packet["observation"])
            return json.dumps({"output_version": outreach_agent.VERSION, "source_sha256": packet["source_sha256"],
                               "decision": "READY", "reason": "Synthetic.", "next_action": "Use the guard.",
                               "findings": {"prior_outreach": False, "has_reply": False, "negative_reply": False,
                                            "opt_out": False},
                               "conversation_id": None, "route": "INVITATION",
                               "receipt_observation": None, "receipt_message": None})

        config = dict(self.config, model="synthetic", codex="NEVER", timezone="UTC", daily_compute_seconds=10800)
        self.port.own_profile = config["sender_profile"]
        with patch("reply_worker.execute_text", side_effect=judge), patch.object(orchestrator, "HeyReach", return_value=self.client):
            self.assertEqual(orchestrator.draft_promotion_cycle(self.database, config, port=self.port)["queued"], 1)
            orchestrator.new_contact_execution_cycle(ROOT, self.database, config, port=self.port)
        self.assertEqual([observation["route"] for observation in observations], ["unipile", "unipile"])
        self.assertEqual(self.port.calls.count("send_invitation"), 1)
        self.client.call.assert_not_called()

    def test_connected_contact_gets_the_initial_in_its_chat_not_an_invite(self):
        """A 1st-degree contact on Unipile: the judge sees network_distance 1 and the chat, picks the
        existing-thread route, and the INITIAL note is sent into that chat over the port."""
        self.port.accept(self.provider_id)
        chat = self.port.add_chat(self.provider_id, [])

        def judge(config, prompt, schema, timeout):
            packet = json.loads(prompt.split("UNTRUSTED_SOURCE_JSON:\n", 1)[1])
            connected = packet["observation"]["person"]["network_distance"] == 1
            return json.dumps({"output_version": outreach_agent.VERSION, "source_sha256": packet["source_sha256"],
                               "decision": "READY", "reason": "Synthetic.", "next_action": "Use the guard.",
                               "findings": {"prior_outreach": False, "has_reply": False, "negative_reply": False,
                                            "opt_out": False},
                               "conversation_id": chat if connected else None,
                               "route": "EXISTING_THREAD" if connected else "INVITATION",
                               "receipt_observation": None, "receipt_message": None})

        config = dict(self.config, model="synthetic", codex="NEVER", timezone="UTC", daily_compute_seconds=10800)
        self.port.own_profile = config["sender_profile"]
        with patch("reply_worker.execute_text", side_effect=judge), patch.object(orchestrator, "HeyReach", return_value=self.client):
            self.assertEqual(orchestrator.draft_promotion_cycle(self.database, config, port=self.port)["queued"], 1)
            row, action = self.invite()
            self.assertEqual((action["purpose"], action["provider"], action["conversation_id"]), ("INITIAL", "unipile", chat))
            orchestrator.new_contact_execution_cycle(ROOT, self.database, config, port=self.port)
        self.assertEqual((self.port.calls.count("send_message"), self.port.calls.count("send_invitation")), (1, 0))
        self.assertEqual(self.port.chat_log[chat]["messages"][-1]["text"], self.draft["first_message"])
        self.client.call.assert_not_called()

    def test_heyreach_mode_never_queues_an_invite(self):
        config = dict(self.config)
        del config["linkedin_provider"]
        context = {"purpose": "INVITE", "proof": self.proof(), "provider_id": self.provider_id}
        result = promotion.cycle(self.database, dict(config, outreach_promotion_contexts={IDENTITY: context}), now())
        self.assertEqual(result["items"][0]["gates"], ["OUTREACH_PURPOSE_UNPROVEN"])
        self.assertIsNone(self.database.execute("SELECT * FROM outreach_actions").fetchone())


if __name__ == "__main__":
    unittest.main()
