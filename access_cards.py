"""The Needs you cards for applications waiting on a sign-in or a human check (E15.T2).

`view` is display only (a read-only connection, nothing written): one card per waiting job, on
Overview's Needs you and on Approvals. Which jobs wait is one rule, `overview_page.waiting_keys`;
what a card says comes from the wait itself (`access_outcomes.waiting`: employer, role, the reason's
sentence, the deadline, the posting link the job already stores).

Grouping: jobs waiting on the same site's sign-in are one card. The job that met the sign-in first
leads it, the others are listed under it, and the card has one action for all of them, because
`access_outcomes.resume` on one of them releases every job waiting on that site for that reason. A
human check is always a card of its own (the site asks each job separately).

The buttons (`abandon`, `done`, `try_again`, `step`) are the first production callers of the
user actions in `access_outcomes`. Each runs inside one saved transaction (`with database:`), takes
the write lock before it checks anything, and acts only on a job key that has a card (Try again: on
a job `access_outcomes.try_again` itself accepts). A second click finds nothing left to do and says
so. Each returns {"tone", "text"} in plain words for the page.

Two seams (joined in E15):
- the user's step (`user_step`: the live view, E15.T5): "Sign in" and "Do the human check", and the
  done buttons beside them, are offered only while `user_step.available(purpose)` is true (not on
  Windows today); where it is not, such a card offers apply yourself, or Abandon. A step the user
  finished in the live view releases the wait through `step_finished`;
- the setting "When a site needs an account" (E15.T6; owner's pick, restored 2026-11-12): every
  account card (a reason in `access_outcomes.HOLDING_REASONS`; never a human check or a passkey
  card) carries a small link to it (`SETTING_HREF`), on Overview and Approvals. Under Skip, where no
  account card is left, the same link shows once under the cards. The sentence `SETTING_WORDS` goes
  with it only once Sidedoor can sign in itself (`access_outcomes.resolver_available`, false today).
  No link while the setting already is "for me".

A waiting job whose submission may have reached the employer (`applications.uncertain_submission`)
is not waiting on the user: its card says `NOT_ABANDONED` and has no step, done or Abandon button, it
never leads a site's card, and Applications says the same sentence (owner, 2026-11-12, F2).

A second source (owner, 2026-11-12, picks L6 a and L7 A): the contact finder waiting on the user's own
step on LinkedIn (`linkedin_contacts.user_wait`: run rows in USER_WAIT, no job and no row in
application_access_waits). It is one card, first in the list, however many searches wait
(`finder_card`), with one button, the user's step; it has no deadline, no posting and no Abandon,
and is counted like any other card (`card_count`). Its key is `linkedin_contacts.CARD_KEY`, which
`step`, `step_refusal` and `step_finished` take in place of a job key. Application cards and
application waits are not changed by it: an application that also waits on LinkedIn keeps its own card.

A third source (owner's pick S7 a, 2026-11-13): an application that may already have reached the
employer, whose confirming read met a sign-in or a human check (`confirm_read`: the job stays in
RECONCILE; a row in its own table, none in application_access_waits). One card per job
(`confirm_cards`), after the finder's, with one button, the user's step; no posting link, no
"apply yourself", no Abandon and no done button of its own (any of them could lead to a second
application). Finishing the step only makes the job's next confirming read due
(`confirm_read.done`). Its key is `confirm_read.key(job)`, never a job key, so the application
cards' buttons cannot be addressed to it. It is counted like any other card. The uncertain job with
no blocked read keeps its card with no buttons, not counted, as before.
"""

import datetime as dt
import hashlib
import json
from zoneinfo import ZoneInfo

import access_outcomes
import access_step
import attended_submit
import live_view
import applications_page
import overview_page
import user_step

SECTION = "sign-ins"                     # the section's id on Approvals
APPROVALS_HREF = "/approvals#" + SECTION
OVERVIEW_LIMIT = 5                       # cards shown on Overview; the rest are on Approvals
# The one place the setting "When a site needs an account" lives: Settings > Applying (E15.T6).
SETTING_HREF = "/settings#account-setting"
FOR_ME = access_outcomes.SIGN_IN_FOR_ME  # "Sign in and create accounts for me": no link then
SETTING_WORDS = "Sidedoor can sign in and create accounts for you."
SETTING_LABEL = "Change \"When a site needs an account\" in Settings"

# What the "done" button says and the line beside it, by what the site asks for. A sign-in or a human
# check has its done button only while the user's step can be shown (`user_step.available`): until
# then Sidedoor offers no way to do either, so the card offers apply yourself or Abandon (owner,
# 2026-11-13). A reason this page does not know has neutral words and no done button.
SIGN_IN, HUMAN_CHECK, CONFIRM, OTHER, UNKNOWN = "sign_in", "human_check", "confirm", "other", "unknown"
# The site's own profile, to be completed before the application can be reached (owner, 2026-11-14): the
# user's step opens the site (the sign-in step's page), with its own label, and the card has its done button.
PROFILE = "profile"
PROFILE_STEP_LABEL = "Open the site"
# The reasons a sign-in in the user's step settles. Listed here, not read from
# `access_outcomes.ACCOUNT_REASONS`: an account reason added there later gets no button until it is
# named here (it shows its sentence, apply yourself and Abandon).
SIGN_IN_REASONS = frozenset({"SIGN_IN", "ACCOUNT_EXISTS", "CODE_UNREADABLE", "SIGN_UP_FAILED", "SIGN_UP_UNCONFIRMED",
                             "SIGN_IN_EMAIL_MISSING"})
