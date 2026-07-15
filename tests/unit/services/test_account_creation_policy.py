from __future__ import annotations

from contextlib import AbstractContextManager
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.crm.domain.stage_policy import InvalidTransitionError

from src.crm.services.account_service import (
    AccountService,
    IdentityHints,
    IdentityReviewRequired,
    ReplayConflictError,
    StageTransitionCommand,
    normalize_company_name,
    normalize_domain,
    normalize_email,
)
from src.crm.services.activity_service import ActivityService, AppendActivityCommand


NOW = datetime(2026, 7, 15, 12, tzinfo=UTC)


class MemoryRepo:
    def __init__(self, rows=None):
        self.rows = list(rows or [])

    def get(self, workspace_id, row_id, *, for_update=False):
        del for_update
        return next(
            (r for r in self.rows if r.workspace_id == workspace_id and r.id == row_id),
            None,
        )

    def add(self, row):
        self.rows.append(row)
        return row


class MemoryUow(AbstractContextManager):
    def __init__(self):
        self.accounts = MemoryRepo()
        self.contacts = MemoryRepo()
        self.leads = MemoryRepo()
        self.activities = MemoryRepo()
        self.source_identities = MemoryRepo()
        self.stage_reduction_claims = {}
        self.activity_replay_locks = []
        self.commits = 0
        self.rollbacks = 0
        self.fail_activity = False

    def __enter__(self):
        self._snapshot = {
            name: deepcopy(getattr(self, name).rows)
            for name in (
                "accounts",
                "contacts",
                "leads",
                "activities",
                "source_identities",
            )
        }
        self._claims_snapshot = deepcopy(self.stage_reduction_claims)
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type:
            self.rollback()
        return False

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1
        for name, rows in self._snapshot.items():
            getattr(self, name).rows[:] = rows
        self.stage_reduction_claims = self._claims_snapshot

    def lock_identities(self, workspace_id, fingerprints):
        return None

    def claim_stage_reduction(self, workspace_id, ingest_event_id, fingerprint):
        key = workspace_id, ingest_event_id
        existing = self.stage_reduction_claims.get(key)
        if existing is not None and existing != fingerprint:
            raise ReplayConflictError(
                "ingest event already records different semantics"
            )
        self.stage_reduction_claims[key] = fingerprint
        return True

    def lock_activity_replay(self, workspace_id, ingest_event_id, activity_type):
        self.activity_replay_locks.append(
            (workspace_id, ingest_event_id, activity_type)
        )
        return True

    def replay(self, workspace_id, ingest_event_id):
        activity = next(
            (
                a
                for a in self.activities.rows
                if a.workspace_id == workspace_id
                and a.ingest_event_id == ingest_event_id
                and a.activity_type == "stage_change"
            ),
            None,
        )
        if activity is None:
            return None
        lead = self.leads.get(workspace_id, activity.lead_id)
        return activity, lead

    def account_candidates(self, workspace_id, hints):
        candidates = []
        if hints.account_id:
            row = self.accounts.get(workspace_id, hints.account_id)
            if row:
                candidates.append(row)
            else:
                raise IdentityReviewRequired("identity requires review")
        if hints.source_identity_id:
            identity = self.source_identities.get(
                workspace_id, hints.source_identity_id
            )
            if identity is None or identity.entity_kind != "account":
                raise IdentityReviewRequired("identity requires review")
            if identity.canonical_entity_id:
                row = self.accounts.get(workspace_id, identity.canonical_entity_id)
                if row:
                    candidates.append(row)
                else:
                    raise IdentityReviewRequired("identity requires review")
        email = normalize_email(hints.contact_email) if hints.contact_email else None
        if email:
            candidates += [
                self.accounts.get(workspace_id, c.account_id)
                for c in self.contacts.rows
                if c.workspace_id == workspace_id and c.primary_email == email
            ]
        domain = normalize_domain(hints.domain) if hints.domain else None
        name = (
            normalize_company_name(
                hints.company_name or hints.display_name or hints.legal_name
            )
            if (hints.company_name or hints.display_name or hints.legal_name)
            else None
        )
        if domain and name:
            candidates += [
                a
                for a in self.accounts.rows
                if a.workspace_id == workspace_id
                and a.primary_domain == domain
                and a.normalized_name == name
            ]
        return [c for c in candidates if c is not None]

    def new_account(self, workspace_id, hints):
        name = hints.company_name or hints.display_name or hints.legal_name
        row = SimpleNamespace(
            id=uuid4(),
            workspace_id=workspace_id,
            display_name=name,
            legal_name=hints.legal_name,
            normalized_name=normalize_company_name(name),
            primary_domain=normalize_domain(hints.domain) if hints.domain else None,
            lifecycle_stage="potential",
            highest_stage_rank=0,
            source_identity_id=hints.source_identity_id,
            sector=hints.sector,
            commercial_vertical=hints.vertical,
            source_origin=hints.source_origin,
            version=1,
        )
        return self.accounts.add(row)

    def new_contact(self, workspace_id, account_id, hints):
        email = normalize_email(hints.contact_email)
        existing = next(
            (
                c
                for c in self.contacts.rows
                if c.workspace_id == workspace_id and c.primary_email == email
            ),
            None,
        )
        if existing:
            if existing.account_id != account_id:
                raise IdentityReviewRequired("identity requires review")
            return existing
        return self.contacts.add(
            SimpleNamespace(
                id=uuid4(),
                workspace_id=workspace_id,
                account_id=account_id,
                primary_email=email,
                full_name=hints.contact_name,
                version=1,
            )
        )

    def new_lead(self, workspace_id, hints):
        return self.leads.add(
            SimpleNamespace(
                id=uuid4(),
                workspace_id=workspace_id,
                account_id=None,
                contact_id=None,
                stage="new",
                highest_stage_rank=0,
                version=1,
                sector=hints.sector,
                commercial_vertical=hints.vertical,
                source_origin=hints.source_origin,
                source_identity_id=hints.source_identity_id,
            )
        )

    def new_activity(self, **values):
        if self.fail_activity:
            raise RuntimeError("activity failed")
        return self.activities.add(SimpleNamespace(id=uuid4(), **values))

    def link_source_identity(self, workspace_id, source_identity_id, account_id):
        if not source_identity_id:
            return
        identity = self.source_identities.get(workspace_id, source_identity_id)
        if identity is None or identity.entity_kind != "account":
            raise IdentityReviewRequired("identity requires review")
        if identity.canonical_entity_id not in (None, account_id):
            raise IdentityReviewRequired("identity requires review")
        identity.canonical_entity_type = "account"
        identity.canonical_entity_id = account_id


