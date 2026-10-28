"""The local dashboard shell (E6.T1, option A: FastAPI + Jinja2 + htmx, server-rendered; owner, 2026-10-21).

`sidedoor dashboard` and `sidedoor setup` (orchestrator.main) call `serve`: it binds 127.0.0.1 on
the one fixed port 47823 (owner, 2026-10-21: "Lets just bind one port to the dashboard"), prints
`http://127.0.0.1:47823/?t=<token>` (`/setup?t=` for setup), opens it on macOS and Windows, and
serves the pages until stopped. When the port is taken, `serve` asks the program on it to prove it
is this install's dashboard (`/_sidedoor/ping`: an HMAC of a fresh nonce under the token, so the
token itself is never sent): if it is, that dashboard is reopened instead of starting a second;
otherwise it stops with the plain "Port 47823 is in use by another program" message.

Pages: the Setup page (E4.T1 option C; E6.T0 spec 3.2, steps in `setup_steps`), Overview's "Sidedoor
is not running" with [Start] once setup is finished (E6.T0 section 5), and placeholders for the
rest (E6.T3 to E6.T8) in the shared layout (sections 1a and 2).

Security (E6.T1 "Common to all three"), all in `guard`, in this order, on every request:

1. Host must be `127.0.0.1:<port>` or `localhost:<port>` (DNS rebinding), else 400.
2. A state-changing method (anything but GET, HEAD, OPTIONS) needs an Origin of
   `http://127.0.0.1:<port>` or `http://localhost:<port>`; missing or other, 403.
3. A request carrying `?t=` is the tokened link: the right token sets the session cookie
   (HttpOnly, SameSite=Strict, path /, persistent: it lasts until the install token changes, owner
   2026-10-21) and redirects (303) to the same path without the query; a wrong one is 403. Every
   other request needs that cookie, else 403, except the GET-only `/_sidedoor/ping` and
   `/_sidedoor/scheduler` (running or stopped, for `sidedoor doctor`; HMAC-signed, no token sent).
4. Every response gets a Content-Security-Policy with no inline script (`script-src 'self'`) and
   `frame-ancestors 'none'`, X-Frame-Options DENY and nosniff; everything except static files is
   `Cache-Control: no-store`.

The token is per install, `dashboard-token` in the Sidedoor home (user_profile.home), mode 0600.
Uvicorn's access log is off: it would write the tokened URL to the terminal.

Database: pages read through `read_connection` (a `mode=ro` URI; never orchestrator.connect,
which runs the schema script). Writes, when a later page needs one, call the existing module
functions on `write_connection` (run_locks.WriteFirstConnection, 10 s busy timeout); a database
still locked after that answers 503 "Busy, try again" instead of hanging the page.
"""

import resources
import errno
import hashlib
import hmac
import html
import http.client
import json
import os
import secrets
import signal
import socket
import sqlite3
import sys
import threading
import webbrowser
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

import login_item
import log
import overview_page as overview_data  # the module; `overview_page` is the route below
import run_locks
import scheduler_control
import scheduler_loop
import settings_page
import setup_steps
import tray as tray_icon
import user_profile
import version
import paths

LOG = log.get(__name__)


ASSETS = resources.path("dashboard_assets")
BRAND = resources.path("assets/brand")
DATABASE = Path(paths.STATE) / paths.DATABASE
TOKEN_FILE = "dashboard-token"
MODE_ERROR = "Choose Automatic or Review."
COOKIE = "sidedoor_session"
PORT = 47823
PORT_IN_USE = "Port 47823 is in use by another program. Close it and open Sidedoor again."
PING_PATH = "/_sidedoor/ping"
# `sidedoor doctor` (E4.T4, UD4) asks the open dashboard whether its scheduler loop runs: GET with a
# fresh nonce; the answer is "running" or "stopped" plus the HMAC of nonce and state under the token.
SCHEDULER_PATH = "/_sidedoor/scheduler"
# `sidedoor serve --start` finding this install's dashboard already open asks it to start its
# scheduler loop: POST with a fresh nonce and the HMAC of it under the token (the token is never
# sent); a nonce is accepted once.
START_PATH = "/_sidedoor/start"
# Persistent sign-in (owner, 2026-10-21, "Sure"): the cookie is the install token, so it stops
# working when the token changes; the browser keeps it this long (ten years).
COOKIE_MAX_AGE = 10 * 365 * 24 * 3600
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
STATIC_PREFIXES = ("/static/", "/brand/")


def asset_url(path):
    """A /static/ or /brand/ URL with ?v=<installed version>, so an update that changes a logo, icon,
    stylesheet or script reaches browsers that cached the old file under the same path."""
    return "%s?v=%s" % (path, version.current())
CONTENT_SECURITY_POLICY = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
                           "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
                           "form-action 'self'; frame-ancestors 'none'")
# The E6 pages (E6.T0 section 2): key, tab label, path. Setup joins while setup is unfinished (E4.T1).
TABS = (("overview", "Overview", "/"),
        ("approvals", "Approvals", "/approvals"),
        ("sent", "Networking & Referrals", "/sent"),
        ("applications", "Applications", "/applications"),
        ("failures", "Failures and logs", "/failures"),
        ("settings", "Settings", "/settings"))
SETUP_TAB = ("setup", "Setup", "/setup")


def token(home=None):
    """The per-install dashboard token, created on first use in the Sidedoor home with mode 0600."""
    home = Path(home) if home is not None else user_profile.home()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = home / TOKEN_FILE
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        os.chmod(path, 0o600)
        value = path.read_text().strip()
        if value:
            return value
        descriptor = os.open(path, os.O_WRONLY | os.O_TRUNC)
    value = secrets.token_urlsafe(32)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(value + "\n")
    return value


def read_connection(root=None):
    """A read-only connection to the execution database, for page reads."""
    path = paths.database(root).resolve()
    database = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True,
                               timeout=run_locks.SESSION_TOOL_BUSY_TIMEOUT_SECONDS)
    database.row_factory = sqlite3.Row
    return database


def write_connection(root=None):
    """A write connection for existing module functions; waits at most 10 s for the write lock."""
    database = sqlite3.connect(paths.database(root), timeout=run_locks.SESSION_TOOL_BUSY_TIMEOUT_SECONDS,
                               factory=run_locks.WriteFirstConnection)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA synchronous=FULL")
    return database


def sync_license_pause(root=None, home=None):
    """E8.T3: after a license action (a check, an activation, a new key) the license pause follows
    the stored license at once, so a renewal resumes work without waiting for the next heartbeat
    step. A failure here (e.g. a busy database) leaves it to that step."""
    try:
        scheduler_loop.sync_license_pause(root, home)
    except Exception:
        pass


def set_user_pause(root, on, reason):
    """Pause and Resume (E6.T0 section 5; E5.T1): the user's pause flag, for Overview's buttons and
    the tray's (E5.T3), so the two never disagree. Nothing new starts while it is set; work already
    running finishes on its own (no signal is sent)."""
    paths.state(root).mkdir(exist_ok=True, mode=0o700)
    database = write_connection(root)
    try:
        scheduler_control.set_user_pause(database, on, reason=reason if on else None)
    finally:
        database.close()


def _refuse(status, text):
    return PlainTextResponse(text, status_code=status)


def ping_answer(secret, nonce):
    """What this install's dashboard answers to `/_sidedoor/ping?n=<nonce>`; proves the token
    without sending it."""
    return hmac.new(secret.encode(), b"sidedoor-ping:" + nonce.encode(), hashlib.sha256).hexdigest()


def scheduler_answer(secret, nonce, state):
    """What this install's dashboard answers to `/_sidedoor/scheduler?n=<nonce>` (state: running or stopped)."""
    return state + " " + hmac.new(secret.encode(), b"sidedoor-scheduler:" + nonce.encode() + b":" + state.encode(),
                                  hashlib.sha256).hexdigest()


def start_proof(secret, nonce):
    """What `sidedoor serve --start` sends with `/_sidedoor/start` to prove the token."""
    return hmac.new(secret.encode(), b"sidedoor-start:" + nonce.encode(), hashlib.sha256).hexdigest()


def load_config(root=None):
    try:
        return resources.read_json("config.json")
    except (OSError, ValueError):
        return {}


# Steps whose lock or result changes when another step changes; re-sent out of band with each step.
DEPENDENT_STEPS = (2, 7, 9, 11)
STATUS = {"reading": ("Checking", "info"), "done": ("Done", "success"), "failed": ("Failed", "error"), "needs_you": ("Needs you", "warning"),
          "checking": ("Checking", "info"), setup_steps.NOT_BUILT: ("Not built yet", "info"),
          "not_started": ("Not started", "muted")}
