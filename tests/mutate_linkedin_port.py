"""Mutation check for linkedin_port's guarded branches (E3.T1 review). Not a unittest module: the
name does not match discover's default pattern (test*.py), so the suite never runs it.

Run from the repository root:  python3.13 tests/mutate_linkedin_port.py

Each mutant is applied to a copy of linkedin_port.py inside a temporary copy of the repository's
top-level files and tests/, and the port tests are run there with
`python3.13 -B -m unittest discover -s tests -p test_linkedin_port.py` with
PYTHONDONTWRITEBYTECODE=1, after deleting every __pycache__ in the copy (no stale bytecode can
stand in for a mutant). The working tree is never modified. A mutant counts as KILLED only when tests ran (a "Ran N tests" line, N > 0) and
failed; an import error is reported as BROKEN, never as killed. The unmutated copy must pass
first. Exit status is 0 only when every mutant is KILLED.

`--property` instead runs only the property sweep (InvitationAbsentProperty) against the
PROPERTY_MUTANTS: mutants that can make invitation_absent wrongly return True within one
provider-id namespace. Each must be killed by the sweep alone. Mutants that are unsafe only when
a caller mixes id namespaces (r08, r23) or that only raise more often are pinned by targeted
tests, not by the sweep.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

MUTANTS = [
    # (name, original text, replacement)
    ("all_items returns after the first page",
     "        items.extend(page.items)\n        if page.next_cursor is None:\n            return items",
     "        items.extend(page.items)\n        if True:\n            return items"),
    ("all_items completeness check dropped",
     "        if not page.complete:\n            raise IncompletePage()", "        if False:\n            raise IncompletePage()"),
    ("PAGE_LIMIT returns items", '    raise IncompletePage("PAGE_LIMIT")', "    return items"),
    ("may_retry uses bool(absent)", "return absent is True and attempts_made", "return bool(absent) and attempts_made"),
    ("pending type check reduced to `is None`",
     "if (type(person.pending_invitation) is not bool or", "if (person.pending_invitation is None or"),
    ("distance type check dropped",
     "type(person.network_distance) is not int\n            or person.network_distance < 1)",
     "False\n            or False)"),
    ("distance 0 or negative accepted (n16)",
     "            or person.network_distance < 1):", "            or False):"),
    ("non-profile caller URL returns True (n17)",
     '        raise ValueError("A LinkedIn /in/ profile URL is required")', "        return True"),
    ("slug NFKC dropped", 'return key, unicodedata.normalize("NFKC", slug)', "return key, slug"),
    ("member id = casefold startswith acoa",
     "    return isinstance(value, str) and MEMBER_ID.fullmatch(value) is not None",
     '    return isinstance(value, str) and value.casefold().startswith("acoa")'),
    ("member id = ACoAA prefix only",
     "    return isinstance(value, str) and MEMBER_ID.fullmatch(value) is not None",
     '    return isinstance(value, str) and value.startswith("ACoAA")'),
    ("member-like without casefold (n04)",
     'value.casefold().startswith(("acoa", "acwa"))', 'value.startswith(("acoa", "acwa"))'),
    ("ACwAA not member-like", 'value.casefold().startswith(("acoa", "acwa"))', 'value.casefold().startswith("acoa")'),
    ("case-only check on all values dropped",
     "        if value is not None and value.casefold() in loose:\n            return None",
     "        if False:\n            return None"),
    ("case-only check only on entry id (n09)", "    for value in (entry_id, slug):", "    for value in (entry_id,):"),
    ("member-like caller slug left out of loose (r23)",
     " | ({slug.casefold()} if _member_like(slug) else set())", ""),
    ("member-like vanity can be someone else",
     "if key is not None and not _member_like(slug) and not _member_id(slug) and vanity is not None:",
     "if key is not None and not _member_id(slug) and vanity is not None:"),
    ("member-id entry can be someone else by vanity",
     "if key is not None and not _member_like(slug) and not _member_id(slug) and vanity is not None:",
     "if key is not None and not _member_like(slug) and vanity is not None:"),
    ("member-like caller slug used as vanity",
     "    elif not _member_like(slug):\n        vanity = key", "    else:\n        vanity = key"),
    ("member_ids -> ids (r08)", "    if _member_id(slug) and member_ids:", "    if _member_id(slug) and ids:"),
    ("`and ids` dropped from the different-id rule",
     "    if entry_id is not None and ids:\n        return False", "    if entry_id is not None:\n        return False"),
    ("lookup id dropped", "_provider_id(provider_id), _provider_id(person.provider_id))", "_provider_id(provider_id),)"),
    ("caller id dropped", "(_provider_id(provider_id), _provider_id(person.provider_id))", "(_provider_id(person.provider_id),)"),
    ("pending decision dropped", "if person.pending_invitation or person.network_distance == 1:",
     "if person.network_distance == 1:"),
    ("connected decision dropped", "if person.pending_invitation or person.network_distance == 1:",
     "if person.pending_invitation:"),
    ("id match dropped", "    if entry_id is not None and entry_id in ids:\n        return True", "    if False:\n        return True"),
    ("member-slug match dropped", "    if _member_id(slug) and slug in ids:\n        return True", "    if False:\n        return True"),
    ("vanity match dropped", "    if key is not None and vanity is not None and key == vanity:\n        return True",
     "    if False:\n        return True"),
    ("member slug never someone else", "    if _member_id(slug) and member_ids:\n        return False",
     "    if False:\n        return False"),
    ("vanity never someone else",
     "if key is not None and not _member_like(slug) and not _member_id(slug) and vanity is not None:\n        return False",
     "if False:\n        return False"),
    ("undecidable counts as absent",
     'if same is None:\n            raise IncompletePage("INVITATION_INVITEE_UNIDENTIFIED")', "if same is None:\n            continue"),
    ("caller member slug not added to ids", "    if _member_id(slug):\n        ids.add(slug)", "    if _member_id(slug):\n        pass"),
    ("any non-None id usable", "return value.strip() if isinstance(value, str) and value.strip() else None", "return value"),
    ("FakePort send_invitation accepts unknown",
     "        def effect():\n            self._known(provider_id)\n            if any(", "        def effect():\n            if any("),
    ("FakePort start_chat accepts unknown",
     "            self._known(provider_id)\n            existing", "            existing"),
    # Round 8.
    ("provider id not stripped", "return value.strip() if isinstance(value, str) and value.strip() else None",
     "return value if isinstance(value, str) and value.strip() else None"),
    ("MEMBER_ID without _ and -", 'MEMBER_ID = re.compile(r"ACoAA[A-Za-z0-9_-]{34}")', 'MEMBER_ID = re.compile(r"ACoAA[A-Za-z0-9]{34}")'),
    ("all_items one page short of the limit", "    for unused in range(limit_pages):", "    for unused in range(limit_pages - 1):"),
    ("all_items one page over the limit", "    for unused in range(limit_pages):", "    for unused in range(limit_pages + 1):"),
    ("FakePort relations lists everyone", '                   for person in self.people.values() if person["distance"] == 1]',
     "                   for person in self.people.values()]"),
    ("FakePort read timeout is UncertainWrite", 'raise UncertainWrite("TIMEOUT") if write else TimeoutError("read timed out")',
     'raise UncertainWrite("TIMEOUT")'),
    ("FakePort allows a second pending invitation",
     '            if any(self._invites(item, provider_id) for item in self.invitations):\n                raise InvitationExists("ALREADY_INVITED", "INVITATION_ALREADY_PENDING")',
     '            if False:\n                raise InvitationExists("ALREADY_INVITED", "INVITATION_ALREADY_PENDING")'),
    ("FakePort messages ignores incomplete_page",
     '        kind = self._enter("messages")\n        if kind == "incomplete_page":', '        kind = self._enter("messages")\n        if False:'),
    ("FakePort drops the invitation note", 'item["note"], item["sent_at"], dict(item)) for item in self.invitations]',
     'None, item["sent_at"], dict(item)) for item in self.invitations]'),
    ("FakePort is_sender not coerced", '{"is_sender": bool(sender), "text": text', '{"is_sender": sender, "text": text'),
    ("FakePort start_chat always creates a chat", "            if existing:", "            if False:"),
    ("HeyReach chats: short page complete", "and (bool(items) or offset >= total)\n", "\n"),
    ("HeyReach chats: over total complete", "                        and offset + len(items) <= total and", "                        and"),
    ("HeyReach chats: over limit complete", "len(items) == len(raw[\"items\"]) and len(items) <= limit", "len(items) == len(raw[\"items\"])"),
    ("HeyReach chats: non-dict items counted", "complete = (len(items) == len(raw[\"items\"]) and", "complete = ("),
    ("HeyReach chats: totalCount type unchecked", 'type(raw.get("totalCount")) is int and isinstance(raw.get("items"), list)',
     'raw.get("totalCount") is not None and isinstance(raw.get("items"), list)'),
    ("HeyReach chats: cursor without completeness", "next_cursor=str(end) if complete and end < total else None",
     "next_cursor=str(end) if total is not None and end < total else None"),
    ("HeyReach chats: cursor even at the end", "next_cursor=str(end) if complete and end < total else None",
     "next_cursor=str(end) if complete else None"),
    ("HeyReach messages: totalMessages type unchecked", 'complete = (type(raw.get("totalMessages")) is int and len(found)',
     'complete = (len(found)'),
    ("HeyReach messages: count not compared", 'and raw["totalMessages"] == len(found))', ")"),
    ("HeyReach messages: non-dict messages counted", 'type(raw.get("totalMessages")) is int and len(found) == len(raw["messages"])',
     'type(raw.get("totalMessages")) is int'),
    ("HeyReach ME/CORRESPONDENT swapped", '{"ME": True, "CORRESPONDENT": False}', '{"ME": False, "CORRESPONDENT": True}'),
    ("HeyReach unknown sender read as ours", '{"ME": True, "CORRESPONDENT": False}.get(sender)', '{"ME": True, "CORRESPONDENT": False}.get(sender, True)'),
    ("HeyReach account.valid not type-checked", 'shaped and isinstance(raw.get("authIsValid"), bool)', "shaped"),
    ("HeyReach lookup id not type-checked", "provider_id=provider_id if isinstance(provider_id, str) else None", "provider_id=provider_id"),
    ("HeyReach send_message key order", '        request = {"conversationId": chat_id, "linkedInAccountId": self.sender_id,\n                   "message": text, "subject": subject}',
     '        request = {"linkedInAccountId": self.sender_id, "conversationId": chat_id,\n                   "message": text, "subject": subject}'),
    ("HeyReach send_message subject default", 'def send_message(self, chat_id, text, *, subject=""):', "def send_message(self, chat_id, text, *, subject=None):"),
    # Round 9 (the reviewer's N-numbers in brackets).
    ("ids = caller id OR lookup id [N30]",
     "ids = {value for value in (_provider_id(provider_id), _provider_id(person.provider_id)) if value}",
     "ids = {_provider_id(provider_id) or _provider_id(person.provider_id)} - {None}"),
    ("case-free set built before the caller member slug is added [N04]",
     "    if _member_id(slug):\n        ids.add(slug)\n    elif not _member_like(slug):\n        vanity = key\n    loose = {value.casefold() for value in ids} | ({slug.casefold()} if _member_like(slug) else set())",
     "    loose = {value.casefold() for value in ids} | ({slug.casefold()} if _member_like(slug) else set())\n    if _member_id(slug):\n        ids.add(slug)\n    elif not _member_like(slug):\n        vanity = key"),
    ("MEMBER_ID {20,34} [N05]", 'MEMBER_ID = re.compile(r"ACoAA[A-Za-z0-9_-]{34}")', 'MEMBER_ID = re.compile(r"ACoAA[A-Za-z0-9_-]{20,34}")'),
    ("MEMBER_ID {34,35}", 'MEMBER_ID = re.compile(r"ACoAA[A-Za-z0-9_-]{34}")', 'MEMBER_ID = re.compile(r"ACoAA[A-Za-z0-9_-]{34,35}")'),
    ("MEMBER_ID match not fullmatch [N06]", "MEMBER_ID.fullmatch(value) is not None", "MEMBER_ID.match(value) is not None"),
    ("slug not unquoted [N07]", '    slug = urllib.parse.unquote(path).strip("/").split("/")[1]', '    slug = path.strip("/").split("/")[1]'),
    ("same-is-True ignored [N01]", "        if same is True:\n            return False", "        if same is True:\n            continue"),
    ("every caller slug into ids [N02]", "    if _member_id(slug):\n        ids.add(slug)", "    ids.add(slug)\n    if _member_id(slug):\n        pass"),
    ("vanity = raw slug [N03]", "    elif not _member_like(slug):\n        vanity = key", "    elif not _member_like(slug):\n        vanity = slug"),
    ("all_items cursor not advanced [N08]", "        cursor = page.next_cursor\n", "        cursor = cursor\n"),
    ("distance isinstance int [N09]", "type(person.network_distance) is not int", "not isinstance(person.network_distance, int)"),
    ("pending truthy [N10]", "if (type(person.pending_invitation) is not bool or", "if (person.pending_invitation not in (True, False) or"),
    ("may_retry <= [N11]", "attempts_made < INVITATION_MAX_ATTEMPTS", "attempts_made <= INVITATION_MAX_ATTEMPTS"),
    ("FakePort lookup pending ignores invitations [N12]",
     '        pending = any(self._invites(item, person["provider_id"]) for item in self.invitations)', "        pending = False"),
    ("FakePort invitee by id only (profile path dropped)",
     '        try:\n            invitee = _profile_key(item["invitee_profile"])\n        except (TypeError, ValueError):\n            return False',
     "        if True:\n            return False"),
    ("FakePort lookup pending by id only",
     '        pending = any(self._invites(item, person["provider_id"]) for item in self.invitations)',
     '        pending = any(item["invitee_provider_id"] == person["provider_id"] for item in self.invitations)'),
    ("FakePort duplicate check by id only",
     "            if any(self._invites(item, provider_id) for item in self.invitations):",
     '            if any(item["invitee_provider_id"] == provider_id for item in self.invitations):'),
    ("FakePort accept removes by id only",
     "        self.invitations = [item for item in self.invitations if not self._invites(item, provider_id)]",
     '        self.invitations = [item for item in self.invitations if item["invitee_provider_id"] != provider_id]'),
    ("FakePort accept keeps invitations [N17]",
     "        self.invitations = [item for item in self.invitations if not self._invites(item, provider_id)]\n", ""),
    ("FakePort chats filter dropped [N13]", 'for chat_id, chat in ordered if person is None or chat["attendee"] == wanted]', "for chat_id, chat in ordered]"),
    ("FakePort session check allows lookup [N14]", 'if not self.session_valid and method != "account":',
     'if not self.session_valid and method not in ("account", "lookup"):'),
    ("FakePort partial page marked complete [N15]",
     "return Page(items=(), next_cursor=None, total=len(records), complete=False, raw=None)",
     "return Page(items=(), next_cursor=None, total=len(records), complete=True, raw=None)"),
    ("FakePort timeout_after_write raised before the write [N16]",
     '        value = effect()\n        if kind == "timeout_after_write":\n            raise UncertainWrite("TIMEOUT_AFTER_WRITE")',
     '        if kind == "timeout_after_write":\n            raise UncertainWrite("TIMEOUT_AFTER_WRITE")\n        value = effect()'),
    ("FakePort send_message no activity bump [N18]",
     '            chat["messages"].append({"is_sender": True, "text": text, "sent_at": _now()})\n            chat["activity"] = next(self._activity)\n            return {"chat_id": chat_id}',
     '            chat["messages"].append({"is_sender": True, "text": text, "sent_at": _now()})\n            return {"chat_id": chat_id}'),
    ("FakePort start_chat picks the oldest chat [N26]",
     '                latest = max(existing, key=lambda chat_id: self.chat_log[chat_id]["activity"])',
     "                latest = existing[0]"),
    ("FakePort start_chat picks the newest-created chat",
     '                latest = max(existing, key=lambda chat_id: self.chat_log[chat_id]["activity"])',
     "                latest = existing[-1]"),
    ("HeyReach get_chatroom key order [N19]",
     '"get_chatroom", {"accountId": self.sender_id, "conversationId": chat_id}',
     '"get_chatroom", {"conversationId": chat_id, "accountId": self.sender_id}'),
    ("HeyReach chats filtered order like inbox [N20]", "        if person is None and lead_id is None:", "        if True:"),
    ("HeyReach chats non-dict items shaped [N21]", 'for item in raw["items"] if isinstance(item, dict))', 'for item in raw["items"])'),
    ("_text passthrough [N22]", "    return value if isinstance(value, str) else None", "    return value"),
    ("HeyReach messages non-dict shaped [N23]", 'for item in raw["messages"] if isinstance(item, dict))', 'for item in raw["messages"])'),
    ("HeyReach cursor ignored [N24]", "        offset = int(cursor or 0)", "        offset = 0"),
    ("case-only loop after someone-else [N25]",
     "    for value in (entry_id, slug):\n        if value is not None and value.casefold() in loose:\n            return None\n    if entry_id is not None and ids:\n        return False",
     "    if entry_id is not None and ids:\n        return False\n    for value in (entry_id, slug):\n        if value is not None and value.casefold() in loose:\n            return None"),
    ("slug lowercased [N28]", '    return key, unicodedata.normalize("NFKC", slug)', '    return key, unicodedata.normalize("NFKC", slug).lower()'),
    ("HeyReach account valid from truthy [N29]",
     'valid = raw.get("authIsValid") if shaped and isinstance(raw.get("authIsValid"), bool) else None',
     'valid = bool(raw.get("authIsValid")) if shaped else None'),
    ("HeyReach person-filtered empty page complete", "\n                        and (bool(items) or person is None))", ")"),
    # Round 10: every type check on a neutral field, loosened or removed.
    ("[T01] totalCount isinstance int (bool passes)", 'type(raw.get("totalCount")) is int', 'isinstance(raw.get("totalCount"), int)'),
    ("[T02] totalMessages isinstance int (bool passes)", 'type(raw.get("totalMessages")) is int', 'isinstance(raw.get("totalMessages"), int)'),
    ("[T03] own_profile not _text", 'own_profile=_text(raw.get("profileUrl")) if shaped else None', 'own_profile=raw.get("profileUrl") if shaped else None'),
    ("[T04] account_id not type-checked", 'raw.get("id") if shaped and type(raw.get("id")) in (int, str) else None', 'raw.get("id") if shaped else None'),
    ("[T05] account_id isinstance (bool passes)", 'type(raw.get("id")) in (int, str)', 'isinstance(raw.get("id"), (int, str))'),
    ("[T06] authIsValid isinstance int (1 passes)", 'isinstance(raw.get("authIsValid"), bool)', 'isinstance(raw.get("authIsValid"), int)'),
    ("[T07] account answer not checked to be a dict", "        shaped = isinstance(raw, dict)", "        shaped = raw is not None"),
    ("[T08] lookup answer not checked to be a dict", 'provider_id = raw.get("linkedin_id") if isinstance(raw, dict) else None',
     'provider_id = raw.get("linkedin_id") if raw is not None else None'),
    ("[T09] chats answer not checked to be a dict", 'if isinstance(raw, dict) and type(raw.get("totalCount")) is int', 'if raw is not None and type(raw.get("totalCount")) is int'),
    ("[T10] chats items not checked to be a list", 'and isinstance(raw.get("items"), list):', 'and raw.get("items") is not None:'),
    ("[T11] messages answer not checked to be a dict", 'if isinstance(raw, dict) and isinstance(raw.get("messages"), list):',
     'if raw is not None and isinstance(raw.get("messages"), list):'),
    ("[T12] messages not checked to be a list", 'if isinstance(raw, dict) and isinstance(raw.get("messages"), list):',
     'if isinstance(raw, dict) and raw.get("messages") is not None:'),
    ("[T13] chat id not _text", '    return Chat(_text(item.get("id")), _text(correspondent.get("linkedin_id")),',
     '    return Chat(item.get("id"), _text(correspondent.get("linkedin_id")),'),
    ("[T14] attendee id not _text", '    return Chat(_text(item.get("id")), _text(correspondent.get("linkedin_id")),',
     '    return Chat(_text(item.get("id")), correspondent.get("linkedin_id"),'),
    ("[T15] attendee profile not _text", '                _text(correspondent.get("profileUrl")), item)', '                correspondent.get("profileUrl"), item)'),
    ("[T16] correspondentProfile not checked to be a dict", "    if not isinstance(correspondent, dict):\n        correspondent = {}",
     "    if correspondent is None:\n        correspondent = {}"),
    ("[T17] message body not _text", '    return Message(is_sender, _text(item.get("body")), _text(item.get("createdAt")), item)',
     '    return Message(is_sender, item.get("body"), _text(item.get("createdAt")), item)'),
    ("[T18] message createdAt not _text", '    return Message(is_sender, _text(item.get("body")), _text(item.get("createdAt")), item)',
     '    return Message(is_sender, _text(item.get("body")), item.get("createdAt"), item)'),
    ("[T19] sender not checked to be a str", '.get(sender) if isinstance(sender, str) else None', '.get(sender) if sender is not None else None'),
    ("[T20] _text accepts bytes", "    return value if isinstance(value, str) else None", "    return value if isinstance(value, (str, bytes)) else None"),
    ("[T21] _member_id without the str check", "    return isinstance(value, str) and MEMBER_ID.fullmatch(value) is not None",
     "    return MEMBER_ID.fullmatch(value) is not None"),
    ("[T22] _member_like without the str check", '    return (isinstance(value, str) and value.casefold().startswith(("acoa", "acwa"))',
     '    return (value.casefold().startswith(("acoa", "acwa"))'),
    ("[T23] _provider_id accepts non-str", "return value.strip() if isinstance(value, str) and value.strip() else None",
     'return str(value).strip() if value is not None and str(value).strip() else None'),
    ("[T24] _profile_key without the str check", '    if not isinstance(value, str):\n        raise TypeError("A profile URL string is required")',
     "    if False:\n        raise TypeError(\"A profile URL string is required\")"),
    ("[T25] lookup provider id isinstance (any truthy)", "provider_id=provider_id if isinstance(provider_id, str) else None",
     "provider_id=provider_id if provider_id else None"),
    ("[T26] pending isinstance int (bool subclass check loosened)", "if (type(person.pending_invitation) is not bool or",
     "if (not isinstance(person.pending_invitation, int) or"),
    ("[T28] _profile lets a non-profile raise", "        key = _profile_key(value)\n    except (TypeError, ValueError):", "        key = _profile_key(value)\n    except ():"),
    ("[T29] FakePort invitee profile errors raise", '            invitee = _profile_key(item["invitee_profile"])\n        except (TypeError, ValueError):',
     '            invitee = _profile_key(item["invitee_profile"])\n        except ():'),
    ("[T30] FakePort person lookup errors raise", "            person = self.people.get(_profile_key(profile_url))\n        except (TypeError, ValueError):",
     "            person = self.people.get(_profile_key(profile_url))\n        except ():"),
    # Round 11.
    ("[Q02] _profile parses a bare spelling without the https:// prefix",
     '    path = urllib.parse.urlsplit(value if "://" in value else "https://" + value).path', "    path = urllib.parse.urlsplit(value).path"),
    ("[Q03] _profile_key without the https:// prefix", '    return profile_key(value if "://" in value else "https://" + value)', "    return profile_key(value)"),
    ("[Q19] FakePort.fail ignores times", "        self.failures.setdefault(method, []).extend([kind] * times)",
     "        self.failures.setdefault(method, []).extend([kind])"),
    ("[Q31] Unsupported code", '    code = "UNSUPPORTED"', '    code = "PORT_ERROR"'),
    ("[Q31b] NotFound code", '    code = "NOT_FOUND"', '    code = "PORT_ERROR"'),
    ("[Q31c] PortError message default", "        super().__init__(message or self.code)", "        super().__init__(message)"),
    ("[Q34] PORT_METHODS missing start_chat", '"messages", "send_message", "start_chat")', '"messages", "send_message")'),
    ("[T27] distance accepts bool", "type(person.network_distance) is not int\n", "not isinstance(person.network_distance, int)\n"),
]


PROPERTY_MUTANTS = [
    "pending type check reduced to `is None`", "distance type check dropped", "distance 0 or negative accepted (n16)",
    "slug NFKC dropped", "member id = casefold startswith acoa", "member-like vanity can be someone else",
    "`and ids` dropped from the different-id rule", "undecidable counts as absent",
    "case-free set built before the caller member slug is added [N04]", "MEMBER_ID {20,34} [N05]",
    "slug not unquoted [N07]", "same-is-True ignored [N01]", "every caller slug into ids [N02]",
    "case-only loop after someone-else [N25]", "[Q02] _profile parses a bare spelling without the https:// prefix",
]


def run(directory, only=None):
    # No bytecode is read or written: a stale .pyc of the same size and mtime second could
    # otherwise stand in for the mutated source and make an unrun mutant look KILLED.
    for cache in Path(directory).rglob("__pycache__"):
        shutil.rmtree(cache)
    result = subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-p", "test_linkedin_port.py"]
                            + (["-k", only] if only else []),
                            cwd=directory, capture_output=True, text=True,
                            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    ran = re.search(r"^Ran (\d+) tests?", result.stderr, re.MULTILINE)
    broken = ran is None or int(ran.group(1)) == 0 or "ImportError" in result.stderr or "_FailedTest" in result.stderr
    summary = " ".join(line for line in result.stderr.splitlines() if line.startswith(("Ran", "OK", "FAILED")))
    return result.returncode, broken, summary


def main():
    source = (ROOT / "linkedin_port.py").read_text()
    only = "InvitationAbsentProperty" if "--property" in sys.argv[1:] else None
    mutants = [m for m in MUTANTS if m[0] in PROPERTY_MUTANTS] if only else MUTANTS
    if only and len(mutants) != len(PROPERTY_MUTANTS):
        print("PROPERTY_MUTANTS names a mutant that is not in MUTANTS")
        return 2
    with tempfile.TemporaryDirectory() as temporary:
        copy = Path(temporary)
        for path in ROOT.iterdir():
            if path.is_file():
                shutil.copy2(path, copy / path.name)
        shutil.copytree(ROOT / "tests", copy / "tests", ignore=shutil.ignore_patterns("__pycache__"))
        code, broken, summary = run(copy, only)
        if code != 0 or broken:
            print("BASELINE FAILS:", summary)
            return 2
        print("baseline passes:", summary)
        survivors = 0
        for index, (name, original, replacement) in enumerate(mutants, 1):
            if source.count(original) != 1:
                print(f"{index:2} STALE    {name} (original text not found exactly once)")
                survivors += 1
                continue
            (copy / "linkedin_port.py").write_text(source.replace(original, replacement))
            code, broken, summary = run(copy, only)
            verdict = "BROKEN  " if broken else "KILLED  " if code else "SURVIVED"
            survivors += verdict != "KILLED  "
            print(f"{index:2} {verdict} {name} | {summary}")
        (copy / "linkedin_port.py").write_text(source)
    print(f"{len(mutants) - survivors}/{len(mutants)} killed" + (" by the property sweep alone" if only else ""))
    return 0 if survivors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
