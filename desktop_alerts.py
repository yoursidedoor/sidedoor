"""E15.T3 (R2): desktop notifications and the tray count, with the dashboard window closed.

One source (owner, 2026-11-11): what Overview's Needs you and Health show (`overview_page.attention`,
the functions `gather` calls) and the banners the layout shows. Nothing is worked out again here, and
nothing is matched on an item's words: a condition is known by the item's own name and tone.

Owner's picks, 2026-11-12:
1. once when a problem starts, then one reminder a day while it is red (REMINDER_SECONDS);
2. an amber item is notified once, with no reminder;
3. more than two arriving in one check become one notification (COMBINE_OVER);
4. the user's own pause is notified only once it has lasted 7 days (PAUSE_SECONDS);
5. one switch in Settings, on by default (`enabled`, SETTING in setup-state.json). While it is off
   nothing is posted; the tray count still shows;
6. an update is notified once per version;
7. clicking opens the page that fixes it (the item's own fix link; Overview when it has none).
Not notified: anything before Setup ends except "Setup is not finished" after SETUP_SECONDS (Overview
shows no Health or Needs you until then), the short OpenAI rate-limit wait, and an item that starts
within USER_ACTION_SECONDS of the user's own action on that item in the dashboard (`user_acted`,
ROUTE_ITEMS: Pause and the Scheduler line, a key saved in Settings and that connection's line). That
only keeps the start quiet: a red item's daily reminder still comes, and Needs you is never quieted.

One condition is an item's name, tone and `state` (the producer's own name for which condition it is,
`overview_page._item`): a change of any of them is a new condition and is notified at once.

Where it runs: its own timer in the Sidedoor process (`Watcher`, started by `dashboard._serve` beside
the license heartbeat and the update check), every INTERVAL_SECONDS, whether the schedule runs, is
paused or has stopped. No network call, nothing written to the database, no process looked at or
stopped and the browser lock never touched: the browser line comes from files only
(`overview_page.health(live=False)`), so a Chrome window holding Sidedoor's profile is seen only on
Overview. Like any page, the read removes a usage-limit wait file whose reset time has passed.

What is remembered: `desktop-alerts.json` in the Sidedoor home (STATE_FILE), written before anything
is posted, so a restart never repeats a notification. Nothing is checked or remembered while there
is no way to post (the tray is not up yet, or there is none), so nothing due is ever marked as shown
without having been shown. The first check of an install that has no such
file (a new install, or the first start after the update that brought this) notifies nothing: what is
wrong and what waits at that moment is taken as known. A red item found then gets its daily reminder.

Windows shows one notification at a time and cannot say which one was clicked, so there two or more
due together are already one combined notification (`combine_over`).

Delivery is `notifications.Desktop` (the OS call is the tray's, `tray.Tray.notify`).
"""

import datetime as dt
import json
import os
import threading
from pathlib import Path

import log
import notifications
import setup_steps

LOG = log.get(__name__)

