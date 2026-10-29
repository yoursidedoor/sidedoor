"""Release gate (hotfix 2026-10-28): the built Mac app makes HTTPS calls on a Mac without the build
machine's developer folders.

    <build venv>/bin/python tools/check_release_tls.py <path>/Sidedoor.app [--arch arm64|x86_64]

tools/build_macos.py runs `check` after signing (before notarizing); standalone it checks any built,
signed (or, for a test build only, ad-hoc signed) app. In order, each step failing the gate:

  1. `codesign --verify --deep --strict` passes on the app.
  2. The app's own `sidedoor net-check` (one GET of OpenRouter's model list through text_model's
     urllib path) succeeds with SSL_CERT_FILE and SSL_CERT_DIR pointed at an empty folder and HOME at
     another, under `perl -e 'alarm 60; exec @ARGV'`: OpenSSL's compiled-in certificate folders count
     for nothing, as on a Mac without Homebrew (arm64) or the python.org framework (Intel). Its trust
     must be the OS trust store (tls_setup "truststore").
  3. The same with SIDEDOOR_TLS_SKIP_TRUSTSTORE=1 and SSL_CERT_FILE unset (release check only,
     tls_setup.SKIP_TRUSTSTORE): truststore is skipped and OpenSSL's default file treated as missing,
     so the app takes its real fallback, certifi.where() inside the bundle (tls_setup "bundled", the
     file inside the app), and that file verifies the connection on its own.
  4. Every Mach-O file is scanned for build-machine paths (/opt/homebrew, /usr/local/Cellar,
     /usr/local/opt, /usr/local/lib, /Library/Frameworks/Python.framework). A load command naming one fails (dyld would look
     there). An LC_RPATH naming a Homebrew folder fails (`homebrew_rpath`, E6/T10: sign_macos deletes every
     absolute LC_RPATH before signing, so one left is a regression); another LC_RPATH naming one is harmless
     only when that file loads nothing through @rpath; an
     LC_ID_DYLIB is a library's own name, never used to find it. Every other occurrence must match
     one entry of DOCUMENTED (file, pattern, reason) or come from a source in SOURCE_REASONS (the
     main binary holds the compiled modules' string constants); anything else fails.
  5. No Mach-O file needs a newer macOS than the app's Info.plist LSMinimumSystemVersion (its
     LC_BUILD_VERSION minos or LC_VERSION_MIN_MACOSX version, from `otool -l`), and the key is there:
     macOS refuses to load such a file on an older Mac the app says it supports (E3, E4).
"""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import sysconfig
import tempfile

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

# Intel Homebrew's folders too (/usr/local/opt, /usr/local/lib); /usr/local/Cellar is where they point.
_ROOTS = r"/opt/homebrew|/usr/local/Cellar|/usr/local/opt/|/usr/local/lib/|/Library/Frameworks/Python\.framework"
DEVELOPER = re.compile(("(?:%s)" % _ROOTS).encode() + rb"[!#-&*-:<-~]*")
SPLIT = re.compile(r"(?=%s)" % _ROOTS)   # one constant each
LOADS = {"LC_LOAD_DYLIB", "LC_LOAD_WEAK_DYLIB", "LC_REEXPORT_DYLIB", "LC_LAZY_LOAD_DYLIB", "LC_LOAD_UPWARD_DYLIB"}
TIMEOUT = 60
PROBE_ENV = ("PATH", "TMPDIR", "LANG")   # the only variables kept from the build's environment

