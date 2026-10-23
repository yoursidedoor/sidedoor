"""The owner-reopen grant tables, as the owner's reopen tool created them (that tool is not part
of the product, E2.T3 decision 4). The runtime still reads grants already on file
(`application_recovery.reopen_scope`); tests build them with this schema."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS application_reopen_grants (
    authorization_id TEXT PRIMARY KEY, authorized_by TEXT NOT NULL, job_keys TEXT NOT NULL,
    investigation_claims INTEGER NOT NULL, deadline_hours REAL NOT NULL,
    authorized_at TEXT NOT NULL, expires_at TEXT NOT NULL, authorization TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS application_reopen_grants_no_update BEFORE UPDATE ON application_reopen_grants
    BEGIN SELECT RAISE(ABORT, 'Immutable reopen authorization'); END;
CREATE TRIGGER IF NOT EXISTS application_reopen_grants_no_delete BEFORE DELETE ON application_reopen_grants
    BEGIN SELECT RAISE(ABORT, 'Immutable reopen authorization'); END;
CREATE TABLE IF NOT EXISTS application_reopen_grant_jobs (
    authorization_id TEXT NOT NULL, job_key TEXT NOT NULL, reopened_at TEXT NOT NULL,
    reopen_event_id INTEGER NOT NULL, defect_classes TEXT NOT NULL, original_abandoned_at TEXT NOT NULL,
    original_reason TEXT NOT NULL, original_attempts INTEGER NOT NULL,
    deadline TEXT NOT NULL, PRIMARY KEY(authorization_id,job_key));
CREATE TRIGGER IF NOT EXISTS application_reopen_grant_jobs_no_update BEFORE UPDATE ON application_reopen_grant_jobs
    BEGIN SELECT RAISE(ABORT, 'Immutable reopen consumption'); END;
CREATE TRIGGER IF NOT EXISTS application_reopen_grant_jobs_no_delete BEFORE DELETE ON application_reopen_grant_jobs
    BEGIN SELECT RAISE(ABORT, 'Immutable reopen consumption'); END;
"""
