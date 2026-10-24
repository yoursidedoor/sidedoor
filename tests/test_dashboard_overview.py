"""The Overview page (E6.T3; E6.T0 spec section 5): display only, from stored records.

`overview_page.gather` is plain Python and runs on the stdlib suite; the page tests need the
dashboard packages (fastapi, httpx) and are skipped without them.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
import hashlib
import importlib.util
import json
import re
import os
import sqlite3
import tempfile
import unittest
import zoneinfo
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import applications
import daily_schedule
import decision_transport
import orchestrator
import outreach
import overview_page
import research_workflow
import setup_steps
import workflow_tasks

DEPENDENCIES = all(importlib.util.find_spec(name) for name in ("fastapi", "httpx"))
if DEPENDENCIES:
    from fastapi.testclient import TestClient

    import dashboard


PORT = 54321
SECRET = "test-token-value"
PACIFIC = ZoneInfo("America/Los_Angeles")
CONFIG = {"timezone": "America/Los_Angeles", "day_start_hour": 9, "research_hour": 9, "daily_compute_seconds": 21000,
          "text_model": {"model": "test/model"}}
CSS = Path(__file__).resolve().parent.parent / "dashboard_assets" / "static" / "dashboard.css"


def pacific(*parts):
    return dt.datetime(*parts, tzinfo=PACIFIC).astimezone(dt.timezone.utc)


def finished_home(home, openrouter=("done", "Key works."), linkedin=True):
    """A home whose setup finished, with the stored checks Setup leaves behind."""
    def change(state):
        state["setup_finished_at"] = "2026-10-21T16:00:00Z"
        state["email_check_passed_at"] = "2026-10-21T16:00:00Z"
        state["locked_email"] = "user@example.com"
        if openrouter:
            state["steps"]["openrouter"] = {"status": openrouter[0], "message": openrouter[1], "at": "2026-10-22T17:02:00Z"}
        else:
            state["steps"].pop("openrouter", None)
        if linkedin:
            state["steps"]["linkedin"] = {"status": "done", "message": "LinkedIn connected: Test.", "offer": "reconnect",
                                          "at": "2026-10-21T16:00:00Z"}
        else:
            state["steps"].pop("linkedin", None)
        state["steps"]["email"] = {"status": "done", "message": "Matches.", "at": "2026-10-21T16:00:00Z"}
    setup_steps.update_state(change, home)


def database_with_schema(root):
    """The product's own schema at <root>/state/orchestrator.sqlite3 (WAL), from the writers' setup."""
    (Path(root) / "state").mkdir(exist_ok=True)
    database = orchestrator.connect(Path(root))
    outreach.initialize_actions(database)          # outreach_actions + engagement_events + workflow tasks
    applications.initialize(database)
    decision_transport.initialize(database)
    research_workflow.initialize(database)
    daily_schedule.initialize(database, "America/Los_Angeles")
    database.commit()
    return database


def event(database, kind, purpose, occurred, source_id, **extra):
    payload = dict(extra, kind=kind, purpose=purpose, verified=True)
    database.execute("INSERT INTO engagement_events VALUES (?,?,?,?,?,?,?,?,?)",
                     ("unipile", source_id, "linkedin.com/in/" + source_id, kind, occurred.isoformat(),
                      occurred.isoformat(), "{}", "null", json.dumps(payload)))


def listed(database, slug):
    """Make this person one Networking & Referrals lists (a referral on record), so their replies count
    on Overview (E6.T5 N11: a stranger who only wrote first is not counted)."""
    import referrals
    referrals.initialize(database)
    database.execute("INSERT OR IGNORE INTO referrals VALUES (?,?,?,?,?,?,?,?)",
                     ("linkedin.com/in/" + slug, "AGREED", "s", "said", None, None, "2026-10-01T00:00:00+00:00", "[]"))


def job(database, key, state, blocker=None):
    database.execute("INSERT INTO application_jobs(job_key,employer,requisition,payload,state,blocker,updated_at) "
                     "VALUES (?,?,?,?,?,?,?)", (key, "acme", key, "{}", state, blocker, "2026-10-21T17:00:00+00:00"))


def question(database, identifier, job_key, now, state="PENDING", kind="fact", expires=None):
    expires = expires if expires is not None else int((now + dt.timedelta(days=1)).timestamp() * 1000)
    database.execute("""INSERT INTO user_decisions(id,job_key,field,semantic,answer_type,choices,payload_hash,job_hash,
                        policy_hash,expires_at,state,summary,event_key,kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (identifier, job_key, "f", "s", "text", "[]", "h", "h", "h", expires, state, "?", identifier, kind))


def invite_action(database, action_id, state, claimed, started, payload=None, result=None):
    database.execute("INSERT INTO outreach_actions VALUES (?,?,?,?,?,?,?,?)",
                     (action_id, "linkedin.com/in/" + action_id, "INITIAL:" + action_id,
                      payload if payload is not None else json.dumps({"purpose": "INVITE"}), "h", state,
                      claimed.isoformat() if claimed else None,
                      result if result is not None else json.dumps({"attempts": [{"started_at": started.isoformat()}]})))


class GatherTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        # A cleanup, not tearDown: it then runs after the connections a test closes with addCleanup
        # (an open database keeps its file in use on Windows).
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.home = self.root / "home"
        finished_home(self.home)
        self.database = database_with_schema(self.root)
        self.now = pacific(2026, 10, 22, 8, 30)          # Thursday 08:30: the day began yesterday 09:00
        self.inside, self.before = pacific(2026, 10, 21, 10, 0), pacific(2026, 10, 21, 8, 59)

    def tearDown(self):
        self.database.close()

    def gather(self, running=False, database=None, now=None):
        return overview_page.gather(database or self.database, CONFIG, self.home, setup_steps.progress(self.home),
                                    running, now=now or self.now)

    def test_service_day_counts_not_calendar_midnight(self):
        db = self.database
        event(db, "initial_sent", "INVITE", self.inside, "a")
        event(db, "initial_sent", "INVITE", pacific(2026, 10, 22, 0, 30), "b")   # after midnight, same service day
        event(db, "initial_sent", "INVITE", self.before, "c")                     # the previous service day
        event(db, "initial_sent", "FIRST_MESSAGE", self.inside, "d")
        event(db, "followup_sent", "FOLLOWUP", self.inside, "e")
        event(db, "outbound_observed", "REPLY", self.inside, "f")
        event(db, "outbound_observed", None, self.inside, "g")                    # a hand-written message is not ours
        event(db, "inbound", None, self.inside, "h")
        listed(db, "h")
        event(db, "inbound", None, self.inside, "cold")                          # a stranger who wrote first
        event(db, "inbound", None, self.before, "i")
        # An acknowledged invitation whose delivery is unconfirmed is never sent, and not "still sending" either:
        # Networking badges it "Invitation sent" (owner 2026-10-24: the link counts by the badge).
        invite_action(db, "x", "ACKNOWLEDGED_DELIVERY_UNCONFIRMED", self.inside, self.inside)
        db.execute("INSERT INTO application_events(job_key,at,event,detail) VALUES ('j1',?,'SUBMISSION_VERIFIED','{}')", (self.inside.isoformat(),))
        db.execute("INSERT INTO application_events(job_key,at,event,detail) VALUES ('j0',?,'SUBMISSION_VERIFIED','{}')", (self.before.isoformat(),))
        db.execute("INSERT INTO worker_compute_reservations VALUES ('a','2026-10-22',600,?)", (self.inside.isoformat(),))
        db.execute("INSERT INTO worker_compute_reservations VALUES ('b','2026-10-21',900,?)", (self.before.isoformat(),))
        db.execute("INSERT INTO rw_companies VALUES ('2026-10-21','acme','plan-1')")
        db.execute("INSERT INTO rw_companies VALUES ('2026-10-21','other','unplanned:x')")
        db.execute("INSERT INTO rw_companies VALUES ('2026-10-20','old','plan-0')")
        db.execute("INSERT INTO research VALUES ('r1',?,'COMPLETE',NULL)", (pacific(2026, 10, 21, 9, 0).isoformat(),))
        db.execute("INSERT INTO research_days VALUES ('2026-10-21','COMPLETE',1,'r1',NULL,?,0)", (self.inside.isoformat(),))
        db.commit()
        view = self.gather()
        self.assertEqual(view["outreach"], {"invitations": 2, "cap": 20, "waiting": 0, "messages": 3, "replies": 1})
        self.assertEqual(view["submitted"], 1)
        self.assertEqual(view["compute"]["used"], 600)
        self.assertEqual(view["compute"]["resets"], pacific(2026, 10, 22, 9, 0))
        self.assertEqual(view["research"]["companies"], 1)
        self.assertEqual(view["research"]["last"]["words"], "finished")
        self.assertEqual(view["research"]["next"]["at"], pacific(2026, 10, 22, 9, 0))

    def test_research_companies_roles_and_contacts_found(self):
        """3 companies, 4 roles (1 judged fit NONE and dropped), 2 drafted contacts plus 1 held,
        and 1 person the contact finder added for that research run."""
        db = self.database
        self.assertEqual({key: self.gather()["research"][key] for key in ("companies", "roles", "contacts")},
                         {"companies": None, "roles": None, "contacts": None})          # no run: "Not run yet"
        for company, plan in (("acme", "plan-1"), ("beta", "plan-1"), ("gamma", "plan-2"), ("seen", "unplanned:x")):
            db.execute("INSERT INTO rw_companies VALUES ('2026-10-21',?,?)", (company, plan))
        db.execute("INSERT INTO rw_companies VALUES ('2026-10-20','old','plan-0')")
        def result(identifier, plan, kept, dropped=()):
            processed = {"opportunities": [{"job_key": key} for key in kept],
                         "rejected_opportunities": [{"job_key": key, "reason": "ROLE_FIT_NONE"} for key in dropped]}
            db.execute("INSERT INTO rw_results VALUES (?,?,?,?,?)",
                       (identifier, plan, self.inside.isoformat(), "{}", json.dumps(processed)))
        result("res-1", "plan-1", ["acme:1", "acme:2"], ["acme:3"])
        result("res-1b", "plan-1", ["acme:1"])                       # a retried worker: same role once
        result("res-2", "plan-2", ["gamma:1"])
        result("res-0", "plan-0", ["old:1"])                         # another day
        for company, identity, plan, historical in (("acme", "p1", "plan-1", 0), ("beta", "p2", "plan-1", 0),
                                                    ("gamma", "p3", "plan-2", 0), ("gamma", "p4", "plan-2", 1),
                                                    ("acme", "p5", "linkedin-contacts-plan:x", 0),
                                                    ("old", "p6", "plan-0", 0)):
            db.execute("INSERT INTO rw_contacts VALUES (?,?,?,?,?)", (company, identity, identity, historical, plan))
        for identity in ("p1", "p2"):                                # p3 is held (reserved, no draft)
            db.execute("INSERT INTO rw_drafts VALUES (?,1,?,'{}')", (identity, identity))
        db.execute("CREATE TABLE linkedin_contact_runs (run_id TEXT PRIMARY KEY, company TEXT NOT NULL, started_at TEXT NOT NULL,"
                   " state TEXT NOT NULL, request_sha256 TEXT NOT NULL, plan_id TEXT, finished_at TEXT, result TEXT)")
        db.execute("INSERT INTO linkedin_contact_runs VALUES ('f1','acme',?,'COMPLETE','h','linkedin-contacts-plan:x',?,?)",
                   (self.inside.isoformat(), self.inside.isoformat(), json.dumps({"research_result_id": "res-1"})))
        db.execute("INSERT INTO research_days VALUES ('2026-10-21','COMPLETE',1,'r1',NULL,?,0)", (self.inside.isoformat(),))
        db.commit()
        research = self.gather()["research"]
        self.assertEqual((research["companies"], research["roles"], research["contacts"]), (3, 3, 4))
        db.execute("DELETE FROM rw_companies")
        db.commit()
        research = self.gather()["research"]                        # a run that found nothing: zeros
        self.assertEqual((research["companies"], research["roles"], research["contacts"]), (0, 0, 0))

    def test_o10_since_yesterday_before_the_day_start(self):
        self.assertEqual(self.gather()["day_start"], "yesterday 09:00")
        self.assertEqual(self.gather(now=pacific(2026, 10, 22, 10, 0))["day_start"], "09:00")

    def test_o3_initial_is_an_invitation_only_from_a_campaign(self):
        event(self.database, "initial_sent", "INITIAL", self.inside, "chat")                       # a direct message
        event(self.database, "initial_sent", "INITIAL", self.inside, "campaign", campaign_id=77,
              preparation_action_id="prep-1")                                                      # the campaign invitation
        event(self.database, "initial_sent", "PREPARE_CAMPAIGN", self.inside, "dead")              # not an invitation purpose
        self.database.commit()
        outreach = self.gather()["outreach"]
        self.assertEqual((outreach["invitations"], outreach["messages"]), (1, 2))

    def test_o2_o5_o6_owner_holds_questions_and_automatic_waits(self):
        db = self.database
        job(db, "eligibility", "BLOCKED", "REQUIRED_ELIGIBILITY_ANSWER")
        job(db, "login-1", "BLOCKED", "AUTHENTICATION_REQUIRED")
        job(db, "login-2", "BLOCKED", "CAPTCHA_REQUIRED")
        job(db, "answers", "BLOCKED", "UNKNOWN_REQUIRED_ANSWERS:salary")
        job(db, "confirming", "RECONCILE", "SUBMISSION_OUTCOME_UNKNOWN")
        job(db, "done", "SUBMITTED")
        question(db, "q1", "eligibility", self.now)
        question(db, "q2", "eligibility", self.now, kind="test")
        question(db, "q3", "answers", self.now, state="ANSWERED")
        question(db, "q4", "answers", self.now, expires=1)
        # The stale owner column says "user" for login walls; the route decides, not it.
        for key, code in (("login-1", "AUTHENTICATION_REQUIRED"), ("login-2", "CAPTCHA_REQUIRED")):
            db.execute("INSERT INTO application_blockers VALUES (?,?,'user','Sign in',1,NULL,'2026-10-21T17:00:00+00:00')", (key, code))
        db.commit()
        view = self.gather()
        self.assertEqual(view["right_now"], {"confirming": 1, "blocked": 2, "retrying": 2})
        # The one needs-you rule: the asked job is a question only, the answered one needs nothing.
        self.assertEqual(view["needs"], {"questions": 1, "applications": 0, "messages": 0, "couldnt_send": 0, "notes": 0})

    def test_o19_spent_reconcile_reads_need_the_owner(self):
        db = self.database
        job(db, "spent", "BLOCKED", "SUBMISSION_OUTCOME_UNKNOWN")
        job(db, "still-reading", "BLOCKED", "SUBMISSION_OUTCOME_UNKNOWN")
        db.execute("UPDATE application_jobs SET reconcile_attempts=? WHERE job_key='spent'", (applications.RECONCILE_MAX_ATTEMPTS,))
        db.execute("UPDATE application_jobs SET reconcile_attempts=4 WHERE job_key='still-reading'")
        db.commit()
        view = self.gather()
        self.assertEqual(view["right_now"], {"confirming": 0, "blocked": 1, "retrying": 1})
        self.assertEqual(view["needs"], {"questions": 0, "applications": 1, "messages": 0, "couldnt_send": 0, "notes": 0})

    def scheduler(self, scheduler, pause=None):
        if pause:
            import scheduler_control
            scheduler_control.set_user_pause(self.database, True, now="2026-10-22T15:00:00+00:00") if pause == "user" else \
                scheduler_control.set_license_pause(self.database, True, now="2026-10-22T15:00:00+00:00")
        return self.gather(running=scheduler)["health"][-1]

    def test_scheduler_running_offers_pause_and_shows_problems_plainly(self):
        item = self.scheduler({"running": True})
        self.assertEqual((item["tone"], item["fix"]), ("success", {"label": "Pause", "post": "/pause"}))
        item = self.scheduler({"running": True, "last_error": "start run failed: OSError: boom",
                               "job_failures": {"research": {"exit_code": 1, "at": "2026-10-22T15:20:00+00:00"}}})
        self.assertEqual(item["tone"], "warning")
        texts = [note["text"] for note in item["notes"]]
        self.assertIn("Sidedoor ran into a problem keeping its schedule.", texts)
        self.assertIn("Research last stopped with an error at 08:20.", texts)
        self.assertEqual(item["notes"][0]["detail"], "start run failed: OSError: boom")
        self.assertEqual(item["notes"][-1]["href"], "/failures#scheduler")

    def test_o27_see_what_failed_whatever_the_state(self):
        crashed = {"job_failures": {"research": {"exit_code": 1, "at": "2026-10-22T15:20:00+00:00"}}}
        for scheduler, pause in (({"running": False, **crashed}, None), ({"running": True, **crashed}, "license"),
                                 ({"running": False, "last_error": "tick failed: X"}, None)):
            item = self.scheduler(scheduler, pause)
            self.assertIn({"text": "See what failed.", "href": "/failures#scheduler"}, item["notes"], (scheduler, pause))
        self.assertEqual(self.scheduler({"running": False})["notes"], [])

    def test_scheduler_user_pause_offers_resume(self):
        item = self.scheduler({"running": True}, pause="user")
        self.assertEqual(item["text"], "Paused by you since 08:00. No cycle yet")
        self.assertEqual(item["fix"], {"label": "Resume", "post": "/resume"})

    def test_scheduler_license_pause_never_offers_resume(self):
        item = self.scheduler({"running": True}, pause="license")
        self.assertEqual(item["tone"], "error")
        self.assertEqual(item["fix"]["label"], "Open License")

    def test_e8t3_license_pause_keeps_pause_available_and_shows_the_users_own_pause(self):
        item = self.scheduler({"running": True}, pause="license")
        self.assertEqual(item["also"], {"label": "Pause", "post": "/pause"})   # section 5: [Pause] at all times
        import scheduler_control
        scheduler_control.set_user_pause(self.database, True, now="2026-10-22T15:00:00+00:00")
        item = self.gather(running={"running": True})["health"][-1]
        self.assertEqual(item["fix"]["label"], "Open License")                 # never [Resume]
        self.assertNotIn("also", item)
        self.assertIn("Also paused by you since 08:00.", [note["text"] for note in item["notes"]])

    def test_loop_state_reads_the_loop_without_the_database(self):
        import scheduler_loop
        loop = mock.Mock(last_error="tick failed: X", job_failures={"applications": {"exit_code": 2, "at": "x"}})
        with mock.patch.object(scheduler_loop, "current", return_value=loop), \
                mock.patch.object(scheduler_loop, "pause_state", side_effect=AssertionError("opens the database")):
            self.assertEqual(overview_page.loop_state(True), {"running": True, "last_error": "tick failed: X",
                                                             "job_failures": {"applications": {"exit_code": 2, "at": "x"}}})
        with mock.patch.object(scheduler_loop, "current", return_value=None):
            self.assertEqual(overview_page.loop_state(False), {"running": False, "last_error": None, "job_failures": {}})

    def test_o7_plain_words_for_state_codes(self):
        db = self.database
        db.execute("INSERT INTO research VALUES ('r1',?,'FAILED',NULL)", (self.inside.isoformat(),))
        db.execute("INSERT INTO research_days VALUES ('2026-10-21','RETRY_WAIT',1,'r1',NULL,NULL,0)")
        db.execute("INSERT INTO runs VALUES ('s1',?,'DEGRADED',?,NULL)", (self.inside.isoformat(), self.inside.isoformat()))
        db.commit()
        view = self.gather()
        self.assertEqual(view["research"]["last"]["words"], "will retry")
        scheduler = view["health"][-1]
        self.assertEqual(scheduler["text"], "Not running. Last cycle finished with problems")
        db.execute("UPDATE research_days SET state='EXHAUSTED'")
        db.execute("UPDATE runs SET state='SOMETHING_NEW'")
        db.commit()
        view = self.gather()
        self.assertEqual(view["research"]["last"]["words"], "gave up for today")
        self.assertNotIn("SOMETHING_NEW", view["health"][-1]["text"].upper())
        self.assertEqual(view["health"][-1]["text"], "Not running. Last cycle ended")

    def test_o11_reads_use_the_writers_indexes(self):
        start = (self.now - dt.timedelta(days=1)).date().isoformat()
        queries = {
            "engagement_events": ("SELECT occurred_at,kind,payload FROM engagement_events WHERE occurred_at>=? "
                                  "AND kind IN ('initial_sent','followup_sent','outbound_observed','inbound')", (start,)),
            "application_events": ("SELECT at FROM application_events WHERE event='SUBMISSION_VERIFIED' AND at>=?", (start,)),
            "runs": ("SELECT started_at,state,completed_at FROM runs ORDER BY started_at DESC LIMIT 1", ()),
            "research": ("SELECT id,started_at,state FROM research ORDER BY started_at DESC LIMIT 1", ()),
            "outreach_actions state": ("SELECT payload FROM outreach_actions WHERE state IN (?,?,?)", overview_page.INVITE_WAITING),
            "outreach_actions claimed_at": ("SELECT claimed_at,payload,result FROM outreach_actions WHERE result IS NOT NULL "
                                            "ORDER BY claimed_at", ()),
            "worker_compute_reservations": ("SELECT COALESCE(SUM(seconds),0) FROM worker_compute_reservations WHERE day=?", ("2026-10-22",)),
        }
        for name, (sql, parameters) in queries.items():
            plan = " ".join(row[3] for row in self.database.execute("EXPLAIN QUERY PLAN " + sql, parameters))
            # SEARCH by an index, or an index walk in order (LIMIT 1, or stopped early by the reader)
            self.assertRegex(plan, r"^(SEARCH|SCAN) \S+ USING (COVERING )?INDEX ", name)
            self.assertNotIn("USE TEMP B-TREE", plan, name)

    def test_o11_first_invite_request_stops_early_and_matches_pacing(self):
        import pacing
        first = pacific(2026, 10, 1, 10, 0)
        invite_action(self.database, "late-claim-early-start", "CONFIRMED_SENT", first - dt.timedelta(minutes=5), first)
        invite_action(self.database, "early-claim-late-start", "CONFIRMED_SENT", first - dt.timedelta(minutes=10),
                      first + dt.timedelta(hours=2))
        invite_action(self.database, "later", "CONFIRMED_SENT", self.inside, self.inside)
        self.database.commit()
        reader = overview_page.Reader(self.database)
        self.assertEqual(overview_page.first_invite_request(reader), first)
        self.assertEqual(overview_page.first_invite_request(reader), pacing.request_times(self.database)[0])
        self.assertEqual(self.gather()["outreach"]["cap"], 25)            # week 3: past the 20/day first week

    def test_o12_malformed_rows_are_skipped(self):
        db = self.database
        event(db, "inbound", None, self.inside, "good")
        listed(db, "good")
        db.execute("INSERT INTO engagement_events VALUES ('unipile','bad','x','inbound',?,?,'{}','null','{not json')",
                   (self.inside.isoformat(), self.inside.isoformat()))
        invite_action(db, "bad-payload", "REQUEST_STARTED", self.inside, self.inside, payload="{not json")
        invite_action(db, "bad-time", "CONFIRMED_SENT", self.before, self.before,
                      result=json.dumps({"attempts": [{"started_at": "not a time"}]}))
        invite_action(db, "ok", "REQUEST_STARTED", self.inside, self.inside)
        db.commit()
        outreach = self.gather()["outreach"]
        self.assertEqual((outreach["replies"], outreach["waiting"], outreach["cap"]), (1, 1, 20))

    def test_o4_an_older_schema_degrades_one_section(self):
        older = sqlite3.connect(self.root / "older.sqlite3")
        older.row_factory = sqlite3.Row
        older.execute("CREATE TABLE runs (slot TEXT PRIMARY KEY, started_at TEXT NOT NULL, state TEXT NOT NULL)")
        older.execute("INSERT INTO runs VALUES ('s',?,'COMPLETE')", (self.inside.isoformat(),))
        older.execute("CREATE TABLE application_events (id INTEGER PRIMARY KEY, job_key TEXT, at TEXT)")
        view = self.gather(database=older)
        self.assertEqual(view["health"][-1]["text"], "Not running. Last cycle: not available")
        self.assertEqual(view["submitted"], overview_page.UNAVAILABLE)
        self.assertEqual(view["outreach"]["invitations"], 0)
        self.assertEqual(view["right_now"], {"confirming": 0, "blocked": 0, "retrying": 0})
        self.assertFalse(view["unreadable"])
        older.close()

    def test_o4_locked_is_not_degraded(self):
        locked = mock.Mock()
        locked.execute.side_effect = sqlite3.OperationalError("database is locked")
        with self.assertRaises(sqlite3.OperationalError):
            self.gather(database=locked)

    def test_empty_and_missing_database(self):
        empty = sqlite3.connect(":memory:")
        self.addCleanup(empty.close)
        for database in (None, empty):
            view = overview_page.gather(database, CONFIG, self.home, setup_steps.progress(self.home), False, now=self.now)
            self.assertEqual(view["outreach"], {"invitations": 0, "cap": 20, "waiting": 0, "messages": 0, "replies": 0})
            self.assertEqual(view["right_now"], {"confirming": 0, "blocked": 0, "retrying": 0})
            self.assertEqual(view["needs"], {"questions": 0, "applications": 0, "messages": 0, "couldnt_send": 0, "notes": 0})
            self.assertEqual(view["compute"]["used"], 0)
            self.assertIsNone(view["research"]["last"])
            self.assertEqual(view["research"]["next"], {"label": "As soon as Sidedoor runs"})

    def test_health_from_stored_checks(self):
        finished_home(self.home, ("failed", "OpenRouter did not accept this key."))
        with mock.patch.object(setup_steps, "chrome_path", return_value="/chrome"):
            health = {item["name"]: item for item in self.gather()["health"]}
        openrouter = health["OpenRouter"]
        self.assertEqual((openrouter["tone"], openrouter["text"]), ("error", "OpenRouter did not accept this key."))
        self.assertEqual(openrouter["fix"], {"label": "Replace key", "href": "/settings#connections"})
        self.assertEqual(openrouter["checked"], dt.datetime(2026, 10, 22, 17, 2, tzinfo=dt.timezone.utc))
        self.assertEqual(health["Scheduler"]["fix"], {"label": "Start", "post": "/start"})
        for item in health.values():   # no dead ends: every red or amber item has a way forward
            if item["tone"] != "success":
                self.assertTrue(item["fix"], item["name"])

    def record_wait(self, name, resume_at=None):
        import provider_limits
        path = self.home / name
        path.write_text(json.dumps({"observed_at": "2026-10-22T15:00:00Z", "resume_at": resume_at,
                                    "stated_reset": None}), encoding="utf-8")
        return provider_limits

    def health_and_banners(self, provider_limits):
        """Health and the banners from one recorded state, read the way the dashboard reads them."""
        with mock.patch.object(setup_steps, "chrome_path", return_value="/chrome"), \
                mock.patch.object(setup_steps, "codex_done", return_value=True):   # Codex signed in
            health = {item["name"]: item for item in self.gather()["health"]}
        return health, provider_limits.banners(self.home, self.now, finished=True)

    def test_health_shows_the_provider_waits_the_banners_show(self):
        import provider_limits
        health, banners = self.health_and_banners(provider_limits)
        self.assertEqual(banners, [])
        self.assertEqual((health["OpenRouter"]["tone"], health["Codex"]["tone"]), ("success", "success"))

        self.record_wait(provider_limits.CREDIT_WAIT_FILE)
        health, banners = self.health_and_banners(provider_limits)
        self.assertEqual((health["OpenRouter"]["tone"], health["OpenRouter"]["text"]), ("error", "Out of credits"))
        self.assertEqual(health["OpenRouter"]["fix"], {"label": "Add credits", "href": provider_limits.CREDITS_URL})
        self.assertEqual([banner["action_href"] for banner in banners], [provider_limits.CREDITS_URL])
        (self.home / provider_limits.CREDIT_WAIT_FILE).unlink()

        reset = (self.now + dt.timedelta(hours=2)).isoformat()
        self.record_wait(provider_limits.CHATGPT_WAIT_FILE, reset)
        health, banners = self.health_and_banners(provider_limits)
        until = provider_limits._local_clock(reset)
        self.assertEqual((health["Codex"]["tone"], health["Codex"]["text"]), ("warning", "Usage limit reached until " + until))
        self.assertEqual(health["Codex"]["fix"]["href"], provider_limits.CHATGPT_USAGE_URL)
        self.assertEqual([banner["tone"] for banner in banners], ["warning"])
        self.assertIn(until, banners[0]["text"])

        self.record_wait(provider_limits.CODEX_WAIT_FILE)   # can't find Codex outranks the usage wait, as in the banners
        health, banners = self.health_and_banners(provider_limits)
        self.assertEqual((health["Codex"]["tone"], health["Codex"]["text"]), ("error", "Can't find Codex"))
        self.assertEqual(health["Codex"]["fix"], {"label": banners[0]["action_label"], "href": banners[0]["action_href"]})
        self.assertEqual(banners[0]["action_href"], "/settings#connection-codex")

        (self.home / provider_limits.CODEX_WAIT_FILE).unlink()
        self.record_wait(provider_limits.CHATGPT_WAIT_FILE, (self.now - dt.timedelta(minutes=1)).isoformat())
        health, banners = self.health_and_banners(provider_limits)   # the reset passed: both clear together
        self.assertEqual((health["Codex"]["tone"], banners), ("success", []))

    def test_o15_never_checked_says_so_once_and_codex_never_none(self):
        finished_home(self.home, openrouter=None, linkedin=False)
        setup_steps.update_state(lambda state: state["steps"].update(codex={"status": "failed", "at": "2026-10-22T17:00:00Z"}),
                                 self.home)
        health = {item["name"]: item for item in self.gather()["health"]}
        for name in ("LinkedIn", "OpenRouter"):
            self.assertEqual(health[name]["text"], "Not checked yet")
            self.assertFalse(health[name]["timed"], name)
        self.assertEqual(health["Codex"]["text"], "Codex stopped working")

    def test_o1_start_message_is_plain(self):
        self.assertEqual(overview_page.start_message({"status": setup_steps.NOT_BUILT, "message": "needs E5.T1"}, False),
                         overview_page.NOT_STARTABLE_YET)
        self.assertEqual(overview_page.start_message({"status": "done"}, True), "Sidedoor is running.")
        self.assertNotRegex(overview_page.NOT_STARTABLE_YET, r"E\d|\(|scheduler")

    def test_o17_snapshot_creates_no_side_files_and_sees_committed_rows(self):
        path = self.root / "state" / "orchestrator.sqlite3"
        event(self.database, "inbound", None, self.inside, "someone")
        self.database.commit()
        self.database.close()
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["orchestrator.sqlite3"])
        opened = []

        def plain():
            opened.append(1)
            database = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
            database.row_factory = sqlite3.Row
            return database
        count = overview_page.snapshot(path, lambda db: db.execute("SELECT COUNT(*) FROM engagement_events").fetchone()[0], plain)
        self.assertEqual((count, opened), (1, []))
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["orchestrator.sqlite3"])
        # A writer connected (its -wal exists): the ordinary read-only connection, which sees its commits.
        writer = sqlite3.connect(path)
        writer.execute("INSERT INTO engagement_events SELECT 'unipile','two',identity,kind,occurred_at,observed_at,raw,inference,payload "
                       "FROM engagement_events")
        writer.commit()
        count = overview_page.snapshot(path, lambda db: db.execute("SELECT COUNT(*) FROM engagement_events").fetchone()[0], plain)
        self.assertEqual((count, opened), (2, [1]))
        writer.close()
        self.database = sqlite3.connect(":memory:")

    def test_o17_a_write_during_the_immutable_read_is_read_again(self):
        path = self.root / "state" / "orchestrator.sqlite3"
        self.database.close()
        calls = []

        def use(database):
            calls.append(database)
            if len(calls) == 1:   # a writer connects and commits mid-read
                writer = sqlite3.connect(path)
                writer.execute("INSERT INTO runs VALUES ('s','2026-10-21T17:00:00+00:00','COMPLETE',NULL,NULL)")
                writer.commit()
                self.addCleanup(writer.close)
            return database.execute("SELECT COUNT(*) FROM runs").fetchone()[0]

        def plain():
            return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        self.assertEqual(overview_page.snapshot(path, use, plain), 1)
        self.assertEqual(len(calls), 2)
        self.database = sqlite3.connect(":memory:")


