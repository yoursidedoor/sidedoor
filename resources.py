"""Sidedoor's bundled read-only files: prompts, schemas, defaults and dashboard assets (E2.T5).

Every module reads a bundled file through `path(name)`, `read_text(name)` or `read_json(name)`;
none builds the path from its own `__file__`, a code-folder ROOT or a caller's `root`. `base()`
finds the folder, checking for MARKERS (Sidedoor-only file names), in this order:

- `SIDEDOOR_RESOURCES`, when set; a folder without the markers raises RESOURCES_NOT_FOUND;
- next to this module: a source checkout, `pip install -e .` and a Nuitka standalone build
  (Nuitka keeps `__file__` working, and the build copies `DATA_FILES` / `DATA_DIRS` to the same
  relative places with `nuitka_arguments()`);
- `<data>/share/sidedoor` for every installed scheme's data path (sysconfig: venv, system, Debian's
  posix_local, `--user`), `site.USER_BASE/share/sidedoor` and `<sys.prefix>/share/sidedoor`, where
  pyproject.toml's `[tool.setuptools.data-files]` puts them for a plain `pip install .`.

None of them holding the markers raises RESOURCES_NOT_FOUND naming the places tried; there is no
silent fallback. Production always reads the bundled copy. Tests that need their own prompt or
config.json set `OVERRIDE` to a folder (`with resources.overridden(folder):`): a file present
there is read instead of the bundled one. Nothing in the product sets it.

Only declared names resolve: a prompt, schema or template added without being listed here raises
ValueError, and tests/test_packaging.py fails until it is listed (and so built into the bundle).
`python resources.py --nuitka` prints the build's --include-data-* arguments.
"""

import contextlib
import json
import os
import site
import sys
import sysconfig
from pathlib import Path

HERE = Path(__file__).resolve().parent
VARIABLE = "SIDEDOOR_RESOURCES"
INSTALLED = Path("share") / "sidedoor"   # pip's data-files folder, under sys.prefix

PROMPTS = (
    "agent-worker-prompt.md", "answering-judge-prompt.md", "answering-prompt.md",
    "desktop-worker-prompt.md", "discovery-prompt.md", "followup-prompt.md",
    "outreach-agent-prompt.md", "outreach-compose-prompt.md", "reply-prompt.md", "research-prompt.md",
)
SCHEMAS = (
    "agent-worker-schema.json", "answering-judge-schema.json", "answering-schema.json",
    "discovery-schema.json", "followup-schema.json", "outreach-agent-schema.json",
    "outreach-compose-schema.json", "reply-schema.json", "research-schema.json",
)
DEFAULTS = ("config.json", "settings.json", "profile.example.json")
# Every bundled single file, as a path relative to the resource folder.
DATA_FILES = PROMPTS + SCHEMAS + DEFAULTS
# Every bundled folder, copied whole: templates (guides included), static files, picklists, the
# brand images and the PDF font.
DATA_DIRS = ("dashboard_assets", "assets")
MARKERS = ("agent-worker-prompt.md", "config.json")   # a Sidedoor resource folder holds both
OVERRIDE = None   # tests only: a folder whose files shadow the bundled ones (see `overridden`)


def _holds_markers(folder):
    return all((folder / name).is_file() for name in MARKERS)


def candidates():
    """Every folder `base()` checks after SIDEDOOR_RESOURCES, in order, without duplicates."""
    found = [HERE]
    for scheme in sysconfig.get_scheme_names():
        try:
            data = sysconfig.get_path("data", scheme)
        except (KeyError, ValueError):
            continue
        if data:
            found.append(Path(data) / INSTALLED)
    if site.USER_BASE:
        found.append(Path(site.USER_BASE) / INSTALLED)
    found.append(Path(sys.prefix) / INSTALLED)
    unique = []
    for folder in found:
        if folder not in unique:
            unique.append(folder)
    return unique


def base():
    """The folder that holds the bundled files. Raises RuntimeError RESOURCES_NOT_FOUND."""
    configured = os.environ.get(VARIABLE)
    if configured:
        folder = Path(configured).expanduser()
        if _holds_markers(folder):
            return folder
        raise RuntimeError(f"RESOURCES_NOT_FOUND: {VARIABLE}={folder} does not hold {', '.join(MARKERS)}")
    tried = candidates()
    for folder in tried:
        if _holds_markers(folder):
            return folder
    raise RuntimeError("RESOURCES_NOT_FOUND: no folder holds " + ", ".join(MARKERS) + "; tried "
                       + ", ".join(str(folder) for folder in tried))


def declared(name):
    parts = Path(name).parts
    if not parts or Path(name).is_absolute() or ".." in parts:
        return False
    return name in DATA_FILES or parts[0] in DATA_DIRS


def path(name):
    """The bundled file or folder `name` (relative, '/'-separated). Raises ValueError for a name
    that is not declared."""
    name = str(name)
    if not declared(name):
        raise ValueError("RESOURCE_NOT_DECLARED: " + name)
    if OVERRIDE is not None and (Path(OVERRIDE) / name).exists():
        return Path(OVERRIDE) / name
    return base() / name


@contextlib.contextmanager
def overridden(folder):
    """Tests only: read the files `folder` holds instead of the bundled ones, for the block."""
    global OVERRIDE
    previous, OVERRIDE = OVERRIDE, folder
    try:
        yield
    finally:
        OVERRIDE = previous


def read_text(name, encoding="utf-8"):
    return path(name).read_text(encoding=encoding)


def read_json(name):
    return json.loads(read_text(name))


def nuitka_arguments(source=None):
    """The Nuitka --include-data-files / --include-data-dir arguments for every bundled file."""
    source = Path(source) if source is not None else HERE
    return ([f"--include-data-files={source / name}={name}" for name in DATA_FILES]
            + [f"--include-data-dir={source / name}={name}" for name in DATA_DIRS])


if __name__ == "__main__":
    if sys.argv[1:] == ["--nuitka"]:
        # Machine contract (E2.T6): the build's file lists, read by the packaging script; stays print.
        print("\n".join(nuitka_arguments()))
    else:
        # Machine contract (E2.T6): the build's file lists, read by the packaging script; stays print.
        print("\n".join(DATA_FILES + tuple(name + "/" for name in DATA_DIRS)))
