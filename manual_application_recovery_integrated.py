import datetime as dt
import json
from pathlib import Path
import tempfile
import sys
from unittest.mock import patch

import application_browser
import application_connection
import application_recovery_decision
import applications
import reporting
import workflow_tasks
from manual_application_browser import write_output
from manual_application_recovery_decision_slice import decision, executor_for
from manual_browser_recovery_slice import RecoveryFixture, identity

sys.path.insert(0, str(Path(__file__).resolve().parent / "tests"))
from test_conversion import ConversionTests


def validate_abandoned_handoffs():
    checks = []
    # Referral scenarios were removed on 2026-09-25: referrals are tracked
    # independently and no longer produce conversion events on a job.
    for scenario in ("later_interview",):
        fixture = ConversionTests()
        fixture.setUp()
        try:
            with fixture.database:
                fixture.database.execute("UPDATE application_jobs SET state='ABANDONED' WHERE job_key=?", (fixture.key,))
            fixture.event("confirmed-interview", "INTERVIEW_STAGE", {"stage_id": "screen-1", "stage": "RECRUITER_SCREEN"})
            fixture.cycle()
            assert fixture.job()["state"] == "ABANDONED", scenario
            if scenario == "later_interview":
                assert fixture.database.execute("SELECT 1 FROM application_history WHERE kind='INTERVIEW'").fetchone()
            checks.append(scenario + " preserves terminal abandonment")
        finally:
            fixture.doCleanups()
    return {"state": "PASS", "checks": checks, "production_modified": False}


