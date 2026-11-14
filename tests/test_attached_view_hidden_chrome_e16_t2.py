"""E16.T2, against the product's hidden Chrome and a local page (not part of the ordinary suite: it starts a
real Chrome, headless only, on a throwaway profile with a mock keychain, so nothing is shown and no keychain
is asked; run with SIDEDOOR_E16_HIDDEN_CHROME=1). The attached view's own client (`live_view.Chrome`) is what
is driven: a press on the form's Submit and Enter in a field are not passed on; a tick in the check's frame
and a press elsewhere are. It also prints the seconds from launch to a painted page."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import asyncio
import http.server
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

import browser_runtime
import live_view

PAGE = b"""<!doctype html><title>Apply</title><body style="margin:0">
<form id="f" onsubmit="window.submitted=(window.submitted||0)+1;return false" style="padding:20px">
<input id="name" name="name" style="display:block;width:300px;height:30px">
<iframe id="check" src="/check" style="display:block;width:300px;height:80px;border:1px solid #888;margin:20px 0"></iframe>
<button id="other" type="button" onclick="window.otherCount=(window.otherCount||0)+1" style="width:200px;height:40px">Add another</button>
<button id="send" style="display:block;width:200px;height:40px;margin-top:20px">Submit application</button>
</form></body>"""
CHECK = b"""<!doctype html><body style="margin:0"><label style="display:block;padding:20px">
<input id="box" type="checkbox" style="width:30px;height:30px"> I am human</label></body>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = CHECK if self.path == "/check" else PAGE
        self.send_response(200)
        self.send_header("content-type", "text/html")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@unittest.skipUnless(os.environ.get("SIDEDOOR_E16_HIDDEN_CHROME") == "1", "starts a real (hidden) Chrome: opt in")
class HiddenChrome(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import setup_steps
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d/apply" % self.server.server_address[1]
        self.profile = Path(tempfile.mkdtemp(prefix="e16-hidden-chrome-"))
        command = browser_runtime.headless_command(setup_steps.chrome_path(), self.profile, "Mozilla/5.0 (Macintosh) e16-test")
        command = command[:-1] + ["--use-mock-keychain", "--password-store=basic", "--no-first-run", command[-1]]
        self.started = time.monotonic()
        self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        port_file = self.profile / browser_runtime.DEVTOOLS_PORT_FILE
        deadline = time.monotonic() + 30
        while not port_file.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        self.endpoint = "http://127.0.0.1:" + port_file.read_text().splitlines()[0]
        self.launched = time.monotonic() - self.started
        self.acted = []
        self.chrome = live_view.Chrome(self.endpoint, protect_submit=True, on_acted=lambda: self.acted.append(1))
        await self.chrome.start()

    async def asyncTearDown(self):
        await self.chrome.close(wait=1)
        self.process.terminate()                       # this test's own Chrome, by its own process handle
        try:
            self.process.wait(10)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.profile, ignore_errors=True)

    async def value(self, expression):
        session = self.chrome.sessions[self.chrome.active]
        answer = await self.chrome.call("Runtime.evaluate", {"expression": expression, "returnByValue": True}, session)
        return (answer.get("result") or {}).get("value")

    async def centre(self, expression):
        box = json.loads(await self.value(
            "(function(){var r=(%s).getBoundingClientRect();return JSON.stringify([r.left+r.width/2,r.top+r.height/2])})()" % expression))
        return box

    async def click(self, x, y):
        for kind, buttons in (("mousePressed", 1), ("mouseReleased", 0)):
            self.chrome.input({"t": "mouse", "type": kind, "x": x, "y": y, "button": "left", "buttons": buttons, "clicks": 1})
        await asyncio.sleep(0.6)

    async def test_submit_is_not_pressed_from_the_view_and_the_check_is(self):
        session = self.chrome.sessions[self.chrome.active]
        navigated = time.monotonic()
        await self.chrome.call("Page.navigate", {"url": self.url}, session)
        deadline = time.monotonic() + 15
        while await self.value("document.readyState") != "complete" and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        painted = time.monotonic() - navigated
        print("\nE16 hidden Chrome: launch to DevTools %.2f s; navigate to a painted local page %.2f s; total %.2f s"
              % (self.launched, painted, self.launched + painted))
        self.assertEqual(await self.value("document.title"), "Apply")
        # A press on the form's Submit: not passed on.
        await self.click(*await self.centre("document.getElementById('send')"))
        self.assertIsNone(await self.value("window.submitted"))
        self.assertEqual(self.chrome.counts.get("submit_refused"), 1)
        # A press on another button, and in the field: passed on.
        await self.click(*await self.centre("document.getElementById('other')"))
        self.assertEqual(await self.value("window.otherCount"), 1)
        await self.click(*await self.centre("document.getElementById('name')"))
        self.assertEqual(await self.value("document.activeElement.id"), "name")
        # Enter in a field of the form: not passed on. A letter: passed on.
        for kind in ("down", "up"):
            self.chrome.input({"t": "key", "type": kind, "key": "Enter", "code": "Enter", "mod": 0})
        for kind in ("down", "up"):
            self.chrome.input({"t": "key", "type": kind, "key": "a", "code": "KeyA", "mod": 0})
        await asyncio.sleep(0.6)
        self.assertIsNone(await self.value("window.submitted"))
        self.assertEqual(await self.value("document.getElementById('name').value"), "a")
        self.assertEqual(self.chrome.counts.get("submit_refused"), 2)
        # The tick, inside the check's own frame: passed on.
        frame = await self.centre("document.getElementById('check')")
        inner = json.loads(await self.value(
            "(function(){var f=document.getElementById('check'),r=f.getBoundingClientRect(),"
            "b=f.contentDocument.getElementById('box').getBoundingClientRect();"
            "return JSON.stringify([r.left+f.clientLeft+b.left+b.width/2,r.top+f.clientTop+b.top+b.height/2])})()"))
        self.assertTrue(frame)
        await self.click(*inner)
        self.assertIs(await self.value("document.getElementById('check').contentDocument.getElementById('box').checked"), True)
        self.assertIsNone(await self.value("window.submitted"))
        # Space on the focused Submit: not passed on. (Focus is put there by the page's own script, not by a press.)
        await self.value("document.getElementById('send').focus()")
        for kind in ("down", "up"):
            self.chrome.input({"t": "key", "type": kind, "key": " ", "code": "Space", "mod": 0})
        await asyncio.sleep(0.6)
        self.assertIsNone(await self.value("window.submitted"))
        self.assertEqual(self.chrome.counts.get("submit_refused"), 3)
        self.assertGreaterEqual(len(self.acted), 4)                            # the presses and keys that were passed on
        # The same client with no protection (an ordinary view) does press it: the page really submits on that press.
        self.chrome.protect_submit = False
        await self.click(*await self.centre("document.getElementById('send')"))
        self.assertEqual(await self.value("window.submitted"), 1)


if __name__ == "__main__":
    unittest.main()
