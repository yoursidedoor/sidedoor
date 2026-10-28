"""Seed-free discovery inputs and offline evidence checks; no provider or DB access.

The caller supplies remaining daily company allowance as limit and retains the
unaltered result alongside returned names. Validation checks evidence shape and
recency, not source authenticity, industry truth, or company/role eligibility.
"""

import resources
import datetime as dt
import ipaddress
import json
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlsplit


def _timestamp(value):
    value = dt.datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if not isinstance(value, dt.datetime) or value.utcoffset() is None:
        raise ValueError("Timezone-aware ISO timestamp or datetime required")
    return value.astimezone(dt.timezone.utc)


def _text(value, maximum):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


def _key(value):
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", value).casefold()))


def _scope(policy, now, limit):
    industries = policy.get("industries") if isinstance(policy, dict) else None
    if (not isinstance(industries, list) or not industries
            or any(not _text(industry, 120) or not _key(industry) for industry in industries)
            or len({_key(industry) for industry in industries}) != len(industries)):
        raise ValueError("policy.industries must be a nonempty unique list of names")
    if type(limit) is not int or limit < 0:
        raise ValueError("limit must be the nonnegative remaining daily allowance")
    return industries, _timestamp(now), min(20, limit)


def _public_url(value):
    if not _text(value, 2048) or re.search(r"\s|[\\\\\x00-\x1f\x7f]", value):
        raise ValueError("Public HTTPS source URL required")
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if (parsed.scheme != "https" or parsed.username is not None
            or parsed.password is not None or parsed.port not in {None, 443}
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host)
            or "." not in host or ".." in host or not host.rsplit(".", 1)[-1].isalpha()
            or host.endswith((".local", ".localhost", ".internal", ".test", ".invalid", ".example"))
            or any(host == domain or host.endswith("." + domain)
                   for domain in ("example.com", "example.org", "example.net"))):
        raise ValueError("Public non-placeholder source URL required")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return
    raise ValueError("Use a public source hostname, not an IP address")


def inputs(root, policy, now, limit):
    """Return prompt str; read only discovery-prompt.md, whitelist search policy."""
    industries, current, maximum = _scope(policy, now, limit)
    # The user's own stages or company types, as picked in Setup (E4.T1); an empty or missing
    # list means any stage. Only the shape is checked: the AI judges the stage words.
    funding_stages = policy.get("funding_stages", [])
    if not isinstance(funding_stages, list) or any(not isinstance(stage, str) for stage in funding_stages):
        raise ValueError("funding_stages must be a list of strings")
    scope = {"industries_in_priority_order": industries, "now": current.isoformat(),
             "funding_stages": funding_stages, "maximum_companies": maximum, "source_max_age_hours": 24}
    return resources.read_text("discovery-prompt.md") + "\n\n" + json.dumps(scope)


def validate(result, policy, now, limit, dropped=None):
    """Return the names of the companies kept, or raise ValueError for a malformed envelope.

    One bad company no longer rejects the whole discovery (owner, 2026-10-27: "One bad entry no
    longer rejects the whole set"): a company whose record fails a check below is left out, and
    `dropped` (a list, when given) gets {"company", "reason"} for it in plain words. Only the
    envelope (its fields, status, summary, blockers, allowance) still fails the whole result."""
    return [record["company"].strip() for record in screen(result, policy, now, limit, dropped)["companies"]]


def screen(result, policy, now, limit, dropped=None):
    """The discovery result with only its well-formed companies (same schema fields), or raise
    ValueError for a malformed envelope. Each left-out company is appended to `dropped`."""
    industries, current, maximum = _scope(policy, now, limit)
    required = {"discovery_status", "summary", "companies", "blockers"}
    if not isinstance(result, dict) or set(result) != required:
        raise ValueError("Discovery result must contain exactly the schema fields")
    status, companies, blockers = result["discovery_status"], result["companies"], result["blockers"]
    if (status not in ("COMPLETE", "PARTIAL", "FAILED") or not _text(result["summary"], 4000)
            or not isinstance(companies, list) or len(companies) > maximum
            or not isinstance(blockers, list) or any(not _text(item, 2000) for item in blockers)):
        raise ValueError("Invalid discovery status, summary, blockers, or company allowance")
    if (status == "COMPLETE" and blockers or status != "COMPLETE" and not blockers
            or status == "FAILED" and companies):
        raise ValueError("Status must agree with blockers and discovered companies")
    kept, seen = [], set()
    dropped = [] if dropped is None else dropped
    for company in companies:
        try:
            name = check_company(company, industries, current, seen)
        except ValueError as error:
            label = company.get("company") if isinstance(company, dict) else None
            dropped.append({"company": " ".join(label.split())[:200] if isinstance(label, str) and label.strip() else None,
                            "reason": str(error),
                            # What the entry is (never shown): keeps two different records apart.
                            "source": company.get("source") if isinstance(company, dict) and isinstance(company.get("source"), str) else None})
            continue
        seen.add(name)
        kept.append(company)
    return dict(result, companies=kept)


def check_company(company, industries, current, seen):
    """One discovered company's checks; returns its key or raises ValueError with a plain reason.
    The same checks run when the AI saves the company as it goes (research_tools.save_company)."""
    fields = {"company", "industry", "source", "observed_at", "excerpt", "excerpt_names_company"}
    if (not isinstance(company, dict) or set(company) != fields
            or any(not _text(company[field], size) for field, size in
                   (("company", 200), ("industry", 120), ("source", 2048),
                    ("observed_at", 64), ("excerpt", 4000)))):
        raise ValueError("its record did not have the expected fields")
    name = _key(company["company"])
    # The model attests that the excerpt names the company (owner, 2026-09-23). This used to
    # require the name, space-padded, to appear VERBATIM inside the excerpt - literal prose
    # matching of the kind the owner has banned. "Sprocket AI" fails it when the page says
    # "Sprocket's platform", "Sprocket, Inc." or just "Sprocket", and Defense abandoned a
    # whole industry on exactly that. Whether a piece of text names a company is a judgement
    # about language, so the read makes it. The source URL, the industry allowlist and
    # uniqueness are all still checked here, so an invented company still cannot enter.
    # The attestation is required, so every record judges; a read that cannot find an
    # excerpt naming the company sets it false and picks a different excerpt.
    if not name:
        raise ValueError("it had no company name")
    if name in seen:
        raise ValueError("it was listed twice")
    if company["industry"] not in industries:
        raise ValueError("it was found for a different industry")
    if company["excerpt_names_company"] is not True:
        raise ValueError("its quoted source did not name the company")
    try:
        _public_url(company["source"])
    except ValueError:
        raise ValueError("its source link was not a public web address") from None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", company["observed_at"]):
        raise ValueError("the time its source was read was not a valid date")
    # No 24-hour expiry (owner, 2026-09-23). A page read yesterday was rejected today, and
    # nothing re-reads it, so a real observation became a permanent refusal. The date is
    # still required, still parsed, and still may not be in the future; whether the role is
    # actually open is re-checked by the application read, which is what caught litware's
    # dead requisition.
    try:
        observed = _timestamp(company["observed_at"])
    except ValueError:
        raise ValueError("the time its source was read was not a valid date") from None
    if current - observed < dt.timedelta(0):
        raise ValueError("the time its source was read was in the future")
    return name