# The OpenSSL folders the bundled libcrypto was configured with (OPENSSLDIR, ENGINESDIR, MODULESDIR).
_OPENSSL = r"(?:/opt/homebrew/etc/openssl@3|/Library/Frameworks/Python\.framework/Versions/3\.\d+/etc/openssl)"
_OPENSSL_LIB = r"(?:/opt/homebrew/Cellar/openssl@3/[^/]+/lib|/Library/Frameworks/Python\.framework/Versions/3\.\d+/lib)"
# (file name pattern, path pattern, why it does not matter at run time). Narrow on purpose: each names
# one library and one compiled-in location.
DOCUMENTED = (
    (r"libcrypto\.3\.dylib", _OPENSSL + r"/(?:cert\.pem|certs)",
     "OpenSSL's default trust file and folder. Sidedoor no longer depends on them: tls_setup trusts the OS "
     "store (or the bundled certifi file), and the net-check above passed with both emptied."),
    (r"libcrypto\.3\.dylib", _OPENSSL,
     "OPENSSLDIR: OpenSSL reads openssl.cnf there only if one exists; absent, its built-in defaults apply."),
    (r"libcrypto\.3\.dylib", _OPENSSL + r"/ct_log_list\.cnf",
     "Certificate Transparency log list; Python never enables CT validation."),
    (r"libcrypto\.3\.dylib", _OPENSSL + r"/private",
     "the default private-key folder; only the openssl command-line tool uses it."),
    (r"libcrypto\.3\.dylib", _OPENSSL_LIB + r"/engines-3",
     "ENGINESDIR: nothing in Python or Sidedoor loads an OpenSSL engine."),
    (r"libcrypto\.3\.dylib", _OPENSSL_LIB + r"/ossl-modules",
     "MODULESDIR: Python uses OpenSSL's built-in default provider only; no provider module is loaded."),
    (r"Python", r"(?:/opt/homebrew/Cellar/python@3\.\d+/[^/]+/Frameworks|/Library/Frameworks)"
                r"/Python\.framework/Versions/3\.\d+",
     "libpython's compiled-in install prefix; the Nuitka binary sets the bundle as Python's home, so the "
     "prefix is never searched."),
    (r"Python", r"/usr/local/lib/python2\.5/site-packages(?:/foo|/bar)?",
     "the example folders in site.py's docstring (frozen into libpython): text, never searched."),
)
# Sources compiled into the main binary whose constants may name a build-machine path, and why.
SOURCE_REASONS = {
    "sysconfig data": "the build Python's recorded configure settings (_sysconfigdata), read only by "
                      "tools that build extensions; Sidedoor never asks sysconfig for a path.",
    "site": "site-packages folders and Homebrew's sitecustomize; the standalone binary runs without site, "
            "and sitecustomize acts only when the running executable is inside Homebrew's Cellar.",
    "ctypes search": "ctypes.util.find_library's fallback folders (ctypes.macholib.dyld); searched in turn, "
                     "a missing folder is skipped (truststore loads the system Security framework).",
    "mime types": "mimetypes.knownfiles, the Unix mime.types files mimetypes reads when present; a missing "
                  "file is skipped.",
    "product": "folders where Sidedoor looks for a program the user installed (Codex, Antigravity); "
               "a search location, fine when absent.",
}


def codesign_verified(app):
    result = subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], capture_output=True, text=True)
    if result.returncode:
        raise SystemExit("release TLS gate: codesign --verify failed, so the app is not checked: "
                         + result.stderr.strip()[-500:])


def bundled_ca(app):
    found = sorted(path for path in Path(app).rglob("cacert.pem") if path.is_file() and "certifi" in path.parts)
    if not found:
        raise SystemExit("release TLS gate: the bundled certifi cacert.pem is missing")
    return found[0]


def net_check(app, arch, skip_truststore=False):
    """Run the app's `sidedoor net-check` with the default certificate locations emptied; returns its
    JSON result. `skip_truststore`: SIDEDOOR_TLS_SKIP_TRUSTSTORE=1 and SSL_CERT_FILE unset (step 3)."""
    binary = Path(app) / "Contents" / "MacOS" / "sidedoor"
    with tempfile.TemporaryDirectory(prefix="sidedoor-tls-empty-") as empty, \
            tempfile.TemporaryDirectory(prefix="sidedoor-tls-home-") as home:
        environment = {name: os.environ[name] for name in PROBE_ENV if name in os.environ}
        environment.update(HOME=home, SIDEDOOR_HOME=str(Path(home) / "sidedoor"), SSL_CERT_DIR=empty)
        if skip_truststore:
            environment["SIDEDOOR_TLS_SKIP_TRUSTSTORE"] = "1"   # tls_setup.SKIP_TRUSTSTORE
        else:
            environment["SSL_CERT_FILE"] = empty
        command = ["perl", "-e", "alarm %d; exec @ARGV" % TIMEOUT, "arch", "-" + arch, str(binary), "net-check"]
        result = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=TIMEOUT + 30)
    lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
    try:
        answer = json.loads(lines[-1])
    except (IndexError, ValueError):
        raise SystemExit("release TLS gate: `sidedoor net-check` gave no result (exit %s): %s"
                         % (result.returncode, (result.stderr or result.stdout)[-800:]))
    answer["exit"] = result.returncode
    return answer


def fallback_ok(answer, app):
    """Step 3 passed: HTTPS worked with trust "bundled" and the file certifi.where() gave lies inside the
    app (the bundled cacert.pem, `bundled_ca`)."""
    ca_file = answer.get("ca_file")
    inside = bool(ca_file) and Path(ca_file).resolve() == bundled_ca(app).resolve()
    return bool(answer.get("ok")) and answer.get("trust") == "bundled" and inside


