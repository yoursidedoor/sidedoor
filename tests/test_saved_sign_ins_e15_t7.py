"""E15.T7 saved sign-ins, by outcome: the vault (one key, a sealed file, the cap, the 90-day rule), the
guard's fill tool (only on the site, and the value reaches nothing the AI or the disk can show), and the
Saved sign-ins page.

No browser: `Page` answers the guard's own fill call the way the real page would (the same checks the
Playwright code makes); `FillCode` runs that code itself under Node against a stand-in page, where Node is
installed.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, the OS credential store off)
import html
import importlib.util
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import application_browser  # noqa: E402
import applications  # noqa: E402
import browser_guard  # noqa: E402
import browser_runtime  # noqa: E402
import credentials  # noqa: E402
import log_retention  # noqa: E402
import model_calls  # noqa: E402
import saved_sign_ins  # noqa: E402
import saved_sign_ins_page  # noqa: E402
import setup_steps  # noqa: E402
import site_sessions  # noqa: E402
import user_profile  # noqa: E402
from manual_application_browser import Fixture, request_at, write_output  # noqa: E402

MARKER = 'Mk7"pw<&>%+ *!~\'()/=?zebra-41c9e0-quartz'   # the password: quotes, markup and URL punctuation on purpose
EMAIL = "candidate@example.test"
SITE = "jobs.ashbyhq.com"                          # the Fixture's job is on https://jobs.ashbyhq.com/synthetic/role-123
OTHER = "login.other-site.test"
DAY = 86400
FORMS = browser_guard.secret_forms(MARKER)


def answer(value):
    return {"result": {"content": [{"type": "text", "text": "### Result\n" + json.dumps(value)}]}}


class Page:
    """The page the guard's fill call meets: an address, and fields by snapshot ref (the frame each is in
    and whether it is a password field). It makes the checks the Playwright code makes, in its order."""

    def __init__(self, url="https://jobs.ashbyhq.com/synthetic/role-123", fields=None):
        self.url, self.typed, self.codes = url, {}, []
        self.fields = fields if fields is not None else {"e1": (url, "email"), "e2": (url, "password")}

    def call(self, code):
        self.codes.append(code)
        given = json.loads(re.search(r"const input = (.*);\n", code).group(1))
        if site_sessions.site_key(self.url) != given["site"]:
            return [{"outcome": "wrong_site"}]
        chosen = []
        for kind in ("email", "password"):
            ref = given[kind + "_ref"]
            if not ref:
                continue
            if ref not in self.fields:
                return [{"outcome": "not_found", "field": kind}]
            frame, field = self.fields[ref]
            if site_sessions.site_key(frame) != given["site"]:
                return [{"outcome": "wrong_site", "field": kind}]
            if kind == "password" and field != "password":
                return [{"outcome": "not_password_field"}]
            chosen.append((kind, ref))
        for kind, ref in chosen:
            self.typed[ref] = given[kind]
        return [{"outcome": "filled", "email": any(kind == "email" for kind, _ in chosen),
                 "password": any(kind == "password" for kind, _ in chosen)}]


class Home(unittest.TestCase):
    """A fresh Sidedoor home per test, as the app finds it (SIDEDOOR_HOME, no explicit home argument)."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="saved-sign-ins-home-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        patch = mock.patch.dict(os.environ, {user_profile.HOME_VARIABLE: str(self.home)})
        patch.start()
        self.addCleanup(patch.stop)
        credentials.clear_cache()
        site_sessions._reported.clear()
        site_sessions.before_session(SITE)      # the application job: the one place the vault key is made

    def sites(self):
        return [row["site"] for row in saved_sign_ins.listing()["rows"]]


