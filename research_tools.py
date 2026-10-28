"""Research saves as it goes (R1, owner 2026-10-27: "1: B, 2: a, 3: a").

A small stdio MCP server one research session loads: `save_company` (discovery) and `save_role`
(qualification: one role with its people). Each saved entry gets the same per-entry check the store
path runs (industry_discovery.check_company / research_workflow.check_role); a problem goes back to
the AI in plain words so it can fix the entry and save it again. An accepted entry is appended as one
flushed JSON line to that session's own journal file. The server never opens the database: Sidedoor
reads the journal on the main thread when the session ends, finished or cut off (parallel_research).

Saved entries and the final output are merged by what each entry IS (the company's name key, the
role's job identity: company plus requisition or posting URL), never an AI-reported id. The last
save of an entry wins, and the final output's copy wins over a saved one.
"""

import copy
import datetime as dt
import json
import os
import sys
from pathlib import Path

SERVER = "research_tools"
JOURNAL_SUFFIX = ".journal.jsonl"
CONTEXT_SUFFIX = ".tools.json"


def journal_path(output):
    """The session's journal: `<research_id>.<industry>.<sequence>.<phase>.journal.jsonl`."""
    return Path(output).with_suffix(JOURNAL_SUFFIX)


def context_path(output):
    return Path(output).with_suffix(CONTEXT_SUFFIX)


def _inline(schema, definitions):
    """The schema with its local $refs written out, so any engine reads the tool's input shape."""
    if isinstance(schema, dict):
        reference = schema.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            return _inline(definitions[reference.split("/")[-1]], definitions)
        return {key: _inline(value, definitions) for key, value in schema.items() if key != "$defs"}
    if isinstance(schema, list):
        return [_inline(value, definitions) for value in schema]
    return schema


def tools(phase):
    import resources
    if phase == "discovery":
        item = json.loads(resources.read_text("discovery-schema.json"))["properties"]["companies"]["items"]
        return [{"name": "save_company",
                 "description": ("Save one discovered company the moment you have confirmed it, with the same fields "
                                 "your final answer uses for it. A problem comes back in plain words: fix the entry "
                                 "and save it again. Your final answer still lists every company."),
                 "inputSchema": {"type": "object", "additionalProperties": False, "required": ["company"],
                                 "properties": {"company": item}}}]
    schema = json.loads(resources.read_text("research-schema.json"))
    item = _inline(schema["properties"]["opportunities"]["items"], schema.get("$defs", {}))
    return [{"name": "save_role",
             "description": ("Save one checked role, with its people, the moment you have confirmed it, with the "
                             "same fields your final answer uses for it. A problem comes back in plain words: fix "
                             "the entry and save it again. Your final answer still lists every role."),
             "inputSchema": {"type": "object", "additionalProperties": False, "required": ["role"],
                             "properties": {"role": item}}}]


# --- entry identity and checks (shared with the ingest) -----------------------------------------

def company_key(record):
    import industry_discovery
    return industry_discovery._key(record["company"])


def role_key(role):
    import applications
    return applications.identity(role)[2]


def _now():
    return dt.datetime.now(dt.timezone.utc)


