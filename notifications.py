import contextlib
import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request

import applications
import decision_transport
import portable


class Unavailable(RuntimeError):
    pass


class Transient(RuntimeError):
    def __init__(self, retry_after=300):
        self.retry_after = max(1, min(int(retry_after), 86400))


class Terminal(RuntimeError):
    pass


class Relay:
    def __init__(self, config):
        self.bridge = decision_transport.Bridge(config.get("bridge_transport", {}))

    def result(self, receipt):
        if receipt.get("state") == "DELIVERED" and receipt.get("message_id") and receipt.get("chat_id"):
            return {"provider": "telegram", "message_id": receipt["message_id"], "chat_id": receipt["chat_id"]}
        if receipt.get("state") == "PENDING":
            raise Transient(receipt.get("retry_after") or 300)
        if receipt.get("state") == "FAILED":
            raise Terminal("RELAY_CONFIRMED_FAILED")
        state = receipt.get("state") if isinstance(receipt, dict) else None
        if isinstance(state, str) and re.fullmatch(r"[A-Z][A-Z_]{0,31}", state):
            raise RuntimeError("RELAY_SEND_UNCERTAIN_" + state)
        raise RuntimeError("RELAY_SEND_UNCERTAIN")

    def send_notification(self, row):
        identifier = hashlib.sha256(row["event_key"].encode()).hexdigest()
        try:
            receipt = self.bridge.call("notifications", {"id": identifier, "message": row["message"], "payload_hash": hashlib.sha256(row["message"].encode()).hexdigest()})
        except decision_transport.PreSendUnavailable:
            raise Unavailable("BRIDGE_PRESEND_UNAVAILABLE") from None
        return self.result(receipt)

    def reconcile_notification(self, row):
        identifier = hashlib.sha256(row["event_key"].encode()).hexdigest()
        return self.result(self.bridge.call("notifications?id=" + identifier))


