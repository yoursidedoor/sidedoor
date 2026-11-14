"""E15.T11 (R11): the Metrics page counts every time a site asked for a sign-in or a check and how
it ended, from the stored waits; the same totals in `sidedoor doctor` and Report a problem."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
import hashlib
import html
import importlib.util
import json
import os
import re
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

import access_outcomes
import doctor
import metrics_page
import overview_page
import report_problem
import setup_steps
from tests.test_dashboard_overview import CONFIG, PORT, SECRET, database_with_schema, finished_home

DEPENDENCIES = all(importlib.util.find_spec(name) for name in ("fastapi", "httpx"))
if DEPENDENCIES:
    from fastapi.testclient import TestClient

    import dashboard

ROOT = Path(__file__).resolve().parents[1]
RAW_CODE = re.compile(r"\b[A-Z]{2,}(?:_[A-Z0-9]+)+\b")
NOW = dt.datetime(2026, 10, 6, 18, 0, tzinfo=dt.timezone.utc)
ALPHA, BETA = "jobs.alpha.example", "beta.example"
SIGN_IN, CHECK = "AUTHENTICATION_REQUIRED", "CAPTCHA_REQUIRED"


def at(month, day, hour=18):
    return dt.datetime(2026, month, day, hour, tzinfo=dt.timezone.utc).isoformat()


def counts(**found):
    return dict(dict.fromkeys(["met"] + [key for key, _ in metrics_page.BUCKETS], 0), **found)


class Store:
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)   # the Sidedoor home and its data folder, as an install has them
        finished_home(self.root)

    def open_store(self):
        self.db = database_with_schema(self.root)
        self.addCleanup(self.db.close)

    def job(self, key, url):
        with self.db:
            self.db.execute("INSERT INTO application_jobs(job_key,employer,requisition,payload,state,updated_at) "
                            "VALUES (?,?,?,?,?,?)", (key, "Acme Robotics", key, json.dumps({"url": url}), "QUEUED", at(8, 1)))

    def row(self, key):
        return dict(self.db.execute("SELECT * FROM application_jobs WHERE job_key=?", (key,)).fetchone())

    def wall(self, key, code, now):
        with self.db:
            self.assertEqual(access_outcomes.record(self.db, self.row(key), code, now), access_outcomes.WAITING)

    def held(self, key, now):
        with self.db:
            self.assertTrue(access_outcomes.wait_on_host(self.db, self.row(key), now))

    def view(self, **arguments):
        return overview_page.snapshot(self.root / "state" / "orchestrator.sqlite3",
                                      lambda database: metrics_page.gather(database, CONFIG, now=NOW, **arguments),
                                      lambda: overview_page._connect((self.root / "state" / "orchestrator.sqlite3").as_uri() + "?mode=ro"))

    def history(self):
        """Three months, two sites, written by access_outcomes itself with a moving clock."""
        self.open_store()
        for key, host in (("a", ALPHA), ("b", ALPHA), ("g", ALPHA), ("c", "www." + BETA), ("d", BETA), ("e", BETA), ("f", BETA)):
            self.job(key, "https://%s/jobs/%s" % (host, key))
        self.wall("a", SIGN_IN, at(8, 10))                 # August, alpha: the user signs in the next day
        before = self.view()["view"]
        self.held("b", at(8, 10, 19))                      # held behind a's sign-in: never asked itself
        self.assertEqual(self.view()["view"], before)      # a held job changes no number
        with self.db:
            self.assertEqual(sorted(access_outcomes.resume(self.db, "a", at(8, 11))), ["a", "b"])
        self.wall("c", SIGN_IN, at(9, 1))                  # September, beta: nobody acts for 7 days
        self.held("f", at(9, 1, 19))
        self.wall("d", CHECK, at(9, 2))                    # September, beta: the user closes it
        with self.db:
            self.assertTrue(access_outcomes.abandon(self.db, "d", at(9, 3)))
            self.assertEqual(access_outcomes.expire(self.db, at(9, 9)), ["c"])
        self.wall("e", SIGN_IN, at(9, 20))                 # September, beta: the job finishes another way
        with self.db:
            self.db.execute("UPDATE application_jobs SET state='SUBMITTED' WHERE job_key='e'")
            access_outcomes.expire(self.db, at(9, 21))
        self.wall("g", CHECK, at(10, 1, 3))                # 3:00 UTC on Oct 1 is still September in Los Angeles
        self.wall("a", CHECK, at(10, 5))                   # October, alpha: the same job is asked a second time
        held = self.db.execute("SELECT COUNT(*) FROM application_access_waits WHERE source LIKE 'host:%'").fetchone()[0]
        self.assertEqual(held, 2)
        self.db.commit()


class GatherTest(Store, unittest.TestCase):
    def test_each_row_is_in_one_bucket_by_total_month_and_site(self):
        self.history()
        view = self.view()["view"]
        self.assertEqual(view["total"], counts(met=6, you=1, waiting=2, closed_7_days=1, you_closed=1, other=1))
        self.assertEqual(view["recent"], counts(met=3, waiting=2, other=1))
        self.assertEqual([(month["label"], month["counts"]) for month in view["months"]], [
            ("October 2026", counts(met=1, waiting=1)),
            ("September 2026", counts(met=4, waiting=1, closed_7_days=1, you_closed=1, other=1)),
            ("August 2026", counts(met=1, you=1))])
        self.assertEqual([(site["site"], site["counts"]) for site in view["sites"]], [
            (BETA, counts(met=3, closed_7_days=1, you_closed=1, other=1)),
            (ALPHA, counts(met=3, you=1, waiting=2))])
        self.assertEqual((view["chosen_label"], view["could_not_show"]), ("All time", 0))
        for found in [view["total"], view["recent"]] + [item["counts"] for item in view["months"] + view["sites"]]:
            self.assertEqual(sum(found[key] for key, _ in metrics_page.BUCKETS), found["met"])
        september = self.view(month="2026-09")["view"]
        self.assertEqual((september["chosen"], september["chosen_label"]), ("2026-09", "September 2026"))
        self.assertEqual([(site["site"], site["counts"]["met"]) for site in september["sites"]], [(BETA, 3), (ALPHA, 1)])
        self.assertIsNone(self.view(month="1999-01' OR 1=1")["view"]["chosen"])

    def test_the_other_endings_each_have_their_bucket(self):
        self.open_store()
        for key, host in (("p", ALPHA), ("s", ALPHA), ("o", BETA), ("k", BETA)):
            self.job(key, "https://%s/jobs/%s" % (host, key))
        self.wall("p", SIGN_IN, at(8, 10))                 # August, alpha: the posting closes while it waits
        self.wall("s", CHECK, at(9, 1))                    # September, alpha: the job is submitted another way
        self.wall("o", SIGN_IN, at(9, 5))                  # September, beta: the job is closed for another reason
        with self.db:
            self.assertTrue(access_outcomes.posting_closed(self.db, "p", at(8, 12)))
            access_outcomes.settle(self.db, "s", "SUBMITTED", None, at(9, 2))
            access_outcomes.settle(self.db, "o", "INELIGIBLE", "POSTING_NOT_ELIGIBLE", at(9, 6))
            # October, beta: closed at once under the Skip setting. No path on this branch writes this
            # row (`access_outcomes.close` has no open wait to end); these are the values the account
            # setting's branch will write: a closed row of the job's own, outcome SKIPPED.
            self.db.execute("INSERT INTO application_access_waits(job_key,host,wall,reason,source,since,deadline,state,"
                            "ended_at,outcome,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            ("k", BETA, SIGN_IN, "SIGN_IN", "session", at(10, 5), at(10, 12), access_outcomes.CLOSED,
                             at(10, 5), "SKIPPED", at(10, 5)))
        stored = [tuple(row) for row in self.db.execute("SELECT job_key,state,outcome FROM application_access_waits ORDER BY id")]
        self.assertEqual(stored, [("p", "CLOSED", "POSTING_CLOSED"), ("s", "RESOLVED", "submitted"),
                                  ("o", "CLOSED", "POSTING_NOT_ELIGIBLE"), ("k", "CLOSED", "SKIPPED")])
        september = self.view(month="2026-09")["view"]
        view = self.view()["view"]
        self.assertEqual(view["total"], counts(met=4, posting_closed=1, skipped=1, other=2))
        self.assertEqual([(month["label"], month["counts"]) for month in view["months"]], [
            ("October 2026", counts(met=1, skipped=1)),
            ("September 2026", counts(met=2, other=2)),
            ("August 2026", counts(met=1, posting_closed=1))])
        self.assertEqual([(site["site"], site["counts"]) for site in view["sites"]], [
            (BETA, counts(met=2, skipped=1, other=1)), (ALPHA, counts(met=2, posting_closed=1, other=1))])
        self.assertEqual([(site["site"], site["counts"]) for site in september["sites"]], [
            (BETA, counts(met=1, other=1)), (ALPHA, counts(met=1, other=1))])

    def test_only_the_latest_twelve_months_are_shown(self):
        self.open_store()
        stamps = ["2025-%02d-15T18:00:00+00:00" % month for month in (10, 11, 12)] + [at(month, 15) for month in range(1, 11)]
        for index, stamp in enumerate(stamps):   # 13 months, October 2025 to October 2026, one each
            self.job("m%d" % index, "https://%s/jobs/m%d" % (ALPHA if index % 2 else BETA, index))
            self.wall("m%d" % index, CHECK, stamp)
        view = self.view()["view"]
        self.assertEqual(metrics_page.MONTHS_SHOWN, 12)
        self.assertEqual([month["key"] for month in view["months"]],
                         ["2026-%02d" % month for month in range(10, 0, -1)] + ["2025-12", "2025-11"])
        self.assertEqual(view["total"]["met"], 13)   # the month no longer listed still counts in the totals
        # A month that is not in the list is not a choice: the sites table stays on all time.
        older = self.view(month="2025-10")["view"]
        self.assertEqual((older["chosen"], older["chosen_label"], sum(site["counts"]["met"] for site in older["sites"])),
                         (None, "All time", 13))
        self.assertEqual(self.view(month="2025-11")["view"]["chosen"], "2025-11")

    def test_the_two_seams_join_the_counts(self):
        self.history()
        with mock.patch.object(metrics_page, "sidedoor_handled", lambda reader: [("www." + BETA, at(10, 2))]), \
                mock.patch.object(metrics_page, "could_not_show", lambda reader: [(ALPHA, at(10, 2))]):
            view = self.view()["view"]
        self.assertEqual((view["total"]["met"], view["total"]["sidedoor"], view["could_not_show"]), (7, 1, 1))
        self.assertEqual(view["months"][0]["counts"], counts(met=2, sidedoor=1, waiting=1))
        with mock.patch.object(metrics_page, "SIDEDOOR_OUTCOMES", frozenset({metrics_page.USER_OUTCOME})):
            self.assertEqual(self.view()["view"]["total"], counts(met=6, sidedoor=1, waiting=2, closed_7_days=1, you_closed=1, other=1))

    def test_nothing_met_and_an_older_database_read_as_empty(self):
        self.assertEqual(self.view()["view"]["total"]["met"], 0)   # no database file yet
        self.open_store()
        self.db.execute("DROP TABLE application_access_waits")
        self.db.commit()
        found = self.view()
        self.assertEqual((found["unavailable"], found["view"]["total"], found["view"]["months"]), (False, counts(), []))
        self.assertEqual(metrics_page.support_line(self.root, CONFIG, NOW), metrics_page.NONE_YET)


class SupportTest(Store, unittest.TestCase):
    def test_doctor_and_the_report_summary_print_the_page_totals_without_site_names(self):
        self.history()
        view = self.view()["view"]
        check = doctor.check_access(self.root, CONFIG, NOW)
        self.assertEqual((check["status"], check["name"]), ("OK", "Sign-ins and checks"))
        self.assertEqual(check["line"], "Last 30 days: %s. All time: %s." % (
            metrics_page.counts_line(view["recent"]), metrics_page.counts_line(view["total"])))
        self.assertIn("All time: a site asked 6 times (Sidedoor handled 0, You handled 1, Still waiting for you 2, "
                      "Closed after 7 days 1, You closed 1, Skipped by your setting 0, Posting closed first 0, "
                      "Ended another way 1).", check["line"])
        self.assertIn("Last 30 days: a site asked 3 times (", check["line"])
        self.assertTrue(metrics_page.counts_line(counts(met=1, you=1)).startswith("a site asked 1 time (Sidedoor handled 0, You handled 1,"))
        self.assertTrue(metrics_page.counts_line(counts()).startswith("a site asked 0 times ("))
        text = report_problem.summary("1.0.0", "macOS Apple chip", [check], [], personal=["Acme Robotics", "Acme", "Robotics"])
        self.assertIn("OK  Sign-ins and checks: " + check["line"] + "\n", text)   # the scrubs change nothing in it
        for private in (ALPHA, BETA, "example", "Acme"):
            self.assertNotIn(private, text)
        self.assertIsNone(RAW_CODE.search(check["line"]))

    def test_a_locked_database_reads_as_not_available_like_recent_failures(self):
        import sqlite3
        self.open_store()
        self.db.close()
        with mock.patch.object(overview_page, "_connect", side_effect=sqlite3.OperationalError("database is locked")):
            check = doctor.check_access(self.root, CONFIG, NOW)
        self.assertEqual((check["status"], check["name"], check["line"]),
                         ("WARN", "Sign-ins and checks", "Not available: the records can't be read."))

    def test_nothing_is_sent_anywhere(self):
        source = (ROOT / "metrics_page.py").read_text(encoding="utf-8")
        for word in ("urllib.request", "http.client", "requests", "relay", "heartbeat", "socket"):
            self.assertNotIn(word, source.split('"""', 2)[2])


