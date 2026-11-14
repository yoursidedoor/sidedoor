"""The Metrics page (E15.T11, R11): how often a site asked for a sign-in or a human check, and how
each of those ended; in total, by month and by site. Read-only, from stored records, on this
computer only: nothing here is sent anywhere.

The access log IS `application_access_waits` (access_outcomes, E15.T1): one row per time a job was
stopped, with the site, the kind, when, and how it ended. No second table to keep in step.

Which rows count. A row counts when the job met the site's sign-in or check in a session of its own
(`source` does not start with "host:"). A job held behind a site because another job already waits
there (`access_outcomes.host_held`) never met anything: it is in no number. A job stopped twice has
two rows and counts twice. Each counted row is in exactly one of BUCKETS, so they add up to "met":

    sidedoor        `sidedoor_handled`: Sidedoor's own sign-in step got the job through with nothing
                    from the user (`access_step.got_through`: once per wall, in the month the site asked;
                    a wall that needed the user after all is counted by its wait row instead). Those
                    steps are never deleted, so "All time" keeps them with no roll-up of their own. Plus a
                    row ended RESOLVED with an outcome in SIDEDOOR_OUTCOMES (none exists today)
    you             ended RESOLVED with outcome "released" (`access_outcomes.resume`: the user signed
                    in, did the check or confirmed; one sign-in also releases the site's other jobs)
    waiting         state WAITING (on the Needs you card)
    closed_7_days   CLOSED, outcome WAITED_7_DAYS (nobody acted for 7 days)
    you_closed      CLOSED, outcome ABANDONED (the user's own choice)
    skipped         CLOSED, outcome SKIPPED (the user's setting)
    posting_closed  CLOSED, outcome POSTING_CLOSED
    other           everything else: the job finished another way while it waited (RESOLVED
                    "submitted" or "job_finished", CLOSED with another reason's code)

A row is counted in the month the site asked (`since`), in the install's time zone, under how it
stands now. Rows older than 24 months are deleted by the applications round once nothing can need them
(`access_outcomes.trim_history`; owner, 2026-11-13), each counted first into
`access_outcomes.HISTORY_TABLE` (one number per site and ending). `rolled_up` reads that, and "All time"
(the totals and By site: All time) is the rows still stored plus it, so those numbers never go down.
The month rows list only the months whose rows are kept (the last 24, `access_outcomes.history_start`):
an older month could otherwise be listed and then lose rows to the trim. They and "Last 30 days" read
the stored rows only. `could_not_show` (the user's step could not be shown, R12) is a count beside these, not
a bucket: each such failure belongs to a row already counted above.
"""
import datetime as dt
from zoneinfo import ZoneInfo

import access_outcomes
import overview_page as overview
import paths

MONTHS_SHOWN = 12
RECENT_DAYS = 30
UNKNOWN_SITE = "Site not recorded"
# Bucket key -> the plain label shown (page, doctor, Report a problem). No codes, no jargon.
MET_LABEL = "Times a site asked"
BUCKETS = (("sidedoor", "Sidedoor handled"),
           ("you", "You handled"),
           ("waiting", "Still waiting for you"),
           ("closed_7_days", "Closed after 7 days"),
           ("you_closed", "You closed"),
           ("skipped", "Skipped by your setting"),
           ("posting_closed", "Posting closed first"),
           ("other", "Ended another way"))
COULD_NOT_SHOW_LABEL = "Times Sidedoor could not show you the step"
NONE_YET = "No site has asked for a sign-in or a check yet."
USER_OUTCOME = "released"   # what `access_outcomes.resume` writes (`_release`'s default outcome)
# Seam 1a: the outcome values a row gets when Sidedoor itself got a waiting job through (a saved
# session, a saved sign-in, an account it created). No writer exists today, so the set is empty.
SIDEDOOR_OUTCOMES = frozenset()
CLOSED_BUCKETS = {"WAITED_7_DAYS": "closed_7_days", "ABANDONED": "you_closed", "SKIPPED": "skipped",
                  "POSTING_CLOSED": "posting_closed"}


def sidedoor_handled(reader):
    """Seam 1b: every time Sidedoor got past a sign-in with no step from the user and no row in the
    waits table, as [(site, when met)]: the access steps that got through (E15.T8, `access_step`)."""
    import access_step
    try:
        return access_step.got_through(reader.database) if reader.database is not None else []
    except Exception:
        return []


def could_not_show(reader):
    """Seam 2: every time the user's step could not be shown (R12; the live view's failure counts
    join here), as [(site, when)]. Nothing records these today, so: none."""
    return []


def bucket(row):
    """The one bucket of a waits row, or None for a row that is not a site asking this job (held
    behind a site)."""
    if access_outcomes.host_held(row):
        return None
    state, outcome = row["state"], row["outcome"]
    if state == access_outcomes.WAITING:
        return "waiting"
    if state == access_outcomes.RESOLVED:
        if outcome in SIDEDOOR_OUTCOMES:
            return "sidedoor"
        return "you" if outcome == USER_OUTCOME else "other"
    return CLOSED_BUCKETS.get(outcome, "other")


def _empty():
    return dict({key: 0 for key, _ in BUCKETS}, met=0)


