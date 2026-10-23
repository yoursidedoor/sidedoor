"""Owner, 2026-10-23: "unknown should never block" and "same applies for reply and followups". A send
judge's UNKNOWN goes ahead like READY; BLOCKED still holds."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import sqlite3
import unittest
from unittest import mock

import json

import outreach
import outreach_agent
import reply_execution


class UnknownProceedsTests(unittest.TestCase):
    def reply_gates(self, decision):
        database = sqlite3.connect(":memory:")
        database.execute("CREATE TABLE outreach_actions(action_id TEXT, slot TEXT)")
        action = {"action_id": "a", "sender_id": 1, "reply_source_key": "k", "reply_source_sha256": "s",
                  "reply_source_id": "i", "reply_inbound_at": "t"}
        proof = {"agent_decision": decision, "provider_snapshot_complete": True, "reply_source_key": "k",
                 "reply_source_sha256": "s", "reply_source_id": "i", "reply_inbound_at": "t",
                 "reply_latest_inbound": True, "reply_already_answered": False, "reply_context_verified": True,
                 "reply_transport_supported": True, "provider_restriction_observed": False}
        with mock.patch.object(outreach, "reply_slot", return_value="slot"), \
                mock.patch.object(reply_execution, "binding_gates", return_value=[]):
            return outreach._reply_gates(database, {"reply_execution": {"enabled": True}, "sender_id": 1},
                                         action, proof, None)

    def test_a_reply_judged_unknown_is_not_held(self):
        self.assertEqual(self.reply_gates("UNKNOWN"), [])
        self.assertEqual(self.reply_gates("READY"), [])
        self.assertEqual(self.reply_gates("BLOCKED"), ["AGENT_DECISION_NOT_READY"])

    def judged(self, **change):
        packet = {"phase": "INSPECTION"}
        output = {"output_version": outreach_agent.VERSION, "source_sha256": outreach_agent.digest(packet),
                  "decision": "UNKNOWN", "reason": "r", "next_action": "n", "conversation_id": None,
                  "receipt_observation": None, "receipt_message": None, "route": "INVITATION",
                  "findings": dict.fromkeys(("prior_outreach", "has_reply", "negative_reply", "opt_out"), False)}
        output.update(change)
        return outreach_agent._validate(json.dumps(output), packet)

    def test_a_judge_must_answer_every_finding_and_the_route(self):
        # Owner, 2026-10-23: "a judge must give an asnswer. leaving things blank introduces ambiguity."
        self.assertEqual(self.judged()["route"], "INVITATION")
        blank = dict.fromkeys(("prior_outreach", "has_reply", "negative_reply", "opt_out"), False, )
        blank["has_reply"] = None
        with self.assertRaisesRegex(ValueError, "FINDINGS_MUST_BE_ANSWERED"):
            self.judged(findings=blank)
        with self.assertRaisesRegex(ValueError, "ROUTE_MUST_BE_ANSWERED"):
            self.judged(route="UNKNOWN")

    def test_the_proceeding_decisions(self):
        self.assertEqual(outreach.PROCEED_DECISIONS, {"READY", "UNKNOWN"})


if __name__ == "__main__":
    unittest.main()
