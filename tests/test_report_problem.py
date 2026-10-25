"""Report a problem (owner pick F3 a): the privacy-safe summary, the issue address, the token-guarded
endpoint and the button on the Failures page and in Settings."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import datetime as dt
import importlib.util
import json
import shutil
import sqlite3
import tempfile
import os
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

import doctor
import failures_page
import browser_runtime
import report_problem
import setup_steps
import test_dashboard_failures as failures_tests
import test_dashboard_overview as base
import user_profile
import version

DEPENDENCIES = all(importlib.util.find_spec(name) for name in ("fastapi", "httpx"))
if DEPENDENCIES:
    from fastapi.testclient import TestClient

    import dashboard

ORIGIN = {"origin": f"http://127.0.0.1:{base.PORT}"}
# Planted values, shaped so the public export's secret scan does not take the fixtures for real ones
# (assembled at run time where a pattern would match them).
LICENSE = "SD-PLANTD7Q2W"
OPENROUTER = "sk-or-v1-planted-key-0123"
NAME, SURNAME, EMAIL = "Zelda", "Quixotic", "zelda.quixotic" + "@" + "example.org"
PHONE, LINKEDIN = "+44 20 7946 0958", "linkedin.com/" + "in/zelda-quixotic-planted"
COMPANY, ROLE, RESUME = "Acmeplanted Robotics", "Principal Widget Engineer", "Built the planted widget pipeline at"
# A small profile.json: every string in it is personal (the example profile's long notes would scrub
# most ordinary words from these tests' lines).
PROFILE = {"answers": {"full_name": {"value": NAME + " " + SURNAME}},
           "resume_raw": {"path": "resume/original.pdf", "text_path": "resume/original.txt"}}
RESUME_CONTEXT = {"text": "Experience. %s Acme in 2024, and more." % RESUME}


def planted_home(root):
    """A finished home with the example profile, planted keys and a planted personal profile form."""
    home = root / "home"
    base.finished_home(home)
    (home / "profile.json").write_text(json.dumps(PROFILE))
    (home / "license.key").write_text(LICENSE + "\n")
    (home / "openrouter.key").write_text(OPENROUTER + "\n")

    def change(state):
        state["steps"]["profile"] = {"status": "done", "form": {
            "given_name": NAME, "family_name": SURNAME, "email": EMAIL, "phone": PHONE, "linkedin": LINKEDIN,
            "current_location": "Tallahassee, Florida"}}
    setup_steps.update_state(change, home)
    return home


def line(status, name, text):
    return {"status": status, "name": name, "line": text, "todo": ""}


class UrlTest(unittest.TestCase):
    def test_issue_url_carries_only_version_and_platform(self):
        url = report_problem.issue_url("0.0.3", "macOS Apple chip")
        self.assertEqual(url, "https://github.com/yoursidedoor/sidedoor/issues/new?template=bug_report.yml"
                              "&version=0.0.3&platform=macOS+Apple+chip")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual(set(query), {"template", "version", "platform"})
        self.assertTrue(report_problem.allowed_url(url))

    def test_only_the_github_issue_form_is_allowed(self):
        for url in ("https://github.com/yoursidedoor/sidedoor/issues/new?template=bug_report.yml&version=1&platform=x&email=a",
                    "https://github.com.evil.test/yoursidedoor/sidedoor/issues/new?template=bug_report.yml&version=1&platform=x",
                    "http://github.com/yoursidedoor/sidedoor/issues/new?template=bug_report.yml&version=1&platform=x",
                    "https://github.com/other/sidedoor/issues/new?template=bug_report.yml&version=1&platform=x",
                    "javascript:alert(1)", None):
            self.assertFalse(report_problem.allowed_url(url), url)

    def test_platform_labels(self):
        self.assertEqual(report_problem.platform_label("darwin", "arm64"), "macOS Apple chip")
        self.assertEqual(report_problem.platform_label("darwin", "x86_64"), "macOS Intel")
        self.assertEqual(report_problem.platform_label("win32", "AMD64"), "Windows")
        self.assertEqual(report_problem.platform_label("linux", "x86_64"), "Other")


FAKE_USER_HOME = Path("/home/u7")   # a fixed user home: no dot, so never read as a web address


class SummaryTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.home = planted_home(self.root)

    def tearDown(self):
        self.directory.cleanup()

    def build(self):
        # A fixed user home, so the summary never depends on where the run's HOME happens to be: a
        # temporary HOME like ".../verify-export.ABCDEF/home" reads as a web address to the redactor.
        with mock.patch.object(Path, "home", return_value=FAKE_USER_HOME):
            return self._build()

    def _build(self):
        planted = [  # every kind of thing that must never reach the summary, in the checks' own lines
            {"status": "OK", "name": "Profile", "line": "Complete for %s %s (%s, %s)." % (NAME, SURNAME, EMAIL, PHONE), "todo": ""},
            {"status": "WARN", "name": "Résumé", "line": "Read %s/resume/original.pdf: %s %s." % (self.home, RESUME, COMPANY), "todo": ""},
            {"status": "FAIL", "name": "License", "line": "Key %s (SD-...7Q2W) and %s." % (LICENSE, OPENROUTER), "todo": "x"},
            {"status": "OK", "name": "LinkedIn", "line": "LinkedIn connected: Zed Q (checked just now) https://www.%s" % LINKEDIN, "todo": ""},
            {"status": "OK", "name": "Home folder", "line": "Writes to %s and /home/someone/x and C:\\Users\\someone\\y." % Path.home(), "todo": ""},
            {"status": "OK", "name": "Dashboard", "line": "Token %s." % base.SECRET, "todo": ""},
        ]
        view = {"needs_you": [{"at": None, "code": "REQUIRED_ELIGIBILITY_ANSWER", "text": "%s, %s: needs you" % (COMPANY, ROLE)}],
                "sections": [{"entries": [{"at": None, "code": "RETRY:reply-monitor", "text": "%s %s" % (COMPANY, ROLE)},
                                          {"at": None, "code": "<script>%s" % EMAIL, "text": ""}]}]}
        database = base.database_with_schema(self.root)
        database.execute("INSERT INTO application_jobs(job_key,employer,requisition,payload,state,updated_at) VALUES (?,?,?,?,?,?)",
                         ("acme:1", COMPANY, "1", "{}", "QUEUED", "2026-10-22T00:00:00+00:00"))
        database.commit()
        database.close()
        with mock.patch.object(doctor, "run", return_value=planted), \
                mock.patch("reply_resume.context", return_value=RESUME_CONTEXT), \
                mock.patch.object(failures_page, "gather", return_value=view), \
                mock.patch.object(failures_page, "_redaction_complete", return_value=True):
            return report_problem.build(self.home, self.root, base.CONFIG, base.SECRET)

    def test_no_secret_or_personal_detail_reaches_the_summary_or_the_address(self):
        text, url = self.build()
        for planted in (LICENSE, "7Q2W", OPENROUTER, base.SECRET, NAME, SURNAME, EMAIL, PHONE, "7946", LINKEDIN,
                        "linkedin.com", "Zed Q", COMPANY, "Acmeplanted", ROLE, RESUME, str(self.home), str(FAKE_USER_HOME), str(Path.home()),
                        "someone", "Tallahassee", "@", "https://"):
            self.assertNotIn(planted.lower(), text.lower(), planted)
            if planted != "https://":
                self.assertNotIn(planted.lower(), urllib.parse.unquote_plus(url).lower(), planted)
        self.assertTrue(report_problem.allowed_url(url))
        self.assertIn("Version: ", text)
        self.assertIn("Platform: ", text)
        self.assertIn("FAIL  License:", text)
        self.assertIn("OK  Home folder: Writes to [path] and [path].", text)
        self.assertIn("OK  LinkedIn: connected\n", text)
        self.assertIn("Recent failure codes: REQUIRED_ELIGIBILITY_ANSWER, RETRY:reply-monitor", text)

    def test_builds_on_the_default_home_the_dashboard_passes_as_none(self):
        # 2026-10-23: the running app passes home=None; the summary was refused ("None/profile.json").
        with mock.patch.dict(os.environ, {"SIDEDOOR_HOME": str(self.home)}):
            original = report_problem.build
            with mock.patch.object(report_problem, "build", lambda home, *rest: original(None, *rest)):
                text, url = self.build()
        self.assertIn("Version: ", text)
        self.assertTrue(report_problem.allowed_url(url))

    def test_hidden_while_the_keychain_is_locked(self):
        with mock.patch.object(failures_page, "_redaction_complete", return_value=False), \
                mock.patch.object(doctor, "run", side_effect=AssertionError("no checks while keys are unknown")):
            text, url = report_problem.build(self.home, self.root, base.CONFIG, base.SECRET)
        self.assertIn(failures_page.DETAILS_HIDDEN, text)
        self.assertTrue(report_problem.allowed_url(url))

    def summary_of(self, checks):
        with mock.patch.object(doctor, "run", return_value=checks), \
                mock.patch("reply_resume.context", return_value=RESUME_CONTEXT), \
                mock.patch.object(failures_page, "_redaction_complete", return_value=True):
            return report_problem.build(self.home, self.root, base.CONFIG, base.SECRET)[0]

    # RP1: a read the scrubs need that fails refuses the summary (fail closed).
    def test_refuses_when_a_scrub_input_cannot_be_read(self):
        base.database_with_schema(self.root).close()
        cases = {"profile missing": lambda: (self.home / "profile.json").unlink(),
                 "profile unreadable": lambda: (self.home / "profile.json").write_text("{not json"),
                 "database": lambda: mock.patch.object(report_problem, "recorded_names",
                                                       side_effect=sqlite3.DatabaseError("malformed")).start(),
                 "résumé": lambda: mock.patch("reply_resume.context", side_effect=ValueError("REPLY_RESUME_UNAVAILABLE")).start()}
        for name, breaks in cases.items():
            with self.subTest(name):
                (self.home / "profile.json").write_text(json.dumps(PROFILE))
                breaks()
                try:
                    with mock.patch.object(doctor, "run", return_value=[line("OK", "Profile", "%s %s" % (NAME, COMPANY))]), \
                            mock.patch.object(failures_page, "_redaction_complete", return_value=True), \
                            mock.patch.object(failures_page, "gather", return_value={"needs_you": [], "sections": []}):
                        if name != "résumé":
                            mock.patch("reply_resume.context", return_value=RESUME_CONTEXT).start()
                        with self.assertRaises(report_problem.Unsafe):
                            report_problem.build(self.home, self.root, base.CONFIG, base.SECRET)
                finally:
                    mock.patch.stopall()

    # Every string leaf of the profile is personal, whatever its key or length (confirming check).
    def test_every_profile_string_is_scrubbed(self):
        profile = dict(PROFILE, location_priority=["Seattle, Washington, United States remote/hybrid/onsite",
                                                   "Boise, Idaho remote"])
        profile["answers"] = dict(PROFILE["answers"], **{
            "relocation_destinations": {"value": ["Lisbon", "Porto"]},
            "mailing_address": {"value": "Apartment 4B, 1200 Northeast Ravenna Boulevard, Seattle, Washington 98115, "
                                         "United States of America"},
            "willing_to_relocate": {"value": True, "source": "User: willing to relocate near my sister Mariana Okafor"},
            "current_location": {"value": "Seattle", "note": "Lives near Green Lake; prefers Fremont"}})
        (self.home / "profile.json").write_text(json.dumps(profile))
        text = self.summary_of([line("WARN", "Places", "Seattle and Boise, Idaho, Lisbon or Porto; Ravenna 98115 "
                                                       "with Mariana Okafor by Green Lake for the Fremont office.")])
        for leaked in ("Seattle", "Boise", "Idaho", "Lisbon", "Porto", "Ravenna", "98115", "Mariana", "Okafor",
                       "Green", "Fremont"):
            self.assertNotIn(leaked, text)
        for kept in ("and", "with", "the"):
            self.assertIn(" %s " % kept, text)

    # RP2: the LinkedIn line never carries the account's display name.
    def test_linkedin_line_is_only_connected_or_not(self):
        names = ("Dr. Janie Q. Doe-Smith (Jay)", "Prof. Ana-María O'Neil Jr.", "山田 太郎", "Ольга Петрова (Olya)")
        for shown in names:
            with self.subTest(shown):
                text = self.summary_of([line("OK", "LinkedIn", "LinkedIn connected: %s (checked just now)." % shown)])
                self.assertIn("OK  LinkedIn: connected\n", text)
                for part in shown.replace("(", " ").replace(")", " ").split():
                    if len(part) > 3:
                        self.assertNotIn(part, text)
        text = self.summary_of([line("FAIL", "LinkedIn", "LinkedIn needs Janie Doe to sign in again.")])
        self.assertIn("FAIL  LinkedIn: not connected\n", text)
        self.assertNotIn("Janie", text)

    # RP3: every filesystem path, including the tray reason and App location lines.
    def test_every_path_becomes_path(self):
        cases = {"/Volumes/Work Disk/Apps/Sidedoor.app/Contents/MacOS/sidedoor": "Volumes",
                 "/private/var/folders/xy/T/sidedoor": "private", "/opt/homebrew/bin/python3": "homebrew",
                 "~/Library/Application Support/Sidedoor/x.log": "Library", "~": None,
                 "C:\\Users\\zq\\OneDrive - Acmeplanted\\Sidedoor\\x.txt": "OneDrive",
                 "D:\\Sidedoor\\data": "Sidedoor\\data", "z:/shared/sidedoor": "shared",
                 "\\\\fileserver\\team share\\cv.pdf": "fileserver"}
        for path, word in cases.items():
            with self.subTest(path):
                scrubbed = report_problem.scrub("Sidedoor runs from %s, so it can't start." % path)
                self.assertEqual(scrubbed, "Sidedoor runs from [path], so it can't start.")
        text = self.summary_of([
            doctor._line("WARN", "Tray icon", "Not shown at the last start: no tray in /Volumes/Ext/Sidedoor.app (x)."),
            doctor._line("WARN", "App location", "Sidedoor runs from E:\\Downloads\\Sidedoor.exe, so it can't start at login.")])
        self.assertIn("WARN  Tray icon: Not shown at the last start: no tray in [path] (x).", text)
        self.assertIn("WARN  App location: Sidedoor runs from [path], so it can't start at login.", text)
        for kept in ("Python 3.13.12, Sidedoor 0.0.3.", "553.7 GB free.", "and/or HTTP 400/500", "e.g. done"):
            self.assertEqual(report_problem.scrub(kept), kept)

    # RP4: links, bare domains and domain/path forms.
    def test_domains_are_scrubbed(self):
        scrubbed = report_problem.scrub("See careers.acme.io/jobs, greenhouse.io/acme, jobs.lever.co and https://x.example/a?b=1.")
        self.assertEqual(scrubbed, "See [link], [link], [link] and [link].")

    # RP6: codes from the Failures page's own reading of real rows reach the summary, screened.
    def test_codes_from_real_failure_rows(self):
        database = base.database_with_schema(self.root)
        database.execute(browser_runtime.UNAVAILABLE_TABLE)
        now = dt.datetime.now(dt.timezone.utc)
        recent = (now - dt.timedelta(hours=2)).isoformat()
        database.execute("INSERT INTO application_jobs(job_key,employer,requisition,payload,state,blocker,next_at,updated_at) "
                         "VALUES (?,?,?,?,?,?,?,?)", ("acme:1", COMPANY, "1", json.dumps({"company": COMPANY, "role": ROLE}),
                                                      "BLOCKED", "FILE_UPLOAD_FAILED", None, recent))
        failures_tests.task(database, "t1", "reply-monitor", "RETRY_WAIT", now - dt.timedelta(hours=1),
                            next_at=now + dt.timedelta(hours=1), events=["RETRY_WAIT"])
        failures_tests.task(database, "t2", "sync.acmeplanted.io", "RETRY_WAIT", now - dt.timedelta(hours=1),
                            next_at=now + dt.timedelta(hours=1), events=["RETRY_WAIT"])
        database.execute("INSERT INTO browser_unavailable(code,source,first_seen,last_seen) VALUES (?,?,?,?)",
                         ("APPLICATION_BROWSER_RUNTIME_MISSING", "applications", (now - dt.timedelta(hours=3)).isoformat(), recent))
        database.commit()
        database.close()
        with mock.patch.object(setup_steps, "chrome_path", return_value="/chrome"):
            text = self.summary_of([line("OK", "Computer", "Fine.")])
        codes = text.split("Recent failure codes: ", 1)[1].strip().split(", ")
        self.assertEqual(sorted(codes), ["APPLICATION_BROWSER_RUNTIME_MISSING", "FILE_UPLOAD_FAILED", "RETRY:reply-monitor"])
        for planted in (COMPANY, "Acmeplanted", ROLE, "acmeplanted.io"):
            self.assertNotIn(planted.lower(), text.lower())

    def test_failure_items_carry_codes(self):
        item = failures_page._item("outreach", "x", "y", code="INVITE_RETRY_WAIT")
        self.assertEqual(item["code"], "INVITE_RETRY_WAIT")


@unittest.skipUnless(DEPENDENCIES, "dashboard packages (fastapi, httpx) are not installed")
class EndpointTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.home = planted_home(self.root)
        resume = mock.patch("reply_resume.context", return_value=RESUME_CONTEXT)
        resume.start()
        self.addCleanup(resume.stop)
        app = dashboard.create_app(base.SECRET, base.PORT, home=self.home, root=self.root, config=base.CONFIG)
        self.client = TestClient(app, base_url=f"http://127.0.0.1:{base.PORT}", follow_redirects=False)
        chrome = mock.patch.object(setup_steps, "chrome_path", return_value="/chrome")
        chrome.start()
        self.addCleanup(chrome.stop)

    def tearDown(self):
        self.directory.cleanup()

    def test_endpoint_needs_the_session_and_the_origin(self):
        self.assertEqual(self.client.post("/report-problem", headers=ORIGIN).status_code, 403)   # no cookie
        self.client.get("/", params={"t": base.SECRET})
        self.assertEqual(self.client.post("/report-problem").status_code, 403)                   # no Origin
        self.assertEqual(self.client.post("/report-problem", headers={"origin": "https://evil.test"}).status_code, 403)
        with mock.patch.object(failures_page, "_redaction_complete", return_value=True):
            response = self.client.post("/report-problem", headers=ORIGIN)
        self.assertEqual(response.status_code, 200)
        answer = response.json()
        self.assertTrue(report_problem.allowed_url(answer["url"]))
        self.assertIn("Sidedoor diagnostic summary", answer["summary"])
        self.assertNotIn(base.SECRET, json.dumps(answer))
        self.assertEqual(self.client.get("/report-problem").status_code, 405)

    def test_refusal_says_so_and_carries_no_summary_or_address(self):
        self.client.get("/", params={"t": base.SECRET})
        (self.home / "profile.json").unlink()
        with mock.patch.object(failures_page, "_redaction_complete", return_value=True):
            response = self.client.post("/report-problem", headers=ORIGIN)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"error": report_problem.REFUSED})
        self.assertEqual(report_problem.REFUSED, "Sidedoor couldn't build a safe summary right now. Try again in a minute.")

    def test_button_renders_on_failures_and_settings(self):
        self.client.get("/", params={"t": base.SECRET})
        for path in ("/failures", "/settings"):
            page = self.client.get(path).text
            self.assertIn('id="report-problem-button"', page, path)
            self.assertIn('<script src="/static/report_problem.js?v=%s" defer></script>' % version.current(), page, path)
            self.assertIn(report_problem.NOTE, page, path)
        script = self.client.get("/static/report_problem.js").text
        self.assertIn('"' + report_problem.ISSUE_FORM + '&"', script)


if __name__ == "__main__":
    unittest.main()
