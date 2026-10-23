"""The question's own text in the inspect contract, and what old reads without it do.

The change under test is one leaf: `application_browser.result_contract("inspect")` now asks
the browser worker for each required question's own visible text under `label`, beside the
`id` and the `semantic` it already asked for.

Why a leaf is a safety change. `applications.answer_scope_conflict` (a proposed fix,
never installed) refuses a recorded answer about one
jurisdiction being handed to a question that names another, and to do that it reads the
question's own text and nothing else - deliberately never the `semantic`, which the browser
worker authors freely and which is the string doing the lying when a question about Germany
is labelled `currently_authorized_to_work_in_us`. Under the old contract the worker was
never asked for the text, so that guard had nothing to inspect and the owner's recorded US
authorization resolved onto a German question. The example user holds no EU or UK work
authorization and is applying in Germany, Spain, France and the UK.

The fixtures below have the shape of saved worker output
(`reports/application-browser/request-*/output.json`), with fictional employers and synthetic
ids. Each mirrors a fixture file under `tests/fixtures/application-browser/` and carries that
file's sha256; `test_every_excerpt_still_matches_the_file_it_came_from` re-reads the files and
fails if an excerpt has drifted. Nothing here reads the checkout's own `reports/`.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import hashlib
import json
from pathlib import Path
import unittest

import application_browser as browser
import applications
from manual_application_browser import event


# ---------------------------------------------------------------------------------------
# Saved reads (fictional), shaped like those from before the contract asked for a label.
# ---------------------------------------------------------------------------------------

# Initrode, a Greenhouse form, partial read. The employer's actual wording for question 11 was
# observed - it is sitting in the evidence array as `Are you authorized to work in the US?` -
# but the contract gave the worker nowhere to record it, so the *question* carries only a
# semantic. This is the read that makes the point: the jurisdiction is not in the result.
INITRODE = {
    "request": "request-fixt0015",
    "sha256": "2e037c07835527c7a17393cefae73978b211167ca1faaacc9aa280137762a785",
    "pointer": ["partial_review", "questions"],
    "questions": [
        {"id": "first_name", "required": True, "semantic": "first_name"},
        {"id": "last_name", "required": True, "semantic": "last_name"},
        {"id": None, "required": True, "semantic": "contact_email"},
        {"id": "country", "required": True, "semantic": "phone_country_code"},
        {"id": None, "required": True, "semantic": "contact_phone"},
        {"id": "candidate-location", "required": True, "semantic": "current_city"},
        {"id": "resume", "required": True, "semantic": "resume"},
        {"id": "question_92000000001", "required": True, "semantic": "linkedin_profile"},
        {"id": "question_92000000003", "required": True, "semantic": "salary_requirements"},
        {"id": "question_92000000004", "required": True, "semantic": "chicago_office_attendance_monday_through_wednesday"},
        {"id": "question_92000000005", "required": True, "semantic": "willing_to_relocate_to_chicago"},
        {"id": "question_92000000006", "required": True, "semantic": "currently_authorized_to_work_in_us"},
        {"id": "question_92000000007", "required": True, "semantic": "requires_employer_visa_sponsorship_now_or_future"},
    ],
}

# Vandelay Security, the only complete enumerated review in the corpus. Four of its five questions
# carry `"id": null`, which is why `applications.question_field` derives a substitute key.
VANDELAY = {
    "request": "request-fixt0008",
    "sha256": "63329d2cc7ce819896d84227414b9affc3bd77d751af8a7e4aded60287f72cce",
    "pointer": ["questions"],
    "questions": [
        {"id": None, "required": True, "semantic": "legal_or_resume_full_name"},
        {"id": None, "required": True, "semantic": "contact_email"},
        {"id": None, "required": True, "semantic": "resume"},
        {"id": "00000000-0000-4000-8000-0000000000b1", "required": True, "semantic": "linkedin_profile"},
        {"id": None, "required": True, "semantic": "office_attendance_2_3_days_per_week_in_springfield_or_austin"},
    ],
}

# The one real read that supplied labels without being asked. It shows `label` is already the
# key a worker reaches for - and, in the other direction, that a worker which starts writing
# prose drops the `id` and the `semantic` entirely, which is why the contract has to show all
# three keys on one question rather than the label alone.
UMBRELLA = {
    "request": "request-fixt0010",
    "sha256": "8c871bf8342683bbad3a0f732b710cc5c4ab46761fd1482f7d062f2c934e8419",
    "pointer": ["partial_review", "questions"],
    "questions": [
        {"label": "Name", "required": True},
        {"label": "Email", "required": True},
        {"label": "Resume", "required": True},
        {"label": "Why Mooby, and why are you a fit for this role?", "required": True,
         "instructions": "Please share in 1-3 sentences."},
        {"label": "Have you worked at an early-stage startup before?", "required": True,
         "conditional_instructions": ["If yes, please describe the company size and stage.",
                                      "If no, write N/A."]},
        {"label": "Current Location", "required": True, "options": None},
        {"label": "LinkedIn Profile", "required": True},
    ],
}

SAVED = (INITRODE, VANDELAY, UMBRELLA)
REPORTS = Path(__file__).resolve().parent / "fixtures" / "application-browser"

# The example profile's recorded US work authorization (`profile.example.json`).
US_AUTHORIZATION = {"value": True, "semantic": "currently_authorized_to_work_in_us",
                    "source": "Example user's statement: US permanent resident / Green Card"}
AUTHORIZATION_QUESTION = INITRODE["questions"][11]


def saved_questions(fixture):
    """The questions of a saved read as they sit in its fixture file."""
    path = REPORTS / fixture["request"] / "output.json"
    if not path.exists():
        return None
    raw = path.read_bytes()
    node = json.loads(json.loads(raw)["result_json"])
    for step in fixture["pointer"]:
        node = node[step]
    return hashlib.sha256(raw).hexdigest(), node


class TheInspectContractAsksForTheQuestionsOwnText(unittest.TestCase):
    """What the worker is handed. Only the values travel - `consume` JSON-encodes this dict."""

    def setUp(self):
        self.question = browser.result_contract("inspect")["questions"][0]

    def test_the_contract_asks_for_a_label(self):
        self.assertIn("label", self.question)

    def test_the_label_sits_beside_the_id_and_the_semantic_on_one_question(self):
        """A worker that writes prose drops the machine keys; see UMBRELLA above.

        Showing all three on the same object is what asks for both at once.
        """
        # answer_type joined them in CZ: applications.py selects which answers become uploaded
        # FILES by it, and nothing produced it, so the generated cover letter was never attached.
        # The point of this test is that the machine keys sit beside the label, not the count.
        # max_characters joined them on 2026-10-01 (free-text character limits).
        self.assertEqual(list(self.question), ["id", "label", "semantic", "required", "answer_type", "max_characters"])

    def test_the_placeholder_is_the_only_wording_that_reaches_the_worker(self):
        """A Python comment in `result_contract` is invisible to the model; this string is not."""
        placeholder = self.question["label"].casefold()
        for demand in ("verbatim", "untranslated", "own visible text", "null if unreadable"):
            with self.subTest(demand=demand):
                self.assertIn(demand, placeholder)

    def test_the_prompt_says_why_the_text_is_required_and_what_not_to_do_with_it(self):
        prompt = browser.PROMPT.casefold()
        for demand in ("record each required question's own visible text verbatim in its label",
                       "untranslated, uncorrected and unsummarized",
                       "reads that text, and only that text",
                       "is not an enumerated question",
                       "never invent, complete or normalize a question's wording"):
            with self.subTest(demand=demand):
                self.assertIn(demand, prompt)

    def test_the_prompt_tells_the_worker_what_to_do_when_it_cannot_read_the_wording(self):
        """The failure mode worth naming: a paraphrase is worse than a null.

        A guard that reads a paraphrase reads the worker's opinion of the question. A guard
        that reads a null knows it has nothing, which is the honest state.
        """
        self.assertIn("leave its label\nnull and completeness false rather than supplying a paraphrase",
                      browser.PROMPT)

    def test_no_other_operation_grew_a_questions_list(self):
        """submit/verify/reconcile/investigate are untouched by this change."""
        for operation in ("submit", "verify", "reconcile", "investigate"):
            with self.subTest(operation=operation):
                self.assertNotIn("questions", browser.result_contract(operation))

    def test_nothing_else_in_the_inspect_contract_moved(self):
        contract = browser.result_contract("inspect")
        self.assertEqual(sorted(contract), [
            "action_scope", "all_required_questions_enumerated",
            # 2026-09-24: how this posting takes applications. A posting with no form and a
            # mailto: apply link (Genco) is an EMAIL application, recorded rather than abandoned.
            "application_method",
            "candidate_email", "compensation",
            # 2026-09-24: the recipient and subject of an EMAIL application, quoted from the page.
            "email_application",
            "employee_count_evidence", "employer",
            # 2026-09-22 (FO): the read's own judgement that a page rendering a legal name is
            # this employer under another rendering. `application_destination.employer_accepted`
            # requires it, the inspect prompt asks for it, and it was in no contract - so no
            # inspect could ever supply it and "Mooby, Inc." against stored "mooby" was refused.
            "employer_matches_expected",
            "facts", "form_revision", "history",
            "official_destination_verified", "official_url", "questions", "requisition", "role",
            # 2026-10-07 (item 3): the read's own judgement that the page's role is this job's
            # role under another rendering (`application_destination.role_reconciliation`).
            "role_matches_expected",
            # 2026-09-19: the AI's level/specialization verdicts against the owner's targets.
            "scope_fit", "verified",
            # Part C3 (2026-09-19): visible required questions may be complete while conditional
            # ones are enumerated inside the guarded submit session.
            "visible_required_questions_enumerated"])
        self.assertEqual(sorted(contract["compensation"]), ["currency", "maximum", "minimum", "period", "verified"])
        self.assertIs(contract["all_required_questions_enumerated"], False)


class ANonNullLabelIsBoundToAnActualPageRead(unittest.TestCase):
    """A label quoted from something the browser never showed is dropped, not kept."""

    OBSERVED_AT = "2026-09-18T06:29:25+00:00"
    PAGE = "23 text Besitzen Sie eine Arbeitserlaubnis fur Deutschland? *"
    OBSERVATION = "Synthetic Company - Stellenangebote\n" + PAGE + "\n24 radio Ja\n25 radio Nein"

    def normalized(self, label, quote):
        review = {"verified": False, "official_destination_verified": False,
                  "all_required_questions_enumerated": False, "employer": "Synthetic Company",
                  "questions": [{"id": "q-1", "label": label, "semantic": "currently_authorized_to_work_in_us",
                                 "required": True}]}
        request = {"request_id": "synthetic-label", "binding": "synthetic-binding", "max_tool_calls": 40,
                   "payload": {"operation": "inspect", "job": {"job_key": "synthetic:role-123"}}, "context": {}}
        # Every factual leaf is sourced from the one observation, exactly as a real read does;
        # only the label's own entry varies, which is what each test below is about.
        label_pointer = "/partial_review/questions/0/label"
        evidence = [{"pointer": pointer, "tool_call_id": "read", "quote": self.PAGE}
                    for pointer, _ in browser.leaves({"partial_review": review})
                    if pointer != label_pointer]
        if quote is not None:
            evidence.append({"pointer": label_pointer, "tool_call_id": "read", "quote": quote})
        raw = {"request_id": request["request_id"], "binding": request["binding"],
               "evidence": evidence, "result_json": json.dumps({"partial_review": review})}
        trace = (json.dumps(event("read", self.OBSERVATION)) + "\n").encode()
        return browser.normalize(request, raw, trace, self.OBSERVED_AT, Path("synthetic-offline-trace.jsonl"))

    def test_a_label_quoted_from_something_the_browser_never_showed_is_recorded(self):
        # 2026-10-07 (item 2): the unobserved quote is recorded as rejected evidence and the
        # label the AI reported is kept (only verify/reconcile unbind on a rejected quote).
        result = self.normalized("Are you authorized to work in the US?",
                                 quote="Are you authorized to work in the US?")
        self.assertEqual(result["partial_review"]["questions"][0]["label"], "Are you authorized to work in the US?")
        self.assertEqual([(entry["pointer"], entry["reason"]) for entry in result["rejected_evidence"]],
                         [("/partial_review/questions/0/label", "APPLICATION_UNOBSERVED_QUOTE")])

    def test_a_sourced_label_survives_normalization_byte_for_byte(self):
        """Including the language it was written in. Translation is the failure being prevented."""
        label = "Besitzen Sie eine Arbeitserlaubnis fur Deutschland?"
        result = self.normalized(label, quote=self.PAGE)
        self.assertEqual(result["partial_review"]["questions"][0]["label"], label)
        self.assertIn({"pointer": "/partial_review/questions/0/label", "tool_call_id": "read",
                       "quote": self.PAGE}, result["browser_evidence"])

    def test_a_null_label_needs_no_evidence_and_is_still_accepted(self):
        """The contract's own escape hatch, and the shape of every read saved before today."""
        result = self.normalized(None, quote=None)
        self.assertIsNone(result["partial_review"]["questions"][0]["label"])


