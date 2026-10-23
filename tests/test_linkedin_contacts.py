"""Logged-in LinkedIn contact finder: the three rules the owner kept (2026-10-01).

1. No click on a control that is itself Connect/Message/Follow/Invite/Send/Withdraw.
2. At most 3 NEW people per company per run.
3. One check against our local records, before a draft is created.
Minimal by owner preference; the realistic check is the read-only replay on real traces.
"""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import browser_guard  # noqa: E402
import linkedin_contacts as lc  # noqa: E402
import outreach_history_browser as browser  # noqa: E402
import outreach_promotion as promotion  # noqa: E402
import research_workflow as research  # noqa: E402
import outreach  # noqa: E402
import strategy_execution  # noqa: E402
import workflow_tasks  # noqa: E402

NOW = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
DESIGNATED = "d" * 64
HELD = "https://www.linkedin.com/in/held-person"
CONFIG = {"linkedin_contacts": {"enabled": True}}


def fixture():
    database = sqlite3.connect(":memory:")
    database.row_factory = sqlite3.Row
    research.initialize(database)
    strategy_execution.initialize(database)
    workflow_tasks.initialize(database)
    claim = {"text": "Designed a sharded job scheduler with lease-based ownership.", "tags": ["distributed systems"],
             "source": "designated_resume", "source_sha256": DESIGNATED, "verified": True, "page": 1,
             "invitation_mapping": {"text": "I designed a sharded scheduler", "kind": "reviewed_paraphrase",
                                    "source_claim_sha256": "c" * 64, "version": 1}}
    observed = (NOW - dt.timedelta(hours=2)).isoformat()
    evidence = {"field": "team_need", "value": "Scale durable distributed workflows", "kind": "official",
                "source": "https://jobs.acme.example/1", "observed_at": observed, "effective_at": None,
                "excerpt": "Scale durable distributed workflows", "verified": True}
    opportunity = {"company": "Acme Robotics", "role": "Senior Backend Engineer", "url": "https://jobs.acme.example/1",
                   "job_key": "acme:1", "evidence": [],
                   "review": {"facts": {"team_need": evidence["value"], "specialization": "distributed systems"},
                              "fact_evidence": {"team_need": evidence}}}
    with database:
        database.execute("INSERT INTO rw_meta VALUES ('history_seeded','true')")
        # The finder has been watching research since yesterday (no backfill before that).
        database.execute("INSERT INTO rw_meta VALUES (?,?)", (lc.SINCE_KEY, (NOW - dt.timedelta(days=1)).isoformat()))
        database.execute("INSERT INTO rw_meta VALUES ('designated_resume',?)", (DESIGNATED,))
        database.execute("INSERT INTO rw_claims VALUES (?,?)", (research.digest(claim), research.encoded(claim)))
        database.execute("INSERT INTO rw_results VALUES ('r1','plan-1',?,?,?)",
                         (observed, research.encoded({"opportunities": []}),
                          research.encoded({"opportunities": [opportunity], "drafts": []})))
    return database


def held_fixture():
    """Research run plan-1 found one held, undrafted person at Acme; an older draft elsewhere."""
    database = fixture()
    with database:
        database.execute("INSERT INTO rw_contacts VALUES ('acme robotics',?,'held person',0,'plan-1')", (HELD,))
        database.execute("INSERT INTO rw_drafts VALUES ('https://www.linkedin.com/in/other',1,'h',?)",
                         (json.dumps({"company": "Acme Robotics"}),))
    return database


def candidate(url, name, verdict=True, **extra):
    return dict({"origin": "PEOPLE_TAB", "profile_url": url + "/", "full_name": name, "current_title": "CTO",
                 "current_company": "Acme Robotics", "current_company_matches_target": verdict, "location": None,
                 "connection_degree": "2nd", "invitation_state": "NONE_VISIBLE",
                 "contact_type": "functional_executive", "relevance": "Leads engineering.",
                 "profile_quote": None, "name_quote": None, "title_quote": None, "company_quote": None,
                 "degree_quote": None}, **extra)


def stub(candidates, calls=(), outcome="FOUND"):
    """A finder session: `calls` are (tool, arguments, text) tool calls; output lists `candidates`."""
    def run(config_, prompt, directory, timeout):
        request = json.loads((directory / "request.json").read_text())
        (directory / "trace.jsonl").write_text("\n".join(json.dumps({"type": "item.completed", "item": {
            "id": f"item_{index}", "type": "mcp_tool_call", "server": "browser", "tool": tool,
            "arguments": arguments, "status": "completed", "error": None,
            "result": {"content": [{"type": "text", "text": text}]}}}) for index, (tool, arguments, text) in enumerate(calls)))
        (directory / "output.json").write_text(json.dumps({
            "version": lc.VERSION, "source_sha256": browser.digest(request), "outcome": outcome, "reason": "Read.",
            "company_page_url": None, "candidates": candidates}))
    return run


