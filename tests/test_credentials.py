"""E2.T4: the OpenRouter and license keys in the OS store (credentials.py, option B).

Fakes only, never a real keychain (owner, 2026-10-22, after a dialog appeared): a dict store
(credential_fixtures.FakeStore), a fake `security` executable for the macOS backend's real code
path (argv, stdin, exit codes, a hang) with an injected lock state, and a fake Credential Manager
api. No test creates, locks, reads or writes any real keychain, and no test can cause a prompt.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; the OS store is off)
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import credential_fixtures
import credentials
import doctor
import failures_page
import scheduler_loop
import setup_steps
import text_model

# A private file's mode: Windows keeps only the read-only bit (VM pass, 2026-10-23).
PRIVATE = 0o666 if os.name == "nt" else 0o600


OPENROUTER = "sk-or-v1-fake-0123456789abcdef-9z9z"
LICENSE = "SD-FAKELICENSE0123456789TLUS"
CONFIG = {"text_model": {"model": "test/model"}}
OK_ANSWER = json.dumps({"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}], "usage": {}}).encode()
DASHBOARD = all(importlib.util.find_spec(name) for name in ("fastapi", "httpx"))

FAKE_SECURITY = r'''#!%(python)s
import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
state_path = os.path.join(here, "store.json")
log_path = os.path.join(here, "calls.jsonl")
mode = open(os.path.join(here, "mode")).read().strip() if os.path.exists(os.path.join(here, "mode")) else "ok"
arguments = sys.argv[1:]
stdin = sys.stdin.read() if arguments[:1] == ["-i"] else None
with open(log_path, "a") as log:
    log.write(json.dumps({"argv": sys.argv, "stdin": stdin}) + "\n")
if mode == "hang":
    time.sleep(60)
store = json.load(open(state_path)) if os.path.exists(state_path) else {}
def option(items, flag):
    return items[items.index(flag) + 1]
if stdin is not None:
    import shlex
    for line in stdin.splitlines():
        items = shlex.split(line)
        if items and items[0] == "add-generic-password":
            store[option(items, "-s") + "|" + option(items, "-a")] = bytes.fromhex(option(items, "-X")).decode()
    json.dump(store, open(state_path, "w"))
    sys.exit(0)
command = arguments[0]
key = option(arguments, "-s") + "|" + option(arguments, "-a")
if command == "find-generic-password":
    if key not in store:
        sys.exit(44)
    value = store[key]
    print(value if value.isascii() else value.encode().hex())   # as security prints non-ASCII
    sys.exit(0)
if command == "delete-generic-password":
    if key not in store:
        sys.exit(44)
    del store[key]
    json.dump(store, open(state_path, "w"))
    sys.exit(0)
sys.exit(1)
'''


class Base(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.store = credential_fixtures.use_fake(self)

    def write(self, name, value):
        (self.home / credentials.NAMES[name]).write_text(value + "\n")

    def exists(self, name):
        return (self.home / credentials.NAMES[name]).exists()

    def lock(self):
        """The store as a locked keychain: every call raises Locked (the cache is cleared)."""
        self.store.fail.update(read=credentials.Locked("KEYCHAIN_LOCKED"), write=credentials.Locked("KEYCHAIN_LOCKED"),
                               delete=credentials.Locked("KEYCHAIN_LOCKED"))
        credentials.clear_cache()

    def unlock(self):
        self.store.fail.clear()
        credentials.clear_cache()


class HelperTest(Base):
    def test_put_goes_to_the_store_namespaced_by_an_install_id_bound_to_the_home(self):
        self.assertEqual(credentials.put("openrouter", OPENROUTER, self.home), "store")
        record = json.loads((self.home / credentials.ID_FILE).read_text())
        self.assertRegex(record["id"], "^[0-9a-f]{32}$")
        self.assertEqual(record["home"], os.path.realpath(self.home))
        self.assertEqual(self.store.items, {record["id"] + ":openrouter": OPENROUTER})
        self.assertEqual(stat.S_IMODE((self.home / credentials.ID_FILE).stat().st_mode), PRIVATE)
        credentials.clear_cache()
        self.assertEqual(credentials.get("openrouter", self.home), OPENROUTER)
        self.assertIsNone(credentials.get("license", self.home))

    def test_another_home_never_reads_this_homes_items(self):
        credentials.put("license", LICENSE, self.home)
        other = self.root / "other"
        other.mkdir()
        self.assertIsNone(credentials.get("license", other))

    def test_the_stored_value_wins_over_a_file(self):   # KR3
        credentials.put("license", LICENSE, self.home)
        self.write("license", "SD-STALEFILE")
        credentials.clear_cache()
        self.assertEqual(credentials.get("license", self.home), LICENSE)

    def test_a_stale_file_is_removed_before_the_store_is_written(self):   # A3
        self.write("license", "SD-STALEFILE")
        seen = []
        write = self.store.write
        self.store.write = lambda account, value: (seen.append(self.exists("license")), write(account, value))
        credentials.put("license", LICENSE, self.home)
        self.assertEqual(seen, [False])

    def test_a_refused_write_with_the_item_confirmed_absent_falls_back_to_the_private_file(self):   # K4
        self.store.fail["write"] = credentials.Unavailable("KEYCHAIN_REFUSED")
        self.assertEqual(credentials.put("openrouter", OPENROUTER, self.home), "file")
        self.assertEqual(stat.S_IMODE((self.home / "openrouter.key").stat().st_mode), PRIVATE)
        self.assertEqual(credentials.get("openrouter", self.home), OPENROUTER)

    def test_a_save_while_locked_is_refused_and_writes_nothing(self):   # KR3
        credentials.put("license", LICENSE, self.home)
        self.lock()
        with self.assertRaises(credentials.Locked):
            credentials.put("license", "SD-BADKEY", self.home)
        self.assertFalse(self.exists("license"))
        self.unlock()
        self.assertEqual(credentials.get("license", self.home), LICENSE)

    def test_a_missing_key_is_cached_and_a_put_clears_it(self):   # C2
        credentials.put("license", LICENSE, self.home)
        credentials.clear_cache()
        self.store.calls.clear()
        for _ in range(3):
            self.assertIsNone(credentials.get("openrouter", self.home))
            credentials.hold_reason(self.home)
        self.assertEqual(self.store.calls.count("read"), 2)   # once per key, then cached
        credentials.put("openrouter", OPENROUTER, self.home)
        self.assertEqual(credentials.get("openrouter", self.home), OPENROUTER)

    def test_delete_removes_the_store_item_then_the_file(self):
        credentials.put("license", LICENSE, self.home)
        credentials.delete("license", self.home)
        self.assertEqual(self.store.items, {})
        self.assertIsNone(credentials.get("license", self.home))

    def test_a_refused_delete_leaves_everything_so_nothing_reappears(self):   # A5
        credentials.put("license", LICENSE, self.home)
        self.store.fail["delete"] = credentials.Unavailable("KEYCHAIN_REFUSED")
        with self.assertRaises(credentials.Unavailable):
            credentials.remove_all(self.home)
        self.assertEqual(self.store.values(), [LICENSE])

    def test_an_invalid_install_id_is_refused(self):   # A7
        (self.home / credentials.ID_FILE).write_text('{"id": "x -a evil", "home": "/"}')
        with self.assertRaises(credentials.Unavailable):
            credentials.put("license", LICENSE, self.home)
        self.assertEqual(self.store.calls, [])
        with self.assertRaises(credentials.Unavailable):
            credentials.MacKeychain(security="/nonexistent", unlocked=lambda: True).write("x -a evil:license", "v")

    def test_readers_go_through_the_store(self):
        credentials.put("openrouter", OPENROUTER, self.home)
        credentials.put("license", LICENSE, self.home)
        with mock.patch.dict(os.environ, {"SIDEDOOR_HOME": str(self.home)}):
            self.assertEqual(text_model.key(CONFIG), OPENROUTER)
            self.assertEqual(text_model.environment(CONFIG, {})[text_model.KEY_VARIABLE], OPENROUTER)
        self.assertEqual(setup_steps.saved_openrouter_key(self.home), OPENROUTER)
        self.assertEqual(setup_steps.license_key(self.home), LICENSE)

    def test_redaction_covers_store_keys_even_while_locked(self):   # A4
        credentials.put("openrouter", OPENROUTER, self.home)
        credentials.put("license", LICENSE, self.home)
        self.lock()
        secrets = failures_page.secrets_of(self.home, CONFIG)
        text = failures_page.redact_text("key %s and %s" % (OPENROUTER, LICENSE), secrets)
        self.assertNotIn(OPENROUTER, text)
        self.assertNotIn(LICENSE, text)
        self.assertTrue(credentials.redaction_complete(self.home))

    def test_redaction_is_incomplete_when_a_locked_key_was_never_seen(self):   # A4
        credentials.put("license", LICENSE, self.home)
        credentials._seen.clear()
        self.lock()
        self.assertFalse(credentials.redaction_complete(self.home))


class TestGuardTest(unittest.TestCase):   # C1
    def test_a_cleared_environment_never_reaches_a_real_store(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        home = Path(directory.name)
        refuse = mock.patch.object(subprocess, "run", side_effect=AssertionError("a security process was started"))
        with refuse, mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(credentials.backend())   # NO_STORE from profile_home, whatever the environment says
            self.assertEqual(credentials.put("license", LICENSE, home), "file")
            self.assertEqual(credentials.get("license", home), LICENSE)
            previous = credentials.use_backend(None)   # even the platform's own backend refuses under unittest
            try:
                chosen = credentials.backend()
                if chosen is not None:
                    with self.assertRaises(credentials.Unavailable):
                        chosen.read("0" * 32 + ":license")
            finally:
                credentials.use_backend(previous)
        with self.assertRaises(credentials.Unavailable):
            credentials.keychain_unlocked()


class MigrationTest(Base):
    def test_success_moves_both_and_deletes_the_files(self):
        self.write("openrouter", OPENROUTER)
        self.write("license", LICENSE)
        tested = {"key_sha256": hashlib.sha256(OPENROUTER.encode()).hexdigest(), "tested_at": "2026-10-22T00:00:00Z"}
        (self.home / "openrouter.key.tested").write_text(json.dumps(tested))
        result = credentials.migrate(self.home)
        self.assertEqual((result["openrouter"], result["license"]), ("moved", "moved"))
        self.assertFalse(self.exists("openrouter") or self.exists("license"))
        self.assertEqual(self.store.values(), sorted([OPENROUTER, LICENSE]))
        credentials.clear_cache()
        self.assertEqual(credentials.get("license", self.home), LICENSE)
        self.assertTrue(setup_steps.openrouter_tested(self.home))   # the marker's hash matches the stored key

    def test_a_second_start_does_nothing(self):
        self.write("license", LICENSE)
        credentials.migrate(self.home)
        self.store.calls.clear()
        result = credentials.migrate(self.home)
        self.assertEqual((result["openrouter"], result["license"], result["home"]), ("absent", "absent", "same"))
        self.assertEqual(self.store.calls, [])

    def test_a_read_back_that_differs_keeps_the_file_and_the_next_start_retries(self):
        self.write("license", LICENSE)
        self.store.garble = True
        lines = []
        self.assertEqual(credentials.migrate(self.home, log=lines.append)["license"], "kept: read-back differs")
        self.assertTrue(self.exists("license"))
        self.assertEqual(self.store.items, {})            # the wrong copy was removed again
        self.assertEqual(credentials.get("license", self.home), LICENSE)
        self.assertEqual(len(lines), 1)
        self.assertNotIn(LICENSE, lines[0])
        self.store.garble = False
        self.assertEqual(credentials.migrate(self.home)["license"], "moved")
        self.assertFalse(self.exists("license"))

    def test_a_file_never_overwrites_a_different_stored_key(self):   # KR3
        credentials.put("license", LICENSE, self.home)
        self.write("license", "SD-OLDERFILE")
        self.assertEqual(credentials.migrate(self.home)["license"], "kept: the keychain holds a different key")
        self.assertEqual(self.store.values(), [LICENSE])
        self.assertIn("older file", doctor.check_credentials(self.home)["line"])

    def test_an_unavailable_store_keeps_the_file_and_doctor_warns(self):
        self.write("openrouter", OPENROUTER)
        self.write("license", LICENSE)
        self.store.fail["read"] = credentials.Unavailable("KEYCHAIN_TOOL_UNAVAILABLE")
        self.assertTrue(credentials.migrate(self.home)["openrouter"].startswith("kept"))
        credentials.clear_cache()
        self.assertEqual(credentials.get("openrouter", self.home), OPENROUTER)
        line = doctor.check_credentials(self.home)
        self.assertEqual(line["status"], doctor.WARN)
        self.assertIn("in a file", line["line"])
        self.assertFalse((self.home / credentials.ID_FILE).exists())   # KC2: no id before a stored value
        self.assertNotIn("paste", (line["line"] + line["todo"]).lower())
        self.unlock()
        self.assertIn("still in a file", doctor.check_credentials(self.home)["line"])

    def test_no_os_store_keeps_the_0600_file_and_doctor_says_so(self):
        credentials.use_backend(credentials.NO_STORE)
        self.write("license", LICENSE)
        self.assertEqual(credentials.migrate(self.home), {"openrouter": "absent", "license": "no_store", "openai": "absent"})
        self.assertTrue(self.exists("license"))
        line = doctor.check_credentials(self.home)
        self.assertEqual(line["status"], doctor.WARN)
        self.assertIn("private file", line["line"])

    def test_a_key_changed_in_settings_is_written_to_the_store(self):
        self.write("openrouter", "sk-or-v1-the-old-key")
        credentials.migrate(self.home)
        new = "sk-or-v1-the-new-key-0123"
        state = setup_steps.save_and_test_openrouter(new, CONFIG, self.home,
                                                     transport=lambda url, data, headers, timeout: (200, OK_ANSWER))
        self.assertEqual(state["steps"]["openrouter"]["status"], "done")
        self.assertFalse(self.exists("openrouter"))
        self.assertEqual(self.store.values(), [new])
        self.assertTrue(setup_steps.openrouter_tested(self.home))

    def test_a_busy_lock_skips_this_start(self):
        import portable
        self.write("license", LICENSE)
        descriptor = portable.open_lock(self.home / credentials.LOCK_FILE)
        self.addCleanup(os.close, descriptor)
        self.assertTrue(portable.try_lock(descriptor))
        self.addCleanup(portable.unlock, descriptor)
        with mock.patch.object(credentials, "LOCK_WAIT_SECONDS", 0.2):
            self.assertEqual(credentials.migrate(self.home), {"skipped": "busy"})
        self.assertTrue(self.exists("license"))

    def test_a_copied_home_gets_its_own_id_and_keeps_its_keys(self):   # A6
        credentials.put("license", LICENSE, self.home)
        copy = self.root / "copy"
        shutil.copytree(self.home, copy)
        self.assertEqual(credentials.migrate(copy)["home"], "copied")
        ids = {json.loads((home / credentials.ID_FILE).read_text())["id"] for home in (self.home, copy)}
        self.assertEqual(len(ids), 2)
        credentials.clear_cache()
        self.assertEqual(credentials.get("license", copy), LICENSE)
        self.assertEqual(credentials.get("license", self.home), LICENSE)
        credentials.put("license", "SD-COPYCHANGED", copy)
        credentials.clear_cache()
        self.assertEqual(credentials.get("license", self.home), LICENSE)   # no longer shared

    def test_a_moved_home_gets_a_new_id_and_the_old_items_go(self):   # A6
        credentials.put("license", LICENSE, self.home)
        moved = self.root / "moved"
        self.home.rename(moved)
        self.assertEqual(credentials.migrate(moved)["home"], "moved")
        self.assertEqual(len(self.store.items), 1)
        credentials.clear_cache()
        self.assertEqual(credentials.get("license", moved), LICENSE)


class WaitingTest(Base):
    """KR1-KR7: a locked or unavailable store is a wait everywhere, never "no key"."""

    def setUp(self):
        super().setUp()
        credentials.put("openrouter", OPENROUTER, self.home)
        credentials.put("license", LICENSE, self.home)
        setup_steps.record(self.home, "openrouter", "done", "Key works.")
        setup_steps.record(self.home, "license", "done", "Active.")
        setup_steps.record(self.home, "linkedin", "done", "LinkedIn connected: Test.")

    def test_locked_holds_jobs_and_unavailable_holds_them_too(self):   # KR4
        self.lock()
        self.assertEqual(credentials.hold_reason(self.home), credentials.LOCKED_TEXT["darwin"])
        self.store.fail["read"] = credentials.Unavailable("KEYCHAIN_REFUSED: read exit 1")
        credentials.clear_cache()
        self.assertEqual(credentials.hold_reason(self.home), credentials.UNAVAILABLE_TEXT)
        spawned = []
        loop = scheduler_loop.Loop(spawner=spawned.append, is_paused=lambda: False, config={"timezone": "UTC"},
                                   credentials_hold=lambda: credentials.hold_reason(self.home))
        self.assertTrue(loop.tick()["paused"])
        self.assertEqual((spawned, loop.last_fired), ([], {}))
        self.unlock()
        self.assertEqual(loop.tick()["started"], list(scheduler_loop.JOBS))

    def test_no_step_reopens_and_no_failure_is_recorded(self):   # KR1, KR2, KR3, KR7
        before = setup_steps.load_state(self.home)["steps"]
        self.lock()
        import license_clock
        value = license_clock.view(self.home)
        self.assertEqual((value["state"], value["label"]), ("keychain", "Keychain locked"))
        self.assertTrue(setup_steps.step_done(5, setup_steps.load_state(self.home), self.home))
        self.assertTrue(setup_steps.step_done(1, setup_steps.load_state(self.home), self.home))
        setup_steps.test_saved_openrouter(CONFIG, self.home)
        setup_steps.save_and_test_openrouter("sk-or-v1-bad", CONFIG, self.home,
                                             transport=lambda *a: (_ for _ in ()).throw(AssertionError("called")))
        setup_steps.activate_license("SD-OTHERKEY", self.home, post=lambda *a: (_ for _ in ()).throw(AssertionError))
        setup_steps.linkedin_status(self.home, transport=lambda *a: (_ for _ in ()).throw(AssertionError("called")))
        self.assertEqual(setup_steps.load_state(self.home)["steps"], before)
        self.unlock()
        self.assertEqual(self.store.values(), sorted([LICENSE, OPENROUTER]))
        self.assertFalse(self.exists("openrouter") or self.exists("license"))

    def test_doctor_says_waiting_not_no_key(self):   # KR6
        self.lock()
        for check in (lambda: doctor.check_openrouter(self.home, CONFIG), lambda: doctor.check_license(self.home),
                      lambda: doctor.check_linkedin(self.home), lambda: doctor.check_credentials(self.home)):
            line = check()
            self.assertEqual(line["status"], doctor.WARN, line)
            self.assertNotIn("No ", line["line"])
            self.assertNotIn("paste", (line["line"] + line["todo"]).lower())

    @unittest.skipUnless(DASHBOARD, "dashboard packages (fastapi, httpx) are not installed")
    def test_locked_pages_show_the_wait_and_no_paste_box(self):   # KR1 renders
        import test_dashboard_overview as base
        from fastapi.testclient import TestClient
        import dashboard
        base.finished_home(self.home)
        setup_steps.record(self.home, "license", "done", "Active.")
        before = setup_steps.load_state(self.home)["steps"]
        self.lock()
        app = dashboard.create_app(base.SECRET, base.PORT, home=self.home, root=self.root, config=base.CONFIG)
        client = TestClient(app, base_url="http://127.0.0.1:%d" % base.PORT, follow_redirects=False)
        client.get("/", params={"t": base.SECRET})
        with mock.patch.object(setup_steps, "chrome_path", return_value="/chrome"):
            pages = {path: client.get(path) for path in ("/", "/settings", "/setup")}
        for path, response in pages.items():
            self.assertEqual(response.status_code, 200, path)
            page = response.text.replace("&#39;", "'")
            self.assertIn("Keychain locked", page, path)
            self.assertNotIn("No license", page, path)
            self.assertNotIn('name="key"', page, path)          # no paste box anywhere
            self.assertNotIn("Test saved key", page, path)
            self.assertNotIn("Setup is not finished", page, path)
        self.assertEqual(setup_steps.load_state(self.home)["steps"], before)
        hidden = client.get("/failures").text
        self.assertTrue("Failure details are hidden" in hidden or OPENROUTER not in hidden)
        self.unlock()
        self.assertIn('name="key"', client.get("/settings").text)

    @unittest.skipUnless(DASHBOARD, "dashboard packages (fastapi, httpx) are not installed")
    def test_settings_removes_the_saved_keys(self):   # A5
        import test_dashboard_overview as base
        from fastapi.testclient import TestClient
        import dashboard
        base.finished_home(self.home)
        app = dashboard.create_app(base.SECRET, base.PORT, home=self.home, root=self.root, config=base.CONFIG)
        client = TestClient(app, base_url="http://127.0.0.1:%d" % base.PORT, follow_redirects=False)
        client.get("/", params={"t": base.SECRET})
        response = client.post("/settings/keys/remove", headers={"hx-request": "true",
                                                                 "origin": "http://127.0.0.1:%d" % base.PORT})
        self.assertEqual(response.status_code, 200)
        self.assertIn("were removed from this computer", response.text)
        self.assertEqual(self.store.items, {})
        self.assertIsNone(setup_steps.license_key(self.home))


class ConfirmFixesTest(Base):
    """KC1-KC4 (keychain confirm review)."""

    def client(self):
        import test_dashboard_overview as base
        from fastapi.testclient import TestClient
        import dashboard
        app = dashboard.create_app(base.SECRET, base.PORT, home=self.home, root=self.root, config=base.CONFIG)
        client = TestClient(app, base_url="http://127.0.0.1:%d" % base.PORT, follow_redirects=False)
        client.get("/", params={"t": base.SECRET})
        self.headers = {"hx-request": "true", "origin": "http://127.0.0.1:%d" % base.PORT}
        return client

    def break_store(self):
        self.store.fail.update(read=credentials.Unavailable("KEYCHAIN_REFUSED: read exit 1"),
                               write=credentials.Unavailable("KEYCHAIN_REFUSED: write exit 1"))
        credentials.clear_cache()

    def test_a_broken_store_offers_the_folder_and_a_locked_one_never_does(self):   # KC1
        credentials.put("license", LICENSE, self.home)
        self.lock()
        for _ in range(credentials.BROKEN_READS + 1):
            credentials.clear_cache()
            credentials.hold_reason(self.home)
        self.assertFalse(credentials.offer_file(self.home))
        self.break_store()
        for _ in range(credentials.BROKEN_READS):
            credentials.clear_cache()
            credentials.hold_reason(self.home)
        self.assertTrue(credentials.offer_file(self.home))
        self.unlock()
        credentials.hold_reason(self.home)
        self.assertFalse(credentials.offer_file(self.home))   # a good answer clears it

    @unittest.skipUnless(DASHBOARD, "dashboard packages (fastapi, httpx) are not installed")
    def test_save_in_sidedoors_folder_instead(self):   # KC1
        credentials.put("openrouter", "sk-or-v1-older", self.home)
        self.break_store()
        client = self.client()
        typed = "sk-or-v1-typed-while-broken"
        response = client.post("/setup/openrouter", data={"key": typed}, headers=self.headers)   # first write attempt
        page = response.text.replace("&#39;", "'")
        self.assertIn("Save in Sidedoor's folder instead", page)
        self.assertIn('value="%s"' % typed, page)            # the typed key stays in its box
        self.assertFalse(self.exists("openrouter"))
        with mock.patch.object(text_model, "_post", return_value=(200, OK_ANSWER)):
            client.post("/setup/openrouter", data={"key": typed, "to_file": "1"}, headers=self.headers)
        self.assertEqual(stat.S_IMODE((self.home / "openrouter.key").stat().st_mode), PRIVATE)
        self.assertEqual(credentials.get("openrouter", self.home), typed)
        self.assertEqual(credentials.status("openrouter", self.home), "file_chosen")
        self.assertIn("Sidedoor's folder instead", doctor.check_credentials(self.home)["todo"])   # the license still waits
        credentials.put_file("license", LICENSE, self.home)
        line = doctor.check_credentials(self.home)["line"]
        self.assertIn("private file", line)
        self.assertIn("as you chose", line)
        self.unlock()
        self.assertEqual(self.store.values(), ["sk-or-v1-older"])
        credentials.delete("license", self.home)
        self.assertEqual(credentials.get("openrouter", self.home), typed)   # the user's choice wins until moved
        self.assertEqual(credentials.migrate(self.home)["openrouter"], "moved")
        self.assertFalse(self.exists("openrouter"))
        credentials.clear_cache()
        self.assertEqual(credentials.get("openrouter", self.home), typed)
        self.assertEqual(self.store.values(), [typed])   # the user's newer choice replaced the older key

    def test_a_first_save_while_locked_is_refused_and_writes_no_id(self):   # KC2
        security = FakeSecurity(self)
        credentials.use_backend(credentials.MacKeychain(security=security.path, unlocked=lambda: False))
        state = setup_steps.save_and_test_openrouter(OPENROUTER, CONFIG, self.home)
        self.assertEqual(state["keychain_refused"], "Unlock your keychain, then click Save.")
        state = setup_steps.activate_license(LICENSE, self.home, post=lambda *a: (_ for _ in ()).throw(AssertionError))
        self.assertEqual(state["keychain_refused"], "Unlock your keychain, then click Save.")
        self.assertEqual(sorted(path.name for path in self.home.iterdir()), [])   # no id, no key, no state
        self.assertEqual(security.calls(), [])

    @unittest.skipUnless(DASHBOARD, "dashboard packages (fastapi, httpx) are not installed")
    def test_the_refusal_keeps_the_typed_key_in_the_box(self):   # KC2
        security = FakeSecurity(self)
        credentials.use_backend(credentials.MacKeychain(security=security.path, unlocked=lambda: False))
        client = self.client()
        for path, number in (("/setup/openrouter", 1), ("/setup/license", 5)):
            page = client.post(path, data={"key": "SD-TYPEDKEY0123"}, headers=self.headers).text
            self.assertIn("Unlock your keychain, then click Save.", page)
            self.assertIn('value="SD-TYPEDKEY0123"', page)
        self.assertFalse((self.home / credentials.ID_FILE).exists())
        self.assertEqual(security.calls(), [])

    @unittest.skipUnless(DASHBOARD, "dashboard packages (fastapi, httpx) are not installed")
    def test_settings_test_and_activate_record_nothing_while_locked(self):   # KC3
        import test_dashboard_overview as base
        credentials.put("openrouter", OPENROUTER, self.home)
        credentials.put("license", LICENSE, self.home)
        base.finished_home(self.home)
        before = setup_steps.load_state(self.home)["steps"]
        client = self.client()
        self.lock()
        for path in ("/settings/openrouter/test", "/settings/license/activate"):
            self.assertEqual(client.post(path, headers=self.headers).status_code, 200)
        self.assertEqual(setup_steps.load_state(self.home)["steps"], before)

    @unittest.skipIf(os.name == "nt", "POSIX fake executable (the macOS keychain tool)")
    def test_every_call_names_the_default_keychain(self):   # KC4
        security = FakeSecurity(self)
        keychain = "/Users/someone/Library/Keychains/login.keychain-db"
        credentials.use_backend(credentials.MacKeychain(security=security.path, unlocked=lambda: (True, keychain)))
        credentials.put("license", LICENSE, self.home)
        credentials.clear_cache()
        credentials.get("license", self.home)
        credentials.delete("license", self.home)
        calls = security.calls()
        self.assertEqual({call["argv"][1] for call in calls},
                         {"find-generic-password", "-i", "delete-generic-password"})
        for call in calls:
            if call["stdin"]:
                self.assertTrue(call["stdin"].rstrip("\n").endswith(' "%s"' % keychain))
            else:
                self.assertEqual(call["argv"][-1], keychain)


class FakeSecurity:
    """A fake `/usr/bin/security` in a temporary folder, so MacKeychain's own code runs."""

    def __init__(self, testcase):
        directory = tempfile.TemporaryDirectory()
        testcase.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.path = self.folder / "security"
        self.path.write_text(FAKE_SECURITY % {"python": sys.executable})
        self.path.chmod(0o755)

    def mode(self, value):
        (self.folder / "mode").write_text(value)

    def calls(self):
        log = self.folder / "calls.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


