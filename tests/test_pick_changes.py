"""Found roles are judged again when the user's picks change (owner, 2026-11-16, M1 a; backlog FU6).

Research's stored verdict used to stand whatever the user changed afterwards: outreach and queued
applications for roles already found carried on under the old picks. `pick_changes.judge_round` has
the AI judge each found, not yet applied role again; a role now out has its queued job closed and
its unsent notes stopped. The model is faked through text_model's transport seam; nothing reaches
the network.
"""
import profile_home
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import applications
import linkedin_contacts
import outreach
import outreach_agent
import outreach_promotion as promotion
import pick_changes
import research_workflow as research
import test_applications_e2e as e2e
import user_profile


NOW = "2026-09-17T16:00:00+00:00"
LATER = "2026-09-17T18:00:00+00:00"
CONFIG = {"text_model": {"model": "test/model"}}
COMPANIES = ["Out Company", "Keep Company", "Third Company", "Fourth Company", "Fifth Company"]
TOO_SENIOR = "The posting is a Staff role; the user's highest target level is Entry-level."


def slug(company):
    return company.lower().replace(" ", "-")


def profile_url(company):
    return "https://www.linkedin.com/in/mock-" + slug(company)


def evidence(field, value, source):
    return {"field": field, "value": value, "source": source, "observed_at": NOW, "verified": True,
            "excerpt": "MOCK fixture only", "kind": "official"}


def found_role(company):
    url = "https://example.test/jobs/" + slug(company)
    person = {"recipient": "Mock " + company.split()[0], "profile": profile_url(company), "employer_matches_target": True,
              "evidence": [evidence("identity", profile_url(company), url), evidence("employment", company, url),
                           evidence("contact_type", "hiring_manager", url)]}
    return {"company": company, "role": "Staff Software Engineer", "url": url, "requisition_id": slug(company),
            "rationale": "MOCK only", "people": [person],
            "role_fit": {"fit": "STRONG", "reason": "A Staff role, one of the target levels."},
            "policy_fit": {"funding_stage": {"in_scope": True, "reason": "Series B is a target stage."},
                           "location": {"in_scope": True, "reason": "Remote in the US."}},
            "evidence": [evidence("team_need", "reliable APIs", url), evidence("specialization", "backend", url)]}