def command(
    workspace_id,
    *,
    stage="meeting_booked",
    lead_id=None,
    hints=None,
    event=None,
    classification="confirmed",
    known=True,
    persisted=None,
    corrected=False,
):
    return StageTransitionCommand(
        workspace_id=workspace_id,
        lead_id=lead_id,
        target_stage=stage,
        identity=hints or IdentityHints(company_name="Acme", domain="acme.invalid"),
        ingest_event_id=event,
        occurred_at=NOW,
        commercial_classification=classification,
        previous_history_known=known,
        persisted_terminal_requires_account=persisted,
        reviewed_correction=corrected,
    )


def service(uow):
    return AccountService(lambda: uow)


def test_normalizers_are_exact_unicode_and_validate_generically():
    assert normalize_company_name("  AＣＭＥ\t GmbH ") == "acme gmbh"
    assert normalize_email(" User@Example.Invalid ") == "user@example.invalid"
    assert normalize_domain(" MÜNCHEN.Example. ") == "xn--mnchen-3ya.example"
    for value, normalizer in (
        ("bad\x00name", normalize_company_name),
        ("bad\n@example.invalid", normalize_email),
        ("bad..example", normalize_domain),
    ):
        with pytest.raises(ValueError) as exc:
            normalizer(value)
        assert value not in str(exc.value)


