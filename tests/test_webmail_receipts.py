"""Receipt reads in the user's own webmail, any provider (owner invariant 2026-10-16, BACKLOG E0.T2.S1;
owner 2026-10-24 "1: Critical. Please fix").

Every mailbox read (verify/reconcile email receipts, the email-application Sent read, the
investigation's saved-message comparison) binds its message URL to the hosts of the webmail the
user chose in Setup (external_history.WEBMAIL_HOSTS), exactly as strictly as it bound Gmail's.
No choice saved, or a request made before context.webmail existed: Gmail, as before.
"""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import copy
import json
from pathlib import Path
import tempfile
import unittest
import urllib.parse

import application_browser as browser
import external_history
import setup_steps


CANDIDATE = "candidate@example.invalid"
STARTED = "2026-10-20T10:00:00+00:00"
RECEIVED = "2026-10-20T10:05:00+00:00"
OBSERVED = "2026-10-20T10:30:00+00:00"
OTHER = {"Gmail": "https://mail.yahoo.com/d/folders/1/messages/AbC123",
         "Outlook.com": "https://mail.google.com/mail/?authuser=x#all/AbC123",
         "Outlook (work or school)": "https://mail.yahoo.com/d/folders/1/messages/AbC123"}
MESSAGE_URLS = {"Gmail": "https://mail.google.com/mail/u/0/#all/AbC123",
                "Outlook.com": "https://outlook.live.com/mail/0/inbox/id/AbC123",
                "Outlook (work or school)": "https://outlook.office.com/mail/inbox/id/AbC123",
                "Yahoo Mail": "https://mail.yahoo.com/d/folders/1/messages/AbC123",
                "iCloud Mail": "https://www.icloud.com/mail/#/inbox/AbC123",
                "Proton Mail": "https://mail.proton.me/u/0/inbox/AbC123",
                "AOL Mail": "https://mail.aol.com/d/folders/1/messages/AbC123",
                "Zoho Mail": "https://mail.zoho.com/zm/#mail/folder/inbox/p/AbC123",
                "Fastmail": "https://app.fastmail.com/mail/Inbox/AbC123"}


def webmail(label):
    choice = setup_steps.option("webmail", label)
    return external_history.webmail_context(choice, CANDIDATE)


def request(context_webmail="absent", operation="verify"):
    context = {"current_attempt": {"attempt_id": "attempt-1", "started_at": STARTED}}
    if context_webmail != "absent":
        context["webmail"] = context_webmail
    return {"request_id": "request-1", "binding": "binding-1", "max_tool_calls": 70,
            "payload": {"operation": operation, "attempt_id": "attempt-1",
                        "job": {"job_key": "acme:role-1", "candidate_email": CANDIDATE}},
            "context": context}


def email_receipt(source_url):
    """Shaped like the harness's recorded employer confirmation email receipts (an ATS acknowledgment
    naming only the employer, no requisition; manual_application_browser's email-receipt case)."""
    return {"status": "SUBMITTED", "source": "independent employer confirmation email", "verified": True,
            "independent_read": True, "employer": "Acme", "employer_matches_expected": True,
            "requisition": None, "candidate_email": CANDIDATE, "receipt_id": "AbC123",
            "evidence_kind": "employer_confirmation_email", "job_key": "acme:role-1", "attempt_id": "attempt-1",
            "mailbox_email": CANDIDATE, "recipient": CANDIDATE, "sender": "no-reply@ashbyhq.com",
            "sender_authenticated": True, "received_at": RECEIVED, "source_url": source_url,
            "provider_message_or_application_id": "AbC123"}


def sent_receipt(source_url):
    return {"status": "SUBMITTED", "source": "Sent folder read", "verified": True, "independent_read": True,
            "employer": "Acme", "requisition": None, "candidate_email": CANDIDATE, "receipt_id": "AbC123",
            "evidence_kind": "sent_application_email", "job_key": "acme:role-1", "attempt_id": "attempt-1",
            "mailbox_email": CANDIDATE, "sender": CANDIDATE, "email_to": ["jobs@acme.example"],
            "sent_at": RECEIVED, "attachment_names": ["resume.pdf"], "source_url": source_url}


def normalize(req, result):
    quote = "SYNTHETIC BROWSER OBSERVATION ONLY\n" + browser.encoded(result)
    event = {"type": "item.completed", "item": {"type": "mcp_tool_call", "id": "browser-1", "server": "browser",
             "tool": "browser_snapshot", "arguments": {}, "status": "completed",
             "result": {"content": [{"type": "text", "text": quote}], "isError": False}}}
    raw = {"request_id": req["request_id"], "binding": req["binding"], "result_json": browser.encoded(result),
           "evidence": [{"pointer": pointer, "tool_call_id": "browser-1", "quote": quote}
                        for pointer, _value in browser.leaves(result)]}
    # Only recorded as browser_trace, never opened: one fixed name, so two decisions compare whole.
    return browser.normalize(req, raw, (browser.encoded(event) + "\n").encode(), OBSERVED,
                             Path("request-1") / "trace.jsonl")


