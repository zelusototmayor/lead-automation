"""Canonical ordinary follow-up identity and calendar policy."""

import hashlib
import json
from datetime import UTC, timedelta
from calendar import monthrange
from uuid import uuid5
from zoneinfo import ZoneInfo

LISBON = ZoneInfo("Europe/Lisbon")
CADENCE_VERSION = "sales-2026-09-26"
MAX_STAGE = 5
INTERVAL_DAYS = (2, 4, 5, 7)


def due_at(sent_at, stage, *, not_before=None):
    if type(stage) is not int or not 1 <= stage <= MAX_STAGE:
        raise ValueError("sequence_finished")
    if sent_at.tzinfo is None or (not_before is not None and not_before.tzinfo is None):
        raise ValueError("aware_time_required")
    local = sent_at.astimezone(LISBON)
    if stage == MAX_STAGE:
        year, month = local.year + (local.month == 12), local.month % 12 + 1
        due = local.replace(year=year, month=month, day=min(local.day, monthrange(year, month)[1]))
    else:
        due = local + timedelta(days=INTERVAL_DAYS[stage - 1])
    if not_before is not None:
        due = max(due, not_before.astimezone(LISBON))
    # José22Oct: weekday follow-ups; preserve the local clock, cadence and
    # explicit not_before, including across DST and calendar-month boundaries.
    while due.weekday() >= 5:
        due += timedelta(days=1)
    return due.astimezone(UTC)


POLICY_ID = "ordinary-followup/v1"


def obligation_key(
    workspace_id, mailbox, lead_id, thread_id, fu_stage, anchor_message_id
):
    values = [
        str(workspace_id),
        mailbox,
        str(lead_id),
        thread_id,
        fu_stage,
        anchor_message_id,
    ]
    return (
        "fu:v1:"
        + hashlib.sha256(
            json.dumps(
                values, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest()
    )


def task_id(workspace_id, key):
    return uuid5(workspace_id, "followup-task:" + key)
