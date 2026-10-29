"""The agent engine layer (U1 phase 2; owner pick, 2026-10-27: option B, one engine layer).

Every AI session Sidedoor starts runs on an engine: a coding-agent CLI. Each engine builds its own
command and environment, and turns its own step log into one neutral record that every reader uses:

- `TraceEvent` / `ToolCall`: one step of the log (a tool call with its id, server, tool, arguments,
  result and error state; an assistant message; a shell command or web search the engine ran itself).
- `RunRecord`: a whole run (tool calls by id, assistant text, usage, web searches, error kind, denials).

Phase 2 has one engine, Codex, with ZERO behaviour change: `CodexEngine` produces exactly the commands
and environments the call sites built before (tests/test_engines.py replays the pre-change builders) and
parses the Codex JSONL `--json` trace that existing saved artifacts hold, so trace.jsonl#<item id>
references stay valid. Claude Code and Antigravity are phase 3 and 4.

Launch rule for every engine (owner, the Kyle rule): find the CLI by absolute path across its install
locations, save and launch the found path, pass the child an allowlisted environment (`ENVIRONMENT`, a
per-engine property: Codex keeps its own list unchanged; Claude Code needs USER too), and a missing CLI
is a no-try wait (`wait`), never a launch of a missing program.

The research launcher is on it too: orchestrator.research_command / run_research_worker, research_recovery's
command rewrite, research_tools' MCP settings, and worker_never_reached_model's log reading.

Phase 3a (neutral readers, still Codex only, zero behaviour change): everything that reads an engine's
output asks the engine that WROTE it. A launch records its engine (`record_run`: engine.json in the run
folder, or <log>.engine.json beside a plain worker log); a reader asks `for_run(folder or file)`, where no
record means Codex (every run saved before 3a). A launcher asks `engine(config)`; code holding only a
command asks `for_command(command)`. Traces are read through a stateful `trace_reader()` (one line can
hold several steps under another engine, and a call's result can arrive on a later line); the pacing key
(`pacing_provider`), a stated limit reset (`reset_at`), a
trace's error events (`error_events`) and a plain log's shown lines (`log_pairs`) come from the engine.
Phase 3c: `ClaudeEngine`, chosen only by the dev-only config["engine"] == "claude" and wired into no worker
yet (3d-3f): find and sign-in probe, command, environment, the stream-json reader, limits, model_calls rows.
"""

import dataclasses
import hashlib
import json
import os
import re
from pathlib import Path


class TraceShapeError(ValueError):
    """A trace line that is JSON but not an event object."""


@dataclasses.dataclass(frozen=True)
class ToolCall:
    """One tool call. `id` is the engine's own call id (Codex: item.id): quotes and saved
    trace.jsonl#<id> references bind to it. `status` is the call's state as the engine logged it
    ("completed", "failed", "in_progress", ...); `error` its error value, if any."""
    id: object
    server: object
    tool: object
    args: dict
    result: object
    status: object
    error: object
    has_id: bool = True

    @property
    def is_error(self):
        """True: the call failed or its answer is an error. False: completed cleanly. None: neither
        (not finished, or an unknown state)."""
        if self.status == "failed" or (isinstance(self.result, dict) and self.result.get("isError")):
            return True
        if self.status == "completed" and not self.error:
            return False
        return None


@dataclasses.dataclass(frozen=True)
class TraceEvent:
    """One line of an engine's step log.

    phase: "started" or "completed" for a step, None for anything else (session, turn, error lines).
    kind: "tool" (an MCP tool call, in `call`), "shell" (a command the engine ran itself), "web_search",
    "message" (assistant text, in `text`), "malformed" (a step whose item is not an object), "other"
    (any other item; the engine's own item type is in `raw_kind`), or None (a step with no item)."""
    phase: object
    kind: object
    call: object = None
    text: object = None
    status: object = None
    raw_kind: object = None


@dataclasses.dataclass
class RunRecord:
    tool_calls: list            # ToolCall, last state per id, in first-seen order
    text: list                  # assistant messages
    usage: dict                 # {"total_tokens": n} when the engine reported it
    web_searches: list          # 1-based trace line numbers of completed web searches
    answer: object = None       # the final structured answer (parsed), when read
    error_kind: object = None   # provider_limits kind, when classified
    denials: list = dataclasses.field(default_factory=list)
    # Claude (U1 3c): every failure code the reader found, WebFetch (url, HTTP code), cost in USD, model,
    # seconds, whether the result event arrived, the last rejected rate_limit_info.
    errors: list = dataclasses.field(default_factory=list)
    web_fetches: list = dataclasses.field(default_factory=list)
    cost: object = None
    model: object = None
    seconds: object = None
    finished: bool = False
    rate_limit: object = None


class EngineRecordError(ValueError):
    """A run's engine record (engine.json) that cannot be read or names no known engine."""


class Engine:
    """The interface every engine implements (Codex now; Claude Code and Antigravity later)."""
    name = None
    ENVIRONMENT = frozenset()
    # The provider_pacing key of this engine's own sessions (Codex: "openai"); a command pointed elsewhere
    # (Codex at OpenRouter) gets its key from `provider_key`.
    pacing_provider = None
    # True when a run is judged from its stream by `finish_run` whatever its exit code (Claude, Antigravity).
    judged_by_stream = False

    def prompt_input(self, prompt):
        """What a launcher writes to the child's stdin for `prompt` (the prompt itself; agy wraps it)."""
        return prompt

    def run_ended(self, command):
        """Called by every launcher once the child is gone, however the run ended (Antigravity removes its
        per-run MCP config). Nothing for Codex and Claude."""

    def built(self, command):
        """True when this engine built `command` (for_command). Codex is the default and never claims."""
        return False

    def record_timed_out(self, trace, command=None, worker=None, home=None, seconds=None):
        """A5: a run stopped at its time limit writes its model_calls row as a finished run does
        (`finish_run`), with the usage its stream showed so far (the reader's, as finish_run records it),
        the outcome "timeout" and, when the stream stated no duration, `seconds` (how long it ran). Only
        the engines judged by their stream write rows for their runs (Claude, Antigravity); Codex writes
        none. `trace`: the run's stream file, read here. Best effort: never raises (a read that fails too)."""
        if not self.judged_by_stream:
            return
        try:
            text = Path(trace).read_text(encoding="utf-8", errors="replace")
            record = self.read_trace(text, allowed=self._allowed(None, command), strict=False)
            if getattr(record, "seconds", None) is None and seconds is not None:
                record.seconds = seconds
            self.record_model_call(record, worker=worker, home=home, outcome="timeout")
        except Exception:
            pass

    def trace_reader(self):
        """A fresh stateful reader of one run's step log: `feed(line)` returns the line's TraceEvents."""
        raise NotImplementedError

    def reset_at(self, message):
        """The limit reset the engine stated (an aware UTC datetime), else None."""
        raise NotImplementedError

    def wait(self, config):
        """A recorded no-try wait when the engine's CLI was not found, else None."""
        raise NotImplementedError

    def base_environment(self):
        return {key: value for key, value in os.environ.items() if key in self.ENVIRONMENT}

    def parse_line(self, line):
        raise NotImplementedError

    def provider_key(self, command):
        raise NotImplementedError


class CodexTraceReader:
    """Codex's --json trace has one step per line: each line is read on its own (`CodexEngine.parse_line`)."""

    def __init__(self, engine):
        self.engine = engine

    def feed(self, line):
        """The line's steps ([one TraceEvent]). Raises as `CodexEngine.parse_line` does."""
        return [self.engine.parse_line(line)]


