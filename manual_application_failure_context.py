"""Synthetic failure-context rehearsal; reads database metadata, never browser artifacts."""

import json
import datetime as dt
from pathlib import Path
import socket
import tempfile
from unittest.mock import patch

import application_recovery
import applications
import manual_application_recovery_lifecycle as fixtures
import user_profile


def request_failure(fixture, key, request_id="synthetic-failed-read", code="APPLICATION_UNOBSERVED_QUOTE", trace=None):
    trace = trace or str(fixture.root / "reports/application-browser/request-synthetic/trace.jsonl")
    with fixture.database:
        fixture.database.execute("""INSERT INTO desktop_requests
            (request_id,binding,job_key,operation,payload,state,created_at,expires_at,claims,result)
            VALUES (?,?,?,'inspect',?,'CONSUMED',?,?,1,?)""",
            (request_id, request_id, key, json.dumps({"job": {"job_key": key}}), fixtures.NOW, fixtures.NOW,
             json.dumps({"error": {"code": code, "trace_path": trace, "secret": "must-not-propagate"}})))


def preflight_preserves_exact_failure(fixture):
    key, _ = fixture.discover(unresolved=False)
    original = fixture.adapter.call

    def failing_inspect(operation, payload):
        if operation == "inspect":
            request_failure(fixture, key)
            raise ConnectionError("Desktop read failed; see durable request evidence")
        return original(operation, payload)

    fixture.adapter.call = failing_inspect
    fixture.cycle()
    saved = fixture.events(key, "APPLICATION_PREFLIGHT_FAILED")[0]
    assert saved["exception"] == {"exception_type": "ConnectionError", "code": None}
    browser = saved["previous_execution_failures"][0]
    assert browser["code"] == "APPLICATION_UNOBSERVED_QUOTE"
    assert browser["request_id"] == "synthetic-failed-read"
    assert browser["artifacts"]["trace"] == "reports/application-browser/request-synthetic/trace.jsonl"
    fixture.adapter.call = original
    fixture.adapter.responses = [RuntimeError("APPLICATION_UNOBSERVED_QUOTE"), RuntimeError("APPLICATION_BROWSER_TIMEOUT")]
    fixture.cycle(1)
    first = fixture.adapter.decision_packets[0]
    assert first["previous_execution_failures"] == first["recovery"]["previous_execution_failures"]
    assert any(item.get("code") == "APPLICATION_UNOBSERVED_QUOTE" for item in first["previous_execution_failures"])
    assert any(item.get("exception_type") == "ConnectionError" for item in first["previous_execution_failures"])
    fixture.cycle(902)
    repeated = fixture.adapter.decision_packets[1]
    assert repeated["previous_execution_failures"] == first["previous_execution_failures"]
    assert repeated["recovery"]["previous_failure"]["reason"] == "APPLICATION_UNOBSERVED_QUOTE"
    assert "must-not-propagate" not in json.dumps(first)


def existing_failed_record_without_new_event(fixture):
    key, _ = fixture.discover(unresolved=False)
    request_failure(fixture, key, request_id="c000000a-0000-4000-8000-00000000000a")
    with fixture.database:
        applications.hold(fixture.database, fixture.job(key), "PREFLIGHT_TRANSIENT", "Legacy generic blocker", fixtures.NOW)
        applications.event(fixture.database, key, "APPLICATION_PREFLIGHT_FAILED", {"reason": "PREFLIGHT_TRANSIENT"}, fixtures.NOW)
    fixture.adapter.responses = [RuntimeError("APPLICATION_BROWSER_TIMEOUT")]
    fixture.cycle()
    packet = fixture.adapter.decision_packets[0]
    assert any(item.get("request_id") == "c000000a-0000-4000-8000-00000000000a"
               and item.get("code") == "APPLICATION_UNOBSERVED_QUOTE" for item in packet["previous_execution_failures"])
    assert len(fixture.events(key, "APPLICATION_PREFLIGHT_FAILED")) == 1
    assert len(fixture.adapter.investigations) == 1


def bounded_and_job_scoped(fixture):
    key, _ = fixture.discover(unresolved=False)
    other, _ = fixture.discover("Different Job", unresolved=True)
    for index in range(9):
        request_failure(fixture, key, "read-" + str(index))
    request_failure(fixture, other, "other-job", "APPLICATION_OTHER_JOB_FAILURE")
    failures = application_recovery.previous_execution_failures(fixture.database, key, fixture.root)
    assert len(failures) == 5
    assert {item["request_id"] for item in failures} == {"read-" + str(index) for index in range(4, 9)}
    assert all(item["job_key"] == key for item in failures)
    assert "APPLICATION_OTHER_JOB_FAILURE" not in json.dumps(failures)


def no_arbitrary_artifact_reads(fixture):
    key, _ = fixture.discover(unresolved=False)
    paths = ["https://user:secret@example.invalid/trace.jsonl?token=secret", "/private/tmp/secret",
             str(fixture.root / "reports/application-browser/request-safe/../../secret/trace.jsonl"),
             str(fixture.root / "reports/application-browser/request-safe/trace.jsonl")]
    for index, trace in enumerate(paths):
        request_failure(fixture, key, "path-" + str(index), trace=trace)
    with patch("builtins.open", side_effect=AssertionError("No artifact reads")), patch.object(Path, "open", side_effect=AssertionError("No artifact reads")):
        failures = application_recovery.previous_execution_failures(fixture.database, key, fixture.root)
    assert sum(bool(item["artifacts"]) for item in failures) == 1
    assert "secret" not in json.dumps(failures)


