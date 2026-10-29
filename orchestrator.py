#!/opt/homebrew/bin/python3.13
import resources
import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import signal
import traceback
import re
import sqlite3
import subprocess
import sys
import threading
import time
import tomllib
import unicodedata
import urllib.parse
import urllib.request
import uuid
from zoneinfo import ZoneInfo

import daily_schedule
import log
import applications
import notifications
import reconciliation
import decision_transport
import reporting
import log_retention
import model_calls
from research_recovery import research_recovery_decision, research_recovery_methods
import workflow_tasks
import research_workflow
import reply_resume
import industry_discovery
import owner_seed
import portable
import run_locks
import scheduler_control
import user_profile
import paths

LOG = log.get(__name__)

RESEARCH_STOP = threading.Event()


ROOT = Path(__file__).resolve().parent
READ_TOOLS = frozenset({"get_campaign", "get_overall_stats", "get_conversations_v2",
                        "get_linked_in_account_by_id", "get_campaign_sequence",
                        "get_leads_from_list", "get_campaigns_for_lead", "get_chatroom", "get_lead",
                        "get_all_lists", "get_list_by_id", "get_all_campaigns", "get_lists_for_lead",
                        "get_my_network_for_sender"})
SOURCES = {
    "pipeline.json": "telegram-bridge/lib/recovered-pipeline.json",
    "delivery-evidence.json": "outputs/job-search-cycle-evidence.json",
    "recovery.json": "outputs/outreach-recovery.json",
}


def utcnow():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def digest(content):
    return hashlib.sha256(content).hexdigest()


def name_key(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def profile_key(value):
    parsed = urllib.parse.urlsplit(value)
    hostname = (parsed.hostname or "").lower()
    parts = urllib.parse.unquote(parsed.path).strip("/").split("/")
    if parsed.scheme not in {"http", "https"} or not (hostname == "linkedin.com" or hostname.endswith(".linkedin.com")):
        raise ValueError("A verified LinkedIn profile URL is required")
    if len(parts) != 2 or parts[0].lower() != "in" or not parts[1]:
        raise ValueError("A personal LinkedIn /in/ profile is required")
    return "linkedin.com/in/" + unicodedata.normalize("NFKC", parts[1]).casefold()


def write_json(path, value):
    # Unique per process: the hourly cycle and the applications job both write reports/.
    temporary = path.with_name(path.name + "." + str(os.getpid()) + "." + uuid.uuid4().hex[:8] + ".tmp")
    with temporary.open("w") as output:
        json.dump(value, output, indent=2, ensure_ascii=False)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


@contextlib.contextmanager
def single_instance(root):
    with (paths.state(root) / "runner.lock").open("a+") as lock:
        if not portable.try_lock(lock):
            yield False
            return
        try:
            yield True
        finally:
            portable.unlock(lock)


def connect(root):
    # The hourly cycle and the applications job write this database at the same time (owner,
    # 2026-10-02), so each waits out the other's short write transactions.
    database = sqlite3.connect(paths.database(root), timeout=run_locks.BUSY_TIMEOUT_SECONDS,
                               factory=run_locks.WriteFirstConnection)
    model_calls.use(paths.database(root))  # one row per model call (C4)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA journal_mode=WAL")
    database.execute("PRAGMA synchronous=FULL")
    database.executescript("""
        CREATE TABLE IF NOT EXISTS history (
            identity TEXT PRIMARY KEY, recipient TEXT NOT NULL,
            company TEXT NOT NULL, profile TEXT, evidence TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS suppressions (
            identity TEXT PRIMARY KEY, reason TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS runs (
            slot TEXT PRIMARY KEY, started_at TEXT NOT NULL,
            state TEXT NOT NULL, completed_at TEXT, result TEXT);
        CREATE INDEX IF NOT EXISTS runs_started_at ON runs(started_at);
        CREATE TABLE IF NOT EXISTS research (
            id TEXT PRIMARY KEY, started_at TEXT NOT NULL, state TEXT NOT NULL,
            result TEXT);
        CREATE INDEX IF NOT EXISTS research_started_at ON research(started_at);
        CREATE TABLE IF NOT EXISTS drafts (
            identity TEXT PRIMARY KEY, recipient TEXT NOT NULL,
            company TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS opportunities (
            url TEXT PRIMARY KEY, payload TEXT NOT NULL, observed_at TEXT NOT NULL);
    """)
    return database


# A fresh install (no backups/manifest.json and no config.json source_root) has no history to
# import: no prior outreach, no companies, no exclusions (E1, 2026-10-15).
EMPTY_PIPELINE = {"verified_prior_outreach": [], "companies": [],
                  "rules": {"exclude_all_outreach": [], "no_email": [], "max_unique_contacts_per_company": 3}}


def snapshot_sources(root, config):
    manifest_path = paths.backups(root) / "manifest.json"
    if not manifest_path.exists():
        if not config.get("source_root"):
            return json.loads(json.dumps(EMPTY_PIPELINE))
        manifest = {}
        for target, relative in SOURCES.items():
            source = Path(config["source_root"]).expanduser() / relative
            content = source.read_bytes()
            json.loads(content)
            destination = paths.backups(root) / target
            if destination.exists():
                if destination.read_bytes() != content:
                    raise ValueError("Existing unregistered backup differs; refusing to overwrite")
            else:
                with destination.open("xb") as output:
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())
            manifest[target] = {"source": str(source), "sha256": digest(content)}
        write_json(manifest_path, manifest)
    manifest = json.loads(manifest_path.read_text())
    for target in SOURCES:
        if digest((paths.backups(root) / target).read_bytes()) != manifest[target]["sha256"]:
            raise ValueError("Preserved tracker backup failed integrity verification")
    return json.loads((paths.backups(root) / "pipeline.json").read_text())


def import_history(database, root, pipeline):
    records = list(pipeline["verified_prior_outreach"])
    companies = pipeline["companies"]
    # Contacts whose delivery is recorded in backups/delivery-evidence.json come from the
    # optional owner seed (owner_seed.py); a new install has none and reads no evidence file.
    seeded = owner_seed.items("delivery_evidence_contacts")
    if seeded:
        evidence = json.loads((paths.backups(root) / "delivery-evidence.json").read_text())
        if evidence["delivery"]["status"] != "SENT_PROVIDER_CONFIRMED_AND_LINKEDIN_VERIFIED":
            raise ValueError("Seeded delivery evidence must be reconciled before proceeding")
        records.extend(dict(record, status=evidence["delivery"]["status"]) for record in seeded)
    with database:
        for record in records:
            recipient = record["recipient"]
            profile = record.get("profile")
            if not profile:
                profile = next((company.get("profile") for company in companies
                                if company.get("advocate") == recipient), None)
            identity = "name:" + name_key(recipient)
            database.execute("INSERT OR IGNORE INTO history VALUES (?,?,?,?,?)",
                             (identity, recipient, record["company"], profile, json.dumps(record)))
            identities = [identity]
            if profile:
                identities.append(profile_key(profile))
            for suppressed in identities:
                database.execute("INSERT OR IGNORE INTO suppressions VALUES (?,?)",
                                 (suppressed, "Previously contacted; no repeat initial outreach"))
        rules = pipeline["rules"]
        for recipient in rules["exclude_all_outreach"] + rules["no_email"]:
            database.execute("INSERT OR IGNORE INTO suppressions VALUES (?,?)",
                             ("name:" + name_key(recipient), "Existing user exclusion"))
        database.execute("UPDATE drafts SET state='SUPPRESSED' WHERE state<>'CLOSED' AND identity IN (SELECT identity FROM suppressions)")


class LeadNotFoundError(RuntimeError):
    pass


def unipile_install(config):
    """True when this install reaches LinkedIn through Unipile (the product default, RL1:
    config.json `"linkedin_provider": "unipile"`); "heyreach" is the owner's live-harness provider."""
    return config.get("linkedin_provider", "heyreach") == "unipile"


class HeyReachNotUsed(Exception):
    """Something reached for HeyReach on a Unipile install (RL1); a defect to fix, never a send."""


class NoHeyReach:
    """The HeyReach client slot on a Unipile install (RL1). A product user has no HeyReach endpoint
    (`credential_config`), so the adapters that still take a client get this one: building it reads
    nothing, and any call raises HeyReachNotUsed, so nothing silently uses HeyReach.
    `heyreach_available` False lets a caller skip HeyReach-only people (inactivity_closure, RB5)."""
    heyreach_available = False

    def __init__(self, config):
        self.config = dict(config)

    def call(self, tool, arguments):
        raise HeyReachNotUsed(tool)

    def start_claimed_campaign(self, database, campaign_id):
        raise HeyReachNotUsed("start_claimed_campaign")


def heyreach_for(config):
    """The HeyReach client for a step: NoHeyReach on a Unipile install, else HeyReach(config)."""
    return NoHeyReach(config) if unipile_install(config) else HeyReach(config)


HEYREACH_ONLY = {"state": "NOT_USED", "reason": "HEYREACH_ONLY_STEP_ON_UNIPILE", "errors": []}


def unipile_ready(config, port=None):
    """RL1: on a Unipile install, the port (built now unless given: no license raises
    LicenseRequired) once a sending identity is saved (`setup_steps.saved_sender`, laid over the
    config by `with_sender`); without one, SenderNotConfigured. Both are holds `worker_step` records
    before any paid model call, provider write or attempt."""
    import unipile_port
    port = port or linkedin_port_for(config)
    if type(config.get("sender_id")) is not int or config["sender_id"] <= 0 or not config.get("sender_profile"):
        raise unipile_port.SenderNotConfigured("SENDER_NOT_SAVED")
    return port


def sync_sender(config, home=None):
    """RL1, once per hourly run on a Unipile install: read the account through the relay and save
    what it reports (fills in an install connected before RL1 without a reconnect). A different
    account or own profile than the saved one (the user connected another LinkedIn) is saved over it
    and noted (UPDATED, with the previous profile): the relay already scopes every call to the
    license's current account, so holding would only stop outreach that can only go from that
    account; claims made under the old sender id stop matching and are refused, never resent. A
    read that fails changes nothing. Returns (config with the saved sender, outcome)."""
    import setup_steps
    home = home if home is not None else user_profile.home()
    try:
        account = linkedin_port_for(config).account()
        outcome = setup_steps.save_sender(home, account.account_id, account.own_profile)
    except Exception as error:   # no license, not linked, offline: the steps hold on their own
        outcome = {"state": "UNCHECKED", "error_type": type(error).__name__,
                   **({"blocker": error.code} if isinstance(getattr(error, "code", None), str) else {})}
    return setup_steps.with_sender(config, home), outcome


