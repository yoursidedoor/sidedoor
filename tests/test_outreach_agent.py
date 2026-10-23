import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import copy
import datetime as dt
import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import orchestrator
import outreach
import outreach_agent
import outreach_provider
import test_outreach
import test_outreach_promotion
import workflow_tasks


ROOT = Path(__file__).resolve().parents[1]


class DraftCapacityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_outreach_promotion.PromotionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.history_config()
        self.config = dict(self.fixture.config, timezone="America/Los_Angeles", model="synthetic", codex="NEVER")
        self.database = self.fixture.database
        self.now = dt.datetime.fromisoformat(test_outreach_promotion.NOW)
        self.client = Mock()
        self.executor = Mock(side_effect=AssertionError("No inference when full"))
        for target in ("socket.socket.connect", "socket.getaddrinfo", "subprocess.Popen"):
            boundary = patch(target, side_effect=AssertionError("External execution forbidden"))
            boundary.start()
            self.addCleanup(boundary.stop)

    def fill(self):
        with self.database:
            self.assertTrue(workflow_tasks.reserve_compute(self.database, "synthetic-full", self.now.isoformat(),
                                                          10800, self.config["timezone"]))

    def run_cycle(self, now=None):
        now = now or self.now
        with patch("outreach_provider._now", return_value=now.isoformat()):
            return outreach_agent.prepare_drafts(ROOT, self.database, self.config, self.client,
                                                  self.executor, now)["items"][0]

    def restart(self):
        self.database.close()
        self.fixture.database = sqlite3.connect(self.fixture.path)
        self.database = self.fixture.database
        self.database.row_factory = sqlite3.Row

    def test_known_full_budget_defers_without_claim_and_survives_restart(self):
        self.fill()
        first = self.run_cycle()
        self.assertEqual(first["state"], "RETRY_WAIT")
        self.assertEqual(first["blocker"], "OUTREACH_AI_SHARED_BUDGET_EXHAUSTED")
        self.assertEqual(first["attempts"], 0)
        self.assertEqual(first["next_at"], "2026-09-18T16:00:00+00:00")
        self.restart()
        self.assertEqual(self.run_cycle(self.now + dt.timedelta(hours=2)), first)
        self.client.call.assert_not_called()
        self.executor.assert_not_called()
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM workflow_task_events WHERE event='CLAIMED'").fetchone()[0], 0)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM workflow_task_events WHERE event='CAPACITY_DEFERRED'").fetchone()[0], 1)

    def test_old_generic_failures_keep_attempts_and_resume_at_day_start(self):
        with patch.object(outreach_agent.Adapter, "inspect", side_effect=TimeoutError("Synthetic old failure")):
            first = self.run_cycle()
            second = self.run_cycle(self.now + dt.timedelta(minutes=15))
        self.assertEqual(second["attempts"], 2)
        self.assertEqual(second["blocker"], "OUTREACH_INVESTIGATION_TimeoutError")
        self.fill()
        self.restart()
        saved = self.run_cycle(self.now + dt.timedelta(hours=2))
        self.assertEqual(saved["attempts"], 2)
        self.assertEqual(saved["deadline"], first["deadline"])
        self.assertEqual(saved["blocker"], "OUTREACH_AI_SHARED_BUDGET_EXHAUSTED")
        self.assertEqual(saved["next_at"], "2026-09-18T16:00:00+00:00")
        self.client.call.assert_not_called()
        reset = dt.datetime.fromisoformat(saved["next_at"])
        with patch.object(outreach_agent.Adapter, "inspect", return_value=self.fixture.history_proof(reset.isoformat())) as inspect:
            resumed = self.run_cycle(reset)
        inspect.assert_called_once()
        self.assertEqual(resumed["state"], "COMPLETE")
        self.assertEqual(resumed["attempts"], 3)
        self.assertEqual(resumed["deadline"], first["deadline"])
        self.assertEqual(self.database.execute("SELECT SUM(seconds) FROM worker_compute_reservations").fetchone()[0], 10800)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM workflow_task_events WHERE event='RETRY_WAIT'").fetchone()[0], 2)

    def test_capacity_race_retains_claimed_failed_attempt_and_specific_audit(self):
        def read(operation, arguments):
            self.fill()
            if operation == "get_linked_in_account_by_id":
                return {"id": self.config["sender_id"], "profileUrl": self.config["sender_profile"],
                        "authIsValid": True, "isActive": True}
            return {"items": [], "totalCount": 0}

        self.client.call.side_effect = read
        first = self.run_cycle()
        self.assertEqual(first["attempts"], 1)
        self.assertEqual(first["blocker"], "OUTREACH_AI_SHARED_BUDGET_EXHAUSTED")
        self.assertEqual(first["next_at"], "2026-09-18T16:00:00+00:00")
        failure = json.loads(self.database.execute("SELECT record FROM outreach_agent_audit WHERE kind='FAILURE'").fetchone()[0])
        self.assertEqual(failure["code"], first["blocker"])
        self.assertEqual(failure["next_at"], first["next_at"])
        calls = self.client.call.call_count
        self.restart()
        self.assertEqual(self.run_cycle(self.now + dt.timedelta(hours=2)), first)
        self.assertEqual(self.client.call.call_count, calls)
        self.executor.assert_not_called()
        self.client._request.assert_not_called()

    def test_a_passed_deadline_never_ends_the_history_check(self):
        """Owner, 2026-09-28: no deadline for this check, only its 7 tries."""
        with patch.object(outreach_agent.Adapter, "inspect", side_effect=TimeoutError("Synthetic prior failure")):
            first = self.run_cycle()
        with self.database:
            self.database.execute("UPDATE workflow_tasks SET deadline=?, status='EXHAUSTED', blocker='DEADLINE_OR_RETRY_LIMIT', next_at=NULL WHERE task_id=?",
                                  ("2026-09-18T07:00:00+00:00", first["task_id"]))
        saved = self.run_cycle(self.now + dt.timedelta(days=30))
        self.assertNotEqual(saved["state"], "EXHAUSTED")
        self.assertGreaterEqual(saved["attempts"], 1)
        events = [row[0] for row in self.database.execute("SELECT event FROM workflow_task_events WHERE task_id=?", (first["task_id"],))]
        self.assertIn("REOPENED_NO_DEADLINE", events)

    def test_claim_retry_and_exhausted_summaries_include_durable_details(self):
        with patch.object(outreach_agent.Adapter, "inspect", side_effect=TimeoutError("Synthetic failure")) as inspect:
            first = self.run_cycle()
            waiting = self.run_cycle(self.now + dt.timedelta(minutes=1))
            self.assertEqual(waiting, first)
            self.assertEqual(inspect.call_count, 1)
            self.run_cycle(self.now + dt.timedelta(minutes=15))
            # Seven attempts on a growing backoff, not three then a grave (owner, 2026-09-23).
            # At three, a transient inspection failure made the draft permanently uncontactable:
            # 6 of 7 outreach tasks were EXHAUSTED with next_at NULL and 4 of 5 people could
            # never be reached. The durable detail this case exists to check - the blocker,
            # next_action, next_at, attempts and deadline carried on every summary - is asserted
            # below and is unchanged.
            retrying = self.run_cycle(self.now + dt.timedelta(minutes=75))
            self.assertEqual(retrying["state"], "RETRY_WAIT")
            self.assertGreaterEqual(retrying["attempts"], 3)
            self.assertLess(retrying["attempts"], 7)
            exhausted = retrying
        for field in ("blocker", "next_action", "next_at", "attempts", "deadline"):
            self.assertIn(field, exhausted)

    def test_day_start_uses_local_clock_across_dst_changes(self):
        # The shared budget resets at the 9:00 Pacific service-day start (2026-09-30; was midnight).
        for now, expected in (("2026-03-08T08:30:00+00:00", "2026-03-08T16:00:00+00:00"),
                              ("2026-11-01T07:30:00+00:00", "2026-11-01T17:00:00+00:00")):
            with self.subTest(now=now):
                error = outreach_agent.SharedBudgetExhausted(self.config, dt.datetime.fromisoformat(now))
                self.assertEqual(error.reset_at.isoformat(), expected)


