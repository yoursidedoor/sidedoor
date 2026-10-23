"""Read saved source observations for a committed stage-2 plan, without promotion.

File root: {"version": 1, "observations": [...]}. Each observation contains
plan_id, company, source, observed_at, excerpt, and provenance, plus an optional
factual summary (up to 4000 characters). No nested evidence list. Provenance contains
method (foreground_browser_read_only or public_page_read_only), source (the exact
observation URL), and reference (the original capture/session/evidence reference).
This validates structure and scope, not source truth or unattended capability.
"""

import datetime as dt
import json
from pathlib import Path
import re

import industry_discovery
import workflow_tasks
import paths


def load(root, plan, now):
    """Return unchanged matching observations; malformed supplied evidence raises."""
    path = paths.observations(root)
    try:
        with path.open(encoding="utf-8") as stream:
            content = stream.read(1_000_001)
    except FileNotFoundError:
        return []
    if len(content) > 1_000_000:
        raise ValueError("Source observations file exceeds limit")
    if not content.strip():
        return []
    document = json.loads(content)
    if (not isinstance(document, dict) or set(document) != {"version", "observations"}
            or type(document["version"]) is not int or document["version"] != 1
            or not isinstance(document["observations"], list)
            or len(document["observations"]) > 120):
        raise ValueError("Invalid source observations root")
    if not document["observations"]:
        return []
    current = industry_discovery._timestamp(now)
    if (not isinstance(plan, dict) or plan.get("scope_enforced") is not True
            or not industry_discovery._text(plan.get("plan_id"), 200)
            or plan.get("day") != workflow_tasks.service_date(current, "America/Los_Angeles")
            or not isinstance(plan.get("assignments"), list)):
        raise ValueError("Current committed research plan required")
    companies = set()
    for assignment in plan["assignments"]:
        if (not isinstance(assignment, dict)
                or not industry_discovery._text(assignment.get("company"), 200)):
            raise ValueError("Invalid research assignment")
        company = industry_discovery._key(assignment["company"])
        if not company:
            raise ValueError("Empty research company identity")
        companies.add(company)
    fields = {"plan_id", "company", "source", "observed_at", "excerpt", "provenance"}
    output = []
    for record in document["observations"]:
        if (not isinstance(record, dict)
                or not industry_discovery._text(record.get("plan_id"), 200)):
            raise ValueError("Source observation requires a plan binding")
        if record["plan_id"] != plan["plan_id"]:
            continue
        if (not industry_discovery._text(record.get("company"), 200)
                or not industry_discovery._key(record["company"])):
            raise ValueError("Source observation requires a company for scope filtering")
        if industry_discovery._key(record["company"]) not in companies:
            continue
        if not fields <= set(record) or set(record) - fields - {"summary"}:
            raise ValueError("Source observation schema mismatch")
        industry_discovery._public_url(record["source"])
        if (not industry_discovery._text(record["observed_at"], 64)
                or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", record["observed_at"])):
            raise ValueError("Original timezone-aware source timestamp required")
        age = current - industry_discovery._timestamp(record["observed_at"])
        if not dt.timedelta(0) <= age <= dt.timedelta(hours=24):
            raise ValueError("Source observation is future-dated or stale")
        if not industry_discovery._text(record["excerpt"], 12000):
            raise ValueError("Source excerpt must be nonempty and bounded")
        if "summary" in record and not industry_discovery._text(record["summary"], 4000):
            raise ValueError("Source summary must be nonempty and bounded when supplied")
        provenance = record["provenance"]
        if (not isinstance(provenance, dict) or set(provenance) != {"method", "source", "reference"}
                or provenance["method"] not in ("foreground_browser_read_only", "public_page_read_only")
                or provenance["source"] != record["source"]
                or not industry_discovery._text(provenance["reference"], 2000)):
            raise ValueError("Exact-source read-only provenance required")
        output.append(record)
    return output
