"""E15.T4 site sessions, by outcome: a session that ends signed in is saved, Chrome exits (its session
cookies are gone), and the next session on that site starts with them back; nothing crosses sites;
nothing secret reaches a prompt, a log, the database, the guard's audit or the store file in clear.

No browser: `Chrome` is a cookie jar that answers the two pieces of Playwright code site_sessions.py
sends through the guard's own call (capture, restore) the way the real page would.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, the OS credential store off)
import io
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import application_browser  # noqa: E402
import browser_guard  # noqa: E402
import browser_runtime  # noqa: E402
import credentials  # noqa: E402
import log_retention  # noqa: E402
import site_sessions  # noqa: E402
import user_profile  # noqa: E402
from manual_application_browser import Fixture, request_at, write_output  # noqa: E402

SECRET = "s3cr3t-session-cookie-value-9f2b7c"
SITE = "jobs.ashbyhq.com"                      # the Fixture's job is on https://jobs.ashbyhq.com/synthetic/role-123
DAY = 86400


def cookie(name, value, domain, expires=-1, path="/"):
    return {"name": name, "value": value, "domain": domain, "path": path, "expires": expires,
            "httpOnly": True, "secure": True, "sameSite": "Lax"}


class Chrome:
    """A cookie jar with a current page. `call` is what the guard's own browser call answers."""

    def __init__(self, url="https://jobs.ashbyhq.com/synthetic/role-123", jar=()):
        self.url, self.jar, self.calls = url, list(jar), []

    @staticmethod
    def bare(domain):
        domain = domain.lower().lstrip(".")
        return domain[4:] if domain.startswith("www.") else domain

    def exit(self):
        """Chrome exits: session cookies (no expiry) are dropped, persistent ones stay in the profile."""
        self.jar = [item for item in self.jar if item["expires"] != -1]

    def call(self, code):
        given = json.loads(re.search(r"const input = (.*);\n", code).group(1))
        self.calls.append(given["sidedoor"])
        if given["sidedoor"] == "site-session-capture":
            hosts = [given["site"], self.bare(self.url.split("/")[2])]
            return [{"url": self.url, "cookies": [dict(item) for item in self.jar if any(
                host == self.bare(item["domain"]) or host.endswith("." + self.bare(item["domain"])) for host in hosts)]}]
        held = {(item["name"], item["domain"].lstrip("."), item["path"]) for item in self.jar}
        absent = [item for item in given["cookies"]
                  if (item["name"], item["domain"].lstrip("."), item["path"]) not in held]
        self.jar.extend(absent)
        return [{"restored": len(absent), "kept": len(given["cookies"]) - len(absent), "failed": 0}]

    def value(self, name):
        return next((item["value"] for item in self.jar if item["name"] == name), None)