@pytest.mark.parametrize(
    ("stage", "lifecycle"),
    (("meeting_booked", "meeting"), ("proposal_sent", "proposal"), ("won", "customer")),
)
def test_required_milestones_create_lead_account_contact_and_activity(stage, lifecycle):
    uow = MemoryUow()
    workspace = uuid4()
    result = service(uow).apply_stage_transition(
        command(
            workspace,
            stage=stage,
            hints=IdentityHints(
                company_name="Acme", contact_email="Person@Example.Invalid"
            ),
        )
    )
    assert result.status == "applied" and result.account_id is not None
    assert (
        len(uow.accounts.rows)
        == len(uow.leads.rows)
        == len(uow.contacts.rows)
        == len(uow.activities.rows)
        == 1
    )
    assert uow.accounts.rows[0].lifecycle_stage == lifecycle
    assert uow.commits == 1


def test_contacted_updates_existing_lead_but_never_account_from_hints():
    uow = MemoryUow()
    workspace = uuid4()
    lead = uow.new_lead(workspace, IdentityHints())
    result = service(uow).apply_stage_transition(
        command(workspace, stage="contacted", lead_id=lead.id)
    )
    assert (
        result.account_id is None and len(uow.leads.rows) == 1 and not uow.accounts.rows
    )


def test_accountless_contacted_appends_timeline_and_ingest_replays_without_duplicates():
    uow = MemoryUow()
    workspace = uuid4()
    with pytest.raises(IdentityReviewRequired):
        service(uow).apply_stage_transition(command(workspace, stage="contacted"))
    lead = uow.new_lead(workspace, IdentityHints())
    event = uuid4()
    first = service(uow).apply_stage_transition(
        command(workspace, stage="contacted", lead_id=lead.id, event=event)
    )
    replay = service(uow).apply_stage_transition(
        command(workspace, stage="contacted", lead_id=lead.id, event=event)
    )
    assert first == replay
    assert first.account_id is None and lead.account_id is None
    assert len(uow.leads.rows) == 1 and len(uow.activities.rows) == 1
    activity = uow.activities.rows[0]
    assert activity.account_id is None and activity.lead_id == lead.id
    assert activity.activity_type == "stage_change"
    assert len(activity.semantic_fingerprint) == 64

    human = uow.new_lead(workspace, IdentityHints())
    service(uow).apply_stage_transition(
        command(workspace, stage="contacted", lead_id=human.id)
    )
    assert len(uow.activities.rows) == 2


def test_terminal_policy_uses_original_rank_and_never_removes_history():
    uow = MemoryUow()
    workspace = uuid4()
    account = uow.new_account(
        workspace, IdentityHints(company_name="Acme", domain="acme.invalid")
    )
    lead = uow.new_lead(workspace, IdentityHints())
    lead.account_id = account.id
    lead.stage = "meeting_booked"
    lead.highest_stage_rank = 40
    result = service(uow).apply_stage_transition(
        command(workspace, stage="lost", lead_id=lead.id, hints=IdentityHints())
    )
    assert (
        result.account_id == account.id
        and lead.highest_stage_rank == account.highest_stage_rank == 90
    )
    uow2 = MemoryUow()
    early = uow2.new_lead(workspace, IdentityHints())
    early.highest_stage_rank = 20
    early.stage = "contacted"
    assert (
        service(uow2)
        .apply_stage_transition(
            command(workspace, stage="lost", lead_id=early.id, hints=IdentityHints())
        )
        .account_id
        is None
    )


def test_unknown_terminal_history_requires_review_without_writes():
    uow = MemoryUow()
    with pytest.raises(IdentityReviewRequired):
        service(uow).apply_stage_transition(
            command(uuid4(), stage="lost", known=False, hints=IdentityHints())
        )
    assert not uow.leads.rows and not uow.activities.rows and uow.rollbacks == 0