class Vault(Home):
    def test_a_sign_in_is_sealed_under_the_one_key_and_adding_sites_adds_no_key(self):
        key_before = credentials.get("vault")
        names_before = dict(credentials.NAMES)
        self.assertEqual(saved_sign_ins.add(SITE, EMAIL, MARKER, saved_sign_ins.SIDEDOOR, now=100.0), "saved")
        self.assertEqual(saved_sign_ins.add(OTHER, EMAIL, "another-password", saved_sign_ins.USER, now=200.0), "saved")
        self.assertEqual(credentials.get("vault"), key_before)                 # one key, unchanged
        self.assertEqual(credentials.NAMES, names_before)
        self.assertEqual(sorted(path.name for path in self.home.iterdir() if "key" in path.name),
                         ["vault-key-made", "vault.key"])                      # no per-site key anywhere
        blob = (self.home / saved_sign_ins.STORE_FILE).read_bytes()
        for form in FORMS + [EMAIL]:
            self.assertNotIn(form.encode(), blob)                              # not in clear
        key = bytes.fromhex(key_before)
        self.assertIn(MARKER, json.loads(site_sessions.unseal(key, blob, saved_sign_ins.PURPOSE))["sites"][SITE]["password"])
        self.assertIsNone(site_sessions.unseal(key, blob))                     # not a site-sessions file
        self.assertEqual(saved_sign_ins.for_fill(SITE), (EMAIL, MARKER))
        self.assertTrue(saved_sign_ins.has(SITE))
        self.assertFalse(saved_sign_ins.has("unknown.example.test"))
        rows = saved_sign_ins.listing()
        self.assertFalse(rows["unavailable"])
        self.assertEqual([(row["site"], row["email"], row["made_by"], row["has_password"]) for row in rows["rows"]],
                         [(SITE, EMAIL, "SIDEDOOR", True), (OTHER, EMAIL, "USER", True)])
        self.assertNotIn(MARKER, json.dumps(rows))                             # the listing never carries a password

    def test_what_is_not_a_sign_in_is_not_saved_and_no_key_means_nothing_is_written(self):
        for site, email, password, made_by in ((None, EMAIL, "p", "USER"), ("Not A Host", EMAIL, "p", "USER"),
                                               (SITE, "", "p", "USER"), (SITE, EMAIL, "", "USER"), (SITE, EMAIL, "p", "AI")):
            self.assertEqual(saved_sign_ins.add(site, email, password, made_by), "invalid")
        self.assertFalse((self.home / saved_sign_ins.STORE_FILE).exists())
        saved_sign_ins.add(SITE, EMAIL, MARKER, saved_sign_ins.USER)
        (self.home / "vault.key").unlink()                                     # the key is lost: never re-made
        credentials.clear_cache()
        self.assertEqual(saved_sign_ins.add(OTHER, EMAIL, "p", saved_sign_ins.USER), "unavailable")
        self.assertIsNone(saved_sign_ins.for_fill(SITE))
        self.assertEqual(saved_sign_ins.listing(), {"rows": [], "unavailable": True})
        self.assertEqual(saved_sign_ins.remove_all(), 0)
        self.assertTrue((self.home / saved_sign_ins.STORE_FILE).exists())      # left as it is
        self.assertFalse((self.home / "vault.key").exists())

    def test_over_200_the_one_unused_for_longest_goes_first(self):
        self.assertEqual(saved_sign_ins.MAX_SIGN_INS, 200)
        with mock.patch.object(saved_sign_ins, "MAX_SIGN_INS", 3):
            for index, site in enumerate(("a.example.test", "b.example.test", "c.example.test")):
                saved_sign_ins.add(site, EMAIL, "p%d" % index, saved_sign_ins.SIDEDOOR, now=100.0 + index)
            saved_sign_ins.used("a.example.test", now=500.0)                   # a fill counts as use
            saved_sign_ins.add("d.example.test", EMAIL, "p3", saved_sign_ins.SIDEDOOR, now=600.0)
        self.assertEqual(self.sites(), ["a.example.test", "c.example.test", "d.example.test"])

    def test_a_password_goes_after_90_days_without_an_open_job_and_the_row_stays_marked(self):
        self.assertEqual(saved_sign_ins.PRUNE_SECONDS, 90 * DAY)
        saved_sign_ins.add(SITE, EMAIL, MARKER, saved_sign_ins.SIDEDOOR, now=0.0)
        saved_sign_ins.add(OTHER, EMAIL, "p", saved_sign_ins.USER, now=0.0)
        self.assertEqual(saved_sign_ins.prune(set(), now=89 * DAY), 0)
        self.assertEqual(saved_sign_ins.prune({OTHER}, now=89 * DAY), 0)      # an open job restarts that site's clock
        self.assertEqual(saved_sign_ins.prune(None, now=400 * DAY), 0)        # jobs unreadable: nothing is removed
        self.assertEqual(saved_sign_ins.prune(set(), now=91 * DAY), 1)
        rows = {row["site"]: row for row in saved_sign_ins.listing()["rows"]}
        self.assertEqual((rows[SITE]["has_password"], rows[SITE]["removed_at"], rows[SITE]["email"]), (False, 91 * DAY, EMAIL))
        self.assertTrue(rows[OTHER]["has_password"])
        self.assertIsNone(saved_sign_ins.for_fill(SITE))
        self.assertFalse(saved_sign_ins.has(SITE))
        self.assertNotIn(MARKER.encode(), site_sessions.unseal(bytes.fromhex(credentials.get("vault")),
                         (self.home / saved_sign_ins.STORE_FILE).read_bytes(), saved_sign_ins.PURPOSE))
        self.assertEqual(saved_sign_ins.prune(set(), now=(89 + 90) * DAY + 1), 1)   # 90 days after its last open job
        self.assertEqual(saved_sign_ins.add(SITE, EMAIL, "new", saved_sign_ins.SIDEDOOR, now=200 * DAY), "saved")
        self.assertTrue(saved_sign_ins.has(SITE))                              # saved again: the mark is gone
        self.assertIsNone({row["site"]: row for row in saved_sign_ins.listing()["rows"]}[SITE]["removed_at"])

    def test_the_daily_sweep_reads_the_open_jobs_from_the_install(self):
        root = Path(tempfile.mkdtemp(prefix="saved-sign-ins-root-"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / "state").mkdir()
        self.assertEqual(saved_sign_ins.open_sites(root), set())               # no database yet
        database = sqlite3.connect(root / "state" / "orchestrator.sqlite3")
        applications.initialize(database)
        for key, url, state in (("open", "https://www.jobs.ashbyhq.com/acme/1", "QUEUED"),
                                ("done", "https://login.other-site.test/role", "SUBMITTED"),
                                ("bad", "not a url", "QUEUED")):
            database.execute("INSERT INTO application_jobs(job_key,employer,requisition,payload,state,updated_at) "
                             "VALUES (?,?,?,?,?,?)", (key, "Acme", key, json.dumps({"url": url}), state, "2026-11-13T00:00:00Z"))
        database.commit()
        database.close()
        self.assertEqual(saved_sign_ins.open_sites(root), {SITE})
        saved_sign_ins.add(SITE, EMAIL, MARKER, saved_sign_ins.SIDEDOOR, now=0.0)
        saved_sign_ins.add(OTHER, EMAIL, "p", saved_sign_ins.SIDEDOOR, now=0.0)
        with mock.patch.object(log_retention, "retain", return_value={"rotated": [], "removed": []}), \
                mock.patch.object(log_retention, "prune_traces", return_value={"removed": 0}):
            log_retention.startup(root)                                        # the real clock: far past 90 days
        self.assertTrue(saved_sign_ins.has(SITE))                              # an open job there
        self.assertFalse(saved_sign_ins.has(OTHER))
        self.assertEqual(self.sites(), [SITE, OTHER])                          # the row stays
        (root / "state" / "orchestrator.sqlite3").write_bytes(b"not a database")
        self.assertIsNone(saved_sign_ins.open_sites(root))

    def test_remove_and_remove_all(self):
        saved_sign_ins.add(SITE, EMAIL, MARKER, saved_sign_ins.SIDEDOOR)
        saved_sign_ins.add(OTHER, EMAIL, "p", saved_sign_ins.USER)
        self.assertEqual(saved_sign_ins.remove("unknown.example.test"), 0)
        self.assertEqual(saved_sign_ins.remove(SITE), 1)
        self.assertEqual(self.sites(), [OTHER])
        self.assertIsNone(saved_sign_ins.for_fill(SITE))
        saved_sign_ins.add(SITE, EMAIL, MARKER, saved_sign_ins.SIDEDOOR)
        self.assertEqual(saved_sign_ins.remove_all(), 2)
        self.assertEqual(self.sites(), [])
        self.assertEqual(saved_sign_ins.remove_all(), 0)
        self.assertTrue(credentials.get("vault"))                              # the key stays for site sessions

    def test_kept_when_the_license_goes_and_gone_with_the_vault_key(self):     # D4; credentials.delete("vault")
        saved_sign_ins.add(SITE, EMAIL, MARKER, saved_sign_ins.SIDEDOOR)
        credentials.put("license", "license-key-value")
        credentials.delete("license")
        self.assertEqual(saved_sign_ins.for_fill(SITE), (EMAIL, MARKER))
        credentials.delete("vault")
        self.assertFalse((self.home / saved_sign_ins.STORE_FILE).exists())
        self.assertEqual(saved_sign_ins.listing(), {"rows": [], "unavailable": False})


class GuardCase(Home):
    def proxy(self, page, site=SITE, directory=None, **options):
        if directory is None:
            directory = Path(tempfile.mkdtemp(prefix="saved-sign-ins-guard-"))
            self.addCleanup(shutil.rmtree, directory, True)
        self.out = io.StringIO()
        with mock.patch.object(browser_guard.subprocess, "Popen"):
            proxy = browser_guard.Proxy("inspect", ["node", "cli.js"], audit=directory / "guard.jsonl",
                                        stdin=io.StringIO(""), stdout=self.out, focus_keeper=False,
                                        saved_sign_in=site, **options)
        self.addCleanup(proxy.audit.close)
        proxy.child_call = lambda method, params: answer(page.call(params["arguments"]["code"]))
        self.forwarded = []
        proxy.to_child = self.forwarded.append
        proxy.audit_path = directory / "guard.jsonl"
        return proxy

    def fill(self, proxy, ident=1, **arguments):
        proxy.handle({"jsonrpc": "2.0", "id": ident, "method": "tools/call",
                      "params": {"name": browser_guard.SAVED_SIGN_IN_TOOL, "arguments": arguments}})
        return json.loads(self.out.getvalue().strip().split("\n")[-1])["result"]

    def audit(self, proxy):
        proxy.audit.flush()
        return [json.loads(line) for line in proxy.audit_path.read_text().splitlines()]


class Guard(GuardCase):
    def test_the_guard_types_the_saved_sign_in_and_answers_with_a_fixed_sentence(self):
        saved_sign_ins.add(SITE, EMAIL, MARKER, saved_sign_ins.SIDEDOOR, now=10.0)
        page = Page()
        proxy = self.proxy(page)
        result = self.fill(proxy, email_target="e1", password_target="e2")
        self.assertEqual(page.typed, {"e1": EMAIL, "e2": MARKER})              # the control: it really was typed
        self.assertEqual((result["isError"], result["content"][0]["text"]),
                         (False, browser_guard.SAVED_SIGN_IN_TEXT["filled"]))
        self.assertEqual(self.forwarded, [])                                   # the guard's own call, nothing forwarded
        rows = [row for row in self.audit(proxy) if row["decision"] == "SAVED_SIGN_IN"]
        self.assertEqual([{k: v for k, v in row.items() if k not in ("at", "began")} for row in rows],
                         [{"decision": "SAVED_SIGN_IN", "tool": "fill_saved_sign_in", "site": SITE, "outcome": "filled",
                           "email": True, "password": True}])
        self.assertGreater({row["site"]: row for row in saved_sign_ins.listing()["rows"]}[SITE]["used_at"], 10.0)

    def test_a_page_on_another_host_gets_nothing_whatever_it_or_the_ai_says(self):
        saved_sign_ins.add(SITE, EMAIL, MARKER, saved_sign_ins.SIDEDOOR)
        saved_sign_ins.add(OTHER, EMAIL, "the-other-password", saved_sign_ins.SIDEDOOR)
        cases = (
            Page("https://login.other-site.test/sign-in"),                     # the job's site is A, the page is on B
            Page("https://jobs.ashbyhq.com.other-site.test/"),                 # a look-alike host
            Page("https://careers.ashbyhq.com/"),                              # another subdomain is another site
            Page(fields={"e1": ("https://jobs.ashbyhq.com/a", "email"),        # a frame from B inside a page on A
                         "e2": ("https://login.other-site.test/frame", "password")}),
            Page("about:blank"),
        )
        for page in cases:
            proxy = self.proxy(page)
            result = self.fill(proxy, email_target="e1", password_target="e2", site=OTHER, url="https://" + SITE)
            self.assertEqual(page.typed, {}, page.url)                         # nothing typed, not even the email
            self.assertEqual((result["isError"], result["content"][0]["text"]),
                             (True, browser_guard.SAVED_SIGN_IN_TEXT["wrong_site"]), page.url)
            self.assertEqual([row["outcome"] for row in self.audit(proxy) if row["decision"] == "SAVED_SIGN_IN"], ["wrong_site"])
        # The other way round: the session is for B's page, the vault holds only A's sign-in.
        saved_sign_ins.remove(OTHER)
        page = Page("https://login.other-site.test/sign-in")
        result = self.fill(self.proxy(page, site=OTHER), email_target="e1", password_target="e2")
        self.assertEqual((page.typed, page.codes), ({}, []))                   # the browser was never asked
        self.assertEqual(result["content"][0]["text"], browser_guard.SAVED_SIGN_IN_TEXT["none"])

    def test_the_password_goes_only_into_a_password_field_and_bad_calls_type_nothing(self):
        saved_sign_ins.add(SITE, EMAIL, MARKER, saved_sign_ins.SIDEDOOR)
        page = Page()
        proxy = self.proxy(page)
        for arguments, outcome in (({"email_target": "e2", "password_target": "e1"}, "not_password_field"),
                                   ({"password_target": "e9"}, "not_found"),
                                   ({}, "malformed"), ({"password_target": "input[type=password]"}, "malformed"),
                                   ({"password_target": ["e2"]}, "malformed")):
            result = self.fill(proxy, **arguments)
            self.assertEqual((result["isError"], result["content"][0]["text"]),
                             (True, browser_guard.SAVED_SIGN_IN_TEXT[outcome]), arguments)
        self.assertEqual(page.typed, {})
        self.assertEqual(self.fill(proxy, password_target="e2")["isError"], False)   # the password step of a two-step sign-in
        self.assertEqual(page.typed, {"e2": MARKER})

    def test_the_tool_is_held_only_by_a_session_started_for_a_site(self):
        self.assertIn("fill_saved_sign_in", self.proxy(Page()).offered())
        self.assertEqual([tool["name"] for tool in self.proxy(Page()).own_tools()], ["fill_saved_sign_in"])
        plain = self.proxy(Page(), site=None)
        self.assertNotIn("fill_saved_sign_in", plain.offered())
        self.assertEqual(plain.own_tools(), [])
        saved_sign_ins.add(SITE, EMAIL, MARKER, saved_sign_ins.SIDEDOOR)
        page = Page()
        result = self.fill(plain, email_target="e1", password_target="e2")
        self.assertIn("not available in this session", result["content"][0]["text"])
        self.assertEqual(page.typed, {})
        # The command line: only a session whose config names the site gets the flag.
        directory = Path(tempfile.mkdtemp(prefix="saved-sign-ins-command-"))
        self.addCleanup(shutil.rmtree, directory, True)
        self.assertNotIn("--saved-sign-in", browser_runtime.guard_command({}, directory, "inspect"))
        command = browser_runtime.guard_command({saved_sign_ins.CONFIG_KEY: SITE}, directory, "inspect")
        self.assertEqual(command[command.index("--saved-sign-in") + 1], SITE)
        self.assertNotIn("--saved-sign-in", browser_runtime.guard_command({saved_sign_ins.CONFIG_KEY: "Not A Host"}, directory, "inspect"))

    def test_a_value_the_page_shows_afterwards_is_removed_from_what_the_ai_and_the_audit_get(self):
        saved_sign_ins.add(SITE, EMAIL, MARKER, saved_sign_ins.SIDEDOOR)
        proxy = self.proxy(Page(), piece_chars=1200)
        shown = "- textbox \"Password\": " + json.dumps(MARKER) + "\nbody: password=" + urllib.parse.quote_plus(MARKER) \
            + "\n" + html.escape(MARKER) + "\n" + MARKER
        before = browser_guard.without_secrets({"text": shown}, proxy.typed)
        self.assertIn(MARKER, before["text"])                                  # nothing typed yet: nothing to remove
        self.fill(proxy, email_target="e1", password_target="e2")
        cleaned = browser_guard.without_secrets({"result": {"content": [{"type": "text", "text": shown}]}}, proxy.typed)
        text = cleaned["result"]["content"][0]["text"]
        self.assertEqual(text.count(browser_guard.SAVED_SIGN_IN_MARK), 4)
        for form in FORMS:
            self.assertNotIn(form, text)
        # Through the guard's own relay, cut into pieces: no piece and no audit row carries any spelling.
        proxy.child = mock.Mock()
        long = (shown + "\n" + "x" * 300 + "\n") * 12
        proxy.child.stdout = [json.dumps({"jsonrpc": "2.0", "id": 7, "result": {"content": [{"type": "text", "text": long}]}}) + "\n"]
        proxy.pump_child(proxy.child)
        for _ in range(40):
            proxy.handle({"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "browser_continue", "arguments": {}}})
        proxy.log(decision="FORWARDED", tool="browser_type", arguments={"text": MARKER})
        proxy.audit.flush()
        relayed = self.out.getvalue() + proxy.audit_path.read_text()
        self.assertGreater(relayed.count(browser_guard.SAVED_SIGN_IN_MARK), 40)
        for form in FORMS:
            self.assertNotIn(form, relayed)


PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\xff\xfe"


class OutputFolder(GuardCase):
    """What the page tools' `filename` writes into the session's output folder, after a fill."""

    def session(self):
        saved_sign_ins.add(SITE, EMAIL, MARKER, saved_sign_ins.SIDEDOOR)
        self.directory = Path(tempfile.mkdtemp(prefix="saved-sign-ins-output-"))
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.folder = self.directory / "browser"
        self.folder.mkdir()
        (self.folder / "before.png").write_bytes(PNG)
        os.utime(self.folder / "before.png", (1000, 1000))                     # a screenshot from before the fill
        self.page = Page()
        with mock.patch.object(browser_guard.Proxy, "__init__", self.with_folder(browser_guard.Proxy.__init__)):
            return self.proxy(self.page, directory=self.directory)

    def with_folder(self, real):
        folder = self.folder

        def start(proxy, operation, child_argv, *args, **options):
            real(proxy, operation, list(child_argv) + ["--output-dir", str(folder), "--config", "x.json"], *args, **options)
        return start

    def written(self):
        """The model snapshots to a file after pressing the site's own show-password control, saves the
        network log and takes a screenshot."""
        (self.folder / "shown.yml").write_text("- textbox \"Password\": " + json.dumps(MARKER) + "\n")
        (self.folder / "deep").mkdir(exist_ok=True)
        (self.folder / "deep" / "network.txt").write_text("\n".join(spellings().values()))
        (self.folder / "plain.txt").write_text("nothing secret here\n")
        (self.folder / "shot.png").write_bytes(PNG)

    def leaked(self):
        return sorted(path.name for path in self.folder.rglob("*") if path.is_file()
                      and any(form.encode() in path.read_bytes() for form in FORMS + ["zebra-41c9e0", "emVicmEtNDFjOWUw"]))

    def test_a_file_a_tool_wrote_is_clean_before_it_can_be_read_back_and_images_go_at_the_end(self):
        proxy = self.session()
        self.assertEqual(proxy.output_folder, str(self.folder))
        self.written()
        proxy.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "browser_snapshot", "arguments": {}}})
        self.assertEqual(self.leaked(), ["network.txt", "shown.yml"])          # the control: no fill yet, nothing is touched
        with proxy.idle:
            proxy.in_flight.clear()
        self.fill(proxy, ident=2, email_target="e1", password_target="e2")
        self.written()
        self.assertEqual(self.leaked(), ["network.txt", "shown.yml"])          # the control: the files do hold it
        # The answer to the call that wrote them is relayed only after the files are clean.
        proxy.child = mock.Mock()
        proxy.child.stdout = [json.dumps({"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": "saved shown.yml"}]}}) + "\n"]
        proxy.pump_child(proxy.child)
        self.assertEqual(self.leaked(), [])
        self.assertIn(browser_guard.SAVED_SIGN_IN_MARK, (self.folder / "shown.yml").read_text())
        self.assertEqual((self.folder / "plain.txt").read_text(), "nothing secret here\n")
        self.assertTrue((self.folder / "shot.png").exists())                   # an image stays while the session runs
        # And before the next call that could read one back.
        self.written()
        proxy.requests.get_nowait()                                            # the relay's end marker
        proxy.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "browser_file_upload", "arguments": {}}})
        self.assertEqual(self.leaked(), [])
        # The session ends: text clean, images taken since the fill deleted, the earlier one kept.
        self.written()
        proxy.child.stdout = []
        proxy.run()
        self.assertEqual(self.leaked(), [])
        self.assertEqual(sorted(path.name for path in self.folder.rglob("*") if path.is_file()),
                         ["before.png", "network.txt", "plain.txt", "shown.yml"])
        cleaned = [row for row in self.audit(proxy) if row["decision"] == "SAVED_SIGN_IN_OUTPUT_CLEANED"]
        self.assertEqual([(row["files"], row["removed"], row["final"]) for row in cleaned], [(2, 0, False), (2, 0, False), (2, 1, True)])
        self.assertNotIn("zebra", proxy.audit_path.read_text())

    def test_the_application_job_cleans_the_folder_of_a_guard_that_was_stopped_hard(self):
        proxy = self.session()
        self.written()
        self.assertEqual(saved_sign_ins.clean_output(self.folder, SITE, proxy.audit_path), (2, 0))   # no fill: images stay
        self.fill(proxy, email_target="e1", password_target="e2")
        proxy.audit.flush()
        self.written()
        self.assertEqual(saved_sign_ins.clean_output(self.folder, None, proxy.audit_path), (0, 0))   # a session without the tool
        self.assertEqual(self.leaked(), ["network.txt", "shown.yml"])
        self.assertEqual(saved_sign_ins.clean_output(self.folder, SITE, proxy.audit_path), (2, 1))
        self.assertEqual(self.leaked(), [])
        self.assertEqual(sorted(path.name for path in self.folder.glob("*.png")), ["before.png"])
        self.written()
        self.assertEqual(saved_sign_ins.clean_output(self.folder, SITE, self.directory / "missing.jsonl"), (2, 2))   # no audit: every image goes

    def test_a_fill_the_browser_never_answered_is_not_called_failed(self):
        proxy = self.session()
        proxy.child_call = lambda method, params: None                         # the 30 s limit ran out
        result = self.fill(proxy, email_target="e1", password_target="e2")
        self.assertEqual(result["content"][0]["text"], browser_guard.SAVED_SIGN_IN_TEXT["unconfirmed"])
        self.assertIn("may or may not have been typed", result["content"][0]["text"])
        self.assertTrue(proxy.typed and proxy.filled_at)                       # treated as typed from then on
        self.written()
        proxy.clean_output(final=True)
        self.assertEqual((self.leaked(), (self.folder / "shot.png").exists()), ([], False))


