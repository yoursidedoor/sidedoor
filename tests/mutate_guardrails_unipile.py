"""Mutation check for the E3.T7 guardrail suite (tests/test_guardrails_unipile.py). Not a unittest
module: the name does not match discover's default pattern (test*.py), so the suite never runs it.

Run from the repository root:  python3.13 tests/mutate_guardrails_unipile.py

Each mutant disables one guard in the production code. It is applied to a temporary copy of the
repository's top-level files and tests/ (the working tree is never modified), and only the guardrail
suite is run there (`python3.13 -B -m unittest discover -s tests -p test_guardrails_unipile.py`,
no bytecode read or written). A mutant is KILLED when tests ran and at least one failed; an import
error is BROKEN, never killed. The unmutated copy must pass first. Offline: the suite itself makes
no network call. Exit status 0 only when every mutant is KILLED.

A SURVIVED mutant is not necessarily a gap: another guard may refuse the same send (defence in
depth). Those are listed in COVERED_ELSEWHERE with the guard that still refuses, printed as
"SURVIVED (covered elsewhere: ...)" and accepted; any other survivor fails the run. The output
names the scenarios that caught each killed mutant.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUITE = "test_guardrails_unipile.py"

MUTANTS = [
    # (guardrail, name, file, original text (must occur exactly once), replacement), or
    # (guardrail, name, [(file, original, replacement), ...]) for a guard spread over several edits.
    # --- claim-before-send ---
    ("claim-before-send", "gates not re-run inside the claim transaction", "outreach.py",
     '                gates = action_gates(database, config, action, proof, now)\n'
     '                if current[1] != row["payload"]',
     '                gates = []\n'
     '                if current[1] != row["payload"]'),
    ("claim-before-send", "payload change after inspection not refused at claim", "outreach.py",
     '                    gates.append("ACTION_PAYLOAD_CHANGED_AFTER_INSPECTION")', '                    pass'),
    ("claim-before-send", "gates not re-run after the claim, before the transport", "outreach.py",
     "                gates = action_gates(database, config, action, proof, engagement.timestamp(now or dt.datetime.now(dt.timezone.utc)))",
     "                gates = []"),
    ("claim-before-send", "an existing claim on the person does not refuse", "outreach.py",
     '    gates.extend(_immutable_claim_gates(database, identity, action["action_id"], continuation=continuation, reply=reply))\n', ""),
    # --- never-resend ---
    ("never-resend", "an undecidable absence read counts as absent", [
        ("sequence.py", "    if absent is not True:\n        _save(database, action_id, state, result, state)",
         "    if absent is False:\n        _save(database, action_id, state, result, state)"),
        ("linkedin_port.py", "    return absent is True and attempts_made < INVITATION_MAX_ATTEMPTS",
         "    return absent is not False and attempts_made < INVITATION_MAX_ATTEMPTS")]),
    ("never-resend", "invitation attempts 3 -> 4", "linkedin_port.py",
     "INVITATION_MAX_ATTEMPTS = 3", "INVITATION_MAX_ATTEMPTS = 4"),
    ("never-resend", "a crashed request is recovered before the stuck window", "sequence.py",
     "or now - started < STUCK_AFTER", "or False"),
    ("never-resend", "the port check before the first invitation ignored", "sequence.py",
     '    if gates:\n        result["provider_guard"] = {"checked_at": _now(),',
     '    if False:\n        result["provider_guard"] = {"checked_at": _now(),'),
    ("never-resend", "the port check before a retry ignored", "sequence.py",
     '        if gates:\n            result["provider_guard"] = {"checked_at": observed,',
     '        if False:\n            result["provider_guard"] = {"checked_at": observed,'),
    ("never-resend", "an unresolved action of the person does not refuse", "outreach.py",
     '        gates.append("UNRESOLVED_ACTION_NO_RETRY")', "        pass"),
    ("never-resend", "the follow-up slot of an earlier attempt is ignored", "followup_execution.py",
     "            if slot:\n                item.update(action_id=slot[0], state=slot[1])",
     "            if False:\n                item.update(action_id=slot[0], state=slot[1])"),
    ("exclusions", "follow-up final checks: action_gates not re-run before the write", "followup_execution.py",
     "            gates.extend(outreach.action_gates(self.database, config, action, proof, now))\n", ""),
    ("exclusions", "follow-up final checks: stop_state not checked before the write", "followup_execution.py",
     "            if stopped:\n                gates.append(stopped)", "            if False:\n                gates.append(stopped)"),
    ("exclusions", "follow-up final checks: suppressions not checked before the write", "followup_execution.py",
     '                        gates.append("SUPPRESSED_CONTACT")', "                        pass"),
    ("exclusions", "follow-up final checks: the thread read's binding/due check dropped", "followup_execution.py",
     "    gates = binding_gates(database, config, action, now)\n    try:\n        if isinstance(room, dict)",
     "    gates = []\n    try:\n        if isinstance(room, dict)"),
    ("exclusions", "follow-up final checks: binding/due check, action_gates, stop_state and suppressions all dropped", [
        ("followup_execution.py", "    gates = binding_gates(database, config, action, now)\n    try:\n        if isinstance(room, dict)",
         "    gates = []\n    try:\n        if isinstance(room, dict)"),
        ("followup_execution.py", "            gates.extend(outreach.action_gates(self.database, config, action, proof, now))\n", ""),
        ("followup_execution.py", "            if stopped:\n                gates.append(stopped)", "            if False:\n                gates.append(stopped)"),
        ("followup_execution.py", '                        gates.append("SUPPRESSED_CONTACT")', "                        pass")]),
    ("never-resend", "the follow-up's final thread read before the write ignored", "followup_execution.py",
     '            check["gates"] = thread_gates(self.database, config, action, observation, outreach_provider._now())',
     '            check["gates"] = []'),
    # --- contact cap ---
    ("contact cap", "the per-run slot count is not enforced", "research_workflow.py",
     "        if changed.rowcount != 1:\n            return {\"state\": \"BLOCKED\", \"reason\": \"CONTACT_BUDGET_OR_UNSEEDED_HISTORY\"}",
     "        if False:\n            return {\"state\": \"BLOCKED\", \"reason\": \"CONTACT_BUDGET_OR_UNSEEDED_HISTORY\"}"),
    # --- exclusions ---
    ("exclusions", "owner-excluded names not refused at send", "outreach.py",
     '        gates.append("EXCLUDED_CONTACT")', "        pass"),
    ("exclusions", "no-automatic-outreach contacts not refused at send", "outreach.py",
     '        gates.append("NO_AUTOMATIC_OUTREACH_CONTACT")', "        pass"),
    ("exclusions", "suppressions not refused at send", "outreach.py",
     '                gates.append("SUPPRESSED_CONTACT")', "                pass"),
    ("exclusions", "engagement stops (reply, opt-out, closure) not refused at send", "outreach.py",
     "        gates.append(engagement.stop_label(database, identity))", "        pass"),
    ("exclusions", "a closed recipient's pending action not closed by the cycle", "outreach.py",
     'if row["state"] in {"PENDING", "BLOCKED"} and engagement.closure_status(database, action["profile"]):',
     'if row["state"] in {"PENDING", "BLOCKED"} and False:'),
    ("exclusions", "owner-excluded names reserved for a draft", "research_workflow.py",
     "        excluded = name in owner_seed.excluded_names() or not name", "        excluded = not name"),
    # --- already contacted ---
    ("already contacted", "local contact records not checked before a draft", "research_workflow.py",
     "        keys = record_keys(database, identity)\n", "        return None\n"),
    ("already contacted", "our own recorded initial_sent does not refuse at send", "outreach.py",
     '        if initials or proof.get("prior_outreach") is True:', "        if False:"),
    # --- INITIAL (existing chat) and FIRST_MESSAGE over the port (outreach_agent) ---
    ("exclusions", "port message final checks: closure/inactivity send_gates dropped", "outreach_agent.py",
     '            gates = list(inactivity_closure.send_gates(self.database, config, action["profile"], now))',
     "            gates = []"),
    ("exclusions", "port message final checks: engagement stops not checked", "outreach_agent.py",
     "            if stopped:\n                gates.append(stopped)", "            if False:\n                gates.append(stopped)"),
    ("exclusions", "port message final checks: suppressions not checked", "outreach_agent.py",
     '                        gates.append("SUPPRESSED_CONTACT")', "                        pass"),
    ("exclusions", "port message final checks: closure, stops and suppressions all dropped", [
        ("outreach_agent.py", '            gates = list(inactivity_closure.send_gates(self.database, config, action["profile"], now))',
         "            gates = []"),
        ("outreach_agent.py", "            if stopped:\n                gates.append(stopped)",
         "            if False:\n                gates.append(stopped)"),
        ("outreach_agent.py", '                        gates.append("SUPPRESSED_CONTACT")', "                        pass")]),
    ("already contacted", "a reply the port read shows in the chat is not a reply", "outreach_agent.py",
     '        if any(message.get("sender") == "CORRESPONDENT" for message in messages):\n            findings["has_reply"] = True',
     '        if False:\n            findings["has_reply"] = True'),
    ("already contacted", "a message of ours the port read shows in the chat is not prior outreach", "outreach_agent.py",
     '        if any(message.get("sender") == "ME" for message in messages):\n            findings["prior_outreach"] = True',
     '        if False:\n            findings["prior_outreach"] = True'),
    # --- REPLY over the port (reply_execution) ---
    ("exclusions", "reply port send: stop_gates not re-run after REQUEST_STARTED", "reply_execution.py",
     '            gates = stop_gates(self.database, config, action, proof if isinstance(proof, dict) else {})\n'
     '            if gates:\n                result["provider_guard"] = {"checked_at": checked_at, "gates": sorted(set(gates))}',
     '            gates = []\n'
     '            if gates:\n                result["provider_guard"] = {"checked_at": checked_at, "gates": sorted(set(gates))}'),
    ("claim-before-send", "reply: stop_gates not re-run after the claim, before REQUEST_STARTED", "outreach.py",
     "                gates = reply_execution.stop_gates(database, config, action, proof)", "                gates = []"),
    ("claim-before-send", "reply: stop_gates dropped after the claim and in the port send", [
        ("outreach.py", "                gates = reply_execution.stop_gates(database, config, action, proof)", "                gates = []"),
        ("reply_execution.py",
         '            gates = stop_gates(self.database, config, action, proof if isinstance(proof, dict) else {})\n'
         '            if gates:\n                result["provider_guard"] = {"checked_at": checked_at, "gates": sorted(set(gates))}',
         '            gates = []\n'
         '            if gates:\n                result["provider_guard"] = {"checked_at": checked_at, "gates": sorted(set(gates))}')]),
    ("exclusions", "reply stop_gates: excluded names not checked", "reply_execution.py",
     '            gates.append("EXCLUDED_CONTACT")', "            pass"),
    ("exclusions", "reply stop_gates: suppressions not checked", "reply_execution.py",
     '                gates.append("SUPPRESSED_CONTACT")', "                pass"),
    ("exclusions", "reply stop_gates: engagement stops (closure, opt-out) not checked", "reply_execution.py",
     '        if stop and stop != "REPLY_REQUIRES_CONTEXT":\n            gates.append(stop)',
     '        if False:\n            gates.append(stop)'),
    ("exclusions", "reply stop_gates: opt-out events not checked", "reply_execution.py",
     '            gates.append("OPTED_OUT")', "            pass"),
    ("never-resend", "reply retry: the chat read before a resend ignored", "reply_execution.py",
     "            outcome, message = _our_reply(room, action, inbound_text(database, action))",
     "            outcome, message = None, None"),
    ("never-resend", "reply send attempts 3 -> 4", "reply_execution.py",
     "MAX_SEND_ATTEMPTS = 3", "MAX_SEND_ATTEMPTS = 4"),
    ("never-resend", "reply retry: an unreadable chat counts as an empty one", "reply_execution.py",
     '                room = _unipile_room(self.port.messages(action["conversation_id"]))',
     '                try:\n'
     '                    room = _unipile_room(self.port.messages(action["conversation_id"]))\n'
     '                except Exception:\n'
     '                    room = {"id": action["conversation_id"], "messages": [], "totalMessages": 0}'),
    ("never-resend", "reply retry: another message of ours after the inbound does not end the retries", "reply_execution.py",
     '        other = other or ("OTHER_ANSWER", message)', "        pass"),
    ("never-resend", "reply retry: a newer inbound (chat or record) does not stand the reply down", [
        ("reply_execution.py", '                newer = newer or ("NEWER_INBOUND", message)', "                pass"),
        ("reply_execution.py", '                outcome, message = "NEWER_INBOUND", None', "                pass")]),
]

# Mutants whose guard is backed by another one the suite already exercises.
COVERED_ELSEWHERE = {
    "payload change after inspection not refused at claim":
        "the outreach_actions immutability trigger refuses the payload replacement before the claim",
    "the follow-up slot of an earlier attempt is ignored":
        "the follow-up task id is deterministic (action_id = 'followup:' + digest(sender, alias, ordinal)), so "
        "workflow_tasks.enqueue refuses rebinding the task to changed inputs, else the task's COMPLETE status "
        "stops the item before a second action",
    "a closed recipient's pending action not closed by the cycle":
        "close_outreach itself moves the person's PENDING/BLOCKED actions to CLOSED in the same transaction "
        "(the only writer of engagement_closures), so no scheduled path leaves a closed person's action "
        "pending for this step; a closure after the claim is refused by the send-time gates (OUTREACH_CLOSED)",
    "follow-up final checks: action_gates not re-run before the write":
        "the final thread read's binding check (thread_gates -> binding_gates -> plan: due/stops, excluded "
        "names, suppressions) refuses the same changes",
    "follow-up final checks: the thread read's binding/due check dropped":
        "the action_gates, stop_state and suppression checks in the same final check refuse the same changes",
    "follow-up final checks: stop_state not checked before the write":
        "the action_gates re-run in the same final check refuses the stop (stop_label)",
    "follow-up final checks: suppressions not checked before the write":
        "the action_gates re-run in the same final check refuses the suppression (SUPPRESSED_CONTACT)",
    "port message final checks: closure/inactivity send_gates dropped":
        "engagement.stop_state in the same final check returns OUTREACH_CLOSED for a closed person; send_gates' "
        "other hold (a reached 27-day inactivity due date) needs a 27-day-old outreach or inbound anchor, which "
        "the earlier gates already refuse for a new contact's INITIAL/FIRST_MESSAGE (no scenario reaches it)",
    "reply stop_gates: opt-out events not checked":
        "an opt_out event or an inbound with a negative/opt_out inference also writes engagement_stops OPTED_OUT "
        "(kept on conflict), which the stop_state check in the same stop_gates refuses",
}


def run(directory):
    for cache in Path(directory).rglob("__pycache__"):
        shutil.rmtree(cache)
    result = subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-p", SUITE],
                            cwd=directory, capture_output=True, text=True,
                            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    ran = re.search(r"^Ran (\d+) tests?", result.stderr, re.MULTILINE)
    broken = ran is None or int(ran.group(1)) == 0 or "ImportError" in result.stderr or "_FailedTest" in result.stderr
    summary = " ".join(line for line in result.stderr.splitlines() if line.startswith(("Ran", "OK", "FAILED")))
    failed = sorted(set(re.findall(r"^(?:FAIL|ERROR): (test_\w+) \(\S+?\.(\w+)\.test_", result.stderr, re.MULTILINE)))
    return result.returncode, broken, summary, failed


def main():
    with tempfile.TemporaryDirectory() as temporary:
        copy = Path(temporary)
        for path in ROOT.iterdir():
            if path.is_file():
                shutil.copy2(path, copy / path.name)
        shutil.copytree(ROOT / "tests", copy / "tests", ignore=shutil.ignore_patterns("__pycache__"))
        code, broken, summary, unused = run(copy)
        if code != 0 or broken:
            print("BASELINE FAILS:", summary)
            return 2
        print("baseline passes:", summary)
        survivors, accepted = 0, 0
        for index, mutant in enumerate(MUTANTS, 1):
            guard, name = mutant[:2]
            edits = mutant[2] if len(mutant) == 3 else [mutant[2:]]
            files = sorted({edit[0] for edit in edits})
            filename = ", ".join(files)
            sources = {file: (ROOT / file).read_text() for file in files}
            mutated = dict(sources)
            stale = [file for file, original, unused in edits if sources[file].count(original) != 1]
            if stale:
                print(f"{index:2} STALE    [{guard}] {name} ({', '.join(stale)}: original text not found exactly once)")
                survivors += 1
                continue
            for file, original, replacement in edits:
                mutated[file] = mutated[file].replace(original, replacement)
            for file in files:
                (copy / file).write_text(mutated[file])
            try:
                code, broken, summary, failed = run(copy)
            finally:
                for file in files:
                    (copy / file).write_text(sources[file])
            verdict = "BROKEN  " if broken else "KILLED  " if code else "SURVIVED"
            caught = ", ".join(cls + "." + test for test, cls in failed[:4]) + (" ..." if len(failed) > 4 else "")
            note = f" | caught by {caught}" if caught else ""
            if verdict == "SURVIVED" and name in COVERED_ELSEWHERE:
                accepted += 1
                note = f" | covered elsewhere: {COVERED_ELSEWHERE[name]}"
            elif verdict != "KILLED  ":
                survivors += 1
            print(f"{index:2} {verdict} [{guard}] {name} ({filename}) | {summary}{note}")
    killed = len(MUTANTS) - survivors - accepted
    print(f"{killed}/{len(MUTANTS)} killed, {accepted} survived but covered elsewhere, {survivors} unexplained")
    return 0 if survivors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
