"""Build Sidedoor.app for macOS with Nuitka (E9.T1), signed and notarized with --sign/--notarize (E9.T2).

    <build venv>/bin/python tools/build_macos.py --output <folder outside the repo>
        [--runtime ~/sidedoor-browser-runtime] [--jobs N]
        [--sign "Developer ID Application: <name> (<team>)" [--notarize <notarytool keychain profile>]]

--sign and --notarize run tools/sign_macos.py last, after every copy into the bundle and the smoke run
(Nuitka's own ad-hoc signature is replaced); with --notarize the stapled app is also packed as
<output>/Sidedoor-mac-apple-silicon.zip (Sidedoor-mac-intel.zip on Intel) with its .sha256. A signed build keeps every data file and data
folder below in Contents/Resources with a symlink at the Contents/MacOS path named here (codesign takes
every file in Contents/MacOS as code); the program sees the same paths.

The build venv holds the project's runtime dependencies (`pip install -e .`) plus nuitka,
ordered-set and zstandard. The result is <output>/Sidedoor.app:

    Contents/MacOS/sidedoor            the `sidedoor` command (tools/sidedoor.py -> orchestrator.cli);
                                       a double-click runs `sidedoor serve` (orchestrator.main). A
                                       regular app: Dock icon plus the menu-bar tray (owner, APP1 b);
                                       the Dock's click and Quit are the tray's (tray.dock_events)
    Contents/MacOS/<data files>        prompts, schemas, defaults, dashboard_assets, assets
                                       (resources.nuitka_arguments; resources.base() finds them
                                       next to the compiled modules)
    Contents/MacOS/browser-runtime/    the pinned Node and @playwright/mcp (E7.T1 option A, bundled;
                                       browser_runtime.runtime_dir() looks beside the binary)
    Contents/MacOS/pystray/, fpdf/     the two LGPL-3.0 libraries' own .py sources, NOT compiled (owner
                                       TR1 a and AP1 a: kept separate and replaceable; imported from there
                                       at run time; no bytecode is written beside them)
    Contents/Resources/licenses/       their licence texts and NOTICE-<library>.txt, and
                                       THIRD-PARTY-NOTICES.txt (every bundled Python distribution's
                                       licence files, CPython's, the Nuitka runtime's, the dylibs')
    Contents/Resources/sidedoor.icns   from assets/brand/yoursidedoor-icon-512.png (sips + iconutil)

Every child Sidedoor starts of itself is the binary with a subcommand (`job`, `browser-guard`,
`reply-tools`, `serve`); tests/test_frozen_spawns.py covers each spawn site. Nothing is downloaded:
the browser runtime is copied from --runtime after browser_runtime.verify() accepts it.
"""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import importlib.metadata
import importlib.util
import ast
import re

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import browser_runtime  # noqa: E402
import resources  # noqa: E402
import version  # noqa: E402

APP = "Sidedoor"
BINARY = "sidedoor"
ENTRY = REPO / "tools" / "sidedoor.py"
ICON = REPO / "assets" / "brand" / "yoursidedoor-icon-512.png"
DEFAULT_RUNTIME = Path("~/sidedoor-browser-runtime").expanduser()
# What the app needs of the runtime folder: Node's binary and licence, and the npm packages.
# (Not node/lib/node_modules/npm, node/include or the downloaded tarball.)
# Node is already only its binary and licence (npm, headers and the tarball stay out). Stripping the
# binary would break the OpenJS signature it carries, so it is copied as it is.
RUNTIME_PARTS = ("node/bin/node", "node/LICENSE", "node_modules")
# The package files come from the repository (browser-runtime/, Node 24.22.1 everywhere; owner Q3), not
# from --runtime: the installed packages must be exactly those the repository's lock pins.
PACKAGE_FILES = ("package.json", "package-lock.json")
# Modules the product imports by string at run time (uvicorn's loop/protocol choices, anyio's backend).
STRING_IMPORTS = ("uvicorn.loops.auto", "uvicorn.loops.asyncio", "uvicorn.protocols.http.auto",
                  "uvicorn.protocols.http.h11_impl", "uvicorn.protocols.websockets.auto",
                  "uvicorn.lifespan.on", "uvicorn.lifespan.off", "anyio._backends._asyncio")
