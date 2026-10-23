"""E6.T2 option C (owner, 2026-10-22): review mode holds every outreach send before its claim
(AWAITING_APPROVAL, no claim row); Approve, Revise and Approve, Reject are insert-only records in
outreach_owner_records; an approved message goes back through outreach.cycle with only the send judge
skipped. Fakes only: FakePort, injected judges, mocked transports. Nothing is sent anywhere."""

import contextlib
import datetime as dt
import json
import os
import shutil
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME for every test)
import approvals
import champion_reporting
import importlib.util
import manual_campaign_e2e as campaign_fixture
import setup_steps
import test_settings_page as settings_tests
import engagement
import manual_reply_execution as manual_reply
import notifications
import orchestrator
import outreach
import outreach_agent
import research_workflow as research
import test_invite_path as invite_path
import test_outreach
import test_unipile_followups as followups
import test_unipile_inbox as inbox
import test_unipile_presend as presend


ROOT = Path(__file__).resolve().parents[1]


def shown(database, action_id):
    """The payload hash the Approvals page shows with a card (approvals.waiting); every decision
    must hand it back (review fix P8)."""
    return database.execute("SELECT payload_hash FROM outreach_actions WHERE action_id=?", (action_id,)).fetchone()[0]


def note(length, word="note"):
    text = ("Hi, I would like to connect about the platform " + word + " work. ") * 10
    return text[:length - 1] + "."


