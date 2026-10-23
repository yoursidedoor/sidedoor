"""E9.T1/E9.T2 Windows: tools/build_windows.py's command lines (Nuitka, signtool with Azure Trusted Signing)
and its PE architecture check. Nothing here compiles, signs or runs a built program."""
import profile_home  # noqa: F401
import json
import shutil
import struct
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import build_windows  # noqa: E402


def pe(machine):
    """A minimal PE header: MZ, e_lfanew at 60, PE\\0\\0, machine."""
    head = bytearray(b"MZ" + b"\0" * 62)
    head[60:64] = struct.pack("<I", 64)
    return bytes(head) + b"PE\0\0" + struct.pack("<H", machine) + b"\0" * 32


class NuitkaCommandTests(unittest.TestCase):
    def command(self, **kwargs):
        with mock.patch.object(build_windows.build_macos, "foreign_imports", lambda package: ["queue"]):
            return build_windows.nuitka_command(Path("C:/out"), Path("C:/t/sidedoor.ico"), python="py.exe", **kwargs)

    def test_utf8_windowed_icon_and_data(self):
        command = self.command()
        self.assertEqual(command[:5], ["py.exe", "-X", "utf8", "-m", "nuitka"])
        for flag in ("--standalone", "--windows-console-mode=attach", "--windows-icon-from-ico=" + str(Path("C:/t/sidedoor.ico")),
                     "--output-filename=sidedoor.exe", "--include-package=tzdata", "--include-package-data=tzdata",
                     "--nofollow-import-to=pystray", "--nofollow-import-to=fpdf", "--no-deployment-flag=excluded-module-usage",
                     "--zig", "--include-module=orchestrator", "--include-module=uvicorn.loops.auto",
                     "--include-module=queue"):
            self.assertIn(flag, command)
        self.assertIn("--windows-file-version=%s.0" % build_windows.version.current(), command)
        self.assertTrue(any(item.startswith("--include-data-dir=") and item.endswith("=dashboard_assets") for item in command))
        self.assertFalse(any(item.startswith("--macos") or item == "arch" for item in command))
        self.assertEqual(command[-1], str(build_windows.ENTRY))

    def test_each_build_gets_an_empty_zig_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            stale = Path(folder) / build_windows.ZIG_LOCAL_CACHE / "h" / "old.txt"
            stale.parent.mkdir(parents=True)
            stale.write_text("old")
            environment = build_windows.nuitka_environment(Path(folder))
            self.assertEqual(environment["ZIG_LOCAL_CACHE_DIR"], str(Path(folder) / build_windows.ZIG_LOCAL_CACHE))
            self.assertEqual(list(Path(environment["ZIG_LOCAL_CACHE_DIR"]).iterdir()), [])

    def test_a_stale_constants_blob_fails_the_build(self):
        with tempfile.TemporaryDirectory() as folder:
            exe, blob = Path(folder) / "sidedoor.exe", Path(folder) / "__constant.bin"
            blob.write_bytes(b"constants of this build")
            exe.write_bytes(b"MZ...constants of an older build...")
            with self.assertRaises(SystemExit):
                build_windows.check_constants(exe, blob)
            exe.write_bytes(b"MZ...constants of this build...")
            build_windows.check_constants(exe, blob)

    def test_msvc_and_jobs(self):
        command = self.command(compiler="msvc", jobs=4)
        self.assertNotIn("--zig", command)
        self.assertIn("--jobs=4", command)


class ArchTests(unittest.TestCase):
    def setUp(self):
        self.app = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.app, True)
        (self.app / "sidedoor.exe").write_bytes(pe(0x8664))
        (self.app / "prompt.md").write_text("MZ not code")

    def test_machines_read_from_headers(self):
        self.assertEqual(build_windows.pe_machine(self.app / "sidedoor.exe"), "x64")
        (self.app / "a.pyd").write_bytes(pe(0xAA64))
        self.assertEqual(build_windows.pe_machine(self.app / "a.pyd"), "arm64")
        self.assertIsNone(build_windows.pe_machine(self.app / "prompt.md"))

    def test_other_arch_fails(self):
        self.assertEqual(build_windows.check_arch(self.app, "x64"), 1)
        (self.app / "browser-runtime").mkdir()
        (self.app / "browser-runtime" / "addon.node").write_bytes(pe(0xAA64))
        with self.assertRaises(SystemExit) as caught:
            build_windows.check_arch(self.app, "x64")
        self.assertIn("addon.node", str(caught.exception))


