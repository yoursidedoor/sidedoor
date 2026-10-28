"""Owner's reply changes of 2026-09-30: backlog cutoff, any-label replies, idempotent retry."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
import json
import engagement
import unittest
from unittest.mock import patch

import manual_reply_execution as manual
import outreach
import reply_execution
import reply_worker
import synthetic_resume  # noqa: F401 (synthetic designated resume; the owner's is not in the repo)


OWNER_CUTOFF = dt.datetime(2026, 9, 30, 18, 0, tzinfo=dt.timezone.utc)


class ReplyBacklogCutoffTests(unittest.TestCase):
    def test_cutoff_is_the_owner_release_instant(self):
        self.assertEqual(OWNER_CUTOFF, reply_execution.BACKLOG_CUTOFF)
        self.assertTrue(reply_execution.backlog("2026-09-19T22:32:50.777763+00:00"))
        self.assertTrue(reply_execution.backlog("2026-02-23T04:15:43.777793+00:00"))
        # HeyReach times drift forward by seconds, so the drift window after the cutoff is backlog.
        self.assertTrue(reply_execution.backlog("2026-09-30T17:59:59.900+00:00"))
        self.assertTrue(reply_execution.backlog("2026-09-30T18:00:03.200+00:00"))
        self.assertTrue(reply_execution.backlog("2026-09-30T18:09:59.999+00:00"))
        self.assertFalse(reply_execution.backlog("2026-09-30T18:10:00+00:00"))
        self.assertFalse(reply_execution.backlog("2026-10-01T09:00:00-07:00"))

    def test_backlog_decision_is_never_promoted_or_sent(self):
        # A decision drafted before the worker skipped the backlog (the 60 on file, 2026-09-30).
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            fixture.seed()
            fixture.classify()
            with patch.object(reply_execution, "BACKLOG_CUTOFF", OWNER_CUTOFF):
                promoted = reply_execution.promote(fixture.database, fixture.config, fixture.now)
                self.assertEqual(["BACKLOG_NOT_SENT"], [item["state"] for item in promoted["items"]])
                self.assertEqual([], fixture.actions())
                fixture.cycle()
                self.assertEqual([], fixture.requests)
                self.assertEqual([], fixture.actions())
                action = {"action_id": "a", "purpose": "REPLY", "profile": manual.PROFILE, "recipient": "Mock Contact",
                          "sender_id": 123, "conversation_id": "thread-1", "reply_task_id": "t",
                          "reply_source_key": "k", "reply_source_sha256": "s", "reply_source": "heyreach",
                          "reply_source_id": "message-1", "reply_inbound_at": manual.BASE.isoformat()}
                self.assertIn(reply_execution.BACKLOG_BLOCKER,
                              reply_execution.stop_gates(fixture.database, fixture.config, action))


class ReplyLabelTests(unittest.TestCase):
    def test_any_label_with_a_proposal_is_answered(self):
        for label in ("INTERVIEW", "REJECTION", "DECLINE"):
            with self.subTest(label=label), manual.isolated_boundaries(), manual.Fixture() as fixture:
                original = fixture.execute

                def execute(config, prompt, schema, timeout, original=original, label=label):
                    output = json.loads(original(config, prompt, schema, timeout))
                    if output.get("output_version") == reply_worker.VERSION:
                        output["label"] = label
                    return json.dumps(output)

                fixture.execute = execute
                fixture.seed()
                fixture.classify()
                promoted = reply_execution.promote(fixture.database, fixture.config, fixture.now)
                self.assertEqual(["PENDING"], [item["state"] for item in promoted["items"]], promoted)


class ReplyRetryTests(unittest.TestCase):
    def test_errored_send_that_was_delivered_is_confirmed_from_heyreach_not_resent(self):
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            fixture.seed()
            fixture.lost_ack = True          # the message lands, then the provider call errors
            fixture.cycle()
            self.assertEqual("UNCERTAIN_NO_RETRY", fixture.actions()[0]["state"])
            fixture.lost_ack = False
            fixture.cycle(seconds=3600)
            self.assertEqual("CONFIRMED_SENT", fixture.actions()[0]["state"])
            self.assertEqual(1, len(fixture.requests))
            result = json.loads(fixture.actions()[0]["result"])
            self.assertEqual("REPLY_RETRY_THREAD_CHECK", result["basis"])

    def test_errored_send_absent_from_heyreach_is_resent_once_due(self):
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            fixture.seed()
            fixture.provider_denied = True
            fixture.cycle()
            fixture.provider_denied = False
            fixture.cycle(seconds=599)
            self.assertEqual(1, len(fixture.requests))
            fixture.cycle(seconds=1)
            self.assertEqual(2, len(fixture.requests))
            fixture.cycle(seconds=1)
            self.assertEqual("CONFIRMED_SENT", fixture.actions()[0]["state"])
            fixture.cycle(seconds=86400)
            self.assertEqual(2, len(fixture.requests))


class ReplyRetryIntegrityTests(unittest.TestCase):
    def failed_first_attempt(self, fixture):
        fixture.seed()
        fixture.provider_denied = True
        fixture.cycle()
        fixture.provider_denied = False
        self.assertEqual("UNCERTAIN_NO_RETRY", fixture.actions()[0]["state"])

    def test_incomplete_thread_never_resends_and_ends_bounded(self):
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            self.failed_first_attempt(fixture)
            fixture.rooms["thread-1"]["totalMessages"] += 1      # the read is partial
            for unused in range(12):
                fixture.cycle(seconds=6 * 3600)
            self.assertEqual(1, len(fixture.requests))
            action = fixture.actions()[0]
            result = json.loads(action["result"])
            # No more sends, but still read-only reconcilable.
            self.assertEqual("UNCERTAIN_NO_RETRY", action["state"])
            self.assertTrue(result["reply_no_more_sends"])
            fixture.rooms["thread-1"]["totalMessages"] -= 1      # readable again: our text is absent
            fixture.cycle(seconds=12 * 3600)
            action = fixture.actions()[0]
            self.assertEqual(1, len(fixture.requests))
            self.assertEqual("CANCELLED", action["state"])
            self.assertEqual("REPLY_NOT_DELIVERED_NO_MORE_SENDS", json.loads(action["result"])["reply_terminal"])

    def test_delivered_reply_behind_a_long_unreadable_thread_is_still_confirmed(self):
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            fixture.seed()
            fixture.lost_ack = True          # delivered, then the provider call errored
            fixture.cycle()
            fixture.lost_ack = False
            fixture.rooms["thread-1"]["totalMessages"] += 1
            for unused in range(10):
                fixture.cycle(seconds=6 * 3600)
            self.assertEqual("UNCERTAIN_NO_RETRY", fixture.actions()[0]["state"])
            fixture.rooms["thread-1"]["totalMessages"] -= 1
            for unused in range(3):                              # reads now back off up to 12 h
                fixture.cycle(seconds=6 * 3600)
            self.assertEqual("CONFIRMED_SENT", fixture.actions()[0]["state"])
            self.assertEqual(1, len(fixture.requests))

    def test_good_read_resets_the_unreadable_streak(self):
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            self.failed_first_attempt(fixture)
            fixture.rooms["thread-1"]["totalMessages"] += 1
            for unused in range(5):
                fixture.cycle(seconds=6 * 3600)
            fixture.rooms["thread-1"]["totalMessages"] -= 1
            fixture.provider_denied = True
            fixture.cycle(seconds=6 * 3600)                     # good read, due -> second attempt
            self.assertEqual(2, len(fixture.requests))
            fixture.rooms["thread-1"]["totalMessages"] += 1
            fixture.cycle(seconds=6 * 3600)                     # one unreadable read after the reset
            result = json.loads(fixture.actions()[0]["result"])
            self.assertEqual(1, result["reply_unreadable_streak"])
            self.assertNotIn("reply_no_more_sends", result)

    def test_newer_inbound_makes_a_pending_retry_stand_down(self):
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            self.failed_first_attempt(fixture)
            fixture.add_message("thread-1", {"id": "later", "sender": "CORRESPONDENT", "body": "Also, is it remote?",
                                             "createdAt": (manual.BASE + dt.timedelta(minutes=5)).isoformat()})
            fixture.cycle(seconds=601)
            self.assertEqual(1, len(fixture.requests))
            action = fixture.actions()[0]
            self.assertEqual("CANCELLED", action["state"])
            self.assertEqual("REPLY_NEWER_CONVERSATION_ACTIVITY", json.loads(action["result"])["reply_terminal"])


class ReplyRepairTests(unittest.TestCase):
    def test_judge_rejected_reply_is_rewritten_once_in_the_same_slot_and_sent(self):
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            original = fixture.execute
            judged = []

            def execute(config, prompt, schema, timeout):
                packet = json.loads(prompt.split("UNTRUSTED_SOURCE_JSON:\n", 1)[1])
                output = json.loads(original(config, prompt, schema, timeout))
                if "previous_reply_proposal" in packet:
                    self.assertTrue(packet["repair_feedback"])
                    output["reply_proposal"]["text"] = "Happy to talk \u2014 which part matters most to you?"
                elif packet.get("phase") == "INSPECTION":
                    judged.append(packet["action"]["message"])
                    if len(judged) == 1:
                        output.update(decision="BLOCKED", reason="The reply is too vague.",
                                      next_action="Ask one specific question instead.")
                return json.dumps(output)

            fixture.execute = execute
            fixture.seed()
            fixture.cycle()
            self.assertEqual(2, len(judged))
            self.assertEqual(manual.TEXT, judged[0])
            actions = fixture.actions()
            self.assertEqual(1, len(actions))
            self.assertEqual(1, len(fixture.requests))
            self.assertEqual("Happy to talk, which part matters most to you?",
                             fixture.requests[0]["arguments"]["message"])


class ReplyInboundTimeDriftTests(unittest.TestCase):
    """HeyReach moves a message's createdAt by seconds between reads (recorded 10:00:00.100, read
    10:00:03.200): the same inbound message read again is not a newer one."""

    def drift(self, fixture, seconds=3.1):
        inbound = fixture.rooms["thread-1"]["messages"][0]
        inbound["createdAt"] = (dt.datetime.fromisoformat(inbound["createdAt"])
                                + dt.timedelta(seconds=seconds)).isoformat()

    def test_drifted_inbound_does_not_stand_a_retry_down(self):
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            ReplyRetryIntegrityTests.failed_first_attempt(self, fixture)
            self.drift(fixture)
            fixture.cycle(seconds=601)
            self.assertEqual(2, len(fixture.requests))
            self.assertNotEqual("CANCELLED", fixture.actions()[0]["state"])

    def test_different_message_seconds_later_still_stands_the_retry_down(self):
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            ReplyRetryIntegrityTests.failed_first_attempt(self, fixture)
            inbound = fixture.rooms["thread-1"]["messages"][0]
            fixture.add_message("thread-1", {"id": "soon", "sender": "CORRESPONDENT", "body": "Also, is it remote?",
                "createdAt": (dt.datetime.fromisoformat(inbound["createdAt"]) + dt.timedelta(seconds=3)).isoformat()})
            fixture.cycle(seconds=601)
            self.assertEqual(1, len(fixture.requests))
            self.assertEqual("REPLY_NEWER_CONVERSATION_ACTIVITY", json.loads(fixture.actions()[0]["result"])["reply_terminal"])

    def test_inspection_accepts_the_drifted_inbound_as_the_latest(self):
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            fixture.seed()
            self.drift(fixture)
            fixture.cycle()
            self.assertEqual(1, len(fixture.requests))
            fixture.cycle(seconds=601)
            self.assertEqual("CONFIRMED_SENT", fixture.actions()[0]["state"])

    def test_identical_text_sent_twice_still_gets_its_reply(self):
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            fixture.seed()
            first = fixture.rooms["thread-1"]["messages"][0]
            fixture.add_message("thread-1", dict(first, id="again", createdAt=(
                dt.datetime.fromisoformat(first["createdAt"]) + dt.timedelta(minutes=2)).isoformat()))
            fixture.cycle()
            self.assertEqual(1, len(fixture.requests))

    def test_our_own_answer_between_two_copies_means_no_automatic_reply(self):
        # Owner rule (Replies, 2026-09-30): our own later message, e.g. the owner answering by
        # hand, means no automatic reply. The inbox reader records it (outbound_observed), and
        # binding_gates stands the reply down even though the person then repeated the text.
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            fixture.seed()
            first = fixture.rooms["thread-1"]["messages"][0]
            at = dt.datetime.fromisoformat(first["createdAt"])
            ours = (at + dt.timedelta(minutes=1)).isoformat()
            fixture.add_message("thread-1", {"id": "ours", "sender": "ME", "body": "Answered by hand", "createdAt": ours})
            fixture.add_message("thread-1", dict(first, id="again", createdAt=(at + dt.timedelta(minutes=2)).isoformat()))
            engagement.record_event(fixture.database, {"source": "heyreach", "source_id": "ours", "profile": manual.PROFILE,
                "kind": "outbound_observed", "occurred_at": ours, "observed_at": ours, "evidence": "synthetic",
                "verified": True, "raw": {"conversation_id": "thread-1", "message": {"sender": "ME", "body": "Answered by hand"}}})
            fixture.database.commit()
            fixture.cycle()
            self.assertEqual(0, len(fixture.requests))
            # Since 2026-10-01 the already-answered message is skipped before the reply AI
            # (reply_execution.newer_activity, the binding_gates check; "Dont generate draft for me").
            self.assertEqual([("CANCELLED", reply_worker.ANSWERED_BLOCKER)], [tuple(row) for row in fixture.database.execute(
                "SELECT status,blocker FROM workflow_tasks WHERE worker='pipeline_routing_review'")])

    def recorded(self, fixture, body, seconds, source_id="copy"):
        inbound = fixture.rooms["thread-1"]["messages"][0]
        stamp = (dt.datetime.fromisoformat(inbound["createdAt"]) + dt.timedelta(seconds=seconds)).isoformat()
        event = {"source": "heyreach", "source_id": source_id, "profile": manual.PROFILE, "kind": "inbound",
                 "occurred_at": stamp, "observed_at": stamp, "evidence": "synthetic", "verified": True,
                 "raw": {"conversation_id": "thread-1", "message": {"sender": "CORRESPONDENT", "body": body}}}
        fixture.database.execute("INSERT INTO engagement_events VALUES (?,?,?,?,?,?,?,?,?)",
            ("heyreach", source_id, outreach.profile_key(manual.PROFILE), "inbound", stamp, stamp,
             json.dumps(event["raw"]), "null", json.dumps(event)))
        fixture.database.commit()

    def test_recorded_drifted_copy_does_not_stand_a_retry_down(self):
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            ReplyRetryIntegrityTests.failed_first_attempt(self, fixture)
            self.recorded(fixture, "Hello!\nCould you tell me more?", 3.1)
            fixture.cycle(seconds=601)
            self.assertEqual(2, len(fixture.requests))

    def test_recorded_different_message_stands_a_retry_down(self):
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            ReplyRetryIntegrityTests.failed_first_attempt(self, fixture)
            self.recorded(fixture, "Something else entirely", 3.1)
            fixture.cycle(seconds=601)
            self.assertEqual(1, len(fixture.requests))
            self.assertEqual("REPLY_NEWER_CONVERSATION_ACTIVITY", json.loads(fixture.actions()[0]["result"])["reply_terminal"])

    def test_the_answered_message_itself_never_counts_as_newer(self):
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            ReplyRetryIntegrityTests.failed_first_attempt(self, fixture)
            row = fixture.database.execute("SELECT payload FROM engagement_events WHERE source_id='message-1'").fetchone()
            event = json.loads(row[0])
            event["occurred_at"] = (dt.datetime.fromisoformat(event["occurred_at"]) + dt.timedelta(seconds=5)).isoformat()
            fixture.database.execute("UPDATE engagement_events SET payload=? WHERE source_id='message-1'", (json.dumps(event),))
            fixture.database.commit()
            fixture.cycle(seconds=601)
            self.assertEqual(2, len(fixture.requests))

    def test_binding_gate_skips_a_recorded_drifted_copy(self):
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            fixture.seed()
            self.recorded(fixture, "Hello!\nCould you tell me more?", 3.1)
            fixture.cycle()
            self.assertEqual(1, len(fixture.requests))
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            fixture.seed()
            self.recorded(fixture, "Something else entirely", 3.1)
            fixture.cycle()
            self.assertEqual(0, len(fixture.requests))

    def test_same_text_outside_the_window_is_a_newer_message(self):
        action = {"conversation_id": "thread-1", "reply_inbound_at": "2026-10-01T10:00:00.100+00:00",
                  "message": "Our answer"}
        room = {"id": "thread-1", "totalMessages": 2, "messages": [
            {"sender": "CORRESPONDENT", "body": "Hi", "createdAt": "2026-10-01T10:00:03.200Z"},
            {"sender": "CORRESPONDENT", "body": "Hi", "createdAt": "2026-10-01T10:30:00.000Z"}]}
        self.assertEqual("NEWER_INBOUND", reply_execution._our_reply(room, action, "Hi")[0])
        room["messages"].pop()
        room["totalMessages"] = 1
        self.assertIsNone(reply_execution._our_reply(room, action, "Hi")[0])
        self.assertEqual("NEWER_INBOUND", reply_execution._our_reply(room, action, None)[0])   # text unknown: as before


class ReplyNewerInboundTests(unittest.TestCase):
    def test_newer_inbound_gets_its_own_reply_and_the_older_one_stands_down(self):
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            fixture.seed(source_id="message-1")
            first = fixture.rooms["thread-1"]["messages"][0]
            original = manual.inbound_message

            def later(body, source_id):
                event = original(body, source_id)
                stamp = (manual.BASE + dt.timedelta(minutes=5)).isoformat()
                event.update(occurred_at=stamp, observed_at=stamp)
                event["inbox"]["authentication"]["observed_at"] = stamp
                return event

            fixture.now = manual.BASE + dt.timedelta(minutes=6)
            with patch.object(manual, "inbound_message", later):
                fixture.seed(source_id="message-2", body="One more question: is it remote?")
            fixture.rooms["thread-1"]["messages"].insert(0, first)
            fixture.rooms["thread-1"]["totalMessages"] = 2
            result = fixture.cycle()
            self.assertEqual(1, len(fixture.requests), result["reply_execution"])
            payload = json.loads(fixture.actions()[0]["payload"])
            self.assertEqual("message-2", payload["reply_source_id"])
            # The older message stands down before the reply AI (2026-10-01).
            self.assertIn(("CANCELLED", reply_worker.ANSWERED_BLOCKER), [tuple(row) for row in fixture.database.execute(
                "SELECT status,blocker FROM workflow_tasks WHERE worker='pipeline_routing_review'")])


class ReplyAfterCancelledReplyTests(unittest.TestCase):
    """A newer inbound message gets its own reply after an earlier REPLY ended CANCELLED (E3.T7 part 2)."""

    def test_newer_inbound_after_a_cancelled_reply_gets_exactly_one_new_reply(self):
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            fixture.seed(source_id="message-1")
            first = fixture.rooms["thread-1"]["messages"][0]
            fixture.provider_denied = True
            fixture.cycle()
            for unused in range(4):
                fixture.cycle(seconds=3600)
            old = fixture.actions()[0]
            self.assertEqual("CANCELLED", old["state"])
            self.assertEqual(reply_execution.TERMINAL_FAILURE, json.loads(old["result"])["reply_terminal"])
            self.assertEqual(3, len(fixture.requests))
            fixture.provider_denied = False
            original = manual.inbound_message
            stamp = (fixture.now + dt.timedelta(minutes=5)).isoformat()

            def later(body, source_id):
                event = original(body, source_id)
                event.update(occurred_at=stamp, observed_at=stamp)
                event["inbox"]["authentication"]["observed_at"] = stamp
                return event

            fixture.now += dt.timedelta(minutes=6)
            with patch.object(manual, "inbound_message", later):
                fixture.seed(source_id="message-2", body="Following up: is the role remote?")
            fixture.rooms["thread-1"]["messages"].insert(0, first)
            fixture.rooms["thread-1"]["totalMessages"] = 2
            new_text = "Happy to share more about remote work."
            with patch.object(manual, "TEXT", new_text):
                for unused in range(4):
                    fixture.cycle(seconds=3600)
            written = [request["arguments"]["message"] for request in fixture.requests]
            self.assertEqual([manual.TEXT] * 3 + [new_text], written)
            replies = [action for action in fixture.actions() if json.loads(action["payload"])["purpose"] == "REPLY"]
            states = {json.loads(action["payload"])["reply_source_id"]: action["state"] for action in replies}
            self.assertEqual({"message-1": "CANCELLED", "message-2": "CONFIRMED_SENT"}, states)

    def test_cancelled_reply_claim_is_skipped_only_for_a_reply(self):
        import sqlite3
        database = sqlite3.connect(":memory:")
        database.executescript("""
            CREATE TABLE outreach_actions (action_id TEXT PRIMARY KEY, identity TEXT, slot TEXT, payload TEXT,
                payload_hash TEXT, state TEXT, claimed_at TEXT, result TEXT);
            CREATE TABLE outreach_action_claims (action_id TEXT PRIMARY KEY, payload TEXT, payload_hash TEXT,
                identity TEXT, sender_id INTEGER, preflight TEXT, claimed_at TEXT);""")
        identity = outreach.profile_key("https://www.linkedin.com/in/someone")

        def claim(action_id, purpose, state):
            payload = json.dumps({"purpose": purpose})
            database.execute("INSERT INTO outreach_actions VALUES (?,?,?,?,'h',?,NULL,NULL)",
                             (action_id, identity, action_id, payload, state))
            database.execute("INSERT INTO outreach_action_claims VALUES (?,?,'h',?,1,'{}','t')",
                             (action_id, payload, identity))

        self.addCleanup(database.close)
        gates = outreach._immutable_claim_gates
        claim("old-reply", "REPLY", "CANCELLED")
        self.assertEqual([], gates(database, identity, "new", continuation=True, reply=True))
        self.assertEqual(["IMMUTABLE_OUTREACH_CLAIM_EXISTS"], gates(database, identity, "new", continuation=True))
        self.assertEqual(["IMMUTABLE_OUTREACH_CLAIM_EXISTS"], gates(database, identity, "new"))
        for other, purpose, state in (("uncertain-reply", "REPLY", "UNCERTAIN_NO_RETRY"),
                                      ("before-send", "REPLY", "CANCELLED_BEFORE_SEND"),
                                      ("cancelled-followup", "FOLLOWUP", "CANCELLED")):
            claim(other, purpose, state)
            self.assertEqual(["IMMUTABLE_OUTREACH_CLAIM_EXISTS"],
                             gates(database, identity, "new", continuation=True, reply=True), other)
            database.execute("DELETE FROM outreach_actions WHERE action_id=?", (other,))
            database.execute("DELETE FROM outreach_action_claims WHERE action_id=?", (other,))


class ReplySmallRulesTests(unittest.TestCase):
    def test_own_invitation_reservation_does_not_stop_a_reply(self):
        self.assertTrue(outreach._reply_suppression_allowed("Outreach reserved; never automatically retry"))
        self.assertFalse(outreach._reply_suppression_allowed("Existing user exclusion"))

    def test_reply_text_loses_em_and_en_dashes_and_keeps_lines(self):
        text = reply_execution.without_dashes("Thanks — happy to talk.\nMonday–Friday works.")
        self.assertNotIn("—", text)
        self.assertNotIn("–", text)
        self.assertEqual(2, len(text.split("\n")))


if __name__ == "__main__":
    unittest.main()
