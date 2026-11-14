"""E18 (one LinkedIn sign-in): the real reader behind `linkedin_session` (the seam the rest of Sidedoor asks).

`read(home)` answers what `linkedin_session.current` promises:
- a `linkedin_session.Session` (LinkedIn's session cookie and the browser's user agent): Sidedoor's browser is
  signed in to LinkedIn;
- None: the browser was read and holds no LinkedIn sign-in;
- raises `linkedin_session.Busy`: another browser session holds Sidedoor's browser right now;
- raises `linkedin_session.Unavailable`: the browser cannot be read (this computer cannot start it without a
  window, a component is missing, it did not start, or its answer could not be read).

How it reads. Chrome is started the product's way and only that way: the browser guard for the `inspect`
operation, exactly as the live view starts it (`live_view.Guard`, under `state/browser.lock` through
`live_view.Hold`), with no site named, so the guard restores and keeps no site session for this read. This
module then attaches a second DevTools client to the port that Chrome opened (`live_view.debugger_url`) and
asks two things: the cookies Chrome holds (`Storage.getCookies`), of which only LinkedIn's `li_at` is looked
at, and the user agent a page of that Chrome presents (`navigator.userAgent`, else `Browser.getVersion`). No
page is opened, nothing is clicked or typed, no model runs. Then the guard and its Chrome are stopped and the
lock is released. When the dashboard's live view is open (the user is signing in there right now) its Chrome is
read instead, through the same two questions, and nothing is started or stopped.

The cookie (owner, 2026-11-12, A1). Its value exists in Chrome, in this call's locals and in the `Session`
handed back; nowhere else. It is never written to a log, the guard's audit, a file, the saved site sessions,
or an exception: every failure here becomes a bare `Unavailable` raised outside its handler (no cause, no
context), and the one log line names the kind of failure only. The DevTools connection's logger is the live
view's, which can never write (the websocket library would otherwise log every frame at DEBUG).

macOS only today, as the live view: where the guard cannot start Chrome without a window (`live_view.supported`)
this is `Unavailable`.
"""

import asyncio
import contextlib
import json
import shutil

import browser_runtime
import linkedin_session
import live_view
import log
import paths

LOG = log.get(__name__)
COOKIE = "li_at"
DOMAIN = "linkedin.com"
FOLDER = "linkedin-session"       # reports/linkedin-session/session: the guard's own records of the latest read
CALL_SECONDS = 15                 # one DevTools answer
READ_SECONDS = 45                 # the two questions together
SIGN_IN_URL = "https://www.linkedin.com/login"

_installed = {}


def install(home=None, root=None, config=None, view=None):
    """Make this the reader the app uses (`dashboard.create_app`). `view` is the dashboard's live view."""
    _installed.update(home=home, root=root, config=config, view=view)
    linkedin_session.set_reader(lambda asked=None: read(asked if asked is not None else home, root, config, view))


def _linkedin(cookie):
    domain = cookie.get("domain") if isinstance(cookie, dict) else None
    if not isinstance(domain, str):
        return False
    domain = domain.lstrip(".").lower()
    return domain == DOMAIN or domain.endswith("." + DOMAIN)


async def _ask(endpoint, connect=None):
    """(cookie value or None, user agent or None) from the Chrome at `endpoint`."""
    if connect is None:
        from websockets.asyncio.client import connect as dial
        connect = lambda url: dial(url, max_size=64 * 1024 * 1024, proxy=None, logger=live_view.WIRE_LOG)
    url = await asyncio.to_thread(live_view.debugger_url, endpoint)
    socket = await connect(url)
    try:
        ident = 0

        async def call(method, params=None, session=None):
            nonlocal ident
            ident += 1
            message = {"id": ident, "method": method, "params": params or {}}
            if session:
                message["sessionId"] = session
            await socket.send(json.dumps(message))
            while True:
                answer = json.loads(await asyncio.wait_for(socket.recv(), CALL_SECONDS))
                if isinstance(answer, dict) and answer.get("id") == ident:
                    result = answer.get("result")
                    return result if isinstance(result, dict) else {}

        cookies = (await call("Storage.getCookies")).get("cookies")
        if not isinstance(cookies, list):
            raise ValueError("cookies")
        value = next((cookie.get("value") for cookie in cookies
                      if _linkedin(cookie) and cookie.get("name") == COOKIE), None)
        value = value if isinstance(value, str) and value.strip() else None
        del cookies
        agent = None
        if value is not None:
            targets = (await call("Target.getTargets")).get("targetInfos")
            page = next((info for info in targets or [] if isinstance(info, dict) and info.get("type") == "page"
                         and isinstance(info.get("targetId"), str)), None)
            if page is not None:
                attached = (await call("Target.attachToTarget", {"targetId": page["targetId"], "flatten": True})).get("sessionId")
                if isinstance(attached, str):
                    shown = (await call("Runtime.evaluate", {"expression": "navigator.userAgent", "returnByValue": True},
                                        attached)).get("result")
                    agent = shown.get("value") if isinstance(shown, dict) else None
                    with contextlib.suppress(Exception):
                        await call("Target.detachFromTarget", {"sessionId": attached})
            if not (isinstance(agent, str) and agent.strip()):
                agent = (await call("Browser.getVersion")).get("userAgent")
            agent = agent if isinstance(agent, str) and agent.strip() else None
        return value, agent
    finally:
        with contextlib.suppress(Exception):
            await socket.close()


