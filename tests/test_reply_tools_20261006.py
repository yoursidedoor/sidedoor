"""The reply AI's tools (owner, 2026-10-06).

"if the reply agent had tools access to the db, it could have checked to see if an soylent
application existed already. Or if one did exist and the contact gave us an application link, the
reply agent could queue the job so the application agent could pick it up." Queued jobs are
applied to automatically ("it should be applied to automatically").
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import applications
import reply_tools
import reply_worker
import test_reply_worker as fixture
from backlog_fixture import setUpModule, tearDownModule  # noqa: F401
import synthetic_resume  # noqa: F401 (synthetic designated resume; the owner's is not in the repo)


POSTING = "https://job-boards.greenhouse.io/soylentenergy/jobs/9000000006"


class ServerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "state").mkdir()
        database = sqlite3.connect(self.root / "state/orchestrator.sqlite3")
        applications.initialize(database)
        key = applications.ingest(database, {"company": "Soylent Energy", "role": "Senior Software Engineer",
                                             "url": POSTING, "rationale": "fit"}, "2026-09-30T08:00:00+00:00")
        database.execute("UPDATE application_jobs SET state='SUBMITTED' WHERE job_key=?", (key,))
        applications.event(database, key, "SUBMISSION_VERIFIED", {}, "2026-09-30T08:58:44+00:00")
        database.commit()
        database.close()
        self.run_directory = self.root / "run"
        self.run_directory.mkdir()
        reply_tools.prepare(self.run_directory, self.root, "task-1")

    def serve(self, *calls):
        lines = [{"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}},
                 {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}]
        lines += [{"jsonrpc": "2.0", "id": index + 2, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
                  for index, (name, arguments) in enumerate(calls)]
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO("".join(json.dumps(line) + "\n" for line in lines))), patch("sys.stdout", output):
            self.assertEqual(0, reply_tools.serve(self.run_directory))
        return [json.loads(line) for line in output.getvalue().splitlines()]

    def test_find_jobs_answers_whether_the_owner_applied_and_writes_queue_only_an_intent(self):
        # "Soylent" must find "Soylent Energy": an exact-only match found nothing in the 2026-10-06 replay.
        responses = self.serve(("find_jobs", {"company": "Soylent"}),
                               ("queue_job", {"url": "https://jobs.example.com/apply/9", "company": "Soylent Energy",
                                              "role": "Staff Engineer", "reason": "The contact sent this link"}),
                               ("queue_job", {"url": "http://jobs.example.com/apply/9", "company": "Soylent Energy",
                                              "role": "Staff Engineer", "reason": "plain http"}))
        self.assertEqual({tool["name"] for tool in reply_tools.TOOLS},
                         {tool["name"] for tool in responses[1]["result"]["tools"]})
        jobs = json.loads(responses[2]["result"]["content"][0]["text"])["jobs"]
        self.assertEqual([("SUBMITTED", "2026-09-30T08:58:44+00:00")], [(job["state"], job["submitted_at"]) for job in jobs])
        self.assertFalse(responses[3]["result"]["isError"])
        self.assertTrue(responses[4]["result"]["isError"])
        calls = reply_tools.collect(self.run_directory)
        self.assertEqual(["find_jobs", "queue_job", "queue_job"], [call["tool"] for call in calls])
        self.assertEqual("https://jobs.example.com/apply/9", calls[1]["intent"]["url"])
        self.assertNotIn("intent", calls[2])
        # Nothing was written during the run: the database still holds only the one job.
        database = sqlite3.connect(self.root / "state/orchestrator.sqlite3")
        self.assertEqual(1, database.execute("SELECT COUNT(*) FROM application_jobs").fetchone()[0])


class CycleTests(fixture.ReplyWorkerTests):
    """The intents are carried out with the decision, and dropped with a failed run."""

    def queue_from_the_run(self):
        # The model calls queue_job on its first turn (run in this process by reply_tools.Session).
        self.tool_turns = [[("queue_job", {"url": "https://jobs.example.com/apply/9", "company": "Example Co",
                                           "role": "Staff Engineer", "reason": "The contact sent the application link"})]]

    def test_a_queued_job_is_ingested_when_the_decision_is_saved(self):
        self.queue_from_the_run()
        task_id = self.enqueue()
        result = self.run_cycle()
        self.assertEqual("COMPLETE", self.task(task_id)["status"])
        job = self.database.execute("SELECT job_key,state,payload FROM application_jobs WHERE employer='example co'").fetchone()
        self.assertEqual("QUEUED", job["state"])
        self.assertEqual("reply", json.loads(job["payload"])["discovered_by"])
        event = self.database.execute("SELECT detail FROM application_events WHERE job_key=? AND event='DISCOVERED'",
                                      (job["job_key"],)).fetchone()
        self.assertEqual("reply", json.loads(event["detail"])["source"])
        self.assertEqual([{"tool": "queue_job", "job_key": job["job_key"], "url": "https://jobs.example.com/apply/9",
                           "state": "QUEUED"}], result["items"][0]["tool_effects"])

    def test_a_failed_run_queues_nothing(self):
        self.queue_from_the_run()
        self.raw = "not json"
        self.enqueue()
        self.run_cycle()
        self.assertIsNone(self.database.execute("SELECT 1 FROM application_jobs WHERE employer='example co'").fetchone())


class ToolLoopTests(fixture.ReplyWorkerTests):
    """The reply AI's tool loop over the direct call (owner, 2026-10-27): tool call, tool result, answer."""

    def test_a_tool_call_gets_its_result_and_the_next_turn_answers(self):
        self.tool_turns = [[("flag_owner", {"reason": "The contact asked for the owner's phone number"})]]
        task_id = self.enqueue()
        result = self.run_cycle()
        self.assertEqual("COMPLETE", self.task(task_id)["status"])
        first, second = self.processes
        self.assertEqual(first["messages"], second["messages"][:2])
        assistant, tool = second["messages"][2:]
        self.assertEqual("flag_owner", assistant["tool_calls"][0]["function"]["name"])
        self.assertEqual({"role": "tool", "tool_call_id": assistant["tool_calls"][0]["id"],
                          "content": "Recorded. It takes effect when your decision is saved."}, tool)
        self.assertEqual(["flag_owner"], [call["tool"] for call in result["items"][0]["tool_calls"]])
        self.assertEqual("The contact asked for the owner's phone number",
                         self.database.execute("SELECT reason FROM reply_owner_flags").fetchone()[0])

    def test_the_loop_ends_at_its_bound_with_a_last_turn_that_has_no_tools(self):
        self.tool_turns = [[("flag_owner", {"reason": "note %d" % turn})] for turn in range(40)]
        task_id = self.enqueue()
        result = self.run_cycle()
        self.assertEqual(reply_tools.MAX_CALLS + 1, len(self.processes))
        last = self.processes[-1]
        self.assertEqual("none", last["tool_choice"])
        self.assertEqual("reply_decision", last["response_format"]["json_schema"]["name"])
        self.assertTrue(last["response_format"]["json_schema"]["strict"])
        self.assertEqual("COMPLETE", self.task(task_id)["status"])
        calls = result["items"][0]["tool_calls"]
        self.assertEqual(reply_tools.MAX_CALLS, len(calls))
        # reply_tools' own limit (at most 2 notes a message) still holds inside the loop.
        self.assertEqual(2, self.database.execute("SELECT COUNT(*) FROM reply_owner_flags").fetchone()[0])

    def test_a_last_turn_that_still_calls_a_tool_is_no_answer(self):
        self.tool_turns = [[("flag_owner", {"reason": "note"})] for turn in range(40)]
        original = self.complete

        def stubborn(config, prompt, **named):
            if named.get("tool_choice") == "none":
                named = dict(named, tool_choice=None)
            return original(config, prompt, **named)
        self.popen.side_effect = stubborn
        task_id = self.enqueue()
        result = self.run_cycle()
        self.assertEqual("CLASSIFICATION_OUTPUT_MISSING", self.task(task_id)["blocker"])
        self.assertEqual(reply_tools.MAX_CALLS + 1, len(self.processes))
        self.assertNotIn("tool_effects", result["items"][0])   # a failed run's intents are dropped

    def test_each_turn_waits_at_most_60_seconds_for_a_slot_within_the_deadline(self):
        # Review B2: the slot wait is capped as the codex run's was and spent from the loop's deadline.
        self.tool_turns = [[("flag_owner", {"reason": "note"})]]
        self.enqueue()
        self.run_cycle()
        for call in self.processes:
            self.assertTrue(call["wait_in_timeout"])
            self.assertLessEqual(call["max_wait"], min(reply_worker.SLOT_WAIT_SECONDS, 10))

    def test_out_of_credit_is_a_wait_not_a_try(self):
        def refused(config, prompt, **named):
            import provider_limits
            error = reply_worker.text_model.TextModelHTTPError(402, "credits")
            raise error from provider_limits.out_of_credit(str(error))
        self.popen.side_effect = refused
        import provider_limits
        self.addCleanup(provider_limits.clear, provider_limits.CREDIT_WAIT_FILE)
        task_id = self.enqueue()
        before = self.task(task_id)["max_attempts"]
        self.run_cycle()
        import provider_limits
        task = self.task(task_id)
        self.assertEqual((provider_limits.CODE, "RETRY_WAIT"), (task["blocker"], task["status"]))
        self.assertEqual(before + 1, task["max_attempts"])   # the claim's try is given back


# The inherited ReplyWorkerTests run once in their own module, not again here.
for name in [name for name in dir(fixture.ReplyWorkerTests) if name.startswith("test_")]:
    setattr(CycleTests, name, None)
    setattr(ToolLoopTests, name, None)


if __name__ == "__main__":
    unittest.main()
