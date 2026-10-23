"""Dedupe before the first submit (owner, 2026-10-06: "we should never block employers unless the
jobs are the same, hence dedupe")."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import contextlib
import json
from pathlib import Path
import unittest
from unittest import mock

import applications
import desktop_adapter
import manual_application_receipts as manual
import orchestrator  # noqa: F401  (bounded_decision resolves research_command from it)
import posting_judge
import workflow_tasks

CONFIG = {"model": "synthetic", "codex": "synthetic"}


def judge(verdict, same_as="", prompts=None):
    """A same-posting judge executor that answers `verdict` (for adapter.same_job_executor)."""
    def executor(working, command, prompt, log, timeout):
        if prompts is not None:
            prompts.append(prompt)
        (Path(working) / "decision.json").write_text(json.dumps(
            {"verdict": verdict, "same_as": same_as, "reason": "judged", "quotes": []}))
    return executor


class SamePostingBeforeSubmitTests(unittest.TestCase):
    def setUp(self):
        self.fixture = manual.Fixture()
        self.addCleanup(self.fixture.close)
        fixture = self.fixture
        workflow_tasks.initialize(fixture.database)
        with fixture.database:
            self.other = applications.ingest(fixture.database, {
                "company": "Synthetic", "role": "Staff Software Engineer",
                "url": "https://www.linkedin.com/jobs/view/4000000001"}, fixture.now)
            fixture.database.execute("UPDATE application_jobs SET state='SUBMITTED' WHERE job_key=?", (self.other,))
        self.prompts = []

    def run_to_submit(self, verdict, same_as=""):
        fixture = self.fixture

        fixture.adapter.same_job_executor = judge(verdict, same_as, self.prompts)
        # Already read (an inspect request exists), so only the before-submit judgment is asked.
        request = fixture.claim(fixture.queue("inspect", {"job": fixture.job()}))
        desktop_adapter.complete(fixture.database, dict(request, result=fixture.review), fixture.now)
        applications.process(fixture.database, fixture.job(), fixture.adapter, fixture.policy, fixture.root,
                             fixture.now, None, CONFIG)
        return fixture.database.execute("SELECT count(*) FROM desktop_requests WHERE operation='submit'").fetchone()[0]

    def test_same_verdict_attaches_the_job_before_any_submit(self):
        submits = self.run_to_submit("SAME", self.other)
        self.assertEqual(len(self.prompts), 1)
        self.assertEqual((self.fixture.job()["state"], self.fixture.job()["blocker"]), ("DUPLICATE", posting_judge.SAME_JOB_BLOCKER))
        self.assertEqual(submits, 0)
        self.assertEqual(self.fixture.database.execute("SELECT count(*) FROM application_attempts").fetchone()[0], 0)
        linked = self.fixture.database.execute("SELECT detail FROM application_events WHERE event='SAME_POSTING_LINKED'").fetchone()
        self.assertEqual((json.loads(linked[0])["same_as"], json.loads(linked[0])["before_submit"]), (self.other, True))

    def test_different_verdict_proceeds_to_submit(self):
        submits = self.run_to_submit("DIFFERENT")
        self.assertEqual(len(self.prompts), 1)
        self.assertEqual(submits, 1)
        self.assertNotEqual(self.fixture.job()["state"], "DUPLICATE")



class ReviewFixTests(SamePostingBeforeSubmitTests):
    """Fixes from the 2026-10-06 adversarial reviews."""

    def test_a_judgment_that_cannot_run_holds_instead_of_submitting(self):
        fixture = self.fixture

        def failing(working, command, prompt, log, timeout):
            self.prompts.append(prompt)
            raise RuntimeError("402 Payment Required")

        fixture.adapter.same_job_executor = failing
        request = fixture.claim(fixture.queue("inspect", {"job": fixture.job()}))
        desktop_adapter.complete(fixture.database, dict(request, result=fixture.review), fixture.now)
        applications.process(fixture.database, fixture.job(), fixture.adapter, fixture.policy, fixture.root,
                             fixture.now, None, CONFIG)
        self.assertEqual(fixture.job()["blocker"], applications.SAME_POSTING_CHECK_PENDING)
        self.assertEqual(fixture.database.execute("SELECT count(*) FROM desktop_requests WHERE operation='submit'").fetchone()[0], 0)

    def test_a_same_naming_an_unsettled_job_waits_instead_of_becoming_a_duplicate(self):
        # Round 5: a copy of a job still RECONCILE waits; it becomes DUPLICATE only once that job is
        # SUBMITTED, and goes on if it turns out not submitted.
        fixture = self.fixture
        with fixture.database:
            fixture.database.execute("UPDATE application_jobs SET state='RECONCILE' WHERE job_key=?", (self.other,))
        self.assertEqual(self.run_to_submit("SAME", self.other), 0)
        self.assertEqual(fixture.job()["blocker"], applications.SAME_POSTING_AWAITING_SETTLEMENT)
        self.assertNotEqual(fixture.job()["state"], "DUPLICATE")

    def test_a_waiting_copy_is_judged_again_when_its_job_was_not_submitted(self):
        fixture = self.fixture
        with fixture.database:
            fixture.database.execute("UPDATE application_jobs SET state='RECONCILE' WHERE job_key=?", (self.other,))
        self.assertEqual(self.run_to_submit("SAME", self.other), 0)
        with fixture.database:
            fixture.database.execute("UPDATE application_jobs SET state='RETRY_WAIT' WHERE job_key=?", (self.other,))
        fixture.adapter.same_job_executor = judge("DIFFERENT", "", self.prompts)
        asked = len(self.prompts)
        applications.process(fixture.database, fixture.job(), fixture.adapter, fixture.policy, fixture.root,
                             fixture.now, None, CONFIG)
        self.assertGreaterEqual(len(self.prompts), asked)
        self.assertNotEqual(fixture.job()["state"], "DUPLICATE")
        self.assertNotEqual(fixture.job()["blocker"], applications.SAME_POSTING_AWAITING_SETTLEMENT)

    def test_a_waiting_copy_never_read_is_asked_as_before_its_first_read(self):
        # Round 12: once the job it waited on settles not submitted, a copy that was never read is
        # judged as before a first read, where a failed judgment holds nothing.
        fixture = self.fixture
        job = fixture.job()
        with fixture.database:
            fixture.database.execute("UPDATE application_jobs SET state='RETRY_WAIT' WHERE job_key=?", (self.other,))
            applied = applications.ingest(fixture.database, {
                "company": "Synthetic", "role": "Data Engineer",
                "url": "https://www.linkedin.com/jobs/view/4000000002"}, fixture.now)
            fixture.database.execute("UPDATE application_jobs SET state='SUBMITTED' WHERE job_key=?", (applied,))
            applications.hold(fixture.database, job, applications.SAME_POSTING_AWAITING_SETTLEMENT, "waiting", fixture.now, "BLOCKED", None)
            applications.event(fixture.database, job["job_key"], "SAME_POSTING_AWAITING_SETTLEMENT", {"same_as": self.other}, fixture.now)

        def failing(working, command, prompt, log, timeout):
            self.prompts.append(prompt)
            raise RuntimeError("402 Payment Required")

        fixture.adapter.same_job_executor = failing
        applications.process(fixture.database, fixture.job(), fixture.adapter, fixture.policy, fixture.root,
                             fixture.now, None, CONFIG)
        self.assertTrue(self.prompts)
        self.assertNotEqual(fixture.job()["blocker"], applications.SAME_POSTING_CHECK_PENDING)

    def failing(self, working, command, prompt, log, timeout):
        self.prompts.append(prompt)
        raise RuntimeError("402 Payment Required")

    def test_a_pending_check_whose_judge_still_fails_holds_again_without_a_read(self):
        fixture = self.fixture
        fixture.adapter.same_job_executor = self.failing
        request = fixture.claim(fixture.queue("inspect", {"job": fixture.job()}))
        desktop_adapter.complete(fixture.database, dict(request, result=fixture.review), fixture.now)
        applications.process(fixture.database, fixture.job(), fixture.adapter, fixture.policy, fixture.root,
                             fixture.now, None, CONFIG)
        self.assertEqual(fixture.job()["blocker"], applications.SAME_POSTING_CHECK_PENDING)
        requests = fixture.database.execute("SELECT count(*) FROM desktop_requests").fetchone()[0]
        asked = len(self.prompts)
        later = desktop_adapter.later(fixture.now, 3601)
        applications.process(fixture.database, fixture.job(), fixture.adapter, fixture.policy, fixture.root,
                             later, None, CONFIG)
        self.assertEqual(fixture.job()["blocker"], applications.SAME_POSTING_CHECK_PENDING)
        self.assertEqual(fixture.database.execute("SELECT count(*) FROM desktop_requests").fetchone()[0], requests)
        self.assertGreater(len(self.prompts), asked)

    def test_a_failed_judgment_before_the_first_read_holds_nothing(self):
        # Before the first read anything but SAME leaves the job as it was, an exception included.
        for label in ("executor raises", "judgment raises"):
            with self.subTest(label):
                self.setUp()
                fixture = self.fixture
                fixture.adapter.same_job_executor = self.failing
                with mock.patch.object(posting_judge, "same_job", side_effect=RuntimeError("database is locked")) \
                        if label == "judgment raises" else contextlib.nullcontext():
                    applications.process(fixture.database, fixture.job(), fixture.adapter, fixture.policy, fixture.root,
                                         fixture.now, None, CONFIG)
                self.assertEqual(len(self.prompts), 1 if label == "executor raises" else 0)  # it was asked
                self.assertNotEqual(fixture.job()["blocker"], applications.SAME_POSTING_CHECK_PENDING)
                self.assertNotEqual(fixture.job()["state"], "DUPLICATE")
                self.assertEqual(fixture.database.execute(
                    "SELECT count(*) FROM desktop_requests WHERE operation='inspect'").fetchone()[0], 1)

class UnappliedCopiesTests(unittest.TestCase):
    """Two unapplied copies of one posting: the first submits (no candidates, the judge is not
    asked); the second's before-submit judgment sees the first and a SAME attaches it."""

    def setUp(self):
        self.fixture = manual.Fixture()
        self.addCleanup(self.fixture.close)
        fixture = self.fixture
        workflow_tasks.initialize(fixture.database)
        url = "https://jobs.ashbyhq.com/synthetic/role-456"
        with fixture.database:
            self.copy = applications.ingest(fixture.database, {"company": "Synthetic", "role": "Staff Software Engineer",
                                                               "url": url, "requisition_id": "role-456"}, fixture.now)
        self.copy_review = dict(fixture.review, requisition="role-456", official_url=url,
                                history=dict(fixture.review["history"], job_key=self.copy))
        self.prompts = []

    def job(self, key):
        return dict(self.fixture.database.execute("SELECT * FROM application_jobs WHERE job_key=?", (key,)).fetchone(),
                    candidate_email="candidate@example.invalid")

    def read_and_process(self, key, review):
        fixture = self.fixture
        request = fixture.claim(fixture.queue("inspect", {"job": self.job(key)}))
        desktop_adapter.complete(fixture.database, dict(request, result=review), fixture.now)
        applications.process(fixture.database, self.job(key), fixture.adapter, fixture.policy, fixture.root,
                             fixture.now, None, CONFIG)

    def submits(self, key):
        return self.fixture.database.execute("SELECT count(*) FROM desktop_requests WHERE operation='submit' AND job_key=?",
                                             (key,)).fetchone()[0]

    def test_the_second_copy_is_attached_to_the_first_once_it_is_applied(self):
        fixture = self.fixture
        # The copy is QUEUED and unapplied: not a candidate, so the first job is not asked about it.
        self.assertIsNone(posting_judge.same_job_packet(fixture.database, self.job(fixture.key), True))
        fixture.adapter.same_job_executor = judge("SAME", fixture.key, self.prompts)
        self.read_and_process(fixture.key, fixture.review)
        self.assertEqual((self.prompts, self.submits(fixture.key)), ([], 1))
        self.read_and_process(self.copy, self.copy_review)
        self.assertEqual(len(self.prompts), 1)
        self.assertIn(fixture.key, self.prompts[0])
        # The first job's submission is not settled yet (its submit result is pending): the copy
        # waits rather than becoming a DUPLICATE (round 5).
        self.assertEqual(self.job(self.copy)["blocker"], applications.SAME_POSTING_AWAITING_SETTLEMENT)
        self.assertEqual(self.submits(self.copy), 0)
        # Once the first job is SUBMITTED, the copy's next pass asks again and attaches it.
        with fixture.database:
            fixture.database.execute("UPDATE application_jobs SET state='SUBMITTED' WHERE job_key=?", (fixture.key,))
            fixture.database.execute("UPDATE application_attempts SET state='VERIFIED' WHERE job_key=?", (fixture.key,))
        applications.process(fixture.database, self.job(self.copy), fixture.adapter, fixture.policy, fixture.root,
                             fixture.now, None, CONFIG)
        self.assertEqual((self.job(self.copy)["state"], self.job(self.copy)["blocker"]), ("DUPLICATE", posting_judge.SAME_JOB_BLOCKER))
        self.assertEqual(self.submits(self.copy), 0)


