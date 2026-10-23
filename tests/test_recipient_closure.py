import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import json
import socket
import sqlite3
import unittest
from unittest.mock import Mock, patch

import engagement
import manual_recipient_closure as manual
import outreach
import research_workflow


class RecipientClosureTests(unittest.TestCase):
    def setUp(self):
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        self.addCleanup(self.database.close)
        outreach.initialize_actions(self.database)

    def test_manual_business_scenarios(self):
        with patch.object(socket.socket, "connect", side_effect=AssertionError("No network")):
            self.assertEqual(manual.validate()["state"], "PASS")

    def test_closure_rollback_is_atomic_and_never_commits_caller(self):
        outreach.enqueue_action(self.database, manual.action("pending"))
        self.database.execute("BEGIN IMMEDIATE")
        engagement.close_outreach(self.database, manual.PROFILE, "Explicit", manual.NOW)
        self.assertTrue(self.database.in_transaction)
        self.database.rollback()
        self.assertIsNone(engagement.closure_status(self.database, manual.PROFILE))
        self.assertEqual(self.database.execute("SELECT state FROM outreach_actions").fetchone()[0], "PENDING")
        self.assertIsNone(self.database.execute("SELECT 1 FROM sqlite_master WHERE name='suppressions'").fetchone())

    def test_invalid_reason_source_and_naive_time_do_not_close(self):
        for fields in ({"reason": ""}, {"source": " "}, {"now": "2026-09-17T12:00:00"}):
            arguments = {"reason": "Explicit", "source": "synthetic", "now": manual.NOW, **fields}
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                engagement.close_outreach(self.database, manual.PROFILE, **arguments)
        self.assertIsNone(engagement.closure_status(self.database, manual.PROFILE))

    def test_late_alias_queue_closes_before_inspection(self):
        outreach.enqueue_action(self.database, manual.action("later-alias", manual.ALIAS))
        engagement.close_outreach(self.database, manual.PROFILE, "Explicit", manual.NOW)
        research_workflow.initialize(self.database)
        research_workflow.register_alias(self.database, "contact", manual.ALIAS, manual.PROFILE, {"source": "verified synthetic"})
        client = Mock()
        result = outreach.cycle(self.database, manual.CONFIG, client, manual.NOW)
        self.assertEqual(self.database.execute("SELECT state FROM outreach_actions WHERE action_id='later-alias'").fetchone()[0], "CLOSED")
        self.assertEqual(result["items"], [])
        self.assertEqual(client.mock_calls, [])

    def test_closure_keeps_uncertain_action_reconcilable_without_resend(self):
        manual.seed_claim(self.database, "uncertain", "UNCERTAIN_NO_RETRY")
        engagement.close_outreach(self.database, manual.PROFILE, "Explicit", manual.NOW)
        claim = dict(self.database.execute("SELECT * FROM outreach_action_claims").fetchone())
        action = json.loads(claim["payload"])
        receipt = {"independent": True, "action_id": claim["action_id"], "identity": claim["identity"],
                   "payload_hash": claim["payload_hash"], "operation": "send_message", "outcome": "SENT",
                   "receipt_id": "synthetic-receipt", "evidence": "synthetic independent observation",
                   "occurred_at": manual.NOW.isoformat(), "observed_at": manual.NOW.isoformat(),
                   "sender_id": claim["sender_id"], "conversation_id": action["conversation_id"]}
        client = Mock()
        client.reconcile_action.return_value = receipt
        result = outreach.cycle(self.database, manual.CONFIG, client, manual.NOW)
        self.assertEqual(result["items"][0]["state"], "CONFIRMED_SENT")
        client.execute_claimed.assert_not_called()
        self.assertEqual(dict(self.database.execute("SELECT * FROM outreach_action_claims").fetchone()), claim)
        self.assertEqual(engagement.stop_state(self.database, manual.PROFILE), "OUTREACH_CLOSED")

    def test_known_alias_closure_survives_alias_archival(self):
        research_workflow.initialize(self.database)
        research_workflow.register_alias(self.database, "contact", manual.ALIAS, manual.PROFILE, {"source": "verified synthetic"})
        engagement.close_outreach(self.database, manual.PROFILE, "Explicit", manual.NOW)
        with self.database:
            self.database.execute("DELETE FROM rw_aliases")
        self.assertEqual(engagement.stop_state(self.database, manual.ALIAS), "OUTREACH_CLOSED")

    def test_later_verified_alias_closure_survives_archival(self):
        research_workflow.initialize(self.database)
        closed = engagement.close_outreach(self.database, manual.PROFILE, "Explicit original", manual.NOW, source="original decision")
        outreach.enqueue_action(self.database, manual.action("later-alias", manual.ALIAS))
        research_workflow.register_alias(self.database, "contact", manual.ALIAS, manual.PROFILE, {"source": "verified synthetic"})
        with self.database:
            self.database.execute("DELETE FROM rw_aliases")
        archived = engagement.closure_status(self.database, manual.ALIAS)
        self.assertEqual({field: archived[field] for field in ("state", "closed_at", "reason", "source")},
                         {field: closed[field] for field in ("state", "closed_at", "reason", "source")})
        self.assertEqual(engagement.stop_state(self.database, manual.ALIAS), "OUTREACH_CLOSED")
        self.assertEqual(self.database.execute("SELECT state FROM outreach_actions WHERE action_id='later-alias'").fetchone()[0], "CLOSED")
        self.assertIsNotNone(self.database.execute("SELECT 1 FROM suppressions WHERE identity=?", (archived["identity"],)).fetchone())

    def test_later_alias_and_closure_propagation_roll_back_together(self):
        research_workflow.initialize(self.database)
        engagement.close_outreach(self.database, manual.PROFILE, "Explicit", manual.NOW)
        outreach.enqueue_action(self.database, manual.action("later-alias", manual.ALIAS))
        self.database.execute("BEGIN IMMEDIATE")
        research_workflow.register_alias(self.database, "contact", manual.ALIAS, manual.PROFILE, {"source": "verified synthetic"})
        self.assertTrue(self.database.in_transaction)
        self.database.rollback()
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM rw_aliases").fetchone()[0], 0)
        self.assertIsNone(engagement.closure_status(self.database, manual.ALIAS))
        self.assertEqual(self.database.execute("SELECT state FROM outreach_actions WHERE action_id='later-alias'").fetchone()[0], "PENDING")


if __name__ == "__main__":
    unittest.main()
