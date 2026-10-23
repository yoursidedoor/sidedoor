"""Build Sidedoor for Windows x64 with Nuitka (E9.T1), unsigned; --sign <mode> signs it (E9.T2).

    <x64 build venv>\\Scripts\\python.exe -X utf8 tools\\build_windows.py --output <folder outside the repo>
        --runtime <folder holding node_modules from `npm ci`> --node <node-v24.22.1-win-x64 folder> [--jobs N]
        [--iscc <Inno Setup 6 ISCC.exe> | --no-installer]
        [--sign ssl (--cert-subject <subject> | --cert-sha1 <thumbprint>) [--signtool <signtool.exe>]]
        [--sign azure --endpoint <url> --account <name> --certificate-profile <name>
                [--signtool <signtool.exe>] [--dlib <Azure.CodeSigning.Dlib.dll>]]

The Windows counterpart of tools/build_macos.py, sharing its pins and helpers (Nuitka 4.3.1, the product
modules, the string imports, the uncompiled LGPL libraries, the licence collection). The target is the
build interpreter's own architecture, read from the PE header of python.exe: users need x64, so the build
runs from an x64 python.org Python (on an ARM64 Windows machine it runs under x64 emulation and so do
Nuitka's compiler and the smoke run). Every PE file in the result (exe, dll, pyd, node addon) must be
that architecture (`check_arch`), or the build fails. Nuitka is run with -X utf8 (portable.UTF8_FLAGS,
W1 a): the binary keeps UTF-8 mode compiled in. Python 3.13 needs MSVC or Zig on Windows (Nuitka
refuses MinGW64 for 3.13); with no MSVC installed Nuitka fetches Zig (the `ziglang` package from
PyPI) into its own cache, `--zig` (the default here, `--compiler msvc` to use an installed Visual
Studio instead). The build venv also holds `ziglang` (ZIG_VERSION) for sidedoor-cli.exe.

The result is <output>\\Sidedoor\\, <output>\\Sidedoor-windows-x64.zip and the per-user installer
<output>\\Sidedoor-windows-x64-setup.exe (tools/sidedoor.iss; owner WI1 a), each with its .sha256 (no
version in the names, as on macOS):

    sidedoor.exe                 the app (tools/sidedoor.py -> orchestrator.cli), a windowed (GUI
                                 subsystem) program: no console window ever, so a double-click (which
                                 runs `sidedoor serve` with the tray icon), the login task and every job
                                 child it starts show none. A terminal does not wait for such a program,
                                 so typed commands use sidedoor-cli.exe (Nuitka
                                 --windows-console-mode=attach: its output still reaches a console or
                                 pipes it was given)
    sidedoor-cli.exe             the console front door for typed commands (`sidedoor-cli doctor`,
                                 `--help`; WB1): runs sidedoor.exe beside it with the same arguments and
                                 standard handles, waits, and exits with its code
                                 (tools/windows_cli_launcher.c, built with `zig cc`)
    <data files>                 prompts, schemas, defaults, dashboard_assets, assets
                                 (resources.nuitka_arguments); tzdata's zone files (no system database)
    browser-runtime\\             node\\node.exe and node\\LICENSE from --node (win-x64), node_modules
                                 from --runtime, package.json and package-lock.json from the repository
    pystray\\, fpdf\\              the LGPL-3.0 libraries' own .py sources, NOT compiled (owner TR1 a,
                                 AP1 a: separate and replaceable; imported from there at run time)
    licenses\\                    their licence texts and notices, THIRD-PARTY-NOTICES.txt
The icon is assets\\brand\\yoursidedoor-icon-512.png made into a multi-size .ico (Pillow).

Signing is opt-in: without --sign the build signs nothing and never runs signtool. Releases are signed
with `--sign azure` (owner, 2026-10-23: Azure Artifact Signing, account sidedoorsigning, certificate
profile sidedoor, Public Trust; the three values are the defaults in
tools/windows-signing.json, none of them secret). --sign <mode> signs every PE file that carries no
valid signature yet (Sidedoor's own exes and the unsigned dlls and pyds; python313.dll and node.exe must already carry their publishers' valid signatures, else
the build stops: they are never re-signed), RFC 3161 timestamped, in batches whose command lines stay
under SIGN_COMMAND_LIMIT characters, then `signtool verify /pa /tw` on each. The zip and the installer
are made after; the installer and its uninstaller are signed in the same mode (ISCC SignTool). Two modes:

  azure (releases): Azure Artifact (Trusted) Signing, signtool with /dlib Azure.CodeSigning.Dlib.dll (the
      Microsoft.ArtifactSigning.Client NuGet package, the renamed Microsoft.Trusted.Signing.Client) and a
      metadata JSON (Endpoint, CodeSigningAccountName, CertificateProfileName: the arguments, else
      AZURE_TRUSTED_SIGNING_ENDPOINT / _ACCOUNT / _CERTIFICATE_PROFILE, else tools/windows-signing.json),
      timestamped by Microsoft. The dlib needs the .NET 8 (or later) runtime of signtool's architecture
      (DOTNET_ROOT_X64 for an x64 signtool on ARM64 Windows) and an Azure login it can use: the owner's
      `az login` (Azure CLI), done by hand; this script never logs in and handles no token. Azure CLI on
      Windows keeps that login with the Windows account broker (WAM), which a process in an SSH session
      cannot use ("a token that does not exist"): run the signing build from the signed-in desktop
      session (a terminal there, or a task started in it).
  ssl (unused): SSL.com's eSigner CKA
      (Cloud Key Adapter), installed and signed in by the owner, puts the certificate in the user's
      Windows certificate store; plain signtool selects it by subject (/n, --cert-subject or
      SIDEDOOR_SIGN_SUBJECT) or SHA-1 thumbprint (/sha1, --cert-sha1 or SIDEDOOR_SIGN_SHA1), timestamped
      by http://ts.ssl.com. No PIN, TOTP or password passes through this script, its arguments or its
      output: CKA asks for and handles them itself.
Both need signtool from the Windows SDK.
"""

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

