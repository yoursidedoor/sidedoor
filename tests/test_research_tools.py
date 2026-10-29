"""R1 (owner, 2026-10-27, "1: B"): the research save tools over real stdio JSON-RPC, and the journal."""
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import profile_home  # noqa: F401  (no test reads the real home)
import research_tools

ROOT = Path(__file__).resolve().parents[1]
NOW = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def company(name="Real Discovery Co", **changes):
    return dict({"company": name, "industry": "AI", "source": "https://www.nasa.gov/", "observed_at": NOW,
                 "excerpt": name + " announced a round.", "excerpt_names_company": True}, **changes)


def role(company_name="Role Co", **changes):
    url = "https://jobs.ashbyhq.com/roleco/123"
    facts = {"company": company_name, "role_title": "Staff Engineer", "open": True}
    return dict({"company": company_name, "role": "Staff Engineer", "url": url, "requisition_id": "123",
                 "rationale": "Fits.", "people": [], "role_fit": {"fit": "STRONG", "reason": "Fits."},
                 "evidence": [{"field": field, "value": value, "source": url, "observed_at": NOW, "effective_at": None,
                               "excerpt": "Posting.", "verified": True, "kind": "official"}
                              for field, value in facts.items()]}, **changes)


class ResearchToolsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.output = Path(directory.name) / "run.AI.0.discovery.json"

    def serve(self, phase, calls, limit=2):
        options = research_tools.prepare(self.output, phase, "AI", limit, {"industries": ["AI"]}, {})
        argv = json.loads(next(o for o in options if ".args=" in o).split("=", 1)[1])
        command = json.loads(next(o for o in options if ".command=" in o).split("=", 1)[1])
        messages = [{"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}]
        messages += [{"jsonrpc": "2.0", "id": 2 + index, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
                     for index, (name, arguments) in enumerate(calls)]
        done = subprocess.run([command] + argv, input="".join(json.dumps(m) + "\n" for m in messages),
                              capture_output=True, text=True, cwd=ROOT, timeout=60)
        replies = [json.loads(line) for line in done.stdout.splitlines()]
        self.assertEqual(len(replies), len(messages), done.stderr)
        return replies

    def journal(self, phase):
        return research_tools.read_journal(research_tools.journal_path(self.output), phase)

    def test_save_company_journals_a_good_entry_and_answers_a_bad_one_in_plain_words(self):
        replies = self.serve("discovery", [("save_company", {"company": company()}),
                                           ("save_company", {"company": company("Bad Co", source="http://nasa.gov/")})])
        self.assertEqual([tool["name"] for tool in replies[1]["result"]["tools"]], ["save_company"])
        self.assertFalse(replies[2]["result"]["isError"])
        bad = replies[3]["result"]
        self.assertTrue(bad["isError"])
        self.assertIn("its source link was not a public web address", bad["content"][0]["text"])
        self.assertEqual([entry["company"] for entry in self.journal("discovery")], ["Real Discovery Co"])

    def test_a_time_a_few_minutes_ahead_is_saved_as_now_and_a_later_one_is_refused(self):
        # C3: Claude guesses observed_at slightly ahead of now; that guess is saved, stamped as now.
        ahead = lambda minutes: (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=minutes)).isoformat()
        replies = self.serve("discovery", [("save_company", {"company": company("Near Co", observed_at=ahead(2))}),
                                           ("save_company", {"company": company("Far Co", observed_at=ahead(30))})])
        self.assertFalse(replies[2]["result"]["isError"])
        self.assertTrue(replies[3]["result"]["isError"])
        self.assertIn("in the future", replies[3]["result"]["content"][0]["text"])
        [saved] = self.journal("discovery")
        self.assertEqual(saved["company"], "Near Co")
        self.assertLessEqual(dt.datetime.fromisoformat(saved["observed_at"]), dt.datetime.now(dt.timezone.utc))

    def test_save_company_holds_to_the_session_allowance_and_a_resave_replaces(self):
        replies = self.serve("discovery", [("save_company", {"company": company("One Co")}),
                                           ("save_company", {"company": company("One Co", excerpt="One Co, fixed.")}),
                                           ("save_company", {"company": company("Two Co")}),
                                           ("save_company", {"company": company("Three Co")})], limit=2)
        self.assertEqual([reply["result"]["isError"] for reply in replies[2:]], [False, False, False, True])
        self.assertEqual([(entry["company"], entry["excerpt"]) for entry in self.journal("discovery")],
                         [("One Co", "One Co, fixed."), ("Two Co", "Two Co announced a round.")])

    def test_save_role_journals_a_good_role_and_answers_a_bad_one_in_plain_words(self):
        replies = self.serve("qualification", [("save_role", {"role": role()}),
                                               ("save_role", {"role": role("Plain Co", url="http://plain.co/job")})])
        self.assertEqual([tool["name"] for tool in replies[1]["result"]["tools"]], ["save_role"])
        self.assertNotIn("$ref", json.dumps(replies[1]["result"]["tools"]))
        self.assertFalse(replies[2]["result"]["isError"], replies[2])
        self.assertTrue(replies[3]["result"]["isError"])
        self.assertIn("Plain Co: HTTPS opportunity required", replies[3]["result"]["content"][0]["text"])
        self.assertEqual([entry["company"] for entry in self.journal("qualification")], ["Role Co"])

    def test_a_half_written_last_line_is_skipped(self):
        path = research_tools.journal_path(self.output)
        path.write_text(json.dumps({"kind": "role", "entry": role()}) + "\n" + '{"kind": "role", "entry": {"comp')
        self.assertEqual([entry["company"] for entry in research_tools.read_journal(path, "qualification")], ["Role Co"])

    def test_a_torn_multibyte_line_loses_only_itself(self):
        # Review R1 S1: one torn write ("Zürich") never empties the whole journal.
        path = research_tools.journal_path(self.output)
        good = json.dumps({"kind": "role", "entry": role()}, ensure_ascii=False).encode()
        torn = json.dumps({"kind": "role", "entry": role("Zürich Co")}, ensure_ascii=False).encode()
        cut = torn.index("ü".encode()) + 1   # half of the two-byte character
        path.write_bytes(good + b"\n" + torn[:cut] + b"\n" + good.replace(b"Role Co", b"Later Co") + b"\n")
        self.assertEqual([entry["company"] for entry in research_tools.read_journal(path, "qualification")],
                         ["Role Co", "Later Co"])

    def test_bad_messages_get_an_error_and_the_server_keeps_serving(self):
        # Review R1 S2.
        research_tools.prepare(self.output, "discovery", "AI", 2, {"industries": ["AI"]}, {})
        lines = ["not json", "", "[1, 2]", json.dumps({"jsonrpc": "2.0", "id": 5, "method": "initialize", "params": None}),
                 json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                 json.dumps({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": None}),
                 json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/list"})]
        done = subprocess.run([sys.executable, str(ROOT / "research_tools.py"), "serve", "--context",
                               str(research_tools.context_path(self.output))],
                              input="\n".join(lines) + "\n", capture_output=True, text=True, cwd=ROOT, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        replies = [json.loads(line) for line in done.stdout.splitlines()]
        self.assertEqual([(reply.get("id"), (reply.get("error") or {}).get("code")) for reply in replies],
                         [(None, -32700), (None, -32600), (5, None), (6, None), (7, None)])
        self.assertTrue(replies[3]["result"]["isError"])
        self.assertEqual(replies[4]["result"]["tools"][0]["name"], "save_company")

    def test_an_undecodable_line_is_answered_and_the_server_keeps_serving(self):
        # Review T4.
        research_tools.prepare(self.output, "discovery", "AI", 2, {"industries": ["AI"]}, {})
        done = subprocess.run([sys.executable, str(ROOT / "research_tools.py"), "serve", "--context",
                               str(research_tools.context_path(self.output))],
                              input=b"\xff\xfe\n" + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode() + b"\n",
                              capture_output=True, cwd=ROOT, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        replies = [json.loads(line) for line in done.stdout.decode().splitlines()]
        self.assertEqual(replies[0]["error"]["code"], -32700)
        self.assertEqual(replies[1]["result"]["tools"][0]["name"], "save_company")

    def test_cut_off_note_names_an_unusable_answer(self):
        # Review T2.
        self.assertIn("it ended without a usable final answer", research_tools.cut_off_note(ValueError(), [1], "role", answered=True))
        self.assertIn("it ended before giving its final answer", research_tools.cut_off_note(ValueError(), [1], "role"))

    def test_saved_companies_older_than_a_week_are_cleaned_when_writing(self):
        # Review T5.
        import orchestrator
        import research_workflow
        (Path(self.output).parent / "state").mkdir()
        database = orchestrator.connect(Path(self.output).parent)
        self.addCleanup(database.close)
        research_workflow.initialize(database)
        research_workflow.record_saved_companies(database, "old-plan", ["old"], "2026-10-01T00:00:00+00:00")
        research_workflow.record_saved_companies(database, "new-plan", ["new"], "2026-10-27T00:00:00+00:00")
        self.assertEqual(research_workflow.saved_companies(database, "old-plan"), [])
        self.assertEqual(research_workflow.saved_companies(database, "new-plan"), ["new"])

    def test_deep_nesting_is_a_parse_error_and_the_server_keeps_serving(self):
        # Review U3.
        research_tools.prepare(self.output, "discovery", "AI", 2, {"industries": ["AI"]}, {})
        done = subprocess.run([sys.executable, str(ROOT / "research_tools.py"), "serve", "--context",
                               str(research_tools.context_path(self.output))],
                              input=b"[" * 200000 + b"\n" + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode() + b"\n",
                              capture_output=True, cwd=ROOT, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr[-500:])
        replies = [json.loads(line) for line in done.stdout.decode().splitlines()]
        self.assertEqual(replies[0]["error"]["code"], -32700)
        self.assertEqual(replies[1]["result"]["tools"][0]["name"], "save_company")

    def test_a_proven_saved_role_replaces_the_unproven_final_role_at_its_company(self):
        # Review U2: one job never stands twice (an unproven final copy beside a proven saved one).
        import research_workflow
        unproven = role(url="https://jobs.ashbyhq.com/roleco/old", requisition_id="old",
                        evidence=[entry for entry in role()["evidence"] if entry["field"] != "open"])
        proven = role(url="https://jobs.ashbyhq.com/roleco/new", requisition_id="new")
        self.assertTrue(research_workflow.role_sourced(research_workflow.check_role(proven, now=NOW)[0]))
        merged = research_tools.merge_research({"opportunities": [unproven], "company_findings": []}, [proven])
        self.assertEqual(merged["opportunities"], [proven])

    def test_a_proven_saved_copy_of_the_same_job_replaces_an_unproven_final_copy(self):
        # Review V1: same job identity, the final copy valid but not proven open, the saved copy proven.
        import research_workflow
        unproven = role(evidence=[entry for entry in role()["evidence"] if entry["field"] != "open"])
        self.assertEqual(research_tools.role_key(unproven), research_tools.role_key(role()))
        merged = research_tools.merge_research({"opportunities": [unproven], "company_findings": []}, [role()])
        self.assertEqual(merged["opportunities"], [role()])
        self.assertEqual(research_workflow.covered_companies(merged, now=NOW), {"role co"})

    def test_coverage_treats_an_unexplained_none_fit_as_the_store_does(self):
        # Review U4: fit NONE without a reason is kept by the store (not assessed), so it still contradicts a finding.
        import research_workflow
        retitled = role(role="Senior Engineer")   # its evidence names another title: open, not sourced
        finding = {"company": "Role Co", "status": "NO_OPEN_ROLES", "source": "https://jobs.ashbyhq.com/roleco",
                   "observed_at": NOW, "excerpt": "No open roles.", "verified": True, "kind": "official"}
        unexplained = dict(retitled, role_fit={"fit": "NONE", "reason": None})
        explained = dict(retitled, role_fit={"fit": "NONE", "reason": "Not a fit."})
        covered = lambda item: research_workflow.covered_companies(
            {"opportunities": [item], "company_findings": [finding]}, now=NOW)
        self.assertEqual(covered(unexplained), set())
        self.assertEqual(covered(explained), {"role co"})

    def test_the_final_output_wins_over_a_saved_copy_with_another_link(self):
        # Review R1 S4: the same role re-reported with another link or a requisition is not added again.
        final = {"opportunities": [role()], "company_findings": []}
        moved = role(url="https://jobs.ashbyhq.com/roleco/999", requisition_id="")
        self.assertIs(research_tools.merge_research(final, [moved]), final)
        other = role("Other Co", url="https://jobs.ashbyhq.com/otherco/1", requisition_id="1")
        self.assertEqual([entry["company"] for entry in research_tools.merge_research(final, [moved, other])["opportunities"]],
                         ["Role Co", "Other Co"])
        broken = dict(role(), rationale="")
        self.assertEqual(research_tools.merge_research({"opportunities": [broken]}, [role()])["opportunities"], [role()])

    def test_merge_without_saves_returns_the_final_output_itself(self):
        final = {"opportunities": [role()], "summary": "s"}
        self.assertIs(research_tools.merge_research(final, []), final)
        self.assertIs(research_tools.merge_research(final, [role()]), final)
        discovered = {"discovery_status": "COMPLETE", "summary": "s", "blockers": [], "companies": [company()]}
        self.assertIs(research_tools.merge_discovery(discovered, [company()], 20, industries=["AI"]), discovered)

    def test_attach_places_the_server_before_the_stdin_marker(self):
        command = research_tools.attach(["codex", "exec", "-"], ["a=1", "b=2"])
        self.assertEqual(command, ["codex", "exec", "-c", "a=1", "-c", "b=2", "-"])
        self.assertEqual(research_tools.attach(("schema", "output"), ["a=1"]), ("schema", "output"))


if __name__ == "__main__":
    unittest.main()