class SigningTests(unittest.TestCase):
    ARGS = types.SimpleNamespace(sign="azure", endpoint="https://eus.codesigning.azure.net", account="sidedoor",
                                 certificate_profile="public", signtool=None, dlib=r"C:\ts\Azure.CodeSigning.Dlib.dll")

    def test_command_uses_the_dlib_and_metadata(self):
        settings = build_windows.sign_settings(self.ARGS, environ={}, defaults_file=None)
        self.assertEqual(build_windows.signing_metadata(settings), {
            "Endpoint": "https://eus.codesigning.azure.net", "CodeSigningAccountName": "sidedoor",
            "CertificateProfileName": "public"})
        self.assertEqual(build_windows.sign_command(settings, Path("m.json"), [Path("a.exe"), Path("b.pyd")]), [
            "signtool.exe", "sign", "/v", "/fd", "SHA256", "/tr", "http://timestamp.acs.microsoft.com", "/td", "SHA256",
            "/dlib", r"C:\ts\Azure.CodeSigning.Dlib.dll", "/dmdf", "m.json", "a.exe", "b.pyd"])

    def test_settings_from_environment_and_missing(self):
        empty = types.SimpleNamespace(sign="azure", endpoint=None, account=None, certificate_profile=None, signtool=None, dlib=None)
        settings = build_windows.sign_settings(empty, environ={
            "AZURE_TRUSTED_SIGNING_ENDPOINT": "https://e", "AZURE_TRUSTED_SIGNING_ACCOUNT": "a",
            "AZURE_TRUSTED_SIGNING_CERTIFICATE_PROFILE": "p", "AZURE_TRUSTED_SIGNING_DLIB": "d.dll",
            "SIGNTOOL": r"C:\sdk\signtool.exe"})
        self.assertEqual((settings["endpoint"], settings["signtool"]), ("https://e", r"C:\sdk\signtool.exe"))
        with self.assertRaises(SystemExit) as caught:
            build_windows.sign_settings(empty, environ={"AZURE_TRUSTED_SIGNING_ENDPOINT": "https://e"}, defaults_file=None)
        self.assertIn("--account", str(caught.exception))
        self.assertIn("--dlib", str(caught.exception))

    def test_release_defaults_come_from_the_checked_in_file(self):
        args = build_windows.parser().parse_args(["--output", "o", "--runtime", "r", "--node", "n", "--sign", "azure",
                                                  "--dlib", "d.dll"])
        settings = build_windows.sign_settings(args, environ={})
        metadata = build_windows.signing_metadata(settings)
        self.assertEqual({key: metadata[key] for key in ("Endpoint", "CodeSigningAccountName", "CertificateProfileName")}, {
            "Endpoint": "https://eus.codesigning.azure.net/", "CodeSigningAccountName": "sidedoorsigning",
            "CertificateProfileName": "sidedoor"})
        self.assertNotIn("AzureCliCredential", metadata["ExcludeCredentials"])
        self.assertIn("ManagedIdentityCredential", metadata["ExcludeCredentials"])
        with self.assertRaises(SystemExit) as caught:   # the dlib's place is the machine's, never a default
            build_windows.sign_settings(types.SimpleNamespace(sign="azure"), environ={})
        self.assertIn("--dlib", str(caught.exception))

    def test_unsigned_by_default(self):
        args = build_windows.parser().parse_args(["--output", "o", "--runtime", "r", "--node", "n"])
        self.assertIsNone(args.sign)
        with self.assertRaises(SystemExit):   # no bare --sign: a mode is named
            build_windows.parser().parse_args(["--output", "o", "--runtime", "r", "--node", "n", "--sign"])

    def test_ssl_mode_selects_the_certificate(self):
        args = build_windows.parser().parse_args(["--output", "o", "--runtime", "r", "--node", "n", "--sign", "ssl",
                                                  "--cert-subject", "Sidedoor Inc"])
        settings = build_windows.sign_settings(args, environ={})
        self.assertEqual(settings["mode"], "ssl")
        self.assertIsNone(build_windows.metadata_file(settings, "/nonexistent"))
        self.assertEqual(build_windows.sign_command(settings, None, [Path("a.exe")]), [
            "signtool.exe", "sign", "/fd", "SHA256", "/tr", "http://ts.ssl.com", "/td", "SHA256",
            "/n", "Sidedoor Inc", "a.exe"])
        by_thumbprint = build_windows.sign_settings(types.SimpleNamespace(sign="ssl"), environ={
            "SIDEDOOR_SIGN_SHA1": "0123456789abcdef0123456789abcdef01234567", "SIGNTOOL": "C:/sdk/signtool.exe"})
        self.assertEqual(build_windows.sign_command(by_thumbprint, None, [Path("a.exe")])[-3:],
                         ["/sha1", "0123456789abcdef0123456789abcdef01234567", "a.exe"])
        self.assertEqual(build_windows.verify_command(by_thumbprint, Path("a.exe")),
                         ["C:/sdk/signtool.exe", "verify", "/pa", "/tw", "a.exe"])
        self.assertEqual(build_windows.inno_sign_tool(settings, None),
                         "$qsigntool.exe$q sign /fd SHA256 /tr http://ts.ssl.com /td SHA256 /n $qSidedoor Inc$q $f")

    def test_ssl_needs_exactly_one_certificate_choice(self):
        for environ in ({}, {"SIDEDOOR_SIGN_SUBJECT": "S", "SIDEDOOR_SIGN_SHA1": "ab"}):
            with self.assertRaises(SystemExit) as caught:
                build_windows.sign_settings(types.SimpleNamespace(sign="ssl"), environ=environ)
            self.assertIn("--cert-subject", str(caught.exception))

    def test_ssl_signing_carries_no_secret(self):
        settings = build_windows.sign_settings(types.SimpleNamespace(sign="ssl", cert_subject="Sidedoor Inc"), environ={
            "SIDEDOOR_SIGN_SUBJECT": "x", "ES_PASSWORD": "hunter2", "ES_TOTP_SECRET": "totp"})
        command = " ".join(build_windows.sign_command(settings, None, [Path("a.exe")]))
        for secret in ("hunter2", "totp", "/p ", "/csp", "/k"):
            self.assertNotIn(secret, command)

    def app_with(self, signed_names):
        app = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, app, True)
        (app / "browser-runtime" / "node").mkdir(parents=True)
        names = ["sidedoor.exe", "sidedoor-cli.exe", "python%d%d.dll" % sys.version_info[:2], "x.pyd",
                 "browser-runtime/node/node.exe"]
        for name in names:
            (app / name).write_bytes(pe(0x8664))
        return app, {str(app / name) for name in signed_names}

    def fake_run(self, signed, calls):
        def settings_of(command):
            return build_windows.sign_settings(self.ARGS, environ={})

        def run(command, **kwargs):
            calls.append(command)
            if command[1] == "sign":
                if "/dmdf" in command:
                    metadata = json.loads(Path(command[command.index("/dmdf") + 1]).read_text())
                    self.assertEqual(metadata["CodeSigningAccountName"], "sidedoor")
                signed.update(command[len(build_windows.sign_command(settings_of(command), None, [])):])
                return types.SimpleNamespace(returncode=0)
            self.assertEqual(command[1:4], ["verify", "/pa", "/tw"])
            return types.SimpleNamespace(returncode=0 if command[-1] in signed else 1)
        return run

    def test_sign_only_unsigned_files_then_verify_all(self):
        python = "python%d%d.dll" % sys.version_info[:2]
        app, signed = self.app_with([python, "browser-runtime/node/node.exe"])
        calls = []
        settings = build_windows.sign_settings(self.ARGS, environ={})
        done = build_windows.sign(app, settings, run=self.fake_run(signed, calls))
        self.assertEqual(sorted(path.name for path in done), ["sidedoor-cli.exe", "sidedoor.exe", "x.pyd"])
        signs = [command for command in calls if command[1] == "sign"]
        self.assertEqual(len(signs), 1)
        self.assertNotIn(str(app / python), signs[0])

    def test_unsigned_python_or_node_stops_without_signing(self):
        for missing in ("python%d%d.dll" % sys.version_info[:2], "browser-runtime/node/node.exe"):
            others = {"python%d%d.dll" % sys.version_info[:2], "browser-runtime/node/node.exe"} - {missing}
            app, signed = self.app_with(others)
            calls = []
            with self.assertRaises(SystemExit) as caught:
                build_windows.sign(app, build_windows.sign_settings(self.ARGS, environ={}), run=self.fake_run(signed, calls))
            self.assertIn(Path(missing).name, str(caught.exception))
            self.assertFalse([command for command in calls if command[1] == "sign"])

    def test_batches_stay_under_the_command_line_limit(self):
        settings = build_windows.sign_settings(self.ARGS, environ={})
        files = [Path("D:/Builds/A rather long folder name/Sidedoor build/Sidedoor/pkg%03d/module_with_a_long_name.pyd" % i)
                 for i in range(400)]
        batches = build_windows.sign_batches(settings, Path("C:/t/metadata.json"), files)
        self.assertGreater(len(batches), 1)
        self.assertEqual([path for batch in batches for path in batch], files)
        for batch in batches:
            self.assertLessEqual(build_windows.command_length(
                build_windows.sign_command(settings, Path("C:/t/metadata.json"), batch)), build_windows.SIGN_COMMAND_LIMIT)

    def test_sign_flag_without_settings_stops_before_building(self):
        with mock.patch.dict(build_windows.os.environ, {}, clear=True), \
                mock.patch.object(build_windows.subprocess, "run") as run, self.assertRaises(SystemExit):
            build_windows.main(["--output", "out", "--runtime", "r", "--node", "n", "--sign", "azure"])
        run.assert_not_called()


