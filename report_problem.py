"""Report a problem (owner pick F3 a, 2026-10-23): the button on the Failures page and in Settings.

`POST /report-problem` builds a privacy-safe diagnostic summary: the Sidedoor version and the
computer's platform, every `sidedoor doctor` check (name, OK/WARN/FAIL and its short line, already
redacted by `doctor.redacted`), and the codes of the latest failures the Failures page lists (codes
only, never their words). The page copies it to the clipboard and opens GitHub's bug-report form
(`issue_url`: only the version and the platform go in the address). Nothing is sent anywhere.

Never in the summary: keys, tokens, the license key (not even masked), the dashboard token, email
addresses, names, phone numbers, LinkedIn or any other web address, company or job names (failure
words are left out), résumé text, or paths under the user's home folder (shown as `~`). Each line
goes through the Failures page's redaction (`failures_page.secrets_of`, which includes
`credentials.values`) and then `scrub`, which also removes the profile's own personal values.

The endpoint sits behind the dashboard's `guard` like every other POST: the session cookie and an
Origin of this dashboard (CSRF), else 403.
"""

import datetime as dt
import platform
import re
import sys
import urllib.parse
from pathlib import Path

ISSUE_FORM = "https://github.com/yoursidedoor/sidedoor/issues/new?template=bug_report.yml"
CODE_LIMIT = 8
PLATFORMS = ("macOS Apple chip", "macOS Intel", "Windows")
NOTE = ("Your summary is copied, not sent. Paste it into the issue. "
        "Account or billing questions: support@yoursidedoor.com")

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_URL = re.compile(r"(?i)\b(?:https?://|www\.)\S+|\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b(?:[/:]\S*)?")   # links and bare domains
_MASKED = re.compile(r"(?:\bSD-)?\.\.\.[A-Za-z0-9_-]{2,8}|\bSD-[A-Za-z0-9_-]+")
_PHONE = re.compile(r"\+?\d[\d ().-]{7,}\d")
# Every filesystem path (RP3): POSIX absolute, ~-paths, Windows drive paths (any letter, OneDrive
# included) and UNC shares. A path runs on across spaces while the next word continues it with a
# separator ("~/Library/Application Support/Sidedoor"); trailing punctuation stays outside.
_SEGMENT = r"[^\s\"'<>|]*"
_PATH = re.compile(r"(?<![\w:/.~\\])(?:~(?=[/\\\s.,;:)]|$)|/(?=[^\s/])|[A-Za-z]:[\\/]|\\\\(?=[^\s\\]))"
                   + _SEGMENT + r"(?:(?: [^\s/\\]+){1,3}?(?=[/\\])" + _SEGMENT + r")*")
_CODE = re.compile(r"[A-Za-z0-9_:.-]{1,64}")
PERSONAL_KEYS = ("name", "email", "phone", "linkedin", "location", "address", "city", "zip", "postal",
                 "school", "title", "employer", "website", "github")
COMMON_WORDS = {"the", "and", "for", "with", "not", "any", "all", "per", "via", "from", "your", "you", "are", "usa"}
REFUSED = "Sidedoor couldn't build a safe summary right now. Try again in a minute."


class Unsafe(Exception):
    """Something the summary's scrubs need could not be read: no summary is built (fail closed)."""


def platform_label(system=None, machine=None):
    """One of PLATFORMS (the bug-report form's choices), else "Other"."""
    system = system or sys.platform
    machine = (machine or platform.machine() or "").lower()
    if system == "darwin":
        return "macOS Apple chip" if machine in ("arm64", "aarch64") else "macOS Intel"
    if system.startswith("win"):
        return "Windows"
    return "Other"


def issue_url(version, label):
    """The new-issue address: the fixed form prefix plus only `version` and `platform`."""
    return ISSUE_FORM + "&" + urllib.parse.urlencode({"version": str(version), "platform": str(label)})


