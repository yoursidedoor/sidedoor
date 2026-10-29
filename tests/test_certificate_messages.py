"""T7 (0.4.0): every place that said "Check your internet" for an HTTPS call says the certificate
message instead when the secure connection could not be verified (tls_setup.is_certificate_error),
and keeps "Check your internet" for a network error, a cut-off handshake included (T8). Offline."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import ssl
import tempfile
import unittest
import urllib.error
from unittest import mock
from pathlib import Path

import doctor
import license_clock
import openai_auth
import setup_steps
from test_license_clock import ClockCase, KEY, at

CERTIFICATE = urllib.error.URLError(ssl.SSLCertVerificationError(
    1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate"))
FLAKY = urllib.error.URLError(ssl.SSLEOFError(8, "EOF occurred in violation of protocol"))
PLAIN = "Run Report a problem so we can see why."


def raising(error):
    def call(*args, **kwargs):
        raise error
    return call


class LicenseTest(ClockCase):
    def test_license_step_says_certificate_not_internet(self):
        state = setup_steps.activate_license(KEY, self.home, "https://relay.test", post=raising(CERTIFICATE))
        self.assertEqual(state["steps"]["license"]["message"],
                         "Sidedoor couldn't verify the license server's secure connection on this computer. " + PLAIN)
        state = setup_steps.activate_license(KEY, self.home, "https://relay.test", post=raising(FLAKY))
        self.assertIn("Check your internet", state["steps"]["license"]["message"])

    def test_grace_and_paused_banners_and_check_again(self):
        self.true = at("2026-10-25T09:00:00")
        self.activate()
        self.true = at("2026-10-27T10:00:00")   # within 24 hours of valid_until: the grace banner
        self.assertEqual(license_clock.check(self.home, "https://relay.test", post=raising(CERTIFICATE),
                                             now=self.local), "offline")
        value = self.view()
        self.assertEqual(value["state"], "grace")
        self.assertEqual(value["text"], "Sidedoor couldn't verify the license server's secure connection on this "
                                        "computer. It keeps working for 23 hours more. " + PLAIN)
        self.assertIn("secure connection", license_clock.check_message("offline", value))
        self.assertNotIn("internet", license_clock.check_message("offline", value))
        self.true = at("2026-10-28T09:01:00")
        value = self.view()
        self.assertEqual(value["state"], "grace_spent")
        self.assertIn("couldn't verify the license server's secure connection", value["text"])
        self.assertNotIn("internet", value["text"])
        # A network error (a handshake cut short too) keeps the internet words.
        self.assertEqual(license_clock.check(self.home, "https://relay.test", post=raising(FLAKY),
                                             now=self.local), "offline")
        value = self.view()
        self.assertIn("Check your internet", value["text"])
        self.assertIn("Check your internet", license_clock.check_message("offline", value))


class LinkedInTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        (self.home / "license.key").write_text("SD-TESTKEY\n")

    def test_status_and_link_say_certificate_not_internet(self):
        expected = "Sidedoor couldn't verify the LinkedIn service's secure connection on this computer. " + PLAIN
        step = setup_steps.linkedin_status(self.home, transport=raising(CERTIFICATE))["steps"]["linkedin"]
        self.assertEqual((step["message"], step["error_class"]), (expected, "URLError"))
        state, url = setup_steps.linkedin_link("create", self.home, make_link=raising(CERTIFICATE))
        self.assertEqual((state["steps"]["linkedin"]["message"], url), (expected, None))
        step = setup_steps.linkedin_status(self.home, transport=raising(FLAKY))["steps"]["linkedin"]
        self.assertIn("Check your internet", step["message"])
        self.assertNotIn("error_class", step)


class DoctorAndOpenAITest(unittest.TestCase):
    def test_relay_line(self):
        line = doctor.check_relay("https://relay.test", get=raising(CERTIFICATE))
        self.assertEqual((line["status"], line["todo"]), (doctor.FAIL, PLAIN))
        self.assertIn("secure connection", line["line"])
        self.assertIn("Check your internet", doctor.check_relay("https://relay.test", get=raising(FLAKY))["todo"])

    def test_openai_key_test(self):
        config = {"model": "gpt-test"}
        with mock.patch.object(openai_auth, "models_used", return_value=["gpt-test"]):
            ok, kind, words = openai_auth.test_key("sk-test", config, raising(CERTIFICATE))
            self.assertEqual((ok, kind), (False, "network"))
            self.assertEqual(words, "Sidedoor couldn't verify OpenAI's secure connection on this computer. " + PLAIN)
            self.assertIn("Check your internet", openai_auth.test_key("sk-test", config, raising(FLAKY))[2])


if __name__ == "__main__":
    unittest.main()
