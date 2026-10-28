"""The Failures and logs page (E6.T7; E6.T0 spec section 9): read-only, real failures only, logs redacted.

`failures_page.gather` and the log readers are plain Python and run on the stdlib suite; the page
tests need the dashboard packages (fastapi, httpx) and are skipped without them.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
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

import failures_page
import notifications
import overview_page
import setup_steps
import test_dashboard_overview as base

DEPENDENCIES = all(importlib.util.find_spec(name) for name in ("fastapi", "httpx"))
if DEPENDENCIES:
    from fastapi.testclient import TestClient

    import dashboard

CONFIG = base.CONFIG
KEY = "sk-or-v1-0123456789abcdef0123456789abcdef"
LICENSE = "SD-LICENSE-9f8e7d6c5b4a"
NOT_RUNNING = {"running": False, "last_error": None, "job_failures": {}}


def text_of(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def task(database, task_id, worker, status, updated, blocker=None, next_at=None, events=(), effect="READ", parent="run"):
    """A task and its events; an event is a kind (recorded with a result, as `finish` records it) or
    (kind, detail, at)."""
    database.execute("""INSERT INTO workflow_tasks(task_id,worker,action_id,parent_run_id,requirement_revision,inputs,
                        deadline,status,attempts,max_attempts,blocker,next_action,owner,effect,next_at,updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (task_id, worker, task_id, parent, "r", "[]", "2026-12-01T00:00:00+00:00", status, 1, 3, blocker, "x",
                      "local-mac", effect, next_at.isoformat() if next_at else None, updated.isoformat()))
    for entry in events:
        kind, detail, at = entry if isinstance(entry, tuple) else (entry, '{"result":{}}', updated)
        database.execute("INSERT INTO workflow_task_events(task_id,at,event,detail) VALUES (?,?,?,?)",
                         (task_id, at.isoformat(), kind, detail))


def app_event(database, key, kind, at, detail):
    database.execute("INSERT INTO application_events(job_key,at,event,detail) VALUES (?,?,?,?)", (key, at, kind, json.dumps(detail)))


def research_day(database, day, state, attempt_id, attempt_state, started, result=None, retry=None, waits=0):
    database.execute("INSERT INTO research_days(day,state,attempts,attempt_id,next_retry_at,completed_at,compute_wait_attempts) "
                     "VALUES (?,?,?,?,?,NULL,?)", (day, state, 1 + waits, attempt_id, retry.isoformat() if retry else None, waits))
    database.execute("INSERT INTO research VALUES (?,?,?,?)",
                     (attempt_id, started.isoformat(), attempt_state, json.dumps(result if result is not None else {"state": attempt_state})))


class GatherBase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.database = base.database_with_schema(self.root)
        notifications.initialize(self.database)
        self.now = base.pacific(2026, 10, 22, 10, 0)     # Thursday 10:00; the service day began 09:00

    def tearDown(self):
        self.database.close()
        self.directory.cleanup()

    def gather(self, loop=NOT_RUNNING, **extra):
        self.database.commit()
        return failures_page.gather(self.database, CONFIG, loop, now=self.now, **extra)

    def items(self, view, key):
        found = [section for section in view["sections"] if section["key"] == key]
        return found[0]["entries"] if found else []