# The reasons that wait on a confirmation the user can do themselves ("I've confirmed it", E15.T8).
CONFIRM_REASONS = frozenset({"CONFIRMATION_EMAIL_MISSING", "CONFIRMATION_LINK_UNOPENED"})
LINK_HINT = "Open the link in the site's email. Then press I've confirmed it."
RESEND_LABEL = "Resend"
# The reasons whose card offers "Sign in" while the user's step is available.
STEP_REASONS = SIGN_IN_REASONS | {"ASK_EACH_TIME"}
# Every other reason has no done button: pressing one would only send the job back to the same
# sign-in with nothing changed (owner, 2026-11-13).
DONE = {SIGN_IN: ("I've signed in", None),
        PROFILE: ("I've done it", None),
        # Owner, 2026-11-14 ("Fix"): every sign-in or human-check card always has a way to say it is done.
        HUMAN_CHECK: ("I've done the check", None),
        # The card's sentence says the email did not arrive, so the hint never sends the user to it.
        CONFIRM: ("I've confirmed it", "Ask the site to send it again, or confirm your account on the site. Then press I've confirmed it."),
        OTHER: (None, None),
        UNKNOWN: (None, None)}
UNKNOWN_WORDS = "This application is waiting for you."
STEP_LABELS = {user_step.SIGN_IN: "Sign in", user_step.HUMAN_CHECK: "Do the human check"}
APPLY_YOURSELF = "You can also apply yourself."          # only on a card that offers something else to do
APPLY_YOURSELF_ONLY = "Apply yourself if you want this job."
POSTING_LABEL = "Open the posting"
CLOSES = "Waits for you until %s, then closes."
# A passkey site: nothing the user does in Sidedoor ends the wait (F5), so it is not "waits for you".
PASSKEY_CLOSES = "Closes on %s unless you abandon it first."
# A job held behind a site, on a computer where the sign-in step cannot be shown (F5).
HELD_NO_STEP_WORDS = access_outcomes.HOST_HELD_WORDS   # one line whatever this computer can show (owner, 2026-11-14)
ALSO_WAITING = "Also waiting on this site:"

# The contact finder's card (the owner's sentences, 2026-11-12).
FINDER_NAME = "LinkedIn"
# The LinkedIn card's sentences (owner, 2026-11-13: the action only, whichever need raised the card; never
# what Sidedoor does with it or why). After Done the redraw says what every card says (live_view.ENDED_WORDS).
LINKEDIN_WORDS = "Sidedoor's browser needs you to sign in to LinkedIn."
LINKEDIN_CHECK_WORDS = "LinkedIn needs you to do a human check in Sidedoor's browser."
FINDER_WORDS = {"SIGN_IN": LINKEDIN_WORDS, "HUMAN_CHECK": LINKEDIN_CHECK_WORDS}
FINDER_GONE = "LinkedIn is no longer waiting for you."
FINDER_PURPOSES = {"SIGN_IN": user_step.SIGN_IN, "HUMAN_CHECK": user_step.HUMAN_CHECK}

# The confirming read's card. Owner's wording, 2026-11-13: state and instruction only; the card's whole
# text is one of these (the sign-in one follows his human-check sentence), with the step's button.
CONFIRM_WORDS = {"HUMAN_CHECK": "The site needs a human check. Do not apply again.",
                 "SIGN_IN": "The site needs you to sign in. Do not apply again."}
CONFIRM_GONE = "This application is no longer waiting for this step."
CONFIRM_PURPOSES = {"SIGN_IN": user_step.SIGN_IN, "HUMAN_CHECK": user_step.HUMAN_CHECK}

GONE = "This application is no longer waiting for you."
ABANDONED = "Abandoned. You can bring it back with Try again on Applications."
NOT_ABANDONED = "This application may already have reached the employer, so it stays open while Sidedoor checks."
NOT_CLOSED = "Sidedoor could not close this application. Reload the page and try again."
RELEASED_ONE = "Sidedoor tries this application on its next round."
RELEASED_MANY = "Sidedoor tries these %d applications on its next round."
STAYS_ONE = "One application stays open while Sidedoor checks whether it reached the employer."
STAYS_MANY = "%d applications stay open while Sidedoor checks whether they reached the employer."
REOPENED = "Reopened. Sidedoor reads the posting again on its next round."
# One sentence for one situation: the live view's own words for a computer that cannot show the step.
# A press from an older page on a passkey card (D11): the owner's passkey sentence, the one constant
# the card, Applications and the live view all read.
PASSKEY_PRESS = access_outcomes.WAIT_REASONS["PASSKEY"]
# The job's stored address is not one the live view opens (C4): what a press from an older page says.
NOT_OPENABLE = "This posting can't be opened here. Apply yourself with the link on the card."
STEP_UNAVAILABLE = "This step can't be done inside Sidedoor on this computer yet. Apply yourself with the link on the card."
NOT_OFFERED = "Sidedoor cannot do that for this application yet."
# A press that failed for a reason the page does not know: said on the card, never nothing (owner, 2026-11-14).
PRESS_FAILED = "That did not go through. Reload the page and try again."
STEP_UNAVAILABLE_HERE = "This step can't be done inside Sidedoor on this computer yet."   # a LinkedIn card: no posting to point at
TRY_AGAIN_LABEL = "Try again"


