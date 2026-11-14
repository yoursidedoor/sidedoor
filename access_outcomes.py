"""Applications behind a sign-in or a human check: waiting or closed, never a silent loss (E15.T1, R1).

A walled job is in exactly one of three states: resolved (Sidedoor got through), WAITING on the user
(a Needs you hold that waits WAIT_DAYS days), or CLOSED with its reason and a Try again (the existing
APPLICATION_REOPENED). This replaces the retry-hourly-then-abandon-on-the-third path (owner,
2026-10-11) for AUTHENTICATION_REQUIRED, MFA_REQUIRED and CAPTCHA_REQUIRED (owner, 2026-11-10;
docs/product/design/E15-E16-access-walls-design.md, "What ends a job").

Cost rule: one blocked session per job per wall, no new session until something changed (`resume`,
a user action in T2/T5), and no session on a host whose sign-in wall is already waiting on the user
(`wait_on_host`, read from this table; the site-accounts table is T7).

One row per wait in `application_access_waits`; the open one (state WAITING) is what the Needs you
card (T2) renders: `waiting()` joins employer, role and the posting link. Additive schema: an older
database without the table reads as "no waits".
"""
import contextlib
import datetime as dt
import json
import sqlite3
import urllib.parse

WAIT_DAYS = 7
WAIT_SECONDS = WAIT_DAYS * 86400

# Every reason a job waits on the user (design doc table). Producers today (E15.T6 stage 1): HUMAN_CHECK,
# ASK_EACH_TIME (every account wall while Sidedoor cannot sign in itself, see `effective_setting`) and
# PASSKEY; SIGN_IN is what an account wall is once the access step exists, and stays readable on rows
# written before the setting. The rest are the extension points the later stages and T8 fill.
WAIT_REASONS = {
    "SIGN_IN": "The site needs you to sign in.",
    "HUMAN_CHECK": "The site needs a human check.",
    "ACCOUNT_EXISTS": "You already have an account on this site. Sign in once.",
    "CODE_UNREADABLE": "The site sent a sign-in code Sidedoor cannot read. Sign in once.",
    "SIGN_UP_FAILED": "The site did not accept a new account. Sign in or create it yourself.",
    "CONFIRMATION_EMAIL_MISSING": "The site's confirmation email did not arrive. Confirm it yourself.",
    # E15.T8 (owner, 2026-11-13, H12 A): the site was asked to email a sign-in link or code and none came.
    "SIGN_IN_EMAIL_MISSING": "The site's sign-in email did not arrive. Sign in yourself.",
    # E15.T8: the link in the email is not on the site itself, so the guard did not open it.
    "CONFIRMATION_LINK_UNOPENED": "The site's confirmation link is on another site. Confirm it yourself.",
    # E15.T8: a password was typed into the sign-up and its outcome is not known (never "the site refused").
    "SIGN_UP_UNCONFIRMED": "Your account on this site may or may not exist. Sign in once, or reset the password on the site.",
    "SSO_SCOPE": "Signing in with Google asks for more than your name and email. Approve it yourself or abandon.",
    "STEP_NOT_SHOWN": "Sidedoor could not show you the sign-in step. Apply yourself if you want this job.",
    # The site's own profile, required before the application can be reached (owner, 2026-11-14).
    # Owner, 2026-11-14 ("No. The AI completes the Profile based on the info it has about the user"): with
    # "Sign in and create accounts for me" the user is never told to complete a site's profile, and a field
    # their stored info does not cover is a question on Approvals (`access_step.ask`), never a card.
    # PROFILE_REQUIRED is the one card with the setting off (Sidedoor does not fill it then, so only the
    # user can); PROFILE_UNCONFIRMED is the outcome nobody knows (a step that cannot be run again).
    "PROFILE_REQUIRED": "This site asks for a profile before you can apply. Fill it in on the site.",
    "PROFILE_UNCONFIRMED": "Your profile on this site may or may not be complete. Check it on the site.",
    # Plain words until the Sign in button exists (owner, 2026-11-12); true whatever the setting says.
    "ASK_EACH_TIME": "This site needs an account.",
    # Owner's pick R8 b, 2026-11-12, his words exactly.
    "PASSKEY": "This site signs in with a passkey. Apply yourself if you want this job.",
}
# The session stop codes this task handles, each mapped to its wait reason.
WALL_REASONS = {"AUTHENTICATION_REQUIRED": "SIGN_IN", "MFA_REQUIRED": "SIGN_IN", "CAPTCHA_REQUIRED": "HUMAN_CHECK"}
# Reasons that mean "this host needs an account Sidedoor does not have": no session starts on that
# host for another job while one of these waits (cost rule). PASSKEY is an account reason (Skip closes
# it) that holds nothing (`HOLDING_REASONS`, owner 2026-11-12): the user cannot sign in there through
# Sidedoor, so each job on that site gets its own session and its own card.
ACCOUNT_REASONS = frozenset({"SIGN_IN", "ACCOUNT_EXISTS", "CODE_UNREADABLE", "SIGN_UP_FAILED", "SIGN_UP_UNCONFIRMED",
                             "SIGN_IN_EMAIL_MISSING", "ASK_EACH_TIME", "PASSKEY"})
# The site's own profile is one per site: such a wait holds its site and is released with it. It is not an
# account wall for the Skip setting (the user has the account; they get the card under every setting).
PROFILE_REASONS = frozenset({"PROFILE_REQUIRED", "PROFILE_UNCONFIRMED"})
HOLDING_REASONS = (ACCOUNT_REASONS - {"PASSKEY"}) | PROFILE_REASONS

# The one setting, "When a site needs an account" (E15.T6, R3). Three stable internal values; None
# while the user has never chosen. One store: a single row in the install's database (the store the
# applications run already has open), written by Settings through `set_install_account_setting`.
SIGN_IN_FOR_ME, ASK, SKIP = "SIGN_IN", "ASK", "SKIP"
ACCOUNT_SETTINGS = (SIGN_IN_FOR_ME, ASK, SKIP)
ACCOUNT_SETTING_TITLE = "When a site needs an account"
# After "Saved: Skip jobs that need an account." when that save closed waiting applications (F4).
SKIP_CLOSED_ONE = "1 waiting application was closed. Bring it back with Try again on Applications."
SKIP_CLOSED_MANY = "%d waiting applications were closed. Bring one back with Try again on Applications."
# The rest of a site's group (D9): the jobs that waited behind a closed one never met the sign-in
# themselves, so they are not closed; they get their own try.
SKIP_FREED_ONE = "1 more on the same site will be tried on the next round."
SKIP_FREED_MANY = "%d more on the same site will be tried on the next round."


def skip_saved_words(report):
    """What follows "Saved: Skip jobs that need an account." for what that save did to the waiting
    applications (`set_account_setting`'s `report`), or None when it touched none."""
    closed, freed = len(report.get("closed") or ()), len(report.get("released") or ())
    if not closed:
        return None
    words = SKIP_CLOSED_ONE if closed == 1 else SKIP_CLOSED_MANY % closed
    return words + ("" if not freed else " " + (SKIP_FREED_ONE if freed == 1 else SKIP_FREED_MANY % freed))
ACCOUNT_SETTING_LABELS = {SIGN_IN_FOR_ME: "Sign in and create accounts for me", ASK: "Ask me each time",
                          SKIP: "Skip jobs that need an account"}
UNREADABLE = "UNREADABLE"   # `install_account_setting`: the database could not be read right now
UNREADABLE_WORDS = "Sidedoor couldn't read this setting just now. Reload the page in a moment."
NOT_CHOSEN_WORDS = "Not chosen yet. Until you choose, Sidedoor asks you each time."
# One line under the choices while "Sign in and create accounts for me" is offered (design: "A line explains
# that passwords are kept encrypted on the computer and listed under Saved sign-ins"; consent to terms said plainly).
# Owner's wording rule (2026-11-13): what is true and what the user must know, nothing about how it works.
ACCOUNT_SETTING_NOTE = "The first choice accepts each site's terms for you. Passwords stay on this computer."
SETTING_TABLE = "application_account_setting"
SETTING_RELEASED = "setting_changed"   # the outcome of a wait released because the setting changed
SETTING_RELEASED_WORDS = "Your setting changed. Sidedoor tries this application on its next round."

# What the session that met an account wall reports about it, inside its blocked result (`wall`):
# structured values only, cleaned by `clean_description`. Python compares these and never page text.
# profile_required (owner, 2026-11-14, "AI need to fill it out, if the user has the setting turned on"): the
# candidate is signed in and the site requires its own profile (or onboarding) completed before this
# application can be reached. The read reports it here, with the account-wall code; never a code of its own.
DESCRIPTION_FLAGS = ("sign_in_offered", "sign_up_offered", "apply_without_account_offered", "account_exists_signal",
                     "profile_required")
