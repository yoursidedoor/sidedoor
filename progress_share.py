"""E12.T2 (owner pick 2026-11-05, option a): opt-in progress counts sent with the daily license
heartbeat (`license_clock._check`).

Settings > License has the checkbox "Share anonymous progress counts with Your Sidedoor", off by
default; the choice is `share_progress` in setup-state.json. While it is on, the heartbeat adds
`milestones` = {"setup_finished", "first_drafted", "first_sent"}, each the first day it happened
("YYYY-MM-DD", UTC) or None. While it is off, only the opt-out marker is added. The relay keeps only these three
dates (relay/milestones.py). Each is read from what Sidedoor already records; nothing new is tracked:
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


def enabled(home=None):
    """True only when the user turned it on (off by default, and off when the file can't be read)."""
    try:
        return setup_steps.load_state(home).get(SETTING) is True
    except (OSError, ValueError):
        return False


def set_enabled(value, home=None):
    setup_steps.update_state(lambda state: state.__setitem__(SETTING, bool(value)), home)


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
    """What the heartbeat adds for this: {"milestones": ...} while on; while off the opt-out marker,
    so the relay deletes any dates it holds (L9)."""
    return {"milestones": milestones(home)} if enabled(home) else {"milestones_opt_out": True}
