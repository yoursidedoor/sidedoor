"""Make an extracted python.org Python.framework run from where it is, for the Mac build venvs (build machine only).

    python3 tools/relocate_framework.py <dir>/Python.framework/Versions/3.13

The Mac builds use python.org's universal2 installer (AGENTS.md, "Mac builds use python.org's Python";
version, URL and SHA-256 in tools/build-requirements-macos.txt), extracted, never installed into /Library:

    pkgutil --expand-full python-3.13.17-macos11.pkg <dir>/pkg
    cp -R <dir>/pkg/Python_Framework.pkg/Payload <dir>/Python.framework
    python3 tools/relocate_framework.py <dir>/Python.framework/Versions/3.13

Every /Library/Frameworks/Python.framework/Versions/3.13/... load command becomes @loader_path-relative and
the framework dylib's id @rpath/Python (lib/Python links to it, where Nuitka looks for @rpath); the
changed files are re-signed ad hoc (`codesign --sign -`, no keychain). lib-dynload gets links to the lib/
libraries its modules use. install_name_tool cannot write the longer absolute paths (no header padding).
sysconfig still reports the /Library/Frameworks prefix, which tools/build_macos.refuse_non_python_org
checks."""
import os, subprocess, sys
from pathlib import Path
ROOT = Path(sys.argv[1]).resolve()          # .../Python.framework/Versions/3.13
PREFIX = "/Library/Frameworks/Python.framework/Versions/3.13/"
MAGIC = {b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe"}
changed = []
for folder, _, files in os.walk(ROOT):
    for name in files:
        p = Path(folder) / name
        if p.is_symlink() or not p.is_file():
            continue
        with open(p, "rb") as h:
            if h.read(4) not in MAGIC:
                continue
        out = subprocess.run(["otool", "-L", str(p)], capture_output=True, text=True).stdout
        deps = sorted({l.split(" (")[0].strip() for l in out.splitlines()[1:] if l.strip().startswith(PREFIX)})
        own = [l.strip() for l in subprocess.run(["otool", "-D", str(p)], capture_output=True, text=True).stdout.splitlines()[1:]]
        own = own[-1] if own else None
        args = []
        for dep in deps:
            if dep == own:
                continue
            target = ROOT / dep[len(PREFIX):]
            if p.parent.name == "lib-dynload" and target.parent == ROOT / "lib":
                # An extension module's library (libssl, libcrypto): a link beside the module, so Nuitka
                # finds it inside the Python path (it drops a stdlib module's libraries from elsewhere)
                # and the module's @loader_path reference holds in the built app too.
                link = p.parent / target.name
                if not link.is_symlink():
                    link.symlink_to("../../" + target.name)
                target = link
            args += ["-change", dep, "@loader_path/" + os.path.relpath(target, p.parent)]
        if own and own.startswith(PREFIX):
            args += ["-id", "@rpath/" + Path(own).name]
        if args:
            subprocess.run(["install_name_tool"] + args + [str(p)], check=True)
            subprocess.run(["codesign", "--force", "--sign", "-", str(p)], check=True, capture_output=True)
            changed.append(str(p.relative_to(ROOT)))
link = ROOT / "lib" / "Python"
if not link.exists():
    link.symlink_to("../Python")
print("rewritten:", len(changed))
for c in changed: print(" ", c)