class HomeMixin:
    def use_home(self):
        self.home = Path(tempfile.mkdtemp(prefix="sidedoor-approvals-home-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        patcher = patch.dict(os.environ, {"SIDEDOOR_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_state(self, text):
        (self.home / approvals.STATE_FILE).write_text(text)


class PortFixture(HomeMixin, presend.Base):
    """The real new-contact adapter (outreach_agent.Adapter) over a FakePort, with a counted judge."""

    def setUp(self):
        self.judged = 0
        super().setUp()
        self.use_home()
        self.enterContext(patch("pacing.check", return_value={"go": True}))
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "fixture.sqlite"
        self.db.close()
        self.reopen()

    def reopen(self):
        """A fresh connection to the same file and a fresh adapter: a restart."""
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        outreach.initialize_actions(self.db)
        self.db.commit()
        self.adapter = outreach_agent.Adapter(ROOT, self.db, self.config, self.client, self.judge, port=self.port)

    def judge(self, config, prompt, schema, timeout):
        self.judged += 1
        return super().judge(config, prompt, schema, timeout)

    def review(self):
        approvals.set_mode(self.db, approvals.REVIEW, home=self.home)

    def queue_invite(self, text):
        action = self.action("INVITE", provider_id=self.person, note=text, message=text)
        outreach.enqueue_action(self.db, action)
        self.db.commit()
        return action

    def cycle(self, purpose="INVITE"):
        return outreach.cycle(self.db, self.config, self.adapter, purpose=purpose)

    def state(self, action_id="invite-1"):
        return self.db.execute("SELECT state FROM outreach_actions WHERE action_id=?", (action_id,)).fetchone()[0]

    def claims(self):
        return self.db.execute("SELECT COUNT(*) FROM outreach_action_claims").fetchone()[0]

    def owner_decision_sources(self, action_id="invite-1"):
        return [json.loads(row[0]).get("decision_source") for row in self.db.execute(
            "SELECT record FROM outreach_agent_audit WHERE action_id=? AND kind='DECISION'", (action_id,))]



class PortReview(PortFixture):
    # Worked example 1
    def test_first_note_is_held_without_a_claim_then_approved_and_sent_once(self):
        self.review()
        self.queue_invite(note(120))
        item = self.cycle()["items"][0]
        self.assertEqual((item["state"], self.state(), self.claims(), self.judged), (approvals.AWAITING, approvals.AWAITING, 0, 1))
        self.assertNotIn("send_invitation", self.port.calls)
        with self.assertRaises(ValueError):  # no claim: no executor can send it
            self.adapter.execute_claimed(self.db, "invite-1", self.config)
        self.assertEqual(self.cycle()["items"], [])  # never selected again, never judged again
        self.assertEqual(self.judged, 1)
        self.assertEqual(approvals.waiting_count(self.db), 1)
        self.assertEqual(approvals.approve(self.db, "invite-1", shown_payload_hash=shown(self.db, "invite-1"))["state"], "APPROVED")
        self.assertEqual(self.state(), "PENDING")
        self.cycle()
        self.assertEqual((self.port.calls.count("send_invitation"), self.claims(), self.judged), (1, 1, 1))
        self.assertTrue(self.owner_decision_sources()[-1].startswith("OWNER_APPROVAL:message-decision:"))
        self.cycle()
        self.cycle()
        self.assertEqual(self.port.calls.count("send_invitation"), 1)
        self.assertEqual(approvals.waiting_count(self.db), 0)

    def test_approved_note_still_meets_every_gate(self):
        self.review()
        self.queue_invite(note(120))
        self.cycle()
        approvals.approve(self.db, "invite-1", shown_payload_hash=shown(self.db, "invite-1"))
        self.port.add_invitation(self.person)  # the person now has a pending invitation
        item = self.cycle()["items"][0]
        self.assertIn("PENDING_INVITATION_PRESENT", item["gates"])
        self.assertEqual((self.port.calls.count("send_invitation"), self.claims(), self.judged), (0, 0, 1))

    def test_initial_message_is_held_then_approved_once(self):
        self.port.accept(self.person)
        chat = self.port.add_chat(self.person, [])
        self.review()
        outreach.enqueue_action(self.db, self.action("INITIAL", conversation_id=chat))
        self.db.commit()
        self.assertEqual(self.cycle("INITIAL")["items"][0]["state"], approvals.AWAITING)
        self.assertNotIn("send_message", self.port.calls)
        approvals.approve(self.db, "initial-1", shown_payload_hash=shown(self.db, "initial-1"))
        self.cycle("INITIAL")
        self.cycle("INITIAL")
        self.assertEqual((self.port.calls.count("send_message"), self.judged), (1, 1))

    # Worked example 2
    def test_revise_to_291_sends_the_edit_and_keeps_the_original(self):
        original = note(278)
        self.review()
        self.queue_invite(original)
        self.cycle()
        edited = note(291, "infrastructure")
        result = approvals.revise_and_approve(self.db, "invite-1", edited, shown_payload_hash=shown(self.db, "invite-1"))
        self.assertEqual(result["state"], "REVISED")
        [record] = approvals.decisions(self.db, "invite-1")
        self.assertEqual((record["kind"], record["original_message"], record["edited_message"]),
                         (approvals.REVISION, original, edited))
        self.cycle()
        self.assertEqual([sent["note"] for sent in self.port.invitations], [edited])
        self.assertEqual(self.judged, 1)  # the edit is not judged again
        claim = self.db.execute("SELECT payload FROM outreach_action_claims").fetchone()[0]
        self.assertEqual(json.loads(claim)["message"], edited)

    def test_revise_to_312_is_refused_with_the_edit_kept_and_nothing_changed(self):
        self.review()
        self.queue_invite(note(278))
        self.cycle()
        before = dict(self.db.execute("SELECT * FROM outreach_actions").fetchone())
        too_long = note(312, "infrastructure")
        result = approvals.revise_and_approve(self.db, "invite-1", too_long, shown_payload_hash=shown(self.db, "invite-1"))
        self.assertEqual((result["state"], result["reason"], result["text"], result["length"]),
                         ("REFUSED", "INVITATION_NOTE_TOO_LONG", too_long, 312))
        self.assertIn("Shorten it by 12", result["message"])
        self.assertEqual(dict(self.db.execute("SELECT * FROM outreach_actions").fetchone()), before)
        self.assertEqual(approvals.decisions(self.db, "invite-1"), [])
        self.assertEqual(self.cycle()["items"], [])
        self.assertNotIn("send_invitation", self.port.calls)
        self.assertEqual(approvals.revise_and_approve(self.db, "invite-1", note(296, "infrastructure"), shown_payload_hash=shown(self.db, "invite-1"))["state"], "REVISED")

    def test_reject_is_never_sent(self):
        self.review()
        self.queue_invite(note(120))
        self.cycle()
        self.assertEqual(approvals.reject(self.db, "invite-1", shown_payload_hash=shown(self.db, "invite-1"))["state"], "REJECTED")
        self.assertEqual(self.state(), "CANCELLED_BEFORE_SEND")
        self.assertEqual(self.cycle()["items"], [])
        self.assertEqual(approvals.approve(self.db, "invite-1", shown_payload_hash=shown(self.db, "invite-1"))["state"], "CHANGED")
        approvals.set_mode(self.db, approvals.AUTOMATIC, home=self.home)
        self.assertEqual(self.cycle()["items"], [])
        self.assertEqual((self.port.calls.count("send_invitation"), self.claims()), (0, 0))

    def test_decision_on_a_changed_card_writes_nothing(self):
        self.review()
        self.queue_invite(note(120))
        self.cycle()
        self.assertEqual(approvals.approve(self.db, "invite-1", shown_payload_hash="stale")["state"], "CHANGED")
        self.assertEqual(approvals.reject(self.db, "invite-1", shown_payload_hash="stale")["state"], "CHANGED")
        self.assertEqual(approvals.revise_and_approve(self.db, "invite-1", "Hi.", shown_payload_hash="stale")["text"], "Hi.")
        self.assertEqual((approvals.decisions(self.db, "invite-1"), self.state()), ([], approvals.AWAITING))

    # Worked example 4
    def test_crash_after_approve_before_claim_sends_exactly_once(self):
        self.review()
        self.queue_invite(note(120))
        self.cycle()
        # A crash inside the approve transaction leaves nothing behind: the card is still waiting.
        real = approvals._approve_row

        def crash(*arguments):
            real(*arguments)
            raise RuntimeError("process died before commit")
        with patch.object(approvals, "_approve_row", side_effect=crash), self.assertRaises(RuntimeError):
            approvals.approve(self.db, "invite-1", shown_payload_hash=shown(self.db, "invite-1"))
        self.assertEqual((self.state(), approvals.decisions(self.db, "invite-1")), (approvals.AWAITING, []))
        # Committed approval, then the process dies before any cycle claims it.
        approvals.approve(self.db, "invite-1", shown_payload_hash=shown(self.db, "invite-1"))
        self.assertEqual(approvals.approve(self.db, "invite-1", shown_payload_hash=shown(self.db, "invite-1"))["state"], "CHANGED")  # a double click
        self.db.close()
        self.reopen()
        # The first cycle after the restart cannot read the person: approved and queued, not sent.
        self.port.fail("lookup", "timeout")
        self.cycle()
        self.assertEqual((self.claims(), self.port.calls.count("send_invitation")), (0, 0))
        self.assertEqual(len(approvals.decisions(self.db, "invite-1")), 1)
        self.db.execute("UPDATE outreach_read_retries SET next_at=NULL")
        self.db.commit()
        for unused in range(3):
            self.cycle()
        self.assertEqual((self.claims(), self.port.calls.count("send_invitation"), self.judged), (1, 1, 1))

    # Worked example 5, as changed by G4 (owner, 2026-10-22): an unreadable file decides nothing.
    def test_unreadable_mode_file_with_a_database_review_row_holds(self):
        approvals.store_mode(self.db, approvals.REVIEW)
        self.db.commit()
        self.write_state("{not json")
        self.queue_invite(note(120))
        item = self.cycle()["items"][0]
        self.assertEqual((item["state"], item["held_by"]), (approvals.AWAITING, approvals.HELD_BY_REVIEW))
        self.assertEqual((self.claims(), self.port.calls.count("send_invitation")), (0, 0))

    def test_unreadable_mode_file_and_no_database_row_sends_in_automatic(self):
        self.write_state("{not json")
        self.queue_invite(note(120))
        self.cycle()
        self.assertEqual((self.claims(), self.port.calls.count("send_invitation")), (1, 1))

    # Worked example 6 (with the whole existing suite, which runs unchanged)
    def test_automatic_mode_sends_as_today_and_writes_nothing_new(self):
        self.queue_invite(note(120))
        self.cycle()
        self.assertEqual((self.claims(), self.port.calls.count("send_invitation"), self.judged), (1, 1, 1))
        tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("approval_mode", tables)
        self.assertNotIn("outreach_owner_records", tables)


class ModeReading(HomeMixin, unittest.TestCase):
    def setUp(self):
        self.use_home()
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)

    def test_unreadable_stores_decide_nothing(self):
        self.assertEqual(approvals.mode(self.db), approvals.AUTOMATIC)  # no file, no table: never ran Setup
        self.write_state(json.dumps({"steps": {}}))
        self.assertEqual(approvals.mode(self.db), approvals.AUTOMATIC)  # step 4 not reached: the default
        # G4 (owner, 2026-10-22): an unreadable file decides nothing; with no database row, Automatic.
        for text in ("{not json", "[]", json.dumps({"mode": "bogus"}), ""):
            self.write_state(text)
            self.assertEqual(approvals.mode_reading(self.db), (approvals.AUTOMATIC, None), text)
        approvals.store_mode(self.db, approvals.REVIEW)
        for text in ("{not json", "[]", json.dumps({"mode": "bogus"}), ""):
            self.write_state(text)
            self.assertEqual(approvals.mode_reading(self.db), (approvals.REVIEW, approvals.HELD_BY_REVIEW), text)
        self.db.execute("DELETE FROM approval_mode")
        (self.home / approvals.STATE_FILE).unlink()
        (self.home / approvals.STATE_FILE).mkdir()
        self.assertEqual(approvals.mode(self.db), approvals.AUTOMATIC)
        (self.home / approvals.STATE_FILE).rmdir()
        approvals.store_mode(self.db, approvals.REVIEW)
        self.write_state(json.dumps({"mode": "automatic"}))
        self.assertEqual(approvals.mode(self.db), approvals.REVIEW)  # either store saying review holds
        approvals.store_mode(self.db, approvals.AUTOMATIC)
        self.assertEqual(approvals.mode(self.db), approvals.AUTOMATIC)
        self.write_state(json.dumps({"mode": "review"}))
        self.assertEqual(approvals.mode(self.db), approvals.REVIEW)
        self.db.execute("UPDATE approval_mode SET mode='garbled'")
        self.assertEqual(approvals.mode(self.db), approvals.REVIEW)   # the file's readable Review still holds
        self.write_state(json.dumps({"mode": "automatic"}))
        self.assertEqual(approvals.mode(self.db), approvals.AUTOMATIC)  # a garbled row decides nothing (G4)

    def test_install_mode_writes_the_database_first_even_before_the_first_cycle(self):
        root = Path(tempfile.mkdtemp(prefix="sidedoor-root-"))
        self.addCleanup(shutil.rmtree, root, True)
        approvals.set_install_mode("review", root=root, home=self.home)
        # GL2: the toggle creates the database's approval_mode row, so Review never lives only in the file.
        with contextlib.closing(sqlite3.connect(root / approvals.DATABASE)) as database:
            self.assertEqual(approvals.stored_mode(database), approvals.REVIEW)
        self.assertEqual(approvals.file_mode(self.home), approvals.REVIEW)
        self.write_state("{not json")   # the file lost: the database's Review still holds
        self.assertEqual(approvals.install_mode(root, self.home), approvals.REVIEW)
        self.write_state(json.dumps({"mode": "review"}))
        approvals.set_install_mode("automatic", root=root, home=self.home)
        with contextlib.closing(sqlite3.connect(root / approvals.DATABASE)) as database:
            self.assertEqual(approvals.stored_mode(database), approvals.AUTOMATIC)
        self.assertEqual(approvals.file_mode(self.home), approvals.AUTOMATIC)
        approvals.set_install_mode("review", root=root, home=self.home)
        with contextlib.closing(sqlite3.connect(root / approvals.DATABASE)) as database:
            self.assertEqual(approvals.mode(database, self.home), approvals.REVIEW)


class EveryPurposeFixture(HomeMixin, unittest.TestCase):
    """Point H is after the gates and before the claim for every purpose. The gates are stubbed to
    pass here (each purpose's own gates have their own tests; the real-gate paths are in PortReview,
    FollowupReview and ReplyReview); the transport must never be reached while held."""

    PROOF = {"identity": "linkedin.com/in/every-purpose", "sender_id": 7, "checked_at": None, "evidence": "mock",
             "sender_ready": True, "entitlement_verified": True, "direct_messages_complete": True,
             "sent_invitations_complete": True, "pending_invitation": False, "has_reply": False, "negative_reply": False,
             "opt_out": False, "cooldown": False, "reply_transport_supported": True, "provider_restriction_observed": False,
             "reply_latest_inbound": True, "reply_already_answered": False, "reply_context_verified": True,
             "followup_transport_supported": True, "followup_context_verified": True, "followup_thread_unchanged": True}

    def setUp(self):
        self.use_home()
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        outreach.initialize_actions(self.db)
        approvals.set_mode(self.db, approvals.REVIEW, home=self.home)
        self.config = {"outreach_mode": "guarded", "outreach_actions_enabled": True, "sender_id": 7}
        self.now = dt.datetime.now(dt.timezone.utc)
        self.transport = Mock(side_effect=AssertionError("held: never sent"))
        self.adapter = outreach.HeyReachActionAdapter(self.transport, lambda action, config: dict(self.PROOF))
        self.adapter.port = Mock(side_effect=AssertionError("held: never sent"))
        self.enterContext(patch("outreach.action_gates", side_effect=lambda *arguments, **named: []))
        self.enterContext(patch("pacing.check", return_value={"go": True}))

    def action(self, purpose, **extra):
        action = {"action_id": purpose.lower(), "profile": "https://www.linkedin.com/in/every-purpose",
                  "recipient": "Every Purpose", "company": "Mock Company", "purpose": purpose, "message": "Hello.",
                  "channel": "linkedin", "provider": "heyreach", "conversation_id": "thread-1",
                  "verification": {"verified_at": self.now.isoformat()}}
        action.update(extra)
        return action

    def holds(self, action):
        outreach.enqueue_action(self.db, action)
        item = outreach.cycle(self.db, self.config, self.adapter, purpose=action["purpose"])["items"][0]
        self.assertEqual(item["state"], approvals.AWAITING)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM outreach_action_claims").fetchone()[0], 0)
        self.transport.assert_not_called()
        self.adapter.port.assert_not_called()



class EveryPurposeHolds(EveryPurposeFixture):
    def test_invite(self):
        self.holds(self.action("INVITE", provider="unipile", provider_id="p-1", note="Hello."))

    def test_initial(self):
        self.holds(self.action("INITIAL"))

    def test_first_message(self):
        self.holds(self.action("FIRST_MESSAGE"))

    def test_prepare_campaign(self):
        self.holds(self.action("PREPARE_CAMPAIGN", list_id=5, campaign_name="One contact"))

    def test_followup(self):
        self.holds(self.action("FOLLOWUP", ordinal=1))

    def test_urgent(self):
        self.holds(self.action("URGENT", ordinal=1))

    def test_reply(self):
        self.holds(self.action("REPLY", sender_id=7, reply_task_id="task-1", reply_source_key="source-1",
                               reply_source_sha256="a" * 64, reply_source="heyreach", reply_source_id="message-1",
                               reply_inbound_at=self.now.isoformat()))


class ResearchFixture(HomeMixin, unittest.TestCase):
    """The research -> promotion -> INVITE path (test_invite_path's fixture)."""

    setUp_invite = invite_path.InvitePathTests.setUp
    proof = invite_path.InvitePathTests.proof
    inspected = invite_path.InvitePathTests.inspected
    promote = invite_path.InvitePathTests.promote
    execute = invite_path.InvitePathTests.execute
    invite = invite_path.InvitePathTests.invite

    def setUp(self):
        self.setUp_invite()
        self.use_home()

    def review(self):
        approvals.set_mode(self.database, approvals.REVIEW, home=self.home)

    def held(self):
        self.assertEqual(self.promote()["queued"], 1)
        self.review()
        self.execute()
        row, action = self.invite()
        self.assertEqual(row["state"], approvals.AWAITING)
        return row, action



class ResearchReview(ResearchFixture):
    def test_revise_of_a_research_action_is_sent_and_promotion_leaves_it(self):
        row, action = self.held()
        edited = "Hi, I read about the API team and would value connecting."
        self.assertEqual(approvals.revise_and_approve(self.database, row["action_id"], edited, shown_payload_hash=shown(self.database, row["action_id"]))["state"], "REVISED")
        revised_hash = self.invite()[0]["payload_hash"]
        item = next(item for item in self.promote()["items"] if item.get("action_id") == row["action_id"])
        self.assertEqual((item["state"], item.get("owner_decision")), ("ALREADY_QUEUED", True))
        self.assertEqual((self.invite()[0]["payload_hash"], self.invite()[0]["state"]), (revised_hash, "PENDING"))
        self.execute()
        self.assertEqual([sent["note"] for sent in self.port.invitations], [edited])

    # Worked example 3
    def test_rejected_person_may_be_drafted_again_but_a_sent_one_may_not(self):
        row, action = self.held()
        self.assertEqual(approvals.reject(self.database, row["action_id"], shown_payload_hash=shown(self.database, row["action_id"]))["state"], "REJECTED")
        later = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        research.plan_research(self.database, "plan-2", ["Mock Company"], now=later)
        self.assertIsNone(research.prior_contact_record(self.database, invite_path.IDENTITY, "Mock Invitee"))
        reservation = research.reserve_contact(self.database, "plan-2", "Mock Company", invite_path.PROFILE,
                                               "Mock Invitee", now=later)
        self.assertEqual(reservation["state"], "RESERVED")
        self.execute()
        self.assertNotIn("send_invitation", self.port.calls)

    def test_held_or_sent_person_is_still_refused(self):
        self.held()
        self.assertEqual(research.prior_contact_record(self.database, invite_path.IDENTITY, "Mock Invitee"), "outreach_actions")
        approvals.approve(self.database, self.invite()[0]["action_id"], shown_payload_hash=shown(self.database, self.invite()[0]["action_id"]))
        self.execute()
        self.assertEqual(self.port.calls.count("send_invitation"), 1)
        later = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        research.plan_research(self.database, "plan-2", ["Mock Company"], now=later)
        self.assertIsNotNone(research.prior_contact_record(self.database, invite_path.IDENTITY, "Mock Invitee"))
        self.assertEqual(research.reserve_contact(self.database, "plan-2", "Mock Company", invite_path.PROFILE,
                                                  "Mock Invitee", now=later)["state"], "BLOCKED")


class SecondSlotAfterRejection(ResearchFixture):
    """Owner decision 1(a), 2026-10-22: a person whose every action was rejected and never sent gets a
    new, sendable action when research drafts them again; every other uniqueness rule is unchanged."""

    def redraft(self, role_title):
        at = invite_path.now()
        person = {"recipient": "Mock Invitee", "profile": invite_path.PROFILE, "employer_matches_target": True, "evidence": [
            invite_path.evidence("identity", invite_path.PROFILE, at), invite_path.evidence("employment", "Mock Company", at),
            invite_path.evidence("team", "API team", at), invite_path.evidence("ability_to_help", "Runs team hiring", at),
            invite_path.evidence("contact_type", "hiring_manager", at)]}
        role = {"company": "Mock Company", "role": role_title, "url": "https://example.test/jobs/123",
                "requisition_id": "123", "rationale": "MOCK only", "people": [person],
                "evidence": [invite_path.evidence("team_need", "reliable APIs", at),
                             invite_path.evidence("specialization", "backend", at)]}
        return research.process_research(self.database, {"research_status": "COMPLETE", "summary": "Mock", "blockers": [],
                                                         "opportunities": [role], "drafts": []}, plan_id="plan", now=at)

    def actions(self):
        return self.database.execute("SELECT action_id,slot,state FROM outreach_actions ORDER BY rowid").fetchall()

    def test_rejected_then_redrafted_person_gets_a_new_sendable_action(self):
        row, unused = self.held()
        approvals.reject(self.database, row["action_id"], shown_payload_hash=shown(self.database, row["action_id"]))
        self.assertEqual(self.actions()[0]["slot"], approvals.REJECTED_SLOT + row["action_id"])
        approvals.set_mode(self.database, approvals.AUTOMATIC, home=self.home)
        self.assertEqual(len(self.redraft("Principal Software Engineer")["drafts"]), 1)
        self.assertEqual(self.promote()["queued"], 1)
        first, second = self.actions()
        self.assertNotEqual(second["action_id"], first["action_id"])
        self.assertEqual((second["slot"], first["state"]), ("INVITE", "CANCELLED_BEFORE_SEND"))
        self.execute()
        self.assertEqual(self.port.calls.count("send_invitation"), 1)
        sent = self.database.execute("SELECT action_id FROM outreach_action_claims").fetchall()
        self.assertEqual([claim[0] for claim in sent], [second["action_id"]])
        # One live action per person: neither a further draft nor promotion adds a third.
        self.redraft("Distinguished Software Engineer")
        self.promote()
        self.execute()
        self.assertEqual((len(self.actions()), self.port.calls.count("send_invitation")), (2, 1))
        later = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        research.plan_research(self.database, "plan-2", ["Mock Company"], now=later)
        self.assertEqual(research.reserve_contact(self.database, "plan-2", "Mock Company", invite_path.PROFILE,
                                                  "Mock Invitee", now=later)["state"], "BLOCKED")

    def test_without_a_rejection_the_action_id_is_unchanged(self):
        self.assertEqual(self.promote()["queued"], 1)
        row = self.invite()[0]
        identity = research.canonical(self.database, "contact", invite_path.PROFILE)
        self.assertEqual(row["action_id"], "research-outreach:" + research.digest([identity, "INVITE"]))


class FollowupReview(followups.Base):
    """A Unipile follow-up composed, judged, held; revised; sent as edited without a second judgment."""

    def setUp(self):
        self.judged = 0
        super().setUp()
        approvals.store_mode(self.db, approvals.REVIEW)
        self.db.commit()

    def execute(self, config, prompt, schema, timeout):
        if "action" in json.loads(prompt.split("UNTRUSTED_SOURCE_JSON:\n", 1)[1]):
            self.judged += 1
        return super().execute(config, prompt, schema, timeout)

    def test_followup_is_held_then_revised_and_sent_once(self):
        self.now = engagement.business_days_after(followups.INVITED, 3)
        self.run_cycle()
        self.port.accept(self.person)
        self.now += dt.timedelta(hours=1, minutes=1)
        self.run_cycle()
        self.assertEqual((self.state(1), self.judged), (approvals.AWAITING, 1))
        self.assertNotIn("start_chat", self.port.calls)
        action_id = self.db.execute("SELECT action_id FROM outreach_actions WHERE slot='FOLLOWUP:1'").fetchone()[0]
        edited = "Thanks for connecting. I would value ten minutes on the platform team's roadmap."
        self.assertEqual(approvals.revise_and_approve(self.db, action_id, edited, shown_payload_hash=shown(self.db, action_id), now=self.now)["state"], "REVISED")
        self.run_cycle()
        self.assertEqual((self.ours(), self.judged, self.port.calls.count("start_chat")), ([edited], 1, 1))
        self.settle()
        self.assertEqual(self.state(1), "CONFIRMED_SENT")
        self.assertEqual(self.ours(), [edited])


class ReplyReview(unittest.TestCase):
    def test_reply_is_held_then_approved_and_sent_once(self):
        with manual_reply.isolated_boundaries(), inbox.UnipileReplyFixture() as fixture:
            fixture.seed()
            approvals.store_mode(fixture.database, approvals.REVIEW)
            fixture.database.commit()
            fixture.cycle()
            action = fixture.actions()[0]
            self.assertEqual(action["state"], approvals.AWAITING)
            self.assertEqual(fixture.port.calls.count("send_message"), 0)
            self.assertEqual(approvals.approve(fixture.database, action["action_id"], shown_payload_hash=shown(fixture.database, action["action_id"]))["state"], "APPROVED")
            fixture.cycle(seconds=60)
            fixture.cycle(seconds=60)
            self.assertEqual(fixture.actions()[0]["state"], "CONFIRMED_SENT")
            self.assertEqual([message["text"] for message in fixture.ours()], [manual_reply.TEXT])
            self.assertEqual(fixture.port.calls.count("send_message"), 1)
            sources = [json.loads(row[0]).get("decision_source") for row in fixture.database.execute(
                "SELECT record FROM outreach_agent_audit WHERE action_id=? AND kind='DECISION'", (action["action_id"],))]
            self.assertTrue(any(source and source.startswith("OWNER_APPROVAL:") for source in sources))


class ApprovalIsTheFreshCheck(unittest.TestCase):
    """Owner, 2026-10-22: an approval more than 7 days after writing counts as fresh; the approval
    time becomes verified_at."""

    setUp = test_outreach.ActionTests.setUp

    def test_old_text_approved_today_passes_the_freshness_gate(self):
        self.action["verification"]["verified_at"] = (self.now - dt.timedelta(days=8)).isoformat()
        outreach.enqueue_action(self.database, self.action)
        self.assertIn("CONTENT_OR_IDENTITY_VERIFICATION_STALE",
                      outreach.action_gates(self.database, self.config, self.action, self.proof, self.now))
        self.database.execute("UPDATE outreach_actions SET state=?", (approvals.AWAITING,))
        self.database.commit()
        self.assertEqual(approvals.approve(self.database, self.action["action_id"], shown_payload_hash=shown(self.database, self.action["action_id"]), now=self.now)["state"], "APPROVED")
        self.assertEqual(approvals.verified_at(self.database, self.action), self.now.isoformat())
        self.assertNotIn("CONTENT_OR_IDENTITY_VERIFICATION_STALE",
                         outreach.action_gates(self.database, self.config, self.action, self.proof, self.now))
        self.assertIn("CONTENT_OR_IDENTITY_VERIFICATION_STALE", outreach.action_gates(
            self.database, self.config, self.action, self.proof, self.now + dt.timedelta(days=8)))


class ModeSwitch(HomeMixin, unittest.TestCase):
    def setUp(self):
        self.use_home()
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        approvals.initialize(self.db)
        approvals.set_mode(self.db, approvals.REVIEW, home=self.home)

    def wait(self, action_id):
        outreach.enqueue_action(self.db, {"action_id": action_id, "profile": "https://www.linkedin.com/in/" + action_id,
            "recipient": "Person " + action_id, "company": "Mock Company", "purpose": "INITIAL", "message": "Hello.",
            "channel": "linkedin", "provider": "heyreach", "conversation_id": "thread", "verification": {}})
        self.db.execute("UPDATE outreach_actions SET state=? WHERE action_id=?", (approvals.AWAITING, action_id))
        self.db.commit()

    def test_send_them_releases_exactly_those_counted_database_first(self):
        self.wait("one")
        self.wait("two")
        shown = approvals.waiting(self.db)
        self.wait("three")  # arrives after the question was shown
        with patch.object(approvals, "_write_file_mode", side_effect=RuntimeError("crash before the file")):
            with self.assertRaises(RuntimeError):
                approvals.release_all(self.db, shown, home=self.home)
        # The database went first: the two are released; the file still says Review, so nothing new
        # goes out unasked (design 3.3), and the third still waits.
        states = dict(self.db.execute("SELECT action_id,state FROM outreach_actions").fetchall())
        self.assertEqual(states, {"one": "PENDING", "two": "PENDING", "three": approvals.AWAITING})
        self.assertEqual(approvals.mode(self.db, self.home), approvals.REVIEW)
        self.assertEqual({record["decided_via"] for action_id in ("one", "two")
                          for record in approvals.decisions(self.db, action_id)}, {"mode_switch"})
        approvals.set_mode(self.db, approvals.AUTOMATIC, home=self.home)
        self.assertEqual(approvals.mode(self.db, self.home), approvals.AUTOMATIC)

    def test_keep_for_review_changes_only_the_mode(self):
        self.wait("one")
        result = approvals.keep_for_review(self.db, home=self.home)
        self.assertEqual((result["mode"], result["waiting"]), (approvals.AUTOMATIC, 1))
        self.assertEqual(approvals.mode(self.db, self.home), approvals.AUTOMATIC)
        self.assertEqual(approvals.waiting_count(self.db), 1)
        self.assertEqual(approvals.decisions(self.db, "one"), [])


class NeedsYouCount(unittest.TestCase):
    def test_count_reads_only(self):
        database = sqlite3.connect(":memory:")
        self.addCleanup(database.close)
        self.assertEqual(approvals.waiting_count(database), 0)
        self.assertEqual(database.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0], 0)