class CodexEngine(Engine):
    name = "codex"
    pacing_provider = "openai"   # every Codex command sets model_provider="openai" unless it is pointed at OpenRouter
    # The child environment allowlist, unchanged from agent_worker/reply_worker (phase 2: zero change).
    ENVIRONMENT = frozenset({"HOME", "PATH", "CODEX_HOME", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT"})
    TOOL = "mcp_tool_call"
    SHELL = frozenset({"command_execution"})
    WEB_SEARCH = frozenset({"web_search", "web_search_call"})
    MESSAGE = "agent_message"
    _PHASES = {"item.started": "started", "item.completed": "completed"}
    _SETTINGS = ("features.shell_tool=false", "features.apps=false", "features.skills=false",
                 "features.apply_patch_freeform=false", "features.unified_exec=false", "features.multi_agent=false")

    # --- finding and launching ---

    def wait(self, config):
        import setup_steps
        return setup_steps.codex_wait(config)

    def _exec(self, config, model, json_trace, ignore_rules=True):
        command = [config["codex"], "exec", "--ignore-user-config"]
        if ignore_rules:
            command.append("--ignore-rules")
        command += ["--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only", "--model", model]
        return command + (["--json"] if json_trace else [])

    def investigation_command(self, config, directory):
        """agent_worker: a public-web investigation (live web search, no tools, a JSON trace)."""
        import openai_auth
        directory = Path(directory)
        command = self._exec(config, config["model"], True)
        for setting in ('approval_policy="never"', 'forced_login_method="chatgpt"', 'model_provider="openai"',
                        'web_search="live"') + self._SETTINGS + ("mcp_servers={}",):
            command.extend(["-c", setting])
        # E0.T8: ChatGPT sign-in returns this command untouched; an API key drops the forced login.
        return openai_auth.command(command + ["--output-schema", str(directory / "schema.json"),
                                              "--output-last-message", str(directory / "output.json"),
                                              "--color", "never", "-"])

    def browser_command(self, config, directory, application=False, main_model=False):
        """outreach_history_browser (application=False) and application_browser (application=True):
        a browser session whose one MCP server is the guarded @playwright/mcp (`sidedoor browser-guard`,
        browser_runtime) when `config["_browser_operation"]` names its operation, else none. The user's
        Codex config is ignored, so no server the user set up loads. Reads run on browser_read_model
        when one is configured; `main_model` switches an application session back to the main model."""
        import openai_auth
        directory = Path(directory)
        result = self._exec(config, config.get("browser_read_model") or config["model"], True, ignore_rules=False)
        for setting in ('approval_policy="never"', 'forced_login_method="chatgpt"',
                        'web_search="disabled"', "features.apps=false",
                        "features.shell_tool=false", "features.unified_exec=false",
                        "features.apply_patch_freeform=false", "features.multi_agent=false"):
            result.extend(["-c", setting])
        operation = config.get("_browser_operation")
        if operation:
            import browser_runtime
            for setting in browser_runtime.codex_options(config, directory, operation,
                                                         claim=config.get("_application_guard_claim"),
                                                         human_check=config.get("_application_human_check")):
                result.extend(["-c", setting])
        else:
            result.extend(["-c", "mcp_servers={}"])
        # E0.T8: ChatGPT sign-in returns this command untouched; an API key drops the forced login.
        result = openai_auth.command(result + ["--output-schema", str(directory / "schema.json"),
                                               "--output-last-message", str(directory / "output.json"), "-"])
        if not application:
            return result
        if main_model:
            result[result.index("--model") + 1] = config["model"]
        for option in ('model_provider="openai"', "features.skills=false"):
            result[-1:-1] = ["-c", option]
        return result

    # --- research (orchestrator.research_command / run_research_worker, research_recovery, research_tools) ---

    # The research child gets this process's environment minus the shell's OpenAI variables (a denylist,
    # unchanged from run_research_worker; phase 2: zero change).
    RESEARCH_STRIPPED = frozenset({
        "OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID",
        "OPENAI_ORGANIZATION", "OPENAI_PROJECT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT"})
    # Lines codex writes into a worker's plain log only once the model is actually running a turn.
    MODEL_ACTIVITY_LINES = ("codex", "thinking", "exec")
    MODEL_ACTIVITY_PREFIXES = ("web search:", "tokens used")

    def research_command(self, root, config, output_path, schema):
        """A research session (discovery, qualification): live web search, a plain log, no --json."""
        import resources
        return [config["codex"], "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
                "--skip-git-repo-check", "--sandbox", "read-only", "--model", config["model"],
                "-c", 'approval_policy="never"', "-c", 'web_search="live"',
                "-c", "features.shell_tool=false", "-c", "features.apps=false",
                "-c", "features.skills=false", "-c", "features.apply_patch_freeform=false",
                "-c", 'model_reasoning_effort="medium"',
                "--output-schema", str(resources.path(schema) if resources.declared(str(schema)) else root / schema),
                "--output-last-message", str(output_path), "--color", "never", "-"]

    def research_recovery_command(self, config, command):
        """research_recovery: the research command with web search and every tool off, pointed at the
        text model when one is configured."""
        import openai_auth
        import text_model
        command = list(command)
        for index, setting in enumerate(command):
            if setting == 'web_search="live"':
                command[index] = 'web_search="disabled"'
        settings = ('forced_login_method="chatgpt"', 'model_provider="openai"', 'web_search="disabled"',
                    "features.shell_tool=false", "features.apps=false", "features.skills=false",
                    "features.apply_patch_freeform=false", "features.unified_exec=false",
                    "features.multi_agent=false", "mcp_servers={}")
        command[-1:-1] = [argument for setting in settings for argument in ("-c", setting)]
        return openai_auth.command(text_model.command(config, command))  # E0.T8

    def mcp_server_options(self, server, argv, startup_timeout, tool_timeout, tools):
        """The settings that attach one stdio MCP server, its tools pre-approved (research_tools)."""
        result = ["mcp_servers." + server + ".command=" + json.dumps(argv[0]),
                  "mcp_servers." + server + ".args=" + json.dumps(argv[1:]),
                  "mcp_servers." + server + ".enabled=true",
                  "mcp_servers." + server + ".startup_timeout_sec=" + str(startup_timeout),
                  "mcp_servers." + server + ".tool_timeout_sec=" + str(tool_timeout)]
        result.extend("mcp_servers." + server + ".tools." + tool + '.approval_mode="approve"' for tool in tools)
        return result

    def attach(self, command, options):
        """`command` with `options` (mcp_server_options) placed before its trailing stdin marker
        (research_command's shape). Any other command shape is returned as it is."""
        if not isinstance(command, list) or not command or command[-1] != "-":
            return command
        settings = [part for option in options for part in ("-c", option)]
        return command[:-1] + settings + ["-"]

    def research_environment(self, command):
        environment = {key: value for key, value in os.environ.items() if key not in self.RESEARCH_STRIPPED}
        # E0.T8: the shell's OpenAI variables are still stripped; with an API key chosen, the saved key
        # goes in as CODEX_API_KEY (ChatGPT sign-in: this environment, untouched).
        import openai_auth
        return openai_auth.environment(command, environment)

    def pre_model_rejection(self, text, rejections):
        """The provider's pre-model request rejection in a plain worker log, when the log shows no model
        turn: (error object, its event) for the first `ERROR: {...}` event whose (type, code, status) is
        in `rejections`, else None."""
        for line in text.splitlines():
            if line.strip() in self.MODEL_ACTIVITY_LINES or line.startswith(self.MODEL_ACTIVITY_PREFIXES):
                return None
        decoder = json.JSONDecoder()
        for match in re.finditer(r"(?m)^ERROR: (?=\{)", text):
            try:
                value, _ = decoder.raw_decode(text, match.end())
            except ValueError:
                continue
            error = value.get("error") if isinstance(value, dict) else None
            if (isinstance(error, dict) and value.get("type") == "error"
                    and (error.get("type"), error.get("code"), value.get("status")) in rejections):
                return error, value
        return None

    def environment(self, arguments, variables, config=None, text=False):
        """The child's environment: `variables` (the allowlisted base), plus the OpenRouter key for a
        text worker with a text model, plus the OpenAI API key in API-key mode (openai_auth)."""
        import openai_auth
        if text:
            import text_model
            variables = text_model.environment(config, variables)
        return openai_auth.environment(arguments, variables)

    def succeeded(self, arguments):
        import openai_auth
        openai_auth.succeeded(arguments)   # E0.T8 AK5: a call that worked ends a billing-wait streak

    # --- errors, provider and usage ---

    def limit(self, returncode, *paths, command=None):
        """The provider wait behind a failed run (provider_limits classifies Codex's output files)."""
        import provider_limits
        return provider_limits.check(returncode, *paths, command=command)

    def limit_output(self, returncode, *texts, command=None):
        import provider_limits
        return provider_limits.check_output(returncode, *texts, command=command)

    def provider_key(self, command):
        """The pacing key for a Codex command: "openrouter" when it is pointed at OpenRouter, else "openai"."""
        import openai_auth
        return "openrouter" if command and openai_auth.OPENROUTER_PROVIDER in command else self.pacing_provider

    def reset_at(self, message):
        """The reset Codex's usage-limit words state ("try again at ..."), else None (provider_limits.reset_time)."""
        import provider_limits
        return provider_limits.reset_time(message)

    def usage(self, stderr):
        """Codex's own total ("tokens used" then the number) from its stderr: {"total_tokens": n} or {}."""
        found = re.findall(r"tokens used\s*:?\s*\n?\s*([\d,]+)", stderr or "")
        return {"total_tokens": int(found[-1].replace(",", ""))} if found else {}

    # --- the step log ---

    def parse_line(self, line):
        """One --json trace line as a TraceEvent. Raises ValueError for a line that is not JSON
        (json.JSONDecodeError) or not an object (TraceShapeError)."""
        event = json.loads(line)
        if not isinstance(event, dict):
            raise TraceShapeError("INVALID_TRACE_EVENT")
        phase = self._PHASES.get(event.get("type"))
        if phase is None or "item" not in event:
            return TraceEvent(phase, None)
        return self.item_event(event["item"], phase)

    def item_event(self, item, phase=None):
        """One Codex trace item (the `item` of an item.started/item.completed line) as a TraceEvent."""
        if not isinstance(item, dict):
            return TraceEvent(phase, "malformed")
        kind = item.get("type")
        status = item.get("status")
        if kind == self.TOOL:
            arguments = item.get("arguments")
            return TraceEvent(phase, "tool", ToolCall(
                id=item.get("id"), server=item.get("server"), tool=item.get("tool"),
                args=arguments if isinstance(arguments, dict) else {}, result=item.get("result", {}),
                status=status, error=item.get("error"), has_id="id" in item), status=status)
        if kind in self.SHELL:
            return TraceEvent(phase, "shell", status=status)
        if kind in self.WEB_SEARCH:
            return TraceEvent(phase, "web_search", status=status)
        if kind == self.MESSAGE:
            return TraceEvent(phase, "message", text=item.get("text"), status=status)
        return TraceEvent(phase, "other", status=status, raw_kind=kind)

    def trace_reader(self):
        return CodexTraceReader(self)

    def read_trace(self, text):
        """A whole trace as a RunRecord. Raises as `parse_line` does."""
        calls, messages, searches, usage = {}, [], [], {}
        reader = self.trace_reader()
        for number, line in enumerate(text.splitlines(), 1):
            for event in reader.feed(line):
                if event.kind == "tool" and event.call.has_id and isinstance(event.call.id, (str, int)):
                    calls[event.call.id] = event.call
                elif event.phase == "completed" and event.kind == "message" and isinstance(event.text, str):
                    messages.append(event.text)
                elif event.phase == "completed" and event.kind == "web_search" and event.status in {None, "completed"}:
                    searches.append(number)
        return RunRecord(list(calls.values()), messages, usage, searches)

    # --- what the Failures page shows of a run ---

    def error_events(self, pairs):
        """A trace's error events over [(byte offset, line)]: (offset, kind, message) for each error line
        (type error), failed turn (turn.failed) or failed item; every other line (reasoning, messages,
        tool arguments and results) is dropped, as is any line that is not a JSON object."""
        for offset, line in pairs:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            kind = str(event.get("type") or "")
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            message = None
            if kind == "error":
                message = event.get("message")
            elif kind == "turn.failed":
                message = (event.get("error") or {}).get("message") if isinstance(event.get("error"), dict) else event.get("error")
            elif item and (str(item.get("status") or "").lower() in ("failed", "error") or item.get("error")):
                error = item.get("error")
                message = (error.get("message") if isinstance(error, dict) else error) or "failed (%s)" % item.get("type")
                kind = "%s %s" % (kind, item.get("type") or "item")
            if message is not None:
                yield offset, kind, message

    def log_pairs(self, pairs, truncated):
        """A plain Codex log's lines that may be shown (its prompt echo and transcript withheld):
        failures_page.codex_pairs."""
        import failures_page
        return failures_page.codex_pairs(pairs, truncated)


CODEX = CodexEngine()

# --- U1 phase 3c: Claude --------------------------------------------------------------------------------
#
# Chosen only by the dev-only config["engine"] == "claude" (no Setup step or Settings choice before 3g) and
# wired into no worker before 3d-3f: this is the engine core (find and sign-in probe, command, environment,
# stream-json reader, limits, model_calls rows). Flags and failure shapes are the ones spike 2 proved on
# Claude 2.1.301 (docs/product/design/U1-phase3-claude-plan.md, "Spike 2 findings"). The UI word is
# "Claude" (Anthropic's branding rule).

class ClaudeRunFailure(RuntimeError):
    """A Claude run that failed although it may have exited 0 (`ClaudeEngine.check_run`): `code` is one of
    the ClaudeEngine failure codes, `message` the engine's own words (bounded). Not a wait: it spends a try."""

    def __init__(self, code, message=""):
        self.code, self.message = code, str(message or "")[:500]
        super().__init__(code)


class ClaudeCommand(list):
    """A `claude -p` argv (ClaudeEngine.command) for a worker whose answer is a file: research, research
    recovery and the agent worker (U1 3d). Besides the argv it keeps `output` (the file write_answer fills
    from result.structured_output, where Codex's --output-last-message writes), `build` (the
    ClaudeEngine.command arguments, so `attach` rebuilds it with an MCP server) and the per-job MCP limits
    in seconds (`tool_timeout`: MCP_TOOL_TIMEOUT, `startup_timeout`: MCP_TIMEOUT; None: the default)."""

    def __init__(self, argv, output=None, build=None, tool_timeout=None, startup_timeout=None, output_tokens=None):
        super().__init__(argv)
        self.output, self.build = output, dict(build or {})
        self.tool_timeout, self.startup_timeout = tool_timeout, startup_timeout
        self.output_tokens = output_tokens   # MAX_MCP_OUTPUT_TOKENS (browser jobs, U1 3e E4); None: Claude's default


class ClaudeTraceReader:
    """Claude's `--output-format stream-json --verbose` log: one JSON event per line, and one assistant line
    can hold several tool_use blocks whose tool_result blocks arrive on later user lines (paired by id), so
    the reader keeps state across lines.

    `feed(line)` returns the line's TraceEvents: a tool_use is a "started" event and its tool_result the
    "completed" one (kind "tool" for an MCP tool mcp__<server>__<tool>, "web_search" / "web_fetch" for the
    built-in WebSearch / WebFetch, "shell" for Bash, "other" for any other built-in), assistant text is a
    "message", and every failure the run reports while still exiting 0 is an event of kind "error" whose
    `raw_kind` is the failure code and `text` the engine's words:

    - MCP_SERVER_FAILED: the init event lists an MCP server whose status is not "connected" (MCP_TIMEOUT).
    - PERMISSION_DENIED: a permission_denied event or result.permission_denials.
    - TOOL_TIMEOUT: an is_error tool_result that says it timed out (MCP_TOOL_TIMEOUT).
    - TOOL_OUTPUT_TOO_LARGE: a tool_result over MAX_MCP_OUTPUT_TOKENS ("exceeds maximum allowed tokens";
      not is_error, so the call is marked failed here).
    - FORBIDDEN_TOOL: a tool_use of any tool outside `allowed` (StructuredOutput, the schema answer, is
      always allowed); with `allowed` None, any built-in tool. The kill switch.
    - RATE_LIMITED: a rate_limit_event whose status is "rejected", or a result whose api_error_status is
      429 or 529. SIGNED_OUT: api_error_status 401. API_ERROR: any other is_error result.

    The structured answer is result.structured_output (`answer`), the run's cost result.total_cost_usd."""

    def __init__(self, engine, allowed=None):
        self.engine = engine
        self.allowed = None if allowed is None else frozenset(allowed) | {engine.ANSWER_TOOL}
        self.calls = {}           # tool_use id -> ToolCall, first-seen order
        self.answer_ids = set()   # StructuredOutput tool_use ids (kept out of the tool calls)
        self.errors = []          # (code, message)
        self.denied = set()       # tool_use ids already reported denied
        self.init = None
        self.result = None
        self.rate_limit = None    # the last rejected rate_limit_info

    def _error(self, code, message, call=None):
        self.errors.append((code, str(message)[:500]))
        return TraceEvent(None, "error", call=call, text=str(message)[:500], raw_kind=code)

    def _allowed(self, name):
        if self.allowed is not None:
            return name in self.allowed
        return name == self.engine.ANSWER_TOOL or (isinstance(name, str) and name.startswith("mcp__"))

    def _deny(self, tool_use_id, name, message):
        if tool_use_id in self.denied:
            return []
        self.denied.add(tool_use_id)
        return [self._error(ClaudeEngine.PERMISSION_DENIED, message or "permission denied: %s" % name)]

    def feed(self, line):
        """The line's TraceEvents. Raises ValueError for a line that is not JSON (json.JSONDecodeError) or
        not an object (TraceShapeError)."""
        event = json.loads(line)
        if not isinstance(event, dict):
            raise TraceShapeError("INVALID_TRACE_EVENT")
        kind, subtype = event.get("type"), event.get("subtype")
        if kind == "system" and subtype == "init":
            self.init = event
            found = []
            for server in event.get("mcp_servers") or []:
                if isinstance(server, dict) and server.get("status") != "connected":
                    found.append(self._error(ClaudeEngine.MCP_SERVER_FAILED, "MCP server %s: %s" % (
                        server.get("name"), server.get("status"))))
            return found or [TraceEvent(None, None, raw_kind="init")]
        if kind == "system" and subtype == "permission_denied":
            return self._deny(event.get("tool_use_id"), event.get("tool_name"), event.get("message"))
        if kind == "assistant":
            return self._assistant(event)
        if kind == "user":
            return self._user(event)
        if kind == "rate_limit_event":
            info = event.get("rate_limit_info") if isinstance(event.get("rate_limit_info"), dict) else {}
            if info.get("status") == "rejected":
                self.rate_limit = info
                return [self._error(ClaudeEngine.RATE_LIMITED, ClaudeEngine.REJECTED + "%s" % info.get("rateLimitType"))]
            return [TraceEvent(None, None, raw_kind="rate_limit_event")]
        if kind == "result":
            return self._result(event)
        return [TraceEvent(None, None, raw_kind=kind)]

    def _assistant(self, event):
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        found = []
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                found.append(TraceEvent("completed", "malformed"))
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                found.append(TraceEvent("completed", "message", text=block["text"], status="completed"))
            elif block.get("type") == "tool_use":
                name, tool_use_id = block.get("name"), block.get("id")
                arguments = block.get("input") if isinstance(block.get("input"), dict) else {}
                if not self._allowed(name):
                    found.append(self._error(ClaudeEngine.FORBIDDEN_TOOL, "tool not allowed: %s" % name))
                if name == self.engine.ANSWER_TOOL:
                    self.answer_ids.add(tool_use_id)
                    continue
                server, tool = self.engine.split_name(name)
                call = ToolCall(id=tool_use_id, server=server, tool=tool, args=arguments, result=None,
                                status="in_progress", error=None, has_id="id" in block)
                if isinstance(tool_use_id, str):
                    self.calls[tool_use_id] = call
                found.append(TraceEvent("started", self.engine.call_kind(name), call, status="in_progress"))
        return found

    def _user(self, event):
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        blocks = [block for block in (message.get("content") or [])
                  if isinstance(block, dict) and block.get("type") == "tool_result"]
        # Each tool_result's own tool_use_result (URL, HTTP code): Claude 2.1.301 writes one result per user
        # line, parallel calls included (3d probe, r3d_parallel_fetch). A line with several results pairs
        # them by position only when tool_use_result is a list of the same length (R3).
        details = event.get("tool_use_result")
        details = ([details] if len(blocks) == 1 else
                   details if isinstance(details, list) and len(details) == len(blocks) else [None] * len(blocks))
        found = []
        for block, detail in zip(blocks, details):
            tool_use_id = block.get("tool_use_id")
            if tool_use_id in self.answer_ids:
                continue
            started = self.calls.get(tool_use_id) or ToolCall(
                id=tool_use_id, server=None, tool=None, args={}, result=None, status=None, error=None,
                has_id=tool_use_id is not None)
            content = block.get("content")
            content = [{"type": "text", "text": content}] if isinstance(content, str) else (
                content if isinstance(content, list) else [])
            text = "\n".join(part.get("text") for part in content
                             if isinstance(part, dict) and isinstance(part.get("text"), str))
            failed = block.get("is_error") is True
            result = {"content": content, "isError": failed}
            name = "mcp__%s__%s" % (started.server, started.tool) if started.server else started.tool
            if isinstance(detail, dict) and name == "WebFetch":
                result.update({key: detail.get(key) for key in ("url", "code", "codeText", "bytes")})
            elif isinstance(detail, dict) and name == "WebSearch":
                result["query"] = detail.get("query")
                result["urls"] = [link.get("url") for entry in detail.get("results") or [] if isinstance(entry, dict)
                                  for link in entry.get("content") or [] if isinstance(link, dict) and link.get("url")]
            error = (text or "failed") if failed else None
            if started.server and self.engine.TOO_LARGE in text:   # MCP results only (MAX_MCP_OUTPUT_TOKENS)
                failed, error = True, text
                found.append(self._error(ClaudeEngine.TOOL_OUTPUT_TOO_LARGE, text))
            elif started.server and failed and self.engine.TIMED_OUT in text:   # MCP_TOOL_TIMEOUT (MCP calls only)
                found.append(self._error(ClaudeEngine.TOOL_TIMEOUT, text))
            call = dataclasses.replace(started, result=result, status="failed" if failed else "completed", error=error)
            if isinstance(tool_use_id, str):
                self.calls[tool_use_id] = call
            found.append(TraceEvent("completed", self.engine.call_kind(name), call, status=call.status))
        return found or [TraceEvent(None, None, raw_kind="user")]

    def _result(self, event):
        self.result = event
        found = []
        for denial in event.get("permission_denials") or []:
            if isinstance(denial, dict):
                found += self._deny(denial.get("tool_use_id"), denial.get("tool_name"),
                                    "permission denied: %s" % denial.get("tool_name"))
        if event.get("is_error") is True:
            status = event.get("api_error_status")
            words = event.get("result") if isinstance(event.get("result"), str) else ""
            if status in self.engine.RATE_LIMIT_STATUSES:
                found.append(self._error(ClaudeEngine.RATE_LIMITED, "HTTP %s: %s" % (status, words)))
            elif status == 401 or words.startswith(self.engine.NOT_LOGGED_IN):
                found.append(self._error(ClaudeEngine.SIGNED_OUT, "HTTP %s: %s" % (status, words)))
            else:
                found.append(self._error(ClaudeEngine.API_ERROR, "HTTP %s: %s" % (status, words)))
        return found + [TraceEvent("completed", "other", text=event.get("result"), raw_kind="result")]

    @property
    def answer(self):
        return self.result.get("structured_output") if isinstance(self.result, dict) else None


class ClaudeEngine(Engine):
    name = "claude"
    pacing_provider = "anthropic"
    # The child environment allowlist: Claude needs USER besides HOME to find its keychain sign-in on macOS
    # (phase 1: without USER, "Not logged in"); the Windows profile variables are kept for 3g. ANTHROPIC_*
    # are never passed through: subscription mode uses the user's own sign-in, API-key mode adds the key
    # saved in Sidedoor's credential store ("anthropic").
    ENVIRONMENT = frozenset({"HOME", "USER", "LOGNAME", "PATH", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT",
                             "USERPROFILE", "APPDATA", "LOCALAPPDATA"})
    MAIN_MODEL, READ_MODEL = "opus", "sonnet"   # the plan's picks; config claude_model / claude_read_model override
    TOOL_TIMEOUT_SECONDS = 110                   # MCP_TOOL_TIMEOUT default (the browser guard's CALL_TIMEOUT)
    ANSWER_TOOL = "StructuredOutput"             # the --json-schema answer, a pseudo-tool
    EXECUTABLES = frozenset({"claude", "claude.exe", "claude.cmd"})
    RATE_LIMIT_STATUSES = frozenset({429, 529})
    TOO_LARGE = "exceeds maximum allowed tokens"
    REJECTED = "rate limit rejected: "
    TIMED_OUT = "timed out"
    NOT_LOGGED_IN = "Not logged in"
    KEY_REFUSED_WORDS = "Your Anthropic key was refused."
    ROUTE = "claude"                             # model_calls route
    judged_by_stream = True

    MCP_SERVER_FAILED = "MCP_SERVER_FAILED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    TOOL_OUTPUT_TOO_LARGE = "TOOL_OUTPUT_TOO_LARGE"
    FORBIDDEN_TOOL = "FORBIDDEN_TOOL"
    RATE_LIMITED = "RATE_LIMITED"
    SIGNED_OUT = "SIGNED_OUT"
    API_ERROR = "API_ERROR"
    NO_ANSWER = "NO_ANSWER"
    EXITED = "CLAUDE_EXITED"
    WAITS = frozenset({RATE_LIMITED, SIGNED_OUT})
    ANSWER_KEPT = frozenset({PERMISSION_DENIED, TOOL_TIMEOUT, TOOL_OUTPUT_TOO_LARGE})

    def built(self, command):
        return (isinstance(command, list) and bool(command) and isinstance(command[0], str)
                and Path(command[0]).name.lower() in self.EXECUTABLES and "-p" in command and "stream-json" in command)

    def provider_key(self, command):
        return self.pacing_provider

    # --- finding, signing in, launching ---

    def prepare(self, config, home=None):
        """`config` with the Claude found (setup_steps.with_claude), or marked missing."""
        import setup_steps
        return setup_steps.with_claude(config, home)

    def api_key_mode(self, home=None):
        """Setup-state `claude_auth` == "api_key" (setup_steps.claude_api_key_mode; 3g writes it), else the
        user's subscription. credentials._wanted reads the same."""
        import setup_steps
        return setup_steps.claude_api_key_mode(home)

    def wait(self, config, home=None, run=None):
        """A no-try wait before a launch, else None: CLAUDE_NOT_FOUND when no usable Claude is found, and
        in subscription mode CLAUDE_SIGNED_OUT when `claude auth status` says loggedIn false (a probe that
        cannot run decides nothing: the run itself then reports)."""
        import provider_limits
        import setup_steps
        if not isinstance(config, dict):
            return None
        if not config.get(setup_steps.CLAUDE_MISSING) and not setup_steps._usable(config.get("claude")):
            config = setup_steps.with_claude(config, home)
        missing = setup_steps.claude_wait(config, home)
        if missing or self.api_key_mode(home):
            return missing
        status = setup_steps.claude_auth_status(config["claude"], self.environment(None, config=config, home=home),
                                                **({"run": run} if run else {}))
        if status is not None and not status["loggedIn"]:
            return provider_limits.noted(provider_limits.ProviderUsageLimit(
                "CLAUDE_SIGNED_OUT: Claude is not signed in", kind=provider_limits.CLAUDE_SIGNED_OUT), home)
        # R5: a probe that says signed in clears nothing (it reads local credentials; a revoked sign-in
        # would flap): only a run that worked ends the wait (finish_run).
        return None

    def model(self, config, read=False):
        key = "claude_read_model" if read else "claude_model"
        return (config.get(key) if isinstance(config, dict) else None) or (self.READ_MODEL if read else self.MAIN_MODEL)

    def command(self, config, directory=None, tools=(), allowed=(), mcp_config=None, schema=None, read=False):
        """A `claude -p` run whose prompt is written to its stdin, in its own cwd (the caller's run folder).
        `tools`: the built-in tools it may see (--tools; "" for none: only StructuredOutput and MCP tools);
        `allowed`: the MCP tool names (mcp__<server>__<tool>) it may call, on top of `tools` (--allowedTools;
        dontAsk refuses everything else); `mcp_config`: an --mcp-config file; `schema`: the JSON schema (a
        path, or a dict) for --json-schema. Never --bare (drops the sign-in), --safe-mode (drops our MCP
        servers) or --dangerously-skip-permissions."""
        result = [config["claude"], "-p", "--output-format", "stream-json", "--verbose", "--no-session-persistence",
                  "--permission-mode", "dontAsk", "--permission-prompts", "none", "--setting-sources", "project",
                  "--strict-mcp-config", "--restricted", "--disable-slash-commands",
                  "--model", self.model(config, read), "--tools", ",".join(tools)]
        permitted = list(dict.fromkeys(list(tools) + list(allowed)))
        if permitted:
            result += ["--allowedTools", ",".join(permitted)]
        if mcp_config is not None:
            result += ["--mcp-config", str(mcp_config)]
        if schema is not None:
            if not isinstance(schema, dict):
                schema = json.loads(Path(schema).read_text(encoding="utf-8"))
            result += ["--json-schema", json.dumps(schema, separators=(",", ":"))]
        return result

    def mcp_config(self, directory, servers, name="mcp.json", environment=None):
        """Write {name: argv} as the run's --mcp-config file (stdio servers), each with its own
        `environment` ({name: {variable: value}}) when given; returns its path."""
        path = Path(directory) / name
        value = {"mcpServers": {server: dict({"type": "stdio", "command": argv[0], "args": list(argv[1:])},
                                             **({"env": dict(environment[server])}
                                                if environment and server in environment else {}))
                                for server, argv in servers.items()}}
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        return path

    def environment(self, arguments, variables=None, config=None, text=False, tool_timeout=None, home=None):
        """The child's environment: `variables` (default: the allowlisted base) without any ANTHROPIC_*,
        plus CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 and MCP_TOOL_TIMEOUT (ms; `tool_timeout` seconds, per job),
        plus ANTHROPIC_API_KEY from the credential store in API-key mode (OSError when none is saved)."""
        if text:
            raise NotImplementedError("CLAUDE_TEXT_WORKER: text workers call the text model directly")
        variables = self.base_environment() if variables is None else variables
        result = {key: value for key, value in variables.items() if not key.startswith("ANTHROPIC_")}
        result["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
        # A browser command carries its own limits (browser_command, U1 3e): the per-job MCP_TOOL_TIMEOUT
        # and the guard's MCP_TIMEOUT.
        tool_timeout = tool_timeout or getattr(arguments, "tool_timeout", None)
        result["MCP_TOOL_TIMEOUT"] = str(int((tool_timeout or self.TOOL_TIMEOUT_SECONDS) * 1000))
        if getattr(arguments, "startup_timeout", None) and getattr(arguments, "build", {}).get("mcp_config"):
            result["MCP_TIMEOUT"] = str(int(arguments.startup_timeout * 1000))
        if getattr(arguments, "output_tokens", None):
            result["MAX_MCP_OUTPUT_TOKENS"] = str(int(arguments.output_tokens))
        if self.api_key_mode(home):
            import credentials
            key = credentials.get("anthropic", home)
            if not key:
                raise OSError("ANTHROPIC_API_KEY_MISSING: no Anthropic API key is saved")
            result["ANTHROPIC_API_KEY"] = key
        return result

    def succeeded(self, arguments):
        """Nothing here: a Claude run is judged by `finish_run` (it can fail with exit 0)."""

    # --- browser reads (U1 3e): the guarded browser (`sidedoor browser-guard`) as the one MCP server ---

    # The guarded browser reads Claude runs (U1 3e), and submit (U1 3f): a submit session holds the private
    # arm claim and a session with the claim is a submit, exactly as application_browser.consume hands it
    # out; any other pairing, or a session that names no operation, has no browser and refuses.
    BROWSER_READS = frozenset({"linkedin", "webmail", "inspect", "investigate", "verify", "reconcile",
                               "support_email"})
    SUBMIT = "submit"

    def browser_tools(self, operation, human_check=None, claim=None):
        """The guard tools a browser session of `operation` may call, as Codex approves them
        (browser_runtime.codex_options): every browser tool, less the code tools for the code-free
        operations, plus `arm` when the session holds the private claim (submit only, U1 3f), plus the
        CAPTCHA / sign-in wait when the session has its context."""
        import browser_guard
        import browser_runtime
        tools = [tool for tool in browser_guard.TOOLS if operation not in browser_runtime.CODE_FREE_OPERATIONS
                 or tool not in browser_runtime.CODE_TOOLS]
        if claim:
            tools.append(browser_guard.ARM_TOOL)
        if human_check:
            tools.append(browser_guard.HUMAN_CHECK_TOOL)
        return tools + [browser_guard.CONTINUE_TOOL]   # Claude only: the pieces of a long answer (E4 b)

    def browser_command(self, config, directory, application=False, main_model=False):
        """outreach_history_browser (application=False) and application_browser (application=True) on
        Claude: the guarded @playwright/mcp (browser_runtime.guard_command) as the --mcp-config server
        "browser", no built-in tool (--tools ""), and --allowedTools carrying exactly the operation's guard
        tools (`browser_tools`, the Codex approval list); the answer by --json-schema from the session's
        schema.json, written to its output.json by finish_run. Reads run on claude_read_model; `main_model`
        (application_browser.MAIN_MODEL_OPERATIONS) on claude_model. MCP_TOOL_TIMEOUT per job: 110 s, or
        630 s when the CAPTCHA / sign-in wait is there (browser_runtime.HUMAN_CHECK_TOOL_TIMEOUT_SECONDS).
        Submit (U1 3f) is the Codex submit: the guard gets the private claim (--claim), so its one-shot
        `arm` (ArmBinding: deadline, lease, pause switch, one arm per request) is listed and allowed;
        no other path to a submit exists."""
        import browser_runtime
        import user_profile
        directory = Path(directory)
        operation = config.get("_browser_operation")
        claim = config.get("_application_guard_claim")
        if (operation == self.SUBMIT) != bool(claim) or (operation not in self.BROWSER_READS and not claim):
            raise NotImplementedError("CLAUDE_BROWSER_SESSION_REFUSED: a browser session needs a known operation, "
                                      "and the submit claim only with the submit operation")
        human_check = config.get("_application_human_check")
        argv = browser_runtime.guard_command(config, directory, operation, claim, human_check)
        # Answers over PIECE_CHARS come in pieces (browser_guard.CONTINUE_TOOL), so Claude never saves one
        # under ~/.claude/projects and the model reads the whole page, as on Codex.
        argv[argv.index("--"):argv.index("--")] = ["--piece-chars", str(self.PIECE_CHARS)]
        # The guard's environment as Codex gives it (codex_options): the user's session variables, so
        # Chrome finds its keychain, and Sidedoor's home, which the guard reads its policy from.
        environment = dict(browser_runtime.session_environment(),
                           **{user_profile.HOME_VARIABLE: str(user_profile.home())})
        if not config.get("claude"):
            config = self.prepare(config)
        mcp_config = self.mcp_config(directory, {browser_runtime.SERVER: argv}, name="browser.mcp.json",
                                     environment={browser_runtime.SERVER: environment})
        allowed = tuple("mcp__%s__%s" % (browser_runtime.SERVER, tool)
                        for tool in self.browser_tools(operation, human_check, claim))
        return self._built(config, directory / "output.json", (), allowed=allowed, mcp_config=mcp_config,
                           schema=directory / "schema.json", read=not main_model,
                           tool_timeout=(browser_runtime.HUMAN_CHECK_TOOL_TIMEOUT_SECONDS if human_check
                                         else self.TOOL_TIMEOUT_SECONDS),
                           startup_timeout=browser_runtime.STARTUP_TIMEOUT_SECONDS,
                           output_tokens=self.BROWSER_OUTPUT_TOKENS)

    # MAX_MCP_OUTPUT_TOKENS for a browser job (review E4): the guard and @playwright/mcp cap no result, so
    # the only bound is the session's trace limit (application_browser.MAX_TRACE, 32 MiB); a token is at
    # least one byte, so this many tokens covers any result the trace can hold and a big page reaches the
    # trace whole, as under Codex (Claude's default, 25,000, would cut it and save it under ~/.claude).
    BROWSER_OUTPUT_TOKENS = 32 * 1024 * 1024
    # Claude 2.1.301 saves a tool result over 50,000 characters under ~/.claude/projects and shows a 2 KB
    # preview (measured 2026-10-28: 49,989 shown whole, 53,183 saved); the guard's pieces stay well below.
    PIECE_CHARS = 40000

    # The failure codes that end a browser session at once, as the live monitor ends a Codex session on a
    # refused tool (application_browser.tool_event): a tool outside the run's list (the kill switch) and a
    # browser server that did not start. Every other code is judged when the run ends (check_run): a
    # refused, oversized or timed-out call keeps the answer (R1); limits and API errors come in the result.
    LIVE_STOPS = frozenset({FORBIDDEN_TOOL, MCP_SERVER_FAILED})

    # --- research and the agent worker (U1 3d): live web through WebSearch and WebFetch ---

    WEB_TOOLS = ("WebSearch", "WebFetch")

    def _built(self, config, output, tools, allowed=(), mcp_config=None, schema=None, read=False,
               tool_timeout=None, startup_timeout=None, output_tokens=None):
        if isinstance(config, dict) and not config.get("claude"):
            config = self.prepare(config)   # a job config that never went through setup_steps.with_claude
        build = {"config": config, "tools": tuple(tools), "allowed": tuple(allowed), "mcp_config": mcp_config,
                 "schema": schema, "read": read}
        return ClaudeCommand(self.command(**build), output=output, build=build, tool_timeout=tool_timeout,
                             startup_timeout=startup_timeout, output_tokens=output_tokens)

    def research_command(self, root, config, output_path, schema):
        """A research session (discovery, qualification): WebSearch and WebFetch, the answer by --json-schema
        (written to `output_path` by finish_run), the stream and stderr in the worker's log."""
        import resources
        path = resources.path(schema) if resources.declared(str(schema)) else Path(root) / schema
        return self._built(config, Path(output_path), self.WEB_TOOLS, schema=path)

    def research_recovery_command(self, config, command):
        """research_recovery: the research command's answer and schema with every tool off (no --tools
        entry, no MCP server). The text model is called directly before this is reached (research_recovery)."""
        if not isinstance(command, ClaudeCommand):
            raise ValueError("CLAUDE_COMMAND_REQUIRED")
        return self._built(config, command.output, (), schema=command.build.get("schema"))

    def mcp_server_options(self, server, argv, startup_timeout, tool_timeout, tools):
        """One stdio MCP server (research_tools) to attach: its argv, its tools (each allowed by name,
        mcp__<server>__<tool>) and its timeouts, the run's MCP_TIMEOUT and MCP_TOOL_TIMEOUT."""
        return {"server": server, "argv": list(argv), "startup_timeout": startup_timeout,
                "tool_timeout": tool_timeout, "tools": list(tools)}

    def attach(self, command, options):
        """`command` rebuilt with the server in `options` (mcp_server_options): its --mcp-config file is
        written beside the command's output (<output>.mcp.json) and its tools join --allowedTools. Any other
        command or option shape is returned as it is."""
        if not isinstance(command, ClaudeCommand) or not isinstance(options, dict) or command.output is None:
            return command
        output = Path(command.output)
        path = self.mcp_config(output.parent, {options["server"]: options["argv"]}, name=output.name + ".mcp.json")
        allowed = tuple(command.build.get("allowed") or ()) + tuple(
            "mcp__%s__%s" % (options["server"], tool) for tool in options["tools"])
        build = dict(command.build, allowed=tuple(dict.fromkeys(allowed)), mcp_config=path)
        return ClaudeCommand(self.command(**build), output=command.output, build=build,
                             tool_timeout=options.get("tool_timeout"), startup_timeout=options.get("startup_timeout"))

    def research_environment(self, command):
        """The allowlisted environment (never the shell's ANTHROPIC_*), with the command's MCP limits."""
        result = self.environment(command, tool_timeout=getattr(command, "tool_timeout", None))
        startup = getattr(command, "startup_timeout", None)
        if startup:
            result["MCP_TIMEOUT"] = str(int(startup * 1000))
        return result

    def investigation_command(self, config, directory):
        """agent_worker: a public-web investigation (WebSearch and WebFetch, no MCP server); the answer by
        --json-schema from the attempt's schema.json, written to its output.json by finish_run."""
        directory = Path(directory)
        return self._built(config, directory / "output.json", self.WEB_TOOLS, schema=directory / "schema.json")

    def finish_run(self, returncode, text, stderr="", command=None, worker=None, home=None):
        """Every Claude run once it ends, whatever its exit code (it can fail with exit 0): the model_calls
        row; the outcome (check_run): None for a clean run, whose answer is then written to
        `command.output` (write_answer) and whose sign-in waits end; a ProviderUsageLimit (a wait, noted
        for the banner); or a ClaudeRunFailure (a spent try)."""
        import provider_limits
        record = self.read_trace(text, allowed=self._allowed(None, command), strict=False)
        outcome = self.check_run(returncode, text, stderr, command=command, home=home)
        output = getattr(command, "output", None)
        if output is not None and not isinstance(outcome, provider_limits.ProviderUsageLimit):
            # R1: any answer is written (a failed run's too, so a cut-off note knows it answered).
            self.write_answer(Path(output).parent, record, path=output)
        if outcome is None:
            provider_limits.claude_fixed(home)
            provider_limits.clear(provider_limits.CLAUDE_USAGE_WAIT_FILE, home)   # L4: the usage limit is over
            try:
                import provider_pacing
                provider_pacing.succeeded(self.pacing_provider)
            except Exception:
                pass
        elif isinstance(outcome, provider_limits.ProviderUsageLimit):
            provider_limits.noted(outcome, home)
            if outcome.kind == provider_limits.RATE_LIMITED and outcome.stated_reset:
                provider_limits.claude_usage_noted(outcome, home)   # L4: the banner with the stated reset (resetsAt)
        try:
            self.record_model_call(record, worker=worker, home=home, outcome="ok" if outcome is None else (
                "wait" if isinstance(outcome, provider_limits.ProviderUsageLimit) else "refused"))
        except Exception:
            pass
        return outcome

    SCHEMA_REFUSED = "Error: --json-schema is not a valid JSON Schema"

    def pre_model_rejection(self, text, rejections):
        """Claude checks --json-schema itself, before any session starts: it exits 1, writes no event and
        prints SCHEMA_REFUSED on stderr (spike, 2026-10-27). A research log (stdout and stderr together)
        holding that line and no stream event is the invalid_json_schema rejection, when it is listed."""
        rejection = ("invalid_request_error", "invalid_json_schema", 400)
        if rejection not in rejections:
            return None
        found = None
        for line in (text or "").splitlines():
            stripped = line.strip()
            if stripped.startswith("{"):
                try:
                    json.loads(stripped)
                except ValueError:
                    pass
                else:
                    return None   # a stream event: the session started
            if found is None and stripped.startswith(self.SCHEMA_REFUSED):
                found = stripped
        if found is None:
            return None
        return ({"type": rejection[0], "code": rejection[1], "message": found[:500]},
                {"type": "error", "status": rejection[2]})

    # --- the step log ---

    @staticmethod
    def split_name(name):
        """(server, tool) of an MCP tool name mcp__<server>__<tool>; (None, name) for a built-in tool."""
        if isinstance(name, str) and name.startswith("mcp__"):
            server, _, tool = name[len("mcp__"):].partition("__")
            if server and tool:
                return server, tool
        return None, name

    @staticmethod
    def call_kind(name):
        if isinstance(name, str) and name.startswith("mcp__"):
            return "tool"
        return {"WebSearch": "web_search", "WebFetch": "web_fetch", "Bash": "shell"}.get(name, "other")

    # The run's allowed tools (--tools plus --allowedTools), when known: `for_run` returns a copy of the
    # engine bound to the set record_run saved, and `allowed_of(command)` reads it from a command.
    allowed = None

    def bound(self, allowed):
        """This engine with `allowed` as the run's tool set (None: unknown)."""
        copy = ClaudeEngine()
        copy.allowed = None if allowed is None else frozenset(allowed)
        return copy

    def allowed_of(self, command):
        """The tools a command built by `command()` allows (--tools and --allowedTools), else None."""
        if not self.built(command):
            return None
        found = []
        for flag in ("--tools", "--allowedTools"):
            if flag in command and command.index(flag) + 1 < len(command):
                found += [part for part in command[command.index(flag) + 1].split(",") if part]
        return frozenset(found)

    def run_record(self, command):
        """The extra fields record_run saves for a run of `command`: its allowed tools."""
        allowed = self.allowed_of(command)
        return {} if allowed is None else {"allowed": sorted(allowed)}

    def _allowed(self, allowed, command=None):
        if allowed is not None:
            return allowed
        if command is not None and self.allowed_of(command) is not None:
            return self.allowed_of(command)
        return self.allowed

    def trace_reader(self, allowed=None):
        return ClaudeTraceReader(self, self._allowed(allowed))

    def parse_line(self, line):
        """One line read on its own (no pairing): its first TraceEvent."""
        return self.trace_reader().feed(line)[0] if line.strip() else TraceEvent(None, None)

    def read_trace(self, text, allowed=None, strict=True):
        """A whole stream as a RunRecord (`error_kind`: the first failure code, `errors` all of them).
        Raises as `ClaudeTraceReader.feed` does unless `strict` is False (lines that are not events skipped)."""
        reader = self.trace_reader(allowed)
        messages, searches, fetches = [], [], []
        for number, line in enumerate((text or "").splitlines(), 1):
            if not line.strip():
                continue
            try:
                events = reader.feed(line)
            except ValueError:
                if strict:
                    raise
                continue
            for event in events:
                if event.phase == "completed" and event.kind == "message":
                    messages.append(event.text)
                elif event.phase == "completed" and event.kind == "web_search":
                    searches.append(number)
                elif event.phase == "completed" and event.kind == "web_fetch":
                    code = event.call.result.get("code")
                    fetches.append({"url": event.call.result.get("url") or event.call.args.get("url"), "code": code})
                    # A fetched page is a web read too (owner, U1 3d), counted with the searches: only a
                    # 2xx answer (tool_use_result's code; a 404 or 429 is not is_error) and never a failed call.
                    if (event.call.status == "completed" and type(code) is int and 200 <= code < 300):
                        searches.append(number)
        result = reader.result if isinstance(reader.result, dict) else {}
        if reader.answer is not None:   # a rejected rate_limit_event, but the run still answered: no wait
            reader.errors = [error for error in reader.errors if error[0] != self.RATE_LIMITED
                             or not error[1].startswith(self.REJECTED)]
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        tokens = {key: usage[key] for key in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                              "cache_creation_input_tokens") if type(usage.get(key)) is int}
        if tokens:
            tokens["total_tokens"] = sum(tokens.values())
        cost = result.get("total_cost_usd")
        record = RunRecord(list(reader.calls.values()), messages, tokens, searches, answer=reader.answer,
                           error_kind=reader.errors[0][0] if reader.errors else None,
                           denials=[denial for denial in result.get("permission_denials") or [] if isinstance(denial, dict)])
        record.errors = list(reader.errors)
        record.web_fetches = fetches
        record.cost = cost if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None
        record.model = (reader.init or {}).get("model")
        record.seconds = result.get("duration_ms") / 1000 if isinstance(result.get("duration_ms"), (int, float)) else None
        record.finished = bool(result)
        record.rate_limit = reader.rate_limit
        return record

    def write_answer(self, directory, record, path=None):
        """output.json in the run folder (or `path`) from result.structured_output, as Codex's
        --output-last-message writes it (atomic). Returns its path, or None when the run gave no answer."""
        if record.answer is None:
            return None
        target = Path(path) if path is not None else Path(directory) / "output.json"
        temporary = target.with_name(target.name + ".tmp-" + str(os.getpid()))
        try:
            temporary.write_text(json.dumps(record.answer) + "\n", encoding="utf-8")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    # --- errors, limits and usage ---

    def reset_at(self, message):
        """The reset a rate_limit_info states (resetsAt, epoch seconds; a dict or the number itself) as an
        aware UTC datetime, else None."""
        import datetime as dt
        value = message.get("resetsAt") if isinstance(message, dict) else message
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            return None
        try:
            return dt.datetime.fromtimestamp(value, dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    def check_run(self, returncode, text, stderr="", allowed=None, answer_expected=True, now=None, command=None,
                  home=None):
        """The outcome of a finished run from its stream (`text`): a provider_limits.ProviderUsageLimit
        (a no-try wait: RATE_LIMITED, the short wait with the stated reset; SIGNED_OUT, a sign-in wait), a
        ClaudeRunFailure (any exit-0 failure the reader found, a non-zero exit, or no structured answer), or
        None for a clean run. `allowed`: the run's tool list (default: from `command`, else the set this
        engine is bound to, `for_run`). In API-key mode a 401 is CLAUDE_KEY_REFUSED (the key was refused)."""
        import provider_limits
        record = self.read_trace(text, allowed=self._allowed(allowed, command), strict=False)
        codes = dict(reversed(record.errors))   # code -> its first message
        if self.RATE_LIMITED in codes:
            import provider_pacing
            reset = self.reset_at(record.rate_limit)
            stated = provider_limits.stated_retry_seconds(codes[self.RATE_LIMITED], now)
            if reset is not None:
                seconds = (reset - provider_limits._utc(now)).total_seconds()
                stated = seconds if 0 < seconds <= provider_limits.MAX_WAIT.total_seconds() else stated
            delay = provider_pacing.rate_limited(self.pacing_provider, stated)
            return provider_limits.ProviderUsageLimit(codes[self.RATE_LIMITED], now, kind=provider_limits.RATE_LIMITED,
                                                      delay=delay, reset_at=reset)
        if self.SIGNED_OUT in codes or (returncode and not record.finished
                                        and self.NOT_LOGGED_IN in "%s\n%s" % (stderr or "", text or "")):
            if self.api_key_mode(home):
                return provider_limits.ProviderUsageLimit(self.KEY_REFUSED_WORDS, now,
                                                          kind=provider_limits.CLAUDE_KEY_REFUSED)
            return provider_limits.ProviderUsageLimit(codes.get(self.SIGNED_OUT) or "CLAUDE_SIGNED_OUT", now,
                                                      kind=provider_limits.CLAUDE_SIGNED_OUT)
        # R1 (keep what was found): a run that gave its structured answer keeps it through a refused or
        # oversized tool call or an MCP call that timed out, as Codex keeps its last message; the answer
        # is judged as Codex's is. A forbidden tool (the kill switch), a failed MCP server (always ours:
        # --strict-mcp-config) and API errors still fail the run.
        errors = [error for error in record.errors
                  if not (record.answer is not None and error[0] in self.ANSWER_KEPT)]
        if errors:
            return ClaudeRunFailure(*errors[0])
        if returncode:
            return ClaudeRunFailure(self.EXITED, "exit %s: %s" % (returncode, (stderr or "")[-300:]))
        if answer_expected and record.answer is None:
            return ClaudeRunFailure(self.NO_ANSWER, "no structured_output")
        return None

    def limit_output(self, returncode, *texts, command=None):
        """The wait behind a run (`check_run` on its stream, then its stderr), else None. Unlike Codex, a
        Claude limit can come with exit 0, so the exit code is not required."""
        stream = next((text for text in texts if isinstance(text, str)), "")
        stderr = "\n".join(text for text in texts[1:] if isinstance(text, str))
        found = self.check_run(returncode, stream, stderr, answer_expected=False, command=command)
        import provider_limits
        return found if isinstance(found, provider_limits.ProviderUsageLimit) else None

    def limit(self, returncode, *paths, command=None):
        import provider_limits
        return self.limit_output(returncode, *[provider_limits._read(path) or "" for path in paths], command=command)

    def usage(self, record):
        return dict(record.usage) if isinstance(record, RunRecord) else {}

    def record_model_call(self, record, worker=None, outcome=None, home=None):
        """One model_calls row for a finished run (route "claude", provider "anthropic", the run's tokens;
        cost: result.total_cost_usd in API-key mode only, NULL on the user's subscription, where it is not
        money the user pays). Best effort, as every model_calls row."""
        import model_calls
        tokens = record.usage
        model_calls.record(self.ROUTE, outcome or ("ok" if record.error_kind is None and record.answer is not None
                                                   else "refused"),
                           worker=worker or model_calls.caller(), model=getattr(record, "model", None),
                           provider=self.pacing_provider,
                           prompt_tokens=sum(tokens.get(key, 0) for key in ("input_tokens", "cache_read_input_tokens",
                                                                            "cache_creation_input_tokens")) if tokens else None,
                           completion_tokens=tokens.get("output_tokens"), total_tokens=tokens.get("total_tokens"),
                           cost=record.cost if self.api_key_mode(home) else None, seconds=getattr(record, "seconds", None))

    # --- what the Failures page shows of a run ---

    def error_events(self, pairs):
        """(offset, code, message) for each failure the reader finds over [(byte offset, line)]; prompts,
        messages and tool arguments and results are never shown."""
        reader = self.trace_reader()   # the run's allowed set when bound (for_run)
        for offset, line in pairs:
            try:
                events = reader.feed(line)
            except ValueError:
                continue
            for event in events:
                if event.kind == "error":
                    yield offset, event.raw_kind, event.text

    def log_pairs(self, pairs, truncated):
        """Claude writes no plain worker log yet (3d): nothing shown."""
        return []


CLAUDE = ClaudeEngine()


# --- U1 phase 4: Antigravity ---------------------------------------------------------------------------
#
# Chosen only by config["engine"] == "antigravity". The flags and failure shapes are the ones the agy spikes
# proved on agy 1.4.8 (docs/product/design/U1-phase3-claude-plan.md, "agy spike 2 findings"):
# - `agy -p "" --input-format stream-json --output-format stream-json --json-schema <schema>`: the prompt is one
#   {"event": "user", ...} line on stdin (agy -p takes no prompt from plain stdin), the answer is
#   result.structured_output. Never --dangerously-skip-permissions.
# - agy has no per-run tool allowlist. Each run gets its own workspace folder (the child's cwd) holding
#   .agents/mcp_config.json (Sidedoor's servers plus the user's own, A2) and .agents/hooks.json, a PreToolUse hook
#   (`sidedoor agy-hook`) that hard-denies every tool outside the run's list. The hook cannot grant, so Setup adds
#   narrow mcp(<server>/<tool>) allow rules for Sidedoor's own tools to the user's global settings (A1,
#   `ensure_allow_rules`).
# - Exit 0 hides failures: success is structured_output present, no denied_actions and no tool outside the
#   list. Tool outputs in the stream are summaries; the full output is read from agy's brain folder
#   (~/.gemini/antigravity-cli/brain/<conversation>/.system_generated/steps/<n>/output.txt), copied into the
#   run's own folder when the run ends so later readers do not depend on agy's files.
# - A dead MCP server is silent, so Sidedoor checks its own servers before a run (`preflight`).

class AntigravityCommand(list):
    """An `agy -p` argv (AntigravityEngine.command). Besides the argv: `output` (the file finish_run fills from
    result.structured_output), `cwd` (the run's own workspace folder, which holds .agents/; every launcher
    starts the child there), `build` (the command arguments, so `attach` can add a server), `tool_timeout`
    (seconds: each MCP server's timeoutSeconds) and `allowed` (the run's tool list, `AntigravityEngine.allowed_of`)."""

    def __init__(self, argv, output=None, cwd=None, build=None, tool_timeout=None, allowed=()):
        super().__init__(argv)
        self.output, self.cwd, self.build = output, cwd, dict(build or {})
        self.tool_timeout, self.allowed = tool_timeout, frozenset(allowed)


class AntigravityTraceReader:
    """agy's `--output-format stream-json` log: one JSON event per line (`init`, `step_update`, `result`). A
    tool step arrives twice under the same step_index, ACTIVE then DONE or ERROR, so the reader keeps state.

    `feed(line)` returns the line's TraceEvents, under the same failure codes as Claude's reader:
    - a call_mcp_tool step is kind "tool" (server mapped back to Sidedoor's neutral name, `neutral_server`), id
      "<tool name>_<step index>" (call_mcp_tool_2); its result is the full output from the brain folder (`outputs`) when it is there.
      The same call by the tool's direct name ("mcp_<server>_<tool>", its arguments as the parameters) is read alike (A4).
    - search_web is "web_search"; finish (the schema answer), a read of an allowed server's tool
      descriptor (agy's own mcp/<server>/ cache, which the model may look at before a call) and a read of a tool
      output agy saved for this run (brain/<conversation>/.system_generated/steps/<n>/output.txt, A2) are not calls.
    - FORBIDDEN_TOOL: any other tool, or a tool of a server outside the list (the hook denies it too).
    - PERMISSION_DENIED: a soft-denied step ("permission check failed") or result.denied_actions.
    - TOOL_TIMEOUT: an MCP step that failed with a timeout.
    - RATE_LIMITED, SIGNED_OUT, API_ERROR: a result whose status is not SUCCESS, by its error words."""

    def __init__(self, engine, allowed=None, outputs=None):
        self.engine = engine
        self.allowed = None if allowed is None else frozenset(allowed)
        self.outputs = outputs        # callable (conversation_id, step_index) -> full output text or None
        self.calls = {}
        self.errors = []
        self.forbidden = set()        # step indexes already reported
        self.texts = {}               # step index -> the model's streamed words so far
        self.init = None
        self.conversation = None
        self.result = None

    def _error(self, code, message, call=None):
        self.errors.append((code, str(message)[:500]))
        return TraceEvent(None, "error", call=call, text=str(message)[:500], raw_kind=code)

    def _mcp_allowed(self, server, tool):
        if self.allowed is None:
            return isinstance(server, str) and bool(server)
        return "%s/%s" % (server, tool) in self.allowed or "%s/*" % server in self.allowed

    def _descriptor(self, path):
        """True for a read of an allowed server's tool descriptor in agy's own cache (mcp/<server>/<tool>.json)."""
        if not isinstance(path, str):
            return False
        try:
            relative = Path(os.path.normpath(path)).relative_to(self.engine.agy_home() / "mcp")
        except ValueError:
            return False
        parts = relative.parts
        return len(parts) == 2 and parts[1].endswith(".json") and self._mcp_allowed(parts[0], parts[1][:-5])

    def _saved_output(self, path, conversation=None):
        """True for a read of a tool output agy saved for THIS run (A2, 2026-10-29): agy keeps a large tool result
        in brain/<conversation>/.system_generated/steps/<n>/output.txt and shows the model only that path, so the
        model may read exactly those files of its own conversation (symlinks resolved, no "..")."""
        return self.engine.saved_output(path, conversation or self.conversation)

    def _direct(self, name):
        """(server, tool) for an allowed MCP tool called by its direct name ("mcp_<server>_<tool>", A4), else None."""
        return self.engine.direct_mcp(name, self.allowed)

    def feed(self, line):
        event = json.loads(line)
        if not isinstance(event, dict):
            raise TraceShapeError("INVALID_TRACE_EVENT")
        kind = event.get("event")
        if kind == "init":
            self.init = event.get("init") if isinstance(event.get("init"), dict) else {}
            self.conversation = event.get("conversation_id") or self.conversation
            return [TraceEvent(None, None, raw_kind="init")]
        if kind == "step_update" and isinstance(event.get("step_update"), dict):
            step = event["step_update"]
            self.conversation = step.get("conversation_id") or self.conversation
            if step.get("step_type") in ("tool", "finish") and (step.get("tool_name") or step.get("step_type") == "finish"):
                return self._tool(step)
            if step.get("step_type") == "agent_response" and isinstance(step.get("text_delta"), str):
                # The model's words stream as deltas under one step index; the message is complete at DONE.
                parts = self.texts.setdefault(step.get("step_index"), [])
                parts.append(step["text_delta"])
                if step.get("state") == "DONE":
                    return [TraceEvent("completed", "message", text="".join(self.texts.pop(step.get("step_index"))),
                                       status="completed")]
                return [TraceEvent(None, None, raw_kind="text_delta")]
            return [TraceEvent(None, None, raw_kind=step.get("step_type"))]
        if kind == "result" and isinstance(event.get("result"), dict):
            return self._result(event["result"])
        if kind is None and "status" in event and "conversation_id" in event:   # --output-format json
            return self._result(event)
        return [TraceEvent(None, None, raw_kind=kind)]

    def _tool(self, step):
        index = step.get("step_index")
        info = step.get("tool_info") if isinstance(step.get("tool_info"), dict) else {}
        name = step.get("tool_name") or info.get("name") or step.get("step_type")
        parameters = info.get("parameters") if isinstance(info.get("parameters"), dict) else {}
        state = step.get("state")
        error = info.get("error") if isinstance(info.get("error"), dict) else None
        words = str((error or {}).get("message") or "")
        if name == "finish":
            return [TraceEvent(None, None, raw_kind="finish")]
        if name == "read_file" and (self._descriptor(parameters.get("Path")) or self._saved_output(parameters.get("Path"))):
            return [TraceEvent(None, "other", raw_kind="read_file")]   # no call: never refused, never counted
        found = []
        direct = self._direct(name)
        if name == "call_mcp_tool":
            raw_server, tool = parameters.get("ServerName"), parameters.get("ToolName")
            arguments = parameters.get("Arguments") if isinstance(parameters.get("Arguments"), dict) else {}
            allowed, server, call_kind = self._mcp_allowed(raw_server, tool), self.engine.neutral_server(raw_server), "tool"
        elif direct is not None:   # A4: the same call by the tool's direct name, its arguments as the parameters
            (raw_server, tool), arguments = direct, parameters
            allowed, server, call_kind = True, self.engine.neutral_server(raw_server), "tool"
        else:
            raw_server, tool, arguments, server = None, name, parameters, None
            call_kind = "web_search" if name == "search_web" else "shell" if name == "run_command" else "other"
            allowed = self.allowed is None and name == "search_web" or (self.allowed is not None and name in self.allowed)
        if not allowed and index not in self.forbidden:
            self.forbidden.add(index)
            found.append(self._error(AntigravityEngine.FORBIDDEN_TOOL, "tool not allowed: %s" % (
                "%s/%s" % (raw_server, tool) if raw_server else name)))
        # The id the model itself names a call by (claude-sonnet-4-6 through agy cited "call_mcp_tool_2" for
        # step 2, 2026-10-28), so its evidence binds to the call.
        identifier = "%s_%s" % (name, index)
        started = self.calls.get(identifier) or ToolCall(id=identifier, server=server, tool=tool, args=arguments,
                                                         result=None, status="in_progress", error=None,
                                                         has_id=index is not None)
        if state not in ("DONE", "ERROR"):
            self.calls[identifier] = started
            return found + [TraceEvent("started", call_kind, started, status="in_progress")]
        failed = state == "ERROR"
        text = None
        if call_kind == "tool" and self.outputs is not None and not failed:
            text = self.outputs(self.conversation, index)
        if text is None:
            output = info.get("output")
            text = words if failed else (output if isinstance(output, str) else "")
        result = {"content": [{"type": "text", "text": text}] if text else [], "isError": failed}
        if failed and AntigravityEngine.SOFT_DENIED in words:
            found.append(self._error(AntigravityEngine.PERMISSION_DENIED, words))
        elif failed and call_kind == "tool" and any(word in words.lower() for word in AntigravityEngine.TIMEOUT_WORDS):
            found.append(self._error(AntigravityEngine.TOOL_TIMEOUT, words))
        call = dataclasses.replace(started, result=result, status="failed" if failed else "completed",
                                   error=(words or "failed") if failed else None)
        self.calls[identifier] = call
        return found + [TraceEvent("completed", call_kind, call, status=call.status)]

    def _result(self, result):
        self.result = result
        self.conversation = result.get("conversation_id") or self.conversation
        found = []
        for denial in result.get("denied_actions") or []:
            if isinstance(denial, dict):
                found.append(self._error(AntigravityEngine.PERMISSION_DENIED, "permission denied: %s" % (
                    denial.get("action"))))
        if result.get("status") not in (None, "SUCCESS"):
            words = str(result.get("error") or result.get("response") or result.get("status"))
            lowered = words.lower()
            if any(word in lowered for word in AntigravityEngine.RATE_WORDS):
                found.append(self._error(AntigravityEngine.RATE_LIMITED, words))
            elif any(word in lowered for word in AntigravityEngine.SIGNED_OUT_WORDS):
                found.append(self._error(AntigravityEngine.SIGNED_OUT, words))
            else:
                found.append(self._error(AntigravityEngine.API_ERROR, words))
        return found + [TraceEvent("completed", "other", text=result.get("response"), raw_kind="result")]

    @property
    def answer(self):
        return self.result.get("structured_output") if isinstance(self.result, dict) else None


class AntigravityEngine(Engine):
    name = "antigravity"
    pacing_provider = "google"
    # The child's allowlist: HOME and USER find the keychain sign-in (spike: `env -i HOME USER PATH` works);
    # LOGNAME, TMPDIR and the Windows profile variables as for Claude.
    ENVIRONMENT = frozenset({"HOME", "USER", "LOGNAME", "PATH", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT",
                             "USERPROFILE", "APPDATA", "LOCALAPPDATA"})
    # Owner pick M2 = b (2026-10-28): Claude models through agy by default (Gemini 3.8 Flash froze on the final
    # answer of a browser inspect three times); users pick their own in Setup or Settings (antigravity_model /
    # antigravity_read_model, laid over the config by setup_steps.with_engine).
    MAIN_MODEL, READ_MODEL = "claude-opus-4-6-thinking", "claude-sonnet-4-6"
    TOOL_TIMEOUT_SECONDS = 110
    EXECUTABLES = frozenset({"agy", "agy.exe", "agy.cmd"})
    ROUTE = "antigravity"
    WORKSPACE = "agy"                     # the run's workspace folder inside its run folder
    STEPS = "steps"                       # full tool outputs copied there when the run ends
    POLICY = "sidedoor-policy.json"
    WEB_TOOLS = ("search_web",)           # no fetch: agy's read_url_content is never offered (spike 2)
    # Sidedoor's own servers under names no user server is likely to have, so the A1 rules stay narrow.
    SERVER_NAMES = {"browser": "sidedoor_browser", "research_tools": "sidedoor_research_tools"}

    FORBIDDEN_TOOL = ClaudeEngine.FORBIDDEN_TOOL
    PERMISSION_DENIED = ClaudeEngine.PERMISSION_DENIED
    TOOL_TIMEOUT = ClaudeEngine.TOOL_TIMEOUT
    MCP_SERVER_FAILED = ClaudeEngine.MCP_SERVER_FAILED
    RATE_LIMITED = ClaudeEngine.RATE_LIMITED
    SIGNED_OUT = ClaudeEngine.SIGNED_OUT
    API_ERROR = ClaudeEngine.API_ERROR
    NO_ANSWER = ClaudeEngine.NO_ANSWER
    EXITED = "ANTIGRAVITY_EXITED"
    LIVE_STOPS = ClaudeEngine.LIVE_STOPS
    ANSWER_KEPT = frozenset({TOOL_TIMEOUT})   # a denial always fails the run (owner rule for agy)
    SOFT_DENIED = "permission check failed"
    TIMEOUT_WORDS = ("timed out", "deadline exceeded", "timeout")
    RATE_WORDS = ("429", "resource_exhausted", "resource exhausted", "quota", "rate limit", "out of credits",
                  "too many requests")
    SIGNED_OUT_WORDS = ("authentication failed", "not logged in", "unauthenticated", "sign in", "401")
    judged_by_stream = True
    allowed = None
    steps = None                          # the run's copied outputs folder, when bound (for_run)

    def built(self, command):
        return (isinstance(command, list) and bool(command) and isinstance(command[0], str)
                and Path(command[0]).name.lower() in self.EXECUTABLES and "-p" in command and "stream-json" in command)

    def provider_key(self, command):
        return self.pacing_provider

    @staticmethod
    def agy_home(home=None):
        """agy's own folder (~/.gemini/antigravity-cli) of the user `home` (default: this user's)."""
        return Path(home or Path.home()) / ".gemini" / "antigravity-cli"

    def neutral_server(self, name):
        return {value: key for key, value in self.SERVER_NAMES.items()}.get(name, name)

    def direct_mcp(self, name, allowed):
        """(server, tool) when `name` is an MCP tool's direct name, "mcp_<server>_<tool>" (A4: a model called
        mcp_sidedoor_research_tools_save_role instead of call_mcp_tool, 2026-10-28), of a tool `allowed` lets
        through ("server/tool", or any tool of "server/*"; unbound, Sidedoor's own servers); else None."""
        if not isinstance(name, str) or not name.startswith("mcp_"):
            return None
        entries = allowed if allowed is not None else {"%s/*" % server for server in self.SERVER_NAMES.values()}
        # G3: every server name the run knows (and Sidedoor's own), so a shorter user server "sidedoor/*" never
        # takes a name that belongs to a longer one ("mcp_sidedoor_research_tools_delete_role").
        servers = {entry.partition("/")[0] for entry in entries} | set(self.SERVER_NAMES.values())
        for entry in sorted(entries, key=lambda entry: (entry.endswith("/*"), -len(entry))):
            server, _, tool = entry.partition("/")
            if not server or not tool:
                continue
            if tool != "*" and name == "mcp_%s_%s" % (server, tool):
                return server, tool
            prefix = "mcp_%s_" % server
            if tool == "*" and name.startswith(prefix) and len(name) > len(prefix) and not any(
                    len(other) > len(server) and name.startswith("mcp_%s_" % other) for other in servers):
                return server, name[len(prefix):]
        return None

    def saved_output(self, path, conversation):
        """True when `path` (a plain path or a file:// URL) is agy's saved output of a step of `conversation`:
        <agy home>/brain/<conversation>/.system_generated/steps/<n>/output.txt, after resolving symlinks and
        with no ".." in it (A2)."""
        if not isinstance(path, str) or not isinstance(conversation, str) or not re.fullmatch(
                r"[0-9A-Za-z-]{1,80}", conversation):
            return False
        if "?" in path or "#" in path:   # G1: nothing a URL parser would drop from the path
            return False
        if path.startswith("file://"):
            from urllib.parse import unquote, urlparse
            parsed = urlparse(path)
            if parsed.netloc not in ("", "localhost") or parsed.params or parsed.query or parsed.fragment:
                return False
            path = unquote(parsed.path)
            if re.match(r"^/[A-Za-z]:[/\\]", path):   # file:///C:/... on Windows
                path = path[1:]
        if ".." in re.split(r"[/\\]", path) or not os.path.isabs(path):
            return False
        steps = Path(os.path.realpath(self.agy_home() / "brain" / conversation / ".system_generated" / "steps"))
        try:
            parts = Path(os.path.realpath(path)).relative_to(steps).parts
        except ValueError:
            return False
        return len(parts) == 2 and re.fullmatch(r"[0-9]+", parts[0]) is not None and parts[1] == "output.txt"

    # --- finding, signing in, A1 ---

    def prepare(self, config, home=None):
        import setup_steps
        return setup_steps.with_antigravity(config, home)

    def wait(self, config, home=None, run=None):
        """A no-try wait before a launch, else None: ANTIGRAVITY_NOT_FOUND when no usable agy is found, and
        ANTIGRAVITY_SIGNED_OUT when `agy models` (no model turn) says it is not signed in (a probe that
        cannot run decides nothing). The A1 allow rules are made sure of here too (idempotent)."""
        import provider_limits
        import setup_steps
        if not isinstance(config, dict):
            return None
        if not config.get(setup_steps.ANTIGRAVITY_MISSING) and not setup_steps._usable(config.get("antigravity")):
            config = setup_steps.with_antigravity(config, home)
        missing = setup_steps.antigravity_wait(config, home)
        if missing:
            return missing
        try:
            self.ensure_allow_rules()
        except (OSError, ValueError):
            pass   # an unreadable settings file is never rewritten; the run then reports the denial
        status = setup_steps.antigravity_signed_in(config["antigravity"], self.environment(None),
                                                   **({"run": run} if run else {}))
        if status is False:
            return provider_limits.noted(provider_limits.ProviderUsageLimit(
                "ANTIGRAVITY_SIGNED_OUT: Antigravity is not signed in", kind=provider_limits.ANTIGRAVITY_SIGNED_OUT), home)
        return None

    def sidedoor_tools(self):
        """{agy server name: [tool]} of every tool Sidedoor's own servers offer (the A1 rules)."""
        import browser_guard
        import research_tools
        return {self.SERVER_NAMES["browser"]: list(browser_guard.TOOLS) + [browser_guard.ARM_TOOL,
                                                                            browser_guard.HUMAN_CHECK_TOOL],
                self.SERVER_NAMES["research_tools"]: [tool["name"] for phase in ("discovery", "qualification")
                                                      for tool in research_tools.tools(phase)]}

    def allow_rules(self, user_home=None, probe=False, present=()):
        """Sidedoor's own tools, then the user's own servers' tools (A2): agy refuses an MCP call that has no
        global allow rule even when the run's hook allows it (checked live 2026-10-28, owner pick N3)."""
        tools = dict(self.sidedoor_tools())
        tools.update(self.user_server_tools(user_home, probe=probe, present=present))
        return ["mcp(%s/%s)" % (server, tool) for server, names in tools.items() for tool in names]

    USER_TOOLS_SECONDS = 10
    NAME = re.compile(r"[A-Za-z0-9_.-]{1,100}")   # a server or tool name an allow rule may carry (N3b/N3c)

    def user_server_tools(self, user_home=None, run=None, probe=False, present=()):
        """{server: [tool]} for each of the user's own servers (`user_servers`): the tools agy cached for it
        (mcp/<server>/<tool>.json), or, for a stdio server agy has not cached yet, the tools it lists itself (MCP
        tools/list, a short handshake). The handshake runs only in Setup (`probe`) or for a server that has no
        rule in the settings yet (`present`): never before every run (N3a). Every name must match NAME; others
        are dropped (N3b/N3c). A server that cannot be read is skipped on its own (N3d)."""
        found = {}
        empty = self._probed_empty()
        for name, spec in self.user_servers(user_home).items():
            try:
                if not self.NAME.fullmatch(name):
                    continue
                key = name + ":" + hashlib.sha256(json.dumps(spec, sort_keys=True, default=str).encode()).hexdigest()
                names = set()
                try:
                    names |= {path.name[:-len(".json")] for path in (self.agy_home(user_home) / "mcp" / name).glob("*.json")}
                except OSError:
                    pass
                ruled = any(isinstance(rule, str) and rule.startswith("mcp(%s/" % name) for rule in present)
                # P1: a server that listed nothing (or failed, or hung) is recorded under its name and spec, so
                # runs never start it again; Setup's Check (`probe`) does, and a changed spec does.
                if not names and (probe or (not ruled and key not in empty)):
                    names |= {tool for tool in self._listed_tools(spec, run)
                              if isinstance(tool, str) and self.NAME.fullmatch(tool)}
                    self._record_probed(key, empty=not names)
                names = {tool for tool in names if isinstance(tool, str) and self.NAME.fullmatch(tool)}
                if names:
                    found[name] = sorted(names)
            except Exception:   # one unreadable server never stops the others or Sidedoor's own rules
                continue
        return found

    PROBED_FILE = "antigravity-probed.json"

    def _probed_path(self):
        import user_profile
        return Path(user_profile.home()) / self.PROBED_FILE

    def _probed_empty(self):
        """The user servers (name plus a hash of their spec) whose tools/list found nothing (P1)."""
        try:
            value = json.loads(self._probed_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        return {item for item in value if isinstance(item, str)} if isinstance(value, list) else set()

    def _record_probed(self, key, empty):
        try:
            keys = self._probed_empty()
            keys = (keys | {key}) if empty else (keys - {key})
            path = self._probed_path()
            temporary = path.with_name(path.name + ".tmp-" + str(os.getpid()))
            temporary.write_text(json.dumps(sorted(keys)) + "\n", encoding="utf-8")
            os.replace(temporary, path)
        except OSError:
            pass

    def _listed_tools(self, spec, run=None):
        """The tool names a stdio server lists (MCP initialize, then tools/list), within USER_TOOLS_SECONDS:
        its whole process tree is killed at the deadline (no hang on Windows either). Empty on any failure."""
        import subprocess
        command, arguments = spec.get("command"), spec.get("args") or []
        if not isinstance(command, str) or not isinstance(arguments, list) or not all(isinstance(a, str) for a in arguments):
            return set()
        messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                        "protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "sidedoor-setup", "version": "1"}}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        extra = spec.get("env") if isinstance(spec.get("env"), dict) else {}
        environment = dict(self.base_environment(), **{str(key): str(value) for key, value in extra.items()})
        text = "".join(json.dumps(message) + "\n" for message in messages)
        if run is not None:   # tests
            output = run([command] + arguments, input=text, capture_output=True, text=True,
                         timeout=self.USER_TOOLS_SECONDS, env=environment).stdout or ""
        else:
            import portable
            worker = subprocess.Popen([command] + arguments, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, text=True, env=environment,
                                      **portable.worker_spawn_options(core=False))
            portable.contain(worker)   # P2: Windows starts it suspended; the Job Object takes it, then it runs
            try:
                output, _ = worker.communicate(text, timeout=self.USER_TOOLS_SECONDS)
            except subprocess.TimeoutExpired:
                portable.kill_tree(worker.pid)
                try:
                    output, _ = worker.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    worker.kill()
                    output = ""
            finally:
                # W1: always, also for a server that exited by itself: on Windows this closes its Job Object
                # (a handle that otherwise stays open until Sidedoor exits) and ends anything it left behind.
                portable.kill_tree(worker.pid)
        names = set()
        for line in (output or "").splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            listed = value.get("result", {}).get("tools") if isinstance(value, dict) and value.get("id") == 2 \
                and isinstance(value.get("result"), dict) else None
            names |= {tool.get("name") for tool in listed or [] if isinstance(tool, dict)}
        return names

    def ensure_allow_rules(self, user_home=None, probe=False):
        """A1: add an mcp(<server>/<tool>) allow rule for each of Sidedoor's own tools (and, owner pick N3, each tool
        of the user's own servers, `user_server_tools`) to the user's global
        Antigravity settings (~/.gemini/antigravity-cli/settings.json), keeping everything already there, in
        order. Idempotent (nothing is written when every rule is there) and atomic (a temporary file, then a
        rename). A settings file that is not a JSON object is never rewritten (ValueError). Returns the rules
        added."""
        path = self.agy_home(user_home) / "settings.json"
        if path.is_symlink():   # a linked settings file (dotfiles): write its target, so the link is kept
            path = Path(os.path.realpath(path))
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = ""
        value = json.loads(text) if text.strip() else {}
        if not isinstance(value, dict):
            raise ValueError("ANTIGRAVITY_SETTINGS_INVALID")
        permissions = value.get("permissions", {})
        if not isinstance(permissions, dict) or not isinstance(permissions.get("allow", []), list):
            raise ValueError("ANTIGRAVITY_SETTINGS_INVALID")
        present = list(permissions.get("allow", []))
        added = [rule for rule in self.allow_rules(user_home, probe=probe, present=present) if rule not in present]
        if not added:
            return []
        value["permissions"] = dict(permissions, allow=present + added)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".sidedoor-tmp-" + str(os.getpid()))
        try:
            temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
            if path.exists():
                os.chmod(temporary, path.stat().st_mode & 0o777)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return added

    # --- the run's workspace and command ---

    def model(self, config, read=False):
        key = "antigravity_read_model" if read else "antigravity_model"
        return (config.get(key) if isinstance(config, dict) else None) or (self.READ_MODEL if read else self.MAIN_MODEL)

    def user_servers(self, user_home=None):
        """A2: the user's own servers from their global agy config (~/.gemini/config/mcp_config.json), else {}."""
        try:
            value = json.loads((Path(user_home or Path.home()) / ".gemini" / "config" / "mcp_config.json")
                               .read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        servers = value.get("mcpServers") if isinstance(value, dict) else None
        if not isinstance(servers, dict):
            return {}
        return {name: spec for name, spec in servers.items()
                if isinstance(name, str) and isinstance(spec, dict) and name not in self.SERVER_NAMES.values()}

    @staticmethod
    def hook_command(policy):
        """The PreToolUse hook's shell command (agy runs it through sh -c / cmd /c): `sidedoor agy-hook`."""
        import subprocess
        import sys
        import browser_runtime
        argv = (browser_runtime.sidedoor_command() + ["agy-hook"] if browser_runtime.frozen() else
                [sys.executable, str(Path(__file__).resolve())] + ["agy-hook"])
        argv += ["--policy", str(policy)]
        if sys.platform.startswith("win"):
            return subprocess.list2cmdline(argv)
        import shlex
        return " ".join(shlex.quote(part) for part in argv)

    def write_workspace(self, workspace, servers, builtin, tool_timeout=None, user_home=None):
        """The run's workspace: .agents/mcp_config.json (`servers`: {neutral name: (argv, env, tools)}, under
        their agy names, plus the user's own servers, A2), .agents/hooks.json and the hook's policy (those
        servers' tools, the user's servers' tools, `builtin` and finish). Returns the run's allowed set."""
        agents = Path(workspace) / ".agents"
        agents.mkdir(parents=True, exist_ok=True)
        user = self.user_servers(user_home)
        configured = dict(user)
        allowed = set(builtin) | {"%s/*" % name for name in user}
        for neutral, (argv, environment, tools) in servers.items():
            name = self.SERVER_NAMES.get(neutral, neutral)
            configured[name] = dict({"command": argv[0], "args": list(argv[1:]),
                                     "timeoutSeconds": int(tool_timeout or self.TOOL_TIMEOUT_SECONDS)},
                                    **({"env": dict(environment)} if environment else {}))
            allowed |= {"%s/%s" % (name, tool) for tool in tools}
        # 0600: it holds the servers' environments (the user's own servers' too, A2). Removed when the run ends
        # (`finish_run`, and `discard_workspace_config` for a run that was stopped); nothing reads it after.
        config_path = agents / "mcp_config.json"
        config_path.unlink(missing_ok=True)
        descriptor = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps({"mcpServers": configured}, indent=2) + "\n")
        policy = agents / self.POLICY
        policy.write_text(json.dumps({"allowed": sorted(allowed), "agy_home": str(self.agy_home(user_home))}) + "\n",
                          encoding="utf-8")
        hooks = {"sidedoor-policy": {"PreToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": self.hook_command(policy), "timeout": 30}]}]}}
        (agents / "hooks.json").write_text(json.dumps(hooks, indent=2) + "\n", encoding="utf-8")
        return frozenset(allowed)

    def command(self, config, schema, read=False):
        """`agy -p ""` reading the prompt as one stream-json line on stdin (`prompt_input`), the answer by
        --json-schema (`schema`: a path or a dict). Never --dangerously-skip-permissions."""
        if not isinstance(schema, dict):
            schema = json.loads(Path(schema).read_text(encoding="utf-8"))
        return [config["antigravity"], "--output-format", "stream-json", "--input-format", "stream-json",
                "--model", self.model(config, read), "--disable-slash-commands",
                "--json-schema", json.dumps(schema, separators=(",", ":")), "-p", ""]

    def _built(self, config, output, workspace, servers=None, builtin=(), schema=None, read=False, tool_timeout=None):
        if isinstance(config, dict) and not config.get("antigravity"):
            config = self.prepare(config)
        if isinstance(config, dict) and not config.get("antigravity"):
            raise OSError("ANTIGRAVITY_NOT_FOUND: no usable agy was found")
        servers = dict(servers or {})
        self.preflight(servers)
        allowed = self.write_workspace(workspace, servers, builtin, tool_timeout)
        build = {"config": config, "output": output, "workspace": workspace, "servers": servers,
                 "builtin": tuple(builtin), "schema": schema, "read": read, "tool_timeout": tool_timeout}
        return AntigravityCommand(self.command(config, schema, read), output=output, cwd=str(workspace), build=build,
                                  tool_timeout=tool_timeout, allowed=allowed)

    def prompt_input(self, prompt):
        """What a launcher writes to the child's stdin for `prompt`: one stream-json user line."""
        if prompt is None:
            return None
        return json.dumps({"event": "user", "message": {"role": "user", "content": prompt}}) + "\n"

    PREFLIGHT_SECONDS = 20

    def preflight(self, servers, run=None):
        """Sidedoor's own MCP servers must start, because agy says nothing about one that does not: each
        server's program must exist and be executable (and, run from source, its script), and research_tools
        must answer an MCP initialize. Raises ClaudeRunFailure(MCP_SERVER_FAILED) otherwise. The browser guard
        is only checked for its program: starting it would start the session's browser."""
        import subprocess
        for neutral, (argv, environment, tools) in servers.items():
            program = argv[0] if argv else None
            if not program or not (os.path.isfile(program) and os.access(program, os.X_OK)):
                raise ClaudeRunFailure(self.MCP_SERVER_FAILED, "MCP server %s: program not found" % neutral)
            script = next((part for part in argv[1:4] if part.endswith(".py")), None)
            if script and not os.path.isfile(script):
                raise ClaudeRunFailure(self.MCP_SERVER_FAILED, "MCP server %s: script not found" % neutral)
            if neutral != "research_tools":
                continue
            message = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "sidedoor-preflight",
                                                                                     "version": "1"}}}) + "\n"
            try:
                finished = (run or subprocess.run)(argv, input=message, capture_output=True, text=True,
                                                   timeout=self.PREFLIGHT_SECONDS,
                                                   env=dict(self.base_environment(), **(environment or {})))
                answered = any('"result"' in line for line in (finished.stdout or "").splitlines())
            except (OSError, subprocess.SubprocessError):
                answered = False
            if not answered:
                raise ClaudeRunFailure(self.MCP_SERVER_FAILED, "MCP server %s did not answer" % neutral)

    def environment(self, arguments, variables=None, config=None, text=False, home=None, **ignored):
        if text:
            raise NotImplementedError("ANTIGRAVITY_TEXT_WORKER: text workers call the text model directly")
        return dict(self.base_environment() if variables is None else variables)

    def research_environment(self, command):
        return self.environment(command)

    def succeeded(self, arguments):
        """Nothing here: an agy run is judged by `finish_run` (it can fail with exit 0)."""

    # --- workers ---

    def investigation_command(self, config, directory):
        """agent_worker: a public-web investigation (search_web only; agy has no usable fetch)."""
        directory = Path(directory)
        return self._built(config, directory / "output.json", directory / self.WORKSPACE, builtin=self.WEB_TOOLS,
                           schema=directory / "schema.json")

    def research_command(self, root, config, output_path, schema):
        import resources
        path = resources.path(schema) if resources.declared(str(schema)) else Path(root) / schema
        output = Path(output_path)
        return self._built(config, output, output.with_name(output.name + "." + self.WORKSPACE),
                           builtin=self.WEB_TOOLS, schema=path)

    def research_recovery_command(self, config, command):
        if not isinstance(command, AntigravityCommand):
            raise ValueError("ANTIGRAVITY_COMMAND_REQUIRED")
        return self._built(config, command.output, command.cwd, schema=command.build.get("schema"))

    def mcp_server_options(self, server, argv, startup_timeout, tool_timeout, tools):
        return {"server": server, "argv": list(argv), "startup_timeout": startup_timeout,
                "tool_timeout": tool_timeout, "tools": list(tools)}

    def attach(self, command, options):
        if not isinstance(command, AntigravityCommand) or not isinstance(options, dict):
            return command
        build = dict(command.build)
        servers = dict(build.get("servers") or {})
        servers[options["server"]] = (list(options["argv"]), None, list(options["tools"]))
        return self._built(build["config"], build["output"], build["workspace"], servers=servers,
                           builtin=build["builtin"], schema=build["schema"], read=build["read"],
                           tool_timeout=options.get("tool_timeout") or build.get("tool_timeout"))

    BROWSER_READS = ClaudeEngine.BROWSER_READS
    SUBMIT = ClaudeEngine.SUBMIT

    def browser_command(self, config, directory, application=False, main_model=False):
        """A browser session on agy: the guarded browser (browser_runtime.guard_command, with the private
        claim for a submit, exactly as for Codex and Claude) as the server sidedoor_browser, its tools the
        Codex approval list (ClaudeEngine.browser_tools less browser_continue: agy gets whole answers), no
        built-in tool. The answer by --json-schema from schema.json, written to output.json by finish_run."""
        import browser_runtime
        import user_profile
        directory = Path(directory)
        operation = config.get("_browser_operation")
        claim = config.get("_application_guard_claim")
        if (operation == self.SUBMIT) != bool(claim) or (operation not in self.BROWSER_READS and not claim):
            raise NotImplementedError("ANTIGRAVITY_BROWSER_SESSION_REFUSED: a browser session needs a known "
                                      "operation, and a submit (only) holds the arm claim")
        human_check = config.get("_application_human_check")
        argv = browser_runtime.guard_command(config, directory, operation, claim, human_check)
        environment = dict(browser_runtime.session_environment(),
                           **{user_profile.HOME_VARIABLE: str(user_profile.home())})
        import browser_guard
        tools = [tool for tool in CLAUDE.browser_tools(operation, human_check, claim) if tool != browser_guard.CONTINUE_TOOL]
        return self._built(config, directory / "output.json", directory / self.WORKSPACE,
                           servers={browser_runtime.SERVER: (argv, environment, tools)},
                           schema=directory / "schema.json", read=not main_model,
                           tool_timeout=(browser_runtime.HUMAN_CHECK_TOOL_TIMEOUT_SECONDS if human_check
                                         else self.TOOL_TIMEOUT_SECONDS))

    # --- the step log ---

    def bound(self, allowed, steps=None):
        copy = AntigravityEngine()
        copy.allowed = None if allowed is None else frozenset(allowed)
        copy.steps = steps
        return copy

    def allowed_of(self, command):
        return command.allowed if isinstance(command, AntigravityCommand) else None

    def run_record(self, command):
        if not isinstance(command, AntigravityCommand):
            return {}
        return {"allowed": sorted(command.allowed), "steps": str(Path(command.cwd) / self.STEPS)}

    def _allowed(self, allowed, command=None):
        if allowed is not None:
            return allowed
        if self.allowed_of(command) is not None:
            return self.allowed_of(command)
        return self.allowed

    def output_of(self, steps=None):
        """(conversation, step index) -> the step's full output: the run's copy first, then agy's brain folder."""
        steps = steps or self.steps

        def read(conversation, index):
            candidates = [Path(steps) / ("%s.txt" % index)] if steps else []
            if isinstance(conversation, str) and re.fullmatch(r"[0-9A-Za-z-]{1,80}", conversation):
                candidates.append(self.agy_home() / "brain" / conversation / ".system_generated" / "steps"
                                  / str(index) / "output.txt")
            for candidate in candidates:
                try:
                    return candidate.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
            return None
        return read

    def trace_reader(self, allowed=None):
        return AntigravityTraceReader(self, self._allowed(allowed), self.output_of())

    def parse_line(self, line):
        return self.trace_reader().feed(line)[0] if line.strip() else TraceEvent(None, None)

    def read_trace(self, text, allowed=None, strict=True, steps=None):
        reader = AntigravityTraceReader(self, self._allowed(allowed), self.output_of(steps))
        messages, searches = [], []
        for number, line in enumerate((text or "").splitlines(), 1):
            if not line.strip():
                continue
            try:
                events = reader.feed(line)
            except ValueError:
                if strict:
                    raise
                continue
            for event in events:
                if event.phase == "completed" and event.kind == "message":
                    messages.append(event.text)
                elif event.phase == "completed" and event.kind == "web_search" and event.call.status == "completed":
                    searches.append(number)
        result = reader.result if isinstance(reader.result, dict) else {}
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        tokens = {key: usage[key] for key in ("input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens",
                                              "total_tokens") if type(usage.get(key)) is int}
        record = RunRecord(list(reader.calls.values()), messages, tokens, searches, answer=reader.answer,
                           error_kind=reader.errors[0][0] if reader.errors else None,
                           denials=[denial for denial in result.get("denied_actions") or [] if isinstance(denial, dict)])
        record.errors = list(reader.errors)
        record.model = (reader.init or {}).get("model")
        seconds = result.get("duration_seconds")
        record.seconds = seconds if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) else None
        record.finished = bool(result)
        record.conversation = reader.conversation
        record.step_indexes = [call.id.rpartition("_")[2] for call in reader.calls.values()
                               if call.server is not None and isinstance(call.id, str)]
        return record

    def write_answer(self, directory, record, path=None):
        """As ClaudeEngine.write_answer. agy never shows the model its step ids, so a browser answer's
        evidence[].tool_call_id that names no call of this run (the model made it up; seen 2026-10-28) is
        written as null, which the host then binds by its quote, as it does for a null id under Claude."""
        answer = record.answer
        if isinstance(answer, dict) and isinstance(answer.get("evidence"), list):
            known = {call.id for call in record.tool_calls}
            # A "context:" id cites the request's saved context, not a call: kept as it is, as under Claude.
            answer = dict(answer, evidence=[
                dict(item, tool_call_id=None) if isinstance(item, dict) and item.get("tool_call_id") is not None
                and item.get("tool_call_id") not in known
                and not str(item.get("tool_call_id")).startswith("context:") else item for item in answer["evidence"]])
            record = dataclasses.replace(record, answer=answer)
        return ClaudeEngine.write_answer(self, directory, record, path)

    def keep_outputs(self, record, command):
        """Copy each MCP step's full output from agy's brain folder into the run's own <workspace>/steps."""
        if not isinstance(command, AntigravityCommand) or not getattr(record, "conversation", None):
            return
        source = self.output_of(None)
        target = Path(command.cwd) / self.STEPS
        for index in getattr(record, "step_indexes", []):
            text = source(record.conversation, index)
            if text is not None:
                target.mkdir(parents=True, exist_ok=True)
                (target / ("%s.txt" % index)).write_text(text, encoding="utf-8")

    def run_ended(self, command):
        self.discard_workspace_config(command)

    @staticmethod
    def discard_workspace_config(command):
        """Remove a run's .agents/mcp_config.json (it holds server environments) once the run has ended."""
        cwd = getattr(command, "cwd", None)
        if isinstance(command, AntigravityCommand) and cwd:
            try:
                (Path(cwd) / ".agents" / "mcp_config.json").unlink(missing_ok=True)
            except OSError:
                pass

    # --- the outcome ---

    def finish_run(self, returncode, text, stderr="", command=None, worker=None, home=None):
        """As ClaudeEngine.finish_run: the model_calls row; None for a clean run (answer written, sign-in wait
        ended), a ProviderUsageLimit (a wait, noted) or a ClaudeRunFailure (a spent try)."""
        import provider_limits
        self.discard_workspace_config(command)
        record = self.read_trace(text, allowed=self._allowed(None, command), strict=False)
        try:
            self.keep_outputs(record, command)
        except OSError:
            pass
        outcome = self.check_run(returncode, text, stderr, command=command)
        output = getattr(command, "output", None)
        if output is not None and not isinstance(outcome, provider_limits.ProviderUsageLimit):
            self.write_answer(Path(output).parent, record, path=output)
        if outcome is None:
            provider_limits.clear(provider_limits.ANTIGRAVITY_WAIT_FILES[provider_limits.ANTIGRAVITY_SIGNED_OUT], home)
            provider_limits.clear(provider_limits.ANTIGRAVITY_USAGE_WAIT_FILE, home)   # A1: the usage limit is over
            try:
                import provider_pacing
                provider_pacing.succeeded(self.pacing_provider)
            except Exception:
                pass
        elif isinstance(outcome, provider_limits.ProviderUsageLimit):
            provider_limits.noted(outcome, home)
            if outcome.kind == provider_limits.RATE_LIMITED and outcome.stated_reset:
                provider_limits.antigravity_usage_noted(outcome, home)   # A1: the banner with the stated reset
        try:
            self.record_model_call(record, worker=worker, home=home, outcome="ok" if outcome is None else (
                "wait" if isinstance(outcome, provider_limits.ProviderUsageLimit) else "refused"))
        except Exception:
            pass
        return outcome

    def check_run(self, returncode, text, stderr="", allowed=None, answer_expected=True, now=None, command=None,
                  home=None):
        """A ProviderUsageLimit (RATE_LIMITED: the short wait; SIGNED_OUT: the sign-in wait), a
        ClaudeRunFailure (any failure the reader found, including any denial and any tool outside the list,
        even when an answer came; a non-zero exit; no structured answer), or None. Success is
        result.structured_output present, no denied_actions and no forbidden tool."""
        import provider_limits
        record = self.read_trace(text, allowed=self._allowed(allowed, command), strict=False)
        codes = dict(reversed(record.errors))
        if self.RATE_LIMITED in codes:
            import provider_pacing
            reset = self.reset_at(codes[self.RATE_LIMITED], now)   # A1: agy's stated reset, as Claude's resetsAt
            stated = provider_limits.stated_retry_seconds(codes[self.RATE_LIMITED], now)
            if reset is not None:
                seconds = (reset - provider_limits._utc(now)).total_seconds()
                stated = seconds if 0 < seconds <= provider_limits.MAX_WAIT.total_seconds() else stated
            delay = provider_pacing.rate_limited(self.pacing_provider, stated)
            return provider_limits.ProviderUsageLimit(codes[self.RATE_LIMITED], now, kind=provider_limits.RATE_LIMITED,
                                                      delay=delay, reset_at=reset)
        signed_out = returncode and not record.answer and any(
            word in (stderr or "").lower() for word in ("authentication failed", "accounts.google.com/o/oauth2"))
        if self.SIGNED_OUT in codes or signed_out:
            return provider_limits.ProviderUsageLimit(codes.get(self.SIGNED_OUT) or "ANTIGRAVITY_SIGNED_OUT", now,
                                                      kind=provider_limits.ANTIGRAVITY_SIGNED_OUT)
        errors = [error for error in record.errors
                  if not (record.answer is not None and error[0] in self.ANSWER_KEPT)]
        if errors:
            return ClaudeRunFailure(*errors[0])
        if returncode:
            return ClaudeRunFailure(self.EXITED, "exit %s: %s" % (returncode, (stderr or "")[-300:]))
        if answer_expected and record.answer is None:
            return ClaudeRunFailure(self.NO_ANSWER, "no structured_output")
        return None

    def limit_output(self, returncode, *texts, command=None):
        stream = next((text for text in texts if isinstance(text, str)), "")
        stderr = "\n".join(text for text in texts[1:] if isinstance(text, str))
        found = self.check_run(returncode, stream, stderr, answer_expected=False, command=command)
        import provider_limits
        return found if isinstance(found, provider_limits.ProviderUsageLimit) else None

    def limit(self, returncode, *paths, command=None):
        import provider_limits
        return self.limit_output(returncode, *[provider_limits._read(path) or "" for path in paths], command=command)

    # agy's quota error states its reset only in its words, as a Go duration ("Individual quota reached. ...
    # Resets in 166h8m9s."; its AGY_ERROR line has status/error_code/retryable but no reset field, 2026-10-28).
    RESETS_IN = re.compile(r"\bResets in (?=\d)(?:(?P<h>\d+)h)?(?:(?P<m>\d+)m(?!s))?(?:(?P<s>\d+(?:\.\d+)?)s)?(?![0-9A-Za-z])")

    def reset_at(self, message, now=None):
        """The reset agy's error words state ("Resets in 166h8m9s", from `now`) as an aware UTC datetime, else
        None (then the short wait, as before)."""
        import datetime as dt
        import provider_limits
        match = self.RESETS_IN.search(message) if isinstance(message, str) else None
        if not match or not any(match.group(unit) for unit in ("h", "m", "s")):
            return None
        seconds = int(match["h"] or 0) * 3600 + int(match["m"] or 0) * 60 + float(match["s"] or 0)
        return provider_limits._utc(now) + dt.timedelta(seconds=seconds) if seconds > 0 else None

    def usage(self, record):
        return dict(record.usage) if isinstance(record, RunRecord) else {}

    def record_model_call(self, record, worker=None, outcome=None, home=None):
        """One model_calls row: route "antigravity", provider "google", tokens; cost NULL (the user's own plan)."""
        import model_calls
        tokens = record.usage
        model_calls.record(self.ROUTE, outcome or ("ok" if record.error_kind is None and record.answer is not None
                                                   else "refused"),
                           worker=worker or model_calls.caller(), model=getattr(record, "model", None),
                           provider=self.pacing_provider,
                           prompt_tokens=(tokens.get("input_tokens", 0) + tokens.get("cache_read_tokens", 0)) if tokens else None,
                           completion_tokens=tokens.get("output_tokens"),
                           # A6: agy's total_tokens leaves out cache reads; with them, as Claude's total does
                           total_tokens=(tokens["total_tokens"] + tokens.get("cache_read_tokens", 0))
                           if "total_tokens" in tokens else None,
                           cost=None, seconds=getattr(record, "seconds", None))

    pre_model_rejection = staticmethod(lambda text, rejections: None)

    def error_events(self, pairs):
        reader = self.trace_reader()
        for offset, line in pairs:
            try:
                events = reader.feed(line)
            except ValueError:
                continue
            for event in events:
                if event.kind == "error":
                    yield offset, event.raw_kind, event.text

    def log_pairs(self, pairs, truncated):
        return []


