from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import os
from pathlib import Path
import subprocess
import sys
from uuid import UUID, uuid4

import pytest
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import create_engine, event as sqlalchemy_event, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from src.crm.ingestion.contracts import EventEnvelope


REPO_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_CONFIG = REPO_ROOT / "migrations" / "alembic.ini"
EXPECTED_TABLES = {
    "workspaces",
    "source_identities",
    "ingest_events",
    "sync_checkpoints",
}


def _database_url() -> str:
    value = os.environ.get("DATABASE_URL")
    if not value or not value.startswith("postgresql+psycopg://"):
        pytest.skip("requires disposable PostgreSQL")
    return value


@pytest.fixture(scope="module")
def engine():
    database_url = _database_url()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    upgrade = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_CONFIG), "upgrade", "head"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert upgrade.returncode == 0, upgrade.stderr
    db_engine = create_engine(database_url)
    yield db_engine
    db_engine.dispose()


@pytest.fixture(autouse=True)
def clean_database(request: pytest.FixtureRequest) -> None:
    if "engine" not in request.fixturenames:
        return
    engine = request.getfixturevalue("engine")
    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE sync_checkpoints, ingest_events, source_identities, workspaces CASCADE"
            )
        )


def envelope(
    *,
    system: str = "gmail",
    facts: dict | None = None,
    event_type: str = "message.received",
):
    from src.crm.ingestion.contracts import EventEnvelope

    return EventEnvelope.model_validate(
        {
            "schema_version": 1,
            "event_type": event_type,
            "source": {
                "system": system,
                "scope": "inbox-a",
                "external_event_id": "evt-1",
            },
            "occurred_at": "2026-07-15T10:00:00+00:00",
            "subject": {"kind": "message", "external_id": "msg-1"},
            "account_hint": {"domain": "example.invalid"},
            "facts": facts or {"nested": {"b": 2, "a": 1}, "status": "new"},
            "evidence": [{"type": "email_message", "external_id": "ref-1"}],
        }
    )


def create_workspace(session: Session, *, slug: str = "workspace-a") -> UUID:
    from src.crm.persistence.models import Workspace

    workspace = Workspace(slug=slug, name=slug)
    session.add(workspace)
    session.flush()
    return workspace.id


def test_contract_is_strict_canonical_and_secret_safe() -> None:
    from src.crm.ingestion.contracts import EventEnvelope

    first = envelope(facts={"z": 1, "a": {"y": 2, "x": 3}})
    second = envelope(facts={"a": {"x": 3, "y": 2}, "z": 1})
    assert first.canonical_json() == second.canonical_json()
    assert first.payload_hash() == second.payload_hash()
    assert len(first.payload_hash()) == 64

    marker = "private-person@example.invalid"
    with pytest.raises(ValidationError) as exc_info:
        EventEnvelope.model_validate(
            {
                "schema_version": 1,
                "event_type": "message.received",
                "source": {"system": "gmail", "scope": "scope", "unexpected": marker},
                "occurred_at": "2026-07-15T10:00:00",
                "subject": {"kind": "message", "external_id": "msg"},
                "facts": {"bad": object()},
                "evidence": [],
            }
        )
    assert marker not in str(exc_info.value)
    assert marker not in repr(exc_info.value.errors())
    assert marker not in exc_info.value.json()


@pytest.mark.parametrize(
    "mutation",
    [
        {"event_type": ""},
        {"occurred_at": datetime(2026, 7, 15, 10, 0)},
        {"schema_version": 2},
        {"source": {"system": "unknown", "scope": "scope"}},
    ],
)
def test_contract_rejects_invalid_envelopes_without_echoing_input(
    mutation: dict,
) -> None:
    from src.crm.ingestion.contracts import EventEnvelope

    data = envelope().model_dump(mode="python")
    data.update(mutation)
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(data)


def test_exact_replay_and_key_order_variation_are_duplicates(engine) -> None:
    from src.crm.ingestion.checkpoints import record_ingest_event
    from src.crm.persistence.models import IngestEvent

    with Session(engine) as session, session.begin():
        workspace_id = create_workspace(session)
        first = record_ingest_event(session, workspace_id, "transport-key", envelope())
        replay = record_ingest_event(
            session,
            workspace_id,
            "transport-key",
            envelope(facts={"status": "new", "nested": {"a": 1, "b": 2}}),
        )
        assert first.duplicate is False
        assert replay.duplicate is True
        assert replay.event_id == first.event_id
        assert len(session.scalars(select(IngestEvent)).all()) == 1


