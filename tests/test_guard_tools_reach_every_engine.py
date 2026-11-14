"""Every tool the guard lists to a session can be called in it on every engine (owner's coordinator,
2026-11-13, after E15.T7/T8's three tools were found listed by the guard and refused by everything else).

One table over the session kinds the product starts. For each kind and each engine (Codex, Claude Code,
Antigravity) the session's real command is built, the guard is made from the very flags that command
carries, and every tool that guard LISTS must be (a) accepted by the trace check a live session runs
under (`application_browser.tool_event`) and (b) in that engine's own allow list for the run. A tool added
to the guard later without this wiring fails here at once. A tool the guard does not list to a session is
in no engine's list for it either. No engine runs: this reads the lists, it proves nothing about a real run.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, the OS credential store off)
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import application_browser  # noqa: E402
import browser_guard  # noqa: E402
import browser_runtime  # noqa: E402
import engines  # noqa: E402
import saved_sign_ins  # noqa: E402
import access_step
import site_sessions  # noqa: E402

SITE = "jobs.ashbyhq.com"
STEP = {site_sessions.CONFIG_KEY: SITE, saved_sign_ins.CONFIG_KEY: SITE, saved_sign_ins.ACCESS_CONFIG_KEY: True}
# kind -> (operation, the worker config keys the product sets for it, the guard's own tools it must list)
SESSIONS = {
    "a read": ("inspect", {site_sessions.CONFIG_KEY: SITE}, set()),
    "a submit": ("submit", {site_sessions.CONFIG_KEY: SITE, "_application_guard_claim": "CLAIM"}, {"arm"}),
    "an access step with a saved sign-in": ("inspect", STEP, {"fill_saved_sign_in", "open_confirmation_link"}),
    "an access step that may create the account": (
        "inspect", dict(STEP, **{saved_sign_ins.CREATE_CONFIG_KEY: "candidate@example.invalid"}),
        {"fill_saved_sign_in", "open_confirmation_link", "type_new_account_password"}),
    "an access step with a sign-in provider": (
        "inspect", dict(STEP, **{saved_sign_ins.CREATE_CONFIG_KEY: "candidate@example.invalid", browser_guard.SSO_CONFIG_KEY: True}),
        {"fill_saved_sign_in", "open_confirmation_link", "type_new_account_password", "single_sign_on"}),
    # The site's own profile (2026-11-14): the session the product builds for it holds no password tool of
    # any kind and no link tool, even for a site with Sidedoor's own unconfirmed sign-up and a sign-in provider.
    "a profile step": ("inspect", {key: value for key, value in access_step.session(
        {}, SITE, access_step.PROFILE, "candidate@example.invalid", may_create=True, recorded=True, single_sign_on=True).items()
        if key != "_application_operation"}, set()),
    "a verify": ("verify", {}, set()),
}
GUARD_OWN = {browser_guard.ARM_TOOL, browser_guard.HUMAN_CHECK_TOOL, browser_guard.CONTINUE_TOOL} | set(browser_guard.OWN_TOOLS)


def item(tool):
    """One completed call of `tool` as Codex's trace carries it (manual_application_browser.event)."""
    return {"type": "mcp_tool_call", "id": "call-1", "server": browser_runtime.SERVER, "tool": tool, "arguments": {},
            "status": "completed", "result": {"content": [{"type": "text", "text": "ok"}], "isError": False}}


