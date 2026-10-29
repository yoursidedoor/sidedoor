# Sidedoor

Job seekers today send hundreds of applications and hear back from almost none, while most good
hires still come through a referral from someone inside the company. Sidedoor closes that gap. It is
a job-search assistant that works every day on the job seeker's behalf: it finds the companies and
roles worth their time, introduces them to the people who can refer them, keeps those conversations
going and submits the applications that matter. The result is more interviews from fewer, better
opportunities, without the hours of manual searching, messaging and form filling. Unlike mass
auto-apply tools, Sidedoor is built around relationships and quality, protects the job seeker's
reputation, and keeps them in control of everything sent in their name.

Sidedoor is the referral-first job-search agent, and it runs on your machine. It
researches companies in the industries you target, finds the open roles that fit your profile,
finds people at those companies who can refer you and reaches out to them on LinkedIn, answers
their replies, and applies to the roles for you, with guardrails against contacting anyone twice
or applying twice. Install from source with `pip install -e .` (Python 3.13), then run
`sidedoor <command>` (`sidedoor --help` lists them; `sidedoor run` is one hourly cycle). Your
profile lives at `~/.config/sidedoor/profile.json` (or `$SIDEDOOR_HOME/profile.json`): start by
copying `profile.example.json` there and editing it. For one release the pre-rename
`$JOBAGENT_HOME`, `$JOBAGENT_OWNER_SEED` and `~/.config/jobagent` are still read when the new
names are not set.

Source available under the Business Source License 1.1; see LICENSE.

## Job search

Local launchd orchestrator for the owner's existing job-search pipeline.

## Contextual replies

The interpreter receives the designated full five-page resume, original verified
invitation when available, and eight recent messages; it does not change application
attachments. `reply_execution.py` promotes source-bound proposals to the existing
outreach queue. A fresh AI assessment checks context and grounded wording; shared
claims, exclusions, permanent closure and final account/thread checks guard the
HeyReach request. `REPLY` is explicitly enabled separately from initial messages.
Unknown provider quota stays unknown and provider-enforced, never fabricated as
remaining allowance. No upgrade or alternate sending account is permitted.
Acknowledged or uncertain sends require independent receipt reconciliation and
cannot be resent. Safe reporting distinguishes proposals, queued actions and
confirmed sends.

**How the pipeline behaves and its execution gates:** see the module docstrings
(`orchestrator.py` first). Passing mocked
tests or creating a draft does not establish unattended application execution.

## Actual schedule

Sidedoor is one process (`sidedoor serve`, E5.T1): the dashboard plus a standard-library scheduler loop (`scheduler_loop.py`) that starts the hourly cycle at :00, applications at :30 and the research tick every 300 seconds, each as a child process of the same program. Start at login (Setup step 10, Settings > Start at login; `login_item.py`, E5.T2) registers one entry per OS that runs `sidedoor serve --start --no-browser`: a LaunchAgent `com.yoursidedoor.sidedoor` on macOS and a Task Scheduler task `Sidedoor` on Windows (Linux comes later). The OS restarts it after a crash, never after a deliberate Quit (exit 0). The product installs no other launchd job; the old three-job `install.py` is gone.

The notes below describe the earlier launchd schedule and are kept as history.

The Mac's system timezone was verified as America/Los_Angeles. launchd calendar triggers use the system timezone; the process's `TZ` environment does not configure launchd. Research eligibility is independently calculated with Python `ZoneInfo("America/Los_Angeles")`, including daylight saving changes. If the system timezone changes, the five-minute dispatcher still uses Pacific eligibility, with up to five minutes of dispatch delay. While another worker holds the shared lock, execution waits for a later trigger; the retry dispatcher recovers missed research dispatches.

Calendar events missed during sleep coalesce into one trigger after wake. The research worker selects only the most recent due Pacific date, never a backlog of every missed day. Successful dates do not run again. A pre-08:00 wake can catch up the previous due date. Nothing runs while the Mac is powered off or logged out; login performs the same due check.

Each due date permits at most three attempts: the initial attempt, then retries after 15 minutes and 60 minutes from the respective failed completion, dispatched on the next five-minute check. Each research attempt has a ten-minute timeout. Failed, partial, timed-out, and interrupted attempts consume retry budget but never record daily success. A zero process exit alone is insufficient: the worker must report substantive research COMPLETE, validate its output, and persist results. Exhausted days remain visibly `EXHAUSTED`; the next due day starts a new bounded budget. Existing completed research is migrated without deleting history.

