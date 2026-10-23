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


def validate(result, policy, now, limit):
    """Return list[str] or raise ValueError; retain discovery sources in result."""
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
    names, seen = [], set()
    fields = {"company", "industry", "source", "observed_at", "excerpt", "excerpt_names_company"}
    for company in companies:
        if (not isinstance(company, dict) or set(company) != fields
                or any(not _text(company[field], size) for field, size in
                       (("company", 200), ("industry", 120), ("source", 2048),
                        ("observed_at", 64), ("excerpt", 4000)))):
            raise ValueError("Company discovery evidence does not match schema")
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
        named = company["excerpt_names_company"] is True
        if not name or name in seen or company["industry"] not in industries or not named:
            raise ValueError("Unique company, target industry, and an excerpt naming it required")
        _public_url(company["source"])
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", company["observed_at"]):
            raise ValueError("observed_at must be an ISO date-time with timezone")
        # No 24-hour expiry (owner, 2026-09-23). A page read yesterday was rejected today, and
        # nothing re-reads it, so a real observation became a permanent refusal. The date is
        # still required, still parsed, and still may not be in the future; whether the role is
        # actually open is re-checked by the application read, which is what caught litware's
        # dead requisition.
        age = current - _timestamp(company["observed_at"])
        if age < dt.timedelta(0):
            raise ValueError("Discovery source observation is future-dated")
        seen.add(name)
        names.append(company["company"].strip())
    return names
