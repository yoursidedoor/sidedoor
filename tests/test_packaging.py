"""pyproject.toml: the `sidedoor` command maps to the CLI and every top-level module is packaged.

E2.T5: every bundled read-only file is declared in resources.py, exists, is in pyproject's
data-files and the Nuitka arguments, and no product module reads one except through resources.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import ast
import fnmatch
import os
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

import orchestrator
import resources


ROOT = Path(__file__).resolve().parents[1]


class PackagingTest(unittest.TestCase):
    def setUp(self):
        self.project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    def test_sidedoor_command_is_the_orchestrator_cli(self):
        module, function = self.project["project"]["scripts"]["sidedoor"].split(":")
        self.assertEqual(module, "orchestrator")
        self.assertTrue(callable(getattr(orchestrator, function)))

    def test_every_top_level_module_is_listed(self):
        listed = self.project["tool"]["setuptools"]["py-modules"]
        self.assertEqual(sorted(listed), sorted(path.stem for path in ROOT.glob("*.py")))



# Top-level tracked files and folders that are not bundled data, each with why (E2.T5 review R1).
# Anything else at the top level must be declared in resources.DATA_FILES / DATA_DIRS.
NOT_DATA_FILES = {
    ".gitignore": "VCS metadata",
    "AGENTS.md": "project instructions",
    "README.md": "docs",
    "VALIDATION.md": "docs",
    "pyproject.toml": "build metadata",
    "browser-requirements.txt": "the spikes' pip requirements, not read by the product",
}
NOT_DATA_PATTERNS = ("README*", "LICENSE*", "CHANGELOG*", "CONTRIBUTING*", ".git*")
NOT_DATA_DIRS = {
    "tests": "tests", "docs": "docs", "tools": "developer tools",
    "spikes": "prototypes, not packaged", "relay": "the relay server, deployed on its own",
    "browser-runtime": "the Node runtime, installed by `npm ci` beside the build (E7.T1)",
    ".github": "CI metadata",
}
# Product modules allowed to name a bundled file in a string literal outside a resources.* call,
# each with why (R2). The manual_*.py developer tools are exempt as a group.
LITERAL_EXEMPTIONS = {
    ("answering_agent.py", "answering-prompt.md"): "PROMPT_FILE, read with resources.read_text",
    ("answering_agent.py", "answering-schema.json"): "SCHEMA_FILE, read with resources.read_text",
    ("answering_agent.py", "answering-judge-prompt.md"): "JUDGE_PROMPT_FILE, read with resources.read_text",
    ("answering_agent.py", "answering-judge-schema.json"): "JUDGE_SCHEMA_FILE, read with resources.read_text",
    ("orchestrator.py", "research-schema.json"): "passed to research_command, which uses resources.path",
    ("parallel_research.py", "discovery-schema.json"): "passed to research_command, which uses resources.path",
    ("approvals_page.py", "settings.json"): "the owner's override file in the home folder, not the bundle",
    ("user_profile.py", "settings.json"): "the owner's override file in the home folder, not the bundle",
}


def bundled_literal(value):
    return (value in resources.DATA_FILES or value in resources.DATA_DIRS
            or value.startswith(tuple(name + "/" for name in resources.DATA_DIRS)))


def literal_reads(name, text):
    """(line, literal) for every string naming a bundled file outside a resources.* call's
    arguments, docstrings aside, less LITERAL_EXEMPTIONS."""
    tree = ast.parse(text)
    allowed = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "resources":
            allowed |= {id(argument) for argument in node.args}
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body \
                and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant):
            allowed.add(id(node.body[0].value))
    return [(node.lineno, node.value) for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and bundled_literal(node.value)
            and id(node) not in allowed and (name, node.value) not in LITERAL_EXEMPTIONS]


def bundled_files():
    files = {Path(name) for name in resources.DATA_FILES}
    for folder in resources.DATA_DIRS:
        files |= {path.relative_to(ROOT) for path in (ROOT / folder).rglob("*") if path.is_file()}
    return files


def product_modules():
    return [path for path in sorted(ROOT.glob("*.py")) if not path.name.startswith("manual_")
            and path.name != "resources.py"]


def tracked():
    try:
        listed = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        raise unittest.SkipTest("not a git checkout")
    return listed.splitlines()


def undeclared_top_level(paths):
    files = {path for path in paths if "/" not in path}
    folders = {path.split("/", 1)[0] for path in paths if "/" in path}
    bad = sorted(name for name in files if not name.endswith(".py") and name not in resources.DATA_FILES
                 and name not in NOT_DATA_FILES and not any(fnmatch.fnmatch(name, pattern) for pattern in NOT_DATA_PATTERNS))
    return bad + sorted(name + "/" for name in folders if name not in resources.DATA_DIRS and name not in NOT_DATA_DIRS)


def resource_folder(folder, markers=resources.MARKERS):
    folder.mkdir(parents=True, exist_ok=True)
    for name in markers:
        (folder / name).write_text("{}")
    return folder


class ResourcesTest(unittest.TestCase):
    def test_every_declared_file_and_folder_exists(self):
        for name in resources.DATA_FILES:
            self.assertTrue((ROOT / name).is_file(), name)
            self.assertEqual(resources.path(name), ROOT / name)
        for name in resources.DATA_DIRS:
            self.assertTrue((ROOT / name).is_dir(), name)

    def test_every_prompt_and_schema_is_declared(self):
        self.assertEqual(sorted(resources.PROMPTS), sorted(path.name for path in ROOT.glob("*-prompt.md")))
        self.assertEqual(sorted(resources.SCHEMAS), sorted(path.name for path in ROOT.glob("*-schema.json")))
        self.assertGreater(len(resources.PROMPTS), 5)   # must-hit control: the globs found the files

    def test_every_top_level_file_and_folder_is_code_metadata_or_declared(self):
        # Must-hit controls: a new data file or folder at the top level is caught; code is not.
        self.assertEqual(undeclared_top_level(["triage-instructions.md", "rubric.yaml", "prompts/a.md", "x.py",
                                               "README.rst", "tests/t.py", "config.json"]),
                         ["rubric.yaml", "triage-instructions.md", "prompts/"])
        paths = tracked()
        self.assertGreater(len([path for path in paths if "/" not in path]), 50)
        self.assertEqual(undeclared_top_level(paths), [])

    def test_undeclared_names_are_refused(self):
        for name in ("new-prompt.md", "../config.json", "/etc/passwd", "state/orchestrator.sqlite3", ""):
            with self.assertRaises(ValueError):
                resources.path(name)
        self.assertEqual(resources.path("dashboard_assets/templates/setup.html"),
                         ROOT / "dashboard_assets/templates/setup.html")

    def test_pyproject_data_files_install_every_bundled_file(self):
        table = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["setuptools"]["data-files"]
        installed = set()
        for target, patterns in table.items():
            prefix = Path(target).relative_to("share/sidedoor") if target != "share/sidedoor" else Path()
            for pattern in patterns:
                for path in ROOT.glob(pattern):
                    self.assertEqual(path.parent.relative_to(ROOT), prefix, pattern)
                    installed.add(path.relative_to(ROOT))
        expected = bundled_files()
        self.assertGreater(len(expected), 40)
        self.assertEqual(installed, expected)

    def test_nuitka_arguments_name_every_bundled_file(self):
        arguments = resources.nuitka_arguments(ROOT)
        self.assertEqual(len(arguments), len(resources.DATA_FILES) + len(resources.DATA_DIRS))
        for name in resources.DATA_FILES:
            self.assertIn(f"--include-data-files={ROOT / name}={name}", arguments)
        for name in resources.DATA_DIRS:
            self.assertIn(f"--include-data-dir={ROOT / name}={name}", arguments)

    def test_the_marker_is_sidedoor_only(self):
        self.assertIn("agent-worker-prompt.md", resources.MARKERS)
        with tempfile.TemporaryDirectory() as temporary:
            only_config = resource_folder(Path(temporary) / "c", ("config.json",))
            self.assertFalse(resources._holds_markers(only_config))
            self.assertTrue(resources._holds_markers(resource_folder(Path(temporary) / "both")))

    def test_installed_layouts_and_no_silent_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            nowhere = temporary / "code"
            nowhere.mkdir()
            user = temporary / "user"
            with mock.patch.object(resources, "HERE", nowhere), \
                    mock.patch.object(resources.sysconfig, "get_scheme_names", return_value=("posix_local",)), \
                    mock.patch.object(resources.sysconfig, "get_path", return_value=str(temporary / "local")), \
                    mock.patch.object(resources.site, "USER_BASE", str(user)), \
                    mock.patch.object(resources.sys, "prefix", str(temporary / "prefix")), \
                    mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(resources.VARIABLE, None)
                with self.assertRaisesRegex(RuntimeError, "RESOURCES_NOT_FOUND.*tried .*code.*local.*user.*prefix"):
                    resources.base()
                resource_folder(user / "share" / "sidedoor")   # pip install --user
                self.assertEqual(resources.base(), user / "share" / "sidedoor")
                resource_folder(temporary / "local" / "share" / "sidedoor")   # Debian's posix_local
                self.assertEqual(resources.base(), temporary / "local" / "share" / "sidedoor")

    def test_sidedoor_resources_needs_the_markers(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict(os.environ, {resources.VARIABLE: temporary}):
                with self.assertRaisesRegex(RuntimeError, "RESOURCES_NOT_FOUND"):
                    resources.path("config.json")
                resource_folder(Path(temporary))
                self.assertEqual(resources.path("config.json"), Path(temporary) / "config.json")

    def test_production_never_reads_a_copy_in_a_caller_folder(self):
        # A stray copy beside the data no longer shadows the bundle; only the tests' OVERRIDE does.
        self.assertIsNone(resources.OVERRIDE)
        with tempfile.TemporaryDirectory() as temporary:
            (Path(temporary) / "reply-prompt.md").write_text("stray")
            self.assertEqual(resources.path("reply-prompt.md"), ROOT / "reply-prompt.md")
            with resources.overridden(temporary):
                self.assertEqual(resources.read_text("reply-prompt.md"), "stray")
                self.assertEqual(resources.path("reply-schema.json"), ROOT / "reply-schema.json")
            self.assertIsNone(resources.OVERRIDE)
        for module in product_modules():
            text = module.read_text(encoding="utf-8")
            self.assertFalse("resources.OVERRIDE" in text or "resources.overridden(" in text, module.name)

    def test_no_product_module_names_a_bundled_file_outside_the_loader(self):
        # Must-hit controls: every way of building the path is flagged; loader calls and docs are not.
        for old in ('t = (root / "reply-prompt.md").read_text()',
                    'ASSETS = ROOT / "dashboard_assets"',
                    'FONT = Path(__file__).resolve().parent / "assets/fonts/DejaVuSans.ttf"',
                    'p = Path(__file__).with_name("config.json")',
                    'p = HERE.joinpath("reply-schema.json")',
                    'p = os.path.join(os.path.dirname(__file__), "research-prompt.md")',
                    'name = "outreach-agent-prompt.md"\np = folder / name'):
            self.assertTrue(literal_reads("new.py", old), old)
        self.assertFalse(literal_reads("new.py", '"""Reads reply-prompt.md."""\nt = resources.read_text("reply-prompt.md")'))
        modules = product_modules()
        self.assertGreater(len(modules), 50)
        offenders = {path.name: literal_reads(path.name, path.read_text(encoding="utf-8")) for path in modules}
        self.assertEqual({name: hits for name, hits in offenders.items() if hits}, {})
        users = [path.name for path in modules if "resources." in path.read_text(encoding="utf-8")]
        self.assertGreaterEqual(len(users), 18)
        # Every exemption is still needed (a stale one would hide a later read).
        for (name, literal) in LITERAL_EXEMPTIONS:
            self.assertIn(repr(literal).strip("'"), (ROOT / name).read_text(encoding="utf-8"), (name, literal))

if __name__ == "__main__":
    unittest.main()