class EveryListedToolCanBeCalled(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="guard-tools-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        (self.folder / "schema.json").write_text('{"type": "object"}')
        self.claim = self.folder / "claim.json"
        self.claim.write_text("{}")
        program = self.folder / "engine"
        program.write_text("#!/bin/sh\n")
        program.chmod(0o755)
        self.program = str(program)

    def config(self, engine, operation, keys):
        keys = {key: (str(self.claim) if value == "CLAIM" else value) for key, value in keys.items()}
        return dict({"engine": engine, engine: self.program, "codex": self.program, "_browser_operation": operation,
                     "_application_operation": operation,
                     "browser_driver": {"isolated": True, "headless": True, "runtime_dir": str(self.folder / "runtime")}}, **keys)

    def guard(self, operation, argv):
        """The guard as `browser_guard.main` builds it from the session's own command line."""
        flags = argv[:argv.index("--")] if "--" in argv else argv
        value = lambda name: flags[flags.index(name) + 1] if name in flags else None
        with mock.patch.object(browser_guard.subprocess, "Popen"):
            return browser_guard.Proxy(operation, ["node", "cli.js"], stdin=io.StringIO(""), stdout=io.StringIO(),
                                       focus_keeper=False, arm_binding=object() if value("--claim") else None,
                                       piece_chars=int(value("--piece-chars")) if value("--piece-chars") else None,
                                       site_session=value("--site-session"), saved_sign_in=value("--saved-sign-in"),
                                       create_account=value("--create-account"), access_step="--access-step" in flags,
                                       single_sign_on="--single-sign-on" in flags)

    # --- each engine's real command for the session: (the guard's argv, the engine's allow list) -------
    def codex(self, config, operation):
        options = browser_runtime.codex_options(config, self.folder, operation, claim=config.get("_application_guard_claim"))
        prefix = "mcp_servers.%s." % browser_runtime.SERVER
        argv = json.loads(next(o for o in options if o.startswith(prefix + "args="))[len(prefix + "args="):])
        return argv, {o.split(".")[3] for o in options if o.endswith('approval_mode="approve"')}

    def claude(self, config, operation):
        command = application_browser.command(config, self.folder)
        allowed = command[command.index("--allowedTools") + 1].split(",")
        argv = json.loads((self.folder / "browser.mcp.json").read_text())["mcpServers"][browser_runtime.SERVER]["args"]
        return argv, {name[len("mcp__%s__" % browser_runtime.SERVER):] for name in allowed}

    def antigravity(self, config, operation):
        with mock.patch.object(engines.ANTIGRAVITY, "preflight"):
            command = application_browser.command(config, self.folder)
        argv, _, tools = command.build["servers"][browser_runtime.SERVER]
        # agy also refuses a call with no global allow rule (`allow_rules`, from `sidedoor_tools`).
        ruled = set(engines.ANTIGRAVITY.sidedoor_tools()[engines.ANTIGRAVITY.SERVER_NAMES["browser"]])
        return list(argv), set(tools) & ruled

    def test_every_tool_the_guard_lists_is_accepted_by_the_trace_check_and_allowed_by_the_engine(self):
        missing = []
        for kind, (operation, keys, own) in SESSIONS.items():
            for engine in ("codex", "claude", "antigravity"):
                argv, allowed = getattr(self, engine)(self.config(engine, operation, keys), operation)
                proxy = self.guard(operation, argv)
                listed = {tool["name"] for tool in proxy.own_tools()}
                offered = set(proxy.offered())
                self.assertEqual(listed - {browser_guard.CONTINUE_TOOL}, own, (kind, engine))   # the control: the kind is what it says
                self.assertTrue(listed <= offered, (kind, engine))
                for tool in sorted(offered):
                    try:
                        accepted = application_browser.tool_item(item(tool), operation == "submit", False, live=True) is True
                    except ValueError:
                        accepted = False
                    if tool != "browser_evaluate" and not accepted:           # in-page code is refused in every trace, as ever
                        missing.append((kind, engine, tool, "refused by the trace check"))
                    if tool not in allowed:
                        missing.append((kind, engine, tool, "not in the engine's allow list"))
                # And nothing of the guard's own that this session does not hold is allowed to it.
                extra = (allowed & GUARD_OWN) - offered
                if engine == "antigravity":
                    extra -= {browser_guard.CONTINUE_TOOL}
                self.assertEqual(extra, set(), (kind, engine))
        self.assertEqual(missing, [])

    def test_a_tool_the_session_does_not_hold_is_still_refused_by_the_guard(self):
        argv, _ = self.codex(self.config("codex", "inspect", SESSIONS["a read"][1]), "inspect")
        proxy = self.guard("inspect", argv)
        out = proxy.stdout
        for tool in ("fill_saved_sign_in", "type_new_account_password", "open_confirmation_link", "single_sign_on", "arm"):
            proxy.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": {}}})
            self.assertIn("Tool %s is not available in this session." % tool, out.getvalue().splitlines()[-1], tool)


if __name__ == "__main__":
    unittest.main()
