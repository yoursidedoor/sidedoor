import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import applications
import conversion
import engagement
import orchestrator
import pipeline_routing as routing
import referrals
import reply_worker
import research_workflow as research
import workflow_tasks
from backlog_fixture import setUpModule, tearDownModule  # noqa: F401 (fixture messages predate the reply cutoff)


NOW = "2026-09-17T16:00:00+00:00"
LATER = "2026-09-17T17:00:00+00:00"
PROFILE = "https://www.linkedin.com/in/mock-contact"
URL = "https://employer.example/jobs/req-1"
RESUME_HASH = "a" * 64


def evidence(field, value, source=URL, **extra):
    return {"field": field, "value": value, "source": source, "observed_at": NOW, "effective_at": None,
            "excerpt": str(value), "verified": True, "kind": "official", **extra}


def opportunity(requisition="req-1"):
    url = "https://employer.example/jobs/" + requisition
    values = {"company": "Mock Company", "requisition_id": requisition, "role_title": "Staff Engineer", "team": "Platform",
              "team_need": "Reliable distributed APIs", "specialization": "backend", "official_destination": url}
    association = {"profile": PROFILE, "employer": "mock company", "requisition": requisition, "role_url": url}
    person = {"profile": PROFILE, "recipient": "Mock Person", "employer_matches_target": True, "evidence": [
        evidence("identity", PROFILE, PROFILE), evidence("employment", "Mock Company", url),
        evidence("role_association", association, url, excerpt="Responsible for requisition " + requisition)]}
    return {"company": "Mock Company", "role": "Staff Engineer", "requisition_id": requisition, "url": url,
            "rationale": "Mock research fixture only", "evidence": [evidence(field, value, url) for field, value in values.items()],
            "people": [person]}


def message(body=None, source_id="message-1", **extra):
    body = body or "I agree to refer you for Staff Engineer (requisition req-1) at Mock Company."
    raw = {"message_id": source_id, "conversation_id": "thread-1", "account_id": 123,
           "sender_profile": PROFILE, "direction": "inbound", "text": body, "quoted": False, "forwarded": False}
    inbox = {**raw, "schema": "linkedin-inbox-v1", "raw_paths": {field: [field] for field in raw},
             "authentication": {"method": "authenticated_provider_read", "source": "https://www.linkedin.com/messaging/thread/thread-1",
                                "observed_at": NOW, "verified_by": "mock-authenticated-decoder"}}
    return {"source": "heyreach", "source_id": source_id, "profile": PROFILE, "kind": "inbound", "raw": raw,
            "occurred_at": NOW, "observed_at": NOW, "verified": True, "evidence": inbox["authentication"]["source"], "inbox": inbox, **extra}


def referral_count(database):
    """Referrals are one row per person in `referrals` (owner, 2026-09-25)."""
    referrals.initialize(database)
    return database.execute("SELECT COUNT(*) FROM referrals").fetchone()[0]


LEGACY_REVIEW = "UNSUPPORTED_CONTEXTUAL_CLASSIFICATION_EXECUTOR:literal-whole-message-required"


