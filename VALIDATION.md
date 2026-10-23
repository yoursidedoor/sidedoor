# Schedule and outreach validation

This is a preserved earlier checkpoint. How the pipeline behaves now is described in
the module docstrings.
Only a deployment receipt establishes installation of the reviewed runtime.

Verified September 16, 2026.

## Installed triggers

The original loaded launch agent had only `RunAtLoad` and an hourly `Minute=0` calendar trigger. Research was a rolling 24-hour application check, not a separate daily launchd trigger. Its failed attempts also incorrectly delayed research for 24 hours.

Both installed plist files now match the tested definitions exactly. `launchctl print` independently confirms:

- Monitoring: `com.sidedoor.orchestrator`, `Minute=0`, unchanged.
- Research: `com.sidedoor.research`, `Hour=8`, `Minute=0`, `run interval=300 seconds`, `RunAtLoad`.
- (Historical. Since E5.T1/E5.T2 the product runs one process, `sidedoor serve`, whose scheduler loop starts these
  jobs; start at login registers it as `com.yoursidedoor.sidedoor`. `install.py` and the `com.sidedoor.*` jobs are gone.)
- Research's first installed dispatch exited 0 and reused the already completed September 16 daily record; it did not run another research attempt.
- Next scheduled research: September 17, 2026, 08:00 PDT.

The host timezone is America/Los_Angeles. The runtime additionally calculates due dates in that zone. Five-minute checks dispatch bounded retries and recover lock contention; they are not additional research runs. Only the latest due date is eligible after sleep. Failed/partial/interrupted attempts never set daily success. There are at most three attempts per due date, with 15-minute and 60-minute backoffs after failures.

## Validation coverage

All 33 tests passed. Coverage includes concurrent daily claims, DST, pre/post-08:00 eligibility, multi-day wake catch-up, interrupted attempts, backoff/exhaustion, a real subprocess writing structured output, persistence across restart, partial output despite exit 0, immutable source snapshots, and single-use outbound claims with lost acknowledgements.

Wake and clock-boundary cases were simulated; the Mac was not physically put to sleep. New outbound calls were tested against a fake provider, not by sending an unnecessary live message. The first real Codex research cycle had already completed before this schedule change. Production research attempt count remains one.

Live read-only validation is saved in `reports/validate.json`:

- Sender <sender account id> is authenticated and active, with no connection/request-note cooldown.
- The legacy campaign remains IN_PROGRESS; provider totals still show exactly one connection sent and one unique contacted lead, zero direct messages/InMails, and zero acceptance.
- The legacy campaign contact's sequence still contains one connection request with both branches ending, no follow-up, and no automatic withdrawal.
- A hand-messaged contact's exact lookup reports that the lead does not exist in HeyReach. His existing LinkedIn outbound history remains preserved; this is not permission to resend or proof that he has no reply.

## Authorized sending status

The guarded dispatcher is enabled and was invoked in non-validation mode. It returned BLOCKED with zero launch attempts. The exact unresolved gate is:

`NO_VERIFIED_UNCONTACTED_RECIPIENT_WITH_PREPARED_SINGLE_CONTACT_CAMPAIGN`

A historical contact is the remaining known candidate, but his exact hiring-team relevance and fresh direct LinkedIn message/sent-invitation history are not verified. There is no prepared eligible campaign in the local queue. The legacy campaign contact is already sent and permanently excluded from repeat launch. No new blanket authorization is being requested; the remaining requirements are factual verification and campaign preparation.

The dispatcher supports only prepared, single-contact initial connection-request campaigns. It rechecks provider history, sender, campaign contents, exclusions, company limits, and fresh direct LinkedIn evidence, then durably reserves the recipient before its one launch request. A crash or uncertain acknowledgement cannot trigger an automatic repeat. A provider launch acknowledgement is recorded separately from actual delivery.

## Preservation

All original tracker/evidence source hashes still match the manifest. All ten prior contacts remain in the local history. SQLite integrity check returned `ok`. Database snapshots were taken before upgrade; the existing hosted tracker was not reset or modified. The only production outbound ledger entry is the legacy campaign contact's historical `ALREADY_SENT` action. New live send attempts: zero.