def browser_form(value):
    """application/x-www-form-urlencoded as a browser writes it: only letters, digits and *-._ stay."""
    return "".join(c if c.isalnum() and c.isascii() or c in "*-._" else "+" if c == " " else
                   "".join("%%%02X" % byte for byte in c.encode("utf-8")) for c in value)


def uri_component(value):
    """JavaScript's encodeURIComponent: letters, digits and -_.!~*'() stay."""
    return "".join(c if c.isalnum() and c.isascii() or c in "-_.!~*'()" else
                   "".join("%%%02X" % byte for byte in c.encode("utf-8")) for c in value)


def spellings(value=MARKER):
    """Every way the reviewer's findings showed the value coming back, by name."""
    import base64
    raw = value.encode("utf-8")
    return {"typed": value, "json": json.dumps(value), "html": html.escape(value),
            "form body": "email=a%40b.test&password=" + browser_form(value) + "&next=1",
            "encodeURIComponent": "password=" + uri_component(value),
            "lower-case hex": re.sub(r"%[0-9A-F]{2}", lambda found: found.group(0).lower(), browser_form(value)),
            "base64 in JSON": json.dumps({"p": base64.b64encode(raw).decode()}),
            "base64 unpadded": base64.b64encode(raw).decode().rstrip("=") + '"',
            "base64 url-safe": base64.urlsafe_b64encode(raw).decode(),
            "base64 url-safe unpadded": base64.urlsafe_b64encode(raw).decode().rstrip("=") + "&"}


