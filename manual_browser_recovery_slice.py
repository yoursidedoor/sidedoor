"""Synthetic read-only recovery rehearsal; no browser, network or production DB."""

import copy
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

import application_browser as browser
import application_destination
import applications
import desktop_adapter
import external_history
import identity_reconciliation
from manual_application_browser import Fixture, event, write_output


class RecoveryFixture(Fixture):
    def review(self):
        return dict(super().review(), official_url=json.loads(self.job()["payload"])["url"],
                    role="Staff Software Engineer", candidate_email=self.job()["candidate_email"])


def forbidden(*args, **kwargs):
    raise AssertionError("No external execution authorized in this rehearsal")


def rejects(action, code=None):
    try:
        action()
    except ValueError as error:
        assert code is None or code in str(error), str(error)
        return
    raise AssertionError("Expected rejection")


def envelope(fixture):
    job = fixture.job()
    return {"request_id": "synthetic-read", "binding": "synthetic-binding", "max_tool_calls": 40,
            "payload": {"operation": "investigate", "job": job, "attempt_id": None},
            "context": browser.context(fixture.root, fixture.database, {"job": job})}


def identity(fixture, requisition="role-123"):
    job = fixture.job()
    return {"job_key": job["job_key"], "employer": job["employer"], "role": "Staff Software Engineer",
            "requisition": requisition, "official_url": "https://careers.synthetic.example/application?job=role-123",
            "candidate_email": job["candidate_email"], "identity_basis": "official_canonical_posting_id",
            "official_destination_verified": True, "independent_read": True, "verified": True}


def normalized(fixture, request, result, mutate=None):
    directory = fixture.root / "normalization"
    directory.mkdir(exist_ok=True)
    write_output(directory, request, result, mutate=mutate)
    raw = json.loads((directory / "output.json").read_text(encoding="utf-8"))
    return browser.normalize(request, raw, (directory / "trace.jsonl").read_bytes(), applications.stamp(), directory / "trace.jsonl")