class HeyReach:
    def __init__(self, config):
        self.config = dict(config)
        with Path(config["credential_config"]).expanduser().open("rb") as source:
            settings = tomllib.load(source)
        self.url = settings["mcp_servers"]["heyreach"]["url"]
        if urllib.parse.urlsplit(self.url).scheme != "https":
            raise ValueError("HeyReach credential endpoint must use HTTPS")

    def call(self, tool, arguments):
        if tool not in READ_TOOLS:
            raise ValueError("Provider mutation is disabled in this orchestrator")
        return self._request(tool, arguments)

    def start_claimed_campaign(self, database, campaign_id):
        import engagement
        import outreach
        queued = database.execute("SELECT payload FROM outreach_queue WHERE campaign_id=?", (campaign_id,)).fetchone()
        if queued and json.loads(queued[0]).get("preparation_action_id"):
            outreach.final_campaign_guard(database, self.config, self, campaign_id)
        with engagement.transaction(database):
            attempt = database.execute("SELECT identity FROM outreach_attempts WHERE campaign_id=? AND state='CLAIMED'", (campaign_id,)).fetchone()
            if attempt and engagement.closure_status(database, attempt[0]):
                raise ValueError("OUTREACH_CLOSED")
            if attempt:
                import inactivity_closure
                gates = inactivity_closure.send_gates(database, getattr(self, "config", {}), attempt[0])
                if gates:
                    raise ValueError(";".join(gates))
            claim = database.execute("UPDATE outreach_attempts SET state='DISPATCHING' WHERE campaign_id=? AND state='CLAIMED'", (campaign_id,))
        if claim.rowcount != 1:
            raise ValueError("No unused durable campaign claim; refusing launch")
        return self._request("start_campaign", {"campaignId": campaign_id})

    def _request(self, tool, arguments):
        request_id = str(uuid.uuid4())
        payload = {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                   "params": {"name": tool, "arguments": arguments}}
        request = urllib.request.Request(self.url, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                raw = response.read(8_000_000).decode()
            packets = [json.loads(line[5:].strip()) for line in raw.splitlines() if line.startswith("data:")]
            if not packets:
                packets = [json.loads(raw)]
            packet = next(packet for packet in packets if str(packet.get("id")) == request_id)
            if packet.get("result", {}).get("isError"):
                content = " ".join(item.get("text", "") for item in packet["result"].get("content", []))
                if "Lead with profile url:" in content and "does not exist" in content:
                    raise LeadNotFoundError("Lead is not present in HeyReach")
                raise ValueError("Provider returned an error")
            if packet.get("error"):
                raise ValueError("Provider returned an error")
            result = packet["result"]
            if "structuredContent" in result:
                return result["structuredContent"]
            text = "\n".join(item["text"] for item in result.get("content", []) if item["type"] == "text")
            try:
                return json.loads(text)
            except ValueError:
                # A tool that answers in prose, not JSON, has still SUCCEEDED (owner, 2026-09-23:
                # "dont forget to fix the json decode error"). start_campaign does exactly this,
                # and json.loads raised AFTER the campaign had already started: campaigns 800001
                # (Avery) and 800002 (Jules) were both read back as status IN_PROGRESS with one
                # user in progress, while dispatch recorded UNCERTAIN_NO_RETRY because this line
                # threw JSONDecodeError. A successful send was being reported as an unknown
                # outcome, and no retry is permitted after one, so the invitation went out and the
                # ledger denied it. Every read tool that answers in JSON still parses above; only
                # a non-JSON body reaches here, and it is returned verbatim for the caller to
                # interpret rather than converted into a failure.
                return {"text": text, "parsed": False}
        except LeadNotFoundError:
            raise RuntimeError("Lead is not present in HeyReach") from None
        except Exception as error:
            # Carry the exception type and, for an HTTP failure, its status code. Deliberately
            # NOT the raw message: urllib embeds the request URL in it and that URL carries the
            # HeyReach credential. `from None` stays for the same reason.
            detail = type(error).__name__
            if isinstance(getattr(error, "code", None), int):
                detail += ":" + str(error.code)
            raise RuntimeError(f"HeyReach {tool} failed ({detail}); observation remains unknown") from None


def known_provider_lead(database, profile, sender_id):
    if not database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='engagement_events'").fetchone():
        return None
    identifiers = set()
    for row in database.execute("SELECT payload FROM engagement_events WHERE source='heyreach' AND identity=?",
                                (profile_key(profile),)):
        try:
            event = json.loads(row[0])
            raw = event["raw"]
            auth = event["inbox"]["authentication"]
            if (event.get("verified") is True and auth.get("method") == "authenticated_provider_read"
                    and auth.get("verified_by") == "heyreach-observed-schema-v1"
                    and raw["account_id"] == sender_id and type(raw["account_id"]) is int
                    and event["inbox"]["account_id"] == raw["account_id"]
                    and type(event["inbox"]["account_id"]) is int
                    and event["inbox"]["conversation_id"] == raw["conversation_id"]
                    and raw["provider_lead_id"] == raw["correspondent_id"]
                    and event["inbox"]["message_id"] == event["source_id"] == raw["message_id"]
                    and profile_key(raw["profile"]) == profile_key(profile)
                    and profile_key(event["profile"]) == profile_key(profile)
                    and isinstance(raw["correspondent_id"], str) and raw["correspondent_id"].strip()):
                identifiers.add(raw["correspondent_id"])
        except (ValueError, KeyError, TypeError):
            continue
    return next(iter(identifiers)) if len(identifiers) == 1 else None


def observe(database, config, client):
    result = {"observed_at": utcnow(), "errors": [], "contacts": [], "provider_mutations": 0}
    # The configured legacy campaign is observed only when one is configured (config.json
    # campaign_id; none for a new install).
    campaign_id = config.get("campaign_id")
    for key, tool, arguments in ([
        ("campaign", "get_campaign", {"campaignId": campaign_id}),
        ("stats", "get_overall_stats", {"campaignIds": [campaign_id]}),
    ] if type(campaign_id) is int else []):
        try:
            result[key] = client.call(tool, arguments)
        except RuntimeError as error:
            result["errors"].append(str(error))
    for contact in database.execute("SELECT recipient,profile FROM history ORDER BY recipient"):
        observation = {"recipient": contact["recipient"], "profile": contact["profile"]}
        try:
            if not contact["profile"]:
                raise RuntimeError("Verified profile missing; history remains unknown")
            lead_id = known_provider_lead(database, contact["profile"], config["sender_id"])
            arguments = {"linkedInAccountIds": [config["sender_id"]], "limit": 100}
            arguments.update({"leadLinkedInId": lead_id} if lead_id else {"leadProfileUrl": contact["profile"]})
            inbox = client.call("get_conversations_v2", arguments)
            if (not isinstance(inbox, dict) or type(inbox.get("totalCount")) is not int
                    or inbox["totalCount"] < 0 or not isinstance(inbox.get("items"), list)):
                raise RuntimeError("Unexpected inbox response; history remains unknown")
            if lead_id and any(not isinstance(item, dict) or item.get("linkedInAccountId") != config["sender_id"]
                               or item.get("linkedInAccount", {}).get("id") != config["sender_id"]
                               or profile_key(item.get("linkedInAccount", {}).get("profileUrl", "")) != profile_key(config.get("sender_profile", ""))
                               or item.get("correspondentProfile", {}).get("linkedin_id") != lead_id
                               or profile_key(item.get("correspondentProfile", {}).get("profileUrl", "")) != profile_key(contact["profile"])
                               for item in inbox["items"]):
                raise RuntimeError("Provider lead binding changed; history remains unknown")
            observation["lookup_basis"] = "SAVED_AUTHENTICATED_PROVIDER_ID" if lead_id else "TRACKER_PROFILE_URL"
            observation["conversation_count"] = inbox["totalCount"]
            observation["status"] = "REVIEW_CONVERSATIONS" if inbox["totalCount"] else "NO_HEYREACH_CONVERSATION"
            observation["history_complete"] = False
        except (RuntimeError, ValueError, TypeError, AttributeError) as error:
            observation["status"] = "UNKNOWN"
            result["errors"].append(f"{contact['recipient']}: {error}" if isinstance(error, RuntimeError)
                                    else f"{contact['recipient']}: Invalid inbox identity; history remains unknown")
        result["contacts"].append(observation)
    return result


def store_research(database, pipeline, result):
    import engagement
    if not isinstance(result.get("summary"), str) or not isinstance(result.get("blockers"), list):
        raise ValueError("Invalid research result")
    if database.in_transaction:
        required = {"opportunities", "drafts", "history", "suppressions", "application_jobs",
                    "application_events", "application_aliases", "application_source_keys",
                    "application_blockers"}
        existing = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = required - existing
        if missing:
            raise ValueError("Initialize research storage before the caller transaction: " + ", ".join(sorted(missing)))
    else:
        applications.initialize(database)
    rejected = []
    # At most max_unique_contacts_per_company new people per company per research run, with no
    # lifetime limit (owner, 2026-09-28). People contacted before are refused above, per person.
    added = {}
    def bad(entry, kind, reason):
        """A dropped entry (owner, 2026-10-27: "One bad entry no longer rejects the whole set"):
        kept out, the rest stored, and the reason recorded for the Failures and Logs pages."""
        entry = entry if isinstance(entry, dict) else {}
        text = lambda value: " ".join(value.split())[:200] if isinstance(value, str) and value.strip() else None
        rejected.append({"dropped": True, "kind": kind, "company": text(entry.get("company")),
                         "role": text(entry.get("role")), "recipient": text(entry.get("recipient")), "reason": reason,
                         # which posting or person this is (never shown): two different ones stay apart
                         "source": next((entry[key][:2048] for key in ("url", "source_url", "profile")
                                         if isinstance(entry.get(key), str) and entry[key].strip()), None)})
    with research_workflow.transaction(database):
        for opportunity in result["opportunities"][:60]:
            if not isinstance(opportunity, dict) or not all(
                    isinstance(opportunity.get(field), str) and opportunity[field].strip()
                    for field in ("company", "role", "url", "rationale")):
                bad(opportunity, "opportunity", "the role was missing its company, title, link or reason")
                continue
            if urllib.parse.urlsplit(opportunity["url"]).scheme != "https":
                bad(opportunity, "opportunity", "the role's link was not a secure (https) web address")
                continue
            try:
                with research_workflow.transaction(database):
                    database.execute("INSERT INTO opportunities VALUES (?,?,?) ON CONFLICT(url) DO UPDATE SET payload=excluded.payload,observed_at=excluded.observed_at",
                                     (opportunity["url"], json.dumps(opportunity), utcnow()))
                    applications.ingest(database, opportunity)
            except (KeyError, TypeError, ValueError) as error:
                bad(opportunity, "opportunity", "the role could not be saved")
                rejected[-1]["error_type"] = type(error).__name__   # internal only; never shown
        for draft in result["drafts"][:180]:
            if not isinstance(draft, dict) or not all(
                    isinstance(draft.get(field), str) and draft[field].strip()
                    for field in ("recipient", "company", "profile", "message", "source_url")):
                bad(draft, "draft", "the note was missing its recipient, company, profile, message or source")
                continue
            identity = profile_key(draft["profile"])
            recipient_key = "name:" + name_key(draft["recipient"])
            if engagement.closure_status(database, identity):
                rejected.append({"recipient": draft["recipient"], "reason": "OUTREACH_CLOSED"})
                continue
            if database.execute("SELECT 1 FROM suppressions WHERE identity IN (?,?)", (identity, recipient_key)).fetchone():
                rejected.append({"recipient": draft["recipient"], "reason": "Contacted or excluded"})
                continue
            existing = list(database.execute("SELECT recipient,company FROM drafts"))
            if any(name_key(row["recipient"]) == name_key(draft["recipient"]) for row in existing):
                rejected.append({"recipient": draft["recipient"], "reason": "Draft already exists"})
                continue
            company = name_key(draft["company"])
            if added.get(company, 0) >= pipeline["rules"]["max_unique_contacts_per_company"]:
                rejected.append({"recipient": draft["recipient"], "reason": "Research run contact limit"})
                continue
            added[company] = added.get(company, 0) + 1
            database.execute("INSERT OR IGNORE INTO drafts VALUES (?,?,?,?,?)",
                             (identity, draft["recipient"], draft["company"], "HELD_VERIFICATION", json.dumps(draft)))
    return rejected


def application_cycle(root, config, database, now=None, only=None, should_stop=None):
    if config.get("scope") == "monitor_existing_pipeline":
        return {"state": "DISABLED_BY_MONITOR_ONLY_SCOPE", "submissions": 0}
    if not config.get("applications_enabled", False):
        return {"state": "PAUSED_FOR_REVISED_REQUIREMENTS", "submissions": 0}
    if not user_profile.profile_path().exists():
        return {"state": "APPLICATION_POLICY_NOT_INSTALLED"}
    # The AI review of jobs parked after their investigations ran out is deleted (owner,
    # 2026-09-27). A parked job stays parked, visible in application_blockers.
    adapter = None
    result = applications.cycle(database, config, root, adapter, now, only=only, should_stop=should_stop)
    write_json(paths.reports(root) / "applications.json", result)
    return result


# Only the exception TYPE was recorded here, so every worker failure in reports/latest.json read
# "ValueError" with no way to tell which one - the conversion worker has been FAILED on every
# cycle since at least 2026-09-24 and the report could not say why. The message is kept now,
# whitespace-collapsed, length-bounded, and with any URL replaced: an exception message must
# never be able to carry the credential-bearing provider URL into a report file.
def failure_detail(error):
    """WHERE a worker failed, never WHAT it said.

    The message stays redacted on purpose - `test_callback_exception_rolls_back_uncommitted_work
    _and_redacts_details` asserts no exception text reaches this result, which is written to
    reports/latest.json and the durable task record. But recording only the type left every
    failure reading "ValueError" with nothing to chase: the conversion worker has been FAILED on
    every cycle since at least 2026-09-24 and the report could not say which raise it was. A
    module basename, a line number and a function name are code identity, not diagnostic text.
    """
    frames = traceback.extract_tb(error.__traceback__) if error.__traceback__ else []
    if not frames:
        return {}
    where = [os.path.basename(f.filename) + ":" + str(f.lineno) + " " + str(f.name) for f in frames[-3:]]
    # The innermost frame alone named workflow_tasks.py:116 for the conversion worker, which is
    # a shared helper with ten callers - true but not actionable. Three frames name the caller.
    return {"raised_at": where[-1], "raised_via": where[:-1]}


def license_hold(error):
    """True for unipile_port.SetupHold (RL1): no license key, the relay refused the license, LinkedIn
    not connected, or no saved sending identity."""
    import unipile_port
    return isinstance(error, unipile_port.SetupHold)


def worker_step(database, parent_run_id, name, function):
    now = utcnow()
    deadline = (dt.datetime.fromisoformat(now) + dt.timedelta(hours=6)).isoformat()
    task_id = None
    claim = None
    try:
        existing = database.execute("SELECT task_id FROM workflow_tasks WHERE worker=? AND action_id=?",
                                    (name, parent_run_id + ":" + name)).fetchone()
        if existing:
            task_id = existing["task_id"]
        else:
            with database:
                task_id = workflow_tasks.enqueue(database, name, parent_run_id + ":" + name, parent_run_id,
                                                  [{"source": "canonical-local-state", "observed_at": now}], deadline, now,
                                                  effect="WRITE", max_attempts=1)
        claim = workflow_tasks.claim(database, task_id, now, lease_seconds=21600)
        if claim["status"] != "CLAIMED":
            return {"state": claim["status"], "task_id": task_id}
        with log.tagged("task", task_id):   # the step's lines carry its task (Failures page, 2026-10-23)
            result = function()
        result = result if isinstance(result, dict) else {"state": "COMPLETE", "result": result}
        with database:
            # The step's record keeps its small fields; the caller (and the cycle's report files)
            # get the full result (owner, 2026-10-06: "trim/remove all the waste"; each hourly
            # step stored its full result twice, ~20 MB a day, the applications step ~12 MB).
            workflow_tasks.finish(database, task_id, claim["token"], utcnow(), compact_fields(result),
                                  [{"source": "local-worker:" + name, "observed_at": utcnow(),
                                    "meaning": "cycle result, not proof of external delivery"}])
        return result
    except Exception as error:
        database.rollback()
        hold = license_hold(error)
        if hold:
            # RL1: LinkedIn work waits on the user's license (none activated, or the relay refused
            # it). A hold the user can act on, not a worker failure.
            result = {"state": "BLOCKED", "blocker": error.code, "worker": name, "next_action": error.next_action}
        else:
            result = {"state": "FAILED", "error_type": type(error).__name__, "worker": name,
                      "next_action": "Inspect the durable worker evidence; reconcile uncertain effects before retry",
                      **failure_detail(error)}
        if claim and claim.get("token"):
            try:
                with database:
                    workflow_tasks.finish(database, task_id, claim["token"], utcnow(), result, [], status="BLOCKED",
                                          blocker=error.code if hold else "WORKER_EXCEPTION",
                                          next_action=result["next_action"])
            except ValueError:
                result["claim_state"] = "STALE_REQUIRES_RECONCILIATION"
            except Exception as persistence_error:
                result["persistence_error_type"] = type(persistence_error).__name__
        return result


def posting_judge_cycle(root, database, config):
    import posting_judge
    return posting_judge.cycle(root, database, config)


def conversion_cycle(root, database, config, parent_run_id):
    import conversion
    import pipeline_routing
    conversion.initialize(database)
    pipeline_routing.initialize(database)
    source = root / reconciliation.HANDOFF_DIR / "REQUIREMENTS.md"
    with database:
        if source.exists():
            conversion.seed_baseline_priorities(database, source)
        routing_config = dict(config)
        routing_config["pipeline_routing"] = dict(config.get("pipeline_routing", {}))
        if type(config.get("sender_id")) is int:
            routing_config["pipeline_routing"].setdefault("account_ids", [config["sender_id"]])
        routing = pipeline_routing.cycle(database, routing_config)
        application_policy = user_profile.load() if user_profile.profile_path().exists() else {}
        result = conversion.cycle(database, config, application_policy=application_policy)
        result["source_routing"] = routing
        import engagement
        result["engagement_review_handoff"] = engagement.handoff_reviews(database, routing_config)
        now = utcnow()
        result["durable_handoffs"] = []
        for task in result["tasks"]:
            prior = database.execute("SELECT task_id FROM workflow_tasks WHERE worker='conversion-handoff' AND action_id=?",
                                     (task["task_id"],)).fetchone()
            if prior:
                result["durable_handoffs"].append(prior[0])
                continue
            deadline = task["deadline"] or (dt.datetime.fromisoformat(now) + dt.timedelta(days=7)).isoformat()
            identifier = workflow_tasks.enqueue(database, "conversion-handoff", task["task_id"], parent_run_id,
                [{"source": "conversion_tasks:" + task["task_id"], "observed_at": now,
                  "payload_sha256": digest(json.dumps(task, sort_keys=True).encode()), "kind": task["kind"]}],
                deadline, now, company_id=task["company"], role_id=task.get("job_key"),
                contact_id=task.get("inputs", {}).get("contact_id"))
            if task.get("blocker"):
                database.execute("UPDATE workflow_tasks SET status='BLOCKED',blocker=?,next_action=? WHERE task_id=?",
                                 (task["blocker"], task["next_action"], identifier))
            result["durable_handoffs"].append(identifier)
    return result


def engagement_cycle(database, config, port=None):
    import engagement
    import heyreach_schema
    engagement.initialize(database)
    if config.get("engagement_enabled") is not True:
        return {"state": "DISABLED", "items": []}

    def prior_event(source, source_id):
        row = database.execute("SELECT payload FROM engagement_events WHERE source=? AND source_id=?",
                               (source, source_id)).fetchone()
        return json.loads(row[0]) if row else None

    if unipile_install(config):
        # RL1: a Unipile install reads only the Unipile inbox (E3.T3); the HeyReach inbox is the
        # owner's live-harness source.
        return {"state": "COMPLETE", "items": [], "heyreach": dict(HEYREACH_ONLY),
                "unipile": unipile_engagement_cycle(database, config, port)}
    client = HeyReach(config)
    decoder = heyreach_schema.Decoder(config["sender_id"], config["sender_profile"], prior_event=prior_event)
    adapter = engagement.HeyReachReadAdapter(client, config["sender_id"],
                                             decoder.decode_conversation, decoder.decode_chatroom,
                                             page_size=config.get("engagement_page_size", 20))
    result = engagement.cycle(database, config, adapter)
    if config.get("linkedin_provider") == "unipile":
        # The Unipile inbox (E3.T3) is read the same way, as its own source, over the port.
        result["unipile"] = unipile_engagement_cycle(database, config, port)
    return result


def unipile_engagement_cycle(database, config, port=None):
    import engagement
    reader = engagement.UnipileReadAdapter(unipile_ready(config, port), config["sender_id"],
                                           config["sender_profile"],
                                           lambda provider_id, chat_id: engagement.unipile_attendee_profile(
                                               database, provider_id, chat_id))
    return engagement.cycle(database, dict(config, engagement_sources=[engagement.UNIPILE]), reader)


def linkedin_port_for(config):
    """The LinkedInPort for one cycle: `unipile_port.for_install(config, <profile home>)` when
    `config["linkedin_provider"]` is "unipile" (the relay with the license key; the owner's
    unipile.json only under `"linkedin_transport": "local-dev"`, RL1; the shipped config.json sets
    "unipile"), else None ("heyreach" when the key is absent: the owner's live-harness provider keeps
    its own client and campaign path). No license key raises
    unipile_port.LicenseRequired, which `worker_step` records as a hold. Tests pass a
    linkedin_port.FakePort instead."""
    if config.get("linkedin_provider", "heyreach") != "unipile":
        return None
    import unipile_port
    return unipile_port.for_install(config, user_profile.home())


def draft_promotion_cycle(database, config, port=None, root=None):
    import outreach
    import outreach_promotion
    import strategy_execution
    if config.get("outreach_agent", {}).get("enabled") is True and database.in_transaction:
        raise ValueError("Commit caller changes before external outreach investigations")
    if database.in_transaction:
        tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"outreach_actions", "outreach_action_claims", "engagement_events", "engagement_stops"} <= tables:
            raise ValueError("Initialize outreach before opening the caller transaction")
    else:
        outreach.initialize_actions(database)
    strategy_execution.initialize(database)
    investigations = None
    if config.get("outreach_agent", {}).get("enabled") is True:
        import outreach_agent
        import outreach_campaign
        database.commit()
        client = heyreach_for(config)
        unipile = config.get("linkedin_provider") == "unipile"
        port = unipile_ready(config, port) if unipile else None
        # History comes from local records; no LinkedIn browser collector (owner, 2026-09-29). On
        # Unipile the inspection reads the person through the port (E3.T3).
        investigations = outreach_agent.prepare_drafts(paths.data(root), database, config, client, port=port)
        if unipile:
            # The invitation-note path becomes one INVITE action instead of a HeyReach campaign.
            import sequence
            contexts, failures = sequence.invite_contexts(port, investigations.get("campaign_candidates", []))
            campaigns = {"contexts": contexts, "invite_lookup_failures": failures}
        else:
            campaigns = outreach_campaign.prepare_candidates(database, config, client,
                                                             investigations.get("campaign_candidates", []))
        config = dict(config, outreach_promotion_contexts={**config.get("outreach_promotion_contexts", {}),
                                                         **investigations["contexts"], **campaigns["contexts"]})
        investigations["campaign_preparations"] = {key: value for key, value in campaigns.items() if key != "contexts"}
    now = utcnow()
    with research_workflow.transaction(database):
        result = outreach_promotion.cycle(database, config, now)
        if investigations is not None:
            result["history_investigations"] = {key: value for key, value in investigations.items()
                                                if key not in {"contexts", "campaign_candidates"}}
    return result


