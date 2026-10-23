"""Read labels and the re-read loop (owner, 2026-10-02).

Part A: an inspect read over 128 evidence entries keeps the first 128; an email application
with no valid address and a fact citing undated saved context no longer refuse or unverify it.
Part B (Cyberdyne): a budget-waiting job is read only when the read and its submit both fit, an
ELIGIBLE read is saved before the submit is booked and reused next pass, and a complete read
spends no inspect claim.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import application_browser
import application_connection
import applications
import desktop_adapter
import external_history
import workflow_tasks
from manual_application_browser import event
import test_part_c_read_gating as part_c

OBSERVED_AT = "2026-09-19T10:00:00+00:00"
PAGE = "Synthetic Company\tSenior Engineer\tRequisition role-123"


def normalize(result, evidence, operation="inspect", context=None):
    request = {"request_id": "synthetic", "binding": "synthetic-binding", "max_tool_calls": 40,
               "payload": {"operation": operation, "job": {"job_key": "synthetic company:role-123"}},
               "context": context or {}}
    raw = {"request_id": "synthetic", "binding": "synthetic-binding", "result_json": json.dumps(result),
           "evidence": evidence}
    trace = (json.dumps(event("read", PAGE)) + "\n").encode()
    return application_browser.normalize(request, raw, trace, OBSERVED_AT, Path("synthetic-trace.jsonl"))


class ReadLabels(unittest.TestCase):
    def test_over_128_entries_keeps_the_first_128_on_a_read_only(self):
        evidence = [{"pointer": "/employer", "tool_call_id": "read", "quote": "Synthetic Company"}] * 130
        result = normalize({"verified": True, "employer": "Synthetic Company"}, evidence)
        self.assertIs(result["verified"], True)
        self.assertEqual(result["evidence_dropped_over_limit"], 2)
        self.assertEqual(len(result["browser_evidence"]), 128)
        with self.assertRaisesRegex(ValueError, "APPLICATION_EVIDENCE_LIMIT"):
            normalize({"status": "UNKNOWN"}, evidence, "verify")

    def test_email_application_without_a_valid_address_is_not_refused(self):
        result = normalize({"verified": True, "employer": "Synthetic Company",
                            "application_method": applications.EMAIL_METHOD,
                            "email_application": {"address": "not an address"}},
                           [{"pointer": "/employer", "tool_call_id": "read", "quote": "Synthetic Company"}])
        self.assertIs(result["verified"], True)
        self.assertIsNone(applications.email_application(result))

    def test_undated_context_fact_is_recorded_not_a_label(self):
        result = normalize({"verified": True, "facts": {"requirements_met": True}},
                           [{"pointer": "/facts/requirements_met", "tool_call_id": "context:/context/notes/0",
                             "quote": "eight years of Python"}],
                           context={"notes": ["eight years of Python"]})
        self.assertIs(result["verified"], True)
        self.assertEqual(result["undated_eligibility_fields"], ["/facts/requirements_met"])


class BudgetWaitAdapter(part_c.Adapter):
    """The Cyberdyne night: the read succeeds, the submit's booking does not."""

    def __init__(self):
        super().__init__()
        self.budget_short = True

    def prepare_submission(self, job):
        if self.budget_short:
            raise application_connection.CapacityWait("APPLICATION_BROWSER_SHARED_BUDGET_EXHAUSTED",
                                                      part_c.at(20 * 3600))


class RereadLoop(unittest.TestCase):
    setUp = part_c.PartCTests.setUp
    job = part_c.PartCTests.job

    def cycle(self, adapter, seconds, config=None):
        adapter.now = part_c.at(seconds)
        applications.cycle(self.database, config or {}, self.root, adapter, adapter.now)

    def events(self, name):
        return self.database.execute("SELECT COUNT(*) FROM application_events WHERE job_key=? AND event=?",
                                     (self.key, name)).fetchone()[0]

    def test_eligible_read_is_saved_before_the_submit_booking_and_reused(self):
        with self.database:
            external_history.ingest_search(self.database, part_c.search("NO_MATCH"), part_c.EMAIL)
        adapter = BudgetWaitAdapter()
        self.cycle(adapter, 60)
        self.assertEqual(self.job()["blocker"], "APPLICATION_BROWSER_SHARED_BUDGET_EXHAUSTED")
        self.assertEqual(self.events(applications.COMPLETE_READ_EVENT), 1)
        self.assertIsNotNone(applications.reusable_read(self.database, self.job(), part_c.at(120)))
        adapter.budget_short = False
        self.cycle(adapter, 21 * 3600)
        self.assertEqual([op for op, _ in adapter.calls][:2], ["inspect", "submit"])
        self.assertEqual(self.events("APPLICATION_READ_REUSED"), 1)

    def test_budget_wait_reads_once_then_reuses_the_read(self):
        # Owner, 2026-10-02: no read-plus-submit room rule; a budget-waiting job is picked up as
        # before, reads once, and every later pass reuses that read instead of reading again.
        workflow_tasks.initialize(self.database)
        with self.database:
            external_history.ingest_search(self.database, part_c.search("NO_MATCH"), part_c.EMAIL)
        adapter = BudgetWaitAdapter()
        for offset in (60, 3660, 7260):
            self.cycle(adapter, offset, {"daily_compute_seconds": 1000})
        self.assertEqual([op for op, _ in adapter.calls].count("inspect"), 1)
        self.assertEqual(self.job()["blocker"], "APPLICATION_BROWSER_SHARED_BUDGET_EXHAUSTED")
        self.assertGreaterEqual(self.events("APPLICATION_READ_REUSED"), 1)

    def test_a_complete_read_spends_no_inspect_claim(self):
        later = part_c.at(4 * 3600)
        adapter = application_connection.HostedDesktopAdapter(self.database, self.root, {"application_browser": {"enabled": True}},
                                                              clock=lambda: later)
        body = adapter.request_body("inspect", {"job": self.job()})
        with self.database:
            for index in range(3):
                self.database.execute("""INSERT INTO desktop_requests(request_id,binding,job_key,operation,payload,state,
                    created_at,expires_at,claims,result) VALUES (?,?,?,?,?,'CONSUMED',?,?,1,?)""",
                    ("r" + str(index), "b" + str(index), self.key, "inspect", desktop_adapter.encode(body), later,
                     part_c.at(4 * 3600 + 900), json.dumps({"verified": True, "source": "application-browser:" + str(index)})))
        allowed = {"reviewed": True}
        with patch.object(application_connection.HostedDesktopAdapter, "reserve", lambda *a, **k: None), \
                patch.object(desktop_adapter.DesktopAdapter, "call", lambda *a, **k: allowed):
            exhausted = adapter.call("inspect", {"job": self.job(), "read_only": True})
            self.assertEqual(exhausted["blocked"]["code"], "APPLICATION_BROWSER_INVESTIGATION_EXHAUSTED")
            with self.database:
                applications.event(self.database, self.key, applications.COMPLETE_READ_EVENT,
                                   {"source": "application-browser:0"}, later)
            self.assertIs(adapter.call("inspect", {"job": self.job(), "read_only": True}), allowed)


if __name__ == "__main__":
    unittest.main()
