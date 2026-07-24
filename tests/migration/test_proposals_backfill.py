from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from src.crm.migration.backfill import backfill_accounts
from src.crm.migration.proposals_backfill import backfill_proposals
from src.crm.migration.sheets_snapshot import save_snapshot, snapshot_sheet
from src.crm.persistence.models import Proposal, ProposalVersion, Workspace

from ._postgres import require_disposable_postgres


ROOT = Path(__file__).resolve().parents[2]


class ProposalFixtureSource:
    def read_values(self, spreadsheet_id, sheet_name):
        return [
            [
                "ID",
                "Company",
                "Status",
                "Proposal Sent",
                "Proposal Status",
                "Proposal Value",
                "Proposal Currency",
            ],
            [
                "missing-value",
                "Northwind",
                "Proposal Sent",
                "2026-01-12",
                "Sent",
                "",
                "EUR",
            ],
            [
                "won",
                "Acme",
                "Proposal Sent",
                "2026-01-10",
                "Won",
                "1250.00",
                "EUR",
            ],
            ["no-proposal", "Beta", "Meeting Booked", "", "", "", "EUR"],
        ]


def proposal_snapshot():
    return snapshot_sheet(
        ProposalFixtureSource(),
        "fixture-spreadsheet",
        "PT Logistics",
        stable_id_column="ID",
    )


def test_dry_run_preserves_missing_values_and_unverified_send_state():
    report = backfill_proposals(proposal_snapshot(), apply=False)

    assert report.safe_dict() == {
        "input_rows": 3,
        "proposal_rows": 2,
        "imported": 2,
        "missing_value": 1,
        "missing_sent_evidence": 2,
        "conflicts": 0,
        "unmatched_account": 0,
        "replay_noop": 0,
        "applied": False,
    }


def test_won_is_imported_as_won_and_never_rewritten_to_meeting_booked():
    report = backfill_proposals(proposal_snapshot(), apply=False)

    assert report.status_counts == {"sent": 1, "won": 1}


def test_cli_is_dry_run_by_default(tmp_path):
    snapshot_path = tmp_path / "proposals.json"
    save_snapshot(proposal_snapshot(), snapshot_path)

    result = subprocess.run(
        [
            sys.executable,
            "scripts/crm_backfill_proposals.py",
            "--snapshot",
            str(snapshot_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["applied"] is False


def test_postgres_apply_and_replay_preserve_status_value_and_identity():
    database_url = require_disposable_postgres()
    workspace_id = uuid4()
    engine = create_engine(database_url)
    with Session(engine) as session, session.begin():
        session.add(
            Workspace(
                id=workspace_id,
                slug=f"proposal-backfill-{workspace_id}",
                name="Proposal Backfill Fixture",
            )
        )

    accounts = backfill_accounts(
        proposal_snapshot(),
        apply=True,
        database_url=database_url,
        workspace_id=workspace_id,
    )
    first = backfill_proposals(
        proposal_snapshot(),
        apply=True,
        database_url=database_url,
        workspace_id=workspace_id,
    )
    replay = backfill_proposals(
        proposal_snapshot(),
        apply=True,
        database_url=database_url,
        workspace_id=workspace_id,
    )

    assert accounts.imported == 3
    assert first.imported == 2
    assert first.unmatched_account == 0
    assert replay.imported == 0
    assert replay.replay_noop == 2
    with Session(engine) as session:
        proposals = list(
            session.scalars(
                select(Proposal)
                .where(Proposal.workspace_id == workspace_id)
                .order_by(Proposal.status)
            )
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(ProposalVersion)
                .join(Proposal, Proposal.id == ProposalVersion.proposal_id)
                .where(Proposal.workspace_id == workspace_id)
            )
            == 2
        )
        assert [proposal.status for proposal in proposals] == ["sent", "won"]
        assert proposals[0].value_state == "missing"
        assert proposals[1].value_state == "candidate"
        assert all(
            proposal.sent_verification_state == "legacy_unverified"
            and proposal.sent_evidence_id is None
            for proposal in proposals
        )
        won_version = session.get(ProposalVersion, proposals[1].selected_version_id)
        assert won_version.one_off_amount == 1250
        assert proposals[1].won_at is not None
    engine.dispose()
