"""E15.T8 account creation and email confirmation, by outcome: under "Sign in and create accounts for me"
a job that meets its site's account wall gets Sidedoor's own step; every branch ends signed in or on a
Needs you card, the generated password reaches nothing the AI or the disk can show, and no second sign-up
can start.

No browser, no AI, no mail: `Page` is the fake page the real guard types into, `Mailbox` is the fake
candidate mailbox, and each test's script plays the AI's part with the real guard in front (started from
the very command line the product builds for the step).
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, the OS credential store off)
import importlib.util
import io
import json
import logging
import re
import shutil
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import access_cards  # noqa: E402
import access_outcomes  # noqa: E402
import access_step  # noqa: E402
import application_browser  # noqa: E402
import applications  # noqa: E402
import browser_guard  # noqa: E402
import browser_runtime  # noqa: E402
import credentials  # noqa: E402
import metrics_page  # noqa: E402
import overview_page  # noqa: E402
import saved_sign_ins  # noqa: E402
import saved_sign_ins_page  # noqa: E402
import site_sessions  # noqa: E402
import user_profile  # noqa: E402
from manual_application_browser import Fixture, request_at, write_output  # noqa: E402

MARKER = 'Gn4"new<&>%+ *!~\'()/=?walrus-77b3d1-onyx'    # the generated password, as the test makes the guard make it
SITE = "jobs.ashbyhq.com"                                # the Fixture's job: https://jobs.ashbyhq.com/synthetic/role-123
URL = "https://jobs.ashbyhq.com/synthetic/role-123"
EMAIL = "candidate@example.invalid"
FORMS = browser_guard.secret_forms(MARKER)
WALL = {"sign_in_offered": True, "sign_up_offered": True, "apply_without_account_offered": False,
        "account_exists_signal": None, "sso_providers": [], "second_step": "NONE", "signed_in_as": None}


def answer(value):
    return {"result": {"content": [{"type": "text", "text": "### Result\n" + json.dumps(value)}]}}


class Page:
    """The site's page as the guard's own calls meet it: fields by snapshot ref, a cookie once signed in."""

    def __init__(self, url=URL):
        self.url, self.typed, self.opened, self.cookies, self.lands = url, {}, [], [], {}
        self.fields = {"e1": "email", "e2": "password", "e3": "password"}

    def call(self, method, params):
        arguments = params["arguments"]
        if params["name"] == "browser_navigate":
            self.opened.append(arguments["url"])
            self.url = self.lands.get(arguments["url"], arguments["url"])      # where the browser really ends up
            return {"result": {"content": [{"type": "text", "text": "### Page\n- Page URL: " + arguments["url"]}]}}
        code = arguments["code"]
        given = json.loads(re.search(r"const input = (.*);\n", code).group(1))
        if given["sidedoor"] == "page-address":
            return answer([{"url": self.url}])
        if given["sidedoor"] == "site-session-capture":
            return answer([{"url": self.url, "cookies": list(self.cookies)}])
        if given["sidedoor"] == "site-session-restore":
            return answer([{"restored": 0, "kept": 0, "failed": 0}])
        if site_sessions.site_key(self.url) != given["site"]:
            return answer([{"outcome": "wrong_site"}])
        if given["sidedoor"] == "new-account-password":
            if any(ref not in self.fields for ref in given["refs"]):
                return answer([{"outcome": "not_found"}])
            if any(self.fields[ref] != "password" for ref in given["refs"]):
                return answer([{"outcome": "not_password_field"}])
            self.typed.update({ref: given["password"] for ref in given["refs"]})
            return answer([{"outcome": "filled", "typed": len(given["refs"])}])
        for kind in ("email", "password"):
            if given[kind + "_ref"]:
                self.typed[given[kind + "_ref"]] = given[kind]
        return answer([{"outcome": "filled", "email": bool(given["email_ref"]), "password": bool(given["password_ref"])}])

    def sign_in(self):
        self.cookies = [{"name": "session", "value": "signed-in-cookie", "domain": SITE, "path": "/", "expires": -1,
                         "httpOnly": True, "secure": True, "sameSite": "Lax"}]


class Mailbox:
    """The candidate's mailbox: what the site has sent so far."""

    def __init__(self):
        self.messages = []

    def code(self):
        return next((m["code"] for m in reversed(self.messages) if "code" in m), None)

    def link(self):
        return next((m["link"] for m in reversed(self.messages) if "link" in m), None)


class Case(unittest.TestCase):
    setting = access_outcomes.SIGN_IN_FOR_ME

    def setUp(self):
        root = Path(tempfile.mkdtemp(prefix="access-step-"))
        self.addCleanup(shutil.rmtree, root, True)
        self.fixture = Fixture(root / "fixture")
        self.db = self.fixture.database
        self.addCleanup(self.db.close)
        self.home = user_profile.home()
        credentials.clear_cache()
        site_sessions._reported.clear()
        site_sessions.before_session(SITE)
        if self.setting:
            with self.db:
                access_outcomes.set_account_setting(self.db, self.setting, applications.stamp())
        patch = mock.patch.object(browser_guard, "generate_password", lambda *rules: MARKER)
        patch.start()
        self.addCleanup(patch.stop)
        self.page, self.mailbox, self.configs, self.prompts, self.relayed = Page(), Mailbox(), [], [], ""

    # --- the product's side ---------------------------------------------------------------------------
    def request(self, session):
        """One request of the job, consumed; then what the applications run does with a wall result."""
        with self.db:
            self.db.execute("UPDATE desktop_requests SET state='CONSUMED' WHERE state='DONE'")
        request_id = self.fixture.queue("inspect")
        summary = application_browser.consume(self.fixture.root, self.db, self.fixture.config, request_id, session)
        row = self.db.execute("SELECT result FROM desktop_requests WHERE request_id=?", (request_id,)).fetchone()
        result = json.loads(row["result"]) if row and row["result"] else None
        code = applications.stop_code(result) if result else None
        verdict = None
        if code in applications.ACCESS_BLOCK_CODES:
            with self.db:
                verdict = applications.hold_access_block(self.db, self.fixture.job(), code, "next", applications.stamp(),
                                                         self.fixture.config, result)
        return summary, code, verdict

    def meet_wall(self, code="AUTHENTICATION_REQUIRED", wall=WALL):
        def read(config, prompt, directory, timeout):
            self.read_configs = getattr(self, "read_configs", []) + [config]
            self.read_prompt = prompt
            write_output(directory, request_at(directory), {"blocked": {"code": code, "next_action": "sign in", "wall": wall}})
        return self.request(read)

    def step(self, play, report, fail=None, reach=None):
        """The job's next request, spent on the access step: `play(proxy)` is what the AI does with the
        tools, `report` what it then reports (a dict for `access`), `fail` an error the session dies with.
        `reach`: the session looks at the page first, as every real one that reports anything has (one
        snapshot through the guard); by default not for a session that dies having done nothing."""
        reach = (play is not None or fail is None) if reach is None else reach
        def session(config, prompt, directory, timeout):
            self.configs.append(config)
            self.prompts.append(prompt)
            self.directory = directory
            proxy = self.guard(config, directory)
            self.tools = sorted(tool["name"] for tool in proxy.own_tools())
            proxy.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
            if reach:
                proxy.handle({"jsonrpc": "2.0", "id": 0, "method": "tools/call", "params": {"name": "browser_snapshot", "arguments": {}}})
                with proxy.idle:
                    proxy.in_flight.clear()
                forwarded = getattr(self, "forwarded", None)   # a harness built on this one records what it forwards
                if isinstance(forwarded, list) and forwarded and forwarded[-1].get("id") == 0:
                    forwarded.pop()                            # the look itself is not what such a test is about
            self.said = [play(proxy)] if play else []
            proxy.capture_site_session(final=True) if proxy.child.poll() is None else None
            proxy.audit.flush()
            self.relayed += self.out.getvalue()
            if fail:
                raise fail
            if report is not None:
                write_output(directory, request_at(directory), {"access": dict({"wall": WALL}, **report)})
        return self.request(session)

    def guard(self, config, directory):
        """The real guard, given exactly the flags the product's command line carries for this session."""
        command = browser_runtime.guard_command(config, directory, "inspect")
        flag = lambda name: command[command.index(name) + 1] if name in command else None
        self.out = io.StringIO()
        with mock.patch.object(browser_guard.subprocess, "Popen"):
            proxy = browser_guard.Proxy("inspect", ["node", "cli.js", "--output-dir", str(directory / "browser"), "--config", "x"],
                                        audit=flag("--audit"), stdin=io.StringIO(""), stdout=self.out, focus_keeper=False,
                                        site_session=flag("--site-session"), saved_sign_in=flag("--saved-sign-in"),
                                        create_account=flag("--create-account"), access_step="--access-step" in command, reset_recorded=flag("--reset-recorded"),
                                        no_page_code="--no-page-code" in command,
                                        human_check=flag("--human-check"), claim=flag("--claim"), uploads=flag("--uploads"))
        self.addCleanup(proxy.audit.close)
        proxy.child.poll = lambda: None
        proxy.child_call = self.page.call
        proxy.to_child = lambda message: None
        proxy.site_captured_at = -1000.0
        return proxy

    def tool(self, proxy, name, **arguments):
        proxy.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
        return json.loads(self.out.getvalue().strip().split("\n")[-1])["result"]["content"][0]["text"]

    # --- reading the outcome ----------------------------------------------------------------------------
    def job(self):
        return dict(self.db.execute("SELECT state,blocker,next_action,next_at FROM application_jobs WHERE job_key=?",
                                    (self.fixture.key,)).fetchone())

    def wait(self):
        wait = access_outcomes.open_wait(self.db, self.fixture.key)
        return wait["reason"] if wait else None

    def steps(self):
        return [(row["kind"], row["state"], row["outcome"]) for row in self.db.execute(
            "SELECT * FROM %s ORDER BY id" % access_step.TABLE)]

    def card(self):
        with mock.patch.object(access_cards.user_step, "available", return_value=True), \
                mock.patch.object(access_cards.user_step, "openable", return_value=True):
            cards = access_cards.view(overview_page.Reader(self.db), self.fixture.config, None)["cards"]
        return cards[0] if cards else None

    def saved(self):
        return [(row["site"], row["email"], row["made_by"], row["has_password"]) for row in saved_sign_ins.listing()["rows"]]

    # --- the AI's part, in pieces -----------------------------------------------------------------------
    def sign_up(self, proxy):
        said = self.tool(proxy, "type_new_account_password", password_targets=["e2", "e3"])
        return said


class Creating(Case):
    def test_a_wall_plans_one_step_and_the_step_creates_the_account_with_an_emailed_code(self):
        summary, code, verdict = self.meet_wall()
        self.assertEqual((code, verdict), ("AUTHENTICATION_REQUIRED", access_step.PLANNED))
        self.assertIsNone(self.wait())                                         # no card: Sidedoor's turn first
        self.assertEqual((self.job()["state"], self.job()["next_action"]), ("RETRY_WAIT", access_step.PLANNED_WORDS))
        self.assertEqual(self.steps(), [("ACCESS", "DUE", None)])
        self.mailbox.messages.append({"code": "481516"})

        def play(proxy):
            said = self.sign_up(proxy)
            self.assertEqual(self.mailbox.code(), "481516")                    # the AI reads the code and types it itself
            self.page.sign_in()
            return said
        summary, code, verdict = self.step(play, {"outcome": "ACCOUNT_CREATED", "account_created": True, "sign_up_submitted": True})
        self.assertEqual(self.said, [browser_guard.CREATE_TEXT["filled"]])
        self.assertEqual(self.page.typed, {"e2": MARKER, "e3": MARKER})        # the guard typed it, twice the same
        # The session Sidedoor started: the step's tools and nothing that submits or waits on a human check.
        config = self.configs[-1]
        self.assertEqual((config[saved_sign_ins.CONFIG_KEY], config[saved_sign_ins.CREATE_CONFIG_KEY],
                          config[site_sessions.CONFIG_KEY], config["_application_operation"]), (SITE, EMAIL, SITE, "inspect"))
        self.assertEqual(self.tools, ["fill_saved_sign_in", "open_confirmation_link", "type_new_account_password"])
        self.assertNotIn("_application_guard_claim", config)
        self.assertNotIn("_application_human_check", config)
        request = json.loads((self.directory / "request.json").read_text())
        self.assertEqual(request["access"], {"kind": "ACCESS", "site": SITE, "saved_sign_in": False, "may_create_account": True,
                                             "sign_up_email": EMAIL, "wall": None, "own_account_unconfirmed": False})
        # Got through: no wait, the job is due again, the account is a saved sign-in, the session is saved.
        self.assertEqual((code, verdict, self.wait()), ("AUTHENTICATION_REQUIRED", access_step.PLANNED, None))
        self.assertEqual((self.job()["state"], self.job()["next_action"], self.job()["next_at"]),
                         ("RETRY_WAIT", access_step.THROUGH_WORDS, None))
        self.assertEqual(self.steps(), [("ACCESS", "DONE", "ACCOUNT_CREATED")])
        self.assertEqual(self.saved(), [(SITE, EMAIL, "SIDEDOOR", True)])
        self.assertFalse(saved_sign_ins.pending(SITE))
        self.assertEqual(saved_sign_ins.for_fill(SITE), (EMAIL, MARKER))
        self.assertEqual(saved_sign_ins_page.view()["rows"][0]["made_by"], "Sidedoor created it")
        self.assertTrue(site_sessions.has(SITE))                               # reused by the read, the submit, later jobs
        self.assertEqual(metrics_page.sidedoor_handled(overview_page.Reader(self.db)), [(SITE, mock.ANY)])
        # The next request is an ordinary read again, started for the site with its session.
        summary, code, verdict = self.request(lambda config, prompt, directory, timeout: (
            self.configs.append(config), write_output(directory, request_at(directory), self.fixture.review())))
        self.assertEqual((summary["state"], code), ("COMPLETE", None))
        self.assertNotIn(saved_sign_ins.CONFIG_KEY, self.configs[-1])          # a read holds no sign-in tool
        # A wall again after a step that got through, with the sign-in saved: a sign-in step, no card (SignedOutAgain).
        summary, code, verdict = self.meet_wall()
        self.assertEqual((verdict, self.wait(), len(self.steps())), (access_step.PLANNED, None, 2))

    def test_an_emailed_link_is_opened_only_on_the_site_itself(self):
        self.meet_wall()
        self.mailbox.messages.append({"link": "https://www.jobs.ashbyhq.com/confirm?token=abc"})

        def play(proxy):
            self.sign_up(proxy)
            said = self.tool(proxy, "open_confirmation_link", url=self.mailbox.link())
            self.page.sign_in()
            return said
        summary, code, verdict = self.step(play, {"outcome": "ACCOUNT_CREATED", "account_created": True, "sign_up_submitted": True})
        self.assertEqual(self.page.opened, ["https://www.jobs.ashbyhq.com/confirm?token=abc"])
        self.assertIn("Page URL: https://www.jobs.ashbyhq.com/confirm", self.said[0])
        self.assertEqual((verdict, self.saved()), (access_step.PLANNED, [(SITE, EMAIL, "SIDEDOOR", True)]))

    def test_a_link_to_another_host_is_not_opened_and_the_card_says_exactly_that(self):
        self.meet_wall()
        for link in ("https://click.mailer.test/c?u=https%3A%2F%2Fjobs.ashbyhq.com%2Fconfirm", "https://jobs.ashbyhq.com.evil.test/confirm",
                     "https://careers.ashbyhq.com/confirm", "javascript:alert(1)", "http://127.0.0.1:9222/json"):
            self.mailbox.messages.append({"link": link})

        def play(proxy):
            self.sign_up(proxy)
            return [self.tool(proxy, "open_confirmation_link", url=m["link"]) for m in self.mailbox.messages]
        summary, code, verdict = self.step(play, {"outcome": "LINK_NOT_OPENED", "account_created": True, "sign_up_submitted": True})
        self.assertEqual(self.page.opened, [])
        self.assertEqual(set(self.said[0]), {browser_guard.LINK_TEXT["elsewhere"]})
        self.assertEqual((verdict, self.wait()), ("WAITING", "CONFIRMATION_LINK_UNOPENED"))
        self.assertEqual(self.saved(), [(SITE, EMAIL, "SIDEDOOR", True)])      # the account exists: the user can see its password
        card = self.card()
        self.assertEqual(card["need"], "The site's confirmation link is on another site. Confirm it yourself.")
        self.assertEqual((card["done_label"], card["resend_label"], card["hint"]),
                         ("I've confirmed it", None, "Open the link in the site's email. Then press I've confirmed it."))

    def test_a_link_the_browser_reads_differently_or_that_lands_elsewhere_is_not_followed(self):   # review round 1, 2
        self.meet_wall()
        slash = "https://evil.test\\@jobs.ashbyhq.com/confirm"                 # urlsplit says the site; Chrome loads evil.test
        self.assertEqual(site_sessions.site_key(slash), SITE)
        moved = "https://jobs.ashbyhq.com/confirm?next=elsewhere"
        self.page.lands[moved] = "https://login.other-site.test/welcome"

        def play(proxy):
            return [self.tool(proxy, "open_confirmation_link", url=url)
                    for url in (slash, "https://jobs.ashbyhq.com/confirm\x00", "https://jobs.ashbyhq.com/con\tfirm", moved)]
        self.step(play, {"outcome": "LINK_NOT_OPENED", "account_created": None, "sign_up_submitted": False})
        self.assertEqual(set(self.said[0]), {browser_guard.LINK_TEXT["elsewhere"]})
        self.assertEqual(self.page.opened, [moved, "about:blank"])            # only the well-formed one was opened, then left
        self.assertNotIn("other-site", self.relayed)                           # nothing of the other site's page reaches the AI
        self.assertEqual(self.wait(), "CONFIRMATION_LINK_UNOPENED")

    def test_an_account_that_already_exists_goes_to_the_user_once(self):
        self.meet_wall()
        summary, code, verdict = self.step(self.sign_up, {"outcome": "ACCOUNT_EXISTS", "account_created": False, "sign_up_submitted": True,
                                                           "after_sign_up": "ACCOUNT_EXISTS_SHOWN"})
        self.assertEqual((code, verdict, self.wait()), ("AUTHENTICATION_REQUIRED", "WAITING", "ACCOUNT_EXISTS"))
        self.assertEqual((self.saved(), saved_sign_ins.pending(SITE)), ([], False))   # the site refused it: nothing is kept
        card = self.card()
        self.assertEqual(card["need"], "You already have an account on this site. Sign in once.")
        self.assertEqual((card["step"]["label"], card["done_label"], card["abandon"]), ("Sign in", "I've signed in", True))
        self.assertEqual(self.job()["state"], "BLOCKED")                       # no retry until something changes
        self.assertIsNone(access_step.due(self.db, self.fixture.key, SITE, "9999"))
        self.assertNotIn("forgot", " ".join(self.configs[-1].keys()).lower())
        self.assertIn("never for an account the\n  candidate had before", self.prompts[-1])   # D2: never a reset of the user's own account
        self.assertIn("never reload and resubmit a sign-up form", self.prompts[-1])   # the owner's line

    def test_a_text_message_code_and_a_human_check_go_to_the_ordinary_cards(self):
        self.meet_wall()
        summary, code, verdict = self.step(self.sign_up, {"outcome": "CODE_UNREADABLE", "account_created": True, "sign_up_submitted": True})
        self.assertEqual((code, verdict, self.wait()), ("MFA_REQUIRED", "WAITING", "CODE_UNREADABLE"))
        self.assertEqual(self.card()["need"], "The site sent a sign-in code Sidedoor cannot read. Sign in once.")
        self.assertEqual(self.saved(), [(SITE, EMAIL, "SIDEDOOR", True)])      # created: Show password lets the user in

    def test_a_human_check_stops_the_step_and_nothing_touches_it(self):
        self.meet_wall()
        summary, code, verdict = self.step(None, {"outcome": "HUMAN_CHECK", "account_created": None, "sign_up_submitted": False})
        self.assertEqual((code, verdict, self.wait()), ("CAPTCHA_REQUIRED", "WAITING", "HUMAN_CHECK"))
        self.assertEqual(self.card()["need"], "The site needs a human check.")
        self.assertNotIn("wait_for_human_check", self.tools)                   # the step has no tool for a check
        self.assertEqual((self.page.typed, self.saved(), saved_sign_ins.pending(SITE)), ({}, [], False))

    def test_a_refused_sign_up_is_a_card_and_the_password_is_forgotten(self):
        self.meet_wall()
        summary, code, verdict = self.step(self.sign_up, {"outcome": "SIGN_UP_REFUSED", "account_created": False, "sign_up_submitted": True,
                                                           "after_sign_up": "REFUSAL_SHOWN"})
        self.assertEqual((verdict, self.wait()), ("WAITING", "SIGN_UP_FAILED"))
        self.assertEqual(self.card()["need"], "The site did not accept a new account. Sign in or create it yourself.")
        self.assertEqual((self.saved(), saved_sign_ins.pending(SITE)), ([], False))
        self.assertEqual(self.fixture.job()["state"], "BLOCKED")               # waiting, never lost


