"""Mac updates in place (owner, 2026-10-23, picks A and B). macOS, the built app only; Windows and
source runs are unchanged.

"when a user downloads a new version and double clicks it to extract it, can we automatically quit
the currently open sidedoor, copy it into applications for them and get them to click 'replace' by
hand, then reopen it back for them."

- B, [Update now] on the update banner (`update_check.banner`): one click downloads the zip for this
  Mac (Apple silicon or Intel, `machine_arch`, from version.json's optional "mac" assets), checks it
  (`verify`), asks the user to click Replace (`confirm`, a native dialog), then quits this Sidedoor
  the normal way and swaps the app in place (`swap`). Runs in a background thread (`start_install`);
  the banner shows how it is going and, after any failure, why, with [Download] beside it.
- A, a downloaded copy opened from outside Applications (`offer_move`, before the dashboard binds
  its port, so it never starts a job): "Replace Sidedoor <installed> with <this> in Applications?"
  [Not now] [Replace], or "Move Sidedoor to Applications?" when there is none. Replace quits the
  running Sidedoor (SIGTERM, its normal stop), and this copy moves itself into that folder. When the
  installed copy is the same or newer, the dialog says so and offers to open that one instead. Not
  now (or any failure) leaves today's "Move Sidedoor to your Applications folder" banner.

Checks before anything is replaced (`verify`): the download's sha256 (B); `ditto -x -k` into a fresh
temporary folder holding exactly one Sidedoor.app (B); `codesign --verify --deep --strict`;
`spctl --assess --type execute` (notarized); the same Team ID as the copy it replaces; the same
bundle identifier; and its version equals the expected one and is newer than the installed one (B).
Any failure: a plain message, nothing touched, [Download] (B) or today's banner (A).

The swap never runs from inside a bundle being replaced: `HELPER` is a /bin/sh script written to a
fresh temporary folder and started detached (`swap`). It waits for the Sidedoor processes to end
(the dashboard up to `WAIT_SECONDS`, then every process running from either bundle, i.e. job
children, which a normal Quit leaves to finish, up to `JOB_WAIT_SECONDS`), moves the old app to the
Trash as `~/.Trash/Sidedoor <version>.app` (never deleted; if the Trash refuses, to
`<Sidedoor home>/previous-versions/`), moves the new app into the same folder, clears the download
quarantine flag on it, and opens it. On any failure it puts the old app back, reopens it and leaves
a notice (`NOTICE_FILE`) that pages show until dismissed (`read_notice`). Every step goes to
logs/updater.log.

Every OS command goes through `runner` (argv -> (exit code, output)); tests inject fakes and never
run codesign, spctl, ditto, open or the swap against a real app.
"""

import hashlib
import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import log

LOG = log.get(__name__)

APP_NAME = "Sidedoor.app"
APPLICATIONS = Path("/Applications")
NOTICE_FILE = "update-notice"
LOG_NAME = "updater.log"
PREVIOUS_VERSIONS = "previous-versions"
ARCHES = ("arm64", "x86_64")
# GitHub sends a release download (github.com/<owner>/<repo>/releases/download/...) on to its asset
# host with a 302 (observed 2026-10-23: release-assets.githubusercontent.com; objects. before it).
ASSET_HOSTS = ("release-assets.githubusercontent.com", "objects.githubusercontent.com")
GITHUB_DOWNLOADS = "/yoursidedoor/sidedoor/releases/download/"
MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024
DOWNLOAD_DEADLINE_SECONDS = 30 * 60
READ_TIMEOUT_SECONDS = 60
CALL_TIMEOUT_SECONDS = 300   # codesign --deep and spctl on a large bundle can take a while
DIALOG_SECONDS = 600         # a dialog left alone this long counts as Not now
WAIT_SECONDS = 120           # for the dashboard to quit
JOB_WAIT_SECONDS = 60 * 60   # then for a job a normal Quit lets finish
QUIT_DELAY_SECONDS = 1.0

# Injection points (tests replace them; None means the real machine).
RUNNER = None
PLATFORM = None
FROZEN = None
PROGRAM = None
USER_HOME = None
SPAWN = None      # (argv, env) -> None: starts the helper detached
QUIT_SELF = None  # () -> None: this Sidedoor's normal stop
KILL = None       # (pid, signal) -> None

UPDATE_FAILED = "Sidedoor %s didn't install. %s"  # the banner offers "or download it" beside it
DOWNLOAD_HINT = " Use Download to install it by hand."


