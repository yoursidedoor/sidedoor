"""LinkedIn Premium and invitation notes (owner, 2026-10-27): the Premium read at connect and before
every invitation, the last-known fallback, the note sent after acceptance when not Premium, and the
follow-up clock starting at that note. FakePort and in-memory SQLite; no network, no AI."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import datetime as dt
import hashlib
import json
import sqlite3
import unittest
import unittest.mock

import engagement
import outreach
import sequence
import setup_steps
from linkedin_port import FakePort, Person
from orchestrator import profile_key


ALICE = "https://www.linkedin.com/in/alice"
NOTE = "Hi Alice, I read your post on data contracts. Would you be open to a short chat?"


def forget_premium():
    setup_steps.update_state(lambda state: state.pop(setup_steps.PREMIUM_KEY, None))


class Base(unittest.TestCase):
    def setUp(self):
        forget_premium()
        self.addCleanup(forget_premium)
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        outreach.initialize_actions(self.db)
        self.db.commit()
        self.port = FakePort()
        self.alice = self.port.add_person(ALICE)
        pacing = unittest.mock.patch("pacing.check", return_value={"go": True})
        pacing.start()
        self.addCleanup(pacing.stop)

    def action(self, action_id="invite-1", profile=ALICE, provider_id=None):
        payload = {"action_id": action_id, "purpose": "INVITE", "provider": "unipile", "channel": "linkedin",
                   "profile": profile, "recipient": "Alice Example", "company": "Acme", "provider_id": provider_id or self.alice,
                   "note": NOTE, "message": NOTE,
                   "verification": {"content_verified": True, "content_sources": ["rw_drafts:alice"],
                                    "identity_verified": True, "identity_source": "research:1",
                                    "team_relevance_verified": True, "team_source": "research:1",
                                    "verified_at": "2026-10-01T00:00:00+00:00"}}
        text = json.dumps(payload, sort_keys=True)
        self.db.execute("INSERT INTO outreach_actions(action_id,identity,slot,payload,payload_hash,state) VALUES (?,?,?,?,?,?)",
                        (action_id, profile_key(profile), "INVITE", text, hashlib.sha256(text.encode()).hexdigest(),
                         "REQUEST_STARTED"))
        self.db.commit()
        return action_id

    def sent_note(self):
        return self.port.invitations[-1]["note"]

    def invite_and_confirm(self):
        sequence.send_invite(self.db, self.port, self.action())
        self.assertEqual(sequence.reconcile_invite(self.db, self.port, "invite-1"), sequence.CONFIRMED)

    def notes_queued(self):
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT payload FROM outreach_actions WHERE json_extract(payload,'$.purpose')='FIRST_MESSAGE'")]


class PortRead(unittest.TestCase):
    def test_own_premium_and_a_note_less_invitation_over_unipile(self):
        import unipile_port
        calls = []

        def transport(method, path, *, query=None, body=None, multipart=None, timeout=None):
            calls.append((method, path, query, body))
            if path == "/users/me":
                return 200, answers.pop(0)
            return 201, {"object": "UserInvitationSent"}
        answers = [{"premium": True}, {"premium": False}, {"provider_id": "X"}]
        port = unipile_port.UnipilePort(transport, account_id="acct-1")
        self.assertEqual([port.own_premium() for unused in range(3)], [True, False, None])
        self.assertEqual(calls[0][:3], ("GET", "/users/me", {"account_id": "acct-1"}))
        port.send_invitation("P1", None)
        self.assertEqual(calls[-1][3], {"provider_id": "P1", "account_id": "acct-1"})


class PremiumRead(Base):
    def test_1_read_when_linkedin_connects(self):
        def transport(method, path, **named):
            if path == "/accounts":
                return 200, {"items": [{"id": "acct-1", "type": "LINKEDIN", "name": "Owner", "sources": [{"status": "OK"}],
                                        "connection_params": {"im": {"publicIdentifier": "owner"}}}], "cursor": None}
            if path == "/users/me":
                return 200, {"provider_id": "OWNER1", "premium": True}
            return 404, {}
        setup_steps.update_state(lambda state: state.update(linkedin_link_started_at=setup_steps.now_iso(),
                                                            linkedin_link_kind="create"))
        with unittest.mock.patch.object(setup_steps, "license_key", return_value="LIC-TEST"):
            state = setup_steps.linkedin_status(transport=transport)
        self.assertEqual(state["steps"]["linkedin"]["status"], "done")
        self.assertIs(setup_steps.saved_premium()["premium"], True)

    def test_1_read_before_every_invitation(self):
        self.port.premium = True
        sequence.send_invite(self.db, self.port, self.action())
        self.assertEqual(self.sent_note(), NOTE)
        self.assertIs(setup_steps.saved_premium()["premium"], True)

    def test_2_a_failed_read_uses_the_last_known_value(self):
        setup_steps.check_premium(self.port)              # saves True
        self.port.failures["own_premium"] = ["timeout"]
        sequence.send_invite(self.db, self.port, self.action())
        self.assertEqual(self.sent_note(), NOTE)

    def test_2_never_read_and_failing_is_not_premium_and_still_sends(self):
        self.port.failures["own_premium"] = ["timeout"]
        self.assertEqual(sequence.send_invite(self.db, self.port, self.action()), sequence.ACKED)
        self.assertIsNone(self.sent_note())
        self.port.premium = None                          # an answer without the flag: the same
        self.assertIs(setup_steps.check_premium(self.port), False)


class NoteAfterAcceptance(Base):
    def setUp(self):
        super().setUp()
        self.port.premium = False

    def test_3_4_no_note_on_the_invite_then_the_exact_note_once_after_acceptance(self):
        self.invite_and_confirm()
        self.assertIsNone(self.sent_note())
        planned = engagement.next_action(self.db, ALICE)
        self.assertEqual((planned["state"], planned["blocker"]), ("BLOCKED", "NOTE_WAITS_FOR_ACCEPTANCE"))
        self.assertEqual(sequence.queue_deferred_notes(self.db, self.port)["items"][0]["acceptance"], "PENDING")
        self.assertEqual(self.notes_queued(), [])
        self.port.accept(self.alice)
        later = engagement.timestamp(setup_steps.now_iso()) + dt.timedelta(hours=2)
        sequence.queue_deferred_notes(self.db, self.port, now=later)   # after the 1 h poll wait
        [note] = self.notes_queued()
        self.assertEqual((note["message"], note["provider"], note["conversation_id"]), (NOTE, "unipile", None))
        due = engagement.next_action(self.db, ALICE)
        self.assertEqual((due["state"], due["purpose"]), ("DUE", "FIRST_MESSAGE"))
        again = sequence.queue_deferred_notes(self.db, self.port, now=later)["items"][0]
        self.assertEqual(again["state"], "ALREADY_QUEUED")
        self.assertEqual(len(self.notes_queued()), 1)

    def test_3_no_note_when_they_already_replied(self):
        self.invite_and_confirm()
        self.port.accept(self.alice)
        engagement.record_event(self.db, {"source": "unipile", "source_id": "inbound:1", "profile": ALICE,
            "kind": "inbound", "occurred_at": setup_steps.now_iso(), "observed_at": setup_steps.now_iso(),
            "evidence": "test", "raw": {}})
        self.db.commit()
        sequence.queue_deferred_notes(self.db, self.port)
        self.assertEqual(self.notes_queued(), [])

    def test_5_premium_ended_while_queued_sends_without_the_note(self):
        self.port.premium = True
        action_id = self.action()                         # queued with its note while Premium
        self.port.premium = False
        sequence.send_invite(self.db, self.port, action_id)
        self.assertIsNone(self.sent_note())
        sequence.reconcile_invite(self.db, self.port, action_id)
        invite = engagement.events_for(self.db, ALICE)[0]
        self.assertTrue(sequence.deferred_invite(invite))

    def test_premium_invite_is_as_today(self):
        self.port.premium = True
        self.invite_and_confirm()
        self.assertEqual(self.sent_note(), NOTE)
        self.assertFalse(sequence.deferred_invite(engagement.events_for(self.db, ALICE)[0]))
        self.assertEqual(engagement.next_action(self.db, ALICE)["purpose"], "FOLLOWUP")

    def test_6_the_follow_up_clock_starts_at_the_note(self):
        self.invite_and_confirm()
        self.port.accept(self.alice)
        sequence.record_acceptance(self.db, self.port, ALICE)
        later = engagement.timestamp(setup_steps.now_iso()) + dt.timedelta(days=4)
        engagement.record_event(self.db, {"source": "unipile", "source_id": "note:1", "profile": ALICE,
            "kind": "initial_sent", "purpose": "FIRST_MESSAGE", "verified": True, "occurred_at": later.isoformat(),
            "observed_at": later.isoformat(), "evidence": "test", "raw": {}})
        self.db.commit()
        planned = engagement.next_action(self.db, ALICE, now=later)
        self.assertEqual((planned["purpose"], planned["ordinal"]), ("FOLLOWUP", 1))
        self.assertEqual(planned["due_at"], engagement.business_days_after(later, 3).isoformat())


class ReviewFixes(Base):
    def setUp(self):
        super().setUp()
        self.port.premium = False
        self.now = engagement.timestamp(setup_steps.now_iso())

    def polls(self):
        return self.db.execute("SELECT COUNT(*) FROM outreach_agent_audit WHERE phase=? AND kind='ACCEPTANCE_POLL'",
                               (sequence.NOTE_PHASE,)).fetchone()[0]

    def test_p1_acceptance_reads_back_off_like_followups(self):
        self.invite_and_confirm()
        sequence.queue_deferred_notes(self.db, self.port, now=self.now)
        sequence.queue_deferred_notes(self.db, self.port, now=self.now + dt.timedelta(minutes=59))
        self.assertEqual(self.polls(), 1)                 # 1 h after the first read
        sequence.queue_deferred_notes(self.db, self.port, now=self.now + dt.timedelta(hours=1))
        sequence.queue_deferred_notes(self.db, self.port, now=self.now + dt.timedelta(hours=2, minutes=59))
        self.assertEqual(self.polls(), 2)                 # then 2 h
        sequence.queue_deferred_notes(self.db, self.port, now=self.now + dt.timedelta(hours=3))
        self.assertEqual(self.polls(), 3)

    def test_p1_a_rate_limit_or_dead_session_stops_the_pass(self):
        import unipile_port
        self.invite_and_confirm()
        bob = "https://www.linkedin.com/in/bob"
        self.action("invite-2", bob, self.port.add_person(bob))
        sequence.send_invite(self.db, self.port, "invite-2")
        sequence.reconcile_invite(self.db, self.port, "invite-2")
        with unittest.mock.patch.object(sequence, "record_acceptance",
                                        side_effect=unipile_port.UnipileHTTPError(429, "rate")) as read:
            result = sequence.queue_deferred_notes(self.db, self.port, now=self.now)
        self.assertEqual(read.call_count, 1)              # the second person is not read this cycle
        self.assertEqual(result["state"], "STOPPED")
        self.assertEqual(result["items"][1]["state"], "WAIT_ACCEPTANCE")

    def test_p3_a_refused_note_is_done_and_the_clock_starts_at_acceptance(self):
        self.invite_and_confirm()
        self.port.accept(self.alice)
        sequence.queue_deferred_notes(self.db, self.port, now=self.now)
        [note] = self.notes_queued()
        self.db.execute("UPDATE outreach_actions SET state='BLOCKED',result=? WHERE action_id=?",
                        (json.dumps(["INITIAL_ALREADY_SENT_OR_UNKNOWN"]), note["action_id"]))
        self.db.commit()
        sequence.queue_deferred_notes(self.db, self.port, now=self.now)
        state = self.db.execute("SELECT state FROM outreach_actions WHERE action_id=?", (note["action_id"],)).fetchone()[0]
        self.assertEqual(state, "CANCELLED_BEFORE_SEND")  # never dispatched again
        accepted = next(event for event in engagement.events_for(self.db, ALICE) if event["kind"] == "accepted")
        planned = engagement.next_action(self.db, ALICE, now=self.now)
        self.assertEqual((planned["purpose"], planned["ordinal"]), ("FOLLOWUP", 1))
        self.assertEqual(planned["due_at"], engagement.business_days_after(accepted["occurred_at"], 3).isoformat())

    def test_p3_only_real_refusals_count(self):
        refused = lambda state, result: sequence._note_refused(self.db, "n", state, result)
        self.assertFalse(refused("BLOCKED", ["INSPECTION_TIMEOUT"]))
        self.assertFalse(refused("BLOCKED", ["DIRECT_HISTORY_MISSING_OR_STALE", "ACTION_PATH_NOT_ENABLED"]))
        self.assertFalse(refused("BLOCKED", ["AGENT_DECISION_NOT_READY"]))   # no BLOCKED objection on record
        self.assertTrue(refused("BLOCKED", ["HAS_REPLY_PRESENT"]))
        self.assertTrue(refused("CANCELLED_BEFORE_SEND", {"owner_rejection": 1}))
        self.assertTrue(refused("CANCELLED_BEFORE_SEND", {"provider_guard": {"gates": ["PROVIDER_ID_UNKNOWN"]}}))
        self.assertTrue(refused("CONFIRMED_NOT_SENT", {}))   # terminal: done, never re-queued

    def test_p3_a_reply_or_undecodable_inbox_gate_ends_the_note(self):
        for gate in ("REPLY_REQUIRES_CONTEXT", "INBOX_CONVERSATION_UNDECODABLE"):
            self.assertTrue(sequence._note_refused(self.db, "n", "BLOCKED", [gate]), gate)

    def test_p3_a_sender_ready_block_keeps_the_note_for_later(self):
        self.invite_and_confirm()
        self.port.accept(self.alice)
        sequence.queue_deferred_notes(self.db, self.port, now=self.now)
        [note] = self.notes_queued()
        self.db.execute("UPDATE outreach_actions SET state='BLOCKED',result=? WHERE action_id=?",
                        (json.dumps(["SENDER_READY_MISSING"]), note["action_id"]))
        self.db.commit()
        sequence.queue_deferred_notes(self.db, self.port, now=self.now)
        state = self.db.execute("SELECT state FROM outreach_actions WHERE action_id=?", (note["action_id"],)).fetchone()[0]
        self.assertEqual(state, "BLOCKED")                # outreach.cycle takes BLOCKED rows again
        planned = engagement.next_action(self.db, ALICE, now=self.now)
        self.assertEqual((planned["state"], planned["purpose"]), ("DUE", "FIRST_MESSAGE"))

    def test_p4_a_noted_attempt_that_may_have_landed_never_defers_the_note(self):
        self.port.premium = True
        self.port.fail("send_invitation", "timeout_after_write")   # attempt 1 lands with the note
        sequence.send_invite(self.db, self.port, self.action())
        self.port.premium = False
        # The absence read lags: the retry is made, and the provider refuses it as already existing.
        with unittest.mock.patch("linkedin_port.invitation_absent", return_value=True), \
                unittest.mock.patch.object(sequence, "acceptance", return_value=("NOT_PENDING", Person(ALICE, self.alice, 2, False, {}))):
            sequence.reconcile_invite(self.db, self.port, "invite-1")
        attempts = json.loads(self.db.execute("SELECT result FROM outreach_actions").fetchone()[0])["attempts"]
        self.assertEqual([(item["note"], item["outcome"]) for item in attempts],
                         [(NOTE, "UNCERTAIN"), (None, "REFUSED_INVITATION_EXISTS")])
        self.assertFalse(sequence.note_deferred({"note": NOTE}, attempts))

    def test_p5_the_premium_read_has_a_ten_second_timeout(self):
        import unipile_port
        seen = []

        def transport(method, path, *, query=None, body=None, multipart=None, timeout=None):
            seen.append(timeout)
            return 200, {"premium": True}
        self.assertIs(unipile_port.UnipilePort(transport, account_id="a").own_premium(), True)
        self.assertEqual(seen, [10])


if __name__ == "__main__":
    unittest.main()
