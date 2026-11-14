"""E15.T9 single sign-on: one of the access step's ways in (access_step.py, E15.T8), and only there.

Under "Sign in and create accounts for me", a job that met its site's account wall gets Sidedoor's own
step. When the mailbox the user chose in Setup has a sign-in provider, that step is told the provider and
the account Setup saw signed in, and holds the guard's `single_sign_on` tool beside the saved sign-in, the
new account and the emailed link. No read or submit holds it or is told anything: their prompt is main's.

Owner, 2026-11-13: the provider is whichever one the mailbox belongs to; the AI finds the button and goes
through the provider's own screens, its permission screen included, with no limit in code on what that
screen asks ("DO NOT CONSTRAIN THE AGENT"), and chooses among the ways in with no order.

The harness is E15.T8's (`Case`): the real `application_browser.consume`, the real guard started from the
product's own command line, a fake page, a script playing the AI. No browser, no provider.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, the OS credential store off)
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import access_outcomes  # noqa: E402
import access_step  # noqa: E402
import application_browser  # noqa: E402
import applications  # noqa: E402
import browser_guard  # noqa: E402
import browser_runtime  # noqa: E402
import engines  # noqa: E402
import setup_steps  # noqa: E402
import site_sessions  # noqa: E402
import test_access_step_e15_t8 as t8  # noqa: E402
from manual_application_browser import request_at, write_output  # noqa: E402

SITE, WALL = t8.SITE, t8.WALL
MAILBOX = "mailbox@example.invalid"      # the mailbox Setup saw signed in; not the job's candidate address
SIGNED_IN = {"outcome": "SIGNED_IN", "email_for": None, "account_created": None, "sign_up_submitted": False}


class Case(t8.Case):
    webmail = "Gmail"

    def setUp(self):
        super().setUp()
        self.mailbox_in_setup(self.webmail)
        self.forwarded = []

    def mailbox_in_setup(self, label, checked=True):
        def change(state):
            for key in ("webmail", "email_check_passed_at", "locked_email"):
                state.pop(key, None)
            if label:
                state["webmail"] = {"label": label, "url": "https://mail.example.invalid/"}
            if checked:
                state.update(email_check_passed_at="2026-11-01T00:00:00+00:00", locked_email=MAILBOX)
        setup_steps.update_state(change)

    def guard(self, config, directory):
        """E15.T8's guard, plus this branch's one flag, read from the same product command line."""
        command = browser_runtime.guard_command(config, directory, "inspect")
        self.command = command[:command.index("--")]
        self.codex = browser_runtime.codex_options(config, directory, "inspect")
        proxy = super().guard(config, directory)
        proxy.single_sign_on = bool("--single-sign-on" in self.command and proxy.access_step)
        proxy.to_child = lambda message: self.forwarded.append(message) if message.get("method") == "tools/call" else None
        return proxy

    def press(self, proxy, name=browser_guard.SSO_TOOL, **arguments):
        """A call the guard forwards answers nothing itself: "" then; else the guard's own answer."""
        before = len(self.out.getvalue())
        proxy.handle({"jsonrpc": "2.0", "id": len(self.forwarded) + 100, "method": "tools/call",
                      "params": {"name": name, "arguments": arguments or {"target": "e9", "element": "the provider's button"}}})
        with proxy.idle:
            proxy.in_flight.clear()
        return self.out.getvalue()[before:]

    def audit(self):
        return [json.loads(line) for line in (self.directory / browser_runtime.GUARD_AUDIT).read_text().splitlines()]

    def asked(self):
        return json.loads((self.directory / "request.json").read_text())

    def finished(self):
        row = self.db.execute("SELECT detail FROM application_events WHERE job_key=? AND event=? ORDER BY rowid DESC LIMIT 1",
                              (self.fixture.key, access_step.FINISHED_EVENT)).fetchone()
        return json.loads(row[0])


