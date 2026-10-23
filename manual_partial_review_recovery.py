import contextlib
import datetime as dt
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from unittest.mock import patch

import application_recovery
import applications
import desktop_adapter
from manual_application_browser import Fixture


CODE = "FORM_REVIEW_INCOMPLETE"
NOW = "2026-09-17T16:00:00+00:00"
PARTIAL = {"blocked": {"code": CODE, "next_action": "Finish the read-only form review"},
           "all_required_questions_enumerated": False, "questions": [],
           "facts": {"requirements_met": None}, "history": {"application": None}}


def forbidden(*args, **kwargs):
    raise AssertionError("External execution forbidden in isolated validation")


class RecoveryAdapter(desktop_adapter.DesktopAdapter):
    def __init__(self, fixture):
        self.now = NOW
        self.decisions = []
        self.operations = []
        super().__init__(fixture.database, fixture.root, fixture.config, lambda: self.now)

    def recovery_decision(self, packet):
        self.decisions.append(packet)
        assert packet["read_only"] and packet["original_blocker"] == CODE
        return {"action": "RETRY", "method": "official_posting", "progress": False,
                "instructions": "Synthetic bounded form read " + str(packet["recovery"]["attempt"]),
                "reason": "Required form facts remain unknown"}

    def call(self, operation, payload):
        assert operation in {"inspect", "investigate", "reconcile"}, operation
        self.operations.append(operation)
        return super().call(operation, payload)


def complete(fixture, operation, result):
    envelope = desktop_adapter.claim(fixture.database, fixture.root, "synthetic-reader",
                                     fixture.adapter.now, read_only=True)
    assert envelope and envelope["payload"]["operation"] == operation
    desktop_adapter.complete(fixture.database, dict(envelope, result=result), fixture.adapter.now)
    return envelope["request_id"]


def cycle(fixture, seconds=0):
    fixture.adapter.now = desktop_adapter.later(NOW, seconds)
    return applications.cycle(fixture.database, fixture.config, fixture.root,
                              fixture.adapter, fixture.adapter.now)


def offset(value):
    return int((dt.datetime.fromisoformat(value) - dt.datetime.fromisoformat(NOW)).total_seconds())


def inspections(fixture):
    return fixture.database.execute("SELECT count(*) FROM desktop_requests WHERE operation='inspect'").fetchone()[0]


def seed(fixture, code=CODE, held=True):
    cycle(fixture)
    result = dict(PARTIAL, blocked=dict(PARTIAL["blocked"], code=code))
    request_id = complete(fixture, "inspect", result)
    if held:
        with fixture.database:
            applications.hold(fixture.database, fixture.job(), code, "Read remaining form", NOW)
    assert fixture.adapter.cached("inspect", {"job": fixture.job()}) == result
    return request_id


def records(fixture, event):
    return application_recovery.records(fixture.database, fixture.key, event)


def reread_phase(fixture, result, start=60, limit=2):
    """The incomplete form is actually re-read, within the existing inspection bounds.

    Each round: the cached incomplete read schedules one bounded re-read without
    consuming job["attempts"] and without starting the recovery lifecycle; nothing
    happens before the backoff elapses; then a genuinely fresh desktop inspection
    is issued rather than the cached incomplete result being reused.
    """
    moment = start
    for expected in range(1, limit + 1):
        before = inspections(fixture)
        cycle(fixture, moment)
        assert not fixture.adapter.decisions, fixture.job()
        assert not records(fixture, "APPLICATION_RECOVERY_STARTED")
        assert len(records(fixture, "INSPECTION_REREAD_SCHEDULED")) == expected
        job = fixture.job()
        assert job["state"] == "RETRY_WAIT" and job["blocker"] == result["blocked"]["code"], job
        assert job["attempts"] == 0 and job["next_at"] is not None
        assert inspections(fixture) == before
        due = offset(job["next_at"])
        operations = len(fixture.adapter.operations)
        cycle(fixture, due - 1)
        assert len(fixture.adapter.operations) == operations
        cycle(fixture, due)
        assert inspections(fixture) == before + 1
        assert not fixture.adapter.decisions and not records(fixture, "APPLICATION_RECOVERY_STARTED")
        complete(fixture, "inspect", result)
        moment = due + 60
    return moment