def kind(card):
    """What the site asks for, from the wait's reason (its stop code only for a human check)."""
    if card["reason"] == "HUMAN_CHECK" or card["wall"] == "CAPTCHA_REQUIRED":
        return HUMAN_CHECK
    if card["reason"] in access_outcomes.PROFILE_REASONS:
        return PROFILE
    if card["reason"] in SIGN_IN_REASONS:
        return SIGN_IN
    if card["reason"] in CONFIRM_REASONS:
        return CONFIRM
    return OTHER if card["reason"] in access_outcomes.WAIT_REASONS else UNKNOWN


def purpose(card):
    """The user's step a card could offer (`user_step`), or None when it has none."""
    if kind(card) == HUMAN_CHECK:
        return user_step.HUMAN_CHECK
    return user_step.SIGN_IN if card["reason"] in STEP_REASONS or card["reason"] in access_outcomes.PROFILE_REASONS else None


def offers(database, card):
    """(the step a card shows or None, its done button's words or None) for the job that leads it: the
    one rule the card, Applications and the buttons' own checks use. A step only where
    `user_step.start` would open it (C4): on this computer, for a job whose stored address the live
    view opens. A sign-in or a human check has a done button only with its step (a human check never,
    F3). Nothing for a job whose submission may have reached the employer (F2)."""
    if card.get("uncertain"):
        return None, None
    what, step = kind(card), purpose(card)
    opens = bool(step) and user_step.available(step) and user_step.openable(database, card["job_key"])
    # Owner, 2026-11-14 ("Fix": a real card offered only Abandon): a sign-in or human-check card always
    # offers its done button, whatever made the wait and whether or not its step can be opened here. An
    # account wall under "Ask me each time" is a sign-in card too. A passkey site still has none.
    label = DONE[SIGN_IN][0] if card["reason"] in STEP_REASONS else DONE[what][0]
    return (step if opens else None), label


# A deadline that has passed while no round has run yet: the next round closes the application.
PAST_CLOSES = "Closes on Sidedoor's next round."


def deadline_words(moment, zone):
    """A deadline as the cards say it, "Nov 19 at 10:00" (local time): always a date and a time, so it
    reads after "Closes on" and "until" (never "tomorrow 10:00" or a bare "12:24")."""
    local = moment.astimezone(zone)
    return "%s %d at %s" % (local.strftime("%b"), local.day, local.strftime("%H:%M"))


def clock(config=None, now=None, zone=None):
    """(now, zone) for the cards' deadlines: the caller's own where it has them."""
    return (now or dt.datetime.now(dt.timezone.utc),
            zone or ZoneInfo((config or {}).get("timezone") or "America/Los_Angeles"))


def _closes(card, at, acts=True):
    """When a job that met the sign-in or check itself closes (`at`: `clock`). Past its deadline with
    no round run yet: `PAST_CLOSES`, whatever its card offers. Otherwise: "Waits for you until ..., then closes."
    where its card offers a step or a done button (`acts`), else "Closes on ... unless you abandon it
    first." (D8: nothing the user does in Sidedoor ends it; a passkey site always). None for a job
    held behind a site (it is released, never closed) and for one whose submission is uncertain."""
    deadline = None if card["held"] or card.get("uncertain") else overview_page._utc(card["deadline"])
    if deadline is None:
        return None
    if deadline <= at[0]:
        return PAST_CLOSES
    return (CLOSES if acts and card["reason"] != "PASSKEY" else PASSKEY_CLOSES) % deadline_words(deadline, at[1])


def held_words(step=False):
    """What a job held behind a site says: that a sign-in is waiting only where the card it waits on
    really offers Sign in (`step`, D7); otherwise that another application needs the user first."""
    return access_outcomes.HOST_HELD_WORDS if step else HELD_NO_STEP_WORDS


def _need(card, step=False, lead=False):
    """The card's (and Applications') sentence for one waiting job; `step`: its card offers Sign in;
    `lead`: the job leads its card. A held job that leads a card offering Sign in (the job it waited
    behind is being checked, D17) says what any lead with that step says: what the site asks for."""
    if card.get("uncertain"):
        return NOT_ABANDONED
    if card["held"] and lead and step and card["reason"] in access_outcomes.WAIT_REASONS:
        return access_outcomes.WAIT_REASONS[card["reason"]]
    if card["held"]:
        return access_outcomes.held_words(card["reason"])
    return UNKNOWN_WORDS if kind(card) == UNKNOWN else card["words"]


def dom(job_key):
    return "access-" + hashlib.sha256(str(job_key).encode()).hexdigest()[:12]


