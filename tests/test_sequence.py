"""E3.T3 option A: INVITE action, acceptance and the lead view over FakePort, with a fake clock for timing."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
import hashlib
import json
import sqlite3
import unittest
import unittest.mock

import engagement
import outreach
import sequence
from linkedin_port import FakePort
from orchestrator import profile_key


ALICE = "https://www.linkedin.com/in/alice"


class Base(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        outreach.initialize_actions(self.db)
        self.db.commit()
        self.port = FakePort()
        self.alice = self.port.add_person(ALICE)
        # Pacing has its own tests; here it says go unless a test checks the wait itself.
        self.pacing = unittest.mock.patch("pacing.check", return_value={"go": True})
        self.pacing.start()
        self.addCleanup(self.pacing.stop)

    def action(self, action_id="invite-1", profile=ALICE, provider_id=None):
        payload = {"action_id": action_id, "purpose": "INVITE", "provider": "unipile", "channel": "linkedin",
                   "profile": profile, "recipient": "Alice Example", "provider_id": provider_id or self.alice,
                   "note": "Hi Alice, a short note."}
        text = json.dumps(payload, sort_keys=True)
        self.db.execute("INSERT INTO outreach_actions(action_id,identity,slot,payload,payload_hash,state) VALUES (?,?,?,?,?,?)",
                        (action_id, profile_key(profile), "INVITE", text, hashlib.sha256(text.encode()).hexdigest(),
                         "REQUEST_STARTED"))
        self.db.commit()
        return action_id

    def state(self, action_id="invite-1"):
        return self.db.execute("SELECT state FROM outreach_actions WHERE action_id=?", (action_id,)).fetchone()[0]

    def kinds(self):
        return [event["kind"] for event in engagement.events_for(self.db, profile_key(ALICE))]


class Invite(Base):
    def test_ack_then_read_confirms_and_records_initial(self):
        self.assertEqual(sequence.send_invite(self.db, self.port, self.action()), sequence.ACKED)
        self.assertEqual(self.kinds(), [])  # an acknowledgement is not delivery
        self.assertEqual(sequence.reconcile_invite(self.db, self.port, "invite-1"), sequence.CONFIRMED)
        self.assertEqual(self.kinds(), ["initial_sent"])
        self.assertEqual(len(self.port.invitations), 1)
        self.assertEqual(sequence.sequence_view(self.db, ALICE)["stage"], "INVITED")

    def test_uncertain_write_that_landed_is_confirmed_not_resent(self):
        self.port.fail("send_invitation", "timeout_after_write")
        self.assertEqual(sequence.send_invite(self.db, self.port, self.action()), sequence.RETRY_WAIT)
        self.assertEqual(sequence.reconcile_invite(self.db, self.port, "invite-1"), sequence.CONFIRMED)
        self.assertEqual(self.port.calls.count("send_invitation"), 1)
        self.assertEqual(len(self.port.invitations), 1)

    def test_uncertain_write_retried_at_most_three_times_after_absence(self):
        self.port.fail("send_invitation", "timeout", times=5)
        self.assertEqual(sequence.send_invite(self.db, self.port, self.action()), sequence.RETRY_WAIT)
        self.assertEqual(sequence.reconcile_invite(self.db, self.port, "invite-1"), sequence.RETRY_WAIT)
        self.assertEqual(sequence.reconcile_invite(self.db, self.port, "invite-1"), sequence.RETRY_WAIT)
        self.assertEqual(sequence.reconcile_invite(self.db, self.port, "invite-1"), sequence.NOT_SENT)
        self.assertEqual(self.port.calls.count("send_invitation"), 3)
        self.assertEqual(self.kinds(), [])

    def test_unreadable_state_waits_without_resending(self):
        self.port.fail("send_invitation", "timeout")
        sequence.send_invite(self.db, self.port, self.action())
        self.port.fail("lookup", "timeout")
        self.assertEqual(sequence.reconcile_invite(self.db, self.port, "invite-1"), sequence.RETRY_WAIT)
        self.port.fail("sent_invitations", "incomplete_page", times=2)
        self.assertEqual(sequence.reconcile_invite(self.db, self.port, "invite-1"), sequence.RETRY_WAIT)
        self.assertEqual(self.port.calls.count("send_invitation"), 1)

    def test_gates_refuse_connected_pending_and_received(self):
        self.port.add_invitation(self.alice)
        self.assertEqual(sequence.send_invite(self.db, self.port, self.action()), "CANCELLED_BEFORE_SEND")
        bob = "https://www.linkedin.com/in/bob"
        bob_id = self.port.add_person(bob, distance=1)
        self.assertEqual(sequence.send_invite(self.db, self.port, self.action("invite-2", bob, bob_id)),
                         "CANCELLED_BEFORE_SEND")
        person = sequence.linkedin_port.Person(ALICE, "x", 2, False, {"invitation": {"type": "RECEIVED"}})
        self.assertIn("INVITATION_RECEIVED_FROM_THEM", sequence.invite_gates(person))
        self.assertEqual(self.port.calls.count("send_invitation"), 0)

    def test_request_is_consumed_once(self):
        sequence.send_invite(self.db, self.port, self.action())
        with self.assertRaises(ValueError):
            sequence.send_invite(self.db, self.port, "invite-1")


def unclear(port):
    """The port's lookup with the invitation state unclear: pending None, distance "?"."""
    real = port.lookup

    def lookup(profile):
        person = real(profile)
        return sequence.linkedin_port.Person(person.profile, person.provider_id, "?", None, person.raw)
    return lookup