class HelperTests(unittest.TestCase):
    def test_find_jobs_reports_when_the_application_was_submitted(self):
        # Round 20: a page confirmation settled later must not read as applied that day.
        import sqlite3
        import reply_tools
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        applications.initialize(database)
        key = applications.ingest(database, {"company": "Hanso", "role": "Platform Engineer",
                                             "url": "https://job-boards.greenhouse.io/hanso/jobs/1"}, "2026-09-20T00:00:00+00:00")
        applications.event(database, key, "SUBMISSION_VERIFIED", {"evidence_kind": "page_confirmation",
                                                                   "armed_at": "2026-09-24T11:40:21+00:00"}, "2026-10-07T17:00:00+00:00")
        self.assertEqual(reply_tools.find_jobs(database, "Hanso")["jobs"][0]["submitted_at"], "2026-09-24T11:40:21+00:00")

    def test_stored_records_keep_what_is_read(self):
        import reporting
        payload = {"worker_tasks": {"state": "OK", "records": [{"worker": "w", "status": "COMPLETE"}] * 5},
                   "bounded_recovery": {"records": [{"category": "x"}] * 5 + [{"category": "abandoned"}]}}
        stored = reporting.stored_payload(payload)
        self.assertIsNone(stored["worker_tasks"]["records"])
        self.assertEqual(stored["bounded_recovery"]["records"][0]["category"], "abandoned")
        self.assertEqual(len(stored["bounded_recovery"]["records"]), 2)
        self.assertEqual(stored["bounded_recovery"]["omitted_records"], 6)
        result = {"observation": {"observed_at": "t"}, "services": {"big": {"state": "COMPLETE", "items": ["x" * 3000]}}}
        compact = orchestrator.stored_run(result)
        self.assertEqual(compact["observation"], result["observation"])
        self.assertEqual(compact["services"]["big"]["state"], "COMPLETE")
        self.assertIn("omitted_bytes", compact["services"]["big"]["items"])


for _name in [name for name in dir(SamePostingBeforeSubmitTests) if name.startswith("test_")]:
    setattr(ReviewFixTests, _name, None)


if __name__ == "__main__":
    unittest.main()
