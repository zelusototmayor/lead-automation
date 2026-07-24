from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from src.crm.persistence.models import (
    Account,
    Lead,
    Proposal,
    ProposalItem,
    ProposalVersion,
)
from src.crm.persistence.repositories import (
    ProposalItemRepository,
    ProposalRepository,
    ProposalVersionRepository,
)
from src.crm.persistence.unit_of_work import SqlAlchemyUnitOfWork
from src.crm.services.proposal_service import (
    AppendProposalVersionCommand,
    CreateProposalCommand,
    PortfolioProjection,
    ProposalConflictError,
    ProposalItemInput,
    ProposalReviewRequired,
    ProposalService,
    SelectProposalVersionCommand,
)

NOW = datetime(2026, 7, 16, 12, tzinfo=UTC)


class MemoryRepo:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.get_calls: list[tuple[object, object, bool]] = []

    def get(self, workspace_id, row_id, *, for_update=False):
        self.get_calls.append((workspace_id, row_id, for_update))
        return next(
            (
                row
                for row in self.rows
                if row.id == row_id
                and (
                    not hasattr(row, "workspace_id") or row.workspace_id == workspace_id
                )
            ),
            None,
        )

    def add(self, row):
        if getattr(row, "id", None) is None:
            row.id = uuid4()
        self.rows.append(row)
        return row


class VersionRepo(MemoryRepo):
    def for_proposal(self, proposal_id):
        return [row for row in self.rows if row.proposal_id == proposal_id]

    def get_for_proposal(self, proposal_id, version_id):
        return next(
            (
                row
                for row in self.rows
                if row.proposal_id == proposal_id and row.id == version_id
            ),
            None,
        )


class ItemRepo(MemoryRepo):
    def for_versions(self, version_ids):
        return [row for row in self.rows if row.proposal_version_id in version_ids]


class MemoryUow:
    def __init__(self):
        self.accounts = MemoryRepo()
        self.leads = MemoryRepo()
        self.proposals = MemoryRepo()
        self.proposal_versions = VersionRepo()
        self.proposal_items = ItemRepo()
        self.commits = 0

    def commit(self):
        self.commits += 1

    def portfolio_rows(self, workspace_id):
        rows = []
        for proposal in self.proposals.rows:
            if proposal.workspace_id != workspace_id:
                continue
            version = (
                self.proposal_versions.get_for_proposal(
                    proposal.id, proposal.selected_version_id
                )
                if proposal.selected_version_id is not None
                else None
            )
            rows.append((proposal, version))
        return rows


def aggregate(uow: MemoryUow, *, with_lead: bool = False):
    workspace_id, account_id, lead_id = uuid4(), uuid4(), uuid4()
    uow.accounts.add(
        Account(
            id=account_id,
            workspace_id=workspace_id,
            display_name="Acme",
            normalized_name="acme",
        )
    )
    if with_lead:
        uow.leads.add(
            Lead(
                id=lead_id,
                workspace_id=workspace_id,
                account_id=account_id,
            )
        )
    return workspace_id, account_id, lead_id


def proposal_row(workspace_id, account_id, **values):
    defaults = dict(
        id=uuid4(),
        workspace_id=workspace_id,
        account_id=account_id,
        title="Proposal",
        currency="EUR",
        version=1,
        value_state="missing",
    )
    defaults.update(values)
    return Proposal(**defaults)


def version_row(proposal_id, number, **values):
    defaults = dict(
        id=uuid4(), proposal_id=proposal_id, version_number=number, status="draft"
    )
    defaults.update(values)
    return ProposalVersion(**defaults)


def test_create_proposal_is_scoped_normalizes_currency_and_leaves_commit_to_caller():
    uow = MemoryUow()
    workspace_id, account_id, lead_id = aggregate(uow, with_lead=True)

    created = ProposalService(uow).create_proposal(
        CreateProposalCommand(
            workspace_id=workspace_id,
            account_id=account_id,
            lead_id=lead_id,
            title="  Migration proposal  ",
            currency=" eur ",
        )
    )

    assert created.workspace_id == workspace_id
    assert created.account_id == account_id
    assert created.lead_id == lead_id
    assert created.title == "Migration proposal"
    assert created.currency == "EUR"
    assert created.value_state == "missing"
    assert uow.commits == 0


