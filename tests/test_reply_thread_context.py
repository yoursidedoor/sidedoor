import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
import json
import unittest
from unittest import mock

import applications
import engagement
import heyreach_schema
import pipeline_routing as routing
import referrals
import reply_worker as worker
import test_heyreach_schema as provider_fixtures
import test_reply_worker_review as review
from backlog_fixture import setUpModule, tearDownModule  # noqa: F401 (fixture messages predate the reply cutoff)


OWN_PROFILE = "https://linkedin.com/in/thread-context-account"
OTHER_PROFILE = "https://linkedin.com/in/other-thread-contact"


class ReplyThreadContextTests(unittest.TestCase):
    queue = review.ReplyWorkerCycleReview.queue
    task = review.ReplyWorkerCycleReview.task
    request = review.ReplyWorkerCycleReview.request
    execute = review.ReplyWorkerCycleReview.execute
    cycle = review.ReplyWorkerCycleReview.cycle
    assert_no_handoff = review.ReplyWorkerCycleReview.assert_no_handoff

    def setUp(self):
        review.ReplyWorkerCycleReview.setUp(self)
        self.config["sender_profile"] = OWN_PROFILE
        self.provider = mock.patch.object(
            review.orchestrator, "HeyReach", side_effect=AssertionError("Provider access forbidden")
        ).start()

    def event(self, text="Earlier outbound discussion.", direction="outbound",
              minute=30, profile=review.PROFILE, account=123, thread="fixture-thread"):
        message = provider_fixtures.message(
            "ME" if direction == "outbound" else "CORRESPONDENT", text,
            f"2026-09-17T15:{minute:02d}:00.000Z")
        room = provider_fixtures.room([message])
        room.update(id=thread, linkedInAccountId=account,
                    linkedInAccount={"id": account, "profileUrl": OWN_PROFILE})
        room["correspondentProfile"]["profileUrl"] = profile
        decoder = heyreach_schema.Decoder(
            account, OWN_PROFILE, clock=lambda: dt.datetime.fromisoformat(review.NOW))
        return decoder.decode_chatroom(room, decoder.decode_conversation(room))[0]

    def record(self, event):
        engagement.record_event(self.database, event)
        self.database.commit()
        return event["source_id"]

    def invitation(self, **fields):
        event = self.event(**fields)
        event["raw"]["message_type"] = "invitation"
        event["inbox"]["message_type"] = "invitation"
        event["inbox"]["raw_paths"]["message_type"] = ["message_type"]
        return event

    def mixed_history(self):
        return [self.record(self.event(
            text=f"Authenticated discussion {index}.",
            direction="inbound" if index % 2 == 0 else "outbound", minute=10 + index))
            for index in range(18)]

    def context_ids(self, request):
        return [item["snapshot"]["source_id"] for item in request["conversation_context"]]

    def prompt_request(self, execute):
        return json.loads(execute.call_args.args[1].split("UNTRUSTED_SOURCE_JSON:\n", 1)[1])

    def test_real_normalized_outbound_keeps_recipient_and_authenticated_own_profile(self):
        task_id = self.queue()
        event = self.event()
        source_id = self.record(event)
        self.assertEqual("https://linkedin.com/in/mock-contact", event["raw"]["profile"])
        self.assertEqual(OWN_PROFILE, event["raw"]["account_profile"])
        self.assertEqual(OWN_PROFILE, event["inbox"]["sender_profile"])
        self.assertEqual("outbound", event["inbox"]["direction"])
        self.assertIsNone(event["inbox"]["quoted"])
        request = self.request(task_id)
        self.assertEqual([source_id], self.context_ids(request))
        snapshot = request["conversation_context"][0]["snapshot"]
        self.assertEqual("outbound_observed", snapshot["kind"])
        self.assertEqual("linkedin.com/in/mock-contact", snapshot["identity"])
        self.assertEqual(event, json.loads(snapshot["payload"]))
        self.assertEqual(routing.digest(snapshot), request["conversation_context"][0]["content_sha256"])

    def test_latest_sixteen_span_both_directions_and_keep_evaluated_source_separate(self):
        task_id = self.queue()
        source_ids = self.mixed_history()
        request = self.request(task_id)
        self.assertEqual(list(reversed(source_ids[2:])), self.context_ids(request))
        self.assertEqual(["outbound_observed", "inbound"] * 8,
                         [item["snapshot"]["kind"] for item in request["conversation_context"]])
        self.assertEqual("review-message", request["source_snapshot"]["source_id"])
        self.assertEqual(review.MESSAGE, request["raw_message"]["text"])
        self.assertNotIn("review-message", self.context_ids(request))
        self.assertEqual(routing.digest(request["source_snapshot"]), request["source_sha256"])
        self.assertEqual(request, self.request(task_id))

    def test_rejects_unverified_forged_identity_direction_and_raw_bindings(self):
        task_id = self.queue()
        valid_id = self.record(self.event(text="Valid outbound context."))

        def change_field(event, field, value):
            event["raw"][field] = event["inbox"][field] = value

        variants = {
            "unverified": lambda event: event.update(verified=False),
            "forged-authentication": lambda event: event["inbox"]["authentication"].update(method="claimed"),
            "wrong-own-profile": lambda event: change_field(event, "sender_profile", OTHER_PROFILE),
            "wrong-account-profile": lambda event: event["raw"].update(account_profile=OTHER_PROFILE),
            "missing-account-profile": lambda event: event["raw"].pop("account_profile"),
            "wrong-raw-recipient": lambda event: event["raw"].update(profile=OTHER_PROFILE),
            "wrong-payload-recipient": lambda event: event.update(profile=OTHER_PROFILE),
            "wrong-direction": lambda event: change_field(event, "direction", "inbound"),
            "wrong-message-id": lambda event: change_field(event, "message_id", "forged-id"),
            "raw-text-mismatch": lambda event: event["inbox"].update(text="Forged normalized text."),
            "raw-account-mismatch": lambda event: event["inbox"].update(account_id=456),
            "raw-sender-mismatch": lambda event: event["inbox"].update(sender_profile=OTHER_PROFILE),
            "forged-raw-path": lambda event: event["inbox"]["raw_paths"].update(sender_profile=["profile"]),
        }
        for name, mutate in variants.items():
            with self.subTest(case=name):
                event = self.event(text=name, minute=50)
                mutate(event)
                self.record(event)
                self.assertEqual([valid_id], self.context_ids(self.request(task_id)))

    def test_authenticated_other_thread_account_and_recipient_are_excluded(self):
        task_id = self.queue()
        self.config["pipeline_routing"]["account_ids"] = [123, 456]
        valid_id = self.record(self.event())
        for direction in ("inbound", "outbound"):
            for scope in ({"thread": "other-thread"}, {"account": 456}, {"account": 789},
                          {"profile": OTHER_PROFILE}):
                with self.subTest(direction=direction, scope=scope):
                    self.record(self.event(text="Different scope.", direction=direction, minute=50, **scope))
                    self.assertEqual([valid_id], self.context_ids(self.request(task_id)))

    def test_stored_raw_payload_mismatch_is_excluded(self):
        task_id = self.queue()
        event = self.event()
        source_id = self.record(event)
        self.assertEqual([source_id], self.context_ids(self.request(task_id)))
        raw = {**event["raw"], "text": "Changed outside the authenticated payload."}
        self.database.execute("UPDATE engagement_events SET raw=? WHERE source_id=?",
                              (json.dumps(raw), source_id))
        self.database.commit()
        self.assertEqual([], self.context_ids(self.request(task_id)))

    def test_executor_receives_latest_sixteen_real_normalized_messages_without_sends(self):
        task_id = self.queue()
        source_ids = self.mixed_history()
        self.assert_executor_context(task_id, list(reversed(source_ids[2:])))

    def test_executor_retains_role_and_person_referral_with_both_directions(self):
        task_id = self.queue()
        inbound_id = self.record(self.event(text="Earlier contact reply.", direction="inbound", minute=20))
        outbound_id = self.record(self.event(minute=30))
        self.assert_executor_context(task_id, [outbound_id, inbound_id])

    def assert_executor_context(self, task_id, expected_ids):
        job_key = applications.identity(review.opportunity())[2]
        contact_id = "linkedin.com/in/mock-contact"
        # Referrals are per person (owner, 2026-09-25): the context carries this contact's
        # referral whatever job it concerns, and never another person's.
        for contact in (contact_id, "other-contact"):
            referrals.record(self.database, contact, "AGREED", "fixture-source-" + contact, "Happy to refer you.",
                             "Send the summary.", "2026-09-17T20:00:00+00:00", review.NOW, {})
        self.database.commit()
        before = routing.rows(self.database, "SELECT * FROM engagement_events ORDER BY source_id")
        result, execute = self.cycle()
        self.assertEqual("COMPLETE", self.task(task_id)["status"], result)
        execute.assert_called_once()
        request = self.prompt_request(execute)
        self.assertEqual(expected_ids, self.context_ids(request))
        self.assertEqual([job_key], [binding["job_key"] for binding in request["job_scope"]])
        self.assertEqual("Staff Engineer", request["job_scope"][0]["role"])
        self.assertEqual([("fixture-source-" + contact_id, "AGREED")],
                         [(item["source_key"], item["state"]) for item in request["referral_context"]])
        self.assertEqual("review-message", request["source_snapshot"]["source_id"])
        self.assertEqual("COMPLETE", self.task(task_id)["status"])
        self.assertEqual("COMPLETE", result["items"][0]["workflow_status"])
        handoff = json.loads(routing.one(self.database,
            "SELECT handoff FROM reply_worker_results WHERE task_id=?", (task_id,))["handoff"])
        self.assertFalse(handoff["can_send"])
        self.assertEqual(before, routing.rows(self.database, "SELECT * FROM engagement_events ORDER BY source_id"))
        self.provider.assert_not_called()

    def test_new_outbound_during_execution_discards_stale_inference(self):
        task_id = self.queue()
        self.record(self.event())

        def arrive(*arguments):
            self.record(self.event(text="New outbound clarification.", minute=55))
            return self.execute(*arguments)

        result, execute = self.cycle(effect=arrive)
        execute.assert_called_once()
        self.assert_no_handoff(task_id, "CLASSIFICATION_SOURCE_CHANGED")
        self.assertFalse(result["items"][0]["can_advance_conversion"])
        self.assertIsNotNone(routing.one(self.database,
            "SELECT output FROM reply_worker_attempts WHERE task_id=?", (task_id,))["output"])
        self.provider.assert_not_called()

    def test_new_outbound_before_retry_invalidates_saved_input_and_refreshes_context(self):
        task_id = self.queue()
        self.record(self.event())
        self.cycle(effect=worker.Held("CLASSIFICATION_TIMEOUT", retry=True))
        saved = routing.one(self.database, "SELECT * FROM reply_worker_inputs WHERE task_id=?", (task_id,))
        due = self.task(task_id)["next_at"]
        new_id = self.record(self.event(text="New outbound before retry.", minute=55))
        result, execute = self.cycle(due)
        execute.assert_not_called()
        self.assert_no_handoff(task_id, "CLASSIFICATION_IMMUTABLE_INPUT_CHANGED")
        successor_id = result["items"][0]["context_revision_task_id"]
        self.assertIsNotNone(successor_id)
        self.assertEqual("READ", self.task(successor_id)["effect"])
        unused, execute = self.cycle(due)
        execute.assert_called_once()
        self.assertEqual(new_id, self.context_ids(self.prompt_request(execute))[0])
        self.assertEqual("COMPLETE", self.task(successor_id)["status"])
        self.assertEqual(saved, routing.one(self.database,
            "SELECT * FROM reply_worker_inputs WHERE task_id=?", (task_id,)))
        self.provider.assert_not_called()

    def test_older_cross_thread_invitation_is_pinned_beyond_latest_sixteen_in_executor(self):
        task_id = self.queue()
        source_ids = self.mixed_history()
        event = self.invitation(text="Original invitation note.", minute=1, thread="invitation-thread")
        invitation_id = self.record(event)
        request = self.request(task_id)
        initial = request["initial_invitation"]
        self.assertEqual(invitation_id, initial["snapshot"]["source_id"])
        self.assertEqual(event, json.loads(initial["snapshot"]["payload"]))
        self.assertEqual(routing.digest(initial["snapshot"]), initial["content_sha256"])
        self.assertEqual(list(reversed(source_ids[2:])), self.context_ids(request))
        self.assertNotIn(invitation_id, self.context_ids(request))
        self.assertEqual("review-message", request["source_snapshot"]["source_id"])
        before = routing.rows(self.database, "SELECT * FROM engagement_events ORDER BY source_id")
        result, execute = self.cycle()
        self.assertEqual("COMPLETE", self.task(task_id)["status"], result)
        execute.assert_called_once()
        self.assertEqual(request, self.prompt_request(execute))
        self.assertGreater(len(execute.call_args.args[1].encode("utf-8")), 32768)
        self.assertLessEqual(len(execute.call_args.args[1].encode("utf-8")), worker.MAX_INPUT_BYTES)
        self.assertEqual(before, routing.rows(self.database, "SELECT * FROM engagement_events ORDER BY source_id"))
        handoff = json.loads(routing.one(self.database,
            "SELECT handoff FROM reply_worker_results WHERE task_id=?", (task_id,))["handoff"])
        self.assertFalse(handoff["can_send"])
        self.provider.assert_not_called()

    def test_earliest_invitation_wins_and_recent_invitations_do_not_consume_context_slots(self):
        task_id = self.queue()
        source_ids = self.mixed_history()
        recent_id = self.record(self.invitation(text="Later invitation.", minute=55))
        earliest_id = self.record(self.invitation(text="Earliest invitation.", minute=1))
        middle_id = self.record(self.invitation(text="Middle invitation.", minute=5))
        request = self.request(task_id)
        self.assertEqual(earliest_id, request["initial_invitation"]["snapshot"]["source_id"])
        self.assertEqual(list(reversed(source_ids[2:])), self.context_ids(request))
        self.assertTrue({recent_id, earliest_id, middle_id}.isdisjoint(self.context_ids(request)))
        self.assertEqual(request, self.request(task_id))

    def test_forged_or_unbound_markers_are_not_initial_invitations(self):
        task_id = self.queue()
        variants = {
            "missing-raw-marker": lambda event: event["raw"].pop("message_type"),
            "wrong-raw-marker": lambda event: event["raw"].update(message_type="message"),
            "missing-inbox-marker": lambda event: event["inbox"].pop("message_type"),
            "wrong-inbox-marker": lambda event: event["inbox"].update(message_type="message"),
            "missing-marker-path": lambda event: event["inbox"]["raw_paths"].pop("message_type"),
            "wrong-marker-path": lambda event: event["inbox"]["raw_paths"].update(message_type=["direction"]),
            "invalid-marker-path": lambda event: event["inbox"]["raw_paths"].update(message_type="message_type"),
        }
        for name, mutate in variants.items():
            with self.subTest(case=name):
                event = self.invitation(text=name)
                mutate(event)
                self.record(event)
                self.assertIsNone(self.request(task_id)["initial_invitation"])
        inbound = self.invitation(text="Inbound text with invitation marker.", direction="inbound")
        self.record(inbound)
        self.assertIsNone(self.request(task_id)["initial_invitation"])

    def test_invitation_still_requires_verified_recipient_account_and_own_identity(self):
        task_id = self.queue()
        self.config["pipeline_routing"]["account_ids"] = [123, 456]
        variants = [
            self.invitation(text="Different recipient.", profile=OTHER_PROFILE),
            self.invitation(text="Other allowed account.", account=456),
            self.invitation(text="Unconfigured account.", account=789),
        ]
        unverified = self.invitation(text="Unverified invitation.")
        unverified["verified"] = False
        variants.append(unverified)
        wrong_own_profile = self.invitation(text="Wrong own profile.")
        wrong_own_profile["raw"]["account_profile"] = OTHER_PROFILE
        variants.append(wrong_own_profile)
        wrong_authentication = self.invitation(text="Unauthenticated invitation.")
        wrong_authentication["inbox"]["authentication"]["method"] = "claimed"
        variants.append(wrong_authentication)
        wrong_recipient = self.invitation(text="Forged recipient.")
        wrong_recipient["raw"]["profile"] = OTHER_PROFILE
        variants.append(wrong_recipient)
        for event in variants:
            with self.subTest(case=event["inbox"]["text"]):
                self.record(event)
                self.assertIsNone(self.request(task_id)["initial_invitation"])
        valid_id = self.record(self.invitation(text="Verified invitation.", thread="different-thread"))
        self.assertEqual(valid_id, self.request(task_id)["initial_invitation"]["snapshot"]["source_id"])

    def test_missing_invitation_remains_none_for_unmarked_provider_history(self):
        task_id = self.queue()
        self.assertIsNone(self.request(task_id)["initial_invitation"])
        self.mixed_history()
        ordinary = self.event(text="I would like to connect with you.", minute=1)
        ordinary["kind"] = "initial_sent"
        self.record(ordinary)
        self.assertIsNone(self.request(task_id)["initial_invitation"])
        unused, execute = self.cycle()
        execute.assert_called_once()
        self.assertIsNone(self.prompt_request(execute)["initial_invitation"])
        self.assertEqual("COMPLETE", self.task(task_id)["status"])

    def test_new_invitation_during_execution_invalidates_inference_and_queues_revision(self):
        task_id = self.queue()
        source_ids = self.mixed_history()
        event = self.invitation(text="Newly discovered original invitation.", minute=1, thread="earlier-thread")

        def arrive(*arguments):
            self.record(event)
            return self.execute(*arguments)

        result, execute = self.cycle(effect=arrive)
        execute.assert_called_once()
        self.assert_no_handoff(task_id, "CLASSIFICATION_SOURCE_CHANGED")
        self.assertIsNone(self.prompt_request(execute)["initial_invitation"])
        successor_id = result["items"][0]["context_revision_task_id"]
        self.assertIsNotNone(successor_id)
        self.assertEqual("READ", self.task(successor_id)["effect"])
        unused, execute = self.cycle()
        execute.assert_called_once()
        request = self.prompt_request(execute)
        self.assertEqual(event["source_id"], request["initial_invitation"]["snapshot"]["source_id"])
        self.assertEqual(list(reversed(source_ids[2:])), self.context_ids(request))
        self.assertEqual("COMPLETE", self.task(successor_id)["status"])
        self.provider.assert_not_called()

    def test_new_invitation_before_retry_invalidates_saved_input(self):
        task_id = self.queue()
        self.cycle(effect=worker.Held("CLASSIFICATION_TIMEOUT", retry=True))
        saved = routing.one(self.database, "SELECT * FROM reply_worker_inputs WHERE task_id=?", (task_id,))
        due = self.task(task_id)["next_at"]
        invitation_id = self.record(self.invitation(text="Recovered original note.", minute=1))
        result, execute = self.cycle(due)
        execute.assert_not_called()
        self.assert_no_handoff(task_id, "CLASSIFICATION_IMMUTABLE_INPUT_CHANGED")
        successor_id = result["items"][0]["context_revision_task_id"]
        self.assertIsNotNone(successor_id)
        unused, execute = self.cycle(due)
        execute.assert_called_once()
        self.assertEqual(invitation_id,
                         self.prompt_request(execute)["initial_invitation"]["snapshot"]["source_id"])
        self.assertEqual("COMPLETE", self.task(successor_id)["status"])
        self.assertEqual(saved, routing.one(self.database,
            "SELECT * FROM reply_worker_inputs WHERE task_id=?", (task_id,)))

    def test_new_invitation_no_longer_rejects_saved_result_and_preserves_immutable_audit(self):
        # A saved decision is consumed as it was made (owner, 2026-09-30: "For 'the saved
        # decision must revalidate' please remove this completely"); newer context does not
        # reject it or queue a re-classification.
        task_id = self.queue()
        with mock.patch.object(routing, "apply_reply_decision", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                self.cycle()
        self.assertEqual("COMPLETE", self.task(task_id)["status"])
        saved = routing.one(self.database, "SELECT * FROM reply_worker_results WHERE task_id=?", (task_id,))
        self.record(self.invitation(text="Newly saved invitation.", minute=1, thread="prior-thread"))
        later = "2026-09-17T16:01:00+00:00"
        result, execute = self.cycle(later)
        execute.assert_not_called()
        self.assertEqual("COMPLETE", self.task(task_id)["status"])
        self.assertEqual(1, len(result["decisions"]))
        self.assertNotEqual("BLOCKED", result["decisions"][0]["state"])
        self.assertNotIn("context_revision_task_id", result["decisions"][0])
        self.assertEqual(saved, routing.one(self.database,
            "SELECT * FROM reply_worker_results WHERE task_id=?", (task_id,)))
        self.provider.assert_not_called()

    def test_prompt_exceeding_the_input_cap_is_held_without_execution(self):
        """The cap is 131072 bytes since 2026-09-24 (it was 65536); anything larger is still held."""
        task_id = self.queue()
        for index in range(8):
            self.record(self.event(text=f"Message {index}: " + "x" * 18000,
                                   direction="inbound" if index % 2 == 0 else "outbound", minute=10 + index))
        self.record(self.invitation(text="Original invitation note.", minute=1))
        request = self.request(task_id)
        self.assertEqual(8, len(request["conversation_context"]))
        prompt = (review.ROOT / "reply-prompt.md").read_text() + "\n\nUNTRUSTED_SOURCE_JSON:\n" + routing.encoded(request)
        self.assertEqual(131072, worker.MAX_INPUT_BYTES)
        self.assertGreater(len(prompt.encode("utf-8")), worker.MAX_INPUT_BYTES)
        result, execute = self.cycle()
        execute.assert_not_called()
        self.assert_no_handoff(task_id, "CLASSIFICATION_INPUT_EXCEEDS_LIMIT")
        self.assertEqual("HELD", result["items"][0]["state"])
        self.assertEqual(0, self.database.execute("SELECT COUNT(*) FROM worker_compute_reservations").fetchone()[0])
        self.provider.assert_not_called()


if __name__ == "__main__":
    unittest.main()