def _named(card):
    return {"key": card["job_key"], "company": applications_page._text(card["employer"]) or "Unknown company",
            "role": applications_page._text(card["role"]) or "Role not recorded",
            "url": applications_page._safe_url(card["url"])}


def _waiting(reader):
    """The waiting jobs' cards as `access_outcomes.waiting` reads them, one per job, oldest wait
    first, with `held` (the job never met the sign-in itself: it waits behind another job's)."""
    wanted = set(overview_page.waiting_keys(reader))
    if not wanted:
        return []
    import applications
    held = {row["job_key"] for row in reader.rows(
        "SELECT job_key,source FROM %s WHERE state=?" % access_outcomes.TABLE, (access_outcomes.WAITING,),
        access_outcomes.TABLE) if access_outcomes.host_held(row)}
    found = {}
    for card in access_outcomes.waiting(reader.database):
        if card["job_key"] in wanted:
            found[card["job_key"]] = dict(card, held=card["job_key"] in held,
                                          uncertain=applications.uncertain_submission(reader.database, card["job_key"]))
    return list(found.values())


def groups(cards):
    """The cards as the page shows them: [(lead, others)]. Jobs waiting on one site's sign-in for one
    reason share a card, led by the first job that met the sign-in itself; every other wait is alone."""
    found, by_site = [], {}
    for card in cards:
        site = access_outcomes.site(card["host"])
        # Only a reason that holds its site groups (E15.T6): a passkey wait holds nothing, so each such
        # job has its own session and its own card.
        slot = (site, card["reason"]) if site and card["reason"] in access_outcomes.HOLDING_REASONS else None
        if slot in by_site:
            by_site[slot].append(card)
            continue
        members = [card]
        found.append(members)
        if slot:
            by_site[slot] = members
    ordered = []
    for members in found:
        # Never led by a job whose submission is uncertain (F2): its button would do nothing.
        sure = [card for card in members if not card.get("uncertain")]
        lead = next((card for card in sure if not card["held"]), (sure or members)[0])
        ordered.append((lead, [card for card in members if card is not lead]))
    return ordered


# E18: the LinkedIn step waiting on the user (`linkedin_connection.card_due`: no job, no wait row). One card,
# first in the list, with one button: the live view on LinkedIn's sign-in page. No deadline, posting or
# Abandon. Shown only where that step can be shown; elsewhere Overview's LinkedIn line and Settings say it.
LINKEDIN_NAME = "LinkedIn"
LINKEDIN_GONE = "LinkedIn is no longer waiting for you."


def linkedin_card():
    """The LinkedIn step's card as the template draws it, or None."""
    import linkedin_connection
    if not linkedin_connection.card_due() or not user_step.available(user_step.SIGN_IN):
        return None
    key = linkedin_connection.CARD_KEY
    return {"key": key, "company": LINKEDIN_NAME, "role": None, "url": None, "dom": dom(key), "need": LINKEDIN_WORDS,
            "closes": None, "others": [], "done_label": None, "hint": None,
            "step": {"purpose": user_step.SIGN_IN, "label": STEP_LABELS[user_step.SIGN_IN]}, "apply": None,
            "abandon": False, "setting": False, "jobs": 0, "counted": True, "linkedin": True}


def finder_wait(database):
    """The contact finder's wait on the user (`linkedin_contacts.user_wait`), or None."""
    import linkedin_contacts
    return linkedin_contacts.user_wait(database) if database is not None else None


def finder_card(database):
    """The contact finder's one card as the template draws it, or None when LinkedIn asks for nothing.
    The button only where the step can be shown on this computer."""
    import linkedin_contacts
    wait = finder_wait(database)
    if wait is None:
        return None
    step = FINDER_PURPOSES[wait["waiting_on"]]
    offered = {"purpose": step, "label": STEP_LABELS[step]} if user_step.available(step) else None
    return {"key": linkedin_contacts.CARD_KEY, "company": FINDER_NAME, "role": None, "url": None,
            "dom": dom(linkedin_contacts.CARD_KEY), "need": FINDER_WORDS[wait["waiting_on"]], "closes": None,
            "others": [], "done_label": None, "hint": None, "step": offered, "apply": None, "abandon": False,
            "setting": False, "jobs": 0, "counted": True, "finder": True}


# One LinkedIn card (owner, 2026-11-13): whichever need raised it. The contact finder met LinkedIn's sign-in
# page or a human check (`finder_wait`), outreach is disconnected (`linkedin_card`), or both. One sign-in in
# Sidedoor's browser and Done satisfies both: the finder's wait is released (`step_finished`) and outreach is
# connected (`user_step._ended`, `linkedin_connection._signed_in`). One sentence, the action only (a human
# check has its own; if a sign-in is still needed after it, the card comes back with the sign-in sentence).

def one_linkedin_card(database):
    """The single LinkedIn card, or None. The finder's wait decides the step and the key when there is one
    (its page, its wait id); outreach alone uses its own."""
    wait, outreach = finder_wait(database), linkedin_card()
    if wait is None:
        return outreach
    return finder_card(database)