SSO_PROVIDERS = ("google", "linkedin", "microsoft", "apple", "other")
SECOND_STEPS = ("NONE", "EMAIL_CODE", "EMAIL_LINK", "SMS", "AUTHENTICATOR", "PASSKEY", "UNKNOWN")
# Single sign-on (E15.T9). Which sign-in provider a mailbox in Sidedoor belongs to, by Setup's "Your webmail"
# label (external_history.WEBMAIL_HOSTS): the name the session is told, nothing about that provider's pages
# or buttons (the AI finds those). A webmail not listed here has no such provider, so it never matches.
SINGLE_SIGN_ON_PROVIDERS = {"Gmail": "Google", "Outlook.com": "Microsoft", "Outlook (work or school)": "Microsoft",
                            "Yahoo Mail": "Yahoo", "iCloud Mail": "Apple"}
# What the session reports in `wall.single_sign_on` when it stopped on an account wall while single sign-on
# was allowed: why it did not get through. For the record only; Python decides nothing from it.
SINGLE_SIGN_ON_STOPS = ("NOT_OFFERED", "PASSWORD", "SECOND_STEP", "SITE_NOT_SIGNED_IN", "FAILED")

# Every way a wait closes, each with the job's blocker code and its plain words.
CLOSE_REASONS = {
    "ABANDONED": ("ACCESS_ABANDONED_BY_YOU", "You abandoned this application."),
    "WAITED_7_DAYS": ("ACCESS_WAITED_7_DAYS", "Sidedoor waited 7 days for you and closed this application."),
    # Past tense, true whatever the setting says now (owner, 2026-11-12, F4).
    "SKIPPED": ("ACCESS_SKIPPED_BY_SETTING", "Closed by your setting."),
    "POSTING_CLOSED": ("ACCESS_POSTING_CLOSED", "The posting closed while this application waited for you."),
}
CLOSE_CODES = frozenset(code for code, _ in CLOSE_REASONS.values())
CLOSE_WORDS = {code: words for code, words in CLOSE_REASONS.values()}

WAITING, RESOLVED, CLOSED = "WAITING", "RESOLVED", "CLOSED"
RECORDED_EVENT = "ACCESS_WAIT_RECORDED"
RELEASED_EVENT = "ACCESS_WAIT_RELEASED"
HOST_RELEASED = "host_released"   # the outcome of a host-held wait whose host is free again
# A host-held job never met the wall and never closes for it: its own true words (review round 3).
# The state only, for what actually holds the site (owner, 2026-11-14): a sign-in card, or a profile card.
# (Open profile questions and another job's profile step: `applications_page.PROFILE_HELD_WORDS`.)
HOST_HELD_WORDS = "Waiting for your sign-in on this site."
PROFILE_CARD_HELD_WORDS = "Waiting for this site's profile."


def held_words(reason):
    """What a job held behind its site says, by the reason of the wait that holds the site."""
    return PROFILE_HELD_WORDS if reason == "PROFILE_PENDING" else PROFILE_CARD_HELD_WORDS if reason in PROFILE_REASONS else HOST_HELD_WORDS
# A job released from a host hold had no problem: what its page says until its session starts.
# A job held behind another job's profile step or open profile questions on its site: nobody is asked to sign in.
PROFILE_HELD_WORDS = "Waiting for another application on this site."
HOST_RELEASED_WORDS = "Sidedoor tries this application on its next round."
JOB_FINISHED = "job_finished"     # the outcome of a wait whose job reached a final state another way
CLOSED_EVENT = "ACCESS_WAIT_CLOSED"
TABLE = "application_access_waits"


def initialize(database):
    database.executescript(f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            id INTEGER PRIMARY KEY, job_key TEXT NOT NULL, host TEXT, wall TEXT NOT NULL,
            reason TEXT NOT NULL, source TEXT NOT NULL, since TEXT NOT NULL, deadline TEXT NOT NULL,
            state TEXT NOT NULL, ended_at TEXT, outcome TEXT, updated_at TEXT NOT NULL, description TEXT,
            human_check TEXT);
        CREATE INDEX IF NOT EXISTS application_access_waits_job ON {TABLE}(job_key, state);
    """)
    _initialize_history(database)
    # A table made before E15.T6 gains the wall description; its rows read as "no description".
    if "description" not in {row[1] for row in database.execute(f"PRAGMA table_info({TABLE})")}:
        database.execute(f"ALTER TABLE {TABLE} ADD COLUMN description TEXT")
    # E16.T2: what is known about a human-check wait's check (`check_of`); older rows read as "nothing known".
    if "human_check" not in {row[1] for row in database.execute(f"PRAGMA table_info({TABLE})")}:
        database.execute(f"ALTER TABLE {TABLE} ADD COLUMN human_check TEXT")
    _initialize_setting(database)
    import access_step   # E15.T8: the steps Sidedoor takes itself under "Sign in and create accounts for me"
    access_step.initialize(database)


def _initialize_setting(database):
    database.execute(f"CREATE TABLE IF NOT EXISTS {SETTING_TABLE} "
                     "(id INTEGER PRIMARY KEY CHECK (id=1), value TEXT NOT NULL, set_at TEXT NOT NULL)")


def _setup_state(home=None):
    try:
        import setup_steps
        state = setup_steps.load_state(home)
    except Exception:
        return {}
    return state if isinstance(state, dict) else {}


def single_sign_on_provider(home=None):
    """The sign-in provider of the mailbox the user chose in Setup ("Your webmail", as saved: an install
    that never saved one has none), or None when that webmail has no such provider."""
    choice = _setup_state(home).get("webmail")
    return SINGLE_SIGN_ON_PROVIDERS.get(choice.get("label")) if isinstance(choice, dict) else None


def resolver_available():
    """True once Sidedoor can sign in or create an account itself: the access step (`access_step.py`,
    E15.T8). Settings then offers "Sign in and create accounts for me" and a stored SIGN_IN applies.
    This one place turns the whole path off again (Settings hides the choice, a stored SIGN_IN applies and
    shows as "Ask me each time", no step is planned or run)."""
    return True


def single_sign_on(home=None):
    """The single sign-on facts the access step is told (E15.T9): {"provider", "account"}, or None when
    the mailbox the user chose in Setup has no sign-in provider. `account` is the mailbox Setup's email
    check saw signed in in Sidedoor's browser (setup state `locked_email`, only when that check passed;
    the same file the provider comes from), never a job's own address; None when Setup recorded none."""
    provider = single_sign_on_provider(home)
    if not provider:
        return None
    state = _setup_state(home)
    account = state.get("locked_email") if state.get("email_check_passed_at") else None
    return {"provider": provider, "account": account if isinstance(account, str) and account else None}


def offered_settings():
    """The choices Settings shows and accepts now."""
    return tuple(value for value in ACCOUNT_SETTINGS if value != SIGN_IN_FOR_ME or resolver_available())


def account_setting(database):
    """The saved choice for "When a site needs an account": SIGN_IN, ASK or SKIP; None when the user
    has never chosen (or the row cannot be read as one of the three)."""
    try:
        return _read_setting(database)
    except (sqlite3.DatabaseError, AttributeError):
        return None


def _read_setting(database):
    if not database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (SETTING_TABLE,)).fetchone():
        return None
    row = database.execute(f"SELECT value FROM {SETTING_TABLE} WHERE id=1").fetchone()
    return row[0] if row and row[0] in ACCOUNT_SETTINGS else None


def effective_setting(database):
    """What applies to a wall now: never chosen is "Ask me each time" (owner, 2026-11-10), and so is
    "for me" until Sidedoor can do it (`resolver_available`)."""
    value = account_setting(database)
    if value == SKIP or (value == SIGN_IN_FOR_ME and resolver_available()):
        return value
    return ASK


def set_account_setting(database, value, now, report=None):
    """Save the choice (an unknown value raises ValueError, nothing written). Choosing "for me" is
    "something changed": `release_for_setting`. Choosing Skip closes the account-wall waits that are
    open, in this same transaction (`skip_open_waits`). Moving away from Skip reopens nothing (Try
    again is the user's way). Call inside a transaction. Returns the released job keys; `report` (a
    dict, optional) gets "closed" and "released": the keys of the waiting jobs this save closed, and of
    those it freed from behind them (F4, D9)."""
    if value not in ACCOUNT_SETTINGS:
        raise ValueError("ACCOUNT_SETTING_UNKNOWN")
    _lock(database)
    _initialize_setting(database)
    previous = account_setting(database)
    database.execute(f"INSERT INTO {SETTING_TABLE} VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value,"
                     "set_at=excluded.set_at", (value, now))
    if value == SKIP:
        done = skip_open_waits(database, now)
        if report is not None:
            report.update(closed=done["closed"], released=done["released"])
        return done["released"]
    return release_for_setting(database, now) if value == SIGN_IN_FOR_ME and previous != SIGN_IN_FOR_ME else []


