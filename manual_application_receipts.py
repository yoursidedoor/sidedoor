import hashlib
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import tempfile
from unittest.mock import patch

import applications
import desktop_adapter
import user_profile


NOW = "2026-09-17T16:00:00+00:00"


class Fixture:
    def __init__(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.now = NOW
        self.database = sqlite3.connect(self.root / "receipt.sqlite3")
        self.database.row_factory = sqlite3.Row
        applications.initialize(self.database)
        resume = b"synthetic receipt validation resume"
        (self.root / "resume.pdf").write_bytes(resume)
        self.policy = {
            "preapproved_applications": True, "max_attempts": 3,
            "max_reconcile_attempts": 3, "retry_delays_seconds": [900],
            "minimum_base_salary_usd": 190000, "funding_stages": ["Series B"],
            "industries": ["AI"], "levels": ["Staff Software Engineer"],
            "resume": {"path": "resume.pdf", "sha256": hashlib.sha256(resume).hexdigest()},
            "answers": {"email": {"semantic": "contact_email", "value": "candidate@example.invalid",
                                    "source": "synthetic fixture"}},
        }
        self.config = {"application_adapter": "desktop", "applications_enabled": True,
                       "execution_ownership_verified": True}
        user_profile.use_policy(self.policy, self.root / "profile-home")
        (self.root / "config.json").write_text(json.dumps(self.config))
        self.adapter = desktop_adapter.DesktopAdapter(self.database, self.root, self.config, lambda: self.now)
        with self.database:
            self.key = applications.ingest(self.database, {"company": "Synthetic", "role": "Staff Software Engineer",
                "url": "https://jobs.ashbyhq.com/synthetic/role-123", "requisition_id": "role-123"}, self.now)
        evidence = {"observed_at": self.now, "verified": True, "source": "synthetic official record"}
        self.review = dict(evidence, employer="synthetic", requisition="role-123",
            official_url=json.loads(self.job()["payload"])["url"], role=json.loads(self.job()["payload"])["role"],
            candidate_email=self.job()["candidate_email"],
            official_destination_verified=True, action_scope="application_only",
            employee_count_evidence=dict(evidence, value="51-200"),
            facts={"open": True, "funding_stage": "Series B", "industry": "AI",
                   "level": "Staff Software Engineer", "specialization": "backend", "country": "US",
                   "city": "Seattle", "work_mode": "remote", "requirements_met": True,
                   "authorization": "US_PERMANENT_RESIDENT_ACCEPTED", "hiring_geography_includes_candidate": True,
                   "citizenship_clearance_requirements_met": True},
            scope_fit={"level": {"in_scope": True, "reason": "fixture: a target level"}, "specialization": {"in_scope": True, "reason": "fixture: a target specialization"}},
            compensation=dict(evidence, currency="USD", minimum=200000, maximum=250000, period="annual_base"),
            history=dict(evidence, job_key=self.key, application=False, interview=False, referral="NONE"),
            questions=[{"id": "email", "semantic": "contact_email", "required": True}],
            all_required_questions_enumerated=True, form_revision="synthetic-v1")

    def close(self):
        self.database.close()
        self.temporary.cleanup()

    def job(self):
        result = dict(self.database.execute("SELECT * FROM application_jobs WHERE job_key=?", (self.key,)).fetchone())
        result["candidate_email"] = "candidate@example.invalid"
        return result

    def queue(self, operation, payload):
        try:
            self.adapter.call(operation, payload)
        except applications.AdapterPending as pending:
            return pending.request_id
        raise AssertionError("Expected queued desktop request")

    def claim(self, request_id):
        result = desktop_adapter.claim(self.database, self.root, "synthetic-receipt-check", self.now,
                                       request_id=request_id)
        assert result is not None
        return result

    def prepare(self, armed=True):
        request = self.claim(self.queue("inspect", {"job": self.job()}))
        desktop_adapter.complete(self.database, dict(request, result=self.review), self.now)
        applications.process(self.database, self.job(), self.adapter, self.policy, self.root, self.now)
        submit = self.database.execute("SELECT request_id FROM desktop_requests WHERE operation='submit'").fetchone()
        self.submit = self.claim(submit[0])
        if armed:
            desktop_adapter.arm(self.database, self.root, dict(self.submit, observed_form_revision="synthetic-v1"), self.now)
            # Unacknowledged, so these checks still exercise verify/reconcile: an acknowledged armed
            # submit now settles at once as a page confirmation (owner, 2026-10-06).
            desktop_adapter.complete(self.database, dict(self.submit, result={"acknowledged": False}), self.now)
        return self.submit

    def receipt(self, status="SUBMITTED"):
        result = {"status": status, "source": "synthetic authenticated ATS record", "verified": True,
                  "observed_at": self.now, "independent_read": True, "employer": "synthetic",
                  "requisition": "role-123", "candidate_email": "candidate@example.invalid",
                  "job_key": self.key, "attempt_id": self.submit["payload"]["attempt_id"],
                  "evidence_kind": "authenticated_ats_application_record",
                  "provider_message_or_application_id": "synthetic-provider-record"}
        if status == "SUBMITTED":
            result["receipt_id"] = "synthetic-receipt"
        else:
            result.update(authoritative_absence=True, no_inflight=True)
        return result


def check_recovery():
    fixture = Fixture()
    try:
        fixture.prepare()
        request = fixture.claim(fixture.queue("verify", {"job": fixture.job()}))
        desktop_adapter.complete(fixture.database, dict(request, result=fixture.receipt()), fixture.now)
        fixture.now = desktop_adapter.later(fixture.now, 172800)
        fixture.database.close()
        fixture.database = sqlite3.connect(fixture.root / "receipt.sqlite3")
        fixture.database.row_factory = sqlite3.Row
        fixture.adapter = desktop_adapter.DesktopAdapter(fixture.database, fixture.root, fixture.config, lambda: fixture.now)
        before = fixture.database.total_changes
        recovered = fixture.adapter.cached("reconcile", {"job": fixture.job()})
        assert recovered["status"] == "SUBMITTED"
        assert fixture.database.total_changes == before
        assert fixture.database.execute("SELECT state FROM desktop_requests WHERE request_id=?", (request["request_id"],)).fetchone()[0] == "DONE"
        fixture.database.execute("BEGIN IMMEDIATE")
        applications.finish_receipt(fixture.database, fixture.job(), recovered, fixture.now)
        fixture.database.rollback()
        assert fixture.job()["state"] != "SUBMITTED"
        assert fixture.database.execute("SELECT state FROM desktop_requests WHERE request_id=?", (request["request_id"],)).fetchone()[0] == "DONE"
        with fixture.database:
            fixture.database.execute("UPDATE application_jobs SET reconcile_attempts=3")
        with patch.object(fixture.adapter, "call", side_effect=AssertionError("Recovery must not launch browser work")):
            applications.reconcile(fixture.database, fixture.job(), fixture.adapter, fixture.policy, fixture.now)
        assert fixture.job()["state"] == "SUBMITTED"
        assert fixture.database.execute("SELECT state FROM desktop_requests WHERE request_id=?", (request["request_id"],)).fetchone()[0] == "CONSUMED"
        assert fixture.database.execute("SELECT count(*) FROM application_attempts").fetchone()[0] == 1
    finally:
        fixture.close()


def check_absence():
    fixture = Fixture()
    try:
        fixture.prepare()
        request = fixture.claim(fixture.queue("reconcile", {"job": fixture.job()}))
        valid = fixture.receipt("NOT_SUBMITTED")
        for field, value in {"candidate_email": "other@example.invalid", "attempt_id": "other-attempt",
                             "job_key": "other-job", "independent_read": False,
                             "observed_at": desktop_adapter.later(fixture.now, -1)}.items():
            # provider_message_or_application_id stopped being a requirement (owner, 2026-09-21):
            # no code reads it to decide anything, so its absence no longer invalidates a read.
            invalid = dict(valid, **{field: value})
            try:
                desktop_adapter.complete(fixture.database, dict(request, result=invalid), fixture.now)
            except ValueError:
                pass
            else:
                raise AssertionError("Accepted invalid absence: " + field)
        desktop_adapter.complete(fixture.database, dict(request, result=valid), fixture.now)
        applications.reconcile(fixture.database, fixture.job(), fixture.adapter, fixture.policy, fixture.now)
        assert fixture.job()["state"] == "RETRY_WAIT"
        assert fixture.database.execute("SELECT state FROM application_attempts").fetchone()[0] == "CONFIRMED_NOT_SUBMITTED"
    finally:
        fixture.close()


def check_blocker():
    fixture = Fixture()
    try:
        request = fixture.prepare(armed=False)
        blocker = {"blocked": {"code": "CAPTCHA_REQUIRED", "next_action": "Resolve synthetic CAPTCHA"}}
        desktop_adapter.block(fixture.database, dict(request, result=blocker), fixture.now)
        stored = fixture.database.execute("SELECT state,armed_at,result FROM desktop_requests WHERE request_id=?", (request["request_id"],)).fetchone()
        assert stored["state"] == "DONE" and stored["armed_at"] is None
        assert json.loads(stored["result"])["blocked"] == blocker["blocked"]
        assert fixture.database.execute("SELECT state FROM application_attempts").fetchone()[0] == "SUBMITTING"
        try:
            desktop_adapter.arm(fixture.database, fixture.root, dict(request, observed_form_revision="synthetic-v1"), fixture.now)
        except ValueError:
            pass
        else:
            raise AssertionError("Blocked claim could still arm")
        with patch.object(fixture.adapter, "call", side_effect=AssertionError("Recover blocker before browser work")):
            applications.reconcile(fixture.database, fixture.job(), fixture.adapter, fixture.policy, fixture.now)
        assert fixture.job()["blocker"] == "CAPTCHA_REQUIRED"
        assert fixture.database.execute("SELECT owner FROM application_blockers").fetchone()[0] == "user"
        assert fixture.database.execute("SELECT count(*) FROM notification_outbox").fetchone()[0] == 1
        assert fixture.database.execute("SELECT state FROM application_attempts").fetchone()[0] == "SUBMITTING"
    finally:
        fixture.close()


def check_submission_blocker_handoff():
    for code in ("CAPTCHA_REQUIRED", "MFA_REQUIRED", "UNKNOWN_REQUIRED_ANSWERS:legal"):
        fixture = Fixture()
        try:
            class Consumer:
                can_submit = True

                def call(self, operation, payload):
                    if operation == "inspect":
                        return fixture.review
                    assert operation == "submit", "Blocked submission must not call verify"
                    request = fixture.claim(fixture.queue(operation, payload))
                    desktop_adapter.block(fixture.database, dict(request, result={
                        "blocked": {"code": code, "next_action": "Resolve the synthetic access or answer blocker"}}), fixture.now)
                    return fixture.adapter.call(operation, payload)

            applications.process(fixture.database, fixture.job(), Consumer(), fixture.policy, fixture.root, fixture.now)
            assert fixture.job()["blocker"] == code
            assert fixture.job()["state"] == "RECONCILE"
            assert fixture.database.execute("SELECT owner FROM application_blockers").fetchone()[0] == "user"
            assert fixture.database.execute("SELECT count(*) FROM notification_outbox").fetchone()[0] == 1
            assert fixture.database.execute("SELECT state FROM application_attempts").fetchone()[0] == "SUBMITTING"
        finally:
            fixture.close()


def check_blocked_reconciliation():
    for code in ("AUTHENTICATION_REQUIRED", "CAPTCHA_REQUIRED", "MFA_REQUIRED"):
        fixture = Fixture()
        try:
            fixture.prepare()
            for repetition in range(2):
                request = fixture.claim(fixture.queue("reconcile", {"job": fixture.job()}))
                blocker = {"blocked": {"code": code, "next_action": "Resolve synthetic account access"}}
                desktop_adapter.complete(fixture.database, dict(request, result=blocker), fixture.now)
                if repetition == 0:
                    try:
                        with patch.object(applications, "hold", side_effect=RuntimeError("synthetic disposition failure")):
                            applications.reconcile(fixture.database, fixture.job(), fixture.adapter, fixture.policy, fixture.now)
                    except RuntimeError:
                        pass
                    else:
                        raise AssertionError("Expected synthetic hold failure")
                    assert fixture.database.execute("SELECT state FROM desktop_requests WHERE request_id=?", (request["request_id"],)).fetchone()[0] == "DONE"
                    assert fixture.job()["reconcile_attempts"] == 0
                applications.reconcile(fixture.database, fixture.job(), fixture.adapter, fixture.policy, fixture.now)
                assert fixture.job()["blocker"] == code
                # An access-blocked read counts toward the 10-read limit (owner, 2026-09-30).
                assert fixture.job()["reconcile_attempts"] == repetition + 1
                assert fixture.job()["attempts"] == 1
                assert fixture.job()["state"] == "RECONCILE"
                assert fixture.job()["next_at"] == desktop_adapter.later(fixture.now, 900)
                assert fixture.database.execute("SELECT owner FROM application_blockers").fetchone()[0] == "user"
                assert fixture.database.execute("SELECT count(*) FROM notification_outbox").fetchone()[0] == 1
                assert fixture.database.execute("SELECT state FROM desktop_requests WHERE request_id=?", (request["request_id"],)).fetchone()[0] == "CONSUMED"
                assert fixture.database.execute("SELECT state FROM application_attempts").fetchone()[0] == "SUBMITTING"
                fixture.now = desktop_adapter.later(fixture.now, 900)
            request = fixture.claim(fixture.queue("reconcile", {"job": fixture.job()}))
            desktop_adapter.complete(fixture.database, dict(request, result=fixture.receipt("NOT_SUBMITTED")), fixture.now)
            applications.reconcile(fixture.database, fixture.job(), fixture.adapter, fixture.policy, fixture.now)
            assert fixture.job()["state"] == "RETRY_WAIT"
            assert fixture.job()["reconcile_attempts"] == 3
            assert fixture.database.execute("SELECT count(*) FROM desktop_requests WHERE operation='submit'").fetchone()[0] == 1
        finally:
            fixture.close()


def main():
    with patch.object(socket.socket, "connect", side_effect=AssertionError("No network")), \
            patch.object(subprocess, "run", side_effect=AssertionError("No subprocess execution")):
        for check in (check_recovery, check_absence, check_blocker, check_submission_blocker_handoff, check_blocked_reconciliation):
            check()
            print(check.__name__ + ": PASS")


if __name__ == "__main__":
    main()