def agy_hook(policy_path, payload):
    """The PreToolUse decision for one agy tool call ({"decision": "allow"} or "deny"), from the run's policy
    (AntigravityEngine.write_workspace): an MCP call of an allowed server tool (through call_mcp_tool or by the
    tool's direct name), an allowed built-in (search_web), finish, a read of an allowed server's tool descriptor
    in agy's own cache, or a read of a tool output agy saved for this run's conversation. Everything else is denied."""
    deny = {"decision": "deny", "reason": "Sidedoor runs allow only their own tools."}
    try:
        policy = json.loads(Path(policy_path).read_text(encoding="utf-8"))
        allowed = frozenset(policy.get("allowed") or [])
        call = payload.get("toolCall") if isinstance(payload, dict) else None
        name = call.get("name") if isinstance(call, dict) else None
        arguments = call.get("args") if isinstance(call, dict) and isinstance(call.get("args"), dict) else {}
    except (OSError, ValueError, AttributeError):
        return deny
    if name == "finish" or (name in allowed and "/" not in name):
        return {"decision": "allow"}
    if AntigravityEngine().direct_mcp(name, allowed) is not None:   # A4: an allowed tool by its direct name
        return {"decision": "allow"}
    if name == "call_mcp_tool":
        server, tool = arguments.get("ServerName"), arguments.get("ToolName")
        if isinstance(server, str) and ("%s/%s" % (server, tool) in allowed or "%s/*" % server in allowed):
            return {"decision": "allow"}
        return deny
    if name == "read_file":
        engine = AntigravityEngine().bound(allowed)
        engine.agy_home = lambda home=None: Path(policy.get("agy_home") or AntigravityEngine.agy_home())
        conversation = payload.get("conversationId")   # the run's own conversation (A2)
        reader = AntigravityTraceReader(engine, allowed)
        if reader._descriptor(arguments.get("Path")) or reader._saved_output(arguments.get("Path"), conversation):
            return {"decision": "allow"}
    return deny


