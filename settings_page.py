"""The Settings page (E6.T8; E6.T0 spec section 10): every section edited the way its Setup step
works, through the same `setup_steps` functions, so a value saved here is stored in exactly the
shape Setup stores it and read identically by every consumer.

Sections (anchors `#profile`, `#resume`, `#mode`, `#connections`, `#license`, `#login`):
- Profile and search: Setup step 3's form, saved by `setup_steps.save_profile` (same validation,
  same profile.json shape, same `setup_picks`). Once the email check has passed the application
  email is read-only, and `validate_profile` keeps the locked address whatever is posted.
- Résumé: replace the file (`setup_steps.save_resume`: the same background read, states and
  `resume_intake.PROFILE_LOCK` as step 2), [Try again], the résumé in use (raw upload and
  application résumé). Its claims are not shown (owner, 2026-10-22: clutter).
- Mode: step 4's toggle (`setup_steps.set_mode`).
- Connections: LinkedIn (step 7), OpenRouter (step 1), Codex (step 6), Sidedoor's browser and the
  email check (steps 8 and 9, the same browser-lock rule through the dashboard's BrowserSession).
- License: what step 5 stores, [Activate] with the stored key and [Change key]
  (`setup_steps.activate_license`), and the license state with its way forward (`license_clock.view`,
  E8.T2: trial days left, payment retrying, the offline countdown, the clock check, [Check again]).
  [Manage subscription] opens Stripe's customer portal login (`config.json` `billing_portal_url`) in a
  new tab; with no URL configured the section says to email support instead.
- Start at login: Setup step 10's checkbox (`setup_steps.start_at_login`, `login_view`; E5.T2).

Nothing here judges content: Python only fits values to controls (owner, 2026-10-07/10-21).
Secrets (OpenRouter key, license key) appear only as the value of their own password box, hidden
until the user clicks its eye button (owner, 2026-10-22), and as masked labels (`masked`); never in a
message, URL or log.

`register(app, ...)` is called once from `dashboard.create_app`; it replaces the placeholder
/settings route.
"""

import sqlite3
from pathlib import Path

from fastapi import Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

import resume_intake
import credentials
import setup_steps
import paths

SECTIONS = ("profile", "resume", "mode", "connections", "license", "login")
BUSY = "Sidedoor is busy with another task. Try again in a moment."
PORTAL_LINE = "Sign in with the email you subscribed with to update your card, see invoices or cancel."
NO_PORTAL = "To update your card, see invoices or cancel, email"
BLOCKS = ("linkedin", "openrouter", "codex", "browser", "webmail")  # Connections parts, swapped on their own
# Setup messages that name Setup's buttons, said with the buttons Settings has (T7). Setup keeps its wording.
SETTINGS_WORDING = (
    ("Sign in there, then click I'm done.", "Sign in there, then click Run email check (or I'm done to just close it)."),
    ("then click Try again. If this message stays", "then click Open again. If this message stays"),
    ("Sidedoor needs Google Chrome. Install it, then click Check again.",
     "Sidedoor needs Google Chrome. Install it, then reload this page."),
    ("Sidedoor could not start Google Chrome. Try again.", "Sidedoor could not start Google Chrome. Click Open to try again."),
    ("signed in to your inbox, then click I'm done.", "signed in to your inbox, then click Run email check."),
    ("or change your application email in step 3.", "or change your application email in Profile and search."),
)
# Setup's prerequisites of the email check (setup_steps.NEEDS[9]): step -> (what to finish, where).
EMAIL_NEEDS = {3: ("your profile", "#profile"), 6: ("Codex", "#connection-codex"),
               "webmail": ("Your webmail", "#connection-webmail"),
               8: ("signing in to Sidedoor's browser (Open, then I'm done)", "#connection-browser")}
KEY_KEPT = "Your previous key is still in use."
MODE_DONE = ("released", "kept", "stayed", "busy")


def mode_notice(done, count, changed=0):
    """What the Review -> Automatic question's answer did, in plain words (E6.T4)."""
    try:
        count, changed = int(count), int(changed or 0)
    except (TypeError, ValueError):
        count, changed = 0, 0
    if done == "released":
        text = "Saved: Automatic. %d message%s will go out through the usual sending checks." % (count, "" if count == 1 else "s")
        if changed:
            text += " %d changed since you were asked and %s still waiting on Approvals." % (changed, "is" if changed == 1 else "are")
        return text
    if done == "kept":
        return ("Saved: Automatic. %d message%s kept for review on Approvals; new messages go out on their own."
                % (count, "" if count == 1 else "s"))
    if done == "stayed":
        return "Still in Review. Nothing changed."
    return BUSY
