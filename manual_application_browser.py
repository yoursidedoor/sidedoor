"""Manual synthetic mailbox/browser/arm walkthrough. Never starts Codex or a browser.

The user preapproved only the existing guarded arm action. Since E7.T1 (option A) the arm tool is
served by the guarded browser server (`sidedoor browser-guard`) and only when the submit session hands
it the private claim (tools.arm.approval_mode="approve"); a read-only session's server has no arm tool.
Sandbox and shell policy remain. The guard round trip runs the real guard against the fake
@playwright/mcp in tests/fake_playwright_mcp.py.
Official references checked September 17, 2026:
https://learn.chatgpt.com/docs/config-file/config-reference
https://learn.chatgpt.com/docs/extend/mcp?surface=cli
Actual Codex-to-MCP approval verification belongs to the parent live guard probe.
"""

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
from unittest.mock import patch

import application_browser as browser
import applications
import desktop_adapter
import user_profile
import paths


ROOT = Path(__file__).resolve().parent


class Fixture:
    def __init__(self, root):
        self.root = Path(root)
        (self.root / "state").mkdir(parents=True)
        self.database = sqlite3.connect(self.root / "state/orchestrator.sqlite3")
        self.database.row_factory = sqlite3.Row
        applications.initialize(self.database)
        self.config = {"application_adapter": "desktop", "applications_enabled": True,
                       "execution_ownership_verified": True, "model": "synthetic-only", "codex": sys.executable}
        resume = b"Synthetic resume, never uploaded"
        (self.root / "resume.pdf").write_bytes(resume)
        self.policy = {"preapproved_applications": True, "max_attempts": 3,
            "max_reconcile_attempts": 3, "retry_delays_seconds": [900, 3600, 7200],
            "minimum_base_salary_usd": 190000, "funding_stages": ["Series B"], "industries": ["AI"],
            "levels": ["Staff Software Engineer"],
            "resume": {"path": "resume.pdf", "sha256": hashlib.sha256(resume).hexdigest()},
            "answers": {"email": {"semantic": "contact_email", "value": "candidate@example.invalid", "source": "synthetic candidate"}}}
        (self.root / "config.json").write_text(json.dumps(self.config))
        user_profile.use_policy(self.policy, self.root / "profile-home")
        self.adapter = desktop_adapter.DesktopAdapter(self.database, self.root, self.config)
        with self.database:
            self.key = applications.ingest(self.database, {
                "company": "Synthetic Company", "role": "Staff Software Engineer",
                "url": "https://jobs.ashbyhq.com/synthetic/role-123", "requisition_id": "role-123"}, applications.stamp())
        self.actions = []

    def job(self):
        job = dict(self.database.execute("SELECT * FROM application_jobs WHERE job_key=?", (self.key,)).fetchone())
        job["candidate_email"] = "candidate@example.invalid"
        return job

    def review(self):
        source = {"source": "synthetic independent evidence", "verified": True, "observed_at": applications.stamp()}
        return dict(source, employer="synthetic company", requisition="role-123",
            official_url=json.loads(self.job()["payload"])["url"], role=json.loads(self.job()["payload"])["role"],
            candidate_email=self.job()["candidate_email"],
            official_destination_verified=True, action_scope="application_only",
            employee_count_evidence=dict(source, value="51-200"),
            facts={"open": True, "funding_stage": "Series B", "industry": "AI", "level": "Staff Software Engineer",
                   "specialization": "backend", "country": "US", "city": "Seattle", "work_mode": "remote",
                   "requirements_met": True, "authorization": "US_PERMANENT_RESIDENT_ACCEPTED",
                   "hiring_geography_includes_candidate": True, "citizenship_clearance_requirements_met": True},
            scope_fit={"level": {"in_scope": True, "reason": "fixture: a target level"}, "specialization": {"in_scope": True, "reason": "fixture: a target specialization"}},
            compensation=dict(source, currency="USD", minimum=200000, maximum=250000, period="annual_base"),
            history=dict(source, job_key=self.key, application=False, interview=False, referral="NONE"),
            questions=[{"id": "email", "semantic": "contact_email", "required": True}],
            all_required_questions_enumerated=True, form_revision="fixture-form-v1")

    def queue(self, operation):
        try:
            self.adapter.call(operation, {"job": self.job()})
        except applications.AdapterPending as pending:
            return pending.request_id
        raise AssertionError("Expected mailbox request")

    def prepare_submit(self):
        fixture = self
        class Prepare:
            can_submit = True

            def call(self, operation, payload):
                return fixture.review() if operation == "inspect" else fixture.adapter.call(operation, payload)

        applications.process(self.database, self.job(), Prepare(), self.policy, self.root, applications.stamp())
        row = self.database.execute("SELECT request_id FROM desktop_requests WHERE operation='submit'").fetchone()
        assert row is not None, dict(self.job())
        return row[0]

    def receipt(self):
        return {"status": "SUBMITTED", "source": "synthetic independent ATS", "verified": True,
                "independent_read": True, "employer": "synthetic company", "requisition": "role-123",
                "candidate_email": "candidate@example.invalid", "receipt_id": "synthetic-receipt",
                "evidence_kind": "authenticated_ats_application_record",
                "provider_message_or_application_id": "synthetic-application"}