STATE_FILE = "desktop-alerts.json"
SETTING = "desktop_alerts"
LABEL = "Show a desktop notification when something needs you or stops working"
INTERVAL_SECONDS = 60
REMINDER_SECONDS = 24 * 3600
PAUSE_SECONDS = 7 * 24 * 3600
SETUP_SECONDS = 24 * 3600
USER_ACTION_SECONDS = 120
COMBINE_OVER = 2
ONCE_KEPT = 20          # how many once-only keys (update versions) are remembered
OVERVIEW = "/"
APP = "Your Sidedoor"
VERSION = 3             # of the saved state; another version starts again quietly
WAIT_SECONDS = 1        # how often the watcher looks for a way to post while there is none
ENGINE = "engine"       # the chosen AI's Health line, whatever its name
# U3: what the user does in the dashboard, and the lines that action can change. A path is matched by
# its start; the first match wins. Anything else (approvals, profile, mode...) quiets nothing.
ROUTE_ITEMS = (
    (("/pause", "/resume", "/start", "/setup/start", "/data/move"), ("Scheduler",)),
    # removing the saved keys also marks OpenRouter's key as gone, and like every license action it
    # sets or clears the license pause (the Scheduler line)
    (("/settings/keys",), ("License", "OpenRouter", "Scheduler")),
    (("/license", "/settings/license", "/setup/license"), ("License", "Scheduler")),
    # the reset setup file forgets every stored check
    (("/setup-file",), ("setup", "banner:setup-file", "License", "LinkedIn", "OpenRouter", ENGINE, "Browser profile",
                        "Scheduler")),
    (("/settings/linkedin", "/setup/linkedin"), ("LinkedIn",)),
    (("/settings/openrouter", "/setup/openrouter"), ("OpenRouter",)),
    (("/settings/engine", "/settings/codex", "/settings/claude", "/settings/antigravity",
      "/setup/engine", "/setup/codex", "/setup/claude", "/setup/antigravity"), (ENGINE, "banner:openai")),
    (("/settings/browser", "/settings/webmail", "/settings/email",
      "/setup/browser", "/setup/webmail", "/setup/email"), ("Browser profile",)),
    (("/setup",), ("setup", "banner:setup-file")),
    # E15 join: the cards' buttons (/access/...), the live view (/live-view/...) and the account setting
    # (/settings/account) have no row. None of them changes a Health line; they only move Needs you
    # counts, which are never quieted, and a count going down never notifies.
)
NEEDS_TITLE = "Needs you"
NOTIFIED_TONES = ("error", "warning")


# --- the Settings switch ----------------------------------------------------------------------------

def enabled(home=None):
    """On unless the user turned it off (on by default, and on when the file can't be read)."""
    try:
        return setup_steps.load_state(home).get(SETTING) is not False
    except Exception:
        return True


def set_enabled(value, home=None):
    setup_steps.update_state(lambda state: state.__setitem__(SETTING, bool(value)), home)


# --- what the user just did -------------------------------------------------------------------------

_acted = {}   # item -> when the user last acted on it in the dashboard