class Encodings(unittest.TestCase):
    def test_every_spelling_a_browser_or_a_page_writes_is_removed_and_a_key_too(self):
        import base64
        raw = MARKER.encode("utf-8")
        self.assertIn("*", browser_form(MARKER))                              # the browser leaves what Python encodes
        self.assertNotEqual(browser_form(MARKER), urllib.parse.quote_plus(MARKER, safe=""))
        self.assertIn("!", uri_component(MARKER))
        self.assertNotEqual(base64.b64encode(raw), base64.urlsafe_b64encode(raw))   # the two alphabets differ here
        leaks = [MARKER, MARKER[:12], browser_form(MARKER), uri_component(MARKER), base64.b64encode(raw).decode().rstrip("="),
                 base64.urlsafe_b64encode(raw).decode().rstrip("=")]
        for name, text in spellings().items():
            self.assertTrue(any(leak in text for leak in leaks + ["zebra-41c9e0"]), name)   # the control: it is in there
            cleaned = browser_guard.without_secrets({"text": [text]}, FORMS)["text"][0]
            self.assertIn(browser_guard.SAVED_SIGN_IN_MARK, cleaned, name)
            for leak in leaks + ["zebra-41c9e0", "emVicmEtNDFjOWUw"]:
                self.assertNotIn(leak, cleaned, name)
        keyed = browser_guard.without_secrets({MARKER: 1, "x": {browser_form(MARKER): 2}}, FORMS)
        self.assertEqual(keyed, {"[saved sign-in]": 1, "x": {"[saved sign-in]": 2}})