class InTheStep(Case):
    def test_the_step_is_told_the_provider_and_the_account_holds_the_tool_and_gets_in(self):
        self.meet_wall()

        def play(proxy):
            refused = self.press(proxy)                                     # the site's button for the provider
            refused += self.press(proxy, "browser_click", target="e21", element="the account already signed in")
            refused += self.press(proxy, "browser_click", target="e34", element="the provider's permission screen")
            self.page.sign_in()
            return refused
        summary, code, verdict = self.step(play, SIGNED_IN)
        self.assertEqual(self.asked()["access"]["single_sign_on"], {"provider": "Google", "account": MAILBOX})
        self.assertIn("--single-sign-on", self.command)
        self.assertIn(browser_guard.SSO_TOOL, self.tools)
        self.assertTrue([option for option in self.codex if browser_guard.SSO_TOOL in option])   # Codex approves it
        self.assertEqual(self.said, [""])                                   # nothing refused
        self.assertEqual([(m["params"]["name"], m["params"]["arguments"]["target"]) for m in self.forwarded],
                         [("browser_click", "e9"), ("browser_click", "e21"), ("browser_click", "e34")])
        self.assertEqual([row["decision"] for row in self.audit() if row["decision"] in ("SINGLE_SIGN_ON_STARTED", "REFUSED")],
                         ["SINGLE_SIGN_ON_STARTED"])
        self.assertEqual(self.page.typed, {})                               # no password, no field typed
        self.assertEqual((verdict, self.wait(), self.steps()), (access_step.PLANNED, None, [("ACCESS", "DONE", "SIGNED_IN")]))
        self.assertTrue(site_sessions.has(SITE))                            # the site's session is saved
        self.assertEqual(self.saved(), [])                                  # and no password anywhere
        self.assertTrue(self.finished()["single_sign_on_pressed"])

    def test_the_permission_screen_is_the_ais_to_pass_whatever_it_asks_for(self):
        # Owner, 2026-11-13: "DO NOT CONSTRAIN THE AGENT". No scope list, no gate on that click.
        self.meet_wall()

        def play(proxy):
            said = self.press(proxy)
            said += self.press(proxy, "browser_click", target="e34", element="Allow: contacts and drive")
            self.page.sign_in()
            return said
        self.step(play, SIGNED_IN)
        self.assertEqual((self.said, len(self.forwarded)), ([""], 2))
        self.assertFalse([row for row in self.audit() if row["decision"] == "REFUSED"])

    def test_a_mailbox_with_no_provider_has_no_tool_and_the_step_keeps_its_other_ways(self):
        for label in ("Proton Mail", None):
            self.setUp()
            self.mailbox_in_setup(label)
            self.meet_wall()
            self.step(lambda proxy: self.press(proxy), {"outcome": "SIGN_UP_NOT_TRIED", "sign_up_submitted": False})
            self.assertNotIn("single_sign_on", self.asked()["access"], label)
            self.assertNotIn("--single-sign-on", self.command)
            self.assertNotIn(browser_guard.SSO_TOOL, self.tools)
            self.assertIn("type_new_account_password", self.tools)          # the other ways in are untouched
            self.assertIn("not available in this session", self.said[0])    # refused by name
            self.assertEqual(self.forwarded, [])
            self.assertFalse([option for option in self.codex if browser_guard.SSO_TOOL in option])

    def test_the_provider_of_each_webmail_and_the_account_only_when_setups_check_passed(self):
        told = {}
        for label in ("Gmail", "Outlook.com", "Outlook (work or school)", "Yahoo Mail", "iCloud Mail",
                      "Proton Mail", "Zoho Mail", "Fastmail", "Other"):
            self.mailbox_in_setup(label)
            told[label] = (access_outcomes.single_sign_on() or {}).get("provider")
        self.assertEqual(told, {"Gmail": "Google", "Outlook.com": "Microsoft", "Outlook (work or school)": "Microsoft",
                                "Yahoo Mail": "Yahoo", "iCloud Mail": "Apple", "Proton Mail": None, "Zoho Mail": None,
                                "Fastmail": None, "Other": None})
        self.mailbox_in_setup("Gmail")
        self.assertNotEqual(self.fixture.job()["candidate_email"], MAILBOX)   # never the job's own address
        self.assertEqual(access_outcomes.single_sign_on(), {"provider": "Google", "account": MAILBOX})
        self.mailbox_in_setup("Gmail", checked=False)
        self.assertEqual(access_outcomes.single_sign_on(), {"provider": "Google", "account": None})
        setup_steps.update_state(lambda state: state.update(locked_email=MAILBOX))   # an address with no passed check
        self.assertEqual(access_outcomes.single_sign_on(), {"provider": "Google", "account": None})

    def test_a_malformed_call_is_refused_and_clicks_nothing(self):
        self.meet_wall()
        self.step(lambda proxy: self.press(proxy, browser_guard.SSO_TOOL, element="no ref"), SIGNED_IN)
        self.assertIn(browser_guard.MALFORMED_CODE, self.said[0])
        self.assertEqual(self.forwarded, [])


