"""Build Sidedoor.app for macOS with Nuitka (E9.T1), signed and notarized with --sign/--notarize (E9.T2).

    <build venv>/bin/python tools/build_macos.py --output <folder outside the repo>
        [--arch arm64|x86_64] [--runtime ~/sidedoor-browser-runtime] [--node <Node folder>] [--jobs N]
        [--sign "Developer ID Application: <name> (<team>)" [--notarize <notarytool keychain profile>]]

Two downloads (owner, IN1 a): Apple silicon (--arch arm64) and Intel (--arch x86_64). --arch defaults to
the build interpreter's own architecture. Nuitka runs under `arch -<arch>` with --macos-target-arch, so
an Intel build is made on an Apple-silicon Mac under Rosetta from an x86_64 (or universal2) build venv,
e.g. the python.org universal2 framework's `arch -x86_64 python3.13 -m venv`. Nuitka thins universal
libraries (python.org's, PyObjC's universal2 wheels) to the target; afterwards every Mach-O in the
bundle must be that architecture only (`check_arch`), or the build fails. The bundled Node must match
too: --node names a Node distribution folder (bin/node and LICENSE, e.g. node-v24.22.1-darwin-x64)
that replaces --runtime's node; @playwright/mcp's node_modules are pure JavaScript (no native addon;
`check_arch` would refuse one of the wrong architecture) and are shared by both builds.

--sign and --notarize run tools/sign_macos.py last, after every copy into the bundle and the smoke run
(Nuitka's own ad-hoc signature is replaced); with --notarize the stapled app is also packed as
<output>/Sidedoor-mac-apple-silicon.zip or <output>/Sidedoor-mac-intel.zip with its .sha256. The names
carry no version (owner: the website links GitHub's "latest release" asset URLs); the version is in the
release's title and tag and inside the app. A signed build keeps every data file and data
folder below in Contents/Resources with a symlink at the Contents/MacOS path named here (codesign takes
every file in Contents/MacOS as code); the program sees the same paths.

Both builds use one Python: python.org's universal2 Python 3.13.17 framework (E3, E9; installer URL and
SHA-256 in tools/build-requirements-macos.txt), extracted and made to run in place with
tools/relocate_framework.py. Any other Python is refused (`refuse_non_python_org`): Homebrew's is built
for the build Mac's own macOS (minos 15.0), so every Mach-O Nuitka makes from it would be too, and it
puts Homebrew paths into the bundle. The arm64 venv is `arch -arm64 <framework>/bin/python3.13 -m venv`,
the Intel one `arch -x86_64 ... -m venv`, from the same framework, each with
`pip install -r tools/build-requirements-macos.txt`. Nuitka compiles for the minimum macOS it reads from
the interpreter binary (python.org's: 11.0 on arm64, 10.13 on x86_64; it overrides any
MACOSX_DEPLOYMENT_TARGET), so the interpreter is the control. The app declares LSMinimumSystemVersion
MINIMUM_MACOS (`--macos-app-macos-min-version`), and the build fails when any Mach-O in the bundle needs
a newer macOS than that (`check_release_tls.above_minimum`).

The build venv holds exactly tools/build-requirements-macos.txt (the project's runtime dependencies
plus nuitka, ordered-set and zstandard). The result is <output>/Sidedoor.app:

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
    Contents/Resources/sidedoor.icns   from assets/brand/yoursidedoor-icon-1024.png (sips + iconutil),
                                       16 and 32 px from the simplified yoursidedoor-icon-16/32.png

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
import platform
import re
import sysconfig

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import browser_runtime  # noqa: E402
import portable  # noqa: E402
import resources  # noqa: E402
import version  # noqa: E402

APP = "Sidedoor"
BINARY = "sidedoor"
ENTRY = REPO / "tools" / "sidedoor.py"
BRAND = REPO / "assets" / "brand"
ICON = BRAND / "yoursidedoor-icon-1024.png"   # the macOS master (tools/make_brand.py)
# Simplified art drawn for 16 and 32 pixels (bigger sign, thicker arrow): used for those exact sizes.
SMALL_ICONS = {16: BRAND / "yoursidedoor-icon-16.png", 32: BRAND / "yoursidedoor-icon-32.png"}
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
# HTTPS trust (hotfix 2026-10-28, tls_setup.py): truststore (the OS trust store) and certifi with its
# cacert.pem (the fallback), compiled in whole on both builds; tools/check_release_tls.py gates the result.
TLS_ARGUMENTS = ("--include-package=truststore", "--include-package=certifi")   # certifi's cacert.pem comes with it (Nuitka's package config)
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

From the Python build ({python_build}):
{python_dylibs}"""
# What python.org's framework (both builds) ships as dylibs: OpenSSL and ncurses as shared libraries,
# sqlite, mpdecimal and xz linked into its modules (Pillow's wheel brings liblzma).
PYTHON_DYLIBS = """- OpenSSL 3 (libssl, libcrypto): Apache License 2.0.
- ncurses (libncurses): MIT (X11 style).
- linked into Python's own modules: sqlite (public domain), mpdecimal (BSD-2-Clause), XZ Utils (0BSD).
- liblzma (XZ Utils, from Pillow's wheel): 0BSD.
"""
# Their licence texts on the build machine, appended when present.
DYLIB_TEXTS = (("OpenSSL 3", "/opt/homebrew/opt/openssl@3/LICENSE.txt"),
               ("XZ Utils (liblzma)", "/opt/homebrew/opt/xz/COPYING.0BSD"),
               ("mpdecimal (libmpdec)", "/opt/homebrew/opt/mpdecimal/COPYRIGHT.txt"),
               ("ncurses (libncurses; python.org build)", "/opt/homebrew/opt/ncurses/COPYING"))