@unittest.skipUnless(shutil.which("node"), "Node is not installed")
class FillCode(unittest.TestCase):
    """The Playwright code itself, run under Node against a stand-in page (no browser)."""

    HARNESS = """
const fn = %s;
const spec = %s;
const typed = {};
const page = {
  url: () => spec.url,
  locator: (selector) => ({ elementHandle: async () => {
    const field = spec.fields[selector.replace('aria-ref=', '')];
    if (!field) throw new Error('no element');
    return { ownerFrame: async () => ({ url: () => field.frame }),
             evaluate: async (f) => f({ tagName: 'INPUT', type: field.type }),
             fill: async (value) => { typed[selector] = value; } };
  } }),
};
fn(page).then((result) => console.log(JSON.stringify({ result, typed })));
"""

    def run_code(self, url, fields, site=SITE):
        code = browser_guard.fill_code(site, "e1", "e2", EMAIL, MARKER)
        done = subprocess.run(["node", "-e", self.HARNESS % (code, json.dumps({"url": url, "fields": fields}))],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr[:300])
        return json.loads(done.stdout)

    def test_only_on_the_site_and_only_into_a_password_field(self):
        here = "https://www.jobs.ashbyhq.com/acme/sign-in"
        fields = {"e1": {"frame": here, "type": "email"}, "e2": {"frame": here, "type": "password"}}
        self.assertEqual(self.run_code(here, fields), {
            "result": [{"outcome": "filled", "email": True, "password": True}],
            "typed": {"aria-ref=e1": EMAIL, "aria-ref=e2": MARKER}})
        for url, changed, outcome in (
                ("https://login.other-site.test/sign-in", {}, "wrong_site"),
                ("https://jobs.ashbyhq.com.other-site.test/", {}, "wrong_site"),
                ("about:blank", {}, "wrong_site"),
                (here, {"e2": {"frame": "https://login.other-site.test/f", "type": "password"}}, "wrong_site"),
                (here, {"e2": {"frame": here, "type": "text"}}, "not_password_field"),
                (here, {"e2": None}, "not_found")):
            done = self.run_code(url, {key: value for key, value in dict(fields, **changed).items() if value})
            self.assertEqual((done["result"][0]["outcome"], done["typed"]), (outcome, {}), url)
            self.assertNotIn(MARKER, json.dumps(done["result"]))


