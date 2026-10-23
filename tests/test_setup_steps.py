"""Setup page steps (E4.T1 option C, E6.T0 spec 3.2): the plain step functions, offline.

Standard library only (no dashboard packages): every network call goes through a fake.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import fake_executables
import json
import os
import stat
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import license_fixtures
import run_locks
import setup_steps

# A private file's mode: Windows keeps only the read-only bit (0o666 when writable); its privacy
# there is the ACL of the user's profile folder, which the home inherits (VM pass, 2026-10-23).
PRIVATE = 0o666 if os.name == "nt" else 0o600
# The fake Codex's file name (tests/fake_executables.stub: a .cmd on Windows).
CODEX = "codex.cmd" if os.name == "nt" else "codex"


CONFIG = {"text_model": {"model": "test/model"}}
OK_ANSWER = json.dumps({"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}], "usage": {}})
SECRET_KEY = "sk-or-v1-secret-test-key"


def valid_form(**overrides):
    form = {"given_name": "Alex", "family_name": "Example", "email": "Alex.Example@example.com", "phone": "+1 (555) 010-0100",
            "linkedin": "https://www.linkedin.com/in/alex-example/", "current_location": ["Washington, DC, United States"],
            "roles": ["Software Engineering"], "levels": ["Senior", "Staff / Principal"], "industries": ["AI"],
            "countries": ["United States"], "funding_stages": ["Seed", "Series A"], "minimum_salary": ["$180,000"], "desired_salary": ["$250,000"],
            "work_modes": ["Remote", "Hybrid"], "places": ["Anywhere in United States"],
            "work_authorization": ["United States: allowed to work, no sponsorship needed", "Germany: not allowed to work, needs sponsorship"],
            "notice_period": "2 weeks", "start_weeks": "4", "gender": "Prefer not to say", "race": "Prefer not to say",
            "veteran": "Prefer not to say", "disability": "Prefer not to say"}
    form.update(overrides)
    return form


class SetupStepsTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.home = Path(self.directory.name)
        signing = license_fixtures.use_test_key()
        signing.start()
        self.addCleanup(signing.stop)

    def tearDown(self):
        self.directory.cleanup()

    def mode(self, name):
        return stat.S_IMODE((self.home / name).stat().st_mode)

    # --- step 1 ---
    def openrouter(self, status, body):
        calls = []

        def transport(url, data, headers, timeout):
            calls.append(headers["Authorization"])
            if isinstance(status, Exception):
                raise status
            return status, body.encode()
        state = setup_steps.save_and_test_openrouter(SECRET_KEY, CONFIG, self.home, transport=transport)
        return state["steps"]["openrouter"], calls

    def test_openrouter_key_is_saved_privately_and_tested_with_that_key(self):
        step, calls = self.openrouter(200, OK_ANSWER)
        self.assertEqual((step["status"], step["message"]), ("done", "Key works."))
        self.assertEqual(calls, ["Bearer " + SECRET_KEY])
        self.assertEqual((self.home / "openrouter.key").read_text().strip(), SECRET_KEY)
        self.assertEqual(self.mode("openrouter.key"), PRIVATE)
        self.assertNotIn(SECRET_KEY, (self.home / setup_steps.STATE_FILE).read_text())

    def test_openrouter_failures_in_plain_words(self):
        self.assertEqual(self.openrouter(401, '{"error": "no"}')[0]["message"], "OpenRouter did not accept this key.")
        self.assertIn("no credits left", self.openrouter(402, '{"error": "no"}')[0]["message"])
        self.assertIn("Could not reach OpenRouter", self.openrouter(OSError("down"), "")[0]["message"])
        self.assertIn("Could not reach OpenRouter", self.openrouter(TimeoutError(), "")[0]["message"])

    # --- step 2 ---
    def reading(self, file_name="My Résumé.pdf", data=b"%PDF-1.4 test"):
        """Upload with the background read captured (not run); returns (state, run the read)."""
        spawned = []
        with mock.patch.object(setup_steps, "_spawn", spawned.append):
            state = setup_steps.save_resume(file_name, data, CONFIG, self.home)
        return state, (spawned[0] if spawned else None)

    def test_resume_upload_starts_a_background_read(self):
        state, read = self.reading()
        step = state["steps"]["resume"]
        self.assertEqual((step["status"], step["message"]), ("reading", setup_steps.READING_MESSAGE))
        saved = Path(step["raw"]["path"])
        self.assertEqual(saved.read_bytes(), b"%PDF-1.4 test")
        self.assertEqual(stat.S_IMODE(saved.stat().st_mode), PRIVATE)
        calls = []
        with mock.patch.object(setup_steps.resume_intake, "ingest",
                               side_effect=lambda config, path: calls.append(path) or
                               {"state": "READY", "claims": 7, "page_count": 2}):
            read()
        self.assertEqual(calls, [str(saved)])
        step = setup_steps.load_state(self.home)["steps"]["resume"]
        self.assertEqual((step["status"], step["claims"], step["pages"]), ("done", 7, 2))
        self.assertIn("7 sentences", step["message"])
        self.assertTrue(setup_steps.progress(self.home)["done"][2])

    def test_one_read_at_a_time(self):
        state, read = self.reading()
        refused, again = self.reading("other.pdf", b"%PDF-other")
        self.assertIsNone(again)
        self.assertEqual(refused["steps"]["resume"]["notice"], setup_steps.STILL_READING)
        self.assertEqual(refused["steps"]["resume"]["status"], "reading")
        self.assertEqual(Path(state["steps"]["resume"]["raw"]["path"]).read_bytes(), b"%PDF-1.4 test")
        self.assertEqual(setup_steps.start_resume_read(CONFIG, self.home)["steps"]["resume"]["notice"], setup_steps.STILL_READING)

    def test_a_failed_read_keeps_the_file_and_tries_again_without_upload(self):
        state, read = self.reading()
        with mock.patch.object(setup_steps.resume_intake, "ingest",
                               return_value={"state": "FAILED", "code": "NO_CLAIMS", "reason": "No sentences found."}):
            read()
        step = setup_steps.load_state(self.home)["steps"]["resume"]
        self.assertEqual((step["status"], step["message"]), ("failed", "No sentences found."))
        spawned = []
        with mock.patch.object(setup_steps, "_spawn", spawned.append):
            again = setup_steps.start_resume_read(CONFIG, self.home)
        self.assertEqual(again["steps"]["resume"]["status"], "reading")
        self.assertEqual(again["steps"]["resume"]["raw"], state["steps"]["resume"]["raw"])
        with mock.patch.object(setup_steps.resume_intake, "ingest", side_effect=RuntimeError("boom")):
            spawned[0]()
        self.assertEqual(setup_steps.load_state(self.home)["steps"]["resume"]["status"], "failed")

    def test_a_read_lost_to_a_restart_becomes_interrupted(self):
        state, read = self.reading()
        setup_steps._reads.discard(state["steps"]["resume"]["read_id"])  # as after a restart
        step = setup_steps.progress(self.home)["state"]["steps"]["resume"]
        self.assertEqual((step["status"], step["message"], step["reason"]), ("failed", setup_steps.INTERRUPTED, "interrupted"))
        self.assertTrue(Path(step["raw"]["path"]).is_file())
        spawned = []
        with mock.patch.object(setup_steps, "_spawn", spawned.append):
            setup_steps.start_resume_read(CONFIG, self.home)  # [Try again] reads the kept file
        with mock.patch.object(setup_steps.resume_intake, "ingest", return_value={"state": "READY", "claims": 1}):
            read()  # the old read finishing late never overwrites the newer one
        self.assertEqual(setup_steps.load_state(self.home)["steps"]["resume"]["status"], "reading")

    def test_resume_refuses_other_files_and_keeps_a_read_resume(self):
        self.assertEqual(self.reading("notes.txt", b"x")[0]["steps"]["resume"]["notice"], setup_steps.resume_intake.UNSUPPORTED)
        self.assertIsNone(self.reading("old.doc", b"x")[1])
        state, read = self.reading()
        with mock.patch.object(setup_steps.resume_intake, "ingest", return_value={"state": "READY", "claims": 3}):
            read()
        kept = self.reading("a.pdf", b"")[0]["steps"]["resume"]
        self.assertEqual((kept["status"], kept["notice"]), ("done", "We could not read this file: it is empty."))

    def test_step_3_save_keeps_the_resume_and_waits_for_an_intake_write(self):
        (self.home / "profile.json").write_text(json.dumps({"resume": {"path": "/a.pdf"}, "resume_raw": {"path": "/r.pdf"}}))
        finished = threading.Event()
        with mock.patch.dict(os.environ, {"SIDEDOOR_HOME": str(self.home)}):
            with setup_steps.resume_intake.PROFILE_LOCK:
                thread = threading.Thread(target=lambda: (setup_steps.save_profile(valid_form(), self.home), finished.set()))
                thread.start()
                self.assertFalse(finished.wait(0.3))
            self.assertTrue(finished.wait(5))
        profile = json.loads((self.home / "profile.json").read_text())
        self.assertEqual((profile["resume"], profile["resume_raw"]), ({"path": "/a.pdf"}, {"path": "/r.pdf"}))

    # --- step 3 ---
    def test_pick_lists_match_by_prefix(self):
        self.assertIn("Software Engineering", setup_steps.matches("roles", "sof"))
        self.assertNotIn("Software Engineering", setup_steps.matches("roles", "eng"))
        self.assertTrue(all(label.lower().startswith("uni") for label in setup_steps.matches("countries", "Uni")))

    def test_owner_pick_lists_for_any_job_seeker(self):
        industries = setup_steps.options("industries")
        self.assertEqual(len(industries), 60)
        for name in ("Accounting", "Hospitals and Health Care", "Venture Capital and Private Equity", "Farming", "AI"):
            self.assertIn(name, industries)
        self.assertNotIn("Developer Tools", industries)
        self.assertEqual(setup_steps.options("levels"), [
            "Intern", "Entry-level / Junior", "Associate", "Mid-level", "Senior", "Lead", "Staff / Principal",
            "Distinguished / Fellow", "Manager", "Senior Manager", "Director", "Senior Director", "Vice President",
            "Head of", "C-level (CEO, CFO, CTO …)"])
        roles = setup_steps.options("roles")
        self.assertGreaterEqual(len(roles), 80)
        for name in ("Nursing", "Product Management", "Teaching", "Culinary / Chef", "Social Work", "Backend Engineering"):
            self.assertIn(name, roles)
        for role in roles:
            if role == "Chief of Staff":  # listed by the owner; "Staff" is not a level there
                continue
            self.assertFalse({"Senior", "Junior", "Staff", "Principal", "Lead", "Intern"} & set(role.split()), role)
        _, errors = setup_steps.save_profile(valid_form(roles=["Nursing", "Teaching"], levels=["Manager", "Director"],
                                                        industries=["Hospitals and Health Care", "Elder Care"]), self.home)
        self.assertEqual(errors, {})
        profile = json.loads((self.home / "profile.json").read_text())
        self.assertEqual(profile["levels"], ["Manager Nursing", "Manager Teaching", "Director Nursing", "Director Teaching"])
        self.assertEqual(profile["industries"], ["Hospitals and Health Care", "Elder Care"])

    def test_profile_is_written_in_the_example_shape(self):
        _, errors = setup_steps.save_profile(valid_form(), self.home)
        self.assertEqual(errors, {})
        profile = json.loads((self.home / "profile.json").read_text())
        answers = profile["answers"]
        self.assertEqual(answers["email"]["value"], "Alex.Example@example.com")
        self.assertEqual(answers["email"]["semantic"], "contact_email")
        self.assertEqual(answers["current_city"]["value"], "Washington")
        self.assertEqual(answers["work_authorization_by_jurisdiction"]["value"],
                         {"*": "NOT_AUTHORIZED", "US": "AUTHORIZED_NO_SPONSORSHIP_REQUIRED", "DE": "NOT_AUTHORIZED"})
        self.assertIs(answers["requires_visa_sponsorship_us"]["value"], False)
        self.assertEqual(answers["earliest_start_date_rule"]["value"]["offset_days"], 28)
        self.assertEqual(profile["countries"], ["US"])
        self.assertEqual(profile["levels"], ["Senior Software Engineering", "Staff / Principal Software Engineering"])
        self.assertEqual((profile["minimum_base_salary_usd"], profile["desired_base_salary_usd"]), (180000, [180000, 250000]))
        self.assertEqual(answers["phone"]["value"], "+1 (555) 010-0100")
        self.assertEqual(answers["phone"]["semantic"], "contact_phone")
        self.assertEqual(self.mode("profile.json"), PRIVATE)

    def test_phone_and_linkedin_are_required(self):
        state, errors = setup_steps.save_profile(valid_form(phone="", linkedin=""), self.home)
        self.assertEqual((errors["phone"], errors["linkedin"]), ("Required.", "Required."))
        self.assertFalse(setup_steps.step_done(3, state, self.home))
        self.assertFalse((self.home / "profile.json").exists())
        _, errors = setup_steps.save_profile(valid_form(phone="call me", linkedin="https://example.com/me"), self.home)
        self.assertEqual(set(errors), {"phone", "linkedin"})
        _, errors = setup_steps.save_profile(valid_form(phone="---"), self.home)
        self.assertEqual(set(errors), {"phone"})  # no digit
        state, errors = setup_steps.save_profile(valid_form(), self.home)
        self.assertEqual(errors, {})
        self.assertTrue(setup_steps.step_done(3, state, self.home))
        answers = json.loads((self.home / "profile.json").read_text())["answers"]
        self.assertEqual(answers["linkedin"]["value"], "https://www.linkedin.com/in/alex-example/")

    def test_common_linkedin_pastes_are_normalized(self):
        cases = {
            "https://www.linkedin.com/in/jane-doe-123/?utm_source=share": "https://www.linkedin.com/in/jane-doe-123/",
            "UK.LinkedIn.com/in/JaneDoe": "https://www.linkedin.com/in/JaneDoe/",
            "linkedin.com/in/jane": "https://www.linkedin.com/in/jane/",
            "http://de.linkedin.com/in/jane#about": "https://www.linkedin.com/in/jane/",
            "  https://m.linkedin.com/in/jane?utm_source=share&utm_medium=member_ios  ": "https://www.linkedin.com/in/jane/",
            "https://www.linkedin.com/in/jane/details/experience/": "https://www.linkedin.com/in/jane/",
            "linkedin.com/in/jane/overlay/contact-info/": "https://www.linkedin.com/in/jane/",
            "https://www.linkedin.com/in/j%C3%A9r%C3%B4me-x/": "https://www.linkedin.com/in/j%C3%A9r%C3%B4me-x/",
        }
        for typed, expected in cases.items():
            self.assertEqual(setup_steps.linkedin_profile(typed), expected, typed)
        for typed in ("linkedin.com/company/acme", "https://example.com/in/jane", "linkedin.com/in/", "linkedin.com/in//x",
                      "notlinkedin.com/in/jane", "https://user@linkedin.com/in/jane", "ftp://linkedin.com/in/jane",
                      "a.b.linkedin.com/in/jane", "linkedin.com/in/jane doe", ""):
            self.assertIsNone(setup_steps.linkedin_profile(typed), typed)
        setup_steps.save_profile(valid_form(linkedin="UK.LinkedIn.com/in/JaneDoe?utm_source=share"), self.home)
        answers = json.loads((self.home / "profile.json").read_text())["answers"]
        self.assertEqual(answers["linkedin"]["value"], "https://www.linkedin.com/in/JaneDoe/")

    def test_sentence_punctuation_is_not_part_of_the_slug(self):
        for typed, expected in (("linkedin.com/in/jane.", "jane"), ("linkedin.com/in/jane)", "jane"),
                                ("linkedin.com/in/jane,", "jane"), ("linkedin.com/in/jane.doe-1", "jane.doe-1"),
                                ("linkedin.com/in/jane%C3%A9", "jane%C3%A9"), ("linkedin.com/in/Jane!\u201d", "Jane")):
            self.assertEqual(setup_steps.linkedin_profile(typed), "https://www.linkedin.com/in/" + expected + "/", typed)
        for typed in ("linkedin.com/in/..", "linkedin.com/in/.", "linkedin.com/in/%20", "linkedin.com/in/%09%20", "linkedin.com/in/.)"):
            self.assertIsNone(setup_steps.linkedin_profile(typed), typed)
        state, errors = setup_steps.save_profile(valid_form(linkedin="linkedin.com/in/.."), self.home)
        self.assertIn("linkedin", errors)
        self.assertEqual(state["steps"]["profile"]["form"]["linkedin"], "linkedin.com/in/..")

    def test_common_phone_formats_are_saved_as_typed(self):
        for typed, saved in (("202.555.0143", "202.555.0143"), ("(202) 555-0143 x12", "(202) 555-0143 x12"),
                             ("+1 202 555 0143 ext. 12", "+1 202 555 0143 ext. 12"), ("202/555/0143", "202/555/0143"),
                             ("\u00a0+1\u00a0202\u2009555\u202f0143 ", "+1 202 555 0143")):
            state, errors = setup_steps.save_profile(valid_form(phone=typed), self.home)
            self.assertEqual(errors, {}, typed)
            self.assertEqual(json.loads((self.home / "profile.json").read_text())["answers"]["phone"]["value"], saved)
        for typed in ("call me", "ext. 12", "x"):
            self.assertIsNone(setup_steps.clean_phone(typed), typed)

    def test_a_refused_value_stays_in_the_box(self):
        state, errors = setup_steps.save_profile(valid_form(phone="call me", linkedin="linkedin.com/company/acme"), self.home)
        self.assertEqual(set(errors), {"phone", "linkedin"})
        form = state["steps"]["profile"]["form"]
        self.assertEqual((form["phone"], form["linkedin"]), ("call me", "linkedin.com/company/acme"))
        self.assertEqual(setup_steps.profile_form(self.home)["linkedin"], "linkedin.com/company/acme")

    def test_profile_errors_name_the_field(self):
        _, errors = setup_steps.save_profile(valid_form(email="not an email", levels=["Typed by hand"], given_name=""), self.home)
        self.assertEqual(set(errors), {"email", "levels", "given_name"})
        self.assertFalse((self.home / "profile.json").exists())

    def test_application_email_is_locked_after_the_check_passed(self):
        setup_steps.update_state(lambda state: state.update(email_check_passed_at="x", locked_email="a@example.com"), self.home)
        setup_steps.save_profile(valid_form(email="other@example.com"), self.home)
        self.assertEqual(json.loads((self.home / "profile.json").read_text())["answers"]["email"]["value"], "a@example.com")

    # --- step 4 ---
    def test_mode_defaults_to_automatic(self):
        self.assertEqual(setup_steps.load_state(self.home)["mode"], "automatic")
        self.assertEqual(setup_steps.set_mode("review", self.home)["mode"], "review")
        with self.assertRaisesRegex(ValueError, "MODE_UNKNOWN"):  # refused, nothing written (E6.T2 P6)
            setup_steps.set_mode("anything", self.home)
        self.assertEqual(setup_steps.load_state(self.home)["mode"], "review")
        self.assertTrue(setup_steps.progress(self.home)["done"][4])

    # --- step 5 ---
    def activate(self, status, body):
        sent = []

        def post(url, payload):
            sent.append((url, payload))
            if isinstance(status, Exception):
                raise status
            return status, json.dumps(body).encode()
        state = setup_steps.activate_license("SD-TESTKEY", self.home, "https://relay.test", post=post)
        return state["steps"]["license"], sent

    def test_license_activation_stores_key_and_entitlement(self):
        now = license_fixtures.dt.datetime.now(license_fixtures.dt.timezone.utc)
        body = license_fixtures.signed("SD-TESTKEY", setup_steps.machine_id(self.home), now, status="trialing")
        step, sent = self.activate(200, body)
        self.assertEqual(step["status"], "done")
        self.assertIn("Active: Trial", step["message"])
        self.assertEqual(sent[0][0], "https://relay.test/license/activate")
        self.assertEqual(sent[0][1]["key"], "SD-TESTKEY")
        self.assertEqual(sent[0][1]["machine"], setup_steps.machine_id(self.home))
        self.assertEqual(self.mode("license.key"), PRIVATE)
        self.assertEqual(setup_steps.license_chip(self.home)["label"], "Trial")
        self.assertTrue(setup_steps.license_done(self.home))
        self.assertNotIn("SD-TESTKEY", (self.home / setup_steps.STATE_FILE).read_text())

    def test_an_unsigned_or_foreign_answer_is_not_stored(self):
        now = license_fixtures.dt.datetime.now(license_fixtures.dt.timezone.utc)
        machine = setup_steps.machine_id(self.home)
        forged = dict(license_fixtures.signed("SD-TESTKEY", machine, now), signature="c2ln")
        other_key = license_fixtures.signed("SD-OTHER", machine, now)
        for body in (forged, other_key):
            step, _ = self.activate(200, body)
            self.assertEqual(step["status"], "failed")
            self.assertIn("license server", step["message"])
            self.assertFalse((self.home / "license.key").exists())

    def test_license_failures(self):
        self.assertIn("starting with SD-", self.activate(401, {"error": "LICENSE_INVALID"})[0]["message"])
        self.assertIn("2 computers", self.activate(409, {"error": "ACTIVATION_LIMIT"})[0]["message"])
        self.assertIn("license server", self.activate(OSError("down"), {})[0]["message"])
        self.assertFalse((self.home / "license.key").exists())
        self.assertEqual(setup_steps.license_chip(self.home)["label"], "No license yet")

    # --- step 6 ---
    def codex(self, output, returncode=0, install=True):
        directory = self.home / "bin"
        directory.mkdir(exist_ok=True)
        if install:
            fake_executables.stub(directory / "codex")
        run = mock.Mock(return_value=subprocess.CompletedProcess([], returncode, output, ""))
        with mock.patch.object(setup_steps.Path, "home", return_value=self.home / "nohome"):
            state = setup_steps.check_codex(self.home, run=run, search_path=str(directory))
        return state, run

    def test_codex_signed_in_with_chatgpt_saves_its_path(self):
        state, run = self.codex("Logged in using ChatGPT\n")
        self.assertEqual(state["steps"]["codex"]["message"], "Codex is installed and signed in.")
        self.assertEqual(state["codex_path"], str(self.home / "bin" / CODEX))
        self.assertEqual(run.call_args[0][0], [str(self.home / "bin" / CODEX), "login", "status"])
        self.assertTrue(setup_steps.codex_done(state))

    def test_codex_failures(self):
        self.assertIn("API key", self.codex("Logged in using an API key - sk-***\n")[0]["steps"]["codex"]["message"])
        self.assertEqual(self.codex("Not logged in\n", 1)[0]["steps"]["codex"]["message"], "Codex is installed but not signed in.")
        (self.home / "bin" / CODEX).unlink()
        state, _ = self.codex("", install=False)
        self.assertEqual(state["steps"]["codex"]["message"], "Codex is not installed.")
        self.assertNotIn("codex_path", state)

    def test_codex_is_looked_for_in_local_bin(self):
        with mock.patch.object(setup_steps.Path, "home", return_value=Path("/u")):
            self.assertIn(str(Path("/u/.local/bin") / CODEX), setup_steps.codex_candidates(search_path=""))

    def test_a_known_email_domain_picks_the_webmail(self):
        # Owner, 2026-10-22: "why do i have to still populate it again?"
        setup_steps.save_profile(valid_form(email="alex@gmail.com"), self.home)
        self.assertEqual(setup_steps.load_state(self.home)["webmail"]["label"], "Gmail")
        self.assertEqual(setup_steps.load_state(self.home)["steps"]["webmail"]["status"], "done")

    def test_an_unknown_domain_still_asks_and_im_done_says_so(self):
        setup_steps.save_profile(valid_form(email="alex@acme-corp.example"), self.home)
        self.assertIsNone(setup_steps.load_state(self.home).get("webmail"))
        state = setup_steps.browser_closed(self.home)
        self.assertIn("choose your webmail", state["steps"]["browser"]["message"])
        self.assertEqual(state["steps"]["browser"]["status"], "done")

    def test_a_chosen_webmail_is_never_replaced(self):
        setup_steps.webmail_choice("Outlook.com", "", self.home)
        setup_steps.save_profile(valid_form(email="alex@gmail.com"), self.home)
        self.assertEqual(setup_steps.load_state(self.home)["webmail"]["label"], "Outlook.com")

    # --- step 7 ---
    def licensed(self):
        (self.home / "license.key").write_text("SD-TESTKEY\n")

    def accounts(self, status, body):
        return setup_steps.linkedin_status(self.home, transport=lambda method, path: (status, body))["steps"]["linkedin"]

    def test_linkedin_offers_connect_only_when_nothing_is_linked(self):
        self.licensed()
        self.assertEqual(self.accounts(403, {"error": "LICENSE_NOT_LINKED"})["offer"], "connect")
        self.assertEqual(self.accounts(200, {"items": []})["offer"], "connect")
        listed = self.accounts(200, {"items": [{"type": "LINKEDIN", "name": "Alex", "status": "CREDENTIALS"}]})
        self.assertEqual((listed["status"], listed["offer"]), ("needs_you", "reconnect"))
        ok = self.accounts(200, {"items": [{"type": "LINKEDIN", "name": "Alex", "sources": [{"status": "OK"}]}]})
        self.assertEqual((ok["status"], ok["message"]), ("done", "LinkedIn connected: Alex."))
        self.assertIn("subscription", self.accounts(401, {"error": "LICENSE_INVALID"})["message"])

    def test_a_lost_unipile_session_offers_reconnect_not_retry(self):
        # Owner, 2026-10-22: step 7 showed "answered HTTP 503, try again" and no Reconnect button
        # when Unipile said the linked account had no session.
        self.licensed()
        lost = self.accounts(503, {"status": 503, "type": "errors/no_client_session", "title": "No client session"})
        self.assertEqual((lost["status"], lost["offer"]), ("needs_you", "reconnect"))
        self.assertEqual(self.accounts(503, {"error": "UPSTREAM"})["offer"], "retry")

    def test_linkedin_link_waits_and_never_stores_the_link(self):
        self.licensed()
        made = []
        state, url = setup_steps.linkedin_link("create", self.home, make_link=lambda relay, key, kind: made.append(kind) or "https://unipile.test/secret-dsn")
        self.assertEqual((made, url, state["steps"]["linkedin"]["offer"]), (["create"], "https://unipile.test/secret-dsn", "waiting"))
        self.assertNotIn("secret-dsn", (self.home / setup_steps.STATE_FILE).read_text())
        self.assertEqual(self.accounts(403, {"error": "LICENSE_NOT_LINKED"})["offer"], "waiting")
        setup_steps.update_state(lambda value: value.update(linkedin_link_started_at="2000-01-01T00:00:00Z"), self.home)
        self.assertIn("not linked to your license yet", self.accounts(403, {"error": "LICENSE_NOT_LINKED"})["message"])

    # --- step 9 ---
    def test_email_check_compares_after_lowercasing_only(self):
        self.assertTrue(setup_steps.compare_mailboxes("Alex.Example@Example.com", "alex.example@example.com"))
        self.assertFalse(setup_steps.compare_mailboxes("alex.example@example.com", "alexexample@example.com"))
        self.assertFalse(setup_steps.compare_mailboxes("alex@example.com", "alex+jobs@example.com"))

    def test_email_check_locks_on_first_pass_and_never_unlocks(self):
        setup_steps.save_profile(valid_form(), self.home)
        # Without Sidedoor's browser runtime (E7.T1) installed, the check says so.
        with mock.patch.dict(os.environ, {"SIDEDOOR_BROWSER_RUNTIME": str(self.home / "no-runtime")}):
            self.assertEqual(setup_steps.run_email_check(self.home)["steps"]["email"]["status"], setup_steps.NOT_BUILT)
        failed = setup_steps.run_email_check(self.home, read=lambda *args: "someone@example.com")["steps"]["email"]
        self.assertEqual((failed["status"], failed["reason"]), ("failed", "mismatch"))
        passed = setup_steps.run_email_check(self.home, read=lambda *args: "alex.example@example.com")
        self.assertEqual(passed["locked_email"], "Alex.Example@example.com")
        later = setup_steps.run_email_check(self.home, read=lambda *args: "someone@example.com")
        self.assertTrue(later["steps"]["email"]["mismatch"])
        self.assertTrue(setup_steps.progress(self.home)["done"][9])

    def test_webmail_choice_and_lock(self):
        self.assertEqual(setup_steps.webmail_choice("Gmail", "", self.home)["webmail"]["url"], "https://mail.google.com/")
        # "Other" and its address box were removed (owner, 2026-10-22: "its confusing").
        self.assertEqual(setup_steps.webmail_choice("Other", "https://mail.example.com/", self.home)["steps"]["webmail"]
                         ["message"], "Pick your webmail from the list.")
        setup_steps.update_state(lambda state: state.update(email_check_passed_at="x"), self.home)
        self.assertEqual(setup_steps.webmail_choice("Outlook.com", "", self.home)["webmail"]["label"], "Gmail")

    # --- review round 1 ---
    def test_reconnect_keeps_waiting_while_the_account_is_listed_but_not_ok(self):
        self.licensed()
        state, url = setup_steps.linkedin_link("reconnect", self.home, make_link=lambda relay, key, kind: "https://u.test/r")
        self.assertEqual(state["steps"]["linkedin"]["kind"], "reconnect")
        listed = {"items": [{"type": "LINKEDIN", "name": "Alex", "status": "CREDENTIALS"}]}
        waiting = self.accounts(200, listed)
        self.assertEqual((waiting["status"], waiting["offer"], waiting["kind"]), ("checking", "waiting", "reconnect"))
        setup_steps.update_state(lambda value: value.update(linkedin_link_started_at="2000-01-01T00:00:00Z"), self.home)
        late = self.accounts(200, listed)
        self.assertEqual(late["offer"], "reconnect")
        self.assertIn("LinkedIn still needs you to sign in. Click Reconnect again", late["message"])
        # A reconnect wait never turns into a create wait when the account is gone.
        setup_steps.update_state(lambda value: value.update(linkedin_link_started_at=setup_steps.now_iso()), self.home)
        self.assertEqual(self.accounts(200, {"items": []})["offer"], "connect")

    def test_authorization_answers_follow_the_current_picks_only(self):
        setup_steps.save_profile(valid_form(), self.home)
        _, errors = setup_steps.save_profile(valid_form(work_authorization=["Germany: not allowed to work, needs sponsorship"]), self.home)
        self.assertEqual(errors, {})
        answers = json.loads((self.home / "profile.json").read_text())["answers"]
        self.assertEqual(answers["work_authorization_by_jurisdiction"]["value"], {"*": "NOT_AUTHORIZED", "DE": "NOT_AUTHORIZED"})
        self.assertNotIn("us_work_authorized", answers)
        self.assertNotIn("requires_visa_sponsorship_us", answers)

    def test_each_authorization_choice_derives_true_answers(self):
        cases = (("allowed to work, no sponsorship needed", "AUTHORIZED_NO_SPONSORSHIP_REQUIRED", True, False),
                 ("allowed to work now, will need sponsorship", "AUTHORIZED_SPONSORSHIP_REQUIRED", True, True),
                 ("not allowed to work, needs sponsorship", "NOT_AUTHORIZED", False, True))
        for choice, status, authorized, sponsorship in cases:
            _, errors = setup_steps.save_profile(valid_form(work_authorization=["United States: " + choice]), self.home)
            self.assertEqual(errors, {})
            answers = json.loads((self.home / "profile.json").read_text())["answers"]
            self.assertEqual(answers["work_authorization_by_jurisdiction"]["value"]["US"], status)
            self.assertIs(answers["us_work_authorized"]["value"], authorized, choice)
            self.assertIs(answers["requires_visa_sponsorship_us"]["value"], sponsorship, choice)

    def test_a_pre_existing_profile_cannot_contradict_the_picks(self):
        example = json.loads((Path(setup_steps.ROOT) / "profile.example.json").read_text())
        (self.home / "profile.json").write_text(json.dumps(example))
        setup_steps.save_profile(valid_form(work_authorization=[
            "United States: not allowed to work, needs sponsorship",
            "EU - Any member country: allowed to work now, will need sponsorship",
            "United Kingdom: allowed to work, no sponsorship needed"]), self.home)
        answers = json.loads((self.home / "profile.json").read_text())["answers"]
        self.assertNotIn("us_permanent_resident", answers)
        self.assertNotIn("requires_visa_sponsorship_outside_us", answers)
        self.assertEqual(answers["work_authorization_by_jurisdiction"]["value"],
                         {"*": "NOT_AUTHORIZED", "US": "NOT_AUTHORIZED", "EU": "AUTHORIZED_SPONSORSHIP_REQUIRED",
                          "GB": "AUTHORIZED_NO_SPONSORSHIP_REQUIRED"})
        self.assertIs(answers["eu_work_authorized"]["value"], True)
        self.assertIs(answers["uk_work_authorized"]["value"], True)
        self.assertIs(answers["us_work_authorized"]["value"], False)
        self.assertEqual(answers["github"], example["answers"]["github"])  # answers Setup does not own stay
        # Without an EU or UK line their answers are gone; with US "allowed, no sponsorship" the
        # recorded permanent residence stays as it is.
        example["answers"]["us_permanent_resident"]["value"] = True
        (self.home / "profile.json").write_text(json.dumps(example))
        setup_steps.save_profile(valid_form(work_authorization=["United States: allowed to work, no sponsorship needed"]), self.home)
        answers = json.loads((self.home / "profile.json").read_text())["answers"]
        self.assertNotIn("eu_work_authorized", answers)
        self.assertNotIn("uk_work_authorized", answers)
        self.assertEqual(answers["us_permanent_resident"], example["answers"]["us_permanent_resident"])

    def test_one_eu_choice_like_any_country(self):
        labels = [label for label in setup_steps.options("work_authorization") if label.startswith("EU - Any member country:")]
        self.assertEqual(len(labels), 3)
        self.assertTrue(all(setup_steps.option("work_authorization", label)["code"] == "EU" for label in labels))
        _, errors = setup_steps.save_profile(valid_form(work_authorization=labels[:2]), self.home)
        self.assertEqual(list(errors), ["work_authorization"])

    def test_funding_stage_is_asked_pick_only_and_saved_as_picked(self):
        setup_steps.save_profile(valid_form(funding_stages=["Seed", "Series D+", "Public company"]), self.home)
        self.assertEqual(json.loads((self.home / "profile.json").read_text())["funding_stages"],
                         ["Seed", "Series D+", "Public company"])
        self.assertEqual(setup_steps.form_from_profile(setup_steps.read_profile(self.home))["funding_stages"],
                         ["Seed", "Series D+", "Public company"])
        setup_steps.save_profile(valid_form(funding_stages=["Any stage"]), self.home)
        self.assertEqual(json.loads((self.home / "profile.json").read_text())["funding_stages"], [])
        self.assertEqual(setup_steps.form_from_profile(setup_steps.read_profile(self.home))["funding_stages"], ["Any stage"])
        with mock.patch.dict(os.environ, {"SIDEDOOR_HOME": str(self.home)}):
            self.assertEqual(setup_steps.user_profile.load()["funding_stages"], [])
        for stages in ([], ["Series Z"]):
            _, errors = setup_steps.save_profile(valid_form(funding_stages=stages), self.home)
            self.assertEqual(list(errors), ["funding_stages"])

    def test_prefill_shows_the_same_picks_after_setup_state_is_gone(self):
        form = valid_form(roles=["Nursing", "Quantum Whisperer"], levels=["Manager", "Director"],
                          funding_stages=["Any stage"], current_location=["Ibadan, Oyo, Nigeria"])
        setup_steps.save_profile(form, self.home)
        profile = json.loads((self.home / "profile.json").read_text())
        self.assertEqual(profile["levels"], ["Manager Nursing", "Manager Quantum Whisperer",
                                             "Director Nursing", "Director Quantum Whisperer"])
        (self.home / setup_steps.STATE_FILE).unlink()
        prefill = setup_steps.profile_form(self.home)
        for field in ("roles", "levels", "work_modes", "places", "funding_stages"):
            self.assertEqual(prefill[field], form[field], field)
        self.assertEqual(prefill["current_location"], "Ibadan, Oyo, Nigeria")
        _, errors = setup_steps.save_profile(prefill, self.home)
        self.assertEqual(errors, {})
        self.assertEqual(json.loads((self.home / "profile.json").read_text()), profile)
        with mock.patch.dict(os.environ, {"SIDEDOOR_HOME": str(self.home)}):
            policy = setup_steps.user_profile.load()
        import decision_transport
        import parallel_research
        self.assertEqual(parallel_research.research_policy(policy)["unclassified"], {})
        self.assertIn("setup_picks", decision_transport.CLASSIFIED_POLICY_KEYS)

    def test_prefill_never_iterates_a_string(self):
        form = setup_steps.form_from_profile({"industries": "AI", "countries": "US", "funding_stages": "Series E"})
        self.assertEqual((form["industries"], form["countries"], form["funding_stages"]),
                         (["AI"], ["United States"], ["Series D+"]))

    def test_old_stage_spellings_prefill_as_series_d_plus(self):
        form = setup_steps.form_from_profile({"funding_stages": ["Seed", "Series D", "Series E", "Series Z"]})
        self.assertEqual(form["funding_stages"], ["Seed", "Series D+"])

    def test_discovery_accepts_any_stage_and_the_new_labels(self):
        import json as json_module
        import industry_discovery
        for stages in ([], ["Pre-seed", "Series D+", "Public company", "Nonprofit", "Government"], None):
            policy = {"industries": ["AI"]}
            if stages is not None:
                policy["funding_stages"] = stages
            prompt = industry_discovery.inputs(setup_steps.ROOT, policy, "2026-10-21T00:00:00Z", 5)
            scope = json_module.loads(prompt.rsplit("\n\n", 1)[1])
            self.assertEqual(scope["funding_stages"], stages or [])
        self.assertIn("an empty list means the user named none", " ".join(prompt.split()))
        self.assertNotIn("Seed through Series D", prompt)
        with self.assertRaises(ValueError):
            industry_discovery.inputs(setup_steps.ROOT, {"industries": ["AI"], "funding_stages": "Seed"},
                                      "2026-10-21T00:00:00Z", 5)

    def test_a_first_connect_succeeds_once_the_account_is_listed(self):
        self.licensed()
        setup_steps.linkedin_link("create", self.home, make_link=lambda relay, key, kind: "https://u.test/c")
        listed = self.accounts(200, {"items": [{"type": "LINKEDIN", "name": "Alex", "status": "CONNECTING"}]})
        self.assertEqual((listed["status"], listed["message"]), ("done", "LinkedIn connected: Alex."))

    def test_step_3_prefills_from_an_existing_profile(self):
        example = json.loads((Path(setup_steps.ROOT) / "profile.example.json").read_text())
        (self.home / "profile.json").write_text(json.dumps(example))
        form = setup_steps.profile_form(self.home)
        self.assertEqual((form["phone"], form["linkedin"], form["email"]),
                         ("+1-555-010-0100", "https://www.linkedin.com/in/alex-example/", "alex.example@example.com"))
        self.assertEqual((form["minimum_salary"], form["desired_salary"], form["start_weeks"]), ("$180,000", "$250,000", "4"))
        self.assertIn("United States: allowed to work, no sponsorship needed", form["work_authorization"])
        self.assertIn("United States", form["countries"])
        setup_steps.save_profile(dict(valid_form(), **{key: form[key] for key in ("phone", "desired_salary")}), self.home)
        answers = json.loads((self.home / "profile.json").read_text())["answers"]
        self.assertEqual(answers["phone"]["value"], "+1-555-010-0100")

    def test_scope_prompts_are_not_software_only_and_take_empty_funding_stages(self):
        import application_browser
        research = (Path(setup_steps.ROOT) / "research-prompt.md").read_text()
        inspect = " ".join(application_browser.OPERATION_PROMPTS["inspect"].split()) if hasattr(application_browser, "OPERATION_PROMPTS") else ""
        source = " ".join((Path(setup_steps.ROOT) / "application_browser.py").read_text().split())
        for text in (" ".join(research.split()), source):
            self.assertIn("an empty or missing list means the user named none: any stage is in scope", text)
        self.assertNotIn("software engineering role", research)
        self.assertNotIn("any software engineering specialization", source)
        self.assertIn("in any field, is in scope", source)

    def test_both_statuses_for_one_country_are_refused(self):
        _, errors = setup_steps.save_profile(valid_form(work_authorization=[
            "United States: allowed to work, no sponsorship needed", "United States: not allowed to work, needs sponsorship"]), self.home)
        self.assertIn("one line per country", errors["work_authorization"])

    def test_typed_values_only_where_the_owner_allowed_them(self):
        _, errors = setup_steps.save_profile(valid_form(
            roles=["Quantum  Whisperer"], industries=["Space Mining"], places=["Lagos, Nigeria (hybrid)"],
            current_location=["Ibadan, Oyo, Nigeria"]), self.home)
        self.assertEqual(errors, {})
        profile = json.loads((self.home / "profile.json").read_text())
        self.assertEqual(profile["answers"]["current_location"]["value"], "Ibadan, Oyo, Nigeria")
        self.assertNotIn("current_city", profile["answers"])
        self.assertEqual(profile["industries"], ["Space Mining"])
        self.assertIn("Senior Quantum Whisperer", profile["levels"])
        for field, value in (("levels", ["Grandmaster"]), ("work_modes", ["Underwater"]), ("countries", ["Atlantis"]),
                             ("minimum_salary", ["$1"]), ("work_authorization", ["Atlantis: allowed"])):
            _, errors = setup_steps.save_profile(valid_form(**{field: value}), self.home)
            self.assertEqual(list(errors), [field])
        self.assertIsNone(setup_steps.custom_value("roles", "x" * 121))
        self.assertIsNone(setup_steps.custom_value("levels", "Senior"))

    # --- order and stubs ---
    def test_order_the_code_forces(self):
        locked = setup_steps.progress(self.home)["locked"]
        self.assertEqual((locked[2], locked[7], locked[9], locked[11]), (1, 5, 3, 1))
        self.assertNotIn(10, locked)
        self.assertEqual(setup_steps.progress(self.home)["left"], 8)

    def test_unbuilt_steps_say_so(self):
        import login_item
        with mock.patch.object(login_item, "PLATFORM", "linux"):  # Linux comes later (owner, 2026-10-22)
            self.assertEqual(setup_steps.start_at_login(True, self.home)["steps"]["login"]["status"], "failed")
        # No scheduler loop in this process (only `sidedoor serve` installs one): it says so in plain words.
        self.assertIn("start from this window", setup_steps.start_scheduler(self.home)["steps"]["start"]["message"])
        self.assertFalse(setup_steps.scheduler_running(self.home))


class FakeChrome:
    def __init__(self):
        self.exited = threading.Event()
        self.terminated = False

    def poll(self):
        return 0 if self.exited.is_set() else None

    def wait(self, timeout=None):
        if not self.exited.wait(timeout):
            raise subprocess.TimeoutExpired("chrome", timeout)
        return 0

    def terminate(self):
        self.terminated = True
        self.exited.set()

    kill = terminate


class BrowserSessionTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.chrome = FakeChrome()
        self.launched = []
        self.closed = []
        self.session = setup_steps.BrowserSession(
            self.root, self.root / "profile", find_chrome=lambda: "/chrome",
            launch=lambda command, **named: self.launched.append(command) or self.chrome,
            on_closed=lambda: self.closed.append(True),
            # macOS's bring-forward (osascript), failing as it does for a fake Chrome's pid; on any
            # other system the session would start Chrome again instead (VM pass, 2026-10-23).
            run=lambda *arguments, **named: subprocess.CompletedProcess(arguments, 1), platform="darwin")

    def tearDown(self):
        self.directory.cleanup()

    def lock_is_free(self):
        with run_locks.held(self.root, run_locks.BROWSER) as free:
            return free

    def test_open_holds_the_browser_lock_until_done(self):
        self.assertEqual(self.session.open(), "opened")
        self.assertIn("--user-data-dir=" + str(self.root / "profile"), self.launched[0])
        self.assertFalse(self.lock_is_free())
        self.assertEqual(self.session.open(), "already_open")
        self.assertEqual(len(self.launched), 1)
        ran = []
        self.assertEqual(self.session.close(then=lambda: ran.append(self.lock_is_free()) or "checked"), "checked")
        self.assertEqual(ran, [False])  # the check ran under the dashboard's own hold
        self.assertTrue(self.chrome.terminated)
        self.assertTrue(self.lock_is_free())
        self.assertEqual(self.closed, [True])

    def test_a_chrome_that_exits_releases_the_hold(self):
        self.session.open()
        self.chrome.exited.set()
        self.assertTrue(self.session.closed_once.wait(5))
        self.assertTrue(self.lock_is_free())
        self.assertFalse(self.session.is_open())

    def test_busy_when_a_scheduled_job_holds_the_browser(self):
        with run_locks.held(self.root, run_locks.BROWSER) as held:
            self.assertTrue(held)
            self.assertEqual(self.session.open(), "busy")
            self.assertIsNone(self.session.close(then=lambda: "never"))
        self.assertEqual(self.launched, [])

    @unittest.skipIf(os.name == "nt", "POSIX-only (E2.T3: process groups, rlimits, flock, lsof or symlinks)")
    def test_a_live_leftover_profile_lock_is_unclean(self):
        profile = self.root / "profile"
        profile.mkdir()
        os.symlink("host-%d" % os.getpid(), profile / "SingletonLock")
        self.assertEqual(self.session.open(), "unclean")
        self.assertTrue(self.lock_is_free())
        os.unlink(profile / "SingletonLock")
        os.symlink("host-999999999", profile / "SingletonLock")
        self.assertEqual(self.session.open(), "opened")
        self.assertFalse(os.path.lexists(profile / "SingletonLock"))
        self.session.close()

    def test_a_second_open_brings_the_window_forward(self):
        self.chrome.pid = 4242
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0))
        session = setup_steps.BrowserSession(self.root, self.root / "profile", find_chrome=lambda: "/chrome",
                                             launch=lambda command, **named: self.launched.append(command) or self.chrome,
                                             run=run, platform="darwin")
        self.assertEqual(session.open(), "opened")
        self.assertEqual(session.open(), "brought_forward")
        self.assertIn("unix id is 4242", run.call_args[0][0][-1])
        run.return_value = subprocess.CompletedProcess([], 1)
        self.assertEqual(session.open(), "already_open")
        self.assertEqual(len(self.launched), 1)
        session.close()
        linux = setup_steps.BrowserSession(self.root, self.root / "profile", find_chrome=lambda: "/chrome",
                                           launch=lambda command, **named: self.launched.append(command) or self.chrome,
                                           platform="linux")
        self.chrome.exited.clear()
        self.assertEqual(linux.open(), "opened")
        self.assertEqual(linux.open(), "brought_forward")
        self.assertEqual(self.launched[-1], ["/chrome", "--user-data-dir=" + str(self.root / "profile")])
        linux.close()

    def test_no_chrome(self):
        session = setup_steps.BrowserSession(self.root, self.root / "profile", find_chrome=lambda: None)
        self.assertEqual(session.open(), "no_chrome")


if __name__ == "__main__":
    unittest.main()


class ResetSetupFileTest(unittest.TestCase):
    """GL-M2 (owner, 2026-10-22): [Reset setup file] keeps the unreadable file as a .broken copy,
    writes a fresh one and re-derives the steps Sidedoor's other files show."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name) / "home"
        self.root = Path(directory.name) / "root"
        self.home.mkdir()
        (self.root / "state").mkdir(parents=True)

    def test_reset_keeps_the_broken_copy_and_rederives_what_the_files_show(self):
        import sqlite3
        import sys
        setup_steps.save_profile(valid_form(), self.home)
        self.save_tested_key("sk-or-synthetic")
        resume = self.home / "resume"
        resume.mkdir()
        (resume / "raw-1.pdf").write_bytes(b"%PDF")
        (resume / "raw-1.txt").write_text("text")
        profile = json.loads((self.home / "profile.json").read_text())
        profile["resume_raw"] = {"path": str(resume / "raw-1.pdf"), "text_path": str(resume / "raw-1.txt"),
                                 "sha256": "a" * 64, "file_name": "cv.pdf"}
        (self.home / "profile.json").write_text(json.dumps(profile))
        before = (self.home / "profile.json").read_text()
        with sqlite3.connect(self.root / "state" / "orchestrator.sqlite3") as database:
            database.execute("CREATE TABLE runs(slot TEXT, started_at TEXT, state TEXT, finished_at TEXT, result TEXT)")
            database.execute("INSERT INTO runs VALUES ('s','t','COMPLETE',NULL,'{}')")
        (self.home / setup_steps.STATE_FILE).write_text("{not json")
        self.assertTrue(setup_steps.state_unreadable(self.home))
        with mock.patch.object(setup_steps, "codex_candidates", return_value=[sys.executable]):
            result = setup_steps.reset_state(self.home, self.root, run=self.signed_in)
        self.assertTrue(result["reset"])
        self.assertEqual(Path(result["kept_as"]).read_text(), "{not json")   # never deleted
        self.assertEqual(result["rederived"], ["codex", "openrouter", "profile", "resume", "locked_email",
                                               "setup_finished_at"])
        state = setup_steps.load_state(self.home)
        self.assertFalse(setup_steps.state_unreadable(self.home))
        progress = setup_steps.progress(self.home)
        self.assertEqual([progress["done"][number] for number in (1, 2, 3, 6, 7, 9)], [True, True, True, True, False, True])
        self.assertEqual(state["locked_email"], setup_steps.saved_email(self.home))   # GL1: the email stays locked
        self.assertTrue(progress["finished"])
        self.assertNotIn("linkedin", state["steps"])
        self.assertEqual((self.home / "profile.json").read_text(), before)   # profile and answers untouched
        setup_steps.record(self.home, "openrouter", "done", "OK")              # saving works again

    def save_tested_key(self, key):
        """A key whose test passed, as save_and_test_openrouter leaves it."""
        transport = mock.Mock(return_value=(200, json.dumps({"choices": [{"message": {"content": "OK"},
                                                             "finish_reason": "stop"}], "usage": {}}).encode()))
        with mock.patch("text_model._post", transport):
            setup_steps.save_and_test_openrouter(key, {"text_model": {"model": "test/model"}}, self.home)
        self.assertTrue(setup_steps.openrouter_tested(self.home))

    @staticmethod
    def signed_in(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, "Logged in using ChatGPT", "")

    def broken(self):
        (self.home / setup_steps.STATE_FILE).write_text("{not json")

    def reset(self, run=None):
        import sys
        with mock.patch.object(setup_steps, "codex_candidates", return_value=[sys.executable]):
            return setup_steps.reset_state(self.home, self.root, run=run or self.signed_in)

    def test_gr2_a_folder_it_cannot_write_gives_a_plain_message(self):
        config = {"text_model": {"model": "test/model"}}
        ok = mock.Mock(return_value=(200, json.dumps({"choices": [{"message": {"content": "OK"},
                                                       "finish_reason": "stop"}], "usage": {}}).encode()))
        real = setup_steps._write_private

        def marker_fails(path, text):
            if Path(path).name == setup_steps.OPENROUTER_TESTED_FILE:
                raise PermissionError("denied")
            return real(path, text)
        with mock.patch("text_model._post", ok), mock.patch.object(setup_steps, "_write_private", side_effect=marker_fails):
            state = setup_steps.save_and_test_openrouter("sk-or-good", config, self.home)
        self.assertEqual((state["steps"]["openrouter"]["status"], state["steps"]["openrouter"]["message"]),
                         ("done", "Key works, but Sidedoor couldn't save that it was tested."))
        self.assertEqual(setup_steps.saved_openrouter_key(self.home), "sk-or-good")   # the key is kept
        with mock.patch.object(Path, "unlink", side_effect=PermissionError("denied")), mock.patch("text_model._post") as post:
            state = setup_steps.save_and_test_openrouter("sk-or-other", config, self.home)
        self.assertEqual(state["steps"]["openrouter"]["status"], "failed")
        self.assertIn("can't write to its folder", state["steps"]["openrouter"]["message"])
        post.assert_not_called()

    def test_gm2_an_untested_or_refused_key_is_not_done_after_a_reset(self):
        (self.home / setup_steps.OPENROUTER_KEY_FILE).write_text("sk-or-untested\n")   # saved, never tested
        self.broken()
        self.assertNotIn("openrouter", self.reset()["rederived"])
        self.save_tested_key("sk-or-good")
        refused = mock.Mock(return_value=(401, b'{"error": {"message": "no"}}'))
        with mock.patch("text_model._post", refused):
            setup_steps.save_and_test_openrouter("sk-or-bad", {"text_model": {"model": "test/model"}}, self.home)
        self.assertFalse(setup_steps.openrouter_tested(self.home))   # a refused new key clears the mark
        self.broken()
        self.assertNotIn("openrouter", self.reset()["rederived"])
        self.save_tested_key("sk-or-good")
        self.broken()
        self.assertIn("openrouter", self.reset()["rederived"])

    def test_gm3_codex_counts_only_when_its_local_sign_in_check_passes(self):
        for output, done in (("Logged in using ChatGPT", True), ("Logged in using an API key", False), ("Not logged in", False)):
            self.broken()
            run = mock.Mock(return_value=subprocess.CompletedProcess([], 0 if done else 1, output, ""))
            self.assertEqual("codex" in self.reset(run)["rederived"], done, output)
            self.assertEqual(run.call_args.args[0][1:], ["login", "status"])
        self.broken()
        self.assertNotIn("codex", self.reset(mock.Mock(side_effect=OSError("cannot run")))["rederived"])

    def test_gl1_an_unfinished_install_is_not_email_locked(self):
        setup_steps.save_profile(valid_form(), self.home)
        self.broken()
        self.assertNotIn("locked_email", self.reset()["rederived"])   # no runs: Setup was not finished
        self.assertNotIn("locked_email", setup_steps.load_state(self.home))

    def test_gl2_a_folder_it_cannot_write_loses_nothing(self):
        self.broken()
        with mock.patch.object(setup_steps.os, "replace", side_effect=PermissionError("denied")):
            result = self.reset()
        self.assertEqual((result["reset"], "can't write to its folder" in result["error"]), (False, True))
        self.assertEqual((self.home / setup_steps.STATE_FILE).read_text(), "{not json")
        with mock.patch.object(setup_steps, "_write_private", side_effect=PermissionError("denied")):
            result = self.reset()
        self.assertFalse(result["reset"])
        self.assertEqual((self.home / setup_steps.STATE_FILE).read_text(), "{not json")   # moved back
        self.assertEqual([path.name for path in self.home.iterdir()], [setup_steps.STATE_FILE])

    def test_a_readable_or_missing_file_is_never_reset(self):
        self.assertEqual(setup_steps.reset_state(self.home, self.root), {"reset": False})
        setup_steps.record(self.home, "openrouter", "done", "OK")
        text = (self.home / setup_steps.STATE_FILE).read_text()
        self.assertEqual(setup_steps.reset_state(self.home, self.root), {"reset": False})
        self.assertEqual((self.home / setup_steps.STATE_FILE).read_text(), text)
        self.assertEqual(sorted(path.name for path in self.home.iterdir()), [setup_steps.STATE_FILE])

    def test_nothing_rederivable_gives_a_fresh_empty_setup(self):
        (self.home / setup_steps.STATE_FILE).write_text("[]")
        with mock.patch.object(setup_steps, "codex_candidates", return_value=[]):
            result = setup_steps.reset_state(self.home, self.root, run=self.signed_in)
        self.assertEqual(result["rederived"], [])
        self.assertEqual(setup_steps.load_state(self.home)["steps"], {})