def load_commands(path):
    """{"loads", "rpaths", "ids", "minos"} from `otool -l` ("minos": the macOS versions the file needs,
    from LC_BUILD_VERSION's minos and LC_VERSION_MIN_MACOSX's version)."""
    found = {"loads": [], "rpaths": [], "ids": [], "minos": []}
    output = subprocess.run(["otool", "-l", str(path)], capture_output=True, text=True).stdout
    command = None
    for line in output.splitlines():
        words = line.split()
        if words[:1] == ["cmd"]:
            command = words[1] if len(words) > 1 else None
        elif len(words) == 2 and ((command == "LC_BUILD_VERSION" and words[0] == "minos")
                                  or (command == "LC_VERSION_MIN_MACOSX" and words[0] == "version")):
            found["minos"].append(words[1])
        elif words[:1] in (["name"], ["path"]) and command:
            value = line.split(None, 1)[1].rsplit(" (offset", 1)[0]
            if command in LOADS:
                found["loads"].append(value)
            elif command == "LC_RPATH":
                found["rpaths"].append(value)
            elif command == "LC_ID_DYLIB":
                found["ids"].append(value)
            command = None
    return found


def source_corpus(root=REPO):
    """{SOURCE_REASONS key: text} of the sources compiled into the main binary that may name build paths:
    this (the building) interpreter's standard library files, and the product's modules. Run the gate
    with the interpreter that built the app."""
    import importlib.util
    stdlib = Path(sysconfig.get_paths()["stdlib"])
    data_module = getattr(sysconfig, "_get_sysconfigdata_name", lambda: "")()
    spec = importlib.util.find_spec(data_module) if data_module else None
    names = {"sysconfig data": ([Path(spec.origin)] if spec and spec.origin else [])
                               + [stdlib / "sysconfig" / "__init__.py", stdlib / "sysconfig.py"],
             "site": [stdlib / "site.py", stdlib / "sitecustomize.py"],
             "ctypes search": [stdlib / "ctypes" / "macholib" / "dyld.py"],
             "mime types": [stdlib / "mimetypes.py"],
             "product": [path for path in sorted(Path(root).glob("*.py")) if not path.name.startswith("manual_")]}
    corpus = {key: "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in paths if path.is_file())
              for key, paths in names.items()}
    corpus["sysconfig data"] += "\n" + json.dumps(sysconfig.get_config_vars(), default=str)
    return corpus


def explain(name, token, commands, corpus, main):
    """Why `token` (a build path found in file `name`) is harmless, or None."""
    if any(token == value for value in commands["ids"]):
        return "LC_ID_DYLIB: the library's own install name, never used to find it."
    if any(token == value for value in commands["rpaths"]):
        if homebrew_rpath(token):
            return None
        if any(value.startswith("@rpath/") for value in commands["loads"]):
            return None
        return "LC_RPATH, but this file loads nothing through @rpath."
    for pattern, path, reason in DOCUMENTED:
        if re.fullmatch(pattern, name) and re.fullmatch(path, token):
            return reason
    if main:
        # A constant runs into the next one's type and length bytes ("...site-packageszM/opt/...").
        for candidate in (token, token[:-1], token[:-2]):
            for key, text in corpus.items():
                if len(candidate) >= len("/opt/homebrew") and candidate in text:
                    return "%s: %s" % (key, SOURCE_REASONS[key])
    return None


def scan(app, corpus=None):
    """(explained, unexplained): [(file, path, reason)] and [(file, path, why)] for every build-machine
    path in the bundle's Mach-O files."""
    import sign_macos
    corpus = source_corpus() if corpus is None else corpus
    main = sign_macos.main_executable(app)
    explained, unexplained = [], []
    for folder, _, files in os.walk(app):
        for file_name in sorted(files):
            path = Path(folder) / file_name
            if path.is_symlink() or not sign_macos.is_macho(path):
                continue
            commands = load_commands(path)
            relative = str(path.relative_to(app))
            for value in commands["loads"]:
                if DEVELOPER.match(value.encode()):
                    unexplained.append((relative, value, "a load command: dyld looks for it there"))
            tokens = sorted({part for match in DEVELOPER.finditer(path.read_bytes())
                             for part in SPLIT.split(match.group().decode("ascii")) if part})
            for token in tokens:
                if token in commands["loads"]:
                    continue
                reason = explain(file_name, token, commands, corpus, path == main)
                (explained if reason else unexplained).append((relative, token, reason or "undocumented"))
    return explained, unexplained