class Truthful(Case):
    """The first real run (2026-11-14): the site took the sign-up and showed nothing; the step called that a
    refusal, the card said "The site did not accept a new account." and the password was thrown away."""

    REAL = {"outcome": "SIGN_UP_REFUSED", "account_created": None, "sign_up_submitted": True}   # that run's own report

    def test_refused_stands_only_when_the_site_itself_showed_a_refusal(self):
        for said in (None, "NOTHING_SHOWN", "CONFIRMATION_ASKED", "The page went back to Sign In", 7):
            self.setUp()
            self.meet_wall()
            report = dict(self.REAL) if said is None else dict(self.REAL, after_sign_up=said)
            summary, code, verdict = self.step(self.sign_up, report)
            # Unknown, never refused; and Sidedoor's own record permits a reset: that step is planned, no card yet.
            self.assertEqual(self.steps(), [("ACCESS", "DONE", "SIGN_UP_UNCONFIRMED"), ("ACCESS", "DUE", None)], said)
            self.assertEqual((verdict, self.wait(), self.card()), (access_step.PLANNED, None, None), said)
            self.assertEqual((saved_sign_ins.pending(SITE), saved_sign_ins.for_fill(SITE)), (True, (EMAIL, MARKER)), said)   # kept
            self.assertEqual(self.saved(), [])                                 # and not yet called a saved sign-in
        # The site showed its refusal: refused, and the password is forgotten.
        self.setUp()
        self.meet_wall()
        self.step(self.sign_up, dict(self.REAL, after_sign_up="REFUSAL_SHOWN"))
        self.assertEqual((self.steps()[-1][2], self.wait(), saved_sign_ins.pending(SITE)), ("SIGN_UP_REFUSED", "SIGN_UP_FAILED", False))
        # "Account exists" is the user's own account however it is reported: nothing of Sidedoor's is kept for it.
        self.setUp()
        self.meet_wall()
        self.step(self.sign_up, {"outcome": "ACCOUNT_EXISTS", "account_created": None, "sign_up_submitted": True, "after_sign_up": "NOTHING_SHOWN"})
        self.assertEqual((self.wait(), saved_sign_ins.pending(SITE)), ("ACCOUNT_EXISTS", False))   # reset review, 2: the outcome alone drops it
        # A refusal claimed with no sign-up sent and nothing typed: a card that claims nothing.
        self.setUp()
        self.meet_wall()
        self.step(None, {"outcome": "SIGN_UP_REFUSED", "account_created": None, "sign_up_submitted": False})
        self.assertEqual((self.steps()[-1][2], self.wait()), ("UNKNOWN", "ASK_EACH_TIME"))

    def test_the_ai_can_sign_in_with_the_password_just_typed_in_the_same_step_and_that_saves_it(self):
        self.meet_wall()

        def play(proxy):   # the site showed nothing, so the AI finds out: the sign-in form, the same password
            typed = self.sign_up(proxy)
            self.page.typed.clear()
            proxy.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "browser_navigate", "arguments": {"url": URL}}})   # a reload
            with proxy.idle:
                proxy.in_flight.clear()
            again = self.tool(proxy, "type_new_account_password", password_targets=["e2"])    # never a second sign-up
            said = self.tool(proxy, "fill_saved_sign_in", email_target="e1", password_target="e2")
            self.page.sign_in()
            return typed, again, said
        summary, code, verdict = self.step(play, {"outcome": "SIGNED_IN", "account_created": None, "sign_up_submitted": True,
                                                  "after_sign_up": "NOTHING_SHOWN"})
        self.assertEqual(self.said[0], (browser_guard.CREATE_TEXT["filled"], browser_guard.CREATE_TEXT["already"],
                                        browser_guard.SAVED_SIGN_IN_TEXT["filled"]))
        self.assertEqual(self.page.typed, {"e1": EMAIL, "e2": MARKER})        # the pending password, on the same site
        self.assertEqual((verdict, self.wait(), self.saved(), saved_sign_ins.pending(SITE)),
                         (access_step.PLANNED, None, [(SITE, EMAIL, "SIDEDOOR", True)], False))
        prompt = self.prompts[-1]
        for fact in ("A site can accept a sign-up and show nothing at all afterwards.", "Its confirmation email can arrive minutes later.",
                     "shows whether the site took the sign-up; reloading the page may show it too"):
            self.assertIn(fact, prompt)
        self.assertLess(prompt.index("A site can accept a sign-up"), prompt.index("TOOLS, all on"))     # among the facts
        self.assertIn("never reload and resubmit a sign-up form", prompt)      # reloading to look is not resubmitting
        self.assertIn("REFUSAL_SHOWN", prompt)

    def test_a_sign_in_with_the_pending_password_on_another_site_types_nothing(self):
        self.meet_wall()

        def play(proxy):
            self.sign_up(proxy)
            self.page.typed.clear()
            self.page.url = "https://login.other-site.test/sign-in"
            return self.tool(proxy, "fill_saved_sign_in", email_target="e1", password_target="e2")
        self.step(play, dict(self.REAL, after_sign_up="NOTHING_SHOWN"))
        self.assertEqual((self.said, self.page.typed), ([browser_guard.SAVED_SIGN_IN_TEXT["wrong_site"]], {}))

    def test_the_step_is_stamped_when_it_ended_not_when_it_began(self):
        self.meet_wall()
        during = []
        self.step(lambda proxy: during.append(applications.stamp()), {"outcome": "NO_ACCOUNT_NEEDED", "sign_up_submitted": False})
        ended = self.db.execute("SELECT ended_at FROM %s" % access_step.TABLE).fetchone()[0]
        event = self.db.execute("SELECT at FROM application_events WHERE event=?", (access_step.FINISHED_EVENT,)).fetchone()[0]
        self.assertGreater(ended, during[0])
        self.assertGreater(event, during[0])


RESET = 'Rs9"new<&>%+ *!~otter-52aa90-jade'      # the password the guard makes for a reset


class Reset(Case):
    """Owner, 2026-11-14: the site's own password reset is fine for an account Sidedoor itself made and never
    confirmed; never for an account the user had before. The tool decides, whatever the AI asks."""

    def unconfirmed(self):
        """Sidedoor sent a sign-up and the site showed nothing: the reset step is planned by itself, no card."""
        self.meet_wall()
        summary, code, verdict = self.step(self.sign_up, {"outcome": "SIGN_UP_REFUSED", "account_created": None, "sign_up_submitted": True,
                                                          "after_sign_up": "NOTHING_SHOWN"})
        self.assertEqual((verdict, self.wait(), self.card(), self.steps()[-1]), (access_step.PLANNED, None, None, ("ACCESS", "DUE", None)))
        self.page.typed.clear()
        patch = mock.patch.object(browser_guard, "generate_password", lambda *rules: RESET)
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_reset_is_typed_where_sidedoor_made_the_account_and_the_new_password_is_saved_once_it_signs_in(self):
        self.unconfirmed()
        self.mailbox.messages.append({"link": "https://jobs.ashbyhq.com/reset?t=4"})

        def play(proxy):
            opened = self.tool(proxy, "open_confirmation_link", url=self.mailbox.link())   # the reset email's link
            typed = self.tool(proxy, "type_reset_password", password_targets=["e2", "e3"])
            self.assertEqual((saved_sign_ins.pending(SITE), saved_sign_ins.has(SITE)), (True, False))   # pending, not yet saved
            again = self.tool(proxy, "type_reset_password", password_targets=["e2"])          # the form is sent once
            self.page.typed.clear()
            said = self.tool(proxy, "fill_saved_sign_in", email_target="e1", password_target="e2")
            self.page.sign_in()
            return typed, again, said
        summary, code, verdict = self.step(play, {"outcome": "SIGNED_IN", "email_for": "PASSWORD_RESET", "sign_up_submitted": False})
        self.assertIn("type_reset_password", self.tools)
        self.assertEqual(self.said[0], (browser_guard.RESET_TEXT["filled"], browser_guard.RESET_TEXT["already"],
                                        browser_guard.SAVED_SIGN_IN_TEXT["filled"]))
        self.assertEqual(self.page.typed, {"e1": EMAIL, "e2": RESET})          # the sign-in used the new one
        self.assertEqual((verdict, self.saved(), saved_sign_ins.pending(SITE)), (access_step.PLANNED, [(SITE, EMAIL, "SIDEDOOR", True)], False))
        self.assertEqual(saved_sign_ins.for_fill(SITE), (EMAIL, RESET))
        request = json.loads((self.directory / "request.json").read_text())
        self.assertEqual((request["access"]["own_account_unconfirmed"], request["access"]["may_create_account"]), (True, False))
        for told in ("Only then is the site's own password reset permitted", "It is never permitted in the session that sent the sign-up."):
            self.assertIn(told, self.prompts[-1])
        audit = (self.directory / "guard.jsonl").read_text()
        self.assertEqual([json.loads(line)["outcome"] for line in audit.splitlines() if '"RESET_PASSWORD"' in line], ["filled", "already"])
        for text in (audit, self.relayed, "\n".join(self.prompts)):
            for form in browser_guard.secret_forms(RESET) + ["otter-52aa90"]:
                self.assertNotIn(form, text)                                   # the AI never sees it

    def test_no_reset_without_sidedoors_own_record_whatever_the_ai_asks(self):
        # A site the user had an account on before: no pending password, so nothing is made or typed.
        self.meet_wall()

        def play(proxy):
            return self.tool(proxy, "type_reset_password", password_targets=["e2", "e3"])
        self.step(play, {"outcome": "ACCOUNT_EXISTS", "account_created": None, "sign_up_submitted": False})
        self.assertIn("not available in this session", self.said[0])           # not held: there is no record
        self.assertNotIn("type_reset_password", self.tools)
        self.assertEqual(self.page.typed, {})
        self.assertEqual((saved_sign_ins.pending(SITE), self.saved(), self.wait()), (False, [], "ACCOUNT_EXISTS"))   # the user's turn, once
        self.assertFalse(json.loads((self.directory / "request.json").read_text())["access"]["own_account_unconfirmed"])
        # A site whose sign-in is already confirmed: the saved password is never replaced by a reset.
        saved_sign_ins.add(SITE, EMAIL, MARKER, saved_sign_ins.SIDEDOOR)
        directory = Path(tempfile.mkdtemp(prefix="access-step-reset-"))
        self.addCleanup(shutil.rmtree, directory, True)
        config = {saved_sign_ins.CONFIG_KEY: SITE, saved_sign_ins.ACCESS_CONFIG_KEY: True, saved_sign_ins.RESET_CONFIG_KEY: EMAIL}
        proxy = self.guard(config, directory)                                   # even a guard told there is a record
        self.assertEqual(self.tool(proxy, "type_reset_password", password_targets=["e2"]), browser_guard.RESET_TEXT["none"])
        self.assertEqual((self.page.typed, saved_sign_ins.for_fill(SITE)), ({}, (EMAIL, MARKER)))
        self.assertEqual(saved_sign_ins.reset_pending(SITE, "x", recorded_email=EMAIL), "none")

    def test_never_in_the_session_that_typed_the_sign_up_and_a_pending_password_alone_permits_nothing(self):   # reset review, 1
        directory = Path(tempfile.mkdtemp(prefix="access-step-reset-"))
        self.addCleanup(shutil.rmtree, directory, True)
        base = {saved_sign_ins.CONFIG_KEY: SITE, saved_sign_ins.ACCESS_CONFIG_KEY: True}
        # The sign-up session: the site answers "account exists" and something steers the AI to forgot-password.
        proxy = self.guard(dict(base, **{saved_sign_ins.CREATE_CONFIG_KEY: EMAIL}), directory)
        self.assertEqual(self.tool(proxy, "type_new_account_password", password_targets=["e2"]), browser_guard.CREATE_TEXT["filled"])
        self.page.typed.clear()
        self.assertTrue(saved_sign_ins.pending(SITE))
        self.assertIn("not available in this session", self.tool(proxy, "type_reset_password", password_targets=["e2"]))
        # Even a guard wrongly told of a record never resets in the session that typed the sign-up.
        saved_sign_ins.drop_pending(SITE)
        both = self.guard(dict(base, **{saved_sign_ins.CREATE_CONFIG_KEY: EMAIL, saved_sign_ins.RESET_CONFIG_KEY: EMAIL}), directory)
        self.tool(both, "type_new_account_password", password_targets=["e2"])
        self.page.typed.clear()
        self.assertEqual(self.tool(both, "type_reset_password", password_targets=["e2"]), browser_guard.RESET_TEXT["none"])
        # A later session with only the pending password and no stored record: not held.
        later = self.guard(base, directory)
        self.assertIn("not available in this session", self.tool(later, "type_reset_password", password_targets=["e2"]))
        self.assertEqual((self.page.typed, saved_sign_ins.for_fill(SITE)), ({}, (EMAIL, MARKER)))

    def test_account_exists_however_reported_and_whenever_ends_the_reset_for_good(self):   # reset review, 2 and 3
        # In the sign-up's own step: the outcome alone, whatever after_sign_up says, drops the password.
        for shown in (None, "NOTHING_SHOWN", "CONFIRMATION_ASKED"):
            self.setUp()
            self.meet_wall()
            report = {"outcome": "ACCOUNT_EXISTS", "account_created": None, "sign_up_submitted": True}
            self.step(self.sign_up, dict(report, after_sign_up=shown) if shown else report)
            self.assertEqual((saved_sign_ins.pending(SITE), access_step.own_account(self.db, SITE)), (False, False), shown)
        # The shown value with any other outcome does the same.
        self.setUp()
        self.meet_wall()
        self.step(self.sign_up, {"outcome": "EMAIL_PENDING", "email_for": "SIGN_IN", "sign_up_submitted": True, "after_sign_up": "ACCOUNT_EXISTS_SHOWN"})
        self.assertEqual((saved_sign_ins.pending(SITE), access_step.own_account(self.db, SITE)), (False, False))
        # A LATER step that meets "account exists" clears a record that was resettable until then, for good.
        self.setUp()
        self.unconfirmed()
        self.assertEqual((saved_sign_ins.pending(SITE), access_step.own_account(self.db, SITE)), (True, True))
        self.step(None, {"outcome": "ACCOUNT_EXISTS", "account_created": None, "sign_up_submitted": False})
        self.assertEqual((saved_sign_ins.pending(SITE), access_step.own_account(self.db, SITE)), (False, False))
        with mock.patch.object(access_cards.user_step, "available", return_value=True), \
                mock.patch.object(access_cards.user_step, "openable", return_value=True):
            access_cards.done(self.db, self.fixture.key, applications.stamp())
        self.meet_wall()
        self.step(lambda proxy: self.tool(proxy, "type_reset_password", password_targets=["e2"]), {"outcome": "SIGN_IN_REFUSED", "sign_up_submitted": False})
        self.assertIn("not available in this session", self.said[0])
        self.assertFalse(json.loads((self.directory / "request.json").read_text())["access"]["own_account_unconfirmed"])
        self.assertEqual(self.page.typed, {})

    def test_a_reset_password_is_saved_only_when_a_sign_in_with_it_worked(self):   # reset review, 4
        self.unconfirmed()
        self.mailbox.messages.append({"link": "https://jobs.ashbyhq.com/magic?t=8"})

        def play(proxy):   # the site rejects the reset form; an emailed sign-in link then gets the browser in
            typed = self.tool(proxy, "type_reset_password", password_targets=["e2"])
            self.tool(proxy, "open_confirmation_link", url=self.mailbox.link())
            self.page.sign_in()
            return typed
        summary, code, verdict = self.step(play, {"outcome": "SIGNED_IN", "sign_up_submitted": False, "after_reset": "REFUSAL_SHOWN"})
        self.assertEqual(self.said, [browser_guard.RESET_TEXT["filled"]])
        self.assertEqual((verdict, self.saved(), saved_sign_ins.pending(SITE)), (access_step.PLANNED, [], False))   # dropped, never saved
        # The reset form itself ends signed in: saved.
        self.setUp()
        self.unconfirmed()
        self.step(lambda proxy: (self.tool(proxy, "type_reset_password", password_targets=["e2"]), self.page.sign_in()),
                  {"outcome": "SIGNED_IN", "sign_up_submitted": False, "after_reset": "SIGNED_IN"})
        self.assertEqual((self.saved(), saved_sign_ins.for_fill(SITE)), ([(SITE, EMAIL, "SIDEDOOR", True)], (EMAIL, RESET)))
        # A reset typed in one step and signed in with in a later one: saved then.
        self.setUp()
        self.unconfirmed()
        self.step(lambda proxy: self.tool(proxy, "type_reset_password", password_targets=["e2"]),
                  {"outcome": "EMAIL_PENDING", "email_for": "PASSWORD_RESET", "sign_up_submitted": False, "after_reset": "CONFIRMATION_ASKED"})
        self.assertEqual((self.saved(), saved_sign_ins.pending_state(SITE)), ([], {"reset": True, "site_confirmed": False}))
        with self.db:
            self.db.execute("UPDATE %s SET not_before='2000-01-01T00:00:00+00:00' WHERE state='DUE'" % access_step.TABLE)
        self.page.typed.clear()
        self.step(lambda proxy: (self.tool(proxy, "fill_saved_sign_in", email_target="e1", password_target="e2"), self.page.sign_in()),
                  {"outcome": "SIGNED_IN", "sign_up_submitted": False})
        self.assertEqual((self.page.typed, self.saved()), ({"e1": EMAIL, "e2": RESET}, [(SITE, EMAIL, "SIDEDOOR", True)]))

    def test_a_rejected_fill_followed_by_another_way_in_saves_nothing_and_drops_nothing(self):   # confirming review, 4
        for reset in (False, True):
            self.setUp()
            self.unconfirmed()
            self.mailbox.messages.append({"link": "https://jobs.ashbyhq.com/magic?t=6"})

            def play(proxy):   # the pending password is filled and rejected; an emailed link then gets the browser in
                if reset:
                    self.tool(proxy, "type_reset_password", password_targets=["e2"])
                self.tool(proxy, "fill_saved_sign_in", email_target="e1", password_target="e2")
                self.tool(proxy, "open_confirmation_link", url=self.mailbox.link())
                self.page.sign_in()
            summary, code, verdict = self.step(play, {"outcome": "SIGNED_IN", "sign_up_submitted": False})
            self.assertEqual((verdict, self.saved(), saved_sign_ins.pending(SITE)), (access_step.PLANNED, [], True), reset)   # kept pending
            self.assertTrue(access_step.own_account(self.db, SITE))            # and the site is as resettable as it was
            # The mirror: the fill is the last way in before the step ended signed in: saved.
            self.setUp()
            self.unconfirmed()
            self.mailbox.messages.append({"link": "https://jobs.ashbyhq.com/reset?t=6"})

            def play(proxy):
                self.tool(proxy, "open_confirmation_link", url=self.mailbox.link())
                if reset:
                    self.tool(proxy, "type_reset_password", password_targets=["e2"])
                self.tool(proxy, "fill_saved_sign_in", email_target="e1", password_target="e2")
                self.page.sign_in()
            self.step(play, {"outcome": "SIGNED_IN", "sign_up_submitted": False})
            self.assertEqual((self.saved(), saved_sign_ins.pending(SITE)), ([(SITE, EMAIL, "SIDEDOOR", True)], False), reset)

    def test_an_error_after_the_new_password_was_kept_never_allows_a_second_reset(self):   # reset review, 6
        self.unconfirmed()

        def play(proxy):
            real = proxy.site_call
            proxy.site_call = mock.Mock(side_effect=RuntimeError("the browser went away"))
            first = self.tool(proxy, "type_reset_password", password_targets=["e2"])
            proxy.site_call = real
            return first, self.tool(proxy, "type_reset_password", password_targets=["e2"])
        self.step(play, {"outcome": "SIGN_IN_REFUSED", "sign_up_submitted": False})
        self.assertEqual(self.said[0], (browser_guard.RESET_TEXT["unconfirmed"], browser_guard.RESET_TEXT["already"]))
        self.assertEqual(self.page.typed, {})

    def test_a_reset_is_never_typed_on_another_site_and_the_earlier_password_stands(self):
        self.unconfirmed()

        def play(proxy):
            self.page.url = "https://login.other-site.test/reset"
            elsewhere = self.tool(proxy, "type_reset_password", password_targets=["e2"])
            self.page.url = URL
            wrong_field = self.tool(proxy, "type_reset_password", password_targets=["e1"])
            return elsewhere, wrong_field
        self.step(play, {"outcome": "SIGN_IN_REFUSED", "sign_up_submitted": False})
        self.assertEqual(self.said[0], (browser_guard.RESET_TEXT["wrong_site"], browser_guard.RESET_TEXT["not_password_field"]))
        self.assertEqual((self.page.typed, saved_sign_ins.for_fill(SITE)), ({}, (EMAIL, MARKER)))   # nothing typed: the first one stands

    def test_a_rejected_sign_in_keeps_the_record_and_a_reset_email_that_never_comes_is_the_unknown_card(self):
        self.unconfirmed()

        def play(proxy):
            said = self.tool(proxy, "fill_saved_sign_in", email_target="e1", password_target="e2")   # the site rejects it
            return said, self.tool(proxy, "type_reset_password", password_targets=["e2"])
        report = {"outcome": "EMAIL_PENDING", "email_for": "PASSWORD_RESET", "sign_up_submitted": False}
        summary, code, verdict = self.step(play, report)
        self.assertEqual((verdict, self.steps()[-2][2], self.steps()[-1][:2]), (access_step.PLANNED, "RESET_EMAIL_PENDING", ("CONFIRM", "DUE")))
        self.assertTrue(saved_sign_ins.pending(SITE))
        with self.db:
            self.db.execute("UPDATE %s SET not_before='2000-01-01T00:00:00+00:00' WHERE state='DUE'" % access_step.TABLE)
        summary, code, verdict = self.step(None, report)
        self.assertEqual((verdict, self.wait()), ("WAITING", "SIGN_UP_UNCONFIRMED"))
        card = self.card()
        self.assertEqual(card["need"], "Your account on this site may or may not exist. Sign in once, or reset the password on the site.")
        self.assertIsNone(card["resend_label"])
        self.assertTrue(saved_sign_ins.pending(SITE))                          # the record stays for a later step
        # A sign-in the site rejected does not forget it either.
        self.setUp()
        self.unconfirmed()
        self.step(lambda proxy: self.tool(proxy, "fill_saved_sign_in", email_target="e1", password_target="e2"),
                  {"outcome": "SIGN_IN_REFUSED", "sign_up_submitted": False})
        self.assertEqual((self.wait(), saved_sign_ins.pending(SITE)), ("SIGN_UP_UNCONFIRMED", True))   # the reset step did not get in


