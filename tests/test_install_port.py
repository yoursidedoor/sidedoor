"""RL1: jobs build their LinkedIn port with `unipile_port.for_install`, always the relay with the
license key; the owner's unipile.json (Sidedoor's master Unipile key) only under the explicit
`"linkedin_transport": "local-dev"` setting. No network beyond 127.0.0.1 (the relay harness)."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import ast
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import failures_page
import orchestrator
import unipile_port
import workflow_tasks
from linkedin_port import SessionInvalid, UncertainWrite
from unipile_port import HttpTransport, LicenseRefused, LicenseRequired, LinkedInNotConnected, RelayTransport
try:
    from relay_optional import LICENSE, MINE, UNLINKED_LICENSE, RelayHarness, needs_relay, server
except ImportError:   # run as tests.test_install_port from the repo root
    from tests.relay_optional import LICENSE, MINE, UNLINKED_LICENSE, RelayHarness, needs_relay, server


ROOT = Path(__file__).resolve().parent.parent
MASTER = {"dsn": "api9.unipile.example.test:13443", "token": "MASTER-unipile-token-0123456789"}
DEV = {"linkedin_transport": "local-dev"}


def make_home(directory, license_key=None, master=True):
    home = Path(directory) / "home"
    home.mkdir()
    if license_key is not None:
        (home / "license.key").write_text(license_key + "\n")
    if master:
        (home / "unipile.json").write_text(json.dumps(MASTER))
    return home


class Factory(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def test_default_is_the_relay_even_when_unipile_json_exists(self):
        home = make_home(self.directory.name, LICENSE)
        with mock.patch.object(unipile_port, "settings", side_effect=AssertionError("read unipile.json")):
            port = unipile_port.for_install({}, home, relay_url="http://127.0.0.1:9")
        self.assertIsInstance(port.transport, RelayTransport)
        self.assertIsNone(port.account_id)   # the relay resolves the license's own account
        self.assertEqual(port.transport._license_key, LICENSE)
        self.assertNotIn(MASTER["token"], repr(vars(port.transport)))

    def test_relay_url_defaults_to_the_apps_relay(self):
        import setup_steps
        port = unipile_port.for_install({}, make_home(self.directory.name, LICENSE))
        self.assertEqual(port.transport._base, setup_steps.RELAY_URL)

    def test_no_license_key_is_a_license_hold_not_a_master_key_fallback(self):
        home = make_home(self.directory.name, None)
        with self.assertRaises(LicenseRequired) as raised:
            unipile_port.for_install({"linkedin_transport": "relay"}, home)
        self.assertIn("Activate your license first", raised.exception.next_action)
        self.assertEqual(raised.exception.code, "LICENSE_REQUIRED")

    def test_an_unusable_license_key_is_a_license_hold(self):
        home = make_home(self.directory.name, "SD-bad key")
        with self.assertRaises(LicenseRequired):
            unipile_port.for_install({}, home)

    def test_local_dev_reads_the_owners_unipile_json(self):
        home = make_home(self.directory.name, None)
        port = unipile_port.for_install(DEV, home)
        self.assertIsInstance(port.transport, HttpTransport)
        self.assertEqual(port.transport._base, "https://" + MASTER["dsn"] + "/api/v1")

    def test_orchestrator_port_is_the_relay_for_a_user_with_no_unipile_json(self):
        home = make_home(self.directory.name, LICENSE, master=False)
        with mock.patch.object(orchestrator.user_profile, "home", return_value=home):
            port = orchestrator.linkedin_port_for({"linkedin_provider": "unipile"})
            self.assertIsInstance(port.transport, RelayTransport)
            self.assertIsNone(orchestrator.linkedin_port_for({}))   # HeyReach: no port
            (home / "license.key").unlink()
            with self.assertRaises(LicenseRequired):
                orchestrator.linkedin_port_for({"linkedin_provider": "unipile"})


class StepHold(unittest.TestCase):
    def database(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        database = sqlite3.connect(Path(directory.name) / "o.sqlite3")
        database.row_factory = sqlite3.Row
        self.addCleanup(database.close)
        workflow_tasks.initialize(database)
        return database

    def test_linkedin_not_connected_is_a_hold_naming_setup_step_7(self):
        database = self.database()

        def step():
            raise LinkedInNotConnected("HTTP_403")
        result = orchestrator.worker_step(database, "parent", "reply-monitor", step)
        self.assertEqual((result["state"], result["blocker"]), ("BLOCKED", "LINKEDIN_NOT_CONNECTED"))
        self.assertIn("Connect LinkedIn in Setup step 7 / Settings", result["next_action"])
        self.assertEqual(database.execute("SELECT blocker FROM workflow_tasks").fetchone()[0], "LINKEDIN_NOT_CONNECTED")

    def test_a_missing_license_is_a_hold_not_a_worker_exception(self):
        database = self.database()

        def step():
            raise LicenseRequired("LICENSE_KEY_MISSING")
        result = orchestrator.worker_step(database, "parent", "guarded-due-followups", step)
        self.assertEqual(result["state"], "BLOCKED")
        self.assertEqual(result["blocker"], "LICENSE_REQUIRED")
        self.assertIn("Activate your license first", result["next_action"])
        row = database.execute("SELECT status,blocker,next_action FROM workflow_tasks "
                               "WHERE worker='guarded-due-followups'").fetchone()
        self.assertEqual((row["status"], row["blocker"]), ("BLOCKED", "LICENSE_REQUIRED"))
        self.assertIn("Activate your license first", row["next_action"])


@needs_relay
class ThroughTheRelay(RelayHarness):
    """for_install's port against the relay in front of a fake Unipile."""

    def setUp(self):
        super().setUp()
        self.home_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.home_directory.cleanup)
        self.home = make_home(self.home_directory.name, LICENSE)

    def install_port(self):
        return unipile_port.for_install({}, self.home, relay_url=self.base, timeout=5)

    def test_every_call_the_jobs_make_goes_through(self):
        port = self.install_port()
        self.assertEqual(port.account().account_id, MINE)
        person = port.lookup("https://www.linkedin.com/in/alice")
        self.assertEqual(person.provider_id, "ALICE1")
        self.assertEqual(len(port.sent_invitations().items), 1)
        self.assertEqual([chat.chat_id for chat in port.chats().items], ["c1"])
        self.assertEqual([chat.chat_id for chat in port.chats(person="https://www.linkedin.com/in/alice").items], ["c1"])
        self.assertEqual(len(port.messages("c1").messages), 1)
        port.send_invitation("ALICE1", "hello")
        port.send_message("c1", "hi")
        port.start_chat("ALICE1", "hi")
        sent = [(request["method"], request["path"]) for request in self.upstream.requests]
        for call in [("GET", "/api/v1/accounts/" + MINE), ("GET", "/api/v1/users/alice"),
                     ("GET", "/api/v1/users/invite/sent"), ("GET", "/api/v1/chats"),
                     ("GET", "/api/v1/chats/c1/messages"), ("POST", "/api/v1/users/invite"),
                     ("POST", "/api/v1/chats/c1/messages"), ("POST", "/api/v1/chats")]:
            self.assertIn(call, sent)
        self.assertTrue(all(request["headers"].get("x-api-key") != MASTER["token"] for request in self.upstream.requests))

    def test_relations_is_the_one_port_call_the_relay_does_not_route(self):
        # Listed as a gap in RL1: no product caller uses UnipilePort.relations.
        with self.assertRaises(server.RelayError) as raised:
            server.match_route("GET", "/users/relations")
        self.assertEqual(raised.exception.code, "ROUTE_NOT_FOUND")

    def test_a_lapsed_license_is_refused_now_not_a_linkedin_session_failure(self):
        self.checker.valid_keys.discard(LICENSE)
        port = self.install_port()
        with mock.patch("license_clock.request_check", return_value=True) as check:
            with self.assertRaises(LicenseRefused) as raised:
                port.account()
            self.assertNotIsInstance(raised.exception, SessionInvalid)
            self.assertIsNot(port._session_valid, False)
            check.assert_called_once_with(self.home)
            with self.assertRaises(LicenseRefused):
                port.lookup("https://www.linkedin.com/in/alice")
            # A write stays uncertain (the caller reads before any retry) and still asks for the check.
            with self.assertRaises(UncertainWrite) as written:
                port.send_message("c1", "hi")
            self.assertEqual(written.exception.status, 401)
            self.assertEqual(check.call_count, 3)

    def test_a_lapse_found_in_a_job_child_leaves_the_heartbeat_its_request_file(self):
        import license_clock
        self.checker.valid_keys.discard(LICENSE)
        request = self.home / license_clock.CHECK_REQUEST_FILE
        self.assertFalse(request.exists())
        # A job child: no heartbeat in this process and no check run here yet.
        with mock.patch.object(license_clock, "_current", None), \
                mock.patch.dict(license_clock._last_check, {"mono": None}):
            with self.assertRaises(LicenseRefused):
                self.install_port().account()
        self.assertTrue(request.exists())
        self.assertTrue(license_clock.check_requested(self.home))

    def test_relay_license_not_linked_is_a_hold_not_a_session_failure(self):
        (self.home / "license.key").write_text(UNLINKED_LICENSE + "\n")
        port = self.install_port()
        with self.assertRaises(LinkedInNotConnected) as raised:
            port.account()
        self.assertNotIsInstance(raised.exception, SessionInvalid)
        self.assertIn("Setup step 7", raised.exception.next_action)
        with self.assertRaises(UncertainWrite):   # writes stay conservative
            port.send_message("c1", "hi")

    def test_a_unipile_401_is_still_a_session_failure(self):
        self.set_response("GET /chats", (401, {"type": "errors/disconnected_account", "title": "Disconnected"}))
        port = self.install_port()
        port.account_id = MINE
        with mock.patch("license_clock.request_check") as check:
            with self.assertRaises(SessionInvalid):
                port.chats()
        check.assert_not_called()


