from __future__ import annotations

from dataclasses import FrozenInstanceError
from types import MappingProxyType

import pytest

from src.crm.domain import stage_policy
from src.crm.domain.enums import CRMStage
from src.crm.domain.stage_policy import (
    MAX_STAGE_RANK,
    STAGE_CATALOG,
    AccountRequirementReviewRequired,
    ConflictingAccountEvidenceError,
    InvalidAccountEvidenceError,
    InvalidCorrectionFlagError,
    InvalidHighestRankError,
    InvalidTransitionError,
    StageDefinition,
    UnknownStageError,
    highest_stage_rank,
    is_terminal_stage,
    normalize_stage,
    requires_account,
    resolve_stage,
    stage_rank,
    validate_transition,
)


STAGE_CASES = (
    (CRMStage.NEW, "new", 10, False),
    (CRMStage.CONTACTED, "contacted", 20, False),
    (CRMStage.QUALIFIED, "qualified", 30, False),
    (CRMStage.MEETING_BOOKED, "meeting_booked", 40, False),
    (CRMStage.MEETING_HELD, "meeting_held", 50, False),
    (CRMStage.PROPOSAL_REQUESTED, "proposal_requested", 60, False),
    (CRMStage.PROPOSAL_SENT, "proposal_sent", 70, False),
    (CRMStage.NEGOTIATION, "negotiation", 80, False),
    (CRMStage.WON, "won", 90, True),
    (CRMStage.LOST, "lost", 90, True),
    (CRMStage.NOT_A_FIT, "not_a_fit", 90, True),
)


@pytest.mark.parametrize(("stage", "canonical", "rank", "terminal"), STAGE_CASES)
def test_catalog_defines_every_canonical_stage(
    stage: CRMStage, canonical: str, rank: int, terminal: bool
) -> None:
    definition = STAGE_CATALOG[stage]

    assert stage.value == canonical
    assert definition == StageDefinition(rank=rank, terminal=terminal)
    assert stage_rank(stage) == rank
    assert is_terminal_stage(stage) is terminal
    assert resolve_stage(canonical) is stage


def test_catalog_is_immutable() -> None:
    assert isinstance(STAGE_CATALOG, MappingProxyType)
    with pytest.raises(TypeError):
        STAGE_CATALOG[CRMStage.NEW] = StageDefinition(rank=999, terminal=True)  # type: ignore[index]


def test_catalog_has_no_exposed_mutable_backing_dictionary() -> None:
    assert not hasattr(stage_policy, "_CATALOG")


def test_stage_definitions_are_frozen() -> None:
    with pytest.raises(FrozenInstanceError):
        STAGE_CATALOG[CRMStage.NEW].rank = 999  # type: ignore[misc]


@pytest.mark.parametrize(
    ("raw", "expected"),
    (
        ("Meeting Booked", CRMStage.MEETING_BOOKED),
        ("Proposal Sent", CRMStage.PROPOSAL_SENT),
        ("Won", CRMStage.WON),
        ("  meeting\t\n booked  ", CRMStage.MEETING_BOOKED),
        ("PROPOSAL\u00a0SENT", CRMStage.PROPOSAL_SENT),
        ("  wOn  ", CRMStage.WON),
        ("  MEETING_BOOKED  ", CRMStage.MEETING_BOOKED),
        ("NOT_A_FIT", CRMStage.NOT_A_FIT),
    ),
)
def test_resolution_normalizes_whitespace_and_casefold(
    raw: str, expected: CRMStage
) -> None:
    assert resolve_stage(raw) is expected


def test_normalize_stage_strips_collapses_whitespace_and_casefolds() -> None:
    assert normalize_stage("  MeEtInG\t\n Booked  ") == "meeting booked"