class AutomaticReset(Case):
    """The mock sites' cases 6 and 7 (real Codex, 2026-11-13): the sign-up shows nothing, the site did not keep
    the password, the sign-in with it is refused. The reset is only possible in a LATER step, and Sidedoor
    plans that step itself (owner: "resetting password would be fine"): no card until that is exhausted."""

    FILL = dict(email_target="e1", password_target="e2")
    CARD = "Your account on this site may or may not exist. Sign in once, or reset the password on the site."

    def first(self):
        """Step 1 as the mock run had it: sign-up sent once, nothing shown, the pending password filled, refused."""
        self.meet_wall()

        def play(proxy):
            return self.sign_up(proxy), self.tool(proxy, "fill_saved_sign_in", **self.FILL), self.tool(proxy, "type_reset_password", password_targets=["e2"])
        summary, code, verdict = self.step(play, {"outcome": "SIGN_IN_REFUSED", "account_created": None, "sign_up_submitted": True,
                                                  "after_sign_up": "NOTHING_SHOWN"})
        self.assertNotIn("type_reset_password", self.tools)                    # never in the sign-up's own session
        self.assertIn("not available in this session", self.said[0][2])
        # No card, no wait: the reset step is planned for the job's next round.
        self.assertEqual((verdict, self.wait(), self.card(), self.job()["state"], self.steps()),
                         (access_step.PLANNED, None, None, "RETRY_WAIT", [("ACCESS", "DONE", "SIGN_IN_REFUSED"), ("ACCESS", "DUE", None)]))
        self.assertEqual((saved_sign_ins.pending(SITE), access_step.own_account(self.db, SITE)), (True, True))
        self.page.typed.clear()
        patch = mock.patch.object(browser_guard, "generate_password", lambda *rules: RESET)
        patch.start()
        self.addCleanup(patch.stop)

    def test_case_6_the_reset_step_is_planned_by_itself_opens_the_link_types_once_signs_in_and_is_saved(self):
        self.first()
        self.mailbox.messages.append({"link": "https://jobs.ashbyhq.com/reset?t=6"})

        def play(proxy):
            opened = self.tool(proxy, "open_confirmation_link", url=self.mailbox.link())
            typed = self.tool(proxy, "type_reset_password", password_targets=["e2", "e3"])
            self.page.typed.clear()
            said = self.tool(proxy, "fill_saved_sign_in", **self.FILL)
            self.page.sign_in()
            return typed, said
        summary, code, verdict = self.step(play, {"outcome": "SIGNED_IN", "email_for": "PASSWORD_RESET", "sign_up_submitted": False})
        self.assertEqual(self.tools, ["fill_saved_sign_in", "open_confirmation_link", "type_reset_password"])   # no second sign-up
        self.assertEqual(self.said[0], (browser_guard.RESET_TEXT["filled"], browser_guard.SAVED_SIGN_IN_TEXT["filled"]))
        self.assertEqual(self.page.typed, {"e1": EMAIL, "e2": RESET})
        self.assertEqual((verdict, self.wait(), self.saved(), saved_sign_ins.for_fill(SITE), saved_sign_ins.pending(SITE)),
                         (access_step.PLANNED, None, [(SITE, EMAIL, "SIDEDOOR", True)], (EMAIL, RESET), False))
        self.assertEqual(len(self.steps()), 2)

    def test_case_7_a_refused_reset_gets_the_one_fresh_sign_up_by_itself_and_then_the_card(self):
        self.first()
        refused = {"outcome": "SIGN_IN_REFUSED", "sign_up_submitted": False, "after_reset": "REFUSAL_SHOWN"}
        summary, code, verdict = self.step(lambda proxy: self.tool(proxy, "fill_saved_sign_in", **self.FILL), refused)
        self.assertEqual((verdict, self.wait(), self.card(), self.steps()[-2:]),
                         (access_step.PLANNED, None, None, [("ACCESS", "DONE", "RESET_REFUSED"), ("ACCESS", "DUE", None)]))
        self.page.typed.clear()

        def again(proxy):
            return self.tool(proxy, "type_reset_password", password_targets=["e2"]), self.sign_up(proxy), self.tool(proxy, "fill_saved_sign_in", **self.FILL)
        summary, code, verdict = self.step(again, {"outcome": "SIGN_IN_REFUSED", "account_created": None, "sign_up_submitted": True,
                                                   "after_sign_up": "NOTHING_SHOWN"})
        self.assertEqual(self.tools, ["fill_saved_sign_in", "open_confirmation_link", "type_new_account_password"])   # the reset is not offered again
        self.assertEqual(self.said[0][1], browser_guard.CREATE_TEXT["filled"])
        # Exhausted: the card, and nothing further is ever planned (no reset, no third sign-up).
        self.assertEqual((verdict, self.wait(), self.job()["state"]), ("WAITING", "SIGN_IN", "BLOCKED"))
        self.assertEqual(self.card()["need"], "The site needs you to sign in.")
        self.assertEqual((len(self.steps()), access_step.own_account(self.db, SITE), access_step.sign_up_allowed(self.db, SITE)), (3, False, False))
        self.assertEqual(self.meet_wall()[2], "WAITING")
        self.assertEqual(len(self.steps()), 3)

    def test_a_reset_step_that_does_not_get_in_is_the_card_and_no_second_reset_step_is_planned(self):
        self.first()
        summary, code, verdict = self.step(lambda proxy: (self.tool(proxy, "type_reset_password", password_targets=["e2"]),
                                                          self.tool(proxy, "fill_saved_sign_in", **self.FILL)),
                                           {"outcome": "SIGN_IN_REFUSED", "sign_up_submitted": False, "after_reset": "NOTHING_SHOWN"})
        self.assertEqual((verdict, self.wait(), self.card()["need"], len(self.steps())), ("WAITING", "SIGN_UP_UNCONFIRMED", self.CARD, 2))
        self.assertEqual(self.meet_wall()[2], "WAITING")
        self.assertEqual(len(self.steps()), 2)

    def test_a_reset_step_whose_sessions_die_before_the_page_ends_on_the_card_not_on_another_reset_step(self):   # review A1
        self.first()
        with self.db:
            code = access_step.settle_unrun(self.db, self.fixture.key, applications.stamp())   # the read cap, the step never ran
            verdict = access_step.on_wall(self.db, self.fixture.job(), code, applications.stamp())
        self.assertEqual((access_step.reset_offered(self.db, SITE), verdict, len(self.steps())), (True, ("WAIT", "SIGN_UP_UNCONFIRMED"), 2))
        for _ in range(5):
            with self.db:
                self.assertEqual(access_step.on_wall(self.db, self.fixture.job(), code, applications.stamp())[0], "WAIT")
                self.assertIsNone(access_step.settle_unrun(self.db, self.fixture.key, applications.stamp()))
        self.assertEqual(len(self.steps()), 2)

    def test_once_the_user_has_signed_in_there_themselves_no_reset_is_planned_and_the_tool_is_refused(self):   # review A2
        import user_step
        for press in ("button", "live view"):
            self.setUp()
            self.first()
            # The reset step did not get in: the card. The user signs in on the site themselves.
            self.step(lambda proxy: self.tool(proxy, "fill_saved_sign_in", **self.FILL), {"outcome": "SIGN_IN_REFUSED", "sign_up_submitted": False})
            self.assertEqual(self.wait(), "SIGN_UP_UNCONFIRMED")
            with self.db:   # a build before `reset_offered`: only the user's own sign-in stands between the record and a reset
                for row in self.db.execute("SELECT id, detail FROM application_events WHERE event=?", (access_step.FINISHED_EVENT,)).fetchall():
                    self.db.execute("UPDATE application_events SET detail=? WHERE id=?",
                                    (json.dumps({k: v for k, v in json.loads(row["detail"]).items() if k != "reset_offered"}), row["id"]))
            self.assertEqual((access_step.own_account(self.db, SITE), access_step.reset_offered(self.db, SITE)), (True, False))
            if press == "button":
                with mock.patch.object(access_cards.user_step, "available", return_value=True), \
                        mock.patch.object(access_cards.user_step, "openable", return_value=True):
                    self.assertEqual(access_cards.done(self.db, self.fixture.key, applications.stamp())["tone"], "success")
            else:
                self.assertEqual(access_cards.step_finished(self.db, self.fixture.key, user_step.SIGN_IN, applications.stamp()), [self.fixture.key])
            self.assertEqual((access_step.own_account(self.db, SITE), access_step.sign_up_allowed(self.db, SITE)), (False, False), press)
            self.assertTrue(access_step.user_has_signed_in(self.db, SITE))
            # The wall comes back (the site signed the browser out): one step as after any user action, with
            # no reset tool and no sign-up; it does not get in: the card, and no reset step is planned.
            self.assertEqual(self.meet_wall()[2], access_step.PLANNED)
            said = []
            summary, code, verdict = self.step(lambda proxy: said.append(self.tool(proxy, "type_reset_password", password_targets=["e2"])),
                                               {"outcome": "SIGN_IN_REFUSED", "sign_up_submitted": False})
            self.assertEqual(self.tools, ["fill_saved_sign_in", "open_confirmation_link"], press)
            self.assertIn("not available in this session", said[0])
            request = json.loads((self.directory / "request.json").read_text())
            self.assertEqual((request["access"]["own_account_unconfirmed"], request["access"]["may_create_account"]), (False, False))
            self.assertEqual((verdict, self.wait(), self.steps()[-1][1]), ("WAITING", "SIGN_IN", "DONE"), press)

    def test_never_for_an_account_the_site_says_exists_or_refused(self):
        for report, reason in (({"outcome": "ACCOUNT_EXISTS", "sign_up_submitted": True, "after_sign_up": "ACCOUNT_EXISTS_SHOWN"}, "ACCOUNT_EXISTS"),
                               ({"outcome": "SIGN_UP_REFUSED", "sign_up_submitted": True, "after_sign_up": "REFUSAL_SHOWN"}, "SIGN_UP_FAILED")):
            self.setUp()
            self.meet_wall()
            summary, code, verdict = self.step(self.sign_up, report)
            self.assertEqual((verdict, self.wait(), len(self.steps())), ("WAITING", reason, 1))

    def test_with_no_user_action_at_most_five_step_sessions_on_one_site_and_then_the_card(self):   # the new bound
        self.meet_wall()
        pending = {"outcome": "EMAIL_PENDING", "sign_up_submitted": True, "after_sign_up": "CONFIRMATION_ASKED"}
        nothing = {"outcome": "SIGN_IN_REFUSED", "sign_up_submitted": False}
        script = [(self.sign_up, dict(nothing, sign_up_submitted=True, after_sign_up="NOTHING_SHOWN")),   # the sign-up
                  (lambda proxy: self.tool(proxy, "type_reset_password", password_targets=["e2"]),     # the reset, its second look
                   {"outcome": "EMAIL_PENDING", "email_for": "PASSWORD_RESET", "sign_up_submitted": False}),
                  (None, dict(nothing, after_reset="REFUSAL_SHOWN")),
                  (self.sign_up, pending), (None, nothing)] + [(None, nothing)] * 10                    # the fresh sign-up, its second look
        ran = 0
        for play, report in script:
            with self.db:
                self.db.execute("UPDATE %s SET not_before='2000-01-01T00:00:00+00:00' WHERE state='DUE'" % access_step.TABLE)
            if self.wait():
                break
            if not access_step.due(self.db, self.fixture.key, SITE, applications.stamp()):
                self.meet_wall()
                continue
            self.step(play, report)
            ran += 1
        self.assertEqual((ran, [row[0] for row in self.steps()]), (5, ["ACCESS", "ACCESS", "CONFIRM", "ACCESS", "CONFIRM"]))
        self.assertEqual((self.job()["state"], bool(self.wait())), ("BLOCKED", True))
        self.assertEqual(sum(1 for value in access_step._records(self.db, SITE) if value.get("password_typed") is True), 2)   # never a third sign-up
        # A sign-up that waits for its email: the second look is itself a later step with the reset permitted,
        # so no further reset step is planned after it (two sessions, then the card).
        self.setUp()
        self.meet_wall()
        self.step(self.sign_up, pending)
        with self.db:
            self.db.execute("UPDATE %s SET not_before='2000-01-01T00:00:00+00:00' WHERE state='DUE'" % access_step.TABLE)
        summary, code, verdict = self.step(None, nothing)
        self.assertIn("type_reset_password", self.tools)
        self.assertEqual((verdict, self.wait(), len(self.steps())), ("WAITING", "SIGN_UP_UNCONFIRMED", 2))