class Diagnostics(unittest.TestCase):
    def test_failures_page_reads_unipile_json_only_under_local_dev(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        home = make_home(directory.name, LICENSE)
        # secrets_of swallows errors, so the read is watched, not made to raise.
        with mock.patch.object(unipile_port, "settings", wraps=unipile_port.settings) as read:
            found = failures_page.secrets_of(home, {}, "tok")
        read.assert_not_called()
        self.assertNotIn(MASTER["token"], found)
        self.assertIn(MASTER["token"], failures_page.secrets_of(home, DEV, "tok"))


# -- no product path reads unipile.json outside the dev setting ------------------------------------

ALLOWED = {("unipile_port.py", "config_path"), ("unipile_port.py", "UnipilePort.from_settings"),
           ("unipile_port.py", "for_install"), ("failures_page.py", "secrets_of")}


def readers(source, filename):
    """(file, enclosing function) of every code reference to unipile.json: a call of
    `unipile_port.settings` (or bare `settings` inside unipile_port.py), any `.from_settings(`, and a
    "unipile.json" string that is not a docstring."""
    tree = ast.parse(source)
    found = set()
    docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                  if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                  and node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)}

    def visit(node, scope):
        for child in ast.iter_child_nodes(node):
            inner = scope
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = scope + (child.name,)
            hit = False
            if isinstance(child, ast.Call):
                function = child.func
                if isinstance(function, ast.Attribute) and function.attr == "from_settings":
                    hit = True
                if (isinstance(function, ast.Attribute) and function.attr == "settings"
                        and isinstance(function.value, ast.Name) and function.value.id == "unipile_port"):
                    hit = True
                if isinstance(function, ast.Name) and function.id == "settings" and filename == "unipile_port.py":
                    hit = True
            if (isinstance(child, ast.Constant) and isinstance(child.value, str) and "unipile.json" in child.value
                    and id(child) not in docstrings):
                hit = True
            if hit:
                found.add((filename, ".".join(scope)))
            visit(child, inner)
    visit(tree, ())
    return found