def skip_open_waits(database, now):
    """The user chose Skip (owner, 2026-11-12): every open wait a job met itself behind an account
    wall closes SKIPPED, with Try again, as a wall met under Skip does. Never a human check, and never
    a job whose submission may have reached the employer (it keeps waiting). A job held behind a site
    never met a wall itself, so it is not closed: once nothing holds its site it is released for its
    own session, and Skip closes it only if that session meets the wall. Call inside a transaction.
    Returns {"closed": job keys, "released": job keys}."""
    done = {"closed": [], "released": []}
    if not present(database) or not database.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='application_jobs'").fetchone():
        return done
    _lock(database)
    rows = [dict(row) for row in database.execute(f"SELECT * FROM {TABLE} WHERE state=? ORDER BY id", (WAITING,)).fetchall()]
    for row in rows:
        if host_held(row) or row["reason"] not in ACCOUNT_REASONS:
            continue
        job = database.execute("SELECT * FROM application_jobs WHERE job_key=?", (row["job_key"],)).fetchone()
        if job and job["state"] not in _final() and close(database, dict(job), "SKIPPED", now):
            done["closed"].append(row["job_key"])
    for row in rows:
        if host_held(row) and not _finished(database, row["job_key"]) \
                and host_holder(database, row["host"], now, row["job_key"]) is None \
                and _release(database, row, now, row["source"][5:], HOST_RELEASED, HOST_RELEASED_WORDS):
            done["released"].append(row["job_key"])
    return done


def release_for_setting(database, now):
    """The user chose "Sign in and create accounts for me" and Sidedoor can (`resolver_available`):
    every job waiting because Sidedoor asks each time is released for one attempt. Nothing is released
    while Sidedoor cannot do it yet. Call inside a transaction. Returns the released job keys."""
    if not resolver_available() or not present(database):
        return []
    _lock(database)
    rows = [dict(row) for row in database.execute(
        f"SELECT * FROM {TABLE} WHERE state=? AND reason='ASK_EACH_TIME' ORDER BY id", (WAITING,)).fetchall()]
    return [row["job_key"] for row in rows if not _finished(database, row["job_key"])
            and _release(database, row, now, "setting", SETTING_RELEASED, SETTING_RELEASED_WORDS)]


def install_account_setting(root):
    """The saved choice as the applications run reads it, for Settings: a read-only look at the
    install's database that creates nothing. None when never chosen or when there is no database yet;
    UNREADABLE when the database could not be read right now (busy or damaged), which is not "never
    chosen": the page says so instead of showing a choice that may not be the one that applies."""
    import overview_page
    import paths
    path = paths.database(root)
    try:
        return overview_page.snapshot(path, lambda database: _read_setting(database) if database is not None else None,
                                      lambda: sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5))
    except sqlite3.DatabaseError:
        return UNREADABLE


