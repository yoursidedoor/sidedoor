"""Read-only full job-description text from saved browser reads of the posting.

job_description(database, root, job_key=None, url=None) returns the posting's main
text as last observed by an existing application-browser read (no network, no
browser): reports/application-browser/request-*/trace.jsonl tool OUTPUTS whose page
header URL is the posting URL. Text is extracted structurally from the accessibility
tree the browser tool returned (AX roles), not by matching prose. When no browser
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
import paths


MAX_CHARS = 20000
MIN_TEXT_NODES = 5
BROWSER_DIRECTORY = paths.REPORTS + "/application-browser"   # under the data folder (paths.reports)
HEADER = re.compile(r'^Browser tab: [^\n]*?URL: "([^"]+)"')
AX_LINE = re.compile(r"^(\t*)(\d+) (\S+)(?: \([^)]*\))?(?: (.*))?$")
KEPT_ROLES = {"text", "AXStaticText", "AXListMarker"}
SKIPPED_SUBTREES = {"link", "button", "tab", "textbox", "AXTextField", "combobox", "checkbox", "radio", "form"}


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


def _reads(root, job_key, url):
    """Yield (observed_at, source, page_url, raw_output) for every saved read of this posting."""
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
        with trace.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                item = event.get("item") or {}
                if (event.get("type") != "item.completed" or item.get("type") != "mcp_tool_call"
                        or item.get("status") != "completed" or item.get("error")):
                    continue
                for content in (item.get("result") or {}).get("content") or []:
                    text = content.get("text") if isinstance(content, dict) else None
                    header = HEADER.match(text or "")
                    if header and _page_key(header.group(1)) == page:
                        yield observed, f"{folder.relative_to(root)}/trace.jsonl#{item.get('id')}", header.group(1), text


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
    for observed, source, page_url, raw in _reads(root, job_key, url):
        text = extract_ax_text(raw)
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
    with trace.open(encoding="utf-8") as handle:
        for line in handle:
            item = (json.loads(line).get("item") or {})
            if item.get("id") == item_id and item.get("type") == "mcp_tool_call" and item.get("status") == "completed":
                for content in (item.get("result") or {}).get("content") or []:
                    text = content.get("text") if isinstance(content, dict) else None
                    header = HEADER.match(text or "")
                    if header and _page_key(header.group(1)) == _page_key(record.get("url", "")):
                        extracted = extract_ax_text(text)[:max_chars]
                        if hashlib.sha256(extracted.encode()).hexdigest() == record.get("sha256"):
                            return extracted
    raise ValueError("POSTING_SOURCE_CHANGED")


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