## Outreach enablement and current gate

The guarded outreach dispatcher is enabled in `guarded` mode. The deployed bridge's business executor is retired, and the existing cloud LinkedIn Pipeline Report was paused and verified in Scheduled. Ownership evidence is in the original install's `ownership-reconciliation` report. The hourly dispatcher can start a prepared, single-recipient connection-request campaign only after all checks pass. Research itself has no provider credentials, sending tools, shell tools, or user-configured plugins. Research drafts do not automatically satisfy verification requirements.

Beyond migration ownership, the current recipient gate is **no verified, uncontacted recipient with a prepared single-contact campaign**. A historical contact's exact hiring-team relevance is unverified. The legacy campaign contact has already received the legacy campaign's invitation and must never be resent as a validation test. A hand-messaged contact's lead is absent from HeyReach; this does not negate his preserved LinkedIn message history.

Before launch, the dispatcher requires:

- Verified recipient identity and team relevance, with sources.
- Our own local contact records, checked within one hour: no prior outreach, pending invitation, reply, or negative reply (the LinkedIn browser history check was removed on 2026-09-29).
- Existing exclusions and the maximum three unique company contacts, conservatively counting known recruiting contacts.
- Live provider reconciliation: the expected sender, valid authentication, no cooldown, one matching lead, a DRAFT/unstarted campaign, zero prior delivery, no other campaign or conversation history, and all provider duplicate exclusions enabled.
- Exactly the prepared connection-request note, no automatic follow-up, and no automatic withdrawal.

The database records a durable claim and recipient suppression before the network request. The transport consumes that claim before launch. A lost acknowledgement, crash, or ambiguous provider error is never automatically retried. A launch acknowledgement is not proof of delivery. The legacy campaign contact's already-sent campaign is seeded as a consumed action. There is no campaign-creation, email, or automatic-follow-up path. Telegram is reserved for owner notifications and scoped factual decisions, not employer outreach.

To register a prepared campaign with independently verified evidence:

```sh
sidedoor enqueue-campaign /path/to/verified-campaign.json
sidedoor validate
sidedoor outreach
```

The JSON requires `recipient`, `company`, `profile`, `message`, `campaign_id`, and `verification`. Verification requires `identity_verified`, `team_relevance_verified`, `identity_source`, `team_source`, `linkedin_messages_checked` and `linkedin_sent_invitations_checked` (recorded from the local-records history proof since 2026-09-29), `prior_outreach`, `pending_invitation`, `has_reply`, `negative_reply`, timezone-aware `verified_at`, and `history_evidence`. Record genuine checks; never synthesize a passing evidence record. No further blanket authorization is required: the remaining gates are factual verification and campaign preparation.

## Preservation and operations

Original tracker/evidence files and the hosted database are preserved. `backups/manifest.json` records the source paths and SHA-256 hashes. Immutable snapshots are verified at startup. A fresh checkout with no `backups/manifest.json` and `source_root` null in `config.json` has nothing to import and starts with an empty history (`sidedoor init` says so); a `source_root` that is set but unreadable still stops startup with the error. `state/orchestrator.sqlite3` supplements the hosted tracker and is backed up before installer migrations. All ten historical contacts remain suppressed. The original cloud workbook and any separate ChatGPT cloud schedule are not controlled by this service; the hosted database is not automatically synchronized.

The shared kernel file lock prevents overlapping local workers. Transactional daily claims prevent duplicate research across calendar, interval, login, and manual triggers. Hourly monitoring retains durable slot claims.

From the root of your Sidedoor checkout, run `sidedoor setup` and tick "Start Sidedoor when I log in" in step 10 (or later in Settings > Start at login); `sidedoor status` shows the pipeline.

Read `reports/latest.json` for monitoring, `reports/research-status.json` for research dispatch, and `reports/validate.json` or `reports/outreach.json` for explicit sending gates. Research outputs and process logs remain under `reports/` and `logs/`. Provider credentials are read from the existing Codex config without being copied into this project. Logs are retained; archive old logs as needed.

To stop starting at login, untick the box in Settings > Start at login; records and logs are kept.

## Validation

```sh
/opt/homebrew/bin/python3.13 -m unittest discover -s tests -v
```

Tests exercise DST, exact 08:00 eligibility, multi-day wake catch-up, concurrent claims, success versus failure, bounded retry/backoff, crash recovery, real subprocess output ingestion, restart deduplication, source preservation, outreach preflight, and uncertain sends. Sending tests use a fake provider; they do not prove new live delivery. Live validation performs read-only reconciliation of the existing campaign and reports unresolved gates without repeating outreach.