def multi_company_fixture():
    """One research result (plan-1) covering Acme, Globex and Initech, drafting nobody."""
    database = fixture()
    processed = json.loads(database.execute("SELECT processed FROM rw_results WHERE id='r1'").fetchone()[0])
    base = processed["opportunities"][0]
    processed["opportunities"] = [dict(base, company=name, job_key=name.lower() + ":1")
                                  for name in ("Acme Robotics", "Globex", "Initech")]
    with database:
        database.execute("UPDATE rw_results SET processed=? WHERE id='r1'", (research.encoded(processed),))
    return database


PROFILE_PAGE = ("Browser tab: 1, URL: \"https://www.linkedin.com/in/ada/\".\n0 AXWebArea Ada Lovelace | LinkedIn\n"
                "\t12 link Description: Ada & 4 other connections follow this page, Value: linkedin.com/company/acme\n"
                "\t13 button Compose message, ID: ember33\n\t14 link Description: Ada Lovelace, Value: linkedin.com/in/ada\n"
                "\t15 button Invite Ada Lovelace to connect\n\t16 button Message\n"
                "\t17 link Description: Send a message to Ada Lovelace, Value: linkedin.com/messaging/compose/\n"
                "\t18 button Follow Acme\n\t19 button Pending, click to withdraw invitation sent to Ada Lovelace\n"
                "\t20 button (collapsed) More, Secondary Actions: Expand\n")


