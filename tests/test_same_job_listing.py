import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import json
from pathlib import Path
import sqlite3
import tempfile
import json
import unittest

import applications
import posting_judge
import workflow_tasks

NOW = "2026-09-27T20:00:00+00:00"
CONFIG = {"model": "synthetic", "codex": "synthetic"}
TITLE = "Backend Engineer (.NET) - Messaging"


class Runtime:
    @staticmethod
    def research_command(root, config, output, schema):
        return ["synthetic-model"]


class ReadAttempted(BaseException):
    """Raised past process()'s own error handling, so reaching the read ends the test cleanly."""


class NoRead:
    """The browser must not be used for a copy the model links to an existing job."""

    def __init__(self, answer):
        self.calls, self.answer, self.read = [], answer, False
        self.same_job_executor = self.executor

    def executor(self, working, command, prompt, log, timeout):
        self.calls.append(prompt)
        (Path(working) / "decision.json").write_text(json.dumps(self.answer))

    def call(self, operation, payload):
        raise ReadAttempted(operation)


class SameJobListingTests(unittest.TestCase):
    """Owner, 2026-09-27: a copy of a posting already on file is attached, not read again."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp()); (self.root / "reports").mkdir()
        self.database = sqlite3.connect(":memory:"); self.database.row_factory = sqlite3.Row
        applications.initialize(self.database); workflow_tasks.initialize(self.database)
        # A job that carries an application: only those are candidates (2026-10-06).
        self.add("mooby:a0000003", "a0000003", "SUBMITTED", "https://jobs.ashbyhq.com/mooby/a0000003")
        self.add("mooby:unresolved:8b1a", "unresolved:8b1a", "QUEUED", "https://www.linkedin.com/jobs/view/4000000001")
        self.policy = {"answers": {"email": {"value": "owner@example.com"}}, "max_attempts": 3}

    def add(self, key, requisition, state, url):
        payload = {"company": "Mooby", "role": TITLE, "url": url}
        self.database.execute("INSERT INTO application_jobs(job_key,employer,requisition,payload,state,updated_at) VALUES (?,?,?,?,?,?)",
                              (key, "mooby", requisition, json.dumps(payload), state, NOW))
        self.database.commit()

    def run_copy(self, answer):
        adapter = NoRead(answer)
        job = self.database.execute("SELECT * FROM application_jobs WHERE job_key='mooby:unresolved:8b1a'").fetchone()
        import orchestrator  # the runtime bounded_decision resolves research_command from
        try:
            applications.process(self.database, job, adapter, self.policy, self.root, NOW, None, CONFIG)
        except ReadAttempted:
            adapter.read = True
        state = self.database.execute("SELECT state,blocker FROM application_jobs WHERE job_key='mooby:unresolved:8b1a'").fetchone()
        return adapter, tuple(state)

    def answer(self, verdict, same_as="", quotes=(TITLE,)):
        return {"verdict": verdict, "same_as": same_as, "reason": "judged", "quotes": list(quotes)}

    def test_a_same_attaches_the_copy_without_a_read_even_without_quotes(self):
        adapter, state = self.run_copy(self.answer("SAME", "mooby:a0000003", []))
        self.assertEqual(state, ("DUPLICATE", posting_judge.SAME_JOB_BLOCKER))
        self.assertFalse(adapter.read)

    def test_a_quoted_same_attaches_the_copy_without_a_read(self):
        adapter, state = self.run_copy(self.answer("SAME", "mooby:a0000003"))
        self.assertEqual(state, ("DUPLICATE", posting_judge.SAME_JOB_BLOCKER))
        self.assertEqual(len(adapter.calls), 1)
        self.assertFalse(adapter.read)
        linked = self.database.execute("SELECT detail FROM application_events WHERE event='SAME_POSTING_LINKED'").fetchone()
        self.assertEqual(json.loads(linked[0])["same_as"], "mooby:a0000003")

    def test_an_unsupported_same_does_not_attach(self):
        # Quotes no longer decide anything (owner, 2026-09-27); naming a job it was not shown does.
        for label, answer in (("a job it was not shown", self.answer("SAME", "someone-else:1")),):
            with self.subTest(label):
                self.setUp()
                adapter, state = self.run_copy(answer)
                self.assertNotEqual(state[0], "DUPLICATE")
                self.assertTrue(adapter.read)

    def test_a_copy_of_a_given_up_job_is_a_fresh_chance(self):
        self.database.execute("UPDATE application_jobs SET state='ABANDONED' WHERE job_key='mooby:a0000003'")
        self.database.commit()
        adapter, state = self.run_copy(self.answer("SAME", "mooby:a0000003"))
        self.assertNotEqual(state[0], "DUPLICATE")
        self.assertTrue(adapter.read)

    def test_different_proceeds_and_is_not_asked_twice(self):
        value = posting_judge.same_job_packet(self.database, self.database.execute(
            "SELECT * FROM application_jobs WHERE job_key='mooby:unresolved:8b1a'").fetchone())
        self.assertEqual([job["job_key"] for job in value["existing_jobs"]], ["mooby:a0000003"])
        first, state = self.run_copy(self.answer("DIFFERENT"))
        self.assertNotEqual(state[0], "DUPLICATE")
        self.assertTrue(first.read)
        self.database.execute("UPDATE application_jobs SET state='QUEUED',blocker=NULL WHERE job_key='mooby:unresolved:8b1a'")
        self.database.commit()
        second, unused = self.run_copy(self.answer("SAME", "mooby:a0000003"))
        self.assertEqual(second.calls, [])

    def test_an_unapplied_job_at_the_employer_is_judged_before_the_first_read(self):
        # Owner, 2026-09-27 (restored in review round 11): before the first read the copy is judged
        # against the company's other jobs on file, and a SAME attaches it unless that job was given
        # up. Before a submit only jobs carrying an application are candidates.
        for state in ("QUEUED", "INELIGIBLE"):
            with self.subTest(state=state):
                self.database.execute("UPDATE application_jobs SET state=?,blocker=NULL WHERE job_key='mooby:a0000003'", (state,))
                self.database.execute("UPDATE application_jobs SET state='QUEUED',blocker=NULL WHERE job_key='mooby:unresolved:8b1a'")
                self.database.commit()
                copy = self.database.execute("SELECT * FROM application_jobs WHERE job_key='mooby:unresolved:8b1a'").fetchone()
                self.assertIn("mooby:a0000003", json.dumps(posting_judge.same_job_packet(self.database, copy)))
                self.assertNotIn("mooby:a0000003", json.dumps(posting_judge.same_job_packet(self.database, copy, before_submit=True)))
                adapter, state_after = self.run_copy(self.answer("SAME", "mooby:a0000003"))
                self.assertEqual(state_after[0], "DUPLICATE")
                self.assertFalse(adapter.read)


if __name__ == "__main__":
    unittest.main()