class MatchLive(Base):
    """G1 (owner, 2026-10-22: "keep guards consistent with what exists in live harness today"): on a
    first send an unclear profile read decides nothing; before a retry it still refuses."""

    # Worked example 1
    def test_first_invite_with_an_unclear_read_is_sent(self):
        with unittest.mock.patch.object(self.port, "lookup", side_effect=unclear(self.port)):
            self.assertEqual(sequence.send_invite(self.db, self.port, self.action()), sequence.ACKED)
        self.assertEqual(self.port.calls.count("send_invitation"), 1)

    def test_what_the_read_did_show_still_refuses_a_first_send(self):
        self.port.add_invitation(self.alice)
        real = self.port.lookup

        def pending_shown(profile):
            person = real(profile)
            return sequence.linkedin_port.Person(person.profile, person.provider_id, "?", True, person.raw)
        with unittest.mock.patch.object(self.port, "lookup", side_effect=pending_shown):
            self.assertEqual(sequence.send_invite(self.db, self.port, self.action()), "CANCELLED_BEFORE_SEND")
        self.assertEqual(self.port.calls.count("send_invitation"), 0)

    def test_unclear_values_refuse_only_before_a_retry(self):
        person = sequence.linkedin_port.Person(ALICE, "x", "?", None, {})
        self.assertEqual(sequence.invite_gates(person), [])
        self.assertEqual(sequence.invite_gates(person, retry=True), ["PROFILE_INVITATION_STATE_UNKNOWN"])

    def test_a_refused_send_on_an_unclear_read_is_never_recorded_as_ours(self):
        # GL1: the owner invited Alice by hand; our read was unclear; the provider refuses our POST.
        self.port.add_invitation(self.alice)
        with unittest.mock.patch.object(self.port, "lookup", side_effect=unclear(self.port)):
            self.assertEqual(sequence.send_invite(self.db, self.port, self.action()), sequence.RETRY_WAIT)
        self.assertEqual(sequence.reconcile_invite(self.db, self.port, "invite-1"), sequence.NOT_SENT)
        result = json.loads(self.db.execute("SELECT result FROM outreach_actions").fetchone()[0])
        self.assertEqual((result["not_sent"]["reason"], result["not_sent"]["by"]), ("ALREADY_CONTACTED_ELSEWHERE", "PENDING"))
        self.assertEqual(self.kinds(), [])   # no initial_sent
        self.port.accept(self.alice)
        self.assertEqual(sequence.reconcile_invite(self.db, self.port, "invite-1"), sequence.NOT_SENT)
        view = sequence.sequence_view(self.db, ALICE)
        self.assertEqual(view["stage"], "INVITE_" + sequence.NOT_SENT)
        self.assertEqual(self.kinds(), [])   # nothing to follow up
        self.assertEqual(self.port.calls.count("send_invitation"), 1)

    def test_an_explicit_already_connected_refusal_on_an_unclear_read_is_never_ours(self):
        bob = "https://www.linkedin.com/in/bob"
        bob_id = self.port.add_person(bob, distance=1)
        refused = sequence.linkedin_port.InvitationExists("ALREADY_CONNECTED")
        with unittest.mock.patch.object(self.port, "lookup", side_effect=unclear(self.port)), \
                unittest.mock.patch.object(self.port, "send_invitation", side_effect=refused):
            self.assertEqual(sequence.send_invite(self.db, self.port, self.action("invite-2", bob, bob_id)),
                             sequence.RETRY_WAIT)
        self.assertEqual(sequence.reconcile_invite(self.db, self.port, "invite-2"), sequence.NOT_SENT)
        self.assertEqual([event["kind"] for event in engagement.events_for(self.db, profile_key(bob))], [])

    def test_gm1_a_clear_read_then_an_explicit_refusal_is_never_ours(self):
        # GM1: the explicit refusal decides, whatever the pre-send read showed.
        refused = sequence.linkedin_port.InvitationExists("ALREADY_INVITED")
        with unittest.mock.patch.object(self.port, "send_invitation", side_effect=refused):
            self.assertEqual(sequence.send_invite(self.db, self.port, self.action()), sequence.RETRY_WAIT)
        self.port.add_invitation(self.alice)   # the invitation someone else made, now visible
        self.assertEqual(sequence.reconcile_invite(self.db, self.port, "invite-1"), sequence.NOT_SENT)
        result = json.loads(self.db.execute("SELECT result FROM outreach_actions").fetchone()[0])
        self.assertEqual(result["not_sent"]["reason"], "ALREADY_CONTACTED_ELSEWHERE")
        self.assertEqual(self.kinds(), [])
        self.assertEqual(sequence.sequence_view(self.db, ALICE)["stage"], "INVITE_" + sequence.NOT_SENT)

    def test_gm1_a_clear_read_then_already_connected_and_accepted_is_never_ours(self):
        refused = sequence.linkedin_port.InvitationExists("ALREADY_CONNECTED")
        with unittest.mock.patch.object(self.port, "send_invitation", side_effect=refused):
            sequence.send_invite(self.db, self.port, self.action())
        self.port.accept(self.alice)
        self.assertEqual(sequence.reconcile_invite(self.db, self.port, "invite-1"), sequence.NOT_SENT)
        self.assertEqual(self.kinds(), [])

    def test_gm1_a_timeout_with_a_clear_read_stays_ours(self):
        self.port.fail("send_invitation", "timeout_after_write")
        self.assertEqual(sequence.send_invite(self.db, self.port, self.action()), sequence.RETRY_WAIT)
        self.assertEqual(sequence.reconcile_invite(self.db, self.port, "invite-1"), sequence.CONFIRMED)
        self.assertEqual(self.kinds(), ["initial_sent"])

    def test_a_timeout_on_an_unclear_read_then_pending_stays_ours(self):
        # GL-I1 (option a): the POST may have reached LinkedIn, so a later PENDING is our send, as live.
        self.port.fail("send_invitation", "timeout_after_write")
        with unittest.mock.patch.object(self.port, "lookup", side_effect=unclear(self.port)):
            self.assertEqual(sequence.send_invite(self.db, self.port, self.action()), sequence.RETRY_WAIT)
        self.assertEqual(sequence.reconcile_invite(self.db, self.port, "invite-1"), sequence.CONFIRMED)
        self.assertEqual(self.kinds(), ["initial_sent"])

    def test_an_unknown_error_on_an_unclear_read_then_connected_stays_ours(self):
        with unittest.mock.patch.object(self.port, "lookup", side_effect=unclear(self.port)), \
                unittest.mock.patch.object(self.port, "send_invitation", side_effect=RuntimeError("reset")):
            self.assertEqual(sequence.send_invite(self.db, self.port, self.action()), sequence.RETRY_WAIT)
        self.port.accept(self.alice)
        self.assertEqual(sequence.reconcile_invite(self.db, self.port, "invite-1"), sequence.CONFIRMED)
        self.assertEqual(self.kinds(), ["initial_sent"])

    # Worked example 2
    def test_a_retry_with_the_same_unclear_read_is_refused(self):
        self.port.fail("send_invitation", "timeout")
        sequence.send_invite(self.db, self.port, self.action())
        with unittest.mock.patch.object(self.port, "lookup", side_effect=unclear(self.port)):
            self.assertEqual(sequence.reconcile_invite(self.db, self.port, "invite-1"), sequence.RETRY_WAIT)
        self.assertEqual(self.port.calls.count("send_invitation"), 1)
        # The retry's own gate: the reconcile read unclear, the absence read clear -> still refused.
        real, reads = self.port.lookup, []

        def first_unclear(profile):
            reads.append(profile)
            return sequence.linkedin_port.Person(profile, self.alice, "?", None, {}) if len(reads) == 1 else real(profile)
        with unittest.mock.patch.object(self.port, "lookup", side_effect=first_unclear):
            self.assertEqual(sequence.reconcile_invite(self.db, self.port, "invite-1"), "CANCELLED_BEFORE_SEND")
        self.assertEqual(self.port.calls.count("send_invitation"), 1)
        guard = json.loads(self.db.execute("SELECT result FROM outreach_actions").fetchone()[0])["provider_guard"]
        self.assertEqual(guard["gates"], ["PROFILE_INVITATION_STATE_UNKNOWN"])