def test_create_rejects_cross_account_lead_and_does_not_leak_input():
    uow = MemoryUow()
    workspace_id, account_id, _ = aggregate(uow)
    secret = "secret-invalid-currency"

    with pytest.raises(ProposalReviewRequired) as error:
        ProposalService(uow).create_proposal(
            CreateProposalCommand(
                workspace_id=workspace_id,
                account_id=account_id,
                lead_id=uuid4(),
                title="Proposal",
                currency=secret,
            )
        )

    assert secret not in str(error.value)
    assert not uow.proposals.rows


def test_append_locks_proposal_and_allocates_after_highest_preserved_version():
    uow = MemoryUow()
    workspace_id, account_id, _ = aggregate(uow)
    proposal = uow.proposals.add(proposal_row(workspace_id, account_id, version=4))
    old_one = uow.proposal_versions.add(version_row(proposal.id, 1))
    old_three = uow.proposal_versions.add(version_row(proposal.id, 3))

    appended = ProposalService(uow).append_version(
        AppendProposalVersionCommand(
            workspace_id=workspace_id,
            proposal_id=proposal.id,
            expected_version=4,
            one_off_amount=Decimal("125.00"),
        )
    )

    assert appended.version_number == 4
    assert appended.one_off_amount == Decimal("125.00")
    assert proposal.version == 5
    assert [old_one, old_three] == uow.proposal_versions.rows[:2]
    assert uow.proposals.get_calls[-1] == (workspace_id, proposal.id, True)
    assert uow.commits == 0

    with pytest.raises(ProposalConflictError):
        ProposalService(uow).append_version(
            AppendProposalVersionCommand(
                workspace_id=workspace_id,
                proposal_id=proposal.id,
                expected_version=4,
            )
        )


def test_append_preserves_unknown_and_confirmed_zero_requires_provenance():
    uow = MemoryUow()
    workspace_id, account_id, _ = aggregate(uow)
    proposal = uow.proposals.add(proposal_row(workspace_id, account_id))
    service = ProposalService(uow)

    unknown = service.append_version(
        AppendProposalVersionCommand(
            workspace_id=workspace_id,
            proposal_id=proposal.id,
            expected_version=1,
        )
    )
    assert unknown.one_off_amount is None
    assert unknown.mrr_amount is None
    assert unknown.arr_amount is None

    with pytest.raises(
        ProposalReviewRequired, match="proposal version requires review"
    ):
        service.append_version(
            AppendProposalVersionCommand(
                workspace_id=workspace_id,
                proposal_id=proposal.id,
                expected_version=2,
                one_off_amount=Decimal("0.00"),
                confirmed_by=uuid4(),
            )
        )

    confirmer = uuid4()
    with pytest.raises(
        ProposalReviewRequired, match="proposal version requires review"
    ):
        service.append_version(
            AppendProposalVersionCommand(
                workspace_id=workspace_id,
                proposal_id=proposal.id,
                expected_version=2,
                one_off_amount=Decimal("0.00"),
                confirmed_by=confirmer,
                confirmed_at=NOW,
            )
        )

    evidence_id = uuid4()
    zero = service.append_version(
        AppendProposalVersionCommand(
            workspace_id=workspace_id,
            proposal_id=proposal.id,
            expected_version=2,
            one_off_amount=Decimal("0.00"),
            source_document_evidence_id=evidence_id,
            confirmed_by=confirmer,
            confirmed_at=NOW,
        )
    )
    assert zero.one_off_amount == Decimal("0.00")
    assert zero.confirmed_by == confirmer
    assert zero.confirmed_at == NOW
    assert zero.source_document_evidence_id == evidence_id


def test_append_rejects_confirmation_without_any_amount_before_persistence():
    uow = MemoryUow()
    workspace_id, account_id, _ = aggregate(uow)
    proposal = uow.proposals.add(proposal_row(workspace_id, account_id))

    with pytest.raises(
        ProposalReviewRequired, match="proposal version requires review"
    ):
        ProposalService(uow).append_version(
            AppendProposalVersionCommand(
                workspace_id=workspace_id,
                proposal_id=proposal.id,
                expected_version=1,
                source_document_evidence_id=uuid4(),
                confirmed_by=uuid4(),
                confirmed_at=NOW,
            )
        )

    assert not uow.proposal_versions.rows


def test_append_rejects_overprecise_extraction_confidence_before_persistence():
    uow = MemoryUow()
    workspace_id, account_id, _ = aggregate(uow)
    proposal = uow.proposals.add(proposal_row(workspace_id, account_id))

    with pytest.raises(
        ProposalReviewRequired, match="proposal version requires review"
    ):
        ProposalService(uow).append_version(
            AppendProposalVersionCommand(
                workspace_id=workspace_id,
                proposal_id=proposal.id,
                expected_version=1,
                extraction_confidence=Decimal("0.12345"),
            )
        )

    assert not uow.proposal_versions.rows