def test_exact_signals_associate_but_name_only_and_conflicts_review():
    workspace = uuid4()
    for signal in ("email", "source", "domain"):
        uow = MemoryUow()
        account = uow.new_account(
            workspace, IdentityHints(company_name="Acme", domain="acme.invalid")
        )
        hints = IdentityHints(company_name="Acme")
        if signal == "email":
            uow.new_contact(
                workspace,
                account.id,
                IdentityHints(contact_email="person@example.invalid"),
            )
            hints = IdentityHints(
                company_name="Other", contact_email="PERSON@example.invalid"
            )
        elif signal == "source":
            identity = uow.source_identities.add(
                SimpleNamespace(
                    id=uuid4(),
                    workspace_id=workspace,
                    entity_kind="account",
                    canonical_entity_id=account.id,
                    canonical_entity_type="account",
                )
            )
            hints = IdentityHints(source_identity_id=identity.id)
        else:
            hints = IdentityHints(company_name=" ACME ", domain="ACME.INVALID.")
        assert (
            service(uow)
            .apply_stage_transition(command(workspace, hints=hints))
            .account_id
            == account.id
        )
        assert len(uow.accounts.rows) == 1
    with pytest.raises(IdentityReviewRequired):
        service(MemoryUow()).apply_stage_transition(
            command(workspace, hints=IdentityHints(company_name="Name only"))
        )
    conflict = MemoryUow()
    a = conflict.new_account(
        workspace, IdentityHints(company_name="A", domain="a.invalid")
    )
    b = conflict.new_account(
        workspace, IdentityHints(company_name="B", domain="b.invalid")
    )
    conflict.new_contact(
        workspace, a.id, IdentityHints(contact_email="x@example.invalid")
    )
    with pytest.raises(IdentityReviewRequired):
        service(conflict).apply_stage_transition(
            command(
                workspace,
                hints=IdentityHints(account_id=b.id, contact_email="x@example.invalid"),
            )
        )


@pytest.mark.parametrize(
    ("classification", "raises"), (("excluded", False), ("review", True))
)
def test_source_first_nonconfirmed_creates_no_rows(classification, raises):
    uow = MemoryUow()
    if raises:
        with pytest.raises(IdentityReviewRequired):
            service(uow).apply_stage_transition(
                command(uuid4(), classification=classification)
            )
    else:
        assert (
            service(uow)
            .apply_stage_transition(command(uuid4(), classification=classification))
            .status
            == "excluded"
        )
    assert not uow.leads.rows and not uow.accounts.rows and not uow.activities.rows


def test_replay_same_returns_original_and_different_target_or_entity_conflicts():
    uow = MemoryUow()
    workspace = uuid4()
    event = uuid4()
    first = service(uow).apply_stage_transition(command(workspace, event=event))
    replay = service(uow).apply_stage_transition(command(workspace, event=event))
    assert (
        replay == first and len(uow.activities.rows) == 1 and len(uow.leads.rows) == 1
    )
    with pytest.raises(ReplayConflictError):
        service(uow).apply_stage_transition(
            command(workspace, stage="won", event=event)
        )
    with pytest.raises(ReplayConflictError):
        service(uow).apply_stage_transition(
            command(workspace, lead_id=uuid4(), event=event)
        )