class OwnerApprovedFixes(Base):
    def test_retry_rechecks_received_invitation(self):
        self.port.fail("send_invitation", "timeout")
        sequence.send_invite(self.db, self.port, self.action())
        self.port.people[profile_key(ALICE)]["invitation"] = {"type": "RECEIVED", "status": "PENDING"}
        self.assertEqual(sequence.reconcile_invite(self.db, self.port, "invite-1"), "CANCELLED_BEFORE_SEND")
        self.assertEqual(self.port.calls.count("send_invitation"), 1)

    def test_stuck_request_started_is_read_before_any_retry(self):
        action_id = self.action()
        started = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat()
        self.db.execute("UPDATE outreach_actions SET result=? WHERE action_id=?",
                        (json.dumps({"attempts": [{"started_at": started, "provider_id": self.alice}]}), action_id))
        self.db.commit()
        self.port.add_invitation(self.alice)  # the interrupted request had landed
        out = sequence.reconcile_invites(self.db, self.port)
        self.assertEqual(out["recovered"], [action_id])
        self.assertEqual(self.state(), sequence.CONFIRMED)
        self.assertEqual(self.port.calls.count("send_invitation"), 0)

    def test_fresh_request_started_is_left_alone(self):
        self.action()
        self.assertEqual(sequence.recover_stuck(self.db), [])

    def test_uncertain_row_without_attempt_is_retried_after_absence(self):
        action_id = self.action()
        self.db.execute("UPDATE outreach_actions SET state='UNCERTAIN_NO_RETRY' WHERE action_id=?", (action_id,))
        self.db.commit()
        sequence.reconcile_invites(self.db, self.port)
        self.assertEqual(self.port.calls.count("send_invitation"), 1)