@pytest.mark.parametrize(
    "raw",
    (
        "",
        "   \t\n  ",
        "meeting",
        "meeting booked!",
        "meeting-booked",
        "meetingbooked",
        "Proposal Requested",
        "Lost Deal",
        "not a fit",
        "neww",
        None,
        40,
    ),
)
def test_unknown_empty_near_match_and_undocumented_aliases_require_explicit_handling(
    raw: object,
) -> None:
    with pytest.raises(UnknownStageError):
        resolve_stage(raw)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("stage", "expected"),
    (
        (CRMStage.NEW, False),
        (CRMStage.CONTACTED, False),
        (CRMStage.QUALIFIED, False),
        (CRMStage.MEETING_BOOKED, True),
        (CRMStage.MEETING_HELD, True),
        (CRMStage.PROPOSAL_REQUESTED, True),
        (CRMStage.PROPOSAL_SENT, True),
        (CRMStage.NEGOTIATION, True),
        (CRMStage.WON, True),
    ),
)
def test_unconditional_account_requirement_for_every_nonconditional_stage(
    stage: CRMStage, expected: bool
) -> None:
    assert requires_account(stage) is expected


@pytest.mark.parametrize("stage", (CRMStage.LOST, CRMStage.NOT_A_FIT))
@pytest.mark.parametrize(
    ("previous_highest_rank", "expected"),
    ((0, False), (39, False), (40, True), (80, True)),
)
def test_conditional_terminal_account_requirement_uses_original_previous_rank(
    stage: CRMStage, previous_highest_rank: int, expected: bool
) -> None:
    assert requires_account(stage, previous_highest_rank=previous_highest_rank) is expected


@pytest.mark.parametrize("stage", (CRMStage.LOST, CRMStage.NOT_A_FIT))
def test_conditional_terminal_without_original_evidence_requires_review(
    stage: CRMStage,
) -> None:
    with pytest.raises(AccountRequirementReviewRequired):
        requires_account(stage)


@pytest.mark.parametrize("stage", (CRMStage.LOST, CRMStage.NOT_A_FIT))
@pytest.mark.parametrize("persisted", (False, True))
def test_conditional_terminal_honors_persisted_original_decision(
    stage: CRMStage, persisted: bool
) -> None:
    assert requires_account(
        stage, persisted_terminal_requires_account=persisted
    ) is persisted


@pytest.mark.parametrize("stage", (CRMStage.LOST, CRMStage.NOT_A_FIT))
@pytest.mark.parametrize("persisted", (False, True))
def test_current_terminal_rank_is_not_original_history_evidence(
    stage: CRMStage, persisted: bool
) -> None:
    with pytest.raises(AccountRequirementReviewRequired):
        requires_account(stage, previous_highest_rank=90)

    assert requires_account(
        stage,
        previous_highest_rank=90,
        persisted_terminal_requires_account=persisted,
    ) is persisted


@pytest.mark.parametrize("stage", (CRMStage.LOST, CRMStage.NOT_A_FIT))
@pytest.mark.parametrize("persisted", (False, True))
def test_terminal_rank_remains_tainted_when_catalog_maximum_increases(
    monkeypatch: pytest.MonkeyPatch, stage: CRMStage, persisted: bool
) -> None:
    monkeypatch.setattr(stage_policy, "MAX_STAGE_RANK", 100)

    with pytest.raises(AccountRequirementReviewRequired):
        requires_account(stage, previous_highest_rank=90)

    assert requires_account(
        stage,
        previous_highest_rank=90,
        persisted_terminal_requires_account=persisted,
    ) is persisted


def test_highest_stage_rank_uses_simulated_catalog_maximum_only_as_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stage_policy, "MAX_STAGE_RANK", 100)

    assert highest_stage_rank(100, CRMStage.NEW) == 100
    with pytest.raises(InvalidHighestRankError):
        highest_stage_rank(101, CRMStage.NEW)


def test_won_always_requires_account_even_without_history() -> None:
    assert requires_account(CRMStage.WON) is True
    assert requires_account(CRMStage.WON, previous_highest_rank=0) is True


@pytest.mark.parametrize(
    "stage",
    (
        CRMStage.NEW,
        CRMStage.CONTACTED,
        CRMStage.MEETING_BOOKED,
        CRMStage.WON,
        CRMStage.LOST,
        CRMStage.NOT_A_FIT,
    ),
)
@pytest.mark.parametrize("invalid", (1, 0, "true", "false", object()))
def test_persisted_account_evidence_rejects_every_supplied_non_boolean(
    stage: CRMStage, invalid: object
) -> None:
    with pytest.raises(InvalidAccountEvidenceError):
        requires_account(
            stage,
            persisted_terminal_requires_account=invalid,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("stage", "expected"),
    (
        (CRMStage.NEW, False),
        (CRMStage.CONTACTED, False),
        (CRMStage.MEETING_BOOKED, True),
        (CRMStage.WON, True),
    ),
)
@pytest.mark.parametrize("persisted", (False, True))
def test_valid_persisted_evidence_is_type_checked_then_ignored_for_unconditional_stages(
    stage: CRMStage, expected: bool, persisted: bool
) -> None:
    assert (
        requires_account(stage, persisted_terminal_requires_account=persisted)
        is expected
    )


