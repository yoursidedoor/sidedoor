import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import application_recovery_decision
import manual_application_recovery_decision_slice as manual
import research_recovery


class ApplicationRecoveryDecisionSliceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="application-decision-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.calls = []
        self.addCleanup(patch.stopall)
        patch("subprocess.Popen", side_effect=AssertionError("No real executor")).start()
        patch("sqlite3.connect", side_effect=AssertionError("No database")).start()
        patch("socket.socket", side_effect=AssertionError("No network")).start()

    def decide(self, value, packet=None, timeout=30):
        return application_recovery_decision.decide(
            self.root, manual.CONFIG, packet if packet is not None else manual.packet(), timeout,
            manual.executor_for(value, self.calls))

    def test_manually_validated_offline_scenarios(self):
        self.assertEqual(manual.run_manual()["state"], "PASS")

    def test_same_capability_different_steps_preserves_honest_progress(self):
        packet = manual.packet()
        packet["previous_decisions"] = [{"decision": manual.decision()}]
        result = self.decide(manual.decision(instructions="Inspect a different observed official link"), packet)
        self.assertEqual(result["method"], "official_posting")
        self.assertIs(result["progress"], False)

    def test_unchanged_finding_rejected_even_without_prior_decision(self):
        packet = manual.packet()
        packet["previous_findings"] = [{"supported_alternative": manual.decision()}]
        with self.assertRaisesRegex(ValueError, "unchanged"):
            self.decide(manual.decision(), packet)

    def test_whitespace_does_not_hide_unchanged_retry(self):
        packet = manual.packet()
        packet["previous_decisions"] = [manual.decision()]
        with self.assertRaisesRegex(ValueError, "unchanged"):
            self.decide(manual.decision(instructions="  " + manual.decision()["instructions"] + "\n"), packet)

    def test_parent_timeout_is_respected_without_reservation(self):
        self.decide(manual.decision(), timeout=0.25)
        self.assertEqual(self.calls[0]["timeout"], 0.25)
        with self.assertRaises(TimeoutError):
            self.decide(manual.decision(), timeout=0)
        self.assertEqual(len(self.calls), 1)

    def test_failure_retains_prompt_schema_trace_and_partial_output(self):
        def failed(working, command, prompt, log, timeout):
            (working / "decision.json").write_text("{partial")
            log.write_text("Synthetic timeout")
            raise TimeoutError("Synthetic timeout")

        with self.assertRaises(TimeoutError) as caught:
            application_recovery_decision.decide(self.root, manual.CONFIG, manual.packet(), executor=failed)
        artifacts = caught.exception.artifacts
        self.assertEqual(Path(artifacts["raw"]).read_text(), "{partial")
        self.assertEqual(Path(artifacts["trace"]).read_text(), "Synthetic timeout")
        self.assertIn("original_blocker", Path(artifacts["prompt"]).read_text())
        # The application form, not the research one (owner, 2026-09-27): the research form's
        # enum is RETRY/ABANDON, which made RELEASE impossible to choose (found, 2026-09-25).
        schema = json.loads(Path(artifacts["schema"]).read_text())
        self.assertEqual(schema, application_recovery_decision.DECISION_SCHEMA)
        self.assertEqual(schema["properties"]["action"]["enum"], ["RETRY", "ABANDON", "RELEASE"])
        self.assertEqual(research_recovery.DECISION_SCHEMA["properties"]["action"]["enum"], ["RETRY", "ABANDON"])

    def test_packet_cannot_redirect_artifacts(self):
        packet = manual.packet()
        packet["artifacts"] = {"directory": str(self.root / "outside")}
        result = self.decide(manual.decision(), packet)
        self.assertEqual(Path(result["artifacts"]["directory"]).parent,
                         self.root / "reports" / "application-recovery")
        self.assertFalse((self.root / "outside").exists())

    def test_output_envelope_keeps_artifacts_out_of_ai_schema(self):
        result = self.decide(manual.decision())
        self.assertEqual(set(result), set(research_recovery.DECISION_SCHEMA["required"]) | {"artifacts"})
        raw = json.loads(Path(result["artifacts"]["raw"]).read_text())
        self.assertEqual(raw, manual.decision())
        second = self.decide(manual.decision())
        self.assertNotEqual(result["artifacts"]["directory"], second["artifacts"]["directory"])
        self.assertEqual(json.loads(Path(result["artifacts"]["raw"]).read_text()), raw)

    def test_no_methods_does_not_allow_retry(self):
        packet = manual.packet()
        packet["supported_methods"] = {}
        with self.assertRaises(ValueError):
            self.decide(manual.decision(), packet)

    def test_abandon_cannot_carry_dispatch_instructions(self):
        with self.assertRaisesRegex(ValueError, "abandonment"):
            self.decide(manual.decision(action="ABANDON"))

    def test_research_repeated_method_rule_is_unchanged(self):
        with self.assertRaisesRegex(ValueError, "different supported method"):
            research_recovery.validate_decision(
                manual.decision(instructions="Different steps"), manual.packet()["supported_methods"],
                [{"decision": manual.decision()}])

    def test_missing_history_cannot_silently_look_like_first_decision(self):
        for field in ("previous_findings", "previous_decisions"):
            packet = manual.packet()
            packet.pop(field)
            with self.assertRaisesRegex(ValueError, "missing explicit fields"):
                self.decide(manual.decision(), packet)
        self.assertEqual(self.calls, [])

    def test_nested_lifecycle_requires_explicit_parent_normalization(self):
        with self.assertRaisesRegex(ValueError, "missing explicit fields"):
            self.decide(manual.decision(), {"job": {}, "read_only": True, "recovery": manual.packet()})
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
