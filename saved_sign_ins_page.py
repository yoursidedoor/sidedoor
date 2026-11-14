"""Saved sign-ins (E15.T7, R8): the page behind the link under Settings > "When a site needs an account".

One row per site Sidedoor holds a sign-in for (`saved_sign_ins.listing`): the site, the sign-in email, how
it was made, when it was last used, and Remove; Remove all above the list. A row whose password the
90-day rule removed stays and says so. No password is ever on this page or in its answers.
Show password asks for the computer's own unlock on every click (`device_unlock`); the value is in that one
answer only (never logged, `Cache-Control: no-store`), and Hide password loads the page without it. Where
the computer cannot confirm the person, no Show button is drawn and the page says so.
Every sentence the page shows is in this module or its template.
"""

import datetime as dt

import device_unlock
import saved_sign_ins


PATH = "/settings/saved-sign-ins"
TITLE = "Saved sign-ins"
INTRO = ("These are the job sites Sidedoor can sign in to for you. "
         "The passwords are kept encrypted on this computer.")
NONE_YET = "No saved sign-ins yet."
UNAVAILABLE = "Sidedoor can't read your saved sign-ins right now."
MADE_BY = {saved_sign_ins.USER: "You signed in", saved_sign_ins.SIDEDOOR: "Sidedoor created it"}
PASSWORD_REMOVED = "Password removed"
REMOVE_CONFIRM = "Remove the saved sign-in for %s?"
REMOVE_ALL_CONFIRM = "Remove every saved sign-in from this computer?"
REMOVED_ONE = "Removed the saved sign-in for %s."
REMOVED_ALL = "Removed every saved sign-in."
NOTHING_REMOVED = "There was nothing to remove."
NO_UNLOCK = "This computer can't confirm it's you, so Sidedoor can't show passwords here."
NOT_UNLOCKED = "The password stays hidden because the computer's unlock wasn't completed."
NOT_SHOWN = "There is no password to show for that site."


def _day(stamp):
    try:
        return dt.datetime.fromtimestamp(stamp).strftime("%b %d, %Y") if stamp else None
    except (OverflowError, OSError, ValueError):
        return None


def view(home=None, message=None, shown=None, warning=None):
    """`shown`: (site, password) for the one row whose password this answer shows, after the unlock."""
    found = saved_sign_ins.listing(home)
    can_show = device_unlock.available()
    rows = [{"site": row["site"], "email": row["email"], "made_by": MADE_BY.get(row["made_by"], ""),
             "used": _day(row["used_at"]), "password_removed": not row["has_password"],
             "password": shown[1] if shown and shown[0] == row["site"] and row["has_password"] else None,
             "can_show": can_show and row["has_password"],
             "confirm": REMOVE_CONFIRM % row["site"]} for row in found["rows"]]
    return {"title": TITLE, "intro": INTRO, "rows": rows, "none_yet": NONE_YET, "path": PATH, "message": message,
            "warning": warning, "no_unlock": NO_UNLOCK if not can_show and any(row["has_password"] for row in found["rows"]) else None,
            "unavailable": UNAVAILABLE if found["unavailable"] else None,
            "password_removed": PASSWORD_REMOVED, "remove_all_confirm": REMOVE_ALL_CONFIRM}


def register(app, templates, layout, *, home):
    """GET the page; POST Remove and Remove all. Behind the dashboard's guard like every page."""
    from fastapi import Request
    from starlette.concurrency import run_in_threadpool

    def page(request, message=None, shown=None, warning=None):
        context = layout("settings", TITLE)
        context["s"] = view(home, message, shown, warning)
        response = templates.TemplateResponse(request, "saved_sign_ins.html", context)
        if shown:
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.post(PATH + "/show")
    async def show(request: Request):
        """Show password: the computer's own unlock on every click, then the value in this answer only."""
        site = (await request.form()).get("site")
        if not isinstance(site, str) or not saved_sign_ins.has(site, home):
            return page(request, warning=NOT_SHOWN)
        if not device_unlock.available():
            return page(request)
        if not await run_in_threadpool(device_unlock.verify):
            return page(request, warning=NOT_UNLOCKED)
        password = saved_sign_ins.reveal(site, home)
        return page(request, shown=(site, password)) if password else page(request, warning=NOT_SHOWN)

    @app.get(PATH, name="saved_sign_ins")
    def saved(request: Request):
        return page(request)

    @app.post(PATH + "/remove")
    async def remove(request: Request):
        site = (await request.form()).get("site")
        known = isinstance(site, str) and any(row["site"] == site for row in saved_sign_ins.listing(home)["rows"])
        removed = await run_in_threadpool(saved_sign_ins.remove, site, home) if known else 0
        return page(request, REMOVED_ONE % site if removed else NOTHING_REMOVED)

    @app.post(PATH + "/remove-all")
    async def remove_all(request: Request):
        removed = await run_in_threadpool(saved_sign_ins.remove_all, home)
        return page(request, REMOVED_ALL if removed else NOTHING_REMOVED)