def reply_classification_cycle(root, database, config):
    import reply_worker
    routing_config = dict(config)
    routing_config["pipeline_routing"] = dict(config.get("pipeline_routing", {}))
    if type(config.get("sender_id")) is int:
        routing_config["pipeline_routing"].setdefault("account_ids", [config["sender_id"]])
    return reply_worker.cycle(root, database, routing_config)


def extended_outreach_cycle(database, config, root=None):
    import outreach
    if config.get("outreach_actions_enabled") is not True:
        return outreach.cycle(database, config, outreach.HeyReachActionAdapter())
    if config.get("outreach_agent", {}).get("enabled") is not True:
        return {"state": "BLOCKED", "sending_gate": "OUTREACH_AGENT_CONNECTION_DISABLED", "attempts": 0, "items": []}
    import followup_execution
    port = unipile_ready(config) if unipile_install(config) else None
    root = paths.data(root)
    adapter = followup_execution.FollowupAdapter(root, database, config, heyreach_for(config), port=port)
    # A judge rejection calls the producing worker back so the message is rewritten and
    # re-judged inside this same cycle (owner, 2026-09-23: "i want it in-run").
    import reply_execution
    return outreach.cycle(database, config, adapter, repair=lambda action, proof, at:
        reply_execution.repair_rejected(root, database, config, adapter, action, proof, at)
        if action.get("purpose") == "REPLY" else
        followup_execution.repair_rejected(root, database, config, adapter, action, proof, at))


def followup_execution_cycle(root, database, config):
    import followup_execution
    if config.get("followup_execution", {}).get("enabled") is not True:
        return {"state": "DISABLED"}
    port = unipile_ready(config) if unipile_install(config) else None
    adapter = followup_execution.FollowupAdapter(root, database, config, heyreach_for(config), port=port)
    return followup_execution.cycle(root, database, config, adapter)


def new_contact_execution_cycle(root, database, config, port=None):
    import outreach
    import outreach_agent
    import outreach_campaign
    if config.get("outreach_actions_enabled") is not True or config.get("outreach_agent", {}).get("enabled") is not True:
        return {"state": "DISABLED"}
    unipile = config.get("linkedin_provider") == "unipile"
    invites = None
    if unipile:
        import sequence
        port = unipile_ready(config, port)
        # Invitations sent in an earlier cycle are read before any new one is sent.
        invites = sequence.reconcile_invites(database, port, config=config)
        # Not Premium (owner, 2026-10-27): a note held back from its invitation is queued as the
        # first message once they accept; the FIRST_MESSAGE pass below judges, holds and sends it.
        invites["deferred_notes"] = sequence.queue_deferred_notes(database, port)
    client = heyreach_for(config)
    adapter = outreach_agent.Adapter(root, database, config, client, port=port if unipile else None)
    executions = {purpose: outreach.cycle(database, config, adapter, purpose=purpose)
                  for purpose in ("INITIAL", "FIRST_MESSAGE", "PREPARE_CAMPAIGN") + (("INVITE",) if unipile else ())}
    result = {"actions": executions, "campaigns": outreach_campaign.cycle(database, config, client, adapter)}
    if unipile:
        result["invites"] = invites
    return result


