"""`sidedoor doctor` (E4.T4): one short report of every dependency, for the user and for a support issue.

Each line is `OK / WARN / FAIL — name — one plain-words line — what to do`. Every check calls the
function the dashboard or Setup already uses for that status, so doctor and the dashboard never
disagree:

    Python and Sidedoor        sys.version_info, pyproject `requires-python`, importlib.metadata
    Home folder                setup_steps._write_private (the writer every saved file goes through);
                               paths.status (data an older version left in the code folder, E9.T1)
    Setup file                 setup_steps.state_unreadable (the dashboard's "couldn't read" banner)
    Profile                    user_profile.load + setup_steps.validate_profile(profile_form)
    Résumé                     reply_resume.context (the raw résumé's text) and the application résumé path
    Credentials                credentials.status / where (E2.T4: the OS store, or the 0600 file)
    OpenRouter                 setup_steps.saved_openrouter_key + openrouter_tested (online: text_model.complete,
                               the Save-and-test call, as a pure read)
    Codex                      setup_steps.codex_candidates / _usable + codex_login_status
    Google Chrome              setup_steps.chrome_path
    Browser runtime            browser_runtime.display_state (missing, profile_in_use, and whether a Sidedoor
                               session owns that Chrome: the browser lock or a running guard)
    License                    license_clock.view (online: license_clock.check first)
    LinkedIn                   setup-state's saved linkedin step (online: the relay's /accounts read that
                               setup_steps.linkedin_status makes, without recording anything)
    Scheduler                  setup_steps.scheduler_running in the dashboard; from the CLI the open dashboard's
                               answer from the same scheduler_loop.running() (dashboard.scheduler_there);
                               both pauses from overview_page.pause_flags (scheduler_control.state)
    Recent failures            failures_page.gather (the Failures page)
    Database                   overview_page.snapshot + overview_page.Reader (the pages' read-only open)
    Tray icon                  tray-state.json, what the dashboard's tray did at its last start (tray.read_state)
    Disk space                 shutil.disk_usage on the Sidedoor home
    Computer                   platform
    Relay (online)             GET /healthz on the relay

Nothing here prints a secret: every line goes through failures_page.redact_text with
failures_page.secrets_of (the license key, the OpenRouter key, the dashboard token, Unipile's token
and DSN), and keys are only ever named masked (`SD-...TLUS`). Without `--online` no network call is made.
"""

import argparse
import datetime as dt
import json
import os
import platform
import shutil
import sys
import urllib.request
import uuid
from pathlib import Path

import paths
import setup_steps
import user_profile


ROOT = Path(__file__).resolve().parent
DATABASE = Path(paths.STATE) / paths.DATABASE
OK, WARN, FAIL = "OK", "WARN", "FAIL"
DASH = " — "
DISK_FAIL_BYTES = 1 << 30          # under 1 GB free: the database and logs can no longer grow
DISK_WARN_BYTES = 5 << 30
SUPPORT = "Email %s with this report." % setup_steps.SUPPORT_EMAIL


def masked(secret, prefix=""):
    """`SD-...` plus the last 4 characters, as Settings shows a key (settings_page.masked)."""
    return prefix + "..." + secret[-4:] if secret else None


def _line(status, name, text, todo=""):
    return {"status": status, "name": name, "line": text, "todo": todo}


def sidedoor_version(root=ROOT):
    """The shared version helper (`version.current()`, the one update_check reads too). Until that
    module lands (E9.T3), the same order update_check uses: pyproject.toml, then the installed metadata."""
    try:
        import version
    except ImportError:
        version = None
    if version is not None:
        return version.current()
    try:
        import tomllib
        with (Path(root) / "pyproject.toml").open("rb") as source:
            return tomllib.load(source)["project"]["version"]
    except Exception:
        pass
    try:
        import importlib.metadata
        return importlib.metadata.version("sidedoor")
    except Exception:
        return "unknown"


def check_python(root=ROOT):
    version = "%d.%d.%d" % sys.version_info[:3]
    text = "Python %s, Sidedoor %s." % (version, sidedoor_version(root))
    if sys.version_info[:2] != (3, 13):
        return _line(WARN, "Python and Sidedoor", text + " Sidedoor is built for Python 3.13.",
                     "Install Python 3.13, or use the Sidedoor app.")
    return _line(OK, "Python and Sidedoor", text)


def shown_path(path):
    """`~/...` for a path under the user's own folder, so the report carries no user name (UD8)."""
    path, mine = Path(path), Path.home()
    try:
        return "~/" + path.relative_to(mine).as_posix()
    except ValueError:
        return str(path)


