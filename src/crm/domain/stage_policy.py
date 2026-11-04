from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Mapping

from .enums import CRMStage


class StagePolicyError(ValueError):
    """Base class for explicit CRM stage policy failures."""


class UnknownStageError(StagePolicyError):
    """A stage value is empty, unknown, or not an exact documented alias."""


class AccountRequirementReviewRequired(StagePolicyError):
    """Original terminal history is missing, so a human decision is required."""


class InvalidTransitionError(StagePolicyError):
    """A transition violates terminal stage rules."""


class InvalidHighestRankError(StagePolicyError):
    """A highest-rank value is not a non-negative integer."""


class InvalidAccountEvidenceError(StagePolicyError):
    """Persisted account evidence is not a boolean decision."""


class ConflictingAccountEvidenceError(StagePolicyError):
    """Original rank and persisted account decisions disagree."""


class InvalidCorrectionFlagError(StagePolicyError):
    """Reviewed-correction authorization is not an exact boolean."""


@dataclass(frozen=True, slots=True)
class StageDefinition:
    rank: int
    terminal: bool


STAGE_CATALOG: Final[Mapping[CRMStage, StageDefinition]] = MappingProxyType(
    {
        CRMStage.NEW: StageDefinition(rank=10, terminal=False),
        CRMStage.CONTACTED: StageDefinition(rank=20, terminal=False),
        CRMStage.QUALIFIED: StageDefinition(rank=30, terminal=False),
        CRMStage.MEETING_BOOKED: StageDefinition(rank=40, terminal=False),
        CRMStage.MEETING_HELD: StageDefinition(rank=50, terminal=False),
        CRMStage.PROPOSAL_REQUESTED: StageDefinition(rank=60, terminal=False),
        CRMStage.PROPOSAL_SENT: StageDefinition(rank=70, terminal=False),
        CRMStage.NEGOTIATION: StageDefinition(rank=80, terminal=False),
        CRMStage.WON: StageDefinition(rank=90, terminal=True),
        CRMStage.LOST: StageDefinition(rank=90, terminal=True),
        CRMStage.NOT_A_FIT: StageDefinition(rank=90, terminal=True),
    }
)
MAX_STAGE_RANK: Final[int] = max(
    definition.rank for definition in STAGE_CATALOG.values()
)
TERMINAL_STAGE_RANKS: Final[frozenset[int]] = frozenset(
    definition.rank
    for definition in STAGE_CATALOG.values()
    if definition.terminal
)

_DISPLAY_ALIASES: Final[Mapping[str, CRMStage]] = MappingProxyType(
    {
        "meeting booked": CRMStage.MEETING_BOOKED,
        "proposal sent": CRMStage.PROPOSAL_SENT,
        "won": CRMStage.WON,
    }
)
_CANONICAL_NAMES: Final[Mapping[str, CRMStage]] = MappingProxyType(
    {stage.value: stage for stage in CRMStage}
)
_CONDITIONAL_TERMINALS: Final[frozenset[CRMStage]] = frozenset(
    {CRMStage.LOST, CRMStage.NOT_A_FIT}
)
_ACCOUNT_REQUIRED: Final[frozenset[CRMStage]] = frozenset(
    {
        CRMStage.MEETING_BOOKED,
        CRMStage.MEETING_HELD,
        CRMStage.PROPOSAL_REQUESTED,
        CRMStage.PROPOSAL_SENT,
        CRMStage.NEGOTIATION,
        CRMStage.WON,
    }
)


def normalize_stage(value: str) -> str:
    """Normalize only case and whitespace, without fuzzy transformations."""
    if not isinstance(value, str):
        raise UnknownStageError("unknown CRM stage; review required")
    return " ".join(value.split()).casefold()


def resolve_stage(value: CRMStage | str) -> CRMStage:
    """Resolve a canonical name or one of the three documented display aliases."""
    if isinstance(value, CRMStage):
        return value

    normalized = normalize_stage(value)
    if not normalized:
        raise UnknownStageError("unknown CRM stage; review required")

    stage = _CANONICAL_NAMES.get(normalized) or _DISPLAY_ALIASES.get(normalized)
    if stage is None:
        raise UnknownStageError("unknown CRM stage; review required")
    return stage


def stage_rank(stage: CRMStage | str) -> int:
    return STAGE_CATALOG[resolve_stage(stage)].rank


def is_terminal_stage(stage: CRMStage | str) -> bool:
    return STAGE_CATALOG[resolve_stage(stage)].terminal


def _validate_previous_rank(previous_highest_rank: object) -> int:
    if type(previous_highest_rank) is not int:
        raise InvalidHighestRankError(
            f"previous_highest_rank must be an integer from 0 to {MAX_STAGE_RANK}"
        )
    if not 0 <= previous_highest_rank <= MAX_STAGE_RANK:
        raise InvalidHighestRankError(
            f"previous_highest_rank must be an integer from 0 to {MAX_STAGE_RANK}"
        )
    return previous_highest_rank


def requires_account(
    stage: CRMStage | str,
    previous_highest_rank: int | None = None,
    persisted_terminal_requires_account: bool | None = None,
) -> bool:
    """Return account policy, requiring original evidence for conditional terminals."""
    resolved = resolve_stage(stage)

    if (
        persisted_terminal_requires_account is not None
        and type(persisted_terminal_requires_account) is not bool
    ):
        raise InvalidAccountEvidenceError(
            "persisted_terminal_requires_account must be a boolean"
        )

    previous = (
        _validate_previous_rank(previous_highest_rank)
        if previous_highest_rank is not None
        else None
    )

    if resolved in _ACCOUNT_REQUIRED:
        return True
    if resolved not in _CONDITIONAL_TERMINALS:
        return False

    if previous is None:
        if persisted_terminal_requires_account is not None:
            return persisted_terminal_requires_account
        raise AccountRequirementReviewRequired(
            "lost/not_a_fit requires original pre-terminal account evidence"
        )

    if previous in TERMINAL_STAGE_RANKS:
        if persisted_terminal_requires_account is not None:
            return persisted_terminal_requires_account
        raise AccountRequirementReviewRequired(
            "terminal rank 90 is not original pre-terminal history evidence"
        )

    rank_requires_account = previous >= 40
    if (
        persisted_terminal_requires_account is not None
        and persisted_terminal_requires_account is not rank_requires_account
    ):
        raise ConflictingAccountEvidenceError(
            "previous rank and persisted terminal account evidence disagree"
        )
    return rank_requires_account


def highest_stage_rank(
    previous_highest_rank: int, incoming_stage: CRMStage | str
) -> int:
    """Apply an incoming explicit stage rank without decreasing history."""
    previous = _validate_previous_rank(previous_highest_rank)
    return max(previous, stage_rank(incoming_stage))


def validate_transition(
    current_stage: CRMStage | str,
    target_stage: CRMStage | str,
    reviewed_correction: bool = False,
) -> None:
    """Require reviewed correction for any exit from a terminal to a different stage."""
    if type(reviewed_correction) is not bool:
        raise InvalidCorrectionFlagError("reviewed_correction must be a boolean")

    current = resolve_stage(current_stage)
    target = resolve_stage(target_stage)
    if (
        current is not target
        and is_terminal_stage(current)
        and not reviewed_correction
    ):
        raise InvalidTransitionError(
            f"terminal transition {current.value!r} -> {target.value!r} "
            "requires reviewed_correction=True"
        )
