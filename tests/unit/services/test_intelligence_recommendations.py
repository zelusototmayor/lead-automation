from datetime import UTC, datetime, timedelta
from uuid import uuid4

from src.crm.services.intelligence_service import (
    IntelligenceFact,
    RecommendationService,
)


def test_supported_rules_are_deterministic_and_evidence_backed():
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    account_id = uuid4()
    proposal_id = uuid4()
    facts = [
        IntelligenceFact(
            rule_code="proposal_missing_next_action",
            account_id=account_id,
            proposal_id=proposal_id,
            evidence=("proposal:current-state",),
            observed_at=now - timedelta(days=1),
        ),
        IntelligenceFact(
            rule_code="proposal_stale",
            account_id=account_id,
            proposal_id=proposal_id,
            evidence=("proposal:verified-sent-at",),
            observed_at=now - timedelta(days=15),
        ),
    ]

    first = RecommendationService.evaluate_facts(facts, now=now)
    second = RecommendationService.evaluate_facts(reversed(facts), now=now)

    assert first == second
    assert [item.rule_code for item in first] == [
        "proposal_stale",
        "proposal_missing_next_action",
    ]
    assert all(item.priority in {"critical", "high", "medium", "low"} for item in first)
    assert all(item.evidence and item.state == "open" for item in first)


def test_unsupported_or_unevidenced_facts_never_become_recommendations():
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    account_id = uuid4()

    assert (
        RecommendationService.evaluate_facts(
            [
                IntelligenceFact(
                    rule_code="invented_rule",
                    account_id=account_id,
                    evidence=("activity:any",),
                    observed_at=now,
                ),
                IntelligenceFact(
                    rule_code="inbound_awaiting_response",
                    account_id=account_id,
                    evidence=(),
                    observed_at=now,
                ),
            ],
            now=now,
        )
        == ()
    )
