"""Offline manual champion handoff acceptance through the existing REPLY sender."""

import contextlib
import datetime as dt
import hashlib
import json
import sqlite3
import sys
import traceback
from unittest.mock import patch

import applications
import conversion
import engagement
import manual_reply_execution as reply_manual
import notifications
import orchestrator
import pipeline_routing as routing
import referrals
import reply_resume
import reply_worker
import research_workflow
from manual_reply_execution import BASE, OTHER_PROFILE, PROFILE, ROOT, Fixture, grounded_once, isolated_boundaries, require
from test_pipeline_routing import opportunity


DEADLINE = (BASE + dt.timedelta(hours=12)).isoformat()
NEXT_ACTION = "Reconcile the advocate's referral progress at the original deadline."
RESUME_QUOTES = [
    "so a pallet scanned on the dock showed up on store dashboards within seconds.",
    "bringing typical pull request\n       checks from twenty minutes to under six.",
    "Designed a sharded job scheduler with lease-based ownership that let route planning",
]
ROLE_QUOTES = ["Reliable distributed APIs"]
SUMMARY = ("Alex Example for Staff Engineer, Platform: Reliable distributed APIs. "
           "Relevant resume evidence: " + " / ".join(RESUME_QUOTES) +
           " Referral agreed; submission pending.")


