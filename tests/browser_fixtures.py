"""Offline helpers for the browser-driver tests (E7.T1): an MCP stdio client, a local fixture web
server, the fake @playwright/mcp, and the real runtime when this machine has one.

Nothing here reaches the internet. Real-browser tests use headless Google Chrome with an in-memory
profile (@playwright/mcp --isolated --headless) against pages served from a temporary directory on
127.0.0.1, never the user's Chrome profile and never Sidedoor's. They are skipped when Node,
@playwright/mcp 0.0.83 or Chrome is not on this machine (nothing is downloaded).
"""

import functools
import http.server
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import urllib.parse

ROOT = Path(__file__).resolve().parent.parent
FAKE_MCP = Path(__file__).resolve().parent / "fake_playwright_mcp.py"


class Client:
    """A minimal stdio MCP client (newline-delimited JSON-RPC), over a process or two streams."""

    def __init__(self, argv=None, cwd=None, env=None, streams=None):
        if streams is None:
            self.process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, cwd=cwd,
                                            env=env, encoding="utf-8")
            self.writer, self.reader = self.process.stdin, self.process.stdout
        else:
            self.process = None
            self.writer, self.reader = streams
        self.counter = 0
        self.calls = 0
        self.stray = []
        self.trace = []  # codex --json shaped items for every tools/call, as a Codex trace would hold them
        self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "e7t1-test", "version": "0"}})
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def send(self, message):
        self.writer.write(json.dumps(message) + "\n")
        self.writer.flush()

    def request(self, method, params=None):
        self.counter += 1
        ident = self.counter
        self.send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}})
        while True:
            line = self.reader.readline()
            if not line:
                raise RuntimeError("server closed")
            message = json.loads(line)
            if "method" not in message and message.get("id") != ident:
                self.stray.append(message)   # an answer to nothing this client is waiting for
                continue
            if message.get("id") == ident and "method" not in message:
                if "error" in message:
                    raise RuntimeError(message["error"])
                return message["result"]
            if "method" in message and "id" in message:  # e.g. roots/list: answer with none
                self.send({"jsonrpc": "2.0", "id": message["id"], "result": {"roots": []}})

    def call(self, name, arguments=None):
        """(isError, text) of one tools/call; also appended to `trace` as a Codex item."""
        self.calls += 1
        result = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        text = "\n".join(c.get("text", "") for c in result.get("content", []) if isinstance(c, dict))
        error = bool(result.get("isError"))
        self.trace.append({"type": "item.completed", "item": {
            "id": "item_%d" % self.calls, "type": "mcp_tool_call", "server": "browser", "tool": name,
            "arguments": arguments or {}, "result": result, "error": None, "status": "failed" if error else "completed"}})
        return error, text

    def nodes(self):
        import browser_guard
        error, _ = self.call("browser_snapshot")
        result = self.trace[-1]["item"]["result"]
        return browser_guard.parse_snapshot(browser_guard.snapshot_text(result) or "")

    def ref(self, role, name):
        found = [ref for ref, node in self.nodes().items() if node["role"] == role and node["name"] == name]
        if len(found) != 1:
            raise AssertionError(f"{role} {name!r}: {found}")
        return found[0]

    def write_trace(self, path):
        Path(path).write_text("".join(json.dumps(item) + "\n" for item in self.trace))

    def close(self):
        try:
            self.writer.close()
        except OSError:
            pass
        if self.process is not None:
            try:
                self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.process.kill()


class Pages(http.server.SimpleHTTPRequestHandler):
    """Serves one temporary directory on 127.0.0.1; GET /hit?... and POST /submit are recorded."""
    hits = []
    posts = []

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/hit":
            Pages.hits.append({key: values[0] for key, values in urllib.parse.parse_qs(url.query).items()})
            self.send_response(204)
            self.end_headers()
            return
        super().do_GET()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        Pages.posts.append({"path": self.path, "body": self.rfile.read(length).decode("utf-8", "replace")})
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html><body><h1>Thank you for applying</h1></body></html>")

    def log_message(self, *args):
        pass


def serve(directory):
    """(server, base URL) for `directory`; call server.shutdown() when done."""
    Pages.hits.clear()
    Pages.posts.clear()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Pages, directory=str(directory)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/"


def real_runtime():
    """config["browser_driver"] for a real, verified Sidedoor browser runtime on this machine (headless,
    isolated), or None. Looked for, never fetched: $SIDEDOOR_BROWSER_RUNTIME as the test run started,
    then ~/sidedoor-browser-runtime. It must pass browser_runtime.verify (Node NODE_VERSION,
    @playwright/mcp PLAYWRIGHT_MCP_VERSION), and Google Chrome must be installed."""
    import browser_runtime
    import profile_home
    import setup_steps
    if not setup_steps.chrome_path():
        return None
    for folder in filter(None, (profile_home.REAL_RUNTIME, str(Path.home() / "sidedoor-browser-runtime"))):
        driver = {"runtime_dir": folder, "isolated": True, "headless": True}
        if not browser_runtime.verify({"browser_driver": driver}):
            return driver
    return None


def fake_runtime(snapshot_path, calls_path):
    """A guard child argv that runs the fake @playwright/mcp instead of Node."""
    return [sys.executable, str(FAKE_MCP), str(snapshot_path), str(calls_path)]