# The invitation tick (owner, 2026-10-24, option c: "run the invitation step every 15 minutes instead
# of hourly"). Invitations are paced 4-12 minutes apart (pacing.py), so the hourly round alone sends
# about one an hour; the scheduler also starts `sidedoor job invites` every 15 minutes
# (scheduler_loop.INVITE_INTERVAL_SECONDS) and it runs only the round's own invitation pieces, the
# same calls in the same order: `sequence.reconcile_invites` (earlier invitations read before any new
# one is sent, and an uncertain one retried under its pacing) and the INVITE pass of `outreach.cycle`
# (pacing.check, inspection, gates, Review-mode hold, claim, `sequence.send_invite`). It takes
# state/runner.lock non-blocking (`single_instance`, the lock the whole hourly round and daily
# research hold), so it never runs beside the round, research or another tick; busy, it skips quietly.
# Nothing else of the round runs here: no research, inbox, follow-ups, replies, first messages or
# applications.
INVITE_RECONCILE_STATES = ("ACKNOWLEDGED_DELIVERY_UNCONFIRMED", "INVITE_RETRY_WAIT", "DISPATCHING",
                           "REQUEST_STARTED", "UNCERTAIN_NO_RETRY")


def invite_tick(root, config, database, port=None):
    """One invitation tick. The caller holds state/runner.lock. Returns {"state": ...}; "logged" is
    True only when the tick read or sent something (a quiet tick writes no log line)."""
    if (not unipile_install(config) or config.get("scope") == "monitor_existing_pipeline"
            or config.get("outreach_actions_enabled") is not True
            or config.get("outreach_agent", {}).get("enabled") is not True):
        return {"state": "DISABLED"}
    if scheduler_control.paused(database):
        return {"state": "PAUSED"}
    if research_holds_turn(config, database):
        database.commit()
        return {"state": "YIELDED_TO_DAILY_RESEARCH"}
    import outreach
    import pacing
    outreach.initialize_actions(database)
    database.commit()
    reconcile = database.execute(
        "SELECT COUNT(*) FROM outreach_actions WHERE json_extract(payload,'$.purpose')='INVITE' AND state IN (%s)"
        % ",".join("?" * len(INVITE_RECONCILE_STATES)), INVITE_RECONCILE_STATES).fetchone()[0]
    queued = database.execute("SELECT COUNT(*) FROM outreach_actions WHERE json_extract(payload,'$.purpose')='INVITE' "
                              "AND state IN ('PENDING','BLOCKED')").fetchone()[0]
    # Nothing due: no invitation awaiting a read, and none queued that pacing lets go now. The INVITE
    # pass would only answer PACING_WAIT for each, so no port is built and nothing is written.
    if not reconcile and not (queued and pacing.check(database, config)["go"]):
        return {"state": "NOTHING_DUE", "queued": queued}
    import outreach_agent
    import sequence
    try:
        port = unipile_ready(config, port)
    except Exception as error:
        if license_hold(error):   # the hourly round records the hold; the tick only waits
            return {"state": "HELD", "blocker": getattr(error, "code", type(error).__name__)}
        raise
    result = {"state": "PROCESSED"}
    if reconcile:
        result["invites"] = sequence.reconcile_invites(database, port, config=config)
    adapter = outreach_agent.Adapter(root, database, config, heyreach_for(config), port=port)
    result["actions"] = outreach.cycle(database, config, adapter, purpose="INVITE")
    result["logged"] = True
    return result


def invite_summary(result):
    """One log line for a tick that did something: invitations sent and reads settled."""
    actions = result.get("actions") or {}
    items = actions.get("items") or []
    waits = sum(1 for item in items if item.get("state") == "PACING_WAIT")
    reads = (result.get("invites") or {}).get("items") or []
    return ("Invitation tick: %d invitation request(s) made, %d queued waiting for pacing, %d earlier invitation(s) read."
            % (actions.get("attempts", 0), waits, len(reads)))


def reply_execution_cycle(root, database, config):
    import champion_reporting
    import reply_execution
    import outreach
    promoted = reply_execution.promote(database, config)
    if promoted["state"] == "DISABLED":
        return promoted
    if config.get("outreach_agent", {}).get("enabled") is not True:
        return {"state": "BLOCKED", "sending_gate": "OUTREACH_AGENT_CONNECTION_DISABLED", "promotion": promoted}
    port = unipile_ready(config) if unipile_install(config) else None
    adapter = reply_execution.Adapter(root, database, config, heyreach_for(config), port=port)
    # A judge-rejected reply is rewritten once for the same inbound message and slot, in this
    # same run (owner, 2026-09-30), instead of staying blocked.
    executed = outreach.cycle(database, config, adapter, purpose="REPLY", repair=lambda action, proof, at:
        reply_execution.repair_rejected(root, database, config, adapter, action, proof, at))
    with database:
        champions = champion_reporting.sync(database)
    return {"state": executed["state"], "promotion": promoted, "execution": executed, "champion_handoffs": champions}


def agent_cycle(root, config, database):
    if config.get("agent_worker", {}).get("enabled") is not True:
        return {"state": "DISABLED", "external_writes": 0}
    import agent_worker
    limit = config["agent_worker"].get("max_items", 1)
    if type(limit) is not int or not 1 <= limit <= 4:
        raise ValueError("INVALID_AGENT_WORKER_LIMIT")
    superseded = []
    if config.get("application_recovery", {}).get("enabled") is True:
        now = utcnow()
        with database:
            for task in database.execute("""SELECT task_id FROM workflow_tasks
                    WHERE worker='agent-investigation' AND effect='READ'
                    AND action_id LIKE 'application-public-evidence:%'
                    AND (status IN ('QUEUED','RETRY_WAIT','BLOCKED')
                         OR (status='RUNNING' AND lease_until<=?))""", (now,)).fetchall():
                database.execute("""UPDATE workflow_tasks SET status='CANCELLED',
                    blocker='APPLICATION_RECOVERY_HANDOFF',next_action=?,updated_at=?,
                    token_hash=NULL,lease_until=NULL,next_at=NULL WHERE task_id=?""",
                    ("Existing application recovery owns this investigation; preserve prior findings", now, task["task_id"]))
                workflow_tasks.record(database, task["task_id"], "CANCELLED", now,
                    {"reason": "APPLICATION_RECOVERY_HANDOFF", "application_queue_preserved": True})
                superseded.append(task["task_id"])
        queued = []
    else:
        queued = agent_worker.enqueue_pending_applications(database, utcnow(), limit=limit)
    result = agent_worker.cycle(root, database, config)
    result["queued_from_applications"] = queued
    result["application_recovery_handoffs"] = superseded
    return result


def service_cycle(root, config, database):
    """One hourly service cycle. Application work is not part of it: applications run as their
    own job (`applications_service`, owner 2026-10-02), which also settles the application
    browser reservations first and last (owner, 2026-09-28), since only it makes them."""
    applications.initialize(database)
    workflow_tasks.initialize(database)
    database.commit()
    try:
        return _service_cycle_steps(root, config, database)
    finally:
        try:
            # Commit, never roll back: the callers commit whatever the cycle left open right
            # after it returns (run() updates `runs` inside `with database:`).
            database.commit()
        except Exception:
            pass


def _service_cycle_steps(root, config, database):
    import conversion
    applications.initialize(database)
    workflow_tasks.initialize(database)
    parent_run_id = "services:" + str(uuid.uuid4())
    result = {}
    result["decision_transport"] = worker_step(database, parent_run_id, "decision-inbox",
                                              lambda: decision_transport.cycle(database, config, root))
    if config.get("outreach_inactivity_days") is not None:
        result["inactivity_closure"] = worker_step(database, parent_run_id, "inactivity-closure",
            lambda: inactivity_closure_cycle(database, config))
    if config.get("agent_worker", {}).get("enabled") is True:
        try:
            result["agent_investigations"] = agent_cycle(root, config, database)
        except Exception as error:
            database.rollback()
            result["agent_investigations"] = {"state": "FAILED", "error_type": type(error).__name__,
                                               "next_action": "Resume durable read investigations; do not change action authorization"}
    if config.get("scope") != "monitor_existing_pipeline":
        result["conversion"] = worker_step(database, parent_run_id, "referral-interview-conversion",
                                            lambda: conversion_cycle(root, database, config, parent_run_id))
        # Asks the model whether saved official role evidence describes the same posting as its
        # application job (owner, 2026-09-24). Its own step, outside conversion's transaction, so
        # no database lock is held across a model call; bounded per cycle, charged to the shared
        # compute allowance, and it never blocks routing.
        result["posting_judge"] = worker_step(database, parent_run_id, "posting-judge",
                                               lambda: posting_judge_cycle(root, database, config))
        if (config.get("linkedin_contacts") or {}).get("enabled") is True:
            result["linkedin_contacts"] = worker_step(database, parent_run_id, "linkedin-contact-discovery",
                                                       lambda: linkedin_contacts_cycle(root, database, config))
        ports = []

        def port():
            # Unipile: one LinkedInPort per cycle, built on first use inside a step. HeyReach: no port.
            if config.get("linkedin_provider") != "unipile":
                return {}
            if not ports:
                ports.append(linkedin_port_for(config))
            return {"port": ports[0]}
        result["draft_promotion"] = worker_step(database, parent_run_id, "research-draft-promotion",
                                                 lambda: draft_promotion_cycle(database, config, root=root, **port()))
        result["new_contact_execution"] = worker_step(database, parent_run_id, "guarded-new-contact-execution",
            lambda: new_contact_execution_cycle(root, database, config, **port()))
        result["reply_classification"] = worker_step(database, parent_run_id, "reply-classification",
                                                       lambda: reply_classification_cycle(root, database, config))
        result["reply_decisions"] = reporting.reply_decisions(database)
        if result["reply_classification"].get("state") == "INTERRUPTED":
            result["state"] = "INTERRUPTED"
            try:
                write_json(paths.reports(root) / "service-status.json", result)
            finally:
                raise SystemExit(128 + signal.SIGTERM)
        if result["reply_classification"].get("decisions"):
            result["reply_conversion"] = worker_step(database, parent_run_id, "reply-referral-conversion",
                lambda: conversion_cycle(root, database, config, parent_run_id))
        result["reply_execution"] = worker_step(database, parent_run_id, "guarded-contextual-replies",
            lambda: reply_execution_cycle(root, database, config))
        result["due_followups"] = worker_step(database, parent_run_id, "guarded-due-followups",
            lambda: followup_execution_cycle(root, database, config))
        result["reply_decisions"] = reporting.reply_decisions(database)
    # Applications run as their own job (owner, 2026-10-02: "Run applications as their own job,
    # like research, with its own schedule and lock"); see `applications_service`.
    result["applications"] = {"state": APPLICATIONS_JOB_STATE}
    # The full recovery listing and daily report go to their report files; the cycle's own records
    # (runs.result, the daily-report task and its event) keep a summary. Owner, 2026-10-06 ("7"):
    # each hourly run stored the ~2 MB recovery listing and, after the report hour, the ~1.6 MB
    # saved report twice, which took the database from 5.2 GB (09-23) to 9.2 GB. The report is
    # saved once a day in daily_reports either way.
    recovery = reporting.bounded_recovery(database, config, utcnow())
    result["application_recovery"] = {key: value for key, value in recovery.items()
                                      if key not in ("records", "authorized_reopens")}
    reports = {"recovery-status.json": recovery}

    def daily_report():
        payload = reporting.daily(database, config)
        reports["daily-report.json"] = payload
        if not isinstance(payload, dict) or "day" not in payload:
            return payload
        return {"state": "SAVED", "day": payload["day"], "saved_in": "daily_reports",
                "report_delivery": payload.get("report_delivery"), "report_parts": payload.get("report_parts")}
    # Once a day, not every hour (owner, 2026-10-06): the step runs only when today's report is due
    # and not yet saved, so the other cycles record nothing for it.
    result["daily_report"] = (worker_step(database, parent_run_id, "daily-report", daily_report)
                              if reporting.daily_due(database, config) else {"state": "NOT_DUE_OR_ALREADY_SAVED"})
    result["notifications"] = worker_step(database, parent_run_id, "notification-dispatch",
                                           lambda: notifications.dispatch(database, config))
    result["decision_transport_after_send"] = worker_step(database, parent_run_id, "decision-bindings",
                                                         lambda: decision_transport.cycle(database, config, root))
    result["persistence_errors"] = []
    files = [("recovery-status.json", lambda: reports["recovery-status.json"]), ("service-status.json", lambda: result)]
    if "daily-report.json" in reports:
        files.insert(0, ("daily-report.json", lambda: reports["daily-report.json"]))
    for filename, payload in files:
        # reports/applications.json belongs to the applications job (application_cycle) now.
        try:
            write_json(paths.reports(root) / filename, payload())
        except Exception as error:
            result["persistence_errors"].append({"file": filename, "error_type": type(error).__name__})
    return result