def user_acted(path, now=None):
    """The dashboard saw the user change something at `path` (a form, a button)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    for starts, items in ROUTE_ITEMS:
        if str(path).startswith(starts):
            for item in items:
                _acted[item] = now
            return


def _user_caused(now):
    """The items the user acted on within USER_ACTION_SECONDS."""
    return {item for item, at in list(_acted.items()) if abs((now - at).total_seconds()) < USER_ACTION_SECONDS}


# --- conditions: one per thing the dashboard shows as wrong --------------------------------------------

def _utc(value):
    try:
        moment = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.timezone.utc)


def _line(item):
    """The Health line as Overview shows it: the item's words, then its notes (never a note's detail,
    never the link notes)."""
    parts = [str(item.get("text") or "")] + [str(note["text"]) for note in item.get("notes") or ()
                                             if note.get("text") and not note.get("href")]
    return " ".join(part if part.endswith((".", "!", "?")) else part + "." for part in parts if part)


def health_conditions(items, engine_names=None):
    """The red and amber Health items, each known by its name, tone and state. The user's own pause
    (`user_paused_at` on the item) is its own condition, due only after PAUSE_SECONDS."""
    engine_names = set(setup_steps.ENGINE_LABELS.values()) if engine_names is None else engine_names
    found = []
    for item in items or ():
        if item.get("tone") not in NOTIFIED_TONES:
            continue
        fix = item.get("fix") or {}
        name = str(item["name"])
        condition = {"key": "health:%s:%s:%s" % (name, item["tone"], item.get("state") or ""), "tone": item["tone"],
                     "title": name, "text": _line(item), "href": fix.get("href"),
                     "item": ENGINE if name in engine_names else name}
        if item.get("user_paused_at"):
            condition.update(key="pause", since=_utc(item["user_paused_at"]), delay=PAUSE_SECONDS)
        found.append(condition)
    return found


def _from_banner(key, banner, **extra):
    return dict({"key": key, "tone": banner.get("tone"), "title": APP, "text": str(banner.get("text") or ""),
                 "href": banner.get("action_href"), "item": key}, **extra)


def banner_conditions(home, progress):
    """The layout's banners that no Health item states: the setup file, the saved keys, the OpenAI key,
    an update, and Setup left unfinished."""
    import credentials
    import dashboard
    import openai_auth
    import update_check
    found = []
    if setup_steps.state_unreadable(home):
        found.append(_from_banner("banner:setup-file", dashboard.SETUP_FILE_BANNER))
    hold = credentials.hold_reason(home)
    if hold:
        found.append({"key": "banner:keys", "tone": "error", "title": APP, "text": str(hold), "href": None})
    if setup_steps.chosen_engine(progress["state"]) == "codex":
        banner = openai_auth.banner(home)
        waiting = openai_auth.last_wait(home) if banner else None
        short = bool(waiting) and waiting.get("kind") == openai_auth.RATE_LIMIT and banner.get("tone") == "warning" \
            and not openai_auth.fallback(home) and not openai_auth.hold(home)
        if banner and not short:   # a short rate-limit wait clears by itself: not notified
            found.append(_from_banner("banner:openai", banner))
    update = update_check.banner(home)
    if update and update.get("dismiss"):   # the version on offer: once per version (pick 6)
        found.append({"key": "update:" + str(update["dismiss"]), "tone": "info", "title": APP,
                      "text": str(update.get("text") or ""), "href": None, "once": True})
    if not progress["finished"]:
        found.append(_from_banner("setup", dashboard.setup_banner(progress), delay=SETUP_SECONDS))
    return found


def needs_rows():
    import overview_page
    return overview_page.NEEDS_ROWS


def gather(home, root, read, now, config=None):
    """{"conditions": [...], "needs": {kind: count} or None (not shown now)} from stored records."""
    import overview_page
    import paths
    import scheduler_loop
    progress = setup_steps.progress(home)
    conditions, needs = banner_conditions(home, progress), None
    if progress["finished"]:   # Overview shows Needs you and Health only once Setup is finished
        config = config if config is not None else scheduler_loop.load_config(root)
        scheduler = overview_page.loop_state(setup_steps.scheduler_running(home))
        view = overview_page.snapshot(
            paths.database(root),
            lambda database: overview_page.attention(database, config, home, progress, scheduler, now), read)
        conditions = health_conditions(view["health"]) + conditions
        needs = view["needs"] if isinstance(view["needs"], dict) else None
    return {"conditions": conditions, "needs": needs}


# --- the rules ---------------------------------------------------------------------------------------

def _seconds(now, text):
    moment = _utc(text)
    return None if moment is None else (now - moment).total_seconds()


def evaluate(state, conditions, needs, now, caused=(), rows=None, combine_over=COMBINE_OVER):
    """(the state to save, the notifications to post). `state` None is an install with nothing
    remembered: everything found now is taken as known and nothing is posted. `caused`: the items the
    user just acted on in the dashboard; one of those starting now is recorded without being posted
    (its reminder still comes).

    A condition that starts is announced unless (review round 2):
    - K4: its line was red a moment ago and is amber now (it got better);
    - K1, K6: this very condition (name, tone and state) was posted in the last REMINDER_SECONDS (a
      problem that clears and comes back). `posted` keeps those times for that long, also after the
      line went green. A different state of the same line is a different problem: announced at once.
      K7: an amber one kept back this way is announced once more if it is still showing when that
      time is up (red has its daily reminder for that).
    Each note carries `undo`: what to take back in the state if its post fails (K3)."""
    rows = needs_rows() if rows is None else rows
    first = state is None
    state = state or {}
    old = state.get("conditions") if isinstance(state.get("conditions"), dict) else {}
    old = {key: entry for key, entry in old.items() if isinstance(entry, dict)}
    once = [key for key in state.get("once") or () if isinstance(key, str)][-ONCE_KEPT:]
    posted = {key: at for key, at in (state.get("posted") if isinstance(state.get("posted"), dict) else {}).items()
              if (_seconds(now, at) is not None and abs(_seconds(now, at)) < REMINDER_SECONDS)}
    stamp = now.isoformat(timespec="seconds")
    kept, notes = {}, []

    def mark(key, entry, previous):
        """Post this condition now; `undo` puts everything back as it was."""
        before, held = posted.get(key), entry.get("held")
        entry["last_notified"] = stamp
        entry.pop("held", None)
        posted[key] = stamp

        def undo():
            entry["last_notified"] = previous
            if held:
                entry["held"] = held
            if before is None:
                posted.pop(key, None)
            else:
                posted[key] = before
        return undo

    for condition in conditions:
        key, tone = condition["key"], condition["tone"]
        note = {"title": condition["title"], "text": condition["text"], "href": condition.get("href") or OVERVIEW}
        if condition.get("once"):
            if key not in once:
                once.append(key)
                if not first:
                    notes.append(dict(note, undo=[lambda key=key: key in once and once.remove(key)]))
            continue
        since = condition.get("since")
        line = str(condition.get("item") or key)
        delayed = bool(condition.get("delay"))
        entry = old.get(key)
        if entry is not None and since is not None and entry.get("first_seen") != since.isoformat(timespec="seconds"):
            entry = None   # a new pause, not the one remembered
        if entry is None:
            entry = {"tone": tone, "first_seen": (since or now).isoformat(timespec="seconds"), "last_notified": None}
        entry = dict(entry, tone=tone, line=line)
        kept[key] = entry   # a condition no longer found is forgotten; `posted` remembers its last post
        lasted = _seconds(now, entry["first_seen"])
        if lasted is not None and lasted < condition.get("delay", 0):
            continue
        if entry.get("last_notified") is None:                    # it started (picks 1, 2, 4)
            before = [other for name, other in old.items() if name != key and other.get("line") == line]
            recent = posted.get(key)
            if first or condition.get("item") in caused:          # known already, or the user's own doing
                entry["last_notified"] = stamp
            elif tone == "warning" and any(other.get("tone") == "error" for other in before):
                entry["last_notified"] = stamp                    # K4: it got better
            elif recent and not delayed:
                entry.update(last_notified=recent, held=True)     # K1: said within the day; the reminder counts from then
            else:
                notes.append(dict(note, undo=[mark(key, entry, None)]))
        elif (tone == "error" or entry.get("held")) and (_seconds(now, entry["last_notified"]) or 0) >= REMINDER_SECONDS:
            # pick 1's reminder while red; K7: the one catch-up post of an amber item that was kept back
            notes.append(dict(note, undo=[mark(key, entry, entry["last_notified"])]))
    counts = dict(state.get("needs")) if isinstance(state.get("needs"), dict) else {}
    if needs is not None:
        for key, one, many, href, _label in rows:
            count, before = int(needs.get(key) or 0), counts.get(key)
            counts[key] = count
            if isinstance(before, int) and count > before and not first:   # a kind never counted is taken as known
                notes.append({"title": NEEDS_TITLE, "text": "%d %s" % (count, one if count == 1 else many), "href": href,
                              "waiting": True, "undo": [lambda key=key, before=before: counts.__setitem__(key, before)]})
    total = sum(int(value or 0) for value in counts.values())
    return ({"version": VERSION, "conditions": kept, "needs": counts, "once": once, "posted": posted},
            combine(notes, total, combine_over))


def combine(notes, waiting=0, over=COMBINE_OVER):
    """Pick 3: more than `over` at once are one notification that opens Overview. "N things need you"
    always counts what waits on the user (Needs you's total, the tray's number); when a problem is among
    them the words say how many notifications it stands for instead."""
    if len(notes) <= over:
        return notes
    undo = [step for note in notes for step in note.get("undo") or ()]
    if all(note.get("waiting") for note in notes) and waiting > 1:
        return [{"title": APP, "text": "%d things need you" % waiting, "href": OVERVIEW, "undo": undo}]
    return [{"title": APP, "text": "%d things need attention in Sidedoor" % len(notes), "href": OVERVIEW, "undo": undo}]


# --- what is remembered --------------------------------------------------------------------------------

def _home(home=None):
    return setup_steps._home(home)


def load(home=None):
    """The saved state, or None (no file yet, or one that cannot be read: start again quietly)."""
    try:
        value = json.loads((_home(home) / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) and value.get("version") == VERSION else None


def save(home, state):
    path = _home(home) / STATE_FILE
    temporary = path.with_name(path.name + ".%d.tmp" % os.getpid())
    temporary.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


# --- the timer -----------------------------------------------------------------------------------------

class Watcher:
    """One check every `interval` seconds. `read` opens the read-only database connection
    (`dashboard.read_connection`); `provider`, `clock` and `gather` are injectable for tests."""

    def __init__(self, home=None, root=None, read=None, provider=None, clock=None, interval=INTERVAL_SECONDS,
                 gather=None, combine_over=None):
        self.home, self.root, self.read = home, root, read
        self.provider = provider or notifications.Desktop()
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self.interval = interval
        self.gather = gather or (lambda now: globals()["gather"](self.home, self.root, self.read, now))
        self.total = 0
        self._refused = False   # the latest post failed (K8)
        # U5: Windows shows one notification at a time and its click carries no identity
        self.combine_over = combine_over if combine_over is not None else (1 if os.name == "nt" else COMBINE_OVER)
        self._stop = threading.Event()
        self._thread = None

    def tick(self):
        """One check. Returns the notifications it posted. With no way to post (no tray yet, or none
        at all) nothing is checked and nothing is remembered, so what is due waits for the tray (U1)."""
        if not self.provider.available():
            return []
        now = self.clock()
        found = self.gather(now)
        state, notes = evaluate(load(self.home), found["conditions"], found["needs"], now, caused=_user_caused(now),
                                combine_over=self.combine_over)
        save(self.home, state)   # before posting: a stop between the two never posts the same thing twice
        if found["needs"] is not None:
            self.total = sum(int(value or 0) for value in state["needs"].values())
        if not notes or not enabled(self.home):
            return []
        posted, failed = [], False
        for number, note in enumerate(notes):
            try:
                self.provider.send_notification({"event_key": "%s#%d" % (now.isoformat(), number), "title": note["title"],
                                                 "message": note["text"], "href": note["href"]})
                posted.append(note)
            except Exception as error:   # the tray is stopping, or the OS refused: taken back, tried at the next check (K3)
                if not self._refused:   # K8: one line per run of failures, not one a minute
                    LOG.warning("Desktop notification not shown (%s); trying again at each check.", type(error).__name__)
                for step in note.get("undo") or ():
                    step()
                failed = True
        if failed:
            save(self.home, state)
        elif self._refused:
            LOG.info("Desktop notifications are shown again.")
        self._refused = failed
        return posted

    def run(self):
        while not self._stop.is_set():
            if not self.provider.available():   # the tray is not up yet: the first check waits for it
                self._stop.wait(WAIT_SECONDS)
                continue
            try:
                self.tick()
            except Exception as error:   # a busy database, a file mid-write: the next check tries again
                LOG.warning("Desktop notification check failed (%s).", type(error).__name__)
            self._stop.wait(self.interval)

    def start(self):
        self._thread = threading.Thread(target=self.run, name="sidedoor-desktop-alerts", daemon=True)
        self._thread.start()
        return self

    def stop(self, wait_seconds=2):
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(wait_seconds)


_current = None


def start(home=None, root=None, read=None):
    """This process's watcher (one; a second call returns the one already running)."""
    global _current
    if _current is None:
        _current = Watcher(home, root, read).start()
    return _current


def stop():
    global _current
    watcher, _current = _current, None
    if watcher is not None:
        watcher.stop()


def waiting():
    """How many things wait on the user (Needs you's total at the latest check), for the tray."""
    return _current.total if _current is not None else 0