@unittest.skipIf(os.name == "nt", "POSIX fake executable")
class MacKeychainTest(Base):
    def setUp(self):
        super().setUp()
        self.security = FakeSecurity(self)
        self.state = {"unlocked": True}
        self.backend = credentials.MacKeychain(security=self.security.path, timeout=2,
                                               unlocked=lambda: self.state["unlocked"])
        credentials.use_backend(self.backend)

    def test_round_trip_never_puts_the_value_on_argv(self):
        self.assertEqual(credentials.put("openrouter", OPENROUTER, self.home), "store")
        credentials.clear_cache()
        self.assertEqual(credentials.get("openrouter", self.home), OPENROUTER)
        calls = self.security.calls()
        self.assertTrue(calls)
        for call in calls:
            self.assertFalse(any(OPENROUTER in argument for argument in call["argv"]))
            self.assertFalse(any(OPENROUTER.encode().hex() in argument for argument in call["argv"]))
        writes = [call for call in calls if call["stdin"]]
        self.assertEqual(len(writes), 1)
        self.assertIn(OPENROUTER.encode().hex(), writes[0]["stdin"])
        self.assertNotIn(OPENROUTER, writes[0]["stdin"])
        credentials.delete("openrouter", self.home)
        credentials.clear_cache()
        self.assertIsNone(credentials.get("openrouter", self.home))

    def test_a_non_ascii_value_round_trips(self):   # A8
        value = "sk-or-v1-ünïcode-key"
        self.assertEqual(credentials.put("openrouter", value, self.home), "store")
        credentials.clear_cache()
        self.assertEqual(credentials.get("openrouter", self.home), value)
        self.assertEqual(credentials._decode("deadbeef"), "deadbeef")   # a printable hex-looking value stays

    def test_a_locked_keychain_starts_no_security_process_at_all(self):   # KR5, A1
        credentials.put("license", LICENSE, self.home)
        before = len(self.security.calls())
        self.state["unlocked"] = False
        credentials.clear_cache()
        for attempt in (lambda: credentials.get("license", self.home), lambda: credentials.put("license", "SD-X", self.home),
                        lambda: credentials.delete("license", self.home), lambda: self.backend.write("0" * 32 + ":license", "v")):
            with self.assertRaises(credentials.Locked):
                attempt()
        self.assertEqual(credentials.migrate(self.home)["home"], "same")
        self.assertIsNotNone(credentials.hold_reason(self.home))
        self.assertEqual(len(self.security.calls()), before)   # not one prompt-capable call

    def test_a_hanging_security_times_out_and_leaves_no_output_behind(self):   # R3, A9
        credentials.put("license", LICENSE, self.home)
        credentials.clear_cache()
        self.security.mode("hang")
        self.backend.timeout = 0.5
        started = time.monotonic()
        with self.assertRaises(credentials.Locked) as caught:
            credentials.get("license", self.home)
        self.assertLess(time.monotonic() - started, 5)
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(setup_steps.license_key(self.home))
        started = time.monotonic()
        self.assertIsNotNone(credentials.hold_reason(self.home))   # cached: no second wait
        self.assertLess(time.monotonic() - started, 0.4)

    def test_a_missing_tool_is_unavailable(self):
        backend = credentials.MacKeychain(security=self.security.folder / "absent", unlocked=lambda: True)
        with self.assertRaises(credentials.Unavailable):
            backend.read("x:license")


