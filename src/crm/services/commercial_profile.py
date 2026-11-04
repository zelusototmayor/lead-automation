"""Reviewed commercial evidence, separate from stages and call evidence."""
from datetime import UTC, datetime
from dataclasses import dataclass
import json
from typing import Literal
from uuid import UUID, uuid5
from urllib.parse import urlsplit

from decimal import Decimal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from src.crm.persistence.models import Account, AuditEvent, Lead
from src.crm.ingestion.outbox import enqueue_outbox_event
from src.crm.services.command_service import CommandConflictError
from src.crm.services.lead_operation_service import LeadOperationService, LeadOperationResult, _semantic_hash

ACTION = 'lead.commercial_profile_updated'


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Evidence(Strict):
    source_url: str = Field(min_length=1, max_length=256)
    source_sha256: str = Field(pattern='^[0-9a-f]{64}$')
    review_ref: str = Field(min_length=1, max_length=128)

    @field_validator('source_url')
    @classmethod
    def public_reference(cls, value):
        url = urlsplit(value)
        if url.scheme != 'https' or not url.hostname or url.username or url.password:
            raise ValueError('https evidence reference required')
        return value  # Reference only: never fetch, execute or certify a URL.


class FitEvidence(Evidence):
    status: Literal['unknown', 'under_review', 'fit', 'no_fit']
    reason: str = Field(min_length=1, max_length=256)


class FinancialEvidence(Evidence):
    amount: str = Field(pattern=r'^-?(0|[1-9][0-9]{0,14})(\.[0-9]{1,6})?$')
    currency: Literal['EUR', 'USD', 'GBP']
    unit: Literal['units', 'thousands', 'millions']
    year: int = Field(ge=1900, le=2100, strict=True)
    entity_account_id: UUID
    reporting_entity: str = Field(min_length=1, max_length=256)
    reporting_basis: Literal['standalone']
    source_excerpt: str = Field(min_length=1, max_length=600)

    @field_validator('year')
    @classmethod
    def not_future(cls, value):
        if value > datetime.now(UTC).year:
            raise ValueError('historical financial year required')
        return value


class ContactSourceRef(Strict):
    activity_id: UUID
    source_digest: str = Field(pattern='^[0-9a-f]{64}$')


class NextContactEvidence(Strict):
    channel: Literal['call', 'email', 'none', 'defer', 'review']
    category: Literal['phone_allowed', 'email_next', 'email_only', 'closed', 'await_client', 'future_callback', 'contact_validation']
    reason: str = Field(min_length=1, max_length=600)
    review_ref: str = Field(min_length=1, max_length=128)
    context_sha256: str = Field(pattern='^[0-9a-f]{64}$')
    tasks_sha256: str = Field(pattern='^[0-9a-f]{64}$')
    source_refs: list[ContactSourceRef] = Field(min_length=1, max_length=8)
    not_before: datetime | None = None
    retry_same_day: bool = False

    @model_validator(mode='after')
    def coherent_route(self):
        expected = {'phone_allowed':'call', 'email_next':'email', 'email_only':'email',
                    'closed':'none', 'await_client':'none', 'future_callback':'call', 'contact_validation':'review'}
        if expected[self.category] != self.channel:
            raise ValueError('channel category mismatch')
        if self.not_before is not None and self.not_before.utcoffset() is None:
            raise ValueError('timezone required')
        if self.category == 'future_callback' and self.not_before is None:
            raise ValueError('future callback needs exact reviewed not-before')
        if self.retry_same_day and self.channel != 'call':
            raise ValueError('retry requires explicit reviewed phone action')
        return self


