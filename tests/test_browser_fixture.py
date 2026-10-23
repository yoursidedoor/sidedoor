import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import http.server
import os
from pathlib import Path
import tempfile
import threading
import unittest

import ats_browser


HTML = b'''<!doctype html><title>Local ATS fixture</title><form method="post">
<label for="email">Email*</label><input id="email" name="email" required type="email">
<label for="resume">Resume/CV*</label><input id="resume" name="resume" type="file" required>
<input required aria-hidden="true" tabindex="-1">
<label for="sensitive">Optional demographic question</label><input id="sensitive" name="sensitive">
<button type="submit">Submit application</button></form>'''


@unittest.skipUnless(os.environ.get("JOB_SEARCH_BROWSER_TESTS") == "1", "Run explicitly with isolated Playwright environment")
class BrowserFixtureTests(unittest.TestCase):
    def test_real_browser_fixture_upload_and_single_click_without_live_submission(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            submissions = []

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(HTML)

            def do_POST(self):
                type(self).submissions.append(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'Local fixture acknowledged; not an employer receipt')

            def log_message(self, *_):
                pass

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                base = root / 'state/ats-browser'
                base.mkdir(parents=True)
                (base / 'browsers').symlink_to(ats_browser.ROOT / 'state/ats-browser/browsers', target_is_directory=True)
                resume = root / 'fixture.pdf'
                resume.write_bytes(b'%PDF-fixture-not-a-real-resume')
                with ats_browser.BrowserPage(root, 'local-fixture-only', f'http://127.0.0.1:{server.server_port}') as page:
                    fields = page.fields()
                    self.assertEqual(len(fields), 3)
                    page.fill(fields, {'email': 'mock@example.invalid'}, resume)
                    self.assertEqual(page.page.locator('#sensitive').input_value(), '')
                    self.assertEqual(page.page.locator('#resume').evaluate('(element)=>element.files[0].name'), 'fixture.pdf')
                    with page.page.expect_navigation():
                        page.submit()
                    self.assertIn('not an employer receipt', page.observation()['text'])
                self.assertEqual(len(Handler.submissions), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