@pytest.mark.parametrize(
    "changed",
    (
        lambda: IdentityHints(
            account_id=uuid4(), company_name="Acme", domain="acme.invalid"
        ),
        lambda: IdentityHints(
            source_identity_id=uuid4(), company_name="Acme", domain="acme.invalid"
        ),
        lambda: IdentityHints(
            contact_email="other@example.invalid",
            company_name="Acme",
            domain="acme.invalid",
        ),
        lambda: IdentityHints(company_name="Beta", domain="beta.invalid"),
    ),
)
def test_source_first_replay_changed_exact_entity_semantics_conflicts(changed):
    uow = MemoryUow()
    workspace = uuid4()
    event = uuid4()
    original = IdentityHints(
        contact_email=" Person@Example.Invalid ",
        company_name=" AＣＭＥ ",
        domain="ACME.INVALID.",
    )
    first = service(uow).apply_stage_transition(
        command(workspace, event=event, hints=original)
    )
    equivalent = IdentityHints(
        contact_email="person@example.invalid",
        company_name="acme",
        domain="acme.invalid",
    )
    assert (
        service(uow).apply_stage_transition(
            command(workspace, event=event, hints=equivalent)
        )
        == first
    )
    with pytest.raises(ReplayConflictError, match="different semantics"):
        service(uow).apply_stage_transition(
            command(workspace, event=event, hints=changed())
        )


def test_existing_lead_replay_changed_exact_account_signal_conflicts():
    uow = MemoryUow()
    workspace = uuid4()
    first_account = uow.new_account(
        workspace, IdentityHints(company_name="Alpha", domain="alpha.invalid")
    )
    second_account = uow.new_account(
        workspace, IdentityHints(company_name="Beta", domain="beta.invalid")
    )
    lead = uow.new_lead(workspace, IdentityHints())
    lead.account_id = first_account.id
    event = uuid4()
    service(uow).apply_stage_transition(
        command(
            workspace,
            lead_id=lead.id,
            event=event,
            hints=IdentityHints(account_id=first_account.id),
        )
    )
    with pytest.raises(ReplayConflictError, match="different semantics"):
        service(uow).apply_stage_transition(
            command(
                workspace,
                lead_id=lead.id,
                event=event,
                hints=IdentityHints(account_id=second_account.id),
            )
        )


def test_activity_failure_rolls_back_without_commit_or_durable_rows():
    uow = MemoryUow()
    uow.fail_activity = True
    with pytest.raises(RuntimeError):
        service(uow).apply_stage_transition(command(uuid4()))
    assert uow.commits == 0 and uow.rollbacks == 1
    assert (
        not uow.accounts.rows
        and not uow.contacts.rows
        and not uow.leads.rows
        and not uow.activities.rows
    )


def test_resolver_failure_after_new_lead_rolls_back_memory_state():
    uow = MemoryUow()
    with pytest.raises(IdentityReviewRequired):
        service(uow).apply_stage_transition(
            command(uuid4(), hints=IdentityHints(company_name="Name only"))
        )
    assert not uow.leads.rows and not uow.accounts.rows and uow.commits == 0


def test_memory_rollback_restores_preexisting_in_place_aggregate_mutations():
    uow = MemoryUow()
    workspace = uuid4()
    account = uow.new_account(
        workspace, IdentityHints(company_name="Acme", domain="acme.invalid")
    )
    lead = uow.new_lead(workspace, IdentityHints())
    lead.account_id = account.id
    lead.stage = "meeting_booked"
    lead.highest_stage_rank = 40
    source = uow.source_identities.add(
        SimpleNamespace(
            id=uuid4(),
            workspace_id=workspace,
            entity_kind="account",
            canonical_entity_type=None,
            canonical_entity_id=None,
        )
    )
    uow.fail_activity = True
    with pytest.raises(RuntimeError, match="activity failed"):
        service(uow).apply_stage_transition(
            command(
                workspace,
                stage="proposal_sent",
                lead_id=lead.id,
                hints=IdentityHints(
                    account_id=account.id,
                    source_identity_id=source.id,
                    contact_email="person@example.invalid",
                ),
            )
        )
    restored_account = uow.accounts.get(workspace, account.id)
    restored_lead = uow.leads.get(workspace, lead.id)
    restored_source = uow.source_identities.get(workspace, source.id)
    assert (restored_account.lifecycle_stage, restored_account.highest_stage_rank) == (
        "potential",
        0,
    )
    assert (
        restored_lead.stage,
        restored_lead.highest_stage_rank,
        restored_lead.contact_id,
    ) == ("meeting_booked", 40, None)
    assert (
        restored_source.canonical_entity_type,
        restored_source.canonical_entity_id,
    ) == (None, None)
    assert not uow.contacts.rows and not uow.activities.rows