class FakeWindowsApi:
    def __init__(self):
        self.items = {}
        self.error = None

    def read(self, target):
        if self.error:
            raise self.error
        return self.items.get(target)

    def write(self, target, blob):
        self.items[target] = blob

    def delete(self, target):
        self.items.pop(target, None)


class WindowsCredentialsTest(Base):
    def test_round_trip_through_the_api_seam(self):
        api = FakeWindowsApi()
        credentials.use_backend(credentials.WindowsCredentials(api))
        value = "sk-or-v1-ünïcode-key"
        self.assertEqual(credentials.put("openrouter", value, self.home), "store")
        install = json.loads((self.home / credentials.ID_FILE).read_text())["id"]
        self.assertEqual(api.items, {"Sidedoor/%s/openrouter" % install: value.encode("utf-8")})
        credentials.clear_cache()
        self.assertEqual(credentials.get("openrouter", self.home), value)

    def test_no_logon_session_holds_with_the_windows_text(self):   # A2
        api = FakeWindowsApi()
        credentials.use_backend(credentials.WindowsCredentials(api))
        credentials.put("license", LICENSE, self.home)
        api.error = credentials.Unavailable("CREDENTIAL_READ_FAILED: 1312")
        credentials.clear_cache()
        self.assertEqual(credentials.hold_reason(self.home), credentials.LOCKED_TEXT["win32"])

    def test_no_advapi32_is_unavailable(self):
        backend = credentials.WindowsCredentials()
        with mock.patch.object(credentials, "_real_store_allowed", return_value=True), \
                mock.patch.object(credentials, "_WindowsApi", side_effect=OSError("no advapi32")):
            with self.assertRaises(credentials.Unavailable):
                backend.read("x:license")


if __name__ == "__main__":
    unittest.main()
