"""The user's own step on a site: the seam between the Needs you cards (E15.T2), the live view
(E15.T5) and site sessions (E15.T4), joined in E15.

A card for a job waiting on a sign-in or a human check offers "Sign in" or "Do the human check" only
while this module says the step can be shown (`available`). Pressing it calls `start`, which offers
the job's page to the dashboard's live view and returns the block the card embeds.

How one step runs:
1. `start(job_key, purpose)`: the job must have an open wait. The page is the job's own address as
   Sidedoor stores it and opens every session on (`desktop_adapter.destination`: https only, checked);
   never anything a page or a model reported (the wait's description is not read here). The live view
   is offered that page for the wait's site.
2. The live view starts Sidedoor's browser for that site with the site's saved session in use
   (`live_view.LiveView.session_config`: the browser guard puts the saved cookies back at the start
   and keeps the cookies the site has set when the session ends, exactly as it does for a read).
3. When the session ends, after its guard has stopped and before the browser is let go, the live view
   itself keeps or drops what the guard kept (`live_view.LiveView._settle`): Done makes it the saved
   session of the wait's site only, marked with what the site asked for; any other ending drops it.
4. `_ended`, the live view's `on_ended`, after the browser is free: on Done the wait this step was
   started for is released if it is still open (`access_cards.step_finished`: a sign-in releases every
   job waiting on that site's account, a human check that job only; never a job whose submission may
   have reached the employer). Any other ending leaves the wait and its card exactly as they are.

"Signed in" is the user's own statement: pressing Done. No page text is read and no site is known by
name. Whatever cookies the site set are kept. If the site was in fact not signed in, or the check's
pass does not carry into the next session, the next read meets it again and the card comes back.
The same holds when nothing could be kept (the count is recorded, never a cookie): the wait is still
released, because the user did their part.

Nothing here runs a model, reads a cookie, or logs anything typed.
"""

import asyncio
import contextlib
import functools

import log

LOG = log.get(__name__)
SIGN_IN, HUMAN_CHECK = "sign_in", "human_check"
PURPOSES = (SIGN_IN, HUMAN_CHECK)
FINISHED_EVENT = "ACCESS_STEP_FINISHED"
# What the card embeds: the live view's own fragment, fetched by its id and started at once (the user
# has already pressed the card's button). The page the card is on loads static/live_view.js.
# `hx-target="this"` is load-bearing: this block is put inside the card's `.access-step`, which has an
# hx-target of its own (the whole section, for the redraw after Done), and htmx hands an hx-target down to
# every element inside that has none. Without it the live view replaced the whole section, every other
# card included (found in the real-browser walkthrough, 2026-11-13).
# `card=1`: the view comes without a start button of its own; the card's button is the one control for the step.
FRAGMENT = '<div hx-get="/live-view/%s/fragment?start=1&card=1" hx-trigger="load" hx-target="this" hx-swap="outerHTML"></div>'

_live = None   # the dashboard's live view and how to reach its install; `connect` sets it


def connect(view, database=None, home=None):
    """The dashboard's one live view (`dashboard.create_app`). `database()` opens a write connection to
    the install's database; `home` is the Sidedoor home the site sessions are kept in. None disconnects."""
    global _live
    _live = None if view is None else {"view": view, "database": database, "home": home, "offered": {}}


def available(purpose):
    """Can the user's step for `purpose` be shown on this computer now? Cheap and side-effect free: it
    is called on every render of a card. False where the live view is not supported (Windows today)."""
    live = _live
    if live is None or purpose not in PURPOSES:
        return False
    try:
        return bool(live["view"].available(purpose))
    except Exception:
        return False


def _page(database, job_key):
    """(url, site, wall code, wait id) for a job that has an open wait: the address its sessions open,
    the wait's site, what the site asked for and which wait this is. None when the job is not
    waiting, or its address is not one the live view opens (not https, with credentials or a port,
    or on another site than the one that asked)."""
    import access_outcomes
    import desktop_adapter
    import linkedin_contacts
    import site_sessions
    if job_key == linkedin_contacts.CARD_KEY:
        # The contact finder's card: no job. LinkedIn's own page for what it asks, the finder's site
        # (the session the finder's next read restores), and the run row that is the wait.
        asked = linkedin_contacts.user_wait(database)
        if asked is None:
            return None
        url, wall = linkedin_contacts.USER_STEPS[asked["waiting_on"]]
        return url, linkedin_contacts.SITE, wall, asked["run_id"]
    import confirm_read
    confirming = confirm_read.job_of(job_key)
    if confirming:
        # The confirming read's card (`confirm_read`): the job's own stored address, as for any job;
        # the wait is that module's row, never an access wait.
        job_key, wait = confirming, confirm_read.wait(database, confirming)
    else:
        wait = access_outcomes.open_wait(database, job_key)
    job = database.execute("SELECT payload FROM application_jobs WHERE job_key=?", (job_key,)).fetchone()
    if wait is None or job is None:
        return None
    try:
        url = desktop_adapter.destination({"payload": job[0]})
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    site = site_sessions.site_key(url)
    if not site or site != access_outcomes.site(wait["host"]):
        return None
    return url, site, wait["wall"], wait["id"]


def openable(database, job_key):
    """Can this waiting job's step be opened here? The rule `start` uses, for the card to decide at
    render time whether to show the button (C4)."""
    try:
        return _page(database, job_key) is not None
    except Exception:
        return False