def outside_user_folder(home, platform=None, user_home=None):
    """WR5: on Windows, True when the Sidedoor home is not inside the user's own folder (e.g. C:\\Sidedoor
    through SIDEDOOR_HOME). Its private files (keys, the dashboard token) are then only as private as
    that folder's permissions; the user folder admits only the user (VM pass, 2026-10-23)."""
    if (platform or sys.platform) != "win32":
        return False
    import ntpath
    home_text = ntpath.normcase(ntpath.abspath(str(home)))
    mine = ntpath.normcase(ntpath.abspath(str(user_home or Path.home())))
    try:
        return ntpath.commonpath([home_text, mine]) != mine
    except ValueError:   # another drive
        return True


def check_home(home, platform=None, user_home=None):
    probe = Path(home) / (".doctor-" + uuid.uuid4().hex)
    try:
        setup_steps._write_private(probe, "ok\n")
        probe.unlink()
    except OSError as error:
        return _line(FAIL, "Home folder", "Sidedoor can't write to its folder %s (%s)." % (shown_path(home), type(error).__name__),
                     "Check the folder's permissions, then run doctor again.")
    moved = paths.status(home)  # E9.T1: data an older version left in the code folder
    mine = str(Path.home())
    text = (paths.message(moved) or "").replace(mine, "~")  # no user name in the report (UD8)
    if moved["state"] == paths.CONFLICT:
        steps = list(dict.fromkeys(step for conflict in moved["conflicts"] for step in conflict["steps"]))
        return _line(FAIL if moved["held"] else WARN, "Home folder", text,
                     ("Open the dashboard and click Move my data now (nothing is deleted), or by hand: "
                      + " ".join(steps)).replace(mine, "~"))
    if moved["state"] in ("WAITING", paths.MOVING, "PENDING"):
        return _line(WARN, "Home folder", text, "Quit Sidedoor and open it again once no job is running: it "
                     "finishes the move before it starts any work.")
    if outside_user_folder(home, platform, user_home):
        return _line(WARN, "Home folder", "Sidedoor's folder %s is outside your user folder, so other people who use "
                     "this computer may be able to read your keys and other private files in it." % shown_path(home),
                     "Use a folder inside your user folder, or make sure only you can open %s." % shown_path(home))
    return _line(OK, "Home folder", "Sidedoor can write to %s." % shown_path(home))


def check_state(home):
    if setup_steps.state_unreadable(home):
        return _line(FAIL, "Setup file", "Sidedoor couldn't read its setup file (%s)." % setup_steps.STATE_FILE,
                     "Open the dashboard and click Reset setup file.")
    if not (Path(home) / setup_steps.STATE_FILE).exists():
        return _line(WARN, "Setup file", "Setup has not started yet.", "Run sidedoor setup.")
    return _line(OK, "Setup file", "Readable.")


def _profile(home):
    """user_profile.load for `home` (not the process's default home)."""
    with _home_variable(home):
        return user_profile.load()


class _home_variable:
    def __init__(self, home):
        self.home = str(home)

    def __enter__(self):
        self.saved = os.environ.get(user_profile.HOME_VARIABLE)
        self.same = Path(self.home) == user_profile.home()   # the usual case: nothing to change
        if not self.same:
            os.environ[user_profile.HOME_VARIABLE] = self.home

    def __exit__(self, *exc):
        if self.same:
            return
        if self.saved is None:
            os.environ.pop(user_profile.HOME_VARIABLE, None)
        else:
            os.environ[user_profile.HOME_VARIABLE] = self.saved


def check_profile(home):
    try:
        _profile(home)
    except user_profile.ProfileMissing:
        return _line(FAIL, "Profile", "There is no profile yet.", "Fill in Your profile in Setup.")
    except (OSError, ValueError) as error:
        return _line(FAIL, "Profile", "Your profile file can't be read (%s)." % type(error).__name__,
                     "Open Settings > Profile and save it again.")
    _clean, errors = setup_steps.validate_profile(setup_steps.profile_form(home), setup_steps.load_state(home))
    if errors:
        return _line(WARN, "Profile", "Some answers are missing or not valid: %s." % ", ".join(sorted(errors)),
                     "Open Settings > Profile, fix them and save.")
    return _line(OK, "Profile", "Complete.")


def check_resume(home, root=ROOT):
    import reply_resume
    try:
        with _home_variable(home):
            context = reply_resume.context(root)
    except ValueError:
        return _line(FAIL, "Résumé", "No readable résumé is saved.", "Upload your résumé in Settings > Résumé.")
    try:
        application = _profile(home).get("resume") or {}
        path = Path(root) / application["path"]
        readable = path.is_file() and os.access(path, os.R_OK)
    except (KeyError, TypeError, OSError, ValueError):
        readable = False
    if not readable:
        return _line(FAIL, "Résumé", "Your résumé is read, but the application version is missing.",
                     "Upload your résumé again in Settings > Résumé.")
    pages = context.get("page_count")
    return _line(OK, "Résumé", "Saved and readable%s." % (" (%s pages)" % pages if pages else ""))