class SignedOutAgain(Case):
    """Mock case 8 (real Codex, 2026-11-13): the step made the account and ended signed in, the sign-in was
    saved, and the next read met the sign-in wall again (the site had signed the browser out). That is a new
    wall, and Sidedoor holds the way in: a sign-in step, not a card; bounded, so a site that signs out on
    every visit ends on the card."""

    CREATED = {"outcome": "ACCOUNT_CREATED", "account_created": True, "sign_up_submitted": True, "after_sign_up": "SIGNED_IN"}

    def created(self):
        self.meet_wall()

        def play(proxy):
            said = self.sign_up(proxy)
            self.page.sign_in()
            return said
        summary, code, verdict = self.step(play, dict(self.CREATED))
        self.assertEqual((verdict, self.saved()), (access_step.PLANNED, [(SITE, EMAIL, "SIDEDOOR", True)]))
        self.page.cookies = []                                                 # the site signs the browser out

    def sign_in(self):
        def play(proxy):
            said = self.tool(proxy, "fill_saved_sign_in", email_target="e1", password_target="e2")
            self.page.sign_in()
            return said
        result = self.step(play, {"outcome": "SIGNED_IN", "sign_up_submitted": False})
        self.assertEqual((self.said[0], self.tools), (browser_guard.SAVED_SIGN_IN_TEXT["filled"], ["fill_saved_sign_in", "open_confirmation_link"]))
        self.page.cookies = []
        return result

    def test_a_wall_after_the_step_got_in_is_a_sign_in_step_twice_and_the_third_wall_in_a_row_is_the_card(self):
        self.created()
        for walls in (1, 2):
            summary, code, verdict = self.meet_wall()                          # the next read is walled again
            self.assertEqual((verdict, self.wait(), self.card(), self.job()["state"], self.steps()[-1]),
                             (access_step.PLANNED, None, None, "RETRY_WAIT", ("ACCESS", "DUE", None)), walls)
            self.assertEqual(self.sign_in()[2], access_step.PLANNED)
        summary, code, verdict = self.meet_wall()                              # a site that signs out on every visit
        self.assertEqual((verdict, self.wait(), self.job()["state"], len(self.steps())), ("WAITING", "SIGN_IN", "BLOCKED", 3))
        self.assertEqual(self.card()["need"], "The site needs you to sign in.")
        self.assertEqual(self.meet_wall()[2], "WAITING")                       # and nothing more by itself
        self.assertEqual(len(self.steps()), 3)

    def test_the_profile_step_is_still_planned_after_a_sign_in_step(self):
        self.created()
        self.meet_wall()
        self.sign_in()
        self.page.sign_in()
        summary, code, verdict = self.meet_wall(wall=PROFILE_WALL)             # signed in now; the site's profile first
        self.assertEqual((verdict, self.wait(), self.steps()[-1]), (access_step.PLANNED, None, ("PROFILE", "DUE", None)))

    def test_a_sign_in_step_that_does_not_get_in_is_the_card(self):
        self.created()
        self.meet_wall()
        summary, code, verdict = self.step(lambda proxy: self.tool(proxy, "fill_saved_sign_in", email_target="e1", password_target="e2"),
                                           {"outcome": "SIGN_IN_REFUSED", "sign_up_submitted": False})
        self.assertEqual((verdict, self.wait(), len(self.steps())), ("WAITING", "SIGN_IN", 2))
        self.assertEqual(self.meet_wall()[2], "WAITING")
        self.assertEqual(len(self.steps()), 2)

    def test_no_sign_in_step_where_sidedoor_holds_no_way_in(self):
        self.meet_wall()
        self.step(None, {"outcome": "SIGNED_IN", "sign_up_submitted": False})  # got in with nothing saved (an emailed link, say)
        summary, code, verdict = self.meet_wall()
        self.assertEqual((verdict, self.wait(), len(self.steps())), ("WAITING", "SIGN_IN", 1))


class ResetByRecord(Reset):
    """The real run's state: the old code threw the pending password away, so only the step's own record says
    Sidedoor sent that sign-up. The record is enough; what the AI says in the current session never is."""

    def real_run(self, **detail):
        """A finished step on the site whose guard typed a new-account password, with nothing pending."""
        self.meet_wall()
        self.step(self.sign_up, {"outcome": "SIGN_UP_REFUSED", "account_created": None, "sign_up_submitted": True, "after_sign_up": "NOTHING_SHOWN"})
        saved_sign_ins.drop_pending(SITE)                                      # as the old code did, or as age does
        with self.db:   # the record as that build wrote it: SIGN_UP_REFUSED, no after_sign_up, no sign_up_submitted
            row = self.db.execute("SELECT id, detail FROM application_events WHERE event=? ORDER BY id DESC", (access_step.FINISHED_EVENT,)).fetchone()
            old = {k: v for k, v in json.loads(row["detail"]).items() if k not in ("after_sign_up", "sign_up_submitted", "reset_password_typed")}
            self.db.execute("UPDATE application_events SET detail=? WHERE id=?", (json.dumps(dict(dict(old, outcome="SIGN_UP_REFUSED"), **detail)), row["id"]))
            self.db.execute("UPDATE %s SET outcome='SIGN_UP_REFUSED' WHERE state='DONE'" % access_step.TABLE)
        # The record permits the reset: its step is planned by itself (the user did nothing; had they signed in
        # themselves, the site would no longer be resettable: AutomaticReset's test of that).
        self.assertEqual((self.card(), self.steps()[-1]), (None, ("ACCESS", "DUE", None)))
        self.page.typed.clear()
        patch = mock.patch.object(browser_guard, "generate_password", lambda *rules: RESET)
        patch.start()
        self.addCleanup(patch.stop)

    def reset_then_sign_in(self, proxy):
        typed = self.tool(proxy, "type_reset_password", password_targets=["e2", "e3"])
        again = self.tool(proxy, "type_reset_password", password_targets=["e2"])
        self.page.typed.clear()
        said = self.tool(proxy, "fill_saved_sign_in", email_target="e1", password_target="e2")
        self.page.sign_in()
        return typed, again, said

    def test_the_step_record_permits_the_reset_when_nothing_is_pending_and_no_second_sign_up_is_offered(self):
        self.real_run()
        self.assertFalse(saved_sign_ins.pending(SITE))
        summary, code, verdict = self.step(self.reset_then_sign_in, {"outcome": "SIGNED_IN", "email_for": "PASSWORD_RESET", "sign_up_submitted": False})
        request = json.loads((self.directory / "request.json").read_text())
        self.assertEqual((request["access"]["own_account_unconfirmed"], request["access"]["may_create_account"]), (True, False))
        self.assertNotIn("type_new_account_password", self.tools)              # never a second sign-up for that account
        self.assertEqual(self.said[0], (browser_guard.RESET_TEXT["filled"], browser_guard.RESET_TEXT["already"],
                                        browser_guard.SAVED_SIGN_IN_TEXT["filled"]))
        self.assertEqual(self.page.typed, {"e1": EMAIL, "e2": RESET})
        self.assertEqual((verdict, self.saved(), saved_sign_ins.for_fill(SITE)), (access_step.PLANNED, [(SITE, EMAIL, "SIDEDOOR", True)], (EMAIL, RESET)))

    def test_nothing_typed_leaves_nothing_pending(self):
        self.real_run()

        def play(proxy):
            self.page.url = "https://login.other-site.test/reset"
            return self.tool(proxy, "type_reset_password", password_targets=["e2"])
        self.step(play, {"outcome": "SIGN_IN_REFUSED", "sign_up_submitted": False})
        self.assertEqual((self.said, self.page.typed, saved_sign_ins.pending(SITE)), ([browser_guard.RESET_TEXT["wrong_site"]], {}, False))

    def test_refused_with_neither_a_pending_password_nor_the_record_whatever_the_ai_reports(self):
        # No step ever typed a password here; the AI claims it did, in this very session.
        self.meet_wall()
        self.step(lambda proxy: self.tool(proxy, "type_reset_password", password_targets=["e2"]),
                  {"outcome": "SIGNED_IN", "sign_up_submitted": True, "account_created": True, "after_sign_up": "SIGNED_IN"})
        self.assertIn("not available in this session", self.said[0])
        self.assertEqual((self.page.typed, saved_sign_ins.pending(SITE), self.saved()), ({}, False, []))
        self.assertFalse(json.loads((self.directory / "request.json").read_text())["access"]["own_account_unconfirmed"])
        # A record that says the site itself showed an account already existed, or showed its refusal: not Sidedoor's account.
        for detail in ({"outcome": "ACCOUNT_EXISTS"}, {"after_sign_up": "REFUSAL_SHOWN"}, {"sign_up_submitted": False}, {"password_typed": False}):
            self.setUp()
            self.real_run(**detail)
            self.step(lambda proxy: self.tool(proxy, "type_reset_password", password_targets=["e2"]), {"outcome": "SIGN_IN_REFUSED", "sign_up_submitted": False})
            self.assertIn("not available in this session", self.said[0], detail)
            self.assertEqual((self.page.typed, saved_sign_ins.pending(SITE)), ({}, False), detail)

    def test_refused_when_a_confirmed_saved_sign_in_exists_even_with_the_record(self):
        self.real_run()
        saved_sign_ins.add(SITE, EMAIL, "the-confirmed-password", saved_sign_ins.SIDEDOOR)
        self.step(lambda proxy: self.tool(proxy, "type_reset_password", password_targets=["e2"]), {"outcome": "SIGN_IN_REFUSED", "sign_up_submitted": False})
        self.assertFalse(json.loads((self.directory / "request.json").read_text())["access"]["own_account_unconfirmed"])
        self.assertIn("not available in this session", self.said[0])
        self.assertEqual(self.page.typed, {})
        self.assertEqual(saved_sign_ins.for_fill(SITE), (EMAIL, "the-confirmed-password"))   # never replaced by a reset


PROFILE_WALL = dict(WALL, sign_in_offered=False, sign_up_offered=False, signed_in_as=EMAIL, profile_required=True)