class Model:
    """Answers each call by the company of the role in its DATA; records the companies asked about."""

    def __init__(self, fits=None, status=200, content=None, location_out=(), statuses=None, unreadable=()):
        self.fits, self.status, self.content, self.location_out = fits or {}, status, content, location_out
        self.statuses, self.unreadable = statuses or {}, unreadable
        self.calls, self.prompts = [], []

    def __call__(self, url, data, headers, timeout):
        body = json.loads(data)
        prompt = body["messages"][-1]["content"]
        packet = json.loads(prompt.split("\n\nDATA:\n", 1)[1])
        company = packet["role"]["company"]
        self.calls.append(company)
        self.prompts.append((prompt, packet))
        self.schema = body["response_format"]["json_schema"]
        status = self.statuses.get(company, self.status)
        if status != 200:
            return status, b'{"error": {"message": "Insufficient credits"}}'
        fit = self.fits.get(company, "STRONG")
        answer = {"role_fit": {"fit": fit, "reason": TOO_SENIOR if fit == "NONE" else "Still one of the target levels."},
                  "policy_fit": {"funding_stage": {"in_scope": None, "reason": "The stage is not stated."},
                                 "location": {"in_scope": company not in self.location_out,
                                              "reason": "Onsite only, in a country the user did not pick."
                                              if company in self.location_out else "Remote in the US."}}}
        content = self.content if self.content is not None else json.dumps(answer)
        if company in self.unreadable:
            content = '{"role_fit": {"fit": "MAYBE", "reason": "?"}}'
        return 200, json.dumps({"choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                                "usage": {}}).encode()


class PickChangeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "resume.txt").write_text("Alex Example\nBuilt reliable APIs\n", encoding="utf-8")
        policy = user_profile.load()
        policy["resume_raw"] = dict(policy["resume_raw"], text_path=str(self.root / "resume.txt"))
        self.home = profile_home.use(self, policy)
        (self.home / "openrouter.key").write_text("test-key")
        self.policy = user_profile.load()
        self.database = sqlite3.connect(self.root / "fixture.sqlite")
        self.addCleanup(self.database.close)
        self.database.row_factory = sqlite3.Row
        research.initialize(self.database)
        applications.initialize(self.database)
        outreach.initialize_actions(self.database)
        research.seed_history(self.database, {"companies": []}, history_complete=True)
        research.register_claims(self.database, [{"text": "Built reliable APIs", "tags": ["backend"]}],
                                 designated_source_text="Built reliable APIs", source_sha256="a" * 64,
                                 designated_sha256="a" * 64)
        self.keys = {}

    def find(self, companies, plan="plan", now=NOW):
        """Research finds one role (with one contact and a draft) at each company and queues its job."""
        research.plan_research(self.database, plan, companies, now=now)
        output = research.process_research(self.database, {
            "research_status": "COMPLETE", "summary": "Mock " + plan, "blockers": [], "drafts": [],
            "opportunities": [found_role(company) for company in companies]}, plan_id=plan, now=now)
        self.assertEqual(len(output["opportunities"]), len(companies), output["blockers"])
        if plan == "plan":   # a later run finds the same contact already drafted for
            self.assertEqual(len(output["drafts"]), len(companies), output["action_blockers"])
        with self.database:
            for opportunity in output["opportunities"]:
                self.keys[opportunity["company"]] = applications.ingest(self.database, opportunity, NOW)
        return output

    def settle(self):
        """The first round ever judges every found role once; here the AI keeps them all."""
        model = Model()
        result = pick_changes.judge_round(self.database, CONFIG, self.root, self.policy, NOW, transport=model)
        self.assertTrue(result["settled"], result)
        return model

    def changed(self):
        return dict(self.policy, levels=["Entry-level Software Engineering"])

    def round(self, model, policy=None, now=LATER, **named):
        result = pick_changes.judge_round(self.database, CONFIG, self.root, policy or self.changed(), now,
                                          transport=model, **named)
        self.assertFalse(self.database.in_transaction, "the round left a transaction open")
        return result

    def close(self, now=LATER):
        with self.database:
            return applications.close_research_ineligible(self.database, now)

    def job(self, company):
        return self.database.execute("SELECT state,blocker,next_action,payload FROM application_jobs WHERE job_key=?",
                                     (self.keys[company],)).fetchone()

    def stored(self, company):
        """Research's record of the company's role as every reader now gets it."""
        return pick_changes.found_roles(self.database)[self.keys[company]][0]

    def research_rows(self):
        return [tuple(row) for row in self.database.execute("SELECT * FROM rw_results ORDER BY id")]

    def finder_skips(self, now=LATER):
        """Why the contact finder leaves each company alone (linkedin_contacts.schedule)."""
        with self.database:
            self.database.execute("INSERT OR IGNORE INTO rw_meta VALUES (?,?)",
                                  (linkedin_contacts.SINCE_KEY, "2026-09-01T00:00:00+00:00"))
        return {item["company"]: item["reason"] for item in linkedin_contacts.schedule(self.database, {}, now)["skipped"]}

    def draft_capable(self, company):
        return linkedin_contacts.draft_capable(self.stored(company), linkedin_contacts._claims(self.database),
                                               research.timestamp(LATER))

    def queue_notes(self, companies):
        """Promotion queues the first note of each company's draft (the unsent action of a draft)."""
        proof = {"identity": None, "sender_id": 900001, "checked_at": NOW, "evidence": "mock:authenticated-history",
                 "sender_ready": True, "entitlement_verified": True, "direct_messages_complete": True,
                 "sent_invitations_complete": True, "pending_invitation": False, "cooldown": False,
                 "prior_outreach": False, "has_reply": False, "negative_reply": False, "opt_out": False,
                 "conversation_id": "mock-thread", "company_contact_count": 0,
                 "company_count_evidence": "mock:company-history", "followups_sent": 0, "initial_message_sent": False,
                 "agent_decision": "READY", "provider_snapshot_complete": True, "next_action": "Use the shared action guard"}
        self.outreach_config = {
            "sender_id": 900001, "sender_profile": "https://www.linkedin.com/in/mock-sender",
            "outreach_actions_enabled": True, "outreach_mode": "guarded", "outreach_allowed_purposes": ["INITIAL"],
            "outreach_history_max_age_seconds": 3600, "outreach_agent": {"enabled": True, "max_items": 7},
            "outreach_promotion_contexts": {profile_url(company): {
                "purpose": "INITIAL", "conversation_id": "mock-thread",
                "proof": dict(proof, identity=outreach.profile_key(profile_url(company)))} for company in companies}}
        self.database.commit()
        result = promotion.cycle(self.database, self.outreach_config, NOW)
        self.database.commit()
        self.assertEqual(result["queued"], len(companies), result["items"])

    def action(self, company):
        return self.database.execute("SELECT * FROM outreach_actions WHERE identity=?",
                                     (outreach.profile_key(profile_url(company)),)).fetchone()

    # --- the owner's cases ---------------------------------------------------------------------

    def test_role_judged_out_has_its_job_closed_and_is_no_longer_draft_capable(self):
        self.find(COMPANIES[:2])
        self.settle()
        self.assertTrue(self.draft_capable("Out Company"))
        self.assertEqual(self.finder_skips(), {"Out Company": "RESEARCH_DRAFTED_A_CONTACT",
                                               "Keep Company": "RESEARCH_DRAFTED_A_CONTACT"})
        rows = self.research_rows()
        model = Model({"Out Company": "NONE"})
        result = self.round(model)
        self.assertEqual(sorted(model.calls), ["Keep Company", "Out Company"])
        self.assertEqual((result["out"], result["kept"], result["settled"]),
                         ([self.keys["Out Company"]], [self.keys["Keep Company"]], True))
        # Recorded as research records its own verdict: the same fields, in every stored copy.
        out = self.stored("Out Company")
        self.assertEqual((out["eligibility"], out["assessment"][:2], out["role_fit"]["fit"]),
                         ("INELIGIBLE", ["INELIGIBLE", "OUTSIDE_CONFIRMED_SCOPE"], "NONE"))
        self.assertFalse(out["review"]["scope_fit"]["level"]["in_scope"])
        self.assertFalse(research.research_eligible(out))
        self.assertFalse(self.draft_capable("Out Company"))
        self.assertEqual(self.finder_skips(), {"Out Company": "NO_DRAFT_CAPABLE_OPPORTUNITY",
                                               "Keep Company": "RESEARCH_DRAFTED_A_CONTACT"})
        # Research's own rows are not rewritten (pipeline_routing keeps each row's content hash).
        self.assertEqual(self.research_rows(), rows)
        # The queued job is still open until the round's existing close reads the verdict.
        self.assertEqual(self.job("Out Company")["state"], "QUEUED")
        self.assertEqual(self.close(), [self.keys["Out Company"]])
        closed = self.job("Out Company")
        self.assertEqual((closed["state"], closed["blocker"]), ("INELIGIBLE", "OUTSIDE_CONFIRMED_SCOPE"))
        self.assertEqual(closed["next_action"],
                         "Judged again after your search changed, this role is now outside it: " + TOO_SENIOR)
        # The role the AI kept is as it was.
        self.assertEqual(self.job("Keep Company")["state"], "QUEUED")
        self.assertTrue(self.draft_capable("Keep Company"))
        self.assertTrue(research.research_eligible(self.stored("Keep Company")))

    def test_the_ai_is_given_the_current_picks_the_current_rules_and_the_saved_role(self):
        self.find(COMPANIES[:1])
        model = Model()
        self.round(model)
        prompt, packet = model.prompts[0]
        section = pick_changes.rules()
        self.assertTrue(section.startswith("## Role fit"))
        self.assertNotIn("\n## ", section)
        self.assertIn("Level rule", section)
        self.assertIn(section, prompt, "the rules are research's own text, read at run time")
        self.assertEqual(packet["policy"]["levels"], ["Entry-level Software Engineering"])
        self.assertIn("Built reliable APIs", packet["candidate_resume"]["text"])
        role = packet["role"]
        self.assertEqual((role["company"], role["role"]), ("Out Company", "Staff Software Engineer"))
        self.assertEqual(role["earlier_verdict"]["role_fit"]["fit"], "STRONG")
        self.assertTrue(role["evidence"])
        self.assertNotIn("people", role)
        self.assertTrue(model.schema["strict"])
        self.assertEqual(model.schema["schema"]["properties"]["role_fit"]["properties"]["fit"]["enum"],
                         ["STRONG", "PARTIAL", "NONE"])

    def test_a_policy_fit_judged_out_closes_the_job_with_the_ai_reason(self):
        self.find(COMPANIES[:1])
        self.settle()
        self.round(Model(location_out=("Out Company",)))
        self.assertEqual(self.close(), [self.keys["Out Company"]])
        self.assertIn("Onsite only, in a country the user did not pick.", self.job("Out Company")["next_action"])
        self.assertFalse(self.draft_capable("Out Company"))

    def test_a_submitted_job_is_never_touched(self):
        self.find(COMPANIES[:3])
        self.settle()
        with self.database:
            self.database.execute("UPDATE application_jobs SET state='SUBMITTED' WHERE job_key=?", (self.keys["Out Company"],))
            self.database.execute("UPDATE application_jobs SET state='BLOCKED',blocker='SUBMISSION_OUTCOME_UNKNOWN' WHERE job_key=?",
                                  (self.keys["Keep Company"],))
        before = {company: (tuple(self.job(company)), self.stored(company)) for company in COMPANIES[:2]}
        model = Model({company: "NONE" for company in COMPANIES[:3]})
        result = self.round(model)
        self.assertEqual(model.calls, ["Third Company"], "an applied or possibly applied role is not asked about")
        self.assertEqual(result["out"], [self.keys["Third Company"]])
        self.close()
        for company in COMPANIES[:2]:
            self.assertEqual((tuple(self.job(company)), self.stored(company)), before[company])

    def test_a_failed_ai_call_closes_nothing_and_is_retried(self):
        self.find(COMPANIES[:2])
        self.settle()
        before = [self.stored(company) for company in COMPANIES[:2]]
        down = Model({"Out Company": "NONE"}, status=402)
        result = self.round(down)
        self.assertEqual((result["judged"], result["out"], result["settled"]), (0, [], False))
        self.assertEqual(len(down.calls), 1, "an outage ends the round's asking")
        unreadable = Model(content='{"role_fit": {"fit": "MAYBE", "reason": "?"}}')
        result = self.round(unreadable)
        self.assertEqual((result["judged"], result["failed"], result["settled"]), (0, 2, False))
        self.assertEqual(self.close(), [])
        self.assertEqual([self.stored(company) for company in COMPANIES[:2]], before)
        self.assertEqual([self.job(company)["state"] for company in COMPANIES[:2]], ["QUEUED", "QUEUED"])
        self.assertFalse(pick_changes.progress(self.database)["settled"])
        # The AI is back: both roles are asked again and the one now out is closed.
        model = Model({"Out Company": "NONE"})
        result = self.round(model)
        self.assertEqual(sorted(model.calls), ["Keep Company", "Out Company"])
        self.assertTrue(result["settled"])
        self.assertEqual(self.close(), [self.keys["Out Company"]])

    def test_unchanged_picks_make_no_ai_call_at_all(self):
        self.find(COMPANIES[:2])
        first = self.settle()
        self.assertEqual(len(first.calls), 2, "with no record yet every open role is judged once")
        model = Model({"Out Company": "NONE"})
        self.assertIsNone(self.round(model, policy=self.policy))
        # A setting that is not a pick, and the résumé file, change nothing either.
        other = dict(self.policy, max_attempts=9, resume_raw=dict(self.policy["resume_raw"], sha256="b" * 64))
        self.assertIsNone(self.round(model, policy=other))
        self.assertEqual(model.calls, [])
        self.assertEqual(self.close(), [])
        # After a change is settled, the same picks are not judged a second time.
        self.round(Model())
        again = Model({"Out Company": "NONE"})
        self.assertIsNone(self.round(again))
        self.assertEqual(again.calls, [])

    def test_the_cap_per_round_carries_over(self):
        self.find(COMPANIES)
        self.settle()
        model = Model({"Fifth Company": "NONE"})
        first = self.round(model, limit=2)
        self.assertEqual((first["judged"], first["left"], first["settled"]), (2, 3, False))
        second = self.round(model, limit=2)
        self.assertEqual((second["judged"], second["left"], second["settled"]), (2, 1, False))
        third = self.round(model, limit=2)
        self.assertEqual((third["judged"], third["left"], third["settled"]), (1, 0, True))
        self.assertEqual(sorted(model.calls), sorted(COMPANIES), "each role is asked about once")
        self.assertIsNone(self.round(model, limit=2))
        self.assertEqual(len(model.calls), 5)
        self.assertEqual(self.close(), [self.keys["Fifth Company"]])

    def test_picks_changed_again_mid_way_start_over_under_the_newest_picks(self):
        self.find(COMPANIES[:3])
        self.settle()
        model = Model()
        self.round(model, limit=2)
        newest = dict(self.policy, levels=["Senior Software Engineering"])
        result = self.round(model, policy=newest, limit=5)
        self.assertEqual((result["judged"], result["settled"]), (3, True))
        self.assertEqual(model.prompts[-1][1]["policy"]["levels"], ["Senior Software Engineering"])

    # --- review fixes V1 to V4 -----------------------------------------------------------------

    def test_a_kept_role_keeps_its_job_payload_and_saved_read(self):
        self.find(COMPANIES[:1])
        self.settle()
        job = self.database.execute("SELECT * FROM application_jobs").fetchone()
        before = (job["payload"], applications.identity_fingerprint(job))
        result = self.round(Model({"Out Company": "PARTIAL"}))    # STRONG before: the structured verdict changed
        self.assertEqual(result["kept"], [self.keys["Out Company"]])
        job = self.database.execute("SELECT * FROM application_jobs").fetchone()
        self.assertEqual((job["payload"], applications.identity_fingerprint(job)), before)
        self.assertEqual(self.stored("Out Company")["role_fit"]["fit"], "PARTIAL", "the new verdict is still kept")
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM application_events WHERE event='ROLE_JUDGED_AGAIN'")
                         .fetchone()[0], 0)

    def test_a_role_whose_own_call_keeps_failing_is_left_after_three_tries(self):
        self.find(COMPANIES[:3])
        self.settle()
        before = self.stored("Out Company")
        model = Model({"Keep Company": "NONE"}, statuses={"Out Company": 400}, unreadable=("Third Company",))
        first = self.round(model)
        # The failing roles do not stop the others from being asked.
        self.assertEqual((first["judged"], first["failed"], first["out"], first["settled"]),
                         (1, 2, [self.keys["Keep Company"]], False))
        self.round(model)
        third = self.round(model)
        self.assertEqual(sorted(third["skipped"]), sorted([self.keys["Out Company"], self.keys["Third Company"]]))
        self.assertTrue(third["settled"])
        self.assertEqual(model.calls.count("Out Company"), 3)
        self.assertEqual(model.calls.count("Third Company"), 3)
        self.assertEqual(model.calls.count("Keep Company"), 1)
        self.assertIsNone(self.round(model), "left alone until the picks change again")
        self.assertEqual(self.stored("Out Company"), before)
        self.assertEqual(self.close(), [self.keys["Keep Company"]])
        self.assertEqual(self.job("Out Company")["state"], "QUEUED")
        # New picks: the role is asked again.
        newest = dict(self.policy, levels=["Senior Software Engineering"])
        again = Model()
        self.round(again, policy=newest)
        self.assertIn("Out Company", again.calls)

    def test_an_outage_counts_against_no_role(self):
        self.find(COMPANIES[:1])
        self.settle()
        down = Model(status=402)
        for unused in range(4):
            result = self.round(down)
            self.assertEqual((result["skipped"], result["settled"]), ([], False))
        self.assertEqual(pick_changes.progress(self.database)["failures"], {})
        self.assertTrue(self.round(Model())["settled"])

    def test_a_stop_request_ends_the_asking_and_keeps_the_progress(self):
        self.find(COMPANIES)
        self.settle()
        model = Model()
        result = self.round(model, should_stop=lambda: len(model.calls) >= 2)
        self.assertEqual((result["judged"], result["left"], result["settled"]), (2, 3, False))
        self.assertEqual(len(pick_changes.progress(self.database)["done"]), 2)
        rest = self.round(model)
        self.assertEqual((rest["judged"], rest["settled"]), (3, True))
        self.assertEqual(sorted(model.calls), sorted(COMPANIES))

    def test_a_role_judged_out_stays_out_until_the_picks_change_again(self):
        self.find(COMPANIES[:1])
        self.settle()
        self.queue_notes(COMPANIES[:1])
        self.round(Model({"Out Company": "NONE"}))
        self.close()
        key = self.keys["Out Company"]
        self.assertTrue(pick_changes.ruled_out(self.database, key))
        # A contact finder run that was under way writes its copy of the role afterwards, and the day
        # after research lists the same role again as a fit: it stays out all the same.
        copied = self.database.execute("SELECT processed FROM rw_results").fetchone()[0]
        with self.database:
            self.database.execute("INSERT INTO rw_results VALUES ('finder','linkedin-contacts-plan:x',?,'{}',?)",
                                  ("2026-09-17T19:00:00+00:00", copied))
        self.find(COMPANIES[:1], plan="next-day", now="2026-09-18T18:00:00+00:00")
        self.assertTrue(pick_changes.ruled_out(self.database, key))
        self.assertFalse(research.research_eligible(self.stored("Out Company")))
        self.assertEqual(self.finder_skips("2026-09-18T19:00:00+00:00"), {"Out Company": "NO_DRAFT_CAPABLE_OPPORTUNITY"})
        self.database.commit()
        promotion.cycle(self.database, self.outreach_config, "2026-09-18T19:00:00+00:00")
        self.database.commit()
        self.assertEqual(self.action("Out Company")["state"], "CANCELLED", "its cancelled note is not queued again")
        asked = Model()
        self.assertIsNone(self.round(asked, now="2026-09-18T19:00:00+00:00"), "same picks: not asked again")
        self.assertEqual(asked.calls, [])
        # The picks change again: the role is judged like any other, and kept this time. Its outreach
        # can come back; the job that was closed stays closed.
        newest = dict(self.policy, levels=["Staff Software Engineering"])
        result = self.round(asked, policy=newest, now="2026-09-18T20:00:00+00:00")
        self.assertEqual((asked.calls, result["kept"]), (["Out Company"], [key]))
        self.assertFalse(pick_changes.ruled_out(self.database, key))
        self.assertTrue(research.research_eligible(self.stored("Out Company")))
        self.assertEqual(self.job("Out Company")["state"], "INELIGIBLE")

    def test_a_research_run_begun_under_the_old_picks_does_not_escape_judging(self):
        self.find(COMPANIES[:1])
        self.settle()
        self.assertTrue(self.round(Model())["settled"])           # the changed picks, settled at LATER
        # A run of that same day's plan stores its roles after the judging: they are judged.
        self.find(COMPANIES[1:3], plan="late", now="2026-09-17T19:00:00+00:00")
        model = Model({"Keep Company": "NONE"})
        result = self.round(model, now="2026-09-17T19:30:00+00:00")
        self.assertEqual(sorted(model.calls), ["Keep Company", "Third Company"])
        self.assertEqual(result["out"], [self.keys["Keep Company"]])
        self.assertTrue(pick_changes.progress(self.database)["settled"])
        self.assertIsNone(self.round(model, now="2026-09-17T19:40:00+00:00"))
        # A plan of a later day was made under the current picks: nothing is asked.
        self.find(COMPANIES[3:4], plan="next-day", now="2026-09-18T18:00:00+00:00")
        self.assertIsNone(self.round(model, now="2026-09-18T19:00:00+00:00"))
        self.assertEqual(len(model.calls), 2)

    # --- outreach: notes not yet sent ----------------------------------------------------------

    def test_unsent_notes_of_a_role_judged_out_do_not_go_out(self):
        self.find(COMPANIES[:4])
        self.settle()
        self.queue_notes(COMPANIES[:4])
        with self.database:
            # One waits on Approvals, one was claimed for sending; the first and the kept one wait to send.
            self.database.execute("UPDATE outreach_actions SET state='AWAITING_APPROVAL' WHERE action_id=?",
                                  (self.action("Third Company")["action_id"],))
            self.database.execute("UPDATE outreach_actions SET state='SENT',claimed_at=? WHERE action_id=?",
                                  (NOW, self.action("Fourth Company")["action_id"]))
        sent = tuple(self.action("Fourth Company"))
        self.round(Model({"Out Company": "NONE", "Third Company": "NONE", "Fourth Company": "NONE"}))
        for company in ("Out Company", "Third Company"):
            action = self.action(company)
            self.assertEqual(action["state"], "CANCELLED", company)
            self.assertEqual(json.loads(action["result"])["promotion_hold"], ["RESEARCH_INELIGIBLE_ROLE"])
        self.assertEqual(self.action("Keep Company")["state"], "PENDING")
        self.assertEqual(tuple(self.action("Fourth Company")), sent, "a note already sent is history")
        # Promotion does not queue them again, and the kept role's note is as it was.
        self.database.commit()
        result = promotion.cycle(self.database, self.outreach_config, LATER)
        self.database.commit()
        gates = {item["identity"]: item["gates"] for item in result["items"]}
        self.assertEqual(gates[research.canonical(self.database, "contact", profile_url("Out Company"))],
                         ["RESEARCH_INELIGIBLE_ROLE"])
        self.assertEqual(self.action("Out Company")["state"], "CANCELLED")
        self.assertEqual(self.action("Third Company")["state"], "CANCELLED")
        self.assertEqual(self.action("Keep Company")["state"], "PENDING")

    def test_no_note_is_written_or_checked_for_a_role_judged_out(self):
        self.find(COMPANIES[:2])
        self.settle()
        self.round(Model({"Out Company": "NONE"}))
        config = {"outreach_agent": {"enabled": True, "max_items": 7, "compose": True}}
        composed = []

        def compose_step(root, database, config, row, execute, now):
            composed.append(json.loads(row["payload"])["company"])
            return {"attempted": True, "state": "RETRY_WAIT"}

        with patch.object(outreach_agent, "_compose_step", compose_step), \
                patch.object(outreach_agent.Adapter, "inspect", side_effect=AssertionError("no check")):
            result = outreach_agent.prepare_drafts(self.root, self.database, config, Mock(spec=["call", "_request"]),
                                                   now=LATER)
        self.assertEqual(composed, ["Keep Company"])
        self.assertEqual(sorted(item["state"] for item in result["items"]), ["RETRY_WAIT", "ROLE_OUT_OF_SEARCH"])