def _run(argv):
    try:
        done = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=CALL_TIMEOUT_SECONDS,
                              stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return 127, "%s: not found" % argv[0]
    except subprocess.TimeoutExpired:
        return 124, "%s: timed out" % argv[0]
    except OSError as error:
        return 126, "%s: %s" % (argv[0], error.strerror or type(error).__name__)
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def _runner(runner=None):
    return runner or RUNNER or _run


def _user_home():
    return Path(USER_HOME or Path.home())


def is_mac_app(platform=None, frozen=None):
    """True only for the built macOS app: every other system and a source run are unchanged."""
    import scheduler_loop
    platform = platform or PLATFORM or sys.platform
    frozen = (FROZEN if FROZEN is not None else scheduler_loop.frozen()) if frozen is None else frozen
    return platform == "darwin" and bool(frozen)


def _program():
    import scheduler_loop
    return str(PROGRAM or scheduler_loop.program())


def bundle_of(program):
    """The .app folder `program` runs from (…/Sidedoor.app/Contents/MacOS/sidedoor), or None."""
    path = Path(program)
    for parent in path.parents:
        if parent.suffix == ".app" and (parent / "Contents" / "Info.plist").is_file():
            return parent
    return None


def info(app):
    try:
        value = plistlib.loads((Path(app) / "Contents" / "Info.plist").read_bytes())
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def bundle_version(app):
    value = info(app).get("CFBundleShortVersionString")
    return value.strip() if isinstance(value, str) else None


def team_id(app, runner=None):
    """The Team ID `codesign -dv` reports for `app`, or None (unsigned, ad hoc, unreadable)."""
    code, output = _runner(runner)(["/usr/bin/codesign", "-dv", str(app)])
    if code != 0:
        return None
    match = re.search(r"^TeamIdentifier=([A-Z0-9]{10})\s*$", output, re.MULTILINE)
    return match.group(1) if match else None


_arch = []


def machine_arch(runner=None):
    """"arm64" on Apple silicon (also under Rosetta, by `sysctl hw.optional.arm64`), else "x86_64".
    Asked once per process (the banner reads it on every page)."""
    if runner is None and RUNNER is None and _arch:
        return _arch[0]
    code, output = _runner(runner)(["/usr/sbin/sysctl", "-n", "hw.optional.arm64"])
    found = "arm64" if code == 0 and output.strip() == "1" else "x86_64"
    if runner is None and RUNNER is None:
        _arch[:] = [found]
    return found


def _newer(a, b):
    import update_check
    a, b = update_check.parse_version(a), update_check.parse_version(b)
    return a is not None and b is not None and a > b


def verify(app, identifier, team, expected_version=None, installed_version=None, runner=None):
    """None when `app` may replace the installed copy, else a plain sentence why not."""
    run = _runner(runner)
    app = Path(app)
    if app.is_symlink() or not (app / "Contents" / "Info.plist").is_file():
        return "the download isn't a Sidedoor app."
    if run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)])[0] != 0:
        return "its signature didn't check out."
    if run(["/usr/sbin/spctl", "--assess", "--type", "execute", str(app)])[0] != 0:
        return "macOS didn't accept it as a notarized app."
    if not team or team_id(app, run) != team:
        return "it isn't signed by the same developer as your Sidedoor."
    if not identifier or info(app).get("CFBundleIdentifier") != identifier:
        return "it isn't the same app as your Sidedoor."
    found = bundle_version(app)
    if expected_version is not None and found != expected_version:
        return "it is version %s, not %s." % (found or "unknown", expected_version)
    if installed_version is not None and not _newer(found, installed_version):
        return "it isn't newer than the Sidedoor you have (%s)." % installed_version
    return None


def extract(archive, folder, runner=None):
    """`ditto -x -k` into the empty `folder`; (Sidedoor.app path, None) or (None, why)."""
    folder = Path(folder)
    if _runner(runner)(["/usr/bin/ditto", "-x", "-k", str(archive), str(folder)])[0] != 0:
        return None, "the download couldn't be unpacked."
    entries = [entry for entry in folder.iterdir()]
    if len(entries) != 1 or entries[0].name != APP_NAME or entries[0].is_symlink() or not entries[0].is_dir():
        return None, "the download doesn't hold exactly one Sidedoor app."
    return entries[0], None


# --- the download (B) -------------------------------------------------------------------------------