WAITING_TODO = "Nothing to re-enter: Sidedoor carries on by itself once the keychain answers."


def _waiting(home, name):
    """KR6: while the keys wait for the keychain, the key's own line says so, never "no key"."""
    waiting = setup_steps.keys_waiting(home)
    return _line(WARN, name, "Waiting for the keychain. " + waiting, WAITING_TODO) if waiting else None


def check_openrouter(home, config, online=False, transport=None):
    key = setup_steps.saved_openrouter_key(home)
    if not key and _waiting(home, "OpenRouter"):
        return _waiting(home, "OpenRouter")
    if not key:
        return _line(FAIL, "OpenRouter", "No OpenRouter key is saved.", "Paste your key in Settings > Connections.")
    shown = masked(key)
    if online:
        # A pure read (UD2): the same test call Save and test makes (text_model.complete), but no
        # setup state or tested marker is touched, so a network blip never undoes a passed step.
        import text_model
        try:
            text_model.complete(config, "Reply with the single word OK.", max_tokens=256, timeout=30,
                                transport=transport, api_key=key)
        except text_model.TextModelHTTPError as error:
            if error.status == 401:
                return _line(FAIL, "OpenRouter", "OpenRouter did not accept key %s." % shown,
                             "Replace the key in Settings > Connections.")
            if error.status == 402:
                return _line(FAIL, "OpenRouter", "Your OpenRouter account has no credits left.", "Add credits.")
            return _line(WARN, "OpenRouter", "OpenRouter refused the test call (HTTP %s)." % error.status,
                         "Try again in a few minutes.")
        except (TimeoutError, OSError, text_model.TextModelResponseError):
            return _line(WARN, "OpenRouter", "Could not reach OpenRouter to test key %s." % shown,
                         "Check your internet and try again.")
        except ValueError:
            return _line(FAIL, "OpenRouter", "Sidedoor has no OpenRouter model configured.", SUPPORT)
        return _line(OK, "OpenRouter", "Key %s works (tested just now)." % shown)
    if setup_steps.openrouter_tested(home):
        return _line(OK, "OpenRouter", "Key %s saved; its last test passed." % shown)
    return _line(WARN, "OpenRouter", "Key %s is saved but its last test did not pass." % shown,
                 "Click Test saved key in Settings > Connections, or run doctor --online.")


CREDENTIAL_LABELS = {"openrouter": "OpenRouter key", "license": "license key", "openai": "OpenAI API key"}


def check_credentials(home):
    """E2.T4: where the two keys are kept (credentials.status / where). WARN when one is still a
    file (not moved yet, or no OS store: K4 keeps the 0600 file) or the store is locked."""
    import credentials
    store = credentials.where()
    states = {name: credentials.status(name, home) for name in credentials.NAMES}
    waiting = credentials.hold_reason(home)
    if waiting:   # KR4: locked or unavailable alike; never "paste again"
        if credentials.offer_file(home):   # KC1: broken, not locked: the folder is offered
            return _line(WARN, "Credentials", waiting, "Or save your key in Sidedoor's folder instead (Settings), "
                         "a private file only you can read.")
        return _line(WARN, "Credentials", waiting, WAITING_TODO)
    chosen = [CREDENTIAL_LABELS[name] for name, value in states.items() if value == "file_chosen"]
    if chosen:   # KC1: say plainly where the key is kept
        return _line(WARN, "Credentials", "Your %s %s kept in a private file (readable only by you) in %s, as you chose "
                     "while %s couldn't be used." % (" and ".join(chosen), "is" if len(chosen) == 1 else "are",
                                                     shown_path(home), store),
                     "Nothing to do; Sidedoor moves it into %s at a start once it works." % store)
    broken = [CREDENTIAL_LABELS[name] for name, value in states.items() if value == "file_unread"]
    files = [CREDENTIAL_LABELS[name] for name, value in states.items() if value in ("file", "file_unread")]
    verb = "is" if len(files) == 1 else "are"
    conflict = [CREDENTIAL_LABELS[name] for name, value in states.items() if value == "conflict"]
    if conflict:
        return _line(WARN, "Credentials", "An older file with your %s is left in %s; Sidedoor uses the key kept in %s." % (
            " and ".join(conflict), shown_path(home), store), "Nothing to do.")
    if files and (store == "file" or broken):
        why = "This computer has no keychain Sidedoor can use" if store == "file" else "Sidedoor could not use %s" % store
        return _line(WARN, "Credentials", "%s, so your %s %s kept in a private file (readable only by you) in %s." % (
            why, " and ".join(files), verb, shown_path(home)),
            "Nothing to do; Sidedoor tries %s again at every start." % store if store != "file" else "Nothing to do.")
    if files:
        return _line(WARN, "Credentials", "Your %s %s still in a file in %s, not yet in %s." % (
            " and ".join(files), verb, shown_path(home), store),
            "Restart Sidedoor; it moves the key at start. If this stays, " + SUPPORT)
    saved = [CREDENTIAL_LABELS[name] for name, value in states.items() if value == "store"]
    if not saved:
        return _line(OK, "Credentials", "No keys saved yet; they will be kept in %s." % store)
    return _line(OK, "Credentials", "Your %s %s kept in %s." % (" and ".join(saved), "is" if len(saved) == 1 else "are",
                                                               store))