class Session:
    def __init__(self, context):
        self.context = context
        self.journal = Path(context["journal"])
        self.saved = {key for key, _ in _entries(self.journal, context["phase"])}

    def call(self, name, arguments):
        phase = self.context["phase"]
        if not isinstance(arguments, dict):
            return "Nothing was saved: the call had no entry.", True
        if phase == "discovery" and name == "save_company":
            return self.save_company(arguments.get("company"))
        if phase == "qualification" and name == "save_role":
            return self.save_role(arguments.get("role"))
        return "Nothing was saved: this session has no tool called " + str(name) + ".", True

    def save_company(self, record):
        import industry_discovery
        try:
            key = industry_discovery.check_company(record, self.context["industries"], _now(), set())
        except ValueError as error:
            return "Not saved: " + _label(record) + str(error) + ". Fix the entry and save it again.", True
        if key not in self.saved and len(self.saved) >= self.context["limit"]:
            return ("Not saved: you have already saved the most companies this session may return ("
                    + str(self.context["limit"]) + ")."), True
        self._append({"kind": "company", "entry": record}, key)
        return "Saved " + record["company"].strip() + ".", False

    def save_role(self, role):
        import research_workflow
        try:
            if not isinstance(role, dict):
                raise ValueError("the role entry was incomplete or malformed")
            research_workflow.check_role(role, now=_now().isoformat(), policy=self.context.get("policy"))
            key = role_key(role)
        except (KeyError, TypeError, ValueError) as error:
            return ("Not saved: " + _label(role) + _plain(error) + ". Fix the entry and save it again."), True
        self._append({"kind": "role", "entry": role}, key)
        return "Saved " + str(role["role"]).strip() + " at " + str(role["company"]).strip() + ".", False

    def _append(self, line, key):
        # One writer per journal; the line is on disk before the AI hears it was saved.
        with self.journal.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self.saved.add(key)


def _label(entry):
    name = entry.get("company") if isinstance(entry, dict) else None
    return (name.strip() + ": " if isinstance(name, str) and name.strip() else "")


def _plain(error):
    text = str(error).strip()
    return text if text else "the role entry was incomplete or malformed"


def _reply(identity, result=None, error=None):
    # Machine contract: MCP JSON-RPC over stdout; stays print.
    message = {"jsonrpc": "2.0", "id": identity}
    message.update({"error": error} if error else {"result": result})
    print(json.dumps(message), flush=True)


def serve(context_file):
    """Serve until stdin closes. A bad message gets a JSON-RPC error and the server keeps serving."""
    context = json.loads(Path(context_file).read_text(encoding="utf-8"))
    session = Session(context)
    listed = tools(context["phase"])
    for raw in sys.stdin.buffer:
        try:
            try:
                line = raw.decode("utf-8")
            except UnicodeDecodeError:   # review T4: an undecodable line is answered, not fatal
                _reply(None, error={"code": -32700, "message": "Parse error"})
                continue
            if not line.strip():
                continue
            if len(line) > 4 * 1024 * 1024:
                _reply(None, error={"code": -32600, "message": "Request too large"})
                continue
            try:
                message = json.loads(line)
            except Exception:   # ValueError, and RecursionError on deep nesting (review U3)
                _reply(None, error={"code": -32700, "message": "Parse error"})
                continue
            if not isinstance(message, dict) or "id" in message and not isinstance(message["id"], (str, int, type(None))):
                _reply(None, error={"code": -32600, "message": "Invalid request"})
                continue
            if "id" not in message:
                continue   # a notification: no reply
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            method = message.get("method")
            try:
                if method == "initialize":
                    result = {"protocolVersion": params.get("protocolVersion", "2024-11-05"),
                              "capabilities": {"tools": {}}, "serverInfo": {"name": "research-tools", "version": "1"}}
                elif method == "ping":
                    result = {}
                elif method == "tools/list":
                    result = {"tools": listed}
                elif method == "tools/call":
                    try:
                        text, failed = session.call(params.get("name"), params.get("arguments"))
                    except Exception:
                        text, failed = "Nothing was saved; put the entry in your final answer.", True
                    result = {"content": [{"type": "text", "text": text}], "isError": failed}
                else:
                    _reply(message["id"], error={"code": -32601, "message": "Method not found"})
                    continue
            except Exception:
                _reply(message["id"], error={"code": -32603, "message": "Internal error"})
                continue
            _reply(message["id"], result)
        except Exception:   # nothing about one message ends the server (review U3)
            try:
                _reply(None, error={"code": -32603, "message": "Internal error"})
            except Exception:
                pass
    return 0


def main(argv):
    if len(argv) == 3 and argv[0] == "serve" and argv[1] == "--context":
        return serve(argv[2])
    sys.stderr.write("usage: research_tools serve --context FILE\n")
    return 2