class CliAndInstallerTests(unittest.TestCase):
    ARGS = SigningTests.ARGS

    def test_cli_launcher_command(self):
        self.assertEqual(build_windows.zig_command(Path("C:/out/Sidedoor"), "x64", python="py.exe"), [
            "py.exe", "-m", "ziglang", "cc", "-target", "x86_64-windows-gnu", "-O2", "-s",
            "-o", str(Path("C:/out/Sidedoor") / "sidedoor-cli.exe"), str(build_windows.LAUNCHER)])

    def test_installer_command_unsigned_and_signed(self):
        plain = build_windows.installer_command(Path("C:/iscc/ISCC.exe"), Path("C:/out/Sidedoor"), Path("C:/out"),
                                                "x64", Path("C:/out/sidedoor.ico"))
        self.assertEqual(plain[0], str(Path("C:/iscc/ISCC.exe")))
        self.assertIn("/DAppVersion=" + build_windows.version.current(), plain)
        self.assertIn("/DOutputName=Sidedoor-windows-x64-setup", plain)
        self.assertEqual(plain[-1], str(build_windows.ISS))
        self.assertFalse([item for item in plain if item.startswith(("/DSign", "/S"))])
        settings = build_windows.sign_settings(self.ARGS, environ={})
        signed = build_windows.installer_command(Path("ISCC.exe"), Path("app"), Path("out"), "x64", Path("i.ico"),
                                                 settings, Path("C:/t/metadata.json"))
        self.assertIn("/DSign", signed)
        self.assertIn("/Ssidedoor=$qsigntool.exe$q sign /v /fd SHA256 /tr http://timestamp.acs.microsoft.com /td SHA256 "
                      "/dlib $qC:\\ts\\Azure.CodeSigning.Dlib.dll$q /dmdf $q%s$q $f" % Path("C:/t/metadata.json"), signed)

    def test_iss_is_per_user_with_no_login_entry(self):
        text = build_windows.ISS.read_text()
        for line in ("PrivilegesRequired=lowest", "DefaultDirName={userpf}\\Sidedoor", "SignTool=sidedoor",
                     "SignedUninstaller=yes", 'Name: "{userprograms}\\Sidedoor"'):
            self.assertIn(line, text)
        lowered = text.lower()
        for absent in ("[uninstalldelete]", "{userstartup}", "schtasks", "[registry]"):
            self.assertNotIn(absent, lowered)


