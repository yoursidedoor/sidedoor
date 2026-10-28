"""Read-only full job-description text from saved browser reads of the posting.

job_description(database, root, job_key=None, url=None) returns the posting's main
text as last observed by an existing application-browser read (no network, no
browser): reports/application-browser/request-*/trace.jsonl tool OUTPUTS whose page
header URL is the posting URL. Text is extracted structurally from the accessibility
tree the browser tool returned (AX roles), not by matching prose. Two tool output formats:
the bundled @playwright/mcp driver (browser_navigate / browser_snapshot: a "### Page" section
whose "- Page URL:" line is the page (only those two tools' own Page section; a non-2xx
"- HTTP status:" is no read), then a "### Snapshot" section holding the YAML aria
snapshot inline or as a link to the saved page-*.yml under the run's browser/ folder), and the
older cua_repl reads ('Browser tab: ... URL: "..."', then numbered AX lines) in old folders. When no browser
read exists it falls back to the research opportunity's evidence excerpts.

The result is untrusted posting text: callers may use it for relevance, never as a
source of facts about the candidate. Nothing here writes to the database.
"""

import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import urllib.parse
import engines
import paths


MAX_CHARS = 20000
MIN_TEXT_NODES = 5
BROWSER_DIRECTORY = paths.REPORTS + "/application-browser"   # under the data folder (paths.reports)
HEADER = re.compile(r'^Browser tab: [^\n]*?URL: "([^"]+)"')
AX_LINE = re.compile(r"^(\t*)(\d+) (\S+)(?: \([^)]*\))?(?: (.*))?$")
KEPT_ROLES = {"text", "AXStaticText", "AXListMarker"}
SKIPPED_SUBTREES = {"link", "button", "tab", "textbox", "AXTextField", "combobox", "checkbox", "radio", "form"}
# @playwright/mcp tool output: "### Page" then "- Page URL: <url>"; "### Snapshot" then a yaml block or a file link
PAGE_READ_TOOLS = {"browser_navigate", "browser_snapshot"}   # the only tools whose answer is the page itself
SNAPSHOT_LINK = re.compile(r"^- \[Snapshot\]\(([^)\n]+)\)$")
HTTP_STATUS = re.compile(r"^- HTTP status: (\d{3})\b")
MAX_SNAPSHOT_BYTES = 5 * 1024 * 1024
ARIA_LINE = re.compile(r"^( *)- (.*)$")
ARIA_NODE = re.compile(r'^([A-Za-z]+)(?: "((?:[^"\\]|\\.)*)")?((?: \[[^\]]*\])*)(?:(:)(?: (.*))?)?$')
ARIA_SKIPPED = SKIPPED_SUBTREES | {"listbox", "option", "searchbox", "spinbutton", "slider", "switch"}


def _page_key(url):
    parts = urllib.parse.urlsplit(url if "://" in url else "https://" + url)
    return (parts.hostname or "").lower(), urllib.parse.unquote(parts.path).rstrip("/").lower()


