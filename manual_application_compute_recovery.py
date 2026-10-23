import concurrent.futures
import contextlib
import json
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import patch

import applications
import orchestrator
import workflow_tasks


NOW = "2026-09-18T04:00:00+00:00"
EXPIRY = "2026-09-18T07:00:00+00:00"


@contextlib.contextmanager
def connect(path):
    """A connection that commits (or rolls back) and is closed at the end of the `with`: an open
    one keeps the file in use on Windows, so the temporary folder could not be removed."""
    database = sqlite3.connect(path, timeout=15)
    database.row_factory = sqlite3.Row
    try:
        with database:
            yield database
    finally:
        database.close()


def validate():
    checks = []
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "state.sqlite3"
        with connect(path) as database:
            applications.initialize(database)
            workflow_tasks.initialize(database)
            job = applications.ingest(database, {"company": "Example", "role": "Senior Engineer",
                "url": "https://jobs.ashbyhq.com/example/role-one"}, NOW)
            second = applications.ingest(database, {"company": "Example", "role": "Staff Engineer",
                "url": "https://jobs.ashbyhq.com/example/role-two"}, NOW)
            assert workflow_tasks.reserve_compute(database, "existing-day", NOW, 10800)
            before = tuple(database.execute("SELECT * FROM worker_compute_reservations").fetchone())
            assert not workflow_tasks.reserve_application_compute(database, "application-browser:waiting", job, NOW, 240)
            assert workflow_tasks.grant_application_recovery(database, "authorized-recovery", [job], 720, NOW, EXPIRY, "Explicit synthetic owner authorization")
            assert not workflow_tasks.grant_application_recovery(database, "authorized-recovery", [job], 720, NOW, EXPIRY, "Explicit synthetic owner authorization")
            assert workflow_tasks.application_recovery_available(database, job, NOW)
            assert not workflow_tasks.application_recovery_available(database, second, NOW)
            assert not workflow_tasks.reserve_application_compute(database, "application-browser:outside", second, NOW, 240)
            assert not workflow_tasks.reserve_compute(database, "unrelated-worker", NOW, 1)
            assert workflow_tasks.reserve_application_compute(database, "application-browser:first", job, NOW, 240)
            assert workflow_tasks.reserve_application_compute(database, "application-browser:first", job, NOW, 240)
            assert tuple(database.execute("SELECT * FROM worker_compute_reservations WHERE action_id='existing-day'").fetchone()) == before
            assert database.execute("SELECT SUM(seconds) FROM worker_compute_recovery_charges").fetchone()[0] == 240
            checks.append("preserved old charges; scoped one-time grant; replay does not replenish; unrelated workers remain blocked")
            for grant_id, scope, amount in [("authorized-recovery", [job], 721), ("second-grant", [job], 720)]:
                try:
                    workflow_tasks.grant_application_recovery(database, grant_id, scope, amount, NOW, EXPIRY, "Explicit synthetic owner authorization")
                except ValueError:
                    pass
                else:
                    raise AssertionError("Grant was changed or replenished")
            try:
                workflow_tasks.reserve_application_compute(database, "application-browser:first", second, NOW, 240)
            except ValueError:
                pass
            else:
                raise AssertionError("Reservation changed job scope")
            checks.append("grant scope and reservation identity cannot be rebound")

        def reserve(index):
            with connect(path) as database:
                return workflow_tasks.reserve_application_compute(database, "application-browser:parallel-" + str(index), job, NOW, 240)

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            outcomes = list(executor.map(reserve, range(6)))
        assert sum(outcomes) == 2, outcomes
        checks.append("six competing restarted connections cannot exceed remaining allowance")
        with connect(path) as database:
            assert database.execute("SELECT SUM(seconds) FROM worker_compute_recovery_charges").fetchone()[0] == 720
            assert not workflow_tasks.application_recovery_available(database, job, NOW)
            assert not workflow_tasks.reserve_application_compute(database, "application-browser:extra", job, NOW, 1)
            assert not workflow_tasks.application_recovery_available(database, job, EXPIRY)
            tomorrow = "2026-09-18T16:00:00+00:00"  # the next 9:00 Pacific service day (2026-09-30)
            assert workflow_tasks.reserve_application_compute(database, "application-browser:normal-next-day", job, tomorrow, 240)
            assert database.execute("SELECT SUM(seconds) FROM worker_compute_recovery_charges").fetchone()[0] == 720
            checks.append("exhaustion/expiry stop recovery; next normal day resumes without deleting history")
        with connect(Path(temporary) / "old.sqlite3") as database:
            assert not workflow_tasks.application_recovery_available(database, "unknown", NOW)
            checks.append("legacy database without recovery tables remains compatible")
    return {"state": "PASS", "checks": checks, "live_actions": 0, "production_modified": False}


def validate_handoff():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / "state").mkdir()
        database = orchestrator.connect(root)
        workflow_tasks.initialize(database)
        inputs = [{"source": "https://example.org/jobs", "observed_at": NOW}]
        deadline = "2099-01-01T00:00:00+00:00"
        with database:
            old = workflow_tasks.enqueue(database, "agent-investigation", "application-public-evidence:old", "parent", inputs, deadline, NOW)
            other = workflow_tasks.enqueue(database, "agent-investigation", "other-public-research", "parent", inputs, deadline, NOW)
            active = workflow_tasks.enqueue(database, "agent-investigation", "application-public-evidence:active", "parent", inputs, deadline, NOW)
            database.execute("UPDATE workflow_tasks SET status='RETRY_WAIT',result=? WHERE task_id=?", ('{"failure":"saved failure"}', old))
        workflow_tasks.claim(database, active, applications.stamp(), lease_seconds=3600)
        config = {"agent_worker": {"enabled": True, "max_items": 1}, "application_recovery": {"enabled": True}}
        with patch("agent_worker.cycle", side_effect=lambda *args: {"state": "COMPLETE", "tasks": []}):
            first = orchestrator.agent_cycle(root, config, database)
            second = orchestrator.agent_cycle(root, config, database)
        assert first["application_recovery_handoffs"] == [old]
        assert second["application_recovery_handoffs"] == []
        previous = database.execute("SELECT * FROM workflow_tasks WHERE task_id=?", (old,)).fetchone()
        assert previous["status"] == "CANCELLED" and json.loads(previous["result"])["failure"] == "saved failure"
        assert database.execute("SELECT status FROM workflow_tasks WHERE task_id=?", (other,)).fetchone()[0] == "QUEUED"
        assert database.execute("SELECT status FROM workflow_tasks WHERE task_id=?", (active,)).fetchone()[0] == "RUNNING"
        assert database.execute("SELECT COUNT(*) FROM workflow_task_events WHERE task_id=? AND event='CANCELLED'", (old,)).fetchone()[0] == 1
        database.close()
    return {"state": "PASS", "checks": ["old duplicate investigation handed off once with prior failure preserved", "unrelated research and unexpired active lease untouched"], "live_actions": 0}


if __name__ == "__main__":
    print(json.dumps({"compute": validate(), "handoff": validate_handoff()}, indent=2))