APPLICATIONS_JOB_STATE = "SEPARATE_APPLICATIONS_JOB"
STORED_SERVICE_FIELD_BYTES = 2000


def compact_fields(value):
    """A dict with each field over STORED_SERVICE_FIELD_BYTES replaced by {"omitted_bytes": n}."""
    if not isinstance(value, dict):
        return value
    kept = {}
    for key, field in value.items():
        size = len(json.dumps(field, default=str))
        kept[key] = field if size <= STORED_SERVICE_FIELD_BYTES else {"omitted_bytes": size}
    return kept


def stored_run(result):
    """What runs.result keeps (owner, 2026-10-06: "trim/remove all the waste"): everything but the
    services' large contents. Each service keeps its scalar fields (state, counts, codes); a field
    over STORED_SERVICE_FIELD_BYTES becomes {"omitted_bytes": n}. Only `observation` is read back
    (reporting.snapshot); reports/latest.json keeps the full result."""
    services = result.get("services")
    stored = {key: (compact_fields(value) if isinstance(value, dict) and key not in {"observation", "services"} else value)
              for key, value in result.items()}
    if isinstance(services, dict):
        stored["services"] = {name: compact_fields(value) for name, value in services.items()}
    return stored


def _schedule_row(database, sql, parameters, read_only):
    """One row of the schedule tables; with `read_only` (Overview) a table not created yet is no row."""
    try:
        return database.execute(sql, parameters).fetchone()
    except sqlite3.OperationalError as error:
        if read_only and "no such table" in str(error):
            return None
        raise


def research_holds_turn(config, database, now=None, read_only=False):
    """The applications job yields exactly as the hourly cycle does: while today's research has
    had no turn yet (`research_turn_pending`), and also while today's research is RUNNING, which
    the hourly cycle never overlaps because research holds state/runner.lock. Research draws the
    fresh daily compute first (AGENTS "Service day starts at 9:00 Pacific").

    `read_only` (Overview's "Next run", so it and the jobs never disagree): the same test without
    creating the schedule tables; a table that does not exist yet reads as no row."""
    if research_turn_pending(config, database, now, read_only):
        return True
    if (config.get("scope") == "monitor_existing_pipeline" or not config.get("research_enabled")
            or "timezone" not in config or "research_hour" not in config):
        return False
    if not read_only:
        daily_schedule.initialize(database, config["timezone"])
    day = daily_schedule.due_day(now or dt.datetime.now(dt.timezone.utc), config)
    row = _schedule_row(database, "SELECT state FROM research_days WHERE day=?", (day,), read_only)
    return bool(row) and row[0] == "RUNNING"


def applications_service(root, config, database, only=None):
    """The applications job (owner, 2026-10-02), the one runner of application work.

    Launched hourly by its own launchd job under state/applications.lock (a run still going when
    the next one starts makes that one skip). It yields to daily research like the hourly cycle,
    settles the application browser reservations first and last (owner, 2026-09-28), and runs
    `application_cycle` as a durable worker step. Between jobs it stops if research takes its
    turn. Browser sessions take turns with the contact finder through state/browser.lock.
    Notifications it queues are sent by the hourly cycle's notification step.
    """
    result = {"job": "applications", "started_at": utcnow(),
              "invocation_source": "launchd" if os.environ.get("XPC_SERVICE_NAME", "").startswith(run_locks.LAUNCHD_LABEL_PREFIX) else "manual_or_unattributed"}
    if only is None and research_holds_turn(config, database):
        database.commit()
        # A yielding job still settles what earlier runs finished (owner, 2026-10-22: the compute
        # meter kept finished sessions' full bookings for hours while research held the turn).
        applications.settle_all_browser_reservations(database)
        result.update(state="YIELDED_TO_DAILY_RESEARCH", completed_at=utcnow())
        write_json(paths.reports(root) / "applications-service.json", result)
        LOG.info("Daily research has not finished its turn today; the applications job yields so research "
                 "draws the fresh allowance first. No second was reserved.")
        return result
    applications.initialize(database)
    workflow_tasks.initialize(database)
    database.commit()
    applications.settle_all_browser_reservations(database)
    try:
        parent_run_id = "applications-job:" + str(uuid.uuid4())
        # Asked between jobs, when nothing is left open (research_turn_pending creates its tables
        # with executescript, which would commit an open transaction).
        # A pause (the user's or the license pause; scheduler_control, E5.T1) stops it the same way:
        # between jobs, never inside one, and nothing signals it.
        stop = (lambda: not database.in_transaction and (research_holds_turn(config, database)
                                                         or scheduler_control.paused(database))) if only is None else None
        result["applications"] = worker_step(database, parent_run_id, "applications",
            lambda: application_cycle(root, config, database, only=only, should_stop=stop))
    finally:
        try:
            # Commit, never roll back, then settle: nothing is left unsettled when the job ends,
            # including when it fails or is interrupted.
            database.commit()
            applications.settle_all_browser_reservations(database)
        except Exception:
            pass
    result["completed_at"] = utcnow()
    result["state"] = "FAILED" if result["applications"].get("state") == "FAILED" else "COMPLETE"
    write_json(paths.reports(root) / "applications-service.json", result)
    return result


def linkedin_contacts_cycle(root, database, config):
    """Read-only logged-in LinkedIn contact verification/discovery (owner 2026-09-19)."""
    import linkedin_contacts
    return linkedin_contacts.cycle(root, database, config)


def inactivity_closure_cycle(database, config):
    import inactivity_closure
    port = unipile_ready(config) if unipile_install(config) else None
    return inactivity_closure.cycle(database, config, heyreach_for(config), **({"port": port} if port is not None else {}))


# Our own not-yet-sent records: a research draft, a queued/blocked/cancelled action (or one held for
# the owner's approval in review mode) or
# queue row, or a campaign-preparation action (creating a DRAFT campaign contacts no one).
UNSENT_OWN_STATES = {"HELD_VERIFICATION", "HELD_HISTORY_AND_USER_REVIEW", "PENDING", "PENDING_VALIDATION",
                     "BLOCKED", "HELD_INSPECTION", "CANCELLED", "CANCELLED_BEFORE_SEND", "SUPPRESSED",
                     "CONFIRMED_NOT_SENT", "PREPARED", "AWAITING_APPROVAL"}


def prior_contacts(database):
    """Contacts seed_history may mark as previously contacted.

    History and any send-capable or sent state (CLAIMED, DISPATCHING, REQUEST_STARTED,
    ACKNOWLEDGED_*, UNCERTAIN_NO_RETRY, CONFIRMED_SENT, CLOSED or an unknown state)
    count. A row that exists only because of our own unsent draft/action/queue entry
    is not prior contact when research already reserved that person at that company
    (rw_contacts), so the reservation keeps consuming its company slot without
    becoming "historical". An unreserved unsent row is still seeded conservatively.
    """
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    contacts = [dict(row) for row in database.execute("SELECT recipient,company,profile FROM history")]
    def reserved(saved):
        company = research_workflow.canonical(database, "company", saved["company"])
        identity = research_workflow.canonical(database, "contact", saved["profile"])
        return database.execute("SELECT 1 FROM rw_contacts WHERE company=? AND (identity=? OR name=?)",
                                (company, identity, research_workflow.name_key(saved["recipient"]))).fetchone() is not None
    for table in ("drafts", "outreach_queue", "outreach_actions"):
        if table not in tables:
            continue
        for row in database.execute("SELECT * FROM " + table):
            values = dict(row)
            saved = json.loads(values["payload"])
            if table == "drafts":
                saved = dict(recipient=values["recipient"], company=values["company"], profile=saved["profile"])
            if not all(saved.get(field) for field in ("recipient", "company", "profile")):
                continue
            unsent = table == "drafts" or values["state"] in UNSENT_OWN_STATES or (
                table == "outreach_actions" and saved.get("purpose") == "PREPARE_CAMPAIGN")
            if unsent and reserved(saved):
                continue
            contacts.append({field: saved[field] for field in ("recipient", "company", "profile")})
    return contacts


def research_inputs(root, config, database, pipeline, observation, output_path, *, companies=None, industry=None,
                    discovery=None, done_companies=()):
    if companies is None:
        raise ValueError("Industry discovery must supply company assignments; tracker seeds are not supported")
    policy = user_profile.load()
    if discovery is not None:
        if industry not in policy["industries"]:
            raise ValueError("Discovery evidence requires a policy industry")
        # Only the well-formed companies are evidence (owner, 2026-10-27: one bad entry no longer
        # rejects the whole set), and a PARTIAL discovery with at least one of them is kept.
        discovery = industry_discovery.screen(discovery, {**policy, "industries": [industry]}, utcnow(), 20)
        if discovery["discovery_status"] != "COMPLETE" and not discovery["companies"]:
            raise ValueError("Stage-2 evidence requires completed discovery")
    research_workflow.initialize(database)
    research_workflow.seed_history(database, pipeline, prior_contacts(database),
                                   history_complete=config.get("execution_ownership_verified", False))
    local_day = workflow_tasks.service_date(dt.datetime.now(dt.timezone.utc), config["timezone"])
    plan_id = "industry-research-plan:" + local_day
    if industry:
        plan_id += ":" + hashlib.sha256(industry.encode()).hexdigest()[:12]
    if config.get("_research_scope_id"):
        plan_id += ":scope:" + config["_research_scope_id"]
    existing_plan = database.execute("SELECT payload FROM rw_plans WHERE id=?", (plan_id,)).fetchone()
    if existing_plan:
        research_workflow.hold_unreported_contact_slots(database, plan_id)
    plan = research_workflow.get_plan(database, plan_id) if existing_plan else research_workflow.plan_research(database, plan_id, companies, limit=60)
    # A retry after a cut-off session covers only the companies not yet done (R1, owner 2026-10-27,
    # "3: a"), including ones an earlier run of this plan finished (review S9); their stored roles stay.
    done = set(done_companies) | set(research_workflow.saved_companies(database, plan_id))
    if done:
        plan = dict(plan, assignments=[item for item in plan["assignments"] if item["company_key"] not in done],
                    assignments_already_saved=[item["company"] for item in plan["assignments"] if item["company_key"] in done])
    discovery_evidence = []
    if discovery is not None:
        by_company = {}
        for record in discovery["companies"]:
            company = research_workflow.canonical(database, "company", record["company"])
            if company in by_company:
                raise ValueError("Ambiguous discovery company identity")
            by_company[company] = record
        # Research the assignments this discovery can vouch for; drop the rest (owner,
        # 2026-09-23). The invariant is that no company is researched without discovery
        # provenance, and filtering enforces that exactly, per company. Refusing the whole
        # INDUSTRY was collateral: `plan_id` is deterministic per day+industry+scope, so a
        # retry reuses the morning's plan while discovery legitimately returns different
        # companies each run - AI's plan held Sprocket AI and Polybius while its fresh discovery
        # returned Sprocket AI, Skynet Labs and Gadgetron. One company dropping
        # out killed the industry, and because discovery rarely repeats itself exactly, this
        # failed on EVERY retry. It is why 2026-09-23 produced zero roles across seven attempts.
        covered = [item for item in plan["assignments"] if item["company_key"] in by_company]
        if plan["assignments"] and not covered:
            raise ValueError("Research assignment lacks discovery evidence")
        dropped = [item["company"] for item in plan["assignments"] if item["company_key"] not in by_company]
        if dropped:
            plan = dict(plan, assignments=covered, assignments_without_discovery_evidence=dropped)
        for assignment in covered:
            discovery_evidence.append(by_company[assignment["company_key"]])
    resume_proof = research_workflow.seed_designated_claims(database, root)
    context = {"today": dt.datetime.now(ZoneInfo(config["timezone"])).isoformat(),
               "pipeline": pipeline,
               "contacted": [dict(row) for row in database.execute("SELECT recipient,company,profile FROM history")],
               "suppressed": [row[0] for row in database.execute("SELECT identity FROM suppressions")],
               "current_observation": observation}
    context["research_plan"] = plan
    context["verified_candidate_claims"] = [claim for _, claim in research_workflow.current_claims(database)]
    context["resume_proof"] = resume_proof
    # The qualification worker judges role_fit against the WHOLE raw résumé text saved at upload
    # (owner, 2026-10-21, E4.T2; it replaces the short candidate_profile cut of D-3).
    try:
        context["candidate_resume"] = reply_resume.context(root)
    except ValueError as error:
        context["candidate_resume"] = {"state": "UNAVAILABLE", "reason": str(error)}
    context["company_origin"] = "Current industry-discovery result, not existing pipeline or opportunity seeds"
    context["industry"] = industry
    context["policy"] = applications.policy_for_model(policy)   # the answer in force per semantic
    context["discovery_evidence"] = discovery_evidence
    import research_sources
    context["source_observations"] = research_sources.load(root, plan, utcnow())
    prompt = resources.read_text("research-prompt.md") + "\n\nDATA:\n" + json.dumps(context)
    command = research_command(root, config, output_path, "research-schema.json")
    return plan, plan_id, prompt, command