class ReviewRound2Fixes(Base):
    def test_retry_waits_for_pacing(self):
        self.pacing.stop()
        self.port.fail("send_invitation", "timeout")
        sequence.send_invite(self.db, self.port, self.action())
        saturday = dt.datetime(2026, 10, 17, 9, 30, tzinfo=dt.timezone.utc)  # 02:30 Pacific
        state = sequence.reconcile_invite(self.db, self.port, "invite-1", {"timezone": "America/Los_Angeles"}, saturday)
        self.assertEqual(state, sequence.RETRY_WAIT)
        self.assertEqual(self.port.calls.count("send_invitation"), 1)

    def test_sent_but_unsaved_invite_is_recovered_and_confirmed(self):
        action_id = self.action()
        started = dt.datetime.now(dt.timezone.utc).isoformat()
        self.db.execute("UPDATE outreach_actions SET state='UNCERTAIN_NO_RETRY',result=? WHERE action_id=?",
                        (json.dumps({"attempts": [{"started_at": started, "provider_id": self.alice}]}), action_id))
        self.db.commit()
        self.port.add_invitation(self.alice)  # it went out; only saving ACKED failed
        out = sequence.reconcile_invites(self.db, self.port)
        self.assertEqual((out["recovered"], self.state()), ([action_id], sequence.CONFIRMED))
        self.assertEqual(self.port.calls.count("send_invitation"), 0)