class Profile(Case):
    """Owner, 2026-11-14: "AI need to fill it out, if the user has the setting turned on". Signed in on a new
    account, the site wants its own profile before the application can be reached."""

    def test_the_read_reports_it_as_a_value_and_the_profile_step_completes_it_from_the_users_own_data(self):
        # Sidedoor got in (an access step that got through), and the next read meets the site's profile.
        self.meet_wall()
        self.step(lambda proxy: self.page.sign_in(), {"outcome": "SIGNED_IN", "sign_up_submitted": False})
        summary, code, verdict = self.meet_wall(wall=PROFILE_WALL)
        self.assertIn("profile_required (true when the candidate", self.read_prompt)   # the read's own report, not a made-up code
        self.assertIn("never with a code of your own", self.read_prompt)
        self.assertEqual((code, verdict, self.wait()), ("AUTHENTICATION_REQUIRED", access_step.PLANNED, None))
        self.assertEqual(self.steps(), [("ACCESS", "DONE", "SIGNED_IN"), ("PROFILE", "DUE", None)])   # beside the access step

        # The user's own details and résumé, as Setup stores them (the product's own profile file).
        (self.fixture.root / "resume.txt").write_text("Alex Example. Staff engineer, eight years of distributed systems.")
        policy = dict(self.fixture.policy, resume_raw={"path": "resume.pdf", "text_path": "resume.txt", "file_name": "Alex Example CV.pdf"})
        policy["answers"] = dict(policy["answers"], full_name={"semantic": "full_name", "value": "Alex Example", "source": "setup"},
                                 phone={"semantic": "phone", "value": "+1 206 555 0100", "source": "setup"},
                                 linkedin={"semantic": "linkedin", "value": "https://www.linkedin.com/in/alex-example", "source": "setup"})
        user_profile.use_policy(policy, self.home)

        def play(proxy):
            self.held = sorted(proxy.offered())
            asked = json.loads((self.directory / "request.json").read_text())
            staged = Path(asked["candidate"]["resume_file"])   # while the session runs: the guard's read-only upload folder
            self.during = (staged.read_bytes(), str(staged.parent) == proxy.uploads, staged.parent.name, oct(staged.stat().st_mode & 0o777))
        summary, code, verdict = self.step(play, {"outcome": "PROFILE_COMPLETED", "needs": [], "profile_saved": True})
        # The step's REAL request, built by the product (`consume` -> `access_step.run`): every fact the prompt
        # promises is in it (review round 1, 5), so the AI is never told to use something it was not given.
        request = json.loads((self.directory / "request.json").read_text())
        self.assertEqual(request["access"], {"kind": "PROFILE", "site": SITE})
        answers = request["context"]["policy"]["answers"]
        self.assertEqual({key: answers[key]["value"] for key in ("email", "full_name", "phone", "linkedin")},
                         {"email": EMAIL, "full_name": "Alex Example", "phone": "+1 206 555 0100",
                          "linkedin": "https://www.linkedin.com/in/alex-example"})
        self.assertEqual(request["candidate"]["resume"], "Alex Example. Staff engineer, eight years of distributed systems.")
        self.assertEqual(self.during, (b"Synthetic resume, never uploaded", True, "uploads", "0o400"))
        self.assertTrue(request["candidate"]["resume_file"].endswith("Alex Example CV.pdf"))    # the name the user uploaded it under
        for promised in ("request.context.policy.answers", "request.candidate.resume", "request.candidate.resume_file"):
            self.assertIn(promised, self.prompts[-1])
        self.assertFalse((self.directory / "uploads").exists())                # unstaged when the session ended (review, 4)
        self.assertIn("browser_file_upload", self.held)
        # No password tool of any kind and no link tool (review round 1, 2): the page tools only.
        self.assertEqual(self.tools, [])
        for key in (saved_sign_ins.CONFIG_KEY, saved_sign_ins.ACCESS_CONFIG_KEY, saved_sign_ins.CREATE_CONFIG_KEY, saved_sign_ins.RESET_CONFIG_KEY):
            self.assertNotIn(key, self.configs[-1])
        self.assertEqual(self.configs[-1][site_sessions.CONFIG_KEY], SITE)
        self.assertNotIn("arm", self.held)                                     # nothing can be submitted from here
        prompt = self.prompts[-1]
        for line in ("Never invent a fact about the candidate.", "is not guessed, not estimated",
                     "The profile is saved once.", "Never press Apply and never submit an application.",
                     "A file is uploaded only from request.candidate.resume_file."):
            self.assertIn(line, prompt)
        self.assertEqual((verdict, self.wait(), self.steps()[-1]), (access_step.PLANNED, None, ("PROFILE", "DONE", "PROFILE_COMPLETED")))
        self.assertEqual(self.job()["state"], "RETRY_WAIT")
        # The site still wants its profile on the next read: the user's turn, no second profile step.
        summary, code, verdict = self.meet_wall(wall=PROFILE_WALL)
        self.assertEqual((verdict, self.wait(), len(self.steps())), ("WAITING", "PROFILE_UNCONFIRMED", 2))   # never "fill it in yourself"

    NEEDS = [{"label": "Do you need visa sponsorship?", "kind": "yes_no", "choices": ["ignored"]},
             {"label": "  Preferred\ncompany size ", "kind": "choice", "choices": ["1-10", "11-50", "51+", 7, "11-50"]},
             {"label": "Years of React experience", "kind": "telepathy"},
             {"label": "Do you need visa sponsorship?", "kind": "text"}, "a bare label", {"kind": "text"}]

    def questions(self):
        import approvals_page
        return self.db.execute("SELECT id,field,label,answer_type,choices,state,payload_hash,required FROM user_decisions "
                               "WHERE job_key=? ORDER BY rowid", (self.fixture.key,)).fetchall()

    def test_a_required_field_the_users_info_does_not_cover_is_a_question_for_them_never_a_card(self):   # owner, 2026-11-14: "a"
        import approvals_page
        import datetime
        import decision_transport
        self.meet_wall(wall=PROFILE_WALL)
        summary, code, verdict = self.step(None, {"outcome": "PROFILE_NEEDS_USER", "needs": self.NEEDS, "profile_saved": True})
        self.assertEqual((verdict, self.wait(), self.card()), (access_step.ASKED, None, None))     # asked, no card
        asked = self.questions()
        # One question per field, under the site's own label, with the field's own control (structured values).
        self.assertEqual([(row["label"], row["answer_type"], json.loads(row["choices"]), row["state"], row["required"]) for row in asked],
                         [("Do you need visa sponsorship?", "boolean", [], "PENDING", 1),
                          ("Preferred company size", "choice", ["1-10", "11-50", "51+"], "PENDING", 1),
                          ("Years of React experience", "text", [], "PENDING", 1)])
        self.assertTrue(all(row["field"].startswith("site_profile:") for row in asked))
        job = self.fixture.job()
        self.assertEqual((job["state"], job["blocker"]), ("BLOCKED", "UNKNOWN_REQUIRED_ANSWERS:" + ",".join(row["field"] for row in asked)))
        # It is on Approvals' "Questions for you", as an application's question is.
        import datetime
        shown = json.dumps(approvals_page.questions(self.db, datetime.datetime.now(datetime.timezone.utc)), default=str)
        for row in asked:
            self.assertIn(row["label"], shown)
        # No card and no sentence asks the user to complete the profile.
        for gone in ("PROFILE_NEEDS_YOU",):
            self.assertNotIn(gone, access_outcomes.WAIT_REASONS)
        for sentence in access_outcomes.WAIT_REASONS.values():
            self.assertNotIn("details only you can give", sentence)
        # Each card names the site its words came from, beside the job, and the site's text is escaped.
        items = approvals_page.questions(self.db, datetime.datetime.now(datetime.timezone.utc))
        self.assertEqual({item["site"] for item in items}, {SITE})
        from fastapi.templating import Jinja2Templates
        import dashboard
        hostile = dict(items[1], question='<script>alert(1)</script> "Size"', options=['<img src=x onerror=1>', "11-50"])
        drawn = Jinja2Templates(directory=dashboard.ASSETS / "templates").get_template("approvals_question.html").render(item=hostile, v=mock.Mock(query=""))
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", drawn)
        self.assertNotIn("<script>alert(1)", drawn)
        self.assertNotIn("<img src=x", drawn)
        self.assertIn("· %s</span>" % SITE, drawn)
        # Answered one of three, two of three: the hold stands (no read is spent to hold it again). No step yet.
        answer = lambda row, raw: decision_transport.answer_locally(self.db, row["id"], raw, shown_payload_hash=row["payload_hash"])["state"]
        held_until = self.fixture.job()["next_at"]
        self.assertEqual(answer(asked[0], "no"), "APPLIED")
        self.assertEqual((self.fixture.job()["next_at"], self.fixture.job()["state"]), (held_until, "BLOCKED"))   # not due
        self.assertEqual(len(self.questions()), 3)
        # All answered: the job resumes through the applications run itself (`applications.process`, as an
        # answered application question resumes it): it reads, meets the profile again, and one more profile
        # step is planned and given the answers, under the site's labels.
        self.assertEqual(answer(asked[1], "11-50"), "APPLIED")
        self.assertEqual(self.fixture.job()["next_at"], held_until)            # two of three: still held
        self.assertEqual(answer(asked[2], "6"), "APPLIED")
        self.assertIsNone(self.fixture.job()["next_at"])                       # the last answer makes it due
        calls = []

        class Reads:
            can_submit = False

            def call(inner, operation, payload):
                calls.append(operation)
                return {"blocked": {"code": "AUTHENTICATION_REQUIRED", "next_action": "sign in", "wall": PROFILE_WALL}}
        applications.process(self.db, self.fixture.job(), Reads(), self.fixture.policy, self.fixture.root, applications.stamp(),
                             config=self.fixture.config)
        self.assertEqual((calls, self.steps()[-1], self.wait(), self.fixture.job()["state"]),
                         (["inspect"], ("PROFILE", "DUE", None), None, "RETRY_WAIT"))
        summary, code, verdict = self.step(None, {"outcome": "PROFILE_COMPLETED", "needs": [], "profile_saved": True})
        request = json.loads((self.directory / "request.json").read_text())
        self.assertEqual({item["label"]: item["value"] for item in request["candidate"]["answers"]},
                         {"Do you need visa sponsorship?": False, "Preferred company size": "11-50", "Years of React experience": "6"})
        self.assertIn("request.candidate.answers", self.prompts[-1])
        self.assertEqual((verdict, self.steps()[-1]), (access_step.PLANNED, ("PROFILE", "DONE", "PROFILE_COMPLETED")))

    def other_jobs(self, count):
        with self.db:
            return [applications.ingest(self.db, {"company": "Synthetic Company", "role": "Engineer %d" % number,
                                                  "url": "https://jobs.ashbyhq.com/synthetic/role-%d" % number,
                                                  "requisition_id": "role-%d" % number}, applications.stamp()) for number in range(count)]

    def row(self, key):
        return dict(self.db.execute("SELECT * FROM application_jobs WHERE job_key=?", (key,)).fetchone())

    def test_five_jobs_on_one_site_one_step_one_set_of_questions_and_all_due_after_the_finishing_step(self):   # review round 1, 1
        import decision_transport
        others = self.other_jobs(4)
        self.meet_wall(wall=PROFILE_WALL)                                      # job 1's read meets the profile: its step is planned
        held = lambda: [access_outcomes.wait_on_host(self.db, self.row(key), applications.stamp()) for key in others]
        with self.db:
            self.assertEqual(held(), [True] * 4)                               # the other four wait on the site: no session, no step
        self.step(None, {"outcome": "PROFILE_NEEDS_USER", "needs": self.NEEDS[:2], "profile_saved": True})
        self.assertEqual((len(self.steps()), len(self.questions()), len(self.configs)), (1, 2, 1))   # one step, one set of questions
        with self.db:
            access_outcomes.expire(self.db, applications.stamp())               # a round passes: the questions are open, the site is held
        self.assertEqual([self.row(key)["state"] for key in others], ["BLOCKED"] * 4)
        self.assertEqual({row[0] for row in self.db.execute("SELECT job_key FROM user_decisions")}, {self.fixture.key})
        # A held job needs nothing from the user: no card on Approvals or Overview, not in the Needs-you count;
        # Applications says "Waiting" with the one true line. The asking job still shows its questions.
        import applications_page
        import datetime
        import approvals_page
        reader = overview_page.Reader(self.db)

        def held_view(key):
            view = applications_page.gather(self.db, {"timezone": "America/Los_Angeles"}, self.home)
            job = next(item for item in view["list"]["jobs"] if item["key"] == key)
            return job["state"], job["todo"], job["problem"], job["needs_you"], job["group"]
        self.assertEqual((self.card(), access_cards.card_count(reader), overview_page.waiting_keys(reader)), (None, 0, []))
        self.assertEqual(overview_page.needs_you(reader, datetime.datetime.now(datetime.timezone.utc))["sign_ins"], 0)
        self.assertEqual(held_view(others[0]), ("Waiting", "Waiting for your answers on Approvals.", None, False, "progress"))
        self.assertEqual(len(approvals_page.questions(self.db, datetime.datetime.now(datetime.timezone.utc))), 2)
        for row in self.questions():
            decision_transport.answer_locally(self.db, row["id"], "Yes" if row["answer_type"] == "boolean" else "11-50", shown_payload_hash=row["payload_hash"])
        with self.db:
            access_outcomes.expire(self.db, applications.stamp())
        self.assertEqual([self.row(key)["state"] for key in others], ["BLOCKED"] * 4)   # still held while the finishing step is to run
        self.assertEqual(held_view(others[0])[:3], ("Waiting", "Waiting for another application on this site.", None))   # no question is open now
        self.assertEqual((self.card(), access_cards.card_count(overview_page.Reader(self.db))), (None, 0))
        self.step(None, {"outcome": "PROFILE_COMPLETED", "needs": [], "profile_saved": True})
        with self.db:
            access_outcomes.expire(self.db, applications.stamp())               # the applications run's own sweep
        self.assertEqual([self.row(key)["state"] for key in others], ["RETRY_WAIT"] * 4)   # all due
        self.assertEqual(self.row(others[0])["next_action"], access_outcomes.HOST_RELEASED_WORDS)
        self.assertEqual((len(self.steps()), len(self.questions())), (2, 2))
        # A later job on the same site that still meets the profile is given the site's answers and asks nothing.
        with self.db:
            verdict = applications.hold_access_block(self.db, self.row(others[0]), "AUTHENTICATION_REQUIRED", "x", applications.stamp(),
                                                     self.fixture.config, {"blocked": {"code": "AUTHENTICATION_REQUIRED", "wall": PROFILE_WALL}})
        self.assertEqual(verdict, access_step.PLANNED)
        self.assertEqual(sorted(item["label"] for item in access_step.profile_answers(self.db, SITE)),
                         ["Do you need visa sponsorship?", "Preferred company size"])
        # Never another site's: the same label on another site is another field.
        self.assertNotEqual(access_step.clean_needs(self.NEEDS[:1], SITE)[0]["field"], access_step.clean_needs(self.NEEDS[:1], "other.example")[0]["field"])
        self.assertEqual(access_step.profile_answers(self.db, "other.example"), [])

    def asked_and_answered(self):
        """A profile step asked two questions for the site and the user answered both on Approvals."""
        import decision_transport
        self.meet_wall(wall=PROFILE_WALL)
        self.step(None, {"outcome": "PROFILE_NEEDS_USER", "needs": self.NEEDS[:2], "profile_saved": True})
        for row in self.questions():
            decision_transport.answer_locally(self.db, row["id"], "Yes" if row["answer_type"] == "boolean" else "11-50", shown_payload_hash=row["payload_hash"])

    def looks(self, config, directory):
        """What every real session does first: one look at the page through the guard (so it reached the page)."""
        proxy = self.guard(config, directory)
        proxy.handle({"jsonrpc": "2.0", "id": 0, "method": "tools/call", "params": {"name": "browser_snapshot", "arguments": {}}})
        proxy.audit.flush()
        return proxy

    def request_for(self, key, session):
        """One read request of another job on the site, consumed, then what the applications run does with it."""
        with self.db:
            self.db.execute("UPDATE desktop_requests SET state='CONSUMED' WHERE state='DONE'")
        try:
            self.fixture.adapter.call("inspect", {"job": dict(self.row(key), candidate_email=EMAIL)})
        except applications.AdapterPending as pending:
            request_id = pending.request_id
        application_browser.consume(self.fixture.root, self.db, self.fixture.config, request_id, session)
        result = json.loads(self.db.execute("SELECT result FROM desktop_requests WHERE request_id=?", (request_id,)).fetchone()[0])
        code = applications.stop_code(result)
        if code in applications.ACCESS_BLOCK_CODES:
            with self.db:
                return applications.hold_access_block(self.db, self.row(key), code, "next", applications.stamp(), self.fixture.config, result)
        return code

    def test_answers_are_used_once_for_the_site_whatever_became_of_the_asking_job(self):   # the real run, 2026-11-14
        [other] = self.other_jobs(1)
        self.asked_and_answered()
        # The asking job's next read judged the posting out of scope: it is closed, and never revived.
        with self.db:
            applications.hold(self.db, self.fixture.job(), "OUTSIDE_CONFIRMED_SCOPE", "closed", applications.stamp(), "INELIGIBLE")
        ran = []

        def finishing(config, prompt, directory, timeout):
            ran.append(json.loads((directory / "request.json").read_text()))
            self.configs.append(config)
            self.looks(config, directory)
            write_output(directory, request_at(directory), {"access": {"outcome": "PROFILE_COMPLETED", "needs": [], "profile_saved": True}})
        # Another job on the site gets its request: the site's finishing step rides on it, once, with the answers.
        verdict = self.request_for(other, finishing)
        self.assertEqual(len(ran), 1)
        self.assertEqual((ran[0]["access"], sorted(item["label"] for item in ran[0]["candidate"]["answers"])),
                         ({"kind": "PROFILE", "site": SITE}, ["Do you need visa sponsorship?", "Preferred company size"]))
        self.assertEqual(ran[0]["payload"]["job"]["job_key"], other)           # carried by the job whose request it is
        self.assertNotIn("_application_guard_claim", self.configs[-1])         # nothing can be submitted from it
        self.assertEqual((verdict, self.row(other)["state"]), (access_step.PLANNED, "RETRY_WAIT"))     # that job goes on to its read
        self.assertEqual((self.fixture.job()["state"], access_step.open_ask(self.db, SITE)), ("INELIGIBLE", None))   # asker stays closed
        # Once: the next request of that job is an ordinary read; no second finishing step.
        reads = []
        self.assertIsNone(self.request_for(other, lambda config, prompt, directory, timeout: (
            reads.append(config), write_output(directory, request_at(directory), self.fixture.review()))))
        self.assertEqual((len(reads), len(ran), [step[0] for step in self.steps()]), (1, 1, ["PROFILE", "PROFILE"]))

    def test_a_lent_step_is_given_what_a_job_planned_one_is_and_one_that_never_acts_strands_nothing(self):   # the real run
        import decision_transport
        [other, third, fourth] = self.other_jobs(3)
        # A job-planned profile step, for comparison: its real request and guard command.
        self.meet_wall(wall=PROFILE_WALL)
        self.step(None, {"outcome": "PROFILE_NEEDS_USER", "needs": self.NEEDS[:2], "profile_saved": True})
        planned = {"request": json.loads((self.directory / "request.json").read_text()),
                   "command": browser_runtime.guard_command(self.configs[-1], self.directory, "inspect"), "tools": self.held_tools()}
        for row in self.questions():
            decision_transport.answer_locally(self.db, row["id"], "Yes" if row["answer_type"] == "boolean" else "11-50", shown_payload_hash=row["payload_hash"])
        with self.db:
            applications.hold(self.db, self.fixture.job(), "OUTSIDE_CONFIRMED_SCOPE", "closed", applications.stamp(), "INELIGIBLE")
        lent = {}

        def idle(config, prompt, directory, timeout):   # the real run: the model answered at once, without one tool call
            lent.update(request=json.loads((directory / "request.json").read_text()), prompt=prompt,
                        command=browser_runtime.guard_command(config, directory, "inspect"))
            proxy = self.guard(config, directory)
            lent["tools"] = sorted(proxy.offered())
            proxy.audit.flush()
            write_output(directory, request_at(directory), {"access": {"outcome": "PROFILE_NOT_SAVED", "needs": [], "profile_saved": False}})
        verdict = self.request_for(other, idle)
        # The lent step's real request and guard command: the same tools, start address and facts, plus the answers.
        flags = lambda command: sorted(part for part in command if part.startswith("--") and part not in ("--audit", "--uploads"))
        self.assertEqual((flags(lent["command"]), lent["tools"]), (flags(planned["command"]), planned["tools"]))
        self.assertIn("--no-page-code", lent["command"])
        self.assertIn("--site-session", lent["command"])
        self.assertEqual(sorted(lent["request"]), sorted(planned["request"]))
        self.assertEqual((lent["request"]["access"], site_sessions.site_key(lent["request"]["payload"]["url"])), ({"kind": "PROFILE", "site": SITE}, SITE))
        self.assertEqual(sorted(lent["request"]["payload"]), ["job", "url"])    # not the read's own flags (read_only, operation)
        self.assertEqual(sorted(planned["request"]["payload"]), ["job", "url"])
        self.assertEqual(lent["request"]["context"]["policy"]["answers"]["email"]["value"], EMAIL)
        self.assertEqual(sorted(lent["request"]["candidate"]), sorted(planned["request"]["candidate"]))
        self.assertTrue(Path(lent["request"]["candidate"]["resume_file"]).name)
        self.assertEqual(sorted(item["label"] for item in lent["request"]["candidate"]["answers"]),
                         ["Do you need visa sponsorship?", "Preferred company size"])
        self.assertIn("the place to start from", lent["prompt"])
        # (a) it never acted on the page: the site's round stays open, the answers are not stranded.
        self.assertEqual((self.steps()[-1], access_step.open_ask(self.db, SITE)["state"]), (("PROFILE", "DONE", "NOT_RUN"), "OPEN"))
        # (b), (c) no card, nothing untrue: the job that lent its read is due again for its own read.
        self.assertEqual((verdict, access_outcomes.open_wait(self.db, other), self.row(other)["state"], self.row(other)["blocker"]),
                         (access_step.PLANNED, None, "RETRY_WAIT", None))
        self.assertEqual(self.card(), None)
        reads = []
        own = lambda config, prompt, directory, timeout: (reads.append(directory.name), write_output(directory, request_at(directory), self.fixture.review()))
        self.assertIsNone(self.request_for(other, own))                        # its next request is its own ordinary read
        self.assertEqual((len(reads), reads[0] != "access"), (1, True))
        # Another job's read may carry it; a session that acts finishes the round. At most two idle ones per round.
        self.assertEqual(self.request_for(third, idle), access_step.PLANNED)
        self.assertEqual((self.steps()[-1], access_step.open_ask(self.db, SITE)["state"]), (("PROFILE", "DONE", "NOT_RUN"), "OPEN"))
        self.assertIsNone(access_step.site_due(self.db, fourth, SITE, applications.stamp(), operation="inspect"))   # the bound: no third
        self.assertEqual(len(self.steps()), 3)

    def held_tools(self):
        directory = Path(tempfile.mkdtemp(prefix="access-step-tools-"))
        self.addCleanup(shutil.rmtree, directory, True)
        return sorted(self.guard(self.configs[-1], directory).offered())

    def test_a_job_planned_profile_step_that_never_acts_runs_once_more_and_closes_nothing(self):
        self.meet_wall(wall=PROFILE_WALL)
        report = {"outcome": "PROFILE_NOT_SAVED", "needs": [], "profile_saved": False}
        summary, code, verdict = self.step(None, report, reach=False)          # no call to the page at all
        self.assertEqual((verdict, self.wait(), self.steps()), (access_step.PLANNED, None, [("PROFILE", "DUE", None)]))   # not run: due again
        summary, code, verdict = self.step(None, {"outcome": "PROFILE_COMPLETED", "needs": [], "profile_saved": True})    # this one acts
        self.assertEqual((verdict, self.steps()), (access_step.PLANNED, [("PROFILE", "DONE", "PROFILE_COMPLETED")]))

    def test_answers_are_used_even_when_no_read_reports_the_profile_as_required_any_more(self):
        self.asked_and_answered()
        ran = []
        # The asking job itself comes due: its request is spent on the finishing step before any read.
        summary, code, verdict = self.step(lambda proxy: ran.append(1), {"outcome": "PROFILE_COMPLETED", "needs": [], "profile_saved": True})
        self.assertEqual((ran, code, verdict, self.steps()[-1]), ([1], "AUTHENTICATION_REQUIRED", access_step.PLANNED, ("PROFILE", "DONE", "PROFILE_COMPLETED")))
        self.assertEqual(json.loads((self.directory / "request.json").read_text())["access"]["kind"], "PROFILE")
        # While a question is still open, or with nothing answered, no step rides on anything.
        self.setUp()
        self.meet_wall(wall=PROFILE_WALL)
        self.step(None, {"outcome": "PROFILE_NEEDS_USER", "needs": self.NEEDS[:2], "profile_saved": True})
        self.assertIsNone(access_step.site_due(self.db, self.fixture.key, SITE, applications.stamp(), operation="inspect"))
        # A finishing step killed mid-session ends the round: it is never run again on another job.
        self.setUp()
        [other] = self.other_jobs(1)
        self.asked_and_answered()
        self.assertEqual(access_step.site_due(self.db, other, SITE, applications.stamp(), operation="inspect")["kind"], "PROFILE")
        with self.db:
            self.db.execute("UPDATE %s SET state='RUNNING' WHERE state='DUE'" % access_step.TABLE)
        self.request_for(other, lambda *arguments: self.fail("a killed step is never run again"))
        self.assertEqual((self.steps()[-1], access_step.open_ask(self.db, SITE), access_step.site_due(self.db, self.fixture.key, SITE, applications.stamp(), operation="inspect")),
                         (("PROFILE", "DONE", "PROFILE_UNCONFIRMED"), None, None))

    def test_only_a_read_of_a_job_with_nothing_uncertain_carries_the_sites_step(self):   # confirming review
        [other] = self.other_jobs(1)
        self.asked_and_answered()
        due = lambda operation, key=None: access_step.site_due(self.db, key or other, SITE, applications.stamp(), operation=operation)
        steps = lambda: len(self.steps())
        # A submit, a verify, a reconcile request is never lent: nothing is planned, the request runs as ever.
        for operation in ("submit", "verify", "reconcile", "investigate", None):
            self.assertIsNone(due(operation), operation)
        self.assertEqual(steps(), 1)
        # Through `consume` itself: a verify request on the site runs its own session, not a profile step.
        ran = []
        with self.db:
            self.db.execute("UPDATE desktop_requests SET state='CONSUMED' WHERE state='DONE'")
        try:
            self.fixture.adapter.call("verify", {"job": dict(self.row(other), candidate_email=EMAIL)})
        except applications.AdapterPending as pending:
            request_id = pending.request_id

        def verify(config, prompt, directory, timeout):
            ran.append((config.get("_application_operation"), saved_sign_ins.ACCESS_CONFIG_KEY in config, (directory / "access").exists()))
            raise RuntimeError("the verify session's own business")
        application_browser.consume(self.fixture.root, self.db, self.fixture.config, request_id, verify)
        self.assertEqual((ran, steps()), ([("verify", False, False)], 1))
        # A job whose submission may have reached the employer lends nothing, even its read.
        with mock.patch.object(applications, "uncertain_submission", side_effect=lambda database, key: key == other):
            self.assertIsNone(due("inspect"))
        with self.db:   # an approved application, one waiting for approval, a submit in flight: the same
            self.db.execute("UPDATE application_jobs SET state='BLOCKED',blocker=? WHERE job_key=?", (applications.APPROVED_CODE if hasattr(applications, "APPROVED_CODE") else applications.APPLICATION_APPROVED, other))
        self.assertIsNone(due("inspect"))
        with self.db:
            self.db.execute("UPDATE application_jobs SET state='SUBMITTING',blocker=NULL WHERE job_key=?", (other,))
        self.assertIsNone(due("inspect"))
        self.assertEqual(steps(), 1)
        # An ordinary read is lent.
        with self.db:
            self.db.execute("UPDATE application_jobs SET state='QUEUED',blocker=NULL WHERE job_key=?", (other,))
        self.assertEqual((due("inspect")["kind"], steps()), ("PROFILE", 2))
        # That job then finishes another way before its request: the step, never run, is released to another read.
        with self.db:
            applications.hold(self.db, self.row(other), "OUTSIDE_CONFIRMED_SCOPE", "closed", applications.stamp(), "INELIGIBLE")
        [third] = [applications.ingest(self.db, {"company": "Synthetic Company", "role": "Third", "url": "https://jobs.ashbyhq.com/synthetic/third",
                                                 "requisition_id": "third"}, applications.stamp())]
        self.db.commit()
        carried = due("inspect", third)
        self.assertEqual((carried["job_key"], carried["kind"], self.steps()[1:]), (third, "PROFILE", [("PROFILE", "DONE", "NOT_RUN"), ("PROFILE", "DUE", None)]))
        # The read cap settling a profile step closes the round too: nothing is planned again.
        with self.db:
            access_step.settle_unrun(self.db, third, applications.stamp())
        self.assertEqual((access_step.open_ask(self.db, SITE), due("inspect", self.fixture.key)), (None, None))

    def answers_in_for_the_site(self):
        """The site's questions were asked by ANOTHER job and are all answered; the fixture's own job is fresh."""
        [asker] = self.other_jobs(1)
        self.asked_and_answered()
        with self.db:
            for table in (access_step.ASKS, access_step.TABLE, "application_answers", "user_decisions", "desktop_requests"):
                self.db.execute("UPDATE %s SET job_key=? WHERE job_key=?" % table, (asker, self.fixture.key))
            self.db.execute("UPDATE application_jobs SET state='QUEUED',blocker=NULL,next_action=NULL,next_at=NULL WHERE job_key=?", (self.fixture.key,))
            self.db.execute("DELETE FROM application_blockers WHERE job_key=?", (self.fixture.key,))
            self.db.execute("UPDATE application_jobs SET state='INELIGIBLE' WHERE job_key=?", (asker,))   # as in the real run
        self.assertEqual(access_step.site_due(self.db, self.fixture.key, SITE, applications.stamp(), operation="reconcile"), None)
        return asker

    def consumed(self, request_id):
        """One request through `application_browser.consume` (the real entry): what the session it started was."""
        ran = []

        def session(config, prompt, directory, timeout):
            ran.append({"operation": config.get("_application_operation"), "claim": "_application_guard_claim" in config,
                        "step_folder": directory.name == "access", "step_keys": saved_sign_ins.ACCESS_CONFIG_KEY in config,
                        "profile_prompt": "profile step" in prompt})
            raise RuntimeError("this session's own outcome is not what is tested")
        application_browser.consume(self.fixture.root, self.db, self.fixture.config, request_id, session)
        return ran

    def pending(self, operation, key=None):
        with self.db:
            self.db.execute("UPDATE desktop_requests SET state='CONSUMED' WHERE state='DONE'")
        try:
            self.fixture.adapter.call(operation, {"job": dict(self.row(key or self.fixture.key), candidate_email=EMAIL)})
        except applications.AdapterPending as pending:
            return pending.request_id
        self.fail("no %s request was queued" % operation)

    def test_through_consume_a_submit_and_a_reconcile_request_are_never_lent_and_run_as_ever(self):   # before the merge
        self.answers_in_for_the_site()
        before = self.steps()
        plain = {"claim": False, "step_folder": False, "step_keys": False, "profile_prompt": False}
        # (b) a RECONCILE and a VERIFY request of a job on the site: each runs its own session; no profile step.
        for operation in ("reconcile", "verify"):
            self.assertEqual(self.consumed(self.pending(operation)), [dict(plain, operation=operation)], operation)
            self.assertEqual(self.steps(), before)
        # (a) a SUBMIT request: the submit session runs, with its arm claim; no profile step.
        submit = self.fixture.prepare_submit()
        self.assertEqual(self.consumed(submit), [dict(plain, operation="submit", claim=True)])
        self.assertEqual(self.steps(), before)
        # (c) that job now has a submit in flight. Main's own claim never hands such a job's read to a session
        # (`desktop_adapter.claim`), so nothing can be lent; and the lending rule refuses it by itself too.
        self.assertTrue(applications.uncertain_submission(self.db, self.fixture.key))
        self.assertEqual((self.consumed(self.pending("inspect")), access_step.lendable(self.db, self.fixture.key, "inspect")), ([], False))
        self.assertEqual(self.steps(), before)
        self.assertEqual(access_step.open_ask(self.db, SITE)["state"], "OPEN")   # the site's step still waits for a read

    def test_through_consume_a_job_held_for_a_confirming_read_lends_nothing_and_an_ordinary_read_does(self):
        self.answers_in_for_the_site()
        before = self.steps()
        plain = {"claim": False, "step_folder": False, "step_keys": False, "profile_prompt": False}
        # (d) a job held for a confirming read: its read is not lent.
        [waiting] = [applications.ingest(self.db, {"company": "Synthetic Company", "role": "Fourth", "url": "https://jobs.ashbyhq.com/synthetic/fourth",
                                                   "requisition_id": "fourth"}, applications.stamp())]
        self.db.commit()
        import confirm_read
        with mock.patch.object(confirm_read, "wait", side_effect=lambda database, key: {"id": 1} if key == waiting else None):
            self.assertEqual(self.consumed(self.pending("inspect", waiting)), [dict(plain, operation="inspect")])
        self.assertEqual(self.steps(), before)
        # The control: the same job's ordinary read IS lent (the profile step's session starts).
        self.assertEqual(self.consumed(self.pending("inspect", waiting)),
                         [{"operation": "inspect", "claim": False, "step_folder": True, "step_keys": False, "profile_prompt": True}])

    def test_through_consume_a_jobs_own_planned_step_is_spent_only_by_a_read(self):
        self.meet_wall()                                                       # the job's own access step is planned
        self.assertEqual(self.steps(), [("ACCESS", "DUE", None)])
        with self.db:
            self.db.execute("UPDATE application_jobs SET state='QUEUED',blocker=NULL,next_action=NULL,next_at=NULL WHERE job_key=?", (self.fixture.key,))
        plain = {"claim": False, "step_folder": False, "step_keys": False, "profile_prompt": False}
        for operation in ("reconcile", "verify"):                              # each runs its own session, not the step
            self.assertEqual(self.consumed(self.pending(operation)), [dict(plain, operation=operation)], operation)
            self.assertEqual(self.steps(), [("ACCESS", "DUE", None)])
        submit = self.fixture.prepare_submit()
        self.assertEqual(self.consumed(submit), [dict(plain, operation="submit", claim=True)])     # the submit, not the step
        self.assertEqual(self.steps(), [("ACCESS", "DUE", None)])              # still to run, on a read

    def test_a_job_held_behind_its_site_says_what_holds_the_site(self):
        import applications_page
        [other] = self.other_jobs(1)
        for value, wall, line in (("ASK", WALL, "Waiting for your sign-in on this site."),
                                  ("ASK", PROFILE_WALL, "Waiting for this site's profile.")):
            self.setUp()
            [other] = self.other_jobs(1)
            with self.db:
                access_outcomes.set_account_setting(self.db, value, applications.stamp())
            self.meet_wall(wall=wall)                                          # a card holds the site
            with self.db:
                self.assertTrue(access_outcomes.wait_on_host(self.db, self.row(other), applications.stamp()))
            view = applications_page.gather(self.db, {"timezone": "America/Los_Angeles"}, self.home)
            todo = next(item for item in view["list"]["jobs"] if item["key"] == other)["todo"]
            self.assertEqual(todo, line)
            self.assertEqual(self.row(other)["next_action"], line)
            for untrue in ("Sidedoor tries this one", "Another application on this site is waiting for you to sign in"):
                self.assertNotIn(untrue, json.dumps(view, default=str))

    def test_at_most_two_rounds_of_questions_a_third_need_is_the_card(self):   # review round 1, 3
        import decision_transport
        self.meet_wall(wall=PROFILE_WALL)
        labels = iter(["First thing", "Second thing", "Third thing"])
        for round_number in (1, 2):
            summary, code, verdict = self.step(None, {"outcome": "PROFILE_NEEDS_USER", "needs": [{"label": next(labels), "kind": "text"}]})
            self.assertEqual(verdict, access_step.ASKED, round_number)
            row = self.questions()[-1]
            decision_transport.answer_locally(self.db, row["id"], "an answer", shown_payload_hash=row["payload_hash"])   # its next request finishes
        summary, code, verdict = self.step(None, {"outcome": "PROFILE_NEEDS_USER", "needs": [{"label": next(labels), "kind": "text"}]})
        self.assertEqual((verdict, self.wait(), len(self.questions())), ("WAITING", "PROFILE_REQUIRED", 2))   # no third round
        card = self.card()
        self.assertEqual(card["need"], "This site asks for a profile before you can apply. Fill it in on the site.")
        self.assertEqual((card["step"]["label"], card["done_label"]), ("Open the site", "I've done it"))

    def test_an_expired_question_is_closed_and_asked_again_as_an_applications_question_is(self):   # review round 1, 4
        self.meet_wall(wall=PROFILE_WALL)
        self.step(None, {"outcome": "PROFILE_NEEDS_USER", "needs": self.NEEDS[:1], "profile_saved": False})
        with self.db:
            self.db.execute("UPDATE user_decisions SET expires_at=1")           # its seven days passed unanswered
        self.assertEqual(self.meet_wall(wall=PROFILE_WALL)[2], access_step.ASKED)       # the job came due at the deadline
        self.assertEqual([(row["state"], row["label"]) for row in self.questions()],
                         [("EXPIRED", "Do you need visa sponsorship?"), ("PENDING", "Do you need visa sponsorship?")])
        self.assertEqual((len(self.steps()), self.db.execute("SELECT COUNT(*) FROM %s" % access_step.ASKS).fetchone()[0]), (1, 1))   # the same round
        self.assertIn("Answer the site's questions on Approvals", self.fixture.job()["next_action"])

    def test_nothing_is_invented_while_the_answer_is_absent(self):
        self.meet_wall(wall=PROFILE_WALL)
        self.step(None, {"outcome": "PROFILE_NEEDS_USER", "needs": self.NEEDS[:1], "profile_saved": False})
        # Unanswered: no step runs, however often the job comes round; the request of no session carries a value for it.
        for _ in range(3):
            self.assertEqual(self.meet_wall(wall=PROFILE_WALL)[2], access_step.ASKED)
        self.assertEqual((len(self.steps()), len(self.configs), access_step.profile_answers(self.db, self.fixture.key)), (1, 1, []))
        for line in ("it is left empty and reported", "is not guessed, not estimated", "save nothing"):
            self.assertIn(line, self.prompts[-1])
        # A finishing step that reports the SAME field again, although it was answered, is not asked again and
        # does not loop: the outcome nobody knows.
        import decision_transport
        row = self.questions()[0]
        decision_transport.answer_locally(self.db, row["id"], "yes", shown_payload_hash=row["payload_hash"])
        summary, code, verdict = self.step(None, {"outcome": "PROFILE_NEEDS_USER", "needs": self.NEEDS[:1], "profile_saved": False})
        self.assertEqual((verdict, self.wait(), len(self.questions())), ("WAITING", "PROFILE_UNCONFIRMED", 1))
        # A report that names nothing askable is the same card, never a guess.
        self.setUp()
        self.meet_wall(wall=PROFILE_WALL)
        summary, code, verdict = self.step(None, {"outcome": "PROFILE_NEEDS_USER", "needs": ["everything"], "profile_saved": False})
        self.assertEqual((verdict, self.wait(), len(self.questions())), ("WAITING", "PROFILE_UNCONFIRMED", 0))

    def test_a_profile_session_that_dies_after_it_reached_the_page_is_a_card_and_is_never_run_again(self):
        self.meet_wall(wall=PROFILE_WALL)

        def play(proxy):
            proxy.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "browser_click", "arguments": {"target": "e7"}}})
        summary, code, verdict = self.step(play, None, fail=TimeoutError("timed out saving the profile"))
        self.assertEqual((self.steps(), verdict, self.wait()), ([("PROFILE", "DONE", "PROFILE_UNCONFIRMED")], "WAITING", "PROFILE_UNCONFIRMED"))
        self.assertEqual(self.card()["need"], "Your profile on this site may or may not be complete. Check it on the site.")
        self.assertEqual(len(self.configs), 1)
        # A report that is not one of the outcomes (a code the AI made up) is the same card, never a guess.
        self.setUp()
        self.meet_wall(wall=PROFILE_WALL)
        self.step(None, {"outcome": "PROFILE_INCOMPLETE", "needs": "everything"})
        self.assertEqual((self.steps()[-1][2], self.wait()), ("PROFILE_UNCONFIRMED", "PROFILE_UNCONFIRMED"))
        # Died before it reached the page: an ordinary failure, the step still due.
        self.setUp()
        self.meet_wall(wall=PROFILE_WALL)
        summary, code, verdict = self.step(None, None, fail=RuntimeError("the engine never started"))
        self.assertEqual((summary["state"], self.steps(), self.wait()), ("FAILED", [("PROFILE", "DUE", None)], None))

    def test_a_step_left_running_by_a_killed_app_is_settled_with_no_session_and_never_run_again(self):   # review round 1, 1
        def killed(kind, wall, before=None):
            """The app was killed mid-session: no `finally` ran, the step's row still says RUNNING."""
            self.setUp()
            if before:
                before()
            self.meet_wall(wall=wall)
            with self.db:
                self.db.execute("UPDATE %s SET state='RUNNING',kind=? WHERE state='DUE'" % access_step.TABLE, (kind,))
            ran = []
            summary, code, verdict = self.step(lambda proxy: ran.append(1), {"outcome": "SIGNED_IN", "sign_up_submitted": False})
            return ran, self.configs, self.steps()[-1], verdict, self.wait(), code
        # A profile step: a page of the profile may have been saved.
        self.assertEqual(killed("PROFILE", PROFILE_WALL), ([], [], ("PROFILE", "DONE", "PROFILE_UNCONFIRMED"), "WAITING", "PROFILE_UNCONFIRMED", "AUTHENTICATION_REQUIRED"))
        self.assertEqual(self.card()["need"], "Your profile on this site may or may not be complete. Check it on the site.")
        # An access step, its second look, a resend: a sign-up or a reset may have been sent.
        for kind in ("ACCESS", "CONFIRM", "RESEND"):
            self.assertEqual(killed(kind, WALL), ([], [], (kind, "DONE", "SIGN_UP_UNCONFIRMED"), "WAITING", "SIGN_UP_UNCONFIRMED", "AUTHENTICATION_REQUIRED"), kind)
        self.assertEqual(self.card()["need"], "Your account on this site may or may not exist. Sign in once, or reset the password on the site.")
        # A killed session that had typed a new password: counted as the site's one sign-up, never a second.
        ran, configs, step, verdict, wait, code = killed("ACCESS", WALL, before=lambda: saved_sign_ins.pend(SITE, EMAIL, MARKER))
        self.assertEqual((step, saved_sign_ins.pending(SITE), access_step.sign_up_allowed(self.db, SITE)), (("ACCESS", "DONE", "SIGN_UP_UNCONFIRMED"), True, False))
        # The read cap reached with a profile step still RUNNING: unconfirmed, not "not saved" (review, 3).
        self.setUp()
        self.meet_wall(wall=PROFILE_WALL)
        with self.db:
            self.assertEqual(access_step.settle_unrun(self.db, self.fixture.key, applications.stamp()), "AUTHENTICATION_REQUIRED")
            self.assertEqual(self.steps()[-1], ("PROFILE", "DONE", "PROFILE_NOT_SAVED"))   # never started: nothing was saved
        self.setUp()
        self.meet_wall(wall=PROFILE_WALL)
        with self.db:
            self.db.execute("UPDATE %s SET state='RUNNING'" % access_step.TABLE)
            access_step.settle_unrun(self.db, self.fixture.key, applications.stamp())
        self.assertEqual(self.steps()[-1], ("PROFILE", "DONE", "PROFILE_UNCONFIRMED"))

    def test_a_step_session_has_no_in_page_code_tool_and_a_session_the_starter_ended_says_so(self):   # the real run, 2026-11-14
        # The starter's live check ends ANY application session at browser_evaluate (application_browser.tool_event);
        # the real profile step used it 31 seconds in and was ended with no report. A step is never offered it.
        for wall in (PROFILE_WALL, WALL):
            self.setUp()
            self.meet_wall(wall=wall)

            def play(proxy):
                self.held = sorted(proxy.offered())
                return self.tool(proxy, "browser_evaluate", function="() => 1")
            self.step(play, {"outcome": "HUMAN_CHECK"})
            self.assertNotIn("browser_evaluate", self.held)
            self.assertIn("browser_fill_form", self.held)                      # the page tools are all there
            self.assertIn("not available in this session", self.said[0])
            self.assertIn("in-page code tool: browser_evaluate is not", self.prompts[-1])        # told as a fact
            item = lambda tool: {"type": "mcp_tool_call", "id": "i", "server": "browser", "tool": tool, "arguments": {}, "status": "completed",
                                 "result": {"content": [{"type": "text", "text": "x"}], "isError": False}}
            for tool in self.held:                                             # every tool the step is offered passes the live check
                self.assertTrue(application_browser.tool_item(item(tool), False, False, live=True), tool)
        # Ended by the starter after it reached the page: the same card, and the record and the audit say who ended it.
        self.setUp()
        self.meet_wall(wall=PROFILE_WALL)

        def play(proxy):
            proxy.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "browser_fill_form", "arguments": {"fields": []}}})
        self.step(play, None, fail=ValueError("APPLICATION_UNSUPPORTED_BROWSER_CODE"))
        self.assertEqual((self.steps()[-1], self.wait()), (("PROFILE", "DONE", "PROFILE_UNCONFIRMED"), "PROFILE_UNCONFIRMED"))
        detail = json.loads(self.db.execute("SELECT detail FROM %s" % access_step.TABLE).fetchone()[0])
        event = json.loads(self.db.execute("SELECT detail FROM application_events WHERE event=?", (access_step.FINISHED_EVENT,)).fetchone()[0])
        audit = [json.loads(line) for line in (self.directory / "guard.jsonl").read_text().splitlines()]
        self.assertEqual((detail["ended_by"], event["ended_by"]), ("APPLICATION_UNSUPPORTED_BROWSER_CODE", "APPLICATION_UNSUPPORTED_BROWSER_CODE"))
        self.assertEqual([row["code"] for row in audit if row["decision"] == "SESSION_ENDED_BY_STARTER"], ["APPLICATION_UNSUPPORTED_BROWSER_CODE"])
        # A crash is not the starter's doing: nothing claims it was.
        self.setUp()
        self.meet_wall(wall=PROFILE_WALL)
        self.step(play, None, fail=RuntimeError("the engine process died"))
        event = json.loads(self.db.execute("SELECT detail FROM application_events WHERE event=?", (access_step.FINISHED_EVENT,)).fetchone()[0])
        self.assertEqual((self.steps()[-1][2], event["ended_by"]), ("PROFILE_UNCONFIRMED", None))
        self.assertNotIn("SESSION_ENDED_BY_STARTER", (self.directory / "guard.jsonl").read_text())

    def test_a_human_check_and_a_lost_sign_in_in_the_profile_step_go_to_their_own_cards(self):
        for outcome, wall, reason in (("HUMAN_CHECK", "CAPTCHA_REQUIRED", "HUMAN_CHECK"), ("NOT_SIGNED_IN", "AUTHENTICATION_REQUIRED", "SIGN_IN"),
                                      ("PROFILE_NOT_SAVED", "AUTHENTICATION_REQUIRED", "PROFILE_UNCONFIRMED")):
            self.setUp()
            self.meet_wall(wall=PROFILE_WALL)
            summary, code, verdict = self.step(None, {"outcome": outcome, "needs": [], "profile_saved": False})
            self.assertEqual((code, verdict, self.wait()), (wall, "WAITING", reason), outcome)


