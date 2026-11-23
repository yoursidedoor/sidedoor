"""K28 (owner, 2026-11-23, picks a+b): a verify/reconcile receipt read is the AI's answer, taken as given.

Replays the shape of a real email-application Sent read (names and addresses made up) that was refused
because the snapshot showed `filename=\\"resume.pdf\\"` while the quote said `filename="resume.pdf"`.
Its quotes are now recorded, never compared. Kept (double submission): a NOT_SUBMITTED read still needs
every quote on the page, and a "not submitted" claim citing only the request context is refused.
"""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import json
from pathlib import Path
import unittest

import application_browser as browser
import applications


CANDIDATE = "candidate@example.com"
JOB_KEY = "examplecorp:unresolved:0123456789abcdef01234567"
ATTEMPT = "attempt-k28"
OBSERVED = "2026-11-23T05:34:00+00:00"
# What the snapshot showed of the Sent message (the browser escapes quote marks with a backslash).
PAGE = ("Google Account: Casey Example (" + CANDIDATE + "), Google membership\n"
        "From: Casey Example <" + CANDIDATE + ">\nTo: jobs@examplecorp.example\n"
        "Date: Sun, 22 Nov 2026 21:32:26 -0800\nMessage ID <MSG-1@mail.gmail.com>\n"
        "Content-Type: application/pdf; name=\\\"resume.pdf\\\" "
        "Content-Disposition: attachment; filename=\\\"resume.pdf\\\"\n"
        "I'm writing to apply for the Reliability Engineer role at examplecorp.\n"
        "button \"Back to Sent Mail\"\nheading \"Original Message\"")
SOURCE_URL = "https://mail.google.com/mail/u/0/#sent/QgrcExampleMessage"


def request():
    return {"request_id": "request-k28", "binding": "binding-k28", "max_tool_calls": 70,
            "payload": {"operation": "verify", "attempt_id": ATTEMPT,
                        "job": {"job_key": JOB_KEY, "candidate_email": CANDIDATE, "employer": "examplecorp"}},
            "context": {"current_attempt": {"attempt_id": ATTEMPT, "job_key": JOB_KEY,
                                            "started_at": "2026-11-23T05:30:24+00:00",
                                            "armed_at": "2026-11-23T05:32:22+00:00"}}}


def sent_read():
    """The real read's result and evidence, as it came back (made-up identity)."""
    result = {"status": "SUBMITTED", "verified": True, "independent_read": True,
              "evidence_kind": "sent_application_email", "job_key": JOB_KEY, "attempt_id": ATTEMPT,
              "candidate_email": CANDIDATE, "mailbox_email": CANDIDATE, "sender": CANDIDATE,
              "email_to": ["jobs@examplecorp.example"], "employer": "examplecorp",
              "employer_matches_expected": True, "requisition": None, "sent_at": "2026-11-22T21:32:26-08:00",
              "attachment_names": ["resume.pdf"], "receipt_id": "<MSG-1@mail.gmail.com>",
              "provider_message_or_application_id": "<MSG-1@mail.gmail.com>", "source_url": SOURCE_URL,
              "reason": "The Sent message confirms the application.", "next_action": None}
    evidence = [
        ("/candidate_email", "Google Account: Casey Example (" + CANDIDATE + "), Google membership", None),
        ("/mailbox_email", "Google Account: Casey Example (" + CANDIDATE + "), Google membership", None),
        ("/sender", "From: Casey Example <" + CANDIDATE + ">", None),
        ("/email_to/0", "To: jobs@examplecorp.example", None),
        ("/sent_at", "Date: Sun, 22 Nov 2026 21:32:26 -0800", None),
        ("/receipt_id", "Message ID <MSG-1@mail.gmail.com>", None),
        ("/attachment_names/0", 'Content-Disposition: attachment; filename="resume.pdf"', None),
        ("/employer", "I'm writing to apply for the Reliability Engineer role at examplecorp.", None),
        ("/status", "Date: Sun, 22 Nov 2026 21:32:26 -0800", None),
        ("/source_url", "[Application: Reliability Engineer - Gmail](" + SOURCE_URL + ")", None),
        ("/job_key", JOB_KEY, "context:/context/current_attempt/job_key"),
        ("/attempt_id", ATTEMPT, "context:/context/current_attempt/attempt_id"),
        ("/reason", "Date: Sun, 22 Nov 2026 21:32:26 -0800", None)]
    return result, [{"pointer": p, "quote": q, "tool_call_id": t} for p, q, t in evidence]


def normalize(result, evidence, page=PAGE):
    event = {"type": "item.completed", "item": {"type": "mcp_tool_call", "id": "browser-1", "server": "browser",
             "tool": "browser_snapshot", "arguments": {}, "status": "completed",
             "result": {"content": [{"type": "text", "text": page}], "isError": False}}}
    req = request()
    raw = {"request_id": req["request_id"], "binding": req["binding"], "result_json": json.dumps(result),
           "evidence": evidence}
    return browser.normalize(req, raw, (json.dumps(event) + "\n").encode(), OBSERVED,
                             Path("request-k28") / "trace.jsonl")


class ReceiptReadsTakeTheAnswer(unittest.TestCase):
    def test_the_sent_read_with_an_escaped_filename_settles_as_submitted(self):
        result, evidence = sent_read()
        read = normalize(result, evidence)
        self.assertEqual(("SUBMITTED", "sent_application_email"), (read["status"], read["evidence_kind"]))
        self.assertEqual(["resume.pdf"], read["attachment_names"])
        self.assertTrue(applications.verified_receipt(read, {"job_key": JOB_KEY}, OBSERVED))

    def test_a_quote_the_page_never_showed_is_recorded_not_a_refusal(self):
        result, evidence = sent_read()
        evidence[3] = dict(evidence[3], quote="To: someone-else@elsewhere.example")
        read = normalize(result, evidence)
        self.assertEqual("SUBMITTED", read["status"])
        self.assertEqual(["jobs@examplecorp.example"], read["email_to"])
        self.assertIn("/email_to/0", [entry["pointer"] for entry in read["rejected_evidence"]])


class AbsenceKeepsEveryCheck(unittest.TestCase):
    def absence(self):
        result = {"status": "NOT_SUBMITTED", "verified": True, "independent_read": True,
                  "authoritative_absence": True, "no_inflight": True, "job_key": JOB_KEY, "attempt_id": ATTEMPT,
                  "employer": "examplecorp", "requisition": "unresolved:0123456789abcdef01234567",
                  "candidate_email": CANDIDATE}
        evidence = [{"pointer": "/" + key, "quote": PAGE.splitlines()[0], "tool_call_id": None}
                    for key in ("status", "authoritative_absence", "no_inflight", "candidate_email")]
        return result, evidence

    def test_an_identity_quote_absent_from_the_page_still_refuses_an_absence(self):
        result, evidence = self.absence()
        evidence.append({"pointer": "/employer", "quote": "examplecorp has no record", "tool_call_id": None})
        with self.assertRaisesRegex(ValueError, "APPLICATION_UNOBSERVED_QUOTE"):
            normalize(result, evidence)

    def test_a_not_submitted_claim_quoting_only_the_context_is_refused(self):
        result, evidence = self.absence()
        evidence[0] = {"pointer": "/status", "quote": ATTEMPT, "tool_call_id": "context:/context/current_attempt/attempt_id"}
        with self.assertRaisesRegex(ValueError, "APPLICATION_SUBMISSION_STATE_NEEDS_LIVE_EVIDENCE"):
            normalize(result, evidence)


if __name__ == "__main__":
    unittest.main()