class ChampionFixture(Fixture):
    def __init__(self):
        super().__init__()
        self.config["conversion"] = {"champion_handoff_enabled": True}
        self.decisions = {}
        self.jobs = {}

    def seed(self, profile=PROFILE, source_id="message-1", thread="thread-1", unknowns=None,
             body="Absolutely, I can refer you for req-1. Please send a forwardable summary.",
             label="AGREEMENT", association=True, handoff=True, stale_role=False,
             flags=None, speaker_context=None, linked=True):
        requisition = "req-1" if profile == PROFILE else "req-2"
        role = opportunity(requisition)
        person = role["people"][0]
        person["profile"] = profile
        for evidence in person["evidence"]:
            if evidence["field"] == "identity":
                evidence.update(value=profile, source=profile, excerpt=profile)
            if evidence["field"] == "role_association":
                evidence["value"]["profile"] = profile
        if not association:
            person["evidence"] = [item for item in person["evidence"] if item["field"] != "role_association"]
        # A contact is linked to a role by being on its people list (owner, 2026-09-24:
        # contacts are discovered per company and the model judges the rest). Dropping only the
        # role_association evidence no longer unlinks them, so an unlinked contact is one the
        # research never placed on this role at all.
        if not linked:
            role["people"] = []
        if stale_role:
            for evidence in role["evidence"]:
                evidence["observed_at"] = (BASE - dt.timedelta(days=30)).isoformat()
        key = applications.ingest(self.database, role, self.now.isoformat())
        research_workflow.process_research(self.database, {
            "research_status": "COMPLETE", "summary": "Offline champion research fixture",
            "opportunities": [role], "drafts": [],
        }, now=self.now.isoformat())
        self.jobs[source_id] = key
        self.decisions[source_id] = {"label": label, "handoff": handoff}
        if speaker_context is not None:
            self.decisions[source_id]["speaker_context"] = speaker_context
        original_message = reply_manual.inbound_message

        def provider_message(text, identifier):
            event = original_message(text, identifier)
            for record in (event["raw"], event["inbox"]):
                record.update(flags or {})
            return event

        with patch.object(reply_manual, "inbound_message", side_effect=provider_message):
            return super().seed(profile, source_id, thread, unknowns, body)

    def execute(self, config, prompt, schema, timeout):
        packet = json.loads(prompt.split("UNTRUSTED_SOURCE_JSON:\n", 1)[1])
        if "source_snapshot" not in packet:
            return super().execute(config, prompt, schema, timeout)
        self.inferences.append(packet)
        inbox = json.loads(packet["source_snapshot"]["payload"])["inbox"]
        source_id = inbox["message_id"]
        decision = self.decisions[source_id]
        body = inbox["text"]
        quote = {"start": 0, "end": len(body), "text": body}
        role_reference = self.jobs[source_id] if packet["job_scope"] else None
        resume_quotes = decision.get("resume_quotes", RESUME_QUOTES)
        role_quotes = decision.get("role_quotes", ROLE_QUOTES)
        return json.dumps({
            "output_version": reply_worker.VERSION, "source_sha256": packet["source_sha256"],
            "label": decision["label"], "literal_quote": body, "quote_span": quote,
            "role_reference": role_reference, "reason": "Offline source-bound advocate interpretation.",
            "unknown_factual_requirements": self.unknowns.get(source_id, []),
            "reply_proposal": {"text": SUMMARY if decision["handoff"] else "Thank you for offering to help.",
                               "basis_quote": quote},
            "champion_handoff": {"job_key": self.jobs[source_id], "resume_quotes": resume_quotes,
                                 "role_quotes": role_quotes} if decision["handoff"] else None,
            "next_action": NEXT_ACTION, "review_at": decision.get("review_at", DEADLINE),
            **({"speaker_context": decision["speaker_context"]} if "speaker_context" in decision else {}),
        })

    def cycle(self, seconds=0):
        self.now += dt.timedelta(seconds=seconds)
        with self.clock(), contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(orchestrator, "ROOT", self.root))
            stack.enter_context(patch.object(orchestrator, "HeyReach", return_value=self))
            stack.enter_context(patch.object(reply_worker, "execute_text", side_effect=self.execute))
            stack.enter_context(patch.object(orchestrator.decision_transport, "cycle", return_value={"state": "MOCK_UNRELATED"}))
            stack.enter_context(patch.object(orchestrator, "draft_promotion_cycle", return_value={"state": "MOCK_UNRELATED"}))
            stack.enter_context(patch.object(notifications.Telegram, "send", side_effect=notifications.Unavailable("Offline notification failure")))
            # Telegram configured but unavailable (without one, the dashboard transport shows the row, E6.T2).
            stack.enter_context(patch.object(notifications, "telegram_configured", return_value=True))
            result = orchestrator.service_cycle(self.root, self.config, self.database)
        self.cycles.append(result)
        require("reply_execution" in result, "Scheduled entry point omitted REPLY execution")
        require(result["reply_execution"].get("state") != "FAILED", "Reply scheduler failed: " + repr(result))
        return result

    def records(self, table):
        if table == "referrals":
            referrals.initialize(self.database)
        return [dict(row) for row in self.database.execute("SELECT * FROM " + table)]

    def delivery_report(self):
        result = self.cycles[-1]["reply_execution"]
        require("champion_handoffs" in result, "Scheduled reply_execution_cycle omitted champion_handoffs")
        return result["champion_handoffs"]

    def snapshot(self):
        return {**super().snapshot(), **{table: self.records(table) for table in (
            "conversion_summaries", "conversion_tasks", "referrals", "conversion_events",
            "conversion_outcomes", "application_history", "workflow_tasks",
            "notification_outbox", "reply_worker_attempts")}}


def assert_single_handoff(fixture):
    require(len(fixture.requests) == 1, "Expected exactly one guarded send; classification=" +
            repr(fixture.cycles[-1].get("reply_classification")) + "; promotion=" +
            repr(fixture.cycles[-1]["reply_execution"].get("promotion")))
    actions = fixture.actions()
    require(len(actions) == 1, "Champion used another action/slot")
    payload = json.loads(actions[0]["payload"])
    require(payload["purpose"] == "REPLY", "Champion bypassed the existing REPLY action")
    require(payload["message"] == SUMMARY, "Forwardable AI proposal was changed or acknowledgment sent instead")
    require(payload["champion_job_key"] == fixture.jobs["message-1"], "Champion job binding missing")
    require(payload["champion_deadline"] == DEADLINE and payload["champion_next_action"] == NEXT_ACTION,
            "Champion lost original deadline or next action")
    summaries = fixture.records("conversion_summaries")
    require(len(summaries) == 1 and summaries[0]["summary_id"] == payload["champion_summary_id"],
            "Summary version is missing or duplicated")
    require(summaries[0]["body"] == SUMMARY, "Stored summary differs from the sent proposal")
    tasks = [item for item in fixture.records("conversion_tasks") if item["kind"] == "GUARDED_CHAMPION_DELIVERY"]
    require(len(tasks) == 1 and actions[0]["action_id"] in tasks[0]["payload"], "Delivery task not bound to existing REPLY")
    return actions[0]