def generic_exception_is_sanitized(fixture):
    key, _ = fixture.discover(unresolved=False)

    def failing_inspect(operation, payload):
        raise ValueError("https://user:password@example.invalid/path?token=credential secret=do-not-log")

    fixture.adapter.call = failing_inspect
    fixture.cycle()
    saved = fixture.events(key, "APPLICATION_PREFLIGHT_FAILED")[0]
    assert saved["exception"] == {"exception_type": "ValueError", "code": None}
    serialized = json.dumps(saved)
    assert all(secret not in serialized for secret in ("https:", "password", "credential", "do-not-log"))
    assert application_recovery.exception_failure(ValueError("APPLICATION_UNOBSERVED_QUOTE")) == {
        "exception_type": "ValueError", "code": "APPLICATION_UNOBSERVED_QUOTE"}


def waits_do_not_become_failures(fixture):
    key, _ = fixture.discover(unresolved=False)

    def pending_inspect(operation, payload):
        raise applications.AdapterPending("synthetic-pending", "2026-09-17T17:00:00+00:00")

    fixture.adapter.call = pending_inspect
    fixture.cycle()
    assert fixture.job(key)["attempts"] == 0
    assert not fixture.events(key, "APPLICATION_PREFLIGHT_FAILED")
    assert not application_recovery.previous_execution_failures(fixture.database, key, fixture.root)


def failure_uses_actual_clock(fixture):
    key, _ = fixture.discover(unresolved=False)
    fixture.adapter.inspect_failures = 1
    actual = (dt.datetime.fromisoformat(fixtures.NOW) + dt.timedelta(minutes=7)).isoformat()
    policy = user_profile.load()
    applications.process(fixture.database, fixture.job(key), fixture.adapter, policy, fixture.root,
                         fixtures.NOW, clock=lambda: actual)
    saved = fixture.database.execute("SELECT at FROM application_events WHERE event='APPLICATION_PREFLIGHT_FAILED' AND job_key=?", (key,)).fetchone()
    assert saved[0] == actual
    assert fixture.job(key)["updated_at"] == actual
    expected_retry = (dt.datetime.fromisoformat(actual) + dt.timedelta(seconds=policy["retry_delays_seconds"][0])).isoformat()
    assert fixture.job(key)["next_at"] == expected_retry


def prepared_packet_stays_immutable(fixture):
    key, _ = fixture.discover(unresolved=False)
    with fixture.database:
        applications.hold(fixture.database, fixture.job(key), "PREFLIGHT_TRANSIENT", "Legacy generic blocker", fixtures.NOW)
    fixture.adapter.responses = [
        applications.AdapterPending("inflight-synthetic-read", "2026-09-17T16:01:00+00:00"),
        RuntimeError("APPLICATION_UNOBSERVED_QUOTE"), RuntimeError("APPLICATION_BROWSER_TIMEOUT")]
    fixture.cycle()
    original = {row[0]: row[1] for row in fixture.database.execute("""SELECT event,detail FROM application_events
        WHERE job_key=? AND event IN ('APPLICATION_RECOVERY_DECIDED','APPLICATION_RECOVERY_PREPARED')""", (key,))}
    assert not fixture.adapter.investigations[0]["previous_execution_failures"]
    request_failure(fixture, key, request_id="previously-consumed-inspection")
    fixture.cycle(61)
    assert len(fixture.adapter.decision_packets) == 1
    assert fixture.adapter.investigations[1] == fixture.adapter.investigations[0]
    fixture.cycle(962)
    assert len(fixture.adapter.decision_packets) == 2
    latest = fixture.adapter.decision_packets[-1]
    assert any(item.get("code") == "APPLICATION_UNOBSERVED_QUOTE" for item in latest["previous_execution_failures"])
    assert latest["recovery"]["previous_failure"]["reason"] == "APPLICATION_UNOBSERVED_QUOTE"
    for event, detail in original.items():
        saved = fixture.database.execute("SELECT detail FROM application_events WHERE job_key=? AND event=? ORDER BY id LIMIT 1", (key, event)).fetchone()[0]
        assert saved == detail


SCENARIOS = [preflight_preserves_exact_failure, existing_failed_record_without_new_event, bounded_and_job_scoped,
             no_arbitrary_artifact_reads, generic_exception_is_sanitized, waits_do_not_become_failures,
             failure_uses_actual_clock, prepared_packet_stays_immutable]


def run(scenarios=SCENARIOS):
    passed = []
    with patch.object(socket, "create_connection", side_effect=AssertionError("No network in synthetic rehearsal")):
        for scenario in scenarios:
            with tempfile.TemporaryDirectory(prefix="application-failure-context-") as temporary:
                fixture = fixtures.Fixture(Path(temporary))
                try:
                    scenario(fixture)
                    passed.append(scenario.__name__)
                finally:
                    fixture.database.close()
    return {"mode": "MOCK_ONLY", "passed": passed, "live_effects": 0}


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