PERSONAL = ("/private/tmp/claude-501", "/Users/")
# Mach-O CPU types (mach/machine.h) for the two macOS targets.
CPU_TYPES = {0x01000007: "x86_64", 0x0100000C: "arm64"}
ARCHES = ("arm64", "x86_64")
# The oldest macOS the app runs on (E4), Info.plist's LSMinimumSystemVersion: the highest minimum of the
# bundled pieces, read from their LC_BUILD_VERSION / LC_VERSION_MIN_MACOSX (otool -l), 2026-10-29:
#   Node v24.22.1 (darwin-arm64 and darwin-x64)         13.5   <- the highest
#   python.org Python 3.13 framework, its modules        11.0 arm64, 10.13 x86_64
#   wheels (Pillow, PyObjC, pydantic-core, ...)          11.0 arm64
#   Nuitka's compiled binary                             the interpreter's (11.0 arm64, 10.13 x86_64)
#   @playwright/mcp                                      JavaScript only, no native addon
MINIMUM_MACOS = "13.5"
# python.org's framework build, as its sysconfig records it (an extracted, relocated copy still does).
PYTHON_ORG_FRAMEWORK = "/Library/Frameworks/Python.framework/"

def product_modules():
    """Every top-level product module (pyproject's py-modules) except the manual_* developer tools,
    named explicitly so a module imported only inside a function is never left out."""
    return sorted(path.stem for path in REPO.glob("*.py") if not path.stem.startswith("manual_"))


def make_icns(png, folder, small=None):
    """A .icns with every size macOS asks for (16 to 512@2x = 1024), made with the built-in sips and
    iconutil from the 1024 master `png`: 1024 itself is copied, the others reduced, never enlarged;
    16 and 32 pixels come from the simplified art in `small` ({pixels: png})."""
    small = SMALL_ICONS if small is None else small
    iconset = Path(folder) / "sidedoor.iconset"
    iconset.mkdir()
    for size in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            pixels = size * scale
            name = "icon_%dx%d%s.png" % (size, size, "@2x" if scale == 2 else "")
            source = Path(small.get(pixels, png))
            if source == Path(png) and pixels == 1024:
                shutil.copyfile(source, iconset / name)
            else:
                subprocess.run(["sips", "-z", str(pixels), str(pixels), str(source), "--out", str(iconset / name)],
                               check=True, capture_output=True)
    icns = Path(folder) / "sidedoor.icns"
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)], check=True)
    return icns


def host_arch():
    """The build interpreter's own architecture (x86_64 under Rosetta)."""
    return {"aarch64": "arm64"}.get(platform.machine(), platform.machine())