def success(fixture, provider_flags=False):
    fixture.seed(**({"flags": {"quoted": None, "forwarded": None}, "speaker_context": "DIRECT_SPEAKER"}
                    if provider_flags else {}))
    fixture.cycle()
    action = assert_single_handoff(fixture)
    require(action["state"] == "ACKNOWLEDGED_UNCONFIRMED", "Acknowledgment became delivery confirmation")
    initial_report = json.dumps(fixture.delivery_report())
    require("ACKNOWLEDGED_UNCONFIRMED" in initial_report, "Report hid acknowledged/unconfirmed delivery")
    saved = fixture.records("conversion_summaries")
    # The agreement is the person's referral record (owner, 2026-09-25), not a per-job hold.
    referral = fixture.records("referrals")
    require(len(referral) == 1 and referral[0]["state"] == "AGREED", "No source-bound person referral agreement")
    require(referral[0]["review_at"] == DEADLINE and referral[0]["next_action"] == NEXT_ACTION,
            "Person referral lost review date/next action")
    require(not any(item["kind"].startswith("REFERRAL_") for item in fixture.records("conversion_events")),
            "Referral was recorded against a job")
    fixture.cycle(seconds=1)
    require(fixture.actions()[0]["state"] == "CONFIRMED_SENT", "Independent receipt did not confirm delivery")
    require("CONFIRMED_SENT" in json.dumps(fixture.delivery_report()), "Delivery report did not reconcile")
    require(fixture.records("referrals") == referral, "Summary delivery changed the person referral")
    require(not fixture.records("conversion_outcomes"), "Summary delivery invented an application/referral outcome")
    try:
        fixture.database.execute("UPDATE conversion_summaries SET body='mutated'")
    except sqlite3.IntegrityError:
        fixture.database.rollback()
    else:
        raise AssertionError("Champion summary is mutable")
    fixture.restart()
    fixture.cycle(seconds=1)
    assert_single_handoff(fixture)
    require(fixture.records("conversion_summaries") == saved, "Restart changed immutable summary")
    require(len(fixture.snapshot()["claims"]) == 1, "Restart duplicated durable claim")
    designated = reply_resume.context()
    phases = set()
    for packet in fixture.inferences:
        resume = packet.get("candidate_resume") or packet.get("reply_context", {}).get("candidate_resume")
        require(resume == designated and resume["page_count"] == 5, "Exact five-page resume missing")
        phases.add(packet.get("phase", "CLASSIFICATION"))
        if "source_snapshot" in packet:
            if provider_flags:
                inbox = json.loads(packet["source_snapshot"]["payload"])["inbox"]
                raw = json.loads(packet["source_snapshot"]["raw"])
                require(all(inbox[flag] is None and raw[flag] is None for flag in ("quoted", "forwarded")),
                        "Provider null quote flags were forged into false")
            role = packet["job_scope"][0].get("role_context")
            require(role and role.get("facts") and role.get("evidence_id"), "Fresh sourced role_context missing")
    require({"CLASSIFICATION", "INSPECTION", "RECONCILIATION"} <= phases, "Missing actual model boundary")