@unittest.skipUnless(DEPENDENCIES, "dashboard packages (fastapi, httpx) are not installed")
class OverviewPageTest(unittest.TestCase):
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

    def page(self):
        with mock.patch.object(setup_steps, "chrome_path", return_value="/chrome"):
            return self.client().get("/")

    def test_fresh_install_without_a_database(self):
        """Worked example 1: nothing has run yet; zeros, "Not run yet", no 500."""
        response = self.page()
        self.assertEqual(response.status_code, 200)
        page = response.text
        for text in ("Sidedoor is not running", "Not run yet", "As soon as Sidedoor runs", "Key works", 'action="/start"',
                     "0 min</strong> <span class=\"muted\">of 5 h 50 min used today", "Right now"):
            self.assertIn(text, page)
        self.assertNotIn('href="/setup"', page)
        self.assertFalse((self.root / "state").exists())   # a GET never creates the database

    def test_health_and_banner_agree_on_the_page(self):
        import provider_limits
        (self.home / provider_limits.CREDIT_WAIT_FILE).write_text(json.dumps({"observed_at": "2026-10-22T15:00:00Z"}),
                                                                  encoding="utf-8")
        page = self.page().text
        self.assertIn("Your OpenRouter account is out of credits.", page)
        health = page[page.index('id="health-title"'):]
        self.assertIn("Out of credits", health)
        self.assertNotIn("Key works", health)
        self.assertIn('href="%s"' % provider_limits.CREDITS_URL, health)

    def test_red_and_amber_health_items_show_under_the_title_unless_a_banner_says_it(self):
        """Owner, 2026-10-24: a red or amber Health item also shows just under the title with the same
        text and link (one source); one a banner or the "not running" card already states is not repeated."""
        import provider_limits
        setup_steps.update_state(lambda state: state["steps"].update(
            linkedin={"status": "checking", "message": "Waiting", "at": "2026-10-22T17:02:00Z"}), self.home)
        (self.home / provider_limits.CREDIT_WAIT_FILE).write_text(json.dumps({"observed_at": "2026-10-22T15:00:00Z"}),
                                                                  encoding="utf-8")
        page = self.page().text
        top = page[page.index('class="overview-list overview-alerts"'):page.index('id="today-title"')]
        health = page[page.index('id="health-title"'):]
        for text in ("Waiting for LinkedIn", '<a class="button health-fix" href="/settings#connections">Check LinkedIn</a>'):
            self.assertIn(text, top)
            self.assertIn(text, health)
        self.assertIn("Out of credits", health)
        self.assertNotIn("Out of credits", top)              # its banner says it
        self.assertNotIn("Scheduler", top)                   # the "Sidedoor is not running" card says it
        self.assertLess(page.index('class="overview-version"'), page.index('class="overview-list overview-alerts"'))

    def test_the_version_is_a_tag_beside_the_title(self):
        import version
        with mock.patch.object(version, "current", return_value="0.0.10"):
            page = self.page().text
        head = page[page.index('class="overview-head"'):]
        self.assertRegex(head, r'^class="overview-head">\s*<h1 class="title gradient-text">Overview</h1>\s*'
                               r'<span class="overview-version" title="Sidedoor 0.0.10">v0.0.10</span>')

    def test_needs_you_counts_are_the_approvals_and_applications_totals(self):
        import approvals_page
        import applications_page
        database = database_with_schema(self.root)
        job(database, "eligibility", "BLOCKED", "REQUIRED_ELIGIBILITY_ANSWER")
        job(database, "answers", "BLOCKED", "UNKNOWN_REQUIRED_ANSWERS:salary")
        job(database, "wall", "BLOCKED", "CAPTCHA_REQUIRED")
        now = dt.datetime.now(dt.timezone.utc)
        question(database, "q1", "eligibility", now)
        database.commit()
        reader = overview_page.Reader(database)
        needs = overview_page.needs_you(reader, now)
        start = workflow_tasks.service_day_start(now, "America/Los_Angeles", 9)
        counts = applications_page.counts(reader, now, start, start + dt.timedelta(days=1))
        self.assertEqual((needs["questions"], needs["applications"]),
                         (approvals_page.question_count(database, now), counts["needs_you"]))
        self.assertEqual((needs["questions"], needs["applications"]), (1, 0))
        database.close()

    def test_get_writes_nothing_not_even_side_files(self):
        database = database_with_schema(self.root)
        event(database, "inbound", None, dt.datetime.now(dt.timezone.utc), "someone")
        listed(database, "someone")
        database.commit()
        database.close()
        path = self.root / "state" / "orchestrator.sqlite3"
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        response = self.page()
        self.assertEqual(response.status_code, 200)
        self.assertIn('href="/sent?show=replied_today#conversations">1</a>', response.text)   # O16, E6.T5: Replied today
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["orchestrator.sqlite3"])

    def test_o4_a_junk_file_shows_the_page(self):
        (self.root / "state").mkdir()
        (self.root / "state" / "orchestrator.sqlite3").write_bytes(os.urandom(4096))
        response = self.page()
        self.assertEqual(response.status_code, 200)
        page = response.text
        self.assertIn("Sidedoor couldn't read its records.", page)
        self.assertGreaterEqual(page.count("Not available"), 6)
        self.assertIn("Key works", page)      # Health comes from the stored setup checks

    def test_e8t3_expired_shows_the_paywall_with_pause_and_the_way_to_paste_a_new_key(self):
        import license_fixtures
        with license_fixtures.use_test_key():
            license_fixtures.install(self.home, valid=False)   # the heartbeat's signed valid: false
            page = self.page().text
            # PW1: [Pause] stays on the paywall; once the user paused, "Paused by you since", no [Resume].
            (self.root / "state").mkdir(exist_ok=True)
            import scheduler_control
            database = sqlite3.connect(self.root / "state" / "orchestrator.sqlite3")
            scheduler_control.set_user_pause(database, True, now="2026-10-22T15:00:00+00:00")
            database.close()
            paused = self.page().text
        self.assertIn('id="paywall-renew" href="https://yoursidedoor.com"', page)
        self.assertIn('hx-get="/setup/guide/license" hx-trigger="click from:#paywall-renew"', page)   # guide 4.5
        self.assertIn("Sidedoor is paused. Renew to continue.", page)                              # banner 1 too
        self.assertEqual(page.count("Bought a new key? Paste it here"), 1)   # PW3: always, once (banner 1)
        self.assertNotIn("Compute budget", page)                                                    # full page
        self.assertIn('action="/pause"', page)
        self.assertNotIn('action="/resume"', page)
        self.assertIn("Paused by you since", paused)
        self.assertNotIn('action="/pause"', paused)
        self.assertNotIn('action="/resume"', paused)

    def test_e8t3_grace_spent_shows_banner_1b_and_no_paywall(self):
        import license_fixtures
        old = dt.datetime.now(dt.timezone.utc).replace(microsecond=0) - dt.timedelta(hours=80)
        with license_fixtures.use_test_key():
            license_fixtures.install(self.home, server_time=old)
            page = self.page().text
        self.assertIn("could not reach the license server for 3 days", page)
        self.assertNotIn("paywall-renew", page)
        self.assertIn("Compute budget", page)

    def test_red_openrouter_shows_its_fix(self):
        """Worked example 3: a refused key is red with [Replace key]."""
        finished_home(self.home, ("failed", "OpenRouter did not accept this key."))
        page = self.page().text
        self.assertIn("OpenRouter did not accept this key.", page)
        self.assertIn('href="/settings#connections">Replace key</a>', page)

    def test_o1_start_result_shows_next_to_the_clicked_button_in_plain_words(self):
        client = self.client()
        origin = {"origin": f"http://127.0.0.1:{PORT}"}

        def not_built(home=None):   # what start_scheduler records while it cannot start anything
            return setup_steps.record(home, "start", setup_steps.NOT_BUILT, "Starting Sidedoor is not built yet: it needs "
                                      "the scheduler (E5.T1).")
        for at in ("card", "health"):
            with mock.patch.object(setup_steps, "chrome_path", return_value="/chrome"), \
                    mock.patch.object(setup_steps, "start_scheduler", not_built):
                page = client.post("/start", data={"at": at}, headers=origin).text
            self.assertEqual(page.count(overview_page.NOT_STARTABLE_YET.replace("'", "&#39;")), 1)
            self.assertNotIn("E5.T1", page)
            result = page.index('class="note note-info start-result"')
            card, health = page.index("Sidedoor is not running"), page.index('id="health-title"')
            self.assertTrue(card < result < health if at == "card" else result > health, at)

    def test_o1_a_started_scheduler_says_so_where_it_was_clicked(self):
        def started(home=None):
            return setup_steps.record(home, "start", "done", "Sidedoor is running.")
        with mock.patch.object(setup_steps, "start_scheduler", started), \
                mock.patch.object(setup_steps, "scheduler_running", return_value=True):
            page = self.client().post("/start", data={"at": "card"}, headers={"origin": f"http://127.0.0.1:{PORT}"}).text
        self.assertNotIn("Sidedoor is not running", page)
        self.assertIn("Sidedoor is running.", page)

    def test_pause_and_resume_answer_beside_the_scheduler(self):
        database_with_schema(self.root).close()
        client = self.client()
        origin = {"origin": f"http://127.0.0.1:{PORT}"}
        with mock.patch.object(setup_steps, "chrome_path", return_value="/chrome"), \
                mock.patch.object(setup_steps, "scheduler_running", return_value=True):
            page = client.post("/pause", headers=origin).text
            self.assertGreater(page.index("Paused. Work already running finishes"), page.index('id="health-title"'))
            self.assertIn('action="/resume"', page)
            page = client.post("/resume", headers=origin).text
            self.assertIn('action="/pause"', page)

    def test_unfinished_setup_keeps_the_setup_card(self):
        setup_steps.update_state(lambda state: state.pop("setup_finished_at"), self.home)
        page = self.page().text
        self.assertIn("Finish setup first", page)
        self.assertNotIn("Compute budget", page)