class PipelineRoutingTests(unittest.TestCase):
    def setUp(self):
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        applications.initialize(self.database)
        research.initialize(self.database)
        engagement.initialize(self.database)
        conversion.initialize(self.database)
        routing.initialize(self.database)
        self.config = {"pipeline_routing": {"account_ids": [123], "designated_resume_sha256": RESUME_HASH}}
        self.addCleanup(self.database.close)

    def research(self, role=None):
        role = role or opportunity()
        key = applications.ingest(self.database, role, NOW)
        result = {"research_status": "COMPLETE", "summary": "Mock saved research", "opportunities": [role], "drafts": []}
        processed = research.process_research(self.database, result, now=NOW)
        return key, processed["result_id"]

    def claims(self):
        claims = [{"text": "Built reliable backend APIs with documented recovery behavior.", "tags": ["backend"], "page": 2},
                  {"text": "Designed a distributed coordination service.", "tags": ["backend", "distributed systems"], "page": 3}]
        research.register_claims(self.database, claims, designated_source_text="\n".join(item["text"] for item in claims),
                                 source_sha256=RESUME_HASH, designated_sha256=RESUME_HASH)
        return claims

    def cycle(self, now=NOW):
        return routing.cycle(self.database, self.config, now)

    def inbox_items(self, result):
        return [item for item in result["items"] if item.get("referral_state") or item.get("blocker")]

    def test_legacy_path_sends_every_inbound_message_to_ai_review(self):
        """With reply decisions off there is no literal matcher: every message goes to the reply AI."""
        self.research()
        bodies = [message()["raw"]["text"], "I cannot refer you.", "Happy to help!", "Maybe later"]
        for index, body in enumerate(bodies):
            engagement.record_event(self.database, message(body, "message-" + str(index)))
        result = self.cycle()
        self.assertEqual(len(bodies), sum(item.get("blocker") == LEGACY_REVIEW for item in result["items"]))
        self.assertEqual(0, referral_count(self.database))
        self.assertEqual(len(bodies), self.database.execute("SELECT COUNT(*) FROM workflow_tasks WHERE worker='pipeline_routing_review'").fetchone()[0])

    def test_authenticated_wrapper_sender_account_and_raw_paths_must_match(self):
        self.research()
        variants = []
        absent = message(source_id="absent")
        absent.pop("inbox")
        variants.append(absent)
        raw_mismatch = message(source_id="mismatch")
        raw_mismatch["inbox"]["text"] = "Different text"
        variants.append(raw_mismatch)
        wrong_account = message(source_id="account")
        wrong_account["raw"]["account_id"] = wrong_account["inbox"]["account_id"] = 456
        variants.append(wrong_account)
        wrong_sender = message(source_id="sender")
        wrong_sender["raw"]["sender_profile"] = wrong_sender["inbox"]["sender_profile"] = "https://www.linkedin.com/in/other"
        variants.append(wrong_sender)
        for flag in ("quoted", "forwarded"):
            variant = message(source_id=flag)
            variant["raw"][flag] = variant["inbox"][flag] = True
            variants.append(variant)
        for variant in variants:
            engagement.record_event(self.database, variant)
        result = self.cycle()
        self.assertEqual(len(variants), sum(item["state"] == "REVIEW" for item in result["items"]))

    def test_a_company_contact_needs_no_role_link_but_an_unlisted_one_is_never_bound(self):
        """Contacts are discovered per company (owner, 2026-09-24).

        This used to assert that a contact without role_association evidence is refused. That
        requirement is gone: the model judges which role a message is about. What still holds
        is that a contact the research never placed on the role is never bound to it.
        """
        role = opportunity()
        role["people"][0]["evidence"] = [item for item in role["people"][0]["evidence"] if item["field"] != "role_association"]
        self.research(role)
        engagement.record_event(self.database, message())
        result = self.cycle()
        self.assertFalse(any(item.get("blocker") == "EXACT_VERIFIED_SENDER_EMPLOYER_ROLE_ASSOCIATION_MISSING"
                             for item in result["items"]))
        # Someone the research never placed on any role is bound to nothing.
        rows = [r for r in self.database.execute("SELECT * FROM rw_results")]
        self.assertEqual([], routing._bindings(self.database, rows, "linkedin.com/in/nobody-listed", NOW))

    def test_official_raw_research_creates_context_and_claims_are_exact(self):
        key, unused = self.research()
        claims = self.claims()
        result = self.cycle()
        conversion.cycle(self.database, {}, NOW)
        role_id = self.database.execute("SELECT evidence_id FROM conversion_roles WHERE job_key=?", (key,)).fetchone()[0]
        role = json.loads(self.database.execute("SELECT record FROM conversion_evidence WHERE evidence_id=?", (role_id,)).fetchone()[0])
        self.assertEqual("Reliable distributed APIs", role["facts"]["needs"][0]["text"])
        imported = [json.loads(row[0])["facts"] for row in self.database.execute("SELECT record FROM conversion_evidence WHERE kind='CANDIDATE_CLAIM'")]
        self.assertEqual({item["text"] for item in claims}, {item["text"] for item in imported})
        self.assertTrue(all(item["category"] == "achievement" for item in imported))
        self.assertEqual(2, len(result["general_preparation"]["cards"]))
        prep = routing.stage_prep(self.database, key, "SYSTEM_DESIGN", NOW, RESUME_HASH)
        self.assertEqual(2, len(prep["cards"]))
        self.assertEqual(["Reliable distributed APIs"], prep["cards"][0]["role_need_quotes"])
        self.assertIn("failure modes", prep["cards"][0]["practice_prompt"])
        self.assertIsNone(prep["cards"][0]["generated_answer"])
        self.assertIsNone(prep["debrief"])
        self.assertIsNone(prep["scheduled_at"])

    def test_a_bad_role_fact_routes_as_unknown_and_is_never_recorded(self):
        """A missing or bad role fact no longer holds the role; it is recorded as unknown.

        This used to assert the role STAYED IN REVIEW. The owner deleted that refusal on
        2026-09-24 (OFFICIAL_ROLE_FIELDS_MISSING and the checks around it), so the role now
        routes. What must never happen is the bad value being written down as a fact, and that
        is what this checks: in every variation `team` is None and named in `unknown_fields`.
        An invented, stale, unofficial or contradicted team never reaches the record.
        """
        for variation in ("missing", "stale", "conflict", "unofficial", "invented"):
            with self.subTest(variation=variation):
                self.database.execute("SAVEPOINT scenario")
                role = opportunity()
                target = next(item for item in role["evidence"] if item["field"] == "team")
                if variation == "missing":
                    role["evidence"].remove(target)
                elif variation == "stale":
                    target["observed_at"] = "2026-09-01T16:00:00+00:00"
                elif variation == "conflict":
                    role["evidence"].append(evidence("team", "Different team"))
                elif variation == "unofficial":
                    target["kind"] = "aggregator"
                else:
                    target["excerpt"] = "No actual team name here"
                self.research(role)
                self.cycle()
                record = self.database.execute(
                    "SELECT record FROM conversion_evidence WHERE kind='ROLE_CONTEXT'").fetchone()
                self.assertIsNotNone(record, "the role should route, not stay in review")
                facts = json.loads(record[0])["facts"]
                self.assertIsNone(facts["team"])
                self.assertIn("team", facts["unknown_fields"])
                self.assertNotIn("Different team", json.dumps(facts))
                self.database.execute("ROLLBACK TO scenario")
                self.database.execute("RELEASE scenario")

    def test_a_claim_without_a_page_routes(self):
        """E4.T2: a .docx résumé's claims carry no page; routing records them without one."""
        research.register_claims(self.database, [{"text": "Wrote the on-call runbooks.", "tags": ["backend"], "page": None}],
                                 designated_source_text="", source_sha256=RESUME_HASH, designated_sha256=RESUME_HASH)
        self.cycle()
        [record] = [json.loads(row[0]) for row in self.database.execute(
            "SELECT record FROM conversion_evidence WHERE kind='CANDIDATE_CLAIM'")]
        self.assertEqual("Wrote the on-call runbooks.", record["facts"]["text"])
        self.assertIsNone(record["facts"]["page"])
        self.assertEqual("designated resume", record["source_locator"])

    def test_designated_hash_mismatch_never_imports_claim(self):
        self.claims()
        self.config["pipeline_routing"]["designated_resume_sha256"] = "b" * 64
        result = self.cycle()
        self.assertFalse(result["general_preparation"]["cards"])
        self.assertEqual(0, self.database.execute("SELECT COUNT(*) FROM conversion_evidence WHERE kind='CANDIDATE_CLAIM'").fetchone()[0])

    def test_research_or_claim_mutation_is_detected_with_preserved_snapshot(self):
        key, result_id = self.research()
        self.claims()
        self.cycle()
        original = self.database.execute("SELECT raw FROM rw_results WHERE id=?", (result_id,)).fetchone()[0]
        changed = json.loads(original)
        changed["opportunities"][0]["role"] = "Invented role"
        self.database.execute("UPDATE rw_results SET raw=? WHERE id=?", (json.dumps(changed), result_id))
        result = self.cycle()
        self.assertTrue(any(item.get("blocker") == "SOURCE_IDENTITY_CONTENT_CHANGED" for item in result["items"]))
        self.assertEqual(1, self.database.execute("SELECT COUNT(*) FROM pipeline_routing_conflicts").fetchone()[0])
        saved = json.loads(self.database.execute("SELECT snapshot FROM pipeline_routing_sources WHERE source_table='rw_results'").fetchone()[0])
        self.assertEqual(original, saved["raw"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.database.execute("UPDATE pipeline_routing_sources SET snapshot='{}'")

    def test_restart_and_repeated_cursor_reads_do_not_duplicate_review(self):
        self.research()
        engagement.record_event(self.database, message())
        self.cycle()
        self.database.commit()
        count = "SELECT COUNT(*) FROM workflow_tasks WHERE worker='pipeline_routing_review'"
        before = self.database.execute(count).fetchone()[0]
        self.assertEqual(1, before)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mock.sqlite"
            target = sqlite3.connect(path)
            self.database.backup(target)
            target.close()
            restored = sqlite3.connect(path)
            restored.row_factory = sqlite3.Row
            try:
                routing.initialize(restored)
                routing.cycle(restored, self.config, LATER)
                self.assertEqual(before, restored.execute(count).fetchone()[0])
            finally:
                restored.close()

    def test_caller_rollback_removes_all_new_routes_handoffs_and_sources(self):
        self.research()
        engagement.record_event(self.database, message("Maybe later"))
        self.database.commit()
        self.database.execute("BEGIN")
        self.cycle()
        self.assertTrue(self.database.in_transaction)
        self.database.rollback()
        self.assertEqual(0, self.database.execute("SELECT COUNT(*) FROM pipeline_routing_sources").fetchone()[0])
        self.assertEqual(0, self.database.execute("SELECT COUNT(*) FROM conversion_events").fetchone()[0])
        self.assertEqual(0, self.database.execute("SELECT COUNT(*) FROM workflow_tasks WHERE worker='pipeline_routing_review'").fetchone()[0])

    def test_classification_contract_is_read_only_and_cannot_authorize_agreement(self):
        engagement.record_event(self.database, message("I may be able to help."))
        result = self.cycle()
        self.assertEqual("DISABLED_LEGACY_LITERAL_ONLY", result["adapters"]["semantic_classification"])
        request = routing.classification_request(self.database, "heyreach", "message-1")
        self.assertEqual("READY_FOR_AI_DECISION", request["state"])
        self.assertEqual("READ_ONLY_DECISION", request["capability"])
        self.assertFalse(request["can_advance_conversion"])
        self.assertEqual("I may be able to help.", request["raw_message"]["text"])
        self.assertEqual(0, self.database.execute("SELECT COUNT(*) FROM conversion_events").fetchone()[0])

    def test_small_batches_do_not_starve_later_events_behind_reviews(self):
        self.research()
        self.config["pipeline_routing"]["max_items"] = 1
        for index in range(3):
            engagement.record_event(self.database, message("Maybe", "review-" + str(index)))
        for unused in range(3):
            self.cycle()
        self.assertEqual(3, self.database.execute("SELECT COUNT(*) FROM pipeline_routing_sources WHERE source_table='engagement_events'").fetchone()[0])

    def test_changed_designated_resume_excludes_previously_imported_cards(self):
        self.claims()
        self.assertTrue(self.cycle()["general_preparation"]["cards"])
        self.database.execute("UPDATE rw_meta SET value=? WHERE key='designated_resume'", ("b" * 64,))
        self.assertFalse(self.cycle()["general_preparation"]["cards"])

    def test_malformed_item_does_not_block_unrelated_valid_inbox_event(self):
        self.research()
        malformed = message(source_id="malformed")
        malformed["inbox"]["authentication"] = []
        engagement.record_event(self.database, malformed)
        engagement.record_event(self.database, message(source_id="good"))
        result = self.cycle()
        blockers = [item.get("blocker") for item in result["items"] if item["state"] == "REVIEW"]
        self.assertEqual(1, blockers.count(LEGACY_REVIEW))
        self.assertEqual(2, len(blockers))

    def test_a_newer_research_snapshot_is_used_never_the_older_association(self):
        """Bindings come from the NEWEST snapshot of a role, never an older one.

        This used to assert no referral routed at all, because the newer snapshot lacked
        role_association and that was required. It is not any more (owner, 2026-09-24), so the
        newer snapshot binds on its own - and the point of the test is that it is the newer
        snapshot's evidence that is used, not the older association falling back in.
        """
        self.research()
        newer = opportunity()
        newer["people"][0]["evidence"] = [item for item in newer["people"][0]["evidence"] if item["field"] != "role_association"]
        result = {"research_status": "COMPLETE", "summary": "New source with unresolved mapping", "opportunities": [newer], "drafts": []}
        research.process_research(self.database, result, now=LATER)
        rows = [r for r in self.database.execute("SELECT * FROM rw_results")]
        profile = research.profile_key(newer["people"][0]["profile"])
        bindings = routing._bindings(self.database, rows, profile, LATER)
        self.assertEqual(1, len(bindings))
        self.assertNotIn("role_association", bindings[0]["person_proofs"])

    def test_inbox_raw_mutation_is_a_conflict_not_a_second_event(self):
        self.research()
        engagement.record_event(self.database, message())
        self.cycle()
        count = self.database.execute("SELECT COUNT(*) FROM conversion_events").fetchone()[0]
        self.database.execute("UPDATE engagement_events SET raw=? WHERE source_id='message-1'", (json.dumps({"text": "different"}),))
        result = self.cycle()
        self.assertTrue(any(item.get("blocker") == "SOURCE_IDENTITY_CONTENT_CHANGED" for item in result["items"]))
        self.assertEqual(count, self.database.execute("SELECT COUNT(*) FROM conversion_events").fetchone()[0])

    def test_future_authentication_and_user_outbound_messages_never_become_agreements(self):
        self.research()
        future = message(source_id="future")
        future["inbox"]["authentication"]["observed_at"] = LATER
        engagement.record_event(self.database, future)
        outbound = message(source_id="outbound", kind="initial_sent")
        engagement.record_event(self.database, outbound)
        result = self.cycle()
        self.assertFalse(any(item.get("referral_state") for item in result["items"]))

    def test_real_interview_stage_receives_ephemeral_claim_specific_prep(self):
        key, unused = self.research()
        self.claims()
        self.cycle()
        conversion.record_evidence(self.database, {"evidence_id": "mock-screen", "kind": "INTERVIEW_STAGE", "source_type": "employer_record",
            "source_ref": "https://employer.example/confirmation", "source_locator": "mock confirmation", "source_sha256": "b" * 64,
            "verified_by": "mock-importer", "observed_at": NOW, "occurred_at": NOW,
            "facts": {"employer": "mock company", "requisition": "req-1", "stage_id": "technical", "stage": "TECHNICAL_SCREEN"}})
        conversion.enqueue_event(self.database, "mock-screen", "INTERVIEW_STAGE", key, "mock-screen")
        conversion.cycle(self.database, {}, NOW)
        result = self.cycle()
        self.assertEqual(1, len(result["stage_preparation"]))
        self.assertEqual("TECHNICAL_SCREEN", result["stage_preparation"][0]["practice_stage"])
        self.assertTrue(result["stage_preparation"][0]["cards"])

    def test_malformed_research_shape_does_not_block_unrelated_inbox_event(self):
        self.research()
        engagement.record_event(self.database, message())
        malformed = []
        identity = research.digest([research.PROCESSOR_VERSION, None, malformed])
        self.database.execute("INSERT INTO rw_results VALUES (?,NULL,?,?,?)",
                              (identity, NOW, json.dumps(malformed), json.dumps({"result_id": identity, "opportunities": []})))
        result = self.cycle()
        blockers = [item.get("blocker") for item in result["items"] if item["state"] == "REVIEW"]
        self.assertIn(LEGACY_REVIEW, blockers)
        self.assertEqual(2, len(blockers))


class ReplyDecisionHarness:
    """Shared offline fixture for the V2 reply-decision path."""

    def setUp(self):
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        self.addCleanup(self.database.close)
        for module in (applications, research, engagement, conversion, routing, reply_worker):
            module.initialize(self.database)
        role = opportunity()
        self.key = applications.ingest(self.database, role, NOW)
        research.process_research(self.database, {"research_status": "COMPLETE", "summary": "Synthetic routing fixture",
            "opportunities": [role], "drafts": []}, now=NOW)
        self.config = {"codex": "/mock-only/codex", "model": "mock-decision", "timezone": "UTC",
                       "pipeline_routing": {"account_ids": [123]},
                       "reply_worker": {"enabled": True, "timeout_seconds": 10}}
        self.database.commit()

    def output(self, request, label="AGREEMENT", unknowns=None):
        body = json.loads(request["source_snapshot"]["payload"])["inbox"]["text"]
        return {"output_version": "reply-decision-v2", "source_sha256": request["source_sha256"],
                "label": label, "literal_quote": body, "quote_span": {"start": 0, "end": len(body), "text": body},
                "role_reference": self.key, "reason": "Synthetic interpretation of the authenticated message",
                "unknown_factual_requirements": unknowns or [], "reply_proposal": None,
                "next_action": "Review the authenticated referral status without sending", "review_at": LATER}

    def persist(self, source_id="decision", label="AGREEMENT", unknowns=None, legacy=False, corrupt=None, body=None):
        engagement.record_event(self.database, message(body or "Absolutely, I can put you forward.", source_id))
        routing.cycle(self.database, self.config, NOW)
        request = routing.classification_request(self.database, "heyreach", source_id)
        stored = routing.one(self.database, "SELECT * FROM pipeline_routing_sources WHERE source_key=?", (request["source_key"],))
        task_id = json.loads(stored["result"])["review_task_id"]
        task = routing.one(self.database, "SELECT * FROM workflow_tasks WHERE task_id=?", (task_id,))
        request, body, holds = reply_worker._source(self.database, task, self.config, NOW)
        template = "Synthetic offline decision prompt"
        prompt = template + "\n\nUNTRUSTED_SOURCE_JSON:\n" + routing.encoded(request)
        output = self.output(request, label, unknowns)
        if legacy:
            output.update(output_version="reply-inference-v1", role_reference=None)
            output.pop("next_action")
            output.pop("review_at")
        raw = routing.encoded(output)
        handoff = ({"output_version": "reply-inference-v1", "classification": output, "can_send": False,
                    "can_advance_conversion": False} if legacy else reply_worker._validate(raw, request, body, holds))
        provenance = {"model": "mock-decision", "executable": "/mock-only/codex", "prompt_sha256": reply_worker.sha(template),
                      "schema_sha256": "a" * 64, "output_version": output["output_version"]}
        handoff.update(task_id=task_id, input_sha256=reply_worker.sha(prompt), output_sha256=reply_worker.sha(raw),
                       source_key=request["source_key"], source_sha256=request["source_sha256"], model_provenance=provenance)
        if corrupt:
            corrupt(handoff)
        self.database.execute("INSERT INTO reply_worker_inputs VALUES (?,?,?,?,?)",
            (task_id, reply_worker.sha(prompt), prompt, request["source_sha256"], routing.encoded(provenance)))
        self.database.execute("INSERT INTO reply_worker_attempts VALUES (?,?,?,?,?,?,?,?)",
            (task_id, 1, NOW, reply_worker.sha(prompt), raw, reply_worker.sha(raw), routing.encoded(provenance), None))
        self.database.execute("INSERT INTO reply_worker_results VALUES (?,?,?,?,?)",
            (task_id, output["output_version"], reply_worker.sha(prompt), reply_worker.sha(raw), routing.encoded(handoff)))
        self.database.execute("UPDATE workflow_tasks SET status='COMPLETE',attempts=1 WHERE task_id=?", (task_id,))
        self.database.commit()
        return task_id, request["source_key"]

    def consume(self, task_id, now=NOW):
        self.database.execute("BEGIN IMMEDIATE")
        try:
            result = routing.apply_reply_decision(self.database, task_id, self.config, now)
            self.database.commit()
            return result
        except BaseException:
            self.database.rollback()
            raise

    def source_result(self, source_key):
        return json.loads(routing.one(self.database, "SELECT result FROM pipeline_routing_sources WHERE source_key=?", (source_key,))["result"])

    def add_context(self, source_id, thread="thread-1", account=123):
        event = message("We are discussing the Platform requisition.", source_id)
        for field, value in (("conversation_id", thread), ("account_id", account)):
            event["raw"][field] = event["inbox"][field] = value
        engagement.record_event(self.database, event)


class ReplyDecisionRoutingTests(ReplyDecisionHarness, unittest.TestCase):
    def test_enabled_literal_message_waits_for_ai_and_uses_decision_provenance(self):
        task_id, source_key = self.persist(body=message()["raw"]["text"], unknowns=["CANDIDATE_FACTS"])
        self.assertEqual("SOURCE_BOUND_AI_REPLY_DECISIONS", routing.cycle(self.database, self.config, NOW)["adapters"]["semantic_classification"])
        self.database.commit()
        self.assertEqual(0, referral_count(self.database))
        result = self.consume(task_id)
        self.assertEqual(("ROUTED", "AGREED", False), (result["state"], result["referral_state"], result["can_send"]))
        self.assertNotIn("job_key", result)
        referral = referrals.current(self.database, result["contact_id"])
        self.assertEqual(("AGREED", source_key, LATER), (referral["state"], referral["source_key"], referral["review_at"]))
        entry = referral["history"][-1]["evidence"]
        self.assertEqual("reply-decision-v2", entry["classification"])
        self.assertEqual(task_id, entry["decision_provenance"]["task_id"])
        self.assertEqual(64, len(entry["decision_provenance"]["output_sha256"]))
        self.assertFalse(self.database.execute("SELECT 1 FROM conversion_events WHERE kind LIKE 'REFERRAL_%'").fetchone())
        self.assertFalse(self.database.execute("SELECT 1 FROM application_attempts").fetchone())

    def test_natural_decline_does_not_fabricate_submission_or_release_application(self):
        task_id, unused = self.persist(label="DECLINE", body="Sorry, I cannot put you forward after all.")
        self.database.execute("UPDATE application_jobs SET state='REFERRAL_HOLD',blocker='PENDING_REFERRAL'")
        self.database.commit()
        self.assertEqual("DECLINED", self.consume(task_id)["referral_state"])
        with self.database:
            conversion.cycle(self.database, {}, NOW)
        self.assertEqual("REFERRAL_HOLD", routing.one(self.database, "SELECT state FROM application_jobs")["state"])
        self.assertFalse(self.database.execute("SELECT 1 FROM conversion_outcomes").fetchone())

    def test_review_labels_and_critical_unknowns_schedule_read_without_referral(self):
        for label, unknowns in [("QUESTION", []), ("AMBIGUOUS", []), ("QUOTED_OR_FORWARDED", []), ("OTHER", []),
                                ("AGREEMENT", ["AGREEMENT_INTENT"])]:
            with self.subTest(label=label, unknowns=unknowns):
                task_id, source_key = self.persist(label + str(len(unknowns)), label, unknowns)
                result = self.consume(task_id)
                task = routing.one(self.database, "SELECT * FROM workflow_tasks WHERE task_id=?", (result["review_task_id"],))
                self.assertEqual(("REVIEW", "QUEUED", "READ", LATER), (result["state"], task["status"], task["effect"], task["next_at"]))
                self.assertIn(task_id, self.source_result(source_key)["reply_decisions"])
        self.assertEqual(0, referral_count(self.database))

    def test_consumed_review_survives_context_change_and_hourly_reread(self):
        task_id, source_key = self.persist(label="QUESTION")
        self.consume(task_id)
        saved = self.source_result(source_key)
        self.add_context("new-context")
        routing.cycle(self.database, self.config, NOW)
        self.database.commit()
        self.assertEqual(saved, self.source_result(source_key))
        with patch.object(reply_worker, "_source", side_effect=AssertionError("Mutable context was revalidated")):
            self.assertEqual("ALREADY_REVIEW", self.consume(task_id)["state"])
        self.assertEqual(saved, self.source_result(source_key))

    def test_routed_replay_survives_changed_referral_context(self):
        task_id, unused = self.persist()
        first = self.consume(task_id)
        with patch.object(reply_worker, "_source", side_effect=AssertionError("Mutable context was revalidated")):
            replay = self.consume(task_id)
        self.assertEqual("ALREADY_ROUTED", replay["state"])
        self.assertEqual(first["referral_state"], replay["referral_state"])
        self.assertEqual(1, len(referrals.current(self.database, first["contact_id"])["history"]))

    # The saved decision is not re-validated any more (owner, 2026-09-30: "For 'the saved decision
    # must revalidate' please remove this completely"). The three cases below asserted that it was.
    def test_source_mutation_no_longer_blocks_replay_and_preserves_consumed_map(self):
        task_id, source_key = self.persist(label="QUESTION")
        self.consume(task_id)
        saved = self.source_result(source_key)
        self.database.execute("UPDATE engagement_events SET raw='{}' WHERE source_id='decision'")
        routing.cycle(self.database, self.config, NOW)
        self.database.commit()
        self.assertTrue(self.consume(task_id)["state"].startswith("ALREADY_"))
        self.assertEqual(saved, self.source_result(source_key))

    def test_new_context_no_longer_invalidates_a_saved_decision(self):
        task_id, unused = self.persist()
        self.add_context("new-context")
        self.consume(task_id)
        self.assertEqual(1, referral_count(self.database))

    def test_forged_advance_flag_does_not_override_audited_unknown_intent(self):
        # Not re-validated, but the decision's own AGREEMENT_INTENT unknown still keeps it from
        # recording a referral.
        task_id, unused = self.persist(unknowns=["AGREEMENT_INTENT"], corrupt=lambda handoff: handoff.update(can_advance_conversion=True))
        self.consume(task_id)
        self.assertEqual(0, referral_count(self.database))

    def test_caller_rollback_removes_referral_and_consumed_marker(self):
        task_id, source_key = self.persist()
        saved = self.source_result(source_key)
        self.database.execute("BEGIN IMMEDIATE")
        routed = routing.apply_reply_decision(self.database, task_id, self.config, NOW)
        self.database.rollback()
        self.assertEqual(saved, self.source_result(source_key))
        self.assertIsNone(referrals.current(self.database, routed["contact_id"]))
        self.assertEqual("ROUTED", self.consume(task_id)["state"])

    def test_new_context_wakes_only_existing_same_account_thread_review_once(self):
        task_id, source_key = self.persist(label="QUESTION")
        review_id = self.consume(task_id)["review_task_id"]
        for source_id, thread, account in [("other-thread", "other", 123), ("other-account", "thread-1", 456)]:
            self.add_context(source_id, thread, account)
        self.assertNotIn(review_id, routing.cycle(self.database, self.config, NOW)["woken_reply_reviews"])
        self.assertEqual(LATER, routing.one(self.database, "SELECT next_at FROM workflow_tasks WHERE task_id=?", (review_id,))["next_at"])
        self.add_context("matching")
        self.assertIn(review_id, routing.cycle(self.database, self.config, NOW)["woken_reply_reviews"])
        self.assertNotIn(review_id, routing.cycle(self.database, self.config, NOW)["woken_reply_reviews"])
        self.assertEqual(review_id, self.source_result(source_key)["review_task_id"])
        self.assertEqual(1, self.database.execute("SELECT COUNT(*) FROM workflow_task_events WHERE task_id=? AND event='CONTEXT_AVAILABLE'", (review_id,)).fetchone()[0])

    def test_due_reviews_are_consumed_and_keep_a_next_date(self):
        task_id, source_key = self.persist(label="QUESTION")
        self.consume(task_id)
        root = Path(__file__).resolve().parents[1]
        def execute(config, prompt, schema, timeout):
            request = json.loads(prompt.partition("\n\nUNTRUSTED_SOURCE_JSON:\n")[2])
            return routing.encoded(self.output(request, "QUESTION"))
        with patch.object(reply_worker, "_execute", side_effect=execute) as executor:
            self.assertFalse(reply_worker.cycle(root, self.database, self.config, NOW)["items"])
            self.assertFalse(executor.called)
            reply_worker.cycle(root, self.database, self.config, LATER)
            later = (dt.datetime.fromisoformat(LATER) + dt.timedelta(minutes=1)).isoformat()
            reply_worker.cycle(root, self.database, self.config, later)
            # At least one due review is consumed; the exact count depended on the old
            # three-round budget scheduling successors back to back. What matters now is
            # that reviews are consumed and a successor is always scheduled (below).
            self.assertGreaterEqual(executor.call_count, 1)
            # The three-round review budget is gone (owner, 2026-09-23). It wrote
            # `review_budget_exhausted` - a field with ZERO readers - froze the source at
            # status REVIEW and told nobody; 7 live inbound messages are parked there, one a
            # recruiter thread. A review that cannot resolve keeps a NEXT DATE instead, and the
            # task layer's 7-attempt growing backoff is what bounds the work.
            self.assertNotIn("review_budget_exhausted", self.source_result(source_key))
            self.assertIsNotNone(self.source_result(source_key)["review_task_id"])
        self.assertGreaterEqual(len(self.source_result(source_key)["reply_decisions"]), 1)
        self.assertEqual(0, referral_count(self.database))

    def test_history_suppression_allows_decision_but_explicit_exclusion_holds(self):
        self.database.execute("CREATE TABLE suppressions(identity TEXT PRIMARY KEY,reason TEXT NOT NULL)")
        self.database.execute("INSERT INTO suppressions VALUES (?,?)", (PROFILE.removeprefix("https://www."), "Previously contacted; no repeat initial outreach"))
        self.database.commit()
        task_id, unused = self.persist()
        self.assertEqual("ROUTED", self.consume(task_id)["state"])
        self.database.execute("INSERT INTO suppressions VALUES ('name:mock person','Existing user exclusion')")
        self.database.commit()
        task_id, unused = self.persist("excluded")
        self.assertEqual("REVIEW", self.consume(task_id)["state"])
        self.assertEqual(1, referral_count(self.database))

    def test_v1_gets_one_bounded_upgrade_without_overwriting_prior_audit(self):
        prior_id, source_key = self.persist(legacy=True)
        prior = routing.one(self.database, "SELECT * FROM reply_worker_results WHERE task_id=?", (prior_id,))
        result = routing.cycle(self.database, self.config, NOW)
        self.assertEqual(1, len(result["upgraded_reply_reviews"]))
        upgrade_id = result["upgraded_reply_reviews"][0]
        self.assertFalse(routing.cycle(self.database, self.config, NOW)["upgraded_reply_reviews"])
        self.assertEqual(upgrade_id, self.source_result(source_key)["review_task_id"])
        self.database.commit()
        def execute(config, prompt, schema, timeout):
            return routing.encoded(self.output(json.loads(prompt.partition("\n\nUNTRUSTED_SOURCE_JSON:\n")[2])))
        with patch.object(reply_worker, "_execute", side_effect=execute):
            reply_worker.cycle(Path(__file__).resolve().parents[1], self.database, self.config, NOW)
        self.assertEqual(prior, routing.one(self.database, "SELECT * FROM reply_worker_results WHERE task_id=?", (prior_id,)))
        self.assertIn(upgrade_id, self.source_result(source_key)["reply_decisions"])
        self.assertEqual(2, self.database.execute("SELECT COUNT(*) FROM reply_worker_results").fetchone()[0])

    def test_timeout_context_revision_preserves_failed_audit_without_result(self):
        engagement.record_event(self.database, message("Could you clarify the team?", "timeout"))
        routing.cycle(self.database, self.config, NOW)
        self.database.commit()
        with patch.object(reply_worker, "_execute", side_effect=reply_worker.Held("CLASSIFICATION_TIMEOUT", retry=True)):
            reply_worker.cycle(Path(__file__).resolve().parents[1], self.database, self.config, NOW)
        task = routing.one(self.database, "SELECT * FROM workflow_tasks WHERE worker='pipeline_routing_review'")
        saved = routing.rows(self.database, "SELECT * FROM reply_worker_inputs")
        attempts = routing.rows(self.database, "SELECT * FROM reply_worker_attempts")
        self.assertFalse(self.database.execute("SELECT 1 FROM reply_worker_results").fetchone())
        self.add_context("fresh")
        self.database.execute("BEGIN IMMEDIATE")
        revised = routing.revise_reply_context(self.database, task, self.config, LATER)
        self.assertIsNotNone(revised)
        self.assertEqual(revised, routing.revise_reply_context(self.database, task, self.config, LATER))
        self.database.commit()
        child = routing.one(self.database, "SELECT * FROM workflow_tasks WHERE task_id=?", (revised,))
        self.assertEqual(("QUEUED", "READ", 1), (child["status"], child["effect"], json.loads(child["inputs"])[0]["review_round"]))
        reply_worker._source(self.database, child, self.config, LATER)
        source_key = json.loads(child["inputs"])[0]["source_key"]
        source_before = self.source_result(source_key)
        routing.cycle(self.database, self.config, LATER)
        self.database.commit()
        self.assertEqual(source_before, self.source_result(source_key))
        self.assertEqual(revised, self.source_result(source_key)["review_task_id"])
        self.assertEqual(saved, routing.rows(self.database, "SELECT * FROM reply_worker_inputs"))
        self.assertEqual(attempts, routing.rows(self.database, "SELECT * FROM reply_worker_attempts"))
        self.database.execute("BEGIN IMMEDIATE")
        self.assertIsNone(routing.revise_reply_context(self.database, task, dict(self.config, model="changed"), LATER))
        self.database.execute("UPDATE engagement_events SET raw='{}' WHERE source_id='timeout'")
        self.assertIsNone(routing.revise_reply_context(self.database, task, self.config, LATER))
        self.database.rollback()

    def test_context_revision_and_review_each_schedule_a_successor(self):
        task_id, source_key = self.persist(label="QUESTION")
        task = routing.one(self.database, "SELECT * FROM workflow_tasks WHERE task_id=?", (task_id,))
        self.add_context("fresh")
        self.database.execute("BEGIN IMMEDIATE")
        revised = routing.revise_reply_context(self.database, task, self.config, NOW)
        self.database.commit()
        self.assertIsNotNone(revised)
        def execute(config, prompt, schema, timeout):
            return routing.encoded(self.output(json.loads(prompt.partition("\n\nUNTRUSTED_SOURCE_JSON:\n")[2]), "QUESTION"))
        with patch.object(reply_worker, "_execute", side_effect=execute):
            reply_worker.cycle(Path(__file__).resolve().parents[1], self.database, self.config, NOW)
            self.database.commit()
            reply_worker.cycle(Path(__file__).resolve().parents[1], self.database, self.config, LATER)
        # No shared three-decision budget any more; a successor review is always scheduled.
        self.assertNotIn("review_budget_exhausted", self.source_result(source_key))
        self.assertGreaterEqual(self.database.execute("SELECT COUNT(*) FROM workflow_tasks WHERE worker='pipeline_routing_review'").fetchone()[0], 3)

    def test_conflicting_context_hold_validates_on_consumption_and_replay(self):
        self.add_context("conflict")
        routing.cycle(self.database, self.config, NOW)
        context = routing.classification_request(self.database, "heyreach", "conflict")
        self.database.execute("INSERT INTO pipeline_routing_conflicts VALUES (?,?,?,?)",
                              ("synthetic-conflict", context["source_key"], NOW, "{}"))
        self.database.commit()
        task_id, unused = self.persist()
        self.assertEqual("REVIEW", self.consume(task_id)["state"])
        self.assertEqual("ALREADY_REVIEW", self.consume(task_id)["state"])
        self.assertEqual(0, referral_count(self.database))


if __name__ == "__main__":
    unittest.main()


class ReplyExclusionRoutingTests(ReplyDecisionHarness, unittest.TestCase):
    """Regression for the profile-anchored reply exclusion gate.

    `_reply_excluded` resolved the correspondent's name only from `history` rows
    carrying a profile, and only in `research_workflow.name_key`'s word-split
    form. Suppression rows are written by `orchestrator.py:163,175` in
    `orchestrator.name_key`'s display form, and a name can be durably bound in
    `rw_contacts`, `outreach_queue` or a draft payload with no `history` row at
    all. Either way the name set was empty or wrongly spelled, the seeded
    exclusion literals could not match, the `name:` suppression expansion
    covered nothing, and an explicitly excluded correspondent advanced to a
    recorded referral agreement.
    """

    PERSON = "Siobhán O'Neill"

    def setUp(self):
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        self.addCleanup(self.database.close)
        for module in (applications, research, engagement, conversion, routing, reply_worker):
            module.initialize(self.database)
        role = opportunity()
        role["people"][0]["recipient"] = self.PERSON
        self.key = applications.ingest(self.database, role, NOW)
        research.process_research(self.database, {"research_status": "COMPLETE", "summary": "Synthetic routing fixture",
            "opportunities": [role], "drafts": []}, now=NOW)
        self.config = {"codex": "/mock-only/codex", "model": "mock-decision", "timezone": "UTC",
                       "pipeline_routing": {"account_ids": [123]},
                       "reply_worker": {"enabled": True, "timeout_seconds": 10}}
        self.database.execute("CREATE TABLE IF NOT EXISTS suppressions (identity TEXT PRIMARY KEY, reason TEXT NOT NULL)")
        self.database.execute("CREATE TABLE IF NOT EXISTS history (identity TEXT PRIMARY KEY, recipient TEXT NOT NULL,"
                              " company TEXT NOT NULL, profile TEXT, evidence TEXT NOT NULL)")
        self.database.commit()

    def exclude(self, recipient, reason="Existing user exclusion"):
        """Write the row exactly as orchestrator.py:173-175 writes it."""
        self.database.execute("INSERT OR IGNORE INTO suppressions VALUES (?,?)",
                              ("name:" + orchestrator.name_key(recipient), reason))
        self.database.commit()

    def referrals(self):
        return referral_count(self.database)

    def test_display_name_exclusion_is_not_missed_by_the_word_split_key(self):
        self.exclude(self.PERSON)
        self.assertEqual([("name:siobhán o'neill", "Existing user exclusion")],
                         [tuple(row) for row in self.database.execute("SELECT identity,reason FROM suppressions")])
        task_id, unused = self.persist()
        result = self.consume(task_id)
        self.assertEqual("REVIEW", result["state"])
        self.assertEqual("REPLY_EXCLUDED_OR_SUPPRESSED", result.get("reply_exclusion_blocker"))
        self.assertEqual(0, self.referrals())

    def test_exclusion_bound_only_to_the_durable_contact_record_is_found(self):
        """reply_execution.binding_gates calls this with bindings=[]; the name
        must still resolve from the research contact record."""
        self.database.execute("INSERT INTO rw_contacts VALUES ('mock company',?,?,1,NULL)",
                              (research.profile_key(PROFILE), research.name_key(self.PERSON)))
        self.exclude(research.name_key(self.PERSON))
        self.database.commit()
        self.assertTrue(routing._reply_excluded(self.database, research.profile_key(PROFILE), []))

    def test_seeded_literal_bound_only_to_the_contact_record_is_found(self):
        self.database.execute("INSERT INTO rw_contacts VALUES ('mock company',?,'rob vance',1,NULL)",
                              (research.profile_key(PROFILE),))
        self.database.commit()
        self.assertTrue(routing._reply_excluded(self.database, research.profile_key(PROFILE), []))

    def test_opted_out_correspondent_never_advances_conversion(self):
        self.database.execute("INSERT INTO engagement_stops VALUES (?, 'OPTED_OUT', ?, ?)",
                              (orchestrator.profile_key(PROFILE), NOW, "mock-opt-out"))
        self.database.commit()
        task_id, unused = self.persist()
        self.assertEqual("REVIEW", self.consume(task_id)["state"])
        self.assertEqual(0, self.referrals())

    def test_seeded_exclusion_literals_still_hold(self):
        """Preserved: the literal carried by the verified research contact."""
        for literal in ("Taylor Quinn", "Robert Vance", "Rob", "Rob Vance"):
            with self.subTest(literal=literal):
                self.PERSON = literal
                self.setUp()
                task_id, unused = self.persist()
                self.assertEqual("REVIEW", self.consume(task_id)["state"])
                self.assertEqual(0, self.referrals())
        self.PERSON = type(self).PERSON

    def test_previously_contacted_reason_still_lets_the_reply_decision_route(self):
        """The history reasons are deliberately reply-allowed; no new false positive."""
        self.database.execute("INSERT INTO suppressions VALUES (?,?)",
                              (orchestrator.profile_key(PROFILE), "Previously contacted; no repeat initial outreach"))
        self.exclude(self.PERSON, "Previously contacted; no repeat initial outreach")
        task_id, unused = self.persist()
        result = self.consume(task_id)
        self.assertEqual("ROUTED", result["state"])
        self.assertNotIn("reply_exclusion_blocker", result)
        self.assertEqual(1, self.referrals())

    def test_named_correspondent_without_any_suppression_still_routes(self):
        task_id, unused = self.persist()
        result = self.consume(task_id)
        self.assertEqual("ROUTED", result["state"])
        self.assertNotIn("reply_exclusion_blocker", result)
        self.assertEqual(1, self.referrals())

    def test_unrelated_persons_exclusion_does_not_block_this_correspondent(self):
        self.database.execute("INSERT INTO history VALUES ('name:taylor quinn','Taylor Quinn','Other Company',"
                              "'https://www.linkedin.com/in/taylor-quinn/','{}')")
        self.exclude("Taylor Quinn")
        task_id, unused = self.persist()
        result = self.consume(task_id)
        self.assertEqual("ROUTED", result["state"])
        self.assertEqual(1, self.referrals())


class UnnamedCorrespondentRoutingTests(ReplyExclusionRoutingTests):
    """The research schema allows an empty display name, and nothing else binds
    one. Without a resolvable name the `name:` suppression space cannot be
    probed at all, so the send must wait for investigation instead of advancing.
    """

    PERSON = ""

    LIVE_EXCLUSIONS = ("Ana Casey Moreau", "Casey Moreau", "casey.moreau@moneypenny.example",
                       "Taylor Quinn", "Robert Vance")

    def live_exclusions(self):
        for recipient in self.LIVE_EXCLUSIONS:
            self.exclude(recipient)

    def test_unresolvable_correspondent_identity_holds_for_investigation(self):
        self.live_exclusions()
        task_id, unused = self.persist()
        result = self.consume(task_id)
        self.assertEqual("REVIEW", result["state"])
        self.assertEqual("REPLY_RECIPIENT_IDENTITY_UNRESOLVED", result.get("reply_exclusion_blocker"))
        self.assertEqual(0, self.referrals())
        task = routing.one(self.database, "SELECT * FROM workflow_tasks WHERE task_id=?", (result["review_task_id"],))
        self.assertEqual(("pipeline_routing_review", "QUEUED", "READ", "REPLY_RECIPIENT_IDENTITY_UNRESOLVED"),
                         (task["worker"], task["status"], task["effect"], task["blocker"]))
        self.assertTrue(task["next_action"])

    def test_recording_the_name_clears_the_hold_without_reopening_anything(self):
        self.live_exclusions()
        self.database.execute("INSERT INTO rw_contacts VALUES ('mock company',?,'mock person',1,NULL)",
                              (research.profile_key(PROFILE),))
        self.database.commit()
        task_id, unused = self.persist()
        result = self.consume(task_id)
        self.assertEqual("ROUTED", result["state"])
        self.assertEqual(1, self.referrals())

    def test_empty_name_exclusion_space_does_not_manufacture_a_hold(self):
        self.database.execute("INSERT INTO suppressions VALUES (?,?)",
                              (orchestrator.profile_key(PROFILE), "Previously contacted; no repeat initial outreach"))
        self.database.commit()
        task_id, unused = self.persist()
        result = self.consume(task_id)
        self.assertEqual("ROUTED", result["state"])
        self.assertNotIn("reply_exclusion_blocker", result)
        self.assertEqual(1, self.referrals())

    def test_unresolvable_identity_still_sees_a_profile_keyed_exclusion(self):
        """Preserved: profile-keyed suppression never depended on a name."""
        self.database.execute("INSERT INTO suppressions VALUES (?,?)",
                              (orchestrator.profile_key(PROFILE), "Existing user exclusion"))
        self.database.commit()
        task_id, unused = self.persist()
        self.assertEqual("REVIEW", self.consume(task_id)["state"])
        self.assertEqual(0, self.referrals())

    # Cases whose meaning depends on a named correspondent do not apply here.
    test_display_name_exclusion_is_not_missed_by_the_word_split_key = None
    test_exclusion_bound_only_to_the_durable_contact_record_is_found = None
    test_seeded_exclusion_literals_still_hold = None
    test_seeded_literal_bound_only_to_the_contact_record_is_found = None
    test_opted_out_correspondent_never_advances_conversion = None
    test_previously_contacted_reason_still_lets_the_reply_decision_route = None
    test_named_correspondent_without_any_suppression_still_routes = None
    test_unrelated_persons_exclusion_does_not_block_this_correspondent = None
