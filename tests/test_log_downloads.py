"""Whole log and report downloads (owner L1 and L2, 2026-11-18): the Files list, a file's download, the
support .zip and a failure card's Download details."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import datetime as dt
import io
import json
import os
import re
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import failures_page
import log_downloads
import overview_page
import test_dashboard_failures as pages
import test_dashboard_overview as base

ATTEMPT = "0b55aa11-2222-4333-8444-555566667777"
LABEL = "automotive-eb589cfb23"
KEY = pages.KEY
LOG_NAME = "%s.%s.1.discovery.log" % (ATTEMPT, LABEL)
REPORT_NAME = "%s.%s.0.discovery.json" % (ATTEMPT, LABEL)


class LogDownloadsTest(unittest.TestCase):
    get = pages.FailuresPageTest.get
    tearDown = pages.FailuresPageTest.tearDown

    def setUp(self):
        pages.FailuresPageTest.setUp(self)
        (self.home / "openrouter.key").write_text(KEY + "\n")
        self.logs, self.reports = self.root / "logs", self.root / "reports"
        self.logs.mkdir()
        self.reports.mkdir()
        (self.logs / LOG_NAME).write_text("start\nkey " + KEY + "\nTimeoutExpired\n")
        (self.reports / REPORT_NAME).write_text('{"companies": []}\n')
        (self.reports / ("%s.%s.industry.json" % (ATTEMPT, LABEL))).write_text('{"state": "FAILED"}\n')
        (self.logs / "research.stderr.log").write_text("FAILED research\n")
        old = self.reports / "old.json"
        old.write_text("{}\n")
        stamp = (dt.datetime.now() - dt.timedelta(days=5)).timestamp()
        os.utime(old, (stamp, stamp))

    def unlocked(self, path, **params):
        with mock.patch.object(failures_page, "_redaction_complete", return_value=True):
            return self.get(path, **params)

    def test_files_are_listed_with_readable_names_and_filtered(self):
        files = log_downloads.all_files(self.root, self.logs)
        by_name = {entry["name"]: entry for entry in files}
        self.assertEqual(by_name[LOG_NAME]["label"], "Research, Automotive, search 1, " + by_name[LOG_NAME]["when"])
        self.assertEqual((by_name[LOG_NAME]["component_label"], by_name[LOG_NAME]["level_label"]), ("Research", "Output"))
        self.assertEqual(by_name["research.stderr.log"]["level_label"], "Errors")
        self.assertTrue(by_name[REPORT_NAME]["label"].startswith("Research, Automotive, search 0 results, "))
        errors, _ = log_downloads.files_paged(self.root, self.logs, level="stderr")
        self.assertEqual([entry["name"] for entry in errors], ["research.stderr.log"])
        page = self.unlocked("/failures", component="research")
        self.assertIn("Research, Automotive, search 1", page.text)
        self.assertNotIn("Old", pages.text_of(page.text).split("Files", 1)[1].split("Runs", 1)[0])

    def test_session_names_show_the_job_never_a_raw_id(self):
        database = base.database_with_schema(self.root)
        import desktop_adapter
        desktop_adapter.initialize(database)
        named = self.reports / "application-browser" / "request-k2x9q7"
        unnamed = self.reports / "application-browser" / "request-3f9a2b7c"
        for folder in (named, unnamed):
            folder.mkdir(parents=True)
            (folder / "stderr.log").write_text("page did not load\n")
        database.execute("INSERT INTO application_jobs(job_key,employer,requisition,payload,state,updated_at) VALUES (?,?,?,?,?,?)",
                         ("job-1", "Acme", "R1", json.dumps({"company": "Acme Motors", "role": "Data Analyst"}), "FAILED",
                          "2026-11-18T18:00:00Z"))
        database.execute("INSERT INTO desktop_requests(request_id,binding,job_key,operation,payload,state,created_at,expires_at,"
                         "result) VALUES (?,?,?,?,?,?,?,?,?)",
                         ("r1", "b", "job-1", "submit", "{}", "FAILED", "2026-11-18T18:00:00Z", "2026-11-18T19:00:00Z",
                          json.dumps({"directory": str(named)})))
        database.commit()
        database.close()
        page = pages.text_of(self.unlocked("/failures").text)
        self.assertRegex(page, r"Browser session, Acme Motors, Data Analyst, errors, \w{3} \d{1,2} \d{2}:\d{2}")
        self.assertRegex(page, r"Browser session, errors, \w{3} \d{1,2} \d{2}:\d{2}")
        files = page.split("Files", 1)[1].split("Runs", 1)[0]
        for raw in ("k2x9q7", "3f9a2b7c", ATTEMPT, "eb589cfb23"):
            self.assertNotIn(raw, files)

    def test_a_file_downloads_whole_with_secrets_removed(self):
        response = self.unlocked("/failures/file", id="logs/" + LOG_NAME)
        self.assertEqual(response.status_code, 200)
        self.assertIn('filename="%s"' % LOG_NAME, response.headers["content-disposition"])
        self.assertNotIn(KEY, response.text)
        self.assertIn("TimeoutExpired", response.text)
        self.assertIn("start", response.text)

    def test_only_files_inside_logs_and_reports_are_served(self):
        outside = self.root / "outside.log"
        outside.write_text("private\n")
        (self.logs / "linked.log").symlink_to(outside)
        (self.reports / "folder").symlink_to(self.root, target_is_directory=True)
        for name in ("logs/linked.log", "logs/../outside.log", "../outside.log", str(outside), "reports/folder/outside.log",
                     "state/orchestrator.sqlite3", "logs/missing.log"):
            self.assertIsNone(log_downloads.resolve(self.root, self.logs, name), name)
            response = self.unlocked("/failures/file", id=name)
            self.assertEqual(response.status_code, 404, name)
            self.assertNotIn("private", response.text)
        self.assertNotIn("linked.log", [entry["name"] for entry in log_downloads.all_files(self.root, self.logs)])

    def test_support_zip_has_the_last_two_days_and_an_index(self):
        response = self.unlocked("/failures/support.zip")
        self.assertEqual(response.status_code, 200)
        self.assertIn("sidedoor-support-", response.headers["content-disposition"])
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        names = set(archive.namelist())
        self.assertTrue({"index.txt", "logs/" + LOG_NAME, "reports/" + REPORT_NAME, "logs/research.stderr.log"} <= names)
        self.assertNotIn("reports/old.json", names)
        self.assertNotIn(KEY, archive.read("logs/" + LOG_NAME).decode())
        index = archive.read("index.txt").decode()
        self.assertRegex(index, r"Research, Automotive, search 1, \w{3} \d{1,2} \d{2}:\d{2} \| Research \| Output")
        self.assertEqual(archive.read("logs/research.stderr.log").decode(), "FAILED research\n")   # whole (owner X5)
        self.assertIn("logs/launchd.stderr.log | no file yet", index)

    def test_failure_card_details_zip_has_exactly_its_files(self):
        database = base.database_with_schema(self.root)
        result = {"state": "FAILED", "day": "2026-11-18", "industries": [
            {"industry": "Automotive", "state": "FAILED", "log": str(self.logs / LOG_NAME),
             "discovery_report": str(self.reports / REPORT_NAME),
             "report": str(self.reports / ("%s.%s.1.qualification.json" % (ATTEMPT, LABEL)))},
            {"industry": "Banking", "state": "COMPLETE", "log": str(self.logs / "other.log")}]}
        database.execute("INSERT INTO research VALUES (?,?,?,?)", (ATTEMPT, "2026-11-18T18:52:00+00:00", "FAILED", json.dumps(result)))
        database.commit()
        database.close()
        (self.logs / "other.log").write_text("not this one\n")
        self.assertEqual(log_downloads.details_href(failures_page.run_href("research", ATTEMPT)),
                         "/failures/details.zip?run=research%3A" + ATTEMPT)
        response = self.unlocked("/failures/details.zip", run="research:" + ATTEMPT)
        self.assertEqual(response.status_code, 200)
        self.assertIn('sidedoor-details-automotive-2026-11-18.zip', response.headers["content-disposition"])
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        self.assertEqual(set(archive.namelist()), {"index.txt", "logs/" + LOG_NAME, "reports/" + REPORT_NAME,
                                                   "reports/%s.%s.industry.json" % (ATTEMPT, LABEL)})
        self.assertNotIn(KEY, archive.read("logs/" + LOG_NAME).decode())
        self.assertIn("no longer on this computer", archive.read("index.txt").decode())

    def test_a_download_is_read_and_redacted_a_line_at_a_time(self):
        big = self.logs / "big.log"
        with big.open("w") as out:
            out.write("first key " + KEY + "\n")
            for number in range(200000):
                out.write("line %d of a long log\n" % number)
        size = big.stat().st_size
        read = []
        real = log_downloads._open_regular

        def counting(path):
            handle = real(path)
            original = handle.readline
            class Counted:
                def __iter__(self):
                    return self
                def __next__(self):
                    line = original()
                    if not line:
                        raise StopIteration
                    read.append(len(line))
                    return line
                def close(self):
                    handle.close()
            return Counted()

        with mock.patch.object(log_downloads, "_open_regular", counting):
            body = log_downloads.file_bytes(big, failures_page.secrets_of(self.home, base.CONFIG))
            first = next(body)
            self.assertNotIn(KEY.encode(), first)
            self.assertLess(sum(read), 1024)            # one line read, not the file
            rest = b"".join(body)
        self.assertEqual(sum(read), size)
        self.assertTrue(rest.endswith(b"line 199999 of a long log\n"))
        response = self.unlocked("/failures/file", id="logs/big.log")
        self.assertEqual(len(response.content.splitlines()), 200001)

    def test_nested_session_files_are_listed_and_zipped(self):
        deep = self.reports / "agent-worker" / "task-1" / "2"
        deep.mkdir(parents=True)
        (deep / "output.json").write_text('{"done": true}\n')
        session = self.reports / "application-browser" / "request-k2x9q7" / "frames" / "page-1"
        session.mkdir(parents=True)
        (session / "trace.jsonl").write_text('{"step": 1, "key": "%s"}\n' % KEY)
        ids = [entry["id"] for entry in log_downloads.all_files(self.root, self.logs)]
        self.assertIn("reports/agent-worker/task-1/2/output.json", ids)
        self.assertIn("reports/application-browser/request-k2x9q7/frames/page-1/trace.jsonl", ids)
        response = self.unlocked("/failures/details.zip", run="dir:application-browser/request-k2x9q7")
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        self.assertIn("reports/application-browser/request-k2x9q7/frames/page-1/trace.jsonl", archive.namelist())
        self.assertNotIn(KEY, archive.read("reports/application-browser/request-k2x9q7/frames/page-1/trace.jsonl").decode())
        too_deep = "reports/" + "/".join(["d"] * log_downloads.MAX_DEPTH) + "/x.log"
        self.assertIsNone(log_downloads.resolve(self.root, self.logs, too_deep))

    def test_over_the_limit_the_newest_files_are_kept(self):
        folder = self.reports / "many"
        folder.mkdir()
        for number in range(6):
            path = folder / ("a%d.json" % number)
            path.write_text("{}\n")
            os.utime(path, (1000 + number, 1000 + number))
        with mock.patch.object(log_downloads, "MAX_LISTED", 3):
            kept = sorted(parts[-1] for parts, _, _ in log_downloads._walk(folder))
        self.assertEqual(kept, ["a3.json", "a4.json", "a5.json"])

    def test_attempt_folders_are_named_so_attempts_differ(self):
        for attempt in ("1", "2"):
            folder = self.reports / "agent-worker" / "task-1" / attempt
            folder.mkdir(parents=True)
            (folder / "output.json").write_text("{}\n")
        labels = [entry["label"] for entry in log_downloads.all_files(self.root, self.logs) if "output.json" in entry["id"]]
        self.assertEqual(sorted(label.split(", ")[1] for label in labels), ["attempt 1", "attempt 2"])

    def test_a_trace_inside_a_session_subfolder_names_the_session(self):
        database = base.database_with_schema(self.root)
        import desktop_adapter
        desktop_adapter.initialize(database)
        inner = self.reports / "application-browser" / "request-k2x9q7" / "frames"
        inner.mkdir(parents=True)
        (inner / "trace.jsonl").write_text("{}\n")
        database.execute("INSERT INTO application_jobs(job_key,employer,requisition,payload,state,updated_at) VALUES (?,?,?,?,?,?)",
                         ("job-1", "Acme", "R1", json.dumps({"company": "Acme Motors", "role": "Data Analyst"}), "FAILED",
                          "2026-11-18T18:00:00Z"))
        database.execute("INSERT INTO desktop_requests(request_id,binding,job_key,operation,payload,state,created_at,expires_at,"
                         "result) VALUES (?,?,?,?,?,?,?,?,?)",
                         ("r1", "b", "job-1", "submit", "{}", "FAILED", "2026-11-18T18:00:00Z", "2026-11-18T19:00:00Z",
                          json.dumps({"error": {"trace_path": str(inner / "trace.jsonl")}})))
        database.commit()
        names = log_downloads.folder_names(overview_page.Reader(database), self.root)
        database.close()
        self.assertEqual(names, {("application-browser", "request-k2x9q7"): "Acme Motors, Data Analyst"})

    def test_a_read_that_fails_part_way_is_noted_and_the_download_ends_cleanly(self):
        real = log_downloads._open_regular

        def failing(path):
            handle = real(path)
            if path is None or path.name != LOG_NAME:
                return handle

            def lines():
                yield handle.readline()
                handle.close()
                raise OSError("disk went away")
            class Broken:
                def __init__(self):
                    self.lines = lines()
                def __iter__(self):
                    return self.lines
                def close(self):
                    handle.close()
            return Broken()

        with mock.patch.object(log_downloads, "_open_regular", failing):
            single = self.unlocked("/failures/file", id="logs/" + LOG_NAME)
            support = self.unlocked("/failures/support.zip")
        self.assertEqual(single.status_code, 200)
        self.assertEqual(single.text, "start\n" + log_downloads.UNREADABLE + "\n")
        archive = zipfile.ZipFile(io.BytesIO(support.content))
        index = archive.read("index.txt").decode()
        self.assertRegex(index, r"logs/%s \| could not be read" % re.escape(LOG_NAME))
        self.assertIn("reports/" + REPORT_NAME, archive.namelist())   # the rest of the zip is finished

    def test_downloads_refuse_while_the_keychain_is_locked(self):
        with mock.patch.object(failures_page, "_redaction_complete", return_value=False):
            for path, params in (("/failures/file", {"id": "logs/" + LOG_NAME}), ("/failures/support.zip", {}),
                                 ("/failures/details.zip", {"run": "research:" + ATTEMPT})):
                response = self.get(path, **params)
                self.assertEqual(response.status_code, 403, path)
                self.assertEqual(response.text, failures_page.DETAILS_HIDDEN)
                self.assertNotIn("attachment", response.headers.get("content-disposition", ""))


if __name__ == "__main__":
    unittest.main()
