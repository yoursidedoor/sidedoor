"""Move the reply backlog cutoff and the cold-inbound skip out of the way for fixture conversations.

Fixture inbound messages are dated before the 2026-09-30 reply backlog cutoff
(reply_execution.BACKLOG_CUTOFF), which applies only to the real backlog on file (owner,
2026-09-30: "dont send these replies"). Since 2026-10-01 the reply worker finishes a backlog
message without running the reply AI, and (owner, 2026-10-01: "cold strangers should be fully
skipped") also a message from a sender no local record tracks. The fixture senders of the
modules below stand for tracked contacts without seeding tracking records, so their senders
count as tracked and answerable for the worker's skip (reply_worker._reply_state). The send-side gate
(outreach.reply_conversation) is unchanged. A module that exercises the worker on fixture
messages imports these:

    from backlog_fixture import setUpModule, tearDownModule  # noqa: F401
"""

import datetime as dt

import reply_execution
import reply_worker

# Captured once at first import, so start/stop stay balanced however many modules share this.
OWNER_CUTOFF = reply_execution.BACKLOG_CUTOFF
OWNER_REPLY_STATE = reply_worker._reply_state


def setUpModule():
    reply_execution.BACKLOG_CUTOFF = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)
    reply_worker._reply_state = lambda database, person, lead_id, thread, snapshot=None: "REPLY"


def tearDownModule():
    reply_execution.BACKLOG_CUTOFF = OWNER_CUTOFF
    reply_worker._reply_state = OWNER_REPLY_STATE