def check_codex(home, run=None, config=None, online=False, transport=None):
    import subprocess
    state = setup_steps.load_state(home)
    path = setup_steps.find_codex(state.get("codex_path"))
    if not path:
        return _line(FAIL, "Codex", "Codex is not installed.", "Install Codex (see Settings > Connections).")
    setup_steps.extend_path(path)   # an npm Codex needs node's folder
    import openai_auth
    if openai_auth.api_key_mode(home):
        return check_openai_key(home, config, online, transport)
    status = setup_steps.codex_login_status(path, run or subprocess.run)
    if status == "SIGNED_IN":
        return _line(OK, "Codex", "Installed and signed in with ChatGPT.")
    if status == "API_KEY":
        return _line(FAIL, "Codex", "Codex is signed in with an API key.", "Sign in with ChatGPT instead (codex login).")
    if status == "UNAVAILABLE":
        return _line(FAIL, "Codex", "Codex is installed but would not run.", "Reinstall Codex.")
    return _line(FAIL, "Codex", "Codex is installed but not signed in.", "Run codex login and sign in with ChatGPT.")


def check_openai_key(home, config, online=False, transport=None):
    """E0.T8: Codex with the user's OpenAI API key: the key, its test, the hold and the last billing
    wait (times in this computer's local time)."""
    import credentials
    import openai_auth
    try:
        key = openai_auth.saved_key(home)
        openai_auth.test_state(home)
    except OSError:   # E2.T4 (KR4): the keychain waits; never "none is saved"
        return _line(WARN, "Codex", credentials.hold_reason(home) or "Sidedoor is waiting for your keychain to read "
                     "your OpenAI API key.", WAITING_TODO)
    if not key:
        return _line(FAIL, "Codex", "Codex is set to use an OpenAI API key, but none is saved.",
                     "Save your key in Settings > Connections, or Sign in with ChatGPT.")
    shown = masked(key)
    if online:
        ok, kind, words = openai_auth.test_key(key, config or {}, transport)  # a pure read: nothing is saved
        if not ok:
            status = WARN if kind in ("network", "http", openai_auth.RATE_LIMIT) else FAIL
            return _line(status, "Codex", "Installed; OpenAI API key %s: %s" % (shown, words),
                         openai_auth.TODO.get(kind, "Check the key in Settings > Connections."))
        lines = [_line(OK, "Codex", "Installed; OpenAI API key %s works (tested just now, using a tiny amount of "
                       "credit)." % shown)]
    else:
        state = openai_auth.test_state(home)
        if state == "passed":
            lines = [_line(OK, "Codex", "Installed; uses OpenAI API key %s; its last test passed." % shown)]
        elif state == "failed":
            lines = [_line(WARN, "Codex", "Installed; OpenAI API key %s is saved but its last test did not pass." % shown,
                           ONLINE_TEST)]
        else:
            lines = [_line(WARN, "Codex", "Installed; OpenAI API key %s is saved but not tested yet." % shown, ONLINE_TEST)]
    used = openai_auth.fallback(home)
    if used:   # OA2 a: the key failed; Codex runs on the ChatGPT sign-in, nothing is held
        lines.append(_line(WARN, "OpenAI API", "%s (since %s; no attempts spent.)" % (
            openai_auth.fallback_words(used["kind"]), openai_auth.local_time(used.get("since"))),
            "Replace the key in Settings > Connections; Sidedoor uses it again once it passes its test."))
        return lines
    held = openai_auth.hold(home)
    if held:
        lines.append(_line(FAIL, "OpenAI API", "%s (since %s; no attempts spent.)" % (
            openai_auth.HELD_WORDS[held["kind"]], openai_auth.local_time(held.get("since"))),
            openai_auth.TODO["held_" + held["kind"]] if "held_" + held["kind"] in openai_auth.TODO
            else openai_auth.TODO[held["kind"]]))
        return lines
    wait = openai_auth.last_wait(home)
    if wait:
        lines.append(_line(WARN if wait["kind"] == openai_auth.RATE_LIMIT else FAIL, "OpenAI API",
                           "%s Work is waiting (no attempts spent); next try after %s." % (
                               openai_auth.WORDS[wait["kind"]], openai_auth.local_time(wait.get("resume_at"))
                               if wait.get("resume_at") else "a short wait"),
                           openai_auth.TODO[wait["kind"]]))
    return lines


