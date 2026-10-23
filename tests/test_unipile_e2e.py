"""RL1 RE1: one real invitation end to end on the shipped config.json, a temporary home and the
local relay in front of a fake Unipile (127.0.0.1 only): connect (the sender is saved), a research
draft, promotion, the INVITE claim, the send, reconcile to CONFIRMED_SENT; then the inbox reads one
inbound reply. The pre-send judge (a paid model call) answers from the relay's own account read."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import datetime as dt
import json
import shutil
import sqlite3
import subprocess
import tempfile
import urllib.request
import zlib
from pathlib import Path
from unittest import mock

import engagement
import orchestrator
import outreach
import outreach_agent
import research_workflow as research
import resources
import setup_steps
import unipile_port
import user_profile
try:
    from relay_optional import LICENSE, MINE, RelayHarness, needs_relay
except ImportError:   # run as tests.test_unipile_e2e from the repo root
    from tests.relay_optional import LICENSE, MINE, RelayHarness, needs_relay


ROOT = Path(__file__).resolve().parents[1]
PROFILE = "https://www.linkedin.com/in/e2e-invitee"
IDENTITY = outreach.profile_key(PROFILE)
OWN = "https://www.linkedin.com/in/owner"   # the fake Unipile's own profile for MINE
REAL_OPEN = urllib.request.OpenerDirector.open


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def local_only(self, request, *args, **named):
    if not str(getattr(request, "full_url", request)).startswith("http://127.0.0.1"):
        raise AssertionError("external call")
    return REAL_OPEN(self, request, *args, **named)


def evidence(field, value, at):
    return {"field": field, "value": value, "source": "https://example.test/jobs/123",
            "observed_at": at, "verified": True, "excerpt": "MOCK fixture only", "kind": "official"}


@needs_relay
class InvitationThroughTheRelay(RelayHarness):
    def setUp(self):
        super().setUp()
        self.home = Path(tempfile.mkdtemp(prefix="sidedoor-e2e-home-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        shutil.copyfile(user_profile.EXAMPLE_PROFILE, self.home / "profile.json")
        (self.home / "license.key").write_text(LICENSE + "\n")
        for patcher in (mock.patch.dict("os.environ", {user_profile.HOME_VARIABLE: str(self.home)}),
                        mock.patch("setup_steps.RELAY_URL", self.base),
                        mock.patch.object(urllib.request.OpenerDirector, "open", local_only),
                        mock.patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
                        mock.patch.object(orchestrator, "HeyReach", side_effect=AssertionError("HeyReach built")),
                        mock.patch("pacing.check", return_value={"go": True}),
                        mock.patch.object(outreach_agent.Adapter, "inspect", side_effect=self.judge)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.set_response("GET /users/e2e-invitee", (200, {"provider_id": "INVITEE1", "network_distance": "SECOND_DEGREE",
                                                           "invitation": None, "public_identifier": "e2e-invitee"}))
        self.set_response("GET /users/invite/sent", (200, {"items": [], "cursor": None}))
        self.set_response("GET /chats", (200, {"items": [], "cursor": None}))
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = sqlite3.connect(Path(directory.name) / "e2e.sqlite3")
        self.database.row_factory = sqlite3.Row
        self.addCleanup(self.database.close)
        at = now()
        research.initialize(self.database)
        outreach.initialize_actions(self.database)
        engagement.initialize(self.database)
        research.seed_history(self.database, {"companies": []}, history_complete=True)
        research.register_claims(self.database, [{"text": "Built reliable APIs", "tags": ["backend"]}],
                                 designated_source_text="Built reliable APIs", source_sha256="a" * 64,
                                 designated_sha256="a" * 64)
        research.plan_research(self.database, "plan", ["Mock Company"], now=at)
        person = {"recipient": "E2E Invitee", "profile": PROFILE, "employer_matches_target": True, "evidence": [
            evidence("identity", PROFILE, at), evidence("employment", "Mock Company", at),
            evidence("team", "API team", at), evidence("ability_to_help", "Runs team hiring", at),
            evidence("contact_type", "hiring_manager", at)]}
        role = {"company": "Mock Company", "role": "Staff Software Engineer", "url": "https://example.test/jobs/123",
                "requisition_id": "123", "rationale": "MOCK only", "people": [person],
                "evidence": [evidence("team_need", "reliable APIs", at), evidence("specialization", "backend", at)]}
        self.draft = research.process_research(self.database, {"research_status": "COMPLETE", "summary": "Mock",
            "blockers": [], "opportunities": [role], "drafts": []}, plan_id="plan", now=at)["drafts"][0]
        self.database.commit()
        # The shipped config, with the note composer off: composing is a paid model call (codex),
        # and the research draft already carries its note.
        self.config = resources.read_json("config.json")
        self.config["outreach_agent"] = dict(self.config["outreach_agent"], compose=False)

    def judge(self, action, config):
        """The pre-send judge's proof, its sender check made as `Adapter.inspect` makes it: the
        account the relay reports must be the configured sender."""
        account = unipile_port.for_install(config, self.home).account()
        return {"identity": IDENTITY, "sender_id": config["sender_id"], "checked_at": now(),
                "evidence": "mock:authenticated-history",
                "sender_ready": account.valid is True
                    and outreach.profile_key(account.own_profile) == outreach.profile_key(config["sender_profile"]),
                "entitlement_verified": True, "direct_messages_complete": True,
                "sent_invitations_complete": True, "pending_invitation": False, "cooldown": False,
                "prior_outreach": False, "has_reply": False, "negative_reply": False, "opt_out": False,
                "conversation_id": None, "route": "INVITATION", "company_contact_count": 0,
                "company_count_evidence": "mock:company-history", "followups_sent": 0, "initial_message_sent": False,
                "agent_decision": "READY", "provider_snapshot_complete": True, "next_action": "Use the shared action guard"}

    def job_config(self):
        """What a job loads (orchestrator.main): config.json with the home's saved sender."""
        return setup_steps.with_sender(self.config, self.home)

    def invite(self):
        row = self.database.execute("SELECT * FROM outreach_actions").fetchone()
        return row, json.loads(row["payload"])

    def test_one_invitation_and_one_reply_through_the_relay(self):
        # 1. Connect: Setup step 7 confirms LinkedIn and saves the sending identity in the home.
        state = setup_steps.linkedin_status(self.home, self.base)
        self.assertEqual(state["steps"]["linkedin"]["status"], "done")
        saved = setup_steps.saved_sender(self.home)
        self.assertEqual(saved["sender_id"], zlib.crc32(MINE.encode()))
        self.assertEqual(saved["sender_profile"], OWN)
        config = self.job_config()
        self.assertEqual((config["sender_id"], config["sender_profile"]), (saved["sender_id"], OWN))

        # 2-3. The research draft is promoted to one INVITE action.
        promoted = orchestrator.draft_promotion_cycle(self.database, config)
        self.assertEqual(promoted["queued"], 1, promoted)
        row, action = self.invite()
        self.assertEqual((action["purpose"], action["provider"], action["provider_id"]), ("INVITE", "unipile", "INVITEE1"))

        # 4-5. The claim (under the saved sender) and the one send through the relay.
        first = orchestrator.new_contact_execution_cycle(ROOT, self.database, config)
        self.assertEqual(first["actions"]["INVITE"]["attempts"], 1, first)
        claim = self.database.execute("SELECT sender_id FROM outreach_action_claims").fetchone()
        self.assertEqual(claim["sender_id"], saved["sender_id"])
        sends = [request for request in self.upstream.requests if (request["method"], request["path"]) ==
                 ("POST", "/api/v1/users/invite")]
        self.assertEqual(len(sends), 1)
        body = json.loads(sends[0]["raw"])
        self.assertEqual((body["provider_id"], body["account_id"], body["message"]),
                         ("INVITEE1", MINE, self.draft["invitation"]))

        # 6. LinkedIn now lists the invitation: the next pass reconciles it to CONFIRMED_SENT.
        self.set_response("GET /users/e2e-invitee", (200, {"provider_id": "INVITEE1", "network_distance": "SECOND_DEGREE",
                                                           "invitation": {"type": "SENT", "status": "PENDING"},
                                                           "public_identifier": "e2e-invitee"}))
        second = orchestrator.new_contact_execution_cycle(ROOT, self.database, config)
        self.assertEqual(second["invites"]["items"], [{"action_id": action["action_id"], "state": "CONFIRMED_SENT"}])
        self.assertEqual([event["kind"] for event in engagement.events_for(self.database, IDENTITY)], ["initial_sent"])
        self.assertEqual(len([request for request in self.upstream.requests
                              if (request["method"], request["path"]) == ("POST", "/api/v1/users/invite")]), 1)

        # 7. The inbox reads the invitee's reply through the relay.
        self.set_response("GET /chats", (200, {"items": [{"id": "c9", "account_id": MINE, "attendee_provider_id": "INVITEE1"}], "cursor": None}))
        self.set_response("GET /chats/c9", (200, {"object": "Chat", "id": "c9", "account_id": MINE}))
        self.set_response("GET /chats/c9/messages", (200, {"items": [
            {"id": "m9", "is_sender": 0, "text": "Thanks for reaching out, happy to talk.",
             "timestamp": now()}], "cursor": None}))
        inbox = orchestrator.unipile_engagement_cycle(self.database, config)
        self.assertEqual(inbox["items"][0].get("inserted"), 1, inbox)
        inbound = [event for event in engagement.events_for(self.database, IDENTITY) if event["kind"] == "inbound"]
        self.assertEqual(len(inbound), 1)
        self.assertIn("Thanks for reaching out, happy to talk.", json.dumps(inbound[0]))
        # Nothing reached Unipile with the master key; every call carried the relay's own key.
        self.assertEqual({request["headers"].get("x-api-key") for request in self.upstream.requests}, {"unipile-secret-KEY-0123456789"})

    def test_without_a_saved_sender_the_passes_hold_with_no_paid_call(self):
        import pipeline_routing
        import reply_worker
        pipeline_routing.initialize(self.database)
        reply_worker.initialize(self.database)
        self.database.commit()
        config = self.job_config()   # LinkedIn never confirmed: nothing saved
        self.assertIsNone(config["sender_id"])
        with mock.patch.object(outreach_agent.Adapter, "inspect", side_effect=AssertionError("paid judge")):
            for step in (lambda: orchestrator.new_contact_execution_cycle(ROOT, self.database, config),
                         lambda: orchestrator.unipile_engagement_cycle(self.database, config),
                         lambda: orchestrator.reply_execution_cycle(ROOT, self.database, config),
                         lambda: orchestrator.followup_execution_cycle(ROOT, self.database, config)):
                with self.assertRaises(unipile_port.SenderNotConfigured) as raised:
                    step()
                self.assertIn("Connect LinkedIn in Setup step 7", raised.exception.next_action)


