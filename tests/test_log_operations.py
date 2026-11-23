"""Logs grouped by operation (owner L1, L2, L3, 2026-11-22): one row per thing Sidedoor did, named from the
record that says what it was about, one Download per row, a search box and each card's Logs link."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import io
import re
import json
import os
import unittest
import zipfile
from unittest import mock

import desktop_adapter
import failures_page
import log_downloads
import test_dashboard_applications as apps
import test_dashboard_failures as pages
import test_dashboard_overview as base

ATTEMPT = "0b55aa11-2222-4333-8444-555566667777"
LABEL = "cloud-services-89bd8cfde0"
KEY = pages.KEY


class LogOperationsTest(unittest.TestCase):
    get = pages.FailuresPageTest.get
    tearDown = pages.FailuresPageTest.tearDown

    def setUp(self):
        pages.FailuresPageTest.setUp(self)
        (self.home / "openrouter.key").write_text(KEY + "\n")
        self.logs, self.reports = self.root / "logs", self.root / "reports"
        self.logs.mkdir()
        self.reports.mkdir()
        self.database = base.database_with_schema(self.root)
        desktop_adapter.initialize(self.database)
        self.job("job-1", "Freestyle", "Infrastructure Engineer")
        self.job("job-2", "Northwind", "Platform Engineer")

    def job(self, key, company, role):
        self.database.execute("INSERT INTO application_jobs(job_key,employer,requisition,payload,state,updated_at) "
                              "VALUES (?,?,?,?,?,?)", (key, company, "R", json.dumps({"company": company, "role": role}),
                                                       "QUEUED", "2026-11-22T18:00:00Z"))

    def folder(self, base_name, session, files):
        folder = self.reports / base_name / session
        folder.mkdir(parents=True)
        for name, text in files.items():
            (folder / name).parent.mkdir(parents=True, exist_ok=True)
            (folder / name).write_text(text)
        return folder

    def rows(self, **filters):
        self.database.commit()
        reader = log_downloads.overview.Reader(self.database)
        return log_downloads.operations_paged(reader, self.root, self.logs, **filters)[0]

    def labelled(self, **filters):
        return {row["label"]: row for row in self.rows(**filters)}

    def send_try(self, session="request-miuvgq5y", request="r-5", method="WEB"):
        for number in range(1, 5):   # four earlier tries: this one is try 5
            self.database.execute("INSERT INTO desktop_requests(request_id,binding,job_key,operation,payload,state,created_at,"
                                  "expires_at,attempt_id,result) VALUES (?,?,?,?,?,?,?,?,?,?)",
                                  ("r-%d" % number, "b", "job-1", "submit", "{}", "CONSUMED", "2026-11-22T0%d:00:00Z" % number,
                                   "2026-11-23T00:00:00Z", "a-%d" % number, "{}"))
        self.database.execute("INSERT INTO desktop_requests(request_id,binding,job_key,operation,payload,state,created_at,"
                              "expires_at,attempt_id,result) VALUES (?,?,?,?,?,?,?,?,?,?)",
                              (request, "b", "job-1", "submit", "{}", "CONSUMED", "2026-11-22T20:30:00Z", "2026-11-23T00:00:00Z",
                               "a-5", json.dumps({"blocked": {"code": "AUTHENTICATION_REQUIRED"}, "status": "UNKNOWN"})))
        self.database.execute("INSERT INTO application_attempts(attempt_id,job_key,idempotency_key,state,started_at,evidence) "
                              "VALUES (?,?,?,?,?,?)", ("a-5", "job-1", "i", "CONFIRMED_NOT_SUBMITTED", "2026-11-22T20:30:00Z",
                                                       json.dumps({"review": {"application_method": method}})))
        return self.folder("application-browser", session, {
            "summary.json": json.dumps({"request_id": request}), "trace.jsonl": '{"step": 1}\n',
            "stderr.log": "key " + KEY + "\n", "browser/console-1.log": "page\n"})

    def test_an_application_session_is_one_row_named_by_its_job_try_and_outcome(self):
        self.send_try()
        row = self.labelled()["Application · Freestyle, Infrastructure Engineer · send try 5"]
        self.assertEqual(row["outcome"], "Not sent: the application site asked for a sign-in")
        self.assertEqual(len(row["files"]), 4)
        self.assertNotIn("miuvgq5y", row["label"])

    def test_an_email_application_stopped_at_sign_in_names_the_mailbox(self):
        self.send_try(method="EMAIL")   # decided from the attempt's saved review, not from text
        row = self.labelled()["Application · Freestyle, Infrastructure Engineer · send try 5"]
        self.assertEqual(row["outcome"], "Not sent: your mailbox needs a sign-in")

    def test_research_stages_are_separate_rows_named_by_industry(self):
        self.database.execute("INSERT INTO research(id,started_at,state,result) VALUES (?,?,?,?)", (
            ATTEMPT, "2026-11-22T17:00:00Z", "COMPLETE",
            json.dumps({"industries": [{"industry": "cloud services", "state": "COMPLETE"}]})))
        for name in ("%s.%s.0.discovery.log", "%s.%s.0.discovery.log.engine.json", "%s.%s.1.qualification.log"):
            (self.logs / (name % (ATTEMPT, LABEL))).write_text("line\n")
        for name in ("%s.%s.0.discovery.json", "%s.%s.1.qualification.json", "%s.%s.industry.json", "%s.parallel.json"):
            (self.reports / (name.replace("%s.%s", "%s.%s" if "%s.%s" in name else "%s") % (
                (ATTEMPT, LABEL) if name.count("%s") == 2 else (ATTEMPT,)))).write_text("{}\n")
        (self.reports / ("parallel-industry-2026-11-22-%s.scope-ab12.json" % LABEL)).write_text("{}\n")
        rows = self.labelled()
        stage_1 = rows["Research · Cloud services · stage 1, search"]
        self.assertIsNone(stage_1["outcome"])   # L7: the industry's result shows on its last stage only
        self.assertEqual(rows["Research · Cloud services · stage 2, company check"]["outcome"], "Finished")
        self.assertEqual(len(stage_1["files"]), 4)   # its log, run details, results and the industry's progress
        self.assertEqual(len(rows["Research · Cloud services · stage 2, company check"]["files"]), 2)
        self.assertIn("Research · whole run, 1 industry", rows)
        self.assertIn("Saved progress · Cloud services, Sun Nov 22", rows)

    def test_posting_checks_and_recovery_decisions_name_what_they_judged(self):
        self.folder("posting-judge", "decision-_2t8ce93", {
            "prompt.txt": 'Decide...\n\n{"evidence_id": "e", "job_key": "job-2", "job": {}}',
            "decision.json": json.dumps({"verdict": "DIFFERENT"})})
        self.folder("research-recovery", "decision-3c8vm37i", {
            "prompt.txt": 'Rules\n{"decision_number": 2, "failure": {"action_id": "%s:cloud services"}}' % ATTEMPT,
            "decision.json": json.dumps({"action": "ABANDON"})})
        decision = self.folder("application-recovery", "decision-0eeagsqr", {"decision.json": json.dumps({"action": "RETRY"})})
        self.database.execute("INSERT INTO application_events(job_key,event,detail,at) VALUES (?,?,?,?)",
                              ("job-1", "APPLICATION_RECOVERY_DECIDED", json.dumps({"artifacts": str(decision)}),
                               "2026-11-22T18:00:00Z"))
        rows = self.labelled()
        self.assertEqual(rows["Posting check · Northwind, Platform Engineer"]["outcome"], "Different posting")
        self.assertEqual(rows["Research recovery · Cloud services · decision 2"]["outcome"], "Stop trying")
        self.assertEqual(rows["Application · Recovery decision · Freestyle, Infrastructure Engineer"]["outcome"], "Try again")

    def test_contact_finding_streams_and_unnamed_folders(self):
        run = self.folder("linkedin-contacts", "run-0d_htguz", {"trace.jsonl": "{}\n", "output.json": "{}\n"})
        self.database.execute("CREATE TABLE IF NOT EXISTS linkedin_contact_runs (run_id TEXT PRIMARY KEY, company TEXT NOT NULL, "
                              "started_at TEXT NOT NULL, state TEXT NOT NULL, request_sha256 TEXT NOT NULL, plan_id TEXT, "
                              "finished_at TEXT, result TEXT)")
        self.database.execute("INSERT INTO linkedin_contact_runs(run_id,company,started_at,state,request_sha256,result) "
                              "VALUES (?,?,?,?,?,?)", ("run-1", "Beam", "2026-11-22T18:00:00Z", "COMPLETE", "s",
                                                       json.dumps({"company": "Beam", "directory": str(run)})))
        self.folder("email-check", "check-m90uih5_", {"check.json": json.dumps({"refused": None})})
        (self.logs / "research.stderr.log").write_text("now\n")
        (self.logs / ("research.stderr.log.retained.%s.%s" % ("1" * 20, "a" * 32))).write_text("before\n")
        rows = self.labelled()
        self.assertEqual(rows["Contact finding · Beam"]["outcome"], "Finished")
        self.assertIn("Application · Email check", rows)
        self.assertEqual(len(rows["Research errors"]["files"]), 2)   # the stream and its earlier copy

    def test_search_and_the_job_filter_match_company_role_or_person(self):
        self.send_try()
        self.folder("posting-judge", "decision-x1", {"prompt.txt": '{"job_key": "job-2"}', "decision.json": "{}"})
        self.assertEqual([row["about"] for row in self.rows(find="northwind")], ["Northwind, Platform Engineer"])
        self.assertEqual([row["about"] for row in self.rows(find="infrastructure engineer")],
                         ["Freestyle, Infrastructure Engineer"])
        self.assertEqual([row["job"] for row in self.rows(job="job-2")], ["job-2"])
        self.assertEqual(self.rows(find="nobody"), [])

    def test_the_page_lists_operations_and_a_row_downloads_as_one_redacted_zip(self):
        self.send_try()
        self.database.commit()
        with mock.patch.object(failures_page, "_redaction_complete", return_value=True):
            page = self.searched("freestyle")
            self.assertIn("Application · Freestyle, Infrastructure Engineer · send try 5", page.text)
            self.assertIn("/failures/operation.zip?id=dir%3Aapplication-browser/request-miuvgq5y", page.text)
            filtered = self.get("/failures", job="job-1")
            self.assertIn("Showing only Freestyle, Infrastructure Engineer", pages.text_of(filtered.text))
            response = self.get("/failures/operation.zip", id="dir:application-browser/request-miuvgq5y")
            refused = self.get("/failures/operation.zip", id="dir:application-browser/../../home")
        self.assertEqual(response.status_code, 200)
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        self.assertEqual(sorted(archive.namelist()), sorted(
            ["index.txt"] + ["reports/application-browser/request-miuvgq5y/" + name
                             for name in ("summary.json", "trace.jsonl", "stderr.log", "browser/console-1.log")]))
        self.assertNotIn(KEY, archive.read("reports/application-browser/request-miuvgq5y/stderr.log").decode())
        self.assertEqual(refused.status_code, 404)

    def searched(self, words):
        """The list after typing `words` in the search box (one app, as a browser keeps it)."""
        app = pages.dashboard.create_app(base.SECRET, base.PORT, home=self.home, root=self.root, config=pages.CONFIG)
        client = pages.TestClient(app, base_url=f"http://127.0.0.1:{base.PORT}", follow_redirects=False)
        client.get("/", params={"t": base.SECRET})
        with mock.patch.object(pages.setup_steps, "chrome_path", return_value="/chrome"):
            sent = client.post("/failures/find", data={"find": words}, headers={"origin": f"http://127.0.0.1:{base.PORT}"})
            self.assertEqual(sent.status_code, 303)
            self.assertNotIn(words, sent.headers["location"])
            return client.get(sent.headers["location"])

    def test_the_search_never_puts_the_words_in_an_address(self):
        self.send_try()
        for number in range(40):   # enough rows for a second page, so the pager's links are checked too
            self.folder("posting-judge", "decision-%02d" % number, {"prompt.txt": 'Rules\n\n{"job_key": "job-1"}'})
        self.database.commit()
        with mock.patch.object(failures_page, "_redaction_complete", return_value=True):
            page = self.searched("Freestyle")
            self.assertIn("Posting check · Freestyle", page.text)
            self.assertIn("log_files_page=2", page.text)   # the pager is on the page and carries only the token
            links = re.findall(r'(?:href|action|hx-get|hx-post)="([^"]*)"', page.text)
            self.assertTrue(links)
            self.assertFalse([link for link in links if "freestyle" in link.lower() or "find=" in link])
            self.assertIn('value="Freestyle"', page.text)   # still shown in the box itself

    def test_a_posting_check_names_the_new_listing_not_an_existing_job(self):
        packet = {"job_key": "job-2", "new_listing": {"company": "Northwind"},
                  "existing_jobs": [{"job_key": "job-1", "company": "Freestyle"}]}
        self.folder("posting-judge", "decision-0kzctzlf", {   # posting_judge.same_job_prompt's real shape
            "prompt.txt": "A job-search tool found the NEW listing below.\n\n" + json.dumps(packet, sort_keys=True),
            "decision.json": json.dumps({"verdict": "SAME"})})
        self.assertEqual([row["job"] for row in self.rows()], ["job-2"])

    def test_contact_finding_reads_person_names_stored_as_plain_text(self):
        run = self.folder("linkedin-contacts", "run-1", {"output.json": "{}\n"})
        self.database.execute("CREATE TABLE IF NOT EXISTS linkedin_contact_runs (run_id TEXT PRIMARY KEY, company TEXT NOT NULL, "
                              "started_at TEXT NOT NULL, state TEXT NOT NULL, request_sha256 TEXT NOT NULL, plan_id TEXT, "
                              "finished_at TEXT, result TEXT)")
        self.database.execute("INSERT INTO linkedin_contact_runs(run_id,company,started_at,state,request_sha256,result) "
                              "VALUES (?,?,?,?,?,?)", ("run-1", "Modal Labs", "2026-11-22T18:00:00Z", "COMPLETE", "s", json.dumps(
                                  {"company": "Modal Labs", "directory": str(run),
                                   "people": [{"recipient": "Jordan Pike", "profile": "x"}]})))   # the stored shape: text
        self.assertEqual([row["label"] for row in self.rows(find="jordan pike")], ["Contact finding · Modal Labs"])

    def messages(self):
        for statement in ("CREATE TABLE IF NOT EXISTS outreach_agent_audit (id TEXT PRIMARY KEY, action_id TEXT NOT NULL, "
                          "phase TEXT NOT NULL, kind TEXT NOT NULL, observed_at TEXT NOT NULL, record TEXT NOT NULL)",
                          "CREATE TABLE IF NOT EXISTS reply_worker_attempts (task_id TEXT NOT NULL, attempt INTEGER NOT NULL, "
                          "observed_at TEXT NOT NULL, input_sha256 TEXT, output TEXT, output_sha256 TEXT, provenance TEXT "
                          "NOT NULL, blocker TEXT, PRIMARY KEY(task_id,attempt))"):
            self.database.execute(statement)
        for action, purpose, state, person in (("a-inv", "INVITE", "CONFIRMED_SENT", "Avery Quill"),
                                               ("a-fol", "FOLLOWUP", "CANCELLED_BEFORE_SEND", "Avery Quill"),
                                               ("a-rep", "REPLY", "CONFIRMED_SENT", "Sam Okoro")):
            identity = "linkedin.com/in/" + person.split()[0].lower()
            self.database.execute("INSERT INTO outreach_actions(action_id,identity,slot,payload,payload_hash,state,claimed_at) "
                                  "VALUES (?,?,?,?,?,?,?)", (action, identity, purpose + ":" + action, json.dumps(
                                      {"purpose": purpose, "company": "Northwind", "recipient": person, "message": "key " + KEY}),
                                      "h", state, "2026-11-22T18:00:00Z"))
        self.database.execute("INSERT INTO outreach_agent_audit VALUES (?,?,?,?,?,?)",
                              ("x1", "a-inv", "INSPECTION", "DECISION", "2026-11-22T18:01:00Z", json.dumps({"verdict": "ok"})))
        self.database.execute("INSERT INTO engagement_events(source,source_id,identity,kind,occurred_at,observed_at,raw,payload) "
                              "VALUES (?,?,?,?,?,?,?,?)", ("inbox", "m-1", "linkedin.com/in/sam", "inbound",
                                                           "2026-11-22T18:00:00Z", "2026-11-22T18:00:00Z", "{}", "{}"))
        self.database.execute("INSERT INTO workflow_tasks(task_id,worker,action_id,parent_run_id,requirement_revision,inputs,"
                              "deadline,status,max_attempts,owner,effect,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                              ("t-1", "pipeline_routing_review", "act", "run", "r",
                               json.dumps([{"source": 'engagement_events:["inbox","m-1"]'}]), "2026-11-23T00:00:00Z",
                               "DONE", 3, "o", "READ", "2026-11-22T18:00:00Z"))
        self.database.execute("INSERT INTO reply_worker_attempts(task_id,attempt,observed_at,output,provenance) VALUES (?,?,?,?,?)",
                              ("t-1", 1, "2026-11-22T18:02:00Z", json.dumps({"label": "QUESTION"}), "{}"))

    def test_messages_follow_ups_replies_and_reply_reads_are_rows_from_their_records(self):
        self.messages()
        rows = self.labelled()
        self.assertEqual(rows["Invitation · Northwind, Avery Quill"]["outcome"], "Sent")
        self.assertEqual(rows["Follow-up · Northwind, Avery Quill"]["outcome"], "Cancelled before sending")
        self.assertEqual(rows["Reply · Northwind, Sam Okoro"]["outcome"], "Sent")
        self.assertEqual(rows["Reply read · Northwind, Sam Okoro"]["outcome"], "Asked a question")
        contact = log_downloads._tag("contact", "linkedin.com/in/avery")   # networking_page's card id for them
        self.assertEqual(sorted(row["label"] for row in self.rows(about=contact)),
                         ["Follow-up · Northwind, Avery Quill", "Invitation · Northwind, Avery Quill"])
        message = log_downloads._tag("message", "a-inv")                     # approvals_page.dom("message", ...)
        self.assertEqual([row["label"] for row in self.rows(about=message)], ["Invitation · Northwind, Avery Quill"])
        self.database.commit()
        with mock.patch.object(failures_page, "_redaction_complete", return_value=True):
            response = self.get("/failures/operation.zip", id="msg:a-inv")
            page = self.get("/failures", about=message)
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        records = archive.read("records.json").decode()
        self.assertIn('"verdict": "ok"', records)
        self.assertNotIn(KEY, records)
        self.assertIn("Showing only Northwind, Avery Quill", pages.text_of(page.text))

    def test_email_checks_name_the_mailbox_and_files_have_plain_names(self):
        self.folder("email-check", "check-1", {"check.json": json.dumps({"signed_in_account": "me@example.com", "refused": None})})
        (self.reports / "outreach-pending-owner-approval.json").write_text("{}\n")
        (self.reports / "linkedin-contacts").mkdir(exist_ok=True)
        (self.reports / "linkedin-contacts" / "pending-owner-approval.json").write_text("{}\n")
        rows = self.labelled()
        self.assertIn("Application · Email check · me@example.com", rows)
        self.assertIn("Messages waiting for your approval", rows)
        self.assertIn("Contacts waiting for your approval", rows)
        entry = dict(rows["Application · Email check · me@example.com"], label="Application · " + "Averyveryverylongcompanyname " * 4)
        name, body = log_downloads.operation_zip(self.root, self.logs, [], entry)
        body.close()
        self.assertRegex(name, r"^sidedoor-application(-averyveryverylongcompanyname)+-\d{4}-\d{2}-\d{2}\.zip$")

    def test_an_ended_search_says_so_and_links_drop_it(self):
        self.send_try()
        self.database.commit()
        with mock.patch.object(failures_page, "_redaction_complete", return_value=True):
            page = self.get("/failures", search="gone-token")   # a fresh app: the token is unknown
        self.assertIn("That search has ended. Search again.", page.text)
        self.assertIn("send try 5", page.text)
        self.assertNotIn("gone-token", page.text)

    def test_a_reply_read_that_failed_says_it_could_not_read(self):
        self.messages()
        self.database.execute("INSERT INTO reply_worker_attempts(task_id,attempt,observed_at,provenance,blocker) VALUES (?,?,?,?,?)",
                              ("t-1", 2, "2026-11-22T19:00:00Z", "{}", "CLASSIFICATION_INPUT_EXCEEDS_LIMIT"))
        self.assertEqual([row["outcome"] for row in self.rows(find="reply read")], ["Could not read", "Asked a question"])

    def test_message_records_leave_out_account_ids_and_stay_valid_json(self):
        self.messages()
        self.database.execute("INSERT INTO outreach_agent_audit VALUES (?,?,?,?,?,?)", (
            "x2", "a-inv", "INSPECTION", "OBSERVATION", "2026-11-22T18:02:00Z",
            json.dumps({"account_id": "ACC123", "auth": "AUTH-LABEL", "sender": {"sender_id": 42}, "note": "key " + KEY,
                        "nested": [{"provider_id": "PRV9", "kept": "yes"}]})))
        self.database.commit()
        with mock.patch.object(failures_page, "_redaction_complete", return_value=True):
            response = self.get("/failures/operation.zip", id="msg:a-inv")
        text = zipfile.ZipFile(io.BytesIO(response.content)).read("records.json").decode()
        for gone in ("ACC123", "AUTH-LABEL", "PRV9", '"sender_id"', '"auth"', KEY):
            self.assertNotIn(gone, text)
        value = json.loads(text)   # still valid JSON after redaction
        self.assertEqual(value["steps"][1]["record"]["nested"], [{"kept": "yes"}])

    def test_an_application_card_links_to_its_logs(self):
        self.database.close()
        apps_test = apps.ApplicationsPageTest("test_fresh_install_says_why_and_what_next")
        apps_test.root, apps_test.home = self.root, self.home
        database = base.database_with_schema(self.root)
        apps.job(database, "acme-1", "QUEUED")
        database.commit()
        database.close()
        self.assertIn('href="/failures?job=acme-1#files-title">Logs</a>', apps_test.get().text)


if __name__ == "__main__":
    unittest.main()