def replay_defect(fixture):
    seed(fixture)
    with patch.object(application_recovery, "RECOVERABLE", application_recovery.RECOVERABLE - {CODE}):
        cycle(fixture, 60)
        cycle(fixture, 120)
    assert fixture.job()["blocker"] == CODE and fixture.job()["next_at"] is None
    assert fixture.adapter.operations == ["inspect", "inspect", "inspect"]
    assert not fixture.adapter.decisions
    assert not records(fixture, "INSPECTION_REREAD_SCHEDULED")
    assert not records(fixture, "APPLICATION_RECOVERY_STARTED")
    assert fixture.database.execute("SELECT count(*) FROM desktop_requests").fetchone()[0] == 1


def bounded_reentry(fixture, held=True):
    request_id = seed(fixture, held=held)
    original = dict(fixture.database.execute("SELECT * FROM desktop_requests WHERE request_id=?", (request_id,)).fetchone())
    original_job = fixture.job()
    base = reread_phase(fixture, PARTIAL)
    assert inspections(fixture) == 3
    for attempt in range(3):
        seconds = base + attempt * 1000
        cycle(fixture, seconds)
        assert len(fixture.adapter.decisions) == attempt + 1
        complete(fixture, "investigate", {"status": "FAILED", "reason": "Required facts still unknown"})
        cycle(fixture, seconds + 60)
        if attempt < 2:
            assert fixture.job()["next_at"] is not None
            count = len(fixture.adapter.operations)
            cycle(fixture, seconds + 61)
            assert len(fixture.adapter.operations) == count
            fixture.database.close()
            fixture.database = sqlite3.connect(fixture.root / "state/orchestrator.sqlite3")
            fixture.database.row_factory = sqlite3.Row
            fixture.adapter.database = fixture.database
    job = fixture.job()
    assert job["state"] == "BLOCKED" and job["blocker"] == application_recovery.CLAIMS_EXHAUSTED_BLOCKER, job
    assert not records(fixture, "APPLICATION_ABANDONED")
    held = records(fixture, application_recovery.CLAIMS_EXHAUSTED_EVENT)
    assert len(held) == 1 and held[0]["reason"] == "RECOVERY_CLAIMS_EXHAUSTED_PENDING_REVIEW"
    assert held[0]["attempts"] == 3 and held[0]["terminal"] is False
    assert held[0]["last_decision"]["action"] == "RETRY"
    assert held[0]["submission_status"] == "NOT_SUBMITTED_BY_RECOVERY"
    assert len(records(fixture, "APPLICATION_RECOVERY_STARTED")) == 1
    assert len(records(fixture, "INSPECTION_REREAD_SCHEDULED")) == 2
    assert inspections(fixture) == 3
    assert fixture.database.execute("SELECT sum(claims) FROM desktop_requests WHERE operation='investigate'").fetchone()[0] == 3
    assert dict(fixture.database.execute("SELECT * FROM desktop_requests WHERE request_id=?", (request_id,)).fetchone()) == original
    assert fixture.job()["payload"] == original_job["payload"]
    assert fixture.job()["attempts"] == 0 and fixture.job()["receipt"] is None
    for table in ("application_answers", "application_attempts", "application_reviews"):
        assert fixture.database.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0
    with fixture.database:
        assert applications.ingest(fixture.database, json.loads(original_job["payload"])) == fixture.key
    count = len(fixture.adapter.operations)
    cycle(fixture, 200000)
    applications.process(fixture.database, original_job, fixture.adapter, fixture.policy,
                         fixture.root, fixture.adapter.now, config=fixture.config)
    cycle(fixture, 300000)
    assert len(fixture.adapter.operations) == count and fixture.job()["state"] == "BLOCKED"
    assert fixture.job()["blocker"] == application_recovery.CLAIMS_EXHAUSTED_BLOCKER
    assert not records(fixture, "APPLICATION_ABANDONED")
    assert len(records(fixture, application_recovery.CLAIMS_EXHAUSTED_EVENT)) == 1
    assert len(records(fixture, "APPLICATION_RECOVERY_INVESTIGATED")) == 3
    assert fixture.database.execute("SELECT sum(claims) FROM desktop_requests WHERE operation='investigate'").fetchone()[0] == 3