def test_semantic_change_raises_generic_conflict_and_preserves_original(engine) -> None:
    from src.crm.ingestion.checkpoints import (
        IdempotencyConflictError,
        record_ingest_event,
    )
    from src.crm.persistence.models import IngestEvent

    marker = "private-person@example.invalid"
    with Session(engine) as session, session.begin():
        workspace_id = create_workspace(session)
        original = record_ingest_event(session, workspace_id, marker, envelope())
        with pytest.raises(IdempotencyConflictError) as exc_info:
            record_ingest_event(
                session,
                workspace_id,
                marker,
                envelope(facts={"status": "changed", "pii": marker}),
            )
        rendered = str(exc_info.value)
        assert rendered == "idempotency key already records a different event"
        assert marker not in rendered
        persisted = session.scalar(
            select(IngestEvent).where(IngestEvent.id == original.event_id)
        )
        assert persisted is not None
        assert persisted.payload["facts"]["status"] == "new"


def test_identity_scope_and_transport_key_boundaries(engine) -> None:
    from src.crm.ingestion.checkpoints import record_ingest_event
    from src.crm.persistence.models import IngestEvent

    with Session(engine) as session, session.begin():
        workspace_a = create_workspace(session, slug="a")
        workspace_b = create_workspace(session, slug="b")
        results = [
            record_ingest_event(session, workspace_a, "same", envelope(system="gmail")),
            record_ingest_event(session, workspace_b, "same", envelope(system="gmail")),
            record_ingest_event(
                session, workspace_a, "same", envelope(system="manual")
            ),
            record_ingest_event(
                session, workspace_a, "different", envelope(system="gmail")
            ),
        ]
        assert len({result.event_id for result in results}) == 4
        assert len(session.scalars(select(IngestEvent)).all()) == 4


def test_concurrent_same_key_insert_returns_one_deterministic_row(engine) -> None:
    from src.crm.ingestion.checkpoints import record_ingest_event
    from src.crm.persistence.models import IngestEvent

    with Session(engine) as setup, setup.begin():
        workspace_id = create_workspace(setup)

    factory = sessionmaker(engine, expire_on_commit=False)

    def insert_once() -> tuple[UUID, bool]:
        with factory() as session, session.begin():
            result = record_ingest_event(
                session, workspace_id, "racing-key", envelope()
            )
            return result.event_id, result.duplicate

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: insert_once(), range(2)))

    assert results[0][0] == results[1][0]
    assert sorted(result[1] for result in results) == [False, True]
    with Session(engine) as session:
        assert len(session.scalars(select(IngestEvent)).all()) == 1
        assert session.execute(text("SELECT 1")).scalar_one() == 1


