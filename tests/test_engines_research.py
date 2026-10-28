"""U1 phase 2: the research launcher on the engine layer changes nothing.

The REFERENCE_* functions are verbatim copies from main f264681 (orchestrator.research_command and
run_research_worker's environment, research_recovery's command rewrite, research_tools.prepare's
settings and attach, research_workflow.worker_never_reached_model's log reading). Each test runs the
engine-layer code and its reference on the same input and asserts identical results.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import contextlib
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import engines
import openai_auth
import orchestrator
import research_recovery
import research_tools
import research_workflow
import resources
import text_model
import user_profile


def REFERENCE_research_command(root, config, output_path, schema):
    return [config["codex"], "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
               "--skip-git-repo-check", "--sandbox", "read-only", "--model", config["model"],
               "-c", 'approval_policy="never"', "-c", 'web_search="live"',
               "-c", "features.shell_tool=false", "-c", "features.apps=false",
               "-c", "features.skills=false", "-c", "features.apply_patch_freeform=false",
               "-c", 'model_reasoning_effort="medium"',
               "--output-schema", str(resources.path(schema) if resources.declared(str(schema)) else root / schema),
               "--output-last-message", str(output_path), "--color", "never", "-"]


def REFERENCE_research_environment(command):
    environment = {key: value for key, value in os.environ.items() if key not in {
        "OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID",
        "OPENAI_ORGANIZATION", "OPENAI_PROJECT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT"}}
    return openai_auth.environment(command, environment)


def REFERENCE_recovery_command(config, command):
    command = list(command)
    for index, setting in enumerate(command):
        if setting == 'web_search="live"':
            command[index] = 'web_search="disabled"'
    settings = ('forced_login_method="chatgpt"', 'model_provider="openai"', 'web_search="disabled"',
                "features.shell_tool=false", "features.apps=false", "features.skills=false",
                "features.apply_patch_freeform=false", "features.unified_exec=false",
                "features.multi_agent=false", "mcp_servers={}")
    command[-1:-1] = [argument for setting in settings for argument in ("-c", setting)]
    command = openai_auth.command(text_model.command(config, command))  # E0.T8
    return command


def REFERENCE_prepare_settings(output, phase):
    SERVER = research_tools.SERVER
    argv = research_tools.server_argv(research_tools.context_path(output))
    result = ["mcp_servers." + SERVER + ".command=" + json.dumps(argv[0]),
              "mcp_servers." + SERVER + ".args=" + json.dumps(argv[1:]),
              "mcp_servers." + SERVER + ".enabled=true",
              "mcp_servers." + SERVER + ".startup_timeout_sec=20",
              "mcp_servers." + SERVER + ".tool_timeout_sec=30"]
    result.extend("mcp_servers." + SERVER + ".tools." + tool["name"] + '.approval_mode="approve"'
                  for tool in research_tools.tools(phase))
    return result


def REFERENCE_attach(command, options):
    if not isinstance(command, list) or not command or command[-1] != "-":
        return command
    settings = [part for option in options for part in ("-c", option)]
    return command[:-1] + settings + ["-"]


def REFERENCE_never_reached_model(text):
    """worker_never_reached_model's log reading (after its file checks): the proof's error fields or None."""
    MODEL_ACTIVITY_LINES = ("codex", "thinking", "exec")
    MODEL_ACTIVITY_PREFIXES = ("web search:", "tokens used")
    for line in text.splitlines():
        if line.strip() in MODEL_ACTIVITY_LINES or line.startswith(MODEL_ACTIVITY_PREFIXES):
            return None
    decoder = json.JSONDecoder()
    for match in re.finditer(r"(?m)^ERROR: (?=\{)", text):
        try:
            value, _ = decoder.raw_decode(text, match.end())
        except ValueError:
            continue
        error = value.get("error") if isinstance(value, dict) else None
        if (isinstance(error, dict) and value.get("type") == "error"
                and (error.get("type"), error.get("code"), value.get("status")) in research_workflow.PRE_MODEL_REJECTIONS):
            return {"error_type": error["type"], "error_code": error["code"], "error_status": value["status"]}
    return None


REJECTION = 'ERROR: {"type":"error","status":400,"error":{"type":"invalid_request_error","code":"invalid_json_schema","message":"bad"}}'
LOGS = {
    "rejected": "OpenAI Codex v0.200\n--------\n" + REJECTION + "\n",
    "rejected after other error": 'ERROR: {"type":"error","status":500,"error":{"type":"server_error","code":"x"}}\n' + REJECTION,
    "model ran": "codex\nthinking\n" + REJECTION,
    "web search": "web search: jobs\n" + REJECTION,
    "tokens": "tokens used\n1,234\n" + REJECTION,
    "other error": 'ERROR: {"type":"error","status":401,"error":{"type":"auth","code":"x"}}',
    "broken json": "ERROR: {not json\n",
    "not an object error": 'ERROR: {"type":"error","status":400,"error":"text"}',
    "usage limit": "ERROR: You've hit your usage limit. Upgrade or try again at Oct 28th, 2026 4:00 PM.\n",
    "empty": "",
}


class ResearchCommandParityTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        key = self.root / "openrouter.key"
        key.write_text("or-test-key\n")
        self.config = {"codex": "/Users/kyle/.npm-global/bin/codex", "model": "gpt-6"}
        self.text_model = {"text_model": {"model": "z-ai/glm-5.3", "key_file": str(key)}}
        variables = {"HOME": "/Users/kyle", "USER": "kyle", "PATH": "/usr/bin", "OPENAI_API_KEY": "strip-me",
                     "CODEX_API_KEY": "strip-me", "AZURE_OPENAI_ENDPOINT": "strip-me", "OTHER": "kept",
                     "SIDEDOOR_HOME": os.environ["SIDEDOOR_HOME"]}
        patcher = patch.dict(os.environ, variables, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def modes(self):
        yield "chatgpt"
        state = user_profile.home() / openai_auth.STATE_FILE
        state.write_text(json.dumps({"codex_auth": openai_auth.API_KEY}))
        try:
            with patch.object(openai_auth, "saved_key", return_value="sk-test"):
                self.assertTrue(openai_auth.api_key_mode())
                yield "api_key"
        finally:
            state.unlink()

    def test_discovery_qualification_and_recovery_commands_and_environments(self):
        output = self.root / "run.discovery.json"
        for mode in self.modes():
            for schema in ("discovery-schema.json", "research-schema.json", str(self.root / "decision-schema.json")):
                command = orchestrator.research_command(self.root, self.config, output, schema)
                with self.subTest(mode=mode, schema=schema):
                    self.assertEqual(command, REFERENCE_research_command(self.root, self.config, output, schema))
                    self.assertEqual(engines.CODEX.research_environment(command), REFERENCE_research_environment(command))
                    for config in (self.config, dict(self.config, **self.text_model)):
                        recovery = engines.CODEX.research_recovery_command(config, command)
                        self.assertEqual(recovery, REFERENCE_recovery_command(config, command))
                        self.assertEqual(engines.CODEX.research_environment(recovery), REFERENCE_research_environment(recovery))
                    for phase in ("discovery", "qualification"):
                        options = research_tools.prepare(output, phase, "AI", 20, {}, config)
                        self.assertEqual(options, REFERENCE_prepare_settings(output, phase))
                        attached = research_tools.attach(command, options)
                        self.assertEqual(attached, REFERENCE_attach(command, options))
                        self.assertEqual(engines.CODEX.research_environment(attached), REFERENCE_research_environment(attached))
        for odd in (None, [], ["codex"], "codex -"):
            self.assertEqual(research_tools.attach(odd, ["x=1"]), REFERENCE_attach(odd, ["x=1"]))

    def test_research_recovery_builds_its_command_through_the_engine(self):
        seen = {}

        def worker(working, command, prompt, log, timeout):
            seen["command"] = command
            raise RuntimeError("stop")

        class Runtime:
            research_command = staticmethod(orchestrator.research_command)

        for config in (self.config, dict(self.config, **self.text_model)):
            with self.subTest(text_model=config is not self.config), self.assertRaisesRegex(RuntimeError, "stop"):
                artifacts = research_recovery.create_artifacts(self.root)
                research_recovery.bounded_decision(self.root, config, "prompt", artifacts, 30,
                                                   runtime=Runtime, executor=worker)
            working = Path(artifacts["directory"]).resolve()
            expected = REFERENCE_recovery_command(config, REFERENCE_research_command(
                self.root, config, working / "decision.json", str(working / "recovery-schema.json")))
            self.assertEqual(seen["command"], expected)

    def test_run_research_worker_passes_the_old_environment(self):
        seen = {}

        class Stop(Exception):
            pass

        def popen(command, **options):
            seen.update(options)
            raise Stop()

        command = orchestrator.research_command(self.root, self.config, self.root / "out.json", "research-schema.json")
        for mode in self.modes():
            with self.subTest(mode=mode), \
                    patch("provider_pacing.slot", return_value=contextlib.nullcontext()), \
                    patch.object(orchestrator.subprocess, "Popen", side_effect=popen), self.assertRaises(Stop):
                orchestrator.run_research_worker(self.root, command, "prompt", self.root / "w.log", 30)
            self.assertEqual(seen["env"], REFERENCE_research_environment(command))
            self.assertNotIn("OPENAI_API_KEY", seen["env"])


class ResearchLogParityTests(unittest.TestCase):
    def test_worker_never_reached_model_reads_the_log_as_before(self):
        with tempfile.TemporaryDirectory() as folder:
            for name, text in LOGS.items():
                log = Path(folder) / (name.replace(" ", "-") + ".log")
                log.write_text(text)
                with self.subTest(log=name):
                    proof = research_workflow.worker_never_reached_model(log, None)
                    expected = REFERENCE_never_reached_model(text)
                    self.assertEqual(None if proof is None else {key: proof[key] for key in expected or {}}
                                     if expected else proof, expected)
        self.assertIsNotNone(REFERENCE_never_reached_model(LOGS["rejected"]))   # the check is not vacuous


if __name__ == "__main__":
    unittest.main()
