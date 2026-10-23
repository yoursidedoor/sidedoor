"""E3.T4 option A: the pacing check for Unipile invitations (owner, 2026-10-17: "a").

Unipile enforces no limits (PRODUCT-PLAN §6a), so before an INVITE is claimed `check` says whether
it may go now or must wait. It counts our own invitation requests (`attempts[].started_at` of the
INVITE rows `sequence.py` writes on `outreach_actions`); nothing else is stored. A wait is a capacity
wait: the action stays queued for a later cycle, no attempt is spent and no gate fails. Pacing decides
only when, never whether: every claim and gate runs as before. Messages and replies are not paced.

Rules (defaults in `DEFAULTS`, overridable in config["pacing"]):
- caps: at most `daily_cap` requests in any 24 hours and `weekly_cap` in any 7 days;
- warm-up: the daily cap is 20 in the account's first week (counted from its first INVITE request on
  our records), then the full cap (25) from day 7 (owner, 2026-10-22: "make it 20/day starting then
  ramp up after a week"; was 5, 10, 15, 20);
- working hours: off by default (`around_the_clock`, owner 2026-10-23); when off, Monday-Friday,
  `start_hour` to `end_hour` in config["timezone"];
- jitter: each request at least 4-12 minutes (random, fixed per previous request) after the last one.
"""

import datetime as dt
import json
import random
from zoneinfo import ZoneInfo

import sequence


# around_the_clock (owner, 2026-10-23: "All around the clock, if there is available capacity"): sends go
# out any hour, any day, within the caps, warm-up and jitter; False brings back the working-hours window.
DEFAULTS = {"daily_cap": 25, "weekly_cap": 100, "start_hour": 9, "end_hour": 18,
            "jitter_minutes": [4, 12], "warmup_start": 20, "warmup_step": 5, "around_the_clock": True}


def _settings(config):
    values = dict(DEFAULTS)
    values.update((config or {}).get("pacing") or {})
    values["timezone"] = (config or {}).get("timezone") or "America/Los_Angeles"
    return values


def _utc(value):
    moment = dt.datetime.fromisoformat(value) if isinstance(value, str) else value
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.timezone.utc)


def request_times(database):
    """Every INVITE request start on record, oldest first."""
    times = []
    for (result,) in database.execute("""SELECT result FROM outreach_actions
            WHERE json_extract(payload,'$.purpose')=? AND result IS NOT NULL""", (sequence.INVITE,)):
        try:
            attempts = json.loads(result).get("attempts") or []
        except (ValueError, AttributeError):
            continue
        times.extend(_utc(attempt["started_at"]) for attempt in attempts
                     if isinstance(attempt, dict) and attempt.get("started_at"))
    return sorted(times)


def daily_cap(settings, times, now):
    """The warm-up ramp: `warmup_start` in the first week, `warmup_step` more per started week since
    the first request, up to the cap."""
    if not times:
        return min(settings["warmup_start"], settings["daily_cap"])
    weeks = max(0, (now - times[0]).days // 7)
    return min(settings["daily_cap"], settings["warmup_start"] + settings["warmup_step"] * weeks)


def _next_working_start(local, settings):
    day = local.replace(hour=settings["start_hour"], minute=0, second=0, microsecond=0)
    if local.hour >= settings["start_hour"]:
        day += dt.timedelta(days=1)
    while day.weekday() >= 5:
        day += dt.timedelta(days=1)
    return day


def check(database, config, now=None):
    """{"go": True} or {"go": False, "wait_until": <ISO UTC>, "reason": <code>}."""
    settings = _settings(config)
    now = _utc(now or dt.datetime.now(dt.timezone.utc))
    times = request_times(database)
    waits = []
    local = now.astimezone(ZoneInfo(settings["timezone"]))
    if not settings["around_the_clock"] and (local.weekday() >= 5
                                             or not settings["start_hour"] <= local.hour < settings["end_hour"]):
        waits.append((_next_working_start(local, settings).astimezone(dt.timezone.utc), "OUTSIDE_WORKING_HOURS"))
    day = [moment for moment in times if now - moment < dt.timedelta(hours=24)]
    if len(day) >= daily_cap(settings, times, now):
        waits.append((day[len(day) - daily_cap(settings, times, now)] + dt.timedelta(hours=24), "DAILY_CAP"))
    week = [moment for moment in times if now - moment < dt.timedelta(days=7)]
    if len(week) >= settings["weekly_cap"]:
        waits.append((week[len(week) - settings["weekly_cap"]] + dt.timedelta(days=7), "WEEKLY_CAP"))
    if times:
        low, high = settings["jitter_minutes"]
        gap = random.Random(times[-1].isoformat()).uniform(low, high)
        ready = times[-1] + dt.timedelta(minutes=gap)
        if now < ready:
            waits.append((ready, "JITTER"))
    if not waits:
        return {"go": True}
    until, reason = max(waits)
    return {"go": False, "wait_until": until.isoformat(), "reason": reason}