@unittest.skipUnless(DEPENDENCIES, "dashboard packages (fastapi, httpx) are not installed")
class PageTest(Store, unittest.TestCase):
    def client(self, signed_in=True):
        # The app's own entry point with its defaults (home=None, root=None), on a temporary SIDEDOOR_HOME.
        environment = mock.patch.dict(os.environ, {"SIDEDOOR_HOME": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)
        client = TestClient(dashboard.create_app(SECRET, PORT, config=CONFIG), base_url=f"http://127.0.0.1:{PORT}",
                            follow_redirects=False)
        if signed_in:
            client.get("/", params={"t": SECRET})
        return client

    def get(self, **params):
        with mock.patch.object(setup_steps, "chrome_path", return_value="/chrome"):
            response = self.client().get("/metrics", params=params)
        self.assertEqual(response.status_code, 200, response.text[:300])
        return response.text

    def rows(self, page):
        return {html.unescape(re.sub(r"<[^>]+>", "", name)).strip(): re.findall(r"<td>(\d+)</td>", cells)
                for name, cells in re.findall(r'<tr[^>]*><th scope="row">(.*?)</th>(.*?)</tr>', page, re.S)}

    def test_the_page_shows_the_counts_in_plain_words_and_writes_nothing(self):
        self.history()
        self.db.close()
        path = self.root / "state" / "orchestrator.sqlite3"
        before, files = hashlib.sha256(path.read_bytes()).hexdigest(), sorted(p.name for p in path.parent.iterdir())
        page = self.get()
        self.assertIn('class="tab active" href="/metrics" aria-current="page">Metrics</a>', page)
        self.assertLess(page.index('href="/failures"'), page.index('href="/metrics"'))
        self.assertLess(page.index('href="/metrics"'), page.index('href="/settings"'))
        main = html.unescape(page.split("<main", 1)[1])
        for label in [metrics_page.MET_LABEL, metrics_page.COULD_NOT_SHOW_LABEL + ": 0."] + [label for _, label in metrics_page.BUCKETS]:
            self.assertIn(label, main)
        rows = self.rows(page)
        order = ["met"] + [key for key, _ in metrics_page.BUCKETS]
        cells = lambda **found: [str(counts(**found)[key]) for key in order]
        self.assertEqual(rows["All time"], cells(met=6, you=1, waiting=2, closed_7_days=1, you_closed=1, other=1))
        self.assertIn("Last 30 days", rows)   # its numbers follow the clock; GatherTest pins them
        self.assertEqual(rows["September 2026"], cells(met=4, waiting=1, closed_7_days=1, you_closed=1, other=1))
        self.assertEqual(rows["August 2026"], cells(met=1, you=1))
        self.assertEqual(rows[ALPHA], cells(met=3, you=1, waiting=2))
        self.assertEqual(rows[BETA], cells(met=3, closed_7_days=1, you_closed=1, other=1))
        october = self.get(month="2026-10")
        self.assertIn("By site: October 2026", october)
        self.assertEqual((self.rows(october)[ALPHA], BETA in self.rows(october)), (cells(met=1, waiting=1), False))
        for text in (main, html.unescape(october.split("<main", 1)[1]), html.unescape(self.get(month="junk").split("<main", 1)[1])):
            self.assertIsNone(RAW_CODE.search(text))
            for banned in ("—", "utomation", "resolved", "escalated", " wall", "host", "session"):
                self.assertNotIn(banned, text)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), files)

    def test_empty_state_before_any_site_asked_and_on_an_older_database(self):
        page = html.unescape(self.get())
        self.assertIn(metrics_page.NONE_YET + " When one does, it is counted here.", page)
        self.assertNotIn("<table", page)
        self.assertFalse((self.root / "state").exists())
        self.open_store()
        self.db.execute("DROP TABLE application_access_waits")
        self.db.commit()
        self.db.close()
        page = html.unescape(self.get())
        self.assertIn(metrics_page.NONE_YET, page)
        self.assertNotIn("<table", page)

    def test_the_route_is_guarded_like_the_other_pages(self):
        self.assertEqual(self.client(signed_in=False).get("/metrics").status_code, 403)
        self.assertEqual(self.client().get("/metrics", headers={"host": "evil.example"}).status_code, 400)
        self.assertEqual(self.client().post("/metrics", headers={"origin": f"http://127.0.0.1:{PORT}"}).status_code, 405)


class PackagingTest(unittest.TestCase):
    def test_the_module_and_template_are_packaged(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["setuptools"]
        self.assertIn("metrics_page", project["py-modules"])
        self.assertEqual(project["data-files"]["share/sidedoor/dashboard_assets/templates"], ["dashboard_assets/templates/*.html"])
        self.assertTrue((ROOT / "dashboard_assets" / "templates" / "metrics.html").is_file())


if __name__ == "__main__":
    unittest.main()
