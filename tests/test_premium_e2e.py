"""Owner pick P8 (2026-10-27): a not-Premium invitation's note, queued after acceptance, goes through
outreach.cycle like any first message: gates, the Review hold, the send over the port, initial_sent
FIRST_MESSAGE, and then the follow-up plan. FakePort, a scripted judge, a frozen clock; no network."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import datetime as dt
import hashlib
import json

import approvals
import engagement
import outreach
import sequence
import setup_steps
from orchestrator import profile_key
import test_guardrails_unipile as harness

SLUG = "note-person"
NOTE = "Hi, I read your post on data contracts. Would you be open to a short chat?"


class DeferredNoteThroughTheCycle(harness.MessageBase):
    PURPOSE = "FIRST_MESSAGE"
    SLUG = SLUG

    def setUp(self):
        super().setUp()
        setup_steps.update_state(lambda state: state.pop(setup_steps.PREMIUM_KEY, None))
        self.addCleanup(lambda: setup_steps.update_state(lambda state: state.pop(setup_steps.PREMIUM_KEY, None)))
        self.port.premium = False
        self.person = self.port.add_person(harness.profile(SLUG), distance=2)
        payload = {"action_id": "invite-1", "purpose": "INVITE", "provider": "unipile", "channel": "linkedin",
                   "profile": harness.profile(SLUG), "recipient": "Note Person", "company": harness.COMPANY,
                   "provider_id": self.person, "note": NOTE, "message": NOTE,
                   "verification": {"content_verified": True, "content_sources": ["test"], "identity_verified": True,
                                    "identity_source": "test", "team_relevance_verified": True, "team_source": "test",
                                    "verified_at": self.clock.now.isoformat()}}
        text = json.dumps(payload, sort_keys=True)
        self.database.execute("INSERT INTO outreach_actions(action_id,identity,slot,payload,payload_hash,state) VALUES (?,?,?,?,?,?)",
                              ("invite-1", profile_key(harness.profile(SLUG)), "INVITE", text,
                               hashlib.sha256(text.encode()).hexdigest(), "REQUEST_STARTED"))
        self.database.commit()
        sequence.send_invite(self.database, self.port, "invite-1")
        sequence.reconcile_invite(self.database, self.port, "invite-1")
        approvals.set_mode(self.database, approvals.REVIEW)
        self.addCleanup(lambda: approvals.set_mode(self.database, approvals.AUTOMATIC))

    def test_the_note_is_held_approved_sent_and_starts_the_follow_up_clock(self):
        self.assertIsNone(self.port.invitations[-1]["note"])
        self.port.accept(self.person)
        self.execute()                                    # acceptance read, note queued, judged, held
        self.assertEqual(self.state(), approvals.AWAITING)
        self.assertEqual(json.loads(self.rows()[0]["payload"])["message"], NOTE)
        self.assertEqual(self.writes("start_chat"), 0)
        approvals.approve(self.database, self.rows()[0]["action_id"], shown_payload_hash=self.rows()[0]["payload_hash"])
        self.messages_hourly(4)
        self.assertEqual(self.state(), "CONFIRMED_SENT")
        self.assertEqual((self.writes("start_chat"), self.ours()), (1, [NOTE]))
        notes = [event for event in engagement.events_for(self.database, harness.profile(SLUG))
                 if event["kind"] == "initial_sent" and event.get("purpose") == "FIRST_MESSAGE"]
        self.assertEqual(len(notes), 1)
        planned = engagement.next_action(self.database, harness.profile(SLUG), self.clock.now)
        self.assertEqual(planned["purpose"], "FOLLOWUP")
        self.assertEqual(planned["due_at"], engagement.business_days_after(notes[0]["occurred_at"], 3).isoformat())
