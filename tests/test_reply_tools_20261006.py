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
        def write_log():
            directory = Path(self.processes[-1].options["cwd"])
            with (directory / reply_tools.LOG_FILE).open("a") as log:
                log.write(json.dumps({"tool": "queue_job", "intent": {
                    "url": "https://jobs.example.com/apply/9", "company": "Example Co", "role": "Staff Engineer",
                    "reason": "The contact sent the application link"}}) + "\n")
        self.during_process = write_log

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


# The inherited ReplyWorkerTests run once in their own module, not again here.
for name in [name for name in dir(fixture.ReplyWorkerTests) if name.startswith("test_")]:
    setattr(CycleTests, name, None)


if __name__ == "__main__":
    unittest.main()