def blocked(fixture, variation):
    options = {}
    if variation in {"AGREEMENT_INTENT", "ROLE_ASSOCIATION", "CANDIDATE_FACTS"}:
        options["unknowns"] = [variation]
    elif variation == "decline":
        options.update(label="DECLINE", body="Sorry, I cannot refer you.")
    elif variation == "unbound":
        options["linked"] = False
    elif variation == "stale_role":
        options["stale_role"] = True
    elif variation == "no_agreement":
        options.update(label="QUESTION", body="What is your experience with reliable APIs?")
    elif variation.startswith("provider_"):
        options.update(flags={"quoted": None, "forwarded": None}, speaker_context="DIRECT_SPEAKER")
        if variation == "provider_unknown_speaker":
            options["speaker_context"] = "UNKNOWN"
        elif variation == "provider_false_flags_unknown_speaker":
            options.update(flags={"quoted": False, "forwarded": False}, speaker_context="UNKNOWN")
        elif variation == "provider_quoted_speaker":
            options["speaker_context"] = "QUOTED_OR_FORWARDED"
        elif variation == "provider_missing_speaker":
            options["speaker_context"] = None
        elif variation == "provider_unknown_intent":
            options["unknowns"] = ["AGREEMENT_INTENT"]
        elif variation == "provider_known_quoted":
            options["flags"]["quoted"] = True
        elif variation == "provider_known_forwarded":
            options["flags"]["forwarded"] = True
    fixture.seed(**options)
    if variation == "disabled":
        fixture.config["conversion"]["champion_handoff_enabled"] = False
    if variation == "deadline_due":
        fixture.decisions["message-1"]["review_at"] = BASE.isoformat()
    fixture.cycle()
    if variation.startswith("provider_"):
        require(not any(item["blocker"] == "CLASSIFICATION_OUTPUT_SCHEMA_INVALID"
                        for item in fixture.records("reply_worker_attempts")),
                "Provider scenario blocked only because speaker_context schema is not integrated")
    require(not fixture.requests, variation + " reached shared sender")
    require(not fixture.records("conversion_summaries"), variation + " generated an unsupported champion summary")
    fixture.restart()
    fixture.cycle(seconds=1)
    require(not fixture.requests, variation + " escaped blocker after restart")


def final_guard(fixture, variation):
    fixture.seed()

    def change_after_inspection():
        if variation == "closed":
            engagement.close_outreach(fixture.database, PROFILE, "Offline recipient closure", fixture.now)
        elif variation == "revoked":
            fixture.config["conversion"]["champion_handoff_enabled"] = False
        elif variation == "agreement_revoked":
            # A later message from the same person declining replaces the agreement.
            contact = fixture.records("referrals")[0]["contact_id"]
            with fixture.database:
                referrals.record(fixture.database, contact, "DECLINED", "offline-later-decline",
                                 "Sorry, I can no longer refer you.", None, None, fixture.now.isoformat(), {})
        elif variation == "role_closed":
            with fixture.database:
                fixture.database.execute("UPDATE application_jobs SET state='CLOSED'")

    fixture.after_inspection = change_after_inspection
    fixture.cycle()
    require(not fixture.requests, "Final guard ignored " + variation)
    require(fixture.records("conversion_summaries"), "Guard scenario never prepared a champion action")
    fixture.restart()
    fixture.cycle(seconds=1)
    require(not fixture.requests, "Restart ignored " + variation)


def same_inbound_ack(fixture):
    fixture.seed(handoff=False, label="QUESTION")
    fixture.decisions["message-1"]["review_at"] = (BASE + dt.timedelta(seconds=30)).isoformat()
    fixture.receipts_visible = False
    fixture.cycle()
    require(len(fixture.requests) == 1 and fixture.actions()[0]["state"] == "ACKNOWLEDGED_UNCONFIRMED",
            "Ordinary acknowledgment did not occupy original reply slot")
    fixture.config["conversion"]["champion_handoff_enabled"] = True
    fixture.decisions["message-1"].update(handoff=True, label="AGREEMENT", review_at=DEADLINE)
    fixture.cycle(seconds=31)
    require(len(fixture.records("reply_worker_results")) == 2,
            "Due actual review did not interpret the revised champion proposal")
    require(len(fixture.requests) == 1 and len(fixture.actions()) == 1, "Summary acquired a second response slot")
    require(not fixture.records("conversion_summaries"), "Occupied inbound slot manufactured a deliverable summary")