# --- the research run's side (main thread) -------------------------------------------------------

def server_argv(context_file):
    """From source: this Python with research_tools.py; a frozen build: its own binary's subcommand."""
    import scheduler_loop
    tail = ["serve", "--context", str(context_file)]
    if scheduler_loop.frozen():
        return [scheduler_loop.program(), "research-tools"] + tail
    import portable
    return [sys.executable, *portable.UTF8_FLAGS, str(Path(__file__).resolve())] + tail


def prepare(output, phase, industry, limit, policy, config):
    """Write the session's tool context and return the settings that attach the server to a session
    started with `config` (Codex: its -c settings)."""
    output = Path(output)
    context = {"phase": phase, "industries": [industry], "limit": limit, "policy": policy,
               "journal": str(journal_path(output))}
    journal_path(output).unlink(missing_ok=True)   # a fresh session starts with an empty journal
    context_path(output).write_text(json.dumps(context), encoding="utf-8")
    import engines
    return engines.engine(config).mcp_server_options(SERVER, server_argv(context_path(output)), 20, 30,
                                               [tool["name"] for tool in tools(phase)])


def attach(command, options):
    """The research command with the server's settings, placed before its trailing stdin marker
    (research_command's shape). Any other command shape is returned as it is."""
    import engines
    return engines.for_command(command).attach(command, options)   # the engine that built the command


def _entries(path, phase):
    """(key, entry) per readable journal line of this phase. Each line is decoded and parsed on its own,
    so a torn or half-written line is skipped and every other line is kept."""
    kind = "company" if phase == "discovery" else "role"
    try:
        data = Path(path).read_bytes()
    except OSError:
        return []
    found = []
    for raw in data.split(b"\n"):
        try:
            record = json.loads(raw.decode("utf-8"))
            if not isinstance(record, dict) or record.get("kind") != kind or not isinstance(record.get("entry"), dict):
                continue
            entry = record["entry"]
            found.append((company_key(entry) if kind == "company" else role_key(entry), entry))
        except (ValueError, KeyError, TypeError, AttributeError):   # UnicodeDecodeError is a ValueError
            continue
    return found


def read_journal(path, phase):
    """The saved entries, one per identity (the last save wins), in the order first saved."""
    entries = {}
    for key, entry in _entries(path, phase):
        entries[key] = entry
    return list(entries.values())


def _key_or_none(function, entry):
    try:
        return function(entry)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def _company_name_key(entry):
    import research_workflow
    return research_workflow.name_key(entry["company"])


def merge_discovery(result, saved, limit, note=None, *, industries=None, now=None):
    """The discovery result to store. The final output wins (review R1 S4): a saved company is used only
    when the final output has no valid copy of it; a saved copy replaces an invalid final copy (S5).

    `result` None (cut off, no usable final output): a PARTIAL result of the saved companies with `note`.
    No saved entries, or nothing to add: `result` itself, unchanged."""
    import industry_discovery
    cap = min(20, limit)
    if result is None:
        return {"discovery_status": "PARTIAL", "summary": note, "companies": list(saved)[:cap], "blockers": [note]}
    if not saved or not isinstance(result.get("companies"), list):
        return result
    now = industry_discovery._timestamp(now) if now else _now()

    def valid(company):
        try:
            industry_discovery.check_company(company, industries or [], now, set())
            return True
        except (ValueError, KeyError, TypeError, AttributeError):
            return False
    by_key = {company_key(company): company for company in saved}
    companies, changed, present = [], False, set()
    for company in result["companies"]:
        key = _key_or_none(company_key, company)
        if key in by_key and not valid(company):
            company, changed = by_key[key], True    # S5: the valid saved copy, not a lost entry
        companies.append(company)
        present.add(key)
    extra = [company for key, company in by_key.items() if key not in present]
    if not extra and not changed:
        return result
    merged = copy.deepcopy(result)
    merged["companies"] = (companies + extra)[:max(cap, len(companies))]
    if merged.get("discovery_status") == "FAILED" and merged["companies"]:
        # The saved companies are kept (the PARTIAL-kept rule); a FAILED result already has blockers.
        merged["discovery_status"] = "PARTIAL"
    return merged


