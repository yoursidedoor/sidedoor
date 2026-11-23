import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
"""Owner H1/H2, 2026-11-20: a reply about a contact at a company with three submitted jobs went over
the 128 KB reply input cap (each attempt carried a ~20 KB pre-submit review) and was held unread.
H1: the attempt evidence given to the reply AI leaves out `review`. H2: an input still over the cap
leaves out background, largest piece first, until it fits; the instructions, the reply format and
the message being answered are never cut."""
import json
import sqlite3
import unittest

import champion_composition
import pipeline_routing as routing
import reply_worker


TEMPLATE = "instructions " * 100


def request(**extra):
    base = {"source_key": "k", "source_sha256": "a" * 64, "raw_message": {"text": "Hi, are you still looking?"},
            "source_snapshot": {"payload": "{}", "raw": "{}"}, "message_text": "Hi, are you still looking?",
            "output_contract": {"label": ["AGREEMENT"]},
            "job_scope": [{"job_key": "job-a", "role_context": {"facts": "r" * 500},
                           "application_status_context": {"claims": "a" * 60000},
                           "application_interview_context": {"history": "h" * 200}},
                          {"job_key": "job-b", "role_context": {"facts": "r" * 500},
                           "application_status_context": {"claims": "b" * 50000}}],
            "candidate_resume": {"source_sha256": "c" * 64, "text": "resume " * 3000},
            "conversation_context": [{"content_sha256": "m1", "snapshot": {"text": "x" * 4000}}],
            "initial_invitation": {"content_sha256": "i1", "snapshot": {"text": "note"}},
            "referral_context": []}
    base.update(extra)
    return base


class FitTests(unittest.TestCase):
    def test_an_input_within_the_cap_is_unchanged(self):
        small = request(job_scope=[{"job_key": "job-a", "application_status_context": {"claims": "a"}}])
        fitted, prompt = reply_worker.fit(TEMPLATE, small)
        self.assertIs(small, fitted)
        self.assertEqual(TEMPLATE + "\n\nUNTRUSTED_SOURCE_JSON:\n" + routing.encoded(small), prompt)
        self.assertNotIn("context_trimmed_for_size", fitted)

    def test_largest_background_goes_first_and_the_message_stays(self):
        original = request()
        fitted, prompt = reply_worker.fit(TEMPLATE, original)
        self.assertIsNotNone(prompt)
        self.assertLessEqual(len(prompt.encode("utf-8")), reply_worker.MAX_INPUT_BYTES)
        self.assertTrue(prompt.startswith(TEMPLATE + "\n\nUNTRUSTED_SOURCE_JSON:\n"))
        self.assertEqual(["job_scope.job-a.application_status_context"], fitted["context_trimmed_for_size"]["left_out"])
        self.assertEqual(reply_worker.TRIM_NOTE, fitted["context_trimmed_for_size"]["note"])
        for field in ("raw_message", "source_snapshot", "message_text", "output_contract", "source_sha256",
                      "candidate_resume", "conversation_context", "initial_invitation"):
            self.assertEqual(original[field], fitted[field], field)
        self.assertEqual(["job-a", "job-b"], [scope["job_key"] for scope in fitted["job_scope"]])
        self.assertNotIn("application_status_context", fitted["job_scope"][0])
        self.assertEqual(original["job_scope"][1], fitted["job_scope"][1])
        self.assertIn("application_status_context", original["job_scope"][0])   # the caller's copy is untouched

    def test_other_large_pieces_go_too_when_job_background_is_not_enough(self):
        big = request(candidate_resume={"source_sha256": "c" * 64, "text": "resume " * 12000})
        fitted, prompt = reply_worker.fit(TEMPLATE, big)
        self.assertIsNotNone(prompt)
        left_out = fitted["context_trimmed_for_size"]["left_out"]
        self.assertEqual("candidate_resume.text", left_out[0])   # 84 KB, the largest
        self.assertIsNone(fitted["candidate_resume"]["text"])
        self.assertEqual("c" * 64, fitted["candidate_resume"]["source_sha256"])
        self.assertEqual(big["raw_message"], fitted["raw_message"])

    def test_the_same_input_trims_the_same_way(self):
        self.assertEqual(reply_worker.fit(TEMPLATE, request()), reply_worker.fit(TEMPLATE, request()))

    def test_still_held_when_nothing_left_to_trim_fits(self):
        fitted, prompt = reply_worker.fit("x" * reply_worker.MAX_INPUT_BYTES, request())
        self.assertIsNone(prompt)