ONLINE_TEST = ("Click Test saved key in Settings > Connections, or run doctor --online (uses a tiny amount of "
               "credit).")


def check_chrome():
    if not setup_steps.chrome_path():
        return _line(FAIL, "Google Chrome", "Google Chrome is not installed.", "Install Google Chrome.")
    return _line(OK, "Google Chrome", "Installed.")


def check_browser_runtime(config, run=None, root=None):
    """FG1 problem 2: "close the window" only when no Sidedoor session holds the browser lock and no running
    guard started the Chrome on the profile (browser_runtime.display_state); FG1 = b: the off-screen note."""
    import browser_runtime
    state = browser_runtime.display_state(config, root, run)
    if state == "RUNTIME_MISSING":
        return _line(FAIL, "Browser runtime", browser_runtime.UNAVAILABLE_WORDS["RUNTIME_MISSING"],
                     browser_runtime.UNAVAILABLE_TODO["RUNTIME_MISSING"])
    if state == "PROFILE_IN_USE":
        return _line(WARN, "Browser runtime", browser_runtime.UNAVAILABLE_WORDS["PROFILE_IN_USE"],
                     browser_runtime.UNAVAILABLE_TODO["PROFILE_IN_USE"])
    note = (" " + browser_runtime.OFFSCREEN_WORDS) if browser_runtime.offscreen(config) else ""
    if state == "SESSION_BUSY":
        return _line(OK, "Browser runtime", "Present. " + browser_runtime.BUSY_WORDS + note)
    return _line(OK, "Browser runtime", "Present, and no other Chrome is using Sidedoor's profile." + note)


def check_license(home, online=False, relay_url=None, post=None):
    import license_clock
    key = setup_steps.license_key(home)
    if not key and _waiting(home, "License"):
        return _waiting(home, "License")
    shown = masked(key[3:], "SD-") if key and key.startswith("SD-") else masked(key)
    prefix = ""
    if online and key:
        result = license_clock.check(home, relay_url, post)
        prefix = "Checked just now (%s). " % result
    value = license_clock.view(home)
    if value["state"] == "no_license":
        return _line(FAIL, "License", "No license key is saved.", "Enter your key in Settings > License.")
    text = prefix + "Key %s: %s" % (shown, value["text"])
    action = value.get("action") or {}
    todo = (action.get("label") + " in Settings > License.") if action.get("label") else ""
    if value["usable"] and value["tone"] not in ("error", "warning"):
        return _line(OK, "License", text)
    return _line(FAIL if not value["usable"] else WARN, "License", text, todo or "Open Settings > License.")


def check_linkedin(home, online=False, relay_url=None, transport=None):
    if not setup_steps.license_key(home) and _waiting(home, "LinkedIn"):
        return _waiting(home, "LinkedIn")
    if online:
        live = _linkedin_read(home, relay_url, transport)
        if live is not None:
            return live
    saved = setup_steps.load_state(home)["steps"].get("linkedin", {})
    status, message = saved.get("status"), saved.get("message")
    if status == "done":
        return _line(OK, "LinkedIn", message or "Connected.")
    if status is None:
        return _line(WARN, "LinkedIn", "Not checked yet.", "Connect LinkedIn in Settings > Connections.")
    if status == "checking":
        return _line(WARN, "LinkedIn", message or "Waiting for LinkedIn.", "Finish signing in on the LinkedIn page.")
    return _line(FAIL, "LinkedIn", message or "Needs you.", "Open Settings > Connections.")


def _linkedin_read(home, relay_url=None, transport=None):
    """The relay's `/accounts` read as Setup step 7 does it (setup_steps.linkedin_status), but as a
    pure read (UD2): nothing is recorded and no license check is requested. None when the relay
    can't be reached (the saved step is then reported)."""
    import unipile_port
    key = setup_steps.license_key(home)
    if not key:
        return _line(FAIL, "LinkedIn", "Needs your license first.", "Enter your key in Settings > License.")
    try:
        status, parsed = (transport or unipile_port.RelayTransport(relay_url or setup_steps.RELAY_URL, key))(
            "GET", "/accounts")
    except (OSError, TimeoutError, ValueError):
        return None
    error = setup_steps._relay_error(parsed)
    items = parsed.get("items") if status == 200 and isinstance(parsed, dict) else None
    if status == 401 or error == "LICENSE_INVALID":
        return _line(FAIL, "LinkedIn", "Your subscription isn't active.", "Check Settings > License.")
    if items:
        item = items[0] if isinstance(items[0], dict) else {}
        name = item.get("name") if isinstance(item.get("name"), str) else "your LinkedIn account"
        statuses = unipile_port._account_statuses(item)
        if not statuses or all(value in unipile_port.VALID_STATUSES for value in statuses):
            return _line(OK, "LinkedIn", "LinkedIn connected: %s (checked just now)." % name)
        return _line(FAIL, "LinkedIn", "LinkedIn needs you to sign in again.", "Click Reconnect in Settings > Connections.")
    if isinstance(parsed, dict) and parsed.get("type") == "errors/no_client_session":
        return _line(FAIL, "LinkedIn", "LinkedIn needs you to sign in again.", "Click Reconnect in Settings > Connections.")
    if (status == 403 and error == "LICENSE_NOT_LINKED") or (status == 200 and items == []):
        return _line(FAIL, "LinkedIn", "LinkedIn is not connected.", "Click Connect in Settings > Connections.")
    return _line(WARN, "LinkedIn", "Sidedoor's LinkedIn service answered HTTP %s." % status, "Try again in a few minutes.")