STEP_KEYS = {number: key for number, key, _ in setup_steps.STEPS}
STEP_TITLES = {number: title for number, _, title in setup_steps.STEPS}
GUIDES = ("openrouter", "codex", "claude", "antigravity", "linkedin", "browser", "chrome", "license")


def create_app(secret, port, home=None, root=None, config=None, browser=None, relay_url=setup_steps.RELAY_URL):
    root = paths.data(root, home)  # the Sidedoor home unless a test gives its own folder (E9.T1)
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    allowed_origins = {"http://" + host for host in allowed_hosts}
    config = load_config(root) if config is None else config
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=ASSETS / "templates")
    templates.env.globals["asset_url"] = asset_url
    app.mount("/static", StaticFiles(directory=ASSETS / "static"), name="static")
    app.mount("/brand", StaticFiles(directory=BRAND), name="brand")
    if browser is None:
        browser = setup_steps.BrowserSession(
            root, on_closed=lambda: setup_steps.browser_closed(home))
    app.state.browser = browser
    app.state.data_move_error = None

    used_nonces = set()

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.headers.get("host") not in allowed_hosts:
            response = _refuse(400, "Unknown host.")
        elif request.method not in SAFE_METHODS and request.headers.get("origin") not in allowed_origins:
            response = _refuse(403, "Cross-site request refused.")
        elif request.url.path == START_PATH and request.method == "POST":
            nonce, proof = request.query_params.get("n", ""), request.query_params.get("p", "")
            if not (16 <= len(nonce) <= 128 and nonce.isalnum() and nonce not in used_nonces
                    and hmac.compare_digest(proof.encode(), start_proof(secret, nonce).encode())):
                response = _refuse(403, "Refused.")
            else:
                used_nonces.add(nonce)
                started = await run_in_threadpool(scheduler_loop.start)
                response = (PlainTextResponse("started") if started
                            else _refuse(409, "This Sidedoor has no scheduler to start."))
        elif request.url.path == SCHEDULER_PATH and request.method == "GET":
            nonce = request.query_params.get("n", "")
            state = "running" if scheduler_loop.running() else "stopped"   # what setup_steps.scheduler_running reads
            response = (PlainTextResponse(scheduler_answer(secret, nonce, state)) if 16 <= len(nonce) <= 128
                        and nonce.isalnum() else _refuse(400, "Bad nonce."))
        elif request.url.path == PING_PATH and request.method == "GET":
            nonce = request.query_params.get("n", "")
            response = (PlainTextResponse(ping_answer(secret, nonce)) if 16 <= len(nonce) <= 128 and nonce.isalnum()
                        else _refuse(400, "Bad nonce."))
        elif "t" in request.query_params:
            if hmac.compare_digest(request.query_params["t"].encode(), secret.encode()):
                response = RedirectResponse(request.url.path, status_code=303)
                response.set_cookie(COOKIE, secret, max_age=COOKIE_MAX_AGE, httponly=True, samesite="strict", path="/")
            else:
                response = _refuse(403, "This link is not valid. Open the dashboard from the link Sidedoor printed.")
        elif not hmac.compare_digest(request.cookies.get(COOKIE, "").encode(), secret.encode()):
            response = _refuse(403, "Open the dashboard from the link Sidedoor printed.")
        else:
            response = await call_next(request)
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        # same-origin, not no-referrer (2026-10-24): under no-referrer a browser sends "Origin: null" on a
        # plain form POST, which the cross-site check above refuses ("Update now" was refused). same-origin
        # keeps the real Origin on our own requests and still sends no referrer to any other site.
        response.headers["Referrer-Policy"] = "same-origin"
        if not request.url.path.startswith(STATIC_PREFIXES):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(setup_steps.StateFileUnreadable)
    def unreadable_state(request: Request, error: setup_steps.StateFileUnreadable):
        # GL2: said plainly, the file is left as it is. GL-M2: not a dead end; htmx follows
        # HX-Redirect whatever the status, a plain request gets the page with [Reset setup file].
        if request.headers.get("hx-request") == "true":
            response = _refuse(409, str(error))
            response.headers["HX-Redirect"] = "/setup-file"
            return response
        return setup_file_page(request, message=str(error), status_code=409)

    # --- [Reset setup file] (GL-M2) ------------------------------------------------------------------

    STEP_NAMES = {key: name for _number, key, name in setup_steps.STEPS}
    RECHECK = ("linkedin", "browser", "email", "login", "start")

    def setup_file_page(request, message=None, result=None, status_code=200):
        context = layout("settings", "Setup file")
        broken = setup_steps.state_unreadable(home)
        if result and result.get("reset"):
            done = set(result["rederived"]) | ({"email"} if "locked_email" in result["rederived"] else set())
            found = [STEP_NAMES[key] for key in result["rederived"] if key in STEP_NAMES]
            result = dict(result, found=found, recheck=[STEP_NAMES[key] for key in RECHECK if key not in done],
                          email_kept=setup_steps.load_state(home).get("locked_email")
                          if "locked_email" in result["rederived"] else None)
        elif result and result.get("error"):
            message = result["error"]
        context.update(broken=broken, result=result, message=message or (
            "Sidedoor could not read its setup file (%s)." % (setup_steps._home(home) / setup_steps.STATE_FILE)))
        return templates.TemplateResponse(request, "setup_file.html", context, status_code=status_code)

    @app.get("/setup-file")
    def setup_file(request: Request):
        return setup_file_page(request)

    @app.post("/setup-file/reset")
    def setup_file_reset(request: Request):
        return setup_file_page(request, result=setup_steps.reset_state(home, root))

    @app.exception_handler(sqlite3.OperationalError)
    def busy(request: Request, error: sqlite3.OperationalError):
        if "locked" in str(error) or "busy" in str(error):
            return _refuse(503, "Busy, try again.")
        raise error

    # --- shared layout context -------------------------------------------------------------------

    def layout(active, title, progress=None):
        progress = progress or setup_steps.progress(home)
        state = progress["state"]
        tabs = TABS if progress["finished"] else TABS + (SETUP_TAB,)
        # The mode as the send path reads it (E6.T2 review fix P13), not the file alone.
        import approvals
        mode = {"label": "Review" if approvals.install_mode(root, home) == approvals.REVIEW else "Automatic", "tone": "info"}
        banners = []
        if setup_steps.state_unreadable(home):
            banners.append({"tone": "error", "text": "Sidedoor couldn't read its setup file, so it can't save setup changes.",
                            "action_label": "Reset setup file", "action_href": "/setup-file"})
        email = state["steps"].get("email", {})
        if email.get("mismatch"):
            banners.append({"tone": "error", "text": "Sidedoor's browser is signed in to a different mailbox. Sign in to %s "
                            "in Sidedoor's browser." % state.get("locked_email", ""),
                            "action_label": "Open Sidedoor's browser",
                            # After setup is finished the fix lives in Settings, never back in Setup.
                            "action_href": "/settings#connections" if progress["finished"] else "/setup#step-8"})
        if progress["finished"]:
            # Once setup is finished the Setup tab stays hidden for good (owner, 2026-10-21); a step
            # that breaks later shows banner 6 pointing to Settings (Settings > Connections is E6.T8).
            broken = []
            import provider_limits
            # U1 3g G1: the chosen engine; while its own wait banner shows, that banner says so (one banner)
            engine_waits = provider_limits.waits(home, finished=True)
            if (not progress["done"][6] and not engine_waits["codex_missing"] and not engine_waits["claude"]
                    and not engine_waits.get("antigravity")):
                broken.append(setup_steps.ENGINE_LABELS[setup_steps.chosen_engine(state)])
            if not setup_steps.chrome_path():
                broken.append("Google Chrome")
            for name in broken:
                banners.append({"tone": "error", "text": "%s stopped working." % name,
                                "action_label": "Fix", "action_href": "/settings#connections"})
        else:
            left = progress["left"]
            banners.append({"tone": "warning", "text": "Setup is not finished: %d step%s left." % (left, "" if left == 1 else "s"),
                            "action_label": "Continue setup", "action_href": "/setup"})
        misplaced = login_item.app_location_problem()
        if misplaced:  # E9.T1 AP4: start at login is never registered or refreshed from this copy
            banners.append({"tone": "warning", "text": misplaced})
        notice = login_item.take_notice(Path(home) if home is not None else user_profile.home())
        if notice:  # why a login launch gave up; shown once, by whichever page renders first
            banners.append({"tone": "warning", "text": notice,
                            "action_label": "Start at login", "action_href": "/settings#login"})
        import openai_auth  # E0.T8: "Your OpenAI API key is out of credit" (and rate limit, refused key)
        # U1 3g G2: the OpenAI key's banners are Codex's: hidden (kept) while another engine is chosen
        api_banner = openai_auth.banner(home) if setup_steps.chosen_engine(state) == "codex" else None
        if api_banner:
            if not progress["finished"]:
                api_banner = dict(api_banner, action_href="/setup#step-6")
            banners.append(api_banner)
        import provider_limits  # out of OpenRouter credits; the ChatGPT usage limit (both clear by themselves)
        banners.extend(provider_limits.banners(home, finished=progress["finished"]))
        moving = data_move_banner(home, app.state.data_move_error)
        if moving:
            banners.insert(0, moving)  # E9.T1: before anything else, so no one acts on an empty home
        import license_clock  # E8.T2: one read of the stored license state for the chip and banners 1/1b/2/3
        license_view = license_clock.view(home)
        license_banner = license_clock.banner(license_view)
        if license_banner:
            banners.insert(0, license_banner)  # most urgent first (E6.T0 section 2)
        import credentials  # E2.T4 (R3): a locked keychain holds every job; first of all (a locked
        # keychain also reads as "no license" below it)
        hold = credentials.hold_reason(home)
        if hold:
            banners.insert(0, {"tone": "error", "text": hold, "action_label": "Check again",
                                "action_href": {key: href for key, _, href in TABS + (SETUP_TAB,)}.get(active, "/")})
        import update_check  # E9.T3: a newer Sidedoor, last of all (license problems come first)
        update_banner = update_check.banner(home)
        if update_banner:
            banners.append(update_banner)
        license_chip = {"label": license_view["label"], "tone": license_view["tone"], "href": license_view["href"]}
        if progress["finished"] and license_chip["href"].startswith("/setup"):
            license_chip["href"] = "/settings#license"  # never back into Setup once it is finished
        return {"tabs": tabs, "active": active, "title": title, "mode": mode,
                "license": license_chip, "banners": banners}

    # Approvals (E6.T4): registered before the placeholders, so its GET /approvals is the one that answers.
    import approvals_page
    approvals_page.register(app, templates, layout, home=home, root=root, config=config)

    # Networking & Referrals (E6.T5; E6.T0 section 7): registered before the placeholders, so its GET /sent answers.
    import networking_page
    networking_page.register(app, templates, layout, home=home, root=root, config=config)

    def page(key):
        title = next(label for name, label, _ in TABS if name == key)

        def handler(request: Request):
            return templates.TemplateResponse(request, "placeholder.html", layout(key, title))
        return handler

    for key, _, path in TABS[1:]:
        if key not in ("applications", "failures"):
            app.add_api_route(path, page(key), methods=["GET"], name=key)

    # Failures and logs (E6.T7; E6.T0 section 9): read-only, like Overview (failures_page).
    import failures_page
    failures_page.register(app, templates, layout, home=home, root=root, config=config, token=secret,
                           database=paths.database(root), connect_read_only=lambda: read_connection(root))
    import report_problem  # Report a problem (owner pick F3 a): POST /report-problem, behind `guard`
    report_problem.register(app, home=home, root=root, config=config, token=secret)
    import support_email  # Email support (owner, 2026-10-24): POST/GET /report-problem/email, behind `guard`
    support_email.register(app, home=home, root=root, config=config, token=secret)
    import doctor  # Failures and logs > Run checks (E4.T4): the `sidedoor doctor` report
    doctor.register(app, templates, layout, home=home, root=root, config=config)

    # Applications (E6.T6; E6.T0 section 8): read-only, from stored records like Overview, and
    # nothing written, not even SQLite's side files (overview_page.snapshot).
    @app.get("/applications", name="applications")
    def applications_list(request: Request, show: str = "all", page: str = "1", company: str = "", q: str = "",
                          job: str = ""):
        import applications_page
        context = layout("applications", "Applications")
        context["view"] = overview_data.snapshot(
            paths.database(root),
            lambda database: applications_page.gather(database, config, home, show, page, company, q, job or None),
            lambda: read_connection(root))
        return templates.TemplateResponse(request, "applications.html", context)

    # --- Overview (E6.T3; E6.T0 section 5): "not running" with [Start], then the day's numbers ------

    def overview(request, message=None, message_at="health"):
        progress = setup_steps.progress(home)
        context = layout("overview", "Overview", progress)
        running = setup_steps.scheduler_running(home)
        context.update(finished=progress["finished"], running=running, message=message, message_at=message_at, view=None)
        import version  # owner, 2026-10-23: "the overview page should show what version of code is installed."
        context["sidedoor_version"] = version.current()
        import update_check  # owner, 2026-10-24: the chip says "· X available" while one is offered
        context["update_offer"] = update_check.offer(home)
        import license_clock
        license = license_clock.view(home)
        # E8.T3 (E6.T0 section 11): Expired shows a full-page paywall on Overview with [Renew]; the
        # other pages stay readable. Grace spent and a 403 show their banner only, no paywall.
        context["paywall"] = license if license["state"] == "expired" else None
        if context["paywall"]:
            # PW1: [Pause] stays available while Expired, and the user's own pause still shows.
            context["paywall_pause"] = overview_data.snapshot(
                paths.database(root), lambda database: overview_data.paywall_pause(database, config),
                lambda: read_connection(root))
            return templates.TemplateResponse(request, "overview.html", context)
        if progress["finished"]:
            # E6.T3: display only, from stored records (overview_page); no network or model call,
            # and nothing written, not even SQLite's side files (overview_page.snapshot).
            context["view"] = overview_data.snapshot(
                paths.database(root),
                lambda database: overview_data.gather(database, config, home, progress, overview_data.loop_state(running),
                                                    root=root),
                lambda: read_connection(root))
        return templates.TemplateResponse(request, "overview.html", context)

    @app.get("/", name="overview")
    def overview_page(request: Request):
        return overview(request)

    @app.post("/start")
    async def start(request: Request):
        form = await request.form()
        # The result shows next to the [Start] that was clicked: the top card or the Health item.
        at = "health" if form.get("at") == "health" else "card"
        state = await run_in_threadpool(setup_steps.start_scheduler, home)
        running = await run_in_threadpool(setup_steps.scheduler_running, home)
        return overview(request, overview_data.start_message(state["steps"].get("start", {}), running), at)

    def set_pause(on):
        set_user_pause(root, on, "dashboard")

    @app.post("/pause")
    def pause(request: Request):
        set_pause(True)
        return overview(request, "Paused. Work already running finishes; nothing new starts until you resume.")

    @app.post("/resume")
    def resume_work(request: Request):
        set_pause(False)
        return overview(request, "Resumed.")

    # [Check again] (E8.T2; banner 1b, the clock problem, Overview's License health, Settings >
    # License): one heartbeat now unless one is running or ran in the last minute (never queued).
    # htmx gets the result in place and the chip out of band; a plain form goes back to its page.
    pages = {path for _, _, path in TABS} | {SETUP_TAB[2]}

    @app.post("/license/check")
    async def license_check(request: Request):
        import license_clock
        form = await request.form()
        result = await run_in_threadpool(license_clock.check_now, home, relay_url)
        await run_in_threadpool(sync_license_pause, root, home)  # E8.T3: a good check clears it at once
        if request.headers.get("hx-request") == "true":
            context = layout("overview", "Overview")
            view = license_clock.view(home)
            message = license_clock.check_message(result, view)
            return HTMLResponse('<span class="license-check-message" role="status">%s</span>' % html.escape(message)
                                + templates.get_template("chips.html").render(mode=context["mode"],
                                                                              license=context["license"], oob=True))
        back = form.get("next") if isinstance(form.get("next"), str) else "/"
        return RedirectResponse(back if back in pages else "/", status_code=303)

    # [Move my data now] on the data-move banner (E9.T1): sets the wrong copy aside (renamed, never
    # deleted) and finishes the move, only while no job runs (paths.resolve).
    @app.post("/data/move")
    async def data_move(request: Request):
        form = await request.form()
        keep = form.get("keep") if form.get("keep") in (paths.HOME, paths.CODE_SIDE) else None
        result = await run_in_threadpool(paths.resolve, home, None, keep)
        app.state.data_move_error = result.get("error")
        back = form.get("next") if isinstance(form.get("next"), str) else "/"
        return RedirectResponse(back if _known_page(back) else "/", status_code=303)

    # [Dismiss] on the update banner (E9.T3): hidden until a newer version appears.
    @app.post("/update/dismiss")
    async def update_dismiss(request: Request):
        import update_check
        form = await request.form()
        version = form.get("version")
        # One Dismiss (owner, 2026-10-24): a failed Mac update's message and, when sent, the offer too.
        if form.get("notice") == "1":
            await run_in_threadpool(update_check.dismiss_notice, home)
        if isinstance(version, str):
            await run_in_threadpool(update_check.dismiss, version, home)
        back = form.get("next") if isinstance(form.get("next"), str) else "/"
        return RedirectResponse(back if _known_page(back) else "/", status_code=303)

    # [Update now] on the update banner (owner, 2026-10-23, B; the built Mac app only): downloads,
    # checks and, after the user clicks Replace in a native dialog, swaps the app (mac_update).
    @app.post("/update/install")
    async def update_install(request: Request):
        import mac_update
        form = await request.form()
        version = form.get("version")
        if isinstance(version, str) and mac_update.can_install():
            await run_in_threadpool(mac_update.start_install, version, home if home is not None else user_profile.home())
        back = form.get("next") if isinstance(form.get("next"), str) else "/"
        return RedirectResponse(back if _known_page(back) else "/", status_code=303)

    # The update banner alone, polled by itself while [Update now] runs (banner_item.html `poll`),
    # so its end (a failed check, Not now, the reason) shows without a reload. Read-only.
    @app.get("/update/banner")
    def update_banner_fragment(request: Request, next: str = "/"):
        import update_check
        found = update_check.banner(home)
        if found is None:
            return HTMLResponse("")
        return HTMLResponse(templates.get_template("banner_item.html").render(
            banner=found, back=next if _known_page(next) else "/"))

    def _known_page(path):
        """A tab, or a sub-path of one (e.g. /setup/guide/x), never another site or a traversal."""
        if path in pages:
            return True
        if len(path) > 200 or "//" in path or ".." in path or not all(c.isalnum() or c in "/_.-" for c in path):
            return False
        return any(path.startswith(page + "/") for page in pages if page != "/")

    # --- Setup (E6.T0 3.2) ------------------------------------------------------------------------

    def step_view(number, progress, extra=None):
        state = progress["state"]
        key = STEP_KEYS[number]
        engine = setup_steps.chosen_engine(state)
        # U1 3g: step 6 is the chosen engine's (its record is kept under the engine's own name)
        saved = state["steps"].get(engine if number == 6 and engine in ("claude", "antigravity") else key, {})
        status = saved.get("status", "not_started")
        message = saved.get("message")
        if number == 4:
            status, message = "done", None
            import approvals  # the mode as the send path reads it (E6.T2 P13)
            state = dict(state, mode=approvals.install_mode(root, home))
        elif number == 5:
            status = "done" if progress["done"][5] else ("failed" if status == "failed" else "not_started")
            import license_clock  # LA4: a saved message with a raw UTC time shows it in local time
            message = license_clock.local_message(saved, config).get("message")
        elif number == 6 and status == "done" and not progress["done"][6]:
            status, message = "failed", "%s is not installed." % setup_steps.ENGINE_LABELS[engine]
        elif number == 8:
            status = "done" if progress["done"][8] else ("failed" if status == "failed" else status)
            if status == "done" and not state.get("webmail"):
                status = "needs_you"
        elif number == 9 and progress["done"][9]:
            status = "done"
        elif number == 10:  # the real registration, not the saved record (E5.T2)
            login = setup_steps.login_view(saved, home)
            status, message = login["status"], login["message"]
            extra = dict(extra or {}, login_on=login["on"] or login.get("default", False),
                         login_supported=login["supported"], login_retry=login.get("retry"),
                         login_retry_label=login.get("retry_label"))
        label, tone = STATUS.get(status, STATUS["not_started"])
        locked = progress["locked"].get(number)
        if locked:
            label, tone = "Needs step %d first" % locked, "muted"
        title = setup_steps.ENGINE_TITLES[engine] if number == 6 else STEP_TITLES[number]
        view = {"number": number, "key": key, "title": title, "status": status, "label": label,
                "tone": tone, "message": message, "locked": locked, "saved": saved, "state": state,
                "progress": progress, "oob": False, "email_locked": setup_steps.email_locked(state),
                "locked_line": setup_steps.LOCKED_LINE, "support": setup_steps.SUPPORT_EMAIL,
                "website": setup_steps.WEBSITE_URL, "busy_line": setup_steps.BUSY_LINE}
        if number == 1 and locked is None and progress["locked"].get(2) == 1:
            view["unlocks"] = 2
        if number in (1, 5, 6):   # 6: the OpenAI API key (E0.T8) is kept the same way
            view["keychain_wait"] = setup_steps.keys_waiting(home)   # E2.T4 (KR1): in place of the key form
            import credentials
            view["offer_file"] = credentials.offer_file(Path(home) if home is not None else user_profile.home())
        if number == 1 and status != "done":
            # GR1: a key saved but never tested (or saved before the tested mark existed) is shown
            # masked with [Test saved key], so the user never types it again.
            stored = setup_steps.saved_openrouter_key(home)
            if stored and not setup_steps.openrouter_tested(home):
                view["saved_key"] = settings_page.masked(stored)
        if number == 6:  # U1 3g: the engine choice, and Claude's sign-in choice (its key only ever masked)
            view.update(engine=engine, engine_options=setup_steps.engine_options(),
                        antigravity_risk=setup_steps.ANTIGRAVITY_RISK,
                        claude_auth_view=setup_steps.claude_auth_view(state),
                        claude_key=settings_page.masked(setup_steps.shown_claude_key(home)) if engine == "claude" else None,
                        claude_path=state.get("claude_path"), antigravity_path=state.get("antigravity_path"))
            models, main_model, read_model, gone = setup_steps.antigravity_model_view(state)   # M2, P3
            view.update(antigravity_models=models, antigravity_model=main_model, antigravity_read_model=read_model,
                        antigravity_model_note=setup_steps.ANTIGRAVITY_MODEL_NOTE, antigravity_model_gone=gone)
        if number == 6:  # E0.T8: the sign-in choice; a saved key only ever masked here
            import openai_auth
            stored = openai_auth.shown_key(home)   # None while the keychain waits (the waiting line shows)
            test_state = settings_page._openai_test_state(home)
            view.update(codex_auth=setup_steps.codex_auth(state), codex_auth_view=setup_steps.codex_auth_view(state),
                        openai_key=settings_page.masked(stored),
                        openai_untested=bool(stored) and test_state != "passed",
                        openai_test_state=test_state)
        if number == 3:
            form = setup_steps.profile_form(home)
            view.update(form=form, errors=saved.get("errors") or {}, pick=pick_view,
                        self_id=setup_steps.picklists()["self_identification"])
        if number == 8:
            view.update(chrome=setup_steps.chrome_path(), browser_open=browser.is_open(),
                        webmail=state.get("webmail"), webmail_record=state["steps"].get("webmail", {}),
                        application_email=(state.get("locked_email") or setup_steps.saved_email(home)),
                        pick=pick_view)
        view.update(extra or {})
        return view

    def pick_view(field, chosen):
        listed, several = (setup_steps.PICK_FIELDS.get(field) or ("webmail", False))
        values = chosen if isinstance(chosen, list) else ([chosen] if chosen else [])
        return {"field": field, "list": listed, "several": several, "chosen": values,
                "custom": field in setup_steps.CUSTOM_FIELDS}

    def setup_response(request, focus, extra=None, also=()):
        """The changed step, plus (htmx) the dependent steps, progress and banners out of band."""
        progress = setup_steps.progress(home)
        if request.headers.get("hx-request") != "true":
            return RedirectResponse("/setup#step-%d" % focus, status_code=303)
        parts = [templates.get_template("setup_step.html").render(step=step_view(focus, progress, extra))]
        for number in DEPENDENT_STEPS + tuple(also):
            if number != focus:
                view = step_view(number, progress)
                view["oob"] = True
                parts.append(templates.get_template("setup_step.html").render(step=view))
        context = layout("setup", "Setup", progress)
        parts.append(templates.get_template("setup_progress.html").render(progress=progress, oob=True))
        parts.append(templates.get_template("banners.html").render(banners=context["banners"], oob=True))
        parts.append(templates.get_template("chips.html").render(mode=context["mode"], license=context["license"], oob=True))
        return HTMLResponse("".join(parts))

    @app.get("/setup", name="setup")
    def setup_page(request: Request):
        progress = setup_steps.progress(home)
        context = layout("setup", "Setup", progress)
        context.update(progress=progress, steps=[step_view(number, progress, {"page_load": True})
                                                 for number, _, _ in setup_steps.STEPS])
        return templates.TemplateResponse(request, "setup.html", context)

    @app.get("/setup/guide/{name}")
    def guide(request: Request, name: str):
        if name not in GUIDES:
            return _refuse(404, "No such guide.")
        if request.headers.get("hx-request") == "true":
            return templates.TemplateResponse(request, "guides/%s.html" % name, {})
        context = layout("setup", "Setup")
        context.update(guide="guides/%s.html" % name)
        return templates.TemplateResponse(request, "guide_page.html", context)

    async def form_of(request):
        return await request.form()

    def locked_refusal(request, number):
        progress = setup_steps.progress(home)
        if progress["locked"].get(number):
            return setup_response(request, number)
        return None

    @app.post("/setup/openrouter/test")
    async def openrouter_test_saved(request: Request):
        await run_in_threadpool(setup_steps.test_saved_openrouter, config, home)
        return setup_response(request, 1)

    @app.post("/setup/openrouter")
    async def openrouter(request: Request):
        form = await form_of(request)
        typed = form.get("key") if isinstance(form.get("key"), str) else ""
        state = await run_in_threadpool(setup_steps.save_and_test_openrouter, typed, config, home, None,
                                        form.get("to_file") == "1")
        if state.get("keychain_refused"):   # KC2: nothing saved or recorded; the typed key stays in its box
            return setup_response(request, 1, {"keychain_notice": state["keychain_refused"], "key_box": typed})
        return setup_response(request, 1)

    @app.post("/setup/resume")
    async def resume(request: Request):
        refused = locked_refusal(request, 2)
        if refused:
            return refused
        form = await form_of(request)
        upload = form.get("resume")
        if upload is None or not hasattr(upload, "read"):
            setup_steps.record_keep(home, "Choose your résumé file first.")
        else:
            data = await upload.read(setup_steps.RESUME_MAX_BYTES + 1)
            # Saves the file and starts the read in the background; returns at once.
            await run_in_threadpool(setup_steps.save_resume, upload.filename, data, config, home)
        return setup_response(request, 2)

    @app.post("/setup/resume/retry")
    def resume_retry(request: Request):
        refused = locked_refusal(request, 2)
        if refused:
            return refused
        setup_steps.start_resume_read(config, home)
        return setup_response(request, 2)

    @app.get("/setup/resume/status")
    def resume_status(request: Request):
        """Step 2's poll while reading: only step 2 while it reads (so the other steps are not
        re-rendered every few seconds), then the usual response with progress and banners."""
        progress = setup_steps.progress(home)
        if progress["state"]["steps"].get("resume", {}).get("status") == "reading" and \
                request.headers.get("hx-request") == "true":
            return HTMLResponse(templates.get_template("setup_step.html").render(step=step_view(2, progress)))
        return setup_response(request, 2)

    @app.post("/setup/profile")
    async def profile(request: Request):
        form = await form_of(request)
        values = {}
        for field in form.keys():
            if field in setup_steps.PICK_FIELDS:
                values[field] = form.getlist(field)
            elif isinstance(form.get(field), str):
                values[field] = form.get(field)
        setup_steps.save_profile(values, home)
        return setup_response(request, 3)

    @app.get("/setup/pick/{field}")
    def pick_options(request: Request, field: str):
        if field not in setup_steps.PICK_FIELDS and field != "webmail":
            return _refuse(404, "No such list.")
        listed = pick_view(field, [])["list"]
        query = request.query_params.get("q_" + field, "")
        found = setup_steps.matches(listed, query)
        typed = setup_steps.custom_value(field, query)
        if typed and typed.casefold() in (label.casefold() for label in found):
            typed = None
        return templates.TemplateResponse(request, "pick_options.html", {
            "field": field, "matches": found, "typed": typed})

    @app.post("/setup/pick/{field}/{action}")
    async def pick_change(request: Request, field: str, action: str):
        if (field not in setup_steps.PICK_FIELDS and field != "webmail") or action not in ("add", "remove"):
            return _refuse(404, "No such list.")
        form = await form_of(request)
        view = pick_view(field, [value for value in form.getlist(field)
                                 if isinstance(value, str) and setup_steps.acceptable(field, pick_view(field, [])["list"], value)])
        value = form.get("pick_value")
        if isinstance(value, str) and setup_steps.option(view["list"], value) is None:
            value = setup_steps.custom_value(field, value)
        if isinstance(value, str):
            if action == "remove":
                view["chosen"] = [item for item in view["chosen"] if item != value]
            elif view["several"]:
                view["chosen"] = list(dict.fromkeys(view["chosen"] + [value]))
            else:
                view["chosen"] = [value]
        return templates.TemplateResponse(request, "pick_chosen.html", {"pick": view})

    @app.post("/setup/mode")
    async def mode(request: Request):
        form = await form_of(request)
        import approvals  # review mode's store (E6.T2): the database row first, then setup-state.json
        try:
            approvals.set_install_mode(form.get("mode"), root=root, home=home)
        except approvals.ModeError:  # an unknown value: nothing written, the mode unchanged (P6)
            return setup_response(request, 4, {"mode_error": MODE_ERROR})
        return setup_response(request, 4)

    @app.post("/setup/license")
    async def license_(request: Request):
        form = await form_of(request)
        typed = form.get("key") if isinstance(form.get("key"), str) else ""
        state = await run_in_threadpool(setup_steps.activate_license, typed, home, relay_url, None,
                                        form.get("to_file") == "1")
        if state.get("keychain_refused"):   # KC2
            return setup_response(request, 5, {"keychain_notice": state["keychain_refused"], "key_box": typed})
        await run_in_threadpool(sync_license_pause, root, home)  # E8.T3
        if setup_steps.license_done(home):  # a newly active key: read LinkedIn again at once
            await run_in_threadpool(setup_steps.linkedin_status, home, relay_url)
        return setup_response(request, 5)

    # U1 3g: step 6 starts with the engine choice; Claude's sign-in mirrors Codex's.
    @app.post("/setup/engine")
    async def engine_choice(request: Request):
        form = await form_of(request)
        state = await run_in_threadpool(setup_steps.choose_engine, form.get("engine"), home)
        if setup_steps.chosen_engine(state) == "claude":
            await run_in_threadpool(setup_steps.check_claude, home)
        elif setup_steps.chosen_engine(state) == "antigravity":   # U1 4: also adds the A1 allow rules
            await run_in_threadpool(setup_steps.check_antigravity, home)
        return setup_response(request, 6)

    @app.post("/setup/antigravity")
    def antigravity_check(request: Request):
        setup_steps.check_antigravity(home)
        return setup_response(request, 6)

    @app.post("/setup/antigravity/models")
    async def antigravity_models(request: Request):   # M2: the user's own models
        form = await form_of(request)
        await run_in_threadpool(setup_steps.choose_antigravity_models, form.get("main"), form.get("read"), home)
        return setup_response(request, 6)

    @app.post("/setup/claude")
    def claude_check(request: Request):
        setup_steps.check_claude(home)
        return setup_response(request, 6)

    @app.post("/setup/claude/auth")
    async def claude_auth(request: Request):
        form = await form_of(request)
        await run_in_threadpool(setup_steps.choose_claude_auth, form.get("choice"), home)
        return setup_response(request, 6)

    @app.post("/setup/claude/key")
    async def claude_key(request: Request):
        form = await form_of(request)
        typed = form.get("key") if isinstance(form.get("key"), str) else ""
        state = await run_in_threadpool(setup_steps.save_claude_key, typed, home, form.get("to_file") == "1")
        if state.get("keychain_refused"):
            return setup_response(request, 6, {"keychain_notice": state["keychain_refused"]})
        return setup_response(request, 6)

    @app.post("/setup/claude/key/remove")
    async def claude_key_remove(request: Request):
        state = await run_in_threadpool(setup_steps.remove_claude_key, home)
        if state.get("keychain_refused"):
            return setup_response(request, 6, {"keychain_notice": state["keychain_refused"]})
        return setup_response(request, 6)

    @app.post("/setup/codex")
    def codex(request: Request):
        setup_steps.check_codex(home)
        return setup_response(request, 6)

    # E0.T8: step 6 offers Sign in with ChatGPT (default) or an OpenAI API key.
    @app.post("/setup/codex/auth")
    async def codex_auth(request: Request):
        form = await form_of(request)
        await run_in_threadpool(setup_steps.choose_codex_auth, form.get("choice"), config, home)
        return setup_response(request, 6)

    @app.post("/setup/codex/key")
    async def codex_key(request: Request):
        form = await form_of(request)
        typed = form.get("key") if isinstance(form.get("key"), str) else ""
        state = await run_in_threadpool(setup_steps.save_and_test_openai, typed, config, home, None, None,
                                        form.get("to_file") == "1")
        if state.get("keychain_refused"):   # KC2: nothing saved or recorded; the typed key stays in its box
            return setup_response(request, 6, {"keychain_notice": state["keychain_refused"], "openai_box": typed})
        # AK1: a refused key is never written; it stays in its hidden box so it is never typed again.
        import openai_auth
        kept = state["steps"]["codex"]["status"] == "failed" and typed.strip() != (openai_auth.shown_key(home) or "")
        return setup_response(request, 6, {"openai_box": typed} if kept else None)

    @app.post("/setup/codex/key/test")
    async def codex_key_test(request: Request):
        await run_in_threadpool(setup_steps.test_saved_openai, config, home)
        return setup_response(request, 6)

    @app.post("/setup/codex/key/remove")
    async def codex_key_remove(request: Request):
        state = await run_in_threadpool(setup_steps.remove_openai_key, home)
        if state.get("keychain_refused"):   # A5: nothing removed
            return setup_response(request, 6, {"keychain_notice": state["keychain_refused"]})
        return setup_response(request, 6)

    @app.get("/setup/linkedin/status")
    def linkedin_status(request: Request):
        if setup_steps.progress(home)["locked"].get(7):
            return setup_response(request, 7)
        state = setup_steps.linkedin_status(home, relay_url)
        saved = state["steps"]["linkedin"]
        if saved.get("offer") == "waiting" and request.query_params.get("poll") == "1":
            return HTMLResponse(templates.get_template("linkedin_wait.html").render(message=saved["message"]))
        response = setup_response(request, 7)
        response.headers["HX-Retarget"] = "#step-7"
        response.headers["HX-Reswap"] = "outerHTML"
        return response

    @app.post("/setup/linkedin/{kind}")
    def linkedin_link(request: Request, kind: str):
        refused = locked_refusal(request, 7)
        if refused:
            return refused
        if kind not in ("connect", "reconnect"):
            return _refuse(404, "No such action.")
        # Connect only when the relay's /accounts shows nothing linked (a create would add a second paid
        # Unipile account otherwise); Reconnect only while the account is listed (E6.T0 step 7, 3.3).
        # While waiting, a fresh link of the same kind may be asked for again (the link is never stored,
        # so this is the way back to it after the step was re-rendered).
        saved = setup_steps.linkedin_status(home, relay_url)["steps"]["linkedin"]
        wanted = "create" if kind == "connect" else "reconnect"
        url = None
        if saved.get("offer") == kind or (saved.get("offer") == "waiting" and saved.get("kind") == wanted):
            _, url = setup_steps.linkedin_link("create" if kind == "connect" else "reconnect", home, relay_url)
        return setup_response(request, 7, {"link_url": url})

    @app.post("/setup/browser/{action}")
    def browser_action(request: Request, action: str):
        if action == "open":
            outcome = browser.open()
            status, message = setup_steps.BROWSER_OPEN_MESSAGES[outcome]
            setup_steps.record(home, "browser", status, message)
        elif action == "done":
            if browser.is_open():
                browser.close()
            else:
                setup_steps.browser_closed(home)
        elif action == "check":
            if not setup_steps.chrome_path():
                setup_steps.record(home, "browser", "failed", "Sidedoor needs Google Chrome. Install it, then click Check again.")
            else:
                setup_steps.record(home, "browser", "not_started", "Google Chrome is installed.")
        else:
            return _refuse(404, "No such action.")
        return setup_response(request, 8)

    @app.post("/setup/webmail")
    async def webmail(request: Request):
        form = await form_of(request)
        setup_steps.webmail_choice(form.get("webmail"), form.get("webmail_url"), home)
        return setup_response(request, 8)

    @app.post("/setup/email")
    def email_check(request: Request):
        refused = locked_refusal(request, 9)
        if refused:
            return refused
        outcome = browser.close(then=lambda: setup_steps.run_email_check(home))
        if outcome is None:
            setup_steps.record(home, "email", "failed", setup_steps.BUSY_LINE)
        return setup_response(request, 9)

    @app.post("/setup/login")
    async def login(request: Request):
        form = await form_of(request)
        await run_in_threadpool(setup_steps.start_at_login, form.get("enabled") == "on", home)
        return await run_in_threadpool(setup_response, request, 10)  # status may ask the OS (L8)

    @app.post("/setup/start")
    def setup_start(request: Request):
        refused = locked_refusal(request, 11)
        if refused:
            return refused
        setup_steps.login_on_continue(home)  # step 10 ticked by default: registered when continuing past it
        setup_steps.start_scheduler(home)
        return setup_response(request, 11, also=(10,))

    settings_page.register(app, templates, layout, pick_view, home=home, root=root, config=config, browser=browser,
                           relay_url=relay_url)  # Settings (E6.T8)
    return app


