from __future__ import annotations

import json

import pytest

from src.crm.migration.backfill import backfill_accounts
from src.crm.migration.sheets_snapshot import (
    SheetSnapshot,
    load_snapshot,
    save_snapshot,
    snapshot_sheet,
)
from src.crm.connectors.sheets_source import READ_ONLY_SCOPES, GoogleSheetsSource


class ReadOnlySource:
    def __init__(self, values):
        self.values = values
        self.calls = []

    def read_values(self, spreadsheet_id: str, sheet_name: str):
        self.calls.append((spreadsheet_id, sheet_name))
        return self.values


def test_snapshot_identity_survives_row_movement():
    source = ReadOnlySource(
        [
            ["ID", "Company", "Status"],
            ["lead-2", "Beta", "Contacted"],
            ["lead-1", "Acme", "Meeting Booked"],
        ]
    )

    first = snapshot_sheet(source, "spreadsheet-1", "Leads", stable_id_column="ID")
    source.values[1], source.values[2] = source.values[2], source.values[1]
    second = snapshot_sheet(source, "spreadsheet-1", "Leads", stable_id_column="ID")

    assert {row.external_id for row in first.rows} == {"lead-1", "lead-2"}
    assert {row.identity for row in first.rows} == {row.identity for row in second.rows}
    assert {row.external_id: row.locator for row in first.rows} != {
        row.external_id: row.locator for row in second.rows
    }
    assert source.calls == [("spreadsheet-1", "Leads"), ("spreadsheet-1", "Leads")]


def test_snapshot_row_values_are_immutable_after_capture():
    snapshot = snapshot_sheet(
        ReadOnlySource([["ID", "Status"], ["lead-1", "Contacted"]]),
        "spreadsheet-1",
        "Leads",
        stable_id_column="ID",
    )

    with pytest.raises(TypeError):
        snapshot.rows[0].values["Status"] = "tampered"  # type: ignore[index]

    assert snapshot.rows[0].values["Status"] == "Contacted"


def test_snapshot_reports_repeated_and_missing_stable_ids_without_guessing():
    source = ReadOnlySource(
        [
            ["ID", "Company", "Status"],
            ["dup", "Same Name", "Meeting Booked"],
            ["dup", "Same Name", "Meeting Booked"],
            ["", "No ID", "Contacted"],
        ]
    )

    snapshot = snapshot_sheet(source, "spreadsheet-1", "Leads", stable_id_column="ID")

    assert snapshot.input_rows == 3
    assert snapshot.duplicate_ids == ("dup",)
    assert snapshot.missing_id_rows == (4,)
    assert snapshot.rows == ()


def test_google_source_exposes_only_read_scope_and_read_operation():
    assert READ_ONLY_SCOPES == (
        "https://www.googleapis.com/auth/spreadsheets.readonly",
    )
    public_operations = {
        name for name in dir(GoogleSheetsSource) if not name.startswith("_")
    }
    assert public_operations == {"read_values"}


def test_snapshot_json_round_trip_preserves_canonical_identity(tmp_path):
    snapshot = snapshot_sheet(
        ReadOnlySource([["ID", "Status"], ["lead-1", "Meeting Booked"]]),
        "spreadsheet-1",
        "Leads",
        stable_id_column="ID",
    )
    path = tmp_path / "snapshot.json"

    save_snapshot(snapshot, path)

    assert load_snapshot(path) == snapshot


def test_empty_sheet_is_a_valid_zero_row_snapshot():
    snapshot = snapshot_sheet(
        ReadOnlySource([]), "spreadsheet-1", "Leads", stable_id_column="ID"
    )

    assert snapshot.input_rows == 0
    assert snapshot.rows == ()


@pytest.mark.parametrize(
    "values",
    [
        [["ID", "", "Status"]],
        [["ID", "Status", "Status"]],
        [["ID", "Status"], ["one", "new", "unexpected"]],
        [["ID", "Status"], {"not": "a row"}],
        [["ID", "Sta\x00tus"]],
        [["ID", "Status"], ["one", "new\u200b"]],
    ],
)
def test_snapshot_rejects_malformed_shapes_and_unsafe_text_generically(values):
    marker = repr(values)

    with pytest.raises(ValueError, match="invalid sheet snapshot input") as exc_info:
        snapshot_sheet(
            ReadOnlySource(values),
            "spreadsheet-1",
            "Leads",
            stable_id_column="ID",
        )

    assert marker not in str(exc_info.value)


def test_load_snapshot_fully_revalidates_identity_and_metadata(tmp_path):
    path = tmp_path / "forged.json"
    path.write_text(
        json.dumps(
            {
                "spreadsheet_id": "spreadsheet-1",
                "sheet_name": "Leads",
                "stable_id_column": "ID",
                "input_rows": 2,
                "duplicate_ids": [],
                "missing_id_rows": [],
                "rows": [
                    {"external_id": "same", "locator": 2, "values": {"ID": "same"}},
                    {"external_id": "same", "locator": 3, "values": {"ID": "forged"}},
                ],
            }
        )
    )

    with pytest.raises(ValueError, match="invalid snapshot file"):
        load_snapshot(path)


def test_processing_boundary_rejects_forged_mutable_snapshot_rows():
    snapshot = snapshot_sheet(
        ReadOnlySource([["ID", "Status"], ["one", "Contacted"]]),
        "fixture-spreadsheet",
        "PT Logistics",
        stable_id_column="ID",
    )
    forged = SheetSnapshot(
        snapshot.spreadsheet_id,
        snapshot.sheet_name,
        snapshot.stable_id_column,
        snapshot.input_rows,
        list(snapshot.rows),  # type: ignore[arg-type]
        snapshot.duplicate_ids,
        snapshot.missing_id_rows,
    )

    with pytest.raises(ValueError, match="invalid snapshot"):
        backfill_accounts(forged)


def test_capture_and_save_enforce_aggregate_snapshot_size(monkeypatch, tmp_path):
    monkeypatch.setattr("src.crm.migration.sheets_snapshot.MAX_SNAPSHOT_BYTES", 100)
    source = ReadOnlySource([["ID", "Status"], ["one", "Contacted"]])

    with pytest.raises(ValueError, match="invalid sheet snapshot input"):
        snapshot_sheet(
            source,
            "fixture-spreadsheet",
            "PT Logistics",
            stable_id_column="ID",
        )

    snapshot = SheetSnapshot(
        "fixture-spreadsheet",
        "PT Logistics",
        "ID",
        1,
        (),
    )
    with pytest.raises(ValueError, match="invalid snapshot file"):
        save_snapshot(snapshot, tmp_path / "too-large.json")