def macho_arches(path):
    """The architectures of a Mach-O file read from its header ([] when it is not one): one for a thin
    file, each slice's for a universal one. Unknown CPU types are named by number."""
    import struct
    with open(path, "rb") as handle:
        head = handle.read(4096)
    if len(head) < 8:
        return []
    magic = head[:4]
    if magic in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe"):        # thin, little-endian
        cpu = struct.unpack("<I", head[4:8])[0]
        return [CPU_TYPES.get(cpu, hex(cpu))]
    if magic in (b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce"):        # thin, big-endian
        cpu = struct.unpack(">I", head[4:8])[0]
        return [CPU_TYPES.get(cpu, hex(cpu))]
    if magic in (b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"):        # universal (fat / fat64)
        count = struct.unpack(">I", head[4:8])[0]
        if not 0 < count < 32:         # a Java class file shares this magic
            return []
        size = 20 if magic.endswith(b"\xbe") else 32
        return [CPU_TYPES.get(cpu, hex(cpu)) for cpu in
                (struct.unpack(">I", head[8 + i * size:12 + i * size])[0] for i in range(count))]
    return []


def check_arch(app, arch):
    """Every Mach-O file in the bundle is `arch` only (thin, no other slice). Returns their count; raises
    SystemExit naming each file that is not."""
    found, wrong = 0, []
    for folder, _, files in os.walk(app):
        for name in files:
            path = Path(folder) / name
            if path.is_symlink():
                continue
            arches = macho_arches(path)
            if not arches:
                continue
            found += 1
            if arches != [arch]:
                wrong.append("%s (%s)" % (path.relative_to(app), " ".join(arches)))
    if wrong:
        raise SystemExit("not %s only: %s" % (arch, "; ".join(wrong)))
    return found


def refuse_non_python_org():
    """SystemExit unless the build interpreter is python.org's framework build: sysconfig's PYTHONFRAMEWORK
    is "Python" and its configured prefix is under /Library/Frameworks/Python.framework. Homebrew, pyenv,
    MacPorts, conda and uv Pythons are refused (E3: Homebrew's binaries need the build Mac's macOS)."""
    framework = sysconfig.get_config_var("PYTHONFRAMEWORK")
    prefix = str(sysconfig.get_config_var("prefix") or "")
    if framework != "Python" or not (prefix + "/").startswith(PYTHON_ORG_FRAMEWORK):
        raise SystemExit("the build Python is not python.org's framework build (prefix %s, framework %r; this "
                         "is %s): build from a venv on python.org's universal2 Python 3.13.17 "
                         "(tools/build-requirements-macos.txt, AGENTS.md \"Mac builds\")"
                         % (prefix or "unknown", framework, sys.base_prefix))


def nuitka_command(output, icns, jobs=None, arch=None):
    arch = arch or host_arch()
    python = sys.executable
    if arch not in macho_arches(Path(python).resolve()):
        raise SystemExit("the build interpreter %s cannot run %s (it is %s); use an %s or universal2 build venv"
                         % (python, arch, " ".join(macho_arches(Path(python).resolve())) or "unknown", arch))
    # `arch -<arch>` runs the (universal2) interpreter and every compiler Nuitka starts as <arch>.
    # Nuitka compiles in the UTF-8 mode of the Python that runs it (Nuitka 4.3.1 MainProgram.c:
    # Py_UTF8Mode = SYSFLAG_UTF8, from sys.flags.utf8_mode at build time). On Windows the build runs
    # it with -X utf8 (portable.UTF8_FLAGS, W1 a; empty elsewhere); a Windows build script must keep this.
    command = ["arch", "-" + arch, python, *portable.UTF8_FLAGS, "-m", "nuitka", "--standalone", "--macos-create-app-bundle",
               "--macos-target-arch=" + arch,
               # libpython as the bundled shared library on both builds (Homebrew's default already;
               # python.org's framework offers no static archive, its libpython3.13.a is the dylib).
               "--static-libpython=no",
               "--report=" + str(Path(output) / "compilation-report.xml"),
               "--macos-app-name=" + APP, "--macos-app-version=" + version.current(),
               "--macos-app-icon=" + str(icns), "--macos-app-mode=gui",
               "--macos-app-macos-min-version=" + MINIMUM_MACOS,   # Info.plist LSMinimumSystemVersion
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
    command += list(TLS_ARGUMENTS)
    if sys.platform == "win32":
        # Windows has no system time-zone database: zoneinfo reads the tzdata package's files
        # (pyproject.toml, win32 only), which Nuitka must carry as data.
        command += ["--include-package=tzdata", "--include-package-data=tzdata"]
    return command + [str(ENTRY)]


def copy_runtime(runtime, target, node=None, arch=None):
    """Copy the verified browser runtime into the app (symlinks kept: node_modules/.bin). With `node` (a
    Node distribution folder holding bin/node and LICENSE), its node replaces the runtime's: the
    Intel build's x64 Node beside the shared node_modules. The bundled node must be `arch`."""
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
        if node and part.startswith("node/"):
            source = Path(node) / part[len("node/"):]
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination, symlinks=True)
        else:
            shutil.copy2(source, destination)
    problems = browser_runtime.verify({"browser_driver": {"runtime_dir": str(target)}})
    if problems:
        raise SystemExit("bundled browser runtime refused: " + "; ".join(problems))
    bundled = macho_arches(Path(target) / "node" / "bin" / "node")
    if arch and bundled != [arch]:
        raise SystemExit("the bundled node is %s, not %s only (pass --node <a darwin-%s Node folder>)"
                         % (" ".join(bundled) or "not a Mach-O", arch, "x64" if arch == "x86_64" else arch))


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
    parts.append(DYLIBS.format(python_build="python.org Python %s framework" % sys.version.split()[0],
                               python_dylibs=PYTHON_DYLIBS))
    for label, where in DYLIB_TEXTS:
        if Path(where).is_file():
            parts.append("%s\n%s\n%s" % (label, "-" * 40, Path(where).read_text(encoding="utf-8", errors="replace")))
    parts.append("Node.js and @playwright/mcp: see Contents/MacOS/browser-runtime (node/LICENSE and each "
                 "package's LICENSE under node_modules).\n")
    target = Path(app) / "Contents" / "Resources" / "licenses" / "THIRD-PARTY-NOTICES.txt"
    target.write_text("\n\n".join(parts), encoding="utf-8")
    return compiled_distributions(report)


def doctor_lines(binary, home, arch=None):
    import json
    environment = dict(os.environ, SIDEDOOR_HOME=str(home))
    environment.pop(browser_runtime.RUNTIME_VARIABLE, None)
    report = subprocess.run(["arch", "-" + (arch or host_arch()), str(binary), "doctor", "--json"],
                            capture_output=True, text=True, env=environment, timeout=300).stdout
    return {check["name"]: check for check in json.loads(report)["checks"]}


def smoke_uncompiled(app, arch=None):
    """The built app imports pystray and fpdf from their folders: doctor's Tray icon (loads pystray)
    and PDF writer (writes a PDF with fpdf2) lines are OK with the folders there, and report the
    library missing with the folders moved away. doctor starts no browser."""
    binary = Path(app) / "Contents" / "MacOS" / BINARY
    folders = [binary.parent / package for package in UNCOMPILED]
    with tempfile.TemporaryDirectory(prefix="sidedoor-smoke-") as home:
        help_text = subprocess.run(["arch", "-" + (arch or host_arch()), str(binary), "--help"],
                                   capture_output=True, text=True, timeout=120)
        if help_text.returncode or "usage" not in help_text.stdout.lower():
            raise SystemExit("`sidedoor --help` failed: " + (help_text.stderr or help_text.stdout)[-500:])
        present = doctor_lines(binary, home, arch)
        for folder in folders:
            folder.rename(folder.with_name(folder.name + ".aside"))
        try:
            absent = doctor_lines(binary, home, arch)
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
    parser.add_argument("--arch", choices=ARCHES, default=host_arch(),
                        help="arm64 (Apple silicon) or x86_64 (Intel); default: this interpreter's (%(default)s)")
    parser.add_argument("--node", type=Path, help="Node distribution folder (bin/node, LICENSE) for --arch, "
                                                  "replacing --runtime's node (the Intel build's darwin-x64 Node)")
    parser.add_argument("--jobs", type=int)
    parser.add_argument("--sign", metavar="IDENTITY", help="Developer ID Application identity (tools/sign_macos.py)")
    parser.add_argument("--notarize", metavar="PROFILE", help="notarytool keychain profile; needs --sign")
    args = parser.parse_args(argv)
    if args.notarize and not args.sign:
        raise SystemExit("--notarize needs --sign")
    if sys.platform != "darwin":
        raise SystemExit("build_macos.py builds on macOS only")
    refuse_non_python_org()
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
        subprocess.run(nuitka_command(output, icns, args.jobs, args.arch), check=True, cwd=REPO)
    built.rename(app)
    copy_runtime(args.runtime, app / "Contents" / "MacOS" / browser_runtime.RUNTIME_FOLDER,
                 args.node.expanduser().resolve() if args.node else None, args.arch)
    copy_uncompiled(app)
    print("architecture: %d Mach-O files, all %s only" % (check_arch(app, args.arch), args.arch))
    sys.path.insert(0, str(REPO / "tools"))
    import check_release_tls
    newer = check_release_tls.above_minimum(app)
    if newer:
        raise SystemExit("Mach-O files that need a newer macOS than LSMinimumSystemVersion %s: %s"
                         % (MINIMUM_MACOS, "; ".join("%s (%s)" % item for item in newer)))
    print("macOS minimum: LSMinimumSystemVersion %s; no Mach-O needs a newer one" % MINIMUM_MACOS)
    smoke_uncompiled(app, args.arch)
    clean_and_check(app)
    compiled = third_party_notices(app, output / "compilation-report.xml")
    print("compiled-in distributions: " + ", ".join(compiled))
    print("built %s (version %s, %s, %.1f MB)" % (app, version.current(), args.arch, size(app) / 1e6))
    if args.sign:
        # Last: nothing may be written into the bundle after it is sealed. The TLS release gate runs on
        # the signed app before it is notarized.
        import sign_macos
        sign_macos.finish(app, args.sign, args.notarize, output,
                          gate=lambda signed: check_release_tls.check(signed, args.arch))
        print("architecture after signing: %d Mach-O files, all %s only" % (check_arch(app, args.arch), args.arch))
    else:
        print("NOT RELEASE-CHECKED: this unsigned build did not run the TLS release gate (it needs codesign "
              "--verify to pass). It is a development build only: never publish it. A release build uses "
              "--sign (and --notarize), which runs the gate before notarizing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