class StuckDispatching(Base):
    """A claim left in DISPATCHING (the process died before execute_claimed) is recovered by the
    same rule as a stuck REQUEST_STARTED (owner, 2026-10-21: "recover it")."""
    def dispatching(self, age=dt.timedelta(hours=1)):
        action_id = self.action()
        claimed = (dt.datetime.now(dt.timezone.utc) - age).isoformat()
        self.db.execute("UPDATE outreach_actions SET state='DISPATCHING',claimed_at=?,result=? WHERE action_id=?",
                        (claimed, json.dumps({"preflight": {}}), action_id))
        self.db.commit()
        return action_id

    def test_invitation_already_there_is_never_sent(self):
        action_id = self.dispatching()
        self.port.add_invitation(self.alice)  # not ours: nothing was sent from DISPATCHING
        out = sequence.reconcile_invites(self.db, self.port)
        self.assertEqual((out["recovered"], self.state()), ([action_id], "CANCELLED_BEFORE_SEND"))
        self.assertEqual(self.port.calls.count("send_invitation"), 0)
        self.assertEqual(self.kinds(), [])

    def test_absent_invitation_is_sent_once(self):
        action_id = self.dispatching()
        out = sequence.reconcile_invites(self.db, self.port)
        self.assertEqual((out["recovered"], self.state()), ([action_id], sequence.ACKED))
        self.assertEqual(self.port.calls.count("send_invitation"), 1)
        sequence.reconcile_invites(self.db, self.port)
        self.assertEqual((self.state(), self.port.calls.count("send_invitation")), (sequence.CONFIRMED, 1))

    def test_fresh_dispatching_is_left_alone(self):
        self.dispatching(age=dt.timedelta(minutes=1))
        self.assertEqual(sequence.recover_stuck(self.db), [])
        self.assertEqual(self.state(), "DISPATCHING")

    def test_unreadable_state_waits_without_sending(self):
        self.dispatching()
        self.port.fail("lookup", "timeout")
        sequence.reconcile_invites(self.db, self.port)
        self.assertEqual(self.state(), sequence.RETRY_WAIT)
        self.port.fail("sent_invitations", "incomplete_page", times=2)
        sequence.reconcile_invites(self.db, self.port)
        self.assertEqual((self.state(), self.port.calls.count("send_invitation")), (sequence.RETRY_WAIT, 0))


class Lifecycle(Base):
    def test_invite_accept_followups_and_stop_on_reply_on_a_fake_clock(self):
        sequence.send_invite(self.db, self.port, self.action())
        sequence.reconcile_invite(self.db, self.port, "invite-1")
        sent = engagement.timestamp(engagement.events_for(self.db, profile_key(ALICE))[0]["occurred_at"])
        self.assertEqual(sequence.record_acceptance(self.db, self.port, ALICE), "PENDING")
        self.port.accept(self.alice)
        self.assertEqual(sequence.record_acceptance(self.db, self.port, ALICE), "ACCEPTED")
        self.assertEqual(sequence.sequence_view(self.db, ALICE)["stage"], "ACCEPTED")
        first_due = engagement.business_days_after(sent, 3)
        early = sequence.sequence_view(self.db, ALICE, now=first_due - dt.timedelta(minutes=1))["next"]
        self.assertEqual((early["state"], early["purpose"], early["ordinal"]), ("WAITING", "FOLLOWUP", 1))
        due = sequence.sequence_view(self.db, ALICE, now=first_due)["next"]
        self.assertEqual(due["state"], "DUE")
        # The follow-up send is the existing path; its verified event moves the plan to follow-up two.
        engagement.record_event(self.db, {"source": "unipile", "source_id": "followup:1", "profile": ALICE,
            "kind": "followup_sent", "verified": True, "occurred_at": first_due.isoformat(),
            "observed_at": first_due.isoformat(), "evidence": "test", "raw": {}})
        second = sequence.sequence_view(self.db, ALICE, now=first_due)
        self.assertEqual((second["stage"], second["next"]["ordinal"]), ("FOLLOWUP_1", 2))
        self.assertEqual(second["next"]["due_at"], engagement.business_days_after(first_due, 5).isoformat())
        engagement.record_event(self.db, {"source": "unipile", "source_id": "inbound:1", "profile": ALICE,
            "kind": "inbound", "occurred_at": first_due.isoformat(), "observed_at": first_due.isoformat(),
            "evidence": "test", "raw": {}})
        stopped = sequence.sequence_view(self.db, ALICE, now=first_due + dt.timedelta(days=30))
        self.assertEqual(stopped["stage"], "REPLIED")
        self.assertNotEqual(stopped["next"].get("state"), "DUE")


if __name__ == "__main__":
    unittest.main()
