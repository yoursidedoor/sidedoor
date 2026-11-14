"""A stand-in for `sidedoor browser-guard` in the live view's tests (E15.T5): MCP JSON-RPC over stdio, the
audit rows the real guard writes (BACKGROUND_LAUNCHED with the endpoint, FORWARDED with a call's arguments),
and an exit when its input closes. It starts no browser.

--no-endpoint: the Chrome never started (no BACKGROUND_LAUNCHED row).
--windowed: the guard fell back to a Chrome with a minimized window (the row has no headless mark).
--hidden-failed: Windows, the hidden launch failed (HEADLESS_LAUNCH_FAILED and no BACKGROUND_LAUNCHED row).
--hang: nothing is ever answered (a start that would hang).
--orphan-chrome <profile>: start a sleeping stand-in process whose command line looks like a session's Chrome on
  that profile, write the profile's SingletonLock for it, and leave it behind on exit (the crash test).
--site-session <site> --cookies <file>: what the real guard does for a site's saved session (E15 join;
  browser_guard.Proxy.restore_site_session / capture_site_session): the saved cookies are put back at the start,
  and when its input closes the cookies its Chrome then holds (the JSON list in <file>: what the site set while
  the person was there) are kept as this session's capture, through site_sessions' own save. Audit rows hold
  counts only.
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import time


class Jar:
    """A cookie jar that answers the two pieces of code site_sessions sends through the guard's own call."""

    def __init__(self, url, cookies):
        self.url, self.jar = url, list(cookies)

    def call(self, code):
        import re
        given = json.loads(re.search(r"const input = (.*);\n", code).group(1))
        if given["sidedoor"] == "site-session-capture":
            return [{"url": self.url, "cookies": [dict(item) for item in self.jar]}]
        held = {(item["name"], item["domain"].lstrip("."), item["path"]) for item in self.jar}
        absent = [item for item in given["cookies"] if (item["name"], item["domain"].lstrip("."), item["path"]) not in held]
        self.jar.extend(absent)
        return [{"restored": len(absent), "kept": len(given["cookies"]) - len(absent), "failed": 0}]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--site-session")
    parser.add_argument("--cookies")
    parser.add_argument("--page")   # the page its Chrome is on when the session ends (default: the site's own)
    parser.add_argument("--audit", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--no-endpoint", action="store_true")
    parser.add_argument("--windowed", action="store_true")
    parser.add_argument("--hidden-failed", action="store_true")
    parser.add_argument("--hang", action="store_true")
    parser.add_argument("--orphan-chrome")
    options = parser.parse_args()

    def log(**row):
        row["at"] = time.time()
        with open(options.audit, "a", encoding="utf-8") as audit:
            audit.write(json.dumps(row) + "\n")

    if options.orphan_chrome:
        os.makedirs(options.orphan_chrome, exist_ok=True)
        stand_in = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)", "--user-data-dir=" + options.orphan_chrome,
             "--remote-debugging-port=0"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
        os.symlink("%s-%d" % (socket.gethostname(), stand_in.pid), os.path.join(options.orphan_chrome, "SingletonLock"))
    if options.windowed:   # the real guard's fallback: Chrome in a minimized window, no headless mark
        log(decision="HEADLESS_LAUNCH_FAILED", reason="test")
        log(decision="BACKGROUND_LAUNCHED", endpoint=options.endpoint)
    elif options.hidden_failed:   # Windows: the hidden launch failed; @playwright/mcp would open a window itself
        log(decision="HEADLESS_LAUNCH_FAILED", reason="test")
    elif not options.no_endpoint:
        log(decision="BACKGROUND_LAUNCHED", endpoint=options.endpoint, headless=True)
    jar = None
    if options.site_session:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import site_sessions
        try:
            with open(options.cookies, encoding="utf-8") as stream:
                cookies = json.load(stream)
        except (OSError, TypeError, ValueError):
            cookies = []
        jar = Jar(options.page or "https://%s/" % options.site_session, cookies)
        done = site_sessions.restore(options.site_session, jar.call)
        log(decision="SITE_SESSION_RESTORED" if done.pop("outcome") == "restored" else "SITE_SESSION_NOT_RESTORED",
            site=options.site_session, **done)
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if "id" not in message or options.hang:
            continue
        result = {}
        if message.get("method") == "tools/call":
            params = message.get("params") or {}
            log(decision="FORWARDED", tool=params.get("name"), arguments=params.get("arguments"), label=None)
            result = {"content": [{"type": "text", "text": "- Page URL: %s" % (params.get("arguments") or {}).get("url")}]}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}) + "\n")
        sys.stdout.flush()
    if jar is not None:   # the real guard's last capture, while its Chrome still answers
        log(decision="SITE_SESSION_CAPTURED", site=options.site_session,
            sites=site_sessions.save(options.site_session, jar.call, pending=True), final=True)
    log(decision="STOPPED")


if __name__ == "__main__":
    main()