Codex research integration follows [official non-interactive mode documentation](https://learn.chatgpt.com/docs/non-interactive-mode) and [configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).
# Guarded AI message connection — September 17, 2026

Explicit outreach closure is permanent for the canonical recipient and linked
aliases, independent of message delivery state. Use `orchestrator.py close-outreach
--profile LINKEDIN_PROFILE --reason REASON` or a JSON file containing `profile`,
`reason`, `source` and optional timezone-aware `closed_at`. The operation uses the
runner lock, records a durable terminal lifecycle, stops unsent work and suppresses
future outbound contact. It does not withdraw invitations, unsend messages, resolve
uncertain delivery or erase history. Replies and acceptance cannot reopen it.
No automatic age-based closure is configured: neither timeout X nor its starting
point has been specified. No existing recipients are closed by installation.

The reply decision worker receives the current inbound message, up to eight other
recent authenticated messages from both sides of that account/thread, the original
invitation when its sent content is available from verified saved evidence, and
the existing role/referral context. The invitation is separate from the eight-message
window. There is no older-history summary or automatic history fetch. Missing
invitation content remains unknown; a prepared draft is not a sent invitation.
The bounded prompt limit is 64 KiB so eight real normalized provider messages plus
the invitation fit without summaries. Oversized inputs are held rather than silently
truncated. Reply proposals remain unsent drafts, not delivery evidence. An invitation
must be explicitly identified in the authenticated observation: `inbox.message_type`
is `invitation`, bound to the same raw value by `inbox.raw_paths.message_type`.
An ordinary earliest outbound message is not assumed to be an invitation. Current
saved inbox data has no such invitation markers; historical tracker invitation
notes without a sender-account binding remain unavailable to this worker.

An authenticated reply can be interpreted before a verified job match exists.
An empty `job_scope` forces a null role reference; it cannot establish a referral
or authorize an application/send. Old pre-input role-scope failures are resumed
once using their original task identity, failed attempt, deadline and retry limit.
Known exhausted shared capacity defers new attempts until the next service day (09:00 America/Los_Angeles),
without spending retries. `reply_decisions` reports AI labels, missing facts and
unsent-draft availability; exact source text and drafts stay in the private audit
tables, not Telegram. Read-only interpretation is not message execution.

HeyReach-first capability verification is recorded in
the original install's `heyreach-first` report. The existing account's active trial and full-inbox
sync setting were observed without changing settings or purchasing anything. Full
inbox imports only the documented recent-history window; it is not all-time history
clearance. The existing collector now resolves an empty profile lookup through
HeyReach's `get_lead` and retries by an exact-profile-bound LinkedIn ID, before
declaring the conversation absent. This does not invent a thread or bypass history.

Known shared-compute exhaustion defers unclaimed draft investigations until the next
service day's capacity window (09:00 America/Los_Angeles), without spending further attempts or provider
reads. Original failures, attempt counts, deadlines and quota reservations remain.
Reports retain the specific capacity blocker and next eligible time; a capacity hold
is neither a successful investigation nor permission to resend an uncertain action.

`outreach_agent.py` connects draft inspection, the existing message queue, guarded
HeyReach transport and independent receipt reconciliation. The existing subscription
agent interprets authenticated observations; it cannot send from its inference step.
`outreach_actions_enabled`, `outreach_agent.enabled` and explicit
`outreach_allowed_purposes` govern the message route. Only `INITIAL` and
`FIRST_MESSAGE` are supported here. Campaign creation, email and automatic follow-ups
are not enabled by these settings; the existing prepared-invitation dispatcher is
separate. A matching authenticated conversation and all existing action gates are
required. R13 preapproval is not proof of technical sending entitlement.

The durable claim and single-use provider request marker prevent overlapping or
restarted workers from repeating transport. AI evaluates independently read messages;
code binds any receipt to the exact account, thread, content and issued-request time.
Acknowledgments are not delivery. Unknown outcomes retain the claim and bounded
read-only reconciliation (three attempts, 15/60-minute backoff). An explicit terminal
not-sent resolution can be recorded by the guard, but the current provider has no
supported terminal-rejection reader or automatic resend path. Absence is not rejection.

Draft investigations use the same durable task/compute budget, three attempts and a
24-hour deadline. Expired successful proofs are rechecked within that original budget;
old evidence and failures remain in task events. Contact history comes from our own
local records (owner, 2026-09-29: the LinkedIn browser check was removed); route-specific
entitlement still comes from the live provider.
`reporting.message_executor_gate` exposes saved blockers and retries without claiming
a live check. No sender, receipt or full-production readiness is implied by enablement.

# Current scope — September 17, 2026

Latest user clarification: **continue the full revision-2 E2E implementation**.
The intervening monitor-only instruction was explicitly retracted before any
launchd service was unloaded. Hourly monitoring and daily research remain in
scope. The application queue and worker are enabled, with ten preserved roles
currently blocked: five first require a configured production ATS/browser adapter,
three have newly discovered employer history requiring requisition matching, and
two first require canonical requisition resolution. No live application has been
attempted. The command-adapter contract and mocked submissions are not a working
production ATS integration. Existing records and sending gates are preserved.
No power settings are changed.

## Browser execution: not operational yet

Reuse of the existing desktop browser is preferred over rebuilding browser control.
A read-only visit to the real Virtucon application form succeeded through Browser
Use, without filling fields, uploading a resume, or submitting. The missing piece
is a verified unattended invocation from launchd into a desktop session, plus
live independent submission verification.
The CLI's documented app-server control socket is absent; CLI queue help alone
does not establish access to desktop browser tools. Desktop scheduled tasks are
a documented alternative, but none was created or validated for this worker.

`ats_browser.py` and its isolated Playwright installation are paused prototypes,
not production adapters. `application_adapter_command` remains unset. See
the original install's `desktop-browser-reuse` report. AiApply is independent and must not
be accessed or changed for this project.

The opt-in `desktop_adapter.py` now connects the existing worker to a durable
desktop request/result mailbox; it does not implement or launch another browser.
`application_adapter: "desktop"` selects it, but production configuration has not
been changed for testing. `docs/desktop-worker-prompt.md` (not bundled) defines the supported consumer
protocol. Claims bind the job, policy, form, answers and attempt; pre-click intent
is consumed once, stale results are rejected, and uncertain submissions reconcile.
Waiting for the browser does not consume retry budget, but requests and retries
are bounded. A stale-worker race that could create a second attempt is fixed.

Validation: 118 tests, 117 passed and one skipped, including 36 new desktop handoff
and safety tests. An isolated real read-only browser round trip committed its
observation back to the worker with zero submissions. This was foreground, not a
scheduled desktop invocation. See the original install's `desktop-handoff-e2e` report.

Latest validation: `reports/tests-application-readiness.log` contains 138 tests,
137 passed and one skipped. Completed jobs now reject stale workers and delayed
results, including errors that previously could overwrite SUBMITTED. Daily reports
expose the missing application executor and guarded-outreach prerequisites.

## Independent employer history

`external_history.py` appends authenticated employer-email observations without
overwriting the original tracker or adopting email aliases as candidate answers.
Six real employer records were reconciled from the authorized Gmail connector,
not from AiApply. Virtucon, Wernham and Sirius require historical requisition
matching; a closed past process is not labeled an active interview or an exact
duplicate of a different role. Unrelated companies continue through their gates.

Imports default to dry-run; `--apply` acquires the existing runner lock and commits
idempotently. Unknown requisitions remain null, never inferred from a generic
confirmation. A sourced SAME_ROLE/DISTINCT_ROLE resolution is scoped to the exact
evidence/job pair. It does not clear every job at that company or prove absence
of other applications/referrals. Details and preservation evidence are in
the original install's `application-readiness` report.

## Telegram transport

The existing Site was deployed using Sites management without requesting Site MCP.
The Mac uses a scoped HTTPS credential in a user-owned mode-0600 file; the existing
Telegram bot token remains encrypted in the cloud. The relay persists notification
intent and receipt, deduplicates updates, and acknowledges events only after local
commit. Neither callbacks nor text replies execute cloud business actions.

The client identifies itself as `job-search-orchestrator/1.0`; the host rejects
Python's default User-Agent with Cloudflare error 1010. Authentication and redirect
protection remain unchanged. Webhook migration keeps the existing destination and
secret, enables message replies alongside callbacks, and never drops pending updates.

`orchestrator.py transport-test` reuses one harmless pending test rather than
sending repeated messages. Reply `transport-ok` to that exact Telegram message.
The decision must become `TEST_CONFIRMED` and the inbox event acknowledged before
claiming a live round trip. See `reports/transport-test.json` and
`reports/revision-2-e2e-acceptance.md` for evidence and remaining production gates.