# Nuitka is pinned: the frozen-mode facts this build relies on (`__compiled__.process_exe`, the
# excluded-module-usage flag) were observed with this version.
NUITKA_VERSION = "4.3.1"
# The LGPL-3.0 libraries (owner TR1 a for pystray, AP1 a for fpdf2): never compiled, shipped as their
# own .py files beside the binary, replaceable by the user. Nuitka never sees their imports, so the
# modules they import are compiled in by name (`foreign_imports`, plus EXTRA_IMPORTS for imports a
# scan cannot resolve: six.moves is made by six at run time). Their dependencies (six, pyobjc,
# Pillow, fontTools, defusedxml) are permissive and compiled normally.
UNCOMPILED = {"pystray": "pystray", "fpdf": "fpdf2"}     # package -> distribution
EXTRA_IMPORTS = {"pystray": ("queue",)}
HOMEPAGES = {"pystray": "https://github.com/moses-palmer/pystray", "fpdf2": "https://github.com/py-pdf/fpdf2"}
NOTICE = """Sidedoor includes {dist} {version} ({homepage}), licensed under the GNU Lesser General Public
License version 3 ({files}; the LGPL builds on the GNU General Public License version 3, GPL-3.0.txt).

{dist} is not compiled into Sidedoor. Its unmodified Python sources are in
Sidedoor.app/Contents/MacOS/{package}/ and are loaded from there each time Sidedoor starts. You may
replace that folder with another version of {dist} (or a modified one) that offers the same interface;
Sidedoor then uses it. Its source is also available from https://pypi.org/project/{dist}/{version}/.
"""
LICENCE_NAME = re.compile(r"(LICEN[CS]E|COPYING|NOTICE)", re.I)
# A compiled-in distribution that ships no licence file in its dist-info, and the distribution of
# the same project whose file is that licence (PyObjC's core and framework wheels share one MIT text).
LICENCE_FROM = {"pyobjc-core": "pyobjc-framework-Cocoa"}
# Not shipped: the build's own tools and this project.
BUILD_ONLY = {"nuitka", "ordered-set", "zstandard", "pip", "setuptools", "sidedoor"}
DYLIBS = """Native libraries in Contents/MacOS (*.dylib)
=============================================

From Pillow's wheel (their full licence texts are in Pillow's licence above):
- libjpeg (libjpeg-turbo): IJG licence and BSD-3-Clause. This software is based in part on the work
  of the Independent JPEG Group.
- libtiff: libtiff licence (BSD-like).
- libwebp, libwebpdemux, libwebpmux, libsharpyuv: BSD-3-Clause.
- lcms2 (Little CMS): MIT.
- openjp2 (OpenJPEG): BSD-2-Clause.
- libavif: BSD-2-Clause.
- libxcb, libXau: MIT (X11).
- zlib-ng: zlib licence.

From the Python build (Homebrew python@3.13):
- OpenSSL 3 (libssl, libcrypto): Apache License 2.0.
- liblzma (XZ Utils): 0BSD.
- libmpdec (mpdecimal): BSD-2-Clause.
- sqlite (libsqlite3): public domain.
"""
# Their licence texts on the build machine, appended when present.
DYLIB_TEXTS = (("OpenSSL 3", "/opt/homebrew/opt/openssl@3/LICENSE.txt"),
               ("XZ Utils (liblzma)", "/opt/homebrew/opt/xz/COPYING.0BSD"),
               ("mpdecimal (libmpdec)", "/opt/homebrew/opt/mpdecimal/COPYRIGHT.txt"))
PERSONAL = ("/private/tmp/claude-501", "/Users/")

def product_modules():
    """Every top-level product module (pyproject's py-modules) except the manual_* developer tools,
    named explicitly so a module imported only inside a function is never left out."""
    return sorted(path.stem for path in REPO.glob("*.py") if not path.stem.startswith("manual_"))


def make_icns(png, folder):
    """A .icns with every size macOS asks for, made with the built-in sips and iconutil."""
    iconset = Path(folder) / "sidedoor.iconset"
    iconset.mkdir()
    for size in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            pixels = size * scale
            name = "icon_%dx%d%s.png" % (size, size, "@2x" if scale == 2 else "")
            subprocess.run(["sips", "-z", str(pixels), str(pixels), str(png), "--out", str(iconset / name)],
                           check=True, capture_output=True)
    icns = Path(folder) / "sidedoor.icns"
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)], check=True)
    return icns