def test_append_rejects_numeric_overflow_before_persistence_without_echoing_value():
    uow = MemoryUow()
    workspace_id, account_id, _ = aggregate(uow)
    proposal = uow.proposals.add(proposal_row(workspace_id, account_id))
    oversized = Decimal("10000000000000000.00")

    with pytest.raises(ProposalReviewRequired) as error:
        ProposalService(uow).append_version(
            AppendProposalVersionCommand(
                workspace_id=workspace_id,
                proposal_id=proposal.id,
                expected_version=1,
                one_off_amount=oversized,
            )
        )

    assert str(oversized) not in str(error.value)
    assert not uow.proposal_versions.rows


def test_append_expected_version_conflict_is_generic():
    uow = MemoryUow()
    workspace_id, account_id, _ = aggregate(uow)
    proposal = uow.proposals.add(proposal_row(workspace_id, account_id, version=7))

    with pytest.raises(ProposalConflictError) as error:
        ProposalService(uow).append_version(
            AppendProposalVersionCommand(
                workspace_id=workspace_id,
                proposal_id=proposal.id,
                expected_version=6,
            )
        )

    assert str(proposal.id) not in str(error.value)
    assert "6" not in str(error.value)
    assert "7" not in str(error.value)


def test_value_mutations_reject_missing_expected_version():
    uow = MemoryUow()
    workspace_id, account_id, _ = aggregate(uow)
    proposal = uow.proposals.add(proposal_row(workspace_id, account_id))
    version = uow.proposal_versions.add(version_row(proposal.id, 1))
    service = ProposalService(uow)

    with pytest.raises(ProposalConflictError):
        service.append_version(
            AppendProposalVersionCommand(
                workspace_id=workspace_id,
                proposal_id=proposal.id,
                expected_version=None,
            )
        )
    with pytest.raises(ProposalConflictError):
        service.select_version(
            SelectProposalVersionCommand(
                workspace_id=workspace_id,
                proposal_id=proposal.id,
                version_id=version.id,
                expected_version=None,
            )
        )


def test_select_replaces_only_previously_selected_version_and_preserves_history():
    uow = MemoryUow()
    workspace_id, account_id, _ = aggregate(uow)
    proposal = uow.proposals.add(proposal_row(workspace_id, account_id, version=2))
    previous = uow.proposal_versions.add(
        version_row(
            proposal.id, 1, status="sent", confirmed_by=uuid4(), confirmed_at=NOW
        )
    )
    unrelated = uow.proposal_versions.add(version_row(proposal.id, 2, status="sent"))
    replacement = uow.proposal_versions.add(
        version_row(
            proposal.id,
            3,
            status="sent",
            one_off_amount=Decimal("0.00"),
            source_document_evidence_id=uuid4(),
            confirmed_by=uuid4(),
            confirmed_at=NOW,
        )
    )
    proposal.selected_version_id = previous.id

    selected = ProposalService(uow).select_version(
        SelectProposalVersionCommand(
            workspace_id=workspace_id,
            proposal_id=proposal.id,
            version_id=replacement.id,
            expected_version=2,
        )
    )

    assert selected is replacement
    assert proposal.selected_version_id == replacement.id
    assert proposal.value_state == "confirmed"
    assert previous.status == "superseded"
    assert unrelated.status == "sent"
    assert len(uow.proposal_versions.rows) == 3
    assert uow.commits == 0


@pytest.mark.parametrize("status", ["rejected", "superseded"])
def test_select_rejects_ineligible_version(status):
    uow = MemoryUow()
    workspace_id, account_id, _ = aggregate(uow)
    proposal = uow.proposals.add(proposal_row(workspace_id, account_id))
    version = uow.proposal_versions.add(version_row(proposal.id, 1, status=status))

    with pytest.raises(
        ProposalReviewRequired, match="proposal version requires review"
    ):
        ProposalService(uow).select_version(
            SelectProposalVersionCommand(workspace_id, proposal.id, version.id, 1)
        )

    assert proposal.selected_version_id is None


def test_select_rejects_version_owned_by_another_proposal():
    uow = MemoryUow()
    workspace_id, account_id, _ = aggregate(uow)
    proposal = uow.proposals.add(proposal_row(workspace_id, account_id))
    other = uow.proposals.add(proposal_row(workspace_id, account_id))
    version = uow.proposal_versions.add(version_row(other.id, 1))

    with pytest.raises(
        ProposalReviewRequired, match="proposal version requires review"
    ):
        ProposalService(uow).select_version(
            SelectProposalVersionCommand(workspace_id, proposal.id, version.id, 1)
        )