def confirm_cards(database):
    """The cards of applications whose confirming read waits on the user's step (`confirm_read`), as
    the template draws them: one sentence and, where the step can be shown on this computer for the
    job's stored address, its button. Nothing else."""
    import confirm_read
    cards = []
    for wait in confirm_read.waiting(database):
        card_key = confirm_read.key(wait["job_key"])
        step = CONFIRM_PURPOSES[wait["asks"]]
        opens = user_step.available(step) and user_step.openable(database, card_key)
        named = _named(wait)
        cards.append({"key": card_key, "company": named["company"], "role": named["role"], "url": None,
                      "dom": dom(card_key), "closes": None, "need": CONFIRM_WORDS[wait["asks"]],
                      "others": [], "done_label": None, "hint": None, "apply": None, "abandon": False, "setting": False,
                      "step": {"purpose": step, "label": STEP_LABELS[step]} if opens else None,
                      "jobs": 1, "counted": True, "confirm": True})
    return cards


def attended_cards(database, now=None):
    """The cards of applications whose attended submit the user asked for (E16.T2, `attended_submit`): the
    job no longer waits, so its own card is gone; this one holds the view until that session is over. One
    sentence while there is no page yet, and the view loads by itself (`attended`)."""
    import applications
    import attended_submit
    cards = []
    for job, done in attended_submit.cards(database, now or applications.stamp()):
        try:
            payload = json.loads(job["payload"]) or {}
        except (TypeError, ValueError):
            payload = {}
        named = _named({"job_key": job["job_key"], "employer": payload.get("company") or job["employer"],
                        "role": payload.get("role") or payload.get("title"), "url": None})
        cards.append({"key": job["job_key"], "company": named["company"], "role": named["role"], "url": None,
                      "dom": dom(job["job_key"]), "closes": None,
                      # After the user's Done the card offers nothing and says only that it is done (owner, 2026-11-13).
                      "need": live_view.ENDED_WORDS[live_view.DONE] if done else access_outcomes.WAIT_REASONS["HUMAN_CHECK"],
                      "quiet": done,
                      "others": [], "done_label": None, "hint": None, "apply": None, "abandon": False, "setting": False,
                      "step": {"purpose": user_step.HUMAN_CHECK, "label": STEP_LABELS[user_step.HUMAN_CHECK]},
                      "jobs": 1, "counted": True, "attended": True,
                      "warning": None if done else live_view.ATTACHED_WORDS})
    return cards


def card_count(reader):
    """How many cards need the user (a site's group is one): Needs you's number, Approvals' chip, the
    tray's and the notification's (C7, D6). A card whose every job may already be with the employer
    is shown but not counted: nothing there waits on the user (D5). The LinkedIn card (the contact finder's
    wait, outreach disconnected, or both: `one_linkedin_card`) is one, and so is each confirming read's card."""
    import confirm_read
    return (sum(1 for lead, others in groups(_waiting(reader)) if not all(card.get("uncertain") for card in [lead] + others))
            + (1 if one_linkedin_card(reader.database) else 0) + len(confirm_read.waiting(reader.database))
            + len(attended_cards(reader.database)))


def view(reader, config, fmt, limit=None, now=None):
    """What the cards show. `total` is the waiting jobs (each once); `count` is `card_count`; `more` is
    how many of those counted cards are beyond `limit` (D19: the same rule, never an uncertain one). The link to the setting (the module's note): `setting` on each account
    card, `setting_footer` once under the cards when the setting is Skip and no card carries it."""
    setting = access_outcomes.effective_setting(reader.database)
    at = clock(config, now)   # the cards say their deadlines themselves (`deadline_words`), not through `fmt`
    link = SETTING_HREF if setting != FOR_ME else None
    cards = _waiting(reader)
    linkedin = one_linkedin_card(reader.database)
    shown = ([linkedin] if linkedin else []) + confirm_cards(reader.database) + attended_cards(reader.database)
    for lead, others in groups(cards):
        what = kind(lead)
        unsure = bool(lead.get("uncertain"))
        step, label = offers(reader.database, lead)
        offered = {"purpose": step, "label": PROFILE_STEP_LABEL if what == PROFILE else STEP_LABELS[step]} if step else None
        acts = bool(step or label)
        # The line beside the posting link: nothing where the card's own sentence already says it (a
        # passkey site) or where the application may already be with the employer; "also" only on a
        # card that offers something else to do.
        need = _need(lead, step == user_step.SIGN_IN, lead=True)
        apply = (None if unsure or APPLY_YOURSELF_ONLY in need
                 else APPLY_YOURSELF if offered or label else APPLY_YOURSELF_ONLY)
        shown.append(dict(_named(lead), dom=dom(lead["job_key"]), need=need, closes=_closes(lead, at, acts),
                          others=[dict(_named(card), closes=_closes(card, at, acts), abandon=not card.get("uncertain"),
                                       note=NOT_ABANDONED if card.get("uncertain") else None) for card in others],
                          done_label=label, step=offered, apply=apply,
                          # A check on the form itself: the one instruction, shown before the press (E16.T2).
                          warning=(live_view.ATTACHED_WORDS if offered and what == HUMAN_CHECK
                                   and attended_submit.form_check_wait(reader.database, lead["job_key"]) else None),
                          hint=(LINK_HINT if lead["reason"] == "CONFIRMATION_LINK_UNOPENED" else DONE[what][1]) if label else None,
                          resend_label=RESEND_LABEL if not unsure and access_step.can_resend(reader.database, lead) else None,
                          abandon=not unsure,
                          setting=bool(link) and not unsure and lead["reason"] in access_outcomes.HOLDING_REASONS,
                          jobs=1 + len(others), counted=not all(card.get("uncertain") for card in [lead] + others)))
    kept = shown if limit is None else shown[:limit]
    count = sum(1 for card in shown if card["counted"])
    return {"cards": kept, "total": len(cards), "count": count, "more": count - sum(1 for card in kept if card["counted"]),
            "setting_href": link, "setting_label": SETTING_LABEL,
            "setting_words": SETTING_WORDS if access_outcomes.resolver_available() else None,
            "setting_footer": bool(link) and setting == access_outcomes.SKIP and not any(card["setting"] for card in kept),
            "apply_yourself": APPLY_YOURSELF,
            "posting_label": POSTING_LABEL, "also_waiting": ALSO_WAITING, "approvals_href": APPROVALS_HREF}