def nuitka_command(output, icns, jobs=None):
    command = [sys.executable, "-m", "nuitka", "--standalone", "--macos-create-app-bundle",
               "--report=" + str(Path(output) / "compilation-report.xml"),
               "--macos-app-name=" + APP, "--macos-app-version=" + version.current(),
               "--macos-app-icon=" + str(icns), "--macos-app-mode=gui",
               "--output-dir=" + str(output), "--output-filename=" + BINARY,
               "--nofollow-import-to=tests", "--nofollow-import-to=spikes", "--nofollow-import-to=manual_*",
               # The LGPL libraries are not compiled and are imported from their folders at run time:
               # Nuitka otherwise refuses to import a module it was told not to follow.
               "--no-deployment-flag=excluded-module-usage",
               "--assume-yes-for-downloads", "--quiet"]
    if jobs:
        command.append("--jobs=%d" % jobs)
    command += ["--nofollow-import-to=" + package for package in UNCOMPILED]
    command += ["--include-module=" + name for name in product_modules() + list(STRING_IMPORTS)
                + sorted({name for package in UNCOMPILED for name in foreign_imports(package)})]
    command += resources.nuitka_arguments(REPO)
    return command + [str(ENTRY)]


def copy_runtime(runtime, target):
    """Copy the verified browser runtime into the app (symlinks kept: node_modules/.bin)."""
    problems = browser_runtime.verify({"browser_driver": {"runtime_dir": str(runtime)}})
    if problems:
        raise SystemExit("browser runtime at %s refused: %s" % (runtime, "; ".join(problems)))
    import json
    pinned = json.loads((REPO / browser_runtime.RUNTIME_FOLDER / "package-lock.json").read_text())["packages"]
    installed = json.loads((Path(runtime) / "node_modules" / ".package-lock.json").read_text())["packages"]
    expected = {name: entry for name, entry in pinned.items() if name}
    if {name: entry.get("integrity") for name, entry in installed.items()} != \
            {name: entry.get("integrity") for name, entry in expected.items()}:
        raise SystemExit("browser runtime at %s does not hold exactly the packages browser-runtime/package-lock.json "
                         "pins; run npm ci there" % runtime)
    for name in PACKAGE_FILES:
        Path(target).mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / browser_runtime.RUNTIME_FOLDER / name, Path(target) / name)
    for part in RUNTIME_PARTS:
        source, destination = Path(runtime) / part, Path(target) / part
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination, symlinks=True)
        else:
            shutil.copy2(source, destination)
    problems = browser_runtime.verify({"browser_driver": {"runtime_dir": str(target)}})
    if problems:
        raise SystemExit("bundled browser runtime refused: " + "; ".join(problems))


def _resolvable(name):
    """True when `name` is a real module here (a file, an extension or built in), not a virtual one."""
    try:
        spec = importlib.util.find_spec(name)
    except Exception:   # a parent that cannot import on this system (another platform's backend)
        return False
    return spec is not None and (spec.origin not in (None, "namespace") or bool(spec.submodule_search_locations))


def foreign_imports(package):
    """Every absolute module the uncompiled `package` imports that resolves here (another platform's
    backends do not), plus EXTRA_IMPORTS: what the frozen binary must contain for it."""
    folder = Path(importlib.util.find_spec(package).origin).parent
    names = set(EXTRA_IMPORTS.get(package, ()))
    for path in folder.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module)
                names.update(node.module + "." + alias.name for alias in node.names
                             if _resolvable(node.module + "." + alias.name))
    return sorted(name for name in names if name.split(".")[0] not in UNCOMPILED and _resolvable(name)
                  and importlib.util.find_spec(name).origin not in ("built-in", "frozen"))  # always there


def licence_files(dist):
    """(name, text) of each licence file in `dist`'s dist-info."""
    found = []
    for item in sorted(dist.files or (), key=str):
        if ".dist-info" in str(item) and LICENCE_NAME.search(item.name) and not item.name.endswith(".py"):
            found.append((str(item).split(".dist-info/", 1)[-1],
                          Path(dist.locate_file(item)).read_text(encoding="utf-8", errors="replace")))
    return found