def first_partial(fixture):
    bounded_reentry(fixture, held=False)




def protected_hold(fixture, code):
    seed(fixture, code)
    cycle(fixture, 60)
    assert fixture.job()["blocker"] == code and fixture.job()["state"] == "BLOCKED"
    assert not fixture.adapter.decisions and not records(fixture, "APPLICATION_RECOVERY_STARTED")
    assert not records(fixture, "INSPECTION_REREAD_SCHEDULED")
    assert not applications.reread_inspection(fixture.database, fixture.job(), fixture.policy, CODE)
    assert not application_recovery.recover(fixture.database, fixture.job(), fixture.adapter,
        fixture.config, CODE, "Partial review", NOW)


def uncertain(fixture):
    seed(fixture)
    with fixture.database:
        fixture.database.execute("INSERT INTO application_attempts(attempt_id,job_key,idempotency_key,state,started_at,evidence) VALUES (?,?,?,'SUBMITTING',?,'{}')",
                                 ("synthetic-attempt", fixture.key, fixture.key, NOW))
    assert not applications.reread_inspection(fixture.database, fixture.job(), fixture.policy, CODE)
    assert not applications.reread_inspection(fixture.database, fixture.job(), fixture.policy, CODE, schedule=True)
    cycle(fixture, 60)
    assert fixture.job()["state"] == "RECONCILE" and not fixture.adapter.decisions
    assert fixture.database.execute("SELECT state FROM application_attempts").fetchone()[0] == "SUBMITTING"
    assert not records(fixture, "APPLICATION_ABANDONED")
    assert not records(fixture, "INSPECTION_REREAD_SCHEDULED")
    assert fixture.adapter.operations == ["inspect", "reconcile"]


def terminal(fixture, state):
    seed(fixture)
    stale = fixture.job()
    with fixture.database:
        fixture.database.execute("UPDATE application_jobs SET state=?", (state,))
    assert not applications.reread_inspection(fixture.database, fixture.job(), fixture.policy, CODE)
    cycle(fixture, 60)
    applications.process(fixture.database, stale, fixture.adapter, fixture.policy,
                         fixture.root, fixture.adapter.now, config=fixture.config)
    assert fixture.job()["state"] == state and fixture.adapter.operations == ["inspect"]
    assert not fixture.adapter.decisions
    assert not records(fixture, "INSPECTION_REREAD_SCHEDULED")


SCENARIOS = {"replay_defect": replay_defect, "bounded_reentry": bounded_reentry,
             "first_partial": first_partial, "uncertain": uncertain}
SCENARIOS.update({code: lambda fixture, code=code: protected_hold(fixture, code)
                  for code in sorted(application_recovery.USER_HOLDS | {"UNKNOWN_REQUIRED_ANSWERS:legal_name"})})
SCENARIOS.update({state: lambda fixture, state=state: terminal(fixture, state)
                  for state in sorted(applications.FINAL)})


def run(names=None):
    passed = []
    with contextlib.ExitStack() as guards:
        for target in ("socket.create_connection", "socket.socket.connect", "socket.getaddrinfo", "subprocess.Popen"):
            guards.enter_context(patch(target, forbidden))
        for name in names if names is not None else SCENARIOS:
            with tempfile.TemporaryDirectory(prefix="partial-review-") as temporary:
                fixture = Fixture(Path(temporary))
                fixture.config["application_recovery"] = {"enabled": True, "max_attempts": 3}
                fixture.adapter = RecoveryAdapter(fixture)
                try:
                    SCENARIOS[name](fixture)
                    assert fixture.database.execute("SELECT count(*) FROM desktop_requests WHERE operation IN ('submit','verify')").fetchone()[0] == 0
                    passed.append(name)
                finally:
                    fixture.database.close()
    return {"mode": "ISOLATED_SYNTHETIC", "passed": passed, "external_effects": 0}


if __name__ == "__main__":
    print(json.dumps(run(sys.argv[1:] or None), indent=2))
