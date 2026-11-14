"""The look-and-feel pass (E15.T14, owner's picks 2026-11-14): what each page says and shows, read from
the real pages (the dashboard's own routes and producers), one class per item. Presentation only, plus
the one Metrics count: nothing here changes which jobs run, submit, retry or close.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import datetime as dt
import re
import unittest
from pathlib import Path

import access_outcomes
import access_step
import application_recovery
import applications
import applications_page
import browser_runtime
import metrics_page
import overview_page
import tests.test_access_cards_e15_t2 as cards

DEPENDENCIES = cards.DEPENDENCIES

ROOT = Path(__file__).resolve().parent.parent
STUCK = "This application is stuck. Press Try again."
WAS_CLOSED = "This application was closed. Press Try again if you want this job."
GENERIC = "Sidedoor is holding this application for the reason above."
TRY_FORM = 'action="/access/try-again"'


@unittest.skipUnless(DEPENDENCIES, "dashboard packages (fastapi, httpx) are not installed")
class Case(unittest.TestCase):
    setUp = cards.AccessCardsTest.setUp
    job, row, wall, behind, site_group, page = (cards.AccessCardsTest.job, cards.AccessCardsTest.row, cards.AccessCardsTest.wall,
                                               cards.AccessCardsTest.behind, cards.AccessCardsTest.site_group, cards.AccessCardsTest.page)

    def parked(self, key="parked", company="Parked Co"):
        self.job(key, company, "Engineer")
        with self.database:
            applications.hold(self.database, self.row(key), application_recovery.NEEDS_YOU_BLOCKER,
                              application_recovery.NEEDS_YOU_WORDS, self.now, "BLOCKED", route="OWNER")
        return key

    def reads_spent(self, key="spent", company="Spent Co"):
        self.job(key, company, "Engineer")
        with self.database:
            applications.hold(self.database, self.row(key), application_recovery.CLAIMS_EXHAUSTED_BLOCKER,
                              application_recovery.CLAIMS_EXHAUSTED_ACTION, self.now, "BLOCKED")
        return key

    def old_closure(self, key="old", company="Old Co"):
        """As the old rule left it, then relabelled by the applications round (`relabel_failed_closures`)."""
        self.job(key, company, "Engineer")
        with self.database:
            lifecycle = {"blocker": "REQUISITION_MISMATCH", "started_at": self.now, "max_attempts": 3}
            self.assertTrue(application_recovery.abandon(self.database, self.row(key), lifecycle, [],
                                                         application_recovery.OLD_FAILED_REASON, [], self.now))
            self.assertEqual(application_recovery.relabel_failed_closures(self.database, self.now), [key])
        return key

    def ai_abandoned(self, key="ai", company="Decided Co"):
        self.job(key, company, "Engineer")
        with self.database:
            lifecycle = {"blocker": "REQUISITION_MISMATCH", "started_at": self.now, "max_attempts": 3}
            application_recovery.abandon(self.database, self.row(key), lifecycle, [], "The posting was removed", [], self.now)
        return key

    def uncertain(self, key):
        with self.database:
            self.database.execute("INSERT INTO application_attempts(attempt_id,job_key,idempotency_key,state,started_at) "
                                  "VALUES (?,?,?,?,?)", ("attempt-" + key, key, "idem-" + key, "SUBMITTING", self.now))

    def rows(self, page=None):
        """{company: (the row's collapsed part, the whole row)} of the Applications list."""
        page = page if page is not None else self.page("/applications")
        found = {}
        for row in re.findall(r'<li class="card app-row.*?</li>', page, re.S):
            company = re.search(r"<strong>(.*?)</strong>", row).group(1)
            found[company] = (re.search(r"<summary>(.*?)</summary>", row, re.S).group(1), row)
        return found

    def chip(self, summary):
        return re.search(r'<span class="chip chip-\w+">(.*?)</span>', summary).group(1)

    def group_of(self, company):
        page = self.page("/applications")
        for key, section in re.findall(r'<section class="app-group" aria-labelledby="app-group-(\w+)">(.*?)</section>', page, re.S):
            if "<strong>%s</strong>" % company in section:
                return key
        return None

    def failure(self, company):
        """The Failures item that names the job, as text."""
        page = self.page("/failures")
        items = [item for item in re.findall(r"<li\b.*?</li>", page, re.S) if company in item]
        self.assertTrue(items, company)
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", items[0]))


class TryAgainOnTheCollapsedRow(Case):   # L1
    def test_every_row_that_says_press_try_again_has_the_button_on_the_row_itself_once(self):
        self.parked()
        self.reads_spent()
        self.old_closure()
        rows = self.rows()
        for company in ("Parked Co", "Spent Co", "Old Co"):
            summary, row = rows[company]
            self.assertIn("Try again", re.sub(r"<[^>]+>", " ", summary), company)   # the line that says to press it
            self.assertIn(TRY_FORM, summary, company)                               # ... and the button beside it
            self.assertEqual(row.count(TRY_FORM), 1, company)
        # A needs-you row keeps its own button where it was.
        self.wall(self.job("lead", "Stripe", "Staff Engineer"))
        summary, _ = self.rows()["Stripe"]
        self.assertIn("Open on Approvals", summary)
        self.assertNotIn(TRY_FORM, summary)

    def test_no_try_again_is_shown_for_a_job_whose_submission_may_have_reached_the_employer(self):
        for make, company in ((self.parked, "Parked Co"), (self.reads_spent, "Spent Co"), (self.old_closure, "Old Co")):
            key = make()
            self.assertIn(TRY_FORM, self.rows()[company][1], company)               # the control: shown before
            self.uncertain(key)
            self.assertNotIn(TRY_FORM, self.rows()[company][1], company)
            self.assertNotIn(TRY_FORM, self.page("/applications?job=" + key), company)
            with self.database:   # and the button's own guard is what it always was
                self.assertEqual(access_outcomes.try_again(self.database, key, self.now)["why"], "SUBMISSION_UNCERTAIN")

    def test_a_job_the_ai_itself_abandoned_has_no_try_again(self):
        self.ai_abandoned()
        summary, row = self.rows()["Decided Co"]
        self.assertNotIn(TRY_FORM, row)
        self.assertEqual(self.chip(summary), "Given up")


class OneSentenceOnce(Case):   # L2, L3, L6
    def test_a_parked_job_says_the_owners_sentence_and_never_the_generic_hold_line(self):
        self.parked()
        summary, row = self.rows()["Parked Co"]
        self.assertEqual((summary.count(STUCK), row.count(STUCK), GENERIC in row), (1, 2, False))   # the row, and the same line inside it
        self.assertEqual((self.chip(summary), self.group_of("Parked Co")), ("Blocked, needs you", "needs_you"))
        detail = self.page("/applications?job=parked")
        steps = detail.split('<dl class="facts">')[1].split("<h3>")[0]         # the job's own lines, above its history
        self.assertEqual((steps.count(STUCK), 'class="app-problem"' in steps, GENERIC in detail), (1, False, False))
        failure = self.failure("Parked Co")
        self.assertEqual((failure.count(STUCK), GENERIC in failure), (1, False))

    def test_the_generic_line_is_left_only_for_a_hold_with_no_sentence_of_its_own(self):
        for code in sorted(application_recovery.USER_HOLDS | {overview_page.PARKED_CODE}):
            row = {"state": "BLOCKED", "blocker": code, "next_at": None}
            todo = applications_page._what_to_do(_Row(row), "Co", "OWNER", True, False, str)[0]
            self.assertNotEqual(todo, GENERIC, code)

    def test_a_job_the_old_rule_closed_says_its_sentence_once_under_a_true_chip(self):
        self.old_closure()
        summary, row = self.rows()["Old Co"]
        self.assertEqual((summary.count(WAS_CLOSED), row.count(WAS_CLOSED)), (1, 2))   # the row, and "Why it closed" inside
        self.assertNotIn("RECOVERY_COORDINATOR_FAILED", row)                         # never its recorded code as a "reason"
        self.assertEqual((self.chip(summary), self.group_of("Old Co")), ("Closed", "closed"))
        failure = self.failure("Old Co")
        self.assertEqual(failure.count(WAS_CLOSED), 1)
        self.assertNotIn("Sidedoor will not try this one again.", failure)           # untrue: it can be tried again
        self.assertIn("Nothing was sent.", failure)                                  # true, and what makes a retry safe

    def test_a_job_that_cannot_be_tried_again_still_says_so_on_failures(self):
        self.ai_abandoned()
        self.assertIn("Sidedoor will not try this one again.", self.failure("Decided Co"))


class _Row(dict):
    def keys(self):
        return list(super().keys())


class WaitingBehindACard(Case):   # L4
    def test_the_job_the_user_acts_on_needs_you_and_the_ones_behind_it_only_wait(self):
        self.site_group()
        self.wall(self.job("check", "Northwind", "Analyst", site="northwind.example"), "CAPTCHA_REQUIRED")
        page = self.page("/applications")
        rows = self.rows(page)
        self.assertNotIn("Will try again automatically", page)
        self.assertNotIn("Waiting to retry</span>", page)
        counts = {"needs_you": 0, "progress": 0}
        for key, section in re.findall(r'<section class="app-group" aria-labelledby="app-group-(\w+)">(.*?)</section>', page, re.S):
            counts[key] = section.count('<li class="card app-row')
        self.assertEqual((counts["needs_you"], counts["progress"]), (2, 2))          # Stripe's lead and Northwind; the two behind
        lead = [row for row in re.findall(r'<li class="card app-row.*?</li>', page, re.S) if "Staff Engineer" in row][0]
        self.assertIn("Blocked, needs you", lead)
        self.assertIn("Open on Approvals", lead)
        for role in ("Data Engineer", "Platform Engineer"):
            row = [row for row in re.findall(r'<li class="card app-row.*?</li>', page, re.S) if role in row][0]
            self.assertEqual(self.chip(row), "Waiting", role)
            self.assertIn(access_outcomes.HOST_HELD_WORDS, row)
            self.assertNotIn("Open on Approvals", row)
        self.assertIn("Open on Approvals", rows["Northwind"][0])
        # The "Blocked, needs you" filter counts the same jobs the heading holds.
        reader = overview_page.Reader(self.database)
        self.assertEqual(sorted(applications_page.needs_you_keys(reader, dt.datetime.now(dt.timezone.utc))), ["check", "lead"])
        self.assertEqual(self.needs()["sign_ins"], 2)                                # Overview's cards are unchanged

    needs = cards.AccessCardsTest.needs

    def test_a_released_job_says_only_what_happens(self):
        self.assertEqual(access_outcomes.HOST_RELEASED_WORDS, "Sidedoor tries this application on its next round.")


class OverviewLayoutAndWords(Case):   # L7, L8
    def test_the_cards_are_a_block_of_their_own_below_the_three_tiles(self):
        self.site_group()
        page = self.page("/")
        wrap = re.search(r'<div class="overview-needs-wrap" id="overview-needs">(.*?)<section class="card overview-now"', page, re.S).group(1)
        tile = re.search(r'<section class="card overview-needs".*?</section>', wrap, re.S).group(0)
        self.assertNotIn("access-card", tile)                                        # the count tile holds no card
        block = re.search(r'<section class="card overview-sign-ins".*?</section>', wrap, re.S).group(0)
        self.assertIn('class="approval-card access-card"', block)
        self.assertIn('hx-target="#overview-needs"', block)                          # an action still redraws the whole part
        css = (ROOT / "dashboard_assets/static/dashboard.css").read_text()
        for rule in (".overview-needs-wrap { display: contents; }", ".overview-sign-ins { grid-column: 1 / -1; order: 1; }",
                     ".overview-health { order: 2; }"):
            self.assertIn(rule, css)

    def test_the_count_lines_are_parallel(self):
        words = {key: (one, many) for key, one, many, _, _ in overview_page.NEEDS_ROWS}
        self.assertEqual(words["sign_ins"], ("sign-in or human check to do", "sign-ins or human checks to do"))
        self.assertEqual(words["questions"], ("question to answer", "questions to answer"))
        for one, many in words.values():
            self.assertFalse(one.endswith(".") or many.endswith("."))

    def test_the_browser_rows_say_the_state_and_the_instruction_only(self):
        said = dict(browser_runtime.UNAVAILABLE_WORDS), dict(browser_runtime.UNAVAILABLE_TODO)
        self.assertEqual(said, ({"RUNTIME_MISSING": "Sidedoor's browser component is missing.",
                                 "PROFILE_IN_USE": "The Sidedoor Chrome window is open."},
                                {"RUNTIME_MISSING": "Reinstall Sidedoor.", "PROFILE_IN_USE": "Close the Sidedoor Chrome window."}))

    def test_the_run_line_is_one_sentence(self):
        page = self.page("/")
        lines = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", line)).strip()
                 for line in re.findall(r'<p class="overview-run">(.*?)</p>', page, re.S)]
        self.assertTrue(lines)
        for line in lines:
            self.assertNotIn("·", line)
            self.assertNotIn("next:", line)
            self.assertTrue(line.endswith(".") and line.count(".") == 1 or "(" in line, line)
        self.assertIn("Not run yet, starts when Sidedoor is running.", lines)


class SidedoorHandled(Case):   # L9
    def step(self, key, kind, outcome, created, ended, epoch=0):
        access_step.initialize(self.database)
        self.database.execute(f"INSERT INTO {access_step.TABLE}(job_key,site,kind,state,epoch,outcome,seen,created_at,ended_at) "
                              "VALUES (?,?,?,?,?,?,0,?,?)", (key, "jobs.example.com", kind, access_step.DONE, epoch, outcome, created, ended))
        self.database.commit()

    def read(self):
        now = dt.datetime(2026, 11, 14, tzinfo=dt.timezone.utc)
        return metrics_page.read(overview_page.Reader(self.database), {"timezone": "UTC"}, now=now)

    def test_a_wall_sidedoor_got_through_is_counted_once_in_the_month_the_site_asked(self):
        self.job("a", "Acme", "Engineer")
        # The site asked on October 31; the emailed link came on November 1: one wall, counted in October.
        self.step("a", "ACCESS", "SIGN_IN_EMAIL_PENDING", "2026-10-31T23:50:00+00:00", "2026-10-31T23:53:00+00:00")
        self.step("a", "CONFIRM", "SIGNED_IN", "2026-10-31T23:53:00+00:00", "2026-11-01T00:03:00+00:00")
        self.job("b", "Beta", "Engineer")
        self.step("b", "ACCESS", "ACCOUNT_CREATED", "2026-11-02T10:00:00+00:00", "2026-11-02T10:02:00+00:00")
        view = self.read()
        self.assertEqual((view["total"]["sidedoor"], view["total"]["met"]), (2, 2))
        months = {month["key"]: month["counts"]["sidedoor"] for month in view["months"]}
        self.assertEqual(months, {"2026-10": 1, "2026-11": 1})
        self.assertEqual(view["sites"][0]["counts"]["sidedoor"], 2)                   # All time, by site

    def test_a_wall_that_needed_the_user_after_all_is_counted_once_under_how_it_stands_today(self):
        self.job("c", "Gamma", "Engineer")
        self.step("c", "ACCESS", "SIGNED_IN", "2026-11-03T10:00:00+00:00", "2026-11-03T10:02:00+00:00")
        self.assertEqual(self.read()["total"]["sidedoor"], 1)
        self.wall("c")                                                               # the site asked again: the user's card
        view = self.read()
        self.assertEqual((view["total"]["sidedoor"], view["total"]["waiting"], view["total"]["met"]), (0, 1, 1))
        with self.database:
            access_outcomes.resume(self.database, "c", self.now)                     # the user signed in
        view = self.read()
        self.assertEqual((view["total"]["sidedoor"], view["total"]["you"], view["total"]["met"]), (0, 1, 1))

    def test_a_step_that_did_not_get_through_is_not_counted(self):
        self.job("d", "Delta", "Engineer")
        self.step("d", "ACCESS", "SIGN_IN_REFUSED", "2026-11-03T10:00:00+00:00", "2026-11-03T10:02:00+00:00")
        self.assertEqual(self.read()["total"]["sidedoor"], 0)


class TextAnswersAndWords(unittest.TestCase):   # L10, L11
    def render(self, control):
        import jinja2
        environment = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "dashboard_assets/templates")), autoescape=True)
        item = {"dom": "q1", "employer": "Acme", "role": "Engineer", "url": None, "site": None, "question": "Why this role?",
                "required": True, "hint": None, "site_reason": None, "expires": None, "control": control, "options": [],
                "id": "d1", "payload_hash": "h"}
        return environment.get_template("approvals_question.html").render(item=item, f={})

    def test_every_free_text_answer_is_a_text_area(self):
        for control in ("text", "textarea", None):
            page = self.render(control)
            self.assertIn('<textarea name="answer" rows="3"', page, control)
            self.assertNotIn('type="text"', page, control)
            self.assertIn("approval-q-prose", page, control)
        for control, mark in (("date", 'type="date"'), ("number", 'type="number"')):
            page = self.render(control)
            self.assertIn(mark, page)
            self.assertNotIn("<textarea", page)

    def test_closed_by_your_setting(self):
        self.assertEqual(applications_page.CODE_WORDS["ACCESS_SKIPPED_BY_SETTING"], "Closed by your setting.")
        self.assertEqual(access_outcomes.CLOSE_REASONS["SKIPPED"][1], "Closed by your setting.")


if __name__ == "__main__":
    unittest.main()