class ContactFinder(unittest.TestCase):
    def test_rule_1_refused_before_the_click_keeps_the_read(self):
        # E7.T1: the guard refuses a contact-control click before it reaches the page
        # (tests/test_browser_driver.py proves the refusal); a refused click never happened, so the
        # read is kept and the refusals are counted with the run.
        refusal = {"decision": "REFUSED", "code": "LINKEDIN_CONTACTS_CONTACT_CONTROL_CLICKED", "tool": "browser_click",
                   "arguments": {"target": "e15"}, "detail": 'button "Invite Ada Lovelace to connect" [e15] is a contact control.'}
        def refused(config_, prompt, directory, timeout):
            (directory / "guard.jsonl").write_text(json.dumps(refusal) + "\n" + json.dumps({"decision": "FORWARDED"}) + "\n")
            return stub([candidate("https://www.linkedin.com/in/ada", "Ada Lovelace")])(config_, prompt, directory, timeout)
        with tempfile.TemporaryDirectory() as root:
            database = fixture()
            run = lc.cycle(root, database, CONFIG, now=NOW, executor=refused)["runs"][0]
            self.assertEqual((run["state"], run["contact_control_refusals"]), ("COMPLETE", 1))
            self.assertEqual(database.execute("SELECT COUNT(*) FROM rw_drafts").fetchone()[0], 1)

    def test_rule_1_name_rule_is_the_live_regex(self):
        # The guard's name rule (browser_guard.CONTACT_NAME) is live rule 1 (CONTACT_CONTROL) after its
        # role prefix, line for line on the live AX lines this file used to replay.
        for line in PROFILE_PAGE.splitlines()[2:]:
            label = line.strip().split(" ", 1)[1]
            role, _, name = label.partition(" ")
            if role == "button" and name.startswith("("):
                name = name.split(") ", 1)[1]
            node = {"role": role, "name": name.split(", ID:")[0].split(", Value:")[0], "texts": []}
            self.assertEqual(bool(lc.CONTACT_CONTROL.match(label)), browser_guard.contact_label(node) is not None, label)


    def test_rule_2_at_most_three_new_people_and_everything_else_is_data(self):
        database = fixture()
        people = [candidate("https://www.linkedin.com/in/ada", "Ada Lovelace", connection_degree="1st"),
                  candidate("https://www.linkedin.com/in/alan", "Alan Turing", invitation_state="PENDING"),
                  candidate("https://www.linkedin.com/in/grace", "Grace Hopper", verdict=False, current_company="Globex"),
                  candidate("https://www.linkedin.com/in/edsger", "Edsger Dijkstra", verdict=None),
                  candidate("https://www.linkedin.com/in/barbara", "Barbara Liskov")]
        with tempfile.TemporaryDirectory() as root:
            run = lc.cycle(root, database, CONFIG, now=NOW, executor=stub(people))["runs"][0]
        self.assertEqual(run["state"], "COMPLETE", run)
        self.assertEqual({item["full_name"]: item["reason"] for item in run["rejected"]},
                         {"Grace Hopper": "CURRENT_COMPANY_MISMATCH", "Barbara Liskov": "RUN_CONTACT_LIMIT"})
        self.assertEqual(sorted(draft["recipient"] for draft in run["drafts"]),
                         ["Ada Lovelace", "Alan Turing", "Edsger Dijkstra"])
        self.assertTrue(promotion.linkedin_verified(json.loads(database.execute("SELECT payload FROM rw_drafts").fetchone()[0])))

    def test_store_caps_new_people_at_the_item_slots(self):
        database = fixture()
        opportunity = json.loads(database.execute("SELECT processed FROM rw_results").fetchone()[0])["opportunities"][0]
        accepted = [dict(candidate("https://www.linkedin.com/in/" + slug, name), identity="https://www.linkedin.com/in/" + slug)
                    for slug, name in (("first-person", "First Person"), ("second-person", "Second Person"))]
        for slots, stored_people in ((0, 0), (1, 1)):
            item = {"company": "Acme Robotics", "company_key": "acme robotics", "opportunity": opportunity,
                    "slots": slots, "rw_result_id": "r1", "verify_held": []}
            with research.transaction(database):
                stored = lc.store(database, item, {"verify_held": [], "proposed_profiles": []}, list(accepted), [],
                                  "run-" + str(slots), NOW, "test")
            self.assertEqual(database.execute("SELECT COUNT(*) FROM rw_contacts").fetchone()[0], stored_people)
        self.assertEqual([entry["reason"] for entry in stored["rejected"]], ["RUN_CONTACT_LIMIT"])

    def test_rule_3_one_local_check_before_the_draft(self):
        """A person with a local record of contact is read (no finder gate) but never drafted."""
        database = held_fixture()
        database.execute("CREATE TABLE suppressions (identity TEXT PRIMARY KEY, reason TEXT NOT NULL)")
        database.execute("INSERT INTO suppressions VALUES ('linkedin.com/in/held-person','contacted')")
        database.commit()
        due = lc.schedule(database, CONFIG, NOW)["due"]
        # The held person research reserved is one of the research run's 3 new people (rule 2).
        self.assertEqual([(item["slots"], item["verify_held"]) for item in due], [(2, [HELD])])
        with tempfile.TemporaryDirectory() as root:
            run = lc.cycle(root, database, CONFIG, now=NOW, executor=stub(
                [candidate(HELD, "Held Person", origin="PROPOSED")]))["runs"][0]
        self.assertEqual(run["drafts"], [])
        self.assertEqual([item["reason"] for item in run["rejected"]], ["HISTORICAL_OR_DUPLICATE_CONTACT"])
        # Unreadable records fail closed for that person only.
        database = held_fixture()
        with tempfile.TemporaryDirectory() as root, patch.object(
                research, "prior_contact_record", return_value=research.LOCAL_HISTORY_UNREADABLE + ": OperationalError"):
            run = lc.cycle(root, database, CONFIG, now=NOW, executor=stub(
                [candidate(HELD, "Held Person", origin="PROPOSED")]))["runs"][0]
        self.assertEqual([item["reason"] for item in run["rejected"]], [research.LOCAL_HISTORY_UNREADABLE])

    def test_held_contact_is_reverified_and_drafted_on_its_reservation(self):
        """Owner, 2026-09-30: go back, verify a held contact and possibly write their draft."""
        database = held_fixture()
        with tempfile.TemporaryDirectory() as root:
            run = lc.cycle(root, database, CONFIG, now=NOW, executor=stub(
                [candidate(HELD, "Held Person", origin="PROPOSED"),
                 candidate("https://www.linkedin.com/in/new-person", "New Person")]))["runs"][0]
        self.assertEqual(run["state"], "COMPLETE", run)
        # An existing draft at Acme no longer stops discovery (owner, 2026-10-01): the held person
        # is drafted on the reservation they hold and the new person takes one of the 3 slots.
        self.assertEqual(sorted(draft["profile"] for draft in run["drafts"]),
                         [HELD, "https://www.linkedin.com/in/new-person"])
        self.assertEqual(run["rejected"], [])
        rows = dict(tuple(row) for row in database.execute("SELECT identity,plan_id FROM rw_contacts"))
        self.assertEqual(rows[HELD], "plan-1")
        self.assertEqual(len(rows), 2)

    def test_due_once_per_research_run_that_drafted_nobody(self):
        """Owner, 2026-10-01: a company research covered but drafted nobody at is due, once."""
        database = fixture()
        self.assertEqual([item["company_key"] for item in lc.schedule(database, CONFIG, NOW)["due"]], ["acme robotics"])
        # A failed finder run is retried; a completed one settles that research run.
        with tempfile.TemporaryDirectory() as root:
            failed = lc.cycle(root, database, CONFIG, now=NOW, executor=lambda *args: 1 / 0)["runs"][0]
            self.assertEqual(failed["state"], "FAILED")
            self.assertEqual(len(lc.schedule(database, CONFIG, NOW)["due"]), 1)
            later = NOW + dt.timedelta(minutes=5)
            self.assertEqual(lc.cycle(root, database, CONFIG, now=later, executor=stub([]))["runs"][0]["state"], "COMPLETE")
        self.assertEqual(lc.schedule(database, CONFIG, NOW)["due"], [])
        # A newer research run for the company that again drafts nobody makes it due again.
        processed = database.execute("SELECT processed FROM rw_results WHERE id='r1'").fetchone()[0]
        with database:
            database.execute("INSERT INTO rw_results VALUES ('r2','plan-2',?,?,?)",
                             (NOW.isoformat(), research.encoded({"opportunities": []}), processed))
        self.assertEqual([item["rw_result_id"] for item in lc.schedule(database, CONFIG, NOW)["due"]], ["r2"])
        # ...but not when that research run drafted someone there.
        with database:
            database.execute("INSERT INTO rw_drafts VALUES ('https://www.linkedin.com/in/x',1,'h',?)",
                             (json.dumps({"company": "Acme Robotics", "plan_id": "plan-2"}),))
        planned = lc.schedule(database, CONFIG, NOW)
        self.assertEqual(planned["due"], [])
        self.assertEqual([item["reason"] for item in planned["skipped"]], ["RESEARCH_DRAFTED_A_CONTACT"])

    def test_settled_per_company_only_by_a_finished_read_and_failures_go_last(self):
        database = multi_company_fixture()
        due = lc.schedule(database, CONFIG, NOW)["due"]
        self.assertEqual(sorted(item["company_key"] for item in due), ["acme robotics", "globex", "initech"])
        first = due[0]["company_key"]
        with tempfile.TemporaryDirectory() as root:
            # UNKNOWN ("could not finish") is retried like a failure, and counts as a failed try.
            run = lc.cycle(root, database, CONFIG, now=NOW, executor=stub([], outcome="UNKNOWN"))["runs"][0]
            self.assertEqual((run["state"], run["outcome"]), ("COMPLETE", "UNKNOWN"))
            due = lc.schedule(database, CONFIG, NOW)["due"]
            self.assertEqual(len(due), 3)
            self.assertEqual(due[-1]["company_key"], first)       # fewest failed tries first
            self.assertEqual(due[-1]["failed_tries"], 1)
            # A finished read settles that company only, not the others in the same research result.
            done = due[0]["company_key"]
            lc.cycle(root, database, CONFIG, now=NOW + dt.timedelta(minutes=5), executor=stub([]))
        remaining = sorted(item["company_key"] for item in lc.schedule(database, CONFIG, NOW)["due"])
        self.assertEqual(len(remaining), 2)
        self.assertNotIn(done, remaining)

    def test_a_held_person_re_found_by_a_later_run_is_verified_in_it(self):
        database = held_fixture()
        with database:
            database.execute("UPDATE rw_contacts SET plan_id='older-plan'")
            database.execute("UPDATE rw_results SET raw=? WHERE id='r1'", (research.encoded({"opportunities": [
                {"company": "Acme Robotics", "people": [{"profile": HELD + "/"}]}]}),))
        due = lc.schedule(database, CONFIG, NOW)["due"]
        self.assertEqual([(item["verify_held"], item["proposed_profiles"]) for item in due], [([HELD], [HELD])])
        # Not re-found by this run: an earlier day's held person is not backfilled.
        with database:
            database.execute("UPDATE rw_results SET raw=? WHERE id='r1'", (research.encoded({"opportunities": []}),))
        self.assertEqual([item["verify_held"] for item in lc.schedule(database, CONFIG, NOW)["due"]], [[]])

    def test_what_is_a_research_run_and_what_supersedes_it(self):
        # The finder's own results are never research runs.
        database = fixture()
        processed = database.execute("SELECT processed FROM rw_results WHERE id='r1'").fetchone()[0]
        with database:
            database.execute("INSERT INTO rw_results VALUES ('finder','linkedin-contacts-plan:x',?,?,?)",
                             (NOW.isoformat(), research.encoded({"opportunities": []}), processed))
        self.assertEqual([item["rw_result_id"] for item in lc.schedule(database, CONFIG, NOW)["due"]], ["r1"])
        # No draft-capable opportunity (no verified resume claim): not due.
        with database:
            database.execute("DELETE FROM rw_claims")
        self.assertEqual([item["reason"] for item in lc.schedule(database, CONFIG, NOW)["skipped"]],
                         ["NO_DRAFT_CAPABLE_OPPORTUNITY"])
        # A newer research run that covered the company without any opportunity supersedes it.
        database = fixture()
        today = workflow_tasks.service_date(NOW, "America/Los_Angeles")
        with database:
            database.execute("INSERT INTO rw_plans VALUES ('plan-1','2000-01-01','h','{}')")
            database.execute("INSERT INTO rw_companies VALUES ('2000-01-01','acme robotics','plan-1')")
        self.assertEqual(len(lc.schedule(database, CONFIG, NOW)["due"]), 1)
        with database:
            database.execute("INSERT INTO rw_plans VALUES ('plan-2',?,'h','{}')", (today,))
            database.execute("INSERT INTO rw_companies VALUES (?,'acme robotics','plan-2')", (today,))
        planned = lc.schedule(database, CONFIG, NOW)
        self.assertEqual(planned["due"], [])
        self.assertEqual([item["reason"] for item in planned["skipped"]], ["SUPERSEDED_BY_NEWER_RESEARCH"])

    def test_held_people_are_those_of_that_research_run(self):
        database = held_fixture()          # HELD was reserved by plan-1, the research run
        with database:
            database.execute("INSERT INTO rw_contacts VALUES ('acme robotics','https://www.linkedin.com/in/earlier',"
                             "'earlier person',0,'older-plan')")
        due = lc.schedule(database, CONFIG, NOW)["due"]
        self.assertEqual([item["verify_held"] for item in due], [[HELD]])

    def test_rule_2_holds_across_retries_of_one_research_run(self):
        database = fixture()
        people = [candidate("https://www.linkedin.com/in/p" + str(index), "Person Number" + " x" * index)
                  for index in range(1, 6)]
        with tempfile.TemporaryDirectory() as root:
            first = lc.cycle(root, database, CONFIG, now=NOW, executor=stub(people[:2], outcome="UNKNOWN"))["runs"][0]
            self.assertEqual(len(first["drafts"]), 2)
            due = lc.schedule(database, CONFIG, NOW)["due"]
            self.assertEqual([item["slots"] for item in due], [1])      # UNKNOWN retries fill what is left
            second = lc.cycle(root, database, CONFIG, now=NOW + dt.timedelta(minutes=5),
                              executor=stub(people[2:], outcome="UNKNOWN"))["runs"][0]
        self.assertEqual(len(second["drafts"]), 1)
        self.assertEqual(database.execute("SELECT COUNT(*) FROM rw_drafts").fetchone()[0], 3)
        planned = lc.schedule(database, CONFIG, NOW)
        self.assertEqual(planned["due"], [])
        self.assertEqual([item["reason"] for item in planned["skipped"]], ["RUN_CONTACT_LIMIT_USED"])

    def mark_run(self, database, state, started, run_id="x"):
        lc.initialize(database)
        with database:
            database.execute("INSERT INTO linkedin_contact_runs VALUES (?,?,?,?,?,?,?,?)",
                             (run_id, "acme robotics", started.isoformat(), state, "h", None, None,
                              json.dumps({"research_result_id": "r1"})))

    def test_waits_and_live_runs_are_not_tries_but_stale_runs_are(self):
        database = fixture()
        self.mark_run(database, "PROVIDER_WAIT", NOW, "wait")
        self.mark_run(database, "RUNNING", NOW - dt.timedelta(minutes=10), "live")
        self.assertEqual(lc.schedule(database, CONFIG, NOW)["due"][0]["failed_tries"], 0)
        with database:
            lc.expire_stale_runs(database, NOW)                          # 10 minutes: still running
        self.assertEqual(lc.schedule(database, CONFIG, NOW)["due"][0]["failed_tries"], 0)
        later = NOW + dt.timedelta(minutes=11)
        with database:
            lc.expire_stale_runs(database, later)                        # 21 minutes: interrupted
        self.assertEqual(database.execute("SELECT state FROM linkedin_contact_runs WHERE run_id='live'").fetchone()[0], "FAILED")
        self.assertEqual(lc.schedule(database, CONFIG, later)["due"][0]["failed_tries"], 1)

    def test_at_most_three_finder_attempts_per_research_run(self):
        """Owner, 2026-10-01: "And also max retry should be 3"."""
        database = fixture()
        for index in range(2):
            self.mark_run(database, "FAILED", NOW, "f" + str(index))
        self.mark_run(database, "PROVIDER_WAIT", NOW, "wait")            # a wait is not an attempt
        self.assertEqual(lc.schedule(database, CONFIG, NOW)["due"][0]["failed_tries"], 2)
        self.mark_run(database, "FAILED", NOW, "f2")
        planned = lc.schedule(database, CONFIG, NOW)
        self.assertEqual(planned["due"], [])
        self.assertEqual([item["reason"] for item in planned["skipped"]], ["FINDER_RETRIES_EXHAUSTED"])

    def test_research_context_reaches_the_finder_request(self):
        """Owner, 2026-10-01: pass research's context and failure reason to the contact finder."""
        database = fixture()
        person = {"profile": "https://www.linkedin.com/in/meera", "recipient": "Meera Iyer",
                  "evidence": [{"field": "employment", "value": "Acme Robotics", "verified": False}]}
        processed = json.loads(database.execute("SELECT processed FROM rw_results WHERE id='r1'").fetchone()[0])
        processed["opportunities"][0]["people"] = [person]
        processed["action_blockers"] = ["Acme Robotics, Meera Iyer: direct LinkedIn profile fetch failed with HTTP 999.",
                                        "Acme Robotics / Meera Iyer: PERSON_EVIDENCE_MISSING", "Globex: unrelated"]
        with database:
            database.execute("UPDATE rw_results SET processed=? WHERE id='r1'", (research.encoded(processed),))
        item = lc.schedule(database, CONFIG, NOW)["due"][0]
        context = lc.build_request(database, item, "run")["research_context"]
        self.assertTrue(context["untrusted"])
        self.assertEqual(context["blockers"], processed["action_blockers"][:2])
        self.assertEqual([(found["name"], found["evidence"][0]["verified"]) for found in context["people_found"]],
                         [("Meera Iyer", False)])
        self.assertEqual(context["roles"][0]["team_need"], "Scale durable distributed workflows")

    def test_a_marketing_role_request_carries_no_engineering_title_list(self):
        """EW1 (owner, 2026-10-22): the finder searches the role's own field; no fixed list."""
        database = fixture()
        processed = json.loads(database.execute("SELECT processed FROM rw_results WHERE id='r1'").fetchone()[0])
        processed["opportunities"][0].update(role="Senior Product Marketing Manager")
        processed["opportunities"][0]["review"]["facts"]["specialization"] = "product marketing"
        with database:
            database.execute("UPDATE rw_results SET processed=? WHERE id='r1'", (research.encoded(processed),))
        item = lc.schedule(database, CONFIG, NOW)["due"][0]
        request = lc.build_request(database, item, "run")
        focus = request["title_focus"]
        self.assertEqual(focus["role_terms"], ["Senior Product Marketing Manager", "product marketing"])
        self.assertNotIn("engineer", json.dumps(focus).casefold())
        self.assertNotIn("cto", json.dumps(focus).casefold().split('"'))
        enum = lc.schema()["properties"]["candidates"]["items"]["properties"]["contact_type"]["enum"]
        self.assertEqual(enum[:5], list(lc.CONTACT_TYPES))   # legacy names follow, accepted and mapped
        self.assertEqual(lc.CONTACT_TYPES, ("functional_executive", "hiring_manager", "senior_peer", "peer", "recruiter"))

    def test_an_old_stored_contact_type_reads_in_the_new_vocabulary(self):
        """EW2: a candidate recorded before the rename is stored under the new name."""
        old = dict(candidate("https://www.linkedin.com/in/ada", "Ada Lovelace"), contact_type="staff_engineer",
                   identity="linkedin.com/in/ada")
        record = lc.person_record(old, "Acme Robotics", NOW.isoformat(), "run")
        self.assertEqual({entry["field"]: entry["value"] for entry in record["evidence"]}["contact_type"], "senior_peer")
        self.assertEqual(record["linkedin_verification"]["contact_type"], "senior_peer")

    def test_a_known_research_person_is_marked_and_takes_no_slot(self):
        """Humongous, 2026-10-01: research re-found a person already drafted earlier. The finder is
        told they are on file, discovery still runs, and returning them spends no new-person slot."""
        database = fixture()
        known = "https://www.linkedin.com/in/owen-pratt"
        processed = json.loads(database.execute("SELECT processed FROM rw_results WHERE id='r1'").fetchone()[0])
        processed["opportunities"][0]["people"] = [{"profile": known, "recipient": "Owen Pratt", "evidence": []}]
        processed["action_blockers"] = ["Acme Robotics / Owen Pratt: HISTORICAL_OR_DUPLICATE_CONTACT"]
        with database:
            database.execute("UPDATE rw_results SET processed=? WHERE id='r1'", (research.encoded(processed),))
            database.execute("INSERT INTO rw_contacts VALUES ('acme robotics',?,'owen pratt',0,'older-plan')", (known,))
            database.execute("INSERT INTO rw_drafts VALUES (?,1,'h',?)", (known, json.dumps({"company": "Acme Robotics"})))
        item = lc.schedule(database, CONFIG, NOW)["due"][0]
        request = lc.build_request(database, item, "run")
        self.assertEqual((request["operation"], request["slots"]), ("VERIFY_AND_DISCOVER", 3))
        self.assertEqual([(person["already_on_file"], person["to_verify"])
                          for person in request["research_context"]["people_found"]], [(True, False)])
        people = [candidate(known, "Owen Pratt")] + [
            candidate("https://www.linkedin.com/in/new" + str(index), "New Person" + " x" * index) for index in range(1, 4)]
        with tempfile.TemporaryDirectory() as root:
            run = lc.cycle(root, database, CONFIG, now=NOW, executor=stub(people))["runs"][0]
        self.assertEqual(run["state"], "COMPLETE", run)
        self.assertEqual(len(run["drafts"]), 3)                          # all 3 slots went to new people
        self.assertEqual([item["reason"] for item in run["rejected"]], ["ALREADY_ON_FILE"])

    def test_a_person_held_here_goes_the_held_path_and_one_held_elsewhere_is_on_file(self):
        """Bluesun-like: a person held (undrafted) at this company by an older research run is
        drafted on their reservation with no new-person slot; one held at another company is not."""
        database = fixture()
        here, elsewhere = "https://www.linkedin.com/in/theo-marsh", "https://www.linkedin.com/in/other-co"
        with database:
            database.execute("INSERT INTO rw_contacts VALUES ('acme robotics',?,'theo marsh',0,'older-plan')", (here,))
            database.execute("INSERT INTO rw_contacts VALUES ('globex',?,'other person',0,'older-plan')", (elsewhere,))
        people = [candidate(here, "Theo Marsh"), candidate(elsewhere, "Other Person")] + [
            candidate("https://www.linkedin.com/in/new" + str(index), "New Person" + " x" * index) for index in range(1, 4)]
        with tempfile.TemporaryDirectory() as root:
            run = lc.cycle(root, database, CONFIG, now=NOW, executor=stub(people))["runs"][0]
        self.assertEqual(run["state"], "COMPLETE", run)
        self.assertIn(here, [draft["profile"] for draft in run["drafts"]])
        self.assertEqual(len(run["drafts"]), 4)                          # the held person plus 3 new people
        self.assertEqual([(item["profile_url"], item["reason"]) for item in run["rejected"]],
                         [(elsewhere, "ALREADY_ON_FILE")])

    def test_same_day_coverage_supersedes_but_unplanned_work_does_not(self):
        database = fixture()
        day = workflow_tasks.service_date(NOW, "America/Los_Angeles")
        with database:
            database.execute("INSERT INTO rw_plans VALUES ('plan-1',?,'h','{}')", (day,))
            database.execute("INSERT INTO rw_companies VALUES (?,'acme robotics','unplanned:task')", (day,))
        self.assertEqual(len(lc.schedule(database, CONFIG, NOW)["due"]), 1)
        with database:
            database.execute("UPDATE rw_companies SET plan_id='plan-2'")
        self.assertEqual([item["reason"] for item in lc.schedule(database, CONFIG, NOW)["skipped"]],
                         ["SUPERSEDED_BY_NEWER_RESEARCH"])

    def test_a_second_result_of_the_same_research_plan_is_the_same_research_run(self):
        database = fixture()
        people = [candidate("https://www.linkedin.com/in/p" + str(index), "Person Number" + " x" * index)
                  for index in range(1, 8)]
        with tempfile.TemporaryDirectory() as root:
            first = lc.cycle(root, database, CONFIG, now=NOW, executor=stub(people[:3]))["runs"][0]
            self.assertEqual(len(first["drafts"]), 3)
            with database:      # the same plan writes another result for the company (a retried worker)
                row = database.execute("SELECT plan_id,raw,processed FROM rw_results WHERE id='r1'").fetchone()
                database.execute("INSERT INTO rw_results VALUES ('r1-retry',?,?,?,?)",
                                 (row[0], (NOW + dt.timedelta(minutes=1)).isoformat(), row[1], row[2]))
            planned = lc.schedule(database, CONFIG, NOW + dt.timedelta(minutes=2))
            self.assertEqual(planned["due"], [])
            self.assertEqual([item["reason"] for item in planned["skipped"]], ["FINDER_COMPLETED_FOR_THIS_RESEARCH_RUN"])
            later = lc.cycle(root, database, CONFIG, now=NOW + dt.timedelta(minutes=30), executor=stub(people[3:]))
        self.assertEqual(later["runs"], [])
        self.assertEqual(database.execute("SELECT COUNT(*) FROM rw_drafts").fetchone()[0], 3)

    def test_cycle_marks_stale_runs_interrupted(self):
        database = fixture()
        self.mark_run(database, "RUNNING", NOW - dt.timedelta(minutes=30), "stale")
        with tempfile.TemporaryDirectory() as root:
            lc.cycle(root, database, CONFIG, now=NOW, executor=lambda *args: 1 / 0)
        self.assertEqual(database.execute("SELECT state FROM linkedin_contact_runs WHERE run_id='stale'").fetchone()[0],
                         "FAILED")

    def test_used_slots_never_settle_while_held_people_remain(self):
        database = held_fixture()          # HELD reserved by plan-1: 1 of the run's 3
        with database:
            for index in (1, 2):
                database.execute("INSERT INTO rw_contacts VALUES ('acme robotics',?,?,0,'plan-1')",
                                 ("https://www.linkedin.com/in/drafted" + str(index), "drafted person"))
                database.execute("INSERT INTO rw_drafts VALUES (?,1,'h',?)",
                                 ("https://www.linkedin.com/in/drafted" + str(index), json.dumps({"company": "Other"})))
        due = lc.schedule(database, CONFIG, NOW)["due"]
        self.assertEqual([(item["slots"], item["verify_held"]) for item in due], [(0, [HELD])])
        with database:
            database.execute("INSERT INTO rw_drafts VALUES (?,1,'h',?)", (HELD, json.dumps({"company": "Other"})))
        self.assertEqual([item["reason"] for item in lc.schedule(database, CONFIG, NOW)["skipped"]],
                         ["RUN_CONTACT_LIMIT_USED"])

    def test_no_backfill_of_research_before_the_first_cycle(self):
        database = fixture()
        with database:
            database.execute("DELETE FROM rw_meta WHERE key=?", (lc.SINCE_KEY,))
        self.assertEqual(lc.schedule(database, CONFIG, NOW)["due"], [])
        with tempfile.TemporaryDirectory() as root:
            output = lc.cycle(root, database, CONFIG, now=NOW, executor=stub([]))
        self.assertEqual(output["runs"], [])     # research r1 was recorded before the first cycle
        self.assertEqual(database.execute("SELECT value FROM rw_meta WHERE key=?", (lc.SINCE_KEY,)).fetchone()[0],
                         NOW.isoformat())

    @patch.object(promotion, "OWNER_APPROVAL_PER_NOTE", True)  # the per-note hold, when re-enabled
    def test_linkedin_verified_drafts_are_never_actions_without_owner_approval(self):
        draft = {"recipient": "Ada Lovelace", "profile": "https://www.linkedin.com/in/ada",
                 "person_evidence": {"identity": {"kind": promotion.LINKEDIN_EVIDENCE_KIND}}}
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        outreach.initialize_actions(database)
        with self.assertRaisesRegex(ValueError, "OWNER_APPROVAL_REQUIRED_LINKEDIN_VERIFIED"):
            promotion._action(database, {"outreach_promotion_contexts": {
                "https://www.linkedin.com/in/ada": {"purpose": "PREPARE_CAMPAIGN"}}},
                draft, "https://www.linkedin.com/in/ada", {}, {}, NOW)

    def test_output_schema_is_strict(self):
        def objects(node):
            if isinstance(node, dict):
                if node.get("properties"):
                    yield node
                for child in list(node.get("properties", {}).values()) + [node.get("items")]:
                    yield from objects(child)
        nodes = list(objects(lc.schema()))
        self.assertEqual(len(nodes), 2)
        for node in nodes:
            self.assertEqual(node["required"], list(node["properties"]))
            self.assertIs(node["additionalProperties"], False)


if __name__ == "__main__":
    unittest.main()
