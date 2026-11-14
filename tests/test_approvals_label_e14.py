"""E14, Approvals: filter by label (owner, 2026-11-10). Chips with counts; a message label narrows
Messages waiting by purpose and hides the other sections; the label is filtered before the page is cut
and every form and pager link keeps it; "Send these N replies" approves only those, mode unchanged."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import hashlib
import json
import re
import sqlite3
import unittest

import applications
import approvals
import test_approvals_page_e6t4 as base
import test_dashboard_applications as apps_base

if base.DEPENDENCIES:
    import approvals_page

PURPOSES = {"invite": "INVITE", "first": "INITIAL", "accepted": "FIRST_MESSAGE", "follow": "FOLLOWUP",
            "urgent": "URGENT", "reply": "REPLY"}


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def section(page, name):
    return page.split('id="%s"' % name, 1)[1].split("</section>", 1)[0] if 'id="%s"' % name in page else None


def chip(page, words):
    found = re.search(r'>%s <span class="app-count">(\d+)</span></a>' % re.escape(words), page)
    return int(found.group(1)) if found else None


@unittest.skipUnless(base.DEPENDENCIES, "dashboard packages (fastapi, httpx) are not installed")
class FilterByLabel(base.PageBase):
    def message(self, action_id, purpose, text=None):
        """A held message with this purpose (an invitation hold, its purpose and hash rewritten)."""
        self.hold(action_id, action_id.replace("-", ""), base.note(100))
        payload = dict(self.payload(action_id), purpose=purpose)
        if text is not None:
            payload.update(message=text, note=text)
        text = json.dumps(payload, sort_keys=True)
        with self.db:
            self.db.execute("UPDATE outreach_actions SET payload=?, payload_hash=? WHERE action_id=?", (text, sha(text), action_id))

    def launch(self, action_id):
        text = base.note(200)
        payload = json.dumps({"action_id": action_id, "message": text, "purpose": "PREPARE_CAMPAIGN"}, sort_keys=True)
        with self.db:
            self.db.execute("INSERT INTO outreach_actions VALUES (?,?,'PREPARE_CAMPAIGN',?,?,'PREPARED',NULL,NULL)",
                            (action_id, "linkedin.com/in/" + action_id, payload, sha(payload)))
            candidate = {"preparation_action_id": action_id, "preparation_payload_hash": sha(payload), "recipient": "Fay Example",
                         "company": "Acme", "profile": "linkedin.com/in/" + action_id, "message": text}
            self.db.execute("INSERT INTO outreach_queue VALUES (?,77,'Fay Example','Acme',?,?,?)",
                            ("linkedin.com/in/" + action_id, approvals.AWAITING, json.dumps(candidate),
                             json.dumps({"held_by": "REVIEW", "held_at": base.NOW.isoformat()})))

    def application(self, key):
        applications.initialize(self.db)
        snapshot = json.dumps({"route": "ats", "destination": "https://jobs.example.com/x", "subject": None,
                               "resume": "ATS version (PDF)", "questions": []})
        with self.db:
            apps_base.job(self.db, key, "BLOCKED", applications.AWAITING_APPLICATION_APPROVAL)
            self.db.execute("INSERT INTO application_approvals(job_key,approval_id,state,held_at,snapshot,payload_hash) "
                            "VALUES (?,?,'WAITING','2026-10-21T17:00:00+00:00',?,'h')", (key, "a-" + key, snapshot))

    def mixed(self):
        self.review()
        for name in ("invite-1", "invite-2"):
            self.message(name, "INVITE")
        self.launch("prep-1")
        self.message("first-1", "INITIAL")
        self.message("accepted-1", "FIRST_MESSAGE")
        self.message("follow-1", "FOLLOWUP")
        self.message("urgent-1", "URGENT")
        for index in range(3):
            self.message("reply-%d" % index, "REPLY")
        self.application("held-1")
        base.EveryListPages.questions(self, 2)
        base.NotesForYou.flag(self, "task-1", "Email Sam about the role.")

    def ids(self, page):
        return set(re.findall(r'name="action_id" value="([^"]+)"', section(page, "messages") or ""))

    def test_each_chip_shows_only_its_purposes_and_counts_equal_cards(self):
        self.mixed()
        everything = self.page()
        expected = {"invitation-notes": ({"invite-1", "invite-2", "prep-1"}, "Invitation notes"),
                    "first-messages": ({"first-1", "accepted-1"}, "First messages"),
                    "follow-ups": ({"follow-1", "urgent-1"}, "Follow-ups"),
                    "replies": ({"reply-0", "reply-1", "reply-2"}, "Replies")}
        for label, (wanted, words) in expected.items():
            page = self.page("/approvals?label=" + label)
            self.assertEqual(self.ids(page) - {""}, wanted, label)
            self.assertEqual(section(page, "messages").count('<article class="approval-card'), len(wanted), label)
            self.assertEqual(chip(everything, words), len(wanted), label)
            for hidden in ("applications", "questions", "notes", "notices", "refused"):
                self.assertIsNone(section(page, hidden), (label, hidden))
        for label, words, cards in (("applications", "Applications", 1), ("questions", "Questions", 2), ("notes", "Notes", 1)):
            page = self.page("/approvals?label=" + label)
            self.assertEqual([name for name in ("messages", "applications", "questions", "notes", "notices")
                              if section(page, name) is not None], [label])
            self.assertEqual(section(page, label).count('<article class="approval-card'), cards, label)
            self.assertEqual(chip(everything, words), cards, label)
        self.assertEqual(chip(everything, "All"), 10 + 1 + 2 + 1)
        self.assertIsNotNone(section(everything, "notices"))
        # An unknown label is All.
        self.assertEqual(self.ids(self.page("/approvals?label=bogus")), self.ids(everything))
        self.assertIsNotNone(section(self.page("/approvals?label=bogus"), "questions"))

    def test_filter_before_paging(self):
        self.review()
        for index in range(approvals_page.PAGE_SIZE + 5):
            self.message("invite-%02d" % index, "INVITE")
        for index in range(approvals_page.PAGE_SIZE + 2):
            self.message("reply-%02d" % index, "REPLY")
        page = self.page("/approvals?label=replies")
        messages = section(page, "messages")
        self.assertEqual(messages.count('<article class="approval-card'), approvals_page.PAGE_SIZE)
        self.assertTrue(all(name.startswith("reply-") for name in self.ids(page)))
        self.assertIn("Page 1 of 2 (27 waiting)", messages)
        self.assertEqual(chip(page, "Replies"), 27)
        self.assertIn('href="/approvals?label=replies&page=2#messages">Next</a>', messages)
        second = self.page("/approvals?label=replies&page=2")
        self.assertEqual(section(second, "messages").count('<article class="approval-card'), 2)
        self.assertIn("Page 2 of 2 (27 waiting)", second)
        # Every card form posts back to the same filtered page.
        self.assertEqual(set(re.findall(r'action="/approvals/messages/(?:send|reject)\?([^"]+)"', second)), {"label=replies&page=2"})
        # A chip opens its label at page 1.
        self.assertIn('href="/approvals?label=invitation-notes">Invitation notes', second)
        self.assertIn('href="/approvals">All', second)

    def test_a_card_approved_under_a_filter_returns_to_the_filter(self):
        self.mixed()
        page = self.post("/approvals/messages/send?label=replies", {"action_id": "reply-0", "payload_hash": self.shown("reply-0"),
                                                                    "text": self.payload("reply-0")["message"]})
        self.assertEqual(self.state("reply-0"), "PENDING")
        self.assertEqual(self.ids(page), {"reply-1", "reply-2"})
        self.assertIsNone(section(page, "questions"))
        self.assertIn('aria-current="true">Replies <span class="app-count">2</span>', page)
        # With htmx the card that comes back (here: shown with an old hash) keeps the label in its forms.
        card = self.post("/approvals/messages/send?label=replies&page=1", {"action_id": "reply-1", "payload_hash": "old",
                                                                           "text": "Hello."}, base.HTMX)
        self.assertEqual(self.state("reply-1"), approvals.AWAITING)
        self.assertIn('action="/approvals/messages/send?label=replies"', card)
        held = self.page("/approvals?label=applications")
        self.assertIn('action="/approvals/applications/approve?label=applications"', held)

    def test_send_these_under_a_filter_approves_only_those_and_keeps_the_mode(self):
        self.mixed()
        page = self.page("/approvals?label=invitation-notes")
        self.assertIn("Send these 3 invitation notes", page)
        self.assertNotIn("Send these", self.page())   # under All the page has no bulk button (unchanged)
        form = page.split('action="/approvals/messages/release?label=invitation-notes"', 1)[1].split("</form>", 1)[0]
        shown = list(zip(re.findall(r'name="action_id" value="([^"]+)"', form), re.findall(r'name="payload_hash" value="([^"]+)"', form)))
        self.assertEqual({action for action, _ in shown}, {"invite-1", "invite-2", "prep-1"})
        page = self.release("/approvals/messages/release?label=invitation-notes",
                         {"action_id": [a for a, _ in shown], "payload_hash": [h for _, h in shown]})
        self.assertIn("Approved 3 messages.", page)
        self.assertEqual(approvals.install_mode(self.root, self.home), approvals.REVIEW)
        waiting = {item["action_id"] for item in approvals.waiting(self.db)}
        self.assertEqual(waiting, {"first-1", "accepted-1", "follow-1", "urgent-1", "reply-0", "reply-1", "reply-2"})
        self.assertEqual([record["decided_via"] for record in approvals.decisions(self.db, "invite-1")], ["dashboard"])
        self.assertIn("Send these 3 replies", self.page("/approvals?label=replies"))
        # Under All the bulk post is refused; nothing changes.
        page = self.release("/approvals/messages/release", {"action_id": ["reply-0"], "payload_hash": [self.shown("reply-0")]})
        self.assertEqual(self.state("reply-0"), approvals.AWAITING)
        self.assertEqual(approvals.install_mode(self.root, self.home), approvals.REVIEW)

    def release(self, path, data):
        """The "Send these N" form as the page sends it (htmx): the page comes back in place."""
        return self.post(path, data, base.HTMX)

    def test_send_these_leaves_the_address_on_the_approvals_page(self):
        """Walkthrough 2026-11-12: the release address is never the page's address."""
        self.mixed()
        page = self.page("/approvals?label=replies")
        form = page.split('<form method="post" action="/approvals/messages/release?label=replies"', 1)[1].split(">", 1)[0]
        self.assertIn('hx-post="/approvals/messages/release?label=replies"', form)
        self.assertIn('hx-target=".approvals-page" hx-select=".approvals-page"', form)
        self.assertIn('hx-push-url="false"', form)
        shown = self.release_form(page, "replies")
        data = {"action_id": [a for a, _ in shown[:1]], "payload_hash": [h for _, h in shown[:1]]}
        # htmx: the page in place, nothing pushed or redirected.
        answer = self.client.post("/approvals/messages/release?label=replies", data=data, headers=base.HTMX)
        self.assertEqual(answer.status_code, 200)
        self.assertIn("Approved 1 message.", answer.text)
        self.assertIn("Send these 2 replies", answer.text)
        self.assertFalse({"hx-push-url", "hx-replace-url", "hx-redirect", "hx-location"} & set(answer.headers))
        # A plain post: back to the page it came from (303), the result line shown once.
        data = {"action_id": [a for a, _ in shown[1:]], "payload_hash": [h for _, h in shown[1:]]}
        answer = self.client.post("/approvals/messages/release?label=replies", data=data, headers=base.ORIGIN)
        self.assertEqual((answer.status_code, answer.headers["location"]), (303, "/approvals?label=replies"))
        self.assertEqual(self.state(shown[1][0]), "PENDING")
        self.assertIn("Approved 2 messages.", self.page("/approvals?label=replies"))
        self.assertNotIn("Approved 2 messages.", self.page("/approvals?label=replies"))
        # The release address opened: the Approvals page with a valid label, never a raw error.
        for path, to in (("/approvals/messages/release?label=follow-ups&page=2", "/approvals?label=follow-ups&page=2"),
                         ("/approvals/messages/release?label=nonsense", "/approvals")):
            answer = self.client.get(path)
            self.assertEqual((answer.status_code, answer.headers["location"]), (303, to))

    def test_keyboard_focus_on_a_chip_is_not_the_current_chips_outline(self):
        page = self.page("/approvals?label=replies")
        self.assertEqual(re.findall(r'<a class="chip [^"]*app-filter-active" href="[^"]*" aria-current="true">(\w+)', page), ["Replies"])
        css = (base.dashboard.ASSETS / "static" / "dashboard.css").read_text()
        focus = re.search(r"\.approvals-labels \.chip:focus-visible \{([^}]*)\}", css).group(1)
        current = re.search(r"\n\.app-filter-active \{([^}]*)\}", css).group(1)
        self.assertIn("outline-offset", focus)
        self.assertNotEqual(focus.strip(), current.strip())

    def release_form(self, page, label):
        form = page.split('action="/approvals/messages/release?label=%s"' % label, 1)[1].split("</form>", 1)[0]
        return list(zip(re.findall(r'name="action_id" value="([^"]+)"', form), re.findall(r'name="payload_hash" value="([^"]+)"', form)))

    def test_send_these_refuses_what_the_single_card_refuses(self):
        """Review 1 and 2: a connection note over the limit and an empty message are not approved by
        "Send these N" (the single card refuses both); each is reported in its own words, not as changed."""
        self.review()
        self.message("invite-long", "INVITE", base.note(400))
        self.message("invite-blank", "INVITE", "   \n ")
        self.message("invite-ok", "INVITE")
        self.launch("prep-1")
        # The single card refuses the same two as written.
        self.assertEqual(approvals.revise_and_approve(self.db, "invite-long", base.note(400), shown_payload_hash=self.shown("invite-long"))["reason"],
                         "INVITATION_NOTE_TOO_LONG")
        self.assertEqual(approvals.revise_and_approve(self.db, "invite-blank", "   \n ", shown_payload_hash=self.shown("invite-blank"))["reason"],
                         "MESSAGE_EMPTY")
        page = self.page("/approvals?label=invitation-notes")
        shown = self.release_form(page, "invitation-notes")
        self.assertEqual({action for action, _ in shown}, {"invite-long", "invite-blank", "invite-ok", "prep-1"})
        page = self.release("/approvals/messages/release?label=invitation-notes",
                         {"action_id": [a for a, _ in shown], "payload_hash": [h for _, h in shown]})
        self.assertIn("Approved 2 messages.", page)
        self.assertIn("1 invitation note is too long and was not sent", page)
        self.assertIn("1 message is empty and was not sent", page)
        self.assertNotIn("changed since", page)
        self.assertEqual(self.state("invite-long"), approvals.AWAITING)
        self.assertEqual(self.state("invite-blank"), approvals.AWAITING)
        self.assertEqual(self.state("invite-ok"), "PENDING")
        self.assertEqual(approvals.decisions(self.db, "invite-long"), [])
        self.assertEqual(approvals.install_mode(self.root, self.home), approvals.REVIEW)
        # Both still wait on their cards; the bulk form now lists exactly those two, in plural words.
        self.assertEqual({action for action, _ in self.release_form(page, "invitation-notes")}, {"invite-long", "invite-blank"})
        page = self.release("/approvals/messages/release?label=invitation-notes",
                         {"action_id": [a for a, _ in shown], "payload_hash": [h for _, h in shown]})
        self.assertIn("Nothing was approved. 1 invitation note is too long and was not sent", page)
        self.assertIn("1 message is empty and was not sent", page)
        self.assertIn("2 were already approved.", page)
        # A long note at the limit, not over it, is approved (an unchanged single card would send it).
        self.message("invite-edge", "INVITE", base.note(approvals.CONNECTION_NOTE_LIMIT))
        result = approvals.release_all(self.db, [("invite-edge", self.shown("invite-edge"))], automatic=False)
        self.assertEqual([item["action_id"] for item in result["released"]], ["invite-edge"])
        self.assertEqual((result["too_long"], result["empty"], result["already"], result["changed"]), ([], [], [], []))

    def test_a_card_approved_by_htmx_is_already_approved_to_send_these(self):
        """Review 3: after one card is approved in place (htmx), the bulk button's list and the chip
        counts are swapped in with it; an old form that still lists the card reports it as already
        approved, not as changed."""
        self.mixed()
        page = self.page("/approvals?label=replies")
        shown = self.release_form(page, "replies")
        self.assertEqual(len(shown), 3)
        self.assertIn('id="approvals-release"', page)
        self.assertNotIn("hx-swap-oob", page)
        answer = self.post("/approvals/messages/send?label=replies", {"action_id": "reply-0", "payload_hash": self.shown("reply-0"),
                                                                      "text": self.payload("reply-0")["message"]}, base.HTMX)
        self.assertEqual(self.state("reply-0"), "PENDING")
        self.assertTrue(answer.lstrip().startswith('<article class="approval-card'))
        self.assertNotIn("<html", answer)
        # The chip row and the bulk form come back beside the card, swapped in by id.
        self.assertIn('id="approvals-labels" aria-label="Filter by label" hx-swap-oob="true"', answer)
        self.assertIn('id="approvals-release" hx-swap-oob="true"', answer)
        self.assertEqual(chip(answer, "Replies"), 2)
        self.assertEqual(chip(answer, "All"), 13)
        self.assertEqual({action for action, _ in self.release_form(answer, "replies")}, {"reply-1", "reply-2"})
        self.assertIn("Send these 2 replies", answer)
        # The old form, posted as it stood: the approved card is "already approved", the rest go.
        page = self.release("/approvals/messages/release?label=replies",
                         {"action_id": [a for a, _ in shown], "payload_hash": [h for _, h in shown]})
        self.assertIn("Approved 2 messages.", page)
        self.assertIn("1 was already approved.", page)
        self.assertNotIn("changed since", page)
        self.assertEqual({self.state(name) for name in ("reply-0", "reply-1", "reply-2")}, {"PENDING"})
        # A launch approved meanwhile is already approved too; a row whose text changed is still "changed".
        page = self.page("/approvals?label=invitation-notes")
        shown = self.release_form(page, "invitation-notes")
        self.post("/approvals/messages/approve?label=invitation-notes", {"action_id": "prep-1", "payload_hash": self.shown("prep-1")}, base.HTMX)
        rewritten = json.dumps(dict(self.payload("invite-2"), message=base.note(120)), sort_keys=True)   # a new hash
        with self.db:
            self.db.execute("UPDATE outreach_actions SET payload=?, payload_hash=? WHERE action_id='invite-2'", (rewritten, sha(rewritten)))
        page = self.release("/approvals/messages/release?label=invitation-notes",
                         {"action_id": [a for a, _ in shown], "payload_hash": [h for _, h in shown]})
        self.assertIn("Approved 1 message.", page)
        self.assertIn("1 was already approved.", page)
        self.assertIn("1 changed since it was shown and was not sent.", page)
        self.assertEqual(self.state("invite-2"), approvals.AWAITING)
        # Under a chip that hides Messages waiting, only the chip row comes back with the card.
        answer = self.post("/approvals/applications/reject?label=applications", {"application_id": "held-1", "payload_hash": "h"}, base.HTMX)
        self.assertIn('id="approvals-labels" aria-label="Filter by label" hx-swap-oob="true"', answer)
        self.assertNotIn('id="approvals-release"', answer)
        self.assertEqual(chip(answer, "Applications"), 0)

    def test_a_card_revised_and_sent_is_already_approved_to_send_these(self):
        """Review 4: a card revised and approved on its card, then sent, has an approval of the edited
        payload only; an old form that still lists the shown payload reports it as already approved,
        not as changed."""
        self.mixed()
        shown = self.release_form(self.page("/approvals?label=invitation-notes"), "invitation-notes")
        self.assertEqual({action for action, _ in shown}, {"invite-1", "invite-2", "prep-1"})
        old = self.shown("invite-1")
        result = approvals.revise_and_approve(self.db, "invite-1", base.note(90, "edited"), shown_payload_hash=old)
        self.assertEqual(result["state"], "REVISED", result)
        self.assertEqual(self.state("invite-1"), "PENDING")
        self.assertNotEqual(self.shown("invite-1"), old)
        self.assertIsNone(approvals.approval(self.db, "invite-1", old))
        with self.db:
            self.db.execute("UPDATE outreach_actions SET state='SENT' WHERE action_id='invite-1'")
        page = self.release("/approvals/messages/release?label=invitation-notes",
                         {"action_id": [a for a, _ in shown], "payload_hash": [h for _, h in shown]})
        self.assertIn("Approved 2 messages.", page)
        self.assertIn("1 was already approved.", page)
        self.assertNotIn("changed since", page)
        self.assertEqual(self.state("invite-1"), "SENT")
        self.assertEqual(self.state("invite-2"), "PENDING")

    def test_send_these_under_a_label_ignores_another_labels_card(self):
        """Review 4: a form posted under Replies that lists an invitation note's card does not approve
        it; the note stays waiting and counts as changed."""
        self.mixed()
        page = self.release("/approvals/messages/release?label=replies",
                         {"action_id": ["reply-0", "invite-1"], "payload_hash": [self.shown("reply-0"), self.shown("invite-1")]})
        self.assertIn("Approved 1 message.", page)
        self.assertIn("1 changed since it was shown and was not sent.", page)
        self.assertNotIn("already approved", page)
        self.assertEqual(self.state("reply-0"), "PENDING")
        self.assertEqual(self.state("invite-1"), approvals.AWAITING)
        self.assertIsNone(approvals.approval(self.db, "invite-1", self.shown("invite-1")))

    def test_unreadable_count_shows_under_all_only(self):
        """Review 4: the count of waiting messages that could not be read is of all of them, so it is
        not shown under a message chip."""
        self.review()
        self.message("reply-0", "REPLY")
        with self.db:
            self.db.execute("INSERT INTO outreach_actions VALUES ('bad','linkedin.com/in/bad','INVITE','{not json','x',?,NULL,'{also bad')",
                            (approvals.AWAITING,))
        self.assertIn("1 waiting message could not be read", self.page())
        for label in ("replies", "invitation-notes", "first-messages", "follow-ups"):
            self.assertNotIn("could not be read", self.page("/approvals?label=" + label), label)
        self.assertEqual(chip(self.page(), "Replies"), 1)

    def test_a_database_from_before_the_label_column_renders(self):
        """No column is added for E14; a user_decisions table without its later columns (label and the
        rest, Q3) still renders under every chip."""
        with self.db:
            self.db.execute("CREATE TABLE user_decisions(id TEXT PRIMARY KEY, job_key TEXT, field TEXT, semantic TEXT, "
                            "answer_type TEXT, choices TEXT, payload_hash TEXT, expires_at INTEGER, state TEXT)")
            self.db.execute("INSERT INTO user_decisions VALUES ('q1','job:1','f','github_profile','text','[]','h',?,'PENDING')",
                            (int(base.NOW.timestamp() * 1000) + 30 * 86400000 * 12,))
        for label, _, _ in approvals_page.LABELS:
            page = self.page("/approvals?label=" + label)
            self.assertIn("Approvals", page)
        self.assertEqual(chip(self.page(), "Questions"), 1)
        self.assertEqual(section(self.page("/approvals?label=questions"), "questions").count('<article class="approval-card'), 1)


if __name__ == "__main__":
    unittest.main()
