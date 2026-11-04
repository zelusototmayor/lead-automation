"""Secondary contact association from fresh verified SENT, never send authority."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid5

from sqlalchemy import select

from src.crm.connectors.note_drafts import valid_address
from src.crm.persistence.models import Account, AuditEvent, Contact, Lead, SourceIdentity
from src.crm.services.account_service import normalize_email
from src.crm.services.followup_context import collect_context, digest
from src.crm.services.followup_review import FollowupError, timestamp
from src.crm.services.followup_service import mailbox_scope, messages
from src.crm.services.note_contacts import _resolve_contact
from src.crm.services.note_plan_service import _locked_company_lead, _locked_rows

SCHEMA = 'sent-secondary-contact/v1'
MAX_CONTEXT_AGE = timedelta(minutes=15)


def _matches_link(link, workspace, lead_id, account_id, contact_id, mailbox, email, source):
    metadata = link.metadata_json or {}
    literal = metadata.get('literal_email')
    return bool(link.workspace_id == workspace
        and link.source_system == 'gmail' and link.entity_kind == 'document'
        and link.source_scope == 'sent-contact-link/v1'
        and link.canonical_entity_type == 'contact' and link.canonical_entity_id == contact_id
        and link.external_id == str(lead_id)+':'+mailbox+':'+source['message_id']+':'+normalize_email(email)
        and metadata.get('schema') == SCHEMA
        and metadata.get('lead_id') == str(lead_id)
        and metadata.get('account_id') == str(account_id)
        and metadata.get('mailbox') == mailbox
        and isinstance(literal, str) and literal in source['to']
        and literal.casefold() == email.casefold()
        and metadata.get('source') == source)


def associate(session, principal, body, provider):
    mailbox_scope(principal, body.mailbox)
    request_hash = digest(body.model_dump(mode='json'))
    command = uuid5(body.lead_id, 'sent-contact:' + str(body.command_id))
    # Same canonical command/audit replay fence as note-contacts, before I/O.
    previous = session.get(AuditEvent, command)
    if previous:
        if (previous.workspace_id != principal.workspace_id
                or previous.actor_id != principal.actor_id
                or previous.action != 'agent.sent_contact_completed'
                or previous.details.get('request_hash') != request_hash):
            raise FollowupError('sent_contact_command_changed')
        return previous.details['receipt']
    age = datetime.now(UTC) - body.context_observed_at
    if not timedelta(0) <= age <= MAX_CONTEXT_AGE:
        raise FollowupError('sent_contact_context_expired')
    lead, _ = _locked_company_lead(session, principal.workspace_id, body.lead_id)
    if lead.account_id != body.account_id or lead.version != body.expected_lead_version:
        raise FollowupError('sent_contact_identity_changed')
    account = session.get(Account, lead.account_id)
    if not account or account.version != body.expected_account_version:
        raise FollowupError('sent_contact_identity_changed')
    contacts = _locked_rows(session, select(Contact).where(
        Contact.workspace_id == principal.workspace_id,
        Contact.account_id == lead.account_id).order_by(Contact.id))
    # Explicit thread binding includes the not-yet-mapped address. Do not call
    # verified_anchor here: its mapping guard remains mandatory for reconcile.
    context = collect_context(session, principal.workspace_id, lead.id,
                              body.mailbox, provider, thread_ids=(body.source.thread_id,))
    coverage = context.get('coverage') or {}
    if (any(coverage.get(key) is not True for key in (
            'complete','crm_complete','gmail_complete','direct_drafts_complete','related_threads_complete'))
            or coverage.get('coverage_id') != body.coverage_id
            or context.get('context_hash') != body.context_hash
            or body.coverage_id != body.context_hash
            or context['crm']['lead']['id'] != str(lead.id)
            or context['crm']['lead']['account_id'] != str(lead.account_id)
            or context['source_versions']['lead_version'] != body.expected_lead_version):
        raise FollowupError('sent_contact_context_changed')
    found = [m for m in messages(context) if m['id'] == body.source.message_id]
    if len(found) != 1:
        raise FollowupError('sent_contact_source_ambiguous')
    source = found[0]
    source_threads = [t for t in context['provider']['threads'] if t['id'] == body.source.thread_id]
    if (len(source_threads) != 1
            or not any(m == source for m in source_threads[0]['messages'])):
        raise FollowupError('sent_contact_source_mismatch')
    if ('SENT' not in source['labels'] or 'DRAFT' in source['labels']
            or source['from'] != [body.mailbox]
            or context['provider']['mailbox'] != body.mailbox
            or not source['headers'].get('message-id')
            or timestamp(source['sent_at']) > datetime.now(UTC)
            or source['thread_id'] != body.source.thread_id
            or digest(source) != body.source.message_sha256
            or source['to'] != body.source.to or source['cc'] != body.source.cc
            or source['headers'].get('to','') != body.source.header_to
            or source['headers'].get('cc','') != body.source.header_cc
            or body.email not in source['to']):
        raise FollowupError('sent_contact_source_mismatch')
    valid_address(body.email)
    known = [lead.contact_email] + [c.primary_email for c in contacts if c.id == lead.contact_id]
    if not ({normalize_email(v) for v in known if v}
            & {normalize_email(v) for v in source['to'] + source['cc']}):
        raise FollowupError('sent_contact_lead_mapping_unproven')
    # Provider I/O can outlive the command's context; fence the mutation too.
    age = datetime.now(UTC) - body.context_observed_at
    if not timedelta(0) <= age <= MAX_CONTEXT_AGE:
        raise FollowupError('sent_contact_context_expired')
    # Shared contact identity resolver: exact email match only, never empty slots,
    # name inference, primary reassignment, or a replacement of Paulo's details.
    change = SimpleNamespace(email=body.email, contact_id=None, full_name=None)
    contact, before = _resolve_contact(session, principal, lead, command, 0, change, contacts)
    if before is not None:
        if contact.id == lead.contact_id or contact.is_primary or contact.status != 'active':
            raise FollowupError('sent_contact_not_secondary')
    else:
        contact.primary_email = body.email
    session.flush()
    provenance = body.source.model_dump(mode='json')
    link_id = uuid5(lead.id, 'sent-contact-link:' + digest({
        'mailbox':body.mailbox,'message_id':body.source.message_id,
        'email':normalize_email(body.email)}))
    link = session.get(SourceIdentity, link_id)
    if link is not None:
        if not _matches_link(link, principal.workspace_id, lead.id, lead.account_id,
                             contact.id, body.mailbox, body.email, provenance):
            raise FollowupError('sent_contact_provenance_changed')
    else:
        link = SourceIdentity(id=link_id,workspace_id=principal.workspace_id,
            source_system='gmail',entity_kind='document',source_scope='sent-contact-link/v1',
            external_id=str(lead.id)+':'+body.mailbox+':'+body.source.message_id+':'+normalize_email(body.email),
            canonical_entity_type='contact',canonical_entity_id=contact.id,
            metadata_json={'schema':SCHEMA,'lead_id':str(lead.id),'account_id':str(lead.account_id),
                'mailbox':body.mailbox,'literal_email':body.email,'source':provenance,
                'coverage_id':body.coverage_id,'context_hash':body.context_hash,
                'source_versions':context['source_versions'],
                'context_observed_at':body.context_observed_at.isoformat(),
                'verified_at':datetime.now(UTC).isoformat()})
        session.add(link)
    receipt = {'schema':SCHEMA,'command_id':str(body.command_id),'lead_id':str(lead.id),
        'account_id':str(lead.account_id),'mailbox':body.mailbox,
        'contact_id':str(contact.id),'contact_version':contact.version,
        'created':before is None,'email':str(contact.primary_email),'make_primary':False,
        'source_identity_id':str(link_id),'source':provenance,
        'context_hash':body.context_hash,'coverage_id':body.coverage_id,
        'lead_version':lead.version,'account_version':account.version,
        'operational_contact_id':str(lead.contact_id) if lead.contact_id else None,
        'external_actions':'none','send_authority':False}
    session.add(AuditEvent(id=command,workspace_id=principal.workspace_id,command_id=command,
        actor_id=principal.actor_id,action='agent.sent_contact_completed',
        entity_type='lead',entity_id=lead.id,details={'request_hash':request_hash,'receipt':receipt}))
    session.flush()
    return receipt


def readback(session, principal, lead_id, command_id):
    """CRM-only verification; a stored receipt is not proof of present state."""
    command = uuid5(lead_id, 'sent-contact:' + str(command_id))
    event = session.get(AuditEvent, command)
    if (not event or event.workspace_id != principal.workspace_id
            or event.entity_id != lead_id or event.action != 'agent.sent_contact_completed'):
        raise FollowupError('sent_contact_command_missing', 404)
    receipt = event.details['receipt']
    mailbox_scope(principal, receipt['mailbox'])
    contact = session.scalar(select(Contact).where(Contact.workspace_id == principal.workspace_id,
        Contact.id == UUID(receipt['contact_id'])))
    lead = session.scalar(select(Lead).where(Lead.workspace_id == principal.workspace_id, Lead.id == lead_id))
    link = session.get(SourceIdentity, UUID(receipt['source_identity_id']))
    verified = bool(contact and lead and link
        and str(contact.account_id) == receipt['account_id'] == str(lead.account_id)
        and contact.version == receipt['contact_version']
        and str(contact.primary_email) == receipt['email']
        and not contact.is_primary and contact.status == 'active'
        and str(lead.contact_id) == receipt['operational_contact_id']
        and _matches_link(link, principal.workspace_id, lead.id, lead.account_id,
                          contact.id, receipt['mailbox'], receipt['email'], receipt['source']))
    return {'receipt':receipt,'verified':verified,
        'current_contact':({'contact_id':str(contact.id),'version':contact.version,
            'primary_email':str(contact.primary_email) if contact.primary_email else None,
            'full_name':contact.full_name,'title':contact.title,'phone':contact.phone,
            'is_primary':contact.is_primary,'status':contact.status} if contact else None),
        'current_operational_contact_id':str(lead.contact_id) if lead and lead.contact_id else None}