class SavedReadsWithoutALabelAreStillConsumable(unittest.TestCase):
    """Backward compatibility, against real files written before the contract asked for a label."""

    def test_every_excerpt_still_matches_the_file_it_came_from(self):
        """Keeps the fixtures above honest: each excerpt equals its fixture file."""
        checked = 0
        for fixture in SAVED:
            actual = saved_questions(fixture)
            if actual is None:
                continue
            checked += 1
            digest, questions = actual
            with self.subTest(request=fixture["request"]):
                self.assertEqual(digest, fixture["sha256"])
                self.assertEqual(questions, fixture["questions"])
        self.assertEqual(checked, len(SAVED), "a saved read named above is missing from the fixtures")

    def test_the_reads_that_matter_carry_no_label_at_all(self):
        for fixture in (INITRODE, VANDELAY):
            for index, question in enumerate(fixture["questions"]):
                with self.subTest(request=fixture["request"], index=index):
                    self.assertNotIn("label", question)

    def test_a_missing_label_never_crashes_the_key_derivation(self):
        for fixture in SAVED:
            for index, question in enumerate(fixture["questions"]):
                with self.subTest(request=fixture["request"], index=index):
                    key = applications.question_field(question)
                    self.assertIsInstance(key, str)
                    self.assertTrue(key.strip())

    def test_the_derived_keys_are_stable_across_re_reads_of_the_same_saved_result(self):
        """An owner's saved answer has to still match on the next cycle."""
        for fixture in SAVED:
            first = [applications.question_field(question) for question in fixture["questions"]]
            second = [applications.question_field(json.loads(json.dumps(question))) for question in fixture["questions"]]
            with self.subTest(request=fixture["request"]):
                self.assertEqual(first, second)

    def test_no_two_questions_in_one_saved_read_collide_on_their_key(self):
        """The `id` argument, measured on the real corpus rather than asserted.

        28 of 58 required questions across the nine real forms carry `"id": null`, and no two
        questions anywhere in the corpus collide on the derived key - so a non-null `id` is
        robustness, not the safety change. See AC/FIX-REPORT.md.
        """
        for fixture in SAVED:
            keys = [applications.question_field(question) for question in fixture["questions"]]
            with self.subTest(request=fixture["request"]):
                self.assertEqual(len(keys), len(set(keys)))

    def test_resolve_answers_consumes_a_labelless_saved_read_without_raising(self):
        policy = {"answers": {"us_work_authorized": US_AUTHORIZATION}}
        answers, missing = applications.resolve_answers(INITRODE["questions"], policy)
        self.assertIsInstance(answers, dict)
        self.assertIsInstance(missing, list)
        self.assertTrue(all(isinstance(field, str) for field in missing))

    def test_a_missing_label_is_never_evidence_that_the_jurisdiction_was_checked(self):
        """A recorded authorization answer is never bound by semantic alone.

        Since 2026-10-07 (item 6) Python applies no recorded yes/no or authorization fact by
        semantic: the answering AI reads the question. So a question of unknown jurisdiction
        stays unresolved for it.
        """
        field = applications.question_field(AUTHORIZATION_QUESTION)
        policy = {"answers": {"us_work_authorized": US_AUTHORIZATION}}
        answers, missing = applications.resolve_answers([AUTHORIZATION_QUESTION], policy)
        self.assertEqual(answers, {})
        self.assertEqual(missing, [field])


if __name__ == "__main__":
    unittest.main()
