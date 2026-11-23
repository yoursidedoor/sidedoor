"""K1 (owner's pick K1 a): an email application whose guarded submit session found the candidate's mailbox
not signed in to Sidedoor's browser waits on the user's sign-in, with a card naming the address, instead
of "Waiting to retry" forever (Freestyle, 2026-11-23)."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import json
import unittest
from unittest import mock

import access_cards
import access_outcomes
import applications
import desktop_adapter
import confirm_read
import test_access_outcomes_e15_t1 as walls
import test_confirm_read_card as confirm_card
import user_step

MAILBOX = "candidate@example.com"
MAILBOX_STOP = {"acknowledged": False, "prearm_blocked": True,
                "blocked": {"code": "AUTHENTICATION_REQUIRED",
                            "next_action": "Sign in to %s in this browser yourself, then retry." % MAILBOX,
                            "wall": {"second_step": "UNKNOWN", "signed_in_as": "someone.else@example.com"}}}


class MailboxSignIn(unittest.TestCase):
    # The wall tests' fixture and helpers, without re-running their own tests here.
    locals().update({name: value for name, value in vars(walls.AccessOutcomeTests).items()
                     if not name.startswith(("test_", "__"))})

    def email_submit(self, setting=access_outcomes.SIGN_IN_FOR_ME):
        """The account setting (the owner's: "Sign in and create accounts for me") and a submit request whose
        attempt's saved review applies by email from MAILBOX."""
        with self.database:
            access_outcomes.set_account_setting(self.database, setting, self.now)
        request = self.run_until_submit()
        attempt = self.database.execute("SELECT attempt_id,evidence FROM application_attempts WHERE job_key=? "
                                        "AND state='SUBMITTING'", (self.key,)).fetchone()
        evidence = json.loads(attempt["evidence"])
        evidence["review"] = dict(evidence.get("review") or {}, application_method="EMAIL",
                                  email_application={"address": "jobs@employer.example", "subject": None})
        payload = json.loads(self.database.execute("SELECT payload FROM desktop_requests WHERE request_id=?",
                                                   (request["request_id"],)).fetchone()[0])
        payload["job"]["candidate_email"] = MAILBOX
        with self.database:
            self.database.execute("UPDATE application_attempts SET evidence=? WHERE attempt_id=?",
                                  (json.dumps(evidence), attempt["attempt_id"]))
            self.database.execute("UPDATE desktop_requests SET payload=? WHERE request_id=?",
                                  (json.dumps(payload), request["request_id"]))
        return request

    def mailbox_wall(self, setting=access_outcomes.SIGN_IN_FOR_ME):
        request = self.email_submit(setting)
        self.assertTrue(desktop_adapter.complete(self.database, dict(request, result=MAILBOX_STOP), self.now)["accepted"])
        self.advance(60)
        self.cycle()
        return request

    def test_the_freestyle_sequence_shows_the_mailbox_sign_in_card(self):
        request = self.mailbox_wall()
        detail = self.events("ATTEMPT_NOT_COUNTED_CAPABILITY_BLOCKED")[-1]
        self.assertEqual((detail["request_state"], detail["code"]), ("CONSUMED", "AUTHENTICATION_REQUIRED"))
        self.assertTrue(self.events("RECONCILED_GUARD_NEVER_ARMED"))
        wait = self.assert_waiting(access_outcomes.MAILBOX, "AUTHENTICATION_REQUIRED")   # attempts 0: no try spent
        self.assertEqual(wait["host"], "mail.google.com")                    # the mailbox's site, not the posting's
        self.assertEqual(self.events("ACCESS_STEP_PLANNED"), [])             # never Sidedoor's own step
        words = "Sign in to %s in Sidedoor's browser." % MAILBOX
        card = access_outcomes.waiting(self.database)
        self.assertEqual([item["words"] for item in card], [words])
        self.assertTrue(self.row()["next_action"].startswith(words))
        self.assertEqual(access_cards.kind(card[0]), access_cards.SIGN_IN)
        self.assertEqual(access_cards.offers(self.database, card[0])[1], "I've signed in")
        for text in (words, self.row()["next_action"]):
            self.assertNotIn("—", text)
            self.assertNotIn("automation", text.lower())
        self.assertFalse(applications.uncertain_submission(self.database, self.key))
        sessions = len(self.requests())
        for _ in range(4):                                                   # never retried on its own
            self.advance(3600)
            self.cycle()
            self.assertIsNone(desktop_adapter.claim(self.database, self.root, "access-fixture", self.now))
        self.assertEqual(len(self.requests()), sessions)
        self.assertEqual(self.row()["blocker"], "AUTHENTICATION_REQUIRED")
        self.assertEqual(request["payload"]["operation"], "submit")

    def test_after_signing_in_the_application_is_tried_again(self):
        self.mailbox_wall()
        submits = [row for row in self.requests() if row["operation"] == "submit"]
        with self.database:
            said = access_cards.done(self.database, self.key, self.now)
        self.assertEqual(said["tone"], "success")
        self.assertEqual(self.row()["state"], "RETRY_WAIT")
        self.assertEqual(self.waits()[-1]["state"], access_outcomes.RESOLVED)
        self.assertEqual(self.run_until_submit()["payload"]["operation"], "submit")   # no re-entry
        self.assertEqual(len([row for row in self.requests() if row["operation"] == "submit"]), len(submits) + 1)

    def test_a_second_step_on_the_mailbox_shows_the_same_sign_in_card(self):
        """K7 a: a code or "verify it's you" prompt on the candidate's mailbox, under "Sign in and create
        accounts for me", is the mailbox sign-in card too, never the RETRY_WAIT loop."""
        request = self.email_submit()
        stop = dict(MAILBOX_STOP, blocked=dict(MAILBOX_STOP["blocked"], code="MFA_REQUIRED"))
        self.assertTrue(desktop_adapter.complete(self.database, dict(request, result=stop), self.now)["accepted"])
        self.advance(60)
        self.cycle()
        wait = self.assert_waiting(access_outcomes.MAILBOX, "MFA_REQUIRED")
        self.assertEqual(wait["host"], "mail.google.com")
        self.assertEqual(self.events("ACCESS_STEP_PLANNED"), [])
        words = "Finish signing in to %s in Sidedoor's browser." % MAILBOX
        card = access_outcomes.waiting(self.database)
        self.assertEqual([item["words"] for item in card], [words])
        self.assertTrue(self.row()["next_action"].startswith(words))
        self.assertEqual(access_cards.offers(self.database, card[0])[1], "I've signed in")
        self.assertNotIn("—", self.row()["next_action"])

    def test_skip_and_ask_each_time_also_wait_on_the_mailbox_and_never_close(self):
        """K13 a (owner's K4 a): the mailbox is not a job site's account, so neither setting closes the job."""
        for setting in (access_outcomes.SKIP, access_outcomes.ASK):
            with self.subTest(setting=setting):
                self.setUp()
                self.mailbox_wall(setting)
                self.assert_waiting(access_outcomes.MAILBOX, "AUTHENTICATION_REQUIRED")
                self.assertNotIn(self.row()["state"], ("ABANDONED", "EXHAUSTED", "INELIGIBLE"))
                self.assertEqual([wait["state"] for wait in self.waits()], [access_outcomes.WAITING])


class ReceiptCheckOnTheMailbox(unittest.TestCase):
    """K11 a: an email application's receipt check (RECONCILE) that meets the candidate's mailbox signed out."""
    for _name in ("setUp", "connect", "write_config", "review", "job", "cycle", "advance", "run_until_submit", "row",
                  "requests", "waits", "events"):
        locals()[_name] = getattr(walls.AccessOutcomeTests, _name)
    for _name in ("next_read", "blocked_read", "counts", "view", "finish_step"):
        locals()[_name] = getattr(confirm_card.TheSequence, _name)
    email_submit = MailboxSignIn.email_submit
    del _name

    def test_the_card_opens_the_mailbox_and_done_only_re_checks_the_receipt(self):
        request = self.email_submit()
        with mock.patch.object(applications, "email_applications_enabled", return_value=True):
            desktop_adapter.arm(self.database, self.root, dict(request, observed_form_revision="fixture-form-v1"), self.now)
        self.assertTrue(desktop_adapter.complete(self.database, dict(request, result={"acknowledged": False}), self.now)["accepted"])
        self.advance(60)
        self.cycle()
        self.assertTrue(applications.uncertain_submission(self.database, self.key))
        self.blocked_read(confirm_card.SIGN_IN)
        [wait] = confirm_read.waiting(self.database)
        self.assertEqual((wait["host"], wait["mailbox"]), ("mail.google.com", MAILBOX))
        [card] = self.view()[0]["cards"]
        self.assertEqual(card["need"], "Sign in to %s in Sidedoor's browser. Do not apply again." % MAILBOX)
        # K18: the job's hold says the same, never "The site needs you to sign in".
        self.assertEqual(self.row()["next_action"], "Sign in to %s in Sidedoor's browser. Do not apply again" % MAILBOX)
        url, site, wall, _ = user_step._page(self.database, confirm_read.key(self.key))
        self.assertEqual((site, wall), ("mail.google.com", "AUTHENTICATION_REQUIRED"))
        self.assertTrue(url.startswith("https://mail.google.com/") and MAILBOX in url)
        before = self.counts()
        self.assertTrue(self.finish_step(wait["id"], user_step.SIGN_IN))
        self.assertEqual(self.row()["state"], "RECONCILE")                    # stays on the receipt check
        read = self.next_read()
        self.assertEqual(read["payload"]["operation"], "reconcile")           # never a fresh submit
        self.assertEqual(self.counts()["submit"], before["submit"])
        self.assertEqual(self.counts()["armed"], before["armed"])

    def form_check_blocked(self, on):
        """K17: a FORM application sent with its outcome unknown; its receipt check stops on a sign-in and
        says where (`blocked.on`)."""
        self.sent_with_outcome_unknown = confirm_card.TheSequence.sent_with_outcome_unknown.__get__(self)
        self.sent_with_outcome_unknown()
        candidate = json.loads(self.database.execute(
            "SELECT payload FROM desktop_requests WHERE job_key=? AND operation='submit'", (self.key,)).fetchone()[0])["job"]["candidate_email"]
        before = self.counts()
        self.blocked_read({"blocked": dict(confirm_card.SIGN_IN["blocked"], on=on)})
        return candidate, before

    def test_k17_a_form_applications_mailbox_wall_gets_the_mailbox_card(self):
        candidate, before = self.form_check_blocked("MAILBOX")
        [wait] = confirm_read.waiting(self.database)
        self.assertEqual((wait["host"], wait["mailbox"]), ("mail.google.com", candidate))
        [card] = self.view()[0]["cards"]
        self.assertEqual(card["need"], "Sign in to %s in Sidedoor's browser. Do not apply again." % candidate)
        self.assertEqual(self.row()["next_action"], "Sign in to %s in Sidedoor's browser. Do not apply again" % candidate)
        self.assertTrue(self.finish_step(wait["id"], user_step.SIGN_IN))
        self.assertEqual(self.row()["state"], "RECONCILE")
        self.assertEqual(self.next_read()["payload"]["operation"], "reconcile")   # never a fresh submit
        self.assertEqual((self.counts()["submit"], self.counts()["armed"]), (before["submit"], before["armed"]))

    def test_k17_a_form_applications_site_wall_is_as_before(self):
        self.form_check_blocked("SITE")
        [wait] = confirm_read.waiting(self.database)
        self.assertEqual((wait["host"], wait["mailbox"]), ("jobs.ashbyhq.com", None))
        [card] = self.view()[0]["cards"]
        self.assertEqual(card["need"], "The site needs you to sign in. Do not apply again.")
        self.assertEqual(self.row()["next_action"], confirm_read.ACTION["SIGN_IN"])


if __name__ == "__main__":
    unittest.main()