def test_database_constraints_are_enforced(engine) -> None:
    from src.crm.persistence.models import (
        IngestEvent,
        SourceIdentity,
        SyncCheckpoint,
        Workspace,
    )

    with Session(engine) as session, session.begin():
        workspace_id = create_workspace(session)
        session.add(
            SourceIdentity(
                workspace_id=workspace_id,
                source_system="gmail",
                entity_kind="message",
                source_scope="scope",
                external_id="id",
                canonical_entity_type="contact",
                canonical_entity_id=None,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()

    with Session(engine) as session, session.begin():
        workspace_id = create_workspace(session)
        session.add(
            SyncCheckpoint(
                workspace_id=workspace_id,
                connector="gmail",
                source_scope="s",
                stream="m",
                consecutive_failures=-1,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()

    with Session(engine) as session, session.begin():
        session.add(
            IngestEvent(
                workspace_id=uuid4(),
                source_system="gmail",
                source_scope="s",
                event_type="x",
                schema_version=1,
                idempotency_key="k",
                occurred_at=datetime.now(UTC),
                payload={},
                payload_hash="0" * 64,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()

    with Session(engine) as session, session.begin():
        session.add(Workspace(slug="", name="invalid"))
        with pytest.raises(IntegrityError):
            session.flush()


def test_alembic_environment_registers_models_without_database_io() -> None:
    script = """
import importlib.util
from pathlib import Path
import sqlalchemy
from sqlalchemy.engine import Engine


def forbidden(*args, **kwargs):
    raise AssertionError("unexpected database activity")


sqlalchemy.create_engine = forbidden
Engine.connect = forbidden
spec = importlib.util.spec_from_file_location("imported_env", Path("migrations/env.py"))
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)
assert set(module.target_metadata.tables) == {
    "workspaces", "source_identities", "ingest_events", "sync_checkpoints"
}
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_migration_lifecycle_schema_and_metadata_from_foreign_cwd(
    engine, tmp_path
) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    command = [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_CONFIG)]

    for args in (("current",), ("downgrade", "base"), ("upgrade", "head")):
        result = subprocess.run(
            command + list(args),
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert _database_url() not in result.stdout + result.stderr
        if args == ("current",):
            assert "0001" in result.stdout
        if args == ("downgrade", "base"):
            assert not EXPECTED_TABLES.intersection(inspect(engine).get_table_names())

    inspector = inspect(engine)
    assert EXPECTED_TABLES <= set(inspector.get_table_names())
    parity = subprocess.run(
        command + ["check"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert parity.returncode == 0, parity.stderr
    assert "No new upgrade operations detected" in parity.stdout
    assert _database_url() not in parity.stdout + parity.stderr
    assert {column["name"] for column in inspector.get_columns("ingest_events")} >= {
        "id",
        "workspace_id",
        "source_system",
        "source_scope",
        "event_type",
        "schema_version",
        "external_event_id",
        "idempotency_key",
        "occurred_at",
        "received_at",
        "payload",
        "payload_hash",
        "processing_status",
        "attempt_count",
        "last_error_redacted",
        "next_attempt_at",
        "applied_at",
        "correlation_id",
        "causation_id",
    }

    from src.crm.persistence.base import Base
    import src.crm.persistence.models  # noqa: F401

    assert EXPECTED_TABLES == set(Base.metadata.tables)
    migration_text = (
        REPO_ROOT / "migrations/versions/0001_identity_events_checkpoints.py"
    ).read_text()
    assert "create_all" not in migration_text
    assert "down_revision: str | None = None" in migration_text


def _assert_sanitized_validation_error(
    exc: ValidationError, marker: str, expected_safe_tokens: tuple[str | int, ...]
) -> None:
    rendered = (str(exc), repr(exc.errors()), exc.json())
    assert all(marker not in value for value in rendered)
    assert all(error.get("input") is None for error in exc.errors())
    assert any("<redacted>" in error["loc"] for error in exc.errors())
    assert any(
        all(token in error["loc"] for token in expected_safe_tokens)
        for error in exc.errors()
    )


@pytest.mark.parametrize("location", ["top_level", "source", "facts"])
def test_all_python_validation_entrypoints_redact_caller_controlled_error_locations(
    location: str,
) -> None:
    marker = "PRIVATE-LOC-MARKER"
    data = envelope().model_dump(mode="python")
    if location == "top_level":
        data[marker] = "value"
        expected_safe_tokens: tuple[str | int, ...] = ()
    elif location == "source":
        data["source"][marker] = "value"
        expected_safe_tokens = ("source",)
    else:
        data["facts"][marker] = object()
        expected_safe_tokens = ("facts",)

    validators = (
        lambda: EventEnvelope(**data),
        lambda: TypeAdapter(EventEnvelope).validate_python(data),
        lambda: EventEnvelope.model_validate(data),
    )
    for validate in validators:
        with pytest.raises(ValidationError) as exc_info:
            validate()
        _assert_sanitized_validation_error(exc_info.value, marker, expected_safe_tokens)


@pytest.mark.parametrize("location", ["top_level", "source"])
def test_json_validation_entrypoint_redacts_caller_controlled_error_locations(
    location: str,
) -> None:
    import json

    marker = "PRIVATE-JSON-LOC-MARKER"
    data = envelope().model_dump(mode="json")
    if location == "top_level":
        data[marker] = "value"
        expected_safe_tokens: tuple[str | int, ...] = ()
    else:
        data["source"][marker] = "value"
        expected_safe_tokens = ("source",)

    with pytest.raises(ValidationError) as exc_info:
        EventEnvelope.model_validate_json(json.dumps(data))
    _assert_sanitized_validation_error(exc_info.value, marker, expected_safe_tokens)


def test_normal_validation_errors_retain_actionable_schema_locations_and_numeric_indices() -> (
    None
):
    data = envelope().model_dump(mode="python")
    data["source"]["system"] = "invalid-system"
    data["evidence"][0]["type"] = "invalid-evidence-type"

    with pytest.raises(ValidationError) as exc_info:
        EventEnvelope.model_validate(data)

    errors = exc_info.value.errors()
    assert any(
        error["loc"] == ("source", "system") and error["type"] == "literal_error"
        for error in errors
    )
    assert any(
        error["loc"] == ("evidence", 0, "type") and error["type"] == "literal_error"
        for error in errors
    )
    assert all(error.get("input") is None for error in errors)


def test_approved_v1_endpoint_example_validates_exactly_and_hashes_stably() -> None:
    import copy

    approved = {
        "schema_version": 1,
        "event_type": "meeting.completed",
        "source": {
            "system": "granola",
            "scope": "team-notes",
            "external_event_id": "meeting-42",
        },
        "occurred_at": "2026-07-15T12:00:00Z",
        "subject": {"kind": "meeting", "external_id": "meeting-42"},
        "account_hint": {
            "account_id": "9d8d8e89-bceb-47c4-9e36-c7dd61b6a3ee",
            "contact_email": "buyer@example.invalid",
            "domain": "example.invalid",
            "company_name": "Example GmbH",
        },
        "facts": {"outcome": "follow_up", "attendees": 3},
        "evidence": [
            {
                "type": "meeting_note",
                "external_id": "note-42",
                "uri": "https://example.invalid/notes/42",
                "content_hash": "sha256:0123456789abcdef",
            }
        ],
        "correlation_id": "80b047e7-540f-49be-94af-27c47a6cb60f",
        "causation_id": "7ec378aa-e367-4661-844e-bd858e78f321",
    }
    parsed = EventEnvelope.model_validate(approved)
    reordered = {key: approved[key] for key in reversed(approved)}
    assert (
        parsed.payload_hash() == EventEnvelope.model_validate(reordered).payload_hash()
    )
    assert parsed.persistence_payload()["account_hint"] == approved["account_hint"]

    for path, extra in (
        ("account_hint", {"name": "invented"}),
        ("evidence", {"kind": "invented"}),
    ):
        invalid = copy.deepcopy(approved)
        target = invalid[path] if path == "account_hint" else invalid[path][0]
        target.update(extra)
        with pytest.raises(ValidationError):
            EventEnvelope.model_validate(invalid)


def test_equivalent_aware_timestamps_have_identical_payload_hashes() -> None:
    first = envelope()
    data = first.model_dump(mode="python")
    data["occurred_at"] = "2026-07-15T12:00:00+02:00"
    second = EventEnvelope.model_validate(data)
    assert second.occurred_at.tzinfo == UTC
    assert first.payload_hash() == second.payload_hash()


@pytest.mark.parametrize(
    "target,field",
    [
        ("source", "system"),
        ("envelope", "occurred_at"),
        ("envelope", "account_hint"),
        ("subject", "external_id"),
        ("account_hint", "domain"),
        ("evidence", "external_id"),
    ],
)
def test_contract_fields_reject_assignment_without_echoing_attempted_value(
    target: str, field: str
) -> None:
    marker = "PRIVATE-ASSIGNMENT-MARKER"
    parsed = envelope()
    model = parsed if target == "envelope" else getattr(parsed, target)
    if target == "evidence":
        model = parsed.evidence[0]

    with pytest.raises((AttributeError, TypeError, ValidationError)) as exc_info:
        setattr(model, field, marker)

    assert marker not in str(exc_info.value)
    assert marker not in repr(exc_info.value)


def test_contract_fields_reject_deletion_generically() -> None:
    parsed = envelope()
    with pytest.raises((AttributeError, TypeError)) as exc_info:
        del parsed.event_type
    assert "message.received" not in str(exc_info.value)
    assert "message.received" not in repr(exc_info.value)


def test_evidence_collection_and_nested_evidence_are_immutable() -> None:
    parsed = envelope()
    assert isinstance(parsed.evidence, tuple)
    with pytest.raises(AttributeError):
        parsed.evidence.append(parsed.evidence[0])
    with pytest.raises((AttributeError, TypeError, ValidationError)):
        parsed.evidence[0].external_id = "replacement"


def _set_path(data: dict, path: tuple[str | int, ...], value: str) -> None:
    target = data
    for token in path[:-1]:
        target = target[token]
    target[path[-1]] = value


@pytest.mark.parametrize("control", ["\x00", "\n", "\u200b", "\u202e"])
@pytest.mark.parametrize(
    "path",
    [
        ("event_type",),
        ("source", "scope"),
        ("source", "external_event_id"),
        ("subject", "external_id"),
        ("account_hint", "contact_email"),
        ("account_hint", "domain"),
        ("account_hint", "company_name"),
        ("evidence", 0, "external_id"),
        ("evidence", 0, "uri"),
        ("evidence", 0, "content_hash"),
    ],
)
def test_schema_owned_identifier_strings_reject_control_and_format_characters(
    path: tuple[str | int, ...], control: str
) -> None:
    marker = f"PRIVATE{control}CONTROL"
    data = envelope().model_dump(mode="python")
    _set_path(data, path, marker)
    with pytest.raises(ValidationError) as exc_info:
        EventEnvelope.model_validate(data)
    rendered = (
        str(exc_info.value),
        repr(exc_info.value.errors()),
        exc_info.value.json(),
    )
    assert all(marker not in value for value in rendered)


def test_arbitrary_business_fact_text_may_contain_newlines() -> None:
    parsed = envelope(facts={"notes": "line one\nline two"})
    assert parsed.facts["notes"] == "line one\nline two"


@pytest.mark.parametrize(
    "facts,leaked_number",
    [
        ({731_991: object()}, "731991"),
        ({"nested": {845_227: object()}}, "845227"),
        ({"evidence": {956_333: object()}}, "956333"),
    ],
)
def test_numeric_caller_mapping_keys_are_redacted_from_all_error_forms(
    facts: dict, leaked_number: str
) -> None:
    data = envelope().model_dump(mode="python")
    data["facts"] = facts
    with pytest.raises(ValidationError) as exc_info:
        EventEnvelope.model_validate(data)
    rendered = (
        str(exc_info.value),
        repr(exc_info.value.errors()),
        exc_info.value.json(),
    )
    assert all(leaked_number not in value for value in rendered)
    assert all(
        token == "<redacted>"
        for error in exc_info.value.errors()
        for token in error["loc"]
        if isinstance(token, int)
    )


def test_only_real_evidence_sequence_indexes_are_preserved_in_error_locations() -> None:
    data = envelope().model_dump(mode="python")
    data["evidence"][0]["type"] = "invalid"
    data["facts"] = {"evidence": [{"bad": object()}]}
    with pytest.raises(ValidationError) as exc_info:
        EventEnvelope.model_validate(data)
    locations = [error["loc"] for error in exc_info.value.errors()]
    assert ("evidence", 0, "type") in locations
    assert all(0 not in location for location in locations if location[0] == "facts")


def test_canonical_event_payload_has_documented_utf8_byte_boundary() -> None:
    from src.crm.ingestion.contracts import MAX_CANONICAL_EVENT_BYTES

    overhead = len(envelope(facts={"blob": ""}).canonical_json().encode("utf-8"))
    boundary = envelope(facts={"blob": "x" * (MAX_CANONICAL_EVENT_BYTES - overhead)})
    assert len(boundary.canonical_json().encode("utf-8")) == MAX_CANONICAL_EVENT_BYTES

    data = boundary.model_dump(mode="python")
    data["facts"]["blob"] += "é"
    with pytest.raises(ValidationError) as exc_info:
        EventEnvelope.model_validate(data)
    assert str(MAX_CANONICAL_EVENT_BYTES + 2) not in str(exc_info.value)


@pytest.mark.parametrize("repository", ["single", "batch"])
@pytest.mark.parametrize("mutation", ["non_json", "numeric_key", "oversized"])
def test_repository_revalidates_mutated_facts_before_any_database_statement(
    engine, repository: str, mutation: str
) -> None:
    from src.crm.ingestion.checkpoints import (
        CheckpointKey,
        EventToPersist,
        InvalidIngestionInputError,
        persist_event_batch_and_advance_checkpoint,
        record_ingest_event,
    )
    from src.crm.ingestion.contracts import MAX_CANONICAL_EVENT_BYTES
    from src.crm.persistence.models import IngestEvent, SyncCheckpoint

    parsed = envelope()
    if mutation == "non_json":
        parsed.facts["bad"] = object()
    elif mutation == "numeric_key":
        parsed.facts[772_661] = "bad"
    else:
        parsed.facts["huge"] = "x" * MAX_CANONICAL_EVENT_BYTES

    statements: list[str] = []
    with Session(engine) as session, session.begin():
        workspace_id = create_workspace(session)

        def capture_statement(*args) -> None:
            statements.append(args[2])

        sqlalchemy_event.listen(engine, "before_cursor_execute", capture_statement)
        try:
            with pytest.raises(InvalidIngestionInputError) as exc_info:
                if repository == "single":
                    record_ingest_event(session, workspace_id, "mutated", parsed)
                else:
                    persist_event_batch_and_advance_checkpoint(
                        session,
                        CheckpointKey(workspace_id, "gmail", "scope", "messages"),
                        "cursor",
                        [EventToPersist("mutated", parsed)],
                    )
        finally:
            sqlalchemy_event.remove(engine, "before_cursor_execute", capture_statement)

        assert str(exc_info.value) == "invalid ingestion input"
        assert statements == []
        assert session.execute(text("SELECT 1")).scalar_one() == 1
        assert session.scalar(select(IngestEvent)) is None
        assert session.scalar(select(SyncCheckpoint)) is None


def test_repository_hashes_a_fresh_validated_copy_after_semantic_facts_mutation(
    engine,
) -> None:
    from src.crm.ingestion.checkpoints import record_ingest_event
    from src.crm.persistence.models import IngestEvent

    parsed = envelope()
    parsed.facts["status"] = "mutated-but-valid"
    with Session(engine) as session, session.begin():
        workspace_id = create_workspace(session)
        result = record_ingest_event(session, workspace_id, "semantic-mutation", parsed)
        row = session.get(IngestEvent, result.event_id)
        assert row.payload["facts"]["status"] == "mutated-but-valid"
        assert (
            row.payload_hash == EventEnvelope.model_validate(row.payload).payload_hash()
        )


def test_exact_replay_with_equivalent_timezone_representation_is_duplicate(
    engine,
) -> None:
    from src.crm.ingestion.checkpoints import record_ingest_event

    shifted_data = envelope().model_dump(mode="python")
    shifted_data["occurred_at"] = "2026-07-15T12:00:00+02:00"
    shifted = EventEnvelope.model_validate(shifted_data)
    with Session(engine) as session, session.begin():
        workspace_id = create_workspace(session)
        first = record_ingest_event(session, workspace_id, "same-instant", envelope())
        replay = record_ingest_event(session, workspace_id, "same-instant", shifted)
        assert replay.event_id == first.event_id
        assert replay.duplicate is True


def test_record_ingest_event_never_commits(engine) -> None:
    from src.crm.ingestion.checkpoints import record_ingest_event
    from src.crm.persistence.models import IngestEvent, Workspace

    session = Session(engine)
    try:
        workspace_id = create_workspace(session)
        record_ingest_event(session, workspace_id, "rollback-me", envelope())
        session.rollback()
    finally:
        session.close()
    with Session(engine) as verification:
        assert verification.scalar(select(Workspace)) is None
        assert verification.scalar(select(IngestEvent)) is None


def test_workspace_delete_is_restricted_and_provenance_rows_survive(engine) -> None:
    from src.crm.ingestion.checkpoints import (
        CheckpointKey,
        EventToPersist,
        persist_event_batch_and_advance_checkpoint,
    )
    from src.crm.persistence.models import (
        IngestEvent,
        SourceIdentity,
        SyncCheckpoint,
        Workspace,
    )

    with Session(engine) as session, session.begin():
        workspace_id = create_workspace(session)
        session.add(
            SourceIdentity(
                workspace_id=workspace_id,
                source_system="gmail",
                entity_kind="message",
                source_scope="scope",
                external_id="identity",
            )
        )
        persist_event_batch_and_advance_checkpoint(
            session,
            CheckpointKey(
                workspace_id=workspace_id,
                connector="gmail",
                source_scope="scope",
                stream="messages",
            ),
            "encrypted-cursor",
            [EventToPersist("event", envelope())],
        )

    with Session(engine) as session:
        session.delete(session.get(Workspace, workspace_id))
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()
        assert session.get(Workspace, workspace_id) is not None
        assert (
            session.scalar(
                select(SourceIdentity).where(
                    SourceIdentity.workspace_id == workspace_id
                )
            )
            is not None
        )
        assert (
            session.scalar(
                select(IngestEvent).where(IngestEvent.workspace_id == workspace_id)
            )
            is not None
        )
        assert (
            session.scalar(
                select(SyncCheckpoint).where(
                    SyncCheckpoint.workspace_id == workspace_id
                )
            )
            is not None
        )


def test_source_identity_seen_interval_constraint(engine) -> None:
    from src.crm.persistence.models import SourceIdentity

    with Session(engine) as session, session.begin():
        workspace_id = create_workspace(session)
        session.add(
            SourceIdentity(
                workspace_id=workspace_id,
                source_system="gmail",
                entity_kind="message",
                source_scope="scope",
                external_id="identity",
                first_seen_at=datetime(2026, 7, 16, tzinfo=UTC),
                last_seen_at=datetime(2026, 7, 15, tzinfo=UTC),
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
