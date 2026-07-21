"""Idempotent operational backfill for legacy Sheet tasks and notes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
import hashlib
import json
from typing import Callable
from uuid import UUID, NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from src.crm.migration.backfill import _scope
from src.crm.migration.sheets_snapshot import (
    SheetSnapshot,
    SnapshotRow,
    validate_snapshot,
)
from src.crm.persistence.models import (
    Activity,
    IngestEvent,
    Lead,
    Proposal,
    SourceIdentity,
    Task,
    Workspace,
)


@dataclass(frozen=True, slots=True)
class OperationalBackfillReport:
    input_rows: int
    task_candidates: int
    note_candidates: int
    tasks_created: int = 0
    activities_created: int = 0
    replay_noop: int = 0
    conflicts: int = 0
    review_reasons: dict[str, int] = field(default_factory=dict)
    full_history_unavailable: bool = True
    applied: bool = False

    def safe_dict(self) -> dict[str, object]:
        return {
            "input_rows": self.input_rows,
            "task_candidates": self.task_candidates,
            "note_candidates": self.note_candidates,
            "tasks_created": self.tasks_created,
            "activities_created": self.activities_created,
            "replay_noop": self.replay_noop,
            "conflicts": self.conflicts,
            "review_reasons": dict(self.review_reasons),
            "full_history_unavailable": self.full_history_unavailable,
            "applied": self.applied,
        }


@dataclass(frozen=True, slots=True)
class _TaskCandidate:
    row: SnapshotRow
    slot: str
    task_type: str
    title: str
    due_at: datetime
    fingerprint: str


@dataclass(frozen=True, slots=True)
class _NoteCandidate:
    row: SnapshotRow
    slot: str
    activity_type: str
    title: str
    summary: str
    occurred_at: datetime | None
    fingerprint: str


class _ReviewRequired(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__("legacy operation requires review")


_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y")
_TIME_FORMATS = ("%H:%M", "%H:%M:%S", "%I:%M %p")
_TASK_FIELDS = (
    ("Next Email Date", "next_email", "email", "Send legacy follow-up email"),
    (
        "Next Proposal Follow-Up",
        "next_proposal_follow_up",
        "follow_up",
        "Follow up on legacy proposal",
    ),
)
_NOTE_FIELDS = (
    ("Notes", "notes", "note", "Legacy note"),
    ("notes", "notes", "note", "Legacy note"),
    ("Call Notes", "call_notes", "call", "Legacy call note"),
    ("Email Notes", "email_notes", "email_sent", "Legacy email note"),
    ("Proposal Notes", "proposal_notes", "proposal", "Legacy proposal note"),
)
_TERMINAL_STAGES = {"lost", "not a fit", "won"}
_EMAIL_STAGES = {"send email", "email sent"}
_LAST_TOUCH_TYPES = {
    "call": "call",
    "called": "call",
    "connected": "call",
    "no answer": "call",
    "voicemail": "call",
    "email sent": "email_sent",
    "sent email": "email_sent",
    "email received": "email_received",
    "received email": "email_received",
    "reply received": "email_received",
    "meeting": "meeting",
    "proposal": "proposal",
    "note": "note",
}
_OUTREACH_SEQUENCE = (
    ("Initial Email Sent", "initial_email_sent"),
    ("Outreach FU1 Sent", "outreach_fu1_sent"),
    ("Outreach FU2 Sent", "outreach_fu2_sent"),
    ("Outreach FU3 Sent", "outreach_fu3_sent"),
    ("Outreach Reactivation Sent", "outreach_reactivation_sent"),
)
_OUTREACH_NEXT = (
    ("outreach_fu1_due", 2, "Send first legacy outreach follow-up"),
    ("outreach_fu2_due", 3, "Send second legacy outreach follow-up"),
    ("outreach_fu3_due", 5, "Send third legacy outreach follow-up"),
    ("outreach_reactivation_due", 30, "Send legacy outreach reactivation"),
)
_OUTREACH_TYPES = {
    "call": "call",
    "phone": "call",
    "email": "email",
    "follow-up": "follow_up",
    "follow up": "follow_up",
    "outreach": "follow_up",
}


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def artifact_uuid(
    workspace_id: UUID,
    source_scope: str,
    external_id: str,
    artifact_kind: str,
    slot: str,
) -> UUID:
    """Return a delimiter-safe deterministic UUID for one source artifact slot."""
    material = json.dumps(
        [str(workspace_id), source_scope, external_id, artifact_kind, slot],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return uuid5(NAMESPACE_URL, material)


def _date(raw: str, reason: str) -> date:
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    raise _ReviewRequired(reason)


def _time(raw: str, reason: str = "invalid_next_call_time") -> time:
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(raw, fmt).time()
        except ValueError:
            pass
    raise _ReviewRequired(reason)


def _at_local(day: date, clock: time, timezone: ZoneInfo) -> datetime:
    naive = datetime.combine(day, clock)
    candidates: dict[datetime, datetime] = {}
    for fold in (0, 1):
        local = naive.replace(tzinfo=timezone, fold=fold)
        utc_value = local.astimezone(UTC)
        round_trip = utc_value.astimezone(timezone).replace(tzinfo=None)
        if round_trip == naive:
            candidates[utc_value] = local
    if len(candidates) != 1:
        raise _ReviewRequired("invalid_or_ambiguous_local_time")
    return next(iter(candidates))


def _task_fingerprint(
    scope: str,
    row: SnapshotRow,
    slot: str,
    task_type: str,
    title: str,
    due_at: datetime,
) -> str:
    return _canonical_hash(
        [scope, row.external_id, slot, task_type, title, due_at.isoformat()]
    )


def _outreach(row: SnapshotRow) -> str:
    supplied = [
        row.values.get(header, "").strip()
        for header in ("Outreach", "Outreach Method")
        if row.values.get(header, "").strip()
    ]
    normalized = {value.casefold() for value in supplied}
    if len(normalized) > 1:
        raise _ReviewRequired("ambiguous_outreach")
    if not supplied:
        raise _ReviewRequired("missing_outreach")
    task_type = _OUTREACH_TYPES.get(supplied[0].casefold())
    if task_type is None:
        raise _ReviewRequired("unknown_outreach")
    return task_type


def _classify(
    snapshot: SheetSnapshot, timezone_name: str
) -> tuple[list[_TaskCandidate], list[_NoteCandidate], dict[str, int]]:
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        raise ValueError("invalid explicit timezone") from None
    scope = _scope(snapshot)
    tasks: list[_TaskCandidate] = []
    notes: list[_NoteCandidate] = []
    reasons: dict[str, int] = {}

    def review(reason: str) -> None:
        reasons[reason] = reasons.get(reason, 0) + 1

    for row in snapshot.rows:
        stage = row.values.get("Stage", "").strip().casefold()
        terminal_stage = stage in _TERMINAL_STAGES
        call_date = row.values.get("Next Call Date", "").strip()
        call_time = row.values.get("Next Call Time", "").strip()
        if call_date or call_time:
            try:
                if terminal_stage:
                    raise _ReviewRequired("terminal_stage_next_call")
                if not call_date:
                    raise _ReviewRequired("missing_next_call_date")
                if not call_time:
                    raise _ReviewRequired("missing_next_call_time")
                due_at = _at_local(
                    _date(call_date, "invalid_next_call_date"),
                    _time(call_time),
                    timezone,
                )
                title = "Make legacy scheduled call"
                tasks.append(
                    _TaskCandidate(
                        row,
                        "next_call",
                        "call",
                        title,
                        due_at,
                        _task_fingerprint(
                            scope, row, "next_call", "call", title, due_at
                        ),
                    )
                )
            except _ReviewRequired as exc:
                review(exc.reason)

        for header, slot, task_type, title in _TASK_FIELDS:
            raw = row.values.get(header, "").strip()
            if not raw:
                continue
            try:
                if terminal_stage:
                    raise _ReviewRequired(f"terminal_stage_{slot}")
                due_at = _at_local(
                    _date(raw, f"invalid_{slot}_date"), time(hour=9), timezone
                )
                tasks.append(
                    _TaskCandidate(
                        row,
                        slot,
                        task_type,
                        title,
                        due_at,
                        _task_fingerprint(scope, row, slot, task_type, title, due_at),
                    )
                )
            except _ReviewRequired as exc:
                review(exc.reason)

        follow_up = row.values.get("Follow-Up Due", "").strip()
        outreach_values_present = any(
            row.values.get(header, "").strip()
            for header in ("Outreach", "Outreach Method")
        )
        if follow_up or outreach_values_present:
            try:
                if terminal_stage:
                    raise _ReviewRequired("terminal_stage_follow_up_due")
                if not follow_up:
                    raise _ReviewRequired("missing_follow_up_due")
                task_type = _outreach(row)
                due_at = _at_local(
                    _date(follow_up, "invalid_follow_up_due_date"),
                    time(hour=9),
                    timezone,
                )
                title = "Complete legacy outreach follow-up"
                tasks.append(
                    _TaskCandidate(
                        row,
                        "follow_up_due",
                        task_type,
                        title,
                        due_at,
                        _task_fingerprint(
                            scope, row, "follow_up_due", task_type, title, due_at
                        ),
                    )
                )
            except _ReviewRequired as exc:
                review(exc.reason)

        due = row.values.get("Due", "").strip()
        due_time = row.values.get("Due Time", "").strip()
        if due or due_time:
            try:
                if not due:
                    raise _ReviewRequired("missing_due_date")
                if not stage:
                    raise _ReviewRequired("missing_stage_for_due")
                if stage in _TERMINAL_STAGES:
                    raise _ReviewRequired("terminal_stage_due")
                task_type = "email" if stage in _EMAIL_STAGES else "call"
                clock = (
                    _time(due_time, "invalid_due_time") if due_time else time(hour=9)
                )
                due_at = _at_local(_date(due, "invalid_due_date"), clock, timezone)
                title = (
                    "Send legacy scheduled email"
                    if task_type == "email"
                    else "Make legacy scheduled call"
                )
                tasks.append(
                    _TaskCandidate(
                        row,
                        "due",
                        task_type,
                        title,
                        due_at,
                        _task_fingerprint(scope, row, "due", task_type, title, due_at),
                    )
                )
            except _ReviewRequired as exc:
                review(exc.reason)

        proposal_due = row.values.get("Proposal Next Action Due", "").strip()
        if proposal_due:
            try:
                if terminal_stage:
                    raise _ReviewRequired("terminal_stage_proposal_next_action_due")
                due_at = _at_local(
                    _date(proposal_due, "invalid_proposal_next_action_due"),
                    time(hour=9),
                    timezone,
                )
                title = "Complete legacy proposal next action"
                tasks.append(
                    _TaskCandidate(
                        row,
                        "proposal_next_action_due",
                        "follow_up",
                        title,
                        due_at,
                        _task_fingerprint(
                            scope,
                            row,
                            "proposal_next_action_due",
                            "follow_up",
                            title,
                            due_at,
                        ),
                    )
                )
            except _ReviewRequired as exc:
                review(exc.reason)

        marker_values = [
            row.values.get(header, "").strip() for header, _slot in _OUTREACH_SEQUENCE
        ]
        if any(marker_values) and not due:
            try:
                parsed_markers = [
                    _date(raw, f"invalid_{slot}_date") if raw else None
                    for raw, (_header, slot) in zip(
                        marker_values, _OUTREACH_SEQUENCE, strict=True
                    )
                ]
                first_missing = next(
                    (
                        index
                        for index, value in enumerate(parsed_markers)
                        if value is None
                    ),
                    len(parsed_markers),
                )
                if any(parsed_markers[first_missing + 1 :]):
                    raise _ReviewRequired("ambiguous_outreach_sequence")
                if first_missing and first_missing < len(parsed_markers):
                    if stage in _TERMINAL_STAGES:
                        raise _ReviewRequired("terminal_stage_outreach_sequence")
                    slot, days_after, title = _OUTREACH_NEXT[first_missing - 1]
                    sent_on = parsed_markers[first_missing - 1]
                    assert sent_on is not None
                    due_at = _at_local(
                        sent_on + timedelta(days=days_after), time(hour=9), timezone
                    )
                    tasks.append(
                        _TaskCandidate(
                            row,
                            slot,
                            "email",
                            title,
                            due_at,
                            _task_fingerprint(scope, row, slot, "email", title, due_at),
                        )
                    )
            except _ReviewRequired as exc:
                review(exc.reason)

        seen_note_slots: set[str] = set()
        for header, slot, activity_type, title in _NOTE_FIELDS:
            summary = row.values.get(header, "").strip()
            if not summary or slot in seen_note_slots:
                continue
            seen_note_slots.add(slot)
            fingerprint = _canonical_hash(
                [scope, row.external_id, slot, activity_type, summary, None]
            )
            notes.append(
                _NoteCandidate(
                    row, slot, activity_type, title, summary, None, fingerprint
                )
            )

        what_happened = row.values.get("What Happened", "").strip()
        if what_happened:
            try:
                last_touch = row.values.get("Last Touch Type", "").strip().casefold()
                if not last_touch:
                    raise _ReviewRequired("missing_last_touch_type")
                activity_type = _LAST_TOUCH_TYPES.get(last_touch)
                if activity_type is None:
                    raise _ReviewRequired("unknown_last_touch_type")
                touched = row.values.get("Dashboard Touched", "").strip()
                if not touched:
                    raise _ReviewRequired("missing_dashboard_touched")
                occurred_at = _at_local(
                    _date(touched, "invalid_dashboard_touched"), time(), timezone
                )
                fingerprint = _canonical_hash(
                    [
                        scope,
                        row.external_id,
                        "what_happened",
                        activity_type,
                        what_happened,
                        occurred_at.isoformat(),
                    ]
                )
                notes.append(
                    _NoteCandidate(
                        row,
                        "what_happened",
                        activity_type,
                        "Legacy dashboard touch",
                        what_happened,
                        occurred_at,
                        fingerprint,
                    )
                )
            except _ReviewRequired as exc:
                review(exc.reason)
    return tasks, notes, reasons


def _lead_identity(
    session: Session, workspace_id: UUID, scope: str, external_id: str
) -> tuple[SourceIdentity, Lead]:
    identity = session.scalar(
        select(SourceIdentity).where(
            SourceIdentity.workspace_id == workspace_id,
            SourceIdentity.source_system == "google_sheets",
            SourceIdentity.source_scope == scope,
            SourceIdentity.entity_kind == "lead",
            SourceIdentity.external_id == external_id,
        )
    )
    if (
        identity is None
        or identity.canonical_entity_type != "lead"
        or identity.canonical_entity_id is None
    ):
        raise _ReviewRequired("missing_lead_identity")
    lead = session.get(Lead, identity.canonical_entity_id)
    if lead is None or lead.workspace_id != workspace_id:
        raise _ReviewRequired("invalid_lead_identity")
    return identity, lead


def _proposal_id(
    session: Session, workspace_id: UUID, scope: str, external_id: str, lead: Lead
) -> UUID:
    identity = session.scalar(
        select(SourceIdentity).where(
            SourceIdentity.workspace_id == workspace_id,
            SourceIdentity.source_system == "google_sheets",
            SourceIdentity.source_scope == scope,
            SourceIdentity.entity_kind == "proposal",
            SourceIdentity.external_id == external_id,
        )
    )
    if identity is None:
        raise _ReviewRequired("missing_proposal_identity")
    if (
        identity.canonical_entity_type != "proposal"
        or identity.canonical_entity_id is None
    ):
        raise _ReviewRequired("invalid_proposal_identity")
    proposal = session.get(Proposal, identity.canonical_entity_id)
    if (
        proposal is None
        or proposal.workspace_id != workspace_id
        or proposal.lead_id != lead.id
        or proposal.account_id != lead.account_id
    ):
        raise _ReviewRequired("invalid_proposal_identity")
    return proposal.id


def _claim_event(
    session: Session,
    workspace_id: UUID,
    scope: str,
    external_id: str,
    artifact_kind: str,
    slot: str,
    fingerprint: str,
) -> tuple[IngestEvent, bool]:
    event_id = artifact_uuid(
        workspace_id, scope, external_id, "event", f"{artifact_kind}:{slot}"
    )
    key = f"sheets-operations-backfill:{event_id}"
    inserted = session.execute(
        insert(IngestEvent)
        .values(
            id=event_id,
            workspace_id=workspace_id,
            source_system="google_sheets",
            source_scope=scope,
            event_type=f"sheets.legacy_{artifact_kind}_backfill",
            schema_version=1,
            external_event_id=external_id,
            idempotency_key=key,
            occurred_at=datetime.now(UTC),
            payload={"external_id_hash": _canonical_hash(external_id), "slot": slot},
            payload_hash=fingerprint,
            processing_status="processing",
        )
        .on_conflict_do_nothing(
            index_elements=[
                IngestEvent.workspace_id,
                IngestEvent.source_system,
                IngestEvent.idempotency_key,
            ]
        )
        .returning(IngestEvent.id)
    ).scalar_one_or_none()
    event = session.get(IngestEvent, event_id)
    if event is None:
        raise _ReviewRequired("event_identity_conflict")
    if event.payload_hash != fingerprint:
        raise _ReviewRequired(f"changed_legacy_{artifact_kind}")
    return event, inserted is not None


def _apply_task(
    session: Session,
    workspace_id: UUID,
    owner_user_id: UUID,
    scope: str,
    candidate: _TaskCandidate,
) -> str:
    _identity, lead = _lead_identity(
        session, workspace_id, scope, candidate.row.external_id
    )
    proposal_id = (
        _proposal_id(session, workspace_id, scope, candidate.row.external_id, lead)
        if candidate.slot in {"next_proposal_follow_up", "proposal_next_action_due"}
        else None
    )
    event, event_inserted = _claim_event(
        session,
        workspace_id,
        scope,
        candidate.row.external_id,
        "task",
        candidate.slot,
        candidate.fingerprint,
    )
    task_id = artifact_uuid(
        workspace_id, scope, candidate.row.external_id, "task", candidate.slot
    )
    task = session.get(Task, task_id)
    expected = (
        lead.account_id,
        lead.id,
        proposal_id,
        candidate.task_type,
        candidate.title,
        candidate.due_at,
        owner_user_id,
        "legacy_sheet:" + candidate.slot,
    )
    if task is not None:
        actual = (
            task.account_id,
            task.lead_id,
            task.proposal_id,
            task.task_type,
            task.title,
            task.due_at,
            task.owner_user_id,
            task.source_rule,
        )
        if task.status != "open" or actual != expected:
            raise _ReviewRequired("conflicting_current_task")
        if event.processing_status != "applied":
            event.processing_status = "applied"
            event.applied_at = datetime.now(UTC)
        return "replay"
    if not event_inserted:
        raise _ReviewRequired("missing_backfilled_task")
    session.add(
        Task(
            id=task_id,
            workspace_id=workspace_id,
            account_id=lead.account_id,
            lead_id=lead.id,
            proposal_id=proposal_id,
            task_type=candidate.task_type,
            title=candidate.title,
            due_at=candidate.due_at,
            owner_user_id=owner_user_id,
            status="open",
            source_rule="legacy_sheet:" + candidate.slot,
        )
    )
    event.processing_status = "applied"
    event.applied_at = datetime.now(UTC)
    return "created"


def _apply_note(
    session: Session,
    workspace_id: UUID,
    scope: str,
    candidate: _NoteCandidate,
) -> str:
    identity, lead = _lead_identity(
        session, workspace_id, scope, candidate.row.external_id
    )
    event, event_inserted = _claim_event(
        session,
        workspace_id,
        scope,
        candidate.row.external_id,
        "note",
        candidate.slot,
        candidate.fingerprint,
    )
    occurred_at = candidate.occurred_at or lead.created_at
    activity_title = candidate.title + (
        " (time unavailable)"
        if candidate.occurred_at is not None
        else " (timestamp unavailable)"
    )
    activity_id = artifact_uuid(
        workspace_id, scope, candidate.row.external_id, "activity", candidate.slot
    )
    activity = session.get(Activity, activity_id)
    if activity is not None:
        expected = (
            lead.account_id,
            lead.id,
            candidate.activity_type,
            occurred_at,
            activity_title,
            candidate.summary,
            candidate.fingerprint,
            "google_sheets",
            identity.id,
            event.id,
        )
        actual = (
            activity.account_id,
            activity.lead_id,
            activity.activity_type,
            activity.occurred_at,
            activity.title,
            activity.summary,
            activity.semantic_fingerprint,
            activity.source_system,
            activity.source_identity_id,
            activity.ingest_event_id,
        )
        if actual != expected:
            raise _ReviewRequired("conflicting_legacy_activity")
        if event.processing_status != "applied":
            event.processing_status = "applied"
            event.applied_at = datetime.now(UTC)
        return "replay"
    if not event_inserted:
        raise _ReviewRequired("missing_backfilled_activity")
    session.add(
        Activity(
            id=activity_id,
            workspace_id=workspace_id,
            account_id=lead.account_id,
            lead_id=lead.id,
            contact_id=lead.contact_id,
            activity_type=candidate.activity_type,
            occurred_at=occurred_at,
            title=activity_title,
            summary=candidate.summary,
            semantic_fingerprint=candidate.fingerprint,
            source_system="google_sheets",
            source_identity_id=identity.id,
            ingest_event_id=event.id,
        )
    )
    event.processing_status = "applied"
    event.applied_at = datetime.now(UTC)
    return "created"


def backfill_legacy_operations(
    snapshot: SheetSnapshot,
    *,
    apply: bool = False,
    database_url: str | None = None,
    workspace_id: object | None = None,
    owner_user_id: object | None = None,
    timezone_name: str = "Europe/Lisbon",
    failure_injector: Callable[[str, str, int], None] | None = None,
) -> OperationalBackfillReport:
    """Plan or atomically apply legacy open tasks and typed note activities."""
    snapshot = validate_snapshot(snapshot)
    tasks, notes, reasons = _classify(snapshot, timezone_name)
    base_reasons = dict(reasons)
    if snapshot.missing_id_rows:
        base_reasons["missing_stable_id"] = len(snapshot.missing_id_rows)
    if snapshot.duplicate_ids:
        base_reasons["duplicate_stable_id"] = len(snapshot.duplicate_ids)
    base_conflicts = sum(base_reasons.values())
    if not apply:
        return OperationalBackfillReport(
            snapshot.input_rows,
            len(tasks),
            len(notes),
            conflicts=base_conflicts,
            review_reasons=base_reasons,
        )
    if not database_url or not database_url.startswith("postgresql+psycopg://"):
        raise ValueError("apply requires an explicit PostgreSQL database_url")
    if type(workspace_id) is not UUID or type(owner_user_id) is not UUID:
        raise ValueError("apply requires explicit workspace and owner UUIDs")

    task_created = activity_created = replay = 0
    conflicts = base_conflicts
    review_reasons = dict(base_reasons)
    scope = _scope(snapshot)
    engine = create_engine(database_url)
    try:
        with Session(engine) as session, session.begin():
            workspace = session.get(Workspace, workspace_id)
            if workspace is None:
                raise ValueError("explicit workspace does not exist")
            if workspace.timezone != timezone_name:
                tasks, notes, classification_reasons = _classify(
                    snapshot, workspace.timezone
                )
                review_reasons = {
                    key: value
                    for key, value in review_reasons.items()
                    if key in {"missing_stable_id", "duplicate_stable_id"}
                }
                review_reasons.update(classification_reasons)
                conflicts = sum(review_reasons.values())
            for index, candidate in enumerate(tasks):
                try:
                    with session.begin_nested():
                        if failure_injector is not None:
                            failure_injector("before", "task", index)
                        outcome = _apply_task(
                            session,
                            workspace_id,
                            owner_user_id,
                            scope,
                            candidate,
                        )
                        if failure_injector is not None:
                            failure_injector("after", "task", index)
                except _ReviewRequired as exc:
                    conflicts += 1
                    review_reasons[exc.reason] = review_reasons.get(exc.reason, 0) + 1
                else:
                    task_created += int(outcome == "created")
                    replay += int(outcome == "replay")
            for index, candidate in enumerate(notes):
                try:
                    with session.begin_nested():
                        if failure_injector is not None:
                            failure_injector("before", "note", index)
                        outcome = _apply_note(session, workspace_id, scope, candidate)
                        if failure_injector is not None:
                            failure_injector("after", "note", index)
                except _ReviewRequired as exc:
                    conflicts += 1
                    review_reasons[exc.reason] = review_reasons.get(exc.reason, 0) + 1
                else:
                    activity_created += int(outcome == "created")
                    replay += int(outcome == "replay")
    finally:
        engine.dispose()
    return OperationalBackfillReport(
        snapshot.input_rows,
        len(tasks),
        len(notes),
        task_created,
        activity_created,
        replay,
        conflicts,
        review_reasons,
        True,
        True,
    )
