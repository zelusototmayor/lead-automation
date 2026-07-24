from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from src.crm.persistence.models import Account
from src.crm.services.proposal_discovery_service import (
    DiscoverProposalCommand,
    ProposalDiscoveryService,
)


NOW = datetime(2026, 7, 16, 12, tzinfo=UTC)


class Repo:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def add(self, row):
        if row.id is None:
            row.id = uuid4()
        self.rows.append(row)
        return row

    def get(self, workspace_id, row_id, *, for_update=False):
        del for_update
        return next(
            (
                row
                for row in self.rows
                if row.id == row_id
                and getattr(row, "workspace_id", workspace_id) == workspace_id
            ),
            None,
        )


class EvidenceRepo(Repo):
    def by_source(self, workspace_id, source_identity_id, content_hash):
        return next(
            (
                row
                for row in self.rows
                if row.workspace_id == workspace_id
                and row.source_identity_id == source_identity_id
                and row.content_hash == content_hash
            ),
            None,
        )


class ProposalRepo(Repo):
    def by_thread(self, workspace_id, thread_source_identity_id, *, for_update=False):
        del for_update
        return next(
            (
                row
                for row in self.rows
                if row.workspace_id == workspace_id
                and row.thread_source_identity_id == thread_source_identity_id
            ),
            None,
        )


class VersionRepo(Repo):
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


class ReviewRepo(Repo):
    def open_by_key(self, workspace_id, dedupe_key):
        return next(
            (
                row
                for row in self.rows
                if row.workspace_id == workspace_id
                and row.dedupe_key == dedupe_key
                and row.state == "open"
            ),
            None,
        )


class Uow:
    def __init__(self, workspace_id, account_id):
        self.accounts = Repo(
            [
                Account(
                    id=account_id,
                    workspace_id=workspace_id,
                    display_name="Acme",
                    normalized_name="acme",
                )
            ]
        )
        self.evidence = EvidenceRepo()
        self.proposals = ProposalRepo()
        self.proposal_versions = VersionRepo()
        self.review_candidates = ReviewRepo()
        self.commits = 0

    def commit(self):
        self.commits += 1

    def lock_identities(self, workspace_id, fingerprints):
        del workspace_id, fingerprints


def command(workspace_id, account_id, thread_id, **changes):
    values = dict(
        workspace_id=workspace_id,
        account_id=account_id,
        message_source_identity_id=uuid4(),
        thread_source_identity_id=thread_id,
        occurred_at=NOW,
        direction="outbound",
        subject="Proposal for Acme",
        classification="sent_attachment",
        attachment_name="proposal-v1.pdf",
        attachment_content_hash="a" * 64,
        currency="EUR",
        one_off_amount=Decimal("1250.00"),
    )
    values.update(changes)
    return DiscoverProposalCommand(**values)


def test_promised_email_creates_review_action_not_sent_proposal():
    workspace_id, account_id, thread_id = uuid4(), uuid4(), uuid4()
    uow = Uow(workspace_id, account_id)

    outcome = ProposalDiscoveryService(uow).discover(
        command(
            workspace_id,
            account_id,
            thread_id,
            classification="promised",
            attachment_name=None,
            attachment_content_hash=None,
            currency=None,
            one_off_amount=None,
        )
    )

    assert outcome.action == "review"
    assert outcome.review_candidate.action_type == "send_promised_proposal"
    assert not uow.proposals.rows
    assert uow.commits == 0


def test_sent_attachment_creates_candidate_with_provenance_and_replay_is_idempotent():
    workspace_id, account_id, thread_id = uuid4(), uuid4(), uuid4()
    uow = Uow(workspace_id, account_id)
    service = ProposalDiscoveryService(uow)
    discovery = command(workspace_id, account_id, thread_id)

    first = service.discover(discovery)
    second = service.discover(discovery)

    assert first.action == "proposal_candidate"
    assert second.proposal.id == first.proposal.id
    assert len(uow.proposals.rows) == len(uow.proposal_versions.rows) == 1
    assert len(uow.evidence.rows) == 2
    assert first.proposal.status == "sent"
    assert first.proposal.sent_evidence_id is not None
    assert first.proposal.value_state == "candidate"
    assert first.version.one_off_amount == Decimal("1250.00")
    assert first.version.source_document_evidence_id is not None


def test_ambiguous_value_goes_to_review_and_preserves_null_amounts():
    workspace_id, account_id, thread_id = uuid4(), uuid4(), uuid4()
    uow = Uow(workspace_id, account_id)

    outcome = ProposalDiscoveryService(uow).discover(
        command(
            workspace_id,
            account_id,
            thread_id,
            value_ambiguous=True,
            one_off_amount=None,
        )
    )

    assert outcome.action == "review"
    assert outcome.proposal.value_state == "missing"
    assert outcome.version.one_off_amount is None
    assert outcome.review_candidate.action_type == "review_proposal_value"


def test_revision_in_same_thread_appends_version_but_followup_does_not():
    workspace_id, account_id, thread_id = uuid4(), uuid4(), uuid4()
    uow = Uow(workspace_id, account_id)
    service = ProposalDiscoveryService(uow)
    first = service.discover(command(workspace_id, account_id, thread_id))

    revised = service.discover(
        command(
            workspace_id,
            account_id,
            thread_id,
            message_source_identity_id=uuid4(),
            attachment_name="proposal-v2.pdf",
            attachment_content_hash="b" * 64,
            one_off_amount=Decimal("1500.00"),
        )
    )
    followup = service.discover(
        command(
            workspace_id,
            account_id,
            thread_id,
            message_source_identity_id=uuid4(),
            classification="followup",
            attachment_name=None,
            attachment_content_hash=None,
            currency=None,
            one_off_amount=None,
        )
    )

    assert revised.proposal.id == first.proposal.id
    assert [row.version_number for row in uow.proposal_versions.rows] == [1, 2]
    assert followup.action == "ignored_followup"
    assert len(uow.proposals.rows) == 1
    assert len(uow.proposal_versions.rows) == 2