NOT_RECALLED = ("Switching to Review applies to anything not yet claimed for sending. Something already being "
                "sent is not recalled.")


def say(message):
    """A Setup message as Settings shows it: Settings' own button names (T7)."""
    if not isinstance(message, str):
        return message
    for setup, settings in SETTINGS_WORDING:
        message = message.replace(setup, settings)
    return message


def masked(secret, prefix=""):
    """`SD-...` plus the last 4 characters (or `...` plus the last 4 for a key with no prefix)."""
    if not secret:
        return None
    return prefix + "..." + secret[-4:]


KEYS_REMOVED = "Your OpenRouter key and license key were removed from this computer."
KEYS_REMOVED_OPENROUTER = "Removed from this computer. Paste your key to use Sidedoor again."
KEYS_NOT_REMOVED = ("Sidedoor couldn't remove your keys because the keychain isn't answering. Nothing was "
                    "changed; try again once it's unlocked.")


def _openrouter_key(home):
    return setup_steps.saved_openrouter_key(home)   # E2.T4: through credentials.get


def _openai_key(home):
    import openai_auth
    return openai_auth.shown_key(setup_steps._home(home))   # E2.T4: None while the keychain waits


def _openai_test_state(home):
    import openai_auth
    try:
        return openai_auth.test_state(setup_steps._home(home))
    except OSError:   # the keychain waits: the waiting line is shown, not a test state
        return "never"


def _openai_tested(home):
    return _openai_test_state(home) == "passed"


def _read(root, read, empty):
    """`read(database)` on a read-only connection; `empty` when the database or its tables do not
    exist yet. A locked database raises (the page answers busy)."""
    import dashboard

    if not paths.database(root).is_file():
        return empty
    try:
        database = dashboard.read_connection(root)
    except sqlite3.OperationalError as error:
        if "locked" in str(error) or "busy" in str(error):
            raise
        return empty
    try:
        return read(database)
    except sqlite3.OperationalError as error:
        if "no such table" in str(error):
            return empty
        raise
    finally:
        database.close()


def current_claims(root):
    """[(claim_id, claim)] of the résumé in use."""
    return _read(root, resume_intake.claims, [])


def removed_count(root, raw_sha256):
    """How many sentences of the résumé with this hash the user removed in Settings."""
    if not raw_sha256:
        return 0
    return _read(root, lambda database: len(resume_intake._removed(database, raw_sha256)), 0)


def _in_home(home, path):
    """The résumé file at `path` when it lies in the Sidedoor home's résumé folder, else None."""
    if not isinstance(path, str) or not path:
        return None
    try:
        file = Path(path).resolve()
        folder = (setup_steps._home(home) / setup_steps.RESUME_DIRECTORY).resolve()
    except OSError:
        return None
    return file if file.is_file() and folder in file.parents else None


def license_times(view, config, now=None):
    """The license section's times in the user's time zone, as Overview writes them (E8.T2)."""
    import datetime as dt
    from zoneinfo import ZoneInfo
    import overview_page
    try:
        zone = ZoneInfo(config.get("timezone") or "America/Los_Angeles")
    except (ValueError, KeyError, OSError):
        zone = ZoneInfo("America/Los_Angeles")
    now = now or dt.datetime.now(dt.timezone.utc)
    return {name: overview_page.when(overview_page._utc(view.get(name)), now, zone)
            for name in ("trial_end", "valid_until", "last_check", "last_attempt")}


def _sync_license_pause(root, home):
    """E8.T3: the license pause follows a new key or an activation at once (`dashboard.sync_license_pause`)."""
    import dashboard
    dashboard.sync_license_pause(root, home)


