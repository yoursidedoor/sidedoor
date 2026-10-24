"""E9.T3 Update check and banner.

Once at dashboard start and then every 30 minutes (owner, 2026-10-23: "it should happen once every
hour. or 30mins"), the dashboard process reads a small JSON from `VERSION_URL`, shaped
{"latest": "1.2.0", "download_url": "https://...", "notes": "...", "mac": {"arm64": {"url", "sha256"},
"x86_64": {"url", "sha256"}}} ("mac" optional; an older Sidedoor ignores it), and compares it with the installed version (`version.current()`). When it is newer, every page
shows a dismissible banner, after every other banner (license problems first). Dismissing hides it
until a newer version appears; the dismissal and the last good answer live in the Sidedoor home
(`update-check.json`). Any failure (offline, bad JSON, a non-https download_url, a host other than
yoursidedoor.com or github.com's Sidedoor releases) is silent: no new banner, no error page, and the
last good answer is kept. Nothing is ever downloaded or
installed automatically; [Download] only opens the link in the user's browser. On the built Mac app
in Applications, when version.json names a zip for this Mac, the banner also has [Update now]
(`mac_update`): downloaded, checked and installed only after the user clicks Replace."""

import datetime as dt
import json
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

VERSION_URL = "https://yoursidedoor.com/version.json"
DOWNLOAD_HOSTS = ("yoursidedoor.com", "www.yoursidedoor.com", "github.com", "www.github.com")
STATE_FILE = "update-check.json"
INTERVAL_SECONDS = 30 * 60  # owner, 2026-10-23: every 30 minutes
TIMEOUT_SECONDS = 10
MAX_BYTES = 64 * 1024
MAX_NOTES = 2000
GITHUB_RELEASES = "/yoursidedoor/sidedoor/releases/"  # the only github.com path a download_url may use
DEADLINE_SECONDS = 30  # the whole fetch, connect to last byte
# Releases use plain N.N.N only (version.py, the runbook): a pre-release such as "1.2.0-rc1" or
# "1.2.0b1" is not understood, so a version.json naming one shows no banner.
_VERSION = re.compile(r"(\d{1,6})\.(\d{1,6})\.(\d{1,6})")


def parse_version(text):
    """"1.2.0" -> (1, 2, 0); anything else -> None."""
    if not isinstance(text, str):
        return None
    match = _VERSION.fullmatch(text.strip())
    return tuple(int(part) for part in match.groups()) if match else None


def installed_version():
    """The installed version (`version.current()`, the one source shared with `sidedoor doctor`)."""
    try:
        import version
        return version.current()
    except Exception:
        return None


def _home(home=None):
    if home is not None:
        return Path(home)
    import user_profile
    return user_profile.home()