def check_scheduler(running, pause):
    flags = pause if isinstance(pause, dict) else {}
    notes = []
    if flags.get("license_paused_at"):
        notes.append("paused because of your license")
    if flags.get("user_paused_at"):
        notes.append("paused by you since %s" % flags["user_paused_at"])
    paused = ("; " + ", ".join(notes)) if notes else ""
    if pause is None:
        paused = "; whether it is paused can't be read"
    if running is None:
        return _line(WARN, "Scheduler", "Can't tell whether Sidedoor is running%s." % paused,
                     "Open the dashboard to see it.")
    if not running:
        return _line(WARN, "Scheduler", "Sidedoor isn't running%s." % paused, "Open it and click Start.")
    if flags.get("license_paused_at"):
        return _line(FAIL, "Scheduler", "Running" + paused + ".", "Open Settings > License.")
    if flags.get("user_paused_at"):
        return _line(WARN, "Scheduler", "Running" + paused + ".", "Click Resume on Overview when you are ready.")
    return _line(OK, "Scheduler", "Running, not paused.")


def _records(root, config, loop, now=None):
    """(database line, failures line, pause flags or None) from one read-only snapshot, as the pages read."""
    import failures_page
    import overview_page as overview
    path = paths.database(root)

    def use(database):
        reader = overview.Reader(database)
        if database is None:
            return "missing", None, None
        if reader.broken:
            return "broken", None, None
        pause = overview.pause_flags(reader)
        view = failures_page.gather(database, config, loop, now=now)
        count = len(view["needs_you"]) + sum(len(section["entries"]) + section.get("more", 0)
                                             for section in view["sections"])
        return "ok", (count, view["recent_days"], len(view["needs_you"])), pause

    try:
        state, failures, pause = overview.snapshot(path, use, lambda: overview._connect(path.resolve().as_uri() + "?mode=ro"))
    except Exception as error:
        return (_line(FAIL, "Database", "Sidedoor couldn't read its records (%s)." % type(error).__name__,
                      "Close other Sidedoor windows and run doctor again. " + SUPPORT),
                _line(WARN, "Recent failures", "Not available: the records can't be read."), None)
    if state == "missing":
        return (_line(WARN, "Database", "No records yet (Sidedoor has not run).", "Finish Setup and click Start."),
                _line(OK, "Recent failures", "None (nothing has run yet)."), {})
    if state == "broken":
        return (_line(FAIL, "Database", "Sidedoor's records file is not readable.", SUPPORT),
                _line(WARN, "Recent failures", "Not available: the records can't be read."), None)
    count, days, needs = failures
    database = _line(OK, "Database", "Readable.")
    if not count:
        return database, _line(OK, "Recent failures", "None in the last %d days." % days), pause
    extra = " (%d need you)" % needs if needs else ""
    return database, _line(WARN, "Recent failures", "%d in the last %d days%s." % (count, days, extra),
                           "See Failures and logs in the dashboard."), pause


def check_model_cost(root, config, now=None):
    """Today's model calls (C4): "Model cost today: $X across N calls (M without cost data)"."""
    import model_calls
    import overview_page as overview
    path = paths.database(root)
    if not path.is_file():
        return _line(OK, "Model cost", "No model calls recorded yet.")
    database = overview._connect(path.resolve().as_uri() + "?mode=ro")
    try:
        totals = model_calls.today(database, now, config.get("timezone") or "America/Los_Angeles")
    finally:
        database.close()
    return _line(OK, "Model cost", model_calls.summary_line(totals) + ".")


def check_disk(home, usage=shutil.disk_usage):
    try:
        free = usage(home).free
    except OSError as error:
        return _line(WARN, "Disk space", "Can't read free space (%s)." % type(error).__name__)
    text = "%.1f GB free." % (free / (1 << 30))
    if free < DISK_FAIL_BYTES:
        return _line(FAIL, "Disk space", text, "Free up some disk space.")
    if free < DISK_WARN_BYTES:
        return _line(WARN, "Disk space", text, "Free up some disk space soon.")
    return _line(OK, "Disk space", text)


