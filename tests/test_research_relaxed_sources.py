"""Owner decisions of 2026-09-19 ("way too strict", "relax"): D-1 source tiers, D-2 an
ambiguous company never fails its industry, D-3 AI role fit enforced by Python.

Each test failed on the code installed before these decisions.
"""

import datetime as dt
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import orchestrator
import parallel_research
import research_workflow
from test_research_integration import validate_schema_subset
import profile_home
import user_profile


ROOT = Path(__file__).resolve().parents[1]


def now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def evidence(field, value, *, kind="official", source="https://employer.example/jobs/1", effective_at=None,
             verified=True, observed_at=None):
    return {"field": field, "value": value, "source": source, "observed_at": observed_at or now(),
            "effective_at": effective_at,
            "excerpt": "SYNTHETIC supporting text", "verified": verified, "kind": kind}


def role(company, title="Staff Software Engineer", *, url="https://employer.example/jobs/1", open_kind="official",
         open_effective_at=None, destination=True, fit="STRONG", extra=()):
    items = [evidence("company", company, source=url), evidence("role_title", title, source=url),
             evidence("open", True, kind=open_kind, source=url, effective_at=open_effective_at)]
    if destination:
        items.append(evidence("official_destination", url, source=url))
    return {"company": company, "role": title, "url": url, "rationale": "SYNTHETIC", "requisition_id": "",
            "people": [], "evidence": items + list(extra), "role_fit": {"fit": fit, "reason": "SYNTHETIC"},
            "policy_fit": {"funding_stage": {"in_scope": True, "reason": "SYNTHETIC"}, "location": {"in_scope": True, "reason": "SYNTHETIC"}},
            "in_user_home_area": None}


def result(opportunities=(), findings=()):
    return {"research_status": "PARTIAL", "summary": "SYNTHETIC", "blockers": ["SYNTHETIC free-text blocker"],
            "action_blockers": [], "inability": None, "scope_violation": {"unknown": False, "companies": []},
            "company_findings": list(findings), "opportunities": list(opportunities), "drafts": []}


def unresolved(company):
    return {"company": company, "status": "UNRESOLVED", "source": "https://employer.example/careers",
            "observed_at": now(), "excerpt": "Careers page lists no roles but a posting shows Apply",
            "verified": False, "kind": "official"}


class ProcessResearchRelaxedRules(unittest.TestCase):
    def setUp(self):
        self.database = sqlite3.connect(":memory:")
        self.addCleanup(self.database.close)
        research_workflow.initialize(self.database)
        research_workflow.plan_research(self.database, "plan", ["Alpha", "Beta"], now=now())
        self.schema = json.loads((ROOT / "research-schema.json").read_text())

    def process(self, payload):
        validate_schema_subset(payload, self.schema, self.schema)
        return research_workflow.process_research(self.database, payload, plan_id="plan", now=now())

    def test_one_unresolved_company_does_not_fail_the_industry(self):
        """D-2: BioTech failed on 2026-09-19 because one company's evidence conflicted."""
        processed = self.process(result([role("Alpha")], [unresolved("Beta")]))
        self.assertEqual(processed["industry_status"], "COMPLETE")
        resolution = processed["company_resolution"]
        self.assertEqual([entry["company"] for entry in resolution["resolved"]], ["alpha"])
        self.assertEqual([entry["company"] for entry in resolution["unresolved"]], ["beta"])
        self.assertEqual(resolution["unresolved"][0]["diagnostics"][0]["type"], "WORKER_UNRESOLVED_FINDING")
        self.assertEqual(len(processed["opportunities"]), 1)
        everything_unresolved = self.process(result([], [unresolved("Alpha"), unresolved("Beta")]))
        self.assertEqual(everything_unresolved["industry_status"], "COMPLETE")

    def test_employer_mirror_proves_open_but_agency_never_does(self):
        """D-1: the Getro/Wellfound listings of 2026-09-19 were refused as non-official."""
        recent = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10)).date().isoformat()
        mirror = "https://indexventures.getro.com/companies/alpha/jobs/1"
        agency = "https://www.lhh.com/us/en/jobs/2"
        processed = self.process(result([
            role("Alpha", url=mirror, open_kind="employer_mirror", open_effective_at=recent, destination=False),
            role("Beta", url=agency, open_kind="public", destination=False)]))
        self.assertEqual(processed["company_coverage"], {"covered": ["alpha"], "uncovered": ["beta"]})
        stored = {item["company"]: item for item in processed["opportunities"]}
        review = stored["Alpha"]["review"]
        self.assertEqual((review["open_basis"], review["destination_status"], review["mirror_source"]),
                         ("EMPLOYER_MIRROR_RECENT", "UNRESOLVED", mirror))
        # 2026-09-27: applications.assess no longer holds an unresolved destination; the
        # research record still labels its own unknowns (informational, not an application gate).
        self.assertEqual(stored["Alpha"]["assessment"][1], "RESEARCH_EVIDENCE_UNRESOLVED")
        self.assertEqual(stored["Beta"]["review"]["open_basis"], "NOT_PROVEN_SOURCE_KIND_PUBLIC")
        # An undated mirror contradicted by the employer's own dated read is not proof.
        an_hour_ago = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).replace(microsecond=0).isoformat()
        closed = evidence("open", False, source="https://employer.example/careers", observed_at=an_hour_ago)
        contradicted = self.process(result([role("Alpha", url=mirror, open_kind="employer_mirror",
                                                 destination=False, extra=[closed])]))
        self.assertEqual(contradicted["opportunities"][0]["review"]["open_basis"],
                         "NOT_PROVEN_MIRROR_CONTRADICTED_BY_EMPLOYER")
        self.assertEqual(contradicted["industry_status"], "COMPLETE")

    def test_role_fit_none_is_rejected_not_stored(self):
        """D-3: a VP, Biotherapeutics Discovery role was stored for a software engineer."""
        processed = self.process(result([
            role("Alpha", "VP, Biotherapeutics Discovery", url="https://employer.example/jobs/vp", fit="NONE"),
            role("Beta", url="https://employer.example/jobs/2", fit="PARTIAL")]))
        self.assertEqual([item["role"] for item in processed["opportunities"]], ["Staff Software Engineer"])
        self.assertEqual(processed["opportunities"][0]["role_fit"]["fit"], "PARTIAL")
        self.assertEqual([(item["role"], item["reason"]) for item in processed["rejected_opportunities"]],
                         [("VP, Biotherapeutics Discovery", "ROLE_FIT_NONE")])