class ProfileBody(Strict):
    command_id: UUID
    expected_version: int = Field(ge=1, strict=True)
    field: Literal['fit', 'revenue', 'ebitda', 'next_contact']
    value: FitEvidence | FinancialEvidence | NextContactEvidence

    @model_validator(mode='after')
    def field_matches_value(self):
        if ({'fit':FitEvidence, 'revenue':FinancialEvidence, 'ebitda':FinancialEvidence,
             'next_contact':NextContactEvidence}[self.field] is not type(self.value)):
            raise ValueError('field evidence mismatch')
        if self.field == 'revenue' and Decimal(self.value.amount) < 0:
            raise ValueError('negative revenue unsupported')
        return self


class RevertBody(Strict):
    command_id: UUID
    expected_version: int = Field(ge=1, strict=True)
    revert_command_id: UUID


@dataclass(frozen=True)
class ProfileCommand:
    command_id: UUID
    workspace_id: UUID
    lead_id: UUID
    expected_version: int


def _records(session, workspace_id, lead_id):
    return list(session.scalars(select(AuditEvent).where(
        AuditEvent.workspace_id == workspace_id, AuditEvent.entity_id == lead_id,
        AuditEvent.entity_type == 'lead', AuditEvent.action == ACTION)))


def _fields(records):
    result = {}
    for row in sorted(records, key=lambda r: r.details['version']):
        data = row.details
        result[data['field']] = {'value': data['after'], 'command_id': str(row.command_id),
            'version': data['version'], 'account_id': data['account_id']}
    return result


def _project_profile(session, workspace_id, lead, account, records):
    lead_id = lead.id
    fields = _fields(records)
    account_id = str(lead.account_id) if lead.account_id else None
    def entity_current(key, value):
        if value['account_id'] != account_id:
            return False
        if key == 'next_contact' and value['value'] is not None:
            from src.crm.services.contact_channel import validate_binding
            return validate_binding(session, workspace_id, lead, value['value'])
        financial = value['value'] if key in ('revenue', 'ebitda') else None
        return financial is None or (account is not None and
            ' '.join(financial['reporting_entity'].casefold().split()) == ' '.join(account.display_name.casefold().split()))
    projected = {key: dict(value, current_entity=entity_current(key, value))
                 for key, value in fields.items()}
    unknown = [key for key in ('fit', 'revenue', 'ebitda') if key not in projected
               or not projected[key]['current_entity'] or projected[key]['value'] is None
               or (key == 'fit' and projected[key]['value']['status'] in ('unknown', 'under_review'))]
    return {'lead_id': str(lead_id), 'version': lead.version, 'fields': projected,
            'unknown_fields': unknown,
            'source_authentication': 'caller_reviewed_not_automatically_verified'}


def read_profile(session, workspace_id, lead_id):
    lead = session.scalar(select(Lead).where(Lead.workspace_id == workspace_id, Lead.id == lead_id))
    if lead is None:
        raise CommandConflictError('profile unavailable')
    account = session.scalar(select(Account).where(Account.workspace_id == workspace_id,
                                                   Account.id == lead.account_id))
    return _project_profile(session, workspace_id, lead, account, _records(session, workspace_id, lead_id))


def read_profiles(session, workspace_id, leads):
    """One-call snapshot, no cross-request cache; canonical binding is unchanged."""
    from collections import defaultdict
    records = defaultdict(list)
    for row in session.scalars(select(AuditEvent).where(
            AuditEvent.workspace_id == workspace_id,
            AuditEvent.entity_type == 'lead', AuditEvent.action == ACTION)):
        records[row.entity_id].append(row)
    accounts = {a.id: a for a in session.scalars(select(Account).where(Account.workspace_id == workspace_id))}
    return {lead.id: _project_profile(session, workspace_id, lead, accounts.get(lead.account_id), records[lead.id])
            for lead in leads}


