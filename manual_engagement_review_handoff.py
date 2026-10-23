"""Offline manual handoff validation; all changes stay in in-memory databases."""

import json
import sqlite3

import engagement
import pipeline_routing as routing
import reply_worker
import workflow_tasks


NOW = "2026-09-18T06:30:00+00:00"
CONFIG = {"pipeline_routing": {"account_ids": [123]}}


def fixture(count=16):
    database = sqlite3.connect(":memory:")
    database.row_factory = sqlite3.Row
    engagement.initialize(database)
    routing.initialize(database)
    database.executescript("""
        CREATE TABLE suppressions (identity TEXT PRIMARY KEY, reason TEXT NOT NULL);
        CREATE TABLE outreach_action_claims (action_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
        INSERT INTO outreach_action_claims VALUES ('existing-uncertain-send','preserved');
    """)
    for index in range(count):
        profile = "https://www.linkedin.com/in/fixture-" + str(index)
        message_id = "message-" + str(index)
        raw = {"message_id": message_id, "conversation_id": "thread-" + str(index), "account_id": 123,
               "sender_profile": profile, "direction": "inbound", "text": "Please clarify the role.",
               "quoted": None, "forwarded": None}
        evidence = "https://www.linkedin.com/messaging/thread/fixture-" + str(index)
        inbox = {**raw, "schema": "linkedin-inbox-v1", "raw_paths": {field: [field] for field in raw},
                 "authentication": {"method": "authenticated_provider_read", "source": evidence,
                                    "observed_at": NOW, "verified_by": "offline-manual-fixture"}}
        event = {"source": "heyreach", "source_id": message_id, "profile": profile, "kind": "inbound",
                 "raw": raw, "occurred_at": NOW, "observed_at": NOW, "verified": True,
                 "evidence": evidence, "inbox": inbox,
                 "inference": {"method": "fixture", "sentiment": "opt_out" if index == 0 else "unknown"}}
        engagement.record_event(database, event)
        saved = routing.one(database, "SELECT * FROM engagement_events WHERE source_id=?", (message_id,))
        with routing.transaction(database):
            stored = routing._snapshot(database, "engagement_events", routing.encoded(["heyreach", message_id]),
                                       "message", saved, NOW)
            owner = routing._review(database, stored, "QUOTED_OR_FORWARDED_MESSAGE_REQUIRES_REVIEW", NOW)
            database.execute("""UPDATE workflow_tasks SET status='RETRY_WAIT',blocker='DAILY_COMPUTE_BUDGET_EXHAUSTED',
                next_action='Resume unresolved classification',next_at='2026-09-18T07:00:00+00:00'
                WHERE task_id=?""", (owner["review_task_id"],))
    return database


def protected(database):
    result = {}
    for table in ("engagement_events", "engagement_stops", "engagement_closures", "suppressions",
                  "outreach_action_claims", "pipeline_routing_sources", "pipeline_routing_conflicts"):
        result[table] = [tuple(row) for row in database.execute("SELECT * FROM " + table + " ORDER BY rowid")]
    result["downstream"] = [tuple(row) for row in database.execute(
        "SELECT * FROM workflow_tasks WHERE worker!='engagement_review' ORDER BY task_id")]
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ("reply_worker_inputs", "reply_worker_attempts", "reply_worker_results", "referrals"):
        if table in tables:
            result[table] = [tuple(row) for row in database.execute("SELECT * FROM " + table + " ORDER BY rowid")]
    return result


def completed_owner(database):
    reply_worker.initialize(database)
    owner = routing.one(database, "SELECT * FROM workflow_tasks WHERE worker='pipeline_routing_review'")
    stored = routing.one(database, "SELECT * FROM pipeline_routing_sources")
    source, source_id = json.loads(stored["source_id"])
    request = routing.classification_request(database, source, source_id)
    message = json.loads(request["source_snapshot"]["payload"])["inbox"]["text"]
    template = "Offline fixture prompt"
    prompt = template + "\n\nUNTRUSTED_SOURCE_JSON:\n" + routing.encoded(request)
    provenance = {"prompt_sha256": reply_worker.sha(template), "model": "offline-fixture"}
    raw = routing.encoded({"output_version": reply_worker.VERSION, "source_sha256": stored["content_sha256"],
        "label": "QUESTION", "literal_quote": message, "quote_span": {"text": message, "start": 0, "end": len(message)},
        "role_reference": None, "reason": "The message asks for clarification.", "unknown_factual_requirements": [],
        "reply_proposal": None, "next_action": "Investigate the exact role", "review_at": NOW})
    handoff = reply_worker._validate(raw, request, message, ["UNKNOWN_QUOTED_CONTEXT", "UNKNOWN_FORWARDED_CONTEXT"])
    handoff.update(task_id=owner["task_id"], input_sha256=reply_worker.sha(prompt), output_sha256=reply_worker.sha(raw),
                   source_key=stored["source_key"], source_sha256=stored["content_sha256"], model_provenance=provenance)
    database.execute("INSERT INTO reply_worker_inputs VALUES (?,?,?,?,?)",
        (owner["task_id"], reply_worker.sha(prompt), prompt, stored["content_sha256"], routing.encoded(provenance)))
    database.execute("INSERT INTO reply_worker_attempts VALUES (?,?,?,?,?,?,?,?)",
        (owner["task_id"], 1, NOW, reply_worker.sha(prompt), raw, reply_worker.sha(raw), routing.encoded(provenance), None))
    database.execute("INSERT INTO reply_worker_results VALUES (?,?,?,?,?)",
        (owner["task_id"], reply_worker.VERSION, reply_worker.sha(prompt), reply_worker.sha(raw), routing.encoded(handoff)))
    database.execute("UPDATE workflow_tasks SET status='COMPLETE',attempts=1 WHERE task_id=?", (owner["task_id"],))
    database.commit()
    return handoff


