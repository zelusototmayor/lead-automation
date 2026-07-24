"""Aggregate, redacted parity checks between a Sheet snapshot and PostgreSQL."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from src.crm.domain.stage_policy import (
    AccountRequirementReviewRequired,
    requires_account,
)
from src.crm.migration.backfill import (
    _LIFECYCLE_ORDER,
    _classified_rows,
    _lifecycle,
    _scope,
)
from src.crm.migration.sheets_snapshot import SheetSnapshot, validate_snapshot
from src.crm.persistence.models import Account, Lead, SourceIdentity


@dataclass(frozen=True, slots=True)
class CompareReport:
    input_rows: int
    snapshot_rows: int
    expected_imports: int
    matched_leads: int
    matched_accounts: int
    missing_leads: int
    missing_accounts: int
    extra_leads: int
    stage_mismatches: int
    account_association_mismatches: int
    source_field_mismatches: int
    account_state_mismatches: int
    duplicates: int
    conflicts: int
    unmapped_stages: dict[str, int] = field(default_factory=dict)
    parity: bool = False

    def safe_dict(self) -> dict[str, object]:
        return {
            "input_rows": self.input_rows,
            "snapshot_rows": self.snapshot_rows,
            "expected_imports": self.expected_imports,
            "matched_leads": self.matched_leads,
            "matched_accounts": self.matched_accounts,
            "missing_leads": self.missing_leads,
            "missing_accounts": self.missing_accounts,
            "extra_leads": self.extra_leads,
            "stage_mismatches": self.stage_mismatches,
            "account_association_mismatches": self.account_association_mismatches,
            "source_field_mismatches": self.source_field_mismatches,
            "account_state_mismatches": self.account_state_mismatches,
            "duplicates": self.duplicates,
            "conflicts": self.conflicts,
            "unmapped_stages": dict(self.unmapped_stages),
            "parity": self.parity,
        }


def _source_fields_match(row, lead: Lead, account: Account | None) -> bool:
    industry = row.values.get("Industry", "").strip() or None
    origin = row.values.get("Source", "").strip() or None
    if (
        lead.sector != industry
        or lead.commercial_vertical != industry
        or lead.source_origin != origin
    ):
        return False
    if account is not None and (
        account.sector != industry
        or account.commercial_vertical != industry
        or account.source_origin != origin
        or account.display_name != row.values.get("Company", "").strip()
    ):
        return False
    return True


def compare_legacy(
    snapshot: SheetSnapshot,
    *,
    database_url: str,
    workspace_id: UUID,
) -> CompareReport:
    """Read PostgreSQL and return only aggregate parity counts."""
    snapshot = validate_snapshot(snapshot)
    if not database_url.startswith("postgresql+psycopg://"):
        raise ValueError("compare requires an explicit PostgreSQL database_url")
    if type(workspace_id) is not UUID:
        raise ValueError("compare requires an explicit workspace UUID")

    mapped, unmapped, classification_reasons = _classified_rows(snapshot)
    rows_by_external_id = {row.external_id: (row, stage) for row, stage in mapped}
    matched_leads = matched_accounts = 0
    missing_leads = missing_accounts = 0
    stage_mismatches = association_mismatches = source_mismatches = 0
    account_state_mismatches = 0
    history_conflicts = 0
    expected_imports = 0
    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            current_external_ids = {row.external_id for row in snapshot.rows} | set(
                snapshot.duplicate_ids
            )
            extra_query = (
                select(func.count())
                .select_from(SourceIdentity)
                .where(
                    SourceIdentity.workspace_id == workspace_id,
                    SourceIdentity.source_system == "google_sheets",
                    SourceIdentity.source_scope == _scope(snapshot),
                    SourceIdentity.entity_kind == "lead",
                )
            )
            if current_external_ids:
                extra_query = extra_query.where(
                    SourceIdentity.external_id.not_in(current_external_ids)
                )
            extra_leads = session.scalar(extra_query) or 0
            identities = session.scalars(
                select(SourceIdentity).where(
                    SourceIdentity.workspace_id == workspace_id,
                    SourceIdentity.source_system == "google_sheets",
                    SourceIdentity.source_scope == _scope(snapshot),
                    SourceIdentity.entity_kind == "lead",
                    SourceIdentity.external_id.in_(rows_by_external_id),
                )
            )
            identities_by_external_id = {
                identity.external_id: identity for identity in identities
            }
            for external_id, (row, stage) in rows_by_external_id.items():
                identity = identities_by_external_id.get(external_id)
                lead = (
                    session.get(Lead, identity.canonical_entity_id)
                    if identity is not None
                    and identity.canonical_entity_type == "lead"
                    and identity.canonical_entity_id is not None
                    else None
                )
                try:
                    account_required = requires_account(
                        stage,
                        lead.highest_stage_rank if lead is not None else None,
                        (lead.account_id is not None) if lead is not None else None,
                    )
                except AccountRequirementReviewRequired:
                    history_conflicts += 1
                    continue
                expected_imports += 1
                if lead is None or lead.workspace_id != workspace_id:
                    missing_leads += 1
                    if account_required:
                        missing_accounts += 1
                    continue
                matched_leads += 1
                if lead.stage != stage:
                    stage_mismatches += 1

                account = (
                    session.get(Account, lead.account_id)
                    if lead.account_id is not None
                    else None
                )
                valid_account = (
                    account is not None and account.workspace_id == workspace_id
                )
                if account_required:
                    if not valid_account:
                        missing_accounts += 1
                        association_mismatches += 1
                    else:
                        matched_accounts += 1
                elif lead.account_id is not None and not valid_account:
                    association_mismatches += 1
                if not _source_fields_match(
                    row, lead, account if valid_account else None
                ):
                    source_mismatches += 1
                expected_lifecycle = _lifecycle(stage)
                if (
                    valid_account
                    and account is not None
                    and (
                        account.highest_stage_rank < lead.highest_stage_rank
                        or _LIFECYCLE_ORDER[account.lifecycle_stage]
                        < _LIFECYCLE_ORDER[expected_lifecycle]
                    )
                ):
                    account_state_mismatches += 1
    finally:
        engine.dispose()

    conflicts = (
        len(snapshot.missing_id_rows)
        + history_conflicts
        + sum(classification_reasons.values())
    )
    parity = (
        missing_leads == 0
        and missing_accounts == 0
        and stage_mismatches == 0
        and association_mismatches == 0
        and source_mismatches == 0
        and account_state_mismatches == 0
        and extra_leads == 0
        and conflicts == 0
        and len(snapshot.duplicate_ids) == 0
        and not unmapped
    )
    return CompareReport(
        input_rows=snapshot.input_rows,
        snapshot_rows=len(snapshot.rows),
        expected_imports=expected_imports,
        matched_leads=matched_leads,
        matched_accounts=matched_accounts,
        missing_leads=missing_leads,
        missing_accounts=missing_accounts,
        extra_leads=extra_leads,
        stage_mismatches=stage_mismatches,
        account_association_mismatches=association_mismatches,
        source_field_mismatches=source_mismatches,
        account_state_mismatches=account_state_mismatches,
        duplicates=len(snapshot.duplicate_ids),
        conflicts=conflicts,
        unmapped_stages=unmapped,
        parity=parity,
    )
