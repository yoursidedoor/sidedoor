"""E2.T6: diagnostics go through log.py; machine-read stdout is unchanged and every line is redacted."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import contextlib
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import decision_transport
import failures_page
import log
import logging
import orchestrator
import sys
import threading
import time
import tray

LINE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z (INFO|WARNING|ERROR) (\S+) (\S+): (.*)$")


class LogStateTest(unittest.TestCase):
    def setUp(self):
        saved = dict(log._state)
        self.addCleanup(log._state.update, saved)


class JobStdoutContractTest(LogStateTest):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "state").mkdir()
        (self.root / "config.json").write_text(json.dumps({"timezone": "America/Los_Angeles", "research_enabled": True}))

    def run_applications(self, argv):
        result = {"job": "applications", "state": "COMPLETE", "processed": 2, "nested": {"a": [1, "b"]}}

        def service(root, config, database, only=None):
            orchestrator.LOG.info("Daily research has not finished its turn today.")
            orchestrator.LOG.warning("a diagnostic")
            return result

        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"SIDEDOOR_HOME": str(self.root)}), \
                patch.object(orchestrator, "applications_service", side_effect=service), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = orchestrator.main(argv)
        self.assertEqual(code, 0)
        return result, out.getvalue(), err.getvalue()

    def test_the_json_result_line_is_byte_identical(self):
        result, stdout, stderr = self.run_applications(["job", "applications"])
        info, rest = stdout.split("\n", 1)
        match = LINE.match(info)
        self.assertTrue(match, info)
        self.assertEqual(match.groups()[:3], ("INFO", "applications", "orchestrator"))
        # The job's result line exactly as log_retention.print_result always wrote it.
        expected = json.dumps({"state": "COMPLETE", "report": str(self.root / "reports" / "applications-service.json")},
                              separators=(",", ":")) + "\n"
        self.assertEqual(rest, expected)
        self.assertEqual(json.loads(rest)["state"], "COMPLETE")
        warning = LINE.match(stderr.strip())
        self.assertTrue(warning, stderr)
        self.assertEqual(warning.groups(), ("WARNING", "applications", "orchestrator", "a diagnostic"))

    def test_a_scoped_run_prints_its_indented_json_unchanged(self):
        result, stdout, _ = self.run_applications(["applications", "--only", "x:1"])
        self.assertTrue(stdout.endswith(json.dumps(result, indent=2) + "\n"))
        self.assertEqual(json.loads(stdout.split("\n", 1)[1]), result)


class RedactionTest(LogStateTest):
    def capture(self, *args, secrets=()):
        log._state["extra"] = tuple(secrets)
        log._state["configured"] = False
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            log.get("demo").warning(*args)
        return err.getvalue()

    def test_a_planted_key_is_redacted(self):
        text = self.capture("calling with api_key=%s and sk-or-v1-abcdef0123456789 via https://h.example/p?k=1",
                            "PLANTED-SECRET-VALUE")
        self.assertNotIn("PLANTED-SECRET-VALUE", text)
        self.assertNotIn("sk-or-v1-abcdef0123456789", text)
        self.assertNotIn("h.example", text)
        self.assertIn("<redacted", text)

    def test_a_configured_secret_is_removed_even_outside_a_field(self):
        planted = "lic_0123456789abcdefXYZ"
        text = self.capture("license check said %s", planted, secrets=[planted])
        self.assertNotIn(planted, text)
        self.assertNotIn(planted[:8], text)

    def test_an_exception_message_is_never_written_only_its_type(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                raise RuntimeError("token=PLANTED-IN-TRACEBACK")
            except RuntimeError:
                log.get("demo").exception("failed")
        self.assertNotIn("PLANTED-IN-TRACEBACK", err.getvalue())
        self.assertIn("failed (RuntimeError)", err.getvalue())

    def test_redaction_that_cannot_run_withholds_the_line(self):
        with patch("failures_page.redact_text", side_effect=RuntimeError):
            text = self.capture("token=%s", "PLANTED")
        self.assertNotIn("PLANTED", text)
        self.assertIn(log.WITHHELD, text)

    def test_a_converted_diagnostic_carries_level_job_and_module(self):
        log._state["job"] = "serve"
        err = io.StringIO()
        icon = tray.Tray.__new__(tray.Tray)
        icon._refresh_flags = lambda: None

        def pause():
            raise RuntimeError("database is locked")
        with contextlib.redirect_stderr(err):
            icon._do(pause)(None)
        match = LINE.match(err.getvalue().strip())
        self.assertTrue(match, err.getvalue())
        self.assertEqual(match.groups(), ("WARNING", "serve", "tray", "Tray: pause failed (RuntimeError)."))



class ReviewBTest(LogStateTest):
    """Logging review B, LG1-LG6."""

    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        environment = patch.dict(os.environ, {"SIDEDOOR_HOME": str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)
        log._state.update(configured=True, extra=(), job="run")

    def line(self, *args, logger=None):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            (logger or log.get("demo")).warning(*args)
        return err.getvalue()

    def test_LG1_a_key_saved_after_the_first_line_is_redacted(self):
        planted = "lic-0123456789abcdefPLANTED"
        self.assertIn("first line", self.line("first line"))
        (self.home / "license.key").write_text(planted)
        text = self.line("the license is %s", planted)
        self.assertNotIn(planted, text)
        self.assertIn("<redacted>", text)

    def test_LG2_unreadable_secrets_withhold_the_line_and_the_next_record_retries(self):
        with patch.object(failures_page, "secrets_of", side_effect=OSError("keychain locked")):
            text = self.line("api said %s", "anything")
        self.assertIn(log.WITHHELD, text)
        self.assertNotIn("anything", text)
        self.assertIn("api said anything", self.line("api said %s", "anything"))

    def test_LG2_incomplete_redaction_in_the_store_withholds(self):
        store = type(sys)("credentials")   # the keychain store's two calls (E2.T4)
        store.values = lambda home=None: []
        store.redaction_complete = lambda home: False
        with patch.dict(sys.modules, {"credentials": store}):
            self.assertIn(log.WITHHELD, self.line("hello"))
        store.redaction_complete = lambda home: True
        with patch.dict(sys.modules, {"credentials": store}):
            self.assertIn("hello", self.line("hello"))

    def test_LG3_a_line_naming_a_provider_host_is_replaced_not_withheld(self):
        found = failures_page.Secrets(["KEY-0123456789abcdef"], ["api.heyreach.io"])
        with patch.object(failures_page, "secrets_of", return_value=found):
            text = self.line("contacting api.heyreach.io failed")
        self.assertNotIn(log.WITHHELD, text)
        self.assertNotIn("api.heyreach.io", text)
        self.assertIn("contacting <redacted> failed", text)

    def test_LG4_secrets_are_read_one_at_a_time(self):
        state = {"now": 0, "most": 0}
        guard = threading.Lock()

        def slow(home, config):
            with guard:
                state["now"] += 1
                state["most"] = max(state["most"], state["now"])
            time.sleep(0.01)
            with guard:
                state["now"] -= 1
            return failures_page.Secrets(["SECRET-0123456789abcdef"])
        err = io.StringIO()
        with patch.object(failures_page, "secrets_of", side_effect=slow), contextlib.redirect_stderr(err):
            threads = [threading.Thread(target=log.get("demo").warning, args=("value %s", "SECRET-0123456789abcdef"))
                       for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(state["most"], 1)
        self.assertEqual(err.getvalue().count("value <redacted>"), 8)
        self.assertNotIn("SECRET-0123456789abcdef", err.getvalue())

    def test_LG5_the_root_logger_and_decision_transport_are_redacted_without_configure(self):
        log._state["configured"] = False
        root = log.get("demo").parent
        self.assertEqual(root.name, log.ROOT)
        self.assertTrue(any(isinstance(item, log.RedactFilter) for item in root.filters))
        self.assertNotIn("PLANTED-ROOT", self.line("token=%s", "PLANTED-ROOT", logger=root))
        self.assertIs(decision_transport.LOG, log.get("decision_transport"))
        self.assertNotIn("PLANTED-DT", self.line("api_key=%s", "PLANTED-DT", logger=decision_transport.LOG))

    def test_LG6_configure_runs_after_home_is_applied(self):
        seen = []

        class Stop(Exception):
            pass

        def record(**kwargs):
            seen.append(os.environ.get("SIDEDOOR_HOME"))
            raise Stop
        other = str(self.home / "other")
        with patch.object(orchestrator.log, "configure", side_effect=record), self.assertRaises(Stop):
            orchestrator.main(["status", "--home", other])
        self.assertEqual(seen, [other])


if __name__ == "__main__":
    unittest.main()
