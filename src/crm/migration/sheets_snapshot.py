"""Immutable, bounded snapshots of read-only Google Sheet values."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Protocol, Sequence
import unicodedata

MAX_COLUMNS = 128
MAX_ROWS = 100_000
MAX_CELL_LENGTH = 4_096
MAX_IDENTITY_LENGTH = 512
MAX_SNAPSHOT_BYTES = 50_000_000


class SheetValuesSource(Protocol):
    def read_values(
        self, spreadsheet_id: str, sheet_name: str
    ) -> Sequence[Sequence[object]]: ...


@dataclass(frozen=True, slots=True)
class SnapshotRow:
    spreadsheet_id: str
    sheet_name: str
    stable_id_column: str
    external_id: str
    locator: int
    values: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))

    @property
    def identity(self) -> tuple[str, str, str, str]:
        return (
            self.spreadsheet_id,
            self.sheet_name,
            self.stable_id_column,
            self.external_id,
        )


@dataclass(frozen=True, slots=True)
class SheetSnapshot:
    spreadsheet_id: str
    sheet_name: str
    stable_id_column: str
    input_rows: int
    rows: tuple[SnapshotRow, ...]
    duplicate_ids: tuple[str, ...] = ()
    missing_id_rows: tuple[int, ...] = ()


def _invalid_input() -> ValueError:
    return ValueError("invalid sheet snapshot input")


def _invalid_file() -> ValueError:
    return ValueError("invalid snapshot file")


def _safe_text(
    value: object,
    *,
    max_length: int,
    nonblank: bool = False,
    allow_layout_controls: bool = False,
) -> str:
    if value is None:
        text = ""
    elif type(value) in {str, int, bool}:
        text = str(value).strip()
    elif type(value) is float and math.isfinite(value):
        text = str(value).strip()
    else:
        raise _invalid_input()
    if allow_layout_controls:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    if len(text) > max_length or (nonblank and not text):
        raise _invalid_input()
    if any(
        unicodedata.category(character) in {"Cc", "Cf"}
        and not (allow_layout_controls and character in {"\n", "\t"})
        for character in text
    ):
        raise _invalid_input()
    return text


def _identity_text(value: object) -> str:
    return _safe_text(value, max_length=MAX_IDENTITY_LENGTH, nonblank=True)


def snapshot_sheet(
    source: SheetValuesSource,
    spreadsheet_id: str,
    sheet_name: str,
    *,
    stable_id_column: str,
) -> SheetSnapshot:
    try:
        spreadsheet_id = _identity_text(spreadsheet_id)
        sheet_name = _identity_text(sheet_name)
        stable_id_column = _identity_text(stable_id_column)
        values = list(source.read_values(spreadsheet_id, sheet_name))
        if len(values) > MAX_ROWS + 1:
            raise _invalid_input()
        if not values:
            return _validate_snapshot(
                SheetSnapshot(spreadsheet_id, sheet_name, stable_id_column, 0, ())
            )
        header_row = values[0]
        if (
            type(header_row) not in {list, tuple}
            or not 1 <= len(header_row) <= MAX_COLUMNS
        ):
            raise _invalid_input()
        headers = tuple(
            _safe_text(value, max_length=255, nonblank=True) for value in header_row
        )
        if len(set(headers)) != len(headers) or headers.count(stable_id_column) != 1:
            raise _invalid_input()

        candidates: list[SnapshotRow] = []
        missing: list[int] = []
        captured_bytes = len(
            json.dumps(
                [spreadsheet_id, sheet_name, stable_id_column, headers],
                ensure_ascii=False,
            ).encode("utf-8")
        )
        for row_number, raw in enumerate(values[1:], start=2):
            if type(raw) not in {list, tuple} or len(raw) > len(headers):
                raise _invalid_input()
            cells = [
                _safe_text(
                    value,
                    max_length=MAX_CELL_LENGTH,
                    allow_layout_controls=True,
                )
                for value in raw
            ]
            cells.extend([""] * (len(headers) - len(cells)))
            mapped = dict(zip(headers, cells, strict=True))
            captured_bytes += (
                len(
                    json.dumps(
                        mapped, ensure_ascii=False, separators=(",", ":")
                    ).encode("utf-8")
                )
                + 512
            )
            if captured_bytes > MAX_SNAPSHOT_BYTES:
                raise _invalid_input()
            external_id = mapped[stable_id_column]
            if not external_id:
                missing.append(row_number)
                continue
            if len(external_id) > MAX_IDENTITY_LENGTH:
                raise _invalid_input()
            candidates.append(
                SnapshotRow(
                    spreadsheet_id=spreadsheet_id,
                    sheet_name=sheet_name,
                    stable_id_column=stable_id_column,
                    external_id=external_id,
                    locator=row_number,
                    values=mapped,
                )
            )
        counts: dict[str, int] = {}
        for row in candidates:
            counts[row.external_id] = counts.get(row.external_id, 0) + 1
        duplicates = tuple(sorted(key for key, count in counts.items() if count > 1))
        rows = tuple(row for row in candidates if row.external_id not in duplicates)
        return _validate_snapshot(
            SheetSnapshot(
                spreadsheet_id,
                sheet_name,
                stable_id_column,
                len(values) - 1,
                rows,
                duplicates,
                tuple(missing),
            )
        )
    except ValueError:
        raise _invalid_input() from None
    except (TypeError, OverflowError, RecursionError):
        raise _invalid_input() from None


def _snapshot_payload(snapshot: SheetSnapshot) -> dict[str, object]:
    return {
        "spreadsheet_id": snapshot.spreadsheet_id,
        "sheet_name": snapshot.sheet_name,
        "stable_id_column": snapshot.stable_id_column,
        "input_rows": snapshot.input_rows,
        "duplicate_ids": list(snapshot.duplicate_ids),
        "missing_id_rows": list(snapshot.missing_id_rows),
        "rows": [
            {
                "external_id": row.external_id,
                "locator": row.locator,
                "values": dict(row.values),
            }
            for row in snapshot.rows
        ],
    }


def _snapshot_text(snapshot: SheetSnapshot) -> str:
    return json.dumps(_snapshot_payload(snapshot), ensure_ascii=False, indent=2) + "\n"


def _enforce_snapshot_size(snapshot: SheetSnapshot) -> None:
    size = 1  # Final newline written by save_snapshot.
    encoder = json.JSONEncoder(ensure_ascii=False, indent=2)
    for chunk in encoder.iterencode(_snapshot_payload(snapshot)):
        size += len(chunk.encode("utf-8"))
        if size > MAX_SNAPSHOT_BYTES:
            raise _invalid_file()


def save_snapshot(snapshot: SheetSnapshot, path: str | Path) -> None:
    # Revalidation makes this serialization boundary reject forged dataclasses too.
    validated = _validate_snapshot(snapshot)
    serialized = _snapshot_text(validated)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(serialized)
    os.chmod(target, 0o600)


def _validate_snapshot(snapshot: object) -> SheetSnapshot:
    if type(snapshot) is not SheetSnapshot:
        raise _invalid_file()
    try:
        spreadsheet_id = _identity_text(snapshot.spreadsheet_id)
        sheet_name = _identity_text(snapshot.sheet_name)
        stable_id_column = _identity_text(snapshot.stable_id_column)
        if (
            type(snapshot.input_rows) is not int
            or not 0 <= snapshot.input_rows <= MAX_ROWS
        ):
            raise _invalid_file()
        if (
            type(snapshot.rows) is not tuple
            or type(snapshot.duplicate_ids) is not tuple
        ):
            raise _invalid_file()
        if type(snapshot.missing_id_rows) is not tuple:
            raise _invalid_file()
        duplicate_ids = tuple(_identity_text(value) for value in snapshot.duplicate_ids)
        if (
            len(set(duplicate_ids)) != len(duplicate_ids)
            or tuple(sorted(duplicate_ids)) != duplicate_ids
        ):
            raise _invalid_file()
        seen_ids: set[str] = set()
        seen_locators: set[int] = set()
        rows: list[SnapshotRow] = []
        for row in snapshot.rows:
            if type(row) is not SnapshotRow or not isinstance(row.values, Mapping):
                raise _invalid_file()
            external_id = _identity_text(row.external_id)
            if external_id in seen_ids or external_id in duplicate_ids:
                raise _invalid_file()
            if (
                row.spreadsheet_id != spreadsheet_id
                or row.sheet_name != sheet_name
                or row.stable_id_column != stable_id_column
                or type(row.locator) is not int
                or not 2 <= row.locator <= snapshot.input_rows + 1
                or row.locator in seen_locators
                or len(row.values) > MAX_COLUMNS
            ):
                raise _invalid_file()
            values: dict[str, str] = {}
            for key, value in row.values.items():
                header = _safe_text(key, max_length=255, nonblank=True)
                if header in values or type(value) is not str:
                    raise _invalid_file()
                values[header] = _safe_text(
                    value,
                    max_length=MAX_CELL_LENGTH,
                    allow_layout_controls=True,
                )
            if values.get(stable_id_column) != external_id:
                raise _invalid_file()
            seen_ids.add(external_id)
            seen_locators.add(row.locator)
            rows.append(
                SnapshotRow(
                    spreadsheet_id=spreadsheet_id,
                    sheet_name=sheet_name,
                    stable_id_column=stable_id_column,
                    external_id=external_id,
                    locator=row.locator,
                    values=values,
                )
            )
        missing = snapshot.missing_id_rows
        if any(
            type(locator) is not int
            or not 2 <= locator <= snapshot.input_rows + 1
            or locator in seen_locators
            for locator in missing
        ) or len(set(missing)) != len(missing):
            raise _invalid_file()
        if len(rows) + len(missing) + 2 * len(duplicate_ids) > snapshot.input_rows:
            raise _invalid_file()
        canonical = SheetSnapshot(
            spreadsheet_id,
            sheet_name,
            stable_id_column,
            snapshot.input_rows,
            tuple(rows),
            duplicate_ids,
            missing,
        )
        _enforce_snapshot_size(canonical)
        return canonical
    except ValueError:
        raise _invalid_file() from None
    except (TypeError, OverflowError, RecursionError):
        raise _invalid_file() from None


def validate_snapshot(snapshot: object) -> SheetSnapshot:
    """Return a detached, bounded and structurally immutable canonical snapshot."""
    return _validate_snapshot(snapshot)


def load_snapshot(path: str | Path) -> SheetSnapshot:
    try:
        target = Path(path)
        if target.stat().st_size > MAX_SNAPSHOT_BYTES:
            raise _invalid_file()
        payload = json.loads(target.read_text())
        if type(payload) is not dict or set(payload) != {
            "spreadsheet_id",
            "sheet_name",
            "stable_id_column",
            "input_rows",
            "duplicate_ids",
            "missing_id_rows",
            "rows",
        }:
            raise _invalid_file()
        if (
            type(payload["rows"]) is not list
            or type(payload["duplicate_ids"]) is not list
        ):
            raise _invalid_file()
        if type(payload["missing_id_rows"]) is not list:
            raise _invalid_file()
        rows = []
        for item in payload["rows"]:
            if type(item) is not dict or set(item) != {
                "external_id",
                "locator",
                "values",
            }:
                raise _invalid_file()
            rows.append(
                SnapshotRow(
                    spreadsheet_id=payload["spreadsheet_id"],
                    sheet_name=payload["sheet_name"],
                    stable_id_column=payload["stable_id_column"],
                    external_id=item["external_id"],
                    locator=item["locator"],
                    values=item["values"],
                )
            )
        return _validate_snapshot(
            SheetSnapshot(
                spreadsheet_id=payload["spreadsheet_id"],
                sheet_name=payload["sheet_name"],
                stable_id_column=payload["stable_id_column"],
                input_rows=payload["input_rows"],
                rows=tuple(rows),
                duplicate_ids=tuple(payload["duplicate_ids"]),
                missing_id_rows=tuple(payload["missing_id_rows"]),
            )
        )
    except (
        OSError,
        json.JSONDecodeError,
        UnicodeError,
        ValueError,
        TypeError,
        KeyError,
    ):
        raise _invalid_file() from None