# Homebrew's folders (Apple silicon and Intel): an LC_RPATH naming one always fails the gate (T10).
HOMEBREW_RPATH = re.compile(r"(?:/opt/homebrew|/usr/local/Cellar|/usr/local/opt|/usr/local/lib)(?:/|$)")


def homebrew_rpath(rpath):
    """True for an LC_RPATH value inside a Homebrew folder."""
    return bool(HOMEBREW_RPATH.match(rpath))


def homebrew_rpaths(app):
    """[(file, rpath)] for every LC_RPATH in the bundle's Mach-O files that names a Homebrew folder
    (/opt/homebrew, /usr/local/Cellar, /usr/local/opt, /usr/local/lib)."""
    import sign_macos
    found = []
    for folder, _, files in os.walk(app):
        for file_name in sorted(files):
            path = Path(folder) / file_name
            if path.is_symlink() or not sign_macos.is_macho(path):
                continue
            found += [(str(path.relative_to(app)), value) for value in load_commands(path)["rpaths"]
                      if homebrew_rpath(value)]
    return found


def _version(text):
    return tuple(int(part) for part in text.split("."))


def above_minimum(app):
    """[(file, minos)] for every Mach-O in the bundle that needs a newer macOS than Info.plist's
    LSMinimumSystemVersion, or ("no macOS version found") for one whose version otool cannot read; [("Contents/Info.plist", "no LSMinimumSystemVersion")] when the key is missing."""
    import plistlib
    import sign_macos
    declared = plistlib.loads((Path(app) / "Contents" / "Info.plist").read_bytes()).get("LSMinimumSystemVersion")
    if not declared:
        return [("Contents/Info.plist", "no LSMinimumSystemVersion")]
    newer = []
    for folder, _, files in os.walk(app):
        for file_name in sorted(files):
            path = Path(folder) / file_name
            if path.is_symlink() or not sign_macos.is_macho(path):
                continue
            versions = load_commands(path)["minos"]
            if not versions:   # otool could not read it, or it has no version load command (M9)
                newer.append((str(path.relative_to(app)), "no macOS version found"))
                continue
            needed = [value for value in versions if _version(value) > _version(declared)]
            if needed:
                newer.append((str(path.relative_to(app)), max(needed, key=_version)))
    return newer


def check(app, arch, verbose=True):
    """The gate (module docstring). Raises SystemExit on any failure; returns the scan's explained list."""
    app = Path(app)
    codesign_verified(app)
    emptied = net_check(app, arch)
    if not emptied.get("ok") or emptied.get("trust") != "truststore":
        raise SystemExit("release TLS gate: HTTPS failed with the default certificate locations empty: %s" % emptied)
    print("release TLS gate: net-check with empty certificate locations: OK (HTTP %s, trust %s)"
          % (emptied["status"], emptied["trust"]))
    fallback = net_check(app, arch, skip_truststore=True)
    if not fallback_ok(fallback, app):
        raise SystemExit("release TLS gate: HTTPS failed through the certifi fallback (truststore skipped): %s"
                         % fallback)
    print("release TLS gate: net-check with only the bundled certifi file: OK (HTTP %s, trust %s)"
          % (fallback["status"], fallback["trust"]))
    explained, unexplained = scan(app)
    if verbose:
        for relative, token, reason in explained:
            print("  harmless: %s: %s (%s)" % (relative, token, reason))
    if unexplained:
        raise SystemExit("release TLS gate: build-machine paths that may matter at run time:\n"
                         + "\n".join("  %s: %s (%s)" % item for item in unexplained))
    print("release TLS gate: %d build-machine path occurrences, all documented as harmless" % len(explained))
    rpaths = homebrew_rpaths(app)
    if rpaths:
        raise SystemExit("release gate: LC_RPATH entries naming a Homebrew folder:\n"
                         + "\n".join("  %s: %s" % item for item in rpaths))
    newer = above_minimum(app)
    if newer:
        raise SystemExit("release gate: Mach-O files that need a newer macOS than LSMinimumSystemVersion:\n"
                         + "\n".join("  %s: %s" % item for item in newer))
    print("release gate: no Mach-O needs a newer macOS than LSMinimumSystemVersion")
    return explained


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("app", type=Path)
    parser.add_argument("--arch", choices=("arm64", "x86_64"))
    parser.add_argument("--quiet", action="store_true", help="do not list the harmless occurrences")
    args = parser.parse_args(argv)
    import build_macos
    check(args.app.expanduser().resolve(), args.arch or build_macos.host_arch(), verbose=not args.quiet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