def final_history_guard(fixture):
    """Application history changing after the claim no longer cancels the summary (owner, 2026-09-30).

    The claim-time comparison of the application context (CHAMPION_CURRENT_CANDIDATE_OR_PIPELINE_
    CONTEXT_CHANGED) was one of the last-second checks the owner removed: right before sending,
    only an opt-out or negative reply, a closed recipient, an excluded name and the one reply per
    inbound message still stop a reply.
    """
    import outreach_provider
    fixture.seed()
    original = outreach_provider.Provider.send_claimed
    injected = []

    def change_history_then_send(provider, action_id, config):
        job = conversion._job(fixture.database, fixture.jobs["message-1"])
        evidence = {"source": "https://employer.example/applications/independent-receipt", "verified": True,
                    "independent_read": True, "status": "SUBMITTED", "observed_at": fixture.now.isoformat()}
        with fixture.database:
            fixture.database.execute("INSERT INTO application_history VALUES (?,?,'APPLICATION',?)",
                                     (job["employer"], job["requisition"], json.dumps(evidence)))
        injected.append(action_id)
        return original(provider, action_id, config)

    with patch.object(outreach_provider.Provider, "send_claimed", new=change_history_then_send):
        fixture.cycle()
    require(len(injected) == 1 and len(fixture.records("outreach_action_claims")) == 1,
            "History-change scenario did not reach the claimed send")
    require(len(fixture.requests) == 1, "A history change after the claim still held the summary")


def transient(fixture, exhausted=False):
    fixture.seed()
    fixture.read_always_fails = True
    fixture.cycle()
    require(not fixture.requests, "Failed inspection sent")
    pending_report = fixture.delivery_report()
    reads = len(fixture.reads)
    fixture.cycle(seconds=1)
    require(len(fixture.reads) == reads, "Read retry ignored due time")
    fixture.restart()
    if not exhausted:
        fixture.read_always_fails = False
    fixture.cycle(seconds=900)
    if not exhausted:
        assert_single_handoff(fixture)
        fixture.cycle(seconds=1)
        require(fixture.actions()[0]["state"] == "CONFIRMED_SENT", "Recovered attempt not reconciled")
        require("RETRY_WAIT" in json.dumps(pending_report), "Report hid pending preflight failure")
        return
    fixture.cycle(seconds=3600)
    retries = fixture.snapshot()["read_retries"]
    # Reads no longer exhaust (owner, 2026-09-21); after three failures the read is holding
    # on a widened next date, not dead. The restart check below still proves nothing is spent
    # before that date arrives, which is the part that actually protects the budget.
    require(any(item["attempts"] == 3 and item["state"] == "RETRY_WAIT" and item["next_at"] for item in retries),
            "Inspection retries did not hold on a next date at three attempts")
    reads = len(fixture.reads)
    fixture.restart()
    fixture.cycle(seconds=3600)
    require(len(fixture.reads) == reads and not fixture.requests, "Read hold failed across restart")
    require("RETRY_WAIT" in json.dumps(fixture.delivery_report()), "Report hid held read failure")
    require("RETRY_WAIT" in json.dumps(pending_report), "Report hid pending preflight failure")


def legacy_output(fixture):
    with patch.object(fixture, "execute", new=Fixture.execute.__get__(fixture, ChampionFixture)):
        grounded_once(fixture)
    require(not fixture.records("conversion_summaries"), "Legacy ordinary output created a champion summary")
    require("champion_summary_id" not in json.loads(fixture.actions()[0]["payload"]), "Legacy reply acquired champion metadata")


