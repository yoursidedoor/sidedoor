"""Whole log and report files for troubleshooting (owner L1 and L2, 2026-11-18).

The Logs list names every file in the profile's logs/ and reports/ folders (a readable name, when it
was last written, its part and kind) and each downloads whole, every line passed through the page's
redaction rule (`reply_worker.redact_line`). "Download for support" is a .zip of every file written in
the last SUPPORT_SPAN plus the scheduler stream tails; a failure card's "Download details" is a .zip of
the files its run names. A file is only ever named by its id ("logs/<name>", "reports/<folder>/<name>"),
never by a path from the browser, and it is served only when it resolves to a regular file inside its
folder with no linked step on the way. Reads only."""

import datetime as dt
import heapq
import io
import json
import os
import re
import stat
import tempfile
import urllib.parse
import zipfile
from pathlib import Path

import failures_page as fp
import overview_page as overview
import paths

SUFFIXES = (".log", ".json", ".jsonl", ".txt")
SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z")
MAX_DEPTH = 6                 # reports/agent-worker/<task>/<attempt>/<file>, session subfolders (owner X3)
MAX_LISTED = 5000             # the newest files listed per folder (owner X4)
CHUNK = 64 * 1024
SUPPORT_SPAN = dt.timedelta(days=2)
REMOVED = "[line removed: it may hold a secret]"
UNREADABLE = "[the rest of this file could not be read]"
CHECKPOINT_BYTES = 8 * 1024 * 1024
DETAIL_KINDS = ("research", "dir", "job", "finder")

_STREAM = re.compile(r"(launchd|research|applications|sidedoor)\.(stdout|stderr)\.log(\.retained\.[0-9]{20}\.[0-9a-f]{32})?\Z")
_RESEARCH = re.compile(r"([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\.(?:([a-z0-9-]*-[0-9a-f]{10})\.)?(.*)\Z")
_STEP = re.compile(r"([0-9]{1,6})\.([a-z]+)(\.processed)?\.(log|json)\Z")
_CHECKPOINT = re.compile(r"parallel-industry-([0-9]{4}-[0-9]{2}-[0-9]{2})-([a-z0-9-]*-[0-9a-f]{10})\.scope-[0-9a-f]+\.json\Z")
PHASE_WORDS = {"discovery": "search", "qualification": "company check"}
FILE_WORDS = {"stderr.log": "errors", "executor.log": "log", "output.json": "result", "decision.json": "decision",
              "trace.jsonl": "steps"}


def _wanted(name):
    return name.endswith(SUFFIXES) or bool(_STREAM.fullmatch(name))


def _bases(root, logs):
    return {paths.LOGS: Path(logs), paths.REPORTS: paths.reports(root)}


def _industry(label):
    """"automotive-eb589cfb23" -> "Automotive" (the slug parallel_research._label made)."""
    words = label.rsplit("-", 1)[0].replace("-", " ").strip()
    return words[:1].upper() + words[1:] if words else "An industry"


def _when(at, zone):
    local = at.astimezone(zone) if zone else at
    return "%s %d %s" % (local.strftime("%b"), local.day, local.strftime("%H:%M"))


def _plain(name):
    stem = name.split(".", 1)[0].replace("-", " ").replace("_", " ").strip()
    return stem[:1].upper() + stem[1:] if stem else name


def _research_words(rest, label):
    """The words after "Research, <industry>, " for a research run's file `rest` (after its label)."""
    if rest == "industry.json":
        return "progress"
    step = _STEP.fullmatch(rest)
    if step:
        number, phase, processed, suffix = step.groups()
        words = "%s %s" % (PHASE_WORDS.get(phase, phase), number)
        return words + (" results" + (", sorted" if processed else "") if suffix == "json" else "")
    if rest.endswith(".engine.json"):
        return _research_words(rest[:-len(".engine.json")], label) + ", run details"
    return _plain(rest) if rest not in ("log", "parallel.json") else ("run log" if rest == "log" else "summary")