class NoMasterKeyReads(unittest.TestCase):
    def product_files(self):
        files = sorted(ROOT.glob("*.py")) + sorted((ROOT / "relay").glob("*.py")) + sorted((ROOT / "tools").glob("*.py"))
        self.assertGreater(len(files), 50)
        return files

    def test_only_the_dev_gated_readers_remain(self):
        found = set()
        for path in self.product_files():
            found |= readers(path.read_text(encoding="utf-8"), path.name)
        # Must-hit control: the scan finds the known (allowed) readers, so an empty scan cannot pass.
        self.assertEqual(found, ALLOWED)

    def test_the_scan_catches_a_new_reader(self):
        planted = ('import unipile_port\n'
                   'def build():\n    """unipile.json in a docstring is fine."""\n'
                   '    return unipile_port.UnipilePort.from_settings()\n'
                   'def peek(home):\n    return home / "unipile.json"\n'
                   'def creds():\n    return unipile_port.settings()\n')
        self.assertEqual(readers(planted, "orchestrator.py"),
                         {("orchestrator.py", "build"), ("orchestrator.py", "peek"), ("orchestrator.py", "creds")})

    def test_for_install_reads_settings_only_in_its_dev_branch(self):
        source = ast.get_source_segment(Path(unipile_port.__file__).read_text(),
                                        next(node for node in ast.parse(Path(unipile_port.__file__).read_text()).body
                                             if isinstance(node, ast.FunctionDef) and node.name == "for_install"))
        dev_branch, relay_branch = source.split("import setup_steps", 1)
        self.assertIn("if local_dev(config):", dev_branch)
        self.assertIn("settings(", dev_branch)
        self.assertNotIn("settings(", relay_branch)
        self.assertNotIn("unipile.json", relay_branch)


if __name__ == "__main__":
    unittest.main()


# -- RL1 review B: the sender, the holds, HeyReach people on Unipile ---------------------------------

