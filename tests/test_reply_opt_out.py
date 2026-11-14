"""Owner, 2026-11-12: "record opt-outs of opt-in (if the person starts replying again.)"

The outcome is a conversation: a person asks for contact to stop (recorded, nothing more may be sent), later
writes again without asking to stop (lifted, handled as any other reply), then asks to stop again (recorded).
The reply model's structured `asks_to_stop_contact` decides; no words are matched anywhere.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import unittest

import datetime as dt
import json

import engagement
import manual_reply_execution as manual
import reply_execution
import reply_worker
import pipeline_routing as routing
import test_reply_worker as fixture
from backlog_fixture import setUpModule, tearDownModule  # noqa: F401 (fixture messages predate the reply cutoff)
from test_pipeline_routing import PROFILE, message


class OptOutConversationTests(unittest.TestCase):
    def setUp(self):
        self.worker = fixture.ReplyWorkerTests("run_cycle")
        self.worker.setUp()
        self.addCleanup(self.worker.doCleanups)
        self.database = self.worker.database

    def says(self, body, source_id, minute, asks, label="OTHER", decide=True, review_at=None, proposal=None):
        """One inbound message at 12:<minute>, read by the reply model, which answers `asks`."""
        event = message(body, source_id, occurred_at="2026-09-17T12:%02d:00+00:00" % minute)
        engagement.record_event(self.database, event)
        routing.cycle(self.database, self.worker.config, fixture.NOW)
        self.database.commit()
        if not decide:
            return None

        def answer(output):
            output.update(label=label, review_at=review_at)
            output["asks_to_stop_contact"] = asks
            if proposal:
                span = output["quote_span"]
                output["reply_proposal"] = {"text": proposal, "basis_quote": span}
        self.worker.mutate_output = answer
        items = self.worker.run_cycle()["items"]
        # The read itself is never blocked by a recorded opt-out: every message is decided.
        self.assertEqual(["COMPLETE"], [item["workflow_status"] for item in items], items)
        return items[0]

    def stopped(self):
        return engagement.stop_state(self.database, PROFILE)

    def sends_refused(self):
        """The existing checks later sends read: the reply send path's exclusion and the shared stop state
        (which follow-ups and new contact refuse on whenever it is set at all)."""
        return routing._reply_excluded(self.database, PROFILE, [])

    def test_stop_then_reply_again_then_stop(self):
        self.says("Please stop messaging me.", "m1", 1, True)
        self.assertEqual("OPTED_OUT", self.stopped())
        self.assertTrue(self.sends_refused())
        self.says("Actually, is the analyst role still open?", "m2", 2, False, label="QUESTION")
        self.assertEqual("REPLY_REQUIRES_CONTEXT", self.stopped())   # lifted: an ordinary replied-to person again
        self.assertFalse(self.sends_refused())
        self.says("Stop.", "m3", 3, True)
        self.assertEqual("OPTED_OUT", self.stopped())
        self.assertTrue(self.sends_refused())
        events = [row[0] for row in self.database.execute(
            "SELECT event FROM workflow_task_events WHERE event LIKE 'REPLY_CONTACT_STOP_%' ORDER BY rowid")]
        self.assertEqual(["REPLY_CONTACT_STOP_RECORDED", "REPLY_CONTACT_STOP_LIFTED", "REPLY_CONTACT_STOP_RECORDED"], events)

    def test_a_decline_is_not_an_opt_out_unless_the_model_says_so(self):
        self.says("Not interested, thanks.", "m1", 1, False, label="DECLINE")
        self.assertEqual("REPLY_REQUIRES_CONTEXT", self.stopped())
        self.assertFalse(self.sends_refused())

    def test_an_old_saved_decision_without_the_answer_still_validates_and_changes_nothing(self):
        self.says("Please stop messaging me.", "m1", 1, True)
        request = json.loads(self.worker.prompts[0].split("UNTRUSTED_SOURCE_JSON:\n", 1)[1])
        old = fixture.model_answer(self.worker, self.worker.prompts[0])
        old.pop("asks_to_stop_contact")                       # as saved before the field existed
        handoff = reply_worker._validate(json.dumps(old), request, "Please stop messaging me.", [])
        self.assertNotIn("asks_to_stop_contact", handoff["classification"])
        identity = engagement.identity_keys(self.database, PROFILE)[0]
        self.assertIsNone(engagement.apply_stop_request(self.database, PROFILE, identity, None, "heyreach", "m1"))
        self.assertEqual("OPTED_OUT", self.stopped())

    def test_a_new_decision_that_does_not_answer_is_held_and_retried_and_nothing_is_queued(self):
        task_id = self.worker.enqueue("Stop contacting me.")
        self.worker.mutate_output = lambda output: output.pop("asks_to_stop_contact")
        item = self.worker.run_cycle()["items"][0]
        self.assertEqual("CLASSIFICATION_STOP_CONTACT_ANSWER_REQUIRED", item["blocker"], item)
        task = self.worker.task(task_id)
        self.assertNotEqual("COMPLETE", task["status"])
        self.assertIsNotNone(task["next_at"])                 # booked to run again
        # No decision was saved, so nothing can be promoted into a reply, queued or sent.
        self.assertEqual(0, self.database.execute("SELECT COUNT(*) FROM reply_worker_results").fetchone()[0])
        self.assertIsNone(self.worker.database.execute(
            "SELECT 1 FROM sqlite_master WHERE name='outreach_actions'").fetchone())
        # The retry answers the question: the decision completes and the stop is recorded.
        self.worker.mutate_output = lambda output: output.update(asks_to_stop_contact=True, review_at=None)
        retried = self.worker.run_cycle(task["next_at"])["items"]
        self.assertEqual(["COMPLETE"], [entry["workflow_status"] for entry in retried], retried)
        self.assertEqual("OPTED_OUT", self.stopped())

    def routed(self):
        """(referral states on the person, the source's status, review tasks still waiting to run)."""
        referrals = [row[0] for row in self.database.execute("SELECT state FROM referrals")] \
            if self.database.execute("SELECT 1 FROM sqlite_master WHERE name='referrals'").fetchone() else []
        status = [row[0] for row in self.database.execute(
            "SELECT status FROM pipeline_routing_sources WHERE source_table='engagement_events'")]
        waiting = self.database.execute("""SELECT COUNT(*) FROM workflow_tasks WHERE worker='pipeline_routing_review'
            AND status NOT IN ('COMPLETE','FAILED','CANCELLED')""").fetchone()[0]
        return referrals, status, waiting

    def test_a_decline_that_also_asks_to_stop_is_still_recorded_as_a_decline(self):
        """The stop blocks only what is sent: the decision carrying it is routed as it would be without it."""
        self.says("Not for me, thanks.", "m0", 1, False, label="DECLINE")
        control = self.routed()
        self.assertEqual((["DECLINED"], 0), (control[0], control[2]))
        self.worker.doCleanups()
        self.setUp()
        item = self.says("No, I can't refer you. Please don't message me again.", "m1", 1, True, label="DECLINE")
        self.assertEqual(control, self.routed())                 # the decline recorded, no review queued by the stop
        self.assertEqual("OPTED_OUT", self.stopped())            # and the opt-out recorded
        self.assertTrue(self.sends_refused())
        self.assertIsNone(self.worker.handoff(item["task_id"])["classification"]["reply_proposal"])   # no reply


class OptOutSendTests(unittest.TestCase):
    """The same outcomes on the real send path: the reply worker, promotion and the scheduled cycle."""

    STOP = "Stop contacting me."

    def run_with(self, steps):
        with manual.isolated_boundaries(), manual.Fixture() as harness:
            original = harness.execute

            def execute(config, prompt, schema, timeout):
                # The test's stand-in for the model, which reads each message on its own: only STOP asks
                # for contact to stop, and every message (STOP too) comes with a proposed reply.
                raw = original(config, prompt, schema, timeout)
                packet = json.loads(prompt.split("UNTRUSTED_SOURCE_JSON:\n", 1)[1])
                if "source_snapshot" not in packet:
                    return raw
                output = json.loads(raw)
                output["asks_to_stop_contact"] = output["literal_quote"] == self.STOP
                if output["asks_to_stop_contact"]:
                    output["review_at"] = None   # nothing about a stop request needs looking at again
                return json.dumps(output)
            harness.execute = execute
            steps(harness)

    def promoted(self, harness):
        return sorted(item["state"] for item in reply_execution.promote(harness.database, harness.config, harness.now)["items"])

    def test_a_stop_request_that_carries_a_reply_is_never_queued_or_sent(self):
        def steps(harness):
            harness.seed(body=self.STOP)
            harness.classify()
            self.assertEqual("OPTED_OUT", engagement.stop_state(harness.database, manual.PROFILE))
            self.assertNotIn("PENDING", self.promoted(harness))
            harness.cycle()
            self.assertEqual([], harness.requests)
            self.assertEqual([], harness.actions())
        self.run_with(steps)

    def test_a_re_review_of_an_older_message_never_lifts_the_opt_out(self):
        def steps(harness):
            harness.seed(source_id="message-a", body="Let me check.")       # A: undecided, a dated re-review booked
            harness.classify()
            harness.seed(source_id="message-b", body=self.STOP)             # B: the person asks to stop
            harness.classify()
            self.assertEqual("OPTED_OUT", engagement.stop_state(harness.database, manual.PROFILE))
            seen = len(harness.inferences)
            harness.now += dt.timedelta(hours=13)                           # A's re-review falls due
            harness.classify()
            again = [json.loads(packet["source_snapshot"]["payload"])["inbox"]["text"]
                     for packet in harness.inferences[seen:] if "source_snapshot" in packet]
            self.assertIn("Let me check.", again)                           # it really was looked at again
            self.assertEqual("OPTED_OUT", engagement.stop_state(harness.database, manual.PROFILE))
            self.assertNotIn("PENDING", self.promoted(harness))
            harness.cycle()
            self.assertEqual([], harness.requests)                          # nothing reached the person
            # Control: a NEW message that does not ask to stop lifts it, and its reply goes out.
            harness.seed(source_id="message-c", body="Is the analyst role still open?")
            harness.classify()
            self.assertEqual("REPLY_REQUIRES_CONTEXT", engagement.stop_state(harness.database, manual.PROFILE))
            self.assertIn("PENDING", self.promoted(harness))
            harness.cycle()
            self.assertEqual(1, len(harness.requests), [(a["state"], a["result"]) for a in harness.actions()])
        self.run_with(steps)


if __name__ == "__main__":
    unittest.main()
