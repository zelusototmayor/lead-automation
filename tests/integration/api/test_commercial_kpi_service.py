"""KPI behavior against real disposable PostgreSQL, never a production URL."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.crm.persistence.models import Activity, Lead, Workspace
from tests.migration._postgres import cleanup_workspace, require_disposable_postgres

WHEN = datetime(2026, 9, 15, 10, tzinfo=UTC)


@pytest.fixture
def kpi_db():
    engine = create_engine(require_disposable_postgres())
    workspace, actor, lead = uuid4(), uuid4(), uuid4()
    with Session(engine) as session, session.begin():
        session.add(
            Workspace(id=workspace, slug=f"kpi-{workspace}", name="Synthetic KPI")
        )
        session.flush()
        session.add(
            Lead(id=lead, workspace_id=workspace, company_name="Synthetic company")
        )
    try:
        with Session(engine) as session:
            yield session, workspace, actor, lead
            session.rollback()
    finally:
        cleanup_workspace(engine, workspace)
        engine.dispose()


def source(session, workspace, lead, **values):
    row = Activity(
        **dict(
            {
                "id": uuid4(),
                "workspace_id": workspace,
                "lead_id": lead,
                "activity_type": "call",
                "title": "Synthetic interaction",
                "occurred_at": WHEN,
                "actor_type": "human",
                "summary": None,
            },
            **values,
        )
    )
    session.add(row)
    session.flush()
    return row


def test_later_attempt_after_no_answer_is_followup_not_email_or_first_answer_proof(
    kpi_db,
):
    from src.crm.services.commercial_kpi_service import SourceIndex

    session, workspace, _actor, lead = kpi_db
    source(
        session,
        workspace,
        lead,
        activity_type="email_sent",
        occurred_at=WHEN - timedelta(days=3),
    )
    first = source(session, workspace, lead, outcome_code="no_answer")
    later = source(
        session,
        workspace,
        lead,
        outcome_code="connected",
        occurred_at=WHEN + timedelta(hours=1),
    )
    index = SourceIndex(session, workspace)
    first_context, later_context = index.context(first.id), index.context(later.id)
    assert first_context["phone_attempt_kind"] == "unknown"
    assert first_context["history_coverage"]["state"] == "incomplete"
    assert later_context["phone_attempt_kind"] == "follow_up"
    assert later_context["history_coverage"]["proof_activity_id"] == str(first.id)
    assert [item["activity_id"] for item in later_context["history"]] == [
        str(first.id),
        str(later.id),
    ]
    assert later_context["source_version"] == later_context["source_digest"]


def test_import_timestamps_do_not_prove_order_but_real_prior_attempt_does(kpi_db):
    from src.crm.services.commercial_kpi_service import SourceIndex

    session, workspace, _actor, lead = kpi_db
    source(
        session,
        workspace,
        lead,
        actor_type="import",
        occurred_at=WHEN - timedelta(days=30),
    )
    tie = source(session, workspace, lead)
    target = source(session, workspace, lead)
    index = SourceIndex(session, workspace)
    assert index.context(target.id)["phone_attempt_kind"] == "unknown"
    assert index.context(target.id)["history_coverage"]["state"] == "ambiguous_order"
    real = source(
        session,
        workspace,
        lead,
        outcome_code="no_answer",
        occurred_at=WHEN - timedelta(days=1),
    )
    index = SourceIndex(session, workspace)
    assert index.context(target.id)["phone_attempt_kind"] == "follow_up"
    assert index.context(target.id)["history_coverage"]["proof_activity_id"] == str(
        real.id
    )
    assert (
        index.context(target.id)["context_digest"]
        != index.context(tie.id)["context_digest"]
    )


def test_canonical_merge_includes_terminal_and_account_only_not_shared_identifiers(
    kpi_db,
):
    from src.crm.persistence.models import Account
    from src.crm.services.commercial_kpi_service import SourceIndex

    session, workspace, _actor, lead = kpi_db
    accounts = [
        Account(
            workspace_id=workspace,
            display_name=f"Synthetic {i}",
            normalized_name=f"synthetic {i}",
            primary_domain="shared.example.test",
        )
        for i in range(2)
    ]
    session.add_all(accounts)
    session.flush()
    lead_row = session.get(Lead, lead)
    lead_row.account_id = accounts[0].id
    lead_row.stage = "lost"
    session.flush()
    first = source(session, workspace, lead, account_id=accounts[0].id)
    later = source(
        session,
        workspace,
        None,
        account_id=accounts[1].id,
        occurred_at=WHEN + timedelta(hours=1),
    )
    before = SourceIndex(session, workspace).context(later.id)
    assert before["phone_attempt_kind"] == "unknown"
    accounts[0].merged_into_account_id = accounts[1].id
    session.flush()
    after = SourceIndex(session, workspace).context(later.id)
    assert after["phone_attempt_kind"] == "follow_up"
    assert after["history_coverage"]["proof_activity_id"] == str(first.id)
    assert after["context_digest"] != before["context_digest"]


def proposal(context, relevant="yes"):
    text = context["summary"] or ""
    return {
        "rule_version": "commercial-call-kpis/v1",
        "activity_id": context["activity_id"],
        "relevant": relevant,
        "phone_attempt_kind": context["phone_attempt_kind"],
        "eligibility": "eligible",
        "exclusion_reason": None,
        "reason": "Substantive operational discussion"
        if relevant == "yes"
        else "Evidence is incomplete",
        "evidence_refs": [
            {
                "activity_id": context["activity_id"],
                "source_version": context["source_version"],
                "field": "summary",
                "start": 0,
                "end": len(text),
            }
        ]
        if text
        else [],
        "history_coverage": context["history_coverage"],
    }


def test_assess_persists_audit_once_and_backdate_invalidates_before_worker(kpi_db):
    from sqlalchemy import select

    from src.crm.persistence.models import AuditEvent
    from src.crm.services.commercial_kpi_service import SourceIndex, assess, effective

    session, workspace, actor, lead = kpi_db
    call = source(
        session,
        workspace,
        lead,
        summary="The operations manager explained the failed routing process and discussed capacity needs.",
    )
    context = SourceIndex(session, workspace).context(call.id)
    command = uuid4()
    args = {
        "command_id": command,
        "expected_source_digest": context["source_digest"],
        "expected_context_digest": context["context_digest"],
        "proposal": proposal(context),
    }
    receipt = assess(session, workspace, actor, **args)
    session.commit()
    replay = assess(session, workspace, actor, **args)
    assert replay["replayed"] is True
    assert receipt["audit_id"] == replay["audit_id"]
    assert (
        len(
            list(
                session.scalars(
                    select(AuditEvent).where(AuditEvent.workspace_id == workspace)
                )
            )
        )
        == 1
    )
    assert (
        effective(session, workspace, call.id)["assessment"]["freshness"] == "current"
    )
    source(
        session,
        workspace,
        lead,
        occurred_at=WHEN - timedelta(days=1),
        outcome_code="no_answer",
    )
    session.commit()
    changed = effective(session, workspace, call.id)["assessment"]
    assert changed["freshness"] == "stale"
    assert session.get(Activity, call.id).summary == context["summary"]


@pytest.mark.parametrize(
    "invalid",
    [
        "source_cas",
        "context_cas",
        "key_reuse",
        "span",
        "ref_version",
        "foreign_ref",
        "no_answer",
        "spoof",
    ],
)
def test_invalid_assessment_never_appends_audit(kpi_db, invalid):
    from sqlalchemy import select

    from src.crm.persistence.models import AuditEvent
    from src.crm.services.commercial_kpi_service import SourceIndex, assess

    session, workspace, actor, lead = kpi_db
    call = source(
        session,
        workspace,
        lead,
        summary="An operational manager discussed route delays.",
        outcome_code="no_answer" if invalid == "no_answer" else None,
    )
    context = SourceIndex(session, workspace).context(call.id)
    args = {
        "command_id": uuid4(),
        "expected_source_digest": context["source_digest"],
        "expected_context_digest": context["context_digest"],
        "proposal": proposal(context),
    }
    expected = 0
    if invalid == "source_cas":
        args["expected_source_digest"] = "c" * 64
    if invalid == "context_cas":
        args["expected_context_digest"] = "c" * 64
    if invalid == "key_reuse":
        assess(session, workspace, actor, **args)
        args["proposal"]["relevant"] = "no"
        expected = 1
    if invalid == "span":
        args["proposal"]["evidence_refs"][0]["end"] = 10000
    if invalid == "ref_version":
        args["proposal"]["evidence_refs"][0]["source_version"] = "d" * 64
    if invalid == "foreign_ref":
        args["proposal"]["evidence_refs"][0]["activity_id"] = str(uuid4())
    if invalid == "spoof":
        args["proposal"]["provenance"] = "human"
    with pytest.raises(ValueError):
        assess(session, workspace, actor, **args)
    assert (
        len(
            list(
                session.scalars(
                    select(AuditEvent).where(AuditEvent.workspace_id == workspace)
                )
            )
        )
        == expected
    )


def test_contradictory_same_revision_is_conflict_not_latest_row(kpi_db):
    from copy import deepcopy

    from src.crm.persistence.models import AuditEvent
    from src.crm.services.commercial_kpi_service import SourceIndex, assess, effective

    session, workspace, actor, lead = kpi_db
    call = source(
        session,
        workspace,
        lead,
        summary="The operations manager discussed process needs.",
    )
    context = SourceIndex(session, workspace).context(call.id)
    receipt = assess(
        session,
        workspace,
        actor,
        command_id=uuid4(),
        expected_source_digest=context["source_digest"],
        expected_context_digest=context["context_digest"],
        proposal=proposal(context),
    )
    original = session.get(AuditEvent, receipt["audit_id"])
    details = deepcopy(original.details)
    details["assessment"]["relevant"] = "no"
    session.add(
        AuditEvent(
            workspace_id=workspace,
            command_id=uuid4(),
            actor_id=actor,
            entity_type="activity",
            entity_id=call.id,
            action="commercial_kpi.assessed",
            details=details,
        )
    )
    session.flush()
    assert (
        effective(session, workspace, call.id)["assessment"]["freshness"] == "conflict"
    )


def test_human_register_prevails_stays_stale_until_explicit_removal(kpi_db):
    from src.crm.services.commercial_kpi_service import (
        SourceIndex,
        assess,
        correct,
        effective,
    )

    session, workspace, actor, lead = kpi_db
    call = source(
        session,
        workspace,
        lead,
        summary="The manager discussed the process but no change was needed.",
    )
    context = SourceIndex(session, workspace).context(call.id)
    initial = {
        "command_id": uuid4(),
        "expected_source_digest": context["source_digest"],
        "expected_context_digest": context["context_digest"],
        "proposal": proposal(context),
    }
    assess(session, workspace, actor, **initial)
    correction = {
        key: value
        for key, value in proposal(context, "no").items()
        if key not in {"rule_version", "activity_id"}
    }
    receipt = correct(
        session,
        workspace,
        actor,
        call.id,
        command_id=uuid4(),
        expected_source_digest=context["source_digest"],
        expected_context_digest=context["context_digest"],
        expected_override_revision=0,
        operation="set",
        correction=correction,
    )
    assert receipt["override_revision"] == 1
    value = effective(session, workspace, call.id)["assessment"]
    assert (value["relevant"], value["provenance"], value["freshness"]) == (
        "no",
        "human",
        "current",
    )
    source(
        session,
        workspace,
        lead,
        occurred_at=WHEN - timedelta(days=1),
        outcome_code="no_answer",
    )
    context = SourceIndex(session, workspace).context(call.id)
    assess(
        session,
        workspace,
        actor,
        command_id=uuid4(),
        expected_source_digest=context["source_digest"],
        expected_context_digest=context["context_digest"],
        proposal=proposal(context),
    )
    value = effective(session, workspace, call.id)["assessment"]
    assert (value["relevant"], value["provenance"], value["freshness"]) == (
        "no",
        "human",
        "stale",
    )
    correct(
        session,
        workspace,
        actor,
        call.id,
        command_id=uuid4(),
        expected_source_digest=context["source_digest"],
        expected_context_digest=context["context_digest"],
        expected_override_revision=1,
        operation="remove",
        correction=None,
    )
    value = effective(session, workspace, call.id)["assessment"]
    assert (value["relevant"], value["provenance"], value["freshness"]) == (
        "yes",
        "inferred",
        "current",
    )


@pytest.mark.parametrize(
    "invalid",
    [
        "source",
        "context",
        "revision",
        "key",
        "extra",
        "operation",
        "remove_payload",
        "empty_set",
    ],
)
def test_correction_preconditions_and_replay(kpi_db, invalid):
    from src.crm.services.commercial_kpi_service import SourceIndex, correct

    session, workspace, actor, lead = kpi_db
    call = source(session, workspace, lead, summary="Only scheduling with reception.")
    context = SourceIndex(session, workspace).context(call.id)
    correction = {
        key: val
        for key, val in proposal(context, "no").items()
        if key not in {"rule_version", "activity_id"}
    }
    args = {
        "command_id": uuid4(),
        "expected_source_digest": context["source_digest"],
        "expected_context_digest": context["context_digest"],
        "expected_override_revision": 0,
        "operation": "set",
        "correction": correction,
    }
    if invalid == "key":
        receipt = correct(session, workspace, actor, call.id, **args)
        replay = correct(session, workspace, actor, call.id, **args)
        assert replay["replayed"] is True
        assert replay["audit_id"] == receipt["audit_id"]
        args["correction"]["reason"] = "Different reason"
    if invalid == "source":
        args["expected_source_digest"] = "f" * 64
    if invalid == "context":
        args["expected_context_digest"] = "f" * 64
    if invalid == "revision":
        args["expected_override_revision"] = 9
    if invalid == "extra":
        args["correction"]["provenance"] = "inferred"
    if invalid == "operation":
        args["operation"] = "replace-all"
    if invalid == "remove_payload":
        args["operation"] = "remove"
    if invalid == "empty_set":
        args["correction"] = None
    with pytest.raises(ValueError):
        correct(session, workspace, actor, call.id, **args)


def test_explicit_supersession_not_text_dedupe_and_future_task_cancellation(kpi_db):
    from src.crm.persistence.models import Task
    from src.crm.services.commercial_kpi_service import SourceIndex

    session, workspace, actor, lead = kpi_db
    original = source(session, workspace, lead, summary="Only reception scheduling.")
    before = SourceIndex(session, workspace).context(original.id)
    amended = source(
        session,
        workspace,
        lead,
        summary="Only reception scheduling.",
        supersedes_activity_id=original.id,
    )
    repeat = source(
        session,
        workspace,
        lead,
        summary=amended.summary,
        occurred_at=WHEN + timedelta(hours=1),
    )
    session.add(
        Task(
            workspace_id=workspace,
            lead_id=lead,
            task_type="call",
            owner_user_id=actor,
            title="Future callback",
            due_at=WHEN + timedelta(days=1),
            status="cancelled",
        )
    )
    session.flush()
    index = SourceIndex(session, workspace)
    assert index.context(original.id)["exclusion_reason"] == "superseded"
    assert index.context(original.id)["context_digest"] != before["context_digest"]
    assert index.context(amended.id)["eligibility"] == "eligible"
    assert index.context(repeat.id)["eligibility"] == "eligible"
    assert index.context(repeat.id)["history_coverage"]["proof_activity_id"] == str(
        amended.id
    )


def test_new_requires_affirmative_proof_or_human_declaration_not_first_answer(kpi_db):
    from src.crm.services.commercial_kpi_service import (
        SourceIndex,
        assess,
        correct,
        effective,
    )

    session, workspace, actor, lead = kpi_db
    first = source(
        session,
        workspace,
        lead,
        outcome_code="no_answer",
        summary="First recorded attempt; no answer.",
    )
    context = SourceIndex(session, workspace).context(first.id)
    claim = proposal(context, "no")
    claim["phone_attempt_kind"] = "new"
    claim["history_coverage"] = {
        "state": "complete_no_prior_attempt",
        "oldest_known_at": None,
        "proof_activity_id": None,
    }
    with pytest.raises(ValueError):
        assess(
            session,
            workspace,
            actor,
            command_id=uuid4(),
            expected_source_digest=context["source_digest"],
            expected_context_digest=context["context_digest"],
            proposal=claim,
        )
    correction = {
        key: val
        for key, val in claim.items()
        if key not in {"rule_version", "activity_id"}
    }
    correction["reason"] = (
        "Human declares no earlier phone attempts; this attempt was unanswered."
    )
    correct(
        session,
        workspace,
        actor,
        first.id,
        command_id=uuid4(),
        expected_source_digest=context["source_digest"],
        expected_context_digest=context["context_digest"],
        expected_override_revision=0,
        operation="set",
        correction=correction,
    )
    value = effective(session, workspace, first.id)["assessment"]
    assert value["phone_attempt_kind"] == "new" and value["provenance"] == "human"
    source(session, workspace, lead, occurred_at=WHEN - timedelta(days=1))
    context = SourceIndex(session, workspace).context(first.id)
    with pytest.raises(ValueError):
        correct(
            session,
            workspace,
            actor,
            first.id,
            command_id=uuid4(),
            expected_source_digest=context["source_digest"],
            expected_context_digest=context["context_digest"],
            expected_override_revision=1,
            operation="set",
            correction=correction,
        )
    assert effective(session, workspace, first.id)["assessment"]["freshness"] == "stale"


def test_exact_event_identity_dedupes_but_company_identity_does_not(kpi_db):
    from src.crm.persistence.models import SourceIdentity
    from src.crm.services.commercial_kpi_service import SourceIndex

    session, workspace, _actor, lead = kpi_db
    original = source(session, workspace, lead)
    identity = SourceIdentity(
        workspace_id=workspace,
        source_system="manual",
        source_scope="synthetic",
        entity_kind="meeting",
        external_id="one-phone-event",
        canonical_entity_type="activity",
        canonical_entity_id=original.id,
    )
    session.add(identity)
    session.flush()
    duplicate = source(session, workspace, lead, source_identity_id=identity.id)
    index = SourceIndex(session, workspace)
    assert index.context(duplicate.id)["exclusion_reason"] == "duplicate"
    assert index.context(original.id)["eligibility"] == "eligible"
    identity.canonical_entity_type = "lead"
    identity.canonical_entity_id = lead
    session.flush()
    assert (
        SourceIndex(session, workspace).context(duplicate.id)["eligibility"]
        == "eligible"
    )
    assert (
        SourceIndex(session, workspace).context(duplicate.id)["context_digest"]
        != index.context(duplicate.id)["context_digest"]
    )


def test_calendar_aggregate_partitions_and_weekly_53_not_month_or_day_numerator(kpi_db):
    from src.crm.services.commercial_kpi_service import SourceIndex, aggregate, assess

    session, workspace, actor, lead = kpi_db
    dates = [datetime(2026, 9, 30, 10, tzinfo=UTC)] + [
        datetime(2026, 10, 1, 10, tzinfo=UTC)
    ] * 52
    calls = [
        source(
            session,
            workspace,
            lead,
            occurred_at=stamp,
            summary="Operations manager explained the process and declined due to an existing contract.",
        )
        for stamp in dates
    ]
    superseded = source(session, workspace, lead, occurred_at=dates[-1])
    source(
        session,
        workspace,
        lead,
        activity_type="note",
        supersedes_activity_id=superseded.id,
        occurred_at=dates[-1],
        summary="Voided duplicate entry.",
    )
    for call in calls:
        ctx = SourceIndex(session, workspace).context(call.id)
        assess(
            session,
            workspace,
            actor,
            command_id=uuid4(),
            expected_source_digest=ctx["source_digest"],
            expected_context_digest=ctx["context_digest"],
            proposal=proposal(ctx),
        )
    day = aggregate(
        session,
        workspace,
        anchor_date=dates[-1].date(),
        period="day",
        legacy_anchor_day_attempts=53,
    )
    month = aggregate(
        session,
        workspace,
        anchor_date=dates[-1].date(),
        period="month",
        legacy_anchor_day_attempts=53,
    )
    week = aggregate(
        session,
        workspace,
        anchor_date=dates[-1].date(),
        period="week",
        legacy_anchor_day_attempts=53,
    )
    assert day["relevance"]["confirmed"] == month["relevance"]["confirmed"] == 52
    assert week["relevance"]["confirmed"] == 53
    for result in (day, month, week):
        assert (
            result["weekly_goal"]["confirmed"] == 53
            and result["weekly_goal"]["target"] == 50
        )
        assert result["phone_attempts"]["total"] == sum(
            result["phone_attempts"][key] for key in ("new", "follow_up", "unknown")
        )
        assert result["phone_attempts"]["total"] == sum(
            result["relevance"][key] for key in ("confirmed", "no", "unknown")
        )
        assert result["exclusions"]["by_reason"]["superseded"] == 1
        assert result["drift"]["anchor_day_delta"] == -1
        assert result["coverage"]["source"]["complete"] is False
        assert result["assessed_at"] is not None
    assert week["weekly_goal"]["week_start"].startswith("2026-09-27T23:00:00")
    assert (
        aggregate(
            session,
            workspace,
            anchor_date=dates[-1].date(),
            period="week",
            legacy_anchor_day_attempts=53,
        )["relevance"]
        == week["relevance"]
    )


def test_superseding_source_keeps_human_stale_until_explicit_child_removal(kpi_db):
    from src.crm.services.commercial_kpi_service import (
        SourceIndex,
        assess,
        correct,
        effective,
    )

    session, workspace, actor, lead = kpi_db
    original = source(session, workspace, lead, summary="Only reception scheduling.")
    context = SourceIndex(session, workspace).context(original.id)
    correction = proposal(context, "no")
    correction.pop("activity_id")
    correction.pop("rule_version")
    correct(
        session,
        workspace,
        actor,
        original.id,
        command_id=uuid4(),
        expected_source_digest=context["source_digest"],
        expected_context_digest=context["context_digest"],
        expected_override_revision=0,
        operation="set",
        correction=correction,
    )
    amended = source(
        session,
        workspace,
        lead,
        summary="Responsible buyer explained a substantive budget objection.",
        supersedes_activity_id=original.id,
    )
    current = SourceIndex(session, workspace).context(amended.id)
    assess(
        session,
        workspace,
        actor,
        command_id=uuid4(),
        expected_source_digest=current["source_digest"],
        expected_context_digest=current["context_digest"],
        proposal=proposal(current),
    )
    pending = effective(session, workspace, amended.id)
    assert (
        pending["assessment"]["relevant"],
        pending["assessment"]["provenance"],
        pending["assessment"]["freshness"],
    ) == ("no", "human", "stale")
    correct(
        session,
        workspace,
        actor,
        amended.id,
        command_id=uuid4(),
        expected_source_digest=current["source_digest"],
        expected_context_digest=current["context_digest"],
        expected_override_revision=pending["override_revision"],
        operation="remove",
        correction=None,
    )
    assert effective(session, workspace, amended.id)["assessment"]["relevant"] == "yes"