_RAW_ID = re.compile(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b|\b(?=[0-9a-f]*[0-9])[0-9a-f]{6,}\b")


def describe(key, parts, at, zone=None, names=None):
    """(component, level, label) for the file at `parts` under logs/ or reports/ (`key`): a name with no
    raw id in it (owner V3), ending with its local date and time when `at` is known (owner V2)."""
    component, level, label = _describe(key, parts, names or {})
    label = re.sub(r"\s*,(\s*,)+", ",", re.sub(r"\s{2,}", " ", _RAW_ID.sub("", label))).strip(" ,-")
    return component, level, label + (", " + _when(at, zone) if at is not None else "")


def _describe(key, parts, names):
    name = parts[-1]
    error = "stderr" if "stderr" in name or "error" in name.lower() else "stdout"
    labels = dict(fp.COMPONENTS)
    if len(parts) == 1:
        stream = _STREAM.fullmatch(name)
        if stream:
            component, level, retained = stream.groups()
            return component, level, "%s %s%s" % (labels[component], "errors" if level == "stderr" else "output",
                                                  ", earlier copy" if retained else "")
        research = _RESEARCH.fullmatch(name)
        if research:
            _, label, rest = research.groups()
            if label:
                return "research", error, "Research, %s, %s" % (_industry(label), _research_words(rest, label))
            return "research", error, "Research run, %s" % _research_words(rest, None)
        checkpoint = _CHECKPOINT.fullmatch(name)
        if checkpoint:
            return "research", "stdout", "Research, %s, saved progress for %s" % (
                _industry(checkpoint.group(2)), fp._day_label(checkpoint.group(1)))
        if name.startswith("research"):
            return "research", error, _plain(name)
        if name.startswith("application"):
            return "applications", error, _plain(name)
        if name.startswith(("linkedin", "outreach")):
            return "launchd", error, _plain(name)
        return "sidedoor", error, _plain(name)
    base = parts[0]
    component = fp.FOLDER_COMPONENTS.get(base, "sidedoor")
    words = fp.FOLDER_WORDS.get(base) or _plain(base)
    about = names.get((base, parts[1])) if len(parts) > 2 and base in NAMED_KINDS else None
    # Owner Q2: what each folder below the session means ("attempt 2"), so files of different attempts differ.
    middle = ["attempt " + part if part.isdigit() else _plain(part) for part in parts[2:-1]]
    return component, error, ", ".join([words] + ([about] if about else []) + middle + [FILE_WORDS.get(name, _plain(name))])


def _walk(base, depth=MAX_DEPTH):
    """[(parts, path, lstat)] of the wanted regular files under `base` at most `depth` steps down, never
    through a link: the newest MAX_LISTED by modification time when there are more (owner X4)."""
    found = []

    def visit(folder, parts):
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            return
        for name in names:
            if not SEGMENT.match(name):
                continue
            path = folder / name
            try:
                info = os.lstat(path)
            except OSError:
                continue
            if stat.S_ISDIR(info.st_mode) and len(parts) + 1 < depth:
                visit(path, parts + [name])
            elif stat.S_ISREG(info.st_mode) and _wanted(name):
                found.append((parts + [name], path, info))

    try:
        if base.is_dir() and not base.is_symlink():
            visit(base, [])
    except OSError:
        pass
    return heapq.nlargest(MAX_LISTED, found, key=lambda item: item[2].st_mtime) if len(found) > MAX_LISTED else found


NAMED_KINDS = ("application-browser", "linkedin-contacts", "application-recovery")   # folders named by a record


class Lazy:
    """Session folder names read once, on the first `get` (owner Q4: only when a name is shown); `source`
    is the dict itself or a function returning it."""

    def __init__(self, source):
        self.source, self.value = source, (None if callable(source) else (source or {}))

    def get(self, key, default=None):
        if self.value is None:
            try:
                found = self.source()
            except Exception:
                found = None
            self.value = found if isinstance(found, dict) else {}
        return self.value.get(key, default)


def _jobs_words(reader, keys):
    """{job key: "Company, Role"} for `keys`, one query per 500 keys (owner Q4)."""
    keys, words = sorted(set(keys)), {}
    for start in range(0, len(keys), 500):
        batch = keys[start:start + 500]
        for found in reader.rows("SELECT job_key,employer,payload FROM application_jobs WHERE job_key IN (%s)"
                                 % ",".join("?" * len(batch)), tuple(batch), "application_jobs"):
            payload = overview._json(found["payload"])
            payload = payload if isinstance(payload, dict) else {}
            company = payload.get("company") or found["employer"]
            role = payload.get("role") or payload.get("title")
            if company:
                words[found["job_key"]] = str(company)[:80] + (", " + str(role)[:80] if role else "")
    return words


def _session_of(root, folder, base):
    """The <kind>/<session> folder name that `folder` (a session folder or one inside it) belongs to (owner Q3)."""
    try:
        return folder.resolve().relative_to((paths.reports(root) / base).resolve()).parts[0]
    except (OSError, ValueError, IndexError):
        return None


def folder_names(reader, root):
    """{(folder kind, session folder name): what it was about} from the records that name each session
    folder (read only): a browser session's job company and role, a contact finder run's company, a
    recovery decision's job (`failures_page.run_names`). Folders nothing names are absent."""
    found, sessions = {}, {}
    for row in reader.rows("SELECT job_key,result FROM desktop_requests WHERE result LIKE '%application-browser%'", (),
                           "desktop_requests"):
        value = overview._json(row["result"])
        folder = fp._folder_of(root, value, "application-browser") if isinstance(value, dict) else None
        session = _session_of(root, folder, "application-browser") if folder is not None else None
        if session and row["job_key"]:
            sessions.setdefault(session, row["job_key"])
    words = _jobs_words(reader, sessions.values())
    for session, key in sessions.items():
        if key in words:
            found[("application-browser", session)] = words[key]
    for row in reader.rows("SELECT company,result FROM linkedin_contact_runs WHERE result LIKE '%linkedin-contacts%'", (),
                           "linkedin_contact_runs"):
        value = overview._json(row["result"])
        folder = fp._folder_of(root, value, "linkedin-contacts") if isinstance(value, dict) else None
        session = _session_of(root, folder, "linkedin-contacts") if folder is not None else None
        company = (value or {}).get("company") or row["company"]
        if session and company:
            found.setdefault(("linkedin-contacts", session), str(company)[:80])
    for name, words in (fp.run_names(reader) or {}).items():
        found.setdefault(("application-recovery", name), words)
    return found


def all_files(root, logs, zone=None, names=None):
    """Every log and report file, newest first: {id, name, label, component, component_label, level,
    level_label, at, when, size}."""
    labels, levels = dict(fp.COMPONENTS), dict(fp.LEVELS)
    names = names if isinstance(names, Lazy) else Lazy(names)
    entries = []
    for key, base in _bases(root, logs).items():
        for parts, path, info in _walk(base):
            at = dt.datetime.fromtimestamp(info.st_mtime, dt.timezone.utc)
            component, level, label = describe(key, parts, at, zone, names)
            entries.append({"id": key + "/" + "/".join(parts), "key": key, "parts": parts, "name": parts[-1], "label": label,
                            "component": component,
                            "component_label": labels.get(component, component), "level": level,
                            "level_label": levels[level], "at": at, "when": _when(at, zone), "size": info.st_size})
    entries.sort(key=lambda entry: entry["at"], reverse=True)
    return entries


def files_paged(root, logs, component="all", level="all", day="", zone=None, page=1, names=None):
    """(files, paging): the Logs list filtered by part, kind and day (the user's date), RUNS_LISTED a page.
    `names` (a dict or a function) is read only when the page shows a file of a named session folder."""
    found = all_files(root, logs, zone)
    if component != "all":
        found = [entry for entry in found if entry["component"] == component]
    if level != "all":
        found = [entry for entry in found if entry["level"] == level]
    if isinstance(day, str) and fp._DAY.match(day):
        found = [entry for entry in found if fp.local_day(entry["at"], zone) == day]
    shown, paging = fp.page_of(found, page, fp.RUNS_LISTED)
    names = Lazy(names)
    for entry in shown:
        if entry["key"] == paths.REPORTS and len(entry["parts"]) > 2 and entry["parts"][0] in NAMED_KINDS:
            entry["label"] = describe(entry["key"], entry["parts"], entry["at"], zone, names)[2]
    return shown, paging


def resolve(root, logs, file_id):
    """The file `file_id` names, or None: only a regular file inside logs/ or reports/, every step on
    the way a real folder (no link), at most MAX_DEPTH deep and of a listed kind."""
    if not isinstance(file_id, str):
        return None
    key, _, rest = file_id.partition("/")
    bases = _bases(root, logs)
    parts = rest.split("/") if rest else []
    if key not in bases or not parts or len(parts) > MAX_DEPTH or not all(SEGMENT.match(part) for part in parts) \
            or not _wanted(parts[-1]):
        return None
    base = bases[key]
    path = base
    try:
        for part in parts:
            path = path / part
            if stat.S_ISLNK(os.lstat(path).st_mode):
                return None
        if not stat.S_ISREG(os.lstat(path).st_mode) or not path.resolve().is_relative_to(base.resolve()):
            return None
    except (OSError, ValueError):
        return None
    return path


def id_of(root, logs, value):
    """The id of a recorded absolute path when it lies inside logs/ or reports/, else None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        path = Path(paths.relocated(value))
    except Exception:
        path = Path(value)
    for key, base in _bases(root, logs).items():
        for candidate, folder in ((path, base), (path.parent.resolve() / path.name, base.resolve())):
            try:
                return key + "/" + candidate.relative_to(folder).as_posix()
            except (OSError, ValueError):
                continue
    return None


def _open_regular(path):
    """A binary reader of the regular file at `path`, never through a link (the checks `_read_bytes`
    makes: lstat, O_NOFOLLOW, O_NONBLOCK, fstat), or None."""
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            return None
        return os.fdopen(descriptor, "rb", buffering=CHUNK)
    except OSError:
        os.close(descriptor)
        return None


def redacted_lines(handle, secrets, problems=None):
    """Each line of the open file `handle`, read one at a time, through `reply_worker.redact_line` (a
    line it drops is marked), ending in a newline; the handle is closed at the end. A read that fails
    part way ends with UNREADABLE (owner Q5) and appends to `problems`."""
    import reply_worker
    fragments = fp._fragments(secrets)
    try:
        for raw in handle:
            for line in raw.decode("utf-8", "replace").splitlines() or [""]:
                kept = reply_worker.redact_line(fp._ESCAPES.sub("", line), secrets, fragments)
                yield (REMOVED if kept is None else kept) + "\n"
    except OSError:
        if problems is not None:
            problems.append(True)
        yield UNREADABLE + "\n"
    finally:
        handle.close()


def file_bytes(path, secrets):
    """A generator of the whole redacted file as UTF-8, a line at a time, or None when it cannot be opened."""
    handle = _open_regular(path) if path is not None else None
    return None if handle is None else (line.encode("utf-8") for line in redacted_lines(handle, secrets))


def chunks(handle):
    """A finished temporary .zip, in CHUNK pieces; the file is closed (and so removed) at the end."""
    try:
        handle.seek(0)
        while True:
            piece = handle.read(CHUNK)
            if not piece:
                break
            yield piece
    finally:
        handle.close()


def _zip(members, index_head, secrets):
    """A temporary file holding a .zip of `members` [(arcname, path or None, label, part, kind, when,
    note)], each written entry by entry and line by line (never whole in memory), and index.txt. A
    member whose file cannot be opened is left out and index.txt says so with `note`."""
    output = tempfile.TemporaryFile()
    index = [index_head, ""]
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for arcname, path, label, part, kind, when, note in members:
            handle = path if hasattr(path, "read") else (_open_regular(path) if path is not None else None)
            if handle is not None:
                problems = []
                with archive.open(arcname, "w", force_zip64=True) as entry:
                    for line in redacted_lines(handle, secrets, problems):
                        entry.write(line.encode("utf-8"))
                note = "could not be read" if problems else None
            elif not note:
                note = "could not be read"
            index.append(" | ".join(value for value in (label, part, kind, when, arcname, note) if value))
        archive.writestr("index.txt", "\n".join(index) + "\n")
    return output


def support_zip(root, logs, secrets, zone=None, now=None, names=None):
    """(filename, temporary file): every log and report file written in the last SUPPORT_SPAN and every
    current scheduler stream file (owner X5), whole and redacted, and index.txt."""
    now = now or dt.datetime.now(dt.timezone.utc)
    since = now - SUPPORT_SPAN
    members = []
    streams = {paths.LOGS + "/" + name for name in fp.stream_names()}
    listed = set()
    for entry in all_files(root, logs, zone, names):
        if entry["at"] < since and entry["id"] not in streams:
            continue
        listed.add(entry["id"])
        members.append((entry["id"], resolve(root, logs, entry["id"]), entry["label"], entry["component_label"],
                        entry["level_label"], None, "could not be read"))
    labels, levels = dict(fp.COMPONENTS), dict(fp.LEVELS)
    for name in fp.stream_names():
        if paths.LOGS + "/" + name not in listed:
            component, level = name.split(".")[:2]
            members.append((paths.LOGS + "/" + name, None, "%s %s" % (labels[component], "errors" if level == "stderr" else "output"),
                            labels[component], levels[level], None, "no file yet"))
    head = ("Sidedoor logs and reports from the last 2 days and the current scheduler logs, secrets removed. Made %s."
            % _when(now, zone) + " Each line: name | part | kind | file | note.")
    return "sidedoor-support-%s.zip" % fp.local_day(now, zone), _zip(members, head, secrets)


def details_href(log_href):
    """The Download details link for a card whose run (its See its log link) names files, else None."""
    if not isinstance(log_href, str):
        return None
    run = (urllib.parse.parse_qs(urllib.parse.urlsplit(log_href).query).get("run") or [""])[0]
    if run.split(":", 1)[0] not in DETAIL_KINDS:
        return None
    return "/failures/details.zip?" + urllib.parse.urlencode({"run": run})


def _folder_ids(root, logs, folder):
    base = paths.reports(root)
    try:
        prefix = folder.relative_to(base).as_posix()
    except ValueError:
        return []
    return ["/".join([paths.REPORTS, prefix] + parts)
            for parts, _, _ in _walk(folder, MAX_DEPTH - len(prefix.split("/")))]


def run_files(reader, root, logs, run):
    """(slug, at, [recorded ids or file names]) for a failure's run reference, or None when it names no
    files. Research: each industry that did not finish (or the one named) with its step log, report,
    progress file and saved progress, and the log and report every recorded failure of it names."""
    match = fp._RUN.match(run or "")
    if not match:
        return None
    kind, ident = match.groups()
    named, slug, at = [], kind, None
    if kind == "research":
        attempt, _, label = ident.partition(":")
        if not fp._RESEARCH_ID.fullmatch(attempt) or (label and not re.fullmatch(r"[a-z0-9-]{1,80}", label)):
            return None
        row = reader.rows("SELECT started_at,result FROM research WHERE id=?", (attempt,), "research")
        result = (overview._json(row[0]["result"]) if row else None) or {}
        at = overview._utc(row[0]["started_at"]) if row and row[0]["started_at"] else None
        industries = [entry for entry in result.get("industries") or [] if isinstance(entry, dict) and entry.get("industry")]
        if label:
            chosen = [entry for entry in industries if fp._research_label(str(entry["industry"])) == label]
        else:
            chosen = [entry for entry in industries if str(entry.get("state") or "").upper()
                      not in ("COMPLETE",) + overview.RESEARCH_WAITING] or industries
        reports = paths.reports(root)
        for entry in chosen:
            own = fp._research_label(str(entry["industry"]))
            named += [entry.get(key) for key in ("log", "report", "discovery_report", "checkpoint")]
            named.append(str(reports / ("%s.%s.industry.json" % (attempt, own))))
            day = result.get("day")
            checkpoints = sorted(reports.glob("parallel-industry-%s-%s.scope-*.json" % (day, own))) \
                if isinstance(day, str) and fp._DAY.match(day) else []
            for checkpoint in [Path(value) for value in (entry.get("checkpoint"),) if isinstance(value, str)] + checkpoints:
                named.append(str(checkpoint))
                named += _failure_paths(checkpoint)
        if not chosen:   # no result recorded: the run's own files by name, as its log view lists them
            prefix = attempt + "." + (label + "." if label else "")
            named += [str(base / name) for base in _bases(root, logs).values() if base.is_dir()
                      for name in sorted(os.listdir(base)) if name.startswith(prefix)]
        slug = _industry(fp._research_label(str(chosen[0]["industry"]))).lower().replace(" ", "-") \
            if len(chosen) == 1 else "research"
    elif kind == "dir":
        base, _, name = ident.partition("/")
        if base not in fp.FOLDER_WORDS or not fp._FOLDER_NAME.match(name) or name in (".", ".."):
            return None
        folder = paths.reports(root) / base / name
        if not fp._inside(folder, paths.reports(root) / base):
            return None
        named, slug = _folder_ids(root, logs, folder), base
    elif kind == "finder":
        row = reader.rows("SELECT started_at,result FROM linkedin_contact_runs WHERE run_id=?", (ident,), "linkedin_contact_runs")
        value = (overview._json(row[0]["result"]) if row else None) or {}
        folder = fp._folder_of(root, value, "linkedin-contacts") if isinstance(value, dict) else None
        named, slug = (_folder_ids(root, logs, folder) if folder else []), "contact-finder"
        at = overview._utc(row[0]["started_at"]) if row and row[0]["started_at"] else None
    elif kind == "job":
        slug = "application"
        for request in reader.rows("SELECT result FROM desktop_requests WHERE job_key=? ORDER BY created_at DESC LIMIT 3",
                                   (ident,), "desktop_requests"):
            value = overview._json(request["result"]) or {}
            folder = fp._folder_of(root, value, "application-browser") if isinstance(value, dict) else None
            named += _folder_ids(root, logs, folder) if folder else []
    else:
        return None
    return re.sub(r"[^a-z0-9-]+", "-", slug.lower()).strip("-") or "details", at, named


def _failure_paths(checkpoint):
    """The log and report each recorded failure in a saved-progress file names."""
    try:
        if checkpoint.is_symlink() or checkpoint.stat().st_size > CHECKPOINT_BYTES:
            return []
        value = json.loads(checkpoint.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    failures = value.get("failures") if isinstance(value, dict) else None
    return [failure.get(key) for failure in failures if isinstance(failure, dict) for key in ("log", "report")] \
        if isinstance(failures, list) else []


def details_zip(root, logs, secrets, found, zone=None, now=None, names=None):
    """(filename, temporary file) for `run_files`' result: each named file whole and redacted, and index.txt (a
    file no longer there is listed as such)."""
    slug, at, named = found
    now = now or dt.datetime.now(dt.timezone.utc)
    names = Lazy(names)
    labels, levels = dict(fp.COMPONENTS), dict(fp.LEVELS)
    members, seen = [], set()
    for value in named:
        file_id = value if isinstance(value, str) and value.split("/", 1)[0] in (paths.LOGS, paths.REPORTS) \
            and not os.path.isabs(value) else id_of(root, logs, value)
        if not file_id or file_id in seen:
            continue
        seen.add(file_id)
        parts = file_id.split("/")
        path = resolve(root, logs, file_id)
        moment = None
        if path is not None:
            try:
                moment = dt.datetime.fromtimestamp(os.lstat(path).st_mtime, dt.timezone.utc)
            except OSError:
                path = None
        component, level, label = describe(parts[0], parts[1:], moment, zone, names)
        members.append((file_id, path, label, labels.get(component, component), levels[level], None,
                        "no longer on this computer"))
    head = "Sidedoor files for one problem, secrets removed. Made %s. Each line: name | part | kind | file | note." \
        % _when(now, zone)
    return "sidedoor-details-%s-%s.zip" % (slug, fp.local_day(at or now, zone)), _zip(members, head, secrets)


# --- One row per operation (owner L1, L2, L3: 2026-11-22) -----------------------------------------------------------
# Every file of one operation (a browser session, one research stage of one industry, a decision, a contact
# search, one stream and its earlier copies) is one row, named from the record that says what it was about.
# The index costs one listing of logs/, reports/ and each session folder kind (one lstat per folder, not per
# file) and one pass over the naming records; the files of an operation are walked only for the rows shown.

OPERATION_KINDS = (("application", "Application"), ("posting", "Posting check"), ("research", "Research"),
                   ("research-recovery", "Research recovery"), ("saved", "Saved progress"),
                   ("contacts", "Contact finding"), ("outreach", "Outreach"), ("message", "Message"),
                   ("sidedoor", "Sidedoor"))
SESSION_KINDS = {   # reports/<folder>: (operation kind, words when nothing names it, component)
    "application-browser": ("application", "Browser session", "applications"),
    "application-recovery": ("application", "Recovery decision", "applications"),
    "application-connection-manual": ("application", "Manual check", "applications"),
    "email-check": ("application", "Email check", "applications"),
    "posting-judge": ("posting", "Posting check", "applications"),
    "research-recovery": ("research-recovery", "Research recovery", "research"),
    "linkedin-contacts": ("contacts", "Contact finding", "launchd"),
    "outreach-history": ("outreach", "Outreach history check", "launchd"),
    "linkedin-session": ("outreach", "Networking page read", "launchd"),
    "live-view": ("sidedoor", "Live view session", "sidedoor"),
    "support-email": ("sidedoor", "Support email", "sidedoor"),
    "agent-worker": ("outreach", "Public look-up", "launchd"),
}
REQUEST_WORDS = {"inspect": "read", "submit": "send try", "reconcile": "check after sending", "verify": "receipt check",
                 "investigate": "look-up"}
ATTEMPT_WORDS = {"VERIFIED": "Sent", "CONFIRMED_NOT_SUBMITTED": "Not sent", "SUBMITTING": "Not confirmed yet"}
STATUS_WORDS = {"RESOLVED": "Found", "UNRESOLVED": "Not found", "SUBMITTED": "Sent"}
VERDICT_WORDS = {"SAME": "Same posting", "DIFFERENT": "Different posting", "UNSURE": "Unsure"}
ACTION_WORDS = {"RETRY": "Try again", "ABANDON": "Stop trying"}
INDUSTRY_WORDS = {"COMPLETE": "Finished", "FAILED": "Failed", "ABANDONED": "Stopped"}
RUN_WORDS = {"COMPLETE": "Finished", "FAILED": "Failed", "USER_WAIT_DONE": "Finished after you"}
MAILBOX_CODES = ("AUTHENTICATION_REQUIRED", "MFA_REQUIRED")   # an email application: the sign-in is the mailbox's
MAILBOX_SIGN_IN = "Your mailbox needs a sign-in"
STAGE_WORDS = {"discovery": "stage 1, search", "qualification": "stage 2, company check"}
FILE_NAMES = {   # L12: single files named in plain words (no internal names such as owner or orchestrator)
    "outreach-pending-owner-approval.json": "Messages waiting for your approval",
    "pending-owner-approval.json": "Contacts waiting for your approval",
    "latest.json": "Latest round result", "hourly-yield.json": "Hourly round results",
    "applications.json": "Applications round result", "applications-service.json": "Applications service status",
    "local-check.json": "Local check result", "inactivity-closure.json": "Quiet contacts check",
    "reconciliation.json": "Sent applications check", "transport-test.json": "Connection test",
    "updater.log": "Updates",
}
_STAGE_FILE = re.compile(r"([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\.([a-z0-9-]*-[0-9a-f]{10})\.(?:[0-9]{1,6}\.)?"
                         r"(discovery|qualification|industry)\b")
_RUN_FILE = re.compile(r"([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\.")
_ACTION_IN_PROMPT = re.compile(r'"action_id": "([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}):((?:[^"\\]|\\.){1,120})"')
_NUMBER_IN_PROMPT = re.compile(r'"decision_number": ([0-9]{1,4})')
PROMPT_BYTES = 256 * 1024
SMALL_BYTES = 64 * 1024


_READS = {}   # L5: (path, limit, size, mtime) -> what a naming file held; a finished session's files never change


def _kept(path, limit, read):
    try:
        info = os.lstat(path)
    except OSError:
        return read(path, limit)
    key = (str(path), limit, info.st_size, info.st_mtime_ns)
    if key not in _READS:
        if len(_READS) > 20000:
            _READS.clear()
        _READS[key] = read(path, limit)
    return _READS[key]


def _small_json(path, limit=SMALL_BYTES):
    return _kept(path, limit, _read_small_json)


def _head_text(path, limit=PROMPT_BYTES):
    return _kept(path, limit, _read_head_text)


def _read_small_json(path, limit=SMALL_BYTES):
    handle = _open_regular(path)
    if handle is None:
        return None
    try:
        with handle:
            return json.loads(handle.read(limit).decode("utf-8", "replace"))
    except (OSError, ValueError):
        return None


def _read_head_text(path, limit=PROMPT_BYTES):
    handle = _open_regular(path)
    if handle is None:
        return ""
    try:
        with handle:
            return handle.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""


def _judged_job(text):
    """The job a posting check judged: the top-level job_key of the JSON packet that ends its prompt
    (`posting_judge.prompt` / `same_job_prompt`: text, a blank line, json.dumps(packet, sort_keys=True)),
    never a key of the existing jobs listed inside it (L6)."""
    _, _, packet = (text or "").rpartition("\n\n")
    try:
        value = json.loads(packet)
    except ValueError:
        return None
    job = value.get("job_key") if isinstance(value, dict) else None
    return job if isinstance(job, str) and job else None


def _code_words(code):
    words = fp.CAUSE_WORDS.get(code) or fp.applications_page.CODE_WORDS.get(code) if code else None
    return words.rstrip(". ") if words else None


def _mtime(path):
    try:
        return dt.datetime.fromtimestamp(os.lstat(path).st_mtime, dt.timezone.utc)
    except OSError:
        return None


def _operation(op_id, kind, at, component, words=None, about=None, what=None, outcome=None, job=None, extra=""):
    return {"id": op_id, "kind": kind, "words": words, "about": about, "what": what, "outcome": outcome, "at": at,
            "component": component, "job": job, "extra": extra}


def _index(root, logs):
    """[operation] without names: one per session folder, research stage, research run, saved progress,
    stream (with its earlier copies) and other single file."""
    found, grouped = [], {}
    reports = paths.reports(root)
    for key, base in _bases(root, logs).items():
        try:
            listed = sorted(os.listdir(base)) if base.is_dir() and not base.is_symlink() else []
        except OSError:
            listed = []
        for name in listed:
            if not SEGMENT.match(name):
                continue
            path = base / name
            try:
                info = os.lstat(path)
            except OSError:
                continue
            at = dt.datetime.fromtimestamp(info.st_mtime, dt.timezone.utc)
            if stat.S_ISDIR(info.st_mode):
                if key != paths.REPORTS:
                    continue
                kind = SESSION_KINDS.get(name)
                if kind is None:
                    continue
                for session, moment in _sessions(path, name):
                    if moment is not None:
                        found.append(_operation("dir:%s/%s" % (name, session), kind[0], moment, kind[2], words=kind[1]))
                for loose in _loose_files(path):   # a file kept beside the session folders (L12)
                    file_id = "%s/%s/%s" % (paths.REPORTS, name, loose[0])
                    found.append(_operation("file:" + file_id, kind[0], loose[1], kind[2],
                                            words=FILE_NAMES.get(loose[0]) or _plain(loose[0]), extra=file_id))
                continue
            if not stat.S_ISREG(info.st_mode) or not _wanted(name):
                continue
            file_id = key + "/" + name
            stream, stage, checkpoint = _STREAM.fullmatch(name), _STAGE_FILE.match(name), _CHECKPOINT.fullmatch(name)
            if key == paths.LOGS and stream:
                group = "stream:%s.%s" % stream.groups()[:2]
                entry = grouped.setdefault(group, _operation(group, "sidedoor", at, stream.group(1)))
            elif stage:
                attempt, label, phase = stage.groups()
                group = "stage:%s.%s.%s" % (attempt, label, "discovery" if phase == "industry" else phase)
                entry = grouped.setdefault(group, _operation(group, "research", at, "research"))
            elif checkpoint:
                group = "saved:%s.%s" % checkpoint.groups()
                entry = grouped.setdefault(group, _operation(group, "saved", at, "research"))
            elif _RUN_FILE.match(name):
                group = "run:" + name[:36]
                entry = grouped.setdefault(group, _operation(group, "research", at, "research"))
            else:
                parts = [name]
                component = describe(key, parts, None)[0]
                kind = "research" if component == "research" else ("outreach" if name.startswith(("linkedin", "outreach"))
                                                                   else "sidedoor")
                words = FILE_NAMES.get(name) or describe(key, parts, None)[2]
                if key == paths.REPORTS and name not in FILE_NAMES and not words.lower().endswith(("status", "report")):
                    words += " report"
                found.append(_operation("file:" + file_id, kind, at, component, words=words, extra=file_id))
                continue
            entry["at"] = max(entry["at"], at)
            entry.setdefault("ids", []).append(file_id)
    found += grouped.values()
    found.sort(key=lambda entry: entry["at"], reverse=True)
    return found


def _loose_files(folder):
    found = []
    try:
        listed = sorted(os.listdir(folder))
    except OSError:
        return found
    for name in listed:
        if SEGMENT.match(name) and _wanted(name):
            try:
                info = os.lstat(os.path.join(folder, name))
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                found.append((name, dt.datetime.fromtimestamp(info.st_mtime, dt.timezone.utc)))
    return found


def _sessions(folder, base):
    """[(session folder name, its time)] of reports/<base>: agent-worker/<task>/<attempt> as "<task>/<attempt>"."""
    def folders(parent, prefix, wanted):
        found = []
        try:
            listed = sorted(os.listdir(parent))
        except OSError:
            return found
        for name in listed:
            if not wanted(name):
                continue
            try:
                info = os.lstat(os.path.join(parent, name))
            except OSError:
                continue
            if stat.S_ISDIR(info.st_mode):
                found.append((prefix + name, dt.datetime.fromtimestamp(info.st_mtime, dt.timezone.utc)))
        return found

    names = folders(str(folder), "", _FOLDER_OK.match)
    if base != "agent-worker":
        return names
    return [found for task, _ in names for found in folders(os.path.join(str(folder), task), task + "/", str.isdigit)]


_FOLDER_OK = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}\Z")


class Names:
    """What each operation was about, from the records that name it (read only), built once per request."""

    def __init__(self, reader, root):
        self.reader, self.root, self.jobs = reader, root, {}
        self.requests, self.folders, self._sessions = {}, {}, None
        self.research, self.workers = {}, {}
        self.recoveries, self.labels, self.recovery_jobs = {}, {}, {}
        self.messages, self.reads, self.people = [], [], {}
        try:
            self._load()
        except Exception:   # names only label rows: an unreadable record leaves the kind and the time
            pass

    def _rows(self, sql, args, table):
        return self.reader.rows(sql, args, table) if self.reader is not None else []

    def _load(self):
        submits = {}
        # L5: SQLite reads the few fields named here out of each stored JSON (no whole-document parse per row).
        for row in self._rows("SELECT request_id,job_key,operation,state,attempt_id,created_at,result AS raw,"
                              "CASE WHEN json_valid(result) THEN json_extract(result,'$.status') END AS status,"
                              "CASE WHEN json_valid(result) THEN json_extract(result,'$.blocked.code') END AS code "
                              "FROM desktop_requests ORDER BY created_at", (), "desktop_requests"):
            entry = dict(row)
            if entry["operation"] == "submit":
                submits[entry["job_key"]] = submits.get(entry["job_key"], 0) + 1
                entry["try"] = submits[entry["job_key"]]
            entry["result"] = {"status": entry.pop("status"), "blocked": {"code": entry.pop("code")}}
            self.requests[entry["request_id"]] = entry

        attempts = {}
        for row in self._rows("SELECT attempt_id,state,CASE WHEN json_valid(evidence) THEN "
                              "json_extract(evidence,'$.review.application_method') END AS method FROM application_attempts",
                              (), "application_attempts"):
            attempts[row["attempt_id"]] = (row["state"], row["method"] == "EMAIL")
        for entry in self.requests.values():
            entry["attempt_state"], entry["email"] = attempts.get(entry.get("attempt_id"), (None, False))
        people_of = {}
        for row in self._rows("SELECT run.run_id AS run_id,"
                              "CASE json_type(person.value,'$.recipient') WHEN 'text' THEN json_extract(person.value,'$.recipient') "
                              "WHEN 'object' THEN json_extract(person.value,'$.recipient.name') END AS name "
                              "FROM linkedin_contact_runs AS run, json_each(run.result,'$.people') AS person "
                              "WHERE json_valid(run.result) AND json_type(run.result,'$.people')='array'", (),
                              "linkedin_contact_runs"):
            if isinstance(row["name"], str):
                people_of.setdefault(row["run_id"], []).append(row["name"][:80])
        for row in self._rows("SELECT run_id,company,state,"
                              "CASE WHEN json_valid(result) THEN json_extract(result,'$.company') END AS named,"
                              "CASE WHEN json_valid(result) THEN json_extract(result,'$.directory') END AS directory,"
                              "CASE WHEN json_valid(result) THEN json_extract(result,'$.trace_path') END AS trace_path,"
                              "CASE WHEN json_valid(result) THEN json_extract(result,'$.error.trace_path') END AS error_trace "
                              "FROM linkedin_contact_runs", (), "linkedin_contact_runs"):
            value = {"company": row["named"], "directory": row["directory"], "trace_path": row["trace_path"],
                     "error": {"trace_path": row["error_trace"]}}
            folder = fp._folder_of(self.root, value, "linkedin-contacts")
            session = _session_of(self.root, folder, "linkedin-contacts") if folder is not None else None
            if session:
                people = people_of.get(row["run_id"], [])
                self.folders[("linkedin-contacts", session)] = {
                    "company": str(value.get("company") or row["company"] or "")[:80], "state": row["state"],
                    "people": people}
        for row in self._rows("SELECT id,started_at,state,result FROM research", (), "research"):
            value = overview._json(row["result"])
            value = value if isinstance(value, dict) else {}
            industries = {fp._research_label(str(item["industry"])): item for item in value.get("industries") or []
                          if isinstance(item, dict) and item.get("industry")}
            for label, item in industries.items():
                self.labels.setdefault(label, str(item["industry"]))
            self.research[row["id"]] = {"state": row["state"], "industries": industries,
                                        "at": overview._utc(row["started_at"]) if row["started_at"] else None}
        self.recoveries = fp.run_names(self.reader) if self.reader is not None else {}
        for row in self._rows("SELECT job_key,detail FROM application_events WHERE event LIKE 'APPLICATION_RECOVERY%' "
                              "AND detail LIKE '%application-recovery%decision-%'", (), "application_events"):
            for folder in set(fp._DECISION_FOLDER.findall(str(row["detail"] or ""))):
                self.recovery_jobs.setdefault(folder, row["job_key"])
        for row in self._rows("SELECT task_id,worker FROM workflow_tasks", (), "workflow_tasks"):
            self.workers[row["task_id"]] = row["worker"]
        self._load_messages()

    def _load_messages(self):
        """L10: outreach, follow-ups and replies keep no files; each message (outreach_actions) and each
        reply read (reply_worker_attempts) is a row from its own records."""
        seen = {}
        for row in self._rows("SELECT action_id,MAX(observed_at) AS at FROM outreach_agent_audit GROUP BY action_id", (),
                              "outreach_agent_audit"):
            seen[row["action_id"]] = row["at"]
        self.people = {}
        for row in self._rows("SELECT action_id,identity,slot,state,claimed_at,"
                              "CASE WHEN json_valid(payload) THEN json_extract(payload,'$.purpose') END AS purpose,"
                              "CASE WHEN json_valid(payload) THEN json_extract(payload,'$.company') END AS company,"
                              "CASE WHEN json_valid(payload) THEN json_extract(payload,'$.recipient') END AS recipient "
                              "FROM outreach_actions", (), "outreach_actions"):
            entry = dict(row)
            entry["at"] = overview._utc(entry["claimed_at"] or seen.get(entry["action_id"]) or "")
            self.messages.append(entry)
            if entry["recipient"] or entry["company"]:
                self.people.setdefault(entry["identity"], (entry["company"], entry["recipient"]))
        for row in self._rows("SELECT company,identity,name FROM rw_contacts", (), "rw_contacts"):
            if row["identity"] and row["identity"] not in self.people and (row["name"] or row["company"]):
                self.people[row["identity"]] = (row["company"], row["name"])
        tasks = {row["task_id"]: row["inputs"] for row in self._rows(
            "SELECT task_id,inputs FROM workflow_tasks WHERE task_id IN (SELECT task_id FROM reply_worker_attempts)", (),
            "workflow_tasks")}
        for row in self._rows("SELECT task_id,attempt,observed_at,blocker,"
                              "CASE WHEN json_valid(output) THEN json_extract(output,'$.label') END AS label "
                              "FROM reply_worker_attempts", (), "reply_worker_attempts"):
            entry = dict(row, at=overview._utc(row["observed_at"] or ""), identity=None)
            try:
                inputs = json.loads(tasks.get(row["task_id"]) or "null")
            except ValueError:
                inputs = None
            for item in inputs if isinstance(inputs, list) else []:
                source = item.get("source") if isinstance(item, dict) else None
                if isinstance(source, str) and source.startswith("engagement_events:"):
                    try:   # the task's own reference: engagement_events:[source, source_id]
                        key = json.loads(source[len("engagement_events:"):])
                    except ValueError:
                        key = None
                    if isinstance(key, list) and len(key) == 2:
                        found = self._rows("SELECT identity FROM engagement_events WHERE source=? AND source_id=?",
                                           (str(key[0]), str(key[1])), "engagement_events")
                        entry["identity"] = found[0]["identity"] if found else None
                        break
            self.reads.append(entry)

    @property
    def sessions(self):
        """{browser session folder: request id} from results that name their folder themselves (older
        results; a newer folder's summary.json names its request), worked out only when first needed."""
        if self._sessions is None:
            self._sessions = {}
            for entry in self.requests.values():
                result = overview._json(entry["raw"])
                if isinstance(result, dict):
                    folder = fp._folder_of(self.root, result, "application-browser")
                    session = _session_of(self.root, folder, "application-browser") if folder is not None else None
                    if session:
                        self._sessions.setdefault(session, entry["request_id"])
        return self._sessions

    def job_words(self, keys):
        wanted = [key for key in set(keys) if key and key not in self.jobs]
        if wanted and self.reader is not None:
            try:
                self.jobs.update(_jobs_words(self.reader, wanted))
            except Exception:
                pass
            for key in wanted:
                self.jobs.setdefault(key, None)
        return self.jobs


def _industry_name(names, attempt, label):
    item = names.research.get(attempt, {}).get("industries", {}).get(label)
    words = str(item["industry"]) if item else names.labels.get(label)
    return (words[:1].upper() + words[1:]) if words else _industry(label)


def _name(entry, names, root):
    """Fill `about`, `what` and `outcome` of one operation from its records."""
    op_id = entry["id"]
    kind, _, rest = op_id.partition(":")
    reports = paths.reports(root)
    if kind == "dir":
        base, _, session = rest.partition("/")
        folder = reports / base / session
        if base == "application-browser":
            summary = _small_json(folder / "summary.json") or {}
            request = names.requests.get(summary.get("request_id") if isinstance(summary, dict) else None)
            request = request or names.requests.get(names.sessions.get(session))
            if request:
                entry["job"] = request["job_key"]
                operation = request["operation"]
                entry["what"] = REQUEST_WORDS.get(operation, "browser session") + (
                    " %d" % request["try"] if request.get("try") else "")
                result = request["result"] if isinstance(request["result"], dict) else {}
                blocked = result.get("blocked") if isinstance(result.get("blocked"), dict) else {}
                reason = MAILBOX_SIGN_IN if request.get("email") and blocked.get("code") in MAILBOX_CODES \
                    else _code_words(blocked.get("code"))
                if operation == "submit" and request.get("attempt_state") in ATTEMPT_WORDS:
                    head = ATTEMPT_WORDS[request["attempt_state"]]
                elif request["state"] == "EXPIRED":
                    head = "Did not finish in time"
                else:
                    head = STATUS_WORDS.get(result.get("status"))
                entry["outcome"] = (head + (": " + reason[:1].lower() + reason[1:] if reason else "")) if head else reason
        elif base == "application-recovery":
            entry["about"], entry["job"] = names.recoveries.get(session), names.recovery_jobs.get(session)
            entry["outcome"] = ACTION_WORDS.get((_small_json(folder / "decision.json") or {}).get("action"))
        elif base == "posting-judge":
            entry["job"] = _judged_job(_head_text(folder / "prompt.txt"))
            decision = _small_json(folder / "decision.json")
            entry["outcome"] = VERDICT_WORDS.get(decision.get("verdict")) if isinstance(decision, dict) else None
        elif base == "research-recovery":
            text = _head_text(folder / "prompt.txt")
            found, number = _ACTION_IN_PROMPT.search(text), _NUMBER_IN_PROMPT.search(text)
            if found:
                industry = json.loads('"%s"' % found.group(2))
                entry["about"] = industry[:1].upper() + industry[1:80]
            entry["what"] = "decision %s" % number.group(1) if number else None
            decision = _small_json(folder / "decision.json")
            entry["outcome"] = ACTION_WORDS.get(decision.get("action")) if isinstance(decision, dict) else None
        elif base == "linkedin-contacts":
            run = names.folders.get(("linkedin-contacts", session))
            if run:
                entry["about"] = run["company"] or None
                entry["outcome"] = RUN_WORDS.get(run["state"])
                entry["extra"] = " ".join(run["people"])
        elif base == "email-check":
            check = _small_json(folder / "check.json")
            entry["outcome"] = "Refused" if isinstance(check, dict) and check.get("refused") else None
            mailbox = check.get("signed_in_account") if isinstance(check, dict) else None
            entry["about"] = mailbox[:120] if isinstance(mailbox, str) and mailbox else None   # L11: no job is recorded
        elif base == "agent-worker":
            worker = names.workers.get(session.split("/", 1)[0])
            entry["about"] = fp.worker_words(worker)[1] if worker else None
            entry["what"] = "try " + session.split("/", 1)[1]
    elif kind == "stage":
        attempt, label, phase = rest.split(".")
        entry["about"] = _industry_name(names, attempt, label)
        entry["what"] = STAGE_WORDS[phase]
        item = names.research.get(attempt, {}).get("industries", {}).get(label) or {}
        entry["outcome"] = INDUSTRY_WORDS.get(str(item.get("state") or "").upper())
    elif kind == "run":
        run = names.research.get(rest)
        if run:
            count = len(run["industries"])
            entry["about"] = "whole run" + (", %d industr%s" % (count, "y" if count == 1 else "ies") if count else "")
            entry["outcome"] = INDUSTRY_WORDS.get(str(run["state"] or "").upper())
        else:
            entry["about"] = "whole run"
    elif kind == "saved":
        day, label = rest.split(".", 1)
        entry["about"] = "%s, %s" % (_industry_name(names, None, label), fp._day_label(day))
    elif kind == "stream":
        component, level = rest.split(".")
        entry["words"] = "%s %s" % (dict(fp.COMPONENTS)[component], "errors" if level == "stderr" else "output")


def _stage_outcomes(found):
    """L7: the industry's result belongs to its last stage, so only the last stage with files of each
    industry's run shows it (no record holds a result per stage)."""
    last = {}
    for entry in found:
        if entry["id"].startswith("stage:"):
            attempt, label, phase = entry["id"][6:].split(".")
            rank = list(STAGE_WORDS).index(phase)
            if rank >= last.get((attempt, label), (-1, None))[0]:
                last[(attempt, label)] = (rank, entry["id"])
    kept = {value[1] for value in last.values()}
    for entry in found:
        if entry["id"].startswith("stage:") and entry["id"] not in kept:
            entry["outcome"] = None


def _finish(entry, names, zone):
    if entry.get("job"):
        words = names.jobs.get(entry["job"])
        entry["about"] = entry["about"] or words
    kind_label = dict(OPERATION_KINDS)[entry["kind"]]
    words = entry.get("words")
    if words and (entry["kind"] in ("sidedoor", "message") or words in FILE_NAMES.values()
                  or words.lower().startswith(kind_label.lower())):
        pieces = [words]
    else:
        pieces = [kind_label] + ([words] if words and words != "Browser session" else [])
    pieces += [value for value in (entry.get("about"), entry.get("what")) if value]
    entry["when"] = _when(entry["at"], zone)
    entry["label"] = re.sub(r"\s{2,}", " ", _RAW_ID.sub("", " · ".join(pieces))).strip()
    entry["kind_label"] = kind_label
    entry["search"] = " ".join(value for value in (entry["label"], entry.get("about") or "", entry.get("extra") or "",
                                                   entry.get("outcome") or "") if value).lower()
    return entry


_OPERATIONS = {}     # L5: (root, logs, zone) -> (fingerprint, monotonic time, operations)
OPERATIONS_FRESH = 60   # seconds a list is reused while nothing it is built from changed


def _fingerprint(root, logs):
    """What the operations list is built from: the database (and its -wal), logs/ and each stream in it,
    reports/ and each session folder kind. A new folder, file or record changes it."""
    database = paths.database(root)
    reports = paths.reports(root)
    found = []
    for path in [database, database.with_name(database.name + "-wal"), Path(logs), reports] + \
            [Path(logs) / name for name in fp.stream_names()] + [reports / name for name in SESSION_KINDS]:
        try:
            info = os.stat(path)
            found.append((info.st_size, info.st_mtime_ns))
        except OSError:
            found.append(None)
    return tuple(found)


def operations(reader, root, logs, zone=None):
    """Every operation, newest first, each named (`label`, `outcome`, `when`) with no raw id. The list is
    reused for OPERATIONS_FRESH seconds while its sources are unchanged (each row's files are read fresh)."""
    import time
    key, stamp = (str(root), str(logs), str(zone)), _fingerprint(root, logs)
    kept = _OPERATIONS.get(key)
    if kept and kept[0] == stamp and time.monotonic() - kept[1] < OPERATIONS_FRESH:
        return [dict(entry) for entry in kept[2]]
    found = _build(reader, root, logs, zone)
    _OPERATIONS.clear()
    _OPERATIONS[key] = (stamp, time.monotonic(), found)
    return [dict(entry) for entry in found]


PURPOSE_WORDS = {"INVITE": "Invitation", "INITIAL": "Message", "FOLLOWUP": "Follow-up", "REPLY": "Reply"}
ACTION_STATE_WORDS = {"CONFIRMED_SENT": "Sent", "CONFIRMED_NOT_SENT": "Not sent",
                      "CANCELLED_BEFORE_SEND": "Cancelled before sending",
                      "ACKNOWLEDGED_DELIVERY_UNCONFIRMED": "Sent, delivery not confirmed"}
READ_WORDS = {"AGREEMENT": "Agreed", "DECLINE": "Declined", "AMBIGUOUS": "Unclear", "QUESTION": "Asked a question",
              "INTERVIEW": "Interview", "REJECTION": "Rejection", "QUOTED_OR_FORWARDED": "Quoted or forwarded",
              "OTHER": "Other"}


def _tag(kind, key):
    import hashlib   # approvals_page.dom / networking_page._digest's form: a card's own id
    return kind + "-" + hashlib.sha1(str(key).encode()).hexdigest()[:16]


def _who(company, person):
    return ", ".join(str(value)[:80] for value in (company, person) if value) or None


def _message_rows(names):
    """L10: one row per message and per reply read, from their records."""
    found = []
    for item in names.messages:
        if item["at"] is None:
            continue
        found.append(_operation("msg:" + item["action_id"], "message", item["at"], "launchd",
                                words=PURPOSE_WORDS.get(str(item["purpose"] or "").upper(), "Message"),
                                about=_who(item["company"], item["recipient"]),
                                outcome=ACTION_STATE_WORDS.get(item["state"]))
                     | {"tags": [_tag("message", item["action_id"]), _tag("contact", item["identity"])]})
    for item in names.reads:
        if item["at"] is None:
            continue
        company, person = names.people.get(item["identity"], (None, None)) if item["identity"] else (None, None)
        found.append(_operation("read:%s.%s" % (item["task_id"], item["attempt"]), "message", item["at"], "launchd",
                                words="Reply read", about=_who(company, person),
                                outcome=READ_WORDS.get(item["label"]) or _code_words(item["blocker"])
                                or ("Could not read" if item["blocker"] else None))   # L14
                     | {"tags": [_tag("contact", item["identity"])] if item["identity"] else []})
    return found


def message_records(reader, op_id):
    """The saved records of one message or reply read, as JSON text (its Download), or None."""
    kind, _, key = op_id.partition(":")
    if kind == "msg":
        rows = reader.rows("SELECT * FROM outreach_actions WHERE action_id=?", (key,), "outreach_actions")
        if not rows:
            return None
        audit = reader.rows("SELECT phase,kind,observed_at,record FROM outreach_agent_audit WHERE action_id=? "
                            "ORDER BY observed_at", (key,), "outreach_agent_audit")
        value = {"message": {name: overview._json(rows[0][name]) if name in ("payload", "result") else rows[0][name]
                             for name in rows[0].keys()},
                 "steps": [dict(row, record=overview._json(row["record"])) for row in audit]}
    elif kind == "read":
        task, _, attempt = key.rpartition(".")
        rows = reader.rows("SELECT * FROM reply_worker_attempts WHERE task_id=? AND attempt=?", (task, attempt),
                           "reply_worker_attempts")
        if not rows:
            return None
        value = {"read": {name: overview._json(rows[0][name]) if name in ("output", "provenance") else rows[0][name]
                          for name in rows[0].keys()}}
    else:
        return None
    return _without_accounts(value)


# L15: account and sender identifiers and the sign-in label stay out of a message's records.json
RECORD_DROPPED = frozenset({"account", "account_id", "account_profile", "auth", "chat_id", "linkedin_account_id",
                            "member_id", "member_urn", "provider_id", "provider_member_id", "public_identifier",
                            "sender", "sender_id", "sender_profile"})


def _without_accounts(value):
    if isinstance(value, dict):
        return {key: _without_accounts(item) for key, item in value.items() if key not in RECORD_DROPPED}
    if isinstance(value, list):
        return [_without_accounts(item) for item in value]
    return value


def _redacted_value(value, secrets, fragments):
    """L15: each text in the records passed through the page's redaction rule on its own, so the file
    stays valid JSON; a text the rule would drop is replaced whole."""
    import reply_worker
    if isinstance(value, dict):
        return {_redacted_value(key, secrets, fragments): _redacted_value(item, secrets, fragments)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_redacted_value(item, secrets, fragments) for item in value]
    if isinstance(value, str):
        kept = reply_worker.redact_line(fp._ESCAPES.sub("", value), secrets, fragments) \
            if "\n" not in value else None
        if kept is None and "\n" in value:
            lines = [reply_worker.redact_line(fp._ESCAPES.sub("", line), secrets, fragments) for line in value.split("\n")]
            return "\n".join(REMOVED if line is None else line for line in lines)
        return REMOVED if kept is None else kept
    return value


def records_text(value, secrets):
    return json.dumps(_redacted_value(value, secrets, fp._fragments(secrets)), indent=1, sort_keys=True, default=str)


def _build(reader, root, logs, zone=None):
    found = _index(root, logs)
    names = Names(reader, root)
    for entry in found:
        _name(entry, names, root)
    _stage_outcomes(found)
    found += _message_rows(names)
    found.sort(key=lambda entry: entry["at"], reverse=True)
    names.job_words([entry["job"] for entry in found if entry.get("job")])
    return [_finish(entry, names, zone) for entry in found]


def operation_files(root, logs, entry, zone=None):
    """[{id, label, when, size}] of one operation's files, newest first."""
    kind, _, rest = entry["id"].partition(":")
    found = []
    if kind == "dir":
        base, _, session = rest.partition("/")
        folder = paths.reports(root) / base / session
        if base in SESSION_KINDS and all(_FOLDER_OK.match(part) for part in session.split("/")) \
                and fp._inside(folder, paths.reports(root) / base):
            prefix = paths.REPORTS + "/" + base + "/" + session
            found = [(prefix + "/" + "/".join(parts), info) for parts, _, info in
                     _walk(folder, MAX_DEPTH - 1 - session.count("/") - 1)]
    else:
        for file_id in entry.get("ids") or ([entry["extra"]] if kind == "file" else []):
            path = resolve(root, logs, file_id)
            try:
                found.append((file_id, os.lstat(path))) if path is not None else None
            except OSError:
                continue
    files = []
    for file_id, info in found:
        parts = file_id.split("/")
        at = dt.datetime.fromtimestamp(info.st_mtime, dt.timezone.utc)
        label = parts[-1] if kind == "dir" else describe(parts[0], parts[1:], None)[2]
        if kind == "dir":
            inner = parts[2 + entry["id"].count("/"):]
            label = "/".join(inner) + (" (%s)" % FILE_WORDS[inner[-1]] if inner and inner[-1] in FILE_WORDS else "") \
                if inner else parts[-1]
        files.append({"id": file_id, "label": label, "name": parts[-1], "at": at, "when": _when(at, zone),
                      "size": info.st_size})
    files.sort(key=lambda item: item["at"], reverse=True)
    return files


def operations_paged(reader, root, logs, component="all", day="", find="", job="", zone=None, page=1, about=""):
    """(rows, paging): the operations matching the filters, RUNS_LISTED a page, each with its `files`."""
    found = operations(reader, root, logs, zone)
    if component != "all":
        found = [entry for entry in found if entry["component"] == component]
    if isinstance(day, str) and fp._DAY.match(day):
        found = [entry for entry in found if fp.local_day(entry["at"], zone) == day]
    if job:
        found = [entry for entry in found if entry.get("job") == job]
    if about:   # a message or Networking card's Logs link: that message's or contact's rows
        found = [entry for entry in found if about in (entry.get("tags") or ())]
    words = [word for word in (find or "").lower().split() if word][:8]
    if words:
        found = [entry for entry in found if all(word in entry["search"] for word in words)]
    shown, paging = fp.page_of(found, page, fp.RUNS_LISTED)
    for entry in shown:
        entry["files"] = operation_files(root, logs, entry, zone)
        entry["records"] = entry["id"].split(":", 1)[0] in ("msg", "read")
    return shown, paging


def find_operation(reader, root, logs, op_id, zone=None):
    """The operation `op_id` names (only one the index lists), with its files, or None."""
    if not isinstance(op_id, str):
        return None
    if op_id.split(":", 1)[0] in ("msg", "read"):
        entry = next((item for item in operations(reader, root, logs, zone) if item["id"] == op_id), None)
        text = message_records(reader, op_id) if entry else None
        if text is None:
            return None
        return dict(entry, files=[], records=text)
    for entry in _index(root, logs):
        if entry["id"] == op_id:
            names = Names(reader, root)
            _name(entry, names, root)
            names.job_words([entry["job"]] if entry.get("job") else [])
            entry = _finish(entry, names, zone)
            entry["files"] = operation_files(root, logs, entry, zone)
            return entry
    return None


def operation_zip(root, logs, secrets, entry, zone=None, now=None):
    """(filename, temporary file): every file of one operation, whole and redacted, and index.txt."""
    now = now or dt.datetime.now(dt.timezone.utc)
    members = [(item["id"], resolve(root, logs, item["id"]), item["label"], entry["kind_label"], None, item["when"],
                "no longer on this computer") for item in entry["files"]]
    if isinstance(entry.get("records"), dict):   # L10: a message's saved records, redacted value by value (L15)
        text = records_text(entry["records"], secrets)
        members.append(("records.json", io.BytesIO(text.encode("utf-8")), "Saved records",
                        entry["kind_label"], None, entry["when"], None))
    head = "Sidedoor files for %s (%s), secrets removed. Made %s. Each line: name | part | when | file | note." % (
        entry["label"], entry["when"], _when(now, zone))
    slug = re.sub(r"[^a-z0-9-]+", "-", entry["label"].lower()).strip("-")
    if len(slug) > 60:   # L12: cut at a word boundary
        slug = slug[:61].rsplit("-", 1)[0]
    slug = slug or "operation"
    return "sidedoor-%s-%s.zip" % (slug, fp.local_day(entry["at"], zone)), _zip(members, head, secrets)