class TheOwnersLines(Case):
    def test_verify_its_you_at_the_provider_stops_the_step_as_a_human_check_anywhere(self):
        self.meet_wall()
        summary, code, verdict = self.step(lambda proxy: self.press(proxy), {"outcome": "HUMAN_CHECK", "sign_up_submitted": False})
        self.assertEqual(len(self.forwarded), 1)                            # the button, then nothing touched the check
        self.assertEqual((code, verdict, self.wait()), ("CAPTCHA_REQUIRED", "WAITING", "HUMAN_CHECK"))
        self.assertEqual(self.page.typed, {})

    def test_a_password_prompt_or_an_unreadable_code_goes_to_the_user_and_nothing_is_typed(self):
        for outcome, why, wall_code, reason in (("SIGN_IN_REFUSED", "PASSWORD", "AUTHENTICATION_REQUIRED", "SIGN_IN"),
                                                ("CODE_UNREADABLE", "SECOND_STEP", "MFA_REQUIRED", "CODE_UNREADABLE")):
            self.setUp()
            self.meet_wall()
            report = {"outcome": outcome, "sign_up_submitted": False, "wall": dict(WALL, single_sign_on=why)}
            summary, code, verdict = self.step(lambda proxy: self.press(proxy), report)
            self.assertEqual((code, verdict, self.wait()), (wall_code, "WAITING", reason), outcome)
            self.assertEqual((self.page.typed, self.finished()["single_sign_on"]), ({}, why))   # the report, kept for the record

    def test_a_step_that_pressed_the_button_and_died_is_one_card_and_is_never_run_again(self):
        # Hard line, no form submitted twice: T8's rule covers the provider's button too.
        self.meet_wall()
        summary, code, verdict = self.step(lambda proxy: self.press(proxy), None, fail=TimeoutError("the session died"))
        self.assertEqual((summary["state"], verdict), ("COMPLETE", "WAITING"))
        self.assertEqual(self.steps(), [("ACCESS", "DONE", "SIGN_UP_UNCONFIRMED")])     # settled, not due again
        self.assertIsNone(access_step.due(self.db, self.fixture.key, SITE, "9999"))
        self.assertEqual(len(self.configs), 1)
        self.assertEqual(self.meet_wall()[2], "WAITING")                    # the wall again: no second step
        self.assertEqual(len(self.steps()), 1)
        # The guard's own row for the button is enough by itself, with no forwarded call beside it.
        self.setUp()
        self.meet_wall()
        with mock.patch.object(access_step, "_audit", return_value=[{"decision": "SINGLE_SIGN_ON_STARTED"}]):
            self.step(None, None, fail=TimeoutError("the session died"))
        self.assertEqual(self.steps(), [("ACCESS", "DONE", "SIGN_UP_UNCONFIRMED")])

    def test_choosing_for_me_never_releases_a_job_whose_submission_may_have_reached_the_employer(self):
        with self.db:
            access_outcomes.set_account_setting(self.db, "ASK", applications.stamp())
            self.assertEqual(access_outcomes.record(self.db, self.fixture.job(), "AUTHENTICATION_REQUIRED", applications.stamp()), "WAITING")
        before = self.job()
        self.assertEqual(self.wait(), "ASK_EACH_TIME")
        with mock.patch.object(applications, "uncertain_submission", return_value=True), self.db:
            self.assertEqual(access_outcomes.set_account_setting(self.db, "SIGN_IN", applications.stamp()), [])
        self.assertEqual((self.wait(), self.job(), self.steps()), ("ASK_EACH_TIME", before, []))
        with self.db:   # once nothing may have reached the employer, the same wait is released for one attempt
            access_outcomes.set_account_setting(self.db, "ASK", applications.stamp())
            self.assertEqual(access_outcomes.set_account_setting(self.db, "SIGN_IN", applications.stamp()), [self.fixture.key])


