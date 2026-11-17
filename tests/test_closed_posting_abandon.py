"""A closed posting closes its job at the first read, jobs already parked on one are closed by the
round, and a job parked for the user has Abandon beside Try again (owner, 2026-11-14)."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import html
import importlib.util
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import access_cards
import access_outcomes
import application_recovery
import applications
import applications_page
import desktop_adapter
import manual_application_receipts as manual
import overview_page
import setup_steps
from tests.test_dashboard_overview import CONFIG, PORT, SECRET, database_with_schema, finished_home

DEPENDENCIES = all(importlib.util.find_spec(name) for name in ("fastapi", "httpx"))
if DEPENDENCIES:
    from fastapi.testclient import TestClient

    import dashboard

HTMX = {"origin": f"http://127.0.0.1:{PORT}", "hx-request": "true"}
RAW_CODE = re.compile(r"\b[A-Z]{2,}(?:_[A-Z0-9]+)+\b")
PARKED = application_recovery.CLAIMS_EXHAUSTED_BLOCKER


def view_of(database, key):
    view = applications_page.gather(database, CONFIG)
    return next(item for item in view["list"]["jobs"] if item["key"] == key)


class FirstReadClosesTest(unittest.TestCase):
    def setUp(self):
        self.fixture = manual.Fixture()
        self.addCleanup(self.fixture.close)
        self.calls = []

    def read(self, code):
        fixture, calls = self.fixture, self.calls

        class Reads:
            can_submit = False

            def call(inner, operation, payload):
                calls.append(operation)
                return {"blocked": {"code": code, "next_action": "The page says this job is no longer available."},
                        "partial_review": {"facts": {"open": False}}}
        applications.process(fixture.database, fixture.job(), Reads(), fixture.policy, fixture.root, fixture.now,
                             config=fixture.config)

    def test_the_prompt_declares_the_code_python_matches(self):
        import application_browser
        source = Path(application_browser.__file__).read_text()
        self.assertIn("return blocked with code\n" + applications.ROLE_CLOSED_CODE + ".", source)

    def test_a_closed_posting_closes_the_job_at_the_first_read(self):
        self.read("ROLE_CLOSED")
        job = self.fixture.job()
        self.assertEqual((self.calls, job["state"], job["blocker"], job["next_at"]),
                         (["inspect"], "ABANDONED", "POSTING_CLOSED", None))
        # No read is recorded as inconclusive, and the round has nothing left to read.
        events = [row[0] for row in self.fixture.database.execute("SELECT event FROM application_events WHERE job_key=?", (job["job_key"],))]
        self.assertNotIn(applications.INCONCLUSIVE_EVENT, events)
        self.assertIn(access_outcomes.PLAIN_CLOSED_EVENT, events)
        view = view_of(self.fixture.database, job["job_key"])
        self.assertEqual((view["group"], view["needs_you"], view["closed"], view["todo"], view["abandon"]),
                         ("closed", False, "The posting has closed.", applications_page.POSTING_CLOSED_TODO, None))
        reader = overview_page.Reader(self.fixture.database)
        self.assertIsNone(applications_page.closed_reason(reader, job["job_key"], job["state"], job["blocker"]))
        self.assertEqual(overview_page._blocked(reader)[0], [])
        for line in applications_page.history(reader, job["job_key"]):
            self.assertIsNone(RAW_CODE.search(line["text"]), line)

    def test_another_stop_is_not_a_closed_posting(self):
        self.read("PAGE_UNUSABLE")
        self.assertNotIn(self.fixture.job()["state"], applications.FINAL)

    def test_a_job_that_may_have_reached_the_employer_is_not_closed(self):
        with mock.patch.object(applications, "uncertain_submission", return_value=True):
            self.read("ROLE_CLOSED")
        self.assertNotIn(self.fixture.job()["state"], applications.FINAL)


@unittest.skipUnless(DEPENDENCIES, "dashboard packages (fastapi, httpx) are not installed")
class ParkedJobsTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.home = self.root / "home"
        finished_home(self.home)
        self.database = database_with_schema(self.root)
        self.addCleanup(self.database.close)
        desktop_adapter.initialize(self.database)
        self.database.commit()
        self.now = applications.stamp()
        chrome = mock.patch.object(setup_steps, "chrome_path", return_value="/chrome")
        chrome.start()
        self.addCleanup(chrome.stop)
        app = dashboard.create_app(SECRET, PORT, home=self.home, root=self.root, config=CONFIG,
                                   browser=mock.Mock(is_open=lambda: False), relay_url="https://relay.test")
        self.client = TestClient(app, base_url=f"http://127.0.0.1:{PORT}", follow_redirects=False)
        self.client.get("/", params={"t": SECRET})

    def parked(self, key, last_read="ROLE_CLOSED"):
        """A job parked with its reads spent; `last_read` is what its last inconclusive read stopped on."""
        payload = json.dumps({"company": "Acme", "role": "Engineer " + key, "url": "https://jobs.example.com/" + key})
        with self.database:
            self.database.execute("INSERT INTO application_jobs(job_key,employer,requisition,payload,state,blocker,updated_at) "
                                  "VALUES (?,?,?,?,'BLOCKED',?,?)", (key, "acme", key, payload, PARKED, self.now))
            if last_read:
                applications.event(self.database, key, applications.INCONCLUSIVE_EVENT,
                                   {"method": "inspect", "blocker": last_read, "missing_fields": ["read/" + last_read]}, self.now)
        return key

    def row(self, key):
        return dict(self.database.execute("SELECT * FROM application_jobs WHERE job_key=?", (key,)).fetchone())

    def armed(self, key):
        """An armed submit on record, its attempt unsettled: it may have reached the employer."""
        self.database.execute("INSERT INTO desktop_requests(request_id,binding,job_key,operation,payload,state,created_at,"
                              "expires_at,armed_at,result,attempt_id) VALUES (?,'b',?,'submit','{}','CONSUMED',?,?,?,'{}','gone')",
                              ("armed-" + key, key, self.now, self.now, self.now))
        self.database.commit()

    def post(self, path, key, headers=HTMX):
        response = self.client.post(path, data={"job_key": key}, headers=headers)
        self.assertEqual(response.status_code, 200, response.text[:300])
        return html.unescape(response.text)

    def page(self, path="/applications"):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, response.text[:300])
        return html.unescape(response.text)

    def round(self):
        with self.database:
            return application_recovery.close_parked_closed_postings(self.database, applications.stamp())

    def test_a_job_parked_on_a_closed_posting_is_closed_by_one_round_and_only_once(self):
        self.parked("closed")
        self.parked("other", last_read="PAGE_UNUSABLE")
        self.parked("sent")
        self.armed("sent")
        self.assertEqual(self.round(), ["closed"])
        self.assertEqual((self.row("closed")["state"], self.row("closed")["blocker"]), ("ABANDONED", "POSTING_CLOSED"))
        self.assertEqual((self.row("other")["state"], self.row("other")["blocker"]), ("BLOCKED", PARKED))
        self.assertEqual((self.row("sent")["state"], self.row("sent")["blocker"]), ("BLOCKED", PARKED))
        before = list(self.database.iterdump())
        self.assertEqual(self.round(), [])
        self.assertEqual(before, list(self.database.iterdump()))
        page = self.page("/applications?job=closed")
        self.assertIn("The posting has closed.", page)
        self.assertIn(applications_page.POSTING_CLOSED_TODO, page)
        self.assertNotIn("apply yourself", page)
        self.assertNotIn("Blocked, needs you", page)
        self.assertIsNone(RAW_CODE.search(re.sub(r"<[^>]+>", " ", page)), RAW_CODE.findall(re.sub(r"<[^>]+>", " ", page)))

    def test_a_read_before_the_last_try_again_does_not_close_the_job(self):
        self.parked("again")
        with self.database:
            applications.event(self.database, "again", "APPLICATION_REOPENED", {"authorized_by": "user:dashboard"}, self.now)
        self.assertEqual(self.round(), [])
        self.assertEqual(self.row("again")["state"], "BLOCKED")

    def test_a_parked_job_has_abandon_beside_try_again(self):
        self.parked("one", last_read=None)
        page = self.page()
        self.assertIn(applications_page.PARKED_TODO, page)
        self.assertIn('action="/access/try-again"', page)
        self.assertIn('action="/applications/abandon"', page)
        self.assertIn(">Try again</button>", page)
        self.assertIn(">Abandon</button>", page)
        answer = self.post("/applications/abandon", "one")
        self.assertIn(access_cards.ABANDONED, answer)
        self.assertEqual((self.row("one")["state"], self.row("one")["blocker"]), ("ABANDONED", "ABANDONED_BY_YOU"))
        page = self.page("/applications?job=one")
        self.assertIn("You abandoned this application.", page)
        self.assertIn(applications_page.PARKED_TODO, page)
        self.assertIn(">Try again</button>", page)
        self.assertNotIn(">Abandon</button>", page)
        self.assertNotIn("sign-in", page)
        self.assertIsNone(RAW_CODE.search(re.sub(r"<[^>]+>", " ", page)))
        # A second press finds nothing to do; Try again brings the job back.
        self.assertIn(access_cards.ALREADY_FINISHED, self.post("/applications/abandon", "one"))
        self.assertIn(access_cards.REOPENED, self.post("/access/try-again", "one"))
        self.assertEqual(self.row("one")["state"], "QUEUED")

    def test_abandon_is_refused_for_a_job_that_may_have_reached_the_employer(self):
        self.parked("sent", last_read=None)
        self.armed("sent")
        self.assertNotIn(">Abandon</button>", self.page())
        self.assertIn(access_cards.NOT_ABANDONED, self.post("/applications/abandon", "sent"))
        self.assertEqual((self.row("sent")["state"], self.row("sent")["blocker"]), ("BLOCKED", PARKED))
        # Whatever its state (Abandon is on every open job since 2026-11-16, tests/test_abandon_any.py):
        # a job that may have reached the employer is never closed by a forged key.
        with self.database:
            self.database.execute("UPDATE application_jobs SET state='QUEUED',blocker=NULL WHERE job_key='sent'")
        self.assertIn(access_cards.NOT_ABANDONED, self.post("/applications/abandon", "sent"))
        self.assertEqual(self.row("sent")["state"], "QUEUED")


if __name__ == "__main__":
    unittest.main()
