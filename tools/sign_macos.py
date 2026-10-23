"""Sign, notarize and staple Sidedoor.app, and pack the release zip (E9.T2).

    python3 tools/sign_macos.py <path>/Sidedoor.app --sign "Developer ID Application: <name> (<team>)"
        [--notarize <notarytool keychain profile>] [--release-dir <folder>]

tools/build_macos.py calls it last (its --sign and --notarize options), after every copy into the
bundle (browser runtime, pystray/fpdf sources, licences) and after its smoke run, so nothing changes
the bundle once it is sealed. Standalone it signs an already built app.

Contents/MacOS is a code location: codesign takes every file there as code and refuses a data file
("code object is not signed at all ... research-prompt.md"). So first every data file and every folder
holding no Mach-O under Contents/MacOS (prompts, schemas, dashboard assets, the pystray/ and fpdf/
sources, the browser runtime's JavaScript and licence) moves to the same place under
Contents/Resources, and a relative symlink is left where it was: the program finds every path as
before, the Mach-O files (the .so modules, the dylibs, Node) stay in Contents/MacOS.

Signing is inside-out, with the hardened runtime (--options runtime) and a secure timestamp:
  1. every Mach-O in the bundle other than the main executable (the .so extension modules, the
     .dylibs, Contents/MacOS/Python, the bundled Node binary), deepest path first. Node is signed
     with tools/entitlements.plist (com.apple.security.cs.allow-jit: V8's JIT);
  2. the bundle itself, which signs Contents/MacOS/sidedoor (its CFBundleExecutable) and seals every
     other file (the uncompiled pystray/ and fpdf/ sources, prompts, the runtime's JavaScript and the
     licences) in Contents/_CodeSignature/CodeResources. On an Intel (x86_64) app the main executable
     gets tools/entitlements-x86_64.plist (com.apple.security.cs.allow-unsigned-executable-memory:
     x86_64 libffi, used by PyObjC for the tray, allocates writable+executable closures, and without it
     `import AppKit` hangs in ffi_closure_alloc). An arm64 app's main executable gets no entitlement.
Library validation stays on (no disable-library-validation): every library the binary loads from the
bundle is signed by the same team. Then `codesign --verify --deep --strict` must pass.

Notarizing zips the app with ditto, submits it with `xcrun notarytool submit --keychain-profile
<profile> --wait` (the profile is the owner's, saved with `notarytool store-credentials`; no password
is read or printed here), prints the notary log on anything but Accepted, staples the ticket,
validates it and asks Gatekeeper (`spctl --assess --type execute`, which does not launch the app).
The release zip (Sidedoor-mac-apple-silicon.zip for Apple silicon, Sidedoor-mac-intel.zip for Intel,
named from the main executable's architecture; made from the stapled app) and its .sha256 go into
--release-dir (default: beside the app). The names carry no version (owner: the website downloads from
GitHub's "latest release" asset URLs); the version is in the release title and tag and inside the app.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import struct
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]
ENTITLEMENTS = REPO / "tools" / "entitlements.plist"
# The main executable's entitlements per architecture (None: none). x86_64 libffi (PyObjC, the tray)
# needs writable+executable memory; arm64 needs nothing, so its posture is unchanged.
MAIN_ENTITLEMENTS = {"x86_64": REPO / "tools" / "entitlements-x86_64.plist", "arm64": None}
# Executables that get ENTITLEMENTS, relative to Contents/MacOS.
JIT_EXECUTABLES = ("browser-runtime/node/bin/node",)
THIN_MAGIC = {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce"}
FAT_MAGIC = {b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"}


def is_macho(path):
    """True for a thin or universal Mach-O file (a Java class file shares the universal magic; its
    second word is a class-file version, far above any real architecture count)."""
    with open(path, "rb") as handle:
        head = handle.read(8)
    if head[:4] in THIN_MAGIC:
        return True
    return head[:4] in FAT_MAGIC and len(head) == 8 and 0 < struct.unpack(">I", head[4:])[0] < 32


def main_executable(app):
    info = plistlib.loads((Path(app) / "Contents" / "Info.plist").read_bytes())
    return Path(app) / "Contents" / "MacOS" / info["CFBundleExecutable"]


def nested_code(app):
    """Every Mach-O file in the bundle except the main executable (symlinks skipped: they are sealed
    as links), deepest path first, then by name."""
    main = main_executable(app)
    found = []
    for folder, _, files in os.walk(app):
        for name in files:
            path = Path(folder) / name
            if not path.is_symlink() and path != main and is_macho(path):
                found.append(path)
    return sorted(found, key=lambda path: (-len(path.parts), str(path)))


def codesign(identity, paths, entitlements=None):
    command = ["codesign", "--force", "--options", "runtime", "--timestamp", "--sign", identity]
    if entitlements:
        command += ["--entitlements", str(entitlements)]
    result = subprocess.run(command + [str(path) for path in paths], capture_output=True, text=True)
    if result.returncode:
        message = result.stderr.strip()
        if "errSecInternalComponent" in message or "canceled" in message.lower():
            message += "\n(the keychain refused codesign the use of the signing key: the access dialog was denied)"
        raise SystemExit("codesign failed: " + message)


def outside_links(app):
    """Symlinks in the bundle that point outside it (codesign refuses to seal them)."""
    root = Path(app).resolve()
    bad = []
    for folder, dirs, files in os.walk(app):
        for name in dirs + files:
            path = Path(folder) / name
            if path.is_symlink():
                target = (path.parent / os.readlink(path)).resolve()
                if root not in target.parents:
                    bad.append(str(path))
    return bad


def holds_macho(folder):
    for parent, _, files in os.walk(folder):
        for name in files:
            path = Path(parent) / name
            if not path.is_symlink() and is_macho(path):
                return True
    return False


def relocate_data(app):
    """Move the data under Contents/MacOS to Contents/Resources, leaving relative symlinks. Returns
    the moved paths (relative to Contents/MacOS)."""
    macos, resources = Path(app) / "Contents" / "MacOS", Path(app) / "Contents" / "Resources"
    moved = []

    def walk(folder):
        for entry in sorted(folder.iterdir()):
            if entry.is_symlink():
                continue
            if entry.is_dir() and holds_macho(entry):
                walk(entry)
                continue
            if entry.is_file() and is_macho(entry):
                continue
            relative = entry.relative_to(macos)
            target = resources / relative
            if target.exists() or target.is_symlink():
                raise SystemExit("cannot move %s: %s already exists" % (relative, target))
            target.parent.mkdir(parents=True, exist_ok=True)
            entry.rename(target)
            entry.symlink_to(os.path.relpath(target, entry.parent))
            moved.append(str(relative))

    walk(macos)
    return moved


def sign(app, identity):
    app = Path(app)
    moved = relocate_data(app)
    if moved:
        print("moved %d data entries from Contents/MacOS to Contents/Resources (symlinked): %s"
              % (len(moved), ", ".join(moved)))
    bad = outside_links(app)
    if bad:
        raise SystemExit("symlinks leaving the bundle: " + ", ".join(bad))
    # Extended attributes (quarantine, Finder info) are refused inside a signature.
    subprocess.run(["xattr", "-cr", str(app)], check=True)
    code = nested_code(app)
    macos = app / "Contents" / "MacOS"
    jit = {macos / name for name in JIT_EXECUTABLES}
    missing = jit - set(code)
    if missing:
        raise SystemExit("expected executable not in the bundle: " + ", ".join(map(str, missing)))
    print("signing %d nested Mach-O files, then the bundle:" % len(code))
    for path in code:
        print("  %s%s" % (path.relative_to(app), "  (entitlements: allow-jit)" if path in jit else ""))
    # Deepest first; files at the same depth do not contain one another, so they go in one call.
    for depth in sorted({len(path.parts) for path in code}, reverse=True):
        level = [path for path in code if len(path.parts) == depth]
        plain = [path for path in level if path not in jit]
        if plain:
            codesign(identity, plain)
        for path in level:
            if path in jit:
                codesign(identity, [path], ENTITLEMENTS)
    main_entitlements = MAIN_ENTITLEMENTS[app_arch(app)]
    if main_entitlements:
        print("  %s  (entitlements: %s)" % (main_executable(app).relative_to(app), main_entitlements.name))
    codesign(identity, [app], main_entitlements)
    verify(app)


def verify(app):
    result = subprocess.run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)],
                            capture_output=True, text=True)
    print(result.stderr.strip())
    if result.returncode:
        raise SystemExit("codesign --verify failed")
    unsigned = []
    for path in nested_code(app) + [main_executable(app)]:
        detail = subprocess.run(["codesign", "-dvv", str(path)], capture_output=True, text=True).stderr
        if "flags=0x10000(runtime)" not in detail or "Timestamp=" not in detail or "Authority=Developer ID Application" not in detail:
            unsigned.append(str(path))
    if unsigned:
        raise SystemExit("not signed with Developer ID, hardened runtime and timestamp: " + ", ".join(unsigned))


def run(command, **options):
    print("$ " + " ".join(command))
    return subprocess.run(command, capture_output=True, text=True, **options)


def notarize(app, profile):
    """Submit, wait, staple, validate, assess. Returns the submission id."""
    app = Path(app)
    with tempfile.TemporaryDirectory(prefix="sidedoor-notarize-") as scratch:
        upload = Path(scratch) / (app.stem + ".zip")
        subprocess.run(["ditto", "-c", "-k", "--keepParent", str(app), str(upload)], check=True)
        result = run(["xcrun", "notarytool", "submit", str(upload), "--keychain-profile", profile,
                      "--wait", "--output-format", "json"])
    try:
        answer = json.loads(result.stdout)
    except ValueError:
        raise SystemExit("notarytool submit failed: " + (result.stderr or result.stdout).strip())
    submission, status = answer.get("id"), answer.get("status")
    print("notarization %s: %s" % (submission, status))
    if status != "Accepted":
        if submission:
            log = run(["xcrun", "notarytool", "log", submission, "--keychain-profile", profile])
            print(log.stdout or log.stderr)
        raise SystemExit("notarization was not accepted (%s)" % status)
    for command in (["xcrun", "stapler", "staple", str(app)], ["xcrun", "stapler", "validate", str(app)]):
        result = run(command)
        print((result.stdout + result.stderr).strip())
        if result.returncode:
            raise SystemExit(" ".join(command[:3]) + " failed")
    verify(app)
    result = run(["spctl", "--assess", "--type", "execute", "--verbose", str(app)])
    print(result.stderr.strip())
    if result.returncode or "source=Notarized Developer ID" not in result.stderr:
        raise SystemExit("Gatekeeper does not accept the app as notarized")
    return submission


# The download each architecture is published as (owner, IN1 a: Apple silicon and Intel), fixed names so
# releases/latest/download/<name> links keep working (the website links to them by name).
ASSET_NAMES = {"arm64": "Sidedoor-mac-apple-silicon.zip", "x86_64": "Sidedoor-mac-intel.zip"}


def app_arch(app):
    """The main executable's one architecture (a universal or unknown one is refused: the release has one
    download per architecture). An Intel build made under Rosetta is named from what it is, not the Mac."""
    sys.path.insert(0, str(REPO / "tools"))
    import build_macos
    arches = build_macos.macho_arches(main_executable(app))
    if len(arches) != 1 or arches[0] not in ASSET_NAMES:
        raise SystemExit("main executable is %s; expected arm64 or x86_64 only" % (" ".join(arches) or "not a Mach-O"))
    return arches[0]


def asset_name(machine=None, app=None):
    """The release asset's fixed name for this architecture: `machine`, else the app's, else this Mac's."""
    machine = machine or (app_arch(app) if app else platform.machine())
    if machine not in ASSET_NAMES:
        raise SystemExit("no release asset name for architecture %s" % machine)
    return ASSET_NAMES[machine]


def release_zip(app, folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / asset_name(app=app)
    if target.exists():
        target.unlink()
    subprocess.run(["ditto", "-c", "-k", "--keepParent", str(app), str(target)], check=True)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    (folder / (target.name + ".sha256")).write_text("%s  %s\n" % (digest, target.name))
    print("release: %s (%.1f MB) sha256 %s" % (target, target.stat().st_size / 1e6, digest))
    return target, digest


def finish(app, identity, profile=None, release_dir=None):
    """What build_macos.py --sign/--notarize runs: sign; then, with a profile, notarize, staple and
    pack the release zip."""
    sign(app, identity)
    if profile:
        notarize(app, profile)
        release_zip(app, release_dir or Path(app).parent)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("app", type=Path)
    parser.add_argument("--sign", required=True, metavar="IDENTITY", help='"Developer ID Application: ..."')
    parser.add_argument("--notarize", metavar="PROFILE", help="notarytool keychain profile")
    parser.add_argument("--release-dir", type=Path)
    args = parser.parse_args(argv)
    if sys.platform != "darwin":
        raise SystemExit("sign_macos.py runs on macOS only")
    finish(args.app.expanduser().resolve(), args.sign, args.notarize, args.release_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
