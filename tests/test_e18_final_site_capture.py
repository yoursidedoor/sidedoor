"""A sign-in made in a session's last seconds was kept nowhere in the product's own site-session store: the
guard captures every 15 seconds and once more at its end, but an engine ends the guard before that end path
runs (the mock-site run: signed in at 21.5 s of a 23.5 s session, no capture row and no end row in the audit).
The process that started the session now takes the last capture itself, before it asks Chrome to close.
The store's own fixture (tests/test_site_sessions.py), a stand-in for Chrome's DevTools answer. No browser."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, the OS credential store off)
import json
import time
import unittest
from unittest import mock

import browser_runtime
import site_sessions
import test_site_sessions as store

SITE = store.SITE
ENDPOINT = "http://127.0.0.1:53111"
SECRET = "LAST-SECONDS-SIGN-IN-VALUE-91"


def devtools(name, value, domain, **more):
    """A cookie as Chrome's DevTools gives it (a session cookie; no sameSite set)."""
    return dict({"name": name, "value": value, "domain": domain, "path": "/", "expires": -1, "size": 40,
                 "httpOnly": True, "secure": False, "session": True, "priority": "Medium"}, **more)


class TheStartersLastCapture(store.Home):
    def setUp(self):
        super().setUp()
        self.folder = self.home / "session"
        self.folder.mkdir()
        (self.folder / browser_runtime.GUARD_AUDIT).write_text(
            json.dumps({"decision": "BACKGROUND_LAUNCHED", "endpoint": ENDPOINT, "headless": True}) + "\n")
        self.config = {"browser_driver": {"profile": str(self.home / "profile")}, "_application_operation": "inspect",
                       site_sessions.CONFIG_KEY: SITE}
        self.jar = [devtools("sid", SECRET, SITE), devtools("other", "x", "mail.example.org")]
        self.events = []

        def cookies(endpoint, seconds):
            self.events.append("cookies read")
            if isinstance(self.jar, Exception):
                raise self.jar
            return [dict(item) for item in self.jar]
        for name, effect in (("windows_host", lambda: False), ("chrome_running", lambda path: True),
                             ("devtools_cookies", cookies),
                             ("close_browser", lambda endpoint: self.events.append("asked to close") or True),
                             ("wait_chrome_gone", lambda path, seconds: self.events.append("waited") or True)):
            patch = mock.patch.object(browser_runtime, name, side_effect=effect)
            patch.start()
            self.addCleanup(patch.stop)

    def rows(self):
        return [json.loads(line) for line in (self.folder / browser_runtime.GUARD_AUDIT).read_text().splitlines()][1:]

    def test_a_sign_in_after_the_guards_last_capture_is_kept_before_chrome_is_asked_to_close(self):
        # The guard captured nothing (the site had set nothing yet) and was ended before its own last capture.
        self.assertTrue(browser_runtime.close_cleanly(self.config, self.folder))
        self.assertEqual(self.events, ["cookies read", "asked to close", "waited"])
        row, = self.rows()
        self.assertEqual({key: row[key] for key in ("decision", "site", "sites", "final", "by")},
                         {"decision": "SITE_SESSION_CAPTURED", "site": SITE, "sites": 1, "final": True, "by": "starter"})
        self.assertNotIn(SECRET, (self.folder / browser_runtime.GUARD_AUDIT).read_text())
        self.assertNotIn(SECRET.encode(), (self.home / site_sessions.STORE_FILE).read_bytes())
        # The application job keeps it when the session's result is known, and the next session gets it back.
        self.assertEqual(site_sessions.settle(SITE, signed_in=True), "saved")
        chrome = store.Chrome()
        self.assertEqual(site_sessions.restore(SITE, chrome.call)["outcome"], "restored")
        self.assertEqual(chrome.value("sid"), SECRET)
        self.assertIsNone(chrome.value("other"))                    # another site's cookie is never kept
        kept = next(item for item in chrome.jar if item["name"] == "sid")
        self.assertEqual((kept["sameSite"], kept["expires"]), ("Lax", -1))   # the form the guard's captures hold
        self.assertNotIn("size", kept)

    def test_it_keeps_the_other_site_the_guards_capture_already_held(self):
        other = "accounts.example.org"
        self.jar = [devtools("sid", SECRET, SITE), devtools("sso", "v2", other)]
        guard = store.Chrome(jar=[store.cookie("sso", "v1", other)], url="https://%s/login" % other)
        self.assertEqual(site_sessions.save(SITE, guard.call, pending=True), 1)     # only the other site so far
        browser_runtime.close_cleanly(self.config, self.folder)
        self.assertEqual(self.rows()[0]["sites"], 2)
        self.assertEqual(site_sessions.settle(SITE, signed_in=True), "saved")
        self.assertTrue(site_sessions.has(SITE) and site_sessions.has(other))

    def test_a_failure_is_recorded_and_never_stops_the_close(self):
        self.jar = TimeoutError("Chrome did not answer " + SECRET)
        self.assertTrue(browser_runtime.close_cleanly(self.config, self.folder))
        self.assertEqual(self.events, ["cookies read", "asked to close", "waited"])
        row, = self.rows()
        self.assertEqual((row["decision"], row["step"], row["reason"], row["by"]),
                         ("SITE_SESSION_FAILED", "capture", "TimeoutError", "starter"))
        self.assertNotIn(SECRET, json.dumps(row))
        self.assertEqual(site_sessions.settle(SITE, signed_in=True), "dropped")

    def test_nothing_is_read_without_a_site_without_time_or_without_the_key(self):
        for config, seconds in ((dict(self.config, **{site_sessions.CONFIG_KEY: None}), 10), (self.config, 0.0)):
            browser_runtime.close_cleanly(config, self.folder, seconds)
            self.assertEqual(self.events, ["asked to close", "waited"])
            self.assertEqual(self.rows(), [])
            del self.events[:]
        import credentials
        credentials.delete(site_sessions.KEY_NAME)
        credentials.clear_cache()
        browser_runtime.close_cleanly(self.config, self.folder)
        self.assertEqual(self.events, ["asked to close", "waited"])       # Chrome is not asked for cookies nobody can keep
        self.assertEqual([(row["decision"], row["reason"]) for row in self.rows()],
                         [("SITE_SESSION_FAILED", site_sessions.NEVER_MADE)])

    def test_an_unchanged_capture_and_an_empty_one_write_no_row(self):
        browser_runtime.close_cleanly(self.config, self.folder)
        browser_runtime.close_cleanly(self.config, self.folder)       # the same cookies again: kept, counted, not rewritten
        self.assertEqual([row["sites"] for row in self.rows()], [1, 1])
        self.jar = [devtools("gone", "x", SITE, expires=time.time() - 5), devtools("other", "x", "mail.example.org")]
        browser_runtime.close_cleanly(self.config, self.folder)
        self.assertEqual(len(self.rows()), 2)                          # nothing to keep is not recorded