import browser_runtime  # noqa: E402
import build_macos  # noqa: E402  (the shared pins and helpers)
import portable  # noqa: E402
import resources  # noqa: E402
import version  # noqa: E402

APP = "Sidedoor"
EXE = "sidedoor.exe"
ENTRY = build_macos.ENTRY
ICON = build_macos.ICON
NUITKA_VERSION = build_macos.NUITKA_VERSION
UNCOMPILED = build_macos.UNCOMPILED
# PE machine types (winnt.h IMAGE_FILE_MACHINE_*).
MACHINES = {0x8664: "x64", 0xAA64: "arm64", 0x014C: "x86", 0xA641: "arm64ec"}
ASSETS = {"x64": "Sidedoor-windows-x64.zip", "arm64": "Sidedoor-windows-arm64.zip"}
PE_SUFFIXES = (".exe", ".dll", ".pyd", ".node")
# Windows' node zip: node.exe and LICENSE at its top (no bin\ folder; browser_runtime.node_path).
NODE_PARTS = ("node.exe", "LICENSE")
COMPILERS = ("zig", "msvc")
CLI = "sidedoor-cli.exe"
LAUNCHER = REPO / "tools" / "windows_cli_launcher.c"
ISS = REPO / "tools" / "sidedoor.iss"
ZIG_VERSION = "0.16.0"      # the ziglang Nuitka 4.3.1 fetched for the first build (2026-10-23)
ZIG_TARGETS = {"x64": "x86_64-windows-gnu", "arm64": "aarch64-windows-gnu"}
SETUPS = {"x64": "Sidedoor-windows-x64-setup", "arm64": "Sidedoor-windows-arm64-setup"}
ISCC_PLACES = (r"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe", r"%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe",
               r"%ProgramFiles%\Inno Setup 6\ISCC.exe")
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
# The build machine's own home and temp folders must not end up inside the build (checked after it).
PERSONAL = tuple(dict.fromkeys(text for base in (Path.home(), Path(tempfile.gettempdir()))
                               for text in (str(base), str(base).replace("\\", "/"))))
SIGN_VARIABLES = {"endpoint": "AZURE_TRUSTED_SIGNING_ENDPOINT", "account": "AZURE_TRUSTED_SIGNING_ACCOUNT",
                  "certificate_profile": "AZURE_TRUSTED_SIGNING_CERTIFICATE_PROFILE",
                  "signtool": "SIGNTOOL", "dlib": "AZURE_TRUSTED_SIGNING_DLIB"}
# Nuitka compiles in the UTF-8 mode of the Python that runs it (W1 a): portable.UTF8_FLAGS on Windows,
# spelled out here so the command line is the same wherever it is built or tested.
UTF8_FLAGS = ("-X", "utf8")
TIMESTAMP_URL = "http://timestamp.acs.microsoft.com"      # Azure Trusted Signing
SSL_TIMESTAMP_URL = "http://ts.ssl.com"
SSL_VARIABLES = {"cert_subject": "SIDEDOOR_SIGN_SUBJECT", "cert_sha1": "SIDEDOOR_SIGN_SHA1"}
SIGN_MODES = ("ssl", "azure")
SIGNING_DEFAULTS = REPO / "tools" / "windows-signing.json"   # the release's Azure account (no secrets)
# Windows' command-line limit is 32,767 characters; each signtool call stays well under it (WB6).
SIGN_COMMAND_LIMIT = 16000
NOTICE = """Sidedoor includes {dist} {version} ({homepage}), licensed under the GNU Lesser General Public
License version 3 ({files}; the LGPL builds on the GNU General Public License version 3, GPL-3.0.txt).

{dist} is not compiled into Sidedoor. Its unmodified Python sources are in the {package} folder
beside sidedoor.exe and are loaded from there each time Sidedoor starts. You may replace that folder
with another version of {dist} (or a modified one) that offers the same interface; Sidedoor then
uses it. Its source is also available from https://pypi.org/project/{dist}/{version}/.
"""
NATIVE = """Native libraries beside sidedoor.exe (*.dll, *.pyd)
=================================================

From the python.org Python {python} build for Windows: OpenSSL 3 (libssl, libcrypto; Apache License
2.0), libffi (MIT), sqlite (sqlite3.dll; public domain), and linked into Python's own modules
mpdecimal (BSD-2-Clause), XZ Utils (0BSD), zlib (zlib licence), bzip2 (bzip2 licence) and expat (MIT);
their texts are in Python's licence above.
From Pillow's wheel, linked into its modules: libjpeg-turbo (IJG and BSD-3-Clause; based in part on
the work of the Independent JPEG Group), libtiff, libwebp, lcms2, OpenJPEG, libavif, freetype,
zlib-ng; their texts are in Pillow's licence above.
The Visual C++ runtime (vcruntime140.dll, vcruntime140_1.dll), when present, is redistributed under
the Microsoft Visual Studio redistribution terms, as Python itself ships it.
"""