def _wildcard_taken(port):
    """macOS: SO_REUSEADDR lets 127.0.0.1:`port` bind beside another program's 0.0.0.0 or [::]
    listener, so after our bind a wildcard probe (also SO_REUSEADDR, never listening) tells: it
    fails with EADDRINUSE only when another program holds the wildcard. (Linux refuses our bind
    in that case already, and would refuse this probe against our own listener.)"""
    for family, address in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")):
        try:
            probe = socket.socket(family, socket.SOCK_STREAM)
        except OSError:
            continue
        try:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((address, port))
        except OSError as error:
            if error.errno == errno.EADDRINUSE:
                return True
        finally:
            probe.close()
    return False


def _wildcard_taken_windows(port):
    """Windows: our exclusive 127.0.0.1 bind succeeds beside another program's 0.0.0.0 or [::]
    listener (seen on the Windows VM, 2026-10-23), and once ours is bound every probe fails. So
    before our bind, an exclusive wildcard probe (never listening) tells: it is refused only when
    another program holds the port on some address."""
    for family, address in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")):
        try:
            probe = socket.socket(family, socket.SOCK_STREAM)
        except OSError:
            continue
        try:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind((address, port))
        except OSError as error:
            # Winsock's codes: WSAEADDRINUSE (10048, errno.EADDRINUSE there) and WSAEACCES (10013, a
            # port held exclusively), which is not errno.EACCES (WR2).
            if error.errno in (errno.EADDRINUSE, errno.EACCES, getattr(errno, "WSAEACCES", None)):
                return True
        finally:
            probe.close()
    return False


