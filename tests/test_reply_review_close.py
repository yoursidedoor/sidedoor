import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import json
import unittest

import referrals
import test_reply_worker_review as fixtures
from backlog_fixture import setUpModule, tearDownModule  # noqa: F401 (fixture messages predate the reply cutoff)


class TheModelCanFinishAMessage(unittest.TestCase):
    """Owner, 2026-09-24: let the AI say a message needs no further look.

    Every decision had to carry a review_at, and only a single-role AGREEMENT or DECLINE could
    finish a message, so everything else was booked another review round. Of 20 inbound messages
    none had finished. A null review_at now finishes the message; a date still books a round.
    """

    def run_decision(self, label, review_at):
        case = fixtures.ReplyWorkerCycleReview("test_success_persists_audit_once_without_changing_source_or_business_tables")
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.case = case
        task_id = case.queue()

        def execute(config, prompt, schema, timeout):
            request = json.loads(prompt.partition("\n\nUNTRUSTED_SOURCE_JSON:\n")[2])
            output = fixtures.inference(request["source_sha256"])
            output.update(label=label, review_at=review_at)
            return json.dumps(output)

        case.cycle(effect=execute)
        task = case.task(task_id)
        key = json.loads(task["inputs"])[0]["source_key"]
        source = case.database.execute("SELECT status,result FROM pipeline_routing_sources WHERE source_key=?", (key,)).fetchone()
        rounds = case.database.execute("""SELECT COUNT(*) FROM workflow_tasks WHERE worker='pipeline_routing_review'
            AND json_extract(inputs,'$[0].review_round')=1""").fetchone()[0]
        return task, source, rounds

    def test_a_blank_review_date_finishes_the_message(self):
        for label in ("OTHER", "QUESTION"):
            with self.subTest(label=label):
                task, source, rounds = self.run_decision(label, None)
                self.assertEqual("COMPLETE", task["status"])
                self.assertEqual("ROUTED", source["status"])
                self.assertEqual("model", json.loads(source["result"])["review_closed_by"])
                self.assertEqual(0, rounds)

    def test_a_dated_review_still_books_the_next_round(self):
        task, source, rounds = self.run_decision("OTHER", "2026-09-18T20:00:00+00:00")
        self.assertEqual("REVIEW", source["status"])
        self.assertEqual(1, rounds)

    def test_an_agreement_or_decline_with_no_job_and_no_date_finishes(self):
        # Owner, 2026-09-24: agreements and declines may close with a blank date, and need no job.
        for label, state in (("AGREEMENT", "AGREED"), ("DECLINE", "DECLINED")):
            with self.subTest(label=label):
                task, source, rounds = self.run_decision(label, None)
                self.assertEqual("COMPLETE", task["status"])
                self.assertEqual("ROUTED", source["status"])
                result = json.loads(source["result"])
                self.assertEqual(state, result["referral_state"])
                self.assertNotIn("job_key", result)
                # Recorded on the person (owner, 2026-09-25), not against a job.
                self.assertEqual(state, referrals.current(self.case.database, result["contact_id"])["state"])
                self.assertEqual(0, rounds)


if __name__ == "__main__":
    unittest.main()
