"""Synthetic manual validation. No credentials, production state or network."""

import contextlib
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from unittest.mock import Mock, patch

import engagement
import orchestrator
import outreach
import outreach_provider
import reporting
import research_workflow
import workflow_tasks


ROOT = Path(__file__).resolve().parent
PROFILE = "https://www.linkedin.com/in/synthetic-closed/"
ALIAS = "https://www.linkedin.com/in/synthetic-alias/"
NOW = dt.datetime(2026, 9, 17, 19, tzinfo=dt.timezone.utc)
CONFIG = {"sender_id": 900001, "sender_profile": "https://www.linkedin.com/in/synthetic-sender",
          "outreach_actions_enabled": True, "outreach_mode": "guarded",
          "outreach_allowed_purposes": ["INITIAL", "FIRST_MESSAGE", "FOLLOWUP", "URGENT"],
          "outreach_history_max_age_seconds": 3600}


def rejected(operation):
    try:
        operation()
    except ValueError:
        return
    raise AssertionError("Closed recipient operation was accepted")


def action(identifier, profile=PROFILE, purpose="INITIAL"):
    return {"action_id": identifier, "profile": profile, "recipient": "Synthetic Recipient",
            "company": "Synthetic Company", "purpose": purpose, "provider": "heyreach",
            "channel": "linkedin", "conversation_id": "synthetic-room", "message": "Synthetic text",
            "verification": {"verified_at": NOW.isoformat()}}


def seed_claim(database, identifier, state):
    candidate = action(identifier)
    identity = orchestrator.profile_key(PROFILE)
    payload = json.dumps(candidate, sort_keys=True)
    payload_hash = hashlib.sha256(payload.encode()).hexdigest()
    proof = json.dumps({"checked_at": NOW.isoformat()})
    with database:
        database.execute("INSERT INTO outreach_actions VALUES (?,?,?,?,?,?,?,?)",
                         (identifier, identity, identifier, payload, payload_hash, state, NOW.isoformat(), '{"original":true}'))
        database.execute("INSERT INTO outreach_action_claims VALUES (?,?,?,?,?,?,?)",
                         (identifier, payload, payload_hash, identity, CONFIG["sender_id"], proof, NOW.isoformat()))


def delivery_snapshot(database):
    return {table: [tuple(row) for row in database.execute("SELECT * FROM " + table + " ORDER BY 1")]
            for table in ("outreach_action_claims", "outreach_attempts", "history", "engagement_events")}


