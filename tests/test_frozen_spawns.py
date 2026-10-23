"""E9.T1: every child Sidedoor starts of its own code runs in a frozen (Nuitka) build.

A frozen build has no python and no .py files: the Sidedoor binary takes a subcommand, never a
script path or `-c`. Under Nuitka `sys.executable` is NOT that binary (it names a `python` beside it
that does not exist); scheduler_loop.program() returns `__compiled__.process_exe` instead. These tests fake frozen mode (`sys.frozen`, as Nuitka's
`__compiled__` reads in scheduler_loop.frozen) and check each spawn site's command, that
orchestrator.main dispatches every subcommand those commands use, and that no product module gained
a `sys.executable` use this file does not know about (SITES).

Spawn sites (what each runs from source / frozen):
- scheduler_loop.job_command: python orchestrator.py job <name> / sidedoor job <name>
- browser_runtime.guard_command (codex's `browser` MCP server; arm and CAPTCHA tools are in it):
  python orchestrator.py browser-guard ... / sidedoor browser-guard ...; its child is the bundled node
- reply_tools.server_argv (codex's `reply_tools` MCP server): python reply_tools.py serve ... /
  sidedoor reply-tools serve ...
- login_item.command / windowed_program (start at login, the Windows supervisor's child):
  python orchestrator.py serve ... / sidedoor serve ...
- codex itself (agent_worker, reply_worker, application_browser, outreach_history_browser, research,
  followup, outreach agents) is config["codex"], an outside program in both modes.
"""

import profile_home  # noqa: F401
import ast
import json
import shutil
import sys
import tempfile
import tomllib
import unittest
import warnings
from pathlib import Path
from unittest import mock

import agent_worker
import browser_runtime
import login_item
import orchestrator
import reply_tools
import reply_worker
import scheduler_loop

ROOT = Path(__file__).resolve().parents[1]
# Every place product code (and tools/) names the running interpreter, as (file, function) -> count,
# with why it is frozen-safe (AP6). A new place, or one more use in a listed function, fails
# InventoryTests until it is made frozen-safe and listed here.
SITES = {
    ("scheduler_loop.py", "program"): 1,       # __compiled__.process_exe under Nuitka, checked to exist
    ("scheduler_loop.py", "job_command"): 1,   # source branch only; frozen: [program(), "job", name]
    ("browser_runtime.py", "sidedoor_command"): 1,  # source branch only; frozen: [program()]
    ("login_item.py", "_program"): 1,          # source branch only; frozen: [program()]
    ("login_item.py", "windowed_program"): 2,  # source branch only (pythonw.exe); frozen: program()
    ("reply_tools.py", "server_argv"): 1,      # source branch only; frozen: [program(), "reply-tools"]
    ("tools/build_macos.py", "nuitka_command"): 1,  # the build venv's python runs Nuitka (not shipped)
}

def frozen(value=True):
    return mock.patch.object(sys, "frozen", value, create=True)


def codex_server(command, name):
    options = [command[index + 1] for index, part in enumerate(command[:-1]) if part == "-c"]
    table = tomllib.loads("\n".join(o for o in options if o.startswith("mcp_servers." + name + ".")))
    return table["mcp_servers"][name]


class FrozenSpawnTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, True)

    def assertNoScript(self, argv):
        self.assertFalse([part for part in argv if str(part).endswith(".py") or part in ("-c", "-m")], argv)

    def test_scheduler_jobs(self):
        for name in scheduler_loop.JOBS:
            with frozen():
                self.assertEqual(scheduler_loop.job_command(name), [sys.executable, "job", name])
            with frozen(False):
                self.assertEqual(scheduler_loop.job_command(name), [sys.executable, str(scheduler_loop.ENTRY), "job", name])

    def test_browser_guard_server(self):
        with frozen():
            argv = browser_runtime.guard_command({}, self.directory, "inspect")
        guard = argv[:argv.index("--")]
        self.assertEqual(guard[:2], [sys.executable, "browser-guard"])
        self.assertNoScript(guard)
        self.assertEqual(argv[argv.index("--") + 1], str(browser_runtime.node_path({})))
        with frozen(), mock.patch.dict("os.environ", {browser_runtime.RUNTIME_VARIABLE: ""}):
            self.assertEqual(browser_runtime.runtime_dir({}), Path(sys.executable).resolve().parent / "browser-runtime")

    def test_reply_tools_server(self):
        with frozen():
            argv = reply_tools.server_argv(self.directory)
            config = {"codex": "/bin/codex", "model": "m", "_reply_tools": {}}
            with mock.patch.object(reply_worker.text_model, "command", side_effect=lambda config, command: command):
                command = reply_worker._command(config, self.directory / "schema.json", self.directory / "out.json")
        self.assertEqual(argv, [sys.executable, "reply-tools", "serve", "--directory", str(self.directory)])
        server = codex_server(command, reply_tools.SERVER)
        self.assertEqual([server["command"]] + server["args"], argv)
        self.assertEqual(command[0], "/bin/codex")
        with frozen(False):
            self.assertEqual(reply_tools.server_argv(self.directory)[:2],
                             [sys.executable, str(Path(reply_tools.__file__).resolve())])

    def test_login_entry_runs_the_binary(self):
        with frozen():
            command = login_item.command(home=self.directory)
            self.assertEqual(login_item.windowed_program(), sys.executable)
        self.assertEqual(command[:2], [sys.executable, "serve"])
        self.assertNoScript(command)

    def test_codex_workers_start_the_configured_codex(self):
        config = {"codex": "/bin/codex", "model": "m"}
        with frozen():
            self.assertEqual(agent_worker._command(config, self.directory)[0], "/bin/codex")
            self.assertEqual(orchestrator.research_command(self.directory, config, self.directory / "o.json",
                                                           "research-schema.json")[0], "/bin/codex")


