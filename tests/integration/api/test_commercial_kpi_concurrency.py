"""Real independent PostgreSQL transactions; no mocked locking semantics."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.crm.persistence.models import AuditEvent, SyncCheckpoint
from src.crm.services.commercial_kpi_reconcile import start_run
from src.crm.services.commercial_kpi_service import (
    KPIConflict,
    SourceIndex,
    aggregate,
    assess,
)
from tests.integration.api import test_commercial_kpi_service as _kpi_fixtures
from tests.integration.api.test_commercial_kpi_service import (
    WHEN,
    proposal,
    source,
)

kpi_db = _kpi_fixtures.kpi_db


def test_parallel_same_source_command_replays_one_persisted_revision(kpi_db):
    session, workspace, actor, lead = kpi_db
    call = source(session, workspace, lead, summary="Synthetic logistical call.")
    context = SourceIndex(session, workspace).context(call.id)
    command = uuid4()
    session.commit()
    barrier = Barrier(4)

    def writer(_):
        with Session(session.get_bind()) as worker, worker.begin():
            barrier.wait(timeout=10)
            return assess(
                worker,
                workspace,
                actor,
                command_id=command,
                expected_source_digest=context["source_digest"],
                expected_context_digest=context["context_digest"],
                proposal=proposal(context, relevant="no"),
            )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(writer, range(4)))
    assert len({result["audit_id"] for result in results}) == 1
    assert sum(not result["replayed"] for result in results) == 1
    assert (
        session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.workspace_id == workspace)
        )
        == 1
    )


@pytest.mark.parametrize("iteration", range(5))
def test_concurrent_first_start_has_one_owner_and_one_conflict(kpi_db, iteration):
    session, workspace, actor, lead = kpi_db
    source(session, workspace, lead)
    session.commit()
    barrier = Barrier(2)

    def start(entrypoint):
        try:
            with Session(session.get_bind()) as worker, worker.begin():
                barrier.wait(timeout=10)
                start_run(
                    worker,
                    workspace,
                    actor,
                    run_id=uuid4(),
                    entrypoint=entrypoint,
                    anchor_date=WHEN.date(),
                    expected_checkpoint=None,
                )
                return "started"
        except KPIConflict:
            return "conflict"
        except Exception as exc:  # noqa: BLE001 - unexpected errors make the asserted result fail.
            return type(exc).__name__

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(start, ["sales_13h", "sales_1730"]))
    assert sorted(outcomes) == ["conflict", "started"]
    assert (
        session.scalar(
            select(func.count())
            .select_from(SyncCheckpoint)
            .where(SyncCheckpoint.workspace_id == workspace)
        )
        == 1
    )


@pytest.mark.parametrize(
    "marker", ["voided", "test_scaffold", "non_phone", "cancelled_before_occurrence"]
)
def test_explicit_non_attempt_marker_removes_only_that_attempt_immediately(
    kpi_db, marker
):
    session, workspace, actor, lead = kpi_db
    first = source(session, workspace, lead, summary="Synthetic completed-call report.")
    later = source(
        session,
        workspace,
        lead,
        summary="Later real call",
        occurred_at=WHEN + timedelta(hours=1),
    )
    context = SourceIndex(session, workspace).context(first.id)
    assess(
        session,
        workspace,
        actor,
        command_id=uuid4(),
        expected_source_digest=context["source_digest"],
        expected_context_digest=context["context_digest"],
        proposal=proposal(context),
    )
    source(
        session,
        workspace,
        lead,
        summary="Explicit non-attempt correction.",
        occurred_at=first.occurred_at,
        supersedes_activity_id=first.id,
        outcome_code=marker,
    )
    session.flush()
    context = SourceIndex(session, workspace).context(later.id)
    assert context["phone_attempt_kind"] == "unknown"
    counts = aggregate(session, workspace, anchor_date=WHEN.date())
    assert counts["phone_attempts"]["total"] == 1
    assert counts["relevance"]["confirmed"] == 0
    assert counts["exclusions"]["by_reason"][marker] == 1


def test_cyclic_exact_source_links_remain_visible_as_conflict_not_zero_calls(kpi_db):
    from src.crm.persistence.models import SourceIdentity

    session, workspace, _actor, lead = kpi_db
    ids = [uuid4(), uuid4()]
    identities = [
        SourceIdentity(
            workspace_id=workspace,
            source_system="manual",
            source_scope="synthetic-cycle",
            entity_kind="meeting",
            external_id=str(i),
            canonical_entity_type="activity",
            canonical_entity_id=ids[1 - i],
        )
        for i in range(2)
    ]
    session.add_all(identities)
    session.flush()
    for i in range(2):
        source(
            session,
            workspace,
            lead,
            id=ids[i],
            source_identity_id=identities[i].id,
            summary="Conflicting source links.",
        )
    counts = aggregate(session, workspace, anchor_date=WHEN.date())
    assert counts["phone_attempts"]["total"] == counts["relevance"]["conflict"] == 2
    assert counts["relevance"]["confirmed"] == counts["exclusions"]["total"] == 0


def test_postgres_refuses_supersession_cycle_but_accepts_real_parent(kpi_db):
    from sqlalchemy.exc import IntegrityError

    from src.crm.persistence.models import Activity

    session, workspace, _actor, lead = kpi_db
    original = source(session, workspace, lead)
    ids = [uuid4(), uuid4()]
    with (
        pytest.raises(IntegrityError, match="activity context mismatch"),
        session.begin_nested(),
    ):
        session.execute(
            Activity.__table__.insert().values(
                [
                    {
                        "id": ids[i],
                        "workspace_id": workspace,
                        "lead_id": lead,
                        "account_id": original.account_id,
                        "contact_id": original.contact_id,
                        "activity_type": "call",
                        "title": "Synthetic cycle",
                        "actor_type": "human",
                        "occurred_at": WHEN,
                        "supersedes_activity_id": ids[1 - i],
                    }
                    for i in range(2)
                ]
            )
        )
    assert (
        session.scalar(
            select(func.count()).select_from(Activity).where(Activity.id.in_(ids))
        )
        == 0
    )
    replacement = source(session, workspace, lead, supersedes_activity_id=original.id)
    assert replacement.supersedes_activity_id == original.id
    assert (
        aggregate(session, workspace, anchor_date=WHEN.date())["phone_attempts"][
            "total"
        ]
        == 1
    )


@pytest.mark.parametrize(
    "anchor,period,start,end",
    [
        ("2026-03-29", "day", "2026-03-29T00:00:00+00:00", "2026-03-29T23:00:00+00:00"),
        ("2026-10-25", "day", "2026-10-24T23:00:00+00:00", "2026-10-26T00:00:00+00:00"),
        (
            "2026-03-29",
            "week",
            "2026-03-23T00:00:00+00:00",
            "2026-03-29T23:00:00+00:00",
        ),
        (
            "2026-10-25",
            "week",
            "2026-10-18T23:00:00+00:00",
            "2026-10-26T00:00:00+00:00",
        ),
        (
            "2026-03-29",
            "month",
            "2026-03-01T00:00:00+00:00",
            "2026-03-31T23:00:00+00:00",
        ),
        (
            "2026-10-25",
            "month",
            "2026-09-30T23:00:00+00:00",
            "2026-11-01T00:00:00+00:00",
        ),
    ],
)
def test_both_lisbon_dst_transitions_and_inclusive_exclusive_boundaries(
    kpi_db, anchor, period, start, end
):
    from datetime import date, datetime

    from src.crm.services.commercial_kpi_service import period_bounds

    session, workspace, _actor, lead = kpi_db
    anchor, start, end = (
        date.fromisoformat(anchor),
        datetime.fromisoformat(start),
        datetime.fromisoformat(end),
    )
    assert period_bounds(anchor, period) == (start, end)
    for stamp in (
        start - timedelta(microseconds=1),
        start,
        end - timedelta(microseconds=1),
        end,
    ):
        source(session, workspace, lead, occurred_at=stamp)
    result = aggregate(session, workspace, anchor_date=anchor, period=period)
    assert result["phone_attempts"]["total"] == result["relevance"]["unknown"] == 2


def test_projection_never_marks_foreign_rule_version_current(kpi_db):
    import copy

    session, workspace, actor, lead = kpi_db
    call = source(
        session,
        workspace,
        lead,
        summary="Responsible person discussed budget and declined.",
    )
    context = SourceIndex(session, workspace).context(call.id)
    with session.begin_nested() as savepoint:
        receipt = assess(
            session,
            workspace,
            actor,
            command_id=uuid4(),
            expected_source_digest=context["source_digest"],
            expected_context_digest=context["context_digest"],
            proposal=proposal(context, relevant="yes"),
        )
        details = copy.deepcopy(session.get(AuditEvent, receipt["audit_id"]).details)
        savepoint.rollback()
    details["assessment"]["rule_version"] = "commercial-call-kpis/v0"
    old = AuditEvent(
        workspace_id=workspace,
        actor_id=actor,
        action="commercial_kpi.assessed",
        entity_type="activity",
        entity_id=call.id,
        command_id=uuid4(),
        details=details,
    )
    session.add(old)
    session.flush()
    counts = aggregate(session, workspace, anchor_date=WHEN.date())
    assert counts["relevance"]["confirmed"] == 0
    assert counts["relevance"]["unknown"] == 1


def test_self_reference_cannot_forge_prior_phone_attempt(kpi_db):
    session, workspace, actor, lead = kpi_db
    call = source(
        session, workspace, lead, summary="Only one recorded logistical call."
    )
    context = SourceIndex(session, workspace).context(call.id)
    claim = proposal(context, relevant="no")
    claim["phone_attempt_kind"] = "follow_up"
    claim["history_coverage"] = {
        "state": "prior_attempt_proved",
        "oldest_known_at": WHEN.isoformat(),
        "proof_activity_id": str(call.id),
    }
    with pytest.raises(KPIConflict):
        assess(
            session,
            workspace,
            actor,
            command_id=uuid4(),
            expected_source_digest=context["source_digest"],
            expected_context_digest=context["context_digest"],
            proposal=claim,
        )
