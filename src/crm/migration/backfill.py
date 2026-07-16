"""Atomic, idempotent shadow-mode import of immutable Sheet snapshots."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import json
from urllib.parse import urlparse
from uuid import UUID, uuid4

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from src.crm.domain.stage_policy import (
    AccountRequirementReviewRequired,
    InvalidTransitionError,
    UnknownStageError,
    requires_account,
    resolve_stage,
    stage_rank,
    validate_transition,
)
from src.crm.migration.sheets_snapshot import (
    SheetSnapshot,
    SnapshotRow,
    validate_snapshot,
)
from src.crm.persistence.models import (
    Account,
    Activity,
    Contact,
    IngestEvent,
    Lead,
    SourceIdentity,
    SyncCheckpoint,
)
from src.crm.services.account_service import (
    normalize_company_name,
    normalize_domain,
    normalize_email,
)


@dataclass(frozen=True, slots=True)
class BackfillReport:
    input_rows: int
    snapshot_rows: int
    imported: int
    accounts_created_or_linked: int
    duplicates: int
    conflicts: int
    unmapped_stages: dict[str, int] = field(default_factory=dict)
    replay_noop: int = 0
    applied: bool = False
    review_reasons: dict[str, int] = field(default_factory=dict)

    def safe_dict(self) -> dict[str, object]:
        return {
            "input_rows": self.input_rows,
            "snapshot_rows": self.snapshot_rows,
            "imported": self.imported,
            "accounts_created_or_linked": self.accounts_created_or_linked,
            "duplicates": self.duplicates,
            "conflicts": self.conflicts,
            "unmapped_stages": dict(self.unmapped_stages),
            "replay_noop": self.replay_noop,
            "applied": self.applied,
            "review_reasons": dict(self.review_reasons),
        }


class _ReviewRequired(RuntimeError):
    def __init__(self, reason: str = "identity_conflict") -> None:
        self.reason = reason
        super().__init__("row requires review")


def _classified_rows(snapshot: SheetSnapshot):
    mapped = []
    unmapped_count = 0
    for row in snapshot.rows:
        try:
            mapped.append((row, resolve_stage(row.values.get("Status", "")).value))
        except UnknownStageError:
            unmapped_count += 1
    return mapped, ({"unmapped": unmapped_count} if unmapped_count else {})


def _scope(snapshot: SheetSnapshot) -> str:
    structured = json.dumps(
        [snapshot.spreadsheet_id, snapshot.sheet_name, snapshot.stable_id_column],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return "sheets:" + hashlib.sha256(structured.encode()).hexdigest()


def _row_hash(row: SnapshotRow) -> str:
    canonical = json.dumps(
        dict(row.values), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _snapshot_hash(snapshot: SheetSnapshot) -> str:
    material = {
        "scope": _scope(snapshot),
        "input_rows": snapshot.input_rows,
        "duplicate_ids": list(snapshot.duplicate_ids),
        "missing_id_rows": list(snapshot.missing_id_rows),
        "rows": sorted((row.external_id, _row_hash(row)) for row in snapshot.rows),
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _event_payload(row: SnapshotRow, stage: str, row_hash: str) -> dict[str, str]:
    return {"external_id": row.external_id, "stage": stage, "row_hash": row_hash}


def _payload_hash(payload: dict[str, str]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _identity(
    session: Session,
    workspace_id: UUID,
    scope: str,
    external_id: str,
    kind: str,
    locator: int,
) -> SourceIdentity:
    identity_id = uuid4()
    inserted_id = session.execute(
        insert(SourceIdentity)
        .values(
            id=identity_id,
            workspace_id=workspace_id,
            source_system="google_sheets",
            source_scope=scope,
            entity_kind=kind,
            external_id=external_id,
            metadata_json={"last_locator": locator},
        )
        .on_conflict_do_nothing(
            index_elements=[
                SourceIdentity.workspace_id,
                SourceIdentity.source_system,
                SourceIdentity.source_scope,
                SourceIdentity.entity_kind,
                SourceIdentity.external_id,
            ]
        )
        .returning(SourceIdentity.id)
    ).scalar_one_or_none()
    identity = (
        session.get(SourceIdentity, inserted_id)
        if inserted_id is not None
        else session.scalar(
            select(SourceIdentity).where(
                SourceIdentity.workspace_id == workspace_id,
                SourceIdentity.source_system == "google_sheets",
                SourceIdentity.source_scope == scope,
                SourceIdentity.entity_kind == kind,
                SourceIdentity.external_id == external_id,
            )
        )
    )
    if identity is None:
        raise RuntimeError("identity claim failed")
    identity.metadata_json = {"last_locator": locator}
    # A concurrent transaction may have inserted this row after our transaction
    # started. PostgreSQL's now() is fixed at transaction start, so it can be
    # older than the winner's first_seen_at. Use the wall clock and preserve the
    # persisted lower bound to keep the seen interval valid under that race.
    identity.last_seen_at = func.greatest(
        identity.first_seen_at,
        func.clock_timestamp(),
    )
    return identity


def _domain(row: SnapshotRow) -> str | None:
    website = row.values.get("Website", "").strip()
    if not website:
        return None
    hostname = urlparse(website).hostname
    if hostname is None:
        raise _ReviewRequired()
    try:
        return normalize_domain(hostname)
    except ValueError:
        raise _ReviewRequired() from None


def _lifecycle(stage: str) -> str:
    rank = stage_rank(stage)
    if stage == "won":
        return "customer"
    if rank >= 60:
        return "proposal"
    if rank >= 40:
        return "meeting"
    return "potential"


_LIFECYCLE_ORDER = {"potential": 0, "meeting": 1, "proposal": 2, "customer": 3}


def _lock_account_evidence(
    session: Session,
    workspace_id: UUID,
    *,
    email: str | None,
    domain: str | None,
    normalized_company: str,
) -> None:
    fingerprints = []
    if email:
        fingerprints.append(f"email:{email}")
    if domain:
        fingerprints.append(f"domain-name:{domain}:{normalized_company}")
    for fingerprint in sorted(fingerprints):
        digest = hashlib.sha256(f"{workspace_id}:{fingerprint}".encode()).digest()
        key = int.from_bytes(digest[:8], "big", signed=True)
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def _existing_lead(
    session: Session, identity: SourceIdentity, workspace_id: UUID
) -> Lead | None:
    if identity.canonical_entity_id is None:
        return None
    if identity.canonical_entity_type != "lead":
        raise _ReviewRequired()
    lead = session.get(Lead, identity.canonical_entity_id)
    if lead is None or lead.workspace_id != workspace_id:
        raise _ReviewRequired()
    return lead


def _apply_row(
    session: Session,
    workspace_id: UUID,
    scope: str,
    row: SnapshotRow,
    stage: str,
) -> tuple[str, bool]:
    row_hash = _row_hash(row)
    key_material = f"{scope}\0{row.external_id}\0{row_hash}"
    idempotency_key = (
        "sheets-backfill:" + hashlib.sha256(key_material.encode()).hexdigest()
    )
    payload = _event_payload(row, stage, row_hash)
    payload_hash = _payload_hash(payload)
    event_id = uuid4()
    inserted_id = session.execute(
        insert(IngestEvent)
        .values(
            id=event_id,
            workspace_id=workspace_id,
            source_system="google_sheets",
            source_scope=scope,
            event_type="sheets.account_backfill",
            schema_version=1,
            external_event_id=row.external_id,
            idempotency_key=idempotency_key,
            occurred_at=datetime.now(UTC),
            payload=payload,
            payload_hash=payload_hash,
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
    if inserted_id is None:
        existing = session.execute(
            select(IngestEvent.id, IngestEvent.payload_hash).where(
                IngestEvent.workspace_id == workspace_id,
                IngestEvent.source_system == "google_sheets",
                IngestEvent.idempotency_key == idempotency_key,
            )
        ).one()
        if existing.payload_hash != payload_hash:
            raise _ReviewRequired("idempotency_conflict")
        return "replay", False
    event = session.get(IngestEvent, inserted_id)
    if event is None:
        raise RuntimeError("event claim failed")

    lead_identity = _identity(
        session, workspace_id, scope, row.external_id, "lead", row.locator
    )
    lead = _existing_lead(session, lead_identity, workspace_id)
    if lead is not None:
        try:
            validate_transition(lead.stage, stage)
        except InvalidTransitionError:
            raise _ReviewRequired("invalid_transition") from None
    try:
        account_required = requires_account(
            stage,
            lead.highest_stage_rank if lead is not None else None,
            (lead.account_id is not None) if lead is not None else None,
        )
    except AccountRequirementReviewRequired:
        raise _ReviewRequired("history_required") from None

    account = (
        session.get(Account, lead.account_id)
        if lead is not None and lead.account_id is not None
        else None
    )
    if (
        lead is not None
        and lead.account_id is not None
        and (account is None or account.workspace_id != workspace_id)
    ):
        raise _ReviewRequired()
    contact = (
        session.get(Contact, lead.contact_id)
        if lead is not None and lead.contact_id is not None
        else None
    )
    if (
        lead is not None
        and lead.contact_id is not None
        and (
            contact is None
            or contact.workspace_id != workspace_id
            or account is None
            or contact.account_id != account.id
        )
    ):
        raise _ReviewRequired()
    if account_required:
        account_identity = _identity(
            session, workspace_id, scope, row.external_id, "account", row.locator
        )
        company = row.values.get("Company", "").strip()
        if not company:
            raise _ReviewRequired()
        try:
            normalized_company = normalize_company_name(company)
            email = (
                normalize_email(row.values.get("Email", ""))
                if row.values.get("Email", "").strip()
                else None
            )
            domain = _domain(row)
        except ValueError:
            raise _ReviewRequired() from None

        _lock_account_evidence(
            session,
            workspace_id,
            email=email,
            domain=domain,
            normalized_company=normalized_company,
        )
        candidates: dict[UUID, Account] = {account.id: account} if account else {}
        if account_identity.canonical_entity_id is not None:
            if account_identity.canonical_entity_type != "account":
                raise _ReviewRequired()
            candidate = session.get(Account, account_identity.canonical_entity_id)
            if candidate is None or candidate.workspace_id != workspace_id:
                raise _ReviewRequired()
            candidates[candidate.id] = candidate
        if email:
            for candidate in session.scalars(
                select(Account)
                .join(Contact, Contact.account_id == Account.id)
                .where(
                    Account.workspace_id == workspace_id, Contact.primary_email == email
                )
            ):
                candidates[candidate.id] = candidate
        if domain:
            for candidate in session.scalars(
                select(Account).where(
                    Account.workspace_id == workspace_id,
                    Account.primary_domain == domain,
                    Account.normalized_name == normalized_company,
                )
            ):
                candidates[candidate.id] = candidate
        if len(candidates) > 1:
            raise _ReviewRequired()
        if candidates:
            account = next(iter(candidates.values()))
        else:
            account = Account(
                workspace_id=workspace_id,
                display_name=company,
                normalized_name=normalized_company,
                primary_domain=domain,
                lifecycle_stage=_lifecycle(stage),
                highest_stage_rank=stage_rank(stage),
                sector=row.values.get("Industry", "").strip() or None,
                commercial_vertical=row.values.get("Industry", "").strip() or None,
                source_origin=row.values.get("Source", "").strip() or None,
                source_identity_id=account_identity.id,
            )
            session.add(account)
            session.flush()
        account.highest_stage_rank = max(account.highest_stage_rank, stage_rank(stage))
        desired_lifecycle = _lifecycle(stage)
        if (
            _LIFECYCLE_ORDER[desired_lifecycle]
            > _LIFECYCLE_ORDER[account.lifecycle_stage]
        ):
            account.lifecycle_stage = desired_lifecycle
        account_identity.canonical_entity_type = "account"
        account_identity.canonical_entity_id = account.id
        if email:
            contact = session.scalar(
                select(Contact).where(
                    Contact.workspace_id == workspace_id, Contact.primary_email == email
                )
            )
            if contact is not None and contact.account_id != account.id:
                raise _ReviewRequired()
            if contact is None:
                contact = Contact(
                    workspace_id=workspace_id,
                    account_id=account.id,
                    full_name=row.values.get("Contact Name", "").strip() or None,
                    primary_email=email,
                )
                session.add(contact)
                session.flush()

    if lead is None:
        lead = Lead(
            workspace_id=workspace_id,
            account_id=account.id if account else None,
            contact_id=contact.id if contact else None,
            source_stage_raw=row.values.get("Status", "").strip() or None,
            stage=stage,
            highest_stage_rank=stage_rank(stage),
            sector=row.values.get("Industry", "").strip() or None,
            commercial_vertical=row.values.get("Industry", "").strip() or None,
            source_origin=row.values.get("Source", "").strip() or None,
            source_identity_id=lead_identity.id,
        )
        session.add(lead)
        session.flush()
    else:
        lead.account_id = account.id if account else lead.account_id
        lead.contact_id = contact.id if contact else lead.contact_id
        lead.source_stage_raw = row.values.get("Status", "").strip() or None
        lead.stage = stage
        lead.highest_stage_rank = max(lead.highest_stage_rank, stage_rank(stage))
        lead.sector = row.values.get("Industry", "").strip() or None
        lead.commercial_vertical = row.values.get("Industry", "").strip() or None
        lead.source_origin = row.values.get("Source", "").strip() or None
    lead_identity.canonical_entity_type = "lead"
    lead_identity.canonical_entity_id = lead.id
    session.add(
        Activity(
            workspace_id=workspace_id,
            account_id=account.id if account else None,
            lead_id=lead.id,
            contact_id=contact.id if contact else None,
            activity_type="stage_change",
            occurred_at=event.occurred_at,
            title=f"Stage changed to {stage}",
            summary=stage,
            semantic_fingerprint=row_hash,
            source_system="google_sheets",
            source_identity_id=lead_identity.id,
            ingest_event_id=event.id,
        )
    )
    event.processing_status = "applied"
    event.applied_at = datetime.now(UTC)
    return "imported", account_required


def _advance_checkpoint(
    session: Session, workspace_id: UUID, scope: str, snapshot_hash: str
) -> None:
    now = datetime.now(UTC)
    statement = insert(SyncCheckpoint).values(
        id=uuid4(),
        workspace_id=workspace_id,
        connector="google_sheets",
        source_scope=scope,
        stream="account_backfill",
        cursor_encrypted=snapshot_hash,
        high_watermark_at=now,
        last_success_at=now,
        last_error_redacted=None,
        consecutive_failures=0,
        updated_at=now,
    )
    session.execute(
        statement.on_conflict_do_update(
            index_elements=[
                SyncCheckpoint.workspace_id,
                SyncCheckpoint.connector,
                SyncCheckpoint.source_scope,
                SyncCheckpoint.stream,
            ],
            set_={
                "cursor_encrypted": snapshot_hash,
                "high_watermark_at": now,
                "last_success_at": now,
                "last_error_redacted": None,
                "consecutive_failures": 0,
                "updated_at": now,
            },
        )
    )


def backfill_accounts(
    snapshot: SheetSnapshot,
    *,
    apply: bool = False,
    database_url: str | None = None,
    workspace_id: object | None = None,
    failure_injector: Callable[[str, int], None] | None = None,
) -> BackfillReport:
    snapshot = validate_snapshot(snapshot)
    mapped, unmapped = _classified_rows(snapshot)
    history_required = 0
    account_candidates = 0
    dry_imports = 0
    for _row, stage in mapped:
        try:
            account_candidates += int(requires_account(stage, None, None))
            dry_imports += 1
        except AccountRequirementReviewRequired:
            history_required += 1
    base_conflicts = len(snapshot.missing_id_rows) + history_required
    base_reasons: dict[str, int] = {}
    if snapshot.missing_id_rows:
        base_reasons["missing_stable_id"] = len(snapshot.missing_id_rows)
    if history_required:
        base_reasons["history_required"] = history_required
    if not apply:
        return BackfillReport(
            input_rows=snapshot.input_rows,
            snapshot_rows=len(snapshot.rows),
            imported=dry_imports,
            accounts_created_or_linked=account_candidates,
            duplicates=len(snapshot.duplicate_ids),
            conflicts=base_conflicts,
            unmapped_stages=unmapped,
            review_reasons=base_reasons,
        )
    if not database_url or not database_url.startswith("postgresql+psycopg://"):
        raise ValueError("apply requires an explicit PostgreSQL database_url")
    if type(workspace_id) is not UUID:
        raise ValueError("apply requires an explicit workspace UUID")

    imported = accounts = replay = 0
    conflicts = len(snapshot.missing_id_rows)
    review_reasons = (
        {"missing_stable_id": len(snapshot.missing_id_rows)}
        if snapshot.missing_id_rows
        else {}
    )
    scope = _scope(snapshot)
    engine = create_engine(database_url)
    try:
        with Session(engine) as session, session.begin():
            for index, (row, stage) in enumerate(mapped):
                if failure_injector is not None:
                    failure_injector("before", index)
                try:
                    with session.begin_nested():
                        status, account_touched = _apply_row(
                            session, workspace_id, scope, row, stage
                        )
                except _ReviewRequired as exc:
                    conflicts += 1
                    review_reasons[exc.reason] = review_reasons.get(exc.reason, 0) + 1
                else:
                    if status == "imported":
                        imported += 1
                        accounts += int(account_touched)
                    else:
                        replay += 1
                if failure_injector is not None:
                    failure_injector("after", index)
            _advance_checkpoint(session, workspace_id, scope, _snapshot_hash(snapshot))
    finally:
        engine.dispose()
    return BackfillReport(
        input_rows=snapshot.input_rows,
        snapshot_rows=len(snapshot.rows),
        imported=imported,
        accounts_created_or_linked=accounts,
        duplicates=len(snapshot.duplicate_ids),
        conflicts=conflicts,
        unmapped_stages=unmapped,
        replay_noop=replay,
        applied=True,
        review_reasons=review_reasons,
    )