def test_portfolio_uses_only_selected_confirmed_eligible_versions_by_currency():
    uow = MemoryUow()
    workspace_id, account_id, _ = aggregate(uow)
    service = ProposalService(uow)

    eligible = uow.proposals.add(
        proposal_row(workspace_id, account_id, value_state="confirmed")
    )
    eligible_version = uow.proposal_versions.add(
        version_row(
            eligible.id,
            2,
            status="sent",
            one_off_amount=Decimal("100.00"),
            mrr_amount=Decimal("20.00"),
            arr_amount=Decimal("240.00"),
            source_document_evidence_id=uuid4(),
            confirmed_by=uuid4(),
            confirmed_at=NOW,
        )
    )
    eligible.selected_version_id = eligible_version.id

    usd = uow.proposals.add(
        proposal_row(workspace_id, account_id, currency="USD", value_state="confirmed")
    )
    usd_version = uow.proposal_versions.add(
        version_row(
            usd.id,
            1,
            status="accepted",
            one_off_amount=Decimal("50.00"),
            source_document_evidence_id=uuid4(),
            confirmed_by=uuid4(),
            confirmed_at=NOW,
        )
    )
    usd.selected_version_id = usd_version.id

    candidate = uow.proposals.add(
        proposal_row(workspace_id, account_id, value_state="candidate")
    )
    candidate_version = uow.proposal_versions.add(
        version_row(candidate.id, 1, one_off_amount=Decimal("999.00"))
    )
    candidate.selected_version_id = candidate_version.id

    stale = uow.proposals.add(
        proposal_row(workspace_id, account_id, value_state="rejected")
    )
    stale_version = uow.proposal_versions.add(
        version_row(
            stale.id,
            1,
            status="superseded",
            one_off_amount=Decimal("888.00"),
            source_document_evidence_id=uuid4(),
            confirmed_by=uuid4(),
            confirmed_at=NOW,
        )
    )
    stale.selected_version_id = stale_version.id
    uow.proposals.add(proposal_row(workspace_id, account_id, value_state="missing"))

    projection = service.portfolio(workspace_id)

    assert projection.totals == {
        "EUR": {
            "one_off": Decimal("100.00"),
            "mrr": Decimal("20.00"),
            "arr": Decimal("240.00"),
        },
        "USD": {"one_off": Decimal("50.00")},
    }
    assert projection.missing_value_count == 1
    assert projection.candidate_value_count == 1
    assert projection.confirmed_value_count == 2


def test_portfolio_excludes_nonconfirmed_proposal_value_states_even_with_provenance():
    uow = MemoryUow()
    workspace_id, account_id, _ = aggregate(uow)
    for value_state in ("missing", "candidate", "rejected"):
        proposal = uow.proposals.add(
            proposal_row(workspace_id, account_id, value_state=value_state)
        )
        version = uow.proposal_versions.add(
            version_row(
                proposal.id,
                1,
                status="sent",
                one_off_amount=Decimal("100.00"),
                confirmed_by=uuid4(),
                confirmed_at=NOW,
            )
        )
        proposal.selected_version_id = version.id

    assert ProposalService(uow).portfolio(workspace_id).totals == {}


def test_portfolio_option_groups_include_selected_items_without_double_counting_totals():
    uow = MemoryUow()
    workspace_id, account_id, _ = aggregate(uow)
    proposal = uow.proposals.add(
        proposal_row(workspace_id, account_id, value_state="confirmed")
    )
    version = uow.proposal_versions.add(
        version_row(
            proposal.id,
            1,
            status="sent",
            one_off_amount=Decimal("100.00"),
            source_document_evidence_id=uuid4(),
            confirmed_by=uuid4(),
            confirmed_at=NOW,
        )
    )
    proposal.selected_version_id = version.id
    for group, selected, amount, billing_period in (
        ("hosting", True, "60.00", "mrr"),
        ("hosting", False, "90.00", "arr"),
        (None, True, "40.00", None),
    ):
        uow.proposal_items.add(
            ProposalItem(
                id=uuid4(),
                proposal_version_id=version.id,
                description="Option",
                option_group=group,
                is_selected=selected,
                amount=Decimal(amount),
                currency="EUR",
                billing_period=billing_period,
            )
        )

    setup_proposal = uow.proposals.add(
        proposal_row(workspace_id, account_id, value_state="confirmed")
    )
    setup_version = uow.proposal_versions.add(
        version_row(
            setup_proposal.id,
            1,
            status="sent",
            one_off_amount=Decimal("25.00"),
            source_document_evidence_id=uuid4(),
            confirmed_by=uuid4(),
            confirmed_at=NOW,
        )
    )
    setup_proposal.selected_version_id = setup_version.id
    uow.proposal_items.add(
        ProposalItem(
            id=uuid4(),
            proposal_version_id=setup_version.id,
            description="Setup option",
            option_group="hosting",
            is_selected=True,
            amount=Decimal("40.00"),
            currency="EUR",
            billing_period=None,
        )
    )

    projection = ProposalService(uow).portfolio(workspace_id)

    assert isinstance(projection, PortfolioProjection)
    assert projection.totals == {"EUR": {"one_off": Decimal("125.00")}}
    assert projection.selected_options == {
        "hosting": {
            "EUR": {
                "one_off": Decimal("40.00"),
                "mrr": Decimal("60.00"),
            }
        }
    }