def event(identifier, text, server="browser", tool="browser_snapshot", arguments=None):
    """One completed tool call of the guarded browser server (E7.T1) in a Codex trace."""
    return {"type": "item.completed", "item": {"type": "mcp_tool_call", "id": identifier,
        "server": server, "tool": tool, "arguments": arguments or {},
        "status": "completed", "result": {"content": [{"type": "text", "text": text}], "isError": False}}}


def write_output(directory, request, result, events=None, mutate=None):
    quote = "SYNTHETIC BROWSER OBSERVATION ONLY\n" + browser.encoded(result)
    events = events if events is not None else [event("browser-1", quote)]
    reference = events[-1]["item"]["id"]
    evidence = [{"pointer": pointer, "tool_call_id": reference, "quote": quote}
                for pointer, value in browser.leaves(result)]
    raw = {"request_id": request["request_id"], "binding": request["binding"],
           "result_json": browser.encoded(result), "evidence": evidence}
    if mutate:
        mutate(raw)
    # UTF-8 as Codex writes them (the trace is read as bytes); never the Windows locale encoding.
    (directory / "trace.jsonl").write_text("\n".join(browser.encoded(entry) for entry in events) + "\n",
                                           encoding="utf-8", newline="")
    (directory / "output.json").write_text(browser.encoded(raw), encoding="utf-8", newline="")


def request_at(directory):
    return json.loads((directory / "request.json").read_text())