def run():
    checks = []
    with tempfile.TemporaryDirectory(prefix="application-recovery-slice-") as temporary, \
            patch("socket.create_connection", forbidden), patch("socket.socket.connect", forbidden), \
            patch("socket.getaddrinfo", forbidden), patch("subprocess.Popen", forbidden):
        fixture = RecoveryFixture(Path(temporary) / "recovery")
        try:
            original_key = fixture.key
            with fixture.database:
                payload = json.loads(fixture.job()["payload"])
                payload.update(url="https://careers.synthetic.example/opening", requisition_id="")
                fixture.database.execute("UPDATE application_jobs SET requisition=?,payload=? WHERE job_key=?",
                    ("unresolved:synthetic", json.dumps(payload), fixture.key))
            packet = {"job": fixture.job(), "recovery": {"attempt": 1, "max_attempts": 3,
                "deadline": desktop_adapter.later(applications.stamp(), 3600),
                "previous_failure": {"reason": "REQUISITION_UNKNOWN"},
                "supported_alternative": {"method": "observed_links", "instructions": "Read observed official application link"}}}
            try:
                fixture.adapter.call("investigate", packet)
            except applications.AdapterPending as pending:
                request_id = pending.request_id
            else:
                raise AssertionError("Expected durable read mailbox")
            def executor(config, prompt, directory, timeout):
                request = json.loads((directory / "request.json").read_text())
                assert "_application_guard_claim" not in config
                assert request["payload"]["read_only"] is True
                assert request["context"]["recovery"]["previous_failure"]["reason"] == "REQUISITION_UNKNOWN"
                write_output(directory, request, {"status": "RESOLVED", "reason": "Observed exact official identity",
                    "next_action": "Reconcile then inspect the current form", "resolution": {"identity": {"requisition": "role-123", "evidence": identity(fixture)}, "history": []}})
            summary = browser.consume(fixture.root, fixture.database, fixture.config, request_id, executor)
            assert summary["state"] == "COMPLETE", summary
            result = fixture.adapter.call("investigate", packet)
            assert result["resolution"]["identity"]["requisition"] == "role-123"
            assert fixture.job()["requisition"] == "unresolved:synthetic"
            assert fixture.database.execute("SELECT count(*) FROM application_attempts").fetchone()[0] == 0
            assert fixture.database.execute("SELECT armed_at FROM desktop_requests").fetchone()[0] is None
            checks.append("investigate mailbox reuses consumer; read-only evidence result; no job mutation or submission intent")
            evidence = result["resolution"]["identity"]["evidence"]
            with fixture.database:
                identity_reconciliation.reconcile_requisition(fixture.database, fixture.key, "role-123", evidence, applications.stamp())
                identity_reconciliation.reconcile_destination(fixture.database, fixture.key, evidence["official_url"], evidence, applications.stamp())
            assert fixture.key == original_key and fixture.job()["requisition"] == "role-123"
            assert fixture.database.execute("SELECT count(*) FROM application_jobs").fetchone()[0] == 1
            assert json.loads(fixture.job()["payload"])["url"] == evidence["official_url"]
            checks.append("canonical key retained; generic application path/query reconciles without creating a duplicate job")
            request = envelope(fixture)
            proposal = {"status": "RESOLVED", "reason": "Current official role observed", "next_action": "Inspect", "resolution": {"identity": {"requisition": "role-123", "evidence": identity(fixture)}, "history": []}}
            for field, value in (("candidate_email", "wrong@example.invalid"), ("requisition", "other-role"), ("role", "Other Role")):
                changed = copy.deepcopy(proposal)
                changed["resolution"]["identity"]["evidence"][field] = value
                rejects(lambda: normalized(fixture, request, changed))
            def invent(raw):
                raw["evidence"][0]["quote"] = "NEVER OBSERVED"
            rejects(lambda: normalized(fixture, request, proposal, invent), "UNOBSERVED")
            checks.append("wrong candidate/role/requisition and fabricated quotes rejected")
            for unsafe in ("http://careers.example/jobs", "https://127.0.0.1/jobs", "https://192.168.1.1/jobs",
                           "https://localhost/jobs", "https://portal.internal/jobs", "https://user:pass@example.com/jobs",
                           "https://careers.example/jobs?access_token=secret", "https://aiapply.co/jobs", "https://example.com:444/jobs"):
                rejects(lambda: application_destination.public_url(unsafe))
            for tool in ("browser_fill_form", "browser_click", "browser_press_key", "browser_file_upload", "browser_type"):
                item = event("mutation", "", tool=tool, arguments={"target": "e1"})["item"]
                rejects(lambda: browser.tool_item(item, False, True), "READ_ONLY")
            rejects(lambda: browser.tool_item(event("arm", "", tool="arm")["item"], False, True))
            checks.append("unsafe destinations, mutation methods and investigation arm rejected")
            for relationship, prior in (("DISTINCT_ROLE", "old-role"), ("SAME_ROLE", "role-123")):
                evidence_id = "synthetic-history-" + relationship
                record = {"verified": True, "independent_read": True, "mailbox_email": fixture.job()["candidate_email"],
                          "source": "https://mail.google.com/mail/u/0/#all/synthetic", "requisition": prior}
                with fixture.database:
                    fixture.database.execute("INSERT INTO application_external_history VALUES (?,?,?,NULL,?,?,?,?,?)",
                        (evidence_id, fixture.job()["employer"], "APPLICATION_CONFIRMED", "Saved prior role", applications.stamp(), applications.stamp(), json.dumps(record), "synthetic-hash"))
                request = envelope(fixture)
                saved_index = next(index for index, row in enumerate(request["context"]["external_history"]) if row["evidence_id"] == evidence_id)
                history = {"evidence_id": evidence_id, "relationship": relationship, "evidence": {
                    "verified": True, "job_key": fixture.key, "evidence_id": evidence_id, "employer": fixture.job()["employer"],
                    "candidate_email": fixture.job()["candidate_email"], "current_requisition": "role-123", "prior_requisition": prior,
                    "comparison": "Exact current requisition role-123 compared to saved prior requisition " + prior}}
                proposal = {"status": "RESOLVED", "reason": "Exact requisitions compared", "next_action": "Apply existing history gate",
                            "resolution": {"identity": None, "history": [history]}}
                def bind_saved(raw):
                    for entry in raw["evidence"]:
                        if entry["pointer"] == "/resolution/history/0/evidence/prior_requisition":
                            entry.update(tool_call_id="context:/context/external_history/" + str(saved_index) + "/payload", quote=prior)
                result = normalized(fixture, request, proposal, bind_saved)
                with fixture.database:
                    external_history.resolve(fixture.database, evidence_id, fixture.key, relationship, result["resolution"]["history"][0]["evidence"], applications.stamp())
                assert fixture.database.execute("SELECT relationship FROM application_history_resolutions WHERE evidence_id=?", (evidence_id,)).fetchone()[0] == relationship
                if relationship == "SAME_ROLE":
                    assert applications.local_gate(fixture.database, fixture.job(), applications.stamp())[0] == "DUPLICATE"
                contradictory = copy.deepcopy(proposal)
                contradictory["resolution"]["history"][0]["relationship"] = "DISTINCT_ROLE" if relationship == "SAME_ROLE" else "SAME_ROLE"
                rejects(lambda: normalized(fixture, request, contradictory, bind_saved))
            checks.append("exact saved SAME_ROLE/DISTINCT_ROLE evidence resolves via existing contract; same-role duplicate gate retained")
            evidence_id = "synthetic-history-missing-id"
            missing_id_record = {"verified": True, "independent_read": True, "mailbox_email": fixture.job()["candidate_email"],
                "source": "https://mail.google.com/mail/u/0/#all/synthetic", "role_title": "Senior Database Engineer",
                "team": "Database Platform", "occurred_at": "2024-01-01T00:00:00+00:00", "requisition": None}
            with fixture.database:
                fixture.database.execute("INSERT INTO application_external_history VALUES (?,?,?,NULL,?,?,?,?,?)",
                    (evidence_id, fixture.job()["employer"], "APPLICATION_CONFIRMED", "Senior Database Engineer", applications.stamp(), applications.stamp(), json.dumps(missing_id_record), "synthetic-hash"))
            request = envelope(fixture)
            saved_index = next(index for index, row in enumerate(request["context"]["external_history"]) if row["evidence_id"] == evidence_id)
            unknown_history = copy.deepcopy(history)
            unknown_history.update(evidence_id=evidence_id, relationship="DISTINCT_ROLE")
            unknown_history["evidence"].update(evidence_id=evidence_id, prior_requisition=None,
                comparison="Saved dated Database Platform process is independently distinct from the observed current infrastructure role")
            def bind_missing_id(raw):
                raw["evidence"].append({"pointer": "/resolution/history/0/evidence/comparison",
                    "tool_call_id": "context:/context/external_history/" + str(saved_index) + "/payload", "quote": "Database Platform"})
            unknown_proposal = {"status": "RESOLVED", "reason": "Source-bound comparison uses role, team and process timing",
                "next_action": "Apply the comparison without inventing the prior requisition", "resolution": {"history": [unknown_history]}}
            unknown_result = normalized(fixture, request, unknown_proposal, bind_missing_id)
            assert unknown_result["resolution"]["history"][0]["evidence"]["prior_requisition"] is None
            with fixture.database:
                external_history.resolve(fixture.database, evidence_id, fixture.key, "DISTINCT_ROLE", unknown_result["resolution"]["history"][0]["evidence"], applications.stamp())
            rejects(lambda: normalized(fixture, request, unknown_proposal), "HISTORY_SOURCE_REQUIRED")
            checks.append("source-bound AI comparison resolves missing prior requisition without invention; unsourced inference rejected")
            live_history = copy.deepcopy(unknown_history)
            live_history["relationship"] = "DISTINCT_ROLE"
            live_history["evidence"].update(prior_requisition="prior-live-999", comparison="Saved source message identifies prior-live-999, distinct from current role-123",
                mailbox_email=fixture.job()["candidate_email"], recipient=fixture.job()["candidate_email"],
                sender="recruiting@synthetic.example", sender_authenticated=True,
                source_message_id="synthetic", source_url="https://mail.google.com/mail/u/0/#all/synthetic")
            live_proposal = {"status": "RESOLVED", "reason": "Exact saved message reread in verified candidate account",
                "next_action": "Apply sourced comparison", "resolution": {"history": [live_history]}}
            def bind_saved_message(raw):
                for entry in raw["evidence"]:
                    if entry["pointer"] == "/resolution/history/0/evidence/evidence_id":
                        entry.update(tool_call_id="context:/context/external_history/" + str(saved_index) + "/evidence_id", quote=evidence_id)
            live_result = normalized(fixture, request, live_proposal, bind_saved_message)
            assert live_result["resolution"]["history"][0]["evidence"]["independent_read"] is True
            wrong_account = copy.deepcopy(live_proposal)
            wrong_account["resolution"]["history"][0]["evidence"]["mailbox_email"] = "wrong@example.invalid"
            rejects(lambda: normalized(fixture, request, wrong_account, bind_saved_message), "MAILBOX_UNBOUND")
            unbound_live = copy.deepcopy(live_proposal)
            del unbound_live["resolution"]["history"][0]["evidence"]["source_message_id"]
            rejects(lambda: normalized(fixture, request, unbound_live, bind_saved_message), "MAILBOX_UNBOUND")
            checks.append("scoped live Gmail history requires observed message/sender/recipient and exact signed-in candidate; wrong or unbound mailbox rejected")
            for status in ("UNRESOLVED", "ABANDON"):
                result = normalized(fixture, request, {"status": status, "reason": "Evidence cannot identify prior requisition",
                    "next_action": "Retain unknown or abandon at bound", "resolution": {"identity": None, "history": []}})
                assert result["status"] == status and result["resolution"]["identity"] is None
            checks.append("reasoned unresolved and abandonment results invent no identity or history clearance")
            # The denied-destination refusal checked here is deleted (owner, 2026-09-27, item 2).
        finally:
            fixture.database.close()
        guarded = RecoveryFixture(Path(temporary) / "guard")
        try:
            request_id = guarded.prepare_submit()
            request = desktop_adapter.claim(guarded.database, guarded.root, "synthetic-guard", request_id=request_id)
            assert request is not None
            evidence = desktop_adapter.submission_gate(guarded.database, guarded.root,
                guarded.database.execute("SELECT * FROM desktop_requests WHERE request_id=?", (request_id,)).fetchone(), applications.stamp())
            # The arm-time OFFICIAL_DESTINATION check is deleted (owner, 2026-09-27, item 27).
            armed = desktop_adapter.arm(guarded.database, guarded.root, dict(request, observed_form_revision="fixture-form-v1"))
            assert armed["may_submit_once"] is True
            rejects(lambda: desktop_adapter.arm(guarded.database, guarded.root, dict(request, observed_form_revision="fixture-form-v1")))
            checks.append("final existing arm rejects changed destination and remains one-shot; no actual submit executed")
        finally:
            guarded.database.close()
        replay = RecoveryFixture(Path(temporary) / "replay")
        try:
            now = applications.stamp()
            replay.adapter.clock = lambda: now
            packet = {"job": replay.job(), "recovery": {"attempt": 1, "max_attempts": 3}}
            try:
                replay.adapter.call("investigate", packet)
            except applications.AdapterPending as pending:
                request_id = pending.request_id
            first = desktop_adapter.claim(replay.database, replay.root, "first-consumer", now, request_id=request_id)
            assert first is not None
            now = desktop_adapter.later(now, desktop_adapter.LEASE_SECONDS + 1)
            assert replay.adapter.cached("investigate", packet)["reason"] == "DESKTOP_INVESTIGATION_EXPIRED"
            assert desktop_adapter.claim(replay.database, replay.root, "restarted-consumer", now, request_id=request_id) is None
            expired = replay.adapter.call("investigate", packet)
            assert expired["reason"] == "DESKTOP_INVESTIGATION_EXPIRED"
            assert replay.adapter.call("investigate", packet) == expired
            assert replay.adapter.cached("investigate", packet) == expired
            assert replay.database.execute("SELECT claims FROM desktop_requests WHERE request_id=?", (request_id,)).fetchone()[0] == 1
            next_packet = {"job": replay.job(), "recovery": {"attempt": 2, "max_attempts": 3, "instruction": "Read another observed authorized source"}}
            try:
                replay.adapter.call("investigate", next_packet)
            except applications.AdapterPending as pending:
                next_id = pending.request_id
            assert next_id != request_id
            second = desktop_adapter.claim(replay.database, replay.root, "next-instruction", now, request_id=next_id)
            result = {"status": "UNRESOLVED", "reason": "No proven role comparison", "next_action": "Parent chooses next instruction",
                      "resolution": {"history": []}, "evidence": {"source": "synthetic dated observation", "observed_at": now, "verified": False}}
            desktop_adapter.complete(replay.database, dict(second, result=result), now)
            now = desktop_adapter.later(now, 90000)
            assert replay.adapter.call("investigate", next_packet) == result
            assert replay.adapter.cached("investigate", next_packet) == result
            assert desktop_adapter.claim(replay.database, replay.root, "late-consumer", now, request_id=next_id) is None
            assert replay.database.execute("SELECT sum(claims) FROM desktop_requests").fetchone()[0] == 2
            checks.append("single claim survives lost lease/restart; completed and expired results replay past TTL without new claims or freshened evidence")
        finally:
            replay.database.close()
    return {"mode": "ISOLATED_MANUAL_SYNTHETIC", "passed": len(checks), "checks": checks,
            "network_calls": 0, "production_writes": 0, "live_submissions": 0}


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
