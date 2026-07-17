from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.crm.migration.backfill import backfill_accounts
from src.crm.migration.proposals_backfill import backfill_proposals
from src.crm.migration.sheets_snapshot import snapshot_sheet
from src.crm.persistence.models import Lead, Workspace

from ._postgres import require_disposable_postgres


class ReadOnlySource:
    def __init__(self, values):
        self.values = values

    def read_values(self, spreadsheet_id: str, sheet_name: str):
        return self.values


def _snapshot(values):
    return snapshot_sheet(
        ReadOnlySource(values),
        "spreadsheet-1",
        "PT Logistics",
        stable_id_column="ID",
        fallback_identity_columns=(
            ("Email",),
            ("Phone",),
            ("Website", "Company"),
            ("Company", "Contact"),
        ),
    )


def test_explicit_fallback_identity_is_stable_across_row_movement_and_note_edits():
    header = ["ID", "Company", "Contact", "Email", "Phone", "Website", "notes"]
    source = ReadOnlySource(
        [
            header,
            ["", "Acme", "Ana", "ANA@EXAMPLE.COM", "", "", "first note"],
            ["", "Beta", "Bruno", "", "+351 210 000 000", "", "other note"],
        ]
    )

    first = snapshot_sheet(
        source,
        "spreadsheet-1",
        "PT Logistics",
        stable_id_column="ID",
        fallback_identity_columns=(("Email",), ("Phone",)),
    )
    source.values[1][6] = "changed note"
    source.values[1], source.values[2] = source.values[2], source.values[1]
    second = snapshot_sheet(
        source,
        "spreadsheet-1",
        "PT Logistics",
        stable_id_column="ID",
        fallback_identity_columns=(("Email",), ("Phone",)),
    )

    assert len(first.rows) == 2
    assert {row.external_id for row in first.rows} == {
        row.external_id for row in second.rows
    }
    assert all(row.external_id.startswith("derived:") for row in first.rows)
    assert all(row.values["ID"] == row.external_id for row in first.rows)


def test_fallback_identity_is_scoped_to_the_sheet_source():
    values = [["ID", "Email"], ["", "same@example.com"]]

    left = snapshot_sheet(
        ReadOnlySource(values),
        "spreadsheet-1",
        "Leads",
        stable_id_column="ID",
        fallback_identity_columns=(("Email",),),
    )
    right = snapshot_sheet(
        ReadOnlySource(values),
        "spreadsheet-2",
        "Leads",
        stable_id_column="ID",
        fallback_identity_columns=(("Email",),),
    )

    assert left.rows[0].external_id != right.rows[0].external_id


def test_fallback_identity_keeps_ambiguous_and_unidentifiable_rows_out_of_snapshot():
    snapshot = _snapshot(
        [
            ["ID", "Company", "Contact", "Email", "Phone", "Website", "Stage"],
            ["", "Acme", "Ana", "same@example.com", "", "", "Meeting Booked"],
            ["", "Other", "Other", "same@example.com", "", "", "Meeting Booked"],
            ["", "Only Company", "", "", "", "", "Meeting Booked"],
        ]
    )

    assert snapshot.rows == ()
    assert len(snapshot.duplicate_ids) == 1
    assert snapshot.missing_id_rows == (4,)


def test_fallback_identity_canonicalizes_declared_email_and_phone_semantics():
    email_left = snapshot_sheet(
        ReadOnlySource([["ID", "Email"], ["", "USER@MÜNCHEN.example"]]),
        "spreadsheet-1",
        "Leads",
        stable_id_column="ID",
        fallback_identity_columns=(("Email",),),
    )
    email_right = snapshot_sheet(
        ReadOnlySource([["ID", "Email"], ["", "user@xn--mnchen-3ya.example"]]),
        "spreadsheet-1",
        "Leads",
        stable_id_column="ID",
        fallback_identity_columns=(("Email",),),
    )
    phone_left = snapshot_sheet(
        ReadOnlySource([["ID", "Phone"], ["", "+351 210 000 000"]]),
        "spreadsheet-1",
        "Leads",
        stable_id_column="ID",
        fallback_identity_columns=(("Phone",),),
    )
    phone_right = snapshot_sheet(
        ReadOnlySource([["ID", "Phone"], ["", "+351-210-000-000"]]),
        "spreadsheet-1",
        "Leads",
        stable_id_column="ID",
        fallback_identity_columns=(("Phone",),),
    )

    assert email_left.rows[0].external_id == email_right.rows[0].external_id
    assert phone_left.rows[0].external_id == phone_right.rows[0].external_id