def guard_roundtrip(claim_path):
    """The real `browser-guard` process with the private claim, in front of the fake @playwright/mcp:
    initialize, list (the browser tools plus arm), arm once, arm again (denied)."""
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "manual", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "arm", "arguments": {"observed_form_revision": "fixture-form-v1"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "arm", "arguments": {"observed_form_revision": "fixture-form-v1"}}},
    ]
    with tempfile.TemporaryDirectory() as scratch:
        snapshot, calls = Path(scratch) / "snapshot.yaml", Path(scratch) / "calls.jsonl"
        snapshot.write_text('- button "Submit application" [ref=e1] [cursor=pointer]\n')
        process = subprocess.Popen([sys.executable, "-B", str(ROOT / "orchestrator.py"), "browser-guard", "--operation", "submit",
                                    "--claim", str(claim_path), "--", sys.executable, str(ROOT / "tests" / "fake_playwright_mcp.py"),
                                    str(snapshot), str(calls)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        responses = []
        for message in messages:
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()
            if "id" in message:
                responses.append(json.loads(process.stdout.readline()))
        process.stdin.close()
        stderr = process.stderr.read()
        process.wait(timeout=10)
        stdout = json.dumps(responses)
    assert [response["id"] for response in responses] == [1, 2, 3, 4]
    names = [tool["name"] for tool in responses[1]["result"]["tools"]]
    assert "arm" in names and "browser_click" in names and "wait_for_human_check" not in names, names
    assert responses[2]["result"]["isError"] is False
    armed = json.loads(responses[2]["result"]["content"][0]["text"])
    assert armed["may_submit_once"] is True
    assert responses[3]["result"]["isError"] is True
    private = json.loads(Path(claim_path).read_text())
    assert private["envelope"]["token"] not in stdout + stderr
    return responses


def run(output_root):
    checks = []
    fixtures = []
    def fixture(name):
        created = Fixture(output_root / name)
        fixtures.append(created)
        return created

    try:
        primary = fixture("roundtrip")
        for private_claim in (None, "/synthetic/private/claim.json"):
            configuration = dict(primary.config)
            if private_claim:
                configuration["_application_guard_claim"] = private_claim
            command = browser.command(configuration, primary.root)
            options = [command[index + 1] for index, argument in enumerate(command[:-1]) if argument == "-c"]
            guard = tomllib.loads("\n".join(option for option in options if option.startswith("mcp_servers.browser.")))["mcp_servers"]["browser"]
            assert guard["command"] == sys.executable and "browser-guard" in guard["args"]
            assert guard["enabled"] is True
            assert ("--claim" in guard["args"]) is bool(private_claim)
            assert ("arm" in guard["tools"]) is bool(private_claim)
            if private_claim:
                assert guard["tools"]["arm"] == {"approval_mode": "approve"}
            assert "--ignore-user-config" in command
            assert "features.shell_tool=false" in options and "features.unified_exec=false" in options
            assert 'approval_policy="never"' in options
            assert command[command.index("--sandbox") + 1] == "read-only"
        checks.append("read-only and submit both define valid guard transport; read-only guard disabled and shell unavailable")
        checks.append("only private submit arm has the documented per-tool approval; sandbox and global approval policy unchanged")
        def inspect(config, prompt, directory, timeout):
            request = request_at(directory)
            assert 0 < timeout < 240
            assert "_application_guard_claim" not in config
            assert request["context"]["policy"]["answers"]["email"]["semantic"] == "contact_email"
            assert request["context"]["local_history"] == []
            write_output(directory, request, primary.review())
        request_id = primary.queue("inspect")
        result = browser.consume(primary.root, primary.database, primary.config, request_id, inspect)
        assert result["state"] == "COMPLETE", result
        assert primary.adapter.call("inspect", {"job": primary.job()})["facts"]["open"] is True
        assert browser.consume(primary.root, primary.database, primary.config, request_id, inspect)["state"] == "NOT_CLAIMED"
        checks.append("inspect consumes exact request; replay cannot execute again; context saved")

        request_id = primary.prepare_submit()
        private_tokens = []
        def submit(config, prompt, directory, timeout):
            request = request_at(directory)
            private = json.loads(Path(config["_application_guard_claim"]).read_text())
            private_tokens.append(private["envelope"]["token"])
            assert private_tokens[-1] not in prompt
            responses = guard_roundtrip(config["_application_guard_claim"])
            (directory / "manual-mcp-protocol.json").write_text(json.dumps(responses, indent=2))
            primary.actions.append("guard-armed")
            result = {"acknowledged": True}
            quote = "SYNTHETIC BROWSER OBSERVATION ONLY\n" + browser.encoded(result)
            events = [event("form", "Synthetic current form fixture-form-v1"),
                      event("arm", responses[2]["result"]["content"][0]["text"], "browser", "arm", {"observed_form_revision": "fixture-form-v1"}),
                      event("submit", quote, tool="browser_click", arguments={"target": "e1", "element": "Submit application"})]
            primary.actions.append("mock-submit")
            write_output(directory, request, result, events)
        result = browser.consume(primary.root, primary.database, primary.config, request_id, submit)
        assert result["state"] == "COMPLETE", result
        saved = json.loads(primary.database.execute("SELECT result FROM desktop_requests WHERE request_id=?", (request_id,)).fetchone()[0])
        assert saved["status"] == "UNKNOWN" and saved["requires_independent_receipt"] is True
        assert primary.actions == ["guard-armed", "mock-submit"]
        for path in (primary.root / "reports").rglob("*"):
            if path.is_file():
                assert private_tokens[0].encode() not in path.read_bytes(), str(path)
        checks.append("real browser-guard subprocess initializes/lists the browser tools plus arm/arms once/rejects replay; token absent in artifacts")

        request_id = primary.queue("reconcile")
        result = browser.consume(primary.root, primary.database, primary.config, request_id,
            lambda config, prompt, directory, timeout: write_output(directory, request_at(directory), primary.receipt()))
        assert result["state"] == "COMPLETE", result
        receipt = primary.adapter.call("reconcile", {"job": primary.job()})
        assert applications.verified_receipt(receipt, primary.job(), applications.stamp())
        assert receipt["attempt_id"] and receipt["job_key"] == primary.key
        health = [json.loads(row[0]) for row in primary.database.execute("SELECT detail FROM desktop_events WHERE event='BROWSER_CONSUMER_COMPLETED'")]
        assert len(health) == 3 and all(item["execution_context"] == "mock" for item in health)
        assert health[0]["receipt_verified"] is False and health[-1]["receipt_verified"] is True
        checks.append("independent reconcile receipt binds account/role/attempt; health marked mock")

        mail = fixture("email-receipt")
        request_id = mail.prepare_submit()
        result = browser.consume(mail.root, mail.database, mail.config, request_id, submit)
        assert result["state"] == "COMPLETE", result
        request_id = mail.queue("verify")
        email_receipt = dict(primary.receipt(), evidence_kind="employer_confirmation_email",
            mailbox_email="candidate@example.invalid", recipient="candidate@example.invalid",
            sender="applications@synthetic.example.invalid", sender_authenticated=True,
            received_at=applications.stamp(), source_url="https://mail.google.com/mail/u/0/#all/synthetic-message",
            provider_message_or_application_id="synthetic-message", receipt_id="synthetic-message")
        def email_executor(config, prompt, directory, timeout):
            request = request_at(directory)
            attempt = request["context"]["current_attempt"]
            assert attempt["attempt_id"] == request["payload"]["attempt_id"] and attempt["job_key"] == mail.key
            assert attempt["evidence"]["review"] and attempt["evidence"]["answers"]
            assert attempt["evidence"]["resume_sha256"] == mail.policy["resume"]["sha256"]
            assert attempt["prior_committed_submit_result"]["status"] == "UNKNOWN"
            write_output(directory, request, email_receipt)
        result = browser.consume(mail.root, mail.database, mail.config, request_id, email_executor)
        assert result["state"] == "COMPLETE", result
        receipt = mail.adapter.call("verify", {"job": mail.job()})
        assert receipt["evidence_kind"] == "employer_confirmation_email"
        assert applications.verified_receipt(receipt, mail.job(), applications.stamp())
        assert receipt["attempt_id"] and receipt["mailbox_email"] == receipt["candidate_email"]
        checks.append("independent employer email receipt binds observed sender/mailbox/recipient/message and post-attempt time through the same browser")

        for name, mutation in (
            ("binding", lambda raw: raw.update(binding="wrong")),
            ("quote", lambda raw: raw["evidence"][0].update(quote="invented quote")),
            ("unsourced", lambda raw: raw.update(evidence=[])),
        ):
            broken = fixture(name)
            request_id = broken.queue("inspect")
            result = browser.consume(broken.root, broken.database, broken.config, request_id,
                lambda config, prompt, directory, timeout: write_output(directory, request_at(directory), broken.review(), mutate=mutation))
            assert result["state"] == "FAILED" and result["failure_committed"], result
            checks.append(name + " rejection commits explicit read failure")

        held = fixture("auth")
        request_id = held.queue("inspect")
        result = browser.consume(held.root, held.database, held.config, request_id,
            lambda config, prompt, directory, timeout: write_output(directory, request_at(directory), {
                "blocked": {"code": "AUTHENTICATION_REQUIRED", "next_action": "Restore existing ATS session"}}))
        assert result["state"] == "COMPLETE", result
        checks.append("authentication blocker completes without inventing eligibility")

        incomplete = fixture("incomplete-read")
        request_id = incomplete.queue("inspect")
        partial = {"blocked": {"code": "INSPECTION_INCOMPLETE", "next_action":
            "The company page at https://example.invalid/company did not load; read it to resolve missing headcount"},
            "partial_review": {"employer": "synthetic company", "requisition": "role-123",
                "facts": {"open": True, "funding_stage": None},
                "questions": [{"id": "email", "semantic": "contact_email", "required": True}]}}
        result = browser.consume(incomplete.root, incomplete.database, incomplete.config, request_id,
            lambda config, prompt, directory, timeout: write_output(directory, request_at(directory), partial))
        assert result["state"] == "COMPLETE", result
        saved = incomplete.adapter.call("inspect", {"job": incomplete.job()})
        assert saved["blocked"]["code"] == "INSPECTION_INCOMPLETE"
        assert saved["partial_review"] == partial["partial_review"]
        assert any(entry["pointer"] == "/partial_review/facts/open" for entry in saved["browser_evidence"])
        checks.append("incomplete-read blocker retains exact code, next action, sourced partial role/form and unknowns")

        unsourced_partial = fixture("unsourced-partial")
        request_id = unsourced_partial.queue("inspect")
        result = browser.consume(unsourced_partial.root, unsourced_partial.database, unsourced_partial.config, request_id,
            lambda config, prompt, directory, timeout: write_output(directory, request_at(directory), partial,
                mutate=lambda raw: raw.update(evidence=[])))
        assert result["state"] == "FAILED" and result["code"] == "APPLICATION_UNSOURCED_FIELD", result
        checks.append("blocked status cannot conceal unsourced partial facts")

        for name, complete_form in (("partial-unknown-completeness", False), ("unsourced-complete-form", True)):
            partial_fixture = fixture(name)
            request_id = partial_fixture.queue("inspect")
            partial_control = {"blocked": {"code": "INSPECTION_INCOMPLETE", "next_action": "Finish the read-only form review"},
                               "partial_review": {"action_scope": "application_only", "all_required_questions_enumerated": complete_form}}
            result = browser.consume(partial_fixture.root, partial_fixture.database, partial_fixture.config, request_id,
                lambda config, prompt, directory, timeout: write_output(directory, request_at(directory), partial_control,
                    mutate=lambda raw: raw.update(evidence=[])))
            assert result["state"] == ("FAILED" if complete_form else "COMPLETE"), result
            checks.append(name + " treats false completeness as uncertainty; true completeness still requires exact source evidence")

        duplicate = fixture("repeated-quote")
        request_id = duplicate.queue("inspect")
        def repeated_executor(config, prompt, directory, timeout):
            partial = {"employer": "synthetic company", "verified": False,
                "blocker": {"code": "INSPECTION_INCOMPLETE", "next_action": "Resolve missing company evidence"}}
            quote = "SYNTHETIC BROWSER OBSERVATION ONLY\n" + browser.encoded(partial)
            def cite(raw):
                raw["evidence"] = [{"pointer": "/employer", "tool_call_id": None, "quote": quote}]
            write_output(directory, request_at(directory), partial,
                [event("role", quote), event("form", quote)], mutate=cite)
        result = browser.consume(duplicate.root, duplicate.database, duplicate.config, request_id, repeated_executor)
        assert result["state"] == "COMPLETE", result
        saved = duplicate.adapter.call("inspect", {"job": duplicate.job()})
        assert saved["browser_evidence"][0]["matching_tool_call_ids"] == ["role", "form"]
        assert saved["employer"] == "synthetic company"
        checks.append("exact repeated quotes preserve all successful source IDs without losing facts")

        for name, partial, pointer, accepted in (
            ("failed-tool-negative-verification", {"verified": False}, "/partial_review/verified", True),
            ("failed-tool-positive-verification", {"verified": True}, "/partial_review/verified", False),
            ("failed-tool-history-absence", {"history": {"application": False}}, "/partial_review/history/application", False),
        ):
            diagnostic = fixture(name)
            request_id = diagnostic.queue("inspect")
            def diagnostic_executor(config, prompt, directory, timeout):
                quote = "Browser use could not load https://example.invalid for this request."
                failed = event("failed-browser", quote)
                failed["item"]["status"] = "failed"
                value = {"blocked": {"code": "INSPECTION_INCOMPLETE", "next_action": "Finish the read-only form review"}, "partial_review": partial}
                write_output(directory, request_at(directory), value, [failed], mutate=lambda raw: raw.update(
                    evidence=[{"pointer": pointer, "tool_call_id": None, "quote": quote}]))
            result = browser.consume(diagnostic.root, diagnostic.database, diagnostic.config, request_id, diagnostic_executor)
            assert result["state"] == ("COMPLETE" if accepted else "FAILED"), result
            if accepted:
                saved = diagnostic.adapter.call("inspect", {"job": diagnostic.job()})
                assert saved["browser_evidence"][0]["source_kind"] == "failed_tool_diagnostic"
                assert saved["observed_browser_calls"] == 0
            checks.append(name + " accepts failed-tool evidence only for negative verification metadata")

        denied = fixture("guard-denied")
        request_id = denied.prepare_submit()
        def denied_executor(config, prompt, directory, timeout):
            with denied.database:
                denied.database.execute("INSERT INTO application_history VALUES (?,?,?,?)", (
                    "synthetic company", "role-123", "APPLICATION", "Synthetic new independent duplicate"))
            binding = browser.ArmBinding(config["_application_guard_claim"])
            try:
                binding.arm({"observed_form_revision": "fixture-form-v1"})
            except ValueError:
                pass
            else:
                raise AssertionError("Final guard must recheck fresh duplicate history")
            write_output(directory, request_at(directory), {"blocked": {
                "code": "APPLICATION_GUARD_DENIED", "next_action": "Reconcile new duplicate evidence"}})
        result = browser.consume(denied.root, denied.database, denied.config, request_id, denied_executor)
        assert result["state"] in {"UNKNOWN", "COMPLETE"}, result
        assert denied.database.execute("SELECT armed_at FROM desktop_requests WHERE request_id=?", (request_id,)).fetchone()[0] is None
        checks.append("final guard rejects newly discovered duplicate; no simulated submit")

        lost = fixture("lost-response")
        request_id = lost.prepare_submit()
        def timeout_executor(config, prompt, directory, timeout):
            browser.ArmBinding(config["_application_guard_claim"]).arm({"observed_form_revision": "fixture-form-v1"})
            (directory / "trace.jsonl").write_text("")
            raise TimeoutError("Synthetic loss after arm")
        result = browser.consume(lost.root, lost.database, lost.config, request_id, timeout_executor)
        assert result["state"] == "UNKNOWN" and result["requires_reconciliation"], result
        saved = json.loads(lost.database.execute("SELECT result FROM desktop_requests WHERE request_id=?", (request_id,)).fetchone()[0])
        assert saved["status"] == "UNKNOWN"
        assert browser.consume(lost.root, lost.database, lost.config, request_id, timeout_executor)["state"] == "NOT_CLAIMED"
        checks.append("lost response after arm remains UNKNOWN; no blind retry")

        scoped = fixture("context-citation")
        request_id = scoped.queue("inspect")
        def context_executor(config, prompt, directory, timeout):
            def cite(raw):
                raw["evidence"] = [{"pointer": "/candidate_fact", "tool_call_id": "context:/context/policy/answers/email/value", "quote": "candidate@example.invalid"}]
            write_output(directory, request_at(directory), {"verified": False, "candidate_fact": "candidate@example.invalid"}, mutate=cite)
        result = browser.consume(scoped.root, scoped.database, scoped.config, request_id, context_executor)
        assert result["state"] == "COMPLETE", result
        checks.append("saved verified candidate facts can be cited without pretending browser supplied them")

        dated = fixture("dated-context")
        (dated.root / "research-source-observations.json").write_text(json.dumps({"observations": [{
            "company": "Synthetic Company", "source": "https://example.invalid/synthetic-evidence",
            "observed_at": "2020-01-01T00:00:00+00:00", "excerpt": "Synthetic headcount 51-200"}]}))
        request_id = dated.queue("inspect")
        def dated_executor(config, prompt, directory, timeout):
            def cite(raw):
                raw["evidence"] = [{"pointer": "/employee_count_evidence/value", "tool_call_id": "context:/context/research_sources/0/excerpt", "quote": "Synthetic headcount 51-200"}]
            write_output(directory, request_at(directory), {"verified": False, "employee_count_evidence": {
                "value": "51-200", "verified": True, "observed_at": applications.stamp()}}, mutate=cite)
        result = browser.consume(dated.root, dated.database, dated.config, request_id, dated_executor)
        assert result["state"] == "COMPLETE", result
        saved = json.loads(dated.database.execute("SELECT result FROM desktop_requests WHERE request_id=?", (request_id,)).fetchone()[0])
        assert saved["employee_count_evidence"]["observed_at"] == "2020-01-01T00:00:00+00:00"
        assert not applications.fresh(saved["employee_count_evidence"], applications.stamp())
        checks.append("saved dated context retains original timestamp; stale headcount cannot become fresh")

        for name, source_date, candidate_only in (
            ("stale-root-facts", "2020-01-01T00:00:00+00:00", False),
            ("undated-root-facts", None, False),
            ("static-candidate-facts", None, True),
        ):
            freshness = fixture(name)
            review = freshness.review()
            if not candidate_only:
                source = {"company": "Synthetic Company", "source": "https://example.invalid/dated-role",
                          "observed_at": source_date or applications.stamp(), "excerpt": browser.encoded(review["facts"])}
                (freshness.root / "research-source-observations.json").write_text(json.dumps({"observations": [source]}))
            request_id = freshness.queue("inspect")
            def freshness_executor(config, prompt, directory, timeout):
                request = request_at(directory)
                if name == "undated-root-facts":
                    request["context"]["research_sources"][0].pop("observed_at")
                    (directory / "request.json").write_text(browser.encoded(request))
                def cite(raw):
                    for entry in raw["evidence"]:
                        if candidate_only and entry["pointer"] == "/facts/requirements_met":
                            entry.update(tool_call_id="context:/context/policy/answers/email/source", quote="synthetic candidate")
                        elif not candidate_only and entry["pointer"].startswith("/facts/"):
                            entry.update(tool_call_id="context:/context/research_sources/0/excerpt", quote=source["excerpt"])
                write_output(directory, request, review, mutate=cite)
                if name == "undated-root-facts":
                    raw = json.loads((directory / "output.json").read_text(encoding="utf-8"))
                    normalized = browser.normalize(request, raw, (directory / "trace.jsonl").read_bytes(), applications.stamp(), directory / "trace.jsonl")
                    # Recorded, never a label (owner, 2026-10-02): undated facts leave `verified` alone.
                    assert normalized["verified"] is not False and normalized["undated_eligibility_fields"]
            result = browser.consume(freshness.root, freshness.database, freshness.config, request_id, freshness_executor)
            assert result["state"] == "COMPLETE", result
            saved = freshness.adapter.call("inspect", {"job": freshness.job()})
            if source_date:
                assert saved["observed_at"] == source_date and saved["browser_observed_at"] != source_date
                assert applications.assess(saved, freshness.job(), freshness.policy, applications.stamp())[1] == "ELIGIBILITY_UNVERIFIED"
            if candidate_only:
                assert applications.assess(saved, freshness.job(), freshness.policy, applications.stamp()) is None
            checks.append(name + " preserves root source age without pretending static candidate evidence was newly observed")

        for name, events in (("tool-limit", [event(str(index), "mock observation") for index in range(41)]),
                             ("forbidden-tool", [event("forbidden", "mock", "other", "execute")])):
            bounded = fixture(name)
            request_id = bounded.queue("verify")
            result = browser.consume(bounded.root, bounded.database, bounded.config, request_id,
                lambda config, prompt, directory, timeout: write_output(directory, request_at(directory), {"status": "UNKNOWN"}, events))
            assert result["state"] == "FAILED" and result["failure_committed"], result
            checks.append(name + " is rejected")

        for name, code, timeout, expected in (
            ("executor-timeout", "import time; time.sleep(30)", 0.2, TimeoutError),
            ("executor-tool-bound", "import json,time; print(" + repr(json.dumps(event("excess", "mock"))) + ",flush=True); time.sleep(30)", 2, ValueError),
        ):
            directory = output_root / name
            directory.mkdir()
            started = time.monotonic()
            with patch.object(browser, "command", return_value=[sys.executable, "-B", "-u", "-c", code]):
                try:
                    browser.execute({"_application_max_tools": 0}, "Synthetic executor input", directory, timeout)
                except expected:
                    pass
                else:
                    raise AssertionError("Expected bounded executor failure")
            assert time.monotonic() - started < 5
            checks.append(name + " terminates actual synthetic subprocess promptly")
        summary = {"state": "PASS", "evidence_level": "MOCK_TESTED", "external_actions": 0,
                   "codex_runs": 0, "browser_runs": 0, "checks": checks, "root": str(output_root)}
        (output_root / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        return summary
    finally:
        for item in fixtures:
            item.database.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--replay", type=Path, help="Normalize saved read-only artifacts offline without touching the mailbox")
    args = parser.parse_args()
    reports = paths.reports() / "manual-application-browser"
    reports.mkdir(parents=True, exist_ok=True)
    output_root = args.output_root or Path(tempfile.mkdtemp(prefix="run-", dir=reports))
    output_root.mkdir(parents=True, exist_ok=True)
    with patch.object(socket.socket, "connect", side_effect=AssertionError("Manual walkthrough forbids network")):
        if args.replay:
            directory = args.replay.resolve()
            request = json.loads((directory / "request.json").read_text())
            assert request["payload"]["operation"] in desktop_adapter.READ_OPERATIONS
            raw = json.loads((directory / "output.json").read_text(encoding="utf-8"))
            trace = (directory / "trace.jsonl").read_bytes()
            artifact_time = dt.datetime.fromtimestamp((directory / "output.json").stat().st_mtime, dt.timezone.utc).isoformat()
            normalized = browser.normalize(request, raw, trace, artifact_time, directory / "trace.jsonl")
            (output_root / "replayed-result.json").write_text(browser.encoded(normalized))
            summary = {"state": "PASS", "evidence_level": "SAVED_TRACE_REPLAY", "new_browser_calls": 0,
                "database_writes": 0, "original_trace_sha256": hashlib.sha256(trace).hexdigest(),
                "original_directory": str(directory), "observed_at_basis": "original output artifact modification time",
                "observed_browser_calls": normalized["observed_browser_calls"],
                "repeated_quote_entries": sum(len(entry.get("matching_tool_call_ids", [])) > 1 for entry in normalized["browser_evidence"]),
                "blocker": normalized.get("blocked", {}).get("code"), "root": str(output_root)}
            (output_root / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        else:
            summary = run(output_root)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
