"""Repair turns on a judge-rejected outreach note, follow-up or reply (owner, 2026-09-23)."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
import json
import sqlite3
import unittest

import outreach_agent


class DraftRepairTests(unittest.TestCase):
    BLOCKED = {"agent_decision": "BLOCKED", "reason": "the sender-role claim is unsupported",
               "next_action": "Correct the sender-role wording and resubmit"}

    def test_text_rejection_becomes_repair_feedback(self):
        objection = outreach_agent._judge_text_rejection(self.BLOCKED)
        self.assertEqual(objection["rejection_reason"], "the sender-role claim is unsupported")
        self.assertEqual(objection["required_correction"], "Correct the sender-role wording and resubmit")
        self.assertEqual(objection["agent_decision"], "BLOCKED")

    def test_ready_never_repairs(self):
        self.assertIsNone(outreach_agent._judge_text_rejection({"agent_decision": "READY"}))

    def test_history_is_not_revalidated_here(self):
        """Owner, 2026-09-23: the pipeline already decided who may be contacted.

        A repair turn only rewrites text. It cannot widen who receives it, because
        outreach.action_gates still applies PREVIOUSLY_CONTACTED_NO_INITIAL, OPTED_OUT,
        SUPPRESSED_CONTACT and the engagement stop state to the repaired note.
        """
        for field in ("prior_outreach", "pending_invitation", "has_reply", "negative_reply", "opt_out"):
            with self.subTest(finding=field):
                self.assertIsNotNone(outreach_agent._judge_text_rejection({**self.BLOCKED, field: True}))

    def test_objection_without_a_stated_reason_does_not_loop(self):
        self.assertIsNone(outreach_agent._judge_text_rejection({"agent_decision": "UNKNOWN"}))

    def test_a_turn_that_changes_nothing_ends_the_round(self):
        """Mirrors answering_agent.repair_round: no progress ends it, it never raises."""
        class SameText:
            def __getitem__(self, key):
                return {"identity": "https://www.linkedin.com/in/x", "content_hash": "same"}[key]

        calls = []

        def compose(*args, **kwargs):
            calls.append(kwargs.get("repair"))
            return {"state": "COMPOSED"}

        class Database:
            def execute(self, *args):
                class Cursor:
                    def fetchone(inner):
                        # the recomposed version is byte-identical to the rejected one
                        return {"identity": "https://www.linkedin.com/in/x", "content_hash": "same"}
                return Cursor()

        original = outreach_agent.compose_draft
        outreach_agent.compose_draft = compose
        try:
            outcome = outreach_agent._draft_repair_turn(
                ".", Database(), {}, SameText(), {"invitation": "old"}, self.BLOCKED, None,
                __import__("datetime").datetime.now(__import__("datetime").timezone.utc))
        finally:
            outreach_agent.compose_draft = original
        self.assertIsNone(outcome, "an unchanged note must not be re-judged forever")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0]["rejection_reason"], "the sender-role claim is unsupported")



class JudgeObjectionStoreTests(unittest.TestCase):
    """A FOLLOWUP or REPLY is written by a different worker in an earlier stage, so the judge's
    objection is left where that worker will find it instead of being repaired in place."""

    PROFILE = "https://www.linkedin.com/in/someone"
    PROOF = {"agent_decision": "BLOCKED", "reason": "claims an interview outcome that is not observed",
             "next_action": "Remove the interview claim and resubmit"}

    def setUp(self):
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        outreach_agent.initialize(self.database)
        self.now = dt.datetime.now(dt.timezone.utc)
        self.action = {"action_id": "followup:abc", "profile": self.PROFILE,
                       "purpose": "FOLLOWUP", "message": "the rejected follow-up text"}

    def tearDown(self):
        self.database.close()

    def test_objection_round_trips_with_the_rejected_text(self):
        self.assertTrue(outreach_agent.record_judge_objection(self.database, self.action, self.PROOF, self.now))
        feedback = outreach_agent.pending_objection(self.database, self.PROFILE, "FOLLOWUP")
        self.assertEqual(feedback[0]["rejection_reason"], self.PROOF["reason"])
        self.assertEqual(feedback[0]["required_correction"], self.PROOF["next_action"])
        self.assertEqual(feedback[0]["previous_message"], "the rejected follow-up text")

    def test_objection_is_scoped_to_purpose_and_recipient(self):
        outreach_agent.record_judge_objection(self.database, self.action, self.PROOF, self.now)
        self.assertIsNone(outreach_agent.pending_objection(self.database, self.PROFILE, "REPLY"))
        self.assertIsNone(outreach_agent.pending_objection(self.database, "https://www.linkedin.com/in/nobody", "FOLLOWUP"))

    def test_an_approved_message_records_no_objection(self):
        self.assertIsNone(outreach_agent.record_judge_objection(
            self.database, self.action, {"agent_decision": "READY"}, self.now))
        self.assertIsNone(outreach_agent.pending_objection(self.database, self.PROFILE, "FOLLOWUP"))


class ActionReplacementTests(unittest.TestCase):
    """A repair turn may rewrite the note it was asked to rewrite, and nothing else.

    enqueue_action stays the authorization boundary: an ordinary caller passing changed text is
    still refused, which is the contract test_same_action_id_cannot_reauthorize_changed_message
    protects. Replacement is a compare-and-swap on the exact rejected payload_hash, and anything
    ever claimed is immutable regardless.
    """

    def setUp(self):
        import outreach
        self.outreach = outreach
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        outreach.initialize_actions(self.database)
        self.action = {"action_id": "followup:t1", "profile": "https://www.linkedin.com/in/someone",
                       "recipient": "Some One", "company": "Mock Co", "purpose": "FOLLOWUP",
                       "ordinal": 1, "message": "original text", "channel": "linkedin",
                       "provider": "heyreach",
                       "verification": {"content_verified": True, "content_sources": ["a"],
                                        "identity_verified": True, "identity_source": "b"}}
        self.outreach.enqueue_action(self.database, self.action)
        self.stored = self.database.execute(
            "SELECT payload_hash FROM outreach_actions WHERE action_id=?", (self.action["action_id"],)).fetchone()[0]

    def tearDown(self):
        self.database.close()

    def changed(self, text="rewritten text"):
        return dict(self.action, message=text)

    def test_plain_enqueue_of_changed_text_is_still_refused(self):
        with self.assertRaises(ValueError):
            self.outreach.enqueue_action(self.database, self.changed())

    def test_wrong_expected_hash_is_refused(self):
        with self.assertRaises(ValueError):
            self.outreach.enqueue_action(self.database, self.changed(), replacing="0" * 64)

    def test_exact_expected_hash_replaces_the_text(self):
        outcome = self.outreach.enqueue_action(self.database, self.changed(), replacing=self.stored)
        self.assertEqual(outcome["state"], "REPLACED")
        payload = self.database.execute("SELECT payload FROM outreach_actions WHERE action_id=?",
                                        (self.action["action_id"],)).fetchone()[0]
        self.assertEqual(json.loads(payload)["message"], "rewritten text")

    def test_a_claimed_action_is_immutable_even_with_the_right_hash(self):
        with self.database:
            self.database.execute("UPDATE outreach_actions SET claimed_at=? WHERE action_id=?",
                                  ("2026-09-24T00:00:00+00:00", self.action["action_id"]))
        with self.assertRaises(ValueError):
            self.outreach.enqueue_action(self.database, self.changed("third text"), replacing=self.stored)

class MalformedOutputRepairTests(unittest.TestCase):
    """Repair turns when Python cannot ACCEPT the model's output, not when it dislikes the note.

    Owner, 2026-09-23: "repair turns for malformed proof". Serves both validators - _validate for
    the judge's proof and _validate_composition for the drafting agent. Nothing is relaxed: an
    output that still fails still fails. The model is simply told what was structurally wrong.
    """

    CONFIG = {"outreach_agent": {"max_prompt_bytes": 400000}}
    PACKET = {"action": {"profile": "x"}, "phase": "INSPECTION"}

    def validator(self, accepted):
        def validate(raw, packet):
            if raw != accepted:
                raise ValueError("OUTREACH_DECISION_BINDING_INVALID")
            return {"decision": "READY", "echoed": outreach_agent.digest(packet)}
        return validate

    def run_turns(self, outputs, raw="bad", **extra):
        produced = iter(outputs)
        return outreach_agent._output_with_repair_turns(
            lambda config, prompt, schema, timeout: next(produced),
            self.CONFIG, "TEMPLATE", "{}", 60, self.PACKET, raw, self.validator("good"), **extra)

    def test_a_rejected_output_is_re_prompted_with_its_exact_reason(self):
        turns = []
        decision, packet = self.run_turns(["good"], record=lambda n, reason: turns.append((n, reason)))
        self.assertEqual(decision["decision"], "READY")
        self.assertEqual(turns, [(1, "OUTREACH_DECISION_BINDING_INVALID")])
        self.assertEqual(packet["repair_feedback"],
                         [{"previous_output": "bad", "rejection_reason": "OUTREACH_DECISION_BINDING_INVALID"}])

    def test_the_model_must_echo_the_new_packet_digest(self):
        """Adding repair_feedback changes digest(packet), so the binding stays exact."""
        decision, packet = self.run_turns(["good"])
        self.assertEqual(decision["echoed"], outreach_agent.digest(packet))
        self.assertNotEqual(outreach_agent.digest(packet), outreach_agent.digest(self.PACKET))

    def test_an_unchanged_output_ends_the_round(self):
        with self.assertRaises(ValueError):
            self.run_turns(["bad", "bad", "bad"])

    def test_a_refused_compute_reservation_stops_the_round(self):
        with self.assertRaises(ValueError):
            self.run_turns(["good"], reserve=lambda turn: False)

    def test_an_already_valid_output_costs_no_extra_call(self):
        calls = []
        decision, packet = outreach_agent._output_with_repair_turns(
            lambda config, prompt, schema, timeout: calls.append(1) or "x",
            self.CONFIG, "T", "{}", 60, self.PACKET, "good", self.validator("good"))
        self.assertEqual(calls, [])
        self.assertNotIn("repair_feedback", packet)

if __name__ == "__main__":
    unittest.main()
