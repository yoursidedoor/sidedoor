"""E9.T3 Update check and banner: no real network call; `get` is always injected."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import importlib.util
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import update_check

DEPENDENCIES = all(importlib.util.find_spec(name) for name in ("fastapi", "httpx"))
if DEPENDENCIES:
    from fastapi.testclient import TestClient
    import dashboard

PORT = 54321
SECRET = "test-token-value"


def answer(latest="9.0.0", url="https://yoursidedoor.com/download", notes="Faster research."):
    return lambda _url: json.dumps({"latest": latest, "download_url": url, "notes": notes}).encode()


def offline(_url):
    raise OSError("network is unreachable")


class UpdateCheckTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.home = Path(self.directory.name)
        patcher = mock.patch("version.__version__", "1.1.0")
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.directory.cleanup()

    def banner_after(self, get):
        update_check.check(self.home, get)
        return update_check.banner(self.home)

    def test_newer_shows_banner(self):
        banner = self.banner_after(answer("1.2.0"))
        self.assertEqual(banner["text"], "Sidedoor 1.2.0 is available.")
        self.assertEqual(banner["action_href"], "https://yoursidedoor.com/download")
        self.assertEqual(banner["notes"], "Faster research.")
        self.assertTrue(banner["external"])

    def test_same_or_older_shows_none(self):
        self.assertIsNone(self.banner_after(answer("1.1.0")))
        self.assertIsNone(self.banner_after(answer("1.0.9")))
        self.assertIsNotNone(self.banner_after(answer("1.10.0")))  # numeric, not text, comparison

    def test_offline_and_bad_answers_show_none(self):
        self.assertIsNone(self.banner_after(offline))
        for raw in (b"not json", b"[]", b'{"latest": "v2"}', b"x" * (update_check.MAX_BYTES + 1)):
            self.assertIsNone(self.banner_after(lambda _url, raw=raw: raw), raw[:20])
        self.assertEqual(self.banner_after(answer("2.0.0"))["text"], "Sidedoor 2.0.0 is available.")
        for get in (offline, lambda _url: b"not json", answer("3.0.0", "https://evil.example/d")):
            banner = self.banner_after(get)  # a later failure keeps the last good answer
            self.assertEqual(banner["text"], "Sidedoor 2.0.0 is available.")

    def test_pre_release_is_not_understood(self):
        self.assertIsNone(self.banner_after(answer("1.2.0-rc1")))

    def test_bad_download_url_shows_none(self):
        for url in ("http://yoursidedoor.com/d", "https://evil.example/d", "https://yoursidedoor.com.evil.example/d",
                    "https://user@github.com/d", "javascript:alert(1)", None, "https://github.com/someone/else/releases/x",
                    "https://yoursidedoor.com:99999/d"):
            self.assertIsNone(self.banner_after(answer("2.0.0", url)), url)
        self.assertIsNotNone(self.banner_after(answer("2.0.0", "https://github.com/yoursidedoor/sidedoor/releases/tag/v2.0.0")))

    def test_dismiss_hides_until_a_newer_version(self):
        self.banner_after(answer("1.2.0"))
        self.assertTrue(update_check.dismiss("1.2.0", self.home))
        self.assertIsNone(update_check.banner(self.home))
        self.assertIsNone(self.banner_after(answer("1.2.0")))  # the next day's check, same version
        self.assertEqual(self.banner_after(answer("1.3.0"))["text"], "Sidedoor 1.3.0 is available.")

    def test_broken_state_file_never_raises(self):
        (self.home / update_check.STATE_FILE).write_text("{broken")
        self.assertIsNone(update_check.banner(self.home))
        (self.home / update_check.STATE_FILE).write_text('{"available": {"latest": 3}}')
        self.assertIsNone(update_check.banner(self.home))

    def test_fetch_has_a_total_deadline(self):
        release = threading.Event()
        self.addCleanup(release.set)
        with self.assertRaises(TimeoutError):
            update_check._get("https://x", fetch=lambda _url: release.wait(5), deadline=0.05)
        self.assertEqual(update_check._get("https://x", fetch=lambda _url: b"ok"), b"ok")

    def test_redirect_to_http_is_refused(self):
        handler = update_check._HttpsRedirectsOnly()
        request = urllib.request.Request("https://yoursidedoor.com/version.json")
        with self.assertRaises(urllib.error.HTTPError):
            handler.redirect_request(request, None, 302, "Found", {}, "http://yoursidedoor.com/version.json")
        self.assertIsNotNone(handler.redirect_request(request, None, 302, "Found", {},
                                                      "https://www.yoursidedoor.com/version.json"))


@unittest.skipUnless(DEPENDENCIES, "dashboard packages (fastapi, httpx) are not installed")
class UpdateBannerPageTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.home = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def client(self):
        client = TestClient(dashboard.create_app(SECRET, PORT, home=self.home), base_url=f"http://127.0.0.1:{PORT}",
                            follow_redirects=False)
        client.get("/", params={"t": SECRET})
        return client

    def test_banner_renders_last_and_dismiss_hides_it(self):
        update_check.check(self.home, answer("999.0.0", notes="<b>bold</b>"))
        client = self.client()
        page = client.get("/applications").text
        self.assertIn("Sidedoor 999.0.0 is available.", page)
        self.assertIn('<label class="banner-link" for="update-notes-toggle">What\'s new</label>', page)
        self.assertIn('<p class="banner-notes-panel">', page)  # below the row, full width
        self.assertIn('class="button" href="https://yoursidedoor.com/download"', page)  # Windows: Download stays the button
        self.assertEqual(page.count(">Dismiss</button>"), 1)
        self.assertIn("&lt;b&gt;bold&lt;/b&gt;", page)  # notes are escaped
        self.assertGreater(page.index("Sidedoor 999.0.0"), page.index("Setup is not finished"))
        response = client.post("/update/dismiss", data={"version": "999.0.0", "next": "/applications"},
                               headers={"origin": f"http://127.0.0.1:{PORT}"})
        self.assertEqual((response.status_code, response.headers["location"]), (303, "/applications"))
        self.assertNotIn("Sidedoor 999.0.0 is available.", client.get("/applications").text)

    def test_dismiss_returns_to_a_known_sub_path_only(self):
        client = self.client()
        origin = {"origin": f"http://127.0.0.1:{PORT}"}
        for back, expected in (("/setup/guide/codex", "/setup/guide/codex"), ("//evil.example", "/"),
                               ("/settings/../x", "/"), ("/nowhere/x", "/"), ("https://evil.example/", "/")):
            response = client.post("/update/dismiss", data={"version": "1.0.0", "next": back}, headers=origin)
            self.assertEqual(response.headers["location"], expected, back)

    def test_broken_state_file_never_breaks_a_page(self):
        (self.home / update_check.STATE_FILE).write_text("\x00garbage")
        self.assertEqual(self.client().get("/applications").status_code, 200)


if __name__ == "__main__":
    unittest.main()