def refused(req, result):
    try:
        normalize(req, result)
    except ValueError as error:
        return str(error)
    return None


class OneHostListForEveryProvider(unittest.TestCase):
    def test_every_webmail_setup_offers_has_its_hosts_and_its_own_url_is_one_of_them(self):
        for label in setup_steps.options("webmail"):
            with self.subTest(label):
                self.assertIn(label, external_history.WEBMAIL_HOSTS)
                url = setup_steps.option("webmail", label)["url"]
                self.assertIn(urllib.parse.urlsplit(url).hostname, external_history.WEBMAIL_HOSTS[label])
                self.assertIn(urllib.parse.urlsplit(MESSAGE_URLS[label]).hostname,
                              external_history.WEBMAIL_HOSTS[label])

    def test_other_webmail_binds_only_its_own_https_host(self):
        self.assertEqual(("mail.example.org",),
                         external_history.webmail_hosts({"label": "Other", "url": "https://mail.example.org/inbox"}))
        self.assertEqual((), external_history.webmail_hosts({"label": "Other", "url": "http://mail.example.org/"}))
        self.assertEqual((), external_history.webmail_hosts({"label": "Other", "url": "https://u:p@mail.example.org/"}))


class TheChosenWebmailReachesTheSession(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())

    def test_no_choice_is_gmail_exactly_as_before(self):
        self.assertEqual(external_history.DEFAULT_WEBMAIL, external_history.chosen_webmail(self.home))
        entry = external_history.webmail_context(external_history.chosen_webmail(self.home), CANDIDATE)
        # The very URLs context.gmail carried before.
        self.assertEqual(external_history.gmail_url(CANDIDATE, "inbox"), entry["mailbox_url"])
        self.assertEqual(external_history.gmail_url(CANDIDATE, "search/"), entry["search_url_prefix"])
        self.assertEqual(["mail.google.com"], entry["hosts"])

    def test_a_saved_choice_is_read_and_other_providers_open_at_their_own_address(self):
        setup_steps.update_state(lambda state: state.update(webmail={"label": "Yahoo Mail",
                                                                    "url": "https://mail.yahoo.com/"}), self.home)
        choice = external_history.chosen_webmail(self.home)
        self.assertEqual("Yahoo Mail", choice["label"])
        entry = external_history.webmail_context(choice, CANDIDATE)
        self.assertEqual({"provider": "Yahoo Mail", "hosts": ["mail.yahoo.com"], "mailbox_email": CANDIDATE,
                          "mailbox_url": "https://mail.yahoo.com/", "search_url_prefix": None}, entry)

    def test_no_candidate_email_keeps_the_host_list(self):
        entry = external_history.webmail_context({"label": "Outlook.com", "url": "https://outlook.live.com/mail/"}, None)
        self.assertIsNone(entry["mailbox_url"])
        self.assertIn("outlook.live.com", entry["hosts"])

    def test_the_prompts_name_the_webmail_and_keep_the_gmail_rule(self):
        text = browser.PROMPT + "".join(browser.OPERATION_PROMPTS.values()) + browser.SUBMIT_SUPPLEMENT
        self.assertNotIn("context.gmail", text)
        for phrase in ("context.webmail.hosts", "context.webmail.mailbox_url", "authuser=<", "ALREADY signed in",
                       "Never enter a password", "AUTHENTICATION_REQUIRED"):
            self.assertIn(phrase, text)

    def test_sending_is_the_email_applications_single_write_and_never_a_reads(self):
        shared = " ".join(browser.PROMPT.split())
        self.assertIn("Sending is never allowed in any read operation (inspect, investigate, verify, reconcile)", shared)
        self.assertIn("The single allowed write in a mailbox, for every provider including Gmail, is in the submit "
                      "operation of an EMAIL APPLICATION", shared)
        self.assertIn("Then press Send exactly once", browser.OPERATION_PROMPTS["submit"])

    def test_zoho_region_hosts(self):
        for host in ("mail.zoho.com", "mail.zoho.eu", "mail.zoho.in", "mail.zoho.com.au"):
            self.assertIn(host, external_history.webmail_hosts({"label": "Zoho Mail", "url": "https://mail.zoho.com/"}))

    def test_a_saved_record_binds_to_its_own_https_host(self):
        self.assertEqual({"mail.google.com"}, external_history.record_hosts("https://mail.google.com/mail/u/0/#all/x"))
        for bad in ("http://mail.google.com/#all/x", "https://u@mail.google.com/#all/x", None, "not a url"):
            self.assertEqual(frozenset(), external_history.record_hosts(bad))