def register(app, templates, layout, pick_view, *, home, root, config, browser, relay_url):
    app.router.routes[:] = [route for route in app.router.routes if getattr(route, "path", None) != "/settings"]

    # --- views -----------------------------------------------------------------------------------

    def view(name, extra=None):
        progress = setup_steps.progress(home)
        state = progress["state"]
        steps = state["steps"]
        result = {"name": name, "state": state, "steps": steps, "progress": progress,
                  "email_locked": setup_steps.email_locked(state), "locked_line": setup_steps.LOCKED_LINE,
                  "support": setup_steps.SUPPORT_EMAIL, "website": setup_steps.WEBSITE_URL, "page_load": False,
                  "pick": pick_view, "say": say}
        if name == "profile":
            saved = steps.get("profile", {})
            result.update(form=setup_steps.profile_form(home), errors=saved.get("errors") or {},
                          saved=saved, self_id=setup_steps.picklists()["self_identification"],
                          kept_answers=setup_steps.owner_answers(home))   # E6.T4: answers given on Approvals
            result["overrides"], result["kept_over"] = setup_steps.kept_overrides(home)   # E6.T4 A3
            result["kept_status"] = setup_steps.kept_status(home)                          # E6.T4 item 5
        elif name == "resume":
            profile = setup_steps.read_profile(home)
            raw = profile.get("resume_raw") if isinstance(profile.get("resume_raw"), dict) else None
            application = profile.get("resume") if isinstance(profile.get("resume"), dict) else None
            result.update(saved=steps.get("resume", {}), locked=progress["locked"].get(2),
                          raw=raw, application=application,
                          raw_file=bool(raw and _in_home(home, raw.get("path"))),
                          application_file=bool(application and _in_home(home, application.get("path"))),
                          claims=current_claims(root),
                          removed=removed_count(root, raw.get("sha256") if raw else None))
        elif name in ("connections",) + BLOCKS:
            result.update(linkedin=steps.get("linkedin", {}), openrouter=steps.get("openrouter", {}),
                          openrouter_key=masked(_openrouter_key(home)), openrouter_box=_openrouter_key(home) or "",
                          openrouter_untested=bool(_openrouter_key(home)) and not setup_steps.openrouter_tested(home),
                          codex=steps.get("codex", {}),
                          codex_done=setup_steps.codex_done(state), codex_path=state.get("codex_path"),
                          codex_auth=setup_steps.codex_auth(state), codex_auth_view=setup_steps.codex_auth_view(state),
                          openai_key=masked(_openai_key(home)), openai_box=_openai_key(home) or "",
                          openai_untested=bool(_openai_key(home)) and not _openai_tested(home),
                          openai_test_state=_openai_test_state(home),
                          browser=steps.get("browser", {}), email=steps.get("email", {}),
                          chrome=setup_steps.chrome_path(), browser_open=browser.is_open(),
                          webmail=state.get("webmail"), webmail_record=steps.get("webmail", {}),
                          has_license=bool(setup_steps.license_key(home)),
                          keychain_wait=setup_steps.keys_waiting(home),
                          offer_file=credentials.offer_file(setup_steps._home(home)),
                          application_email=state.get("locked_email") or setup_steps.saved_email(home))
        elif name == "license":
            import license_clock  # E8.T2: the stored state (no relay call on render)
            view = license_clock.view(home)
            value = setup_steps.entitlement(home) or {}
            key = setup_steps.license_key(home)
            chip = {"label": view["label"], "tone": view["tone"], "href": view["href"]}
            result.update(license=license_clock.local_message(steps.get("license", {}), config), chip=chip, key=masked(key, "SD-" if key and key.startswith("SD-") else ""),
                          has_key=bool(key), key_box=key or "", view=view, times=license_times(view, config),
                          entitlement=value, active=bool(value) and view["problem"] != "machine",
                          portal=str(config.get("billing_portal_url") or "").strip(),
                          portal_line=PORTAL_LINE, no_portal=NO_PORTAL,
                          keychain_wait=setup_steps.keys_waiting(home),
                          offer_file=credentials.offer_file(setup_steps._home(home)))
        elif name == "mode":
            import approvals  # the mode as the send path reads it (E6.T2 review fix P13)
            result.update(not_recalled=NOT_RECALLED, state=dict(state, mode=approvals.install_mode(root, home)))
        elif name == "login":
            login = setup_steps.login_view(steps.get("login"), home)
            if login.get("default"):  # Settings shows the real state; the ticked default is Setup's only
                login = dict(login, default=False, message=None)
            result.update(login=login)
        result.update(extra or {})
        return result

    def render(name, extra=None, oob=False):
        if name in BLOCKS:  # one part of Connections, so what is typed in the others is kept (T6)
            return templates.get_template("settings_%s.html" % name).render(x=view(name, extra), oob=oob)
        return templates.get_template("settings_section.html").render(section=view(name, extra), oob=oob)

    def respond(request, name, extra=None, also=()):
        """The changed section or Connections part, the parts that depend on it (`also`) and the
        header chips and banners, out of band (htmx); a plain form post (no JavaScript) goes back
        to the page at that section."""
        if request.headers.get("hx-request") != "true":
            return RedirectResponse("/settings#" + ("connections" if name in BLOCKS else name), status_code=303)
        context = layout("settings", "Settings")
        parts = [render(name, extra)] + [render(other, oob=True) for other in dict.fromkeys(also) if other != name] + [
                 templates.get_template("banners.html").render(banners=context["banners"], oob=True),
                 templates.get_template("chips.html").render(mode=context["mode"], license=context["license"], oob=True)]
        return HTMLResponse("".join(parts))

    @app.get("/settings", name="settings")
    def settings(request: Request, ask: str = "", mode_done: str = "", n: str = "0", changed: str = ""):
        context = layout("settings", "Settings")
        # A plain form post (no JavaScript) to Mode comes back here at #mode (E6.T4 review 7): the
        # Review -> Automatic question (ask=switch, read now; a GET writes nothing) or what was done.
        extra = {"page_load": True}
        if ask == "switch":
            import approvals
            shown = waiting_now() if approvals.install_mode(root, home) == approvals.REVIEW else []
            if shown:
                extra["switch_question"] = shown
        elif mode_done in MODE_DONE:
            extra["mode_notice"] = mode_notice(mode_done, n, changed)
        context.update(sections=[view(name, extra if name == "mode" else {"page_load": True}) for name in SECTIONS],
                       setup_file_broken=setup_steps.state_unreadable(home))
        return templates.TemplateResponse(request, "settings.html", context)

    # --- Profile and search (Setup step 3) ---------------------------------------------------------

    @app.post("/settings/profile")
    async def profile(request: Request):
        form = await request.form()
        values = {}
        for field in form.keys():
            if field in setup_steps.PICK_FIELDS:
                values[field] = form.getlist(field)
            elif isinstance(form.get(field), str):
                values[field] = form.get(field)
        before = setup_steps.saved_email(home)
        state, errors = setup_steps.save_profile(values, home)
        # The browser part shows the saved application email.
        return respond(request, "profile", {"saved_now": not errors},
                       also=("browser",) if setup_steps.saved_email(home) != before else ())

    # --- Résumé (Setup step 2) ---------------------------------------------------------------------

    def resume_locked(request):
        return respond(request, "resume") if setup_steps.progress(home)["locked"].get(2) else None

    def resume_response(request):
        """Step 2's notice (e.g. "Choose your résumé file first.") is shown in this response only and
        then cleared, so it never outlives the action that caused it (T9)."""
        notice = {}

        def take(state):
            notice["text"] = state["steps"].get("resume", {}).pop("notice", None)
        setup_steps.update_state(take, home)
        return respond(request, "resume", {"notice": notice["text"]})

    @app.post("/settings/resume")
    async def resume(request: Request):
        refused = resume_locked(request)
        if refused:
            return refused
        form = await request.form()
        upload = form.get("resume")
        if upload is None or not hasattr(upload, "read"):
            setup_steps.record_keep(home, "Choose your résumé file first.")
        else:
            data = await upload.read(setup_steps.RESUME_MAX_BYTES + 1)
            await run_in_threadpool(setup_steps.save_resume, upload.filename, data, config, home)
        return resume_response(request)

    @app.post("/settings/resume/retry")
    def resume_retry(request: Request):
        refused = resume_locked(request)
        if refused:
            return refused
        setup_steps.start_resume_read(config, home)
        return resume_response(request)

    @app.get("/settings/resume/status")
    def resume_status(request: Request):
        if request.headers.get("hx-request") == "true" and setup_steps.resume_reading(home):
            return HTMLResponse(render("resume"))  # only the section while it reads
        return respond(request, "resume")

    @app.get("/settings/resume/file/{which}")
    def resume_file(which: str):
        profile = setup_steps.read_profile(home)
        record = profile.get({"raw": "resume_raw", "application": "resume"}.get(which, ""))
        file = _in_home(home, record.get("path")) if isinstance(record, dict) else None
        if file is None:
            return HTMLResponse("This file is not on this computer any more. Upload your résumé again in Settings.",
                                status_code=404)
        name = record.get("file_name") if which == "raw" and isinstance(record.get("file_name"), str) else (
            "application-resume.pdf" if which == "application" else file.name)
        return FileResponse(file, filename=name, content_disposition_type="inline")

    # Claims are not shown or edited in Settings (owner, 2026-10-22: "clutter").

    # --- Mode (Setup step 4) -----------------------------------------------------------------------

    @app.post("/settings/mode")
    async def mode(request: Request):
        form = await request.form()
        import approvals  # review mode's store (E6.T2): the database row first, then setup-state.json
        if form.get("mode") == approvals.AUTOMATIC and approvals.install_mode(root, home) == approvals.REVIEW:
            # E6.T4 (E6.T0 section 10): Review -> Automatic with messages waiting asks once, listing
            # exactly the messages counted; nothing is written until the user answers.
            shown = await run_in_threadpool(waiting_now)
            if shown:
                if not htmx(request):
                    return RedirectResponse("/settings?ask=switch#mode", status_code=303)
                return respond(request, "mode", {"switch_question": shown})
        try:
            approvals.set_install_mode(form.get("mode"), root=root, home=home)
        except approvals.ModeError:  # an unknown value: nothing written, the mode unchanged (P6)
            return respond(request, "mode", {"mode_error": "Choose Automatic or Review."})
        return respond(request, "mode", {"saved_mode": True})

    def htmx(request):
        return request.headers.get("hx-request") == "true"

    def waiting_now():
        import approvals
        import dashboard
        import overview_page
        return overview_page.snapshot(paths.database(root),
                                      lambda database: [{"action_id": item["action_id"], "payload_hash": item["payload_hash"]}
                                                        for item in approvals.waiting(database)] if database is not None else [],
                                      lambda: dashboard.read_connection(root))

    def with_database(change):
        import dashboard
        try:
            database = dashboard.write_connection(root)
            try:
                return change(database)
            finally:
                database.close()
        except sqlite3.OperationalError as error:
            if "locked" in str(error) or "busy" in str(error):
                return None
            raise

    def mode_done(request, done, count, changed=0):
        if not htmx(request):
            return RedirectResponse("/settings?mode_done=%s&n=%d&changed=%d#mode" % (done, count, changed), status_code=303)
        return respond(request, "mode", {"mode_notice": mode_notice(done, count, changed)})

    @app.post("/settings/mode/release")
    async def mode_release(request: Request):
        """[Send them]: approve exactly the messages the question listed, then Automatic (one transaction)."""
        form = await request.form()
        import approvals
        shown = list(zip(form.getlist("action_id"), form.getlist("payload_hash")))
        result = await run_in_threadpool(with_database, lambda database: approvals.release_all(database, shown, home=home))
        if result is None:
            if not htmx(request):
                return RedirectResponse("/settings?mode_done=busy#mode", status_code=303)
            return respond(request, "mode", {"switch_question": [{"action_id": a, "payload_hash": h} for a, h in shown],
                                             "mode_error": BUSY})
        return mode_done(request, "released", len(result["released"]), len(result["changed"]))

    @app.post("/settings/mode/keep")
    async def mode_keep(request: Request):
        """[Keep for review]: only the mode changes; the waiting messages stay on Approvals."""
        import approvals
        result = await run_in_threadpool(with_database, lambda database: approvals.keep_for_review(database, home=home))
        if result is None:
            if not htmx(request):
                return RedirectResponse("/settings?mode_done=busy#mode", status_code=303)
            return respond(request, "mode", {"switch_question": await run_in_threadpool(waiting_now), "mode_error": BUSY})
        return mode_done(request, "kept", result["waiting"])

    @app.post("/settings/mode/stay")
    async def mode_stay(request: Request):
        """"Stay in Review": the question goes away and nothing changes (E6.T4 review 6)."""
        return mode_done(request, "stayed", 0)

    # --- Answers kept from Approvals (E6.T4; owner, 2026-10-22, "a") ----------------------------------
    # Each answer's Save and Remove swap that answer alone (htmx), so unsaved Profile edits stay
    # (review 13); without JavaScript the page comes back at the list.

    def kept_fragment(key, notice, oob=False):
        answer = dict(setup_steps.owner_answers(home)).get(key)
        over = setup_steps.kept_overrides(home)[1]
        return templates.get_template("settings_kept_answer.html").render(key=key, answer=answer, notice=notice, over=over, oob=oob,
                                                                          status=setup_steps.kept_status(home))

    def kept_answer(request, key, notice):
        if not htmx(request):
            return RedirectResponse("/settings#kept-answers", status_code=303)
        return HTMLResponse(kept_fragment(key, notice))

    @app.post("/settings/answers/use-setup")
    async def kept_answer_use_setup(request: Request):
        """[Use this value] on a Profile and search field whose kept Approvals answer was in force
        instead (E6.T4 A3): the kept answer is removed, so the field's value counts again. Only the
        note and the kept answer are swapped, so unsaved Profile edits stay."""
        form = await request.form()
        key = form.get("key") or ""
        semantic = (dict(setup_steps.owner_answers(home)).get(key) or {}).get("semantic")
        removed = await run_in_threadpool(setup_steps.remove_owner_answer, key, home)
        if not htmx(request):
            return RedirectResponse("/settings#profile", status_code=303)
        # Show what is actually in force now (E6.T4 item 4, owner: "show the one now in force"): an
        # older kept answer for the same question may take over; only with none left is the Profile
        # and search value used.
        by_field = setup_steps.kept_overrides(home)[0]
        now = next((entry for entries in by_field.values() for entry in entries if entry["semantic"] == semantic), None)
        if now is not None:
            parts = [templates.get_template("settings_kept_override.html").render(o=now,
                     notice="Your answer on Approvals was removed. An earlier answer you gave there is used now, not this value."),
                     kept_fragment(now["key"], None, oob=True)]
            removed_notice = "Removed. An earlier answer you gave on Approvals is used now."
        else:
            parts = ['<div class="kept-override"><p class="note note-success" role="status">%s</p></div>'
                     % ("Now using this value. Your answer on Approvals was removed." if removed
                        else "That answer was already removed; this value is in use.")]
            removed_notice = "Removed. The value in Profile and search is used instead."
        return HTMLResponse(parts[0] + kept_fragment(key, removed_notice if removed else None, oob=True) + "".join(parts[1:]))

    @app.post("/settings/answers/change")
    async def kept_answer_change(request: Request):
        form = await request.form()
        key = form.get("key") or ""
        changed = await run_in_threadpool(setup_steps.change_owner_answer, key, form.get("value"), home)
        return kept_answer(request, key, "Saved the answer." if changed else "Write an answer (Yes or No for a yes/no one), or remove it.")

    @app.post("/settings/answers/remove")
    async def kept_answer_remove(request: Request):
        form = await request.form()
        key = form.get("key") or ""
        removed = await run_in_threadpool(setup_steps.remove_owner_answer, key, home)
        return kept_answer(request, key, "Removed. Sidedoor no longer uses it for later applications."
                           if removed else "That answer was already removed.")

    # --- Start at login (Setup step 10, E5.T2) -------------------------------------------------------

    @app.post("/settings/login")
    async def login(request: Request):
        form = await request.form()
        await run_in_threadpool(setup_steps.start_at_login, form.get("enabled") == "on", home)
        return await run_in_threadpool(respond, request, "login")  # status may ask the OS (L8)

    # --- Connections (Setup steps 1, 6, 7, 8, 9): each action swaps only its own part (T6) -----------

    def resume_unlocked_changed(before):
        return ("resume",) if setup_steps.progress(home)["locked"].get(2) != before else ()

    @app.post("/settings/openrouter")
    async def openrouter_replace(request: Request):
        # Settings only (owner, "sure"): when OpenRouter refuses the new key, the previous key is
        # put back and stays in use; the refused key stays in its (hidden) box to be fixed.
        form = await request.form()
        typed = form.get("key") if isinstance(form.get("key"), str) else ""
        to_file = form.get("to_file") == "1"
        previous = _openrouter_key(home)
        locked = setup_steps.progress(home)["locked"].get(2)
        state = await run_in_threadpool(setup_steps.save_and_test_openrouter, typed, config, home, None, to_file)
        if state.get("keychain_refused"):   # KR3/KC2: nothing saved or recorded; the typed key stays in its box
            return respond(request, "openrouter", {"openrouter_box": typed, "keychain_notice": state["keychain_refused"]})
        saved = state["steps"]["openrouter"]
        if saved["status"] == "failed" and previous and _openrouter_key(home) != previous:
            try:
                setup_steps._save_key("openrouter", previous, home, to_file)
            except OSError:
                pass   # the store stopped answering: the refused key stays saved and says so below
            setup_steps.record(home, "openrouter", "failed", saved["message"] + " " + KEY_KEPT)
        return respond(request, "openrouter", {"openrouter_box": typed} if saved["status"] == "failed" else None,
                       also=resume_unlocked_changed(locked))

    @app.post("/settings/openrouter/test")
    async def openrouter_test(request: Request):
        if setup_steps.keys_waiting(home):   # KC3: never "no key saved" while the keychain waits
            return respond(request, "openrouter")
        stored = _openrouter_key(home)
        locked = setup_steps.progress(home)["locked"].get(2)
        if not stored:
            setup_steps.record(home, "openrouter", "failed", "No OpenRouter key is saved yet. Paste yours below and "
                               "click Save and test.")
        else:
            await run_in_threadpool(setup_steps.save_and_test_openrouter, stored, config, home)
        return respond(request, "openrouter", also=resume_unlocked_changed(locked))

    @app.post("/settings/codex")
    def codex(request: Request):
        setup_steps.check_codex(home)
        return respond(request, "codex")

    # E0.T8: Sign in with ChatGPT or an OpenAI API key, as in Setup step 6.
    @app.post("/settings/codex/auth")
    async def codex_auth(request: Request):
        form = await request.form()
        await run_in_threadpool(setup_steps.choose_codex_auth, form.get("choice"), config, home)
        return respond(request, "codex")

    @app.post("/settings/codex/key")
    async def codex_key(request: Request):
        # AK1: the new key is tested before it is written, so a refused one never replaces the key in
        # use; it stays in its (hidden) box to be fixed, never typed again.
        form = await request.form()
        typed = form.get("key") if isinstance(form.get("key"), str) else ""
        previous = _openai_key(home)
        state = await run_in_threadpool(setup_steps.save_and_test_openai, typed, config, home, None, None,
                                        form.get("to_file") == "1")
        if state.get("keychain_refused"):   # KR3/KC2: nothing saved or recorded; the typed key stays in its box
            return respond(request, "codex", {"openai_box": typed, "keychain_notice": state["keychain_refused"]})
        saved = state["steps"]["codex"]
        failed = saved["status"] == "failed" and _openai_key(home) != typed.strip()
        if failed and previous:
            setup_steps.record(home, "codex", "failed", saved["message"] + " " + KEY_KEPT)
        return respond(request, "codex", {"openai_box": typed} if failed else None)

    @app.post("/settings/codex/key/test")
    async def codex_key_test(request: Request):
        await run_in_threadpool(setup_steps.test_saved_openai, config, home)
        return respond(request, "codex")

    @app.post("/settings/codex/key/remove")
    async def codex_key_remove(request: Request):
        state = await run_in_threadpool(setup_steps.remove_openai_key, home)
        if state.get("keychain_refused"):   # A5: nothing removed
            return respond(request, "codex", {"keychain_notice": state["keychain_refused"]})
        return respond(request, "codex")

    @app.get("/settings/linkedin/status")
    def linkedin_status(request: Request):
        state = setup_steps.linkedin_status(home, relay_url)
        saved = state["steps"]["linkedin"]
        if saved.get("offer") == "waiting" and request.query_params.get("poll") == "1":
            return HTMLResponse(templates.get_template("linkedin_wait.html").render(message=saved["message"]))
        response = respond(request, "linkedin")
        response.headers["HX-Retarget"] = "#connection-linkedin"
        response.headers["HX-Reswap"] = "outerHTML"
        return response

    @app.post("/settings/linkedin/{kind}")
    def linkedin_link(request: Request, kind: str):
        if kind not in ("connect", "reconnect"):
            return HTMLResponse("No such action.", status_code=404)
        # The same rule as Setup step 7: Connect only while nothing is linked (a create would add a
        # second paid Unipile account), Reconnect only while the account is listed.
        saved = setup_steps.linkedin_status(home, relay_url)["steps"]["linkedin"]
        wanted = "create" if kind == "connect" else "reconnect"
        url = None
        if saved.get("offer") == kind or (saved.get("offer") == "waiting" and saved.get("kind") == wanted):
            _, url = setup_steps.linkedin_link(wanted, home, relay_url)
        return respond(request, "linkedin", {"link_url": url})

    @app.post("/settings/browser/{action}")
    def browser_action(request: Request, action: str):
        if action == "open":
            status, message = setup_steps.BROWSER_OPEN_MESSAGES[browser.open()]
            setup_steps.record(home, "browser", status, message)
        elif action == "done":
            if browser.is_open():
                browser.close()
            else:
                setup_steps.browser_closed(home)
        else:
            return HTMLResponse("No such action.", status_code=404)
        return respond(request, "browser")

    @app.post("/settings/webmail")
    async def webmail(request: Request):
        form = await request.form()
        label = form.get("webmail") if isinstance(form.get("webmail"), str) else ""
        url = form.get("webmail_url") if isinstance(form.get("webmail_url"), str) else ""
        setup_steps.webmail_choice(label, url, home)
        # A refused save keeps the posted pick and address in their boxes (T5).
        return respond(request, "webmail", {"webmail_posted": True, "webmail_pick": label, "webmail_url": url,
                                            "webmail_saved_now": True})

    def email_needs():
        """What Setup's step-9 gate (steps 3, 6 and 8) still needs, as (label, link). The browser
        being open is not missing: the check closes it first and runs under its hold."""
        progress = setup_steps.progress(home)
        state, done = progress["state"], progress["done"]
        needs = [EMAIL_NEEDS[number] for number in (3, 6) if not done[number]]
        if not state.get("webmail"):
            needs.append(EMAIL_NEEDS["webmail"])
        if state["steps"].get("browser", {}).get("status") != "done" and not browser.is_open():
            needs.append(EMAIL_NEEDS[8])
        return needs

    @app.post("/settings/email")
    def email_check(request: Request):
        needs = email_needs()
        if needs:
            return respond(request, "browser", {"email_needs": needs})
        locked = setup_steps.email_locked(setup_steps.load_state(home))
        # [Run email check]: closes the dashboard's own browser first and checks under its hold, or
        # takes the browser lock fresh (busy: nothing happens) - Setup step 8's lock rule.
        if browser.close(then=lambda: setup_steps.run_email_check(home)) is None:
            setup_steps.record(home, "email", "failed", setup_steps.BUSY_LINE)
        # A first pass locks the email and the webmail: those parts change too.
        changed = setup_steps.email_locked(setup_steps.load_state(home)) != locked
        return respond(request, "browser", also=("webmail", "profile") if changed else ())

    # --- License (Setup step 5) --------------------------------------------------------------------

    async def activate(request, key, to_file=False):
        had_key = bool(setup_steps.license_key(home))
        state = await run_in_threadpool(setup_steps.activate_license, key, home, relay_url, None, to_file)
        if state.get("keychain_refused"):   # KC2: nothing saved or recorded; the typed key stays in its box
            return respond(request, "license", {"key_box": key if isinstance(key, str) else "",
                                                "keychain_notice": state["keychain_refused"]})
        await run_in_threadpool(_sync_license_pause, root, home)  # E8.T3: a new key resumes at once
        if setup_steps.license_done(home):  # a newly active key: read LinkedIn again at once
            await run_in_threadpool(setup_steps.linkedin_status, home, relay_url)
        refused = state["steps"]["license"]["status"] == "failed"
        # LinkedIn depends on the license: send it along when it changed (T4).
        also = ("linkedin",) if not refused or bool(setup_steps.license_key(home)) != had_key else ()
        # A refused key stays in its (hidden) box so it can be fixed without pasting it again.
        return respond(request, "license", {"key_box": key if isinstance(key, str) else ""} if refused else None,
                       also=also)

    @app.post("/settings/license/activate")
    async def license_activate(request: Request):
        if setup_steps.keys_waiting(home):   # KC3: never "no key saved" while the keychain waits
            return respond(request, "license")
        key = setup_steps.license_key(home)
        if not key:
            setup_steps.record(home, "license", "failed", "No license key is saved yet. Paste yours below and click "
                               "Activate.")
            return respond(request, "license")
        return await activate(request, key)

    @app.post("/settings/keys/remove")
    async def keys_remove(request: Request):
        # A5: "Remove Sidedoor's saved keys from this computer": the keychain items first, then
        # any file; a refused delete leaves everything as it was and says so.
        try:
            await run_in_threadpool(credentials.remove_all, setup_steps._home(home))
        except OSError:
            return respond(request, "license", {"keys_removed": KEYS_NOT_REMOVED})
        (setup_steps._home(home) / setup_steps.OPENROUTER_TESTED_FILE).unlink(missing_ok=True)
        setup_steps.record(home, "openrouter", "failed", KEYS_REMOVED_OPENROUTER)
        await run_in_threadpool(_sync_license_pause, root, home)
        return respond(request, "license", {"keys_removed": KEYS_REMOVED}, also=("connections",))

    @app.post("/settings/license/change")
    async def license_change(request: Request):
        form = await request.form()
        return await activate(request, form.get("key"), form.get("to_file") == "1")
