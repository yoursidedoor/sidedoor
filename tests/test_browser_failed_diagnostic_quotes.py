import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import json
import unittest

import manual_browser_failed_diagnostic_quotes as manual


class FailedDiagnosticQuoteTests(unittest.TestCase):
    def test_manual_failed_quote_evidence_boundaries(self):
        self.assertEqual(manual.run()["passed"], 5)

    def test_blocker_alias_retains_exact_failed_diagnostic_source(self):
        request, raw, trace = manual.fixture()
        result = json.loads(raw["result_json"])
        result["blocker"] = result.pop("blocked")
        raw["result_json"] = json.dumps(result)
        for entry in raw["evidence"]:
            entry["pointer"] = entry["pointer"].replace("/blocked/", "/blocker/")
        normalized = manual.normalize(request, raw, trace)
        self.assertEqual(normalized["blocked"]["code"], "INSPECTION_INCOMPLETE")
        bound = next(entry for entry in normalized["browser_evidence"] if entry["pointer"] == "/blocker/code")
        self.assertEqual(bound["tool_call_id"], "failed")
        self.assertEqual(bound["source_kind"], "failed_tool_diagnostic")

    def test_employer_fact_cannot_cite_failure_even_when_blocker_is_valid(self):
        request, raw, trace = manual.fixture()
        for entry in raw["evidence"]:
            if entry["pointer"] == "/partial_review/employer":
                entry.update(tool_call_id="failed", quote=manual.FAILURE)
        # 2026-10-07 (item 2): the refused quote is recorded and no longer withdraws the
        # employer the inspect read reported.
        normalized = manual.normalize(request, raw, trace)
        self.assertEqual(normalized["partial_review"]["employer"], "Synthetic Company")
        self.assertIs(normalized["partial_review"]["verified"], False)
        self.assertEqual([(entry["pointer"], entry["reason"]) for entry in normalized["rejected_evidence"]],
                         [("/partial_review/employer", "APPLICATION_UNOBSERVED_QUOTE")])

    def test_near_match_diagnostic_is_not_treated_as_verbatim(self):
        # Whitespace runs are presentation (2026-09-19) and still bind; a changed character does not.
        request, raw, trace = manual.fixture()
        raw["evidence"][0]["quote"] = manual.FAILURE.replace(" ", "  ")
        self.assertNotIn("rejected_evidence", manual.normalize(request, raw, trace))
        raw["evidence"][0]["quote"] = manual.FAILURE.replace("could not load", "loaded")
        normalized = manual.normalize(request, raw, trace)
        self.assertEqual(normalized["rejected_evidence"][0]["reason"], "APPLICATION_UNOBSERVED_QUOTE")
        self.assertNotIn("/blocked/code", {entry["pointer"] for entry in normalized["browser_evidence"]})


if __name__ == "__main__":
    unittest.main()
