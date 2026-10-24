"""The Failures page shows the recorded reasons and each run's own records (owner, 2026-10-23: "why dont
we display the actual failure reasons?" ... "not just research reasons", and "so users dont have to
start running code"). Headlines and next steps are unchanged; Report a problem still sends codes only.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import contextlib
import io
import json
import logging
import re
import unittest
from unittest import mock

import failures_page
import linkedin_contacts
import log
import parallel_research
import report_problem
import test_dashboard_failures as pages
from test_dashboard_failures import task, research_day

base = pages.base
KEY = pages.KEY


def at(hour, minute=0):
    return base.pacific(2026, 10, 22, hour, minute)


def detail(blocker, **result):
    return json.dumps({"result": result, "evidence": [], "blocker": blocker})


def finder_run(database, run_id, state, started, **result):
    database.execute("INSERT INTO linkedin_contact_runs VALUES (?,?,?,?,?,?,?,?)",
                     (run_id, "acme-key", started.isoformat(), state, "sha", None, started.isoformat(),
                      json.dumps(dict({"company": "Acme", "research_result_id": "r1"}, **result))))


class Reasons(pages.GatherBase):
    def test_a_task_shows_its_recorded_causes_redacted_and_never_its_waits(self):
        failure = {"exit_code": 1, "stderr_tail": ["ERROR: unexpected status 500 key " + KEY, "ERROR: stream ended"]}
        task(self.database, "t1", "outreach-draft-inspection", "RETRY_WAIT", at(9, 30), "SUBSCRIPTION_EXECUTOR_FAILED",
             next_at=at(11), events=[("RETRY_WAIT", detail("SUBSCRIPTION_EXECUTOR_FAILED", executor_failure=failure), at(9)),
                                     ("RETRY_WAIT", detail("SUBSCRIPTION_EXECUTOR_FAILED", executor_failure=failure), at(9, 30)),
                                     ("PROVIDER_USAGE_LIMIT_WAIT", "{}", at(9, 40))])
        view = self.gather(secrets=failures_page.Secrets([KEY]))
        [item] = self.items(view, "outreach")
        self.assertEqual(item["text"], "Checking a note before it is sent didn't work.")   # unchanged headline
        [reason] = item["reasons"]
        self.assertEqual((reason["words"], reason["code"], reason["count"]),
                         ("The AI call failed.", "SUBSCRIPTION_EXECUTOR_FAILED", 2))
        self.assertIn("AI call: exit 1; ERROR: stream ended", reason["error"])
        self.assertEqual(item["log_href"], "/failures?run=task%3At1#run")
        self.assertEqual(item["refs"], {"task_ids": ["t1"]})
        self.assertNotIn(KEY, json.dumps(item, default=str))

    def test_at_most_three_distinct_causes_newest_first_then_more_with_a_logs_link(self):
        events = [("RETRY_WAIT", detail("CODE_%d" % number), at(9, number)) for number in range(5)]
        task(self.database, "t2", "message_writer", "RETRY_WAIT", at(9, 5), "CODE_4", next_at=at(11), events=events)
        [item] = self.items(self.gather(), "outreach")
        self.assertEqual([reason["code"] for reason in item["reasons"]], ["CODE_4", "CODE_3", "CODE_2"])
        self.assertEqual(item["reasons_more"], 2)
        self.assertTrue(item["reasons_href"].startswith("/failures?component=launchd"))

    def test_research_shows_each_industry_that_did_not_finish(self):
        result = {"state": "FAILED", "industries": [
            {"industry": "Fusion energy", "state": "FAILED", "abandonment_reason": "RESEARCH_DEADLINE_EXHAUSTED",
             "error": "TimeoutExpired after 600 s"},
            {"industry": "Robotics", "state": "COMPLETE"}]}
        research_day(self.database, "2026-10-22", "EXHAUSTED", "a1", "FAILED", at(9), result=result)
        [item] = self.items(self.gather(), "research")
        self.assertEqual(item["text"], "Research didn't finish today: no industry finished its search.")
        [reason] = item["reasons"]
        self.assertEqual((reason["where"], reason["code"], reason["error"]),
                         ("Fusion energy", "RESEARCH_DEADLINE_EXHAUSTED", "TimeoutExpired after 600 s"))
        self.assertEqual(reason["log_href"], failures_page.run_href(
            "research", "a1:" + parallel_research._label("Fusion energy")))
        self.assertEqual(item["refs"]["research_id"], "a1")

    def test_finder_failures_with_tries_and_the_models_own_reason(self):
        linkedin_contacts.initialize(self.database)
        self.database.execute("INSERT INTO rw_results VALUES ('r1','p1','2026-10-22T00:00:00+00:00','{}','{}')")
        finder_run(self.database, "f1", "FAILED", at(7), blocker="LINKEDIN_CONTACTS_INTERRUPTED")
        finder_run(self.database, "f2", "COMPLETE", at(8), outcome="UNKNOWN", reason="LinkedIn showed a sign-in wall",
                   research_failure_diagnosis="Research read signed out (HTTP 999)")
        finder_run(self.database, "f3", "PROVIDER_WAIT", at(8, 30), blocker="PROVIDER_USAGE_LIMIT_WAIT")   # a wait
        [item] = self.items(self.gather(), "outreach")
        self.assertEqual(item["text"], "Finding people to contact at Acme didn't work (2 of 3 tries used).")
        codes = [reason["code"] for reason in item["reasons"]]
        self.assertEqual(codes, ["LINKEDIN_CONTACTS_UNKNOWN", "LINKEDIN_CONTACTS_INTERRUPTED"])
        self.assertIn("sign-in wall", item["reasons"][0]["error"])
        self.assertIn("HTTP 999", item["reasons"][0]["error"])
        self.assertEqual(item["log_href"], "/failures?run=finder%3Af2#run")
        finder_run(self.database, "f4", "FAILED", at(9), blocker="LINKEDIN_CONTACTS_RESULT_BINDING_INVALID")
        [item] = self.items(self.gather(), "outreach")
        self.assertEqual(item["text"], "Finding people to contact at Acme failed 3 of 3 tries, so Sidedoor stopped trying.")
        self.assertIn("FINDER_RETRIES_EXHAUSTED", [reason["code"] for reason in item["reasons"]])
        finder_run(self.database, "f5", "COMPLETE", at(9, 30), outcome="FOUND")   # settled: no longer a failure
        self.assertEqual(self.items(self.gather(), "outreach"), [])

    def test_a_tail_that_began_inside_the_echo_keeps_only_codex_levels(self):
        text = "Jane Doe, 12 Main St\nERROR: stream disconnected\n"
        self.assertEqual(failures_page.codex_safe(text, truncated=True).splitlines(),
                         [failures_page.PROMPT_WITHHELD, "ERROR: stream disconnected"])
        self.assertEqual(failures_page.codex_safe("plain line\n", truncated=False), "plain line\n")

    def test_notifications_show_the_transport_code_first(self):
        self.database.execute("INSERT INTO notification_outbox(event_key,message,state,attempts,last_error,send_error,updated_at) "
                              "VALUES ('e1','m','FAILED',3,'NOTIFICATION_SEND_FAILED','BRIDGE_HTTP_400',?)", (at(9).isoformat(),))
        [item] = self.items(self.gather(), "connections")
        self.assertEqual([reason["code"] for reason in item["reasons"]], ["BRIDGE_HTTP_400"])

    def test_industries_that_are_not_a_list_never_break_the_page(self):
        research_day(self.database, "2026-10-22", "EXHAUSTED", "a1", "FAILED", at(9),
                     result={"state": "FAILED", "industries": 7, "error": "boom"})
        [item] = self.items(self.gather(), "research")
        self.assertEqual([reason["code"] for reason in item["reasons"]], ["RESEARCH_FAILED"])

    def test_finder_runs_group_by_research_plan_as_the_runtime_does(self):
        # A retried research worker writes a second rw_results row for the same plan: one research run.
        linkedin_contacts.initialize(self.database)
        for result_id in ("r1", "r2"):
            self.database.execute("INSERT INTO rw_results VALUES (?,'p1','2026-10-22T00:00:00+00:00','{}','{}')", (result_id,))
        finder_run(self.database, "g1", "FAILED", at(7), blocker="LINKEDIN_CONTACTS_INTERRUPTED")
        finder_run(self.database, "g2", "COMPLETE", at(8), outcome="FOUND", research_result_id="r2")
        self.assertEqual(self.items(self.gather(), "outreach"), [])   # settled for plan p1

    def test_answering_retry_shows_each_open_question_and_why(self):
        import answering_agent
        answering_agent.initialize(self.database)
        base.job(self.database, "acme:1", "RETRY_WAIT", "ANSWERING_RETRY")
        key = "unidentified-question:office_attendance_3_days:529c9e1439f1"
        audit = {"unknown": [key], "answered": [], "refused": [],
                 "verdicts": [{"question_key": key, "outcome": "UNKNOWN", "reason": "No recorded fact covers relocation"}]}
        self.database.execute("INSERT INTO application_answering_attempts VALUES (?,?,?,?,?,?,?,?,?,?)",
                              ("att1", "acme:1", at(9).isoformat(), "p", None, None, None, "m", "ANSWERING_RETRY", json.dumps(audit)))
        self.database.execute("UPDATE application_jobs SET updated_at=?", (at(9).isoformat(),))
        [item] = self.items(self.gather(), "applications")
        [reason] = item["reasons"]
        self.assertEqual((reason["where"], reason["error"]),
                         ("Question: office attendance 3 days", "No recorded fact covers relocation"))
        self.database.commit()
        view = failures_page.run_view(failures_page.overview.Reader(self.database), self.root, self.root / "logs",
                                      "job:acme:1", [])
        self.assertIn("Question still open: office attendance 3 days - No recorded fact covers relocation",
                      [entry["text"] for entry in view["records"]])

    def test_a_job_key_with_spaces_and_parentheses_opens_its_run_view(self):
        import answering_agent
        answering_agent.initialize(self.database)
        key = "insforge (instacloud):unresolved:2b717e4657c57b67dde5277a"
        base.job(self.database, key, "RETRY_WAIT", "ANSWERING_RETRY")
        self.database.execute("UPDATE application_jobs SET updated_at=?", (at(9).isoformat(),))
        self.database.execute("INSERT INTO application_events(job_key,event,at,detail) VALUES (?,?,?,?)",
                              (key, "RETRY_WAIT", at(9).isoformat(), json.dumps({"code": "ANSWERING_RETRY"})))
        audit = {"unknown": ["start_date:abc123def456"], "verdicts": [], "refused": []}
        self.database.execute("INSERT INTO application_answering_attempts VALUES (?,?,?,?,?,?,?,?,?,?)",
                              ("att2", key, at(9).isoformat(), "p", None, None, None, "m", "ANSWERING_RETRY", json.dumps(audit)))
        [item] = self.items(self.gather(), "applications")
        run = failures_page.urllib.parse.parse_qs(item["log_href"].split("?", 1)[1].split("#")[0])["run"][0]
        self.assertEqual(run, "job:" + key)
        self.database.commit()
        view = failures_page.run_view(failures_page.overview.Reader(self.database), self.root, self.root / "logs", run, [])
        texts = [entry["text"] for entry in view["records"]]
        self.assertIn("Question still open: start date - ANSWERING_RETRY", texts)
        self.assertTrue(any(text.startswith("RETRY_WAIT ANSWERING_RETRY") for text in texts), texts)
        for bad in ("dir:linkedin-contacts/..", "dir:linkedin-contacts/a b", "research:../x", "job:bad\x00key"):
            self.assertIsNone(failures_page.run_view(failures_page.overview.Reader(self.database), self.root,
                                                     self.root / "logs", bad, []), bad)

    def test_research_groups_industries_that_stopped_the_same_way(self):
        same = {"state": "FAILED", "abandonment_reason": "RESEARCH_DEADLINE_EXHAUSTED", "error": "timed out"}
        result = {"state": "FAILED", "industries": [dict(same, industry=name) for name in ("Fusion", "Robotics", "Space")]
                  + [{"industry": "Biotech", "state": "FAILED", "error": "HTTP 500"}]}
        research_day(self.database, "2026-10-22", "EXHAUSTED", "a1", "FAILED", at(9), result=result)
        [item] = self.items(self.gather(), "research")
        wheres = sorted(reason["where"] for reason in item["reasons"])
        self.assertEqual(wheres, ["3 industries (Fusion, Robotics, Space)", "Biotech"])

    def test_day_links_and_filters_use_the_local_date(self):
        late = base.pacific(2026, 10, 22, 20, 0)   # 03:00 UTC on the 23rd, the 22nd in Pacific time
        failures_page._ZONE.set(failures_page.ZoneInfo("America/Los_Angeles"))
        self.assertIn("day=2026-10-22", failures_page.logs_href("launchd", late))
        logs = self.root / "logs"
        logs.mkdir()
        (logs / "launchd.stderr.log").write_text("2026-10-23T03:00:00Z WARNING run x: evening line\n"
                                                 "2026-10-23T18:00:00Z WARNING run x: next day\n")
        view = failures_page.logs_view(logs, [], "launchd", "stderr", day="2026-10-22",
                                       zone=failures_page.ZoneInfo("America/Los_Angeles"))
        self.assertEqual([line[-12:] for line in view["streams"][0]["lines"]], ["evening line"])

    def test_a_long_codex_log_keeps_its_header_and_marks_the_gap(self):
        logs = self.root / "logs"
        logs.mkdir()
        research_id = "0a1b2c3d-0000-4000-8000-00000000abcd"
        body = "OpenAI Codex v0.1\nmodel: m\nuser\n" + "PROMPT Jane Doe\n" * 4000 + "ERROR: final failure\n"
        (logs / (research_id + "." + parallel_research._label("Fusion") + ".1.discovery.log")).write_text(body)
        view = failures_page.run_view(failures_page.overview.Reader(self.database), self.root, logs,
                                      "research:" + research_id, [])
        [lines] = [entry["lines"] for entry in view["files"]]
        self.assertEqual(lines, ["OpenAI Codex v0.1", "model: m", failures_page.PROMPT_WITHHELD, failures_page.GAP,
                                 "ERROR: final failure"])

    def test_a_finder_crash_records_what_it_said_and_where(self):
        import workflow_tasks
        linkedin_contacts.initialize(self.database)
        workflow_tasks.initialize(self.database)
        self.database.commit()

        def crashed(config, prompt, directory, timeout):
            raise RuntimeError("socket closed key " + KEY)
        item = {"company": "Acme", "company_key": "acme-key", "slots": 3}
        with mock.patch.object(linkedin_contacts, "build_request", return_value={"verify_held": []}):
            summary = linkedin_contacts._run_one(self.root, self.database, {"timezone": "UTC"}, item, at(9), executor=crashed)
        self.assertEqual(summary["blocker"], "LINKEDIN_CONTACTS_RuntimeError")
        self.assertIn("socket closed", summary["error"])
        self.assertNotIn(KEY, summary["error"])
        self.assertTrue(summary["raised_at"].startswith("test_failure_reasons.py:"))

    def test_report_a_problem_still_sends_codes_only(self):
        task(self.database, "t3", "message_writer", "RETRY_WAIT", at(9, 5), "SUBSCRIPTION_EXECUTOR_FAILED", next_at=at(11),
             events=[("RETRY_WAIT", detail("SUBSCRIPTION_EXECUTOR_FAILED", error="secret detail text"), at(9))])
        self.database.commit()
        codes = report_problem.failure_codes(self.database, pages.CONFIG, pages.NOT_RUNNING)
        self.assertEqual(codes, ["RETRY:message_writer"])
        self.assertNotIn("secret detail text", report_problem.summary("1", "mac", [], codes))

    def test_run_view_shows_a_runs_records_files_and_tagged_lines(self):
        logs = self.root / "logs"
        logs.mkdir()
        task(self.database, "t4abcdefghijklm", "message_writer", "RETRY_WAIT", at(9, 5), "X", next_at=at(11),
             events=[("RETRY_WAIT", detail("SUBSCRIPTION_EXECUTOR_FAILED", error="model refused " + KEY), at(9))])
        (logs / "launchd.stderr.log").write_text("2026-10-22T16:00:00Z WARNING run [task:t4abcdefghij] writer: it broke\n"
                                                 "2026-10-22T16:00:01Z WARNING run other: unrelated\n")
        research_id = "0a1b2c3d-0000-4000-8000-00000000abcd"
        (logs / (research_id + "." + parallel_research._label("Fusion") + ".1.discovery.log")).write_text(
            "OpenAI Codex v0.1\nmodel: m\nuser\nCANDIDATE RESUME: Jane Doe, 12 Main St\nthinking\nprivate working\n"
            "ERROR: unexpected status 402 credits\n")
        self.database.commit()
        reader = failures_page.overview.Reader(self.database)
        secrets = failures_page.Secrets([KEY])
        view = failures_page.run_view(reader, self.root, logs, "task:t4abcdefghijklm", secrets)
        self.assertIn("SUBSCRIPTION_EXECUTOR_FAILED", view["records"][0]["text"])
        self.assertNotIn(KEY, json.dumps(view, default=str))
        self.assertEqual(len(view["tagged"]), 1)
        self.assertIn("it broke", view["tagged"][0])
        research = failures_page.run_view(reader, self.root, logs, "research:" + research_id, secrets)
        [lines] = [entry["lines"] for entry in research["files"]]
        self.assertEqual(lines, ["OpenAI Codex v0.1", "model: m", failures_page.PROMPT_WITHHELD,
                                 "ERROR: unexpected status 402 credits"])   # no prompt echo, no transcript
        self.assertIsNone(failures_page.run_view(reader, self.root, logs, "research:a1", secrets))   # not a run id
        self.assertIsNone(failures_page.run_view(reader, self.root, logs, "research:%s:../x" % research_id, secrets))
        self.assertIsNone(failures_page.run_view(reader, self.root, logs, "dir:../etc/x", secrets))
        self.assertIsNone(failures_page.run_view(reader, self.root, logs, "bogus:1", secrets))
        listed = failures_page.runs_list(self.root, logs)
        self.assertEqual([entry["href"] for entry in listed if entry["component"] == "research"],
                         [failures_page.run_href("research", research_id)])   # the Runs list opens this run

    def test_a_claimed_task_tags_its_own_lines_inside_its_step(self):
        import workflow_tasks
        workflow_tasks.initialize(self.database)
        with self.database:
            child = workflow_tasks.enqueue(self.database, "message_writer", "c1", "step", [{"source": "s", "observed_at": "x"}],
                                           "2026-12-01T00:00:00+00:00", "2026-10-22T16:00:00+00:00", max_attempts=3)
        stream = io.StringIO()
        logger = log.get("test_failure_reasons")
        with contextlib.redirect_stderr(stream), log.tagged("task", "stepstepstepstep"):
            claim = workflow_tasks.claim(self.database, child, "2026-10-22T16:00:00+00:00")
            logger.warning("in the child")
            with self.database:
                workflow_tasks.finish(self.database, child, claim["token"], "2026-10-22T16:00:01+00:00", {},
                                      [{"source": "s", "observed_at": "x"}])
            logger.warning("back in the step")
        lines = stream.getvalue().splitlines()
        self.assertIn("[%s]" % log.run_tag("task", child), lines[0])
        self.assertIn("[task:stepstepstep]", lines[1])

    def test_leaving_a_task_restores_the_blocks_own_tag(self):
        with log.tagged("task", "stepstepstepstep"):
            log.enter_task("aaaaaaaaaaaaaaaa")
            log.enter_task("bbbbbbbbbbbbbbbb")   # A ended without leaving (a raise), B claimed
            log.leave_task("bbbbbbbbbbbbbbbb")
            self.assertEqual(log._tag.get(), "task:stepstepstep")   # never A's
            log.enter_task("cccccccccccccccc")    # ends without leaving
        self.assertIsNone(log._tag.get())          # the block's end restores the outer value
        log.enter_task("dddddddddddddddd")         # outside any block: nothing is tagged
        self.assertIsNone(log._tag.get())

    def test_inside_the_prompt_echo_only_codex_levels_are_kept(self):
        text = ("OpenAI Codex v0.1\nuser\nError: never state a salary\nexit early if blocked\nERROR: echo-level\n"
                "exec\nError: unexpected argument\n")
        self.assertEqual(failures_page.codex_safe(text, truncated=False).splitlines(),
                         ["OpenAI Codex v0.1", failures_page.PROMPT_WITHHELD, "ERROR: echo-level", "Error: unexpected argument"])
        self.assertEqual(failures_page.codex_safe("Error: the user's own words\nERROR: real\n", truncated=True).splitlines(),
                         [failures_page.PROMPT_WITHHELD, "ERROR: real"])

    def test_a_trace_shows_only_its_error_events(self):
        events = [{"type": "item.completed", "item": {"type": "reasoning", "text": "the candidate Jane lives at 12 Main"}},
                  {"type": "item.completed", "item": {"type": "agent_message", "text": "Filled phone 555-0100"}},
                  {"type": "item.completed", "item": {"type": "mcp_tool_call", "arguments": {"value": "Jane Doe"},
                                                      "status": "completed"}},
                  {"type": "item.completed", "item": {"type": "command_execution", "status": "failed",
                                                      "aggregated_output": "Jane Doe resume"}},
                  {"type": "error", "message": "stream disconnected"},
                  {"type": "turn.failed", "error": {"message": "402 Payment Required"}}]
        shown = failures_page.trace_errors("\n".join(json.dumps(event) for event in events))
        self.assertEqual(shown.splitlines(), [failures_page.TRACE_NOTE, "item.completed command_execution: failed (command_execution)",
                                              "error: stream disconnected", "turn.failed: 402 Payment Required"])
        self.assertNotIn("Jane", shown)

    def test_codex_own_error_lines_survive_but_not_the_transcript(self):
        text = ("OpenAI Codex v0.1\nuser\nprompt line\nthinking\nError is something I reason about\ncodex\nanswer\n"
                "exec\nError: unexpected argument '--foo'\nfatal: cannot read config\n")
        kept = failures_page.codex_safe(text, truncated=False).splitlines()
        self.assertEqual(kept, ["OpenAI Codex v0.1", failures_page.PROMPT_WITHHELD, "Error: unexpected argument '--foo'",
                                "fatal: cannot read config"])

    def test_a_tagged_block_tags_its_lines(self):
        stream = io.StringIO()
        with contextlib.redirect_stderr(stream):
            logger = log.get("test_failure_reasons")
            with log.tagged("task", "0123456789abcdef"):
                logger.warning("inside")
            logger.warning("outside")
        lines = stream.getvalue().splitlines()
        self.assertIn("[task:0123456789ab] test_failure_reasons: inside", lines[0])
        self.assertNotIn("[task:", lines[1])


@unittest.skipUnless(pages.DEPENDENCIES, "dashboard packages (fastapi, httpx) are not installed")
class Page(unittest.TestCase):
    setUp = pages.FailuresPageTest.setUp
    tearDown = pages.FailuresPageTest.tearDown
    get = pages.FailuresPageTest.get

    def test_reasons_and_a_run_view_render_on_the_page(self):
        database = base.database_with_schema(self.root)
        now = failures_page.dt.datetime.now(failures_page.dt.timezone.utc)
        task(database, "t5", "message_writer", "RETRY_WAIT", now, "SUBSCRIPTION_EXECUTOR_FAILED",
             next_at=now + failures_page.dt.timedelta(hours=1),
             events=[("RETRY_WAIT", detail("SUBSCRIPTION_EXECUTOR_FAILED", error="the model said no"), now)])
        database.commit()
        database.close()
        page = pages.text_of(self.get().text)
        self.assertIn("The AI call failed. SUBSCRIPTION_EXECUTOR_FAILED the model said no", page)
        self.assertIn("See its log", page)
        run = self.get(run="task:t5")
        self.assertEqual(run.status_code, 200)
        self.assertIn('id="run"', run.text)
        self.assertIn("the model said no", pages.text_of(run.text))
        self.assertIn("Runs", pages.text_of(run.text))
        self.assertIn("Nothing is recorded for this run any more.", pages.text_of(self.get(run="task:nope").text))
        self.assertIn("Sidedoor has no records for this run", pages.text_of(self.get(run="bogus:x").text))

    def test_only_the_codex_not_found_banner_while_codex_is_missing(self):
        import provider_limits
        self.assertIn("Codex stopped working.", pages.text_of(self.get("/").text))   # this home's Codex step is not done
        (self.home / provider_limits.CODEX_WAIT_FILE).write_text(json.dumps({"observed_at": "2026-10-23T00:00:00+00:00"}))
        page = pages.text_of(self.get("/").text).replace("&#39;", "'")
        self.assertNotIn("Codex stopped working.", page)
        self.assertIn(provider_limits.CODEX_WORDS_FINISHED, page)


if __name__ == "__main__":
    unittest.main()