def pe_machine(path):
    """The PE machine of an exe/dll/pyd ("x64", "arm64", ...), or None when the file is not a PE image."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(4096)
    except OSError:
        return None
    if len(head) < 64 or head[:2] != b"MZ":
        return None
    offset = struct.unpack("<I", head[60:64])[0]
    if offset + 6 > len(head) or head[offset:offset + 4] != b"PE\0\0":
        return None
    machine = struct.unpack("<H", head[offset + 4:offset + 6])[0]
    return MACHINES.get(machine, hex(machine))


def host_arch():
    """The build interpreter's architecture from its own PE header (platform.machine() names the
    machine, ARM64, even inside an emulated x64 Python)."""
    return pe_machine(Path(sys.executable).resolve()) or pe_machine(Path(sys._base_executable).resolve())


def check_arch(folder, arch):
    """Every PE file under `folder` is `arch`. Returns their count; raises SystemExit naming the others."""
    found, wrong = 0, []
    for path in sorted(Path(folder).rglob("*")):
        if not path.is_file():
            continue
        machine = pe_machine(path)
        if machine is None:
            continue
        found += 1
        if machine != arch:
            wrong.append("%s (%s)" % (path.relative_to(folder), machine))
    if wrong:
        raise SystemExit("not %s: %s" % (arch, "; ".join(wrong)))
    return found


def make_ico(png, folder):
    """A multi-size .ico from the brand PNG (Pillow, a pystray dependency on Windows)."""
    from PIL import Image
    ico = Path(folder) / "sidedoor.ico"
    with Image.open(png) as image:
        image.convert("RGBA").save(ico, format="ICO", sizes=[(size, size) for size in ICO_SIZES])
    return ico


def windows_version():
    """Windows' four-part file version from Sidedoor's N.N.N."""
    return version.current() + ".0"


def nuitka_command(output, ico, jobs=None, compiler="zig", python=None):
    """The Nuitka command line. `python` is the build interpreter (default: this one)."""
    command = [python or sys.executable, *UTF8_FLAGS, "-m", "nuitka", "--standalone",
               "--windows-console-mode=attach", "--windows-icon-from-ico=" + str(ico),
               "--windows-company-name=" + APP, "--windows-product-name=" + APP,
               "--windows-file-description=" + APP, "--windows-file-version=" + windows_version(),
               "--windows-product-version=" + windows_version(),
               "--report=" + str(Path(output) / "compilation-report.xml"),
               "--output-dir=" + str(output), "--output-filename=" + EXE,
               "--nofollow-import-to=tests", "--nofollow-import-to=spikes", "--nofollow-import-to=manual_*",
               "--no-deployment-flag=excluded-module-usage",
               "--assume-yes-for-downloads", "--quiet"]
    if compiler == "zig":
        command.append("--zig")
    if jobs:
        command.append("--jobs=%d" % jobs)
    command += ["--nofollow-import-to=" + package for package in UNCOMPILED]
    command += ["--include-module=" + name for name in build_macos.product_modules() + list(build_macos.STRING_IMPORTS)
                + sorted({name for package in UNCOMPILED for name in build_macos.foreign_imports(package)})]
    command += resources.nuitka_arguments(REPO)
    # Windows has no system time-zone database: zoneinfo reads the tzdata package's files.
    command += ["--include-package=tzdata", "--include-package-data=tzdata"]
    return command + [str(ENTRY)]


def copy_runtime(runtime, node, target, arch):
    """The browser runtime into the app: node.exe and LICENSE from `node` (a win-<arch> Node folder),
    node_modules from `runtime` (an `npm ci` of browser-runtime/package-lock.json, pure JavaScript),
    the package files from the repository. Then browser_runtime.verify() and the lock's integrity."""
    target = Path(target)
    for part in NODE_PARTS:
        (target / "node").mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(node) / part, target / "node" / part)
    for name in build_macos.PACKAGE_FILES:
        shutil.copy2(REPO / browser_runtime.RUNTIME_FOLDER / name, target / name)
    shutil.copytree(Path(runtime) / "node_modules", target / "node_modules")
    problems = browser_runtime.verify({"browser_driver": {"runtime_dir": str(target)}})
    if problems:
        raise SystemExit("bundled browser runtime refused: " + "; ".join(problems))
    pinned = json.loads((REPO / browser_runtime.RUNTIME_FOLDER / "package-lock.json").read_text(encoding="utf-8"))["packages"]
    installed = json.loads((target / "node_modules" / ".package-lock.json").read_text(encoding="utf-8"))["packages"]
    if {name: entry.get("integrity") for name, entry in installed.items()} != \
            {name: entry.get("integrity") for name, entry in pinned.items() if name}:
        raise SystemExit("browser runtime at %s does not hold exactly the packages browser-runtime/package-lock.json "
                         "pins; run npm ci there" % runtime)
    machine = pe_machine(target / "node" / "node.exe")
    if machine != arch:
        raise SystemExit("the bundled node.exe is %s, not %s (pass --node <a win-%s Node folder>)" % (machine, arch, arch))


