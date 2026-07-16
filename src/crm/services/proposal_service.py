"""Caller-transactional proposal versioning and portfolio projection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from src.crm.domain.money import normalize_currency
from src.crm.persistence.models import Proposal, ProposalItem, ProposalVersion


class ProposalReviewRequired(RuntimeError):
    """Proposal input or aggregate ownership requires human review."""


class ProposalConflictError(RuntimeError):
    """The proposal changed since the caller read it."""


@dataclass(frozen=True, slots=True)
class CreateProposalCommand:
    workspace_id: UUID
    account_id: UUID
    title: str
    currency: str
    lead_id: UUID | None = None
    proposal_number: str | None = None


@dataclass(frozen=True, slots=True)
class ProposalItemInput:
    description: str
    currency: str
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    billing_period: str | None = None
    option_group: str | None = None
    is_selected: bool = False
    amount: Decimal | None = None


@dataclass(frozen=True, slots=True)
class AppendProposalVersionCommand:
    workspace_id: UUID
    proposal_id: UUID
    expected_version: int
    status: str = "draft"
    sent_at: datetime | None = None
    valid_until: date | None = None
    one_off_amount: Decimal | None = None
    mrr_amount: Decimal | None = None
    arr_amount: Decimal | None = None
    tax_inclusion: str = "unknown"
    source_document_evidence_id: UUID | None = None
    extraction_confidence: Decimal | None = None
    confirmed_by: UUID | None = None
    confirmed_at: datetime | None = None
    items: tuple[ProposalItemInput, ...] = ()


@dataclass(frozen=True, slots=True)
class SelectProposalVersionCommand:
    workspace_id: UUID
    proposal_id: UUID
    version_id: UUID
    expected_version: int


@dataclass(frozen=True, slots=True)
class PortfolioProjection:
    totals: dict[str, dict[str, Decimal]]
    selected_options: dict[str, dict[str, dict[str, Decimal]]]
    missing_value_count: int
    candidate_value_count: int
    confirmed_value_count: int


def _review(message: str = "proposal requires review") -> ProposalReviewRequired:
    return ProposalReviewRequired(message)


def _uuid(value: object) -> bool:
    return type(value) is UUID


def _clean_text(value: object, *, maximum: int) -> str:
    if type(value) is not str:
        raise _review()
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum or any(ord(char) < 32 for char in cleaned):
        raise _review()
    return cleaned


def _optional_text(value: object, *, maximum: int) -> str | None:
    if value is None:
        return None
    return _clean_text(value, maximum=maximum)


def _amount(value: object) -> Decimal | None:
    if value is None:
        return None
    if (
        not isinstance(value, Decimal)
        or not value.is_finite()
        or value < 0
        or value > Decimal("9999999999999999.99")
    ):
        raise _review("proposal version requires review")
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int) or exponent < -2:
        raise _review("proposal version requires review")
    return value.quantize(Decimal("0.01"))


def _quantity(value: object) -> Decimal | None:
    if value is None:
        return None
    if (
        not isinstance(value, Decimal)
        or not value.is_finite()
        or value <= 0
        or value > Decimal("99999999999999.9999")
    ):
        raise _review("proposal version requires review")
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int) or exponent < -4:
        raise _review("proposal version requires review")
    return value.quantize(Decimal("0.0001"))


def _aware(value: object) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )


class ProposalService:
    """Mutate proposals inside a caller-owned unit of work without committing it."""

    def __init__(self, uow: Any):
        self.uow = uow

    def create_proposal(self, command: CreateProposalCommand) -> Proposal:
        if type(command) is not CreateProposalCommand:
            raise _review()
        if not _uuid(command.workspace_id) or not _uuid(command.account_id):
            raise _review()
        if command.lead_id is not None and not _uuid(command.lead_id):
            raise _review()
        title = _clean_text(command.title, maximum=512)
        proposal_number = _optional_text(command.proposal_number, maximum=512)
        currency = normalize_currency(command.currency)
        if currency is None:
            raise _review()

        account = self.uow.accounts.get(command.workspace_id, command.account_id)
        if account is None:
            raise _review()
        if command.lead_id is not None:
            lead = self.uow.leads.get(command.workspace_id, command.lead_id)
            if lead is None or lead.account_id != command.account_id:
                raise _review()

        return self.uow.proposals.add(
            Proposal(
                workspace_id=command.workspace_id,
                account_id=command.account_id,
                lead_id=command.lead_id,
                title=title,
                proposal_number=proposal_number,
                currency=currency,
                value_state="missing",
            )
        )

    def append_version(self, command: AppendProposalVersionCommand) -> ProposalVersion:
        if type(command) is not AppendProposalVersionCommand:
            raise _review("proposal version requires review")
        if not _uuid(command.workspace_id) or not _uuid(command.proposal_id):
            raise _review("proposal version requires review")
        proposal = self.uow.proposals.get(
            command.workspace_id, command.proposal_id, for_update=True
        )
        if proposal is None:
            raise _review("proposal version requires review")
        self._check_expected(proposal, command.expected_version)

        values = self._version_values(command)
        item_values = self._item_values(command.items, proposal.currency)
        versions = self.uow.proposal_versions.for_proposal(proposal.id)
        next_number = max((row.version_number for row in versions), default=0) + 1
        version = self.uow.proposal_versions.add(
            ProposalVersion(
                proposal_id=proposal.id,
                version_number=next_number,
                **values,
            )
        )
        for values in item_values:
            self.uow.proposal_items.add(
                ProposalItem(proposal_version_id=version.id, **values)
            )
        proposal.version += 1
        return version

    def select_version(self, command: SelectProposalVersionCommand) -> ProposalVersion:
        if type(command) is not SelectProposalVersionCommand:
            raise _review("proposal version requires review")
        if not all(
            _uuid(value)
            for value in (command.workspace_id, command.proposal_id, command.version_id)
        ):
            raise _review("proposal version requires review")
        proposal = self.uow.proposals.get(
            command.workspace_id, command.proposal_id, for_update=True
        )
        if proposal is None:
            raise _review("proposal version requires review")
        self._check_expected(proposal, command.expected_version)
        selected = self.uow.proposal_versions.get_for_proposal(
            proposal.id, command.version_id
        )
        if selected is None or selected.status in {"rejected", "superseded"}:
            raise _review("proposal version requires review")

        previous_id = proposal.selected_version_id
        if previous_id is not None and previous_id != selected.id:
            previous = self.uow.proposal_versions.get_for_proposal(
                proposal.id, previous_id
            )
            if previous is None:
                raise _review("proposal version requires review")
            previous.status = "superseded"
        proposal.selected_version_id = selected.id
        proposal.value_state = self._value_state(selected)
        proposal.version += 1
        return selected

    def portfolio(self, workspace_id: UUID) -> PortfolioProjection:
        if not _uuid(workspace_id):
            raise _review()
        totals: dict[str, dict[str, Decimal]] = {}
        eligible_version_ids: set[UUID] = set()
        value_counts = {"missing": 0, "candidate": 0, "confirmed": 0}
        for proposal, version in self.uow.portfolio_rows(workspace_id):
            if proposal.value_state in value_counts:
                value_counts[proposal.value_state] += 1
            if (
                version is None
                or proposal.value_state != "confirmed"
                or version.status in {"rejected", "superseded"}
                or version.source_document_evidence_id is None
                or version.confirmed_by is None
                or version.confirmed_at is None
            ):
                continue
            eligible_version_ids.add(version.id)
            currency_totals = totals.setdefault(proposal.currency, {})
            for dimension, amount in (
                ("one_off", version.one_off_amount),
                ("mrr", version.mrr_amount),
                ("arr", version.arr_amount),
            ):
                if amount is not None:
                    currency_totals[dimension] = (
                        currency_totals.get(dimension, Decimal("0.00")) + amount
                    )
            if not currency_totals:
                totals.pop(proposal.currency, None)

        selected_options: dict[str, dict[str, dict[str, Decimal]]] = {}
        for item in self.uow.proposal_items.for_versions(eligible_version_ids):
            if not item.is_selected or item.option_group is None or item.amount is None:
                continue
            group = selected_options.setdefault(item.option_group, {})
            currency = group.setdefault(item.currency, {})
            dimension = item.billing_period or "one_off"
            currency[dimension] = currency.get(dimension, Decimal("0.00")) + item.amount
        return PortfolioProjection(
            totals=totals,
            selected_options=selected_options,
            missing_value_count=value_counts["missing"],
            candidate_value_count=value_counts["candidate"],
            confirmed_value_count=value_counts["confirmed"],
        )

    @staticmethod
    def _check_expected(proposal: Proposal, expected: int) -> None:
        if type(expected) is not int or proposal.version != expected:
            raise ProposalConflictError("proposal changed; retry required") from None

    @staticmethod
    def _value_state(version: ProposalVersion) -> str:
        if (
            version.confirmed_by is not None
            and version.confirmed_at is not None
            and version.source_document_evidence_id is not None
            and any(
                amount is not None
                for amount in (
                    version.one_off_amount,
                    version.mrr_amount,
                    version.arr_amount,
                )
            )
        ):
            return "confirmed"
        if any(
            amount is not None
            for amount in (
                version.one_off_amount,
                version.mrr_amount,
                version.arr_amount,
            )
        ):
            return "candidate"
        return "missing"

    @staticmethod
    def _version_values(command: AppendProposalVersionCommand) -> dict[str, object]:
        if command.status not in {"draft", "sent", "accepted", "rejected"}:
            raise _review("proposal version requires review")
        if command.status == "sent" and not _aware(command.sent_at):
            raise _review("proposal version requires review")
        if command.sent_at is not None and not _aware(command.sent_at):
            raise _review("proposal version requires review")
        if command.valid_until is not None and type(command.valid_until) is not date:
            raise _review("proposal version requires review")
        if command.tax_inclusion not in {"exclusive", "inclusive", "unknown"}:
            raise _review("proposal version requires review")
        one_off_amount = _amount(command.one_off_amount)
        mrr_amount = _amount(command.mrr_amount)
        arr_amount = _amount(command.arr_amount)
        if (command.confirmed_by is None) != (command.confirmed_at is None):
            raise _review("proposal version requires review")
        if (
            command.confirmed_by is not None
            and command.source_document_evidence_id is None
        ):
            raise _review("proposal version requires review")
        if command.confirmed_by is not None and not _uuid(command.confirmed_by):
            raise _review("proposal version requires review")
        if command.confirmed_at is not None and not _aware(command.confirmed_at):
            raise _review("proposal version requires review")
        if command.confirmed_by is not None and not any(
            amount is not None for amount in (one_off_amount, mrr_amount, arr_amount)
        ):
            raise _review("proposal version requires review")
        if command.source_document_evidence_id is not None and not _uuid(
            command.source_document_evidence_id
        ):
            raise _review("proposal version requires review")
        confidence = command.extraction_confidence
        confidence_exponent = (
            confidence.as_tuple().exponent if isinstance(confidence, Decimal) else None
        )
        if confidence is not None and (
            not isinstance(confidence, Decimal)
            or not confidence.is_finite()
            or not Decimal("0") <= confidence <= Decimal("1")
            or not isinstance(confidence_exponent, int)
            or confidence_exponent < -4
        ):
            raise _review("proposal version requires review")
        if confidence is not None:
            confidence = confidence.quantize(Decimal("0.0001"))
        return {
            "status": command.status,
            "sent_at": command.sent_at,
            "valid_until": command.valid_until,
            "one_off_amount": one_off_amount,
            "mrr_amount": mrr_amount,
            "arr_amount": arr_amount,
            "tax_inclusion": command.tax_inclusion,
            "source_document_evidence_id": command.source_document_evidence_id,
            "extraction_confidence": confidence,
            "confirmed_by": command.confirmed_by,
            "confirmed_at": command.confirmed_at,
        }

    @staticmethod
    def _item_values(items: object, proposal_currency: str) -> list[dict[str, object]]:
        if type(items) is not tuple:
            raise _review("proposal version requires review")
        result: list[dict[str, object]] = []
        selected_groups: set[str] = set()
        for item in items:
            if (
                type(item) is not ProposalItemInput
                or type(item.is_selected) is not bool
            ):
                raise _review("proposal version requires review")
            description = _clean_text(item.description, maximum=2048)
            currency = normalize_currency(item.currency)
            if currency is None or currency != proposal_currency:
                raise _review("proposal version requires review")
            billing_period = _optional_text(item.billing_period, maximum=32)
            if billing_period not in {None, "mrr", "arr"}:
                raise _review("proposal version requires review")
            group = _optional_text(item.option_group, maximum=512)
            if group is not None and item.is_selected:
                if group in selected_groups:
                    raise _review("proposal version requires review")
                selected_groups.add(group)
            quantity = _quantity(item.quantity)
            result.append(
                {
                    "description": description,
                    "currency": currency,
                    "quantity": quantity,
                    "unit_price": _amount(item.unit_price),
                    "billing_period": billing_period,
                    "option_group": group,
                    "is_selected": item.is_selected,
                    "amount": _amount(item.amount),
                }
            )
        return result