class TheReceiptBindsToTheChosenProvider(unittest.TestCase):
    def test_each_provider_accepts_its_own_host_and_refuses_another_or_a_lookalike(self):
        for label, url in MESSAGE_URLS.items():
            req = request(webmail(label))
            with self.subTest(label):
                self.assertEqual("SUBMITTED", normalize(req, email_receipt(url))["status"])
                self.assertEqual("SUBMITTED", normalize(req, sent_receipt(url))["status"])
                host = urllib.parse.urlsplit(url).hostname
                for bad in (OTHER.get(label, MESSAGE_URLS["Gmail"]),
                            url.replace(host, host + ".evil.example", 1),
                            url.replace(host, "evil-" + host, 1),
                            url.replace("https://", "http://", 1),
                            url.replace("https://", "https://user@", 1)):
                    self.assertEqual("APPLICATION_EMAIL_RECEIPT_UNBOUND", refused(req, email_receipt(bad)), bad)
                    self.assertEqual("APPLICATION_SENT_RECEIPT_UNBOUND", refused(req, sent_receipt(bad)), bad)

    def test_the_named_lookalikes_are_refused(self):
        self.assertEqual("APPLICATION_EMAIL_RECEIPT_UNBOUND", refused(
            request(webmail("Gmail")), email_receipt("https://mail.google.com.evil.example/mail/#all/AbC123")))
        self.assertEqual("APPLICATION_EMAIL_RECEIPT_UNBOUND", refused(
            request(webmail("Yahoo Mail")), email_receipt("https://yahoo.com.example/d/messages/AbC123")))

    def test_the_candidate_binding_is_unchanged_for_every_provider(self):
        for label, url in MESSAGE_URLS.items():
            req = request(webmail(label))
            with self.subTest(label):
                self.assertEqual("APPLICATION_EMAIL_RECEIPT_UNBOUND", refused(
                    req, dict(email_receipt(url), mailbox_email="someone@else.example")))
                self.assertEqual("APPLICATION_SENT_RECEIPT_UNBOUND", refused(
                    req, dict(sent_receipt(url), sender="someone@else.example")))

    def test_a_webmail_with_no_host_binds_nothing(self):
        req = request(external_history.webmail_context({"label": "Other", "url": "http://mail.example.org/"}, CANDIDATE))
        self.assertEqual("APPLICATION_EMAIL_RECEIPT_UNBOUND",
                         refused(req, email_receipt("https://mail.example.org/AbC123")))
        self.assertEqual("APPLICATION_EMAIL_RECEIPT_UNBOUND",
                         refused(req, email_receipt(MESSAGE_URLS["Gmail"])))


class GmailReplay(unittest.TestCase):
    """A Gmail receipt decides exactly as before, whether the request predates context.webmail or
    carries Gmail as the user's choice."""

    def test_a_gmail_receipt_validates_identically_with_and_without_the_webmail_context(self):
        for result in (email_receipt(MESSAGE_URLS["Gmail"]), sent_receipt(MESSAGE_URLS["Gmail"]),
                       email_receipt(external_history.gmail_url(CANDIDATE, "all/AbC123"))):
            old = normalize(request(), copy.deepcopy(result))
            new = normalize(request(webmail("Gmail")), copy.deepcopy(result))
            self.assertEqual(json.dumps(old, sort_keys=True), json.dumps(new, sort_keys=True))
            self.assertEqual("SUBMITTED", new["status"])
        for bad in (OTHER["Gmail"], "https://mail.google.com.evil.example/mail/#all/AbC123"):
            self.assertEqual(refused(request(), email_receipt(bad)), refused(request(webmail("Gmail")), email_receipt(bad)))
            self.assertEqual("APPLICATION_EMAIL_RECEIPT_UNBOUND", refused(request(), email_receipt(bad)))


class MessageLocator(unittest.TestCase):
    def test_gmail_keeps_its_fragment_and_other_webmail_names_the_message_anywhere_in_its_url(self):
        gmail = urllib.parse.urlsplit("https://mail.google.com/mail/AbC123/#inbox")
        self.assertNotIn("AbC123", browser.message_locator(gmail))
        outlook = urllib.parse.urlsplit(MESSAGE_URLS["Outlook.com"])
        self.assertIn("AbC123", browser.message_locator(outlook))


if __name__ == "__main__":
    unittest.main()


# --- W1 (owner, 2026-10-24): typed search and filters in mailbox reads; absence must be exhaustive ---
GMAIL_INBOX = "https://mail.google.com/mail/u/0/#inbox"