def _add(counts, key):
    counts[key] += 1
    counts["met"] += 1


def _local(value, zone):
    moment = overview._utc(value)
    return moment.astimezone(zone) if moment else None


def entries(reader, zone):
    """[(site, local time met or None, bucket)] for every counted row and every seam entry."""
    found = []
    for row in reader.rows(f"SELECT host,source,since,state,outcome FROM {access_outcomes.TABLE} ORDER BY id", (),
                           access_outcomes.TABLE):
        key = bucket(row)
        if key:
            found.append((access_outcomes.site(row["host"]) or UNKNOWN_SITE, _local(row["since"], zone), key))
    found += [(access_outcomes.site(site) or UNKNOWN_SITE, _local(at, zone), "sidedoor") for site, at in sidedoor_handled(reader)]
    return found


def rolled_up(reader):
    """[(site, ending, times)] for the rows the trim has deleted (`access_outcomes.trim_history`): none
    until a first trim (no table). An ending this page no longer has is counted as "other"."""
    known = {key for key, _ in BUCKETS}
    return [(access_outcomes.site(row["site"]) or UNKNOWN_SITE, row["ending"] if row["ending"] in known else "other", int(row["times"]))
            for row in reader.rows(f"SELECT site,ending,times FROM {access_outcomes.HISTORY_TABLE} ORDER BY site,ending", (),
                                   access_outcomes.HISTORY_TABLE)]


def _add_many(counts, key, times):
    counts[key] += times
    counts["met"] += times


def read(reader, config, month=None, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    zone = ZoneInfo(config.get("timezone") or "America/Los_Angeles")
    found = entries(reader, zone)
    total, recent, months = _empty(), _empty(), {}
    edge = now - dt.timedelta(days=RECENT_DAYS)
    kept_from = overview._utc(access_outcomes.history_start(now, zone)).astimezone(zone)   # the oldest month kept
    for _, at, key in found:
        _add(total, key)
        if at is not None:
            if at >= kept_from:
                _add(months.setdefault(at.strftime("%Y-%m"), _empty()), key)
            if at >= edge:
                _add(recent, key)
    shown = sorted(months, reverse=True)[:MONTHS_SHOWN]
    chosen = month if month in shown else None
    sites = {}
    for site, at, key in found:
        if chosen is None or (at is not None and at.strftime("%Y-%m") == chosen):
            _add(sites.setdefault(site, _empty()), key)
    for site, key, times in rolled_up(reader):   # all time only: what the trim deleted, still counted
        _add_many(total, key, times)
        if chosen is None:
            _add_many(sites.setdefault(site, _empty()), key, times)
    label = lambda key: dt.date(int(key[:4]), int(key[5:]), 1).strftime("%B %Y")
    failed = could_not_show(reader)
    return {"total": total, "recent": recent, "recent_days": RECENT_DAYS,
            "months": [{"key": key, "label": label(key), "counts": months[key]} for key in shown],
            "sites": [{"site": site, "counts": counts}
                      for site, counts in sorted(sites.items(), key=lambda item: (-item[1]["met"], item[0]))],
            "chosen": chosen, "chosen_label": label(chosen) if chosen else "All time",
            "could_not_show": len(failed)}


def gather(database, config, month=None, now=None):
    """Everything the page shows. `database` is a read-only connection or None (no file yet). An
    older database without the table reads as nothing met; `unavailable` when the read fails."""
    reader = overview.Reader(database)
    view = overview.section(reader, read, config, month, now)
    return {"unreadable": reader.broken, "unavailable": view == overview.UNAVAILABLE,
            "view": None if view == overview.UNAVAILABLE else view,
            "met_label": MET_LABEL, "buckets": BUCKETS, "could_not_show_label": COULD_NOT_SHOW_LABEL, "none_yet": NONE_YET}


def counts_line(counts):
    """One short line of counts only (no site names): doctor and Report a problem."""
    return "a site asked %d %s (%s)" % (counts["met"], "time" if counts["met"] == 1 else "times", ", ".join("%s %d" % (label, counts[key]) for key, label in BUCKETS))


def support_line(root, config, now=None):
    """The same totals as the page, for `sidedoor doctor` (and so Report a problem): the last
    RECENT_DAYS days and all time. Read-only, like the page."""
    path = paths.database(root)
    found = overview.snapshot(path, lambda database: gather(database, config, now=now),
                              lambda: overview._connect(path.resolve().as_uri() + "?mode=ro"))
    view = found["view"]
    if view is None:
        return None
    if not view["total"]["met"]:
        return NONE_YET
    return "Last %d days: %s. All time: %s." % (RECENT_DAYS, counts_line(view["recent"]), counts_line(view["total"]))


def register(app, templates, layout, *, home, root, config):
    """GET /metrics, behind the dashboard's guard like every page. Reads only."""
    from fastapi import Request

    import dashboard

    @app.get("/metrics", name="metrics")
    def metrics(request: Request, month: str = ""):
        context = layout("metrics", "Metrics")
        context["m"] = overview.snapshot(paths.database(root), lambda database: gather(database, config, month or None),
                                         lambda: dashboard.read_connection(root))
        return templates.TemplateResponse(request, "metrics.html", context)