def set_install_account_setting(value, *, root, now=None, report=None):
    """Settings' save: the same one row, in the install's database (created when no run has made it
    yet, as `approvals.set_install_mode` does). An unknown value raises ValueError before anything
    is written. Returns the released job keys."""
    import paths
    if value not in ACCOUNT_SETTINGS:
        raise ValueError("ACCOUNT_SETTING_UNKNOWN")
    path = paths.database(root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.closing(sqlite3.connect(path, timeout=10)) as database:
        database.row_factory = sqlite3.Row
        with database:
            return set_account_setting(database, value, now or dt.datetime.now(dt.timezone.utc).isoformat(), report)


# The release banner (E15.T10, R15): one banner for an install that finished Setup before the question
# was in it. The owner's sentence is "New: Sidedoor can sign in to job sites for you. Choose how.";
# its last two words are the link to the setting.
BANNER_TEXT = "New: Sidedoor can sign in to job sites for you."
BANNER_ACTION = "Choose how"
BANNER_HREF = "/settings#account-setting"
BANNER_DISMISS = "/account-banner/dismiss"
CHOICE_ERROR = "Choose one of the options shown."


def release_banner(root, finished, over):
    """The banner, or None. It shows while all three hold: Setup is finished, the banner is not over
    (`setup_steps.ACCOUNT_BANNER_OVER`: dismissed, or the question was in this install's Setup), and
    no choice is saved. A setting that cannot be read right now is not "no choice": no banner."""
    if not finished or over or install_account_setting(root) is not None:
        return None
    return {"tone": "info", "text": BANNER_TEXT, "action_label": BANNER_ACTION, "action_href": BANNER_HREF,
            "dismiss_action": BANNER_DISMISS}


def setup_question(root):
    """The same question inside Setup's "Review or automatic" step: the choices Settings offers, with
    the saved one ticked, or "Ask me each time" while none is saved (what applies until one is)."""
    saved, offered = install_account_setting(root), offered_settings()
    return {"title": ACCOUNT_SETTING_TITLE,
            "chosen": None if saved == UNREADABLE else saved if saved in offered else ASK,
            "unreadable": UNREADABLE_WORDS if saved == UNREADABLE else None,
            "options": [(value, ACCOUNT_SETTING_LABELS[value]) for value in offered],
            # The line Settings shows under the same choices, from the one constant.
            "note": ACCOUNT_SETTING_NOTE if SIGN_IN_FOR_ME in offered else None}


def origin_of(url):
    """scheme://host[:port] of a page address, lower case; None for anything that is not a web page."""
    try:
        parts = urllib.parse.urlsplit(url) if isinstance(url, str) else None
        if not parts or parts.scheme not in ("http", "https") or not parts.hostname:
            return None
        return "%s://%s%s" % (parts.scheme, parts.hostname.lower(), ":%d" % parts.port if parts.port else "")
    except ValueError:
        return None


def clean_description(value):
    """The model's wall description fitted to its contract: only the known keys, a wrong type or an
    unknown value dropped to None (second_step: UNKNOWN), never an error. None when it is not an
    object at all. `observed_origin` is not a key the model may set (see `description_of`)."""
    if not isinstance(value, dict):
        return None
    cleaned = {flag: value.get(flag) if isinstance(value.get(flag), bool) else None for flag in DESCRIPTION_FLAGS}
    providers = value.get("sso_providers")
    cleaned["sso_providers"] = ([name for name in SSO_PROVIDERS if any(isinstance(item, str) and item == name for item in providers)]
                                if isinstance(providers, list) else None)
    step = value.get("second_step")
    cleaned["second_step"] = step if isinstance(step, str) and step in SECOND_STEPS else "UNKNOWN"
    shown = value.get("signed_in_as")
    stopped = value.get("single_sign_on")
    if isinstance(stopped, str) and stopped in SINGLE_SIGN_ON_STOPS:   # absent unless reported (E15.T9)
        cleaned["single_sign_on"] = stopped
    cleaned["signed_in_as"] = (shown.strip() if isinstance(shown, str) and 3 <= len(shown.strip()) <= 254
                               and "@" in shown and not any(mark.isspace() for mark in shown.strip()) else None)
    return cleaned


def description_of(result):
    """The wall description a session result carries, as stored with its wait: the model's cleaned
    values (`blocked.wall`) plus `observed_origin`: the origin of the last page a navigation or
    snapshot answer of the browser showed in that session (`application_browser.normalize` writes
    `result["observed_origin"]`, after removing anything the model put under that name). It is an
    OBSERVATION, stored for the record only, and NOT trustworthy for any security decision: the model
    chooses where the session navigates last, so it can make this value any site it can open. The
    site a saved sign-in may be filled on is read by the guard from the live page at fill time, never
    from this value. None when the session reported no description (an older session or prompt)."""
    stop = result.get("blocked") if isinstance(result, dict) else None
    cleaned = clean_description(stop.get("wall")) if isinstance(stop, dict) else None
    if cleaned is None:
        return None
    return dict(cleaned, observed_origin=origin_of(result.get("observed_origin")))


# Where a human check sits, as a session reports it (E16.T2, owner 2026-11-12: "this should be gotten on
# read session"): BEFORE_FORM gates the page (a site check: its result can be carried in the site's saved
# session), ON_FORM sits on the application form itself (a one-time pass: done in the session that submits).
CHECK_PLACES = ("BEFORE_FORM", "ON_FORM")
# The kind of check, as the session names it from the widget itself: the order an attended session works
# in is one stored setting per kind (attended_submit.order_for). Anything else, or nothing: OTHER.
CHECK_KINDS = ("HCAPTCHA", "RECAPTCHA", "TURNSTILE", "OTHER")
CHECK_QUESTION_LIMIT = 1000


def clean_check(value):
    """A session's `human_check` report fitted to its contract, or None when it reports no check or
    nothing usable: {"place": BEFORE_FORM | ON_FORM, "after_question": for ON_FORM the zero-based
    position in the read's questions of the last question displayed above the check (-1: above every
    question), else None, "kind": one of CHECK_KINDS, "form_readable": True only when the session
    reported true (every question of the form is displayed without doing the check)}. Structured values only; never read from page text."""
    if not isinstance(value, dict) or not isinstance(value.get("place"), str) or value["place"] not in CHECK_PLACES:
        return None
    after = value.get("after_question")
    usable = (value["place"] == "ON_FORM" and isinstance(after, int) and not isinstance(after, bool)
              and -1 <= after < CHECK_QUESTION_LIMIT)
    kind = value.get("kind")
    return {"place": value["place"], "after_question": after if usable else None,
            "kind": kind if isinstance(kind, str) and kind in CHECK_KINDS else "OTHER",
            # True only when the session says so: an omitted value is not "readable" (review, 2026-11-13).
            "form_readable": value.get("form_readable") is True}


def reported_check(database, job_key, result):
    """Where the job's human check sits: as the session that stopped on it reported (`blocked.
    human_check`), else as the job's latest finished read reported (the read result's `human_check`).
    None when neither says."""
    stop = result.get("blocked") if isinstance(result, dict) else None
    found = clean_check(stop.get("human_check")) if isinstance(stop, dict) else None
    if found:
        return found
    try:
        row = database.execute(
            "SELECT result FROM desktop_requests WHERE job_key=? AND operation IN ('inspect','investigate') "
            "AND result IS NOT NULL ORDER BY rowid DESC LIMIT 1", (job_key,)).fetchone()
        read = json.loads(row[0]) if row else None
    except (sqlite3.Error, TypeError, ValueError):
        return None
    return clean_check(read.get("human_check")) if isinstance(read, dict) else None


def form_check(value):
    """The cleaned report when it is a human check ON THE FORM whose questions can all be read without
    doing it (owner, 2026-11-13): such a check does not stop a read, and the application goes on to
    answering, approval and an attended submit. None for anything else."""
    found = clean_check(value)
    return found if found and found["place"] == "ON_FORM" and found["form_readable"] else None


def waited_check(database, job_key):
    """What the job's newest wait recorded about its human check (`check`), or None: a job that has once
    waited on a form check is known to have one, whichever session found it."""
    if not present(database):
        return None
    row = database.execute(f"SELECT human_check FROM {TABLE} WHERE job_key=? ORDER BY id DESC LIMIT 1", (job_key,)).fetchone()
    return check(row) if row else None


def check_of(database, job, code, result):
    """What is known about the human check a job now waits on (E16.T2), stored with its wait, or None
    for any other wall. `place` and `after_question`: where it sits (`reported_check`); nothing reads
    the position yet. `timed_out`: the job's attended submit session held for the user and the hold
    ended at its time limit (form_check_hold.TIMED_OUT_EVENT on that request, written by the guard)."""
    if WALL_REASONS.get(code) != "HUMAN_CHECK":
        return None
    import form_check_hold
    known = dict(reported_check(database, job["job_key"], result) or {})
    if form_check_hold.timed_out(database, job["job_key"]):
        known["timed_out"] = True
    return known or None


def check(wait):
    """A wait's stored human-check record as a plain dict, or None (other walls, older rows)."""
    try:
        value = json.loads(wait["human_check"]) if wait["human_check"] else None
    except (TypeError, ValueError, KeyError, IndexError):
        return None
    return value if isinstance(value, dict) else None


def check_words(wait):
    """The sentence of a human-check wait: the owner's Y4 words after an attended hold timed out, else
    the standing one."""
    import form_check_hold
    known = check(wait)
    return form_check_hold.TIMED_OUT_WORDS if known and known.get("timed_out") is True else WAIT_REASONS["HUMAN_CHECK"]


def description(wait):
    """A wait's stored wall description as a plain dict, or None (rows from before E15.T6, human
    checks, host-held jobs, sessions that reported none)."""
    try:
        value = json.loads(dict(wait).get("description") or "null")
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def present(database):
    return bool(database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone())


def host_of(job):
    try:
        url = json.loads(job["payload"]).get("url") if job.get("payload") else None
    except (TypeError, ValueError, AttributeError):
        url = None
    return site(urllib.parse.urlsplit(url).hostname) if isinstance(url, str) else None


def site(host):
    """The host as the host rule compares it: a leading "www." is ignored (owner, 2026-11-12), nothing
    else. Other subdomains stay apart (jobs.site.com is not site.com; job boards are never merged).
    Applied when a wait's host is written and to both sides of every comparison, so a row written
    before this rule still matches."""
    host = (host or "").lower()
    return (host[4:] if host.startswith("www.") else host) or None


def open_wait(database, job_key):
    if not present(database):
        return None
    row = database.execute(f"SELECT * FROM {TABLE} WHERE job_key=? AND state=? ORDER BY id DESC LIMIT 1",
                           (job_key, WAITING)).fetchone()
    return dict(row) if row else None


def waiting(database, now=None):
    """Every open wait with what a card needs: job, employer, role, reason (key and words), since,
    deadline, the posting link and the state. Oldest first."""
    if not present(database):
        return []
    rows = database.execute(f"""SELECT wait.*, job.employer, job.payload, job.state AS job_state
        FROM {TABLE} AS wait JOIN application_jobs AS job ON job.job_key=wait.job_key
        WHERE wait.state=? ORDER BY wait.id""", (WAITING,)).fetchall()
    cards = []
    for row in rows:
        if row["job_state"] in _final():   # a finished job waits on nobody (`expire` ends the row)
            continue
        try:
            payload = json.loads(row["payload"]) or {}
        except (TypeError, ValueError):
            payload = {}
        payload = payload if isinstance(payload, dict) else {}
        cards.append({"job_key": row["job_key"], "employer": payload.get("company") or row["employer"],
                      "role": payload.get("role") or payload.get("title"), "url": payload.get("url"),
                      "host": row["host"], "wall": row["wall"], "reason": row["reason"],
                      "words": (held_words(row["reason"]) if host_held(row)
                                else check_words(row) if row["reason"] == "HUMAN_CHECK"
                                else WAIT_REASONS.get(row["reason"], WAIT_REASONS["SIGN_IN"])),
                      "since": row["since"], "deadline": row["deadline"], "state": row["state"],
                      "job_state": row["job_state"], "description": description(row)})
    return cards


def _lock(database):
    """Take the write lock before any check, so a check and the write it allows cannot be separated by
    another connection (`with database:` alone opens no transaction until the first write)."""
    if not database.in_transaction:
        database.execute("BEGIN IMMEDIATE")


def _final():
    import applications
    return applications.FINAL


# --- The history kept (owner, 2026-11-13, T7 b) -------------------------------------------------------
# The waits table is also the history the Metrics page counts. Its rows are kept for this many calendar
# months: the current month and the HISTORY_MONTHS - 1 before it, in the install's time zone, so a month is
# either wholly kept or wholly dropped. A row that is deleted is first counted into HISTORY_TABLE (one
# number per site and ending, so it is bounded by sites times endings, never by time), which the Metrics
# page adds to its "All time" numbers: they keep counting everything ever recorded. A row is counted
# exactly once across its life: in the waits table until it is trimmed, in HISTORY_TABLE after.
HISTORY_MONTHS = 24
HISTORY_TABLE = "application_access_history"


def _initialize_history(database):
    database.execute(f"CREATE TABLE IF NOT EXISTS {HISTORY_TABLE} (site TEXT NOT NULL, ending TEXT NOT NULL, "
                     "times INTEGER NOT NULL, PRIMARY KEY (site, ending))")


def _forget(database, identifier):
    """Delete one finished row (the last step of trimming it)."""
    database.execute(f"DELETE FROM {TABLE} WHERE id=? AND state IN (?,?)", (identifier, RESOLVED, CLOSED))


def history_start(now, zone):
    """The first instant (UTC, ISO) of the oldest month kept: 00:00 local on the first day of the month
    HISTORY_MONTHS - 1 before `now`'s month. `now`: a datetime or an ISO string; `zone`: a tzinfo."""
    moment = dt.datetime.fromisoformat(now) if isinstance(now, str) else now
    local = (moment if moment.tzinfo else moment.replace(tzinfo=dt.timezone.utc)).astimezone(zone)
    months = local.year * 12 + (local.month - 1) - (HISTORY_MONTHS - 1)
    return dt.datetime(months // 12, months % 12 + 1, 1, tzinfo=zone).astimezone(dt.timezone.utc).isoformat()


def trim_history(database, now, zone):
    """Delete the rows older than the history kept (`history_start`) that nothing can need again, each
    counted into HISTORY_TABLE first (under the ending the Metrics page counts it as today,
    `metrics_page.bucket`; a row held behind a site is in no number and adds nothing). Run by each
    applications round beside `expire`; cheap, and a second run finds nothing more. Call inside a
    transaction: the count and the delete are saved together or not at all. Returns the number of rows
    deleted.

    A row goes only when ALL of this holds:
    - it is finished (RESOLVED or CLOSED); a WAITING row is never touched, whatever its age;
    - the site asked before the oldest month kept (`since`);
    - its job can never be stopped or reopened again: the job is gone, or is in a final state on a
      reason other than this module's own closes (`CLOSE_CODES`).
    Kept on purpose, however old: every row of a job still in progress (a later wait's notice is numbered
    by the job's earlier rows, `record`), and every row of a job this module closed and Try again could
    still reopen (`try_again` reads the job's latest row). Such a row goes on being counted where it is."""
    import metrics_page
    if not present(database):
        return 0
    start = history_start(now, zone)
    _lock(database)
    gone = []
    for row in database.execute(f"SELECT * FROM {TABLE} WHERE state IN (?,?) AND since<? ORDER BY id",
                                (RESOLVED, CLOSED, start)).fetchall():
        try:
            met = dt.datetime.fromisoformat(str(row["since"]).replace("Z", "+00:00"))
        except ValueError:
            continue   # a time that cannot be read is not known to be old
        if (met if met.tzinfo else met.replace(tzinfo=dt.timezone.utc)) >= dt.datetime.fromisoformat(start):
            continue
        job = database.execute("SELECT state,blocker FROM application_jobs WHERE job_key=?", (row["job_key"],)).fetchone()
        if job is None or (job["state"] in _final() and str(job["blocker"] or "").split(":", 1)[0] not in CLOSE_CODES):
            gone.append(row)
    if gone:
        _initialize_history(database)
    for row in gone:
        ending = metrics_page.bucket(row)
        if ending:
            database.execute(f"INSERT INTO {HISTORY_TABLE}(site,ending,times) VALUES (?,?,1) "
                             "ON CONFLICT(site,ending) DO UPDATE SET times=times+1", (site(row["host"]) or "", ending))
        _forget(database, row["id"])
    return len(gone)


def _finished(database, job_key):
    """True when the wait's job is in a final state (or gone), whoever wrote that state."""
    job = database.execute("SELECT state FROM application_jobs WHERE job_key=?", (job_key,)).fetchone()
    return not job or job[0] in _final()


def host_held(wait):
    """True for a wait a job never met itself: it was held because its host's sign-in already waits."""
    return str(wait["source"] or "").startswith("host:")


def host_holder(database, host, now, job_key=None):
    """The wait that holds a host: an open sign-in wait a job met in its own session, not yet past its
    deadline. A host-held wait never holds the host, and neither does the wait of a job that has
    finished another way, nor the wait of a job whose submission may have reached the employer
    (`applications.uncertain_submission`; owner, 2026-11-12, D21): that job is being checked and is
    never retried or released here, so nothing the user does would ever free the site behind it. Its
    own row and wait are not touched by this. Of several, the latest deadline governs: the host is
    held until the last of them closes."""
    import applications
    if not site(host) or not present(database):
        return None
    rows = [row for row in database.execute(
        f"SELECT * FROM {TABLE} WHERE state=? AND job_key<>? AND deadline>? ORDER BY deadline DESC, id",
        (WAITING, job_key or "", now))
        if site(row["host"]) == site(host) and row["reason"] in HOLDING_REASONS and not host_held(row)
        and not _finished(database, row["job_key"])
        and not applications.uncertain_submission(database, row["job_key"])]
    if rows:
        return dict(rows[0])
    # A site's own profile is one per site (E15, review of the profile questions): another job's profile
    # step still to run, or its open questions about the site, hold the site too.
    import access_step
    return access_step.site_holder(database, host, job_key, now)


def own_session(database, job_key, now):
    """True while the job has a session of its own to read (owner, 2026-11-12: "Never hold a job behind
    a site while it has a session of its own to read"): a request in flight (PENDING or CLAIMED, not
    expired, any operation), or a finished read (inspect, investigate) still DONE inside the adapter's
    reuse window, which the adapter hands back as this tick's read. A finished read older than that is
    not one: the adapter reads afresh (a wall among those is `unread_wall`'s). A finished submit is not
    counted: a job with a submit to read is on the reconcile route and never reaches the host rule."""
    import desktop_adapter
    if not database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='desktop_requests'").fetchone():
        return False
    return bool(database.execute(
        "SELECT 1 FROM desktop_requests WHERE job_key=? AND ((state IN ('PENDING','CLAIMED') AND expires_at>?) "
        "OR (state='DONE' AND result IS NOT NULL AND operation IN ('inspect','investigate') AND created_at>?)) LIMIT 1",
        (job_key, now, desktop_adapter.later(now, -desktop_adapter.READ_REUSE_SECONDS))).fetchone())


def _end(database, wait, state, outcome, now):
    database.execute(f"UPDATE {TABLE} SET state=?,ended_at=?,outcome=?,updated_at=? WHERE id=? AND state=?",
                     (state, now, outcome, now, wait["id"], WAITING))


def record(database, job, code, now, reason=None, source="session", config=None, deadline=None, since=None, notice=None,
           result=None):
    """A wall stopped a session (or `wait_on_host` found the host already waiting): the job waits on
    the user for WAIT_DAYS, or, for an account wall under the "Skip" setting, closes at once. Call
    inside a transaction. `result` is the session's result, for its wall description.
    Returns the wait state written: WAITING or CLOSED, or None when the hold was refused (nothing written).

    An account wall a session met (E15.T6 stage 1; decided by the setting and the description's
    structured values only): Skip closes it (owner's pick R4 b: account walls only, a human check
    always waits); a site that signs in only with a passkey waits as PASSKEY (R8 b); otherwise, while
    Sidedoor asks (`effective_setting`), it waits as ASK_EACH_TIME."""
    import applications
    _lock(database)
    since = since or now   # when the wall was met; a result read late still counts its 7 days from then
    described = None
    if reason is None and WALL_REASONS.get(code, "SIGN_IN") == "SIGN_IN":
        described = description_of(result)
        setting = effective_setting(database)
        profile = bool(described and described.get("profile_required") is True)
        reason = ("PASSKEY" if described and described["second_step"] == "PASSKEY"
                  else "PROFILE_REQUIRED" if profile
                  else "ASK_EACH_TIME" if setting == ASK else "SIGN_IN")
        if setting == SKIP and not profile and _skip(database, job, code, reason, now, source, since, described):
            return CLOSED
        if setting == SIGN_IN_FOR_ME and reason in ("SIGN_IN", "PROFILE_REQUIRED") and source == "session" \
                and open_wait(database, job["job_key"]) is None:
            # E15.T8: Sidedoor's own step first (one per job and site until something changes); the job
            # goes on without a wait, or the step's outcome is the reason it now waits on the user.
            import access_step
            verdict, reason = access_step.on_wall(database, job, code, now, profile=profile)
            if verdict in (access_step.PLANNED, access_step.ASKED):
                return verdict   # a step is planned, or the user was asked on Approvals: no wait, no card
    reason = reason or WALL_REASONS.get(code, "SIGN_IN")
    deadline = deadline or (dt.datetime.fromisoformat(since) + dt.timedelta(seconds=WAIT_SECONDS)).isoformat()
    current = open_wait(database, job["job_key"])
    if current is not None:
        deadline = current["deadline"]
    # A passkey wait says the owner's words alone (2026-11-12); its 7 days still apply.
    known = check_of(database, job, code, result) if reason == "HUMAN_CHECK" and not source.startswith("host:") else None
    import form_check_hold
    action = held_words(reason) if source.startswith("host:") else WAIT_REASONS["PASSKEY"] if reason == "PASSKEY" else (
        (form_check_hold.TIMED_OUT_WORDS if known and known.get("timed_out") else WAIT_REASONS.get(reason, WAIT_REASONS["SIGN_IN"]))
        + " Sidedoor waits %d days, then closes this application." % WAIT_DAYS)
    # Route ORDINARY as today (the dashboard's words and counts are unchanged until the Needs you
    # cards, T2); `process` starts nothing for a job with an open wait. The hold comes first: a
    # refused hold (the job already reached a final state) leaves no waiting row behind.
    # `notice`: a site hold's notice has its own key, so the job's own wall later writes its own.
    # Each wait of the job's own is told once (owner: "Not silent misses"): its first wait on a wall
    # keeps the plain key `job:code` (the key a notice written before this rule has, so that wait is
    # never told twice), and its Nth later one has `job:code:WAIT:N`. The number is counted from
    # the wait rows, so every call for one wait gives one key.
    if notice is None and not source.startswith("host:") and not (current and host_held(current)):
        earlier = database.execute(
            f"SELECT COUNT(*) FROM {TABLE} WHERE job_key=? AND wall=? AND source NOT LIKE 'host:%' AND id<?",
            (job["job_key"], code, current["id"] if current else 1 << 62)).fetchone()[0] if present(database) else 0
        notice = "WAIT:%d" % (earlier + 1) if earlier else None
    if not applications.hold(database, job, code, action, now, "BLOCKED", deadline, route="ORDINARY", notice=notice):
        return None
    if current is None:
        # A repeated wall on one job after a release brings the hold back: a new row, never a close.
        _insert(database, job, code, reason, source, since, deadline, now, described, known)
        applications.event(database, job["job_key"], RECORDED_EVENT,
                           {"wall": code, "reason": reason, "source": source, "since": since, "deadline": deadline}, now)
    return WAITING


def _insert(database, job, code, reason, source, since, deadline, now, described=None, known=None):
    return database.execute(
        f"INSERT INTO {TABLE}(job_key,host,wall,reason,source,since,deadline,state,updated_at,description,human_check) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (job["job_key"], host_of(job), code, reason, source, since, deadline, WAITING, now,
         json.dumps(described, sort_keys=True) if described else None,
         json.dumps(known, sort_keys=True) if known else None)).lastrowid


def _skip(database, job, code, reason, now, source, since, described=None):
    """The "Skip" setting closes a job behind an account wall: its wait row is written and closed
    SKIPPED in the caller's one transaction, so Try again finds a wait this module closed and its
    guards apply. Never with a submission that may have reached the employer: False, and the caller
    records an ordinary wait instead (never a silent loss). Returns True when the job closed."""
    import applications
    if applications.uncertain_submission(database, job["job_key"]):
        return False
    created = None
    if open_wait(database, job["job_key"]) is None:
        deadline = (dt.datetime.fromisoformat(since) + dt.timedelta(seconds=WAIT_SECONDS)).isoformat()
        created = _insert(database, job, code, reason, source, since, deadline, now, described)
    if close(database, job, "SKIPPED", now, wall=code, reason=reason):
        return True
    if created is not None:   # the hold was refused (the job already finished another way): no row stays
        database.execute(f"DELETE FROM {TABLE} WHERE id=?", (created,))
    return False


def wait_on_host(database, job, now, config=None):
    """Cost rule: a job on a host whose sign-in wall is already waiting on the user starts no session;
    it waits too, on the same wall and until the same deadline (owner, 2026-11-12: the site frees up
    when that wait closes, see `expire`). Call inside a transaction. Returns True when the job now waits."""
    _lock(database)
    # Its own session comes first: a result of its own is read through the ordinary path (a wall there
    # is the job's own wait, a good read lets it proceed), never spent behind another job's wall.
    if own_session(database, job["job_key"], now):
        return False
    # A job just released from a host hold is owed its one session (owner, 2026-11-12: "each new job
    # after that gets its one attempt"): it is not held again behind a job released beside it. The
    # pass is that one session: once a session has started for the job after the release, it is gone.
    if released_pass(database, job["job_key"]):
        return False
    # Every open wait on the host is read: a human check waits per job and holds nobody else, so an
    # older one must not hide a sign-in wait behind it.
    other = host_holder(database, host_of(job), now, job["job_key"])
    if other is None:
        return False
    return record(database, job, other["wall"], now, reason=other["reason"], source="host:" + other["job_key"],
                  config=config, deadline=other["deadline"], notice="SITE_HOLD:%d" % other["id"]) is not None


def released_pass(database, job_key):
    """True while a job released from a host hold has not yet started the one session it is owed: its
    latest wait is `host_released` and no session has picked up a request made for the job since (a
    request no session ever claimed is a compute wait, not its session; while one is still queued,
    `own_session` keeps the job from being held)."""
    last = database.execute(f"SELECT outcome,updated_at FROM {TABLE} WHERE job_key=? ORDER BY id DESC LIMIT 1",
                            (job_key,)).fetchone() if present(database) else None
    if not last or last[0] != HOST_RELEASED:
        return False
    if not database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='desktop_requests'").fetchone():
        return True
    return not database.execute("SELECT 1 FROM desktop_requests WHERE job_key=? AND created_at>=? AND claims>0 LIMIT 1",
                                (job_key, last[1])).fetchone()


def _stopped_reads(database, job_key):
    """The job's finished, never-armed, not yet consumed sessions that stopped on a wall:
    (request id, wall code, created at), oldest first."""
    import applications
    if not database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='desktop_requests'").fetchone():
        return []
    found = []
    for request_id, raw, created in database.execute(
            "SELECT request_id,result,created_at FROM desktop_requests WHERE job_key=? AND state='DONE' AND armed_at IS NULL "
            "AND operation IN ('inspect','investigate','submit') AND result IS NOT NULL ORDER BY rowid", (job_key,)).fetchall():
        try:
            code = applications.stop_code(json.loads(raw))
        except (TypeError, ValueError):
            continue
        if code in WALL_REASONS:
            found.append((request_id, code, created))
    return found


def _spend_stopped_reads(database, job_key):
    """The session that met the wall is spent: its stored result must not be replayed as the next
    read (the desktop adapter reuses a DONE read for READ_REUSE_SECONDS), so it is marked CONSUMED."""
    for request_id, _, _ in _stopped_reads(database, job_key):
        database.execute("UPDATE desktop_requests SET state='CONSUMED' WHERE request_id=?", (request_id,))


def usable_wall(database, request, now):
    """(wall code, when it was met) when a finished request is a wall result the job must wait on, not
    read again; else None. The one answer both sides use: `unread_wall` (which records the wait) and
    the desktop adapter (which never queues a fresh read over such a result, see `DesktopAdapter.
    reusable`). The rules (owner, 2026-11-12, review round 3): a never-armed DONE inspect,
    investigate or submit that stopped on a wall, the job's newest request (the last
    `desktop_requests` row of the job, of any operation and any state: a later read that got
    through, a later request in flight or expired, a later submit or reconcile each show the job
    moved on), not from before the job's latest wait (that wait answered it), and less than
    WAIT_DAYS old (older: the user never saw a card for it)."""
    import applications
    import desktop_adapter
    if (not request or request["state"] != "DONE" or request["armed_at"] or not request["result"]
            or request["operation"] not in ("inspect", "investigate", "submit")):
        return None
    try:
        code = applications.stop_code(json.loads(request["result"]))
    except (TypeError, ValueError):
        return None
    if code not in WALL_REASONS:
        return None
    newest = database.execute("SELECT request_id FROM desktop_requests WHERE job_key=? ORDER BY rowid DESC LIMIT 1",
                              (request["job_key"],)).fetchone()
    if newest[0] != request["request_id"]:
        return None
    last = database.execute(f"SELECT updated_at FROM {TABLE} WHERE job_key=? ORDER BY id DESC LIMIT 1",
                            (request["job_key"],)).fetchone() if present(database) else None
    if last and request["created_at"] < last[0]:
        return None
    met = database.execute("SELECT at FROM desktop_events WHERE request_id=? AND event='RESULT_COMMITTED' ORDER BY id LIMIT 1",
                           (request["request_id"],)).fetchone()
    since = met[0] if met else request["created_at"]
    return (code, since) if now < desktop_adapter.later(since, WAIT_SECONDS) else None


def reused_wall_since(database, job_key, code, now):
    """When the wall was met, for a wait about to be recorded from a read the adapter handed back past
    READ_REUSE_SECONDS (`DesktopAdapter.reusable`): the job's newest request is then that usable wall
    result, and its time is the one `unread_wall` would have used. None for a read inside the window
    (the wait counts from now, as it always has) and when the newest request is anything else."""
    import desktop_adapter
    request = database.execute("SELECT * FROM desktop_requests WHERE job_key=? ORDER BY rowid DESC LIMIT 1", (job_key,)).fetchone() \
        if database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='desktop_requests'").fetchone() else None
    usable = usable_wall(database, request, now)
    if not usable or usable[0] != code or now < desktop_adapter.later(request["created_at"], desktop_adapter.READ_REUSE_SECONDS):
        return None
    return usable[1]


def unread_wall(database, job, now, config=None):
    """Before a new session: a wall result nobody read in time (owner, 2026-11-12: "A wall does not go
    stale the way page content does"). The desktop adapter reuses a finished read only for
    READ_REUSE_SECONDS, so a wall result first looked at later than that was ignored and a new session
    started on the same wall. Such a result records the wait here, counted from when the session
    ended on the wall, and no session starts, but only if it is the job's newest request and less
    than WAIT_DAYS old (owner, 2026-11-12, review round 3). Newest means the last `desktop_requests`
    row of the job, of any operation and any state: a later read that got through, a later request
    still in flight or expired, a later submit or reconcile each show the job moved on after that
    wall. Any other late wall result is only marked CONSUMED (a fresh session, as before this
    module): an older one, one from before the job's latest wait (that wait answered it), or one
    whose 7 days are already over (the user never saw a card for it). A result still inside the reuse
    window is left to the ordinary path. Call inside a transaction. Returns True when the job now waits or closed."""
    import desktop_adapter
    _lock(database)
    late = [item for item in _stopped_reads(database, job["job_key"])
            if now >= desktop_adapter.later(item[2], desktop_adapter.READ_REUSE_SECONDS)]
    if not late:
        return False
    usable = usable_wall(database, database.execute("SELECT * FROM desktop_requests WHERE request_id=?", (late[-1][0],)).fetchone(), now)
    for request_id, _, _ in late:
        database.execute("UPDATE desktop_requests SET state='CONSUMED' WHERE request_id=?", (request_id,))
    if not usable:
        return False
    try:
        result = json.loads(database.execute("SELECT result FROM desktop_requests WHERE request_id=?", (late[-1][0],)).fetchone()[0])
    except (TypeError, ValueError):
        result = None
    return record(database, job, usable[0], now, config=config, since=usable[1], result=result) is not None


def _release(database, item, now, released_by, outcome="released", action="Continue the application after your step"):
    """One wait ends RESOLVED and its job is due again. Returns True when this call released it.
    Never a job whose submission may have reached the employer (`applications.uncertain_submission`,
    the guard `close` and `try_again` use; owner, 2026-11-13): its wait and its state stay exactly as
    they are, on every path that releases (`resume`, a site release in `expire`)."""
    import applications
    if applications.uncertain_submission(database, item["job_key"]):
        return False
    ended = database.execute(f"UPDATE {TABLE} SET state=?,ended_at=?,outcome=?,updated_at=? WHERE id=? AND state=?",
                             (RESOLVED, now, outcome, now, item["id"], WAITING))
    if ended.rowcount != 1:
        return False
    changed = database.execute(
        "UPDATE application_jobs SET state='RETRY_WAIT',blocker=NULL,next_action=?,next_at=NULL,updated_at=? "
        "WHERE job_key=? AND state='BLOCKED' AND blocker=?", (action, now, item["job_key"], item["wall"]))
    if changed.rowcount == 1:
        database.execute("DELETE FROM application_blockers WHERE job_key=?", (item["job_key"],))
    _spend_stopped_reads(database, item["job_key"])
    applications.event(database, item["job_key"], RELEASED_EVENT,
                       {"wall": item["wall"], "reason": item["reason"], "released_by": released_by,
                        "outcome": outcome, "since": item["since"]}, now)
    return True


def resume(database, job_key, now, siblings=True):
    """The user acted (a sign-in or a cleared check, T2/T5) or something changed: the hold is
    released and the job is due again on the next round. A sign-in on a host releases
    the other jobs waiting on that host's sign-in too (R6: a step done once is not asked twice).
    Returns the released job keys."""
    _lock(database)
    wait = open_wait(database, job_key)
    if wait is None:
        return []
    released = [wait]
    if siblings and wait["host"] and wait["reason"] in ACCOUNT_REASONS | PROFILE_REASONS:
        # Every account wait on the site, whatever its reason (E15.T6): one left behind under another
        # reason (a row from before the setting) would hold the site against the job just released,
        # which would then wait again without its session. A passkey wait too (owner, 2026-11-12: "if
        # the user can successfully sign in, then it should release it"): the site now has a signed-in
        # session, so that job gets one more attempt; a passkey prompt again is a new wait and card.
        released += [dict(row) for row in database.execute(
            f"SELECT * FROM {TABLE} WHERE state=? AND job_key<>?", (WAITING, job_key)).fetchall()
            if site(row["host"]) == site(wait["host"]) and row["reason"] in ACCOUNT_REASONS | PROFILE_REASONS]
    return [item["job_key"] for item in released if _release(database, item, now, job_key)]


# E16.T3 (owner, 2026-11-10; the read path, 2026-11-13): "If the page times out after the user's step: one
# retry in a new session with the saved result, without spending one of the job's attempts." For a check
# that gates the site the user's step is the live view on the card and the next session is a read.
FREE_READ_EVENT = "READ_NOT_COUNTED_TIMEOUT_AFTER_USER_STEP"
READ_TIMEOUT_CODE = "APPLICATION_BROWSER_TIMEOUT"


def free_read_after_step(database, job, error, now):
    """A read failed (`error`, as `applications.process` caught it). True when it is the one free retry:
    the read timed out, it is the first read since the user's step released the job's human-check wait, and
    that step has not had its free retry yet. Then only FREE_READ_EVENT is written: no try is spent, the
    read is not counted against the read limit (`free_reads`), no recovery is opened, and the job stays due,
    so the next round reads again in a new session (which starts from the site's saved session). A second
    timeout is an ordinary failed read; a site that asks again is a new wait and its card."""
    import applications
    code = getattr(error, "code", None) or str(error)
    request_id = getattr(error, "request_id", None)
    if code != READ_TIMEOUT_CODE or not isinstance(request_id, str) or not present(database):
        return False
    _lock(database)
    wait = database.execute(
        f"SELECT * FROM {TABLE} WHERE job_key=? AND state=? AND outcome='released' ORDER BY id DESC LIMIT 1",
        (job["job_key"], RESOLVED)).fetchone()
    if wait is None or wait["reason"] != "HUMAN_CHECK" or not wait["ended_at"]:
        return False
    since = [row[0] for row in database.execute(
        "SELECT request_id FROM desktop_requests WHERE job_key=? AND operation='inspect' AND created_at>=? ORDER BY rowid",
        (job["job_key"], wait["ended_at"]))]
    if since[:1] != [request_id]:
        return False   # not the first read after the step
    if database.execute("SELECT 1 FROM application_events WHERE job_key=? AND event=? AND json_extract(detail,'$.wait')=? LIMIT 1",
                        (job["job_key"], FREE_READ_EVENT, wait["id"])).fetchone():
        return False
    applications.event(database, job["job_key"], FREE_READ_EVENT,
                       {"wait": wait["id"], "request_id": request_id, "code": code}, now)
    return True


def free_reads(database, job_key):
    """The request ids of the job's reads that `free_read_after_step` did not count."""
    found = set()
    for (detail,) in database.execute("SELECT detail FROM application_events WHERE job_key=? AND event=?",
                                      (job_key, FREE_READ_EVENT)):
        try:
            found.add(json.loads(detail)["request_id"])
        except (TypeError, ValueError, KeyError):
            continue
    return found


def close(database, job, why, now, wall=None, reason=None):
    """Close a walled job with its reason: ABANDONED (the user), WAITED_7_DAYS, SKIPPED (the setting)
    or POSTING_CLOSED. The job is ABANDONED under the reason's code and keeps Try again. Never with a
    submission that may have reached the employer. Call inside a transaction. Returns True when closed."""
    import applications
    _lock(database)
    code, words = CLOSE_REASONS[why]
    if applications.uncertain_submission(database, job["job_key"]):
        return False
    wait = open_wait(database, job["job_key"])
    if not applications.hold(database, job, code, words + ("" if why == "POSTING_CLOSED" else " Apply yourself if you want this job."), now, "ABANDONED"):
        return False
    if wait is not None:
        _end(database, wait, CLOSED, why, now)
    applications.event(database, job["job_key"], CLOSED_EVENT,
                       {"why": why, "code": code, "wall": wall or (wait or {}).get("wall"),
                        "reason": reason or (wait or {}).get("reason"), "since": (wait or {}).get("since"),
                        "submission_status": "NOT_SUBMITTED"}, now)
    return True


def abandon(database, job_key, now):
    """The user's Abandon (the card button is T2)."""
    _lock(database)
    job = database.execute("SELECT * FROM application_jobs WHERE job_key=?", (job_key,)).fetchone()
    return bool(job) and close(database, dict(job), "ABANDONED", now)


def posting_closed(database, job_key, now):
    """The posting closed or was removed while the job waited: closed, with that reason."""
    _lock(database)
    job = database.execute("SELECT * FROM application_jobs WHERE job_key=?", (job_key,)).fetchone()
    if not job or open_wait(database, job_key) is None:
        return False
    return close(database, dict(job), "POSTING_CLOSED", now)


def expire(database, now, only=None):
    """Every wait a job met itself closes with "waited 7 days" past its deadline. A host-held wait
    never closes that way (owner, 2026-11-12: its job never had a session): it is released, to get
    its one session, as soon as nothing holds its host any more, which is at the holding wait's
    deadline or earlier when that wait ends another way. Call inside a transaction.
    Returns the closed job keys."""
    if not present(database):
        return []
    _lock(database)
    closed = []
    # A wait whose job is already finished ends here, whoever finished the job (owner, 2026-11-12:
    # "End a wait whenever its job is already finished"; a writer that skips `applications.hold` never
    # reaches `settle`). It is not a close: no "waited 7 days", nothing for Try again to reopen.
    for row in database.execute(f"SELECT * FROM {TABLE} WHERE state=? ORDER BY id", (WAITING,)).fetchall():
        if (only is None or row["job_key"] in only) and _finished(database, row["job_key"]):
            _end(database, row, RESOLVED, JOB_FINISHED, now)
    rows = [dict(row) for row in database.execute(f"SELECT * FROM {TABLE} WHERE state=? ORDER BY id", (WAITING,)).fetchall()]
    for row in rows:
        if host_held(row) or row["deadline"] > now or (only is not None and row["job_key"] not in only):
            continue
        job = database.execute("SELECT * FROM application_jobs WHERE job_key=?", (row["job_key"],)).fetchone()
        if job and close(database, dict(job), "WAITED_7_DAYS", now):
            closed.append(row["job_key"])
    for row in rows:
        if not host_held(row) or (only is not None and row["job_key"] not in only):
            continue
        if host_holder(database, row["host"], now, row["job_key"]) is None:
            _release(database, row, now, row["source"][5:], HOST_RELEASED, HOST_RELEASED_WORDS)
    return closed


def settle(database, job_key, state, code, now):
    """A job with an open wait reached a final state by another path (submitted, or closed by research
    or a read): the wait record follows it, so no finished job shows as waiting. Called by
    `applications.hold`."""
    wait = open_wait(database, job_key)
    if wait is None:
        return
    if state == "SUBMITTED":
        _end(database, wait, RESOLVED, "submitted", now)
    elif code not in CLOSE_CODES:
        _end(database, wait, CLOSED, str(code or state), now)


# The reopen record recovery reads (`application_recovery.reopen_scope`, `application_connection.
# inspect_scope_start`, `reporting.authorized_reopens`): the owner's reopen tool wrote these two tables
# beside each APPLICATION_REOPENED; Try again writes the same rows. Same schema, created when absent.
REOPEN_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS application_reopen_grants (
        authorization_id TEXT PRIMARY KEY, authorized_by TEXT NOT NULL, job_keys TEXT NOT NULL,
        investigation_claims INTEGER NOT NULL, deadline_hours REAL NOT NULL,
        authorized_at TEXT NOT NULL, expires_at TEXT NOT NULL, authorization TEXT NOT NULL)""",
    """CREATE TRIGGER IF NOT EXISTS application_reopen_grants_no_update BEFORE UPDATE ON application_reopen_grants
        BEGIN SELECT RAISE(ABORT, 'Immutable reopen authorization'); END""",
    """CREATE TRIGGER IF NOT EXISTS application_reopen_grants_no_delete BEFORE DELETE ON application_reopen_grants
        BEGIN SELECT RAISE(ABORT, 'Immutable reopen authorization'); END""",
    """CREATE TABLE IF NOT EXISTS application_reopen_grant_jobs (
        authorization_id TEXT NOT NULL, job_key TEXT NOT NULL, reopened_at TEXT NOT NULL,
        reopen_event_id INTEGER NOT NULL, defect_classes TEXT NOT NULL, original_abandoned_at TEXT NOT NULL,
        original_reason TEXT NOT NULL, original_attempts INTEGER NOT NULL,
        deadline TEXT NOT NULL, PRIMARY KEY(authorization_id,job_key))""",
    """CREATE TRIGGER IF NOT EXISTS application_reopen_grant_jobs_no_update BEFORE UPDATE ON application_reopen_grant_jobs
        BEGIN SELECT RAISE(ABORT, 'Immutable reopen consumption'); END""",
    """CREATE TRIGGER IF NOT EXISTS application_reopen_grant_jobs_no_delete BEFORE DELETE ON application_reopen_grant_jobs
        BEGIN SELECT RAISE(ABORT, 'Immutable reopen consumption'); END""",
)
# A reopen has no deadline (owner, 2026-09-28); the grant columns are NOT NULL, so they hold this.
NO_DEADLINE = "9999-12-31T23:59:59+00:00"
# Why a Try again was refused, in plain words.
TRY_AGAIN_REFUSED = {
    "NOT_CLOSED": "This application is not closed.",
    "NOT_CLOSED_BY_A_WALL": "This application was not closed while it waited for you.",
    "SUBMISSION_UNCERTAIN": "This application may already have reached the employer, so Sidedoor will not apply again.",
}


def recovery_try_again(state, code):
    """True for a job the user may try again although no wall closed it (owner, 2026-11-13, G2 and G4):
    parked for the user after the AI's answers could not be used, parked with its reads spent, or closed
    by the old rule after failed AI calls (`application_recovery.relabel_failed_closures`). Never a job
    the AI itself decided to abandon: that one keeps its code and is not here."""
    import application_recovery
    code = str(code or "")
    return ((state == "BLOCKED" and code in (application_recovery.NEEDS_YOU_BLOCKER, application_recovery.CLAIMS_EXHAUSTED_BLOCKER))
            or (state == "ABANDONED" and code == application_recovery.NOT_DECIDED_BLOCKER))


def try_again(database, job_key, now):
    """Try again on a closed card: the existing APPLICATION_REOPENED, written by the dashboard (the
    button is T2). Only for a job this module closed behind a wall (its latest wait is CLOSED with one
    of CLOSE_REASONS and the job still carries that reason's code), and never for one whose submission
    may have reached the employer (`applications.uncertain_submission`, the guard `close` uses). The
    job is queued again; its closing stays in the ledger. A refusal changes nothing. Call inside a
    transaction. Returns {"reopened": True} or {"reopened": False, "why": key, "words": plain words}."""
    import uuid
    import application_recovery
    import applications
    refused = lambda why: {"reopened": False, "why": why, "words": TRY_AGAIN_REFUSED[why]}
    _lock(database)
    job = database.execute("SELECT * FROM application_jobs WHERE job_key=?", (job_key,)).fetchone()
    # A job parked because its reads of the posting are spent (BLOCKED, never retried on its own; "only an
    # explicit owner reopen releases this job", application_recovery): the user's Try again is that reopen
    # (owner, 2026-11-14). So is it for a job parked after answers that could not be used, and for one the
    # old rule closed after failed AI calls (owner, 2026-11-13; `recovery_try_again` names all three).
    # The grant written below starts a new read scope.
    recovery = bool(job) and recovery_try_again(job["state"], job["blocker"])
    if not job or (job["state"] not in ("ABANDONED", "EXHAUSTED") and not recovery):
        return refused("NOT_CLOSED")
    wait = database.execute(f"SELECT * FROM {TABLE} WHERE job_key=? ORDER BY id DESC LIMIT 1",
                            (job_key,)).fetchone() if present(database) else None
    if not recovery and (not wait or wait["state"] != CLOSED or wait["outcome"] not in CLOSE_REASONS
                         or job["blocker"] != CLOSE_REASONS[wait["outcome"]][0]):
        return refused("NOT_CLOSED_BY_A_WALL")
    if applications.uncertain_submission(database, job_key):
        return refused("SUBMISSION_UNCERTAIN")
    # The check and the write are one statement: the job is reopened only from the very state and
    # reason read above, so a job that moved on in between is never reset.
    changed = database.execute(
        "UPDATE application_jobs SET state='QUEUED',blocker=NULL,next_action=?,next_at=NULL,attempts=0,updated_at=? "
        "WHERE job_key=? AND state=? AND blocker=? AND attempts=? AND updated_at=?",
        ("Read the posting again", now, job_key, job["state"], job["blocker"], job["attempts"], job["updated_at"]))
    if changed.rowcount != 1:
        return refused("NOT_CLOSED")
    authorization = "try-again-" + uuid.uuid4().hex
    claims = application_recovery.MAX_RECOVERY_ATTEMPTS
    applications.event(database, job_key, "APPLICATION_REOPENED", {
        "authorization_id": authorization, "authorized_by": "user:dashboard",
        "reason": {"words": "Try again", "defect_classes": []}, "allowance": {"investigation_claims": claims},
        "previous_state": {"state": job["state"], "blocker": job["blocker"], "attempts": job["attempts"]},
        "submission_status": "NOT_SUBMITTED", "history_preserved": True}, now)
    reopened = database.execute("SELECT last_insert_rowid()").fetchone()[0]
    for statement in REOPEN_SCHEMA:
        database.execute(statement)
    database.execute("INSERT INTO application_reopen_grants VALUES (?,?,?,?,?,?,?,?)",
                     (authorization, "user:dashboard", json.dumps([job_key]), claims, 0, now, NO_DEADLINE, "Try again"))
    database.execute("INSERT INTO application_reopen_grant_jobs VALUES (?,?,?,?,?,?,?,?,?)",
                     (authorization, job_key, now, reopened, "[]", job["updated_at"], str(job["blocker"]),
                      job["attempts"], NO_DEADLINE))
    database.execute("DELETE FROM application_blockers WHERE job_key=?", (job_key,))
    _spend_stopped_reads(database, job_key)
    return {"reopened": True}