def unapplied_agreement(fixture):
    """The summary needs the person's current agreement from this exact decision.

    The agreement used to be a per-job REFERRAL_AGREED event that stayed PENDING until the
    conversion cycle applied it. It is now the person's referral record (owner, 2026-09-25),
    written when the decision is applied, so the case left is an agreement that is not the
    one this decision produced: a later message from the same person replaced it.
    """
    import reply_execution

    fixture.seed()
    with fixture.clock():
        orchestrator.conversion_cycle(fixture.root, fixture.database, fixture.config, "manual-apply-role")
    fixture.classify()
    referral = fixture.records("referrals")
    require(len(referral) == 1 and referral[0]["state"] == "AGREED", "Decision did not record the person referral")
    with fixture.database:
        referrals.record(fixture.database, referral[0]["contact_id"], "AGREED", "offline-later-message",
                         "Yes, still happy to refer you.", NEXT_ACTION, DEADLINE, fixture.now.isoformat(), {})
    result = reply_execution.promote(fixture.database, fixture.config, fixture.now)
    require(not fixture.actions() and not fixture.records("conversion_summaries"),
            "Agreement from another message authorized champion promotion")
    require("CHAMPION_PERSON_REFERRAL_AGREEMENT_REQUIRED" in json.dumps(result),
            "Wrong blocker for an agreement not bound to this decision")
    fixture.cycle()
    require(not fixture.requests and not fixture.records("conversion_summaries"),
            "Scheduled cycle promoted a summary from an unbound agreement")


def omitted_summary(fixture, exhausted=False):
    fixture.seed(handoff=False)
    fixture.cycle()
    task = routing.one(fixture.database, "SELECT * FROM workflow_tasks WHERE worker='pipeline_routing_review'")
    require(task["status"] == "RETRY_WAIT" and task["blocker"] == "CHAMPION_SUMMARY_REQUIRED",
            "Eligible acknowledgment omission did not schedule bounded interpretation retry: " +
            repr({key: task[key] for key in ("status", "blocker", "attempts", "next_at")}))
    require(task["effect"] == "READ" and task["attempts"] == 1 and task["next_at"], "Omission consumed a write or lacked a retry deadline")
    require(not fixture.actions() and not fixture.requests and not fixture.records("reply_worker_results"),
            "Acknowledgment-only eligible agreement burned a reply slot")
    failed_attempts = fixture.records("reply_worker_attempts")
    require(len(failed_attempts) == 1 and failed_attempts[0]["blocker"] == "CHAMPION_SUMMARY_REQUIRED",
            "Missing summary did not retain its failed inference evidence")
    fixture.cycle(seconds=1)
    require(fixture.records("reply_worker_attempts") == failed_attempts, "Summary retry ignored next_at")
    fixture.restart()
    if not exhausted:
        fixture.decisions["message-1"]["handoff"] = True
    fixture.cycle(seconds=900)
    if exhausted:
        fixture.cycle(seconds=3600)
        final = routing.one(fixture.database, "SELECT * FROM workflow_tasks WHERE task_id=?", (task["task_id"],))
        require(final["status"] == "EXHAUSTED" and final["attempts"] == 3 and final["next_at"] is None,
                "Omitted-summary inference retries were not bounded at three")
        attempts = fixture.records("reply_worker_attempts")
        require(len(attempts) == 3 and all(item["blocker"] == "CHAMPION_SUMMARY_REQUIRED" for item in attempts),
                "Summary retries lost failed attempt evidence")
        fixture.restart()
        fixture.cycle(seconds=3600)
        require(fixture.records("reply_worker_attempts") == attempts and not fixture.actions() and not fixture.requests,
                "Exhausted omitted-summary task retried or burned a reply slot after restart")
    else:
        assert_single_handoff(fixture)
        require(fixture.records("reply_worker_attempts")[:1] == failed_attempts, "Successful recovery overwrote failed inference")
        fixture.cycle(seconds=1)
        require(fixture.actions()[0]["state"] == "CONFIRMED_SENT", "Corrected summary did not reconcile")
    final = routing.one(fixture.database, "SELECT * FROM workflow_tasks WHERE task_id=?", (task["task_id"],))
    require(final["deadline"] == task["deadline"], "Retry extended the original interpretation deadline")