def allowed_asset(url):
    """An https release asset of yoursidedoor/sidedoor on GitHub, or a file on yoursidedoor.com."""
    if not isinstance(url, str):
        return False
    try:
        parts = urllib.parse.urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.username or parts.password or port not in (None, 443):
        return False
    if host in ("github.com", "www.github.com"):
        return parts.path.startswith(GITHUB_DOWNLOADS)
    return host in ("yoursidedoor.com", "www.yoursidedoor.com")


def _allowed_redirect(url):
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.username or parts.password or port not in (None, 443):
        return False
    return host in ASSET_HOSTS or allowed_asset(url)


class _AllowedRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to https on the allow-list (GitHub's asset host); else fail."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _allowed_redirect(newurl):
            raise urllib.error.HTTPError(newurl, code, "redirect to an unexpected host refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch(url, handle, deadline):
    request = urllib.request.Request(url, headers={"User-Agent": "Sidedoor"})
    opener = urllib.request.build_opener(_AllowedRedirects)
    with opener.open(request, timeout=READ_TIMEOUT_SECONDS) as response:
        if not _allowed_redirect(response.geturl()):
            raise OSError("unexpected download host")
        total = 0
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError("download took too long")
            chunk = response.read(1024 * 1024)
            if not chunk:
                return
            total += len(chunk)
            if total > MAX_DOWNLOAD_BYTES:
                raise OSError("download too large")
            handle.write(chunk)


def download(url, sha256, folder, fetch=None):
    """The zip at `url` in `folder`, or (None, why). The sha256 must match."""
    target = Path(folder) / "Sidedoor.zip"
    try:
        with open(target, "wb") as handle:
            (fetch or _fetch)(url, handle, time.monotonic() + DOWNLOAD_DEADLINE_SECONDS)
    except Exception as error:
        LOG.warning("mac update: download failed: %s", type(error).__name__)
        return None, "the download didn't finish (check your connection)."
    digest = hashlib.sha256()
    with open(target, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != sha256:
        return None, "the download didn't match its published checksum."
    return target, None


# --- the dialog (A and B: the one place the user says Replace) ---------------------------------------

DIALOG = ('on run argv\n'
          'activate\n'
          'display dialog (item 1 of argv) with title "Sidedoor" buttons {(item 2 of argv), (item 3 of argv)} '
          'default button 2 giving up after %d\n'
          'end run' % DIALOG_SECONDS)
MESSAGE = ('on run argv\n'
           'activate\n'
           'display dialog (item 1 of argv) with title "Sidedoor" buttons {"OK"} default button 1 '
           'giving up after %d\n'
           'end run' % DIALOG_SECONDS)


def confirm(text, yes, no="Not now", runner=None):
    """A native dialog [no] [yes] (yes the default); True only for a click on `yes`."""
    code, output = _runner(runner)(["/usr/bin/osascript", "-e", DIALOG, text, no, yes])
    return code == 0 and "button returned:%s" % yes in output and "gave up:true" not in output


def tell(text, runner=None):
    _runner(runner)(["/usr/bin/osascript", "-e", MESSAGE, text])


# --- processes ----------------------------------------------------------------------------------------

def processes(runner=None):
    """[(pid, command line)] of every process, or None when `ps` fails."""
    code, output = _runner(runner)(["/bin/ps", "-axww", "-o", "pid=,command="])
    if code != 0:
        return None
    found = []
    for line in output.splitlines():
        pid, _, command = line.strip().partition(" ")
        if pid.isdigit():
            found.append((int(pid), command.strip()))
    return found


def running_from(bundles, exclude=(), runner=None):
    """Pids of processes started from inside any of `bundles` (e.g. job children), `exclude` aside."""
    marks = [str(bundle) + "/Contents/" for bundle in bundles if bundle]
    return [pid for pid, command in (processes(runner) or [])
            if pid not in exclude and any(mark in command for mark in marks)]


def listener_pids(port, runner=None):
    code, output = _runner(runner)(["/usr/sbin/lsof", "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN", "-t"])
    return [int(value) for value in output.split() if value.isdigit()] if code == 0 else []


def command_of(pid, runner=None):
    code, output = _runner(runner)(["/bin/ps", "-ww", "-o", "command=", "-p", str(pid)])
    return output.strip() if code == 0 else ""


def _job_names(pids, runner=None):
    names = []
    for pid in pids:
        words = command_of(pid, runner).split()
        if "job" in words[1:-1]:
            names.append(words[words.index("job") + 1])
    return sorted(set(names))


# --- the swap -----------------------------------------------------------------------------------------

# $SD_* from the environment (paths may hold any character; nothing is ever evaluated). SD_BUNDLES is
# newline-separated. SD_OPEN is /usr/bin/open (tests point it at a recorder).
HELPER = r'''#!/bin/sh
cleanup() {
  case "$SD_CLEANUP" in */sidedoor-download-*) /bin/rm -rf "$SD_CLEANUP" ;; esac
  case "$0" in */sidedoor-update-*/swap.sh) /bin/rm -rf "$(/usr/bin/dirname "$0")" ;; esac
}
trap cleanup EXIT
log() { printf '%s mac-update: %s\n' "$(/bin/date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" >>"$SD_LOG" 2>/dev/null; }
notice() { printf '%s\n' "$1" >"$SD_NOTICE" 2>/dev/null; }
reopen_old() {
  # The old copy when there was one; with none (a first move), the copy that was to be moved.
  if [ "$SD_HAD_OLD" = 1 ]; then back=$SD_TARGET; else back=$SD_NEW; fi
  if [ -d "$back" ]; then
    if "$SD_OPEN" "$back" >>"$SD_LOG" 2>&1; then log "reopened $back"; else log "could not reopen $back"; fi
  fi
}
give_up() { log "failed: $1"; notice "Sidedoor $SD_VERSION didn't install: $2 Your Sidedoor was not changed."; reopen_old; exit 1; }
MV=${SD_MV:-/bin/mv}
DIALOG=${SD_DIALOG:-/usr/bin/osascript}
# Every move into SD_TARGET first makes sure nothing is there (mv into an existing folder nests).
clear_target() {   # moves a partial or new copy at SD_TARGET to the Bin (or the home); 0 when the place is free
  [ -e "$SD_TARGET" ] || return 0
  stamp=$(/bin/date +%Y-%m-%d\ %H.%M.%S)
  for folder in "$(/usr/bin/dirname "$SD_TRASH")" "$(/usr/bin/dirname "$SD_FALLBACK")"; do
    aside="$folder/Sidedoor $SD_VERSION (not installed) $stamp.app"
    if [ -d "$folder" ] && [ ! -e "$aside" ] && $MV "$SD_TARGET" "$aside" 2>>"$SD_LOG" && [ ! -e "$SD_TARGET" ]; then
      log "moved the copy in the way to $aside"; return 0
    fi
  done
  log "could not move the copy in the way at $SD_TARGET"; return 1
}
restore() {   # 0 only when SD_TARGET is the old app again (with no old app: when the place is free)
  clear_target || return 1
  [ -n "$parked" ] || return 0
  if $MV "$parked" "$SD_TARGET" 2>>"$SD_LOG" && [ ! -e "$parked" ] && [ -d "$SD_TARGET/Contents" ]; then
    log "put the old app back"; return 0
  fi
  log "could not put the old app back from $parked"; return 1
}
stuck() {   # the old app could not go back: say truthfully where it is and what to do, on screen too
  log "failed: $1; Sidedoor at $SD_TARGET is not the old app"
  if [ -n "$parked" ] && [ -d "$parked" ]; then
    show=$parked
    if [ "$parked" = "$SD_TRASH" ]; then where="in the Bin"; else where="in $(/usr/bin/dirname "$parked")"; fi
    notice "Sidedoor $SD_VERSION didn't install: $2 Your old Sidedoor is at $parked. Move it back into $(/usr/bin/dirname "$SD_TARGET") (replace what is there) and open it."
    text="The Sidedoor update didn't finish. Your previous Sidedoor is $where. Drag it back into $(/usr/bin/dirname "$SD_TARGET") to keep using it."
  else
    show=$SD_TARGET
    notice "Sidedoor $SD_VERSION didn't install: $2 Something is left at $SD_TARGET. Move it to the Bin, then move Sidedoor into $(/usr/bin/dirname "$SD_TARGET") by hand."
    text="The Sidedoor update didn't finish. Something is left at $SD_TARGET: move it to the Bin, then move Sidedoor into $(/usr/bin/dirname "$SD_TARGET") by hand."
  fi
  # Nothing of Sidedoor is running to show the notice, so the helper says it itself. display dialog
  # runs inside osascript (no Apple event to another app, so no Automation prompt).
  answer=$("$DIALOG" -e 'on run argv' -e 'activate' \
    -e 'display dialog (item 1 of argv) with title "Sidedoor" buttons {"OK", "Show in Finder"} default button 2 giving up after 3600' \
    -e 'end run' "$text" 2>>"$SD_LOG")
  case "$answer" in *"button returned:Show in Finder"*) "$SD_OPEN" -R "$show" >>"$SD_LOG" 2>&1 ;; esac
  exit 1
}
alive() {
  for p in $SD_PIDS; do
    s=$(/bin/ps -o stat= -p "$p" 2>/dev/null)
    case "$s" in ""|Z*) ;; *) return 0 ;; esac   # gone, or ended and not yet reaped
  done
  return 1
}
busy() {
  procs=$(/bin/ps -axww -o command= 2>/dev/null) || return 0
  saved=$IFS; IFS='
'
  for b in $SD_BUNDLES; do
    case "$procs" in *"$b/Contents/"*) IFS=$saved; return 0 ;; esac
  done
  IFS=$saved; return 1
}
log "waiting for Sidedoor to quit (pids $SD_PIDS)"
n=0
while alive; do
  if [ "$n" -ge "$SD_WAIT" ]; then
    log "failed: Sidedoor did not quit within $SD_WAIT s"
    notice "Sidedoor $SD_VERSION didn't install: Sidedoor didn't quit in time. Your Sidedoor was not changed."
    exit 1
  fi
  /bin/sleep 1; n=$((n + 1))
done
log "Sidedoor quit; waiting for running jobs to finish"
n=0
while busy; do
  [ "$n" -ge "$SD_JOB_WAIT" ] && give_up "a job was still running after $SD_JOB_WAIT s" "a Sidedoor job was still running."
  /bin/sleep 2; n=$((n + 2))
done
[ -d "$SD_NEW" ] || give_up "the new app is gone: $SD_NEW" "the new copy was no longer there."
parked=""
if [ -e "$SD_TARGET" ]; then
  if [ ! -e "$SD_TRASH" ] && $MV "$SD_TARGET" "$SD_TRASH" 2>>"$SD_LOG" && [ ! -e "$SD_TARGET" ]; then
    parked=$SD_TRASH; log "moved the old app to $SD_TRASH"
  elif [ -e "$SD_TARGET" ] && [ ! -e "$SD_FALLBACK" ] && /bin/mkdir -p "$(/usr/bin/dirname "$SD_FALLBACK")" \
      && $MV "$SD_TARGET" "$SD_FALLBACK" 2>>"$SD_LOG" && [ ! -e "$SD_TARGET" ]; then
    parked=$SD_FALLBACK; log "the Trash refused; moved the old app to $SD_FALLBACK"
  else
    give_up "could not move the old app aside" "the old copy couldn't be moved to the Bin."
  fi
fi
[ -e "$SD_TARGET" ] && give_up "$SD_TARGET is still taken" "the old copy couldn't be moved to the Bin."
if ! $MV "$SD_NEW" "$SD_TARGET" 2>>"$SD_LOG"; then
  if restore; then give_up "could not move the new app into $SD_TARGET" "the new copy couldn't be moved into Applications."; fi
  stuck "could not move the new app into $SD_TARGET" "the new copy couldn't be moved into Applications."
fi
log "moved the new app to $SD_TARGET"
/usr/bin/xattr -dr com.apple.quarantine "$SD_TARGET" 2>/dev/null
if "$SD_OPEN" "$SD_TARGET" >>"$SD_LOG" 2>&1; then
  log "opened the new Sidedoor $SD_VERSION"
  /bin/rm -f "$SD_NOTICE"
  exit 0
fi
if [ ! -e "$SD_NEW" ] && $MV "$SD_TARGET" "$SD_NEW" 2>>"$SD_LOG" && [ ! -e "$SD_TARGET" ]; then
  log "moved the new app back to $SD_NEW"
fi
if restore; then give_up "could not open the new app" "the new copy wouldn't open."; fi
stuck "could not open the new app" "the new copy wouldn't open."
'''


def trash_destination(old_version):
    """~/.Trash/Sidedoor <version>.app, made unique with the time when that name is taken."""
    trash = _user_home() / ".Trash"
    name = "Sidedoor %s" % (old_version or "old")
    target = trash / (name + ".app")
    if target.exists():
        target = trash / ("%s %s.app" % (name, time.strftime("%Y-%m-%d %H.%M.%S")))
    return target


def _writable(target, has_old):
    folder = Path(target).parent
    if not os.access(folder, os.W_OK):
        return False
    return not has_old or os.access(target, os.W_OK)


def _spawn(argv, env):
    subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True)


def swap(home, new_app, target, old_version, new_version, pids, bundles, spawn=None, open_command="/usr/bin/open",
         cleanup=None):
    """Writes HELPER to a fresh temporary folder (outside every bundle) and starts it detached.
    `cleanup`: B's download folder, removed when the helper ends. Returns the helper's path."""
    import paths
    home = Path(home)
    folder = Path(tempfile.mkdtemp(prefix="sidedoor-update-"))
    script = folder / "swap.sh"
    script.write_text(HELPER)
    os.chmod(script, 0o700)
    logs = paths.logs(None, home)
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = "Sidedoor %s.app" % (old_version or "old")
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "SD_LOG": str(logs / LOG_NAME), "SD_NOTICE": str(home / NOTICE_FILE),
           "SD_TARGET": str(target), "SD_NEW": str(new_app), "SD_TRASH": str(trash_destination(old_version)),
           "SD_FALLBACK": str(home / PREVIOUS_VERSIONS / name), "SD_VERSION": new_version or "",
           "SD_HAD_OLD": "1" if Path(target).exists() else "0", "SD_PIDS": " ".join(str(pid) for pid in pids),
           "SD_BUNDLES": "\n".join(str(bundle) for bundle in bundles if bundle),
           "SD_WAIT": str(WAIT_SECONDS), "SD_JOB_WAIT": str(JOB_WAIT_SECONDS), "SD_OPEN": open_command,
           "SD_CLEANUP": str(cleanup or "")}
    LOG.warning("mac update: starting the swap helper for %s -> %s", target, new_version)
    (spawn or SPAWN or _spawn)(["/bin/sh", str(script)], env)
    return script


def read_notice(home):
    """The helper's failure notice, or None. Kept (a layout built for a partial response, e.g. the
    license check's chips, must not use it up) until the user dismisses it (`clear_notice`, from
    update_check.dismiss), a later swap succeeds (the helper removes it), or the version it names is
    installed some other way (`update_check.banner`)."""
    try:
        text = (Path(home) / NOTICE_FILE).read_text().strip()
    except OSError:
        return None
    return text[:500] or None


def clear_notice(home):
    try:
        (Path(home) / NOTICE_FILE).unlink(missing_ok=True)
    except OSError:
        pass


# --- B: [Update now] ----------------------------------------------------------------------------------

_lock = threading.Lock()
_state = {}


def status():
    """{"state": "working"|"failed", "version", "message"} of this process's latest [Update now], or {}."""
    with _lock:
        return dict(_state)


def _set(**values):
    with _lock:
        _state.clear()
        _state.update(values)


def can_install(platform=None, frozen=None):
    """[Update now] is offered: the built Mac app, running from Applications."""
    if not is_mac_app(platform, frozen):
        return False
    import login_item
    try:
        return login_item.app_location_problem(program=_program(), platform="darwin", frozen=True) is None \
            and bundle_of(_program()) is not None
    except Exception:
        return False


def asset_for(available, runner=None):
    """This Mac's {"url", "sha256"} from a stored version.json answer, or None."""
    assets = available.get("mac") if isinstance(available, dict) else None
    if not isinstance(assets, dict):
        return None
    asset = assets.get(machine_arch(runner))
    return asset if isinstance(asset, dict) and allowed_asset(asset.get("url")) else None


def _quit_self():
    os.kill(os.getpid(), signal.SIGTERM)  # the normal stop (login_item.clean_stop, the tray's Quit)


def install(version, home, runner=None, fetch=None, spawn=None, quit_self=None, available=None):
    """The whole of [Update now]. Returns {"state": "replacing"|"declined"|"failed", "message"}."""
    import update_check
    import version as installed
    run = _runner(runner)

    def failed(why):
        LOG.warning("mac update: %s not installed: %s", version, why)
        return {"state": "failed", "message": UPDATE_FAILED % (version, why if why.startswith("macOS") else why[:1].upper() + why[1:])}

    if not can_install():
        return failed("this copy of Sidedoor can't update itself.")
    if available is None:
        available = update_check._read(home).get("available") or {}
    if available.get("latest") != version or not _newer(version, installed.current()):
        return failed("that version is no longer offered.")
    asset = asset_for(available, run)
    if asset is None:
        return failed("there is no download for this Mac yet.")
    current = bundle_of(_program())
    team = team_id(current, run)
    identifier = info(current).get("CFBundleIdentifier")
    if not _writable(current, True):
        return failed("Sidedoor can't write to %s." % current.parent)
    folder = Path(tempfile.mkdtemp(prefix="sidedoor-download-"))
    keep = False
    try:
        archive, why = download(asset["url"], asset["sha256"], folder, fetch)
        if why:
            return failed(why)
        unpacked = folder / "unpacked"
        unpacked.mkdir()
        app, why = extract(archive, unpacked, run)
        if why:
            return failed(why)
        why = verify(app, identifier, team, expected_version=version, installed_version=installed.current(), runner=run)
        if why:
            return failed(why)
        jobs = _job_names(running_from([current], exclude=(os.getpid(),), runner=run), run)
        text = ("Replace Sidedoor %s with %s? Sidedoor quits, swaps itself in %s and opens again. Your old "
                "copy goes to the Bin." % (installed.current(), version, current.parent))
        if jobs:
            text += (" Sidedoor is finishing a job (%s): the new version opens once it ends." % ", ".join(jobs))
        if not confirm(text, "Replace", runner=run):
            LOG.warning("mac update: %s: the user chose Not now", version)
            return {"state": "declined", "message": None}
        swap(home, app, current, installed.current(), version, [os.getpid()], [current], spawn=spawn, cleanup=folder)
        keep = True
        timer = threading.Timer(QUIT_DELAY_SECONDS, quit_self or QUIT_SELF or _quit_self)
        timer.daemon = True
        timer.start()
        return {"state": "replacing", "message": "Sidedoor is updating to %s and opens again by itself." % version}
    except Exception as error:
        return failed("something went wrong (%s)." % type(error).__name__)
    finally:
        if not keep:
            shutil.rmtree(folder, ignore_errors=True)


def start_install(version, home, **options):
    """[Update now]: `install` in a background thread (a download can take minutes). False when one
    is already going. A retry clears an earlier failure's notice first."""
    with _lock:
        if _state.get("state") in ("working", "replacing"):
            return False
        clear_notice(home)
        _state.clear()
        _state.update(state="working", version=version, message="Downloading and checking Sidedoor %s…" % version)

    def work():
        try:
            result = install(version, home, **options)
        except Exception as error:
            result = {"state": "failed", "message": UPDATE_FAILED % (version, "Something went wrong (%s)."
                                                                     % type(error).__name__)}
        if result["state"] == "declined":
            _set()
        else:
            _set(state=result["state"], version=version, message=result["message"])
    threading.Thread(target=work, name="sidedoor-mac-update", daemon=True).start()
    return True


# --- A: a downloaded copy opened from outside Applications ------------------------------------------

def original_path(app):
    """The real place of a translocated app (macOS runs a downloaded app from a random read-only
    folder until it is moved), via Security.framework's SecTranslocateCreateOriginalPathForURL;
    `app` itself when it is not translocated; None when it cannot be told."""
    if "/AppTranslocation/" not in str(app):
        return Path(app)
    try:
        import ctypes
        cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        security = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Security.framework/Security")
        cf.CFURLCreateFromFileSystemRepresentation.restype = ctypes.c_void_p
        cf.CFURLCreateFromFileSystemRepresentation.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long,
                                                               ctypes.c_bool]
        cf.CFURLGetFileSystemRepresentation.restype = ctypes.c_bool
        cf.CFURLGetFileSystemRepresentation.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_char_p, ctypes.c_long]
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        security.SecTranslocateCreateOriginalPathForURL.restype = ctypes.c_void_p
        security.SecTranslocateCreateOriginalPathForURL.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        raw = os.fsencode(str(app))
        url = cf.CFURLCreateFromFileSystemRepresentation(None, raw, len(raw), True)
        if not url:
            return None
        try:
            original = security.SecTranslocateCreateOriginalPathForURL(url, None)
        finally:
            cf.CFRelease(url)
        if not original:
            return None
        try:
            buffer = ctypes.create_string_buffer(4096)
            if not cf.CFURLGetFileSystemRepresentation(original, True, buffer, len(buffer)):
                return None
            return Path(os.fsdecode(buffer.value))
        finally:
            cf.CFRelease(original)
    except Exception:
        return None