class NoSecretAnywhere(GuardCase):
    """The marker test: a sign-in whose password is a unique marker, a session that asks for it and has it
    typed, then every prompt, log, audit row, trace and database row is searched for it. The control: the
    same search finds the marker where it is planted."""

    def search(self, folders, database):
        hits = []
        for folder in folders:
            for path in Path(folder).rglob("*"):
                if path.is_file() and any(form.encode() in path.read_bytes() for form in FORMS):
                    hits.append(path.name)
        for (table,) in database.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            for row in database.execute("SELECT * FROM %s" % table).fetchall():
                if any(form in repr(tuple(row)) or form in json.dumps(tuple(row), default=str) for form in FORMS):
                    hits.append("table " + table)
        return hits

    def test_the_marker_reaches_no_prompt_log_audit_trace_or_database_row(self):
        root = Path(tempfile.mkdtemp(prefix="saved-sign-ins-consume-"))
        self.addCleanup(shutil.rmtree, root, True)
        fixture = Fixture(root / "fixture")
        self.addCleanup(fixture.database.close)
        home = user_profile.home()                                             # the fixture's own home
        credentials.clear_cache()
        site_sessions.before_session(SITE)
        self.assertEqual(saved_sign_ins.add(SITE, EMAIL, MARKER, saved_sign_ins.SIDEDOOR), "saved")
        model_calls.use(fixture.root / "state" / "orchestrator.sqlite3")
        self.addCleanup(model_calls.use, None)
        records = []
        handler = logging.Handler()
        handler.emit = lambda record: records.append(record.getMessage() + repr(record.args))
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        page = Page()
        seen = {}

        def session(config, prompt, directory, timeout):
            seen["prompt"] = prompt
            proxy = self.proxy(page, directory=directory)
            listed = [tool["name"] for tool in proxy.own_tools()]
            result = self.fill(proxy, email_target="e1", password_target="e2")
            # The page now shows the value (a snapshot, a form body): what the browser answers is relayed.
            proxy.child = mock.Mock()
            proxy.child.stdout = [json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text":
                "- textbox \"Password\": " + json.dumps(MARKER) + "\npassword=" + urllib.parse.quote_plus(MARKER)}]}}) + "\n"]
            proxy.pump_child(proxy.child)
            proxy.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                          "params": {"name": "browser_snapshot", "arguments": {}}})
            proxy.audit.flush()
            # The model saved a snapshot and the network log to files, and took a screenshot (the guard here has
            # no output folder of its own: the application job's pass is what cleans them).
            folder = directory / browser_runtime.OUTPUT_FOLDER
            folder.mkdir(exist_ok=True)
            (folder / "shown.yml").write_text("- textbox \"Password\": " + json.dumps(MARKER))
            (folder / "network.txt").write_text("\n".join(spellings().values()))
            (folder / "shot.png").write_bytes(PNG)
            seen["folder"] = folder
            # Everything the AI was given, as an engine's trace keeps it.
            (directory / "trace.jsonl").write_text(json.dumps({"tools": listed, "answer": result}) + "\n" + self.out.getvalue())
            model_calls.record("browser", "ok", worker="applications", model="test-model", prompt_tokens=10)
            seen["relayed"] = self.out.getvalue()
            write_output(directory, request_at(directory), fixture.review())

        fixture.config[saved_sign_ins.CONFIG_KEY] = SITE                       # as the access step will set it (E15.T8)
        request_id = fixture.queue("inspect")
        application_browser.consume(fixture.root, fixture.database, fixture.config, request_id, session)
        saved_sign_ins.prune(saved_sign_ins.open_sites(fixture.root))
        saved_sign_ins_page.view()
        fixture.database.commit()

        self.assertEqual(page.typed, {"e1": EMAIL, "e2": MARKER})              # control 1: the marker really travelled
        self.assertTrue(any(MARKER in json.loads(re.search(r"const input = (.*);\n", code).group(1))["password"]
                            for code in page.codes))
        self.assertIn(browser_guard.SAVED_SIGN_IN_MARK, seen["relayed"])       # the page's own echo was replaced
        self.assertEqual(sorted(path.name for path in seen["folder"].iterdir()), ["network.txt", "shown.yml"])   # no image kept
        self.assertEqual((seen["folder"] / "network.txt").read_text().count(browser_guard.SAVED_SIGN_IN_MARK), len(spellings()))
        folders = {Path(fixture.root), Path(home), self.home}
        names = {path.name for folder in folders for path in folder.rglob("*")}
        self.assertTrue({"guard.jsonl", "prompt.txt", "request.json", "trace.jsonl", saved_sign_ins.STORE_FILE} <= names, names)
        tables = {name for (name,) in fixture.database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("model_calls", tables)
        self.assertEqual(fixture.database.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0], 1)
        self.assertEqual(self.search(folders, fixture.database), [])           # no file, no row
        for text in (seen["prompt"], seen["relayed"], "\n".join(records)):
            for form in FORMS:
                self.assertNotIn(form, text)
        # Control 2 (must hit): the same search finds the marker in a file and in a row.
        (fixture.root / "control.log").write_text("typed " + json.dumps(MARKER))
        fixture.database.execute("CREATE TABLE control_rows (value TEXT)")
        fixture.database.execute("INSERT INTO control_rows VALUES (?)", (urllib.parse.quote_plus(MARKER),))
        self.assertEqual(sorted(self.search(folders, fixture.database)), ["control.log", "table control_rows"])