class GatherTest(GatherBase):
    def test_example_1_research_failed_today_says_when_it_tries_again(self):
        research_day(self.database, "2026-10-22", "RETRY_WAIT", "a1", "FAILED", base.pacific(2026, 10, 22, 9, 0),
                     result={"state": "FAILED", "error": "Parallel research failed (KeyError)"},
                     retry=base.pacific(2026, 10, 22, 10, 30))
        [item] = self.items(self.gather(), "research")   # F: a pending retry, not "didn't finish today"
        self.assertEqual(item["text"] + " " + item["todo"],
                         "Research will try again at 10:30: today's last try didn't finish because it ran into an "
                         "error. Nothing you need to do.")

    def test_c_an_interrupted_day_is_not_a_failure(self):
        research_day(self.database, "2026-10-22", "RETRY_WAIT", "a1", "INTERRUPTED", base.pacific(2026, 10, 22, 9, 0),
                     result={"state": "INTERRUPTED", "error": "Research stopped"}, retry=base.pacific(2026, 10, 22, 10, 30))
        self.assertEqual(self.items(self.gather(), "research"), [])

    def test_b_a_day_that_is_only_waiting_is_not_a_failure(self):
        industries = [{"industry": "fintech", "state": "COMPLETE"}, {"industry": "health", "state": "COMPUTE_WAIT"},
                      {"industry": "media", "state": "WAITING_SHARED_RESEARCH"}]
        research_day(self.database, "2026-10-22", "RETRY_WAIT", "a1", "FAILED", base.pacific(2026, 10, 22, 9, 0),
                     result={"state": "FAILED", "industries": industries}, retry=base.pacific(2026, 10, 22, 10, 30))
        self.assertEqual(self.items(self.gather(), "research"), [])

    def test_research_compute_wait_and_finished_days_are_not_failures(self):
        research_day(self.database, "2026-10-22", "RETRY_WAIT", "a1", "PARTIAL", base.pacific(2026, 10, 22, 9, 0),
                     result={"state": "PARTIAL", "compute_wait": True}, retry=base.pacific(2026, 10, 22, 10, 30))
        research_day(self.database, "2026-10-21", "COMPLETE", "a2", "COMPLETE", base.pacific(2026, 10, 21, 9, 0))
        self.assertEqual(self.items(self.gather(), "research"), [])
        research_day(self.database, "2026-10-20", "EXHAUSTED", "a3", "FAILED", base.pacific(2026, 10, 20, 9, 0),
                     result={"state": "FAILED", "error": "Parallel research failed (KeyError); inspect"})
        # D: Oct 21 completed after it, so Oct 20's failure is resolved and drops off.
        self.assertEqual(self.items(self.gather(), "research"), [])
        self.database.execute("DELETE FROM research_days WHERE day='2026-10-21'")
        [item] = self.items(self.gather(), "research")
        self.assertEqual(item["text"], "Research didn't finish on Tue Oct 20: it ran into an error.")
        self.assertNotIn("KeyError", item["text"] + item["todo"])

    def test_a_finished_day_with_skipped_entries_shows_them_as_information(self):
        industries = [{"industry": "AI", "state": "COMPLETE", "dropped_entries": [
            {"stage": "discovery", "company": "Airia", "role": None, "reason": "it was listed twice"},
            {"stage": "saving", "company": "WitnessAI", "role": "Staff Engineer",
             "reason": "the role's link was not a secure (https) web address"}]}]
        research_day(self.database, "2026-10-22", "COMPLETE", "a1", "COMPLETE", base.pacific(2026, 10, 22, 9, 0),
                     result={"state": "COMPLETE", "industries": industries})
        view = self.gather()
        # Owner 2026-10-27 ("3:a"): shown, but not counted as a failure anywhere.
        self.assertEqual(self.items(view, "research"), [])
        self.assertEqual((view["sections"], view["needs_you"], view["needs_calm"], view["needs_total"]), ([], [], [], 0))
        self.assertTrue(view["nothing"])
        [section] = view["info_sections"]   # shown in the Research section, not under Needs you
        self.assertEqual(section["key"], "research")
        [item] = section["info"]
        self.assertEqual(item["calm"], "info")
        self.assertEqual(item["text"], "Research finished today. 2 entries were skipped: Airia: it was listed twice; "
                                       "WitnessAI, Staff Engineer: the role's link was not a secure (https) web address.")
        self.assertEqual(item["todo"], "Nothing you need to do. The rest of that day's results were kept.")

    def test_a_finished_day_with_a_session_cut_off_shows_it_as_information(self):
        # R1 review S11: a session that ended early but kept what it saved is shown, never counted.
        note = "A research session ended early (it reached its time limit); kept the 3 roles it saved."
        industries = [{"industry": "AI", "state": "COMPLETE", "session_cut_offs": [{"stage": "qualification", "note": note}]}]
        research_day(self.database, "2026-10-22", "COMPLETE", "a1", "COMPLETE", base.pacific(2026, 10, 22, 9, 0),
                     result={"state": "COMPLETE", "industries": industries})
        view = self.gather()
        self.assertEqual((view["sections"], view["needs_you"], view["needs_calm"], view["needs_total"]), ([], [], [], 0))
        [section] = view["info_sections"]
        [item] = section["info"]
        self.assertEqual(item["calm"], "info")
        self.assertEqual(item["text"], "Research finished today. " + note)
        self.assertTrue(item["todo"].startswith("Nothing you need to do."))

    def test_example_2_a_crashed_job_shows_its_time_and_its_redacted_error_log(self):
        logs = self.root / "logs"
        logs.mkdir()
        (logs / "applications.stderr.log").write_text(
            "Traceback (most recent call last):\nRuntimeError: Authorization: Bearer sk-abc123456789xyz refused\n")
        loop = {"running": True, "last_error": None,
                "job_failures": {"applications": {"exit_code": 1, "at": base.pacific(2026, 10, 22, 10, 30).isoformat()}}}
        self.now = base.pacific(2026, 10, 22, 10, 40)
        view = self.gather(loop, log_tail=lambda name: failures_page.tail(logs, name, [], 20))
        [item] = self.items(view, "scheduler")
        self.assertEqual(item["text"], "Applications stopped with an error at 10:30.")
        self.assertEqual(item["todo"], "Nothing you need to do. It runs again at 11:30.")
        self.assertIn("Traceback (most recent call last):", item["log"])
        self.assertNotIn("sk-abc123456789xyz", "\n".join(item["log"]))
        self.assertIn("<redacted>", "\n".join(item["log"]))

    def test_a_stopped_scheduler_points_to_start(self):
        loop = {"running": False, "last_error": "start run failed: OSError: token=abc123secret",
                "job_failures": {"run": {"exit_code": 2, "at": "not a time"}}}
        found = self.items(self.gather(loop), "scheduler")
        self.assertEqual(found[0]["text"], "The hourly round stopped with an error.")
        self.assertEqual(found[0]["fix"], {"label": "Open Overview", "href": "/"})
        self.assertEqual(found[1]["text"], "Sidedoor ran into a problem keeping its schedule.")
        self.assertNotIn("abc123secret", found[1]["detail"])

    def test_example_4_fresh_install_has_nothing(self):
        view = failures_page.gather(None, CONFIG, NOT_RUNNING, now=self.now)
        self.assertTrue(view["nothing"])
        self.assertEqual((view["sections"], view["needs_you"]), ([], []))
        self.assertTrue(self.gather()["nothing"])   # the schema with no rows

    def test_tasks_count_only_real_failures(self):
        before = self.now - dt.timedelta(hours=1)
        retry = self.now + dt.timedelta(minutes=45)
        task(self.database, "t1", "reply-monitor", "BLOCKED", before, "WORKER_EXCEPTION", events=("BLOCKED",))
        task(self.database, "t2", "reply-monitor", "BLOCKED", before - dt.timedelta(hours=1), "WORKER_EXCEPTION", events=("BLOCKED",))
        task(self.database, "t3", "message_writer", "RETRY_WAIT", before, "OUTREACH_COMPOSITION_ValueError", retry,
             events=("CLAIMED", "RETRY_WAIT"))
        task(self.database, "t4", "message_writer", "RETRY_WAIT", before, "OUTREACH_AI_SHARED_BUDGET_EXHAUSTED", retry,
             events=("CLAIMED", "RETRY_WAIT"))                                         # a budget wait by its code
        task(self.database, "t5", "engagement_review", "RETRY_WAIT", before, "SOMETHING", retry,
             events=("CLAIMED", "COMPUTE_WAIT"))                                       # a compute wait by its event
        task(self.database, "t6", "pipeline_routing_review", "BLOCKED", before, "REPLY_SENDER_IDENTITY_UNRESOLVED")  # a hold
        task(self.database, "t7", "outreach-draft-composition", "EXHAUSTED", before, "DEADLINE_OR_RETRY_LIMIT",
             events=("RETRY_WAIT", "RETRY_WAIT", "EXHAUSTED"))
        task(self.database, "t8", "mystery-worker", "UNCERTAIN", before, "INTERRUPTED_ATTEMPT", events=("UNCERTAIN",))
        task(self.database, "t9", "reply-monitor", "BLOCKED", self.now - dt.timedelta(days=15), "WORKER_EXCEPTION")  # too old (D8)
        view = self.gather()
        outreach = {item["text"]: item for item in self.items(view, "outreach")}
        self.assertEqual(set(outreach), {"Reading your LinkedIn inbox stopped with an error.",
                                         "Writing a follow-up message didn't work.",
                                         "Writing an invitation note kept failing, so Sidedoor stopped trying."})
        self.assertEqual(outreach["Reading your LinkedIn inbox stopped with an error."]["count"], 2)
        self.assertEqual(outreach["Writing a follow-up message didn't work."]["todo"],
                         "Nothing you need to do. Sidedoor will try again at 10:45.")
        self.assertEqual(outreach["Writing an invitation note kept failing, so Sidedoor stopped trying."]["count"], 3)
        [other] = self.items(view, "other")
        self.assertEqual(other["text"], "A background task was interrupted before it finished.")

    def test_applications_needs_you_and_failures_but_not_waits_holds_give_ups_or_decisions(self):
        recent = (self.now - dt.timedelta(hours=2)).isoformat()
        rows = (("own", "BLOCKED", "DESIGNATED_RESUME_MISMATCH", None), ("fail", "BLOCKED", "FILE_UPLOAD_FAILED",
                (self.now + dt.timedelta(hours=1)).isoformat()), ("odd", "RETRY_WAIT", "ValueError: boom", None),
                ("wait", "BLOCKED", "APPLICATION_BROWSER_SHARED_BUDGET_EXHAUSTED", None),
                ("check", "BLOCKED", "ELIGIBILITY_UNVERIFIED", None), ("access", "BLOCKED", "CAPTCHA_REQUIRED", None),
                ("gone", "EXHAUSTED", "FILE_UPLOAD_FAILED_3_TIMES", None), ("no", "INELIGIBLE", "OUTSIDE_CONFIRMED_SCOPE", None))
        for key, state, code, next_at in rows:
            self.database.execute("INSERT INTO application_jobs(job_key,employer,requisition,payload,state,blocker,next_at,updated_at) "
                                  "VALUES (?,?,?,?,?,?,?,?)", (key, "acme", key, json.dumps({"company": "Acme", "role": key.title()}),
                                                               state, code, next_at, recent))
        view = self.gather()
        [you] = view["needs_you"]
        self.assertEqual(you["text"], "Acme, Own: Your résumé file changed since you set it.")
        self.assertEqual(you["fix"], {"label": "Open Settings", "href": "/settings#resume"})
        texts = [item["text"] for item in self.items(view, "applications")]
        # Given-up jobs are listed too (owner 2026-10-25); F4: an unknown code is a hold, not a failure.
        self.assertEqual(sorted(texts), ["Acme, Fail: The site did not take the résumé upload.",
                                         "Acme, Gone: The site refused the résumé upload three times."])
        for item in self.items(view, "applications"):
            self.assertNotRegex(item["text"] + item["todo"], r"[A-Z]{3,}_[A-Z_]+|ValueError")

    def test_outreach_writes_and_notifications(self):
        recent = self.now - dt.timedelta(hours=3)
        base.invite_action(self.database, "i1", "INVITE_RETRY_WAIT", recent, recent)
        base.invite_action(self.database, "i2", "INVITE_RETRY_WAIT", recent, recent)
        base.invite_action(self.database, "r1", "UNCERTAIN_NO_RETRY", recent, recent, payload=json.dumps({"purpose": "REPLY"}))
        base.invite_action(self.database, "w1", "ACKNOWLEDGED_DELIVERY_UNCONFIRMED", recent, recent)   # waiting, not failed
        for key, state, error, attempts in (("n1", "FAILED", "NOTIFICATION_RETRIES_EXHAUSTED", 5),
                                            ("n2", "PENDING", "TELEGRAM_RATE_LIMIT_OR_CONFIRMED_TRANSIENT", 1),
                                            ("n3", "PENDING", None, 0), ("n4", "DELIVERED", None, 1)):
            self.database.execute("INSERT INTO notification_outbox(event_key,message,state,attempts,next_at,last_error,updated_at) "
                                  "VALUES (?,?,?,?,?,?,?)", (key, "m", state, attempts,
                                                             (self.now + dt.timedelta(minutes=20)).isoformat(), error, recent.isoformat()))
        view = self.gather()
        self.assertEqual(sorted(item["text"] for item in self.items(view, "outreach")),
                         ["1 reply didn't go through.", "2 invitations didn't go through."])
        self.assertEqual(sorted(item["text"] for item in self.items(view, "connections")),
                         ["1 notification couldn't be delivered.", "1 notification didn't go through yet."])

    def test_a_not_sent_invitation_shows_its_real_reason(self):
        # GL-L1: an invitation the owner had already made is not "couldn't be sent after 3 tries".
        recent = self.now - dt.timedelta(hours=3)
        elsewhere = json.dumps({"attempts": [{"started_at": recent.isoformat()}],
                                "not_sent": {"reason": "ALREADY_CONTACTED_ELSEWHERE", "by": "PENDING"}})
        base.invite_action(self.database, "e1", "CONFIRMED_NOT_SENT", recent, recent, result=elsewhere)
        base.invite_action(self.database, "t1", "CONFIRMED_NOT_SENT", recent, recent)
        self.assertEqual(sorted(item["text"] for item in self.items(self.gather(), "outreach")),
                         ["1 invitation couldn't be sent after 3 tries.",
                          "You'd already invited or connected with them yourself, so Sidedoor didn't count this as its "
                          "invitation."])

    def test_hourly_round_failures(self):
        self.database.execute("INSERT INTO runs VALUES (?,?,?,?,?)", ("s1", (self.now - dt.timedelta(hours=1)).isoformat(),
                                                                       "FAILED", None, "{}"))
        self.database.execute("INSERT INTO runs VALUES (?,?,?,?,?)", ("s2", (self.now - dt.timedelta(hours=2)).isoformat(),
                                                                       "DEGRADED", None, "{}"))
        [item] = self.items(self.gather(), "scheduler")
        self.assertEqual(item["text"], "The hourly round stopped with an error.")

    def test_malformed_rows_never_raise(self):
        self.database.execute("INSERT INTO runs VALUES ('x','garbage','FAILED',NULL,NULL)")
        task(self.database, "t1", "reply-monitor", "RETRY_WAIT", self.now, "X")
        self.database.execute("UPDATE workflow_tasks SET updated_at='nonsense', next_at='also nonsense'")
        self.database.execute("INSERT INTO application_jobs(job_key,employer,requisition,payload,state,blocker,next_at,updated_at) "
                              "VALUES ('j','acme','j','{not json',?,?,?,?)", ("BLOCKED", "FILE_UPLOAD_FAILED", "bad", "bad"))
        self.database.execute("INSERT INTO outreach_actions VALUES ('a','i','s','{bad',?,?,?,?)",
                              ("h", "UNCERTAIN_NO_RETRY", "2026-10-22T16:00:00+00:00", None))
        self.database.execute("INSERT INTO research_days(day,state,attempts,attempt_id) VALUES ('2026-10-22','RETRY_WAIT',1,'gone')")
        self.database.execute("INSERT INTO notification_outbox(event_key,message,state,attempts,updated_at) VALUES ('n','m','FAILED',1,'bad')")
        view = self.gather()
        self.assertFalse(view["nothing"])
        [job] = self.items(view, "applications")
        self.assertEqual(job["text"], "acme: The site did not take the résumé upload.")

    def test_an_older_schema_degrades_only_its_own_section(self):
        self.database.close()
        path = self.root / "old.sqlite3"
        self.database = sqlite3.connect(path)
        self.database.row_factory = sqlite3.Row
        self.database.execute("CREATE TABLE workflow_tasks (task_id TEXT, worker TEXT, status TEXT, updated_at TEXT)")
        self.database.execute("CREATE TABLE workflow_task_events (id INTEGER PRIMARY KEY, task_id TEXT, at TEXT, event TEXT)")
        self.database.execute("CREATE TABLE runs (slot TEXT, started_at TEXT, state TEXT, completed_at TEXT, result TEXT)")
        self.database.execute("INSERT INTO runs VALUES ('s',?,'FAILED',NULL,NULL)", ((self.now - dt.timedelta(hours=1)).isoformat(),))
        view = self.gather()
        sections = {section["key"]: section for section in view["sections"]}
        self.assertEqual(len(sections["scheduler"]["entries"]), 1)
        self.assertTrue(sections["outreach"]["unavailable"])
        self.assertNotIn("research", sections)

    def test_locked_is_not_degraded(self):
        with mock.patch.object(failures_page, "task_failures", side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertRaises(sqlite3.OperationalError):
                self.gather()



class ReviewFixTest(GatherBase):
    """The owner's decisions (D5, D8) and the approved review fixes (F1-F14), one test each."""

    def job_row(self, key, state, code, updated=None):
        self.database.execute("INSERT INTO application_jobs(job_key,employer,requisition,payload,state,blocker,updated_at) "
                              "VALUES (?,?,?,?,?,?,?)", (key, "acme", key, json.dumps({"company": "Acme", "role": key}), state,
                                                         code, (updated or self.now - dt.timedelta(hours=1)).isoformat()))

    def test_given_up_jobs_are_failures_but_not_rejections_or_closures(self):
        # Owner 2026-10-25 (replacing D5): Together AI's shape, three tool-limit reads then RETRIES_EXHAUSTED.
        self.job_row("together", "EXHAUSTED", "RETRIES_EXHAUSTED")
        for hours in (5, 4, 3):
            app_event(self.database, "together", "BLOCKED", (self.now - dt.timedelta(hours=hours)).isoformat(),
                      {"code": "APPLICATION_TOOL_LIMIT", "route": "ORDINARY"})
        self.job_row("left", "ABANDONED", "ACCESS_BLOCKED_3_TIMES")
        app_event(self.database, "left", "BLOCKED", (self.now - dt.timedelta(hours=2)).isoformat(),
                  {"code": "CAPTCHA_REQUIRED", "route": "ORDINARY"})
        self.job_row("mine", "ABANDONED", "APPLICATION_REJECTED_BY_YOU")
        self.job_row("no", "INELIGIBLE", "OUTSIDE_CONFIRMED_SCOPE")
        self.job_row("same", "DUPLICATE", "SAME_POSTING_AS_EXISTING_JOB")
        self.job_row("old", "EXHAUSTED", "RETRIES_EXHAUSTED", self.now - dt.timedelta(days=15))
        items = {item["refs"]["job_key"]: item for item in self.items(self.gather(), "applications")}
        self.assertEqual(sorted(items), ["left", "together"])
        together = items["together"]
        self.assertEqual(together["text"], "Acme, together: Sidedoor used all of its tries.")
        self.assertEqual(together["todo"], "Nothing was sent. Sidedoor will not try this one again.")
        self.assertEqual(together["count"], 3)
        self.assertEqual([(r["words"], r["count"]) for r in together["reasons"]],
                         [("The browser session used all of its allowed steps before it finished.", 3)])
        self.assertEqual(together["href"], "/applications?job=together")
        self.assertEqual(items["left"]["reasons"][0]["words"], "The application site asked for a human check.")

    def test_given_up_nothing_sent_only_when_true(self):
        self.job_row("sent", "EXHAUSTED", "RETRIES_EXHAUSTED")
        self.database.execute("INSERT INTO application_attempts(attempt_id,job_key,idempotency_key,started_at,state) VALUES ('a','sent','sent',?,?)",
                              (self.now.isoformat(), "SUBMITTING"))
        [item] = self.items(self.gather(), "applications")
        self.assertNotIn("Nothing was sent", item["todo"])

    def test_owner_picked_stops_count_while_retrying(self):
        codes = ("APPLICATION_BROWSER_EXECUTOR_FAILED", "APPLICATION_BROWSER_FAILED", "RECONCILED_ABSENT",
                 "FORM_UNAVAILABLE", "APPLICATION_TOOL_LIMIT")
        for number, code in enumerate(codes):
            self.job_row("k%d" % number, "RETRY_WAIT", code)
            for hours in (3, 2):
                app_event(self.database, "k%d" % number, "RETRY_WAIT", (self.now - dt.timedelta(hours=hours)).isoformat(), {"code": code})
        self.job_row("login", "BLOCKED", "AUTHENTICATION_REQUIRED")   # D6: not counted while retrying
        items = {item["refs"]["job_key"]: item for item in self.items(self.gather(), "applications")}
        self.assertEqual(sorted(items), ["k0", "k1", "k2", "k3", "k4"])
        for item in items.values():
            self.assertEqual(item["count"], 2)
            self.assertNotRegex(item["text"] + item["todo"], r"[A-Z]{3,}_[A-Z_]+|\u2014")
        self.assertEqual(items["k2"]["text"], "Acme, k2: The submit try did not go through. "
                                              "Sidedoor checked and found no application at the site.")

    def test_d8_the_window_is_fourteen_days(self):
        task(self.database, "old", "reply-monitor", "BLOCKED", self.now - dt.timedelta(days=13), "WORKER_EXCEPTION",
             events=("BLOCKED",))
        task(self.database, "older", "guarded-outreach", "BLOCKED", self.now - dt.timedelta(days=15), "WORKER_EXCEPTION",
             events=("BLOCKED",))
        self.assertEqual([item["text"] for item in self.items(self.gather(), "outreach")],
                         ["Reading your LinkedIn inbox stopped with an error."])

    def test_f1_one_hourly_crash_is_one_item_carrying_the_job_log(self):
        crashed = self.now - dt.timedelta(minutes=40)
        task(self.database, "t1", "reply-monitor", "BLOCKED", crashed, "WORKER_EXCEPTION", events=("BLOCKED",), parent="s")
        self.database.execute("INSERT INTO runs VALUES ('s',?,'FAILED',?,NULL)",
                              ((self.now - dt.timedelta(hours=1)).isoformat(), (self.now - dt.timedelta(minutes=30)).isoformat()))
        loop = {"running": True, "last_error": None,
                "job_failures": {"run": {"exit_code": 1, "at": (self.now - dt.timedelta(minutes=30)).isoformat()}}}
        view = self.gather(loop, log_tail=lambda name: ["Traceback from " + name])
        everything = [item for section in view["sections"] for item in section["entries"]]
        self.assertEqual([item["text"] for item in everything], ["Reading your LinkedIn inbox stopped with an error."])
        self.assertEqual(everything[0]["log"], ["Traceback from launchd.stderr.log"])

    def test_f1_a_research_failure_and_its_job_exit_are_one_item(self):
        research_day(self.database, "2026-10-22", "RETRY_WAIT", "a1", "FAILED", base.pacific(2026, 10, 22, 9, 0),
                     result={"state": "FAILED", "industries": [{"industry": "health", "state": "FAILED"}]},
                     retry=base.pacific(2026, 10, 22, 10, 30))
        loop = {"running": True, "last_error": None,
                "job_failures": {"research": {"exit_code": 1, "at": base.pacific(2026, 10, 22, 9, 40).isoformat()}}}
        view = self.gather(loop)
        everything = [item["text"] for section in view["sections"] for item in section["entries"]]
        self.assertEqual(everything, ["Research will try again at 10:30: today's last try didn't finish because no "
                                      "industry finished its search."])
        # An exit nothing explains still shows on its own (worked example 2).
        loop["job_failures"]["run"] = {"exit_code": 1, "at": base.pacific(2026, 10, 22, 9, 50).isoformat()}
        self.assertEqual([item["text"] for item in self.items(self.gather(loop), "scheduler")],
                         ["The hourly round stopped with an error at 09:50."])

    def test_e1_an_exhausted_day_and_a_late_research_exit_are_one_item(self):
        self.now = base.pacific(2026, 10, 22, 22, 0)
        research_day(self.database, "2026-10-22", "EXHAUSTED", "a1", "FAILED", base.pacific(2026, 10, 22, 9, 0),
                     result={"state": "FAILED", "error": "Parallel research failed (KeyError)"})
        loop = {"running": True, "last_error": None,
                "job_failures": {"research": {"exit_code": 1, "at": base.pacific(2026, 10, 22, 21, 30).isoformat()}}}
        everything = [item["text"] for section in self.gather(loop)["sections"] for item in section["entries"]]
        self.assertEqual(everything, ["Research didn't finish today: it ran into an error."])

    def test_f2_a_reply_with_tries_left_is_retried_not_abandoned(self):
        recent = self.now - dt.timedelta(hours=3)
        base.invite_action(self.database, "r1", "UNCERTAIN_NO_RETRY", recent, recent, payload=json.dumps({"purpose": "REPLY"}),
                           result=json.dumps({"provider_request": {"started_at": recent.isoformat()}}))
        [item] = self.items(self.gather(), "outreach")
        self.assertEqual(item["text"], "1 reply didn't go through.")
        self.assertIn("sends it again only if it isn't there", item["todo"])
        self.database.execute("UPDATE outreach_actions SET result=?", (json.dumps({"provider_request": {}, "reply_retries": [{}, {}]}),))
        [item] = self.items(self.gather(), "outreach")
        self.assertEqual(item["text"], "Sidedoor couldn't confirm whether 1 reply went out.")
        self.assertIn("keeps checking the conversation but won't send it again", item["todo"])
        self.database.execute("UPDATE outreach_actions SET result=?", (json.dumps({"provider_request": {}, "reply_no_more_sends": True}),))
        self.assertIn("won't send it again", self.items(self.gather(), "outreach")[0]["todo"])

    def test_f4_failures_agree_with_the_applications_page(self):
        import applications_page
        codes = {"hold1": "APPLICATION_READ_AWAITING_NEW_INPUT", "hold2": "SCOPE_REVIEW_REQUIRED",
                 "wait": "APPLICATION_BROWSER_CYCLE_CAPACITY", "access": "MFA_REQUIRED", "unknown": "ValueError: boom",
                 "upload": "FILE_UPLOAD_FAILED", "read": "INSPECTION_INCOMPLETE", "tool": "BROWSER_CAPABILITY_UNAVAILABLE",
                 "lost": "SUBMISSION_OBSERVATION_LOST", "owner": "REQUIRED_ELIGIBILITY_ANSWER",
                 "reported": "SUBMISSION_REPORTED_FAILED"}
        for key, code in codes.items():
            self.job_row(key, "BLOCKED", code)
        view = self.gather()
        shown = {item["href"].split("=")[-1]: item for item in self.items(view, "applications")}
        self.assertEqual(set(shown), {"upload", "read", "tool", "reported"})
        self.assertEqual([item["href"].split("=")[-1] for item in view["needs_you"]], ["owner"])
        listing = applications_page.gather(self.database, CONFIG, None, now=self.now)
        rows = {row["key"]: row for row in listing["list"]["jobs"]}
        for key, item in shown.items():
            self.assertEqual((rows[key]["needs_you"], rows[key]["state"]), (False, "Waiting to retry"))
            self.assertIn(rows[key]["problem"], item["text"])
            self.assertIn(rows[key]["todo"], item["todo"])
        self.assertTrue(rows["owner"]["needs_you"])
        self.assertEqual(listing["counts"]["needs_you"], len(view["needs_you"]))

    def test_f6_an_interrupted_write_says_what_is_true(self):
        task(self.database, "w", "message_writer", "UNCERTAIN", self.now - dt.timedelta(hours=1), "INTERRUPTED_ATTEMPT",
             events=("UNCERTAIN",), effect="WRITE")
        task(self.database, "h", "guarded-outreach", "UNCERTAIN", self.now - dt.timedelta(hours=1), "INTERRUPTED_ATTEMPT",
             events=("UNCERTAIN",), effect="WRITE")
        items = {item["text"]: item["todo"] for item in self.items(self.gather(), "outreach")}
        self.assertEqual(items["Writing a follow-up message was interrupted while making a change, and Sidedoor can't "
                               "tell whether the change went through."],
                         "Sidedoor won't repeat it on its own, so nothing happens twice. If it was a message to someone, "
                         "look at that conversation on LinkedIn.")
        self.assertIn("The next round runs it again", items["Sending invitations was interrupted before it finished."])
        self.assertNotIn("checks what happened", " ".join(items.values()))

    def test_f7_uncertain_notifications_use_the_window_and_say_when_checking_stopped(self):
        for key, checks, updated in (("old", 0, self.now - dt.timedelta(days=20)), ("live", 1, self.now),
                                     ("spent", 5, self.now)):
            self.database.execute("INSERT INTO notification_outbox(event_key,message,state,attempts,updated_at,reconcile_attempts) "
                                  "VALUES (?,?,?,?,?,?)", (key, "m", "UNCERTAIN", 1, updated.isoformat(), checks))
        texts = sorted(item["text"] for item in self.items(self.gather(), "connections"))
        self.assertEqual(texts, ["Sidedoor couldn't confirm whether 1 notification arrived, and has stopped checking.",
                                 "Sidedoor couldn't confirm whether 1 notification arrived."])

    def test_f8_counts_are_failed_tries_only(self):
        at = self.now - dt.timedelta(hours=2)
        task(self.database, "t", "message_writer", "RETRY_WAIT", at, "X", self.now + dt.timedelta(hours=1),
             events=("COMPUTE_WAIT", "COMPUTE_WAIT", "PROVIDER_USAGE_LIMIT_WAIT", "RETRY_WAIT", ("EXHAUSTED", "{}", at)))
        [item] = self.items(self.gather(), "outreach")
        self.assertEqual(item["count"], 1)
        self.job_row("j", "BLOCKED", "FILE_UPLOAD_FAILED")
        for code in ("FILE_UPLOAD_FAILED", "APPLICATION_BROWSER_SHARED_BUDGET_EXHAUSTED", "ELIGIBILITY_UNVERIFIED",
                     "FILE_UPLOAD_FAILED"):
            self.database.execute("INSERT INTO application_events(job_key,at,event,detail) VALUES ('j',?,'BLOCKED',?)",
                                  (at.isoformat(), json.dumps({"code": code})))
        [job] = self.items(self.gather(), "applications")
        self.assertEqual(job["count"], 2)

    def test_f11_running_out_of_time_waiting_for_compute_is_not_kept_failing(self):
        task(self.database, "t", "message_writer", "EXHAUSTED", self.now - dt.timedelta(hours=1), "DEADLINE_OR_RETRY_LIMIT",
             events=("COMPUTE_WAIT", "COMPUTE_WAIT", ("EXHAUSTED", "{}", self.now - dt.timedelta(hours=1))))
        [item] = self.items(self.gather(), "outreach")
        self.assertEqual(item["text"], "Writing a follow-up message ran out of time while waiting for compute, so Sidedoor "
                                       "stopped trying.")

    def test_f12_a_real_failure_followed_by_a_wait_still_shows(self):
        task(self.database, "t", "message_writer", "RETRY_WAIT", self.now - dt.timedelta(hours=1), "DAILY_COMPUTE_BUDGET_EXHAUSTED",
             self.now + dt.timedelta(hours=2), events=("RETRY_WAIT", "COMPUTE_WAIT"))
        [item] = self.items(self.gather(), "outreach")
        self.assertEqual(item["text"], "Writing a follow-up message didn't work.")
        task(self.database, "w", "engagement_review", "RETRY_WAIT", self.now - dt.timedelta(hours=1), "X",
             self.now + dt.timedelta(hours=2), events=("COMPUTE_WAIT",))
        self.assertEqual(len(self.items(self.gather(), "outreach")), 1)   # waits alone still never show

    # --- Confirming review of e416ca5 (owner-approved items 1-4) ---------------------------------------

    def test_r1_an_interrupted_round_is_not_explained_by_a_later_rounds_crash(self):
        self.database.execute("INSERT INTO runs VALUES ('old',?,'INTERRUPTED',NULL,NULL)",
                              ((self.now - dt.timedelta(hours=5)).isoformat(),))
        task(self.database, "t", "reply-monitor", "BLOCKED", self.now - dt.timedelta(hours=3), "WORKER_EXCEPTION",
             events=("BLOCKED",), parent="later")
        everything = [item["text"] for section in self.gather()["sections"] for item in section["entries"]]
        self.assertEqual(sorted(everything), ["Reading your LinkedIn inbox stopped with an error.",
                                              "The hourly round was interrupted before it finished."])

    def test_r2_a_newer_exit_is_not_folded_into_an_older_rounds_crash(self):
        start, crashed = self.now - dt.timedelta(minutes=95), self.now - dt.timedelta(minutes=90)
        self.database.execute("INSERT INTO runs VALUES ('done',?,'DEGRADED',?,NULL)",
                              (start.isoformat(), (self.now - dt.timedelta(minutes=85)).isoformat()))
        task(self.database, "t", "reply-monitor", "BLOCKED", crashed, "WORKER_EXCEPTION", events=("BLOCKED",), parent="done")
        loop = {"running": True, "last_error": None,
                "job_failures": {"run": {"exit_code": 1, "at": (self.now - dt.timedelta(minutes=10)).isoformat()}}}
        everything = [item["text"] for section in self.gather(loop)["sections"] for item in section["entries"]]
        self.assertEqual(sorted(everything), ["Reading your LinkedIn inbox stopped with an error.",
                                              "The hourly round stopped with an error at 09:50."])

    def test_r3_budget_only_retry_waits_count_nothing(self):
        at = self.now - dt.timedelta(hours=1)
        wait = ("RETRY_WAIT", json.dumps({"result": {}, "blocker": "OUTREACH_AI_SHARED_BUDGET_EXHAUSTED"}), at)
        real = ("RETRY_WAIT", json.dumps({"result": {}, "blocker": "OUTREACH_COMPOSITION_ValueError"}), at)
        task(self.database, "w", "message_writer", "RETRY_WAIT", at, "OUTREACH_AI_SHARED_BUDGET_EXHAUSTED",
             self.now + dt.timedelta(hours=1), events=(wait, wait, wait))
        self.assertEqual(self.items(self.gather(), "outreach"), [])
        task(self.database, "r", "outreach-draft-inspection", "RETRY_WAIT", at, "OUTREACH_AI_SHARED_BUDGET_EXHAUSTED",
             self.now + dt.timedelta(hours=1), events=(real, wait, real))
        [item] = self.items(self.gather(), "outreach")
        self.assertEqual((item["text"], item["count"]), ("Checking a note before it is sent didn't work.", 2))

    def test_r3_finish_records_the_blocker_with_its_event(self):
        import workflow_tasks
        stamp = self.now.isoformat()
        task_id = workflow_tasks.enqueue(self.database, "message_writer", "a", "p", [{"source": "s", "observed_at": stamp}],
                                         (self.now + dt.timedelta(days=1)).isoformat(), stamp)
        self.database.commit()
        claim = workflow_tasks.claim(self.database, task_id, stamp)
        workflow_tasks.finish(self.database, task_id, claim["token"], stamp, {}, [], status="RETRY_WAIT",
                              blocker="OUTREACH_AI_SHARED_BUDGET_EXHAUSTED", next_action="wait")
        self.database.commit()
        detail = json.loads(self.database.execute("SELECT detail FROM workflow_task_events WHERE task_id=? AND event='RETRY_WAIT'",
                                                  (task_id,)).fetchone()[0])
        self.assertEqual(detail["blocker"], "OUTREACH_AI_SHARED_BUDGET_EXHAUSTED")
        self.assertEqual(self.items(self.gather(), "outreach"), [])

    def test_r4_a_reported_submit_failure_shows_in_both_views(self):
        import applications_page
        self.job_row("rep", "BLOCKED", "SUBMISSION_REPORTED_FAILED")
        [item] = self.items(self.gather(), "applications")
        self.assertEqual(item["text"], "Acme, rep: The application page reported a problem after Submit.")
        listing = applications_page.gather(self.database, CONFIG, None, now=self.now)
        [row] = listing["list"]["jobs"]
        self.assertEqual((row["state"], row["problem"]), ("Waiting to retry", "The application page reported a problem after Submit."))

class PolishTest(GatherBase):
    """Owner, 2026-10-24: questions and answered jobs are calm, causes never repeat the headline or show
    engineers' notes, codes lead with words, day-old items fold, runs are named, empty logs share a line."""

    def job_row(self, key, state, code, updated):
        self.database.execute("INSERT INTO application_jobs(job_key,employer,requisition,payload,state,blocker,updated_at) "
                              "VALUES (?,?,?,?,?,?,?)", (key, "acme", key, json.dumps({"company": "Acme", "role": key}), state,
                                                         code, updated.isoformat()))

    def test_questions_and_answered_jobs_are_calm_and_never_counted(self):
        recent = self.now - dt.timedelta(hours=1)
        unknown = "UNKNOWN_REQUIRED_ANSWERS:unidentified-question:gpa:2d8b"
        self.job_row("asked", "BLOCKED", unknown, recent)
        base.question(self.database, "q1", "asked", self.now)
        base.question(self.database, "q2", "asked", self.now)
        self.job_row("answered", "BLOCKED", unknown, recent)
        base.question(self.database, "q3", "answered", self.now, state="ANSWERED")
        self.job_row("unreadable", "BLOCKED", failures_page.applications_page.FORM_NOT_READABLE, recent)
        view = self.gather()
        asked, answered = view["needs_calm"]
        self.assertEqual((asked["calm"], asked["text"], asked["fix"]),
                         ("question", "Answer Sidedoor's questions about Acme, asked.",
                          {"label": "Answer on Approvals", "href": "/approvals#questions"}))
        self.assertEqual((answered["calm"], answered["text"]), ("waiting", "Acme, answered: your answers are in."))
        self.assertIn("Waiting for its next run", answered["todo"])
        # Only the unreadable form (a real hold) is red, and only it reaches doctor and Report a problem.
        self.assertEqual([entry["refs"]["job_key"] for entry in view["needs_you"]], ["unreadable"])
        self.assertEqual(self.items(view, "applications"), [])

    def test_one_needs_you_rule_overview_applications_and_failures_agree(self):
        import applications_page
        recent = self.now - dt.timedelta(hours=1)
        unknown = "UNKNOWN_REQUIRED_ANSWERS:unidentified-question:gpa:2d8b"
        self.job_row("asked", "BLOCKED", unknown, recent)
        base.question(self.database, "q1", "asked", self.now)
        self.job_row("answered", "BLOCKED", unknown, recent)
        self.job_row("resume", "BLOCKED", "DESIGNATED_RESUME_MISMATCH", recent)
        self.job_row("unreadable", "BLOCKED", applications_page.FORM_NOT_READABLE, recent)
        view = self.gather()
        needs = overview_page.needs_you(overview_page.Reader(self.database), self.now)
        listing = applications_page.gather(self.database, CONFIG, None, now=self.now)
        chip = applications_page.gather(self.database, CONFIG, None, show="needs_you", now=self.now)
        grouped = [job["key"] for item in listing["list"]["groups"] if item["key"] == "needs_you" for job in item["jobs"]]
        failing = sorted(entry["refs"]["job_key"] for entry in view["needs_you"])
        self.assertEqual(failing, ["resume", "unreadable"])
        self.assertEqual(sorted(grouped), failing)
        self.assertEqual(sorted(job["key"] for job in chip["list"]["jobs"]), failing)
        self.assertEqual((needs["applications"], listing["counts"]["needs_you"], needs["questions"]), (2, 2, 1))

    def test_causes_say_the_headline_once_without_engineer_notes(self):
        self.job_row("retry", "RETRY_WAIT", "ANSWERING_RETRY", self.now - dt.timedelta(hours=1))
        self.database.execute("INSERT INTO application_events(job_key,event,at,detail) VALUES (?,?,?,?)",
                              ("retry", "RETRY_WAIT", (self.now - dt.timedelta(hours=1)).isoformat(),
                               json.dumps({"code": "ANSWERING_RETRY", "next_action": "Re-run the AI answering step on the next cycle"})))
        [item] = self.items(self.gather(), "applications")
        [reason] = item["reasons"]
        self.assertEqual((reason["words"], reason["where"], reason["error"], reason["code"]), (None, None, None, "ANSWERING_RETRY"))

    def test_an_exception_code_leads_with_plain_words(self):
        self.assertEqual(failures_page.cause_words("LINKEDIN_CONTACTS_KeyError"), "The contact finder hit an internal error.")
        self.assertEqual(failures_page.cause_words("ValueError"), "Sidedoor hit an internal error.")
        self.assertEqual(failures_page.cause_words("SOMETHING_NEW"), "Something went wrong.")

    def test_items_older_than_a_day_fold_into_earlier(self):
        self.database.execute("INSERT INTO runs VALUES ('old',?,'INTERRUPTED',NULL,NULL)", ((self.now - dt.timedelta(hours=30)).isoformat(),))
        self.database.execute("INSERT INTO runs VALUES ('new',?,'FAILED',?,NULL)",
                              ((self.now - dt.timedelta(hours=2)).isoformat(), (self.now - dt.timedelta(hours=2)).isoformat()))
        [section] = [s for s in self.gather()["sections"] if s["key"] == "scheduler"]
        self.assertEqual([entry["code"] for entry in section["recent"]], ["ROUND_FAILED"])
        self.assertEqual([entry["code"] for entry in section["earlier"]], ["ROUND_INTERRUPTED"])
        self.assertEqual(len(section["entries"]), 2)   # readers of the full list are unchanged

    def test_runs_are_named_by_what_they_were_about(self):
        folder = self.root / "reports" / "application-recovery" / "decision-ab_1"
        folder.mkdir(parents=True)
        (self.root / "reports" / "application-recovery" / "decision-zz").mkdir()
        (self.root / "reports" / "email-check" / "check-x1").mkdir(parents=True)
        self.job_row("cf:1", "BLOCKED", "FILE_UPLOAD_FAILED", self.now)
        self.database.execute("INSERT INTO application_events(job_key,event,at,detail) VALUES (?,?,?,?)",
                              ("cf:1", "APPLICATION_RECOVERY_DECIDED", self.now.isoformat(),
                               json.dumps({"decision": {"artifacts": {"directory": str(folder)}}})))
        logs = self.root / "logs"
        logs.mkdir()
        for label in ("ai-1", "cloud-2"):
            (logs / ("7feefb1b-f6b3-45d2-8d2d-69d916574ff4.%s.1.discovery.log" % label)).write_text("x\n")
        self.database.commit()
        names = failures_page.run_names(overview_page.Reader(self.database))
        runs, _ = failures_page.runs_paged(self.root, logs, names=names)
        self.assertEqual(sorted(run["label"] for run in runs),
                         ["Email check", "Recovery decision decision-zz", "Recovery decision \u00b7 Acme, cf:1",
                          "Research \u00b7 2 industries"])

    def test_empty_and_missing_logs_share_one_line(self):
        logs = self.root / "logs"
        logs.mkdir()
        (logs / "research.stderr.log").write_text("")
        (logs / "launchd.stderr.log").write_text("2026-10-22T17:00:00Z boom\n")
        view = failures_page.logs_view(logs, [])
        self.assertEqual([stream["name"] for stream in view["boxes"]], ["launchd.stderr.log"])
        self.assertEqual(view["quiet"], ["No errors from: Research, Applications, Sidedoor.",
                                         "No output from: Hourly round, Research, Applications, Sidedoor."])
        self.assertEqual(len(view["streams"]), 8)   # the download still lists every stream


class LogTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.logs = Path(self.directory.name) / "logs"
        self.logs.mkdir()

    def tearDown(self):
        self.directory.cleanup()

    def test_example_3_keys_and_tokens_never_show(self):
        (self.logs / "launchd.stderr.log").write_text("\n".join((
            "request failed: Authorization: Bearer sk-zzzQQQwwwRRR",
            "OpenRouter refused key " + KEY,
            "license " + LICENSE + " checked",
            "opened http://127.0.0.1:47823/?t=dashboardtoken123",
            "hosted auth https://account.unipile.com/abc?dsn=QUJDREVGR0g=",
            "api_key=hunter2hunter2 cookie: session=abc",
            "a split key " + KEY[10:24],
            "\x1b[31mcoloured\x1b[0m plain line")) + "\n")
        view = failures_page.logs_view(self.logs, [KEY, LICENSE, "dashboardtoken123"])
        stream = [s for s in view["streams"] if s["name"] == "launchd.stderr.log"][0]
        shown = "\n".join(stream["lines"])
        for secret in ("sk-zzzQQQwwwRRR", KEY, LICENSE, "dashboardtoken123", "QUJDREVGR0g", "hunter2", "session=abc",
                       KEY[10:24], "\x1b"):
            self.assertNotIn(secret, shown)
        self.assertIn("coloured plain line", shown)
        self.assertIn("request failed: Authorization: <redacted>", shown)
        self.assertEqual(len(stream["lines"]), 7)   # the line with a piece of the key is dropped whole

    def test_tail_is_bounded_and_starts_on_a_whole_line(self):
        (self.logs / "research.stdout.log").write_text("".join("line %05d %s\n" % (n, "x" * 40) for n in range(5000)))
        state, text = failures_page.read_tail(self.logs / "research.stdout.log")
        self.assertEqual(state, "ok")
        self.assertLessEqual(len(text.encode()), failures_page.LOG_TAIL_BYTES)
        self.assertTrue(text.startswith("line "))
        lines = failures_page.tail(self.logs, "research.stdout.log", [])
        self.assertEqual(len(lines), failures_page.LOG_LINES)
        self.assertTrue(lines[-1].startswith("line 04999"))

    @unittest.skipIf(os.name == "nt", "POSIX modes, uids and symlinks")
    def test_missing_unreadable_symlinked_or_odd_files_never_raise(self):
        (self.logs / "research.stderr.log").write_text("secret stuff\n")
        os.chmod(self.logs / "research.stderr.log", 0)
        outside = Path(self.directory.name) / "outside.txt"
        outside.write_text("not a log\n")
        (self.logs / "applications.stderr.log").symlink_to(outside)
        (self.logs / "sidedoor.stdout.log").mkdir()
        (self.logs / "launchd.stdout.log").write_bytes(b"\xff\xfe bytes \x00 here\n")
        states = {s["name"]: s["state"] for s in failures_page.logs_view(self.logs, [])["streams"]}
        if os.geteuid() != 0:
            self.assertEqual(states["research.stderr.log"], "unreadable")
        self.assertEqual(states["applications.stderr.log"], "unreadable")
        self.assertEqual(states["sidedoor.stdout.log"], "unreadable")
        self.assertEqual(states["launchd.stderr.log"], "missing")
        self.assertEqual(states["launchd.stdout.log"], "ok")
        self.assertEqual(failures_page.logs_view(Path(self.directory.name) / "nowhere", [])["streams"][0]["state"], "missing")

    def test_filters_and_search(self):
        (self.logs / "research.stderr.log").write_text("alpha failed\nbeta ok\n")
        (self.logs / "research.stdout.log").write_text("alpha out\n")
        view = failures_page.logs_view(self.logs, [], component="research", level="stderr", query="ALPHA")
        self.assertEqual([(s["name"], s["lines"]) for s in view["streams"]], [("research.stderr.log", ["alpha failed"])])
        view = failures_page.logs_view(self.logs, [], component="<script>", level="nope")
        self.assertEqual((view["component"], view["level"], len(view["streams"])), ("all", "all", 8))

    def test_secrets_are_read_from_the_home_without_creating_anything(self):
        home = Path(self.directory.name) / "home"
        home.mkdir()
        (home / "license.key").write_text(LICENSE + "\n")
        found = failures_page.secrets_of(home, {}, "tok")
        self.assertEqual(sorted(found), sorted(["tok", LICENSE]))
        self.assertEqual(sorted(p.name for p in home.iterdir()), ["license.key"])


    def test_f5_unipile_and_heyreach_never_show(self):
        home = Path(self.directory.name) / "home"
        home.mkdir()
        (home / "unipile.json").write_text(json.dumps({"dsn": "api8.unipile.com:13443", "token": "UniTok3nValue+abc/123="}))
        credentials = Path(self.directory.name) / "codex.toml"
        credentials.write_text('[mcp_servers.heyreach]\nurl = "https://mcp.heyreach.io/mcp/9f8e7d6c5b4a39281706"\n')
        # The owner's local-dev install reads unipile.json (RL1); a relay install never does.
        secrets = failures_page.secrets_of(home, {"credential_config": str(credentials),
                                                  "linkedin_transport": "local-dev"}, "tok")
        (self.logs / "launchd.stderr.log").write_text(
            "unipile said X-API-KEY UniTok3nValue+abc/123= refused\n"     # 1. the Unipile token
            "connect to api8.unipile.com:13443 failed\n"                  # 2. the bare DSN host:port
            "HeyReach host mcp.heyreach.io timed out\n"                   # 3. the bare HeyReach host
            "ok line\n")
        shown = "\n".join(failures_page.tail(self.logs, "launchd.stderr.log", secrets))
        for secret in ("UniTok3nValue", "api8.unipile.com", "13443", "mcp.heyreach.io", "9f8e7d6c5b4a39281706"):
            self.assertNotIn(secret, shown)
        self.assertIn("ok line", shown)
        self.assertIn("HeyReach host <redacted> timed out", shown)

    def test_f10_an_escaped_json_secret_field_is_redacted(self):
        (self.logs / "launchd.stderr.log").write_text('payload: {\\"token\\": \\"s3cr3tvalue\\", \\"n\\": 1}\n'
                                                      "{\\'password\\': \\'hunter2pw\\'}\n")
        shown = "\n".join(failures_page.tail(self.logs, "launchd.stderr.log", []))
        self.assertNotIn("s3cr3tvalue", shown)
        self.assertNotIn("hunter2pw", shown)

    @unittest.skipIf(os.name == "nt", "no FIFOs (os.mkfifo) on Windows")
    def test_f14_a_fifo_never_hangs_the_page(self):
        os.mkfifo(self.logs / "research.stdout.log")
        states = {s["name"]: s["state"] for s in failures_page.logs_view(self.logs, [])["streams"]}
        self.assertEqual(states["research.stdout.log"], "unreadable")

@unittest.skipUnless(DEPENDENCIES, "dashboard packages (fastapi, httpx) are not installed")
class FailuresPageTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.home = self.root / "home"
        base.finished_home(self.home)

    def tearDown(self):
        self.directory.cleanup()

    def get(self, path="/failures", **params):
        app = dashboard.create_app(base.SECRET, base.PORT, home=self.home, root=self.root, config=CONFIG)
        client = TestClient(app, base_url=f"http://127.0.0.1:{base.PORT}", follow_redirects=False)
        client.get("/", params={"t": base.SECRET})
        with mock.patch.object(setup_steps, "chrome_path", return_value="/chrome"):
            return client.get(path, params=params)

    def test_example_4_fresh_install(self):
        response = self.get()
        self.assertEqual(response.status_code, 200)
        page = text_of(response.text)
        self.assertIn("Nothing has failed.", page)
        self.assertIn("Nothing needs you right now.", page)
        self.assertIn('id="scheduler"', response.text)
        self.assertIn('id="needs-you"', response.text)
        self.assertIn("No errors from: Hourly round, Research, Applications, Sidedoor.", page)   # one line, no empty boxes
        self.assertNotIn("no log yet", page)
        self.assertFalse((self.root / "state").exists())
        self.assertFalse((self.root / "logs").exists())

    def test_skipped_research_entries_show_in_the_research_section_and_are_not_failures(self):
        database = base.database_with_schema(self.root)
        now = dt.datetime.now(dt.timezone.utc)
        industries = [{"industry": "AI", "state": "COMPLETE", "dropped_entries": [
            {"stage": "discovery", "company": "Airia", "role": None, "reason": "it was listed twice"}]}]
        research_day(database, now.date().isoformat(), "COMPLETE", "a1", "COMPLETE", now - dt.timedelta(minutes=5),
                     result={"state": "COMPLETE", "industries": industries})
        database.commit()
        database.close()
        page = self.get()
        research = text_of(page.text.split('id="research"', 1)[1].split("</section>", 1)[0])
        self.assertIn("1 entry was skipped: Airia: it was listed twice.", research)
        self.assertIn("Nothing you need to do.", research)
        text = text_of(page.text)
        self.assertIn("Nothing has failed.", text)
        self.assertIn("Nothing needs you right now.", text)

    def test_example_5_overview_links_land_on_their_sections(self):
        loop = {"running": True, "last_error": None, "job_failures": {"applications": {"exit_code": 1, "at": "2026-10-22T17:30:00+00:00"}}}
        with mock.patch.object(overview_page, "loop_state", return_value=loop):
            overview = self.get("/")
            page = self.get()
        self.assertIn('href="/failures#scheduler"', overview.text)
        self.assertIn('id="scheduler"', page.text)
        self.assertIn("Applications stopped with an error at", text_of(page.text))
        database = base.database_with_schema(self.root)
        base.job(database, "own", "BLOCKED", "DESIGNATED_RESUME_MISMATCH")
        database.commit()
        database.close()
        # Overview's Needs you leads to Applications' "Blocked, needs you" (owner, 2026-10-24); the
        # same hold is still listed under this page's Needs you.
        self.assertIn('href="/applications?show=needs_you"', self.get("/").text)
        page = self.get()
        self.assertIn("Your résumé file changed since you set it.", text_of(page.text))
        needs = page.text[page.text.index('id="needs-you"'):page.text.index("</section>", page.text.index('id="needs-you"'))]
        self.assertIn("Upload your résumé again in Settings.", needs)

    def test_get_writes_nothing_not_even_side_files(self):
        database = base.database_with_schema(self.root)
        database.execute("INSERT INTO runs VALUES ('s',?,'FAILED',NULL,NULL)", (dt.datetime.now(dt.timezone.utc).isoformat(),))
        database.commit()
        database.close()
        path = self.root / "state" / "orchestrator.sqlite3"
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertIn("The hourly round stopped with an error.", text_of(response.text))
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["orchestrator.sqlite3"])

    def test_a_junk_database_and_an_old_schema_still_render(self):
        (self.root / "state").mkdir()
        (self.root / "state" / "orchestrator.sqlite3").write_bytes(os.urandom(4096))
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertIn("Sidedoor couldn't read its records.", text_of(response.text))
        os.remove(self.root / "state" / "orchestrator.sqlite3")
        old = sqlite3.connect(self.root / "state" / "orchestrator.sqlite3")
        old.execute("CREATE TABLE workflow_tasks (task_id TEXT)")
        old.execute("CREATE TABLE application_jobs (job_key TEXT)")
        old.commit()
        old.close()
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertIn("Some of these records are not available right now.", text_of(response.text))

    def test_example_3_logs_on_the_page_and_in_the_download_are_redacted(self):
        (self.home / "license.key").write_text(LICENSE + "\n")
        logs = self.root / "logs"
        logs.mkdir()
        (logs / "sidedoor.stderr.log").write_text("Authorization: Bearer sk-abc123456789def\nkey " + KEY + "\nlicense "
                                                   + LICENSE + "\nopen http://127.0.0.1:47823/?t=" + base.SECRET + "\nfine\n")
        page = self.get()
        download = self.get("/failures/logs.txt")
        self.assertEqual(download.status_code, 200)
        self.assertIn("attachment", download.headers["content-disposition"])
        for body in (page.text, download.text):
            for secret in ("sk-abc123456789def", KEY, LICENSE, base.SECRET):
                self.assertNotIn(secret, body)
            self.assertIn("fine", body)
        searched = self.get(component="sidedoor", level="stderr", q="fine")
        self.assertIn("1 line)", text_of(searched.text))

    def test_page_answers_at_phone_width(self):
        css = base.CSS.read_text()
        rules = css[css.index("/* Failures and logs (E6.T7) */"):]
        media = rules[rules.index("@media (max-width: 640px)"):]
        for needed in (".log-filters label", "flex: 1 1 100%", ".log-lines"):
            self.assertIn(needed, media)
        for needed in ("overflow-wrap: anywhere", "white-space: pre-wrap", "min-width: 0"):
            self.assertIn(needed, rules)


if __name__ == "__main__":
    unittest.main()