def _folder(root):
    folder = paths.reports(root) / FOLDER / "session"
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True, mode=0o700)
    return folder


async def _own_session(root, config, guard_command, connect, endpoint_wait, folder):
    """Start the guard's Chrome, ask, stop it. The caller holds the browser's lock."""
    guard = live_view.Guard(guard_command(config, folder), folder)
    try:
        await guard.start()
        endpoint = await guard.wait_endpoint(endpoint_wait)
        if not endpoint:
            raise ValueError("endpoint")
        return await asyncio.wait_for(_ask(endpoint, connect), READ_SECONDS)
    finally:
        with contextlib.suppress(Exception):
            await guard.stop()


def _stop_chrome(root, config):
    with contextlib.suppress(Exception):
        if browser_runtime.chrome_running(browser_runtime.profile(config)):
            browser_runtime.end_session(config)
    shutil.rmtree(paths.reports(root) / FOLDER / "session", ignore_errors=True)


def _open_view_endpoint(view):
    """The DevTools endpoint of the live view's Chrome while a step is open in the dashboard, else None."""
    try:
        session = view.session if view is not None else None
        guard = getattr(session, "guard", None) if session is not None and not session.closing else None
        endpoint = guard.endpoint() if guard is not None else None
        return endpoint if isinstance(endpoint, str) and endpoint else None
    except Exception:
        return None


def read_running(endpoint, connect=None):
    """The session of the Chrome at DevTools `endpoint`, which is running now (the live view's, at Done,
    before it is stopped): a `Session`, None when it holds no LinkedIn sign-in, or raises `Unavailable`.
    The same two questions as `read`; nothing is started, stopped or written."""
    failed = found = None
    try:
        found = asyncio.run(asyncio.wait_for(_ask(endpoint, connect), READ_SECONDS))
    except Exception as error:
        failed = type(error).__name__
    if failed is None and found[0] is not None and found[1] is None:
        failed = "NO_USER_AGENT"
    if failed is not None:   # outside every handler: no cause and no context travel with it
        LOG.warning("LINKEDIN_SESSION_NOT_READ (%s)", failed)
        raise linkedin_session.Unavailable()
    value, agent = found
    del found
    return None if value is None else linkedin_session.Session(value, agent)


def read(home=None, root=None, config=None, view=None, guard_command=None, connect=None,
         endpoint_wait=live_view.ENDPOINT_WAIT_SECONDS, ps=None, is_supported=None):
    """See the module docstring. Blocking; call it from a worker thread, never from an event loop."""
    root = paths.data(root, home)
    failed = found = None
    endpoint = _open_view_endpoint(view)
    if endpoint is not None:
        try:
            found = asyncio.run(asyncio.wait_for(_ask(endpoint, connect), READ_SECONDS))
        except Exception as error:
            failed = type(error).__name__
    elif not (is_supported or live_view.supported)(config):
        failed = "NOT_SUPPORTED"
    else:
        hold = live_view.Hold(root, config, 0, ps)
        reason = "LOCK"
        try:
            reason = hold.take()
        except Exception as error:
            reason = type(error).__name__
        if reason is not None:
            hold.drop()
            if reason == live_view.BUSY:
                raise linkedin_session.Busy()
            failed = str(reason)
        else:
            try:
                command = guard_command or (lambda config, directory: browser_runtime.guard_command(
                    config, directory, live_view.OPERATION))
                found = asyncio.run(_own_session(root, config, command, connect, endpoint_wait, _folder(root)))
            except Exception as error:
                failed = type(error).__name__
            finally:
                _stop_chrome(root, config)
                hold.drop()
    if failed is not None:   # outside every handler: no cause and no context travel with it
        LOG.warning("LINKEDIN_SESSION_NOT_READ (%s)", failed)
        raise linkedin_session.Unavailable()
    value, agent = found
    del found
    if value is None:
        return None
    if agent is None:
        LOG.warning("LINKEDIN_SESSION_NOT_READ (%s)", "NO_USER_AGENT")
        raise linkedin_session.Unavailable()
    return linkedin_session.Session(value, agent)