def validate():
    checks = []
    original_decide = application_recovery_decision.decide
    with tempfile.TemporaryDirectory() as temporary:
        for outcome in ("resolved", "abandoned"):
            fixture = RecoveryFixture(Path(temporary) / outcome)
            try:
                now = applications.stamp()
                current = [now]
                config = dict(fixture.config, application_browser={"enabled": True, "max_operations": 4},
                              application_recovery={"enabled": True, "max_attempts": 3})
                (fixture.root / "config.json").write_text(json.dumps(config))
                workflow_tasks.initialize(fixture.database)
                with fixture.database:
                    payload = json.loads(fixture.job()["payload"])
                    payload.update(url="https://careers.synthetic.example/opening", requisition_id="")
                    # Seeded, not minted, and seeded as REQUISITION_MISMATCH. Setting the
                    # requisition alone no longer parks the job: local_gate stopped refusing a
                    # URL identity on 2026-09-24 (approach A), and the REQUISITION_UNKNOWN this
                    # used to rely on is now superseded on sight, so no lifecycle would run.
                    # This rehearsal is about the identity RECOVERY protocol, which is
                    # unchanged and which REQUISITION_MISMATCH enters with the same methods.
                    fixture.database.execute(
                        "UPDATE application_jobs SET requisition=?,payload=?,state='BLOCKED',"
                        "blocker='REQUISITION_MISMATCH',next_at=NULL,"
                        "next_action='Reconcile employer/requisition identity before applying' WHERE job_key=?",
                        ("unresolved:synthetic", json.dumps(payload), fixture.key))
                    assert workflow_tasks.reserve_compute(fixture.database, "original-spent-day", now, 10800)
                    workflow_tasks.grant_application_recovery(fixture.database, "synthetic-authorized-recovery", [fixture.key],
                        2400, now, (dt.datetime.fromisoformat(now) + dt.timedelta(hours=1)).isoformat(), "Synthetic owner authorization")
                decisions, operations = [], []

                def judge(root, settings, packet, timeout):
                    value = decision()
                    if packet["previous_findings"]:
                        assert packet["previous_decisions"]
                        value = decision(action="ABANDON", method="", instructions="",
                                         reason="The scoped read found no supported resolution or remaining source")
                    return original_decide(root, settings, packet, timeout, executor_for(value, decisions))

                def executor(settings, prompt, directory, timeout):
                    request = json.loads((directory / "request.json").read_text())
                    operation = request["payload"]["operation"]
                    operations.append(operation)
                    assert operation in {"investigate", "inspect"}
                    if operation == "inspect":
                        result = {"blocked": {"code": "AUTHENTICATION_REQUIRED", "next_action": "Synthetic isolated account blocker"}}
                    elif outcome == "resolved":
                        result = {"status": "RESOLVED", "reason": "Exact role identity was observed",
                                  "next_action": "Inspect the resolved official form", "resolution": {
                                      "identity": {"requisition": "role-123", "evidence": identity(fixture)}, "history": []}}
                    else:
                        result = {"status": "UNRESOLVED", "reason": "Scoped official source cannot resolve the historical identity",
                                  "next_action": "Return this inability to the AI orchestrator", "resolution": {}}
                    write_output(directory, request, result)

                def consume(root, database, settings, request_id):
                    return application_browser.consume(root, database, settings, request_id, executor=executor)

                def cycle():
                    adapter = application_connection.HostedDesktopAdapter(fixture.database, fixture.root, config,
                        clock=lambda: current[0], consumer=consume)
                    with patch("application_recovery_decision.decide", side_effect=judge):
                        return applications.cycle(fixture.database, config, fixture.root, adapter=adapter, now=current[0])

                # The fixture's résumé is a mock file, not a PDF: no extractor runs here, so the read
                # packet carries the résumé as unavailable (reply_resume.context).
                with patch("subprocess.Popen", side_effect=AssertionError("No external executor")), \
                        patch("socket.socket.connect", side_effect=AssertionError("No network")), \
                        patch("reply_resume.context", side_effect=ValueError("REPLY_RESUME_UNAVAILABLE")):
                    cycle()
                    if outcome == "resolved":
                        assert operations == ["investigate", "inspect"], (operations, fixture.job())
                        assert fixture.job()["requisition"] == "role-123"
                        assert fixture.job()["blocker"] == "AUTHENTICATION_REQUIRED"
                    else:
                        current[0] = (dt.datetime.fromisoformat(now) + dt.timedelta(minutes=16)).isoformat()
                        cycle()
                        assert operations == ["investigate"] and len(decisions) == 2
                        assert fixture.job()["state"] == "ABANDONED", fixture.job()
                        cycle()
                        assert len(decisions) == 2 and operations == ["investigate"]
                        summary = reporting.bounded_recovery(fixture.database, config, current[0])
                        assert "The scoped read found no supported resolution" in json.dumps(summary)
                assert fixture.database.execute("SELECT COUNT(*) FROM application_attempts").fetchone()[0] == 0
                assert fixture.database.execute("SELECT seconds FROM worker_compute_reservations WHERE action_id='original-spent-day'").fetchone()[0] == 10800
                # Both branches admit three recovery actions: a 30-second coordinator
                # reservation, a 480-second browser read, and a third action. Count the actions,
                # not the grant charges. The grant only pays for an action the ordinary daily
                # allowance cannot, and in the abandoned branch the unused tail of the first
                # read is credited back before the second coordinator call, so that call is
                # funded normally and never touches the grant. Counting charges instead read 2
                # there and had been failing since 2026-09-22, when removing the submit gate's
                # history re-checks (EU) changed which action settles first. Preserving the
                # grant is the allowance working, not a recovery action going missing.
                actions = [row[0] for row in fixture.database.execute(
                    "SELECT action_id FROM worker_compute_reservations WHERE action_id LIKE 'application-browser:%'")]
                assert len(actions) == 3, actions
                # Nothing may charge the authorized grant except one of those admitted actions.
                charged = {row[0] for row in fixture.database.execute(
                    "SELECT action_id FROM worker_compute_recovery_charges")}
                assert charged and charged <= set(actions), (charged, actions)
                checks.append(outcome + ": real host admission, AI coordinator protocol, browser normalization, lifecycle and report integrated without external effects")
            finally:
                fixture.database.close()
    return {"state": "PASS", "checks": checks, "production_modified": False, "live_submissions": 0}


if __name__ == "__main__":
    print(json.dumps({"integrated": validate(), "terminal_handoffs": validate_abandoned_handoffs()}, indent=2))
