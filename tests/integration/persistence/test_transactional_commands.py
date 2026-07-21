from __future__ import annotations

from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from src.crm.persistence.models import AuditEvent, Lead, OutboxEvent, Workspace
from src.crm.persistence.unit_of_work import SqlAlchemyUnitOfWork
from src.crm.services.command_service import (
    CommandAuthorizationError,
    CommandConflictError,
    HumanCommandPrincipal,
    HumanCommandService,
    TransitionLeadCommand,
)
from tests.migration._postgres import cleanup_workspace, require_disposable_postgres


def _seed(engine):
    workspace_id, lead_id, actor_id = uuid4(), uuid4(), uuid4()
    with Session(engine) as session, session.begin():
        session.add(
            Workspace(id=workspace_id, slug=f"commands-{workspace_id}", name="Commands")
        )
        session.flush()
        session.add(Lead(id=lead_id, workspace_id=workspace_id))
    return workspace_id, lead_id, actor_id


def _command(workspace_id, lead_id, **changes):
    values = dict(
        command_id=uuid4(),
        workspace_id=workspace_id,
        lead_id=lead_id,
        target_stage="contacted",
        expected_version=1,
        reviewed_correction=False,
    )
    values.update(changes)
    return TransitionLeadCommand(**values)


def _principal(workspace_id, actor_id):
    return HumanCommandPrincipal(
        actor_id=actor_id,
        workspace_id=workspace_id,
        permissions=frozenset({"crm:lead-stage:write"}),
    )


def test_rollback_persists_neither_domain_outbox_nor_audit_and_commit_persists_all():
    engine = create_engine(require_disposable_postgres())
    factory = sessionmaker(engine, expire_on_commit=False)
    workspace_id, lead_id, actor_id = _seed(engine)
    command = _command(workspace_id, lead_id)
    try:
        with pytest.raises(RuntimeError, match="after command"):
            with SqlAlchemyUnitOfWork(factory) as uow:
                HumanCommandService(uow).transition_lead(
                    _principal(workspace_id, actor_id), command
                )
                raise RuntimeError("after command")

        with Session(engine) as session:
            assert session.get(Lead, lead_id).stage == "new"
            assert session.scalar(select(func.count(OutboxEvent.id))) == 0
            assert session.scalar(select(func.count(AuditEvent.id))) == 0

        with SqlAlchemyUnitOfWork(factory) as uow:
            result = HumanCommandService(uow).transition_lead(
                _principal(workspace_id, actor_id), command
            )
            assert result.replayed is False
            uow.commit()

        with Session(engine) as session:
            lead = session.get(Lead, lead_id)
            outbox = session.scalar(select(OutboxEvent))
            audit = session.scalar(select(AuditEvent))
            assert (lead.stage, lead.version) == ("contacted", 2)
            assert outbox.status == "pending" and outbox.published_at is None
            assert audit.action == "lead.stage_transitioned"
            assert outbox.command_id == audit.command_id == command.command_id
            assert outbox.payload == {
                "lead_id": str(lead_id),
                "stage": "contacted",
                "version": 2,
            }
            assert audit.details == {
                "from_stage": "new",
                "reviewed_correction": False,
                "to_stage": "contacted",
                "version": 2,
            }
    finally:
        cleanup_workspace(engine, workspace_id)
        engine.dispose()