class ProfileWithoutTheSetting(Case):
    setting = None

    def test_under_ask_or_skip_the_user_gets_the_card_and_no_step(self):
        for value in (None, "ASK", "SKIP"):
            self.setUp()
            if value:
                with self.db:
                    access_outcomes.set_account_setting(self.db, value, applications.stamp())
            summary, code, verdict = self.meet_wall(wall=PROFILE_WALL)
            self.assertEqual((verdict, self.wait(), self.steps(), self.fixture.job()["state"]), ("WAITING", "PROFILE_REQUIRED", [], "BLOCKED"), value)
            card = self.card()
            self.assertEqual(card["need"], "This site asks for a profile before you can apply. Fill it in on the site.")
            self.assertEqual((card["step"]["label"], card["done_label"], card["abandon"], card["hint"]),
                             ("Open the site", "I've done it", True, None))
        self.assertEqual(access_outcomes.clean_description({"profile_required": "yes"})["profile_required"], None)   # a value, never prose


class ResetRefused(Reset):
    """Owner, 2026-11-14 ("a+b"): the site refused the password reset, so no usable account is behind Sidedoor's
    record: ONE fresh sign-up, then the user's card; never a third sign-up, never the reset again."""

    REFUSED = {"outcome": "SIGN_UP_NOT_TRIED", "sign_up_submitted": False, "after_reset": "REFUSAL_SHOWN"}

    def refused(self):
        """Sidedoor's sign-up record, a later step that cannot sign in, and the site refusing the reset."""
        self.unconfirmed()
        return self.step(lambda proxy: self.tool(proxy, "fill_saved_sign_in", email_target="e1", password_target="e2"), dict(self.REFUSED))

    def test_a_refused_reset_gets_one_fresh_sign_up_not_the_reset_again(self):
        summary, code, verdict = self.refused()
        self.assertEqual((verdict, self.wait(), self.steps()[-2:]), (access_step.PLANNED, None, [("ACCESS", "DONE", "RESET_REFUSED"), ("ACCESS", "DUE", None)]))
        self.assertEqual((saved_sign_ins.pending(SITE), access_step.own_account(self.db, SITE)), (False, False))   # nothing usable behind that record
        self.page.typed.clear()

        def play(proxy):
            reset = self.tool(proxy, "type_reset_password", password_targets=["e2"])        # not offered again
            typed = self.tool(proxy, "type_new_account_password", password_targets=["e2", "e3"])
            self.page.sign_in()
            return reset, typed
        summary, code, verdict = self.step(play, {"outcome": "ACCOUNT_CREATED", "account_created": True, "sign_up_submitted": True, "after_sign_up": "SIGNED_IN"})
        request = json.loads((self.directory / "request.json").read_text())
        self.assertEqual((request["access"]["may_create_account"], request["access"]["own_account_unconfirmed"]), (True, False))
        self.assertEqual(self.tools, ["fill_saved_sign_in", "open_confirmation_link", "type_new_account_password"])
        self.assertIn("not available in this session", self.said[0][0])
        self.assertEqual(self.said[0][1], browser_guard.CREATE_TEXT["filled"])
        self.assertEqual(self.page.typed, {"e2": RESET, "e3": RESET})          # a newly made password (the test's second one)
        self.assertEqual((verdict, self.saved(), saved_sign_ins.for_fill(SITE)), (access_step.PLANNED, [(SITE, EMAIL, "SIDEDOOR", True)], (EMAIL, RESET)))

    def test_a_fresh_sign_up_that_meets_account_exists_is_the_users_card_and_nothing_further_is_offered(self):
        self.refused()
        summary, code, verdict = self.step(self.sign_up, {"outcome": "ACCOUNT_EXISTS", "account_created": None, "sign_up_submitted": True,
                                                          "after_sign_up": "ACCOUNT_EXISTS_SHOWN"})
        self.assertEqual((verdict, self.wait(), saved_sign_ins.pending(SITE)), ("WAITING", "ACCOUNT_EXISTS", False))
        card = self.card()
        self.assertEqual((card["step"]["label"], card["done_label"], card["abandon"]), ("Sign in", "I've signed in", True))
        # The user presses I've signed in and the wall is still there: a step that can only look, nothing to send.
        with mock.patch.object(access_cards.user_step, "available", return_value=True), \
                mock.patch.object(access_cards.user_step, "openable", return_value=True):
            access_cards.done(self.db, self.fixture.key, applications.stamp())
        self.meet_wall()
        self.page.typed.clear()

        def play(proxy):   # a third attempt, and the reset: neither tool is held, whatever the AI asks
            return (self.tool(proxy, "type_new_account_password", password_targets=["e2"]),
                    self.tool(proxy, "type_reset_password", password_targets=["e2"]))
        self.step(play, {"outcome": "SIGN_UP_NOT_TRIED", "sign_up_submitted": True, "account_created": True})
        request = json.loads((self.directory / "request.json").read_text())
        self.assertEqual((request["access"]["may_create_account"], request["access"]["own_account_unconfirmed"]), (False, False))
        self.assertEqual(self.tools, ["fill_saved_sign_in", "open_confirmation_link"])
        for said in self.said[0]:
            self.assertIn("not available in this session", said)
        self.assertEqual(self.page.typed, {})
        self.assertEqual(sum(1 for step in self.steps() if step[0] == "ACCESS"), 4)

    def test_a_fresh_sign_up_that_still_does_not_get_in_is_the_card_the_user_can_act_on(self):
        self.refused()
        summary, code, verdict = self.step(self.sign_up, {"outcome": "SIGN_UP_NOT_TRIED", "account_created": None, "sign_up_submitted": True,
                                                          "after_sign_up": "NOTHING_SHOWN"})
        self.assertEqual((verdict, self.wait()), ("WAITING", "SIGN_UP_UNCONFIRMED"))
        card = self.card()
        self.assertEqual(card["need"], "Your account on this site may or may not exist. Sign in once, or reset the password on the site.")
        self.assertEqual((card["step"]["label"], card["done_label"], card["abandon"]), ("Sign in", "I've signed in", True))
        self.assertEqual((access_step.own_account(self.db, SITE), access_step.sign_up_allowed(self.db, SITE)), (False, False))   # no reset, no third

    def test_a_refused_reset_without_sidedoors_own_record_changes_nothing(self):
        self.meet_wall()   # no sign-up of Sidedoor's on this site: the AI's report alone opens nothing
        summary, code, verdict = self.step(None, dict(self.REFUSED))
        self.assertEqual((verdict, self.wait(), self.steps()), ("WAITING", "ASK_EACH_TIME", [("ACCESS", "DONE", "SIGN_UP_NOT_TRIED")]))
        self.assertTrue(access_step.sign_up_allowed(self.db, SITE))            # its first sign-up is still its only one

    def test_one_sign_up_per_record_a_second_only_after_a_refused_reset(self):
        self.unconfirmed()                                                     # one sign-up sent, outcome unknown
        self.assertEqual((access_step.sign_up_allowed(self.db, SITE), access_step.own_account(self.db, SITE)), (False, True))
        saved_sign_ins.drop_pending(SITE)                                      # even with nothing pending: no second sign-up
        self.step(None, {"outcome": "SIGN_IN_REFUSED", "sign_up_submitted": False})
        self.assertFalse(json.loads((self.directory / "request.json").read_text())["access"]["may_create_account"])
        self.assertNotIn("type_new_account_password", self.tools)