class WithoutReviewTests(unittest.TestCase):
    def setUp(self):
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        self.database.execute("CREATE TABLE application_jobs (job_key TEXT, state TEXT, blocker TEXT, receipt TEXT)")
        self.database.execute("CREATE TABLE application_attempts (attempt_id TEXT, job_key TEXT, state TEXT, started_at TEXT, evidence TEXT)")
        self.database.execute("INSERT INTO application_jobs VALUES ('job-a','SUBMITTED',NULL,NULL)")

    def claims(self, *evidence):
        for index, value in enumerate(evidence):
            self.database.execute("INSERT INTO application_attempts VALUES (?,?,?,?,?)",
                                  ("attempt-%d" % index, "job-a", "VERIFIED", "2026-11-0%dT00:00:00+00:00" % (index + 1), value))
        return champion_composition.application_context(self.database, {"job_key": "job-a"})["submission_claims"]

    def test_review_is_left_out_and_answers_stay(self):
        evidence = {"answers": {"q1": "Yes"}, "answer_sources": {"q1": "profile"}, "resume_sha256": "d" * 64,
                    "requirement_revision": 1, "review": {"checks": "z" * 20000}}
        claim, = self.claims(json.dumps(evidence))
        kept = json.loads(claim["evidence"])
        self.assertNotIn("review", kept)
        self.assertEqual({key: value for key, value in evidence.items() if key != "review"}, kept)
        self.assertEqual(("attempt-0", "VERIFIED"), (claim["attempt_id"], claim["state"]))

    def test_evidence_without_a_review_or_not_an_object_is_unchanged(self):
        values = ['{"answers":{"q1":"Yes"}}', "[1,2]", "not json", None]
        self.assertEqual(values, [claim["evidence"] for claim in self.claims(*values)])


if __name__ == "__main__":
    unittest.main()


class FoundForJobTests(unittest.TestCase):
    """Owner, 2026-11-20: the background of the job the contact was found for is never trimmed."""

    def test_the_found_for_job_keeps_its_background_and_others_are_cut(self):
        packet = request(job_scope=[
            {"job_key": "found-for", "rw_result_id": "rw-1", "role_context": {"facts": "r" * 500},
             "application_status_context": {"claims": "f" * 90000}},
            {"job_key": "other", "rw_result_id": None, "application_status_context": {"claims": "o" * 30000}}])
        fitted, prompt = reply_worker.fit(TEMPLATE, packet)
        self.assertIsNotNone(prompt)
        self.assertEqual(packet["job_scope"][0], fitted["job_scope"][0])
        self.assertNotIn("job_scope.found-for.application_status_context", fitted["context_trimmed_for_size"]["left_out"])
        self.assertIn("job_scope.other.application_status_context", fitted["context_trimmed_for_size"]["left_out"])

    def test_a_found_for_job_too_big_to_fit_holds_rather_than_cut(self):
        packet = request(job_scope=[{"job_key": "found-for", "rw_result_id": "rw-1",
                                     "application_status_context": {"claims": "f" * 140000}}])
        self.assertIsNone(reply_worker.fit(TEMPLATE, packet)[1])

    def test_a_revision_still_compares_the_found_for_job_background(self):
        packet = request(job_scope=[{"job_key": "found-for", "rw_result_id": "rw-1", "role_context": {"facts": "a"}},
                                    {"job_key": "other", "rw_result_id": None, "role_context": {"facts": "b"}}])
        stripped = reply_worker.without_trimmable(packet)
        self.assertEqual({"facts": "a"}, stripped["job_scope"][0]["role_context"])
        self.assertNotIn("role_context", stripped["job_scope"][1])


