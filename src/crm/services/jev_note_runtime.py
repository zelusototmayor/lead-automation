"""Explicit, default-OFF, single-outbox runtime. No scheduler or Sales lease.

The budget is a pre-approved suballocation, NOT a new global spending allowance.
Its immutable PG row and per-request reservation are committed with the intent.
"""
from dataclasses import dataclass
import json
import os
import re
import stat
from uuid import UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from src.crm.persistence.models import AgentWork
from src.crm.services.jev_note_policy import digest
from src.crm.services.jev_worker import JevHTTP, LivePermit

RESERVATION_MICROEUR = 5376  # 64k input tokens * $0.042/M * EUR/USD bound 2
BUDGET_KIND = 'jev_note_budget'
BUDGET_KEY = 'jev-note-budget/v1'


@dataclass(frozen=True)
class BudgetGrant:
    ref: str
    microeur: int


def grants_from_file(path, target, workspace_id):
    """Read a private deployment attestation exported by the canonical Ledger.

    The deployer mounts the exact reviewed export read-only after reservation;
    callers never supply this file or grant values through the API.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.getuid() or info.st_size > 32768):
            raise ValueError('grant_file_permissions')
        value = json.load(stream)
    if (set(value) != {'version','target','workspace_id','aggregate_cap_microeur',
            'aggregate_committed_microeur','ledger_snapshot_sha256','grants'}
            or value['version'] != 'jev-budget-grants/v1' or value['target'] != target
            or value['workspace_id'] != str(workspace_id) or value['aggregate_cap_microeur'] != 20_000_000
            or type(value['aggregate_committed_microeur']) is not int
            or not 0 < value['aggregate_committed_microeur'] <= 20_000_000
            or not re.fullmatch('[0-9a-f]{64}',value['ledger_snapshot_sha256'])
            or not isinstance(value['grants'],list) or not 1 <= len(value['grants']) <= 20):
        raise ValueError('grant_evidence_invalid')
    grants = []
    prefix = 'jev-runtime:' + target + ':' + str(workspace_id) + ':'
    for item in value['grants']:
        if (not isinstance(item,dict) or set(item) != {'ref','microeur'}
                or not isinstance(item['ref'],str) or not item['ref'].startswith(prefix)
                or len(item['ref']) > 128 or type(item['microeur']) is not int
                or not RESERVATION_MICROEUR <= item['microeur'] <= 20_000_000):
            raise ValueError('grant_evidence_invalid')
        grants.append(BudgetGrant(**item))
    if (len({g.ref for g in grants}) != len(grants)
            or sum(g.microeur for g in grants) > value['aggregate_committed_microeur']):
        raise ValueError('grant_evidence_invalid')
    return tuple(grants)


def credential_from_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.getuid() or info.st_size > 4096):
            raise ValueError('credential_file_permissions')
        return stream.read().strip()


async def classify_authorized_work(factory, workspace_id, work_id, *, advisory=False, retry=None, personal=False):
    """API gateway: deployment-owned allowlist/budget, never request-owned flags."""
    if os.environ.get('CRM_JEV_ENABLED') != '1':
        return {'status': 'disabled'}
    if personal and (advisory or retry is not None or os.environ.get('CRM_JEV_PERSONAL_NOTES_ENABLED') != '1'):
        return {'status': 'disabled'}
    if advisory and os.environ.get('CRM_JEV_NOTE_ADVISORY_ENABLED') != '1':
        return {'status': 'disabled'}
    try:
        scope = os.environ.get('CRM_JEV_SCOPE', 'allowlist')
        if (str(workspace_id) != os.environ['CRM_JEV_WORKSPACE_ID']
                or scope not in ('allowlist', 'workspace_call_facts')
                or (scope == 'allowlist' and str(work_id) not in os.environ['CRM_JEV_WORK_IDS'].split(','))):
            raise ValueError('runtime_scope_invalid')
        target = os.environ['CRM_JEV_TARGET']
        grants = grants_from_file(os.environ['CRM_JEV_BUDGET_GRANTS_FILE'], target, workspace_id) \
            if os.environ.get('CRM_JEV_BUDGET_GRANTS_FILE') else ()
        # The note route adds no caller-selectable flags or budget. It uses the
        # same deployment-owned allocation with an exact note-only source gate.
        runtime_scope = 'workspace_personal_notes' if personal else 'workspace_note_interest' if advisory else scope
        config = NoteRuntime(enabled=True, target=os.environ['CRM_JEV_TARGET'],
            workspace_id=workspace_id, work_id=work_id,
            policy='personal_notes_v2' if personal else 'interest' if advisory else 'call_facts',
            budget_microeur=int(os.environ['CRM_JEV_BUDGET_MICROEUR']),
            budget_ref=os.environ['CRM_JEV_BUDGET_REF'], review_ref=os.environ['CRM_JEV_REVIEW_REF'],
            privacy_ref=os.environ['CRM_JEV_PRIVACY_REF'], scope=runtime_scope, grants=grants, retry=retry)
        result = await run_once(factory, config, credential=credential_from_file(os.environ['CRM_JEV_CREDENTIAL_FILE']))
        return {k: result[k] for k in ('status', 'receipt_id', 'error', 'usage', 'cost_status',
                                      'model', 'version', 'application') if k in result}
    except (KeyError, ValueError, TypeError, OSError):
        return {'status': 'fallback', 'error': 'runtime_configuration_unavailable'}


def target_identity(session):
    """Non-secret target binding; reject implicit/redirected libpq connections."""
    url = session.get_bind().url
    if (url.drivername != 'postgresql+psycopg' or not url.host or not url.database
            or url.query or any(os.environ.get(k) for k in (
                'PGDATABASE', 'PGHOST', 'PGHOSTADDR', 'PGPASSFILE', 'PGPASSWORD',
                'PGPORT', 'PGSERVICE', 'PGSERVICEFILE', 'PGUSER', 'PGOPTIONS'))):
        raise ValueError('explicit_database_required')
    actual = session.scalar(select(func.current_database()))
    if actual != url.database:
        raise ValueError('database_mismatch')
    return digest({'driver': url.drivername, 'host': url.host, 'port': url.port or 5432,
                   'database': actual, 'user': url.username})


@dataclass(frozen=True)
class NoteRuntime:
    enabled: bool = False
    target: str = ''
    workspace_id: UUID | None = None
    work_id: UUID | None = None
    budget_microeur: int = 0
    budget_ref: str = ''
    review_ref: str = ''
    privacy_ref: str = ''
    policy: str = 'interest'
    scope: str = 'allowlist'
    grants: tuple[BudgetGrant, ...] = ()
    retry: object | None = None

    def validate(self, session, workspace_id):
        if self.retry is not None:
            from src.crm.services.jev_retry import RetryCommand
            if type(self.retry) is not RetryCommand or self.policy != 'call_facts' or self.scope != 'workspace_call_facts':
                raise ValueError('explicit_factual_retry_scope_required')
        if (self.policy not in ('interest', 'call_facts', 'personal_notes', 'personal_notes_v2')
                or self.scope not in ('allowlist','workspace_call_facts','workspace_note_interest','workspace_personal_notes')
                or (self.scope == 'workspace_personal_notes' and self.policy not in ('personal_notes','personal_notes_v2'))
                or (self.policy in ('personal_notes','personal_notes_v2') and self.scope != 'workspace_personal_notes')
                or (self.scope == 'workspace_call_facts' and self.policy != 'call_facts')
                or (self.scope == 'workspace_note_interest' and self.policy != 'interest')
                or self.enabled is not True or type(self.workspace_id) is not UUID
                or type(self.work_id) is not UUID or workspace_id != self.workspace_id
                or type(self.budget_microeur) is not int
                or not RESERVATION_MICROEUR <= self.budget_microeur <= 20_000_000
                or not all(isinstance(s, str) and 0 < len(s) <= 128
                           for s in (self.budget_ref, self.review_ref, self.privacy_ref))
                or self.target != target_identity(session)):
            raise ValueError('runtime_scope_or_budget_invalid')
        prefix = 'jev-runtime:' + self.target + ':' + str(self.workspace_id) + ':'
        if (type(self.grants) is not tuple or len(self.grants) > 20
                or any(type(g) is not BudgetGrant or not isinstance(g.ref,str)
                    or not g.ref.startswith(prefix) or len(g.ref) > 128
                    or type(g.microeur) is not int or not RESERVATION_MICROEUR <= g.microeur <= 20_000_000
                    for g in self.grants)
                or len({g.ref for g in self.grants}) != len(self.grants)
                or self.budget_ref in {g.ref for g in self.grants}
                or self.budget_microeur + sum(g.microeur for g in self.grants) > 20_000_000):
            raise ValueError('runtime_grants_invalid')

    def validate_source(self, session, work):
        if self.scope not in ('workspace_call_facts', 'workspace_note_interest', 'workspace_personal_notes'):
            return
        if (work.workspace_id != self.workspace_id or work.id != self.work_id
                or work.kind != 'call_followup'):
            raise ValueError('work_scope_invalid')
        from src.crm.persistence.models import Activity, Lead
        from src.crm.services.agent_work_service import lead_is_suppressed
        from src.crm.services.note_source_service import note_context
        from src.crm.activity_provenance import operational_activity_filter
        lead = session.scalar(select(Lead).where(Lead.workspace_id == self.workspace_id,
                                                Lead.id == work.lead_id))
        source = note_context(session,self.workspace_id,work.lead_id,work.payload)
        try:
            activity_id = UUID(work.payload['activity_id'])
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ValueError('workspace_call_source_ineligible') from None
        activity_type = 'note' if self.scope == 'workspace_note_interest' else 'call'
        personal = self.scope == 'workspace_personal_notes'
        query = select(Activity).where(Activity.workspace_id == self.workspace_id,
            Activity.id == activity_id, Activity.lead_id == work.lead_id)
        query = query.where(Activity.activity_type.in_(('note','call'))) if personal else query.where(
            Activity.activity_type == activity_type, operational_activity_filter())
        activity = session.scalar(query)
        # Historical interpretation does not unsuppress, enqueue or send anything.
        if (lead is None or (not personal and lead_is_suppressed(session,lead)) or source.get('status') != 'current'
                or activity is None or not activity.summary or not activity.summary.strip()):
            raise ValueError('workspace_call_source_ineligible')

    def accepts_ref(self, ref):
        return ref == self.budget_ref or any(g.ref == ref for g in self.grants)

    def budget(self, session):
        """Lock a fixed tenant budget; changing config cannot reset it."""
        self.validate(session, self.workspace_id)
        return self._budget(session, BUDGET_KEY, self.budget_ref, self.budget_microeur)

    def budgets(self, session):
        # Every worker locks the same legacy row first, serializing grants and
        # receipt replay across processes without resetting the original cap.
        return [self.budget(session)] + [self._budget(session,
            BUDGET_KEY + ':grant:' + digest(g.ref), g.ref, g.microeur)
            for g in sorted(self.grants,key=lambda grant: grant.ref)]

    def _budget(self, session, key, ref, cap):
        contract = {'target': self.target, 'cap_microeur': cap, 'budget_ref': ref}
        session.execute(insert(AgentWork).values(
            id=uuid5(self.workspace_id, key), workspace_id=self.workspace_id,
            source_key=key, kind=BUDGET_KIND, status='waiting',
            payload=contract, result={'reserved_microeur': 0})
            .on_conflict_do_nothing(constraint='uq_agent_work_source'))
        row = session.scalar(select(AgentWork).where(
            AgentWork.workspace_id == self.workspace_id, AgentWork.source_key == key)
            .with_for_update())
        if row.kind != BUDGET_KIND or row.payload != contract:
            raise ValueError('budget_binding_changed')
        if (set(row.result or {}) != {'reserved_microeur'}
                or type(row.result['reserved_microeur']) is not int
                or not 0 <= row.result['reserved_microeur'] <= cap):
            raise ValueError('budget_usage_invalid')
        return row


async def run_once(factory, config=NoteRuntime(), *, credential=None):
    """One confirmed work ID only; production and tests share this exact path."""
    from src.crm.services.jev_note_worker import process_work, preview_hints, apply_hints
    if not config.enabled:
        return {'status': 'disabled', 'decisions': {}}
    try:
        with factory() as session:
            config.validate(session, config.workspace_id)
        http = JevHTTP(credential=credential, permit=LivePermit(
            review_ref=config.review_ref, privacy_ref=config.privacy_ref,
            reservation_ref=config.budget_ref))
        from src.crm.services import jev_call_facts, jose_note_policy, jose_note_units
        result = await process_work(factory, config.workspace_id, config.work_id,
            http=http, flags=jose_note_units.FLAGS if config.policy == 'personal_notes_v2' else
            jose_note_policy.FLAGS if config.policy == 'personal_notes' else
            jev_call_facts.FLAGS if config.policy == 'call_facts' else {config.policy: 'canary'}, runtime=config)
        if config.policy in ('call_facts','personal_notes','personal_notes_v2'):
            # Interpretation only; authenticated canonical note-facts applies it.
            return result
        if result.get('status') == 'succeeded':
            preview = preview_hints(factory, config.workspace_id, result['receipt_id'])
            if 'diff_digest' in preview:
                application = apply_hints(factory, config.workspace_id, result['receipt_id'],
                    expected_diff=preview['diff_digest'], runtime=config)
            else:
                application = preview
            result = dict(result, application=application)
            if application['status'] != 'applied':
                result['decisions'] = {}
        return result
    except (ValueError, SQLAlchemyError):
        return {'status': 'fallback', 'error': 'runtime_unavailable', 'decisions': {}}