def research_command(root, config, output_path, schema):
    import engines
    return engines.engine(config).research_command(root, config, output_path, schema)


def run_research_worker(root, command, prompt, log_path, timeout, on_start=None, stop=None):
    """One research codex session. `on_start` (optional) is called with time.monotonic() at the moment the
    session really starts, after its pacing slot was granted, so a caller can start its own clock then.
    `stop` (optional callable): true once the caller's run has ended; a session still waiting for its slot
    then stops waiting, and one granted a slot after that never starts (review R2, 2026-10-27)."""
    ended = lambda: RESEARCH_STOP.is_set() or (stop is not None and bool(stop()))
    if timeout <= 0 or RESEARCH_STOP.is_set():
        raise TimeoutError("Research attempt time budget exhausted")
    import engines
    engine = engines.for_command(command)   # the engine that built the command (research_command)
    # The shell's OpenAI variables stripped; the saved API key added in API-key mode (E0.T8).
    environment = engine.research_environment(command)
    claude = engine.judged_by_stream   # Claude, Antigravity (U1 4): judged from the stream by finish_run
    import provider_pacing   # a slot for this provider; waiting for it never spends the session's timeout
    import tempfile   # Claude runs in an empty folder of its own (no project files or CLAUDE.md to load; U1 3d)
    with (tempfile.TemporaryDirectory(prefix="research-claude-") if claude else contextlib.nullcontext()) as working, \
            provider_pacing.slot(command, timeout, ended), log_path.open("w") as log:
        if ended():
            raise InterruptedError("Research run ended before this session got its slot")
        # <log>.engine.json: which engine wrote this log (U1 3a), with a Claude run's allowed tools (3c C3).
        engines.record_run(log_path, engine, command=command)
        # Its own session (POSIX) or Job Object (Windows), so the kill below reaches its whole tree.
        try:
            # E10: a .cmd shim (npm, pnpm, yarn) is started as the program it runs, never through cmd.exe.
            launch, environment = portable.direct_command(command, environment)
            worker = subprocess.Popen(launch, stdin=subprocess.PIPE, text=True,
                                      encoding="utf-8",
                                      cwd=getattr(command, "cwd", None) or working or root,
                                      stdout=log, stderr=log, env=environment, **portable.worker_spawn_options(core=False))
        except BaseException:
            engine.run_ended(command)   # U1 4 N3e: Antigravity's per-run MCP config goes when the start fails too
            raise
        portable.contain(worker)
        if on_start is not None:
            on_start(time.monotonic())

        started = time.monotonic()
        deadline = started + timeout
        pending_input = engine.prompt_input(prompt)
        timed_out = False
        try:
            while True:
                if RESEARCH_STOP.is_set():
                    raise InterruptedError("Research interrupted by service shutdown")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    worker.communicate(pending_input, timeout=min(1, remaining))
                    break
                except subprocess.TimeoutExpired:
                    pending_input = None
        finally:
            portable.kill_tree(worker.pid)
            worker.wait()
            engine.run_ended(command)   # U1 4: Antigravity's per-run MCP config goes
            if timed_out:   # A5: stopped at its time limit, its model_calls row all the same
                log.flush()
                engine.record_timed_out(log_path, command=command,
                                        worker="research", seconds=round(time.monotonic() - started, 3))
    if claude:
        # A Claude run can fail with exit 0 (U1 3d): judged from its stream whatever the exit code; a clean
        # run's answer is written to the command's output file, as Codex's --output-last-message does.
        failure = engine.finish_run(worker.returncode, log_path.read_text(encoding="utf-8", errors="replace"),
                                    command=command, worker="research")
        if failure is not None:
            words = (f"exited {worker.returncode}" if worker.returncode
                     else "failed " + str(getattr(failure, "code", None) or getattr(failure, "kind", None)))
            raise RuntimeError(f"Research worker {words}; see {log_path.name}") from failure
        return
    if worker.returncode:
        raise RuntimeError(f"Research worker exited {worker.returncode}; see {log_path.name}") from engine.limit(
            worker.returncode, log_path, command=command)
    engine.succeeded(command)   # E0.T8 AK5


def research(root, config, database, pipeline, observation):
    if config.get("scope") == "monitor_existing_pipeline" or not config.get("research_enabled", True):
        return {"state": "DISABLED_BY_MONITOR_ONLY_SCOPE"}
    daily_schedule.initialize(database, config["timezone"])
    import engines
    missing = engines.engine(config).wait(config)
    if missing:
        # No usable Codex (2026-10-23): research never starts and claims no attempt of the day; the
        # day's turn is recorded (CODEX_WAIT) so other work does not yield all day, and research runs
        # on the first check after Codex is found again. Claude (U1 3d) and Antigravity (U1 4): the same, in
        # their own words.
        return daily_schedule.codex_wait(database, config, dt.datetime.now(dt.timezone.utc),
                                         kind=getattr(missing, "kind", None))
    claim = daily_schedule.claim(database, config, dt.datetime.now(dt.timezone.utc))
    if claim["state"] != "CLAIMED":
        return claim
    research_id = claim["id"]
    log.set_tag("research", research_id)   # this run's lines carry its id (Failures page, 2026-10-23)
    RESEARCH_STOP.clear()

    def interrupted(signum, frame):
        RESEARCH_STOP.set()

    previous_handlers = {signum: signal.signal(signum, interrupted) for signum in (signal.SIGTERM, signal.SIGINT)}
    try:
        import parallel_research
        daily_limit = config.get("daily_compute_seconds", 10800)
        headroom = config.get("research_service_reserve_seconds", 1800)
        if (type(daily_limit) is not int or not 1 <= daily_limit <= 21000
                or type(headroom) is not int or not 0 <= headroom < daily_limit):
            raise ValueError("INVALID_RESEARCH_SERVICE_RESERVE")
        research_config = dict(config, daily_compute_seconds=daily_limit - headroom)
        final = parallel_research.run(root, research_config, database, pipeline, observation, research_id,
                                      runtime=sys.modules[__name__])
        final["compute_allocation"] = {"daily_seconds": daily_limit,
                                       "research_admission_ceiling": daily_limit - headroom,
                                       "protected_service_seconds": headroom}
        if RESEARCH_STOP.is_set():
            final.update(state="INTERRUPTED", error="Research stopped; completed industry handoffs remain durable")
    except Exception as error:
        final = {"state": "INTERRUPTED" if RESEARCH_STOP.is_set() else "FAILED",
                 "error": f"Parallel research failed ({type(error).__name__}); inspect per-industry reports for {research_id}"}
    finally:
        log.set_tag("research", None)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    final["schedule"] = daily_schedule.finish(database, config, research_id, final, dt.datetime.now(dt.timezone.utc))
    write_json(paths.reports(root) / (research_id + ".parallel.json"), final)
    return final


def research_service_tick(root, config, database, pipeline):
    """One research-service tick: daily research FIRST, then the service cycles.

    Research and every service consumer draw on the SAME daily compute pool
    (`workflow_tasks.reserve_compute`, one service day's allowance). Only research is
    capped below that pool, at `daily_compute_seconds - research_service_reserve_seconds`,
    and that reserve only protects the services if research draws first. So research
    runs before any `service_cycle` on this tick: on the tick that opens the compute day
    it claims the fresh budget up to its own ceiling and the remainder is left for the
    services.

    On every other tick the ordering is a no-op that costs nothing: `daily_schedule.claim`
    inside `research()` returns the recorded day state (COMPLETE / EXHAUSTED / RETRY_WAIT)
    without starting a worker or reserving a single second, exactly as before. The daily
    gate, its attempt counts and its retry state are untouched by this ordering.

    Both service cycles still run, in the same number and order relative to each other,
    and the second one still supplies the reported `services`/`applications` payload. The
    one behavioural difference is on the INTERRUPTED (SIGTERM) path, which now returns
    before either cycle instead of after the first: a shutdown tick no longer starts a
    fresh service cycle it cannot finish, and the next tick resumes it.
    """
    if not config["research_enabled"]:
        LOG.info("Research disabled in configuration")
        return 0
    applications.initialize(database)
    database.commit()
    # Application browser reservations are settled by the applications job, which alone makes
    # them (owner, 2026-10-02); settling them here could refund a booking that job is using.
    try:
        return _research_service_tick(root, config, database, pipeline)
    finally:
        try:
            database.commit()
        except Exception:
            pass


def _research_service_tick(root, config, database, pipeline):
    latest_path = paths.reports(root) / "latest.json"
    observation = json.loads(latest_path.read_text()).get("observation", {}) if latest_path.exists() else {}
    result = research(root, config, database, pipeline, observation)
    if result["state"] == "INTERRUPTED":
        # A shutdown (Sidedoor quit or restarted) is not a failure (owner 2026-10-24): exit 0.
        write_json(paths.reports(root) / "research-status.json", result)
        return 0
    # Research ONLY (owner, 2026-09-23: "separate it so that research only runs research alone
    # and the other runs the full cycle (excluding researh)"). This used to run research and
    # then TWO service_cycle passes in the same process, so loading the research agent by itself
    # still drove applications, replies and outreach: on 2026-09-23 that submitted aperture and
    # primatech while the owner believed only research was running. The hourly `run` job owns the
    # service cycles and already excludes research (`SEPARATE_DAILY_SERVICE`), so the two jobs
    # are now genuinely disjoint and either can be loaded on its own.
    result["services"] = {"state": "SEPARATE_HOURLY_SERVICE"}
    result["applications"] = {"state": APPLICATIONS_JOB_STATE}
    result["dispatcher_observed_at"] = utcnow()
    result["invocation_source"] = "launchd" if os.environ.get("XPC_SERVICE_NAME", "").startswith(run_locks.LAUNCHD_LABEL_PREFIX) else "manual_or_unattributed"
    write_json(paths.reports(root) / "research-status.json", result)
    log_retention.print_result(result, paths.reports(root) / "research-status.json")
    # Only a run that failed on this tick is an error. A tick that finds the day already EXHAUSTED
    # (`daily_schedule.claim`) started nothing (owner 2026-10-24: not a new error).
    return 1 if result["state"] == "FAILED" else 0


def research_turn_pending(config, database, now=None, read_only=False):
    """True on the one tick per day where the once-daily research has had no turn yet.

    Both launchd jobs run this file and share `state/runner.lock`. The hourly job
    (`run`) fires at Minute => 0 of EVERY hour, including the hour that opens the compute
    day; the research job fires every 300 seconds. At the boundary they race, and when
    `run` wins it holds the lock and spends the fresh allowance on services while the
    research service prints "Another orchestrator cycle is active; skipped."

    That is not a fair race, because research is admitted against the WHOLE day's spend:
    `reserve_compute` compares `SUM(seconds) WHERE day=?` against
    `daily_compute_seconds - research_service_reserve_seconds`, so every second the
    services draw first comes straight off research's ceiling rather than out of a
    separate bucket. `research_service_tick` already orders research first, but only for
    the job that calls it, which is the job that loses the race.

    So on that one tick, this cycle yields the lock instead of spending. The research
    service takes the lock within its 300-second interval and runs research ALONE (owner,
    2026-09-23); this hour's services are therefore deferred to the next hourly tick rather
    than being run by the research job, which no longer touches them.

    Bounded to at most one hourly slot per day. `daily_schedule.claim` writes the day's
    row on its first attempt whatever the outcome - RUNNING, COMPLETE, RETRY_WAIT or
    EXHAUSTED - so from the next tick on this returns False and `run` proceeds normally.
    Every uncertainty resolves toward running, never toward waiting: this returns False
    unless the configuration positively describes a daily research that will write that
    row. Disabled research, monitor-only scope, or a configuration missing `timezone` or
    `research_hour` all fall through to a normal cycle, so a deferral can never become a
    permanent stall.
    """
    if (config.get("scope") == "monitor_existing_pipeline" or not config.get("research_enabled")
            or "timezone" not in config or "research_hour" not in config):
        return False
    if not read_only:
        daily_schedule.initialize(database, config["timezone"])
    day = daily_schedule.due_day(now or dt.datetime.now(dt.timezone.utc), config)
    row = _schedule_row(database, "SELECT state FROM research_days WHERE day=?", (day,), read_only)
    if row is None:
        return True
    # An owner-authorized retry that research has not yet consumed is also a pending turn:
    # the retry comes with a restored allowance, and the same race would hand it to the
    # services first. daily_schedule.claim consumes the authorization on research's next
    # tick, so this too lasts at most one hourly slot.
    saved = _schedule_row(database, "SELECT value FROM schedule_meta WHERE key=?", ("forced_research_retry:" + day,), read_only)
    try:
        pending = json.loads(saved[0]).get("state") == "AUTHORIZED" if saved else False
    except (TypeError, ValueError, AttributeError):
        pending = False
    return pending and row[0] not in {"RUNNING", "COMPLETE", daily_schedule.CODEX_WAIT}