class Unknown(Case):
    def test_a_session_that_dies_after_the_password_was_typed_keeps_it_pending_and_never_signs_up_again(self):
        self.meet_wall()
        summary, code, verdict = self.step(self.sign_up, None, fail=RuntimeError("the page hung after the button"))
        self.assertEqual((summary["state"], code, verdict), ("COMPLETE", "AUTHENTICATION_REQUIRED", "WAITING"))
        self.assertEqual(self.steps(), [("ACCESS", "DONE", "SIGN_UP_UNCONFIRMED")])
        self.assertEqual(self.wait(), "SIGN_UP_UNCONFIRMED")
        card = self.card()
        self.assertEqual(card["need"], "Your account on this site may or may not exist. Sign in once, "
                                       "or reset the password on the site.")
        self.assertEqual(card["step"]["label"], "Sign in")
        # Not a saved sign-in (it may not exist), not lost either (it may).
        self.assertEqual((self.saved(), saved_sign_ins.has(SITE), saved_sign_ins.pending(SITE)), ([], False, True))
        self.assertEqual(saved_sign_ins_page.view()["rows"], [])
        # Something changes (the user presses I've signed in): the next wall gets a step that can only sign in.
        with mock.patch.object(access_cards.user_step, "available", return_value=True), \
                mock.patch.object(access_cards.user_step, "openable", return_value=True):
            self.assertEqual(access_cards.done(self.db, self.fixture.key, applications.stamp())["tone"], "success")
        self.assertEqual(self.meet_wall()[2], access_step.PLANNED)

        def play(proxy):
            refused = self.tool(proxy, "type_new_account_password", password_targets=["e2"])   # not held: no second sign-up
            said = self.tool(proxy, "fill_saved_sign_in", email_target="e1", password_target="e2")
            self.page.sign_in()
            return refused, said
        self.page.typed.clear()
        summary, code, verdict = self.step(play, {"outcome": "SIGNED_IN", "account_created": None, "sign_up_submitted": False})
        self.assertNotIn(saved_sign_ins.CREATE_CONFIG_KEY, self.configs[-1])
        self.assertNotIn("type_new_account_password", self.tools)
        self.assertIn("not available in this session", self.said[0][0])
        self.assertEqual(self.said[0][1], browser_guard.SAVED_SIGN_IN_TEXT["filled"])
        request = json.loads((self.directory / "request.json").read_text())
        self.assertEqual((request["access"]["saved_sign_in"], request["access"]["may_create_account"]), (True, False))
        self.assertEqual(self.page.typed, {"e1": EMAIL, "e2": MARKER})        # the same password: the account, if any, is the one made
        self.assertEqual((verdict, self.saved(), saved_sign_ins.pending(SITE)), (access_step.PLANNED, [(SITE, EMAIL, "SIDEDOOR", True)], False))

    def test_a_guard_given_the_tool_anyway_makes_no_second_password_and_types_once(self):
        saved_sign_ins.pend(SITE, EMAIL, "the-earlier-pending-one")
        directory = Path(tempfile.mkdtemp(prefix="access-step-guard-"))
        self.addCleanup(shutil.rmtree, directory, True)
        config = {saved_sign_ins.CONFIG_KEY: SITE, saved_sign_ins.CREATE_CONFIG_KEY: EMAIL, saved_sign_ins.ACCESS_CONFIG_KEY: True}
        proxy = self.guard(config, directory)
        self.assertEqual(self.tool(proxy, "type_new_account_password", password_targets=["e2"]), browser_guard.CREATE_TEXT["exists"])
        self.assertEqual((self.page.typed, saved_sign_ins.for_fill(SITE)), ({}, (EMAIL, "the-earlier-pending-one")))
        saved_sign_ins.drop_pending(SITE)
        # Nothing typed (a wrong field, a missing one, another site): nothing is kept, and it may be tried again.
        for arguments, outcome in (({"password_targets": ["e1"]}, "not_password_field"), ({"password_targets": ["e9"]}, "not_found"),
                                   ({"password_targets": []}, "malformed"), ({"password_targets": "e2"}, "malformed")):
            self.assertEqual(self.tool(proxy, "type_new_account_password", **arguments), browser_guard.CREATE_TEXT[outcome])
            self.assertEqual((self.page.typed, saved_sign_ins.pending(SITE)), ({}, False))
        self.page.url = "https://login.other-site.test/sign-up"
        self.assertEqual(self.tool(proxy, "type_new_account_password", password_targets=["e2"]), browser_guard.CREATE_TEXT["wrong_site"])
        self.assertEqual((self.page.typed, saved_sign_ins.pending(SITE)), ({}, False))
        self.page.url = URL
        # Typed once; the second call types nothing, so a second sign-up cannot be filled in.
        self.assertEqual(self.tool(proxy, "type_new_account_password", password_targets=["e2", "e3"]), browser_guard.CREATE_TEXT["filled"])
        self.page.typed.clear()
        self.assertEqual(self.tool(proxy, "type_new_account_password", password_targets=["e2"]), browser_guard.CREATE_TEXT["already"])
        self.assertEqual((self.page.typed, saved_sign_ins.pending(SITE)), ({}, True))
        rows = [json.loads(line) for line in (directory / "guard.jsonl").read_text().splitlines()]
        self.assertEqual([row["outcome"] for row in rows if row["decision"] == "ACCOUNT_PASSWORD"],
                         ["exists", "not_password_field", "not_found", "malformed", "malformed", "wrong_site", "filled", "already"])
        self.assertNotIn("walrus", (directory / "guard.jsonl").read_text())

    def test_a_session_that_dies_after_it_began_on_the_page_is_one_card_and_is_never_run_again(self):   # review round 1, 1
        self.meet_wall()

        def play(proxy):   # a sign-up with no password (an emailed link): filled and sent with the page tools alone
            proxy.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "browser_click", "arguments": {"target": "e7"}}})
        summary, code, verdict = self.step(play, None, fail=TimeoutError("timed out waiting for the email"))
        self.assertEqual((summary["state"], code, verdict), ("COMPLETE", "AUTHENTICATION_REQUIRED", "WAITING"))
        self.assertEqual(self.steps(), [("ACCESS", "DONE", "SIGN_UP_UNCONFIRMED")])    # settled, not due again
        self.assertEqual((self.wait(), self.page.typed), ("SIGN_UP_UNCONFIRMED", {}))
        self.assertEqual(self.card()["need"], "Your account on this site may or may not exist. Sign in once, or reset the password on the site.")
        self.assertIsNone(access_step.due(self.db, self.fixture.key, SITE, "9999"))
        self.assertEqual(len(self.configs), 1)                                 # one session, no second run that may create

    def test_a_session_the_guard_stopped_is_a_failed_one_whatever_it_answered(self):   # parity with the ordinary session
        self.meet_wall()

        def play(proxy):
            said = self.sign_up(proxy)
            self.page.sign_in()
            (self.directory / browser_runtime.GUARD_STOPPED).write_text(json.dumps({"reason": "SETUP_STOPPED"}))
            return said
        summary, code, verdict = self.step(play, {"outcome": "ACCOUNT_CREATED", "account_created": True, "sign_up_submitted": True})
        self.assertEqual(self.page.typed, {"e2": MARKER, "e3": MARKER})        # the control: it did reach the page
        self.assertEqual(self.steps(), [("ACCESS", "DONE", "SIGN_UP_UNCONFIRMED")])    # not the success it answered
        self.assertEqual((verdict, self.wait()), ("WAITING", "SIGN_UP_UNCONFIRMED"))
        self.assertEqual((self.saved(), saved_sign_ins.pending(SITE), site_sessions.has(SITE)), ([], True, False))   # nothing promoted
        # Stopped before it ever reached the page: an ordinary failure, the step still due.
        self.setUp()
        self.meet_wall()
        stop = lambda proxy: (self.directory / browser_runtime.GUARD_STOPPED).write_text(json.dumps({"reason": "SETUP_STOPPED"}))
        summary, code, verdict = self.step(stop, {"outcome": "SIGNED_IN", "sign_up_submitted": False}, reach=False)
        self.assertEqual((summary["state"], self.steps(), self.wait()), ("FAILED", [("ACCESS", "DUE", None)], None))

    def test_a_session_that_dies_before_any_password_is_typed_is_an_ordinary_failure_and_the_step_stays_due(self):
        self.meet_wall()
        summary, code, verdict = self.step(None, None, fail=RuntimeError("the engine never started"))
        self.assertEqual((summary["state"], summary["code"]), ("FAILED", "APPLICATION_BROWSER_FAILED"))
        self.assertEqual((self.steps(), self.wait(), saved_sign_ins.pending(SITE)), ([("ACCESS", "DUE", None)], None, False))

    def test_a_report_that_is_not_one_of_the_outcomes_is_a_card_that_claims_nothing(self):
        self.meet_wall()
        summary, code, verdict = self.step(None, {"outcome": "Everything went great, account made!", "account_created": "yes"})
        self.assertEqual((self.steps()[-1][2], verdict, self.wait()), ("UNKNOWN", "WAITING", "ASK_EACH_TIME"))
        self.assertEqual(self.card()["need"], "This site needs an account.")
        self.assertEqual(self.saved(), [])

    def test_generated_passwords_follow_the_rules_the_page_states(self):
        self.addCleanup(mock.patch.stopall)
        mock.patch.stopall()
        made = {browser_guard.generate_password() for _ in range(20)}
        self.assertEqual((len(made), {len(value) for value in made}), (20, {20}))
        for value in made:
            self.assertTrue(any(c.islower() for c in value) and any(c.isupper() for c in value) and any(c.isdigit() for c in value)
                            and any(c in browser_guard.PASSWORD_SYMBOLS for c in value))
        plain = browser_guard.generate_password(12, False)
        self.assertEqual((len(plain), plain.isalnum()), (12, True))
        self.assertEqual(len(browser_guard.generate_password(3)), 20)           # not a rule a page can state