def behind_keys(reader):
    """The jobs that only wait behind another application's card (held behind its site, not leading a card):
    nothing is asked of the user for them, so Applications lists them under In progress (`by_job`'s `behind`)."""
    return {card["job_key"] for lead, others in groups(_waiting(reader)) for card in others if card["held"]}


def by_job(reader, fmt, now=None, zone=None):
    """What the Applications page says for each waiting job: {job key: {"words", "href", "label"}}.
    A job that met the sign-in or check itself: what is needed and when it closes. A job held behind
    a site's sign-in: `held_words` (it has no deadline of its own). `uncertain`: its submission may
    have reached the employer, so it is not waiting on the user (`NOT_ABANDONED`, no link)."""
    found = {}
    for lead, others in groups(_waiting(reader)):
        step, label = offers(reader.database, lead)   # what the job's card offers decides its words
        for card in [lead] + others:
            closes = _closes(card, clock(None, now, zone), bool(step or label))
            unsure = bool(card.get("uncertain"))
            found[card["job_key"]] = {"words": _need(card, step == user_step.SIGN_IN, card is lead) + (" " + closes if closes else ""),
                                      "held": card["held"], "behind": bool(card["held"]) and card is not lead,
                                      "uncertain": unsure, "href": None if unsure else APPROVALS_HREF,
                                      "label": None if unsure else "Open on Approvals"}
    # An application whose confirming read waits on the user's step (`confirm_read`): the card's own
    # sentences, so Applications never says "Nothing to do." while that card waits. `confirming`: it is
    # not one of the waiting jobs above (it stays in RECONCILE, uncertain) and is not grouped with them.
    for card in confirm_cards(reader.database):
        import confirm_read
        found.setdefault(confirm_read.job_of(card["key"]), {
            "words": card["need"], "held": False, "uncertain": False,
            "confirming": True, "href": APPROVALS_HREF, "label": "Open on Approvals"})
    return found


# --- The buttons ------------------------------------------------------------------------------------

def _locked(database):
    """The write lock, taken before any check (as `access_outcomes` does), inside `with database:`."""
    if not database.in_transaction:
        database.execute("BEGIN IMMEDIATE")


def _has_card(database, job_key):
    """The open wait of a job that has a card, or None: the key a client sends is trusted only here."""
    wait = access_outcomes.open_wait(database, job_key) if job_key else None
    if wait is None:
        return None
    job = database.execute("SELECT state,blocker FROM application_jobs WHERE job_key=?", (job_key,)).fetchone()
    return wait if job and job["state"] == "BLOCKED" and job["blocker"] == wait["wall"] else None


def _free_site(database, host, now):
    """After a wait on `host` ended: every job held behind that site is freed at once when nothing
    holds the site any more, whichever job it was held behind. The test is `access_outcomes.expire`'s
    own (`host_holder`), the rule each applications run applies, so no job goes on saying another
    application is waiting. Inside the caller's locked transaction."""
    site = access_outcomes.site(host)
    held = {row["job_key"] for row in database.execute(
        "SELECT job_key,host,source FROM %s WHERE state=?" % access_outcomes.TABLE, (access_outcomes.WAITING,))
        if access_outcomes.host_held(row) and site and access_outcomes.site(row["host"]) == site}
    if held:
        access_outcomes.expire(database, now, only=held)


def abandon(database, job_key, now):
    """Abandon on a card: the job closes with that reason and keeps Try again. When it was the last
    job holding its site, the jobs held behind that site are freed at once (`_free_site`)."""
    with database:
        _locked(database)
        wait = _has_card(database, job_key)
        if wait is None:
            return {"tone": "warning", "text": GONE}
        import applications
        if applications.uncertain_submission(database, job_key):
            return {"tone": "warning", "text": NOT_ABANDONED}
        if not access_outcomes.abandon(database, job_key, now):
            return {"tone": "error", "text": NOT_CLOSED}
        _free_site(database, wait["host"], now)
        return {"tone": "success", "text": ABANDONED}