def copy_uncompiled(app):
    """Each LGPL library's sources (no bytecode) into Contents/MacOS/<package>, its licence files and
    NOTICE-<dist>.txt into Contents/Resources/licenses, with the GPL-3.0 text the LGPL builds on."""
    licenses = Path(app) / "Contents" / "Resources" / "licenses"
    licenses.mkdir(parents=True, exist_ok=True)
    gpl = None
    for package, name in UNCOMPILED.items():
        distribution = importlib.metadata.distribution(name)
        source = Path(importlib.util.find_spec(package).origin).parent
        shutil.copytree(source, Path(app) / "Contents" / "MacOS" / package,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        written = []
        for file_name, text in licence_files(distribution):
            if "GENERAL PUBLIC LICENSE" in text and "LESSER" not in text[:400]:
                gpl = text   # pystray's COPYING is the GPL-3.0 text itself
                continue
            target = licenses / ("%s-%s" % (Path(file_name).name, name))
            target.write_text(text, encoding="utf-8")
            written.append(target.name)
        if not written:
            raise SystemExit("%s ships no licence file in its dist-info" % name)
        (licenses / ("NOTICE-%s.txt" % name)).write_text(NOTICE.format(
            dist=name, version=distribution.version, homepage=HOMEPAGES[name], package=package,
            files=", ".join(written)), encoding="utf-8")
    if gpl is None:
        raise SystemExit("no GPL-3.0 text found among the LGPL libraries' licence files")
    (licenses / "GPL-3.0.txt").write_text(gpl, encoding="utf-8")


def compiled_distributions(report):
    """The distributions whose modules Nuitka compiled in (its --report), by name."""
    import xml.etree.ElementTree as ElementTree
    owners = importlib.metadata.packages_distributions()
    names = set()
    for module in ElementTree.parse(report).getroot().iter("module"):
        top = (module.get("name") or "").split(".")[0]
        names.update(owners.get(top, ()))
    return sorted((name for name in names if name.lower() not in BUILD_ONLY), key=str.lower)


def third_party_notices(app, report):
    """Contents/Resources/licenses/THIRD-PARTY-NOTICES.txt. Fails when a compiled-in distribution has
    no licence file (and no LICENCE_FROM)."""
    parts = ["Third-party software in Sidedoor %s\n%s\n" % (version.current(), "=" * 40)]
    python_licence = Path(sys.base_prefix) / "lib" / ("python%d.%d" % sys.version_info[:2]) / "LICENSE.txt"
    parts.append("Python %s (CPython), Python Software Foundation License\n%s\n%s"
                 % (sys.version.split()[0], "-" * 40, python_licence.read_text(encoding="utf-8")))
    nuitka = importlib.metadata.distribution("nuitka")
    runtime = [text for name, text in licence_files(nuitka) if "RUNTIME" in name]
    parts.append("Nuitka %s runtime library (compiled into the binary): AGPL-3.0 with the Nuitka Runtime "
                 "Library Exception\n%s\n%s" % (nuitka.version, "-" * 40, "\n".join(runtime)))
    missing = []
    for name in compiled_distributions(report) + sorted(UNCOMPILED.values()):
        distribution = importlib.metadata.distribution(name)
        files = licence_files(distribution) or licence_files(importlib.metadata.distribution(LICENCE_FROM.get(name, name)))
        if not files:
            missing.append(name)
            continue
        how = "not compiled; replaceable, in Contents/MacOS/" if name in UNCOMPILED.values() else "compiled in"
        if name in UNCOMPILED.values():
            how += next(package for package, dist in UNCOMPILED.items() if dist == name)
        parts.append("%s %s (%s): %s\n%s\n%s" % (
            distribution.metadata["Name"], distribution.version, how,
            distribution.metadata.get("License-Expression") or distribution.metadata.get("License") or "see below",
            "-" * 40, "\n\n".join("[%s]\n%s" % (file_name, text) for file_name, text in files)))
    if missing:
        raise SystemExit("compiled-in distributions with no licence file: " + ", ".join(missing))
    parts.append(DYLIBS)
    for label, where in DYLIB_TEXTS:
        if Path(where).is_file():
            parts.append("%s\n%s\n%s" % (label, "-" * 40, Path(where).read_text(encoding="utf-8", errors="replace")))
    parts.append("Node.js and @playwright/mcp: see Contents/MacOS/browser-runtime (node/LICENSE and each "
                 "package's LICENSE under node_modules).\n")
    target = Path(app) / "Contents" / "Resources" / "licenses" / "THIRD-PARTY-NOTICES.txt"
    target.write_text("\n\n".join(parts), encoding="utf-8")
    return compiled_distributions(report)


def doctor_lines(binary, home):
    import json
    environment = dict(os.environ, SIDEDOOR_HOME=str(home))
    environment.pop(browser_runtime.RUNTIME_VARIABLE, None)
    report = subprocess.run([str(binary), "doctor", "--json"], capture_output=True, text=True,
                            env=environment, timeout=300).stdout
    return {check["name"]: check for check in json.loads(report)["checks"]}


def smoke_uncompiled(app):
    """The built app imports pystray and fpdf from their folders: doctor's Tray icon (loads pystray)
    and PDF writer (writes a PDF with fpdf2) lines are OK with the folders there, and report the
    library missing with the folders moved away. doctor starts no browser."""
    binary = Path(app) / "Contents" / "MacOS" / BINARY
    folders = [binary.parent / package for package in UNCOMPILED]
    with tempfile.TemporaryDirectory(prefix="sidedoor-smoke-") as home:
        present = doctor_lines(binary, home)
        for folder in folders:
            folder.rename(folder.with_name(folder.name + ".aside"))
        try:
            absent = doctor_lines(binary, home)
        finally:
            for folder in folders:
                folder.with_name(folder.name + ".aside").rename(folder)
    checks = {"Tray icon": "missing", "PDF writer": "can't write PDFs"}
    for name, gone in checks.items():
        if present[name]["status"] != "OK" or gone not in absent[name]["line"]:
            raise SystemExit("%s: not loaded from its folder (with it %r, without it %r)" % (name, present[name], absent[name]))
        print("%s: %s; folder moved away: %s" % (name, present[name]["line"], absent[name]["line"]))


def clean_and_check(app):
    """No bytecode anywhere in the bundle (removed, then asserted), and no build-machine path in the
    uncompiled libraries' folders (AP2)."""
    for cache in list(Path(app).rglob("__pycache__")):
        shutil.rmtree(cache)
    left = [str(path) for path in Path(app).rglob("*") if path.name == "__pycache__" or path.suffix == ".pyc"]
    if left:
        raise SystemExit("bytecode left in the bundle: " + ", ".join(left[:5]))
    for package in UNCOMPILED:
        for path in (Path(app) / "Contents" / "MacOS" / package).rglob("*"):
            if path.is_file():
                data = path.read_bytes()
                found = [text for text in PERSONAL if text.encode() in data]
                if found:
                    raise SystemExit("%s holds a build-machine path (%s)" % (path, ", ".join(found)))


def size(path):
    total = 0
    for folder, _, files in os.walk(path):
        for name in files:
            item = Path(folder) / name
            if not item.is_symlink():
                total += item.stat().st_size
    return total


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, required=True, help="build folder, outside the repository")
    parser.add_argument("--runtime", type=Path, default=Path(os.environ.get(browser_runtime.RUNTIME_VARIABLE)
                                                                or DEFAULT_RUNTIME).expanduser())
    parser.add_argument("--jobs", type=int)
    parser.add_argument("--sign", metavar="IDENTITY", help="Developer ID Application identity (tools/sign_macos.py)")
    parser.add_argument("--notarize", metavar="PROFILE", help="notarytool keychain profile; needs --sign")
    args = parser.parse_args(argv)
    if args.notarize and not args.sign:
        raise SystemExit("--notarize needs --sign")
    if sys.platform != "darwin":
        raise SystemExit("build_macos.py builds on macOS only")
    if importlib.metadata.version("nuitka") != NUITKA_VERSION:
        raise SystemExit("Nuitka %s is pinned; this venv has %s" % (NUITKA_VERSION, importlib.metadata.version("nuitka")))
    output = args.output.expanduser().resolve()
    if output == REPO or REPO in output.parents:
        raise SystemExit("--output must be outside the repository: " + str(output))
    output.mkdir(parents=True, exist_ok=True)
    app = output / (APP + ".app")
    built = output / (ENTRY.stem + ".app")   # Nuitka names the bundle after the entry script
    for old in (app, built, output / (ENTRY.stem + ".build"), output / (ENTRY.stem + ".dist")):
        if old.exists():
            shutil.rmtree(old)
    with tempfile.TemporaryDirectory(prefix="sidedoor-icon-") as scratch:
        icns = make_icns(ICON, scratch)
        subprocess.run(nuitka_command(output, icns, args.jobs), check=True, cwd=REPO)
    built.rename(app)
    copy_runtime(args.runtime, app / "Contents" / "MacOS" / browser_runtime.RUNTIME_FOLDER)
    copy_uncompiled(app)
    smoke_uncompiled(app)
    clean_and_check(app)
    compiled = third_party_notices(app, output / "compilation-report.xml")
    print("compiled-in distributions: " + ", ".join(compiled))
    print("built %s (version %s, %.1f MB)" % (app, version.current(), size(app) / 1e6))
    if args.sign:
        # Last: nothing may be written into the bundle after it is sealed.
        sys.path.insert(0, str(REPO / "tools"))
        import sign_macos
        sign_macos.finish(app, args.sign, args.notarize, output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