def allowed_url(url):
    """True only for ISSUE_FORM followed by version and platform (what the page may open)."""
    if not isinstance(url, str) or not url.startswith(ISSUE_FORM + "&"):
        return False
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
    return (parts.scheme, parts.netloc, parts.path) == ("https", "github.com", "/yoursidedoor/sidedoor/issues/new") \
        and set(query) == {"template", "version", "platform"} and query["template"] == ["bug_report.yml"]


def personal_values(home):
    """The profile's own strings, longest first: every string in profile.json (each whole and word by
    word, common words aside) and the personal fields of the Setup form."""
    import doctor
    import setup_steps
    found = []
    form = setup_steps.profile_form(home)
    found += [value for key, value in form.items() if isinstance(value, str)
              and any(word in key.lower() for word in PERSONAL_KEYS)]

    def walk(value):   # every string leaf of the profile: values, notes and sources alike
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            found.append(value)
    walk(doctor._profile(home))   # raises when the profile is missing or unreadable (RP1)
    words = set()
    for value in found:
        value = value.strip()
        if len(value) >= 3:
            words.add(value)
            words.update(part for part in re.split(r"[\s,]+", value)
                         if len(part) >= 3 and part.casefold() not in COMMON_WORDS)
    return sorted(words, key=len, reverse=True)


_WORD = re.compile(r"[^\W_]+")
SHINGLE = 3   # three résumé words in a row are taken as résumé text


def _normal(word):
    return word.casefold()


def shingles(text):
    """Every run of SHINGLE consecutive words of `text` (the résumé), normalised."""
    words = [_normal(word) for word in _WORD.findall(text or "")]
    return {tuple(words[index:index + SHINGLE]) for index in range(len(words) - SHINGLE + 1)}


def _without_resume(text, resume):
    if not resume:
        return text
    found = list(_WORD.finditer(text))
    marked = set()
    for index in range(len(found) - SHINGLE + 1):
        if tuple(_normal(match.group()) for match in found[index:index + SHINGLE]) in resume:
            marked.update(range(index, index + SHINGLE))
    for index in sorted(marked, reverse=True):
        match = found[index]
        text = text[:match.start()] + "[résumé]" + text[match.end():]
    return re.sub(r"\[résumé\](?:\W*\[résumé\])+", "[résumé]", text)


def scrub(text, personal=(), homes=(), resume=()):
    """`text` without home paths (shown as `~`), addresses, links, phone numbers, masked keys, any
    of the `personal` values (the profile's, and company and contact names on record) or any run of
    SHINGLE words from the résumé (`resume`, from `shingles`)."""
    text = _without_resume(str(text or ""), resume)
    for home in sorted({str(h) for h in homes if h and len(str(h)) > 1}, key=len, reverse=True):
        text = text.replace(home, "~")
    text = _PATH.sub(lambda match: _kept_punctuation(match, "[path]"), text)
    text = _EMAIL.sub("[email]", text)
    text = _URL.sub(lambda match: _kept_punctuation(match, "[link]"), text)
    text = _MASKED.sub("[key]", text)
    text = _PHONE.sub("[number]", text)
    for value in personal:   # whole words only, so a short name never eats part of another word
        text = re.sub(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(value), "[personal]", text, flags=re.IGNORECASE)
    return text


def recorded_names(database):
    """Company and contact names on record (applications, research), to keep them out of the summary.
    A table that is not there yet holds no names; any other read error raises (RP1: fail closed)."""
    import sqlite3
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    names = []
    for table, column in (("application_jobs", "employer"), ("rw_companies", "company"),
                          ("rw_contacts", "company"), ("rw_contacts", "name")):
        if table in tables:
            names += [row[0] for row in database.execute("SELECT DISTINCT %s FROM %s" % (column, table))
                      if isinstance(row[0], str)]
    return names


def _kept_punctuation(match, word):
    """`word` for the match, keeping the sentence's own punctuation after it."""
    found = match.group()
    return word + found[len(found.rstrip(".,;:)")):]