class DashboardNotifications(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = sqlite3.connect(Path(directory.name) / "fixture.sqlite")
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.execute("""CREATE TABLE notification_outbox (event_key TEXT PRIMARY KEY, message TEXT NOT NULL,
            state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_at TEXT, delivery TEXT, last_error TEXT,
            updated_at TEXT NOT NULL, reconcile_attempts INTEGER NOT NULL DEFAULT 0)""")
        self.db.execute("INSERT INTO notification_outbox(event_key,message,state,updated_at) VALUES ('k','Hello','PENDING','2026-10-22T00:00:00+00:00')")
        self.db.commit()

    def row(self):
        return dict(self.db.execute("SELECT * FROM notification_outbox").fetchone())

    def test_without_telegram_the_row_is_shown_in_the_dashboard(self):
        with patch("subprocess.run", side_effect=AssertionError("no keychain")), \
                patch("urllib.request.urlopen", side_effect=AssertionError("no network")):
            notifications.dispatch(self.db, {})
        row = self.row()
        self.assertEqual(row["state"], "DELIVERED")
        self.assertEqual(json.loads(row["delivery"]), {"provider": "dashboard", "message_id": "k", "chat_id": "local"})

    def test_with_telegram_configured_telegram_is_still_used(self):
        config = {"telegram": {"keychain_service": "s", "keychain_account": "a", "chat_id": "1"}}
        with patch.object(notifications.Telegram, "send", side_effect=notifications.Unavailable("offline")):
            notifications.dispatch(self.db, config)
        self.assertEqual((self.row()["state"], self.row()["last_error"]), ("PENDING", "TELEGRAM_SECURE_CREDENTIAL_OR_CHAT_UNAVAILABLE"))


class ChampionSummaryRevision(unittest.TestCase):
    """Owner decision 2(a): an advocate summary the owner revised is bound by the MESSAGE_REVISION."""

    def test_revised_summary_is_not_a_binding_mismatch(self):
        database = sqlite3.connect(":memory:")
        self.addCleanup(database.close)
        approvals.initialize(database)
        body, edited = "Original advocate summary.", "Edited advocate summary."
        original = json.dumps({"action_id": "reply-1", "purpose": "REPLY", "reply_task_id": "task-1", "message": body}, sort_keys=True)
        payload = json.dumps({"action_id": "reply-1", "purpose": "REPLY", "reply_task_id": "task-1", "message": edited}, sort_keys=True)
        digest = lambda text: __import__("hashlib").sha256(text.encode()).hexdigest()
        summary = {"contact_id": "linkedin.com/in/advocate", "body": body}
        provenance = {"action_id": "reply-1", "reply_task_id": "task-1"}
        action = {"action_id": "reply-1", "identity": "linkedin.com/in/advocate", "state": "PENDING",
                  "payload": payload, "payload_hash": digest(payload)}
        before = champion_reporting._delivery(database, summary, provenance, action, None, "2026-10-22T00:00:00+00:00")
        self.assertEqual(before.get("error"), "ACTION_SUMMARY_BINDING_MISMATCH")
        with database:
            approvals._record(database, approvals.REVISION, action["identity"], {
                "action_id": "reply-1", "shown_payload_hash": digest(original), "original_message": body,
                "original_payload": original, "edited_message": edited, "edited_payload": payload,
                "edited_payload_hash": digest(payload), "approved_payload_hash": digest(payload),
                "decided_at": "2026-10-22T00:00:00+00:00"}, "2026-10-22T00:00:00+00:00")
        after = champion_reporting._delivery(database, summary, provenance, action, None, "2026-10-22T00:00:00+00:00")
        self.assertNotIn("error", after)
        self.assertEqual(after["delivery_state"], "QUEUED")


class CampaignLaunchReview(unittest.TestCase):
    """Review fixes P1 (a launch needs an approval in Review) and P2 (a revised campaign note is sent)."""

    def setUp(self):
        self.fixture = campaign_fixture.Fixture()
        self.addCleanup(self.fixture.close)
        self.addCleanup(patch.stopall)
        patch("socket.socket.connect", side_effect=AssertionError("NO NETWORK")).start()

    def starts(self):
        return [write[0] for write in self.fixture.writes].count("start_campaign")

    def queue_state(self):
        return self.fixture.database.execute("SELECT state FROM outreach_queue").fetchone()[0]

    def review(self):
        with self.fixture.database:
            approvals.store_mode(self.fixture.database, approvals.REVIEW)

    def test_campaign_prepared_in_automatic_waits_for_approval_after_switching_to_review(self):
        fixture = self.fixture
        fixture.create(fixture.prepare_scheduled())
        self.review()
        self.assertEqual(fixture.dispatch()["items"][0]["state"], approvals.AWAITING)
        self.assertEqual((self.queue_state(), self.starts()), (approvals.AWAITING, 0))
        cycle = fixture.run_campaign_cycle()
        self.assertEqual((cycle["items"], self.starts()), ([], 0))  # not inspected again, not launched
        [item] = approvals.waiting(fixture.database)
        self.assertEqual((item["stage"], item["revisable"], item["action_id"]), ("LAUNCH", False, fixture.action["action_id"]))
        self.assertEqual(approvals.waiting_count(fixture.database), 1)
        self.assertEqual(approvals.revise_and_approve(fixture.database, item["action_id"], "Hi.",
                                                      shown_payload_hash=item["payload_hash"])["reason"], "CAMPAIGN_ALREADY_PREPARED")
        self.assertEqual(approvals.approve(fixture.database, item["action_id"], shown_payload_hash=item["payload_hash"])["state"], "APPROVED")
        fixture.dispatch()
        fixture.dispatch()
        self.assertEqual(self.starts(), 1)

    def test_blocked_or_held_inspection_queue_rows_are_held_too_and_reject_never_launches(self):
        fixture = self.fixture
        fixture.create(fixture.prepare_scheduled())
        with fixture.database:
            fixture.database.execute("UPDATE outreach_queue SET state='BLOCKED'")
        self.review()
        fixture.dispatch()
        self.assertEqual((self.queue_state(), self.starts()), (approvals.AWAITING, 0))
        [item] = approvals.waiting(fixture.database)
        self.assertEqual(approvals.reject(fixture.database, item["action_id"], shown_payload_hash=item["payload_hash"])["state"], "REJECTED")
        with fixture.database:
            approvals.store_mode(fixture.database, approvals.AUTOMATIC)
        fixture.dispatch()
        fixture.run_campaign_cycle()
        self.assertEqual((self.queue_state(), self.starts()), (approvals.QUEUE_REJECTED, 0))

    def test_revised_campaign_note_is_created_and_launched_with_the_edit(self):
        fixture = self.fixture
        context = fixture.prepare_scheduled()
        import outreach_promotion
        result = outreach_promotion.cycle(fixture.database, dict(fixture.config, outreach_promotion_contexts={
            fixture.action["profile"]: context}), fixture.now)
        self.assertEqual(result["queued"], 1)
        fixture.proof = context["proof"]
        self.review()
        adapter = outreach.HeyReachActionAdapter(fixture.create_transport, lambda action, config: fixture.proof, fixture.reconcile)
        outreach.cycle(fixture.database, fixture.config, adapter, fixture.now, purpose="PREPARE_CAMPAIGN")
        action_id = fixture.action["action_id"]
        self.assertEqual(fixture.database.execute("SELECT state FROM outreach_actions").fetchone()[0], approvals.AWAITING)
        edited = "Hi, I would value connecting about your platform team."
        self.assertEqual(approvals.revise_and_approve(fixture.database, action_id, edited, now=fixture.now,
                                                      shown_payload_hash=shown(fixture.database, action_id))["state"], "REVISED")
        # The fake HeyReach and the stand-in inspector follow the stored (edited) action, as the real
        # adapter's proof does.
        fixture.action = json.loads(fixture.database.execute("SELECT payload FROM outreach_actions").fetchone()[0])
        fixture.proof = dict(fixture.proof, sequence=fixture.action["sequence"])
        outreach.cycle(fixture.database, fixture.config, adapter, fixture.now, purpose="PREPARE_CAMPAIGN")
        fixture.restart()
        outreach.cycle(fixture.database, fixture.config, adapter, fixture.now, purpose="PREPARE_CAMPAIGN")
        self.assertEqual(fixture.database.execute("SELECT state FROM outreach_actions").fetchone()[0], "PREPARED")
        campaign_fixture.campaign.handoff(fixture.database, fixture.config, fixture, action_id, fixture.proof, fixture.now)
        fixture.dispatch()
        created = [arguments for operation, arguments in fixture.writes if operation == "create_campaign"]
        self.assertEqual(json.loads(created[0]["sequenceJson"])["payload"]["messages"], [edited])
        self.assertEqual(self.starts(), 1)


class NotificationTransportChoice(unittest.TestCase):
    """Review fixes P3 and P5."""

    setUp = DashboardNotifications.setUp
    row = DashboardNotifications.row

    def test_shipped_config_uses_the_dashboard_and_the_owners_setup_is_unchanged(self):
        shipped = json.loads((ROOT / "config.json").read_text())
        self.assertIsInstance(notifications.transport_for(shipped), notifications.Dashboard)
        relay = dict(shipped, bridge_transport=dict(shipped.get("bridge_transport") or {}, url="https://relay.example.test"))
        self.assertIsInstance(notifications.transport_for(relay), notifications.Relay)
        telegram = {"telegram": {"keychain_service": "s", "keychain_account": "a", "chat_id": "1"}}
        self.assertIsInstance(notifications.transport_for(telegram), notifications.Telegram)

    def test_partial_telegram_setup_still_fails_visibly(self):
        notifications.dispatch(self.db, {"telegram": {"keychain_service": "s", "keychain_account": "a"}})
        row = self.row()
        self.assertEqual((row["state"], row["last_error"]), ("PENDING", "TELEGRAM_SECURE_CREDENTIAL_OR_CHAT_UNAVAILABLE"))

    def test_dashboard_reconciles_only_rows_it_sent(self):
        with self.db:
            self.db.execute("UPDATE notification_outbox SET state='SENDING'")   # an interrupted send by another transport
        notifications.dispatch(self.db, {})
        row = self.row()
        self.assertEqual(row["state"], "UNCERTAIN")
        self.assertEqual(row["reconcile_error"], "DASHBOARD_CANNOT_RECONCILE_ANOTHER_TRANSPORT")
        with self.db:
            self.db.execute("UPDATE notification_outbox SET state='SENDING',delivery=?,next_at=NULL", (notifications.Dashboard.MARK,))
        notifications.dispatch(self.db, {})
        self.assertEqual(self.row()["state"], "DELIVERED")


class AwaitingClosure(HomeMixin, unittest.TestCase):
    """Review fix P4: a waiting message to a closed or opted-out person is closed, with why."""

    setUp_actions = test_outreach.ActionTests.setUp
    record = test_outreach.ActionTests.record

    def setUp(self):
        self.setUp_actions()
        self.use_home()

    def held(self, action_id, profile):
        outreach.enqueue_action(self.database, dict(self.action, action_id=action_id, profile=profile))
        with self.database:
            self.database.execute("UPDATE outreach_actions SET state=? WHERE action_id=?", (approvals.AWAITING, action_id))

    def test_closed_and_opted_out_people_leave_approvals(self):
        self.held("closed-1", "https://www.linkedin.com/in/closed-person")
        self.held("opted-1", "https://www.linkedin.com/in/mock-person")
        engagement.close_outreach(self.database, "https://www.linkedin.com/in/closed-person", "Owner closed", self.now)
        self.record("opt_out")
        outreach.cycle(self.database, self.config, self.adapter, self.now)
        states = {row[0]: (row[1], json.loads(row[2])["closed_while_awaiting_approval"]["reason"])
                  for row in self.database.execute("SELECT action_id,state,result FROM outreach_actions")}
        self.assertEqual(states["closed-1"], ("CLOSED", "OUTREACH_CLOSED: Owner closed"))
        self.assertEqual(states["opted-1"], ("CLOSED", "OPTED_OUT"))
        self.assertEqual((approvals.waiting(self.database), approvals.waiting_count(self.database)), ([], 0))
        self.transport.assert_not_called()


class StrictInputs(HomeMixin, unittest.TestCase):
    """Review fixes P6, P7, P8."""

    def setUp(self):
        self.use_home()
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        approvals.initialize(self.db)

    def test_unknown_mode_is_refused_and_nothing_changes(self):
        approvals.set_mode(self.db, approvals.REVIEW, home=self.home)
        for value in (None, "", "bogus", "Automatic"):
            with self.assertRaises(approvals.ModeError):
                approvals.set_mode(self.db, value, home=self.home)
            with self.assertRaises(approvals.ModeError):
                approvals.set_install_mode(value, root=self.home, home=self.home)
            with self.assertRaises(ValueError):
                setup_steps.set_mode(value, self.home)
        self.assertEqual((approvals.stored_mode(self.db), approvals.file_mode(self.home)), (approvals.REVIEW, approvals.REVIEW))

    def test_decisions_require_what_the_owner_was_shown(self):
        with self.assertRaises(TypeError):
            approvals.approve(self.db, "a")
        with self.assertRaises(TypeError):
            approvals.reject(self.db, "a")
        with self.assertRaises(TypeError):
            approvals.revise_and_approve(self.db, "a", "Hi.")
        for function, arguments in ((approvals.approve, ()), (approvals.reject, ()), (approvals.revise_and_approve, ("Hi.",))):
            with self.assertRaisesRegex(ValueError, "SHOWN_PAYLOAD_HASH_REQUIRED"):
                function(self.db, "a", *arguments, shown_payload_hash=None)
        with self.assertRaisesRegex(ValueError, "SHOWN_MESSAGES_REQUIRED"):
            approvals.release_all(self.db, None, home=self.home)
        self.assertEqual(approvals.stored_mode(self.db), None)  # nothing written


class HoldReporting(EveryPurposeFixture):
    """Review fix P9: the cycle reports a hold only when the row actually changed."""

    def test_unrecorded_hold_reports_what_happened(self):
        outreach.enqueue_action(self.db, self.action("INITIAL"))
        with patch.object(approvals, "mark_held", return_value=False):
            item = outreach.cycle(self.db, self.config, self.adapter, purpose="INITIAL")["items"][0]
        self.assertEqual((item["state"], item["blocker"]), ("PENDING", "APPROVAL_HOLD_NOT_RECORDED"))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM outreach_action_claims").fetchone()[0], 0)
        self.transport.assert_not_called()


class UnreadableModeHold(PortFixture):
    """G4 (owner, 2026-10-22): an unreadable mode no longer holds; a row an earlier version held for
    that reason (tagged UNREADABLE_MODE) is released on the next cycle, so none stays stuck."""

    def legacy_hold(self):
        action = self.queue_invite(note(120))
        held = {"awaiting_approval": {"held_at": "2026-10-21T00:00:00+00:00", "held_by": "UNREADABLE_MODE",
                                      "evidence": None, "agent_decision": None}}
        self.db.execute("UPDATE outreach_actions SET state=?,result=? WHERE action_id=?",
                        (approvals.AWAITING, json.dumps(held), action["action_id"]))
        self.db.commit()

    def test_a_legacy_unreadable_hold_is_released_and_sent_on_the_next_cycle(self):
        self.write_state("{not json")   # still unreadable: no longer a reason to hold
        self.legacy_hold()
        result = self.cycle()
        self.assertEqual(result["approvals"]["released"], ["invite-1"])
        self.assertEqual((self.port.calls.count("send_invitation"), self.claims()), (1, 1))

    def test_a_legacy_unreadable_hold_under_a_readable_review_waits_for_the_owner(self):
        self.legacy_hold()
        self.review()
        result = self.cycle()
        self.assertEqual(result["approvals"]["released"], ["invite-1"])
        self.assertEqual((self.port.calls.count("send_invitation"), self.claims()), (0, 0))
        self.assertEqual(self.state(), approvals.AWAITING)
        self.assertEqual(approvals._held_by(self.db.execute("SELECT result FROM outreach_actions").fetchone()[0],
                                            "awaiting_approval"), approvals.HELD_BY_REVIEW)

    def test_a_legacy_unreadable_launch_hold_is_released(self):
        outreach.initialize(self.db)
        self.db.execute("""INSERT INTO outreach_queue(identity,campaign_id,recipient,company,state,payload,gates)
                           VALUES (?,?,?,?,?,?,?)""", ("linkedin.com/in/x", 99, "X", "Y", approvals.AWAITING, "{}",
                                                       json.dumps({"held_by": "UNREADABLE_MODE"})))
        self.db.commit()
        self.assertEqual(approvals.release_unreadable_holds(self.db), [99])
        self.assertEqual(self.db.execute("SELECT state,gates FROM outreach_queue").fetchone()[:],
                         ("PENDING_VALIDATION", None))

    def test_review_hold_stays_after_switching_to_automatic(self):
        self.review()
        self.queue_invite(note(120))
        self.cycle()
        approvals.keep_for_review(self.db, home=self.home)
        self.cycle()
        self.assertEqual((self.state(), self.port.calls.count("send_invitation")), (approvals.AWAITING, 0))


class ModeShownAsTheSendPathReadsIt(HomeMixin, unittest.TestCase):
    """Review fix P13."""

    def setUp(self):
        self.use_home()

    def test_unreadable_file_reads_as_the_send_path_reads_it(self):
        # G4: an unreadable file decides nothing; Setup reads it as the default, as the send path does.
        self.write_state("{not json")
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        self.assertEqual(setup_steps.load_state(self.home)["mode"], approvals.mode(db, self.home))
        self.assertEqual(setup_steps.load_state(self.home)["mode"], "automatic")

    def test_an_unreadable_file_is_never_saved_over(self):
        # GL2: saving the default over it would turn a Review it held into a readable Automatic.
        for text in ("{not json", "[]", ""):
            self.write_state(text)
            for save in (lambda: setup_steps.record(self.home, "openrouter", "done", "OK"),
                         lambda: setup_steps.set_mode("automatic", self.home),
                         lambda: setup_steps.update_state(lambda state: None, self.home)):
                with self.assertRaises(setup_steps.StateFileUnreadable):
                    save()
                self.assertEqual((self.home / approvals.STATE_FILE).read_text(), text)
        (self.home / approvals.STATE_FILE).unlink()
        setup_steps.record(self.home, "openrouter", "done", "OK")   # a missing file is written as before
        self.assertEqual(setup_steps.load_state(self.home)["steps"]["openrouter"]["status"], "done")

    def test_install_mode_with_an_unreadable_database_reads_the_file(self):
        # G4: an unreadable database decides nothing; the chip shows what the file says.
        root = Path(tempfile.mkdtemp(prefix="sidedoor-root-"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / "state").mkdir()
        (root / approvals.DATABASE).write_bytes(b"not a database at all, just bytes" * 64)
        self.write_state(json.dumps({"mode": "automatic"}))
        self.assertEqual(approvals.install_mode(root, self.home), approvals.AUTOMATIC)
        self.write_state(json.dumps({"mode": "review"}))
        self.assertEqual(approvals.install_mode(root, self.home), approvals.REVIEW)

    def test_install_mode_reads_the_database_row_too(self):
        root = Path(tempfile.mkdtemp(prefix="sidedoor-root-"))
        self.addCleanup(shutil.rmtree, root, True)
        self.write_state(json.dumps({"mode": "automatic"}))
        self.assertEqual(approvals.install_mode(root, self.home), approvals.AUTOMATIC)
        (root / "state").mkdir()
        with contextlib.closing(sqlite3.connect(root / approvals.DATABASE)) as database:
            with database:
                approvals.store_mode(database, approvals.REVIEW)
        self.assertEqual(approvals.install_mode(root, self.home), approvals.REVIEW)


@unittest.skipUnless(importlib.util.find_spec("fastapi") and importlib.util.find_spec("httpx"),
                     "dashboard packages (fastapi, httpx) are not installed")
class DashboardModeRoutes(unittest.TestCase):
    """P6 and P13 through the real Settings and Setup routes."""

    setUp = settings_tests.SettingsPageTest.setUp
    client_for = settings_tests.SettingsPageTest.client_for
    tearDown = settings_tests.SettingsPageTest.tearDown

    def test_an_unreadable_setup_file_is_said_plainly_and_nothing_is_saved(self):
        # GL-M1: the file is checked before the database row changes, so "nothing was saved" is true.
        (self.root / "state").mkdir()
        with contextlib.closing(sqlite3.connect(self.root / approvals.DATABASE)) as database:
            with database:
                approvals.store_mode(database, approvals.AUTOMATIC)
        (self.home / approvals.STATE_FILE).write_text("{not json")
        response = self.client.post("/settings/mode", data={"mode": "review"}, headers=settings_tests.HTMX)
        self.assertEqual(response.status_code, 409)
        self.assertIn("could not read its setup file", response.text)
        self.assertIn("nothing was saved", response.text)
        self.assertEqual(response.headers.get("HX-Redirect"), "/setup-file")   # GL-M2: not a dead end
        self.assertEqual((self.home / approvals.STATE_FILE).read_text(), "{not json")
        with contextlib.closing(sqlite3.connect(self.root / approvals.DATABASE)) as database:
            self.assertEqual(approvals.stored_mode(database), approvals.AUTOMATIC)   # the row is unchanged
        response = self.client.post("/setup/mode", data={"mode": "review"}, headers=settings_tests.ORIGIN)
        self.assertEqual(response.status_code, 409)
        self.assertIn("Reset setup file", response.text)                         # the page offers the fix

    def test_reset_setup_file_from_settings_then_saving_works_again(self):
        (self.home / approvals.STATE_FILE).write_text("{not json")
        page = self.client.get("/settings").text
        self.assertIn('action="/setup-file/reset"', page)
        self.assertIn("Reset setup file", page)
        response = self.client.post("/setup-file/reset", headers=settings_tests.ORIGIN)
        self.assertEqual(response.status_code, 200)
        self.assertIn("kept the old one as", response.text)
        self.assertIn("Check these again in Settings:", response.text)
        self.assertIn("Start at login (optional), Start.", response.text)   # GL1: the reminder lists them
        kept = [path for path in self.home.iterdir() if path.name.startswith(approvals.STATE_FILE + ".broken-")]
        self.assertEqual([path.read_text() for path in kept], ["{not json"])
        self.assertNotIn('action="/setup-file/reset"', self.client.get("/settings").text)
        self.client.post("/settings/mode", data={"mode": "review"}, headers=settings_tests.HTMX)
        self.assertEqual(approvals.install_mode(self.root, self.home), approvals.REVIEW)
        self.assertEqual(setup_steps.load_state(self.home)["mode"], "review")

    def test_unknown_mode_shows_the_form_error_and_changes_nothing(self):
        self.client.post("/settings/mode", data={"mode": "review"}, headers=settings_tests.HTMX)
        for path in ("/settings/mode", "/setup/mode"):
            response = self.client.post(path, data={"mode": "bogus"}, headers=settings_tests.HTMX).text
            self.assertIn("Choose Automatic or Review.", response)
            self.assertEqual(setup_steps.load_state(self.home)["mode"], "review")

    def test_chip_and_section_show_the_database_review_mode(self):
        (self.root / "state").mkdir()
        with contextlib.closing(sqlite3.connect(self.root / approvals.DATABASE)) as database:
            with database:
                approvals.store_mode(database, approvals.REVIEW)
        page = self.client.get("/settings").text
        self.assertIn(">Review</a>", page)
        self.assertIn('value="review" checked', page)


class NoTestReadsTheRealHome(unittest.TestCase):
    """Review fix P15: every test module points SIDEDOOR_HOME at a temporary home."""

    def test_every_test_module_imports_profile_home(self):
        missing = [path.name for path in sorted(Path(__file__).parent.glob("test_*.py"))
                   if "\nimport profile_home" not in "\n" + path.read_text()]
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