def test_all_identity_signals_are_locked_and_existing_account_links_contact_and_source():
    uow = MemoryUow()
    workspace = uuid4()
    account = uow.new_account(
        workspace, IdentityHints(company_name="Acme", domain="acme.invalid")
    )
    lead = uow.new_lead(workspace, IdentityHints())
    lead.account_id = account.id
    identity = uow.source_identities.add(
        SimpleNamespace(
            id=uuid4(),
            workspace_id=workspace,
            entity_kind="account",
            canonical_entity_id=None,
            canonical_entity_type=None,
        )
    )
    captured = []
    uow.lock_identities = lambda workspace_id, fingerprints: captured.extend(
        fingerprints
    )
    hints = IdentityHints(
        account_id=account.id,
        source_identity_id=identity.id,
        contact_email="person@example.invalid",
        company_name="Acme",
        domain="acme.invalid",
    )
    result = service(uow).apply_stage_transition(
        command(workspace, lead_id=lead.id, hints=hints)
    )
    assert result.account_id == account.id and lead.contact_id is not None
    assert identity.canonical_entity_id == account.id
    assert captured == sorted(captured)
    assert {item.split(":", 1)[0] for item in captured} == {
        "account",
        "source",
        "email",
        "domain-name",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("workspace_id", "not-uuid"),
        ("lead_id", "not-uuid"),
        ("ingest_event_id", "not-uuid"),
    ),
)
def test_transition_rejects_non_exact_uuid_types_without_opening_uow(field, value):
    values = {
        "workspace_id": uuid4(),
        "target_stage": "meeting_booked",
        "identity": IdentityHints(company_name="Acme", domain="acme.invalid"),
        "occurred_at": NOW,
        field: value,
    }
    uow = MemoryUow()
    with pytest.raises(IdentityReviewRequired) as exc:
        service(uow).apply_stage_transition(StageTransitionCommand(**values))
    assert exc.value.__context__ is None and uow.commits == uow.rollbacks == 0


def test_activity_service_is_idempotent_and_maps_supersedes():
    uow = MemoryUow()
    workspace = uuid4()
    account = uow.new_account(
        workspace, IdentityHints(company_name="Acme", domain="acme.invalid")
    )
    event = uuid4()
    supersedes = uow.new_activity(
        workspace_id=workspace,
        account_id=account.id,
        activity_type="note",
        occurred_at=NOW,
        title="old",
        ingest_event_id=None,
    )
    activity_command = AppendActivityCommand(
        workspace_id=workspace,
        account_id=account.id,
        activity_type="note",
        occurred_at=NOW,
        title="correction",
        ingest_event_id=event,
        supersedes_activity_id=supersedes.id,
    )
    first = ActivityService(lambda: uow).append(activity_command)
    second = ActivityService(lambda: uow).append(activity_command)
    assert first == second and len(uow.activities.rows) == 2
    assert uow.activities.rows[-1].supersedes_activity_id == supersedes.id


def test_activity_service_accepts_accountless_lead_and_exact_nullable_correction_context():
    uow = MemoryUow()
    workspace = uuid4()
    lead = uow.new_lead(workspace, IdentityHints())
    original = ActivityService(lambda: uow).append(
        AppendActivityCommand(
            workspace_id=workspace,
            account_id=None,
            lead_id=lead.id,
            activity_type="note",
            occurred_at=NOW,
            title="Original",
        )
    )
    correction = ActivityService(lambda: uow).append(
        AppendActivityCommand(
            workspace_id=workspace,
            account_id=None,
            lead_id=lead.id,
            activity_type="note",
            occurred_at=NOW,
            title="Correction",
            supersedes_activity_id=original.activity_id,
        )
    )
    assert correction.activity_id != original.activity_id

    account = uow.new_account(
        workspace, IdentityHints(company_name="Acme", domain="acme.invalid")
    )
    with pytest.raises(ValueError, match="activity requires review"):
        ActivityService(lambda: uow).append(
            AppendActivityCommand(
                workspace_id=workspace,
                account_id=account.id,
                activity_type="note",
                occurred_at=NOW,
                title="Wrong context",
                supersedes_activity_id=original.activity_id,
            )
        )


