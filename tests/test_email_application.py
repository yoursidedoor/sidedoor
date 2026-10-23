import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import json
from pathlib import Path
import unittest
from unittest.mock import Mock

import application_browser as browser
import applications
import test_applications_e2e as e2e
from test_question_label_contract import event


def first_e2e_test():
    return next(name for name in dir(e2e.ApplicationEndToEndTests) if name.startswith("test_"))


class AnEmailPostingIsRecognisedNotAbandoned(unittest.TestCase):
    """Owner, 2026-09-24: "system should be able to apply by email".

    A posting with no form and a mailto: apply link (Genco) was abandoned for having nothing to
    fill. It is now read as an EMAIL application: the answering AI writes the message (and a
    subject when the posting names none), and then it either waits for the owner's switch or,
    when that is on, goes through exactly the claim a form does - never to the form filler.
    """

    TARGET = {"address": "careers@mockcompany.example", "subject": "Application"}

    def run_with(self, email_application, enabled=False, answered=True):
        case = e2e.ApplicationEndToEndTests(first_e2e_test())
        case.setUp()
        self.addCleanup(case.doCleanups)
        original = case.adapter.call

        def call(operation, payload):
            result = original(operation, payload)
            if operation == "inspect" and email_application is not None:
                result.update(application_method="EMAIL", email_application=email_application, questions=[])
            return result

        case.adapter.call = call
        case.discover()
        key = applications.identity(e2e.role())[2]
        if answered and email_application is not None and applications.email_application(
                {"application_method": "EMAIL", "email_application": email_application}):
            target = applications.email_application({"application_method": "EMAIL", "email_application": email_application})
            with case.database:
                for question in applications.email_questions(target):
                    case.database.execute("INSERT INTO application_answers VALUES (?,?,?,?,?)",
                                          (key, applications.question_field(question), question["semantic"],
                                           json.dumps("Synthetic drafted text for " + question["semantic"]),
                                           "ai-answer:synthetic"))
        case.adapter.now = e2e.NOW
        applications.cycle(case.database, {"email_applications_enabled": enabled}, case.root, case.adapter, e2e.NOW)
        submits = [payload for operation, payload in case.adapter.calls if operation == "submit"]
        attempts = case.database.execute("SELECT COUNT(*) FROM application_attempts").fetchone()[0]
        return case.job(), submits, attempts

    def test_with_the_switch_off_a_drafted_email_application_waits_and_is_never_submitted(self):
        job, submits, attempts = self.run_with(self.TARGET, enabled=False)
        self.assertEqual(("BLOCKED", "EMAIL_APPLICATION_READY"), (job["state"], job["blocker"]))
        self.assertIn("careers@mockcompany.example", job["next_action"])
        self.assertEqual(([], 0), (submits, attempts))

    def test_with_the_switch_on_it_takes_the_normal_claim_carrying_its_verified_recipient(self):
        job, submits, attempts = self.run_with(self.TARGET, enabled=True)
        self.assertEqual((1, 1), (len(submits), attempts))
        self.assertEqual(self.TARGET, submits[0]["email_application"])
        self.assertEqual({applications.EMAIL_BODY_SEMANTIC}, {question["semantic"] for question in submits[0]["questions"]})

    def test_the_message_is_drafted_by_the_answering_step_before_anything_else(self):
        job, submits, attempts = self.run_with(self.TARGET, enabled=True, answered=False)
        self.assertTrue(job["blocker"].startswith("ANSWERING_RETRY"), job["blocker"])
        self.assertEqual(([], 0), (submits, attempts))

    def test_an_email_posting_without_a_valid_address_never_falls_through_to_the_form_path(self):
        job, submits, attempts = self.run_with({"address": None, "subject": None}, enabled=True)
        self.assertEqual(("BLOCKED", "EMAIL_APPLICATION_ADDRESS_UNVERIFIED"), (job["state"], job["blocker"]))
        self.assertEqual(([], 0), (submits, attempts))

    def test_a_form_posting_is_unaffected(self):
        job, submits, attempts = self.run_with(None)
        self.assertEqual("SUBMITTED", job["state"])
        self.assertEqual(1, len(submits))
        self.assertNotIn("email_application", submits[0])