class PhoneWidthTest(unittest.TestCase):
    def test_shared_header_has_a_phone_width_rule(self):
        css = CSS.read_text()
        rule = css[css.index("@media (max-width: 640px)"):]
        for needed in (".header .tabs", "overflow-x: auto", ".banner { flex-wrap: wrap; }", ".page { padding: 24px 16px; }"):
            self.assertIn(needed, rule)


class JobRunsTest(unittest.TestCase):
    """Outreach and Applications "Last run" / "Next run" (overview_page.job_runs)."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.home = self.root / "home"
        finished_home(self.home)
        self.reports = self.root / "reports"
        self.reports.mkdir()
        self.database = database_with_schema(self.root)
        self.now = pacific(2026, 10, 22, 10, 40)
        self.config = dict(CONFIG, research_enabled=True, applications_enabled=True)
        # Today's research already ran, so it holds no turn unless a test says so.
        self.database.execute("INSERT INTO research_days VALUES ('2026-10-22','COMPLETE',1,'r1',NULL,?,0)",
                              (pacific(2026, 10, 22, 9, 20).isoformat(),))
        self.database.commit()

    def tearDown(self):
        self.database.close()
        self.directory.cleanup()

    def runs(self, scheduler=None, database=False, config=None):
        view = overview_page.gather(self.database if database is False else database, config or self.config, self.home,
                                    setup_steps.progress(self.home), scheduler or {"running": True},
                                    now=self.now, root=self.root)
        return view["runs"], view["when"]

    def hourly(self, slot, state, started, completed):
        self.database.execute("INSERT INTO runs(slot,started_at,state,completed_at) VALUES (?,?,?,?)",
                              (slot, started.isoformat(), state, completed.isoformat() if completed else None))
        self.database.commit()

    def applications_run(self, status, started, ended, result):
        task = "t-" + started.isoformat()
        self.database.execute("""INSERT INTO workflow_tasks(task_id,worker,action_id,parent_run_id,requirement_revision,
                                 inputs,deadline,status,max_attempts,result,owner,effect,updated_at)
                                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                              (task, "applications", "applications-job:x:applications", "applications-job:x", "r", "[]",
                               "2099-01-01T00:00:00+00:00", status, 1, json.dumps(result), "o", "WRITE", ended.isoformat()))
        self.database.execute("INSERT INTO workflow_task_events(task_id,at,event,detail) VALUES (?,?,?,?)",
                              (task, started.isoformat(), "ENQUEUED", "{}"))
        self.database.commit()

    def verified(self, at):
        self.database.execute("INSERT INTO application_events(job_key,at,event,detail) VALUES ('j',?,'SUBMISSION_VERIFIED','{}')",
                              (at.isoformat(),))
        self.database.commit()

    def test_finished_and_next_from_the_scheduler(self):
        self.hourly("2026-10-22T17", "COMPLETE", pacific(2026, 10, 22, 10, 0), pacific(2026, 10, 22, 10, 12))
        self.applications_run("COMPLETE", pacific(2026, 10, 22, 9, 30), pacific(2026, 10, 22, 9, 50), {"verified_submissions": 4})
        runs, when = self.runs()
        self.assertEqual(runs["outreach"]["last"], {"at": pacific(2026, 10, 22, 10, 12), "words": "finished"})
        self.assertEqual(runs["applications"]["last"]["words"], "finished")   # the total on file is not this run's
        # scheduler_loop.schedule: the next :00 and the next :30.
        self.assertEqual(when(runs["outreach"]["next"]["at"]), "11:00")
        self.assertEqual(when(runs["applications"]["next"]["at"]), "11:30")

    def test_submitted_counts_verifications_inside_the_run(self):
        self.applications_run("COMPLETE", pacific(2026, 10, 22, 9, 30), pacific(2026, 10, 22, 9, 50), {})
        self.verified(pacific(2026, 10, 22, 9, 35))
        self.verified(pacific(2026, 10, 22, 9, 45))
        self.verified(pacific(2026, 10, 22, 8, 0))     # before the run
        self.assertEqual(self.runs()[0]["applications"]["last"]["words"], "2 submitted")

    def test_did_not_finish(self):
        self.hourly("2026-10-22T17", "FAILED", pacific(2026, 10, 22, 10, 0), pacific(2026, 10, 22, 10, 2))
        self.applications_run("FAILED", pacific(2026, 10, 22, 9, 30), pacific(2026, 10, 22, 9, 31), {"state": "FAILED"})
        runs = self.runs()[0]
        self.assertEqual(runs["outreach"]["last"]["words"], "did not finish")
        self.assertEqual(runs["applications"]["last"]["words"], "did not finish")

    def test_a_crashed_child_without_a_record_did_not_finish(self):
        self.hourly("2026-10-22T16", "COMPLETE", pacific(2026, 10, 22, 9, 0), pacific(2026, 10, 22, 9, 10))
        crash = pacific(2026, 10, 22, 10, 0, 5)
        scheduler = {"running": True, "job_failures": {"run": {"exit_code": 1, "at": crash.isoformat()}},
                     "last_fired": {"run": pacific(2026, 10, 22, 10, 0).timestamp()}}
        self.assertEqual(self.runs(scheduler)[0]["outreach"]["last"], {"at": crash, "words": "did not finish"})
        # A child that wrote its own record (a DEGRADED round exits 1 too) is described by that record.
        self.hourly("2026-10-22T17", "DEGRADED", pacific(2026, 10, 22, 10, 0), pacific(2026, 10, 22, 10, 3))
        self.assertEqual(self.runs(scheduler)[0]["outreach"]["last"]["words"], "finished")

    def test_ov_a_duplicate_slot_is_never_a_last_run(self):
        # After a restart the scheduler fires the hourly job again (last_fired = this :00); the
        # slot is recorded, so that start writes nothing and Last run stays the real round.
        self.hourly("2026-10-22T17", "COMPLETE", pacific(2026, 10, 22, 10, 0, 3), pacific(2026, 10, 22, 10, 12))
        refired = {"running": True, "job_failures": {}, "last_fired": {"run": pacific(2026, 10, 22, 10, 0).timestamp()}}
        self.assertEqual(self.runs(refired)[0]["outreach"]["last"], {"at": pacific(2026, 10, 22, 10, 12), "words": "finished"})

    def test_ov_a_round_past_the_hour_is_judged_by_its_own_start(self):
        # The 9:00 round ran past 10:00 (the scheduler passed over 10:00, last_fired = 10:00) and
        # exited 1 as DEGRADED: its own runs row is shown, not "did not finish" at the exit time.
        self.hourly("2026-10-22T16", "DEGRADED", pacific(2026, 10, 22, 9, 0, 2), pacific(2026, 10, 22, 10, 20))
        scheduler = {"running": True, "last_fired": {"run": pacific(2026, 10, 22, 10, 0).timestamp()},
                     "job_failures": {"run": {"exit_code": 1, "at": pacific(2026, 10, 22, 10, 20, 1).isoformat(),
                                              "started": pacific(2026, 10, 22, 9, 0).isoformat()}}}
        self.assertEqual(self.runs(scheduler)[0]["outreach"]["last"], {"at": pacific(2026, 10, 22, 10, 20), "words": "finished"})
        # The same for an applications run that ran past :30.
        self.applications_run("COMPLETE", pacific(2026, 10, 22, 9, 30, 2), pacific(2026, 10, 22, 10, 35), {})
        scheduler["last_fired"]["applications"] = pacific(2026, 10, 22, 10, 30).timestamp()
        scheduler["job_failures"]["applications"] = {"exit_code": 1, "at": pacific(2026, 10, 22, 10, 35, 1).isoformat(),
                                                     "started": pacific(2026, 10, 22, 9, 30).isoformat()}
        self.assertEqual(self.runs(scheduler)[0]["applications"]["last"]["words"], "finished")

    def test_ov_a_skipped_applications_run_is_never_a_last_run(self):
        # "Another applications run is active; skipped." writes no step and no report: after a
        # restart refires :30, the run still going (or the latest real one) is what is shown.
        self.applications_run("RUNNING", pacific(2026, 10, 22, 10, 30, 4), pacific(2026, 10, 22, 10, 31), {})
        refired = {"running": True, "job_failures": {}, "last_fired": {"applications": pacific(2026, 10, 22, 10, 30).timestamp()}}
        self.assertEqual(self.runs(refired)[0]["applications"]["last"]["words"], "running now")

    def test_ov_the_hourly_run_records_nothing_for_a_recorded_slot(self):
        root = self.root / "dup"
        (root / "state").mkdir(parents=True)
        database = orchestrator.connect(root)
        try:
            slot = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H")
            database.execute("INSERT INTO runs(slot,started_at,state,completed_at) VALUES (?,?,?,?)",
                             (slot, "2026-10-22T17:00:03+00:00", "COMPLETE", "2026-10-22T17:12:00+00:00"))
            database.commit()
            # Research pending too: a duplicate start must not write a yield record either.
            with mock.patch.object(orchestrator, "research_turn_pending", return_value=True), \
                    mock.patch.object(orchestrator, "settle_idle_browser_reservations",
                                      side_effect=AssertionError("a duplicate does no work")):
                self.assertEqual(orchestrator.run(root, self.config, database, {}), 0)
            self.assertEqual([tuple(row) for row in database.execute("SELECT slot,state,completed_at FROM runs")],
                             [(slot, "COMPLETE", "2026-10-22T17:12:00+00:00")])
        finally:
            database.close()
        self.assertFalse((root / "reports" / "hourly-yield.json").exists())

    def test_yielded_to_research(self):
        self.hourly("2026-10-22T15", "COMPLETE", pacific(2026, 10, 22, 8, 0), pacific(2026, 10, 22, 8, 10))
        (self.reports / "hourly-yield.json").write_text(json.dumps(
            {"state": "YIELDED_TO_DAILY_RESEARCH", "slot": "2026-10-22T16", "at": pacific(2026, 10, 22, 9, 0).isoformat()}))
        (self.reports / "applications-service.json").write_text(json.dumps(
            {"state": "YIELDED_TO_DAILY_RESEARCH", "started_at": pacific(2026, 10, 22, 9, 30).isoformat(),
             "completed_at": pacific(2026, 10, 22, 9, 30, 1).isoformat()}))
        runs = self.runs()[0]
        self.assertEqual(runs["outreach"]["last"], {"at": pacific(2026, 10, 22, 9, 0), "words": "waited for research"})
        self.assertEqual(runs["applications"]["last"]["words"], "waited for research")
        # A run stopped between jobs because research took its turn.
        self.applications_run("COMPLETE", pacific(2026, 10, 22, 10, 30), pacific(2026, 10, 22, 10, 35),
                              {"stopped": "YIELDED_TO_DAILY_RESEARCH"})
        self.assertEqual(self.runs()[0]["applications"]["last"]["words"], "waited for research")

    def test_the_hourly_run_writes_its_yield_record(self):
        root = self.root / "other"
        (root / "state").mkdir(parents=True)
        database = orchestrator.connect(root)
        try:
            with mock.patch.object(orchestrator, "research_turn_pending", return_value=True):
                self.assertEqual(orchestrator.run(root, self.config, database, {}), 0)
            self.assertEqual(database.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)   # the slot stays free
        finally:
            database.close()
        saved = json.loads((root / "reports" / "hourly-yield.json").read_text())
        self.assertEqual(saved["state"], "YIELDED_TO_DAILY_RESEARCH")

    def test_after_todays_research_while_research_holds_its_turn(self):
        self.database.execute("DELETE FROM research_days")
        self.database.commit()
        runs = self.runs()[0]
        self.assertEqual(runs["outreach"]["next"], {"label": "after today's research"})
        self.assertEqual(runs["applications"]["next"], {"label": "after today's research"})
        self.database.execute("INSERT INTO research_days VALUES ('2026-10-22','RUNNING',1,'r1',NULL,NULL,0)")
        self.database.commit()
        self.assertEqual(self.runs()[0]["applications"]["next"], {"label": "after today's research"})

    def test_paused(self):
        import scheduler_control
        scheduler_control.set_user_pause(self.database, True, now="2026-10-22T15:00:00+00:00")
        runs = self.runs()[0]
        self.assertEqual(runs["outreach"]["next"], {"label": "Paused"})
        self.assertEqual(runs["applications"]["next"], {"label": "Paused"})

    def test_turned_off(self):
        runs = self.runs(config=dict(self.config, applications_enabled=False))[0]
        self.assertEqual(runs["applications"]["next"], {"label": "Turned off"})
        self.assertIn("at", runs["outreach"]["next"])

    def test_not_running_and_data_hold(self):
        runs = self.runs({"running": False})[0]
        self.assertEqual(runs["outreach"]["next"], {"label": "Not running"})
        self.assertEqual(runs["applications"]["next"], {"label": "Not running"})
        runs = self.runs({"running": True, "data_hold": "Waiting to move your data."})[0]
        self.assertEqual(runs["outreach"]["next"], {"label": "Waiting to move your data"})

    def test_fresh_install(self):
        runs = self.runs(database=None)[0]
        self.assertIsNone(runs["outreach"]["last"])
        self.assertIsNone(runs["applications"]["last"])
        self.assertEqual(runs["outreach"]["next"], {"label": "after today's research"})   # research goes first
        runs = self.runs(database=None, config=dict(self.config, research_enabled=False))[0]
        self.assertIn("at", runs["applications"]["next"])
        # A database without the tables yet (an older or partial schema): nothing raised, nothing created.
        empty = sqlite3.connect(":memory:")
        try:
            runs = self.runs(database=empty)[0]
            self.assertIsNone(runs["outreach"]["last"])
            self.assertEqual(runs["applications"]["next"], {"label": "after today's research"})
            self.assertEqual(empty.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0], 0)
        finally:
            empty.close()

    def test_loop_state_carries_last_fired(self):
        import scheduler_loop
        loop = mock.Mock(last_error=None, job_failures={}, data_hold=None, last_fired={"run": 1.0})
        with mock.patch.object(scheduler_loop, "current", return_value=loop):
            self.assertEqual(overview_page.loop_state(True)["last_fired"], {"run": 1.0})

    @unittest.skipUnless(DEPENDENCIES, "dashboard packages (fastapi, httpx) are not installed")
    def test_the_cards_show_the_rows(self):
        self.hourly("2026-10-22T17", "COMPLETE", pacific(2026, 10, 22, 10, 0), pacific(2026, 10, 22, 10, 12))
        self.database.close()
        app = dashboard.create_app(SECRET, PORT, home=self.home, root=self.root, config=self.config)
        client = TestClient(app, base_url=f"http://127.0.0.1:{PORT}", follow_redirects=False)
        client.get("/", params={"t": SECRET})
        with mock.patch.object(setup_steps, "chrome_path", return_value="/chrome"):
            page = client.get("/").text
        self.database = sqlite3.connect(":memory:")
        outreach_card = page.split("<h3>Outreach</h3>")[1].split("<h3>Applications</h3>")[0]
        applications_card = page.split("<h3>Applications</h3>")[1].split("</section>")[0]
        self.assertIn('<span class="muted">(finished)</span>', outreach_card)
        self.assertIn("next: Not running", outreach_card)      # this test process has no scheduler
        self.assertIn("Not run yet", applications_card)
        self.assertIn("next: Not running", applications_card)

    @unittest.skipUnless(DEPENDENCIES, "dashboard packages (fastapi, httpx) are not installed")
    def test_no_row_is_empty_with_an_applications_record_and_no_loop(self):
        # create_app without serve (a preview): Next run reads "Not running" on both cards, and Last
        # run shows the applications run with its result; no Last/Next row renders empty.
        # The day the page's clock is pinned to below, so the rows render as bare times whatever day
        # the suite runs on (the runs used today's date and read "tomorrow" against the pin).
        today = dt.date(2026, 10, 22)
        y, m, d = today.year, today.month, today.day
        self.hourly(f"{today.isoformat()}T17", "COMPLETE", pacific(y, m, d, 10, 0), pacific(y, m, d, 10, 12))
        self.applications_run("COMPLETE", pacific(y, m, d, 9, 30), pacific(y, m, d, 9, 50), {})
        self.database.close()
        app = dashboard.create_app(SECRET, PORT, home=self.home, root=self.root, config=self.config)
        client = TestClient(app, base_url=f"http://127.0.0.1:{PORT}", follow_redirects=False)
        client.get("/", params={"t": SECRET})
        gather = overview_page.gather   # the page's clock pinned to that day (E2.T2: no date bomb)
        with mock.patch.object(setup_steps, "chrome_path", return_value="/chrome"), \
                mock.patch.object(overview_page, "gather",
                                  lambda *a, **k: gather(*a, **{**k, "now": pacific(2026, 10, 22, 11, 0)})):
            page = client.get("/").text
        self.database = sqlite3.connect(":memory:")
        outreach_card = page.split("<h3>Outreach</h3>")[1].split("<h3>Applications</h3>")[0]
        applications_card = page.split("<h3>Applications</h3>")[1].split("</section>")[0]
        # One line each (owner, 2026-10-24): "Last run 10:12 (finished) · next: Not running".
        line = lambda card: " ".join(re.search(r'<p class="overview-run">(.*?)</p>', card, re.S).group(1).split())
        self.assertEqual(line(outreach_card), 'Last run 10:12 <span class="muted">(finished)</span> '
                                              '<span class="muted" aria-hidden="true">·</span> next: Not running')
        self.assertEqual(line(applications_card), 'Last run 09:50 <span class="muted">(finished)</span> '
                                                  '<span class="muted" aria-hidden="true">·</span> next: Not running')


if __name__ == "__main__":
    unittest.main()