class Email(Case):
    def pending(self):
        """A sign-up whose email has not arrived after the step's two minutes."""
        self.meet_wall()
        return self.step(self.sign_up, {"outcome": "EMAIL_PENDING", "email_for": "NEW_ACCOUNT", "account_created": True, "sign_up_submitted": True})

    def missing(self):
        self.pending()
        with self.db:
            self.db.execute("UPDATE %s SET not_before='2000-01-01T00:00:00+00:00' WHERE state='DUE'" % access_step.TABLE)
        return self.step(None, {"outcome": "EMAIL_PENDING", "email_for": "NEW_ACCOUNT", "account_created": None, "sign_up_submitted": False})

    def test_no_email_frees_the_browser_and_one_more_look_comes_at_ten_minutes(self):
        summary, code, verdict = self.pending()
        self.assertEqual((verdict, self.wait()), (access_step.PLANNED, None))
        job = self.job()
        self.assertEqual((job["state"], job["next_action"]), ("RETRY_WAIT", access_step.CONFIRM_WORDS))
        self.assertEqual(self.steps(), [("ACCESS", "DONE", "EMAIL_PENDING"), ("CONFIRM", "DUE", None)])
        self.assertEqual(access_step.CONFIRM_AFTER_SECONDS, 480)               # the step waited 2 of the 10 minutes
        now = applications.stamp()
        self.assertIsNone(access_step.due(self.db, self.fixture.key, SITE, now))   # not yet: no session is started for it
        self.assertEqual(access_step.due(self.db, self.fixture.key, SITE, access_step._later(now, 481))["kind"], "CONFIRM")
        self.assertGreater(job["next_at"], access_step._later(now, 400))
        self.assertEqual(self.saved(), [(SITE, EMAIL, "SIDEDOOR", True)])
        # At ten minutes the email is there: used, signed in.
        with self.db:
            self.db.execute("UPDATE %s SET not_before='2000-01-01T00:00:00+00:00' WHERE state='DUE'" % access_step.TABLE)
        self.mailbox.messages.append({"code": "112233"})
        summary, code, verdict = self.step(lambda proxy: self.page.sign_in(), {"outcome": "SIGNED_IN", "sign_up_submitted": False})
        self.assertNotIn("type_new_account_password", self.tools)              # the sign-up is never sent again
        self.assertEqual(json.loads((self.directory / "request.json").read_text())["access"]["kind"], "CONFIRM")
        self.assertEqual((verdict, self.wait(), site_sessions.has(SITE)), (access_step.PLANNED, None, True))

    def test_a_later_step_that_gets_in_without_the_password_still_saves_the_sign_in(self):   # review round 1, 3
        self.meet_wall()
        self.step(self.sign_up, {"outcome": "EMAIL_PENDING", "email_for": "NEW_ACCOUNT", "account_created": None, "sign_up_submitted": True,
                                 "after_sign_up": "CONFIRMATION_ASKED"})    # the site itself confirmed the sign-up
        self.assertEqual((self.saved(), saved_sign_ins.pending(SITE)), ([], True))
        with self.db:
            self.db.execute("UPDATE %s SET not_before='2000-01-01T00:00:00+00:00' WHERE state='DUE'" % access_step.TABLE)
        self.mailbox.messages.append({"link": "https://jobs.ashbyhq.com/confirm?t=2"})

        def play(proxy):   # the emailed link signs the browser in: nothing is filled
            said = self.tool(proxy, "open_confirmation_link", url=self.mailbox.link())
            self.page.sign_in()
            return said
        summary, code, verdict = self.step(play, {"outcome": "SIGNED_IN", "account_created": None, "sign_up_submitted": False})
        self.assertEqual((verdict, self.saved(), saved_sign_ins.pending(SITE)), (access_step.PLANNED, [(SITE, EMAIL, "SIDEDOOR", True)], False))

    def app_job(self):
        import applications_page
        view = applications_page.gather(self.db, {"timezone": "America/Los_Angeles"}, self.home)
        return next(item for item in view["list"]["jobs"] if item["key"] == self.fixture.key)

    def test_applications_says_what_is_true_for_a_job_on_its_way_to_a_step_or_waiting_for_the_email(self):
        self.meet_wall()                                                       # a step is planned: nothing went wrong
        job = self.app_job()
        self.assertEqual((job["state"], job["todo"], job["problem"]),
                         ("Waiting to retry", "Sidedoor tries this application on its next round.", None))
        self.step(self.sign_up, {"outcome": "EMAIL_PENDING", "email_for": "NEW_ACCOUNT", "account_created": True, "sign_up_submitted": True})
        job = self.app_job()
        self.assertEqual((job["todo"], job["problem"]), ("Sidedoor is waiting for the site's email.", None))
        self.assertNotIn("ran into a problem", json.dumps(job, default=str))
        self.assertNotIn("Will try again automatically", json.dumps(job, default=str))
        # The control: a job that did run into a problem still says so.
        with self.db:
            self.db.execute("UPDATE application_jobs SET next_action='Read the posting again' WHERE job_key=?", (self.fixture.key,))
        job = self.app_job()
        self.assertEqual(job["problem"], "Sidedoor ran into a problem it will retry.")
        self.assertTrue(job["todo"].startswith("Will try again automatically"))

    def test_getting_in_another_way_never_saves_a_password_the_site_may_not_have_taken(self):   # reset review, 4
        self.meet_wall()
        self.step(self.sign_up, {"outcome": "EMAIL_PENDING", "email_for": "SIGN_IN", "account_created": None, "sign_up_submitted": True,
                                 "after_sign_up": "NOTHING_SHOWN"})         # the site showed nothing after the sign-up
        with self.db:
            self.db.execute("UPDATE %s SET not_before='2000-01-01T00:00:00+00:00' WHERE state='DUE'" % access_step.TABLE)
        self.mailbox.messages.append({"link": "https://jobs.ashbyhq.com/magic?t=3"})

        def play(proxy):   # an emailed sign-in link gets the browser in: perhaps into an account the user had before
            said = self.tool(proxy, "open_confirmation_link", url=self.mailbox.link())
            self.page.sign_in()
            return said
        summary, code, verdict = self.step(play, {"outcome": "SIGNED_IN", "account_created": None, "sign_up_submitted": False})
        self.assertEqual((verdict, self.saved(), saved_sign_ins.pending(SITE)), (access_step.PLANNED, [], False))   # in, and nothing saved
        self.assertTrue(site_sessions.has(SITE))

    def test_still_no_email_is_the_card_with_resend_confirmed_and_abandon(self):
        summary, code, verdict = self.missing()
        self.assertEqual((verdict, self.wait()), ("WAITING", "CONFIRMATION_EMAIL_MISSING"))
        card = self.card()
        self.assertEqual(card["need"], "The site's confirmation email did not arrive. Confirm it yourself.")
        self.assertEqual((card["resend_label"], card["done_label"], card["abandon"], card["step"]), ("Resend", "I've confirmed it", True, None))

    def test_resend_asks_the_site_again_and_looks(self):
        self.missing()
        said = access_cards.resend(self.db, self.fixture.key, applications.stamp())
        self.assertEqual(said, {"tone": "success", "text": "Sidedoor tries this application on its next round."})
        self.assertEqual((self.wait(), self.job()["state"], self.steps()[-1]), (None, "RETRY_WAIT", ("RESEND", "DUE", None)))
        self.assertEqual(access_cards.resend(self.db, self.fixture.key, applications.stamp())["text"], access_cards.GONE)   # a second press
        self.mailbox.messages.append({"link": "https://jobs.ashbyhq.com/confirm?t=1"})

        def play(proxy):
            said = self.tool(proxy, "open_confirmation_link", url=self.mailbox.link())
            self.page.sign_in()
            return said
        summary, code, verdict = self.step(play, {"outcome": "SIGNED_IN", "sign_up_submitted": False})
        self.assertEqual(json.loads((self.directory / "request.json").read_text())["access"]["kind"], "RESEND")
        self.assertNotIn("type_new_account_password", self.tools)
        self.assertEqual((verdict, self.wait(), len(self.page.opened)), (access_step.PLANNED, None, 1))

    def test_resend_that_still_brings_nothing_puts_the_card_back(self):
        self.missing()
        access_cards.resend(self.db, self.fixture.key, applications.stamp())
        summary, code, verdict = self.step(None, {"outcome": "EMAIL_PENDING", "email_for": "NEW_ACCOUNT", "sign_up_submitted": False})
        self.assertEqual((verdict, self.wait(), self.card()["resend_label"]), ("WAITING", "CONFIRMATION_EMAIL_MISSING", "Resend"))

    def test_i_have_confirmed_it_makes_sidedoor_sign_in_next(self):
        self.missing()
        said = access_cards.done(self.db, self.fixture.key, applications.stamp())
        self.assertEqual((said["tone"], self.wait(), self.steps()[-1]), ("success", None, ("ACCESS", "DUE", None)))

        def play(proxy):
            said = self.tool(proxy, "fill_saved_sign_in", email_target="e1", password_target="e2")
            self.page.sign_in()
            return said
        self.page.typed.clear()
        summary, code, verdict = self.step(play, {"outcome": "SIGNED_IN", "sign_up_submitted": False})
        self.assertEqual((self.page.typed, verdict, self.wait()), ({"e1": EMAIL, "e2": MARKER}, access_step.PLANNED, None))
        self.assertNotIn("type_new_account_password", self.tools)

    def test_abandon_closes_it_with_try_again(self):
        self.missing()
        self.assertEqual(access_cards.abandon(self.db, self.fixture.key, applications.stamp())["text"], access_cards.ABANDONED)
        self.assertEqual((self.fixture.job()["state"], self.fixture.job()["blocker"]), ("ABANDONED", "ACCESS_ABANDONED_BY_YOU"))

    def test_an_emailed_sign_in_link_gets_an_existing_account_in_with_no_password(self):   # owner, 2026-11-13, H12 A
        self.meet_wall()
        self.mailbox.messages.append({"link": "https://jobs.ashbyhq.com/magic?t=9"})

        def play(proxy):
            said = self.tool(proxy, "open_confirmation_link", url=self.mailbox.link())
            self.page.sign_in()
            return said
        summary, code, verdict = self.step(play, {"outcome": "SIGNED_IN", "email_for": "SIGN_IN", "account_created": None, "sign_up_submitted": False})
        self.assertEqual((verdict, self.wait(), self.page.typed, self.saved()), (access_step.PLANNED, None, {}, []))
        self.assertTrue(site_sessions.has(SITE))
        prompt = self.prompts[-1]
        for told in ("GOAL.", "FACTS", "TOOLS", "THE OWNER'S LINES", "a sign-in link or code the site emails", "You work out the best feasible way in"):
            self.assertIn(told, prompt)
        self.assertNotIn("Send login link", prompt)                            # the goal and the facts, no site's steps

    def test_a_sign_in_email_that_never_comes_says_so_and_offers_sign_in_and_resend(self):
        self.meet_wall()
        report = {"outcome": "EMAIL_PENDING", "email_for": "SIGN_IN", "account_created": None, "sign_up_submitted": False}
        summary, code, verdict = self.step(None, report)
        self.assertEqual((verdict, self.steps()[0][2], self.steps()[1][:2]), (access_step.PLANNED, "SIGN_IN_EMAIL_PENDING", ("CONFIRM", "DUE")))
        with self.db:
            self.db.execute("UPDATE %s SET not_before='2000-01-01T00:00:00+00:00' WHERE state='DUE'" % access_step.TABLE)
        summary, code, verdict = self.step(None, report)
        self.assertEqual((verdict, self.wait()), ("WAITING", "SIGN_IN_EMAIL_MISSING"))
        card = self.card()
        self.assertEqual(card["need"], "The site's sign-in email did not arrive. Sign in yourself.")
        self.assertEqual((card["step"]["label"], card["resend_label"], card["abandon"]), ("Sign in", "Resend", True))


class Setting(Case):
    setting = None

    def test_only_under_sign_in_and_create_accounts_for_me(self):
        for value, reason in ((None, "ASK_EACH_TIME"), ("ASK", "ASK_EACH_TIME")):
            if value:
                with self.db:
                    access_outcomes.set_account_setting(self.db, value, applications.stamp())
            summary, code, verdict = self.meet_wall()
            self.assertEqual((verdict, self.wait(), self.steps()), ("WAITING", reason, []))
            self.assertEqual(self.card()["resend_label"], None)
        # Choosing it is "something changed": the waiting job is released and its next wall gets the step.
        with self.db:
            released = access_outcomes.set_account_setting(self.db, "SIGN_IN", applications.stamp())
        self.assertEqual((released, self.wait()), ([self.fixture.key], None))
        self.assertEqual((self.meet_wall()[2], self.steps()), (access_step.PLANNED, [("ACCESS", "DUE", None)]))
        # With the path switched off again nothing is planned or run.
        with mock.patch.object(access_outcomes, "resolver_available", return_value=False):
            self.assertEqual(access_outcomes.offered_settings(), ("ASK", "SKIP"))
            ran = []
            self.request(lambda config, prompt, directory, timeout: (
                ran.append(config), write_output(directory, request_at(directory), self.fixture.review())))
            self.assertNotIn(saved_sign_ins.ACCESS_CONFIG_KEY, ran[0])
        self.assertEqual(access_outcomes.offered_settings(), ("SIGN_IN", "ASK", "SKIP"))
        self.assertTrue(access_outcomes.resolver_available())

    def test_with_no_user_action_a_job_gets_at_most_four_sessions_and_then_a_card(self):   # the bound without the read cap
        with self.db:
            access_outcomes.set_account_setting(self.db, "SIGN_IN", applications.stamp())
        reports = iter([{"outcome": "EMAIL_PENDING", "email_for": "SIGN_IN", "sign_up_submitted": False},
                        {"outcome": "SIGNED_IN", "sign_up_submitted": False}] + [{"outcome": "SIGNED_IN", "sign_up_submitted": False}] * 20)
        sessions = []
        for _ in range(20):                                    # the site walls every read, whatever the step reports
            if self.wait():
                break
            with self.db:
                self.db.execute("UPDATE %s SET not_before='2000-01-01T00:00:00+00:00' WHERE state='DUE'" % access_step.TABLE)
            if access_step.due(self.db, self.fixture.key, SITE, applications.stamp()):
                self.step(None, next(reports))
                sessions.append("step")
            else:
                self.meet_wall()
                sessions.append("read")
        self.assertEqual(sessions, ["read", "step", "step", "read"])           # N = 4: wall, step, its second look, wall
        self.assertEqual((self.wait(), self.job()["state"]), ("SIGN_IN", "BLOCKED"))   # then the user's turn
        # Each step's own request, and each wall read a step answered, is marked on its request row.
        marks = [row[0] for row in self.db.execute("SELECT event FROM desktop_events WHERE event IN (?,?) ORDER BY id",
                                                   (access_step.STEP_REQUEST_EVENT, access_step.WALL_ANSWERED_EVENT))]
        self.assertEqual(marks.count(access_step.STEP_REQUEST_EVENT), 2)
        self.assertGreaterEqual(marks.count(access_step.WALL_ANSWERED_EVENT), 1)
        self.assertEqual(len(access_step.not_reads(self.db, self.fixture.key)), 3)     # the last wall read became a card: it counts

    def test_a_site_with_a_saved_sign_in_gets_the_fill_tool_and_never_the_sign_up(self):
        with self.db:
            access_outcomes.set_account_setting(self.db, "SIGN_IN", applications.stamp())
        saved_sign_ins.add(SITE, EMAIL, MARKER, saved_sign_ins.SIDEDOOR)
        self.meet_wall()

        def play(proxy):
            said = self.tool(proxy, "fill_saved_sign_in", email_target="e1", password_target="e2")
            self.page.sign_in()
            return said
        summary, code, verdict = self.step(play, {"outcome": "SIGNED_IN", "sign_up_submitted": False})
        self.assertEqual(self.tools, ["fill_saved_sign_in", "open_confirmation_link"])
        self.assertEqual((self.said, self.page.typed, verdict), ([browser_guard.SAVED_SIGN_IN_TEXT["filled"]], {"e1": EMAIL, "e2": MARKER}, access_step.PLANNED))
        self.assertEqual(self.read_configs[0].get(saved_sign_ins.CONFIG_KEY), None)

    def test_a_site_that_rejects_the_saved_sign_in_is_the_users_turn(self):
        with self.db:
            access_outcomes.set_account_setting(self.db, "SIGN_IN", applications.stamp())
        saved_sign_ins.add(SITE, EMAIL, MARKER, saved_sign_ins.SIDEDOOR)
        self.meet_wall()
        summary, code, verdict = self.step(None, {"outcome": "SIGN_IN_REFUSED", "sign_up_submitted": False})
        self.assertEqual((verdict, self.wait(), self.saved()), ("WAITING", "SIGN_IN", [(SITE, EMAIL, "SIDEDOOR", True)]))


class NoSecretAnywhere(Case):
    """The marker test for the generated password, with its controls."""

    def search(self, folders):
        hits = []
        for folder in folders:
            for path in Path(folder).rglob("*"):
                if path.is_file() and any(form.encode() in path.read_bytes() for form in FORMS + ["walrus-77b3d1"]):
                    hits.append(path.name)
        for (table,) in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            for row in self.db.execute("SELECT * FROM %s" % table).fetchall():
                if any(form in repr(tuple(row)) or form in json.dumps(tuple(row), default=str) for form in FORMS + ["walrus-77b3d1"]):
                    hits.append("table " + table)
        return hits

    def test_the_generated_password_reaches_no_prompt_log_audit_trace_file_or_row(self):
        records = []
        handler = logging.Handler()
        handler.emit = lambda record: records.append(record.getMessage() + repr(record.args))
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        self.meet_wall()

        def play(proxy):
            said = self.sign_up(proxy)
            # The page shows it afterwards (its own "show password"), the model saves a snapshot and a screenshot.
            folder = Path(proxy.output_folder)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "shown.yml").write_text("- textbox \"Password\": " + json.dumps(MARKER))
            (folder / "form.txt").write_text("password=" + urllib.parse.quote_plus(MARKER, safe="*").replace("~", "%7E"))
            (folder / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe")
            proxy.child.stdout = [json.dumps({"jsonrpc": "2.0", "id": 5, "result": {"content": [{"type": "text", "text":
                "- textbox \"Password\": " + json.dumps(MARKER)}]}}) + "\n"]
            proxy.pump_child(proxy.child)
            self.page.sign_in()
            (self.directory / "trace.jsonl").write_text(self.out.getvalue())   # all the AI was given, as a trace keeps it
            return said
        summary, code, verdict = self.step(play, {"outcome": "ACCOUNT_CREATED", "account_created": True, "sign_up_submitted": True})
        saved_sign_ins_page.view()
        self.db.commit()
        self.assertEqual(self.page.typed, {"e2": MARKER, "e3": MARKER})        # control 1: the marker really travelled
        self.assertEqual(saved_sign_ins.for_fill(SITE), (EMAIL, MARKER))       # and is the saved sign-in
        self.assertIn(browser_guard.SAVED_SIGN_IN_MARK, self.relayed)
        folders = {Path(self.fixture.root), Path(self.home)}
        names = {path.name for folder in folders for path in folder.rglob("*")}
        self.assertTrue({"guard.jsonl", "prompt.txt", "request.json", "trace.jsonl", "shown.yml", "form.txt",
                         saved_sign_ins.STORE_FILE} <= names, names)
        self.assertNotIn("shot.png", names)                                    # an image taken after it was typed is not kept
        self.assertEqual(self.search(folders), [])
        for text in self.prompts + [self.relayed, "\n".join(records)]:
            for form in FORMS:
                self.assertNotIn(form, text)
        # Control 2 (must hit): the same search finds it in a file and in a row.
        (self.fixture.root / "control.log").write_text(json.dumps(MARKER))
        self.db.execute("CREATE TABLE control_rows (value TEXT)")
        self.db.execute("INSERT INTO control_rows VALUES (?)", (urllib.parse.quote(MARKER, safe=""),))
        self.assertEqual(sorted(self.search(folders)), ["control.log", "table control_rows"])


DEPENDENCIES = all(importlib.util.find_spec(name) for name in ("fastapi", "httpx"))


@unittest.skipUnless(DEPENDENCIES, "dashboard packages (fastapi, httpx) are not installed")
class SettingsPage(unittest.TestCase):
    def test_settings_offers_the_first_choice_with_one_line_and_still_one_control(self):
        import os
        from fastapi.testclient import TestClient
        import dashboard
        import setup_steps
        from tests.test_dashboard_overview import CONFIG, PORT, SECRET, finished_home
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        finished_home(Path(directory.name))
        with mock.patch.dict(os.environ, {"SIDEDOOR_HOME": directory.name}), \
                mock.patch.object(setup_steps, "chrome_path", return_value="/chrome"):
            client = TestClient(dashboard.create_app(SECRET, PORT, config=CONFIG), base_url=f"http://127.0.0.1:{PORT}", follow_redirects=False)
            client.get("/", params={"t": SECRET})
            page = client.get("/settings").text
            section = page[page.index('id="account-setting"'):page.index("</section>", page.index('id="account-setting"'))]
            self.assertEqual(re.findall(r'name="account" value="(\w+)"', section), ["SIGN_IN", "ASK", "SKIP"])
            self.assertEqual(section.count("<strong>Sign in and create accounts for me</strong>"), 1)
            self.assertEqual(section.count("The first choice accepts each site&#39;s terms for you. "
                                           "Passwords stay on this computer."), 1)
            self.assertEqual(re.findall(r'name="account" value="\w+" checked', section), [])   # never chosen for the user
            self.assertEqual(page.count('action="/settings/account"'), 1)
            saved = client.post("/settings/account", data={"account": "SIGN_IN"}, headers={"origin": f"http://127.0.0.1:{PORT}", "hx-request": "true"})
            self.assertIn("Saved: Sign in and create accounts for me.", saved.text)
            self.assertEqual(access_outcomes.install_account_setting(Path(directory.name)), "SIGN_IN")


if __name__ == "__main__":
    unittest.main()