class Telegram:
    def __init__(self, config):
        self.config = config

    def send(self, message):
        settings = self.config.get("telegram", {})
        if not all(settings.get(field) for field in ("keychain_service", "keychain_account", "chat_id")):
            raise Unavailable("TELEGRAM_SECURE_CREDENTIAL_AND_CHAT_NOT_CONFIGURED")
        try:
            result = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", settings["keychain_service"], "-a", settings["keychain_account"], "-w"], capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            raise Unavailable("TELEGRAM_KEYCHAIN_CREDENTIAL_UNAVAILABLE") from None
        if result.returncode or not result.stdout.strip():
            raise Unavailable("TELEGRAM_KEYCHAIN_CREDENTIAL_UNAVAILABLE")
        token = result.stdout.strip()
        if any(character.isspace() for character in token):
            raise Unavailable("TELEGRAM_KEYCHAIN_CREDENTIAL_UNAVAILABLE")
        request = urllib.request.Request("https://api.telegram.org/bot" + token + "/sendMessage",
                                         data=json.dumps({"chat_id": settings["chat_id"], "text": message, "protect_content": True}).encode(),
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.load(response)
            sent = payload.get("result", {})
            if payload.get("ok") is not True or str(sent.get("chat", {}).get("id")) != str(settings["chat_id"]) or not sent.get("message_id"):
                raise ValueError("Invalid Telegram acknowledgment")
            return {"provider": "telegram", "chat_id": str(settings["chat_id"]), "message_id": sent["message_id"]}
        except urllib.error.HTTPError as error:
            if error.code == 429:
                try:
                    retry_after = json.loads(error.read(64000)).get("parameters", {}).get("retry_after", 300)
                except Exception:
                    retry_after = 300
                raise Transient(retry_after) from None
            if error.code in {400, 401, 403, 404}:
                raise Terminal("TELEGRAM_REJECTED_REQUEST") from None
            raise RuntimeError("TELEGRAM_DELIVERY_UNCONFIRMED") from None
        except Exception:
            raise RuntimeError("TELEGRAM_DELIVERY_UNCONFIRMED") from None


class Dashboard:
    """The in-process transport (E6.T2 S2; owner, 2026-10-22: approvals and notifications go through
    the dashboard only, Telegram "is ONLY for me"). Chosen when no Telegram or relay is set up at all
    (`transport_for`): the row is then DELIVERED as shown in the dashboard, which lists
    notification_outbox rows whatever their state, instead of ending FAILED for lack of a Telegram
    relay. Nothing leaves the machine. Notification rows are information only, never decisions."""

    PROVIDER = "dashboard"
    MARK = json.dumps({"transport": "dashboard"})   # written on the row when this transport claims it

    def receipt(self, row):
        return {"provider": self.PROVIDER, "message_id": row["event_key"], "chat_id": "local"}

    def send_notification(self, row):
        return self.receipt(row)

    def reconcile_notification(self, row):
        # Only a row this transport itself claimed (an interrupted local "send" wrote nothing outside
        # this database) is reconciled as shown; any other row keeps its own reconciliation path,
        # which this transport cannot do (review fix P5).
        if row["delivery"] != self.MARK:
            raise RuntimeError("DASHBOARD_CANNOT_RECONCILE_ANOTHER_TRANSPORT")
        return self.receipt(row)


TELEGRAM_FIELDS = ("keychain_service", "keychain_account", "chat_id")


def relay_configured(config):
    """The owner's relay: transport bridge_relay with a bridge URL actually set (review fix P3: the
    shipped config.json names bridge_relay with an empty URL)."""
    return ((config.get("telegram") or {}).get("transport") == "bridge_relay"
            and bool(str((config.get("bridge_transport") or {}).get("url") or "").strip()))


def telegram_configured(config):
    """The owner's Telegram or relay is set up, even partly: a relay URL, or any Telegram credential or
    chat field. A partial setup keeps its own transport, so it fails visibly as before (P5)."""
    settings = config.get("telegram") or {}
    return relay_configured(config) or any(settings.get(field) for field in TELEGRAM_FIELDS)


def transport_for(config):
    if relay_configured(config):
        return Relay(config)
    return Telegram(config) if telegram_configured(config) else Dashboard()


def initialize(database):
    columns = {row[1] for row in database.execute("PRAGMA table_info(notification_outbox)")}
    for name, definition in {"presend_attempts": "INTEGER NOT NULL DEFAULT 0", "next_action": "TEXT",
                             "send_error": "TEXT", "reconcile_error": "TEXT"}.items():
        if name not in columns:
            database.execute(f"ALTER TABLE notification_outbox ADD COLUMN {name} {definition}")
    # E6.T4: Approvals' "Recent notices" reads the newest delivered rows.
    database.execute("CREATE INDEX IF NOT EXISTS notification_outbox_updated ON notification_outbox(updated_at)")
    database.commit()


def error_code(error):
    """The transport's own error code, kept beside the outbox state (owner, 2026-10-06: "fix daily
    report"; a bridge refusal such as BRIDGE_HTTP_400 used to be recorded only as
    TELEGRAM_SEND_UNCERTAIN_RECONCILE_BEFORE_RESEND). Only a code-shaped message is kept (upper
    case letters, digits and underscores, at most 80); anything else is reduced to the exception's
    type name, so no URL, token or provider text is ever stored."""
    text = str(error)
    if re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", text):
        return text
    return type(error).__name__[:80]


def validate_receipt(receipt):
    if (not isinstance(receipt, dict) or receipt.get("provider") not in {"telegram", Dashboard.PROVIDER}
            or not receipt.get("message_id") or not receipt.get("chat_id")):
        raise RuntimeError("TELEGRAM_DELIVERY_UNCONFIRMED")


def retry_at(now, delay):
    return (dt.datetime.fromisoformat(now) + dt.timedelta(seconds=delay)).isoformat()


# Seconds one dispatcher waits for another to finish (owner, 2026-10-02: applications run as
# their own job, so the hourly cycle's dispatch and an application session's human-check message
# can now run at the same moment). A dispatcher that cannot get its turn sends nothing; the rows
# stay PENDING for the next dispatch.
DISPATCH_LOCK_WAIT_SECONDS = 60


@contextlib.contextmanager
def _one_dispatcher(database, wait_seconds=DISPATCH_LOCK_WAIT_SECONDS):
    """One dispatcher per database file at a time. dispatch() treats any SENDING row as an
    interrupted send (UNCERTAIN), which is only true when no other process is mid-send."""
    try:
        files = [row[2] for row in database.execute("PRAGMA database_list") if row[1] == "main"]
    except Exception:
        files = []
    if not files or not files[0]:
        yield True
        return
    descriptor = portable.open_lock(Path(files[0]).parent / "notifications.lock")
    try:
        deadline = time.monotonic() + wait_seconds
        while True:
            if portable.try_lock(descriptor):
                break
            if time.monotonic() >= deadline:
                yield False
                return
            time.sleep(0.5)
        try:
            yield True
        finally:
            portable.unlock(descriptor)
    finally:
        os.close(descriptor)


def dispatch(database, config, transport=None, now=None, lock_wait_seconds=DISPATCH_LOCK_WAIT_SECONDS):
    with _one_dispatcher(database, lock_wait_seconds) as turn:
        if not turn:
            return {"state": "DEFERRED_ANOTHER_DISPATCHER_ACTIVE"}
        return _dispatch(database, config, transport, now)


def _dispatch(database, config, transport=None, now=None):
    now = now or applications.stamp()
    initialize(database)
    transport = transport or transport_for(config)
    with database:
        database.execute("UPDATE notification_outbox SET state='UNCERTAIN',next_at=NULL,last_error='INTERRUPTED_SEND_REQUIRES_RECONCILIATION',next_action='Reconcile the existing notification before any resend' WHERE state='SENDING'")
    if hasattr(transport, "reconcile_notification"):
        for row in list(database.execute("SELECT * FROM notification_outbox WHERE state='UNCERTAIN' AND reconcile_attempts<5 AND (next_at IS NULL OR next_at<=?) LIMIT 20", (now,))):
            with database:
                database.execute("UPDATE notification_outbox SET reconcile_attempts=reconcile_attempts+1 WHERE event_key=?", (row["event_key"],))
            try:
                receipt = transport.reconcile_notification(row)
                validate_receipt(receipt)
                with database:
                    database.execute("UPDATE notification_outbox SET state='DELIVERED',delivery=?,last_error=NULL,next_at=NULL,next_action=NULL WHERE event_key=?", (json.dumps(receipt), row["event_key"]))
            except Transient as error:
                with database:
                    database.execute("UPDATE notification_outbox SET state='PENDING',next_at=?,last_error='RELAY_CONFIRMED_PENDING',next_action='Retry the same notification after backoff',reconcile_error=? WHERE event_key=?", (retry_at(now, error.retry_after), error_code(error), row["event_key"]))
            except Terminal as error:
                with database:
                    database.execute("UPDATE notification_outbox SET state='FAILED',next_at=NULL,last_error='RELAY_CONFIRMED_FAILED',next_action='Repair notification delivery; the user blocker remains unresolved',reconcile_error=? WHERE event_key=?", (error_code(error), row["event_key"]))
            except Exception as error:
                exhausted = row["reconcile_attempts"] + 1 >= 5
                with database:
                    database.execute("UPDATE notification_outbox SET next_at=?,last_error=?,next_action=?,reconcile_error=? WHERE event_key=?",
                                     (None if exhausted else retry_at(now, 300 * 2 ** row["reconcile_attempts"]),
                                      "NOTIFICATION_RECONCILIATION_EXHAUSTED" if exhausted else "NOTIFICATION_RECONCILIATION_UNAVAILABLE",
                                      "Reconcile delivery evidence manually before any resend" if exhausted else "Reconcile the same notification after backoff",
                                      error_code(error), row["event_key"]))
    rows = list(database.execute("SELECT * FROM notification_outbox WHERE state='PENDING' AND (next_at IS NULL OR next_at<=?) ORDER BY updated_at, rowid LIMIT 20", (now,)))
    for row in rows:
        if row["attempts"] >= 5:
            with database:
                database.execute("UPDATE notification_outbox SET state='FAILED',next_at=NULL,last_error='NOTIFICATION_RETRIES_EXHAUSTED',next_action='Repair notification delivery; the user blocker remains unresolved' WHERE event_key=?", (row["event_key"],))
            continue
        with database:
            claimed = database.execute("UPDATE notification_outbox SET state='SENDING',attempts=attempts+1,updated_at=?,delivery=COALESCE(?,delivery) WHERE event_key=? AND state='PENDING'",
                                       (now, Dashboard.MARK if isinstance(transport, Dashboard) else None, row["event_key"]))
        if claimed.rowcount != 1:
            continue
        try:
            receipt = transport.send_notification(row) if hasattr(transport, "send_notification") else transport.send(row["message"])
            validate_receipt(receipt)
            with database:
                database.execute("UPDATE notification_outbox SET state='DELIVERED',delivery=?,last_error=NULL,next_at=NULL,next_action=NULL WHERE event_key=?", (json.dumps(receipt), row["event_key"]))
        except Exception as error:
            attempts = row["attempts"] + 1
            delay = min(86400, 300 * 2 ** min(row["attempts"], 8))
            presend_attempts = row["presend_attempts"]
            if isinstance(error, Unavailable):
                presend_attempts += 1
                state, reason = ("FAILED" if presend_attempts >= 5 else "PENDING"), "TELEGRAM_SECURE_CREDENTIAL_OR_CHAT_UNAVAILABLE"
                attempts -= 1
                delay = 3600
                action = "Restore secure notification credentials/configuration; retry the retained notification after repair"
            elif isinstance(error, Transient):
                state, reason = ("FAILED" if attempts >= 5 else "PENDING"), "TELEGRAM_RATE_LIMIT_OR_CONFIRMED_TRANSIENT"
                delay = max(delay, error.retry_after)
                action = "Retry the same notification after backoff" if state == "PENDING" else "Repair notification delivery; retry budget exhausted"
            elif isinstance(error, Terminal):
                state, reason = "FAILED", "TELEGRAM_REJECTED_REQUEST"
                action = "Repair notification delivery; the user blocker remains unresolved"
            else:
                state, reason = "UNCERTAIN", "TELEGRAM_SEND_UNCERTAIN_RECONCILE_BEFORE_RESEND"
                action = "Reconcile the existing notification before any resend"
                if not hasattr(transport, "reconcile_notification"):
                    action = "Automatic reconciliation unsupported by this adapter; verify delivery manually before any resend"
            next_at = retry_at(now, delay) if state == "PENDING" else None
            with database:
                database.execute("UPDATE notification_outbox SET state=?,attempts=?,presend_attempts=?,last_error=?,next_at=?,next_action=?,send_error=? WHERE event_key=?", (state, attempts, presend_attempts, reason, next_at, action, error_code(error), row["event_key"]))
