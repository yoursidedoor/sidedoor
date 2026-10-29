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
                mock.patch.object(sign_macos, "verify"), mock.patch.object(sign_macos.subprocess, "run"), \
                mock.patch.object(sign_macos, "build_rpaths", return_value=[]):
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


OTOOL = """x.so:
Load command 0
      cmd LC_BUILD_VERSION
  cmdsize 32
 platform 1
    minos %s
      sdk 15.0
Load command 1
          cmd LC_RPATH
      cmdsize 32
         path /opt/homebrew/lib (offset 12)
Load command 2
          cmd LC_RPATH
      cmdsize 32
         path @loader_path/../lib (offset 12)
Load command 3
          cmd LC_RPATH
      cmdsize 32
         path /Users/runner/work/Pillow/Pillow/build/deps/darwin/lib (offset 12)
Load command 4
      cmd LC_VERSION_MIN_MACOSX
  cmdsize 16
  version 10.13
      sdk 15.0
"""


class MacOSMinimumTests(unittest.TestCase):
    """E3, E4, E6, E9: no Homebrew build Python; LSMinimumSystemVersion declared and no Mach-O above it;
    build-machine LC_RPATHs deleted before signing."""

    def setUp(self):
        import plistlib
        import check_release_tls
        self.check_release_tls = check_release_tls
        self.app = Path(tempfile.mkdtemp()) / "Sidedoor.app"
        self.addCleanup(shutil.rmtree, self.app.parent, True)
        (self.app / "Contents" / "MacOS").mkdir(parents=True)
        (self.app / "Contents" / "MacOS" / "sidedoor").write_bytes(thin(ARM))
        (self.app / "Contents" / "MacOS" / "notes.txt").write_text("not code")
        (self.app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(
            {"CFBundleExecutable": "sidedoor", "LSMinimumSystemVersion": build_macos.MINIMUM_MACOS}))

    def otool(self, minos):
        from unittest import mock
        result = mock.Mock(stdout=OTOOL % minos, returncode=0)
        return mock.patch.object(self.check_release_tls.subprocess, "run", return_value=result)

    def test_load_commands_read_minos_from_both_commands(self):
        with self.otool("15.0"):
            found = self.check_release_tls.load_commands(self.app / "Contents" / "MacOS" / "sidedoor")
        self.assertEqual(found["minos"], ["15.0", "10.13"])
        self.assertEqual(found["rpaths"], ["/opt/homebrew/lib", "@loader_path/../lib",
                                           "/Users/runner/work/Pillow/Pillow/build/deps/darwin/lib"])

    def test_a_mach_o_above_the_declared_minimum_fails(self):
        with self.otool("15.0"):
            self.assertEqual(self.check_release_tls.above_minimum(self.app), [("Contents/MacOS/sidedoor", "15.0")])
        with self.otool("11.0"):
            self.assertEqual(self.check_release_tls.above_minimum(self.app), [])
        with self.otool("13.5"):
            self.assertEqual(self.check_release_tls.above_minimum(self.app), [])

    def test_a_homebrew_rpath_left_in_the_app_fails_the_gate(self):
        with self.otool("11.0"):
            self.assertEqual(self.check_release_tls.homebrew_rpaths(self.app),
                             [("Contents/MacOS/sidedoor", "/opt/homebrew/lib")])

    def test_a_mach_o_with_no_version_fails(self):
        from unittest import mock
        with mock.patch.object(self.check_release_tls.subprocess, "run", return_value=mock.Mock(stdout="", returncode=1)):
            self.assertEqual(self.check_release_tls.above_minimum(self.app),
                             [("Contents/MacOS/sidedoor", "no macOS version found")])

    def test_no_declared_minimum_fails(self):
        import plistlib
        (self.app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleExecutable": "sidedoor"}))
        with self.otool("11.0"):
            self.assertEqual(self.check_release_tls.above_minimum(self.app),
                             [("Contents/Info.plist", "no LSMinimumSystemVersion")])

    def test_minimum_is_node_s_and_goes_into_info_plist(self):
        # Node v24.22.1 needs 13.5, the highest of the bundled pieces (E4).
        from unittest import mock
        self.assertEqual(build_macos.MINIMUM_MACOS, "13.5")
        with mock.patch.object(build_macos, "foreign_imports", return_value=[]):
            command = build_macos.nuitka_command(self.app.parent, self.app.parent / "x.icns", arch=build_macos.host_arch())
        self.assertIn("--macos-app-macos-min-version=13.5", command)

    def test_only_python_org_s_framework_build_is_accepted(self):
        from unittest import mock

        def configured(framework, prefix):
            return mock.patch.object(build_macos.sysconfig, "get_config_var",
                                     lambda name: {"PYTHONFRAMEWORK": framework, "prefix": prefix}.get(name))
        refused = (("Python", "/opt/homebrew/opt/python@3.13/Frameworks/Python.framework/Versions/3.13"),   # Homebrew
                   ("Python", "/usr/local/Cellar/python@3.13/3.13.12/Frameworks/Python.framework/Versions/3.13"),
                   ("", "/Users/someone/.pyenv/versions/3.13.17"),                                     # pyenv
                   ("Python", "/opt/local/Library/Frameworks/Python.framework/Versions/3.13"),          # MacPorts
                   ("", "/Users/someone/miniconda3"),                                                  # conda
                   ("", "/Users/someone/.local/share/uv/python/cpython-3.13.17-macos-aarch64-none"),    # uv
                   ("", "/Library/Frameworks/Python.framework/Versions/3.13"))                         # not a framework build
        for framework, prefix in refused:
            with configured(framework, prefix), self.assertRaises(SystemExit):
                build_macos.refuse_non_python_org()
        with configured("Python", "/Library/Frameworks/Python.framework/Versions/3.13"):
            build_macos.refuse_non_python_org()

    def test_build_machine_rpaths_are_deleted_before_signing(self):
        from unittest import mock
        calls = []

        def run(command, **_):
            if command[0] == "otool":
                return mock.Mock(stdout=OTOOL % "11.0", returncode=0)
            calls.append(command)
            return mock.Mock(returncode=0)
        with mock.patch.object(sign_macos.subprocess, "run", run):
            self.assertEqual(sign_macos.strip_build_rpaths(self.app), 2)
        binary = str(self.app / "Contents" / "MacOS" / "sidedoor")
        self.assertEqual(calls, [["install_name_tool", "-delete_rpath", "/opt/homebrew/lib", binary],
                                 ["install_name_tool", "-delete_rpath",
                                  "/Users/runner/work/Pillow/Pillow/build/deps/darwin/lib", binary]])


if __name__ == "__main__":
    unittest.main()
