"""E12.T2 (owner pick 2026-11-05, option a): opt-in progress counts sent with the daily license
heartbeat (`license_clock._check`).

Settings > License and Setup's Start step have the checkbox "Share anonymous progress counts with
Your Sidedoor"; the choice is `share_progress` in setup-state.json. One rule (`shown`) says what the
box shows and what the heartbeat sends: the saved choice; before one is saved, on while Start
(step 10) is still pending and off once Start is done. Pressing Start saves what the box showed when
no choice is saved yet (`save_shown_if_unchosen`, only while Start is pending). While it is on, the
heartbeat adds `milestones` = {"setup_finished", "first_drafted", "first_sent"}, each the first day
it happened ("YYYY-MM-DD", UTC) or None. While it is off, only the opt-out marker is added. The relay
keeps only these three dates (relay/milestones.py). Each is read from what Sidedoor already records;
nothing new is tracked:
- setup_finished: setup-state.json `setup_finished_at` (latched once the required steps are done).
- first_drafted: the earliest outreach message the AI composed (outreach_agent_audit,
  phase COMPOSITION, kind DECISION).
- first_sent: the earliest first outreach Sidedoor sent (engagement_events kind `initial_sent` from
  `SENT_SOURCES`; history imported from LinkedIn is not counted).
While it is off, the heartbeat says so (`milestones_opt_out`) and the relay deletes what it holds.
"""

import contextlib
import sqlite3

import setup_steps

SETTING = "share_progress"
# L8: initial_sent sources of sends Sidedoor made itself (outreach.py, sequence.py, outreach_campaign.py).
# Imported LinkedIn history is "linkedin-browser" (outreach_history_browser.py) and is not counted.
SENT_SOURCES = ("unipile", "heyreach")
LABEL = "Share anonymous progress counts with Your Sidedoor"
# M3 (owner, 2026-11-12): the one line under the box on both pages, saying what the box adds to what
# is sent (`milestones`: three first-reached days) and what it does not. Keep it true to `milestones`.
HELP = ("Shares three dates only: the day you finished setup, the day Sidedoor wrote its first outreach "
        "message and the day it sent its first one. Never your name, messages or job details.")


def enabled(home=None):
    """True only when a saved choice says on (the saved choice alone; what the pages show and the
    heartbeat sends is `shown`)."""
    try:
        return setup_steps.load_state(home).get(SETTING) is True
    except (OSError, ValueError):
        return False


def set_enabled(value, home=None):
    setup_steps.update_state(lambda state: state.__setitem__(SETTING, bool(value)), home)


def _start_pending(state):
    return state["steps"].get("start", {}).get("status") != "done"


def _shown_in(state):
    if SETTING in state:
        return state[SETTING] is True
    return _start_pending(state)


def shown(home=None):
    """What the box shows, on Setup's Start step and on Settings > License alike, and what the
    heartbeat sends (one rule, so the pages and the heartbeat always agree; owner, 2026-11-10): the
    saved choice; before one is saved, on while Start is still pending (what pressing Start then
    saves; also an install re-derived by [Reset setup file], whose Start reads pending again) and off
    once Start is done (an install that started before the box existed never shows it ticked). Start
    done is step 10's own record (`setup_steps.started`): `setup_finished_at` latches once the
    required steps are done, which is before Start is pressed. A setup file that is there but cannot
    be read is not "no choice yet": off (`load_state` reads it as empty, which would show on)."""
    try:
        # One read decides both "can it be read" and "what does it say" (review round 5: two reads
        # left an instant in which a file corrupted between them showed on).
        state, readable = setup_steps._read_state(setup_steps._home(home) / setup_steps.STATE_FILE)
    except (OSError, ValueError):
        return False
    if not readable:
        return False
    state.setdefault("steps", {})
    return _shown_in(state)


def save_shown_if_unchosen(home=None):
    """Pressing Start with the box untouched saves what it showed: one read-and-write inside the state
    lock, so a change the box's own form posts in between is never overwritten. Saves only while no
    choice is saved and Start is still pending (a finished install pressing Start again writes
    nothing: its box shows off, and off is not a choice the user made)."""
    def change(state):
        if SETTING not in state and _start_pending(state):
            state[SETTING] = True
    setup_steps.update_state(change, home)


def _day(text):
    return text[:10] if isinstance(text, str) and len(text) >= 10 and text[4] == "-" and text[7] == "-" else None


def _earliest(read, table, sql):
    """The first day, None when the table does not exist yet (not reached). A failed read raises."""
    if not read.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return None
    row = read.execute(sql).fetchone()
    return _day(row[0]) if row else None


def milestones(home=None):
    """{"setup_finished", "first_drafted", "first_sent"}: first-reached dates or None (not reached).
    A database that cannot be read (locked, broken) leaves its two dates out of this heartbeat, so
    the relay keeps what it has (M2); a missing database means not reached."""
    import paths
    found = {"setup_finished": _day(setup_steps.load_state(home).get("setup_finished_at"))}
    database = paths.database(home=home)
    if not database.is_file():
        return dict(found, first_drafted=None, first_sent=None)
    try:
        with contextlib.closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as read:
            drafted = _earliest(read, "outreach_agent_audit", "SELECT MIN(observed_at) FROM outreach_agent_audit "
                                                              "WHERE phase='COMPOSITION' AND kind='DECISION'")
            sent = _earliest(read, "engagement_events", "SELECT MIN(occurred_at) FROM engagement_events "
                                                        "WHERE kind='initial_sent' AND source IN (%s)"
                             % ",".join("'%s'" % source for source in SENT_SOURCES))
    except sqlite3.Error:
        return found
    return dict(found, first_drafted=drafted, first_sent=sent)


def heartbeat_fields(home=None):
    """What the heartbeat adds for this: {"milestones": ...} while the box shows on (`shown`: the
    saved choice, else on until Start is done); while off the opt-out marker, so the relay deletes
    any dates it holds (L9)."""
    return {"milestones": milestones(home)} if shown(home) else {"milestones_opt_out": True}