def bind(port=PORT, platform=None):
    """The dashboard's listening socket on 127.0.0.1:`port`; raises OSError when it is taken."""
    platform = platform or sys.platform
    if platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE") and _wildcard_taken_windows(port):
        raise OSError(errno.EADDRINUSE, "Port %d is held by another program" % port)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: never share the port with another program
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:  # POSIX: allows a restart over TIME_WAIT
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen()
        if platform == "darwin" and _wildcard_taken(port):
            raise OSError(errno.EADDRINUSE, "Port %d is held by another program on all addresses" % port)
    except OSError:
        listener.close()
        raise
    return listener


def running_here(secret, port=PORT, timeout=3):
    """True when the program on `port` is this install's dashboard (it answers the ping with the
    HMAC of a fresh nonce under the token; the token itself is never sent)."""
    nonce = secrets.token_hex(16)
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("GET", PING_PATH + "?n=" + nonce, headers={"Host": f"127.0.0.1:{port}"})
        response = connection.getresponse()
        body = response.read(256).decode("ascii", "replace").strip()
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()
    return response.status == 200 and hmac.compare_digest(body.encode(), ping_answer(secret, nonce).encode())


def scheduler_there(secret, port=PORT, timeout=3):
    """"running" or "stopped" from this install's dashboard on `port` (its scheduler loop, as the
    dashboard itself reads it), or None when no such dashboard answers."""
    nonce = secrets.token_hex(16)
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("GET", SCHEDULER_PATH + "?n=" + nonce, headers={"Host": f"127.0.0.1:{port}"})
        response = connection.getresponse()
        body = response.read(256).decode("ascii", "replace").strip()
    except (OSError, http.client.HTTPException):
        return None
    finally:
        connection.close()
    for state in ("running", "stopped"):
        if response.status == 200 and hmac.compare_digest(body.encode(), scheduler_answer(secret, nonce, state).encode()):
            return state
    return None