class NotADeadEnd(Case):
    """The provider's sign-in finished and the site still asks for a sign-in (an account there already uses
    the address): the same step goes on to another way in. Nothing in code ends it early."""

    def test_the_same_step_goes_on_to_the_emailed_sign_in_link(self):
        self.meet_wall()
        self.mailbox.messages.append({"link": "https://jobs.ashbyhq.com/magic?t=9"})

        def play(proxy):
            self.press(proxy)                                               # the provider finishes; the site is not signed in
            said = self.tool(proxy, "open_confirmation_link", url=self.mailbox.link())
            self.page.sign_in()
            return said
        summary, code, verdict = self.step(play, dict(SIGNED_IN, email_for="SIGN_IN"))
        self.assertEqual(self.page.opened, ["https://jobs.ashbyhq.com/magic?t=9"])     # the link tool still worked after the button
        self.assertEqual((verdict, self.wait(), self.steps()), (access_step.PLANNED, None, [("ACCESS", "DONE", "SIGNED_IN")]))
        self.assertTrue(site_sessions.has(SITE))

    def test_the_email_not_there_yet_is_one_more_look_not_a_card(self):
        self.meet_wall()
        report = {"outcome": "EMAIL_PENDING", "email_for": "SIGN_IN", "account_created": None, "sign_up_submitted": False,
                  "wall": dict(WALL, account_exists_signal=True, single_sign_on="SITE_NOT_SIGNED_IN")}
        summary, code, verdict = self.step(lambda proxy: self.press(proxy), report)
        self.assertEqual((verdict, self.wait()), (access_step.PLANNED, None))
        self.assertEqual(self.steps(), [("ACCESS", "DONE", access_step.SIGN_IN_EMAIL_PENDING), ("CONFIRM", "DUE", None)])
        self.assertEqual(self.finished()["single_sign_on"], "SITE_NOT_SIGNED_IN")      # reported as met
        self.assertNotIn("single_sign_on", access_outcomes.clean_description({"single_sign_on": "made up"}))


