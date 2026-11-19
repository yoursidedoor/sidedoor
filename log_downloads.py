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
            handle = _open_regular(path) if path is not None else None
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