def legacy_ack_promotion_guard(fixture):
    import reply_execution

    fixture.config["conversion"]["champion_handoff_enabled"] = False
    fixture.seed(handoff=False)
    with fixture.clock():
        orchestrator.conversion_cycle(fixture.root, fixture.database, fixture.config, "manual-role-before-legacy-ack")
    fixture.classify()
    with fixture.clock():
        orchestrator.conversion_cycle(fixture.root, fixture.database, fixture.config, "manual-legacy-agreement")
    fixture.config["conversion"]["champion_handoff_enabled"] = True
    result = reply_execution.promote(fixture.database, fixture.config, fixture.now)
    require("CHAMPION_SUMMARY_REQUIRED_BEFORE_REPLY_SLOT" in json.dumps(result), "Legacy eligible acknowledgment escaped the promotion guard")
    require(not fixture.actions() and not fixture.requests and not fixture.records("conversion_summaries"),
            "Legacy eligible acknowledgment acquired the reserved summary reply slot")


def unscoped_ack(fixture):
    fixture.seed(linked=False, handoff=False)
    fixture.cycle()
    require(len(fixture.requests) == 1 and len(fixture.actions()) == 1, "Unscoped ordinary acknowledgment was incorrectly held for a summary")
    require(not fixture.records("conversion_summaries"), "Unscoped acknowledgment invented a champion summary")
    request = next(packet for packet in fixture.inferences if "source_snapshot" in packet)
    require(request["job_scope"] == [], "Unscoped acknowledgment fixture invented role association")
    fixture.cycle(seconds=1)
    require(fixture.actions()[0]["state"] == "CONFIRMED_SENT", "Unscoped acknowledgment did not reconcile")


def ambiguous(fixture):
    fixture.seed()
    fixture.receipts_visible = False
    fixture.lost_ack = True
    fixture.cycle()
    require(assert_single_handoff(fixture)["state"] == "UNCERTAIN_NO_RETRY", "Ambiguous write did not stay claimed")
    require("UNCERTAIN_NO_RETRY" in json.dumps(fixture.delivery_report()), "Report hid ambiguous send")
    fixture.restart()
    fixture.cycle(seconds=1)
    require(len(fixture.requests) == 1 and fixture.actions()[0]["state"] != "CONFIRMED_SENT", "Ambiguous write retried or invented receipt")
    fixture.reveal_receipts()
    fixture.cycle(seconds=901)
    require(fixture.actions()[0]["state"] == "CONFIRMED_SENT" and len(fixture.requests) == 1,
            "Independent reconciliation failed or resent")


def isolation(fixture):
    fixture.seed()
    fixture.seed(OTHER_PROFILE, "message-2", "thread-2", body="I can refer you for req-2; send the summary.")
    engagement.close_outreach(fixture.database, PROFILE, "Offline closed recipient", fixture.now)
    with fixture.database:
        applications.notify(fixture.database, "champion-offline-alert", "A genuine candidate fact needs your answer.", fixture.now.isoformat())
    fixture.cycle()
    require(len(fixture.requests) == 1 and fixture.requests[0]["arguments"]["conversationId"] == "thread-2",
            "Blocked advocate prevented independent handoff")
    notification = next(item for item in fixture.records("notification_outbox") if item["event_key"] == "champion-offline-alert")
    require(notification["state"] == "PENDING" and notification["next_at"] and notification["presend_attempts"] > 0,
            "Failed notification disappeared or claimed success")
    fixture.restart()
    fixture.cycle(seconds=1)
    require(len(fixture.requests) == 1, "Independent handoff repeated after restart")
    require("CONFIRMED_SENT" in json.dumps(fixture.delivery_report()), "Report omitted independent successful handoff")


