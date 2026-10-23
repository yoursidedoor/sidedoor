"""Isolated manual rehearsal of application scheduling and the desktop mailbox."""

import datetime as dt
import argparse
import hashlib
import json
from pathlib import Path
import sys
import traceback
import types
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "tests"))

import application_browser
import applications
import application_connection
import desktop_adapter
import notifications
import orchestrator
import workflow_tasks
from zoneinfo import ZoneInfo
from test_applications_e2e import MockATS, MockTelegram, PIPELINE, role
from test_desktop_adapter import DesktopHandoffTests
import user_profile
import paths


NOW = "2026-09-17T16:00:00+00:00"
TABLES = (
    "opportunities", "drafts", "application_jobs", "application_attempts",
    "application_history", "application_events", "application_reviews",
    "application_blockers", "application_answers",
    "desktop_requests", "desktop_events", "notification_outbox",
    "user_decisions", "transport_inbox",
)


def write(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def forbidden(*args, **kwargs):
    raise AssertionError("Manual rehearsal forbids network, subprocesses and approval prompts")


class RecordedATS(MockATS):
    def __init__(self, root):
        super().__init__(root)
        self.trace = []
        self.draft_only = False
        self.read_blocker = None

    def call(self, operation, payload):
        entry = {"operation": operation, "at": self.now, "payload": payload,
                 "mode": "MOCK_ONLY"}
        existing_receipts = set(self.records())
        self.trace.append(entry)
        try:
            if self.read_blocker and operation in {"verify", "reconcile"}:
                self.calls.append((operation, payload))
                result = {"blocked": dict(self.read_blocker)}
            elif self.draft_only and operation == "submit":
                self.calls.append((operation, payload))
                result = {"status": "DRAFT", "acknowledged": True}
            else:
                result = super().call(operation, payload)
            if operation in {"verify", "reconcile"} and result.get("status") == "NOT_SUBMITTED":
                result.update(attempt_id=payload["attempt_id"], job_key=payload["job"]["job_key"],
                              employer=payload["job"]["employer"], requisition=payload["job"]["requisition"],
                              candidate_email=payload["job"]["candidate_email"], independent_read=True)
            entry["result"] = result
            return result
        except Exception as error:
            entry["original_failure"] = {"type": type(error).__name__, "message": str(error)}
            raise
        finally:
            if operation == "submit":
                receipt_path = self.root / (hashlib.sha256(payload["job"]["job_key"].encode()).hexdigest() + ".receipt")
                if receipt_path.exists() and receipt_path.name not in existing_receipts:
                    receipt = json.loads(receipt_path.read_text())
                    receipt.update(attempt_id=payload["attempt_id"], job_key=payload["job"]["job_key"])
                    write(receipt_path, receipt)
            entry["provider_records_after"] = self.records()
            write(self.root / "provider-trace.json", self.trace)

    def records(self):
        return {path.name: json.loads(path.read_text()) for path in sorted(self.root.glob("*.receipt"))}


class Rehearsal:
    def __init__(self, root):
        self.root = root
        for folder in ("state", "reports", "logs", "provider"):
            (root / folder).mkdir(parents=True, exist_ok=False)
        self.now = NOW
        self.config = {"application_adapter": "desktop", "applications_enabled": True,
                       "mode": "ISOLATED_MOCK_ONLY"}
        resume = b"SYNTHETIC REHEARSAL RESUME; NEVER UPLOAD"
        (root / "mock-resume.pdf").write_bytes(resume)
        self.policy = {
            "source": "synthetic manual rehearsal policy; no production config loaded",
            "minimum_base_salary_usd": 180000,
            "funding_stages": ["Seed", "Series A", "Series B", "Series C", "Series D"],
            "industries": ["AI"], "levels": ["Staff Software Engineer"],
            "max_attempts": 3, "max_reconcile_attempts": 3,
            "retry_delays_seconds": [900, 3600], "preapproved_applications": True,
            "resume": {"path": "mock-resume.pdf", "sha256": hashlib.sha256(resume).hexdigest()},
            "answers": {"email": {"semantic": "contact_email", "value": "candidate@example.invalid",
                                  "source": "verified synthetic fixture only"}},
        }
        write(root / "config.json", self.config)
        user_profile.use_policy(self.policy, root / "profile-home")
        self.database = orchestrator.connect(root)
        applications.initialize(self.database)
        desktop_adapter.initialize(self.database)
        self.provider = RecordedATS(root / "provider")
        self.actions = []
        self.checks = []
        self.snapshots = []
        self.notes = []
        self.hosted = False
        self.request_id = None

    def enable_hosted(self):
        self.hosted = True
        self.config["application_browser"] = {"enabled": True}
        write(self.root / "config.json", self.config)
        workflow_tasks.initialize(self.database)

    def consumer(self, root, database, config, request_id):
        if root != self.root or database is not self.database or config != self.config:
            raise AssertionError("Mock consumer received another environment")
        self.request_id = request_id
        try:
            serviced = DesktopHandoffTests.service(self)
            self.capture("hosted_mock_consumer_completed")
            return {"serviced": serviced, "request_id": request_id, "mock_only": True}
        finally:
            self.request_id = None

    def advance(self, seconds=60):
        self.now = desktop_adapter.later(self.now, seconds)
        self.provider.now = self.now

    def discover(self, company="Rehearsal AI", requisition="123", alias=False, draft=False):
        opportunity = dict(role(company, requisition),
                           url="https://job-boards.greenhouse.io/mock-only/jobs/" + requisition)
        if alias:
            opportunity["url"] += "?utm_source=restart"
        drafts = []
        if draft:
            drafts.append({"recipient": "Synthetic Recipient", "company": company,
                           "profile": "https://www.linkedin.com/in/mock-only-rehearsal",
                           "message": "Unsent fixture draft", "source_url": opportunity["url"]})
        with patch.object(applications, "stamp", lambda: self.now), patch.object(orchestrator, "utcnow", lambda: self.now):
            orchestrator.store_research(self.database, PIPELINE,
                                       {"summary": "MOCK discovery handoff", "blockers": [],
                                        "opportunities": [opportunity], "drafts": drafts})
        return applications.identity(opportunity)[2]

    def cycle(self):
        browser = types.ModuleType("application_browser")
        browser.consume = self.consumer
        with patch.object(applications, "stamp", lambda: self.now), patch.dict(sys.modules, {"application_browser": browser}):
            result = orchestrator.application_cycle(self.root, self.config, self.database)
        self.capture("application_cycle")
        return result

    def claim(self):
        return desktop_adapter.claim(self.database, self.root, "manual-mock-existing-consumer", self.now,
                                     request_id=self.request_id)

    def complete(self, request, result):
        return desktop_adapter.complete(self.database, dict(request, result=result), self.now)

    def service(self):
        if self.hosted:
            return False
        serviced = DesktopHandoffTests.service(self)
        if serviced:
            self.capture("existing_mock_consumer_completed")
        return serviced

    def drive(self, rounds=4):
        for _ in range(rounds):
            self.cycle()
            for _ in range(120):
                if not self.service():
                    break
            else:
                raise AssertionError("Mock consumer exceeded bounded drain")
            self.advance()
        return self.cycle()

    def job(self, key=None):
        if key:
            return dict(self.database.execute("SELECT * FROM application_jobs WHERE job_key=?", (key,)).fetchone())
        return dict(self.database.execute("SELECT * FROM application_jobs ORDER BY job_key LIMIT 1").fetchone())

    def rows(self, table):
        return [dict(row) for row in self.database.execute("SELECT * FROM " + table)]

    def check(self, name, condition, evidence=None):
        self.checks.append({"check": name, "passed": bool(condition), "evidence": evidence})

    def capture(self, stage):
        tables = list(TABLES) + (["worker_compute_reservations"] if self.hosted else [])
        self.snapshots.append({"stage": stage, "at": self.now,
                               "tables": {table: self.rows(table) for table in tables},
                               "provider_records": self.provider.records()})

    def restart(self):
        self.database.close()
        self.database = orchestrator.connect(self.root)
        applications.initialize(self.database)
        desktop_adapter.initialize(self.database)
        self.capture("database_reopened")

    def finish(self):
        self.capture("final")
        result = {"status": "PASS" if all(check["passed"] for check in self.checks) else "FAIL",
                  "checks": self.checks, "notes": self.notes, "actions": self.actions,
                  "provider_trace": self.provider.trace, "snapshots": self.snapshots,
                  "report": applications.report(self.database, self.now),
                  "host_health": desktop_adapter.host_health(self.database, self.now)}
        write(self.root / "evidence.json", result)
        self.database.close()
        return {key: value for key, value in result.items() if key not in {"snapshots", "provider_trace"}}


def eligible(rehearsal):
    rehearsal.discover(draft=True)
    rehearsal.cycle()
    rehearsal.check("discovery and draft alone are not submissions", not rehearsal.provider.records()
                    and len(rehearsal.rows("drafts")) == 1 and rehearsal.job()["receipt"] is None)
    rehearsal.service()
    rehearsal.advance()
    rehearsal.cycle()
    rehearsal.service()
    rehearsal.check("mock submit changes provider state but acknowledgment is not verified",
                    len(rehearsal.provider.records()) == 1 and rehearsal.job()["state"] == "SUBMITTING"
                    and not rehearsal.rows("application_history"))
    result = rehearsal.drive()
    saved = json.loads((rehearsal.root / "reports/applications.json").read_text())
    rehearsal.check("independent receipt reaches tracker and entry-point report",
                    result["verified_submissions"] == saved["verified_submissions"] == 1
                    and rehearsal.actions == ["inspect", "submit", "reconcile"]
                    and len(rehearsal.rows("application_history")) == 1)
    rehearsal.check("draft remains distinct", saved["drafts_are_not_submissions"]
                    and len(rehearsal.rows("drafts")) == 1)


def duplicates(rehearsal):
    for company, requisition, kind in (("Applied AI", "123", "APPLICATION"), ("Interview AI", "456", "INTERVIEW")):
        rehearsal.discover(company, requisition)
        with rehearsal.database:
            rehearsal.database.execute("INSERT INTO application_history VALUES (?,?,?,?)",
                                       (applications.normalized(company), "*" if kind == "INTERVIEW" else requisition,
                                        kind, "preserved synthetic historical evidence"))
    original = rehearsal.rows("application_history")
    rehearsal.drive()
    rehearsal.check("existing application and interview prevent dispatch", not rehearsal.actions
                    and all(job["state"] == "DUPLICATE" for job in rehearsal.rows("application_jobs")))
    rehearsal.check("original historical evidence preserved", original == rehearsal.rows("application_history"))


def unknowns(rehearsal):
    rehearsal.discover("Answer AI", "456")

    def customize(result, job):
        result["questions"].append({"id": "citizenship", "semantic": "citizenship", "required": True})

    rehearsal.provider.customize = customize
    rehearsal.drive()
    rehearsal.check("required citizenship never inferred", rehearsal.job()["blocker"] == "UNKNOWN_REQUIRED_ANSWERS:citizenship"
                    and not rehearsal.rows("application_answers") and "submit" not in rehearsal.actions)


def transient(rehearsal):
    rehearsal.discover()
    rehearsal.provider.inspect_failures = 9
    for attempt in range(3):
        rehearsal.cycle()
        rehearsal.service()
        rehearsal.advance()
        rehearsal.cycle()
        job = rehearsal.job()
        rehearsal.check("failure consumes bounded attempt " + str(attempt + 1), job["attempts"] == attempt + 1)
        if job["next_at"]:
            rehearsal.now = desktop_adapter.later(job["next_at"], -1)
            rehearsal.provider.now = rehearsal.now
            count = len(rehearsal.actions)
            rehearsal.cycle()
            rehearsal.check("backoff prevents early retry " + str(attempt + 1), len(rehearsal.actions) == count
                            and not rehearsal.service())
            rehearsal.advance(1)
    rehearsal.advance(86400)
    rehearsal.cycle()
    rehearsal.check("three failures exhaust budget", rehearsal.job()["state"] == "EXHAUSTED"
                    and rehearsal.actions == ["inspect"] * 3)
    rehearsal.check("original exceptions retained", sum("original_failure" in entry for entry in rehearsal.provider.trace) == 3)


def ambiguous(rehearsal):
    rehearsal.discover()
    rehearsal.provider.timeout_after_submit = True
    rehearsal.cycle()
    rehearsal.service()
    rehearsal.advance()
    rehearsal.cycle()
    rehearsal.service()
    rehearsal.check("lost response remains uncertain despite simulated provider acceptance",
                    rehearsal.job()["state"] != "SUBMITTED" and len(rehearsal.provider.records()) == 1)
    rehearsal.restart()
    rehearsal.drive()
    rehearsal.check("restart reconciles before any retry", rehearsal.job()["state"] == "SUBMITTED"
                    and rehearsal.actions == ["inspect", "submit", "reconcile"])
    rehearsal.check("original timeout survives successful recovery",
                    any(entry.get("original_failure", {}).get("type") == "TimeoutError" for entry in rehearsal.provider.trace))
    rehearsal.notes.append("desktop_adapter.complete normalizes submit errors to UNKNOWN and does not retain their raw detail; provider-trace.json preserves the original exception.")


def isolation(rehearsal):
    blocked = rehearsal.discover("Blocked AI", "123")
    ready = rehearsal.discover("Ready AI", "456")

    def customize(result, job):
        if job["job_key"] == blocked:
            result["blocked"] = {"code": "AUTHENTICATION_REQUIRED", "next_action": "Sign in to the existing synthetic account"}

    rehearsal.provider.customize = customize
    rehearsal.drive()
    rehearsal.check("blocked item does not prevent unrelated submission", rehearsal.job(blocked)["state"] == "BLOCKED"
                    and rehearsal.job(ready)["state"] == "SUBMITTED" and rehearsal.actions.count("submit") == 1)


def preapproved(rehearsal):
    rehearsal.discover()
    rehearsal.drive()
    submissions = [payload for operation, payload in rehearsal.provider.calls if operation == "submit"]
    saved = json.loads(rehearsal.rows("application_attempts")[0]["evidence"])
    rehearsal.check("preapproved submit completes while all input prompts are forbidden",
                    rehearsal.job()["state"] == "SUBMITTED" and len(submissions) == 1)
    rehearsal.check("exact verified answers and designated mock resume are persisted",
                    saved["answers"] == submissions[0]["answers"] == {"email": "candidate@example.invalid"}
                    and submissions[0]["no_extra_answers"] is True
                    and saved["resume_sha256"] == rehearsal.policy["resume"]["sha256"])


def blocker_notification(rehearsal):
    rehearsal.discover()
    rehearsal.cycle()
    request = rehearsal.claim()
    rehearsal.complete(request, {"blocked": {"code": "AUTHENTICATION_REQUIRED",
                                             "next_action": "Sign in to the existing synthetic employer account"}})
    rehearsal.advance()
    rehearsal.cycle()
    rehearsal.check("authentication blocker belongs to user", rehearsal.rows("application_blockers")[0]["owner"] == "user")
    sender = MockTelegram([notifications.Transient(600)])
    notifications.dispatch(rehearsal.database, {}, sender, rehearsal.now)
    rehearsal.capture("mock_notification_failed")
    failed = rehearsal.rows("notification_outbox")[0]
    rehearsal.check("failed notification remains pending with error and next retry", failed["state"] == "PENDING"
                    and failed["last_error"] and failed["next_at"] and not sender.messages)
    rehearsal.restart()
    notifications.dispatch(rehearsal.database, {}, sender, rehearsal.now)
    rehearsal.check("restart respects notification backoff", not sender.messages)
    rehearsal.advance(601)
    notifications.dispatch(rehearsal.database, {}, sender, rehearsal.now)
    rehearsal.capture("mock_notification_delivered")
    rehearsal.check("same retained notification delivers without resolving user blocker",
                    rehearsal.rows("notification_outbox")[0]["state"] == "DELIVERED"
                    and len(sender.messages) == 1 and rehearsal.job()["state"] == "BLOCKED")


def restart_dedup(rehearsal):
    rehearsal.discover()
    rehearsal.cycle()
    request_id = rehearsal.rows("desktop_requests")[0]["request_id"]
    rehearsal.restart()
    rehearsal.advance()
    rehearsal.cycle()
    rehearsal.check("restart retains the original pending mailbox request",
                    [row["request_id"] for row in rehearsal.rows("desktop_requests")] == [request_id])
    rehearsal.drive()
    rehearsal.restart()
    rehearsal.discover(alias=True)
    rehearsal.drive()
    rehearsal.check("rediscovery after restart creates no duplicate application",
                    rehearsal.actions.count("submit") == 1 and len(rehearsal.rows("application_jobs")) == 1
                    and len(rehearsal.rows("application_history")) == 1 and len(rehearsal.provider.records()) == 1)


def draft_only(rehearsal):
    rehearsal.discover(draft=True)
    rehearsal.provider.draft_only = True
    rehearsal.provider.unknown_reconcile = True
    rehearsal.drive()
    for _ in range(5):
        rehearsal.advance(901)
        rehearsal.drive(2)
    rehearsal.check("draft and acknowledgment never become a submission",
                    rehearsal.job()["state"] == "BLOCKED" and not rehearsal.provider.records()
                    and not rehearsal.rows("application_history")
                    and applications.report(rehearsal.database)["verified_submissions"] == 0)
    rehearsal.check("unknown outcome is bounded without repeat submit", rehearsal.actions.count("submit") == 1
                    and sum(operation in {"verify", "reconcile"} for operation in rehearsal.actions)
                    == rehearsal.policy["max_reconcile_attempts"])


def absence_retry(rehearsal):
    rehearsal.discover()
    rehearsal.provider.timeout_after_submit = True
    rehearsal.provider.lose_submission = True
    rehearsal.drive()
    rehearsal.check("independent confirmed absence precedes retry eligibility",
                    rehearsal.job()["state"] == "RETRY_WAIT"
                    and rehearsal.rows("application_attempts")[0]["state"] == "CONFIRMED_NOT_SUBMITTED"
                    and rehearsal.actions == ["inspect", "submit", "reconcile"])
    due = rehearsal.job()["next_at"]
    if due:
        rehearsal.now = desktop_adapter.later(due, -1)
        rehearsal.provider.now = rehearsal.now
        rehearsal.cycle()
        rehearsal.check("confirmed absence still respects backoff", not rehearsal.service()
                        and rehearsal.actions.count("submit") == 1)
        rehearsal.advance(1)
        rehearsal.provider.timeout_after_submit = False
        rehearsal.provider.lose_submission = False
        rehearsal.drive()
        states = [row["state"] for row in rehearsal.rows("application_attempts")]
        rehearsal.check("retry uses new attempt while retaining original absence evidence",
                        rehearsal.job()["state"] == "SUBMITTED" and rehearsal.actions.count("submit") == 2
                        and states == ["CONFIRMED_NOT_SUBMITTED", "VERIFIED"]
                        and len(rehearsal.provider.records()) == 1, states)


def hosted_eligible(rehearsal):
    rehearsal.discover(draft=True)
    result = rehearsal.cycle()
    rehearsal.check("real hosted wrapper completes inspect, submit, independent verify",
                    rehearsal.actions == ["inspect", "submit", "verify"]
                    and result["verified_submissions"] == 1 and len(rehearsal.provider.records()) == 1)
    after_submit = [snapshot for snapshot in rehearsal.snapshots
                    if snapshot["stage"] == "hosted_mock_consumer_completed"
                    and len(snapshot["provider_records"]) == 1
                    and not snapshot["tables"]["application_history"]]
    rehearsal.check("actual simulated submit is distinct from acknowledgment and draft",
                    bool(after_submit) and len(rehearsal.rows("drafts")) == 1)
    reservations = rehearsal.rows("worker_compute_reservations")
    rehearsal.check("three operations reserve exactly three session ceilings",
                    [row["seconds"] for row in reservations]
                    == [application_browser.MAX_SECONDS, 2 * application_browser.MAX_SECONDS], reservations)
    saved = json.loads((rehearsal.root / "reports/applications.json").read_text())
    rehearsal.check("actual entry point persists verified report", saved["verified_submissions"] == 1
                    and saved["drafts_are_not_submissions"])


def hosted_transient(rehearsal):
    rehearsal.discover()
    rehearsal.provider.inspect_failures = 9
    for attempt in range(3):
        rehearsal.cycle()
        job = rehearsal.job()
        rehearsal.check("hosted failed read consumes attempt " + str(attempt + 1), job["attempts"] == attempt + 1)
        if job["next_at"]:
            rehearsal.now = desktop_adapter.later(job["next_at"], -1)
            rehearsal.provider.now = rehearsal.now
            count = len(rehearsal.actions)
            rehearsal.cycle()
            rehearsal.check("hosted backoff " + str(attempt + 1), len(rehearsal.actions) == count)
            rehearsal.advance(1)
    rehearsal.advance(86400)
    rehearsal.cycle()
    rehearsal.check("hosted failures exhaust after three reads", rehearsal.job()["state"] == "EXHAUSTED"
                    and rehearsal.actions == ["inspect"] * 3)
    rehearsal.check("hosted original failures retained", sum("original_failure" in entry for entry in rehearsal.provider.trace) == 3)


def hosted_ambiguous(rehearsal):
    rehearsal.discover()
    rehearsal.provider.timeout_after_submit = True
    rehearsal.provider.unknown_reconcile = True
    rehearsal.cycle()
    rehearsal.check("uncertain submit remains unverified", rehearsal.job()["state"] == "RECONCILE"
                    and len(rehearsal.provider.records()) == 1 and not rehearsal.rows("application_history"))
    rehearsal.restart()
    rehearsal.provider.unknown_reconcile = False
    rehearsal.advance()
    rehearsal.cycle()
    if rehearsal.job()["state"] == "RECONCILE" and rehearsal.job()["next_at"]:
        rehearsal.now = rehearsal.job()["next_at"]
        rehearsal.provider.now = rehearsal.now
        rehearsal.cycle()
    rehearsal.check("real wrapper reconciles after restart without resubmission",
                    rehearsal.job()["state"] == "SUBMITTED"
                    and rehearsal.actions == ["inspect", "submit", "verify", "reconcile"])
    rehearsal.check("timeout preserved after recovery", any("original_failure" in entry for entry in rehearsal.provider.trace))


def hosted_blocker(rehearsal, blocker_code="AUTHENTICATION_REQUIRED"):
    rehearsal.discover()
    next_action = "Sign in to the existing synthetic account"

    def customize(result, job):
        result["blocked"] = {"code": blocker_code, "next_action": next_action}

    rehearsal.provider.customize = customize
    rehearsal.cycle()
    rehearsal.check("real wrapper reports genuine user blocker", rehearsal.job()["state"] == "BLOCKED"
                    and rehearsal.rows("application_blockers")[0]["owner"] == "user"
                    and rehearsal.job()["blocker"] == blocker_code and rehearsal.job()["next_action"] == next_action
                    and rehearsal.actions == ["inspect"])
    sender = MockTelegram([notifications.Transient(600)])
    notifications.dispatch(rehearsal.database, {}, sender, rehearsal.now)
    rehearsal.capture("hosted_notification_failure")
    outbox = rehearsal.rows("notification_outbox")
    rehearsal.check("failed transport retains pending user notification", outbox[0]["state"] == "PENDING"
                    and outbox[0]["last_error"] and not sender.messages)
    rehearsal.restart()
    rehearsal.advance(601)
    notifications.dispatch(rehearsal.database, {}, sender, rehearsal.now)
    rehearsal.capture("hosted_notification_recovered")
    outbox = rehearsal.rows("notification_outbox")
    rehearsal.check("pending notification recovers once after restart", len(sender.messages) == 1
                    and outbox[0]["state"] == "DELIVERED"
                    and blocker_code in sender.messages[0] and next_action in sender.messages[0]
                    and rehearsal.job()["state"] == "BLOCKED" and not rehearsal.rows("application_attempts"))


def hosted_restart(rehearsal):
    rehearsal.discover()
    rehearsal.cycle()
    rehearsal.restart()
    rehearsal.discover(alias=True)
    rehearsal.drive()
    rehearsal.check("hosted restart and rediscovery cannot repeat submit", rehearsal.actions.count("submit") == 1
                    and len(rehearsal.rows("application_jobs")) == 1 and len(rehearsal.rows("application_history")) == 1)


def hosted_capacity(rehearsal):
    rehearsal.config["application_browser"]["max_operations"] = 3
    write(rehearsal.root / "config.json", rehearsal.config)
    first = rehearsal.discover("First AI", "123")
    second = rehearsal.discover("Second AI", "456")
    rehearsal.cycle()
    rehearsal.check("cycle limit stops before second application attempt", rehearsal.job(first)["state"] == "SUBMITTED"
                    and rehearsal.job(second)["attempts"] == 0 and rehearsal.job(second)["state"] == "WAITING_BROWSER"
                    and rehearsal.job(second)["blocker"] == "APPLICATION_BROWSER_CYCLE_CAPACITY")
    rehearsal.check("cycle capacity is not a user approval", not any(row["owner"] == "user" for row in rehearsal.rows("application_blockers")))
    rehearsal.advance()
    rehearsal.cycle()
    rehearsal.check("next cycle resumes same job", rehearsal.job(second)["state"] == "SUBMITTED"
                    and rehearsal.actions.count("submit") == 2)


def hosted_shared_budget(rehearsal):
    rehearsal.discover()
    with rehearsal.database:
        # Leave exactly one browser session's booking: the read fits, the two-operation submit does not.
        workflow_tasks.reserve_compute(rehearsal.database, "synthetic-other-worker", rehearsal.now,
                                       10800 - application_browser.MAX_SECONDS)
    rehearsal.cycle()
    rehearsal.check("reserve two operations before durable submission attempt",
                    rehearsal.actions == ["inspect"] and not rehearsal.rows("application_attempts")
                    and rehearsal.job()["attempts"] == 0
                    and rehearsal.job()["blocker"] == "APPLICATION_BROWSER_SHARED_BUDGET_EXHAUSTED")
    before = rehearsal.rows("worker_compute_reservations")
    rehearsal.restart()
    rehearsal.advance(60)
    rehearsal.cycle()
    rehearsal.check("shared budget wait retains reservation and creates no user question",
                    rehearsal.rows("worker_compute_reservations") == before
                    and not rehearsal.rows("notification_outbox") and rehearsal.job()["next_at"] == "2026-09-18T16:00:00+00:00")
    # The shared budget rolls over at the 9:00 Pacific service-day start (2026-09-30; was midnight).
    rehearsal.now = "2026-09-18T16:00:00+00:00"
    rehearsal.provider.now = rehearsal.now
    rehearsal.cycle()
    rehearsal.check("new budget day resumes after fresh inspection", rehearsal.job()["state"] == "SUBMITTED"
                    and rehearsal.actions.count("submit") == 1)


def hosted_injected_class(rehearsal):
    rehearsal.discover()
    browser = types.ModuleType("application_browser")
    browser.consume = forbidden
    with patch.dict(sys.modules, {"application_browser": browser}):
        adapter = application_connection.HostedDesktopAdapter(rehearsal.database, rehearsal.root, rehearsal.config,
                                                             clock=lambda: rehearsal.now, consumer=rehearsal.consumer)
        result = applications.cycle(rehearsal.database, rehearsal.config, rehearsal.root, adapter=adapter, now=rehearsal.now)
    rehearsal.check("real class honors explicitly injected consumer", type(adapter) is application_connection.HostedDesktopAdapter
                    and adapter.allocated == 3 and result["verified_submissions"] == 1)
    rehearsal.capture("explicit_injected_consumer")


def hosted_cached(rehearsal):
    rehearsal.discover()
    browser = types.ModuleType("application_browser")
    browser.consume = forbidden
    with patch.dict(sys.modules, {"application_browser": browser}):
        adapter = application_connection.HostedDesktopAdapter(rehearsal.database, rehearsal.root, rehearsal.config,
                                                             clock=lambda: rehearsal.now, consumer=rehearsal.consumer)
        job = dict(rehearsal.job(), candidate_email="candidate@example.invalid")
        first = adapter.call("inspect", {"job": job, "read_only": True})
        second = adapter.call("inspect", {"job": job, "read_only": True})
    rehearsal.check("cached inspect consumes no additional browser allocation", first == second
                    and adapter.allocated == 1 and rehearsal.actions == ["inspect"]
                    and sum(row["seconds"] for row in rehearsal.rows("worker_compute_reservations")) == application_browser.MAX_SECONDS)


def hosted_three_operation_fairness(rehearsal):
    # Two eligible jobs, a three-operation cycle limit: the first job's inspect/submit/verify
    # spends the cycle, the second waits before any claim and progresses on the next cycle.
    rehearsal.config["application_browser"]["max_operations"] = 3
    write(rehearsal.root / "config.json", rehearsal.config)
    keys = (rehearsal.discover("First AI", "123"), rehearsal.discover("Second AI", "456"))
    rehearsal.cycle()
    states = {key: rehearsal.job(key) for key in keys}
    waiting = [job for job in states.values() if job["state"] == "WAITING_BROWSER"]
    rehearsal.check("three-operation limit defers eligible work before claiming",
                    len(waiting) == 1 and waiting[0]["attempts"] == 0
                    and waiting[0]["blocker"] == "APPLICATION_BROWSER_CYCLE_CAPACITY"
                    and len(rehearsal.rows("application_attempts")) == 1
                    and rehearsal.actions == ["inspect", "submit", "verify"],
                    {"actions": rehearsal.actions, "states": {key: job["state"] for key, job in states.items()}})
    rehearsal.advance()
    rehearsal.cycle()
    rehearsal.check("deferred eligible work progresses on next cycle",
                    all(rehearsal.job(key)["state"] == "SUBMITTED" for key in keys)
                    and rehearsal.actions == ["inspect", "submit", "verify"] * 2)
    rehearsal.check("only the six performed browser operations consume shared compute",
                    sum(row["seconds"] for row in rehearsal.rows("worker_compute_reservations"))
                    == 6 * application_browser.MAX_SECONDS)


def hosted_spent_budget_restart(rehearsal):
    rehearsal.discover()
    rehearsal.provider.inspect_failures = 1
    rehearsal.cycle()
    first_failure = rehearsal.rows("desktop_requests")[0]
    rehearsal.check("original failed read is durably retained", first_failure["state"] == "CONSUMED"
                    and json.loads(first_failure["result"])["error"]["code"] == "MOCK_TRANSIENT"
                    and rehearsal.job()["attempts"] == 1)
    # The failed read's unused tail is refunded once it settles (owner, 2026-09-29), so settle it
    # first and then book exactly the rest of the default 10800s day: whatever the refund left,
    # the budget is fully spent and this rehearsal cannot silently become a no-op.
    applications.settle_all_browser_reservations(rehearsal.database, rehearsal.now)
    with rehearsal.database:
        day = workflow_tasks.budget_day(rehearsal.now, "America/Los_Angeles")
        workflow_tasks.reserve_compute(rehearsal.database, "synthetic-rest-of-budget", rehearsal.now,
                                       10800 - workflow_tasks.compute_used(rehearsal.database, day))
    rehearsal.advance(900)
    rehearsal.cycle()
    before = rehearsal.rows("worker_compute_reservations")
    rehearsal.check("fully spent budget waits without a new request or attempt",
                    rehearsal.job()["blocker"] == "APPLICATION_BROWSER_SHARED_BUDGET_EXHAUSTED"
                    and rehearsal.job()["attempts"] == 1 and rehearsal.actions == ["inspect"]
                    and len(rehearsal.rows("desktop_requests")) == 1 and not rehearsal.rows("application_attempts"))
    rehearsal.restart()
    rehearsal.advance(60)
    rehearsal.cycle()
    rehearsal.check("restart preserves spent budget and original failure", rehearsal.rows("worker_compute_reservations") == before
                    and rehearsal.rows("desktop_requests")[0] == first_failure
                    and rehearsal.job()["attempts"] == 1 and rehearsal.actions == ["inspect"]
                    and not rehearsal.rows("notification_outbox"))


def hosted_cache_exhausted_budget(rehearsal):
    rehearsal.discover()
    browser = types.ModuleType("application_browser")
    browser.consume = forbidden
    with patch.dict(sys.modules, {"application_browser": browser}):
        adapter = application_connection.HostedDesktopAdapter(rehearsal.database, rehearsal.root, rehearsal.config,
                                                             clock=lambda: rehearsal.now, consumer=rehearsal.consumer)
        job = dict(rehearsal.job(), candidate_email="candidate@example.invalid")
        first = adapter.call("inspect", {"job": job, "read_only": True})
        with rehearsal.database:
            workflow_tasks.reserve_compute(rehearsal.database, "synthetic-other-workers-exhausted",
                                           rehearsal.now, 10800 - application_browser.MAX_SECONDS)
        before = rehearsal.rows("worker_compute_reservations")
        rehearsal.restart()
        restarted = application_connection.HostedDesktopAdapter(rehearsal.database, rehearsal.root, rehearsal.config,
                                                               clock=lambda: rehearsal.now, consumer=forbidden)
        cached = restarted.call("inspect", {"job": job, "read_only": True})
    rehearsal.check("completed inspect available after restart with fully spent budget",
                    cached == first and restarted.allocated == 0 and rehearsal.rows("worker_compute_reservations") == before
                    and rehearsal.actions == ["inspect"] and sum(row["seconds"] for row in before) == 10800)


HOSTED_CASES = (
    ("13-hosted-discovery-worker-tracker", hosted_eligible),
    ("14-hosted-existing-application-interview", duplicates),
    ("16-hosted-unknown-facts", unknowns),
    ("17-hosted-bounded-transient-retry", hosted_transient),
    ("18-hosted-ambiguous-reconcile", hosted_ambiguous),
    ("19-hosted-blocked-isolation", isolation),
    ("20-hosted-preapproved", preapproved),
    ("21-hosted-blocker-notification", hosted_blocker),
    ("22-hosted-restart-dedup", hosted_restart),
    ("23-hosted-cycle-capacity", hosted_capacity),
    ("24-hosted-shared-budget", hosted_shared_budget),
    ("25-hosted-injected-real-class", hosted_injected_class),
    ("26-hosted-draft-is-not-submission", draft_only),
    ("27-hosted-cached-no-allocation", hosted_cached),
    ("28-hosted-three-operation-fairness", hosted_three_operation_fairness),
    ("29-hosted-spent-budget-restart", hosted_spent_budget_restart),
    ("30-hosted-cache-under-exhausted-budget", hosted_cache_exhausted_budget),
)


REVIEW_CASES = ()


CASES = (
    ("01-discovery-worker-tracker", eligible),
    ("02-existing-application-interview", duplicates),
    ("04-unknown-facts", unknowns),
    ("05-bounded-transient-retry", transient),
    ("06-ambiguous-reconcile", ambiguous),
    ("07-blocked-isolation", isolation),
    ("08-preapproved", preapproved),
    ("09-blocker-notification", blocker_notification),
    ("10-restart-dedup", restart_dedup),
    ("11-draft-is-not-submission", draft_only),
    ("12-absence-before-retry", absence_retry),
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-only", action="store_true")
    args = parser.parse_args()
    started = dt.datetime.now(dt.timezone.utc)
    output = paths.reports() / "application-connection-manual" / started.strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True, exist_ok=False)
    sources = ["manual_application_connection.py", "applications.py", "application_browser.py", "application_connection.py", "desktop_adapter.py", "orchestrator.py",
               "notifications.py", "tests/test_applications_e2e.py", "tests/test_desktop_adapter.py"]
    source_hashes = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in sources}
    (output / "sources").mkdir()
    for name in sources:
        (output / "sources" / name.replace("/", "__")).write_bytes((ROOT / name).read_bytes())
    results = {}
    with patch("socket.create_connection", forbidden), patch("socket.socket.connect", forbidden), \
            patch("socket.socket.connect_ex", forbidden), patch("socket.socket.sendto", forbidden), \
            patch("socket.getaddrinfo", forbidden), patch("subprocess.Popen", forbidden), patch("builtins.input", forbidden):
        for name, scenario in (REVIEW_CASES if args.review_only else CASES + HOSTED_CASES + REVIEW_CASES):
            rehearsal = Rehearsal(output / name)
            try:
                if "hosted" in name:
                    rehearsal.enable_hosted()
                scenario(rehearsal)
            except Exception as error:
                rehearsal.check("scenario completed", False, {"type": type(error).__name__,
                                "message": str(error), "traceback": traceback.format_exc()})
            finally:
                results[name] = rehearsal.finish()
            print(name + ": " + results[name]["status"], flush=True)
    source_hashes_after = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in sources}
    summary = {"mode": "MOCK_ONLY", "started_at": started.isoformat(), "output": str(output),
               "status": "PASS" if all(result["status"] == "PASS" for result in results.values()) else "FAIL",
               "entry_point": "orchestrator.application_cycle", "consumer": "DesktopHandoffTests.service reused directly",
               "hosted_wrapper": "Real HostedDesktopAdapter with injected mock consumer; temporary in-memory application_browser module",
               "live_verified": False, "scheduled_host_verified": False, "production_config_or_db_loaded": False,
               "source_sha256": source_hashes, "cases": results,
               "source_sha256_after": source_hashes_after, "sources_changed_during_run": source_hashes != source_hashes_after,
               "scope": "Synthetic discovery handoff through actual application/mailbox code; no real browser, send, discovery model, or launchd execution"}
    write(output / "summary.json", summary)
    print(json.dumps({"status": summary["status"], "summary": str(output / "summary.json")}))
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
