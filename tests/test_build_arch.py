"""IN1 a (owner): two macOS downloads, Apple silicon and Intel. tools/build_macos.check_arch fails a build
whose bundle holds a Mach-O that is not the target architecture only; sign_macos names the zip from it,
with no version (owner: the website links GitHub's "latest release" asset URLs)."""
import profile_home  # noqa: F401
import shutil
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import build_macos  # noqa: E402
import sign_macos  # noqa: E402

X86, ARM = 0x01000007, 0x0100000C


def thin(cpu):
    return b"\xcf\xfa\xed\xfe" + struct.pack("<I", cpu) + b"\0" * 24


def fat(*cpus):
    return b"\xca\xfe\xba\xbe" + struct.pack(">I", len(cpus)) + b"".join(
        struct.pack(">IIIII", cpu, 0, 4096, 4096, 12) for cpu in cpus)


class ArchCheckTests(unittest.TestCase):
    def setUp(self):
        self.app = Path(tempfile.mkdtemp()) / "Sidedoor.app"
        self.addCleanup(shutil.rmtree, self.app.parent, True)
        (self.app / "Contents" / "MacOS").mkdir(parents=True)
        (self.app / "Contents" / "MacOS" / "sidedoor").write_bytes(thin(X86))
        (self.app / "Contents" / "MacOS" / "prompt.md").write_text("not code")

    def test_thin_target_passes(self):
        self.assertEqual(build_macos.check_arch(self.app, "x86_64"), 1)

    def test_universal_or_other_arch_fails(self):
        for data in (fat(X86, ARM), thin(ARM)):
            (self.app / "Contents" / "MacOS" / "lib.so").write_bytes(data)
            with self.assertRaises(SystemExit) as caught:
                build_macos.check_arch(self.app, "x86_64")
            self.assertIn("lib.so", str(caught.exception))

    def test_arches_read_from_headers(self):
        path = self.app / "Contents" / "MacOS" / "u"
        path.write_bytes(fat(X86, ARM))
        self.assertEqual(build_macos.macho_arches(path), ["x86_64", "arm64"])
        path.write_bytes(b"\xca\xfe\xba\xbe\x00\x00\x00\x34")   # a Java class file
        self.assertEqual(build_macos.macho_arches(path), [])

    def test_release_names_have_no_version(self):
        import plistlib
        (self.app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleExecutable": "sidedoor"}))
        main = self.app / "Contents" / "MacOS" / "sidedoor"
        for data, name in ((thin(X86), "Sidedoor-mac-intel.zip"), (thin(ARM), "Sidedoor-mac-apple-silicon.zip")):
            main.write_bytes(data)
            self.assertEqual(sign_macos.asset_name(app=self.app), name)
        main.write_bytes(fat(X86, ARM))
        with self.assertRaises(SystemExit):
            sign_macos.asset_name(app=self.app)


class MainEntitlementTests(unittest.TestCase):
    """The x86_64 main executable is signed with allow-unsigned-executable-memory (PyObjC's libffi on
    Intel hangs in `import AppKit` without it); an arm64 one with no entitlement."""

    def signed(self, cpu):
        import plistlib
        from unittest import mock
        app = Path(tempfile.mkdtemp()) / "Sidedoor.app"
        self.addCleanup(shutil.rmtree, app.parent, True)
        node = app / "Contents" / "MacOS" / "browser-runtime" / "node" / "bin"
        node.mkdir(parents=True)
        (node / "node").write_bytes(thin(cpu))
        (app / "Contents" / "MacOS" / "sidedoor").write_bytes(thin(cpu))
        (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleExecutable": "sidedoor"}))
        calls = []
        with mock.patch.object(sign_macos, "codesign", lambda identity, paths, entitlements=None:
                               calls.append((list(paths), entitlements))), \
                mock.patch.object(sign_macos, "verify"), mock.patch.object(sign_macos.subprocess, "run"):
            sign_macos.sign(app, "Developer ID Application: Test (TEAM)")
        return app, calls

    def test_x86_64_app_gets_unsigned_executable_memory(self):
        app, calls = self.signed(X86)
        self.assertEqual(calls[-1][0], [app])
        entitlements = calls[-1][1]
        self.assertEqual(entitlements.name, "entitlements-x86_64.plist")
        import plistlib
        self.assertEqual(plistlib.loads(entitlements.read_bytes()),
                         {"com.apple.security.cs.allow-unsigned-executable-memory": True})
        self.assertEqual(calls[0][1], sign_macos.ENTITLEMENTS)   # Node keeps allow-jit

    def test_arm64_app_gets_no_entitlement(self):
        app, calls = self.signed(ARM)
        self.assertEqual(calls[-1], ([app], None))


if __name__ == "__main__":
    unittest.main()