class SmokeCheckTests(unittest.TestCase):
    def test_program_check_compares_files(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        exe, other = root / "sidedoor.exe", root / "other.exe"
        exe.write_bytes(b"x")
        other.write_bytes(b"y")
        line = {"App location": {"line": "Fine for start at login (Sidedoor runs from %s)." % exe}}
        self.assertTrue(build_windows.program_check(line, exe))
        with self.assertRaises(SystemExit):
            build_windows.program_check(line, other)
        with self.assertRaises(SystemExit):
            build_windows.program_check({"App location": {"line": "Fine for start at login."}}, exe)


class PackTests(unittest.TestCase):
    def test_zip_holds_the_sidedoor_folder_and_a_checksum(self):
        import zipfile
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        app = root / "Sidedoor"
        (app / "fpdf").mkdir(parents=True)
        (app / "sidedoor.exe").write_bytes(pe(0x8664))
        (app / "fpdf" / "__init__.py").write_text("")
        archive, digest = build_windows.pack(app, root, "x64")
        self.assertEqual(archive.name, "Sidedoor-windows-x64.zip")
        self.assertEqual(sorted(zipfile.ZipFile(archive).namelist()), ["Sidedoor/fpdf/__init__.py", "Sidedoor/sidedoor.exe"])
        self.assertEqual((root / "Sidedoor-windows-x64.zip.sha256").read_text(), "%s  Sidedoor-windows-x64.zip\n" % digest)


if __name__ == "__main__":
    unittest.main()