DEPENDENCIES = all(importlib.util.find_spec(name) for name in ("fastapi", "httpx"))
if DEPENDENCIES:
    from fastapi.testclient import TestClient

    import dashboard
    from tests.test_dashboard_overview import CONFIG, PORT, SECRET, finished_home


@unittest.skipUnless(DEPENDENCIES, "dashboard packages (fastapi, httpx) are not installed")
class PageTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        finished_home(self.root)
        environment = mock.patch.dict(os.environ, {"SIDEDOOR_HOME": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)
        credentials.clear_cache()
        site_sessions._reported.clear()
        site_sessions.before_session(SITE)
        chrome = mock.patch.object(setup_steps, "chrome_path", return_value="/chrome")
        chrome.start()
        self.addCleanup(chrome.stop)
        # The app's own entry point with its defaults (home=None, root=None).
        self.client = TestClient(dashboard.create_app(SECRET, PORT, config=CONFIG), base_url=f"http://127.0.0.1:{PORT}",
                                 follow_redirects=False)
        self.client.get("/", params={"t": SECRET})
        self.origin = {"origin": f"http://127.0.0.1:{PORT}"}

    def page(self):
        response = self.client.get("/settings/saved-sign-ins")
        self.assertEqual(response.status_code, 200, response.text[:300])
        return response.text

    def rows(self, page):
        return [[html.unescape(re.sub(r"<[^>]+>", " ", cell)).split() for cell in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row, re.S)][:4]
                for row in re.findall(r"<tr>(.*?)</tr>", page.split("<tbody>")[1], re.S)] if "<tbody>" in page else []

    def test_the_setting_links_to_the_page_and_settings_gains_no_control(self):
        settings = self.client.get("/settings").text
        section = settings[settings.index('id="account-setting"'):settings.index("</section>", settings.index('id="account-setting"'))]
        self.assertEqual(section.count('<a href="/settings/saved-sign-ins">Saved sign-ins</a>'), 1)
        self.assertEqual(settings.count('action="/settings/account"'), 1)
        self.assertEqual(sorted(set(re.findall(r'<input type="radio" name="(\w+)"', section))), ["account"])
        page = self.page()
        self.assertIn('class="tab active" href="/settings" aria-current="page">Settings</a>', page)
        self.assertIn("No saved sign-ins yet.", page)
        self.assertNotIn("Remove all", page)
        fresh = TestClient(dashboard.create_app(SECRET, PORT, config=CONFIG), base_url=f"http://127.0.0.1:{PORT}")
        self.assertEqual(fresh.get("/settings/saved-sign-ins").status_code, 403)   # behind the dashboard's guard

    def test_the_rows_remove_and_remove_all(self):
        saved_sign_ins.add(SITE, EMAIL, MARKER, saved_sign_ins.SIDEDOOR, now=1762000000.0)
        saved_sign_ins.add(OTHER, "me@example.test", "p", saved_sign_ins.USER, now=1762000000.0)
        saved_sign_ins.add("old.example.test", EMAIL, "gone", saved_sign_ins.SIDEDOOR, now=40 * DAY)
        saved_sign_ins.prune(set(), now=1762000000.0)                          # the old site's password is removed
        page = self.page()
        recent, old = saved_sign_ins_page._day(1762000000.0).split(), saved_sign_ins_page._day(40 * DAY).split()
        self.assertEqual((recent[0], recent[2], old[2]), ("Nov", "2025", "1970"))   # "Nov 01, 2025", local time
        self.assertEqual(self.rows(page), [
            [["jobs.ashbyhq.com"], [EMAIL], ["Sidedoor", "created", "it"], recent],
            [["login.other-site.test"], ["me@example.test"], ["You", "signed", "in"], recent],
            [["old.example.test", "Password", "removed"], [EMAIL], ["Sidedoor", "created", "it"], old]])
        for form in FORMS + ["gone"]:
            self.assertNotIn(form, page)                                       # never a password on the page
        self.assertNotIn("Show password", page)                                # this computer cannot confirm the person
        self.assertEqual(page.count("This computer can&#39;t confirm it&#39;s you, so Sidedoor can&#39;t show passwords here."), 1)
        self.assertNotIn("—", page)
        self.assertNotIn("automation", page.lower())
        self.assertEqual(page.count('action="/settings/saved-sign-ins/remove"'), 3)
        self.assertEqual(page.count('action="/settings/saved-sign-ins/remove-all"'), 1)
        self.assertIn('hx-confirm="Remove every saved sign-in from this computer?"', page)
        # A post from another origin changes nothing.
        self.assertEqual(self.client.post("/settings/saved-sign-ins/remove-all", headers={"origin": "https://evil.test"}).status_code, 403)
        self.assertEqual(len(saved_sign_ins.listing()["rows"]), 3)
        page = self.client.post("/settings/saved-sign-ins/remove", data={"site": OTHER}, headers=self.origin).text
        self.assertIn("Removed the saved sign-in for login.other-site.test.", page)
        self.assertEqual([row[0][0] for row in self.rows(page)], ["jobs.ashbyhq.com", "old.example.test"])
        page = self.client.post("/settings/saved-sign-ins/remove", data={"site": OTHER}, headers=self.origin).text
        self.assertIn("There was nothing to remove.", page)
        page = self.client.post("/settings/saved-sign-ins/remove-all", headers=self.origin).text
        self.assertIn("Removed every saved sign-in.", page)
        self.assertIn("No saved sign-ins yet.", page)
        self.assertEqual(saved_sign_ins.listing()["rows"], [])

    def test_show_password_asks_the_computer_every_time_and_the_value_is_in_that_one_answer_only(self):
        import device_unlock
        self.assertFalse(device_unlock.available())                            # never a real prompt from the tests
        self.assertFalse(device_unlock.verify())
        saved_sign_ins.add(SITE, EMAIL, MARKER, saved_sign_ins.SIDEDOOR)
        saved_sign_ins.add(OTHER, EMAIL, "the-other-password", saved_sign_ins.SIDEDOOR)
        saved_sign_ins.add("old.example.test", EMAIL, "gone", saved_sign_ins.SIDEDOOR, now=40 * DAY)
        saved_sign_ins.prune(set(), now=200 * DAY)
        saved_sign_ins.add(SITE, EMAIL, MARKER, saved_sign_ins.SIDEDOOR)
        saved_sign_ins.add(OTHER, EMAIL, "the-other-password", saved_sign_ins.SIDEDOOR)
        records, asked = [], []
        handler = logging.Handler()
        handler.emit = lambda record: records.append(record.getMessage() + repr(record.args))
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        shown = html.escape(MARKER).replace("&quot;", "&#34;").replace("&#x27;", "&#39;")   # as the page writes it
        self.assertIn(shown, FORMS)
        post = lambda site: self.client.post("/settings/saved-sign-ins/show", data={"site": site}, headers=self.origin)
        with mock.patch.object(device_unlock, "available", return_value=True):
            page = self.page()
            self.assertEqual(page.count('action="/settings/saved-sign-ins/show"'), 2)   # not the row with no password
            self.assertNotIn(shown, page)
            # The unlock is not completed: nothing is shown.
            with mock.patch.object(device_unlock, "verify", side_effect=lambda: asked.append(1) or False):
                response = post(SITE)
            self.assertIn("The password stays hidden because the computer&#39;s unlock wasn&#39;t completed.", response.text)
            self.assertNotIn(shown, response.text)
            # It is completed: that one row's password, in this answer only, not to be cached.
            with mock.patch.object(device_unlock, "verify", side_effect=lambda: asked.append(1) or True):
                response = post(SITE)
                self.assertEqual(response.text.count(shown), 1)
                self.assertNotIn("the-other-password", response.text)
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertIn('href="/settings/saved-sign-ins">Hide password</a>', response.text)
                self.assertNotIn(shown, self.page())                           # hidden again: not left in the page
                again = post(SITE)                                             # every click asks again
                self.assertEqual((len(asked), again.text.count(shown)), (3, 1))
                # A row with no password, an unknown site, another origin: the computer is not even asked.
                for site in ("old.example.test", "unknown.example.test", ""):
                    answer = post(site).text
                    self.assertIn("There is no password to show for that site.", answer)
                    self.assertNotIn(shown, answer)
                refused = self.client.post("/settings/saved-sign-ins/show", data={"site": SITE}, headers={"origin": "https://evil.test"})
                self.assertEqual((refused.status_code, shown in refused.text, len(asked)), (403, False, 3))
        # No way to confirm the person: asked for anyway, nothing is shown.
        with mock.patch.object(device_unlock, "verify", return_value=True):
            self.assertNotIn(shown, post(SITE).text)
        for form in FORMS:
            self.assertNotIn(form, "\n".join(records))                        # never logged
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertFalse(any(form.encode() in path.read_bytes() for form in FORMS), path.name)

    def test_a_key_that_cannot_be_read_says_so(self):
        saved_sign_ins.add(SITE, EMAIL, MARKER, saved_sign_ins.SIDEDOOR)
        (self.root / "vault.key").unlink()
        credentials.clear_cache()
        page = self.page()
        self.assertIn("Sidedoor can&#39;t read your saved sign-ins right now.", page)
        self.assertNotIn("No saved sign-ins yet.", page)


if __name__ == "__main__":
    unittest.main()