class NuitkaProgramTests(unittest.TestCase):
    """Under Nuitka every self-spawn names `__compiled__.process_exe`, never sys.executable."""

    def test_every_self_spawn_uses_the_compiled_binary(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        binary = str(directory / "Sidedoor.app" / "Contents" / "MacOS" / "sidedoor")
        Path(binary).parent.mkdir(parents=True)
        Path(binary).write_bytes(b"")
        compiled = mock.Mock(process_exe=binary)
        with mock.patch.object(scheduler_loop, "__compiled__", compiled, create=True):
            self.assertTrue(scheduler_loop.frozen())
            self.assertEqual(scheduler_loop.program(), binary)
            self.assertEqual(scheduler_loop.job_command("applications"), [binary, "job", "applications"])
            self.assertEqual(browser_runtime.guard_command({}, directory, "inspect")[:2], [binary, "browser-guard"])
            self.assertEqual(reply_tools.server_argv(directory)[:2], [binary, "reply-tools"])
            self.assertEqual(login_item.command(home=directory)[:2], [binary, "serve"])
            self.assertEqual(login_item.windowed_program(), binary)
            with mock.patch.dict("os.environ", {browser_runtime.RUNTIME_VARIABLE: ""}):
                self.assertEqual(browser_runtime.runtime_dir({}), Path(binary).resolve().parent / "browser-runtime")
            with self.assertRaises(ValueError):   # AP8: no injected interpreter in a frozen build
                reply_tools.server_argv(directory, executable="/usr/bin/python3")
        with mock.patch.object(scheduler_loop, "__compiled__", mock.Mock(process_exe=str(directory / "gone")), create=True):
            with self.assertRaisesRegex(RuntimeError, "FROZEN_BINARY_NOT_FOUND"):
                scheduler_loop.job_command("run")


class AppLocationTests(unittest.TestCase):
    """AP4: start at login is never registered or refreshed from a temporary or Downloads copy."""

    def problem(self, where):
        return login_item.app_location_problem(program=where, platform="darwin", user_home="/Users/u", frozen=True)

    def test_locations(self):
        moved = login_item.MOVE_TO_APPLICATIONS
        for where in ("/private/var/folders/x/T/AppTranslocation/ABC/d/Sidedoor.app/Contents/MacOS/sidedoor",
                      "/var/folders/x/T/Sidedoor.app/Contents/MacOS/sidedoor",
                      "/Users/u/Downloads/Sidedoor.app/Contents/MacOS/sidedoor",
                      "/Volumes/Sidedoor/Sidedoor.app/Contents/MacOS/sidedoor"):
            self.assertEqual(self.problem(where), moved, where)
        for where in ("/Applications/Sidedoor.app/Contents/MacOS/sidedoor",
                      "/Users/u/Applications/Sidedoor.app/Contents/MacOS/sidedoor"):
            self.assertIsNone(self.problem(where), where)
        self.assertIsNone(login_item.app_location_problem(program="/tmp/x", platform="darwin", frozen=False))
        self.assertIsNone(login_item.app_location_problem(program="/tmp/x", platform="win32", frozen=True))

    def test_a_misplaced_copy_neither_registers_nor_repoints(self):
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home, True)
        plist = login_item.plist_path(home)
        plist.parent.mkdir(parents=True)
        plist.write_bytes(b"the good /Applications entry")
        runner = mock.Mock(side_effect=AssertionError("no launchctl"))
        with mock.patch.object(login_item, "app_location_problem", return_value=login_item.MOVE_TO_APPLICATIONS):
            result = login_item.enable(runner=runner, platform="darwin", user_home=home, home=home)
            self.assertFalse(login_item.refresh(runner=runner, platform="darwin", user_home=home, home=home))
        self.assertEqual((result["ok"], result["message"]), (False, login_item.MOVE_TO_APPLICATIONS))
        self.assertEqual(plist.read_bytes(), b"the good /Applications entry")