class CommercialProfileService(LeadOperationService):
    """Canonical UoW/replay/version locks; no Activity, stage or outbound write."""

    def apply(self, principal, lead_id, body):
        command = ProfileCommand(body.command_id, principal.workspace_id, lead_id, body.expected_version)
        self._authorize(principal, command, 'crm:lead:edit')
        semantic = _semantic_hash(ACTION, command, body.model_dump(mode='json'))
        replay = self._claim_or_replay(principal, command, semantic)
        if replay is not None:
            return replay
        from src.crm.services.note_plan_service import _locked_company_lead
        from src.crm.services.agent_work_service import WorkConflict
        try:
            lead, _ = _locked_company_lead(self.uow.session, command.workspace_id, lead_id)
        except WorkConflict:
            raise CommandConflictError('profile context locked or changed') from None
        if lead.version != body.expected_version:
            raise CommandConflictError('profile conflict')
        records = _records(self.uow.session, command.workspace_id, lead_id)
        current = _fields(records)
        if isinstance(body, RevertBody):
            original = next((r for r in records if r.command_id == body.revert_command_id), None)
            if original is None:
                raise CommandConflictError('profile conflict')
            field = original.details['field']
            if current[field]['command_id'] != str(original.command_id):
                raise CommandConflictError('profile conflict')
            after = original.details['before']
            if field == 'next_contact':
                from src.crm.services.contact_channel import validate_binding
                if not validate_binding(self.uow.session, command.workspace_id, lead,
                        current[field]['value'], applying=True):
                    raise CommandConflictError('next contact rollback context changed')
        else:
            field, after = body.field, body.value.model_dump(mode='json')
        account_id = str(lead.account_id) if lead.account_id else None
        if field in ('revenue', 'ebitda') and after is not None:
            account = self.uow.session.scalar(select(Account).where(
                Account.workspace_id == command.workspace_id, Account.id == lead.account_id))
            if (account is None or after['entity_account_id'] != account_id
                    or ' '.join(after['reporting_entity'].casefold().split()) != ' '.join(account.display_name.casefold().split())):
                raise CommandConflictError('financial reporting entity mismatch')
        if field in current and current[field]['account_id'] != account_id:
            raise CommandConflictError('profile entity changed')
        if field == 'next_contact' and after is not None:
            from src.crm.services.contact_channel import validate_binding, scope_ids
            # Lock tasks too: a new human callback is not overwritten by a
            # reviewed interpretation of an older source/task snapshot.
            from src.crm.persistence.models import Task
            from src.crm.services.note_plan_service import _locked_rows
            scope = scope_ids(self.uow.session, command.workspace_id, lead)
            _locked_rows(self.uow.session, select(Lead).where(Lead.workspace_id == command.workspace_id,
                Lead.id.in_(scope)))
            _locked_rows(self.uow.session, select(Task).where(Task.workspace_id == command.workspace_id,
                Task.lead_id.in_(scope)))
            if not validate_binding(self.uow.session, command.workspace_id, lead, after, applying=True):
                raise CommandConflictError('next contact source or task context changed')
        before = current.get(field, {}).get('value')
        lead.updated_at = datetime.now(UTC)
        self.uow.session.flush()
        payload = {'schema_version': 1, 'field': field, 'before': before, 'after': after,
            'account_id': account_id, 'version': lead.version,
            'reverts': str(body.revert_command_id) if isinstance(body, RevertBody) else None}
        if len(json.dumps(payload, ensure_ascii=False).encode()) > 3500:
            raise CommandConflictError('profile evidence too large')
        self.uow.audit_events.add(AuditEvent(id=uuid5(command.workspace_id, f'{body.command_id}:audit:{ACTION}'),
            workspace_id=command.workspace_id, command_id=body.command_id, actor_id=principal.actor_id,
            action=ACTION, entity_type='lead', entity_id=lead_id, details=payload))
        enqueue_outbox_event(self.uow, workspace_id=command.workspace_id, command_id=body.command_id,
            semantic_hash=semantic, event_type=ACTION, aggregate_type='lead', aggregate_id=lead_id,
            payload={'lead_id': str(lead_id), 'version': lead.version, 'field': field})
        return LeadOperationResult(body.command_id, lead_id, lead.version, False)