def check_tray(home, platform=None, load=None):
    """E5.T3: the tray icon. A tray that could not start is a WARN: the dashboard runs without it."""
    import tray
    platform = platform or sys.platform
    if platform.startswith("linux"):
        return _line(OK, "Tray icon", "Not used on Linux yet.")
    state = tray.read_state(home)
    if state and state.get("state") == "unavailable":
        return _line(WARN, "Tray icon", "Not shown at the last start: %s" % (state.get("reason") or "unknown reason."),
                     "Sidedoor still runs; open it from your browser. Start Sidedoor from your desktop to get the icon.")
    try:
        (load or tray.load)()
    except Exception as error:
        return _line(WARN, "Tray icon", "The tray library is missing (%s)." % type(error).__name__,
                     "Reinstall Sidedoor.")
    if state and state.get("state") == "shown":
        return _line(OK, "Tray icon", "Shown at the last start.")
    return _line(OK, "Tray icon", "Not shown yet (Sidedoor has not started with it).")


def check_pdf_writer():
    """The PDF writer (fpdf2) the ATS résumé and cover letters need: a one-line PDF written to a
    temporary folder and removed. In the built app fpdf2 is loaded from its own folder beside the
    binary (E9.T1 AP1), so this is also the build's check that it loads from there."""
    import cover_letter
    import tempfile
    try:
        with tempfile.TemporaryDirectory(prefix="sidedoor-pdf-") as folder:
            cover_letter.render("Sidedoor check.", Path(folder) / "check.pdf")
    except Exception as error:
        return _line(FAIL, "PDF writer", "Sidedoor can't write PDFs (%s)." % type(error).__name__, "Reinstall Sidedoor.")
    return _line(OK, "PDF writer", "Can write PDFs.")


def check_app_location():
    """E9.T1 AP4: the built Mac app must run from Applications for start at login."""
    import login_item
    problem = login_item.app_location_problem()
    if problem:
        return _line(WARN, "App location", "Sidedoor runs from %s, so it can't start at login."
                     % login_item.scheduler_loop.program(), problem)
    if login_item.scheduler_loop.frozen():
        # The built app names the program every child and the login entry run (WB2: the build's smoke
        # checks it is the built sidedoor.exe / Sidedoor binary itself).
        return _line(OK, "App location", "Fine for start at login (Sidedoor runs from %s)."
                     % login_item.scheduler_loop.program())
    return _line(OK, "App location", "Fine for start at login.")


def check_platform():
    return _line(OK, "Computer", "%s %s (%s)." % (platform.system() or sys.platform, platform.release(), platform.machine()))


def relay_get(url, timeout=10):
    with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=timeout) as response:
        return response.status


def check_relay(relay_url=None, get=None):
    url = (relay_url or setup_steps.RELAY_URL).rstrip("/") + "/healthz"
    try:
        status = (get or relay_get)(url)
    except Exception as error:
        return _line(FAIL, "Relay", "Can't reach Sidedoor's service (%s)." % type(error).__name__,
                     "Check your internet, then run doctor --online again.")
    if status != 200:
        return _line(FAIL, "Relay", "Sidedoor's service answered HTTP %s." % status, "Try again in a few minutes.")
    return _line(OK, "Relay", "Sidedoor's service answers.")


def cli_running(home):
    """From the terminal: whether this install's open dashboard says its scheduler loop runs
    (`dashboard.scheduler_there`, answered from `scheduler_loop.running()`, what the dashboard
    itself shows). False when no dashboard answers or Start was never clicked; None when that can't
    be asked. Never creates the token."""
    try:
        token = (Path(home) / "dashboard-token").read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if not token:
        return False
    try:
        import dashboard
    except Exception:
        return None
    return dashboard.scheduler_there(token) == "running"


_ASK = object()


def dashboard_checks(home, root, config, running, loop, openrouter_transport=None):
    """Run checks on the dashboard: local, except that while the OpenRouter out-of-credit banner shows
    (owner, 2026-10-23, pick 3b) the OpenRouter check makes its one small test call, as doctor
    --online does; a call that works clears the banner (text_model.complete)."""
    import provider_limits
    return run(home=home, root=root, config=config, running=running, loop=loop, openrouter_transport=openrouter_transport,
               openrouter_online=provider_limits.out_of_credit_shown(home))