class Home(unittest.TestCase):
    """A fresh Sidedoor home per test, as the app finds it (SIDEDOOR_HOME, no explicit home argument)."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="site-sessions-home-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        patch = mock.patch.dict(os.environ, {user_profile.HOME_VARIABLE: str(self.home)})
        patch.start()
        self.addCleanup(patch.stop)
        credentials.clear_cache()
        site_sessions._reported.clear()
        site_sessions.before_session(SITE)      # the application job: the one place the store's key is made

    def signed_in(self, chrome, site=SITE, **options):
        """A session on `site` that ended signed in: captured by the guard, kept by the application job."""
        self.assertGreaterEqual(site_sessions.save(site, chrome.call, pending=True, **options), 1)
        self.assertEqual(site_sessions.settle(site, signed_in=True), "saved")


class Store(Home):
    def test_a_session_that_ends_signed_in_is_restored_after_chrome_exits_and_chromes_own_value_wins(self):
        chrome = Chrome(jar=[cookie("sid", SECRET, SITE), cookie("remember", "old", ".ashbyhq.com", time.time() + 30 * DAY),
                             cookie("other", "x", "mail.example.org", time.time() + DAY)])
        self.signed_in(chrome)
        self.assertTrue(site_sessions.has(SITE))
        chrome.exit()
        self.assertIsNone(chrome.value("sid"))                     # Chrome dropped the session cookie
        next(item for item in chrome.jar if item["name"] == "remember")["value"] = "newer"   # rotated on shutdown
        self.assertEqual(site_sessions.restore(SITE, chrome.call),
                         {"outcome": "restored", "restored": 1, "kept": 1, "failed": 0})
        self.assertEqual(chrome.value("sid"), SECRET)              # back for the next session
        self.assertEqual(chrome.value("remember"), "newer")        # never overwritten by the saved, older one
        self.assertEqual(len([item for item in chrome.jar if item["name"] == "other"]), 1)   # another site's: untouched

    def test_another_site_gets_nothing_and_www_is_the_same_site(self):
        chrome = Chrome(url="https://www.example.com/careers/7", jar=[cookie("sid", SECRET, "www.example.com")])
        self.assertEqual(site_sessions.site_key("https://www.example.com/careers/7"), "example.com")
        self.assertEqual(site_sessions.site_key("https://EXAMPLE.com/apply"), "example.com")
        self.signed_in(chrome, "example.com")
        fresh = Chrome(url="about:blank")
        self.assertEqual(site_sessions.restore("other.example.org", fresh.call)["outcome"], "none")
        self.assertEqual(site_sessions.restore("careers.example.com", fresh.call)["outcome"], "none")   # a host is its own site
        self.assertEqual(fresh.jar, [])
        self.assertEqual(site_sessions.restore(site_sessions.site_key("https://example.com/jobs"), fresh.call)["restored"], 1)
        self.assertEqual(fresh.value("sid"), SECRET)
        # Shared boards are one site whatever the employer path; a Workday tenant is its own.
        self.assertEqual(site_sessions.site_key("https://boards.greenhouse.io/acme/jobs/1"),
                         site_sessions.site_key("https://boards.greenhouse.io/globex/jobs/2"))
        self.assertNotEqual(site_sessions.site_key("https://acme.wd5.myworkdayjobs.com/x"),
                            site_sessions.site_key("https://globex.wd5.myworkdayjobs.com/x"))
        for bad in ("about:blank", "file:///etc/passwd", "", None, "https://"):
            self.assertIsNone(site_sessions.site_key(bad))

    def test_only_the_sign_in_wall_forgets_the_saved_session(self):                    # X2
        chrome = Chrome(jar=[cookie("sid", SECRET, SITE)])
        site_sessions.save(SITE, chrome.call, pending=True)
        self.assertEqual(site_sessions.settle(SITE, signed_in=False), "dropped")       # the session failed
        self.assertFalse(site_sessions.has(SITE))
        self.signed_in(chrome)
        chrome.jar.append(cookie("challenge", "c", SITE))
        site_sessions.save(SITE, chrome.call, pending=True)
        self.assertEqual(site_sessions.settle(SITE, signed_in=False), "dropped")       # a human check or a code: kept
        self.assertTrue(site_sessions.has(SITE))
        self.assertEqual(site_sessions._load(None)["pending"], {})
        self.assertEqual(site_sessions.settle(SITE, signed_in=False, sign_in_again=True), "discarded")
        self.assertFalse(site_sessions.has(SITE))

    def test_a_failed_restore_is_told_apart_from_nothing_saved(self):                  # X6
        self.assertEqual(site_sessions.restore(SITE, lambda code: None)["outcome"], "none")
        self.signed_in(Chrome(jar=[cookie("sid", SECRET, SITE)]))
        self.assertEqual(site_sessions.restore(SITE, lambda code: None),               # the browser did not answer
                         {"outcome": "failed", "restored": 0, "kept": 0, "failed": 1})

        def broken(code):
            raise OSError("pipe")
        self.assertEqual(site_sessions.restore(SITE, broken)["outcome"], "failed")

    def test_only_cookies_the_site_would_be_sent_are_stored_and_restored(self):        # X8
        self.assertTrue(site_sessions.sent_to(cookie("a", "v", ".ashbyhq.com"), SITE))
        self.assertTrue(site_sessions.sent_to(cookie("a", "v", "www.jobs.ashbyhq.com"), SITE))
        for foreign in (".other.org", "com", ".com", "evil-ashbyhq.com", "x.jobs.ashbyhq.com", ""):
            self.assertFalse(site_sessions.sent_to(cookie("a", "v", foreign), SITE), foreign)
        liar = Chrome(jar=[cookie("sid", SECRET, SITE)])
        liar.call = lambda code: [{"url": liar.url, "cookies": liar.jar + [cookie("spy", "x", ".other.org")]}]
        self.signed_in(liar)                                                           # a capture answering too much
        stored = site_sessions._load(None)["sites"][SITE]["cookies"]
        self.assertEqual([item["name"] for item in stored], ["sid"])
        # A store that already holds a foreign cookie under the site (an older or damaged entry): not sent.
        site_sessions._change(None, lambda data: data["sites"][SITE]["cookies"].append(cookie("spy", "x", ".other.org")) or True)
        fresh = Chrome()
        self.assertEqual(site_sessions.restore(SITE, fresh.call)["restored"], 1)
        self.assertEqual([item["name"] for item in fresh.jar], ["sid"])

    def test_at_most_200_sites_and_an_unchanged_capture_is_not_rewritten(self):        # X7
        self.assertEqual(site_sessions.MAX_SITES, 200)
        with mock.patch.object(site_sessions, "MAX_SITES", 3):
            for index in range(5):
                host = "h%d.example.com" % index
                self.signed_in(Chrome(url="https://%s/" % host, jar=[cookie("sid", "v", host)]), host, now=1000.0 + index)
            self.assertEqual(sorted(site_sessions._load(None)["sites"]), ["h2.example.com", "h3.example.com", "h4.example.com"])
            site_sessions.restore("h2.example.com", Chrome().call, now=2000.0)         # used: no longer the oldest
            self.signed_in(Chrome(url="https://h5.example.com/", jar=[cookie("sid", "v", "h5.example.com")]),
                           "h5.example.com", now=1500.0)
            self.assertEqual(sorted(site_sessions._load(None)["sites"]), ["h2.example.com", "h4.example.com", "h5.example.com"])
        chrome, path = Chrome(jar=[cookie("sid", SECRET, SITE)]), self.home / site_sessions.STORE_FILE
        self.assertEqual(site_sessions.save(SITE, chrome.call, pending=True), 1)
        before = path.read_bytes()
        self.assertEqual(site_sessions.save(SITE, chrome.call, pending=True), 1)       # the same cookies 15 s later
        self.assertEqual(path.read_bytes(), before)                                    # not sealed or written again
        chrome.jar[0]["value"] = "rotated"
        self.assertEqual(site_sessions.save(SITE, chrome.call, pending=True), 1)
        self.assertNotEqual(path.read_bytes(), before)

    def test_unused_for_ninety_days_is_pruned_and_a_restore_counts_as_use(self):
        start = time.time()
        chrome = Chrome(jar=[cookie("sid", SECRET, SITE)])
        self.signed_in(chrome, now=start)
        other = Chrome(url="https://jobs.lever.co/acme/1", jar=[cookie("sid", "v", "jobs.lever.co")])
        self.signed_in(other, "jobs.lever.co", now=start)
        self.assertEqual(site_sessions.prune(now=start + 89 * DAY), 0)
        site_sessions.restore(SITE, Chrome().call, now=start + 60 * DAY)               # used on day 60
        self.assertEqual(site_sessions.prune(now=start + 91 * DAY), 1)
        self.assertTrue(site_sessions.has(SITE))
        self.assertFalse(site_sessions.has("jobs.lever.co"))
        self.assertEqual(site_sessions.prune(now=start + 151 * DAY), 1)
        self.assertFalse(site_sessions.has(SITE))
        self.signed_in(chrome)
        with mock.patch.object(log_retention, "prune_traces", return_value={"removed": 0}), \
                mock.patch.object(log_retention, "retain", return_value={"rotated": 0, "removed": 0}), \
                mock.patch.object(site_sessions.time, "time", return_value=time.time() + 91 * DAY):
            log_retention.startup(self.home)                                           # the sweep the app runs
        self.assertFalse(site_sessions.has(SITE))

    def test_an_older_install_and_a_corrupt_store_with_its_key_are_an_empty_store(self):
        chrome = Chrome(jar=[cookie("sid", SECRET, SITE)])
        self.assertFalse(site_sessions.has(SITE))                                      # an older install: no file
        self.assertEqual(site_sessions.restore(SITE, chrome.call)["outcome"], "none")
        self.assertEqual(site_sessions.prune(), 0)
        self.assertEqual(site_sessions.check(SITE), ("none", None))
        self.assertFalse((self.home / site_sessions.STORE_FILE).exists())              # reading makes nothing
        self.signed_in(chrome)
        path = self.home / site_sessions.STORE_FILE
        self.assertEqual(os.stat(path).st_mode & 0o077, 0)
        blob = path.read_bytes()
        path.write_bytes(blob[:-5] + b"XXXXX")                                         # tampered / corrupt, key readable
        self.assertFalse(site_sessions.has(SITE))
        self.assertEqual(site_sessions.restore(SITE, Chrome().call)["outcome"], "none")
        self.signed_in(chrome)                                                         # replaced by the next save
        self.assertTrue(site_sessions.has(SITE))
        self.assertEqual([item.name for item in self.home.iterdir() if "site-sessions" in item.name and ".tmp" in item.name], [])

    def test_only_the_application_job_makes_the_key_and_a_lost_key_is_never_replaced(self):   # X3
        key_file = self.home / credentials.NAMES[site_sessions.KEY_NAME]
        fresh = Path(tempfile.mkdtemp(prefix="site-sessions-nokey-"))
        self.addCleanup(shutil.rmtree, fresh, True)
        chrome = Chrome(jar=[cookie("sid", SECRET, SITE)])
        # Never made: the guard's capture, a save and the user-step hook make no key and keep nothing.
        self.assertEqual(site_sessions.save(SITE, chrome.call, pending=True, home=fresh), 0)
        self.assertEqual(site_sessions.user_step_ended(SITE, chrome.call, home=fresh), 0)
        self.assertEqual(chrome.calls, [])                                             # Chrome was not even asked
        self.assertEqual(sorted(item.name for item in fresh.iterdir()), [])
        self.assertIsNone(site_sessions.before_session(SITE, home=fresh))              # the one place
        self.assertTrue((fresh / credentials.NAMES[site_sessions.KEY_NAME]).exists())
        self.assertEqual(site_sessions.save(SITE, chrome.call, home=fresh), 1)
        # A key that should exist but is gone: nothing is minted, replaced, read or deleted.
        self.signed_in(chrome)
        store = (self.home / site_sessions.STORE_FILE).read_bytes()
        old_key = key_file.read_text()
        key_file.unlink()
        credentials.clear_cache()
        with self.assertLogs("sidedoor", level="WARNING") as logged:
            self.assertIsNone(site_sessions.before_session(SITE))
        self.assertIn("key missing", "\n".join(logged.output))                         # recorded, once
        self.assertEqual(site_sessions.save(SITE, chrome.call, pending=True), 0)
        self.assertEqual(site_sessions.restore(SITE, Chrome().call)["outcome"], "unavailable")
        self.assertFalse(site_sessions.discard(SITE))
        self.assertEqual(site_sessions.prune(now=time.time() + 400 * DAY), 0)
        self.assertFalse(key_file.exists())                                            # no new key
        self.assertEqual((self.home / site_sessions.STORE_FILE).read_bytes(), store)   # the store untouched
        key_file.write_text(old_key)                                                   # the key is readable again
        credentials.clear_cache()
        self.assertTrue(site_sessions.has(SITE))                                       # and everything is still there
        # A key the store cannot give right now (a locked keychain) is the same: unavailable, nothing touched.
        with mock.patch.object(credentials, "get", side_effect=credentials.Locked("locked")):
            self.assertEqual(site_sessions.restore(SITE, Chrome().call)["outcome"], "unavailable")
            self.assertEqual(site_sessions.save(SITE, chrome.call), 0)
            self.assertIsNone(site_sessions.before_session(SITE))
        self.assertEqual((self.home / site_sessions.STORE_FILE).read_bytes(), store)

    def test_removing_the_saved_keys_starts_site_sessions_afresh(self):                 # Q3
        chrome = Chrome(jar=[cookie("sid", SECRET, SITE)])
        self.signed_in(chrome)
        credentials.remove_all(self.home)                                              # Settings > Remove saved keys
        for name in (site_sessions.STORE_FILE, site_sessions.KEY_MADE_FILE, credentials.NAMES[site_sessions.KEY_NAME]):
            self.assertFalse((self.home / name).exists(), name)
        self.assertEqual(site_sessions.restore(SITE, Chrome().call)["outcome"], "none")
        self.assertIsNone(site_sessions.before_session(SITE))                          # the next session start
        self.assertTrue((self.home / credentials.NAMES[site_sessions.KEY_NAME]).exists())
        self.signed_in(chrome)
        fresh = Chrome()
        self.assertEqual(site_sessions.restore(SITE, fresh.call)["outcome"], "restored")
        self.assertEqual(fresh.value("sid"), SECRET)
        # A refused delete (the keychain would not let go) removes nothing.
        store = (self.home / site_sessions.STORE_FILE).read_bytes()
        with mock.patch.object(credentials, "_path", side_effect=credentials.Locked("locked")):
            with self.assertRaises(credentials.Locked):
                credentials.delete(site_sessions.KEY_NAME, self.home)
        self.assertEqual((self.home / site_sessions.STORE_FILE).read_bytes(), store)

    def test_a_file_sealed_for_another_purpose_or_in_the_old_format_does_not_open(self):   # X3 c
        key = os.urandom(32)
        sealed = site_sessions.seal(key, b"cookies")
        self.assertEqual(site_sessions.unseal(key, sealed), b"cookies")
        self.assertIsNone(site_sessions.unseal(key, sealed, purpose=b"password-vault"))
        self.assertIsNone(site_sessions.unseal(key, site_sessions.seal(key, b"passwords", purpose=b"password-vault")))
        self.assertEqual(site_sessions.unseal(key, site_sessions.seal(key, b"p", purpose=b"password-vault"),
                                              purpose=b"password-vault"), b"p")
        self.assertIsNone(site_sessions.unseal(key, b"SDV1" + sealed[4:]))             # the first format: empty
        self.assertIsNone(site_sessions.unseal(os.urandom(32), sealed))

    def test_a_job_scoped_session_is_its_own_entry_and_goes_with_the_job(self):
        chrome = Chrome(jar=[cookie("pass", SECRET, SITE)])
        self.assertEqual(site_sessions.user_step_ended(SITE, chrome.call, cleared="CAPTCHA_REQUIRED", job="job-1"), 1)
        self.assertTrue(site_sessions.has(SITE, job="job-1"))
        self.assertFalse(site_sessions.has(SITE))
        fresh = Chrome()
        self.assertEqual(site_sessions.restore(SITE, fresh.call, job="job-2")["outcome"], "none")
        self.assertEqual(site_sessions.restore(SITE, fresh.call, job="job-1")["restored"], 1)
        self.assertTrue(site_sessions.discard_job("job-1"))
        self.assertFalse(site_sessions.has(SITE, job="job-1"))


def answer(value):
    return {"result": {"content": [{"type": "text", "text": "### Result\n" + json.dumps(value)}]}}


class Guard(Home):
    """The real guard proxy in front of the fake Chrome: restore after `initialized`, capture while it runs."""

    def proxy(self, chrome, site=SITE):
        self.now = 1000.0
        directory = Path(tempfile.mkdtemp(prefix="site-sessions-guard-"))
        self.addCleanup(shutil.rmtree, directory, True)
        with mock.patch.object(browser_guard.subprocess, "Popen"):
            proxy = browser_guard.Proxy("inspect", ["node", "cli.js"], audit=directory / "guard.jsonl",
                                        stdin=io.StringIO(""), stdout=io.StringIO(), focus_keeper=False,
                                        clock=lambda: self.now, site_session=site)
        self.addCleanup(proxy.audit.close)
        proxy.child_call = lambda method, params: answer(chrome.call(params["arguments"]["code"]))
        self.forwarded = []
        proxy.to_child = self.forwarded.append
        proxy.audit_path = directory / "guard.jsonl"
        return proxy

    def step(self, proxy, seconds=20):
        self.now += seconds
        proxy.handle({"jsonrpc": "2.0", "id": len(self.forwarded) + 1, "method": "tools/call",
                      "params": {"name": "browser_snapshot", "arguments": {}}})
        with proxy.idle:
            proxy.in_flight.clear()

    def test_session_a_signs_in_chrome_exits_and_session_b_starts_signed_in(self):
        chrome = Chrome()
        first = self.proxy(chrome)
        first.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(chrome.calls, [])                          # nothing saved yet: Chrome is not even asked
        self.assertNotIn("SITE_SESSION", first.audit_path.read_text())
        chrome.jar.append(cookie("sid", SECRET, SITE))              # the site signs the session in
        self.step(first)
        first.child = mock.Mock(poll=lambda: None)
        first.capture_site_session(final=True)
        self.assertFalse(site_sessions.has(SITE))                   # a capture is not a saved session yet
        self.assertEqual(site_sessions.settle(SITE, signed_in=True), "saved")
        chrome.exit()
        self.assertIsNone(chrome.value("sid"))

        second = self.proxy(chrome)
        second.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(chrome.value("sid"), SECRET)               # restored before any page work
        self.assertEqual([m.get("method") for m in self.forwarded], ["notifications/initialized"])   # no navigation
        elsewhere = Chrome(url="about:blank")
        third = self.proxy(elsewhere, site="jobs.lever.co")
        third.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(elsewhere.jar, [])                         # a session on another site gets nothing
        audit = (second.audit_path.read_text() + first.audit_path.read_text())
        self.assertIn("SITE_SESSION_RESTORED", audit)
        self.assertIn("SITE_SESSION_CAPTURED", audit)
        self.assertNotIn(SECRET, audit)

    def test_a_restore_the_browser_does_not_answer_is_one_audit_row_with_counts_only(self):   # X6
        chrome = Chrome(jar=[cookie("sid", SECRET, SITE)])
        self.signed_in(chrome)
        proxy = self.proxy(chrome)
        proxy.child_call = lambda method, params: None              # timed out
        proxy.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        rows = [json.loads(line) for line in proxy.audit_path.read_text().splitlines()]
        failed = [row for row in rows if row["decision"] == "SITE_SESSION_RESTORE_FAILED"]
        self.assertEqual([(row["site"], row["outcome"], row["failed"]) for row in failed], [(SITE, "failed", 1)])
        self.assertNotIn(SECRET, proxy.audit_path.read_text())

    def test_the_guard_is_told_the_site_on_its_command_line_and_nothing_else(self):
        with tempfile.TemporaryDirectory() as directory:
            argv = browser_runtime.guard_command({site_sessions.CONFIG_KEY: SITE}, directory, "inspect")
            self.assertEqual(argv[argv.index("--site-session") + 1], SITE)
            self.assertNotIn("--site-session", browser_runtime.guard_command({}, directory, "inspect"))
            self.assertNotIn("--site-session", browser_runtime.guard_command(
                {site_sessions.CONFIG_KEY: "evil host; rm"}, directory, "inspect"))


class Sessions(unittest.TestCase):
    """Through the real `application_browser.consume` (the app's read and submit path), a scripted session."""

    def setUp(self):
        root = Path(tempfile.mkdtemp(prefix="site-sessions-consume-"))
        self.addCleanup(shutil.rmtree, root, True)
        self.fixture = Fixture(root / "fixture")
        self.addCleanup(self.fixture.database.close)
        credentials.clear_cache()
        site_sessions._reported.clear()
        site_sessions.before_session(SITE)      # as an earlier read would have: the store's key exists
        self.chrome = Chrome(jar=[cookie("sid", SECRET, SITE)])
        self.started = []

    def read(self, result=None, fail=False):
        with self.fixture.database:   # an earlier read's result is used up, as its consumer leaves it
            self.fixture.database.execute("UPDATE desktop_requests SET state='CONSUMED' WHERE state='DONE'")
        request_id = self.fixture.queue("inspect")

        def session(config, prompt, directory, timeout):
            self.started.append(config.get(site_sessions.CONFIG_KEY))
            self.prompt = prompt
            site_sessions.save(config[site_sessions.CONFIG_KEY], self.chrome.call, pending=True)   # the guard's capture
            if fail:
                (directory / "trace.jsonl").write_text("")
                raise RuntimeError("session ends")
            write_output(directory, request_at(directory), result or self.fixture.review())
        summary = application_browser.consume(self.fixture.root, self.fixture.database, self.fixture.config,
                                              request_id, session)
        row = self.fixture.database.execute("SELECT state,result FROM desktop_requests WHERE request_id=?",
                                            (request_id,)).fetchone()
        return summary, json.loads(row["result"]) if row["result"] else None

    def test_a_read_that_gets_through_saves_the_session_for_the_submit_and_later_jobs(self):
        self.assertFalse(site_sessions.has(SITE))
        summary, _ = self.read()
        self.assertEqual(summary["state"], "COMPLETE")
        self.assertEqual(self.started, [SITE])                      # the guard was told the site
        self.assertTrue(site_sessions.has(SITE))
        self.chrome.exit()
        self.assertEqual(site_sessions.restore(SITE, self.chrome.call)["restored"], 1)
        self.assertEqual(self.chrome.value("sid"), SECRET)

    def test_a_read_that_ends_on_a_wall_or_fails_saves_nothing(self):
        summary, result = self.read({"blocked": {"code": "AUTHENTICATION_REQUIRED", "next_action": "Sign in"}})
        self.assertEqual((summary["state"], result["blocked"]["code"]), ("COMPLETE", "AUTHENTICATION_REQUIRED"))
        self.assertFalse(site_sessions.has(SITE))
        self.read(fail=True)
        self.assertFalse(site_sessions.has(SITE))
        self.assertEqual(site_sessions._load(None)["pending"], {})

    def test_a_human_check_or_a_code_keeps_the_sign_in_and_only_the_sign_in_wall_forgets_it(self):   # X2
        for code, kept in (("CAPTCHA_REQUIRED", True), ("MFA_REQUIRED", True), ("AUTHENTICATION_REQUIRED", False)):
            site_sessions.before_session(SITE)
            self.assertEqual(site_sessions.save(SITE, self.chrome.call), 1)             # signed in earlier
            summary, result = self.read({"blocked": {"code": code, "next_action": "Needs the user"}})
            self.assertEqual((summary["state"], result["blocked"]["code"]), ("COMPLETE", code))
            self.assertEqual(site_sessions.has(SITE), kept, code)
            self.assertEqual(site_sessions._load(None)["pending"], {})                  # a walled capture is never kept
            self.fixture = Fixture(Path(tempfile.mkdtemp(prefix="x2-", dir=self.fixture.root.parent)) / "fixture")
            self.addCleanup(self.fixture.database.close)
            credentials.clear_cache()

    def test_a_result_that_is_not_accepted_saves_nothing(self):                         # X4
        real = application_browser.desktop_adapter.complete
        with mock.patch.object(application_browser.desktop_adapter, "complete",
                               side_effect=lambda *a, **k: dict(real(*a, **k), accepted=False)):
            summary, _ = self.read()
        self.assertEqual((summary["state"], summary["accepted"]), ("COMPLETE", False))
        self.assertEqual(self.started, [SITE])
        self.assertFalse(site_sessions.has(SITE))

    def test_a_dead_session_from_a_cleared_wall_takes_the_wall_path_without_a_model_session(self):
        past = time.time() - 10 * DAY
        expiring = Chrome(jar=[cookie("auth", SECRET, SITE, expires=past + DAY)])
        self.assertEqual(site_sessions.user_step_ended(SITE, expiring.call, cleared="AUTHENTICATION_REQUIRED", now=past), 1)
        self.assertTrue(site_sessions.has(SITE))
        summary, result = self.read()
        self.assertEqual(self.started, [])                          # no session was started or paid for
        self.assertEqual(summary["state"], "COMPLETE")
        self.assertEqual(result["blocked"]["code"], "AUTHENTICATION_REQUIRED")
        self.assertEqual(result["observed_browser_calls"], 0)
        self.assertFalse(site_sessions.has(SITE))                   # discarded
        self.assertEqual(self.fixture.database.execute("SELECT count(*) FROM model_calls").fetchone()[0]
                         if self.fixture.database.execute("SELECT 1 FROM sqlite_master WHERE name='model_calls'").fetchone()
                         else 0, 0)
        self.read()                                                 # with nothing saved, the next read runs
        self.assertEqual(self.started, [SITE])

    def test_a_dead_session_the_site_never_walled_is_dropped_and_the_session_runs(self):
        past = time.time() - 10 * DAY
        self.assertGreaterEqual(site_sessions.save(SITE, Chrome(jar=[cookie("pref", "v", SITE, expires=past + DAY)]).call,
                                                   now=past), 1)
        self.assertEqual(site_sessions.check(SITE, now=past), ("live", None))
        summary, result = self.read()
        self.assertEqual(self.started, [SITE])
        self.assertNotIn("blocked", result)

    def test_no_secret_reaches_a_prompt_a_log_the_database_the_audit_or_the_store_in_clear(self):
        records = []
        handler = logging.Handler()
        handler.emit = lambda record: records.append(record.getMessage() + repr(record.args))
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        home = user_profile.home()
        # The guard's side, with its audit file inside the scanned folder.
        self.now = 0.0
        with mock.patch.object(browser_guard.subprocess, "Popen"):
            proxy = browser_guard.Proxy("inspect", ["node", "cli.js"], audit=self.fixture.root / "guard.jsonl",
                                        stdin=io.StringIO(""), stdout=io.StringIO(), focus_keeper=False,
                                        clock=lambda: self.now, site_session=SITE)
        self.addCleanup(proxy.audit.close)
        proxy.child_call = lambda method, params: answer(self.chrome.call(params["arguments"]["code"]))
        proxy.to_child = lambda message: None
        proxy.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.now = 60.0
        proxy.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": "browser_snapshot", "arguments": {}}})
        # The application job's side: a read that saves, one that restores, a wall, a failure, the sweep.
        self.read()
        self.chrome.exit()
        proxy.restore_site_session()
        self.assertEqual(self.chrome.value("sid"), SECRET)          # the control: the secret really travelled
        self.read()
        self.read(fail=True)
        site_sessions.prune()
        site_sessions.check(SITE)
        self.fixture.database.commit()
        proxy.audit.flush()

        key = site_sessions._key(None)
        store = (home / site_sessions.STORE_FILE).read_bytes()
        self.assertIn(SECRET.encode(), site_sessions.unseal(key, store))               # it is in the store...
        self.assertNotIn(SECRET.encode(), store)                                       # ...and not in clear
        scanned = 0
        for folder in {Path(self.fixture.root), Path(home)}:
            for path in folder.rglob("*"):
                if path.is_file():
                    scanned += 1
                    self.assertNotIn(SECRET.encode(), path.read_bytes(), path)         # SQLite, traces, audit, prompts
        names = {path.name for folder in (Path(self.fixture.root), Path(home)) for path in folder.rglob("*")}
        self.assertTrue({"guard.jsonl", "prompt.txt", "request.json", site_sessions.STORE_FILE} <= names, names)
        self.assertTrue(any(name.endswith((".sqlite3", ".db", ".sqlite")) for name in names), names)
        self.assertGreater(scanned, 5)
        self.assertNotIn(SECRET, self.prompt)
        self.assertNotIn(SECRET, "\n".join(records))
        self.assertNotIn(SECRET, proxy.stdout.getvalue())
        self.assertNotIn(SECRET, " ".join(browser_runtime.guard_command({site_sessions.CONFIG_KEY: SITE},
                                                                         self.fixture.root, "inspect")))
        for row in self.fixture.database.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            for record in self.fixture.database.execute("SELECT * FROM " + row[0]).fetchall():
                self.assertNotIn(SECRET, repr(tuple(record)))


if __name__ == "__main__":
    unittest.main()