def replacement_owner(database):
    handoff = completed_owner(database)
    previous = routing.one(database, "SELECT * FROM workflow_tasks WHERE task_id=?", (handoff["task_id"],))
    bound = json.loads(previous["inputs"])[0]
    stored = routing.one(database, "SELECT * FROM pipeline_routing_sources")
    bound.update(reply_decision_task_id=previous["task_id"], reply_decision_output_sha256=handoff["output_sha256"],
                 review_round=1, next_action=handoff["classification"]["next_action"], review_at=NOW)
    replacement = workflow_tasks.enqueue(database, "pipeline_routing_review", routing.reply_review_action(database, stored, bound),
        previous["parent_run_id"], [bound], previous["deadline"], NOW)
    database.execute("UPDATE workflow_tasks SET status='CANCELLED' WHERE task_id=?", (previous["task_id"],))
    database.execute("UPDATE pipeline_routing_sources SET result=?", (routing.encoded({
        "reply_context_task_id": previous["task_id"], "review_task_id": replacement}),))
    database.commit()
    return replacement


def referral_owner(database):
    """A message the reply AI routed as a referral agreement: recorded on the person, not a job."""
    import referrals

    stored = routing.one(database, "SELECT * FROM pipeline_routing_sources")
    event = json.loads(stored["snapshot"])
    inbox = json.loads(event["payload"])["inbox"]
    referrals.record(database, event["identity"], "AGREED", stored["source_key"], inbox["text"],
                     "Ask for the referral", None, NOW, {"provider_source_id": event["source_id"]})
    database.execute("UPDATE pipeline_routing_sources SET status='ROUTED',result=?", (routing.encoded({
        "contact_id": event["identity"], "referral_state": "AGREED", "reply_decisions": {},
        "next_action": "Ask for the referral", "review_at": None, "can_send": False}),))
    database.execute("UPDATE workflow_tasks SET status='CANCELLED' WHERE worker='pipeline_routing_review'")
    database.commit()


def main():
    database = fixture()
    before = protected(database)
    result = engagement.handoff_reviews(database, CONFIG, NOW)
    assert len(result["items"]) == 16
    assert all(item["state"] == "HANDED_OFF" and item["classification_resolved"] is False for item in result["items"])
    assert protected(database) == before
    assert database.execute("SELECT count(*) FROM workflow_tasks WHERE worker='engagement_review' AND status='CANCELLED'").fetchone()[0] == 16
    restarted = sqlite3.connect(":memory:")
    restarted.row_factory = sqlite3.Row
    database.backup(restarted)
    assert engagement.handoff_reviews(restarted, CONFIG, NOW)["items"] == []
    assert protected(restarted) == before
    database.close()
    restarted.close()
    database = fixture(1)
    database.execute("UPDATE workflow_tasks SET parent_run_id='different-message' WHERE worker='pipeline_routing_review'")
    before = protected(database)
    assert engagement.handoff_reviews(database, CONFIG, NOW)["items"][0]["state"] == "RETAINED"
    assert protected(database) == before
    assert database.execute("SELECT status FROM workflow_tasks WHERE worker='engagement_review'").fetchone()[0] == "QUEUED"
    database.close()
    for prepare in (completed_owner, replacement_owner, referral_owner):
        database = fixture(1)
        prepare(database)
        before = protected(database)
        result = engagement.handoff_reviews(database, CONFIG, NOW)
        assert result["items"][0]["state"] == "HANDED_OFF", result
        assert result["items"][0]["classification_resolved"] is False
        assert protected(database) == before
        database.close()
    print(json.dumps({"manual": "PASS", "verified_handoffs": 16, "restart_replay": "PASS",
                      "unproven_ownership_retained": "PASS", "stops_optouts_claims_downstream": "UNCHANGED",
                      "completed_result_cancelled_replacement_routed_referral": "PASS"}))


if __name__ == "__main__":
    main()