def done(database, job_key, now):
    """"I've signed in" (or the check is done): the wait is released and the job is due again, and so
    is every job on the same card (the jobs waiting on that site's sign-in). Only where the card
    offers the button (`offers`), and never a job whose submission may have reached the employer
    (`applications.uncertain_submission`, the test Abandon uses): that one stays as it is."""
    import applications
    with database:
        _locked(database)
        wait = _has_card(database, job_key)
        if wait is None:
            return {"tone": "warning", "text": GONE}
        members = next(([lead] + others for lead, others in groups(_waiting(overview_page.Reader(database)))
                        if job_key in [card["job_key"] for card in [lead] + others]), [])
        if not members or offers(database, members[0])[1] is None:   # the card's own rule, `openable` included (D14)
            return {"tone": "warning", "text": NOT_OFFERED}
        # `resume` releases the card's other jobs too, and never one whose submission is uncertain.
        # The rest of the card is released even when the job that leads it is the uncertain one.
        if not applications.uncertain_submission(database, job_key):
            access_step.after_confirmed(database, wait, now)   # E15.T8: "I've confirmed it": Sidedoor signs in next
        released = access_outcomes.resume(database, job_key, now)
        stays = sum(1 for card in members if card["job_key"] not in released
                    and applications.uncertain_submission(database, card["job_key"]))
        if not released:
            return {"tone": "warning", "text": NOT_ABANDONED if stays else GONE}
        if wait["reason"] in STEP_REASONS:   # "I've signed in": the user's own sign-in, stored with its site
            access_step.user_signed_in(database, job_key, access_outcomes.site(wait["host"]), now)
        _free_site(database, wait["host"], now)
        text = RELEASED_ONE if len(released) == 1 else RELEASED_MANY % len(released)
        if stays:
            text += " " + (STAYS_ONE if stays == 1 else STAYS_MANY % stays)
        return {"tone": "success", "text": text}


def resend(database, job_key, now):
    """Resend on a "confirmation email did not arrive" card (E15.T8): Sidedoor's next step for the job
    presses the site's own resend and looks for the email again; the job leaves the card meanwhile and
    comes back to it if the email still does not arrive. Only where the card offers it."""
    import applications
    with database:
        _locked(database)
        wait = _has_card(database, job_key)
        if wait is None:
            return {"tone": "warning", "text": GONE}
        if applications.uncertain_submission(database, job_key):
            return {"tone": "warning", "text": NOT_ABANDONED}
        if not access_step.can_resend(database, wait):
            return {"tone": "warning", "text": NOT_OFFERED}
        access_step.resend(database, wait, now)
        if not access_outcomes.resume(database, job_key, now, siblings=False):
            return {"tone": "warning", "text": GONE}
        return {"tone": "success", "text": RELEASED_ONE}


def step_finished(database, job_key, purpose, now, session_saved=None, wait_id=None):
    """The user finished their step in the live view (pressed Done there; `user_step._ended`): the wait
    is released as "I've signed in" releases it. A sign-in releases every job waiting on that site's
    account (`access_outcomes.resume`), a human check that job only; then the jobs held behind the site
    are freed (`_free_site`). Never a job whose submission may have reached the employer
    (`access_outcomes._release` refuses it: its wait and card stay). A job with no card changes
    nothing, and neither does a step that was started for another wait than the one now open
    (`wait_id`, C6: a late Done never releases a newer wait). One saved transaction; the record of it
    holds counts only. Returns the released job keys."""
    import applications
    import linkedin_contacts
    if job_key == linkedin_contacts.CARD_KEY:
        # The contact finder's card: its waiting runs are released (only the wait the step was started
        # for), and nothing of an application is touched.
        with database:
            _locked(database)
            return [job_key] if linkedin_contacts.release_user_wait(database, wait_id) else []
    import confirm_read
    if confirm_read.job_of(job_key):
        # The confirming read's card: the job's next READ is made due and nothing else is changed
        # (`confirm_read.done`). No wait is released: the job stays uncertain and in RECONCILE.
        with database:
            _locked(database)
            job = confirm_read.job_of(job_key)
            due = confirm_read.done(database, job, now, wait_id)
            if due:
                applications.event(database, job, user_step.FINISHED_EVENT,
                                   {"purpose": purpose, "site_session_saved": bool(session_saved), "released": 0,
                                    "confirming_read_due": True}, now)
            return [job_key] if due else []
    with database:
        _locked(database)
        wait = _has_card(database, job_key)
        if wait is None or (wait_id is not None and wait["id"] != wait_id):
            return []
        released = access_outcomes.resume(database, job_key, now, siblings=purpose == user_step.SIGN_IN)
        if released:
            _free_site(database, wait["host"], now)
            if purpose == user_step.SIGN_IN and wait["reason"] in STEP_REASONS:
                access_step.user_signed_in(database, job_key, access_outcomes.site(wait["host"]), now)
        applications.event(database, job_key, user_step.FINISHED_EVENT,
                           {"purpose": purpose, "site_session_saved": bool(session_saved), "released": len(released)}, now)
        return released


