import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import applications
import conversion
import decision_transport
import engagement
import identity_reconciliation
import notifications
import orchestrator
import outreach
import pipeline_routing
import reconciliation
import reporting
import research_workflow
import workflow_tasks


PROTECTED = ("history", "suppressions", "application_history", "application_referrals", "application_attempts")


def snapshot(database):
    result = {}
    for table in PROTECTED:
        records = [dict(row) for row in database.execute('SELECT * FROM "' + table + '"')]
        serialized = sorted(json.dumps(record, sort_keys=True) for record in records)
        result[table] = {"rows": len(records), "sha256": hashlib.sha256(json.dumps(serialized).encode()).hexdigest()}
    result["job_keys"] = [row[0] for row in database.execute("SELECT job_key FROM application_jobs ORDER BY job_key")]
    return result


def resolved_proof(evidence):
    """The saved identity proof to rehearse: its "resolved" entry, else the first entry carrying a
    job key and requisition, so no employer is named here."""
    if isinstance(evidence.get("resolved"), dict):
        return evidence["resolved"]
    for value in evidence.values():
        if isinstance(value, dict) and value.get("job_key") and value.get("requisition"):
            return value
    raise AssertionError("role-identity-evidence.json holds no resolved identity proof")


def rehearse(source, project, output):
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    with tempfile.TemporaryDirectory(prefix="job-search-migration-") as directory:
        root = Path(directory)
        for folder in ("state", "reports", reconciliation.HANDOFF_DIR):
            (root / folder).mkdir()
        shutil.copyfile(project / reconciliation.HANDOFF_DIR / "REQUIREMENTS.md", root / reconciliation.HANDOFF_DIR / "REQUIREMENTS.md")
        destination = root / "state/orchestrator.sqlite3"
        with sqlite3.connect("file:" + str(source) + "?mode=ro", uri=True) as existing:
            with sqlite3.connect(destination) as copy:
                existing.backup(copy)
        database = orchestrator.connect(root)
        before = snapshot(database)
        config = {"scope": "job_search_e2e", "timezone": "America/Los_Angeles", "applications_enabled": True,
                  "execution_ownership_verified": True, "reporting_owner_confirmed": True,
                  "outreach_mode": "guarded", "daily_report_hour": 18}
        cycles = []
        with patch.object(socket, "create_connection", side_effect=AssertionError("NETWORK_FORBIDDEN")), \
                patch.object(subprocess, "Popen", side_effect=AssertionError("SUBPROCESS_FORBIDDEN")), \
                patch.object(decision_transport.Bridge, "call", side_effect=decision_transport.PreSendUnavailable("OFFLINE_REHEARSAL")), \
                patch.object(notifications.Telegram, "send", side_effect=notifications.Unavailable("OFFLINE_REHEARSAL")):
            for iteration in range(2):
                for module in (applications, workflow_tasks, research_workflow, engagement, outreach,
                               conversion, pipeline_routing, reporting, decision_transport):
                    module.initialize(database)
                notifications.initialize(database)
                database.commit()
                cycles.append(orchestrator.service_cycle(root, config, database))
                if snapshot(database) != before:
                    raise AssertionError("Historical records, queue identity or application attempts changed")
                if database.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise AssertionError("Database integrity failed")
                database.close()
                database = orchestrator.connect(root)
            official = resolved_proof(json.loads((project / "reports/role-identity-evidence.json").read_text()))
            with database:
                identity = identity_reconciliation.reconcile_requisition(
                    database, official["job_key"], official["requisition"], official, started)
            if snapshot(database) != before:
                raise AssertionError("Identity reconciliation changed protected records")
            with patch.object(reporting, "daily", wraps=reporting.daily):
                report_time = "2026-09-18T01:00:00+00:00"
                reporting.daily(database, config, report_time)
                reporting.daily(database, config, report_time)
            report_count = database.execute("SELECT COUNT(*) FROM daily_reports WHERE day='2026-09-17'").fetchone()[0]
            if report_count != 1:
                raise AssertionError("Daily report was not idempotent")
            failures = [{"cycle": index, "worker": name, "result": value} for index, cycle in enumerate(cycles)
                        for name, value in cycle.items() if isinstance(value, dict) and value.get("state") == "FAILED"]
            if failures:
                raise AssertionError(json.dumps(failures))
            result = {"state": "PASS", "started_at": started,
                      "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                      "scope": "Isolated SQLite backup migration and close/reopen rehearsal, not a live service restart",
                      "network_and_subprocesses": "forbidden", "production_writes": 0,
                      "external_application_or_outreach_calls": 0, "service_cycles": len(cycles),
                      "protected_records": {key: value for key, value in before.items() if key != "job_keys"},
                      "preserved_job_count": len(before["job_keys"]), "daily_reports_same_day": report_count,
                      "identity_rehearsal": identity,
                      "cycle_states": [{name: value.get("state", "structured_result") for name, value in cycle.items()
                                        if isinstance(value, dict)} for cycle in cycles],
                      "remaining_gate": "DESKTOP_UNATTENDED_HOST_UNAVAILABLE"}
            database.close()
    output.write_text(json.dumps(result, indent=2) + "\n")
    # Machine contract (E2.T6): the JSON result; stays print.
    print(json.dumps({"state": result["state"], "report": str(output)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    rehearse(arguments.database, arguments.project, arguments.output)
