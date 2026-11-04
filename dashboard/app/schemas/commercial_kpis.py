"""Strict command wire shapes; no client-owned tenant, actor or provenance."""

from datetime import date
from typing import Annotated, Literal

from pydantic import Field, RootModel, model_validator

from src.crm.domain.commercial_kpi_contract import (
    CanonicalUUID,
    CorrectionV1,
    Counter,
    Hash,
    ProposalV1,
    StrictValue,
)


class CorrectionCommand(StrictValue):
    command_id: CanonicalUUID
    expected_source_digest: Hash
    expected_context_digest: Hash
    expected_override_revision: Counter
    operation: Literal["set", "remove"]
    correction: CorrectionV1 | None

    @model_validator(mode="after")
    def coherent_operation(self):
        if (self.operation == "set") != (self.correction is not None):
            raise ValueError("Set requires correction; remove requires null")
        return self


class AssessmentCommand(StrictValue):
    command_id: CanonicalUUID
    expected_source_digest: Hash
    expected_context_digest: Hash
    assessment: ProposalV1


class RunCommand(StrictValue):
    run_id: CanonicalUUID
    entrypoint: Literal["sales_13h", "sales_1730"]
    mode: Literal["classification_only"]
    anchor_date: date
    operation: Literal["start", "finish"]
    expected_checkpoint: Hash | None


class AckPageCommand(StrictValue):
    operation: Literal["ack_page"]
    run_id: CanonicalUUID
    page_token: Hash
    receipt_ids: list[CanonicalUUID] = Field(max_length=100)
    expected_checkpoint: Hash


class ReconciliationCommand(
    RootModel[Annotated[RunCommand | AckPageCommand, Field(discriminator="operation")]]
):
    pass
