"""Independent offline connector review; every database is an in-memory fixture."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import copy
import datetime as dt
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import Mock, patch

import engagement
import orchestrator
import outreach
import outreach_promotion as promotion
import research_workflow as research
import strategy_execution as strategy
import test_research_workflow as fixtures
import test_heyreach_schema as schema_fixtures


NOW = fixtures.NOW
PROFILE = "https://www.linkedin.com/in/review-person"


class ConnectorStrategyReviewTests(unittest.TestCase):
    def setUp(self):
        for target in ("socket.socket", "socket.create_connection", "subprocess.run", "subprocess.Popen",
                       "outreach.HeyReachActionAdapter.execute_claimed"):
            guard = patch(target, side_effect=AssertionError("External work forbidden in review"))
            guard.start()
            self.addCleanup(guard.stop)
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        self.addCleanup(self.database.close)
        research.initialize(self.database)
        outreach.initialize_actions(self.database)
        strategy.initialize(self.database)
        research.seed_history(self.database, {"companies": []}, history_complete=True)
        research.register_claims(self.database, [{"text": "Built reliable APIs", "tags": ["backend"]}],
                                 designated_source_text="Built reliable APIs", source_sha256="a" * 64,
                                 designated_sha256="a" * 64)
        self.database.commit()
        research.plan_research(self.database, "review-plan", ["Example AI"], now=NOW)

    def draft(self, need="reliable APIs", slug="review-person"):
        role = fixtures.role()
        fixtures.change(role, "team_need", need)
        role["people"] = [fixtures.person(name="Review Person " + slug, slug=slug)]
        output = research.process_research(self.database, fixtures.result(role), plan_id="review-plan", now=NOW)
        self.assertTrue(output["drafts"], output["blockers"])
        return output["drafts"][0]

    def accept(self, profile=PROFILE):
        engagement.record_event(self.database, {
            "source": "mock-review", "source_id": profile, "profile": profile, "kind": "accepted",
            "occurred_at": (research.timestamp(NOW) - dt.timedelta(hours=13)).isoformat(),
            "observed_at": NOW, "verified": True, "evidence": "MOCK acceptance", "raw": {}})

    def action_row(self):
        return self.database.execute("SELECT * FROM outreach_actions").fetchone()

    def queue(self, draft, source_changes=None):
        result = promotion.cycle(self.database, {}, NOW)
        self.assertEqual(result["queued"], 1, result)
        action = json.loads(self.action_row()["payload"])
        if source_changes:
            action["source"].update(source_changes)
            payload = json.dumps(action, sort_keys=True)
            self.database.execute("UPDATE outreach_actions SET payload=?,payload_hash=? WHERE action_id=?",
                                  (payload, strategy.text_hash(payload), action["action_id"]))
        return action

    def test_real_baseline_promotes_and_mapping_resolves_actual_draft_id(self):
        draft = self.draft()
        self.accept()
        result = promotion.cycle(self.database, {}, NOW)
        self.assertEqual(result["queued"], 1, result)
        mapping = result["mappings"][0]
        self.assertEqual(mapping["strategy_draft_id"], draft["draft_id"])
        source = self.database.execute(
            "SELECT payload FROM rw_drafts WHERE identity=? AND version=? AND content_hash=?",
            (mapping["identity"], mapping["version"], mapping["content_hash"])).fetchone()
        self.assertEqual(json.loads(source[0])["draft_id"], draft["draft_id"])

    def test_unchanged_promotion_is_idempotent(self):
        draft = self.draft()
        self.accept()
        self.queue(draft)
        original = tuple(self.action_row())
        result = promotion.cycle(self.database, {}, NOW)
        self.assertEqual(result["refreshed"], 0)
        self.assertEqual(result["held"], 0, result)
        self.assertEqual(result["queued"], 0)
        self.assertEqual(tuple(self.action_row()), original)

    def test_verified_profile_alias_promotes_canonical_recipient(self):
        alias = "https://www.linkedin.com/in/review-alias"
        research.register_alias(self.database, "contact", alias, PROFILE, "MOCK verified identity alias")
        draft = self.draft(slug="review-alias")
        self.accept(PROFILE)
        action = self.queue(draft)
        self.assertEqual(research.canonical(self.database, "contact", action["profile"]), PROFILE)
        self.assertEqual(action["source"]["draft_id"], draft["draft_id"])

    def test_unbound_latest_source_refreshes_same_action_without_extra_contact(self):
        draft = self.draft()
        self.accept()
        action = self.queue(draft)
        changed = self.draft(need="durable services")
        result = promotion.cycle(self.database, {}, NOW)
        self.assertEqual(result["refreshed"], 1, result)
        self.assertEqual(result["mappings"][0]["strategy_draft_id"], changed["draft_id"])
        self.assertEqual(result["mappings"][0]["action_id"], action["action_id"])
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM rw_contacts").fetchone()[0], 1)

    def test_promoted_baseline_cancelled_on_changed_resume_without_claim(self):
        draft = self.draft()
        self.accept()
        self.queue(draft)
        original = self.action_row()["payload"]
        self.database.execute("UPDATE rw_meta SET value=? WHERE key='designated_resume'", ("b" * 64,))
        result = promotion.cycle(self.database, {}, NOW)
        self.assertEqual(result["held"], 1)
        self.assertEqual(self.action_row()["state"], "CANCELLED")
        self.assertEqual(self.action_row()["payload"], original)

    def test_changed_source_never_rewrites_claimed_or_uncertain_action(self):
        draft = self.draft()
        self.accept()
        action = self.queue(draft)
        row = self.action_row()
        self.database.execute("INSERT INTO outreach_action_claims VALUES (?,?,?,?,?,?,?)",
                              (action["action_id"], row["payload"], row["payload_hash"], row["identity"], 123, "{}", NOW))
        self.database.execute("UPDATE outreach_actions SET state='UNCERTAIN_NO_RETRY',claimed_at=?,result='mock-unknown'", (NOW,))
        original = tuple(self.action_row())
        claim = tuple(self.database.execute("SELECT * FROM outreach_action_claims").fetchone())
        self.draft(need="changed source")
        self.assertEqual(promotion.cycle(self.database, {}, NOW)["held"], 1)
        self.assertEqual(tuple(self.action_row()), original)
        self.assertEqual(tuple(self.database.execute("SELECT * FROM outreach_action_claims").fetchone()), claim)

    def test_historical_contact_cannot_be_redrafted(self):
        research.seed_history(self.database, {"companies": []}, [{"company": "Old Employer", "recipient": "Review Person",
                              "profile": PROFILE}], history_complete=True)
        role = fixtures.role()
        role["people"] = [fixtures.person(name="Review Person", slug="review-person")]
        output = research.process_research(self.database, fixtures.result(role), plan_id="review-plan", now=NOW)
        self.assertEqual(output["drafts"], [])
        self.assertIn("Example AI / Review Person: HISTORICAL_OR_DUPLICATE_CONTACT", output["action_blockers"])
        self.assertEqual(output["blockers"], [])

    def test_three_lifetime_reservations_block_a_fourth(self):
        for number in range(3):
            self.draft(slug="review-cap-" + str(number))
        role = fixtures.role()
        role["people"] = [fixtures.person(name="Fourth Review", slug="review-cap-fourth")]
        output = research.process_research(self.database, fixtures.result(role), plan_id="review-plan", now=NOW)
        self.assertEqual(output["drafts"], [])
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM rw_contacts").fetchone()[0], 3)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM strategy_drafts").fetchone()[0], 3)

    def test_source_change_invalidation_preserves_historical_outreach(self):
        draft = self.draft()
        self.accept()
        self.queue(draft)
        self.database.execute("UPDATE outreach_actions SET state='CONFIRMED_SENT',claimed_at=?,result='mock-receipt'", (NOW,))
        original = tuple(self.action_row())
        self.database.execute("UPDATE rw_meta SET value=? WHERE key='designated_resume'", ("b" * 64,))
        result = promotion.cycle(self.database, {}, NOW)
        self.assertEqual(result["held"], 1)
        self.assertEqual(tuple(self.action_row()), original)


class ConnectorRuntimeIndependentReviewTests(unittest.TestCase):
    draft = ConnectorStrategyReviewTests.draft
    accept = ConnectorStrategyReviewTests.accept

    def setUp(self):
        ConnectorStrategyReviewTests.setUp(self)
        self.database.executescript("""
            CREATE TABLE history(identity TEXT PRIMARY KEY,recipient TEXT,company TEXT,profile TEXT,evidence TEXT);
            CREATE TABLE suppressions(identity TEXT PRIMARY KEY,reason TEXT);
            CREATE TABLE review_caller_marker(value TEXT);
        """)

    def observed(self, sender="ME"):
        room = schema_fixtures.room([schema_fixtures.message(sender)])
        decoder = schema_fixtures.heyreach_schema.Decoder(424242, schema_fixtures.SENDER,
                                                         clock=lambda: schema_fixtures.NOW)
        return decoder.decode_chatroom(room, decoder.decode_conversation(room))[0]

    def remember(self, event=None):
        event = event or self.observed()
        engagement.record_event(self.database, event)
        self.database.execute("INSERT OR IGNORE INTO history VALUES (?,?,?,?,?)", (
            orchestrator.profile_key(schema_fixtures.PROFILE), "Mock observed person", "Mock employer",
            schema_fixtures.PROFILE, "MOCK history"))
        self.database.commit()

    def config(self):
        return {"sender_id": 424242, "sender_profile": schema_fixtures.SENDER, "campaign_id": 999,
                "engagement_enabled": True, "engagement_max_pages": 1}

    def observe(self, room):
        provider = Mock()
        provider.call.return_value = {"totalCount": 1, "items": [room]}
        return orchestrator.observe(self.database, self.config(), provider)

    def test_saved_provider_lookup_rejects_contradictory_authenticated_account(self):
        event = self.observed()
        event["inbox"]["account_id"] = 777
        self.remember(event)
        self.assertIsNone(orchestrator.known_provider_lead(self.database, schema_fixtures.PROFILE, 424242))

    def test_saved_provider_lookup_rejects_disagreeing_member_identifiers(self):
        event = self.observed()
        event["raw"]["provider_lead_id"] = "different-member"
        self.remember(event)
        self.assertIsNone(orchestrator.known_provider_lead(self.database, schema_fixtures.PROFILE, 424242))

    def test_saved_provider_lookup_requires_one_unambiguous_authenticated_member(self):
        self.remember()
        room = schema_fixtures.room()
        room["correspondentProfile"]["linkedin_id"] = "second-member"
        decoder = schema_fixtures.heyreach_schema.Decoder(424242, schema_fixtures.SENDER,
                                                         clock=lambda: schema_fixtures.NOW)
        event = decoder.decode_chatroom(room, decoder.decode_conversation(room))[0]
        engagement.record_event(self.database, event)
        self.assertIsNone(orchestrator.known_provider_lead(self.database, schema_fixtures.PROFILE, 424242))
        self.assertIsNone(orchestrator.known_provider_lead(self.database, schema_fixtures.PROFILE, 777))

    def test_provider_id_response_rejects_nested_sender_account_mismatch(self):
        self.remember()
        room = schema_fixtures.room()
        room["linkedInAccount"]["id"] = 777
        result = self.observe(room)
        self.assertEqual(result["contacts"][0]["status"], "UNKNOWN", result)

    def test_provider_lookup_and_empty_inbox_never_clear_history(self):
        self.remember()
        original = [tuple(row) for row in self.database.execute("SELECT * FROM history")]
        provider = Mock()
        provider.call.return_value = {"totalCount": 0, "items": []}
        result = orchestrator.observe(self.database, self.config(), provider)
        self.assertEqual(result["contacts"][0]["lookup_basis"], "SAVED_AUTHENTICATED_PROVIDER_ID")
        self.assertIs(result["contacts"][0]["history_complete"], False)
        self.assertEqual([tuple(row) for row in self.database.execute("SELECT * FROM history")], original)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM outreach_action_claims").fetchone()[0], 0)
        self.assertTrue(outreach._new_path_reservations(self.database, schema_fixtures.PROFILE))

    def test_observed_outbound_alias_blocks_both_outreach_paths_without_delivery(self):
        alias = "https://www.linkedin.com/in/observed-alias"
        research.register_alias(self.database, "contact", alias, schema_fixtures.PROFILE, "MOCK identity alias")
        self.remember()
        self.assertEqual(engagement.next_action(self.database, alias, schema_fixtures.NOW)["blocker"],
                         "OBSERVED_OUTBOUND_REQUIRES_PURPOSE_RECONCILIATION")
        self.assertTrue(outreach._new_path_reservations(self.database, alias))
        action = {"action_id": "mock-observed", "profile": alias, "recipient": "Mock observed person",
                  "company": "Mock employer", "purpose": "FIRST_MESSAGE", "message": "Mock message",
                  "channel": "linkedin", "provider": "heyreach", "verification": {}}
        gates = outreach.action_gates(self.database, {}, action, {}, schema_fixtures.NOW)
        self.assertIn("OBSERVED_OUTBOUND_REQUIRES_PURPOSE_RECONCILIATION", gates)
        self.assertIn("DIRECT_HISTORY_MISSING_OR_STALE", gates)

    def test_actual_inbox_replay_preserves_original_profile_and_name_suppressions(self):
        identity = orchestrator.profile_key(schema_fixtures.PROFILE)
        originals = [(identity, "Original user exclusion"), ("name:mock observed person", "Original historical contact")]
        self.database.executemany("INSERT INTO suppressions VALUES (?,?)", originals)
        self.database.commit()
        room = schema_fixtures.room()
        provider = Mock()
        provider.call.side_effect = lambda operation, arguments: (
            {"totalCount": 1, "items": [copy.deepcopy(room)]} if operation == "get_conversations_v2"
            else copy.deepcopy(room))
        with patch.object(orchestrator, "HeyReach", return_value=provider):
            first = orchestrator.engagement_cycle(self.database, self.config())
            replay = orchestrator.engagement_cycle(self.database, self.config())
        self.assertEqual(first["items"][0]["inserted"], 1, first)
        self.assertEqual(replay["items"][0]["inserted"], 0, replay)
        self.assertEqual(sorted(tuple(row) for row in self.database.execute("SELECT * FROM suppressions")), sorted(originals))
        self.assertEqual(engagement.stop_state(self.database, schema_fixtures.PROFILE), "REPLY_REQUIRES_CONTEXT")

    def test_main_promotion_preserves_caller_transaction_and_rollback(self):
        self.draft()
        self.accept()
        self.database.commit()
        self.database.execute("BEGIN")
        self.database.execute("INSERT INTO review_caller_marker VALUES ('uncommitted-caller-work')")
        with patch.object(orchestrator, "utcnow", return_value=NOW):
            result = orchestrator.draft_promotion_cycle(self.database, {})
        self.assertEqual(len(result["mappings"]), 1)
        caller_transaction_preserved = self.database.in_transaction
        self.database.rollback()
        for table in ("review_caller_marker", "outreach_actions"):
            self.assertEqual(self.database.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0, table)
        self.assertTrue(caller_transaction_preserved, "Promotion unexpectedly commits the caller transaction")

    def test_real_promotion_failure_isolated_from_following_service_workers(self):
        self.draft()
        second = self.draft(slug="review-second")
        self.accept()
        self.accept(second["profile"])
        self.database.execute("CREATE TRIGGER review_promotion_failure BEFORE INSERT ON outreach_actions "
                              "WHEN (SELECT COUNT(*) FROM outreach_actions)>0 "
                              "BEGIN SELECT RAISE(ABORT, 'MOCK second promotion failure'); END")
        self.database.commit()
        with patch.object(orchestrator, "utcnow", return_value=NOW), \
                patch.object(orchestrator.decision_transport, "cycle", return_value={"state": "COMPLETE"}) as transport, \
                patch.object(orchestrator, "conversion_cycle", return_value={"state": "COMPLETE"}), \
                patch.object(orchestrator, "reply_classification_cycle", return_value={"state": "COMPLETE"}) as replies, \
                patch.object(orchestrator, "application_cycle", return_value={"state": "MOCK_COMPLETE"}) as applications, \
                patch.object(orchestrator.reporting, "daily_due", return_value=True), \
                patch.object(orchestrator.reporting, "daily", return_value={"state": "NOT_DUE"}) as reporting, \
                patch.object(orchestrator.notifications, "dispatch", return_value={"state": "COMPLETE"}) as notifications, \
                patch.object(orchestrator, "write_json"):
            result = orchestrator.service_cycle(Path("/mock-review-root"), {}, self.database)
        self.assertEqual(result["draft_promotion"]["state"], "FAILED")
        self.assertNotIn("MOCK second promotion failure", str(result))
        # Applications run as their own job (owner, 2026-10-02), never in the hourly cycle.
        self.assertEqual(result["applications"]["state"], orchestrator.APPLICATIONS_JOB_STATE)
        applications.assert_not_called()
        self.assertNotIn("learning", result)
        for worker in (replies, reporting, notifications):
            worker.assert_called_once()
        self.assertEqual(transport.call_count, 2)
        self.assertEqual(self.database.execute("SELECT status FROM workflow_tasks WHERE worker='research-draft-promotion'").fetchone()[0], "BLOCKED")
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM outreach_actions").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
