#!/opt/homebrew/bin/python3.13
import hashlib
import json
import os
from pathlib import Path
import sys


def evidence(field, value, observed_at, source):
    return {"field": field, "value": value, "source": source,
            "observed_at": observed_at, "effective_at": None, "excerpt": "SYNTHETIC local worker evidence",
            "verified": True, "kind": "official"}


def opportunity(company, industry, observed_at):
    requisition = hashlib.sha256(company.encode()).hexdigest()[:12]
    destination = "https://fixture-research.org/jobs/" + requisition
    facts = {"company": company, "role_title": "Staff Software Engineer", "requisition_id": requisition,
             "open": True, "funding_stage": "Series B", "industry": industry,
             "level": "Staff Software Engineer", "specialization": "backend", "country": "US",
             "city": "Seattle", "work_mode": "remote", "requirements_met": True,
             "authorization": "US_PERMANENT_RESIDENT_ACCEPTED", "hiring_geography_includes_candidate": True,
             "citizenship_clearance_requirements_met": True, "employee_count": "51–200",
             "official_destination": destination, "team_need": "reliable APIs",
             "compensation": {"minimum": 200000, "maximum": 250000, "currency": "USD", "period": "annual_base"}}
    return {"company": company, "role": "Staff Software Engineer", "url": facts["official_destination"],
            "rationale": "SYNTHETIC test fixture only", "requisition_id": requisition, "people": [],
            "role_fit": {"fit": "STRONG", "reason": "SYNTHETIC fixture: Staff backend role matches target levels"},
            "policy_fit": {"funding_stage": {"in_scope": True, "reason": "SYNTHETIC fixture"}, "location": {"in_scope": True, "reason": "SYNTHETIC fixture"}},
            "in_user_home_area": None,
            "evidence": [evidence(field, value, observed_at, destination) for field, value in facts.items()]}


def main():
    prompt = sys.stdin.read()
    output = Path(sys.argv[sys.argv.index("--output-last-message") + 1])
    schema_path = Path(sys.argv[sys.argv.index("--output-schema") + 1])
    schema = schema_path.name
    # Behave like the provider: strict structured output rejects any object that declares
    # a property it does not require, before a model ever runs. Production failed exactly
    # this way on 2026-09-19 while this fake, which never read the schema, kept passing.
    document = json.loads(schema_path.read_text())
    undeclared = [key for key in document.get("properties", {}) if key not in document.get("required", [])]
    if undeclared:
        print(f"invalid_json_schema: Missing {undeclared!r}. param: text.format.schema", file=sys.stderr)
        sys.exit(1)
    custom = json.loads(os.environ["JOB_SEARCH_FIXTURE_OPPORTUNITY"]) if "JOB_SEARCH_FIXTURE_OPPORTUNITY" in os.environ else None
    if schema == "discovery-schema.json":
        scope = json.loads(prompt.rsplit("\n\n", 1)[1])
        industry = scope["industries_in_priority_order"][0]
        company = custom["company"] if custom else "Fixture " + industry
        result = {"discovery_status": "COMPLETE", "summary": "SYNTHETIC industry discovery", "blockers": [],
                  "inability": None,
                  "companies": [{"company": company, "industry": industry,
                                 "source": "https://fixture-research.org/companies", "observed_at": scope["now"],
                                 "excerpt": company + " is a SYNTHETIC discovered company.", "excerpt_names_company": True}]}
    elif schema == "research-schema.json":
        context = json.loads(prompt.rsplit("\n\nDATA:\n", 1)[1])
        status = os.environ.get("JOB_SEARCH_FIXTURE_STATUS", "COMPLETE")
        assignments = context["research_plan"]["assignments"]
        result = {"research_status": status, "summary": "End-to-end fixture", "inability": None,
                  "blockers": [] if status == "COMPLETE" else ["SYNTHETIC incomplete research"],
                  "action_blockers": [], "scope_violation": {"unknown": False, "companies": []},
                  "company_findings": [], "drafts": [],
                  "opportunities": [custom] if custom else [opportunity(assignment["company"], context["industry"], context["today"])
                                                            for assignment in assignments]}
        if status != "COMPLETE" and not custom:
            # An incomplete fixture models what an incomplete worker actually returns: every
            # assigned company recorded UNRESOLVED, with no role proven open.
            result["opportunities"] = []
            result["company_findings"] = [
                {"company": assignment["company"], "status": "UNRESOLVED",
                 "source": "https://fixture-research.org/careers", "observed_at": context["today"],
                 "excerpt": "SYNTHETIC careers page unreadable", "verified": False, "kind": "unknown"}
                for assignment in assignments]
    elif schema == "recovery-schema.json":
        assert 'web_search="disabled"' in sys.argv
        assert "features.multi_agent=false" in sys.argv
        result = {"action": "ABANDON", "method": "", "instructions": "",
                  "reason": "SYNTHETIC no supported alternative", "progress": False}
    else:
        raise ValueError("Fake worker accepts only research workflow schemas")
    output.write_text(json.dumps(result))


if __name__ == "__main__":
    main()