def failure_codes(database, config, loop, limit=CODE_LIMIT):
    """The codes of the newest failures on the Failures page (`failures_page.gather`), newest first."""
    import failures_page
    view = failures_page.gather(database, config, loop)
    entries = list(view["needs_you"]) + [entry for section in view["sections"] for entry in section["entries"]]
    oldest = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    entries.sort(key=lambda entry: entry["at"] or oldest, reverse=True)
    codes = []
    for entry in entries:
        code = entry.get("code")
        if isinstance(code, str) and _CODE.fullmatch(code):
            codes.append(code)
        if len(codes) >= limit:
            break
    return codes


def summary(version, label, checks, codes, personal=(), homes=(), hidden=None, resume=()):
    """The text pasted into the issue."""
    out = ["Sidedoor diagnostic summary", "Version: %s" % version, "Platform: %s (%s)" % (label, platform.machine() or "unknown"), ""]
    if hidden:
        out += [hidden, ""]
    else:
        out.append("Checks:")
        for check in checks:
            line = scrub(check.get("line") or "", personal, homes, resume)
            if check.get("name") == "LinkedIn":   # RP2: never the account's display name
                line = "connected" if check.get("status") == "OK" else "not connected"
            out.append("%s  %s: %s" % (check.get("status"), scrub(check.get("name"), personal, homes, resume), line))
        codes = [code for code in codes if scrub(code, personal, homes, resume) == code]   # a code is shown whole or not at all
        out += ["", "Recent failure codes: %s" % (", ".join(codes) or "none")]
    return "\n".join(out) + "\n"


def build(home, root, config, token):
    """(summary text, issue url) for this install."""
    import doctor
    import failures_page
    import overview_page
    import paths
    import setup_steps
    import version as version_module
    label, current = platform_label(), version_module.current()
    homes = [Path.home(), Path(home).resolve() if home else None, home, Path(root).resolve() if root else None, root]
    homes = [str(h) for h in homes if h]
    if not failures_page._redaction_complete(home):
        return summary(current, label, [], [], hidden=failures_page.DETAILS_HIDDEN), issue_url(current, label)
    running = setup_steps.scheduler_running(home)
    loop = overview_page.loop_state(running)
    checks = doctor.run(home=home, root=root, config=config, running=running, loop=loop)
    secrets = failures_page.secrets_of(home, config, token)
    checks = [{key: failures_page.redact_text(value, secrets) if key in ("name", "line") and value else value
               for key, value in check.items()} for check in checks]
    database = paths.database(root)

    def read(connection):
        if connection is None:
            return [], []
        return failure_codes(connection, config, loop), recorded_names(connection)
    try:   # RP1: a read the scrubs need that fails refuses the whole summary
        codes, names = overview_page.snapshot(database, read,
                                              lambda: overview_page._connect(database.resolve().as_uri() + "?mode=ro"))
        profile = doctor._profile(home)
        resume = set()
        if profile.get("resume_raw"):   # no résumé saved: nothing of it to keep out
            import reply_resume
            with doctor._home_variable(home):
                resume = shingles(reply_resume.context(root)["text"])
        mine = personal_values(home)
    except Exception as error:
        raise Unsafe(type(error).__name__) from None
    personal = mine + [name.strip() for name in names if len(name.strip()) >= 3] \
        + [value for value in secrets if isinstance(value, str) and len(value) >= 3]
    personal = sorted(set(personal), key=len, reverse=True)
    text = summary(current, label, checks, codes, personal, homes, resume=resume)
    return text, issue_url(current, label)


def register(app, home, root, config, token):
    """POST /report-problem -> {"summary", "url"}, or 503 {"error": REFUSED} when a summary can't be
    made safe (nothing is copied or opened); behind the dashboard's guard (cookie + Origin)."""
    from fastapi.responses import JSONResponse
    from starlette.concurrency import run_in_threadpool

    @app.post("/report-problem", name="report_problem")
    async def report_problem():
        try:
            text, url = await run_in_threadpool(build, home, root, config, token)
        except Unsafe:
            return JSONResponse({"error": REFUSED}, status_code=503)
        if not allowed_url(url):
            return JSONResponse({"error": "refused"}, status_code=500)
        return JSONResponse({"summary": text, "url": url})
