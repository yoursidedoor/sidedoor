"""The Applications page (E6.T6; E6.T0 spec section 8): display only, from stored records.

`applications_page.gather` is plain Python and runs on the stdlib suite; the page tests need the
dashboard packages (fastapi, httpx) and are skipped without them.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import applications
import applications_page
import desktop_adapter
import overview_page
import setup_steps
from tests.test_dashboard_overview import (CONFIG, CSS, PORT, SECRET, database_with_schema, finished_home, pacific,
                                           question)

DEPENDENCIES = all(importlib.util.find_spec(name) for name in ("fastapi", "httpx"))
if DEPENDENCIES:
    from fastapi.testclient import TestClient

    import dashboard

RAW_CODE = re.compile(r"\b[A-Z]{2,}(?:_[A-Z0-9]+)+\b")


def job(database, key, state, blocker=None, company="Acme Robotics", role="Robotics Engineer", url=None,
        updated="2026-10-21T17:00:00+00:00", receipt=None, next_at=None, reconcile=0, payload=None):
    payload = payload if payload is not None else json.dumps(
        {"company": company, "role": role, "url": url or "https://jobs.example.com/" + key})
    database.execute("INSERT INTO application_jobs(job_key,employer,requisition,payload,state,blocker,updated_at,receipt,"
                     "next_at,reconcile_attempts) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (key, company.lower(), key, payload, state, blocker, updated, receipt, next_at, reconcile))


def app_event(database, key, kind, at, detail=None):
    database.execute("INSERT INTO application_events(job_key,at,event,detail) VALUES (?,?,?,?)",
                     (key, at.isoformat() if isinstance(at, dt.datetime) else at,
                      kind, detail if isinstance(detail, str) else json.dumps(detail or {})))


def visible_text(view):
    """Every string the page shows for a gathered view (its list jobs or its one job)."""
    jobs = view["list"]["jobs"] if view.get("list") not in (None, overview_page.UNAVAILABLE) else [view["job"]]
    return json.dumps(jobs, default=str)


class GatherTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.home = self.root / "home"
        finished_home(self.home)
        self.database = database_with_schema(self.root)
        desktop_adapter.initialize(self.database)
        self.now = pacific(2026, 10, 22, 8, 30)
        self.inside, self.before = pacific(2026, 10, 21, 10, 0), pacific(2026, 10, 21, 8, 59)

    def tearDown(self):
        self.database.close()
        self.directory.cleanup()

    def gather(self, database=None, **arguments):
        return applications_page.gather(database or self.database, CONFIG, self.home, now=self.now, **arguments)

    def overview(self):
        return overview_page.gather(self.database, CONFIG, self.home, setup_steps.progress(self.home), False, now=self.now)

    def keys(self, **arguments):
        return [item["key"] for item in self.gather(**arguments)["list"]["jobs"]]

    def buckets(self):
        db = self.database
        job(db, "eligibility", "BLOCKED", "REQUIRED_ELIGIBILITY_ANSWER")
        job(db, "login-1", "BLOCKED", "AUTHENTICATION_REQUIRED", next_at="2026-10-22T17:25:00+00:00")
        job(db, "login-2", "BLOCKED", "CAPTCHA_REQUIRED")
        job(db, "answers", "BLOCKED", "UNKNOWN_REQUIRED_ANSWERS:salary")
        job(db, "spent", "BLOCKED", "SUBMISSION_OUTCOME_UNKNOWN", company="Globex", reconcile=applications.RECONCILE_MAX_ATTEMPTS)
        job(db, "still-reading", "BLOCKED", "SUBMISSION_OUTCOME_UNKNOWN", reconcile=4)
        job(db, "confirming", "RECONCILE", "SUBMISSION_OUTCOME_UNKNOWN")
        job(db, "submitting", "SUBMITTING")
        page = {"status": "SUBMITTED", "evidence_kind": "page_confirmation", "confirmation_text": "Thanks for applying!",
                "armed_at": self.inside.isoformat(), "receipt_id": "request-1"}
        job(db, "today", "SUBMITTED", receipt=json.dumps(page))
        job(db, "yesterday", "SUBMITTED", receipt=json.dumps(dict(page, armed_at=self.before.isoformat())))
        app_event(db, "today", "SUBMISSION_VERIFIED", self.inside, page)
        app_event(db, "yesterday", "SUBMISSION_VERIFIED", self.before, page)
        question(db, "q1", "eligibility", self.now)
        db.commit()

    def test_counts_and_lists_match_overview(self):
        """Worked example 5: each Overview number leads to a list with exactly those jobs."""
        self.buckets()
        overview = self.overview()
        owner, waiting = overview_page._blocked(overview_page.Reader(self.database))
        view = self.gather()
        counts = view["counts"]
        pairs = {"needs_you": overview["needs"]["applications"], "retrying": overview["right_now"]["retrying"],
                 "confirming": overview["right_now"]["confirming"], "submitted_today": overview["submitted"]}
        for show, expected in pairs.items():
            self.assertEqual(counts[show], expected, show)
            self.assertEqual(self.gather(show=show)["list"]["total"], expected, show)
        self.assertEqual(sorted(self.keys(show="needs_you")),
                         sorted(overview_page.blocked_on_owner_keys(overview_page.Reader(self.database), self.now)))
        self.assertEqual(self.keys(show="needs_you"), ["spent"])   # the asked and the answered job are not here
        self.assertEqual(sorted(self.keys(show="retrying")), sorted(waiting))
        self.assertEqual(sorted(self.keys(show="confirming")), ["confirming", "submitting"])
        self.assertEqual(self.keys(show="submitted_today"), ["today"])
        self.assertEqual(counts["all"], 10)

    def test_waiting_to_retry_badge_chip_and_overview_agree(self):
        """Owner 2026-10-24: "Waiting to retry" is every job whose badge says so (RETRY_WAIT plus the
        BLOCKED automatic waits); "Queued" is QUEUED only. Badge counts == chip counts == Overview."""
        db = self.database
        job(db, "later-1", "RETRY_WAIT", "PREFLIGHT_TRANSIENT", next_at="2026-10-22T17:25:00+00:00")
        job(db, "later-2", "RETRY_WAIT", "ANSWERING_RETRY")
        job(db, "auto", "BLOCKED", "CAPTCHA_REQUIRED")
        job(db, "owner", "BLOCKED", "DESIGNATED_RESUME_MISMATCH")
        job(db, "queued", "QUEUED")
        db.commit()
        badges = [item["state"] for item in self.gather()["list"]["jobs"]]
        counts, overview = self.gather()["counts"], self.overview()
        self.assertEqual(badges.count("Waiting to retry"), 3)
        self.assertEqual(counts["retrying"], 3)
        self.assertEqual(overview["right_now"]["retrying"], 3)
        self.assertEqual(sorted(self.keys(show="retrying")), ["auto", "later-1", "later-2"])
        self.assertEqual(badges.count("Queued"), 1)
        self.assertEqual(counts["queued"], 1)
        self.assertEqual(self.keys(show="queued"), ["queued"])
        self.assertEqual(counts["needs_you"], overview["needs"]["applications"])
        self.assertEqual(badges.count("Blocked, needs you"), counts["needs_you"])

    def test_needs_you_says_what_to_do_and_waits_say_they_retry(self):
        """Worked example 2."""
        self.buckets()
        jobs = {item["key"]: item for item in self.gather()["list"]["jobs"]}
        spent = jobs["spent"]
        self.assertTrue(spent["needs_you"])
        self.assertEqual(spent["state"], "Blocked, needs you")
        self.assertIn("Check your applicant account or confirmation email at Globex to see whether it went through.", spent["todo"])
        self.assertNotIn("tell Sidedoor", spent["todo"])     # no control behind it (owner, 2026-10-22)
        login = jobs["login-1"]
        self.assertFalse(login["needs_you"])
        self.assertEqual(login["state"], "Waiting to retry")
        self.assertTrue(login["todo"].startswith("Will try again automatically"))
        self.assertEqual(login["problem"], "The application site asked for a sign-in.")
        self.assertEqual((jobs["eligibility"]["todo_href"], jobs["eligibility"]["todo_label"]), ("/approvals", "Open Approvals"))
        for item in jobs.values():
            if item["needs_you"]:
                self.assertTrue(item["todo"], item["key"])
        self.assertIsNone(RAW_CODE.search(visible_text(self.gather())))

    def test_only_a_verified_receipt_is_submitted(self):
        """Worked example 1: an acknowledged submit stays "Waiting for confirmation" with no receipt."""
        db = self.database
        acknowledged = {"status": "SUBMITTED", "evidence_kind": "page_confirmation", "confirmation_text": "Got it"}
        job(db, "acknowledged", "RECONCILE", "SUBMISSION_OUTCOME_UNKNOWN", receipt=json.dumps(acknowledged))
        job(db, "page", "SUBMITTED", receipt=json.dumps(dict(acknowledged, armed_at=self.inside.isoformat())))
        job(db, "email", "SUBMITTED", receipt=json.dumps({"evidence_kind": "sent_application_email", "email_to": ["jobs@acme.test"],
                                                          "sent_at": self.inside.isoformat()}))
        job(db, "employer-email", "SUBMITTED", receipt=json.dumps({"evidence_kind": "employer_confirmation_email"}))
        db.commit()
        jobs = {item["key"]: item for item in self.gather()["list"]["jobs"]}
        self.assertEqual(jobs["acknowledged"]["state"], "Waiting for confirmation")
        self.assertIsNone(jobs["acknowledged"]["receipt"])
        self.assertEqual(jobs["page"]["receipt"]["confirmation"], "Got it")
        self.assertEqual(jobs["page"]["receipt"]["at"], self.inside)
        self.assertEqual(jobs["email"]["receipt"]["to"], "jobs@acme.test")
        self.assertEqual(jobs["employer-email"]["receipt"]["words"], "Confirmation email from the employer")
        self.assertEqual(self.gather()["counts"]["submitted"], 3)

    def test_empty_and_missing_database(self):
        """Worked example 3: a fresh install has nothing and says so (the template words are tested below)."""
        empty = sqlite3.connect(":memory:")
        self.addCleanup(empty.close)
        for database in (None, empty):
            view = applications_page.gather(database, CONFIG, self.home, now=self.now)
            self.assertEqual(view["list"], {"jobs": [], "groups": [], "total": 0, "page": 1, "pages": 1})
            self.assertEqual(set(view["counts"].values()), {0})
            self.assertEqual(applications_page.gather(database, CONFIG, self.home, job="x", now=self.now)["job"], None)

    def test_pages_of_fifty(self):
        """Worked example 4."""
        for number in range(120):
            job(self.database, "job-%03d" % number, "QUEUED", updated="2026-10-21T%02d:%02d:00+00:00" % (number // 60, number % 60))
        self.database.commit()
        first = self.gather()["list"]
        self.assertEqual((len(first["jobs"]), first["total"], first["pages"]), (50, 120, 3))
        self.assertEqual(first["jobs"][0]["key"], "job-119")          # newest activity first
        self.assertEqual(len(self.gather(page=3)["list"]["jobs"]), 20)
        self.assertEqual(self.gather(page=99)["list"]["page"], 3)
        self.assertEqual(self.gather(page="junk")["list"]["page"], 1)

    def test_search_company_and_unknown_filter(self):
        db = self.database
        job(db, "a", "QUEUED", company="Acme Robotics", role="Controls Engineer")
        job(db, "b", "QUEUED", company="Globex", role="Robotics Lead")
        job(db, "c", "QUEUED", company="Initech", role="Data_Analyst 100%")
        db.commit()
        self.assertEqual(sorted(self.keys(query="robotics")), ["a", "b"])
        self.assertEqual(self.keys(query="100%"), ["c"])
        self.assertEqual(self.keys(query="%"), ["c"])          # LIKE wildcards are literal
        self.assertEqual(self.keys(query="jobs.example"), [])  # the URL in the payload is not a name
        self.assertEqual(self.keys(company="globex"), ["b"])
        self.assertEqual(self.gather(show="bogus")["show"], "all")

    def test_malformed_rows_are_skipped_or_shown_plainly(self):
        db = self.database
        job(db, "bad", "SUBMITTED", payload="{not json", receipt="{not json")
        job(db, "odd", "BLOCKED", "SOMETHING_NEW_CODE:x", payload="[1, 2]")
        db.execute("INSERT INTO application_attempts(attempt_id,job_key,idempotency_key,state,started_at,evidence) "
                   "VALUES ('t','bad','bad','WHATEVER','not a time','{not json')")
        app_event(db, "bad", "BLOCKED", "not a time", "{not json")
        db.execute("INSERT INTO application_reviews VALUES ('r','bad','2026-10-21','ELIGIBLE','{not json')")
        db.commit()
        jobs = {item["key"]: item for item in self.gather()["list"]["jobs"]}
        self.assertEqual((jobs["bad"]["company"], jobs["bad"]["role"]), ("acme robotics", "Role not recorded"))
        self.assertEqual(jobs["bad"]["receipt"]["words"], "Receipt not readable")
        self.assertEqual(jobs["bad"]["attempts"], [{"at": None, "outcome": "Ended"}])
        self.assertEqual(jobs["odd"]["problem"], "Sidedoor ran into a problem it will retry.")
        detail = self.gather(job="bad")["job"]
        self.assertEqual(detail["history"][0]["text"], "Held")
        self.assertEqual(detail["verdict"]["words"], applications_page.ELIGIBLE_WORDS)
        self.assertIsNone(RAW_CODE.search(visible_text(self.gather())))

    def test_an_older_schema_degrades_and_locked_is_raised(self):
        older = sqlite3.connect(self.root / "older.sqlite3")
        older.row_factory = sqlite3.Row
        older.execute("CREATE TABLE application_jobs (job_key TEXT PRIMARY KEY, state TEXT)")
        older.execute("INSERT INTO application_jobs VALUES ('x','QUEUED')")
        view = self.gather(database=older)
        self.assertEqual(view["list"], overview_page.UNAVAILABLE)
        self.assertEqual(self.gather(database=older, job="x")["job"], overview_page.UNAVAILABLE)
        older.close()
        locked = mock.Mock()
        locked.execute.side_effect = sqlite3.OperationalError("database is locked")
        with self.assertRaises(sqlite3.OperationalError):
            self.gather(database=locked)

    def test_reads_use_the_writers_indexes(self):
        now = self.now.isoformat()
        queries = {
            "all": ("SELECT job_key FROM application_jobs ORDER BY updated_at DESC", ()),
            "state": ("SELECT job_key FROM application_jobs WHERE state IN (?) ORDER BY updated_at DESC", ("SUBMITTED",)),
            "blocked": ("SELECT job_key FROM application_jobs WHERE state='BLOCKED' ORDER BY updated_at DESC", ()),
            "company": ("SELECT job_key FROM application_jobs WHERE employer=? ORDER BY updated_at DESC", ("acme",)),
            "counts": ("SELECT state,COUNT(*) FROM application_jobs GROUP BY state", ()),
            "attempts": ("SELECT state,started_at FROM application_attempts WHERE job_key=? ORDER BY started_at DESC", ("k",)),
            "history": ("SELECT at,event,detail FROM application_events WHERE job_key=? ORDER BY id DESC LIMIT 50", ("k",)),
            "linked": ("SELECT detail FROM application_events WHERE job_key=? AND event='SAME_POSTING_LINKED' "
                       "ORDER BY id DESC LIMIT 1", ("k",)),
            "verdict": ("SELECT observed_at,eligibility,evidence FROM application_reviews WHERE job_key=? "
                        "ORDER BY observed_at DESC LIMIT 1", ("k",)),
            "reading": ("SELECT job_key FROM desktop_requests WHERE state='CLAIMED' AND lease_until>? AND operation IN (?,?)",
                        (now, "inspect", "investigate")),
            "today": ("SELECT at,job_key FROM application_events WHERE event='SUBMISSION_VERIFIED' AND at>=?", (now,)),
        }
        for name, (sql, parameters) in queries.items():
            plan = " ".join(row[3] for row in self.database.execute("EXPLAIN QUERY PLAN " + sql, parameters))
            self.assertRegex(plan, r"^(SEARCH|SCAN) \S+ USING (COVERING )?INDEX ", name)
            self.assertNotIn("USE TEMP B-TREE", plan, name)

    def test_reading_shows_while_a_read_session_holds_the_job(self):
        db = self.database
        job(db, "reading", "QUEUED")
        job(db, "idle", "QUEUED")
        lease = (self.now + dt.timedelta(minutes=5)).isoformat()
        db.execute("INSERT INTO desktop_requests(request_id,binding,job_key,operation,payload,state,created_at,expires_at,"
                   "lease_until) VALUES ('r1','b','reading','inspect','{}','CLAIMED',?,?,?)", (now := self.now.isoformat(), now, lease))
        db.execute("INSERT INTO desktop_requests(request_id,binding,job_key,operation,payload,state,created_at,expires_at,"
                   "lease_until) VALUES ('r2','b','idle','inspect','{}','CLAIMED',?,?,?)", (now, now, now))   # lease ran out
        db.commit()
        jobs = {item["key"]: item["state"] for item in self.gather()["list"]["jobs"]}
        self.assertEqual(jobs, {"reading": "Reading now", "idle": "Queued"})
        self.assertEqual((self.gather()["counts"]["reading"], self.keys(show="reading")), (1, ["reading"]))

    def test_detail_shows_verdict_answers_resume_history_and_closing_reasons(self):
        db = self.database
        profile = {"resume": {"sha256": "c" * 64}}
        (self.home / "profile.json").write_text(json.dumps(profile))
        review = {"scope_fit": {"location": {"in_scope": False, "reason": "Onsite in Seattle, which you excluded."},
                                "level": {"in_scope": True, "reason": "Senior matches."}},
                  "questions": [{"id": "q_1", "label": "Why do you want to work here?", "required": True}]}
        job(db, "dup", "DUPLICATE", "SAME_POSTING_AS_EXISTING_JOB")
        job(db, "original", "SUBMITTED", company="Acme Robotics", role="Robotics Engineer (Remote)")
        app_event(db, "dup", "SAME_POSTING_LINKED", self.inside, {"same_as": "original", "reason": "Same title and job id."})
        app_event(db, "dup", "DUPLICATE", self.inside, {"code": "SAME_POSTING_AS_EXISTING_JOB", "route": "ORDINARY"})
        job(db, "out", "INELIGIBLE", "OUTSIDE_CONFIRMED_SCOPE")
        db.execute("INSERT INTO application_reviews VALUES ('r1','out',?,'OUTSIDE_CONFIRMED_SCOPE',?)",
                   (self.inside.isoformat(), json.dumps(review)))
        app_event(db, "out", "ELIGIBILITY_CHECKED", self.inside, {"decision": "OUTSIDE_CONFIRMED_SCOPE"})
        job(db, "gone", "ABANDONED", "APPLICATION_RECOVERY_ABANDONED")
        app_event(db, "gone", "APPLICATION_ABANDONED", self.inside, {"reason": "The posting was taken down."})
        field = applications.question_field(review["questions"][0])
        for number, sha in ((1, "c" * 64), (2, "d" * 64)):
            db.execute("INSERT INTO application_attempts(attempt_id,job_key,idempotency_key,state,started_at,evidence) "
                       "VALUES (?,?,?,?,?,?)", ("a%d" % number, "original", "original", "VERIFIED" if number == 1 else "CONFIRMED_NOT_SUBMITTED",
                                                (self.inside - dt.timedelta(hours=number)).isoformat(),
                                                json.dumps({"review": review, "answers": {field: "I like robots."}, "resume_sha256": sha})))
        db.commit()
        duplicate = self.gather(job="dup")["job"]
        self.assertEqual(duplicate["closing"], {"other": "Acme Robotics, Robotics Engineer (Remote)", "other_key": "original",
                                                 "reason": "Same title and job id."})
        self.assertEqual(duplicate["history"][0]["text"], "Closed as a duplicate: Same posting as another job Sidedoor already has.")
        out = self.gather(job="out")["job"]
        self.assertEqual(out["closed"], applications_page.CODE_WORDS["OUTSIDE_CONFIRMED_SCOPE"])
        self.assertIn({"topic": "Location", "fits": False, "reason": "Onsite in Seattle, which you excluded."}, out["verdict"]["reasons"])
        self.assertEqual(self.gather(job="gone")["job"]["closing"]["reason"], "The posting was taken down.")
        original = self.gather(job="original")["job"]
        self.assertEqual([item["outcome"] for item in original["attempts"]], ["Submitted and confirmed", "Not submitted"])
        self.assertEqual(original["attempts"][0]["answers"], [{"question": "Why do you want to work here?", "answer": "I like robots."}])
        self.assertEqual((original["attempts"][0]["resume"], original["attempts"][1]["resume"]),
                         ("Your current résumé", "An earlier résumé (version dddddddd)"))
        for key in ("dup", "out", "gone", "original"):
            self.assertIsNone(RAW_CODE.search(visible_text(self.gather(job=key))), key)
        self.assertTrue(self.gather(job="missing")["detail_missing"])

    def test_every_closed_kind_shows_its_specific_reason(self):
        db = self.database
        retain = ". Retain evidence; do not submit outside the resolved search policy"
        def closed(key, state, code, note=None, review=None):
            job(db, key, state, code)
            db.execute("UPDATE application_jobs SET next_action=? WHERE job_key=?", (note, key))
            if review is not None:
                db.execute("INSERT INTO application_reviews VALUES (?,?,?,?,?)",
                           ("r-" + key, key, self.inside.isoformat(), code, json.dumps(review)))
        hubs = "This role can be hybrid in our San Francisco, New York City, or Seattle hubs."
        closed("scope", "INELIGIBLE", "OUTSIDE_CONFIRMED_SCOPE", "AI judged level out of scope: Staff role" + retain,
               {"scope_fit": {"level": {"in_scope": False, "reason": "A staff role, above your target levels."}}})
        closed("geo", "INELIGIBLE", "OUTSIDE_CONFIRMED_SCOPE", "The read observed hiring geography excludes the candidate" + retain,
               {"facts": {"hiring_geography_includes_candidate": False},
                "browser_evidence": [{"pointer": "/facts/hiring_geography_includes_candidate", "quote": hubs}]})
        closed("shut", "INELIGIBLE", "OUTSIDE_CONFIRMED_SCOPE", "The read observed the role is not open" + retain, {"facts": {"open": False}})
        closed("abroad", "INELIGIBLE", "OUTSIDE_CONFIRMED_SCOPE", "Location DE is outside the user's countries" + retain,
               {"facts": {"country": "DE"}})
        closed("citizen", "INELIGIBLE", "CLEARANCE_OR_CITIZENSHIP_REQUIRED",
               "The posting requires US citizenship, which the owner's recorded answers do not meet. Retain evidence; do not submit.")
        closed("walled", "ABANDONED", "ACCESS_BLOCKED_3_TIMES", "Abandoned: 3 application sessions stopped (AUTHENTICATION_REQUIRED)")
        app_event(db, "walled", "APPLICATION_ACCESS_BLOCKS_SPENT", self.inside,
                  {"blocks": [{"code": "AUTHENTICATION_REQUIRED"}, {"code": "CAPTCHA_REQUIRED"}, {"code": "AUTHENTICATION_REQUIRED"}]})
        closed("recovered", "ABANDONED", "APPLICATION_RECOVERY_ABANDONED", "Retain terminal abandonment and its evidence; do not automatically reopen")
        app_event(db, "recovered", "APPLICATION_ABANDONED", self.inside, {"reason": "The posting now redirects to the careers home page"})
        closed("uploads", "EXHAUSTED", "FILE_UPLOAD_FAILED_3_TIMES", "Given up: ...")
        app_event(db, "uploads", "APPLICATION_UPLOAD_FAILURES_SPENT", self.inside, {"failures": [{}, {}, {}]})
        closed("tries", "EXHAUSTED", "RETRIES_EXHAUSTED", "Inspect failure evidence and explicitly requeue only after resolution")
        app_event(db, "tries", "RETRY_WAIT", self.inside, {"code": "INSPECTION_INCOMPLETE"})
        closed("dup", "DUPLICATE", "SAME_POSTING_AS_EXISTING_JOB", "Same posting as original; this posting is handled there")
        job(db, "original", "QUEUED")
        app_event(db, "dup", "SAME_POSTING_LINKED", self.inside, {"same_as": "original", "reason": "Same title and job id."})
        db.commit()
        expected = {
            "scope": "A staff role, above your target levels.",
            "geo": "The posting says: \u201c" + hubs + "\u201d Sidedoor judged that doesn't include you.",
            "shut": "The posting says the role is no longer open.",
            "abroad": "The role is in DE, outside the countries in your search.",
            "citizen": "The posting requires US citizenship, which your recorded answers do not meet.",
            "walled": "The application site asked for a human check or a sign-in each of the 3 times Sidedoor tried, so nothing was submitted.",
            "recovered": "The posting now redirects to the careers home page.",
            "uploads": "The site did not take the résumé or a required file in 3 tries, each stopped before anything was submitted.",
            "tries": "Its last try stopped on this: " + applications_page.CODE_WORDS["INSPECTION_INCOMPLETE"],
            "dup": "Same title and job id.",
        }
        for key, reason in expected.items():
            view = self.gather(job=key)
            self.assertEqual(view["job"]["reason"], reason, key)
            self.assertIsNone(RAW_CODE.search(visible_text(view)), key)
        self.assertEqual(self.gather(job="dup")["job"]["closing"]["other_key"], "original")
        listed = {item["key"]: item for item in self.gather()["list"]["jobs"]}
        self.assertEqual(listed["geo"]["reason"], expected["geo"])
        self.assertIsNone(listed["original"]["reason"])

    # --- review fixes A1-A9 ---------------------------------------------------------------------

    def test_a1_only_a_verified_attempt_says_submitted(self):
        db = self.database
        job(db, "live", "SUBMITTING")
        for state in ("SUBMITTING", "VERIFIED", "CONFIRMED_NOT_SUBMITTED"):
            db.execute("INSERT INTO application_attempts(attempt_id,job_key,idempotency_key,state,started_at,evidence) "
                       "VALUES (?,?,?,?,?,'{}')", (state, "live", "live", state, self.inside.isoformat()))
        db.commit()
        outcomes = {item["outcome"] for item in self.gather()["list"]["jobs"][0]["attempts"]}
        self.assertIn("Submitting, outcome being checked", outcomes)
        self.assertEqual([words for words in outcomes if words.startswith("Submitted")], ["Submitted and confirmed"])

    def test_a2_every_state_the_pipeline_writes_has_words_and_a_filter(self):
        root = Path(applications.__file__).resolve().parent
        source = "".join((root / name).read_text() for name in ("applications.py", "application_recovery.py", "conversion.py"))
        written = set(re.findall(r"UPDATE application_jobs SET state='([A-Z_]+)'", source))
        written |= set(re.findall(r'(?:hold|wait_for_adapter)\((?:[^()]|\([^()]*\))*?\bnow, "([A-Z_]+)"', source, re.S))
        written |= {"INELIGIBLE"}   # assess() returns it to hold through a variable
        self.assertIn("WAITING_BROWSER", written)
        filtered = {state for states in applications_page.STATE_FILTERS.values() for state in states} | {"BLOCKED", "RETRY_WAIT"}
        for state in written:
            self.assertTrue(state in applications_page.STATE_WORDS or state == "BLOCKED", state)
            self.assertIn(state, filtered, state)
        db = self.database
        job(db, "browser", "WAITING_BROWSER", "DESKTOP_BROWSER_PENDING", next_at="2026-10-22T16:00:00+00:00")
        app_event(db, "browser", "WAITING_BROWSER", self.inside, {"code": "DESKTOP_BROWSER_PENDING", "route": "ORDINARY"})
        for number, state in enumerate(sorted(written - {"WAITING_BROWSER"})):
            job(db, "s%d" % number, state, "AUTHENTICATION_REQUIRED" if state == "BLOCKED" else None)
        job(db, "owner", "BLOCKED", "DESIGNATED_RESUME_MISMATCH")
        db.commit()
        item = self.gather(show="waiting_browser")["list"]["jobs"][0]
        self.assertEqual(item["state"], "Waiting for the browser")
        self.assertTrue(item["todo"].startswith("Sidedoor will continue when the browser is free"))
        self.assertEqual(self.gather(job="browser")["job"]["history"][0]["text"],
                         "Waiting for the browser: The browser has not picked this up yet.")
        counts = self.gather()["counts"]
        job(db, "later", "RETRY_WAIT", "PREFLIGHT_TRANSIENT")   # "Waiting to retry", not "Queued"
        db.commit()
        disjoint = ("needs_you", "retrying", "confirming", "submitted", "queued", "waiting_browser", "ineligible",
                    "duplicate", "given_up")
        self.assertEqual(sum(counts[key] for key in disjoint), counts["all"])

    def test_a3_a_malformed_receipt_kind_never_breaks_the_list(self):
        job(self.database, "odd", "SUBMITTED", receipt=json.dumps({"evidence_kind": ["page_confirmation"]}))
        self.database.commit()
        self.assertEqual(self.gather()["list"]["jobs"][0]["receipt"]["words"], "Receipt not readable")

    def test_a4_an_unreadable_form_is_described_truthfully(self):
        blocker = "UNKNOWN_REQUIRED_ANSWERS:" + __import__("decision_transport").FORM_NOT_READABLE_FIELD
        self.assertEqual(blocker, applications.FORM_NOT_READABLE_BLOCKER)
        job(self.database, "unreadable", "BLOCKED", blocker)
        self.database.commit()
        item = self.gather()["list"]["jobs"][0]
        self.assertTrue(item["needs_you"])
        self.assertEqual(item["problem"], applications_page.FORM_NOT_READABLE_WORDS)
        self.assertNotIn("only you can answer", item["todo"])
        self.assertNotIn("apply yourself", item["todo"])
        self.assertIn("couldn't read this form", item["todo"])

    def test_a4c_only_a_sign_in_or_human_check_says_apply_yourself(self):
        job(self.database, "gaveup", "ABANDONED", "ACCESS_BLOCKED_3_TIMES")
        job(self.database, "other", "BLOCKED", "DESIGNATED_RESUME_MISMATCH")
        self.database.commit()
        todos = {item["key"] if "key" in item else item.get("job_key"): item["todo"] for item in self.gather(show="all")["list"]["jobs"]}
        texts = list(todos.values())
        self.assertTrue(any(text and "apply yourself" in text for text in texts))
        self.assertEqual(sum(1 for text in texts if text and "apply yourself" in text), 1)

    def test_a4b_answered_owner_questions_never_say_apply_yourself(self):
        # 2026-10-24: InsForge after the owner answered both cards showed "apply yourself", which could
        # make the owner apply by hand while Sidedoor submits (a double application).
        job(self.database, "answered", "BLOCKED", "UNKNOWN_REQUIRED_ANSWERS:unidentified-question:github_profile:5b77")
        self.database.commit()
        item = self.gather()["list"]["jobs"][0]
        self.assertNotIn("apply yourself", item["todo"])
        self.assertIn("continues this application on its next run", item["todo"])

    def test_a5_the_authorization_hold_links_to_no_missing_control(self):
        job(self.database, "auth", "BLOCKED", "APPLICATION_AUTHORIZATION_REQUIRED")
        self.database.commit()
        item = self.gather()["list"]["jobs"][0]
        self.assertIsNone(item["todo_href"])
        self.assertIn("support@yoursidedoor.com", item["todo"])

    def test_a6_search_finds_non_ascii_text_and_quotes(self):
        job(self.database, "cafe", "QUEUED", company="Café Robotics", role='The "Hands-on" Engineer')
        self.database.commit()
        self.assertEqual(self.keys(query="café"), ["cafe"])
        self.assertEqual(self.keys(query='"hands-on"'), ["cafe"])

    def test_a7_odd_history_route_and_questions_never_break_the_detail(self):
        db = self.database
        job(db, "odd", "BLOCKED", "AUTHENTICATION_REQUIRED")
        app_event(db, "odd", "BLOCKED", self.inside, {"code": "AUTHENTICATION_REQUIRED", "route": ["OWNER"]})
        db.execute("INSERT INTO application_attempts(attempt_id,job_key,idempotency_key,state,started_at,evidence) "
                   "VALUES ('a','odd','odd','CONFIRMED_NOT_SUBMITTED',?,?)",
                   (self.inside.isoformat(), json.dumps({"review": {"questions": 5}, "answers": {"f": "x"}})))
        db.commit()
        detail = self.gather(job="odd")["job"]
        self.assertEqual(detail["history"][0]["text"], "Held: The application site asked for a sign-in.")
        self.assertEqual(detail["attempts"][0]["answers"], [{"question": "A form question", "answer": "x"}])

    def test_a9_rows_under_reading_are_labelled_reading_now(self):
        db = self.database
        job(db, "wall", "BLOCKED", "CAPTCHA_REQUIRED")
        job(db, "owner", "BLOCKED", "DESIGNATED_RESUME_MISMATCH")
        lease = (self.now + dt.timedelta(minutes=5)).isoformat()
        for key in ("wall", "owner"):
            db.execute("INSERT INTO desktop_requests(request_id,binding,job_key,operation,payload,state,created_at,expires_at,"
                       "lease_until) VALUES (?,'b',?,'investigate','{}','CLAIMED',?,?,?)", (key, key, lease, lease, lease))
        db.commit()
        view = self.gather(show="reading")
        self.assertEqual([item["key"] for item in view["list"]["jobs"]], ["wall"])   # the Reading chip counts by the badge
        self.assertEqual((view["list"]["jobs"][0]["state"], view["counts"]["reading"]), ("Reading now", 1))
        jobs = {item["key"]: item for item in self.gather()["list"]["jobs"]}
        self.assertEqual((jobs["owner"]["state"], jobs["owner"]["reading"]), ("Blocked, needs you", True))


    def test_p1_owner_answers_read_calm_and_jobs_sit_under_headings(self):
        """Owner, 2026-10-24: waiting on an answer is calm with a button; an answered one continues on
        its own; the list is grouped Needs you, In progress, Submitted, Closed across pages."""
        db = self.database
        job(db, "asked", "BLOCKED", "UNKNOWN_REQUIRED_ANSWERS:salary", updated="2026-10-21T10:00:00+00:00")
        job(db, "answered", "BLOCKED", "UNKNOWN_REQUIRED_ANSWERS:start", updated="2026-10-21T18:00:00+00:00")
        job(db, "resume", "BLOCKED", "DESIGNATED_RESUME_MISMATCH", updated="2026-10-21T09:00:00+00:00")
        job(db, "queued", "QUEUED", updated="2026-10-21T19:00:00+00:00")
        job(db, "done", "SUBMITTED", receipt=json.dumps({"evidence_kind": "page_confirmation", "armed_at": self.inside.isoformat()}),
            updated="2026-10-21T20:00:00+00:00")
        job(db, "gone", "EXHAUSTED", "FILE_UPLOAD_FAILED_3_TIMES", updated="2026-10-21T21:00:00+00:00")
        job(db, "copy", "DUPLICATE", "EXISTING_APPLICATION", updated="2026-10-21T22:00:00+00:00")
        question(db, "q", "asked", self.now)
        db.commit()
        listing = self.gather()["list"]
        jobs = {item["key"]: item for item in listing["jobs"]}
        asked, answered = jobs["asked"], jobs["answered"]
        self.assertEqual((asked["state"], asked["tone"], asked["attention"], asked["needs_you"]),
                         ("Waiting for your answer", "info", False, False))   # a question only (one rule)
        self.assertEqual((asked["todo_href"], asked["todo_label"]), ("/approvals#questions", "Answer on Approvals"))
        self.assertEqual((answered["state"], answered["attention"], answered["group"]), ("Continues on the next run", False, "progress"))
        self.assertTrue(jobs["resume"]["attention"])
        self.assertEqual(jobs["done"]["submitted_at"], self.inside)
        self.assertEqual([item["key"] for item in listing["jobs"]], ["resume", "queued", "answered", "asked", "done", "copy", "gone"])
        self.assertEqual([(group["label"], len(group["jobs"])) for group in listing["groups"]],
                         [("Needs you", 1), ("In progress", 3), ("Submitted", 1), ("Closed", 2)])
        self.assertEqual(self.gather()["counts"]["needs_you"], 1)      # the one needs-you rule: only the résumé
        with mock.patch.object(applications_page, "PAGE_SIZE", 3):
            pages = [self.gather(page=number)["list"] for number in (1, 2, 3)]
        self.assertEqual([[group["key"] for group in item["groups"]] for item in pages],
                         [["needs_you", "progress"], ["progress", "submitted", "closed"], ["closed"]])


@unittest.skipUnless(DEPENDENCIES, "dashboard packages (fastapi, httpx) not installed")
class ApplicationsPageTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.home = self.root / "home"
        finished_home(self.home)

    def tearDown(self):
        self.directory.cleanup()

    def client(self):
        app = dashboard.create_app(SECRET, PORT, home=self.home, root=self.root, config=CONFIG)
        client = TestClient(app, base_url=f"http://127.0.0.1:{PORT}", follow_redirects=False)
        client.get("/", params={"t": SECRET})
        return client

    def get(self, path="/applications", **params):
        with mock.patch.object(setup_steps, "chrome_path", return_value="/chrome"):
            return self.client().get(path, params=params)

    def test_fresh_install_says_why_and_what_next(self):
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertIn("No applications yet. Sidedoor applies once research finds matching roles", response.text)
        self.assertNotIn("This page is not built yet.", response.text)
        self.assertFalse((self.root / "state").exists())   # a GET never creates the database

    def test_get_writes_nothing_and_never_fails_on_odd_input(self):
        database = database_with_schema(self.root)
        now = dt.datetime.now(dt.timezone.utc)
        job(database, "spent", "BLOCKED", "SUBMISSION_OUTCOME_UNKNOWN", reconcile=applications.RECONCILE_MAX_ATTEMPTS)
        job(database, "bad", "QUEUED", payload="{not json", updated=now.isoformat())
        job(database, "evil", "QUEUED", url="javascript:alert(1)", role="<script>x</script>")
        job(database, "odd-receipt", "SUBMITTED", receipt=json.dumps({"evidence_kind": {"a": 1}}))
        database.commit()
        database.close()
        path = self.root / "state" / "orchestrator.sqlite3"
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        for params in ({}, {"show": "needs_you"}, {"show": "bogus", "page": "-4"}, {"q": "%_"}, {"company": "nobody"},
                       {"job": "spent"}, {"job": "missing"}, {"page": "x" * 50}):
            response = self.get(**params)
            self.assertEqual(response.status_code, 200, params)
            self.assertIsNone(RAW_CODE.search(response.text.split("<main", 1)[1]), params)
        page = self.get().text
        self.assertNotIn("javascript:alert", page)
        self.assertNotIn("<script>x</script>", page)
        self.assertIn("Receipt not readable", page)                                  # A3: the page still renders
        self.assertIn("to see whether it went through", page)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["orchestrator.sqlite3"])

    def test_a_junk_file_shows_the_page(self):
        (self.root / "state").mkdir()
        (self.root / "state" / "orchestrator.sqlite3").write_bytes(os.urandom(4096))
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertIn("Sidedoor couldn't read its records.", response.text)
        self.assertIn("Not available", response.text)

    def test_overview_links_land_on_the_matching_list(self):
        """Worked example 5: "Blocked, needs you: N" leads to exactly those N jobs."""
        database = database_with_schema(self.root)
        job(database, "one", "BLOCKED", "REQUIRED_ELIGIBILITY_ANSWER", company="One Co")
        job(database, "two", "BLOCKED", "DESIGNATED_RESUME_MISMATCH", company="Two Co")
        job(database, "three", "BLOCKED", "SUBMISSION_OUTCOME_UNKNOWN", company="Three Co",
            reconcile=applications.RECONCILE_MAX_ATTEMPTS)
        job(database, "wall", "BLOCKED", "CAPTCHA_REQUIRED", company="Wall Co")
        database.commit()
        database.close()
        overview = self.get("/").text
        for show in ("needs_you", "retrying", "confirming", "submitted_today"):
            self.assertIn('href="/applications?show=%s"' % show, overview)
        # Overview's one Needs you card (owner, 2026-10-24): "3 applications blocked on you" [Open].
        needs = overview[overview.index('id="needs-title"'):overview.index('id="now-title"')]
        self.assertRegex(needs, r'<span class="overview-number">3</span>\s*<span class="overview-needs-what">applications blocked on you</span>'
                                r'\s*<a class="button overview-needs-go" href="/applications\?show=needs_you">Open</a>')
        self.assertNotIn("Blocked, needs you", overview)
        page = self.get(show="needs_you").text
        self.assertEqual(page.count('class="card app-row'), 3)
        for company in ("One Co", "Two Co", "Three Co"):
            self.assertIn(company, page)
        self.assertNotIn("Wall Co", page)

    def test_p2_page_hides_empty_filters_and_explains_closed_jobs(self):
        database = database_with_schema(self.root)
        job(database, "original", "SUBMITTED", company="One Co", role="Engineer",
            receipt=json.dumps({"evidence_kind": "page_confirmation", "armed_at": "2026-10-21T17:00:00+00:00"}))
        job(database, "copy", "DUPLICATE", "SAME_POSTING_AS_EXISTING_JOB", company="One Co", role="Engineer II")
        app_event(database, "copy", "SAME_POSTING_LINKED", "2026-10-21T17:00:00+00:00", {"same_as": "original"})
        job(database, "recorded", "DUPLICATE", "EXISTING_APPLICATION", company="Two Co")
        job(database, "gone", "ABANDONED", "ACCESS_BLOCKED_3_TIMES", company="Three Co")
        database.commit()
        database.close()
        page = self.get().text
        self.assertIn(">Duplicate <span", page)
        self.assertNotIn(">Queued <span", page)                                   # 0 jobs: hidden
        self.assertIn(">Queued <span", self.get(show="queued").text)              # unless selected
        self.assertIn('Same job as: <a href="/applications?job=original">One Co, Engineer</a>', page)
        self.assertIn("Same job as an earlier application", page)
        self.assertIn("The site asked for a sign-in or a human check three times.", page)
        self.assertIn("Submitted ", page.split("One Co")[1])
        for heading in ("Submitted", "Closed"):
            self.assertIn('class="app-group-title" id="app-group-%s"' % heading.lower(), page)


class PhoneWidthTest(unittest.TestCase):
    def test_applications_rules_fit_a_phone(self):
        css = CSS.read_text()
        rules = css[css.index("/* Applications (E6.T6) */"):]
        for needed in (".app-filters { display: flex; flex-wrap: wrap;", "overflow-wrap: anywhere", "min-width: 0",
                       "@media (max-width: 640px)", ".app-answers { grid-template-columns: 1fr; }"):
            self.assertIn(needed, rules)


if __name__ == "__main__":
    unittest.main()
