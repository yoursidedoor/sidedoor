import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import copy
import json
import socket
import sqlite3
import unittest
from unittest.mock import patch

import champion_composition
import conversion
import engagement
import pipeline_routing as routing
import reply_worker
import resources
import test_pipeline_routing as pipeline_fixtures
import test_reply_worker as worker_fixtures
import test_reply_worker_review as review_fixtures
from backlog_fixture import setUpModule, tearDownModule  # noqa: F401 (fixture messages predate the reply cutoff)
import synthetic_resume  # noqa: F401 (synthetic designated resume; the owner's is not in the repo)


MESSAGE = "Yes, I can advocate for you. Send a short summary."
UNKNOWN_HOLDS = ["UNKNOWN_QUOTED_CONTEXT", "UNKNOWN_FORWARDED_CONTEXT"]


class ChampionSpeakerContextTests(unittest.TestCase):
    def setUp(self):
        self.request = {"source_sha256": "a" * 64, "champion_handoff_enabled": True,
                        "job_scope": [{"job_key": "verified-role"}], "context_issues": []}
        self.flags(None, None)
        self.decision = review_fixtures.inference(self.request["source_sha256"], MESSAGE)
        self.decision.update(role_reference="verified-role", speaker_context="DIRECT_SPEAKER", champion_handoff=None)

    def flags(self, quoted, forwarded):
        self.request["source_snapshot"] = {"payload": json.dumps({"inbox": {
            "text": MESSAGE, "quoted": quoted, "forwarded": forwarded}})}

    def validate(self, holds=UNKNOWN_HOLDS):
        return reply_worker._validate(json.dumps(self.decision), self.request, MESSAGE, holds)

    def test_unknown_provider_flags_can_be_adjudicated_without_mutating_source(self):
        original = copy.deepcopy(self.request)
        for label in ("AGREEMENT", "DECLINE"):
            with self.subTest(label=label):
                self.decision["label"] = label
                self.assertTrue(champion_composition.direct_intent(self.decision, self.request))
                if label == "AGREEMENT":
                    # The summary is demanded only when the packet carries the role evidence a
                    # summary needs (owner, 2026-09-23). This fixture supplies no
                    # `role_context.evidence_id`, and the old unconditional Held(retry=True)
                    # would have burned every attempt asking for something unproducible - there
                    # are zero ROLE_CONTEXT rows in the live database. It now advances instead.
                    handoff = self.validate()
                    self.assertTrue(handoff["can_advance_conversion"])
                    self.assertFalse(handoff["can_send"])
                    continue
                handoff = self.validate()
                self.assertTrue(handoff["can_advance_conversion"])
                self.assertFalse(handoff["can_send"])
                self.assertEqual([], handoff["observations"])
                self.assertEqual("DIRECT_SPEAKER", handoff["classification"]["speaker_context"])
        self.assertEqual(original, self.request)

    def test_explicit_uncertainty_overrides_false_provider_flags(self):
        self.flags(False, False)
        for context, hold in (("UNKNOWN", "UNKNOWN_SPEAKER_CONTEXT"),
                              ("QUOTED_OR_FORWARDED", "CONTEXTUAL_QUOTED_OR_FORWARDED")):
            with self.subTest(context=context):
                self.decision["speaker_context"] = context
                self.assertFalse(champion_composition.direct_intent(self.decision, self.request))
                handoff = self.validate([])
                self.assertFalse(handoff["can_advance_conversion"])
                # The signal is recorded, not blocking (owner, 2026-09-23): every former hold is an
                # `observation` now. can_advance_conversion above is the property that matters.
                self.assertIn(hold, handoff["observations"])

    def test_absent_new_fields_preserve_legacy_decision_and_strict_unknown_flags(self):
        self.decision.pop("speaker_context")
        self.decision.pop("champion_handoff")
        self.request.pop("champion_handoff_enabled")
        handoff = self.validate()
        self.assertEqual(self.decision, handoff["classification"])
        self.assertEqual(sorted(UNKNOWN_HOLDS), handoff["observations"])
        self.assertFalse(handoff["can_advance_conversion"])
        self.assertFalse(champion_composition.direct_intent(self.decision, self.request))
        self.flags(False, False)
        self.assertTrue(champion_composition.direct_intent(self.decision, self.request))
        self.assertTrue(self.validate([])["can_advance_conversion"])

    def test_new_semantic_judgment_requires_enabled_request_and_supported_intent(self):
        for enabled in (False, None, "true", 1):
            with self.subTest(enabled=enabled):
                self.request["champion_handoff_enabled"] = enabled
                self.assertFalse(champion_composition.direct_intent(self.decision, self.request))
                self.assertFalse(self.validate()["can_advance_conversion"])
        self.request.pop("champion_handoff_enabled")
        self.assertFalse(champion_composition.direct_intent(self.decision, self.request))
        self.request["champion_handoff_enabled"] = True
        for label in ("AMBIGUOUS", "QUESTION", "OTHER", "QUOTED_OR_FORWARDED"):
            with self.subTest(label=label):
                self.decision["label"] = label
                self.assertFalse(champion_composition.direct_intent(self.decision, self.request))
                self.assertFalse(self.validate()["can_advance_conversion"])

    def test_known_true_or_malformed_flags_cannot_be_overridden(self):
        for quoted, forwarded in ((True, None), (None, True), (True, True), (0, None), (None, "false")):
            with self.subTest(quoted=quoted, forwarded=forwarded):
                self.flags(quoted, forwarded)
                self.assertFalse(champion_composition.direct_intent(self.decision, self.request))
                self.assertFalse(self.validate()["can_advance_conversion"])

    def test_speaker_unknowns_and_context_conflicts_prevent_adjudication(self):
        for requirement in ("QUOTED_CONTEXT", "FORWARDED_CONTEXT", "AGREEMENT_INTENT"):
            with self.subTest(requirement=requirement):
                self.decision["unknown_factual_requirements"] = [requirement]
                self.assertFalse(champion_composition.direct_intent(self.decision, self.request))
                self.assertFalse(self.validate()["can_advance_conversion"])
        self.decision["unknown_factual_requirements"] = []
        self.request["context_issues"] = [{"blocker": "CONFLICTING_CONVERSATION_CONTEXT"}]
        self.assertFalse(champion_composition.direct_intent(self.decision, self.request))
        self.assertFalse(self.validate()["can_advance_conversion"])

    def test_only_unknown_provider_flag_holds_are_removed(self):
        for hold in ("AUTHENTICATED_MESSAGE_BINDING_REQUIRED", "CONFLICTING_CONVERSATION_CONTEXT",
                     "QUOTED_CONTEXT_REQUIRES_ADJUDICATION", "FORWARDED_CONTEXT_REQUIRES_ADJUDICATION"):
            with self.subTest(hold=hold):
                handoff = self.validate(UNKNOWN_HOLDS + [hold])
                self.assertEqual([hold], handoff["observations"])
                self.assertFalse(handoff["can_advance_conversion"])

    def test_semantic_judgment_requires_source_binding(self):
        original = copy.deepcopy(self.decision)
        variants = ({"source_sha256": "b" * 64},)
        for changes in variants:
            with self.subTest(changes=changes):
                self.decision = {**original, **changes}
                self.assertFalse(champion_composition.direct_intent(self.decision, self.request))
                with self.assertRaises(reply_worker.Held):
                    self.validate()

    def test_invalid_speaker_context_is_not_a_legacy_absence(self):
        for context in (None, True, [], {}, "DIRECT", ""):
            with self.subTest(context=context):
                self.decision["speaker_context"] = context
                with self.assertRaises(reply_worker.Held) as raised:
                    self.validate()
                self.assertEqual("CLASSIFICATION_SPEAKER_CONTEXT_INVALID", raised.exception.code)

    def test_champion_metadata_uses_same_source_bound_semantic_judgment(self):
        self.request.update(decision_time=pipeline_fixtures.NOW,
                            candidate_resume={"text": "Led a verified platform migration."})
        self.request["job_scope"][0]["role_context"] = {
            "evidence_id": "fixture-role-evidence", "facts": {"source_proofs": {
                "team_need": {"value": "Reliable services", "excerpt": "Reliable services"}}}}
        self.decision.update(champion_handoff={"job_key": "verified-role",
            "resume_quotes": ["Led a verified platform migration."], "role_quotes": ["Reliable services"]},
            reply_proposal={"text": "Fixture summary only.", "basis_quote": self.decision["quote_span"]})
        self.assertTrue(self.validate()["can_advance_conversion"])
        self.flags(False, False)
        for context in ("UNKNOWN", "QUOTED_OR_FORWARDED"):
            with self.subTest(context=context):
                self.decision["speaker_context"] = context
                with self.assertRaises(reply_worker.Held) as raised:
                    self.validate([])
                self.assertEqual("CHAMPION_EXACT_AGREEMENT_REQUIRED", raised.exception.code)


class ChampionLegacyMigrationTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(socket.socket, "connect", side_effect=AssertionError("No network in migration tests")))

    def fixture(self, legacy=True, template="Immutable legacy fixture prompt.", schema_sha256="a" * 64, champion=True):
        worker = worker_fixtures.ReplyWorkerTests()
        self.addCleanup(worker.doCleanups)
        worker.setUp()
        worker.config.update(conversion={"champion_handoff_enabled": champion},
                             sender_profile="https://www.linkedin.com/in/fixture-candidate")
        task_id = worker.enqueue(MESSAGE, flags={"quoted": None, "forwarded": None})
        conversion.cycle(worker.database, worker.config, pipeline_fixtures.NOW)
        worker.database.commit()
        request, unused_message, unused_holds = reply_worker._source(
            worker.database, worker.task(task_id), worker.config, pipeline_fixtures.NOW)
        if legacy:
            for field in ("champion_handoff_enabled", "message_text", "message_full_span", "source_span_resolution"):
                request.pop(field, None)
            for item in request["job_scope"]:
                for field in ("role_context", "candidate_profile", "application_interview_context"):
                    item.pop(field, None)
        reply_worker.initialize(worker.database)
        saved_input = template + "\n\nUNTRUSTED_SOURCE_JSON:\n" + routing.encoded(request)
        # A read saved while the reply AI ran through codex (the executable, no route).
        provenance = {"model": reply_worker.text_model.model(worker.config), "executable": worker.config["codex"],
                      "auth": reply_worker.text_model.auth(worker.config), "model_source": "configured_cli_argument",
                      "output_version": reply_worker.VERSION, "prompt_sha256": reply_worker.sha(template),
                      "schema_sha256": schema_sha256}
        worker.database.execute("INSERT INTO reply_worker_inputs VALUES (?,?,?,?,?)",
            (task_id, reply_worker.sha(saved_input), saved_input, request["source_sha256"], routing.encoded(provenance)))
        worker.database.commit()
        return worker, task_id, request

    def saved(self, worker, task_id):
        return routing.one(worker.database, "SELECT * FROM reply_worker_inputs WHERE task_id=?", (task_id,))

    def assert_no_revision(self, worker, task_id):
        saved = self.saved(worker, task_id)
        result = worker.run_cycle()
        self.assertEqual("CLASSIFICATION_IMMUTABLE_INPUT_CHANGED", result["items"][0]["blocker"])
        self.assertIsNone(result["items"][0]["context_revision_task_id"])
        self.assertEqual(saved, self.saved(worker, task_id))
        self.assertEqual([], worker.processes)

    def test_bounded_revision_preserves_audit_and_resumes_once_after_restart(self):
        worker, task_id, legacy = self.fixture()
        saved = self.saved(worker, task_id)
        current, unused_message, unused_holds = reply_worker._source(
            worker.database, worker.task(task_id), worker.config, pipeline_fixtures.NOW)
        self.assertEqual(legacy, current)
        result = worker.run_cycle()
        successor_id = result["items"][0]["context_revision_task_id"]
        self.assertIsNotNone(successor_id)
        self.assertEqual(saved, self.saved(worker, task_id))
        prior, successor = worker.task(task_id), worker.task(successor_id)
        self.assertEqual("CANCELLED", prior["status"])
        self.assertEqual(0, prior["attempts"])
        self.assertEqual(prior["deadline"], successor["deadline"])
        self.assertEqual(prior["max_attempts"] - prior["attempts"], successor["max_attempts"])
        self.assertEqual("READ", successor["effect"])
        bound = json.loads(successor["inputs"])[0]
        self.assertEqual(task_id, bound["context_revision_task_id"])
        self.assertEqual(saved["input_sha256"], bound["context_revision_input_sha256"])
        attempts = routing.rows(worker.database, "SELECT * FROM reply_worker_attempts WHERE task_id=?", (task_id,))
        self.assertEqual([], attempts)
        self.assertEqual([], worker.processes)
        worker.database.close()
        worker.database = sqlite3.connect(worker.path)
        worker.database.row_factory = sqlite3.Row
        def compose(output):
            packet = json.loads(worker.prompts[-1].split("UNTRUSTED_SOURCE_JSON:\n", 1)[1])
            scope = packet["job_scope"][0]
            quote = next(line.strip() for line in packet["candidate_resume"]["text"].splitlines() if len(line.strip()) > 60)
            span = {**output["quote_span"], "start": None, "end": None}
            output.update(label="AGREEMENT", speaker_context="DIRECT_SPEAKER", quote_span=span,
                champion_handoff={"job_key": scope["job_key"], "resume_quotes": [quote],
                                  "role_quotes": [scope["role_context"]["facts"]["title"]]},
                reply_proposal={"text": "Fixture summary: " + quote, "basis_quote": dict(span)})

        worker.mutate_output = compose
        worker.run_cycle()
        self.assertEqual("COMPLETE", worker.task(successor_id)["status"])
        self.assertEqual(1, len(worker.processes))
        packet = json.loads(worker.prompts[0].split("UNTRUSTED_SOURCE_JSON:\n", 1)[1])
        self.assertTrue(packet["champion_handoff_enabled"])
        self.assertEqual(MESSAGE, packet["message_text"])
        self.assertEqual("EXACT_UNIQUE_QUOTE", packet["source_span_resolution"])
        self.assertIsNotNone(packet["job_scope"][0]["role_context"]["evidence_id"])
        handoff = worker.handoff(successor_id)
        self.assertFalse(handoff["can_send"])
        raw_attempt = routing.one(worker.database, "SELECT * FROM reply_worker_attempts WHERE task_id=?", (successor_id,))
        self.assertIsNone(json.loads(raw_attempt["output"])["quote_span"]["start"])
        self.assertEqual(reply_worker.sha(raw_attempt["output"]), handoff["output_sha256"])
        # The agreement is recorded on the person, bound to this decision (owner, 2026-09-25).
        referral = routing.one(worker.database, "SELECT * FROM referrals WHERE source_key=?", (handoff["source_key"],))
        self.assertEqual("AGREED", referral["state"])
        provenance = json.loads(referral["history"])[-1]["evidence"]["decision_provenance"]
        self.assertEqual((successor_id, handoff["output_sha256"]), (provenance["task_id"], provenance["output_sha256"]))
        self.assertEqual(legacy["source_key"], handoff["source_key"])
        worker.run_cycle()
        self.assertEqual(1, len(worker.processes))
        self.assertEqual(saved, self.saved(worker, task_id))
        self.assertEqual(attempts, routing.rows(worker.database,
            "SELECT * FROM reply_worker_attempts WHERE task_id=?", (task_id,)))

    def test_disabled_feature_or_changed_model_does_not_upgrade_legacy_assets(self):
        # The flag no longer gates an asset-only continuation (review B1, 2026-10-27); a model change still does.
        for change in ({"text_model": {"model": "changed-model"}},):
            with self.subTest(change=change):
                worker, task_id, unused_request = self.fixture()
                worker.config.update(change)
                self.assert_no_revision(worker, task_id)

    def test_changed_conversation_is_not_silently_combined_with_asset_upgrade(self):
        worker, task_id, unused_request = self.fixture()
        engagement.record_event(worker.database, pipeline_fixtures.message(
            "Could you clarify the role first?", "changed-context"))
        worker.database.commit()
        self.assert_no_revision(worker, task_id)

    def test_last_remaining_attempt_migrates_without_consuming_it_or_erasing_failures(self):
        for maximum, attempts in ((1, 0), (3, 2)):
            with self.subTest(maximum=maximum, attempts=attempts):
                worker, task_id, unused_request = self.fixture()
                saved = self.saved(worker, task_id)
                worker.database.execute("UPDATE workflow_tasks SET attempts=?,max_attempts=? WHERE task_id=?",
                                        (attempts, maximum, task_id))
                for attempt in range(1, attempts + 1):
                    worker.database.execute("INSERT INTO reply_worker_attempts VALUES (?,?,?,?,?,?,?,?)",
                        (task_id, attempt, pipeline_fixtures.NOW, saved["input_sha256"], None, None,
                         saved["provenance"], "CLASSIFICATION_TIMEOUT"))
                worker.database.commit()
                previous = routing.rows(worker.database, "SELECT * FROM reply_worker_attempts ORDER BY attempt")
                result = worker.run_cycle()
                successor = worker.task(result["items"][0]["context_revision_task_id"])
                self.assertEqual(attempts, worker.task(task_id)["attempts"])
                self.assertEqual(1, successor["max_attempts"])
                self.assertEqual(0, successor["attempts"])
                self.assertEqual(previous, routing.rows(worker.database, "SELECT * FROM reply_worker_attempts ORDER BY attempt"))
                self.assertEqual(0, worker.database.execute("SELECT COUNT(*) FROM worker_compute_reservations").fetchone()[0])

    def test_exhausted_attempt_budget_does_not_create_new_task(self):
        worker, task_id, unused_request = self.fixture()
        worker.database.execute("UPDATE workflow_tasks SET attempts=max_attempts WHERE task_id=?", (task_id,))
        worker.database.commit()
        before = worker.database.execute("SELECT COUNT(*) FROM workflow_tasks").fetchone()[0]
        result = worker.run_cycle()
        self.assertEqual("EXHAUSTED", result["items"][0]["state"])
        self.assertEqual(before, worker.database.execute("SELECT COUNT(*) FROM workflow_tasks").fetchone()[0])
        self.assertEqual([], worker.processes)

    def test_a_read_saved_under_codex_continues_once_as_a_direct_call(self, champion=True):
        # 2026-10-27: same prompt and schema, only the route changed (codex executable -> direct call).
        schema = resources.read_text("reply-schema.json")
        template = (resources.read_text("reply-prompt.md") + "\n" + champion_composition.INDEPENDENT_REFERRAL_INSTRUCTIONS
                    + "\n" + reply_worker.TOOLS_OUTPUT_INSTRUCTIONS + "\n" + schema)
        worker, task_id, unused_request = self.fixture(legacy=False, template=template,
                                                       schema_sha256=reply_worker.sha(schema), champion=champion)
        result = worker.run_cycle()
        self.assertEqual("CONTEXT_REVISED", result["items"][0]["state"])
        self.assertEqual([], worker.processes)
        successor = result["items"][0]["context_revision_task_id"]
        worker.run_cycle()
        self.assertEqual("COMPLETE", worker.task(successor)["status"])
        self.assertEqual(1, len(worker.processes))
        provenance = worker.handoff(successor)["model_provenance"]
        self.assertEqual((None, reply_worker.DIRECT_ROUTE), (provenance["executable"], provenance["route"]))
        worker.run_cycle()
        self.assertEqual(1, len(worker.processes))   # recomputed once, not again

    def test_a_read_saved_under_codex_continues_with_the_champion_flag_off(self):
        # Review B1: the route-only continuation does not depend on champion_handoff_enabled.
        self.test_a_read_saved_under_codex_continues_once_as_a_direct_call(champion=False)

    def test_current_format_saved_input_is_upgraded_when_the_assets_change(self):
        # Owner, 2026-09-24: "requeue the 4". Only legacy packets used to be upgraded, so a
        # current-format read stranded on CLASSIFICATION_IMMUTABLE_INPUT_CHANGED after a prompt change.
        worker, task_id, unused_request = self.fixture(legacy=False)
        result = worker.run_cycle()
        self.assertEqual("CONTEXT_REVISED", result["items"][0]["state"])
        self.assertEqual([], worker.processes)

