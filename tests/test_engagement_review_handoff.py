import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import json
import sqlite3
import unittest
from unittest.mock import patch

import engagement
from manual_engagement_review_handoff import CONFIG, NOW, completed_owner, fixture, protected, referral_owner, replacement_owner


class EngagementReviewHandoffTests(unittest.TestCase):
    def database(self, count=1):
        database = fixture(count)
        self.addCleanup(database.close)
        return database

    def test_handoff_retains_downstream_unresolved_state_and_all_protections(self):
        database = self.database(16)
        before = protected(database)
        result = engagement.handoff_reviews(database, CONFIG, NOW)
        self.assertEqual(len(result["items"]), 16)
        self.assertTrue(all(item["state"] == "HANDED_OFF" for item in result["items"]))
        self.assertTrue(all(item["owner_status_at_handoff"] == "RETRY_WAIT" for item in result["items"]))
        self.assertTrue(all(item["classification_resolved"] is False and item["can_send"] is False for item in result["items"]))
        self.assertEqual(protected(database), before)
        self.assertEqual(database.execute("SELECT count(*) FROM workflow_task_events WHERE event='ENGAGEMENT_REVIEW_HANDED_OFF'").fetchone()[0], 16)
        restarted = sqlite3.connect(":memory:")
        restarted.row_factory = sqlite3.Row
        self.addCleanup(restarted.close)
        database.backup(restarted)
        self.assertEqual(engagement.handoff_reviews(restarted, CONFIG, NOW)["items"], [])
        self.assertEqual(protected(restarted), before)

    def test_unproven_or_claimed_ownership_is_never_retired(self):
        mutations = [
            "DELETE FROM workflow_tasks WHERE worker='pipeline_routing_review'",
            "UPDATE workflow_tasks SET parent_run_id='wrong-source' WHERE worker='pipeline_routing_review'",
            "UPDATE workflow_tasks SET inputs=json_set(inputs,'$[0].content_sha256','wrong-hash') WHERE worker='pipeline_routing_review'",
            "UPDATE workflow_tasks SET inputs=json_set(inputs,'$[0].source','wrong-message') WHERE worker='pipeline_routing_review'",
            "UPDATE workflow_tasks SET effect='WRITE' WHERE worker='pipeline_routing_review'",
            "UPDATE workflow_tasks SET status='CANCELLED' WHERE worker='pipeline_routing_review'",
            "UPDATE workflow_tasks SET status='COMPLETE' WHERE worker='pipeline_routing_review'",
            "UPDATE workflow_tasks SET inputs=json_set(inputs,'$[0].identity','wrong-person') WHERE worker='engagement_review'",
            "UPDATE engagement_events SET raw='{}'",
            "INSERT INTO pipeline_routing_conflicts SELECT 'conflict',source_key,first_seen_at,'{}' FROM pipeline_routing_sources",
            "UPDATE workflow_tasks SET status='RUNNING',token_hash='claim',lease_until='2026-09-18T07:00:00+00:00' WHERE worker='engagement_review'",
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                database = self.database()
                database.execute(mutation)
                before = list(database.iterdump())
                result = engagement.handoff_reviews(database, CONFIG, NOW)
                self.assertFalse(any(item["state"] == "HANDED_OFF" for item in result["items"]))
                self.assertEqual(list(database.iterdump()), before)

    def test_wrong_account_retained_and_failure_does_not_block_independent_item(self):
        database = self.database(2)
        self.assertTrue(all(item["state"] == "RETAINED" for item in
                            engagement.handoff_reviews(database, {"pipeline_routing": {"account_ids": [456]}}, NOW)["items"]))
        database.execute("UPDATE workflow_tasks SET inputs=json_set(inputs,'$[0].source_id','missing') WHERE worker='engagement_review' AND action_id='heyreach:message-0'")
        result = engagement.handoff_reviews(database, CONFIG, NOW)
        self.assertEqual(sorted(item["state"] for item in result["items"]), ["HANDED_OFF", "RETAINED"])

    def test_prior_attempt_result_and_evidence_preserved_with_atomic_audit(self):
        database = self.database()
        database.execute("""UPDATE workflow_tasks SET attempts=2,result='{"old":"failure"}',evidence='[]',
            blocker='OLD_FAILURE' WHERE worker='engagement_review'""")
        database.commit()
        before = list(database.iterdump())
        with patch.object(engagement.workflow_tasks, "record", side_effect=RuntimeError("audit unavailable")):
            with self.assertRaises(RuntimeError):
                engagement.handoff_reviews(database, CONFIG, NOW)
        self.assertEqual(list(database.iterdump()), before)
        engagement.handoff_reviews(database, CONFIG, NOW)
        task = database.execute("SELECT * FROM workflow_tasks WHERE worker='engagement_review'").fetchone()
        self.assertEqual((task["attempts"], task["result"], task["evidence"]), (2, '{"old":"failure"}', '[]'))
        audit = json.loads(database.execute("SELECT detail FROM workflow_task_events WHERE event='ENGAGEMENT_REVIEW_HANDED_OFF'").fetchone()[0])
        self.assertEqual(audit["previous_blocker"], "OLD_FAILURE")

    def test_complete_owner_requires_exact_immutable_result_and_preserves_holds(self):
        database = self.database()
        handoff = completed_owner(database)
        before = protected(database)
        result = engagement.handoff_reviews(database, CONFIG, NOW)["items"][0]
        self.assertEqual(result["state"], "HANDED_OFF")
        self.assertEqual(result["owner_status_at_handoff"], "COMPLETE")
        self.assertEqual(result["reply_output_sha256"], handoff["output_sha256"])
        self.assertEqual(result["classification_holds"], handoff["holds"])
        self.assertFalse(result["classification_resolved"])
        self.assertEqual(protected(database), before)
        self.assertEqual(engagement.handoff_reviews(database, CONFIG, NOW)["items"], [])

    def test_consumed_complete_result_without_review_pointer_is_owned(self):
        database = self.database()
        handoff = completed_owner(database)
        database.execute("UPDATE pipeline_routing_sources SET result=?", (json.dumps({
            "reply_decisions": {handoff["task_id"]: "wrong-output-hash"}, "review_budget_exhausted": True}),))
        self.assertEqual(engagement.handoff_reviews(database, CONFIG, NOW)["items"][0]["state"], "RETAINED")
        database.execute("UPDATE pipeline_routing_sources SET result=?", (json.dumps({
            "reply_decisions": {handoff["task_id"]: handoff["output_sha256"]}, "review_budget_exhausted": True}),))
        self.assertEqual(engagement.handoff_reviews(database, CONFIG, NOW)["items"][0]["state"], "HANDED_OFF")

    def test_completed_label_cannot_substitute_for_source_input_output_audit(self):
        mutations = [
            (None, "DROP TABLE reply_worker_attempts"),
            ("reply_worker_results_no_update", "UPDATE reply_worker_results SET handoff=json_set(handoff,'$.source_key','wrong-source')"),
            ("reply_worker_results_no_update", "UPDATE reply_worker_results SET handoff=json_set(handoff,'$.classification.label','AGREEMENT')"),
            ("reply_worker_inputs_no_update", "UPDATE reply_worker_inputs SET input=input || 'changed'"),
            ("reply_worker_attempts_no_update", "UPDATE reply_worker_attempts SET output='{}'"),
        ]
        for trigger, mutation in mutations:
            with self.subTest(mutation=mutation):
                database = self.database()
                completed_owner(database)
                if trigger:
                    database.execute("DROP TRIGGER " + trigger)
                database.execute(mutation)
                before = list(database.iterdump())
                self.assertEqual(engagement.handoff_reviews(database, CONFIG, NOW)["items"][0]["state"], "RETAINED")
                self.assertEqual(list(database.iterdump()), before)

    def test_cancelled_owner_follows_only_bound_replacement(self):
        database = self.database()
        replacement = replacement_owner(database)
        before = protected(database)
        result = engagement.handoff_reviews(database, CONFIG, NOW)["items"][0]
        self.assertEqual(result["state"], "HANDED_OFF")
        self.assertEqual(result["owner_task_id"], replacement)
        self.assertEqual(len(result["replaced_cancelled_task_ids"]), 1)
        self.assertEqual(protected(database), before)
        database = self.database()
        replacement = replacement_owner(database)
        database.execute("UPDATE workflow_tasks SET inputs=json_set(inputs,'$[0].reply_decision_task_id','unrelated') WHERE task_id=?", (replacement,))
        self.assertEqual(engagement.handoff_reviews(database, CONFIG, NOW)["items"][0]["state"], "RETAINED")

    def test_routed_referral_owns_message_but_is_not_reported_as_business_success(self):
        database = self.database()
        referral_owner(database)
        before = protected(database)
        result = engagement.handoff_reviews(database, CONFIG, NOW)["items"][0]
        self.assertEqual(result["state"], "HANDED_OFF")
        self.assertEqual(result["referral_state_recorded"], "AGREED")
        self.assertIsNone(result["owner_task_id"])
        self.assertFalse(result["classification_resolved"])
        self.assertFalse(result["can_send"])
        self.assertEqual(protected(database), before)

    def test_routed_referral_state_not_recorded_on_the_person_is_insufficient(self):
        mutations = [
            "DELETE FROM referrals",
            "UPDATE pipeline_routing_sources SET result=json_set(result,'$.contact_id','someone-else')",
            "UPDATE pipeline_routing_sources SET result=json_set(result,'$.referral_state','DECLINED')",
            "UPDATE referrals SET history=json_set(history,'$[0].source_key','another-message')",
            "UPDATE pipeline_routing_sources SET result=json_remove(result,'$.referral_state')",
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                database = self.database()
                referral_owner(database)
                database.execute(mutation)
                before = list(database.iterdump())
                self.assertEqual(engagement.handoff_reviews(database, CONFIG, NOW)["items"][0]["state"], "RETAINED")
                self.assertEqual(list(database.iterdump()), before)


if __name__ == "__main__":
    unittest.main()