def copy_uncompiled(app):
    """pystray's and fpdf's sources (no bytecode) beside the exe, their licences and notices in licenses\\."""
    licenses = Path(app) / "licenses"
    licenses.mkdir(parents=True, exist_ok=True)
    gpl = None
    for package, name in UNCOMPILED.items():
        distribution = importlib.metadata.distribution(name)
        source = Path(importlib.util.find_spec(package).origin).parent
        shutil.copytree(source, Path(app) / package, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        written = []
        for file_name, text in build_macos.licence_files(distribution):
            if "GENERAL PUBLIC LICENSE" in text and "LESSER" not in text[:400]:
                gpl = text
                continue
            target = licenses / ("%s-%s" % (Path(file_name).name, name))
            target.write_text(text, encoding="utf-8")
            written.append(target.name)
        if not written:
            raise SystemExit("%s ships no licence file in its dist-info" % name)
        (licenses / ("NOTICE-%s.txt" % name)).write_text(NOTICE.format(
            dist=name, version=distribution.version, homepage=build_macos.HOMEPAGES[name], package=package,
            files=", ".join(written)), encoding="utf-8")
    if gpl is None:
        raise SystemExit("no GPL-3.0 text found among the LGPL libraries' licence files")
    (licenses / "GPL-3.0.txt").write_text(gpl, encoding="utf-8")


def third_party_notices(app, report):
    """licenses\\THIRD-PARTY-NOTICES.txt; fails when a compiled-in distribution has no licence file."""
    parts = ["Third-party software in Sidedoor %s\n%s\n" % (version.current(), "=" * 40)]
    parts.append("Python %s (CPython), Python Software Foundation License\n%s\n%s" % (
        sys.version.split()[0], "-" * 40, (Path(sys.base_prefix) / "LICENSE.txt").read_text(encoding="utf-8")))
    nuitka = importlib.metadata.distribution("nuitka")
    runtime = [text for name, text in build_macos.licence_files(nuitka) if "RUNTIME" in name]
    parts.append("Nuitka %s runtime library (compiled into the binary): AGPL-3.0 with the Nuitka Runtime "
                 "Library Exception\n%s\n%s" % (nuitka.version, "-" * 40, "\n".join(runtime)))
    missing = []
    compiled = build_macos.compiled_distributions(report)
    for name in compiled + sorted(UNCOMPILED.values()):
        distribution = importlib.metadata.distribution(name)
        files = build_macos.licence_files(distribution) or build_macos.licence_files(
            importlib.metadata.distribution(build_macos.LICENCE_FROM.get(name, name)))
        if not files:
            missing.append(name)
            continue
        package = next((package for package, dist in UNCOMPILED.items() if dist == name), None)
        how = "not compiled; replaceable, in the %s folder" % package if package else "compiled in"
        parts.append("%s %s (%s): %s\n%s\n%s" % (
            distribution.metadata["Name"], distribution.version, how,
            distribution.metadata.get("License-Expression") or distribution.metadata.get("License") or "see below",
            "-" * 40, "\n\n".join("[%s]\n%s" % (file_name, text) for file_name, text in files)))
    if missing:
        raise SystemExit("compiled-in distributions with no licence file: " + ", ".join(missing))
    parts.append(NATIVE.format(python=sys.version.split()[0]))
    parts.append("Node.js and @playwright/mcp: see browser-runtime (node\\LICENSE and each package's LICENSE "
                 "under node_modules).\n")
    (Path(app) / "licenses" / "THIRD-PARTY-NOTICES.txt").write_text("\n\n".join(parts), encoding="utf-8")
    return compiled


def smoke_environment(home):
    """A temp Sidedoor home, no browser runtime override, and no OS credential store (never the
    signed-in user's Credential Manager)."""
    import credentials
    environment = dict(os.environ, SIDEDOOR_HOME=str(home))
    environment[credentials.STORE_VARIABLE] = "file"
    environment.pop(browser_runtime.RUNTIME_VARIABLE, None)
    return environment


def hidden():
    """No console window for a console program the build starts (the smoke runs on a user's screen)."""
    return portable.no_window()


def zig_command(app, arch, python=None):
    """sidedoor-cli.exe from tools/windows_cli_launcher.c, with the build venv's `ziglang`."""
    return [python or sys.executable, "-m", "ziglang", "cc", "-target", ZIG_TARGETS[arch], "-O2", "-s",
            "-o", str(Path(app) / CLI), str(LAUNCHER)]


def build_cli(app, arch):
    try:
        found = importlib.metadata.version("ziglang")
    except importlib.metadata.PackageNotFoundError:
        found = None
    if found != ZIG_VERSION:
        raise SystemExit("sidedoor-cli.exe needs ziglang %s in the build venv (pip install ziglang==%s); found %s"
                         % (ZIG_VERSION, ZIG_VERSION, found))
    subprocess.run(zig_command(app, arch), check=True, **hidden())
    for leftover in Path(app).glob("sidedoor-cli.pdb"):
        leftover.unlink()


def doctor_checks(cli, home):
    result = subprocess.run([str(cli), "doctor", "--json"], capture_output=True, text=True, encoding="utf-8",
                            env=smoke_environment(home), timeout=300, **hidden())
    try:
        return {check["name"]: check for check in json.loads(result.stdout)["checks"]}
    except (ValueError, KeyError):
        raise SystemExit("`sidedoor-cli doctor --json` gave no report (exit %s): %s"
                         % (result.returncode, (result.stderr or result.stdout)[-800:]))


def program_check(present, exe):
    """WB2: the built app's scheduler_loop.program() (Nuitka's __compiled__.process_exe) is the windowed
    sidedoor.exe itself: doctor's App location line names it in a built app. Compared as files, not text:
    Nuitka 4.3.1 reports it as an 8.3 short path (for example ...\\WINBUI~2\\Sidedoor\\sidedoor.exe)."""
    import re
    line = present["App location"]["line"]
    named = re.search(r"runs from (.+)\)\.$", line)
    if not named or not os.path.isfile(named.group(1)) or not os.path.samefile(named.group(1), exe):
        raise SystemExit("scheduler_loop.program() is not %s: %r" % (exe, line))
    return line


def reply_tools_round_trip(exe, home):
    """WB2: `sidedoor.exe reply-tools serve` answers an MCP initialize through pipes, as codex starts it."""
    import reply_tools
    with tempfile.TemporaryDirectory(prefix="sidedoor-reply-tools-") as directory:
        (Path(directory) / reply_tools.CONTEXT_FILE).write_text("{}", encoding="utf-8")
        request = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                              "clientInfo": {"name": "build-smoke", "version": "1"}}}
        process = subprocess.Popen([str(exe), "reply-tools", "serve", "--directory", directory],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", env=smoke_environment(home), **hidden())
        try:
            out, err = process.communicate(json.dumps(request) + "\n", timeout=120)
        except subprocess.TimeoutExpired:
            process.kill()
            raise SystemExit("`sidedoor reply-tools serve` did not answer within 120 s")
    try:
        answer = json.loads(out.splitlines()[0])
        name = answer["result"]["serverInfo"]["name"]
    except (ValueError, IndexError, KeyError, TypeError):
        raise SystemExit("`sidedoor reply-tools serve` gave no initialize answer: %r %r" % (out[-300:], err[-300:]))
    if answer.get("id") != 1 or name != "reply-tools":
        raise SystemExit("`sidedoor reply-tools serve` answered %r" % answer)
    return answer


def smoke(app):
    """Through sidedoor-cli.exe: --help works; doctor's Tray icon and PDF writer are OK with pystray and fpdf
    in their folders and report them missing with the folders moved away; App location names sidedoor.exe
    (program()). Then sidedoor.exe's reply-tools answers an MCP initialize through pipes. doctor starts no
    browser; nothing here starts Chrome."""
    exe, cli = Path(app) / EXE, Path(app) / CLI
    folders = [Path(app) / package for package in UNCOMPILED]
    with tempfile.TemporaryDirectory(prefix="sidedoor-smoke-") as home:
        shown = subprocess.run([str(cli), "--help"], capture_output=True, text=True, encoding="utf-8",
                               env=smoke_environment(home), timeout=120, **hidden())
        if shown.returncode or "usage" not in shown.stdout.lower():
            raise SystemExit("`sidedoor-cli --help` failed: " + (shown.stderr or shown.stdout)[-500:])
        present = doctor_checks(cli, home)
        print("App location: " + program_check(present, exe))
        for folder in folders:
            folder.rename(folder.with_name(folder.name + ".aside"))
        try:
            absent = doctor_checks(cli, home)
        finally:
            for folder in folders:
                folder.with_name(folder.name + ".aside").rename(folder)
        reply_tools_round_trip(exe, home)
        print("reply-tools: answered initialize through pipes")
    for name, gone in {"Tray icon": "missing", "PDF writer": "can't write PDFs"}.items():
        if present[name]["status"] != "OK" or gone not in absent[name]["line"]:
            raise SystemExit("%s: not loaded from its folder (with it %r, without it %r)" % (name, present[name], absent[name]))
        print("%s: %s; folder moved away: %s" % (name, present[name]["line"], absent[name]["line"]))
    return present


def clean_and_check(app):
    """No bytecode anywhere, and no build-machine path in the uncompiled libraries' folders."""
    for cache in list(Path(app).rglob("__pycache__")):
        shutil.rmtree(cache)
    left = [str(path) for path in Path(app).rglob("*") if path.name == "__pycache__" or path.suffix == ".pyc"]
    if left:
        raise SystemExit("bytecode left in the build: " + ", ".join(left[:5]))
    for package in UNCOMPILED:
        for path in (Path(app) / package).rglob("*"):
            if path.is_file():
                data = path.read_bytes()
                found = [text for text in PERSONAL if text.encode() in data]
                if found:
                    raise SystemExit("%s holds a build-machine path (%s)" % (path, ", ".join(found)))


# --- Azure Trusted Signing -------------------------------------------------------------------------

def sign_settings(args, environ=None, defaults_file=SIGNING_DEFAULTS):
    """The signing settings for args.sign's mode ("ssl" or "azure"), from the arguments, else the
    environment. Raises SystemExit naming what is missing."""
    environ = os.environ if environ is None else environ
    mode = getattr(args, "sign", None)
    if mode not in SIGN_MODES:
        raise SystemExit("--sign is ssl or azure, not %r" % mode)
    signtool = getattr(args, "signtool", None) or environ.get(SIGN_VARIABLES["signtool"]) or "signtool.exe"
    if mode == "ssl":
        settings = {key: getattr(args, key, None) or environ.get(variable) for key, variable in SSL_VARIABLES.items()}
        chosen = [key for key in SSL_VARIABLES if settings[key]]
        if len(chosen) != 1:
            raise SystemExit("--sign ssl needs exactly one of --cert-subject (or SIDEDOOR_SIGN_SUBJECT) and "
                             "--cert-sha1 (or SIDEDOOR_SIGN_SHA1)")
        return dict(settings, mode="ssl", signtool=signtool)
    defaults = json.loads(Path(defaults_file).read_text(encoding="utf-8")).get("azure", {}) if defaults_file else {}
    settings = {key: getattr(args, key, None) or environ.get(variable) or defaults.get(key)
                for key, variable in SIGN_VARIABLES.items()}
    settings.update(mode="azure", signtool=signtool, exclude_credentials=defaults.get("exclude_credentials"))
    missing = ["--%s (or %s)" % (key.replace("_", "-"), SIGN_VARIABLES[key])
               for key in ("endpoint", "account", "certificate_profile", "dlib") if not settings[key]]
    if missing:
        raise SystemExit("--sign azure needs " + ", ".join(missing))
    return settings


def signing_metadata(settings):
    """Azure: the metadata JSON the dlib reads (/dmdf). The release file excludes every credential but
    Azure CLI's (the owner's `az login`): the others are probed first and one of them hung signtool for
    minutes on the VM."""
    metadata = {"Endpoint": settings["endpoint"], "CodeSigningAccountName": settings["account"],
                "CertificateProfileName": settings["certificate_profile"]}
    if settings.get("exclude_credentials"):
        metadata["ExcludeCredentials"] = list(settings["exclude_credentials"])
    return metadata


def certificate_selector(settings):
    """SSL.com: the certificate in the user's store, by thumbprint or subject."""
    return ["/sha1", settings["cert_sha1"]] if settings.get("cert_sha1") else ["/n", settings["cert_subject"]]


def sign_command(settings, metadata_path, files):
    """signtool sign with a SHA-256 file digest and an RFC 3161 timestamp, in the settings' mode."""
    files = [str(path) for path in files]
    if settings["mode"] == "ssl":
        return [settings["signtool"], "sign", "/fd", "SHA256", "/tr", SSL_TIMESTAMP_URL, "/td", "SHA256",
                *certificate_selector(settings), *files]
    return [settings["signtool"], "sign", "/v", "/fd", "SHA256", "/tr", TIMESTAMP_URL, "/td", "SHA256",
            "/dlib", str(settings["dlib"]), "/dmdf", str(metadata_path), *files]


def metadata_file(settings, folder):
    """Azure's metadata JSON written into `folder`; None in the ssl mode (CKA needs none)."""
    if settings["mode"] != "azure":
        return None
    path = Path(folder) / "metadata.json"
    path.write_text(json.dumps(signing_metadata(settings), indent=2), encoding="utf-8")
    return path


def verify_command(settings, path):
    """Authenticode policy, warning when the signature carries no timestamp (WB5)."""
    return [settings["signtool"], "verify", "/pa", "/tw", str(path)]


def command_length(command):
    """subprocess.list2cmdline's length, the command line Windows receives."""
    return len(subprocess.list2cmdline([str(part) for part in command]))


def sign_batches(settings, metadata_path, files, limit=SIGN_COMMAND_LIMIT):
    """`files` split so each sign command line stays under `limit` characters (WB6)."""
    batches, batch = [], []
    for path in files:
        if batch and command_length(sign_command(settings, metadata_path, batch + [path])) > limit:
            batches.append(batch)
            batch = []
        if command_length(sign_command(settings, metadata_path, [path])) > limit:
            raise SystemExit("path too long to sign: %s" % path)
        batch.append(path)
    if batch:
        batches.append(batch)
    return batches


def publisher_signed(app):
    """The files that must already carry their publishers' valid signatures (WB4): CPython's DLL
    (Python Software Foundation) and node.exe (OpenJS Foundation). Never re-signed."""
    return [Path(app) / ("python%d%d.dll" % sys.version_info[:2]),
            Path(app) / browser_runtime.RUNTIME_FOLDER / "node" / "node.exe"]


def pe_files(app):
    return sorted(path for path in Path(app).rglob("*") if path.is_file() and path.suffix.lower() in PE_SUFFIXES
                  and pe_machine(path))


def sign(app, settings, run=subprocess.run):
    """Sign every PE file under `app` that has no valid signature yet, then verify every PE file. Stops
    first when python3NN.dll or node.exe does not verify (WB4): they are never re-signed."""
    def verifies(path):
        return run(verify_command(settings, path), capture_output=True, **hidden()).returncode == 0
    broken = [str(path) for path in publisher_signed(app) if not verifies(path)]
    if broken:
        raise SystemExit("not signing: these must carry their publishers' valid signatures and do not: "
                         + "; ".join(broken))
    unsigned = [path for path in pe_files(app) if not verifies(path)]
    if not unsigned:
        raise SystemExit("nothing to sign under %s" % app)
    with tempfile.TemporaryDirectory(prefix="sidedoor-sign-") as scratch:
        metadata = metadata_file(settings, scratch)
        for batch in sign_batches(settings, metadata, unsigned):
            run(sign_command(settings, metadata, batch), check=True, **hidden())
    failed = [str(path) for path in pe_files(app) if not verifies(path)]
    if failed:
        raise SystemExit("signature does not verify: " + "; ".join(failed[:10]))
    return unsigned


# --- packing -----------------------------------------------------------------------------------------

def checksum(target):
    digest = hashlib.sha256(Path(target).read_bytes()).hexdigest()
    Path(target).with_name(Path(target).name + ".sha256").write_text("%s  %s\n" % (digest, Path(target).name),
                                                                    encoding="utf-8")
    return digest


def pack(app, output, arch):
    """<output>\\Sidedoor-windows-<arch>.zip holding the Sidedoor folder, and its .sha256."""
    target = Path(output) / ASSETS[arch]
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(Path(app).rglob("*")):
            if path.is_file():
                archive.write(path, Path(APP) / path.relative_to(app))
    return target, checksum(target)


def find_iscc(given=None, environ=None):
    """Inno Setup 6's ISCC.exe: --iscc, else ISCC in the environment, else its usual places."""
    environ = os.environ if environ is None else environ
    if given or environ.get("ISCC"):
        return Path(given or environ["ISCC"])
    for place in ISCC_PLACES:
        path = Path(os.path.expandvars(place))
        if path.is_file():
            return path
    raise SystemExit("Inno Setup 6 not found: pass --iscc <ISCC.exe> (or --no-installer)")


def inno_sign_tool(settings, metadata_path):
    """ISCC's SignTool command ($q a quote, $f the quoted file): sign_command with Inno's quoting."""
    command = sign_command(settings, metadata_path, [])
    quoted = {0}
    if settings["mode"] == "ssl":
        quoted.add(len(command) - 1)                       # the subject or thumbprint
    else:
        quoted.update((command.index("/dlib") + 1, command.index("/dmdf") + 1))
    return " ".join("$q%s$q" % part if index in quoted else part for index, part in enumerate(command)) + " $f"


def installer_command(iscc, app, output, arch, ico, settings=None, metadata_path=None):
    command = [str(iscc), "/Q", "/DAppVersion=" + version.current(), "/DSourceDir=" + str(app),
               "/DOutputDir=" + str(output), "/DIconFile=" + str(ico), "/DOutputName=" + SETUPS[arch]]
    if settings:
        command += ["/DSign", "/Ssidedoor=" + inno_sign_tool(settings, metadata_path)]
    return command + [str(ISS)]


def build_installer(iscc, app, output, arch, ico, settings=None, run=subprocess.run):
    """<output>\\Sidedoor-windows-<arch>-setup.exe (tools/sidedoor.iss), signed with its uninstaller
    under --sign, and its .sha256."""
    target = Path(output) / (SETUPS[arch] + ".exe")
    with tempfile.TemporaryDirectory(prefix="sidedoor-setup-") as scratch:
        metadata = metadata_file(settings, scratch) if settings else None
        run(installer_command(iscc, app, output, arch, ico, settings, metadata), check=True, **hidden())
    if settings and run(verify_command(settings, target), capture_output=True, **hidden()).returncode:
        raise SystemExit("the installer's signature does not verify: %s" % target)
    return target, checksum(target)


def size(path):
    return sum(item.stat().st_size for item in Path(path).rglob("*") if item.is_file())


def parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, required=True, help="build folder, outside the repository")
    parser.add_argument("--runtime", type=Path, required=True,
                        help="folder holding node_modules from `npm ci` of browser-runtime/package-lock.json")
    parser.add_argument("--node", type=Path, required=True, help="Node folder for the target (node-v24.22.1-win-x64)")
    parser.add_argument("--jobs", type=int)
    parser.add_argument("--compiler", choices=COMPILERS, default="zig")
    parser.add_argument("--iscc", help="Inno Setup 6 ISCC.exe; default: ISCC in the environment, else its usual places")
    parser.add_argument("--no-installer", action="store_true", help="skip the Inno Setup installer")
    parser.add_argument("--sign", choices=SIGN_MODES,
                        help="off by default (the beta ships unsigned); sign before packing: ssl (SSL.com eSigner "
                             "CKA) or azure (Trusted Signing)")
    parser.add_argument("--cert-subject", dest="cert_subject", help="ssl: the certificate's subject name (signtool /n)")
    parser.add_argument("--cert-sha1", dest="cert_sha1", help="ssl: the certificate's SHA-1 thumbprint (signtool /sha1)")
    parser.add_argument("--endpoint", help="azure: Trusted Signing endpoint, e.g. https://eus.codesigning.azure.net")
    parser.add_argument("--account", help="azure: Trusted Signing account name")
    parser.add_argument("--certificate-profile", dest="certificate_profile", help="azure: certificate profile name")
    parser.add_argument("--signtool", help="signtool.exe (Windows SDK); default: signtool.exe on PATH")
    parser.add_argument("--dlib", help="azure: Azure.CodeSigning.Dlib.dll (Microsoft.Trusted.Signing.Client NuGet)")
    return parser