def validate():
    evidence = []
    with tempfile.TemporaryDirectory(prefix="closure-manual-", dir=ROOT) as directory:
        root = Path(directory)
        (root / "state").mkdir()
        with contextlib.closing(orchestrator.connect(root)) as database:
            outreach.initialize(database)
            outreach.initialize_actions(database)
            research_workflow.initialize(database)
            research_workflow.register_alias(database, "contact", ALIAS, PROFILE, {"source": "synthetic verified alias"})
            identity = orchestrator.profile_key(PROFILE)
            candidate = dict(action("invitation"), campaign_id=900123)
            outreach.enqueue(database, candidate)
            outreach.enqueue_action(database, action("ready", ALIAS))
            with database:
                database.execute("INSERT INTO drafts VALUES (?,?,?,'HELD_VERIFICATION',?)",
                                 (identity, "Synthetic Recipient", "Synthetic Company", json.dumps(candidate)))
                task = workflow_tasks.enqueue(database, "message_writer", "ready", "synthetic", [],
                    (NOW + dt.timedelta(days=1)).isoformat(), NOW.isoformat(), contact_id=identity)
                database.execute("INSERT INTO outreach_attempts VALUES (?,900124,'UNCERTAIN_NO_RETRY',?,?)",
                                 (identity, NOW.isoformat(), '{"lost_ack":true}'))
                database.execute("INSERT INTO history VALUES (?,?,?,?,?)",
                                 (identity, "Synthetic Recipient", "Old Company", PROFILE, "original delivery evidence"))
            for state in ("CONFIRMED_SENT", "CONFIRMED_NOT_SENT", "UNCERTAIN_NO_RETRY", "ACKNOWLEDGED_UNCONFIRMED", "DISPATCHING", "REQUEST_STARTED"):
                seed_claim(database, state, state)
            before = delivery_snapshot(database)
            actions_before = [tuple(row) for row in database.execute("SELECT * FROM outreach_actions WHERE action_id<>'ready' ORDER BY action_id")]
            closed = engagement.close_outreach(database, ALIAS, "Synthetic explicit closure", NOW, source="manual synthetic decision")
            assert closed["state"] == "CLOSED"
            assert delivery_snapshot(database) == before
            assert [tuple(row) for row in database.execute("SELECT * FROM outreach_actions WHERE action_id<>'ready' ORDER BY action_id")] == actions_before
            assert database.execute("SELECT state FROM outreach_actions WHERE action_id='ready'").fetchone()[0] == "CLOSED"
            assert database.execute("SELECT state FROM outreach_queue").fetchone()[0] == "CLOSED"
            assert database.execute("SELECT state FROM drafts").fetchone()[0] == "CLOSED"
            assert database.execute("SELECT status FROM workflow_tasks WHERE task_id=?", (task,)).fetchone()[0] == "CANCELLED"
            assert engagement.close_outreach(database, PROFILE, "Repeated closure", NOW + dt.timedelta(days=10)) == closed
            evidence.append("Closure terminalizes ready work; all sent, not-sent, uncertain, acknowledged, dispatching and started delivery facts/claims/history unchanged")
            with contextlib.closing(orchestrator.connect(root)) as restarted:
                for profile in (PROFILE, ALIAS, "https://uk.linkedin.com/in/SYNTHETIC-CLOSED/?x=1"):
                    assert engagement.stop_state(restarted, profile) == "OUTREACH_CLOSED"
                    assert engagement.next_action(restarted, profile, NOW + dt.timedelta(days=365))["state"] == "CLOSED"
                late_alias = "https://linkedin.com/in/synthetic-later"
                outreach.enqueue_action(restarted, action("late-ready", late_alias))
                research_workflow.register_alias(restarted, "contact", late_alias, PROFILE, {"source": "later verified alias"})
                assert engagement.closure_status(restarted, late_alias)["state"] == "CLOSED"
                with restarted:
                    restarted.execute("DELETE FROM rw_aliases")
                archived = engagement.closure_status(restarted, late_alias)
                assert archived is not None, "Late verified alias lost closure after alias archival"
                assert (archived["reason"], archived["source"], archived["closed_at"]) == (closed["reason"], closed["source"], closed["closed_at"])
                assert restarted.execute("SELECT state FROM outreach_actions WHERE action_id='late-ready'").fetchone()[0] == "CLOSED"
                assert restarted.execute("SELECT 1 FROM suppressions WHERE identity=?", (orchestrator.profile_key(late_alias),)).fetchone()
                with contextlib.closing(orchestrator.connect(root)) as after_archival:
                    assert engagement.closure_status(after_archival, late_alias) == archived
                for kind in ("inbound", "accepted", "initial_sent"):
                    engagement.record_event(restarted, {"source": "synthetic", "source_id": kind, "profile": late_alias,
                        "kind": kind, "occurred_at": NOW.isoformat(), "observed_at": NOW.isoformat(),
                        "evidence": "synthetic independent observation", "raw": {"retained": True}, "verified": True})
                assert engagement.next_action(restarted, late_alias, NOW)["state"] == "CLOSED"
                draft = dict(action("new", late_alias), company="Different Company", recipient="Different Verified Name",
                             source_url="https://example.invalid/synthetic")
                assert orchestrator.store_research(restarted, {"companies": [], "rules": {"max_unique_contacts_per_company": 3}},
                    {"summary": "synthetic", "blockers": [], "opportunities": [], "drafts": [draft]})[0]["reason"] == "OUTREACH_CLOSED"
                rejected(lambda: outreach.enqueue(restarted, dict(draft, campaign_id=900125)))
                for purpose in ("INITIAL", "FIRST_MESSAGE", "FOLLOWUP", "URGENT", "PREPARE_CAMPAIGN"):
                    planned = dict(action("blocked-" + purpose, late_alias, purpose), ordinal=1, list_id=1, campaign_name="Synthetic")
                    rejected(lambda: outreach.enqueue_action(restarted, planned))
                    assert "OUTREACH_CLOSED" in outreach.action_gates(restarted, CONFIG, planned, {}, NOW)
                assert not outreach.claim_send(restarted, candidate)
                evidence.append("Restart, normalized URLs, aliases registered before/after closure and later archived, inbound, acceptance, observed delivery, research, all purposes and alternate campaign remain closed; late-alias queued work terminalized")
            provider_client = Mock()
            provider_client._request.side_effect = AssertionError("External mutation forbidden")
            provider = outreach_provider.Provider(database, CONFIG, provider_client)
            with patch.object(outreach_provider, "_now", return_value=NOW.isoformat()):
                rejected(lambda: provider.send_claimed("REQUEST_STARTED", CONFIG))
            provider_client._request.assert_not_called()
            adapter = outreach.HeyReachActionAdapter(transport=provider_client._request)
            rejected(lambda: adapter.execute_claimed(database, "DISPATCHING", CONFIG, NOW))
            with database:
                database.execute("INSERT INTO outreach_attempts VALUES (?,900126,'CLAIMED',?,NULL)",
                                 (orchestrator.profile_key(ALIAS), NOW.isoformat()))
            legacy = object.__new__(orchestrator.HeyReach)
            legacy._request = provider_client._request
            rejected(lambda: legacy.start_claimed_campaign(database, 900126))
            provider_client._request.assert_not_called()
            evidence.append("Legacy claimed invitation, modern adapter and final provider guard reject closure with zero external requests")
            report = reporting.snapshot(database, CONFIG, NOW.isoformat())
            assert all(row["recipient_state"] == row["workflow_state"] == "CLOSED" for row in report["outreach_closures"]["records"])
            assert any(row["state"] == "UNCERTAIN_NO_RETRY" for row in report["message_actions"]["records"])
            evidence.append("Reporting exposes closed recipient/workflow separately from uncertain delivery")
        request = root / "closure.json"
        request.write_text(json.dumps({"profile": PROFILE, "reason": "CLI decision", "source": "synthetic CLI", "closed_at": NOW.isoformat()}))
        output = io.StringIO()
        with patch.dict(os.environ, {"SIDEDOOR_HOME": str(root)}), patch.object(sys, "argv", ["orchestrator.py", "close-outreach", str(request)]), contextlib.redirect_stdout(output):
            assert orchestrator.main() == 0
        assert json.loads(output.getvalue())["reason"] == "Synthetic explicit closure"
        request.write_text(json.dumps({"profile": "https://linkedin.com/in/synthetic-cli-only", "reason": "CLI decision",
                                       "source": "synthetic CLI", "closed_at": NOW.isoformat()}))
        output = io.StringIO()
        with patch.dict(os.environ, {"SIDEDOOR_HOME": str(root)}), patch.object(sys, "argv", ["orchestrator.py", "close-outreach", str(request)]), contextlib.redirect_stdout(output):
            assert orchestrator.main() == 0
        assert json.loads(output.getvalue())["source"] == "synthetic CLI"
        with contextlib.closing(orchestrator.connect(root)) as restarted:
            assert engagement.closure_status(restarted, "https://linkedin.com/in/synthetic-cli-only")["closed_at"] == NOW.isoformat()
        evidence.append("JSON CLI executes locally and idempotently without config, snapshots, import or provider calls")
    return {"state": "PASS", "synthetic_only": True, "external_calls": 0, "checks": evidence}


if __name__ == "__main__":
    with patch.object(socket.socket, "connect", side_effect=AssertionError("No network allowed")):
        print(json.dumps(validate(), indent=2))