class RoundWiringTests(unittest.TestCase):
    """`applications.cycle` judges once per ordinary round, before the close, and is never broken by it."""

    def setUp(self):
        self.case = e2e.ApplicationEndToEndTests(next(name for name in dir(e2e.ApplicationEndToEndTests)
                                                       if name.startswith("test_")))
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def test_an_ordinary_round_judges_after_its_application_work_and_closes_what_is_out(self):
        key = self.case.discover(dict(e2e.role(), eligibility="ELIGIBLE"))
        seen, stop = [], (lambda: False)

        def judge(database, config, root, policy, now, **named):
            seen.append((self.case.job(key)["state"], named["should_stop"], policy["levels"]))
            return {"out": ["some:role"]}

        with patch.object(pick_changes, "judge_round", side_effect=judge), \
                patch.object(applications, "close_research_ineligible", return_value=[]) as close:
            applications.cycle(self.case.database, {}, self.case.root, self.case.adapter, e2e.NOW, should_stop=stop)
        self.assertEqual(seen, [("SUBMITTED", stop, user_profile.load()["levels"])], "after the round's applications")
        self.assertEqual(close.call_count, 2, "at the top of the round, and again for the roles just judged out")

    def test_a_stopped_round_does_not_judge(self):
        self.case.discover(dict(e2e.role(), eligibility="ELIGIBLE"))
        with patch.object(pick_changes, "judge_round") as judge:
            applications.cycle(self.case.database, {}, self.case.root, self.case.adapter, e2e.NOW, should_stop=lambda: True)
        judge.assert_not_called()

    def test_a_round_for_chosen_jobs_does_not_judge(self):
        with patch.object(pick_changes, "judge_round") as judge:
            applications.cycle(self.case.database, {}, self.case.root, self.case.adapter, e2e.NOW, only={"none:none"})
        judge.assert_not_called()

    def test_a_failure_never_breaks_the_round(self):
        key = self.case.discover(dict(e2e.role(), eligibility="ELIGIBLE"))
        with patch.object(pick_changes, "judge_round", side_effect=RuntimeError("boom")):
            self.case.cycle()
        self.assertEqual(self.case.job(key)["state"], "SUBMITTED")


if __name__ == "__main__":
    unittest.main()
