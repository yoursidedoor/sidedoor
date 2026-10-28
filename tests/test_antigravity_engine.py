"""U1 phase 4: the Antigravity (agy) engine.

tests/fixtures/antigravity/inspect.jsonl is a real agy 1.4.8 stream (a browser inspect on the localhost fake site,
claude-sonnet-4-6 through agy, 2026-10-28) with most streamed text deltas dropped; denied.jsonl is spike-2 run r3
(a hook-denied read and a soft-denied MCP call). Paths are scrubbed. Every test uses a temporary HOME for agy's
own files: the real ~/.gemini is never read or written.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
import unittest.mock
from unittest.mock import patch

import engines
import provider_limits
import setup_steps

FIXTURES = Path(__file__).parent / "fixtures" / "antigravity"
AGY = engines.ANTIGRAVITY


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def result_line(**result):
    return json.dumps({"event": "result", "result": dict({"conversation_id": "c1", "status": "SUCCESS"}, **result)})


class TempHome(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        patcher = patch.object(Path, "home", return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)


class CommandTest(TempHome):
    def test_command_and_workspace(self):
        program = self.home / "agy"
        program.write_text("#!/bin/sh\n")
        program.chmod(0o755)
        (self.home / ".gemini" / "config").mkdir(parents=True)
        (self.home / ".gemini" / "config" / "mcp_config.json").write_text(json.dumps(
            {"mcpServers": {"notes": {"command": "/bin/notes"}, "sidedoor_browser": {"command": "/bin/evil"}}}))
        run = self.home / "run"
        run.mkdir()
        (run / "schema.json").write_text('{"type": "object"}')
        command = AGY._built({"antigravity": str(program)}, run / "output.json", run / "agy",
                             servers={"browser": ([str(program), "browser-guard"], {"HOME": "/h"}, ["browser_click", "arm"])},
                             schema=run / "schema.json", read=True)
        self.assertEqual(list(command), [str(program), "--output-format", "stream-json", "--input-format", "stream-json",
                                         "--model", AGY.READ_MODEL, "--disable-slash-commands",
                                         "--json-schema", '{"type":"object"}', "-p", ""])
        self.assertNotIn("--dangerously-skip-permissions", command)
        self.assertEqual(command.cwd, str(run / "agy"))
        self.assertIs(engines.for_command(command), AGY)
        servers = json.loads((run / "agy" / ".agents" / "mcp_config.json").read_text())["mcpServers"]
        self.assertEqual(servers["sidedoor_browser"]["command"], str(program))   # ours win over a same-named user server
        self.assertEqual(servers["notes"], {"command": "/bin/notes"})              # A2: the user's own server
        self.assertEqual(command.allowed, {"sidedoor_browser/browser_click", "sidedoor_browser/arm", "notes/*"})
        hooks = json.loads((run / "agy" / ".agents" / "hooks.json").read_text())
        self.assertIn("agy-hook", hooks["sidedoor-policy"]["PreToolUse"][0]["hooks"][0]["command"])
        self.assertEqual(json.loads(AGY.prompt_input("hi")), {"event": "user", "message": {"role": "user", "content": "hi"}})

    def test_hook_policy(self):
        workspace = self.home / "ws"
        AGY.write_workspace(workspace, {"browser": (["/bin/g"], None, ["browser_click"])}, ("search_web",))
        policy = workspace / ".agents" / AGY.POLICY

        def decide(name, **args):
            return engines.agy_hook(policy, {"toolCall": {"name": name, "args": args}})["decision"]
        self.assertEqual(decide("call_mcp_tool", ServerName="sidedoor_browser", ToolName="browser_click"), "allow")
        self.assertEqual(decide("call_mcp_tool", ServerName="sidedoor_browser", ToolName="arm"), "deny")
        self.assertEqual(decide("search_web", query="x"), "allow")
        self.assertEqual(decide("finish"), "allow")
        self.assertEqual(decide("run_command", CommandLine="ls"), "deny")
        descriptor = str(self.home / ".gemini" / "antigravity-cli" / "mcp" / "sidedoor_browser" / "browser_click.json")
        self.assertEqual(decide("read_file", Path=descriptor), "allow")
        self.assertEqual(decide("read_file", Path=str(self.home / ".ssh" / "id_rsa")), "deny")


class TraceTest(TempHome):
    ALLOWED = AGY.write_workspace(Path(tempfile.mkdtemp()), {"browser": (["/bin/g"], None, ["browser_navigate",
                                                                                            "browser_snapshot"])}, ())

    def test_recorded_inspect(self):
        record = AGY.read_trace(fixture("inspect.jsonl"), allowed=self.ALLOWED)
        self.assertEqual([(call.id, call.server, call.tool, call.status) for call in record.tool_calls],
                         [("call_mcp_tool_2", "browser", "browser_navigate", "completed"),
                          ("call_mcp_tool_4", "browser", "browser_snapshot", "completed")])
        self.assertIn("Backend Engineer", record.tool_calls[1].result["content"][0]["text"])
        self.assertEqual(record.errors, [])
        self.assertEqual(record.answer["binding"], "bind-inspect")
        self.assertEqual(record.model, "claude-sonnet-4-6")
        self.assertIsNone(AGY.check_run(0, fixture("inspect.jsonl"), allowed=self.ALLOWED))

    def test_full_output_from_the_runs_copy(self):
        steps = self.home / "steps"
        steps.mkdir()
        (steps / "4.txt").write_text("FULL SNAPSHOT")
        record = AGY.bound(self.ALLOWED, str(steps)).read_trace(fixture("inspect.jsonl"))
        self.assertEqual(record.tool_calls[1].result["content"][0]["text"], "FULL SNAPSHOT")

    def test_tool_outside_the_list_fails_even_with_an_answer(self):
        text = fixture("inspect.jsonl").replace('"ToolName":"browser_snapshot"', '"ToolName":"browser_evaluate"')
        failure = AGY.check_run(0, text, allowed=self.ALLOWED)
        self.assertEqual(failure.code, AGY.FORBIDDEN_TOOL)

    def test_denials_fail(self):
        allowed = {"spike/save_note"}
        failure = AGY.check_run(0, fixture("denied.jsonl").replace("/Users/user", str(self.home)), allowed=allowed)
        self.assertEqual(failure.code, AGY.PERMISSION_DENIED)   # soft deny: no answer, denied_actions
        answered = fixture("inspect.jsonl").replace('"usage":{"input_tokens":24958',
                                                    '"denied_actions":[{"action":"command"}],"usage":{"input_tokens":24958')
        self.assertIn('"denied_actions"', answered)
        self.assertEqual(AGY.check_run(0, answered, allowed=self.ALLOWED).code, AGY.PERMISSION_DENIED)

    def test_no_answer_and_exit(self):
        self.assertEqual(AGY.check_run(0, result_line(response="")).code, AGY.NO_ANSWER)
        self.assertEqual(AGY.check_run(1, result_line(status="ERROR", error="unknown model")).code, AGY.API_ERROR)

    def test_waits(self):
        limit = AGY.check_run(0, result_line(status="ERROR", error="RESOURCE_EXHAUSTED: Quota exhausted"))
        self.assertEqual(limit.kind, provider_limits.RATE_LIMITED)
        limit = AGY.check_run(1, result_line(status="ERROR", error="authentication failed or timed out"))
        self.assertEqual(limit.kind, provider_limits.ANTIGRAVITY_SIGNED_OUT)

    def test_invented_evidence_ids_are_written_as_null(self):
        record = AGY.read_trace(fixture("inspect.jsonl"), allowed=self.ALLOWED)
        record.answer = dict(record.answer, evidence=[{"pointer": "/a", "quote": "q", "tool_call_id": "o9XcCsBbm"},
                                                      {"pointer": "/b", "quote": "q", "tool_call_id": "call_mcp_tool_4"}])
        path = AGY.write_answer(self.home, record)
        evidence = json.loads(path.read_text())["evidence"]
        self.assertEqual([item["tool_call_id"] for item in evidence], [None, "call_mcp_tool_4"])

    def test_run_record_round_trip(self):
        run = self.home / "run"
        run.mkdir()
        command = engines.AntigravityCommand(["agy", "-p", "", "stream-json"], cwd=str(run / "agy"),
                                             allowed={"sidedoor_browser/arm"})
        engines.record_run(run, AGY, command=command)
        bound = engines.for_run(run)
        self.assertEqual((bound.name, bound.allowed, bound.steps), ("antigravity", {"sidedoor_browser/arm"},
                                                                      str(run / "agy" / "steps")))


class OwnerPicksTest(TempHome):
    def test_n1_context_ids_are_kept(self):
        record = AGY.read_trace(fixture("inspect.jsonl"), allowed=TraceTest.ALLOWED)
        record.answer = dict(record.answer, evidence=[
            {"pointer": "/a", "quote": "q", "tool_call_id": "context:/context/external_history/0/role"},
            {"pointer": "/b", "quote": "q", "tool_call_id": "made-up"}])
        evidence = json.loads(AGY.write_answer(self.home, record).read_text())["evidence"]
        self.assertEqual([item["tool_call_id"] for item in evidence], ["context:/context/external_history/0/role", None])

    def test_n4_symlinked_settings_keep_the_link(self):
        target = self.home / "dotfiles" / "agy-settings.json"
        target.parent.mkdir()
        target.write_text(json.dumps({"model": "M"}))
        link = self.home / ".gemini" / "antigravity-cli" / "settings.json"
        link.parent.mkdir(parents=True)
        link.symlink_to(target)
        self.assertTrue(AGY.ensure_allow_rules())
        self.assertTrue(link.is_symlink())
        self.assertEqual(json.loads(target.read_text())["model"], "M")
        self.assertIn("mcp(sidedoor_browser/arm)", json.loads(target.read_text())["permissions"]["allow"])

    def test_n5_odd_user_configs_are_skipped(self):
        config = self.home / ".gemini" / "config" / "mcp_config.json"
        config.parent.mkdir(parents=True)
        for value in ({"mcpServers": ["x"]}, {"mcpServers": "x"}, ["x"], {"mcpServers": {"a": "x", "b": {"command": "/b"}}}):
            config.write_text(json.dumps(value))
            self.assertEqual(AGY.user_servers(), {"b": {"command": "/b"}} if isinstance(value, dict)
                             and isinstance(value.get("mcpServers"), dict) else {})

    def test_n6_run_config_is_private_and_removed_when_the_run_ends(self):
        workspace = self.home / "ws"
        AGY.write_workspace(workspace, {"browser": (["/bin/g"], {"SECRET": "s"}, ["browser_click"])}, ())
        config = workspace / ".agents" / "mcp_config.json"
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        command = engines.AntigravityCommand(["agy", "-p", "", "stream-json"], cwd=str(workspace))
        with patch.object(engines.ANTIGRAVITY, "record_model_call"):
            AGY.finish_run(1, "", "boom", command=command, home=self.home)   # a failed run
        self.assertFalse(config.exists())
        self.assertTrue((workspace / ".agents" / "hooks.json").exists())
        AGY.write_workspace(workspace, {}, ())
        AGY.run_ended(command)   # a stopped run (no finish_run)
        self.assertFalse(config.exists())
        engines.CODEX.run_ended(command)   # Codex and Claude: nothing


class StartFailureTest(TempHome):
    def test_n3e_config_removed_when_the_start_fails(self):
        import agent_worker
        directory = self.home / "attempt"
        directory.mkdir()
        workspace = directory / "agy"
        AGY.write_workspace(workspace, {}, ())
        command = engines.AntigravityCommand(["/nonexistent/agy", "-p", "", "stream-json"], cwd=str(workspace))
        with patch.object(engines.AntigravityEngine, "wait", return_value=None), \
                patch.object(agent_worker, "_command", return_value=command), \
                patch("subprocess.Popen", side_effect=OSError("no such file")):
            with self.assertRaises(OSError):
                agent_worker._execute({"engine": "antigravity"}, "p", directory, 10)
        self.assertFalse((workspace / ".agents" / "mcp_config.json").exists())


class ModelChoiceTest(unittest.TestCase):
    def test_m2_models_listed_saved_and_laid_over(self):
        home = Path(tempfile.mkdtemp())
        probe = lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "Fetching...\nclaude-sonnet-4-6\tClaude Sonnet 4.6\n"
                                                                       "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n", "")
        status, models = setup_steps.antigravity_probe("/x/agy", {}, run=probe)
        self.assertEqual((status, models[0]), (True, ("claude-sonnet-4-6", "Claude Sonnet 4.6")))
        setup_steps.update_state(lambda value: value.update(antigravity_models=[list(m) for m in models]), home)
        self.assertEqual(setup_steps.antigravity_model_view(setup_steps.load_state(home))[1:3],
                         (AGY.MAIN_MODEL, AGY.READ_MODEL))   # the defaults until the user picks
        setup_steps.choose_antigravity_models("gemini-3.8-flash-high", "not-listed", home)
        state = setup_steps.load_state(home)
        self.assertEqual((state.get("antigravity_model"), state.get("antigravity_read_model")), ("gemini-3.8-flash-high", None))
        config = setup_steps.with_engine({"engine": "antigravity"}, home)
        self.assertEqual(AGY.model(config), "gemini-3.8-flash-high")
        self.assertEqual(AGY.model(config, read=True), "claude-sonnet-4-6")


class ModelGoneTest(unittest.TestCase):
    def test_p3_a_pick_no_longer_listed_falls_back_to_the_default(self):
        home = Path(tempfile.mkdtemp())
        setup_steps.update_state(lambda value: value.update(antigravity_model="gone-model",
                                                            antigravity_read_model="gemini-3.8-flash-low"), home)
        state = setup_steps.load_state(home)
        self.assertEqual(setup_steps.antigravity_models_in_use(state),   # no list yet: the saved picks stand
                         ({"antigravity_model": "gone-model", "antigravity_read_model": "gemini-3.8-flash-low"}, []))
        setup_steps.update_state(lambda value: value.update(antigravity_models=[
            ["claude-opus-4-6-thinking", "Claude Opus 4.6 (Thinking)"], ["gemini-3.8-flash-low", "Gemini 3.8 Flash (Low)"]]), home)
        models, main, read, notes = setup_steps.antigravity_model_view(setup_steps.load_state(home))
        self.assertEqual((main, read), ("claude-opus-4-6-thinking", "gemini-3.8-flash-low"))
        self.assertEqual(notes, ["The model you picked is no longer offered by Antigravity, so Sidedoor uses "
                                 "Claude Opus 4.6 (Thinking)."])
        config = setup_steps.with_engine({"engine": "antigravity"}, home)
        self.assertEqual((AGY.model(config), AGY.model(config, read=True)), ("claude-opus-4-6-thinking", "gemini-3.8-flash-low"))


class ResearchDayTest(unittest.TestCase):
    def test_n2_antigravity_waits_have_their_own_day_state_and_never_spend_a_try(self):
        import datetime as dt
        import sqlite3
        import daily_schedule
        database = sqlite3.connect(":memory:")
        database.execute("CREATE TABLE research(id TEXT, started_at TEXT, state TEXT)")
        daily_schedule.initialize(database, "UTC")
        config, now = {"timezone": "UTC", "research_hour": 6}, dt.datetime.now(dt.timezone.utc)
        for kind, state, words in ((provider_limits.ANTIGRAVITY_SIGNED_OUT, "ANTIGRAVITY_SIGNED_OUT", "Sign in to Antigravity again"),
                                   (provider_limits.ANTIGRAVITY_NOT_FOUND, "ANTIGRAVITY_NOT_FOUND",
                                    "Find Antigravity again in Setup or Settings")):
            day = daily_schedule.codex_wait(database, config, now, kind=kind)
            self.assertEqual((day["state"], day["next_action"]), (state, words))
            self.assertIn(kind, daily_schedule.LONG_WAIT_KINDS)
        database.close()


class WaitTest(TempHome):
    def test_not_found_is_a_wait(self):
        with patch.object(setup_steps, "find_antigravity", return_value=None):
            wait = AGY.wait({"engine": "antigravity"}, home=self.home)
        self.assertEqual(wait.kind, provider_limits.ANTIGRAVITY_NOT_FOUND)

    def test_signed_out_probe_is_a_wait(self):
        program = self.home / "agy"
        program.write_text("#!/bin/sh\n")
        program.chmod(0o755)
        signed_out = lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", "Visit https://accounts.google.com/o/oauth2/auth?x")
        signed_in = lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "Fetching...\ngemini-3.8-flash-low\tGemini\n", "")
        config = {"engine": "antigravity", "antigravity": str(program)}
        self.assertEqual(AGY.wait(config, home=self.home, run=signed_out).kind, provider_limits.ANTIGRAVITY_SIGNED_OUT)
        self.assertIsNone(AGY.wait(config, home=self.home, run=signed_in))
        crashed = lambda *a, **k: (_ for _ in ()).throw(OSError("no"))
        self.assertIsNone(AGY.wait(config, home=self.home, run=crashed))   # a probe that cannot run decides nothing


class AllowRulesTest(TempHome):
    def test_merge_is_idempotent_and_keeps_settings(self):
        settings = self.home / ".gemini" / "antigravity-cli" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"model": "M", "permissions": {"allow": ["command(git status)"], "deny": ["x"]}}))
        added = AGY.ensure_allow_rules()
        value = json.loads(settings.read_text())
        self.assertEqual(value["model"], "M")
        self.assertEqual(value["permissions"]["deny"], ["x"])
        self.assertEqual(value["permissions"]["allow"][0], "command(git status)")
        self.assertIn("mcp(sidedoor_browser/arm)", added)
        self.assertIn("mcp(sidedoor_research_tools/save_role)", added)
        self.assertTrue(all(rule.startswith("mcp(sidedoor_") and "*" not in rule for rule in added))
        before = settings.read_text()
        self.assertEqual(AGY.ensure_allow_rules(), [])
        self.assertEqual(settings.read_text(), before)

    def user_config(self, servers):
        config = self.home / ".gemini" / "config" / "mcp_config.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({"mcpServers": servers}))

    def test_n3_user_server_tools_get_exact_rules(self):
        self.user_config({"notes": {"command": "/bin/notes"}, "cached": {"url": "https://x"}})
        cache = self.home / ".gemini" / "antigravity-cli" / "mcp" / "cached"
        cache.mkdir(parents=True)
        for name in ("lookup", "*", "a b)", "a?", ""):   # N3b: cache file names are checked too
            (cache / (name + ".json")).write_text("{}")
        listed = lambda argv, **k: subprocess.CompletedProcess(argv, 0, json.dumps({"jsonrpc": "2.0", "id": 2, "result": {
            "tools": [{"name": n} for n in ("save_note", "*", "", "a b)", "a?", "x/y")]}}) + "\n", "")
        self.assertEqual(AGY.user_server_tools(run=listed, probe=True), {"notes": ["save_note"], "cached": ["lookup"]})
        with patch.object(AGY, "_listed_tools", return_value={"save_note", "x/y"}):
            added = AGY.ensure_allow_rules(probe=True)
        self.assertEqual(sorted(rule for rule in added if not rule.startswith("mcp(sidedoor_")),
                         ["mcp(cached/lookup)", "mcp(notes/save_note)"])

    def test_n3a_wait_never_starts_a_server_that_has_rules(self):
        self.user_config({"notes": {"command": "/bin/notes"}})
        with patch.object(AGY, "_listed_tools", return_value={"save_note"}) as listed:
            AGY.ensure_allow_rules()           # no rule yet: asked once
            AGY.ensure_allow_rules()           # its rules exist: never started again before a run
            self.assertEqual(listed.call_count, 1)
            AGY.ensure_allow_rules(probe=True)   # Setup asks again
            self.assertEqual(listed.call_count, 2)

    def test_p1_a_server_that_lists_nothing_is_not_started_before_runs(self):
        self.user_config({"empty": {"command": "/bin/empty"}})
        (Path(os.environ["SIDEDOOR_HOME"]) / AGY.PROBED_FILE).unlink(missing_ok=True)
        with patch.object(AGY, "_listed_tools", return_value=set()) as listed:
            for _ in range(3):
                AGY.ensure_allow_rules()           # wait(): started once only
            self.assertEqual(listed.call_count, 1)
            AGY.ensure_allow_rules(probe=True)   # Setup's Check asks again
            self.assertEqual(listed.call_count, 2)
            self.user_config({"empty": {"command": "/bin/empty", "args": ["--new"]}})
            AGY.ensure_allow_rules()               # a changed spec is asked again
            self.assertEqual(listed.call_count, 3)

    def test_p2_the_probe_is_contained_right_after_it_starts(self):
        """Mocks only: the Windows Job Object path itself is tested in test_portable (not run on Windows here)."""
        import portable
        order = []
        worker = unittest.mock.MagicMock(pid=1)
        worker.communicate.return_value = ("", "")
        worker.poll.return_value = 0
        with patch("subprocess.Popen", side_effect=lambda *a, **k: order.append("popen") or worker), \
                patch.object(portable, "contain", side_effect=lambda w: order.append("contain")):
            AGY._listed_tools({"command": "/bin/x"})
        self.assertEqual(order, ["popen", "contain"])

    def test_n3d_one_bad_server_is_skipped(self):
        self.user_config({"bad": {"command": "/bin/bad"}, "good": {"command": "/bin/good"}})
        def listed(spec, run=None):
            if spec["command"] == "/bin/bad":
                raise ValueError("embedded null byte")
            return {"ok"}
        with patch.object(AGY, "_listed_tools", side_effect=listed):
            added = AGY.ensure_allow_rules(probe=True)
        self.assertIn("mcp(good/ok)", added)
        self.assertIn("mcp(sidedoor_browser/arm)", added)
        self.assertFalse(any("bad" in rule for rule in added))

    def test_listed_tools_times_out_without_hanging(self):
        AGY_SLOW = engines.AntigravityEngine()
        AGY_SLOW.USER_TOOLS_SECONDS = 1
        script = self.home / "slow.sh"
        script.write_text("#!/bin/sh\nsleep 30\n")
        script.chmod(0o755)
        import time
        started = time.monotonic()
        self.assertEqual(AGY_SLOW._listed_tools({"command": str(script)}), set())
        self.assertLess(time.monotonic() - started, 10)

    def test_unreadable_settings_are_never_rewritten(self):
        settings = self.home / ".gemini" / "antigravity-cli" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text("{not json")
        with self.assertRaises(ValueError):
            AGY.ensure_allow_rules()
        self.assertEqual(settings.read_text(), "{not json")


class LauncherTest(TempHome):
    def test_found_without_path_in_install_folders(self):
        folder = self.home / ".local" / "bin"
        folder.mkdir(parents=True)
        program = folder / "agy"
        program.write_text("#!/bin/sh\n")
        program.chmod(0o755)
        self.assertEqual(setup_steps.find_antigravity(search_path=""), str(program))   # a Dock start: no PATH
        candidates = setup_steps.antigravity_candidates(search_path="")
        self.assertIn("/opt/homebrew/bin/agy", candidates)
        self.assertIn("/usr/local/bin/agy", candidates)

    def test_environment_is_the_allowlist(self):
        with patch.dict(os.environ, {"HOME": "/h", "USER": "u", "LOGNAME": "u", "TMPDIR": "/t", "SECRET": "s",
                                     "GEMINI_API_KEY": "k"}, clear=True):
            self.assertEqual(AGY.environment(None), {"HOME": "/h", "USER": "u", "LOGNAME": "u", "TMPDIR": "/t"})


if __name__ == "__main__":
    unittest.main()
