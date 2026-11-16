"""Owner, 2026-11-15: a note the user rejected on Approvals is never checked again.

Seen on the owner's own records: 40 rejected, never-sent notes were checked every hour (the check's result
lasts an hour) until each check's 7 tries were gone, and each then showed on Failures. Driven through the
real promotion round (test_invite_path's fixture)."""
import json
import unittest
from unittest.mock import patch

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME for every test)
import approvals
import orchestrator
import outreach_agent
import test_approvals_e6t2
from test_approvals_e6t2 import shown
import test_invite_path as invite_path

CHECKS = "SELECT * FROM workflow_tasks WHERE worker='outreach-draft-inspection' ORDER BY rowid"


class RejectedDraftIsNotCheckedAgain(test_approvals_e6t2.ResearchFixture):
    redraft = test_approvals_e6t2.SecondSlotAfterRejection.redraft

    def checks(self):
        return self.database.execute(CHECKS).fetchall()

    def round_without_a_check(self):
        """One promotion round in which running the check at all is an error."""
        with patch.object(orchestrator, "HeyReach", return_value=self.client), \
                patch.object(outreach_agent.Adapter, "inspect", side_effect=AssertionError("checked a rejected note")), \
                patch("outreach_campaign.prepare_candidates", side_effect=AssertionError("No HeyReach campaign")):
            return orchestrator.draft_promotion_cycle(self.database, self.config, port=self.port)

    def rejected(self):
        row, unused = self.held()
        approvals.reject(self.database, row["action_id"], shown_payload_hash=shown(self.database, row["action_id"]))
        return row

    def test_an_expired_check_of_a_rejected_note_is_closed_not_run_again(self):
        self.rejected()
        (check,) = self.checks()
        self.assertEqual(check["status"], "COMPLETE")
        result = json.loads(check["result"])
        result["proof"]["checked_at"] = "2020-01-01T00:00:00+00:00"   # the check's hour has passed
        with self.database:
            self.database.execute("UPDATE workflow_tasks SET result=? WHERE task_id=?",
                                  (json.dumps(result, sort_keys=True), check["task_id"]))
        for unused in range(3):
            done = self.round_without_a_check()
        (check,) = self.checks()
        self.assertEqual((check["status"], check["blocker"], check["next_at"]),
                         ("CANCELLED", outreach_agent.REJECTED_BY_OWNER, None))
        events = [row[0] for row in self.database.execute(
            "SELECT event FROM workflow_task_events WHERE task_id=? ORDER BY id", (check["task_id"],))]
        self.assertEqual((events.count("EVIDENCE_EXPIRED"), events.count("CANCELLED")), (0, 1))
        states = [item["state"] for item in done["history_investigations"]["items"]]
        self.assertEqual(states, ["REJECTED_BY_OWNER"])
        self.assertEqual(self.port.calls.count("send_invitation"), 0)

    def test_a_check_that_already_gave_up_leaves_failures(self):
        self.rejected()
        (check,) = self.checks()
        with self.database:
            self.database.execute("UPDATE workflow_tasks SET status='EXHAUSTED',blocker='DEADLINE_OR_RETRY_LIMIT' WHERE task_id=?",
                                  (check["task_id"],))
        self.round_without_a_check()
        (check,) = self.checks()
        self.assertEqual((check["status"], check["blocker"]), ("CANCELLED", outreach_agent.REJECTED_BY_OWNER))

    def test_a_new_draft_for_the_same_person_is_still_checked_and_sent(self):
        self.rejected()
        approvals.set_mode(self.database, approvals.AUTOMATIC, home=self.home)
        self.assertEqual(len(self.redraft("Principal Software Engineer")["drafts"]), 1)
        self.assertEqual(self.promote()["queued"], 1)
        self.execute()
        self.assertEqual(self.port.calls.count("send_invitation"), 1)
        self.assertEqual(len(self.checks()), 2)   # the new draft got its own check