def main(argv=None):
    args = parser().parse_args(argv)
    settings = sign_settings(args) if args.sign else None   # before an hour of compiling
    if sys.platform != "win32":
        raise SystemExit("build_windows.py builds on Windows only")
    iscc = None if args.no_installer else find_iscc(args.iscc)
    if importlib.metadata.version("nuitka") != NUITKA_VERSION:
        raise SystemExit("Nuitka %s is pinned; this venv has %s" % (NUITKA_VERSION, importlib.metadata.version("nuitka")))
    if not sys.flags.utf8_mode:
        raise SystemExit("run the build with -X utf8 (Nuitka compiles in the mode it runs in)")
    arch = host_arch()
    if arch not in ASSETS:
        raise SystemExit("the build interpreter is %s; use an x64 (or arm64) python.org Python" % arch)
    output = args.output.expanduser().resolve()
    if output == REPO or REPO in output.parents:
        raise SystemExit("--output must be outside the repository: " + str(output))
    output.mkdir(parents=True, exist_ok=True)
    app = output / APP
    built = output / (ENTRY.stem + ".dist")
    for old in (app, built, output / (ENTRY.stem + ".build"), output / ASSETS[arch], output / (SETUPS[arch] + ".exe")):
        if old.is_dir():
            shutil.rmtree(old)
        elif old.exists():
            old.unlink()
    ico = make_ico(ICON, output)
    subprocess.run(nuitka_command(output, ico, args.jobs, args.compiler), check=True, cwd=REPO)
    built.rename(app)
    build_cli(app, arch)
    copy_runtime(args.runtime.expanduser().resolve(), args.node.expanduser().resolve(),
                 app / browser_runtime.RUNTIME_FOLDER, arch)
    copy_uncompiled(app)
    print("architecture: %d PE files, all %s" % (check_arch(app, arch), arch))
    smoke(app)
    clean_and_check(app)
    compiled = third_party_notices(app, output / "compilation-report.xml")
    print("compiled-in distributions: " + ", ".join(compiled))
    if settings:
        signed = sign(app, settings)
        print("signed %d files" % len(signed))
    archive, digest = pack(app, output, arch)
    print("built %s (version %s, %s, %.1f MB); %s %.1f MB sha256 %s" % (
        app, version.current(), arch, size(app) / 1e6, archive.name, archive.stat().st_size / 1e6, digest))
    if iscc:
        setup, digest = build_installer(iscc, app, output, arch, ico, settings)
        print("installer %s %.1f MB sha256 %s" % (setup.name, setup.stat().st_size / 1e6, digest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