class TheRecipientQuoteFromThePage(unittest.TestCase):
    """A recipient quote that does not match the page drops only that value."""

    PAGE = "Infrastructure Engineer. To apply, email careers@genco.example with the subject Genco Careers"

    def normalized(self, address, quote):
        review = {"verified": False, "official_destination_verified": False,
                  "all_required_questions_enumerated": False, "employer": "Genco", "application_method": "EMAIL",
                  "email_application": {"address": address, "subject": "Genco Careers"}}
        request = {"request_id": "email", "binding": "email-binding", "max_tool_calls": 40,
                   "payload": {"operation": "inspect", "job": {"job_key": "genco:role"}}, "context": {}}
        pointer = "/partial_review/email_application/address"
        evidence = [{"pointer": leaf, "tool_call_id": "read", "quote": self.PAGE}
                    for leaf, unused in browser.leaves({"partial_review": review}) if leaf != pointer]
        if quote is not None:
            evidence.append({"pointer": pointer, "tool_call_id": "read", "quote": quote})
        raw = {"request_id": "email", "binding": "email-binding", "evidence": evidence,
               "result_json": json.dumps({"partial_review": review})}
        trace = (json.dumps(event("read", self.PAGE)) + "\n").encode()
        return browser.normalize(request, raw, trace, "2026-09-18T06:29:25+00:00", Path("synthetic.jsonl"))

    def test_an_address_the_page_shows_is_kept(self):
        result = self.normalized("careers@genco.example", self.PAGE)
        self.assertEqual("careers@genco.example", result["partial_review"]["email_application"]["address"])

    def test_an_address_the_page_never_showed_is_dropped(self):
        result = self.normalized("attacker@elsewhere.example", "email attacker@elsewhere.example")
        self.assertIsNone(result["partial_review"]["email_application"]["address"])

import desktop_adapter
import test_desktop_adapter_safety as safety


class TheArmBeforeSend(unittest.TestCase):
    """The last check before Send, on the real guard: one-shot arm and the owner's email switch."""

    ADDRESS = "careers@synthetic.example"

    def setUp(self):
        self.fixture = safety.DesktopAdapterSafetyTests("test_pause_after_claim_prevents_arm")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        fixture, address = self.fixture, self.ADDRESS
        base = fixture.review

        def email_review():
            review = base()
            review.update(application_method="EMAIL", email_application={"address": address, "subject": "Application"},
                          questions=[])
            return review

        fixture.review = email_review
        fixture.config["email_applications_enabled"] = True
        fixture.write_config()
        target = {"address": address, "subject": "Application"}
        with fixture.database:
            for question in applications.email_questions(target):
                fixture.database.execute("INSERT INTO application_answers VALUES (?,?,?,?,?)",
                                         (fixture.key, applications.question_field(question), question["semantic"],
                                          json.dumps("Synthetic drafted message"), "ai-answer:synthetic"))
        self.request_id, self.payload = self.prepare_submit()
        self.claimed = fixture.claim()

    def prepare_submit(self):
        """The safety fixture's own prepare_submit, but handing `process` the config it runs with."""
        fixture = self.fixture

        def inspect_or_queue(operation, payload):
            if operation == "inspect":
                return fixture.review()
            return fixture.adapter.call(operation, payload)

        provider = Mock(can_submit=True)
        provider.call.side_effect = inspect_or_queue
        applications.process(fixture.database, fixture.job(), provider, fixture.policy, fixture.root, fixture.now,
                             config=fixture.config)
        request = fixture.database.execute("SELECT * FROM desktop_requests WHERE operation='submit'").fetchone()
        self.assertIsNotNone(request, fixture.job()["blocker"])
        return request["request_id"], json.loads(request["payload"])

    def arm(self, to, sender=None):
        envelope = dict(self.claimed, observed_questions=self.payload["questions"],
                        observed_candidate_email=sender or self.fixture.job()["candidate_email"], observed_email_to=to)
        return desktop_adapter.arm(self.fixture.database, self.fixture.root, envelope, self.fixture.now)

    def test_the_verified_recipient_from_the_owners_mailbox_is_allowed_once(self):
        self.assertTrue(self.arm([self.ADDRESS.upper()])["may_submit_once"])

    def test_turning_the_switch_off_after_planning_stops_the_send(self):
        self.fixture.config["email_applications_enabled"] = False
        self.fixture.write_config()
        with self.assertRaises(ValueError) as refusal:
            self.arm([self.ADDRESS])
        self.assertEqual("EMAIL_APPLICATIONS_NOT_ENABLED", str(refusal.exception))


import manual_application_receipts as receipts


