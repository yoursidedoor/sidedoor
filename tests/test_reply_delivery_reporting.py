import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import contextlib
import json
import sqlite3
import unittest

import engagement
import manual_reply_execution as manual
import reply_execution
import reporting
import synthetic_resume  # noqa: F401 (synthetic designated resume; the owner's is not in the repo)


def setUpModule():
    # Fixture inbound messages are dated before the 2026-09-30 backlog cutoff, which applies
    # only to the real backlog on file (owner, 2026-09-30: "dont send these replies").
    global _BACKLOG_PATCH
    import datetime as _dt
    from unittest import mock as _mock
    import reply_execution as _reply_execution
    _BACKLOG_PATCH = _mock.patch.object(_reply_execution, "BACKLOG_CUTOFF",
                                        _dt.datetime(2000, 1, 1, tzinfo=_dt.timezone.utc))
    _BACKLOG_PATCH.start()


def tearDownModule():
    _BACKLOG_PATCH.stop()


class ReplyDeliveryReportingTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(manual.isolated_boundaries())
        self.fixture = self.enterContext(manual.Fixture())
        self.fixture.seed()
        self.fixture.classify()
        self.original = self.fixture.database.execute("SELECT handoff FROM reply_worker_results").fetchone()[0]

    def record(self):
        summary = reporting.reply_decisions(self.fixture.database, self.fixture.now.isoformat())
        self.assertNotIn(manual.TEXT, json.dumps(summary))
        self.assertNotIn("Could you tell me more?", json.dumps(summary))
        self.assertEqual(self.original, self.fixture.database.execute("SELECT handoff FROM reply_worker_results").fetchone()[0])
        record = summary["records"][0]
        self.assertFalse(record["can_send"])
        return record

    def confirm(self):
        self.fixture.cycle()
        self.fixture.cycle(seconds=1)
        self.assertEqual("CONFIRMED_SENT", self.fixture.actions()[0]["state"])

    def test_proposal_queue_block_ack_receipt_closure_and_restart(self):
        self.assertEqual("UNSENT_PROPOSAL", self.record()["delivery_state"])
        self.assertTrue(self.record()["unsent_draft_available"])
        reply_execution.promote(self.fixture.database, self.fixture.config, self.fixture.now)
        queued = self.record()
        self.assertEqual("QUEUED", queued["delivery_state"])
        self.assertFalse(queued["unsent_draft_available"])
        self.assertEqual(self.fixture.actions()[0]["action_id"], queued["delivery_actions"][0]["action_id"])
        with self.fixture.database:
            self.fixture.database.execute("UPDATE outreach_actions SET state='BLOCKED',result=?",
                                          (json.dumps(["PRIVATE BLOCKER"]),))
        self.assertEqual("BLOCKED", self.record()["delivery_state"])
        self.assertNotIn("PRIVATE BLOCKER", json.dumps(self.record()))
        self.fixture.cycle()
        acknowledged = self.record()
        self.assertEqual("ACKNOWLEDGED_UNCONFIRMED", acknowledged["delivery_state"])
        self.assertIsNone(acknowledged["delivery_actions"][0]["receipt_reference"])
        claim = tuple(self.fixture.database.execute("SELECT * FROM outreach_action_claims").fetchone())
        self.fixture.cycle(seconds=1)
        confirmed = self.record()
        self.assertEqual("CONFIRMED_SENT", confirmed["delivery_state"])
        receipt = json.loads(self.fixture.actions()[0]["result"])
        self.assertEqual(receipt["receipt_id"], confirmed["delivery_actions"][0]["receipt_reference"])
        self.assertEqual({"action_id", "action_state", "delivery_state", "receipt_reference"},
                         set(confirmed["delivery_actions"][0]))
        engagement.close_outreach(self.fixture.database, manual.PROFILE, "Synthetic closure", self.fixture.now)
        self.fixture.restart()
        closed = self.record()
        self.assertEqual("CLOSED", closed["state"])
        self.assertEqual(confirmed["delivery_actions"], closed["delivery_actions"])
        self.assertEqual(claim, tuple(self.fixture.database.execute("SELECT * FROM outreach_action_claims").fetchone()))
        self.assertEqual(1, len(self.fixture.requests))

    def test_lost_ack_remains_uncertain_until_independent_receipt(self):
        self.fixture.lost_ack = True
        self.fixture.receipts_visible = False
        self.fixture.cycle()
        self.assertEqual("UNCERTAIN", self.record()["delivery_state"])
        self.assertIsNone(self.record()["delivery_actions"][0]["receipt_reference"])
        self.fixture.restart()
        self.assertEqual("UNCERTAIN", self.record()["delivery_state"])
        self.fixture.reveal_receipts()
        self.fixture.cycle(seconds=1)
        self.assertEqual("CONFIRMED_SENT", self.record()["delivery_state"])
        self.assertEqual(1, len(self.fixture.requests))

    def test_confirmed_state_requires_intact_claim_and_independent_receipt(self):
        self.confirm()
        action = self.fixture.actions()[0]
        receipt = json.loads(action["result"])
        for field, value in (("independent", False), ("sender_id", 999), ("conversation_id", "wrong-thread"),
                             ("payload_hash", "wrong-hash"), ("receipt_id", None), ("evidence", None)):
            with self.subTest(field=field):
                with self.fixture.database:
                    self.fixture.database.execute("UPDATE outreach_actions SET result=?",
                                                  (json.dumps(dict(receipt, **{field: value})),))
                record = self.record()
                self.assertEqual("UNCERTAIN", record["delivery_state"])
                self.assertEqual("CONFIRMED_SENT", record["delivery_actions"][0]["action_state"])
                self.assertIsNone(record["delivery_actions"][0]["receipt_reference"])
        with self.fixture.database:
            self.fixture.database.execute("UPDATE outreach_actions SET result=?,claimed_at=?",
                                          (action["result"], "2026-09-17T00:00:00+00:00"))
        self.assertEqual("UNCERTAIN", self.record()["delivery_state"])

    def test_reply_binding_excludes_other_tasks_sources_and_purposes(self):
        reply_execution.promote(self.fixture.database, self.fixture.config, self.fixture.now)
        original = json.loads(self.fixture.actions()[0]["payload"])
        for field in ("reply_task_id", "reply_source_key", "reply_source_id", "purpose"):
            with self.subTest(field=field):
                with self.fixture.database:
                    self.fixture.database.execute("UPDATE outreach_actions SET payload=?",
                                                  (json.dumps(dict(original, **{field: "unrelated"})),))
                self.assertEqual("UNSENT_PROPOSAL", self.record()["delivery_state"])
                self.assertEqual([], self.record()["delivery_actions"])

    def test_malformed_action_and_receipt_do_not_leak_or_crash(self):
        reply_execution.promote(self.fixture.database, self.fixture.config, self.fixture.now)
        original = self.fixture.actions()[0]["payload"]
        for payload in ("{", "[]", json.dumps({"purpose": "REPLY", "reply_task_id": ["PRIVATE"]})):
            with self.subTest(payload=payload):
                with self.fixture.database:
                    self.fixture.database.execute("UPDATE outreach_actions SET payload=?", (payload,))
                self.assertEqual([], self.record()["delivery_actions"])
        with self.fixture.database:
            self.fixture.database.execute("UPDATE outreach_actions SET payload=?,state='CONFIRMED_SENT',result='[]'",
                                          (original,))
        self.assertEqual("UNCERTAIN", self.record()["delivery_state"])

    def test_daily_payload_and_notification_keep_only_safe_delivery_metadata(self):
        self.confirm()
        payload = reporting.daily(self.fixture.database, dict(self.fixture.config, daily_report_hour=0),
                                  self.fixture.now.isoformat())
        self.assertEqual("CONFIRMED_SENT", payload["reply_decisions"]["records"][0]["delivery_state"])
        self.assertEqual("CONFIRMED_SENT", payload["message_actions"]["records"][0]["state"])
        stored = self.fixture.database.execute("SELECT payload FROM daily_reports").fetchone()[0]
        notification = reporting.full_report(self.fixture.database, json.loads(stored)["day"])
        self.assertIn("Saved reply delivery states", notification)
        self.assertIn("CONFIRMED_SENT", notification)
        for private in (manual.TEXT, "Could you tell me more?"):
            self.assertNotIn(private, stored)
            self.assertNotIn(private, notification)

    def test_service_status_persists_post_execution_delivery_state(self):
        self.confirm()
        status = json.loads((self.fixture.root / "reports/service-status.json").read_text())
        record = status["reply_decisions"]["records"][0]
        self.assertEqual("CONFIRMED_SENT", record["delivery_state"])
        self.assertEqual(self.record()["delivery_actions"], record["delivery_actions"])
        self.assertFalse(record["can_send"])
        self.assertNotIn(manual.TEXT, json.dumps(status))

    def test_missing_action_table_remains_unknown_without_initialization(self):
        with contextlib.closing(sqlite3.connect(":memory:")) as database:
            database.execute("CREATE TABLE reply_worker_results (task_id,output_version,input_sha256,output_sha256,handoff)")
            database.execute("INSERT INTO reply_worker_results VALUES (?,?,?,?,?)",
                             ("synthetic", 1, "input", "output", self.original))
            self.assertEqual("UNKNOWN", reporting.reply_decisions(database)["records"][0]["delivery_state"])
            self.assertIsNone(database.execute("SELECT name FROM sqlite_master WHERE name='outreach_actions'").fetchone())


if __name__ == "__main__":
    unittest.main()