def settle_idle_browser_reservations(root, database):
    """Settle finished application browser bookings at the start of an hourly round.

    Owner, 2026-10-22 ("compute budget still says 2 h 18 min ... used"): only the applications
    job settled them, and it yields to daily research for hours. The round settles them only
    while the applications job is not running (its lock is free): a running job settles its own
    bookings per job, and a booking between its read and its submit must not be closed under it.
    `settle_browser_reservations` itself never refunds a started session that has not committed,
    nor a request still queued. Never raises; returns the seconds credited (None when skipped).
    """
    try:
        with run_locks.held(root, run_locks.APPLICATIONS) as free:
            if not free:
                return None
            if database.in_transaction:
                database.commit()
            return applications.settle_all_browser_reservations(database)
    except Exception:
        try:
            database.rollback()
        except Exception:
            pass
        return None


def run(root, config, database, pipeline, local_only=False):
    slot = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H") + (":local-check" if local_only else "")
    with database:
        recorded = database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'").fetchone() and \
            database.execute("SELECT 1 FROM runs WHERE slot=?", (slot,)).fetchone()
        if recorded:
            # A duplicate start of a slot already run (e.g. the scheduler firing again after a
            # restart): nothing runs and nothing is recorded, not even a yield, so Overview's
            # "Last run" keeps the slot's real round. A row left RUNNING by a killed round is
            # still closed (this process holds the runner lock, so no round is running).
            database.execute("UPDATE runs SET state='INTERRUPTED' WHERE state='RUNNING'")
    if recorded:
        LOG.info("This hourly slot is already recorded; no duplicate execution.")
        return 0
    if not local_only:
        settle_idle_browser_reservations(root, database)
    if not local_only and research_turn_pending(config, database):
        # The only record of a yield (no runs row is claimed, so the slot stays free): Overview's
        # Outreach "Last run" shows it as "waited for research".
        # Display only: a failed write never changes the yield.
        try:
            paths.reports(root).mkdir(parents=True, exist_ok=True)
            write_json(paths.reports(root) / "hourly-yield.json",
                       {"state": "YIELDED_TO_DAILY_RESEARCH", "slot": slot, "at": utcnow()})
        except OSError:
            pass
        LOG.info("Daily research has not run yet today; this hourly cycle yields the lock so "
                 "research draws the fresh allowance first. No second was reserved.")
        return 0
    with database:
        database.execute("UPDATE runs SET state='INTERRUPTED' WHERE state='RUNNING'")
        claimed = database.execute("INSERT OR IGNORE INTO runs(slot,started_at,state) VALUES (?,?,'RUNNING')", (slot, utcnow()))
    if claimed.rowcount != 1:
        LOG.info("This hourly slot is already recorded; no duplicate execution.")
        return 0
    monitor_only = config.get("scope") == "monitor_existing_pipeline"
    result = {"slot": slot, "mode": "MONITOR_EXISTING_PIPELINE" if monitor_only else "GUARDED_OUTREACH", "outreach_sends": 0,
              "preserved_contacts": database.execute("SELECT COUNT(*) FROM history").fetchone()[0]}
    try:
        if local_only:
            result["observation"] = {"state": "LOCAL_CHECK_ONLY"}
            result["research"] = {"state": "LOCAL_CHECK_ONLY"}
        else:
            workflow_tasks.initialize(database)
            result["observation"] = worker_step(database, slot, "provider-observation",
                                                  lambda: dict(HEYREACH_ONLY) if unipile_install(config)
                                                  else observe(database, config, HeyReach(config)))
            if monitor_only:
                result["research"] = {"state": "DISABLED_BY_MONITOR_ONLY_SCOPE"}
                result["outreach"] = {"state": "DISABLED_BY_MONITOR_ONLY_SCOPE", "provider_mutations": 0}
                result["applications"] = {"state": "DISABLED_BY_MONITOR_ONLY_SCOPE", "submissions": 0}
            else:
                result["research"] = {"state": "SEPARATE_DAILY_SERVICE"}
                import outreach
                if unipile_install(config):
                    config, result["linkedin_sender"] = sync_sender(config)
                result["engagement"] = worker_step(database, slot, "reply-monitor",
                                                    lambda: engagement_cycle(database, config))
                # Inactivity review runs inside service_cycle below; running it here as well
                # made every hourly tick review the same recipients twice.
                result["outreach"] = worker_step(database, slot, "guarded-outreach",
                                                  lambda: dict(HEYREACH_ONLY) if unipile_install(config)
                                                  else outreach.dispatch(database, config, pipeline, HeyReach(config)))
                result["extended_outreach"] = worker_step(database, slot, "guarded-message-actions",
                                                           lambda: extended_outreach_cycle(database, config, root))
            result["services"] = service_cycle(root, config, database)
            result["applications"] = {"state": APPLICATIONS_JOB_STATE}
        result["state"] = "DEGRADED" if result["observation"].get("errors") or any(
            value.get("state") == "FAILED" for value in result.values() if isinstance(value, dict)) or any(
            value.get("state") == "FAILED" for value in result.get("services", {}).values() if isinstance(value, dict)) else "COMPLETE"
    except Exception as error:
        result.update(state="FAILED", error=f"Cycle failed ({type(error).__name__}); inspect the durable outreach ledger before any retry")
    result["completed_at"] = utcnow()
    with database:
        database.execute("UPDATE runs SET state=?,completed_at=?,result=? WHERE slot=?",
                         (result["state"], result["completed_at"], json.dumps(stored_run(result)), slot))
    write_json(paths.reports(root) / ("local-check.json" if local_only else "latest.json"), result)
    log_retention.print_result(result, paths.reports(root) / ("local-check.json" if local_only else "latest.json"))
    return 0 if result["state"] == "COMPLETE" else 1


JOB_COMMANDS = ("run", "research", "applications", "invites")