def run(home=None, root=None, config=None, online=False, running=_ASK, loop=None, relay_url=None,
        codex_run=None, browser_run=None, openrouter_transport=None, openai_transport=None, license_post=None,
        linkedin_transport=None, relay=None, disk_usage=shutil.disk_usage, now=None, openrouter_online=False):
    """The checks, in order, as [{"status", "name", "line", "todo"}]. `running` is whether the
    scheduler runs (default: ask `cli_running`; None: unknown); the rest are injectable for tests.
    `openrouter_online` makes the OpenRouter check's one test call even when `online` is off (the
    dashboard's Run checks while the out-of-credit banner shows, `dashboard_checks`)."""
    home = Path(home) if home is not None else user_profile.home()
    root = paths.data(root, home)  # the data folder (E9.T1): the database lives in the home
    if config is None:
        import scheduler_loop
        config = scheduler_loop.load_config(root)
    if running is _ASK:
        running = cli_running(home)
    if not isinstance(loop, dict):
        import overview_page
        loop = overview_page.loop_state(bool(running))
    lines = []

    def guarded(name, check, *arguments, **keywords):
        try:
            result = check(*arguments, **keywords)
        except Exception as error:
            result = _line(FAIL, name, "The check itself failed (%s)." % type(error).__name__, SUPPORT)
        lines.extend(result if isinstance(result, list) else [result])

    guarded("Python and Sidedoor", check_python)  # the code folder's own version
    guarded("Home folder", check_home, home)
    guarded("Setup file", check_state, home)
    guarded("Profile", check_profile, home)
    guarded("Résumé", check_resume, home, root)
    guarded("Credentials", check_credentials, home)
    guarded("OpenRouter", check_openrouter, home, config, online or openrouter_online, openrouter_transport)
    guarded("Codex", check_codex, home, codex_run, config=config, online=online, transport=openai_transport)
    guarded("Google Chrome", check_chrome)
    guarded("Browser runtime", check_browser_runtime, config, browser_run, root)
    if online:
        guarded("Relay", check_relay, relay_url, relay)
    guarded("License", check_license, home, online, relay_url, license_post)
    guarded("LinkedIn", check_linkedin, home, online, relay_url, linkedin_transport)
    records = []
    guarded("Database", lambda: records.extend(_records(root, config, loop, now)) or list(records[:2]))
    pause = records[2] if len(records) == 3 else None
    guarded("Model cost", check_model_cost, root, config, now)
    guarded("Scheduler", check_scheduler, running, pause)
    guarded("Tray icon", check_tray, home)
    guarded("PDF writer", check_pdf_writer)
    guarded("App location", check_app_location)
    guarded("Disk space", check_disk, home, disk_usage)
    guarded("Computer", check_platform)
    return redacted(lines, home, config)


def redacted(lines, home, config):
    """Every text field through the Failures page's redaction (failures_page.secrets_of / redact_text)."""
    import failures_page
    secrets = failures_page.secrets_of(home, config)
    if not secrets:
        return lines
    return [{key: failures_page.redact_text(value, secrets) if key in ("line", "todo") and value else value
             for key, value in line.items()} for line in lines]


def report_text(lines, now=None):
    moment = (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")
    counts = {status: sum(1 for line in lines if line["status"] == status) for status in (OK, WARN, FAIL)}
    out = ["Sidedoor doctor, %s: %d OK, %d WARN, %d FAIL" % (moment, counts[OK], counts[WARN], counts[FAIL])]
    for line in lines:
        parts = [line["status"].ljust(4), line["name"], line["line"]]
        if line["todo"]:
            parts.append(line["todo"])
        out.append(DASH.join(parts))
    return "\n".join(out) + "\n"


def report_json(lines, now=None):
    moment = (now or dt.datetime.now(dt.timezone.utc)).isoformat(timespec="seconds")
    return json.dumps({"at": moment, "ok": not any(line["status"] == FAIL for line in lines), "checks": lines},
                      indent=2, ensure_ascii=False) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="sidedoor doctor", description="Check every part Sidedoor needs.")
    parser.add_argument("--online", action="store_true",
                        help="also run the network checks once: the relay, the license, the OpenRouter key, LinkedIn, "
                             "and an OpenAI API key when one is used (uses a tiny amount of credit)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    lines = run(online=args.online)
    # Machine contract (E2.T6): `doctor --json` / the user's report; stays print.
    sys.stdout.write(report_json(lines) if args.json else report_text(lines))
    return 1 if any(line["status"] == FAIL for line in lines) else 0


def register(app, templates, layout, home, root, config, token=None):
    """GET /failures/doctor: the same report in the dashboard (Failures and logs > Run checks), with
    [Copy report]. Local checks only, and the scheduler state from this process's loop."""
    from fastapi import Request
    from starlette.concurrency import run_in_threadpool
    import overview_page

    @app.get("/failures/doctor", name="doctor")
    async def doctor_page(request: Request):
        def build():
            running = setup_steps.scheduler_running(home)
            return dashboard_checks(home, root, config, running, overview_page.loop_state(running))
        lines = await run_in_threadpool(build)
        context = layout("failures", "Checks")
        context.update(checks=lines, report=report_text(lines))
        return templates.TemplateResponse(request, "doctor.html", context)