class ChromesAnswer(unittest.TestCase):
    def test_the_cookies_are_asked_on_chromes_own_socket_and_a_bad_endpoint_is_refused(self):
        sent = []

        class Socket:
            def send(self, text):
                sent.append(json.loads(text))

            def recv(self, timeout=None):
                return json.dumps({"id": 1, "result": {"cookies": [devtools("sid", SECRET, SITE)]}})

            def close(self):
                sent.append("closed")

        class Answer:
            def __enter__(self):
                return self

            def __exit__(self, *given):
                return False

            def read(self, limit):
                return json.dumps({"webSocketDebuggerUrl": "ws://127.0.0.1:53111/devtools/browser/x"}).encode()
        cookies = browser_runtime.devtools_cookies(ENDPOINT, 2, connect=lambda url: Socket(), opener=lambda url, timeout: Answer())
        self.assertEqual([cookie["name"] for cookie in cookies], ["sid"])
        self.assertEqual(sent, [{"id": 1, "method": "Storage.getCookies"}, "closed"])
        with self.assertRaises(ValueError):
            browser_runtime.devtools_cookies("http://example.org:9222", 2)

    def test_a_partitioned_cookie_keeps_its_top_level_site(self):
        made = site_sessions.from_devtools(devtools("p", "v", SITE, sameSite="None",
                                                    partitionKey={"topLevelSite": "https://example.org", "hasCrossSiteAncestor": True}))
        self.assertEqual((made["sameSite"], made["partitionKey"]), ("None", "https://example.org"))
        self.assertIsNone(site_sessions.from_devtools("x"))


if __name__ == "__main__":
    unittest.main()