def test_replay_is_idempotent_stale_version_is_generic_and_scope_is_exact():
    engine = create_engine(require_disposable_postgres())
    factory = sessionmaker(engine, expire_on_commit=False)
    workspace_id, lead_id, actor_id = _seed(engine)
    command = _command(workspace_id, lead_id)
    try:
        with SqlAlchemyUnitOfWork(factory) as uow:
            HumanCommandService(uow).transition_lead(
                _principal(workspace_id, actor_id), command
            )
            uow.commit()
        with SqlAlchemyUnitOfWork(factory) as uow:
            replay = HumanCommandService(uow).transition_lead(
                _principal(workspace_id, actor_id), command
            )
            assert replay.replayed is True
            uow.commit()

        with Session(engine) as session:
            assert session.get(Lead, lead_id).version == 2
            assert session.scalar(select(func.count(OutboxEvent.id))) == 1
            assert session.scalar(select(func.count(AuditEvent.id))) == 1

        changed = _command(
            workspace_id,
            lead_id,
            command_id=command.command_id,
            target_stage="replied",
        )
        stale = _command(workspace_id, lead_id, target_stage="replied")
        for rejected in (changed, stale):
            with (
                SqlAlchemyUnitOfWork(factory) as uow,
                pytest.raises(CommandConflictError, match="^command conflict$"),
            ):
                HumanCommandService(uow).transition_lead(
                    _principal(workspace_id, actor_id), rejected
                )

        other_workspace = uuid4()
        wrong_principals = (
            HumanCommandPrincipal(
                actor_id, other_workspace, frozenset({"crm:lead-stage:write"})
            ),
            HumanCommandPrincipal(actor_id, workspace_id, frozenset({"crm:write"})),
        )
        for principal in wrong_principals:
            with (
                SqlAlchemyUnitOfWork(factory) as uow,
                pytest.raises(CommandAuthorizationError, match="^command forbidden$"),
            ):
                HumanCommandService(uow).transition_lead(principal, command)
    finally:
        cleanup_workspace(engine, workspace_id)
        engine.dispose()


def test_account_required_stage_rejects_accountless_lead_atomically():
    engine = create_engine(require_disposable_postgres())
    factory = sessionmaker(engine, expire_on_commit=False)
    workspace_id, lead_id, actor_id = _seed(engine)
    try:
        with (
            SqlAlchemyUnitOfWork(factory) as uow,
            pytest.raises(CommandConflictError, match="^command conflict$"),
        ):
            HumanCommandService(uow).transition_lead(
                _principal(workspace_id, actor_id),
                _command(workspace_id, lead_id, target_stage="meeting_booked"),
            )

        with Session(engine) as session:
            lead = session.get(Lead, lead_id)
            assert (lead.stage, lead.version, lead.account_id) == ("new", 1, None)
            assert session.scalar(select(func.count(OutboxEvent.id))) == 0
            assert session.scalar(select(func.count(AuditEvent.id))) == 0
    finally:
        cleanup_workspace(engine, workspace_id)
        engine.dispose()


def test_principal_may_hold_additional_permissions():
    engine = create_engine(require_disposable_postgres())
    factory = sessionmaker(engine, expire_on_commit=False)
    workspace_id, lead_id, actor_id = _seed(engine)
    try:
        principal = HumanCommandPrincipal(
            actor_id,
            workspace_id,
            frozenset({"crm:lead-stage:write", "crm:read"}),
        )
        with SqlAlchemyUnitOfWork(factory) as uow:
            result = HumanCommandService(uow).transition_lead(
                principal, _command(workspace_id, lead_id)
            )
            uow.commit()
        assert result.version == 2
    finally:
        cleanup_workspace(engine, workspace_id)
        engine.dispose()


def test_same_command_id_is_isolated_between_workspaces():
    engine = create_engine(require_disposable_postgres())
    factory = sessionmaker(engine, expire_on_commit=False)
    first_workspace, first_lead, first_actor = _seed(engine)
    second_workspace, second_lead, second_actor = _seed(engine)
    command_id = uuid4()
    try:
        for workspace_id, lead_id, actor_id in (
            (first_workspace, first_lead, first_actor),
            (second_workspace, second_lead, second_actor),
        ):
            with SqlAlchemyUnitOfWork(factory) as uow:
                HumanCommandService(uow).transition_lead(
                    _principal(workspace_id, actor_id),
                    _command(
                        workspace_id,
                        lead_id,
                        command_id=command_id,
                    ),
                )
                uow.commit()
        with Session(engine) as session:
            assert session.scalar(select(func.count(OutboxEvent.id))) == 2
            assert session.scalar(select(func.count(AuditEvent.id))) == 2
    finally:
        cleanup_workspace(engine, first_workspace)
        cleanup_workspace(engine, second_workspace)
        engine.dispose()