def test_activity_summary_allows_business_whitespace_but_rejects_hidden_controls():
    uow = MemoryUow()
    workspace = uuid4()
    account = uow.new_account(
        workspace, IdentityHints(company_name="Acme", domain="acme.invalid")
    )
    summary = "Agenda:\r\n\t- pricing\n\t- timeline"
    ActivityService(lambda: uow).append(
        AppendActivityCommand(
            workspace_id=workspace,
            account_id=account.id,
            activity_type="note",
            occurred_at=NOW,
            title="Meeting notes",
            summary=summary,
        )
    )
    assert uow.activities.rows[-1].summary == summary
    for invalid in ("hidden\x00value", "hidden\u200bvalue", "hidden\x1fvalue"):
        with pytest.raises(ValueError, match="activity requires review"):
            ActivityService(lambda: uow).append(
                AppendActivityCommand(
                    workspace_id=workspace,
                    account_id=account.id,
                    activity_type="note",
                    occurred_at=NOW,
                    title="Meeting notes",
                    summary=invalid,
                )
            )


def test_reviewed_terminal_correction_still_applies_account_policy():
    uow = MemoryUow()
    workspace = uuid4()
    lead = uow.new_lead(workspace, IdentityHints())
    lead.stage = "lost"
    lead.highest_stage_rank = 90
    result = service(uow).apply_stage_transition(
        command(workspace, stage="meeting_booked", lead_id=lead.id, corrected=True)
    )
    assert result.account_id is not None


def test_service_preserves_typed_transition_error_with_normalized_safe_target():
    uow = MemoryUow()
    workspace = uuid4()
    lead = uow.new_lead(workspace, IdentityHints())
    lead.stage = "lost"
    lead.highest_stage_rank = 90
    raw_target = "  Meeting Booked  "
    with pytest.raises(InvalidTransitionError) as exc:
        service(uow).apply_stage_transition(
            command(workspace, stage=raw_target, lead_id=lead.id, hints=IdentityHints())
        )
    assert raw_target not in str(exc.value)
    assert "meeting_booked" in str(exc.value)


def test_excluded_transition_claims_event_and_conflicts_with_confirmation():
    uow = MemoryUow()
    workspace, event = uuid4(), uuid4()
    excluded = command(workspace, event=event, classification="excluded")
    assert service(uow).apply_stage_transition(excluded).status == "excluded"
    assert service(uow).apply_stage_transition(excluded).status == "excluded"
    assert uow.commits == 2 and len(uow.stage_reduction_claims) == 1
    assert not uow.leads.rows and not uow.accounts.rows and not uow.activities.rows
    with pytest.raises(ReplayConflictError, match="different semantics"):
        service(uow).apply_stage_transition(
            replace(excluded, commercial_classification="confirmed")
        )


def test_confirmed_transition_claim_conflicts_with_later_exclusion():
    uow = MemoryUow()
    confirmed = command(uuid4(), event=uuid4())
    service(uow).apply_stage_transition(confirmed)
    with pytest.raises(ReplayConflictError, match="different semantics"):
        service(uow).apply_stage_transition(
            replace(confirmed, commercial_classification="excluded")
        )