def agy_hook_main(argv):
    """`sidedoor agy-hook --policy FILE`: read the hook payload on stdin, print the decision on stdout."""
    import sys
    policy = argv[argv.index("--policy") + 1] if "--policy" in argv and argv.index("--policy") + 1 < len(argv) else ""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        payload = {}
    sys.stdout.write(json.dumps(agy_hook(policy, payload)) + "\n")
    return 0


ANTIGRAVITY = AntigravityEngine()
ENGINES = {CODEX.name: CODEX, CLAUDE.name: CLAUDE, ANTIGRAVITY.name: ANTIGRAVITY}


def supported():
    """The engine names Setup and Settings offer (U1 3g): every engine registered in ENGINES, so a new
    engine (Antigravity, phase 4) shows up there once it is registered."""
    return frozenset(ENGINES)
ENGINE_FILE = "engine.json"


def engine(config=None):
    """The engine a session started with `config` runs on: Claude or Antigravity for config["engine"] == "claude" / "antigravity" (chosen
    in Setup or Settings, laid over the config by setup_steps.with_engine, U1 3g), else Codex. Every
    launcher passes its config."""
    if isinstance(config, dict) and config.get("engine") == CLAUDE.name:
        return CLAUDE
    if isinstance(config, dict) and config.get("engine") == ANTIGRAVITY.name:
        return ANTIGRAVITY   # U1 phase 4
    return CODEX


