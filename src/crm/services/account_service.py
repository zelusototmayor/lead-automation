"""Exact, source-first account resolution and transactional stage application."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import re
import unicodedata
from typing import Any, Callable
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from src.crm.domain.stage_policy import (
    AccountRequirementReviewRequired,
    highest_stage_rank,
    requires_account,
    resolve_stage,
    stage_rank,
    validate_transition,
)


class IdentityReviewRequired(RuntimeError):
    """Exact identity evidence is absent, invalid, or conflicting."""


class ReplayConflictError(RuntimeError):
    """An ingest event was already applied to different semantics."""


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _safe_text(value: object, *, maximum: int, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if type(value) is not str:
        raise IdentityReviewRequired("identity requires review") from None
    normalized = unicodedata.normalize("NFKC", value).strip()
    if not normalized or len(normalized) > maximum or _CONTROL.search(normalized):
        raise IdentityReviewRequired("identity requires review") from None
    if any(unicodedata.category(char) in {"Cc", "Cf"} for char in normalized):
        raise IdentityReviewRequired("identity requires review") from None
    return normalized


def _text(value: object, kind: str, *, email: bool = False) -> str:
    del kind, email
    try:
        result = _safe_text(value, maximum=512, required=True)
    except IdentityReviewRequired:
        raise ValueError("invalid identity; review required") from None
    assert result is not None
    return result


def normalize_company_name(value: str) -> str:
    if type(value) is not str:
        raise ValueError("invalid company identity; review required") from None
    raw = unicodedata.normalize("NFKC", value)
    if any(unicodedata.category(char) in {"Cc", "Cf"} and char != "\t" for char in raw):
        raise ValueError("invalid company identity; review required") from None
    normalized = " ".join(raw.split()).casefold()
    if not normalized or len(normalized) > 512:
        raise ValueError("invalid company identity; review required") from None
    return normalized


def normalize_email(value: str) -> str:
    normalized = _text(value, "email identity", email=True).casefold()
    if (
        len(normalized) > 320
        or normalized.count("@") != 1
        or any(char.isspace() for char in normalized)
    ):
        raise ValueError("invalid email identity; review required") from None
    local, domain = normalized.rsplit("@", 1)
    if not local or len(local) > 64 or not domain:
        raise ValueError("invalid email identity; review required") from None
    return f"{local}@{normalize_domain(domain)}"


def normalize_domain(value: str) -> str:
    raw = _text(value, "domain identity").rstrip(".").casefold()
    if not raw or ".." in raw or any(char.isspace() for char in raw):
        raise ValueError("invalid domain identity; review required") from None
    try:
        labels = [label.encode("idna").decode("ascii") for label in raw.split(".")]
    except (UnicodeError, ValueError):
        raise ValueError("invalid domain identity; review required") from None
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or not re.fullmatch(r"[a-z0-9-]+", label)
        for label in labels
    ):
        raise ValueError("invalid domain identity; review required") from None
    normalized = ".".join(labels)
    if len(normalized) > 253:
        raise ValueError("invalid domain identity; review required") from None
    return normalized


@dataclass(frozen=True, slots=True)
class IdentityHints:
    account_id: UUID | None = None
    source_identity_id: UUID | None = None
    contact_email: str | None = None
    contact_name: str | None = None
    company_name: str | None = None
    display_name: str | None = None
    legal_name: str | None = None
    domain: str | None = None
    sector: str | None = None
    vertical: str | None = None
    source_origin: str | None = None


@dataclass(frozen=True, slots=True)
class StageTransitionCommand:
    workspace_id: UUID
    target_stage: str
    identity: IdentityHints
    occurred_at: datetime
    lead_id: UUID | None = None
    ingest_event_id: UUID | None = None
    reviewed_correction: bool = False
    persisted_terminal_requires_account: bool | None = None
    previous_history_known: bool = True
    commercial_classification: str = "confirmed"


@dataclass(frozen=True, slots=True)
class StageTransitionResult:
    status: str
    lead_id: UUID | None
    account_id: UUID | None
    stage: str
    highest_stage_rank: int


_LIFECYCLE_ORDER = {
    "potential": 0,
    "meeting": 1,
    "proposal": 2,
    "customer": 3,
    "lost": 4,
    "inactive": 5,
}


def _lifecycle(stage: str) -> str:
    rank = stage_rank(stage)
    if stage == "won":
        return "customer"
    if rank >= 60:
        return "proposal"
    if rank >= 40:
        return "meeting"
    return "potential"


def _normalized_hints(hints: IdentityHints) -> IdentityHints:
    if type(hints) is not IdentityHints:
        raise IdentityReviewRequired("identity requires review") from None
    if hints.account_id is not None and type(hints.account_id) is not UUID:
        raise IdentityReviewRequired("identity requires review") from None
    if (
        hints.source_identity_id is not None
        and type(hints.source_identity_id) is not UUID
    ):
        raise IdentityReviewRequired("identity requires review") from None
    try:
        return IdentityHints(
            account_id=hints.account_id,
            source_identity_id=hints.source_identity_id,
            contact_email=normalize_email(hints.contact_email)
            if hints.contact_email is not None
            else None,
            contact_name=_safe_text(hints.contact_name, maximum=512),
            company_name=_safe_text(hints.company_name, maximum=512),
            display_name=_safe_text(hints.display_name, maximum=512),
            legal_name=_safe_text(hints.legal_name, maximum=512),
            domain=normalize_domain(hints.domain) if hints.domain is not None else None,
            sector=_safe_text(hints.sector, maximum=255),
            vertical=_safe_text(hints.vertical, maximum=255),
            source_origin=_safe_text(hints.source_origin, maximum=255),
        )
    except (ValueError, UnicodeError):
        raise IdentityReviewRequired("identity requires review") from None


def _identity_fingerprints(hints: IdentityHints) -> tuple[str, ...]:
    values: set[str] = set()
    if hints.account_id:
        values.add(f"account:{hints.account_id}")
    if hints.source_identity_id:
        values.add(f"source:{hints.source_identity_id}")
    if hints.contact_email:
        values.add(f"email:{hints.contact_email}")
    company = hints.company_name or hints.display_name or hints.legal_name
    if hints.domain and company:
        values.add(f"domain-name:{hints.domain}:{normalize_company_name(company)}")
    return tuple(sorted(values))


def _transition_semantic_fingerprint(
    command: StageTransitionCommand,
    target: str,
    hints: IdentityHints,
    occurred_at: datetime,
) -> str:
    def normalized_company(value: str | None) -> str | None:
        return normalize_company_name(value) if value is not None else None

    semantics = {
        "target": target,
        "lead_id": str(command.lead_id) if command.lead_id is not None else None,
        "occurred_at": occurred_at.isoformat(timespec="microseconds"),
        "commercial_classification": command.commercial_classification,
        "reviewed_correction": command.reviewed_correction,
        "previous_history_known": command.previous_history_known,
        "persisted_terminal_requires_account": command.persisted_terminal_requires_account,
        "identity": {
            "account_id": str(hints.account_id)
            if hints.account_id is not None
            else None,
            "source_identity_id": str(hints.source_identity_id)
            if hints.source_identity_id is not None
            else None,
            "contact_email": hints.contact_email,
            "domain": hints.domain,
            "company_name": normalized_company(hints.company_name),
            "display_name": normalized_company(hints.display_name),
            "legal_name": normalized_company(hints.legal_name),
            "contact_name": hints.contact_name,
            "sector": hints.sector,
            "vertical": hints.vertical,
            "source_origin": hints.source_origin,
        },
    }
    canonical = json.dumps(
        semantics, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_command(
    command: StageTransitionCommand,
) -> tuple[str, IdentityHints, datetime]:
    if type(command) is not StageTransitionCommand:
        raise IdentityReviewRequired("transition requires review") from None
    if type(command.workspace_id) is not UUID:
        raise IdentityReviewRequired("transition requires review") from None
    if command.lead_id is not None and type(command.lead_id) is not UUID:
        raise IdentityReviewRequired("transition requires review") from None
    if (
        command.ingest_event_id is not None
        and type(command.ingest_event_id) is not UUID
    ):
        raise IdentityReviewRequired("transition requires review") from None
    if (
        type(command.occurred_at) is not datetime
        or command.occurred_at.tzinfo is None
        or command.occurred_at.utcoffset() is None
    ):
        raise IdentityReviewRequired("transition requires review") from None
    if (
        type(command.reviewed_correction) is not bool
        or type(command.previous_history_known) is not bool
    ):
        raise IdentityReviewRequired("transition requires review") from None
    if (
        command.persisted_terminal_requires_account is not None
        and type(command.persisted_terminal_requires_account) is not bool
    ):
        raise IdentityReviewRequired("transition requires review") from None
    if command.commercial_classification not in {"confirmed", "excluded", "review"}:
        raise IdentityReviewRequired("classification requires review") from None
    try:
        target_text = _safe_text(command.target_stage, maximum=64, required=True)
        assert target_text is not None
        target = resolve_stage(target_text).value
    except (ValueError, TypeError, IdentityReviewRequired):
        raise IdentityReviewRequired("transition requires review") from None
    return (
        target,
        _normalized_hints(command.identity),
        command.occurred_at.astimezone(UTC),
    )


class AccountService:
    def __init__(self, uow_factory: Callable[[], Any]):
        self.uow_factory = uow_factory

    def apply_stage_transition(
        self, command: StageTransitionCommand
    ) -> StageTransitionResult:
        target, hints, occurred_at = _validate_command(command)
        semantic_fingerprint = _transition_semantic_fingerprint(
            command, target, hints, occurred_at
        )
        identity_fingerprints = _identity_fingerprints(hints)
        if command.ingest_event_id is None:
            if command.lead_id is None and not requires_account(target, 0, None):
                raise IdentityReviewRequired(
                    "source-first transition requires review"
                ) from None
            if (
                command.lead_id is None
                and command.commercial_classification != "confirmed"
            ):
                if command.commercial_classification == "excluded":
                    return StageTransitionResult("excluded", None, None, target, 0)
                raise IdentityReviewRequired("classification requires review") from None
        try:
            with self.uow_factory() as uow:
                if command.ingest_event_id is not None:
                    if not uow.claim_stage_reduction(
                        command.workspace_id,
                        command.ingest_event_id,
                        semantic_fingerprint,
                    ):
                        raise IdentityReviewRequired(
                            "ingest event requires review"
                        ) from None
                    replay = uow.replay(command.workspace_id, command.ingest_event_id)
                    if replay is not None:
                        activity, replay_lead = replay
                        if (
                            activity.summary != target
                            or activity.semantic_fingerprint != semantic_fingerprint
                            or replay_lead is None
                            or (
                                command.lead_id is not None
                                and replay_lead.id != command.lead_id
                            )
                        ):
                            raise ReplayConflictError(
                                "ingest event already records different semantics"
                            ) from None
                        return StageTransitionResult(
                            "applied",
                            replay_lead.id,
                            activity.account_id,
                            target,
                            replay_lead.highest_stage_rank,
                        )

                if command.lead_id is None and not requires_account(target, 0, None):
                    raise IdentityReviewRequired(
                        "source-first transition requires review"
                    ) from None
                if (
                    command.lead_id is None
                    and command.commercial_classification != "confirmed"
                ):
                    if command.commercial_classification == "excluded":
                        uow.commit()
                        return StageTransitionResult("excluded", None, None, target, 0)
                    raise IdentityReviewRequired(
                        "classification requires review"
                    ) from None

                if identity_fingerprints:
                    uow.lock_identities(command.workspace_id, identity_fingerprints)
                lead = (
                    uow.leads.get(
                        command.workspace_id, command.lead_id, for_update=True
                    )
                    if command.lead_id
                    else None
                )
                if command.lead_id and lead is None:
                    raise IdentityReviewRequired(
                        "lead identity requires review"
                    ) from None
                from_stage = lead.stage if lead is not None else "new"
                if from_stage == target:
                    raise IdentityReviewRequired("transition requires review") from None
                if lead is not None:
                    validate_transition(lead.stage, target, command.reviewed_correction)
                    previous = (
                        lead.highest_stage_rank
                        if command.previous_history_known
                        else None
                    )
                else:
                    previous = 0 if command.previous_history_known else None
                try:
                    account_needed = requires_account(
                        target, previous, command.persisted_terminal_requires_account
                    )
                except AccountRequirementReviewRequired:
                    raise IdentityReviewRequired(
                        "account policy requires review"
                    ) from None
                original_rank = lead.highest_stage_rank if lead is not None else 0
                account = (
                    uow.accounts.get(
                        command.workspace_id, lead.account_id, for_update=True
                    )
                    if lead is not None and lead.account_id
                    else None
                )
                if account_needed or account is not None:
                    candidates = {
                        candidate.id: candidate
                        for candidate in uow.account_candidates(
                            command.workspace_id, hints
                        )
                    }
                    if account is not None:
                        if any(
                            candidate_id != account.id for candidate_id in candidates
                        ):
                            raise IdentityReviewRequired(
                                "identity requires review"
                            ) from None
                    elif len(candidates) > 1:
                        raise IdentityReviewRequired(
                            "identity requires review"
                        ) from None
                    elif candidates:
                        selected_id = next(iter(candidates))
                        account = uow.accounts.get(
                            command.workspace_id, selected_id, for_update=True
                        )
                        if account is None:
                            raise IdentityReviewRequired(
                                "identity requires review"
                            ) from None
                    elif account_needed:
                        company = (
                            hints.company_name or hints.display_name or hints.legal_name
                        )
                        stable = (
                            hints.source_identity_id
                            or hints.contact_email
                            or hints.domain
                        )
                        if not company or not stable:
                            raise IdentityReviewRequired(
                                "identity requires review"
                            ) from None
                        account = uow.new_account(command.workspace_id, hints)

                if lead is None:
                    lead = uow.new_lead(command.workspace_id, hints)
                if account is not None:
                    lead.account_id = account.id
                    if hints.contact_email:
                        contact = uow.new_contact(
                            command.workspace_id, account.id, hints
                        )
                        if lead.contact_id not in (None, contact.id):
                            raise IdentityReviewRequired(
                                "identity requires review"
                            ) from None
                        lead.contact_id = contact.id
                    uow.link_source_identity(
                        command.workspace_id, hints.source_identity_id, account.id
                    )

                lead.stage = target
                lead.highest_stage_rank = highest_stage_rank(original_rank, target)
                if account is not None:
                    account.highest_stage_rank = max(
                        account.highest_stage_rank, lead.highest_stage_rank
                    )
                    desired = _lifecycle(target)
                    if _LIFECYCLE_ORDER[desired] > _LIFECYCLE_ORDER[
                        account.lifecycle_stage
                    ] and desired not in {"lost", "inactive"}:
                        account.lifecycle_stage = desired
                uow.new_activity(
                    workspace_id=command.workspace_id,
                    account_id=account.id if account is not None else None,
                    lead_id=lead.id,
                    contact_id=lead.contact_id,
                    activity_type="stage_change",
                    occurred_at=occurred_at,
                    title=f"Stage changed to {target}",
                    summary=target,
                    ingest_event_id=command.ingest_event_id,
                    semantic_fingerprint=semantic_fingerprint,
                    from_stage=from_stage,
                    to_stage=target,
                )
                uow.commit()
                return StageTransitionResult(
                    "applied",
                    lead.id,
                    account.id if account else None,
                    target,
                    lead.highest_stage_rank,
                )
        except (IdentityReviewRequired, ReplayConflictError):
            raise
        except IntegrityError:
            raise IdentityReviewRequired("identity or replay requires review") from None