def try_again(database, job_key, now):
    """Try again on a job this feature closed. `access_outcomes.try_again` is the whole test (closed
    behind a sign-in or check, still on that reason, and no submission that may have reached the
    employer); its refusal is shown as it words it and changes nothing."""
    with database:
        result = access_outcomes.try_again(database, str(job_key or ""), now)
    if result.get("reopened"):
        return {"tone": "success", "text": REOPENED, "reopened": True}
    return {"tone": "warning", "text": result.get("words") or access_outcomes.TRY_AGAIN_REFUSED["NOT_CLOSED"], "reopened": False}


def step_refusal(database, job_key):
    """Why `step` showed nothing, in the words the page says: the job no longer waits, it may already
    be with the employer, or this computer cannot show the step."""
    import applications
    import linkedin_connection
    import linkedin_contacts
    if job_key == linkedin_contacts.CARD_KEY or (job_key == linkedin_connection.CARD_KEY and finder_wait(database) is not None):
        return FINDER_GONE if finder_wait(database) is None or user_step.finishing(linkedin_contacts.CARD_KEY) else STEP_UNAVAILABLE_HERE
    if job_key == linkedin_connection.CARD_KEY:
        return LINKEDIN_GONE if linkedin_card() is None else STEP_UNAVAILABLE_HERE
    import confirm_read
    if confirm_read.job_of(job_key):
        gone = confirm_read.wait(database, confirm_read.job_of(job_key)) is None or user_step.finishing(job_key)
        return CONFIRM_GONE if gone else STEP_UNAVAILABLE_HERE
    if _has_card(database, job_key) is None or user_step.finishing(job_key):
        return GONE   # also a step just finished, while its ending is still being recorded (D22)
    if applications.uncertain_submission(database, job_key):
        return NOT_ABANDONED
    cards = [card for card in access_outcomes.waiting(database) if card["job_key"] == job_key]
    wanted = purpose(cards[-1]) if cards else None
    if cards and cards[-1]["reason"] == "PASSKEY" and cards[-1]["words"] == access_outcomes.WAIT_REASONS["PASSKEY"]:
        return PASSKEY_PRESS   # D11: never "on this computer yet" for a site no computer can do it on
    if wanted and user_step.available(wanted) and not user_step.openable(database, job_key):
        return NOT_OPENABLE
    return STEP_UNAVAILABLE


def step(database, job_key, at=None, poll=False):
    """"Sign in" / "Do the human check": the fragment `user_step.start` returns for a job that has a
    card, or None when the job has no card or the step cannot be shown."""
    import linkedin_connection
    import linkedin_contacts
    if job_key in (linkedin_contacts.CARD_KEY, linkedin_connection.CARD_KEY):
        # The one LinkedIn card. While the contact finder waits, the step is the finder's (its page and
        # its wait), whichever key the page was drawn with; outreach alone opens its own.
        wait = finder_wait(database)
        if wait is None:
            return linkedin_connection.sign_in_view() if job_key == linkedin_connection.CARD_KEY and linkedin_card() else None
        wanted = FINDER_PURPOSES[wait["waiting_on"]]
        result = (user_step.start(linkedin_contacts.CARD_KEY, wanted) or {}) if user_step.available(wanted) else {}
        return result.get("html") if result.get("available") else None
    import confirm_read
    if confirm_read.job_of(job_key):
        wait = confirm_read.wait(database, confirm_read.job_of(job_key))
        wanted = CONFIRM_PURPOSES[wait["asks"]] if wait else None
        result = (user_step.start(job_key, wanted) or {}) if wanted and user_step.available(wanted) else {}
        return result.get("html") if result.get("available") else None
    import applications
    # E16.T2: a human check on the form itself is done while the submit session runs (attended_submit): the
    # press asks for that session, and the card shows it. Never with an uncertain submission.
    import attended_submit
    # A job whose attended submit was asked for keeps its place by that REQUEST, not by the wait the press
    # released: once its session runs the job is SUBMITTING (an uncertain submission by `applications`' rule),
    # and the card must still show that session (first real sitting, 2026-11-13: it said "no longer waiting").
    if (attended_submit.live(database, job_key) or (poll and attended_submit.over(database, job_key))
            or not applications.uncertain_submission(database, job_key)):
        attended = attended_submit.step(database, job_key, applications.stamp(), at=at, poll=poll)
        if attended is not None:
            return attended
    cards = [card for card in access_outcomes.waiting(database) if card["job_key"] == job_key]
    if not cards or _has_card(database, job_key) is None or applications.uncertain_submission(database, job_key):
        return None
    wanted = purpose(cards[-1])
    if wanted is None or not user_step.available(wanted):
        return None
    result = user_step.start(job_key, wanted) or {}
    return result.get("html") if result.get("available") else None


def can_try_again(state, code):
    """A closed job the Applications page offers Try again on: one this feature closed."""
    if state == "BLOCKED" and applications_page.code_root(code) == overview_page.PARKED_CODE:
        return True   # parked with its reads spent: never retried on its own, so the user can ask (owner, 2026-11-14)
    return state in ("ABANDONED", "EXHAUSTED") and applications_page.code_root(code) in access_outcomes.CLOSE_CODES
