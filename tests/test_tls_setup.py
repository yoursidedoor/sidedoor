"""HTTPS trust at process start (tls_setup, hotfix 2026-10-28), doctor's Secure connection line, and the
release gate's path scan. Offline: no test makes a network call."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import json
import ssl
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import doctor
import text_model
import tls_setup

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import check_release_tls  # noqa: E402
import check_release_tls_windows  # noqa: E402


def broken():
    raise ImportError("no truststore")


CERT_FAILURE = urllib.error.URLError(ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"))


class ConfigureTest(unittest.TestCase):
    def setUp(self):
        tls_setup.reset()
        self.addCleanup(tls_setup.reset)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.bundled = self.folder / "cacert.pem"
        self.bundled.write_text("bundled")
        self.missing = str(self.folder / "absent" / "cert.pem")

    def test_truststore_working_leaves_the_environment_alone(self):
        environ, injected = {}, []
        state = tls_setup.configure(environ, inject=lambda: injected.append(1), default_cafile=self.missing,
                                    ca_file=str(self.bundled))
        self.assertEqual((state["mode"], injected, environ), (tls_setup.TRUSTSTORE, [1], {}))

    def test_bundled_file_only_when_truststore_fails_nothing_is_set_and_the_default_is_missing(self):
        environ = {}
        state = tls_setup.configure(environ, inject=broken, default_cafile=self.missing, ca_file=str(self.bundled))
        self.assertEqual(environ, {"SSL_CERT_FILE": str(self.bundled)})
        self.assertEqual((state["mode"], state["truststore_error"]), (tls_setup.BUNDLED, "ImportError"))
        present = self.folder / "default.pem"
        present.write_text("x")
        environ = {}
        state = tls_setup.configure(environ, inject=broken, default_cafile=str(present), ca_file=str(self.bundled))
        self.assertEqual((environ, state["mode"]), ({}, tls_setup.DEFAULT))

    def test_a_user_set_ssl_cert_file_is_never_overridden(self):
        own = self.folder / "corporate.pem"
        own.write_text("mine")
        environ, injected = {"SSL_CERT_FILE": str(own)}, []
        state = tls_setup.configure(environ, inject=lambda: injected.append(1), default_cafile=self.missing,
                                    ca_file=str(self.bundled))
        self.assertEqual((state["mode"], injected, environ), (tls_setup.USER, [], {"SSL_CERT_FILE": str(own)}))

    def test_an_ssl_cert_file_naming_no_file_is_not_a_user_choice(self):
        # T11: set but naming nothing (user_choice's test): truststore failing, the bundled file replaces it.
        environ = {"SSL_CERT_FILE": self.missing}
        state = tls_setup.configure(environ, inject=broken, default_cafile=self.missing, ca_file=str(self.bundled))
        self.assertEqual((environ, state["mode"]), ({"SSL_CERT_FILE": str(self.bundled)}, tls_setup.BUNDLED))
        # ... and with OpenSSL's default file present, the dead value is dropped so the default is read.
        present = self.folder / "default.pem"
        present.write_text("x")
        environ = {"SSL_CERT_FILE": self.missing}
        state = tls_setup.configure(environ, inject=broken, default_cafile=str(present), ca_file=str(self.bundled))
        self.assertEqual((environ, state["mode"]), ({}, tls_setup.DEFAULT))

    def test_release_check_switch_skips_truststore_and_takes_the_bundled_file(self):
        # T6: even with truststore working and OpenSSL's default file present (the build Mac).
        present = self.folder / "default.pem"
        present.write_text("x")
        environ, injected = {tls_setup.SKIP_TRUSTSTORE: "1"}, []
        state = tls_setup.configure(environ, inject=lambda: injected.append(1), default_cafile=str(present),
                                    ca_file=str(self.bundled))
        self.assertEqual((state["mode"], state["ca_file"], injected), (tls_setup.BUNDLED, str(self.bundled), []))
        self.assertEqual(environ["SSL_CERT_FILE"], str(self.bundled))

    def test_certificate_errors_are_told_apart_from_network_errors(self):
        self.assertTrue(tls_setup.is_certificate_error(CERT_FAILURE))
        self.assertFalse(tls_setup.is_certificate_error(urllib.error.URLError(ConnectionRefusedError(61, "refused"))))
        # T8: a handshake cut short by the network is not a certificate failure.
        for flaky in (ssl.SSLEOFError(8, "EOF occurred in violation of protocol"),
                      ssl.SSLZeroReturnError(6, "TLS/SSL connection has been closed"), ssl.SSLError(1, "other")):
            self.assertFalse(tls_setup.is_certificate_error(urllib.error.URLError(flaky)), flaky)
        coded = ssl.SSLError(1, "certificate verify failed")
        coded.reason = "CERTIFICATE_VERIFY_FAILED"
        self.assertTrue(tls_setup.is_certificate_error(urllib.error.URLError(coded)))
        with self.assertRaises(text_model.TextModelResponseError) as caught:
            text_model.check_connection(transport=lambda url, timeout: (_ for _ in ()).throw(CERT_FAILURE))
        self.assertEqual((caught.exception.transport_error, caught.exception.certificate_error), ("URLError", True))


class DoctorLineTest(unittest.TestCase):
    def setUp(self):
        tls_setup.reset()
        self.addCleanup(tls_setup.reset)

    def test_secure_connection_fails_plainly_with_the_error_class_in_detail(self):
        def check():
            raise text_model._transport_failure(CERT_FAILURE, None)
        line = doctor.check_secure_connection(online=True, check=check)
        self.assertEqual(line["status"], doctor.FAIL)
        self.assertTrue(line["line"].startswith("Sidedoor can't make secure connections on this computer"))
        self.assertEqual(line["detail"]["error_class"], "URLError")
        self.assertEqual(doctor.check_secure_connection(online=True, check=lambda: 200)["status"], doctor.OK)

    def test_offline_line_reports_how_trust_was_set(self):
        tls_setup.configure({}, inject=lambda: None)
        self.assertEqual(doctor.check_secure_connection()["status"], doctor.OK)
        tls_setup.reset()
        tls_setup.configure({}, inject=broken, default_cafile="/nonexistent/cert.pem", ca_file="")
        self.assertEqual(doctor.check_secure_connection()["status"], doctor.FAIL)


class GateScanTest(unittest.TestCase):
    """Must-hit controls for the release gate's classification."""

    def test_undocumented_paths_and_rpath_loads_are_not_explained(self):
        none = {"loads": [], "rpaths": [], "ids": []}
        self.assertIsNone(check_release_tls.explain("libfoo.dylib", "/opt/homebrew/etc/foo/config", none, {}, False))
        self.assertIsNone(check_release_tls.explain("libcrypto.3.dylib", "/opt/homebrew/etc/other/cert.pem", none, {}, False))
        rpath = {"loads": ["@rpath/libbar.dylib"], "rpaths": ["/opt/homebrew/lib"], "ids": []}
        self.assertIsNone(check_release_tls.explain("x.so", "/opt/homebrew/lib", rpath, {}, False))
        # T10: a Homebrew LC_RPATH fails even when the file loads nothing through @rpath; another build
        # path in an LC_RPATH stays harmless then.
        self.assertIsNone(check_release_tls.explain("x.so", "/opt/homebrew/lib", dict(rpath, loads=[]), {}, False))
        framework = "/Library/Frameworks/Python.framework/Versions/3.13/lib"
        self.assertIsNotNone(check_release_tls.explain("x.so", framework, dict(rpath, loads=[], rpaths=[framework]),
                                                       {}, False))
        for value in ("/opt/homebrew/lib", "/usr/local/Cellar/sqlite/3.50/lib", "/usr/local/opt/xz/lib",
                      "/usr/local/lib"):
            self.assertTrue(check_release_tls.homebrew_rpath(value), value)
        for value in ("@loader_path/../lib", "/usr/local/libexec", framework):
            self.assertFalse(check_release_tls.homebrew_rpath(value), value)
        self.assertIsNotNone(check_release_tls.explain("libcrypto.3.dylib", "/opt/homebrew/etc/openssl@3/cert.pem",
                                                       none, {}, False))

    def test_intel_homebrew_folders_are_build_paths(self):
        # T9: /usr/local/opt/ and /usr/local/lib/ (Intel Homebrew); the bare /usr/local/lib of ctypes'
        # search list is not one.
        for path in ("/usr/local/opt/openssl@3/lib/libssl.3.dylib", "/usr/local/lib/libx.dylib"):
            self.assertTrue(check_release_tls.DEVELOPER.match(path.encode()), path)
        self.assertEqual(list(check_release_tls.DEVELOPER.finditer(b"\0/usr/local/lib\0")), [])