def finishing(job_key):
    """True while a step the user finished for this job is still being recorded (its save is being
    written, its wait about to be released): the step is over, and is not offered again (D22)."""
    live = _live
    try:
        return live is not None and any(job == job_key for job, _ in list(live["view"].finishing))
    except Exception:
        return False


def sync():
    """A step offered for a job that no longer has a card (abandoned, released, skipped, closed after 7
    days or with its posting), or whose card is now for another wait than the one the step was
    offered for (C6), is withdrawn; a view open on it ends as a Cancel. Called after the
    dashboard's own actions and, while a view is open, every few seconds (`_check`). Never raises."""
    import access_cards
    live = _live
    if live is None or not live["offered"] or live["database"] is None:
        return
    try:
        with contextlib.closing(live["database"]()) as database:
            for target, (job_key, wait_id) in list(live["offered"].items()):
                wait = None if live["view"].target(target) is None else _card_wait(database, job_key)
                if live["view"].target(target) is None:
                    live["offered"].pop(target, None)
                elif wait is None or wait["id"] != wait_id:
                    live["offered"].pop(target, None)
                    live["view"].withdraw(target)
    except Exception as error:
        LOG.warning("USER_STEP_NOT_SYNCED (%s)", type(error).__name__)


def _card_wait(database, job_key):
    """The wait a card is for, as {"id": ...}, or None when the card is gone: a job's open wait, or the
    contact finder's waiting run."""
    import access_cards
    import linkedin_contacts
    if job_key == linkedin_contacts.CARD_KEY:
        asked = linkedin_contacts.user_wait(database)
        return {"id": asked["run_id"]} if asked else None
    import confirm_read
    if confirm_read.job_of(job_key):
        return confirm_read.wait(database, confirm_read.job_of(job_key))
    return access_cards._has_card(database, job_key)


async def _check(page):
    """The live view's `check` hook, used only to notice a job that stopped waiting while its view is
    open (`sync`). It reads nothing of the page and never says the step is finished."""
    await asyncio.to_thread(sync)
    return False


def start(job_key, purpose):
    """Show the user's step for one waiting job: {"available": True, "html": <the block the card
    embeds>}, or {"available": False} when it cannot be shown."""
    import access_cards
    import applications
    live = _live
    if not available(purpose) or not isinstance(job_key, str) or live["database"] is None:
        return {"available": False}
    if finishing(job_key):
        return {"available": False, "words": access_cards.GONE}
    try:
        sync()
        with contextlib.closing(live["database"]()) as database:
            if applications.uncertain_submission(database, job_key):
                # It may already be with the employer: Sidedoor is checking, and no step is offered (F2).
                return {"available": False, "words": access_cards.NOT_ABANDONED}
            page = _page(database, job_key)
        if page is None:
            return {"available": False}
        # The step is for THIS wait (C6): Done releases it only, and only while it is still open.
        import linkedin_connection
        import linkedin_contacts
        # The LinkedIn card: Done also connects outreach when that waits on this sign-in (`_ended`), from
        # what is read in the step's own Chrome before it is stopped.
        before_stop = linkedin_connection.capture_if_due if job_key == linkedin_contacts.CARD_KEY else None
        import confirm_read
        target = live["view"].offer(job_key, page[0], purpose, domain=page[1], check=_check, cleared=page[2],
                                    on_ended=functools.partial(_ended, wait_id=page[3]), before_stop=before_stop,
                                    # The confirming read's card has no posting link, and its application may
                                    # already be with the employer: the view never says "apply yourself" there.
                                    # Nor on the LinkedIn card, which has no posting at all.
                                    apply_link=not (confirm_read.job_of(job_key)
                                                    or job_key == linkedin_contacts.CARD_KEY))
        if target:
            live["offered"][target] = (job_key, page[3])
    except Exception as error:
        LOG.warning("USER_STEP_NOT_OFFERED (%s)", type(error).__name__)
        return {"available": False}
    return {"available": True, "html": FRAGMENT % target} if target else {"available": False}


def _ended(ended, live=None, wait_id=None):
    """The live view's `on_ended`: called on a worker thread after the browser has stopped and is free.
    See the module's note for what each ending does. Never raises."""
    import access_cards
    import applications
    import live_view
    live = live or _live
    if live is None or ended.reason not in live_view.STEP_FINISHED:
        return
    # The live view kept or dropped the site's session itself, before it let the browser go (C1).
    saved = bool(getattr(ended, "site_session_saved", False))
    released = []
    try:
        now = applications.stamp()
        with contextlib.closing(live["database"]()) as database:
            released = access_cards.step_finished(database, ended.job, ended.purpose, now, saved, wait_id)
    except Exception as error:
        LOG.warning("USER_STEP_NOT_RELEASED (%s)", type(error).__name__)
    LOG.info("USER_STEP_FINISHED purpose=%s site_session_saved=%s released=%d", ended.purpose, saved, len(released))
    # The one LinkedIn card (owner, 2026-11-13): the same sign-in also connects outreach when that was
    # waiting on the user. Nothing is read or sent for an install whose outreach is connected.
    import linkedin_contacts
    if ended.job == linkedin_contacts.CARD_KEY:
        import linkedin_connection
        linkedin_connection.after_linkedin_step()