@pytest.mark.parametrize("flag", [0, 1, "false", None])
def test_review_flag_requires_an_exact_boolean(flag):
    engine = create_engine(require_disposable_postgres())
    factory = sessionmaker(engine)
    workspace_id, lead_id, actor_id = _seed(engine)
    try:
        with (
            SqlAlchemyUnitOfWork(factory) as uow,
            pytest.raises(CommandConflictError, match="^command conflict$"),
        ):
            HumanCommandService(uow).transition_lead(
                _principal(workspace_id, actor_id),
                _command(workspace_id, lead_id, reviewed_correction=flag),
            )
    finally:
        cleanup_workspace(engine, workspace_id)
        engine.dispose()


def test_concurrent_replay_has_one_domain_change_outbox_and_audit():
    engine = create_engine(require_disposable_postgres())
    factory = sessionmaker(engine, expire_on_commit=False)
    workspace_id, lead_id, actor_id = _seed(engine)
    command = _command(workspace_id, lead_id)

    def apply():
        with SqlAlchemyUnitOfWork(factory) as uow:
            result = HumanCommandService(uow).transition_lead(
                _principal(workspace_id, actor_id), command
            )
            uow.commit()
            return result.replayed

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(lambda _: apply(), range(2))) == [False, True]
        with Session(engine) as session:
            assert session.get(Lead, lead_id).version == 2
            assert session.scalar(select(func.count(OutboxEvent.id))) == 1
            assert session.scalar(select(func.count(AuditEvent.id))) == 1
    finally:
        cleanup_workspace(engine, workspace_id)
        engine.dispose()


def test_database_enforces_append_only_audit_and_bounded_payloads():
    engine = create_engine(require_disposable_postgres())
    factory = sessionmaker(engine, expire_on_commit=False)
    workspace_id, lead_id, actor_id = _seed(engine)
    command = _command(workspace_id, lead_id)
    try:
        with SqlAlchemyUnitOfWork(factory) as uow:
            HumanCommandService(uow).transition_lead(
                _principal(workspace_id, actor_id), command
            )
            uow.commit()
        with Session(engine) as session:
            with pytest.raises(IntegrityError):
                session.execute(
                    update(AuditEvent)
                    .where(AuditEvent.command_id == command.command_id)
                    .values(details={"secret": "changed"})
                )
                session.commit()
        with Session(engine) as session:
            outbox = session.scalar(select(OutboxEvent))
            outbox.payload = {"value": "x" * 5000}
            with pytest.raises(IntegrityError):
                session.commit()
    finally:
        cleanup_workspace(engine, workspace_id)
        engine.dispose()


def test_replay_is_bound_to_the_original_actor_without_duplicate_writes():
    engine = create_engine(require_disposable_postgres())
    factory = sessionmaker(engine, expire_on_commit=False)
    workspace_id, lead_id, actor_id = _seed(engine)
    command = _command(workspace_id, lead_id)
    try:
        with SqlAlchemyUnitOfWork(factory) as uow:
            HumanCommandService(uow).transition_lead(
                _principal(workspace_id, actor_id), command
            )
            uow.commit()

        with (
            SqlAlchemyUnitOfWork(factory) as uow,
            pytest.raises(CommandConflictError, match="^command conflict$"),
        ):
            HumanCommandService(uow).transition_lead(
                _principal(workspace_id, uuid4()), command
            )

        with Session(engine) as session:
            assert session.get(Lead, lead_id).version == 2
            assert session.scalar(select(func.count(OutboxEvent.id))) == 1
            assert session.scalar(select(func.count(AuditEvent.id))) == 1
    finally:
        cleanup_workspace(engine, workspace_id)
        engine.dispose()
