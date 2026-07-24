"""Deterministic, evidence-backed CRM recommendations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Iterable
from uuid import UUID

from sqlalchemy import and_, func, select

from src.crm.persistence.models import (
    RECOMMENDATION_RULE_CODES,
    Activity,
    Evidence,
    Lead,
    Proposal,
    ProposalVersion,
    Recommendation,
    ReviewCandidate,
)


class IntelligenceUnavailable(RuntimeError):
    """Recommendation inputs cannot be evaluated safely."""


_PRIORITY = {
    "contradictory_value_status_sources": "critical",
    "promised_proposal_not_sent": "high",
    "inbound_awaiting_response": "high",
    "proposal_stale": "high",
    "held_meeting_without_notes": "medium",
    "meeting_without_calendar_event": "medium",
    "proposal_missing_next_action": "medium",
    "matching_review_candidate": "medium",
    "value_review_candidate": "medium",
}
_PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_OPEN_PROPOSAL_STATES = ("draft", "promised", "sent", "viewed", "negotiation")


@dataclass(frozen=True, slots=True)
class IntelligenceFact:
    rule_code: str
    account_id: UUID
    evidence: tuple[str, ...]
    observed_at: datetime
    proposal_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class RecommendationCandidate:
    rule_code: str
    priority: str
    account_id: UUID
    proposal_id: UUID | None
    evidence: tuple[str, ...]
    state: str
    observed_at: datetime
    dedupe_key: str


class RecommendationService:
    """Evaluate canonical facts and persist them in a caller-owned transaction."""

    def __init__(self, uow):
        self.uow = uow

    @staticmethod
    def evaluate_facts(
        facts: Iterable[IntelligenceFact], *, now: datetime
    ) -> tuple[RecommendationCandidate, ...]:
        if not _aware(now):
            raise IntelligenceUnavailable("intelligence unavailable")
        candidates: dict[str, RecommendationCandidate] = {}
        for fact in facts:
            if (
                type(fact) is not IntelligenceFact
                or fact.rule_code not in RECOMMENDATION_RULE_CODES
                or type(fact.account_id) is not UUID
                or not _aware(fact.observed_at)
                or not fact.evidence
                or any(not _safe_reference(value) for value in fact.evidence)
            ):
                continue
            entity = fact.proposal_id or fact.account_id
            key = f"{fact.rule_code}:{entity}"
            candidate = RecommendationCandidate(
                rule_code=fact.rule_code,
                priority=_PRIORITY[fact.rule_code],
                account_id=fact.account_id,
                proposal_id=fact.proposal_id,
                evidence=tuple(sorted(set(fact.evidence))),
                state="open",
                observed_at=fact.observed_at,
                dedupe_key=key,
            )
            candidates[key] = candidate
        return tuple(
            sorted(
                candidates.values(),
                key=lambda item: (
                    _PRIORITY_ORDER[item.priority],
                    item.rule_code,
                    str(item.proposal_id or item.account_id),
                ),
            )
        )

    def refresh(
        self, workspace_id: UUID, *, now: datetime | None = None
    ) -> tuple[Recommendation, ...]:
        if type(workspace_id) is not UUID:
            raise IntelligenceUnavailable("intelligence unavailable")
        now = now or datetime.now(UTC)
        if not _aware(now) or self.uow.session is None:
            raise IntelligenceUnavailable("intelligence unavailable")
        self.uow.lock_identities(workspace_id, ("recommendations",))
        candidates = self.evaluate_facts(self._collect(workspace_id, now), now=now)
        session = self.uow.session
        existing = {
            row.dedupe_key: row
            for row in session.scalars(
                select(Recommendation).where(
                    Recommendation.workspace_id == workspace_id,
                    Recommendation.state == "open",
                )
            )
        }
        active = {candidate.dedupe_key for candidate in candidates}
        for key, row in existing.items():
            if key not in active:
                row.state = "resolved"
                row.resolved_at = now
        rows = []
        for candidate in candidates:
            row = existing.get(candidate.dedupe_key)
            if row is None:
                row = Recommendation(
                    workspace_id=workspace_id,
                    account_id=candidate.account_id,
                    proposal_id=candidate.proposal_id,
                    rule_code=candidate.rule_code,
                    priority=candidate.priority,
                    evidence_json=list(candidate.evidence),
                    state="open",
                    dedupe_key=candidate.dedupe_key,
                    observed_at=candidate.observed_at,
                )
                session.add(row)
            else:
                row.priority = candidate.priority
                row.evidence_json = list(candidate.evidence)
                row.observed_at = candidate.observed_at
            rows.append(row)
        session.flush()
        return tuple(rows)

    def _collect(self, workspace_id: UUID, now: datetime) -> list[IntelligenceFact]:
        session = self.uow.session
        assert session is not None
        facts: list[IntelligenceFact] = []
        proposals = session.scalars(
            select(Proposal).where(
                Proposal.workspace_id == workspace_id,
                Proposal.status.in_(_OPEN_PROPOSAL_STATES),
            )
        ).all()
        for proposal in proposals:
            reference = (f"proposal:{proposal.id}",)
            if proposal.next_action is None:
                facts.append(
                    IntelligenceFact(
                        "proposal_missing_next_action",
                        proposal.account_id,
                        reference,
                        proposal.updated_at,
                        proposal.id,
                    )
                )
            if proposal.sent_at is not None and proposal.sent_at <= now - timedelta(
                days=14
            ):
                facts.append(
                    IntelligenceFact(
                        "proposal_stale",
                        proposal.account_id,
                        reference
                        + (
                            f"sent-evidence:{proposal.sent_evidence_id}"
                            if proposal.sent_evidence_id
                            else "sent-state:legacy-unverified",
                        ),
                        proposal.sent_at,
                        proposal.id,
                    )
                )

        meetings = session.scalars(
            select(Activity).where(
                Activity.workspace_id == workspace_id,
                Activity.activity_type == "meeting",
                Activity.account_id.is_not(None),
                Activity.occurred_at <= now,
            )
        ).all()
        for meeting in meetings:
            has_notes = session.scalar(
                select(func.count(Evidence.id)).where(
                    Evidence.workspace_id == workspace_id,
                    Evidence.account_id == meeting.account_id,
                    Evidence.evidence_type == "meeting_note",
                    Evidence.captured_at >= meeting.occurred_at - timedelta(hours=12),
                    Evidence.captured_at <= meeting.occurred_at + timedelta(days=1),
                )
            )
            has_calendar = session.scalar(
                select(func.count(Evidence.id)).where(
                    Evidence.workspace_id == workspace_id,
                    Evidence.account_id == meeting.account_id,
                    Evidence.evidence_type == "calendar_event",
                    Evidence.captured_at >= meeting.occurred_at - timedelta(days=1),
                    Evidence.captured_at <= meeting.occurred_at + timedelta(days=1),
                )
            )
            ref = (f"activity:{meeting.id}",)
            if not has_notes:
                facts.append(
                    IntelligenceFact(
                        "held_meeting_without_notes",
                        meeting.account_id,
                        ref,
                        meeting.occurred_at,
                    )
                )
            if not has_calendar:
                facts.append(
                    IntelligenceFact(
                        "meeting_without_calendar_event",
                        meeting.account_id,
                        ref,
                        meeting.occurred_at,
                    )
                )

        latest_email = session.execute(
            select(
                Activity.account_id,
                func.max(Activity.occurred_at)
                .filter(Activity.direction == "inbound")
                .label("inbound_at"),
                func.max(Activity.occurred_at)
                .filter(Activity.direction == "outbound")
                .label("outbound_at"),
            )
            .where(
                Activity.workspace_id == workspace_id,
                Activity.activity_type.in_(("email_received", "email_sent")),
                Activity.account_id.is_not(None),
            )
            .group_by(Activity.account_id)
        ).all()
        for row in latest_email:
            if row.inbound_at is not None and (
                row.outbound_at is None or row.inbound_at > row.outbound_at
            ):
                facts.append(
                    IntelligenceFact(
                        "inbound_awaiting_response",
                        row.account_id,
                        (f"account-email-latest:{row.account_id}",),
                        row.inbound_at,
                    )
                )

        reviews = session.scalars(
            select(ReviewCandidate).where(
                ReviewCandidate.workspace_id == workspace_id,
                ReviewCandidate.state == "open",
            )
        ).all()
        for review in reviews:
            if review.action_type == "send_promised_proposal":
                code = "promised_proposal_not_sent"
            elif review.action_type == "review_proposal_value":
                code = "value_review_candidate"
            else:
                code = "matching_review_candidate"
            facts.append(
                IntelligenceFact(
                    code,
                    review.account_id,
                    (f"evidence:{review.evidence_id}", f"review:{review.id}"),
                    review.created_at,
                    review.proposal_id,
                )
            )

        contradictory = session.execute(
            select(Proposal, ProposalVersion)
            .join(ProposalVersion, ProposalVersion.id == Proposal.selected_version_id)
            .where(
                Proposal.workspace_id == workspace_id,
                Proposal.value_state != "confirmed",
                ProposalVersion.confirmed_at.is_not(None),
            )
        ).all()
        for proposal, version in contradictory:
            facts.append(
                IntelligenceFact(
                    "contradictory_value_status_sources",
                    proposal.account_id,
                    (f"proposal:{proposal.id}", f"proposal-version:{version.id}"),
                    proposal.updated_at,
                    proposal.id,
                )
            )
        status_conflicts = session.execute(
            select(Proposal, Lead)
            .join(
                Lead,
                and_(
                    Lead.workspace_id == Proposal.workspace_id,
                    Lead.id == Proposal.lead_id,
                ),
            )
            .where(
                Proposal.workspace_id == workspace_id,
                Proposal.status.in_(("draft", "promised")),
                Lead.stage.in_(("proposal_sent", "negotiation", "won", "lost")),
            )
        ).all()
        for proposal, lead in status_conflicts:
            facts.append(
                IntelligenceFact(
                    "contradictory_value_status_sources",
                    proposal.account_id,
                    (f"proposal:{proposal.id}", f"lead:{lead.id}"),
                    proposal.updated_at,
                    proposal.id,
                )
            )
        return facts


def _aware(value: object) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )


def _safe_reference(value: object) -> bool:
    return (
        type(value) is str
        and 0 < len(value) <= 160
        and all(char.isalnum() or char in ":-_" for char in value)
    )
