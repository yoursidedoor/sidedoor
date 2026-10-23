import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import copy
import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import engagement
import orchestrator
import outreach
import outreach_agent
import outreach_promotion as promotion
import research_workflow as research


NOW = "2026-09-17T16:00:00+00:00"
PROFILE = "https://www.linkedin.com/in/mock-promotion"


def evidence(field, value):
    return {"field": field, "value": value, "source": "https://example.test/jobs/123",
            "observed_at": NOW, "verified": True, "excerpt": "MOCK fixture only", "kind": "official"}


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "fixture.sqlite"
        self.database = sqlite3.connect(self.path)
        self.addCleanup(lambda: self.database.close())
        self.database.row_factory = sqlite3.Row
        research.initialize(self.database)
        outreach.initialize_actions(self.database)
        research.seed_history(self.database, {"companies": []}, history_complete=True)
        research.register_claims(self.database, [{"text": "Built reliable APIs", "tags": ["backend"]}],
                                 designated_source_text="Built reliable APIs", source_sha256="a" * 64,
                                 designated_sha256="a" * 64)
        research.plan_research(self.database, "plan", ["Mock Company"], now=NOW)
        self.config = {}
        self.draft = self.make_draft()

    def make_draft(self, need="reliable APIs", profile=PROFILE, recipient="Mock Person"):
        facts = {"team_need": need, "specialization": "backend"}
        person = {"recipient": recipient, "profile": profile, "employer_matches_target": True, "evidence": [
            evidence("identity", profile), evidence("employment", "Mock Company"),
            evidence("team", "API team"), evidence("ability_to_help", "Runs team hiring"),
            evidence("contact_type", "hiring_manager")]}
        role = {"company": "Mock Company", "role": "Staff Software Engineer",
                "url": "https://example.test/jobs/123", "requisition_id": "123", "rationale": "MOCK only",
                "people": [person], "evidence": [evidence(field, value) for field, value in facts.items()]}
        output = research.process_research(self.database, {"research_status": "COMPLETE", "summary": "Mock",
                                           "blockers": [], "opportunities": [role], "drafts": []}, plan_id="plan", now=NOW)
        return output["drafts"][0]

    def accept(self, profile=PROFILE, hours=13):
        occurred = research.timestamp(NOW) - dt.timedelta(hours=hours)
        engagement.record_event(self.database, {"source": "mock", "source_id": profile,
            "profile": profile, "kind": "accepted", "occurred_at": occurred.isoformat(),
            "observed_at": NOW, "evidence": "mock independently verified acceptance", "verified": True, "raw": {}})

    def run_cycle(self, now=NOW):
        with patch.object(outreach.HeyReachActionAdapter, "execute_claimed", side_effect=AssertionError("No execution")), \
                patch("subprocess.run", side_effect=AssertionError("No external work")):
            return promotion.cycle(self.database, self.config, now)

    def action(self):
        return self.database.execute("SELECT * FROM outreach_actions").fetchone()

    def history_config(self):
        self.config = {"sender_id": 900001, "sender_profile": "https://www.linkedin.com/in/mock-sender",
                       "outreach_actions_enabled": True, "outreach_mode": "guarded",
                       "outreach_allowed_purposes": ["INITIAL", "FIRST_MESSAGE"],
                       "outreach_history_max_age_seconds": 3600,
                       "outreach_agent": {"enabled": True, "max_items": 2},
                       "outreach_promotion_contexts": {}}
        self.database.commit()

    def history_proof(self, now=NOW):
        return {"identity": outreach.profile_key(PROFILE), "sender_id": 900001,
                "checked_at": now, "evidence": "mock:authenticated-history",
                "sender_ready": True, "entitlement_verified": True,
                "direct_messages_complete": True, "sent_invitations_complete": True,
                "pending_invitation": False, "cooldown": False, "prior_outreach": False,
                "has_reply": False, "negative_reply": False, "opt_out": False,
                "conversation_id": "mock-thread", "company_contact_count": 0,
                "company_count_evidence": "mock:company-history", "followups_sent": 0,
                "initial_message_sent": False, "agent_decision": "READY",
                "provider_snapshot_complete": True, "next_action": "Use the shared action guard"}

    def prepare_history(self, inspector, now=NOW):
        client = Mock(spec=["call", "_request"])
        with patch.object(outreach_agent.Adapter, "inspect", inspector):
            result = outreach_agent.prepare_drafts(Path(__file__).resolve().parents[1],
                self.database, self.config, client, now=now)
        client.call.assert_not_called()
        client._request.assert_not_called()
        return result

    def history_task(self):
        return dict(self.database.execute(
            "SELECT * FROM workflow_tasks WHERE worker='outreach-draft-inspection'").fetchone())

    def history_events(self):
        return [dict(row) for row in self.database.execute(
            "SELECT * FROM workflow_task_events WHERE task_id=? ORDER BY id", (self.history_task()["task_id"],))]

    def test_completed_history_is_reused_without_spending_an_attempt(self):
        self.history_config()
        inspector = Mock(return_value=self.history_proof())
        first = self.prepare_history(inspector)
        original_task, original_events = self.history_task(), self.history_events()
        repeated = self.prepare_history(inspector, "2026-09-17T16:30:00+00:00")
        inspector.assert_called_once()
        self.assertEqual(first["contexts"], repeated["contexts"])
        self.assertEqual(repeated["items"][0]["state"], "OBSERVED")
        self.assertEqual(original_task, self.history_task())
        self.assertEqual(original_events, self.history_events())
        self.assertIsNone(self.action())

    def test_expired_completed_history_refreshes_after_restart_with_original_budget(self):
        self.history_config()
        inspector = Mock(return_value=self.history_proof())
        first = self.prepare_history(inspector)
        original_task, original_events = self.history_task(), self.history_events()
        self.database.close()
        self.database = sqlite3.connect(self.path)
        self.database.row_factory = sqlite3.Row
        later = "2026-09-17T17:00:01+00:00"
        inspector.return_value = self.history_proof(later)
        refreshed = self.prepare_history(inspector, later)
        current = self.history_task()
        self.assertEqual(current["status"], "COMPLETE")
        self.assertEqual(current["attempts"], 2)
        for field in ("task_id", "deadline", "max_attempts", "inputs", "parent_run_id"):
            self.assertEqual(current[field], original_task[field])
        self.assertEqual(self.history_events()[:len(original_events)], original_events)
        expired = [event for event in self.history_events() if event["event"] == "EVIDENCE_EXPIRED"]
        self.assertEqual(len(expired), 1)
        self.assertEqual(json.loads(expired[0]["detail"])["previous_result"], first["contexts"][PROFILE])
        self.assertEqual(refreshed["contexts"][PROFILE]["proof"]["checked_at"], later)
        self.assertEqual(inspector.call_count, 2)
        self.assertIsNone(self.action())

    def test_repeated_proof_expiry_retries_without_resetting_the_deadline(self):
        self.history_config()
        inspector = Mock()
        for attempt in range(1, 4):
            current_time = (research.timestamp(NOW) + dt.timedelta(seconds=3601 * (attempt - 1))).isoformat()
            inspector.return_value = self.history_proof(current_time)
            result = self.prepare_history(inspector, current_time)
            self.assertIn(PROFILE, result["contexts"])
            self.assertEqual(self.history_task()["attempts"], attempt)
        before = self.history_task()
        previous_events = self.history_events()
        expired_at = (research.timestamp(NOW) + dt.timedelta(seconds=10803)).isoformat()
        exhausted = self.prepare_history(inspector, expired_at)
        self.assertEqual(exhausted["contexts"], {})
        # 7 attempts on a growing backoff, not 3 then a grave (owner, 2026-09-23): at three
        # attempts a transient failure made the task terminal with no next date, which is how 6
        # of 7 outreach tasks died. The case still proves the deadline, result and prior events
        # are untouched by a retry.
        self.assertEqual(exhausted["items"][0]["state"], "RETRY_WAIT")
        self.assertEqual(self.history_task()["attempts"], 4)
        self.assertEqual(self.history_task()["deadline"], before["deadline"])
        self.assertEqual(self.history_task()["result"], before["result"])
        self.assertEqual(self.history_events()[:len(previous_events)], previous_events)
        # A fourth attempt happens now: seven, on a growing backoff, instead of three then a
        # grave (owner, 2026-09-23).
        self.assertEqual(inspector.call_count, 4)
        event_count = len(self.history_events())
        self.assertEqual(self.prepare_history(inspector, expired_at)["contexts"], {})
        self.assertEqual(len(self.history_events()), event_count)
        self.assertEqual(inspector.call_count, 4)

    def test_failed_refresh_never_reuses_expired_completed_context(self):
        self.history_config()
        inspector = Mock(return_value=self.history_proof())
        first = self.prepare_history(inspector)
        deadline = self.history_task()["deadline"]
        inspector.side_effect = TimeoutError("Synthetic read timeout")
        result = self.prepare_history(inspector, "2026-09-17T17:00:01+00:00")
        self.assertEqual(result["contexts"], {})
        self.assertEqual(self.history_task()["status"], "RETRY_WAIT")
        self.assertEqual(self.history_task()["attempts"], 2)
        self.assertEqual(self.history_task()["deadline"], deadline)
        self.assertEqual(json.loads(self.history_task()["result"])["proof"], {})
        expired = next(event for event in self.history_events() if event["event"] == "EVIDENCE_EXPIRED")
        self.assertEqual(json.loads(expired["detail"])["previous_result"], first["contexts"][PROFILE])
        self.assertIsNone(self.action())

    def test_orchestrator_promotion_preserves_queue_without_persisting_temporary_context(self):
        self.history_config()
        original_config = copy.deepcopy(self.config)
        client = Mock(spec=["call", "_request"])
        inspector = Mock(return_value=self.history_proof())
        prepare = outreach_agent.prepare_drafts

        def prepare_at_fixture_time(root, database, config, provider_client, **options):
            return prepare(root, database, config, provider_client, now=NOW, **options)

        with patch.object(orchestrator, "HeyReach", return_value=client), \
                patch.object(orchestrator, "utcnow", return_value=NOW), \
                patch.object(outreach_agent, "prepare_drafts", side_effect=prepare_at_fixture_time), \
                patch.object(outreach_agent.Adapter, "inspect", inspector):
            first = orchestrator.draft_promotion_cycle(self.database, self.config)
            self.assertEqual(first["queued"], 1)
            original_action = tuple(self.action())
            self.database.close()
            self.database = sqlite3.connect(self.path)
            self.database.row_factory = sqlite3.Row
            repeated = orchestrator.draft_promotion_cycle(self.database, self.config)
        self.assertEqual(repeated["queued"], 0)
        self.assertEqual(repeated["refreshed"], 0)
        self.assertEqual(len(repeated["items"]), 1)
        self.assertEqual(repeated["items"][0]["state"], "ALREADY_QUEUED")
        self.assertEqual(repeated["mappings"], first["mappings"])
        self.assertEqual(tuple(self.action()), original_action)
        self.assertEqual(self.config, original_config)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM outreach_action_claims").fetchone()[0], 0)
        inspector.assert_called_once()
        client.call.assert_not_called()
        client._request.assert_not_called()

    def test_preserved_claimed_queue_reports_item_without_resetting_claim_or_state(self):
        self.accept()
        first = self.run_cycle()
        row = self.action()
        with self.database:
            self.database.execute("INSERT INTO outreach_action_claims VALUES (?,?,?,?,?,?,?)",
                (row["action_id"], row["payload"], row["payload_hash"], row["identity"], 123, "{}", NOW))
            self.database.execute("UPDATE outreach_actions SET state='UNCERTAIN_NO_RETRY',claimed_at=?,result=?",
                                  (NOW, json.dumps({"provider_request": {"started_at": NOW}})))
        original_action = tuple(self.action())
        original_claim = tuple(self.database.execute("SELECT * FROM outreach_action_claims").fetchone())
        repeated = self.run_cycle()
        self.assertEqual(len(repeated["items"]), 1)
        self.assertEqual(repeated["items"][0]["state"], "ALREADY_QUEUED")
        self.assertEqual(repeated["mappings"], first["mappings"])
        self.assertEqual(repeated["queued"], 0)
        self.assertEqual(repeated["refreshed"], 0)
        self.assertEqual(tuple(self.action()), original_action)
        self.assertEqual(tuple(self.database.execute("SELECT * FROM outreach_action_claims").fetchone()), original_claim)

    def test_current_verified_first_message_queues_once_without_application(self):
        self.accept()
        original = self.database.execute("SELECT payload FROM rw_drafts").fetchone()[0]
        result = self.run_cycle()
        self.assertEqual(result["queued"], 1)
        self.assertEqual(len(result["mappings"]), 1)
        action = json.loads(self.action()["payload"])
        self.assertEqual(action["purpose"], "FIRST_MESSAGE")
        self.assertEqual(action["message"], self.draft["first_message"])
        self.assertEqual(action["verification"]["verified_at"], NOW)
        self.assertNotIn("sender_id", action)
        self.assertNotIn("receipt", action)
        self.assertNotIn("direct_messages_complete", action["verification"])
        self.assertEqual(self.database.execute("SELECT payload FROM rw_drafts").fetchone()[0], original)
        repeat = self.run_cycle()
        self.assertEqual(repeat["queued"], 0)
        self.assertEqual(repeat["mappings"], result["mappings"])
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM outreach_actions").fetchone()[0], 1)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM outreach_action_claims").fetchone()[0], 0)
        gates = outreach.action_gates(self.database, {}, action, {}, research.timestamp(NOW))
        self.assertIn("DIRECT_HISTORY_MISSING_OR_STALE", gates)
        self.assertIn("ENTITLEMENT_VERIFIED_MISSING", gates)
        self.assertIn("VERIFIED_CONVERSATION_MISSING", gates)

    def test_unknown_purpose_and_unverified_claim_held(self):
        self.assertIn("OUTREACH_PURPOSE_UNPROVEN", self.run_cycle()["items"][0]["gates"])
        self.accept()
        with self.database:
            self.database.execute("DELETE FROM rw_claims")
        self.assertIn("CANDIDATE_CLAIM_UNKNOWN", self.run_cycle()["items"][0]["gates"])
        self.assertIsNone(self.action())

    def test_latest_version_only_and_pending_refresh(self):
        self.accept()
        first = self.run_cycle()["mappings"][0]
        newest = self.make_draft("durable services")
        result = self.run_cycle()
        self.assertEqual(result["refreshed"], 1)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["mappings"][0]["version"], 2)
        self.assertEqual(result["mappings"][0]["action_id"], first["action_id"])
        self.assertNotEqual(result["mappings"][0]["draft_id"], first["draft_id"])
        self.assertEqual(json.loads(self.action()["payload"])["message"], newest["first_message"])
        self.assertEqual(self.run_cycle()["refreshed"], 0)

    def test_claimed_and_uncertain_actions_are_immutable(self):
        self.accept()
        self.run_cycle()
        row = self.action()
        with self.database:
            self.database.execute("INSERT INTO outreach_action_claims VALUES (?,?,?,?,?,?,?)",
                (row["action_id"], row["payload"], row["payload_hash"], row["identity"], 123, "{}", NOW))
            self.database.execute("UPDATE outreach_actions SET state='UNCERTAIN_NO_RETRY',claimed_at=?,result='mock unknown'", (NOW,))
        original = tuple(self.action())
        claim = tuple(self.database.execute("SELECT * FROM outreach_action_claims").fetchone())
        self.make_draft("changed need")
        result = self.run_cycle()
        self.assertEqual(result["held"], 1)
        self.assertEqual(tuple(self.action()), original)
        self.assertEqual(tuple(self.database.execute("SELECT * FROM outreach_action_claims").fetchone()), claim)

    def test_immutable_claim_blocks_even_pending_state(self):
        self.accept()
        self.run_cycle()
        row = self.action()
        with self.database:
            self.database.execute("INSERT INTO outreach_action_claims VALUES (?,?,?,?,?,?,?)",
                (row["action_id"], row["payload"], row["payload_hash"], row["identity"], 123, "{}", NOW))
        original = tuple(self.action())
        self.make_draft("changed need")
        self.assertEqual(self.run_cycle()["held"], 1)
        self.assertEqual(tuple(self.action()), original)

    def test_restart_preserves_action_and_mapping(self):
        self.accept()
        result = self.run_cycle()
        self.database.close()
        self.database = sqlite3.connect(self.path)
        self.assertIsNone(self.database.row_factory)
        repeated = self.run_cycle()
        self.assertIsNone(self.database.row_factory)
        self.assertEqual(repeated["mappings"], result["mappings"])
        self.assertEqual(repeated["queued"], 0)

    def test_rerun_and_restart_leave_the_queued_action_unchanged(self):
        self.accept()
        with research.transaction(self.database):
            first = self.run_cycle()["mappings"][0]
            self.assertEqual(first["strategy_draft_id"], self.draft["draft_id"])
        original = tuple(self.action())
        self.assertEqual(self.run_cycle()["mappings"][0], first)
        self.assertEqual(tuple(self.action()), original)
        self.database.close()
        self.database = sqlite3.connect(self.path)
        self.database.row_factory = sqlite3.Row
        self.assertEqual(self.run_cycle()["mappings"][0], first)
        self.assertEqual(tuple(self.action()), original)

    def test_claimed_payload_and_state_survive_source_change(self):
        self.accept()
        self.run_cycle()
        row = self.action()
        with self.database:
            self.database.execute("INSERT INTO outreach_action_claims VALUES (?,?,?,?,?,?,?)",
                (row["action_id"], row["payload"], row["payload_hash"], row["identity"], 123, "{}", NOW))
            self.database.execute("UPDATE outreach_actions SET state='REQUEST_STARTED',claimed_at=?", (NOW,))
        original = tuple(self.action())
        self.make_draft("new source")
        self.assertEqual(self.run_cycle()["held"], 1)
        self.assertEqual(tuple(self.action()), original)

    def test_old_draft_schema_promotes_without_strategy_assignment(self):
        self.accept()
        legacy = {key: value for key, value in self.draft.items() if key not in {"draft_id", "strategy_execution", "segment"}}
        content_hash = research.digest({key: value for key, value in legacy.items() if key not in {"plan_id", "created_at", "version"}})
        with self.database:
            self.database.execute("UPDATE rw_drafts SET payload=?,content_hash=?", (json.dumps(legacy), content_hash))
            for row in self.database.execute("SELECT id,processed FROM rw_results").fetchall():
                processed = json.loads(row["processed"])
                processed["drafts"] = [legacy]
                self.database.execute("UPDATE rw_results SET processed=? WHERE id=?", (json.dumps(processed), row["id"]))
        result = self.run_cycle()
        self.assertEqual(result["queued"], 1)
        self.assertIsNone(result["mappings"][0]["strategy_draft_id"])
        self.assertNotIn("strategy_execution", json.loads(self.action()["payload"]))

    def test_outbound_observed_never_infers_purpose(self):
        self.accept()
        engagement.record_event(self.database, {"source": "mock", "source_id": "me-observation",
            "profile": PROFILE, "kind": "outbound_observed", "occurred_at": NOW,
            "observed_at": NOW, "evidence": "Mock ME observation", "raw": {"sender": "ME"}})
        self.assertEqual(self.run_cycle()["items"][0]["gates"], ["PREVIOUS_MESSAGE_OR_REPLY"])
        self.assertIsNone(self.action())

    def test_malformed_ledger_is_held_without_uncaught_exception(self):
        self.accept()
        with self.database:
            self.database.execute("INSERT INTO rw_drafts VALUES (?,?,?,?)",
                                  ("https://www.linkedin.com/in/broken", 1, "bad-hash", "{"))
        result = self.run_cycle()
        self.assertEqual(result["queued"], 0)
        self.assertEqual(result["held"], 2)
        with self.database:
            self.database.execute("DELETE FROM rw_drafts WHERE content_hash='bad-hash'")
        self.assertEqual(self.run_cycle()["queued"], 1)

    def test_alias_source_profile_promotes(self):
        alias = "https://www.linkedin.com/in/source-alias"
        research.register_alias(self.database, "contact", alias, PROFILE, "Mock verified alias")
        self.make_draft(profile=alias)
        self.accept()
        result = self.run_cycle()
        self.assertEqual(result["queued"], 1)
        mapping = result["mappings"][0]
        self.assertEqual(json.loads(self.action()["payload"])["profile"], alias)
        self.assertEqual(self.run_cycle()["mappings"][0], mapping)

    def test_initial_promotes_on_incomplete_coverage_but_not_on_confirmed_contact(self):
        self.history_config()
        proof = self.history_proof()
        self.config["outreach_promotion_contexts"] = {PROFILE: {"purpose": "INITIAL", "proof": proof,
                                                              "conversation_id": "mock-thread"}}
        # Owner, 2026-09-23: "We only block on a solid yes. Unknown is not a block."
        # The CONFIRMED case must be asserted first: once a cycle promotes the action it is
        # queued, and a later cycle reports ALREADY_QUEUED rather than re-evaluating gates.
        proof["prior_outreach"] = True
        # Refused by KNOWN_PRIOR_OUTREACH_OR_STOP, which fires in _local_gates before promotion
        # reaches the INITIAL check at all - the duplicate guard is earlier than this test knew.
        self.assertEqual(self.run_cycle()["items"][0]["gates"], ["KNOWN_PRIOR_OUTREACH_OR_STOP"])
        self.assertIsNone(self.action())
        # An INCOMPLETE read is not a refusal: this is the ordinary LinkedIn case, and it must
        # promote on its own rather than waiting for a completeness that never arrives.
        proof["prior_outreach"] = False
        proof["direct_messages_complete"] = False
        self.assertEqual(self.run_cycle()["queued"], 1)
        self.assertEqual(json.loads(self.action()["payload"])["purpose"], "INITIAL")
        original_action = tuple(self.action())
        proof["direct_messages_complete"] = False
        repeated = self.run_cycle()
        self.assertEqual(repeated["items"][0]["state"], "ALREADY_QUEUED")
        self.assertEqual(tuple(self.action()), original_action)
        # Owner, 2026-09-23: an incomplete coverage read no longer refuses at the dispatcher
        # either. What this case still proves is that a queued action's payload is IMMUTABLE
        # across re-evaluation - that part is unchanged and is the reason the case exists.
        gates = outreach.action_gates(self.database, self.config, json.loads(self.action()["payload"]),
                                      proof, research.timestamp(NOW))
        self.assertNotIn("DIRECT_MESSAGES_COMPLETE_MISSING", gates)
        self.assertNotIn("SENT_INVITATIONS_COMPLETE_MISSING", gates)
        adapter = Mock(spec=outreach.HeyReachActionAdapter)
        adapter.inspect_action.return_value = copy.deepcopy(proof)
        dispatched = outreach.cycle(self.database, self.config, adapter, now=NOW)
        self.assertNotIn("DIRECT_MESSAGES_COMPLETE_MISSING", dispatched["items"][0].get("gates", []))
        self.assertEqual(self.action()["payload"], original_action[3],
                         "a queued action's payload must never be rewritten")

    def test_external_alias_action_is_not_rewritten(self):
        alias = "https://www.linkedin.com/in/existing-alias"
        research.register_alias(self.database, "contact", alias, PROFILE, "Mock verified alias")
        self.accept()
        outreach.enqueue_action(self.database, {"action_id": "external-action", "profile": alias,
            "recipient": "Mock Person", "company": "Mock Company", "message": "Existing independently owned text",
            "purpose": "INITIAL", "channel": "linkedin", "provider": "heyreach"})
        original = tuple(self.action())
        self.assertEqual(self.run_cycle()["items"][0]["gates"], ["EXISTING_OUTREACH_RESERVATION"])
        self.assertEqual(tuple(self.action()), original)

    def test_resume_change_cancels_unclaimed_then_same_identity_resumes(self):
        self.accept()
        initial = self.run_cycle()["mappings"][0]
        with self.database:
            self.database.execute("UPDATE rw_meta SET value=? WHERE key='designated_resume'", ("b" * 64,))
        self.assertEqual(self.run_cycle()["items"][0]["gates"], ["DESIGNATED_RESUME_CHANGED"])
        self.assertEqual(self.action()["state"], "CANCELLED")
        with self.database:
            self.database.execute("UPDATE rw_meta SET value=? WHERE key='designated_resume'", ("a" * 64,))
        resumed = self.run_cycle()
        self.assertEqual(resumed["refreshed"], 1)
        self.assertEqual(resumed["mappings"][0], initial)
        self.assertEqual(self.action()["state"], "PENDING")

    def test_source_corruption_never_promotes_but_age_alone_no_longer_blocks(self):
        """Corruption still refuses; age alone does not (owner, 2026-09-23).

        The owner's rule is that a research fact about a person never expires and that outreach
        is never blocked on the recipient's team or title: "we should not block an outreach on
        the receipients team or Job title. The research subsys has already validated it for us
        no? ... A 'dont know' also shouldnt be evidence to block an out reach." An earlier pass
        lifted the expiry from the five person facts but left it on the team need, so a dated
        observation still died here. What must still refuse is real corruption: a draft whose
        bytes no longer match the hash it was verified under.
        """
        self.accept()
        with self.database:
            changed = dict(self.draft, first_message="Invented result")
            self.database.execute("UPDATE rw_drafts SET payload=?", (json.dumps(changed),))
        self.assertEqual(self.run_cycle()["items"][0]["gates"], ["DRAFT_HASH_MISMATCH"])
        with self.database:
            self.database.execute("UPDATE rw_drafts SET payload=?", (json.dumps(self.draft),))
        gates = self.run_cycle("2026-09-19T16:00:00+00:00")["items"][0]["gates"]
        self.assertNotIn("TEAM_NEED_UNVERIFIED", gates, "A dated team need must not block outreach")
        self.assertNotIn("PERSON_EVIDENCE_UNVERIFIED", gates, "A dated person fact must not block outreach")

    def test_missing_source_opportunity_no_longer_refuses(self):
        """Owner, 2026-09-23: "doing a word for word python comparison here is guaranteed
        failure, no?" Locating the source job is a lookup, not a safety check. Its absence is
        recorded and the note proceeds; the designated-resume and text checks still apply."""
        self.accept()
        with self.database:
            self.database.execute("DELETE FROM rw_results")
        self.assertNotIn("DRAFT_SOURCE_BINDING_UNVERIFIED", self.run_cycle()["items"][0]["gates"])

    def test_unknown_saved_history_holds(self):
        self.accept()
        research.seed_history(self.database, {"companies": []}, history_complete=False)
        self.assertEqual(self.run_cycle()["items"][0]["gates"], ["SAVED_HISTORY_UNKNOWN"])

    def test_alias_acceptance_and_suppression_share_identity(self):
        alias = "https://www.linkedin.com/in/mock-alias"
        research.register_alias(self.database, "contact", alias, PROFILE, "Mock verified alias")
        self.accept(alias)
        self.assertEqual(self.run_cycle()["queued"], 1)
        with self.database:
            self.database.execute("CREATE TABLE suppressions(identity TEXT PRIMARY KEY, reason TEXT)")
            self.database.execute("INSERT INTO suppressions VALUES (?,?)", (outreach.profile_key(alias), "Mock exclusion"))
        # Promotion no longer refuses a suppressed recipient (owner, 2026-09-23: "checked in
        # earlier stage"). The protection that matters is unchanged: outreach.action_gates
        # reads the same suppression row, through the same alias, right before any send.
        self.assertNotIn("SUPPRESSED_CONTACT", self.run_cycle()["items"][0]["gates"])
        action = json.loads(self.action()["payload"])
        self.assertIn("SUPPRESSED_CONTACT",
                      outreach.action_gates(self.database, self.config, action, {}, research.timestamp(NOW)))

    def test_cross_ledger_company_alias_cap(self):
        self.accept()
        research.register_alias(self.database, "company", "Mock Incorporated", "Mock Company", "Mock legal name")
        with self.database:
            self.database.execute("CREATE TABLE drafts(identity TEXT,recipient TEXT,company TEXT,state TEXT,payload TEXT)")
            other = {"company": "Mock Incorporated", "recipient": "Other", "profile": "https://www.linkedin.com/in/other"}
            self.database.execute("INSERT INTO drafts VALUES (?,?,?,?,?)", ("other", "Other", other["company"], "HELD", json.dumps(other)))
        # The cap is applied in outreach.action_gates now, not here (owner, 2026-09-23). What
        # this test exists to protect is the COUNTING: a company alias and a person held in a
        # different ledger must both land in the same total. That is unchanged.
        self.assertNotIn("COMPANY_CONTACT_LIMIT", self.run_cycle()["items"][0]["gates"])
        usage = outreach.contact_usage(self.database, "Mock Incorporated", self.draft, self.config.get("pipeline"))
        # Both people land in one total through the company alias and across two ledgers.
        self.assertEqual(usage["distinct_reserved"], 2)
        # And the cap they exceed is still applied, by outreach.action_gates, before any send.
        self.assertGreater(usage["projected"], usage["limit"])

    def test_historical_identity_at_other_company_blocks(self):
        self.accept()
        with self.database:
            self.database.execute("CREATE TABLE history(identity TEXT, recipient TEXT,company TEXT,profile TEXT,payload TEXT)")
            self.database.execute("INSERT INTO history VALUES (?,?,?,?,?)", (outreach.profile_key(PROFILE), "Mock Person", "Old Employer", PROFILE, "{}"))
        self.assertEqual(self.run_cycle()["items"][0]["gates"], ["HISTORICAL_RECIPIENT"])

    def test_legacy_attempt_and_alias_claim_block(self):
        self.accept()
        outreach.initialize(self.database)
        with self.database:
            self.database.execute("INSERT INTO outreach_attempts VALUES (?,?,'CLAIMED',?,NULL)", (outreach.profile_key(PROFILE), 900, NOW))
        self.assertEqual(self.run_cycle()["items"][0]["gates"], ["LEGACY_OUTREACH_RESERVATION"])

    def test_no_automatic_contact_alias_blocks_even_under_new_display_name(self):
        research.register_alias(self.database, "contact", "https://www.linkedin.com/in/jrivera", PROFILE, "Mock identity")
        self.accept()
        self.assertEqual(self.run_cycle()["items"][0]["gates"], ["EXCLUDED_CONTACT"])

    def test_followups_email_and_unknown_initial_not_inferred(self):
        for purpose in ("FOLLOWUP", "URGENT", "email", "INITIAL"):
            with self.subTest(purpose=purpose):
                self.config = {"outreach_promotion_contexts": {PROFILE: {"purpose": purpose}}}
                self.assertEqual(self.run_cycle()["held"], 1)
                self.assertIsNone(self.action())

    def preparation(self):
        sequence = {"nodeType": "CONNECTION_REQUEST", "payload": {"messages": [self.draft["invitation"]]},
                    "conditionalNode": {"nodeType": "END", "actionDelay": 3, "actionDelayUnit": "HOUR"},
                    "unconditionalNode": {"nodeType": "END", "actionDelay": 3, "actionDelayUnit": "HOUR"}}
        proof = {"identity": outreach.profile_key(PROFILE), "checked_at": NOW, "evidence": "Mock observed list",
                 "single_contact_list_verified": True, "list_id": 100, "sequence": copy.deepcopy(sequence),
                 "sequence_source": "Mock observed sequence"}
        context = {"purpose": "PREPARE_CAMPAIGN", "list_id": 100, "campaign_name": "Mock prepared draft",
                   "sequence": sequence, "proof": proof}
        self.config = {"outreach_promotion_contexts": {PROFILE: context}}
        return context

    def test_preparation_queues_without_provider_creation_or_receipt(self):
        self.preparation()
        result = self.run_cycle()
        self.assertEqual(result["queued"], 1)
        action = json.loads(self.action()["payload"])
        self.assertEqual(action["purpose"], "PREPARE_CAMPAIGN")
        self.assertEqual(action["message"], self.draft["invitation"])
        self.assertNotIn("campaign_id", action)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM outreach_prepared_campaigns").fetchone()[0], 0)

    def test_preparation_rejects_missing_list_proof_and_followup(self):
        context = self.preparation()
        context["proof"]["single_contact_list_verified"] = False
        self.assertEqual(self.run_cycle()["held"], 1)
        context = self.preparation()
        context["sequence"]["conditionalNode"]["nodeType"] = "MESSAGE"
        context["proof"]["sequence"] = copy.deepcopy(context["sequence"])
        self.assertEqual(self.run_cycle()["held"], 1)
        self.assertIsNone(self.action())

    def test_pending_invitation_blocks_preparation(self):
        self.preparation()["proof"]["pending_invitation"] = True
        self.assertEqual(self.run_cycle()["items"][0]["gates"], ["KNOWN_PRIOR_OUTREACH_OR_STOP"])

    def test_caller_transaction_rolls_back_queue_and_restores_factory(self):
        self.accept()
        self.database.execute("BEGIN")
        self.assertEqual(self.run_cycle()["queued"], 1)
        self.assertTrue(self.database.in_transaction)
        self.database.rollback()
        self.assertIsNone(self.action())

    def test_unknown_schema_is_explicitly_held(self):
        with sqlite3.connect(":memory:") as database:
            database.execute("CREATE TABLE rw_drafts(id TEXT,payload TEXT)")
            result = promotion.cycle(database, {}, NOW)
            self.assertEqual(result["blocker"], "RESEARCH_DRAFT_SCHEMA_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