def for_command(command):
    """The engine that built `command` (an argv from one of its builders), for code that holds only the
    command (provider pacing, the research worker). Codex unless another engine claims it."""
    for candidate in ENGINES.values():
        if candidate is not CODEX and candidate.built(command):
            return candidate
    return CODEX


def _record_paths(path):
    path = Path(path)
    if path.is_dir():
        return [path / ENGINE_FILE]
    return [path.with_name(path.name + "." + ENGINE_FILE), path.parent / ENGINE_FILE]


def record_run(path, chosen, command=None):
    """Record that `chosen` (an Engine) wrote the run at `path`: engine.json inside a run folder, or
    <name>.engine.json beside a plain worker log that shares its folder with other runs. Claude also
    records the run's allowed tools from `command` (readers' kill switch); Codex's record is unchanged."""
    target = _record_paths(path)[0]
    # Atomic: a crash or a full disk never leaves a partial record (which would read as ENGINE_RECORD_INVALID).
    temporary = target.with_name(target.name + ".tmp-" + str(os.getpid()))
    try:
        extra = chosen.run_record(command) if command is not None and hasattr(chosen, "run_record") else {}
        temporary.write_text(json.dumps({"engine": chosen.name, **extra}) + "\n", encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def for_run(path):
    """The engine that wrote the run at `path` (a run folder, or a file of one: its own <name>.engine.json,
    else its folder's engine.json). No record: Codex, which wrote every run saved before phase 3a.
    Raises EngineRecordError for a record that is a link, unreadable, or names no known engine."""
    for candidate in _record_paths(path):
        if candidate.is_symlink():
            raise EngineRecordError("ENGINE_RECORD_INVALID")
        try:
            text = candidate.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except (OSError, UnicodeDecodeError):
            raise EngineRecordError("ENGINE_RECORD_INVALID") from None
        try:
            value = json.loads(text)
        except ValueError:
            raise EngineRecordError("ENGINE_RECORD_INVALID") from None
        name = value.get("engine") if isinstance(value, dict) else None
        found = ENGINES.get(name) if isinstance(name, str) else None
        if found is None:
            raise EngineRecordError("ENGINE_RECORD_INVALID")
        if found is CLAUDE and "allowed" in value:
            allowed = value["allowed"]
            if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
                raise EngineRecordError("ENGINE_RECORD_INVALID")
            return CLAUDE.bound(allowed)
        if found is ANTIGRAVITY and "allowed" in value:
            allowed, steps = value["allowed"], value.get("steps")
            if (not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed)
                    or not (steps is None or isinstance(steps, str))):
                raise EngineRecordError("ENGINE_RECORD_INVALID")
            return ANTIGRAVITY.bound(allowed, steps)
        return found
    return CODEX


if __name__ == "__main__":   # the agy PreToolUse hook, run from source (AntigravityEngine.hook_command)
    import sys
    if sys.argv[1:2] == ["agy-hook"]:
        sys.exit(agy_hook_main(sys.argv[2:]))