class JobScopeCapTests(unittest.TestCase):
    """Owner H7, 2026-11-20: at most 10 jobs; submitted or in progress before abandoned, most recent first."""

    def setUp(self):
        import applications
        import conversion
        import engagement
        import research_workflow as research
        import workflow_tasks
        from test_pipeline_routing import NOW, PROFILE, message
        self.now = NOW
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        self.addCleanup(self.database.close)
        for module in (applications, research, engagement, conversion, routing, workflow_tasks, reply_worker):
            module.initialize(self.database)
        self.config = {"pipeline_routing": {"account_ids": [123]}, "reply_worker": {"enabled": True}}
        event = message("Could you explain?", "cap-message")
        event["raw"]["quoted"] = event["inbox"]["quoted"] = None
        engagement.record_event(self.database, event)
        routing.cycle(self.database, self.config, NOW)
        source = routing.one(self.database, "SELECT * FROM pipeline_routing_sources WHERE source_id=?",
                             (routing.encoded(["heyreach", "cap-message"]),))
        self.task = routing.one(self.database, "SELECT * FROM workflow_tasks WHERE task_id=?",
                                (json.loads(source["result"])["review_task_id"],))
        self.database.execute("INSERT INTO rw_contacts (company, identity, name, historical, plan_id) VALUES (?,?,?,?,?)",
                              ("Mock Company", sorted(engagement.identity_keys(self.database, PROFILE))[0], "Contact", 0, "plan-1"))

    def job(self, number, state, updated_at):
        columns = [row[1] for row in self.database.execute("PRAGMA table_info(application_jobs)")]
        row = {"job_key": "mock-company:%02d" % number, "employer": "Mock Company", "requisition": "req-%02d" % number,
               "payload": json.dumps({"role": "Role %d" % number}), "state": state, "attempts": 0,
               "reconcile_attempts": 0, "updated_at": updated_at}
        self.database.execute("INSERT INTO application_jobs (%s) VALUES (%s)" % (",".join(row), ",".join("?" * len(row))),
                              [row[column] for column in row if column in columns])

    def scope(self):
        return reply_worker._source(self.database, self.task, self.config, self.now)[0]

    def test_at_most_ten_jobs_in_order_and_the_rest_counted(self):
        for number in range(1, 8):   # 7 abandoned, newest last
            self.job(number, "ABANDONED", "2026-11-%02dT00:00:00+00:00" % (10 + number))
        for number in range(8, 14):   # 6 submitted or in progress, oldest first
            self.job(number, "SUBMITTED" if number % 2 else "RECONCILE", "2026-10-%02dT00:00:00+00:00" % number)
        request = self.scope()
        self.assertEqual(10, len(request["job_scope"]))
        listed = {scope["job_key"] for scope in request["job_scope"]}
        self.assertTrue({"mock-company:%02d" % number for number in range(8, 14)} <= listed)   # all 6 active
        self.assertEqual({"mock-company:%02d" % number for number in (7, 6, 5, 4)}, listed - {"mock-company:%02d" % number for number in range(8, 14)})
        self.assertEqual(3, request["job_scope_left_out"]["count"])
        self.assertEqual(reply_worker.JOB_SCOPE_NOTE, request["job_scope_left_out"]["note"])

    def test_ten_or_fewer_jobs_are_all_listed_without_a_note(self):
        for number in range(1, 11):
            self.job(number, "SUBMITTED", "2026-11-01T00:00:00+00:00")
        request = self.scope()
        self.assertEqual(10, len(request["job_scope"]))
        self.assertNotIn("job_scope_left_out", request)


class PinnedJobScopeTests(JobScopeCapTests):
    """Owner H9, 2026-11-20: once a read is saved, its retries and revisions keep the jobs it was given."""

    def test_a_status_change_after_the_read_was_saved_keeps_the_selection(self):
        for number in range(1, 13):
            self.job(number, "ABANDONED", "2026-11-%02dT00:00:00+00:00" % number)
        first = self.scope()
        self.assertNotIn("mock-company:01", {scope["job_key"] for scope in first["job_scope"]})
        prompt = reply_worker.prompt_template() + "\n\nUNTRUSTED_SOURCE_JSON:\n" + routing.encoded(first)
        self.database.execute("INSERT INTO reply_worker_inputs VALUES (?,?,?,?,?)",
                              (self.task["task_id"], reply_worker.sha(prompt), prompt, first["source_sha256"], "{}"))
        # Job 1 is now being applied to again: it would outrank every abandoned job in a fresh selection.
        self.database.execute("UPDATE application_jobs SET state='RECONCILE', updated_at='2026-11-20T00:00:00+00:00' "
                              "WHERE job_key='mock-company:01'")
        again = self.scope()
        self.assertEqual([scope["job_key"] for scope in first["job_scope"]], [scope["job_key"] for scope in again["job_scope"]])
        self.assertEqual(first["job_scope_left_out"], again["job_scope_left_out"])
