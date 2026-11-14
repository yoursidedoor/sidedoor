"""Stand-ins for the live view's tests (E15.T5): a scripted Chrome DevTools websocket server and the command
line of a fake browser guard (tests/fake_live_guard.py). No browser, no Node, nothing outside 127.0.0.1."""

import asyncio
import base64
import http
import json
import logging
import sys
import threading
import time
from pathlib import Path

import browser_runtime

GUARD = Path(__file__).resolve().parent / "fake_live_guard.py"
FRAME = b"FAKEJPEG"   # every fake picture starts with this, so a scan can look for one on disk
QUIET = logging.Logger("fake-chrome", level=logging.CRITICAL + 1)   # Chrome is where typed text is meant to arrive
HIDDEN_REPLY_SECONDS = 5.0   # what headless Chrome does for a mouse event on a window that is not the front one


def guard_command(chrome, *extra):
    """A `guard_command` for LiveView: the fake guard, pointing at `chrome` (a FakeChrome). Counts its starts."""
    def command(config, directory):
        command.starts += 1
        return [sys.executable, str(GUARD), "--audit", str(Path(directory) / browser_runtime.GUARD_AUDIT),
                "--endpoint", chrome.endpoint, *extra]
    command.starts = 0
    return command


class FakeChrome:
    """One browser endpoint. Pages are {targetId: info}; exactly one is the front (visible) window. A mouse
    event on a page that is not the front one is answered after HIDDEN_REPLY_SECONDS, as headless Chrome does;
    `delays[method]` delays any other answer. Everything received is kept, in order, in `received`."""

    def __init__(self, pages=None):
        self.pages = dict(pages or {"MAIN": {"targetId": "MAIN", "type": "page", "url": "about:blank", "title": ""}})
        self.front = next(iter(self.pages))
        self.delays, self.received, self.casting, self.frames = {}, [], set(), 0
        self.connections, self.loop, self.server, self.port = set(), None, None, None
        self.ready, self.lock = threading.Event(), threading.Lock()
        self.thread = threading.Thread(target=self._run, daemon=True)

    # --- lifecycle (test thread) ---
    def start(self):
        self.thread.start()
        assert self.ready.wait(10)
        return self

    @property
    def endpoint(self):
        return "http://127.0.0.1:%d" % self.port

    def stop(self):
        if self.loop is not None and self.loop.is_running():
            self.loop.call_soon_threadsafe(self._stopping.set)
            self.thread.join(10)

    def _run(self):
        asyncio.run(self._serve())

    async def _serve(self):
        from websockets.asyncio.server import serve
        self.loop, self._stopping = asyncio.get_running_loop(), asyncio.Event()

        def version(connection, request):
            if request.path == "/json/version":
                return connection.respond(http.HTTPStatus.OK, json.dumps(
                    {"webSocketDebuggerUrl": "ws://127.0.0.1:%d/devtools/browser/fake" % self.port}))
            return None

        async with serve(self._client, "127.0.0.1", 0, process_request=version, max_size=None, logger=QUIET) as server:
            self.port = server.sockets[0].getsockname()[1]
            self.ready.set()
            await self._stopping.wait()

    # --- scripting (test thread) ---
    def do(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(10)

    def open_page(self, target, url, opener=None, take_front=False):
        info = {"targetId": target, "type": "page", "url": url, "title": ""}
        if opener:
            info["openerId"] = opener
        self.pages[target] = info
        if take_front:
            self.front = target
        self.do(self._emit({"method": "Target.targetCreated", "params": {"targetInfo": info}}))

    def change(self, target, url):
        self.pages[target] = dict(self.pages[target], url=url)
        self.do(self._emit({"method": "Target.targetInfoChanged", "params": {"targetInfo": self.pages[target]}}))

    def close_page(self, target):
        self.pages.pop(target, None)
        self.do(self._emit({"method": "Target.targetDestroyed", "params": {"targetId": target}}))

    def take_front(self, target):
        self.front = target

    def frame(self, target):
        self.do(self._frame("S-" + target))

    def drop_clients(self):
        self.do(self._drop())

    def methods(self, method=None, since=0):
        with self.lock:
            rows = list(self.received[since:])
        return [row for row in rows if method is None or row["method"] == method]

    def wait_for(self, check, seconds=3.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if check():
                return True
            time.sleep(0.01)
        return bool(check())

    # --- the server (its own loop) ---
    async def _emit(self, message):
        text = json.dumps(message)
        for connection in list(self.connections):
            try:
                await connection.send(text)
            except Exception:
                pass

    async def _drop(self):
        for connection in list(self.connections):
            await connection.close()

    async def _frame(self, session):
        self.frames += 1
        data = base64.b64encode(FRAME + b":%s:%d" % (session.encode(), self.frames)).decode()
        await self._emit({"method": "Page.screencastFrame", "sessionId": session, "params": {
            "data": data, "sessionId": self.frames, "metadata": {"deviceWidth": 1280, "deviceHeight": 900}}})

    async def _client(self, connection):
        self.connections.add(connection)
        try:
            async for raw in connection:
                message = json.loads(raw)
                with self.lock:
                    self.received.append({"method": message.get("method"), "params": message.get("params"),
                                          "session": message.get("sessionId"), "at": time.monotonic()})
                asyncio.create_task(self._answer(connection, message))   # a slow answer never holds up the next
        except Exception:
            pass
        finally:
            self.connections.discard(connection)

    async def _answer(self, connection, message):
        method, params, session = message.get("method"), message.get("params") or {}, message.get("sessionId")
        target = session[2:] if session else None
        delay = self.delays.get(method, 0)
        if method == "Input.dispatchMouseEvent" and target != self.front:
            delay = max(delay, HIDDEN_REPLY_SECONDS)
        if delay:
            await asyncio.sleep(delay)
        result = {}
        if method == "Target.getTargets":
            result = {"targetInfos": list(self.pages.values())}
        elif method == "Target.attachToTarget":
            result = {"sessionId": "S-" + params["targetId"]}
        elif method == "Target.activateTarget":
            self.front = params["targetId"]
        elif method == "Page.bringToFront":
            self.front = target
        elif method == "Runtime.evaluate" and "sidedoor-submit-check" in params.get("expression", ""):
            # An attached view asks whether a press or a key would submit: `submits(expression)` says (default no).
            asked = getattr(self, "submits", None)
            if asked is None or asked(params["expression"]) is not None:
                result = {"result": {"type": "boolean", "value": bool(asked(params["expression"])) if asked else False}}
        elif method == "Runtime.evaluate":
            result = {"result": {"type": "string", "value": "visible" if target == self.front else "hidden"}}
        elif method == "Page.startScreencast":
            self.casting.add(session)
        elif method == "Page.stopScreencast":
            self.casting.discard(session)
        try:
            await connection.send(json.dumps({"id": message["id"], "result": result}))
        except Exception:
            return
        if method == "Page.startScreencast":
            await self._frame(session)   # Chrome sends one picture when the frames start, then only on change