class GateRunTest(unittest.TestCase):
    """T4, T5, T6: the gates' steps, with the app's run faked (nothing is launched)."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.app = Path(folder.name) / "Sidedoor"
        self.ca = self.app / "certifi" / "cacert.pem"
        self.ca.parent.mkdir(parents=True)
        self.ca.write_text("pem")
        (self.app / "sidedoor.exe").write_text("")

    def fake(self, answers, seen):
        def run(command, **kwargs):
            seen.append(kwargs["env"])
            return mock.Mock(stdout=json.dumps(answers[len(seen) - 1]) + "\n", stderr="", returncode=0)
        return run

    def test_mac_step_three_uses_the_switch_with_ssl_cert_file_unset(self):
        seen = []
        with mock.patch.object(check_release_tls.subprocess, "run", self.fake([{"ok": True}], seen)):
            check_release_tls.net_check(self.app, "arm64", skip_truststore=True)
        self.assertEqual(seen[0].get("SIDEDOOR_TLS_SKIP_TRUSTSTORE"), "1")
        self.assertNotIn("SSL_CERT_FILE", seen[0])
        good = {"ok": True, "trust": "bundled", "ca_file": str(self.ca)}
        self.assertTrue(check_release_tls.fallback_ok(good, self.app))
        self.assertFalse(check_release_tls.fallback_ok(dict(good, trust="user"), self.app))
        self.assertFalse(check_release_tls.fallback_ok(dict(good, ca_file="/elsewhere/cacert.pem"), self.app))

    def test_windows_gate_runs_both_steps_and_fails_on_any_miss(self):
        good = [{"ok": True, "status": 200, "trust": "truststore"},
                {"ok": True, "status": 200, "trust": "bundled", "ca_file": str(self.ca)}]
        seen = []
        check_release_tls_windows.check(self.app, run=self.fake(good, seen))
        self.assertEqual(seen[0]["SSL_CERT_FILE"], seen[0]["SSL_CERT_DIR"])
        self.assertNotIn("SIDEDOOR_TLS_SKIP_TRUSTSTORE", seen[0])
        self.assertEqual(seen[1]["SIDEDOOR_TLS_SKIP_TRUSTSTORE"], "1")
        self.assertNotIn("SSL_CERT_FILE", seen[1])
        for bad in ([dict(good[0], ok=False), good[1]], [good[0], dict(good[1], trust="default")],
                    [dict(good[0], trust="bundled"), good[1]]):
            with self.assertRaises(SystemExit):
                check_release_tls_windows.check(self.app, run=self.fake(bad, []))

    def test_notarize_never_runs_without_the_gate(self):
        import sign_macos
        calls = []
        with mock.patch.object(sign_macos, "sign", lambda app, identity: calls.append("sign")), \
                mock.patch.object(sign_macos, "tls_gate", side_effect=SystemExit("gate failed")), \
                mock.patch.object(sign_macos, "release_zip"), \
                mock.patch.object(sign_macos, "run", side_effect=AssertionError("notarytool must not run")), \
                mock.patch.object(sign_macos.subprocess, "run", side_effect=AssertionError("ditto must not run")):
            with self.assertRaises(SystemExit):   # standalone main: finish with the default gate
                sign_macos.main([str(self.app), "--sign", "Developer ID", "--notarize", "profile"])
            with self.assertRaises(SystemExit):   # notarize on its own: refused
                sign_macos.notarize(self.app / "Other.app", "profile")
        self.assertEqual(calls, ["sign"])


if __name__ == "__main__":
    unittest.main()
