"""The dashboard shell (E6.T1 option A): token, Host, Origin, headers, layout, read-only reads.

Needs the pinned dashboard packages and httpx (`pip install -e '.[test]'`); skipped without them.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import contextlib
import importlib.util
import io
import os
import re
import socket
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# A private file's mode: Windows keeps only the read-only bit (0o666 when writable); its privacy
# there is the ACL of the user's profile folder, which the home inherits (VM pass, 2026-10-23).
PRIVATE = 0o666 if os.name == "nt" else 0o600

DEPENDENCIES = all(importlib.util.find_spec(name) for name in ("fastapi", "httpx"))
if DEPENDENCIES:
    from fastapi.testclient import TestClient

    import dashboard
    import version


PORT = 54321
SECRET = "test-token-value"


@unittest.skipUnless(DEPENDENCIES, "dashboard packages (fastapi, httpx) are not installed")
class DashboardTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.home = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def client(self, host="127.0.0.1"):
        return TestClient(dashboard.create_app(SECRET, PORT, home=self.home), base_url=f"http://{host}:{PORT}",
                          follow_redirects=False)

    def signed_in(self):
        client = self.client()
        client.get("/", params={"t": SECRET})
        return client

    def test_no_token_is_refused(self):
        self.assertEqual(self.client().get("/").status_code, 403)
        self.assertEqual(self.client().get("/", params={"t": "wrong"}).status_code, 403)
        self.assertEqual(self.client().get("/static/dashboard.css").status_code, 403)

    def test_token_link_sets_cookie_and_redirects_without_token(self):
        response = self.client().get("/sent", params={"t": SECRET})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/sent")
        cookie = response.headers["set-cookie"].lower()
        for attribute in ("httponly", "samesite=strict", "path=/"):
            self.assertIn(attribute, cookie)

    def test_cookie_admits_and_localhost_is_a_local_host(self):
        self.assertEqual(self.signed_in().get("/").status_code, 200)
        client = self.client("localhost")
        client.get("/", params={"t": SECRET})
        self.assertEqual(client.get("/settings").status_code, 200)

    def test_other_host_is_refused(self):
        client = self.signed_in()
        self.assertEqual(client.get("/", headers={"host": "evil.example:%d" % PORT}).status_code, 400)
        self.assertEqual(client.get("/", headers={"host": "127.0.0.1:1"}).status_code, 400)

    def test_pages_keep_the_origin_on_plain_form_posts(self):
        # Under "no-referrer" a browser sends "Origin: null" on a plain <form method=post> (Update now was
        # refused, 2026-10-24); "same-origin" keeps our own Origin and still sends no referrer elsewhere.
        client = self.client()
        self.assertEqual(client.get("/").headers["referrer-policy"], "same-origin")
        self.assertEqual(client.post("/", headers={"origin": "null"}).status_code, 403)

    def test_cross_origin_post_is_refused(self):
        client = self.signed_in()
        self.assertEqual(client.post("/", headers={"origin": "http://evil.example"}).status_code, 403)
        self.assertEqual(client.post("/").status_code, 403)
        # A local Origin passes the guard and reaches routing (no page accepts POST yet).
        self.assertEqual(client.post("/", headers={"origin": f"http://127.0.0.1:{PORT}"}).status_code, 405)

    def test_security_headers(self):
        for response in (self.client().get("/"), self.signed_in().get("/")):
            policy = response.headers["content-security-policy"]
            self.assertIn("script-src 'self'", policy)
            self.assertIn("frame-ancestors 'none'", policy)
            self.assertNotIn("unsafe-inline", policy)
            self.assertEqual(response.headers["x-frame-options"], "DENY")
            self.assertEqual(response.headers["cache-control"], "no-store")

    def test_layout_renders_logo_favicon_and_tabs(self):
        client = self.signed_in()
        page = client.get("/applications").text
        v = "?v=" + version.current()
        self.assertIn('src="/brand/yoursidedoor-logo.png%s"' % v, page)
        for icon in ("yoursidedoor-icon-win-32.png", "yoursidedoor-icon-win-16.png", "yoursidedoor-apple-touch-180.png"):
            self.assertIn('href="/brand/%s%s"' % (icon, v), page)
            self.assertEqual(client.get("/brand/" + icon).status_code, 200)
        self.assertNotIn("<script>", page)
        for key, label, path in dashboard.TABS + (dashboard.SETUP_TAB,):
            self.assertIn(f'href="{path}"', page)
            self.assertEqual(client.get(path).status_code, 200)
        self.assertIn('class="tab active" href="/applications"', page)
        self.assertEqual(client.get("/brand/yoursidedoor-logo.png").status_code, 200)

    def test_every_asset_reference_carries_the_installed_version(self):
        # An update that changes the logo or a script must reach browsers that cached the old file.
        client = self.signed_in()
        v = "?v=" + version.current()
        for key, label, path in dashboard.TABS + (dashboard.SETUP_TAB,) + (("doctor", "", "/failures/doctor"),):
            page = client.get(path).text
            urls = re.findall(r'(?:src|href)="(/(?:static|brand)/[^"]*)"', page)
            self.assertTrue(urls, path)
            for url in urls:
                self.assertTrue(url.endswith(v), (path, url))
                self.assertEqual(client.get(url).status_code, 200, url)

    def test_vendored_htmx_is_served(self):
        response = self.signed_in().get("/static/htmx.min.js")
        self.assertEqual(response.status_code, 200)
        self.assertIn("htmx 2.0.11", response.text[:200])

    def test_read_connection_cannot_write(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "state").mkdir()
            database = sqlite3.connect(Path(root) / dashboard.DATABASE)
            database.execute("CREATE TABLE t (x)")
            database.close()
            database = dashboard.read_connection(root)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    database.execute("INSERT INTO t VALUES (1)")
            finally:
                database.close()

    def test_token_file_is_private_and_reused(self):
        with tempfile.TemporaryDirectory() as home:
            first = dashboard.token(home)
            self.assertEqual(dashboard.token(home), first)
            mode = stat.S_IMODE((Path(home) / dashboard.TOKEN_FILE).stat().st_mode)
            self.assertEqual(mode, PRIVATE)


    def serve(self, platform, bound=True, ours=False, path="/"):
        """Run serve() with the server stopped by Ctrl-C at once; returns (status, browser opened, output)."""
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.object(dashboard.sys, "platform", platform), \
                mock.patch.object(dashboard.webbrowser, "open") as opened, \
                mock.patch.object(dashboard, "bind", side_effect=None if bound else OSError(48, "in use")) as bind, \
                mock.patch.object(dashboard, "running_here", return_value=ours), \
                mock.patch.object(dashboard.uvicorn, "Server") as server, \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            server.return_value.run.side_effect = KeyboardInterrupt
            status = dashboard.serve(home=home, path=path)
            secret = dashboard.token(home)
        text = output.getvalue()
        self.assertEqual(bind.call_args, mock.call())
        if opened.called:
            self.assertEqual(opened.call_args[0][0], f"http://127.0.0.1:47823{path}?t={secret}")
        return status, opened.called, text

    def test_linux_prints_the_link_without_opening_a_browser(self):
        self.assertEqual(self.serve("linux")[:2], (0, False))
        self.assertIn("http://127.0.0.1:47823/?t=", self.serve("linux")[2])
        self.assertEqual(self.serve("darwin")[:2], (0, True))
        self.assertEqual(self.serve("win32")[:2], (0, True))

    def test_one_fixed_port(self):
        self.assertEqual(dashboard.PORT, 47823)
        self.assertEqual(self.serve("darwin", path="/setup")[:2], (0, True))

    def test_a_running_dashboard_is_reopened_not_started_twice(self):
        status, opened, text = self.serve("darwin", bound=False, ours=True, path="/setup")
        self.assertEqual((status, opened), (0, True))
        self.assertIn("already running", text)
        status, opened, text = self.serve("linux", bound=False, ours=True)
        self.assertEqual((status, opened), (0, False))
        self.assertIn("?t=", text)

    def test_port_held_by_another_program_is_a_plain_error(self):
        status, opened, text = self.serve("darwin", bound=False, ours=False)
        self.assertEqual((status, opened), (1, False))
        self.assertIn("Port 47823 is in use by another program. Close it and open Sidedoor again.", text)
        self.assertNotIn("?t=", text)

    def test_bind_refuses_beside_another_programs_wildcard_listener(self):
        other = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        other.bind(("0.0.0.0", 0))
        other.listen()
        port = other.getsockname()[1]
        try:
            with self.assertRaises(OSError):
                dashboard.bind(port)
        finally:
            other.close()
        listener = dashboard.bind(port)
        listener.close()

    def test_session_cookie_is_persistent(self):
        cookie = self.client().get("/", params={"t": SECRET}).headers["set-cookie"].lower()
        self.assertIn("max-age=%d" % dashboard.COOKIE_MAX_AGE, cookie)

    def test_ping_proves_the_token_without_sending_it(self):
        client = self.client()
        response = client.get(dashboard.PING_PATH, params={"n": "a" * 32})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, dashboard.ping_answer(SECRET, "a" * 32))
        self.assertNotIn(SECRET, response.text)
        self.assertEqual(client.get(dashboard.PING_PATH, params={"n": "short"}).status_code, 400)
        self.assertEqual(client.get(dashboard.PING_PATH, params={"n": "a" * 32},
                                    headers={"host": "evil.example:%d" % PORT}).status_code, 400)
        self.assertEqual(client.post(dashboard.PING_PATH, headers={"origin": f"http://127.0.0.1:{PORT}"}).status_code, 403)

    def test_running_here_checks_the_answer(self):
        class Response:
            status = 200

            def __init__(self, body):
                self.body = body

            def read(self, limit):
                return self.body

        for answer, expected in ((None, True), (b"wrong", False)):
            with mock.patch.object(dashboard.http.client, "HTTPConnection") as connection:
                def respond(method, path, headers):
                    nonce = path.split("n=")[1]
                    body = answer or dashboard.ping_answer(SECRET, nonce).encode()
                    connection.return_value.getresponse.return_value = Response(body)
                connection.return_value.request.side_effect = respond
                self.assertIs(dashboard.running_here(SECRET), expected)
                self.assertNotIn(SECRET, str(connection.return_value.request.call_args))
        with mock.patch.object(dashboard.http.client, "HTTPConnection") as connection:
            connection.return_value.request.side_effect = ConnectionRefusedError
            self.assertFalse(dashboard.running_here(SECRET))


if __name__ == "__main__":
    unittest.main()