def start_there(secret, port=PORT, timeout=5):
    """Asks this install's dashboard already running on `port` to start its scheduler loop
    (`sidedoor serve --start`); True when it answers that it started."""
    nonce = secrets.token_hex(16)
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("POST", START_PATH + "?n=" + nonce + "&p=" + start_proof(secret, nonce),
                           headers={"Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}"})
        response = connection.getresponse()
        body = response.read(256).decode("ascii", "replace").strip()
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()
    return response.status == 200 and body == "started"


def announce(url, open_browser):
    # Machine contract (E2.T6): the dashboard URL line `serve` announces; stays print.
    print(f"Your Sidedoor dashboard: {url}", flush=True)
    # On Linux webbrowser runs xdg-open with the URL in its argv, which other local users can read
    # in /proc/<pid>/cmdline, so the link is only printed there.
    if sys.platform.startswith("linux"):
        print("Open this link in your browser.", flush=True)
    elif open_browser:
        webbrowser.open(url)


def serve(open_browser=True, home=None, path="/", start=False, scheduler=None, tray=False, resume=False):
    """Run the dashboard on 127.0.0.1:47823 until interrupted, or reopen the one already running.
    `sidedoor setup` passes path "/setup" (E4.T1 option C).

    This is the Sidedoor process (E5.T1 option B): it installs the scheduler loop
    (`scheduler_loop`), started by [Start], or at once once Setup's Start has succeeded: with `start`
    (`sidedoor serve --start`, what start at login registers, E5.T2) or `resume` (every `sidedoor
    serve`, which is what opening the app runs: after an update, a crash or a reopen it keeps running,
    owner 2026-10-25). `sidedoor dashboard` and `setup` pass neither and only serve the pages. When the dashboard stops (Quit, Ctrl-C,
    SIGTERM) the loop stops at once, any running job child finishes on its own (owner, 2026-10-22,
    option (a)) and the exit code is 0, so the OS never restarts a deliberate stop; a crash exits
    non-zero. SIGTERM is a normal stop for the whole of serve (login_item.clean_stop, L4).

    With `tray` (the CLI's serve, dashboard and setup) the tray icon (E5.T3, `tray`) takes the main
    thread and the web server runs in a thread; when the tray cannot start the dashboard runs as
    without it."""
    if home is not None:
        # Every job child, and everything in this process, resolves the same home (one database).
        os.environ[user_profile.HOME_VARIABLE] = str(home)
    # HM4: its job children know the port is held by this Sidedoor (paths.migrate's wording).
    os.environ[paths.DASHBOARD_PID_VARIABLE] = str(os.getpid())
    try:
        with login_item.clean_stop():
            return _serve(open_browser, home, path, start, scheduler, tray, resume)
    except KeyboardInterrupt:
        return 0