class FrozenDispatchTests(unittest.TestCase):
    """orchestrator.main accepts every subcommand a frozen spawn uses."""

    def test_reply_tools_subcommand(self):
        with mock.patch.object(reply_tools, "serve", return_value=0) as serve:
            self.assertEqual(orchestrator.main(["reply-tools", "serve", "--directory", "/tmp/x"]), 0)
        serve.assert_called_once_with(Path("/tmp/x"))

    def test_browser_guard_and_job_subcommands(self):
        import browser_guard
        with mock.patch.object(browser_guard, "main", return_value=0) as guard:
            self.assertEqual(orchestrator.main(["browser-guard", "--operation", "inspect"]), 0)
        guard.assert_called_once_with(["--operation", "inspect"])
        self.assertEqual(orchestrator.main(["job", "nope"]), 2)

    def test_double_click_runs_serve(self):
        import dashboard
        for argv in (["sidedoor"], ["sidedoor", "-psn_0_12345"]):
            with frozen(), mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(dashboard, "serve", return_value=0) as serve:
                self.assertEqual(orchestrator.main(), 0)
            serve.assert_called_once_with(open_browser=True, path="/", start=False, tray=True)

    def test_no_arguments_from_source_is_still_a_usage_error(self):
        with frozen(False), mock.patch.object(sys, "argv", ["orchestrator.py"]), self.assertRaises(SystemExit):
            orchestrator.main()


def interpreter_uses(source, name="<source>"):
    """[(function, what)] for every use of the running interpreter in `source`: `sys.executable` and
    `sys._base_executable` (also through `import sys as x`), `from sys import executable`,
    `getattr(sys, "executable")`, and a list or tuple spawn naming `__file__` or a "python..." string."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)   # an old regex literal elsewhere
        tree = ast.parse(source, name)
    aliases = {"sys"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases.update(alias.asname for alias in node.names if alias.name == "sys" and alias.asname)
    found = []

    def visit(node, function):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        what = None
        if isinstance(node, ast.Attribute) and node.attr in ("executable", "_base_executable") \
                and isinstance(node.value, ast.Name) and node.value.id in aliases:
            what = node.attr
        elif isinstance(node, ast.ImportFrom) and node.module == "sys" \
                and any(alias.name in ("executable", "_base_executable") for alias in node.names):
            what = "from sys import executable"
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr" \
                and len(node.args) >= 2 and isinstance(node.args[0], ast.Name) and node.args[0].id in aliases \
                and isinstance(node.args[1], ast.Constant) and "executable" in str(node.args[1].value):
            what = "getattr(sys, executable)"
        elif isinstance(node, (ast.List, ast.Tuple)) and any(
                isinstance(item, ast.Name) and item.id == "__file__"
                or isinstance(item, ast.Constant) and isinstance(item.value, str) and item.value.startswith("python")
                for item in node.elts):
            what = "spawn of python or __file__"
        if what:
            found.append((function, what))
        for child in ast.iter_child_nodes(node):
            if not (what == "getattr(sys, executable)" and child in node.args):
                visit(child, function)
    visit(tree, "<module>")
    return found


class InventoryTests(unittest.TestCase):
    def test_every_interpreter_use_is_known(self):
        counts = {}
        for path in sorted(ROOT.glob("*.py")) + sorted((ROOT / "tools").glob("*.py")):
            if path.stem.startswith("manual_"):
                continue
            name = str(path.relative_to(ROOT))
            for function, _ in interpreter_uses(path.read_text(encoding="utf-8"), name):
                counts[(name, function)] = counts.get((name, function), 0) + 1
        self.assertEqual(counts, SITES, "a new interpreter use: make it frozen-safe and list it in SITES")

    def test_controls_every_form_is_found(self):
        """Must-hit controls: each form the inventory claims to catch is caught."""
        cases = {
            "import sys\ndef a():\n    return [sys.executable, 'x']\n": [("a", "executable")],
            "import sys as s\ndef b():\n    return s.executable\n": [("b", "executable")],
            "from sys import executable\n": [("<module>", "from sys import executable")],
            "import sys\ndef c():\n    return getattr(sys, 'executable')\n": [("c", "getattr(sys, executable)")],
            "import sys\ndef d():\n    return sys._base_executable\n": [("d", "_base_executable")],
            "def e():\n    return ['python3', 'x.py']\n": [("e", "spawn of python or __file__")],
            "def f(run):\n    run((interpreter, __file__, 'serve'))\n": [("f", "spawn of python or __file__")],
            "import os\ndef g():\n    return os.executable\n": [],   # not sys
        }
        for source, expected in cases.items():
            self.assertEqual(interpreter_uses(source), expected, source)


if __name__ == "__main__":
    unittest.main()
