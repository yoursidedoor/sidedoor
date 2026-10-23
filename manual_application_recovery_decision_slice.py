"""Offline manual validation of the pure application coordinator decision slice."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

import application_recovery_decision


CONFIG = {"codex": "/MOCK_DO_NOT_EXECUTE/codex", "model": "synthetic-model"}


def packet():
    return {"job": {"job_key": "synthetic-role", "candidate_email": "synthetic@example.invalid"},
            "original_blocker": "REQUISITION_UNKNOWN", "previous_findings": [],
            "previous_decisions": [], "deadline": "2026-09-18T12:00:00+00:00",
            "supported_methods": {"official_posting": "Read observed official posting",
                                  "observed_links": "Read observed official links"}}


def decision(**changes):
    return dict({"action": "RETRY", "method": "official_posting",
                 "instructions": "Read the observed posting for the exact requisition",
                 "reason": "An existing scoped read can investigate the original blocker",
                 "progress": False}, **changes)


def executor_for(value, calls):
    def execute(working, command, prompt, log, timeout):
        assert 0 < timeout <= 30
        assert 'web_search="live"' not in command
        for setting in ('forced_login_method="chatgpt"', 'model_provider="openai"',
                        'web_search="disabled"', "features.shell_tool=false", "features.apps=false",
                        "features.skills=false", "features.apply_patch_freeform=false",
                        "features.unified_exec=false", "features.multi_agent=false", "mcp_servers={}"):
            assert setting in command, setting
        assert (working / "prompt.txt").read_text() == prompt
        assert json.loads((working / "recovery-schema.json").read_text()) == application_recovery_decision.DECISION_SCHEMA
        output = Path(command[command.index("--output-last-message") + 1])
        output.write_text(json.dumps(value))
        log.write_text("Synthetic tools-disabled executor trace; no process launched\n")
        calls.append({"timeout": timeout, "prompt": prompt})
    return execute


def run_manual():
    calls = []
    with tempfile.TemporaryDirectory(prefix="application-decision-slice-") as temporary:
        root = Path(temporary)
        with patch("subprocess.Popen", side_effect=AssertionError("No real executor")), \
                patch("sqlite3.connect", side_effect=AssertionError("No database")), \
                patch("socket.socket", side_effect=AssertionError("No network")):
            initial = packet()
            original = json.dumps(initial, sort_keys=True)
            with ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(application_recovery_decision.decide, root, CONFIG, initial,
                                     30, executor_for(decision(), calls)).result()
            assert json.dumps(initial, sort_keys=True) == original
            assert result["action"] == "RETRY" and result["progress"] is False
            assert all(Path(reference).exists() for reference in result["artifacts"].values())
            followup = packet()
            followup["previous_decisions"] = [result]
            followup["previous_findings"] = [{"supported_alternative": decision(),
                                               "result": {"status": "FAILED", "reason": "Posting unreadable"}}]
            changed = decision(instructions="Read the already observed alternate official requisition page")
            assert application_recovery_decision.decide(
                root, CONFIG, followup, executor=executor_for(changed, calls))["method"] == "official_posting"
            try:
                application_recovery_decision.decide(root, CONFIG, followup, executor=executor_for(decision(), calls))
            except ValueError as error:
                assert "unchanged" in str(error)
                assert Path(error.artifacts["raw"]).is_file()
            else:
                raise AssertionError("Literal unchanged retry accepted")
            no_route = packet()
            no_route["supported_methods"] = {}
            abandon = decision(action="ABANDON", method="", instructions="", reason="No supported route")
            assert application_recovery_decision.decide(
                root, CONFIG, no_route, executor=executor_for(abandon, calls))["action"] == "ABANDON"
            for invalid in (decision(method="invented_tool"), decision(action="USER_BLOCKED"),
                            decision(progress="yes"), dict(decision(), authorized=True)):
                try:
                    application_recovery_decision.decide(root, CONFIG, packet(), executor=executor_for(invalid, calls))
                except ValueError as error:
                    assert Path(error.artifacts["prompt"]).is_file()
                else:
                    raise AssertionError("Invalid output accepted")
    return {"state": "PASS", "mock_decisions": len(calls), "database_calls": 0,
            "network_calls": 0, "real_model_calls": 0, "same_method_new_steps": "ACCEPTED",
            "unchanged_loop": "REJECTED"}


if __name__ == "__main__":
    print(json.dumps(run_manual()))