MIGRATION_WAIT_SECONDS = 3 * 3600  # an old job still running when data first moves into the home


MIGRATION_RETRY_SECONDS = 10


def data_move_banner(home, error=None):
    """E9.T1: the banner for a data move that is waiting, stopped part way or refused
    (`paths.status`): what happened, where the real data is, and the way out, by button or by hand."""
    try:
        value = paths.status(home)
    except Exception:
        return None
    text = paths.message(value)
    if not text:
        return None
    banner = {"tone": "error" if value["held"] or value["state"] == paths.MOVING else "warning", "text": text}
    choices = [conflict for conflict in value["conflicts"] if conflict["choice_required"]]
    if choices:
        # HM1: both copies were used after they split; the owner chooses, the other is set aside.
        conflict = choices[0]
        banner["forms"] = [{"action": "/data/move", "label": "Keep this one: %s (%s)" % (conflict["home"], conflict["home_detail"]),
                            "fields": {"keep": paths.HOME}},
                           {"action": "/data/move", "label": "Keep that one: %s (%s)" % (conflict["code"], conflict["code_detail"]),
                            "fields": {"keep": paths.CODE_SIDE}}]
    elif value["state"] == paths.CONFLICT:
        banner.update(form_action="/data/move", action_label="Move my data now")
    elif value["state"] in (paths.MOVING, "PENDING"):
        # HM3: settled by hand meanwhile, or stopped part way: finish it now.
        banner.update(form_action="/data/move", action_label="Check again")
    steps = [step for conflict in value["conflicts"] for step in conflict["steps"]]
    if steps:
        # The same by hand, one item at a time (duplicate Quit/Open steps folded).
        banner["steps"] = list(dict.fromkeys(steps))
    if error:
        banner["text"] += " " + error
    return banner


