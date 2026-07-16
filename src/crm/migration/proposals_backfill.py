"""Idempotent legacy Sheet proposal backfill without synthetic evidence."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from decimal import Decimal, InvalidOperation
import hashlib
import json
from uuid import UUID, uuid4

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
    Account,
    IngestEvent,
    Lead,
    Proposal,
    ProposalVersion,
    SourceIdentity,
)


@dataclass(frozen=True, slots=True)
class ProposalBackfillReport:
    input_rows: int
    proposal_rows: int
    imported: int
    missing_value: int
    missing_sent_evidence: int
    conflicts: int
    unmatched_account: int
    replay_noop: int = 0
    applied: bool = False
    status_counts: dict[str, int] = field(default_factory=dict)

    def safe_dict(self) -> dict[str, object]:
        return {
            "input_rows": self.input_rows,
            "proposal_rows": self.proposal_rows,
            "imported": self.imported,
            "missing_value": self.missing_value,
            "missing_sent_evidence": self.missing_sent_evidence,
            "conflicts": self.conflicts,
            "unmatched_account": self.unmatched_account,
            "replay_noop": self.replay_noop,
            "applied": self.applied,
        }


class _ReviewRequired(RuntimeError):
    pass


_STATUS_MAP = {
    "requested": "draft",
    "draft": "draft",
    "sent": "sent",
    "follow-up 1": "sent",
    "follow-up 2": "sent",
    "follow-up 3": "sent",
    "reactivation": "sent",
    "viewed": "viewed",
    "negotiation": "negotiation",
    "won": "won",
    "lost": "lost",
    "withdrawn": "withdrawn",
    "expired": "expired",
}


def _sent_at(value: str) -> datetime | None:
    raw = value.strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.combine(datetime.strptime(raw, fmt).date(), time(), UTC)
        except ValueError:
            pass
    raise _ReviewRequired("invalid sent date")


def _amount(value: str) -> Decimal | None:
    raw = value.strip()
    if not raw:
        return None
    try:
        parsed = Decimal(raw)
    except InvalidOperation:
        raise _ReviewRequired("invalid value") from None
    if not parsed.is_finite() or parsed < 0 or parsed > Decimal("9999999999999999.99"):
        raise _ReviewRequired("invalid value")
    if parsed.as_tuple().exponent < -2:
        raise _ReviewRequired("invalid value")
    return parsed.quantize(Decimal("0.01"))


def _currency(row: SnapshotRow) -> str:
    value = (row.values.get("Proposal Currency") or "EUR").strip().upper()
    if len(value) != 3 or not value.isascii() or not value.isalpha():
        raise _ReviewRequired("invalid currency")
    return value


def _status(row: SnapshotRow) -> str:
    raw = row.values.get("Proposal Status", "").strip()
    if not raw:
        stage = row.values.get("Status", "").strip().lower()
        raw = "Won" if stage == "won" else "Lost" if stage == "lost" else "Sent"
    status = _STATUS_MAP.get(raw.lower())
    if status is None:
        raise _ReviewRequired("unknown proposal status")
    return status


def _is_proposal(row: SnapshotRow) -> bool:
    return any(
        row.values.get(field, "").strip()
        for field in ("Proposal Sent", "Proposal Status", "Proposal Value")
    )


def _classified(snapshot: SheetSnapshot):
    result = []
    for row in snapshot.rows:
        if not _is_proposal(row):
            continue
        result.append(
            (
                row,
                _status(row),
                _sent_at(row.values.get("Proposal Sent", "")),
                _amount(row.values.get("Proposal Value", "")),
                _currency(row),
            )
        )
    return result


def _hash(
    row: SnapshotRow,
    status: str,
    sent_at: datetime | None,
    amount: Decimal | None,
    currency: str,
) -> str:
    material = {
        "external_id": row.external_id,
        "status": status,
        "sent_at": sent_at.isoformat() if sent_at else None,
        "amount": str(amount) if amount is not None else None,
        "currency": currency,
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _lead_account(
    session: Session, workspace_id: UUID, scope: str, external_id: str
) -> tuple[Lead, Account] | None:
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
        return None
    lead = session.get(Lead, identity.canonical_entity_id)
    if lead is None or lead.workspace_id != workspace_id or lead.account_id is None:
        return None
    account = session.get(Account, lead.account_id)
    return (
        (lead, account)
        if account is not None and account.workspace_id == workspace_id
        else None
    )


def _apply(
    session: Session,
    workspace_id: UUID,
    scope: str,
    row: SnapshotRow,
    status: str,
    sent_at: datetime | None,
    amount: Decimal | None,
    currency: str,
) -> str:
    linked = _lead_account(session, workspace_id, scope, row.external_id)
    if linked is None:
        raise _ReviewRequired("unmatched account")
    if status != "draft" and sent_at is None:
        raise _ReviewRequired("missing sent date")
    if status == "lost" and not row.values.get("Proposal Lost Reason", "").strip():
        raise _ReviewRequired("missing lost reason")
    lead, account = linked
    fingerprint = _hash(row, status, sent_at, amount, currency)
    key = f"sheets-proposal-backfill:{scope}:{row.external_id}:{fingerprint}"
    event_id = session.execute(
        insert(IngestEvent)
        .values(
            id=uuid4(),
            workspace_id=workspace_id,
            source_system="google_sheets",
            source_scope=scope,
            event_type="sheets.proposal_backfill",
            schema_version=1,
            external_event_id=row.external_id,
            idempotency_key=key,
            occurred_at=datetime.now(UTC),
            payload={"external_id": row.external_id, "fingerprint": fingerprint},
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
    if event_id is None:
        return "replay"
    proposal_identity = session.scalar(
        select(SourceIdentity).where(
            SourceIdentity.workspace_id == workspace_id,
            SourceIdentity.source_system == "google_sheets",
            SourceIdentity.source_scope == scope,
            SourceIdentity.entity_kind == "proposal",
            SourceIdentity.external_id == row.external_id,
        )
    )
    if proposal_identity is not None:
        raise _ReviewRequired("changed legacy proposal")
    proposal_identity = SourceIdentity(
        workspace_id=workspace_id,
        source_system="google_sheets",
        source_scope=scope,
        entity_kind="proposal",
        external_id=row.external_id,
    )
    session.add(proposal_identity)
    proposal = Proposal(
        workspace_id=workspace_id,
        account_id=account.id,
        lead_id=lead.id,
        title=f"Legacy proposal — {account.display_name}",
        status=status,
        sent_at=sent_at if status != "draft" else None,
        sent_verification_state="legacy_unverified" if status != "draft" else None,
        currency=currency,
        value_state="candidate" if amount is not None else "missing",
        won_at=sent_at if status == "won" else None,
        lost_at=sent_at if status == "lost" else None,
        lost_reason=row.values.get("Proposal Lost Reason", "").strip() or None,
    )
    session.add(proposal)
    session.flush()
    version = ProposalVersion(
        proposal_id=proposal.id,
        version_number=1,
        status="sent" if status != "draft" else "draft",
        sent_at=sent_at,
        one_off_amount=amount,
    )
    session.add(version)
    session.flush()
    proposal.selected_version_id = version.id
    proposal_identity.canonical_entity_type = "proposal"
    proposal_identity.canonical_entity_id = proposal.id
    event = session.get(IngestEvent, event_id)
    event.processing_status = "applied"
    event.applied_at = datetime.now(UTC)
    return "imported"


def backfill_proposals(
    snapshot: SheetSnapshot,
    *,
    apply: bool = False,
    database_url: str | None = None,
    workspace_id: object | None = None,
) -> ProposalBackfillReport:
    snapshot = validate_snapshot(snapshot)
    conflicts = 0
    rows = []
    for row in snapshot.rows:
        if not _is_proposal(row):
            continue
        try:
            rows.append(
                (
                    row,
                    _status(row),
                    _sent_at(row.values.get("Proposal Sent", "")),
                    _amount(row.values.get("Proposal Value", "")),
                    _currency(row),
                )
            )
        except _ReviewRequired:
            conflicts += 1
    missing_value = sum(amount is None for _, _, _, amount, _ in rows)
    missing_evidence = sum(status != "draft" for _, status, _, _, _ in rows)
    status_counts: dict[str, int] = {}
    for _, status, _, _, _ in rows:
        status_counts[status] = status_counts.get(status, 0) + 1
    if not apply:
        return ProposalBackfillReport(
            snapshot.input_rows,
            len(rows),
            len(rows),
            missing_value,
            missing_evidence,
            conflicts,
            0,
            status_counts=status_counts,
        )
    if not database_url or not database_url.startswith("postgresql+psycopg://"):
        raise ValueError("apply requires an explicit PostgreSQL database_url")
    if type(workspace_id) is not UUID:
        raise ValueError("apply requires an explicit workspace UUID")
    imported = replay = unmatched = 0
    engine = create_engine(database_url)
    try:
        with Session(engine) as session, session.begin():
            for values in rows:
                try:
                    with session.begin_nested():
                        outcome = _apply(
                            session, workspace_id, _scope(snapshot), *values
                        )
                except _ReviewRequired as exc:
                    conflicts += 1
                    unmatched += int(str(exc) == "unmatched account")
                else:
                    imported += int(outcome == "imported")
                    replay += int(outcome == "replay")
    finally:
        engine.dispose()
    return ProposalBackfillReport(
        snapshot.input_rows,
        len(rows),
        imported,
        missing_value,
        missing_evidence,
        conflicts,
        unmatched,
        replay,
        True,
        status_counts,
    )