def main(argv=None):
    from_command_line = argv is None
    argv = list(sys.argv[1:] if argv is None else argv)
    import scheduler_loop
    if from_command_line and scheduler_loop.frozen():
        # The frozen app (E9.T1): Finder starts Sidedoor.app's binary with no arguments (old macOS
        # adds a -psn_ process serial number), and a double-click is `sidedoor serve`.
        argv = [value for value in argv if not value.startswith("-psn_")] or ["serve"]
    # `sidedoor reply-tools serve --directory D` (E9.T1): the reply AI's MCP server in a frozen build,
    # which has no reply_tools.py to start (reply_tools.server_argv). Its stdout is the JSON-RPC channel.
    if argv[:1] == ["reply-tools"]:
        import reply_tools
        log.configure(job="reply-tools")  # the install's secrets redacted from its stderr; INFO never on stdout
        return reply_tools.main(argv[1:])
    # `sidedoor research-tools serve --context F` (R1): a research session's save tools in a frozen build.
    if argv[:1] == ["research-tools"]:
        import research_tools
        log.configure(job="research-tools")  # stderr only; stdout is the JSON-RPC channel
        return research_tools.main(argv[1:])
    # `sidedoor browser-guard ...` (E7.T1, option A): the guarded browser MCP server Codex starts for
    # every browser job (browser_runtime.guard_command). Its stdout is the JSON-RPC channel, so it is
    # dispatched before anything else here can print.
    if argv[:1] == ["browser-guard"]:
        import browser_guard
        log.configure(job="browser-guard")  # as reply-tools: stderr only, the install's secrets redacted
        return browser_guard.main(argv[1:])
    # `sidedoor agy-hook --policy FILE` (U1 4): an Antigravity run's PreToolUse hook (engines.agy_hook). Its
    # stdout is the decision.
    if argv[:1] == ["agy-hook"]:
        import engines
        return engines.agy_hook_main(argv[1:])
    # `sidedoor net-check` (hotfix 2026-10-28): one HTTPS GET of OpenRouter's model list through
    # text_model's urllib path, with this process's trust (tls_setup). The Mac build's release gate
    # runs it with every default certificate location empty (tools/check_release_tls.py).
    if argv[:1] == ["net-check"]:
        return net_check()
    # `sidedoor doctor [--online] [--json]` (E4.T4): the dependency report (doctor.main).
    if argv[:1] == ["doctor"]:
        import doctor
        return doctor.main(argv[1:])
    # `sidedoor job run|research|applications` (E5.T1): how the scheduler loop (scheduler_loop)
    # starts each job as a child of this same program. It enters exactly the paths `run`,
    # `research` and `applications` enter, with the same locks and outputs.
    if argv[:1] == ["job"]:
        if len(argv) < 2 or argv[1] not in JOB_COMMANDS:
            print("usage: sidedoor job run|research|applications|invites", file=sys.stderr)
            return 2
        argv = argv[1:]
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["init", "run", "check", "status", "research", "applications", "invites", "transport-test", "outreach", "validate", "enqueue-campaign", "close-outreach", "expire-outreach", "dashboard", "setup", "serve", "supervise", "doctor"])
    parser.add_argument("campaign_file", nargs="?")
    parser.add_argument("--profile")
    parser.add_argument("--reason")
    parser.add_argument("--only", action="append",
                        help="restrict an applications run to these job keys; repeatable. "
                             "Scopes the Gmail history sweep too, not just the job loop.")
    parser.add_argument("--no-browser", action="store_true", help="dashboard, setup, serve: print the link without opening it")
    parser.add_argument("--start", action="store_true",
                        help="serve: start the scheduler at once (start at login, E5.T2); serve already does once Setup's Start succeeded")
    parser.add_argument("--at-login", action="store_true",
                        help="serve, supervise: started by the start-at-login entry (E5.T2)")
    parser.add_argument("--home", help="serve, supervise: the Sidedoor home the start-at-login entry was registered for")
    args = parser.parse_args(argv)
    if args.start and args.command not in ("serve", "supervise"):
        parser.error("--start is for serve only")
    if args.home:  # before anything reads the home (user_profile.home)
        os.environ["SIDEDOOR_HOME"] = args.home
    # Diagnostics carry this job's name (log.py, E2.T6), set after --home so their secrets are this
    # home's; the scheduler's job commands keep their INFO status lines on stdout, as always.
    log.configure(job="serve" if args.command in ("dashboard", "setup", "serve", "supervise") else args.command,
                  stdout_info=args.command in ("run", "check", "research", "applications", "invites"))
    if args.at_login:
        import login_item
        login_item.mark_started_at_login()
    if args.command == "supervise":
        # Windows start at login (E5.T2): runs `serve` with no console and starts it again after a
        # non-zero exit (login_item.supervise).
        import login_item
        return login_item.supervise_main(args.home)
    os.umask(0o077)
    if args.command in ("dashboard", "setup", "serve"):
        # FastAPI and uvicorn are imported only for these commands. `setup` (E4.T1 option C) asks
        # nothing in the terminal: it creates the home and the dashboard token, starts the dashboard
        # (or reopens the running one) and opens it on the Setup page. `serve` is the Sidedoor
        # process (E5.T1): the dashboard plus the scheduler loop, started at once once Setup's Start
        # has succeeded (what opening the app runs; owner, 2026-10-25); `dashboard` and `setup` only
        # serve the pages until [Start].
        import dashboard
        return dashboard.serve(open_browser=not args.no_browser, path="/setup" if args.command == "setup" else "/",
                               start=args.start, tray=True, resume=args.command == "serve")
    # Everything this command reads or writes is in the Sidedoor home (E9.T1); data an older
    # version left in the code folder moves there first, once (paths.migrate).
    try:
        root = paths.prepare()
    except paths.MigrationPending as error:
        # A wait, not a failure: the scheduler shows HOLD_EXIT as "Waiting to move your data".
        LOG.warning("Waiting to move your data: %s", error)
        return paths.HOLD_EXIT
    if args.command == "close-outreach":
        closure = json.loads(Path(args.campaign_file).read_text()) if args.campaign_file else {
            "profile": args.profile, "reason": args.reason, "source": "explicit-cli"}
        if not isinstance(closure, dict) or not all(closure.get(field) for field in ("profile", "reason", "source")):
            parser.error("close-outreach requires a JSON file with profile/reason/source and optional closed_at, or --profile and --reason")
        import engagement
        with single_instance(root) as acquired:
            if not acquired:
                raise ValueError("Another orchestrator cycle is active; closure was not recorded")
            with contextlib.closing(connect(root)) as database:
                engagement.initialize(database)
                # Machine contract (E2.T6): the command's JSON result; stays print.
                print(json.dumps(engagement.close_outreach(database, closure["profile"], closure["reason"],
                                                          closure.get("closed_at"), source=closure["source"]), indent=2))
        return 0
    import setup_steps
    # RL1: the sending identity saved in the home when LinkedIn was confirmed (config.json is
    # bundled and read-only); every job's config reader sees it from here on.
    config = setup_steps.with_sender(resources.read_json("config.json"))
    if args.command == "expire-outreach":
        with single_instance(root) as acquired:
            if not acquired:
                raise ValueError("Another orchestrator cycle is active; inactivity review deferred")
            with contextlib.closing(connect(root)) as database:
                result = inactivity_closure_cycle(database, config)
                write_json(paths.reports(root) / "inactivity-closure.json", result)
                # Machine contract (E2.T6): the command's JSON result; stays print.
                print(json.dumps(result, indent=2))
        return 0
    if config.get("scope") == "monitor_existing_pipeline" and args.command in {"research", "applications", "outreach", "enqueue-campaign"}:
        # Machine contract (E2.T6): the command's JSON result; stays print.
        print(json.dumps({"state": "DISABLED_BY_MONITOR_ONLY_SCOPE", "command": args.command}))
        return 0
    if args.command == "status":
        with contextlib.closing(sqlite3.connect(f"file:{paths.database(root)}?mode=ro", uri=True)) as database:
            database.row_factory = sqlite3.Row
            latest = database.execute("SELECT slot,state,started_at,completed_at FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
            # Machine contract (E2.T6): the command's JSON result; stays print.
            print(json.dumps({"scope": config.get("scope", "guarded_outreach"),
                              "research_enabled": config["research_enabled"],
                              "applications_enabled": config.get("applications_enabled", False),
                              "outreach_mode": config["outreach_mode"],
                              "latest_run": dict(latest) if latest else None,
                              "preserved_contacts": database.execute("SELECT COUNT(*) FROM history").fetchone()[0],
                              "held_drafts": database.execute("SELECT COUNT(*) FROM drafts WHERE state IN ('HELD_HISTORY_AND_USER_REVIEW','HELD_VERIFICATION')").fetchone()[0]}, indent=2))
            if database.execute("SELECT 1 FROM sqlite_master WHERE name='research_days'").fetchone():
                # Machine contract (E2.T6): the command's JSON result; stays print.
                print(json.dumps({"next_daily_research": daily_schedule.next_daily_time(dt.datetime.now(dt.timezone.utc), config) if config["research_enabled"] and config.get("scope") != "monitor_existing_pipeline" else None,
                                  "research_days": [dict(row) for row in database.execute("SELECT * FROM research_days ORDER BY day DESC LIMIT 3")]}, indent=2))
            if database.execute("SELECT 1 FROM sqlite_master WHERE name='engagement_skipped_pages'").fetchone():
                # Machine contract (E2.T6): the command's JSON result; stays print.
                print(json.dumps({"inbox_skipped_pages": [dict(row) for row in database.execute(
                    "SELECT source,cursor,next_cursor,failures,error_type,error,skipped_at "
                    "FROM engagement_skipped_pages ORDER BY skipped_at DESC LIMIT 5")]}, indent=2))
            if database.execute("SELECT 1 FROM sqlite_master WHERE name='engagement_decode_holds'").fetchone():
                # Machine contract (E2.T6): the command's JSON result; stays print.
                print(json.dumps({"inbox_undecodable": [dict(row) for row in database.execute(
                    "SELECT identity,conversation_id,error_type,reason,first_seen,last_seen,failures "
                    "FROM engagement_decode_holds ORDER BY first_seen")]}, indent=2))
            import reply_worker
            held_senders = reply_worker.held_senders(database)
            if held_senders:
                # Machine contract (E2.T6): the command's JSON result; stays print.
                print(json.dumps({"held_reply_senders": held_senders}, indent=2))
            import sequence  # each contact's place in the outreach sequence (E3.T3 sequence_view)
            from zoneinfo import ZoneInfo
            # Machine contract (E2.T6): the command's JSON result; stays print.
            print(json.dumps({"outreach_sequences": sequence.positions(
                database, ZoneInfo(config.get("timezone") or "America/Los_Angeles"))}, indent=2))
            if database.execute("SELECT 1 FROM sqlite_master WHERE name='workflow_tasks'").fetchone():
                # Machine contract (E2.T6): the command's JSON result; stays print.
                print(json.dumps({"applications_job": reporting.applications_job(database)}, indent=2))
            if database.execute("SELECT 1 FROM sqlite_master WHERE name='application_jobs'").fetchone():
                # Machine contract (E2.T6): the command's JSON result; stays print.
                print(json.dumps(applications.report(database), indent=2))
            return 0
    if args.command == "invites":
        # The invitation tick (owner, 2026-10-24; `invite_tick`): state/runner.lock, non-blocking. A
        # round, research or another tick holding it makes this one skip, quietly.
        with single_instance(root) as acquired:
            if not acquired:
                return 0
            with contextlib.closing(connect(root)) as database:
                with log.tagged("invites", uuid.uuid4().hex):
                    try:
                        result = invite_tick(root, config, database)
                    except Exception as error:
                        LOG.warning("Invitation tick stopped (%s); the hourly round sends invitations as before.",
                                    type(error).__name__)
                        return 1
                    if result.get("logged"):
                        LOG.info("%s", invite_summary(result))
        return 0
    if args.command in {"run", "research"}:
        log_retention.startup(root)
    if args.command == "applications":
        # The applications job (owner, 2026-10-02): its own lock, never state/runner.lock, so the
        # hourly cycle and research run whatever applications are doing. A run still going when
        # the next one starts makes the new one skip. It waits up to 10 s first, so the hourly
        # round's brief settle under this lock (settle_idle_browser_reservations) never makes it skip.
        with run_locks.held(root, run_locks.APPLICATIONS, wait_seconds=10, poll_seconds=0.5) as acquired:
            if not acquired:
                # Machine contract (E2.T6): the skip line the scheduler log and tests read; stays print.
                print("Another applications run is active; skipped.")
                return 0
            with contextlib.closing(connect(root)) as database:
                result = applications_service(root, config, database, only=set(args.only) if args.only else None)
        if args.only:
            # An explicitly scoped run: the application path for these job keys only.
            # Machine contract (E2.T6): the command's JSON result; stays print.
            print(json.dumps(result, indent=2))
        else:
            log_retention.print_result(result, paths.reports(root) / "applications-service.json")
        return 1 if result.get("state") == "FAILED" else 0
    with single_instance(root) as acquired:
        if not acquired:
            # Machine contract (E2.T6): the skip line the scheduler log and tests read; stays print.
            print("Another orchestrator cycle is active; skipped.")
            return 0
        with contextlib.closing(connect(root)) as database:
            pipeline = snapshot_sources(root, config)
            import_history(database, root, pipeline)
            daily_schedule.initialize(database, config["timezone"])
            daily_schedule.recover_interrupted(database, config, dt.datetime.now(dt.timezone.utc))
            import outreach
            outreach.initialize(database)
            applications.initialize(database)
            reconciliation_result = reconciliation.merge(database, root, profile_key, name_key) if config.get("applications_enabled", False) and config.get("scope") != "monitor_existing_pipeline" else {}
            reconciliation.apply_baselines(database, pipeline, name_key)
            if reconciliation_result:
                write_json(paths.reports(root) / "reconciliation.json", reconciliation_result)
            if args.command == "init":
                if (paths.backups(root) / "manifest.json").exists():
                    print("Tracker backed up and verified; historical contacts imported idempotently.")
                else:
                    print("No tracker backups and no config.json source_root: starting with an empty history.")
                return 0
            if args.command == "transport-test":
                with database:
                    identifier = decision_transport.request_transport_test(database, user_profile.load(), utcnow())
                registered = decision_transport.cycle(database, config, root)
                notifications.dispatch(database, config)
                bound = decision_transport.cycle(database, config, root)
                result = {"test_id": identifier, "registered": registered, "message_binding_and_poll": bound,
                          "decision": dict(database.execute("SELECT id,state,message_id FROM user_decisions WHERE id=?", (identifier,)).fetchone()),
                          "application_submissions_attempted_by_test": 0, "outreach_attempted_by_test": 0}
                write_json(paths.reports(root) / "transport-test.json", result)
                # Machine contract (E2.T6): the command's JSON result; stays print.
                print(json.dumps(result, indent=2))
                return 0
            if args.command == "research":
                return research_service_tick(root, config, database, pipeline)
            if args.command == "enqueue-campaign":
                if not args.campaign_file:
                    raise ValueError("A prepared campaign evidence file is required")
                # Machine contract (E2.T6): the command's JSON result; stays print.
                print(json.dumps(outreach.enqueue(database, json.loads(Path(args.campaign_file).read_text())), indent=2))
                return 0
            if args.command in {"outreach", "validate"}:
                result = outreach.dispatch(database, config, pipeline, HeyReach(config), validate_only=args.command == "validate")
                if args.command == "validate":
                    client = HeyReach(config)
                    result["observation"] = observe(database, config, client)
                    import linkedin_port
                    result["sender"] = linkedin_port.HeyReachPort(client, config["sender_id"]).account().raw
                    if type(config.get("campaign_id")) is int:
                        result["existing_campaign_sequence"] = client.call("get_campaign_sequence", {"campaignId": config["campaign_id"]})
                    result["verified_at"] = utcnow()
                write_json(paths.reports(root) / (args.command + ".json"), result)
                # Machine contract (E2.T6): the command's JSON result; stays print.
                print(json.dumps(result, indent=2))
                return 0
            return run(root, config, database, pipeline, local_only=args.command == "check")


def net_check(check=None):
    """`sidedoor net-check`: prints one JSON line {ok, status, trust, ca_file, error_class,
    certificate_error, error} and returns 0 when OpenRouter answered over HTTPS (any HTTP status),
    else 1. `ca_file`: the certificate file tls_setup chose (the bundled one, or the user's), else null."""
    import text_model
    import tls_setup
    trust = tls_setup.state()
    result = {"ok": False, "status": None, "trust": trust["mode"], "ca_file": trust["ca_file"], "error_class": None,
              "certificate_error": False, "error": None}
    try:
        result["status"] = (check or text_model.check_connection)()
        result["ok"] = True
    except text_model.TextModelResponseError as error:
        result.update(error_class=error.transport_error or type(error).__name__,
                      certificate_error=error.certificate_error, error=str(error))
    except Exception as error:
        result.update(error_class=type(error).__name__, error=str(error)[:300])
    # Machine contract (hotfix 2026-10-28): the release gate reads this line; stays print.
    print(json.dumps(result))
    return 0 if result["ok"] else 1


def cli():
    """The `sidedoor` console command (pyproject.toml); the same as `python3.13 orchestrator.py`."""
    import tls_setup
    tls_setup.configure()   # HTTPS trust before anything can make a call (tools/sidedoor.py does it first)
    import scheduler_loop
    code = portable.ensure_utf8_mode(scheduler_loop.frozen(), LOG.warning)   # Windows only (W1 a)
    if code is not None:
        sys.exit(code)
    try:
        sys.exit(main())
    except Exception as error:
        LOG.error("Orchestrator stopped (%s: %s); original tracker preserved.", type(error).__name__, error)
        sys.exit(1)


if __name__ == "__main__":
    cli()