class OutreachAgentTests(unittest.TestCase):
    def setUp(self):
        test_outreach.ActionTests.setUp(self)
        for target in ("socket.socket.connect", "socket.getaddrinfo", "subprocess.Popen",
                       "reply_worker._execute"):
            boundary = patch(target, side_effect=AssertionError("External execution forbidden"))
            boundary.start()
            self.addCleanup(boundary.stop)
        clock = patch("outreach_provider._now", side_effect=lambda: self.now.isoformat())
        clock.start()
        self.addCleanup(clock.stop)
        self.proof["entitlement_evidence"] = "synthetic-authorization"
        self.config.update(
            sender_profile="https://www.linkedin.com/in/synthetic-sender",
            outreach_allowed_purposes=["INITIAL", "FIRST_MESSAGE"],
            model="synthetic-injected", codex="NEVER_EXECUTED", timezone="UTC",
            outreach_agent={"enabled": True, "timeout_seconds": 1},
            outreach_promotion_contexts={self.action["profile"]: {"proof": self.proof}})
        self.messages = []
        self.room_overrides = {}
        self.packets = []
        self.decision_override = {}
        self.during_inference = None
        self.during_send = None
        self.client = Mock()
        self.client.call.side_effect = self.read
        self.client._request.side_effect = self.send
        self.executor = Mock(side_effect=self.judge)
        self.adapter = self.make_adapter(self.database)
        outreach.enqueue_action(self.database, self.action)

    def make_adapter(self, database):
        def collector(request):
            proof = copy.deepcopy(self.proof)
            return {"source": proof.get("evidence"), "authenticated": True,
                    "sender_id": request["sender_id"], "profile": request["profile"],
                    "observed_at": proof.get("checked_at"), "raw": {"synthetic": True}, "proof": proof,
                    "coverage": {"direct_messages": "ALL_TIME", "sent_invitations": "ALL_TIME",
                                 "pending_invitations": "COMPLETE"}}

        adapter = outreach_agent.Adapter(ROOT, database, self.config, self.client, self.executor,
                                         history_collector=collector)
        self.assertIsInstance(adapter.provider, outreach_provider.Provider)
        return adapter

    def read(self, operation, arguments):
        self.assertIn(operation, outreach_provider.READ_TOOLS)
        account = {"id": self.config["sender_id"], "profileUrl": self.config["sender_profile"],
                   "authIsValid": True, "isActive": True}
        if operation == "get_linked_in_account_by_id":
            self.assertEqual(arguments, {"accountId": self.config["sender_id"]})
            return account
        if operation == "get_campaigns_for_lead":
            return {"items": [], "totalCount": 0}
        if operation in {"get_lists_for_lead", "get_my_network_for_sender"}:
            return {"items": [], "totalCount": 0}
        if operation == "get_lead":
            return {"profileUrl": self.action["profile"], "linkedin_id": "synthetic-lead"}
        room = {"id": self.action["conversation_id"], "groupChat": False,
                "blockedByMe": False, "blockedByParticipant": False,
                "linkedInAccountId": self.config["sender_id"], "linkedInAccount": account,
                "correspondentProfile": {"profileUrl": self.action["profile"]},
                "messages": copy.deepcopy(self.messages), "totalMessages": len(self.messages)}
        if operation == "get_conversations_v2":
            self.assertEqual(arguments["linkedInAccountIds"], [self.config["sender_id"]])
            self.assertEqual(arguments["leadProfileUrl"], self.action["profile"])
            return {"items": [room], "totalCount": 1}
        self.assertEqual(arguments, {"accountId": self.config["sender_id"],
                                     "conversationId": self.action["conversation_id"]})
        room.update(copy.deepcopy(self.room_overrides))
        return room

    def send(self, operation, arguments):
        self.assertEqual(operation, "send_message")
        self.assertEqual(arguments, {"conversationId": self.action["conversation_id"],
                                    "linkedInAccountId": self.config["sender_id"],
                                    "message": self.action["message"], "subject": ""})
        observer = orchestrator.connect(self.root)
        try:
            row = observer.execute("SELECT state,result FROM outreach_actions").fetchone()
            self.assertEqual(row["state"], "REQUEST_STARTED")
            self.assertEqual(json.loads(row["result"])["provider_request"]["arguments"], arguments)
            self.assertEqual(observer.execute("SELECT count(*) FROM outreach_action_claims").fetchone()[0], 1)
        finally:
            observer.close()
        if self.during_send:
            self.during_send()
        self.messages.append({"createdAt": self.now.isoformat(), "body": arguments["message"],
                              "subject": None, "sender": "ME"})
        return {"success": True}

    def judge(self, config, prompt, schema, timeout):
        self.assertEqual(config["model"], "synthetic-injected")
        self.assertEqual(timeout, 1)
        packet = json.loads(prompt.split("UNTRUSTED_SOURCE_JSON:\n", 1)[1])
        source_hash = packet.pop("source_sha256")
        serialized = json.dumps(packet, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        self.assertEqual(source_hash, hashlib.sha256(serialized.encode()).hexdigest())
        self.assertEqual(packet["action_sha256"], outreach_agent.digest(packet["action"]))
        self.packets.append(copy.deepcopy(packet))
        rooms = [(index, item) for index, item in enumerate(packet["observation"]["observations"])
                 if item["operation"] == "get_chatroom" and item.get("raw", {}).get("messages")]
        sent = packet["phase"] == "RECONCILIATION" and bool(rooms)
        output = {"output_version": outreach_agent.VERSION, "source_sha256": source_hash,
                  "decision": "READY" if packet["phase"] == "INSPECTION" else "SENT" if sent else "UNKNOWN",
                  "reason": "Synthetic decision requiring independent verification",
                  "next_action": "Inspect authenticated evidence before advancing",
                  "findings": {"prior_outreach": False, "has_reply": False,
                               "negative_reply": False, "opt_out": False},
                  "conversation_id": packet["action"]["conversation_id"],
                  "route": "EXISTING_THREAD",
                  "receipt_observation": rooms[0][0] if sent else None,
                  "receipt_message": 0 if sent else None}
        output.update(self.decision_override)
        self.assertEqual(set(output), set(json.loads(schema)["required"]))
        if self.during_inference:
            self.during_inference()
        return json.dumps(output)

    def cycle(self):
        return outreach.cycle(self.database, self.config, self.adapter, self.now)

    def state(self):
        return self.database.execute("SELECT state FROM outreach_actions").fetchone()[0]

    def assert_no_send(self):
        self.client._request.assert_not_called()
        self.assertEqual(self.database.execute("SELECT count(*) FROM outreach_action_claims").fetchone()[0], 0)

    def restart(self):
        self.database.close()
        self.database = orchestrator.connect(self.root)
        self.addCleanup(self.database.close)
        self.adapter = self.make_adapter(self.database)

    def test_source_hash_binds_action_observations_and_direct_history(self):
        self.adapter.inspect(self.action, self.config)
        previous_hash = outreach_agent.digest(self.packets[-1])
        self.proof["evidence"] = "different-authenticated-history"
        self.decision_override["source_sha256"] = previous_hash
        result = self.cycle()
        self.assertNotEqual(previous_hash, outreach_agent.digest(self.packets[-1]))
        self.assertEqual(result["items"][0]["state"], "BLOCKED")
        self.assert_no_send()
        records = self.database.execute("SELECT kind,record FROM outreach_agent_audit").fetchall()
        observations = [json.loads(row["record"]) for row in records if row["kind"] == "OBSERVATION"]
        self.assertEqual(len(observations), 2)
        for record in observations:
            self.assertEqual(record["source_sha256"], outreach_agent.digest(record["packet"]))
        self.assertEqual(sum(row["kind"] == "FAILURE" for row in records), 1)
        self.assertEqual(sum(row["kind"] == "DECISION" for row in records), 1)

    def test_ai_false_findings_cannot_erase_observed_outreach_or_reply(self):
        for sender, finding in (("ME", "prior_outreach"), ("CORRESPONDENT", "has_reply")):
            with self.subTest(sender=sender):
                self.messages[:] = [{"sender": sender, "body": "Existing conversation"}]
                proof = self.adapter.inspect(self.action, self.config)
                self.assertTrue(proof[finding])
                self.assertTrue(outreach.action_gates(self.database, self.config, self.action, proof, self.now))
        self.assert_no_send()

    def test_sent_without_durable_request_marker_remains_unknown(self):
        with patch.object(self.adapter, "execute_claimed", side_effect=TimeoutError("Synthetic pre-transport crash")):
            self.assertEqual(self.cycle()["items"][0]["state"], "UNCERTAIN_NO_RETRY")
        self.messages.append({"createdAt": self.now.isoformat(), "body": self.action["message"],
                              "subject": None, "sender": "ME"})
        saved = json.loads(self.database.execute("SELECT result FROM outreach_actions").fetchone()[0])
        self.assertNotIn("provider_request", saved)
        self.assertEqual(self.database.execute("SELECT count(*) FROM outreach_action_claims").fetchone()[0], 1)
        receipt = self.adapter.reconcile(self.action, self.config)
        self.assertEqual(receipt["agent_decision"], "SENT")
        self.assertEqual(receipt["state"], "UNKNOWN")
        self.assertNotIn("independent", receipt)
        self.assertIsNone(self.packets[-1]["request"])
        self.restart()
        self.now += dt.timedelta(seconds=2)
        self.assertEqual(self.cycle()["attempts"], 0)
        self.assertEqual(self.state(), "UNCERTAIN_NO_RETRY")
        self.assertEqual(self.database.execute("SELECT count(*) FROM engagement_events").fetchone()[0], 0)
        self.client._request.assert_not_called()

    def test_sent_after_claim_but_before_request_started_remains_unknown(self):
        execute = self.adapter.execute_claimed

        def delayed(*arguments, **keywords):
            self.now += dt.timedelta(seconds=2)
            return execute(*arguments, **keywords)

        with patch.object(self.adapter, "execute_claimed", side_effect=delayed):
            self.assertEqual(self.cycle()["items"][0]["state"], "ACKNOWLEDGED_UNCONFIRMED")
        claim = self.database.execute("SELECT claimed_at FROM outreach_action_claims").fetchone()[0]
        saved = json.loads(self.database.execute("SELECT result FROM outreach_actions").fetchone()[0])
        request = saved["provider_request"]
        occurred = dt.datetime.fromisoformat(claim) + dt.timedelta(seconds=1)
        self.assertLess(dt.datetime.fromisoformat(claim), occurred)
        self.assertLess(occurred, dt.datetime.fromisoformat(request["started_at"]))
        self.messages[0]["createdAt"] = occurred.isoformat()
        receipt = self.adapter.reconcile(self.action, self.config)
        self.assertEqual(receipt["agent_decision"], "SENT")
        self.assertEqual(receipt["state"], "UNKNOWN")
        self.assertNotIn("independent", receipt)
        self.assertEqual(self.packets[-1]["request"], request)
        self.restart()
        self.now += dt.timedelta(seconds=2)
        self.assertEqual(self.cycle()["attempts"], 0)
        self.assertEqual(self.state(), "ACKNOWLEDGED_UNCONFIRMED")
        self.assertEqual(self.database.execute("SELECT count(*) FROM engagement_events").fetchone()[0], 0)
        self.client._request.assert_called_once()

    def test_receipt_requires_exact_claim_thread_content_sender_and_time(self):
        self.assertEqual(self.cycle()["items"][0]["state"], "ACKNOWLEDGED_UNCONFIRMED")
        claim = dict(self.database.execute("SELECT * FROM outreach_action_claims").fetchone())
        original = copy.deepcopy(self.messages[0])
        self.now += dt.timedelta(seconds=2)
        variants = {
            "before_claim": {"createdAt": (dt.datetime.fromisoformat(claim["claimed_at"]) - dt.timedelta(microseconds=1)).isoformat()},
            "after_observation": {"createdAt": (self.now + dt.timedelta(microseconds=1)).isoformat()},
            "invalid_timestamp": {"createdAt": "unknown"},
            "wrong_content": {"body": original["body"] + " changed"},
            "wrong_subject": {"subject": "unexpected subject"},
            "wrong_sender": {"sender": "CORRESPONDENT"},
            "wrong_thread": {},
            "wrong_account": {},
        }
        for name, changes in variants.items():
            with self.subTest(receipt=name):
                self.messages[:] = [dict(original, **changes)]
                self.room_overrides = {"id": "other-thread"} if name == "wrong_thread" else (
                    {"linkedInAccountId": self.config["sender_id"] + 1} if name == "wrong_account" else {})
                receipt = self.adapter.reconcile(self.action, self.config)
                self.assertEqual(receipt["agent_decision"], "SENT")
                self.assertEqual(receipt["state"], "UNKNOWN")
                self.assertNotIn("independent", receipt)
        self.room_overrides = {}
        self.messages[:] = [original]
        receipt = self.adapter.reconcile(self.action, self.config)
        self.assertEqual(receipt["outcome"], "SENT")
        for field in ("action_id", "identity", "payload_hash", "sender_id"):
            self.assertEqual(receipt[field], claim[field])
        self.assertEqual(receipt["occurred_at"], claim["claimed_at"])
        self.assertEqual(receipt["observed_at"], self.now.isoformat())
        self.assertEqual(self.database.execute("SELECT count(*) FROM engagement_events").fetchone()[0], 0)
        self.assertEqual(self.cycle()["items"][0]["state"], "CONFIRMED_SENT")
        self.client._request.assert_called_once()

    def test_absence_is_unknown_and_timeout_restart_never_sends_again(self):
        self.client._request.side_effect = TimeoutError("Synthetic lost acknowledgment")
        self.assertEqual(self.cycle()["items"][0]["state"], "UNCERTAIN_NO_RETRY")
        self.decision_override["decision"] = "NOT_SENT"
        receipt = self.adapter.reconcile(self.action, self.config)
        self.assertEqual(receipt["state"], "UNKNOWN")
        self.assertEqual(receipt["agent_decision"], "NOT_SENT")
        self.restart()
        for advance in (2, 901, 3601, 86400):
            self.now += dt.timedelta(seconds=advance)
            self.assertEqual(self.cycle()["attempts"], 0)
        self.assertEqual(self.state(), "UNCERTAIN_NO_RETRY")
        result = json.loads(self.database.execute("SELECT result FROM outreach_actions").fetchone()[0])
        self.assertIn("provider_request", result)
        self.assertEqual(self.database.execute("SELECT count(*) FROM engagement_events").fetchone()[0], 0)
        self.client._request.assert_called_once()

    def test_action_changed_during_inference_cannot_advance(self):
        def mutate():
            other = orchestrator.connect(self.root)
            try:
                with other:
                    changed = json.dumps(dict(self.action, message="Concurrent replacement"), sort_keys=True)
                    other.execute("UPDATE outreach_actions SET payload=?,payload_hash=? WHERE action_id=?",
                                  (changed, hashlib.sha256(changed.encode()).hexdigest(), self.action["action_id"]))
            finally:
                other.close()

        self.during_inference = mutate
        self.cycle()
        self.assertEqual(self.state(), "BLOCKED")
        self.assert_no_send()
        self.assertEqual(self.database.execute("SELECT count(*) FROM outreach_agent_audit WHERE kind='FAILURE'").fetchone()[0], 1)
        self.assertEqual(self.database.execute("SELECT count(*) FROM outreach_agent_audit WHERE kind='DECISION'").fetchone()[0], 0)

    def test_overlapping_consumer_restart_and_duplicate_preserve_one_send(self):
        overlapping = []

        def consume():
            other = orchestrator.connect(self.root)
            try:
                overlapping.append(outreach.cycle(other, self.config, self.make_adapter(other), self.now))
            finally:
                other.close()

        self.during_send = consume
        self.assertEqual(self.cycle()["items"][0]["state"], "ACKNOWLEDGED_UNCONFIRMED")
        self.assertEqual(overlapping[0]["attempts"], 0)
        self.restart()
        outreach.enqueue_action(self.database, self.action)
        self.now += dt.timedelta(seconds=901)
        self.cycle()
        self.assertEqual(self.state(), "CONFIRMED_SENT")
        self.restart()
        outreach.enqueue_action(self.database, self.action)
        self.assertEqual(self.cycle()["attempts"], 0)
        self.client._request.assert_called_once()
        for table in ("outreach_actions", "outreach_action_claims", "engagement_events"):
            self.assertEqual(self.database.execute("SELECT count(*) FROM " + table).fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
