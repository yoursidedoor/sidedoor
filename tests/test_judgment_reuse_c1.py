import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import datetime as dt
import json
import unittest

import outreach
import test_outreach_agent


class JudgmentReuseTests(test_outreach_agent.OutreachAgentTests):
    """C1: the pre-send judge of a first note is reused while its inputs are unchanged."""

    def wait_cycles(self, count):
        proofs = []
        for unused in range(count):
            # A draft waiting on pacing or a hold: its inspection expires every hour and runs again.
            self.now += dt.timedelta(hours=1)
            proofs.append(self.adapter.inspect(self.action, self.config))
        return proofs

    def test_before_every_waiting_cycle_called_the_model(self):
        self.config["outreach_agent"]["judgment_reuse_seconds"] = 0
        self.wait_cycles(5)
        self.assertEqual(self.executor.call_count, 5)

    def test_five_waiting_cycles_make_one_judgment_and_gates_see_fresh_proof(self):
        proofs = self.wait_cycles(5)
        self.assertEqual(self.executor.call_count, 1)
        for proof in proofs:
            self.assertEqual(proof["agent_decision"], "READY")
        # Each proof is built from that cycle's own read, so the freshness gates are unchanged.
        self.assertEqual(proofs[-1]["checked_at"], self.now.isoformat())
        self.assertEqual(outreach.action_gates(self.database, self.config, self.action, proofs[-1], self.now), [])
        kinds = [row[0] for row in self.database.execute(
            "SELECT json_extract(record,'$.route') FROM outreach_agent_audit WHERE kind='DECISION'")]
        self.assertEqual(sorted(kind or "MODEL" for kind in kinds), ["MODEL"] + ["REUSED"] * 4)

    def test_a_changed_draft_or_snapshot_is_judged_again(self):
        self.wait_cycles(2)
        self.assertEqual(self.executor.call_count, 1)
        self.action = dict(self.action, message="A rewritten note")
        with self.database:
            self.database.execute("UPDATE outreach_actions SET payload=? WHERE action_id=?",
                                  (json.dumps(self.action), self.action["action_id"]))
        self.wait_cycles(2)
        self.assertEqual(self.executor.call_count, 2)
        self.messages.append({"sender": "CORRESPONDENT", "body": "Hello", "createdAt": self.now.isoformat()})
        proof = self.wait_cycles(1)[0]
        self.assertEqual(self.executor.call_count, 3)
        self.assertTrue(proof["has_reply"])

    def test_a_verdict_past_the_bound_is_judged_again(self):
        self.config["outreach_agent"]["judgment_reuse_seconds"] = 3 * 3600
        self.wait_cycles(5)
        self.assertEqual(self.executor.call_count, 2)


# Run only this class's own tests, not the inherited fixture's.
for _name in dir(test_outreach_agent.OutreachAgentTests):
    if _name.startswith("test_") and _name not in JudgmentReuseTests.__dict__:
        setattr(JudgmentReuseTests, _name, None)


if __name__ == "__main__":
    unittest.main()