def test_fallback_identity_rejects_company_name_only_policy():
    with pytest.raises(ValueError, match="invalid sheet snapshot input"):
        snapshot_sheet(
            ReadOnlySource([["ID", "Company"], ["", "Acme"]]),
            "spreadsheet-1",
            "Leads",
            stable_id_column="ID",
            fallback_identity_columns=(("Company",),),
        )


def test_duplicate_and_missing_fallback_rows_propagate_to_backfill_conflicts():
    snapshot = _snapshot(
        [
            ["ID", "Company", "Contact", "Email", "Phone", "Website", "Stage"],
            ["", "Acme", "Ana", "same@example.com", "", "", "Meeting Booked"],
            ["", "Other", "Other", "same@example.com", "", "", "Meeting Booked"],
            ["", "Only Company", "", "", "", "", "Meeting Booked"],
        ]
    )

    accounts = backfill_accounts(snapshot, apply=False)
    proposals = backfill_proposals(snapshot, apply=False)

    assert accounts.conflicts == 2
    assert accounts.review_reasons == {
        "duplicate_stable_id": 1,
        "missing_stable_id": 1,
    }
    assert proposals.conflicts == 2


def test_explicit_duplicate_ids_are_conflicts_not_only_duplicate_metrics():
    snapshot = snapshot_sheet(
        ReadOnlySource(
            [
                ["ID", "Company", "Stage"],
                ["duplicate", "Acme", "Meeting Booked"],
                ["duplicate", "Other", "Meeting Booked"],
            ]
        ),
        "spreadsheet-1",
        "PT Logistics",
        stable_id_column="ID",
    )

    accounts = backfill_accounts(snapshot, apply=False)
    proposals = backfill_proposals(snapshot, apply=False)

    assert accounts.duplicates == 1
    assert accounts.conflicts == 1
    assert accounts.review_reasons == {"duplicate_stable_id": 1}
    assert proposals.conflicts == 1


def test_fallback_identity_normalizes_nfkc_equivalent_company_and_contact_values():
    left = _snapshot(
        [
            ["ID", "Company", "Contact", "Email", "Phone", "Website"],
            ["", "ＡＣＭＥ", "Ana", "", "", ""],
        ]
    )
    right = _snapshot(
        [
            ["ID", "Company", "Contact", "Email", "Phone", "Website"],
            ["", "ACME", "Ana", "", "", ""],
        ]
    )

    assert left.rows[0].external_id == right.rows[0].external_id


def test_proposal_backfill_treats_equivalent_stage_alias_columns_as_consistent():
    snapshot = snapshot_sheet(
        ReadOnlySource(
            [
                [
                    "ID",
                    "Company",
                    "Status",
                    "Stage",
                    "Proposal Sent",
                    "Proposal Status",
                ],
                [
                    "proposal-1",
                    "Acme",
                    "Proposal Sent",
                    "proposal_sent",
                    "2026/07/15",
                    "Sent",
                ],
            ]
        ),
        "spreadsheet-1",
        "PT Logistics",
        stable_id_column="ID",
    )

    report = backfill_proposals(snapshot, apply=False)

    assert report.imported == 1
    assert report.conflicts == 0


def test_malformed_proposal_date_never_promotes_account_stage():
    snapshot = _snapshot(
        [
            [
                "ID",
                "Company",
                "Contact",
                "Email",
                "Phone",
                "Website",
                "Stage",
                "Proposal Sent",
                "Proposal Status",
            ],
            [
                "",
                "Acme",
                "Ana",
                "ana@example.com",
                "",
                "",
                "Email Sent",
                "2026/99/99",
                "Sent",
            ],
        ]
    )

    accounts = backfill_accounts(snapshot, apply=False)
    proposals = backfill_proposals(snapshot, apply=False)

    assert accounts.imported == 0
    assert accounts.conflicts == 1
    assert accounts.review_reasons == {"invalid_proposal_artifact": 1}
    assert proposals.imported == 0
    assert proposals.conflicts == 1