WAITING_PAGE = ("<!doctype html><html><head><meta charset=\"utf-8\"><meta http-equiv=\"refresh\" content=\"10\">"
                "<title>Sidedoor</title></head><body style=\"font-family:sans-serif;max-width:40em;margin:4em auto\">"
                "<h1>Moving your data</h1><p>Waiting for a running job to finish&hellip;</p><p>%s</p>"
                "<p>This page refreshes by itself.</p></body></html>")


class WaitingPage:
    """E9.T1, while the move waits (up to MIGRATION_WAIT_SECONDS): the bound port answers with a page
    that says so and refreshes, and answers the ping and [Start] like the dashboard, so a second
    launch opens this page instead of failing with PORT_IN_USE. A start asked for meanwhile is kept
    (`start_requested`) and honoured once the dashboard runs."""

    def __init__(self, listener, secret, home):
        import http.server
        import socketserver
        import threading
        from urllib.parse import parse_qs, urlsplit
        page = self
        self.start_requested = False
        self.home = home
        used = set()

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *arguments):
                pass

            def _send(self, status, body, kind="text/plain"):
                data = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", kind + "; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                parts = urlsplit(self.path)
                if parts.path == PING_PATH:
                    nonce = (parse_qs(parts.query).get("n") or [""])[0]
                    return self._send(200, ping_answer(secret, nonce))
                try:
                    detail = paths.message(paths.status(page.home)) or ""
                except Exception:
                    detail = ""
                self._send(200, WAITING_PAGE % html.escape(detail), "text/html")

            def do_POST(self):
                parts = urlsplit(self.path)
                query = parse_qs(parts.query)
                nonce, proof = (query.get("n") or [""])[0], (query.get("p") or [""])[0]
                if parts.path == START_PATH and nonce and nonce not in used \
                        and hmac.compare_digest(proof.encode(), start_proof(secret, nonce).encode()):
                    used.add(nonce)
                    page.start_requested = True
                    return self._send(200, "started")
                self._send(503, "Sidedoor is moving your data; try again in a minute.")

        class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True

            def __init__(self):
                socketserver.BaseServer.__init__(self, listener.getsockname()[:2], Handler)
                self.socket = listener

            def server_close(self):
                pass  # the listener belongs to serve: the dashboard answers on it next

        self.server = Server()
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.2},
                                       name="sidedoor-moving-data", daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.thread.join(timeout=5)


def prepare_data(home, listener=None, secret=None, announce_waiting=None, sleep=None, clock=None):
    """E9.T1: before the dashboard or its scheduler touches data, move what an older version left in
    the code folder into the home (paths.prepare; this process holds the port, so no other dashboard
    is open). Returns (data folder or None, a start asked for while waiting).

    - state/ in both places (MigrationHeld): the dashboard still opens; its banner explains and offers
      [Move my data now], and every job holds until then.
    - BUSY (a job of the old layout holds its locks, or its database is in use): the port shows the
      waiting page (`WaitingPage`) and the move is tried again every MIGRATION_RETRY_SECONDS, at most
      MIGRATION_WAIT_SECONDS; then None (exit 1, the OS starts Sidedoor again)."""
    import time
    sleep, clock = sleep or time.sleep, clock or time.monotonic
    try:
        return paths.prepare(home, serving=True), False
    except paths.MigrationHeld:
        return paths.data(None, home), False
    except paths.MigrationPending as error:
        print("Waiting to move your data: %s" % error, flush=True)
    page = WaitingPage(listener, secret, home).start() if listener is not None else None
    if announce_waiting:
        announce_waiting()
    try:
        deadline = clock() + MIGRATION_WAIT_SECONDS
        while True:
            try:
                return paths.prepare(home, serving=True), bool(page and page.start_requested)
            except paths.MigrationHeld:
                return paths.data(None, home), bool(page and page.start_requested)
            except paths.MigrationPending as error:
                if clock() >= deadline:
                    LOG.error("Could not move your data: %s", error)
                    return None, False
            sleep(MIGRATION_RETRY_SECONDS)
    finally:
        if page is not None:
            page.stop()