def merge_research(result, saved, note=None, *, now=None, policy=None):
    """The qualification result to store. Guarantees:
    - The final output wins where it covers a company by the store path's own rule (a sourced role or a
      verified, uncontradicted finding; research_workflow.covered_companies): saved roles there are not
      added, so a role re-reported under another link is stored once (S4, T1).
    - At a company the final output does not cover, every saved role is added; when one of those is
      sourced, the final output's unsourced roles at that company are dropped (U2).
    - A saved role replaces the final copy of the same job identity when that copy is invalid (S5), or
      when it is unsourced and the saved copy is sourced (V1).
    - With no saved roles, or nothing to change, `result` itself is returned unchanged.

    `result` None (cut off, no usable final output): a PARTIAL result of the saved roles with `note`.
    No saved entries, or nothing to add: `result` itself, unchanged."""
    import research_workflow
    if result is None:
        return {"inability": None, "research_status": "PARTIAL", "summary": note, "blockers": [note],
                "action_blockers": [], "scope_violation": {"unknown": False, "companies": []},
                "company_findings": [], "opportunities": list(saved), "drafts": []}
    if not saved or not isinstance(result.get("opportunities"), list):
        return result
    now = now or _now().isoformat()

    def valid(role):
        try:
            research_workflow.check_role(role, now=now, policy=policy)
            return True
        except (ValueError, KeyError, TypeError, AttributeError):
            return False
    def sourced(role):
        try:
            return research_workflow.role_sourced(research_workflow.check_role(role, now=now, policy=policy)[0])
        except (ValueError, KeyError, TypeError, AttributeError):
            return False
    by_key = {role_key(role): role for role in saved}
    roles, changed, used = [], False, set()
    for role in result["opportunities"]:
        key = _key_or_none(role_key, role)
        # S5: the saved copy of the same job replaces an invalid final copy; V1: also an unsourced
        # final copy when the saved copy is sourced (proven open).
        if key in by_key and (not valid(role) or not sourced(role) and sourced(by_key[key])):
            role, changed = by_key[key], True
        if key in by_key:
            used.add(key)
        roles.append(role)
    # Covered by the store path's own rule (review T1): a sourced role or a verified finding.
    covered = research_workflow.covered_companies(dict(result, opportunities=roles), now=now, policy=policy)
    extra = [role for key, role in by_key.items() if key not in used and _company_name_key(role) not in covered]
    # Review U2: at a company where only a saved role is sourced, the final output's unsourced roles
    # there are dropped, so one job never stands twice (an unproven final copy beside a proven saved one).
    proven = {_company_name_key(role) for role in extra if sourced(role)}
    if proven:
        kept = [role for role in roles if _key_or_none(_company_name_key, role) not in proven or sourced(role)]
        changed = changed or len(kept) != len(roles)
        roles = kept
    if not extra and not changed:
        return result
    merged = copy.deepcopy(result)
    merged["opportunities"] = roles + extra
    return merged


def cut_off_note(error, saved, phase, answered=False):
    """The plain note for a session that ended without a usable final answer (user-facing; no codes)."""
    import subprocess
    import provider_limits
    reason = ("the AI service asked it to wait" if provider_limits.wait_of(error)
              else "it reached its time limit" if isinstance(error, (subprocess.TimeoutExpired, TimeoutError))
              else "it was stopped" if isinstance(error, InterruptedError)
              else "it ended without a usable final answer" if answered   # review T2
              else "it ended before giving its final answer")
    count = len(saved)
    noun = ("company" if count == 1 else "companies") if phase == "discovery" else ("role" if count == 1 else "roles")
    return "A research session ended early (" + reason + "); kept the " + str(count) + " " + noun + " it saved."


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
