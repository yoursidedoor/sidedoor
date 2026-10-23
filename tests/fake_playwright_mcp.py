"""A stand-in for @playwright/mcp in offline tests of `sidedoor browser-guard` (E7.T1).

Speaks newline-delimited JSON-RPC on stdio like the real server, lists every tool @playwright/mcp
0.0.83 lists, answers browser_snapshot with the YAML snapshot in the file given as argv[1], and
appends every tools/call it receives to the JSONL file given as argv[2] (so a test can prove a
refused click never reached the "browser"). Standard library only.

  python3 fake_playwright_mcp.py <snapshot.yaml> <calls.jsonl>
"""

import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import browser_guard  # noqa: E402  (the tool list only)


def main(snapshot_path, calls_path):
    # Test knobs: FAKE_HANG="tool,tool" never answers those tools; FAKE_LATE="tool" answers it after
    # FAKE_DELAY seconds; FAKE_GUARD_DELAY delays answers to the guard's own snapshots (ids guard-N).
    hang = set(filter(None, os.environ.get("FAKE_HANG", "").split(",")))
    late = set(filter(None, os.environ.get("FAKE_LATE", "").split(",")))
    delay = float(os.environ.get("FAKE_DELAY", "0") or 0)
    guard_delay = float(os.environ.get("FAKE_GUARD_DELAY", "0") or 0)
    lock = threading.Lock()

    def reply(message, result, wait=0.0):
        def send():
            if wait:
                time.sleep(wait)
            with lock:
                sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}) + "\n")
                sys.stdout.flush()
        if wait:
            threading.Thread(target=send, daemon=True).start()
        else:
            send()

    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if "id" not in message or "method" not in message:
            continue
        method, params = message["method"], message.get("params") or {}
        if method == "initialize":
            result = {"protocolVersion": params.get("protocolVersion", "2025-06-18"), "capabilities": {"tools": {}},
                      "serverInfo": {"name": "fake-playwright-mcp", "version": "0"}}
        elif method == "tools/list":
            result = {"tools": [{"name": name, "inputSchema": {"type": "object"}} for name in browser_guard.TOOLS]}
        elif method == "tools/call":
            name, arguments = params.get("name"), params.get("arguments") or {}
            if not str(message["id"]).startswith("guard-"):
                with open(calls_path, "a", encoding="utf-8") as calls:
                    calls.write(json.dumps({"tool": name, "arguments": arguments}) + "\n")
            with open(snapshot_path, encoding="utf-8") as handle:
                snapshot = handle.read()
            text = "### Page\n- Page URL: https://www.example.test/page\n- Page Title: Fixture\n"
            if name == "browser_snapshot":
                text += "### Snapshot\n```yaml\n" + snapshot + "\n```"
            else:
                text = ("### Ran Playwright code\n```js\nawait page.fake(" + json.dumps(arguments) + ");\n```\n" + text)
            result = {"content": [{"type": "text", "text": text}], "isError": False}
        else:
            result = {}
        name = params.get("name") if method == "tools/call" else None
        if name in hang:
            continue
        guard = str(message["id"]).startswith("guard-")
        reply(message, result, guard_delay if guard else (delay if name in late else 0.0))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