def _serve(open_browser, home, path, start, scheduler, tray=False, resume=False):
    home_path = Path(home) if home is not None else user_profile.home()
    if login_item.unregistered_login_launch(home=home_path):
        return 0  # start at login was turned off from inside the session: a KeepAlive restart ends here (L3)
    secret = token(home)
    import mac_update  # A (owner, 2026-10-23): a downloaded copy outside Applications offers to replace
    if mac_update.offer_move(home_path, running=lambda: running_here(secret), port=PORT):  # before any job
        return 0
    url = f"http://127.0.0.1:{PORT}{path}?t={secret}"
    try:
        listener = bind()
    except OSError:
        if running_here(secret):
            print("Sidedoor's dashboard is already running.", flush=True)
            # A login-started launch (E5.T2) finding Sidedoor already open leaves it as it is: the
            # user opened it, or just ticked start at login in it (which launches the entry now).
            if login_item.started_at_login():
                return 0
            if start and setup_steps.started(home) and not start_there(secret):
                print("Could not start the scheduler of the Sidedoor already running.", file=sys.stderr, flush=True)
                return 1
            announce(url, open_browser)
            return 0
        print(PORT_IN_USE, file=sys.stderr, flush=True)
        if login_item.started_at_login() and login_item.note_port_busy(home_path) >= login_item.PORT_BUSY_LIMIT:
            return 0  # a few tries, then stop the OS's restarts; the next open says why (owner, 2026-10-22)
        return 1
    login_item.clear_port_busy(home_path)
    announced = []

    def announce_waiting():
        announce(url, open_browser)
        announced.append(True)
    root, start_requested = prepare_data(home_path, listener, secret, announce_waiting)
    if root is None:
        listener.close()
        return 1
    import credentials  # E2.T4: move the key files into the OS store before any job starts (K2)
    try:
        credentials.migrate(home_path, log=lambda line: print(line, file=sys.stderr, flush=True))
    except Exception as error:  # never stops the dashboard: the files stay and are read first
        print("Sidedoor could not move its keys this start (%s); it tries again next start." % type(error).__name__,
              file=sys.stderr, flush=True)
    setup_steps.refresh_login_entry(home)  # a moved program, folder or Codex: rewrite the entry (E5.T2)
    setup_steps.use_saved_codex(home)      # the Codex setup found runs from a Dock-opened app (npm: node)
    login_item.sweep_own_logs(root)
    close_orphaned_browser(root)   # a session Chrome the last run left behind (it was stopped mid-session)
    rotation = login_item.start_hourly_rotation(root)
    loop = scheduler_loop.install(scheduler if scheduler is not None else scheduler_loop.Loop(
        root, license_sync=lambda: scheduler_loop.sync_license_pause(root, home),
        before_start=lambda: paths.migrate(root, serving=True),
        license_reason=lambda: scheduler_loop.license_pause_reason(home),
        credentials_hold=lambda: credentials.hold_reason(home_path)))
    try:
        # The loop starts only once the app exists and inside this try, so whatever stops the
        # dashboard also stops and uninstalls the loop.
        config = uvicorn.Config(create_app(secret, PORT, home=home), log_level="warning", access_log=False,
                                server_header=False)
        server = uvicorn.Server(config)
        scheduler_loop.start_license_heartbeat(root, home=home)  # E8.T2: checks once now, then daily
        import update_check
        update_check.start(home=home)  # E9.T3: checks once now, then every 30 minutes; never blocks a page
        # Wait for Start (owner, 2026-10-22): nothing runs until Setup's own Start (step 11) has
        # succeeded. After that, every app open (`resume`, `sidedoor serve`) starts the loop (owner,
        # 2026-10-25: after an update, a crash or a reopen it keeps running); Pause, which is saved,
        # still holds new work. `sidedoor dashboard` and `setup` only serve the pages.
        if (start or start_requested or resume) and setup_steps.started(home):
            loop.start()
        if not announced:  # the waiting page was announced already: it turns into the dashboard
            announce(url, open_browser)
        icon = make_tray(secret, home_path, root) if tray else None
        if icon is None:
            server.run(sockets=[listener])
        else:
            run_with_tray(server, listener, icon, home_path)
    except KeyboardInterrupt:
        # Ctrl-C or SIGTERM: uvicorn shuts down, then re-raises the signal; stopping the dashboard
        # is not an error. A crash propagates: non-zero exit, the OS restarts.
        pass
    finally:
        loop.stop()
        scheduler_loop.stop_license_heartbeat()
        import update_check
        update_check.stop()
        if rotation is not None:
            rotation.set()
        if scheduler_loop.current() is loop:
            scheduler_loop.install(None)  # nothing in this process can start it again
        listener.close()
        # Quit: a session still running keeps its Chrome (it holds the browser lock and its guard
        # closes that Chrome when it ends); a Chrome no session owns any more is closed now.
        close_orphaned_browser(root)
    return 0


def close_orphaned_browser(root):
    """browser_runtime.close_orphan for Sidedoor's profile at start and at Quit. Never raises."""
    try:
        import browser_runtime
        return browser_runtime.close_orphan(None, root)
    except Exception:
        return False


# --- the tray icon (E5.T3) ------------------------------------------------------------------------

STOP_SIGNALS = tuple(getattr(signal, name) for name in ("SIGTERM", "SIGINT") if hasattr(signal, name))


class TrayActions:
    """What the tray's menu does: the same functions as `sidedoor serve` reopening a running
    dashboard (`announce`) and Overview's Pause and Resume (`set_user_pause`); `quit` is set by
    `run_with_tray` to the dashboard's normal stop."""

    def __init__(self, secret, root):
        self.url = f"http://127.0.0.1:{PORT}/?t={secret}"
        self.root = root
        self.quit = lambda: None

    def open_dashboard(self):
        announce(self.url, True)

    def pause(self):
        set_user_pause(self.root, True, "tray")

    def resume(self):
        set_user_pause(self.root, False, "tray")

    def flags(self):
        return scheduler_loop.pause_state(self.root)


def make_tray(secret, home, root, modules=None):
    """The tray for this process, or None (and why, in tray-state.json) when it cannot be shown."""
    reason = tray_icon.unavailable()
    if reason is None:
        try:
            return tray_icon.Tray(TrayActions(secret, root), modules=modules)
        except Exception as error:  # ImportError (pystray, pyobjc, Pillow missing) or a backend error
            reason = "The tray library could not load (%s: %s)." % (type(error).__name__, str(error)[:200])
    tray_icon.write_state(home, "unavailable", reason)
    return None


def run_with_tray(server, listener, icon, home):
    """The web server in a thread, the tray on this (the main) thread until Quit, a stop signal or
    the server ending. Quit and SIGTERM/SIGINT set `server.should_exit`, exactly what uvicorn's own
    handlers do when it runs on the main thread (it installs none in a thread); `_serve`'s finally
    then stops the loop, the heartbeat and the update check as on any stop. If the tray fails to
    start, the server keeps running without it. A server crash is raised here (non-zero exit)."""
    failure = []

    def serve_in_thread():
        try:
            server.run(sockets=[listener])
        except BaseException as error:
            failure.append(error)
        finally:
            icon.stop()

    def request_stop(*args):
        server.should_exit = True
        icon.stop()

    icon.actions.quit = request_stop
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for number in STOP_SIGNALS:
            previous[number] = signal.signal(number, request_stop)
    worker = threading.Thread(target=serve_in_thread, name="sidedoor-dashboard", daemon=True)
    worker.start()

    def ready():
        tray_icon.write_state(home, "shown")
        icon.call_on_main(lambda: tray_icon.mac_signals(request_stop, STOP_SIGNALS))
    try:
        try:
            icon.run(on_ready=ready)
        except Exception as error:
            tray_icon.write_state(home, "unavailable", "The tray icon could not start (%s: %s)."
                                  % (type(error).__name__, str(error)[:200]))
            LOG.warning("Sidedoor runs without its tray icon (%s).", type(error).__name__)
        finally:
            for number in previous:  # Python's own signal handling again (macOS: after the Mach hooks)
                signal.signal(number, request_stop)
        while worker.is_alive():  # the tray ended (Quit, a signal) or never started: wait for the server
            worker.join(0.5)
    finally:
        server.should_exit = True
        worker.join(10)
        for number, handler in previous.items():
            signal.signal(number, handler)
    if failure:
        raise failure[0]