@pytest.mark.parametrize(
    "mutation",
    (
        lambda c: replace(c, occurred_at=c.occurred_at + timedelta(microseconds=1)),
        lambda c: replace(c, reviewed_correction=True),
        lambda c: replace(c, previous_history_known=False),
        lambda c: replace(c, persisted_terminal_requires_account=True),
        lambda c: replace(c, identity=replace(c.identity, account_id=uuid4())),
        lambda c: replace(c, identity=replace(c.identity, source_identity_id=uuid4())),
        lambda c: replace(
            c, identity=replace(c.identity, contact_email="other@example.invalid")
        ),
        lambda c: replace(c, identity=replace(c.identity, domain="other.invalid")),
        lambda c: replace(c, identity=replace(c.identity, company_name="Other")),
        lambda c: replace(
            c, identity=replace(c.identity, display_name="Other display")
        ),
        lambda c: replace(c, identity=replace(c.identity, legal_name="Other legal")),
        lambda c: replace(c, identity=replace(c.identity, contact_name="Other person")),
        lambda c: replace(c, identity=replace(c.identity, sector="Other sector")),
        lambda c: replace(c, identity=replace(c.identity, vertical="Other vertical")),
        lambda c: replace(
            c, identity=replace(c.identity, source_origin="Other origin")
        ),
    ),
)
def test_claim_conflicts_when_any_normalized_semantic_field_changes(mutation):
    uow = MemoryUow()
    base = command(
        uuid4(),
        event=uuid4(),
        hints=IdentityHints(
            contact_email="person@example.invalid",
            contact_name="Person",
            company_name="Acme",
            display_name="Acme Display",
            legal_name="Acme Legal",
            domain="acme.invalid",
            sector="Services",
            vertical="B2B",
            source_origin="manual",
        ),
    )
    service(uow).apply_stage_transition(base)
    with pytest.raises(ReplayConflictError, match="different semantics"):
        service(uow).apply_stage_transition(mutation(base))


def test_claim_normalizes_target_identity_and_utc_instant_for_replay():
    uow = MemoryUow()
    base = command(
        uuid4(),
        event=uuid4(),
        hints=IdentityHints(
            contact_email=" Person@Example.Invalid ",
            company_name=" AＣＭＥ ",
            domain="ACME.INVALID.",
        ),
    )
    first = service(uow).apply_stage_transition(base)
    equivalent = replace(
        base,
        target_stage=" Meeting Booked ",
        occurred_at=base.occurred_at.astimezone(timezone(timedelta(hours=2))),
        identity=IdentityHints(
            contact_email="person@example.invalid",
            company_name="acme",
            domain="acme.invalid",
        ),
    )
    assert service(uow).apply_stage_transition(equivalent) == first


def test_activity_replay_uses_normalized_type_and_deterministic_lock():
    uow = MemoryUow()
    workspace, event = uuid4(), uuid4()
    account = uow.new_account(
        workspace, IdentityHints(company_name="Acme", domain="acme.invalid")
    )
    raw = AppendActivityCommand(
        workspace_id=workspace,
        account_id=account.id,
        activity_type=" note ",
        occurred_at=NOW,
        title="Normalized",
        ingest_event_id=event,
    )
    first = ActivityService(lambda: uow).append(raw)
    assert (
        ActivityService(lambda: uow).append(replace(raw, activity_type="note")) == first
    )
    assert len(uow.activities.rows) == 1
    assert uow.activity_replay_locks == [
        (workspace, event, "note"),
        (workspace, event, "note"),
    ]


def test_activity_service_rejects_generic_stage_change_append():
    uow = MemoryUow()
    workspace = uuid4()
    lead = uow.new_lead(workspace, IdentityHints())
    with pytest.raises(ValueError, match="activity requires review"):
        ActivityService(lambda: uow).append(
            AppendActivityCommand(
                workspace_id=workspace,
                account_id=None,
                lead_id=lead.id,
                activity_type="stage_change",
                occurred_at=NOW,
                title="Bypass",
                semantic_fingerprint="0" * 64,
            )
        )