@needs_relay
class SenderIdentity(RelayHarness):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = make_home(directory.name, LICENSE, master=False)
        self.config = {"linkedin_provider": "unipile", "sender_id": None, "sender_profile": None}

    def sync(self):
        with mock.patch("setup_steps.RELAY_URL", self.base), \
                mock.patch.object(orchestrator.user_profile, "home", return_value=self.home):
            return orchestrator.sync_sender(self.config, self.home)

    def test_an_install_connected_before_rl1_is_filled_in_by_the_first_cycle(self):
        import zlib
        import setup_steps
        config, outcome = self.sync()
        self.assertEqual(outcome["state"], "SAVED")
        self.assertEqual((config["sender_id"], config["sender_profile"]),
                         (zlib.crc32(MINE.encode()), "https://www.linkedin.com/in/owner"))
        self.assertEqual(setup_steps.saved_sender(self.home)["account_id"], MINE)
        self.assertEqual(self.sync()[1]["state"], "UNCHANGED")

    def test_another_linkedin_replaces_the_saved_sender_and_is_noted(self):
        import setup_steps
        setup_steps.save_sender(self.home, "acct-old", "https://www.linkedin.com/in/someone-else")
        config, outcome = self.sync()
        self.assertEqual(outcome["state"], "UPDATED")
        self.assertEqual(outcome["previous_profile"], "https://www.linkedin.com/in/someone-else")
        self.assertEqual(config["sender_profile"], "https://www.linkedin.com/in/owner")

    def test_a_failed_account_read_changes_nothing(self):
        import setup_steps
        self.checker.valid_keys.discard(LICENSE)
        with mock.patch("license_clock.request_check"):
            config, outcome = self.sync()
        self.assertEqual((outcome["state"], outcome.get("blocker")), ("UNCHECKED", "LICENSE_INVALID"))
        self.assertIsNone(config["sender_id"])
        self.assertIsNone(setup_steps.saved_sender(self.home))

    def test_the_overlay_only_applies_to_a_unipile_install(self):
        import setup_steps
        setup_steps.save_sender(self.home, MINE, "https://www.linkedin.com/in/owner")
        self.assertEqual(setup_steps.with_sender({"sender_id": 5}, self.home), {"sender_id": 5})
        self.assertGreater(setup_steps.with_sender(self.config, self.home)["sender_id"], 0)


class ShippedConfig(unittest.TestCase):
    def test_the_shipped_config_never_opts_into_the_master_key(self):
        import resources
        self.assertNotIn("linkedin_transport", resources.read_json("config.json"))
        self.assertNotIn("linkedin_transport", (ROOT / "config.json").read_text())


class OutreachReadHold(unittest.TestCase):
    def test_a_setup_hold_during_an_inspection_waits_and_spends_no_try(self):
        import datetime as dt
        import outreach
        for error in (LicenseRefused("HTTP_401"), LinkedInNotConnected("HTTP_403")):
            self.assertEqual(outreach._read_error(error), ("RETRY_WAIT", error.code))
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        self.addCleanup(database.close)
        outreach.initialize_actions(database)
        now = dt.datetime.now(dt.timezone.utc)
        for unused in range(3):
            token = outreach._claim_read(database, "a1", "INSPECTION", now)
            self.assertIsNotNone(token)
            self.assertTrue(outreach._finish_read(database, "a1", "INSPECTION", token, now, "RETRY_WAIT", "LICENSE_INVALID"))
            row = database.execute("SELECT state,attempts,next_at FROM outreach_read_retries").fetchone()
            self.assertEqual((row["state"], row["attempts"]), ("RETRY_WAIT", 0))
            self.assertLessEqual(dt.datetime.fromisoformat(row["next_at"]), now + dt.timedelta(minutes=15))
            now += dt.timedelta(minutes=16)


class SequenceHoldCodes(unittest.TestCase):
    def test_a_lookup_hold_names_what_to_do(self):
        import sequence

        class Port:
            def lookup(self, profile):
                raise LinkedInNotConnected("HTTP_403")
        contexts, failures = sequence.invite_contexts(Port(), [{"profile": "https://www.linkedin.com/in/x", "proof": {}}])
        self.assertEqual(contexts, {})
        self.assertEqual(failures[0]["hold"], "LINKEDIN_NOT_CONNECTED")
        self.assertIn("Connect LinkedIn", failures[0]["next_action"])


class HeyReachPeopleOnUnipile(unittest.TestCase):
    def test_inactivity_skips_a_heyreach_history_person_and_the_step_does_not_fail(self):
        import inactivity_closure
        import manual_inactivity_closure as manual
        from linkedin_port import FakePort
        database, root = self.enterContext(manual.fixture())
        manual.record(database, "initial_sent", manual.START)
        result = inactivity_closure.cycle(database, manual.CONFIG, orchestrator.NoHeyReach(manual.CONFIG),
                                          manual.NOW, port=FakePort())
        skipped = [item for item in result["items"] if item.get("blocker") == "HEYREACH_PERSON_NOT_READABLE_ON_UNIPILE"]
        self.assertEqual(len(skipped), 1, result)
        self.assertIsNone(database.execute("SELECT 1 FROM outreach_inactivity_checks").fetchone())