class OnePlace(Case):
    """One session never holds both "stop at a login" and "get signed in": a read or a submit stops at the
    wall (main's prompt, untouched) and the step, a session of its own, gets in."""

    STOPS = ("For authentication or MFA return blocked with code\nAUTHENTICATION_REQUIRED or MFA_REQUIRED and next_action.",
             "the role is closed, a login is\nrequired, human verification as above,")

    def main_prompt(self, operation, request):
        prompt = application_browser.PROMPT
        for section in application_browser.OPERATION_SECTIONS.get(operation, ()):
            prompt += "\n\n" + application_browser.OPERATION_PROMPTS[section]
        if operation == "submit":
            prompt += "\n" + application_browser.SUBMIT_SUPPLEMENT
        return prompt + "\nUNTRUSTED_REQUEST_JSON:\n" + application_browser.encoded(request)

    def read(self):
        seen = {}

        def session(config, prompt, directory, timeout):
            command = browser_runtime.guard_command(config, directory, "inspect")
            seen.update(prompt=prompt, request=request_at(directory), command=command[:command.index("--")],
                        codex=browser_runtime.codex_options(config, directory, "inspect"))
            write_output(directory, seen["request"], {"blocked": {"code": "AUTHENTICATION_REQUIRED", "next_action": "sign in", "wall": WALL}})
        self.request(session)
        return seen

    def test_a_read_is_mains_prompt_byte_for_byte_whatever_the_setting_and_never_holds_the_tool(self):
        for setting in (access_outcomes.SIGN_IN_FOR_ME, access_outcomes.ASK, None):
            self.setting = setting
            self.setUp()
            seen = self.read()
            self.assertEqual(seen["prompt"], self.main_prompt("inspect", seen["request"]), setting)
            for stop in self.STOPS:
                self.assertEqual(seen["prompt"].count(stop), 1, setting)
            self.assertNotIn("single_sign_on", seen["prompt"], setting)
            self.assertNotIn("--single-sign-on", seen["command"], setting)
            self.assertFalse([option for option in seen["codex"] if browser_guard.SSO_TOOL in option], setting)
        self.assertFalse(hasattr(application_browser, "SINGLE_SIGN_ON") or hasattr(application_browser, "GETTING_IN_SWAPS"))

    def test_the_step_holds_the_goal_and_no_instruction_to_stop_because_a_login_is_required(self):
        self.meet_wall()
        self.step(lambda proxy: self.press(proxy), SIGNED_IN)
        rules = self.prompts[-1].split("\nUNTRUSTED_REQUEST_JSON:\n")[0]
        for stop in self.STOPS + ("a login is", "For authentication or MFA return blocked"):
            self.assertNotIn(stop, rules)
        for told in ("GOAL.", "You work out the best feasible way in", "single_sign_on (offered only when",
                     "request.access.single_sign_on.provider", "SITE_NOT_SIGNED_IN", "THE OWNER'S LINES"):
            self.assertIn(told, rules)
        # No provider is named and no order among the ways in is given.
        for name in set(access_outcomes.SINGLE_SIGN_ON_PROVIDERS.values()):
            self.assertNotIn(name, access_step.PROMPT)
        for order in ("at most once", "first try", "before any other", "another provider"):
            self.assertNotIn(order, access_step.PROMPT)

    def test_ask_me_each_time_plans_no_step_so_nothing_can_press_the_button(self):
        self.setting = access_outcomes.ASK
        self.setUp()
        summary, code, verdict = self.meet_wall()
        self.assertEqual((verdict, self.wait(), self.steps()), ("WAITING", "ASK_EACH_TIME", []))
        self.assertIsNone(access_step.due(self.db, self.fixture.key, SITE, "9999"))

    def test_only_an_access_step_can_hold_the_tool(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {browser_guard.SSO_CONFIG_KEY: True}
            for operation in browser_guard.OPERATIONS:                       # the config key alone gives nothing
                self.assertNotIn("--single-sign-on", browser_runtime.guard_command(config, directory, operation), operation)
                with mock.patch.object(browser_guard.subprocess, "Popen"):
                    proxy = browser_guard.Proxy(operation, ["node", "cli.js"], stdin=io.StringIO(""), stdout=io.StringIO(),
                                                focus_keeper=False, single_sign_on=True)
                self.assertNotIn(browser_guard.SSO_TOOL, proxy.offered(), operation)
            with mock.patch.object(browser_guard.subprocess, "Popen"):
                step = browser_guard.Proxy("inspect", ["node", "cli.js"], stdin=io.StringIO(""), stdout=io.StringIO(),
                                           focus_keeper=False, saved_sign_in=SITE, access_step=True, single_sign_on=True)
            self.assertIn(browser_guard.SSO_TOOL, step.offered())
        self.assertIn(browser_guard.SSO_TOOL, engines.CLAUDE.browser_tools("inspect", held=browser_guard.held_step_tools(["--saved-sign-in", SITE, "--access-step", "--single-sign-on"])))
        self.assertNotIn(browser_guard.SSO_TOOL, engines.CLAUDE.browser_tools("inspect"))


if __name__ == "__main__":
    unittest.main()