def absence(**extra):
    result = {"status": "NOT_SUBMITTED", "source": "webmail read", "verified": True, "independent_read": True,
              "employer": "Acme", "requisition": "role-1", "candidate_email": CANDIDATE, "job_key": "acme:role-1",
              "attempt_id": "attempt-1", "authoritative_absence": True, "no_inflight": True}
    result.update(extra)
    return result


def normalize_on(req, result, page_url, extra_text="", oldest_quote=None):
    """`normalize` with every quote from one tool call whose answer showed `page_url` (the oldest
    message's date quoted as `oldest_quote` alone when given, as an opened message shows it)."""
    quote = "SYNTHETIC BROWSER OBSERVATION ONLY\n" + extra_text + "\n" + browser.encoded(result)
    text = "### Page\n- Page URL: " + page_url + "\n- Page Title: Mail\n### Snapshot\n" + quote
    event = {"type": "item.completed", "item": {"type": "mcp_tool_call", "id": "browser-1", "server": "browser",
             "tool": "browser_snapshot", "arguments": {}, "status": "completed",
             "result": {"content": [{"type": "text", "text": text}], "isError": False}}}
    raw = {"request_id": req["request_id"], "binding": req["binding"], "result_json": browser.encoded(result),
           "evidence": [{"pointer": pointer, "tool_call_id": "browser-1",
                         "quote": oldest_quote if oldest_quote and pointer == "/mailbox_oldest_reached/received_at" else quote}
                        for pointer, _value in browser.leaves(result)]}
    return browser.normalize(req, raw, (browser.encoded(event) + "\n").encode(), OBSERVED,
                             Path("request-1") / "trace.jsonl")


class AWebmailAbsenceMustBeExhaustive(unittest.TestCase):
    OUTLOOK = "https://outlook.live.com/mail/0/inbox"

    def setUp(self):
        self.outlook = request(webmail("Outlook.com"))

    def assertDowngraded(self, result):
        self.assertEqual(("UNKNOWN", False, browser.MAILBOX_ABSENCE_DOWNGRADE),
                         (result["status"], result["authoritative_absence"], result.get("absence_downgraded")))

    def assertCertain(self, result):
        self.assertEqual(("NOT_SUBMITTED", True), (result["status"], result["authoritative_absence"]))
        self.assertNotIn("absence_downgraded", result)

    def test_a_first_inbox_page_is_not_absence(self):
        self.assertDowngraded(normalize_on(self.outlook, absence(), self.OUTLOOK))
        self.assertDowngraded(normalize_on(request(webmail("Gmail")), absence(), GMAIL_INBOX))
        self.assertDowngraded(normalize_on(request(), absence(), GMAIL_INBOX))   # a request before context.webmail

    def test_a_quoted_search_covering_the_employer_is_absence(self):
        search = {"query": "Acme", "results": "No messages matched your search", "covers_employer": True}
        self.assertCertain(normalize_on(self.outlook, absence(mailbox_search=search), self.OUTLOOK))
        self.assertDowngraded(normalize_on(self.outlook, absence(mailbox_search=dict(search, covers_employer=False)),
                                           self.OUTLOOK))
        self.assertDowngraded(normalize_on(self.outlook, absence(mailbox_search=dict(search, results=None)), self.OUTLOOK))

    def test_browsing_back_past_the_attempt_is_absence(self):
        older = {"received_at": "2026-10-19T09:00:00+00:00"}
        self.assertCertain(normalize_on(self.outlook, absence(mailbox_oldest_reached=older), self.OUTLOOK,
                                        "Sun, Oct 19, 2026 9:00 AM", "Sun, Oct 19, 2026 9:00 AM"))
        newer = {"received_at": "2026-10-20T10:01:00+00:00"}
        self.assertDowngraded(normalize_on(self.outlook, absence(mailbox_oldest_reached=newer), self.OUTLOOK,
                                           "Mon, Oct 20, 2026 10:01 AM", "Mon, Oct 20, 2026 10:01 AM"))
        # The date the read reports must be the day the page shows.
        self.assertDowngraded(normalize_on(self.outlook, absence(mailbox_oldest_reached=older), self.OUTLOOK,
                                           "Sat, Oct 4, 2025", "Sat, Oct 4, 2025"))

    def test_gmails_search_url_route_is_absence_as_before(self):
        url = external_history.gmail_url(CANDIDATE, "search/%22Acme%22")
        self.assertCertain(normalize_on(request(webmail("Gmail")), absence(), url))
        self.assertCertain(normalize_on(request(), absence(), url))

    def test_an_absence_read_off_another_page_is_unchanged(self):
        self.assertCertain(normalize_on(self.outlook, absence(), "https://jobs.ashbyhq.com/acme/application/status"))