@pytest.mark.parametrize(
    "quantity",
    (Decimal("1.23456"), Decimal("100000000000000.0000")),
)
def test_append_items_rejects_quantity_that_would_round_or_overflow(quantity):
    uow = MemoryUow()
    workspace_id, account_id, _ = aggregate(uow)
    proposal = uow.proposals.add(proposal_row(workspace_id, account_id))

    with pytest.raises(
        ProposalReviewRequired, match="proposal version requires review"
    ):
        ProposalService(uow).append_version(
            AppendProposalVersionCommand(
                workspace_id=workspace_id,
                proposal_id=proposal.id,
                expected_version=1,
                items=(ProposalItemInput("A", "EUR", quantity=quantity),),
            )
        )

    assert not uow.proposal_versions.rows
    assert not uow.proposal_items.rows


def test_append_items_rejects_currency_that_differs_from_proposal():
    uow = MemoryUow()
    workspace_id, account_id, _ = aggregate(uow)
    proposal = uow.proposals.add(proposal_row(workspace_id, account_id, currency="EUR"))

    with pytest.raises(
        ProposalReviewRequired, match="proposal version requires review"
    ):
        ProposalService(uow).append_version(
            AppendProposalVersionCommand(
                workspace_id=workspace_id,
                proposal_id=proposal.id,
                expected_version=1,
                items=(ProposalItemInput("A", "USD", amount=Decimal("10.00")),),
            )
        )

    assert not uow.proposal_versions.rows
    assert not uow.proposal_items.rows


def test_append_items_only_marks_one_option_selected_per_group():
    uow = MemoryUow()
    workspace_id, account_id, _ = aggregate(uow)
    proposal = uow.proposals.add(proposal_row(workspace_id, account_id))

    with pytest.raises(
        ProposalReviewRequired, match="proposal version requires review"
    ):
        ProposalService(uow).append_version(
            AppendProposalVersionCommand(
                workspace_id=workspace_id,
                proposal_id=proposal.id,
                expected_version=1,
                items=(
                    ProposalItemInput(
                        "A", "EUR", option_group="choice", is_selected=True
                    ),
                    ProposalItemInput(
                        "B", "EUR", option_group="choice", is_selected=True
                    ),
                ),
            )
        )

    assert not uow.proposal_versions.rows
    assert not uow.proposal_items.rows


def test_sqlalchemy_uow_exposes_proposal_repositories_and_portfolio_projection_rows():
    session = MagicMock()
    session.execute.return_value.all.return_value = [("proposal", "version")]
    factory = MagicMock(return_value=session)

    with SqlAlchemyUnitOfWork(factory) as uow:
        assert isinstance(uow.proposals, ProposalRepository)
        assert isinstance(uow.proposal_versions, ProposalVersionRepository)
        assert isinstance(uow.proposal_items, ProposalItemRepository)
        assert uow.portfolio_rows(uuid4()) == [("proposal", "version")]


def test_proposal_version_and_item_repositories_keep_history_queries_scoped():
    session = MagicMock()
    version = SimpleNamespace(id=uuid4())
    item = SimpleNamespace(id=uuid4())
    session.scalars.side_effect = [(row for row in [version]), (row for row in [item])]
    session.scalar.return_value = version
    versions = ProposalVersionRepository(session)
    items = ProposalItemRepository(session)
    proposal_id = uuid4()

    assert versions.for_proposal(proposal_id) == [version]
    assert versions.get_for_proposal(proposal_id, version.id) is version
    assert items.for_versions({version.id}) == [item]
    assert items.for_versions(set()) == []