@pytest.mark.parametrize(
    ("previous", "incoming", "expected"),
    (
        (0, CRMStage.NEW, 10),
        (40, CRMStage.QUALIFIED, 40),
        (40, CRMStage.MEETING_HELD, 50),
        (80, CRMStage.LOST, 90),
        (90, CRMStage.NEW, 90),
    ),
)
def test_highest_stage_rank_is_monotonic(
    previous: int, incoming: CRMStage, expected: int
) -> None:
    assert highest_stage_rank(previous, incoming) == expected


@pytest.mark.parametrize("invalid", (True, False, -1, 1.5, "40", None))
def test_highest_stage_rank_rejects_invalid_previous_rank(invalid: object) -> None:
    with pytest.raises(InvalidHighestRankError):
        highest_stage_rank(invalid, CRMStage.NEW)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("current", "target"),
    (
        (CRMStage.WON, CRMStage.LOST),
        (CRMStage.WON, CRMStage.NOT_A_FIT),
        (CRMStage.LOST, CRMStage.WON),
        (CRMStage.LOST, CRMStage.NOT_A_FIT),
        (CRMStage.NOT_A_FIT, CRMStage.WON),
        (CRMStage.NOT_A_FIT, CRMStage.LOST),
    ),
)
def test_different_terminal_to_terminal_normal_transition_is_rejected(
    current: CRMStage, target: CRMStage
) -> None:
    with pytest.raises(InvalidTransitionError):
        validate_transition(current, target)


@pytest.mark.parametrize(
    "current", (CRMStage.WON, CRMStage.LOST, CRMStage.NOT_A_FIT)
)
@pytest.mark.parametrize(
    "target",
    (CRMStage.NEW, CRMStage.CONTACTED, CRMStage.MEETING_BOOKED, CRMStage.NEGOTIATION),
)
def test_terminal_to_nonterminal_transition_requires_reviewed_correction(
    current: CRMStage, target: CRMStage
) -> None:
    with pytest.raises(InvalidTransitionError):
        validate_transition(current, target, reviewed_correction=False)


@pytest.mark.parametrize("stage", (CRMStage.WON, CRMStage.LOST, CRMStage.NOT_A_FIT))
def test_same_terminal_transition_is_idempotent(stage: CRMStage) -> None:
    validate_transition(stage, stage)


@pytest.mark.parametrize(
    ("current", "target"),
    (
        (CRMStage.WON, CRMStage.LOST),
        (CRMStage.LOST, CRMStage.NOT_A_FIT),
        (CRMStage.NOT_A_FIT, CRMStage.WON),
    ),
)
def test_reviewed_terminal_correction_is_allowed(
    current: CRMStage, target: CRMStage
) -> None:
    validate_transition(current, target, reviewed_correction=True)


@pytest.mark.parametrize(
    ("current", "target"),
    (
        (CRMStage.WON, CRMStage.NEW),
        (CRMStage.LOST, CRMStage.MEETING_BOOKED),
        (CRMStage.NOT_A_FIT, CRMStage.NEGOTIATION),
    ),
)
def test_reviewed_terminal_to_nonterminal_correction_is_allowed(
    current: CRMStage, target: CRMStage
) -> None:
    validate_transition(current, target, reviewed_correction=True)


@pytest.mark.parametrize(
    "invalid",
    (1, 0, "true", "false", None, object()),
    ids=("one", "zero", "true-string", "false-string", "none", "object"),
)
def test_reviewed_correction_rejects_every_non_boolean(invalid: object) -> None:
    with pytest.raises(InvalidCorrectionFlagError):
        validate_transition(
            CRMStage.WON,
            CRMStage.NEW,
            reviewed_correction=invalid,  # type: ignore[arg-type]
        )


def test_normal_nonterminal_to_terminal_transition_is_allowed() -> None:
    validate_transition(CRMStage.NEW, CRMStage.WON, reviewed_correction=False)

    assert requires_account(CRMStage.WON) is True