class RelayRefusedInvitation(InvitationThroughTheRelay):
    """RB6. Reuses the fixture only; the parent's tests are not run again here."""
    test_one_invitation_and_one_reply_through_the_relay = None
    test_without_a_saved_sender_the_passes_hold_with_no_paid_call = None

    def test_a_send_the_relay_refused_is_not_sent_and_spends_no_try(self):
        setup_steps.linkedin_status(self.home, self.base)
        config = self.job_config()
        self.assertEqual(orchestrator.draft_promotion_cycle(self.database, config)["queued"], 1)
        original = unipile_port.UnipilePort.send_invitation

        def lapse_then_send(port, provider_id, note):
            self.checker.valid_keys.discard(LICENSE)   # the license lapses between the read and the send
            return original(port, provider_id, note)
        with mock.patch.object(unipile_port.UnipilePort, "send_invitation", lapse_then_send), \
                mock.patch("license_clock.request_check", return_value=True):
            orchestrator.new_contact_execution_cycle(ROOT, self.database, config)
        row, action = self.invite()
        result = json.loads(row["result"])
        self.assertEqual(row["state"], "INVITE_RETRY_WAIT")
        self.assertEqual(result.get("attempts"), [])
        self.assertEqual(result["relay_refusals"][0]["hold"], "LICENSE_INVALID")
        self.assertNotIn(("POST", "/api/v1/users/invite"),
                         [(request["method"], request["path"]) for request in self.upstream.requests])
        # The license is good again: the next pass reads, finds no invitation and sends (try 1 of 3).
        self.checker.valid_keys.add(LICENSE)
        orchestrator.new_contact_execution_cycle(ROOT, self.database, config)
        row, action = self.invite()
        self.assertEqual(len(json.loads(row["result"])["attempts"]), 1)
        self.assertEqual(row["state"], "ACKNOWLEDGED_DELIVERY_UNCONFIRMED")