def installed_copy():
    """The Sidedoor.app in /Applications, else in ~/Applications, else None."""
    for folder in (APPLICATIONS, _user_home() / "Applications"):
        candidate = folder / APP_NAME
        if (candidate / "Contents" / "Info.plist").is_file():
            return candidate
    return None


def _destination():
    """Where a first copy goes: /Applications when Sidedoor may write there, else ~/Applications."""
    if os.access(APPLICATIONS, os.W_OK):
        return APPLICATIONS / APP_NAME
    return _user_home() / "Applications" / APP_NAME


def offer_move(home, running=None, port=None, runner=None, spawn=None, kill=None, translocated=None):
    """A, called by `dashboard._serve` before it binds its port. True when this copy has handed over
    to the swap helper (or opened the installed copy) and must exit now; False to go on as today
    (the "Move Sidedoor to your Applications folder" banner). `running()` is True when this install's
    dashboard answers on `port` (`dashboard.running_here`)."""
    import login_item
    import version as this
    if not is_mac_app() or login_item.started_at_login():
        return False
    program = _program()
    if login_item.app_location_problem(program=program, platform="darwin", frozen=True) is None:
        return False
    run = _runner(runner)
    here = bundle_of(program)
    source = (translocated or original_path)(here) if here is not None else None
    if source is None or not (source / "Contents" / "Info.plist").is_file():
        LOG.warning("mac update: can't tell where this copy is (%s); showing the Move banner", here)
        return False
    installed = installed_copy()
    if installed is not None and installed.resolve() == source.resolve():
        return False
    old_version = bundle_version(installed) if installed is not None else None
    if installed is not None and old_version and not _newer(this.current(), old_version):
        word = "newer" if _newer(old_version, this.current()) else "the same version"
        text = ("Sidedoor %s in %s is %s than this copy (%s). Open that one instead?" % (
            old_version, installed.parent, word, this.current())) if word == "newer" else (
            "Sidedoor %s is already in %s. Open that one instead?" % (old_version, installed.parent))
        if confirm(text, "Open Sidedoor %s" % old_version, runner=run):
            run(["/usr/bin/open", str(installed)])
            return True
        return False
    target = installed or _destination()
    team = team_id(installed, run) if installed is not None else None
    identifier = info(installed).get("CFBundleIdentifier") if installed is not None else info(source).get(
        "CFBundleIdentifier")
    if installed is None:
        team = team_id(source, run)
    why = verify(source, identifier, team, runner=run)
    if why:
        tell("Sidedoor can't move this copy into Applications: %s Move it there by hand, then open it." % why, run)
        return False
    if not _writable(target, installed is not None) or not os.access(source.parent, os.W_OK):
        tell("Sidedoor can't write to %s. Move Sidedoor there by hand, then open it." % target.parent, run)
        return False
    pids, bundles = [os.getpid()], [here, source]
    if installed is not None:
        bundles.append(installed)
    old_pid = None
    if running is not None and running():
        candidates = listener_pids(port, run) if port else []
        old_pid = candidates[0] if len(candidates) == 1 else None
        if old_pid is None:
            tell("Quit Sidedoor from its menu bar icon, then open this copy again to move it into Applications.", run)
            return False
        old_command = command_of(old_pid, run)
        old_bundle = bundle_of(old_command.split(" serve")[0]) if old_command else None
        if old_bundle is not None:
            bundles.append(old_bundle)
        pids.append(old_pid)
    if installed is not None:
        text = ("Replace Sidedoor %s with %s in %s? Sidedoor quits, moves this copy there and opens it. "
                "Your old copy goes to the Bin." % (old_version or "", this.current(), target.parent))
        yes = "Replace"
    else:
        text = "Move Sidedoor to %s? Sidedoor moves this copy there and opens it." % target.parent
        yes = "Move"
    jobs = _job_names(running_from([installed] if installed else [], exclude=tuple(pids), runner=run), run)
    if jobs:
        text += " Sidedoor is finishing a job (%s): it opens once that ends." % ", ".join(jobs)
    if not confirm(text, yes, runner=run):
        return False
    swap(home, source, target, old_version, this.current(), pids, bundles, spawn=spawn)
    if old_pid is not None:
        (kill or KILL or os.kill)(old_pid, signal.SIGTERM)  # its normal stop: the tray's Quit, exit 0
    return True