class IndustryStateFollowsCompanyResolution(unittest.TestCase):
    """D-2 through the real coordinator: the industry state comes from company resolution."""

    def test_partial_worker_with_one_resolved_company_completes_its_industry(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())  # a cleanup: runs after the connections the test closes (Windows)
        root = Path(directory)
        for folder in ("state", "reports", "logs"):
            (root / folder).mkdir()
        shutil.copyfile(ROOT / "discovery-prompt.md", root / "discovery-prompt.md")
        policy = user_profile.load()
        policy["industries"] = ["AI"]
        profile_home.use(self, policy)
        database = orchestrator.connect(root)
        self.addCleanup(database.close)
        stored = []

        def research_inputs(root, config, database, pipeline, observation, output, *, companies, industry, discovery):
            research_workflow.seed_history(database, pipeline, history_complete=True)
            plan_id = "relaxed-plan:scope:" + config["_research_scope_id"]
            plan = research_workflow.plan_research(database, plan_id, companies, now=now(), limit=60)
            return plan, plan_id, "PROMPT", ("research-schema.json", output)

        def worker(root, command, prompt, log, timeout):
            schema, output = command
            if schema == "discovery-schema.json":
                payload = {"discovery_status": "COMPLETE", "summary": "SYNTHETIC", "blockers": [],
                           "companies": [{"company": name, "industry": "AI", "source": "https://www.nasa.gov/",
                                          "observed_at": now(), "excerpt": name + " builds AI tools.",
                                          "excerpt_names_company": True}
                                         for name in ("Alpha", "Beta")]}
            else:
                payload = result([role("Alpha")], [unresolved("Beta")])
            Path(output).write_text(json.dumps(payload))

        runtime = SimpleNamespace(
            RESEARCH_STOP=threading.Event(), utcnow=now,
            write_json=lambda path, payload: Path(path).write_text(json.dumps(payload)),
            research_command=lambda root, config, output, schema: (schema, output),
            research_inputs=research_inputs, run_research_worker=worker,
            store_research=lambda database, pipeline, processed: stored.extend(processed["opportunities"]) or [])
        with patch("socket.socket.connect", side_effect=AssertionError("Network prohibited")):
            final = parallel_research.run(root, {"research_timeout_seconds": 30, "research_company_limit": 2},
                                          database, {"companies": []}, {}, "relaxed-run",
                                          runtime=runtime, deadline=time.monotonic() + 30)
        industry = final["industries"][0]
        self.assertEqual((final["state"], industry["state"], industry["worker_status"]),
                         ("COMPLETE", "COMPLETE", "PARTIAL"))
        self.assertEqual([entry["company"] for entry in industry["unresolved_companies"]], ["beta"])
        self.assertEqual(final["unresolved_companies"], {"AI": industry["unresolved_companies"]})
        self.assertEqual([item["company"] for item in stored], ["Alpha"])


if __name__ == "__main__":
    unittest.main()