def test_reviewed_correction_does_not_bypass_account_evidence_policy() -> None:
    validate_transition(CRMStage.WON, CRMStage.LOST, reviewed_correction=True)

    with pytest.raises(AccountRequirementReviewRequired):
        requires_account(CRMStage.LOST, previous_highest_rank=90)

    assert requires_account(CRMStage.WON) is True


@pytest.mark.parametrize(
    "near_match",
    (
        "meeting-booked",
        "meetingbooked",
        "meeting book",
        "proposal send",
        "winner",
        "loss",
        "not fit",
        "zzz won",
    ),
)
def test_resolution_has_no_fuzzy_or_lexical_fallback(near_match: str) -> None:
    with pytest.raises(UnknownStageError):
        resolve_stage(near_match)


def test_unknown_stage_diagnostic_does_not_echo_external_input() -> None:
    private_marker = "customer@example.com PRIVATE-MARKER-DO-NOT-LOG"

    with pytest.raises(UnknownStageError) as exc_info:
        resolve_stage(private_marker)

    assert str(exc_info.value) == "unknown CRM stage; review required"
    assert private_marker not in str(exc_info.value)


@pytest.mark.parametrize(
    "stage",
    (
        CRMStage.NEW,
        CRMStage.MEETING_BOOKED,
        CRMStage.WON,
        CRMStage.LOST,
        CRMStage.NOT_A_FIT,
    ),
)
@pytest.mark.parametrize("invalid", (True, "40", 40.0, -1, 91, 10**100))
@pytest.mark.parametrize("persisted", (False, True))
def test_requires_account_rejects_supplied_invalid_previous_rank_before_policy_returns(
    stage: CRMStage, invalid: object, persisted: bool
) -> None:
    with pytest.raises(InvalidHighestRankError):
        requires_account(
            stage,
            previous_highest_rank=invalid,  # type: ignore[arg-type]
            persisted_terminal_requires_account=persisted,
        )


@pytest.mark.parametrize("stage", (CRMStage.LOST, CRMStage.NOT_A_FIT))
@pytest.mark.parametrize(
    ("previous", "persisted"),
    ((0, False), (39, False), (40, True), (80, True)),
)
def test_conditional_dual_evidence_accepts_agreement(
    stage: CRMStage, previous: int, persisted: bool
) -> None:
    assert requires_account(
        stage,
        previous_highest_rank=previous,
        persisted_terminal_requires_account=persisted,
    ) is persisted


@pytest.mark.parametrize("stage", (CRMStage.LOST, CRMStage.NOT_A_FIT))
@pytest.mark.parametrize(
    ("previous", "persisted"),
    ((0, True), (39, True), (40, False), (80, False)),
)
def test_conditional_dual_evidence_rejects_conflict(
    stage: CRMStage, previous: int, persisted: bool
) -> None:
    with pytest.raises(ConflictingAccountEvidenceError):
        requires_account(
            stage,
            previous_highest_rank=previous,
            persisted_terminal_requires_account=persisted,
        )


def test_max_stage_rank_is_derived_from_catalog() -> None:
    assert MAX_STAGE_RANK == max(definition.rank for definition in STAGE_CATALOG.values())
    assert MAX_STAGE_RANK == 90


def test_terminal_stage_ranks_are_immutable_and_derived_from_catalog() -> None:
    expected = frozenset(
        definition.rank
        for definition in STAGE_CATALOG.values()
        if definition.terminal
    )

    assert isinstance(stage_policy.TERMINAL_STAGE_RANKS, frozenset)
    assert stage_policy.TERMINAL_STAGE_RANKS == expected == frozenset({90})


@pytest.mark.parametrize("valid", (0, 39, 40, 80, 90))
def test_highest_stage_rank_accepts_catalog_rank_boundaries(valid: int) -> None:
    assert highest_stage_rank(valid, CRMStage.NEW) == max(valid, 10)


@pytest.mark.parametrize("invalid", (91, 999, 10**100))
def test_highest_stage_rank_rejects_rank_above_catalog_maximum(invalid: int) -> None:
    with pytest.raises(InvalidHighestRankError):
        highest_stage_rank(invalid, CRMStage.NEW)