class TheSentFolderConfirmsAnEmailApplication(unittest.TestCase):
    """Step 3: the candidate's own sent message is the receipt, bound to this attempt.

    After Send the outcome is UNKNOWN by design. A separate read of the Sent folder decides it,
    and Python checks the sent message against this attempt's own saved review: exactly the
    verified recipient, sent no earlier than the arm.
    """

    ADDRESS = "careers@synthetic.example"

    def build(self, email=True):
        cls = next(value for value in vars(receipts).values()
                   if isinstance(value, type) and hasattr(value, "prepare") and hasattr(value, "receipt"))
        fixture = cls()
        self.addCleanup(fixture.close)
        target = {"address": self.ADDRESS, "subject": "Application"}
        fixture.config["email_applications_enabled"] = True
        (fixture.root / "config.json").write_text(json.dumps(fixture.config))
        if email:
            fixture.review = dict(fixture.review, application_method="EMAIL", email_application=target, questions=[])
            with fixture.database:
                for question in applications.email_questions(target):
                    fixture.database.execute("INSERT INTO application_answers VALUES (?,?,?,?,?)",
                                             (fixture.key, applications.question_field(question), question["semantic"],
                                              json.dumps("Synthetic drafted message"), "ai-answer:synthetic"))
        request = fixture.claim(fixture.queue("inspect", {"job": fixture.job()}))
        desktop_adapter.complete(fixture.database, dict(request, result=fixture.review), fixture.now)
        applications.process(fixture.database, fixture.job(), fixture.adapter, fixture.policy, fixture.root,
                             fixture.now, config=fixture.config)
        submit = fixture.database.execute("SELECT request_id, payload FROM desktop_requests WHERE operation='submit'").fetchone()
        fixture.submit = fixture.claim(submit[0])
        payload = json.loads(submit[1])
        envelope = (dict(fixture.submit, observed_questions=payload["questions"],
                         observed_candidate_email=fixture.job()["candidate_email"], observed_email_to=[self.ADDRESS])
                    if email else dict(fixture.submit, observed_form_revision="synthetic-v1"))
        desktop_adapter.arm(fixture.database, fixture.root, envelope, fixture.now)
        desktop_adapter.complete(fixture.database, dict(fixture.submit, result={"acknowledged": True}), fixture.now)
        return fixture

    def sent(self, fixture, **change):
        candidate = fixture.job()["candidate_email"]
        result = {"status": "SUBMITTED", "source": "synthetic Gmail Sent read", "verified": True,
                  "observed_at": fixture.now, "independent_read": True, "employer": "synthetic", "requisition": None,
                  "candidate_email": candidate, "job_key": fixture.key,
                  "attempt_id": fixture.submit["payload"]["attempt_id"], "evidence_kind": "sent_application_email",
                  "receipt_id": "message-1", "email_to": [self.ADDRESS], "sent_at": fixture.now,
                  "attachment_names": ["resume.pdf"], "mailbox_email": candidate, "sender": candidate,
                  "source_url": "https://mail.google.com/mail/u/0/#sent/message-1"}
        result.update(change)
        return result

    def confirm(self, fixture, result):
        read = fixture.claim(fixture.queue("verify", {"job": fixture.job()}))
        desktop_adapter.complete(fixture.database, dict(read, result=result), fixture.now)

    def test_the_sent_message_to_the_verified_recipient_marks_the_application_submitted(self):
        fixture = self.build()
        self.confirm(fixture, self.sent(fixture))
        applications.reconcile(fixture.database, fixture.job(), fixture.adapter, fixture.policy, fixture.now,
                               config=fixture.config)
        self.assertEqual("SUBMITTED", fixture.job()["state"])
        self.assertEqual("VERIFIED", fixture.database.execute("SELECT state FROM application_attempts").fetchone()[0])
        self.assertEqual([("APPLICATION",)], [tuple(row) for row in fixture.database.execute(
            "SELECT kind FROM application_history WHERE employer='synthetic'")])

    def test_a_sent_message_that_does_not_prove_this_attempt_is_refused(self):
        for label, change in (("different recipient", {"email_to": ["attacker@elsewhere.example"]}),
                              ("extra recipient", {"email_to": [self.ADDRESS, "someone@else.example"]}),
                              ("sent before the arm", {"sent_at": desktop_adapter.later(receipts.NOW, -60)})):
            with self.subTest(label):
                fixture = self.build()
                with self.assertRaises(ValueError):
                    self.confirm(fixture, self.sent(fixture, **change))

    def test_a_sent_message_cannot_prove_a_form_application(self):
        fixture = self.build(email=False)
        with self.assertRaises(ValueError):
            self.confirm(fixture, self.sent(fixture))


if __name__ == "__main__":
    unittest.main()