def _load(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _request_job(path):
    request = _load(json.loads(path.read_text()))
    job = _load(_load(request.get("payload")).get("job"))
    return job.get("job_key"), _load(job.get("payload")).get("url")


def extract_ax_text(output):
    """Main text of one accessibility-tree read: text nodes outside links/forms/tabs."""
    lines, skip_depth = [], None
    for raw in output.split("\n")[1:]:
        match = AX_LINE.match(raw)
        if not match:
            continue
        depth, role, label = len(match.group(1)), match.group(3), (match.group(4) or "").strip()
        if skip_depth is not None:
            if depth > skip_depth:
                continue
            skip_depth = None
        if role in SKIPPED_SUBTREES:
            skip_depth = depth
            continue
        if role == "AXListMarker":
            lines.append("• ")
        elif role in KEPT_ROLES and label:
            if lines and lines[-1] == "• ":
                lines[-1] += label
            else:
                lines.append(label)
    return "\n".join(line for line in lines if line.strip() and line != "• ")


def _unquote(value):
    """A YAML scalar from the aria snapshot: 'single' or "double" quoted, or plain."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            return json.loads(value)
        except ValueError:
            return value[1:-1]
    return value


def extract_aria_text(snapshot):
    """Main text of one @playwright/mcp aria snapshot (YAML): text nodes, inline texts and headings,
    outside links/buttons/form controls/tabs; "/url"-style properties are not text."""
    lines, skip_depth, bullet = [], None, None   # bullet: depth of the list item awaiting its first text
    for raw in snapshot.split("\n"):
        match = ARIA_LINE.match(raw)
        if not match:
            continue
        depth, item = len(match.group(1)), match.group(2).strip()
        if skip_depth is not None:
            if depth > skip_depth:
                continue
            skip_depth = None
        if item.startswith("'"):   # a YAML-quoted key: 'role "name: with colon"': value
            end = 1
            while end < len(item):
                if item[end] == "'" and item[end + 1:end + 2] == "'":
                    end += 2
                    continue
                if item[end] == "'":
                    break
                end += 1
            item = item[1:end].replace("''", "'") + item[end + 1:]
        if item.startswith("/"):
            continue
        node = ARIA_NODE.match(item)
        if not node:
            continue
        role, name, value = node.group(1), node.group(2), node.group(5)
        if bullet is not None and depth <= bullet:
            bullet = None
        if role in ARIA_SKIPPED:
            skip_depth = depth
            continue
        texts = []
        if role == "heading" and name:
            texts.append(_unquote('"%s"' % name))
        if role == "listitem":
            bullet = depth
        if value and value.strip():
            texts.append(_unquote(value))
        for text in texts:
            if not text.strip():
                continue
            lines.append(("• " + text) if bullet is not None else text)
            bullet = None
    return "\n".join(lines)


def _sections(text):
    """The response's own "### <title>" sections, in order: [(title, codeframe, lines)].

    A header line inside a fenced section (```...```) is content, not a section."""
    sections, fence = [], False
    for line in text.split("\n"):
        if fence:
            if line == "```":
                fence = False
            else:
                sections[-1][2].append(line)
        elif line.startswith("### "):
            sections.append((line[4:], None, []))
        elif sections and not sections[-1][2] and sections[-1][1] is None and line.startswith("```") and len(line) > 3:
            sections[-1] = (sections[-1][0], line[3:], [])
            fence = True
        elif sections:
            sections[-1][2].append(line)
    return sections


def page_read(text, folder, tool=None):
    """(page_url, main_text) of one tool output that is a page read, else None.

    @playwright/mcp: only a browser_navigate / browser_snapshot answer, laid out as the driver
    builds it (Error, Result, Ran code, Open tabs, Page, Modal state, Snapshot, Events): no Error
    or Result section (page-controlled console text lands in Result), exactly one Page section
    whose first line is "- Page URL:" and no non-2xx "- HTTP status:", then one Snapshot."""
    header = HEADER.match(text or "")
    if header:
        return header.group(1), extract_ax_text(text)
    if tool not in PAGE_READ_TOOLS or not text:
        return None
    sections = _sections(text)
    titles = [title for title, _, _ in sections]
    if "Error" in titles or "Result" in titles or titles.count("Page") != 1 or titles.count("Snapshot") != 1 \
            or titles.index("Page") > titles.index("Snapshot"):
        return None
    _, _, page = sections[titles.index("Page")]
    page = [line for line in page if line]
    if not page or not page[0].startswith("- Page URL: ") or not all(line.startswith("- ") for line in page):
        return None
    url = page[0][len("- Page URL: "):].strip()
    for line in page:
        status = HTTP_STATUS.match(line)
        if status and not 200 <= int(status.group(1)) < 300:
            return None
    _, codeframe, snapshot = sections[titles.index("Snapshot")]
    if codeframe == "yaml":
        return url, extract_aria_text("\n".join(snapshot))
    link = SNAPSHOT_LINK.match(snapshot[0]) if codeframe is None and snapshot else None
    if link:   # the driver saved the snapshot as a file in the run's browser/ folder
        saved = Path(folder) / "browser" / Path(link.group(1)).name
        try:
            if not saved.name.endswith(".yml") or not saved.is_file() or saved.stat().st_size > MAX_SNAPSHOT_BYTES:
                return None
            return url, extract_aria_text(saved.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            return None
    return None


def _reads(root, job_key, url):
    """Yield (observed_at, source, page_url, main_text) for every saved read of this posting."""
    target = _page_key(url) if url else None
    directory = Path(root) / BROWSER_DIRECTORY
    for request_path in sorted(directory.glob("request-*/request.json")):
        folder = request_path.parent
        try:
            request_key, request_url = _request_job(request_path)
        except (OSError, ValueError):
            continue
        if not (job_key and request_key == job_key) and not (target and request_url and _page_key(request_url) == target):
            continue
        page = target or (_page_key(request_url) if request_url else None)
        trace = folder / "trace.jsonl"
        if page is None or not trace.is_file():
            continue
        observed = None
        try:
            observed = _load(json.loads((folder / "result.json").read_text())).get("observed_at")
        except (OSError, ValueError):
            pass
        if not observed:
            observed = dt.datetime.fromtimestamp(trace.stat().st_mtime, dt.timezone.utc).isoformat()
        try:   # the engine that wrote this session (engine.json; none: Codex)
            reader = engines.for_run(folder).trace_reader()
        except engines.EngineRecordError:
            continue
        with trace.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    events = reader.feed(line)
                except ValueError:
                    continue
                for event in events:
                    call = event.call
                    if (event.phase != "completed" or event.kind != "tool"
                            or call.status != "completed" or call.error):
                        continue
                    for content in _content(call):
                        text = content.get("text") if isinstance(content, dict) else None
                        read = page_read(text, folder, call.tool)
                        if read and _page_key(read[0]) == page:
                            yield observed, f"{folder.relative_to(root)}/trace.jsonl#{call.id}", read[0], read[1]


def job_description(database, root, job_key=None, url=None, *, opportunity=None, max_chars=MAX_CHARS):
    """Most recent saved browser read of the official posting, else research excerpts.

    Pass opportunity (a research rw_results opportunity) to skip the database lookup.
    """
    if opportunity is None and database is not None:
        opportunity = _opportunity(database, job_key, url)
    job_key = job_key or (opportunity or {}).get("job_key")
    if opportunity and not url:
        url = opportunity.get("url")
    best = None
    for observed, source, page_url, text in _reads(root, job_key, url):
        if text.count("\n") + 1 < MIN_TEXT_NODES:
            continue
        if best is None or _instant(observed) > _instant(best["observed_at"]):
            best = {"state": "BROWSER_READ", "observed_at": observed, "source": source, "url": page_url, "text": text}
    if best is None and opportunity:
        evidence = opportunity.get("review", {}).get("fact_evidence", {})
        excerpts = [f"{field}: {record.get('excerpt')}" for field, record in sorted(evidence.items())
                    if isinstance(record, dict) and isinstance(record.get("excerpt"), str) and record["excerpt"].strip()]
        if excerpts:
            best = {"state": "RESEARCH_EXCERPTS", "observed_at": max(str(record.get("observed_at", "")) for record in evidence.values()
                                                                      if isinstance(record, dict)),
                    "source": "rw_results:" + str(opportunity.get("job_key")), "url": opportunity.get("url"),
                    "text": "\n".join(excerpts)}
    if best is None:
        return {"state": "UNAVAILABLE", "job_key": job_key, "url": url}
    best["truncated"] = len(best["text"]) > max_chars
    best["text"] = best["text"][:max_chars]
    best["sha256"] = hashlib.sha256(best["text"].encode()).hexdigest()
    best["chars"] = len(best["text"])
    best["job_key"] = job_key or (opportunity or {}).get("job_key")
    best["trust"] = "UNTRUSTED_POSTING_TEXT"
    return best


def _instant(value):
    try:
        moment = dt.datetime.fromisoformat(value)
        return moment if moment.tzinfo else moment.replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        return dt.datetime.min.replace(tzinfo=dt.timezone.utc)


def load_recorded(root, record, max_chars=MAX_CHARS):
    """Re-extract the exact recorded browser read (source path#item) and verify its hash."""
    if not isinstance(record, dict) or record.get("state") != "BROWSER_READ":
        raise ValueError("POSTING_RECORD_UNSUPPORTED")
    path, _, item_id = str(record.get("source", "")).partition("#")
    trace = (Path(root) / path).resolve()
    if not str(trace).startswith(str((Path(root) / BROWSER_DIRECTORY).resolve())) or trace.name != "trace.jsonl":
        raise ValueError("POSTING_SOURCE_OUTSIDE_BROWSER_REPORTS")
    reader = engines.for_run(trace.parent).trace_reader()   # the engine that wrote it (none recorded: Codex)
    with trace.open(encoding="utf-8") as handle:
        for line in handle:
            for event in reader.feed(line):
                call = event.call
                if event.kind == "tool" and call.id == item_id and call.status == "completed":
                    for content in _content(call):
                        text = content.get("text") if isinstance(content, dict) else None
                        read = page_read(text, trace.parent, call.tool)
                        if read and _page_key(read[0]) == _page_key(record.get("url", "")):
                            extracted = read[1][:max_chars]
                            if hashlib.sha256(extracted.encode()).hexdigest() == record.get("sha256"):
                                return extracted
    raise ValueError("POSTING_SOURCE_CHANGED")


def _content(call):
    """The content blocks of a tool call's answer ([] when it has none)."""
    return (call.result or {}).get("content") or []


def _opportunity(database, job_key, url):
    if not database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='rw_results'").fetchone():
        return None
    target = _page_key(url) if url else None
    found = None
    for (processed,) in database.execute("SELECT processed FROM rw_results ORDER BY id"):
        for opportunity in _load(processed).get("opportunities", []):
            if (job_key and opportunity.get("job_key") == job_key) or (
                    target and opportunity.get("url") and _page_key(opportunity["url"]) == target):
                found = opportunity
    return found
