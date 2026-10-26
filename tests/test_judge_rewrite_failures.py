"""A judge-rejected note handed back for a rewrite is a normal step, not a failure (owner, 2026-10-25).

Drives the whole lifecycle through outreach_agent.prepare_drafts and reads the result with
failures_page.task_failures: a rewrite that passes closes the old check as replaced and Failures
stays empty; a note the judge rejects every time stops when the check's 7 tries are spent and
Failures names the person.
"""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import datetime as dt
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import failures_page
import outreach_agent
import outreach_promotion
import overview_page
import test_outreach_promotion


ROOT = Path(__file__).resolve().parents[1]


class JudgeRewriteLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_outreach_promotion.PromotionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.history_config()
        self.config = dict(self.fixture.config, timezone="America/Los_Angeles", model="synthetic", codex="NEVER")
        self.database = self.fixture.database
        self.now = dt.datetime.fromisoformat(test_outreach_promotion.NOW)
        self.rewrites = 0
        for target in ("socket.socket.connect", "socket.getaddrinfo", "subprocess.Popen"):
            boundary = patch(target, side_effect=AssertionError("External execution forbidden"))
            boundary.start()
            self.addCleanup(boundary.stop)

    def compose(self, root, database, config, row, execute=None, now=None, *, repair=None):
        """The writer's repair turn: a new, shorter version of the same note."""
        assert repair and repair[0]["agent_decision"] == "BLOCKED"
        self.rewrites += 1
        draft = json.loads(row["payload"])
        scaffold = outreach_promotion._scaffold(database, row, draft) if "composition" in draft else draft
        payload = outreach_promotion.superseding_payload(
            scaffold, invitation="Rewritten note %d" % self.rewrites, first_message=None,
            composition={"version": "synthetic", "evidence": "synthetic:composition", "resume_text_sha256": "t" * 64})
        with database:
            created = outreach_promotion.insert_version(database, row["identity"], payload, now)
        return {"state": "COMPOSED", "draft_id": created["draft_id"], "version": created["version"]}

    def proof(self, decision, now):
        proof = self.fixture.history_proof(now.isoformat())
        if decision == "BLOCKED":
            proof.update(agent_decision="BLOCKED", reason="The invitation note is over 300 characters",
                         next_action="Hand the invitation note back to its writer to shorten it")
        return proof

    def run_cycle(self, decide, now):
        def inspect(adapter, candidate, config):
            return self.proof(decide(candidate), now)
        with patch.object(outreach_agent.Adapter, "inspect", inspect), \
                patch.object(outreach_agent, "compose_draft", self.compose), \
                patch.object(outreach_promotion, "resume_context", return_value={"source_sha256": "a" * 64, "text_sha256": "t" * 64}), \
                patch("outreach_provider._now", return_value=now.isoformat()):
            return outreach_agent.prepare_drafts(ROOT, self.database, self.config, Mock(spec=["call", "_request"]),
                                                  Mock(side_effect=AssertionError("No AI")), now)

    def tasks(self):
        return [dict(row) for row in self.database.execute(
            "SELECT * FROM workflow_tasks WHERE worker='outreach-draft-inspection' ORDER BY rowid")]

    def failures(self, now):
        self.database.commit()
        return failures_page.task_failures(overview_page.Reader(self.database), now, lambda moment: moment.isoformat())

    def test_a_rewrite_that_passes_closes_the_old_check_and_nothing_is_listed(self):
        result = self.run_cycle(lambda candidate: "READY" if candidate["invitation"].startswith("Rewritten") else "BLOCKED",
                                self.now)
        old, new = self.tasks()
        self.assertEqual(result["items"][0]["repair"]["state"], "RECOMPOSED")
        self.assertEqual((old["status"], old["blocker"]), ("CANCELLED", "REPLACED_BY_REWRITE"))
        self.assertIsNone(old["next_at"])
        self.assertEqual(new["status"], "COMPLETE")
        closed = json.loads(self.database.execute(
            "SELECT detail FROM workflow_task_events WHERE task_id=? AND event='CANCELLED'", (old["task_id"],)).fetchone()[0])
        self.assertEqual(closed["replacement_draft_id"], new["parent_run_id"].split(":", 1)[1])
        self.assertEqual(closed["chain_tries"], 1)
        self.assertEqual(new["max_attempts"], 6)   # the rewrite continues the same check's 7 tries
        (passed,) = list(result["contexts"].values()) + result["campaign_candidates"]
        self.assertEqual(passed["draft"]["invitation"], "Rewritten note 1")   # only the rewrite goes on
        # Long after, the old check is still closed: it is never re-inspected, never gives up.
        later = self.now + dt.timedelta(days=3)
        self.run_cycle(lambda candidate: "READY", later)
        self.assertEqual(self.tasks()[0]["status"], "CANCELLED")
        self.assertEqual(self.failures(later), [])

    def test_a_rejection_waiting_for_its_rewrite_is_not_listed(self):
        self.rewrites = 0
        with patch.object(outreach_agent, "_draft_repair_turn", return_value=None):
            self.run_cycle(lambda candidate: "BLOCKED", self.now)
        (task,) = self.tasks()
        self.assertEqual((task["status"], task["blocker"]), ("RETRY_WAIT", "AGENT_DECISION_NOT_READY"))
        self.assertEqual(self.failures(self.now + dt.timedelta(minutes=1)), [])

    def test_rewrites_rejected_until_the_tries_run_out_are_listed_by_name(self):
        moment, inspections = self.now, 0
        for unused in range(12):
            result = self.run_cycle(lambda candidate: "BLOCKED", moment)
            inspections += sum(1 for item in result["items"] if item.get("attempts") is not None and item.get("state") != "CANCELLED")
            if any(task["status"] == "EXHAUSTED" for task in self.tasks()):
                break
            if not any(task["status"] == "RETRY_WAIT" for task in self.tasks()):
                break
            self.assertEqual(self.failures(moment), [], "a rewrite in progress is not a failure")
            moment += dt.timedelta(days=1)
        tasks = self.tasks()
        self.assertEqual([task["status"] for task in tasks][-1], "EXHAUSTED")
        self.assertTrue(all(task["status"] == "CANCELLED" for task in tasks[:-1]))
        spent = sum(task["attempts"] for task in tasks)
        self.assertEqual(spent, 7, "the rewrites share the check's 7 tries")
        self.assertEqual(self.rewrites, len(tasks) - 1)
        (item,) = self.failures(moment)
        self.assertEqual(item["text"], "Sidedoor tried a few times, but its check kept rejecting the note to "
                                       "Mock Person (Mock Company), so it stopped trying. Nothing was sent.")
        # Owner, 2026-10-25: no number of tries on this item.
        self.assertEqual(item["count"], 1)
        self.assertTrue(all(reason["count"] == 1 for reason in item["reasons"]))
        self.assertEqual(item["todo"], "Nothing you need to do. Sidedoor moves on to its other work.")
        self.assertEqual(item["reasons"][0]["code"], "AGENT_DECISION_NOT_READY")
        self.assertEqual(item["reasons"][0]["words"], "Sidedoor's check rejected the note before it was sent.")
        self.assertIsNone(item["reasons"][0]["where"])   # the headline already names the person
        self.assertIsNone(self.database.execute("SELECT 1 FROM outreach_actions").fetchone())

    def test_a_check_left_waiting_before_this_fix_is_closed_once_a_rewrite_exists(self):
        """The live 2026-10-25 shape: rejected, rewritten, the rewrite sent, the old check RETRY_WAIT."""
        with patch.object(outreach_agent, "close_rewritten_check", return_value=False):
            self.run_cycle(lambda candidate: "READY" if candidate["invitation"].startswith("Rewritten") else "BLOCKED",
                           self.now)
        old, new = self.tasks()
        self.assertEqual((old["status"], new["status"]), ("RETRY_WAIT", "COMPLETE"))
        self.assertEqual(self.failures(self.now), [])
        result = self.run_cycle(lambda candidate: "READY", self.now + dt.timedelta(hours=1))
        self.assertEqual(result["replaced_checks"], [old["task_id"]])
        self.assertEqual(self.tasks()[0]["status"], "CANCELLED")


class JudgeWordsTests(unittest.TestCase):
    def test_the_code_has_plain_words(self):
        self.assertEqual(failures_page.cause_words("AGENT_DECISION_NOT_READY"),
                         "Sidedoor's check rejected the note before it was sent.")


if __name__ == "__main__":
    unittest.main()