class _HttpsRedirectsOnly(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to another https URL; anything else fails the fetch."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise urllib.error.HTTPError(newurl, code, "redirect away from https refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch(url):
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Sidedoor"})
    opener = urllib.request.build_opener(_HttpsRedirectsOnly)
    with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
        if urllib.parse.urlsplit(response.geturl()).scheme != "https":
            raise OSError("not https")
        return response.read(MAX_BYTES + 1)


def _get(url, fetch=None, deadline=DEADLINE_SECONDS):
    """`fetch(url)` with a total deadline (the socket timeout bounds each read only). A fetch still
    running at the deadline is left to finish in its daemon thread and ignored."""
    result = {}

    def work():
        try:
            result["body"] = (fetch or _fetch)(url)
        except BaseException as error:
            result["error"] = error

    thread = threading.Thread(target=work, name="sidedoor-update-fetch", daemon=True)
    thread.start()
    thread.join(deadline)
    if thread.is_alive():
        raise TimeoutError("update check took over %d s" % deadline)
    if "error" in result:
        raise result["error"]
    return result["body"]


def _allowed_download(url):
    if not isinstance(url, str):
        return False
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return False
    try:
        port = parts.port
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.username or parts.password or port not in (None, 443):
        return False
    if host in ("github.com", "www.github.com"):
        return parts.path.startswith(GITHUB_RELEASES)
    return host in DOWNLOAD_HOSTS


def parse(raw):
    """The fetched body -> {"latest", "download_url", "notes"}, or None when anything is wrong."""
    if not isinstance(raw, (bytes, str)) or len(raw) > MAX_BYTES:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(value, dict) or parse_version(value.get("latest")) is None:
        return None
    if not _allowed_download(value.get("download_url")):
        return None
    notes = value.get("notes")
    notes = notes.strip()[:MAX_NOTES] if isinstance(notes, str) else ""
    result = {"latest": value["latest"].strip(), "download_url": value["download_url"].strip(), "notes": notes}
    mac = _mac_assets(value.get("mac"))
    if mac:
        result["mac"] = mac
    return result


_SHA256 = re.compile(r"[0-9a-f]{64}")


def _mac_assets(value):
    """version.json's optional "mac": {"arm64"|"x86_64": {"url", "sha256"}}, each kept only when its
    url is an allowed release asset (`mac_update.allowed_asset`) and its sha256 is 64 hex digits."""
    if not isinstance(value, dict):
        return {}
    import mac_update
    assets = {}
    for arch in mac_update.ARCHES:
        item = value.get(arch)
        if not isinstance(item, dict) or not isinstance(item.get("sha256"), str):
            continue
        digest = item["sha256"].strip().lower()
        if mac_update.allowed_asset(item.get("url")) and _SHA256.fullmatch(digest):
            assets[arch] = {"url": item["url"].strip(), "sha256": digest}
    return assets


def _read(home):
    try:
        value = json.loads((_home(home) / STATE_FILE).read_text())
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _write(home, value):
    path = _home(home) / STATE_FILE
    temporary = path.with_name("%s.%d.tmp" % (path.name, os.getpid()))
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


_lock = threading.Lock()


def check(home=None, get=None, now=None):
    """One check. A good answer replaces the stored one; a failed fetch or parse (offline, a
    timeout, an HTTP error, bad JSON, a refused download_url) keeps the last good answer and shows
    nothing new. Returns the good answer, or None after a failure. Never raises."""
    try:
        try:
            available = parse((get or _get)(VERSION_URL))
        except Exception:
            available = None
        moment = (now or dt.datetime.now(dt.timezone.utc)).isoformat()
        with _lock:
            state = _read(home)
            state["checked_at"] = moment
            if available is not None:
                state["available"] = available
            _write(home, state)
        return available
    except Exception:
        return None


def dismiss(version, home=None):
    """Hide the banner for this version (and any older one) until a newer one appears."""
    if parse_version(version) is None:
        return False
    try:
        with _lock:
            state = _read(home)
            dismissed = parse_version(state.get("dismissed"))
            if dismissed is None or parse_version(version) > dismissed:
                state["dismissed"] = version.strip()
                _write(home, state)
        return True
    except Exception:
        return False


def dismiss_notice(home=None):
    """Hide a failed Mac update's notice only; the offer of that version stays until its own Dismiss."""
    try:
        import mac_update
        mac_update.clear_notice(_home(home))
        return True
    except Exception:
        return False


def banner(home=None):
    """The update banner for the layout, or None. Never raises: a broken file never breaks a page.
    A failed Mac update (`mac_update.read_notice`) leads the banner's text until it is dismissed."""
    try:
        import mac_update
        notice = mac_update.read_notice(_home(home))
        named = re.match(r"Sidedoor (\S+) ", notice or "")
        installed = parse_version(installed_version())
        if named and installed is not None and parse_version(named.group(1)) is not None \
                and parse_version(named.group(1)) <= installed:
            mac_update.clear_notice(_home(home))  # that version is installed now (by hand, say)
            notice = None
    except Exception:
        notice = None
    found = _banner(home)
    if notice:
        if found is None:
            return {"tone": "warning", "text": notice, "dismiss_notice": True}
        return dict(found, tone="warning", text=notice + " " + found["text"], dismiss_notice=True)
    return found


def _banner(home):
    try:
        state = _read(home)
        available = state.get("available")
        if not isinstance(available, dict):
            return None
        latest = parse_version(available.get("latest"))
        installed = parse_version(installed_version())
        if latest is None or installed is None or latest <= installed:
            return None
        if not _allowed_download(available.get("download_url")):
            return None
        dismissed = parse_version(state.get("dismissed"))
        if dismissed is not None and dismissed >= latest:
            return None
        notes = available.get("notes") if isinstance(available.get("notes"), str) else ""
        found = {"tone": "info", "text": "Sidedoor %s is available." % available["latest"],
                 "action_label": "Download", "action_href": available["download_url"], "external": True,
                 "notes": notes[:MAX_NOTES] or None, "dismiss": available["latest"]}
        return _with_update_now(found, available)
    except Exception:
        return None


def _with_update_now(found, available):
    """B (owner, 2026-10-23): [Update now] beside [Download] on the built Mac app in Applications when
    version.json names a zip for this Mac; while it runs, or after it failed, the banner says so.
    Anywhere else, or without the zip, the banner is unchanged."""
    try:
        import mac_update
        if not mac_update.can_install() or mac_update.asset_for(available) is None:
            return found
        state = mac_update.status()
        found = dict(found, download_href=found["action_href"])
        if state.get("version") == available["latest"] and state.get("state") in ("working", "replacing"):
            # poll: the banner re-reads itself (GET /update/banner, every 3 s) until the state ends.
            return dict(found, text=state["message"], dismiss=None, poll=True)
        if state.get("version") == available["latest"] and state.get("state") == "failed":
            found = dict(found, tone="warning", text=state["message"])
        return dict(found, forms=[{"action": "/update/install", "fields": {"version": available["latest"]},
                                   "label": "Update now"}])
    except Exception:
        return None


class Checker:
    """Checks once at once, then every 30 minutes, on `time.monotonic()`, in a daemon thread."""

    def __init__(self, home=None, get=None, interval=INTERVAL_SECONDS):
        self.home, self.get, self.interval = home, get, interval
        self._stop = threading.Event()
        self._thread = None

    def run(self):
        while True:
            check(self.home, self.get)
            if self._stop.wait(self.interval):
                break

    def start(self):
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self.run, name="sidedoor-update-check", daemon=True)
            self._thread.start()
        return self

    def stop(self, wait_seconds=2):
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(wait_seconds)

    def alive(self):
        return self._thread is not None and self._thread.is_alive()


_current = None


def start(home=None, get=None):
    """Start (once per process) the update check (now, then every 30 minutes); returns it."""
    global _current
    if _current is None or not _current.alive():
        _current = Checker(home, get).start()
    return _current


def stop():
    global _current
    if _current is not None:
        _current.stop()
    _current = None