def test_known_and_unknown_stage_columns_conflict_even_with_explicit_proposal_status():
    snapshot = _snapshot(
        [
            [
                "ID",
                "Company",
                "Contact",
                "Email",
                "Phone",
                "Website",
                "Status",
                "Stage",
                "Proposal Sent",
                "Proposal Status",
            ],
            [
                "",
                "Acme",
                "Ana",
                "ana@example.com",
                "",
                "",
                "Meeting Booked",
                "Email Sent",
                "2026/07/15",
                "Sent",
            ],
        ]
    )

    accounts = backfill_accounts(snapshot, apply=False)
    proposals = backfill_proposals(snapshot, apply=False)

    assert accounts.imported == 0
    assert accounts.review_reasons == {"conflicting_columns": 1}
    assert proposals.imported == 0
    assert proposals.conflicts == 1


def test_real_sheet_stage_and_contact_headers_are_explicitly_supported():
    snapshot = _snapshot(
        [
            ["ID", "Company", "Contact", "Email", "Phone", "Website", "Stage"],
            ["", "Acme", "Ana", "ana@example.com", "", "", "Meeting Booked"],
            ["", "Beta", "Bruno", "bruno@example.com", "", "", "Proposal Sent"],
        ]
    )

    report = backfill_accounts(snapshot, apply=False)

    assert report.imported == 2
    assert report.accounts_created_or_linked == 2
    assert report.unmapped_stages == {}


def test_conflicting_status_and_stage_headers_require_review():
    snapshot = _snapshot(
        [
            [
                "ID",
                "Company",
                "Contact",
                "Email",
                "Phone",
                "Website",
                "Status",
                "Stage",
            ],
            [
                "",
                "Acme",
                "Ana",
                "ana@example.com",
                "",
                "",
                "Contacted",
                "Meeting Booked",
            ],
        ]
    )

    report = backfill_accounts(snapshot, apply=False)

    assert report.imported == 0
    assert report.conflicts == 1
    assert report.review_reasons == {"conflicting_columns": 1}


def test_real_sheet_proposal_date_format_and_stage_header_are_supported():
    snapshot = _snapshot(
        [
            [
                "ID",
                "Company",
                "Contact",
                "Email",
                "Phone",
                "Website",
                "Stage",
                "Proposal Sent",
                "Proposal Status",
                "Proposal Value",
            ],
            [
                "",
                "Acme",
                "Ana",
                "ana@example.com",
                "",
                "",
                "Proposal Sent",
                "2026/07/15",
                "Sent",
                "",
            ],
        ]
    )

    accounts = backfill_accounts(snapshot, apply=False)
    proposals = backfill_proposals(snapshot, apply=False)

    assert accounts.imported == 1
    assert proposals.proposal_rows == 1
    assert proposals.imported == 1
    assert proposals.conflicts == 0
    assert proposals.missing_value == 1


def test_legacy_proposal_artifact_promotes_effective_stage_for_account_backfill():
    snapshot = _snapshot(
        [
            [
                "ID",
                "Company",
                "Contact",
                "Email",
                "Phone",
                "Website",
                "Stage",
                "Proposal Sent",
                "Proposal Status",
                "Proposal Value",
            ],
            [
                "",
                "Acme",
                "Ana",
                "ana@example.com",
                "",
                "",
                "",
                "2026/07/15",
                "Sent",
                "",
            ],
        ]
    )

    accounts = backfill_accounts(snapshot, apply=False)

    assert accounts.imported == 1
    assert accounts.accounts_created_or_linked == 1
    assert accounts.unmapped_stages == {}


def test_promoted_legacy_proposal_stage_applies_without_reparsing_unknown_raw_stage():
    database_url = require_disposable_postgres()
    workspace_id = uuid4()
    engine = create_engine(database_url)
    snapshot = _snapshot(
        [
            [
                "ID",
                "Company",
                "Contact",
                "Email",
                "Phone",
                "Website",
                "Stage",
                "Proposal Sent",
            ],
            [
                "",
                "Acme",
                "Ana",
                "ana@example.com",
                "",
                "",
                "Email Sent",
                "2026/07/15",
            ],
        ]
    )
    try:
        with Session(engine) as session, session.begin():
            session.add(
                Workspace(
                    id=workspace_id,
                    slug=f"real-sheet-compat-{workspace_id}",
                    name="Real Sheet Compatibility",
                )
            )

        report = backfill_accounts(
            snapshot,
            apply=True,
            database_url=database_url,
            workspace_id=workspace_id,
        )

        assert report.imported == 1
        assert report.accounts_created_or_linked == 1
        with Session(engine) as session:
            lead = session.execute(
                select(Lead).where(Lead.workspace_id == workspace_id)
            ).scalar_one()
            assert lead.stage == "proposal_sent"
            assert lead.source_stage_raw == "Email Sent"
    finally:
        engine.dispose()