SCENARIOS = {
    "automatic_summary_shared_reply_and_restart": success,
    "provider_null_flags_direct_speaker_success": lambda fixture: success(fixture, provider_flags=True),
    **{name: lambda fixture, variation=name: blocked(fixture, variation) for name in (
        "AGREEMENT_INTENT", "ROLE_ASSOCIATION", "CANDIDATE_FACTS", "decline", "unbound",
        "no_agreement", "stale_role", "disabled", "deadline_due",
        "provider_unknown_speaker", "provider_quoted_speaker", "provider_missing_speaker", "provider_unknown_intent",
        "provider_known_quoted", "provider_known_forwarded", "provider_false_flags_unknown_speaker")},
    "requires_applied_source_agreement": unapplied_agreement,
    "eligible_ack_omission_retries_without_slot_burn": omitted_summary,
    "eligible_ack_omission_retry_exhaustion": lambda fixture: omitted_summary(fixture, exhausted=True),
    "legacy_eligible_ack_promotion_guard": legacy_ack_promotion_guard,
    "unscoped_ordinary_ack_remains_allowed": unscoped_ack,
    "closed_final_guard": lambda fixture: final_guard(fixture, "closed"),
    "revoked_final_guard": lambda fixture: final_guard(fixture, "revoked"),
    "agreement_revoked_final_guard": lambda fixture: final_guard(fixture, "agreement_revoked"),
    "role_closed_final_guard": lambda fixture: final_guard(fixture, "role_closed"),
    "history_change_after_claim_no_longer_cancels": final_history_guard,
    "same_inbound_ack_blocks_summary": same_inbound_ack,
    "transient_bounded_recovery": transient,
    "transient_retry_exhaustion": lambda fixture: transient(fixture, exhausted=True),
    "ambiguous_reconciles_without_retry": ambiguous,
    "blocked_item_and_notification_isolation": isolation,
    "legacy_ordinary_output_enabled_unchanged": legacy_output,
}


def validate(names=None):
    report = {"state": "PASS", "synthetic_only": True, "network_calls": 0, "live_sends": 0,
              "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
              "scenarios": [], "limitations": ["Provider and inference boundaries mocked; no live behavior verified.",
              "Decision transport and draft promotion stubbed; actual conversion and reporting run."]}
    with isolated_boundaries():
        for name in names or SCENARIOS:
            with ChampionFixture() as fixture:
                result = {"name": name, "state": "PASS"}
                try:
                    SCENARIOS[name](fixture)
                except Exception as error:
                    report["state"] = "FAIL"
                    result.update(state="FAIL", error_type=type(error).__name__, error=str(error), traceback=traceback.format_exc())
                result["evidence"] = fixture.snapshot()
                report["scenarios"].append(result)
                print(result["state"] + " " + name + (": " + result["error"] if "error" in result else ""), file=sys.stderr, flush=True)
    report["passed"] = sum(item["state"] == "PASS" for item in report["scenarios"])
    report["failed"] = len(report["scenarios"]) - report["passed"]
    report["completed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    report["runtime_sha256"] = {filename: hashlib.sha256((ROOT / filename).read_bytes()).hexdigest()
        for filename in ("manual_champion_handoff.py", "champion.py", "champion_reporting.py", "champion_composition.py",
                         "reply_worker.py", "pipeline_routing.py", "conversion.py", "reply_execution.py", "orchestrator.py",
                         "outreach.py", "outreach_provider.py", "outreach_agent.py", "reply_resume.py",
                         "reply-prompt.md", "reply-schema.json", "outreach-agent-prompt.md", "outreach-agent-schema.json")
        if (ROOT / filename).exists()}
    return report


if __name__ == "__main__":
    result = validate(sys.argv[1:] or None)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["state"] == "PASS" else 2)
